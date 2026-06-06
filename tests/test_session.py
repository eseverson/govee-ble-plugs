"""Transport tests for GoveeBleSession against a fake device that models the firmware
state machine (session key -> token window -> auth -> relay). No hardware required.

The fake device uses the *same* crypto module, so this also exercises the re-key handoff:
the device re-keys after replying to E7 02 and the session re-keys after reading that reply;
if they diverged, every post-rekey frame would fail to decode and the test would break.
"""
import asyncio
import os
import sys

import pytest

sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "custom_components", "govee_ble_plugs")
)

import crypto  # noqa: E402
from session import GoveeBleSession, SessionError  # noqa: E402


class FakeGoveeDevice:
    """Mimics enough of FUN_9b01ee34 / the 0xE7 handler to drive the transport."""

    def __init__(self, token: bytes, button_after: int = 3):
        self.crypto = crypto.GoveeSession()
        self.material = bytes(range(0x10, 0x20))
        self.token = bytes(token)
        self.button_after = button_after
        self.authed = False
        self.relay_on = False
        self._aab1_calls = 0

    def _reply(self, payload: bytes) -> bytes:
        return self.crypto.encrypt(crypto.build_frame(payload))

    def handle(self, enc: bytes):
        f = self.crypto.decrypt(enc)
        c, s = f[0], f[1]
        if c == 0xE7 and s == 0x01:
            return [self._reply(bytes([0xE7, 0x01]) + self.material)]
        if c == 0xE7 and s == 0x02:
            reply = self._reply(bytes([0xE7, 0x02]))   # encrypted on the OLD keys
            self.crypto.rekey(self.material)            # ...then re-key
            return [reply]
        if c == 0xAA and s == 0xB1:
            self._aab1_calls += 1
            if self._aab1_calls >= self.button_after:   # "button pressed"
                return [self._reply(bytes([0xAA, 0xB1, 0x01]) + self.token[:8])]
            return [self._reply(bytes([0xAA, 0xB1, 0x00]) + b"\xde\xad\xbe\xef\xca\xfe\x00\x01")]
        if c == 0x33 and s == 0xB2:
            ok = f[2:9] == self.token[:7]
            self.authed = self.authed or ok
            return [self._reply(bytes([0x33, 0xB2, 0x00 if ok else 0x01]))]
        if c == 0x33 and s == 0x01:
            if not self.authed:
                return []                               # dropped pre-auth (matches firmware)
            self.relay_on = f[2] == 0xFF
            return [self._reply(bytes([0x33, 0x01, 0x00]))]
        if c == 0x33 and s == 0x00:                      # status query
            if not self.authed:
                return []
            return [self._reply(bytes([0x33, 0x01, 0xFF if self.relay_on else 0x00]))]
        return []


class FakeClient:
    def __init__(self, device: FakeGoveeDevice):
        self.device = device
        self._cb = None

    async def start_notify(self, uuid, cb):
        self._cb = cb

    async def write_gatt_char(self, uuid, data, response=False):
        for reply in self.device.handle(bytes(data)):
            self._cb(uuid, reply)


async def _bring_up(button_after=3, token=None):
    token = token or bytes(range(0x20, 0x30))
    dev = FakeGoveeDevice(token, button_after=button_after)
    sess = GoveeBleSession(FakeClient(dev), "send", "recv")
    await sess.start()
    return dev, sess, token


def test_open_session_rekeys_from_material():
    async def go():
        dev, sess, _ = await _bring_up()
        await sess.open_session()
        assert sess.crypto.rekeyed
        assert sess.crypto.aes_key == dev.material  # both sides agree on the new key
    asyncio.run(go())


def test_fetch_token_after_button():
    async def go():
        dev, sess, token = await _bring_up(button_after=4)
        await sess.open_session()
        t = await sess.fetch_token(retries=10, delay=0)
        assert t is not None and t[:8] == token[:8]
    asyncio.run(go())


