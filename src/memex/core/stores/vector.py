"""
Vector store: sqlite + numpy brute-force.

Optimized for simplicity, not scale. At v0.1 we expect <100K nodes; brute-force
cosine on numpy is sub-millisecond per query at that scale and avoids the
sqlite-vec extension which has wheel-availability issues on some platforms.

Upgrade path: swap to sqlite-vec, hnswlib, or Kuzu's HNSW when N > 100K.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import numpy as np


class NumpyVectorStore:
    """sqlite + numpy brute-force vector index. Implements `memex.core.protocols.VectorStore`."""

    def __init__(self, path: Path, dim: int = 384):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.dim = dim
        self.conn = sqlite3.connect(str(path), check_same_thread=False, timeout=30.0)
        # See episodic.py for rationale on these pragmas (cross-process safety + speed).
        cur = self.conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA busy_timeout=10000")
        cur.execute("PRAGMA temp_store=MEMORY")
        cur.execute("PRAGMA mmap_size=268435456")
        cur.execute("PRAGMA cache_size=-65536")
        cur.close()
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS vectors (
                id TEXT PRIMARY KEY,
                dim INTEGER NOT NULL,
                embedding BLOB NOT NULL
            );
            """
        )
        self.conn.commit()
        self._lock = threading.RLock()

    def add(self, concept_id: str, embedding: np.ndarray) -> None:
        if embedding.ndim != 1 or embedding.shape[0] != self.dim:
            raise ValueError(f"expected 1-D vector of dim {self.dim}, got shape {embedding.shape}")
        vec = embedding.astype(np.float32)
        with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO vectors (id, dim, embedding) VALUES (?, ?, ?)",
                (concept_id, self.dim, vec.tobytes()),
            )
            self.conn.commit()

    def remove(self, concept_id: str) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM vectors WHERE id = ?", (concept_id,))
            self.conn.commit()

    def search(self, query: np.ndarray, limit: int = 20) -> list[tuple[str, float]]:
        """Return [(id, cosine_score)] sorted descending."""
        if query.ndim != 1 or query.shape[0] != self.dim:
            raise ValueError(f"expected 1-D vector of dim {self.dim}")
        q = query.astype(np.float32)
        q_norm = float(np.linalg.norm(q)) or 1.0

        with self._lock:
            rows = self.conn.execute("SELECT id, embedding FROM vectors").fetchall()
        if not rows:
            return []

        ids = [r[0] for r in rows]
        mat = np.frombuffer(b"".join(r[1] for r in rows), dtype=np.float32).reshape(
            len(rows), self.dim
        )
        norms = np.linalg.norm(mat, axis=1)
        norms[norms == 0] = 1.0
        scores = (mat @ q) / (norms * q_norm)
        top = np.argsort(-scores)[: max(1, limit)]
        return [(ids[int(i)], float(scores[int(i)])) for i in top]

    def count(self) -> int:
        with self._lock:
            return int(self.conn.execute("SELECT count(*) FROM vectors").fetchone()[0])

    def close(self) -> None:
        self.conn.close()
