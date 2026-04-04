from __future__ import annotations

import asyncio
import logging
import queue
import typing as T

from typing import Any

_LOGGER = logging.getLogger(__package__)

from bleak import BleakClient
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from bleak_retry_connector import establish_connection, BleakOutOfConnectionSlotsError

from homeassistant.components.light import (
    ColorMode,
    LightEntity,
    LightEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import (
    AddEntitiesCallback,
)
from homeassistant.exceptions import ConfigEntryError

from .const import DOMAIN
from .coordinator import GoveePlugDataUpdateCoordinator
from .entity import GoveePlugEntity


# Shared helper functions
def _b(s: str):
    return bytes(bytearray.fromhex(s))


def _sign_payload(data):
    checksum = 0
    for b in data:
        checksum ^= b
    return checksum & 0xFF


# H6163 Light Device API
class GoveePlugH6xxx:
    def __init__(
        self,
        device: BLEDevice,
        token: str,  # Token is ignored for H6xxx series
        RECV_CHARACTERISTIC_UUID: str,
        SEND_CHARACTERISTIC_UUID: str,
    ) -> None:
        self._device = device
        self._RECV_CHARACTERISTIC_UUID = RECV_CHARACTERISTIC_UUID
        self._SEND_CHARACTERISTIC_UUID = SEND_CHARACTERISTIC_UUID

        self._connection_task: T.Optional[asyncio.Task] = None
        self._msgqueue = asyncio.Queue[T.Tuple[bytes, asyncio.Future[bool]]]()
        self._connection_lock = asyncio.Lock()

    async def _send_message(self, msg: bytes) -> bool:
        f = asyncio.Future[bool]()
        self._msgqueue.put_nowait((msg, f))
        self._ensure_message_task()
        return await f

    def _ensure_message_task(self):
        # Use a synchronous check to avoid race conditions
        # The lock is only held during task creation, not during execution
        if self._connection_task is None:
            self._connection_task = asyncio.create_task(self._message_task_fn())
            self._connection_task.add_done_callback(self._message_task_done)

    def _message_task_done(self, task: asyncio.Task):
        try:
            task.result()
        except Exception:
            # if this failed, it was logged or failed while disconnecting
            pass

        if self._connection_task is task:
            self._connection_task = None

        if self._connection_task is None and not self._msgqueue.empty():
            self._ensure_message_task()

    async def _message_task_fn(self):
        client = None
        must_process = queue.Queue[T.Tuple[bytes, asyncio.Future]]()
        device_name = f"{self._device.name} ({self._device.address})"

        try:
            # Pull anything on the message queue directly off, these must
            # be processed one way or another
            while not self._msgqueue.empty():
                must_process.put(self._msgqueue.get_nowait())

            # Use connection lock to prevent concurrent connection attempts
            async with self._connection_lock:
                try:
                    client = await establish_connection(
                        BleakClient,
                        self._device,
                        device_name,
                        max_attempts=2,  # Reduced to 2 to fail faster and free slots
                        connection_timeout=10.0,  # 10 second timeout per attempt
                    )
                except BleakOutOfConnectionSlotsError as e:
                    _LOGGER.error(
                        "failed to set state: %s - No available connection slots. "
                        "Please disconnect unused devices or add more BLE proxies.",
                        device_name
                    )
                    # Mark all pending messages as failed
                    while not must_process.empty():
                        _, f = must_process.get_nowait()
                        f.set_result(False)
                    return
                except Exception as e:
                    _LOGGER.error("failed to connect to %s: %s", device_name, e)
                    # Mark all pending messages as failed
                    while not must_process.empty():
                        _, f = must_process.get_nowait()
                        f.set_result(False)
                    return

            async def _send_msg(msg: bytes, f: asyncio.Future):
                try:
                    await client.write_gatt_char(self._SEND_CHARACTERISTIC_UUID, msg)
                except Exception:
                    f.set_result(False)
                    raise
                else:
                    f.set_result(True)

            # Process must process entries first
            while not must_process.empty():
                msg, f = must_process.get_nowait()
                await _send_msg(msg, f)

            # Then process anything else that might be in the queue
            while True:
                try:
                    msg, f = await asyncio.wait_for(self._msgqueue.get(), timeout=1)
                except TimeoutError:
                    break
                else:
                    await _send_msg(msg, f)

            # H6xxx devices don't use notifications, so don't try to stop

        except Exception as e:
            _LOGGER.error("failed to set state for %s: %s", device_name, e)
        finally:
            # We only force clearing the must process queue. Anything that
            # was queued while the connection was failing deserves another try
            # and will be requeued when this task's done callback is called
            while not must_process.empty():
                _, f = must_process.get_nowait()
                f.set_result(False)

            if client is not None:
                try:
                    # Ensure we disconnect to free up the connection slot
                    if client.is_connected:
                        await client.disconnect()
                    # Give a small delay to ensure the slot is released
                    await asyncio.sleep(0.1)
                except Exception as e:
                    _LOGGER.debug("Error disconnecting %s: %s", device_name, e)


class GoveePlugH6163(GoveePlugH6xxx):
    MODEL = "H6163"

    MSG_TURN_ON = _b("3301010000000000000000000000000000000033")
    MSG_TURN_OFF = _b("3301000000000000000000000000000000000032")
    MSG_QUERY_STATUS = _b("3300000000000000000000000000000000000033")

    SEND_CHARACTERISTIC_UUID = "00010203-0405-0607-0809-0a0b0c0d2b11"
    RECV_CHARACTERISTIC_UUID = "00010203-0405-0607-0809-0a0b0c0d2b10"

    def __init__(self, device: BLEDevice, token: str) -> None:
        super().__init__(
            device, token, self.RECV_CHARACTERISTIC_UUID, self.SEND_CHARACTERISTIC_UUID
        )
        self._is_on = None
        self._rgb: T.Optional[tuple[int, int, int]] = None
        self._brightness: T.Optional[int] = None
        self._last_brightness: int = 255  # Track last non-zero brightness for restore
        self._effect: T.Optional[str] = "normal"

    def port_names(self) -> T.List[T.Tuple[T.Optional[int], T.Optional[str]]]:
        # H6163 is a light device, not a plug - no switch entities
        return []

    def is_on(self, port: int):
        return self._is_on

    def handle_bluetooth_event(self, device: BLEDevice, adv: AdvertisementData):
        old_state = self._is_on
        
        _LOGGER.debug(
            "H6163 handle_bluetooth_event called for %s, manufacturer_data present: %s",
            device.address,
            adv.manufacturer_data is not None and len(adv.manufacturer_data) > 0
        )
        
        # Ensure device is initialized with default brightness on first discovery
        # This makes entity available even without manufacturer data
        if self._brightness is None:
            self._brightness = 255
            _LOGGER.debug(
                "H6163 %s: Device discovered, initializing brightness to %d (no state data yet)",
                device.address,
                self._brightness
            )
        
        if not adv.manufacturer_data:
            _LOGGER.debug(
                "H6163 %s: No manufacturer_data in advertisement (mfr_data=%s)",
                device.address,
                adv.manufacturer_data
            )
            return
        
        for mfr_id, mfr_data in adv.manufacturer_data.items():
            _LOGGER.debug(
                "H6163 %s: Received manufacturer data - mfr_id=%d(0x%04x), data=%s, len=%d",
                device.address,
                mfr_id,
                mfr_id,
                mfr_data.hex(),
                len(mfr_data)
            )
            self._device = device
            if len(mfr_data) > 0:
                new_state = mfr_data[-1] == 0x01
                self._is_on = new_state
                
                # Update brightness based on manufacturer data state
                if new_state is False and old_state is True:
                    # Device turned off - sync brightness to 0
                    self._brightness = 0
                    _LOGGER.debug(
                        "H6163 %s: Synced brightness to 0 (device turned off)",
                        device.address
                    )

                if old_state != new_state:
                    _LOGGER.info(
                        "H6163 %s: State changed from advertisement - is_on=%s (was=%s, mfr_data=%s)",
                        device.address,
                        new_state,
                        old_state,
                        mfr_data.hex()
                    )
                else:
                    _LOGGER.debug(
                        "H6163 %s: State updated from advertisement - is_on=%s (mfr_data=%s)",
                        device.address,
                        new_state,
                        mfr_data.hex()
                    )

    async def async_turn_on(self, port: int):
        assert port == 0
        if await self._send_message(self.MSG_TURN_ON):
            self._is_on = True

    async def async_turn_off(self, port: int):
        assert port == 0
        if await self._send_message(self.MSG_TURN_OFF):
            self._is_on = False

    def has_light(self) -> bool:
        return True

    def get_light_state(self) -> T.Tuple[T.Optional[tuple[int, int, int]], T.Optional[int]]:
        _LOGGER.debug(
            "H6163 get_light_state: rgb=%s, brightness=%s",
            self._rgb,
            self._brightness
        )
        return self._rgb, self._brightness

    async def async_set_light_rgb(self, rgb: tuple[int, int, int]) -> None:
        """Set RGB color. RGB values should be in range 0-255."""
        red, green, blue = rgb

        # Create RGB message: [0x33, 0x05, 0x02, RED, GREEN, BLUE, 0x00, 0xFF, 0xAE, 0x54, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00]
        msg = bytearray([0x33, 0x05, 0x02, red, green, blue, 0x00, 0xFF, 0xAE, 0x54,
                         0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00])

        # Append XOR checksum
        msg.append(_sign_payload(msg))

        if await self._send_message(bytes(msg)):
            self._rgb = rgb

    async def async_set_light_brightness(self, brightness: int) -> None:
        """Set brightness. Brightness should be in range 0-255."""
        # Create brightness message: [0x33, 0x04, BRIGHTNESS, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00]
        msg = bytearray([0x33, 0x04, brightness] + [0x00] * 16)

        # Append XOR checksum
        msg.append(_sign_payload(msg))

        if await self._send_message(bytes(msg)):
            self._brightness = brightness
            # Remember last non-zero brightness for restore when turning back on
            if brightness > 0:
                self._last_brightness = brightness

    def get_effect(self) -> T.Optional[str]:
        return self._effect

    async def async_set_effect(self, effect: str) -> None:
        """Set effect mode. Returns None if effect is not recognized."""
        # Effect mappings
        effects = {
            "normal": _b("3301010000000000000000000000000000000033"),
            "music_energetic": _b("3305010000000000000000000000000000000037"),
            "music_spectrum_red": _b("3305010100ff00000000000000000000000000c9"),
            "music_spectrum_blue": _b("33050101000000ff0000000000000000000000c9"),
            "music_rolling_red": _b("33050102ff0000000000000000000000000000ca"),
            "music_rolling_blue": _b("330501020000ff000000000000000000000000ca"),
            "music_rhythm": _b("3305010300000000000000000000000000000034"),
            "scene_sunrise": _b("3305040000000000000000000000000000000032"),
            "scene_sunset": _b("3305040100000000000000000000000000000033"),
            "scene_movie": _b("3305040400000000000000000000000000000036"),
            "scene_dating": _b("3305040500000000000000000000000000000037"),
            "scene_romantic": _b("3305040700000000000000000000000000000035"),
            "scene_blinking": _b("330504080000000000000000000000000000003a"),
            "scene_candlelight": _b("330504090000000000000000000000000000003b"),
            "scene_snowflake": _b("3305040f0000000000000000000000000000003d"),
        }

        if effect not in effects:
            return

        if await self._send_message(effects[effect]):
            self._effect = effect

    async def async_query_status(self) -> None:
        """Query the current status of the device."""
        client = None
        device_name = f"{self._device.name} ({self._device.address})"
        status_data = [None]

        try:
            async with self._connection_lock:
                try:
                    client = await establish_connection(
                        BleakClient,
                        self._device,
                        device_name,
                        max_attempts=1,  # Single attempt for polling
                        connection_timeout=5.0,  # Shorter timeout for polling
                    )
                except (BleakOutOfConnectionSlotsError, Exception) as e:
                    _LOGGER.debug("failed to connect for status query to %s: %s", device_name, e)
                    return

            on_status_ready = asyncio.Event()

            async def recv_handler(c, data):
                if data[0] == 0x33 and data[1] == 0x01:
                    # Status response received
                    status_data[0] = data
                    on_status_ready.set()

            await client.start_notify(self._RECV_CHARACTERISTIC_UUID, recv_handler)

            # H6163 doesn't require authentication, send query directly
            await client.write_gatt_char(self._SEND_CHARACTERISTIC_UUID, self.MSG_QUERY_STATUS)

            try:
                await asyncio.wait_for(on_status_ready.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                _LOGGER.debug("Status response timeout for %s", device_name)
                return

            # Parse status from response if available
            if status_data[0] and len(status_data[0]) >= 3:
                if len(status_data[0]) >= 20:
                    self._is_on = status_data[0][-1] == 0x01

        except Exception as e:
            _LOGGER.debug("Error querying status for %s: %s", device_name, e)
        finally:
            if client is not None:
                try:
                    if client.is_connected:
                        await client.disconnect()
                    await asyncio.sleep(0.1)
                except Exception as e:
                    _LOGGER.debug("Error disconnecting %s: %s", device_name, e)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up govee light based on a config entry."""
    entry_data = hass.data[DOMAIN][entry.entry_id]
    coordinator: GoveePlugDataUpdateCoordinator = entry_data["coordinator"]

    # Only add light entity if the device supports it
    if coordinator.api and coordinator.api.has_light():
        # Pass None, None for port and port_name since lights aren't port-based
        async_add_entities([GoveePlugLight(coordinator, entry, None, None)])
    elif not coordinator.api:
        # Device not found yet, but still add the light entity with unavailable status
        # Check if the model supports lights based on the entry data
        model = entry_data.get("model", "")
        if model == "H6163":
            async_add_entities([GoveePlugLight(coordinator, entry, None, None)])


class GoveePlugLight(GoveePlugEntity, LightEntity):
    """Govee light class."""

    _attr_supported_color_modes = {ColorMode.RGB}
    _attr_supported_features = LightEntityFeature.EFFECT
    _attr_translation_key = "led"
    _attr_effect_list = [
        "normal",
        "music_energetic",
        "music_spectrum_red",
        "music_spectrum_blue",
        # "music_rolling_red",
        # "music_rolling_blue",
        "music_rhythm",
        "scene_sunrise",
        "scene_sunset",
        "scene_movie",
        "scene_dating",
        "scene_romantic",
        "scene_blinking",
        "scene_candlelight",
        "scene_snowflake",
    ]

    @property
    def color_mode(self) -> ColorMode:
        """Return the color mode of the light."""
        return ColorMode.RGB

    @property
    def available(self) -> bool:
        """Return if entity is available.

        Light is available when API is initialized and we have valid state data.
        """
        if not self.coordinator.api:
            _LOGGER.debug("GoveePlugLight.available: API not initialized, returning False")
            return False

        # Light is available if we have brightness data
        _, brightness = self.coordinator.api.get_light_state()
        is_available = brightness is not None
        _LOGGER.debug(
            "GoveePlugLight.available: brightness=%s, is_available=%s",
            brightness,
            is_available
        )
        return is_available

    @property
    def rgb_color(self) -> tuple[int, int, int] | None:
        """Return the rgb color value."""
        if not self.coordinator.api:
            return None
        rgb, _ = self.coordinator.api.get_light_state()
        return rgb

    @property
    def brightness(self) -> int | None:
        """Return the brightness of this light between 0..255."""
        if not self.coordinator.api:
            return None
        _, brightness = self.coordinator.api.get_light_state()
        return brightness

    @property
    def effect(self) -> str | None:
        """Return the current effect."""
        if not self.coordinator.api:
            return None
        return self.coordinator.api.get_effect()

    @property
    def is_on(self) -> bool | None:
        """Return true if light is on."""
        if not self.coordinator.api:
            return None
        # Light is considered on if brightness is set
        _, brightness = self.coordinator.api.get_light_state()
        return brightness is not None and brightness > 0

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on or control the light."""
        if not self.coordinator.api:
            return

        rgb, brightness = self.coordinator.api.get_light_state()

        # If an effect is requested, set it and return (effects handle their own brightness/color)
        if effect := kwargs.get("effect"):
            await self.coordinator.api.async_set_effect(effect)
            self.async_write_ha_state()
            return

        # Determine new RGB value
        new_rgb = kwargs.get("rgb_color", rgb)
        if new_rgb is None:
            # Default to white if no color is set
            new_rgb = (255, 255, 255)

        # Determine new brightness
        # If brightness is not specified, use last known good brightness (or 255 if none)
        new_brightness = kwargs.get("brightness")
        if new_brightness is None:
            # Use the last non-zero brightness if available, otherwise default to 255
            new_brightness = self.coordinator.api._last_brightness if hasattr(self.coordinator.api, '_last_brightness') else 255

        # If brightness is 0, turn off instead
        if new_brightness == 0:
            await self.async_turn_off()
            return

        # Update RGB if changed
        if new_rgb != rgb:
            await self.coordinator.api.async_set_light_rgb(new_rgb)

        # Update brightness if changed
        if new_brightness != brightness:
            await self.coordinator.api.async_set_light_brightness(new_brightness)

        # Clear effect when setting manual color/brightness
        await self.coordinator.api.async_set_effect("normal")

        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off the light."""
        if not self.coordinator.api:
            return

        # Use the proper turn off command instead of setting brightness to 0
        await self.coordinator.api.async_turn_off(0)
        # Update brightness state to reflect off
        self.coordinator.api._brightness = 0
        self.async_write_ha_state()
