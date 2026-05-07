"""
Unified DuckDB store: replaces Kuzu (semantic), SQLite (episodic), and the
sqlite+numpy hybrid (vector) with a single embedded engine.

Three Protocol-conforming implementations share one DuckDB connection and
one .duckdb file:

    - DuckDBSemanticStore  — concepts + edges, recursive-CTE graph traversal
    - DuckDBEpisodicStore  — time-indexed event log
    - DuckDBVectorStore    — HNSW vector search via the VSS extension

Single engine, single file, one query language. DuckDB's writer model is
single-writer / many-readers, which matches our daemon architecture exactly
(one process owns the file, MCP clients proxy through it). Lock-free reads
inside that one process via cursors.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import duckdb
import numpy as np

from memex.core.schema import Concept, Edge, EdgeKind, EpisodicEvent, NodeKind, Source

log = logging.getLogger(__name__)


def _ensure_aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


_DDL = """
CREATE TABLE IF NOT EXISTS concepts (
    id                TEXT PRIMARY KEY,
    name              TEXT NOT NULL,
    description       TEXT NOT NULL DEFAULT '',
    kind              TEXT NOT NULL,
    source            TEXT NOT NULL,
    confidence        DOUBLE NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL,
    last_confirmed_at TIMESTAMPTZ NOT NULL,
    metadata          JSON NOT NULL DEFAULT '{}',
    verification      TEXT
);

CREATE TABLE IF NOT EXISTS edges (
    from_id           TEXT NOT NULL,
    to_id             TEXT NOT NULL,
    kind              TEXT NOT NULL,
    source            TEXT NOT NULL,
    confidence        DOUBLE NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL,
    last_confirmed_at TIMESTAMPTZ NOT NULL,
    metadata          JSON NOT NULL DEFAULT '{}',
    PRIMARY KEY (from_id, to_id, kind)
);

CREATE TABLE IF NOT EXISTS events (
    id        TEXT PRIMARY KEY,
    timestamp TIMESTAMPTZ NOT NULL,
    kind      TEXT NOT NULL,
    actor     TEXT NOT NULL,
    payload   JSON NOT NULL DEFAULT '{}'
);

