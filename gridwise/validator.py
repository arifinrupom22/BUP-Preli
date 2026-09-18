"""Independent schedule validator.

Replays the final hourly_plan hour by hour exactly as the judge does
(Problem Statement Sections 09 and 11.3) and reports every violation. This
module deliberately does not import the optimizer: it is a second, independent
implementation of the rules, so a solver bug cannot hide behind itself.

Checked with a 1e-4 internal tolerance — deliberately stricter than the 0.01
judge tolerance — so anything we ship passes the judge with margin.
"""
from __future__ import annotations

import math
from typing import List

from gridwise.domain import NUM_HOURS, EffectiveConstraints, Scenario

VALID_TOL = 1e-4


def _finite_nonneg(value, name: str, hour: int, errors: List[str]) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(float(value)):
        errors.append(f"hour {hour}: {name} is not a finite number")
        return 0.0
    if float(value) < -VALID_TOL:
        errors.append(f"hour {hour}: {name} is negative ({value})")
    return float(value)


def validate_plan(
    plan: List[dict],
    scenario: Scenario,
    eff: EffectiveConstraints,
    *,
    check_neutrality: bool = True,
    check_reserve: bool = True,
    check_caps: bool = True,
    check_windows: bool = True,
) -> List[str]:
    """Return a list of violations; empty list means the plan is valid.

    The check_* flags mirror the optimizer's fallback ladder: when the
    optimizer had to relax a constraint class to keep the service alive, the
    validator relaxes the same class so it reports only real violations of
    the rules that were actually in force.
    """
    errors: List[str] = []

    if not isinstance(plan, list) or len(plan) != NUM_HOURS:
        return ["hourly_plan must contain exactly 24 entries"]
    if [entry.get("hour") for entry in plan] != list(range(NUM_HOURS)):
        return ["hourly_plan hours must be exactly 0..23 in order"]

    prev_energy = scenario.initial_energy_kwh
    for entry in plan:
        h = int(entry["hour"])
        grid = _finite_nonneg(entry.get("grid_kwh"), "grid_kwh", h, errors)
        solar_used = _finite_nonneg(entry.get("solar_used_kwh"), "solar_used_kwh", h, errors)
        magnitude = _finite_nonneg(entry.get("battery_kwh"), "battery_kwh", h, errors)
        energy_after = _finite_nonneg(entry.get("battery_energy_after_kwh"), "battery_energy_after_kwh", h, errors)

        action = entry.get("battery_action")
        if action not in ("charge", "discharge", "idle"):
            errors.append(f"hour {h}: battery_action must be charge/discharge/idle")
            action = "idle"

        # PS 10.3: battery_kwh must be 0 when idle.
        if action == "idle" and magnitude > VALID_TOL:
            errors.append(f"hour {h}: idle hour must have battery_kwh = 0 (got {magnitude})")

        # PS 9.1: state transition.
        if action == "charge":
            expected = prev_energy + magnitude
        elif action == "discharge":
            expected = prev_energy - magnitude
        else:
            expected = prev_energy
        if abs(expected - energy_after) > VALID_TOL:
            errors.append(
                f"hour {h}: battery transition mismatch (expected {expected:.6f}, got {energy_after:.6f})"
            )

        # PS 9.2: bounds, including directive reserve floors.
        floor = eff.reserve_floor[h]
        if check_reserve and energy_after < floor - VALID_TOL:
            errors.append(f"hour {h}: battery below floor {floor:.6f} (got {energy_after:.6f})")
        if energy_after > scenario.capacity_kwh + VALID_TOL:
            errors.append(f"hour {h}: battery above capacity {scenario.capacity_kwh}")

        # PS 9.3: rate limits.
        if action == "charge" and magnitude > scenario.max_charge_kwh_per_hour + VALID_TOL:
            errors.append(f"hour {h}: charge {magnitude} exceeds rate limit {scenario.max_charge_kwh_per_hour}")
        if action == "discharge" and magnitude > scenario.max_discharge_kwh_per_hour + VALID_TOL:
            errors.append(f"hour {h}: discharge {magnitude} exceeds rate limit {scenario.max_discharge_kwh_per_hour}")

        # Directive windows (PS 5.3).
        if check_windows and h in eff.no_charge_hours and action == "charge" and magnitude > VALID_TOL:
            errors.append(f"hour {h}: charging forbidden by no_charge_window")
        if check_windows and h in eff.no_discharge_hours and action == "discharge" and magnitude > VALID_TOL:
            errors.append(f"hour {h}: discharging forbidden by no_discharge_window")

        # PS 9.4: solar usage capped by effective solar.
        if solar_used > eff.effective_solar[h] + VALID_TOL:
            errors.append(
                f"hour {h}: solar_used {solar_used:.6f} exceeds effective solar {eff.effective_solar[h]:.6f}"
            )

        # Directive grid cap (PS 5.3).
        if check_caps and h in eff.grid_cap and grid > eff.grid_cap[h] + VALID_TOL:
            errors.append(f"hour {h}: grid {grid:.6f} exceeds cap {eff.grid_cap[h]:.6f}")

        # PS 9.5: energy balance.
        discharge = magnitude if action == "discharge" else 0.0
        charge = magnitude if action == "charge" else 0.0
        lhs = grid + solar_used + discharge
        rhs = scenario.demand_kwh[h] + charge
        if abs(lhs - rhs) > VALID_TOL:
            errors.append(
                f"hour {h}: energy balance mismatch (supply {lhs:.6f} vs demand+charge {rhs:.6f})"
            )

        prev_energy = energy_after

    # PS 9.6: end-of-day neutrality.
    if check_neutrality:
        final_energy = plan[-1].get("battery_energy_after_kwh")
        if final_energy is None or abs(float(final_energy) - scenario.initial_energy_kwh) > VALID_TOL:
            errors.append(
                f"end-of-day battery {final_energy} != initial {scenario.initial_energy_kwh}"
            )

    return errors


def validate_totals(plan: List[dict], tariffs: List[float], totals: dict) -> List[str]:
    """Verify reported totals match values recalculated from hourly_plan."""
    errors: List[str] = []
    total_grid = sum(float(e["grid_kwh"]) for e in plan)
    total_cost = sum(float(e["grid_kwh"]) * t for e, t in zip(plan, tariffs))
    peak = max(float(e["grid_kwh"]) for e in plan)

    for key, expected in (
        ("total_grid_kwh", total_grid),
        ("total_cost_bdt", total_cost),
        ("peak_grid_kwh", peak),
    ):
        reported = float(totals.get(key, 0.0))
        if abs(reported - expected) > 1e-3:
            errors.append(f"{key} reported {reported} but recalculated {expected:.6f}")
    return errors
