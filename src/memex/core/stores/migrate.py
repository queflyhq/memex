"""
One-shot migration: legacy Kuzu + SQLite stores → unified DuckDB.

Run via `memex migrate` (or call `migrate_to_duckdb` directly). Reads from
`settings.graph_path` / `settings.episodic_path` / `settings.vectors_path`
and writes into `settings.store_path`. Idempotent — safe to re-run; concepts
and events upsert by primary key.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from memex.config import Settings, get_settings
from memex.core.stores.duckdb_store import DuckDBStores
from memex.core.stores.episodic import SqliteEpisodicStore
from memex.core.stores.semantic import KuzuSemanticStore

log = logging.getLogger(__name__)


@dataclass
class MigrationResult:
    concepts: int = 0
    edges: int = 0
    events: int = 0
    vectors: int = 0
    skipped: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.skipped is None:
            self.skipped = []

    def summary(self) -> str:
        parts = [
            f"concepts={self.concepts}",
            f"edges={self.edges}",
            f"events={self.events}",
            f"vectors={self.vectors}",
        ]
        if self.skipped:
            parts.append(f"skipped={','.join(self.skipped)}")
        return " ".join(parts)


def migrate_to_duckdb(
    settings: Settings | None = None,
    *,
    vector_dim: int = 384,
) -> MigrationResult:
    """Pull everything out of the legacy stores into the unified DuckDB file."""
    settings = settings or get_settings()
    settings.ensure_dirs()
    result = MigrationResult()

    target = DuckDBStores(settings.store_path, vector_dim=vector_dim)
    try:
        # ---- semantic (Kuzu → DuckDB) ----
        if settings.graph_path.exists():
            log.info("migrating concepts + edges from %s", settings.graph_path)
            src_sem = KuzuSemanticStore(settings.graph_path)
            try:
                concepts = src_sem.all_concepts()
                for c in concepts:
                    target.semantic.add_concept(c)
                result.concepts = len(concepts)
                # Edges: Kuzu doesn't expose a bulk-edges API in the existing
                # store; pull them via a raw query.
                edge_rows = src_sem.conn.execute(
                    """
                    MATCH (a:Concept)-[r:Relates]->(b:Concept)
                    RETURN
                        a.id AS from_id,
                        b.id AS to_id,
                        r.kind AS kind,
                        r.source AS source,
                        r.confidence AS confidence,
                        r.created_at AS created_at,
                        r.last_confirmed_at AS last_confirmed_at,
                        r.metadata AS metadata
                    """
                )
                from memex.core.stores.semantic import _row_to_edge

                while edge_rows.has_next():
                    e = _row_to_edge(edge_rows.get_next())
                    target.semantic.add_edge(e)
                    result.edges += 1
            finally:
                src_sem.close()
        else:
            result.skipped.append("kuzu(no-file)")

        # ---- episodic (SQLite → DuckDB) ----
        if settings.episodic_path.exists():
            log.info("migrating events from %s", settings.episodic_path)
            src_ep = SqliteEpisodicStore(settings.episodic_path)
            try:
                # Pull all events at once — even a busy memex has events in the
                # tens of thousands, fine in memory.
                events = src_ep.recent(limit=10_000_000)
                for ev in events:
                    target.episodic.append(ev)
                result.events = len(events)
            finally:
                src_ep.close()
        else:
            result.skipped.append("episodic(no-file)")

        # ---- vectors (SQLite → DuckDB) ----
        if settings.vectors_path.exists():
            log.info("migrating vectors from %s", settings.vectors_path)
            try:
                conn = sqlite3.connect(str(settings.vectors_path))
                try:
                    rows = conn.execute(
                        "SELECT id, dim, embedding FROM vectors"
                    ).fetchall()
                    for vid, dim, blob in rows:
                        if dim != vector_dim:
                            log.warning(
                                "skipping vector %s: dim=%d != target=%d",
                                vid, dim, vector_dim,
                            )
                            continue
                        vec = np.frombuffer(blob, dtype=np.float32)
                        target.vector.add(vid, vec)
                        result.vectors += 1
                finally:
                    conn.close()
            except sqlite3.DatabaseError as e:
                log.warning("vectors migration failed: %s", e)
                result.skipped.append("vectors(error)")
        else:
            result.skipped.append("vectors(no-file)")

    finally:
        target.close()

    return result


def archive_legacy_files(settings: Settings | None = None) -> list[Path]:
    """Rename legacy files to `*.pre-duckdb.bak` so a future run starts clean."""
    settings = settings or get_settings()
    archived: list[Path] = []
    for src in (settings.graph_path, settings.episodic_path, settings.vectors_path):
        if src.exists():
            dst = src.with_suffix(src.suffix + ".pre-duckdb.bak")
            src.rename(dst)
            archived.append(dst)
    return archived
