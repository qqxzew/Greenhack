# api/routes.py

import json
import os
import re
import time
from collections import Counter, defaultdict, deque

import anthropic as _anthropic
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from api.schemas import (
    TaskRequest, PreCallResponse, PostCallRequest, StateResponse
)
from core.pipeline import OptimizationPipeline
from core.tracking import cost_of, latency_of, BASELINE_MODEL
from agents.registry import AGENT_ROSTER, ROSTER_BY_ID


# ── Live-dashboard baseline (per-call, decoupled from the offline report) ─────
# The default "without Argus" model for the LIVE dashboard is the standard
# mid-tier (Sonnet). BUT when a task is genuinely routed UP to the frontier
# model (Opus), comparing it to a cheaper Sonnet baseline would make Argus look
# "more expensive". So the per-call baseline is max(Sonnet, actual-tier): an
# Opus call is compared to Opus (→ $0 saved, an honest "quality investment"),
# while haiku/sonnet calls are still measured against Sonnet.
# NOTE: this is the LIVE constant — the global tracking.BASELINE_MODEL is left
# untouched so the offline investor report keeps its own semantics.
LIVE_BASELINE_MODEL = "claude-sonnet-4-5"


def _model_tier(m: str | None) -> int:
    m = m or ""
    if "opus" in m:   return 3
    if "sonnet" in m: return 2
    if "haiku" in m:  return 1
    return 2


def _baseline_for(model: str | None) -> str:
    """Per-call baseline: the pricier of {Sonnet, the model actually used}."""
    return model if (model and _model_tier(model) > _model_tier(LIVE_BASELINE_MODEL)) \
                 else LIVE_BASELINE_MODEL


# ── Human-readable method + verdict for each pipeline decision ───────────────
def _describe_decision(decision: dict, model: str | None) -> tuple[str, str, str]:
    """Return (method_id, method_label, verdict) for one pipeline decision."""
    action = decision.get("action", "")
    if action == "cache_hit":
        src = decision.get("source", "")
        if src == "dedup":
            return ("dedup", "MinHash deduplication",
                    "Exact-duplicate prompt — served the previous answer without calling any LLM.")
        if src == "semantic":
            return ("semantic_cache", "Semantic cache",
                    "Cosine-similar prompt found in cache — returned the stored answer instead of calling the LLM.")
        return ("cache", "Cache hit", "Served from cache.")
    if action == "blocked":
        reason = decision.get("reason", "")
        if reason == "budget_exhausted":
            return ("budget_block", "Hierarchical budget guard",
                    "Agent's token budget is exhausted — the call was refused before reaching the LLM.")
        if reason.endswith("_by_human"):
            return ("human_control", "Human supervision",
                    f"Agent {reason.split('_')[0]} by an operator — call refused.")
        return ("blocked", "Blocked", reason or "Call refused.")
    if action == "call_llm":
        tier = _model_tier(model)
        if tier > _model_tier(LIVE_BASELINE_MODEL):
            # Routed UP to the frontier model — a deliberate quality investment.
            return ("route_up", "Frontier model — routed up for quality",
                    f"This task was judged to need the frontier model ({model}). "
                    "Argus routed up to protect answer quality — no cost saving, by design.")
        if tier < _model_tier(LIVE_BASELINE_MODEL):
            return ("route_down", "Hierarchical router — downgraded model",
                    f"Router judged this prompt simple enough for {model}, "
                    f"cheaper than the {LIVE_BASELINE_MODEL} baseline.")
        return ("baseline", "Standard model (no down-route)",
                "Router kept this on the standard mid-tier model — no cheaper option qualified.")
    return ("unknown", action or "Unknown", "")


def _estimate_tokens(text: str) -> int:
    """Rough ~4 chars/token estimate (matches the extension's heuristic)."""
    return max(1, round(len(text or "") / 4))


