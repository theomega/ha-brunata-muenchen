"""Tests for collecting meters from the portal."""

import asyncio
import logging
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from brunata_muenchen import fetch
from brunata_muenchen.fetch import fetch_meters


class FakeClient:
    """Portal with one period offering cold water and an unsupported type."""

    async def get_periods(self) -> list[object]:
        return [object()]

    async def get_supported_cost_types(self) -> dict[str, set[str]]:
        return {"2026": {"KW01", "EL01"}}

    async def get_monthly_consumption(
        self, *, cost_type: str, in_kwh: bool, period_index: int
    ) -> list[SimpleNamespace]:
        return [SimpleNamespace(timestamp=datetime(2026, 1, 31, tzinfo=UTC), value=8.03, unit="m³")]


def test_unsupported_cost_type_is_skipped_and_logged_once(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fetch, "_REPORTED_UNSUPPORTED", set())
    with caplog.at_level(logging.WARNING):
        first = asyncio.run(fetch_meters(FakeClient()))
        second = asyncio.run(fetch_meters(FakeClient()))

    assert list(first) == list(second) == ["KW01"]
    warnings = [r.getMessage() for r in caplog.records if "EL01" in r.getMessage()]
    assert warnings == ["Ignoring unsupported Brunata cost type EL01; supported are KW.., WW.., HZ.."]
