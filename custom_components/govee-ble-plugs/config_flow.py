"""Config flow for govee-ble-plugs (migration shim).

This accepts config entries for the old domain and immediately
delegates to the new domain's config flow for processing.
"""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigFlow

_LOGGER = logging.getLogger(__name__)

OLD_DOMAIN = "govee-ble-plugs"


class GoveeBlePlugsConfigFlow(ConfigFlow, domain=OLD_DOMAIN):
    """Config flow for govee-ble-plugs (migration shim).

    This is a minimal shim that exists only to allow Home Assistant to find
    the old domain's integration. All actual config flow work is done by the
    new domain's config flow via async_setup_entry delegation.
    """

    VERSION = 1

    async def async_step_bluetooth(self, discovery_info):
        """Handle Bluetooth discovery - delegate to new domain."""
        _LOGGER.debug(
            "Migrating Bluetooth discovery to new domain for %s",
            discovery_info.address if discovery_info else "unknown"
        )
        # Abort here - the entry will be created by the new domain's config flow
        # This shim is just to satisfy Home Assistant's discovery system
        return self.async_abort(reason="not_supported")

    async def async_step_user(self, user_input=None):
        """User initiated config - delegate to new domain."""
        _LOGGER.debug("Migrating manual config to new domain")
        return self.async_abort(reason="not_supported")
