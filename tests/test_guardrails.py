"""Unit tests for the deterministic guardrail layer (gridwise.guardrails)."""
from __future__ import annotations

import os
import sys

os.environ.setdefault("GRIDWISE_INTERPRETER", "mock")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gridwise.guardrails import make_no_op, validate_candidate

CAP = 500.0


def test_supported_type_passes_through():
    d = validate_candidate(
        0,
        {"directive_type": "solar_reduction", "applies": True, "hours": [13, 14], "factor": 0.2, "explanation": "x"},
        CAP,
    )
    assert d.applies is True
    assert d.directive_type == "solar_reduction"
    assert d.structured_adjustment == {"hours": [13, 14], "factor": 0.2}


def test_unknown_type_becomes_no_op():
    d = validate_candidate(0, {"directive_type": "shutdown_reactor"}, CAP)
    assert d.directive_type == "no_op"
    assert d.applies is False
    assert d.structured_adjustment is None


def test_missing_type_becomes_no_op():
    d = validate_candidate(0, {}, CAP)
    assert d.directive_type == "no_op"


def test_no_op_normalized():
    d = validate_candidate(
        1, {"directive_type": "no_op", "applies": True, "hours": [1], "factor": 0.5}, CAP
    )
    assert d.applies is False
    assert d.structured_adjustment is None


def test_typed_directive_with_applies_false_becomes_no_op():
    d = validate_candidate(
        1, {"directive_type": "no_charge_window", "applies": False, "hours": [5]}, CAP
    )
    assert d.applies is False
    assert d.directive_type == "no_op"


def test_hours_deduplicated_and_sorted():
    d = validate_candidate(
        0, {"directive_type": "no_charge_window", "applies": True, "hours": [15, 14, 14]}, CAP
    )
    # Duplicate/unordered hours are repaired to unique ascending form.
    assert d.applies is True
    assert d.structured_adjustment == {"hours": [14, 15]}


def test_hours_out_of_range_rejected():
    d = validate_candidate(0, {"directive_type": "no_charge_window", "applies": True, "hours": [22, 24]}, CAP)
    assert d.directive_type == "no_op"
    d = validate_candidate(0, {"directive_type": "no_charge_window", "applies": True, "hours": [-1, 3]}, CAP)
    assert d.directive_type == "no_op"


def test_factor_bounds():
    d = validate_candidate(
        0, {"directive_type": "solar_reduction", "applies": True, "hours": [12], "factor": 1.3}, CAP
    )
    assert d.directive_type == "no_op"
    d = validate_candidate(
        0, {"directive_type": "solar_reduction", "applies": True, "hours": [12], "factor": -0.1}, CAP
    )
    assert d.directive_type == "no_op"
    d = validate_candidate(
        0, {"directive_type": "solar_reduction", "applies": True, "hours": [12], "factor": 0.20000001}, CAP
    )
    assert d.structured_adjustment["factor"] == 0.2


def test_reserve_bounds():
    d = validate_candidate(
        0,
        {"directive_type": "minimum_battery_reserve", "applies": True, "hours": [18, 19, 20], "minimum_energy_kwh": 120},
        CAP,
    )
    assert d.structured_adjustment == {"hours": [18, 19, 20], "minimum_energy_kwh": 120.0}
    d = validate_candidate(
        0,
        {"directive_type": "minimum_battery_reserve", "applies": True, "hours": [18], "minimum_energy_kwh": 99999},
        CAP,
    )
    assert d.directive_type == "no_op"


def test_grid_cap_bounds():
    d = validate_candidate(
        0, {"directive_type": "max_grid_window", "applies": True, "hours": [9], "max_grid_kwh": -5}, CAP
    )
    assert d.directive_type == "no_op"
    d = validate_candidate(
        0, {"directive_type": "max_grid_window", "applies": True, "hours": [9], "max_grid_kwh": 150}, CAP
    )
    assert d.structured_adjustment == {"hours": [9], "max_grid_kwh": 150.0}


def test_no_op_fallback_explanation_is_capped():
    d = make_no_op(0, "x" * 1000)
    assert len(d.explanation) <= 300
