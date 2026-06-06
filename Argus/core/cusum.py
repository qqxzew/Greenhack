# core/cusum.py

from collections import deque
import numpy as np


class CUSUMDetector:
    """
    CUSUM (Cumulative Sum) control chart for anomaly detection.

    Maintains a running statistic S_t:
      S_t = max(0, S_{t-1} + (x_t - mu0 - k))

    Alert fires when S_t > h.
    """

    def __init__(
        self,
        mu0:     float = 1200.0,
        k:       float = 300.0,
        h:       float = 3000.0,
        warmup:  int   = 10,
    ):
        self.mu0    = mu0
        self.k      = k
        self.h      = h
        self.warmup = warmup
        self.S      = 0.0
        self.step   = 0
        self.recent = deque(maxlen=50)
        self.alerts = 0

    def update(self, tokens_used: int) -> bool:
        """Update with observed token count. Returns True if anomaly detected."""
        self.step += 1
        self.recent.append(tokens_used)

        if self.step <= self.warmup:
            self.mu0 = float(np.mean(self.recent))
            return False

        self.S = max(0.0, self.S + tokens_used - self.mu0 - self.k)

        if self.S > self.h:
            self.alerts += 1
            self.S = 0.0
            return True

        return False

    def reset(self):
        self.S = 0.0

    def state(self) -> dict:
        recent = list(self.recent)
        return {
            "S":          round(self.S, 1),
            "mu0":        round(self.mu0, 1),
            "h":          self.h,
            "alerts":     self.alerts,
            "step":       self.step,
            "recent_avg": round(float(np.mean(recent)), 1) if recent else 0.0,
        }
