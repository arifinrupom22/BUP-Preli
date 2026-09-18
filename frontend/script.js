/* ============================================================
   GRIDWISE — Smart Campus Energy Optimization
   Frontend logic: health status, operator panel, results, chart.
   The browser NEVER sees or sends the Gemini API key; it talks
   only to this project's backend, which owns the LLM call.
   ============================================================ */

/* ------------------------------------------------------------
   CONFIGURATION — the only line you need to edit.
   Local development:  http://localhost:8000
   Production:         https://your-deployed-backend.example.com
   ------------------------------------------------------------ */
const API_BASE_URL = "http://localhost:8000";

/* ---------------- State ---------------- */
let notes = [
  "Reduce solar usage by 80% from 1 PM to 3 PM.",
  "Maintain at least 30 kWh battery reserve from 6 PM to 10 PM.",
];
let optimizing = false;

/* Resolved API base. API_BASE_URL above is authoritative; when the dashboard
   is served by the backend itself (local demo, Docker), the same origin is
   used automatically so the page works on any port without edits. */
let resolvedApiBase = API_BASE_URL;

const $ = (id) => document.getElementById(id);

const DEMO_SCENARIO = {
  scenario_id: "GRID-101",
  hours: [
    { hour: 0, demand_kwh: 200, solar_kwh: 0, tariff_bdt_per_kwh: 7 },
    { hour: 1, demand_kwh: 190, solar_kwh: 0, tariff_bdt_per_kwh: 7 },
    { hour: 2, demand_kwh: 185, solar_kwh: 0, tariff_bdt_per_kwh: 7 },
    { hour: 3, demand_kwh: 180, solar_kwh: 0, tariff_bdt_per_kwh: 7 },
    { hour: 4, demand_kwh: 185, solar_kwh: 0, tariff_bdt_per_kwh: 7 },
    { hour: 5, demand_kwh: 200, solar_kwh: 0, tariff_bdt_per_kwh: 7 },
    { hour: 6, demand_kwh: 220, solar_kwh: 30, tariff_bdt_per_kwh: 7 },
    { hour: 7, demand_kwh: 240, solar_kwh: 60, tariff_bdt_per_kwh: 7 },
    { hour: 8, demand_kwh: 240, solar_kwh: 90, tariff_bdt_per_kwh: 7 },
    { hour: 9, demand_kwh: 235, solar_kwh: 110, tariff_bdt_per_kwh: 7 },
    { hour: 10, demand_kwh: 230, solar_kwh: 110, tariff_bdt_per_kwh: 7 },
    { hour: 11, demand_kwh: 225, solar_kwh: 90, tariff_bdt_per_kwh: 7 },
    { hour: 12, demand_kwh: 230, solar_kwh: 60, tariff_bdt_per_kwh: 9 },
    { hour: 13, demand_kwh: 240, solar_kwh: 30, tariff_bdt_per_kwh: 9 },
    { hour: 14, demand_kwh: 250, solar_kwh: 0, tariff_bdt_per_kwh: 9 },
    { hour: 15, demand_kwh: 255, solar_kwh: 0, tariff_bdt_per_kwh: 9 },
    { hour: 16, demand_kwh: 260, solar_kwh: 0, tariff_bdt_per_kwh: 9 },
    { hour: 17, demand_kwh: 270, solar_kwh: 0, tariff_bdt_per_kwh: 9 },
    { hour: 18, demand_kwh: 280, solar_kwh: 0, tariff_bdt_per_kwh: 8 },
    { hour: 19, demand_kwh: 290, solar_kwh: 0, tariff_bdt_per_kwh: 8 },
    { hour: 20, demand_kwh: 285, solar_kwh: 0, tariff_bdt_per_kwh: 8 },
    { hour: 21, demand_kwh: 270, solar_kwh: 0, tariff_bdt_per_kwh: 8 },
    { hour: 22, demand_kwh: 250, solar_kwh: 0, tariff_bdt_per_kwh: 8 },
    { hour: 23, demand_kwh: 220, solar_kwh: 0, tariff_bdt_per_kwh: 8 },
  ],
  battery: {
    capacity_kwh: 500,
    initial_energy_kwh: 200,
    minimum_energy_kwh: 50,
    max_charge_kwh_per_hour: 100,
    max_discharge_kwh_per_hour: 100,
  },
};

