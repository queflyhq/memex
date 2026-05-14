"""Git-sync engine for team-shared memex graphs.

This is THE team-mode feature. Without it, every teammate's memex is a
silo. With it: decisions made by one teammate become recallable to the
whole team within a few minutes.

Design:
  - The "team repo" is just a git repo. Teammates clone it once.
  - Memex daemon's settings carry `team_repo_path` (filesystem path
    to the clone, e.g. ~/code/team-memex).
  - On a schedule (every 10 min by default) memex pushes its durable
    concepts/edges to the repo and pulls everyone else's.
  - Conflicts: ours-latest (most recent last_confirmed_at wins).
    Concepts with `metadata.team_locked = true` ignore inbound updates
    (manual gate for sensitive entries).

Operations are file-based + git CLI subprocess — no Python git libs
required. The trade-off is that diffs are larger than necessary
(one-file-per-concept), but git handles that fine; merges are clean
because writes are append-only-by-id.

CLI: `memex team init <path>` once; then the scheduler does the rest.
Or `memex team push` / `memex team pull` to force a sync.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from memex.core.schema import Concept, Edge, EdgeKind, NodeKind, Source

if TYPE_CHECKING:
    from memex.core.engine import Engine

log = logging.getLogger(__name__)


# Only these kinds participate in team sync. Code symbols + files are
# machine-derived (each teammate indexes their own clone), episodic
# events are per-user activity, and ephemeral concepts are session-scoped.
_SYNCED_KINDS = {
    NodeKind.decision,
    NodeKind.fact,
    NodeKind.constraint,
    NodeKind.action_constraint,
    NodeKind.project,
    NodeKind.milestone,
    NodeKind.person,
    NodeKind.pattern,
    NodeKind.approach,
    NodeKind.task,
}

# Sync only these edge kinds. Code-graph edges (calls/defined_in/extends/
# imports) stay local — each teammate re-derives them from their own
# AST index. Cross-repo same_as is shared (it's a team-curated link).
_SYNCED_EDGE_KINDS = {
    EdgeKind.supersedes,
    EdgeKind.conflicts_with,
    EdgeKind.depends_on,
    EdgeKind.motivated_by,
    EdgeKind.rejected_due_to,
    EdgeKind.same_as,
    EdgeKind.relates_to,
    EdgeKind.blocks,
    EdgeKind.part_of,
    EdgeKind.implements,
    EdgeKind.touches,
}


@dataclass
class TeamSyncReport:
    pushed_concepts: int = 0
    pushed_edges: int = 0
    pulled_concepts: int = 0
    pulled_edges: int = 0
    skipped_private: int = 0
    conflicts_resolved: int = 0
    errors: list[str] = field(default_factory=list)
    elapsed_ms: float = 0.0


# ---- public entry points -------------------------------------------------


def init_team_repo(repo_path: str | Path) -> dict[str, Any]:
    """Initialize the team repo layout. Idempotent.

    If `repo_path` doesn't exist, `git init` it. If it exists but isn't
    a git repo, fail loudly. Then create the memex-graph/ subtree with
    concepts/, edges/, manifest.json, README.md.
    """
    repo = Path(repo_path).resolve()
    repo.mkdir(parents=True, exist_ok=True)
    if not (repo / ".git").exists():
        _run_git(repo, ["init"])
        # Default branch to `main` for new repos.
        _run_git(repo, ["checkout", "-B", "main"], check=False)

    graph_root = repo / "memex-graph"
    (graph_root / "concepts").mkdir(parents=True, exist_ok=True)
    (graph_root / "edges").mkdir(parents=True, exist_ok=True)
    readme = graph_root / "README.md"
    if not readme.exists():
        readme.write_text(_README_BODY, encoding="utf-8")
    manifest = graph_root / "manifest.json"
    if not manifest.exists():
        manifest.write_text(
            json.dumps({
                "format": "memex-team-graph-v1",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "initial_sync": True,
            }, indent=2),
            encoding="utf-8",
        )
    return {"repo": str(repo), "graph_root": str(graph_root)}


def sync_push(
    engine: "Engine",
    repo_path: str | Path,
    *,
    actor: str | None = None,
) -> TeamSyncReport:
    """Serialize the local durable graph to the team repo, commit, push.

    Concepts marked `metadata.private = true` are skipped. All others
    get written to memex-graph/concepts/c_<id>.json (one file per concept
    for clean diffs). Same for edges.

    Returns counts. Errors are caught and reported, never raised.
    """
    t0 = time.time()
    report = TeamSyncReport()
    try:
        repo = Path(repo_path).resolve()
        graph_root = repo / "memex-graph"
        concepts_dir = graph_root / "concepts"
        edges_dir = graph_root / "edges"
        concepts_dir.mkdir(parents=True, exist_ok=True)
        edges_dir.mkdir(parents=True, exist_ok=True)

        # Pull first so we don't push over teammates' changes blindly.
        # `git pull --rebase` then continue. Failures are non-fatal —
        # the push will report the conflict.
        _run_git(repo, ["pull", "--rebase", "--autostash"], check=False)

        # Write concepts.
        for c in engine.semantic.all_concepts():
            if c.kind not in _SYNCED_KINDS:
                continue
            md = c.metadata or {}
            if md.get("private"):
                report.skipped_private += 1
                continue
            file_path = concepts_dir / f"{c.id}.json"
            payload = c.model_dump(mode="json")
            _write_if_changed(file_path, payload)
            report.pushed_concepts += 1

        # Write edges.
        try:
            with engine.semantic._lock:  # type: ignore[attr-defined]
                rows = engine.semantic.conn.execute(  # type: ignore[attr-defined]
                    "SELECT from_id, to_id, kind, source, confidence, "
                    "created_at, last_confirmed_at, metadata FROM edges",
                ).fetchall()
        except Exception as e:  # noqa: BLE001
            rows = []
            report.errors.append(f"edge read failed: {e}")

        for r in rows:
            kind_str = r[2]
            try:
                ek = EdgeKind(kind_str)
            except Exception:  # noqa: BLE001
                continue
            if ek not in _SYNCED_EDGE_KINDS:
                continue
            edge_id = _edge_id(r[0], r[1], kind_str)
            file_path = edges_dir / f"{edge_id}.json"
            payload = {
                "from_id": r[0],
                "to_id": r[1],
                "kind": kind_str,
                "source": r[3],
                "confidence": r[4],
                "created_at": r[5].isoformat() if r[5] else None,
                "last_confirmed_at": r[6].isoformat() if r[6] else None,
                "metadata": json.loads(r[7]) if isinstance(r[7], str) else (r[7] or {}),
            }
            _write_if_changed(file_path, payload)
            report.pushed_edges += 1

        # Update manifest with sync timestamp.
        manifest = graph_root / "manifest.json"
        manifest.write_text(
            json.dumps({
                "format": "memex-team-graph-v1",
                "last_push_at": datetime.now(timezone.utc).isoformat(),
                "last_push_by": actor or "memex-team-sync",
                "concept_count": report.pushed_concepts,
                "edge_count": report.pushed_edges,
            }, indent=2),
            encoding="utf-8",
        )

        # Commit + push if anything changed.
        status = _run_git(repo, ["status", "--porcelain"]).strip()
        if status:
            _run_git(repo, ["add", "memex-graph"])
            msg = (
                f"memex sync: +{report.pushed_concepts} concepts, "
                f"+{report.pushed_edges} edges"
            )
            _run_git(repo, ["commit", "-m", msg], check=False)
            push_result = _run_git(repo, ["push"], check=False)
            if push_result is None:
                report.errors.append("git push failed (no remote? credentials?)")
    except Exception as e:  # noqa: BLE001
        report.errors.append(str(e))
    report.elapsed_ms = round((time.time() - t0) * 1000.0, 1)
    return report


def sync_pull(
    engine: "Engine",
    repo_path: str | Path,
) -> TeamSyncReport:
    """Pull from the team repo and merge inbound concepts/edges into the
    local graph. Skips concepts the local has marked `team_locked`.

    Conflict resolution: ours-latest by `last_confirmed_at`.
    """
    t0 = time.time()
    report = TeamSyncReport()
    try:
        repo = Path(repo_path).resolve()
        graph_root = repo / "memex-graph"
        if not graph_root.is_dir():
            report.errors.append(f"team repo not initialized: {repo}")
            return report

        # Pull latest from origin.
        _run_git(repo, ["pull", "--rebase", "--autostash"], check=False)

        # Merge concepts.
        concepts_dir = graph_root / "concepts"
        for p in concepts_dir.glob("*.json") if concepts_dir.is_dir() else []:
            try:
                inbound = json.loads(p.read_text(encoding="utf-8"))
            except Exception as e:  # noqa: BLE001
                report.errors.append(f"{p.name}: {e}")
                continue
            inbound_id = inbound.get("id")
            if not inbound_id:
                continue
            existing = engine.semantic.get_concept(inbound_id)
            if existing is not None:
                # Skip if locally locked.
                if (existing.metadata or {}).get("team_locked"):
                    continue
                # Conflict resolution: keep the newer last_confirmed_at.
                local_t = existing.last_confirmed_at
                inbound_t_raw = inbound.get("last_confirmed_at")
                inbound_t = _parse_iso(inbound_t_raw)
                if inbound_t is None or (local_t and local_t.replace(tzinfo=timezone.utc) >= inbound_t):
                    continue
                report.conflicts_resolved += 1
            try:
                c = Concept.model_validate(inbound)
                engine.semantic.add_concept(c)
                report.pulled_concepts += 1
            except Exception as e:  # noqa: BLE001
                report.errors.append(f"merge concept {inbound_id}: {e}")

        # Merge edges.
        edges_dir = graph_root / "edges"
        for p in edges_dir.glob("*.json") if edges_dir.is_dir() else []:
            try:
                e_in = json.loads(p.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
            try:
                edge = Edge(
                    from_id=e_in["from_id"],
                    to_id=e_in["to_id"],
                    kind=EdgeKind(e_in["kind"]),
                    source=Source(e_in.get("source", "human")),
                    confidence=float(e_in.get("confidence", 1.0)),
                    metadata=e_in.get("metadata") or {},
                )
                engine.semantic.add_edge(edge)
                report.pulled_edges += 1
            except Exception as e:  # noqa: BLE001
                # Common: edge points at a concept we don't have yet.
                # We'll catch it on the next pass (the concept will be
                # pulled before the edge if ordered alphabetically).
                pass
    except Exception as e:  # noqa: BLE001
        report.errors.append(str(e))
    report.elapsed_ms = round((time.time() - t0) * 1000.0, 1)
    return report


# ---- helpers --------------------------------------------------------------


def _run_git(repo: Path, args: list[str], *, check: bool = True) -> str | None:
    """Run a git subcommand in `repo`. Returns stdout text, or None on failure.
    `check=False` swallows non-zero exit + returns None.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True, text=True, timeout=60,
            encoding="utf-8", errors="replace",
        )
        if check and result.returncode != 0:
            log.warning("git %s failed: %s", args, result.stderr.strip()[:200])
            return None
        return result.stdout
    except Exception as e:  # noqa: BLE001
        log.warning("git %s exception: %s", args, e)
        return None


