# core/sprt.py

import numpy as np
import math


class SPRTStopper:
    """
    Sequential Probability Ratio Test for detecting stuck agents.

    Hypotheses:
      H0: agent is NOT progressing (mean progress ~ mu0)
      H1: agent IS progressing     (mean progress ~ mu1)

    Boundaries (Wald):
      Stop -> H0 (stuck)       if Lambda_t <= log(beta / (1 - alpha))
      Stop -> H1 (progressing) if Lambda_t >= log((1 - beta) / alpha)
      Continue                  otherwise
    """

    def __init__(
        self,
        alpha: float = 0.05,
        beta:  float = 0.10,
        # Progress is reported on a [0,1] scale (see pipeline.post_call:
        # quality·0.5 + (1−cost/0.05)·0.5). The hypotheses are calibrated to
        # THAT scale: a healthy agent's progress clusters near μ1, a
        # stuck/wasteful one near μ0. σ controls sensitivity — with these
        # values a persistently-wasteful agent (progress ≈ 0.15) trips
        # STOP_STUCK in ~4 steps, while a healthy one (≈ 0.90) never does.
        # (Old values 0.02/0.15/0.08 were calibrated for a different,
        # near-zero progress scale and made the live detector inert.)
        mu0:   float = 0.35,
        mu1:   float = 0.70,
        sigma: float = 0.45,
    ):
        self.mu0   = mu0
        self.mu1   = mu1
        self.sigma = sigma

        self.log_A = math.log(beta / (1 - alpha))
        self.log_B = math.log((1 - beta) / alpha)

        self.log_lambda = 0.0
        self.step = 0
        self.history: list[float] = []

    def update(self, progress_delta: float) -> str:
        """
        Update with observed progress delta at this agent step.
        Returns: "STOP_STUCK" | "STOP_PROGRESSING" | "CONTINUE"
        """
        self.step += 1
        self.history.append(progress_delta)

        # Per-step Gaussian log-likelihood ratio (Wald 1945):
        #   ln f_{μ1}(x)/f_{μ0}(x) = [(x−μ0)² − (x−μ1)²] / (2σ²)
        #                          = [x·(μ1−μ0) − ½(μ1²−μ0²)] / σ²
        # i.e. (μ1−μ0)/σ² · (x − (μ0+μ1)/2): positive when x is above the
        # midpoint of the two hypotheses, negative below.
        # NOTE: the leading term is x·(μ1−μ0), NOT (x−μ0)·(μ1−μ0) — the latter
        # injected a spurious −μ0·(μ1−μ0)/σ² bias every step, pushing the
        # statistic toward STOP_STUCK and inflating the true Type-I error.
        log_ratio = (
            progress_delta * (self.mu1 - self.mu0)
            - 0.5 * (self.mu1**2 - self.mu0**2)
        ) / (self.sigma**2)

        self.log_lambda += log_ratio

        if self.log_lambda <= self.log_A:
            return "STOP_STUCK"
        elif self.log_lambda >= self.log_B:
            return "STOP_PROGRESSING"
        else:
            return "CONTINUE"

    def reset(self):
        self.log_lambda = 0.0
        self.step = 0
        self.history = []

    def state(self) -> dict:
        return {
            "log_lambda":  round(self.log_lambda, 4),
            "step":        self.step,
            "boundary_lo": round(self.log_A, 4),
            "boundary_hi": round(self.log_B, 4),
        }