/* ---------------- Notes UI ---------------- */
function renderNotes() {
  const list = $("notes-list");
  list.innerHTML = "";
  notes.forEach((note, i) => {
    const row = document.createElement("div");
    row.className = "note-row";

    const idx = document.createElement("span");
    idx.className = "note-index";
    idx.textContent = String(i);

    const input = document.createElement("input");
    input.type = "text";
    input.value = note;
    input.placeholder = i === 0 ? "e.g. Reduce solar usage by 80% from 1 PM to 3 PM." : "e.g. Do not charge the battery between 2 PM and 4 PM.";
    input.addEventListener("input", () => { notes[i] = input.value; });

    const remove = document.createElement("button");
    remove.className = "btn btn-ghost btn-small";
    remove.type = "button";
    remove.textContent = "✕";
    remove.title = "Remove note";
    remove.disabled = notes.length <= 1;
    remove.addEventListener("click", () => {
      if (notes.length > 1) { notes.splice(i, 1); renderNotes(); }
    });

    row.appendChild(idx);
    row.appendChild(input);
    row.appendChild(remove);
    list.appendChild(row);
  });
  $("btn-add-note").disabled = notes.length >= 3;
}

/* ---------------- Pipeline animation ---------------- */
const STAGES = ["note", "llm", "guard", "opt", "val"];
let pipelineTimer = null;

function pipelineReset() {
  document.querySelectorAll(".pipeline-step").forEach((el) => {
    el.classList.remove("active", "done", "failed");
  });
}
function pipelineStart() {
  pipelineReset();
  let i = 0;
  const advance = () => {
    document.querySelectorAll(".pipeline-step").forEach((el, j) => {
      el.classList.toggle("active", j === i);
      el.classList.toggle("done", j < i);
    });
    i = (i + 1) % STAGES.length;
  };
  advance();
  pipelineTimer = setInterval(advance, 700);
}
function pipelineDone(ok) {
  if (pipelineTimer) { clearInterval(pipelineTimer); pipelineTimer = null; }
  document.querySelectorAll(".pipeline-step").forEach((el) => {
    el.classList.remove("active");
    el.classList.add(ok ? "done" : "failed");
  });
}

/* ---------------- Health ---------------- */
async function probeHealth(base) {
  const controller = new AbortController();
  const t = setTimeout(() => controller.abort(), 5000);
  try {
    const resp = await fetch(`${base}/health`, { signal: controller.signal });
    if (resp.ok) {
      const body = await resp.json();
      if (body && body.status === "ok") return true;
    }
    return false;
  } catch {
    return false;
  } finally {
    clearTimeout(t);
  }
}

async function checkHealth() {
  const pill = $("api-status");
  const text = $("api-status-text");
  const badge = $("env-badge");

  // Probe the configured API and the same origin in parallel (the dashboard
  // may be served by the backend itself, e.g. locally or in Docker). The
  // configured API_BASE_URL wins when both respond.
  const sameOrigin = window.location.origin && window.location.origin !== API_BASE_URL
    ? window.location.origin
    : null;
  const [cfgOk, sameOk] = await Promise.all([
    probeHealth(API_BASE_URL),
    sameOrigin ? probeHealth(sameOrigin) : Promise.resolve(false),
  ]);

  const base = cfgOk ? API_BASE_URL : sameOk ? sameOrigin : null;
  if (base) {
    resolvedApiBase = base;
    pill.dataset.state = "ok";
    text.textContent = "API Online";
    badge.textContent = "LIVE";
    badge.classList.remove("badge-demo");
    badge.classList.add("badge-live");
    return true;
  }
  pill.dataset.state = "down";
  text.textContent = "API Offline";
  badge.textContent = "DEMO";
  badge.classList.add("badge-demo");
  badge.classList.remove("badge-live");
  return false;
}

/* ---------------- Errors ---------------- */
function showError(message) {
  const banner = $("error-banner");
  banner.textContent = message;
  banner.hidden = false;
  clearTimeout(showError._t);
  showError._t = setTimeout(() => { banner.hidden = true; }, 9000);
}

