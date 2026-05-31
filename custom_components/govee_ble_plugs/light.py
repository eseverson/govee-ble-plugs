from __future__ import annotations

import asyncio
import logging
import typing as T

from typing import Any

_LOGGER = logging.getLogger(__package__)

# Keep the connection open only briefly after the last command. A rapid drag
# (writes <1s apart) keeps resetting this and reuses one connection, but we let
# it go quickly when idle: this bulb drops the link itself after ~2s, and a
# longer hold just leaves a dead client whose writes fail with "characteristic
# not found" and silently lose the command (e.g. a turn-off that never applies).
IDLE_DISCONNECT_SECONDS = 1
# Hard cap on one connect cycle. bleak-retry-connector ignores max_attempts for
# "transient" proxy errors and can retry ~9 times (~25s), stalling every queued
# command behind it; this bounds that.
CONNECT_TIMEOUT_SECONDS = 12
# How many times to (re)connect and rewrite a single command before giving up.
# The bulb drops idle connections, so a write can hit a stale client; retrying
# on a fresh connection keeps commands from being silently lost.
MAX_COMMAND_ATTEMPTS = 3

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
from homeassistant.util.color import color_temperature_to_rgb

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
        device_name = f"{self._device.name} ({self._device.address})"
        client = None

        async def _ensure_client():
            """Return a live client, (re)connecting if needed. Raises on failure."""
            nonlocal client
            if client is not None and client.is_connected:
                return client
            if client is not None:
                try:
                    await client.disconnect()
                except Exception:
                    pass
                client = None
            async with self._connection_lock:
                client = await asyncio.wait_for(
                    establish_connection(
                        BleakClient, self._device, device_name, max_attempts=1
                    ),
                    timeout=CONNECT_TIMEOUT_SECONDS,
                )
            return client

        async def _deliver(msg: bytes, f: asyncio.Future):
            """Write a command, reconnecting and retrying on failure.

            The bulb drops idle connections, so a write can hit a stale client
            ("characteristic not found"). Retrying on a fresh connection keeps
            commands from being silently lost; we give up (logged) after
            MAX_COMMAND_ATTEMPTS rather than hanging or dropping silently.
            """
            nonlocal client
            last_err = None
            for attempt in range(1, MAX_COMMAND_ATTEMPTS + 1):
                try:
                    c = await _ensure_client()
                    await c.write_gatt_char(self._SEND_CHARACTERISTIC_UUID, msg)
                except Exception as e:
                    last_err = e
                    _LOGGER.debug(
                        "H6163 %s: attempt %d/%d failed for %s: %s",
                        device_name, attempt, MAX_COMMAND_ATTEMPTS, msg.hex(), e,
                    )
                    client = None  # force a fresh connection on the next attempt
                    continue
                else:
                    if not f.done():
                        f.set_result(True)
                    return
            _LOGGER.error(
                "H6163 %s: gave up on command %s after %d attempts: %s",
                device_name, msg.hex(), MAX_COMMAND_ATTEMPTS, last_err,
            )
            if not f.done():
                f.set_result(False)

        try:
            # Drain whatever is already queued, then keep serving briefly so an
            # active drag reuses the warm connection instead of reconnecting.
            while not self._msgqueue.empty():
                msg, f = self._msgqueue.get_nowait()
                await _deliver(msg, f)

            while True:
                try:
                    msg, f = await asyncio.wait_for(
                        self._msgqueue.get(), timeout=IDLE_DISCONNECT_SECONDS
                    )
                except TimeoutError:
                    break
                await _deliver(msg, f)
        finally:
            if client is not None:
                try:
                    if client.is_connected:
                        await client.disconnect()
                    await asyncio.sleep(0.1)
                except Exception:
                    pass


