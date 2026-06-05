# api/routes.py

import json
import os
import re
from collections import Counter, defaultdict

import anthropic as _anthropic
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from api.schemas import (
    TaskRequest, PreCallResponse, PostCallRequest, StateResponse
)
from core.pipeline import OptimizationPipeline
from agents.registry import AGENT_ROSTER, ROSTER_BY_ID


_pipeline: OptimizationPipeline | None = None

# Human-in-the-loop supervision lives in the control plane (here), not in the
# core pipeline: agent_id -> "paused" | "killed". A paused/killed agent's calls
# are short-circuited to "blocked" before the pipeline ever runs.
_controls: dict[str, str] = {}


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


@router.post("/pre_call", response_model=PreCallResponse)
def pre_call(agent_id: str, task: TaskRequest):
    # Human supervision overrides everything: a paused/killed agent does no work.
    ctl = _controls.get(agent_id)
    if ctl in ("paused", "killed"):
        return {"action": "blocked", "reason": f"{ctl}_by_human",
                "task_id": task.id or ""}
    pipeline = get_pipeline()
    decision = pipeline.pre_call(agent_id, task.model_dump())
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
    return event


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

    # ── Efficiency / energy impact (honest, from real counters) ──
    cache_stats   = pipeline.cache.stats()
    dedup_stats   = pipeline.dedup.stats()
    tok_samples   = [int(e["tokens_total"]) for e in events if e.get("tokens_total")]
    avg_tokens    = (sum(tok_samples) / len(tok_samples)) if tok_samples else 1200.0
    avoided_calls = int(cache_stats.get("hits", 0)) + int(dedup_stats.get("duplicates", 0))
    tokens_used   = sum(a["tokens"] for a in agg.values())
    tokens_saved  = int(avoided_calls * avg_tokens)

    return {
        "agents": out,
        "org": {
            "budget":        budget_state.get("org_budget"),
            "used":          budget_state.get("org_used"),
            "remaining":     budget_state.get("org_remaining"),
            "tokens_used":   tokens_used,
            "tokens_saved":  tokens_saved,
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
    text = msg.content[0].text
    m = re.search(r'\{[\s\S]*\}', text)
    if not m:
        raise HTTPException(status_code=502, detail="Claude returned unexpected format")
    return json.loads(m.group(0))


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
