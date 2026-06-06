# core/compression.py
"""
Deterministic, extractive context compression.

No LLM is used: compression is rule-based and fully reproducible, so the
BEFORE/AFTER prompt diff shown in the report is real and verifiable — not a
narrative we invented.

The compressor operates on a structured ConversationPrompt:
  - system    : the system / persona prompt   (verbose      -> condensed)
  - history   : list of (role, text) turns     (verbatim     -> rolling summary)
  - question  : the final user question         (preserved exactly, never cut)

Mechanism:
  1. System prompt  -> extractive: keep the highest-signal sentences and drop
     filler / boilerplate, up to a token budget.
  2. Conversation   -> rolling summary: pull factual nuggets (numbers, proper
     nouns, scope / decision keywords) out of each turn, drop pleasantries,
     and emit one compact CONVERSATION SUMMARY block.
  3. Final question -> kept verbatim.
"""

import re
from dataclasses import dataclass, field


def estimate_tokens(text: str) -> int:
    """Rough token estimate, consistent with core/toon.py (len // 4)."""
    return max(0, len(text) // 4)


# ── Heuristics ─────────────────────────────────────────────────────
_FILLER_MARKERS = (
    "of course", "i'd be happy", "i would be happy", "happy to help",
    "let me", "certainly", "great question", "understood", "thank you",
    "thanks", "no problem", "as an ai", "feel free", "i can cover",
    "i can help", "absolutely", "got it", "sure thing", "you're welcome",
    "does that", "anything else", "hope this helps", "let me know",
)

_SIGNAL_KEYWORDS = (
    "scope", "focus", "agreed", "deadline", "due", "must", "require",
    "required", "budget", "target", "priority", "decision", "decided",
    "constraint", "deliverable", "milestone", "blocker", "risk", "approved",
    "rejected", "policy", "limit", "threshold", "sla", "deploy",
)

_WORD_RE    = re.compile(r"[A-Za-z][A-Za-z0-9\-']+")
_NUM_RE     = re.compile(r"\d[\d.,%]*")
_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+")


def _split_sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENT_SPLIT.split(text.strip()) if s.strip()]


def _informativeness(sentence: str) -> float:
    """Higher = more worth keeping. Numbers and proper nouns are facts;
    pleasantries are noise."""
    low   = sentence.lower()
    score = 0.0
    score += 2.0 * len(_NUM_RE.findall(sentence))          # numbers = hard facts
    words = _WORD_RE.findall(sentence)
    for i, w in enumerate(words):
        if i > 0 and w[0].isupper():                       # proper nouns
            score += 1.0
    for kw in _SIGNAL_KEYWORDS:
        if kw in low:
            score += 1.5
    for fm in _FILLER_MARKERS:
        if fm in low:
            score -= 3.0
    return score


# ── Data classes ───────────────────────────────────────────────────
@dataclass
class ConversationPrompt:
    """A structured prompt: system + multi-turn history + final question."""
    system:   str
    history:  list[tuple[str, str]] = field(default_factory=list)  # (role, text)
    question: str = ""

    def render(self) -> str:
        """Full, uncompressed prompt — the BEFORE text."""
        lines = [f"SYSTEM: {self.system.strip()}", ""]
        if self.history:
            lines.append("CONVERSATION HISTORY (verbatim):")
            for role, text in self.history:
                lines.append(f"[{role}]: {text.strip()}")
            lines.append("")
        lines.append(f"USER: {self.question.strip()}")
        return "\n".join(lines)


@dataclass
class CompressionResult:
    before_text:   str
    after_text:    str
    before_tokens: int
    after_tokens:  int
    system_before: str
    system_after:  str
    turns_total:   int
    turns_dropped: int
    facts_kept:    int

    @property
    def ratio(self) -> float:
        return self.after_tokens / self.before_tokens if self.before_tokens else 1.0

    @property
    def saved_tokens(self) -> int:
        return max(0, self.before_tokens - self.after_tokens)


# ── Compressor ─────────────────────────────────────────────────────
class ContextCompressor:
    """Rule-based extractive compressor. Deterministic and API-free."""

    def __init__(self, system_keep_ratio: float = 0.4, max_facts: int = 8):
        self.system_keep_ratio = system_keep_ratio
        self.max_facts         = max_facts

    def _compress_system(self, system: str) -> str:
        sents = _split_sentences(system)
        if len(sents) <= 1:
            return system.strip()
        scored  = sorted(enumerate(sents),
                         key=lambda x: _informativeness(x[1]), reverse=True)
        budget  = max(1, round(len(sents) * self.system_keep_ratio))
        keep    = sorted(i for i, _ in scored[:budget])
        return " ".join(sents[i] for i in keep)

    def _summarize_history(self, history) -> tuple[list[str], int]:
        facts:   list[str] = []
        dropped = 0
        for _role, text in history:
            sents      = _split_sentences(text)
            turn_facts = [s for s in sents if _informativeness(s) > 1.0]
            if not turn_facts:
                dropped += 1
                continue
            best = max(turn_facts, key=_informativeness)
            facts.append(best.strip())

        # de-duplicate while preserving order, then cap
        seen, uniq = set(), []
        for f in facts:
            key = f.lower()
            if key in seen:
                continue
            seen.add(key)
            uniq.append(f)
        return uniq[: self.max_facts], dropped

    def compress(self, prompt: ConversationPrompt) -> CompressionResult:
        before_text   = prompt.render()
        system_after  = self._compress_system(prompt.system)
        facts, dropped = self._summarize_history(prompt.history)

        lines = [f"SYSTEM: {system_after}", ""]
        if prompt.history:
            lines.append("CONVERSATION SUMMARY (compressed by rolling summarizer):")
            if facts:
                lines.append("[Context: " + " ".join(facts) + "]")
            else:
                lines.append("[Context: no substantive prior content]")
            lines.append("")
        lines.append(f"USER: {prompt.question.strip()}")
        after_text = "\n".join(lines)

        return CompressionResult(
            before_text=before_text,
            after_text=after_text,
            before_tokens=estimate_tokens(before_text),
            after_tokens=estimate_tokens(after_text),
            system_before=prompt.system.strip(),
            system_after=system_after,
            turns_total=len(prompt.history),
            turns_dropped=dropped,
            facts_kept=len(facts),
        )
