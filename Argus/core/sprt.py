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
        mu0:   float = 0.02,
        mu1:   float = 0.15,
        sigma: float = 0.08,
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

        log_ratio = (
            (progress_delta - self.mu0) * (self.mu1 - self.mu0)
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
