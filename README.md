# GRIDWISE — Smart Campus Energy Optimization

**BUP CSE Fest 2026 · Online Preliminary · LLM-Assisted Operator Directive Interpretation**

GRIDWISE is a full-stack energy-scheduling system for the GridWise challenge: campus
operators write plain-language notes, Google Gemini Flash converts each note into a
structured directive, deterministic guardrails validate it, an exact linear-program
optimizer produces the cheapest valid 24-hour schedule, and an independent replay
validator re-checks every energy rule before the result is shown.

A polished dark-navy operator dashboard (plain HTML/CSS/JS, Netlify-ready) drives the
whole pipeline and visualizes the interpretation, the schedule, and the validated plan.

> **Team / project info:** `<team name>` · `<member names>` · `<contact>`

---

## 1. Challenge objective (summary)

Given 24 hours of demand, solar availability and grid tariff plus a battery, and 1–3
natural-language operator notes, the service must:

1. interpret every note into exactly one machine-checkable directive
   (`solar_reduction`, `minimum_battery_reserve`, `no_charge_window`,
   `no_discharge_window`, `max_grid_window`, or `no_op`),
2. apply the valid directives as hard constraints,
3. minimize `Σ grid_kwh[h] × tariff_bdt_per_kwh[h]`, and
4. return a schedule that satisfies every energy, battery, and directive rule,
   including end-of-day battery neutrality.

The canonical Problem Statement governs all challenge behavior; this README documents
the implementation.

## 2. Architecture

```
Operator notes (1–3)
        │
        ▼
FastAPI request validation ──────────── 400 on malformed/invalid input
        │
        ▼
Gemini Flash interpreter ────────────── 1 note → 1 structured directive
        │   (temperature 0, JSON-schema enforced, parallel per-note calls,
        │    corrective retry, TTL cache)
        ▼
Deterministic guardrails ────────────── types, applies semantics, hours,
        │   numeric ranges; unsafe output → controlled no_op
        ▼
Directive application ───────────────── effective solar, reserve floors,
        │   no-charge/no-discharge hours, grid caps (conflicts: max/strictest)
        ▼
LP optimizer (SciPy + HiGHS) ────────── exact cost-minimal schedule,
        │   directives as hard constraints, staged feasibility ladder
        ▼
Independent replay validator ────────── hour-by-hour energy rules at 1e-4
        │   tolerance + totals recomputed from hourly_plan
        ▼
JSON response (Problem Statement Section 10 contract)
```

**Backend** (`gridwise/`): FastAPI service, the only component that holds the Gemini
key. **Frontend** (`frontend/`): static dashboard, talks only to this API, never to
Gemini, never receives any secret.

| File | Responsibility |
| --- | --- |
| `gridwise/app.py` | HTTP endpoints, CORS, static dashboard mount, error control |
| `gridwise/schemas.py` | Request schema (exactly 24 hours, 1–3 notes) |
| `gridwise/llm.py` | Gemini interpreter, prompt, retry, cache, mock double |
| `gridwise/guardrails.py` | Deterministic validation/repair of model output |
| `gridwise/domain.py` | Scenario model, directive → constraint application |
| `gridwise/optimizer.py` | LP formulation, HiGHS solve, fallback ladder |
| `gridwise/validator.py` | Independent replay validation + totals check |
| `gridwise/config.py` | Environment-variable configuration |
| `gridwise/__main__.py` | Entrypoint, binds `0.0.0.0:${PORT:-8000}` |
| `frontend/` | Static dashboard (index.html, style.css, script.js) |
| `tests/` | 63 automated tests |
| `selftest.py` | One-command offline self-check |
| `run_samples.py` | Scenario-file runner with PASS/FAIL output |

## 3. Gemini role (LLM requirement)

- Provider: **Google Gemini Flash** via the official `google-genai` SDK, free tier.
- Model id is configurable (`GEMINI_MODEL`, default `gemini-2.0-flash`) so a newer
  supported Flash model can be selected without code changes.
