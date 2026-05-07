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
import time
from collections import Counter, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Any


# In-memory ring buffer of recent log records — populated by a log
# handler attached at server-build time. Read by /logs.
_LOG_RING: deque = deque(maxlen=400)
_DAEMON_STARTED_AT: float = time.time()


class _RingHandler(logging.Handler):
    """Logging handler that appends rendered records to a deque."""

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D401
        try:
            msg = self.format(record)
            _LOG_RING.append({
                "ts": datetime.fromtimestamp(
                    record.created, timezone.utc
                ).isoformat(),
                "level": record.levelname,
                "logger": record.name,
                "msg": msg,
            })
        except Exception:
            pass


def _attach_ring_handler() -> None:
    """Idempotent — attach once on first call."""
    root = logging.getLogger()
    for h in root.handlers:
        if isinstance(h, _RingHandler):
            return
    h = _RingHandler(level=logging.INFO)
    h.setFormatter(logging.Formatter("%(message)s"))
    root.addHandler(h)

from fastapi import Body, Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from memex import __version__
from memex.core.engine import Engine
from memex.core.schema import EdgeKind, NodeKind, Source

log = logging.getLogger(__name__)


def _db_schema(engine: Engine) -> dict[str, Any]:
    """Introspect the DuckDB connection's tables + columns + row counts."""
    conn = engine.semantic.conn
    lock = engine.semantic._lock
    with lock:
        # Tables in the main DB.
        rows = conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'main' ORDER BY table_name"
        ).fetchall()
        tables: list[dict[str, Any]] = []
        for (tname,) in rows:
            cols = conn.execute(
                "SELECT column_name, data_type, is_nullable "
                "FROM information_schema.columns "
                "WHERE table_schema = 'main' AND table_name = ? "
                "ORDER BY ordinal_position",
                [tname],
            ).fetchall()
            try:
                count = conn.execute(
                    f"SELECT count(*) FROM {tname}"
                ).fetchone()[0]
            except Exception:  # noqa: BLE001
                count = None
            tables.append({
                "name": tname,
                "row_count": count,
                "columns": [
                    {"name": c[0], "type": c[1], "nullable": (c[2] == "YES")}
                    for c in cols
                ],
            })
    return {"tables": tables}


def _db_table_rows(
    engine: Engine, table: str, *, limit: int = 50, offset: int = 0,
) -> dict[str, Any]:
    """Read-only sample of rows from a table. Whitelisted to prevent SQL
    injection via the path param."""
    allowed = {
        "concepts", "edges", "events", "vectors",
        "concept_history",
    }
    if table not in allowed:
        raise HTTPException(status_code=400, detail=f"unknown table: {table}")
    if limit < 1 or limit > 500:
        limit = 50
    if offset < 0:
        offset = 0
    conn = engine.semantic.conn
    lock = engine.semantic._lock
    with lock:
        cols = [
            r[0] for r in conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'main' AND table_name = ? "
                "ORDER BY ordinal_position",
                [table],
            ).fetchall()
        ]
        if table == "vectors":
            # Join concept names so the user sees what each vector embeds.
            # Show vector dim + magnitude + first 4 components as preview
            # — never the full 384-float array.
            select = (
                "v.id, c.name, c.kind, "
                "len(v.embedding) AS dim, "
                "round(sqrt(list_aggregate(list_transform(v.embedding, x -> x*x), 'sum'))::DOUBLE, 3) AS magnitude, "
                "list_transform(v.embedding[1:4], x -> round(x::DOUBLE, 3)) AS preview"
            )
            rows = conn.execute(
                f"SELECT {select} FROM vectors v "
                f"LEFT JOIN concepts c ON c.id = v.id "
                f"ORDER BY c.kind, c.name LIMIT ? OFFSET ?",
                [limit, offset],
            ).fetchall()
            cols_returned = ["id", "name", "kind", "dim", "magnitude", "preview"]
        else:
            select = ", ".join(cols)
            rows = conn.execute(
                f"SELECT {select} FROM {table} "
                f"ORDER BY 1 LIMIT ? OFFSET ?",
                [limit, offset],
            ).fetchall()
            cols_returned = cols
    return {
        "table": table,
        "columns": cols_returned,
        "rows": [
            {c: _stringify(v) for c, v in zip(cols_returned, row)}
            for row in rows
        ],
        "limit": limit,
        "offset": offset,
    }


def _stringify(v: Any) -> Any:
    """Render DuckDB row values as JSON-safe — datetimes → ISO strings,
    dicts/lists pass through, bytes → repr."""
    from datetime import date, datetime as _dt
    if isinstance(v, (_dt, date)):
        return v.isoformat()
    if isinstance(v, (bytes, bytearray)):
        return repr(v[:80])
    return v


