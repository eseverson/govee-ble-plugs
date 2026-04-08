from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity, SwitchDeviceClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import (
    AddEntitiesCallback,
)

from .const import DOMAIN
from .coordinator import GoveePlugDataUpdateCoordinator
from .entity import GoveePlugEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up govee plug based on a config entry."""
    entry_data = hass.data[DOMAIN][entry.entry_id]
    coordinator: GoveePlugDataUpdateCoordinator = entry_data["coordinator"]
    entities = []
    if coordinator.api:
        port_names = coordinator.api.port_names()
    else:
        port_names = [(None, None)]

    for port, port_name in port_names:
        entities.append(GoveePlugSwitch(coordinator, entry, port, port_name))
    async_add_entities(entities)


class GoveePlugSwitch(GoveePlugEntity, SwitchEntity):
    """Govee switch class."""

    _attr_device_class = SwitchDeviceClass.OUTLET
    _attr_translation_key = "power"

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on the switch."""
        if not self.coordinator.api:
            return
        await self.coordinator.api.async_turn_on(self._port)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off the switch."""
        if not self.coordinator.api:
            return
        await self.coordinator.api.async_turn_off(self._port)
        self.async_write_ha_state()

    @property
    def is_on(self) -> bool | None:
        """Return true if the switch is on."""
        if not self.coordinator.api:
            return None
        return self.coordinator.api.is_on(self._port)