/* ---------------- Optimize ---------------- */
async function optimize() {
  if (optimizing) return;
  const cleaned = notes.map((n) => n.trim()).filter((n) => n.length > 0);
  if (cleaned.length === 0) {
    showError("Add at least one operator note before optimizing.");
    return;
  }
  const scenarioId = $("scenario-id").value.trim() || "GRID-UI-1";
  const payload = {
    scenario_id: scenarioId,
    operator_notes: cleaned.slice(0, 3),
    hours: DEMO_SCENARIO.hours,
    battery: DEMO_SCENARIO.battery,
  };

  optimizing = true;
  $("btn-optimize").disabled = true;
  const status = $("optimize-status");
  status.hidden = false;
  status.classList.remove("error");
  status.textContent = "Interpreting notes and optimizing…";
  pipelineStart();

  const started = performance.now();
  try {
    const controller = new AbortController();
    const t = setTimeout(() => controller.abort(), 45000);
    const resp = await fetch(`${resolvedApiBase}/optimize-energy`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      signal: controller.signal,
    });
    clearTimeout(t);

    if (resp.status === 400) {
      const body = await resp.json().catch(() => ({}));
      throw new Error(`Bad request (400): ${body.detail || "the scenario payload was structurally invalid"}`);
    }
    if (resp.status === 422) {
      const body = await resp.json().catch(() => ({}));
      throw new Error(`Infeasible scenario (422): ${body.detail || "no valid schedule exists for these directives"}`);
    }
    if (resp.status === 500) {
      const body = await resp.json().catch(() => ({}));
      throw new Error(`Backend error (500): ${body.detail || "the server reported an internal error"}`);
    }
    if (!resp.ok) throw new Error(`Unexpected HTTP status ${resp.status}`);

    const body = await resp.json();
    validateResponseShape(body);
    renderResults(payload, body);
    pipelineDone(true);
    status.textContent = `Completed in ${Math.round(performance.now() - started)} ms`;
  } catch (err) {
    pipelineDone(false);
    status.classList.add("error");
    if (err.name === "AbortError") {
      status.textContent = "Timed out after 45 s";
      showError("The optimization request timed out. The backend may be starting up or overloaded — try again.");
    } else if (err instanceof TypeError && /fetch/i.test(String(err))) {
      status.textContent = "Backend unreachable";
      showError(`Cannot reach the backend at ${resolvedApiBase}. Start it locally with "python -m gridwise" or set API_BASE_URL in script.js.`);
      checkHealth();
    } else {
      status.textContent = "Failed";
      showError(err.message || String(err));
    }
  } finally {
    optimizing = false;
    $("btn-optimize").disabled = false;
  }
}

function validateResponseShape(body) {
  const required = ["scenario_id", "directive_interpretation", "hourly_plan", "total_grid_kwh", "total_cost_bdt", "peak_grid_kwh", "plan_summary"];
  for (const key of required) {
    if (!(key in body)) throw new Error(`Invalid response: missing "${key}" — the backend contract may have changed.`);
  }
  if (!Array.isArray(body.hourly_plan) || body.hourly_plan.length !== 24) {
    throw new Error('Invalid response: "hourly_plan" must contain exactly 24 entries.');
  }
  if (!Array.isArray(body.directive_interpretation)) {
    throw new Error('Invalid response: "directive_interpretation" must be an array.');
  }
}

/* ---------------- Rendering ---------------- */
const fmt = (v, d = 1) => Number(v).toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });

function renderResults(request, body) {
  $("results").hidden = false;
  $("scenario-label").textContent = body.scenario_id;
  $("validation-badge").className = "validation-badge ok";
  $("validation-badge").textContent = "✓ PLAN VALIDATED";

  const plan = body.hourly_plan;
  $("stat-total-grid").textContent = fmt(body.total_grid_kwh);
  $("stat-total-cost").textContent = fmt(body.total_cost_bdt);
  $("stat-peak-grid").textContent = fmt(body.peak_grid_kwh);
  const finalB = plan[plan.length - 1].battery_energy_after_kwh;
  $("stat-final-battery").textContent = fmt(finalB);

  renderInterpretations(body.directive_interpretation);
  renderPlanTable(plan);
  $("plan-summary").textContent = body.plan_summary || "";
  drawChart(DEMO_SCENARIO.hours, plan);
  $("results").scrollIntoView({ behavior: "smooth", block: "start" });
}

