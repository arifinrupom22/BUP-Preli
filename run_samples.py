#!/usr/bin/env python3
"""Run scenario JSON files against the /optimize-energy endpoint.

Usage:
    python run_samples.py samples.json [more_samples.json ...]

Each input file may contain a single scenario object or an array of scenario
objects. For every scenario the runner POSTs the request to /optimize-energy
(in-process, via the ASGI test client) and then validates the RESPONSE:

  1. response schema: scenario_id echo, 24-entry hourly_plan,
     one interpretation entry per note in note_index order,
  2. directive interpretation: applies semantics, supported types,
     exact structured_adjustment shapes, hours rules,
  3. hourly_plan replayed hour by hour against every energy/directive rule,
  4. totals recomputed from hourly_plan and compared to the reported ones.

Prints PASS/FAIL per scenario; exit code 0 means all passed.

The interpreter follows GRIDWISE_INTERPRETER exactly like the server:
    GRIDWISE_INTERPRETER=mock  -> offline rule-based double (CI/selftest)
    GRIDWISE_INTERPRETER=api   -> Gemini (requires GEMINI_API_KEY)

Note: this runner checks VALIDITY (schema + rules), not optimality — the
judge checks optimality against hidden organizer optimal costs.
"""
from __future__ import annotations

import json
import sys
from typing import Any, Dict, List

from gridwise.domain import NUM_HOURS, Scenario, build_effective_constraints
from gridwise.schemas import OptimizeRequest
from gridwise.validator import validate_plan, validate_totals

DIRECTIVE_SHAPES = {
    "solar_reduction": {"hours", "factor"},
    "minimum_battery_reserve": {"hours", "minimum_energy_kwh"},
    "no_charge_window": {"hours"},
    "no_discharge_window": {"hours"},
    "max_grid_window": {"hours", "max_grid_kwh"},
}


