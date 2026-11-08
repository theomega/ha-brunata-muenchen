"""Spread monthly consumption values evenly over the hours of each month.

Kept free of Home Assistant imports so it can be tested and used by tools/.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo

HOUR = timedelta(hours=1)


@dataclass(frozen=True)
class MonthlyValue:
    """Consumption of one calendar month."""

    year: int
    month: int
    value: float


@dataclass(frozen=True)
class HourlySum:
    """Cumulative consumption at the end of the hour starting at `start` (UTC)."""

    start: datetime
    sum: float


def month_bounds(year: int, month: int, tz: tzinfo) -> tuple[datetime, datetime]:
    """Return start and end of a local calendar month, in UTC."""
    start = datetime(year, month, 1, tzinfo=tz)
    end = datetime(year + month // 12, month % 12 + 1, 1, tzinfo=tz)
    return start.astimezone(UTC), end.astimezone(UTC)


def hourly_sums(
    months: Iterable[MonthlyValue], tz: tzinfo, now: datetime, base_sum: float = 0.0
) -> list[HourlySum]:
    """Distribute each month's value evenly over its hours, starting at base_sum.

    Hours at or after `now` are never written; a month that is still running is
    spread over the hours that have already passed.
    """
    now_hour = now.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    total = base_sum
    out: list[HourlySum] = []
    for m in sorted(months, key=lambda m: (m.year, m.month)):
        start, end = month_bounds(m.year, m.month, tz)
        end = min(end, now_hour)
        hours = (end - start) // HOUR
        if hours <= 0:
            continue
        per_hour = m.value / hours
        for h in range(hours):
            total += per_hour
            out.append(HourlySum(start + h * HOUR, total))
    return out