def _compute_stats(
    engine: Engine,
    *,
    window_hours: int | None = None,
) -> dict[str, Any]:
    """Aggregate the desktop Dashboard / Impact payload.

    Pulls concept counts (by kind), edge counts (by kind), event counts
    (by kind + actor), task status breakdown, vector index size, and
    derived "impact" tiles from the episodic stream. Window-clip via
    `window_hours` for "this session / today / this week" tabs.
    """
    base_engine_stats = engine.stats()
    # Use single-pass SQL aggregates — at ~8.8k concepts + ~19k edges,
    # iterating per-concept like the original implementation made /stats
    # take >10s. SQL aggregates land in <100ms.
    conn = engine.semantic.conn
    lock = engine.semantic._lock
    with lock:
        concepts_total = conn.execute(
            "SELECT count(*) FROM concepts"
        ).fetchone()[0]
        concepts_by_kind = dict(conn.execute(
            "SELECT kind, count(*) FROM concepts GROUP BY kind"
        ).fetchall())
        edges_total = conn.execute(
            "SELECT count(*) FROM edges"
        ).fetchone()[0]
        edges_by_kind = dict(conn.execute(
            "SELECT kind, count(*) FROM edges GROUP BY kind"
        ).fetchall())
        tasks_by_status_rows = conn.execute(
            """
            SELECT json_extract_string(metadata, '$.status') AS status,
                   count(*) AS n
            FROM concepts WHERE kind = 'task' GROUP BY status
            """
        ).fetchall()
    tasks_by_status = {(s or "pending"): n for s, n in tasks_by_status_rows}

    # Aggregate events via SQL — earlier we sampled engine.episodic.recent(
    # 10_000), which blew up after the cross-repo linker dumped 13.4k
    # edge_added events into the stream and drowned every other kind in
    # the sample window. SQL GROUP BY scales to any volume.
    epi_conn = engine.episodic.conn
    epi_lock = engine.episodic._lock
    where = ""
    params: list[Any] = []
    if window_hours is not None and window_hours > 0:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)
        where = "WHERE timestamp >= ?"
        params.append(cutoff)
    with epi_lock:
        events_total_window = int(epi_conn.execute(
            f"SELECT count(*) FROM events {where}", params,
        ).fetchone()[0])
        events_by_kind = dict(epi_conn.execute(
            f"SELECT kind, count(*) FROM events {where} GROUP BY kind",
            params,
        ).fetchall())
        events_by_actor = dict(epi_conn.execute(
            f"SELECT actor, count(*) FROM events {where} GROUP BY actor",
            params,
        ).fetchall())

    impact_tiles = {
        "auto_approvals": events_by_kind.get("auto_approval", 0),
        "auto_denies":    events_by_kind.get("auto_deny", 0),
        "secrets_redacted": events_by_kind.get("secret_redacted", 0),
        "user_corrections": events_by_kind.get("user_correction", 0),
        "files_reindexed": events_by_kind.get("file_reindexed", 0),
        # Sum every hook-fire kind so the tile reflects real Claude Code
        # activity. Hooks emit tool_pre / tool_post; legacy tool_call also
        # counted for forward-compat with older hook configs.
        "tool_calls_observed": (
            events_by_kind.get("tool_pre", 0)
            + events_by_kind.get("tool_post", 0)
            + events_by_kind.get("tool_call", 0)
        ),
        "user_prompts": events_by_kind.get("user_prompt", 0),
        "afk_sessions": events_by_kind.get("afk_enabled", 0),
        "comments_added": events_by_kind.get("comment_added", 0),
        "turns_observed": events_by_kind.get("turn_end", 0),
        # Recall + write activity — closes the "what did memex actually do
        # for me" loop on the dashboard. Recalls = how often AI asked memex.
        "recalls": events_by_kind.get("recall_executed", 0),
        "concepts_added": events_by_kind.get("concept_added", 0),
        "concepts_deleted": events_by_kind.get("concept_deleted", 0),
        "edges_added": events_by_kind.get("edge_added", 0),
        "skills_validated": events_by_kind.get("skill_validated", 0),
        "skills_installed": events_by_kind.get("skill_installed", 0),
        "policies_changed": (
            events_by_kind.get("policy_added", 0)
            + events_by_kind.get("policy_removed", 0)
        ),
        "sources_indexed": events_by_kind.get("source_indexed", 0),
        "consolidations": events_by_kind.get("consolidation_run", 0),
        # Tokens auto-injected via SessionStart + UserPromptSubmit hooks —
        # context the user didn't have to type and the AI didn't have to
        # re-derive via tool calls. Counted from context_injected events
        # whose payload.est_tokens is the char-count / 4 estimate.
        "context_injections": events_by_kind.get("context_injected", 0),
    }
    # Sum est_tokens directly from context_injected payloads — events_by_kind
    # only counts how many fired; the total tokens needs payload aggregation.
    token_sql = (
        "SELECT coalesce(sum(cast(json_extract(payload, '$.est_tokens') AS BIGINT)), 0) "
        "FROM events WHERE kind = 'context_injected'"
    )
    token_params: list[Any] = []
    if window_hours is not None and window_hours > 0:
        token_sql += " AND timestamp >= ?"
        token_params.append(datetime.now(timezone.utc) - timedelta(hours=window_hours))
    with epi_lock:
        token_sum = int(epi_conn.execute(token_sql, token_params).fetchone()[0] or 0)
    impact_tiles["tokens_injected"] = token_sum

    # Per-actor by-kind breakdown — also via SQL for volume safety.
    by_actor: dict[str, dict[str, int]] = {}
    with epi_lock:
        rows = epi_conn.execute(
            f"SELECT actor, kind, count(*) FROM events {where} "
            f"GROUP BY actor, kind",
            params,
        ).fetchall()
    for actor, kind, n in rows:
        d = by_actor.setdefault(actor, {})
        d["events_total"] = d.get("events_total", 0) + int(n)
        d[kind] = int(n)

    # Recent activity strip — sample the most-recent 30 events via the
    # episodic store (cheap; doesn't grow with volume).
    recent_strip_events = engine.episodic.recent(limit=30)
    recent_strip = [
        {
            "timestamp": ev.timestamp.isoformat(),
            "kind": ev.kind,
            "actor": ev.actor.value,
            "payload": ev.payload,
        }
        for ev in recent_strip_events
    ]

    # Distinguish "events in the active window" from "events in DB total"
    # so the Dashboard can show "200 events this week of 28k total" when
    # window_hours is set, rather than misreporting either.
    events_in_db = engine.episodic.count()
    return {
        **base_engine_stats,
        "concepts_total": concepts_total,
        "concepts_by_kind": concepts_by_kind,
        "edges_total": edges_total,
        "edges_by_kind": edges_by_kind,
        "events_total": events_in_db,
        "events_in_window": events_total_window,
        "events_by_kind": dict(events_by_kind),
        "events_by_actor": dict(events_by_actor),
        # Aliases — keep compatibility with desktop tabs that read either
        # the engine-stats shape or the rich-stats shape.
        "vectors_total": base_engine_stats.get("vectors"),
        "vector_dim": getattr(engine.vector, "dim", 384),
        "by_actor": by_actor,
        "tasks_by_status": tasks_by_status,
        "impact": impact_tiles,
        "recent_events": recent_strip,
        "window_hours": window_hours,
    }


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
        # Capture log records into the in-memory ring so /logs has data.
        _attach_ring_handler()

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

        @app.get("/system", tags=["meta"], dependencies=[Depends(check_auth)])
        def system_info() -> dict[str, Any]:
            """Daemon process self-report: RAM, CPU, uptime, DB size,
            embedding model. Powers the desktop's bottom status bar."""
            import os
            data: dict[str, Any] = {}
            try:
                import psutil
                p = psutil.Process(os.getpid())
                with p.oneshot():
                    mem = p.memory_info()
                    data["ram_mb"] = round(mem.rss / 1024 / 1024, 1)
                    data["cpu_percent"] = p.cpu_percent(interval=0.05)
                    data["pid"] = p.pid
                    data["threads"] = p.num_threads()
            except Exception as e:  # noqa: BLE001
                data["ram_mb"] = None
                data["error"] = f"psutil unavailable: {e}"

            data["uptime_seconds"] = int(time.time() - _DAEMON_STARTED_AT)
            data["python"] = (
                f"{__import__('sys').version_info.major}."
                f"{__import__('sys').version_info.minor}."
                f"{__import__('sys').version_info.micro}"
            )

            # Database size — sum every memex DB / index file in data_dir.
            try:
                from memex.config import get_settings
                d = Path(str(get_settings().data_dir))
                total = 0
                breakdown: dict[str, int] = {}
                for f in d.iterdir():
                    if f.is_file():
                        sz = f.stat().st_size
                        breakdown[f.name] = sz
                        total += sz
                # Include secrets index
                sec = d / "secrets" / "secrets.json"
                if sec.is_file():
                    breakdown["secrets/secrets.json"] = sec.stat().st_size
                    total += breakdown["secrets/secrets.json"]
                data["db_bytes_total"] = total
                data["db_breakdown"] = breakdown
            except Exception as e:  # noqa: BLE001
                data["db_bytes_total"] = None
                data["db_error"] = str(e)

            data["embed_model"] = engine.stats().get("embed_model")
            data["embed_tier_available"] = engine.stats().get(
                "embed_tier_available", False
            )
            return data

        @app.get("/logs", tags=["meta"], dependencies=[Depends(check_auth)])
        def daemon_logs(limit: int = 200) -> dict[str, Any]:
            """Tail of the in-memory daemon log ring buffer.
            Capped at 400 entries (the buffer's max)."""
            limit = max(1, min(limit, 400))
            return {
                "logs": list(_LOG_RING)[-limit:],
                "buffer_size": len(_LOG_RING),
            }

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

        @app.post("/sources/add", tags=["write"], dependencies=[Depends(check_auth)])
        def add_source_endpoint(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
            """Register a codebase as a source and (by default) index it.
            Lets clients (desktop, CLI-via-daemon) add sources without
            having to spawn a separate engine that contests the DuckDB lock.
            Body: {"path": "...", "name": null, "index_now": true}."""
            from memex.codebase import (
                add_source as _add_source,
                index_source as _index_source,
            )
            path = body.get("path")
            name = body.get("name")
            index_now = bool(body.get("index_now", True))
            if not path:
                raise HTTPException(status_code=400, detail="missing path")
            try:
                src = _add_source(engine, path, name=name)
            except Exception as e:  # noqa: BLE001
                raise HTTPException(status_code=400, detail=f"add_source: {e}") from None
            payload: dict[str, Any] = {
                "id": src.id,
                "name": src.name,
                "path": src.metadata.get("path"),
                "indexed": False,
                "files": 0,
                "symbols": 0,
                "languages": {},
                "skipped_files": 0,
            }
            if index_now:
                try:
                    res = _index_source(engine, src.id)
                except Exception as e:  # noqa: BLE001
                    raise HTTPException(status_code=500, detail=f"index_source: {e}") from None
                payload.update({
                    "indexed": True,
                    "files": res.files_indexed,
                    "symbols": res.symbols_indexed,
                    "languages": res.languages,
                    "skipped_files": res.skipped_files,
                })
            return payload

        @app.get("/secrets", tags=["secrets"], dependencies=[Depends(check_auth)])
        def secrets_list_endpoint() -> dict[str, Any]:
            """List every registered secret handle. NEVER returns values."""
            from memex.config import get_settings as _settings
            from memex.secrets import SecretsStore
            store = SecretsStore(Path(str(_settings().data_dir)) / "secrets")
            return {
                "secrets": [
                    {
                        "handle": f"secret://{r.provider}/{r.name}",
                        "provider": r.provider,
                        "name": r.name,
                        "created_at": r.created_at,
                        "last_resolved_at": r.last_resolved_at,
                        "last_resolved_by": r.last_resolved_by,
                    }
                    for r in store.list()
                ],
            }

        @app.post("/secrets", tags=["secrets"], dependencies=[Depends(check_auth)])
        def secrets_put_endpoint(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
            """Store a secret in the OS keychain. Body: {provider, name, value}.
            Value lands in keychain; never persists in any concept/event/log."""
            from memex.config import get_settings as _settings
            from memex.secrets import SecretsStore
            provider = body.get("provider")
            name = body.get("name")
            value = body.get("value")
            if not provider or not name or not value:
                raise HTTPException(status_code=400,
                                    detail="provider, name, value all required")
            store = SecretsStore(Path(str(_settings().data_dir)) / "secrets")
            handle = store.put(provider, name, value)
            return {"handle": str(handle), "provider": provider, "name": name}

        @app.delete("/secrets/{provider}/{name}", tags=["secrets"], dependencies=[Depends(check_auth)])
        def secrets_delete_endpoint(provider: str, name: str) -> dict[str, Any]:
            from memex.config import get_settings as _settings
            from memex.secrets import SecretsStore
            store = SecretsStore(Path(str(_settings().data_dir)) / "secrets")
            removed = store.delete(f"secret://{provider}/{name}")
            return {"removed": removed}

        @app.post("/secrets/redact-preview", tags=["secrets"], dependencies=[Depends(check_auth)])
        def secrets_redact_preview(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
            """Preview what auto-redact would do. Does NOT store anything."""
            from memex.secrets.redact import _DEFAULT_PATTERNS
            text = body.get("text", "")
            events = []
            redacted = text
            for pat in _DEFAULT_PATTERNS:
                for m in pat.pattern.finditer(text):
                    events.append({
                        "pattern_name": pat.name,
                        "provider": pat.provider,
                        "severity": pat.severity,
                        "match_preview": m.group(0)[:8] + "…",
                        "span": [m.start(), m.end()],
                    })
                redacted = pat.pattern.sub(f"<<{pat.name}>>", redacted)
            return {
                "redacted_text": redacted,
                "events": events,
                "changed": bool(events),
            }

        @app.post("/upstreams/install", tags=["integrations"], dependencies=[Depends(check_auth)])
        def upstreams_install(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
            """Install a catalog entry into upstreams.json. Body:
              {"catalog_id": "github", "name": null}
            Daemon needs a restart for the new upstream to become active —
            response includes a restart_required:true flag."""
            from memex.upstreams.catalog import find_entry
            from memex.upstreams.config import UpstreamConfig, UpstreamsFile
            from memex.upstreams import load_upstreams
            import json as _json
            catalog_id = body.get("catalog_id") or body.get("id")
            name_override = body.get("name")
            if not catalog_id:
                raise HTTPException(status_code=400, detail="catalog_id required")
            e = find_entry(catalog_id)
            if e is None:
                raise HTTPException(status_code=404, detail=f"not in catalog: {catalog_id}")
            rendered = e.render()
            if name_override:
                rendered["name"] = name_override
            new_cfg = UpstreamConfig.model_validate(rendered)
            # Read existing config + append. Prefer project-local
            # ./.memex/upstreams.json when its parent dir exists, else
            # ~/.memex/upstreams.json (user scope) — same precedence as
            # the memex CLI.
            from pathlib import Path as _P
            proj = _P.cwd() / ".memex" / "upstreams.json"
            path = proj if proj.parent.is_dir() else _P.home() / ".memex" / "upstreams.json"
            file_obj: UpstreamsFile
            if path.is_file():
                file_obj = UpstreamsFile.model_validate(
                    _json.loads(path.read_text(encoding="utf-8"))
                )
            else:
                file_obj = UpstreamsFile()
            if any(u.name == new_cfg.name for u in file_obj.upstreams):
                raise HTTPException(
                    status_code=409,
                    detail=f"upstream `{new_cfg.name}` already configured",
                )
            file_obj.upstreams.append(new_cfg)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                _json.dumps(file_obj.model_dump(mode="json"), indent=2),
                encoding="utf-8",
            )
            return {
                "name": new_cfg.name,
                "path": str(path),
                "setup_steps": list(e.setup_steps or []),
                "paired_skills": list(e.paired_skills or []),
                "restart_required": True,
            }

        @app.delete("/upstreams/{name}", tags=["integrations"], dependencies=[Depends(check_auth)])
        def upstreams_remove(name: str) -> dict[str, Any]:
            """Remove an upstream from upstreams.json. Daemon restart
            needed to actually drop the active connection."""
            from memex.upstreams.config import UpstreamsFile
            from pathlib import Path as _P
            import json as _json
            proj = _P.cwd() / ".memex" / "upstreams.json"
            path = proj if proj.is_file() else _P.home() / ".memex" / "upstreams.json"
            if not path.is_file():
                raise HTTPException(status_code=404, detail="no upstreams.json")
            file_obj = UpstreamsFile.model_validate(
                _json.loads(path.read_text(encoding="utf-8"))
            )
            before = len(file_obj.upstreams)
            file_obj.upstreams = [u for u in file_obj.upstreams if u.name != name]
            if len(file_obj.upstreams) == before:
                raise HTTPException(status_code=404, detail=f"upstream `{name}` not found")
            path.write_text(
                _json.dumps(file_obj.model_dump(mode="json"), indent=2),
                encoding="utf-8",
            )
            return {"removed": name, "restart_required": True}

        @app.get("/upstreams", tags=["integrations"], dependencies=[Depends(check_auth)])
        def upstreams_list() -> dict[str, Any]:
            """Configured upstream MCP servers + their connection status."""
            from memex.upstreams import load_upstreams
            cfg = load_upstreams()
            out = []
            for u in cfg.upstreams:
                d = u.model_dump(mode="json")
                # Scrub secrets from response.
                auth = d.get("auth", {})
                if auth and "token" in auth:
                    auth["token"] = "<redacted>"
                out.append(d)
            return {"upstreams": out}

        @app.get("/upstreams/catalog", tags=["integrations"], dependencies=[Depends(check_auth)])
        def upstreams_catalog() -> dict[str, Any]:
            """Curated catalog of installable upstream MCP servers."""
            from memex.upstreams.catalog import load_catalog
            return {"entries": [
                e.model_dump(mode="json") for e in load_catalog()
            ]}

        @app.get("/hooks/status", tags=["integrations"], dependencies=[Depends(check_auth)])
        def hooks_status() -> dict[str, Any]:
            """Read ~/.claude/settings.json and report which Claude Code hook
            events memex is wired into. Same shape as the desktop's
            GetHooksStatus Wails method, but available over HTTP for any
            client."""
            import os
            home = os.path.expanduser("~")
            settings_path = os.path.join(home, ".claude", "settings.json")
            if not os.path.exists(settings_path):
                return {"installed": False, "reason": "settings.json not found",
                        "settings_path": settings_path}
            import json as _json
            try:
                data = _json.loads(open(settings_path, encoding="utf-8").read())
            except Exception as e:  # noqa: BLE001
                return {"installed": False, "reason": "parse error",
                        "error": str(e), "settings_path": settings_path}
            hooks_cfg = (data or {}).get("hooks", {}) or {}
            wired: dict[str, list[str]] = {}
            for ev, handlers in hooks_cfg.items():
                if not isinstance(handlers, list):
                    continue
                for h in handlers:
                    inner = (h or {}).get("hooks", []) or []
                    for c in inner:
                        cmd = (c or {}).get("command", "") or ""
                        if "memex" in cmd:
                            wired.setdefault(ev, []).append(cmd)
            return {
                "installed": bool(wired),
                "events_wired": wired,
                "settings_path": settings_path,
            }

        @app.post("/sources/link", tags=["write"], dependencies=[Depends(check_auth)])
        def link_sources_endpoint(body: dict[str, Any] = Body(default={})) -> dict[str, Any]:
            """Run the cross-repo linker — creates same_as edges between
            symbols that are likely the same logical concept across
            registered sources. Body (all optional):
              {"source_ids": [...], "threshold": 0.7, "dry_run": false}
            """
            from memex.codebase import link_cross_repo
            try:
                res = link_cross_repo(
                    engine,
                    source_ids=body.get("source_ids") or None,
                    threshold=float(body.get("threshold") or 0.7),
                    dry_run=bool(body.get("dry_run") or False),
                )
            except Exception as e:  # noqa: BLE001
                raise HTTPException(status_code=500, detail=str(e)) from None
            return {
                "pairs": [
                    {
                        "a_id": p.a_id, "b_id": p.b_id,
                        "a_name": p.a_name, "b_name": p.b_name,
                        "a_source": p.a_source, "b_source": p.b_source,
                        "similarity": p.similarity,
                    }
                    for p in res.pairs
                ],
                "pairs_created": len(res.pairs),
                "sources_considered": res.sources_considered,
                "candidates_examined": res.candidates_examined,
                "skipped_generic": res.skipped_generic,
                "skipped_short_name": res.skipped_short_name,
            }

        @app.get("/sources/{source_id}/stats", tags=["read"], dependencies=[Depends(check_auth)])
        def source_stats_endpoint(source_id: str) -> dict[str, Any]:
            """Per-source breakdown: file count, symbol counts by kind +
            language, cross-repo same_as count, embedding count. Powers
            the desktop Sources detail view."""
            from collections import Counter
            from memex.core.schema import EdgeKind, NodeKind
            files: list[Any] = []
            symbols: list[Any] = []
            for c in engine.find_by_kind(NodeKind.file):
                if c.metadata.get("source_id") == source_id:
                    files.append(c)
            for c in engine.find_by_kind(NodeKind.symbol):
                if c.metadata.get("source_id") == source_id:
                    symbols.append(c)
            files_by_lang = Counter(f.metadata.get("language", "?") for f in files)
            symbols_by_kind = Counter(s.metadata.get("symbol_kind", "?") for s in symbols)
            symbols_by_lang = Counter(s.metadata.get("language", "?") for s in symbols)
            # Cross-repo same_as — count outgoing same_as edges from
            # symbols in this source to symbols in other sources.
            sym_id_set = {s.id for s in symbols}
            cross_links: dict[str, int] = {}
            for s in symbols:
                for e in engine.edges_for(s.id):
                    if e.kind != EdgeKind.same_as:
                        continue
                    other = e.to_id if e.from_id == s.id else e.from_id
                    if other in sym_id_set:
                        continue
                    other_concept = engine.get(other)
                    if other_concept is None:
                        continue
                    other_src = other_concept.metadata.get("source_id")
                    if other_src and other_src != source_id:
                        cross_links[other_src] = cross_links.get(other_src, 0) + 1
            return {
                "source_id": source_id,
                "files_total": len(files),
                "symbols_total": len(symbols),
                "files_by_language": dict(files_by_lang),
                "symbols_by_kind": dict(symbols_by_kind),
                "symbols_by_language": dict(symbols_by_lang),
                "cross_repo_links": cross_links,
            }

        @app.post("/sources/{source_id}/reindex", tags=["write"], dependencies=[Depends(check_auth)])
        def reindex_source_endpoint(source_id: str) -> dict[str, Any]:
            from memex.codebase import reindex_source as _reindex_source
            try:
                res = _reindex_source(engine, source_id)
            except Exception as e:  # noqa: BLE001
                raise HTTPException(status_code=400, detail=str(e)) from None
            return {
                "source_id": source_id,
                "files": res.files_indexed,
                "symbols": res.symbols_indexed,
                "languages": res.languages,
                "skipped_files": res.skipped_files,
            }

        @app.delete("/sources/{source_id}", tags=["write"], dependencies=[Depends(check_auth)])
        def remove_source_endpoint(source_id: str) -> dict[str, Any]:
            from memex.codebase import remove_source as _remove_source
            try:
                deleted = _remove_source(engine, source_id)
            except Exception as e:  # noqa: BLE001
                raise HTTPException(status_code=400, detail=str(e)) from None
            return {"source_id": source_id, "deleted_concepts": deleted}

        @app.delete("/nodes/{concept_id}", tags=["write"], dependencies=[Depends(check_auth)])
        def delete_node(concept_id: str) -> dict[str, Any]:
            """Hard-delete a concept and every edge touching it.
            Powers the desktop's task / concept delete actions."""
            removed = engine.delete(concept_id)
            return {"deleted": removed, "id": concept_id}

        @app.patch("/nodes/{concept_id}", tags=["write"], dependencies=[Depends(check_auth)])
        def patch_node(concept_id: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
            """Edit a concept in place. Body fields (all optional):
              {name, description, kind, confidence, verification, metadata_patch}
            metadata_patch is a shallow merge — pass {key: null} to remove a key.
            History (concept_history) preserves the prior version."""
            c = engine.get(concept_id)
            if c is None:
                raise HTTPException(status_code=404, detail="not found")
            if "name" in body and body["name"]:
                c.name = body["name"].strip()
            if "description" in body:
                c.description = body["description"] or ""
            if "kind" in body and body["kind"]:
                c.kind = NodeKind(body["kind"])
            if "confidence" in body and body["confidence"] is not None:
                c.confidence = max(0.0, min(1.0, float(body["confidence"])))
            if "verification" in body:
                c.verification = body["verification"]
            if "metadata_patch" in body and isinstance(body["metadata_patch"], dict):
                md = dict(c.metadata or {})
                for k, v in body["metadata_patch"].items():
                    if v is None:
                        md.pop(k, None)
                    else:
                        md[k] = v
                c.metadata = md
            from datetime import datetime as _dt, timezone as _tz
            c.last_confirmed_at = _dt.now(_tz.utc)
            engine.put(c)
            return c.model_dump(mode="json")

        @app.post("/should-approve", tags=["enforcement"], dependencies=[Depends(check_auth)])
        def should_approve_endpoint(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
            """Layer-4 enforcement check. Body: {tool_name, tool_input}.
            Returns {decision, policy_id, reason}. Used by the PreToolUse
            hook so it can decide via the daemon's already-open engine
            (avoiding the DuckDB write-lock contest that was making the
            hook fail silently → user always got prompted even in AFK)."""
            from memex.enforcement import should_approve as _should_approve
            tool_name = body.get("tool_name")
            tool_input = body.get("tool_input") or {}
            if not tool_name:
                raise HTTPException(status_code=400, detail="tool_name required")
            try:
                m = _should_approve(
                    engine, tool_name=tool_name, tool_input=tool_input,
                )
            except Exception as e:  # noqa: BLE001
                return {"decision": "ask", "policy_id": "", "reason": f"error: {e}"}
            return m.to_dict()

        @app.get("/afk", tags=["enforcement"], dependencies=[Depends(check_auth)])
        def afk_get() -> dict[str, Any]:
            from memex.enforcement import afk_status
            st = afk_status(engine)
            return {"active": st is not None, "status": st}

        @app.post("/afk/on", tags=["enforcement"], dependencies=[Depends(check_auth)])
        def afk_on(body: dict[str, Any] = Body(default={})) -> dict[str, Any]:
            """Enable AFK mode. Body: {duration_hours: float = 4, note: str = ""}.
            Auto-approves every PreToolUse-gated call EXCEPT hard-deny patterns.
            Time-boxed; auto-expires after duration_hours."""
            from memex.enforcement import enable_afk_mode
            duration_hours = float(body.get("duration_hours") or 4.0)
            note = str(body.get("note") or "")
            flag = enable_afk_mode(engine, duration_hours=duration_hours, note=note)
            return {
                "id": flag.id,
                "expires_at": flag.metadata.get("expires_at"),
                "started_at": flag.metadata.get("started_at"),
                "duration_hours": flag.metadata.get("duration_hours"),
                "note": flag.metadata.get("note"),
            }

        @app.post("/afk/off", tags=["enforcement"], dependencies=[Depends(check_auth)])
        def afk_off() -> dict[str, Any]:
            from memex.enforcement import disable_afk_mode
            return {"disabled": disable_afk_mode(engine)}

        @app.get("/tasks/{task_id}/comments", tags=["tasks"], dependencies=[Depends(check_auth)])
        def task_comments_get(task_id: str) -> dict[str, Any]:
            """Comments / instructions attached to a task. Each comment is
            a kind=note concept with metadata.comment_on=task_id. Returned
            in chronological order so the AI reads them as a thread."""
            comments = []
            for c in engine.find_by_kind(NodeKind.note):
                if (c.metadata or {}).get("comment_on") == task_id:
                    comments.append(c.model_dump(mode="json"))
            comments.sort(key=lambda c: c.get("created_at", ""))
            return {"task_id": task_id, "comments": comments}

        @app.post("/tasks/{task_id}/comments", tags=["tasks"], dependencies=[Depends(check_auth)])
        def task_comments_post(task_id: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
            """Add a comment / instruction to a task. Body: {text, actor?='human'}.
            Persists as kind=note linked to the task via comment_on metadata
            AND a `relates_to` edge so cross-cutting graph walks find it."""
            text = body.get("text", "").strip()
            actor = body.get("actor", "human")
            if not text:
                raise HTTPException(status_code=400, detail="text required")
            target = engine.get(task_id)
            if target is None:
                raise HTTPException(status_code=404, detail="task not found")
            c = engine.add(
                name=f"comment on {target.name[:60]}",
                description=text,
                kind=NodeKind.note,
                source=Source(actor),
                metadata={"comment_on": task_id, "task_name": target.name},
            )
            engine.link(
                from_id=c.id, to_id=task_id,
                kind=EdgeKind.relates_to, source=Source(actor),
            )
            engine.observe(
                kind="comment_added", actor=Source(actor),
                payload={"comment_id": c.id, "task_id": task_id,
                         "text_len": len(text)},
            )
            return c.model_dump(mode="json")

        @app.get("/sources/{source_id}/files/{file_id}/symbols", tags=["read"], dependencies=[Depends(check_auth)])
        def file_symbols(source_id: str, file_id: str) -> dict[str, Any]:
            """Symbols defined in one file — for the Sources file-drilldown."""
            from memex.core.schema import NodeKind as _NK
            symbols = []
            for c in engine.find_by_kind(_NK.symbol):
                if c.metadata.get("file_id") == file_id:
                    symbols.append(c.model_dump(mode="json"))
            symbols.sort(key=lambda s: s.get("metadata", {}).get("start_line", 0))
            return {"file_id": file_id, "source_id": source_id, "symbols": symbols}

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

        @app.get("/stats", tags=["meta"], dependencies=[Depends(check_auth)])
        def stats(window_hours: int | None = None) -> dict[str, Any]:
            """Rich stats payload for the desktop Dashboard / Impact view.

            Aggregates concept counts, event counts, edge counts, and
            impact tiles (auto-approvals, redactions, corrections, etc.)
            from the episodic stream. Optional `window_hours` clips
            counts to the last N hours; omit for all-time totals.
            """
            return _compute_stats(engine, window_hours=window_hours)

        @app.get("/concepts", tags=["read"], dependencies=[Depends(check_auth)])
        def list_concepts(
            kind: NodeKind | None = None,
            limit: int = 200,
            offset: int = 0,
        ) -> dict[str, Any]:
            """List concepts (optionally filtered by kind). Backs the
            desktop Concepts browser."""
            if kind is not None:
                rows = engine.find_by_kind(kind)
            else:
                rows = engine.semantic.all_concepts()
            total = len(rows)
            window = rows[offset:offset + limit]
            return {
                "concepts": [c.model_dump(mode="json") for c in window],
                "total": total,
                "offset": offset,
                "limit": limit,
            }

        @app.get("/edges/{concept_id}", tags=["read"], dependencies=[Depends(check_auth)])
        def edges_for(concept_id: str) -> dict[str, Any]:
            """Every edge touching a concept — both directions. Used by
            the graph view to expand a node's neighborhood."""
            edges = engine.edges_for(concept_id)
            return {"edges": [e.model_dump(mode="json") for e in edges]}

        @app.get("/edges-bulk", tags=["read"], dependencies=[Depends(check_auth)])
        def edges_bulk(ids: str = "", limit: int = 5000) -> dict[str, Any]:
            """All edges where from_id OR to_id is in the given comma-
            separated id list. One round-trip instead of N — used by the
            desktop Graph view so 500-node renders aren't 500 HTTP calls."""
            wanted = {s.strip() for s in ids.split(",") if s.strip()}
            if not wanted:
                # No filter → all edges (capped). Cheap when caller wants
                # the global graph.
                conn = engine.semantic.conn
                lock = engine.semantic._lock
                with lock:
                    rows = conn.execute(
                        "SELECT from_id, to_id, kind, source, confidence, "
                        "created_at, last_confirmed_at, metadata "
                        "FROM edges LIMIT ?", [limit],
                    ).fetchall()
                from memex.core.stores.duckdb_store import _row_to_edge
                edges = [_row_to_edge(r) for r in rows]
            else:
                placeholders = ",".join(["?"] * len(wanted))
                wanted_list = list(wanted)
                conn = engine.semantic.conn
                lock = engine.semantic._lock
                with lock:
                    rows = conn.execute(
                        f"SELECT from_id, to_id, kind, source, confidence, "
                        f"created_at, last_confirmed_at, metadata "
                        f"FROM edges WHERE from_id IN ({placeholders}) "
                        f"   OR to_id IN ({placeholders}) "
                        f"LIMIT ?",
                        [*wanted_list, *wanted_list, limit],
                    ).fetchall()
                from memex.core.stores.duckdb_store import _row_to_edge
                edges = [_row_to_edge(r) for r in rows]
            return {"edges": [e.model_dump(mode="json") for e in edges]}

        @app.get("/nodes/{concept_id}/history", tags=["read"], dependencies=[Depends(check_auth)])
        def node_history(concept_id: str, limit: int = 20) -> dict[str, Any]:
            """Versioned history of a concept — every edit's prior snapshot
            kept in concept_history. Powers the desktop's "task history" view."""
            return {"history": engine.semantic.history(concept_id, limit=limit)}

        @app.get("/nodes/{concept_id}/neighborhood", tags=["read"], dependencies=[Depends(check_auth)])
        def node_neighborhood(concept_id: str) -> dict[str, Any]:
            """Full "associated with" surface for a concept — neighbors via
            every typed edge, every prior version (concept_history), and
            every episodic event whose payload references this concept's id.

            This is the "what all is associated with this task" view in the
            desktop: linked decisions/constraints/files/symbols, the task's
            edit history, and the events (auto-approvals, observations,
            corrections) that touched it.
            """
            edges = engine.edges_for(concept_id)
            neighbor_ids: set[str] = set()
            for e in edges:
                if e.from_id == concept_id:
                    neighbor_ids.add(e.to_id)
                else:
                    neighbor_ids.add(e.from_id)
            neighbors: list[dict[str, Any]] = []
            for nid in neighbor_ids:
                c = engine.get(nid)
                if c is not None:
                    neighbors.append(c.model_dump(mode="json"))

            # Episodic events whose payload mentions this id — concept_added
            # / observe(payload={id: ...}) / spawned_from references / etc.
            related_events: list[dict[str, Any]] = []
            for ev in engine.episodic.recent(limit=2000):
                payload = ev.payload or {}
                hit = False
                for v in payload.values():
                    if isinstance(v, str) and v == concept_id:
                        hit = True; break
                    if isinstance(v, list) and concept_id in v:
                        hit = True; break
                if hit:
                    related_events.append(ev.model_dump(mode="json"))
                if len(related_events) >= 100:
                    break

            history = engine.semantic.history(concept_id, limit=20)

            return {
                "neighbors": neighbors,
                "edges": [e.model_dump(mode="json") for e in edges],
                "events": related_events,
                "history": history,
            }

        @app.get("/events", tags=["read"], dependencies=[Depends(check_auth)])
        def list_events(
            kind: str | None = None, limit: int = 100,
        ) -> dict[str, Any]:
            """Episodic event feed. Backs the desktop Activity page."""
            evs = engine.episodic.recent(limit=limit, kind=kind)
            return {"events": [e.model_dump(mode="json") for e in evs]}

        @app.get("/schema", tags=["read"], dependencies=[Depends(check_auth)])
        def db_schema() -> dict[str, Any]:
            """List every DuckDB table with columns + row count. Backs the
            desktop Database tab so the user can see what memex actually
            persists."""
            return _db_schema(engine)

        @app.get("/schema/{table}/rows", tags=["read"], dependencies=[Depends(check_auth)])
        def db_table_rows(
            table: str, limit: int = 50, offset: int = 0,
        ) -> dict[str, Any]:
            """Read-only sample of rows from a DuckDB table."""
            return _db_table_rows(engine, table, limit=limit, offset=offset)

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
