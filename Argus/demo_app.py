#!/usr/bin/env python3
# demo_app.py
"""
Argus Demo Webapp — FastAPI app that wraps make_investor_report.py with a nice UI.

Launch:
    uvicorn demo_app:app --port 7860 --reload
"""

import os
import sys
import subprocess
import threading
import uuid
import glob
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

# ---------------------------------------------------------------------------
# Project root (where make_investor_report.py lives)
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).parent.resolve()
REPORTS_DIR  = PROJECT_ROOT / "reports"

# ---------------------------------------------------------------------------
# In-memory job store
# ---------------------------------------------------------------------------

@dataclass
class Job:
    job_id: str
    status: str = "running"   # running | complete | error
    lines: list = field(default_factory=list)
    report_path: Optional[str] = None


_jobs: dict[str, Job] = {}
_jobs_lock = threading.Lock()


def _find_latest_report() -> Optional[str]:
    """Return path of the most recently modified investor_report.html."""
    pattern = str(REPORTS_DIR / "investor_*" / "investor_report.html")
    candidates = glob.glob(pattern)
    if not candidates:
        return None
    return max(candidates, key=os.path.getmtime)


def _run_job(job: Job, cmd: list[str]) -> None:
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=str(PROJECT_ROOT),
            text=True,
            bufsize=1,
        )
        for line in proc.stdout:
            stripped = line.rstrip("\n")
            with _jobs_lock:
                job.lines.append(stripped)
        proc.wait()
        with _jobs_lock:
            if proc.returncode == 0:
                job.report_path = _find_latest_report()
                job.status = "complete"
            else:
                job.status = "error"
    except Exception as exc:
        with _jobs_lock:
            job.lines.append(f"[internal error] {exc}")
            job.status = "error"


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="Argus Demo")


@app.get("/", response_class=HTMLResponse)
def index():
    return HTML_PAGE


@app.get("/api/env")
def get_env():
    has_key = bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())
    return JSONResponse({"has_api_key": has_key})


@app.post("/api/run")
async def run_simulation(body: dict):
    # Build CLI args
    n           = int(body.get("n", 300))
    seed        = int(body.get("seed", 7))
    difficulty  = str(body.get("difficulty", "balanced"))
    strong_frac = body.get("strong_frac")          # None or float
    reasoning   = float(body.get("reasoning_depth", 1.0))
    cache_rate  = float(body.get("cache_rate", 0.12))
    loop_rate   = float(body.get("loop_rate", 0.04))
    compressible = float(body.get("compressible", 0.65))
    live        = bool(body.get("live", False))

    # Clamp to safe ranges
    n            = max(10, min(n, 2000))
    seed         = max(0, seed)
    reasoning    = max(0.5, min(reasoning, 3.0))
    cache_rate   = max(0.0, min(cache_rate, 0.40))
    loop_rate    = max(0.0, min(loop_rate, 0.20))
    compressible = max(0.10, min(compressible, 1.0))

    cmd = [
        sys.executable, "make_investor_report.py",
        "--n", str(n),
        "--seed", str(seed),
        "--difficulty", difficulty,
        "--reasoning-depth", str(round(reasoning, 2)),
        "--cache-rate", str(round(cache_rate, 3)),
        "--loop-rate", str(round(loop_rate, 3)),
        "--compressible", str(round(compressible, 3)),
    ]
    if strong_frac is not None:
        cmd += ["--strong-frac", str(round(float(strong_frac), 3))]
    if live:
        cmd.append("--live")

    job_id = str(uuid.uuid4())[:8]
    job = Job(job_id=job_id)
    with _jobs_lock:
        _jobs[job_id] = job

    t = threading.Thread(target=_run_job, args=(job, cmd), daemon=True)
    t.start()

    return JSONResponse({"job_id": job_id})


@app.get("/api/status/{job_id}")
def job_status(job_id: str):
    with _jobs_lock:
        job = _jobs.get(job_id)
    if job is None:
        return JSONResponse({"error": "unknown job"}, status_code=404)
    return JSONResponse({
        "status":     job.status,
        "lines":      list(job.lines),
        "report_url": "/api/report" if job.report_path else None,
    })


