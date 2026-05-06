"""
Active forgetting — delete nodes that meet stale-and-orphaned criteria.

A node is forgettable if all of:
  - last retrieved more than `unused_days` ago
  - no incoming or outgoing high-confidence edges
  - last_confirmed_at older than `unused_days`

v0.1: scaffold only. Retrieval-frequency tracking lands in v0.4.
"""

from __future__ import annotations

from memex.core.stores.semantic import SemanticStore


def run_forgetting(
    semantic: SemanticStore,
    *,
    unused_days: int = 365,
    min_confidence: float = 0.05,
) -> int:
    """Returns number of nodes forgotten. v0.1 stub: returns 0."""
    _ = (semantic, unused_days, min_confidence)
    return 0
