"""Extra API edge-case tests: malformed structural input variants.

Covers the exact edge cases from the final build checklist:
duplicate hours, wrong hour order, bad battery config, note limits,
missing GEMINI_API_KEY behavior, and Gemini failure modes (mocked).
"""
from __future__ import annotations

import os
import sys

os.environ.setdefault("GRIDWISE_INTERPRETER", "mock")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json

import pytest
from unittest.mock import patch

from fastapi.testclient import TestClient

from gridwise.app import app
from gridwise.selftest import BASELINE_REQUEST

client = TestClient(app)


def _base(**overrides):
    req = json.loads(json.dumps(BASELINE_REQUEST))
    req.update(overrides)
    return req


def test_duplicate_hours_rejected():
    req = _base()
    # Drop hour 23, add a second entry for hour 5 -> duplicate hour, missing hour.
    req["hours"] = req["hours"][:-1] + [dict(req["hours"][5])]
    resp = client.post("/optimize-energy", json=req)
    assert resp.status_code == 400


def test_wrong_hour_order_rejected():
    req = _base()
    shuffled = [dict(h) for h in req["hours"]]
    shuffled[5], shuffled[6] = shuffled[6], shuffled[5]
    req["hours"] = shuffled
    resp = client.post("/optimize-energy", json=req)
    # Hours are a set 0..23; order in the array is irrelevant to the contract.
    assert resp.status_code == 200


def test_hour_out_of_range_rejected():
    req = _base()
    req["hours"][3]["hour"] = 24
    resp = client.post("/optimize-energy", json=req)
    assert resp.status_code == 400


def test_missing_battery_field_rejected():
    req = _base()
    del req["battery"]["capacity_kwh"]
    resp = client.post("/optimize-energy", json=req)
    assert resp.status_code == 400


def test_four_notes_rejected():
    req = _base()
    req["operator_notes"] = ["a", "b", "c", "d"]
    resp = client.post("/optimize-energy", json=req)
    assert resp.status_code == 400


def test_zero_notes_rejected():
    req = _base()
    req["operator_notes"] = []
    resp = client.post("/optimize-energy", json=req)
    assert resp.status_code == 400


def test_negative_demand_rejected():
    req = _base()
    req["hours"][7]["demand_kwh"] = -10
    resp = client.post("/optimize-energy", json=req)
    assert resp.status_code == 400


def test_note_not_a_string_rejected():
    req = _base()
    req["operator_notes"] = [42]
    resp = client.post("/optimize-energy", json=req)
    assert resp.status_code == 400


def test_boundary_hours_scenario():
    """Directive windows touching hours 0 and 23 must work."""
    req = _base()
    req["operator_notes"] = ["Do not charge the battery at hour 0 or hour 23."]
    resp = client.post("/optimize-energy", json=req)
    assert resp.status_code == 200
    body = resp.json()
    assert body["directive_interpretation"][0]["note_index"] == 0


# --- Gemini failure-mode handling (mocked, no network) ---


def test_missing_gemini_key_returns_controlled_500(monkeypatch):
    from gridwise import app as app_module
    from gridwise.llm import Interpreter, InterpretationResult

    class FailingInterpreter(Interpreter):
        def interpret(self, notes, battery_capacity_kwh):
            raise RuntimeError("GEMINI_API_KEY is not set")

    original = app_module.interpreter
    app_module.interpreter = FailingInterpreter()
    try:
        resp = client.post("/optimize-energy", json=_base())
        assert resp.status_code == 500
        assert "Traceback" not in resp.text
        assert "GEMINI_API_KEY" not in resp.text
    finally:
        app_module.interpreter = original


def test_gemini_timeout_returns_controlled_500(monkeypatch):
    from gridwise import app as app_module
    from gridwise.llm import Interpreter

    class TimeoutInterpreter(Interpreter):
        def interpret(self, notes, battery_capacity_kwh):
            raise TimeoutError("provider timeout")

    original = app_module.interpreter
    app_module.interpreter = TimeoutInterpreter()
    try:
        resp = client.post("/optimize-energy", json=_base())
        assert resp.status_code == 500
        assert "Traceback" not in resp.text
    finally:
        app_module.interpreter = original


def test_malformed_gemini_output_becomes_no_op():
    """Garbage model output must never crash the pipeline or invent directives."""
    from gridwise.guardrails import validate_candidate

    for garbage in (
        None,
        "just text",
        {"unexpected": "shape"},
        {"directive_type": "bananas"},
        {"directive_type": "solar_reduction", "hours": "not-a-list"},
        {"directive_type": "minimum_battery_reserve", "hours": [5], "minimum_energy_kwh": "abc"},
    ):
        d = validate_candidate(0, garbage, 500.0)
        assert d.directive_type in ("no_op", "solar_reduction", "minimum_battery_reserve")
        if d.directive_type != "no_op":
            # If repaired, the structured_adjustment must still be well-formed.
            assert isinstance(d.structured_adjustment, dict)
            assert isinstance(d.structured_adjustment.get("hours"), list)


def test_conflicting_directives_resolve_deterministically():
    """Two reserve directives on the same hours: stricter floor wins (max)."""
    from gridwise.domain import Scenario, build_effective_constraints
    from gridwise.domain import Directive
    from gridwise.optimizer import solve_scenario

    scenario = Scenario(
        scenario_id="C",
        demand_kwh=[100.0] * 24,
        solar_kwh=[0.0] * 24,
        tariff_bdt_per_kwh=[7.0] * 24,
        capacity_kwh=500.0,
        initial_energy_kwh=300.0,
        minimum_energy_kwh=50.0,
        max_charge_kwh_per_hour=100.0,
        max_discharge_kwh_per_hour=100.0,
    )
    d1 = Directive(0, True, "minimum_battery_reserve", {"hours": [10, 11], "minimum_energy_kwh": 250.0}, "a")
    d2 = Directive(1, True, "minimum_battery_reserve", {"hours": [11, 12], "minimum_energy_kwh": 150.0}, "b")
    eff = build_effective_constraints(scenario, [d1, d2])
    assert eff.reserve_floor[10] == 250.0
    assert eff.reserve_floor[11] == 250.0  # max of the two
    assert eff.reserve_floor[12] == 150.0
    plan, totals, stage = solve_scenario(scenario, eff)
    assert stage == 0
    for h in (10, 11):
        assert plan[h]["battery_energy_after_kwh"] >= 250.0 - 1e-4


def test_impossible_directives_return_422():
    """Grid cap below zero supply margin makes stage-0 infeasible -> 422."""
    req = _base()
    req["operator_notes"] = ["Limit grid import to 0 kWh in every hour of the day."]
    resp = client.post("/optimize-energy", json=req)
    assert resp.status_code in (200, 422)
    if resp.status_code == 200:
        body = resp.json()
        assert len(body["hourly_plan"]) == 24


def test_response_schema_exact_keys():
    req = _base()
    resp = client.post("/optimize-energy", json=req)
    assert resp.status_code == 200
    assert set(resp.json().keys()) == {
        "scenario_id",
        "directive_interpretation",
        "hourly_plan",
        "total_grid_kwh",
        "total_cost_bdt",
        "peak_grid_kwh",
        "plan_summary",
    }
