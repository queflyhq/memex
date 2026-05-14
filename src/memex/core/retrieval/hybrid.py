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


# Module-level fallback constants — used when HybridRetriever is constructed
# without a Settings object (test paths). Production wiring in
# Engine.build_default() always passes Settings, so the live values come
# from src/memex/config.py.
_DEFAULT_RRF_K = 60
_DEFAULT_HALF_LIFE_DAYS = 90.0
_DEFAULT_CONFIDENCE_FLOOR = 0.05
_DEFAULT_CONFIDENCE_EXPONENT = 1.0


class HybridRetriever:
    """Routes queries through BM25 (always) + vector (if available) + graph expand.

    Tunable knobs are read from a Settings-shaped object passed at
    construction. Falls back to module defaults when no settings are wired
    (test paths only).
    """

    def __init__(
        self,
        semantic: SemanticStore,
        vector: VectorStore | None = None,
        bm25: BM25Index | None = None,
        reranker: Any | None = None,
        rerank_top_k: int = 20,
        settings: Any | None = None,
    ):
        self.semantic = semantic
        self.vector = vector
        self.bm25 = bm25 or BM25Index()
        self.reranker = reranker
        self.rerank_top_k = rerank_top_k
        self.settings = settings

    # ---- knob accessors (settings-driven, with module-level fallbacks) ----

    def _knob(self, name: str, default):
        if self.settings is None:
            return default
        return getattr(self.settings, name, default)

    @property
    def rrf_k(self) -> int:
        return int(self._knob("rrf_k", _DEFAULT_RRF_K))

    @property
    def confidence_floor(self) -> float:
        return float(self._knob("confidence_floor", _DEFAULT_CONFIDENCE_FLOOR))

    @property
    def confidence_exponent(self) -> float:
        return float(self._knob("confidence_exponent", _DEFAULT_CONFIDENCE_EXPONENT))

    @property
    def min_similarity(self) -> float:
        return float(self._knob("min_similarity", 0.0))

    @property
    def bm25_pool(self) -> int:
        return int(self._knob("bm25_pool", 50))

    @property
    def vector_pool(self) -> int:
        return int(self._knob("vector_pool", 50))

    @property
    def expand_seeds(self) -> int:
        return int(self._knob("expand_seeds", 5))

    def _rrf(self, rank: int) -> float:
        return 1.0 / (self.rrf_k + rank)

    def _half_life_for(self, c: Concept) -> float:
        """Per-kind half-life lookup. Decisions age slowly; opinions fast."""
        if self.settings is None:
            return _DEFAULT_HALF_LIFE_DAYS
        kind = c.kind.value if hasattr(c.kind, "value") else str(c.kind)
        attr = f"decay_half_life_{kind}_days"
        return float(getattr(
            self.settings, attr,
            self.settings.decay_half_life_default_days
        ))

    def _recency_weight(self, c: Concept, now: datetime | None = None) -> float:
        """Multiplier in (0, 1]: 1.0 for fresh concepts, decays with per-kind half-life."""
        now = now or datetime.now(timezone.utc)
        confirmed = c.last_confirmed_at
        if confirmed.tzinfo is None:
            confirmed = confirmed.replace(tzinfo=timezone.utc)
        age_days = max(0.0, (now - confirmed).total_seconds() / 86400.0)
        return math.pow(0.5, age_days / self._half_life_for(c))

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
        rerank: bool = False,
    ) -> RecallResult:
        """Hybrid retrieval — returns a budget-bounded subgraph.

        `embed_query` should be provided by the caller when the embedding tier
        is installed; the retriever stays agnostic about which model is used.

        `rerank` (default False) controls whether the cross-encoder runs as a
        second-stage precision pass. Off-by-default because the cross-encoder
        is the recall hot-path bottleneck (~5s for ~50 docs on CPU) while the
        quality delta over RRF hybrid is small (~1-2 nDCG@10). Set rerank=True
        for code-search-style precision queries from the UI.
        """
        self._ensure_bm25()

        # 1. BM25 candidates
        bm25_hits = self.bm25.search(query, limit=self.bm25_pool)
        rrf_scores: dict[str, float] = {}
        for rank, (c, _score) in enumerate(bm25_hits):
            rrf_scores[c.id] = rrf_scores.get(c.id, 0.0) + self._rrf(rank)
        seen: dict[str, Concept] = {c.id: c for c, _ in bm25_hits}

        # 2. Vector candidates (when available). Apply the min_similarity
        # floor BEFORE merging into RRF so noisy distant hits don't even
        # enter ranking.
        strategy = "bm25"
        floor = self.min_similarity
        if self.vector is not None and embed_query is not None and self.vector.count() > 0:
            import numpy as np

            vec_hits = self.vector.search(
                np.asarray(embed_query, dtype=np.float32),
                limit=self.vector_pool,
            )
            strategy = "hybrid"
            kept_rank = 0
            for cid, score in vec_hits:
                if floor > 0.0 and float(score) < floor:
                    continue
                rrf_scores[cid] = rrf_scores.get(cid, 0.0) + self._rrf(kept_rank)
                kept_rank += 1
                if cid not in seen:
                    c = self.semantic.get_concept(cid)
                    if c is not None:
                        seen[cid] = c

        # 3. Filter by kind if requested
        if kind is not None:
            seen = {cid: c for cid, c in seen.items() if c.kind == kind}

        # 4. Rank by RRF score × confidence^p × recency (per-kind half-life).
        # Recency boost keeps last-week's decisions ranked above last-year's
        # when both match. confidence_exponent lets you penalise low-conf
        # nodes harder (>1) or trust uncertain nodes more (<1).
        now = datetime.now(timezone.utc)
        floor_c = self.confidence_floor
        p = self.confidence_exponent
        ranked: list[Concept] = sorted(
            seen.values(),
            key=lambda c: (
                rrf_scores.get(c.id, 0.0)
                * math.pow(max(floor_c, c.confidence), p)
                * self._recency_weight(c, now=now)
            ),
            reverse=True,
        )

        # 4b. Cross-encoder rerank — OPTIONAL second-stage precision filter.
        # Off-by-default because it adds 5-8 seconds per query on CPU while
        # the quality delta over RRF-hybrid is small (~1-2 nDCG@10).
        #
        # When rerank=True, runs over the top-50 RRF survivors before graph
        # expansion. OMP §4.1 strategy enum: "bm25" | "hybrid" | "hybrid+rerank".
        if rerank and self.reranker is not None and getattr(self.reranker, "is_available", lambda: True)():
            try:
                ranked = self.reranker.rerank(query, ranked, top_k=self.rerank_top_k)
                if strategy == "hybrid":
                    strategy = "hybrid+rerank"
            except Exception as e:
                log.warning("rerank failed, falling back to RRF order: %s", e)

        # 5. 1-hop graph expand for context (best-effort)
        edges: list[Edge] = []
        if expand_hops > 0 and ranked:
            top_seeds = ranked[: min(self.expand_seeds, len(ranked))]
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
