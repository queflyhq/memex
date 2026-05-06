"""
Verification pass — re-check falsifiable claims against ground truth.

Every Concept may carry a `verification` predicate (a Cypher snippet, regex
against a file, HTTP probe URL). The verification pass evaluates the predicate
and updates `last_confirmed_at` + `confidence` accordingly.

v0.1: scaffold only. Real predicate executors land in v0.5.
"""

from __future__ import annotations

from memex.core.stores.semantic import SemanticStore


def run_verification(semantic: SemanticStore) -> dict[str, int]:
    """Returns counts: {confirmed, refuted, skipped}. v0.1 stub: all zero."""
    _ = semantic
    # TODO(v0.5): per-predicate-type executors (cypher / regex / http).
    return {"confirmed": 0, "refuted": 0, "skipped": 0}
