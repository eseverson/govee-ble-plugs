import asyncio
import dataclasses
import logging
import queue
import typing as T

from bleak import BleakClient
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from bleak_retry_connector import establish_connection, BleakOutOfConnectionSlotsError

from homeassistant.exceptions import ConfigEntryError

_LOGGER: logging.Logger = logging.getLogger(__package__)


def _b(s: str):
    return bytes(bytearray.fromhex(s))


def _sign_payload(data):
    checksum = 0
    for b in data:
        checksum ^= b
    return checksum & 0xFF


class GoveePlugApi(T.Protocol):
    MODEL: T.Final[str]

    def __init__(self, device: BLEDevice, token: str) -> None: ...

    def port_names(self) -> T.List[T.Tuple[T.Optional[int], T.Optional[str]]]: ...

    def is_on(self, port: int) -> bool | None: ...

    def handle_bluetooth_event(self, device: BLEDevice, adv: AdvertisementData): ...

    async def async_turn_on(self, port: int): ...

    async def async_turn_off(self, port: int): ...

    async def async_query_status(self) -> None: ...

    # Optional light API methods (only H6163 implements these)
    def has_light(self) -> bool: ...
    def get_light_state(self) -> T.Tuple[T.Optional[tuple[int, int, int]], T.Optional[int]]: ...
    async def async_set_light_rgb(self, rgb: tuple[int, int, int]): ...
    async def async_set_light_brightness(self, brightness: int): ...
    def get_effect(self) -> T.Optional[str]: ...
    async def async_set_effect(self, effect: str): ...


class GoveePairApi(T.Protocol):

    async def begin(self): ...

    async def finish(self) -> str | None: ...


def get_api_by_model(model: str, device: BLEDevice, token: str) -> GoveePlugApi:
    if model == "H5080":
        return GoveePlugH5080(device, token)

    if model == "H5082":
        return GoveePlugH5082(device, token)

    if model == "H5086":
        return GoveePlugH5086(device, token)

    if model == "H6163":
        # Import here to avoid circular dependency
        from .light import GoveePlugH6163
        return GoveePlugH6163(device, token)

    raise ConfigEntryError(f"Unsupported model {model}")


def get_pair_by_model(model: str, device: BLEDevice) -> GoveePairApi:
    if model == "H5080":
        return GoveePlugPairer(
            device,
            GoveePlugH5080.RECV_CHARACTERISTIC_UUID,
            GoveePlugH5080.SEND_CHARACTERISTIC_UUID,
            GoveePlugH5080.MSG_GET_AUTH_KEY,
        )

    if model == "H5082":
        return GoveePlugPairer(
            device,
            GoveePlugH5082.RECV_CHARACTERISTIC_UUID,
            GoveePlugH5082.SEND_CHARACTERISTIC_UUID,
            GoveePlugH5082.MSG_GET_AUTH_KEY,
        )

    if model == "H5086":
        return GoveePlugPairer(
            device,
            GoveePlugH5086.RECV_CHARACTERISTIC_UUID,
            GoveePlugH5086.SEND_CHARACTERISTIC_UUID,
            GoveePlugH5086.MSG_GET_AUTH_KEY,
        )

    if model == "H6163":
        # Import here to avoid circular dependency
        from .light import GoveePlugH6163
        return NoOpPlugPairer(
            device,
            GoveePlugH6163.RECV_CHARACTERISTIC_UUID,
            GoveePlugH6163.SEND_CHARACTERISTIC_UUID,
            GoveePlugH6163.MSG_GET_AUTH_KEY,
        )

    raise ConfigEntryError(f"Unsupported model {model}")


@dataclasses.dataclass
class GoveeAdvertisementData:
    name: str
    address: str
    device: BLEDevice
    model: str


def parse_advertisement_data(
    device: BLEDevice, adv: AdvertisementData
) -> GoveeAdvertisementData | None:
    local_name = adv.local_name
    if not local_name:
        return

    if local_name.startswith("ihoment_H5080_"):
        return GoveeAdvertisementData(
            local_name, device.address, device, GoveePlugH5080.MODEL
        )

    if local_name.startswith("ihoment_H5082_"):
        return GoveeAdvertisementData(
            local_name, device.address, device, GoveePlugH5082.MODEL
        )

    if local_name.startswith("GVH5086"):
        return GoveeAdvertisementData(
            local_name, device.address, device, GoveePlugH5086.MODEL
        )

    if local_name.startswith("ihoment_H6163_"):
        # Import here to avoid circular dependency
        from .light import GoveePlugH6163
        return GoveeAdvertisementData(
            local_name, device.address, device, GoveePlugH6163.MODEL
        )


