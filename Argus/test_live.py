# test_live.py
"""
Live multi-agent test against the real Claude API.

Usage:
    python test_live.py

Required env:
    ANTHROPIC_API_KEY=your_key
"""

import asyncio
import os
import sys
from dotenv import load_dotenv

load_dotenv(override=True)

from core.pipeline           import OptimizationPipeline
from agents.hr_agent         import make_hr_agent, HR_TASKS
from agents.dev_agent        import make_dev_agent, DEV_TASKS
from agents.finance_agent    import make_finance_agent, FINANCE_TASKS
from agents.wasteful_agent   import WastefulAgent
from agents.spammer_agent    import make_spammer_agent, SPAMMER_TASKS
from eval.metrics            import TestReport
from eval.report             import generate_visual_report
from core.toon               import toon_savings_report


if not os.environ.get("ANTHROPIC_API_KEY"):
    print("ERROR: ANTHROPIC_API_KEY not set.")
    sys.exit(1)


async def run_all_agents():
    pipeline = OptimizationPipeline(daily_budget=500_000)

    hr       = make_hr_agent(pipeline)
    dev      = make_dev_agent(pipeline)
    finance  = make_finance_agent(pipeline)
    wasteful = WastefulAgent(pipeline)
    spammer  = make_spammer_agent(pipeline)

    total = len(HR_TASKS) + len(DEV_TASKS) + len(FINANCE_TASKS) + len(SPAMMER_TASKS) + 5
    print("=" * 60)
    print("  ARGUS -- Live Multi-Agent Test")
    print(f"  {total} total tasks")
    print("=" * 60)

    wasteful_tasks = [HR_TASKS[0], DEV_TASKS[0], FINANCE_TASKS[0],
                      HR_TASKS[1], DEV_TASKS[1]]

    results = await asyncio.gather(
        hr.run_tasks_async(HR_TASKS),
        dev.run_tasks_async(DEV_TASKS),
        finance.run_tasks_async(FINANCE_TASKS),
        wasteful.run_tasks_async(wasteful_tasks),
        spammer.run_tasks_async(SPAMMER_TASKS),
    )

    all_results = [r for batch in results for r in batch]

    report = TestReport(
        pipeline=pipeline,
        all_results=all_results,
        wasteful_results=results[3],
        agent_names=["hr", "dev", "finance", "wasteful", "spammer"],
    )
    report.print_summary()
    report.save_json("test_results.json")

    # Generate PNG report
    generate_visual_report(report)

    # Export TOON
    pipeline.logger.export_toon("test_results.toon")
    toon_info = toon_savings_report(pipeline.logger.recent(200))
    print(f"\n  TOON savings vs JSON: {toon_info['savings_pct']}%")
    print(f"  Events: {toon_info['json_approx_tokens']} tokens (JSON)"
          f"  →  {toon_info['toon_approx_tokens']} tokens (TOON)")

    return report


if __name__ == "__main__":
    asyncio.run(run_all_agents())
