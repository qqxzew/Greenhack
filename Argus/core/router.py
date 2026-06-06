# core/router.py

import numpy as np
from core.features import FeatureExtractor
from core.logistic import OnlineLogisticRegression
from core.linucb import LinUCBRouter


MODELS = ["claude-haiku-4-5", "claude-sonnet-4-5"]

MODEL_COSTS = {
    "claude-haiku-4-5":   {"input": 0.00025, "output": 0.00125},
    "claude-sonnet-4-5":  {"input": 0.003,   "output": 0.015},
}


class HierarchicalRouter:
    """
    Variant B: Two-level hierarchical routing.

    Level 1 -- OnlineLogReg:
      Estimates task complexity from x_base.
      Updated when Haiku handles a task (provides ground truth label).

    Level 2 -- LinUCB:
      Selects optimal model using x_aug = [x_base | complexity_score].
      Reward: quality - lambda * cost.
    """

    AUGMENTED_DIM = 9

    def __init__(self, alpha: float = 1.2, lambda_cost: float = 0.4):
        self.extractor = FeatureExtractor()
        self.logistic  = OnlineLogisticRegression(dim=FeatureExtractor.DIM)
        self.linucb    = LinUCBRouter(
            models=MODELS,
            context_dim=self.AUGMENTED_DIM,
            alpha=alpha,
            lambda_cost=lambda_cost,
        )
        self.call_count = 0

    def choose(self, task: dict) -> tuple[str, dict]:
        """Decide which model to use for this task."""
        x_base = self.extractor.extract(task)
        complexity_score = self.logistic.predict(x_base)
        x_aug = np.append(x_base, complexity_score)

        model, ucb_debug = self.linucb.choose(x_aug)

        meta = {
            "x_base":           x_base.tolist(),
            "x_aug":            x_aug.tolist(),
            "complexity_score": round(complexity_score, 4),
            "ucb_scores":       ucb_debug,
        }
        return model, meta

    def update(self, model: str, task: dict, meta: dict,
               quality: float, cost: float) -> dict:
        """Update both learning components after receiving LLM result."""
        self.call_count += 1
        x_base = np.array(meta["x_base"])
        x_aug  = np.array(meta["x_aug"])

        reward = self.linucb.update(model, x_aug, quality, cost)

        logistic_updated = False
        if model == MODELS[0]:
            self.logistic.update(x_base, quality)
            logistic_updated = True

        return {
            "reward":           round(reward, 4),
            "logistic_updated": logistic_updated,
            "call_count":       self.call_count,
        }

    def get_state(self) -> dict:
        return {
            "call_count":   self.call_count,
            "linucb":       self.linucb.to_dict(),
            "logistic":     self.logistic.to_dict(),
            "routing_dist": self.linucb.get_routing_distribution(),
        }
