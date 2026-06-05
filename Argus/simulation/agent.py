# simulation/agent.py

import time
import random
import numpy as np
from simulation.task_generator import generate_task
from simulation.llm_mock       import simulate_llm_call
from core.pipeline             import OptimizationPipeline


class SimulatedAgent:
    """
    Simulates an AI agent that processes tasks and calls LLMs.

    Behaviors:
      normal   -- standard agent, uses optimization pipeline
      wasteful -- ignores routing, always calls expensive model (CUSUM demo)
      stuck    -- occasionally loops on same task 8-14 times (SPRT demo)
    """

    def __init__(
        self,
        agent_id:  str,
        behavior:  str,
        pipeline:  OptimizationPipeline,
        task_rate: float = 1.0,
    ):
        self.agent_id   = agent_id
        self.behavior   = behavior
        self.pipeline   = pipeline
        self.task_rate  = task_rate
        self.running    = False
        self.tasks_done = 0
        self.events: list[dict] = []

    def run_one_task(self):
        task = generate_task()

        if self.behavior == "wasteful":
            # Bypass routing -- always use expensive model
            result = simulate_llm_call("claude-sonnet-4-5", task)
            # Still run it through the pipeline so it's logged + monitored
            decision = {
                "action":  "call_llm",
                "model":   "claude-sonnet-4-5",
                "meta":    {"x_base": [0.0] * 8, "x_aug": [0.0] * 9,
                            "complexity_score": 0.5, "ucb_scores": {}},
                "task_id": task["id"],
            }
            # Bump tokens to actually trigger CUSUM
            result["tokens_total"] = int(result["tokens_total"] * 2.5)
            result["cost"] *= 2.5
            self.pipeline.post_call(self.agent_id, task, decision, result)

        elif self.behavior == "stuck":
            # Simulate getting stuck in a loop
            n_loops = random.randint(8, 14) if random.random() < 0.3 else 1
            for _ in range(n_loops):
                decision = self.pipeline.pre_call(self.agent_id, task)
                if decision["action"] == "call_llm":
                    model  = decision["model"]
                    result = simulate_llm_call(model, task)
                    # Stuck agent makes near-zero progress -- force low quality
                    result["quality"] = max(0.05, result["quality"] * 0.2)
                    self.pipeline.post_call(self.agent_id, task, decision, result)
                time.sleep(0.05)

        else:   # normal
            decision = self.pipeline.pre_call(self.agent_id, task)

            if decision["action"] == "cache_hit":
                self.tasks_done += 1
                return

            if decision["action"] == "blocked":
                return

            model  = decision.get("model", "claude-haiku-4-5")
            result = simulate_llm_call(model, task)
            self.pipeline.post_call(self.agent_id, task, decision, result)

        self.tasks_done += 1

    def run(self, duration_seconds: float = 60.0):
        """Run agent loop for given duration."""
        self.running = True
        end_time = time.time() + duration_seconds

        while self.running and time.time() < end_time:
            try:
                self.run_one_task()
            except Exception as e:
                print(f"[{self.agent_id}] error: {e}")
            sleep_time = np.random.exponential(1.0 / self.task_rate)
            time.sleep(sleep_time)

        self.running = False
