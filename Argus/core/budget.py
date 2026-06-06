# core/budget.py

import numpy as np
from dataclasses import dataclass, field


@dataclass
class BudgetNode:
    name:      str
    budget:    float
    used:      float = 0.0
    children:  list = field(default_factory=list)

    @property
    def remaining(self) -> float:
        return max(0.0, self.budget - self.used)

    @property
    def utilization(self) -> float:
        return self.used / self.budget if self.budget > 0 else 0.0

    @property
    def is_exhausted(self) -> bool:
        return self.used >= self.budget * 0.95


def water_filling(sigmas: np.ndarray, total_budget: float) -> np.ndarray:
    """
    Optimal budget allocation via water-filling.

    Each agent i has 'noise level' sigma_i (uncertainty / task difficulty).
    Low sigma -> predictable agent -> gets more budget.
    High sigma -> unpredictable -> gets less.

    b_i* = max(0, mu - sigma_i^2)
    where mu is the water level, solved by binary search to satisfy sum = B.
    """
    n = len(sigmas)
    variances = sigmas ** 2

    lo = float(np.min(variances))
    hi = float(np.max(variances)) + total_budget

    for _ in range(200):
        mu = (lo + hi) / 2.0
        allocation = np.maximum(0.0, mu - variances)
        if allocation.sum() < total_budget:
            lo = mu
        else:
            hi = mu

    allocation = np.maximum(0.0, mu - variances)
    total = allocation.sum()
    if total > 0:
        allocation = allocation * total_budget / total

    return allocation


class HierarchicalBudget:
    """
    Tree-structured token budget. Org -> Teams -> Agents -> Tasks.

    When an agent's budget is exhausted:
      - Degrade to smaller model automatically
      - Block further calls if critically over budget
    """

    def __init__(self, daily_budget: float = 100_000):
        self.root = BudgetNode("org", daily_budget)
        self.agents: dict[str, BudgetNode] = {}

    def register_agent(self, agent_id: str, budget: float):
        node = BudgetNode(agent_id, budget)
        self.agents[agent_id] = node
        self.root.children.append(node)

    def consume(self, agent_id: str, tokens: int) -> str:
        """
        Record token consumption and return recommended action:
          'ok'      -- within budget
          'degrade' -- over 80% of budget, use smaller model
          'block'   -- budget exhausted
        """
        if agent_id not in self.agents:
            # Demo budget bumped from 10k -> 1M so the live-traffic generator
            # doesn't blow per-agent budgets within a minute and stop the dashboard.
            self.register_agent(agent_id, budget=1_000_000)

        node = self.agents[agent_id]
        node.used += tokens
        self.root.used += tokens

        if node.utilization >= 1.0:
            return "block"
        elif node.utilization >= 0.80:
            return "degrade"
        return "ok"

    def reallocate(self, agent_sigmas: dict[str, float]):
        """Rebalance budgets using water-filling."""
        if not agent_sigmas:
            return

        ids = list(agent_sigmas.keys())
        sigmas = np.array([agent_sigmas[i] for i in ids])
        remaining_budget = self.root.remaining

        new_budgets = water_filling(sigmas, remaining_budget)

        for agent_id, budget in zip(ids, new_budgets):
            if agent_id in self.agents:
                self.agents[agent_id].budget = (
                    self.agents[agent_id].used + budget
                )

    def state(self) -> dict:
        return {
            "org_budget":    self.root.budget,
            "org_used":      round(self.root.used, 0),
            "org_remaining": round(self.root.remaining, 0),
            "agents": {
                aid: {
                    "budget":      round(n.budget, 0),
                    "used":        round(n.used, 0),
                    "utilization": round(n.utilization, 4),
                    "action":      "block" if n.is_exhausted else
                                   "degrade" if n.utilization >= 0.80 else "ok",
                }
                for aid, n in self.agents.items()
            }
        }
