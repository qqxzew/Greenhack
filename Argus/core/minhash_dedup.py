# core/minhash_dedup.py

from datasketch import MinHash, MinHashLSH
import time


class MinHashDeduplicator:
    """
    Detects near-duplicate tasks using MinHash + LSH.

    If two tasks have Jaccard similarity > threshold (default 0.82),
    the second task is served the result of the first without an LLM call.
    """

    def __init__(self, threshold: float = 0.82, num_perm: int = 64):
        self.threshold = threshold
        self.num_perm  = num_perm
        self.lsh       = MinHashLSH(threshold=threshold, num_perm=num_perm)
        self.store:    dict[str, dict] = {}
        self.dup_count = 0
        self.total     = 0

    def _make_minhash(self, text: str) -> MinHash:
        m = MinHash(num_perm=self.num_perm)
        words = text.lower().split()
        for i in range(max(1, len(words) - 2)):
            trigram = " ".join(words[i:i+3])
            m.update(trigram.encode("utf-8"))
        return m

    def check(self, task_id: str, prompt: str) -> dict | None:
        """Check if this prompt is a near-duplicate of a cached task."""
        self.total += 1
        m = self._make_minhash(prompt)

        try:
            results = self.lsh.query(m)
        except Exception:
            results = []

        for key in results:
            if key in self.store:
                self.dup_count += 1
                return self.store[key]["result"]

        return None

    def store_result(self, task_id: str, prompt: str, result: dict):
        """Store a completed task result for future deduplication."""
        m = self._make_minhash(prompt)
        key = f"task_{task_id}"
        try:
            self.lsh.insert(key, m)
        except ValueError:
            pass
        self.store[key] = {"result": result, "timestamp": time.time()}

    @property
    def dedup_rate(self) -> float:
        return self.dup_count / self.total if self.total > 0 else 0.0

    def stats(self) -> dict:
        return {
            "total":      self.total,
            "duplicates": self.dup_count,
            "dedup_rate": round(self.dedup_rate, 4),
        }