-- Concept history: every upsert writes the previous snapshot here. Enables
-- "what did we know last week" queries and concept-level rollback.
CREATE TABLE IF NOT EXISTS concept_history (
    id         TEXT NOT NULL,
    version    INTEGER NOT NULL,
    snapshot   JSON NOT NULL,
    changed_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (id, version)
);
"""


def open_duckdb(path: Path, *, vector_dim: int = 384) -> duckdb.DuckDBPyConnection:
    """Open the unified DuckDB file, install extensions, init schema.

    Idempotent. Safe to call on a fresh path or an existing file. The vector
    table dimension is fixed at creation time — passing a different dim on a
    pre-existing file leaves the old table alone (caller's responsibility to
    re-create if they really want to change embedding shape).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(path))

    # VSS = HNSW vectors; FTS = BM25 full-text. Both are bundled with DuckDB
    # but live in extension packages — INSTALL is idempotent and offline after
    # first run.
    conn.execute("INSTALL vss; LOAD vss;")
    conn.execute("INSTALL fts;  LOAD fts;")
    # HNSW index persistence requires this experimental flag at session level.
    conn.execute("SET hnsw_enable_experimental_persistence = true;")

    conn.execute(_DDL)
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS vectors (
            id        TEXT PRIMARY KEY,
            embedding FLOAT[{vector_dim}] NOT NULL
        );
        """
    )

    # HNSW index on embedding column. WITH metric=cosine matches our existing
    # cosine-similarity ranking. Idempotent via IF NOT EXISTS.
    try:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS vectors_hnsw "
            "ON vectors USING HNSW (embedding) WITH (metric = 'cosine');"
        )
    except duckdb.Error as e:
        log.warning("HNSW index creation skipped: %s", e)

    return conn


# ---- Semantic store ----------------------------------------------------------


class DuckDBSemanticStore:
    """Concepts + edges in DuckDB. Implements `SemanticStore` protocol.

    Graph traversal uses recursive CTEs — slightly more verbose than Cypher
    but identical semantics for our 1–5 hop neighbor queries.
    """

    def __init__(self, conn: duckdb.DuckDBPyConnection, lock: threading.RLock):
        self.conn = conn
        self._lock = lock

    def add_concept(self, c: Concept) -> str:
        with self._lock:
            # Capture previous snapshot for versioning. Single round-trip:
            # SELECT existing + max(version), INSERT into history if found.
            existing = self.conn.execute(
                "SELECT * FROM concepts WHERE id = ?", [c.id]
            ).fetchone()
            if existing is not None:
                prev = _row_to_concept(existing)
                row = self.conn.execute(
                    "SELECT COALESCE(MAX(version), 0) FROM concept_history WHERE id = ?",
                    [c.id],
                ).fetchone()
                next_version = int(row[0]) + 1
                self.conn.execute(
                    "INSERT INTO concept_history (id, version, snapshot, changed_at) "
                    "VALUES (?, ?, ?, ?)",
                    [
                        c.id,
                        next_version,
                        json.dumps(prev.model_dump(mode="json")),
                        _ensure_aware(prev.last_confirmed_at),
                    ],
                )
            self.conn.execute(
                """
                INSERT INTO concepts
                  (id, name, description, kind, source, confidence,
                   created_at, last_confirmed_at, metadata, verification)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (id) DO UPDATE SET
                  name = excluded.name,
                  description = excluded.description,
                  kind = excluded.kind,
                  source = excluded.source,
                  confidence = excluded.confidence,
                  last_confirmed_at = excluded.last_confirmed_at,
                  metadata = excluded.metadata,
                  verification = excluded.verification
                """,
                [
                    c.id,
                    c.name,
                    c.description,
                    c.kind.value,
                    c.source.value,
                    float(c.confidence),
                    _ensure_aware(c.created_at),
                    _ensure_aware(c.last_confirmed_at),
                    json.dumps(c.metadata),
                    c.verification or "",
                ],
            )
        return c.id

    def history(self, concept_id: str, limit: int = 20) -> list[dict]:
        """Return recent versions of a concept, newest first.

        Each entry: {version, snapshot, changed_at}. snapshot is the prior
        Concept JSON; the *current* state lives in the concepts table.
        """
        with self._lock:
            rows = self.conn.execute(
                "SELECT version, snapshot, changed_at FROM concept_history "
                "WHERE id = ? ORDER BY version DESC LIMIT ?",
                [concept_id, limit],
            ).fetchall()
        out: list[dict] = []
        for ver, snap, changed_at in rows:
            snap_obj = json.loads(snap) if isinstance(snap, str) else snap
            out.append({"version": int(ver), "snapshot": snap_obj, "changed_at": changed_at})
        return out

    def add_edge(self, e: Edge) -> None:
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO edges
                  (from_id, to_id, kind, source, confidence,
                   created_at, last_confirmed_at, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (from_id, to_id, kind) DO UPDATE SET
                  source = excluded.source,
                  confidence = excluded.confidence,
                  last_confirmed_at = excluded.last_confirmed_at,
                  metadata = excluded.metadata
                """,
                [
                    e.from_id,
                    e.to_id,
                    e.kind.value,
                    e.source.value,
                    float(e.confidence),
                    _ensure_aware(e.created_at),
                    _ensure_aware(e.last_confirmed_at),
                    json.dumps(e.metadata),
                ],
            )

    def get_concept(self, concept_id: str) -> Concept | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM concepts WHERE id = ?", [concept_id]
            ).fetchone()
        return _row_to_concept(row) if row else None

    def all_concepts(self) -> list[Concept]:
        with self._lock:
            rows = self.conn.execute("SELECT * FROM concepts").fetchall()
        return [_row_to_concept(r) for r in rows]

    def neighbors(
        self,
        concept_id: str,
        depth: int = 1,
        kinds: list[EdgeKind] | None = None,
    ) -> tuple[list[Concept], list[Edge]]:
        depth = max(1, min(depth, 5))
        kind_filter = ""
        params: list[Any] = [concept_id, depth]
        if kinds:
            kind_filter = " AND e.kind = ANY(?)"
            params.append([k.value for k in kinds])

        with self._lock:
            edge_rows = self.conn.execute(
                f"""
                WITH RECURSIVE traversal(from_id, to_id, kind, depth) AS (
                    SELECT e.from_id, e.to_id, e.kind, 1
                    FROM edges e
                    WHERE e.from_id = ?{kind_filter}
                    UNION ALL
                    SELECT e.from_id, e.to_id, e.kind, t.depth + 1
                    FROM edges e
                    JOIN traversal t ON e.from_id = t.to_id
                    WHERE t.depth < ?
                )
                SELECT DISTINCT
                    e.from_id, e.to_id, e.kind, e.source, e.confidence,
                    e.created_at, e.last_confirmed_at, e.metadata
                FROM edges e
                JOIN traversal t
                  ON e.from_id = t.from_id AND e.to_id = t.to_id AND e.kind = t.kind
                """,
                # Reorder params to match: traversal anchor concept, optional kinds,
                # then depth limit at the end.
                [concept_id, *(params[2:] if kinds else []), depth],
            ).fetchall()

            edges = [_row_to_edge(r) for r in edge_rows]
            seen_ids = {concept_id} | {e.from_id for e in edges} | {e.to_id for e in edges}
            if not seen_ids:
                return [], edges
            placeholders = ", ".join(["?"] * len(seen_ids))
            node_rows = self.conn.execute(
                f"SELECT * FROM concepts WHERE id IN ({placeholders})",
                list(seen_ids),
            ).fetchall()

        return [_row_to_concept(r) for r in node_rows], edges

    def search_lexical(
        self,
        query: str,
        limit: int = 50,
        kind: NodeKind | None = None,
    ) -> list[Concept]:
        # Cheap LIKE-based fallback — BM25 ranking happens upstream in BM25Index.
        # FTS extension is loaded; switching to it is a 1-line upgrade once
        # the retriever knows to consume scored rows directly.
        like = f"%{query.lower()}%"
        params: list[Any] = [like, like]
        kind_clause = ""
        if kind is not None:
            kind_clause = " AND kind = ?"
            params.append(kind.value)
        params.append(int(limit))
        with self._lock:
            rows = self.conn.execute(
                f"""
                SELECT * FROM concepts
                WHERE (lower(name) LIKE ? OR lower(description) LIKE ?)
                  {kind_clause}
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [_row_to_concept(r) for r in rows]

    def count(self) -> int:
        with self._lock:
            return int(self.conn.execute("SELECT count(*) FROM concepts").fetchone()[0])

    def close(self) -> None:
        # The connection is shared — only the bundle owner closes it.
        pass


# ---- Episodic store ----------------------------------------------------------


class DuckDBEpisodicStore:
    """Time-indexed event log. Implements `EpisodicStore` protocol."""

    def __init__(self, conn: duckdb.DuckDBPyConnection, lock: threading.RLock):
        self.conn = conn
        self._lock = lock

    def append(self, event: EpisodicEvent) -> str:
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO events (id, timestamp, kind, actor, payload)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (id) DO UPDATE SET
                  timestamp = excluded.timestamp,
                  kind      = excluded.kind,
                  actor     = excluded.actor,
                  payload   = excluded.payload
                """,
                [
                    event.id,
                    _ensure_aware(event.timestamp),
                    event.kind,
                    event.actor.value,
                    json.dumps(event.payload),
                ],
            )
        return event.id

    def recent(self, limit: int = 100, kind: str | None = None) -> list[EpisodicEvent]:
        with self._lock:
            if kind:
                rows = self.conn.execute(
                    "SELECT id, timestamp, kind, actor, payload FROM events "
                    "WHERE kind = ? ORDER BY timestamp DESC LIMIT ?",
                    [kind, limit],
                ).fetchall()
            else:
                rows = self.conn.execute(
                    "SELECT id, timestamp, kind, actor, payload FROM events "
                    "ORDER BY timestamp DESC LIMIT ?",
                    [limit],
                ).fetchall()
        return [
            EpisodicEvent(
                id=r[0],
                timestamp=r[1],
                kind=r[2],
                actor=Source(r[3]),
                payload=json.loads(r[4]) if isinstance(r[4], str) else r[4],
            )
            for r in rows
        ]

    def count(self) -> int:
        with self._lock:
            return int(self.conn.execute("SELECT count(*) FROM events").fetchone()[0])

    def close(self) -> None:
        pass


