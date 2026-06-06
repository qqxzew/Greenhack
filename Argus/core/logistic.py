# core/logistic.py

import numpy as np


class OnlineLogisticRegression:
    """
    Binary classifier: predicts P(task is hard | x_base).

    Training signal:
      - Called ONLY when small model (Haiku) handled the task
      - Label: was_hard = (quality < QUALITY_THRESHOLD)
      - Update: SGD with decaying learning rate

    This acts as Level 1 of the hierarchical router.
    Its output (complexity_score) is appended to x_base
    to form x_aug (dim = 9), which feeds LinUCB.
    """

    QUALITY_THRESHOLD = 0.72

    def __init__(self, dim: int = 8, lr: float = 0.05, reg: float = 1e-4):
        self.dim = dim
        self.lr = lr
        self.reg = reg
        self.w = np.zeros(dim, dtype=np.float64)
        self.n_updates = 0

    def predict(self, x: np.ndarray) -> float:
        """Returns P(hard | x) in [0, 1]."""
        logit = float(np.dot(self.w, x))
        logit = np.clip(logit, -15.0, 15.0)
        return float(1.0 / (1.0 + np.exp(-logit)))

    def update(self, x: np.ndarray, quality: float) -> None:
        """
        Update weights given observed quality on a task handled by small model.
        was_hard = True  if quality < threshold (small model struggled)
        was_hard = False if quality >= threshold (small model was fine)
        """
        y = 1.0 if quality < self.QUALITY_THRESHOLD else 0.0
        pred = self.predict(x)
        grad = (pred - y) * x + self.reg * self.w

        effective_lr = self.lr / np.sqrt(1.0 + self.n_updates * 0.1)
        self.w -= effective_lr * grad
        self.n_updates += 1

    def to_dict(self) -> dict:
        return {"weights": self.w.tolist(), "n_updates": self.n_updates}
