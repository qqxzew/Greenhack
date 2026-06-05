#!/usr/bin/env python3
# chat_app.py
"""
Argus Chat Interface — a token-optimized chat UI backed by the real Claude API.

Every message is routed through the Argus optimization pipeline:
  - Semantic cache + MinHash dedup    (repeat questions served instantly)
  - Extractive context compression    (history trimmed before each send)
  - Hierarchical model routing        (Auto: cheapest capable model chosen)

Per-message savings badges + a live corner widget show exactly what was saved.

Launch:
    uvicorn chat_app:app --port 7861 --reload

Cloud Run (reads $PORT):
    CMD ["sh", "-c", "uvicorn chat_app:app --host 0.0.0.0 --port ${PORT}"]
"""

import json
import os
import time
import uuid
from dataclasses import dataclass, field
from threading import Lock
from typing import AsyncIterator

import anthropic
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse

from core.compression import ContextCompressor, ConversationPrompt, estimate_tokens
from core.pipeline    import OptimizationPipeline
from core.tracking    import MODEL_COSTS, BASELINE_MODEL, cost_of

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MODELS = {
    "claude-haiku-4-5":  "Haiku",
    "claude-sonnet-4-5": "Sonnet",
    "claude-opus-4-7":   "Opus",
}

# Baseline for savings comparison: Sonnet with no optimisation
_BASELINE = BASELINE_MODEL

# Rough latency model (ms) matching core/tracking.py
_LATENCY = {
    "claude-haiku-4-5":  {"base": 180,  "in": 0.020, "out": 4.0},
    "claude-sonnet-4-5": {"base": 380,  "in": 0.045, "out": 11.0},
    "claude-opus-4-7":   {"base": 700,  "in": 0.070, "out": 26.0},
}


def _modeled_latency(model: str, tok_in: int, tok_out: int) -> float:
    p = _LATENCY.get(model, _LATENCY["claude-sonnet-4-5"])
    return p["base"] + tok_in * p["in"] + tok_out * p["out"]


# ---------------------------------------------------------------------------
# Per-session state
# ---------------------------------------------------------------------------

@dataclass
class SessionState:
    pipeline:    OptimizationPipeline = field(default_factory=OptimizationPipeline)
    compressor:  ContextCompressor    = field(default_factory=ContextCompressor)
    # cumulative totals
    tokens_saved:   int   = 0
    cost_saved:     float = 0.0
    latency_saved:  float = 0.0   # ms
    messages_total: int   = 0
    messages_opt:   int   = 0


_sessions: dict[str, SessionState] = {}
_sess_lock = Lock()


def _get_session(session_id: str) -> SessionState:
    with _sess_lock:
        if session_id not in _sessions:
            _sessions[session_id] = SessionState()
        return _sessions[session_id]


# ---------------------------------------------------------------------------
# FastAPI
# ---------------------------------------------------------------------------

app = FastAPI(title="Argus Chat")


@app.get("/", response_class=HTMLResponse)
def index():
    return HTMLResponse(CHAT_PAGE)


@app.delete("/api/sessions/{session_id}")
def reset_session(session_id: str):
    with _sess_lock:
        _sessions.pop(session_id, None)
    return {"ok": True}


