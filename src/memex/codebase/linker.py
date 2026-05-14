"""Cross-repo linker — fuse logically-equivalent symbols across sources.

This is the codebase-memory differentiator. After per-source indexing
produces typed graphs of `source → file → symbol`, the linker walks
across all registered sources and creates `EdgeKind.same_as` edges
between symbols that are the same logical concept. End-to-end graph
traversal then crosses repository boundaries:

    admin-service.handleAuth → calls → admin-service.JWTClaims.Verify
                                       → same_as → auth-service.JWTClaims.Verify
                                                   → calls → auth-service.cryptoVerify

Without these edges, recall stops at the source boundary. With them,
you can stand on any node and walk forward (callees) or backward
(callers) until you have the complete cross-service flow — exactly
the "innovation in itself" surface the codebase-memory pitch promises.

Slice B-α: signature-similarity heuristic only. Inputs are name +
symbol_kind + parsed-signature tokens. The threshold is conservative
(0.7 Jaccard) and there's a hard exclusion list of common-noise
names (`parse`, `init`, `__init__`, etc.) that shouldn't be cross-
linked across services.

Future heuristics (queued, not in this pass):
- Shared proto/OpenAPI/gRPC definitions
- HTTP route ↔ consumer matching
- Env-var lineage
- LLM-assisted disambiguation for hard cases (gated, opt-in)
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from memex.core.schema import Concept, EdgeKind, NodeKind, Source as SourceActor

if TYPE_CHECKING:
    from memex.core.engine import Engine

log = logging.getLogger(__name__)


# Names that are too generic to link across repos with name-only heuristic.
# A `parse` in service A is rarely the SAME `parse` as one in service B.
_GENERIC_NAMES: frozenset[str] = frozenset({
    # Lifecycle / constructors
    "__init__", "__new__", "__del__", "__enter__", "__exit__",
    "init", "new", "create", "destroy", "close", "open",
    # Common short verbs
    "parse", "format", "serialize", "deserialize", "encode", "decode",
    "render", "build", "make", "get", "set", "update", "delete",
    "run", "execute", "start", "stop", "restart", "reset",
    "load", "save", "read", "write", "fetch", "send", "receive",
    "validate", "check", "assert", "test", "verify",
    "to_string", "to_dict", "to_json", "from_dict", "from_json",
    "main", "handle", "process", "filter", "map", "reduce",
    # Toplevel idioms
    "Server", "Client", "Handler", "Worker", "Manager",
    "Config", "Settings", "Options", "Args", "Result", "Response",
    "Error", "Exception",
})


# Minimum identifier length that's eligible for cross-repo linking.
_MIN_LINKABLE_NAME_LEN = 4


@dataclass(slots=True)
class LinkPair:
    """One same_as candidate created by the linker."""
    a_id: str
    b_id: str
    a_name: str
    b_name: str
    a_source: str
    b_source: str
    similarity: float
    heuristic: str  # "name+sig" | "name+kind" | "proto" | ...


@dataclass(slots=True)
class LinkResult:
    """Outcome of one linker pass."""
    pairs: list[LinkPair] = field(default_factory=list)
    sources_considered: int = 0
    candidates_examined: int = 0
    skipped_generic: int = 0
    skipped_short_name: int = 0


# ---- Public API --------------------------------------------------------------


def link_cross_repo(
    engine: "Engine",
    *,
    source_ids: list[str] | None = None,
    threshold: float = 0.7,
    min_name_len: int = _MIN_LINKABLE_NAME_LEN,
    dry_run: bool = False,
) -> LinkResult:
    """Run the cross-repo linker.

    Looks across every registered source (or the provided subset) and
    creates `same_as` edges between symbols that are likely the same
    logical concept.

    Heuristic (Slice B-α — name + signature similarity):
      For every (name, symbol_kind) group spanning 2+ different sources,
      compare each pair's signatures via Jaccard over identifier tokens.
      If similarity ≥ threshold (default 0.7) and the name is not in the
      generic-noise list, create a `same_as` edge.

    Pass `dry_run=True` to compute pairs without writing edges (good for
    preview / desktop UI).

    Idempotent: re-running the linker against an already-linked graph
    re-creates the same edges (engine.link is upsert-on-conflict in the
    DuckDB store), so running it after each `index_source` is safe.
    """
    sources = _scope_sources(engine, source_ids)
    result = LinkResult(sources_considered=len(sources))
    if len(sources) < 2:
        return result

    by_key: dict[tuple[str, str], list[Concept]] = {}
    for sym in engine.find_by_kind(NodeKind.symbol):
        sid = sym.metadata.get("source_id")
        if sid not in sources:
            continue
        kind = sym.metadata.get("symbol_kind") or "?"
        by_key.setdefault((sym.name, kind), []).append(sym)

    for (name, kind), syms in by_key.items():
        if len(syms) < 2:
            continue
        result.candidates_examined += len(syms)
        if name in _GENERIC_NAMES:
            result.skipped_generic += len(syms)
            continue
        if len(name) < min_name_len:
            result.skipped_short_name += len(syms)
            continue

        # Pair across distinct sources.
        for i, a in enumerate(syms):
            for b in syms[i + 1:]:
                if a.metadata.get("source_id") == b.metadata.get("source_id"):
                    continue
                sim = _signature_similarity(
                    a.metadata.get("signature", ""),
                    b.metadata.get("signature", ""),
                )
                if sim < threshold:
                    continue
                pair = LinkPair(
                    a_id=a.id, b_id=b.id,
                    a_name=a.name, b_name=b.name,
                    a_source=a.metadata.get("source_id", ""),
                    b_source=b.metadata.get("source_id", ""),
                    similarity=sim,
                    heuristic="name+kind+sig",
                )
                result.pairs.append(pair)
                if not dry_run:
                    engine.link(
                        from_id=a.id,
                        to_id=b.id,
                        kind=EdgeKind.same_as,
                        source=SourceActor.agent,
                        confidence=sim,
                        metadata={"heuristic": "name+kind+sig"},
                    )

    return result


# ---- Internals ---------------------------------------------------------------


def _scope_sources(
    engine: "Engine",
    source_ids: list[str] | None,
) -> set[str]:
    """Return the set of source ids to consider for linking."""
    if source_ids:
        return set(source_ids)
    return {s.id for s in engine.find_by_kind(NodeKind.source)}


_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _tokenize_signature(sig: str) -> set[str]:
    """Tokenize a signature string into identifiers, lowercased.

    Splits camelCase / snake_case / dotted paths into constituent parts
    so `getJWTClaims` and `get_jwt_claims` tokenize compatibly.
    """
    tokens: set[str] = set()
    for raw in _TOKEN_RE.findall(sig):
        # snake_case
        for piece in raw.split("_"):
            if not piece:
                continue
            # camelCase / PascalCase
            for sub in re.findall(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+", piece):
                tokens.add(sub.lower())
    return tokens


def _signature_similarity(a: str, b: str) -> float:
    """Jaccard over identifier tokens. 0..1."""
    ta = _tokenize_signature(a)
    tb = _tokenize_signature(b)
    if not ta and not tb:
        return 1.0  # both empty signatures (no params, void return)
    if not ta or not tb:
        return 0.0
    intersection = ta & tb
    union = ta | tb
    if not union:
        return 0.0
    return len(intersection) / len(union)