def _compute_savings(entry: dict) -> None:
    """Fill in baseline cost/latency + savings for the stream entry, in place."""
    action = entry.get("action")
    tin    = entry.get("tokens_in") or 0
    tout   = entry.get("tokens_out") or 0

    # For cache_hit we don't have real tokens_in/out (no LLM ran). Estimate from
    # the cached call's stored total + the new prompt's length.
    if action == "cache_hit" and (tin == 0 and tout == 0):
        saved_total = entry.get("tokens_saved") or 1200
        # Assume the new prompt drives the input side; output is whatever the
        # cached answer would have produced (= saved_total - input).
        est_in = _estimate_tokens(entry.get("prompt", ""))
        est_in = min(est_in, saved_total - 50)   # leave room for output
        est_out = max(50, saved_total - est_in)
        tin, tout = est_in, est_out

    if action == "blocked":
        # Nothing was spent and nothing would-have-been spent — Argus simply refused.
        entry["baseline_model"]      = LIVE_BASELINE_MODEL
        entry["baseline_cost"]       = 0.0
        entry["baseline_latency_ms"] = 0.0
        entry["actual_cost"]         = 0.0
        entry["actual_latency_ms"]   = 0.0
        entry["saved_cost"]          = 0.0
        entry["saved_latency_ms"]    = 0.0
        entry["saved_tokens"]        = 0   # a refusal isn't "savings" — don't credit it
        return

    # Per-call baseline: Sonnet, unless the call was routed UP to a pricier model
    # (Opus), in which case the baseline IS that model → saved 0 (a quality
    # investment, never "Argus is more expensive"). cache_hit has no model, so it
    # falls back to the Sonnet baseline (conservative credit for the skipped call).
    baseline_model   = _baseline_for(entry.get("model")) if action == "call_llm" else LIVE_BASELINE_MODEL
    baseline_cost    = round(cost_of(baseline_model, tin, tout), 6)
    baseline_latency = round(latency_of(baseline_model, tin, tout), 1)

    if action == "cache_hit":
        # Cache serve is essentially free + instant on the LLM axis (network/disk
        # lookup is negligible compared to a 600-2000 ms LLM round trip).
        actual_cost    = 0.0
        actual_latency = 8.0   # nominal "cache lookup" ms
    else:  # call_llm
        actual_cost    = round(entry.get("cost") or 0.0, 6)
        # Pipeline stores latency in seconds; if missing, model it.
        if entry.get("latency") is not None:
            actual_latency = round(float(entry["latency"]) * 1000.0, 1)
        else:
            actual_latency = round(latency_of(entry.get("model") or "claude-haiku-4-5", tin, tout), 1)

    entry["baseline_model"]      = baseline_model
    entry["baseline_cost"]       = baseline_cost
    entry["baseline_latency_ms"] = baseline_latency
    entry["actual_cost"]         = actual_cost
    entry["actual_latency_ms"]   = actual_latency
    entry["saved_cost"]          = round(max(0.0, baseline_cost - actual_cost), 6)
    entry["saved_latency_ms"]    = round(max(0.0, baseline_latency - actual_latency), 1)
    # Tokens are only genuinely "saved" when a call is SKIPPED (cache/dedup):
    # routing-down spends the same tokens on a cheaper model (money saved, not
    # tokens). This is the single definition of saved-tokens used everywhere
    # (org counter, eco-note, report), replacing the old avoided_calls×avg
    # heuristic so all views agree.
    entry["saved_tokens"] = (tin + tout) if action == "cache_hit" else 0
    # Fill estimated tokens so the UI has something to display for cache_hit.
    if action == "cache_hit":
        entry.setdefault("estimated_tokens_in",  tin)
        entry.setdefault("estimated_tokens_out", tout)


_pipeline: OptimizationPipeline | None = None

# Human-in-the-loop supervision lives in the control plane (here), not in the
# core pipeline: agent_id -> "paused" | "killed". A paused/killed agent's calls
# are short-circuited to "blocked" before the pipeline ever runs.
_controls: dict[str, str] = {}

# Live telemetry pushed by the VS Code extension via POST /v1/extension/heartbeat.
_extension_heartbeat: dict = {}
_extension_heartbeat_ts: float = 0.0
_HEARTBEAT_STALE_SEC = 15