class GoveePlugH6163(GoveePlugH6xxx):
    MODEL = "H6163"

    MSG_TURN_ON = _b("3301010000000000000000000000000000000033")
    MSG_TURN_OFF = _b("3301000000000000000000000000000000000032")
    MSG_QUERY_STATUS = _b("3300000000000000000000000000000000000033")

    MSG_KEEP_ALIVE = _b("aa010000000000000000000000000000000000ab")
    MSG_GET_BRIGHTNESS = _b("aa040000000000000000000000000000000000ae")
    MSG_GET_COLOR = _b("aa050100000000000000000000000000000000ae")

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
        self._effect: T.Optional[str] = "Normal"
        self._color_temp_kelvin: T.Optional[int] = None
        self._color_mode: str = "rgb"  # active mode: "rgb" or "color_temp"

    def port_names(self) -> T.List[T.Tuple[T.Optional[int], T.Optional[str]]]:
        # H6163 is a light device, not a plug - no switch entities
        return []

    def is_on(self, port: int):
        return self._is_on

    def handle_bluetooth_event(self, device: BLEDevice, adv: AdvertisementData):
        # The H6163 doesn't report usable state in its advertisements; on/off,
        # brightness and colour are tracked optimistically and seeded via
        # async_query_status. Just keep the BLEDevice reference fresh.
        self._device = device

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
        return self._rgb, self._brightness

    async def async_set_light_rgb(self, rgb: tuple[int, int, int]) -> None:
        """Set RGB color. RGB values should be in range 0-255."""
        red, green, blue = rgb

        # Create RGB message: [0x33, 0x05, 0x02, RED, GREEN, BLUE, 0x00, 0xFF, 0xAE, 0x54, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00]
        msg = bytearray([0x33, 0x05, 0x02, red, green, blue, 0x00, 0xFF, 0xAE, 0x54]
                         + [0x00] * 9)

        # Append XOR checksum
        msg.append(_sign_payload(msg))

        if await self._send_message(bytes(msg)):
            self._rgb = rgb
            self._color_mode = "rgb"
            self._color_temp_kelvin = None

    def get_color_mode(self) -> str:
        """Return the active color mode: 'rgb' or 'color_temp'."""
        return self._color_mode

    def get_color_temp_kelvin(self) -> T.Optional[int]:
        return self._color_temp_kelvin

    async def async_set_light_color_temp(self, kelvin: int) -> None:
        """Set white color temperature (Kelvin).

        The H6163 has no dedicated white channel exposed over BLE, so the
        temperature is sent as a manual-mode (0x05/0x02) color whose RGB is
        computed from the Kelvin value. Packet form 0x33 0x05 0x02 FF FF FF 01
        R G B is corroborated by wez/govee-py and chvolkmann/govee_btled.
        """
        red, green, blue = (int(c) for c in color_temperature_to_rgb(kelvin))
        msg = bytearray(
            [0x33, 0x05, 0x02, 0xFF, 0xFF, 0xFF, 0x01, red, green, blue]
            + [0x00] * 9
        )
        msg.append(_sign_payload(msg))
        _LOGGER.debug(
            "H6163 set color_temp %dK -> rgb=(%d,%d,%d) packet=%s",
            kelvin, red, green, blue, msg.hex(),
        )
        if await self._send_message(bytes(msg)):
            self._color_temp_kelvin = kelvin
            self._rgb = (red, green, blue)
            self._color_mode = "color_temp"

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
            "Normal": _b("3301010000000000000000000000000000000033"),
            "Music - Energetic": _b("3305010000000000000000000000000000000037"),
            "Music - Spectrum (Red)": _b("3305010100ff00000000000000000000000000c9"),
            "Music - Spectrum (Blue)": _b("33050101000000ff0000000000000000000000c9"),
            "Music - Rolling (Red)": _b("33050102ff0000000000000000000000000000ca"),
            "Music - Rolling (Blue)": _b("330501020000ff000000000000000000000000ca"),
            "Music - Rhythm": _b("3305010300000000000000000000000000000034"),
            "Sunrise": _b("3305040000000000000000000000000000000032"),
            "Sunset": _b("3305040100000000000000000000000000000033"),
            "Movie": _b("3305040400000000000000000000000000000036"),
            "Dating": _b("3305040500000000000000000000000000000037"),
            "Romantic": _b("3305040700000000000000000000000000000035"),
            "Blinking": _b("330504080000000000000000000000000000003a"),
            "Candlelight": _b("330504090000000000000000000000000000003b"),
            "Snowflake": _b("3305040f0000000000000000000000000000003d"),
        }

        if effect not in effects:
            return

        if await self._send_message(effects[effect]):
            self._effect = effect

    async def async_query_status(self) -> bool:
        """Query current status. Returns True if state (brightness) was seeded."""
        client = None
        device_name = f"{self._device.name} ({self._device.address})"
        got_state = False

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
                    return False

            on_status_ready = asyncio.Event()

            async def recv_handler(c, data):
                _LOGGER.debug("Received data in status query from %s: %s", device_name, data.hex())
                if data[0] == 0xaa and data[1] == 0x04:
                    # Brightness response received (aa04[brightness]...)
                    self._brightness = data[2]
                    _LOGGER.debug("Brightness response: %d", self._brightness)
                    on_status_ready.set()
                elif data[0] == 0xaa and data[1] == 0x05:
                    # Color response received (aa05[xx][R][G][B]...)
                    self._rgb = (data[3], data[4], data[5])
                    _LOGGER.debug("Color response: rgb=%s, data[2]=%d", self._rgb, data[2])
                    on_status_ready.set()
                elif data[0] == 0xaa and data[1] == 0x01:
                    _LOGGER.debug("On/off response received for %s: is_on=%s", device_name, data[2] == 0x01)
                    self._is_on = data[2] == 0x01
                    on_status_ready.set()
                else:
                    _LOGGER.debug("Unexpected data format in status query response from %s: %s", device_name, data.hex())

            _LOGGER.debug("listening to uuid %s for status response from %s", self._RECV_CHARACTERISTIC_UUID, device_name)
            await client.start_notify(self._RECV_CHARACTERISTIC_UUID, recv_handler)

            # H6163 doesn't require authentication, send query directly
            await client.write_gatt_char(0x15, self.MSG_GET_BRIGHTNESS, response=False)

            try:
                await asyncio.wait_for(on_status_ready.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                _LOGGER.debug("Status response timeout for %s", device_name)
                return False

            got_state = True
            on_status_ready.clear()
            _LOGGER.debug("Brightness query successful, now querying color for %s", device_name)
            await client.write_gatt_char(0x15, self.MSG_GET_COLOR, response=True)

            try:
                await asyncio.wait_for(on_status_ready.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                _LOGGER.debug("Status response timeout for %s", device_name)
                return got_state

            on_status_ready.clear()
            _LOGGER.debug("Color query successful for %s, now sending keep alive", device_name)
            await client.write_gatt_char(0x15, self.MSG_KEEP_ALIVE, response=True)

            try:
                await asyncio.wait_for(on_status_ready.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                _LOGGER.debug("Keep alive response timeout for %s (this may be normal)", device_name)
                # Keep alive response is not critical, so we can ignore timeout here
                return got_state

            return got_state

        except Exception as e:
            _LOGGER.debug("Error querying status for %s: %s", device_name, e)
            return got_state
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

    _attr_supported_color_modes = {ColorMode.RGB, ColorMode.COLOR_TEMP}
    _attr_supported_features = LightEntityFeature.EFFECT
    # Govee's app exposes ~2000-9000K for these bulbs; widen if a model differs.
    _attr_min_color_temp_kelvin = 2000
    _attr_max_color_temp_kelvin = 9000
    _attr_translation_key = "led"
    _attr_effect_list = [
        "Normal",
        "Music - Energetic",
        "Music - Spectrum (Red)",
        "Music - Spectrum (Blue)",
        # "Music - Rolling (Red)",
        # "Music - Rolling (Blue)",
        "Music - Rhythm",
        "Sunrise",
        "Sunset",
        "Movie",
        "Dating",
        "Romantic",
        "Blinking",
        "Candlelight",
        "Snowflake",
    ]

    @property
    def color_mode(self) -> ColorMode:
        """Return the currently active color mode."""
        if self.coordinator.api and self.coordinator.api.get_color_mode() == "color_temp":
            return ColorMode.COLOR_TEMP
        return ColorMode.RGB

    @property
    def color_temp_kelvin(self) -> int | None:
        """Return the current color temperature in Kelvin."""
        if not self.coordinator.api:
            return None
        return self.coordinator.api.get_color_temp_kelvin()

    @property
    def available(self) -> bool:
        """Available once the device has been discovered.

        Deliberately NOT gated on polled state. The light is controlled
        optimistically, so requiring a successful status poll would make it go
        unavailable whenever polling is slow or disabled.
        """
        return self.coordinator.api is not None

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
        # Use the actual device on/off state from polling/commands
        return self.coordinator.api._is_on

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on or control the light."""
        if not self.coordinator.api:
            return

        rgb, brightness = self.coordinator.api.get_light_state()

        # If an effect is requested, set it and return (effects handle their own brightness/color)
        if effect := kwargs.get("effect"):
            await self.coordinator.api.async_set_effect(effect)
            # Update on state immediately
            self.coordinator.api._is_on = True
            self.async_write_ha_state()
            return

        # If a color temperature is requested, set white mode and return.
        # The color command itself exits any active scene/effect, so mark it normal.
        if (kelvin := kwargs.get("color_temp_kelvin")) is not None:
            await self.coordinator.api.async_set_light_color_temp(kelvin)
            new_brightness = kwargs.get("brightness")
            if new_brightness and new_brightness != brightness:
                await self.coordinator.api.async_set_light_brightness(new_brightness)
            self.coordinator.api._effect = "Normal"
            self.coordinator.api._is_on = True
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
        await self.coordinator.api.async_set_effect("Normal")

        # Update on state immediately to reflect the turn-on action
        self.coordinator.api._is_on = True

        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off the light."""
        if not self.coordinator.api:
            return

        # Use the proper turn off command instead of setting brightness to 0
        await self.coordinator.api.async_turn_off(0)
        # Update state to reflect off
        self.coordinator.api._brightness = 0
        self.coordinator.api._is_on = False
        self.async_write_ha_state()
