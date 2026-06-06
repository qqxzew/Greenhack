# core/prefix_cache.py

from collections import OrderedDict


class PrefixCacheManager:
    """
    Tracks common prompt prefixes (system messages, task templates) so
    that downstream providers can leverage prompt-caching billing tiers.

    This is a lightweight bookkeeping layer: it groups prompts by their
    leading N characters and records how often each prefix repeats. The
    Anthropic API does the actual KV-cache reuse on the server side; here
    we just expose statistics for the dashboard.
    """

    def __init__(self, prefix_len: int = 200, max_prefixes: int = 1024):
        self.prefix_len   = prefix_len
        self.max_prefixes = max_prefixes
        self.counts: "OrderedDict[str, int]" = OrderedDict()
        self.total = 0

    def observe(self, prompt: str) -> dict:
        """Record a prompt, returning hit/miss bookkeeping."""
        self.total += 1
        key = prompt[: self.prefix_len]

        if key in self.counts:
            self.counts[key] += 1
            self.counts.move_to_end(key)
            return {"prefix_hit": True, "prefix_count": self.counts[key]}

        self.counts[key] = 1
        if len(self.counts) > self.max_prefixes:
            self.counts.popitem(last=False)
        return {"prefix_hit": False, "prefix_count": 1}

    @property
    def reuse_rate(self) -> float:
        if self.total == 0:
            return 0.0
        reused = sum(c - 1 for c in self.counts.values())
        return reused / self.total

    def stats(self) -> dict:
        return {
            "unique_prefixes": len(self.counts),
            "total_prompts":   self.total,
            "reuse_rate":      round(self.reuse_rate, 4),
        }
