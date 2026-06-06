# core/linucb.py

import numpy as np
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ModelStats:
    name: str
    calls: int = 0
    total_reward: float = 0.0
    total_tokens: int = 0
    total_cost: float = 0.0

    @property
    def avg_reward(self) -> float:
        return self.total_reward / self.calls if self.calls > 0 else 0.0

    @property
    def avg_cost(self) -> float:
        return self.total_cost / self.calls if self.calls > 0 else 0.0


class LinUCBRouter:
    """
    LinUCB (Linear Upper Confidence Bound) for model routing.

    For each model m, maintains:
      A_m in R^{d x d}  -- feature covariance matrix (initialized to 0.1 * I)
      b_m in R^d        -- reward-weighted feature sum

    At decision time for feature vector x:
      theta_m = A_m^{-1} b_m
      score   = theta_m^T x + alpha * sqrt(x^T A_m^{-1} x)

    Reward function: quality - lambda_cost * normalized_cost
    """

    def __init__(
        self,
        models: list[str],
        context_dim: int,
        alpha: float = 1.2,
        lambda_cost: float = 0.4,
    ):
        self.models = models
        self.dim = context_dim
        self.alpha = alpha
        self.lambda_cost = lambda_cost
        self.t = 1

        self.A: dict[str, np.ndarray] = {
            m: np.eye(context_dim, dtype=np.float64) * 0.1
            for m in models
        }
        self.b: dict[str, np.ndarray] = {
            m: np.zeros(context_dim, dtype=np.float64)
            for m in models
        }
        self.stats: dict[str, ModelStats] = {
            m: ModelStats(name=m) for m in models
        }

    def choose(self, x: np.ndarray) -> tuple[str, dict]:
        """Choose the best model for feature vector x."""
        scores = {}
        debug = {}

        for m in self.models:
            A_inv = np.linalg.inv(self.A[m])
            theta = A_inv @ self.b[m]

            exploit = float(theta @ x)
            explore = self.alpha * float(np.sqrt(x @ A_inv @ x))
            scores[m] = exploit + explore

            debug[m] = {
                "exploit": round(exploit, 4),
                "explore": round(explore, 4),
                "score":   round(exploit + explore, 4),
            }

        # Random tie-break: at cold start every arm has the same score, so a
        # deterministic argmax would always pick the first model and never
        # explore. Break ties uniformly so cold-start exploration is real.
        max_score = max(scores.values())
        candidates = [m for m, s in scores.items() if s >= max_score - 1e-9]
        chosen = candidates[0] if len(candidates) == 1 \
                 else candidates[int(np.random.randint(len(candidates)))]
        self.t += 1
        return chosen, debug

    def update(self, model: str, x: np.ndarray, quality: float, cost: float) -> float:
        """
        Update model m's matrices after observing quality and cost.
        Reward = quality - lambda_cost * (cost / max_cost)
        """
        MAX_COST = 0.05
        normalized_cost = min(cost / MAX_COST, 1.0)
        reward = quality - self.lambda_cost * normalized_cost

        # Lazily register any model we're asked to learn from but didn't choose
        # ourselves — e.g. a call the caller force-routed to a model outside our
        # pool (Opus). Without this, A[model] would KeyError on the first such
        # call. The model still isn't added to `self.models`, so choose() never
        # auto-selects it; we just track its stats and feed back its reward.
        if model not in self.A:
            self.A[model] = np.eye(self.dim, dtype=np.float64) * 0.1
            self.b[model] = np.zeros(self.dim, dtype=np.float64)
            self.stats[model] = ModelStats(name=model)

        self.A[model] += np.outer(x, x)
        self.b[model] += reward * x

        s = self.stats[model]
        s.calls += 1
        s.total_reward += reward
        s.total_cost += cost

        return reward

    def get_routing_distribution(self) -> dict[str, float]:
        """Returns fraction of calls per model."""
        total = sum(s.calls for s in self.stats.values())
        if total == 0:
            return {m: 0.0 for m in self.models}
        return {m: s.calls / total for m, s in self.stats.items()}

    def to_dict(self) -> dict:
        return {
            "t": self.t,
            "stats": {m: vars(s) for m, s in self.stats.items()},
            "routing": self.get_routing_distribution(),
        }