def test_fetch_token_times_out_without_button():
    async def go():
        dev, sess, _ = await _bring_up(button_after=999)
        await sess.open_session()
        assert await sess.fetch_token(retries=5, delay=0) is None
    asyncio.run(go())


def test_authenticate_and_toggle_relay():
    async def go():
        dev, sess, token = await _bring_up(button_after=2)
        await sess.open_session()
        t = await sess.fetch_token(retries=10, delay=0)
        assert await sess.authenticate(t) is True
        assert sess.authed and dev.authed

        assert await sess.send_command(bytes([0x33, 0x01, 0xFF])) is not None
        assert dev.relay_on is True
        assert await sess.send_command(bytes([0x33, 0x01, 0xF0])) is not None
        assert dev.relay_on is False
    asyncio.run(go())


def test_control_dropped_before_auth():
    async def go():
        dev, sess, _ = await _bring_up()
        await sess.open_session()
        # no auth -> device drops 33 01, send_command gets no ack
        assert await sess.send_command(bytes([0x33, 0x01, 0xFF]), timeout=0.2) is None
        assert dev.relay_on is False
    asyncio.run(go())


def test_query_returns_status_frame():
    async def go():
        token = bytes(range(0x40, 0x50))
        dev, sess, _ = await _bring_up(token=token)
        await sess.bring_up(token)
        await sess.send_command(bytes([0x33, 0x01, 0xFF]))  # turn on
        frames = await sess.query(bytes([0x33, 0x00]), timeout=0.5, idle=0.1)
        status = [f for f in frames if f[0] == 0x33 and f[1] == 0x01]
        assert status and status[0][2] == 0xFF  # reports "on"
    asyncio.run(go())


def test_bring_up_with_known_token():
    async def go():
        token = bytes(range(0x40, 0x50))
        dev, sess, _ = await _bring_up(token=token)
        assert await sess.bring_up(token) is True   # session key + auth, no button needed
        assert await sess.send_command(bytes([0x33, 0x01, 0xFF])) is not None
        assert dev.relay_on is True
    asyncio.run(go())


class FakePlaintextDevice:
    """A legacy plug: never answers 0xE7, speaks unencrypted frames, and (unlike the cipher
    firmware) replies 33 b2 with a nonzero resp[2]."""

    def __init__(self, token: bytes, button_after: int = 2):
        self.token = bytes(token)
        self.button_after = button_after
        self.authed = False
        self.relay_on = False
        self._calls = 0

    def handle(self, data):
        data = bytes(data)
        c, s = data[0], data[1]
        if c == 0xAA and s == 0xB1:
            self._calls += 1
            if self._calls >= self.button_after:
                return [crypto.build_frame(bytes([0xAA, 0xB1, 0x01]) + self.token[:8])]
            return [crypto.build_frame(bytes([0xAA, 0xB1, 0x00]) + b"\x00" * 8)]
        if c == 0x33 and s == 0xB2:
            self.authed = True
            return [crypto.build_frame(bytes([0x33, 0xB2, 0x07]))]  # nonzero, still success
        if c == 0x33 and s == 0x01:
            if not self.authed:
                return []
            self.relay_on = data[2] == 0xFF
            return [crypto.build_frame(bytes([0x33, 0x01, 0x00]))]
        return []  # ignore the encrypted E7 probe etc.


def test_plaintext_fallback():
    async def go():
        token = bytes(range(0x50, 0x60))
        dev = FakePlaintextDevice(token, button_after=2)
        sess = GoveeBleSession(FakeClient(dev), "send", "recv")
        await sess.start()
        try:
            await sess.open_session(timeout=0.2)
            raise AssertionError("legacy plug should not answer E7")
        except SessionError:
            sess.set_plaintext()
        t = await sess.fetch_token(retries=10, delay=0)
        assert t is not None and t[:8] == token[:8]
        assert await sess.authenticate(t) is True       # plaintext: any 33 b2 reply = ok
        assert await sess.send_command(bytes([0x33, 0x01, 0xFF])) is not None
        assert dev.relay_on is True
    asyncio.run(go())
