"""LLM operator-note interpretation.

The language model is a mandatory part of the interpretation path (PS Section
02): natural-language notes go in, a structured directive candidate comes out.
Its output is always untrusted and is validated by gridwise.guardrails before
anything reaches the optimizer.

Provider: Google Gemini (google-genai SDK) with native JSON-schema structured
output. If the model's first attempt fails guardrail validation, we retry with
the specific violation appended to the conversation - a corrective loop, not a
blind retry.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from typing import Any, Dict, List, Optional

from gridwise.config import get_settings
from gridwise.guardrails import make_no_op, validate_candidate

# ---------------------------------------------------------------------------
# JSON Schema sent to the model (enforced by Gemini's responseSchema).
# ---------------------------------------------------------------------------

RESPONSE_SCHEMA: Dict[str, Any] = {
    "type": "OBJECT",
    "properties": {
        "directive_type": {
            "type": "STRING",
            "enum": [
                "solar_reduction",
                "minimum_battery_reserve",
                "no_charge_window",
                "no_discharge_window",
                "max_grid_window",
                "no_op",
            ],
        },
        "applies": {"type": "BOOLEAN"},
        "explanation": {"type": "STRING"},
        "hours": {"type": "ARRAY", "items": {"type": "INTEGER", "minimum": 0, "maximum": 23}},
        "factor": {"type": "NUMBER", "nullable": True},
        "minimum_energy_kwh": {"type": "NUMBER", "nullable": True},
        "max_grid_kwh": {"type": "NUMBER", "nullable": True},
    },
    "required": ["directive_type", "applies", "explanation", "hours"],
    "propertyOrdering": [
        "directive_type",
        "applies",
        "explanation",
        "hours",
        "factor",
        "minimum_energy_kwh",
        "max_grid_kwh",
    ],
}

DIRECTIVE_TYPE_FIELDS = (
    "solar_reduction",
    "minimum_battery_reserve",
    "no_charge_window",
    "no_discharge_window",
    "max_grid_window",
)

# Model fallback chain: tried in order when a model id is retired (404) or a
# model hits free-tier quota/capacity pressure (429/503). All chain members
# use separate free-tier quota pools, so a rate-limited primary model
# transparently fails over. Verified live: gemini-3.5-flash-lite responds in
# ~1s with correct structured output.
FALLBACK_MODELS = (
    "gemini-flash-lite-latest",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
)

SYSTEM_PROMPT = """You are the directive interpreter for a campus energy scheduling system.

You receive ONE operator note. Decide whether it changes TODAY's 24-hour energy
schedule, and if so map it to EXACTLY ONE supported directive type.

Supported directive types:
- solar_reduction: usable solar output is reduced in specific hours.
  "factor" is the FRACTION THAT REMAINS. An 80% reduction means factor = 0.2.
  A drop "to about 20%" also means factor = 0.2.
- minimum_battery_reserve: battery energy must stay at or above a level
  (minimum_energy_kwh, in kWh) in specific hours.
- no_charge_window: battery charging is forbidden in specific hours.
- no_discharge_window: battery discharging is forbidden in specific hours.
- max_grid_window: grid import may not exceed max_grid_kwh (in kWh) in
  specific hours.
- no_op: the note does not affect today's energy schedule.

Time rules:
- Whole-hour windows. The start hour is INCLUDED and the end hour is EXCLUDED.
  "from 1 PM to 3 PM" and "between 13:00 and 15:00" mean hours [13, 14].
  "from 6 PM until 9 PM" means hours [18, 19, 20].
- Hour 0 is midnight, 12 is noon, 13 is 1 PM, 23 is 11 PM.

Interpretation rules:
- Output ONLY the structured object; no extra prose.
- For no_op: applies = false, and set all numeric fields to null with hours [].
- For every other type: applies = true and fill the fields required by that
  type (leave the other numeric fields null).
- Never invent demand, solar, tariff, or battery values. Never invent an
  unsupported directive type. If the note is irrelevant chatter, a question,
  about another day, or about anything other than the five supported rules,
  choose no_op.

