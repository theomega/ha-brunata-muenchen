"""Tests for spreading monthly values over hours."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from brunata_muenchen.series import HOUR, MonthlyValue, hourly_sums, month_bounds

BERLIN = ZoneInfo("Europe/Berlin")
LATER = datetime(2030, 1, 1, tzinfo=UTC)


@pytest.mark.parametrize(
    ("year", "month", "hours"),
    [
        pytest.param(2026, 1, 744, id="31-day month"),
        pytest.param(2026, 3, 743, id="DST starts"),
        pytest.param(2026, 10, 745, id="DST ends"),
        pytest.param(2025, 12, 744, id="year boundary"),
    ],
)
def test_month_bounds_follow_local_time(year: int, month: int, hours: int) -> None:
    start, end = month_bounds(year, month, BERLIN)
    assert start.astimezone(BERLIN).day == 1
    assert end.astimezone(BERLIN).day == 1
    assert (end - start) // HOUR == hours


def test_month_is_spread_evenly_and_sums_accumulate() -> None:
    rows = hourly_sums(
        [MonthlyValue(2026, 2, 6.72), MonthlyValue(2026, 1, 7.44)], BERLIN, LATER
    )
    assert len(rows) == 744 + 672
    assert rows[0].start == datetime(2025, 12, 31, 23, tzinfo=UTC)
    assert rows[0].sum == pytest.approx(0.01)
    assert rows[743].sum == pytest.approx(7.44)
    assert rows[744].sum == pytest.approx(7.44 + 0.01)
    assert rows[-1].sum == pytest.approx(7.44 + 6.72)
    assert all(b.start - a.start == HOUR for a, b in zip(rows, rows[1:]))


def test_base_sum_is_continued() -> None:
    rows = hourly_sums([MonthlyValue(2026, 1, 7.44)], BERLIN, LATER, base_sum=100.0)
    assert rows[0].sum == pytest.approx(100.01)
    assert rows[-1].sum == pytest.approx(107.44)


def test_running_month_stops_before_now() -> None:
    now = datetime(2026, 1, 1, 10, 30, tzinfo=BERLIN)
    rows = hourly_sums([MonthlyValue(2026, 1, 1.0)], BERLIN, now)
    assert len(rows) == 10
    assert rows[-1].start == datetime(2026, 1, 1, 9, tzinfo=BERLIN)
    assert rows[-1].sum == pytest.approx(1.0)


def test_future_month_is_skipped() -> None:
    now = datetime(2026, 1, 15, tzinfo=UTC)
    assert hourly_sums([MonthlyValue(2026, 2, 5.0)], BERLIN, now) == []
