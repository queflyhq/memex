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
    metadata = {
        "path": abs_path,
        "slug": _slugify(abs_path),
        "indexed_files": 0,
        "indexed_symbols": 0,
        "last_indexed_at": None,
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
    """Stamp a source as freshly indexed. No-op if the source is missing."""
    c = engine.get(source_id)
    if c is None:
        return
    c.metadata["indexed_files"] = files
    c.metadata["indexed_symbols"] = symbols
    c.metadata["last_indexed_at"] = datetime.now(timezone.utc).isoformat()
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
