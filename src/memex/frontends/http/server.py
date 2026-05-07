"""
HTTP frontend (FastAPI).

Class-based, no module globals — multiple HTTPFrontend instances coexist
cleanly (e.g., one for the real engine, one for tests). Auth and engine
are injected via the constructor; the FastAPI `app` object is built lazily.

Defaults:
  - bind to 127.0.0.1 (local-only)
  - non-localhost binding without an auth_token raises at construction time
    — loud failure, not silent insecurity
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import Body, Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from memex import __version__
from memex.core.engine import Engine
from memex.core.schema import EdgeKind, NodeKind, Source

log = logging.getLogger(__name__)


_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}  # 0.0.0.0 still requires token


# Request bodies must live at module scope — Pydantic v2 / FastAPI can't
# resolve ForwardRefs for classes defined inside a function (the closure
# fails to provide the right scope, and `_type_adapter` is never built).
class AddNodeBody(BaseModel):
    name: str
    description: str = ""
    kind: NodeKind = NodeKind.fact
    source: Source = Source.agent
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    verification: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    related_to: list[str] | None = None


class AddEdgeBody(BaseModel):
    from_id: str
    to_id: str
    kind: EdgeKind = EdgeKind.relates_to
    source: Source = Source.agent
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ObserveBody(BaseModel):
    kind: str
    actor: Source = Source.agent
    payload: dict[str, Any] = Field(default_factory=dict)


class ValidateBody(BaseModel):
    skill: str
    actor: Source = Source.agent


class InstallSkillBody(BaseModel):
    name: str


class AddTaskBody(BaseModel):
    title: str
    description: str = ""
    status: str = "pending"
    priority: str = "p2"
    due: str | None = None
    project_id: str | None = None
    blocked_by: list[str] | None = None
    owner: str | None = None


class UpdateTaskBody(BaseModel):
    status: str | None = None
    priority: str | None = None
    due: str | None = None
    owner: str | None = None
    description: str | None = None


class InsecureBindingError(RuntimeError):
    """Raised when caller asked to bind a non-localhost address without an auth token."""


class HTTPFrontend:
    """Class-based HTTP frontend. Build one, call `app` for FastAPI, `run` to serve."""

    def __init__(
        self,
        engine: Engine,
        *,
        host: str = "127.0.0.1",
        port: int = 7777,
        auth_token: str | None = None,
    ):
        if host not in {"127.0.0.1", "localhost", "::1"} and not auth_token:
            raise InsecureBindingError(
                f"refusing to expose memex on non-localhost address `{host}` "
                "without an auth_token. Pass auth_token=<secret> or bind to 127.0.0.1."
            )
        self.engine = engine
        self.host = host
        self.port = port
        self.auth_token = auth_token
        self._app: FastAPI | None = None

    @property
    def app(self) -> FastAPI:
        if self._app is None:
            self._app = self._build_app()
        return self._app

    # ---- routes --------------------------------------------------------

    def _build_app(self) -> FastAPI:
        app = FastAPI(
            title="memex",
            version=__version__,
            description="Persistent cognitive memory for AI coding tools.",
        )
        engine = self.engine
        check_auth = self._make_auth_dependency()

        @app.get("/health", tags=["meta"])
        def health() -> dict[str, Any]:
            return {"ok": True, "version": __version__, **engine.stats()}

        @app.get("/recall", tags=["query"], dependencies=[Depends(check_auth)])
        def recall(
            q: str,
            budget: int = 2000,
            kind: NodeKind | None = None,
            expand: int = 1,
        ) -> dict[str, Any]:
            return engine.recall(
                query=q, budget_tokens=budget, kind=kind, expand_hops=expand
            ).to_dict()

        @app.post("/nodes", tags=["write"], dependencies=[Depends(check_auth)])
        def add_node(body: AddNodeBody = Body(...)) -> dict[str, Any]:
            c = engine.add(**body.model_dump())
            return c.model_dump(mode="json")

        @app.get("/nodes/{concept_id}", tags=["read"], dependencies=[Depends(check_auth)])
        def get_node(concept_id: str) -> dict[str, Any]:
            c = engine.get(concept_id)
            if c is None:
                raise HTTPException(status_code=404, detail="not found")
            return c.model_dump(mode="json")

        @app.post("/edges", tags=["write"], dependencies=[Depends(check_auth)])
        def add_edge(body: AddEdgeBody = Body(...)) -> dict[str, str]:
            engine.link(**body.model_dump())
            return {"status": "ok"}

        @app.post("/observe", tags=["write"], dependencies=[Depends(check_auth)])
        def observe(body: ObserveBody = Body(...)) -> dict[str, str]:
            ev = engine.observe(kind=body.kind, actor=body.actor, payload=body.payload)
            return {"id": ev.id}

        @app.post("/validate", tags=["skills"], dependencies=[Depends(check_auth)])
        def validate(body: ValidateBody = Body(...)) -> dict[str, Any]:
            return engine.validate(body.skill, actor=body.actor)

        @app.get("/progress", tags=["skills"], dependencies=[Depends(check_auth)])
        def progress(actor: Source | None = None) -> dict[str, Any]:
            return engine.progress(actor=actor)

        @app.get("/skills", tags=["skills"], dependencies=[Depends(check_auth)])
        def list_skills() -> list[dict[str, Any]]:
            from memex.skills import list_builtin_skills

            return [
                {"name": s.name, "version": s.version, "description": s.description}
                for s in list_builtin_skills()
            ]

        @app.post("/skills/install", tags=["skills"], dependencies=[Depends(check_auth)])
        def install_skill_endpoint(body: InstallSkillBody = Body(...)) -> dict[str, Any]:
            from memex.skills import find_builtin_skill, install_skill

            skill = find_builtin_skill(body.name)
            if skill is None:
                raise HTTPException(status_code=404, detail=f"skill `{body.name}` not found")
            return install_skill(engine, skill)

        # ---- tasks ----
        @app.post("/tasks", tags=["tasks"], dependencies=[Depends(check_auth)])
        def add_task_endpoint(body: AddTaskBody = Body(...)) -> dict[str, Any]:
            return engine.add_task(**body.model_dump()).model_dump(mode="json")

        @app.patch("/tasks/{task_id}", tags=["tasks"], dependencies=[Depends(check_auth)])
        def update_task_endpoint(task_id: str, body: UpdateTaskBody = Body(...)) -> dict[str, Any]:
            updated = engine.update_task(task_id=task_id, **body.model_dump(exclude_none=True))
            if updated is None:
                raise HTTPException(status_code=404, detail=f"task `{task_id}` not found")
            return updated.model_dump(mode="json")

        @app.get("/tasks", tags=["tasks"], dependencies=[Depends(check_auth)])
        def list_tasks_endpoint(
            status: str = "pending",
            project_id: str | None = None,
            owner: str | None = None,
            limit: int = 50,
        ) -> list[dict[str, Any]]:
            tasks = engine.list_tasks(
                status=status, project_id=project_id, owner=owner, limit=limit
            )
            return [t.model_dump(mode="json") for t in tasks]

        @app.get("/next-actions", tags=["tasks"], dependencies=[Depends(check_auth)])
        def next_actions_endpoint(limit: int = 5) -> list[dict[str, Any]]:
            tasks = engine.next_actions(limit=limit)
            return [t.model_dump(mode="json") for t in tasks]

        return app

    # ---- helpers -------------------------------------------------------

    def _make_auth_dependency(self):
        token = self.auth_token

        def check(authorization: Annotated[str | None, Header()] = None) -> None:
            if token is None:
                return
            if not authorization or not authorization.startswith("Bearer "):
                raise HTTPException(status_code=401, detail="missing bearer token")
            if authorization.removeprefix("Bearer ").strip() != token:
                raise HTTPException(status_code=403, detail="invalid bearer token")

        return check

    def run(self) -> None:
        """Start the HTTP daemon. Blocks until shutdown."""
        import uvicorn

        log.info(
            "memex HTTP daemon listening on %s:%d (auth=%s)",
            self.host,
            self.port,
            "yes" if self.auth_token else "no",
        )
        uvicorn.run(self.app, host=self.host, port=self.port, log_level="info")


def run_http(
    host: str = "127.0.0.1",
    port: int = 7777,
    auth_token: str | None = None,
    engine: Engine | None = None,
) -> None:
    """Convenience entry: build engine if not provided, then serve."""
    engine = engine or Engine.build_default()
    frontend = HTTPFrontend(engine=engine, host=host, port=port, auth_token=auth_token)
    frontend.run()