Field mapping:
- solar_reduction: factor = remaining fraction.
- minimum_battery_reserve: minimum_energy_kwh = required level in kWh.
- max_grid_window: max_grid_kwh = the cap in kWh.
"""


class InterpretationResult:
    def __init__(self, directives: List[Any], model_used: str, attempts: int):
        self.directives = directives
        self.model_used = model_used
        self.attempts = attempts


class Interpreter:
    """Common interface implemented by both real and mock interpreters."""

    def interpret(self, notes: List[str], battery_capacity_kwh: float) -> InterpretationResult:
        raise NotImplementedError


class GeminiInterpreter(Interpreter):
    """Google Gemini interpreter with structured output and a corrective retry."""

    def __init__(
        self,
        api_key: str,
        model: str,
        max_attempts: int = 3,
        timeout_s: float = 20.0,
        deadline_s: float = 24.0,
    ):
        from google import genai  # imported lazily so mock mode never needs it
        from google.genai import types

        self._client = genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(timeout=int(timeout_s * 1000)),  # ms
        )
        # Model chain: configured model first, then stable fallbacks.
        self._models: List[str] = []
        for candidate in (model, *FALLBACK_MODELS):
            if candidate and candidate not in self._models:
                self._models.append(candidate)
        self._model = self._models[0]
        self._good_model_idx = 0  # last known-working model
        self._max_attempts = max(1, int(max_attempts))
        self._timeout_s = float(timeout_s)
        self._deadline_s = float(deadline_s)

    @staticmethod
    def _is_model_missing(exc: Exception) -> bool:
        """Permanent per-model failure (retired/unknown id): try the next model."""
        text = str(exc).lower()
        return "404" in text or "not found" in text or "no longer available" in text

    @staticmethod
    def _is_transient(exc: Exception) -> bool:
        """Capacity/rate/network pressure worth retrying (on another model)."""
        name = type(exc).__name__.lower()
        if "timeout" in name or "deadline" in name or "connection" in name:
            return True
        text = str(exc).lower()
        return any(
            marker in text
            for marker in (
                "503",
                "500",
                "502",
                "504",
                "429",
                "unavailable",
                "resource_exhausted",
                "rate limit",
                "overload",
                "high demand",
            )
        )

    def _call(self, model: str, note: str) -> Dict[str, Any]:
        from google.genai import types

        response = self._client.models.generate_content(
            model=model,
            contents=note,
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                temperature=0.0,
                response_mime_type="application/json",
                response_schema=RESPONSE_SCHEMA,
            ),
        )
        text = response.text
        if not text:
            raise ValueError("empty model response")
        return json.loads(text)

    def _call_bounded(self, model: str, note: str, budget_s: float) -> Dict[str, Any]:
        """One model call with a HARD wall-clock budget.

        The google-genai SDK retries transient errors internally, so a single
        call can hang far beyond the configured HTTP timeout when the provider
        is degraded. We wait on a daemon thread and abandon the attempt when
        the budget expires; the API's latency budget is never hostage to the
        SDK's internal retry policy.
        """
        outcome: Dict[str, Any] = {}

        def _run() -> None:
            try:
                outcome["value"] = self._call(model, note)
            except BaseException as exc:  # noqa: BLE001 - re-raised below
                outcome["error"] = exc

        worker = threading.Thread(target=_run, daemon=True)
        worker.start()
        worker.join(timeout=max(1.0, budget_s))
        if worker.is_alive():
            raise TimeoutError(f"model {model} exceeded its {budget_s:.1f}s budget")
        if "error" in outcome:
            raise outcome["error"]
        return outcome["value"]

    def _call_with_fallback(self, note: str, deadline: float) -> Optional[Dict[str, Any]]:
        """Try the model chain; return raw JSON, or None if all models failed.

        Retired/unknown model ids are skipped immediately; transient
        capacity/rate errors get a brief deadline-aware pause before the next
        model. The last known-working model is preferred for later calls.
        Every attempt is hard-bounded so the total stays inside the deadline.
        """
        for offset in range(len(self._models)):
            mi = (self._good_model_idx + offset) % len(self._models)
            model = self._models[mi]
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            budget = max(2.0, min(self._timeout_s, remaining))
            try:
                raw = self._call_bounded(model, note, budget)
                self._good_model_idx = mi
                return raw
            except Exception as exc:
                if self._is_model_missing(exc):
                    continue
                if self._is_transient(exc):
                    left = deadline - time.monotonic()
                    if left > 0:
                        time.sleep(min(1.0, left))
                    continue
                raise  # unexpected non-transient error: handled by the caller
        return None

    def _interpret_one(self, index: int, note: str, battery_capacity_kwh: float) -> Any:
        """Interpret one note: model fallback chain + corrective retry loop,
        bounded by a hard per-note deadline (keeps the 30 s API budget even
        when the provider is degraded)."""
        deadline = time.monotonic() + self._deadline_s
        note_text = note
        last_candidate: Any = None
        for attempt in range(1, self._max_attempts + 1):
            if time.monotonic() >= deadline:
                break
            raw = self._call_with_fallback(note_text, deadline)
            if raw is None:  # whole chain failed within the deadline
                break
            candidate = validate_candidate(index, raw, battery_capacity_kwh)
            if candidate.directive_type != "no_op" or _raw_was_no_op(raw):
                return candidate
            # The model named a directive but produced an invalid one: retry
            # with the guardrail complaint, then accept the safe no_op.
            last_candidate = candidate
            if time.monotonic() >= deadline:
                return candidate
            note_text = (
                note_text
                + "\n\nYour previous interpretation was rejected. "
                + str(candidate.explanation)
                + " Return a corrected structured interpretation."
            )
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(1.0 * attempt, remaining))
        if last_candidate is not None and last_candidate.directive_type == "no_op":
            return last_candidate
        return make_no_op(
            index,
            "Interpretation service was temporarily unavailable; "
            "treated as not affecting the schedule.",
        )

    def interpret(self, notes: List[str], battery_capacity_kwh: float) -> InterpretationResult:
        """Interpret all notes. Calls run in parallel (there is no
        inter-note dependency), keeping total latency near one call even for
        3-note scenarios."""
        from concurrent.futures import ThreadPoolExecutor

        if not notes:
            return InterpretationResult(directives=[], model_used=self._model, attempts=0)
        attempts_used = 0
        with ThreadPoolExecutor(max_workers=len(notes)) as pool:
            futures = [
                pool.submit(self._interpret_one, i, n, battery_capacity_kwh)
                for i, n in enumerate(notes)
            ]
            directives = [f.result() for f in futures]
        attempts_used = len(notes)  # informational; retries tracked per note
        model_used = self._models[self._good_model_idx]
        return InterpretationResult(directives=directives, model_used=model_used, attempts=attempts_used)


def _raw_was_no_op(raw: Dict[str, Any]) -> bool:
    return isinstance(raw, dict) and raw.get("directive_type") == "no_op"


class MockInterpreter(Interpreter):
    """Deterministic test double. NEVER used for judged runs.

    Understands a small fixed set of phrasings so unit tests can exercise the
    full pipeline without network access.
    """

    def __init__(self):
        self.calls = 0

    def interpret(self, notes: List[str], battery_capacity_kwh: float) -> InterpretationResult:
        self.calls += 1
        directives = []
        for index, note in enumerate(notes):
            directives.append(self._mock_one(index, note))
        return InterpretationResult(directives=directives, model_used="mock", attempts=self.calls)

    def _mock_one(self, index: int, note: str) -> Any:
        text = note.lower()
        if "cafeteria" in text or "menu" in text:
            return make_no_op(index, "Irrelevant operator note; no energy impact.")

        if "solar" in text or "pv" in text or "panel" in text:
            factor = self._factor_from(text)
            hours = self._hours_from(text)
            if factor is not None and hours:
                return self._directive(index, "solar_reduction", {"hours": hours, "factor": factor})
        if "reserve" in text or ("keep" in text and "kwh" in text):
            hours = self._hours_from(text)
            level = self._number_before_kwh(text)
            if hours and level is not None:
                return self._directive(
                    index, "minimum_battery_reserve", {"hours": hours, "minimum_energy_kwh": level}
                )
        if "not charge" in text or "don't charge" in text or "do not charge" in text:
            hours = self._hours_from(text)
            if hours:
                return self._directive(index, "no_charge_window", {"hours": hours})
        if "not discharge" in text or "don't discharge" in text or "do not discharge" in text:
            hours = self._hours_from(text)
            if hours:
                return self._directive(index, "no_discharge_window", {"hours": hours})
        if "grid" in text and ("below" in text or "not exceed" in text or "max" in text):
            hours = self._hours_from(text)
            cap = self._number_before_kwh(text)
            if hours and cap is not None:
                return self._directive(index, "max_grid_window", {"hours": hours, "max_grid_kwh": cap})
        return make_no_op(index, "No supported rule recognized by the mock interpreter.")

    def _directive(self, index: int, dtype: str, adjustment: Dict[str, Any]) -> Any:
        from gridwise.domain import Directive

        return Directive(
            note_index=index,
            applies=True,
            directive_type=dtype,
            structured_adjustment=adjustment,
            explanation=f"Mock interpretation: {dtype}.",
        )

    def _hours_from(self, text: str) -> Optional[List[int]]:
        """Parse the FIRST whole-hour time range in the note.

        Understands '1 PM', '13:00', '1-3 PM', '1 PM to 3 PM'. Times with no
        meridiem in a range are treated as 24h clock times (e.g. '1-3 PM' is
        [13, 14] because PM applies to the range). Numbers that are clearly
        not clock times (percentages, kWh values, capacities) are ignored.
        """
        import re

        text = text.lower()
        # Drop numbers that are quantities, not times.
        cleaned = re.sub(r"(\d+(?:\.\d+)?)\s*(%|kwh|kw|bdt)", " ", text)
        cleaned = re.sub(r"\b(at least|no less than|about|roughly|approximately|around)\b", " ", cleaned)

        candidates: List[tuple] = []
        # Range forms: '1 pm to 3 pm', '13:00 to 15:00', '1-3 pm', '1 to 3 pm'
        for m in re.finditer(
            r"\b(\d{1,2})(?::00)?\s*(am|pm)?\s*(?:-|–|to|until|till|through|and)\s*(\d{1,2})(?::00)?\s*(am|pm)?\b",
            cleaned,
        ):
            a, mera, b, merb = int(m.group(1)), m.group(2), int(m.group(3)), m.group(4)
            mer = merb or mera
            if a > 23 or b > 23:
                continue
            if mer == "pm" and a < 12:
                a += 12
            if mer == "pm" and b < 12:
                b += 12
            if mer == "am" and a == 12:
                a = 0
            if mer == "am" and b == 12:
                b = 0
            candidates.append((a, b))
        # Single-time form: 'from 1 pm', 'at 14:00'
        if not candidates:
            m = re.search(r"\b(\d{1,2})(?::00)?\s*(am|pm)?\b", cleaned)
            if m:
                a, mer = int(m.group(1)), m.group(2)
                if a <= 23:
                    if mer == "pm" and a < 12:
                        a += 12
                    if mer == "am" and a == 12:
                        a = 0
                    candidates.append((a, None))
        if not candidates:
            return None
        a, b = candidates[0]
        if b is None or b <= a:
            return [a]
        return list(range(a, b))

    def _factor_from(self, text: str) -> Optional[float]:
        """Map percentage wording to the remaining-fraction factor.

        'drop to about 20%' / 'reduced to 20%'   -> remaining 20% -> 0.2
        '80% reduction' / 'reduce usage by 80%'  -> 1 - 0.8      -> 0.2
        """
        import re

        lowered = text.lower()
        match = re.search(r"(\d{1,3})\s*%", lowered)
        if not match:
            fractions = {"one-fifth": 0.2, "one fifth": 0.2, "one quarter": 0.25, "half": 0.5}
            for word, value in fractions.items():
                if word in lowered:
                    return value
            return None
        pct = float(match.group(1)) / 100.0
        # 'to (about)? N%' states the REMAINING output level.
        if re.search(r"\bto\s+(?:about\s+|roughly\s+|approximately\s+|around\s+)?\d{1,3}\s*%", lowered):
            return round(pct, 6)
        # Otherwise a reduction verb (reduce/cut/drop/fall/less/lower) means
        # the percentage is the amount LOST.
        if re.search(r"\b(reduc|cut|drop|fall|less|lower|decreas)", lowered):
            return round(1.0 - pct, 6)
        return round(pct, 6)

    def _number_before_kwh(self, text: str) -> Optional[float]:
        import re

        match = re.search(r"(\d+(?:\.\d+)?)\s*kwh", text)
        return float(match.group(1)) if match else None


class CachedInterpreter(Interpreter):
    """TTL cache keyed by (note, capacity). Identical repeated notes skip the
    network call; different notes always get a fresh interpretation."""

    def __init__(self, inner: Interpreter, max_size: int, ttl_s: float):
        self._inner = inner
        self._max_size = max(0, int(max_size))
        self._ttl = float(ttl_s)
        self._store: Dict[str, Any] = {}

    def interpret(self, notes: List[str], battery_capacity_kwh: float) -> InterpretationResult:
        key = hashlib.sha256(
            json.dumps({"notes": notes, "cap": battery_capacity_kwh}, sort_keys=True).encode()
        ).hexdigest()
        hit = self._store.get(key)
        if hit is not None and (self._ttl <= 0 or time.time() - hit[0] <= self._ttl):
            return hit[1]
        result = self._inner.interpret(notes, battery_capacity_kwh)
        if self._max_size > 0:
            if len(self._store) >= self._max_size:
                self._store.clear()
            self._store[key] = (time.time(), result)
        return result


def build_interpreter() -> Interpreter:
    """Factory honoring GRIDWISE_INTERPRETER = api (default) | mock."""
    settings = get_settings()
    if settings.interpreter == "mock":
        return MockInterpreter()
    if not settings.gemini_api_key:
        raise RuntimeError(
            "GEMINI_API_KEY is not set. Configure it in the environment or .env, "
            "or set GRIDWISE_INTERPRETER=mock for offline testing."
        )
    inner = GeminiInterpreter(
        api_key=settings.gemini_api_key,
        model=settings.gemini_model,
        max_attempts=settings.llm_max_attempts,
        timeout_s=settings.llm_timeout_s,
    )
    if settings.cache_size > 0 and settings.cache_ttl_s > 0:
        return CachedInterpreter(inner, settings.cache_size, settings.cache_ttl_s)
    return inner