# ---- Vector store ------------------------------------------------------------


class DuckDBVectorStore:
    """HNSW vector index via DuckDB VSS extension. Implements `VectorStore`."""

    def __init__(
        self, conn: duckdb.DuckDBPyConnection, lock: threading.RLock, dim: int = 384
    ):
        self.conn = conn
        self.dim = dim
        self._lock = lock

    def add(self, concept_id: str, embedding: np.ndarray) -> None:
        if embedding.ndim != 1 or embedding.shape[0] != self.dim:
            raise ValueError(f"expected 1-D vector of dim {self.dim}, got shape {embedding.shape}")
        vec = embedding.astype(np.float32).tolist()
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO vectors (id, embedding) VALUES (?, ?::FLOAT[])
                ON CONFLICT (id) DO UPDATE SET embedding = excluded.embedding
                """,
                [concept_id, vec],
            )

    def remove(self, concept_id: str) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM vectors WHERE id = ?", [concept_id])

    def search(self, query: np.ndarray, limit: int = 20) -> list[tuple[str, float]]:
        if query.ndim != 1 or query.shape[0] != self.dim:
            raise ValueError(f"expected 1-D vector of dim {self.dim}")
        q = query.astype(np.float32).tolist()
        # array_cosine_distance requires fixed-length arrays of identical dim,
        # so the query side must be cast to FLOAT[<self.dim>] explicitly. The
        # dim is class-private so this is not a SQL-injection vector.
        cast = f"?::FLOAT[{self.dim}]"
        with self._lock:
            rows = self.conn.execute(
                f"""
                SELECT id, 1.0 - array_cosine_distance(embedding, {cast}) AS score
                FROM vectors
                ORDER BY array_cosine_distance(embedding, {cast})
                LIMIT ?
                """,
                [q, q, max(1, limit)],
            ).fetchall()
        return [(r[0], float(r[1])) for r in rows]

    def count(self) -> int:
        with self._lock:
            return int(self.conn.execute("SELECT count(*) FROM vectors").fetchone()[0])

    def close(self) -> None:
        pass


# ---- Bundle ------------------------------------------------------------------


class DuckDBStores:
    """Owns the connection + lock, hands out the three protocol implementations.

    Engine.build_default uses this to wire all three stores against one file.
    """

    def __init__(self, path: Path, *, vector_dim: int = 384):
        self.path = path
        self.vector_dim = vector_dim
        self.conn = open_duckdb(path, vector_dim=vector_dim)
        # Single RLock for the connection — DuckDB connections are not
        # thread-safe; serializing all access keeps reads + writes correct
        # without needing per-call cursors.
        self._lock = threading.RLock()
        self.semantic = DuckDBSemanticStore(self.conn, self._lock)
        self.episodic = DuckDBEpisodicStore(self.conn, self._lock)
        self.vector = DuckDBVectorStore(self.conn, self._lock, dim=vector_dim)

    def close(self) -> None:
        with self._lock:
            self.conn.close()


# ---- row mapping -------------------------------------------------------------


def _row_to_concept(row: Any) -> Concept:
    (
        id_,
        name,
        description,
        kind,
        source,
        confidence,
        created_at,
        last_confirmed_at,
        metadata,
        verification,
    ) = row[:10]
    return Concept(
        id=id_,
        name=name,
        description=description or "",
        kind=NodeKind(kind),
        source=Source(source),
        confidence=float(confidence),
        created_at=_ensure_aware(created_at),
        last_confirmed_at=_ensure_aware(last_confirmed_at),
        metadata=json.loads(metadata) if isinstance(metadata, str) else (metadata or {}),
        verification=verification or None,
    )


def _row_to_edge(row: Any) -> Edge:
    from_id, to_id, kind, source, confidence, created_at, last_confirmed_at, metadata = row[:8]
    return Edge(
        from_id=from_id,
        to_id=to_id,
        kind=EdgeKind(kind),
        source=Source(source),
        confidence=float(confidence),
        created_at=_ensure_aware(created_at),
        last_confirmed_at=_ensure_aware(last_confirmed_at),
        metadata=json.loads(metadata) if isinstance(metadata, str) else (metadata or {}),
    )
