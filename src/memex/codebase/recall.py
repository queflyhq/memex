"""recall_code — typed-graph query for symbols and their neighborhood.

This is the query surface of codebase memory. It does NOT return ranked
text snippets. It returns:
  - matched `kind=symbol` concepts (or `kind=file` when the query is
    file-shaped)
  - their immediate neighborhood: defining file, parent class, and any
    cross-repo `same_as` siblings
  - any concept-memory nodes (decisions, constraints) that mention the
    matched symbol's name

The point: when the AI asks "what is X" the answer is a connected
subgraph, not a fuzzy snippet list. See the
`memex codebase memory recall returns full neighborhood` constraint.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from memex.core.schema import Concept, Edge, EdgeKind, NodeKind

if TYPE_CHECKING:
    from memex.core.engine import Engine

log = logging.getLogger(__name__)


@dataclass(slots=True)
class CodeRecallResult:
    """A typed-graph response — nodes + edges, not snippets."""

    matches: list[Concept] = field(default_factory=list)   # primary symbols/files matching the query
    neighborhood: list[Concept] = field(default_factory=list)  # files, parents, cross-repo siblings
    edges: list[Edge] = field(default_factory=list)
    related_concepts: list[Concept] = field(default_factory=list)  # decisions/constraints mentioning the symbol
    query: str = ""
    expand_hops: int = 1
    degraded: bool = False
    degraded_reason: str | None = None

    def to_dict(self) -> dict:
        return {
            "matches": [c.model_dump(mode="json") for c in self.matches],
            "neighborhood": [c.model_dump(mode="json") for c in self.neighborhood],
            "edges": [e.model_dump(mode="json") for e in self.edges],
            "related_concepts": [c.model_dump(mode="json") for c in self.related_concepts],
            "query": self.query,
            "expand_hops": self.expand_hops,
            "degraded": self.degraded,
            "degraded_reason": self.degraded_reason,
        }


def recall_code(
    engine: "Engine",
    query: str,
    *,
    source_id: str | None = None,
    symbol_kind: str | None = None,
    expand_hops: int = 1,
    limit: int = 20,
) -> CodeRecallResult:
    """Find symbols matching `query` and return them with their neighborhood.

    `query` is matched against symbol name (exact / case-insensitive prefix /
    substring), then signature. Vector similarity ranking is added in slice
    A.1 — slice A is name-anchored to keep the typed-graph promise
    obvious.
    """
    q_norm = query.strip()
    if not q_norm:
        return CodeRecallResult(query=query, expand_hops=expand_hops)

    matches: list[Concept] = []
    matches.extend(_find_symbols_by_name(engine, q_norm, source_id, symbol_kind, limit))
    if len(matches) < limit and len(q_norm) >= 3:
        matches.extend(_find_symbols_by_signature(engine, q_norm, source_id, limit))

    # Dedup while preserving order.
    seen: set[str] = set()
    deduped: list[Concept] = []
    for c in matches:
        if c.id in seen:
            continue
        seen.add(c.id)
        deduped.append(c)
    matches = deduped[:limit]

    # Neighborhood expansion: defining file + parent symbol + same_as siblings.
    neighborhood: list[Concept] = []
    edges: list[Edge] = []
    if expand_hops > 0:
        for sym in matches:
            n_concepts, n_edges = _expand_one_hop(engine, sym)
            for c in n_concepts:
                if c.id != sym.id and c.id not in seen:
                    seen.add(c.id)
                    neighborhood.append(c)
            edges.extend(n_edges)

    # Related concepts: decisions/constraints/facts whose description mentions
    # any matched symbol name. Cheap text grep over the concept store.
    related = _find_related_concepts(engine, [c.name for c in matches], seen)

    return CodeRecallResult(
        matches=matches,
        neighborhood=neighborhood,
        edges=edges,
        related_concepts=related,
        query=query,
        expand_hops=expand_hops,
    )


# ---- Match helpers -----------------------------------------------------------


def _find_symbols_by_name(
    engine: "Engine",
    query: str,
    source_id: str | None,
    symbol_kind: str | None,
    limit: int,
) -> list[Concept]:
    """Exact match wins; case-insensitive prefix beats substring."""
    q_lower = query.lower()
    candidates = engine.find_by_kind(NodeKind.symbol)
    if source_id:
        candidates = [c for c in candidates if c.metadata.get("source_id") == source_id]
    if symbol_kind:
        candidates = [c for c in candidates if c.metadata.get("symbol_kind") == symbol_kind]

    exact: list[Concept] = []
    prefix: list[Concept] = []
    substring: list[Concept] = []
    for c in candidates:
        n_lower = c.name.lower()
        if n_lower == q_lower:
            exact.append(c)
        elif n_lower.startswith(q_lower):
            prefix.append(c)
        elif q_lower in n_lower:
            substring.append(c)

    ranked = exact + prefix + substring
    return ranked[:limit]


def _find_symbols_by_signature(
    engine: "Engine",
    query: str,
    source_id: str | None,
    limit: int,
) -> list[Concept]:
    q_lower = query.lower()
    candidates = engine.find_by_kind(NodeKind.symbol)
    if source_id:
        candidates = [c for c in candidates if c.metadata.get("source_id") == source_id]
    return [
        c for c in candidates
        if q_lower in c.metadata.get("signature", "").lower()
    ][:limit]


def _expand_one_hop(
    engine: "Engine",
    sym: Concept,
) -> tuple[list[Concept], list[Edge]]:
    """Walk ALL edges out of a symbol — code-shaped (defined_in/calls/imports)
    AND knowledge-shaped (motivated_by/implements/relates_to/...).

    The unified-memory-model principle: when you stand on a code symbol and
    walk neighbors, you should see the file it's in AND the decisions that
    motivated it AND the constraints it must satisfy. Filtering to only
    code-shaped edges defeats the purpose.
    """
    edges = engine.edges_for(sym.id)
    neighbor_ids: set[str] = set()
    for e in edges:
        if e.from_id == sym.id:
            neighbor_ids.add(e.to_id)
        else:
            neighbor_ids.add(e.from_id)
    concepts: list[Concept] = []
    for nid in neighbor_ids:
        c = engine.get(nid)
        if c is not None:
            concepts.append(c)
    return concepts, list(edges)


# Concept kinds that carry knowledge worth surfacing alongside a code match.
# Includes project-management kinds (task, project, milestone) so open
# work about a symbol is part of the recall picture, per the unified-
# memory-model principle.
_KNOWLEDGE_KINDS: frozenset[NodeKind] = frozenset({
    NodeKind.decision, NodeKind.constraint, NodeKind.fact,
    NodeKind.pattern, NodeKind.module, NodeKind.endpoint,
    NodeKind.task, NodeKind.project, NodeKind.milestone,
    NodeKind.opinion, NodeKind.question,
})


def _find_related_concepts(
    engine: "Engine",
    symbol_names: list[str],
    already_seen: set[str],
) -> list[Concept]:
    """Surface knowledge nodes (decisions, constraints, tasks, ADRs, …)
    whose body mentions a matched symbol — that's how memex bridges code
    back to architecture, philosophy, and open work in one retrieval.
    See `memex unified memory model` decision for the rationale.
    """
    if not symbol_names:
        return []
    out: list[Concept] = []
    name_set = {n for n in symbol_names if n and len(n) >= 3}
    for kind in _KNOWLEDGE_KINDS:
        for c in engine.find_by_kind(kind):
            if c.id in already_seen:
                continue
            body = c.description or ""
            if any(n in body or n in c.name for n in name_set):
                out.append(c)
                already_seen.add(c.id)
                if len(out) >= 20:
                    return out
    return out