# ── Live stream of governed calls ────────────────────────────────────────────
# A rolling ring buffer of every request the pipeline saw, with the full prompt
# text and the decision (call_llm / cache_hit / blocked). post_call patches the
# matching record by task_id with real tokens/cost when the LLM stage finishes.
# This is what the dashboard's "Live Stream" view tails.
_STREAM_MAX = 500
_stream: deque = deque(maxlen=_STREAM_MAX)
_stream_by_task: dict[str, dict] = {}
_stream_seq = 0  # monotonic id so the client can poll "since"

# ── Single source of truth for savings ───────────────────────────────────────
# CUMULATIVE aggregates (NOT windowed by the 500-event stream buffer). Every
# finalized event is folded in exactly once. /v1/report and the /v1/agents org
# block both read from here, so "saved" means the same number everywhere — no
# more avoided_calls×avg_tokens in one place and Σ(baseline−actual) in another.
def _new_savings_totals() -> dict:
    return {
        "baseline_cost": 0.0, "actual_cost": 0.0, "saved_cost": 0.0,
        "baseline_latency_ms": 0.0, "actual_latency_ms": 0.0, "saved_latency_ms": 0.0,
        "tokens_in": 0, "tokens_out": 0, "saved_tokens": 0,
    }


def _new_agent_savings() -> dict:
    return {
        "agent_id": "", "agent_name": "",
        "calls": 0, "cache_hits": 0, "blocked": 0,
        "baseline_cost": 0.0, "actual_cost": 0.0, "saved_cost": 0.0,
        "baseline_latency_ms": 0.0, "actual_latency_ms": 0.0, "saved_latency_ms": 0.0,
        "saved_tokens": 0,
    }


_agg = {
    "events": 0,
    "by_action": Counter(),
    "by_method": defaultdict(lambda: {"count": 0, "label": "",
                                      "saved_cost": 0.0, "saved_latency_ms": 0.0}),
    "by_agent":  defaultdict(_new_agent_savings),
    "totals":    _new_savings_totals(),
    "ts_min": None, "ts_max": None,
}


def _accumulate(entry: dict) -> None:
    """Fold one FINALIZED stream entry into the cumulative aggregates exactly
    once. cache_hit/blocked are final at pre_call; call_llm at post_call."""
    action = entry.get("action", "")
    _agg["events"] += 1
    _agg["by_action"][action] += 1

    t = _agg["totals"]
    t["baseline_cost"]       += float(entry.get("baseline_cost")       or 0.0)
    t["actual_cost"]         += float(entry.get("actual_cost")         or 0.0)
    t["saved_cost"]          += float(entry.get("saved_cost")          or 0.0)
    t["baseline_latency_ms"] += float(entry.get("baseline_latency_ms") or 0.0)
    t["actual_latency_ms"]   += float(entry.get("actual_latency_ms")   or 0.0)
    t["saved_latency_ms"]    += float(entry.get("saved_latency_ms")    or 0.0)
    t["tokens_in"]   += int(entry.get("tokens_in")  or entry.get("estimated_tokens_in")  or 0)
    t["tokens_out"]  += int(entry.get("tokens_out") or entry.get("estimated_tokens_out") or 0)
    t["saved_tokens"]+= int(entry.get("saved_tokens") or 0)

    m = _agg["by_method"][entry.get("method", "unknown")]
    m["label"]             = entry.get("method_label", entry.get("method", "unknown"))
    m["count"]            += 1
    m["saved_cost"]       += float(entry.get("saved_cost")       or 0.0)
    m["saved_latency_ms"] += float(entry.get("saved_latency_ms") or 0.0)

    a = _agg["by_agent"][entry.get("agent_id", "?")]
    a["agent_id"]   = entry.get("agent_id", "?")
    a["agent_name"] = entry.get("agent_name", a["agent_id"])
    a["calls"]      += 1 if action == "call_llm" else 0
    a["cache_hits"] += 1 if action == "cache_hit" else 0
    a["blocked"]    += 1 if action == "blocked"   else 0
    a["baseline_cost"]       += float(entry.get("baseline_cost")       or 0.0)
    a["actual_cost"]         += float(entry.get("actual_cost")         or 0.0)
    a["saved_cost"]          += float(entry.get("saved_cost")          or 0.0)
    a["baseline_latency_ms"] += float(entry.get("baseline_latency_ms") or 0.0)
    a["actual_latency_ms"]   += float(entry.get("actual_latency_ms")   or 0.0)
    a["saved_latency_ms"]    += float(entry.get("saved_latency_ms")    or 0.0)
    a["saved_tokens"]        += int(entry.get("saved_tokens") or 0)

    ts = float(entry.get("ts") or 0.0)
    if ts:
        _agg["ts_min"] = ts if _agg["ts_min"] is None else min(_agg["ts_min"], ts)
        _agg["ts_max"] = ts if _agg["ts_max"] is None else max(_agg["ts_max"], ts)


