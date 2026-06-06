# agents/wasteful_agent.py

from agents.base_agent import BaseAgent
from core.pipeline import OptimizationPipeline


class WastefulAgent(BaseAgent):
    """
    Control agent: always calls claude-sonnet-4-5, no caching, no routing.
    Purpose:
      1. Establishes cost baseline for savings calculation
      2. Triggers CUSUM anomaly detector (high token consumption)
      3. Demonstrates what unoptimized agents look like
    """

    def __init__(self, pipeline: OptimizationPipeline):
        super().__init__(
            agent_id="agent-wasteful",
            persona="You are a helpful assistant.",
            pipeline=pipeline,
            use_optimization=False,
        )

    def execute(self, task: dict):
        from eval.judge import score_quality

        result = self.call_claude("claude-sonnet-4-5", task)
        result.quality = score_quality(task, result.response)

        # Still log to pipeline so CUSUM has data on this agent
        self.pipeline.post_call(
            self.agent_id, task,
            {"action": "call_llm", "model": "claude-sonnet-4-5",
             "meta": {"x_base": [0.0] * 8, "x_aug": [0.0] * 9,
                      "complexity_score": 0.5, "ucb_scores": {}}},
            {
                "response":     result.response,
                "quality":      result.quality,
                "tokens_in":    result.tokens_in,
                "tokens_out":   result.tokens_out,
                "tokens_total": result.tokens_total,
                "cost":         result.cost,
                "latency":      result.latency,
            },
        )
        self.results.append(result)
        return result
