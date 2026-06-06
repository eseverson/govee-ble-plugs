"""Per-connection BLE session transport for post-OTA H5080 plugs.

Wraps a connected bleak client and layers on the recovered protocol:
  1. ``open_session`` — the 0xE7 session-key exchange (+ re-key from the device material).
  2. ``fetch_token`` — the ``aa b1`` token fetch (device hands it out during a ~5s window
     opened by a SHORT button press).
  3. ``authenticate`` — the ``33 b2 <token>`` app-auth that unlocks control commands.
  4. ``write`` / ``exchange`` — cipher-wrapped GATT writes; notifies are decrypted into a queue.

The cipher itself lives in ``crypto.py``. This module is BLE-aware but kept free of Home
Assistant imports so it can be exercised against a fake client in tests.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

try:  # package context (Home Assistant)
    from .crypto import GoveeSession, build_frame
except ImportError:  # standalone (tests / tooling)
    from crypto import GoveeSession, build_frame

_LOGGER = logging.getLogger(__name__)

CMD_SESSION_KEY = 0xE7
CMD_GET_AUTH_KEY = 0xAA
SUB_GET_AUTH_KEY = 0xB1
CMD_CONTROL = 0x33
SUB_AUTH = 0xB2
SUB_ONOFF = 0x01


class SessionError(Exception):
    """Raised when a required handshake step gets no/!valid response."""


class GoveeBleSession:
    def __init__(self, client, send_uuid: str, recv_uuid: str) -> None:
        self._client = client
        self._send_uuid = send_uuid
        self._recv_uuid = recv_uuid
        self.crypto = GoveeSession()
        self.token: Optional[bytes] = None
        self.authed = False
        self._q: asyncio.Queue = asyncio.Queue()

    # ---- transport ----
    async def start(self) -> None:
        await self._client.start_notify(self._recv_uuid, self._on_notify)

    def set_plaintext(self) -> None:
        """Switch to identity framing for legacy un-OTA'd plugs. Keeps the single notify
        subscription (no stop/re-start churn, which some BLE proxies reject with GATT 133)."""
        self.crypto.plaintext = True

    def _on_notify(self, _char, data) -> None:
        try:
            dec = self.crypto.decrypt(bytes(data))
        except Exception:  # pragma: no cover - defensive
            _LOGGER.debug("failed to decrypt notify: %s", bytes(data).hex())
            return
        self._q.put_nowait(dec)

    def _drain(self) -> None:
        while not self._q.empty():
            try:
                self._q.get_nowait()
            except asyncio.QueueEmpty:
                break

    async def write(self, data: bytes) -> None:
        enc = self.crypto.encrypt(build_frame(data))
        await self._client.write_gatt_char(self._send_uuid, enc, response=False)

    async def _recv_match(self, b0: int, b1: Optional[int] = None, timeout: float = 2.0):
        """Return the next decrypted frame matching byte0 (and optionally byte1), dropping
        unsolicited frames (the device pushes async ``aa 01`` status notifications)."""
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return None
            try:
                r = await asyncio.wait_for(self._q.get(), remaining)
            except asyncio.TimeoutError:
                return None
            if len(r) >= 2 and r[0] == b0 and (b1 is None or r[1] == b1):
                return r

    async def exchange(self, data: bytes, b0: int, b1: Optional[int] = None, timeout: float = 2.0):
        self._drain()
        await self.write(data)
        return await self._recv_match(b0, b1, timeout)

    async def query(self, data: bytes, timeout: float = 3.0, idle: float = 0.4):
        """Send a query and collect all decrypted reply frames until a quiet gap (``idle``)
        or ``timeout`` elapses. Useful when one query yields several frames (e.g. the H5086
        returns both a 33 01 status and an ee 19 power frame)."""
        self._drain()
        await self.write(data)
        frames = []
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        while True:
            remaining = min(idle, deadline - loop.time())
            if remaining <= 0:
                break
            try:
                frames.append(await asyncio.wait_for(self._q.get(), remaining))
            except asyncio.TimeoutError:
                break
        return frames

    # ---- protocol steps ----
    async def open_session(self, timeout: float = 3.0) -> None:
        """0xE7 exchange: capture the 16-byte material from the E7 01 reply, then E7 02, then
        re-key both ciphers (the device re-keys after sending the E7 02 reply, so order matters)."""
        r = await self.exchange(bytes([CMD_SESSION_KEY, 0x01]), CMD_SESSION_KEY, timeout=timeout)
        if not r or len(r) < 18:
            raise SessionError("no/short E7 01 reply (session-key exchange failed)")
        material = bytes(r[2:18])
        r2 = await self.exchange(bytes([CMD_SESSION_KEY, 0x02]), CMD_SESSION_KEY, timeout=timeout)
        if not r2:
            raise SessionError("no E7 02 reply")
        self.crypto.rekey(material)

    async def fetch_token(self, retries: int = 45, delay: float = 0.4, timeout: float = 1.0):
        """Poll ``aa b1`` until the device reports the token ready (resp[2]==1). The caller must
        get the user to SHORT-press the plug button to open the ~5s window. Returns the 16-byte
        token (resp[3:19]) or None."""
        for _ in range(retries):
            r = await self.exchange(
                bytes([CMD_GET_AUTH_KEY, SUB_GET_AUTH_KEY]), CMD_GET_AUTH_KEY, SUB_GET_AUTH_KEY,
                timeout=timeout,
            )
            if r and len(r) >= 19 and r[2] == 1:
                self.token = bytes(r[3:19])
                return self.token
            await asyncio.sleep(delay)
        return None

    async def authenticate(self, token: bytes, timeout: float = 3.0) -> bool:
        """``33 b2 <token>`` -> auth state 2. On the cipher firmware success is reply 33 b2 with
        resp[2]==0 (resp[2]==1 = wrong token); legacy plaintext plugs just echo 33 b2."""
        self.token = bytes(token)
        r = await self.exchange(
            bytes([CMD_CONTROL, SUB_AUTH]) + self.token, CMD_CONTROL, SUB_AUTH, timeout=timeout
        )
        if not r:
            self.authed = False
        else:
            self.authed = self.crypto.plaintext or (len(r) >= 3 and r[2] == 0)
        return self.authed

    async def send_command(self, frame: bytes, timeout: float = 3.0):
        """Send a control frame (e.g. 33 01 ff) and wait for its 33 01 ack. Requires auth."""
        return await self.exchange(frame, CMD_CONTROL, SUB_ONOFF, timeout=timeout)

    async def bring_up(self, token: bytes) -> bool:
        """Convenience: session key + auth with a known token. Returns auth success."""
        await self.open_session()
        return await self.authenticate(token)