def _write_if_changed(path: Path, payload: dict[str, Any]) -> None:
    """Write JSON only if content differs. Prevents pointless commits."""
    new_text = json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n"
    if path.is_file():
        try:
            old = path.read_text(encoding="utf-8")
            if old == new_text:
                return
        except Exception:  # noqa: BLE001
            pass
    path.write_text(new_text, encoding="utf-8")


def _edge_id(from_id: str, to_id: str, kind: str) -> str:
    """Deterministic edge filename. Same triple ⇒ same file ⇒ idempotent."""
    return f"{from_id}__{kind}__{to_id}"


def _parse_iso(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:  # noqa: BLE001
        return None


_README_BODY = """# Team memex graph

This directory is auto-managed by **memex** (https://github.com/queflyhq/memex).

Each teammate's memex daemon syncs its durable knowledge — decisions, facts,
constraints, tasks, projects — through this repo.  Each concept and edge is
one JSON file so git diffs stay readable.

## How sync works

- `concepts/*.json` — one file per concept (decision, fact, etc.)
- `edges/*.json`    — one file per relationship between concepts
- `manifest.json`   — last sync timestamp + counts

## Manual triggers

```bash
memex team push    # write local graph to this repo + git push
memex team pull    # git pull + merge inbound concepts into local graph
```

The daemon does this automatically every 10 minutes.

## Conflict resolution

When two teammates touch the same concept, the one with the **most recent
`last_confirmed_at` wins**.  Lock a concept against inbound updates by
setting `metadata.team_locked = true` in your local memex.

## What does NOT sync

Episodic events (per-user activity stream), code symbols, vectors, and
secrets stay local.  Set `metadata.private = true` on any concept to
exclude it from sync.
"""
