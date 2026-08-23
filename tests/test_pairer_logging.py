"""A failed BLE connect must log the underlying error, not a canned substitute.

``BleakOutOfConnectionSlotsError`` does not mean "out of slots". bleak-retry-connector
assigns that class by substring match over the error text::

    OUT_OF_SLOTS_ERRORS = {"available connection", "connection slot",
                           "ESP_GATT_CONN_CONN_CANCEL"}

and habluetooth raises the only message in the stack that matches it —
``"No backend with an available connection slot that can reach address <addr> was
found: <reachability diagnostics>"`` — for *any* failure to find a connectable route,
slot exhaustion or not. That message carries habluetooth's
``async_address_reachability_diagnostics()``, which states the actual reason.

Both handlers used to bind the exception and never log it, so the diagnostics were
discarded and users were told to disconnect devices that had never been connected
(eseverson/govee-ble-plugs#2: reported against an adapter sitting at 0/5 slots).

Imports the module the same way ``test_status_parse`` does: stub the one HA symbol
``plugs`` needs and mount the component dir as a package, so no full HA install is
required and the package ``__init__`` never runs.
"""
import asyncio
import importlib
import logging
import os
import sys
import types

import pytest
from bleak_retry_connector import BleakOutOfConnectionSlotsError

PKG = "govee_ble_plugs"
_PKGDIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "custom_components", "govee_ble_plugs")
)

_ha = types.ModuleType("homeassistant")
_ha_exc = types.ModuleType("homeassistant.exceptions")
_ha_exc.ConfigEntryError = type("ConfigEntryError", (Exception,), {})
_ha.exceptions = _ha_exc
sys.modules.setdefault("homeassistant", _ha)
sys.modules.setdefault("homeassistant.exceptions", _ha_exc)

if PKG not in sys.modules:
    _pkg = types.ModuleType(PKG)
    _pkg.__path__ = [_PKGDIR]
    sys.modules[PKG] = _pkg

plugs = importlib.import_module(f"{PKG}.plugs")

# The real shape of what habluetooth raises, diagnostics tail included.
HABLUETOOTH_MSG = (
    "ihoment_H5082_1F1A (7C:A6:B0:5F:1F:1A) - Failed to connect after 3 attempt(s): "
    "No backend with an available connection slot that can reach address "
    "7C:A6:B0:5F:1F:1A was found: no connectable scanner currently sees this address"
)


class _FakeDevice:
    name = "ihoment_H5082_1F1A"
    address = "7C:A6:B0:5F:1F:1A"


@pytest.fixture
def no_slots(monkeypatch):
    """Make every connection attempt fail the way habluetooth's no-route path does."""

    async def _boom(*_args, **_kwargs):
        raise BleakOutOfConnectionSlotsError(HABLUETOOTH_MSG)

    monkeypatch.setattr(plugs, "establish_connection", _boom)


def test_pairing_connect_logs_underlying_error(no_slots, caplog):
    """``GoveePlugPairer.begin`` surfaces the real reason the connect failed."""

    async def _run():
        # Built inside the loop: the pairer allocates a Future in __init__.
        pairer = plugs.GoveePlugPairer(_FakeDevice(), "recv-uuid", "send-uuid", b"")
        await pairer.begin()

    with caplog.at_level(logging.ERROR):
        with pytest.raises(BleakOutOfConnectionSlotsError):
            asyncio.run(_run())

    assert HABLUETOOTH_MSG in caplog.text


def test_command_connect_logs_underlying_error(no_slots, caplog):
    """The command path (``_message_task_fn``) does the same, and fails the command."""

    async def _run():
        plug = plugs.GoveePlugH5082(_FakeDevice(), "00")
        return await plug._send_message(plugs.GoveePlugH5082.MSG_LEFT_ON)

    with caplog.at_level(logging.ERROR):
        assert asyncio.run(_run()) is False

    assert HABLUETOOTH_MSG in caplog.text
