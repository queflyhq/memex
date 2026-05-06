"""
Working set — the L1 cache layer of the memory hierarchy.

Modeled on the human working-memory + CPU-cache analogy: a small bounded
set of concepts that are *currently relevant*, biased toward the agent's
recent reads. Retrieval can use this as a prior — if a query matches a
concept already in the working set, it ranks higher (priming).

This is intentionally simple at v0.1: an LRU-like recency cache of concept
ids, capped in size. Future versions can promote/demote based on retrieval
frequency, dwell time, or explicit "focus" hints from the editor (cwd, open
file, current symbol).
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict


class WorkingSet:
    """Bounded recency cache of concept ids — the L1 layer."""

    def __init__(self, capacity: int = 64):
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        self._capacity = capacity
        self._items: OrderedDict[str, float] = OrderedDict()
        self._lock = threading.RLock()

    @property
    def capacity(self) -> int:
        return self._capacity

    def touch(self, concept_id: str, weight: float = 1.0) -> None:
        """Bring a concept to the front (most-recently-used)."""
        with self._lock:
            now = time.time()
            self._items.pop(concept_id, None)
            self._items[concept_id] = now * weight
            while len(self._items) > self._capacity:
                self._items.popitem(last=False)

    def touch_many(self, concept_ids: list[str], weight: float = 1.0) -> None:
        for cid in concept_ids:
            self.touch(cid, weight=weight)

    def contains(self, concept_id: str) -> bool:
        with self._lock:
            return concept_id in self._items

    def ids(self) -> list[str]:
        with self._lock:
            return list(reversed(self._items.keys()))

    def bias(self, concept_id: str) -> float:
        """Return a multiplicative bias for retrieval ranking — 1.0 baseline,
        higher when the concept is in the working set (priming).
        """
        with self._lock:
            return 1.25 if concept_id in self._items else 1.0

    def clear(self) -> None:
        with self._lock:
            self._items.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)
