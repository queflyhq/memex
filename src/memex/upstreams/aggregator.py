"""
Aggregator that connects to upstream MCP servers and re-exposes their tools.

Architecture:
  - Each upstream is a long-lived async connection. Two transports ship:
      * `UpstreamStdioConnection` — subprocess via stdin/stdout
      * `UpstreamHTTPConnection`  — Streamable HTTP / SSE per the MCP spec
  - All upstream I/O is async (mcp SDK uses anyio). Memex's MCP server
    surface is sync (FastMCP `@mcp.tool()` accepts both, but proxy handlers
    need to dispatch into the async world).
  - We use anyio's `BlockingPortal` to run a dedicated background event
    loop on a worker thread; sync handlers call into it via `portal.call()`
    and block until the upstream returns.
  - Connections are opened at `start()` and closed at `stop()`. Failures to
    connect are logged and skipped — a dead upstream must not take memex down.
  - Every proxied call is wrapped with `engine.observe(kind="tool_call", ...)`
    so the perception layer fills automatically.

Failure modes:
  - Upstream subprocess crashes / HTTP server unreachable: next `call()`
    raises; surfaced to the AI client as a tool error. Auto-reconnect is
    a v0.7 concern.
  - Upstream returns a result that isn't JSON-serializable: we log and
    return a structured error to the AI client; the tool_call event still
    records the attempt.
"""

from __future__ import annotations

import fnmatch
import logging
import threading
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any, Protocol

from memex.upstreams.config import UpstreamConfig

log = logging.getLogger(__name__)


@dataclass
class ProxiedTool:
    """One tool re-exported from an upstream."""

    upstream: str          # config.name
    upstream_tool: str     # original tool name on the upstream
    exposed_name: str      # name as registered on memex's MCP server
    description: str
    input_schema: dict[str, Any] = field(default_factory=dict)


class _ConnectionLike(Protocol):
    """Common shape for stdio + http upstream connections."""

    config: UpstreamConfig
    tools: list[Any]
    connected: bool
    last_error: str | None

    async def open(self) -> None: ...
    async def call(self, tool_name: str, arguments: dict[str, Any]) -> Any: ...
    async def close(self) -> None: ...


