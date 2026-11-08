"""Coordinator that fetches Brunata data and imports it as external statistics."""

from __future__ import annotations

import logging
from datetime import datetime

import httpx

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    statistics_during_period,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util, slugify

from .brunata_api import BrunataClient
from .brunata_api.errors import BrunataError, LoginError
from .const import BASE_URL, DOMAIN, SAP_CLIENT, STATISTIC_NAMES, UNIT_CLASSES, UPDATE_INTERVAL
from .fetch import Meter, fetch_meters, is_credential_error
from .series import HOUR, hourly_sums, month_bounds

_LOGGER = logging.getLogger(__name__)

type BrunataConfigEntry = ConfigEntry[BrunataCoordinator]


def statistic_id(account_id: str, cost_type: str) -> str:
    """Return the external statistic id for a meter of an account."""
    return f"{DOMAIN}:{slugify(account_id)}_{cost_type.lower()}"


class BrunataCoordinator(DataUpdateCoordinator[dict[str, Meter]]):
    """Fetch monthly values and keep the hourly statistics in sync."""

    config_entry: BrunataConfigEntry

    def __init__(self, hass: HomeAssistant, entry: BrunataConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=UPDATE_INTERVAL,
        )

    async def _async_update_data(self) -> dict[str, Meter]:
        meters = await self._async_fetch()
        for meter in meters.values():
            await self._async_import_statistics(meter)
        return meters

    async def _async_fetch(self) -> dict[str, Meter]:
        data = self.config_entry.data
        try:
            async with BrunataClient(
                base_url=BASE_URL,
                username=data[CONF_USERNAME],
                password=data[CONF_PASSWORD],
                sap_client=SAP_CLIENT,
            ) as client:
                try:
                    await client.login()
                except LoginError as err:
                    if is_credential_error(err):
                        raise ConfigEntryAuthFailed("Brunata rejected the credentials") from err
                    raise
                return await fetch_meters(client)
        except (BrunataError, httpx.HTTPError) as err:
            raise UpdateFailed(f"Error fetching Brunata data: {err}") from err

    async def _async_import_statistics(self, meter: Meter) -> None:
        if (unit_class := UNIT_CLASSES.get(meter.unit)) is None:
            _LOGGER.warning("Not importing %s: unsupported unit %s", meter.cost_type, meter.unit)
            return
        stat_id = statistic_id(self.config_entry.unique_id, meter.cost_type)
        tz = dt_util.get_default_time_zone()
        first = meter.months[0]
        first_start, _ = month_bounds(first.year, first.month, tz)
        # Continue from what is stored before the first month the portal still has, so
        # history that has dropped off the portal stays consistent.
        base_sum = await self._async_sum_before(stat_id, first_start)
        rows = hourly_sums(meter.months, tz, dt_util.utcnow(), base_sum)
        metadata = StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=f"{STATISTIC_NAMES[meter.kind]} {meter.cost_type}",
            source=DOMAIN,
            statistic_id=stat_id,
            unit_class=unit_class,
            unit_of_measurement=meter.unit,
        )
        async_add_external_statistics(
            self.hass,
            metadata,
            [StatisticData(start=r.start, state=r.sum, sum=r.sum) for r in rows],
        )

    async def _async_sum_before(self, stat_id: str, start: datetime) -> float:
        stats = await get_instance(self.hass).async_add_executor_job(
            statistics_during_period,
            self.hass,
            start - HOUR,
            start,
            {stat_id},
            "hour",
            None,
            {"sum"},
        )
        if (rows := stats.get(stat_id)) and rows[0].get("sum") is not None:
            return rows[0]["sum"]
        return 0.0
