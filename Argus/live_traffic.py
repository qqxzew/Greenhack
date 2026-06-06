# live_traffic.py
"""
Traffic generator for the Argus dashboard. TWO modes:

  SYNTHETIC (default) — fabricates token counts, NO Claude calls, NO spend.
      Drives /v1/pre_call + /v1/post_call so the dashboard shows the pipeline
      (routing/cache/dedup/CUSUM/SPRT) working on a made-up workload.
          .venv/bin/python live_traffic.py

  LIVE (--live) — makes REAL Anthropic API calls with YOUR key and reports the
      REAL usage through Argus. This SPENDS REAL MONEY. Bounded by --max-calls
      and slowed by --interval so it can't run away. Reads ANTHROPIC_API_KEY
      from the environment or from Argus/.env.
          .venv/bin/python live_traffic.py --live
          .venv/bin/python live_traffic.py --live --max-calls 20 --interval 5

Stop with Ctrl-C.
"""

import argparse
import json
import os
import random
import time
import urllib.request

# Use the canonical cost/latency tables from the pipeline so the per-event
# "actual cost" exactly matches what _compute_savings() compares against —
# otherwise the dashboard shows nonsense like haiku > sonnet.
from core.tracking import cost_of, latency_of

BASE = "http://localhost:8000/v1"

# ── Real-LLM (live) configuration ────────────────────────────────────────────
# The backend router returns short model ids ("claude-haiku-4-5" /
# "claude-sonnet-4-5"). Map them to the real Anthropic API model ids you want to
# bill against. Override per-env if these aliases aren't valid for your account:
#   export ARGUS_HAIKU_MODEL=claude-haiku-4-5-YYYYMMDD
#   export ARGUS_SONNET_MODEL=claude-sonnet-4-5-YYYYMMDD
LIVE_MODEL_MAP = {
    "claude-haiku-4-5":  os.getenv("ARGUS_HAIKU_MODEL",  "claude-haiku-4-5"),
    "claude-sonnet-4-5": os.getenv("ARGUS_SONNET_MODEL", "claude-sonnet-4-5"),
    "claude-opus-4-8":   os.getenv("ARGUS_OPUS_MODEL",   "claude-opus-4-8"),
}

_client = None


