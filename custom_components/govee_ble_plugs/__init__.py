from __future__ import annotations

import logging

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry, SOURCE_IMPORT
from homeassistant.const import CONF_ACCESS_TOKEN, CONF_ADDRESS, CONF_MODEL, Platform
from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

from .const import DOMAIN, CONF_ENABLE_POLLING
from .coordinator import GoveePlugDataUpdateCoordinator

from .plugs import GoveePlugApi, get_api_by_model, default_enable_polling

PLATFORMS: list[str] = [Platform.SWITCH, Platform.LIGHT, Platform.SENSOR]

OLD_DOMAIN = "govee-ble-plugs"


async def async_setup(hass: HomeAssistant, _config: dict) -> bool:
    """Migrate config entries from old govee-ble-plugs domain."""
    old_entries = hass.config_entries.async_entries(OLD_DOMAIN)
    if not old_entries:
        return True

    new_addresses = {
        e.unique_id
        for e in hass.config_entries.async_entries(DOMAIN)
    }

    for old_entry in old_entries:
        address = old_entry.data.get(CONF_ADDRESS)
        if address in new_addresses:
            # Already migrated — remove the stale old entry
            _LOGGER.info("Removing already-migrated old entry: %s", old_entry.title)
            hass.async_create_task(hass.config_entries.async_remove(old_entry.entry_id))
        else:
            # Trigger an import flow to create the new entry
            _LOGGER.info("Scheduling migration of old entry: %s", old_entry.title)
            hass.async_create_task(
                hass.config_entries.flow.async_init(
                    DOMAIN,
                    context={"source": SOURCE_IMPORT},
                    data={
                        CONF_ADDRESS: address,
                        CONF_ACCESS_TOKEN: old_entry.data.get(CONF_ACCESS_TOKEN),
                        CONF_MODEL: old_entry.data.get(CONF_MODEL),
                        "title": old_entry.title,
                        "options": dict(old_entry.options) if old_entry.options else {},
                        "_old_entry_id": old_entry.entry_id,
                    },
                )
            )

    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    hass.data.setdefault(DOMAIN, {})
    model: str = entry.data[CONF_MODEL]
    token: str = entry.data[CONF_ACCESS_TOKEN]
    bdaddr: str = entry.data[CONF_ADDRESS]

    # Try to find the device, but don't fail if not found yet
    # The coordinator will continue listening for the device via Bluetooth advertisements
    ble_device = bluetooth.async_ble_device_from_address(hass, bdaddr, connectable=True)
    if not ble_device:
        _LOGGER.info(
            "Device not found for Govee %s with address %s. "
            "Will continue listening for device to appear.",
            model,
            bdaddr
        )

    # Store address and model for coordinator to use when creating API
    hass.data[DOMAIN][entry.entry_id] = {
        "model": model,
        "token": token,
        "address": bdaddr,
        "ble_device": ble_device,
    }

    if ble_device:
        api: GoveePlugApi = get_api_by_model(model, ble_device, token)
        coordinator = GoveePlugDataUpdateCoordinator(
            hass, api=api, ble_device=ble_device, address=bdaddr, enable_polling=entry.options.get(CONF_ENABLE_POLLING, default_enable_polling(model))
        )
        hass.data[DOMAIN][entry.entry_id]["coordinator"] = coordinator
    else:
        # Create coordinator without initial device - will be set when device is discovered
        coordinator = GoveePlugDataUpdateCoordinator(
            hass, api=None, ble_device=None, address=bdaddr, model=model, token=token, enable_polling=entry.options.get(CONF_ENABLE_POLLING, default_enable_polling(model))
        )
        hass.data[DOMAIN][entry.entry_id]["coordinator"] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    # Register coordinator start (returns cleanup function)
    # This starts passive Bluetooth listening and polling if enabled
    entry.async_on_unload(coordinator.async_start())

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Handle removal of an entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        entry_data = hass.data[DOMAIN].pop(entry.entry_id)
        coordinator = entry_data.get("coordinator")
        if coordinator:
            await coordinator.async_shutdown()

    return unload_ok