@app.post("/api/chat")
async def chat(
    request: Request,
    x_api_key: str = Header(..., alias="X-API-Key"),
):
    """SSE stream: emits `token`, `done`, or `error` events as JSON lines."""
    if not x_api_key or not x_api_key.startswith("sk-ant-"):
        raise HTTPException(status_code=401, detail="Invalid API key format")

    body = await request.json()
    session_id: str       = body.get("session_id", "default")
    user_model: str | None = body.get("model")        # None → Auto
    history: list         = body.get("history", [])   # [{role, content}, ...]
    question: str         = body.get("message", "").strip()
    system_prompt: str    = body.get("system", "You are a helpful assistant.")

    if not question:
        raise HTTPException(status_code=400, detail="Empty message")

    return StreamingResponse(
        _stream_response(x_api_key, session_id, user_model, history, question, system_prompt),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# Core streaming logic
# ---------------------------------------------------------------------------

async def _stream_response(
    api_key:       str,
    session_id:    str,
    user_model:    str | None,
    history:       list,
    question:      str,
    system_prompt: str,
) -> AsyncIterator[str]:

    sess = _get_session(session_id)
    sess.messages_total += 1

    # Build structured ConversationPrompt for compressor
    history_tuples = [(m["role"], m["content"]) for m in history]
    conv = ConversationPrompt(
        system=system_prompt,
        history=history_tuples,
        question=question,
    )
    full_prompt = conv.render()

    # ── Baseline metrics (what Sonnet with full prompt would cost) ──────────
    baseline_tok_in = estimate_tokens(full_prompt)

    # ── Pipeline: pre-call ─────────────────────────────────────────────────
    task = {
        "id":         str(uuid.uuid4()),
        "prompt":     full_prompt,
        "type":       "chat",
        "user_model": user_model,  # None → router decides; str → bypass router
    }

    decision = sess.pipeline.pre_call(session_id, task)
    action   = decision["action"]

    # ── CACHE HIT ───────────────────────────────────────────────────────────
    if action == "cache_hit":
        cached = decision.get("cached_response", {})
        response_text = (
            cached.get("response") or
            cached.get("output_text") or
            str(cached)
        )
        # Estimate what we saved: full baseline cost, full baseline latency
        baseline_tok_out = estimate_tokens(response_text)
        b_cost    = cost_of(_BASELINE, baseline_tok_in, baseline_tok_out)
        b_latency = _modeled_latency(_BASELINE, baseline_tok_in, baseline_tok_out)
        tokens_saved  = baseline_tok_in + baseline_tok_out
        cost_saved    = b_cost
        latency_saved = b_latency

        _update_totals(sess, tokens_saved, cost_saved, latency_saved)
        source = decision.get("source", "cache")

        yield _sse({
            "type": "token",
            "text": response_text,
            "cache": True,
        })
        yield _sse({
            "type":          "done",
            "mechanism":     "CACHE",
            "source":        source,
            "model":         "cache",
            "tokens_saved":  tokens_saved,
            "cost_saved":    round(cost_saved, 6),
            "latency_saved": round(latency_saved, 1),
            "totals":        _totals(sess),
        })
        return

    # ── BLOCKED ─────────────────────────────────────────────────────────────
    if action == "blocked":
        yield _sse({"type": "error", "message": "Budget exhausted — request blocked."})
        return

    # ── LLM CALL ────────────────────────────────────────────────────────────
    chosen_model: str = decision["model"]

    # Compress context before sending
    cr = sess.compressor.compress(conv)
    compressed_prompt = cr.after_text

    # Build messages list (only the final question, history already in prompt)
    messages = [{"role": "user", "content": compressed_prompt}]

    # Yield compression info as a metadata event (before first token)
    yield _sse({
        "type":              "compression",
        "before_tokens":     cr.before_tokens,
        "after_tokens":      cr.after_tokens,
        "tokens_saved_comp": cr.saved_tokens,
        "ratio":             round(cr.ratio, 3),
    })

    # Stream from Anthropic
    client = anthropic.Anthropic(api_key=api_key)
    full_text   = ""
    tok_in      = 0
    tok_out     = 0
    t_start     = time.perf_counter()

    try:
        with client.messages.stream(
            model=chosen_model,
            max_tokens=1024,
            system=system_prompt,
            messages=[{"role": "user", "content": cr.after_text}],
        ) as stream:
            for text in stream.text_stream:
                full_text += text
                yield _sse({"type": "token", "text": text})

            final_msg = stream.get_final_message()
            tok_in    = final_msg.usage.input_tokens
            tok_out   = final_msg.usage.output_tokens

    except anthropic.AuthenticationError:
        yield _sse({"type": "error", "message": "Invalid API key."})
        return
    except anthropic.RateLimitError:
        yield _sse({"type": "error", "message": "Rate limit reached. Please wait and retry."})
        return
    except Exception as exc:
        yield _sse({"type": "error", "message": f"API error: {exc}"})
        return

    latency_ms = (time.perf_counter() - t_start) * 1000

    # ── Savings accounting ──────────────────────────────────────────────────
    # Baseline: Sonnet on full (uncompressed) prompt
    baseline_tok_out = tok_out  # same output regardless of model/compression
    b_cost    = cost_of(_BASELINE, baseline_tok_in, baseline_tok_out)
    b_latency = _modeled_latency(_BASELINE, baseline_tok_in, baseline_tok_out)

    actual_cost    = cost_of(chosen_model, tok_in, tok_out)
    actual_latency = latency_ms

    cost_saved_route    = cost_of(_BASELINE, baseline_tok_in, baseline_tok_out) \
                        - cost_of(chosen_model, baseline_tok_in, baseline_tok_out)
    cost_saved_compress = cost_of(chosen_model, cr.saved_tokens, 0)
    total_cost_saved    = b_cost - actual_cost
    tokens_saved_total  = baseline_tok_in - tok_in

    latency_saved = max(0.0, b_latency - actual_latency)

    # Determine mechanism label
    if chosen_model == _BASELINE and cr.saved_tokens < 10:
        mechanism = "NONE"
    elif chosen_model != _BASELINE and cost_of(chosen_model, 1, 0) < cost_of(_BASELINE, 1, 0):
        mechanism = "ROUTE↓"
    elif chosen_model != _BASELINE and cost_of(chosen_model, 1, 0) > cost_of(_BASELINE, 1, 0):
        mechanism = "ROUTE↑"
    elif cr.saved_tokens >= 10:
        mechanism = "COMPRESS"
    else:
        mechanism = "NONE"

    # Post-call pipeline update
    quality_estimate = 0.85  # heuristic for chat (avoids scoring API round-trip)
    sess.pipeline.post_call(
        session_id,
        task,
        decision,
        {
            "response":     full_text,
            "quality":      quality_estimate,
            "tokens_in":    tok_in,
            "tokens_out":   tok_out,
            "tokens_total": tok_in + tok_out,
            "cost":         actual_cost,
            "latency":      latency_ms / 1000,
        },
    )

    _update_totals(sess, max(0, tokens_saved_total), max(0.0, total_cost_saved), latency_saved)

    yield _sse({
        "type":              "done",
        "mechanism":         mechanism,
        "model":             chosen_model,
        "tok_in":            tok_in,
        "tok_out":           tok_out,
        "tokens_saved":      max(0, tokens_saved_total),
        "cost_actual":       round(actual_cost, 6),
        "cost_saved":        round(max(0.0, total_cost_saved), 6),
        "cost_saved_route":  round(max(0.0, cost_saved_route), 6),
        "cost_saved_comp":   round(max(0.0, cost_saved_compress), 6),
        "latency_ms":        round(latency_ms, 1),
        "latency_saved":     round(latency_saved, 1),
        "compression_ratio": round(cr.ratio, 3),
        "totals":            _totals(sess),
    })


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sse(data: dict) -> str:
    return f"data: {json.dumps(data)}\n\n"


def _update_totals(sess: SessionState, tokens: int, cost: float, latency: float):
    sess.tokens_saved  += tokens
    sess.cost_saved    += cost
    sess.latency_saved += latency
    if tokens > 0 or cost > 0:
        sess.messages_opt += 1


def _totals(sess: SessionState) -> dict:
    return {
        "tokens_saved":   sess.tokens_saved,
        "cost_saved":     round(sess.cost_saved, 4),
        "latency_saved":  round(sess.latency_saved / 1000, 2),  # → seconds
        "messages_opt":   sess.messages_opt,
        "messages_total": sess.messages_total,
    }


# ---------------------------------------------------------------------------
# HTML / CSS / JS
# ---------------------------------------------------------------------------

CHAT_PAGE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Argus Chat</title>
  <style>
    :root { color-scheme: dark; }
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

    body {
      background: #0C0C12;
      color: #F0F0FF;
      font-family: -apple-system, Segoe UI, Roboto, sans-serif;
      height: 100dvh;
      display: flex;
      flex-direction: column;
      overflow: hidden;
    }

    /* ── Top bar ── */
    .topbar {
      display: flex;
      align-items: center;
      gap: 16px;
      padding: 12px 20px;
      border-bottom: 1px solid #2A2A3E;
      background: #0E0E18;
      flex-shrink: 0;
    }
    .topbar-title { font-size: 15px; font-weight: 700; }
    .topbar-title span { color: #2DD4B4; }
    .topbar-spacer { flex: 1; }

    /* model pills */
    .model-group { display: flex; gap: 6px; }
    .model-btn {
      background: #14141E;
      border: 1px solid #2A2A3E;
      border-radius: 20px;
      color: #8888AA;
      font-size: 12px;
      padding: 4px 13px;
      cursor: pointer;
      transition: border-color .15s, color .15s, background .15s;
      white-space: nowrap;
    }
    .model-btn:hover { border-color: #444460; color: #C0C0E0; }
    .model-btn.active {
      background: #0E2920;
      border-color: #2DD4B4;
      color: #2DD4B4;
    }

    /* new-chat button */
    .new-chat-btn {
      background: transparent;
      border: 1px solid #2A2A3E;
      border-radius: 8px;
      color: #8888AA;
      font-size: 12px;
      padding: 5px 12px;
      cursor: pointer;
      transition: border-color .15s, color .15s;
    }
    .new-chat-btn:hover { border-color: #444460; color: #C0C0E0; }

    /* ── Thread ── */
    .thread {
      flex: 1;
      overflow-y: auto;
      padding: 24px 20px 8px;
      display: flex;
      flex-direction: column;
      gap: 18px;
      scroll-behavior: smooth;
    }

    .msg-row { display: flex; flex-direction: column; max-width: 720px; }
    .msg-row.user  { align-self: flex-end; align-items: flex-end; }
    .msg-row.asst  { align-self: flex-start; align-items: flex-start; }

    .bubble {
      padding: 11px 15px;
      border-radius: 14px;
      font-size: 14px;
      line-height: 1.6;
      max-width: 680px;
      word-break: break-word;
      white-space: pre-wrap;
    }
    .msg-row.user .bubble {
      background: #1A1A2E;
      border: 1px solid #2A2A4E;
      color: #E0E0F8;
    }
    .msg-row.asst .bubble {
      background: #14141E;
      border: 1px solid #2A2A3E;
      color: #F0F0FF;
    }

    .model-tag {
      font-size: 10px;
      color: #555570;
      margin-bottom: 4px;
      font-family: monospace;
    }

    /* savings badge */
    .badge {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      margin-top: 6px;
      padding: 3px 10px 3px 8px;
      border-radius: 20px;
      font-size: 11px;
      font-family: monospace;
      border: 1px solid;
      white-space: nowrap;
    }
    .badge.teal   { background: #0A1F18; border-color: #2DD4B455; color: #2DD4B4; }
    .badge.amber  { background: #1A1208; border-color: #F5A62355; color: #F5A623; }
    .badge.grey   { background: #141418; border-color: #2A2A3E;   color: #666688; }
    .badge-dot    { width: 6px; height: 6px; border-radius: 50%; background: currentColor; }

    /* streaming cursor */
    .cursor::after {
      content: '▋';
      animation: blink .7s step-end infinite;
      color: #2DD4B4;
      font-size: 13px;
    }
    @keyframes blink { 50% { opacity: 0; } }

    /* ── Input area ── */
    .input-area {
      padding: 12px 20px 16px;
      border-top: 1px solid #2A2A3E;
      background: #0E0E18;
      flex-shrink: 0;
    }
    .input-row { display: flex; gap: 10px; align-items: flex-end; }
    textarea {
      flex: 1;
      background: #14141E;
      border: 1px solid #2A2A3E;
      border-radius: 12px;
      color: #F0F0FF;
      font-family: inherit;
      font-size: 14px;
      padding: 10px 14px;
      resize: none;
      min-height: 44px;
      max-height: 180px;
      outline: none;
      transition: border-color .15s;
      overflow-y: auto;
    }
    textarea:focus { border-color: #2DD4B4; }
    textarea::placeholder { color: #444460; }
    .send-btn {
      background: #2DD4B4;
      border: none;
      border-radius: 10px;
      color: #0C0C12;
      cursor: pointer;
      font-size: 18px;
      font-weight: 700;
      width: 44px;
      height: 44px;
      flex-shrink: 0;
      display: flex;
      align-items: center;
      justify-content: center;
      transition: background .15s, opacity .15s;
    }
    .send-btn:hover:not(:disabled) { background: #25B89C; }
    .send-btn:disabled { opacity: .4; cursor: not-allowed; }

    /* ── Savings corner widget ── */
    .savings-widget {
      position: fixed;
      bottom: 80px;
      right: 20px;
      background: #0E1A14;
      border: 1px solid #2DD4B455;
      border-radius: 14px;
      padding: 12px 16px;
      min-width: 180px;
      z-index: 100;
      transition: opacity .3s;
    }
    .savings-widget.hidden { opacity: 0; pointer-events: none; }
    .sw-title {
      font-size: 10px;
      text-transform: uppercase;
      letter-spacing: .07em;
      color: #2DD4B4;
      margin-bottom: 8px;
      font-weight: 600;
    }
    .sw-row {
      display: flex;
      justify-content: space-between;
      align-items: baseline;
      gap: 12px;
      margin-bottom: 4px;
    }
    .sw-label { font-size: 11px; color: #8888AA; }
    .sw-val   { font-size: 13px; font-family: monospace; color: #F0F0FF; font-weight: 600; }
    .sw-val.green { color: #2DD4B4; }

    /* ── API key screen ── */
    #key-screen {
      position: fixed;
      inset: 0;
      background: #0C0C12;
      display: flex;
      align-items: center;
      justify-content: center;
      z-index: 200;
    }
    .key-card {
      background: #14141E;
      border: 1px solid #2A2A3E;
      border-radius: 16px;
      padding: 36px 40px;
      width: 100%;
      max-width: 420px;
    }
    .key-card h1 { font-size: 22px; margin-bottom: 6px; }
    .key-card h1 span { color: #2DD4B4; }
    .key-card p {
      color: #8888AA;
      font-size: 13px;
      line-height: 1.55;
      margin-bottom: 24px;
    }
    .key-label {
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: .05em;
      color: #8888AA;
      margin-bottom: 7px;
    }
    .key-input {
      width: 100%;
      background: #0C0C12;
      border: 1px solid #2A2A3E;
      border-radius: 8px;
      color: #F0F0FF;
      font-family: monospace;
      font-size: 14px;
      padding: 9px 12px;
      outline: none;
      transition: border-color .15s;
      margin-bottom: 16px;
    }
    .key-input:focus { border-color: #2DD4B4; }
    .key-btn {
      width: 100%;
      background: #2DD4B4;
      border: none;
      border-radius: 10px;
      color: #0C0C12;
      font-size: 15px;
      font-weight: 700;
      padding: 12px;
      cursor: pointer;
      transition: background .15s;
    }
    .key-btn:hover { background: #25B89C; }
    .key-err { color: #E05A3A; font-size: 12px; margin-top: 10px; }

    /* ── Responsive ── */
    @media (max-width: 600px) {
      .topbar { gap: 8px; padding: 10px 12px; }
      .thread { padding: 16px 12px 6px; }
      .input-area { padding: 10px 12px 14px; }
      .savings-widget { right: 10px; bottom: 72px; min-width: 150px; }
    }
  </style>
</head>
<body>

<!-- ── API key gate ─────────────────────────────────────────────────────── -->
<div id="key-screen">
  <div class="key-card">
    <h1>Argus <span>Chat</span></h1>
    <p>Enter your Anthropic API key to start. It is sent directly to the Claude API
       on each message and is never stored.</p>
    <div class="key-label">Anthropic API Key</div>
    <input class="key-input" id="key-input" type="password"
           placeholder="sk-ant-api03-…" autocomplete="off" spellcheck="false">
    <button class="key-btn" id="key-btn" onclick="connectKey()">Connect</button>
    <div class="key-err" id="key-err"></div>
  </div>
</div>

<!-- ── Main chat UI ─────────────────────────────────────────────────────── -->
<div class="topbar">
  <div class="topbar-title">Argus <span>Chat</span></div>
  <div class="topbar-spacer"></div>

  <!-- Model selector -->
  <div class="model-group" id="model-group">
    <button class="model-btn" data-model="claude-haiku-4-5"  onclick="selectModel(this)">Haiku</button>
    <button class="model-btn" data-model="claude-sonnet-4-5" onclick="selectModel(this)">Sonnet</button>
    <button class="model-btn" data-model="claude-opus-4-7"   onclick="selectModel(this)">Opus</button>
    <button class="model-btn active" data-model=""            onclick="selectModel(this)">Auto ★</button>
  </div>

  <button class="new-chat-btn" onclick="newChat()">+ New chat</button>
</div>

<div class="thread" id="thread">
  <!-- messages injected here -->
</div>

<div class="input-area">
  <div class="input-row">
    <textarea id="msg-input" rows="1" placeholder="Message Argus…"
              onkeydown="handleKey(event)" oninput="autoResize(this)"></textarea>
    <button class="send-btn" id="send-btn" onclick="sendMessage()" title="Send">&#9650;</button>
  </div>
</div>

<!-- ── Corner savings widget ──────────────────────────────────────────── -->
<div class="savings-widget hidden" id="savings-widget">
  <div class="sw-title">Session savings</div>
  <div class="sw-row">
    <span class="sw-label">Tokens saved</span>
    <span class="sw-val green" id="sw-tokens">0</span>
  </div>
  <div class="sw-row">
    <span class="sw-label">Cost saved</span>
    <span class="sw-val green" id="sw-cost">$0.0000</span>
  </div>
  <div class="sw-row">
    <span class="sw-label">Time saved</span>
    <span class="sw-val green" id="sw-latency">0.0s</span>
  </div>
  <div class="sw-row">
    <span class="sw-label">Optimized</span>
    <span class="sw-val" id="sw-msgs">0 / 0</span>
  </div>
</div>

<script>
  // ── State ────────────────────────────────────────────────────────────────
  let _apiKey     = '';
  let _sessionId  = _newSessionId();
  let _model      = '';           // '' = Auto
  let _history    = [];           // [{role, content}, ...]
  let _streaming  = false;

  function _newSessionId() { return Math.random().toString(36).slice(2); }

  // ── API key ──────────────────────────────────────────────────────────────
  document.getElementById('key-input').addEventListener('keydown', e => {
    if (e.key === 'Enter') connectKey();
  });

  function connectKey() {
    const val = document.getElementById('key-input').value.trim();
    if (!val.startsWith('sk-ant-')) {
      document.getElementById('key-err').textContent =
        'Key must start with sk-ant- (Anthropic format)';
      return;
    }
    _apiKey = val;
    document.getElementById('key-screen').style.display = 'none';
    document.getElementById('msg-input').focus();
  }

  // ── Model selection ──────────────────────────────────────────────────────
  function selectModel(btn) {
    document.querySelectorAll('.model-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    _model = btn.dataset.model;
  }

  // ── New chat ─────────────────────────────────────────────────────────────
  async function newChat() {
    if (_streaming) return;
    await fetch('/api/sessions/' + _sessionId, { method: 'DELETE' });
    _sessionId = _newSessionId();
    _history   = [];
    document.getElementById('thread').innerHTML = '';
    document.getElementById('savings-widget').classList.add('hidden');
    _resetWidget();
  }

  // ── Input helpers ────────────────────────────────────────────────────────
  function handleKey(e) {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); }
  }
  function autoResize(el) {
    el.style.height = 'auto';
    el.style.height = Math.min(el.scrollHeight, 180) + 'px';
  }

  // ── Send message ─────────────────────────────────────────────────────────
  async function sendMessage() {
    if (_streaming) return;
    const input = document.getElementById('msg-input');
    const text  = input.value.trim();
    if (!text) return;

    input.value = '';
    input.style.height = 'auto';

    // Append user bubble
    _appendBubble('user', text);
    _history.push({ role: 'user', content: text });

    // Prepare assistant bubble
    const { bubble, badgeSlot } = _appendBubble('asst', '');
    bubble.classList.add('cursor');

    _setStreaming(true);
    let modelTag = '';
    let compressionInfo = null;

    try {
      const resp = await fetch('/api/chat', {
        method:  'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-API-Key':    _apiKey,
        },
        body: JSON.stringify({
          session_id: _sessionId,
          model:      _model || null,
          history:    _history.slice(0, -1),  // exclude the message we just added
          message:    text,
          system:     'You are a helpful assistant.',
        }),
      });

      if (!resp.ok) {
        const err = await resp.json().catch(() => ({ detail: resp.statusText }));
        _appendError(bubble, err.detail || 'Request failed');
        return;
      }

      const reader = resp.body.getReader();
      const dec    = new TextDecoder();
      let   buf    = '';

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        const lines = buf.split('\\n\\n');
        buf = lines.pop();

        for (const line of lines) {
          if (!line.startsWith('data: ')) continue;
          let evt;
          try { evt = JSON.parse(line.slice(6)); } catch { continue; }

          if (evt.type === 'compression') {
            compressionInfo = evt;

          } else if (evt.type === 'token') {
            bubble.textContent += evt.text;
            _scrollThread();

          } else if (evt.type === 'done') {
            bubble.classList.remove('cursor');
            modelTag = evt.model || 'cache';
            _renderBadge(badgeSlot, evt, compressionInfo);
            _history.push({ role: 'assistant', content: bubble.textContent });
            _updateWidget(evt.totals);

          } else if (evt.type === 'error') {
            _appendError(bubble, evt.message);
            return;
          }
        }
      }
    } catch (e) {
      _appendError(bubble, 'Connection error: ' + e.message);
    } finally {
      bubble.classList.remove('cursor');
      _setStreaming(false);
    }
  }

  // ── DOM helpers ──────────────────────────────────────────────────────────
  function _appendBubble(role, text) {
    const thread = document.getElementById('thread');
    const row    = document.createElement('div');
    row.className = 'msg-row ' + role;

    if (role === 'asst') {
      const tag = document.createElement('div');
      tag.className = 'model-tag';
      tag.textContent = 'Argus';
      row.appendChild(tag);
    }

    const bubble = document.createElement('div');
    bubble.className = 'bubble';
    bubble.textContent = text;
    row.appendChild(bubble);

    const badgeSlot = document.createElement('div');
    row.appendChild(badgeSlot);

    thread.appendChild(row);
    _scrollThread();
    return { bubble, badgeSlot };
  }

  function _appendError(bubble, msg) {
    bubble.classList.remove('cursor');
    bubble.style.color = '#E05A3A';
    bubble.textContent = '⚠ ' + msg;
  }

  function _scrollThread() {
    const t = document.getElementById('thread');
    t.scrollTop = t.scrollHeight;
  }

  function _setStreaming(val) {
    _streaming = val;
    document.getElementById('send-btn').disabled  = val;
    document.getElementById('msg-input').disabled = val;
  }

  // ── Badge rendering ──────────────────────────────────────────────────────
  function _renderBadge(slot, evt, compInfo) {
    const mech      = evt.mechanism || 'NONE';
    const costSaved = evt.cost_saved || 0;
    const tokSaved  = evt.tokens_saved || 0;
    const latSaved  = evt.latency_saved || 0;
    const model     = evt.model || '';
    const modelName = {'claude-haiku-4-5':'Haiku','claude-sonnet-4-5':'Sonnet','claude-opus-4-7':'Opus','cache':'Cache'}[model] || model;

    if (mech === 'NONE' && costSaved < 0.000001) {
      const b = _badge('grey');
      b.innerHTML = '<span class="badge-dot"></span> No saving · ' + modelName;
      slot.appendChild(b);
      return;
    }

    const parts = [];
    if (mech === 'CACHE')    parts.push('CACHE HIT');
    if (mech === 'ROUTE↓')  parts.push('ROUTED → ' + modelName);
    if (mech === 'ROUTE↑')  parts.push('QUALITY PROTECT → ' + modelName);
    if (mech === 'COMPRESS' || (compInfo && compInfo.tokens_saved_comp > 0))
      parts.push('COMPRESSED ' + Math.round((1 - (compInfo ? compInfo.ratio : 1)) * 100) + '%');
    if (!parts.length) parts.push(modelName);

    const label = parts.join(' · ');
    const isUp  = mech === 'ROUTE↑';

    const b = _badge(isUp ? 'amber' : 'teal');
    let html = '<span class="badge-dot"></span> ' + label;
    if (costSaved > 0.000001)
      html += ' &nbsp;·&nbsp; <b>$' + costSaved.toFixed(4) + ' saved</b>';
    if (tokSaved > 0)
      html += ' · ' + tokSaved + ' tok';
    if (latSaved > 50)
      html += ' · ' + (latSaved / 1000).toFixed(2) + 's faster';
    b.innerHTML = html;
    slot.appendChild(b);
  }

  function _badge(cls) {
    const el = document.createElement('div');
    el.className = 'badge ' + cls;
    return el;
  }

  // ── Corner widget ────────────────────────────────────────────────────────
  function _updateWidget(totals) {
    if (!totals) return;
    document.getElementById('savings-widget').classList.remove('hidden');
    document.getElementById('sw-tokens').textContent  = totals.tokens_saved.toLocaleString();
    document.getElementById('sw-cost').textContent    = '$' + totals.cost_saved.toFixed(4);
    document.getElementById('sw-latency').textContent = totals.latency_saved.toFixed(2) + 's';
    document.getElementById('sw-msgs').textContent    = totals.messages_opt + ' / ' + totals.messages_total;
  }

  function _resetWidget() {
    document.getElementById('sw-tokens').textContent  = '0';
    document.getElementById('sw-cost').textContent    = '$0.0000';
    document.getElementById('sw-latency').textContent = '0.00s';
    document.getElementById('sw-msgs').textContent    = '0 / 0';
  }
</script>
</body>
</html>
"""
