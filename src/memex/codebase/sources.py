"""Source primitive — registered codebases.

A `Source` is a `Concept(kind="source")` with metadata pointing at the
filesystem path of an indexed repo and tracking when it was last indexed.
This intentionally reuses the existing concept store (no new tables) per
the v0.7 codebase memory plan.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from memex.core.schema import Concept, NodeKind, Source as SourceActor

if TYPE_CHECKING:
    from memex.core.engine import Engine


def _slugify(name: str) -> str:
    """Stable short id for a path: lowercased basename + 8 hex of full path."""
    p = Path(name)
    base = p.name.lower().replace(" ", "-") or "src"
    digest = hashlib.sha256(str(p.resolve()).encode("utf-8")).hexdigest()[:8]
    return f"{base}-{digest}"


def _detect_git_info(repo_path: Path) -> dict[str, str]:
    """Best-effort: capture git remote URL, host, current branch, and HEAD
    commit. Pure file reads — no subprocess. Returns {} for non-git dirs.
    Stores a snapshot at index time so reindex can flag drift later.
    """
    git_dir = repo_path / ".git"
    if not git_dir.is_dir():
        return {}
    info: dict[str, str] = {}
    # remote URL
    cfg = git_dir / "config"
    if cfg.is_file():
        try:
            text = cfg.read_text(encoding="utf-8", errors="replace")
            in_origin = False
            for line in text.splitlines():
                s = line.strip()
                if s.startswith("[remote "):
                    in_origin = '"origin"' in s
                    continue
                if s.startswith("["):
                    in_origin = False
                    continue
                if in_origin and s.startswith("url ="):
                    info["url"] = s.split("=", 1)[1].strip()
                    break
        except Exception:  # noqa: BLE001
            pass
    if "url" in info:
        url = info["url"]
        if "github.com" in url:
            info["host"] = "github"
        elif "gitlab.com" in url or "gitlab" in url:
            info["host"] = "gitlab"
        elif "bitbucket" in url:
            info["host"] = "bitbucket"
        elif "azure.com" in url or "dev.azure" in url:
            info["host"] = "azure"
        else:
            info["host"] = ""
    # current branch + HEAD commit
    head = git_dir / "HEAD"
    if head.is_file():
        try:
            ref = head.read_text(encoding="utf-8", errors="replace").strip()
            if ref.startswith("ref:"):
                ref_path = ref[4:].strip()  # e.g. "refs/heads/main"
                info["branch"] = ref_path.rsplit("/", 1)[-1]
                ref_file = git_dir / ref_path
                if ref_file.is_file():
                    info["commit"] = ref_file.read_text(
                        encoding="utf-8", errors="replace"
                    ).strip()
                else:
                    # Try packed-refs
                    packed = git_dir / "packed-refs"
                    if packed.is_file():
                        for line in packed.read_text(
                            encoding="utf-8", errors="replace"
                        ).splitlines():
                            if line.endswith(" " + ref_path):
                                info["commit"] = line.split(" ", 1)[0]
                                break
            else:
                # Detached HEAD — ref IS the sha
                info["commit"] = ref
                info["branch"] = "(detached)"
        except Exception:  # noqa: BLE001
            pass
    return info


# Backwards-compat shim — old tests / callers.
def _detect_git_remote(repo_path: Path) -> dict[str, str]:
    info = _detect_git_info(repo_path)
    return {"url": info.get("url", ""), "host": info.get("host", "")} if info else {}


def add_source(
    engine: "Engine",
    path: str | Path,
    name: str | None = None,
) -> Concept:
    """Register a directory as a memex source. Idempotent — re-adding the same
    path returns the existing concept rather than creating a duplicate.
    """
    abs_path = str(Path(path).resolve())
    existing = get_source(engine, abs_path)
    if existing is not None:
        return existing

    display_name = name or _slugify(abs_path)
    git = _detect_git_info(Path(abs_path))
    metadata = {
        "path": abs_path,
        "slug": _slugify(abs_path),
        "indexed_files": 0,
        "indexed_symbols": 0,
        "last_indexed_at": None,
        "git_remote": git.get("url", ""),
        "git_host": git.get("host", ""),
        "git_branch": git.get("branch", ""),
        "git_commit": git.get("commit", ""),
    }
    return engine.add(
        name=display_name,
        description=f"Codebase indexed at {abs_path}",
        kind=NodeKind.source,
        source=SourceActor.agent,
        metadata=metadata,
    )


def get_source(engine: "Engine", path: str | Path) -> Concept | None:
    """Look up a source by its absolute path. None if not registered."""
    abs_path = str(Path(path).resolve())
    for c in _all_sources(engine):
        if c.metadata.get("path") == abs_path:
            return c
    return None


def list_sources(engine: "Engine") -> list[Concept]:
    """Return every registered source, oldest first."""
    return sorted(_all_sources(engine), key=lambda c: c.created_at)


def remove_source(engine: "Engine", source_id: str) -> int:
    """Delete a source plus every file + symbol it owns. Returns the
    number of concepts deleted (source + files + symbols).
    """
    deleted = 0
    # Delete owned files (and their symbols transitively) by walking
    # incoming `part_of` edges. Simpler approach: query metadata.source_id.
    for f in _children_by_metadata(engine, "source_id", source_id, NodeKind.file):
        for s in _children_by_metadata(engine, "file_id", f.id, NodeKind.symbol):
            engine.delete(s.id)
            deleted += 1
        engine.delete(f.id)
        deleted += 1
    engine.delete(source_id)
    deleted += 1
    return deleted


def mark_indexed(
    engine: "Engine",
    source_id: str,
    files: int,
    symbols: int,
) -> None:
    """Stamp a source as freshly indexed. Refreshes git_commit / git_branch
    so the user can see what HEAD the index reflects (and detect drift on
    next reindex)."""
    c = engine.get(source_id)
    if c is None:
        return
    c.metadata["indexed_files"] = files
    c.metadata["indexed_symbols"] = symbols
    c.metadata["last_indexed_at"] = datetime.now(timezone.utc).isoformat()
    # Re-detect git state so the indexed_at + commit pair tells the truth.
    repo_path = Path(str(c.metadata.get("path", "")))
    if repo_path.exists():
        git = _detect_git_info(repo_path)
        if git.get("commit"):
            c.metadata["git_commit"] = git["commit"]
        if git.get("branch"):
            c.metadata["git_branch"] = git["branch"]
    engine.put(c)


# ---- Internal helpers --------------------------------------------------------


def _all_sources(engine: "Engine") -> list[Concept]:
    return engine.find_by_kind(NodeKind.source)


def _children_by_metadata(
    engine: "Engine",
    field: str,
    value: str,
    kind: NodeKind,
) -> list[Concept]:
    return [
        c for c in engine.find_by_kind(kind)
        if c.metadata.get(field) == value
    ]
