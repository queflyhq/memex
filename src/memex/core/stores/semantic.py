"""
Semantic store backed by Kuzu (embedded graph DB).

Holds concepts and typed relations. Provenance and confidence are first-class
columns, not metadata — they're forced by axiom 4 (truth has a source/timestamp).
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import kuzu

from memex.core.schema import Concept, Edge, EdgeKind, NodeKind, Source

log = logging.getLogger(__name__)


_DDL_NODE = """
CREATE NODE TABLE IF NOT EXISTS Concept(
    id STRING,
    name STRING,
    description STRING,
    kind STRING,
    source STRING,
    confidence DOUBLE,
    created_at TIMESTAMP,
    last_confirmed_at TIMESTAMP,
    metadata STRING,
    verification STRING,
    PRIMARY KEY(id)
)
"""

_DDL_REL = """
CREATE REL TABLE IF NOT EXISTS Relates(
    FROM Concept TO Concept,
    kind STRING,
    source STRING,
    confidence DOUBLE,
    created_at TIMESTAMP,
    last_confirmed_at TIMESTAMP,
    metadata STRING
)
"""


def _ensure_aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


class KuzuSemanticStore:
    """Kuzu-backed concept graph with provenance + confidence on every node and edge.

    Implements the `memex.core.protocols.SemanticStore` Protocol.
    """

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.db = kuzu.Database(str(path))
        self.conn = kuzu.Connection(self.db)
        self._lock = threading.RLock()
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self.conn.execute(_DDL_NODE)
            self.conn.execute(_DDL_REL)

    # ---- writes ---------------------------------------------------------

    def add_concept(self, c: Concept) -> str:
        with self._lock:
            self.conn.execute(
                """
                MERGE (n:Concept {id: $id})
                ON CREATE SET
                    n.name = $name,
                    n.description = $description,
                    n.kind = $kind,
                    n.source = $source,
                    n.confidence = $confidence,
                    n.created_at = $created_at,
                    n.last_confirmed_at = $last_confirmed_at,
                    n.metadata = $metadata,
                    n.verification = $verification
                ON MATCH SET
                    n.name = $name,
                    n.description = $description,
                    n.kind = $kind,
                    n.source = $source,
                    n.confidence = $confidence,
                    n.last_confirmed_at = $last_confirmed_at,
                    n.metadata = $metadata,
                    n.verification = $verification
                """,
                {
                    "id": c.id,
                    "name": c.name,
                    "description": c.description,
                    "kind": c.kind.value,
                    "source": c.source.value,
                    "confidence": float(c.confidence),
                    "created_at": _ensure_aware(c.created_at),
                    "last_confirmed_at": _ensure_aware(c.last_confirmed_at),
                    "metadata": json.dumps(c.metadata),
                    "verification": c.verification or "",
                },
            )
        return c.id

    def add_edge(self, e: Edge) -> None:
        with self._lock:
            self.conn.execute(
                """
                MATCH (a:Concept {id: $from_id}), (b:Concept {id: $to_id})
                CREATE (a)-[r:Relates {
                    kind: $kind,
                    source: $source,
                    confidence: $confidence,
                    created_at: $created_at,
                    last_confirmed_at: $last_confirmed_at,
                    metadata: $metadata
                }]->(b)
                """,
                {
                    "from_id": e.from_id,
                    "to_id": e.to_id,
                    "kind": e.kind.value,
                    "source": e.source.value,
                    "confidence": float(e.confidence),
                    "created_at": _ensure_aware(e.created_at),
                    "last_confirmed_at": _ensure_aware(e.last_confirmed_at),
                    "metadata": json.dumps(e.metadata),
                },
            )

    # ---- reads ---------------------------------------------------------

    def get_concept(self, concept_id: str) -> Concept | None:
        with self._lock:
            result = self.conn.execute(
                "MATCH (n:Concept {id: $id}) RETURN n.*",
                {"id": concept_id},
            )
            if not result.has_next():
                return None
            row = result.get_next()
        return _row_to_concept(row)

    def all_concepts(self) -> list[Concept]:
        with self._lock:
            result = self.conn.execute("MATCH (n:Concept) RETURN n.*")
            rows = []
            while result.has_next():
                rows.append(result.get_next())
        return [_row_to_concept(r) for r in rows]

    def neighbors(
        self,
        concept_id: str,
        depth: int = 1,
        kinds: list[EdgeKind] | None = None,
    ) -> tuple[list[Concept], list[Edge]]:
        """Return concepts within `depth` hops and the edges traversed."""
        kinds_clause = ""
        params: dict[str, Any] = {"id": concept_id}
        if kinds:
            kinds_clause = " WHERE r.kind IN $kinds"
            params["kinds"] = [k.value for k in kinds]

        # Kuzu supports variable-length paths: -[r:Relates*1..N]->
        # We collect the matched concepts and edges separately.
        depth = max(1, min(depth, 5))
        with self._lock:
            edge_result = self.conn.execute(
                f"""
                MATCH (a:Concept {{id: $id}})-[r:Relates]->(b:Concept)
                {kinds_clause}
                RETURN
                    a.id AS from_id,
                    b.id AS to_id,
                    r.kind AS kind,
                    r.source AS source,
                    r.confidence AS confidence,
                    r.created_at AS created_at,
                    r.last_confirmed_at AS last_confirmed_at,
                    r.metadata AS metadata
                """,
                params,
            )
            edges: list[Edge] = []
            seen_ids: set[str] = {concept_id}
            while edge_result.has_next():
                row = edge_result.get_next()
                edges.append(_row_to_edge(row))
                seen_ids.add(row[1])

            # For depth > 1, expand iteratively.
            frontier = {e.to_id for e in edges} - {concept_id}
            for _ in range(depth - 1):
                if not frontier:
                    break
                next_frontier: set[str] = set()
                for nid in list(frontier):
                    sub_result = self.conn.execute(
                        """
                        MATCH (a:Concept {id: $id})-[r:Relates]->(b:Concept)
                        RETURN
                            a.id AS from_id,
                            b.id AS to_id,
                            r.kind AS kind,
                            r.source AS source,
                            r.confidence AS confidence,
                            r.created_at AS created_at,
                            r.last_confirmed_at AS last_confirmed_at,
                            r.metadata AS metadata
                        """,
                        {"id": nid},
                    )
                    while sub_result.has_next():
                        row = sub_result.get_next()
                        edges.append(_row_to_edge(row))
                        if row[1] not in seen_ids:
                            next_frontier.add(row[1])
                            seen_ids.add(row[1])
                frontier = next_frontier

        nodes: list[Concept] = []
        for nid in seen_ids:
            n = self.get_concept(nid)
            if n is not None:
                nodes.append(n)
        return nodes, edges

    def search_lexical(
        self,
        query: str,
        limit: int = 50,
        kind: NodeKind | None = None,
    ) -> list[Concept]:
        """Cheap lexical match using LOWER + CONTAINS. BM25 ranking happens upstream."""
        like = query.lower()
        params: dict[str, Any] = {"like": like, "limit": int(limit)}
        kind_clause = ""
        if kind is not None:
            kind_clause = " AND n.kind = $kind"
            params["kind"] = kind.value
        with self._lock:
            result = self.conn.execute(
                f"""
                MATCH (n:Concept)
                WHERE
                    contains(lower(n.name), $like)
                    OR contains(lower(n.description), $like)
                    {kind_clause}
                RETURN n.*
                LIMIT $limit
                """,
                params,
            )
            rows = []
            while result.has_next():
                rows.append(result.get_next())
        return [_row_to_concept(r) for r in rows]

    def count(self) -> int:
        with self._lock:
            result = self.conn.execute("MATCH (n:Concept) RETURN count(n)")
            if result.has_next():
                return int(result.get_next()[0])
        return 0

    def close(self) -> None:
        self.conn.close()


# ---- row mapping ---------------------------------------------------------


def _row_to_concept(row: list[Any]) -> Concept:
    # Order matches `RETURN n.*` (Kuzu returns properties in declaration order).
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
        metadata=json.loads(metadata) if metadata else {},
        verification=verification or None,
    )


def _row_to_edge(row: list[Any]) -> Edge:
    from_id, to_id, kind, source, confidence, created_at, last_confirmed_at, metadata = row[:8]
    return Edge(
        from_id=from_id,
        to_id=to_id,
        kind=EdgeKind(kind),
        source=Source(source),
        confidence=float(confidence),
        created_at=_ensure_aware(created_at),
        last_confirmed_at=_ensure_aware(last_confirmed_at),
        metadata=json.loads(metadata) if metadata else {},
    )
