"""
Consolidation pass — episodic → semantic promotion + dedup.

Periodic background job. Two responsibilities:

  1. **Dedup**: detect concepts whose names normalize to the same key
     (case-fold + collapse whitespace/punctuation) within the same kind.
     Link them via `same_as` edges and bump confidence on the most-
     confirmed one. We do NOT auto-merge — provenance is preserved per
     `source` so the original author is still attributable.

  2. **Pattern promotion**: scan recent `recall_executed` events for
     concept ids that co-occur in N+ recalls in the same time window.
     Link them via `relates_to` edges if not already connected. Future
     v0.7 enhancement: use the LLM hook to synthesize a parent concept.

This is the "sleep cycle" — runs periodically (cleanup_interval_seconds
in Settings; default daily) so the graph self-organizes between
sessions instead of accumulating entropy.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from memex.core.schema import Concept, Edge, EdgeKind, EpisodicEvent, NodeKind, Source

log = logging.getLogger(__name__)


_NORM_RE = re.compile(r"[\s\W_]+", re.UNICODE)


def _normalize_name(s: str) -> str:
    return _NORM_RE.sub("", (s or "").lower())


# Kinds we run name-dedup on. Symbol/file are managed by the code indexer
# (different identity rule — qualified name + source). Project/source/
# milestone are structural — we leave them alone too.
_DEDUP_KINDS: set[NodeKind] = {
    NodeKind.fact,
    NodeKind.decision,
    NodeKind.constraint,
    NodeKind.action_constraint,
    NodeKind.approach,
    NodeKind.pattern,
    NodeKind.opinion,
    NodeKind.note,
    NodeKind.task,
    NodeKind.person,
}


def run_consolidation(
    episodic,
    semantic,
    *,
    min_frequency: int = 3,
    cooccurrence_window_events: int = 200,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Run one consolidation pass.

    Args:
      episodic: EpisodicStore — read recent events for pattern detection.
      semantic: SemanticStore — read concepts, write same_as / relates_to edges.
      min_frequency: minimum co-occurrence count to emit a relates_to edge.
      cooccurrence_window_events: how many recent `recall_executed` events
        to scan for pattern promotion.
      dry_run: scan + report without writing.

    Returns:
      {
        same_as_links: int,   # name-dedup edges created
        cooccurrence_links: int,  # pattern-promotion edges created
        confidence_bumps: int,
        dry_run: bool,
        elapsed_ms: float,
      }
    """
    t0 = datetime.now(timezone.utc)
    out: dict[str, Any] = {
        "same_as_links": 0,
        "cooccurrence_links": 0,
        "confidence_bumps": 0,
        "groups_found": 0,
        "dry_run": dry_run,
    }

    # ---- 1. Name-dedup -------------------------------------------------
    by_norm: dict[tuple[str, NodeKind], list[Concept]] = defaultdict(list)
    for c in semantic.all_concepts():
        if c.kind not in _DEDUP_KINDS:
            continue
        norm = _normalize_name(c.name)
        if not norm:
            continue
        by_norm[(norm, c.kind)].append(c)

    for (_norm, _kind), group in by_norm.items():
        if len(group) < 2:
            continue
        out["groups_found"] += 1
        # Sort by confidence desc, then last_confirmed_at desc so the
        # canonical one is first. Subsequent members link to it.
        group.sort(key=lambda c: (c.confidence, c.last_confirmed_at), reverse=True)
        canonical = group[0]
        # Existing same_as edges so we don't duplicate.
        existing_edges = {(e.from_id, e.to_id): e for e in semantic.edges_for(canonical.id)}
        for dup in group[1:]:
            key1 = (dup.id, canonical.id)
            key2 = (canonical.id, dup.id)
            if key1 in existing_edges or key2 in existing_edges:
                continue
            if dry_run:
                out["same_as_links"] += 1
                continue
            try:
                semantic.add_edge(Edge(
                    from_id=dup.id,
                    to_id=canonical.id,
                    kind=EdgeKind.same_as,
                    source=Source.system,
                    confidence=0.9,
                    metadata={"reason": "consolidation:name_dedup"},
                ))
                out["same_as_links"] += 1
            except Exception as e:  # noqa: BLE001
                log.warning(
                    "consolidation: same_as edge failed (%s → %s): %s",
                    dup.id, canonical.id, e,
                )

    # ---- 2. Co-occurrence pattern promotion ---------------------------
    # For each recent recall, the set of recalled_ids forms a "scene".
    # Pairs that co-appear in many scenes are semantically related.
    pair_counts: dict[tuple[str, str], int] = defaultdict(int)
    scanned_events = 0
    for ev in episodic.recent(limit=cooccurrence_window_events):
        if ev.kind != "recall_executed":
            continue
        ids = list((ev.payload or {}).get("recalled_ids") or [])
        if len(ids) < 2:
            continue
        scanned_events += 1
        # Cap per-event combinatorial blow-up.
        ids = ids[:8]
        for i, a in enumerate(ids):
            for b in ids[i + 1:]:
                pair = (a, b) if a < b else (b, a)
                pair_counts[pair] += 1
    out["recall_events_scanned"] = scanned_events

    for (a, b), cnt in pair_counts.items():
        if cnt < min_frequency:
            continue
        # Skip if already linked in either direction.
        try:
            ed = {(e.from_id, e.to_id) for e in semantic.edges_for(a)}
        except Exception:  # noqa: BLE001
            continue
        if (a, b) in ed or (b, a) in ed:
            continue
        if dry_run:
            out["cooccurrence_links"] += 1
            continue
        try:
            semantic.add_edge(Edge(
                from_id=a, to_id=b,
                kind=EdgeKind.relates_to,
                source=Source.system,
                confidence=min(0.95, 0.5 + 0.05 * cnt),
                metadata={
                    "reason": "consolidation:cooccurrence",
                    "support": cnt,
                },
            ))
            out["cooccurrence_links"] += 1
        except Exception as e:  # noqa: BLE001
            log.warning(
                "consolidation: relates_to edge failed: %s", e,
            )

    out["elapsed_ms"] = (datetime.now(timezone.utc) - t0).total_seconds() * 1000.0

    # Audit event so /stats consolidations counter advances.
    if not dry_run:
        try:
            episodic.append(EpisodicEvent(
                kind="consolidation_run",
                actor=Source.system,
                payload=dict(out),
            ))
        except Exception as e:  # noqa: BLE001
            log.warning("consolidation: audit event append failed: %s", e)

    log.info(
        "consolidation: same_as=%d cooccur=%d groups=%d events_scanned=%d %s",
        out["same_as_links"], out["cooccurrence_links"],
        out["groups_found"], scanned_events,
        "(dry)" if dry_run else "",
    )
    return out
