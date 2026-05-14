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
        llm: Any | None = None,
    ):
        self.settings = settings
        self.semantic = semantic
        self.episodic = episodic
        self.vector = vector
        self.embedding_provider = embedding_provider
        self.retriever = retriever
        self.bm25 = bm25
        self.working_set = working_set or WorkingSet(
            capacity=settings.working_set_size
        )
        # Optional LLM provider — gates query expansion, HyDE, and richer
        # consolidation summaries. None means "those features stay dark."
        self.llm = llm

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
        from memex.ml.llm import build_default_llm
        from memex.ml.rerank import NoOpReranker, build_default_reranker

        settings = settings or get_settings()
        settings.ensure_dirs()

        provider = build_default_provider(
            model_name=settings.embed_model,
            dim=settings.embed_dim,
            query_prefix=settings.embed_query_prefix,
            doc_prefix=settings.embed_doc_prefix,
            l2_normalize=settings.embed_l2_normalize,
            cache_size=settings.embed_cache_size,
        )
        # Embeddings are correctness. BM25-only recall misses semantic
        # matches ("auth flow" -> "JWT validation") so users would silently
        # get wrong results. Refuse to start instead of silently degrading.
        #
        # Escape hatch: MEMEX_ALLOW_BM25_ONLY=1 for tests / CI / dev
        # environments without working ONNX. Production should never set
        # this — fix the install instead.
        import os as _os
        if not provider.is_available() and _os.environ.get("MEMEX_ALLOW_BM25_ONLY") != "1":
            raise RuntimeError(
                "embedding provider unavailable — memex refuses to start without "
                "working embeddings (recall correctness depends on them).\n"
                "  Fix: pip install --force-reinstall onnxruntime==1.19.2 fastembed\n"
                "  On Windows: install the VC++ Redistributable first "
                "(https://aka.ms/vs/17/release/vc_redist.x64.exe), then reinstall onnxruntime.\n"
                "  Override (degrades correctness): MEMEX_ALLOW_BM25_ONLY=1"
            )

        # Reranker defaults ON; if fastembed isn't installed,
        # build_default_reranker returns a NoOp (loud-failure-or-graceful-
        # fallback discipline). Setting MEMEX_RERANK_ENABLED=false force-
        # disables.
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
            settings=settings,
        )
        # LLM is optional — Ollama if running, Anthropic if API key set,
        # otherwise NoOp. Gates query expansion + HyDE + richer
        # consolidation summaries.
        llm = build_default_llm()
        engine = cls(
            settings=settings,
            semantic=stores.semantic,
            episodic=stores.episodic,
            vector=stores.vector,
            embedding_provider=provider,
            retriever=retriever,
            bm25=bm25,
            llm=llm,
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

    # Edge kinds whose semantics are worth embedding as a standalone
    # `kind=relation` concept so retrieval can hit the *relationship*
    # directly (e.g. "what supersedes the old auth design"). Other
    # edges — relates_to, calls, defined_in, imports — are too generic
    # or too high-volume to embed without polluting the vector pool.
    _HIGH_SIGNAL_EDGE_KINDS: set[EdgeKind] = {
        EdgeKind.supersedes,
        EdgeKind.depends_on,
        EdgeKind.motivated_by,
        EdgeKind.conflicts_with,
        EdgeKind.blocks,
        EdgeKind.same_as,
        EdgeKind.rejected_due_to,
    }

    def link(
        self,
        from_id: str,
        to_id: str,
        kind: EdgeKind | str = EdgeKind.relates_to,
        source: Source | str = Source.human,
        confidence: float = 1.0,
        metadata: dict[str, Any] | None = None,
    ) -> Edge:
        edge_kind = EdgeKind(kind) if isinstance(kind, str) else kind
        e = Edge(
            from_id=from_id,
            to_id=to_id,
            kind=edge_kind,
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
        # Multi-granularity embedding — for relationships the user is
        # likely to query directly (X supersedes Y, A depends_on B), also
        # synthesise a kind=relation concept whose text is the edge
        # sentence. This concept enters BM25 + vector pools and gets a
        # 1-hop expansion to its endpoints, so a query about the
        # relation finds both the rule and the parties. Best-effort —
        # if either endpoint is missing, skip silently.
        if edge_kind in self._HIGH_SIGNAL_EDGE_KINDS:
            try:
                self._maybe_embed_relation(e)
            except Exception as ex:  # noqa: BLE001
                log.debug("relation embedding skipped: %s", ex)
        return e

    def _maybe_embed_relation(self, edge: Edge) -> None:
        """Create or update a `kind=relation` concept summarising `edge`.

        Idempotent: subsequent calls with the same edge update the
        existing concept by stable id. Skipped silently if either
        endpoint concept is unknown.
        """
        from_c = self.semantic.get_concept(edge.from_id)
        to_c = self.semantic.get_concept(edge.to_id)
        if from_c is None or to_c is None:
            return
        # Stable, deterministic id so re-linking the same pair updates in
        # place rather than creating a duplicate relation node.
        rel_id = f"rel::{edge.kind.value}::{edge.from_id}::{edge.to_id}"
        verb = edge.kind.value.replace("_", " ")
        sentence = f"{from_c.name} {verb} {to_c.name}"
        body = (
            f"{from_c.name} ({from_c.kind.value}) {verb} "
            f"{to_c.name} ({to_c.kind.value})"
        )
        rel = Concept(
            id=rel_id,
            name=sentence[:120],
            description=body[:400],
            kind=NodeKind.relation,
            source=edge.source,
            confidence=edge.confidence,
            metadata={
                "from_id": edge.from_id,
                "to_id": edge.to_id,
                "edge_kind": edge.kind.value,
            },
        )
        self.semantic.add_concept(rel)
        self.bm25.mark_dirty()
        self._maybe_index_vector(rel)

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
        # Calibrate confidence on signal-bearing event kinds. This is the
        # closing edge of the perception-cognition loop: corrections
        # down-weight recalled concepts; context-injection / auto-approval
        # up-weight them. Best-effort — calibration failures don't break
        # the observe contract.
        if kind in self._CALIBRATION_DELTAS:
            try:
                self._calibrate_from_recent_recall(kind, payload or {})
            except Exception as e:  # noqa: BLE001
                log.warning("calibration on observe(%s) failed: %s", kind, e)
        return ev

    # ---- reads ---------------------------------------------------------

    def get(self, concept_id: str) -> Concept | None:
        return self.semantic.get_concept(concept_id)

    def find_by_kind(self, kind: NodeKind | str) -> list[Concept]:
        """Every concept of a given kind. Used by codebase memory to walk
        sources / files / symbols without re-grepping the whole graph."""
        k = NodeKind(kind) if isinstance(kind, str) else kind
        return self.semantic.find_by_kind(k)

    def find_by_name_kind_source(
        self,
        name: str,
        kind: NodeKind | str | None = None,
        source: Source | str | None = None,
    ) -> Concept | None:
        """Idempotency lookup for OMP §4.2 `remember` — returns the
        existing concept when (name, kind, source) all match, else None."""
        k = NodeKind(kind) if isinstance(kind, str) else kind
        s = Source(source) if isinstance(source, str) else source
        return self.semantic.find_by_name_kind_source(name, k, s)

    # ---- spaced repetition & retrieval-induced forgetting ----------

    def next_due_for_review(
        self,
        *,
        limit: int = 50,
        kinds: list[NodeKind] | None = None,
    ) -> list[Concept]:
        """Spaced-repetition view: concepts whose effective review window
        has elapsed since `last_confirmed_at`. The window varies by kind
        per the decay half-lives in Settings — decisions/constraints get
        a yearly window, opinions a monthly one. Returned oldest first
        so the user can knock the longest-stale ones down first.

        Use this to power a "review queue" UI. Each item is a concept
        the user should confirm (refresh last_confirmed_at via /remember)
        or revise. A run through the queue produces user_correction or
        context_injected events that calibrate confidence.
        """
        from datetime import datetime as _dt, timedelta as _td, timezone as _tz
        from memex.core.lifecycle.decay import half_life_for_kind

        now = _dt.now(_tz.utc)
        out: list[tuple[float, Concept]] = []
        for c in self.semantic.all_concepts():
            if kinds is not None and c.kind not in kinds:
                continue
            hl_days = half_life_for_kind(c.kind, self.settings)
            window = _td(days=hl_days)
            age = now - c.last_confirmed_at
            if age <= window:
                continue
            overdue_days = (age - window).total_seconds() / 86400.0
            out.append((overdue_days, c))
        out.sort(reverse=True, key=lambda t: t[0])
        return [c for _, c in out[:limit]]

    def apply_retrieval_induced_forgetting(
        self,
        query: str,
        recalled_ids: list[str],
        *,
        decrement: float = 0.01,
    ) -> int:
        """Penalize concepts whose names lexically match the query but
        did not surface in this recall. Anderson-style RIF: every act
        of recalling A subtly inhibits competitors B,C,…

        Bounded: only kind in {fact, decision, opinion, approach} (we
        don't want to penalize structural primitives or constraints).
        Per-call decrement is tiny (0.01) so a single recall doesn't
        meaningfully change ranks — but over thousands of recalls the
        graph self-organizes: useful concepts stay confident, near-
        synonyms that never actually help fade out.

        Returns the number of concepts decremented.
        """
        if not query or not recalled_ids:
            return 0
        recalled_set = set(recalled_ids)
        # Cheap competitor detection: BM25 candidates above the recall
        # cut. We don't have BM25 ranks here directly; use search_lexical
        # which returns ranked candidates from the underlying store.
        try:
            candidates = self.semantic.search_lexical(query, limit=20)
        except Exception:  # noqa: BLE001
            return 0
        n = 0
        permitted_kinds = {
            NodeKind.fact, NodeKind.decision, NodeKind.opinion, NodeKind.approach,
        }
        for c in candidates:
            if c.id in recalled_set or c.kind not in permitted_kinds:
                continue
            try:
                self.calibrate_confidence(c.id, -float(decrement), reason="rif")
                n += 1
            except Exception as e:  # noqa: BLE001
                log.debug("rif: calibrate failed for %s: %s", c.id, e)
        return n

    # ---- cognitive bundle (recall_bundle) ----------------------------

    def recall_bundle(
        self,
        target: str,
        *,
        max_neighbors_per_relation: int = 8,
    ) -> dict[str, Any]:
        """Hydrate a "cognitive view" around a code symbol or concept.

        The thesis: any single concept is only as useful as the surrounding
        graph — the decisions that justify it, the constraints it obeys,
        the tests that prove it, the cross-repo siblings, the callers.
        `recall_bundle` walks the typed edges in one pass and returns the
        full neighborhood as a structured dict.

        `target` can be:
          - A concept id ("c_abc123...")
          - An exact symbol/concept name (looked up by name + kind preference)

        Returns:
          {
            primary: <Concept dict | None>,
            defined_in: <file Concept dict | None>,
            same_as: [<Concept>, ...],          # cross-repo siblings
            callers: [<symbol Concept>, ...],   # symbols that call this
            callees: [<symbol Concept>, ...],   # symbols this calls
            decisions: [<Concept>, ...],         # decisions referencing this
            constraints: [<Concept>, ...],       # constraints/rules touching it
            tests: [<file Concept>, ...],        # heuristic: file paths with /test/
            related: [<Concept>, ...],           # generic relates_to edge targets
            notes: [<Concept>, ...],             # notes/comments attached
            blame: [],                           # placeholder — needs git upstream
          }

        Cognitive view = one-call replacement for the recall+grep+blame
        loop the AI used to do across multiple tools.
        """
        # Resolve target: id-first, then exact-name across all kinds.
        primary = self.semantic.get_concept(target)
        if primary is None:
            # Try exact name in priority order: symbol/file for code work,
            # project for workspace queries, then knowledge kinds.
            preferred = [
                NodeKind.symbol, NodeKind.file,
                NodeKind.project, NodeKind.task, NodeKind.milestone,
                NodeKind.fact, NodeKind.decision, NodeKind.constraint,
                NodeKind.action_constraint, NodeKind.approach,
                NodeKind.pattern, NodeKind.person, NodeKind.note,
            ]
            for k in preferred:
                hits = [
                    c for c in self.semantic.find_by_kind(k)
                    if c.name == target
                ]
                if hits:
                    primary = hits[0]
                    break
            # Last-resort fuzzy: case-insensitive name match across all
            # concepts. Caps at 5000 nodes scanned to avoid pathological
            # latency on million-concept stores.
            if primary is None:
                target_lc = target.lower()
                scanned = 0
                for c in self.semantic.all_concepts():
                    scanned += 1
                    if scanned > 5000:
                        break
                    if c.name.lower() == target_lc:
                        primary = c
                        break
        if primary is None:
            return {
                "primary": None,
                "target": target,
                "error": "not_found",
            }

        # One-hop neighborhood — pull all edges touching the primary.
        nodes_by_id: dict[str, Concept] = {primary.id: primary}
        edges = list(self.semantic.edges_for(primary.id))
        for e in edges:
            for nid in (e.from_id, e.to_id):
                if nid == primary.id or nid in nodes_by_id:
                    continue
                n = self.semantic.get_concept(nid)
                if n is not None:
                    nodes_by_id[nid] = n

        # Classify neighbors by edge kind + direction.
        def _take(neighbors: list[Concept], n: int = max_neighbors_per_relation) -> list[dict[str, Any]]:
            return [c.model_dump(mode="json") for c in neighbors[:n]]

        defined_in: Concept | None = None
        same_as: list[Concept] = []
        callers: list[Concept] = []
        callees: list[Concept] = []
        decisions: list[Concept] = []
        constraints: list[Concept] = []
        tests: list[Concept] = []
        related: list[Concept] = []
        notes: list[Concept] = []
        for e in edges:
            other_id = e.to_id if e.from_id == primary.id else e.from_id
            other = nodes_by_id.get(other_id)
            if other is None:
                continue
            outgoing = (e.from_id == primary.id)
            if e.kind == EdgeKind.defined_in and outgoing:
                defined_in = other
            elif e.kind == EdgeKind.same_as:
                same_as.append(other)
            elif e.kind == EdgeKind.calls:
                # outgoing = primary calls other; incoming = other calls primary
                (callees if outgoing else callers).append(other)
            elif e.kind in (EdgeKind.motivated_by, EdgeKind.supersedes, EdgeKind.rejected_due_to):
                # If the other side is a decision/approach, surface as decisions
                if other.kind in (NodeKind.decision, NodeKind.approach, NodeKind.pattern):
                    decisions.append(other)
                elif other.kind in (NodeKind.constraint, NodeKind.action_constraint):
                    constraints.append(other)
                else:
                    related.append(other)
            elif e.kind == EdgeKind.relates_to:
                if other.kind == NodeKind.decision:
                    decisions.append(other)
                elif other.kind in (NodeKind.constraint, NodeKind.action_constraint):
                    constraints.append(other)
                elif other.kind == NodeKind.note:
                    notes.append(other)
                elif other.kind == NodeKind.file and "test" in (other.name.lower() + other.description.lower()):
                    tests.append(other)
                else:
                    related.append(other)
            elif e.kind == EdgeKind.depends_on or e.kind == EdgeKind.implements:
                related.append(other)
            elif e.kind == EdgeKind.part_of and other.kind == NodeKind.note:
                notes.append(other)

        # Heuristic test discovery: scan kind=file nodes that mention this
        # symbol's name in their description and live under a path with
        # /test/. Cheap; cap to 20 files scanned via find_by_kind iteration.
        if defined_in is not None and len(tests) < 4:
            primary_name = primary.name
            for f in self.semantic.find_by_kind(NodeKind.file)[:5000]:
                if f.id == defined_in.id:
                    continue
                fname = (f.name or "").lower()
                fdesc = (f.description or "").lower()
                if "test" not in fname and "spec" not in fname:
                    continue
                if primary_name.lower() in fdesc or primary_name.lower() in fname:
                    tests.append(f)
                    if len(tests) >= max_neighbors_per_relation:
                        break

        return {
            "primary": primary.model_dump(mode="json"),
            "defined_in": defined_in.model_dump(mode="json") if defined_in else None,
            "same_as": _take(same_as),
            "callers": _take(callers),
            "callees": _take(callees),
            "decisions": _take(decisions),
            "constraints": _take(constraints),
            "tests": _take(tests),
            "related": _take(related),
            "notes": _take(notes),
            "blame": [],  # populated when a git upstream is installed
        }

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
        rerank: bool = False,
    ) -> RecallResult:
        # Input validation — empty / whitespace / single-char queries are
        # garbage in, garbage out. Return an empty result fast instead of
        # spending 5s reranking nonsense.
        if not query or not query.strip() or len(query.strip()) < 2:
            return RecallResult(
                nodes=[], edges=[], tokens_used=0,
                strategy="rejected", degraded=False,
                degraded_reason="query too short",
            )
        # Budget validation — clamp to a sensible range.
        budget_tokens = max(50, min(int(budget_tokens), 8000))
        embed_q: list[float] | None = None
        degraded_reason: str | None = None

        # 0. Optional LLM-driven query preprocessing. Both off by default
        # (latency-sensitive). When on, mutates `query` (BM25 sees the
        # expanded form) and `embed_text` (HyDE replaces the embed
        # target with a hypothetical answer document). Failures are
        # silent — original query path stays intact.
        embed_text = query
        bm25_query = query
        applied: list[str] = []
        if self.settings.query_expansion_enabled:
            from memex.core.retrieval.query import expand_query
            expanded = expand_query(
                query,
                self.llm,
                max_alternates=self.settings.query_expansion_max_alternates,
                max_tokens=self.settings.query_expansion_max_tokens,
            )
            if expanded != query:
                bm25_query = expanded
                applied.append("query_expansion")
        if self.settings.hyde_enabled:
            from memex.core.retrieval.query import hyde_document
            hypo = hyde_document(
                query,
                self.llm,
                max_tokens=self.settings.hyde_max_tokens,
            )
            if hypo:
                # HyDE: embed the hypothetical answer, but BM25 still uses
                # the lexical query — synthetic documents are too noisy
                # for BM25's term-frequency scoring.
                embed_text = hypo
                applied.append("hyde")

        if not self.embedding_provider.is_available():
            degraded_reason = "embedding provider not installed (Tier 0 — BM25 only)"
        elif self.vector.count() == 0:
            degraded_reason = "vector store empty (no concepts have been indexed yet)"
        else:
            try:
                # is_query=True keeps the query prefix; HyDE output is a
                # *document*, so when HyDE fired we need is_query=False.
                is_q = "hyde" not in applied
                embed_q = self.embedding_provider.embed(
                    embed_text, is_query=is_q,
                ).tolist()
            except Exception as e:
                log.warning("query embedding failed; falling back to BM25 only: %s", e)
                degraded_reason = f"query embedding failed: {e}"

        kind_filter: NodeKind | None = None
        if kind is not None:
            kind_filter = NodeKind(kind) if isinstance(kind, str) else kind

        result = self.retriever.recall(
            query=bm25_query,
            budget_tokens=budget_tokens,
            kind=kind_filter,
            expand_hops=expand_hops,
            embed_query=embed_q,
            rerank=rerank,
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

        # Activity event — every recall is recorded so the activity log stays
        # complete. recalled_ids is the calibration handle: when a later event
        # (user_correction, context_injected) arrives, observe() walks back
        # through recall_executed events and applies confidence deltas to the
        # concepts that produced this recall. This is the closing edge of
        # the perception-cognition loop — what makes memex *learn* rather
        # than just remember.
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
                    "recalled_ids": [n.id for n in result.nodes],
                },
            )
        )
        # Optional retrieval-induced forgetting: penalize competitors of
        # the recalled set. Opt-in via MEMEX_RIF=1 because each recall
        # writes O(20) calibration events which can dominate the
        # episodic stream when recall traffic is high. Disabled by
        # default; enable on stores where you want stronger self-
        # organization over time.
        try:
            import os as _os
            if (
                _os.environ.get("MEMEX_RIF", "").lower() in {"1", "true", "yes"}
                and result.nodes
            ):
                self.apply_retrieval_induced_forgetting(
                    query=query,
                    recalled_ids=[n.id for n in result.nodes],
                )
        except Exception as e:  # noqa: BLE001
            log.debug("rif post-recall failed: %s", e)
        return result

    # ---- validate-before-act ------------------------------------------

    def check_action(
        self,
        intent: str,
        *,
        budget_tokens: int = 800,
        deny_threshold: float = 0.85,
        step_up_threshold: float = 0.55,
        project: str | None = None,
    ) -> dict[str, Any]:
        """Memory-as-runtime-safety-check (OMP §4.5 `validate`).

        Given a natural-language description of what the AI is about to
        do, this looks up matching `kind=action_constraint` nodes from the
        graph and returns a verdict the caller can enforce: allow,
        step_up, or deny.

        Designed to be invoked from a PreToolUse hook. The flow is:

            1. AI signals intent: "I'm about to run `rm -rf node_modules`"
            2. Hook calls memex `check_action(intent)` / OMP `validate(intent)`
            3. memex recalls action_constraint nodes; if any high-confidence
               match (e.g. "never auto-delete from CWD"), returns deny
            4. Hook denies the tool call and feeds the reasons back to
               the AI so it can self-correct

        Only `kind=action_constraint` nodes are consulted. Plain
        `kind=constraint` nodes are knowledge / preferences and never
        gate tool calls (this split was added after the 2026-05 audit
        that found prose principles were mis-firing the gate).

        action_constraint metadata fields honored:
          applies_to: list[str]   — if set, only fires when tool_name
                                    appears in this list (case-insensitive).
                                    Empty / missing = applies to any tool.
          match_pattern: str|null — regex over `intent`; when set, MUST
                                    match in addition to the semantic
                                    score (defense in depth).
          verdict: "deny"|"step_up" — per-node verdict override (default
                                       "deny").

        Returns: {
            decision: "allow" | "step_up" | "deny",
            reasons: [{constraint_id, name, confidence, snippet,
                        applies_to, verdict, promoted}, ...],
            query: <intent>,
            checked: <int>,         # number of action_constraints scored
            top_confidence: float,
        }
        """
        # Recall against action_constraint nodes only — these carry the
        # rules the AI must respect. See OMP §4.5 and the comment above
        # NodeKind.action_constraint for why plain constraints are excluded.
        result = self.recall(
            query=intent,
            budget_tokens=budget_tokens,
            kind=NodeKind.action_constraint,
        )
        # The retriever applies decay + per-kind half-life + RRF + rerank
        # already; we just turn the ranked nodes into a verdict.
        reasons: list[dict[str, Any]] = []
        decision = "allow"
        top_conf = 0.0
        intent_lc = (intent or "").lower()
        # Per-node verdict can be `deny` (default) or `step_up`. When any
        # matching node says step_up but none say deny, the overall
        # decision is step_up regardless of confidence (the rule itself
        # declares its severity).
        any_step_up_verdict = False
        any_deny_verdict = False
        import re as _re
        for n in result.nodes:
            # Decay-adjusted confidence above step_up_threshold is the
            # minimum bar to even consider the node a match.
            conf = float(n.confidence)
            if conf < step_up_threshold:
                continue
            md = n.metadata or {}
            # Soft-disable: a rule with metadata.disabled=true is preserved
            # in the graph (regex + reason intact) but ignored by the gate.
            # Used to silence a misfiring baseline rule without deleting it.
            if md.get("disabled"):
                continue
            applies_to = md.get("applies_to") or []
            match_pattern = md.get("match_pattern")
            verdict = (md.get("verdict") or "deny").lower()
            # Scope filter — every action_constraint declares `scope`:
            #   "global"       → fires in any project (default)
            #   "project"      → fires only when `project` arg matches the
            #                    rule's metadata.project value
            # A rule without explicit scope is treated as global (back-compat).
            scope = (md.get("scope") or "global").lower()
            rule_project = md.get("project")
            if scope == "project":
                if not rule_project or rule_project != project:
                    continue  # rule doesn't apply to current project
            # applies_to filter — when set, the rule only fires for tools
            # listed. The hook passes the literal tool name as the first
            # word of the intent (`"Bash: rm -rf ..."`), so we case-fold
            # both sides and do a substring check.
            if applies_to:
                applies_lc = [str(t).lower() for t in applies_to]
                if not any(t in intent_lc for t in applies_lc):
                    continue
            # match_pattern is a regex over the intent. Defense in depth:
            # both the semantic recall AND the regex must agree before we
            # gate. When unset, semantic match alone is enough.
            if match_pattern:
                try:
                    if not _re.search(match_pattern, intent, _re.IGNORECASE):
                        continue
                except _re.error:
                    # Bad regex in user data: log + skip; never crash the gate.
                    log.warning(
                        "action_constraint %s has invalid regex %r",
                        n.id, match_pattern,
                    )
                    continue
            reasons.append({
                "constraint_id": n.id,
                "name": n.name,
                "confidence": conf,
                "snippet": (n.description or "")[:300],
                "applies_to": list(applies_to),
                "match_pattern": match_pattern,
                "verdict": verdict,
                "scope": scope,
                "project": rule_project,
                "promoted": bool(md.get("promoted_from")),
            })
            top_conf = max(top_conf, conf)
            if verdict == "step_up":
                any_step_up_verdict = True
            else:
                any_deny_verdict = True
        # Decision precedence:
        #   1. Any matched rule whose own verdict is "deny" AND conf ≥ deny_threshold → deny
        #   2. Any matched rule (deny or step_up) above step_up_threshold → step_up
        #   3. allow
        if any_deny_verdict and top_conf >= deny_threshold:
            decision = "deny"
        elif (any_deny_verdict or any_step_up_verdict) and top_conf >= step_up_threshold:
            decision = "step_up"

        # LLM-as-judge for borderline cases. The semantic check above can
        # miss subtle violations (regex-only would miss "delete the prod
        # database") or false-positive (regex matches but intent is
        # benign). When an LLM provider is available AND the verdict is
        # ambiguous (top_conf is in the gray zone), ask the LLM to break
        # the tie. Strict opt-in: requires LLM configured + reasons exist.
        # Cost: 1 extra LLM call per check_action (~150-400 tokens), only
        # for ambiguous cases.
        if (
            getattr(self, "llm", None) is not None
            and self.llm.is_available()
            and reasons
            and decision != "deny"  # don't override a hard deny
            and step_up_threshold <= top_conf < deny_threshold
        ):
            try:
                llm_verdict = self._llm_judge_action(intent, reasons)
                if llm_verdict in {"deny", "step_up"}:
                    decision = llm_verdict
                    for r in reasons:
                        r["llm_confirmed"] = True
            except Exception as exc:  # noqa: BLE001
                log.debug("LLM judge skipped: %s", exc)
        # Audit — every check_action emits an event so the dashboard's
        # validate-coverage metric goes up and the activity feed shows
        # what was checked.
        self.episodic.append(
            EpisodicEvent(
                kind="action_checked",
                actor=Source.agent,
                payload={
                    "intent": intent,
                    "decision": decision,
                    "reasons_count": len(reasons),
                    "top_confidence": top_conf,
                    "project": project,
                },
            )
        )
        return {
            "decision": decision,
            "reasons": reasons,
            "query": intent,
            "checked": len(reasons),
            "top_confidence": top_conf,
            "project": project,
        }

    def _llm_judge_action(self, intent: str, reasons: list[dict[str, Any]]) -> str:
        """LLM-as-judge: given the matched constraints, decide allow/step_up/deny.

        Run only on borderline cases (top_conf in the gray zone). The
        prompt is minimal — model returns the bare verdict word so we
        don't have to parse natural language. Bounded to 30 tokens
        output to keep latency under 400ms typical.
        """
        rule_lines = []
        for i, r in enumerate(reasons[:5], 1):
            rule_lines.append(
                f"  {i}. {r.get('name', '?')}: {(r.get('snippet') or '')[:200]}"
            )
        prompt = (
            "You are a strict safety gate for an AI coding assistant. "
            "Decide whether the AI's intended action is allowed under these stored constraints.\n\n"
            "Constraints:\n" + "\n".join(rule_lines) + "\n\n"
            f"Intended action: {intent[:400]}\n\n"
            "Reply with exactly one word: 'allow', 'step_up', or 'deny'. No explanation."
        )
        out = self.llm.generate(prompt, max_tokens=30, temperature=0.0).strip().lower()
        # Normalize — the model may add punctuation or hedge.
        for verdict in ("deny", "step_up", "allow"):
            if verdict in out:
                return verdict
        return "allow"

    # ---- confidence calibration ---------------------------------------

    # Calibration deltas — additive, clamped to [0, 1] in apply.
    # User corrections are a strong negative signal (the recalled concept
    # was unhelpful or misleading). Context injection is a mild positive
    # signal (the concept made it into the AI's context window). Auto-
    # approvals are a stronger positive signal (the recall correlated
    # with a saved prompt).
    _CALIBRATION_DELTAS: dict[str, float] = {
        "user_correction":  -0.10,
        "context_injected": +0.02,
        "auto_approval":    +0.05,
    }

    def calibrate_confidence(
        self,
        concept_id: str,
        delta: float,
        reason: str,
    ) -> Concept | None:
        """Adjust a concept's confidence by `delta`, clamped to [0, 1].

        Positive deltas also bump `last_confirmed_at`, refreshing the
        recency-decay clock. Emits a `confidence_calibrated` event for audit.
        Returns the updated concept (or None if id is unknown).
        """
        existing = self.semantic.get_concept(concept_id)
        if existing is None:
            return None
        old = float(existing.confidence)
        new = max(0.0, min(1.0, old + float(delta)))
        if abs(new - old) < 1e-6:
            return existing  # no-op
        updated = Concept(
            id=existing.id,
            name=existing.name,
            description=existing.description,
            kind=existing.kind,
            source=existing.source,
            confidence=new,
            created_at=existing.created_at,
            last_confirmed_at=(
                datetime.now(timezone.utc) if delta > 0 else existing.last_confirmed_at
            ),
            metadata=existing.metadata,
            verification=existing.verification,
        )
        self.semantic.add_concept(updated)
        self.episodic.append(
            EpisodicEvent(
                kind="confidence_calibrated",
                actor=Source.agent,
                payload={
                    "id": concept_id,
                    "name": updated.name,
                    "old": old,
                    "new": new,
                    "delta": float(delta),
                    "reason": reason,
                },
            )
        )
        return updated

    def _calibrate_from_recent_recall(self, kind: str, payload: dict[str, Any]) -> None:
        """Walk back to the most recent `recall_executed` event and apply
        the kind's calibration delta to each `recalled_id` it produced.

        Called by `observe()` for kinds in `_CALIBRATION_DELTAS`. The
        explicit `recalled_ids` in the payload (when present) takes
        precedence — useful when hooks already correlated the event to
        a specific recall.
        """
        delta = self._CALIBRATION_DELTAS.get(kind)
        if delta is None:
            return
        ids: list[str] = list(payload.get("recalled_ids") or [])
        if not ids:
            # Find the most recent recall_executed event in the stream.
            recent = self.episodic.recent(limit=20)
            for ev in recent:
                if ev.kind == "recall_executed":
                    ids = list((ev.payload or {}).get("recalled_ids") or [])
                    break
        for cid in ids:
            try:
                self.calibrate_confidence(cid, delta, reason=kind)
            except Exception as e:  # noqa: BLE001
                log.warning("calibration failed for %s: %s", cid, e)

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
        # Emit perception event so consolidation, dashboard, and audit see
        # task lifecycle. Without this, status transitions are invisible.
        old_meta = existing.metadata or {}
        changed = {
            k: {"old": old_meta.get(k), "new": new_meta.get(k)}
            for k in ("status", "priority", "due", "owner")
            if old_meta.get(k) != new_meta.get(k)
        }
        if changed or description is not None:
            self.episodic.append(
                EpisodicEvent(
                    kind="task_updated",
                    actor=Source.agent,
                    payload={
                        "id": updated.id,
                        "name": updated.name,
                        "changed": changed,
                        "description_changed": description is not None,
                    },
                )
            )
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
        """Top tasks you can work on right now — non-blocked, ranked.

        Performance: one bulk-query for tasks + one for blocks-edges.
        Previously this called all_concepts() (full table scan) and then
        N+1 neighbors() traversals, making /next-actions take 7-8s on
        databases with hundreds of tasks.
        """
        all_tasks = self.semantic.find_by_kind(NodeKind.task)
        active_tasks = [
            t for t in all_tasks
            if (t.metadata or {}).get("status") in (None, "pending", "in_progress")
        ]
        done_ids = {
            t.id for t in all_tasks
            if (t.metadata or {}).get("status") == "completed"
        }
        # Build the "X is blocked by Y" map via ONE query over all blocks
        # edges, instead of N per-blocker traversals.
        blocked_by: dict[str, list[str]] = {}
        try:
            with self.semantic._lock:  # type: ignore[attr-defined]
                rows = self.semantic.conn.execute(  # type: ignore[attr-defined]
                    "SELECT from_id, to_id FROM edges WHERE kind = ?",
                    ["blocks"],
                ).fetchall()
            for from_id, to_id in rows:
                blocked_by.setdefault(to_id, []).append(from_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("next_actions bulk-blocks query failed: %s", exc)
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

    def promote_patterns(
        self,
        min_support: int = 3,
        lookback_events: int = 1000,
    ) -> dict[str, int]:
        """Promote recurring episodic patterns into durable constraint nodes.

        This is the *learning* mechanism that distinguishes memex from a
        static memory store. The calibration loop already adjusts a
        concept's confidence on each correction, but if the same concept
        keeps getting corrected, the *pattern itself* is the lesson:
        whatever this concept represents, the user wants it down-ranked
        or replaced. We capture that as a new constraint node so future
        recalls find the rule, not just the lower-confidence original.

        Walks the recent episodic stream looking for `user_correction`
        events and the `recall_executed` events that precede them. When a
        target concept appears in `min_support` or more correction
        contexts, emits an "avoid:<name>" constraint pointing at it via a
        `relates_to` edge, plus a `pattern_promoted` event for audit.

        Symmetrically: `auto_approval` events that share a recalled
        concept >= `min_support` times produce a "prefer:<name>" decision
        node — the positive-pattern promotion.

        Returns counts: {events_seen, correction_promotions,
        approval_promotions}.
        """
        events = list(self.episodic.recent(limit=lookback_events))
        if not events:
            return {
                "events_seen": 0,
                "correction_promotions": 0,
                "approval_promotions": 0,
            }

        # Walk forward in time so each correction/approval picks up the
        # *most recent* preceding recall_executed and credits its
        # recalled_ids. episodic.recent returns most-recent first; reverse
        # for a stable temporal walk.
        ordered = list(reversed(events))
        last_recall_ids: list[str] = []
        correction_counts: dict[str, int] = {}
        approval_counts: dict[str, int] = {}
        for ev in ordered:
            payload = ev.payload or {}
            if ev.kind == "recall_executed":
                last_recall_ids = list(payload.get("recalled_ids") or [])
                continue
            ids = list(payload.get("recalled_ids") or last_recall_ids)
            if ev.kind == "user_correction":
                for cid in ids:
                    correction_counts[cid] = correction_counts.get(cid, 0) + 1
            elif ev.kind == "auto_approval":
                for cid in ids:
                    approval_counts[cid] = approval_counts.get(cid, 0) + 1

        promoted_corrections = self._promote_one(
            correction_counts,
            min_support=min_support,
            kind=NodeKind.constraint,
            prefix="avoid",
            promoter_tag="correction_pattern",
            description_template=(
                "User corrected {count} recalls of {name}. Treat as low-"
                "trust; prefer alternatives where available."
            ),
            confidence=0.75,
        )
        promoted_approvals = self._promote_one(
            approval_counts,
            min_support=min_support,
            kind=NodeKind.decision,
            prefix="prefer",
            promoter_tag="approval_pattern",
            description_template=(
                "{count} auto-approvals correlated with recalls of {name}. "
                "Treat as a validated approach for similar queries."
            ),
            confidence=0.75,
        )
        return {
            "events_seen": len(events),
            "correction_promotions": promoted_corrections,
            "approval_promotions": promoted_approvals,
        }

    def _promote_one(
        self,
        counts: dict[str, int],
        *,
        min_support: int,
        kind: NodeKind,
        prefix: str,
        promoter_tag: str,
        description_template: str,
        confidence: float,
    ) -> int:
        """Inner helper: emit one promoted concept per qualifying target.

        Idempotent — re-running the promoter doesn't create duplicate
        constraints. Existing promotions are detected via the
        `promoted_from` metadata field.
        """
        promoted = 0
        # Build a set of already-promoted target ids so we don't double-write.
        already = set()
        for c in self.semantic.find_by_kind(kind):
            promoted_from = (c.metadata or {}).get("promoted_from")
            if promoted_from and (c.metadata or {}).get("promoter") == promoter_tag:
                already.add(promoted_from)
        for cid, count in counts.items():
            if count < min_support or cid in already:
                continue
            target = self.semantic.get_concept(cid)
            if target is None:
                continue
            promoted_concept = self.add(
                name=f"{prefix}: {target.name[:80]}",
                description=description_template.format(
                    count=count, name=target.name
                ),
                kind=kind,
                source=Source.extractor,
                confidence=confidence,
                metadata={
                    "promoted_from": cid,
                    "promoter": promoter_tag,
                    "support": count,
                },
            )
            try:
                self.link(
                    promoted_concept.id,
                    cid,
                    EdgeKind.relates_to,
                    source=Source.extractor,
                )
            except Exception as e:  # noqa: BLE001
                log.warning("promotion link failed: %s", e)
            self.episodic.append(
                EpisodicEvent(
                    kind="pattern_promoted",
                    actor=Source.extractor,
                    payload={
                        "constraint_id": promoted_concept.id,
                        "target_id": cid,
                        "target_name": target.name,
                        "support": count,
                        "promoter": promoter_tag,
                    },
                )
            )
            promoted += 1
        return promoted

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

    # ---- external-MCP enrichment ------------------------------------

    def run_enrichment(
        self,
        *,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Pull facts from configured upstream MCPs into memeX's graph.

        Reads `<data_dir>/enrichment.json` for the list of sources, builds
        a one-shot Aggregator over the user's existing upstreams config,
        runs each source, and ingests results as ext::-prefixed concepts.

        Returns counts per source plus an aggregate summary, suitable for
        rendering on the Maintenance dashboard. Idempotent — re-runs
        upsert by external id.
        """
        from memex.upstreams.enrichment import (
            load_enrichment_config, run_enrichment as _run,
        )
        from memex.upstreams.config import load_upstreams
        from memex.upstreams.aggregator import Aggregator

        cfg_path = self.settings.data_dir / self.settings.enrich_config_filename
        sources = load_enrichment_config(cfg_path)
        if not sources:
            return {
                "sources_run": 0,
                "added": 0,
                "refreshed": 0,
                "failed": 0,
                "dry_run": dry_run,
                "per_source": [],
                "note": (
                    f"no enrichment sources configured. Add to "
                    f"{cfg_path} — see docs/architecture.md or "
                    f"src/memex/upstreams/enrichment.py for the schema."
                ),
            }

        upstreams_file = load_upstreams()
        aggregator = Aggregator(upstreams_file.upstreams)
        try:
            aggregator.start()
            result = _run(self, aggregator, sources, dry_run=dry_run)
        finally:
            try:
                aggregator.stop()
            except Exception as e:  # noqa: BLE001
                log.warning("enrichment: aggregator stop failed: %s", e)

        # Audit so the run shows up on the dashboard timeline.
        self.episodic.append(
            EpisodicEvent(
                kind="enrichment_run",
                actor=Source.extractor,
                payload={
                    "added": result.get("added", 0),
                    "refreshed": result.get("refreshed", 0),
                    "failed": result.get("failed", 0),
                    "sources_run": result.get("sources_run", 0),
                    "dry_run": dry_run,
                },
            )
        )
        return result

    # ---- cleanup / forgetting ---------------------------------------

    def run_cleanup(
        self,
        *,
        forget_unused_days: int | None = None,
        forget_min_confidence: float | None = None,
        forget_max_edges: int | None = None,
        episodic_ttl_days: int | None = None,
        episodic_keep_minimum: int | None = None,
        episodic_keep_kinds: list[str] | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Active forgetting + episodic TTL prune in one pass.

        Returns counts so callers can show a "freed N concepts and M
        events" summary on the Maintenance tab. Idempotent and safe
        under concurrent writes — both stages use atomic deletes.
        """
        from datetime import datetime as _dt, timedelta as _td, timezone as _tz
        from memex.core.lifecycle.forgetting import run_forgetting

        s = self.settings
        forget_result = run_forgetting(
            self.semantic,
            unused_days=forget_unused_days or s.forget_unused_days,
            min_confidence=forget_min_confidence or s.forget_min_confidence,
            max_edges=forget_max_edges if forget_max_edges is not None else s.forget_max_edges,
            half_life_days=s.decay_half_life_default_days,
            dry_run=dry_run,
        )

        ttl_days = episodic_ttl_days or s.episodic_ttl_days
        keep_min = episodic_keep_minimum or s.episodic_keep_minimum
        keep_kinds = episodic_keep_kinds if episodic_keep_kinds is not None else s.episodic_keep_kinds
        cutoff = (_dt.now(_tz.utc) - _td(days=ttl_days)).isoformat()
        events_pruned = 0
        if not dry_run:
            try:
                events_pruned = self.episodic.prune_older_than(
                    cutoff,
                    keep_kinds=keep_kinds,
                    keep_minimum=keep_min,
                )
            except Exception as e:  # noqa: BLE001
                log.warning("episodic prune failed: %s", e)
        # Concept deletion may have left vector rows orphaned; mark BM25
        # dirty so the next recall rebuilds without the deleted rows.
        if forget_result.get("deleted", 0) > 0:
            self.bm25.mark_dirty()

        # Consolidation pass — name-dedup + co-occurrence pattern promotion.
        # Runs after forgetting so we don't waste cycles on concepts that
        # were just deleted. Cheap; bounded by the dedup-kind concept count.
        from memex.core.lifecycle.consolidation import run_consolidation
        try:
            consol = run_consolidation(
                self.episodic,
                self.semantic,
                dry_run=dry_run,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("consolidation: pass failed: %s", e)
            consol = {"same_as_links": 0, "cooccurrence_links": 0}

        # Audit event so the cleanup is visible in the dashboard timeline.
        self.episodic.append(
            EpisodicEvent(
                kind="cleanup_run",
                actor=Source.extractor,
                payload={
                    "forgotten_concepts": forget_result.get("deleted", 0),
                    "pruned_events": events_pruned,
                    "dry_run": dry_run,
                },
            )
        )
        return {
            "forgotten_concepts": forget_result.get("deleted", 0),
            "forget_candidates": forget_result.get("candidates", 0),
            "considered_concepts": forget_result.get("considered", 0),
            "pruned_events": events_pruned,
            "consolidation_same_as_links": consol.get("same_as_links", 0),
            "consolidation_cooccurrence_links": consol.get("cooccurrence_links", 0),
            "ttl_cutoff": cutoff,
            "dry_run": dry_run,
        }

    # ---- maintenance status helpers ----------------------------------

    # Above this vector count, linear-scan recall starts to dominate p99
    # latency and the user benefits from enabling DuckDB's HNSW index.
    # Below: linear scan is faster than HNSW build/load amortised cost.
    _VSS_RECOMMEND_THRESHOLD = 100_000

    def vss_status(self) -> dict[str, Any]:
        """Report vector index posture + recommendation.

        Returns:
            mode: "linear" (current) or "hnsw" (future)
            vectors: total vectors in store
            recommend_hnsw: True when vectors > _VSS_RECOMMEND_THRESHOLD
            note: human-readable advisory
        """
        n = self.vector.count()
        # HNSW is intentionally not auto-enabled in v0.x — a prior bulk-
        # index attempt corrupted the DB file via the VSS extension's
        # broken upsert path. Until that's fixed, mode is always linear.
        mode = "linear"
        recommend = n > self._VSS_RECOMMEND_THRESHOLD
        if not recommend:
            note = (
                f"linear scan over {n} vectors is fine. The HNSW upgrade "
                f"path opens above ~{self._VSS_RECOMMEND_THRESHOLD:,} vectors; "
                f"you have headroom."
            )
        else:
            note = (
                f"{n} vectors — linear scan is starting to dominate p99 latency. "
                f"Plan an HNSW build via the DuckDB VSS extension. See "
                f"docs/architecture.md (Memory layer) for the procedure; the "
                f"build is currently a manual / supervised step due to a "
                f"v0.x VSS-extension upsert bug we hit in production."
            )
        return {
            "mode": mode,
            "vectors": n,
            "recommend_hnsw": recommend,
            "threshold": self._VSS_RECOMMEND_THRESHOLD,
            "note": note,
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
