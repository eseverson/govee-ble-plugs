from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfPower,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .entity import GoveePlugEntity

# Ported from nsheaps@'s H5086 power-monitoring work.


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up power-monitoring sensors for devices that support them (H5086)."""
    entry_data = hass.data[DOMAIN][entry.entry_id]
    coordinator = entry_data["coordinator"]

    api = coordinator.api
    if api is not None and hasattr(api, "supports_power_monitoring"):
        supports = api.supports_power_monitoring()
    else:
        # Device not discovered yet — fall back to the configured model so the
        # sensors still register (they report unavailable until data arrives).
        supports = entry_data.get("model") == "H5086"

    if not supports:
        return

    async_add_entities(
        [
            GoveePlugVoltageSensor(coordinator, entry),
            GoveePlugCurrentSensor(coordinator, entry),
            GoveePlugPowerSensor(coordinator, entry),
            GoveePlugEnergySensor(coordinator, entry),
            GoveePlugPowerFactorSensor(coordinator, entry),
        ]
    )


class GoveePlugSensorBase(GoveePlugEntity, SensorEntity):
    """Base class for Govee power-monitoring sensors."""

    _sensor_type: str = ""

    def __init__(self, coordinator, config_entry: ConfigEntry):
        # Device-level sensor: no port, name comes from the subclass _attr_name.
        super().__init__(coordinator, config_entry, None, self._attr_name)
        self._attr_unique_id = f"{self._address}-{self._sensor_type}"

    @property
    def available(self) -> bool:
        api = self.coordinator.api
        return (
            api is not None
            and getattr(api, "supports_power_monitoring", lambda: False)()
            and api.get_power_data() is not None
        )

    def _power(self):
        api = self.coordinator.api
        return api.get_power_data() if api else None


class GoveePlugVoltageSensor(GoveePlugSensorBase):
    _sensor_type = "voltage"
    _attr_name = "Voltage"
    _attr_device_class = SensorDeviceClass.VOLTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfElectricPotential.VOLT

    @property
    def native_value(self) -> float | None:
        data = self._power()
        return data.voltage if data else None


class GoveePlugCurrentSensor(GoveePlugSensorBase):
    _sensor_type = "current"
    _attr_name = "Current"
    _attr_device_class = SensorDeviceClass.CURRENT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfElectricCurrent.AMPERE

    @property
    def native_value(self) -> float | None:
        data = self._power()
        return data.current if data else None


class GoveePlugPowerSensor(GoveePlugSensorBase):
    _sensor_type = "power"
    _attr_name = "Power"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfPower.WATT

    @property
    def native_value(self) -> float | None:
        data = self._power()
        return data.power if data else None


class GoveePlugEnergySensor(GoveePlugSensorBase):
    _sensor_type = "energy"
    _attr_name = "Energy"
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = UnitOfEnergy.WATT_HOUR

    @property
    def native_value(self) -> float | None:
        data = self._power()
        return data.energy if data else None


class GoveePlugPowerFactorSensor(GoveePlugSensorBase):
    _sensor_type = "power_factor"
    _attr_name = "Power Factor"
    _attr_device_class = SensorDeviceClass.POWER_FACTOR
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = PERCENTAGE

    @property
    def native_value(self) -> int | None:
        data = self._power()
        return data.power_factor if data else None
