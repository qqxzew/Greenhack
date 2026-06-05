# simulation/conversation_tasks.py
"""
Verbose, multi-turn conversation tasks for the Post-Run Optimization Report.

These are NOT the short synthetic tasks used by simulation/task_generator.py.
They are deliberately verbose (long boilerplate system prompts + chatty history
with real facts buried inside) so the context compressor has something genuine
to cut — which makes the BEFORE/AFTER prompt diff in the report real.

The set is hand-authored and deterministic (no randomness), so the generated
report is reproducible without spending any API money.

Each task is a dict:
    {
      "id", "agent_id", "type", "complexity", "urgency",
      "conversation": ConversationPrompt,
      "prompt":   <rendered full prompt>,   # used for cache/dedup keys
      "question": <final user question>,
      "allow_compress": bool,
      "out_tokens": int,
      "expected_quality": float,
      "baseline_quality": float,
      # optional loop fields:
      "is_loop", "loop_expected", "loop_call_tokens_in", "loop_call_tokens_out",
      "sample_output",
    }
"""

from core.compression import ConversationPrompt, estimate_tokens


# ── reusable verbose blocks ────────────────────────────────────────
SUMMARIZER_SYSTEM = (
    "You are a helpful summarization assistant. Your job is to summarize weekly "
    "digests for enterprise clients. Always be thorough, professional, and "
    "comprehensive in your responses. Make sure to maintain a warm and friendly "
    "tone at all times. Never use jargon the client might not understand. Always "
    "double-check your work before sending. Remember that the client is paying a "
    "premium for this service and expects excellence. Be polite and courteous."
)

ANALYST_SYSTEM = (
    "You are a meticulous financial analysis assistant supporting the corporate "
    "finance team. You should always be careful, rigorous, and precise. Take your "
    "time and think step by step. Be respectful and professional in all answers. "
    "Avoid speculation and only state what the data supports. Always show your "
    "reasoning clearly so reviewers can follow along."
)

SUPPORT_SYSTEM = (
    "You are a senior customer-support resolution specialist. You must be empathetic, "
    "patient, and exceptionally thorough. Always acknowledge the customer's feelings "
    "first. Never dismiss a complaint. Escalate appropriately. Maintain a calm and "
    "reassuring tone. The company's reputation depends on your professionalism."
)

CLASSIFIER_SYSTEM = "Intent classifier. Return one label from the allowed set."


def _digest_history(sales, hires, project, deadline):
    return [
        ("User", "Hey, can you help me put together the weekly report?"),
        ("Asst", "Of course! I'd be happy to help with that. What would you like me "
                 "to include in the weekly report? I can cover sales figures, team "
                 "updates, project milestones, customer feedback, or any other areas "
                 "you care about. Just let me know what matters most this week."),
        ("User", "Let's focus on sales and team updates please, keep it tight."),
        ("Asst", "Understood! I'll focus on sales and team updates and keep it concise. "
                 f"Here is what I have on record: sales rose {sales} year over year, "
                 f"we made {hires} new engineering hires this quarter, and project "
                 f"{project} is on track for the {deadline} deadline. Does that match "
                 "your notes?"),
        ("User", "Yes that's right. Anything else you remember from our chats?"),
        ("Asst", "Sure thing! Earlier you also mentioned the marketing budget was "
                 "approved and the customer churn target is 4%. I hope this is helpful. "
                 "Let me know if you'd like me to add anything else at all."),
    ]


def _make_digest_task(call_no, sales, hires, project, deadline, complexity=0.31):
    conv = ConversationPrompt(
        system=SUMMARIZER_SYSTEM,
        history=_digest_history(sales, hires, project, deadline),
        question="Now write the final weekly digest.",
    )
    return {
        "id":               f"digest-{call_no}",
        "agent_id":         "summarizer",
        "type":             "summarize",
        "complexity":       complexity,
        "urgency":          0.3,
        "conversation":     conv,
        "prompt":           conv.render(),
        "question":         conv.question,
        "allow_compress":   True,
        "out_tokens":       430,
        "expected_quality": 0.91,
        "baseline_quality": 0.93,
        "sample_output":    '{"points":["Sales up","3 hires","Artemis on track"],"sentiment":"positive"}',
    }