function renderInterpretations(entries) {
  const list = $("interpretation-list");
  list.innerHTML = "";
  entries.forEach((e) => {
    const card = document.createElement("div");
    card.className = "interp-card";

    const note = document.createElement("div");
    note.className = "interp-note";
    const tag = document.createElement("span");
    tag.className = "note-tag";
    tag.textContent = `note ${e.note_index}`;
    note.appendChild(tag);
    note.appendChild(document.createTextNode(notes[e.note_index] || "(note)"));

    const meta = document.createElement("div");
    meta.className = "interp-meta";
    const type = document.createElement("span");
    type.className = `badge-type ${e.directive_type}`;
    type.textContent = e.directive_type;
    const applies = document.createElement("span");
    applies.className = `badge-applies ${e.applies ? "yes" : "no"}`;
    applies.textContent = e.applies ? "applies" : "not applied";
    meta.appendChild(type);
    meta.appendChild(applies);

    const adj = document.createElement("div");
    adj.className = "interp-adj";
    adj.textContent = e.structured_adjustment === null ? "structured_adjustment = null" : JSON.stringify(e.structured_adjustment);

    const expl = document.createElement("div");
    expl.className = "interp-expl";
    expl.textContent = e.explanation || "";

    card.appendChild(note);
    card.appendChild(meta);
    card.appendChild(adj);
    card.appendChild(expl);
    list.appendChild(card);
  });
}

function renderPlanTable(plan) {
  const tbody = $("plan-body");
  tbody.innerHTML = "";
  plan.forEach((p) => {
    const tr = document.createElement("tr");
    const cells = [
      String(p.hour).padStart(2, "0"),
      fmt(p.grid_kwh),
      fmt(p.solar_used_kwh),
      null,
      fmt(p.battery_kwh),
      fmt(p.battery_energy_after_kwh),
    ];
    cells.forEach((value, idx) => {
      const td = document.createElement("td");
      if (idx === 0) td.className = "hour-cell";
      if (idx === 3) {
        const badge = document.createElement("span");
        badge.className = `action-badge action-${p.battery_action}`;
        badge.textContent = p.battery_action;
        td.appendChild(badge);
      } else {
        td.textContent = value;
      }
      tr.appendChild(td);
    });
    tbody.appendChild(tr);
  });
}

/* ---------------- Chart (dependency-free canvas) ---------------- */
let chartGeometry = null;
let chartHoverHour = null;

