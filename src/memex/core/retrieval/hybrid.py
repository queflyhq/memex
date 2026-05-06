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

from memex.core.protocols import SemanticStore, VectorStore
from memex.core.retrieval.bm25 import BM25Index
from memex.core.retrieval.budget import enforce_budget
from memex.core.schema import Concept, Edge, EdgeKind, NodeKind, RecallResult

log = logging.getLogger(__name__)


_RRF_K = 60


def _rrf(rank: int) -> float:
    return 1.0 / (_RRF_K + rank)


class HybridRetriever:
    """Routes queries through BM25 (always) + vector (if available) + graph expand."""

    def __init__(
        self,
        semantic: SemanticStore,
        vector: VectorStore | None = None,
        bm25: BM25Index | None = None,
    ):
        self.semantic = semantic
        self.vector = vector
        self.bm25 = bm25 or BM25Index()

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

        # 4. Rank by RRF score weighted by node confidence (decayed handled upstream).
        ranked: list[Concept] = sorted(
            seen.values(),
            key=lambda c: rrf_scores.get(c.id, 0.0) * max(0.05, c.confidence),
            reverse=True,
        )

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
