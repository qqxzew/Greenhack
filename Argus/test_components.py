# test_components.py
"""
Fast component tests -- no real API calls needed.
Run: python test_components.py
"""

import numpy as np
import sys


def test_feature_extractor():
    from core.features import FeatureExtractor
    fe = FeatureExtractor()
    task = {"prompt": "Analyze the security implications of this design", "type": "analysis", "urgency": 0.8}
    x = fe.extract(task)
    assert x.shape == (8,), f"Expected dim=8, got {x.shape}"
    assert 0 <= x[2] <= 1, "Urgency out of range"
    print("  ✅ FeatureExtractor: dim=8, values in range")


def test_logistic_regression():
    from core.logistic import OnlineLogisticRegression
    lr = OnlineLogisticRegression(dim=8)
    x = np.abs(np.random.randn(8))   # positive features so updates push w consistently
    p0 = lr.predict(x)
    assert 0 <= p0 <= 1
    for _ in range(50):
        lr.update(x, quality=0.4)
    p1 = lr.predict(x)
    assert p1 > p0, f"LogReg should learn: p0={p0:.3f}, p1={p1:.3f}"
    print(f"  ✅ OnlineLogReg: learned complexity  {p0:.3f} → {p1:.3f}")


def test_linucb():
    from core.linucb import LinUCBRouter
    router = LinUCBRouter(["haiku", "sonnet"], context_dim=9, alpha=1.0)
    x = np.ones(9) * 0.3

    choices = [router.choose(x)[0] for _ in range(20)]
    assert "haiku" in choices and "sonnet" in choices, "Cold start should explore both"

    for _ in range(100):
        router.update("haiku",  x, quality=0.92, cost=0.0001)
        router.update("sonnet", x, quality=0.90, cost=0.005)

    choices_after = [router.choose(x)[0] for _ in range(20)]
    haiku_pct = choices_after.count("haiku") / 20
    assert haiku_pct >= 0.7, f"Expected haiku to dominate, got {haiku_pct:.0%}"
    print(f"  ✅ LinUCB: haiku chosen {haiku_pct:.0%} after learning (expected ≥70%)")


def test_semantic_cache():
    from core.cache import SemanticCache
    cache = SemanticCache()
    cache.put("What is the remote work policy?", '{"answer": "2 days/week"}', quality=0.9)
    r, sim = cache.get("What is the remote work policy?")
    assert r is not None, "Exact query should hit cache"
    r2, _ = cache.get("What is the expense policy?")
    assert r2 is None, "Different query should miss cache"
    print(f"  ✅ SemanticCache: hit on exact match (sim={sim:.3f}), miss on different query")


def test_sprt():
    from core.sprt import SPRTStopper

    stopper = SPRTStopper()
    decisions = []
    for _ in range(30):
        d = stopper.update(progress_delta=0.01)
        decisions.append(d)
        if d == "STOP_STUCK":
            break
    assert "STOP_STUCK" in decisions, "Should detect stuck agent within 30 steps"
    print(f"  ✅ SPRT: detected stuck agent at step {decisions.index('STOP_STUCK') + 1}")

    stopper2 = SPRTStopper()
    d2 = "CONTINUE"
    for _ in range(15):
        d2 = stopper2.update(progress_delta=0.20)
        if d2 != "CONTINUE":
            break
    assert d2 == "STOP_PROGRESSING", f"Expected PROGRESSING, got {d2}"
    print(f"  ✅ SPRT: confirmed healthy agent at step {stopper2.step}")


def test_cusum():
    from core.cusum import CUSUMDetector
    cusum = CUSUMDetector(mu0=1000, k=200, h=2000, warmup=5)

    alerts_normal = [cusum.update(1050) for _ in range(10)]
    assert not any(alerts_normal), "Normal usage should not alert"

    alerts_spike = [cusum.update(4000) for _ in range(10)]
    assert any(alerts_spike), "Token spike should trigger CUSUM alert"
    print(f"  ✅ CUSUM: no alert on normal usage, alert on spike")


