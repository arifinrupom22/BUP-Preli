"""HTTP API layer.

Endpoints (Problem Statement Section 06):
  GET  /health           -> 200 {"status": "ok"}
  POST /optimize-energy  -> 200 with interpretation + plan, or a controlled
                            400/422/500. No secrets or stack traces are ever
                            returned.

The response contract (Section 10) is assembled here; totals are recomputed
from hourly_plan and verified by the independent validator before shipping.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, List

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from gridwise.config import get_settings
from gridwise.domain import Scenario, build_effective_constraints
from gridwise.guardrails import finalize_interpretations
from gridwise.llm import build_interpreter
from gridwise.optimizer import OptimizationError, solve_scenario, stage_validation_flags
from gridwise.schemas import OptimizeRequest
from gridwise.validator import validate_plan, validate_totals

logger = logging.getLogger("gridwise")

_settings = get_settings()

# CORS: the browser dashboard must be able to call this API. Development
# defaults allow localhost origins; production sets FRONTEND_ORIGIN to the
# deployed Netlify origin. No secrets are ever exposed to the frontend.
_allowed_origins = [
    "http://localhost:5500",
    "http://127.0.0.1:5500",
    "http://localhost:8000",
    "http://127.0.0.1:8000",
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "http://localhost:5173",
    "http://127.0.0.1:5173",
]
if _settings.frontend_origin:
    _allowed_origins.append(_settings.frontend_origin)

app = FastAPI(title="GridWise Energy Optimizer", version="1.1.0", docs_url=None, redoc_url=None)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "Accept"],
)

# Interpreter is built once at startup; failures raise a controlled 500 at
# request time rather than crashing the process.
try:
    interpreter = build_interpreter()
    _INTERPRETER_READY = True
except Exception as exc:  # pragma: no cover - startup guard
    interpreter = None
    _INTERPRETER_READY = False
    logger.error("interpreter init failed: %s", type(exc).__name__)


@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok"}


def _scenario_from_request(req: OptimizeRequest) -> Scenario:
    return Scenario(
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


def _serialize_interpretations(directives: List[Any]) -> List[Dict[str, Any]]:
    return [
        {
            "note_index": d.note_index,
            "applies": bool(d.applies),
            "directive_type": d.directive_type,
            "structured_adjustment": d.structured_adjustment,
            "explanation": d.explanation,
        }
        for d in directives
    ]


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"detail": message})


@app.post("/optimize-energy")
async def optimize_energy(request: Request):
    started = time.perf_counter()

    # --- Parse body. Malformed JSON or non-object bodies -> 400.
    try:
        payload = await request.json()
    except Exception:
        return _error(400, "Malformed JSON body.")
    if not isinstance(payload, dict):
        return _error(400, "Request body must be a JSON object.")

    # --- Structural validation -> 400.
    try:
        req = OptimizeRequest.model_validate(payload)
    except ValidationError as exc:
        return _error(400, f"Structurally invalid request: {exc.error_count()} error(s).")

    # --- Interpreter availability -> controlled 500.
    if interpreter is None:
        return _error(500, "Interpretation service is not configured.")
    if not _INTERPRETER_READY:
        return _error(500, "Interpretation service is not ready.")

    scenario = _scenario_from_request(req)

    # --- LLM interpretation (mandatory path) with per-request guard.
    try:
        result = interpreter.interpret(req.operator_notes, scenario.capacity_kwh)
    except Exception:
        logger.exception("interpretation failed")
        return _error(500, "Interpretation service error.")

    directives = finalize_interpretations(result.directives, len(req.operator_notes))

    # --- Directives -> hard constraints -> optimization.
    eff = build_effective_constraints(scenario, directives)
    try:
        plan, totals, stage = solve_scenario(scenario, eff)
    except OptimizationError:
        return _error(422, "No feasible schedule exists for this scenario under the given directives.")

    # --- Independent replay: never ship a plan we cannot verify ourselves.
    violations = validate_plan(plan, scenario, eff, **stage_validation_flags(stage)) + validate_totals(
        plan, scenario.tariff_bdt_per_kwh, totals
    )
    if violations:
        # Controlled failure without leaking internals; logged server-side only.
        logger.error("plan validation failed: %s", "; ".join(violations[:5]))
        return _error(500, "Schedule validation failed internally.")

    # --- Contract response (Section 10).
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    summary = _plan_summary(plan, totals, scenario)
    return {
        "scenario_id": req.scenario_id,
        "directive_interpretation": _serialize_interpretations(directives),
        "hourly_plan": plan,
        "total_grid_kwh": totals["total_grid_kwh"],
        "total_cost_bdt": totals["total_cost_bdt"],
        "peak_grid_kwh": totals["peak_grid_kwh"],
        "plan_summary": summary,
    }


# --- Static dashboard (served by the same service for local demos).
# The frontend is plain HTML/CSS/JS and never contains or receives secrets;
# it talks to this API only. Mounting last so /health and /optimize-energy
# keep priority.
_FRONTEND_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "frontend")
if os.path.isdir(_FRONTEND_DIR):  # pragma: no cover - trivial static mount
    app.mount("/", StaticFiles(directory=_FRONTEND_DIR, html=True), name="frontend")


def _plan_summary(plan: List[dict], totals: Dict[str, float], scenario: Scenario) -> str:
    charge_hours = [p["hour"] for p in plan if p["battery_action"] == "charge"]
    discharge_hours = [p["hour"] for p in plan if p["battery_action"] == "discharge"]
    parts = [
        f"Purchased {totals['total_grid_kwh']:.2f} kWh from the grid at a total cost of "
        f"{totals['total_cost_bdt']:.2f} BDT with a peak hourly import of {totals['peak_grid_kwh']:.2f} kWh."
    ]
    if charge_hours:
        parts.append(f"Battery charges during hours {charge_hours}.")
    else:
        parts.append("Battery does not charge.")
    if discharge_hours:
        parts.append(f"Battery discharges during hours {discharge_hours}.")
    else:
        parts.append("Battery does not discharge.")
    return " ".join(parts)
