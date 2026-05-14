"""
Perception layer — events in.

Re-exports the observe-side primitives. Every external observation
(tool call, user prompt, file edit, correction) lands here as an
immutable event. Perception is append-only and cheap; never blocks
an AI tool call.

Layer in the 5-layer OMP architecture (see docs/architecture.md):
  PERCEPTION → context → cognition → memory ← action → (back to perception)
"""

from __future__ import annotations

from memex.core.schema import EpisodicEvent, Source

# The episodic store is the durable home of perception events. We
# import it from the memory layer (its physical home) so callers who
# think "perception" can find it, but the data still lives in memory.
from memex.core.stores.episodic import SqliteEpisodicStore  # noqa: F401

__all__ = [
    "EpisodicEvent",
    "Source",
    "SqliteEpisodicStore",
]
