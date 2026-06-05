#!/usr/bin/env python3.11
# make_investor_report.py
"""
Generate the Argus Investor Report — a single self-contained HTML page showing
cost AND time before/after at scale, plus a transparent account of exactly how
the synthetic workload was generated.

Everything is configurable at startup and fully reproducible (seeded). The
default run is a deterministic MOCK: no API key, no spend.

Examples
--------
    # headline 300-task run (free, reproducible)
    python3.11 make_investor_report.py

    # bigger workload to show scalability
    python3.11 make_investor_report.py --n 2000

    # a frontier-heavy mix (more tasks that truly need Opus)
    python3.11 make_investor_report.py --difficulty frontier --reasoning-depth 1.6

    # dial individual knobs
    python3.11 make_investor_report.py --n 500 --strong-frac 0.08 \
        --cache-rate 0.18 --loop-rate 0.05 --compressible 0.7 --seed 42

    # real Claude API calls (needs ANTHROPIC_API_KEY)
    python3.11 make_investor_report.py --n 60 --live
"""

import argparse
import shutil
import time
from pathlib import Path

from simulation.dataset       import (GenerationConfig, generate_dataset,
                                       make_mock_responder, ROUTING_TIERS)
from core.pipeline            import OptimizationPipeline
from core.compression         import ContextCompressor
from eval.investor_report     import generate_investor_report


REPORTS_DIR = Path("reports")


def clean_old_reports() -> int:
    """Delete previous investor reports so each run leaves exactly one.

    Only touches `reports/investor_*` directories — the namespace this script
    owns. Other report kinds (e.g. reports/report_*) are left untouched.
    """
    if not REPORTS_DIR.exists():
        return 0
    removed = 0
    for path in sorted(REPORTS_DIR.glob("investor_*")):
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
            removed += 1
    if removed:
        print(f"  Cleaned {removed} previous investor report(s) from {REPORTS_DIR}/")
    return removed


def make_live_responder():
    """Real Claude API responder. Imported lazily so mock runs need no key."""
    import os
    from dotenv import load_dotenv
    load_dotenv(override=True)
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise SystemExit("ERROR: ANTHROPIC_API_KEY not set (needed for --live).")

    import anthropic
    from simulation.dataset import quality_for
    from core.tracking      import BASELINE_MODEL
    client = anthropic.Anthropic()

    def responder(model: str, prompt_text: str, task: dict) -> dict:
        t0 = time.time()
        resp = client.messages.create(
            model=model, max_tokens=min(1024, task.get("out_tokens", 512) or 512),
            messages=[{"role": "user", "content": prompt_text}],
        )
        latency_ms = (time.time() - t0) * 1000
        text = resp.content[0].text if resp.content else ""
        required = task.get("required_model", BASELINE_MODEL)
        ceiling  = task.get("ceiling_quality", 0.93)
        return {
            "tokens_in":   resp.usage.input_tokens,
            "tokens_out":  resp.usage.output_tokens,
            "quality":     quality_for(model, required, ceiling),
            "output_text": text,
            "latency_ms":  round(latency_ms, 1),
        }

    return responder


def build_config(args) -> GenerationConfig:
    return GenerationConfig(
        n_tasks=args.n,
        seed=args.seed,
        difficulty=args.difficulty,
        strong_model_fraction=args.strong_frac,
        reasoning_depth=args.reasoning_depth,
        cache_hit_rate=args.cache_rate,
        loop_rate=args.loop_rate,
        compressible_fraction=args.compressible,
    )


def run(args):
    cfg       = build_config(args)
    if not args.keep_old:
        clean_old_reports()
    tasks, manifest = generate_dataset(cfg)
    responder = make_live_responder() if args.live else make_mock_responder(cfg)
    pipeline  = OptimizationPipeline(daily_budget=10**12)
    compressor = ContextCompressor()

    print("=" * 62)
    print(f"  ARGUS — Investor Report  ({'LIVE' if args.live else 'MOCK'})")
    print(f"  {len(tasks)} tasks · difficulty={cfg.difficulty} · seed={cfg.seed}")
    print("=" * 62)

    t0 = time.time()
    calls = []
    for i, task in enumerate(tasks, 1):
        tc = pipeline.process_tracked(
            task, responder, call_id=i, compressor=compressor, tiers=ROUTING_TIERS)
        calls.append(tc)
    runtime = time.time() - t0

    return generate_investor_report(calls, manifest, runtime_s=runtime)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="Generate the Argus investor report (mock by default, free).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    ap.add_argument("--n", type=int, default=300, help="number of tasks to generate")
    ap.add_argument("--seed", type=int, default=7, help="RNG seed (reproducibility)")
    ap.add_argument("--difficulty", choices=["easy", "balanced", "hard", "frontier"],
                    default="balanced", help="band-weight preset")
    ap.add_argument("--strong-frac", type=float, default=None,
                    help="override expert (Opus-needing) task fraction, e.g. 0.10")
    ap.add_argument("--reasoning-depth", type=float, default=1.0,
                    help="scale reasoning tokens on hard+expert tasks")
    ap.add_argument("--cache-rate", type=float, default=0.12,
                    help="fraction of tasks that are exact cache repeats")
    ap.add_argument("--loop-rate", type=float, default=0.04,
                    help="fraction of tasks that are stuck loops (SPRT stop)")
    ap.add_argument("--compressible", type=float, default=0.65,
                    help="fraction of verbose tasks allowed to compress")
    ap.add_argument("--live", action="store_true",
                    help="use the real Claude API instead of the deterministic mock")
    ap.add_argument("--keep-old", action="store_true",
                    help="do NOT delete previous reports/investor_* dirs before running")
    args = ap.parse_args()
    run(args)
