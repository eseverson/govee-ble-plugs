from __future__ import annotations

from typing import Any, Optional

from homeassistant.components.bluetooth.passive_update_coordinator import (
    PassiveBluetoothCoordinatorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo

from .const import MANUFACTURER
from .coordinator import GoveePlugDataUpdateCoordinator


class GoveePlugEntity(
    PassiveBluetoothCoordinatorEntity[GoveePlugDataUpdateCoordinator]
):
    """Generic entity for all plugs."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator,
        config_entry: ConfigEntry,
        port: Optional[int],
        port_name: Optional[str],
    ):
        """Initialise the entity."""
        super().__init__(coordinator)
        self._address = self.coordinator.ble_device.address if self.coordinator.ble_device else self.coordinator._address
        self._port = port or 0

        # backwards compat -- original H5080 entity
        if port is None:
            self._attr_unique_id = self._address
        else:
            self._attr_unique_id = f"{self._address}-{port}"
        self._attr_name = port_name
        self._attr_device_info = DeviceInfo(
            connections={(dr.CONNECTION_BLUETOOTH, self._address)},
            manufacturer=MANUFACTURER,
            model=self.coordinator.api.MODEL if self.coordinator.api else (self.coordinator._model or "Unknown"),
            name=config_entry.title,
        )

    @property
    def available(self) -> bool:
        """Return if entity is available.

        Entity is available only if:
        1. The coordinator has API initialized (device was discovered)
        2. The coordinator has valid state data
        """
        # If API not initialized yet, entity is unavailable
        if not self.coordinator.api:
            return False

        # For devices with state data, check if we have data
        # is_on() returns None when no state data is available
        state = self.coordinator.api.is_on(self._port)
        return state is not None
