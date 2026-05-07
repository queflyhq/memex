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
        """Wire the default Tier-0/Tier-1 stack from settings.

        Single DuckDB file backs all three stores (concepts/edges, events,
        vectors). Kuzu and the per-store SQLite files are legacy paths used
        only by the migration helper in `memex.core.stores.migrate`.
        """
        from memex.core.stores.duckdb_store import DuckDBStores
        from memex.ml.embeddings import build_default_provider
        from memex.ml.rerank import NoOpReranker, build_default_reranker

        settings = settings or get_settings()
        settings.ensure_dirs()

        provider = build_default_provider(
            model_name=settings.embed_model, dim=settings.embed_dim
        )

        # Reranker is opt-in: only built when MEMEX_RERANK_ENABLED=true. Avoids
        # paying the model-load tax for users who haven't asked for it.
        reranker = (
            build_default_reranker(model_name=settings.rerank_model)
            if settings.rerank_enabled
            else NoOpReranker()
        )

        stores = DuckDBStores(settings.store_path, vector_dim=provider.dim)
        bm25 = BM25Index()
        retriever = HybridRetriever(
            semantic=stores.semantic,
            vector=stores.vector,
            bm25=bm25,
            reranker=reranker,
            rerank_top_k=settings.rerank_top_k,
        )
        engine = cls(
            settings=settings,
            semantic=stores.semantic,
            episodic=stores.episodic,
            vector=stores.vector,
            embedding_provider=provider,
            retriever=retriever,
            bm25=bm25,
        )
        # Stash the bundle so `engine.close()` shuts the shared connection.
        engine._stores_bundle = stores
        return engine

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

    def find_by_kind(self, kind: NodeKind | str) -> list[Concept]:
        """Every concept of a given kind. Used by codebase memory to walk
        sources / files / symbols without re-grepping the whole graph."""
        k = NodeKind(kind) if isinstance(kind, str) else kind
        return self.semantic.find_by_kind(k)

    def edges_for(self, concept_id: str) -> list[Edge]:
        """Every edge touching `concept_id` — outgoing + incoming. Single hop.

        Use this when you want to walk the immediate neighborhood without
        the recursive depth-walk semantics of `neighbors`.
        """
        return self.semantic.edges_for(concept_id)

    # ---- mutations -----------------------------------------------------

    def put(self, c: Concept) -> Concept:
        """Upsert a concept. Existing-id writes preserve history via the
        store's versioning; bumping `last_confirmed_at` is the caller's
        responsibility (set it before calling)."""
        self.semantic.add_concept(c)
        self.bm25.mark_dirty()
        self._maybe_index_vector(c)
        return c

    def delete(self, concept_id: str) -> bool:
        """Hard-delete a concept and every edge touching it. Returns True
        when something was removed.

        Used by codebase memory's reindex path. Callers that want soft-delete
        (preserving the concept_history record) should NOT use this — they
        should mark the concept as obsolete via metadata instead.
        """
        existing = self.semantic.get_concept(concept_id)
        if existing is None:
            return False
        removed = self.semantic.delete_concept(concept_id)
        if removed:
            self.bm25.mark_dirty()
            try:
                self.vectors.remove(concept_id)
            except Exception:  # noqa: BLE001
                # Vector store may not have an entry for non-embedded concepts.
                pass
            self.episodic.append(
                EpisodicEvent(
                    kind="concept_deleted",
                    actor=existing.source,
                    payload={
                        "id": concept_id,
                        "name": existing.name,
                        "kind": existing.kind.value,
                    },
                )
            )
        return removed

    def recall(
        self,
        query: str,
        budget_tokens: int = 2000,
        kind: NodeKind | str | None = None,
        expand_hops: int = 1,
    ) -> RecallResult:
        embed_q: list[float] | None = None
        degraded_reason: str | None = None

        if not self.embedding_provider.is_available():
            degraded_reason = "embedding provider not installed (Tier 0 — BM25 only)"
        elif self.vector.count() == 0:
            degraded_reason = "vector store empty (no concepts have been indexed yet)"
        else:
            try:
                embed_q = self.embedding_provider.embed(query).tolist()
            except Exception as e:
                log.warning("query embedding failed; falling back to BM25 only: %s", e)
                degraded_reason = f"query embedding failed: {e}"

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

        # Loud failure on degraded mode — agents must know when results are
        # lexical-only so they don't trust them as semantically complete.
        if degraded_reason is not None:
            result.degraded = True
            result.degraded_reason = degraded_reason
            log.warning("recall ran degraded: %s", degraded_reason)
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

    # ---- project management --------------------------------------------

    def add_task(
        self,
        title: str,
        description: str = "",
        status: str = "pending",
        priority: str = "p2",
        due: str | None = None,
        project_id: str | None = None,
        blocked_by: list[str] | None = None,
        owner: str | None = None,
    ) -> Concept:
        """Add a task. See `add_node` for the underlying mechanic."""
        metadata: dict[str, Any] = {"status": status, "priority": priority}
        if due:
            metadata["due"] = due
        if owner:
            metadata["owner"] = owner
        c = self.add(
            name=title[:200],
            description=description or title,
            kind=NodeKind.task,
            source=Source.agent,
            confidence=1.0 if status == "completed" else 0.5,
            metadata=metadata,
        )
        if project_id:
            try:
                self.link(
                    from_id=c.id,
                    to_id=project_id,
                    kind=EdgeKind.part_of,
                    source=Source.agent,
                )
            except Exception as e:  # noqa: BLE001
                log.warning("could not link task %s to project %s: %s", c.id, project_id, e)
        for blocker in blocked_by or []:
            try:
                self.link(
                    from_id=blocker,
                    to_id=c.id,
                    kind=EdgeKind.blocks,
                    source=Source.agent,
                )
            except Exception as e:  # noqa: BLE001
                log.warning("could not link blocker %s -> %s: %s", blocker, c.id, e)
        return c

    def update_task(
        self,
        task_id: str,
        status: str | None = None,
        priority: str | None = None,
        due: str | None = None,
        owner: str | None = None,
        description: str | None = None,
    ) -> Concept | None:
        """Patch a task's metadata + body. Returns None if id is not a task."""
        existing = self.get(task_id)
        if existing is None or existing.kind != NodeKind.task:
            return None
        new_meta = dict(existing.metadata or {})
        if status is not None:
            new_meta["status"] = status
        if priority is not None:
            new_meta["priority"] = priority
        if due is not None:
            new_meta["due"] = due
        if owner is not None:
            new_meta["owner"] = owner
        new_conf = 1.0 if new_meta.get("status") == "completed" else existing.confidence
        new_desc = description if description is not None else existing.description
        updated = Concept(
            id=existing.id,
            name=existing.name,
            description=new_desc,
            kind=existing.kind,
            source=existing.source,
            confidence=new_conf,
            created_at=existing.created_at,
            metadata=new_meta,
            verification=existing.verification,
        )
        self.semantic.add_concept(updated)
        return updated

    def list_tasks(
        self,
        status: str = "pending",
        project_id: str | None = None,
        owner: str | None = None,
        limit: int = 50,
    ) -> list[Concept]:
        """All tasks matching the filters, ordered by priority+due."""
        tasks = [c for c in self.semantic.all_concepts() if c.kind == NodeKind.task]
        if status and status != "*":
            tasks = [t for t in tasks if (t.metadata or {}).get("status") == status]
        if owner:
            tasks = [t for t in tasks if (t.metadata or {}).get("owner") == owner]
        if project_id:
            # part_of edges point FROM task TO project, so we walk each
            # candidate task's outgoing edges to find membership.
            in_project: set[str] = set()
            for t in tasks:
                _, edges = self.semantic.neighbors(t.id, depth=1)
                for e in edges:
                    if (
                        e.kind == EdgeKind.part_of
                        and e.from_id == t.id
                        and e.to_id == project_id
                    ):
                        in_project.add(t.id)
                        break
            tasks = [t for t in tasks if t.id in in_project]
        return sorted(tasks, key=_task_sort_key)[:limit]

    def next_actions(self, limit: int = 5) -> list[Concept]:
        """Top tasks you can work on right now — non-blocked, ranked."""
        all_concepts = self.semantic.all_concepts()
        all_tasks = [c for c in all_concepts if c.kind == NodeKind.task]
        active_tasks = [
            t for t in all_tasks
            if (t.metadata or {}).get("status") in (None, "pending", "in_progress")
        ]
        done_ids = {
            t.id for t in all_tasks
            if (t.metadata or {}).get("status") == "completed"
        }
        # Build the "X is blocked by Y" map. neighbors() returns outgoing edges
        # from a node, so we walk every potential blocker and record the
        # tasks they block (the inverse direction).
        blocked_by: dict[str, list[str]] = {}
        for blocker in all_tasks:
            _, edges = self.semantic.neighbors(blocker.id, depth=1)
            for e in edges:
                if e.kind == EdgeKind.blocks and e.from_id == blocker.id:
                    blocked_by.setdefault(e.to_id, []).append(blocker.id)
        unblocked = [
            t
            for t in active_tasks
            if not any(b not in done_ids for b in blocked_by.get(t.id, []))
        ]
        unblocked.sort(key=_task_sort_key)
        return unblocked[:limit]

    def consolidate(
        self,
        window_events: int = 500,
        min_cluster: int = 5,
        llm: Any | None = None,
    ) -> dict[str, int]:
        """Compress recent episodic events into durable summary concepts.

        The "sleep cycle" of memex: recent events get clustered (by event kind
        for now) and clusters above threshold become summary `kind=fact`
        concepts so future recall finds them as durable knowledge instead of
        having to traverse hundreds of raw events.

        v0.1 strategy: bucket by `event.kind`, summarize via templated
        extraction (no LLM required). When an LLM provider is passed in, it
        produces a richer prose summary instead.

        Returns counts: {events_seen, clusters, concepts_added}.
        """
        events = self.episodic.recent(limit=window_events)
        if not events:
            return {"events_seen": 0, "clusters": 0, "concepts_added": 0}

        # Bucket by kind; ignore consolidation byproducts so we don't loop.
        buckets: dict[str, list[Any]] = {}
        for ev in events:
            if ev.kind in {"consolidation_run", "concept_added", "edge_added", "recall_executed"}:
                continue
            buckets.setdefault(ev.kind, []).append(ev)

        added = 0
        clusters = 0
        for kind, evs in buckets.items():
            if len(evs) < min_cluster:
                continue
            clusters += 1
            start_ts = min(e.timestamp for e in evs)
            end_ts = max(e.timestamp for e in evs)
            sample_payloads = [str(e.payload)[:200] for e in evs[:3]]
            if llm is not None and getattr(llm, "is_available", lambda: False)():
                try:
                    body = llm.generate(
                        prompt=(
                            f"Summarize these {len(evs)} events of kind={kind} into one short fact "
                            f"(2-3 sentences). Focus on patterns/decisions/changes, not noise. "
                            f"Sample payloads: {sample_payloads}"
                        ),
                        max_tokens=200,
                    ) or ""
                except Exception as e:  # noqa: BLE001
                    log.warning("LLM summary failed; using template: %s", e)
                    body = ""
            else:
                body = ""
            if not body:
                body = (
                    f"{len(evs)} `{kind}` events between {start_ts.isoformat()} and "
                    f"{end_ts.isoformat()}. Sample payloads:\n  - "
                    + "\n  - ".join(sample_payloads)
                )
            self.add(
                name=f"consolidated:{kind}@{end_ts.date()}",
                description=body,
                kind=NodeKind.fact,
                source=Source.extractor,
                confidence=0.6,
                metadata={
                    "consolidated": True,
                    "event_kind": kind,
                    "event_count": len(evs),
                    "window_start": start_ts.isoformat(),
                    "window_end": end_ts.isoformat(),
                },
            )
            added += 1
        # Audit our own run so we don't loop on next pass.
        self.observe(
            kind="consolidation_run",
            actor=Source.extractor,
            payload={"events_seen": len(events), "clusters": clusters, "concepts_added": added},
        )
        return {"events_seen": len(events), "clusters": clusters, "concepts_added": added}

    def reindex_vectors(self) -> dict[str, int]:
        """Embed every concept and write to the vector store.

        Backfill / repair operation — runs after migrating from a degraded-
        mode store (no embeddings) or after a model swap. Idempotent: vector
        rows upsert by id. Returns counts so callers can report progress.
        """
        if not self.embedding_provider.is_available():
            raise RuntimeError(
                "embedding provider is not available — install fastembed or set "
                "MEMEX_EMBED_MODEL to a working backend before reindexing."
            )
        all_c = self.semantic.all_concepts()
        embedded = 0
        failed = 0
        for c in all_c:
            try:
                vec = self.embedding_provider.embed(_doc_text(c))
                self.vector.add(c.id, vec)
                embedded += 1
            except Exception as e:  # noqa: BLE001
                log.warning("reindex failed for %s: %s", c.id, e)
                failed += 1
        return {"total": len(all_c), "embedded": embedded, "failed": failed}

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
        # When build_default wired a shared DuckDB bundle, close it once;
        # the per-store close() calls are no-ops in that case but harmless.
        bundle = getattr(self, "_stores_bundle", None)
        self.semantic.close()
        self.episodic.close()
        self.vector.close()
        if bundle is not None:
            bundle.close()

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


_PRIO_ORDER = {"p0": 0, "p1": 1, "p2": 2, "p3": 3}


def _task_sort_key(t: Concept) -> tuple[int, str]:
    m = t.metadata or {}
    return (_PRIO_ORDER.get(m.get("priority", "p2"), 9), m.get("due", "9999-99-99"))