def _record_pre_call(agent_id: str, task: TaskRequest, decision: dict) -> None:
    """Capture one pre_call event (with full prompt + decision) into the stream."""
    global _stream_seq
    _stream_seq += 1
    method_id, method_label, verdict = _describe_decision(decision, decision.get("model"))
    entry = {
        "id":            _stream_seq,
        "ts":            time.time(),
        "agent_id":      agent_id,
        "agent_name":    ROSTER_BY_ID.get(agent_id, {}).get("name", agent_id),
        "task_id":       decision.get("task_id") or task.id or "",
        "task_type":     task.type,
        "urgency":       task.urgency,
        "prompt":        task.prompt,
        "action":        decision.get("action", "unknown"),
        "model":         decision.get("model"),
        "source":        decision.get("source"),          # "dedup" | "semantic" for cache_hit
        "tokens_saved":  decision.get("tokens_saved"),    # for cache_hit
        "reason":        decision.get("reason"),          # for blocked
        # human-readable explanation:
        "method":        method_id,
        "method_label":  method_label,
        "verdict":       verdict,
        # filled in by post_call:
        "tokens_total":  None,
        "tokens_in":     None,
        "tokens_out":    None,
        "cost":          None,
        "quality":       None,
        "latency":       None,
    }
    # For cache_hit / blocked we already have everything needed to compute the
    # money / latency comparison. For call_llm we'll re-compute in post_call once
    # actual tokens are known.
    _compute_savings(entry)
    # cache_hit / blocked are FINAL at pre_call — fold them into the cumulative
    # aggregates now. call_llm is deferred to post_call (tokens not known yet).
    if entry["action"] in ("cache_hit", "blocked"):
        _accumulate(entry)
    _stream.append(entry)
    if entry["task_id"]:
        # Trim the index so it doesn't grow unbounded as old deque entries fall off.
        if len(_stream_by_task) > _STREAM_MAX * 2:
            keep = {e["task_id"] for e in _stream if e["task_id"]}
            for k in list(_stream_by_task):
                if k not in keep:
                    _stream_by_task.pop(k, None)
        _stream_by_task[entry["task_id"]] = entry


def _record_post_call(req: PostCallRequest) -> None:
    """Patch the matching stream entry with real LLM-result numbers + savings."""
    entry = _stream_by_task.get(req.task.id or "")
    if entry is None:
        return
    r = req.llm_result
    # If the caller overrode `decision["model"]` between pre_call and post_call
    # (the demo traffic generator does this for the "wasteful" agent), the real
    # model used is in the decision dict, NOT what the router originally chose.
    # Without this sync the dashboard would show "actual: haiku" with a
    # sonnet-priced cost, making it look like haiku is somehow more expensive.
    final_model = req.decision.get("model")
    if final_model:
        entry["model"] = final_model
    entry["tokens_total"] = r.tokens_total or (r.tokens_in + r.tokens_out)
    entry["tokens_in"]    = r.tokens_in
    entry["tokens_out"]   = r.tokens_out
    entry["cost"]         = round(r.cost, 5)
    entry["quality"]      = r.quality
    entry["latency"]      = r.latency
    # Refresh the explanation in case the model swap changed it (e.g. router
    # picked haiku → caller forced sonnet → method becomes "baseline" not "route_down").
    method_id, method_label, verdict = _describe_decision(
        {"action": entry["action"], "source": entry.get("source"), "reason": entry.get("reason")},
        entry.get("model"),
    )
    entry["method"]       = method_id
    entry["method_label"] = method_label
    entry["verdict"]      = verdict
    # Now that we know real tokens + cost, recompute the baseline-vs-actual story.
    _compute_savings(entry)
    # A call_llm event becomes final here (tokens/cost now known) — fold it in
    # exactly once. (cache_hit/blocked were already accumulated at pre_call.)
    if entry["action"] == "call_llm" and not entry.get("_accumulated"):
        entry["_accumulated"] = True
        _accumulate(entry)