function drawChart(hours, plan) {
  const canvas = $("energy-chart");
  const errorBox = $("chart-error");
  try {
    const dpr = window.devicePixelRatio || 1;
    const cssW = canvas.clientWidth || canvas.parentElement.clientWidth;
    const cssH = 270;
    canvas.width = Math.round(cssW * dpr);
    canvas.height = Math.round(cssH * dpr);
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, cssW, cssH);

    const padL = 44, padR = 14, padT = 14, padB = 26;
    const W = cssW - padL - padR, H = cssH - padT - padB;
    const demand = hours.map((h) => h.demand_kwh);
    const grid = plan.map((p) => p.grid_kwh);
    const solar = plan.map((p) => p.solar_used_kwh);
    const batt = plan.map((p) => (p.battery_action === "charge" ? p.battery_kwh : p.battery_action === "discharge" ? -p.battery_kwh : 0));
    const peak = Math.max(...demand, ...grid, 1) * 1.08;
    const x = (h) => padL + (h / 23) * W;
    const y = (v) => padT + H - (v / peak) * (H / 2) - H / 4;
    const yB = (v) => padT + H / 2 - (v / peak) * (H / 2);

    // grid lines + labels
    ctx.font = "10px 'JetBrains Mono', monospace";
    ctx.textAlign = "right";
    ctx.textBaseline = "middle";
    const steps = 4;
    for (let i = 0; i <= steps; i++) {
      const v = (peak / steps) * i;
      const yy = y(v);
      ctx.strokeStyle = "rgba(99,130,180,0.12)";
      ctx.beginPath(); ctx.moveTo(padL, yy); ctx.lineTo(padL + W, yy); ctx.stroke();
      ctx.fillStyle = "rgba(143,163,194,0.8)";
      ctx.fillText(String(Math.round(v)), padL - 7, yy);
    }
    // x labels (sparser on narrow screens so they never collide)
    ctx.textAlign = "center";
    ctx.textBaseline = "top";
    const labelStep = cssW < 520 ? 6 : 3;
    for (let h = 0; h < 24; h += labelStep) {
      ctx.fillStyle = "rgba(100,120,154,0.9)";
      ctx.fillText(`${String(h).padStart(2, "0")}:00`, x(h), padT + H + 7);
    }

    const line = (values, color, width, dash) => {
      ctx.strokeStyle = color;
      ctx.lineWidth = width;
      ctx.setLineDash(dash || []);
      ctx.beginPath();
      values.forEach((v, h) => (h === 0 ? ctx.moveTo(x(h), y(v)) : ctx.lineTo(x(h), y(v))));
      ctx.stroke();
      ctx.setLineDash([]);
    };

    // solar area
    ctx.beginPath();
    ctx.moveTo(x(0), y(0));
    solar.forEach((v, h) => ctx.lineTo(x(h), y(v)));
    ctx.lineTo(x(23), y(0));
    ctx.closePath();
    const grad = ctx.createLinearGradient(0, padT, 0, padT + H);
    grad.addColorStop(0, "rgba(251,191,36,0.28)");
    grad.addColorStop(1, "rgba(251,191,36,0.02)");
    ctx.fillStyle = grad;
    ctx.fill();

    line(demand, "rgba(167,139,250,0.95)", 2);
    line(grid, "rgba(56,189,248,0.95)", 2);

    // battery bars around mid-line
    const barW = Math.max(3, (W / 24) * 0.42);
    batt.forEach((v, h) => {
      if (v === 0) return;
      ctx.fillStyle = v > 0 ? "rgba(52,211,153,0.75)" : "rgba(251,191,36,0.75)";
      const yTop = v > 0 ? yB(v) : padT + H / 2;
      const hgt = Math.abs(yB(v) - (padT + H / 2));
      ctx.fillRect(x(h) - barW / 2, yTop, barW, Math.max(2, hgt));
    });

    // grid dots (hover targets)
    chartGeometry = { padL, padT, W, H, peak, x, y, grid, demand, solar, batt };
    canvas.onmousemove = onChartHover;
    canvas.onmouseleave = onChartLeave;
    attachTouchHandlers(canvas);
    drawHoverHighlight(ctx, padL, padT, W, H, x, y, barW);
    errorBox.hidden = true;
  } catch {
    errorBox.hidden = false;
  }
}

function drawHoverHighlight(ctx, padL, padT, W, H, x, y, barW) {
  if (chartHoverHour === null || !chartGeometry) return;
  const g = chartGeometry;
  if (chartHoverHour < 0 || chartHoverHour > 23) return;
  const hx = x(chartHoverHour);
  ctx.save();
  ctx.strokeStyle = "rgba(159,220,255,0.45)";
  ctx.lineWidth = 1;
  ctx.setLineDash([4, 4]);
  ctx.beginPath(); ctx.moveTo(hx, padT); ctx.lineTo(hx, padT + H); ctx.stroke();
  ctx.setLineDash([]);
  const dot = (px, py, color) => {
    ctx.beginPath(); ctx.arc(px, py, 4, 0, Math.PI * 2);
    ctx.fillStyle = color; ctx.fill();
    ctx.strokeStyle = "rgba(7,13,26,0.9)"; ctx.lineWidth = 1.5; ctx.stroke();
  };
  dot(hx, y(g.demand[chartHoverHour]), "#a78bfa");
  dot(hx, y(g.grid[chartHoverHour]), "#38bdf8");
  const bv = g.batt[chartHoverHour];
  if (bv !== 0) {
    // Same anchor the battery bars use: top of the bar for charge, the
    // mid-line for discharge.
    const by = bv > 0 ? padT + H / 2 - (bv / g.peak) * (H / 2) : padT + H / 2;
    dot(hx, by, "#34d399");
  }
  ctx.restore();
}

function chartPointFromEvent(clientX) {
  if (!chartGeometry) return null;
  const g = chartGeometry;
  const canvas = $("energy-chart");
  const rect = canvas.getBoundingClientRect();
  const step = g.W / 23;
  const h = Math.max(0, Math.min(23, Math.round((clientX - rect.left - g.padL) / step)));
  return h;
}

