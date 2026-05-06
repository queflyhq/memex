"""
Interfaces for the swappable parts of memex.

Every storage and capability layer is defined as a Protocol so v0.2 swaps
(sqlite-vec, hnswlib, postgres-backed Kuzu, Qdrant for vectors, etc.) don't
require a rewrite. Tests inject in-memory fakes that satisfy the same
protocols.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    import numpy as np

    from memex.core.schema import (
        Concept,
        Edge,
        EdgeKind,
        EpisodicEvent,
        NodeKind,
        RecallResult,
    )


@runtime_checkable
class SemanticStore(Protocol):
    """Concept graph: typed nodes + typed edges with provenance + confidence."""

    def add_concept(self, c: Concept) -> str: ...
    def add_edge(self, e: Edge) -> None: ...
    def get_concept(self, concept_id: str) -> Concept | None: ...
    def all_concepts(self) -> list[Concept]: ...
    def neighbors(
        self,
        concept_id: str,
        depth: int = 1,
        kinds: list[EdgeKind] | None = None,
    ) -> tuple[list[Concept], list[Edge]]: ...
    def search_lexical(
        self,
        query: str,
        limit: int = 50,
        kind: NodeKind | None = None,
    ) -> list[Concept]: ...
    def count(self) -> int: ...
    def close(self) -> None: ...


@runtime_checkable
class EpisodicStore(Protocol):
    """Time-indexed event log."""

    def append(self, event: EpisodicEvent) -> str: ...
    def recent(self, limit: int = 100, kind: str | None = None) -> list[EpisodicEvent]: ...
    def count(self) -> int: ...
    def close(self) -> None: ...


@runtime_checkable
class VectorStore(Protocol):
    """Approximate-nearest-neighbor index over concept embeddings."""

    dim: int

    def add(self, concept_id: str, embedding: np.ndarray) -> None: ...
    def remove(self, concept_id: str) -> None: ...
    def search(self, query: np.ndarray, limit: int = 20) -> list[tuple[str, float]]: ...
    def count(self) -> int: ...
    def close(self) -> None: ...


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Pluggable embedding model — `NoOp` when Tier 1 isn't installed."""

    dim: int

    def is_available(self) -> bool: ...
    def embed(self, text: str) -> np.ndarray: ...
    def embed_batch(self, texts: list[str]) -> list[np.ndarray]: ...


@runtime_checkable
class RetrievalStrategy(Protocol):
    """A strategy that turns a query into a budget-bounded subgraph."""

    def recall(
        self,
        query: str,
        budget_tokens: int = 2000,
        kind: NodeKind | None = None,
        expand_hops: int = 1,
        embed_query: list[float] | None = None,
    ) -> RecallResult: ...


class StoreInvalidationListener(Protocol):
    """Hook called when the semantic store mutates — used to invalidate caches."""

    def on_semantic_change(self) -> None: ...


# Re-export common Any binding so callers don't need typing.Any imports
__all__ = [
    "Any",
    "EmbeddingProvider",
    "EpisodicStore",
    "RetrievalStrategy",
    "SemanticStore",
    "StoreInvalidationListener",
    "VectorStore",
]