class AgentControl(BaseModel):
    action: str  # "pause" | "resume" | "kill"


def get_pipeline() -> OptimizationPipeline:
    global _pipeline
    if _pipeline is None:
        _pipeline = OptimizationPipeline()
    return _pipeline


router = APIRouter()


@router.get("/health")
def health():
    return {"status": "ok"}


@router.post("/extension/heartbeat")
def extension_heartbeat(body: dict):
    """
    Receive live telemetry from the VS Code extension.
    The extension POSTs here every 2 s with its current usage state so the
    frontend can display real data for the 'Extension' agent.
    """
    global _extension_heartbeat, _extension_heartbeat_ts
    _extension_heartbeat = body
    _extension_heartbeat_ts = time.time()
    return {"ok": True}


@router.post("/pre_call", response_model=PreCallResponse)
def pre_call(agent_id: str, task: TaskRequest):
    # Human supervision overrides everything: a paused/killed agent does no work.
    ctl = _controls.get(agent_id)
    if ctl in ("paused", "killed"):
        decision = {"action": "blocked", "reason": f"{ctl}_by_human",
                    "task_id": task.id or ""}
        _record_pre_call(agent_id, task, decision)
        return decision
    pipeline = get_pipeline()
    decision = pipeline.pre_call(agent_id, task.model_dump())
    _record_pre_call(agent_id, task, decision)
    return decision


@router.post("/post_call")
def post_call(req: PostCallRequest):
    pipeline = get_pipeline()
    event = pipeline.post_call(
        req.agent_id,
        req.task.model_dump(),
        req.decision,
        req.llm_result.model_dump(),
    )
    _record_post_call(req)
    return event


def _savings_snapshot() -> dict:
    """Serialize the cumulative aggregates -- the single source of truth shared by
    /v1/report and the /v1/agents org block. Cumulative since server start (NOT
    windowed), so every view reports identical savings figures."""
    t = _agg["totals"]
    saved_pct_cost = (t["saved_cost"] / t["baseline_cost"] * 100.0) if t["baseline_cost"] > 0 else 0.0
    saved_pct_lat  = (t["saved_latency_ms"] / t["baseline_latency_ms"] * 100.0) if t["baseline_latency_ms"] > 0 else 0.0

    by_agent_list = sorted(
        (dict(a) for a in _agg["by_agent"].values()),
        key=lambda x: -x["saved_cost"],
    )
    for a in by_agent_list:
        for k in ("baseline_cost", "actual_cost", "saved_cost"):
            a[k] = round(a[k], 6)
        for k in ("baseline_latency_ms", "actual_latency_ms", "saved_latency_ms"):
            a[k] = round(a[k], 1)

    by_method_sorted = sorted(
        ({"id": k, "label": v["label"], "count": v["count"],
          "saved_cost": round(v["saved_cost"], 6),
          "saved_latency_ms": round(v["saved_latency_ms"], 1)}
         for k, v in _agg["by_method"].items()),
        key=lambda x: -x["saved_cost"],
    )

    window = 0.0
    if _agg["ts_min"] is not None and _agg["ts_max"] is not None:
        window = round(_agg["ts_max"] - _agg["ts_min"], 1)

    return {
        "events":    _agg["events"],
        "by_action": dict(_agg["by_action"]),
        "by_method": by_method_sorted,
        "by_agent":  by_agent_list,
        "totals": {
            "baseline_cost":       round(t["baseline_cost"],  6),
            "actual_cost":         round(t["actual_cost"],    6),
            "saved_cost":          round(t["saved_cost"],     6),
            "saved_pct_cost":      round(saved_pct_cost,      1),
            "baseline_latency_ms": round(t["baseline_latency_ms"], 1),
            "actual_latency_ms":   round(t["actual_latency_ms"],   1),
            "saved_latency_ms":    round(t["saved_latency_ms"],    1),
            "saved_pct_latency":   round(saved_pct_lat,       1),
            "tokens_in":           t["tokens_in"],
            "tokens_out":          t["tokens_out"],
            "saved_tokens":        t["saved_tokens"],
        },
        "window_seconds": window,
    }


