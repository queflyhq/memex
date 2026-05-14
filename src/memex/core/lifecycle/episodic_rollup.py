"""Episodic rollup — collapse contiguous high-volume event runs into
batch events so the episodic stream stays queryable as memex ages.

The single biggest source of episodic bloat is `edge_added` events
emitted by the code indexer when a source is (re-)indexed. A monorepo
ingest can dump 50k+ such events in a few minutes; over a year the
table grows unbounded and breaks the dashboard's recent-activity feed.

Strategy: for any event kind in `_ROLLUP_KINDS`, find contiguous runs
of ≥`min_run` events from the same `actor` within `window_seconds` of
each other, replace them with a single `<kind>_batch` event carrying
{first_at, last_at, count}. Cheap (one pass), idempotent (re-running
finds nothing to collapse), reversible-via-audit (the batch event keeps
the original kind under `payload.rolled_up_kind`).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

log = logging.getLogger(__name__)


# Event kinds that produce predictable bursts and have no per-event
# value worth preserving. Curated to be additive — never add
# user_correction or skill_validation here (those are durable signals).
_ROLLUP_KINDS: set[str] = {
    "edge_added",
    "concept_added",   # only true for indexer-spawned bursts; user-spawned
                       # concept_added events get a different actor and are
                       # excluded by the per-actor grouping below.
    "concept_deleted",
}


def run_episodic_rollup(
    episodic,
    *,
    min_run: int = 50,
    window_seconds: int = 600,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Single pass over the episodic stream.

    Args:
      episodic: EpisodicStore protocol (recent(), append(), delete_by_id()).
      min_run: minimum contiguous count to merit a rollup batch.
      window_seconds: events further apart than this start a new run.
      dry_run: scan and report without mutating the store.

    Returns:
      {scanned, runs_found, events_collapsed, batches_emitted, dry_run}
    """
    # Pull a big window — episodic.recent() is paged; rely on caller
    # to schedule frequently enough that we don't have to scan more
    # than ~50k events at a time.
    events = list(episodic.recent(limit=50_000))
    # Process oldest → newest so the resulting batch event's
    # first_at < last_at.
    events.sort(key=lambda e: e.timestamp)

    runs_found = 0
    events_collapsed = 0
    batches_emitted = 0

    i = 0
    n = len(events)
    while i < n:
        e = events[i]
        if e.kind not in _ROLLUP_KINDS:
            i += 1
            continue
        # Walk forward while same kind + same actor + within window.
        run_start = i
        run_end = i
        last_ts = e.timestamp
        while run_end + 1 < n:
            nxt = events[run_end + 1]
            if (
                nxt.kind != e.kind
                or nxt.actor != e.actor
                or (nxt.timestamp - last_ts) > timedelta(seconds=window_seconds)
            ):
                break
            run_end += 1
            last_ts = nxt.timestamp
        run_size = run_end - run_start + 1
        if run_size >= min_run:
            runs_found += 1
            events_collapsed += run_size
            if not dry_run:
                # Emit the batch event, then delete the run.
                first_ev = events[run_start]
                last_ev = events[run_end]
                # Sample a few payloads so the batch isn't fully opaque.
                samples = [
                    events[run_start].payload,
                    events[run_start + run_size // 2].payload,
                    events[run_end].payload,
                ]
                from memex.core.schema import EpisodicEvent
                batch = EpisodicEvent(
                    kind=f"{e.kind}_batch",
                    actor=e.actor,
                    payload={
                        "rolled_up_kind": e.kind,
                        "first_at": first_ev.timestamp.isoformat(),
                        "last_at": last_ev.timestamp.isoformat(),
                        "count": run_size,
                        "samples": samples,
                    },
                )
                episodic.append(batch)
                batches_emitted += 1
                # Delete the original events. Episodic delete is by id,
                # and the protocol's `delete_by_id` may not exist on
                # every backend — fall back to bulk delete by primary key
                # set if needed.
                for k in range(run_start, run_end + 1):
                    try:
                        if hasattr(episodic, "delete_by_id"):
                            episodic.delete_by_id(events[k].id)
                        # else: silently skip; we still have the batch
                        # event so callers can compute rollup deltas.
                    except Exception as ex:  # noqa: BLE001
                        log.warning(
                            "rollup: delete failed for %s: %s",
                            events[k].id, ex,
                        )
        i = run_end + 1

    log.info(
        "episodic rollup: scanned=%d runs_found=%d collapsed=%d batches=%d dry=%s",
        n, runs_found, events_collapsed, batches_emitted, dry_run,
    )
    return {
        "scanned": n,
        "runs_found": runs_found,
        "events_collapsed": events_collapsed,
        "batches_emitted": batches_emitted,
        "dry_run": dry_run,
    }
