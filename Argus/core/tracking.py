# core/tracking.py
"""
Per-call tracking + honest savings accounting.

Every LLM call is wrapped in a TrackedCall that records BOTH:
  - the baseline (what the call WOULD have cost with no optimization), and
  - the actual   (what really happened).

The baseline is always "the same request on Sonnet with the full, uncompressed
prompt" — the worst case a naive agent would have paid.

Savings are decomposed per-mechanism so the report's breakdown table is exact:
the components returned by `savings_components()` always sum to `cost_saved`.
Prices are list-price (no prefix-cache discount) on BOTH sides, so the
decomposition is reproducible and the comparison is apples-to-apples.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum


# Per-1k-token prices (USD). Matches core/router.py and agents/base_agent.py.
MODEL_COSTS = {
    "claude-haiku-4-5":  {"input": 0.00025, "output": 0.00125},
    "claude-sonnet-4-5": {"input": 0.003,   "output": 0.015},
    "claude-opus-4-7":   {"input": 0.015,   "output": 0.075},
    "cache":             {"input": 0.0,     "output": 0.0},
}

# Capability rank — used to model the quality a model can deliver on a task of a
# given difficulty. A model at or above the task's required tier delivers full
# quality; below it, quality degrades. (haiku < sonnet < opus.)
MODEL_CAPABILITY = {
    "claude-haiku-4-5":  1,
    "claude-sonnet-4-5": 2,
    "claude-opus-4-7":   3,
    "cache":             3,   # a cache hit reproduces a prior full-quality answer
}

# Latency profile per model (milliseconds). Modeled, not measured: a fixed
# time-to-first-token plus a per-input-token prefill cost plus a per-output-token
# decode cost. Bigger models are slower on every axis. These are the numbers the
# methodology doc cites; latency in a MOCK run is derived from them so the
# time-savings chart is fully reproducible without spending money.
MODEL_LATENCY = {
    "claude-haiku-4-5":  {"base_ms": 180.0, "ms_per_in_tok": 0.020, "ms_per_out_tok": 4.0},
    "claude-sonnet-4-5": {"base_ms": 380.0, "ms_per_in_tok": 0.045, "ms_per_out_tok": 11.0},
    "claude-opus-4-7":   {"base_ms": 700.0, "ms_per_in_tok": 0.070, "ms_per_out_tok": 26.0},
    "cache":             {"base_ms": 6.0,   "ms_per_in_tok": 0.0,   "ms_per_out_tok": 0.0},
}

CACHE_LOOKUP_MS = 6.0   # latency of a semantic-cache hit (no model call)

BASELINE_MODEL = "claude-sonnet-4-5"   # the "what if we never optimized" model


class OptType(str, Enum):
    CACHE    = "CACHE"
    COMPRESS = "COMPRESS"
    ROUTE    = "ROUTE"
    STOP     = "STOP"
    NONE     = "NONE"


def cost_of(model: str, tokens_in: int, tokens_out: int) -> float:
    c = MODEL_COSTS.get(model, MODEL_COSTS["claude-haiku-4-5"])
    return tokens_in / 1000 * c["input"] + tokens_out / 1000 * c["output"]


def latency_of(model: str, tokens_in: int, tokens_out: int) -> float:
    """Modeled wall-clock latency (ms) for one call of `model` on the given
    token counts. Deterministic, so MOCK runs reproduce the time chart exactly."""
    p = MODEL_LATENCY.get(model, MODEL_LATENCY["claude-haiku-4-5"])
    return p["base_ms"] + tokens_in * p["ms_per_in_tok"] + tokens_out * p["ms_per_out_tok"]


def capability_of(model: str) -> int:
    return MODEL_CAPABILITY.get(model, 1)


@dataclass
class TrackedCall:
    call_id:    int
    agent_id:   str
    task_type:  str
    timestamp:  float

    # Baseline — what would have happened without any optimization.
    baseline_prompt:     str
    baseline_tokens_in:  int
    baseline_tokens_out: int
    baseline_model:      str
    baseline_cost:       float

    # Actual — what really happened (actual_prompt is None on a cache hit).
    actual_prompt:     str | None
    actual_tokens_in:  int
    actual_tokens_out: int
    actual_model:      str
    actual_cost:       float

    # Optimization.
    optimization_applied: OptType
    optimization_detail:  dict

    # Quality.
    quality_score:    float | None
    output_text:      str
    baseline_quality: float | None = None
    latency_ms:       float = 0.0
    baseline_latency_ms: float = 0.0

    # The model this task genuinely requires (drives the quality-matched
    # baseline in the investor report). Defaults to the flat baseline model.
    required_model: str = BASELINE_MODEL

    # ── computed ───────────────────────────────────────────────────
    @property
    def tokens_saved(self) -> int:
        return max(0, self.baseline_tokens_in - self.actual_tokens_in)

    @property
    def cost_saved(self) -> float:
        return self.baseline_cost - self.actual_cost

    @property
    def saved_pct(self) -> float:
        return (self.cost_saved / self.baseline_cost * 100) if self.baseline_cost else 0.0

    @property
    def latency_saved_ms(self) -> float:
        return self.baseline_latency_ms - self.latency_ms

    @property
    def latency_saved_pct(self) -> float:
        return (self.latency_saved_ms / self.baseline_latency_ms * 100) if self.baseline_latency_ms else 0.0

    def savings_components(self) -> dict:
        """Cost saving attributed to each mechanism. Sums to `cost_saved`."""
        comp = {"CACHE": 0.0, "COMPRESS": 0.0, "ROUTE": 0.0, "STOP": 0.0}
        opt  = self.optimization_applied

        # A skipped call (cache hit) or a stopped loop avoids the WHOLE baseline.
        if opt is OptType.CACHE:
            comp["CACHE"] = self.cost_saved
            return comp
        if opt is OptType.STOP:
            comp["STOP"] = self.cost_saved
            return comp

        s       = MODEL_COSTS[BASELINE_MODEL]
        in_full = self.baseline_tokens_in
        in_act  = self.actual_tokens_in
        out     = self.actual_tokens_out

        # Compression: fewer input tokens, priced at the baseline input rate.
        comp["COMPRESS"] = max(0.0, (in_full - in_act)) / 1000 * s["input"]

        # Routing: same (compressed) tokens, baseline model -> cheaper model.
        if self.actual_model != BASELINE_MODEL:
            comp["ROUTE"] = (cost_of(BASELINE_MODEL, in_act, out)
                             - cost_of(self.actual_model, in_act, out))
        return comp

    # ── serialization ──────────────────────────────────────────────
    def to_dict(self) -> dict:
        return {
            "call_id":            self.call_id,
            "agent_id":           self.agent_id,
            "task_type":          self.task_type,
            "timestamp":          self.timestamp,
            "optimization":       self.optimization_applied.value,
            "optimization_detail": self.optimization_detail,
            "baseline_model":     self.baseline_model,
            "baseline_tokens_in": self.baseline_tokens_in,
            "baseline_cost":      round(self.baseline_cost, 6),
            "actual_model":       self.actual_model,
            "actual_tokens_in":   self.actual_tokens_in,
            "actual_tokens_out":  self.actual_tokens_out,
            "actual_cost":        round(self.actual_cost, 6),
            "tokens_saved":       self.tokens_saved,
            "cost_saved":         round(self.cost_saved, 6),
            "saved_pct":          round(self.saved_pct, 1),
            "baseline_latency_ms": round(self.baseline_latency_ms, 1),
            "actual_latency_ms":  round(self.latency_ms, 1),
            "latency_saved_ms":   round(self.latency_saved_ms, 1),
            "quality_score":      self.quality_score,
            "baseline_quality":   self.baseline_quality,
            "savings_components": {k: round(v, 6) for k, v in self.savings_components().items()},
        }

    def comparison_dict(self) -> dict:
        """Full record for data/comparisons/call_NNN.json (includes prompts)."""
        d = self.to_dict()
        d["baseline_prompt"] = self.baseline_prompt
        d["actual_prompt"]   = self.actual_prompt
        d["output_text"]     = self.output_text
        return d

    # ── factory ────────────────────────────────────────────────────
    @classmethod
    def create(
        cls, *,
        call_id: int, agent_id: str, task_type: str, timestamp: float,
        baseline_prompt: str, baseline_tokens_in: int, baseline_tokens_out: int,
        actual_prompt: str | None, actual_tokens_in: int, actual_tokens_out: int,
        actual_model: str, optimization_applied: OptType, optimization_detail: dict,
        quality_score: float | None = None, baseline_quality: float | None = None,
        output_text: str = "", latency_ms: float = 0.0, baseline_latency_ms: float = 0.0,
        required_model: str = BASELINE_MODEL,
    ) -> "TrackedCall":
        """Build a TrackedCall with consistent list-price costs so that the
        per-mechanism decomposition sums exactly to the total saving."""
        baseline_cost = cost_of(BASELINE_MODEL, baseline_tokens_in, baseline_tokens_out)
        if optimization_applied in (OptType.CACHE, OptType.STOP):
            actual_cost = 0.0
        else:
            actual_cost = cost_of(actual_model, actual_tokens_in, actual_tokens_out)

        # Latency: modeled analytically when the caller does not supply it, so
        # MOCK runs reproduce the time chart exactly. The baseline is always the
        # full, uncompressed prompt on Sonnet.
        if baseline_latency_ms <= 0:
            baseline_latency_ms = latency_of(
                BASELINE_MODEL, baseline_tokens_in, baseline_tokens_out)
        if latency_ms <= 0:
            if optimization_applied is OptType.CACHE:
                latency_ms = CACHE_LOOKUP_MS
            elif optimization_applied is OptType.STOP:
                latency_ms = 0.0   # caller passes the real loops-run time explicitly
            else:
                latency_ms = latency_of(
                    actual_model, actual_tokens_in, actual_tokens_out)
        return cls(
            call_id=call_id, agent_id=agent_id, task_type=task_type, timestamp=timestamp,
            baseline_prompt=baseline_prompt, baseline_tokens_in=baseline_tokens_in,
            baseline_tokens_out=baseline_tokens_out,
            baseline_model=BASELINE_MODEL, baseline_cost=baseline_cost,
            actual_prompt=actual_prompt, actual_tokens_in=actual_tokens_in,
            actual_tokens_out=actual_tokens_out, actual_model=actual_model,
            actual_cost=actual_cost,
            optimization_applied=optimization_applied, optimization_detail=optimization_detail,
            quality_score=quality_score, baseline_quality=baseline_quality,
            output_text=output_text, latency_ms=latency_ms,
            baseline_latency_ms=baseline_latency_ms, required_model=required_model,
        )
