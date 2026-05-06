"""
BM25 retrieval — the always-available baseline.

This is the Tier 0 retrieval path. It works on any computer without a model
download. For text-heavy lookups against ~10K-100K node corpora, BM25 is
genuinely competitive with vector search and never has cold-start issues.

The index is rebuilt lazily and cached in memory; semantic-store writes mark
the cache stale.
"""

from __future__ import annotations

import re
import threading

from rank_bm25 import BM25Okapi

from memex.core.schema import Concept

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


class BM25Index:
    """Lazy in-memory BM25 over a list of Concepts."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._concepts: list[Concept] = []
        self._index: BM25Okapi | None = None
        self._dirty = True

    def rebuild(self, concepts: list[Concept]) -> None:
        with self._lock:
            self._concepts = list(concepts)
            corpus = [tokenize(_doc_text(c)) for c in self._concepts]
            # rank_bm25 errors on empty corpus; guard for it.
            self._index = BM25Okapi(corpus) if corpus else None
            self._dirty = False

    def mark_dirty(self) -> None:
        with self._lock:
            self._dirty = True

    @property
    def is_dirty(self) -> bool:
        return self._dirty

    def search(self, query: str, limit: int = 50) -> list[tuple[Concept, float]]:
        with self._lock:
            if self._index is None or not self._concepts:
                return []
            q_tokens = tokenize(query)
            if not q_tokens:
                return []
            scores = self._index.get_scores(q_tokens)
            ranked = sorted(
                zip(self._concepts, scores, strict=False),
                key=lambda x: x[1],
                reverse=True,
            )
            positive = [(c, float(s)) for c, s in ranked[:limit] if s > 0.0]
            if positive:
                return positive
            # Fallback: BM25 IDF degenerates on tiny corpora (≤ a few docs)
            # and yields non-positive scores even for relevant matches.
            # Use token-set overlap as a graceful degradation.
            q_set = set(q_tokens)
            overlap_hits: list[tuple[Concept, float]] = []
            for c, _s in ranked[:limit]:
                doc_tokens = set(tokenize(_doc_text(c)))
                overlap = len(q_set & doc_tokens)
                if overlap > 0:
                    overlap_hits.append((c, float(overlap)))
            return overlap_hits


def _doc_text(c: Concept) -> str:
    parts = [c.name, c.description, c.kind.value]
    parts.extend(str(v) for v in c.metadata.values() if isinstance(v, str))
    return " ".join(parts)
