"""
HTTP client that mirrors the Engine API surface used by the MCP server.

Lets a stdio MCP process act as a thin proxy to a long-running daemon — so
multiple Claude / Cursor sessions on the same machine all read and write to
the same Kuzu+SQLite stores instead of fighting over the Kuzu directory lock.

Returns Pydantic model instances (Concept, Edge, RecallResult, EpisodicEvent)
so callers can use `.model_dump()` and attribute access exactly as they would
with the in-process Engine — both backends are duck-type compatible from the
MCP server's perspective.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from memex.core.schema import (
    Concept,
    Edge,
    EdgeKind,
    EpisodicEvent,
    NodeKind,
    RecallResult,
    Source,
)

log = logging.getLogger(__name__)

# Localhost RTT is ~1-3ms; 10s covers cold starts and embed-on-write.
DEFAULT_TIMEOUT = 10.0


class MemexClient:
    """Thin HTTP wrapper that exposes the same methods MCP/CLI expect from Engine."""

    def __init__(
        self,
        base_url: str,
        auth_token: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ):
        self.base_url = base_url.rstrip("/")
        headers = {"Authorization": f"Bearer {auth_token}"} if auth_token else {}
        # One Client = one connection pool, kept alive for the MCP process lifetime.
        self._http = httpx.Client(
            base_url=self.base_url,
            headers=headers,
            timeout=timeout,
        )

    # ---- writes ---------------------------------------------------------

    def add(
        self,
        name: str,
        description: str = "",
        kind: NodeKind | str = NodeKind.fact,
        source: Source | str = Source.human,
        confidence: float = 1.0,
        verification: str | None = None,
        metadata: dict[str, Any] | None = None,
        related_to: list[str] | None = None,
    ) -> Concept:
        body = {
            "name": name,
            "description": description,
            "kind": _enum_value(kind, NodeKind),
            "source": _enum_value(source, Source),
            "confidence": confidence,
            "verification": verification,
            "metadata": metadata or {},
            "related_to": related_to,
        }
        r = self._http.post("/nodes", json=body)
        r.raise_for_status()
        return Concept.model_validate(r.json())

    def link(
        self,
        from_id: str,
        to_id: str,
        kind: EdgeKind | str = EdgeKind.relates_to,
        source: Source | str = Source.human,
        confidence: float = 1.0,
        metadata: dict[str, Any] | None = None,
    ) -> Edge:
        body = {
            "from_id": from_id,
            "to_id": to_id,
            "kind": _enum_value(kind, EdgeKind),
            "source": _enum_value(source, Source),
            "confidence": confidence,
            "metadata": metadata or {},
        }
        r = self._http.post("/edges", json=body)
        r.raise_for_status()
        # /edges returns {"status": "ok"} — synthesize an Edge for caller parity.
        return Edge(
            from_id=from_id,
            to_id=to_id,
            kind=EdgeKind(body["kind"]),
            source=Source(body["source"]),
            confidence=confidence,
            metadata=metadata or {},
        )

    def observe(
        self,
        kind: str,
        actor: Source | str = Source.agent,
        payload: dict[str, Any] | None = None,
    ) -> EpisodicEvent:
        body = {
            "kind": kind,
            "actor": _enum_value(actor, Source),
            "payload": payload or {},
        }
        r = self._http.post("/observe", json=body)
        r.raise_for_status()
        # /observe returns just {"id": "..."} — synthesize a minimal EpisodicEvent.
        return EpisodicEvent(
            id=r.json()["id"],
            kind=kind,
            actor=Source(body["actor"]),
            payload=body["payload"],
        )

    # ---- reads ---------------------------------------------------------

    def get(self, concept_id: str) -> Concept | None:
        r = self._http.get(f"/nodes/{concept_id}")
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return Concept.model_validate(r.json())

    def recall(
        self,
        query: str,
        budget_tokens: int = 2000,
        kind: NodeKind | str | None = None,
        expand_hops: int = 1,
    ) -> RecallResult:
        params: dict[str, Any] = {
            "q": query,
            "budget": budget_tokens,
            "expand": expand_hops,
        }
        if kind is not None:
            params["kind"] = _enum_value(kind, NodeKind)
        r = self._http.get("/recall", params=params)
        r.raise_for_status()
        return RecallResult.model_validate(r.json())

    # ---- skills + meta -------------------------------------------------

    def validate(
        self,
        skill_name: str,
        actor: Source | str = Source.agent,
    ) -> dict[str, Any]:
        body = {"skill": skill_name, "actor": _enum_value(actor, Source)}
        r = self._http.post("/validate", json=body)
        r.raise_for_status()
        return r.json()

    def progress(self, actor: Source | str | None = None) -> dict[str, Any]:
        params = {"actor": _enum_value(actor, Source)} if actor is not None else {}
        r = self._http.get("/progress", params=params)
        r.raise_for_status()
        return r.json()

    def list_skills(self) -> list[dict[str, Any]]:
        r = self._http.get("/skills")
        r.raise_for_status()
        return r.json()

    def install_skill(self, name: str) -> dict[str, Any]:
        r = self._http.post("/skills/install", json={"name": name})
        r.raise_for_status()
        return r.json()

    # ---- tasks ---------------------------------------------------------

    def add_task(
        self,
        title: str,
        description: str = "",
        status: str = "pending",
        priority: str = "p2",
        due: str | None = None,
        project_id: str | None = None,
        blocked_by: list[str] | None = None,
        owner: str | None = None,
    ) -> Concept:
        body = {
            "title": title,
            "description": description,
            "status": status,
            "priority": priority,
            "due": due,
            "project_id": project_id,
            "blocked_by": blocked_by,
            "owner": owner,
        }
        r = self._http.post("/tasks", json=body)
        r.raise_for_status()
        return Concept.model_validate(r.json())

    def update_task(
        self,
        task_id: str,
        status: str | None = None,
        priority: str | None = None,
        due: str | None = None,
        owner: str | None = None,
        description: str | None = None,
    ) -> Concept | None:
        body = {
            k: v
            for k, v in {
                "status": status,
                "priority": priority,
                "due": due,
                "owner": owner,
                "description": description,
            }.items()
            if v is not None
        }
        r = self._http.patch(f"/tasks/{task_id}", json=body)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return Concept.model_validate(r.json())

    def list_tasks(
        self,
        status: str = "pending",
        project_id: str | None = None,
        owner: str | None = None,
        limit: int = 50,
    ) -> list[Concept]:
        params: dict[str, Any] = {"status": status, "limit": limit}
        if project_id:
            params["project_id"] = project_id
        if owner:
            params["owner"] = owner
        r = self._http.get("/tasks", params=params)
        r.raise_for_status()
        return [Concept.model_validate(c) for c in r.json()]

    def next_actions(self, limit: int = 5) -> list[Concept]:
        r = self._http.get("/next-actions", params={"limit": limit})
        r.raise_for_status()
        return [Concept.model_validate(c) for c in r.json()]

    def stats(self) -> dict[str, Any]:
        r = self._http.get("/health")
        r.raise_for_status()
        data = r.json()
        # /health includes {"ok", "version", ...stats}; drop the meta keys.
        return {k: v for k, v in data.items() if k not in {"ok", "version"}}

    # ---- lifecycle -----------------------------------------------------

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> MemexClient:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


def _enum_value(value: Any, enum_cls: type) -> str:
    """Accept either an Enum or its string value; return the string."""
    if value is None:
        return value
    if isinstance(value, enum_cls):
        return value.value
    return str(value)
