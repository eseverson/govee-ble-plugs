import asyncio
import dataclasses
import logging
import queue
import time
import typing as T

from bleak import BleakClient
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from bleak_retry_connector import establish_connection, BleakOutOfConnectionSlotsError

from homeassistant.exceptions import ConfigEntryError

from .session import GoveeBleSession, SessionError

_LOGGER: logging.Logger = logging.getLogger(__package__)

# Ignore BLE advertisements for this many seconds after sending a command, so a
# stale advertisement can't revert the optimistic state we set on success.
COMMAND_COOLDOWN_SECONDS = 3.0


@dataclasses.dataclass
class GoveePowerData:
    """Power-monitoring snapshot from a Govee plug (currently the H5086)."""

    time_on: T.Optional[int] = None       # seconds the outlet has been on
    energy: T.Optional[float] = None      # Wh accumulated
    voltage: T.Optional[float] = None     # V
    current: T.Optional[float] = None     # A
    power: T.Optional[float] = None       # W
    power_factor: T.Optional[int] = None  # percent


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

    async def async_query_status(self) -> bool: ...

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


# Model/advertisement dispatch lives in ``devices/registry.py`` (data-driven).
# ``get_api_by_model``, ``get_pair_by_model``, ``default_enable_polling`` and
# ``parse_advertisement_data`` are re-exported from ``.devices``; import them from there.


