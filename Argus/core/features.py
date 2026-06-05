# core/features.py

import numpy as np
import re


HARD_KEYWORDS = [
    "analyze", "analyse", "compare", "critique", "reason", "argue",
    "evaluate", "design", "architect", "debug", "security", "optimize",
    "explain why", "prove", "derive", "strategize"
]
EASY_KEYWORDS = [
    "summarize", "summarise", "translate", "list", "format",
    "extract", "convert", "what is", "define", "rephrase"
]
TASK_TYPES = ["summarize", "code_review", "qa", "analysis", "translation", "generation"]


class FeatureExtractor:
    """
    Extracts x_base in R^8 from a raw task dict.

    Feature layout:
      [0] normalized prompt length        (float, 0-1)
      [1] lexical complexity score        (float, 0-1)
      [2] urgency                         (float, 0-1)
      [3] is_code_task                    (0 or 1)
      [4] is_summarize_task               (0 or 1)
      [5] is_analysis_task                (0 or 1)
      [6] is_qa_task                      (0 or 1)
      [7] has_structured_output_request   (0 or 1)
    """

    DIM = 8

    def extract(self, task: dict) -> np.ndarray:
        prompt = task.get("prompt", "").lower()
        task_type = task.get("type", "qa").lower()
        urgency = float(task.get("urgency", 0.5))

        length_score = min(len(prompt.split()) / 300, 1.0)

        hard = sum(1 for kw in HARD_KEYWORDS if kw in prompt)
        easy = sum(1 for kw in EASY_KEYWORDS if kw in prompt)
        lexical = np.clip((hard - easy * 0.5) / 4.0 + 0.3, 0.0, 1.0)

        urgency = np.clip(urgency, 0.0, 1.0)

        is_code     = 1.0 if "code" in task_type or "debug" in task_type else 0.0
        is_summarize = 1.0 if "summar" in task_type else 0.0
        is_analysis  = 1.0 if "analys" in task_type else 0.0
        is_qa        = 1.0 if "qa" in task_type or "question" in task_type else 0.0
        has_json     = 1.0 if "json" in prompt or "structured" in prompt else 0.0

        return np.array([
            length_score, lexical, urgency,
            is_code, is_summarize, is_analysis, is_qa, has_json
        ], dtype=np.float64)
