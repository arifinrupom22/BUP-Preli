"""Deterministic guardrails: never trust raw LLM output.

Every interpretation candidate is validated here against the Problem Statement
rules. Malformed or out-of-range output is repaired when safe, or rejected and
downgraded to a controlled no_op with a technical explanation.

Key invariants (Problem Statement Sections 05, 08):
- directive_type must be one of the supported types
- applies = false is allowed only for no_op, and no_op always uses
  applies = false with structured_adjustment = null
- hours are unique integers 0..23 in ascending order
- solar factor is within [0, 1] (usable fraction remaining)
- reserve values are finite, non-negative and never exceed capacity
- grid caps are finite and non-negative
"""
from __future__ import annotations

import math
from typing import Any, List, Optional

from gridwise.domain import DIRECTIVE_TYPES, NUM_HOURS, Directive

# Small tolerance for LLM-provided numerics that are within rounding distance
# of a valid value (e.g. 0.20000001 for a 0.2 factor).
NUMERIC_TOLERANCE = 0.01


def make_no_op(note_index: int, explanation: str) -> Directive:
    """Controlled no_op fallback for invalid or irrelevant interpretations."""
    text = (explanation or "").strip()
    if len(text) > 300:
        text = text[:297] + "..."
    return Directive(
        note_index=note_index,
        applies=False,
        directive_type="no_op",
        structured_adjustment=None,
        explanation=text or "This note does not affect the 24-hour energy schedule.",
    )


