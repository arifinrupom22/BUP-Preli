"""Pydantic schemas for the /optimize-energy request.

The shapes mirror the Problem Statement (Section 07) exactly. Structural
validation happens here (400-level); semantic checks live in the API layer.
"""
from __future__ import annotations

from typing import Any, List

from pydantic import BaseModel, ConfigDict, Field, field_validator

MAX_NOTES = 3
MIN_NOTES = 1
NUM_HOURS = 24


class HourInput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    hour: int = Field(ge=0, le=23)
    demand_kwh: float = Field(ge=0)
    solar_kwh: float = Field(ge=0)
    tariff_bdt_per_kwh: float = Field(ge=0)


class BatteryInput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    capacity_kwh: float = Field(gt=0)
    initial_energy_kwh: float = Field(ge=0)
    minimum_energy_kwh: float = Field(ge=0)
    max_charge_kwh_per_hour: float = Field(ge=0)
    max_discharge_kwh_per_hour: float = Field(ge=0)


class OptimizeRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    scenario_id: str = Field(min_length=1)
    operator_notes: List[str] = Field(min_length=MIN_NOTES, max_length=MAX_NOTES)
    hours: List[HourInput] = Field(min_length=NUM_HOURS, max_length=NUM_HOURS)
    battery: BatteryInput

    @field_validator("scenario_id")
    @classmethod
    def _scenario_id_nonempty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("scenario_id must be non-empty")
        return v

    @field_validator("operator_notes")
    @classmethod
    def _notes_nonempty(cls, v: List[str]) -> List[str]:
        cleaned = [n for n in v]
        if any(not isinstance(n, str) or not n.strip() for n in cleaned):
            raise ValueError("operator_notes entries must be non-empty strings")
        return cleaned

    @field_validator("hours")
    @classmethod
    def _hours_exact_range(cls, v: List[HourInput]) -> List[HourInput]:
        hours = [h.hour for h in v]
        if sorted(hours) != list(range(NUM_HOURS)):
            raise ValueError("hours must contain exactly the unique integers 0..23")
        return v