def test_minhash():
    from core.minhash_dedup import MinHashDeduplicator
    # Short prompts share few word-trigrams, so use a lower threshold here.
    # Long prompts in production will routinely exceed 0.75 Jaccard.
    dedup = MinHashDeduplicator(threshold=0.3)

    result1 = {"response": "remote work allowed 2 days/week", "quality": 0.9}
    dedup.store_result(
        "task1",
        "What is the remote work policy at TechCorp for engineers",
        result1,
    )

    hit = dedup.check(
        "task2",
        "Tell me about the remote work policy at TechCorp for staff",
    )
    assert hit is not None, "Near-duplicate should be detected"

    miss = dedup.check("task3", "How do I submit expense reimbursement forms quarterly?")
    assert miss is None, "Unrelated query should not match"
    print(f"  ✅ MinHash: detected near-duplicate, passed unrelated query")


def test_hierarchical_router():
    from core.router import HierarchicalRouter
    router = HierarchicalRouter()

    simple_task = {"prompt": "Summarize this in 3 bullet points: ...", "type": "summarize", "urgency": 0.2}
    complex_task = {"prompt": "Analyze and critique the security architecture of this system, reason through each component", "type": "analysis", "urgency": 0.9}

    # Untrained LogReg returns sigmoid(0)=0.5 for everything. Feed it a
    # handful of supervised examples so the complexity head differentiates.
    x_simple  = router.extractor.extract(simple_task)
    x_complex = router.extractor.extract(complex_task)
    for _ in range(20):
        router.logistic.update(x_simple,  quality=0.92)   # quality high -> easy
        router.logistic.update(x_complex, quality=0.45)   # quality low  -> hard

    _, meta_s = router.choose(simple_task)
    _, meta_c = router.choose(complex_task)

    assert meta_s["complexity_score"] < meta_c["complexity_score"], \
        f"Simple task should have lower complexity score (got {meta_s['complexity_score']} vs {meta_c['complexity_score']})"
    print(f"  ✅ HierarchicalRouter: simple={meta_s['complexity_score']:.3f}, complex={meta_c['complexity_score']:.3f}")


def test_toon():
    from core.toon import (
        encode_event, decode_event,
        encode_stream, decode_stream,
        toon_savings_report,
    )

    event = {
        "ts":               1735000001,
        "agent_id":         "agent-hr",
        "model":            "claude-haiku-4-5",
        "task_type":        "summarize",
        "quality":          0.871,
        "cost":             0.000124,
        "tokens_total":     1150,
        "complexity_score": 0.213,
        "is_anomaly":       False,
        "sprt":             "CONTINUE",
    }

    # Encode
    line = encode_event(event)
    assert "|" in line,          "TOON record must use | separator"
    assert "agent" not in line,  "Full key names must not appear in records"
    assert "claude" not in line, "Full model names must not appear in records"
    assert "summarize" not in line, "Full task types must not appear in records"

    # Roundtrip
    recovered = decode_event(line)
    assert recovered["agent_id"]   == event["agent_id"]
    assert recovered["model"]      == event["model"]
    assert recovered["task_type"]  == event["task_type"]
    assert abs(recovered["quality"] - event["quality"]) < 0.001
    assert abs(recovered["cost"]    - event["cost"])    < 1e-6
    assert recovered["is_anomaly"] == event["is_anomaly"]

    # Stream roundtrip
    events_in  = [event] * 20
    toon_str   = encode_stream(events_in)
    events_out = decode_stream(toon_str)
    assert len(events_out) == 20, f"Expected 20 events, got {len(events_out)}"

    # Savings
    info = toon_savings_report([event] * 50)
    assert info["savings_pct"] > 50, \
        f"Expected >50% savings vs JSON, got {info['savings_pct']}%"

    print(f"  ✅ TOON: roundtrip OK | {info['savings_pct']}% smaller than JSON")
    print(f"     50 events: {info['json_approx_tokens']} tokens (JSON)"
          f" → {info['toon_approx_tokens']} tokens (TOON)")
    print(f"     Sample record: '{line}'")


