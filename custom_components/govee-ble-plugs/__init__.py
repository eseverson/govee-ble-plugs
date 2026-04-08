"""Migration shim for govee-ble-plugs -> govee_ble_plugs domain change.

HA loads this integration because old entries exist for this domain.
async_setup uses that to trigger import flows into the new domain.
async_setup_entry is a no-op so no duplicate entities are created.
"""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry, SOURCE_IMPORT
from homeassistant.const import CONF_ACCESS_TOKEN, CONF_ADDRESS, CONF_MODEL
from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

OLD_DOMAIN = "govee-ble-plugs"
NEW_DOMAIN = "govee_ble_plugs"
CONF_ENABLE_POLLING = "enable_polling"


async def async_setup(hass: HomeAssistant, _config: dict) -> bool:
    """Trigger migration of all old entries into the new domain."""
    old_entries = hass.config_entries.async_entries(OLD_DOMAIN)
    if not old_entries:
        return True

    new_addresses = {e.unique_id for e in hass.config_entries.async_entries(NEW_DOMAIN)}

    for old_entry in old_entries:
        address = old_entry.data.get(CONF_ADDRESS)
        if address in new_addresses:
            _LOGGER.info("Already migrated %s, scheduling removal", old_entry.title)
            hass.async_create_task(hass.config_entries.async_remove(old_entry.entry_id))
        else:
            _LOGGER.info("Migrating %s to new domain", old_entry.title)
            hass.async_create_task(
                hass.config_entries.flow.async_init(
                    NEW_DOMAIN,
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


async def async_setup_entry(_hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """No-op: old entry will be removed by migration."""
    _LOGGER.debug("Old entry %s (%s) pending migration", entry.title, entry.entry_id)
    return True


async def async_unload_entry(_hass: HomeAssistant, _entry: ConfigEntry) -> bool:
    return True