@app.get("/api/report", response_class=HTMLResponse)
def get_report():
    path = _find_latest_report()
    if not path:
        return HTMLResponse("<p style='color:#ccc;padding:2rem;font-family:sans-serif'>No report found yet.</p>",
                            status_code=404)
    return HTMLResponse(Path(path).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Inline HTML / CSS / JS  (matches the dark theme of the investor reports)
# ---------------------------------------------------------------------------

HTML_PAGE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Argus — Token Governance Demo</title>
  <style>
    :root { color-scheme: dark; }
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

    body {
      background: #0C0C12;
      color: #F0F0FF;
      font-family: -apple-system, Segoe UI, Roboto, sans-serif;
      min-height: 100vh;
    }

    .wrap { max-width: 900px; margin: auto; padding: 40px 24px 80px; }

    /* ── Header ── */
    .header { margin-bottom: 36px; }
    .header h1 { font-size: 28px; font-weight: 700; margin-bottom: 6px; }
    .header .sub {
      color: #8888AA;
      font-size: 14px;
      line-height: 1.55;
    }
    .t-teal  { color: #2DD4B4; }
    .t-coral { color: #E05A3A; }
    .t-amber { color: #F5A623; }

    /* ── Section headings ── */
    h2 {
      font-size: 14px;
      text-transform: uppercase;
      letter-spacing: .06em;
      color: #8888AA;
      margin-bottom: 14px;
    }

    /* ── Card ── */
    .card {
      background: #14141E;
      border: 1px solid #2A2A3E;
      border-radius: 14px;
      padding: 24px 28px;
      margin-bottom: 20px;
    }

    /* ── Config grid ── */
    .cfg-grid {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 20px 32px;
    }
    @media (max-width: 600px) { .cfg-grid { grid-template-columns: 1fr; } }

    .field { display: flex; flex-direction: column; gap: 7px; }
    .field label {
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: .05em;
      color: #8888AA;
    }
    .field .hint {
      font-size: 11px;
      color: #555566;
      margin-top: -4px;
    }

    /* number inputs */
    input[type=number] {
      background: #0C0C12;
      border: 1px solid #2A2A3E;
      border-radius: 8px;
      color: #F0F0FF;
      font-family: monospace;
      font-size: 15px;
      padding: 7px 12px;
      width: 100%;
      outline: none;
      transition: border-color .15s;
    }
    input[type=number]:focus { border-color: #2DD4B4; }

    /* range sliders */
    .slider-row { display: flex; align-items: center; gap: 10px; }
    input[type=range] {
      flex: 1;
      -webkit-appearance: none;
      appearance: none;
      height: 4px;
      border-radius: 2px;
      background: #2A2A3E;
      outline: none;
      cursor: pointer;
    }
    input[type=range]::-webkit-slider-thumb {
      -webkit-appearance: none;
      width: 16px; height: 16px;
      border-radius: 50%;
      background: #2DD4B4;
      cursor: pointer;
      border: 2px solid #0C0C12;
    }
    input[type=range]::-moz-range-thumb {
      width: 16px; height: 16px;
      border-radius: 50%;
      background: #2DD4B4;
      cursor: pointer;
      border: 2px solid #0C0C12;
    }
    .slider-val {
      font-family: monospace;
      font-size: 14px;
      color: #2DD4B4;
      min-width: 44px;
      text-align: right;
    }

    /* pill radio group (difficulty) */
    .pill-group { display: flex; gap: 8px; flex-wrap: wrap; }
    .pill-group input[type=radio] { display: none; }
    .pill-group label {
      display: inline-block;
      background: #0C0C12;
      border: 1px solid #2A2A3E;
      border-radius: 20px;
      padding: 5px 16px;
      font-size: 13px;
      color: #8888AA;
      cursor: pointer;
      text-transform: none;
      letter-spacing: 0;
      transition: border-color .15s, color .15s, background .15s;
    }
    .pill-group input[type=radio]:checked + label {
      background: #0E2920;
      border-color: #2DD4B4;
      color: #2DD4B4;
    }

    /* toggle (live mode) */
    .toggle-row { display: flex; align-items: center; gap: 12px; }
    .toggle {
      position: relative;
      display: inline-block;
      width: 44px; height: 24px;
    }
    .toggle input { opacity: 0; width: 0; height: 0; }
    .toggle-slider {
      position: absolute; inset: 0;
      background: #2A2A3E;
      border-radius: 24px;
      cursor: pointer;
      transition: background .2s;
    }
    .toggle-slider::before {
      content: '';
      position: absolute;
      width: 18px; height: 18px;
      left: 3px; top: 3px;
      background: #8888AA;
      border-radius: 50%;
      transition: transform .2s, background .2s;
    }
    .toggle input:checked ~ .toggle-slider { background: #0E2920; }
    .toggle input:checked ~ .toggle-slider::before {
      transform: translateX(20px);
      background: #2DD4B4;
    }
    .toggle-label { font-size: 13px; color: #8888AA; }
    .toggle-label.active { color: #F0F0FF; }
    .toggle.disabled { opacity: .35; pointer-events: none; }
    .no-key-note { font-size: 11px; color: #555566; margin-left: 4px; }

    /* optional strong-frac row */
    .strong-frac-row { display: flex; align-items: center; gap: 12px; margin-top: 2px; }
    .auto-label { font-size: 12px; color: #8888AA; cursor: pointer; white-space: nowrap; }
    .auto-label input[type=checkbox] { accent-color: #2DD4B4; width: 14px; height: 14px; }

    /* ── Run button ── */
    .run-btn {
      display: block;
      width: 100%;
      padding: 16px;
      background: #2DD4B4;
      color: #0C0C12;
      font-size: 16px;
      font-weight: 700;
      border: none;
      border-radius: 12px;
      cursor: pointer;
      letter-spacing: .03em;
      transition: background .15s, opacity .15s;
      margin-bottom: 20px;
    }
    .run-btn:hover:not(:disabled) { background: #25B89C; }
    .run-btn:disabled { opacity: .45; cursor: not-allowed; }

    /* ── Progress ── */
    #progress-section { display: none; }
    .spinner-row { display: flex; align-items: center; gap: 14px; margin-bottom: 14px; }
    .spinner {
      width: 22px; height: 22px;
      border: 3px solid #2A2A3E;
      border-top-color: #2DD4B4;
      border-radius: 50%;
      animation: spin .8s linear infinite;
    }
    @keyframes spin { to { transform: rotate(360deg); } }
    .spinner-text { font-size: 14px; color: #8888AA; }

    .terminal {
      background: #080810;
      border: 1px solid #2A2A3E;
      border-radius: 10px;
      padding: 14px 16px;
      font-family: 'SF Mono', 'Fira Code', 'Consolas', monospace;
      font-size: 12px;
      line-height: 1.6;
      color: #9FE8D4;
      max-height: 260px;
      overflow-y: auto;
      white-space: pre-wrap;
      word-break: break-all;
    }

    /* ── Report ── */
    #report-section { display: none; }
    .callout {
      background: #101820;
      border: 1px solid #2DD4B455;
      border-radius: 12px;
      padding: 14px 18px;
      font-size: 14px;
      color: #C8E8E0;
      margin-bottom: 16px;
    }
    .callout b { color: #2DD4B4; }
    .report-frame {
      width: 100%;
      height: 820px;
      border: 1px solid #2A2A3E;
      border-radius: 12px;
      background: #0C0C12;
    }
    .error-box {
      background: #1A0A0A;
      border: 1px solid #E05A3A55;
      border-radius: 10px;
      color: #E8A8A8;
      padding: 12px 16px;
      font-size: 13px;
      margin-top: 12px;
      display: none;
    }
  </style>
</head>
<body>
<div class="wrap">

  <!-- Header -->
  <div class="header">
    <h1>Argus — <span class="t-teal">Token Governance</span> Demo</h1>
    <p class="sub">
      Configure a synthetic agent workload, click <em>Run Simulation</em>, and see an investor-grade
      cost &amp; time breakdown generated in seconds — no API key required in mock mode.
    </p>
  </div>

  <!-- Config card -->
  <div class="card">
    <h2>Simulation Parameters</h2>
    <div class="cfg-grid">

      <!-- # Tasks -->
      <div class="field">
        <label for="n">Number of tasks</label>
        <input type="number" id="n" value="300" min="10" max="2000" step="10">
        <span class="hint">10 – 2 000 (300 is the headline demo)</span>
      </div>

      <!-- Seed -->
      <div class="field">
        <label for="seed">RNG seed</label>
        <input type="number" id="seed" value="7" min="0" max="99999" step="1">
        <span class="hint">Same seed → identical results every run</span>
      </div>

      <!-- Difficulty -->
      <div class="field" style="grid-column: 1 / -1;">
        <label>Difficulty mix</label>
        <div class="pill-group">
          <input type="radio" name="difficulty" id="d-easy"     value="easy">
          <label for="d-easy">Easy</label>
          <input type="radio" name="difficulty" id="d-balanced" value="balanced" checked>
          <label for="d-balanced">Balanced ★</label>
          <input type="radio" name="difficulty" id="d-hard"     value="hard">
          <label for="d-hard">Hard</label>
          <input type="radio" name="difficulty" id="d-frontier" value="frontier">
          <label for="d-frontier">Frontier</label>
        </div>
        <span class="hint">Controls the ratio of easy/medium/hard/expert tasks in the workload</span>
      </div>

      <!-- Expert fraction -->
      <div class="field">
        <label>Expert (Opus-needing) fraction</label>
        <div class="strong-frac-row">
          <label class="auto-label"><input type="checkbox" id="strong-auto" checked> Auto</label>
          <div class="slider-row" id="strong-frac-slider" style="opacity:.3;pointer-events:none;flex:1">
            <input type="range" id="strong-frac" min="0" max="0.50" step="0.01" value="0.10">
            <span class="slider-val" id="strong-frac-val">0.10</span>
          </div>
        </div>
        <span class="hint">Override the % of tasks that truly need the frontier model</span>
      </div>

      <!-- Reasoning depth -->
      <div class="field">
        <label>Reasoning depth</label>
        <div class="slider-row">
          <input type="range" id="reasoning-depth" min="0.5" max="3.0" step="0.1" value="1.0">
          <span class="slider-val" id="reasoning-depth-val">1.0×</span>
        </div>
        <span class="hint">Scales token count on hard + expert tasks</span>
      </div>

      <!-- Cache hit rate -->
      <div class="field">
        <label>Cache hit rate</label>
        <div class="slider-row">
          <input type="range" id="cache-rate" min="0" max="0.40" step="0.01" value="0.12">
          <span class="slider-val" id="cache-rate-val">12%</span>
        </div>
        <span class="hint">Fraction of tasks that are exact cache repeats</span>
      </div>

      <!-- Stuck loop rate -->
      <div class="field">
        <label>Stuck loop rate</label>
        <div class="slider-row">
          <input type="range" id="loop-rate" min="0" max="0.20" step="0.01" value="0.04">
          <span class="slider-val" id="loop-rate-val">4%</span>
        </div>
        <span class="hint">Fraction of tasks that are runaway loops (SPRT stops them)</span>
      </div>

      <!-- Compressible fraction -->
      <div class="field">
        <label>Compressible fraction</label>
        <div class="slider-row">
          <input type="range" id="compressible" min="0.10" max="1.00" step="0.05" value="0.65">
          <span class="slider-val" id="compressible-val">65%</span>
        </div>
        <span class="hint">Fraction of verbose tasks eligible for context compression</span>
      </div>

      <!-- Live mode toggle -->
      <div class="field" style="grid-column: 1 / -1;">
        <label>Live mode</label>
        <div class="toggle-row">
          <label class="toggle" id="live-toggle-wrap">
            <input type="checkbox" id="live-toggle">
            <span class="toggle-slider"></span>
          </label>
          <span class="toggle-label" id="live-toggle-label">Use real Claude API</span>
          <span class="no-key-note" id="no-key-note" style="display:none">
            (ANTHROPIC_API_KEY not set — live mode unavailable)
          </span>
        </div>
        <span class="hint">When OFF: deterministic mock — free, no API key needed. When ON: real Claude calls.</span>
      </div>

    </div><!-- /.cfg-grid -->
  </div><!-- /.card -->

  <!-- Run button -->
  <button class="run-btn" id="run-btn" onclick="runSimulation()">
    ▶ Run Simulation
  </button>

  <!-- Progress -->
  <div id="progress-section">
    <div class="card">
      <div class="spinner-row">
        <div class="spinner" id="spinner"></div>
        <span class="spinner-text" id="spinner-text">Running simulation…</span>
      </div>
      <div class="terminal" id="terminal"></div>
      <div class="error-box" id="error-box"></div>
    </div>
  </div>

  <!-- Report -->
  <div id="report-section">
    <div class="callout">
      <b>Report ready.</b> The full investor-grade analysis is shown below —
      cost &amp; time savings decomposed by routing, compression, caching and loop-stopping.
    </div>
    <iframe class="report-frame" id="report-frame" src="" title="Investor Report" frameborder="0"></iframe>
  </div>

</div><!-- /.wrap -->

<script>
  // ── Slider value display ──────────────────────────────────────────────────
  function pct(id, valId) {
    const el = document.getElementById(id);
    const out = document.getElementById(valId);
    el.addEventListener('input', () => {
      out.textContent = Math.round(el.value * 100) + '%';
    });
  }
  function mult(id, valId) {
    const el = document.getElementById(id);
    const out = document.getElementById(valId);
    el.addEventListener('input', () => {
      out.textContent = parseFloat(el.value).toFixed(1) + '\u00d7';
    });
  }
  function dec(id, valId) {
    const el = document.getElementById(id);
    const out = document.getElementById(valId);
    el.addEventListener('input', () => {
      out.textContent = parseFloat(el.value).toFixed(2);
    });
  }

  pct('cache-rate',       'cache-rate-val');
  pct('loop-rate',        'loop-rate-val');
  pct('compressible',     'compressible-val');
  mult('reasoning-depth', 'reasoning-depth-val');
  dec('strong-frac',      'strong-frac-val');

  // ── Expert-fraction "auto" checkbox ──────────────────────────────────────
  document.getElementById('strong-auto').addEventListener('change', function () {
    const sliderWrap = document.getElementById('strong-frac-slider');
    if (this.checked) {
      sliderWrap.style.opacity = '0.3';
      sliderWrap.style.pointerEvents = 'none';
    } else {
      sliderWrap.style.opacity = '1';
      sliderWrap.style.pointerEvents = 'auto';
    }
  });

  // ── Check API key on load ─────────────────────────────────────────────────
  window.addEventListener('DOMContentLoaded', async () => {
    try {
      const res = await fetch('/api/env');
      const data = await res.json();
      if (!data.has_api_key) {
        const wrap = document.getElementById('live-toggle-wrap');
        wrap.classList.add('disabled');
        document.getElementById('live-toggle').disabled = true;
        document.getElementById('no-key-note').style.display = 'inline';
      } else {
        document.getElementById('live-toggle-label').classList.add('active');
      }
    } catch (e) { /* ignore */ }
  });

  // ── Run simulation ────────────────────────────────────────────────────────
  let _pollInterval = null;
  let _lastLineCount = 0;

  async function runSimulation() {
    // Reset UI
    clearInterval(_pollInterval);
    _lastLineCount = 0;
    document.getElementById('terminal').textContent = '';
    document.getElementById('error-box').style.display = 'none';
    document.getElementById('error-box').textContent = '';
    document.getElementById('report-section').style.display = 'none';
    document.getElementById('report-frame').src = '';

    // Show progress
    document.getElementById('progress-section').style.display = 'block';
    document.getElementById('spinner').style.display = 'block';
    document.getElementById('spinner-text').textContent = 'Running simulation\u2026';
    document.getElementById('run-btn').disabled = true;

    // Collect params
    const autoFrac = document.getElementById('strong-auto').checked;
    const body = {
      n:               parseInt(document.getElementById('n').value, 10),
      seed:            parseInt(document.getElementById('seed').value, 10),
      difficulty:      document.querySelector('input[name="difficulty"]:checked').value,
      strong_frac:     autoFrac ? null : parseFloat(document.getElementById('strong-frac').value),
      reasoning_depth: parseFloat(document.getElementById('reasoning-depth').value),
      cache_rate:      parseFloat(document.getElementById('cache-rate').value),
      loop_rate:       parseFloat(document.getElementById('loop-rate').value),
      compressible:    parseFloat(document.getElementById('compressible').value),
      live:            document.getElementById('live-toggle').checked,
    };

    let jobId;
    try {
      const r = await fetch('/api/run', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      const data = await r.json();
      jobId = data.job_id;
    } catch (e) {
      showError('Failed to start simulation: ' + e.message);
      return;
    }

    _pollInterval = setInterval(() => pollStatus(jobId), 600);
  }

  async function pollStatus(jobId) {
    try {
      const r = await fetch('/api/status/' + jobId);
      const data = await r.json();

      // Append new lines to terminal
      const term = document.getElementById('terminal');
      const newLines = data.lines.slice(_lastLineCount);
      _lastLineCount = data.lines.length;
      if (newLines.length > 0) {
        term.textContent += newLines.join('\\n') + (newLines.length ? '\\n' : '');
        term.scrollTop = term.scrollHeight;
      }

      if (data.status === 'complete') {
        clearInterval(_pollInterval);
        document.getElementById('spinner').style.display = 'none';
        document.getElementById('spinner-text').textContent = 'Done.';
        document.getElementById('run-btn').disabled = false;

        // Show report
        document.getElementById('report-section').style.display = 'block';
        document.getElementById('report-frame').src = '/api/report?t=' + Date.now();
        document.getElementById('report-section').scrollIntoView({ behavior: 'smooth' });

      } else if (data.status === 'error') {
        clearInterval(_pollInterval);
        document.getElementById('spinner').style.display = 'none';
        document.getElementById('spinner-text').textContent = 'Error.';
        document.getElementById('run-btn').disabled = false;
        showError('Simulation failed. See terminal output above for details.');
      }
    } catch (e) {
      /* network hiccup — keep polling */
    }
  }

  function showError(msg) {
    const box = document.getElementById('error-box');
    box.textContent = msg;
    box.style.display = 'block';
  }
</script>
</body>
</html>
"""