def _load_api_key() -> str | None:
    """ANTHROPIC_API_KEY from env, else from Argus/.env next to this file."""
    key = os.getenv("ANTHROPIC_API_KEY")
    if key:
        return key.strip()
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(env_path):
        for line in open(env_path, encoding="utf-8"):
            line = line.strip()
            if line.startswith("ANTHROPIC_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def _get_client(key: str):
    global _client
    if _client is None:
        import anthropic  # lazy: only needed in --live mode
        _client = anthropic.Anthropic(api_key=key)
    return _client

# ── Prompts spanning real complexity ─────────────────────────────────────────
# Argus' router (core/features.py) reads complexity from keywords + length:
#   EASY  words  → "summarize/translate/list/what is/define/format/extract"  → haiku
#   HARD  words  → "analyze/design/debug/security/optimize/evaluate/derive"   → sonnet
# So each agent draws from a SIMPLE and a COMPLEX sub-pool: the 40-call run
# spans the whole range and the router visibly routes some down (haiku) and
# some up (sonnet). Templated nouns keep embeddings distinct so the semantic
# cache only merges genuinely-similar prompts.
DEV_OBJECTS = ["the checkout queue", "the auth middleware", "the GraphQL gateway",
               "the embeddings index", "the websocket fan-out", "the OCR worker",
               "the billing reconciler", "the analytics ingester", "the SLA tracker",
               "the password-reset flow", "the audit-log writer", "the webhook router"]
DEV_SIMPLE = ["List the public functions in", "Format the JSON config for",
              "Summarize what {o} does in one line", "Extract the TODO comments from",
              "Convert the config of {o} to YAML"]
DEV_COMPLEX = ["Debug the race condition in", "Analyze {o} for security vulnerabilities",
               "Optimize the hot path of", "Design a sharding strategy for",
               "Evaluate the failure modes of"]

FIN_SIMPLE = ["List the line items in {o}", "What is the gross margin of {o}",
              "Summarize the Q2 numbers for {o}", "Define burn multiple for {o}"]
FIN_COMPLEX = ["Analyze the IRR of {o}", "Evaluate the 18-month runway for {o}",
               "Compare usage vs subscription pricing for {o}",
               "Derive the unit economics of {o}"]
FIN_OBJECTS = ["the EU SMB segment", "enterprise renewals", "the self-serve cohort",
               "the new pricing tier", "the AWS commit", "the channel partners"]

HR_SIMPLE = ["Translate this leave notice to German for {o}",
             "Summarize the PTO policy for {o}", "What is the vesting rule for {o}",
             "List the onboarding steps for {o}", "Define 'sabbatical' for {o}"]
HR_COMPLEX = ["Evaluate the compliance risk of the remote-work policy for {o}",
              "Analyze the cost impact of the parental-leave policy for {o}"]
HR_OBJECTS = ["a new sales hire", "an EU contractor", "a returning parent",
              "a relocating engineer", "an intern cohort"]

# Genuinely frontier-hard tasks — the kind a real platform routes UP to Opus.
# Hard keywords (prove/derive/design/architect) push complexity high.
RESEARCH_TASK = [
    "Prove the correctness of the consensus protocol in",
    "Derive the closed-form latency bound for",
    "Design a provably-deadlock-free scheduler for",
    "Architect a multi-region failover strategy for",
    "Formally analyze the security model of",
    "Reason step-by-step about the race conditions in",
]
RESEARCH_OBJECT = [
    "the distributed transaction log", "the leaderless replication layer",
    "the cross-shard 2-phase commit", "the real-time bidding engine",
    "the gradient-sharding training loop", "the zero-downtime migration plan",
]

WASTE_VERB = ["Generate", "Draft", "Produce", "Compose", "Author"]
WASTE_THING = ["an exhaustive 6000-word competitive teardown",
               "a full 80-page onboarding manual verbatim",
               "a 5000-word line-by-line README rewrite",
               "50 long-form blog posts back-to-back",
               "a 200-slide annual deck with speaker notes"]


def _mk(simple, complex_, objects, simple_frac=0.5):
    """Return a prompt drawing from simple/complex sub-pools at the given mix."""
    pool = simple if random.random() < simple_frac else complex_
    o = random.choice(objects)
    tmpl = random.choice(pool)
    return tmpl.replace("{o}", o) if "{o}" in tmpl else f"{tmpl} {o}."


AGENTS = {
    "agent-dev": {
        # Half simple (→ haiku), half complex (→ sonnet): router splits visibly.
        "type": "code_review", "model": None,
        "make": lambda: _mk(DEV_SIMPLE, DEV_COMPLEX, DEV_OBJECTS, 0.5),
        "in": (700, 1100), "out": (180, 320), "q": (0.85, 0.95),
    },
    "agent-finance": {
        # Analysis skews complex (→ sonnet), but routine lookups go cheap.
        "type": "analysis", "model": None,
        "make": lambda: _mk(FIN_SIMPLE, FIN_COMPLEX, FIN_OBJECTS, 0.35),
        "in": (1000, 1400), "out": (300, 500), "q": (0.88, 0.95),
    },
    "agent-hr": {
        # Mostly simple Q&A (→ haiku): the clearest down-routing savings.
        "type": "qa", "model": None,
        "make": lambda: _mk(HR_SIMPLE, HR_COMPLEX, HR_OBJECTS, 0.8),
        "in": (600, 900), "out": (180, 320), "q": (0.80, 0.90),
    },
    "agent-spammer": {
        "type": "qa", "model": None,
        # Fixed on purpose — dedup + cache should absorb most of these.
        "make": lambda: "What is the remote work policy?",
        "in": (450, 600), "out": (160, 220), "q": (0.78, 0.85),
    },
    "agent-research": {
        # Genuinely hard frontier work — routed UP to Opus on purpose. Under the
        # per-call baseline this shows $0 cost-saved (it IS the frontier) and is
        # labelled "routed up — quality investment", NOT "Argus more expensive".
        "type": "analysis", "model": "claude-opus-4-8",
        "make": lambda: f"{random.choice(RESEARCH_TASK)} {random.choice(RESEARCH_OBJECT)}.",
        "in": (1400, 2200), "out": (500, 900), "q": (0.92, 0.97),
    },
    "agent-wasteful": {
        # The abuser archetype — also on the frontier model (matches its persona
        # "always calls the most expensive model"), but on bloated generation
        # work that doesn't need it → high spend, trips CUSUM / budget.
        "type": "generation", "model": "claude-opus-4-8",
        "make": lambda: f"{random.choice(WASTE_VERB)} {random.choice(WASTE_THING)} about {random.choice(DEV_OBJECTS)}.",
        "in": (2200, 3000), "out": (1200, 1800), "q": (0.65, 0.78),
    },
}

# Weight so the dashboard isn't dominated by one agent. Opus agents (research +
# wasteful) kept low-weight — Opus is ~5x sonnet, so a few calls is plenty.
WEIGHTS = {
    "agent-dev": 4,
    "agent-finance": 3,
    "agent-hr": 4,
    "agent-spammer": 3,   # spammer fires often; dedup catches repeats
    "agent-research": 1,  # frontier (Opus) — genuine hard tasks
    "agent-wasteful": 1,  # frontier (Opus) — abuser/control
}


def _post(path: str, body: dict, params: str = "") -> dict:
    req = urllib.request.Request(
        f"{BASE}{path}{params}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode())


def fire(agent_id: str, n: int, *, live: bool = False, key: str | None = None,
         max_tokens: int = 200) -> tuple[str, bool, float]:
    """Run one task. Returns (log_line, was_real_llm_call, real_cost)."""
    cfg = AGENTS[agent_id]
    prompt = cfg["make"]()
    task = {
        "type": cfg["type"],
        "prompt": prompt,
        "urgency": round(random.uniform(0.3, 0.9), 2),
        "id": f"{agent_id}-{n}",
    }
    decision = _post("/pre_call", task, params=f"?agent_id={agent_id}")
    action = decision.get("action", "?")
    if action != "call_llm":
        # cache_hit / blocked: backend already recorded it; no LLM, no spend.
        return action, False, 0.0
    if cfg["model"]:
        decision["model"] = cfg["model"]
    model = decision.get("model", "claude-haiku-4-5")

    if live:
        # ── REAL Anthropic call — this spends money ──
        api_model = LIVE_MODEL_MAP.get(model, model)
        client = _get_client(key)
        t0 = time.time()
        try:
            resp = client.messages.create(
                model=api_model,
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as e:
            # Surface the real API error (e.g. bad model id) so it's fixable,
            # and DON'T record a post_call (nothing happened).
            return f"API_ERR {type(e).__name__}: {str(e)[:140]}", False, 0.0
        tin = int(resp.usage.input_tokens)
        tout = int(resp.usage.output_tokens)
        latency = round(time.time() - t0, 3)            # measured wall-clock
        quality = 0.9                                   # no judge; nominal
        tag = " [LIVE]"
    else:
        tin = random.randint(*cfg["in"])
        tout = random.randint(*cfg["out"])
        latency = round(latency_of(model, tin, tout) / 1000.0, 3)
        quality = round(random.uniform(*cfg["q"]), 2)
        tag = ""

    total = tin + tout
    # Price real tokens at the SAME table the dashboard baseline uses, so the
    # baseline-vs-actual comparison stays internally consistent.
    cost = round(cost_of(model, tin, tout), 6)
    _post("/post_call", {
        "agent_id": agent_id,
        "task": task,
        "decision": decision,
        "llm_result": {
            "response": "{}",
            "quality": quality,
            "tokens_in": tin, "tokens_out": tout, "tokens_total": total,
            "cost": cost, "latency": latency,
        },
    })
    return f"call · {model.split('-')[1]} · {total}tok · ${cost:.5f}{tag}", live, cost


def main() -> None:
    ap = argparse.ArgumentParser(description="Argus traffic generator")
    ap.add_argument("--live", action="store_true",
                    help="make REAL Anthropic calls with your key (spends money)")
    ap.add_argument("--max-calls", type=int, default=30,
                    help="[live] stop after this many REAL LLM calls (cache hits don't count)")
    ap.add_argument("--interval", type=float, default=6.0,
                    help="[live] seconds between attempts (default 6)")
    ap.add_argument("--max-tokens", type=int, default=200,
                    help="[live] max output tokens per call (caps cost)")
    args = ap.parse_args()

    key = None
    if args.live:
        key = _load_api_key()
        if not key:
            print("ERROR: --live needs an Anthropic key.\n"
                  "  Put it in Argus/.env as:  ANTHROPIC_API_KEY=sk-ant-...\n"
                  "  (or export ANTHROPIC_API_KEY=... in your shell)")
            return
        print(f"LIVE mode — REAL Anthropic calls, REAL spend.\n"
              f"  cap: {args.max_calls} calls · every ~{args.interval}s · "
              f"max {args.max_tokens} out-tokens/call\n"
              f"  models: {LIVE_MODEL_MAP}\n"
              f"  Ctrl-C to stop early.\n")
    else:
        print("SYNTHETIC mode — no Claude calls, no spend. (use --live for real)")

    agent_pool = [a for a, w in WEIGHTS.items() for _ in range(w)]
    n = 0
    real_calls = 0
    real_spend = 0.0
    while True:
        n += 1
        agent = random.choice(agent_pool)
        try:
            outcome, was_real, cost = fire(
                agent, n, live=args.live, key=key, max_tokens=args.max_tokens)
        except urllib.error.HTTPError as e:
            try:
                body = e.read().decode("utf-8", "replace")[:200]
            except Exception:
                body = ""
            outcome, was_real, cost = f"HTTP {e.code}: {body}", False, 0.0
        except urllib.error.URLError as e:
            outcome, was_real, cost = f"NET {e.reason}", False, 0.0
        except Exception as e:
            outcome, was_real, cost = f"ERR {type(e).__name__}: {e}", False, 0.0

        if was_real:
            real_calls += 1
            real_spend += cost
        suffix = f"   [{real_calls}/{args.max_calls} real · ${real_spend:.4f}]" if args.live else ""
        print(f"[{n:>5}] {agent:<16} {outcome}{suffix}")

        if args.live and real_calls >= args.max_calls:
            print(f"\nReached --max-calls={args.max_calls}. "
                  f"Total real spend ≈ ${real_spend:.4f}. Stopping.")
            break

        time.sleep(args.interval if args.live else random.uniform(0.25, 0.9))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nstopped.")
