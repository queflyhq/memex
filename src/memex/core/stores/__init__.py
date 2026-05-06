"""
Concrete store implementations.

The Protocol-typed interfaces these satisfy live in `memex.core.protocols`.
Engine takes Protocols, not concrete classes — so swapping (e.g.) the vector
backend to sqlite-vec is a Protocol-conforming new module, no Engine change.
"""

from memex.core.stores.episodic import SqliteEpisodicStore
from memex.core.stores.semantic import KuzuSemanticStore
from memex.core.stores.vector import NumpyVectorStore

__all__ = ["KuzuSemanticStore", "NumpyVectorStore", "SqliteEpisodicStore"]
