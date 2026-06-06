# simulation/llm_mock.py

import numpy as np
import time

MODEL_PROFILES = {
    "claude-haiku-4-5": {
        "cost_input":      0.00025,
        "cost_output":     0.00125,
        "latency_base":    0.6,
        "quality_easy":    (0.82, 0.94),
        "quality_hard":    (0.52, 0.71),
        "tokens_in_base":  900,
        "tokens_out_base": 200,
    },
    "claude-sonnet-4-5": {
        "cost_input":      0.003,
        "cost_output":     0.015,
        "latency_base":    1.4,
        "quality_easy":    (0.87, 0.96),
        "quality_hard":    (0.80, 0.93),
        "tokens_in_base":  900,
        "tokens_out_base": 380,
    },
}

COMPLEXITY_THRESHOLD = 0.45


def simulate_llm_call(model: str, task: dict) -> dict:
    """Simulates an LLM API call with realistic statistical behavior."""
    profile    = MODEL_PROFILES.get(model, MODEL_PROFILES["claude-haiku-4-5"])
    complexity = task.get("complexity", 0.5)

    if complexity < COMPLEXITY_THRESHOLD:
        quality = float(np.random.uniform(*profile["quality_easy"]))
    else:
        quality = float(np.random.uniform(*profile["quality_hard"]))

    tokens_in  = int(profile["tokens_in_base"]  * (1 + complexity * 0.6)
                     * np.random.lognormal(0, 0.1))
    tokens_out = int(profile["tokens_out_base"] * (1 + complexity * 0.4)
                     * np.random.lognormal(0, 0.15))
    tokens_total = tokens_in + tokens_out

    cost = (
        tokens_in  / 1000 * profile["cost_input"] +
        tokens_out / 1000 * profile["cost_output"]
    )
    latency = profile["latency_base"] * float(np.random.lognormal(0, 0.2))

    return {
        "model":        model,
        "response":     f"[Mock response for {task.get('type','task')} task, quality={quality:.2f}]",
        "quality":      round(quality, 4),
        "tokens_in":    tokens_in,
        "tokens_out":   tokens_out,
        "tokens_total": tokens_total,
        "cost":         round(cost, 6),
        "latency":      round(latency, 3),
    }