function chartTooltipContent(h) {
  const g = chartGeometry;
  return (
    `<b>${String(h).padStart(2, "0")}:00</b><br>` +
    `demand ${fmt(g.demand[h])} kWh<br>` +
    `grid&nbsp;&nbsp; ${fmt(g.grid[h])} kWh<br>` +
    `solar&nbsp; ${fmt(g.solar[h])} kWh<br>` +
    `batt&nbsp;&nbsp; ${g.batt[h] > 0 ? "+" : ""}${fmt(g.batt[h])} kWh`
  );
}

function positionTooltip(cursorX, cursorY) {
  const tooltip = $("chart-tooltip");
  tooltip.hidden = false;
  const pad = 10, gap = 14;
  const tw = tooltip.offsetWidth || 150;
  const th = tooltip.offsetHeight || 90;
  let left = cursorX + gap;
  let top = cursorY + gap;
  // Flip to the other side when the tooltip would leave the viewport,
  // then clamp so it is never clipped and never far from the cursor.
  if (left + tw + pad > window.innerWidth) left = cursorX - gap - tw;
  if (top + th + pad > window.innerHeight) top = cursorY - gap - th;
  left = Math.max(pad, Math.min(left, window.innerWidth - tw - pad));
  top = Math.max(pad, Math.min(top, window.innerHeight - th - pad));
  tooltip.style.left = `${left}px`;
  tooltip.style.top = `${top}px`;
}

function onChartHover(evt) {
  const h = chartPointFromEvent(evt.clientX);
  if (h === null) return;
  if (h !== chartHoverHour) {
    chartHoverHour = h;
    drawChart(DEMO_SCENARIO.hours, currentPlan || []);
  }
  const tooltip = $("chart-tooltip");
  tooltip.innerHTML = chartTooltipContent(h);
  positionTooltip(evt.clientX, evt.clientY);
}

function onChartLeave() {
  $("chart-tooltip").hidden = true;
  if (chartHoverHour !== null) {
    chartHoverHour = null;
    drawChart(DEMO_SCENARIO.hours, currentPlan || []);
  }
}

/* Touch support: same tooltip behavior with finger position. */
function attachTouchHandlers(canvas) {
  if (canvas._gridwiseTouchAttached) return;
  canvas._gridwiseTouchAttached = true;
  const fromTouch = (e) => {
    const t = e.touches && e.touches[0];
    if (!t) return null;
    return { clientX: t.clientX, clientY: t.clientY };
  };
  canvas.addEventListener("touchstart", (e) => {
    const p = fromTouch(e);
    if (p) onChartHover(p);
  }, { passive: true });
  canvas.addEventListener("touchmove", (e) => {
    const p = fromTouch(e);
    if (p) onChartHover(p);
  }, { passive: true });
  canvas.addEventListener("touchend", () => {
    setTimeout(onChartLeave, 1500);
  });
}

/* ---------------- Boot ---------------- */
window.addEventListener("resize", () => {
  if (!$("results").hidden) drawChart(DEMO_SCENARIO.hours, currentPlan || []);
});
let currentPlan = null;
const _origRender = renderResults;
renderResults = function (request, body) {
  currentPlan = body.hourly_plan;
  _origRender(request, body);
};

document.addEventListener("DOMContentLoaded", () => {
  // Hoist the tooltip to <body>: fixed-position elements inside .panel are
  // re-anchored by its backdrop-filter (the "tooltip far below the chart"
  // bug); as a direct body child its viewport coordinates are correct.
  document.body.appendChild($("chart-tooltip"));
  renderNotes();
  $("btn-load-demo").addEventListener("click", () => {
    $("scenario-id").value = DEMO_SCENARIO.scenario_id;
    notes = [
      "Reduce solar usage by 80% from 1 PM to 3 PM.",
      "Maintain at least 30 kWh battery reserve from 6 PM to 10 PM.",
    ];
    renderNotes();
  });
  $("btn-add-note").addEventListener("click", () => {
    if (notes.length < 3) { notes.push(""); renderNotes(); }
  });
  $("btn-optimize").addEventListener("click", optimize);
  checkHealth();
  setInterval(checkHealth, 30000);
});