def _make_analysis_task(call_no, region, growth, complexity=0.34):
    history = [
        ("User", "I need help analyzing this quarter's regional performance."),
        ("Asst", "I'd be happy to help you analyze the regional performance. Could you "
                 "tell me which regions and metrics you care about? I can look at "
                 "revenue, margin, headcount, or growth rates."),
        ("User", "Just summarize the headline numbers for now."),
        ("Asst", f"Certainly. For the {region} region, revenue grew {growth} quarter "
                 "over quarter, margin held at 22%, and we onboarded 5 new enterprise "
                 "accounts. The pipeline for next quarter looks healthy at $1.2M."),
        ("User", "Great, thanks. That all sounds correct to me."),
        ("Asst", "You're welcome! I'm glad that lines up. Let me know if you'd like me "
                 "to break any of those numbers down further or add commentary."),
    ]
    conv = ConversationPrompt(
        system=ANALYST_SYSTEM,
        history=history,
        question="Write a 3-bullet executive summary of the quarter.",
    )
    return {
        "id":               f"analysis-{call_no}",
        "agent_id":         "analyst",
        "type":             "analysis",
        "complexity":       complexity,
        "urgency":          0.4,
        "conversation":     conv,
        "prompt":           conv.render(),
        "question":         conv.question,
        "allow_compress":   True,
        "out_tokens":       360,
        "expected_quality": 0.90,
        "baseline_quality": 0.92,
        "sample_output":    '{"recommendation":"hold","pros":["growth"],"cons":["margin"],"confidence":0.8}',
    }


def _make_cache_task(call_no, query):
    """Short classifier task — used to demonstrate a semantic-cache HIT when the
    identical query repeats later in the run."""
    conv = ConversationPrompt(system=CLASSIFIER_SYSTEM, history=[], question=query)
    return {
        "id":               f"intent-{call_no}",
        "agent_id":         "classifier",
        "type":             "qa",
        "complexity":       0.18,
        "urgency":          0.5,
        "conversation":     conv,
        "prompt":           conv.render(),
        "question":         query,
        "allow_compress":   False,          # already tiny
        "out_tokens":       45,
        "expected_quality": 0.95,
        "baseline_quality": 0.95,
        "sample_output":    '{"answer":"balance_inquiry","confidence":0.97}',
    }


def _make_complaint_task(call_no, complexity=0.83):
    """High-complexity task: routing must KEEP Sonnet and the prompt passes
    through unchanged (no compression)."""
    history = [
        ("User", "This is the third time I've contacted you about a double charge."),
        ("Asst", "I'm truly sorry for the frustration this has caused. A double charge "
                 "is not acceptable and I want to make this right. Let me pull up your "
                 "account history so I fully understand what happened."),
        ("User", "I was charged $129 twice on May 3rd and nobody has fixed it."),
    ]
    conv = ConversationPrompt(
        system=SUPPORT_SYSTEM,
        history=history,
        question="Draft a resolution that refunds the duplicate charge and offers a goodwill credit.",
    )
    return {
        "id":               f"complaint-{call_no}",
        "agent_id":         "responder",
        "type":             "generation",
        "complexity":       complexity,
        "urgency":          0.9,
        "conversation":     conv,
        "prompt":           conv.render(),
        "question":         conv.question,
        "allow_compress":   False,          # full context required
        "out_tokens":       480,
        "expected_quality": 0.94,
        "baseline_quality": 0.94,
        "sample_output":    '{"content":"Refund issued + $20 credit","word_count":160}',
    }


