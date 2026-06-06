# core/pipeline.py

import time
import uuid
from core.router        import HierarchicalRouter, MODEL_COSTS
from core.cache         import SemanticCache
from core.minhash_dedup import MinHashDeduplicator
from core.sprt          import SPRTStopper
from core.cusum         import CUSUMDetector
from core.budget        import HierarchicalBudget
from core.prefix_cache  import PrefixCacheManager
from core.compression   import ContextCompressor, estimate_tokens
from core.tracking      import TrackedCall, OptType, BASELINE_MODEL, cost_of
from storage.event_log  import EventLogger


class OptimizationPipeline:
    """
    Main pipeline. Every LLM call in the agent system goes through here.

    Layers applied in order (each can short-circuit the rest):

    1. MinHash Deduplication
    2. Semantic Cache
    3. Budget Check
    4. HierarchicalRouter (LogReg + LinUCB)
    5. [LLM call happens here -- external]
    6. SPRT Stopper
    7. CUSUM Detector
    8. Update all components
    9. EventLogger
    """

    def __init__(self, daily_budget: float = 100_000):
        self.router    = HierarchicalRouter()
        self.cache     = SemanticCache()
        self.dedup     = MinHashDeduplicator()
        self.budget    = HierarchicalBudget(daily_budget)
        self.prefix    = PrefixCacheManager()
        self.logger    = EventLogger()
        self.stoppers: dict[str, SPRTStopper]    = {}
        self.cusums:   dict[str, CUSUMDetector]  = {}

    # ----- pre-call -----

    def pre_call(self, agent_id: str, task: dict) -> dict:
        task_id = task.get("id", str(uuid.uuid4()))
        prompt  = task.get("prompt", "")

        # Layer 1: MinHash deduplication
        dup_result = self.dedup.check(task_id, prompt)
        if dup_result is not None:
            return {
                "action":          "cache_hit",
                "source":          "dedup",
                "cached_response": dup_result,
                "tokens_saved":    dup_result.get("tokens_total", 1200),
                "task_id":         task_id,
            }

        # Layer 2: Semantic cache
        cached_text, similarity = self.cache.get(prompt)
        if cached_text is not None:
            return {
                "action":          "cache_hit",
                "source":          "semantic",
                "cached_response": {"response": cached_text, "similarity": similarity},
                "tokens_saved":    1200,
                "task_id":         task_id,
            }

        # Track prefix reuse for analytics
        self.prefix.observe(prompt)

        # Layer 3: Budget check (reserve, not yet spent)
        budget_action = self.budget.consume(agent_id, 0)

        # Layer 4: Route to model
        model, meta = self.router.choose(task)

        if budget_action == "block":
            return {"action": "blocked", "reason": "budget_exhausted", "task_id": task_id}
        elif budget_action == "degrade":
            model = "claude-haiku-4-5"

        return {
            "action":  "call_llm",
            "model":   model,
            "meta":    meta,
            "task_id": task_id,
        }

    # ----- post-call -----

    def post_call(
        self,
        agent_id:   str,
        task:       dict,
        decision:   dict,
        llm_result: dict,
    ) -> dict:

        model        = decision.get("model", "claude-haiku-4-5")
        meta         = decision.get("meta", {})
        quality      = llm_result.get("quality",      0.8)
        cost         = llm_result.get("cost",         0.001)
        tokens_total = llm_result.get("tokens_total", 1200)
        response     = llm_result.get("response",     "")

        update_info = self.router.update(model, task, meta, quality, cost)

        self.budget.consume(agent_id, tokens_total)

        self.cache.put(task.get("prompt", ""), response, quality)

        self.dedup.store_result(
            decision.get("task_id", ""),
            task.get("prompt", ""),
            llm_result,
        )

        if agent_id not in self.cusums:
            self.cusums[agent_id] = CUSUMDetector()
        is_anomaly = self.cusums[agent_id].update(tokens_total)

        if agent_id not in self.stoppers:
            self.stoppers[agent_id] = SPRTStopper()
        progress_delta = quality * 0.5 + (1 - cost / 0.05) * 0.5
        sprt_decision = self.stoppers[agent_id].update(progress_delta)

        event = {
            "agent_id":         agent_id,
            "task_id":          decision.get("task_id", ""),
            "task_type":        task.get("type", "unknown"),
            "model":            model,
            "quality":          quality,
            "cost":             cost,
            "tokens_total":     tokens_total,
            "is_anomaly":       is_anomaly,
            "sprt":             sprt_decision,
            "complexity_score": meta.get("complexity_score", 0.5),
            "reward":           update_info.get("reward", 0.0),
        }
        self.logger.log(event)

        return {
            **event,
            "alert_anomaly": is_anomaly,
            "alert_sprt":    sprt_decision == "STOP_STUCK",
        }

    # ----- tracked path (Post-Run Optimization Report) -----

    def process_tracked(
        self,
        task:        dict,
        responder,
        call_id:     int,
        compressor:  ContextCompressor | None = None,
        complexity_threshold: float = 0.5,
        tiers:       list[tuple[float, str]] | None = None,
    ) -> TrackedCall:
        """
        Run one task and return a fully-accounted TrackedCall.

        This is a SEPARATE code path from pre_call/post_call. It reuses the same
        mechanisms (compression, routing, semantic cache, SPRT) but records the
        baseline-vs-actual decomposition the report needs. `responder(model,
        prompt_text, task)` performs the actual generation (mock or live) and
        returns {tokens_in?, tokens_out, quality, output_text, latency_ms}.
        """
        import time

        compressor = compressor or ContextCompressor()
        if not hasattr(self, "_tracked_prompts"):
            self._tracked_prompts: dict[str, dict] = {}

        agent_id    = task.get("agent_id", "agent")
        task_type   = task.get("type", "qa")
        conv        = task["conversation"]
        full_prompt = conv.render()
        full_in     = estimate_tokens(full_prompt)
        ts          = time.time()

        # ── 1. Stuck-loop detection (SPRT actually stops the loop) ──
        if task.get("is_loop"):
            stopper = self.stoppers.setdefault(agent_id, SPRTStopper())
            iters, decision = 0, "CONTINUE"
            for _ in range(task.get("loop_expected", 11)):
                iters += 1
                decision = stopper.update(progress_delta=0.01)
                if decision == "STOP_STUCK":
                    break
            expected   = task.get("loop_expected", 11)
            prevented  = max(1, expected - iters)
            per_in     = task.get("loop_call_tokens_in", full_in)
            per_out    = task.get("loop_call_tokens_out", 200)
            return TrackedCall.create(
                call_id=call_id, agent_id=agent_id, task_type=task_type, timestamp=ts,
                baseline_prompt=full_prompt,
                baseline_tokens_in=per_in * prevented,
                baseline_tokens_out=per_out * prevented,
                actual_prompt=None, actual_tokens_in=0, actual_tokens_out=0,
                actual_model="cache", optimization_applied=OptType.STOP,
                optimization_detail={
                    "loops_run":       iters,
                    "loops_prevented": prevented,
                    "reason":          "SPRT STOP_STUCK — no new tool_calls",
                },
                quality_score=None, baseline_quality=None, output_text="",
                required_model=task.get("required_model", BASELINE_MODEL),
            )

        # ── 2. Exact-repeat semantic cache HIT (LLM never called) ──
        prior = self._tracked_prompts.get(full_prompt)
        if prior is not None:
            self.cache.get(full_prompt)   # let the real cache record the hit
            out_tok = prior["tokens_out"]
            return TrackedCall.create(
                call_id=call_id, agent_id=agent_id, task_type=task_type, timestamp=ts,
                baseline_prompt=full_prompt, baseline_tokens_in=full_in,
                baseline_tokens_out=out_tok,
                actual_prompt=None, actual_tokens_in=0, actual_tokens_out=out_tok,
                actual_model="cache", optimization_applied=OptType.CACHE,
                optimization_detail={
                    "cache_similarity": 1.0,
                    "matched_call":     prior["call_id"],
                },
                quality_score=prior["quality"], baseline_quality=prior["quality"],
                output_text=prior["output_text"],
                required_model=task.get("required_model", BASELINE_MODEL),
            )

        # ── 3. Compression ──
        compressed, ratio, dropped, facts = False, 1.0, 0, 0
        actual_prompt, actual_in = full_prompt, full_in
        sys_before_tok = sys_after_tok = 0
        if task.get("allow_compress", True):
            cres = compressor.compress(conv)
            sys_before_tok = estimate_tokens(cres.system_before)
            sys_after_tok  = estimate_tokens(cres.system_after)
            if cres.after_tokens < cres.before_tokens * 0.98:
                compressed    = True
                actual_prompt = cres.after_text
                actual_in     = cres.after_tokens
                ratio, dropped, facts = round(cres.ratio, 3), cres.turns_dropped, cres.facts_kept

        # ── 4. Routing (deterministic by task complexity) ──
        # Two modes:
        #   • legacy 2-tier: complexity < threshold -> haiku, else Sonnet baseline.
        #   • N-tier (investor): `tiers` is a list of (upper_bound, model) sorted
        #     ascending; the first tier whose upper_bound > complexity wins. This
        #     can route DOWN to haiku (cheap, easy work) or UP to opus (expensive,
        #     genuinely hard work) — the report shows both directions honestly.
        complexity = float(task.get("complexity", 0.5))
        if tiers:
            model = tiers[-1][1]
            for upper, m in tiers:
                if complexity < upper:
                    model = m
                    break
        else:
            model = "claude-haiku-4-5" if complexity < complexity_threshold else BASELINE_MODEL
        routed = model != BASELINE_MODEL

        # ── 5. Generation (mock or live) ──
        r          = responder(model, actual_prompt, task)
        actual_in  = int(r.get("tokens_in", actual_in))
        actual_out = int(r.get("tokens_out", estimate_tokens(r.get("output_text", "")) or 200))
        quality    = r.get("quality")
        out_text   = r.get("output_text", "")
        latency_ms = float(r.get("latency_ms", 0.0))

        if compressed:
            opt = OptType.COMPRESS
        elif routed:
            opt = OptType.ROUTE
        else:
            opt = OptType.NONE

        detail = {
            "complexity_score":  round(complexity, 3),
            "threshold":         complexity_threshold,
            "tiers":             [[u, m] for u, m in tiers] if tiers else None,
            "routed_to":         model,
            "would_have_been":   BASELINE_MODEL,
            "compression_ratio": ratio,
            "turns_dropped":     dropped,
            "facts_kept":        facts,
            "system_before_tokens": sys_before_tok,
            "system_after_tokens":  sys_after_tok,
        }

        tc = TrackedCall.create(
            call_id=call_id, agent_id=agent_id, task_type=task_type, timestamp=ts,
            baseline_prompt=full_prompt, baseline_tokens_in=full_in,
            baseline_tokens_out=actual_out,
            actual_prompt=actual_prompt, actual_tokens_in=actual_in,
            actual_tokens_out=actual_out, actual_model=model,
            optimization_applied=opt, optimization_detail=detail,
            quality_score=quality, baseline_quality=task.get("baseline_quality"),
            output_text=out_text, latency_ms=latency_ms,
            required_model=task.get("required_model", model),
        )

        # warm the cache so an identical later prompt is a hit
        self.cache.put(full_prompt, out_text, quality or 0.8)
        self._tracked_prompts[full_prompt] = {
            "call_id": call_id, "tokens_out": actual_out,
            "quality": quality, "output_text": out_text,
        }
        return tc

    # ----- global state -----

    def get_full_state(self) -> dict:
        return {
            "router":  self.router.get_state(),
            "cache":   self.cache.stats(),
            "dedup":   self.dedup.stats(),
            "budget":  self.budget.state(),
            "prefix":  self.prefix.stats(),
            "events":  self.logger.recent(100),
            "cusum":   {aid: d.state() for aid, d in self.cusums.items()},
        }
