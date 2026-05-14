"""LLM-summarized weekly digest.

The episodic event stream is voluminous (~70K events in this user's graph).
A user can't read 10K events from the last week. But "what did my project
shift on this week?" is the kind of answer LLMs are good at synthesizing.

This job runs once a week (every Monday morning by default) and:

  1. Pulls all significant events from the last 7 days
  2. Asks the configured LLM to summarize into 5–10 bullet points
  3. Stores the result as a `kind=fact` concept tagged `kind=digest`
     so it's recallable: "what happened last week" returns this concept
  4. Records an event so the user can see the digest in the activity feed

Gated on LLM availability. With NoOpLLMProvider, this job is a no-op
(logs and exits). The result is stored even with shallow LLMs — but the
text quality scales with the model.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from memex.core.schema import Concept, EpisodicEvent, NodeKind, Source

if TYPE_CHECKING:
    from memex.core.engine import Engine

log = logging.getLogger(__name__)


# Events worth summarizing — drop the noise (tool_pre, context_injected)
# and keep the things that represent meaningful project state.
_DIGEST_KINDS = {
    "concept_created", "concept_revised", "decision_recorded",
    "task_completed", "rule_added", "user_correction",
    "pattern_promoted", "gate_denied", "edge_added",
}


def run_weekly_digest(
    engine: "Engine",
    *,
    window_days: int = 7,
    max_events_to_llm: int = 60,
) -> dict[str, Any]:
    """Compose + store a weekly digest. Returns counts.

    If LLM is unavailable, falls back to a heuristic summary built from
    event counts (no prose narrative, but at least useful numbers).
    """
    t0 = time.time()
    if getattr(engine, "llm", None) is None or not engine.llm.is_available():
        # Heuristic fallback — no narrative, just counts. Still useful.
        return _run_heuristic_digest(engine, window_days)

    # Pull events.
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    raw = list(engine.episodic.recent(limit=max_events_to_llm * 4) or [])
    events = [
        e for e in raw
        if getattr(e, "kind", "") in _DIGEST_KINDS
        and getattr(e, "timestamp", cutoff) >= cutoff
    ][:max_events_to_llm]
    if not events:
        log.info("weekly_digest: no events in window — skipping")
        return {"summarized": 0, "skipped_reason": "empty window"}

    # Build a compact LLM prompt: one line per event.
    lines = []
    for e in events:
        payload = getattr(e, "payload", {}) or {}
        name = payload.get("name") or payload.get("intent") or payload.get("tool_name") or ""
        lines.append(f"- [{getattr(e, 'kind', '?')}] {name[:120]}")
    prompt = (
        "Summarize what happened in this developer's project this past week. "
        "Below is a list of significant events. Produce 5–10 bullet points capturing "
        "the most important shifts: decisions made, patterns that emerged, "
        "tasks completed, things that got blocked. Concise. No filler.\n\n"
        f"Events ({len(events)}):\n" + "\n".join(lines)
    )
    try:
        summary = engine.llm.generate(
            prompt, max_tokens=600, temperature=0.3,
            system="You write concise, accurate weekly digests for developers.",
        )
    except Exception as e:  # noqa: BLE001
        log.warning("weekly_digest LLM call failed: %s", e)
        return _run_heuristic_digest(engine, window_days)

    summary = (summary or "").strip()
    if not summary:
        return {"summarized": 0, "skipped_reason": "empty LLM response"}

    # Persist as a fact concept tagged for recall.
    digest_name = f"Weekly digest — {datetime.now(timezone.utc).strftime('%Y-%m-%d')}"
    try:
        engine.semantic.add_concept(Concept(
            name=digest_name,
            kind=NodeKind.fact,
            description=summary[:4000],
            source=Source.system,
            confidence=0.85,
            metadata={
                "digest_kind": "weekly",
                "window_days": window_days,
                "event_count": len(events),
                "generated_by": getattr(engine.llm, "name", "llm"),
                "durability": "permanent",  # digests are immutable history
            },
        ))
        engine.observe(
            kind="weekly_digest_generated",
            actor=Source.system,
            payload={
                "name": digest_name,
                "event_count": len(events),
                "summary_chars": len(summary),
            },
        )
    except Exception as e:  # noqa: BLE001
        log.warning("weekly_digest persist failed: %s", e)
        return {"summarized": 0, "error": str(e)}

    return {
        "summarized": 1,
        "events_included": len(events),
        "summary_chars": len(summary),
        "elapsed_ms": round((time.time() - t0) * 1000.0, 1),
    }


def _run_heuristic_digest(engine: "Engine", window_days: int) -> dict[str, Any]:
    """Fallback when no LLM is available — count-only summary."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    raw = list(engine.episodic.recent(limit=2000) or [])
    in_window = [e for e in raw if getattr(e, "timestamp", cutoff) >= cutoff]
    by_kind: dict[str, int] = {}
    for e in in_window:
        k = getattr(e, "kind", "?")
        by_kind[k] = by_kind.get(k, 0) + 1
    notable = [
        (k, v) for k, v in by_kind.items()
        if k in _DIGEST_KINDS or v > 5
    ]
    notable.sort(key=lambda kv: -kv[1])
    bullets = [f"• {v} {k.replace('_', ' ')}" for k, v in notable[:10]]
    summary = (
        f"Heuristic digest ({len(in_window)} events in last {window_days}d).\n"
        + "\n".join(bullets)
        + "\n\n(Configure an LLM provider for a richer narrative summary.)"
    )
    try:
        digest_name = f"Weekly digest — {datetime.now(timezone.utc).strftime('%Y-%m-%d')}"
        engine.semantic.add_concept(Concept(
            name=digest_name,
            kind=NodeKind.fact,
            description=summary,
            source=Source.system,
            confidence=0.6,
            metadata={
                "digest_kind": "weekly",
                "window_days": window_days,
                "event_count": len(in_window),
                "generated_by": "heuristic",
                "durability": "permanent",
            },
        ))
    except Exception as e:  # noqa: BLE001
        log.warning("heuristic digest persist failed: %s", e)
    return {"summarized": 1, "mode": "heuristic", "events": len(in_window)}
