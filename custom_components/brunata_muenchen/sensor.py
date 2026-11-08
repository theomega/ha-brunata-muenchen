"""Sensors showing the latest monthly consumption per Brunata meter.

These sensors have no state_class on purpose: the Energy dashboard uses the
imported external statistics, not these sensors.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import BrunataConfigEntry, BrunataCoordinator, statistic_id

DEVICE_CLASSES = {
    "cold_water": SensorDeviceClass.WATER,
    "hot_water": SensorDeviceClass.WATER,
    "heating": SensorDeviceClass.ENERGY,
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BrunataConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up one sensor per meter."""
    coordinator = entry.runtime_data
    async_add_entities(
        BrunataLastMonthSensor(coordinator, cost_type) for cost_type in coordinator.data
    )


class BrunataLastMonthSensor(CoordinatorEntity[BrunataCoordinator], SensorEntity):
    """Consumption of the most recent month the portal has published."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: BrunataCoordinator, cost_type: str) -> None:
        super().__init__(coordinator)
        account_id = coordinator.config_entry.unique_id
        meter = coordinator.data[cost_type]
        self._cost_type = cost_type
        self._attr_unique_id = f"{account_id}_{cost_type.lower()}_last_month"
        self._attr_translation_key = f"{meter.kind}_last_month"
        self._attr_translation_placeholders = {"cost_type": cost_type}
        self._attr_device_class = DEVICE_CLASSES[meter.kind]
        self._attr_native_unit_of_measurement = meter.unit
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, account_id)},
            name="Brunata Nutzerportal",
            manufacturer="Brunata München",
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def available(self) -> bool:
        return super().available and self._cost_type in self.coordinator.data

    @property
    def native_value(self) -> float | None:
        if (meter := self.coordinator.data.get(self._cost_type)) is None:
            return None
        return meter.months[-1].value

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if (meter := self.coordinator.data.get(self._cost_type)) is None:
            return None
        last = meter.months[-1]
        return {
            "month": f"{last.year:04d}-{last.month:02d}",
            "statistic_id": statistic_id(self.coordinator.config_entry.unique_id, self._cost_type),
        }
