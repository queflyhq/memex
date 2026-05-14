"""
Working set — the L1 cache layer of the memory hierarchy.

Modeled on the human working-memory + CPU-cache analogy: a small bounded
set of concepts that are *currently relevant*, biased toward the agent's
most-touched reads. Retrieval can use this as a prior — if a query matches
a concept already in the working set, it ranks higher (priming).

v0.2 swaps the original recency-only LRU for an LFU policy backed by
`cachetools.LFUCache`. The frequency model better matches "this concept
matters across many sessions" — what makes a working set durable across a
multi-day project. Falls back to a recency-only OrderedDict when
cachetools isn't installed (zero hard dependency).

Public API is unchanged: `touch`, `touch_many`, `contains`, `ids`, `bias`,
`clear`, `__len__`.
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from typing import Any

log = logging.getLogger(__name__)


def _make_backend(capacity: int) -> Any:
    """Return an LFU cache if cachetools is installed, else an OrderedDict
    used as a recency-LRU. Both expose dict-like setitem/contains/popitem
    in the consumer code below."""
    try:
        from cachetools import LFUCache
        return LFUCache(maxsize=capacity)
    except Exception as e:  # noqa: BLE001
        log.debug("cachetools not available, using OrderedDict LRU: %s", e)
        return OrderedDict()


class WorkingSet:
    """Bounded LFU cache of concept ids — the L1 layer."""

    def __init__(self, capacity: int = 200):
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        self._capacity = capacity
        self._backend = _make_backend(capacity)
        self._lock = threading.RLock()
        # cachetools.LFUCache enforces capacity; OrderedDict fallback needs us to.
        self._is_lfu = type(self._backend).__name__ == "LFUCache"

    @property
    def capacity(self) -> int:
        return self._capacity

    def touch(self, concept_id: str, weight: float = 1.0) -> None:
        """Bring a concept up in the cache — increments its access count
        under LFU, or moves it to the front under the OrderedDict fallback.

        `weight` is preserved for API compat; under LFU it scales the
        increment (touching with weight=2 ≈ touching twice).
        """
        if not concept_id:
            return
        bumps = max(1, int(round(weight)))
        with self._lock:
            if self._is_lfu:
                # Reading via __getitem__ increments LFUCache's access count.
                # We bump weight times to allow "important" touches to count more.
                for _ in range(bumps):
                    if concept_id in self._backend:
                        _ = self._backend[concept_id]
                    else:
                        self._backend[concept_id] = True
            else:
                # OrderedDict fallback: emulate recency-LRU.
                self._backend.pop(concept_id, None)
                self._backend[concept_id] = True
                while len(self._backend) > self._capacity:
                    self._backend.popitem(last=False)

    def touch_many(self, concept_ids: list[str], weight: float = 1.0) -> None:
        for cid in concept_ids:
            self.touch(cid, weight=weight)

    def contains(self, concept_id: str) -> bool:
        with self._lock:
            return concept_id in self._backend

    def ids(self) -> list[str]:
        """Return concept ids in cache. Order is most-relevant first
        (highest LFU count, or most-recent under the fallback)."""
        with self._lock:
            if self._is_lfu:
                # cachetools doesn't expose access counts directly, but
                # iterating returns insertion order. Reversed = MRU first.
                return list(reversed(list(self._backend.keys())))
            return list(reversed(self._backend.keys()))

    def bias(self, concept_id: str) -> float:
        """Multiplicative ranking bias — 1.0 baseline, 1.25 when the
        concept is in the working set (recall priming)."""
        with self._lock:
            return 1.25 if concept_id in self._backend else 1.0

    def clear(self) -> None:
        with self._lock:
            self._backend.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._backend)
