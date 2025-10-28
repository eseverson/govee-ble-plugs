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
from bleak_retry_connector import establish_connection

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

    async def _send_message(self, msg: bytes) -> bool:
        f = asyncio.Future[bool]()
        self._msgqueue.put_nowait((msg, f))
        self._ensure_message_task()
        return await f

    def _ensure_message_task(self):
        if not self._connection_task:
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

        try:
            # Pull anything on the message queue directly off, these must
            # be processed one way or another
            while not self._msgqueue.empty():
                must_process.put(self._msgqueue.get_nowait())

            client = await establish_connection(
                BleakClient,
                self._device,
                f"{self._device.name} ({self._device.address})",
            )

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
            _LOGGER.error("failed to set state: %s", e)
        finally:
            # We only force clearing the must process queue. Anything that
            # was queued while the connection was failing deserves another try
            # and will be requeued when this task's done callback is called
            while not must_process.empty():
                _, f = must_process.get_nowait()
                f.set_result(False)

            if client is not None:
                await client.disconnect()


class GoveePlugH6163(GoveePlugH6xxx):
    MODEL = "H6163"

    MSG_TURN_ON = _b("3301010000000000000000000000000000000033")
    MSG_TURN_OFF = _b("3301100000000000000000000000000000000022")

    SEND_CHARACTERISTIC_UUID = "00010203-0405-0607-0809-0a0b0c0d2b11"
    RECV_CHARACTERISTIC_UUID = "00010203-0405-0607-0809-0a0b0c0d2b10"

    def __init__(self, device: BLEDevice, token: str) -> None:
        super().__init__(
            device, token, self.RECV_CHARACTERISTIC_UUID, self.SEND_CHARACTERISTIC_UUID
        )
        self._is_on = None
        self._rgb: T.Optional[tuple[int, int, int]] = None
        self._brightness: T.Optional[int] = None

    def port_names(self) -> T.List[T.Tuple[T.Optional[int], T.Optional[str]]]:
        # H6163 is a light device, not a plug - no switch entities
        return []

    def is_on(self, port: int):
        return self._is_on

    def handle_bluetooth_event(self, device: BLEDevice, adv: AdvertisementData):
        for _, mfr_data in adv.manufacturer_data.items():
            self._device = device
            self._is_on = mfr_data[-1] == 0x01

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


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up govee light based on a config entry."""
    coordinator: GoveePlugDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]

    # Only add light entity if the device supports it
    if coordinator.api.has_light():
        # Pass None, None for port and port_name since lights aren't port-based
        async_add_entities([GoveePlugLight(coordinator, entry, None, None)])


class GoveePlugLight(GoveePlugEntity, LightEntity):
    """Govee light class."""

    _attr_supported_color_modes = {ColorMode.RGB, ColorMode.BRIGHTNESS}
    _attr_supported_features = LightEntityFeature(0)
    _attr_translation_key = "led"

    @property
    def color_mode(self) -> ColorMode:
        """Return the color mode of the light."""
        return ColorMode.RGB

    @property
    def rgb_color(self) -> tuple[int, int, int] | None:
        """Return the rgb color value."""
        rgb, _ = self.coordinator.api.get_light_state()
        return rgb

    @property
    def brightness(self) -> int | None:
        """Return the brightness of this light between 0..255."""
        _, brightness = self.coordinator.api.get_light_state()
        return brightness

    @property
    def is_on(self) -> bool | None:
        """Return true if light is on."""
        # Light is considered on if brightness is set
        _, brightness = self.coordinator.api.get_light_state()
        return brightness is not None and brightness > 0

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on or control the light."""
        rgb, brightness = self.coordinator.api.get_light_state()

        # Determine new RGB value
        new_rgb = kwargs.get("rgb_color", rgb)
        if new_rgb is None:
            # Default to white if no color is set
            new_rgb = (255, 255, 255)

        # Determine new brightness
        new_brightness = kwargs.get("brightness", brightness)
        if new_brightness is None:
            new_brightness = 255

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

        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off the light."""
        await self.coordinator.api.async_set_light_brightness(0)
        self.async_write_ha_state()
