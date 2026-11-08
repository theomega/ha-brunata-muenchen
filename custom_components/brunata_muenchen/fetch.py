"""Fetch monthly consumption per meter from the Brunata portal.

Kept free of Home Assistant imports so it can be tested and used by tools/.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .brunata_api import BrunataClient
from .brunata_api.errors import LoginError
from .series import MonthlyValue

_LOGGER = logging.getLogger(__name__)

# Cost type prefix -> (meter kind, request values in kWh)
METER_KINDS: dict[str, tuple[str, bool]] = {
    "KW": ("cold_water", False),
    "WW": ("hot_water", False),
    "HZ": ("heating", True),
}


@dataclass(frozen=True)
class Meter:
    """Monthly consumption series of one portal cost type (e.g. KW01)."""

    cost_type: str
    kind: str
    unit: str
    months: list[MonthlyValue]


def is_credential_error(err: LoginError) -> bool:
    """Return True if the portal rejected the username or password.

    The library raises LoginError for every login problem; only the CredentialSet
    step answers on wrong credentials.
    """
    return str(err).startswith("CredentialSet")


async def fetch_meters(client: BrunataClient) -> dict[str, Meter]:
    """Return all supported meters with their monthly values across all periods."""
    periods = await client.get_periods()
    cost_types = sorted(
        {ct for cts in (await client.get_supported_cost_types()).values() for ct in cts}
    )
    meters: dict[str, Meter] = {}
    for cost_type in cost_types:
        if (kind := METER_KINDS.get(cost_type[:2])) is None:
            continue
        kind_name, in_kwh = kind
        values: dict[tuple[int, int], float] = {}
        units: set[str] = set()
        # Oldest period first, so a newer period wins if months overlap.
        for period_index in reversed(range(len(periods))):
            for reading in await client.get_monthly_consumption(
                cost_type=cost_type, in_kwh=in_kwh, period_index=period_index
            ):
                values[(reading.timestamp.year, reading.timestamp.month)] = reading.value
                if reading.unit:
                    units.add(reading.unit)
        if not values:
            continue
        if len(units) != 1:
            _LOGGER.warning("Skipping %s: inconsistent units %s", cost_type, units)
            continue
        meters[cost_type] = Meter(
            cost_type=cost_type,
            kind=kind_name,
            unit=units.pop(),
            months=[MonthlyValue(y, m, v) for (y, m), v in sorted(values.items())],
        )
    return meters