def _as_finite_float(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    if not math.isfinite(result):
        return None
    return result


def _repair_hours(raw_hours: Any) -> Optional[List[int]]:
    """Normalize an hours value into unique ascending ints 0..23, or None."""
    if not isinstance(raw_hours, (list, tuple)):
        return None
    out: List[int] = []
    for item in raw_hours:
        value = _as_finite_float(item)
        if value is None:
            return None
        nearest = int(round(value))
        if abs(value - nearest) > NUMERIC_TOLERANCE:
            return None
        if nearest < 0 or nearest >= NUM_HOURS:
            return None
        out.append(nearest)
    if not out:
        return None
    # Duplicates collapse (same directive intent); ordering is normalized.
    return sorted(set(out))


def validate_candidate(
    note_index: int,
    raw: Any,
    battery_capacity_kwh: float,
) -> Directive:
    """Validate one raw LLM interpretation and return a safe Directive.

    Anything that cannot be brought into a supported shape becomes a no_op.
    """
    raw_dict: dict = {}
    if isinstance(raw, dict):
        raw_dict = raw

    # --- Shape normalization: the model returns flat fields (hours, factor,
    # minimum_energy_kwh, max_grid_kwh); the contract uses a nested
    # structured_adjustment. Build the nested view deterministically here.
    adj_raw: Any = raw_dict.get("structured_adjustment")
    if not isinstance(adj_raw, dict):
        adj_raw = {
            k: raw_dict[k]
            for k in ("hours", "factor", "minimum_energy_kwh", "max_grid_kwh")
            if k in raw_dict
        }

    dtype_raw = raw_dict.get("directive_type")
    applies_raw = raw_dict.get("applies")
    explanation = raw_dict.get("explanation")
    explanation_text = explanation.strip() if isinstance(explanation, str) else ""

    # Unknown / missing / unsupported directive type -> controlled no_op.
    if not isinstance(dtype_raw, str):
        return make_no_op(
            note_index,
            "Interpretation did not identify a supported directive type; treated as not affecting the schedule. "
            + explanation_text,
        )
    dtype = dtype_raw.strip().lower()
    if dtype not in DIRECTIVE_TYPES:
        return make_no_op(
            note_index,
            "Interpretation used an unsupported directive type; treated as not affecting the schedule. "
            + explanation_text,
        )

    # no_op must always be returned as applies=false with null adjustment.
    if dtype == "no_op":
        return make_no_op(note_index, explanation_text or "This note does not affect the 24-hour energy schedule.")

    # Applies/type consistency: applies=false is only valid for no_op. When the
    # model emits a typed directive with applies=false (or a contradictory
    # applies value), the safe normalization is a full no_op.
    if isinstance(applies_raw, bool) and applies_raw is False:
        return make_no_op(note_index, explanation_text or "Note marked as not applicable.")

    if dtype in ("no_charge_window", "no_discharge_window"):
        if not isinstance(adj_raw, dict):
            return make_no_op(note_index, "Directive was missing its hours window; treated as no_op. " + explanation_text)
        hours = _repair_hours(adj_raw.get("hours"))
        if hours is None:
            return make_no_op(note_index, "Directive hours were invalid; treated as no_op. " + explanation_text)
        return Directive(
            note_index=note_index,
            applies=True,
            directive_type=dtype,
            structured_adjustment={"hours": hours},
            explanation=explanation_text or ("Battery charging is not allowed in the listed hours." if dtype == "no_charge_window" else "Battery discharging is not allowed in the listed hours."),
        )

    if dtype == "solar_reduction":
        if not isinstance(adj_raw, dict):
            return make_no_op(note_index, "Solar reduction was missing its adjustment; treated as no_op. " + explanation_text)
        hours = _repair_hours(adj_raw.get("hours"))
        factor = _as_finite_float(adj_raw.get("factor"))
        if hours is None or factor is None:
            return make_no_op(note_index, "Solar reduction hours or factor were invalid; treated as no_op. " + explanation_text)
        if factor < -NUMERIC_TOLERANCE or factor > 1.0 + NUMERIC_TOLERANCE:
            return make_no_op(note_index, "Solar reduction factor was outside [0, 1]; treated as no_op. " + explanation_text)
        factor = round(min(1.0, max(0.0, factor)), 6)
        return Directive(
            note_index=note_index,
            applies=True,
            directive_type=dtype,
            structured_adjustment={"hours": hours, "factor": factor},
            explanation=explanation_text or "Usable solar output is reduced to the given fraction in the listed hours.",
        )

    if dtype == "minimum_battery_reserve":
        if not isinstance(adj_raw, dict):
            return make_no_op(note_index, "Battery reserve was missing its adjustment; treated as no_op. " + explanation_text)
        hours = _repair_hours(adj_raw.get("hours"))
        level = _as_finite_float(adj_raw.get("minimum_energy_kwh"))
        if hours is None or level is None:
            return make_no_op(note_index, "Battery reserve hours or level were invalid; treated as no_op. " + explanation_text)
        if level < -NUMERIC_TOLERANCE or level > battery_capacity_kwh + NUMERIC_TOLERANCE:
            return make_no_op(note_index, "Battery reserve level was outside [0, capacity]; treated as no_op. " + explanation_text)
        level = round(min(battery_capacity_kwh, max(0.0, level)), 6)
        return Directive(
            note_index=note_index,
            applies=True,
            directive_type=dtype,
            structured_adjustment={"hours": hours, "minimum_energy_kwh": level},
            explanation=explanation_text or "Battery must stay at or above the given reserve level in the listed hours.",
        )

    if dtype == "max_grid_window":
        if not isinstance(adj_raw, dict):
            return make_no_op(note_index, "Grid cap was missing its adjustment; treated as no_op. " + explanation_text)
        hours = _repair_hours(adj_raw.get("hours"))
        cap = _as_finite_float(adj_raw.get("max_grid_kwh"))
        if hours is None or cap is None:
            return make_no_op(note_index, "Grid cap hours or value were invalid; treated as no_op. " + explanation_text)
        if cap < -NUMERIC_TOLERANCE:
            return make_no_op(note_index, "Grid cap was negative; treated as no_op. " + explanation_text)
        cap = round(max(0.0, cap), 6)
        return Directive(
            note_index=note_index,
            applies=True,
            directive_type=dtype,
            structured_adjustment={"hours": hours, "max_grid_kwh": cap},
            explanation=explanation_text or "Grid import may not exceed the given amount in the listed hours.",
        )

    # Unreachable: every supported dtype is handled above.
    return make_no_op(note_index, "Interpretation could not be validated; treated as no_op. " + explanation_text)


def finalize_interpretations(candidates: List[Directive], num_notes: int) -> List[Directive]:
    """Ensure exactly one entry per note, in note_index order 0..N-1.

    Missing or out-of-range indices are filled with controlled no_ops so the
    response schema can never be violated, even if the interpreter misbehaved.
    """
    by_index: dict = {}
    for d in candidates:
        if isinstance(d, Directive) and 0 <= d.note_index < num_notes and d.note_index not in by_index:
            by_index[d.note_index] = d
    result: List[Directive] = []
    for i in range(num_notes):
        result.append(by_index.get(i) or make_no_op(i, "No interpretation was produced for this note."))
    return result