@router.get("/report")
def report():
    """
    Aggregated impact report -- the investor-grade "what did Argus save us this
    session" view. Reads the cumulative aggregates (same source as the
    /v1/agents org block), so totals, per-agent and per-method all reconcile.
    """
    return _savings_snapshot()


@router.get("/stream")
def stream(limit: int = 100, since: int = 0):
    """
    Newest-first ring buffer of every governed call: the actual prompt text,
    the pipeline's decision (call_llm / cache_hit / blocked), and the LLM-stage
    outcome (tokens, cost, latency) once post_call lands.

    Powers the dashboard's Live Stream view. Pass `since=<last seen id>` to
    fetch only events newer than the client has already rendered.
    """
    limit = max(1, min(limit, _STREAM_MAX))
    # Newest first; filter by monotonic id.
    items = [e for e in reversed(_stream) if e["id"] > since][:limit]
    return {
        "events":     items,
        "latest_id":  _stream[-1]["id"] if _stream else 0,
        "buffer_size": len(_stream),
        "capacity":   _STREAM_MAX,
    }


@router.get("/state", response_model=StateResponse)
def state():
    pipeline = get_pipeline()
    return pipeline.get_full_state()


@router.get("/metrics")
def metrics():
    pipeline = get_pipeline()
    return pipeline.logger.aggregate()


def _derive_status(calls: int, anomaly: bool, stuck: bool, budget_action: str,
                   utilization: float) -> str:
    """Roll the governance signals into a single frontend-facing status."""
    if anomaly or stuck or budget_action == "block":
        return "flagged"
    if budget_action == "degrade" or utilization >= 0.80:
        return "degraded"
    if calls == 0:
        return "idle"
    return "active"


