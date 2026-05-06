"""
Engine façade — the single object every frontend (MCP / HTTP / CLI) talks to.

Wires together the three stores, the retrieval strategy, and the optional
embedding provider via constructor dependency injection. Frontends never
reach into stores directly; they go through this façade.

Default wiring is in `Engine.build_default()`. Tests construct the Engine
directly with in-memory or fake protocol-compatible implementations.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from memex.config import Settings, get_settings
from memex.core.lifecycle.decay import decayed_confidence
from memex.core.protocols import (
    EmbeddingProvider,
    EpisodicStore,
    RetrievalStrategy,
    SemanticStore,
    VectorStore,
)
from memex.core.retrieval.bm25 import BM25Index
from memex.core.retrieval.hybrid import HybridRetriever
from memex.core.schema import (
    Concept,
    Edge,
    EdgeKind,
    EpisodicEvent,
    NodeKind,
    RecallResult,
    Source,
)
from memex.core.working_set import WorkingSet

log = logging.getLogger(__name__)


class Engine:
    """Constructor-injected façade. See `Engine.build_default()` for wiring."""

    def __init__(
        self,
        *,
        settings: Settings,
        semantic: SemanticStore,
        episodic: EpisodicStore,
        vector: VectorStore,
        embedding_provider: EmbeddingProvider,
        retriever: RetrievalStrategy,
        bm25: BM25Index,
        working_set: WorkingSet | None = None,
    ):
        self.settings = settings
        self.semantic = semantic
        self.episodic = episodic
        self.vector = vector
        self.embedding_provider = embedding_provider
        self.retriever = retriever
        self.bm25 = bm25
        self.working_set = working_set or WorkingSet(capacity=64)

    # ---- factory --------------------------------------------------------

    @classmethod
    def build_default(cls, settings: Settings | None = None) -> Engine:
        """Wire the default Tier-0/Tier-1 stack from settings."""
        from memex.core.stores.episodic import SqliteEpisodicStore
        from memex.core.stores.semantic import KuzuSemanticStore
        from memex.core.stores.vector import NumpyVectorStore
        from memex.ml.embeddings import build_default_provider

        settings = settings or get_settings()
        settings.ensure_dirs()

        provider = build_default_provider(
            model_name=settings.embed_model, dim=settings.embed_dim
        )

        semantic = KuzuSemanticStore(settings.graph_path)
        episodic = SqliteEpisodicStore(settings.episodic_path)
        vector = NumpyVectorStore(settings.vectors_path, dim=provider.dim)
        bm25 = BM25Index()
        retriever = HybridRetriever(semantic=semantic, vector=vector, bm25=bm25)
        return cls(
            settings=settings,
            semantic=semantic,
            episodic=episodic,
            vector=vector,
            embedding_provider=provider,
            retriever=retriever,
            bm25=bm25,
        )

    # ---- writes ---------------------------------------------------------

    def add(
        self,
        name: str,
        description: str = "",
        kind: NodeKind | str = NodeKind.fact,
        source: Source | str = Source.human,
        confidence: float = 1.0,
        verification: str | None = None,
        metadata: dict[str, Any] | None = None,
        related_to: list[str] | None = None,
    ) -> Concept:
        c = Concept(
            name=name,
            description=description,
            kind=NodeKind(kind) if isinstance(kind, str) else kind,
            source=Source(source) if isinstance(source, str) else source,
            confidence=confidence,
            verification=verification,
            metadata=metadata or {},
        )
        self.semantic.add_concept(c)
        self.bm25.mark_dirty()
        self._maybe_index_vector(c)
        self.working_set.touch(c.id)
        # Activity event — `progress()` reads this back so AI knows what's been done.
        self.episodic.append(
            EpisodicEvent(
                kind="concept_added",
                actor=c.source,
                payload={"id": c.id, "name": c.name, "kind": c.kind.value},
            )
        )
        if related_to:
            for other_id in related_to:
                self.link(c.id, other_id, EdgeKind.relates_to, source=c.source)
        return c

    def link(
        self,
        from_id: str,
        to_id: str,
        kind: EdgeKind | str = EdgeKind.relates_to,
        source: Source | str = Source.human,
        confidence: float = 1.0,
        metadata: dict[str, Any] | None = None,
    ) -> Edge:
        e = Edge(
            from_id=from_id,
            to_id=to_id,
            kind=EdgeKind(kind) if isinstance(kind, str) else kind,
            source=Source(source) if isinstance(source, str) else source,
            confidence=confidence,
            metadata=metadata or {},
        )
        self.semantic.add_edge(e)
        self.episodic.append(
            EpisodicEvent(
                kind="edge_added",
                actor=e.source,
                payload={"from_id": e.from_id, "to_id": e.to_id, "kind": e.kind.value},
            )
        )
        return e

    def observe(
        self,
        kind: str,
        actor: Source | str = Source.agent,
        payload: dict[str, Any] | None = None,
    ) -> EpisodicEvent:
        ev = EpisodicEvent(
            kind=kind,
            actor=Source(actor) if isinstance(actor, str) else actor,
            payload=payload or {},
        )
        self.episodic.append(ev)
        return ev

    # ---- reads ---------------------------------------------------------

    def get(self, concept_id: str) -> Concept | None:
        return self.semantic.get_concept(concept_id)

    def recall(
        self,
        query: str,
        budget_tokens: int = 2000,
        kind: NodeKind | str | None = None,
        expand_hops: int = 1,
    ) -> RecallResult:
        embed_q: list[float] | None = None
        if self.embedding_provider.is_available() and self.vector.count() > 0:
            try:
                embed_q = self.embedding_provider.embed(query).tolist()
            except Exception as e:
                log.warning("query embedding failed; falling back to BM25 only: %s", e)

        kind_filter: NodeKind | None = None
        if kind is not None:
            kind_filter = NodeKind(kind) if isinstance(kind, str) else kind

        result = self.retriever.recall(
            query=query,
            budget_tokens=budget_tokens,
            kind=kind_filter,
            expand_hops=expand_hops,
            embed_query=embed_q,
        )
        # Apply time-decay to displayed confidence (does not mutate stored values).
        now = datetime.now(timezone.utc)
        for n in result.nodes:
            n.confidence = decayed_confidence(n.confidence, n.last_confirmed_at, now=now)

        # RAM-style refresh on access: bump retrieved ids in the working set.
        # The L1 cache is biased toward recently-touched concepts.
        self.working_set.touch_many([n.id for n in result.nodes])

        # Activity event — every recall is recorded so the activity log stays complete.
        self.episodic.append(
            EpisodicEvent(
                kind="recall_executed",
                actor=Source.agent,
                payload={
                    "query": query,
                    "budget_tokens": budget_tokens,
                    "n_nodes": len(result.nodes),
                    "n_edges": len(result.edges),
                    "strategy": result.strategy,
                },
            )
        )
        return result

    # ---- skill validation ---------------------------------------------

    def validate(
        self,
        skill_name: str,
        actor: Source | str = Source.agent,
    ) -> dict[str, Any]:
        """Return the structured `approach + checks + examples` for a skill entry.

        AI tools call this *before* generating code in the relevant context.
        The returned payload is the contract: agents must self-attest each
        check passes and report the result back to the user.

        Side effect: emits an episodic event so callers can later query which
        skills have been validated in this session via `recent_validations()`.
        """
        approach = self._find_approach(skill_name)
        actor_enum = Source(actor) if isinstance(actor, str) else actor
        if approach is None:
            self.observe(
                kind="skill_validation_failed",
                actor=actor_enum,
                payload={"skill": skill_name, "reason": "not_installed"},
            )
            return {
                "ok": False,
                "skill": skill_name,
                "error": f"no approach named `{skill_name}` is installed",
            }
        meta = approach.metadata or {}
        # Episodic audit trail — `progress` and `recent_validations` read this back.
        self.observe(
            kind="skill_validated",
            actor=actor_enum,
            payload={
                "skill": skill_name,
                "concept_id": approach.id,
                "skill_bundle": meta.get("skill"),
            },
        )
        return {
            "ok": True,
            "skill": skill_name,
            "kind": approach.kind.value,
            "description": approach.description,
            "approach": meta.get("approach", ""),
            "triggers": meta.get("triggers", []),
            "checks": meta.get("checks", []),
            "examples_good": meta.get("examples_good", []),
            "examples_bad": meta.get("examples_bad", []),
            "skill_bundle": meta.get("skill"),
            "confidence": approach.confidence,
        }

    def recent_validations(
        self,
        actor: Source | str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Return recent skill-validation events. AI uses this to know what's done."""
        events = self.episodic.recent(limit=limit, kind="skill_validated")
        if actor is not None:
            actor_enum = Source(actor) if isinstance(actor, str) else actor
            events = [e for e in events if e.actor == actor_enum]
        return [
            {
                "id": ev.id,
                "timestamp": ev.timestamp.isoformat(),
                "actor": ev.actor.value,
                "skill": ev.payload.get("skill"),
                "concept_id": ev.payload.get("concept_id"),
                "skill_bundle": ev.payload.get("skill_bundle"),
            }
            for ev in events
        ]

    def progress(self, actor: Source | str | None = None) -> dict[str, Any]:
        """Summarize what's been done: which skills validated, how many concepts added, etc."""
        validations = self.recent_validations(actor=actor, limit=200)
        validated_skills = sorted({v["skill"] for v in validations if v["skill"]})
        return {
            "validated_skills": validated_skills,
            "validations_count": len(validations),
            "concepts_total": self.semantic.count(),
            "events_total": self.episodic.count(),
            "actor_filter": (actor.value if isinstance(actor, Source) else actor),
        }

    def _find_approach(self, name: str) -> Concept | None:
        # Prefer exact name match against approach-kind concepts.
        for c in self.semantic.search_lexical(name, limit=20, kind=NodeKind.approach):
            if c.name == name:
                return c
        for c in self.semantic.search_lexical(name, limit=20):
            if c.name == name and c.kind == NodeKind.approach:
                return c
        return None

    # ---- diagnostics ---------------------------------------------------

    def stats(self) -> dict[str, Any]:
        return {
            "data_dir": str(self.settings.data_dir),
            "concepts": self.semantic.count(),
            "vectors": self.vector.count(),
            "events": self.episodic.count(),
            "embed_tier_available": self.embedding_provider.is_available(),
            "embed_model": (
                self.settings.embed_model if self.embedding_provider.is_available() else None
            ),
        }

    def close(self) -> None:
        self.semantic.close()
        self.episodic.close()
        self.vector.close()

    # ---- internals -----------------------------------------------------

    def _maybe_index_vector(self, c: Concept) -> None:
        if not self.embedding_provider.is_available():
            return
        try:
            vec = self.embedding_provider.embed(_doc_text(c))
            self.vector.add(c.id, vec)
        except Exception as e:
            log.warning("embedding failed for concept %s: %s", c.id, e)


def _doc_text(c: Concept) -> str:
    return f"{c.name}\n{c.description}".strip()
