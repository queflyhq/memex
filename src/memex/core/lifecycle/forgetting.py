"""
Active forgetting — delete nodes that meet stale-and-orphaned criteria.

A concept is forgettable when ALL of:

  * `last_confirmed_at` older than `unused_days` (recency-decayed
    confidence has dropped below the floor by now)
  * decayed confidence below `min_confidence` floor
  * fewer than `max_edges` typed edges touching the node (low-graph-
    centrality — losing it doesn't break recall paths)
  * not in a protected kind set (constraints, decisions, projects,
    sources are never auto-forgotten — those are the load-bearing
    rules)

Returns the number of concepts removed. Idempotent and safe under
concurrent writes — the semantic store's delete is atomic.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from memex.core.lifecycle.decay import decayed_confidence
from memex.core.schema import NodeKind

log = logging.getLogger(__name__)


# Kinds that carry user-stated rules / structural primitives we never
# auto-delete. The user can manually delete these via the desktop or
# CLI, but the cleanup pass refuses to.
_PROTECTED_KINDS: set[NodeKind] = {
    NodeKind.constraint,
    NodeKind.decision,
    NodeKind.project,
    NodeKind.milestone,
    NodeKind.source,
    NodeKind.person,
}


# Per-durability TTL multipliers. A concept tagged `permanent` is never
# auto-forgotten; `sprint` is checked at sprint cadence; `session` is the
# default and gets the normal threshold.
_DURABILITY_TTL_MULTIPLIER: dict[str, float] = {
    "session":   1.0,
    "sprint":    3.0,    # 3x the normal TTL (~3 months at default config)
    "permanent": float("inf"),  # never forget
}


def run_forgetting(
    semantic,
    *,
    unused_days: int = 365,
    min_confidence: float = 0.05,
    max_edges: int = 0,
    half_life_days: float = 90.0,
    dry_run: bool = False,
) -> dict[str, int]:
    """Sweep the concept graph and forget stale-and-orphaned nodes.

    Args:
        semantic: SemanticStore protocol implementation.
        unused_days: only consider concepts whose `last_confirmed_at`
            is older than this many days.
        min_confidence: decayed-confidence threshold; concepts above
            this stay.
        max_edges: only consider concepts with ≤ this many edges
            touching them. Default 0 = "completely orphaned".
        half_life_days: feed to `decayed_confidence` so the threshold
            comparison uses time-decayed confidence, not stored.
        dry_run: count candidates without actually deleting.

    Returns:
        {"considered": N, "candidates": M, "deleted": D, "protected": P}
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=unused_days)

    considered = 0
    candidates: list[str] = []
    protected = 0
    from memex.core.schema import NodeKind as _NK
    for c in semantic.all_concepts():
        considered += 1
        # Ephemeral concepts: TTL via metadata.expires_at. If expired,
        # immediately a forgetting candidate regardless of edges/confidence.
        # If not yet expired, skip (not stale by definition).
        if c.kind == _NK.ephemeral:
            expires_raw = (c.metadata or {}).get("expires_at")
            if expires_raw:
                try:
                    if isinstance(expires_raw, str):
                        exp_dt = datetime.fromisoformat(expires_raw.replace("Z", "+00:00"))
                    else:
                        exp_dt = expires_raw
                    if exp_dt.tzinfo is None:
                        exp_dt = exp_dt.replace(tzinfo=timezone.utc)
                    if exp_dt < now:
                        candidates.append(c.id)
                except Exception:  # noqa: BLE001
                    pass
            continue
        if c.kind in _PROTECTED_KINDS:
            protected += 1
            continue
        # Durability check — never forget `permanent`, and stretch the TTL
        # for `sprint`. Default is `session` (normal TTL).
        durability = (c.metadata or {}).get("durability", "session")
        ttl_mult = _DURABILITY_TTL_MULTIPLIER.get(durability, 1.0)
        if ttl_mult == float("inf"):
            protected += 1
            continue
        # Recency check: only consider stale concepts (with durability stretch).
        confirmed = c.last_confirmed_at
        if confirmed.tzinfo is None:
            confirmed = confirmed.replace(tzinfo=timezone.utc)
        stretched_cutoff = now - timedelta(days=int(unused_days * ttl_mult))
        if confirmed >= stretched_cutoff:
            continue
        # Confidence check using current time-decayed value, not stored.
        d_conf = decayed_confidence(
            c.confidence, confirmed, now=now, half_life_days=half_life_days,
        )
        if d_conf >= min_confidence:
            continue
        # Edge-count check — orphans only by default.
        try:
            n_edges = len(semantic.edges_for(c.id))
        except Exception:  # noqa: BLE001
            n_edges = 0
        if n_edges > max_edges:
            continue
        candidates.append(c.id)

    deleted = 0
    if not dry_run:
        # Tombstone before delete: insert the concept blob into
        # concept_tombstones so it can be restored within the retention
        # window. Falls back to plain delete if the tombstone table
        # isn't available (legacy stores).
        import json as _json
        for cid in candidates:
            try:
                # Fetch the concept once so we have a blob to tombstone.
                c = semantic.get_concept(cid)
                if c is not None and hasattr(semantic, "conn") and hasattr(semantic, "_lock"):
                    try:
                        with semantic._lock:  # type: ignore[attr-defined]
                            semantic.conn.execute(  # type: ignore[attr-defined]
                                "INSERT OR REPLACE INTO concept_tombstones "
                                "(id, concept_blob, deleted_at, reason) VALUES (?, ?, CURRENT_TIMESTAMP, ?)",
                                [cid, _json.dumps(c.model_dump(mode="json"), default=str), "forgetting"],
                            )
                    except Exception as te:  # noqa: BLE001
                        log.debug("tombstone write skipped for %s: %s", cid, te)
                if semantic.delete_concept(cid):
                    deleted += 1
            except Exception as e:  # noqa: BLE001
                log.warning("forgetting: delete failed for %s: %s", cid, e)

    log.info(
        "forgetting: considered=%d, candidates=%d, deleted=%d, protected=%d",
        considered, len(candidates), deleted, protected,
    )
    return {
        "considered": considered,
        "candidates": len(candidates),
        "deleted": deleted,
        "protected": protected,
        "dry_run": dry_run,
    }
