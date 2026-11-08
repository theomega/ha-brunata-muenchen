from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

from .client import BrunataClient
from .models import (
    ConsumptionComparison,
    ConsumptionForecast,
    CurrentConsumption,
    MeterReading,
    Period,
    Reading,
    ReadingKind,
    RoomConsumption,
)


def __getattr__(name: str):  # pragma: no cover
    if name == "__version__":
        try:
            return version("brunata-nutzerportal-api")
        except PackageNotFoundError:
            return "0.0.0"
    raise AttributeError(name)


__all__ = [
    "BrunataClient",
    "ConsumptionComparison",
    "ConsumptionForecast",
    "CurrentConsumption",
    "MeterReading",
    "Period",
    "Reading",
    "ReadingKind",
    "RoomConsumption",
    "__version__",
]

