# core/cache.py

import numpy as np
import faiss
import hashlib
import time
from dataclasses import dataclass


@dataclass
class CacheEntry:
    query:      str
    response:   str
    embedding:  np.ndarray
    quality:    float
    timestamp:  float
    hits:       int = 0


class EWMAThreshold:
    """
    Adapts cache similarity threshold based on observed quality.
    If quality drops below q_min -> raise threshold (stricter, fewer hits).
    If quality stays high -> lower threshold (more aggressive caching).
    """

    def __init__(self, delta0: float = 0.82, q_min: float = 0.75,
                 alpha: float = 0.15, beta: float = 0.03):
        self.delta = delta0
        self.q_ema = q_min
        self.q_min = q_min
        self.alpha = alpha
        self.beta  = beta

    def update(self, observed_quality: float):
        self.q_ema = self.alpha * observed_quality + (1 - self.alpha) * self.q_ema
        adjustment = self.beta * (self.q_ema - self.q_min)
        self.delta = float(np.clip(self.delta - adjustment, 0.70, 0.97))

    @property
    def current(self) -> float:
        return self.delta


def _embed_text(text: str) -> np.ndarray:
    """
    Simple deterministic embedding for hackathon.
    In production: use sentence-transformers or Anthropic embeddings API.

    Uses character n-gram hashing trick to produce a 128-dim vector.
    """
    vec = np.zeros(128, dtype=np.float32)
    text = text.lower().strip()
    for i in range(len(text) - 2):
        ngram = text[i:i+3]
        h = int(hashlib.md5(ngram.encode()).hexdigest(), 16)
        idx  = h % 128
        sign = 1 if (h >> 7) & 1 else -1
        vec[idx] += sign
    norm = np.linalg.norm(vec)
    if norm > 0:
        vec /= norm
    return vec


class SemanticCache:
    """
    Cache LLM responses by semantic similarity.

    Uses FAISS flat IP index for nearest-neighbour lookup.
    Cosine similarity is computed via normalized vectors + inner product.
    """

    def __init__(self, dim: int = 128, max_size: int = 10_000):
        self.dim       = dim
        self.max_size  = max_size
        self.threshold = EWMAThreshold()
        self.entries:  list[CacheEntry] = []
        self.index     = faiss.IndexFlatIP(dim)
        self.hits      = 0
        self.misses    = 0

    def _search(self, embedding: np.ndarray) -> tuple[float, CacheEntry | None]:
        if self.index.ntotal == 0:
            return 0.0, None
        vec = embedding.reshape(1, -1)
        distances, indices = self.index.search(vec, 1)
        sim = float(distances[0][0])
        entry = self.entries[indices[0][0]] if sim >= 0 else None
        return sim, entry

    def get(self, query: str) -> tuple[str | None, float]:
        """Look up a query. Returns (cached_response, similarity) or (None, 0.0)."""
        emb = _embed_text(query)
        sim, entry = self._search(emb)

        if entry is not None and sim >= self.threshold.current:
            entry.hits += 1
            self.hits += 1
            self.threshold.update(entry.quality)
            return entry.response, sim

        self.misses += 1
        return None, sim

    def put(self, query: str, response: str, quality: float):
        """Store a new response in the cache."""
        if len(self.entries) >= self.max_size:
            self.entries.pop(0)
            self.index = faiss.IndexFlatIP(self.dim)
            vecs = np.array([e.embedding for e in self.entries], dtype=np.float32)
            if len(vecs) > 0:
                self.index.add(vecs)

        emb = _embed_text(query)
        entry = CacheEntry(
            query=query, response=response,
            embedding=emb, quality=quality,
            timestamp=time.time()
        )
        self.entries.append(entry)
        self.index.add(emb.reshape(1, -1))

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total > 0 else 0.0

    def stats(self) -> dict:
        return {
            "size":      len(self.entries),
            "hits":      self.hits,
            "misses":    self.misses,
            "hit_rate":  round(self.hit_rate, 4),
            "threshold": round(self.threshold.current, 4),
        }
