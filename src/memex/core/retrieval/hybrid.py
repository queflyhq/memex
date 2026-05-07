"""
Hybrid retrieval router.

Always runs BM25 (Tier 0). Optionally fuses vector results when the embedding
tier is installed. Expands the seed set by 1-hop graph traversal for context,
then enforces the token budget.

Reciprocal Rank Fusion (RRF) is used to combine BM25 + vector ranks. RRF is
parameter-light and robust to score-scale differences.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from typing import Any

from memex.core.protocols import SemanticStore, VectorStore
from memex.core.retrieval.bm25 import BM25Index
from memex.core.retrieval.budget import enforce_budget
from memex.core.schema import Concept, Edge, EdgeKind, NodeKind, RecallResult

log = logging.getLogger(__name__)


_RRF_K = 60
# Half-life in days — older concepts get a multiplicative score discount so
# "what we decided last week" beats "what we decided 6 months ago" when both
# match the query. 30 days = recent decisions still rank near full weight.
_RECENCY_HALF_LIFE_DAYS = 30.0


def _rrf(rank: int) -> float:
    return 1.0 / (_RRF_K + rank)


def _recency_weight(c: Concept, now: datetime | None = None) -> float:
    """Multiplier in (0, 1]: 1.0 for fresh concepts, decays with half-life."""
    now = now or datetime.now(timezone.utc)
    confirmed = c.last_confirmed_at
    if confirmed.tzinfo is None:
        confirmed = confirmed.replace(tzinfo=timezone.utc)
    age_days = max(0.0, (now - confirmed).total_seconds() / 86400.0)
    return math.pow(0.5, age_days / _RECENCY_HALF_LIFE_DAYS)


class HybridRetriever:
    """Routes queries through BM25 (always) + vector (if available) + graph expand."""

    def __init__(
        self,
        semantic: SemanticStore,
        vector: VectorStore | None = None,
        bm25: BM25Index | None = None,
        reranker: Any | None = None,
        rerank_top_k: int = 20,
    ):
        self.semantic = semantic
        self.vector = vector
        self.bm25 = bm25 or BM25Index()
        self.reranker = reranker
        self.rerank_top_k = rerank_top_k

    def _ensure_bm25(self) -> None:
        if self.bm25.is_dirty:
            self.bm25.rebuild(self.semantic.all_concepts())

    def recall(
        self,
        query: str,
        budget_tokens: int = 2000,
        kind: NodeKind | None = None,
        expand_hops: int = 1,
        embed_query: list[float] | None = None,
    ) -> RecallResult:
        """Hybrid retrieval — returns a budget-bounded subgraph.

        `embed_query` should be provided by the caller when the embedding tier
        is installed; the retriever stays agnostic about which model is used.
        """
        self._ensure_bm25()

        # 1. BM25 candidates
        bm25_hits = self.bm25.search(query, limit=50)
        rrf_scores: dict[str, float] = {}
        for rank, (c, _score) in enumerate(bm25_hits):
            rrf_scores[c.id] = rrf_scores.get(c.id, 0.0) + _rrf(rank)
        seen: dict[str, Concept] = {c.id: c for c, _ in bm25_hits}

        # 2. Vector candidates (when available)
        strategy = "bm25"
        if self.vector is not None and embed_query is not None and self.vector.count() > 0:
            import numpy as np

            vec_hits = self.vector.search(np.asarray(embed_query, dtype=np.float32), limit=50)
            strategy = "hybrid"
            for rank, (cid, _score) in enumerate(vec_hits):
                rrf_scores[cid] = rrf_scores.get(cid, 0.0) + _rrf(rank)
                if cid not in seen:
                    c = self.semantic.get_concept(cid)
                    if c is not None:
                        seen[cid] = c

        # 3. Filter by kind if requested
        if kind is not None:
            seen = {cid: c for cid, c in seen.items() if c.kind == kind}

        # 4. Rank by RRF score × confidence × recency. Recency boost keeps
        # last-week's decisions ranked above last-year's when both match.
        now = datetime.now(timezone.utc)
        ranked: list[Concept] = sorted(
            seen.values(),
            key=lambda c: (
                rrf_scores.get(c.id, 0.0)
                * max(0.05, c.confidence)
                * _recency_weight(c, now=now)
            ),
            reverse=True,
        )

        # 4b. Cross-encoder rerank — second-stage precision filter on the
        # top-50 RRF survivors. Cuts noisy hits before graph expansion so we
        # don't pull in neighbors of a false positive.
        if self.reranker is not None and getattr(self.reranker, "is_available", lambda: True)():
            try:
                ranked = self.reranker.rerank(query, ranked, top_k=self.rerank_top_k)
                strategy = strategy + "+rerank"
            except Exception as e:
                log.warning("rerank failed, falling back to RRF order: %s", e)

        # 5. 1-hop graph expand for context (best-effort)
        edges: list[Edge] = []
        if expand_hops > 0 and ranked:
            top_seeds = ranked[: min(5, len(ranked))]
            extra_nodes: dict[str, Concept] = {}
            for seed in top_seeds:
                hood_nodes, hood_edges = self.semantic.neighbors(
                    seed.id, depth=expand_hops
                )
                edges.extend(hood_edges)
                for n in hood_nodes:
                    if n.id not in seen and n.id not in extra_nodes:
                        extra_nodes[n.id] = n
            ranked.extend(extra_nodes.values())

        # 6. De-dup edges by (from, to, kind)
        edges = _dedup_edges(edges)

        # 7. Budget enforcement
        return enforce_budget(ranked, edges, budget_tokens, strategy=strategy)


def _dedup_edges(edges: list[Edge]) -> list[Edge]:
    seen: set[tuple[str, str, EdgeKind]] = set()
    out: list[Edge] = []
    for e in edges:
        key = (e.from_id, e.to_id, e.kind)
        if key in seen:
            continue
        seen.add(key)
        out.append(e)
    return out