class GoveePlugH508x:

    def __init__(
        self,
        device: BLEDevice,
        token: str,
        RECV_CHARACTERISTIC_UUID: str,
        SEND_CHARACTERISTIC_UUID: str,
    ) -> None:
        self._device = device
        self._token = token
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

            # events to control execution flow
            on_auth_ready = asyncio.Event()
            on_set_state_ready = asyncio.Event()

            async def recv_handler(c, data):
                if data[0] == 0x33 and data[1] == 0xB2:
                    on_auth_ready.set()
                elif data[0] == 0x33 and data[1] == 0x01:
                    on_set_state_ready.set()

            await client.start_notify(self._RECV_CHARACTERISTIC_UUID, recv_handler)

            ba = bytearray([0x33, 0xB2]) + bytearray.fromhex(self._token).ljust(
                17, b"\0"
            )
            ba.append(_sign_payload(ba))
            await client.write_gatt_char(self._SEND_CHARACTERISTIC_UUID, ba)
            await on_auth_ready.wait()

            #
            # Send messages after authentication occurs
            #

            async def _send_msg(msg: bytes, f: asyncio.Future):
                try:
                    await client.write_gatt_char(self._SEND_CHARACTERISTIC_UUID, msg)
                    await on_set_state_ready.wait()
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

            await client.stop_notify(self._RECV_CHARACTERISTIC_UUID)

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
                    await client.stop_notify(self._RECV_CHARACTERISTIC_UUID)
                except Exception:
                    pass  # Ignore errors when stopping notifications
                try:
                    # Ensure we disconnect to free up the connection slot
                    if client.is_connected:
                        await client.disconnect()
                    # Give a small delay to ensure the slot is released
                    await asyncio.sleep(0.1)
                except Exception as e:
                    _LOGGER.debug("Error disconnecting %s: %s", device_name, e)

    async def _query_status_internal(self, query_msg: bytes) -> bool:
        """Internal method to query device status by connecting and sending a query message."""
        client = None
        device_name = f"{self._device.name} ({self._device.address})"
        status_received = asyncio.Event()
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
                    return False

            # Events to control execution flow
            on_auth_ready = asyncio.Event()
            on_status_ready = asyncio.Event()

            async def recv_handler(c, data):
                if data[0] == 0x33 and data[1] == 0xB2:
                    on_auth_ready.set()
                elif data[0] == 0x33 and data[1] == 0x01:
                    # Status response received
                    status_data[0] = data
                    on_status_ready.set()

            await client.start_notify(self._RECV_CHARACTERISTIC_UUID, recv_handler)

            # Authenticate
            ba = bytearray([0x33, 0xB2]) + bytearray.fromhex(self._token).ljust(17, b"\0")
            ba.append(_sign_payload(ba))
            await client.write_gatt_char(self._SEND_CHARACTERISTIC_UUID, ba)

            try:
                await asyncio.wait_for(on_auth_ready.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                _LOGGER.debug("Authentication timeout for status query to %s", device_name)
                return False

            # Send query message
            await client.write_gatt_char(self._SEND_CHARACTERISTIC_UUID, query_msg)

            try:
                await asyncio.wait_for(on_status_ready.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                _LOGGER.debug("Status response timeout for %s", device_name)
                return False

            # Parse status from response if available
            if status_data[0] and len(status_data[0]) >= 3:
                self._parse_status_response(status_data[0])

            return True

        except Exception as e:
            _LOGGER.debug("Error querying status for %s: %s", device_name, e)
            return False
        finally:
            if client is not None:
                try:
                    await client.stop_notify(self._RECV_CHARACTERISTIC_UUID)
                except Exception:
                    pass
                try:
                    if client.is_connected:
                        await client.disconnect()
                    await asyncio.sleep(0.1)
                except Exception as e:
                    _LOGGER.debug("Error disconnecting %s: %s", device_name, e)

    def _parse_status_response(self, data: bytearray) -> None:
        """Parse status response. Override in subclasses if needed."""
        # Default implementation - subclasses can override
        pass

# H6163 and H6xxx base class moved to light.py


class GoveePlugH5080(GoveePlugH508x):
    MODEL = "H5080"

    MSG_GET_AUTH_KEY = _b("aab100000000000000000000000000000000001b")
    MSG_TURN_ON = _b("3301ff00000000000000000000000000000000cd")
    MSG_TURN_OFF = _b("3301f000000000000000000000000000000000c2")
    MSG_QUERY_STATUS = _b("3300000000000000000000000000000000000033")

    SEND_CHARACTERISTIC_UUID = "00010203-0405-0607-0809-0a0b0c0d2b11"
    RECV_CHARACTERISTIC_UUID = "00010203-0405-0607-0809-0a0b0c0d2b10"

    def __init__(self, device: BLEDevice, token: str) -> None:
        super().__init__(
            device, token, self.RECV_CHARACTERISTIC_UUID, self.SEND_CHARACTERISTIC_UUID
        )
        self._is_on = None

    def port_names(self) -> T.List[T.Tuple[T.Optional[int], T.Optional[str]]]:
        return [(None, None)]

    def is_on(self, port: int):
        return self._is_on

    def handle_bluetooth_event(self, device: BLEDevice, adv: AdvertisementData):
        # H5080 uses manufacturer ID 34818 (0x8802) for status advertisements
        # Format: 0xEC 0x00 0x01 0x01 0x00 (off) or 0xEC 0x00 0x01 0x01 0x01 (on)
        # Last byte indicates state: 0x00 = off, 0x01 = on
        GOvee_MANUFACTURER_ID = 34818  # 0x8802

        # Log all manufacturer data received
        if adv.manufacturer_data:
            for mfr_id, mfr_data in adv.manufacturer_data.items():
                _LOGGER.debug(
                    "H5080 %s: Received manufacturer data - mfr_id=%d(0x%04x), data=%s, len=%d",
                    device.address,
                    mfr_id,
                    mfr_id,
                    mfr_data.hex(),
                    len(mfr_data)
                )

        if GOvee_MANUFACTURER_ID in adv.manufacturer_data:
            mfr_data = adv.manufacturer_data[GOvee_MANUFACTURER_ID]
            if len(mfr_data) >= 5:  # Ensure we have at least 5 bytes
                old_state = self._is_on
                self._device = device
                # Last byte indicates state: 0x00 = off, 0x01 = on
                self._is_on = mfr_data[-1] == 0x01
                if old_state != self._is_on:
                    _LOGGER.info(
                        "H5080 %s: State changed from advertisement - is_on=%s (was=%s, mfr_data=%s)",
                        device.address,
                        self._is_on,
                        old_state,
                        mfr_data.hex()
                    )
                else:
                    _LOGGER.debug(
                        "H5080 %s: State updated from advertisement - is_on=%s (mfr_data=%s)",
                        device.address,
                        self._is_on,
                        mfr_data.hex()
                    )
            else:
                _LOGGER.debug(
                    "H5080 %s: Manufacturer data too short - len=%d, expected>=5, data=%s",
                    device.address,
                    len(mfr_data),
                    mfr_data.hex()
                )
        else:
            _LOGGER.debug(
                "H5080 %s: No matching manufacturer data (looking for %d/0x%04x), received: %s",
                device.address,
                GOvee_MANUFACTURER_ID,
                GOvee_MANUFACTURER_ID,
                list(adv.manufacturer_data.keys()) if adv.manufacturer_data else "none"
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
        return False

    def get_light_state(self) -> T.Tuple[T.Optional[tuple[int, int, int]], T.Optional[int]]:
        return None, None

    async def async_set_light_rgb(self, rgb: tuple[int, int, int]):
        pass

    async def async_set_light_brightness(self, brightness: int):
        pass

    def get_effect(self) -> T.Optional[str]:
        return None

    async def async_set_effect(self, effect: str):
        pass

    async def async_query_status(self) -> None:
        """Query the current status of the device."""
        await self._query_status_internal(self.MSG_QUERY_STATUS)

    def _parse_status_response(self, data: bytearray) -> None:
        """Parse status response from device."""
        if len(data) >= 3 and data[0] == 0x33 and data[1] == 0x01:
            # Status is in the third byte or last byte
            if len(data) >= 20:
                # Check last byte for status (similar to advertisement parsing)
                self._is_on = data[-1] == 0x01
            elif len(data) >= 3:
                # Try third byte
                self._is_on = (data[2] & 0xFF) == 0xFF


class GoveePlugH5082(GoveePlugH508x):
    MODEL = "H5082"

    MSG_GET_AUTH_KEY = _b("aab100000000000000000000000000000000001b")

    MSG_LEFT_ON = _b("3301220000000000000000000000000000000010")
    MSG_LEFT_OFF = _b("3301200000000000000000000000000000000012")
    MSG_RIGHT_ON = _b("3301110000000000000000000000000000000023")
    MSG_RIGHT_OFF = _b("3301100000000000000000000000000000000022")
    MSG_QUERY_STATUS = _b("3300000000000000000000000000000000000033")

    SEND_CHARACTERISTIC_UUID = "00010203-0405-0607-0809-0a0b0c0d2b11"
    RECV_CHARACTERISTIC_UUID = "00010203-0405-0607-0809-0a0b0c0d2b10"

    def __init__(self, device: BLEDevice, token: str) -> None:
        super().__init__(
            device, token, self.RECV_CHARACTERISTIC_UUID, self.SEND_CHARACTERISTIC_UUID
        )
        self._is_on: T.List[T.Optional[bool]] = [None, None]

    def port_names(self) -> T.List[T.Tuple[T.Optional[int], T.Optional[str]]]:
        return [(0, "Left Power"), (1, "Right Power")]

    def is_on(self, port: int):
        return self._is_on[port]

    def handle_bluetooth_event(self, device: BLEDevice, adv: AdvertisementData):
        old_state = self._is_on.copy() if self._is_on[0] is not None and self._is_on[1] is not None else [None, None]
        for mfr_id, mfr_data in adv.manufacturer_data.items():
            _LOGGER.debug(
                "H5082 %s: Received manufacturer data - mfr_id=%d(0x%04x), data=%s, len=%d",
                device.address,
                mfr_id,
                mfr_id,
                mfr_data.hex(),
                len(mfr_data)
            )
            self._device = device
            if len(mfr_data) > 0:
                new_left = (mfr_data[-1] & 0x2) == 0x2
                new_right = (mfr_data[-1] & 0x1) == 0x1
                self._is_on[0] = new_left
                self._is_on[1] = new_right
                if old_state[0] != new_left or old_state[1] != new_right:
                    _LOGGER.info(
                        "H5082 %s: State changed from advertisement - left=%s, right=%s (was left=%s, right=%s, mfr_data=%s)",
                        device.address,
                        new_left,
                        new_right,
                        old_state[0],
                        old_state[1],
                        mfr_data.hex()
                    )
                else:
                    _LOGGER.debug(
                        "H5082 %s: State updated from advertisement - left=%s, right=%s (mfr_data=%s)",
                        device.address,
                        new_left,
                        new_right,
                        mfr_data.hex()
                    )

    async def async_turn_on(self, port: int):
        if port == 0:
            msg = self.MSG_LEFT_ON
        elif port == 1:
            msg = self.MSG_RIGHT_ON
        else:
            assert False

        if await self._send_message(msg):
            self._is_on[port] = True

    async def async_turn_off(self, port: int):
        if port == 0:
            msg = self.MSG_LEFT_OFF
        elif port == 1:
            msg = self.MSG_RIGHT_OFF
        else:
            assert False

        if await self._send_message(msg):
            self._is_on[port] = False

    def has_light(self) -> bool:
        return False

    def get_light_state(self) -> T.Tuple[T.Optional[tuple[int, int, int]], T.Optional[int]]:
        return None, None

    async def async_set_light_rgb(self, rgb: tuple[int, int, int]):
        pass

    async def async_set_light_brightness(self, brightness: int):
        pass

    def get_effect(self) -> T.Optional[str]:
        return None

    async def async_set_effect(self, effect: str):
        pass

    async def async_query_status(self) -> None:
        """Query the current status of the device."""
        await self._query_status_internal(self.MSG_QUERY_STATUS)

    def _parse_status_response(self, data: bytearray) -> None:
        """Parse status response from device."""
        if len(data) >= 3 and data[0] == 0x33 and data[1] == 0x01:
            # Status is in the last byte (similar to advertisement parsing)
            if len(data) >= 20:
                status_byte = data[-1]
                self._is_on[0] = (status_byte & 0x2) == 0x2
                self._is_on[1] = (status_byte & 0x1) == 0x1


class GoveePlugH5086(GoveePlugH508x):
    MODEL = "H5086"

    MSG_GET_AUTH_KEY = _b("aab100000000000000000000000000000000001b")
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

    def port_names(self) -> T.List[T.Tuple[T.Optional[int], T.Optional[str]]]:
        return [(None, None)]

    def is_on(self, port: int):
        return self._is_on

    def handle_bluetooth_event(self, device: BLEDevice, adv: AdvertisementData):
        old_state = self._is_on
        for mfr_id, mfr_data in adv.manufacturer_data.items():
            _LOGGER.debug(
                "H5086 %s: Received manufacturer data - mfr_id=%d(0x%04x), data=%s, len=%d",
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
                if old_state != new_state:
                    _LOGGER.info(
                        "H5086 %s: State changed from advertisement - is_on=%s (was=%s, mfr_data=%s)",
                        device.address,
                        new_state,
                        old_state,
                        mfr_data.hex()
                    )
                else:
                    _LOGGER.debug(
                        "H5086 %s: State updated from advertisement - is_on=%s (mfr_data=%s)",
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
        return False

    def get_light_state(self) -> T.Tuple[T.Optional[tuple[int, int, int]], T.Optional[int]]:
        return None, None

    async def async_set_light_rgb(self, rgb: tuple[int, int, int]):
        pass

    async def async_set_light_brightness(self, brightness: int):
        pass

    async def async_query_status(self) -> None:
        """Query the current status of the device."""
        await self._query_status_internal(self.MSG_QUERY_STATUS)

    def _parse_status_response(self, data: bytearray) -> None:
        """Parse status response from device."""
        if len(data) >= 3 and data[0] == 0x33 and data[1] == 0x01:
            # Status is in the last byte (similar to advertisement parsing)
            if len(data) >= 20:
                self._is_on = data[-1] == 0x01


class GoveePlugPairer:

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


class GoveePlugPairer:
    # At least H5080, H5082, and H5086 all have the same pairing procedure
    # as implemented here

    def __init__(
        self, device: BLEDevice, recv_uuid: str, send_uuid: str, auth_msg: bytes
    ) -> None:
        self._device = device
        self._recv_uuid = recv_uuid
        self._send_uuid = send_uuid
        self._auth_msg = auth_msg
        self._result = asyncio.Future()

    async def begin(self):
        device_name = f"{self._device.name} ({self._device.address})"
        _LOGGER.info("%s: connecting to begin pairing", device_name)
        try:
            self._client = await establish_connection(
                BleakClient,
                self._device,
                device_name,
                max_attempts=3,
            )
        except BleakOutOfConnectionSlotsError as e:
            _LOGGER.error(
                "failed to connect for pairing: %s - No available connection slots. "
                "Please disconnect unused devices or add more BLE proxies.",
                device_name
            )
            raise
        except Exception as e:
            _LOGGER.error("failed to connect for pairing: %s: %s", device_name, e)
            raise

        await self._client.start_notify(self._recv_uuid, self._recv_handler)
        await self._send_get_auth_key()

    async def finish(self) -> str | None:
        token = await self._result
        device_name = f"{self._device.name} ({self._device.address})"
        _LOGGER.info("%s: finishing pairing", device_name)
        try:
            await self._client.stop_notify(self._recv_uuid)
        except Exception:
            pass  # Ignore errors when stopping notifications
        try:
            await self._client.disconnect()
        except Exception as e:
            _LOGGER.debug("Error disconnecting %s: %s", device_name, e)
        return token

    async def _send_get_auth_key(self):
        _LOGGER.info(f"%s: asking for auth key", self._device.name)
        await self._client.write_gatt_char(self._send_uuid, self._auth_msg)

    async def _recv_handler(self, _, msg: bytearray):
        if len(msg) != 20:
            return

        # Check for the response type and subtype
        if msg[0] == 0xAA and msg[1] == 0xB1:
            if msg[2] == 0x01:
                auth_key = msg[3:-1]
                _LOGGER.info(f"%s: received authentication key", self._device.name)
                if not self._result.done():
                    self._result.set_result(auth_key.hex())
            else:
                await self._send_get_auth_key()


class NoOpPlugPairer:
    # H6163 doesn't seem to need pairing, just return a dummy token

    def __init__(
        self, device: BLEDevice, recv_uuid: str, send_uuid: str, auth_msg: bytes
    ) -> None:
        self._device = device
        self._recv_uuid = recv_uuid
        self._send_uuid = send_uuid
        self._auth_msg = auth_msg
        self._result = asyncio.Future()

    async def begin(self):
        pass

    async def finish(self) -> str | None:
        return "0"