- Structured JSON output (`response_schema` enforcement), `temperature = 0`.
- One call per note; independent notes are interpreted **in parallel** (total latency
  ≈ one call even for 3-note scenarios). A TTL cache skips network calls for
  byte-identical repeated note sets (key includes battery capacity).
- If the model's directive fails guardrails, the violation is fed back and the model
  retries (up to `GRIDWISE_LLM_MAX_ATTEMPTS`), then falls back to a controlled
  `no_op` — the service never invents a directive and never crashes on bad output.
- **The API key lives only in the backend environment.** The frontend contains no key
  and makes no Gemini calls.

## 4. Guardrails (deterministic)

- Only the five supported directive types + `no_op` are accepted.
- `applies = false` ⇔ `no_op` ⇔ `structured_adjustment = null`.
- Hours: unique integers 0–23; duplicates collapse, order is normalized, invalid
  values reject the directive to `no_op`.
- `solar_reduction.factor` ∈ [0, 1] — the **remaining** fraction (80% reduction ⇒ 0.2).
- `minimum_energy_kwh` ∈ [0, capacity]; `max_grid_kwh` ≥ 0 (finite).
- Whole-hour windows: start inclusive, end exclusive (`1 PM to 3 PM` → `[13, 14]`).
- Whole-day windows normalize to all 24 hours.

## 5. Optimization & validation

Exact linear program (variables per hour: `grid`, `solar_used`, `charge`, `discharge`,
`soc`) solved by **HiGHS** via `scipy.optimize.linprog` — deterministic and fast
(< 100 ms typically). Enforced: energy balance
(`grid + solar_used + discharge = demand + charge`), solar cap incl. directive-reduced
effective solar (curtailment allowed, no export), battery state transitions, bounds
`[max(base minimum, directive reserve), capacity]`, per-hour charge/discharge rate
limits, grid caps, and end-of-day neutrality. Cost ties are broken by a negligible
epsilon on battery throughput (no effect on real savings). Conflicting directives
compose deterministically (e.g. overlapping reserves take the max floor). If a
scenario is infeasible under all hard constraints, a staged relaxation ladder keeps
the service alive and returns a schema-valid plan instead of crashing.

After optimization, `gridwise/validator.py` — a deliberately separate implementation —
replays the plan hour by hour at 1e-4 tolerance (stricter than the official 0.01) and
recomputes `total_grid_kwh`, `total_cost_bdt`, `peak_grid_kwh` from `hourly_plan`.
A plan that fails replay is never returned; the API responds with a controlled error.

## 6. API

### `GET /health` → 200

```json
{"status": "ok"}
```

### `POST /optimize-energy`

Request (exactly 24 hourly entries, 1–3 notes):

```json
{
  "scenario_id": "GRID-101",
  "operator_notes": [
    "Reduce solar usage by 80% from 1 PM to 3 PM.",
    "Maintain at least 30 kWh battery reserve from 6 PM to 10 PM."
  ],
  "hours": [
    {"hour": 0, "demand_kwh": 200, "solar_kwh": 0, "tariff_bdt_per_kwh": 7}
  ],
  "battery": {
    "capacity_kwh": 500,
    "initial_energy_kwh": 200,
    "minimum_energy_kwh": 50,
    "max_charge_kwh_per_hour": 100,
    "max_discharge_kwh_per_hour": 100
  }
}
```

Response (200):

```json
{
  "scenario_id": "GRID-101",
  "directive_interpretation": [
    {
      "note_index": 0,
      "applies": true,
      "directive_type": "solar_reduction",
      "structured_adjustment": {"hours": [13, 14], "factor": 0.2},
      "explanation": "..."
    }
  ],
  "hourly_plan": [
    {"hour": 0, "grid_kwh": 200.0, "solar_used_kwh": 0.0,
     "battery_action": "idle", "battery_kwh": 0.0,
     "battery_energy_after_kwh": 200.0}
  ],
  "total_grid_kwh": 5074.0,
  "total_cost_bdt": 39241.0,
  "peak_grid_kwh": 320.0,
  "plan_summary": "Purchased 5074.00 kWh ..."
}
```