def load_scenarios(path: str) -> List[Dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if isinstance(data, dict):
        return [data]
    if isinstance(data, list):
        return data
    raise ValueError(f"{path}: top-level JSON must be an object or array")


def check_response(payload: Dict[str, Any], response_json: Dict[str, Any], status_code: int) -> List[str]:
    """Validate the API response for one scenario request."""
    problems: List[str] = []

    if status_code != 200:
        return [f"endpoint returned HTTP {status_code}: {response_json}"]

    # --- Top-level schema (PS Section 10.1).
    for key in (
        "scenario_id",
        "directive_interpretation",
        "hourly_plan",
        "total_grid_kwh",
        "total_cost_bdt",
        "peak_grid_kwh",
        "plan_summary",
    ):
        if key not in response_json:
            problems.append(f"response missing field: {key}")
    if problems:
        return problems
    if response_json["scenario_id"] != payload.get("scenario_id"):
        problems.append("scenario_id does not echo the request value")

    # --- Structural request validation for replay context.
    try:
        req = OptimizeRequest.model_validate(payload)
    except Exception as exc:
        return problems + [f"request schema invalid: {exc}"]

    scenario = Scenario(
        scenario_id=req.scenario_id,
        demand_kwh=[float(h.demand_kwh) for h in req.hours],
        solar_kwh=[float(h.solar_kwh) for h in req.hours],
        tariff_bdt_per_kwh=[float(h.tariff_bdt_per_kwh) for h in req.hours],
        capacity_kwh=float(req.battery.capacity_kwh),
        initial_energy_kwh=float(req.battery.initial_energy_kwh),
        minimum_energy_kwh=float(req.battery.minimum_energy_kwh),
        max_charge_kwh_per_hour=float(req.battery.max_charge_kwh_per_hour),
        max_discharge_kwh_per_hour=float(req.battery.max_discharge_kwh_per_hour),
    )

    # --- Directive interpretation checks (PS Sections 5.1, 8, 10.2).
    interp = response_json["directive_interpretation"]
    if not isinstance(interp, list) or len(interp) != len(req.operator_notes):
        problems.append(
            f"directive_interpretation must have one entry per note "
            f"(expected {len(req.operator_notes)}, got {len(interp) if isinstance(interp, list) else 'non-list'})"
        )
        interp = interp if isinstance(interp, list) else []
    for expected_index, entry in enumerate(interp):
        if not isinstance(entry, dict):
            problems.append(f"interpretation entry {expected_index} is not an object")
            continue
        if entry.get("note_index") != expected_index:
            problems.append(
                f"interpretation entry {expected_index} has note_index {entry.get('note_index')}"
            )
        dtype = entry.get("directive_type")
        applies = entry.get("applies")
        adj = entry.get("structured_adjustment")
        if not isinstance(entry.get("explanation"), str) or not entry.get("explanation").strip():
            problems.append(f"note {expected_index}: explanation must be a non-empty string")
        if dtype == "no_op":
            if applies is not False or adj is not None:
                problems.append(f"note {expected_index}: no_op requires applies=false and null adjustment")
            continue
        if applies is not True:
            problems.append(f"note {expected_index}: non-no_op requires applies=true")
        if dtype not in DIRECTIVE_SHAPES:
            problems.append(f"note {expected_index}: unsupported directive_type {dtype!r}")
            continue
        if not isinstance(adj, dict):
            problems.append(f"note {expected_index}: structured_adjustment must be an object")
            continue
        if set(adj.keys()) != DIRECTIVE_SHAPES[dtype]:
            problems.append(
                f"note {expected_index}: adjustment keys {sorted(adj)} != expected "
                f"{sorted(DIRECTIVE_SHAPES[dtype])}"
            )
            continue
        hours = adj.get("hours")
        if (
            not isinstance(hours, list)
            or not hours
            or any(not isinstance(h, int) or isinstance(h, bool) for h in hours)
        ):
            problems.append(f"note {expected_index}: hours must be a non-empty list of integers")
        elif len(set(hours)) != len(hours) or hours != sorted(hours) or any(h < 0 or h > 23 for h in hours):
            problems.append(f"note {expected_index}: hours must be unique ints 0..23 in ascending order")
        else:
            for key in DIRECTIVE_SHAPES[dtype] - {"hours"}:
                value = adj.get(key)
                if not isinstance(value, (int, float)) or isinstance(value, bool):
                    problems.append(f"note {expected_index}: {key} must be a number")
        if dtype == "solar_reduction" and isinstance(adj.get("factor"), (int, float)):
            if not (0.0 <= float(adj["factor"]) <= 1.0):
                problems.append(f"note {expected_index}: factor must be within [0, 1]")
        if dtype == "minimum_battery_reserve" and isinstance(adj.get("minimum_energy_kwh"), (int, float)):
            if float(adj["minimum_energy_kwh"]) > scenario.capacity_kwh:
                problems.append(f"note {expected_index}: reserve exceeds battery capacity")

    # --- Plan replay against the DIRECTIVES AS REPORTED.
    directives = []
    for entry in interp:
        if not isinstance(entry, dict):
            continue
        directives.append(
            type("D", (), {
                "note_index": entry.get("note_index", -1),
                "applies": bool(entry.get("applies")),
                "directive_type": entry.get("directive_type", "no_op"),
                "structured_adjustment": entry.get("structured_adjustment"),
                "explanation": entry.get("explanation", ""),
            })()
        )
    eff = build_effective_constraints(scenario, directives)

    plan = response_json["hourly_plan"]
    problems.extend(validate_plan(plan, scenario, eff))

    # --- Totals recomputed from hourly_plan (PS 11.3).
    problems.extend(
        validate_totals(
            plan,
            scenario.tariff_bdt_per_kwh,
            {
                "total_grid_kwh": response_json["total_grid_kwh"],
                "total_cost_bdt": response_json["total_cost_bdt"],
                "peak_grid_kwh": response_json["peak_grid_kwh"],
            },
        )
    )
    return problems


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2

    from fastapi.testclient import TestClient

    from gridwise.app import app

    client = TestClient(app)
    all_ok = True
    for path in sys.argv[1:]:
        try:
            scenarios = load_scenarios(path)
        except Exception as exc:
            print(f"FAIL {path}: cannot read ({type(exc).__name__}: {exc})")
            all_ok = False
            continue
        for scenario in scenarios:
            sid = scenario.get("scenario_id", "<missing scenario_id>")
            try:
                resp = client.post("/optimize-energy", json=scenario)
                problems = check_response(scenario, resp.json(), resp.status_code)
            except Exception as exc:
                problems = [f"runner crashed: {type(exc).__name__}: {exc}"]
            if problems:
                all_ok = False
                print(f"FAIL {sid}")
                for p in problems:
                    print(f"     - {p}")
            else:
                totals = (
                    f"grid={resp.json().get('total_grid_kwh')} kWh, "
                    f"cost={resp.json().get('total_cost_bdt')} BDT"
                )
                print(f"PASS {sid}  ({totals})")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
