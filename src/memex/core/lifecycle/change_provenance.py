"""Change provenance — connect tasks/changes to the code they touched.

Memex captures the pieces separately:
  - Task concepts (TodoWrite items, manual additions)
  - File / symbol concepts (from code indexing)
  - tool_post episodic events (Claude Code Edit/Write/MultiEdit calls)

The pieces aren't linked. So "which task changed Login()?" has no answer
even though all the data is there.

This module runs as a periodic batch job: scan recent tool_post events
for Edit/Write/MultiEdit, find the file concept matching the touched
path, find the currently-active task, and add a `touches` edge.

Idempotent. Re-running is safe — an existing edge is detected and the
new write is skipped.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from memex.core.schema import Edge, EdgeKind, Source

if TYPE_CHECKING:
    from memex.core.engine import Engine

log = logging.getLogger(__name__)


_EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}


def link_recent_changes(
    engine: "Engine",
    *,
    lookback_events: int = 500,
    max_links: int = 200,
) -> dict[str, Any]:
    """Walk recent tool_post events; link active task -[touches]-> file/symbol.

    Returns a counts dict — caller can log it.
    """
    t0 = time.time()
    events = list(engine.episodic.recent(limit=lookback_events) or [])
    if not events:
        return {"events_scanned": 0, "links_added": 0, "elapsed_ms": 0.0}

    # Find the most recently active task. "Active" = status in
    # {pending, in_progress} and recently confirmed.
    active_task_id = _find_active_task(engine)
    if active_task_id is None:
        return {"events_scanned": len(events), "links_added": 0, "elapsed_ms": 0.0,
                "skipped_reason": "no active task"}

    # Cache existing touches edges from this task so we don't double-link.
    existing_targets = _existing_touches(engine, active_task_id)

    links_added = 0
    seen_paths: set[str] = set()
    for e in events:
        if links_added >= max_links:
            break
        if getattr(e, "kind", "") != "tool_post":
            continue
        payload = getattr(e, "payload", {}) or {}
        tool_name = payload.get("tool_name") or ""
        if tool_name not in _EDIT_TOOLS:
            continue
        tool_input = payload.get("tool_input") or {}
        path = tool_input.get("file_path") or tool_input.get("path")
        if not path or path in seen_paths:
            continue
        seen_paths.add(path)

        # Find file concept matching this path.
        file_id = _find_file_by_path(engine, path)
        if file_id is None or file_id in existing_targets:
            continue
        try:
            engine.semantic.add_edge(Edge(
                from_id=active_task_id, to_id=file_id,
                kind=EdgeKind.touches, source=Source.system,
                metadata={"linked_from": "change_provenance", "path": path},
            ))
            existing_targets.add(file_id)
            links_added += 1
        except Exception as exc:  # noqa: BLE001
            log.debug("touches edge failed: %s", exc)

    return {
        "events_scanned": len(events),
        "active_task": active_task_id,
        "links_added": links_added,
        "elapsed_ms": round((time.time() - t0) * 1000.0, 1),
    }


def _find_active_task(engine: "Engine") -> str | None:
    """Pick the task most likely to be the one the AI is working on right
    now. Heuristic: a `kind=task` with metadata.status in {pending,
    in_progress}, sorted by last_confirmed_at descending.
    """
    try:
        with engine.semantic._lock:  # type: ignore[attr-defined]
            rows = engine.semantic.conn.execute(  # type: ignore[attr-defined]
                "SELECT id FROM concepts "
                "WHERE kind = 'task' "
                "AND json_extract_string(metadata, '$.status') IN ('pending','in_progress') "
                "ORDER BY last_confirmed_at DESC NULLS LAST "
                "LIMIT 1",
            ).fetchone()
        if rows:
            return rows[0]
    except Exception as e:  # noqa: BLE001
        log.debug("_find_active_task failed: %s", e)
    return None


def _existing_touches(engine: "Engine", task_id: str) -> set[str]:
    """Set of concept ids this task already has touches edges to."""
    try:
        with engine.semantic._lock:  # type: ignore[attr-defined]
            rows = engine.semantic.conn.execute(  # type: ignore[attr-defined]
                "SELECT to_id FROM edges WHERE from_id = ? AND kind = 'touches'",
                [task_id],
            ).fetchall()
        return {r[0] for r in rows}
    except Exception:  # noqa: BLE001
        return set()


def _find_file_by_path(engine: "Engine", path: str) -> str | None:
    """Find the file concept whose metadata.path == this filesystem path.

    Supports both absolute and forward-slash variants (Windows mixes both).
    """
    candidates = {path, path.replace("\\", "/")}
    try:
        with engine.semantic._lock:  # type: ignore[attr-defined]
            for cand in candidates:
                row = engine.semantic.conn.execute(  # type: ignore[attr-defined]
                    "SELECT id FROM concepts "
                    "WHERE kind = 'file' "
                    "AND json_extract_string(metadata, '$.path') = ? "
                    "LIMIT 1",
                    [cand],
                ).fetchone()
                if row:
                    return row[0]
    except Exception:  # noqa: BLE001
        pass
    return None