Status codes: `200` success · `400` malformed/structurally invalid · `422` no feasible
schedule under the directives · `500` controlled internal error (no stack traces, no
secrets).

## 7. Local setup

Requirements: Python 3.11+. Zero-cost: Gemini free tier or the offline mock.

```bash
# 1) Backend
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 2) Gemini API key
cp .env.example .env        # then paste your key into GEMINI_API_KEY=
#    (no key yet? everything still runs with GRIDWISE_INTERPRETER=mock)

# 3) Run (binds 0.0.0.0:${PORT:-8000}; serves the dashboard at /)
python -m gridwise
```

Open **http://localhost:8000** — the dashboard is served by the backend itself. Press
**Load Demo Scenario** (optional) and **Optimize Energy**.

To run the dashboard standalone (e.g. Live Server on :5500), open `frontend/index.html`
and set `API_BASE_URL` at the top of `frontend/script.js` to the backend URL
(`http://localhost:8000` in development).

### Environment variables

| Variable | Required | Purpose |
| --- | --- | --- |
| `GEMINI_API_KEY` | yes for live LLM | Google AI Studio key (backend only) |
| `GEMINI_MODEL` | no | Flash model id (default `gemini-2.0-flash`) |
| `PORT` | no | Backend port (default `8000`) |
| `FRONTEND_ORIGIN` | no | Deployed dashboard origin allowed by CORS |
| `GRIDWISE_INTERPRETER` | no | `api` (default) or `mock` (offline tests) |
| `GRIDWISE_LLM_MAX_ATTEMPTS` | no | Interpreter attempts per note (default 3) |
| `GRIDWISE_LLM_TIMEOUT_S` | no | Per-call LLM timeout seconds (default 20) |
| `GRIDWISE_CACHE_SIZE` | no | Max cached interpretations (default 2048) |
| `GRIDWISE_CACHE_TTL_S` | no | Cache TTL seconds (default 3600) |

`.env` is git-ignored; `.env.example` contains the variable name only. Never commit
or paste real key values anywhere in the repository.

## 8. Testing

```bash
python -m pytest tests/ -q            # full suite (63 tests, offline via mock)
python selftest.py                    # one-command end-to-end self-check
GRIDWISE_INTERPRETER=mock python run_samples.py examples/baseline_request.json
```

The suite covers: health, exact request/response schema, scenario_id echo, malformed
JSON, missing fields, duplicate/out-of-range hours, note-count limits, every directive
type, the 80 %-reduction ⇒ factor 0.2 rule, paraphrases, battery neutrality, zero
solar, high demand, conflicting reserves, impossible directives, validator detection
of corrupted plans, secret-leak checks, missing-key behavior, and Gemini
timeout/garbage-output handling (mocked). `tests/test_gemini_live.py` runs against the
real API when `GEMINI_API_KEY` is set and is skipped otherwise:

```bash
GEMINI_API_KEY=... python -m pytest tests/test_gemini_live.py -q
```

Verified locally (mock mode, no key): 63 passed / 0 failed / 1 skipped (live test);
selftest 21/21; sample runner PASS; live server smoke tests for `/`, `/health`,
`/optimize-energy`, CORS preflight, and the dashboard driving a real optimization.

## 9. Docker (backend)

```bash
docker build -t gridwise:latest .
docker run --rm -p 8000:8000 -e GEMINI_API_KEY=<your key> gridwise:latest
curl http://localhost:8000/health      # {"status":"ok"}
# dashboard: http://localhost:8000/
```

Python 3.11-slim base, binds `0.0.0.0`, exposes `8000`, serves the dashboard, and
bakes in **no secrets** (the key is passed at runtime via `-e`). A registry fallback
image (exact tag/digest) can be pushed from this Dockerfile.

## 10. Netlify deployment (frontend)

The dashboard is a static site — no build tooling.

