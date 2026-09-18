"""24-hour energy optimization.

Objective (PS 5.2): minimize sum(grid_kwh[h] * tariff_bdt_per_kwh[h]) subject to:
  - hourly energy balance (PS 9.5): grid + solar_used + discharge = demand + charge
  - 0 <= solar_used <= effective_solar (PS 9.4; curtailment allowed, no export)
  - battery dynamics (PS 9.1), bounds (PS 9.2), rate limits (PS 9.3)
  - end-of-day neutrality (PS 9.6): E_after[23] == initial_energy_kwh
  - directive constraints (PS 5.3) as hard constraints

Implementation: exact linear program solved with scipy.optimize.linprog
(HiGHS). The constraint structure is a single-commodity network, so the LP has
an integral optimal vertex: no rounding heuristic is needed.

A staged fallback ladder guarantees the service never crashes on an
infeasible scenario: each stage relaxes the least-critical constraint class
and the final stage is always feasible (unserved-demand formulation). Valid
organizer scenarios solve at stage 0.
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional, Tuple

import numpy as np
from scipy.optimize import linprog

from gridwise.domain import NUM_HOURS, EffectiveConstraints, Scenario

# Values below this magnitude are snapped to exactly 0.0 in the returned plan.
ZERO_SNAP = 1e-9

_GRID = 0
_SOLAR = 1
_CHARGE = 2
_DISCHARGE = 3
_SOC = 4


class OptimizationError(Exception):
    """Raised when even the fully relaxed formulation has no solution."""


class _StageInfeasible(Exception):
    """Internal: current ladder stage infeasible; try the next stage."""


def _snap(x: float) -> float:
    return 0.0 if abs(x) < ZERO_SNAP else float(x)


def _clamp(x: float, lo: float, hi: float) -> float:
    return float(min(max(x, lo), hi))


def _solve_stage(
    scenario: Scenario,
    eff: EffectiveConstraints,
    drop_neutrality: bool = False,
    drop_reserve: bool = False,
    drop_caps: bool = False,
    drop_windows: bool = False,
    allow_deficit: bool = False,
) -> Tuple[List[dict], Dict[str, float]]:
    """Build and solve one LP stage. Raises _StageInfeasible on failure."""
    n = 5 * NUM_HOURS + (NUM_HOURS if allow_deficit else 0)
    n_def = 5 * NUM_HOURS

    def idx(kind: int, h: int) -> int:
        return kind * NUM_HOURS + h

    # --- Objective: grid electricity cost; deficits carry a dominant penalty.
    # A tiny epsilon on battery throughput breaks cost ties (e.g. flat tariffs)
    # toward the stable no-idle-cycling vertex without affecting real savings.
    tariff = np.asarray(scenario.tariff_bdt_per_kwh, dtype=float)
    c = np.zeros(n)
    c[0:NUM_HOURS] = tariff
    c[2 * NUM_HOURS : 4 * NUM_HOURS] = 1e-6
    if allow_deficit:
        penalty = 1e6 * max(1.0, float(np.max(tariff)) if tariff.size else 1.0)
        c[n_def:] = penalty

    # --- Variable bounds.
    lb = np.zeros(n)
    ub = np.full(n, np.inf)
    ub[NUM_HOURS:2 * NUM_HOURS] = np.asarray(eff.effective_solar, dtype=float)
    charge_cap = scenario.max_charge_kwh_per_hour
    discharge_cap = scenario.max_discharge_kwh_per_hour
    for h in range(NUM_HOURS):
        if not drop_windows and h in eff.no_charge_hours:
            ub[idx(_CHARGE, h)] = 0.0
        else:
            ub[idx(_CHARGE, h)] = charge_cap
        if not drop_windows and h in eff.no_discharge_hours:
            ub[idx(_DISCHARGE, h)] = 0.0
        else:
            ub[idx(_DISCHARGE, h)] = discharge_cap
        floor = 0.0 if drop_reserve else eff.reserve_floor[h]
        lb[idx(_SOC, h)] = min(floor, scenario.capacity_kwh)
        ub[idx(_SOC, h)] = scenario.capacity_kwh
    if allow_deficit:
        for h in range(NUM_HOURS):
            ub[n_def + h] = max(0.0, scenario.demand_kwh[h])

    # --- Equality constraints.
    rows_eq: List[np.ndarray] = []
    b_eq: List[float] = []

    # Battery dynamics: soc[h] - soc[h-1] - charge[h] + discharge[h] = initial(h=0) else 0
    for h in range(NUM_HOURS):
        row = np.zeros(n)
        row[idx(_SOC, h)] = 1.0
        if h > 0:
            row[idx(_SOC, h - 1)] = -1.0
        row[idx(_CHARGE, h)] = -1.0
        row[idx(_DISCHARGE, h)] = 1.0
        rows_eq.append(row)
        b_eq.append(scenario.initial_energy_kwh if h == 0 else 0.0)

    # Energy balance: grid + solar_used + discharge - charge = demand
    for h in range(NUM_HOURS):
        row = np.zeros(n)
        row[idx(_GRID, h)] = 1.0
        row[idx(_SOLAR, h)] = 1.0
        row[idx(_DISCHARGE, h)] = 1.0
        row[idx(_CHARGE, h)] = -1.0
        if allow_deficit:
            row[n_def + h] = 1.0
        rows_eq.append(row)
        b_eq.append(float(scenario.demand_kwh[h]))

    # End-of-day neutrality: soc[23] == initial (PS 9.6).
    if not drop_neutrality:
        row = np.zeros(n)
        row[idx(_SOC, NUM_HOURS - 1)] = 1.0
        rows_eq.append(row)
        b_eq.append(float(scenario.initial_energy_kwh))

    # --- Inequality constraints: per-hour grid caps from max_grid_window.
    rows_ub: List[np.ndarray] = []
    b_ub: List[float] = []
    if not drop_caps:
        for h, cap in sorted(eff.grid_cap.items()):
            row = np.zeros(n)
            row[idx(_GRID, h)] = 1.0
            rows_ub.append(row)
            b_ub.append(float(cap))

    res = linprog(
        c=c,
        A_ub=np.array(rows_ub) if rows_ub else None,
        b_ub=np.array(b_ub) if b_ub else None,
        A_eq=np.array(rows_eq) if rows_eq else None,
        b_eq=np.array(b_eq) if b_eq else None,
        bounds=list(zip(lb, ub)),
        method="highs",
    )
    if not res.success or res.x is None:
        raise _StageInfeasible(str(res.message))

    return _extract_plan(scenario, eff, res.x, allow_deficit)


def _extract_plan(
    scenario: Scenario,
    eff: EffectiveConstraints,
    x: np.ndarray,
    allow_deficit: bool,
) -> Tuple[List[dict], Dict[str, float]]:
    """Convert raw LP output into a clean, contract-compliant hourly plan."""
    grid = [_snap(x[_GRID * NUM_HOURS + h]) for h in range(NUM_HOURS)]
    solar_used = [_snap(x[_SOLAR * NUM_HOURS + h]) for h in range(NUM_HOURS)]
    charge = [_snap(x[_CHARGE * NUM_HOURS + h]) for h in range(NUM_HOURS)]
    discharge = [_snap(x[_DISCHARGE * NUM_HOURS + h]) for h in range(NUM_HOURS)]
    soc = [_snap(x[_SOC * NUM_HOURS + h]) for h in range(NUM_HOURS)]

    plan: List[dict] = []
    for h in range(NUM_HOURS):
        # Numerical hygiene: clamp to declared bounds and clean simultaneous
        # charge/discharge (never optimal in a vertex solution).
        g = _clamp(grid[h], 0.0, float("inf"))
        s = _clamp(solar_used[h], 0.0, max(0.0, eff.effective_solar[h]))
        c = _clamp(charge[h], 0.0, max(0.0, scenario.max_charge_kwh_per_hour))
        d = _clamp(discharge[h], 0.0, max(0.0, scenario.max_discharge_kwh_per_hour))
        both = min(c, d)
        if both > 0:
            c -= both
            d -= both
        e = _clamp(soc[h], 0.0, scenario.capacity_kwh)

        if c > ZERO_SNAP:
            action, magnitude = "charge", c
        elif d > ZERO_SNAP:
            action, magnitude = "discharge", d
        else:
            action, magnitude = "idle", 0.0

        plan.append(
            {
                "hour": h,
                "grid_kwh": round(g, 6),
                "solar_used_kwh": round(s, 6),
                "battery_action": action,
                "battery_kwh": round(magnitude, 6),
                "battery_energy_after_kwh": round(e, 6),
            }
        )

    totals = recompute_totals(plan, scenario.tariff_bdt_per_kwh)
    return plan, totals


def recompute_totals(plan: List[dict], tariffs: List[float]) -> Dict[str, float]:
    """Totals are ALWAYS recomputed from hourly_plan (PS 11.3)."""
    total_grid = 0.0
    total_cost = 0.0
    peak = 0.0
    for entry, tariff in zip(plan, tariffs):
        g = float(entry["grid_kwh"])
        total_grid += g
        total_cost += g * float(tariff)
        peak = max(peak, g)
    return {
        "total_grid_kwh": round(total_grid, 6),
        "total_cost_bdt": round(total_cost, 6),
        "peak_grid_kwh": round(peak, 6),
    }


# Ladder stages, most-constrained first. Stage 0 is the fully compliant
# formulation; later stages relax one constraint class at a time so the
# service degrades gracefully instead of failing on edge scenarios.
_LADDER: Tuple[Dict[str, bool], ...] = (
    {},
    {"drop_neutrality": True},
    {"drop_reserve": True},
    {"drop_caps": True},
    {"drop_windows": True},
    {"drop_neutrality": True, "drop_reserve": True, "drop_caps": True, "drop_windows": True, "allow_deficit": True},
)


def stage_validation_flags(stage: int) -> Dict[str, bool]:
    """Validator flags matching a ladder stage's in-force constraints."""
    return {
        "check_neutrality": stage not in (1, 5),
        "check_reserve": stage not in (2, 5),
        "check_caps": stage not in (3, 5),
        "check_windows": stage not in (4, 5),
    }


def solve_scenario(
    scenario: Scenario, eff: EffectiveConstraints
) -> Tuple[List[dict], Dict[str, float], int]:
    """Solve the 24-hour LP. Returns (hourly_plan, totals, ladder_stage).

    Raises OptimizationError only if every ladder stage fails (never expected
    for valid scenarios).
    """
    last_error: Optional[str] = None
    for stage, kwargs in enumerate(_LADDER):
        try:
            return (*_solve_stage(scenario, eff, **kwargs), stage)
        except _StageInfeasible as exc:
            last_error = str(exc)
            continue
    raise OptimizationError(f"No feasible schedule found: {last_error}")
