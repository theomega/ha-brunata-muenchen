"""Sensors per Brunata meter: last month, this year and the ever-increasing total.

These sensors have no state_class on purpose: the Energy dashboard uses the
imported external statistics, and a state_class would make Home Assistant compile
statistics from these sensors, with one spike per update.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import BrunataConfigEntry, BrunataCoordinator, MeterData, statistic_id
from .series import latest_year_total

DEVICE_CLASSES = {
    "cold_water": SensorDeviceClass.WATER,
    "hot_water": SensorDeviceClass.WATER,
    "heating": SensorDeviceClass.ENERGY,
}


def _month(data: MeterData) -> str:
    last = data.meter.months[-1]
    return f"{last.year:04d}-{last.month:02d}"


def _this_year(data: MeterData) -> float | None:
    if (result := latest_year_total(data.meter.months)) is None:
        return None
    return round(result[1], 3)


def _this_year_attrs(data: MeterData) -> dict[str, Any]:
    result = latest_year_total(data.meter.months)
    return {"year": result[0] if result else None, "up_to_month": _month(data)}


@dataclass(frozen=True, kw_only=True)
class BrunataSensorDescription(SensorEntityDescription):
    """Describes a Brunata sensor."""

    value_fn: Callable[[MeterData], float | None]
    attrs_fn: Callable[[MeterData], dict[str, Any]]


SENSORS: tuple[BrunataSensorDescription, ...] = (
    BrunataSensorDescription(
        key="last_month",
        value_fn=lambda d: d.meter.months[-1].value,
        attrs_fn=lambda d: {"month": _month(d)},
    ),
    BrunataSensorDescription(
        key="this_year",
        value_fn=_this_year,
        attrs_fn=_this_year_attrs,
    ),
    BrunataSensorDescription(
        key="total",
        value_fn=lambda d: None if d.total is None else round(d.total, 3),
        attrs_fn=lambda d: {"up_to_month": _month(d)},
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BrunataConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the sensors for every meter."""
    coordinator = entry.runtime_data
    async_add_entities(
        BrunataSensor(coordinator, cost_type, description)
        for cost_type in coordinator.data
        for description in SENSORS
    )


class BrunataSensor(CoordinatorEntity[BrunataCoordinator], SensorEntity):
    """A value derived from one meter's monthly consumption."""

    _attr_has_entity_name = True
    entity_description: BrunataSensorDescription

    def __init__(
        self,
        coordinator: BrunataCoordinator,
        cost_type: str,
        description: BrunataSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        account_id = coordinator.config_entry.unique_id
        meter = coordinator.data[cost_type].meter
        self._cost_type = cost_type
        self._attr_unique_id = f"{account_id}_{cost_type.lower()}_{description.key}"
        self._attr_translation_key = f"{meter.kind}_{description.key}"
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
    def _data(self) -> MeterData | None:
        return self.coordinator.data.get(self._cost_type)

    @property
    def available(self) -> bool:
        return super().available and self._data is not None

    @property
    def native_value(self) -> float | None:
        if (data := self._data) is None:
            return None
        return self.entity_description.value_fn(data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if (data := self._data) is None:
            return None
        return {
            **self.entity_description.attrs_fn(data),
            "statistic_id": statistic_id(self.coordinator.config_entry.unique_id, self._cost_type),
        }
