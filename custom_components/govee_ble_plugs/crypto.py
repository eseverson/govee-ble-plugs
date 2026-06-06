"""Frame cipher + session key schedule for post-OTA H5080 (fw 1.00.28+) plugs.

A Govee OTA (fw 1.00.28, build Mar 17 2026) wrapped the existing plaintext BLE control
protocol in an encrypted, per-connection session-key scheme. The inner protocol (20-byte
command codec, XOR checksum, aa b1 token / 33 b2 auth) is unchanged from the pre-OTA
firmware; only the E7 session exchange + AES/RC4 frame cipher are new. Recovered by
reverse-engineering the RTL8720CF firmware; see
docs/superpowers/notes/2026-06-05-h5080-ble-protocol.md for the full derivation.

Frame format (20 bytes):
  * AES-128-ECB over each full 16-byte block + RC4 over the trailing ``len % 16`` bytes.
  * XOR checksum of bytes[0..0x12] stored at byte 0x13.
  * RC4 is reset per frame (fresh S-box copy, standard PRGA from i=j=0).

Keys:
  * Initial (every fresh connection): AES key = ASCII ``MakingLifeSmarte``; RC4 = a pre-baked
    256-byte S-box from flash.
  * After the 0xE7 session-key exchange, BOTH ciphers re-key from the 16-byte session material
    the device hands back: AES key = material; RC4 = standard KSA(material).

This module is intentionally free of Home Assistant imports so it can be unit-tested standalone.
"""
from __future__ import annotations

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

FRAME_LEN = 0x14
CKSUM_POS = 0x13

# AES-128 key for the initial (pre-session-key) state — firmware const @0x9B8139DC.
INITIAL_AES_KEY = bytes.fromhex("4d616b696e674c696665536d61727465")  # "MakingLifeSmarte"

# Pre-baked RC4 S-box for the initial state — firmware const @0x9B8139EC.
INITIAL_RC4_SBOX = bytes.fromhex(
    "60af1c7186f2b81296216cd9464ae6bb5fc240834e81e363dbedc402013e8254"
    "c19b525c79a0fb55e9cef48c576253246b72be99f69822976fcfff09a1c32085"
    "281b0aebcd921a0b9170a619a43d7577006d80de44aaf00db9c7a718ab25b405"
    "42bd06c031d30310d1aeb1506107cb9f3888dfbc5b66d6ec3c8fd4d79d367ad5"
    "0f45d03234563bb635b311a98e8d9aee262e17e5f716870874137d0c59307ffd"
    "c8335a235ed2c5b7f1a576fc5d49414c956ae2892a8469b2a38b931ea24d0490"
    "ad3a8a1f4b78da64cab57e7329944f9ef3a8671d58e8feea2bf9e7dc37fa7be0"
    "15bac60ee1b047272f656eacc9ef392cbfccf8435114d848f5dd3f68e49c7c2d"
)


def xor_checksum(frame: bytes) -> int:
    """XOR of the first 0x13 bytes (the value the firmware writes to byte 0x13)."""
    c = 0
    for b in frame[:CKSUM_POS]:
        c ^= b
    return c & 0xFF


def build_frame(data: bytes) -> bytes:
    """Pad/truncate ``data`` to a 20-byte frame and set the XOR checksum at byte 0x13."""
    f = bytearray(FRAME_LEN)
    f[: min(len(data), FRAME_LEN)] = data[:FRAME_LEN]
    f[CKSUM_POS] = xor_checksum(f)
    return bytes(f)


def _aes_ecb(key: bytes, block: bytes, *, encrypt: bool) -> bytes:
    cipher = Cipher(algorithms.AES(key), modes.ECB())
    op = cipher.encryptor() if encrypt else cipher.decryptor()
    return op.update(block) + op.finalize()


def rc4_ksa(key: bytes) -> bytes:
    """RC4 key-scheduling -> 256-byte S-box (used to re-key from session material)."""
    s = list(range(256))
    j = 0
    for i in range(256):
        j = (j + s[i] + key[i % len(key)]) & 0xFF
        s[i], s[j] = s[j], s[i]
    return bytes(s)


def rc4_keystream(sbox: bytes, n: int) -> bytes:
    """RC4 keystream of length ``n`` from a (pre-built) S-box, starting fresh (i=j=0)."""
    s = list(sbox)
    out = bytearray()
    i = j = 0
    for _ in range(n):
        i = (i + 1) & 0xFF
        j = (j + s[i]) & 0xFF
        s[i], s[j] = s[j], s[i]
        out.append(s[(s[i] + s[j]) & 0xFF])
    return bytes(out)


def _crypt(aes_key: bytes, sbox: bytes, data: bytes, *, encrypt: bool) -> bytes:
    """AES-ECB on full 16-byte blocks, RC4 on the trailing partial block. RC4 is symmetric,
    so the only direction-dependent part is the AES op."""
    nblk, tail = divmod(len(data), 16)
    out = bytearray()
    for k in range(nblk):
        out += _aes_ecb(aes_key, data[k * 16 : (k + 1) * 16], encrypt=encrypt)
    if tail:
        ks = rc4_keystream(sbox, tail)
        out += bytes(a ^ b for a, b in zip(data[nblk * 16 :], ks))
    return bytes(out)


def frame_encrypt(aes_key: bytes, sbox: bytes, frame: bytes) -> bytes:
    return _crypt(aes_key, sbox, frame, encrypt=True)


def frame_decrypt(aes_key: bytes, sbox: bytes, enc: bytes) -> bytes:
    return _crypt(aes_key, sbox, enc, encrypt=False)


class GoveeSession:
    """Per-connection cipher state. Starts on the initial keys; ``rekey`` switches both
    ciphers to the session-material-derived keys after the 0xE7 exchange."""

    def __init__(self) -> None:
        self.aes_key = INITIAL_AES_KEY
        self.sbox = INITIAL_RC4_SBOX
        self.rekeyed = False
        # When True the frame transform is identity — i.e. this connection negotiated no
        # encryption (older, un-OTA'd plugs that still speak plaintext). Lets one transport
        # path serve both protocols without re-subscribing notifications.
        self.plaintext = False

    def encrypt(self, frame: bytes) -> bytes:
        if self.plaintext:
            return bytes(frame)
        return frame_encrypt(self.aes_key, self.sbox, frame)

    def decrypt(self, enc: bytes) -> bytes:
        if self.plaintext:
            return bytes(enc)
        return frame_decrypt(self.aes_key, self.sbox, enc)

    def rekey(self, material: bytes) -> None:
        """Re-key both ciphers from the 16-byte session material (firmware FUN_9b032ed0)."""
        if len(material) != 16:
            raise ValueError("session material must be 16 bytes")
        self.aes_key = bytes(material)
        self.sbox = rc4_ksa(material)
        self.rekeyed = True

    @staticmethod
    def frame_ok(decrypted: bytes) -> bool:
        """True if a decrypted frame is full length with a valid checksum."""
        return len(decrypted) >= FRAME_LEN and xor_checksum(decrypted) == decrypted[CKSUM_POS]
