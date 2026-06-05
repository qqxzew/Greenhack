# simulate.py
"""
CLI entry point for running the full simulation.
Usage: python simulate.py --duration 60 --agents 5
"""

import argparse
import threading
import time
import json
from core.pipeline    import OptimizationPipeline
from simulation.agent import SimulatedAgent


def run_simulation(duration: int = 60, n_agents: int = 5):
    pipeline = OptimizationPipeline(daily_budget=500_000)

    agents = [
        SimulatedAgent("agent-finance-1",  "normal",   pipeline, task_rate=1.5),
        SimulatedAgent("agent-finance-2",  "normal",   pipeline, task_rate=1.0),
        SimulatedAgent("agent-hr-1",       "normal",   pipeline, task_rate=0.8),
        SimulatedAgent("agent-pipeline-1", "wasteful", pipeline, task_rate=1.2),
        SimulatedAgent("agent-pipeline-2", "stuck",    pipeline, task_rate=0.5),
    ][:n_agents]

    print(f"Starting simulation: {len(agents)} agents, {duration}s duration")
    print("=" * 60)

    threads = [
        threading.Thread(target=a.run, args=(duration,), daemon=True)
        for a in agents
    ]
    for t in threads:
        t.start()

    start = time.time()
    while time.time() - start < duration:
        time.sleep(min(10, max(1, duration / 6)))
        agg = pipeline.logger.aggregate()
        state = pipeline.router.get_state()
        print(f"\n[{int(time.time()-start)}s] Events: {agg.get('total_events',0)} | "
              f"Avg cost: ${agg.get('avg_cost',0):.5f} | "
              f"Avg quality: {agg.get('avg_quality',0):.3f} | "
              f"Cache hit rate: {pipeline.cache.hit_rate:.2%} | "
              f"Anomalies: {agg.get('anomaly_count',0)}")
        routing = state.get("routing_dist", {})
        for model, frac in routing.items():
            bar = "#" * int(frac * 20)
            print(f"  {model[-20:]:20s} {bar:<20} {frac:.1%}")

    for t in threads:
        t.join(timeout=2)

    print("\n" + "=" * 60)
    print("FINAL STATE")
    print(json.dumps(pipeline.logger.aggregate(), indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration", type=int, default=60)
    parser.add_argument("--agents",   type=int, default=5)
    args = parser.parse_args()
    run_simulation(args.duration, args.agents)
