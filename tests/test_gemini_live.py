"""Optional live test against the real Gemini API.

Skipped unless GEMINI_API_KEY is available (from the environment or the
project .env), so CI and local runs stay offline-safe. Run explicitly with:
    python -m pytest tests/test_gemini_live.py -q
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gridwise.config  # noqa: F401,E402 - loads .env so the key is visible

pytestmark = pytest.mark.skipif(
    not os.getenv("GEMINI_API_KEY"), reason="GEMINI_API_KEY not set; live LLM test skipped"
)


def test_gemini_paraphrase_variants():
    from gridwise.config import get_settings
    from gridwise.llm import GeminiInterpreter

    interp = GeminiInterpreter(
        api_key=os.environ["GEMINI_API_KEY"],
        model=get_settings().gemini_model,
    )
    notes = [
        "PV production will drop to about 20% between 13:00 and 15:00.",   # solar_reduction
        "The cafeteria menu changes tomorrow.",                            # no_op
        "Hold no less than 120 kWh of charge in the battery from 6 PM until 9 PM.",  # reserve
    ]
    result = interp.interpret(notes, battery_capacity_kwh=500.0)
    d0, d1, d2 = result.directives

    assert d0.directive_type == "solar_reduction"
    assert d0.applies is True
    assert d0.structured_adjustment["hours"] == [13, 14]
    assert abs(d0.structured_adjustment["factor"] - 0.2) <= 0.01

    assert d1.directive_type == "no_op"
    assert d1.applies is False
    assert d1.structured_adjustment is None

    assert d2.directive_type == "minimum_battery_reserve"
    assert d2.structured_adjustment["hours"] == [18, 19, 20]
    assert abs(d2.structured_adjustment["minimum_energy_kwh"] - 120.0) <= 0.01
