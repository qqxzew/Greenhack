# agents/base_agent.py

import anthropic
import time
import uuid
import asyncio
from dataclasses import dataclass
from core.pipeline import OptimizationPipeline


_client: anthropic.Anthropic | None = None


def _get_client() -> anthropic.Anthropic:
    """Lazy client initializer so importing this module without an API key
    does not crash (e.g. when only running component tests)."""
    global _client
    if _client is None:
        _client = anthropic.Anthropic()
    return _client


RESPONSE_SCHEMAS = {
    "summarize":   '{"points": ["str", "str", "str"], "sentiment": "positive|neutral|negative"}',
    "qa":          '{"answer": "str", "confidence": 0.0}',
    "translation": '{"translated": "str", "language": "str"}',
    "code_review": '{"issues": [{"line": 0, "severity": "low|medium|high", "msg": "str"}], "score": 0.0}',
    "analysis":    '{"recommendation": "str", "pros": ["str"], "cons": ["str"], "confidence": 0.0}',
    "generation":  '{"content": "str", "word_count": 0}',
}


@dataclass
class CallResult:
    task_id:      str
    model:        str
    response:     str
    tokens_in:    int
    tokens_out:   int
    tokens_total: int
    cost:         float
    latency:      float
    quality:      float
    source:       str   # "llm" | "semantic_cache" | "dedup_cache" | "blocked" | "error"


class BaseAgent:
    """
    Agent that makes real Claude API calls and routes through OptimizationPipeline.

    Key integrations with the Claude ecosystem:
      1. cache_control on system prompt  -> Anthropic native prefix caching
      2. Structured JSON output          -> forces compact responses
      3. claude-haiku-4-5 as judge       -> quality scoring (see eval/judge.py)
      4. Async parallel calls            -> asyncio.gather for concurrent agents
    """

    MODEL_COSTS = {
        "claude-haiku-4-5":  {"input": 0.00025, "output": 0.00125},
        "claude-sonnet-4-5": {"input": 0.003,   "output": 0.015},
    }

    def __init__(
        self,
        agent_id:         str,
        persona:          str,
        pipeline:         OptimizationPipeline,
        use_optimization: bool = True,
    ):
        self.agent_id         = agent_id
        self.persona          = persona
        self.pipeline         = pipeline
        self.use_optimization = use_optimization
        self.results: list[CallResult] = []

    # ----- core call with prefix caching -----

    def call_claude(self, model: str, task: dict) -> CallResult:
        """
        Real Claude API call. Uses cache_control on the system blocks so
        Anthropic's infrastructure caches the prompt prefix across repeated
        calls from this agent.
        """
        task_type = task.get("type", "qa")
        schema    = RESPONSE_SCHEMAS.get(task_type, '{"response": "str"}')

        system_blocks = [
            {
                "type": "text",
                "text": self.persona,
                "cache_control": {"type": "ephemeral"},
            },
            {
                "type": "text",
                "text": (
                    f"You MUST respond ONLY with valid JSON matching this schema:\n"
                    f"{schema}\n"
                    f"No explanation. No markdown. No preamble. Pure JSON only."
                ),
                "cache_control": {"type": "ephemeral"},
            },
        ]

        user_message = task.get("prompt", "")

        t0 = time.time()
        try:
            response = _get_client().messages.create(
                model=model,
                max_tokens=512,
                system=system_blocks,
                messages=[{"role": "user", "content": user_message}],
            )
        except anthropic.APIError:
            return CallResult(
                task_id=task.get("id", ""), model=model,
                response="{}",
                tokens_in=0, tokens_out=0, tokens_total=0,
                cost=0.0, latency=0.0, quality=0.0, source="error",
            )

        latency   = time.time() - t0
        tok_in    = response.usage.input_tokens
        tok_out   = response.usage.output_tokens
        tok_cache = getattr(response.usage, "cache_read_input_tokens", 0) or 0
        content   = response.content[0].text

        costs = self.MODEL_COSTS.get(model, self.MODEL_COSTS["claude-haiku-4-5"])
        cost = (
            (tok_in - tok_cache) / 1000 * costs["input"]
            + tok_cache           / 1000 * costs["input"] * 0.1
            + tok_out             / 1000 * costs["output"]
        )

        return CallResult(
            task_id=task.get("id", str(uuid.uuid4())[:8]),
            model=model,
            response=content,
            tokens_in=tok_in,
            tokens_out=tok_out,
            tokens_total=tok_in + tok_out,
            cost=round(cost, 7),
            latency=round(latency, 3),
            quality=0.0,
            source="llm",
        )

    # ----- main task execution through pipeline -----

    def execute(self, task: dict) -> CallResult:
        """Run one task through the full optimization pipeline."""
        # Lazy import so test_components.py (which only touches core/) can
        # run without the eval module's Anthropic client being constructed.
        from eval.judge import score_quality

        if not self.use_optimization:
            result = self.call_claude("claude-sonnet-4-5", task)
            result.quality = score_quality(task, result.response)
            self.results.append(result)
            return result

        decision = self.pipeline.pre_call(self.agent_id, task)

        if decision["action"] == "cache_hit":
            cached = decision.get("cached_response", {})
            if isinstance(cached, dict):
                resp_text = str(cached.get("response", cached))
                cached_quality = float(cached.get("quality", 0.8))
            else:
                resp_text = str(cached)
                cached_quality = 0.8
            cr = CallResult(
                task_id=task.get("id", ""),
                model="cache",
                response=resp_text,
                tokens_in=0, tokens_out=0, tokens_total=0,
                cost=0.0, latency=0.0,
                quality=cached_quality,
                source=decision.get("source", "cache"),
            )
            self.results.append(cr)
            return cr

        if decision["action"] == "blocked":
            return CallResult(
                task_id=task.get("id", ""), model="blocked",
                response="{}", tokens_in=0, tokens_out=0, tokens_total=0,
                cost=0.0, latency=0.0, quality=0.0, source="blocked",
            )

        model  = decision.get("model", "claude-haiku-4-5")
        result = self.call_claude(model, task)

        result.quality = score_quality(task, result.response)

        self.pipeline.post_call(
            self.agent_id, task, decision,
            {
                "response":     result.response,
                "quality":      result.quality,
                "tokens_in":    result.tokens_in,
                "tokens_out":   result.tokens_out,
                "tokens_total": result.tokens_total,
                "cost":         result.cost,
                "latency":      result.latency,
            },
        )

        self.results.append(result)
        return result

    # ----- async runner -----

    async def run_tasks_async(self, tasks: list[dict]) -> list[CallResult]:
        """Run a list of tasks sequentially (one agent = sequential)."""
        results = []
        for task in tasks:
            result = self.execute(task)
            results.append(result)
            await asyncio.sleep(0.1)
        return results
