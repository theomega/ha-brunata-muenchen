from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class ReadingKind(str, Enum):
    heating = "heating"
    hot_water = "hot_water"


class CostType(str, Enum):
    """Portal cost types seen in the Munich instance."""

    heating = "HZ01"
    hot_water = "WW01"


class Reading(BaseModel):
    timestamp: datetime = Field(..., description="Timestamp for the reading (as best as known).")
    value: float = Field(..., description="Numeric value of the reading.")
    unit: str | None = Field(default=None, description="Optional unit, if known.")
    kind: ReadingKind


class MeterReading(BaseModel):
    """A cumulative meter/index reading (Zählerstand) for a given cost type."""

    timestamp: datetime = Field(..., description="Timestamp for the reading (period end).")
    value: float = Field(..., description="Numeric value of the meter/index.")
    unit: str | None = Field(default=None, description="Unit label (e.g. m³, Einh.).")
    cost_type: str = Field(..., description="Portal cost type (e.g. HZ01, WW01).")
    kind: ReadingKind


class Period(BaseModel):
    """A dashboard period (e.g. calendar year) with start and end dates."""

    start: datetime = Field(..., description="Period start (Abdatum).")
    end: datetime = Field(..., description="Period end (Bisdatum).")


class CurrentConsumption(BaseModel):
    """Current cumulative consumption (e.g. year-to-date) shown in the dashboard cards."""

    as_of: datetime = Field(..., description="The dashboard's 'as of' date.")
    value: float = Field(..., description="Cumulative consumption up to as_of.")
    unit: str = Field(..., description="Unit label (typically kWh in the dashboard toggle).")
    kind: ReadingKind
    cost_type: str


class ConsumptionComparison(BaseModel):
    """Comparison of your consumption vs building average vs national average (kWh/m²)."""

    cost_type: str = Field(..., description="Portal cost type (e.g. HZ01, WW01).")
    your_value: float = Field(..., description="Your consumption (kWh/m²).")
    building_average: float = Field(..., description="Building average (Gebäudedurchschnitt, kWh/m²).")
    national_average: float = Field(..., description="National average (Bundesdurchschnitt, kWh/m²).")
    unit: str | None = Field(default=None, description="Unit label.")
    kind: ReadingKind


class ConsumptionForecast(BaseModel):
    """Forecast and year-over-year comparison for a cost type."""

    cost_type: str = Field(..., description="Portal cost type (e.g. HZ01, WW01).")
    current: float = Field(..., description="Current year consumption (Aktuell).")
    previous_year: float = Field(..., description="Previous year consumption (Vorjahr).")
    forecast: float = Field(..., description="Forecast for full year (Prognose).")
    difference: float = Field(..., description="Difference current - previous_year (Mehrverbrauch).")
    unit: str | None = Field(default=None, description="Unit label.")
    kind: ReadingKind


class RoomConsumption(BaseModel):
    """Consumption breakdown per room (Raumvergleich)."""

    cost_type: str = Field(..., description="Portal cost type (e.g. HZ01, WW01).")
    room_id: str = Field(..., description="Room identifier.")
    room_name: str = Field(..., description="Room name/description.")
    share_percent: float = Field(..., description="Share of total consumption (%).")
    value: float = Field(..., description="Consumption value for this room.")
    unit: str | None = Field(default=None, description="Unit label.")
    kind: ReadingKind

