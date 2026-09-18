"""Core domain model shared across the pipeline.

All directive semantics here mirror the Problem Statement exactly:
- solar_reduction: effective_solar[h] = original_solar[h] * factor
- minimum_battery_reserve: reserve floor for listed hours
- no_charge_window / no_discharge_window: battery action forbidden in listed hours
- max_grid_window: grid_kwh[h] <= max_grid_kwh in listed hours
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

# Supported directive types (Problem Statement Section 04). "no_op" means the
# note does not affect the schedule.
DIRECTIVE_TYPES = (
    "solar_reduction",
    "minimum_battery_reserve",
    "no_charge_window",
    "no_discharge_window",
    "max_grid_window",
    "no_op",
)

NUM_HOURS = 24
TOL = 1e-9


@dataclass
class Scenario:
    scenario_id: str
    demand_kwh: List[float]
    solar_kwh: List[float]
    tariff_bdt_per_kwh: List[float]
    capacity_kwh: float
    initial_energy_kwh: float
    minimum_energy_kwh: float
    max_charge_kwh_per_hour: float
    max_discharge_kwh_per_hour: float


@dataclass
class Directive:
    note_index: int
    applies: bool
    directive_type: str
    structured_adjustment: Optional[dict]
    explanation: str


@dataclass
class EffectiveConstraints:
    """Directives flattened into hard optimization constraints."""

    effective_solar: List[float]
    reserve_floor: List[float]            # per-hour minimum battery energy
    no_charge_hours: set = field(default_factory=set)
    no_discharge_hours: set = field(default_factory=set)
    grid_cap: Dict[int, float] = field(default_factory=dict)  # hour -> cap


def build_effective_constraints(scenario: Scenario, directives: List[Directive]) -> EffectiveConstraints:
    """Convert validated directives into the deterministic constraint set.

    Directives are applied before optimization (Problem Statement 5.1). The
    reserve floor is max(base minimum, directive minimum) per listed hour, so
    multiple reserve directives compose safely.
    """
    effective_solar = list(scenario.solar_kwh)
    reserve_floor = [scenario.minimum_energy_kwh] * NUM_HOURS
    out = EffectiveConstraints(effective_solar=effective_solar, reserve_floor=reserve_floor)

    for d in directives:
        if not d.applies or d.directive_type == "no_op":
            continue
        adj = d.structured_adjustment or {}
        hours = adj.get("hours", [])
        if d.directive_type == "solar_reduction":
            factor = float(adj["factor"])
            for h in hours:
                effective_solar[h] = effective_solar[h] * factor
        elif d.directive_type == "minimum_battery_reserve":
            level = float(adj["minimum_energy_kwh"])
            for h in hours:
                out.reserve_floor[h] = max(out.reserve_floor[h], level)
        elif d.directive_type == "no_charge_window":
            out.no_charge_hours.update(int(h) for h in hours)
        elif d.directive_type == "no_discharge_window":
            out.no_discharge_hours.update(int(h) for h in hours)
        elif d.directive_type == "max_grid_window":
            cap = float(adj["max_grid_kwh"])
            for h in hours:
                out.grid_cap[h] = min(out.grid_cap.get(h, float("inf")), cap)
    return out