def test_compression():
    from core.compression import ContextCompressor, ConversationPrompt, estimate_tokens

    conv = ConversationPrompt(
        system=("You are a helpful summarization assistant. Always be thorough, "
                "professional, and comprehensive. Maintain a warm and friendly tone "
                "at all times. Never use jargon. Always double-check your work."),
        history=[
            ("User", "Can you help me with the weekly report?"),
            ("Asst", "Of course! I'd be happy to help. What would you like to include? "
                     "I can cover sales, team updates, or project milestones."),
            ("User", "Focus on sales and team updates please."),
            ("Asst", "Understood! Sales rose 12% year over year, we made 3 new "
                     "engineering hires, and project Artemis is on track for August."),
        ],
        question="Now write the final weekly digest.",
    )
    res = ContextCompressor().compress(conv)

    assert res.after_tokens < res.before_tokens * 0.7, \
        f"Expected >30% token reduction, got ratio {res.ratio:.2f}"
    assert "Now write the final weekly digest." in res.after_text, \
        "Final question must be preserved verbatim"
    assert "12%" in res.after_text and "Artemis" in res.after_text, \
        "Key facts (numbers, project names) must survive compression"
    assert "I'd be happy" not in res.after_text, "Pleasantries should be dropped"
    print(f"  ✅ Compression: {res.before_tokens} → {res.after_tokens} tokens "
          f"({(1-res.ratio)*100:.0f}% smaller), facts + question preserved")


def test_tracked_savings():
    from core.tracking import TrackedCall, OptType, cost_of, BASELINE_MODEL

    # Compressed AND routed to haiku — both mechanisms credited.
    tc = TrackedCall.create(
        call_id=1, agent_id="summarizer", task_type="summarize", timestamp=0.0,
        baseline_prompt="SYSTEM: ...", baseline_tokens_in=3840, baseline_tokens_out=430,
        actual_prompt="SYSTEM: ...", actual_tokens_in=890, actual_tokens_out=430,
        actual_model="claude-haiku-4-5",
        optimization_applied=OptType.COMPRESS, optimization_detail={},
        quality_score=0.89, baseline_quality=0.91,
    )
    comps = tc.savings_components()
    assert abs(sum(comps.values()) - tc.cost_saved) < 1e-9, \
        f"Components {comps} must sum to cost_saved {tc.cost_saved}"
    assert comps["COMPRESS"] > 0 and comps["ROUTE"] > 0, "Both mechanisms should contribute"
    assert all(v >= 0 for v in comps.values()), "No negative component savings"

    # Cache hit — whole baseline avoided, attributed entirely to CACHE.
    hit = TrackedCall.create(
        call_id=2, agent_id="classifier", task_type="qa", timestamp=0.0,
        baseline_prompt="SYSTEM: ...", baseline_tokens_in=240, baseline_tokens_out=45,
        actual_prompt=None, actual_tokens_in=0, actual_tokens_out=45,
        actual_model="cache", optimization_applied=OptType.CACHE, optimization_detail={},
        quality_score=0.95, baseline_quality=0.95,
    )
    hcomps = hit.savings_components()
    assert hit.actual_cost == 0.0 and abs(hcomps["CACHE"] - hit.baseline_cost) < 1e-12, \
        "Cache hit should save the full baseline cost"
    print(f"  ✅ TrackedCall: COMPRESS+ROUTE decompose exactly "
          f"(${comps['COMPRESS']:.4f}+${comps['ROUTE']:.4f}=${tc.cost_saved:.4f}); "
          f"cache hit = 100% saved")


if __name__ == "__main__":
    print("Running component tests...\n")
    tests = [
        test_feature_extractor,
        test_logistic_regression,
        test_linucb,
        test_semantic_cache,
        test_sprt,
        test_cusum,
        test_minhash,
        test_hierarchical_router,
        test_toon,
        test_compression,
        test_tracked_savings,
    ]
    passed = 0
    for test in tests:
        try:
            test()
            passed += 1
        except Exception as e:
            print(f"  ❌ {test.__name__}: {e}")

    print(f"\n{passed}/{len(tests)} tests passed")
    sys.exit(0 if passed == len(tests) else 1)