def _make_loop_task(call_no):
    """A stuck agent that keeps re-calling without progress; SPRT forces a stop."""
    conv = ConversationPrompt(
        system="You are a pipeline processing agent. Continue until the task is done.",
        history=[("Asst", "Retrying the same step again with no new tool calls.")] * 3,
        question="Process the next record.",
    )
    return {
        "id":                   f"loop-{call_no}",
        "agent_id":             "pipeline-processor-3",
        "type":                 "qa",
        "complexity":           0.5,
        "urgency":              0.5,
        "conversation":         conv,
        "prompt":               conv.render(),
        "question":             conv.question,
        "allow_compress":       False,
        "is_loop":              True,
        "loop_expected":        11,         # would have looped 11 times
        "loop_call_tokens_in":  420,
        "loop_call_tokens_out": 180,
        "out_tokens":           0,
        "expected_quality":     0.0,
        "baseline_quality":     0.0,
        "sample_output":        "",
    }


def build_conversation_tasks() -> list[dict]:
    """Deterministic ~20-task pipeline for the report demo."""
    tasks: list[dict] = []

    # Compression-heavy digests (route -> haiku).
    tasks.append(_make_digest_task(1, "+12%", "3",  "Artemis",  "August"))
    tasks.append(_make_digest_task(2, "+8%",  "2",  "Borealis", "September"))
    tasks.append(_make_analysis_task(3, "EMEA", "+15%"))
    tasks.append(_make_digest_task(4, "+21%", "5",  "Cygnus",   "October"))

    # First occurrences of cacheable classifier queries.
    tasks.append(_make_cache_task(5, 'Classify the intent of: "What\'s my account balance?"'))
    tasks.append(_make_cache_task(6, 'Classify the intent of: "How do I reset my password?"'))

    tasks.append(_make_analysis_task(7, "APAC", "+9%"))
    tasks.append(_make_digest_task(8, "+5%",  "1",  "Draco",    "November"))

    # High-complexity pass-through (keep Sonnet, no compression).
    tasks.append(_make_complaint_task(9))

    # Repeat of an earlier classifier query -> semantic cache HIT (LLM skipped).
    tasks.append(_make_cache_task(10, 'Classify the intent of: "What\'s my account balance?"'))

    tasks.append(_make_digest_task(11, "+18%", "4", "Eridanus", "December"))
    tasks.append(_make_analysis_task(12, "LATAM", "+11%"))

    # Stuck loop -> SPRT forced stop.
    tasks.append(_make_loop_task(13))

    tasks.append(_make_digest_task(14, "+7%",  "2", "Fornax",   "January"))
    tasks.append(_make_complaint_task(15, complexity=0.79))

    # Second repeat of the password query -> another cache HIT.
    tasks.append(_make_cache_task(16, 'Classify the intent of: "How do I reset my password?"'))

    tasks.append(_make_digest_task(17, "+14%", "3", "Gemini",   "February"))
    tasks.append(_make_analysis_task(18, "NA",  "+6%"))
    tasks.append(_make_digest_task(19, "+9%",  "2", "Hydra",    "March"))
    tasks.append(_make_analysis_task(20, "EMEA", "+13%", complexity=0.33))

    return tasks


def mock_responder(model: str, prompt_text: str, task: dict) -> dict:
    """Deterministic stand-in for a real LLM call (no API).

    Returns the fields process_tracked() needs: token counts, a quality score,
    output text, and latency. Quality dips slightly when a hard task is routed
    to the small model — exactly the trade-off the report is meant to surface.
    """
    out_tokens = task.get("out_tokens", 220)
    base_q     = task.get("expected_quality", 0.9)
    if model == "claude-haiku-4-5" and task.get("complexity", 0.5) > 0.5:
        quality = max(0.0, base_q - 0.08)
    else:
        quality = base_q
    return {
        "tokens_in":   estimate_tokens(prompt_text),
        "tokens_out":  out_tokens,
        "quality":     round(quality, 3),
        "output_text": task.get("sample_output", '{"ok": true}'),
        "latency_ms":  30.0,
    }
