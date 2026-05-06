"""
MCP stdio frontend (Model Context Protocol).

Class-based wrapper around the official `mcp` Python SDK's FastMCP. Tool
implementations delegate to the engine façade — same shapes as the HTTP
endpoints, so behaviour is consistent across frontends.
"""

from __future__ import annotations

import logging
from typing import Any

from memex.core.engine import Engine
from memex.core.schema import EdgeKind, NodeKind, Source

log = logging.getLogger(__name__)


class MCPServer:
    """MCP server wrapping a memex Engine. Run via `serve_stdio()`."""

    def __init__(self, engine: Engine, *, name: str = "memex"):
        try:
            from mcp.server.fastmcp import FastMCP
        except ImportError as e:  # pragma: no cover
            raise RuntimeError(
                "mcp Python SDK not installed. This is a required dependency — "
                "reinstall memex via `pipx install --force memex`."
            ) from e
        self.engine = engine
        self.mcp = FastMCP(name)
        self._register_tools()

    def _register_tools(self) -> None:
        engine = self.engine
        mcp = self.mcp

        @mcp.tool()
        def recall(
            query: str,
            budget_tokens: int = 2000,
            kind: str | None = None,
            expand_hops: int = 1,
        ) -> dict[str, Any]:
            """Retrieve a budget-bounded subgraph relevant to `query`.

            Always call this before generating code in non-trivial contexts.
            """
            kind_enum = NodeKind(kind) if kind else None
            return engine.recall(
                query=query,
                budget_tokens=budget_tokens,
                kind=kind_enum,
                expand_hops=expand_hops,
            ).to_dict()

        @mcp.tool()
        def add_node(
            name: str,
            description: str = "",
            kind: str = "fact",
            source: str = "agent",
            confidence: float = 1.0,
            verification: str | None = None,
        ) -> dict[str, Any]:
            """Add a concept (fact, decision, pattern, constraint, ...) to memex.

            Use when you discover something the next session should know.
            """
            c = engine.add(
                name=name,
                description=description,
                kind=NodeKind(kind),
                source=Source(source),
                confidence=confidence,
                verification=verification,
            )
            return c.model_dump(mode="json")

        @mcp.tool()
        def link(
            from_id: str,
            to_id: str,
            kind: str = "relates_to",
            source: str = "agent",
        ) -> dict[str, str]:
            """Create a typed edge between two concepts."""
            engine.link(
                from_id=from_id,
                to_id=to_id,
                kind=EdgeKind(kind),
                source=Source(source),
            )
            return {"status": "ok"}

        @mcp.tool()
        def observe(
            kind: str,
            actor: str = "agent",
            payload: dict[str, Any] | None = None,
        ) -> dict[str, str]:
            """Emit an episodic event (something happened, time-indexed)."""
            ev = engine.observe(kind=kind, actor=Source(actor), payload=payload or {})
            return {"id": ev.id}

        @mcp.tool()
        def validate(skill: str, actor: str = "agent") -> dict[str, Any]:
            """Get the structured `approach + checks + examples` for a skill.

            Call BEFORE generating code in security-, money-, concurrency-,
            crypto-, SQL-, or untrusted-input-handling contexts. You must
            self-attest each check passes; refuse to generate if it can't.
            Validation is auto-recorded — query `progress()` to see your trail.
            """
            return engine.validate(skill, actor=Source(actor))

        @mcp.tool()
        def progress(actor: str | None = None) -> dict[str, Any]:
            """Summary of what's been validated, observed, and added in this store."""
            return engine.progress(actor=Source(actor) if actor else None)

        @mcp.tool()
        def list_skills() -> list[dict[str, Any]]:
            """List installed skill bundles."""
            from memex.skills import list_builtin_skills

            return [
                {"name": s.name, "version": s.version, "description": s.description}
                for s in list_builtin_skills()
            ]

        @mcp.tool()
        def install_skill(name: str) -> dict[str, Any]:
            """Install a builtin skill bundle by name (e.g. 'core-validations')."""
            from memex.skills import find_builtin_skill
            from memex.skills import install_skill as _install

            skill = find_builtin_skill(name)
            if skill is None:
                return {"ok": False, "error": f"skill `{name}` not found"}
            counts = _install(engine, skill)
            return {"ok": True, **counts}

        @mcp.tool()
        def stats() -> dict[str, Any]:
            """Storage stats and tier status."""
            return engine.stats()

    def serve_stdio(self) -> None:
        """Run the stdio MCP server. Blocks for the editor's lifetime."""
        self.mcp.run()


def run_stdio(engine: Engine | None = None) -> None:
    """Convenience entry: build engine if not provided, then run stdio MCP."""
    engine = engine or Engine.build_default()
    MCPServer(engine).serve_stdio()
