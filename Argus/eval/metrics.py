# eval/metrics.py

import json
from dataclasses import dataclass
from agents.base_agent import CallResult
from core.pipeline import OptimizationPipeline


@dataclass
class TestReport:
    pipeline:         OptimizationPipeline
    all_results:      list[CallResult]
    wasteful_results: list[CallResult]
    agent_names:      list[str]

    def _baseline_cost(self) -> float:
        """What all LLM calls would have cost if all used Sonnet, priced fairly
        with separate input/output rates (output is 5x input on Sonnet)."""
        return sum(
            r.tokens_in / 1000 * 0.003 + r.tokens_out / 1000 * 0.015
            for r in self.all_results
            if r.source == "llm"
        )

    def _actual_cost(self) -> float:
        return sum(r.cost for r in self.all_results)

    def _cache_savings(self) -> dict:
        cache_hits = [r for r in self.all_results if "cache" in r.source]
        tokens_saved = len(cache_hits) * 1200
        cost_saved   = len(cache_hits) * 1200 / 1000 * 0.003
        return {
            "hit_count":    len(cache_hits),
            "hit_rate":     len(cache_hits) / len(self.all_results) if self.all_results else 0,
            "tokens_saved": tokens_saved,
            "cost_saved":   round(cost_saved, 4),
        }

    def _routing_summary(self) -> dict:
        llm_calls = [r for r in self.all_results if r.source == "llm"]
        from collections import Counter
        model_dist = Counter(r.model for r in llm_calls)
        total = len(llm_calls)
        return {
            model: {"count": n, "pct": round(n / total * 100, 1) if total else 0}
            for model, n in model_dist.items()
        }

    def _quality_summary(self) -> dict:
        scored = [r.quality for r in self.all_results if r.quality > 0]
        wasteful_q = [r.quality for r in self.wasteful_results if r.quality > 0]
        return {
            "optimized_avg": round(sum(scored) / len(scored), 4) if scored else 0,
            "baseline_avg":  round(sum(wasteful_q) / len(wasteful_q), 4) if wasteful_q else 0,
            "quality_delta": round(
                (sum(scored) / len(scored) if scored else 0)
                - (sum(wasteful_q) / len(wasteful_q) if wasteful_q else 0),
                4,
            ),
        }

    def _anomaly_summary(self) -> dict:
        cusum_states: dict = {}
        try:
            full_state = self.pipeline.get_full_state()
            cusum_states = full_state.get("cusum", {})
        except Exception:
            pass
        return {
            agent_id: {
                "alerts":  s.get("alerts", 0),
                "current": round(s.get("S", 0), 1),
            }
            for agent_id, s in cusum_states.items()
        }

    def print_summary(self):
        baseline  = self._baseline_cost()
        actual    = self._actual_cost()
        savings   = (1 - actual / baseline) * 100 if baseline > 0 else 0
        cache     = self._cache_savings()
        routing   = self._routing_summary()
        quality   = self._quality_summary()
        anomalies = self._anomaly_summary()

        print("\n" + "=" * 60)
        print("  FINAL REPORT")
        print("=" * 60)
        print(f"\n  Total tasks:      {len(self.all_results)}")
        print(f"  Baseline cost:    ${baseline:.4f}  (all Sonnet)")
        print(f"  Actual cost:      ${actual:.4f}")
        print(f"  Cost saved:       {savings:.1f}%  (-${baseline - actual:.4f})")
        print(f"\n  Cache hits:       {cache['hit_count']}  ({cache['hit_rate']:.1%})")
        print(f"  Tokens saved:     {cache['tokens_saved']:,}")
        print(f"\n  Routing:")
        for model, data in routing.items():
            bar = "█" * int(data["pct"] / 5)
            print(f"    {model[-20:]:20s} {bar:<20} {data['pct']:.1f}%")
        print(f"\n  Quality (optimized): {quality['optimized_avg']:.3f}")
        print(f"  Quality (baseline):  {quality['baseline_avg']:.3f}")
        print(f"  Quality delta:       {quality['quality_delta']:+.3f}")
        print(f"\n  CUSUM anomalies:")
        for agent_id, data in anomalies.items():
            flag = "🚨" if data["alerts"] > 0 else "✅"
            print(f"    {flag} {agent_id}: {data['alerts']} alerts")
        print("\n" + "=" * 60)

    def save_json(self, path: str):
        data = {
            "baseline_cost":  self._baseline_cost(),
            "actual_cost":    self._actual_cost(),
            "savings_pct":    (1 - self._actual_cost() / self._baseline_cost()) * 100
                              if self._baseline_cost() > 0 else 0,
            "cache":          self._cache_savings(),
            "routing":        self._routing_summary(),
            "quality":        self._quality_summary(),
            "anomalies":      self._anomaly_summary(),
            "pipeline_state": self.pipeline.get_full_state(),
        }
        with open(path, "w") as f:
            json.dump(data, f, indent=2, default=str)
        print(f"\n  Saved: {path}")
