# storage/metrics.py

from collections import deque
from statistics import mean


class MetricsStore:
    """
    Computes rolling metrics from the event stream.
    Lightweight in-memory companion to EventLogger.
    """

    def __init__(self, window: int = 200):
        self.window = window
        self.costs   = deque(maxlen=window)
        self.tokens  = deque(maxlen=window)
        self.quality = deque(maxlen=window)
        self.rewards = deque(maxlen=window)

    def add(self, event: dict):
        if "cost" in event:
            self.costs.append(event["cost"])
        if "tokens_total" in event:
            self.tokens.append(event["tokens_total"])
        if "quality" in event:
            self.quality.append(event["quality"])
        if "reward" in event:
            self.rewards.append(event["reward"])

    def snapshot(self) -> dict:
        def avg(d):
            return round(mean(d), 6) if d else 0.0
        return {
            "rolling_cost":    avg(self.costs),
            "rolling_tokens":  avg(self.tokens),
            "rolling_quality": avg(self.quality),
            "rolling_reward":  avg(self.rewards),
            "window":          self.window,
            "samples":         len(self.costs),
        }
