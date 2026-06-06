# seed_demo.py
"""
Populate a running Argus server with synthetic traffic so the frontend has
real metrics to show — without needing an Anthropic API key.

Drives /v1/pre_call + /v1/post_call directly (post_call only *records* a result,
it never calls an LLM), so every number is produced by the real pipeline:
router, budget, CUSUM, SPRT and the event log.

Usage:
    # 1. start the server
    uvicorn main:app --port 8000
    # 2. in another shell
    python seed_demo.py
"""

import json
import urllib.request

BASE = "http://localhost:8000/v1"


def _post(path: str, body: dict, params: str = "") -> dict:
    url = f"{BASE}{path}{params}"
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode())


def run_call(agent_id, task, *, force_model=None, tokens_in=900, tokens_out=300,
             quality=0.86, cost=None):
    decision = _post("/pre_call", task, params=f"?agent_id={agent_id}")
    if decision.get("action") != "call_llm":
        return decision  # cache_hit / blocked — nothing to record
    if force_model:
        decision["model"] = force_model
    total = tokens_in + tokens_out
    model = decision.get("model", "claude-haiku-4-5")
    if cost is None:
        rate = 0.018 if "sonnet" in model else 0.0015
        cost = round(total / 1000 * rate, 5)
    _post("/post_call", {
        "agent_id": agent_id,
        "task": task,
        "decision": decision,
        "llm_result": {
            "response": "{}", "quality": quality,
            "tokens_in": tokens_in, "tokens_out": tokens_out,
            "tokens_total": total, "cost": cost, "latency": 0.6,
        },
    })
    return decision


def task(t, prompt, urgency=0.5, tid=None):
    d = {"type": t, "prompt": prompt, "urgency": urgency}
    if tid:
        d["id"] = tid
    return d


def main():
    # Diverse prompts per agent so each is a distinct LLM call (the semantic
    # cache only merges genuinely similar prompts — which is exactly what we
    # want the spammer to demonstrate).
    DEV = [
        "Audit this auth middleware for token-replay vulnerabilities.",
        "Why is our checkout endpoint p99 latency 4x the median? Suggest fixes.",
        "Design a sharding strategy for a 2TB orders table.",
        "Review this regex used to validate uploaded filenames for ReDoS risk.",
        "Should we migrate the worker queue from Redis to Kafka? Trade-offs.",
        "Find the race condition in this async cache-warmer snippet.",
        "Propose an index plan for time-range queries over the events table.",
    ]
    FIN = [
        "Model the IRR of hiring 3 engineers at $180k for an $800k ARR feature.",
        "Compare usage-based vs subscription pricing at 500 customers.",
        "Explain the 2pp gross-margin compression from cloud infra costs.",
        "Project 18-month runway if burn rises 12% and revenue grows 8% MoM.",
        "Summarise the budget variance: eng +240k, marketing -120k, G&A +45k.",
    ]
    HR = [
        "What is the remote-work policy for international contractors?",
        "Summarise the 16-week parental leave policy eligibility rules.",
        "Translate the timesheet reminder into German.",
        "How does an employee request an ergonomic accommodation?",
        "What is the expense-reimbursement approval chain over $2k?",
        "Draft a concise onboarding checklist for a new sales hire.",
    ]
    for p in DEV:
        run_call("agent-dev", task("code_review", p), tokens_in=850, tokens_out=240, quality=0.9)
    for p in FIN:
        run_call("agent-finance", task("analysis", p),
                 force_model="claude-sonnet-4-5", tokens_in=1180, tokens_out=380, quality=0.92)
    for p in HR:
        run_call("agent-hr", task("qa", p), tokens_in=720, tokens_out=240, quality=0.85)

    # spammer — fires the SAME question repeatedly; dedup + semantic cache
    # absorb almost all of it, so it logs only a handful of real calls.
    for n in range(12):
        run_call("agent-spammer", task("qa", "What is the remote work policy?", tid=f"sp{n}"),
                 tokens_in=500, tokens_out=180, quality=0.8)

    # wasteful — distinct expensive jobs on the priciest model: blows past its
    # 10k-token budget and trips the CUSUM anomaly detector -> flagged.
    WASTE = [
        "Write an exhaustive 6000-word competitive teardown of every rival.",
        "Generate a full 80-page onboarding manual from scratch, verbatim.",
        "Produce a line-by-line 5000-word rewrite of the entire codebase README.",
        "Draft 50 long-form blog posts about our product, one after another.",
        "Summarise every support ticket from the last year, in full detail.",
        "Re-derive the whole financial model with maximal explanation per cell.",
    ]
    for p in WASTE:
        run_call("agent-wasteful", task("generation", p),
                 force_model="claude-sonnet-4-5", tokens_in=2600, tokens_out=1400,
                 quality=0.7, cost=0.42)

    agents = json.loads(urllib.request.urlopen(f"{BASE}/agents", timeout=10).read().decode())["agents"]
    print(f"{'AGENT':<16}{'STATUS':<11}{'CALLS':>6}{'TOKENS':>9}{'COST':>9}  QUALITY")
    for a in agents:
        q = f"{a['avg_quality']:.2f}" if a["avg_quality"] is not None else "  -"
        print(f"{a['name']:<16}{a['status']:<11}{a['calls']:>6}{a['tokens']:>9}{a['cost']:>9.3f}  {q}")


if __name__ == "__main__":
    main()
