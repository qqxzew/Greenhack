# simulation/dataset.py
"""
Configurable synthetic workload generator for the Investor Report.

Everything in this file is parametrized and fully deterministic (seeded), so a
reader can see EXACTLY how the data was produced and reproduce it byte-for-byte.
There is no hidden randomness and no API spend in the default (mock) mode.

What it produces
----------------
A list of agent tasks spanning four difficulty bands. Each band maps to the
model an honest router *should* pick:

    band      complexity range     required model        why
    --------  -------------------  --------------------  -----------------------
    simple    0.05 – 0.28          claude-haiku-4-5      cheap, mechanical work
    medium    0.38 – 0.60          claude-sonnet-4-5     standard reasoning
    hard      0.60 – 0.69          claude-sonnet-4-5     same model, deeper think
    expert    0.74 – 0.97          claude-opus-4-7       frontier-only reasoning

On top of the band mix it injects a controllable rate of:
  • cache repeats  — a task whose prompt exactly repeats an earlier one
                     (the optimizer serves it from cache, skipping the LLM);
  • stuck loops    — an agent that re-calls with no progress
                     (SPRT force-stops it).

The point of the spread is to exercise the optimizer in BOTH directions:
route easy work DOWN to Haiku (large savings) and genuinely hard work UP to
Opus (a deliberate quality investment). The report shows both honestly.

Quality model
-------------
A model at or above a task's required capability tier delivers the task's
ceiling quality; below it, quality degrades by `_UNDERPOWER_PENALTY` per missing
tier. This is what makes the all-Sonnet baseline *lose quality* on expert tasks
(Sonnet < Opus) while Argus keeps it by routing up.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from core.compression import ConversationPrompt, estimate_tokens
from core.tracking     import BASELINE_MODEL, capability_of


# ── routing policy (the actual thresholds the pipeline uses) ────────────
HAIKU  = "claude-haiku-4-5"
SONNET = "claude-sonnet-4-5"
OPUS   = "claude-opus-4-7"

# (upper_bound, model): first tier whose bound > complexity wins.
ROUTING_TIERS: list[tuple[float, str]] = [
    (0.33, HAIKU),
    (0.70, SONNET),
    (float("inf"), OPUS),
]

# Difficulty bands: complexity range + the model genuinely required.
BANDS = {
    "simple": {"range": (0.05, 0.28), "required": HAIKU,  "base_out": 120, "reason_out":   0},
    "medium": {"range": (0.38, 0.60), "required": SONNET, "base_out": 320, "reason_out":  70},
    "hard":   {"range": (0.60, 0.69), "required": SONNET, "base_out": 470, "reason_out": 230},
    "expert": {"range": (0.74, 0.97), "required": OPUS,   "base_out": 640, "reason_out": 430},
}

_UNDERPOWER_PENALTY = 0.13   # quality lost per missing capability tier


def required_model_for(complexity: float) -> str:
    for band in BANDS.values():
        lo, hi = band["range"]
        if lo <= complexity <= hi:
            return band["required"]
    # complexity in a gap between bands -> fall back to the routed tier
    for upper, m in ROUTING_TIERS:
        if complexity < upper:
            return m
    return OPUS


def quality_for(model: str, required: str, ceiling: float) -> float:
    """Quality `model` delivers on a task that requires `required` capability."""
    gap = capability_of(required) - capability_of(model)
    if gap <= 0:
        return round(ceiling, 3)
    return round(max(0.30, ceiling - _UNDERPOWER_PENALTY * gap), 3)


# ── configuration ──────────────────────────────────────────────────────
_DIFFICULTY_PRESETS = {
    # band weights for the UNIQUE (non-cache, non-loop) tasks
    "easy":     {"simple": 0.60, "medium": 0.28, "hard": 0.08, "expert": 0.04},
    "balanced": {"simple": 0.42, "medium": 0.30, "hard": 0.16, "expert": 0.12},
    "hard":     {"simple": 0.25, "medium": 0.30, "hard": 0.25, "expert": 0.20},
    "frontier": {"simple": 0.15, "medium": 0.25, "hard": 0.28, "expert": 0.32},
}


@dataclass
class GenerationConfig:
    """Every knob that shapes the synthetic workload. Defaults reproduce the
    headline 300-task run. Pass `difficulty` to pick a band-weight preset, or
    set `band_weights` directly for full control."""

    n_tasks:               int   = 300
    seed:                  int   = 7
    difficulty:            str   = "balanced"           # easy | balanced | hard | frontier
    band_weights:          dict  | None = None          # overrides the preset if given
    strong_model_fraction: float | None = None          # overrides the expert band share
    reasoning_depth:       float = 1.0                   # scales reasoning tokens on hard+expert
    cache_hit_rate:        float = 0.12                  # share of tasks that are exact repeats
    loop_rate:             float = 0.04                  # share that are stuck loops (SPRT stop)
    compressible_fraction: float = 0.65                  # share of verbose tasks allowed to compress
    ceiling_quality:       float = 0.93                  # quality a correctly-routed task reaches

    def resolved_band_weights(self) -> dict:
        weights = dict(self.band_weights or _DIFFICULTY_PRESETS[self.difficulty])
        if self.strong_model_fraction is not None:
            f = max(0.0, min(1.0, self.strong_model_fraction))
            others = {k: v for k, v in weights.items() if k != "expert"}
            tot = sum(others.values()) or 1.0
            weights = {k: v / tot * (1.0 - f) for k, v in others.items()}
            weights["expert"] = f
        s = sum(weights.values()) or 1.0
        return {k: v / s for k, v in weights.items()}


# ── verbose domain templates (give the compressor real work to do) ──────
_FIN_SYSTEM = (
    "You are a meticulous financial analysis assistant supporting the corporate "
    "finance team. You should always be careful, rigorous, and precise. Take your "
    "time and think step by step. Be respectful and professional in every answer. "
    "Avoid speculation and only state what the underlying data supports. Always show "
    "your reasoning clearly so reviewers can follow along, and never omit caveats."
)
_SUM_SYSTEM = (
    "You are a helpful summarization assistant. Your job is to summarize weekly "
    "digests for enterprise clients. Always be thorough, professional, and "
    "comprehensive. Maintain a warm and friendly tone at all times. Never use jargon "
    "the client might not understand. Always double-check your work before sending. "
    "The client pays a premium and expects excellence, so be polite and courteous."
)
_SUP_SYSTEM = (
    "You are a senior customer-support resolution specialist. You must be empathetic, "
    "patient, and exceptionally thorough. Always acknowledge the customer's feelings "
    "first. Never dismiss a complaint. Escalate appropriately. Maintain a calm and "
    "reassuring tone. The company's reputation depends on your professionalism."
)
_RES_SYSTEM = (
    "You are a principal research strategist. You synthesize conflicting evidence into "
    "a defensible recommendation under uncertainty. Reason carefully through trade-offs, "
    "surface hidden assumptions, quantify confidence, and state what would change your "
    "mind. Rigor matters more than speed; partial or hand-wavy analysis is unacceptable."
)
_CLS_SYSTEM = "Intent classifier. Return exactly one label from the allowed set."

_REGIONS  = ["EMEA", "APAC", "LATAM", "NA", "ANZ", "MENA", "DACH", "Nordics"]
_PROJECTS = ["Artemis", "Borealis", "Cygnus", "Draco", "Eridanus", "Fornax",
             "Gemini", "Hydra", "Indus", "Janus", "Kepler", "Lyra"]
_MONTHS   = ["August", "September", "October", "November", "December",
             "January", "February", "March", "April", "May"]
_INTENTS  = [
    'Classify the intent of: "What\'s my account balance?"',
    'Classify the intent of: "How do I reset my password?"',
    'Classify the intent of: "Where is my refund?"',
    'Classify the intent of: "Can I upgrade my plan?"',
    'Classify the intent of: "How do I cancel my subscription?"',
    'Classify the intent of: "Why was I charged twice?"',
]


def _finance_conv(rng) -> ConversationPrompt:
    region = rng.choice(_REGIONS)
    growth = f"+{rng.randint(4, 24)}%"
    margin = rng.randint(18, 31)
    accts  = rng.randint(2, 9)
    pipe   = round(rng.uniform(0.6, 3.4), 1)
    hist = [
        ("User", "I need help analyzing this quarter's regional performance, when you have a moment."),
        ("Asst", "I'd be happy to help you analyze the regional performance. Could you tell me which "
                 "regions and metrics you care about? I can look at revenue, margin, headcount, or growth."),
        ("User", "Just summarize the headline numbers for now, keep it tight please."),
        ("Asst", f"Certainly. For the {region} region, revenue grew {growth} quarter over quarter, margin "
                 f"held at {margin}%, and we onboarded {accts} new enterprise accounts. The pipeline for "
                 f"next quarter looks healthy at ${pipe}M and churn is tracking at 4%."),
        ("User", "Great, thanks. That all sounds correct to me."),
        ("Asst", "You're welcome! I'm glad that lines up. Let me know if you'd like me to break any of "
                 "those numbers down further or add commentary on the drivers."),
    ]
    return ConversationPrompt(_FIN_SYSTEM, hist,
                              "Write a 3-bullet executive summary of the quarter.")


def _summary_conv(rng) -> ConversationPrompt:
    sales   = f"+{rng.randint(3, 22)}%"
    hires   = rng.randint(1, 6)
    project = rng.choice(_PROJECTS)
    month   = rng.choice(_MONTHS)
    hist = [
        ("User", "Hey, can you help me put together the weekly report when you get a chance?"),
        ("Asst", "Of course! I'd be happy to help with that. What would you like me to include in the "
                 "weekly report? I can cover sales figures, team updates, project milestones, customer "
                 "feedback, or any other areas you care about. Just let me know what matters most."),
        ("User", "Let's focus on sales and team updates please, keep it tight."),
        ("Asst", f"Understood! I'll focus on sales and team updates and keep it concise. Here is what I "
                 f"have on record: sales rose {sales} year over year, we made {hires} new engineering "
                 f"hires this quarter, and project {project} is on track for the {month} deadline."),
        ("User", "Yes that's right. Anything else you remember from our chats?"),
        ("Asst", "Sure thing! Earlier you also mentioned the marketing budget was approved and the "
                 "customer churn target is 4%. I hope this is helpful — let me know if you'd like more."),
    ]
    return ConversationPrompt(_SUM_SYSTEM, hist, "Now write the final weekly digest.")


def _support_conv(rng) -> ConversationPrompt:
    amount = rng.choice([49, 89, 129, 199, 249])
    day    = rng.randint(1, 28)
    month  = rng.choice(_MONTHS)
    hist = [
        ("User", "This is the third time I've contacted you about a double charge and I'm losing patience."),
        ("Asst", "I'm truly sorry for the frustration this has caused. A double charge is not acceptable "
                 "and I want to make this right. Let me pull up your full account history so I understand "
                 "exactly what happened and why the earlier tickets did not resolve it."),
        ("User", f"I was charged ${amount} twice on {month} {day} and nobody has fixed it yet."),
    ]
    return ConversationPrompt(_SUP_SYSTEM, hist,
                              "Draft a resolution that refunds the duplicate charge and offers a goodwill credit.")


def _research_conv(rng) -> ConversationPrompt:
    market = rng.choice(_REGIONS)
    a      = rng.randint(10, 40)
    b      = rng.randint(10, 40)
    hist = [
        ("User", "We need a build-vs-buy recommendation for the data platform and the board meets Friday."),
        ("Asst", "Understood. To reason about this properly I need the constraints. What are the cost, "
                 "timeline, and compliance requirements, and how strategic is owning this capability?"),
        ("User", f"Buying costs ${a}M over three years; building costs ${b}M but takes 14 months and ties up "
                 f"the platform team. Compliance must meet the new {market} data-residency rules either way."),
        ("Asst", "Thank you, that's the crux. The trade-off is near-term spend and speed against long-run "
                 "control and switching cost, and the residency rules narrow the viable vendors materially."),
        ("User", "Right. We also can't slip the Q3 launch and legal flagged vendor lock-in as a real risk."),
    ]
    return ConversationPrompt(_RES_SYSTEM, hist,
                              "Give a build-vs-buy recommendation with a confidence level and the key risks.")


_DOMAIN_BY_BAND = {
    "simple": ["summary", "classify"],
    "medium": ["finance", "summary"],
    "hard":   ["finance", "support"],
    "expert": ["research", "support"],
}
_SAMPLE_OUTPUT = {
    "finance":  '{"summary":["growth","stable margin","healthy pipeline"],"confidence":0.82}',
    "summary":  '{"points":["sales up","new hires","project on track"],"sentiment":"positive"}',
    "support":  '{"action":"refund + goodwill credit","tone":"empathetic","word_count":170}',
    "research": '{"recommendation":"buy","confidence":0.71,"key_risks":["lock-in","residency"]}',
    "classify": '{"label":"balance_inquiry","confidence":0.97}',
}


def _band_for(complexity: float) -> str:
    for name, band in BANDS.items():
        lo, hi = band["range"]
        if lo <= complexity <= hi:
            return name
    return "medium"


def _make_task(call_no, band, domain, complexity, cfg, rng) -> dict:
    if domain == "classify":
        conv  = ConversationPrompt(_CLS_SYSTEM, [], rng.choice(_INTENTS))
        verbose = False
    elif domain == "finance":
        conv, verbose = _finance_conv(rng), True
    elif domain == "summary":
        conv, verbose = _summary_conv(rng), True
    elif domain == "support":
        conv, verbose = _support_conv(rng), False     # full context required
    else:  # research
        conv, verbose = _research_conv(rng), True

    required = BANDS[band]["required"]
    out_tok  = BANDS[band]["base_out"] + round(BANDS[band]["reason_out"] * cfg.reasoning_depth)
    allow_compress = verbose and (rng.random() < cfg.compressible_fraction)

    return {
        "id":               f"{domain}-{call_no}",
        "agent_id":         f"agent-{domain}",
        "type":             domain,
        "band":             band,
        "complexity":       round(complexity, 3),
        "required_model":   required,
        "urgency":          round(rng.uniform(0.2, 0.9), 2),
        "conversation":     conv,
        "prompt":           conv.render(),
        "question":         conv.question,
        "allow_compress":   allow_compress,
        "out_tokens":       out_tok,
        "ceiling_quality":  cfg.ceiling_quality,
        "expected_quality": quality_for(required, required, cfg.ceiling_quality),
        "baseline_quality": quality_for(BASELINE_MODEL, required, cfg.ceiling_quality),
        "sample_output":    _SAMPLE_OUTPUT[domain],
    }


def _make_loop_task(call_no, cfg, rng) -> dict:
    conv = ConversationPrompt(
        "You are a pipeline processing agent. Continue until the task is done.",
        [("Asst", "Retrying the same step again with no new tool calls.")] * 3,
        "Process the next record.",
    )
    return {
        "id":                   f"loop-{call_no}",
        "agent_id":             f"agent-pipeline-{rng.randint(1, 6)}",
        "type":                 "loop",
        "band":                 "medium",
        "complexity":           0.5,
        "required_model":       SONNET,
        "urgency":              0.5,
        "conversation":         conv,
        "prompt":               conv.render(),
        "question":             conv.question,
        "allow_compress":       False,
        "is_loop":              True,
        "loop_expected":        rng.randint(8, 16),
        "loop_call_tokens_in":  420,
        "loop_call_tokens_out": 180,
        "out_tokens":           0,
        "ceiling_quality":      0.0,
        "expected_quality":     0.0,
        "baseline_quality":     0.0,
        "sample_output":        "",
    }


def _weighted_bands(n: int, weights: dict, rng) -> list[str]:
    """Deterministic band assignment: exact counts by weight, then shuffled."""
    names  = list(weights)
    counts = {k: int(weights[k] * n) for k in names}
    while sum(counts.values()) < n:                      # hand out the remainder
        leftover = sorted(names, key=lambda k: weights[k] * n - counts[k], reverse=True)
        counts[leftover[0]] += 1
    bag = [b for b, c in counts.items() for _ in range(c)]
    rng.shuffle(bag)
    return bag


def generate_dataset(config: GenerationConfig | None = None) -> tuple[list[dict], dict]:
    """Build the task list plus a transparency manifest describing how it was made.

    Returns (tasks, manifest). The manifest is what the report and METHODOLOGY
    surface so the generation is fully auditable."""
    cfg = config or GenerationConfig()
    rng = random.Random(cfg.seed)

    n_total  = cfg.n_tasks
    n_loops  = round(cfg.loop_rate * n_total)
    n_cache  = round(cfg.cache_hit_rate * n_total)
    n_unique = max(1, n_total - n_loops - n_cache)

    weights  = cfg.resolved_band_weights()
    bands    = _weighted_bands(n_unique, weights, rng)

    unique: list[dict] = []
    for i, band in enumerate(bands, 1):
        lo, hi     = BANDS[band]["range"]
        complexity = rng.uniform(lo, hi)
        domain     = rng.choice(_DOMAIN_BY_BAND[band])
        unique.append(_make_task(i, band, domain, complexity, cfg, rng))

    # Cache repeats: exact copies of earlier unique tasks (verbatim prompt).
    repeats: list[dict] = []
    if n_cache > 0 and unique:
        for j in range(n_cache):
            src  = unique[rng.randrange(len(unique))]
            copy = dict(src)
            copy["id"]        = f"{src['id']}-repeat{j+1}"
            copy["is_repeat"] = True
            repeats.append(copy)

    loops = [_make_loop_task(i, cfg, rng) for i in range(1, n_loops + 1)]

    # Interleave so repeats land AFTER their source and loops are spread out.
    tasks = list(unique)
    rng2  = random.Random(cfg.seed + 1)
    for r in repeats:
        pos = rng2.randint(len(unique) // 2, len(tasks))     # always after originals
        tasks.insert(pos, r)
    for lp in loops:
        tasks.insert(rng2.randint(0, len(tasks)), lp)

    # renumber call_ids in final order
    for k, t in enumerate(tasks, 1):
        t["call_no"] = k

    band_counts = {b: sum(1 for t in unique if t["band"] == b) for b in BANDS}
    manifest = {
        "config": {
            "n_tasks":               cfg.n_tasks,
            "seed":                  cfg.seed,
            "difficulty":            cfg.difficulty,
            "reasoning_depth":       cfg.reasoning_depth,
            "cache_hit_rate":        cfg.cache_hit_rate,
            "loop_rate":             cfg.loop_rate,
            "compressible_fraction": cfg.compressible_fraction,
            "strong_model_fraction": cfg.strong_model_fraction,
            "ceiling_quality":       cfg.ceiling_quality,
        },
        "band_weights":   weights,
        "band_counts":    band_counts,
        "n_unique":       len(unique),
        "n_cache_repeat": len(repeats),
        "n_loops":        len(loops),
        "n_emitted":      len(tasks),
        "routing_tiers":  [[u, m] for u, m in ROUTING_TIERS],
        "required_by_band": {b: BANDS[b]["required"] for b in BANDS},
    }
    return tasks, manifest


# ── deterministic mock responder (no API) ───────────────────────────────
def make_mock_responder(config: GenerationConfig | None = None):
    """Return a responder(model, prompt_text, task) -> dict with no API calls.

    Quality reflects whether `model` meets the task's required capability tier;
    tokens/latency are left to be modeled analytically downstream (latency_ms=0
    tells TrackedCall.create to derive wall-clock time from the latency profile)."""
    cfg = config or GenerationConfig()

    def responder(model: str, prompt_text: str, task: dict) -> dict:
        required = task.get("required_model", BASELINE_MODEL)
        ceiling  = task.get("ceiling_quality", cfg.ceiling_quality)
        return {
            "tokens_in":   estimate_tokens(prompt_text),
            "tokens_out":  task.get("out_tokens", 220),
            "quality":     quality_for(model, required, ceiling),
            "output_text": task.get("sample_output", '{"ok": true}'),
            "latency_ms":  0.0,        # 0 => model it analytically (reproducible)
        }

    return responder
