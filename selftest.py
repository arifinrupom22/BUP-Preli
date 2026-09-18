#!/usr/bin/env python3
"""Offline self-check for the GridWise service.

Runs the full pipeline in-process with the mock interpreter (no network, no
API key) and verifies:
  1. /health returns {"status": "ok"},
  2. /optimize-energy returns the full Section 10 contract,
  3. totals are consistent with hourly_plan,
  4. health still responds afterwards (no shared-state corruption),
  5. the interpreter layer rejects/neutralizes malformed model output.

Exit code 0 = all checks passed.

Usage:  python selftest.py
"""
from __future__ import annotations

import json
import os
import sys

os.environ.setdefault("GRIDWISE_INTERPRETER", "mock")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi.testclient import TestClient  # noqa: E402

from gridwise.app import app  # noqa: E402
from gridwise.guardrails import validate_candidate  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  PASS {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {detail}")


def main() -> int:
    client = TestClient(app)

    print("[1/5] /health")
    r = client.get("/health")
    check("status 200", r.status_code == 200)
    check("body is {'status': 'ok'}", r.json() == {"status": "ok"})

    print("[2/5] /optimize-energy full contract")
    with open(os.path.join("examples", "baseline_request.json"), "r", encoding="utf-8") as fh:
        payload = json.load(fh)
    r = client.post("/optimize-energy", json=payload)
    check("status 200", r.status_code == 200, r.text[:200])
    body = r.json()
    for key in (
        "scenario_id",
        "directive_interpretation",
        "hourly_plan",
        "total_grid_kwh",
        "total_cost_bdt",
        "peak_grid_kwh",
        "plan_summary",
    ):
        check(f"has {key}", key in body)
    check("scenario_id echo", body.get("scenario_id") == payload["scenario_id"])
    check("24 plan entries", len(body.get("hourly_plan", [])) == 24)
    check("3 interpretations", len(body.get("directive_interpretation", [])) == 3)
    check(
        "note_index order",
        [e["note_index"] for e in body.get("directive_interpretation", [])] == [0, 1, 2],
    )

    print("[3/5] totals consistency")
    plan = body["hourly_plan"]
    grid = [p["grid_kwh"] for p in plan]
    check("total_grid_kwh", abs(sum(grid) - body["total_grid_kwh"]) < 0.01)
    cost = sum(p["grid_kwh"] * h["tariff_bdt_per_kwh"] for p, h in zip(plan, payload["hours"]))
    check("total_cost_bdt", abs(cost - body["total_cost_bdt"]) < 0.01)
    check("peak_grid_kwh", abs(max(grid) - body["peak_grid_kwh"]) < 0.01)

    print("[4/5] service still healthy after optimize")
    r2 = client.get("/health")
    check("health after optimize", r2.status_code == 200 and r2.json() == {"status": "ok"})

    print("[5/5] malformed LLM output is neutralized")
    d = validate_candidate(0, {"directive_type": "warp_drive"}, 500.0)
    check("unknown type -> no_op", d.directive_type == "no_op" and d.applies is False)
    d = validate_candidate(0, {"directive_type": "solar_reduction", "applies": True, "hours": [25], "factor": 0.2}, 500.0)
    check("bad hours -> no_op", d.directive_type == "no_op")
    d = validate_candidate(0, {"directive_type": "solar_reduction", "applies": True, "hours": [13], "factor": 7.5}, 500.0)
    check("bad factor -> no_op", d.directive_type == "no_op")

    print(f"\nSelftest: {PASS} passed, {FAIL} failed")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
