"""
Episodic store: time-indexed event log.

Events are *what happened* — the consolidation pass (v0.4) promotes
high-frequency episodic events into semantic facts.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from memex.core.schema import EpisodicEvent, Source


def _ensure_aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


_DDL = """
CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    timestamp TEXT NOT NULL,
    kind TEXT NOT NULL,
    actor TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_time ON events (timestamp);
CREATE INDEX IF NOT EXISTS idx_events_kind ON events (kind);
"""


class SqliteEpisodicStore:
    """SQLite-backed event log. Implements `memex.core.protocols.EpisodicStore`."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.executescript(_DDL)
        self.conn.commit()
        self._lock = threading.RLock()

    def append(self, event: EpisodicEvent) -> str:
        with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO events (id, timestamp, kind, actor, payload) VALUES (?, ?, ?, ?, ?)",
                (
                    event.id,
                    _ensure_aware(event.timestamp).isoformat(),
                    event.kind,
                    event.actor.value,
                    json.dumps(event.payload),
                ),
            )
            self.conn.commit()
        return event.id

    def recent(self, limit: int = 100, kind: str | None = None) -> list[EpisodicEvent]:
        with self._lock:
            if kind:
                cursor = self.conn.execute(
                    "SELECT id, timestamp, kind, actor, payload FROM events WHERE kind = ? ORDER BY timestamp DESC LIMIT ?",
                    (kind, limit),
                )
            else:
                cursor = self.conn.execute(
                    "SELECT id, timestamp, kind, actor, payload FROM events ORDER BY timestamp DESC LIMIT ?",
                    (limit,),
                )
            rows = cursor.fetchall()
        return [
            EpisodicEvent(
                id=r[0],
                timestamp=datetime.fromisoformat(r[1]),
                kind=r[2],
                actor=Source(r[3]),
                payload=json.loads(r[4]),
            )
            for r in rows
        ]

    def count(self) -> int:
        with self._lock:
            return int(self.conn.execute("SELECT count(*) FROM events").fetchone()[0])

    def close(self) -> None:
        self.conn.close()
