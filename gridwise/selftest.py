"""Baseline scenario used by selftests and README examples.

Synthetic data consistent with the Problem Statement example scale. This file
contains no judge data: it exists so the pipeline can be exercised without the
(undistributed) Public Sample Cases JSON.
"""
from __future__ import annotations

from gridwise.domain import Scenario

# Grid tariff rises at peak evening hours; solar covers midday.
_TARIFF = [7.0] * 12 + [9.0] * 6 + [8.0] * 6
_SOLAR = [0.0] * 6 + [30.0, 60.0, 90.0, 110.0, 110.0, 90.0, 60.0, 30.0] + [0.0] * 10
_DEMAND = [200.0, 190.0, 185.0, 180.0, 185.0, 200.0, 220.0, 240.0, 240.0, 235.0, 230.0, 225.0,
           230.0, 240.0, 250.0, 255.0, 260.0, 270.0, 280.0, 290.0, 285.0, 270.0, 250.0, 220.0]


def baseline_scenario() -> Scenario:
    return Scenario(
        scenario_id="GRID-101",
        demand_kwh=_DEMAND,
        solar_kwh=_SOLAR,
        tariff_bdt_per_kwh=_TARIFF,
        capacity_kwh=500.0,
        initial_energy_kwh=200.0,
        minimum_energy_kwh=50.0,
        max_charge_kwh_per_hour=100.0,
        max_discharge_kwh_per_hour=100.0,
    )


BASELINE_REQUEST = {
    "scenario_id": "GRID-101",
    "operator_notes": [
        "Solar output will drop to about 20% from 1 PM to 3 PM.",
        "Do not charge the battery between 2 PM and 4 PM.",
        "The cafeteria menu changes tomorrow.",
    ],
    "hours": [
        {"hour": h, "demand_kwh": _DEMAND[h], "solar_kwh": _SOLAR[h], "tariff_bdt_per_kwh": _TARIFF[h]}
        for h in range(24)
    ],
    "battery": {
        "capacity_kwh": 500.0,
        "initial_energy_kwh": 200.0,
        "minimum_energy_kwh": 50.0,
        "max_charge_kwh_per_hour": 100.0,
        "max_discharge_kwh_per_hour": 100.0,
    },
}
