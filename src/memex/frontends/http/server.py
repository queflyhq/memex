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

# Per-verb latency histogram (OMP §6). Buckets are MMP's p99 ceilings;
# anything beyond the last bucket lands in `over_ceiling`. The histogram
# is monotonically additive — readers compute deltas over time windows.
_LATENCY_BUCKETS_MS: list[float] = [5, 10, 20, 50, 100, 200, 500, 1000, 2000]


def _new_verb_hist() -> dict[str, Any]:
    return {
        "count": 0,
        "sum_ms": 0.0,
        "max_ms": 0.0,
        "buckets": [0] * len(_LATENCY_BUCKETS_MS),  # ≤ bucket boundary
        "over_ceiling": 0,
    }


# Map HTTP routes → OMP verb name. Endpoints not in this map don't
# emit verb-level latency (still observed by uvicorn's default metrics).
_ROUTE_TO_VERB: dict[tuple[str, str], str] = {
    ("GET", "/recall"): "recall",
    ("POST", "/nodes"): "remember",         # memex-native name for §4.2
    ("POST", "/remember"): "remember",
    ("POST", "/omp/remember"): "remember",
    ("POST", "/edges"): "link",
    ("POST", "/observe"): "observe",
    ("POST", "/check_action"): "validate",  # memex-native name for §4.5
    ("POST", "/validate_action"): "validate",
    ("POST", "/omp/validate"): "validate",
}

# Global per-verb stats. Reset when the daemon restarts.
_VERB_LATENCY: dict[str, dict[str, Any]] = {
    v: _new_verb_hist() for v in {"recall", "remember", "link", "observe", "validate"}
}


def _record_latency(verb: str, ms: float) -> None:
    h = _VERB_LATENCY.setdefault(verb, _new_verb_hist())
    h["count"] += 1
    h["sum_ms"] += ms
    if ms > h["max_ms"]:
        h["max_ms"] = ms
    # Find the smallest bucket ≥ ms; if ms exceeds the last bucket, over_ceiling++.
    placed = False
    for i, b in enumerate(_LATENCY_BUCKETS_MS):
        if ms <= b:
            h["buckets"][i] += 1
            placed = True
            break
    if not placed:
        h["over_ceiling"] += 1