class _BaseUpstreamConnection:
    """Shared lifecycle bookkeeping for stdio + http connections."""

    def __init__(self, config: UpstreamConfig):
        self.config = config
        self._stack: AsyncExitStack | None = None
        self._session: Any = None
        self.tools: list[Any] = []
        self.connected: bool = False
        self.last_error: str | None = None

    async def _post_init_session(self, session: Any) -> None:
        """Initialize MCP session and list tools — common for both transports."""
        await session.initialize()
        tools_result = await session.list_tools()
        self._session = session
        self.tools = list(tools_result.tools)
        self.connected = True
        self.last_error = None
        log.info(
            "upstream `%s` (%s) connected: %d tool(s)",
            self.config.name,
            self.config.type,
            len(self.tools),
        )

    async def call(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        if not self._session:
            raise RuntimeError(f"upstream `{self.config.name}` not connected")
        return await self._session.call_tool(tool_name, arguments)

    async def close(self) -> None:
        if self._stack is not None:
            try:
                await self._stack.aclose()
            finally:
                self._stack = None
                self._session = None
                self.connected = False


class UpstreamStdioConnection(_BaseUpstreamConnection):
    """A long-lived async connection to one stdio upstream MCP server.

    Lifecycle:
      open()  → spawn subprocess, init MCP session, list tools
      call()  → forward a tool call, return upstream result
      close() → tear down session + subprocess
    """

    async def open(self) -> None:
        from mcp.client.session import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client

        if self.config.type != "stdio":
            raise RuntimeError(f"wrong connection class for type `{self.config.type}`")
        if not self.config.command:
            raise ValueError(f"upstream `{self.config.name}` missing command")

        params = StdioServerParameters(
            command=self.config.command,
            args=self.config.args,
            env={**self.config.env} if self.config.env else None,
            cwd=self.config.cwd,
        )

        self._stack = AsyncExitStack()
        try:
            transport = await self._stack.enter_async_context(stdio_client(params))
            read_stream, write_stream = transport
            session = await self._stack.enter_async_context(
                ClientSession(read_stream, write_stream)
            )
            await self._post_init_session(session)
        except Exception as e:
            self.last_error = str(e)
            self.connected = False
            try:
                if self._stack:
                    await self._stack.aclose()
            except Exception:
                pass
            self._stack = None
            self._session = None
            raise


class UpstreamHTTPConnection(_BaseUpstreamConnection):
    """A long-lived async connection to one HTTP upstream MCP server.

    Uses the Streamable HTTP transport (mcp.client.streamable_http). For
    servers that only speak the older SSE transport, the same `url` works
    if it ends with the SSE endpoint — but we default to streamable_http
    since that's the spec direction.

    Auth headers are resolved at connect time from `AuthConfig` (env-var
    or OS keychain). The token is never persisted to upstreams.json.
    """

    async def open(self) -> None:
        from mcp.client.session import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        if self.config.type != "http":
            raise RuntimeError(f"wrong connection class for type `{self.config.type}`")
        if not self.config.url:
            raise ValueError(f"upstream `{self.config.name}` missing url")

        headers = self._build_headers()

        self._stack = AsyncExitStack()
        try:
            transport = await self._stack.enter_async_context(
                streamablehttp_client(
                    url=self.config.url,
                    headers=headers,
                    timeout=self.config.timeout_seconds,
                )
            )
            # streamable_http_client yields (read_stream, write_stream, get_session_id)
            read_stream, write_stream, _ = transport
            session = await self._stack.enter_async_context(
                ClientSession(read_stream, write_stream)
            )
            await self._post_init_session(session)
        except Exception as e:
            self.last_error = str(e)
            self.connected = False
            try:
                if self._stack:
                    await self._stack.aclose()
            except Exception:
                pass
            self._stack = None
            self._session = None
            raise

    def _build_headers(self) -> dict[str, str]:
        headers = dict(self.config.headers)
        auth = self.config.auth
        if auth and auth.kind != "none":
            token = auth.resolve_token()
            if token:
                if auth.kind == "bearer" or auth.kind == "oauth2":
                    headers["Authorization"] = f"Bearer {token}"
                elif auth.kind == "header":
                    if not auth.header_name:  # belt-and-braces; validator catches this
                        raise RuntimeError(
                            f"upstream `{self.config.name}` auth.header_name missing"
                        )
                    headers[auth.header_name] = token
        return headers


# Backward compat alias — pre-0.7 code referenced `UpstreamConnection` for the
# stdio class. Keep the name resolvable so external callers don't break.
UpstreamConnection = UpstreamStdioConnection


def _connection_for(config: UpstreamConfig) -> _BaseUpstreamConnection:
    if config.type == "stdio":
        return UpstreamStdioConnection(config)
    if config.type == "http":
        return UpstreamHTTPConnection(config)
    raise ValueError(f"unknown upstream type `{config.type}`")


class Aggregator:
    """Coordinates upstream connections and synchronous proxy dispatch.

    Threading model:
      - One background daemon thread runs anyio's blocking portal (own loop).
      - All upstream coroutines run on that loop.
      - Sync proxy handlers call `portal.call(coro)` which blocks the caller
        until the upstream returns.

    This keeps the FastMCP synchronous tool API working without forcing the
    SDK to be async-aware everywhere.
    """

    def __init__(self, configs: list[UpstreamConfig]):
        self.configs = list(configs)
        self.connections: dict[str, _BaseUpstreamConnection] = {}
        self._portal: Any = None
        self._portal_cm: Any = None
        self._portal_thread: threading.Thread | None = None
        self._started = False

    # ---- lifecycle -----------------------------------------------------

    def start(self) -> None:
        """Open the background event loop and connect to all upstreams.

        Connection failures per upstream are logged and skipped; the rest
        proceed.
        """
        if self._started:
            return
        from anyio.from_thread import start_blocking_portal

        self._portal_cm = start_blocking_portal()
        self._portal = self._portal_cm.__enter__()
        self._started = True

        for cfg in self.configs:
            conn = _connection_for(cfg)
            try:
                self._portal.call(conn.open)
                self.connections[cfg.name] = conn
            except Exception as e:
                log.warning(
                    "upstream `%s` (%s) failed to connect: %s — skipping",
                    cfg.name,
                    cfg.type,
                    e,
                )
                # Keep the entry so doctor can show the failure.
                self.connections[cfg.name] = conn

    def stop(self) -> None:
        if not self._started:
            return
        for conn in list(self.connections.values()):
            try:
                self._portal.call(conn.close)
            except Exception as e:
                log.warning("error closing upstream `%s`: %s", conn.config.name, e)
        try:
            if self._portal_cm is not None:
                self._portal_cm.__exit__(None, None, None)
        finally:
            self._portal = None
            self._portal_cm = None
            self._started = False

    # ---- introspection -------------------------------------------------

    def proxied_tools(self) -> list[ProxiedTool]:
        """Flat list of every upstream tool, after allow/deny filtering."""
        out: list[ProxiedTool] = []
        for name, conn in self.connections.items():
            if not conn.connected:
                continue
            cfg = conn.config
            prefix = cfg.render_prefix()
            for t in conn.tools:
                tool_name = getattr(t, "name", None) or t["name"]
                if not _matches_any(tool_name, cfg.allow):
                    continue
                if _matches_any(tool_name, cfg.deny):
                    continue
                desc = getattr(t, "description", None) or t.get("description", "") or ""
                schema = getattr(t, "inputSchema", None) or t.get("inputSchema") or {}
                exposed = f"{prefix}{tool_name}"
                out.append(
                    ProxiedTool(
                        upstream=name,
                        upstream_tool=tool_name,
                        exposed_name=exposed,
                        description=f"[via {name}] {desc}".strip(),
                        input_schema=schema,
                    )
                )
        return out

    def status(self) -> list[dict[str, Any]]:
        """Doctor-friendly summary of every configured upstream."""
        rows: list[dict[str, Any]] = []
        for cfg in self.configs:
            conn = self.connections.get(cfg.name)
            target: str
            if cfg.type == "stdio":
                target = f"{cfg.command or ''} {' '.join(cfg.args)}".strip()
            elif cfg.type == "http":
                target = cfg.url or ""
            else:
                target = ""
            rows.append({
                "name": cfg.name,
                "type": cfg.type,
                "target": target,
                "connected": bool(conn and conn.connected),
                "tools": len(conn.tools) if conn and conn.connected else 0,
                "error": (conn.last_error if conn else None),
            })
        return rows

    # ---- dispatch ------------------------------------------------------

    def call(self, upstream: str, tool: str, arguments: dict[str, Any]) -> Any:
        """Synchronous dispatch — blocks until the upstream returns."""
        if not self._started or self._portal is None:
            raise RuntimeError("aggregator not started")
        conn = self.connections.get(upstream)
        if conn is None or not conn.connected:
            raise RuntimeError(f"upstream `{upstream}` not connected")
        return self._portal.call(conn.call, tool, arguments)


def _matches_any(name: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(name, p) for p in patterns)
