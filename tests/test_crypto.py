"""Tests for the H5080 post-OTA frame cipher.

The ciphertext vectors below are NOT synthetic — they were captured at the bench against a live
plug (D4:AD:FC:48:20:9D) on 2026-06-05. The device accepted the `E7` frames we sent (it replied
SESSION_KEY_EXCHANGED/CONFIRMED) and we decrypted its replies, so these vectors prove the
implementation matches real hardware, not just itself.
"""
import os
import sys

import pytest

sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "custom_components", "govee_ble_plugs")
)

import crypto  # noqa: E402


def h(s: str) -> bytes:
    return bytes.fromhex(s)


def test_sbox_is_a_permutation():
    assert sorted(crypto.INITIAL_RC4_SBOX) == list(range(256))


def test_initial_aes_key_is_the_ascii_slogan():
    assert crypto.INITIAL_AES_KEY == b"MakingLifeSmarte"


def test_build_frame_sets_checksum():
    f = crypto.build_frame(h("e701"))
    assert f == h("e7010000000000000000000000000000000000e6")
    assert f[crypto.CKSUM_POS] == crypto.xor_checksum(f)


# (plaintext frame, expected ciphertext) on the INITIAL keys — all device-accepted at the bench.
INITIAL_ENC_VECTORS = [
    ("e7010000000000000000000000000000000000e6", "a070ef269debe4dfeb8af6f7e67e83a668ee40b9"),
    ("e7020000000000000000000000000000000000e5", "4f9d8b3f90dc228df35f8f0cdedcb61768ee40ba"),
    ("aab100000000000000000000000000000000001b", "d65df1646a721d82cde2975c7ce74ed368ee4044"),
    ("3300000000000000000000000000000000000033", "5db0015a5066164376975cf01bdfbbb068ee406c"),
]


@pytest.mark.parametrize("plain,enc", INITIAL_ENC_VECTORS)
def test_initial_key_encrypt_matches_hardware(plain, enc):
    s = crypto.GoveeSession()
    assert s.encrypt(h(plain)).hex() == enc


def test_initial_key_decrypt_matches_hardware():
    # The plug's reply to our E7 01 (still on the initial keys), captured at the bench.
    s = crypto.GoveeSession()
    dec = s.decrypt(h("e797105e2d6a76fde9e45c6d04c635b12692403d"))
    assert dec.hex() == "e701d4022f5c89b7e4113f6c99c7f4214e7c0062"
    assert s.frame_ok(dec)
    assert dec[2:18].hex() == "d4022f5c89b7e4113f6c99c7f4214e7c"  # session material at [2:18]


def test_rekey_matches_hardware():
    # Same connection: after re-keying from that material, the `aa b1` frame we then sent
    # produced this ciphertext on the wire.
    s = crypto.GoveeSession()
    s.rekey(h("d4022f5c89b7e4113f6c99c7f4214e7c"))
    assert s.rekeyed
    assert s.encrypt(h("aab100000000000000000000000000000000001b")).hex() == (
        "4e287b3c883d304f12fd108f8924f4c308192bc8"
    )


def test_rekey_rejects_bad_length():
    s = crypto.GoveeSession()
    with pytest.raises(ValueError):
        s.rekey(b"\x00" * 8)


@pytest.mark.parametrize(
    "material",
    [None, h("d4022f5c89b7e4113f6c99c7f4214e7c")],
)
def test_round_trip(material):
    s = crypto.GoveeSession()
    if material:
        s.rekey(material)
    for payload in (h("e701"), h("3301ff"), h("aab1"), b"\x00" * 20, bytes(range(20))):
        frame = crypto.build_frame(payload)
        assert s.decrypt(s.encrypt(frame)) == frame
