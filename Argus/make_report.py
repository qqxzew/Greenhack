# make_report.py
"""
Generate the Post-Run Optimization Report (report_spec.md).

Default is a fully deterministic MOCK run — no API key, no spend — so the
report (HTML + Markdown + scatter PNG + per-call before/after diffs) can be
produced and inspected for free.

Usage:
    python3.11 make_report.py            # mock (free, reproducible)
    python3.11 make_report.py --live     # real Claude API calls (needs key)
"""

import argparse
import time

from core.pipeline               import OptimizationPipeline
from core.compression            import ContextCompressor, estimate_tokens
from simulation.conversation_tasks import build_conversation_tasks, mock_responder
from eval.report                 import generate_report


def make_live_responder():
    """Real Claude API responder. Imported lazily so mock runs need no key."""
    import os
    from dotenv import load_dotenv
    load_dotenv(override=True)
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise SystemExit("ERROR: ANTHROPIC_API_KEY not set (needed for --live).")

    import anthropic
    client = anthropic.Anthropic()

    def responder(model: str, prompt_text: str, task: dict) -> dict:
        t0 = time.time()
        resp = client.messages.create(
            model=model, max_tokens=512,
            messages=[{"role": "user", "content": prompt_text}],
        )
        latency_ms = (time.time() - t0) * 1000
        text = resp.content[0].text if resp.content else ""
        return {
            "tokens_in":   resp.usage.input_tokens,
            "tokens_out":  resp.usage.output_tokens,
            "quality":     task.get("expected_quality", 0.9),
            "output_text": text,
            "latency_ms":  round(latency_ms, 1),
        }

    return responder


def run(live: bool = False):
    pipeline   = OptimizationPipeline(daily_budget=1_000_000)
    compressor = ContextCompressor()
    tasks      = build_conversation_tasks()
    responder  = make_live_responder() if live else mock_responder

    print("=" * 50)
    print(f"  ARGUS — Post-Run Optimization Report  ({'LIVE' if live else 'MOCK'})")
    print(f"  {len(tasks)} tasks")
    print("=" * 50)

    t0 = time.time()
    calls = []
    for i, task in enumerate(tasks, 1):
        tc = pipeline.process_tracked(task, responder, call_id=i, compressor=compressor)
        calls.append(tc)
    runtime = time.time() - t0

    out = generate_report(calls, runtime_s=runtime)
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true",
                    help="Use the real Claude API instead of the deterministic mock.")
    args = ap.parse_args()
    run(live=args.live)
