"""API contract tests: health, valid flow, schema echo, no_op, error control."""
from __future__ import annotations

import os
import sys

import pytest

os.environ.setdefault("GRIDWISE_INTERPRETER", "mock")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient

from gridwise.app import app
from gridwise.selftest import BASELINE_REQUEST

client = TestClient(app)


def test_health():
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"status": "ok"}


def test_optimize_baseline_three_notes():
    resp = client.post("/optimize-energy", json=BASELINE_REQUEST)
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # Top-level schema (PS 10.1).
    assert body["scenario_id"] == "GRID-101"
    assert len(body["hourly_plan"]) == 24
    assert len(body["directive_interpretation"]) == 3
    for key in ("total_grid_kwh", "total_cost_bdt", "peak_grid_kwh", "plan_summary"):
        assert key in body
    assert isinstance(body["plan_summary"], str) and body["plan_summary"]

    # Interpretation entries in note_index order with applies semantics.
    interp = body["directive_interpretation"]
    assert [e["note_index"] for e in interp] == [0, 1, 2]
    assert interp[0]["directive_type"] == "solar_reduction"
    assert interp[0]["applies"] is True
    assert interp[0]["structured_adjustment"] == {"hours": [13, 14], "factor": 0.2}
    assert interp[1]["directive_type"] == "no_charge_window"
    assert interp[1]["structured_adjustment"] == {"hours": [14, 15]}
    assert interp[2]["directive_type"] == "no_op"
    assert interp[2]["applies"] is False
    assert interp[2]["structured_adjustment"] is None

    # Hourly plan entry schema (PS 10.3).
    for entry in body["hourly_plan"]:
        assert set(entry.keys()) == {
            "hour",
            "grid_kwh",
            "solar_used_kwh",
            "battery_action",
            "battery_kwh",
            "battery_energy_after_kwh",
        }
        assert entry["battery_action"] in ("charge", "discharge", "idle")

    # Totals consistent with plan.
    grid = [e["grid_kwh"] for e in body["hourly_plan"]]
    assert abs(sum(grid) - body["total_grid_kwh"]) < 0.01
    assert abs(max(grid) - body["peak_grid_kwh"]) < 0.01


def test_directives_actually_applied():
    """The plan must obey the interpreted directives (not just report them)."""
    resp = client.post("/optimize-energy", json=BASELINE_REQUEST)
    body = resp.json()
    plan = body["hourly_plan"]

    # no_charge_window at [14, 15]: no charging in those hours.
    for h in (14, 15):
        entry = plan[h]
        assert not (entry["battery_action"] == "charge" and entry["battery_kwh"] > 0)

    # solar_reduction at [13, 14] with factor 0.2: solar used <= 20% of base.
    base_solar = {e["hour"]: e["solar_kwh"] for e in BASELINE_REQUEST["hours"]}
    for h in (13, 14):
        assert plan[h]["solar_used_kwh"] <= base_solar[h] * 0.2 + 1e-4


def test_all_no_op_notes_still_optimize():
    req = dict(BASELINE_REQUEST)
    req["scenario_id"] = "GRID-NOOP"
    req["operator_notes"] = ["The cafeteria menu changes tomorrow."]
    resp = client.post("/optimize-energy", json=req)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["directive_interpretation"][0]["directive_type"] == "no_op"
    assert body["directive_interpretation"][0]["applies"] is False
    assert body["directive_interpretation"][0]["structured_adjustment"] is None
    assert len(body["hourly_plan"]) == 24


def test_malformed_json_returns_400():
    resp = client.post(
        "/optimize-energy",
        content=b"{not valid json",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 400
    assert "detail" in resp.json()


def test_missing_fields_return_400():
    resp = client.post("/optimize-energy", json={"scenario_id": "X"})
    assert resp.status_code == 400


def test_wrong_hours_count_returns_400():
    req = dict(BASELINE_REQUEST)
    req["hours"] = BASELINE_REQUEST["hours"][:23]
    resp = client.post("/optimize-energy", json=req)
    assert resp.status_code == 400


def test_bad_note_types_return_400():
    req = dict(BASELINE_REQUEST)
    req["operator_notes"] = ["", "ok note"]
    resp = client.post("/optimize-energy", json=req)
    assert resp.status_code == 400


def test_extra_fields_tolerated():
    """Benign extra fields are ignored rather than hard-failing the judge."""
    req = dict(BASELINE_REQUEST)
    req["unexpected_field"] = 1
    resp = client.post("/optimize-energy", json=req)
    assert resp.status_code == 200


def test_no_stack_traces_or_secrets_in_errors():
    resp = client.post("/optimize-energy", json={"scenario_id": 12345})
    assert resp.status_code == 400
    text = resp.text
    assert "Traceback" not in text
    assert "GEMINI_API_KEY" not in text
    for secret in (os.environ.get("GEMINI_API_KEY", ""),):
        if secret:
            assert secret not in text
