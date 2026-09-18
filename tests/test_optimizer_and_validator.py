"""Optimizer and validator tests: correctness of the scheduling engine."""
from __future__ import annotations

import os
import sys

os.environ.setdefault("GRIDWISE_INTERPRETER", "mock")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gridwise.domain import Scenario, build_effective_constraints
from gridwise.optimizer import solve_scenario
from gridwise.validator import validate_plan, validate_totals

CAP = 500.0
INIT = 200.0
MIN = 50.0
CHARGE_RATE = 100.0
DISCHARGE_RATE = 100.0

# Simple two-tariff day: cheap 00-12 (7 BDT), expensive 12-24 (9 BDT).
CHEAP, EXPENSIVE = 7.0, 9.0


def make_scenario(demand, solar=None, tariffs=None, initial=INIT, capacity=CAP, minimum=MIN,
                  charge_rate=CHARGE_RATE, discharge_rate=DISCHARGE_RATE) -> Scenario:
    return Scenario(
        scenario_id="T",
        demand_kwh=demand,
        solar_kwh=solar or [0.0] * 24,
        tariff_bdt_per_kwh=tariffs or ([CHEAP] * 12 + [EXPENSIVE] * 12),
        capacity_kwh=capacity,
        initial_energy_kwh=initial,
        minimum_energy_kwh=minimum,
        max_charge_kwh_per_hour=charge_rate,
        max_discharge_kwh_per_hour=discharge_rate,
    )


def solve_and_validate(scenario: Scenario, eff=None):
    eff = eff or build_effective_constraints(scenario, [])
    plan, totals, stage = solve_scenario(scenario, eff)
    assert validate_plan(plan, scenario, eff) == []
    assert validate_totals(plan, scenario.tariff_bdt_per_kwh, totals) == []
    return plan, totals, stage


def test_flat_demand_flat_tariff_stage0():
    demand = [100.0] * 24
    plan, totals, stage = solve_and_validate(make_scenario(demand, tariffs=[CHEAP] * 24))
    assert stage == 0
    # With a flat tariff and no solar, the only feasible plan is grid = demand.
    for entry in plan:
        assert abs(entry["grid_kwh"] - 100.0) < 0.01
    assert abs(totals["total_cost_bdt"] - 100 * 24 * CHEAP) < 0.01


def test_battery_arbitrage_is_optimal():
    # Two-tariff day: the battery can shift at most 300 kWh (capacity headroom
    # 200->500) from cheap to expensive hours. Savings = 300 * (9 - 7) = 600.
    demand = [100.0] * 24
    scenario = make_scenario(demand)
    plan, totals, _ = solve_and_validate(scenario)
    baseline = 100 * (12 * CHEAP + 12 * EXPENSIVE)
    assert abs(totals["total_cost_bdt"] - (baseline - 600.0)) < 0.01


def test_neutrality_enforced():
    demand = [100.0] * 24
    scenario = make_scenario(demand, initial=300.0)
    plan, _, _ = solve_and_validate(scenario)
    assert abs(plan[-1]["battery_energy_after_kwh"] - 300.0) < 1e-4


def test_solar_used_when_free():
    demand = [100.0] * 24
    solar = [0.0] * 24
    solar[12] = 50.0
    scenario = make_scenario(demand, solar=solar)
    plan, totals, _ = solve_and_validate(scenario)
    assert abs(plan[12]["solar_used_kwh"] - 50.0) < 1e-4
    assert abs(plan[12]["grid_kwh"] - 50.0) < 1e-4


def test_no_charge_window_effect():
    demand = [100.0] * 24
    scenario = make_scenario(demand)
    eff = build_effective_constraints(
        scenario, [type("D", (), {"note_index": 0, "applies": True, "directive_type": "no_charge_window",
                                  "structured_adjustment": {"hours": [0, 1, 2]}, "explanation": ""})()]
    )
    plan, _, _ = solve_and_validate(scenario, eff)
    for h in (0, 1, 2):
        assert plan[h]["battery_action"] != "charge"


def test_no_discharge_window_effect():
    demand = [100.0] * 24
    scenario = make_scenario(demand)
    eff = build_effective_constraints(
        scenario, [type("D", (), {"note_index": 0, "applies": True, "directive_type": "no_discharge_window",
                                  "structured_adjustment": {"hours": [12, 13]}, "explanation": ""})()]
    )
    plan, _, _ = solve_and_validate(scenario, eff)
    for h in (12, 13):
        assert plan[h]["battery_action"] != "discharge"


def test_reserve_floor_effect():
    demand = [100.0] * 24
    scenario = make_scenario(demand, initial=120.0)
    eff = build_effective_constraints(
        scenario, [type("D", (), {"note_index": 0, "applies": True, "directive_type": "minimum_battery_reserve",
                                  "structured_adjustment": {"hours": [18, 19, 20], "minimum_energy_kwh": 120.0}, "explanation": ""})()]
    )
    plan, _, _ = solve_and_validate(scenario, eff)
    # Battery may not drop below 120 in hours 18-20.
    for h in (18, 19, 20):
        assert plan[h]["battery_energy_after_kwh"] >= 120.0 - 1e-4