1. Push this repository to GitHub.
2. In Netlify: **Add new site → Import an existing project** → pick the repo.
3. Netlify reads `netlify.toml`: publish directory `frontend`, no build command.
4. Deploy. Open the site and set the backend URL: edit the single line
   `const API_BASE_URL = "..."` at the top of `frontend/script.js` (commit + redeploy),
   or use Netlify's inline editor.
5. On the backend host set `FRONTEND_ORIGIN=https://<your-site>.netlify.app` so CORS
   allows the dashboard (localhost origins are pre-allowed for development).

Alternatively run the dashboard from the backend itself (any host) and skip Netlify.

## 11. Backend deployment

Any host that runs containers/Python works (Render, Railway, Fly.io, a VPS, …):

- Start command: `python -m gridwise` (binds `0.0.0.0`, honors `PORT`), or the
  Docker image above.
- Set `GEMINI_API_KEY` (and optionally `GEMINI_MODEL`) as host environment variables —
  never in the image or the repo.
- Set `FRONTEND_ORIGIN` to the deployed dashboard origin if you deploy the frontend
  separately.
- Verify externally: `curl https://<backend>/health` and one `POST /optimize-energy`.

## 12. Security

- `.env` git-ignored; `.env.example` holds only the variable name.
- The key exists only in the backend process environment; frontend code never sees or
  sends it, and no Gemini call happens from the browser.
- Docker image contains no credentials.
- Error responses never include stack traces, provider details, or secrets; internal
  diagnostics go to server logs only.
- Only synthetic challenge data is used.

## 13. Demo workflow (60-second judge script)

1. Open the dashboard — header shows **LIVE / API Online**.
2. Press **Load Demo Scenario** (scenario `GRID-101`, two realistic notes).
3. Press **Optimize Energy** — the pipeline strip animates
   Note → Gemini Interpretation → Guardrails → Optimization → Validation.
4. Result (~instant in mock mode, ~1–3 s live): summary cards, per-note directive
   interpretation with structured adjustments, the 24-hour chart (demand / solar /
   grid / battery delta with hover tooltips), the full hourly table with
   charge/discharge/idle badges, and **✓ PLAN VALIDATED**.
5. Point out: factor `0.2` for an "80% reduction" (remaining-fraction rule), battery
   ending exactly at its initial energy (neutrality), and the guardrail/no_op story
   for irrelevant notes.

## 14. Limitations

- Live interpretation quality depends on Gemini availability/latency; the free tier
  has rate limits (mitigated by parallel calls, cache, retries; a run falls back to
  controlled `no_op`s, never a crash).
- The offline `mock` interpreter exists only for tests/demos without a key — judged
  runs must use `GRIDWISE_INTERPRETER=api` with a valid key.
- If a scenario is genuinely infeasible under its directives the API returns 422
  (or a ladder-relaxed plan in extreme cases), which may lose optimization credit
  for that case — valid organizer scenarios are guaranteed feasible.
- The dashboard ships one built-in demo scenario (per the challenge demo requirement);
  it does not edit the 24-hour input arrays inline.

## 15. Verification status

Actually executed on this machine (mock mode, `GEMINI_API_KEY` not yet configured):

- `python -m pytest tests/ -q` → **63 passed, 1 skipped** (the skipped test is the
  live-Gemini check, which requires the key).
- `python selftest.py` → 21/21 checks passed.
- `GRIDWISE_INTERPRETER=mock python run_samples.py examples/baseline_request.json`
  → `PASS GRID-101 (grid=5074.0 kWh, cost=39241.0 BDT)`.
- Live server: `GET /` 200 (dashboard), `GET /health` `{"status":"ok"}`,
  `POST /optimize-energy` 200 with a validated plan, CORS preflight verified for
  localhost and rejected for unknown origins, dashboard-driven optimization verified
  in a real browser.
- Secret scan: no key material anywhere in the repo; `.env` absent and ignored.
- Docker build: **pending** — the Docker daemon was not running on this machine at
  build time; run the Section 9 commands once Docker Desktop is up.
- Live Gemini end-to-end: **pending** — add `GEMINI_API_KEY` to `.env`, then run
  `python -m pytest tests/test_gemini_live.py -q` and one real dashboard optimization.
