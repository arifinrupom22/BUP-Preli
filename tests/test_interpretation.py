"""Interpreter-layer tests (mock double, caching, ordering)."""
from __future__ import annotations

import os
import sys

os.environ.setdefault("GRIDWISE_INTERPRETER", "mock")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gridwise.llm import CachedInterpreter, MockInterpreter
from gridwise.guardrails import finalize_interpretations

CAP = 500.0


def test_mock_solar_reduction():
    d = MockInterpreter().interpret(["Solar output will drop to about 20% from 1 PM to 3 PM."], CAP).directives[0]
    assert d.directive_type == "solar_reduction"
    assert d.applies is True
    assert d.structured_adjustment == {"hours": [13, 14], "factor": 0.2}


def test_mock_no_charge():
    d = MockInterpreter().interpret(["Do not charge the battery between 2 PM and 4 PM."], CAP).directives[0]
    assert d.directive_type == "no_charge_window"
    assert d.structured_adjustment["hours"] == [14, 15]


def test_mock_reserve():
    d = MockInterpreter().interpret(["Keep at least 120 kWh in reserve from 6 PM until 9 PM."], CAP).directives[0]
    assert d.directive_type == "minimum_battery_reserve"
    assert d.structured_adjustment == {"hours": [18, 19, 20], "minimum_energy_kwh": 120.0}


def test_mock_irrelevant_is_no_op():
    d = MockInterpreter().interpret(["The cafeteria menu changes tomorrow."], CAP).directives[0]
    assert d.directive_type == "no_op"
    assert d.applies is False
    assert d.structured_adjustment is None


def test_mock_80pct_reduction_is_factor_02():
    d = MockInterpreter().interpret(["Expect an 80% reduction in rooftop solar during the 1-3 PM window."], CAP).directives[0]
    assert d.directive_type == "solar_reduction"
    assert abs(d.structured_adjustment["factor"] - 0.2) < 1e-9


def test_mock_reduce_by_80pct_is_factor_02():
    d = MockInterpreter().interpret(["Reduce solar usage by 80% from 1 PM to 3 PM."], CAP).directives[0]
    assert d.directive_type == "solar_reduction"
    assert abs(d.structured_adjustment["factor"] - 0.2) < 1e-9


def test_mock_drop_to_20pct_is_factor_02():
    d = MockInterpreter().interpret(["Solar output will drop to about 20% from 1 PM to 3 PM."], CAP).directives[0]
    assert d.directive_type == "solar_reduction"
    assert abs(d.structured_adjustment["factor"] - 0.2) < 1e-9


def test_finalize_fills_missing_and_orders():
    from gridwise.domain import Directive

    partial = [Directive(1, True, "no_charge_window", {"hours": [3]}, "x")]
    out = finalize_interpretations(partial, 3)
    assert [d.note_index for d in out] == [0, 1, 2]
    assert out[0].directive_type == "no_op"
    assert out[2].directive_type == "no_op"


def test_cache_hits_identical_notes():
    inner = MockInterpreter()
    cached = CachedInterpreter(inner, max_size=10, ttl_s=60)
    notes = ["Do not charge the battery between 2 PM and 4 PM."]
    r1 = cached.interpret(notes, CAP)
    r2 = cached.interpret(notes, CAP)
    assert inner.calls == 1  # second call served from cache
    assert r1.directives[0].directive_type == r2.directives[0].directive_type


def test_cache_different_notes_miss():
    inner = MockInterpreter()
    cached = CachedInterpreter(inner, max_size=10, ttl_s=60)
    cached.interpret(["Do not charge the battery between 2 PM and 4 PM."], CAP)
    cached.interpret(["The cafeteria menu changes tomorrow."], CAP)
    assert inner.calls == 2


def test_capacity_is_part_of_cache_key():
    inner = MockInterpreter()
    cached = CachedInterpreter(inner, max_size=10, ttl_s=60)
    notes = ["Keep at least 100 kWh in reserve from 1 PM to 2 PM."]
    cached.interpret(notes, 500.0)
    cached.interpret(notes, 800.0)
    assert inner.calls == 2