def _latency_snapshot() -> dict[str, Any]:
    """Render the histogram for /stats consumption."""
    out: dict[str, Any] = {
        "buckets_ms": _LATENCY_BUCKETS_MS,
        "verbs": {},
    }
    for verb, h in _VERB_LATENCY.items():
        avg = (h["sum_ms"] / h["count"]) if h["count"] else 0.0
        out["verbs"][verb] = {
            "count": h["count"],
            "avg_ms": round(avg, 2),
            "max_ms": round(h["max_ms"], 2),
            "buckets": list(h["buckets"]),
            "over_ceiling": h["over_ceiling"],
        }
    return out


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

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Request
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
        # Pattern promotions (consolidation promoter, IP claim #2) —
        # episodic patterns that the system promoted into durable
        # constraint/decision nodes. This is the *learning* count.
        "pattern_promotions": events_by_kind.get("pattern_promoted", 0),
        # Validate-before-act hits (IP claim #3) — how often check_action
        # was invoked, regardless of decision. Subdivided into denies
        # below so the dashboard can show "of N checks, M denied".
        "actions_checked": events_by_kind.get("action_checked", 0),
        # Confidence calibrations — the closing edge of the loop.
        "calibrations": events_by_kind.get("confidence_calibrated", 0),
        "task_updates": events_by_kind.get("task_updated", 0),
        # Tokens auto-injected via SessionStart + UserPromptSubmit hooks —
        # context the user didn't have to type and the AI didn't have to
        # re-derive via tool calls. Counted from context_injected events
        # whose payload.est_tokens is the char-count / 4 estimate.
        "context_injections": events_by_kind.get("context_injected", 0),
    }
    # Validate-before-act denies — useful as a sub-metric of actions_checked.
    deny_sql = (
        "SELECT count(*) FROM events WHERE kind = 'action_checked' "
        "AND json_extract(payload, '$.decision') = '\"deny\"'"
    )
    deny_params: list[Any] = []
    if window_hours is not None and window_hours > 0:
        deny_sql += " AND timestamp >= ?"
        deny_params.append(datetime.now(timezone.utc) - timedelta(hours=window_hours))
    with epi_lock:
        try:
            n_denies = int(
                epi_conn.execute(deny_sql, deny_params).fetchone()[0] or 0
            )
        except Exception:  # noqa: BLE001
            # Older DuckDB / payloads without 'decision' — count as 0.
            n_denies = 0
    impact_tiles["actions_denied"] = n_denies
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

        # OMP §9 — canonical error envelope. FastAPI's default is
        # {"detail": "..."} which is not conformant. Wrap every
        # HTTPException + uncaught exception in the OMP envelope:
        #   {error, message, retry_after, details}
        # We map HTTP status codes to OMP error codes per §9.
        from fastapi.exceptions import RequestValidationError
        from fastapi.responses import JSONResponse
        from starlette.exceptions import HTTPException as StarletteHTTPException

        _STATUS_TO_OMP = {
            400: "invalid_argument",
            401: "unauthenticated",
            403: "permission_denied",
            404: "not_found",
            429: "quota_exceeded",
            500: "internal",
            503: "unavailable",
        }

        def _mmp_envelope(status: int, message: str, details: Any = None,
                          retry_after: int | None = None) -> JSONResponse:
            return JSONResponse(
                status_code=status,
                content={
                    "error": _STATUS_TO_OMP.get(status, "internal"),
                    "message": message,
                    "retry_after": retry_after,
                    "details": details,
                },
            )

        # OMP §6 — per-verb latency middleware. Records wall time for each
        # request whose (method, path) maps to an OMP verb. p99 view exposed
        # in /stats.
        @app.middleware("http")
        async def _mmp_latency_mw(request, call_next):  # type: ignore[no-untyped-def]
            verb = _ROUTE_TO_VERB.get((request.method, request.url.path))
            if verb is None:
                return await call_next(request)
            t0 = time.perf_counter()
            try:
                resp = await call_next(request)
                return resp
            finally:
                _record_latency(verb, (time.perf_counter() - t0) * 1000.0)

        @app.exception_handler(StarletteHTTPException)
        async def _http_exc(_req, exc: StarletteHTTPException) -> JSONResponse:
            return _mmp_envelope(
                status=exc.status_code,
                message=str(exc.detail) if not isinstance(exc.detail, dict) else "",
                details=exc.detail if isinstance(exc.detail, dict) else None,
            )

        @app.exception_handler(RequestValidationError)
        async def _validation_exc(_req, exc: RequestValidationError) -> JSONResponse:
            return _mmp_envelope(
                status=400,
                message="request validation failed",
                details={"errors": exc.errors()},
            )

        # ---- Web UI mount -------------------------------------------------
        # Serves the built Svelte UI at /app/* so users can `memex ui` and
        # interact with the daemon in their default browser. We drop Wails
        # in favour of this — same UI code, no per-platform binaries to
        # build, ship, sign, or notarize.
        #
        # Index.html gets the daemon auth token injected as a meta tag
        # before being served so the JS shim (webui/wailsjs/go/main/App.js)
        # can authenticate every subsequent /recall, /remember, etc. call
        # against the same daemon.
        from fastapi import Response
        from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
        from fastapi.staticfiles import StaticFiles
        from pathlib import Path as _Path

        # Root URL convenience: `localhost:7777` -> UI. Daemon is local-only
        # so there's nothing else useful to serve at /.
        @app.get("/", include_in_schema=False)
        def _root_redirect() -> RedirectResponse:
            return RedirectResponse(url="/app/", status_code=307)

        # ---- Jinja2 + htmx UI ---------------------------------------------
        # Server-rendered pages. Daemon and UI are the same process: pages
        # call engine methods directly (no HTTP round-trip), so the typical
        # page renders in <50ms even on cold cache. htmx is vendored for
        # partial swaps. No JS bundle, no npm build.
        from jinja2 import Environment, FileSystemLoader, select_autoescape

        _TPL_DIR = _Path(__file__).resolve().parent / "templates"
        _STATIC_DIR = _Path(__file__).resolve().parent / "static"
        _jinja = Environment(
            loader=FileSystemLoader(str(_TPL_DIR)),
            autoescape=select_autoescape(["html"]),
            trim_blocks=True,
            lstrip_blocks=True,
        )

        def _rel_time(ts: Any) -> str:
            """Humanize an absolute timestamp → "5m ago"/"2h ago"/"3d ago"."""
            try:
                from datetime import datetime as _dt2
                if ts is None:
                    return ""
                if isinstance(ts, (int, float)):
                    diff = time.time() - float(ts)
                else:
                    parsed = _dt2.fromisoformat(str(ts).replace("Z", "+00:00"))
                    diff = time.time() - parsed.timestamp()
                if diff < 60:
                    return "just now"
                if diff < 3600:
                    return f"{int(diff // 60)}m ago"
                if diff < 86400:
                    return f"{int(diff // 3600)}h ago"
                return f"{int(diff // 86400)}d ago"
            except Exception:  # noqa: BLE001
                return ""

        def _humanize_event(kind: str) -> str:
            return {
                "concept_created": "Remembered",
                "concept_revised": "Updated",
                "concept_updated": "Updated",
                "edge_added": "Linked",
                "user_correction": "Learned from correction",
                "pattern_promoted": "Found pattern",
                "task_completed": "Finished task",
                "gate_denied": "Blocked risky action",
                "recall_executed": "Answered query",
                "afk_enabled": "Paused (AFK)",
                "consolidation_added": "Consolidated",
                "rule_added": "Added rule",
            }.get(kind, kind.replace("_", " "))

        def _render(name: str, **ctx: Any) -> HTMLResponse:
            """Render a Jinja template with the standard layout context.

            Globals that every page needs (AFK banner on top, daemon
            health pill on sidebar) get injected here so individual
            handlers don't repeat themselves.
            """
            tpl = _jinja.get_template(name)
            ctx.setdefault("daemon_alive", True)
            # Inject AFK state for the persistent banner in base.html.
            if "global_afk_on" not in ctx:
                try:
                    from memex.enforcement.policies import _afk_state
                    afk_c = _afk_state(engine)
                    ctx["global_afk_on"] = afk_c is not None
                    ctx["global_afk_until"] = (
                        (afk_c.metadata or {}).get("expires_at") if afk_c else None
                    )
                except Exception:  # noqa: BLE001
                    ctx["global_afk_on"] = False
                    ctx["global_afk_until"] = None
            return HTMLResponse(content=tpl.render(**ctx))

        # Static assets (CSS, vendored htmx, brand SVG).
        app.mount(
            "/app/static",
            StaticFiles(directory=str(_STATIC_DIR), html=False),
            name="app-static",
        )

        @app.get("/app", include_in_schema=False)
        @app.get("/app/", include_in_schema=False)
        def _app_root() -> RedirectResponse:
            return RedirectResponse(url="/app/home", status_code=307)

        def _today_summary() -> dict[str, int]:
            """Counts of meaningful events since midnight (local TZ-approx UTC)."""
            try:
                with engine.semantic._lock:  # type: ignore[attr-defined]
                    rows = engine.semantic.conn.execute(  # type: ignore[attr-defined]
                        "SELECT kind, count(*) FROM events "
                        "WHERE timestamp > (CURRENT_TIMESTAMP - INTERVAL 24 HOUR) "
                        "GROUP BY kind",
                    ).fetchall()
                by_kind = {k: int(c) for k, c in rows}
            except Exception:  # noqa: BLE001
                by_kind = {}
            return {
                "concepts_created": by_kind.get("concept_created", 0) + by_kind.get("concept_revised", 0),
                "tasks_completed":  by_kind.get("task_completed", 0),
                "recalls":          by_kind.get("recall_executed", 0),
                "denials":          by_kind.get("gate_denied", 0),
                "corrections":      by_kind.get("user_correction", 0),
            }

        def _top_projects(limit: int = 8) -> list[dict[str, Any]]:
            """Per-project mini-dashboard rows: task / decision / source counts."""
            from memex.core.schema import NodeKind as _NK
            project_concepts = engine.semantic.find_by_kind(_NK.project) or []
            if not project_concepts:
                return []
            # Pre-aggregate task counts by metadata.project name.
            task_count_by_proj: dict[str, int] = {}
            try:
                with engine.semantic._lock:  # type: ignore[attr-defined]
                    rows = engine.semantic.conn.execute(  # type: ignore[attr-defined]
                        "SELECT json_extract_string(metadata, '$.project'), count(*) "
                        "FROM concepts WHERE kind = 'task' "
                        "AND json_extract_string(metadata, '$.status') IN ('pending','in_progress') "
                        "GROUP BY json_extract_string(metadata, '$.project')",
                    ).fetchall()
                for proj, cnt in rows:
                    if proj:
                        task_count_by_proj[proj] = int(cnt)
            except Exception:  # noqa: BLE001
                pass
            # Decision counts via `part_of` edges to project.
            decision_by_proj: dict[str, int] = {}
            source_by_proj: dict[str, int] = {}
            try:
                with engine.semantic._lock:  # type: ignore[attr-defined]
                    rows = engine.semantic.conn.execute(  # type: ignore[attr-defined]
                        "SELECT e.to_id, c.kind, count(*) "
                        "FROM edges e JOIN concepts c ON c.id = e.from_id "
                        "WHERE e.kind = 'part_of' AND c.kind IN ('decision','source') "
                        "GROUP BY e.to_id, c.kind",
                    ).fetchall()
                for to_id, kind, cnt in rows:
                    if kind == "decision":
                        decision_by_proj[to_id] = int(cnt)
                    elif kind == "source":
                        source_by_proj[to_id] = int(cnt)
            except Exception:  # noqa: BLE001
                pass
            out = []
            for p in project_concepts:
                md = p.metadata or {}
                task_n = task_count_by_proj.get(p.name, 0)
                decision_n = decision_by_proj.get(p.id, 0)
                source_n = source_by_proj.get(p.id, 0)
                if task_n + decision_n + source_n == 0:
                    continue
                out.append({
                    "id": p.id,
                    "name": p.name,
                    "path": md.get("path", ""),
                    "task_count": task_n,
                    "decision_count": decision_n,
                    "source_count": source_n,
                    "last_activity_rel": _rel_time(getattr(p, "last_confirmed_at", None)),
                })
            out.sort(key=lambda x: -(x["task_count"] + x["decision_count"]))
            return out[:limit]

        def _service_state() -> dict[str, Any]:
            try:
                from memex.service import service_status
                s = service_status()
            except Exception:  # noqa: BLE001
                s = {"installed": False, "running": False}
            import sys as _sys
            return {
                "running": bool(s.get("running")),
                "platform": {"darwin": "macOS", "win32": "Windows", "linux": "Linux"}.get(_sys.platform, _sys.platform),
            }

        def _team_state() -> dict[str, Any]:
            import os as _os
            repo = _os.environ.get("MEMEX_TEAM_REPO")
            if not repo:
                return {"configured": False, "last_sync": None}
            try:
                manifest_p = _Path(repo) / "memex-graph" / "manifest.json"
                if manifest_p.is_file():
                    import json as _json
                    data = _json.loads(manifest_p.read_text(encoding="utf-8"))
                    return {
                        "configured": True,
                        "last_sync": _rel_time(data.get("last_push_at")),
                    }
            except Exception:  # noqa: BLE001
                pass
            return {"configured": True, "last_sync": None}

        def _scheduler_state() -> dict[str, Any]:
            """Most recent scheduler tick + job count. Read from the
            HTTPFrontend's scheduler instance via the closure."""
            import os as _os2
            try:
                sched = getattr(self, "_scheduler", None)
                if sched is None:
                    return {"job_count": 0, "last_run": None}
                last_run = sched.last_run or {}
                if not last_run:
                    return {"job_count": 7, "last_run": None}
                most_recent = max(last_run.values())
                return {
                    "job_count": 7 + (1 if _os2.environ.get("MEMEX_TEAM_REPO") else 0),
                    "last_run": _rel_time(most_recent),
                }
            except Exception:  # noqa: BLE001
                return {"job_count": 7, "last_run": None}

        @app.get("/app/home", include_in_schema=False)
        def _ui_home() -> HTMLResponse:
            import os as _os
            stats = engine.stats()
            # Doctor: lift the 8 checks via the same handler logic. Inline
            # to skip the HTTP round-trip + JSON serialization.
            doctor_data = doctor_endpoint()  # type: ignore[name-defined]
            summary = doctor_data["summary"]["overall"]
            warn_count = doctor_data["summary"]["warn"] + doctor_data["summary"]["fail"]
            # Learning timeline — recent episodic events that represent
            # "memex learned something." Filter OUT tool_pre/tool_post and
            # other workflow noise; user wants to see what got stored, not
            # what the AI did this minute.
            _LEARN_KINDS = {
                "concept_created", "concept_revised",
                "edge_added", "pattern_promoted",
                "user_correction", "afk_enabled",
                "consolidation_added", "rule_added",
            }
            raw_events = list(engine.episodic.recent(limit=200) or [])
            events = [
                e for e in raw_events
                if getattr(e, "kind", "") in _LEARN_KINDS
            ][:8]
            timeline = []
            for e in events:
                payload = getattr(e, "payload", {}) or {}
                # Surface concept name when available — much more useful
                # than the bare event kind.
                name = payload.get("name") or payload.get("concept_name")
                if not name:
                    nested = payload.get("concept")
                    if isinstance(nested, dict):
                        name = nested.get("name")
                timeline.append({
                    "rel_time": _rel_time(getattr(e, "timestamp", None)),
                    "label": _humanize_event(getattr(e, "kind", "")),
                    "name": name,
                })
            # Next actions — top unblocked tasks.
            nxt = engine.next_actions(limit=5)
            next_actions = [
                {
                    "id": t.id,
                    "name": t.name,
                    "priority": (t.metadata or {}).get("priority"),
                    "due_rel": _rel_time((t.metadata or {}).get("due_at")),
                }
                for t in nxt
            ]
            # Recent denials — gate-blocked tool calls in the last week.
            denial_events = [
                e for e in engine.episodic.recent(limit=50) or []
                if getattr(e, "kind", "") == "gate_denied"
            ][:5]
            recent_denials = [
                {
                    "rel_time": _rel_time(getattr(e, "timestamp", None)),
                    "intent": (getattr(e, "payload", {}) or {}).get("intent") or "blocked action",
                }
                for e in denial_events
            ]
            # AFK status
            try:
                from memex.enforcement.policies import _afk_state
                afk_concept = _afk_state(engine)
                afk_on = afk_concept is not None
                afk_until = (afk_concept.metadata or {}).get("expires_at") if afk_concept else None
            except Exception:  # noqa: BLE001
                afk_on, afk_until = False, None

            # Dashboard greeting
            from datetime import datetime as _dt3
            hour = _dt3.now().hour
            greeting = (
                "Good morning." if hour < 12
                else "Good afternoon." if hour < 18
                else "Good evening."
            ) + " Here's where memex stands."

            # Embed degraded check — if engine fell back to BM25, surface a banner.
            embed_degraded = False
            try:
                embed_degraded = not engine.embedding_provider.is_available()
            except Exception:  # noqa: BLE001
                pass
            return _render(
                "home.html",
                active="home",
                stats=stats,
                greeting=greeting,
                doctor_summary=summary,
                doctor_warn_count=warn_count,
                timeline=timeline,
                next_actions=next_actions,
                recent_denials=recent_denials,
                token_savings=_token_savings_7d(),
                today=_today_summary(),
                projects=_top_projects(8),
                stale_count=_stale_count(),
                afk_on=afk_on,
                afk_until=afk_until,
                embed_degraded=embed_degraded,
                service_running=_service_state()["running"],
                service_platform=_service_state()["platform"],
                team_sync_configured=_team_state()["configured"],
                team_sync_last=_team_state()["last_sync"],
                scheduler_job_count=_scheduler_state()["job_count"],
                scheduler_last_run=_scheduler_state()["last_run"],
            )

        def _token_savings_7d() -> dict[str, Any]:
            """Estimate how many input tokens memex saved this week vs. the
            naive baseline of re-explaining everything each session.

            Heuristic — not metering tokens at the model boundary. We count:
              - `context_injected` events (each one delivered the recall
                bundle as context — average bundle is ~800 tokens of
                concept names + descriptions injected into the prompt)
              - `recall_executed` events (a successful recall that
                preceded a tool call — we credit the budget_tokens it
                consumed as "saved" since the AI didn't have to ask)
              - `auto_approval` events (gate approved without re-asking
                the user — saves ~150 tokens of clarifying back-and-forth)

            Multiply by a conservative $/M token rate so the number on
            Home reads as money saved, not just tokens. Anthropic Sonnet
            input is $3/M. Result is rounded to a single sig fig so the
            tile doesn't look fake-precise.
            """
            try:
                with engine.semantic._lock:  # type: ignore[attr-defined]
                    rows = engine.semantic.conn.execute(  # type: ignore[attr-defined]
                        "SELECT kind, count(*) FROM events "
                        "WHERE timestamp > (CURRENT_TIMESTAMP - INTERVAL 7 DAY) "
                        "GROUP BY kind",
                    ).fetchall()
                by_kind = {k: int(c) for k, c in rows}
            except Exception:  # noqa: BLE001
                by_kind = {}
            ctx_injected = by_kind.get("context_injected", 0)
            recalls = by_kind.get("recall_executed", 0)
            approvals = by_kind.get("auto_approval", 0)
            # Conservative bytes-per-event (these are input-token estimates).
            saved_tokens = (
                ctx_injected * 800
                + recalls * 400
                + approvals * 150
            )
            # $3 per million input tokens (Claude Sonnet baseline).
            saved_usd = saved_tokens * 3.0 / 1_000_000
            return {
                "saved_tokens": saved_tokens,
                "saved_tokens_human": f"{saved_tokens/1000:.1f}k" if saved_tokens >= 1000 else str(saved_tokens),
                "saved_usd": round(saved_usd, 2),
                "ctx_injected": ctx_injected,
                "recalls": recalls,
                "approvals": approvals,
            }

        def _stale_count() -> int:
            """How many concepts haven't been confirmed in 90+ days.

            Surfaces in the Memory header as a 'review N stale' ribbon.
            Cheap query — one COUNT() against an indexed column.
            """
            try:
                with engine.semantic._lock:  # type: ignore[attr-defined]
                    row = engine.semantic.conn.execute(  # type: ignore[attr-defined]
                        "SELECT count(*) FROM concepts "
                        "WHERE last_confirmed_at < (CURRENT_TIMESTAMP - INTERVAL 90 DAY) "
                        "AND kind NOT IN ('symbol','file','source','module','project','action_constraint')",
                    ).fetchone()
                return int(row[0]) if row else 0
            except Exception:  # noqa: BLE001
                return 0

        def _gather_search_results(q: str, kind: str, rerank: bool = False) -> tuple[list[dict[str, Any]], str, float]:
            """Run the actual recall / browse query and return result rows.

            Split out from the page handler so /app/search/results (htmx
            fragment) can call it without re-rendering the shell. Returns
            (results, error_message, elapsed_ms).

            `rerank` opts into the cross-encoder for higher-precision results
            at the cost of 5-8s latency. Default is fast hybrid (no rerank).
            """
            t0 = time.time()
            results: list[dict[str, Any]] = []
            error = ""
            try:
                if q:
                    r = engine.recall(query=q, budget_tokens=2000, rerank=rerank)
                    items = list(getattr(r, "nodes", []) or [])
                    for c in items[:25]:
                        results.append({
                            "id": c.id, "name": c.name,
                            "kind": c.kind.value if hasattr(c.kind, "value") else str(c.kind),
                            "description": c.description or "",
                            "rel_time": _rel_time(getattr(c, "created_at", None)),
                        })
                else:
                    if kind:
                        from memex.core.schema import NodeKind as _NK
                        try:
                            concepts = engine.semantic.find_by_kind(_NK(kind))
                        except Exception:  # noqa: BLE001
                            concepts = []
                    else:
                        with engine.semantic._lock:  # type: ignore[attr-defined]
                            rows = engine.semantic.conn.execute(  # type: ignore[attr-defined]
                                "SELECT * FROM concepts ORDER BY last_confirmed_at DESC NULLS LAST LIMIT 50",
                            ).fetchall()
                        from memex.core.stores.duckdb_store import _row_to_concept
                        concepts = [_row_to_concept(r) for r in rows]
                    concepts = sorted(
                        concepts,
                        key=lambda c: getattr(c, "created_at", 0) or 0,
                        reverse=True,
                    )[:50]
                    for c in concepts:
                        results.append({
                            "id": c.id, "name": c.name,
                            "kind": c.kind.value if hasattr(c.kind, "value") else str(c.kind),
                            "description": c.description or "",
                            "rel_time": _rel_time(getattr(c, "created_at", None)),
                        })
            except Exception as exc:  # noqa: BLE001
                error = str(exc)
            elapsed_ms = round((time.time() - t0) * 1000.0, 1)
            return results, error, elapsed_ms

        @app.get("/app/search/results", include_in_schema=False)
        def _ui_search_results(q: str = "", kind: str = "", stale: int = 0, rerank: int = 0) -> HTMLResponse:
            """htmx fragment: just the results list. Mounted by /app/search
            via hx-get on load so the page shell renders in <30ms while the
            actual recall (1-3s) streams in afterward.
            """
            if stale:
                # Stale path: surface concepts with last_confirmed_at older
                # than 90 days, oldest first, excluding code/structural kinds.
                results: list[dict[str, Any]] = []
                error = ""
                t0 = time.time()
                try:
                    with engine.semantic._lock:  # type: ignore[attr-defined]
                        rows = engine.semantic.conn.execute(  # type: ignore[attr-defined]
                            "SELECT * FROM concepts "
                            "WHERE last_confirmed_at < (CURRENT_TIMESTAMP - INTERVAL 90 DAY) "
                            "AND kind NOT IN ('symbol','file','source','module','project','action_constraint') "
                            "ORDER BY last_confirmed_at ASC NULLS FIRST LIMIT 50",
                        ).fetchall()
                    from memex.core.stores.duckdb_store import _row_to_concept
                    for r in rows:
                        c = _row_to_concept(r)
                        results.append({
                            "id": c.id, "name": c.name,
                            "kind": c.kind.value if hasattr(c.kind, "value") else str(c.kind),
                            "description": c.description or "",
                            "rel_time": _rel_time(getattr(c, "last_confirmed_at", None)),
                        })
                except Exception as exc:  # noqa: BLE001
                    error = str(exc)
                elapsed_ms = round((time.time() - t0) * 1000.0, 1)
                tpl = _jinja.get_template("search_results.html")
                return HTMLResponse(content=tpl.render(
                    results=results, error=error, query="", kind="",
                    selected=None, elapsed_ms=elapsed_ms, stale=True,
                ))
            results, error, elapsed_ms = _gather_search_results(q, kind, rerank=bool(rerank))
            tpl = _jinja.get_template("search_results.html")
            return HTMLResponse(content=tpl.render(
                results=results, error=error, query=q, kind=kind,
                selected=None, elapsed_ms=elapsed_ms, stale=False,
                rerank=bool(rerank),
            ))

        @app.get("/app/search", include_in_schema=False)
        def _ui_search(q: str = "", kind: str = "", action: str = "", stale: int = 0, precision: int = 0) -> HTMLResponse:
            """Page shell — renders instantly (<30ms). When q or kind is
            set, the template emits an hx-get that loads search_results.html
            from /app/search/results, so the real recall (1-3s) doesn't
            block the page paint.
            """
            return _render(
                "search.html",
                active="search",
                query=q,
                kind=kind,
                action=action,
                stale_filter=bool(stale),
                stale_count=_stale_count(),
                precision=bool(precision),
                selected=None,
                kinds=["decision", "constraint", "action_constraint", "fact", "task", "project", "person", "pattern", "source"],
                results=[],
                error="",
            )

        @app.get("/app/search/{cid}", include_in_schema=False)
        def _ui_search_detail(cid: str, q: str = "", kind: str = "") -> HTMLResponse:
            try:
                c = engine.semantic.get_concept(cid)
            except Exception:  # noqa: BLE001
                c = None
            if c is None:
                return RedirectResponse(url="/app/search", status_code=303)
            # Summarize neighborhood by edge kind so the user sees
            # "3 tasks reference this" not raw edge rows.
            try:
                _, edges = engine.semantic.neighbors(cid, depth=1)
            except Exception:  # noqa: BLE001
                edges = []
            by_kind: dict[str, int] = {}
            for e in edges:
                ek = e.kind.value if hasattr(e.kind, "value") else str(e.kind)
                by_kind[ek] = by_kind.get(ek, 0) + 1
            # Concept history — every prior version with timestamp.
            history = []
            try:
                with engine.semantic._lock:  # type: ignore[attr-defined]
                    hrows = engine.semantic.conn.execute(  # type: ignore[attr-defined]
                        "SELECT version, snapshot, changed_at FROM concept_history "
                        "WHERE id = ? ORDER BY version DESC LIMIT 20",
                        [cid],
                    ).fetchall()
                import json as _json5
                for version, snap_raw, changed_at in hrows:
                    snap = _json5.loads(snap_raw) if isinstance(snap_raw, str) else snap_raw
                    history.append({
                        "version": version,
                        "name": (snap or {}).get("name", "?"),
                        "description_len": len((snap or {}).get("description") or ""),
                        "changed_rel": _rel_time(changed_at),
                    })
            except Exception:  # noqa: BLE001
                pass
            # Episodic events touching this concept (recent)
            activity = []
            try:
                with engine.semantic._lock:  # type: ignore[attr-defined]
                    erows = engine.semantic.conn.execute(  # type: ignore[attr-defined]
                        "SELECT id, timestamp, kind, payload FROM events "
                        "WHERE json_extract_string(payload, '$.id') = ? "
                        "OR json_extract_string(payload, '$.concept_id') = ? "
                        "ORDER BY timestamp DESC LIMIT 15",
                        [cid, cid],
                    ).fetchall()
                import json as _json5b
                for _, ts, ek, pl in erows:
                    activity.append({
                        "kind": ek,
                        "rel_time": _rel_time(ts),
                    })
            except Exception:  # noqa: BLE001
                pass
            # Code symbols whose description / body mentions this concept's name.
            # Cheap heuristic — substring match against symbol bodies. Limits
            # to 10 hits so a common word doesn't blow up the page.
            code_mentions = []
            try:
                with engine.semantic._lock:  # type: ignore[attr-defined]
                    sym_rows = engine.semantic.conn.execute(  # type: ignore[attr-defined]
                        "SELECT id, name, metadata FROM concepts "
                        "WHERE kind = 'symbol' "
                        "AND (description LIKE ? OR json_extract_string(metadata, '$.body') LIKE ?) "
                        "LIMIT 10",
                        [f"%{c.name}%", f"%{c.name}%"],
                    ).fetchall()
                import json as _json5c
                for sid, sname, smeta in sym_rows:
                    if sid == cid:
                        continue
                    md = _json5c.loads(smeta) if isinstance(smeta, str) else (smeta or {})
                    code_mentions.append({
                        "id": sid, "name": sname,
                        "symbol_kind": md.get("symbol_kind"),
                        "file_path": md.get("file_path") or "",
                    })
            except Exception:  # noqa: BLE001
                pass
            # Grouped connections — for each edge kind, list a few destinations
            # so user can navigate (not just see counts).
            grouped_connections: dict[str, list[dict[str, str]]] = {}
            for e in edges[:60]:
                ek = e.kind.value if hasattr(e.kind, "value") else str(e.kind)
                other_id = e.to_id if e.from_id == cid else e.from_id
                if other_id == cid:
                    continue
                bucket = grouped_connections.setdefault(ek, [])
                if len(bucket) >= 6:
                    continue
                try:
                    other = engine.get(other_id)
                    if other is not None:
                        bucket.append({"id": other.id, "name": other.name,
                                       "kind": other.kind.value if hasattr(other.kind, "value") else str(other.kind)})
                except Exception:  # noqa: BLE001
                    pass
            selected = {
                "id": c.id, "name": c.name,
                "kind": c.kind.value if hasattr(c.kind, "value") else str(c.kind),
                "description": c.description or "",
                "connections": sorted(by_kind.items(), key=lambda kv: -kv[1]),
                "grouped_connections": grouped_connections,
                "created_rel": _rel_time(getattr(c, "created_at", None)),
                "last_confirmed_rel": _rel_time(getattr(c, "last_confirmed_at", None)),
                "confidence": getattr(c, "confidence", None),
                "source": (c.source.value if hasattr(c.source, "value") else str(c.source)) if getattr(c, "source", None) else None,
                "history": history,
                "activity": activity,
                "code_mentions": code_mentions,
            }
            # Re-render the list alongside so user keeps context.
            sub = _ui_search(q=q, kind=kind)
            # _ui_search returns HTMLResponse but we need to inject selected.
            # Simpler: re-fetch the list ourselves.
            results: list[dict[str, Any]] = []
            if q:
                try:
                    r = engine.recall(query=q, budget_tokens=2000)
                    items = getattr(r, "items", None) or getattr(r, "concepts", []) or []
                    for cc in items[:25]:
                        results.append({
                            "id": cc.id, "name": cc.name,
                            "kind": cc.kind.value if hasattr(cc.kind, "value") else str(cc.kind),
                            "description": cc.description or "",
                            "rel_time": _rel_time(getattr(cc, "created_at", None)),
                        })
                except Exception:  # noqa: BLE001
                    pass
            else:
                with engine.semantic._lock:  # type: ignore[attr-defined]
                    rows = engine.semantic.conn.execute(  # type: ignore[attr-defined]
                        "SELECT * FROM concepts ORDER BY last_confirmed_at DESC NULLS LAST LIMIT 50",
                    ).fetchall()
                from memex.core.stores.duckdb_store import _row_to_concept
                for cc in [_row_to_concept(r) for r in rows]:
                    results.append({
                        "id": cc.id, "name": cc.name,
                        "kind": cc.kind.value if hasattr(cc.kind, "value") else str(cc.kind),
                        "description": cc.description or "",
                        "rel_time": _rel_time(getattr(cc, "created_at", None)),
                    })
            return _render(
                "search.html",
                active="search",
                query=q, kind=kind, action="",
                kinds=["decision", "constraint", "action_constraint", "fact", "task", "project", "person", "pattern", "source"],
                results=results,
                selected=selected,
                error="",
            )

        @app.post("/app/search/create", include_in_schema=False)
        async def _ui_search_create(request: Request) -> RedirectResponse:
            """Remember a new concept. Supports an `ephemeral_hours` field
            that, when set, marks the concept `kind=ephemeral` with
            `metadata.expires_at` so cleanup will reap it after the TTL.
            """
            form = await request.form()
            from memex.core.schema import Concept, NodeKind as _NK, Source
            from datetime import datetime as _dt5, timedelta as _td5, timezone as _tz5
            kind_str = str(form.get("kind") or "fact")
            try:
                k = _NK(kind_str)
            except Exception:  # noqa: BLE001
                k = _NK.fact
            name = str(form.get("name") or "").strip()
            description = str(form.get("description") or "").strip() or None
            if not name:
                return RedirectResponse(url="/app/search", status_code=303)
            metadata: dict[str, Any] = {}
            # Optional TTL — if set, override kind to ephemeral.
            eph_hours_raw = str(form.get("ephemeral_hours") or "").strip()
            if eph_hours_raw:
                try:
                    hours = max(1, min(168, int(eph_hours_raw)))
                    expires = _dt5.now(_tz5.utc) + _td5(hours=hours)
                    metadata["expires_at"] = expires.isoformat()
                    metadata["ephemeral_kind"] = kind_str  # remember the original
                    k = _NK.ephemeral
                except Exception:  # noqa: BLE001
                    pass
            engine.semantic.add_concept(Concept(
                name=name, kind=k, description=description,
                source=Source.human,
                metadata=metadata,
            ))
            return RedirectResponse(url="/app/search", status_code=303)

        @app.post("/app/search/{cid}/delete", include_in_schema=False)
        def _ui_search_delete(cid: str) -> RedirectResponse:
            try:
                engine.semantic.delete_concept(cid)
            except Exception:  # noqa: BLE001
                pass
            return RedirectResponse(url="/app/search", status_code=303)

        @app.get("/app/c/{cid}/edit-description", include_in_schema=False)
        def _ui_edit_description_form(cid: str) -> HTMLResponse:
            """htmx GET fragment: the inline edit form, swapped in over the description."""
            c = engine.get(cid)
            if c is None:
                return HTMLResponse(content="<div class='error'>not found</div>")
            return HTMLResponse(content=(
                f"<form hx-post='/app/c/{cid}/description' "
                f"hx-target='#desc-{cid}' hx-swap='outerHTML' "
                f"style='display:flex; flex-direction:column; gap:6px;'>"
                f"<textarea name='description' rows='4' "
                f"style='padding:8px 12px; border:1px solid var(--border-2); border-radius:var(--radius); font-family:inherit; font-size:13px;' autofocus>"
                f"{(c.description or '').replace('<','&lt;').replace('>','&gt;')}"
                f"</textarea>"
                f"<div style='display:flex; gap:6px;'>"
                f"<button class='btn-primary' type='submit'>Save</button>"
                f"<a class='btn-secondary' hx-get='/app/c/{cid}/description' hx-target='#desc-{cid}' hx-swap='outerHTML'>Cancel</a>"
                f"</div></form>"
            ))

        @app.get("/app/c/{cid}/description", include_in_schema=False)
        def _ui_description_view(cid: str) -> HTMLResponse:
            """htmx GET fragment: the read-only description block (cancel target)."""
            c = engine.get(cid)
            if c is None:
                return HTMLResponse(content="<div class='error'>not found</div>")
            desc = (c.description or "").replace("<", "&lt;").replace(">", "&gt;")
            return HTMLResponse(content=(
                f"<p id='desc-{cid}' class='d-desc' "
                f"hx-get='/app/c/{cid}/edit-description' hx-target='this' hx-swap='outerHTML' "
                f"style='cursor:text;' title='click to edit'>{desc or '<em style=\"color:var(--muted-2)\">click to add description</em>'}</p>"
            ))

        @app.post("/app/c/{cid}/description", include_in_schema=False)
        async def _ui_description_save(cid: str, request: Request) -> HTMLResponse:
            """htmx POST: save the edited description, return the read-only view."""
            form = await request.form()
            new_desc = str(form.get("description") or "").strip()
            try:
                c = engine.get(cid)
                if c is not None:
                    c.description = new_desc
                    engine.put(c)
            except Exception as e:  # noqa: BLE001
                log.warning("description save failed: %s", e)
            return _ui_description_view(cid)

        @app.get("/app/work", include_in_schema=False)
        def _ui_work(action: str = "") -> HTMLResponse:
            """Unified workflow surface: AFK + denials + rules + tasks.

            All four are part of the same mental model — "things that
            shape what AI does next" — so they live together instead of
            being scattered across separate pages.
            """
            from memex.core.schema import NodeKind as _NK
            # Rules
            rule_concepts = engine.semantic.find_by_kind(_NK.action_constraint) or []
            rules = []
            for c in rule_concepts:
                md = c.metadata or {}
                rules.append({
                    "id": c.id, "name": c.name,
                    "description": c.description or "",
                    "verdict": md.get("verdict", "deny"),
                    "match_pattern": md.get("match_pattern"),
                    "disabled": bool(md.get("disabled")),
                    "baseline": bool(md.get("baseline")),
                })
            # Denials
            denial_events = [
                e for e in engine.episodic.recent(limit=100) or []
                if getattr(e, "kind", "") == "gate_denied"
            ][:20]
            recent_denials = [{
                "rel_time": _rel_time(getattr(e, "timestamp", None)),
                "intent": (getattr(e, "payload", {}) or {}).get("intent") or "blocked action",
                "rule": (getattr(e, "payload", {}) or {}).get("rule"),
            } for e in denial_events]
            # AFK
            try:
                from memex.enforcement.policies import _afk_state
                afk_concept = _afk_state(engine)
                afk_on = afk_concept is not None
                afk_until = (afk_concept.metadata or {}).get("expires_at") if afk_concept else None
            except Exception:  # noqa: BLE001
                afk_on, afk_until = False, None
            # Tasks (Jira-style grouping by project, same shape as _ui_tasks).
            all_tasks = engine.semantic.find_by_kind(_NK.task) or []
            by_project: dict[str, dict[str, list[Any]]] = {}
            counts = {"pending": 0, "in_progress": 0, "completed": 0}
            for t in all_tasks:
                md = t.metadata or {}
                proj = md.get("project") or "Unfiled"
                status = md.get("status") or "pending"
                if status not in counts and status != "completed":
                    status = "pending"
                by_project.setdefault(proj, {"in_progress": [], "pending": [], "completed": []})
                if status not in by_project[proj]:
                    status = "pending"
                by_project[proj][status].append({
                    "id": t.id, "name": t.name,
                    "description": t.description or "",
                    "priority": md.get("priority"),
                    "due_rel": _rel_time(md.get("due_at")),
                    "status": status,
                })
                if status in counts:
                    counts[status] += 1
            projects = []
            for pname, lanes in by_project.items():
                active_count = len(lanes["in_progress"]) + len(lanes["pending"])
                if active_count == 0:
                    continue
                projects.append({
                    "name": pname,
                    "active": active_count,
                    "in_progress": lanes["in_progress"],
                    "pending": lanes["pending"],
                    "completed": lanes["completed"][:5],
                })
            projects.sort(key=lambda p: -p["active"])
            return _render(
                "work.html",
                active="work",
                action=action,
                rules=rules,
                recent_denials=recent_denials,
                afk_on=afk_on,
                afk_until=afk_until,
                projects=projects,
                counts=counts,
            )

        @app.get("/app/rules", include_in_schema=False)
        def _ui_rules(action: str = "") -> HTMLResponse:
            from memex.core.schema import NodeKind as _NK
            rule_concepts = engine.semantic.find_by_kind(_NK.action_constraint) or []
            rules = []
            for c in rule_concepts:
                md = c.metadata or {}
                rules.append({
                    "id": c.id,
                    "name": c.name,
                    "description": c.description or "",
                    "verdict": md.get("verdict", "deny"),
                    "applies_to": md.get("applies_to"),
                    "match_pattern": md.get("match_pattern"),
                    "scope": md.get("scope"),
                })
            denial_events = [
                e for e in engine.episodic.recent(limit=100) or []
                if getattr(e, "kind", "") == "gate_denied"
            ][:20]
            recent_denials = [
                {
                    "rel_time": _rel_time(getattr(e, "timestamp", None)),
                    "intent": (getattr(e, "payload", {}) or {}).get("intent") or "blocked action",
                    "rule": (getattr(e, "payload", {}) or {}).get("rule"),
                }
                for e in denial_events
            ]
            try:
                from memex.enforcement.policies import _afk_state
                afk_concept = _afk_state(engine)
                afk_on = afk_concept is not None
                afk_until = (afk_concept.metadata or {}).get("expires_at") if afk_concept else None
            except Exception:  # noqa: BLE001
                afk_on, afk_until = False, None
            return _render(
                "rules.html",
                active="rules",
                action=action,
                rules=rules,
                recent_denials=recent_denials,
                afk_on=afk_on,
                afk_until=afk_until,
            )

        @app.post("/app/rules/install-baseline", include_in_schema=False)
        def _ui_install_baseline_rules() -> RedirectResponse:
            """One-click install of the 15-rule safety baseline."""
            try:
                from memex.enforcement.baseline_rules import install_baseline_rules
                install_baseline_rules(engine)
            except Exception as e:  # noqa: BLE001
                log.warning("baseline rules install failed: %s", e)
            return RedirectResponse(url="/app/work#rules", status_code=303)

        @app.post("/app/rules/create", include_in_schema=False)
        async def _ui_rules_create(request: Request) -> RedirectResponse:
            form = await request.form()
            from memex.core.schema import Concept, NodeKind as _NK, Source
            name = str(form.get("name") or "").strip()
            pattern = str(form.get("match_pattern") or "").strip()
            description = str(form.get("description") or "").strip() or None
            if not name or not pattern:
                return RedirectResponse(url="/app/rules?action=create", status_code=303)
            engine.semantic.add_concept(Concept(
                name=name, kind=_NK.action_constraint, description=description,
                source=Source.human,
                metadata={"verdict": "deny", "match_pattern": pattern, "scope": "all"},
            ))
            return RedirectResponse(url="/app/rules", status_code=303)

        @app.post("/app/rules/{cid}/delete", include_in_schema=False)
        def _ui_rules_delete(cid: str) -> RedirectResponse:
            try:
                engine.semantic.delete_concept(cid)
            except Exception:  # noqa: BLE001
                pass
            return RedirectResponse(url="/app/rules", status_code=303)

        @app.post("/app/rules/{cid}/toggle", include_in_schema=False)
        def _ui_rules_toggle(cid: str) -> RedirectResponse:
            """Soft-disable a rule: flip metadata.disabled. Check_action
            skips disabled rules. Allows users to silence a misfiring
            baseline rule without losing the regex.
            """
            try:
                c = engine.get(cid)
                if c is not None:
                    md = dict(c.metadata or {})
                    md["disabled"] = not bool(md.get("disabled"))
                    c.metadata = md
                    engine.put(c)
            except Exception as e:  # noqa: BLE001
                log.warning("rule toggle failed: %s", e)
            return RedirectResponse(url="/app/work#rules", status_code=303)

        @app.post("/app/rules/afk/on", include_in_schema=False)
        async def _ui_afk_on(request: Request) -> RedirectResponse:
            form = await request.form()
            hours = int(form.get("hours") or 2)
            try:
                from memex.enforcement import enable_afk_mode
                enable_afk_mode(engine, duration_hours=hours, note="paused from web UI")
            except Exception:  # noqa: BLE001
                pass
            return RedirectResponse(url="/app/rules", status_code=303)

        @app.post("/app/rules/afk/off", include_in_schema=False)
        def _ui_afk_off() -> RedirectResponse:
            try:
                from memex.enforcement import disable_afk_mode
                disable_afk_mode(engine)
            except Exception:  # noqa: BLE001
                pass
            return RedirectResponse(url="/app/rules", status_code=303)

        @app.get("/app/tasks", include_in_schema=False)
        def _ui_tasks(action: str = "", project: str = "") -> HTMLResponse:
            """Jira-style: tasks grouped by project, with pending/in-progress/done lanes
            inside each project. The "Unfiled" bucket catches tasks without a project.
            """
            from memex.core.schema import NodeKind as _NK
            all_tasks = engine.semantic.find_by_kind(_NK.task) or []
            # Group by project, lane within project.
            by_project: dict[str, dict[str, list[Any]]] = {}
            counts = {"pending": 0, "in_progress": 0, "completed": 0}
            for t in all_tasks:
                md = t.metadata or {}
                proj = md.get("project") or "Unfiled"
                status = md.get("status") or "pending"
                if status not in counts and status != "completed":
                    status = "pending"
                by_project.setdefault(proj, {"in_progress": [], "pending": [], "completed": []})
                if status not in by_project[proj]:
                    status = "pending"
                by_project[proj][status].append({
                    "id": t.id,
                    "name": t.name,
                    "description": t.description or "",
                    "priority": md.get("priority"),
                    "project": proj,
                    "due_rel": _rel_time(md.get("due_at")),
                    "blocked_by_names": [],
                    "status": status,
                })
                if status in counts:
                    counts[status] += 1
            # Optional project filter
            project_names = sorted(by_project.keys())
            if project and project in by_project:
                shown = {project: by_project[project]}
            else:
                shown = by_project
            projects = []
            for pname, lanes in shown.items():
                active_count = len(lanes["in_progress"]) + len(lanes["pending"])
                if active_count == 0 and pname != project:
                    continue  # collapse empty projects
                projects.append({
                    "name": pname,
                    "active": active_count,
                    "in_progress": lanes["in_progress"],
                    "pending": lanes["pending"],
                    "completed": lanes["completed"][:5],
                })
            projects.sort(key=lambda p: -p["active"])
            return _render(
                "tasks.html", active="tasks", action=action,
                projects=projects, project_filter=project, project_names=project_names,
                counts=counts,
            )

        @app.post("/app/tasks/create", include_in_schema=False)
        async def _ui_tasks_create(request: Request) -> RedirectResponse:
            form = await request.form()
            from memex.core.schema import Concept, Edge, EdgeKind, NodeKind as _NK, Source
            name = str(form.get("name") or "").strip()
            description = str(form.get("description") or "").strip() or None
            priority = str(form.get("priority") or "").strip() or None
            project = str(form.get("project") or "").strip() or None
            if not name:
                return RedirectResponse(url="/app/tasks?action=create", status_code=303)
            meta: dict[str, Any] = {"status": "pending"}
            if priority: meta["priority"] = priority
            if project: meta["project"] = project
            task_id = engine.semantic.add_concept(Concept(
                name=name, kind=_NK.task, description=description,
                source=Source.human, metadata=meta,
            ))
            # If a project name was supplied, link the task to the matching
            # project concept via `part_of` so the Project detail page +
            # cross-page navigation works ("show me all tasks in project X").
            # Idempotent: only links if a project of that name exists.
            if project:
                try:
                    existing_project = engine.semantic.find_by_name_kind_source(
                        project, _NK.project, Source.human,
                    )
                    if existing_project is not None:
                        engine.semantic.add_edge(Edge(
                            from_id=task_id, to_id=existing_project.id,
                            kind=EdgeKind.part_of, source=Source.human,
                        ))
                except Exception as e:  # noqa: BLE001
                    log.debug("task->project link skipped: %s", e)
            return RedirectResponse(url="/app/tasks", status_code=303)

        @app.post("/app/tasks/{cid}/start", include_in_schema=False)
        def _ui_tasks_start(cid: str) -> RedirectResponse:
            """Flip a task from pending → in_progress. Used by Kanban 'Start'."""
            try:
                c = engine.get(cid)
                if c is not None:
                    md = dict(c.metadata or {})
                    md["status"] = "in_progress"
                    c.metadata = md
                    engine.put(c)
            except Exception:  # noqa: BLE001
                pass
            ref = (md or {}).get("project") if locals().get("md") else None
            url = f"/app/project/lookup?name={ref}" if ref else "/app/work"
            return RedirectResponse(url=url, status_code=303)

        @app.get("/app/project/{pid}", include_in_schema=False)
        def _ui_project_detail(pid: str) -> HTMLResponse:
            """Per-project dashboard: tasks Kanban + decisions + sources + people."""
            from memex.core.schema import NodeKind as _NK
            p = engine.get(pid)
            if p is None or (p.kind.value if hasattr(p.kind, "value") else str(p.kind)) != "project":
                return RedirectResponse(url="/app/home", status_code=303)
            project = {
                "id": p.id,
                "name": p.name,
                "description": p.description or "",
                "path": (p.metadata or {}).get("path", ""),
            }
            # Tasks: filter by metadata.project == project.name
            buckets: dict[str, list[Any]] = {"in_progress": [], "pending": [], "completed": []}
            tasks_total = 0
            try:
                with engine.semantic._lock:  # type: ignore[attr-defined]
                    rows = engine.semantic.conn.execute(  # type: ignore[attr-defined]
                        "SELECT * FROM concepts "
                        "WHERE kind = 'task' "
                        "AND json_extract_string(metadata, '$.project') = ? "
                        "ORDER BY last_confirmed_at DESC LIMIT 500",
                        [p.name],
                    ).fetchall()
                from memex.core.stores.duckdb_store import _row_to_concept
                for r in rows:
                    t = _row_to_concept(r)
                    tasks_total += 1
                    md = t.metadata or {}
                    status = md.get("status") or "pending"
                    if status not in buckets:
                        status = "pending"
                    buckets[status].append({
                        "id": t.id, "name": t.name,
                        "description": t.description or "",
                        "priority": md.get("priority"),
                        "due_rel": _rel_time(md.get("due_at")),
                    })
            except Exception:  # noqa: BLE001
                pass
            # Decisions linked via part_of edge → this project
            decisions = []
            sources = []
            try:
                with engine.semantic._lock:  # type: ignore[attr-defined]
                    rows = engine.semantic.conn.execute(  # type: ignore[attr-defined]
                        "SELECT c.* FROM concepts c "
                        "JOIN edges e ON e.from_id = c.id "
                        "WHERE e.to_id = ? AND e.kind = 'part_of' "
                        "AND c.kind IN ('decision','source','person') "
                        "ORDER BY c.last_confirmed_at DESC LIMIT 200",
                        [pid],
                    ).fetchall()
                from memex.core.stores.duckdb_store import _row_to_concept
                for r in rows:
                    c = _row_to_concept(r)
                    kind = c.kind.value if hasattr(c.kind, "value") else str(c.kind)
                    if kind == "decision":
                        decisions.append({
                            "id": c.id, "name": c.name,
                            "description": c.description or "",
                            "rel_time": _rel_time(getattr(c, "created_at", None)),
                        })
                    elif kind == "source":
                        md = c.metadata or {}
                        sources.append({
                            "id": c.id, "name": c.name,
                            "path": md.get("path", ""),
                            "indexed_files": int(md.get("indexed_files") or 0),
                            "indexed_symbols": int(md.get("indexed_symbols") or 0),
                        })
            except Exception:  # noqa: BLE001
                pass
            # People → all `kind=person` concepts (project-scoping is loose)
            people = []
            try:
                with engine.semantic._lock:  # type: ignore[attr-defined]
                    rows = engine.semantic.conn.execute(  # type: ignore[attr-defined]
                        "SELECT c.* FROM concepts c "
                        "JOIN edges e ON e.from_id = c.id "
                        "WHERE e.to_id = ? AND e.kind = 'part_of' AND c.kind = 'person'",
                        [pid],
                    ).fetchall()
                from memex.core.stores.duckdb_store import _row_to_concept
                for r in rows:
                    c = _row_to_concept(r)
                    people.append({
                        "id": c.id, "name": c.name,
                        "email": (c.metadata or {}).get("email"),
                    })
            except Exception:  # noqa: BLE001
                pass
            counts = {
                "tasks_open": len(buckets["in_progress"]) + len(buckets["pending"]),
                "tasks_completed": len(buckets["completed"]),
                "tasks_total": tasks_total,
                "decisions": len(decisions),
                "sources": len(sources),
                "files": sum(s["indexed_files"] for s in sources),
                "symbols": sum(s["indexed_symbols"] for s in sources),
                "people": len(people),
            }
            return _render(
                "project.html",
                active="home",  # project pages "belong" to Home dashboard
                project=project,
                counts=counts,
                tasks={
                    "in_progress": buckets["in_progress"],
                    "pending": buckets["pending"],
                    "completed": buckets["completed"][:10],
                },
                decisions=decisions,
                sources=sources,
                people=people,
            )

        @app.post("/app/tasks/{cid}/done", include_in_schema=False)
        def _ui_tasks_done(cid: str) -> RedirectResponse:
            try:
                c = engine.get(cid)
                if c is not None:
                    md = dict(c.metadata or {})
                    md["status"] = "completed"
                    c.metadata = md
                    engine.put(c)
            except Exception:  # noqa: BLE001
                pass
            return RedirectResponse(url="/app/tasks", status_code=303)

        @app.post("/app/tasks/{cid}/delete", include_in_schema=False)
        def _ui_tasks_delete(cid: str) -> RedirectResponse:
            try:
                engine.semantic.delete_concept(cid)
            except Exception:  # noqa: BLE001
                pass
            return RedirectResponse(url="/app/tasks", status_code=303)

        @app.get("/app/code", include_in_schema=False)
        def _ui_code(action: str = "") -> HTMLResponse:
            """Code memory landing: sources grouped under projects + files/symbols/cross-repo."""
            from memex.codebase.sources import list_sources
            from memex.core.schema import NodeKind as _NK
            sources_raw = list_sources(engine) or []
            sources = []
            file_count = 0
            symbol_count = 0
            for s in sources_raw:
                md = s.metadata or {}
                ifiles = int(md.get("indexed_files") or 0)
                isyms = int(md.get("indexed_symbols") or 0)
                file_count += ifiles
                symbol_count += isyms
                sources.append({
                    "id": s.id,
                    "name": s.name,
                    "path": md.get("path", ""),
                    "git_remote": md.get("git_remote") or "",
                    "git_host": md.get("git_host") or "",
                    "indexed_files": ifiles,
                    "indexed_symbols": isyms,
                    "last_indexed_rel": _rel_time(md.get("last_indexed_at")),
                })
            # cross-repo same_as edge count
            try:
                with engine.semantic._lock:  # type: ignore[attr-defined]
                    cross_repo_count = int(engine.semantic.conn.execute(  # type: ignore[attr-defined]
                        "SELECT count(*) FROM edges WHERE kind = 'same_as'",
                    ).fetchone()[0])
            except Exception:  # noqa: BLE001
                cross_repo_count = 0
            # Source-to-project mapping via `part_of` edges. Sources without
            # a project parent fall into "Unfiled".
            source_to_project: dict[str, str] = {}
            project_concepts = engine.semantic.find_by_kind(_NK.project) or []
            project_lookup = {p.id: p for p in project_concepts}
            try:
                with engine.semantic._lock:  # type: ignore[attr-defined]
                    edge_rows = engine.semantic.conn.execute(  # type: ignore[attr-defined]
                        "SELECT from_id, to_id FROM edges WHERE kind = 'part_of'",
                    ).fetchall()
                for from_id, to_id in edge_rows:
                    if to_id in project_lookup:
                        source_to_project[from_id] = to_id
            except Exception:  # noqa: BLE001
                pass
            # Group sources by project_id.
            grouped: dict[str, dict[str, Any]] = {}
            for s in sources:
                pid = source_to_project.get(s["id"])
                if pid and pid in project_lookup:
                    pname = project_lookup[pid].name
                    key = pid
                else:
                    pname = "Unfiled"
                    key = "_unfiled"
                if key not in grouped:
                    grouped[key] = {"id": pid, "name": pname, "sources": []}
                grouped[key]["sources"].append(s)
            project_groups = sorted(grouped.values(), key=lambda g: (g["name"] == "Unfiled", g["name"]))
            return _render(
                "code.html",
                active="code",
                action=action,
                sources=sources,
                project_groups=project_groups,
                all_projects=[{"id": p.id, "name": p.name} for p in project_concepts],
                source_count=len(sources),
                file_count=file_count,
                symbol_count=symbol_count,
                cross_repo_count=cross_repo_count,
                selected_source=None,
                files=[], orphans=[], orphan_count=0,
            )

        @app.post("/app/code/{sid}/move-to-project", include_in_schema=False)
        async def _ui_code_move_to_project(sid: str, request: Request) -> RedirectResponse:
            """Reparent a source under a project (or create a new project)."""
            form = await request.form()
            target = str(form.get("project_id") or "").strip()
            new_name = str(form.get("new_project_name") or "").strip()
            from memex.core.schema import Concept as _C, Edge as _E, EdgeKind as _EK, NodeKind as _NK, Source as _SRC
            project_id: str | None = None
            if target == "__new__" and new_name:
                # Create the new project if it doesn't exist.
                existing = engine.semantic.find_by_name_kind_source(new_name, _NK.project, _SRC.human)
                if existing is not None:
                    project_id = existing.id
                else:
                    pid = engine.semantic.add_concept(_C(
                        name=new_name, kind=_NK.project, source=_SRC.human,
                        description=f"Project grouping sources",
                    ))
                    project_id = pid
            elif target and target != "__none__":
                project_id = target
            # Remove existing source→project part_of edges first (idempotent).
            try:
                with engine.semantic._lock:  # type: ignore[attr-defined]
                    engine.semantic.conn.execute(  # type: ignore[attr-defined]
                        "DELETE FROM edges WHERE from_id = ? AND kind = 'part_of' "
                        "AND to_id IN (SELECT id FROM concepts WHERE kind = 'project')",
                        [sid],
                    )
            except Exception:  # noqa: BLE001
                pass
            if project_id:
                try:
                    engine.semantic.add_edge(_E(
                        from_id=sid, to_id=project_id,
                        kind=_EK.part_of, source=_SRC.human,
                    ))
                except Exception:  # noqa: BLE001
                    pass
            return RedirectResponse(url="/app/code", status_code=303)

        @app.get("/app/code/{sid}", include_in_schema=False)
        def _ui_code_source(sid: str) -> HTMLResponse:
            """Drill into one source: file list + orphans + cross-repo siblings."""
            from memex.codebase.sources import list_sources
            from memex.codebase.recall import find_orphans
            from memex.core.schema import NodeKind as _NK
            src = engine.get(sid)
            if src is None or (src.kind.value if hasattr(src.kind, "value") else str(src.kind)) != "source":
                return RedirectResponse(url="/app/code", status_code=303)
            md = src.metadata or {}
            selected_source = {
                "id": src.id,
                "name": src.name,
                "path": md.get("path", ""),
                "indexed_files": int(md.get("indexed_files") or 0),
                "indexed_symbols": int(md.get("indexed_symbols") or 0),
            }
            # Files: find all `file` concepts whose metadata.source_id == sid.
            try:
                with engine.semantic._lock:  # type: ignore[attr-defined]
                    rows = engine.semantic.conn.execute(  # type: ignore[attr-defined]
                        "SELECT id, name, description, metadata FROM concepts "
                        "WHERE kind = 'file' AND json_extract_string(metadata, '$.source_id') = ? "
                        "ORDER BY name LIMIT 500",
                        [sid],
                    ).fetchall()
                import json as _json
                files = []
                for fid, name, desc, meta_json in rows:
                    meta = _json.loads(meta_json) if isinstance(meta_json, str) else (meta_json or {})
                    files.append({
                        "id": fid, "name": name,
                        "path": meta.get("path", ""),
                        "symbol_count": int(meta.get("symbol_count") or 0),
                    })
            except Exception as exc:  # noqa: BLE001
                log.warning("file listing failed: %s", exc)
                files = []
            # Orphans (dead-code candidates).
            try:
                orphan_concepts = find_orphans(engine, source_id=sid, include_private=False)[:50]
                orphans = []
                for o in orphan_concepts:
                    om = o.metadata or {}
                    orphans.append({
                        "id": o.id, "name": o.name,
                        "symbol_kind": om.get("symbol_kind", "?"),
                        "file_path": om.get("file_path", ""),
                        "start_line": om.get("start_line"),
                    })
            except Exception as exc:  # noqa: BLE001
                log.warning("find_orphans failed: %s", exc)
                orphans = []
            # Source/file/symbol totals (for hero — same as landing).
            return _render(
                "code.html",
                active="code",
                action="",
                sources=[],
                source_count=0, file_count=0, symbol_count=0, cross_repo_count=0,
                selected_source=selected_source,
                files=files,
                orphans=orphans,
                orphan_count=len(orphans),
            )

        @app.post("/app/code/add", include_in_schema=False)
        async def _ui_code_add(request: Request) -> RedirectResponse:
            form = await request.form()
            path = str(form.get("path") or "").strip()
            name = str(form.get("name") or "").strip() or None
            if not path:
                return RedirectResponse(url="/app/code?action=add", status_code=303)
            try:
                from memex.codebase.sources import add_source
                add_source(engine, path, name=name)
            except Exception as exc:  # noqa: BLE001
                log.warning("add_source failed: %s", exc)
            return RedirectResponse(url="/app/code", status_code=303)

        @app.get("/app/code/symbol/{cid}", include_in_schema=False)
        def _ui_code_symbol(cid: str) -> HTMLResponse:
            """Symbol detail: body + callers + callees + cross-repo siblings + touches."""
            c = engine.get(cid)
            if c is None:
                return RedirectResponse(url="/app/code", status_code=303)
            md = c.metadata or {}
            # File and source context
            file_id = md.get("file_id")
            source_id = md.get("source_id")
            file_path = ""
            source_name = ""
            if file_id:
                f = engine.get(file_id)
                if f is not None:
                    file_path = (f.metadata or {}).get("path", "") or f.name
            if source_id:
                s = engine.get(source_id)
                if s is not None:
                    source_name = s.name
            # Map language hint for Prism
            lang_map = {"py": "python", "go": "go", "js": "javascript", "ts": "typescript",
                        "java": "java", "rs": "rust", "sh": "bash", "yaml": "yaml",
                        "yml": "yaml", "json": "json", "sql": "sql", "md": "markdown"}
            lang = md.get("language") or "plaintext"
            lang = lang_map.get(lang.lower() if lang else "", lang)
            symbol = {
                "id": c.id, "name": c.name,
                "symbol_kind": md.get("symbol_kind"),
                "signature": md.get("signature"),
                "docstring": md.get("docstring"),
                "body": md.get("body"),
                "language": lang,
                "start_line": md.get("start_line"),
                "end_line": md.get("end_line"),
                "file_id": file_id,
                "file_path": file_path,
                "source_id": source_id,
                "source_name": source_name,
                "is_public": not (c.name or "").startswith("_"),
            }
            # Callers (incoming `calls` edges)
            callers, callees, siblings, touched_by = [], [], [], []
            try:
                with engine.semantic._lock:  # type: ignore[attr-defined]
                    caller_rows = engine.semantic.conn.execute(  # type: ignore[attr-defined]
                        "SELECT from_id FROM edges WHERE to_id = ? AND kind = 'calls' LIMIT 50",
                        [cid],
                    ).fetchall()
                    callee_rows = engine.semantic.conn.execute(  # type: ignore[attr-defined]
                        "SELECT to_id FROM edges WHERE from_id = ? AND kind = 'calls' LIMIT 50",
                        [cid],
                    ).fetchall()
                    sibling_rows = engine.semantic.conn.execute(  # type: ignore[attr-defined]
                        "SELECT to_id FROM edges WHERE from_id = ? AND kind = 'same_as' LIMIT 20",
                        [cid],
                    ).fetchall()
                    touched_rows = engine.semantic.conn.execute(  # type: ignore[attr-defined]
                        "SELECT from_id FROM edges WHERE to_id = ? AND kind = 'touches' LIMIT 20",
                        [cid],
                    ).fetchall()
                # Helper to resolve a symbol's file path via its file_id.
                # The symbol's metadata stores file_id, not file_path —
                # we deref one hop so the UI can show "func X in foo.go".
                _path_cache: dict[str, str] = {}
                def _file_path_for(sym_meta: dict[str, Any]) -> str:
                    fid = sym_meta.get("file_id")
                    if not fid:
                        return ""
                    if fid in _path_cache:
                        return _path_cache[fid]
                    f = engine.get(fid)
                    p = ""
                    if f is not None:
                        p = (f.metadata or {}).get("path") or f.name or ""
                    _path_cache[fid] = p
                    return p
                for (other_id,) in caller_rows:
                    o = engine.get(other_id)
                    if o is not None:
                        callers.append({
                            "id": o.id, "name": o.name,
                            "file_path": _file_path_for(o.metadata or {}),
                        })
                for (other_id,) in callee_rows:
                    o = engine.get(other_id)
                    if o is not None:
                        callees.append({
                            "id": o.id, "name": o.name,
                            "file_path": _file_path_for(o.metadata or {}),
                        })
                for (other_id,) in sibling_rows:
                    o = engine.get(other_id)
                    if o is not None:
                        s_src_id = (o.metadata or {}).get("source_id")
                        s_src_name = ""
                        if s_src_id:
                            s_src = engine.get(s_src_id)
                            if s_src:
                                s_src_name = s_src.name
                        siblings.append({"id": o.id, "name": o.name, "source_name": s_src_name})
                for (other_id,) in touched_rows:
                    o = engine.get(other_id)
                    if o is not None:
                        touched_by.append({
                            "id": o.id, "name": o.name,
                            "kind": o.kind.value if hasattr(o.kind, "value") else str(o.kind),
                        })
            except Exception as exc:  # noqa: BLE001
                log.warning("symbol detail neighbors failed: %s", exc)
            return _render(
                "symbol.html",
                active="code",
                symbol=symbol,
                callers=callers, callees=callees,
                siblings=siblings, touched_by=touched_by,
            )

        @app.get("/app/code/file/{cid}", include_in_schema=False)
        def _ui_code_file(cid: str) -> HTMLResponse:
            """File viewer: symbol list + body (if stored)."""
            c = engine.get(cid)
            if c is None:
                return RedirectResponse(url="/app/code", status_code=303)
            md = c.metadata or {}
            source_id = md.get("source_id")
            source_name = ""
            if source_id:
                s = engine.get(source_id)
                if s is not None:
                    source_name = s.name
            file = {
                "id": c.id, "name": c.name,
                "path": md.get("path", ""),
                "language": md.get("language") or "plaintext",
                "symbol_count": md.get("symbol_count") or 0,
                "body": md.get("body"),
                "source_id": source_id,
                "source_name": source_name,
            }
            # Child symbols
            symbols = []
            try:
                with engine.semantic._lock:  # type: ignore[attr-defined]
                    rows = engine.semantic.conn.execute(  # type: ignore[attr-defined]
                        "SELECT id, name, metadata FROM concepts "
                        "WHERE kind = 'symbol' AND json_extract_string(metadata, '$.file_id') = ? "
                        "ORDER BY CAST(json_extract_string(metadata, '$.start_line') AS INTEGER) ASC LIMIT 500",
                        [cid],
                    ).fetchall()
                import json as _json
                for sid_, name, meta_json in rows:
                    sm = _json.loads(meta_json) if isinstance(meta_json, str) else (meta_json or {})
                    symbols.append({
                        "id": sid_, "name": name,
                        "symbol_kind": sm.get("symbol_kind"),
                        "signature": sm.get("signature"),
                        "start_line": sm.get("start_line"),
                    })
            except Exception as exc:  # noqa: BLE001
                log.warning("file symbol listing failed: %s", exc)
            return _render("file.html", active="code", file=file, symbols=symbols)

        @app.post("/app/code/{sid}/reindex", include_in_schema=False)
        def _ui_code_reindex(sid: str) -> RedirectResponse:
            try:
                from memex.codebase.indexer import reindex_source
                reindex_source(engine, sid)
            except Exception as exc:  # noqa: BLE001
                log.warning("reindex failed: %s", exc)
            return RedirectResponse(url=f"/app/code/{sid}", status_code=303)

        @app.post("/app/seed", include_in_schema=False)
        async def _ui_seed(request: Request) -> HTMLResponse:
            """Seed memex from a repo path. Mounted on Home as a CTA when
            the graph looks empty. Returns a report fragment for htmx swap.
            """
            form = await request.form()
            path = str(form.get("path") or "").strip()
            dry_run = bool(form.get("dry_run"))
            if not path:
                return HTMLResponse(content="<div class='error'>path required</div>")
            try:
                from memex.seed import seed_from_repo
                report = seed_from_repo(
                    engine, path,
                    max_commits=int(form.get("max_commits") or 500),
                    include_code_symbols=not form.get("skip_code"),
                    dry_run=dry_run,
                )
            except Exception as e:  # noqa: BLE001
                return HTMLResponse(content=f"<div class='error'>seed failed: {e}</div>")
            html = (
                f"<div class='card' style='background:#f0fdf4; border-color:#bbf7d0;'>"
                f"<h2 style='color:#15803d;'>Seeded {report.total_added()} concepts from {path}</h2>"
                f"<ul style='font-size:12.5px; margin:8px 0;'>"
                f"<li>{report.decisions_added} decisions</li>"
                f"<li>{report.facts_added} facts</li>"
                f"<li>{report.people_added} people</li>"
                f"<li>{report.commits_kept} of {report.commits_scanned} commits kept</li>"
                f"<li>{report.docs_scanned} docs scanned</li>"
                f"<li>{report.manifests_scanned} manifests scanned</li>"
                f"<li>code indexed: {'yes' if report.code_indexed else 'no'}</li>"
                f"</ul>"
                f"<a class='cta' href='/app/search'>Browse memory →</a>"
                f"</div>"
            )
            return HTMLResponse(content=html)

        @app.get("/app/system", include_in_schema=False)
        def _ui_system(kind: str = "") -> HTMLResponse:
            """System page: SLO board, maintenance triggers, event stream,
            doctor, export. The 'how memex itself is running' surface.
            """
            stats = engine.stats()
            doctor_data = doctor_endpoint()  # type: ignore[name-defined]
            # SLO board — pulled from in-memory _VERB_LATENCY histogram.
            # OMP §6 budgets:
            verb_budgets = {
                "recall": 200, "remember": 100, "link": 100,
                "observe": 50, "validate": 80,
            }
            verbs = []
            snap = _latency_snapshot()
            for verb_name, budget_ms in verb_budgets.items():
                h = snap["verbs"].get(verb_name, {
                    "count": 0, "max_ms": 0.0, "over_ceiling": 0,
                })
                verbs.append({
                    "name": verb_name,
                    "budget_ms": budget_ms,
                    "count": h.get("count", 0),
                    "max_ms": round(h.get("max_ms", 0.0), 1),
                    "over_ceiling": h.get("over_ceiling", 0),
                })
            # Maintenance jobs the scheduler runs.
            maintenance_jobs = [
                {"slug": "consolidate", "label": "Consolidate",
                 "desc": "Dedup concepts + promote co-occurring patterns",
                 "url": "/maintenance/consolidate", "cadence": "30 min"},
                {"slug": "rollup", "label": "Episodic rollup",
                 "desc": "Fold high-volume events into summary concepts",
                 "url": "/maintenance/episodic-rollup", "cadence": "12 h"},
                {"slug": "cleanup", "label": "Cleanup",
                 "desc": "Forget stale + orphaned concepts (protected kinds safe)",
                 "url": "/maintenance/cleanup", "cadence": "24 h"},
                {"slug": "change-link", "label": "Change provenance",
                 "desc": "Link active task to files it just edited",
                 "url": "/maintenance/change-link", "cadence": "5 min"},
                {"slug": "prompt-facts", "label": "Prompt facts",
                 "desc": "Extract durable facts from your recent user prompts",
                 "url": "/maintenance/prompt-facts", "cadence": "10 min"},
                {"slug": "enrich", "label": "Enrichment",
                 "desc": "Pull facts from configured upstream MCPs",
                 "url": "/maintenance/enrich", "cadence": "daily"},
                {"slug": "cross-repo", "label": "Cross-repo same_as linker",
                 "desc": "Detect symbols that exist in multiple sources (e.g. shared types)",
                 "url": "/maintenance/cross-repo-link", "cadence": "manual"},
                {"slug": "link-tasks", "label": "Link tasks to projects",
                 "desc": "Retroactively create part_of edges from tasks to their project concept",
                 "url": "/maintenance/link-tasks-to-projects", "cadence": "manual"},
            ]
            # Recent events (optionally filtered by kind).
            raw_events = list(engine.episodic.recent(limit=200, kind=kind or None) or [])
            events = []
            for e in raw_events[:60]:
                payload = getattr(e, "payload", {}) or {}
                summary = (
                    payload.get("name")
                    or payload.get("intent")
                    or payload.get("tool_name")
                    or payload.get("prompt", "")[:120]
                    or ""
                )
                events.append({
                    "kind": getattr(e, "kind", ""),
                    "rel_time": _rel_time(getattr(e, "timestamp", None)),
                    "actor": getattr(e, "actor", None).value if hasattr(getattr(e, "actor", None), "value") else str(getattr(e, "actor", "")),
                    "summary": (summary or "(no payload)")[:160],
                })
            # Available event kinds (for the filter dropdown).
            event_kinds = sorted({getattr(e, "kind", "") for e in raw_events})
            return _render(
                "system.html",
                active="system",
                stats=stats,
                omp_version="v0.1",
                verbs=verbs,
                maintenance_jobs=maintenance_jobs,
                events=events,
                event_kinds=event_kinds,
                event_filter=kind,
                doctor=doctor_data,
            )

        @app.get("/app/system/forgotten", include_in_schema=False)
        def _ui_system_forgotten() -> HTMLResponse:
            """Tombstoned concepts (anything cleanup deleted). One-click restore."""
            items: list[dict[str, Any]] = []
            try:
                import json as _json
                with engine.semantic._lock:  # type: ignore[attr-defined]
                    rows = engine.semantic.conn.execute(  # type: ignore[attr-defined]
                        "SELECT id, concept_blob, deleted_at, reason, restored_at "
                        "FROM concept_tombstones "
                        "ORDER BY deleted_at DESC LIMIT 200",
                    ).fetchall()
                for cid, blob, deleted_at, reason, restored_at in rows:
                    data = _json.loads(blob) if isinstance(blob, str) else (blob or {})
                    items.append({
                        "id": cid,
                        "name": data.get("name", "?"),
                        "kind": data.get("kind", "?"),
                        "description": (data.get("description") or "")[:200],
                        "reason": reason or "?",
                        "deleted_rel": _rel_time(deleted_at),
                        "restored": restored_at is not None,
                    })
            except Exception as e:  # noqa: BLE001
                log.warning("tombstone list failed: %s", e)
            tpl = _jinja.get_template("forgotten.html")
            return HTMLResponse(content=tpl.render(items=items, active="system"))

        @app.post("/app/system/forgotten/{cid}/restore", include_in_schema=False)
        def _ui_restore_tombstone(cid: str) -> RedirectResponse:
            """Restore a tombstoned concept by re-inserting from its blob."""
            try:
                import json as _json
                from memex.core.schema import Concept
                with engine.semantic._lock:  # type: ignore[attr-defined]
                    row = engine.semantic.conn.execute(  # type: ignore[attr-defined]
                        "SELECT concept_blob FROM concept_tombstones "
                        "WHERE id = ? AND restored_at IS NULL",
                        [cid],
                    ).fetchone()
                if row:
                    blob = row[0]
                    data = _json.loads(blob) if isinstance(blob, str) else blob
                    c = Concept.model_validate(data)
                    engine.semantic.add_concept(c)
                    with engine.semantic._lock:  # type: ignore[attr-defined]
                        engine.semantic.conn.execute(  # type: ignore[attr-defined]
                            "UPDATE concept_tombstones SET restored_at = CURRENT_TIMESTAMP WHERE id = ?",
                            [cid],
                        )
            except Exception as e:  # noqa: BLE001
                log.warning("restore failed for %s: %s", cid, e)
            return RedirectResponse(url="/app/system/forgotten", status_code=303)

        @app.post("/app/system/doctor-refresh", include_in_schema=False)
        def _ui_system_doctor_refresh() -> HTMLResponse:
            """Bust the 30s doctor cache so the next page load runs fresh."""
            try:
                _doctor_cache["data"] = None
                _doctor_cache["ts"] = 0.0
            except Exception:  # noqa: BLE001
                pass
            return HTMLResponse(content="", status_code=204)

        @app.post("/maintenance/change-link", include_in_schema=False)
        def _ui_run_change_link() -> dict[str, Any]:
            """Manual trigger for the change-provenance linker."""
            from memex.core.lifecycle.change_provenance import link_recent_changes
            return link_recent_changes(engine)

        @app.post("/maintenance/prompt-facts", include_in_schema=False)
        def _ui_run_prompt_facts() -> dict[str, Any]:
            """Manual trigger for the prompt-fact extractor."""
            from memex.core.lifecycle.prompt_facts import extract_prompt_facts
            return extract_prompt_facts(engine)

        @app.post("/maintenance/link-tasks-to-projects", include_in_schema=False)
        def _ui_link_tasks_to_projects() -> dict[str, Any]:
            """Retroactive: link every task whose metadata.project matches an
            existing project concept name. Idempotent — re-running is safe.
            """
            from memex.core.schema import Edge, EdgeKind, NodeKind as _NK, Source
            try:
                projects = engine.semantic.find_by_kind(_NK.project) or []
                proj_by_name = {p.name: p.id for p in projects}
                tasks = engine.semantic.find_by_kind(_NK.task) or []
                # Existing part_of edges from tasks to projects
                with engine.semantic._lock:  # type: ignore[attr-defined]
                    existing_rows = engine.semantic.conn.execute(  # type: ignore[attr-defined]
                        "SELECT from_id, to_id FROM edges WHERE kind = 'part_of'",
                    ).fetchall()
                existing = {(f, t) for f, t in existing_rows}
                linked = 0
                for t in tasks:
                    proj_name = (t.metadata or {}).get("project")
                    if not proj_name or proj_name not in proj_by_name:
                        continue
                    pid = proj_by_name[proj_name]
                    if (t.id, pid) in existing:
                        continue
                    try:
                        engine.semantic.add_edge(Edge(
                            from_id=t.id, to_id=pid,
                            kind=EdgeKind.part_of, source=Source.system,
                        ))
                        linked += 1
                    except Exception:  # noqa: BLE001
                        pass
                return {"linked": linked, "tasks_scanned": len(tasks),
                        "projects": len(projects)}
            except Exception as e:  # noqa: BLE001
                return {"error": str(e)}

        @app.post("/maintenance/cross-repo-link", include_in_schema=False)
        def _ui_run_cross_repo_link() -> dict[str, Any]:
            """Manual trigger for cross-repo `same_as` symbol linker.

            Scans every indexed source's symbols and creates `same_as`
            edges between symbols that look identical across repos
            (same name + similar signature). Without this, "this JWT
            struct exists in 5 services" recall doesn't work.
            """
            try:
                from memex.codebase.linker import link_cross_repo
                return link_cross_repo(engine)
            except Exception as e:  # noqa: BLE001
                return {"error": str(e)}

        @app.get("/app/system/export", include_in_schema=False)
        def _ui_system_export(format: str = "json") -> Response:
            """Export the entire graph. No lock-in — re-ingest into any
            OMP-compatible tool. Streams the response so even large graphs
            don't blow memory.
            """
            import json as _json
            from datetime import datetime as _dt2, timezone as _tz2
            ts = _dt2.now(_tz2.utc).strftime("%Y%m%d-%H%M%S")
            if format == "parquet":
                # Parquet via DuckDB's COPY TO — writes to a temp file and
                # streams it back. No extra deps.
                import tempfile as _tf, os as _os3
                tmp_dir = _tf.mkdtemp(prefix="memex-export-")
                concepts_p = _os3.path.join(tmp_dir, "concepts.parquet")
                edges_p = _os3.path.join(tmp_dir, "edges.parquet")
                events_p = _os3.path.join(tmp_dir, "events.parquet")
                try:
                    with engine.semantic._lock:  # type: ignore[attr-defined]
                        engine.semantic.conn.execute(  # type: ignore[attr-defined]
                            f"COPY (SELECT * FROM concepts) TO '{concepts_p}' (FORMAT PARQUET)",
                        )
                        engine.semantic.conn.execute(  # type: ignore[attr-defined]
                            f"COPY (SELECT * FROM edges) TO '{edges_p}' (FORMAT PARQUET)",
                        )
                        engine.semantic.conn.execute(  # type: ignore[attr-defined]
                            f"COPY (SELECT * FROM events) TO '{events_p}' (FORMAT PARQUET)",
                        )
                    # Bundle into a single zip for download.
                    import zipfile as _zf
                    zip_path = _os3.path.join(tmp_dir, f"memex-export-{ts}.zip")
                    with _zf.ZipFile(zip_path, "w", _zf.ZIP_DEFLATED) as zf:
                        zf.write(concepts_p, arcname="concepts.parquet")
                        zf.write(edges_p, arcname="edges.parquet")
                        zf.write(events_p, arcname="events.parquet")
                    return FileResponse(
                        zip_path, media_type="application/zip",
                        filename=f"memex-export-{ts}.zip",
                    )
                except Exception as exc:  # noqa: BLE001
                    return Response(
                        content=f"parquet export failed: {exc}",
                        status_code=500, media_type="text/plain",
                    )
            # JSON path (default). Walks tables, emits one JSON object.
            try:
                with engine.semantic._lock:  # type: ignore[attr-defined]
                    concept_rows = engine.semantic.conn.execute(  # type: ignore[attr-defined]
                        "SELECT * FROM concepts",
                    ).fetchall()
                    edge_rows = engine.semantic.conn.execute(  # type: ignore[attr-defined]
                        "SELECT from_id, to_id, kind, source, confidence, "
                        "created_at, last_confirmed_at, metadata FROM edges",
                    ).fetchall()
                    cols = [r[0] for r in engine.semantic.conn.execute(  # type: ignore[attr-defined]
                        "PRAGMA table_info('concepts')",
                    ).fetchall()]
                from memex.core.stores.duckdb_store import _row_to_concept
                concepts = [_row_to_concept(r).model_dump(mode="json") for r in concept_rows]
                edges_out = []
                for r in edge_rows:
                    edges_out.append({
                        "from_id": r[0], "to_id": r[1], "kind": r[2],
                        "source": r[3], "confidence": r[4],
                        "created_at": r[5].isoformat() if r[5] else None,
                        "last_confirmed_at": r[6].isoformat() if r[6] else None,
                        "metadata": _json.loads(r[7]) if isinstance(r[7], str) else (r[7] or {}),
                    })
                payload = {
                    "omp_version": "v0.1",
                    "exported_at": _dt2.now(_tz2.utc).isoformat(),
                    "concepts": concepts,
                    "edges": edges_out,
                }
                body = _json.dumps(payload, default=str, indent=2)
                return Response(
                    content=body, media_type="application/json",
                    headers={"Content-Disposition": f"attachment; filename=memex-export-{ts}.json"},
                )
            except Exception as exc:  # noqa: BLE001
                return Response(
                    content=f"json export failed: {exc}",
                    status_code=500, media_type="text/plain",
                )

        @app.get("/app/skills", include_in_schema=False)
        def _ui_skills() -> HTMLResponse:
            """Skills page — installed + built-in catalog + recent validations."""
            from memex.core.schema import NodeKind as _NK
            # Installed: kind=approach or kind=pattern (memex stores skills as concepts)
            try:
                approach_concepts = engine.semantic.find_by_kind(_NK.approach) or []
                pattern_concepts = engine.semantic.find_by_kind(_NK.pattern) or []
                installed_concepts = approach_concepts + pattern_concepts
            except Exception:  # noqa: BLE001
                installed_concepts = []
            installed = []
            installed_names: set[str] = set()
            for c in installed_concepts:
                md = c.metadata or {}
                # Only include those that look like skills (have checks)
                checks = md.get("checks") or []
                if not checks and not md.get("approach"):
                    continue
                installed.append({
                    "name": c.name,
                    "version": md.get("version"),
                    "description": c.description or "",
                    "checks_count": len(checks),
                })
                installed_names.add(c.name)
            # Built-ins from the skills module
            builtins_data = []
            try:
                from memex.skills import list_builtin_skills
                for s in list_builtin_skills():
                    builtins_data.append({
                        "name": s.name,
                        "version": getattr(s, "version", None),
                        "description": getattr(s, "description", "") or "",
                        "triggers": getattr(s, "triggers", None) or [],
                        "installed": s.name in installed_names,
                    })
            except Exception as e:  # noqa: BLE001
                log.warning("list_builtin_skills failed: %s", e)
            # Recent validations (skill_validated episodic events)
            recent_validations = []
            try:
                events = engine.episodic.recent(limit=200) or []
                for e in events:
                    if getattr(e, "kind", "") in ("skill_validated", "skill_validation_failed"):
                        payload = getattr(e, "payload", {}) or {}
                        recent_validations.append({
                            "rel_time": _rel_time(getattr(e, "timestamp", None)),
                            "skill": payload.get("skill", "?"),
                            "outcome": "✓" if getattr(e, "kind", "") == "skill_validated" else "✗ " + (payload.get("reason") or "failed"),
                        })
                recent_validations = recent_validations[:15]
            except Exception:  # noqa: BLE001
                pass
            return _render(
                "skills.html",
                active="skills",
                installed=installed,
                builtins=builtins_data,
                recent_validations=recent_validations,
            )

        @app.post("/app/skills/install", include_in_schema=False)
        async def _ui_skills_install(request: Request) -> RedirectResponse:
            form = await request.form()
            name = str(form.get("name") or "").strip()
            if name:
                try:
                    from memex.skills import find_builtin_skill, install_skill
                    skill = find_builtin_skill(name)
                    if skill is not None:
                        install_skill(engine, skill)
                except Exception as e:  # noqa: BLE001
                    log.warning("skill install %s failed: %s", name, e)
            return RedirectResponse(url="/app/skills", status_code=303)

        @app.post("/app/skills/test", include_in_schema=False)
        async def _ui_skills_test(request: Request) -> RedirectResponse:
            form = await request.form()
            name = str(form.get("skill") or "").strip()
            try:
                engine.validate(name)
            except Exception as e:  # noqa: BLE001
                log.warning("skill validate %s failed: %s", name, e)
            return RedirectResponse(url="/app/skills", status_code=303)

        @app.post("/app/settings/service/install", include_in_schema=False)
        def _ui_service_install() -> RedirectResponse:
            try:
                from memex.service import install_service
                install_service()
            except Exception as e:  # noqa: BLE001
                log.warning("service install failed: %s", e)
            return RedirectResponse(url="/app/settings#service", status_code=303)

        @app.post("/app/settings/service/uninstall", include_in_schema=False)
        def _ui_service_uninstall() -> RedirectResponse:
            try:
                from memex.service import uninstall_service
                uninstall_service()
            except Exception as e:  # noqa: BLE001
                log.warning("service uninstall failed: %s", e)
            return RedirectResponse(url="/app/settings#service", status_code=303)

        @app.post("/app/settings/team/init", include_in_schema=False)
        async def _ui_team_init(request: Request) -> RedirectResponse:
            form = await request.form()
            path = str(form.get("path") or "").strip()
            if path:
                try:
                    from memex.team import init_team_repo
                    init_team_repo(path)
                    # Set env var so the scheduler picks it up. Persistent
                    # across restarts only if the user adds it to their
                    # shell profile — we surface a note in the UI.
                    import os as _os3
                    _os3.environ["MEMEX_TEAM_REPO"] = path
                except Exception as e:  # noqa: BLE001
                    log.warning("team init failed: %s", e)
            return RedirectResponse(url="/app/settings#team", status_code=303)

        @app.post("/app/settings/team/push", include_in_schema=False)
        def _ui_team_push() -> RedirectResponse:
            try:
                import os as _os3
                from memex.team import sync_push
                repo = _os3.environ.get("MEMEX_TEAM_REPO")
                if repo:
                    sync_push(engine, repo)
            except Exception as e:  # noqa: BLE001
                log.warning("team push failed: %s", e)
            return RedirectResponse(url="/app/settings#team", status_code=303)

        @app.post("/app/settings/team/pull", include_in_schema=False)
        def _ui_team_pull() -> RedirectResponse:
            try:
                import os as _os3
                from memex.team import sync_pull
                repo = _os3.environ.get("MEMEX_TEAM_REPO")
                if repo:
                    sync_pull(engine, repo)
            except Exception as e:  # noqa: BLE001
                log.warning("team pull failed: %s", e)
            return RedirectResponse(url="/app/settings#team", status_code=303)

        @app.post("/app/settings/hooks/install", include_in_schema=False)
        async def _ui_hooks_install(request: Request) -> RedirectResponse:
            form = await request.form()
            editor_key = str(form.get("editor") or "").strip()
            try:
                from memex.integrations import ClaudeCode, Cursor, Windsurf, Cline
                editors_map = {
                    "claude_code": ClaudeCode(),
                    "cursor": Cursor(),
                    "windsurf": Windsurf(),
                    "cline": Cline(),
                }
                target = editors_map.get(editor_key)
                if target is not None:
                    target.wire()
            except Exception as e:  # noqa: BLE001
                log.warning("hook install failed for %s: %s", editor_key, e)
            return RedirectResponse(url="/app/settings#integrations", status_code=303)

        @app.post("/app/settings/hooks/uninstall", include_in_schema=False)
        async def _ui_hooks_uninstall(request: Request) -> RedirectResponse:
            form = await request.form()
            editor_key = str(form.get("editor") or "").strip()
            try:
                from memex.integrations import ClaudeCode, Cursor, Windsurf, Cline
                editors_map = {
                    "claude_code": ClaudeCode(),
                    "cursor": Cursor(),
                    "windsurf": Windsurf(),
                    "cline": Cline(),
                }
                target = editors_map.get(editor_key)
                if target is not None:
                    # Read config, remove memex from mcpServers, write back.
                    import json as _json3
                    p = target.config_path()
                    if p.is_file():
                        data = _json3.loads(p.read_text(encoding="utf-8"))
                        servers = data.get("mcpServers") or {}
                        if "memex" in servers:
                            servers.pop("memex", None)
                            p.write_text(_json3.dumps(data, indent=2), encoding="utf-8")
            except Exception as e:  # noqa: BLE001
                log.warning("hook uninstall failed for %s: %s", editor_key, e)
            return RedirectResponse(url="/app/settings#integrations", status_code=303)

        def _detect_editors() -> list[dict[str, Any]]:
            """Inspect each AI-IDE config path; report detected + wired."""
            from memex.integrations import ClaudeCode, Cursor, Windsurf, Cline
            specs = [
                ("claude_code", "Claude Code",      "claude.svg",   ClaudeCode()),
                ("cursor",      "Cursor",           "cursor.svg",   Cursor()),
                ("windsurf",    "Windsurf",         "windsurf.svg", Windsurf()),
                ("cline",       "Cline (VS Code)",  "cline.png",    Cline()),
            ]
            out = []
            for key, name, logo, integ in specs:
                try:
                    status = integ.status()
                    out.append({
                        "key": key,
                        "name": name,
                        "logo": logo,
                        "detected": status.tool_installed,
                        "wired": status.memex_present,
                        "config_path": str(status.config_path) if status.tool_installed else "",
                    })
                except Exception:  # noqa: BLE001
                    out.append({
                        "key": key, "name": name, "logo": logo,
                        "detected": False, "wired": False, "config_path": "",
                    })
            return out

        @app.get("/app/settings", include_in_schema=False)
        def _ui_settings(section: str = "doctor") -> HTMLResponse:
            doctor_data = doctor_endpoint()  # type: ignore[name-defined]
            stats = engine.stats()
            try:
                from memex.upstreams import load_upstreams
                ups_cfg = load_upstreams()
                upstreams = [
                    {"name": u.name, "kind": getattr(u, "kind", "?"), "target": getattr(u, "command", None) or getattr(u, "url", "?")}
                    for u in ups_cfg.upstreams
                ]
            except Exception:  # noqa: BLE001
                upstreams = []
            try:
                from memex.frontends.http.server import _LOG_RING  # self-ref ok at runtime
                hooks_status = {"wired": [c["name"] for c in doctor_data["checks"] if c["name"] == "Claude Code hooks" and c["status"] == "ok"]}
            except Exception:  # noqa: BLE001
                hooks_status = {"wired": []}
            try:
                from memex.secrets import list_secrets
                raw_secrets = list_secrets(get_settings()) or []
                secrets = [
                    {
                        "provider": s.get("provider"), "name": s.get("name"),
                        "last_resolved_rel": _rel_time(s.get("last_resolved")),
                        "last_resolved": s.get("last_resolved"),
                        "created_rel": _rel_time(s.get("created_at")),
                    }
                    for s in raw_secrets
                ]
            except Exception:  # noqa: BLE001
                secrets = []
            try:
                from memex.core.schema import NodeKind as _NK2
                source_concepts = engine.semantic.find_by_kind(_NK2.source) or []
                sources = [
                    {
                        "id": c.id,
                        "name": c.name,
                        "path": (c.metadata or {}).get("path", ""),
                        "symbol_count": (c.metadata or {}).get("indexed_symbols"),
                    }
                    for c in source_concepts
                ]
            except Exception:  # noqa: BLE001
                sources = []
            # Service status (Mac launchd / Win Task Scheduler / Linux systemd-user)
            try:
                from memex.service import service_status
                svc = service_status()
            except Exception:  # noqa: BLE001
                svc = {"installed": False, "running": False}
            import sys as _sys2
            service_ctx = {
                "installed": bool(svc.get("installed")),
                "running": bool(svc.get("running")),
                "platform": {"darwin": "macOS", "win32": "Windows", "linux": "Linux"}.get(_sys2.platform, _sys2.platform),
            }
            # Team-mode status
            import os as _os3
            team_repo = _os3.environ.get("MEMEX_TEAM_REPO")
            team_ctx = {"configured": bool(team_repo), "repo": team_repo, "last_sync": None,
                        "concept_count": None, "edge_count": None}
            if team_repo:
                try:
                    import json as _json4
                    manifest_p = _Path(team_repo) / "memex-graph" / "manifest.json"
                    if manifest_p.is_file():
                        data = _json4.loads(manifest_p.read_text(encoding="utf-8"))
                        team_ctx["last_sync"] = _rel_time(data.get("last_push_at"))
                        team_ctx["concept_count"] = data.get("concept_count")
                        team_ctx["edge_count"] = data.get("edge_count")
                except Exception:  # noqa: BLE001
                    pass
            return _render(
                "settings.html",
                active="settings",
                open_section=section,
                doctor=doctor_data,
                stats=stats,
                upstreams=upstreams,
                hooks_status=hooks_status,
                editors=_detect_editors(),
                service=service_ctx,
                team=team_ctx,
                secrets=secrets,
                sources=sources,
            )

        @app.get("/health", tags=["meta"])
        def health() -> dict[str, Any]:
            return {"ok": True, "version": __version__, **engine.stats()}

        # Doctor result cache — checks include a 300ms Ollama probe and
        # filesystem scans. Many UI pages need the summary (health pill,
        # Home, Settings) so we serve a cached result for 30s. /doctor?refresh=1
        # bypasses the cache.
        _doctor_cache: dict[str, Any] = {"data": None, "ts": 0.0}

        @app.get("/doctor", tags=["meta"], dependencies=[Depends(check_auth)])
        def doctor_endpoint(refresh: bool = False) -> dict[str, Any]:
            """Structured doctor report — 8 cheap probes. Cached 30s.

            Idempotent + side-effect-free. Latency budget: <50ms cached,
            <400ms uncached (Ollama probe dominates).
            """
            if not refresh and _doctor_cache["data"] is not None and (time.time() - _doctor_cache["ts"]) < 30.0:
                return _doctor_cache["data"]
            import os as _os
            import socket as _socket
            from datetime import datetime as _dt, timezone as _tz
            from pathlib import Path as _Path
            checks: list[dict[str, Any]] = []

            def _add(status: str, name: str, detail: str, hint: str = "") -> None:
                checks.append({
                    "name": name, "status": status,
                    "detail": detail, "hint": hint,
                })

            # 1. data dir
            from memex.config import get_settings
            s = get_settings()
            ddir = _Path(str(s.data_dir))
            if ddir.is_dir():
                _add("ok", "data dir", str(ddir))
            else:
                _add("fail", "data dir", f"missing: {ddir}",
                     "run any memex command to auto-create")

            # 2. daemon — we're in it; trivially ok
            _add("ok", "daemon",
                 f"v{__version__} concepts={engine.stats().get('concepts',0)}")

            # 3. auth token
            from memex.runtime_state import read_auth_token
            t = s.auth_token or read_auth_token(s)
            if t:
                src = "env" if s.auth_token else "daemon.token disk"
                _add("ok", "auth token", f"loaded ({len(t)} bytes) — from {src}")
            else:
                _add("warn", "auth token",
                     "no MEMEX_AUTH_TOKEN env var and no daemon.token file",
                     "daemon mints one on first start")

            # 4. OMP version
            _add("ok", "OMP version", "v0.1, 5 verbs exposed")

            # 5. embeddings — cheap availability check, NOT a real recall.
            # Running engine.recall() here would force a fastembed forward
            # pass on every /doctor poll (~5-8s cold, ~500ms warm), which
            # is way over our 200ms latency budget and the reason this
            # endpoint felt broken.
            try:
                if engine.embedding_provider.is_available():
                    _add("ok", "embeddings",
                         f"available ({engine.settings.embed_model})")
                else:
                    _add("warn", "embeddings", "no embed model loaded",
                         "run `memex setup-models` or pin fastembed/onnxruntime")
            except Exception as e:  # noqa: BLE001
                _add("warn", "embeddings", f"probe failed: {e}")

            # 6. LLM hook
            providers: list[str] = []
            if _os.environ.get("ANTHROPIC_API_KEY"):
                providers.append("anthropic")
            if _os.environ.get("OPENAI_API_KEY"):
                providers.append("openai")
            try:
                import httpx as _httpx
                ollama_url = (_os.environ.get("OLLAMA_HOST")
                              or "http://127.0.0.1:11434")
                rr = _httpx.get(ollama_url + "/api/tags", timeout=0.3)
                if rr.status_code == 200:
                    providers.append("ollama")
            except Exception:  # noqa: BLE001
                pass
            if providers:
                _add("ok", "LLM hook",
                     f"available providers: {', '.join(providers)}")
            else:
                _add("warn", "LLM hook", "no LLM configured",
                     "set ANTHROPIC_API_KEY / OPENAI_API_KEY / run Ollama")

            # 7. Claude Code hooks
            home = _Path(_os.path.expanduser("~"))
            settings_json = home / ".claude" / "settings.json"
            if settings_json.is_file():
                try:
                    import json as _json
                    data = _json.loads(settings_json.read_text(encoding="utf-8"))
                    hooks_cfg = (data or {}).get("hooks", {}) or {}
                    wired: set[str] = set()
                    for ev, handlers in hooks_cfg.items():
                        if not isinstance(handlers, list):
                            continue
                        for h in handlers:
                            for c in (h or {}).get("hooks", []) or []:
                                if "memex" in ((c or {}).get("command") or ""):
                                    wired.add(ev)
                                    break
                    if wired:
                        _add("ok", "Claude Code hooks",
                             f"wired: {', '.join(sorted(wired))}")
                    else:
                        _add("warn", "Claude Code hooks",
                             "settings.json present but no memex hooks",
                             "run `memex hooks-install --apply`")
                except Exception as e:  # noqa: BLE001
                    _add("warn", "Claude Code hooks",
                         f"settings.json parse error: {e}")
            else:
                _add("warn", "Claude Code hooks",
                     f"{settings_json} not found",
                     "Claude Code not installed yet or first run")

            # 8. upstream MCPs
            try:
                from memex.upstreams import load_upstreams
                cfg = load_upstreams()
                ups = list(cfg.upstreams)
                if ups:
                    _add("ok", "upstream MCPs",
                         f"{len(ups)} installed: {', '.join(u.name for u in ups[:5])}")
                else:
                    _add("warn", "upstream MCPs", "none installed",
                         "run `memex upstream install fetch`")
            except Exception as e:  # noqa: BLE001
                _add("warn", "upstream MCPs", f"probe failed: {e}")

            fails = sum(1 for c in checks if c["status"] == "fail")
            warns = sum(1 for c in checks if c["status"] == "warn")
            result = {
                "ts": _dt.now(_tz.utc).isoformat(),
                "host": _socket.gethostname(),
                "memex_version": __version__,
                "checks": checks,
                "summary": {
                    "total": len(checks),
                    "ok": len(checks) - fails - warns,
                    "warn": warns,
                    "fail": fails,
                    "overall": "fail" if fails else ("warn" if warns else "ok"),
                },
            }
            _doctor_cache["data"] = result
            _doctor_cache["ts"] = time.time()
            return result

        @app.get("/version", tags=["meta"])
        def omp_version() -> dict[str, Any]:
            """OMP §11 version probe. Open (no auth) so federation peers
            can negotiate before exchanging bearer tokens. Returns the
            OMP spec version this server implements + the server's own
            build version. `supported_majors` lets clients fall back to
            an older protocol when they don't speak the current major.
            """
            return {
                "omp_version": "0.1",
                "supported_majors": [0],
                "server": "memex",
                "server_version": __version__,
                "verbs": ["recall", "remember", "link", "observe", "validate"],
            }

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

        @app.post("/code/regenerate", tags=["query"], dependencies=[Depends(check_auth)])
        def code_regenerate(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
            """Draft a code change from the cognitive memory neighborhood.

            Body:
              {
                "target": "<symbol-name-or-concept-id>",   # required
                "intent": "<bug description / desired behavior>",  # required
                "max_tokens": 2000,            # optional, LLM cap
                "heavy": true,                 # use the heavy model tier
              }

            Pipeline:
              1. recall_bundle(target) → primary + decisions + constraints
                 + tests + same_as + callers
              2. Compose a prompt: the AI sees the symbol, the rules it
                 must obey (constraints), the design rationale (decisions),
                 and the test signatures
              3. LLM (heavy by default) drafts a unified diff
              4. Return {bundle, draft_diff, llm_provider, llm_model_tier}

            Side effects: emits a `code_regenerate_requested` episodic
            event so the developer can audit AI-proposed changes.
            Does NOT apply the diff — the caller is responsible for
            review + apply. This is memory + intelligence, not autonomy.
            """
            target = (body or {}).get("target", "").strip()
            intent = (body or {}).get("intent", "").strip()
            if not target or not intent:
                raise HTTPException(
                    status_code=400,
                    detail="both 'target' and 'intent' are required",
                )
            bundle = engine.recall_bundle(target)
            if bundle.get("primary") is None:
                return {
                    "bundle": bundle,
                    "draft_diff": None,
                    "error": "target not found in memex; cannot regenerate without anchor concept",
                }
            llm = getattr(engine, "llm", None)
            if llm is None or not llm.is_available():
                return {
                    "bundle": bundle,
                    "draft_diff": None,
                    "error": "no LLM configured; set ANTHROPIC_API_KEY / OPENAI_API_KEY / OLLAMA",
                }

            # Compose a tight prompt. The neighborhood IS the context; we
            # don't dump the full file (the AI has the file via its IDE
            # context). The LLM's job is to propose the *change*, not
            # rewrite the file.
            primary = bundle["primary"]
            decisions = bundle.get("decisions", [])
            constraints = bundle.get("constraints", [])
            same_as = bundle.get("same_as", [])
            tests = bundle.get("tests", [])

            def _bullets(items: list[dict[str, Any]], n: int = 5) -> str:
                return "\n".join(
                    f"  - [{c.get('kind','')}] {c.get('name','')}: "
                    f"{(c.get('description') or '')[:300]}"
                    for c in items[:n]
                ) or "  (none)"

            prompt = (
                "You are a senior engineer asked to draft a code change.\n"
                "Below is the cognitive neighborhood from memex (the project's\n"
                "structured memory). Honor every constraint listed; respect the\n"
                "decisions; produce a unified diff against the primary symbol's\n"
                "defining file.\n\n"
                f"## Intent\n{intent}\n\n"
                f"## Primary symbol\n"
                f"{primary.get('name','')} (kind={primary.get('kind','')})\n"
                f"{(primary.get('description') or '')[:1000]}\n\n"
                f"## Defined in\n{(bundle.get('defined_in') or {}).get('name','(unknown)')}\n\n"
                f"## Constraints (MUST obey)\n{_bullets(constraints)}\n\n"
                f"## Decisions (the why)\n{_bullets(decisions)}\n\n"
                f"## Cross-repo siblings (same_as)\n{_bullets(same_as, 3)}\n\n"
                f"## Tests (must keep passing)\n{_bullets(tests)}\n\n"
                "## Your task\n"
                "Return a unified diff (`--- a/...` / `+++ b/...` headers, "
                "@@ hunks). Keep changes minimal. If a constraint blocks the "
                "intent, say so and return an empty diff plus an explanation. "
                "Never violate a listed constraint."
            )
            try:
                draft = llm.generate(
                    prompt,
                    system=(
                        "You produce minimal unified diffs that honor stored "
                        "constraints. You do not reach for unrelated changes."
                    ),
                    max_tokens=int(body.get("max_tokens", 2000)),
                    temperature=0.2,
                    heavy=bool(body.get("heavy", True)),
                )
            except Exception as e:  # noqa: BLE001
                return {
                    "bundle": bundle,
                    "draft_diff": None,
                    "error": f"LLM generation failed: {type(e).__name__}: {e}",
                }
            # Audit — every regen is logged for review.
            engine.observe(
                kind="code_regenerate_requested",
                actor=Source.agent,
                payload={
                    "target": target,
                    "intent": intent[:400],
                    "primary_id": primary.get("id"),
                    "constraints_count": len(constraints),
                    "decisions_count": len(decisions),
                    "draft_len": len(draft or ""),
                    "llm_provider": getattr(llm, "name", "unknown"),
                },
            )
            return {
                "bundle": bundle,
                "draft_diff": draft,
                "llm_provider": getattr(llm, "name", "unknown"),
                "llm_heavy": bool(body.get("heavy", True)),
            }

        @app.get("/review/next-due", tags=["read"], dependencies=[Depends(check_auth)])
        def review_next_due(
            limit: int = 50,
            kinds: str | None = None,
        ) -> dict[str, Any]:
            """Spaced-repetition queue: concepts whose review window has
            elapsed since `last_confirmed_at`. Oldest-overdue first.
            Surface this in the UI as 'memory health' — clearing items
            confirms or revises them, calibrating confidence over time.
            `kinds` is a comma-separated NodeKind filter (e.g. 'fact,decision')."""
            kind_filter: list[NodeKind] | None = None
            if kinds:
                kind_filter = [NodeKind(k.strip()) for k in kinds.split(",") if k.strip()]
            items = engine.next_due_for_review(limit=limit, kinds=kind_filter)
            return {"items": [c.model_dump(mode="json") for c in items]}

        @app.get("/recall_bundle", tags=["query"], dependencies=[Depends(check_auth)])
        def recall_bundle_endpoint(
            target: str,
            max_neighbors: int = 8,
        ) -> dict[str, Any]:
            """Cognitive view per symbol/concept: hydrates the primary node
            with its file, callers/callees, decisions, constraints, tests,
            cross-repo siblings, and notes — all from typed-graph edges
            (NOT fuzzy/RAG). Designed to replace the recall+grep+blame
            loop with a single call. `target` is either a concept id or
            an exact name."""
            return engine.recall_bundle(target, max_neighbors_per_relation=max_neighbors)

        @app.get("/recall", tags=["query"], dependencies=[Depends(check_auth)])
        def recall(
            q: str,
            budget: int = 2000,
            kind: NodeKind | None = None,
            expand: int = 1,
            rerank: bool = False,
        ) -> dict[str, Any]:
            """OMP §4.1 recall verb.

            `rerank` (default False) controls whether the cross-encoder runs
            as a second-stage precision pass. Off-by-default for hot-path
            latency. AI editors should leave this off; UI 'precision' queries
            can opt-in with rerank=true.
            """
            return engine.recall(
                query=q, budget_tokens=budget, kind=kind, expand_hops=expand,
                rerank=rerank,
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
            """Curated catalog of installable upstream MCP servers.
            CatalogEntry is a @dataclass (not Pydantic), so we serialize via
            dataclasses.asdict — the previous .model_dump() call was the
            source of the 500 Internal Server Error this endpoint had been
            returning since 0.7."""
            from dataclasses import asdict
            from memex.upstreams.catalog import load_catalog
            return {"entries": [asdict(e) for e in load_catalog()]}

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

            Now also includes OMP §6 per-verb latency histograms under
            `latency` so callers can verify the implementation meets the
            spec's p99 targets on their workload.
            """
            base = _compute_stats(engine, window_hours=window_hours)
            base["latency"] = _latency_snapshot()
            return base

        @app.get("/stats/learning", tags=["meta"], dependencies=[Depends(check_auth)])
        def stats_learning(limit: int = 8) -> dict[str, Any]:
            """Distinct *learning* events for the dashboard timeline.

            Filters the episodic stream to events that represent durable
            learning — corrections captured, constraints/decisions added,
            cross-source links created, skills validated, consolidations
            run. Excludes high-volume noise (tool_pre/post, recall_executed,
            user_prompt) so the timeline reads like 'today memex learned X'.
            """
            learning_kinds = {
                "user_correction",
                "concept_added",
                "edge_added",
                "skill_validated",
                "consolidation_run",
                "policy_added",
                "policy_removed",
                "task_updated",
                "source_indexed",
            }
            # Pull recent events and filter; cap output at `limit` distinct
            # entries. We over-fetch (limit*8) to be sure we have enough
            # qualifying events even when the stream is noisy.
            raw = engine.episodic.recent(limit=max(limit * 8, 64))
            seen: set[tuple[str, str]] = set()
            out: list[dict[str, Any]] = []
            for ev in raw:
                if ev.kind not in learning_kinds:
                    continue
                # For concept_added, only show the kinds that mean "learning"
                # (constraint/decision/fact) — skip symbol/file/source which
                # are bulk-indexing noise.
                if ev.kind == "concept_added":
                    inner = (ev.payload or {}).get("kind", "")
                    if inner not in {"constraint", "decision", "fact",
                                     "approach", "pattern", "person", "task",
                                     "project", "milestone", "opinion",
                                     "rejected"}:
                        continue
                # Dedup near-duplicates: same (kind, name) within the window.
                payload = ev.payload or {}
                name = payload.get("name") or payload.get("id") or ""
                key = (ev.kind, name)
                if key in seen:
                    continue
                seen.add(key)
                out.append({
                    "timestamp": ev.timestamp.isoformat(),
                    "kind": ev.kind,
                    "actor": ev.actor.value,
                    "name": name,
                    "payload": payload,
                })
                if len(out) >= limit:
                    break
            return {"events": out, "limit": limit}

        @app.post("/maintenance/enrich", tags=["meta"], dependencies=[Depends(check_auth)])
        def enrich(body: dict[str, Any] | None = None) -> dict[str, Any]:
            """Pull facts from configured upstream MCPs into memeX's graph.
            Idempotent — re-runs upsert by external id. Pass `{"dry_run": true}`
            to preview counts without ingesting."""
            body = body or {}
            return engine.run_enrichment(dry_run=bool(body.get("dry_run", False)))

        @app.post("/maintenance/cleanup", tags=["meta"], dependencies=[Depends(check_auth)])
        def cleanup(body: dict[str, Any] | None = None) -> dict[str, Any]:
            """Active forgetting + episodic TTL prune. With body={\"dry_run\": true},
            counts candidates without mutating. Idempotent.
            """
            body = body or {}
            return engine.run_cleanup(
                forget_unused_days=body.get("forget_unused_days"),
                forget_min_confidence=body.get("forget_min_confidence"),
                forget_max_edges=body.get("forget_max_edges"),
                episodic_ttl_days=body.get("episodic_ttl_days"),
                episodic_keep_minimum=body.get("episodic_keep_minimum"),
                episodic_keep_kinds=body.get("episodic_keep_kinds"),
                dry_run=bool(body.get("dry_run", False)),
            )

        @app.get("/maintenance/eval-history", tags=["meta"], dependencies=[Depends(check_auth)])
        def eval_history(limit: int = 50) -> dict[str, Any]:
            """Return the last N AutoML eval records — the source-of-truth
            ML metric trail. Each entry has ts + precision_at_1 + MRR + pair
            counts; the dashboard plots precision_at_1 over time so users
            can SEE memex learning instead of taking it on faith.
            """
            import json as _json
            history_path = engine.settings.data_dir / "automl" / "eval-history.jsonl"
            if not history_path.exists():
                return {"records": [], "count": 0, "note": "no automl runs yet"}
            try:
                with history_path.open("r", encoding="utf-8") as f:
                    lines = f.readlines()
                records = []
                for line in lines[-limit:]:
                    line = line.strip()
                    if line:
                        records.append(_json.loads(line))
                return {"records": records, "count": len(records)}
            except Exception as e:  # noqa: BLE001
                return {"records": [], "count": 0, "error": str(e)}

        @app.post("/maintenance/eval", tags=["meta"], dependencies=[Depends(check_auth)])
        def retrieval_eval(body: dict[str, Any] | None = None) -> dict[str, Any]:
            """Real retrieval evaluation: precision@1, precision@5, MRR
            over a held-out split of mined episodic pairs. This is the
            'is the memory actually working' number — A/B-able across
            model swaps so retrains can prove they're better.
            """
            from memex.ml.finetune import mine_pairs as _mine
            from memex.ml.eval import evaluate_against_pairs
            body = body or {}
            pairs = _mine(
                engine,
                lookback_events=int(body.get("lookback_events", 50_000)),
                attribution_window_s=int(body.get("attribution_window_s", 600)),
            )
            return evaluate_against_pairs(
                engine,
                pairs,
                test_fraction=float(body.get("test_fraction", 0.2)),
                seed=int(body.get("seed", 42)),
                k_top=int(body.get("k_top", 20)),
                budget_tokens=int(body.get("budget_tokens", 800)),
            )

        @app.post("/maintenance/mine-pairs", tags=["meta"], dependencies=[Depends(check_auth)])
        def mine_pairs(body: dict[str, Any] | None = None) -> dict[str, Any]:
            """Mine (query, useful-result) pairs from the episodic stream
            for embedding fine-tuning. With body={\"write\": true}, persists
            JSONL to data_dir/training/pairs-<ts>.jsonl.

            Returns a summary including 'ready_to_train' which the
            dashboard surfaces as a learning-progress signal.
            """
            from memex.ml.finetune import mine_pairs as _mine, write_jsonl, summary
            from datetime import datetime as _dt
            body = body or {}
            pairs = _mine(
                engine,
                lookback_events=int(body.get("lookback_events", 50_000)),
                attribution_window_s=int(body.get("attribution_window_s", 600)),
            )
            out = summary(pairs)
            if body.get("write"):
                ts = _dt.now().strftime("%Y%m%dT%H%M%S")
                out_path = engine.settings.data_dir / "training" / f"pairs-{ts}.jsonl"
                n = write_jsonl(pairs, out_path)
                out["written_to"] = str(out_path)
                out["written_count"] = n
            return out

        @app.post("/maintenance/consolidate", tags=["meta"], dependencies=[Depends(check_auth)])
        def consolidate_endpoint(body: dict[str, Any] = Body(default={})) -> dict[str, Any]:
            """Run the consolidation pass — name-dedup + co-occurrence
            pattern promotion. Body (all optional):
              {"min_frequency": 3, "cooccurrence_window_events": 200, "dry_run": false}
            """
            from memex.core.lifecycle.consolidation import run_consolidation
            return run_consolidation(
                engine.episodic,
                engine.semantic,
                min_frequency=int(body.get("min_frequency", 3)),
                cooccurrence_window_events=int(body.get("cooccurrence_window_events", 200)),
                dry_run=bool(body.get("dry_run", False)),
            )

        @app.post("/maintenance/episodic-rollup", tags=["meta"], dependencies=[Depends(check_auth)])
        def episodic_rollup_endpoint(body: dict[str, Any] = Body(default={})) -> dict[str, Any]:
            """Collapse contiguous edge_added / concept_added / concept_deleted
            event runs into single batch events. Body (all optional):
              {"min_run": 50, "window_seconds": 600, "dry_run": false}
            Default dry_run=False once delete_by_id is implemented; until then
            the rollup emits the batch event but cannot reclaim space."""
            from memex.core.lifecycle.episodic_rollup import run_episodic_rollup
            return run_episodic_rollup(
                engine.episodic,
                min_run=int(body.get("min_run", 50)),
                window_seconds=int(body.get("window_seconds", 600)),
                dry_run=bool(body.get("dry_run", False)),
            )

        @app.get("/maintenance/vss-status", tags=["meta"], dependencies=[Depends(check_auth)])
        def vss_status() -> dict[str, Any]:
            """Vector index posture + HNSW upgrade advisory. Powers the
            'plan an index build' CTA on the dashboard once the user
            crosses the recommended-HNSW threshold."""
            return engine.vss_status()

        def _do_check_action(body: dict[str, Any]) -> dict[str, Any]:
            intent = (body or {}).get("intent", "")
            if not intent:
                return {
                    "decision": "allow",
                    "reasons": [],
                    "query": "",
                    "checked": 0,
                    "top_confidence": 0.0,
                    "project": (body or {}).get("project"),
                    "note": "empty intent — no check performed",
                }
            return engine.check_action(
                intent=intent,
                budget_tokens=int((body or {}).get("budget_tokens", 800)),
                deny_threshold=float((body or {}).get("deny_threshold", 0.85)),
                step_up_threshold=float((body or {}).get("step_up_threshold", 0.55)),
                project=(body or {}).get("project"),
            )

        @app.post("/check_action", tags=["meta"], dependencies=[Depends(check_auth)])
        def check_action(body: dict[str, Any]) -> dict[str, Any]:
            """Validate-before-act gate. Returns {decision: allow|step_up|deny,
            reasons: [...]} given an `intent` string. Designed to be called
            from a Claude Code PreToolUse hook.

            Memex-native name; see /validate_action and /omp/validate for the
            OMP §4.5 conformant aliases.
            """
            return _do_check_action(body)

        @app.post("/validate_action", tags=["meta"], dependencies=[Depends(check_auth)])
        def validate_action(body: dict[str, Any]) -> dict[str, Any]:
            """OMP §4.5 `validate` verb — action gate. Identical semantics to
            /check_action; this name matches the open Open Memory Protocol
            spec so federation clients can call it generically.
            """
            return _do_check_action(body)

        @app.post("/omp/validate", tags=["mmp"], dependencies=[Depends(check_auth)])
        def mmp_validate(body: dict[str, Any]) -> dict[str, Any]:
            """OMP §4.5 strict — same as /validate_action; lives under the
            /mmp namespace for clients that want explicit protocol versioning."""
            return _do_check_action(body)

        @app.post("/remember", tags=["write"], dependencies=[Depends(check_auth)])
        def remember(body: AddNodeBody = Body(...)) -> dict[str, Any]:
            """OMP §4.2 `remember` verb. Idempotent on (name, kind, source):
            re-calling with the same triple updates `last_confirmed_at`,
            merges metadata, and — when the description has changed —
            preserves the prior version in metadata.previous_descriptions
            (most-recent first, capped at 5 entries) and emits a
            `concept_revised` event for audit. This is the lightweight
            memory-hygiene primitive — full supersession with a new node
            + supersedes edge is /supersede (separate endpoint)."""
            from memex.core.schema import Concept as _Concept, EpisodicEvent as _Ev, Source as _Src
            from datetime import datetime as _dt, timezone as _tz
            payload = body.model_dump()
            existing = engine.find_by_name_kind_source(
                name=payload["name"],
                kind=payload.get("kind"),
                source=payload.get("source"),
            )
            if existing is not None:
                merged_md = dict(existing.metadata or {})
                merged_md.update(payload.get("metadata") or {})
                new_desc = payload.get("description") or existing.description
                revised = (new_desc or "").strip() != (existing.description or "").strip()
                if revised:
                    prev_list = list(merged_md.get("previous_descriptions") or [])
                    prev_list.insert(0, {
                        "description": existing.description,
                        "confidence_at_revision": float(existing.confidence),
                        "revised_at": _dt.now(_tz.utc).isoformat(),
                    })
                    merged_md["previous_descriptions"] = prev_list[:5]
                updated = _Concept(
                    id=existing.id,
                    name=existing.name,
                    description=new_desc,
                    kind=existing.kind,
                    source=existing.source,
                    confidence=float(payload.get("confidence", existing.confidence)),
                    created_at=existing.created_at,
                    last_confirmed_at=_dt.now(_tz.utc),
                    metadata=merged_md,
                    verification=payload.get("verification") or existing.verification,
                )
                engine.semantic.add_concept(updated)
                if revised:
                    engine.episodic.append(_Ev(
                        kind="concept_revised",
                        actor=_Src.agent,
                        payload={
                            "id": existing.id,
                            "name": existing.name,
                            "prev_len": len(existing.description or ""),
                            "new_len": len(new_desc or ""),
                        },
                    ))
                return updated.model_dump(mode="json")
            c = engine.add(**payload)
            return c.model_dump(mode="json")

        @app.post("/omp/remember", tags=["mmp"], dependencies=[Depends(check_auth)])
        def mmp_remember(body: AddNodeBody = Body(...)) -> dict[str, Any]:
            return remember(body)

        @app.post("/promote", tags=["meta"], dependencies=[Depends(check_auth)])
        def promote(
            min_support: int = 3,
            lookback_events: int = 1000,
        ) -> dict[str, Any]:
            """Run the consolidation promoter — episodic patterns
            (corrections / auto-approvals) become durable constraint /
            decision nodes when they recur >= min_support times.

            Idempotent: re-running won't duplicate previously-promoted
            constraints. Returns counts of promotions emitted.
            """
            return engine.promote_patterns(
                min_support=min_support,
                lookback_events=lookback_events,
            )

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

        def check(
            request: Request,
            authorization: Annotated[str | None, Header()] = None,
        ) -> None:
            # Loopback callers (the local UI, CLI, MCP clients on the same
            # box) skip auth entirely — they already have full filesystem
            # privilege. The token only gates non-loopback callers, which
            # the constructor refuses to allow without one being set.
            client_host = request.client.host if request.client else None
            if client_host in {"127.0.0.1", "::1", "localhost"}:
                return
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
        # Prime the doctor cache on startup so the user's first /app/home
        # hit is fast (~50ms instead of ~400ms with cold doctor).
        try:
            for route in self.app.routes:
                if getattr(route, "path", None) == "/doctor":
                    route.endpoint()  # type: ignore[attr-defined]
                    break
        except Exception:  # noqa: BLE001
            pass
        # Pre-warm the embedding model in a background thread. First
        # recall would otherwise take ~12s (fastembed ONNX cold-load
        # + reranker cold-load). We fire-and-forget so daemon comes up
        # immediately; first recall just blocks on whichever model
        # finished loading first.
        try:
            import threading as _thr
            def _warm() -> None:
                try:
                    self.engine.embedding_provider.embed(
                        "warmup", is_query=True,
                    )
                    log.info("embed model warmed")
                except Exception as e:  # noqa: BLE001
                    log.info("embed warmup skipped: %s", e)
                try:
                    self.engine.recall(query="warmup", budget_tokens=200)
                    log.info("reranker warmed")
                except Exception as e:  # noqa: BLE001
                    log.info("reranker warmup skipped: %s", e)
            _thr.Thread(target=_warm, daemon=True, name="ml-warmup").start()
        except Exception:  # noqa: BLE001
            pass
        # Start auto-maintenance scheduler. Disable with MEMEX_NO_AUTO_MAINT=1.
        try:
            from memex.scheduler import MaintenanceScheduler
            self._scheduler = MaintenanceScheduler(self.engine)
            self._scheduler.start()
        except Exception as e:  # noqa: BLE001
            log.warning("scheduler failed to start: %s", e)
        uvicorn.run(self.app, host=self.host, port=self.port, log_level="info")


def run_http(
    host: str = "127.0.0.1",
    port: int = 7777,
    auth_token: str | None = None,
    engine: Engine | None = None,
) -> None:
    """Convenience entry: build engine if not provided, then serve.

    On startup we resolve the effective auth token via runtime_state:

    - explicit ``auth_token`` arg wins (CLI ``--auth-token``)
    - else honor ``MEMEX_AUTH_TOKEN`` from settings
    - else read or mint ``<data_dir>/daemon.token`` (mode 0600)

    The resolved token is also mirrored to the data dir so the desktop
    app and other local clients can discover it without an env var.
    The listening URL is published to ``<data_dir>/daemon.url`` for
    the same reason. See PRIVACY.md and SECURITY.md for the trust model.
    """
    from memex.config import get_settings
    from memex.runtime_state import bootstrap_auth_token, write_daemon_url

    engine = engine or Engine.build_default()

    settings = get_settings()
    if auth_token is not None:
        # Mirror explicit override into settings so bootstrap persists it.
        settings.auth_token = auth_token
    resolved_token = bootstrap_auth_token(settings)
    public_host = "127.0.0.1" if host == "0.0.0.0" else host
    write_daemon_url(settings, f"http://{public_host}:{port}")

    # Background scheduler — runs the consolidation promoter periodically
    # so episodic patterns get promoted to durable constraint/decision
    # nodes without requiring an external cron. Daemon-thread so it dies
    # with the daemon; no graceful shutdown needed.
    if settings.promote_interval_seconds > 0:
        _start_promote_scheduler(engine, settings)
    # AutoML scheduler — periodic pair mining + retrieval evaluation +
    # JSONL writeout. The trainer is launched out-of-band (subprocess) so
    # torch isn't a hard daemon dependency.
    if settings.automl_interval_seconds > 0:
        _start_automl_scheduler(engine, settings)
    # Cleanup scheduler — daily forgetting + episodic TTL prune so the
    # DuckDB doesn't grow unbounded as memex accumulates years of
    # signal. Protected kinds (constraint/decision/project/source/person)
    # never auto-delete.
    if settings.cleanup_interval_seconds > 0:
        _start_cleanup_scheduler(engine, settings)
    # Autonomous discovery worker — opt-in via MEMEX_DISCOVERY=1. Disabled
    # by default so OSS users don't get surprise network calls. When on,
    # the worker scans recent low-recall events and uses the LLM hook +
    # upstream MCPs (when configured) to fetch context.
    import os as _os
    if _os.environ.get("MEMEX_DISCOVERY", "").lower() in {"1", "true", "yes"}:
        _start_discovery_scheduler(engine, settings)

    frontend = HTTPFrontend(engine=engine, host=host, port=port, auth_token=resolved_token)
    frontend.run()


def _start_promote_scheduler(engine: Engine, settings: Any) -> None:
    """Spawn a daemon thread that runs `engine.promote_patterns` on an
    interval. First run is delayed by a full interval so the daemon has
    time to come up and accept traffic before doing housekeeping work.
    """
    import threading
    log = logging.getLogger(__name__)

    def _loop() -> None:
        # Delay first run so the daemon's startup costs (BGE model load
        # on first recall, etc.) don't collide with the promoter's
        # recall calls inside check_action / pattern detection.
        time.sleep(settings.promote_interval_seconds)
        while True:
            try:
                result = engine.promote_patterns(
                    min_support=settings.promote_min_support,
                    lookback_events=settings.promote_lookback_events,
                )
                total = (
                    result.get("correction_promotions", 0)
                    + result.get("approval_promotions", 0)
                )
                if total > 0:
                    log.info(
                        "promote_scheduler: emitted %d promotions (%s)",
                        total, result,
                    )
            except Exception as e:  # noqa: BLE001
                log.warning("promote_scheduler: pass failed: %s", e)
            time.sleep(settings.promote_interval_seconds)

    t = threading.Thread(
        target=_loop,
        name="memex-promote-scheduler",
        daemon=True,
    )
    t.start()
    log.info(
        "promote_scheduler started: interval=%ds, min_support=%d",
        settings.promote_interval_seconds, settings.promote_min_support,
    )


def _start_automl_scheduler(engine: Engine, settings: Any) -> None:
    """Periodic auto-ML pass: mine pairs, evaluate retrieval, write JSONL.

    Persists each eval result as a JSON line in
    `data_dir/automl/eval-history.jsonl` so the dashboard can render
    precision/MRR over time as a real ML metric, not a self-claim.

    Training itself (sentence-transformers MNR-loss) runs as a separate
    subprocess so torch stays out of the daemon's import path. The
    scheduler emits a `train_recommended` event when ready_to_train
    crosses threshold; an external trainer (or future in-process
    invocation when extras installed) picks that up.
    """
    import json as _json
    import threading
    log = logging.getLogger(__name__)
    history_path = settings.data_dir / "automl" / "eval-history.jsonl"

    def _persist_eval(record: dict[str, Any]) -> None:
        history_path.parent.mkdir(parents=True, exist_ok=True)
        with history_path.open("a", encoding="utf-8") as f:
            f.write(_json.dumps(record, ensure_ascii=False) + "\n")

    def _last_p1() -> float | None:
        if not history_path.exists():
            return None
        try:
            with history_path.open("r", encoding="utf-8") as f:
                lines = f.readlines()
            for line in reversed(lines):
                rec = _json.loads(line)
                if "precision_at_1" in rec:
                    return float(rec["precision_at_1"])
        except Exception:  # noqa: BLE001
            return None
        return None

    def _loop() -> None:
        # Delay first run so it doesn't collide with the daemon's BGE
        # model load on the first real recall.
        time.sleep(settings.automl_interval_seconds)
        while True:
            try:
                from memex.ml.finetune import mine_pairs, write_jsonl, summary
                from memex.ml.eval import evaluate_against_pairs
                from datetime import datetime as _dt

                pairs = mine_pairs(engine)
                pairs_summary = summary(pairs)
                record: dict[str, Any] = {
                    "ts": _dt.now().isoformat(),
                    "pairs": pairs_summary,
                }

                if pairs_summary["total_pairs"] >= settings.automl_min_pairs_to_eval:
                    eval_result = evaluate_against_pairs(
                        engine,
                        pairs,
                        test_fraction=settings.automl_test_fraction,
                    )
                    record["eval"] = eval_result
                    prev = _last_p1()
                    if prev is not None:
                        delta = eval_result["precision_at_1"] - prev
                        record["p1_delta"] = round(delta, 4)
                        if abs(delta) >= settings.automl_significant_delta:
                            engine.observe(
                                kind="ml_significant_delta",
                                payload={
                                    "delta": delta,
                                    "p1_now": eval_result["precision_at_1"],
                                    "p1_prev": prev,
                                },
                            )

                # Auto-write JSONL when training is worth running.
                if pairs_summary["total_pairs"] >= settings.automl_min_pairs_to_train:
                    ts_label = _dt.now().strftime("%Y%m%dT%H%M%S")
                    out_path = settings.data_dir / "training" / f"pairs-{ts_label}.jsonl"
                    n_written = write_jsonl(pairs, out_path)
                    record["training_jsonl"] = {
                        "path": str(out_path),
                        "count": n_written,
                    }
                    engine.observe(
                        kind="train_recommended",
                        payload={
                            "pairs_path": str(out_path),
                            "pair_count": n_written,
                        },
                    )

                _persist_eval(record)
                log.info("automl pass: pairs=%d eval=%s",
                         pairs_summary["total_pairs"],
                         record.get("eval", {}).get("precision_at_1"))
            except Exception as e:  # noqa: BLE001
                log.warning("automl_scheduler: pass failed: %s", e)
            time.sleep(settings.automl_interval_seconds)

    t = threading.Thread(
        target=_loop,
        name="memex-automl-scheduler",
        daemon=True,
    )
    t.start()
    log.info(
        "automl_scheduler started: interval=%ds, min_eval=%d, min_train=%d",
        settings.automl_interval_seconds,
        settings.automl_min_pairs_to_eval,
        settings.automl_min_pairs_to_train,
    )


def _start_discovery_scheduler(engine: Engine, settings: Any) -> None:
    """Autonomous discovery worker.

    Periodically scans recent low-yield recall events (`recalled_ids` was
    empty, `degraded=True`, or the user_correction stream shows a gap)
    and, if an LLM is configured AND upstream MCPs are installed, uses
    the LLM to choose a tool to fetch context. Results land as kind=fact
    nodes via /remember (idempotent on call hash, same as auto-promote).

    Disabled by default. Activate with MEMEX_DISCOVERY=1.

    Failure-tolerant: if the LLM is unavailable or no upstreams are
    installed, the worker logs once and sleeps. No retries, no thrash.
    """
    import threading
    log = logging.getLogger(__name__)

    def _loop() -> None:
        time.sleep(120)  # 2-minute warmup so we don't fire mid-startup
        cycle = 0
        while True:
            cycle += 1
            try:
                llm = getattr(engine, "llm", None)
                if llm is None or not llm.is_available():
                    log.debug("discovery: no LLM configured, idle cycle %d", cycle)
                    time.sleep(600)  # 10-min idle pause
                    continue
                # Scan last ~50 recall events for empty/degraded ones.
                recent = list(engine.episodic.recent(limit=50))
                gaps: list[str] = []
                for ev in recent:
                    if ev.kind != "recall_executed":
                        continue
                    p = ev.payload or {}
                    if p.get("n_nodes", 0) > 0 and not p.get("degraded"):
                        continue
                    q = (p.get("query") or "").strip()
                    if q and q not in gaps:
                        gaps.append(q)
                    if len(gaps) >= 5:
                        break
                if not gaps:
                    time.sleep(900)
                    continue
                # Ask the LLM what (if anything) it would research about
                # these gaps. Keep prompt short — heavy=False so we use
                # the cheap model tier.
                prompt = (
                    "You are a memory-helper for a coding AI. Below are "
                    "recent queries that produced no useful results. For "
                    "each, respond with one short suggestion of an "
                    "upstream tool to call OR `SKIP` if it's not worth "
                    "investigating. One line per query.\n\n"
                    + "\n".join(f"- {g}" for g in gaps[:5])
                )
                try:
                    suggestions = llm.generate(
                        prompt, max_tokens=300, temperature=0.1,
                    )
                except Exception as e:  # noqa: BLE001
                    log.warning("discovery: LLM call failed: %s", e)
                    suggestions = ""
                if suggestions:
                    # Save the LLM's suggestions as an ephemeral concept
                    # so the user can see what discovery proposed. Real
                    # tool dispatch (calling the suggested upstream) is
                    # v0.7 — we don't auto-execute side-effecting MCP
                    # tools without explicit user opt-in beyond the env var.
                    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
                    expires = (_dt.now(_tz.utc) + _td(hours=24)).isoformat()
                    engine.add(
                        name=f"discovery suggestions {_dt.now(_tz.utc):%Y-%m-%dT%H:%M}",
                        description=suggestions,
                        kind="ephemeral",
                        source="system",
                        confidence=0.6,
                        metadata={
                            "expires_at": expires,
                            "gap_queries": gaps[:5],
                            "discovery_cycle": cycle,
                        },
                    )
                    engine.observe(
                        kind="discovery_proposed",
                        actor="system",
                        payload={"gaps": gaps[:5], "summary": suggestions[:400]},
                    )
            except Exception as e:  # noqa: BLE001
                log.warning("discovery: cycle %d failed: %s", cycle, e)
            time.sleep(1800)  # 30-min between cycles

    t = threading.Thread(target=_loop, name="memex-discovery", daemon=True)
    t.start()
    log.info("discovery scheduler started (MEMEX_DISCOVERY=1)")


def _start_cleanup_scheduler(engine: Engine, settings: Any) -> None:
    """Periodic cleanup pass: forget stale-orphaned concepts + prune
    expired episodic events. Daemon thread; dies with the daemon."""
    import threading
    log = logging.getLogger(__name__)

    def _loop() -> None:
        # Big initial delay (1 hour) so cleanup never runs in the first
        # minutes after startup — those are when the user is most likely
        # actively touching concepts and least wants the DB hit.
        time.sleep(3600)
        while True:
            try:
                result = engine.run_cleanup()
                if result.get("forgotten_concepts", 0) or result.get("pruned_events", 0):
                    log.info(
                        "cleanup_scheduler: forgot=%d events_pruned=%d",
                        result["forgotten_concepts"],
                        result["pruned_events"],
                    )
            except Exception as e:  # noqa: BLE001
                log.warning("cleanup_scheduler: pass failed: %s", e)
            time.sleep(settings.cleanup_interval_seconds)

    t = threading.Thread(
        target=_loop,
        name="memex-cleanup-scheduler",
        daemon=True,
    )
    t.start()
    log.info(
        "cleanup_scheduler started: interval=%ds, episodic_ttl=%dd",
        settings.cleanup_interval_seconds,
        settings.episodic_ttl_days,
    )