@dataclasses.dataclass
class GoveeAdvertisementData:
    name: str
    address: str
    device: BLEDevice
    model: str



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
        self._last_command_time: float = 0

    async def _send_message(self, msg: bytes) -> bool:
        f = asyncio.Future[bool]()
        self._msgqueue.put_nowait((msg, f))
        self._ensure_message_task()
        try:
            # shield: a caller-side cancel/timeout must not cancel the future
            # the message task will still try to fulfill from the BLE side
            result = await asyncio.wait_for(asyncio.shield(f), timeout=45.0)
        except asyncio.TimeoutError:
            _LOGGER.error(
                "timed out waiting for plug to acknowledge: %s (%s)",
                self._device.name,
                self._device.address,
            )
            return False
        # Record when the command landed so passive advertisements arriving in
        # the next few seconds don't overwrite our optimistic state update.
        if result:
            self._last_command_time = time.monotonic()
        return result

    def _should_ignore_advertisement(self) -> bool:
        """True if a command was sent recently enough that advertisements may be stale."""
        if self._last_command_time == 0:
            return False
        return (time.monotonic() - self._last_command_time) < COMMAND_COOLDOWN_SECONDS

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
                except Exception as e:
                    # Always log `e`. In particular BleakOutOfConnectionSlotsError does
                    # NOT reliably mean slot exhaustion — see the note in
                    # GoveePlugPairer.begin — and its text carries the real reason.
                    _LOGGER.error("failed to connect to %s: %s", device_name, e)
                    # Mark all pending messages as failed
                    while not must_process.empty():
                        _, f = must_process.get_nowait()
                        f.set_result(False)
                    return

            # Post-OTA (fw 1.00.28+) units wrap everything in an encrypted session and
            # require token auth; older units still speak plaintext. One session object
            # handles both: probe the 0xE7 exchange, and on no reply fall back to identity
            # framing in-place (no notify re-subscribe, which some BLE proxies reject 133).
            session = GoveeBleSession(
                client, self._SEND_CHARACTERISTIC_UUID, self._RECV_CHARACTERISTIC_UUID
            )
            await session.start()
            try:
                await session.open_session(timeout=5.0)
                _LOGGER.debug("%s: using encrypted session protocol", device_name)
            except SessionError:
                session.set_plaintext()
                _LOGGER.debug("%s: no session-key exchange; using legacy plaintext", device_name)

            def _fail_all():
                while not must_process.empty():
                    _, f = must_process.get_nowait()
                    f.set_result(False)

            if not await session.authenticate(bytes.fromhex(self._token)):
                _LOGGER.error("authentication timeout for %s", device_name)
                _fail_all()
                return

            async def _send_msg(msg: bytes, f: asyncio.Future):
                try:
                    ack = await session.send_command(msg, timeout=5.0)
                except Exception:
                    f.set_result(False)
                    raise
                f.set_result(ack is not None)

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
        """Connect, authenticate, send a query, and parse whatever frames come back.

        One query can yield several frames, so both a 0x3301 status frame and an
        H5086 power frame are collected and handed to their parsers if present.
        """
        client = None
        device_name = f"{self._device.name} ({self._device.address})"
        status_data = [None]
        power_data = [None]

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

            # One session for both protocols: probe 0xE7, fall back to identity framing
            # in-place (no notify re-subscribe).
            session = GoveeBleSession(
                client, self._SEND_CHARACTERISTIC_UUID, self._RECV_CHARACTERISTIC_UUID
            )
            await session.start()
            try:
                await session.open_session(timeout=5.0)
            except SessionError:
                session.set_plaintext()

            if not await session.authenticate(bytes.fromhex(self._token)):
                _LOGGER.debug("Authentication failed for status query to %s", device_name)
                return False
            for frame in await session.query(query_msg, timeout=3.0):
                if len(frame) >= 3 and frame[0] == 0x33 and frame[1] == 0x01:
                    status_data[0] = frame
                # Power frames arrive under TWO headers: 0xAA 0x19 is the synchronous
                # reply to our `aa 19` read, 0xEE 0x19 is the device's unsolicited push.
                # The Govee app handles both and parses them identically — see
                # _parse_power_response.
                elif len(frame) >= 2 and frame[0] in (0xAA, 0xEE) and frame[1] == 0x19:
                    power_data[0] = frame

            # Parse whichever responses arrived
            if status_data[0] and len(status_data[0]) >= 3:
                self._parse_status_response(status_data[0])
            if power_data[0]:
                self._parse_power_response(power_data[0])

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

    def supports_power_monitoring(self) -> bool:
        """Whether this device reports power/energy data. Overridden by H5086."""
        return False

    def get_power_data(self) -> T.Optional[GoveePowerData]:
        """Latest power snapshot, or None if unsupported."""
        return None

    def _parse_power_response(self, data: bytearray) -> None:
        """Parse a power-monitoring response. Overridden by H5086."""
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
        if self._should_ignore_advertisement():
            _LOGGER.debug(
                "%s: ignoring advertisement due to recent command",
                device.name or device.address,
            )
            return
        # H5080 advertises status under TWO manufacturer IDs — 34818 (0x8802) and
        # 34883 (0x8843) — and the firmware rotates between them. Both encode the on/off
        # state in the last byte: 0x00 = off, 0x01 = on (e.g. ec0001 01 00 / ec0002 01 01).
        # Accept either so weak links that only catch one of them still update state.
        GOvee_MANUFACTURER_IDS = (34818, 34883)  # 0x8802, 0x8843

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

        mfr_data = next(
            (adv.manufacturer_data[m] for m in GOvee_MANUFACTURER_IDS if m in adv.manufacturer_data),
            None,
        )
        if mfr_data is not None and len(mfr_data) >= 5:
            old_state = self._is_on
            self._device = device
            # Last byte indicates state: 0x00 = off, 0x01 = on
            self._is_on = mfr_data[-1] == 0x01
            # Payload is `ec 00 02 01 <state>`. Confirmed by FW2 RE: bytes[2:4] are hardcoded
            # constants and the trailing byte is the true relay state (firmware reads the live
            # relay state and rebroadcasts it on every on/off change; there is no steady-state
            # heartbeat and no stale/default value is ever sent). So mfr_data[-1] is correct.
            # See docs/superpowers/notes/2026-06-05-h5080-ble-protocol.md.
            _LOGGER.debug(
                "H5080 %s: advert %s -> is_on=%s (state byte 0x%02x)",
                device.address, mfr_data.hex(), self._is_on, mfr_data[-1],
            )
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
        elif mfr_data is not None:
            _LOGGER.debug(
                "H5080 %s: Manufacturer data too short - len=%d, expected>=5, data=%s",
                device.address,
                len(mfr_data),
                mfr_data.hex()
            )
        else:
            _LOGGER.debug(
                "H5080 %s: No matching manufacturer data (looking for %s), received: %s",
                device.address,
                GOvee_MANUFACTURER_IDS,
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

    async def async_query_status(self) -> bool:
        """Query the current status of the device."""
        return await self._query_status_internal(self.MSG_QUERY_STATUS)

    def _parse_status_response(self, data: bytearray) -> None:
        """Parse a 33 01 status reply: ``33 01 <state> 00.. <xor-cksum>`` (20 bytes).

        The relay state is byte[2] (0x00 = off, nonzero = on). byte[-1] is the frame's
        XOR checksum, NOT the state — reading it (as the old ``len>=20`` branch did)
        reported the wrong on/off on every poll and, with polling enabled, overwrote the
        correct advertisement-derived state each interval.
        """
        if len(data) >= 3 and data[0] == 0x33 and data[1] == 0x01:
            self._is_on = data[2] != 0x00


class GoveePlugH5083(GoveePlugH508x):
    # Ported from zaza7@. The H5083 shares the H5080 command set and the H5086
    # GATT characteristics. Untested here (no H5083 hardware).
    MODEL = "H5083"

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
        if self._should_ignore_advertisement():
            _LOGGER.debug(
                "%s: ignoring advertisement due to recent command",
                device.name or device.address,
            )
            return
        old_state = self._is_on
        for mfr_id, mfr_data in adv.manufacturer_data.items():
            _LOGGER.debug(
                "H5083 %s: Received manufacturer data - mfr_id=%d(0x%04x), data=%s, len=%d",
                device.address, mfr_id, mfr_id, mfr_data.hex(), len(mfr_data),
            )
            self._device = device
            if len(mfr_data) > 0:
                self._is_on = mfr_data[-1] == 0x01
                if old_state != self._is_on:
                    _LOGGER.info(
                        "H5083 %s: State changed from advertisement - is_on=%s (was=%s, mfr_data=%s)",
                        device.address, self._is_on, old_state, mfr_data.hex(),
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

    async def async_query_status(self) -> bool:
        """Query the current status of the device."""
        return await self._query_status_internal(self.MSG_QUERY_STATUS)

    def _parse_status_response(self, data: bytearray) -> None:
        """Parse a 33 01 status reply: state at byte[2] (byte[-1] is the XOR checksum)."""
        if len(data) >= 3 and data[0] == 0x33 and data[1] == 0x01:
            self._is_on = data[2] != 0x00


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
        if self._should_ignore_advertisement():
            _LOGGER.debug(
                "%s: ignoring advertisement due to recent command",
                device.name or device.address,
            )
            return
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

    async def async_query_status(self) -> bool:
        """Query the current status of the device."""
        return await self._query_status_internal(self.MSG_QUERY_STATUS)

    def _parse_status_response(self, data: bytearray) -> None:
        """Parse a 33 01 status reply: ``33 01 <bitfield> 00.. <xor-cksum>`` (20 bytes).

        The per-port bitfield is byte[2] (bit1 = left, bit0 = right). byte[-1] is the
        frame checksum, not the state.
        """
        if len(data) >= 3 and data[0] == 0x33 and data[1] == 0x01:
            status_byte = data[2]
            self._is_on[0] = (status_byte & 0x2) == 0x2
            self._is_on[1] = (status_byte & 0x1) == 0x1


class GoveePlugH5086(GoveePlugH508x):
    MODEL = "H5086"

    MSG_GET_AUTH_KEY = _b("aab100000000000000000000000000000000001b")
    MSG_TURN_ON = _b("3301010000000000000000000000000000000033")
    MSG_TURN_OFF = _b("3301000000000000000000000000000000000032")
    MSG_QUERY_STATUS = _b("3300000000000000000000000000000000000033")
    # Request power-monitoring data. proType 0xAA (read) + cmdType 0x19, per the Govee
    # APK (`DeviceElectricController.getCommandType()` returns 25 = 0x19). XOR checksum
    # aa ^ 19 = b3. The older `aa 00` query came from reydanro@'s capture; the device
    # answers 0x19 either way, but 0x19 is what the app itself sends.
    MSG_GET_POWER = _b("aa190000000000000000000000000000000000b3")

    SEND_CHARACTERISTIC_UUID = "00010203-0405-0607-0809-0a0b0c0d2b11"
    RECV_CHARACTERISTIC_UUID = "00010203-0405-0607-0809-0a0b0c0d2b10"

    def __init__(self, device: BLEDevice, token: str) -> None:
        super().__init__(
            device, token, self.RECV_CHARACTERISTIC_UUID, self.SEND_CHARACTERISTIC_UUID
        )
        self._is_on = None
        self._power_data = GoveePowerData()

    def port_names(self) -> T.List[T.Tuple[T.Optional[int], T.Optional[str]]]:
        return [(None, None)]

    def is_on(self, port: int):
        return self._is_on

    def handle_bluetooth_event(self, device: BLEDevice, adv: AdvertisementData):
        if self._should_ignore_advertisement():
            _LOGGER.debug(
                "%s: ignoring advertisement due to recent command",
                device.name or device.address,
            )
            return
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
            # H5086 advertisement format: ec 00 01 01 [STATE] 00 — the on/off
            # state is the second-to-last byte, not the last one. (Empirically
            # derived by cmorgannorris@ on real H5086 hardware; unverified here.)
            if len(mfr_data) >= 2:
                new_state = mfr_data[-2] == 0x01
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

    def supports_power_monitoring(self) -> bool:
        return True

    def get_power_data(self) -> T.Optional[GoveePowerData]:
        return self._power_data

    async def async_query_status(self) -> bool:
        """Actively poll power data; on/off state comes from advertisements."""
        return await self._query_status_internal(self.MSG_GET_POWER)

    def _parse_power_response(self, data: bytearray) -> None:
        """Parse a power-monitoring frame.

        Layout (big-endian, 13-byte payload):
          <aa|ee> 19 [time:3][energy:3][voltage:2][current:2][power:3][factor:1]

        Two headers carry this payload and the app parses both identically:
        ``aa 19`` is the synchronous reply to our read (matched by the app's
        ``isSameController``: proType 0xAA for a read + cmdType 0x19), and ``ee 19``
        is the device's unsolicited push (the app's ``AbsNotify.parse`` gates on a
        leading 0xEE, strips it, then dispatches on cmdType 0x19 to the *same*
        ``DeviceElectricController`` parser). Accept either.

        - time: seconds the outlet has been on (the app divides by 60 to show minutes)
        - energy: 1/10 Wh (the app reads it as 1/10000 kWh, which is the same scale)
        - voltage: 1/100 V
        - current: 1/100 A
        - power: 1/100 W
        - power_factor: percent. The app does *not* read this byte — it parses only
          13 payload bytes and has no power-factor field anywhere. But the device
          clearly emits it: two independent captures agree with the power factor
          computed from their own V/A/W to within rounding (reydanro@: 120.93V ×
          1.45A vs 175.09W -> 99.9%, byte = 0x64 = 100; flash1228@: 102.01V × 0.27A
          vs 26.26W -> 95.3%, byte = 0x61 = 97). Keep parsing it.
        """
        if len(data) < 16 or data[0] not in (0xAA, 0xEE) or data[1] != 0x19:
            return
        time_on = (data[2] << 16) | (data[3] << 8) | data[4]
        energy = ((data[5] << 16) | (data[6] << 8) | data[7]) / 10.0
        voltage = ((data[8] << 8) | data[9]) / 100.0
        current = ((data[10] << 8) | data[11]) / 100.0
        power = ((data[12] << 16) | (data[13] << 8) | data[14]) / 100.0
        power_factor = data[15]
        self._power_data = GoveePowerData(
            time_on=time_on,
            energy=energy,
            voltage=voltage,
            current=current,
            power=power,
            power_factor=power_factor,
        )
        _LOGGER.debug(
            "H5086 %s power: %.2fV %.2fA %.2fW %.1fWh pf=%d%% on=%ds",
            self._device.address, voltage, current, power, energy, power_factor, time_on,
        )

    def _parse_status_response(self, data: bytearray) -> None:
        """Parse a 33 01 status reply: state at byte[2] (byte[-1] is the XOR checksum).

        The H5086 poll path requests power frames, not this status frame, so on/off
        normally comes from advertisements; corrected here for parity with the family.
        """
        if len(data) >= 3 and data[0] == 0x33 and data[1] == 0x01:
            self._is_on = data[2] != 0x00


# class GoveePlugPairer:

#     SEND_CHARACTERISTIC_UUID = "00010203-0405-0607-0809-0a0b0c0d2b11"
#     RECV_CHARACTERISTIC_UUID = "00010203-0405-0607-0809-0a0b0c0d2b10"

#     def __init__(self, device: BLEDevice, token: str) -> None:
#         super().__init__(
#             device, token, self.RECV_CHARACTERISTIC_UUID, self.SEND_CHARACTERISTIC_UUID
#         )
#         self._is_on = None
#         self._rgb: T.Optional[tuple[int, int, int]] = None
#         self._brightness: T.Optional[int] = None

#     def port_names(self) -> T.List[T.Tuple[T.Optional[int], T.Optional[str]]]:
#         # H6163 is a light device, not a plug - no switch entities
#         return []

#     def is_on(self, port: int):
#         return self._is_on

#     def handle_bluetooth_event(self, device: BLEDevice, adv: AdvertisementData):
#         for _, mfr_data in adv.manufacturer_data.items():
#             self._device = device
#             self._is_on = mfr_data[-1] == 0x01

#     async def async_turn_on(self, port: int):
#         assert port == 0
#         if await self._send_message(self.MSG_TURN_ON):
#             self._is_on = True

#     async def async_turn_off(self, port: int):
#         assert port == 0
#         if await self._send_message(self.MSG_TURN_OFF):
#             self._is_on = False

#     def has_light(self) -> bool:
#         return True

#     def get_light_state(self) -> T.Tuple[T.Optional[tuple[int, int, int]], T.Optional[int]]:
#         return self._rgb, self._brightness

#     async def async_set_light_rgb(self, rgb: tuple[int, int, int]) -> None:
#         """Set RGB color. RGB values should be in range 0-255."""
#         red, green, blue = rgb

#         # Create RGB message: [0x33, 0x05, 0x02, RED, GREEN, BLUE, 0x00, 0xFF, 0xAE, 0x54, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00]
#         msg = bytearray([0x33, 0x05, 0x02, red, green, blue, 0x00, 0xFF, 0xAE, 0x54,
#                          0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00])

#         # Append XOR checksum
#         msg.append(_sign_payload(msg))

#         if await self._send_message(bytes(msg)):
#             self._rgb = rgb

#     async def async_set_light_brightness(self, brightness: int) -> None:
#         """Set brightness. Brightness should be in range 0-255."""
#         # Create brightness message: [0x33, 0x04, BRIGHTNESS, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00]
#         msg = bytearray([0x33, 0x04, brightness] + [0x00] * 16)

#         # Append XOR checksum
#         msg.append(_sign_payload(msg))

#         if await self._send_message(bytes(msg)):
#             self._brightness = brightness


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
        self._session: T.Optional[GoveeBleSession] = None
        self._cipher = False

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
        except Exception as e:
            # Log `e` verbatim, never a canned substitute. BleakOutOfConnectionSlotsError
            # in particular does NOT reliably mean slot exhaustion: bleak-retry-connector
            # assigns that class by substring match over the error text
            # ({"available connection", "connection slot", "ESP_GATT_CONN_CONN_CANCEL"}),
            # and habluetooth's generic no-connectable-route error — "No backend with an
            # available connection slot that can reach address ..." — matches the first
            # two. Its text carries habluetooth's async_address_reachability_diagnostics()
            # tail, which states the actual reason; a canned "out of slots, disconnect
            # something" message threw that away and misdirected users whose adapters
            # were idle.
            _LOGGER.error("failed to connect for pairing: %s: %s", device_name, e)
            raise

        # Post-OTA units require the encrypted session + a SHORT button press to hand out
        # the token; older units answer the plaintext `aa b1` directly. Probe 0xE7 to decide.
        self._session = GoveeBleSession(self._client, self._send_uuid, self._recv_uuid)
        await self._session.start()
        try:
            await self._session.open_session(timeout=5.0)
            self._cipher = True
        except SessionError:
            self._session.set_plaintext()  # legacy plug: identity framing, same subscription
            self._cipher = False

        _LOGGER.info(
            "%s: connected for pairing; short-press the plug button to release its token",
            device_name,
        )
        # Poll aa b1 either way (encrypted or plaintext). ~30s so the user has time to
        # short-press the button, which opens the device's ~5s token window.
        asyncio.create_task(self._fetch_token())

    async def _fetch_token(self):
        token = None
        try:
            token = await self._session.fetch_token(retries=75, delay=0.4)
        except Exception as e:
            # e.g. the (often weak) BLE link dropped mid-pairing -> services invalidated.
            _LOGGER.error("%s: token fetch failed: %s", self._device.address, e)
        if not self._result.done():
            self._result.set_result(token.hex() if token else None)

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