def test_grid_cap_effect():
    # demand 200 in capped hours: grid 100 + discharge 100 is feasible.
    demand = [200.0] * 24
    scenario = make_scenario(demand, initial=500.0)
    eff = build_effective_constraints(
        scenario, [type("D", (), {"note_index": 0, "applies": True, "directive_type": "max_grid_window",
                                  "structured_adjustment": {"hours": [12, 13], "max_grid_kwh": 100.0}, "explanation": ""})()]
    )
    plan, _, _ = solve_and_validate(scenario, eff)
    for h in (12, 13):
        assert plan[h]["grid_kwh"] <= 100.0 + 1e-4


def test_solar_reduction_effect():
    demand = [100.0] * 24
    solar = [0.0] * 24
    solar[13] = 80.0
    scenario = make_scenario(demand, solar=solar)
    eff = build_effective_constraints(
        scenario, [type("D", (), {"note_index": 0, "applies": True, "directive_type": "solar_reduction",
                                  "structured_adjustment": {"hours": [13], "factor": 0.2}, "explanation": ""})()]
    )
    plan, _, _ = solve_and_validate(scenario, eff)
    # Effective solar is capped at 16 kWh; the plan may use no more.
    assert plan[13]["solar_used_kwh"] <= 80.0 * 0.2 + 1e-4
    # With a reduced solar cap at hour 13, more demand is met from grid or
    # battery than in the unrestricted optimum.
    eff_free = build_effective_constraints(scenario, [])
    plan_free, _, _ = solve_and_validate(scenario, eff_free)
    assert plan[13]["grid_kwh"] >= plan_free[13]["grid_kwh"] - 1e-4


def test_high_demand_exhausts_battery_neutrality_forces_grid():
    demand = [500.0] * 24
    scenario = make_scenario(demand)
    plan, totals, _ = solve_and_validate(scenario)
    # Grid must cover everything the battery cannot sustainably provide.
    assert totals["total_grid_kwh"] >= 500.0 * 24 - 500.0  # battery cycles at most its capacity


def test_zero_solar_zero_demand_edge():
    demand = [0.0] * 24
    scenario = make_scenario(demand)
    plan, totals, stage = solve_and_validate(scenario)
    assert stage == 0
    assert totals["total_grid_kwh"] == 0.0
    assert totals["total_cost_bdt"] == 0.0


def test_boundary_hours_in_windows():
    demand = [100.0] * 24
    scenario = make_scenario(demand)
    eff = build_effective_constraints(
        scenario, [type("D", (), {"note_index": 0, "applies": True, "directive_type": "no_charge_window",
                                  "structured_adjustment": {"hours": [0, 23]}, "explanation": ""})()]
    )
    plan, _, _ = solve_and_validate(scenario, eff)
    assert plan[0]["battery_action"] != "charge"
    assert plan[23]["battery_action"] != "charge"


def test_validator_catches_balance_violation():
    scenario = make_scenario([100.0] * 24)
    eff = build_effective_constraints(scenario, [])
    plan, _, _ = solve_scenario(scenario, eff)
    broken = [dict(e) for e in plan]
    broken[5]["grid_kwh"] = broken[5]["grid_kwh"] - 10.0
    assert validate_plan(broken, scenario, eff) != []


def test_validator_catches_neutrality_violation():
    scenario = make_scenario([100.0] * 24, initial=300.0)
    eff = build_effective_constraints(scenario, [])
    plan, _, _ = solve_scenario(scenario, eff)
    broken = [dict(e) for e in plan]
    broken[23]["battery_energy_after_kwh"] = 100.0
    assert validate_plan(broken, scenario, eff) != []


def test_validator_catches_reserve_violation():
    scenario = make_scenario([100.0] * 24, initial=150.0)
    eff = build_effective_constraints(
        scenario, [type("D", (), {"note_index": 0, "applies": True, "directive_type": "minimum_battery_reserve",
                                  "structured_adjustment": {"hours": [10], "minimum_energy_kwh": 200.0}, "explanation": ""})()]
    )
    plan, _, _ = solve_scenario(scenario, eff)
    assert validate_plan(plan, scenario, eff) == []
    broken = [dict(e) for e in plan]
    broken[10]["battery_energy_after_kwh"] = 150.0
    assert validate_plan(broken, scenario, eff) != []


def test_validator_catches_rate_limit_violation():
    scenario = make_scenario([100.0] * 24)
    eff = build_effective_constraints(scenario, [])
    plan, _, _ = solve_scenario(scenario, eff)
    broken = [dict(e) for e in plan]
    # Make hour 5 a 150 kWh charge (rate limit is 100).
    broken[5]["battery_action"] = "charge"
    broken[5]["battery_kwh"] = 150.0
    # Repair balance/transition so only the rate limit violation remains is not
    # required; validator reports all violations including the rate limit.
    errors = validate_plan(broken, scenario, eff)
    assert any("rate limit" in e for e in errors)