@router.get("/agents")
def agents():
    """
    The roster of agents Argus governs, each merged with its live metrics
    (calls, tokens, cost, quality, budget, anomaly/stuck flags).

    This is what the Argus frontend renders as a fleet of agent "rooms".
    Works on a cold start too — agents simply report zeroed metrics until
    traffic flows through the pipeline.
    """
    pipeline     = get_pipeline()
    events       = pipeline.logger.recent(1000)
    budget_state = pipeline.budget.state()
    budgets      = budget_state.get("agents", {})

    agg = defaultdict(lambda: {
        "calls": 0, "tokens": 0, "cost": 0.0,
        "quality": [], "models": Counter(), "task_types": Counter(),
        "anomaly": False, "stuck": False,
    })
    for e in events:
        aid = e.get("agent_id")
        if not aid:
            continue
        a = agg[aid]
        a["calls"]  += 1
        a["tokens"] += int(e.get("tokens_total", 0) or 0)
        a["cost"]   += float(e.get("cost", 0.0) or 0.0)
        if "quality" in e:
            a["quality"].append(e["quality"])
        if e.get("model"):
            a["models"][e["model"]] += 1
        a["task_types"][e.get("task_type", "unknown")] += 1
        if e.get("is_anomaly"):
            a["anomaly"] = True
        if e.get("sprt") == "STOP_STUCK":
            a["stuck"] = True

    out = []
    for r in AGENT_ROSTER:
        aid   = r["id"]
        a     = agg.get(aid)
        b     = budgets.get(aid, {})
        calls = a["calls"] if a else 0
        avg_q = round(sum(a["quality"]) / len(a["quality"]), 3) if (a and a["quality"]) else None
        model = a["models"].most_common(1)[0][0] if (a and a["models"]) else None
        anomaly = bool(a and a["anomaly"])
        stuck   = bool(a and a["stuck"])
        util    = float(b.get("utilization", 0.0))
        action  = b.get("action", "ok")
        ctl     = _controls.get(aid)
        status  = ctl if ctl in ("paused", "killed") else _derive_status(calls, anomaly, stuck, action, util)
        out.append({
            "id":            aid,
            "name":          r["name"],
            "role":          r["role"],
            "kind":          r["kind"],
            "character":     r["character"],
            "persona":       r["persona"],
            "model":         model,
            "calls":         calls,
            "tokens":        a["tokens"] if a else 0,
            "cost":          round(a["cost"], 5) if a else 0.0,
            "avg_quality":   avg_q,
            "task_types":    dict(a["task_types"]) if a else {},
            "budget":        b.get("budget", 0),
            "used":          b.get("used", 0),
            "utilization":   util,
            "budget_action": action,
            "anomaly":       anomaly,
            "stuck":         stuck,
            "control":       ctl,
            "status":        status,
        })

    # ── Real Extension agent (built from live heartbeat, not event log) ──
    age = time.time() - _extension_heartbeat_ts
    ext_online = bool(_extension_heartbeat) and age < _HEARTBEAT_STALE_SEC
    hb = _extension_heartbeat if ext_online else {}

    # Defensive: the extension is third-party JSON; any field may be null/missing
    # or the wrong type. Coerce with explicit fallbacks so a malformed heartbeat
    # never breaks /v1/agents (which the whole dashboard depends on).
    def _num(key: str, default: float) -> float:
        v = hb.get(key)
        try:
            return float(v) if v is not None else default
        except (TypeError, ValueError):
            return default

    ext_calls   = int(_num("calls", 0))
    ext_spend_c = _num("spend_cents", 0.0)
    ext_saved_c = _num("saved_cents", 0.0)
    ext_tokens  = int(_num("tokens_estimated", 0))
    ext_status  = hb.get("status", "idle") if ext_online else "idle"
    ext_model   = hb.get("last_model")
    ext_budget  = int(_num("budget_cents", 10000))

    out.insert(0, {
        "id":             "agent-extension",
        "name":           "Extension",
        "role":           "VS Code Integration",
        "kind":           "real",
        "character":      "extension",
        "persona":        "Live VS Code Extension — routes prompts through the cheapest available model and reports real usage metrics back to Argus.",
        "model":          ext_model,
        "calls":          ext_calls,
        "tokens":         ext_tokens,
        "cost":           round(ext_spend_c / 100.0, 5),
        "avg_quality":    None,
        "task_types":     {"prompt": ext_calls} if ext_calls else {},
        "budget":         ext_budget,
        "used":           ext_spend_c,
        "utilization":    min(ext_spend_c / max(float(ext_budget), 1.0), 1.0),
        "budget_action":  "ok",
        "anomaly":        False,
        "stuck":          False,
        "control":        None,
        "status":         ext_status,
        "real":           True,
        "active_task":    hb.get("active_task", ""),
        "providers_count": int(hb.get("providers_count", 0)),
        "saved_cents":    ext_saved_c,
        "last_heartbeat": _extension_heartbeat_ts if ext_online else None,
    })

    # ── Efficiency / energy impact ──
    # Single source of truth: the cumulative savings aggregates (same numbers the
    # Impact Report shows). tokens_saved / cost_saved here now reconcile exactly
    # with /v1/report — no more avoided_calls×avg_tokens heuristic that disagreed
    # with the per-event baseline-vs-actual sum.
    cache_stats   = pipeline.cache.stats()
    snap          = _savings_snapshot()
    avoided_calls = int(snap["by_action"].get("cache_hit", 0))
    tokens_used   = sum(a["tokens"] for a in agg.values())

    return {
        "agents": out,
        "org": {
            "budget":        budget_state.get("org_budget"),
            "used":          budget_state.get("org_used"),
            "remaining":     budget_state.get("org_remaining"),
            "tokens_used":   tokens_used,
            "tokens_saved":  snap["totals"]["saved_tokens"],
            "cost_saved":    snap["totals"]["saved_cost"],
            "saved_pct_cost": snap["totals"]["saved_pct_cost"],
            "avoided_calls": avoided_calls,
            "cache_hit_rate": cache_stats.get("hit_rate", 0.0),
        },
    }


