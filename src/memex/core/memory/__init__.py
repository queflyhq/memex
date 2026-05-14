"""
Memory layer — durable typed graph.

The long-term store. Concepts (typed nodes), edges (typed relations),
vectors (384-d embeddings). One DuckDB file. Single-writer rule:
the daemon is the only mutator.

Layer in the 5-layer OMP architecture:
  perception → context → cognition ↔ MEMORY ← action

Memory is partitioned by `kind=source` concept (one per indexed
codebase / collection). Cross-source `same_as` edges federate
identical symbols transparently.
"""

from __future__ import annotations

from memex.core.stores.duckdb_store import (
    DuckDBStores,
    DuckDBSemanticStore,
    DuckDBVectorStore,
)
from memex.core.stores.episodic import SqliteEpisodicStore
from memex.core.schema import (
    Concept,
    Edge,
    EpisodicEvent,
    NodeKind,
    EdgeKind,
    Source,
    RecallResult,
)

__all__ = [
    # Stores
    "DuckDBStores",
    "DuckDBSemanticStore",
    "DuckDBVectorStore",
    "SqliteEpisodicStore",
    # Schema
    "Concept",
    "Edge",
    "EpisodicEvent",
    "NodeKind",
    "EdgeKind",
    "Source",
    "RecallResult",
]