@router.post("/agents/{agent_id}/control")
def control_agent(agent_id: str, body: AgentControl):
    """Human-in-the-loop supervision: pause / resume / kill an agent."""
    if agent_id not in ROSTER_BY_ID:
        raise HTTPException(status_code=404, detail="unknown agent")
    action = (body.action or "").lower()
    if action == "pause":
        _controls[agent_id] = "paused"
    elif action == "kill":
        _controls[agent_id] = "killed"
    elif action in ("resume", "reactivate"):
        _controls.pop(agent_id, None)
    else:
        raise HTTPException(status_code=400, detail="action must be pause|resume|kill")
    return {"agent_id": agent_id, "control": _controls.get(agent_id)}


@router.post("/generate-agent")
async def generate_agent(body: dict):
    """Call Claude to generate an optimised agent config from a name + task description."""
    name = (body.get("name") or "").strip()
    task = (body.get("task") or "").strip()
    savings_pct = int(body.get("savings_pct") or 68)
    if not name or not task:
        raise HTTPException(status_code=400, detail="name and task are required")

    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY not configured")

    client = _anthropic.Anthropic(api_key=api_key)
    msg = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        system=(
            "You configure AI agents for Argus, a token-governance platform that cuts compute "
            "waste through semantic caching and request deduplication. "
            "savings_pct: realistic integer 35-90 — the % of LLM calls Argus can cache or dedup "
            "for this specific task type. "
            "High (70-90): repetitive monitoring, data fetching, classification, FAQ answering, research queries. "
            "Medium (50-69): mixed analytical pipelines, scheduled reports, structured extraction. "
            "Low (35-49): unique creative writing, one-off open-ended reasoning, highly personalised responses. "
            "Reply with valid JSON only — no markdown, no extra text."
        ),
        messages=[{
            "role": "user",
            "content": (
                f'Agent name: "{name}"\nTask: "{task}"\n'
                'Return JSON: {"role": "<concise job title>", '
                '"model": "claude-haiku-4-5" or "claude-sonnet-4-6", '
                '"task": "<one sentence refined task>", '
                '"savings_pct": <integer 35-90>}'
            )
        }]
    )
    # Anthropic returns a list of content blocks; only text blocks have .text.
    # Guard against empty content / non-text blocks / malformed JSON.
    blocks = getattr(msg, "content", None) or []
    text = next((b.text for b in blocks if getattr(b, "text", None)), "")
    if not text:
        raise HTTPException(status_code=502, detail="Claude returned no text content")
    m = re.search(r'\{[\s\S]*\}', text)
    if not m:
        raise HTTPException(status_code=502, detail="Claude returned unexpected format")
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=502, detail=f"Claude returned invalid JSON: {e}")


@router.get("/audit")
def audit(limit: int = 100):
    """Newest-first audit trail of every governed LLM call — for the control room."""
    pipeline = get_pipeline()
    events = pipeline.logger.recent(max(1, min(limit, 1000)))
    rows = []
    for e in reversed(events):
        aid = e.get("agent_id", "")
        rows.append({
            "ts":         e.get("ts"),
            "agent_id":   aid,
            "agent_name": ROSTER_BY_ID.get(aid, {}).get("name", aid),
            "task_type":  e.get("task_type", "unknown"),
            "model":      e.get("model"),
            "tokens":     int(e.get("tokens_total", 0) or 0),
            "cost":       round(float(e.get("cost", 0.0) or 0.0), 5),
            "quality":    e.get("quality"),
            "anomaly":    bool(e.get("is_anomaly")),
            "stuck":      e.get("sprt") == "STOP_STUCK",
        })
    return {"events": rows, "count": len(rows)}
