"""
memex CLI — typer-based, designed to be friendly on first run.

Subcommands:
  memex add NAME [--description] [--kind] [--source]
  memex recall QUERY [--budget] [--kind]
  memex link FROM_ID TO_ID --kind
  memex validate SKILL [--actor]
  memex progress [--actor]
  memex install skill:NAME | model:embed
  memex serve                  # MCP stdio (for editor pipes)
  memex daemon [--listen]      # long-running HTTP server
  memex doctor                 # diagnostics
  memex stats
  memex list                   # list installed skill bundles
  memex version
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from memex import __version__
from memex.config import get_settings
from memex.core.engine import Engine
from memex.core.schema import EdgeKind, NodeKind, Source

app = typer.Typer(
    name="memex",
    help="Persistent cognitive memory for AI coding tools.",
    no_args_is_help=True,
    add_completion=False,
)

console = Console()
err_console = Console(stderr=True)


def _setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )


def _engine() -> Engine:
    return Engine.build_default()


# ---- write commands -----------------------------------------------------


@app.command()
def add(
    name: Annotated[str, typer.Argument(help="Short, memorable name for the concept.")],
    description: Annotated[str, typer.Option("--description", "-d", help="Longer body.")] = "",
    kind: Annotated[
        NodeKind, typer.Option("--kind", "-k", case_sensitive=False)
    ] = NodeKind.fact,
    source: Annotated[
        Source, typer.Option("--source", "-s", case_sensitive=False)
    ] = Source.human,
    confidence: Annotated[float, typer.Option("--confidence", "-c", min=0.0, max=1.0)] = 1.0,
) -> None:
    """Add a concept to the memex graph."""
    eng = _engine()
    c = eng.add(
        name=name,
        description=description,
        kind=kind,
        source=source,
        confidence=confidence,
    )
    console.print(f"[green]added[/green] {c.id}  [dim]{c.kind.value}[/dim]  {c.name}")


@app.command()
def recall(
    query: Annotated[str, typer.Argument(help="What to look up.")],
    budget: Annotated[int, typer.Option("--budget", "-b", help="Token budget.")] = 2000,
    kind: Annotated[NodeKind | None, typer.Option("--kind", "-k", case_sensitive=False)] = None,
    expand: Annotated[int, typer.Option("--expand", "-e", help="Graph expand hops.")] = 1,
    json_out: Annotated[bool, typer.Option("--json", help="Emit JSON instead of a table.")] = False,
) -> None:
    """Retrieve a budget-bounded subgraph that matches the query."""
    eng = _engine()
    result = eng.recall(query=query, budget_tokens=budget, kind=kind, expand_hops=expand)

    if json_out:
        import json as _json

        console.print_json(_json.dumps(result.to_dict()))
        return

    if not result.nodes:
        err_console.print(f"[yellow]no matches[/yellow] for: {query}")
        return

    table = Table(title=f"recall: {query}", show_lines=False)
    table.add_column("id", style="dim")
    table.add_column("kind", style="cyan")
    table.add_column("name", style="bold")
    table.add_column("conf", style="green", justify="right")
    table.add_column("source", style="magenta")
    for n in result.nodes:
        table.add_row(
            n.id,
            n.kind.value,
            n.name,
            f"{n.confidence:.2f}",
            n.source.value,
        )
    console.print(table)
    console.print(
        f"[dim]{len(result.nodes)} nodes, {len(result.edges)} edges, "
        f"~{result.tokens_used} tokens, strategy={result.strategy}[/dim]"
    )


@app.command()
def link(
    from_id: Annotated[str, typer.Argument(help="Source concept id.")],
    to_id: Annotated[str, typer.Argument(help="Target concept id.")],
    kind: Annotated[
        EdgeKind, typer.Option("--kind", "-k", case_sensitive=False)
    ] = EdgeKind.relates_to,
    source: Annotated[Source, typer.Option("--source", "-s")] = Source.human,
) -> None:
    """Link two concepts with a typed edge."""
    eng = _engine()
    eng.link(from_id=from_id, to_id=to_id, kind=kind, source=source)
    console.print(f"[green]linked[/green] {from_id} -[{kind.value}]-> {to_id}")


# ---- skill validation ---------------------------------------------


@app.command()
def validate(
    skill: Annotated[str, typer.Argument(help="Skill name (e.g. secure-subprocess).")],
    actor: Annotated[Source, typer.Option("--actor", "-a")] = Source.agent,
) -> None:
    """Fetch the structured approach + checks for a skill (auto-recorded)."""
    eng = _engine()
    result = eng.validate(skill, actor=actor)
    if not result.get("ok"):
        err_console.print(f"[red]not found[/red]: {result.get('error', skill)}")
        raise typer.Exit(code=1)

    console.print(f"\n[bold]{result['skill']}[/bold]  [dim]({result['kind']})[/dim]")
    if result.get("description"):
        console.print(f"[italic]{result['description']}[/italic]\n")
    if result.get("approach"):
        console.print("[bold cyan]Approach[/bold cyan]")
        console.print(result["approach"], "\n")
    if result.get("checks"):
        console.print("[bold cyan]Checks[/bold cyan]")
        for chk in result["checks"]:
            console.print(f"  [green]▢[/green] {chk}")
    if result.get("triggers"):
        console.print(
            f"\n[dim]triggers: {', '.join(result['triggers'])}[/dim]"
        )


@app.command()
def progress(
    actor: Annotated[Source | None, typer.Option("--actor", "-a")] = None,
) -> None:
    """Summary of skills validated, concepts added, events recorded."""
    eng = _engine()
    p = eng.progress(actor=actor)
    table = Table(show_header=False, box=None)
    table.add_row("[dim]validations[/dim]", str(p["validations_count"]))
    table.add_row(
        "[dim]validated skills[/dim]",
        ", ".join(p["validated_skills"]) or "[dim]none[/dim]",
    )
    table.add_row("[dim]concepts total[/dim]", str(p["concepts_total"]))
    table.add_row("[dim]events total[/dim]", str(p["events_total"]))
    if p.get("actor_filter"):
        table.add_row("[dim]actor filter[/dim]", p["actor_filter"])
    console.print(table)


# ---- install -----------------------------------------------------------


@app.command(name="install")
def install_target(
    target: Annotated[str, typer.Argument(help="skill:NAME or model:embed")],
) -> None:
    """Install a skill bundle or an ML tier."""
    if ":" not in target:
        err_console.print(
            "[red]invalid target[/red] — expected `skill:NAME` or `model:embed`"
        )
        raise typer.Exit(code=2)

    kind, _, name = target.partition(":")
    if kind == "skill":
        from memex.skills import find_builtin_skill, install_skill

        skill = find_builtin_skill(name)
        if skill is None:
            err_console.print(
                f"[red]skill `{name}` not found[/red]. Builtin skills:"
            )
            list_skills()
            raise typer.Exit(code=1)
        eng = _engine()
        counts = install_skill(eng, skill)
        console.print(
            f"[green]installed[/green] skill [bold]{skill.name}[/bold]@{skill.version}: "
            f"{counts['concepts']} concepts, {counts['edges']} edges"
        )
    elif kind == "model":
        if name == "embed":
            console.print(
                "[yellow]Tier 1 (embeddings) requires the `embed` extra.[/yellow]\n"
                "  Run: [bold]pipx install --force 'memex[embed]'[/bold]\n"
                "  Or:  [bold]uv tool install --reinstall 'memex[embed]'[/bold]\n\n"
                "After install, the embedding model is downloaded lazily on first recall."
            )
        else:
            err_console.print(f"[red]unknown model `{name}`[/red] — known: embed")
            raise typer.Exit(code=2)
    else:
        err_console.print(
            f"[red]unknown install kind `{kind}`[/red] — expected `skill:NAME` or `model:embed`"
        )
        raise typer.Exit(code=2)


@app.command(name="install-from-path")
def install_from_local_path(
    path: Annotated[Path, typer.Argument(help="Path to a skill bundle directory.")],
) -> None:
    """Install a skill bundle from a local filesystem directory."""
    from memex.skills import install_from_path

    eng = _engine()
    counts = install_from_path(eng, path)
    console.print(f"[green]installed[/green] skill from {path}: {counts}")


@app.command()
def bootstrap(
    path: Annotated[Path, typer.Argument(help="Repo or directory root to scan.")] = Path("."),
) -> None:
    """Auto-discover and install skill bundles from a repo's .memex/skills/ + *.memex.{json,jsonl}."""
    from memex.skills import bootstrap as do_bootstrap

    eng = _engine()
    result = do_bootstrap(eng, path)
    if not result.skills_installed and not result.skills_skipped:
        console.print(f"[dim]no skill bundles found under {path.resolve()}[/dim]")
        return
    if result.skills_installed:
        console.print(
            f"[green]installed[/green] {len(result.skills_installed)} skill(s) "
            f"({result.concepts_added} concepts):"
        )
        for name in result.skills_installed:
            console.print(f"  [bold]{name}[/bold]")
    if result.skills_skipped:
        console.print(
            f"[dim]skipped {len(result.skills_skipped)} already-installed skill(s):[/dim]"
        )
        for name in result.skills_skipped:
            console.print(f"  [dim]{name}[/dim]")


# ---- list / stats / doctor ---------------------------------------------


@app.command(name="list")
def list_skills() -> None:
    """List builtin skill bundles."""
    from memex.skills import list_builtin_skills

    skills = list_builtin_skills()
    if not skills:
        console.print("[dim]no builtin skills found[/dim]")
        return
    table = Table(title="builtin skills")
    table.add_column("name", style="bold")
    table.add_column("version", style="dim")
    table.add_column("description")
    for skill in skills:
        table.add_row(skill.name, skill.version, skill.description)
    console.print(table)


@app.command()
def stats() -> None:
    """Show storage stats and tier status."""
    eng = _engine()
    s = eng.stats()
    table = Table(show_header=False, box=None)
    for k, v in s.items():
        table.add_row(f"[dim]{k}[/dim]", str(v))
    console.print(table)


@app.command()
def setup(
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Wire up every detected tool without prompting.")] = False,
    only: Annotated[str | None, typer.Option("--only", help="Wire only this integration (Claude Code, Cursor, Windsurf, Cline).")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show what would change without writing.")] = False,
) -> None:
    """Detect installed AI tools and wire memex into each — cross-tool memory in one shot.

    Without flags, asks before each tool. With --yes, wires every detected
    tool. With --only "<name>", wires just one.
    """
    from memex.integrations import SUPPORTED, detect_all, memex_mcp_block

    statuses = detect_all()

    # Header summary
    console.print("\n[bold]memex setup[/bold] — auto-wire memex into your AI tools\n")
    table = Table(show_header=True, box=None)
    table.add_column("AI tool", style="bold")
    table.add_column("config path", style="dim")
    table.add_column("memex wired?")
    for s in statuses:
        if not s.tool_installed:
            wired = "[dim]not installed[/dim]"
        elif s.memex_present:
            wired = "[green]yes[/green]"
        elif s.config_exists:
            wired = "[yellow]no — config exists[/yellow]"
        else:
            wired = "[yellow]no — no config yet[/yellow]"
        table.add_row(s.name, str(s.config_path), wired)
    console.print(table)
    console.print("")

    # What we're about to write
    block = memex_mcp_block()
    console.print("[dim]The block we'll write into each tool's mcpServers:[/dim]")
    console.print(f"  [bold]command:[/bold] {block['command']}")
    console.print(f"  [bold]args:[/bold]    {block['args']}\n")

    # Pick targets
    targets: list = []
    for t, s in zip(SUPPORTED, statuses):
        if only and t.name != only:
            continue
        if not s.tool_installed:
            continue
        if s.memex_present:
            console.print(f"[dim]skipping {t.name}[/dim] — already wired")
            continue
        if yes:
            targets.append(t)
        else:
            ok = typer.confirm(f"Wire memex into {t.name}?", default=True)
            if ok:
                targets.append(t)

    if not targets:
        console.print("\n[dim]Nothing to do. All detected tools either skipped or already wired.[/dim]")
        return

    # Execute
    console.print("")
    for t in targets:
        if dry_run:
            console.print(f"[cyan]would wire[/cyan] {t.name} -> {t.config_path()}")
            continue
        result = t.wire()
        if result.error:
            err_console.print(f"[red]{result.name}:[/red] {result.error}")
            continue
        if result.action == "added":
            console.print(f"[green]added[/green] memex to {result.name} -> {result.config_path}")
        elif result.action == "updated":
            console.print(f"[green]updated[/green] memex in {result.name} -> {result.config_path}")
        elif result.action == "already-up-to-date":
            console.print(f"[dim]{result.name}: already up-to-date[/dim]")

    if not dry_run:
        console.print(
            "\n[bold]Restart your AI tool(s)[/bold] to pick up the new MCP server.\n"
            "Then try: [bold]memex recall \"what do you know about my project\"[/bold] from inside any wired tool."
        )


@app.command()
def doctor() -> None:
    """Diagnose installation, paths, and tier availability."""
    settings = get_settings()
    settings.ensure_dirs()
    from memex.ml.embeddings import build_default_provider

    provider = build_default_provider(model_name=settings.embed_model, dim=settings.embed_dim)
    embed_ok = provider.is_available()
    embed_diag = _diagnose_embed_tier()

    table = Table(title="memex doctor", show_header=False, box=None)
    table.add_row("memex version", __version__)
    table.add_row("python", sys.version.split()[0])
    table.add_row("data_dir", str(settings.data_dir))
    table.add_row(
        "data_dir writable", "yes" if _is_writable(settings.data_dir) else "[red]NO[/red]"
    )
    table.add_row("listen", settings.listen)
    table.add_row(
        "embed tier",
        "[green]installed[/green]" if embed_ok else "[red]NOT WORKING[/red]",
    )
    table.add_row("embed model", settings.embed_model)
    if not embed_ok and embed_diag:
        table.add_row("embed reason", f"[yellow]{embed_diag}[/yellow]")

    # Upstream gateway status
    from memex.upstreams import load_upstreams
    cfg = load_upstreams()
    table.add_row("gateway upstreams", str(len(cfg.upstreams)))
    console.print(table)

    if cfg.upstreams:
        ut = Table(title="upstreams", show_lines=False)
        ut.add_column("name", style="bold")
        ut.add_column("type", style="cyan")
        ut.add_column("target", style="dim")
        ut.add_column("status")
        for u in cfg.upstreams:
            if u.type == "http":
                target = u.url or "—"
            else:
                target = f"{u.command or ''} {' '.join(u.args)}".strip() or "—"
            ut.add_row(u.name, u.type, target, "[dim]not tested[/dim]")
        console.print(ut)
        console.print("[dim]Run [bold]memex upstream test <name>[/bold] to verify a connection.[/dim]")
    else:
        console.print(
            "\n[dim]No upstream MCP servers configured. Browse the catalog with:[/dim]\n"
            "  [bold]memex upstream catalog[/bold]"
        )

    if not embed_ok:
        console.print(
            "\n[dim]Tip: install Tier 1 embeddings for vector search:[/dim]\n"
            "  [bold]pipx install --force 'memex[embed]'[/bold]"
        )


def _diagnose_embed_tier() -> str | None:
    """Return a short reason string when the embed tier isn't working.

    Honest diagnosis beats a generic 'not installed': missing module vs
    onnxruntime DLL failure vs model download failure are very different
    fixes.
    """
    try:
        import fastembed  # noqa: F401
    except ImportError:
        return "fastembed package not installed (pip install memex[embed])"
    except OSError as e:  # native lib load failure (Windows DLL etc.)
        return f"native dependency failed to load: {e}"
    try:
        import onnxruntime  # noqa: F401
    except ImportError:
        return "onnxruntime not installed (transitive dep of fastembed)"
    except OSError as e:
        return f"onnxruntime native lib failed: {e} — install Microsoft VC++ redistributable"
    return None


def _is_writable(path: Path) -> bool:
    try:
        probe = path / ".write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return True
    except OSError:
        return False


# ---- frontends ---------------------------------------------------------


def _maybe_auto_bootstrap() -> None:
    """If MEMEX_AUTO_BOOTSTRAP=true, scan cwd for .memex/skills + *.memex.{json,jsonl}."""
    settings = get_settings()
    if not settings.auto_bootstrap:
        return
    try:
        from memex.skills import bootstrap as do_bootstrap

        eng = _engine()
        result = do_bootstrap(eng, settings.bootstrap_root)
        if result.skills_installed:
            err_console.print(
                f"[dim]auto-bootstrap: installed {len(result.skills_installed)} skill(s) "
                f"({result.concepts_added} concepts) from {settings.bootstrap_root}[/dim]"
            )
    except Exception as e:
        err_console.print(f"[yellow]auto-bootstrap failed: {e}[/yellow]")


@app.command()
def serve(
    direct: Annotated[
        bool,
        typer.Option(
            "--direct/--via-daemon",
            help=(
                "--direct: open Kuzu in this process (legacy; not safe across "
                "concurrent editor sessions). --via-daemon (default): proxy "
                "through the long-running daemon, auto-spawning it if needed."
            ),
        ),
    ] = False,
) -> None:
    """Run the MCP stdio server (for AI editors that pipe stdio).

    Default mode (--via-daemon): becomes a thin HTTP client to the memex
    daemon. Multiple editor sessions all share one engine, eliminating the
    Kuzu directory-lock collisions that happen when each session opens its
    own DB. The daemon is auto-spawned on first call if not already running.
    """
    _setup_logging()
    if direct:
        _maybe_auto_bootstrap()
        from memex.frontends.mcp.server import run_stdio

        run_stdio()
        return

    # Daemon mode: ensure a daemon is up, then run stdio as a thin client.
    from memex.frontends.mcp.server import run_stdio_via_daemon

    try:
        run_stdio_via_daemon(auto_spawn=True)
    except RuntimeError as e:
        err_console.print(f"[red]daemon mode failed:[/red] {e}")
        err_console.print(
            "[yellow]falling back to --direct mode for this session[/yellow]"
        )
        _maybe_auto_bootstrap()
        from memex.frontends.mcp.server import run_stdio

        run_stdio()


@app.command()
def daemon(
    listen: Annotated[str | None, typer.Option("--listen", "-l")] = None,
    auth_token: Annotated[
        str | None, typer.Option("--auth-token", envvar="MEMEX_AUTH_TOKEN")
    ] = None,
) -> None:
    """Run the long-running HTTP daemon."""
    _setup_logging()
    settings = get_settings()
    bind = listen or settings.listen
    host, _, port_s = bind.partition(":")
    if not port_s:
        err_console.print(f"[red]invalid --listen value `{bind}`[/red] — expected HOST:PORT")
        raise typer.Exit(code=2)

    _maybe_auto_bootstrap()

    from memex.frontends.http.server import InsecureBindingError, run_http

    try:
        run_http(host=host, port=int(port_s), auth_token=auth_token or settings.auth_token)
    except InsecureBindingError as e:
        err_console.print(f"[red]{e}[/red]")
        raise typer.Exit(code=2) from None


@app.command()
def migrate(
    archive: Annotated[
        bool,
        typer.Option("--archive/--no-archive", help="Rename legacy files to *.pre-duckdb.bak after a successful migration."),
    ] = False,
) -> None:
    """One-shot import of legacy Kuzu + SQLite stores into the unified DuckDB file.

    Idempotent: re-running merges by primary key. After confirming the new
    store works, run with --archive to rename the old files out of the way.
    """
    _setup_logging()
    from memex.core.stores.migrate import archive_legacy_files, migrate_to_duckdb

    settings = get_settings()
    result = migrate_to_duckdb(settings, vector_dim=settings.embed_dim)
    console.print(f"[green]migration complete:[/green] {result.summary()}")
    console.print(f"target: {settings.store_path}")
    if archive:
        moved = archive_legacy_files(settings)
        if moved:
            console.print(f"archived legacy files: {', '.join(p.name for p in moved)}")
        else:
            console.print("no legacy files to archive")


@app.command(name="observe-event")
def observe_event(
    kind: str = typer.Argument(..., help="Event kind, e.g. user_prompt, tool_use, turn_end."),
    actor: Annotated[str, typer.Option("--actor")] = "agent",
    via_daemon: Annotated[
        bool,
        typer.Option("--via-daemon/--direct", help="Default uses the daemon (fast, ~5ms). --direct opens the engine in-process (slow; useful for debugging)."),
    ] = True,
) -> None:
    """Record a Claude Code / agent hook event into memex's episodic memory.

    Reads the event payload as JSON on stdin. Designed for Claude Code hook
    handlers — UserPromptSubmit, PostToolUse, Stop, etc. — to pipe their event
    JSON in and have it captured automatically. A non-zero exit only on parse
    errors so a misconfigured hook fails loud rather than corrupting the stream.

    On `tool_pre` events for memex MCP tool calls, emits a Claude Code
    PreToolUse hook output of {"systemMessage": "memex"} so the user sees a
    branded badge in chat each time memex is invoked.

    Latency target: <10ms via daemon, so hooks don't block the editor.

    Example settings.json hook block:
      {"matcher": "*",
       "hooks": [{"type": "command",
                  "command": "memex observe-event user_prompt --actor=human"}]}
    """
    import json as _json
    import sys as _sys
    raw = _sys.stdin.read()
    if not raw.strip():
        return
    try:
        payload = _json.loads(raw)
    except _json.JSONDecodeError as e:
        err_console.print(f"[red]observe-event:[/red] invalid JSON on stdin: {e}")
        raise typer.Exit(code=2) from None

    if kind == "tool_pre" and isinstance(payload, dict):
        tool_name = payload.get("tool_name", "")
        if isinstance(tool_name, str) and tool_name.startswith("mcp__memex__"):
            _sys.stdout.write(_json.dumps({"systemMessage": "memex"}))
            _sys.stdout.write("\n")
            _sys.stdout.flush()

    # Trim very large fields so we don't blow up episodic storage. Hooks see
    # the full conversation transcripts; we only want navigation breadcrumbs.
    payload = _scrub_event_payload(payload)

    settings = get_settings()
    if via_daemon:
        from memex.frontends.mcp.client import MemexClient
        from memex.frontends.mcp.daemon import (
            daemon_url,
            ensure_daemon,
            is_daemon_alive,
        )
        url = daemon_url(settings)
        # Auto-spawn unless MEMEX_DAEMON_URL is explicit (remote/K8s).
        if not settings.daemon_url and not is_daemon_alive(url, settings.auth_token):
            try:
                url = ensure_daemon(settings)
            except RuntimeError:
                # Hooks must never break the editor flow. Fail silently.
                return
        try:
            with MemexClient(base_url=url, auth_token=settings.auth_token, timeout=2.0) as c:
                c.observe(kind=kind, actor=actor, payload=payload)
        except Exception as e:  # noqa: BLE001
            log = logging.getLogger(__name__)
            log.warning("observe-event via daemon failed: %s", e)
        return

    # --direct fallback: spin up an Engine. Slow (~500ms cold) — debugging only.
    engine = _engine()
    try:
        engine.observe(kind=kind, actor=Source(actor), payload=payload)
    finally:
        engine.close()


@app.command(name="todowrite-sync")
def todowrite_sync(
    via_daemon: Annotated[bool, typer.Option("--via-daemon/--direct")] = True,
) -> None:
    """Sync Claude Code's TodoWrite list into memex tasks.

    Reads the PostToolUse hook payload for TodoWrite on stdin and upserts each
    todo as a memex task (kind=task) with deterministic ids derived from the
    session + content. Lets your in-session todo list survive across sessions
    automatically — the user's TodoWrite stays the interface, memex becomes the
    durable backing store.

    Hook wiring (in ~/.claude/settings.json):
      "PostToolUse": [{"matcher": "TodoWrite",
                       "hooks": [{"type": "command",
                                  "command": "memex todowrite-sync"}]}]
    """
    import hashlib
    import json as _json
    import sys as _sys
    raw = _sys.stdin.read()
    if not raw.strip():
        return
    try:
        data = _json.loads(raw)
    except _json.JSONDecodeError:
        return  # silent — hook noise shouldn't break the editor

    # PostToolUse payload: {tool_name, tool_input: {todos: [...]}, session_id, ...}
    todos = (data.get("tool_input") or {}).get("todos") or []
    session_id = data.get("session_id", "unknown")
    if not todos:
        return

    settings = get_settings()
    from memex.frontends.mcp.client import MemexClient
    from memex.frontends.mcp.daemon import (
        daemon_url,
        ensure_daemon,
        is_daemon_alive,
    )
    url = daemon_url(settings)
    if via_daemon and not settings.daemon_url and not is_daemon_alive(url, settings.auth_token):
        try:
            url = ensure_daemon(settings)
        except RuntimeError:
            return

    try:
        with MemexClient(base_url=url, auth_token=settings.auth_token, timeout=3.0) as c:
            for t in todos:
                content = (t.get("content") or "").strip()
                if not content:
                    continue
                status = t.get("status", "pending")
                # Deterministic id so re-runs upsert the same task.
                key = f"task:{session_id}:{content}"
                stable_id = "c_" + hashlib.sha1(key.encode()).hexdigest()[:12]
                # We can't pre-set the id via add_node; instead embed it in
                # metadata so future syncs can find + update via list_tasks.
                c.add(
                    name=content[:200],
                    description=content,
                    kind="task",
                    source="claude_code",
                    confidence=1.0,
                    metadata={
                        "status": status,
                        "session_id": session_id,
                        "stable_id": stable_id,
                    },
                )
    except Exception as e:  # noqa: BLE001
        log = logging.getLogger(__name__)
        log.warning("todowrite-sync failed: %s", e)


def _scrub_event_payload(payload: Any, max_str: int = 2000, max_depth: int = 6) -> Any:
    """Trim large strings and deep nesting from a hook payload."""
    if max_depth <= 0:
        return "<truncated:depth>"
    if isinstance(payload, str):
        if len(payload) > max_str:
            return payload[:max_str] + f"…(+{len(payload) - max_str} chars)"
        return payload
    if isinstance(payload, dict):
        return {k: _scrub_event_payload(v, max_str, max_depth - 1) for k, v in payload.items()}
    if isinstance(payload, list):
        return [_scrub_event_payload(v, max_str, max_depth - 1) for v in payload[:50]]
    return payload


@app.command(name="hooks-install")
def hooks_install(
    apply: Annotated[
        bool,
        typer.Option("--apply", help="Merge into ~/.claude/settings.json. Default prints the snippet for manual paste."),
    ] = False,
) -> None:
    """Print (or merge) the recommended Claude Code hooks for memex auto-capture.

    The hooks fire on every user prompt, every tool call, every turn end —
    each event is recorded as an episodic memex memory. You can then `recall`
    "what did I do yesterday" and get a meaningful answer instead of "nothing,
    I'm a fresh session."
    """
    import json as _json

    block = {
        "hooks": {
            "UserPromptSubmit": [
                {
                    "matcher": "*",
                    "hooks": [
                        {"type": "command", "command": "memex observe-event user_prompt --actor=human"}
                    ],
                }
            ],
            "PreToolUse": [
                {
                    "matcher": "*",
                    "hooks": [
                        {"type": "command", "command": "memex observe-event tool_pre"}
                    ],
                }
            ],
            "PostToolUse": [
                {
                    "matcher": "TodoWrite",
                    "hooks": [
                        {"type": "command", "command": "memex todowrite-sync"}
                    ],
                },
                {
                    "matcher": "*",
                    "hooks": [
                        {"type": "command", "command": "memex observe-event tool_post"}
                    ],
                },
            ],
            "Stop": [
                {
                    "matcher": "*",
                    "hooks": [
                        {"type": "command", "command": "memex observe-event turn_end"}
                    ],
                }
            ],
        }
    }
    if not apply:
        console.print(_json.dumps(block, indent=2))
        console.print(
            "\n[yellow]Above: paste the [bold]hooks[/bold] block into your "
            "~/.claude/settings.json (merging with anything already there).[/yellow]\n"
            "Or re-run with [bold]--apply[/bold] to merge automatically."
        )
        return

    settings_path = Path.home() / ".claude" / "settings.json"
    existing: dict = {}
    if settings_path.exists():
        try:
            existing = _json.loads(settings_path.read_text(encoding="utf-8"))
        except _json.JSONDecodeError as e:
            err_console.print(f"[red]existing settings.json is invalid:[/red] {e}")
            raise typer.Exit(code=2) from None
    merged_hooks = existing.get("hooks", {})
    added = 0
    for event_name, handlers in block["hooks"].items():
        existing_handlers = merged_hooks.setdefault(event_name, [])
        existing_signatures = {
            (h.get("matcher", ""), tuple(sorted((c.get("type", ""), c.get("command", "")) for c in h.get("hooks", []))))
            for h in existing_handlers
            if isinstance(h, dict)
        }
        for handler in handlers:
            sig = (handler.get("matcher", ""), tuple(sorted((c.get("type", ""), c.get("command", "")) for c in handler.get("hooks", []))))
            if sig in existing_signatures:
                continue
            existing_handlers.append(handler)
            existing_signatures.add(sig)
            added += 1
    existing["hooks"] = merged_hooks
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(_json.dumps(existing, indent=2), encoding="utf-8")
    console.print(f"[green]merged hooks into[/green] {settings_path}")


task_app = typer.Typer(name="task", help="Tasks: persistent TODOs that survive across sessions.", no_args_is_help=True)
app.add_typer(task_app, name="task")


@task_app.command("add")
def task_add(
    title: str = typer.Argument(..., help="One-line task summary."),
    description: Annotated[str, typer.Option("--description", "-d")] = "",
    priority: Annotated[str, typer.Option("--priority", "-p")] = "p2",
    due: Annotated[str | None, typer.Option("--due")] = None,
    project: Annotated[str | None, typer.Option("--project")] = None,
    owner: Annotated[str | None, typer.Option("--owner")] = None,
) -> None:
    """Add a task to your durable TODO graph."""
    _setup_logging()
    engine = _engine()
    try:
        c = engine.add_task(
            title=title,
            description=description,
            priority=priority,
            due=due,
            project_id=project,
            owner=owner,
        )
        console.print(f"[green]+[/green] {c.id}  {c.name}")
    finally:
        engine.close()


@task_app.command("done")
def task_done(task_id: str) -> None:
    """Mark a task completed."""
    _setup_logging()
    engine = _engine()
    try:
        updated = engine.update_task(task_id=task_id, status="completed")
        if updated is None:
            err_console.print(f"[red]task `{task_id}` not found[/red]")
            raise typer.Exit(code=1)
        console.print(f"[green]✓[/green] {updated.id}  {updated.name}")
    finally:
        engine.close()


@task_app.command("block")
def task_block(
    task_id: str,
    by: Annotated[str, typer.Option("--by", help="Blocker task id.")] = ...,
) -> None:
    """Mark a task blocked by another task."""
    _setup_logging()
    engine = _engine()
    try:
        engine.link(from_id=by, to_id=task_id, kind=EdgeKind.blocks, source=Source.human)
        engine.update_task(task_id=task_id, status="blocked")
        console.print(f"[yellow]blocked[/yellow] {task_id} by {by}")
    finally:
        engine.close()


@task_app.command("list")
def task_list(
    status: Annotated[str, typer.Option("--status")] = "pending",
    project: Annotated[str | None, typer.Option("--project")] = None,
    limit: Annotated[int, typer.Option("--limit")] = 50,
) -> None:
    """List tasks."""
    _setup_logging()
    engine = _engine()
    try:
        tasks = engine.list_tasks(status=status, project_id=project, limit=limit)
        if not tasks:
            console.print("[dim]no matching tasks[/dim]")
            return
        table = Table(show_header=True, header_style="bold")
        table.add_column("priority", style="cyan", width=4)
        table.add_column("status", style="yellow", width=12)
        table.add_column("due", width=10)
        table.add_column("title")
        table.add_column("id", style="dim", width=14)
        for t in tasks:
            m = t.metadata or {}
            table.add_row(
                m.get("priority", "p2"),
                m.get("status", "pending"),
                m.get("due", "-"),
                t.name,
                t.id,
            )
        console.print(table)
    finally:
        engine.close()


@task_app.command("next")
def task_next(limit: Annotated[int, typer.Option("--limit")] = 5) -> None:
    """Top N tasks you should pick up right now."""
    _setup_logging()
    engine = _engine()
    try:
        tasks = engine.next_actions(limit=limit)
        if not tasks:
            console.print("[dim]nothing actionable right now — all tasks blocked or done[/dim]")
            return
        for t in tasks:
            m = t.metadata or {}
            console.print(
                f"[cyan]{m.get('priority','p2')}[/cyan] "
                f"[yellow]{m.get('status','pending')}[/yellow] "
                f"{t.name}  [dim]{t.id}[/dim]"
            )
    finally:
        engine.close()


@app.command(name="ingest-repos")
def ingest_repos(
    roots: Annotated[
        list[Path],
        typer.Argument(help="One or more parent directories to scan (e.g. ~/authfi-* via shell glob)."),
    ] = None,
    glob: Annotated[
        str,
        typer.Option("--glob", help="Match repos under each root by name pattern."),
    ] = "*",
    dry_run: Annotated[bool, typer.Option("--dry-run/--apply")] = True,
) -> None:
    """Walk repos and create per-repo bookmark concepts (kind=fact, metadata.repo=true).

    Reads each repo's README + package manifest (package.json / pyproject.toml /
    Cargo.toml / go.mod) and synthesizes a structured bookmark with **Purpose:**,
    **Stack:**, **Layout:**. This solves the "KB is cluttered with strategy
    docs but no per-repo grounding" problem.

    Default is dry-run — prints what it would add. Pass --apply to write.
    """
    _setup_logging()
    if not roots:
        roots = [Path.home()]

    repos: list[Path] = []
    for root in roots:
        if not root.exists():
            err_console.print(f"[yellow]skip[/yellow] {root} (does not exist)")
            continue
        if (root / ".git").exists():
            repos.append(root)
        else:
            for child in sorted(root.iterdir()):
                if child.is_dir() and (child / ".git").exists():
                    if not _glob_match(child.name, glob):
                        continue
                    repos.append(child)

    if not repos:
        console.print("[yellow]no git repositories found under given roots[/yellow]")
        return

    console.print(f"[bold]found {len(repos)} repo(s)[/bold]")
    engine = None if dry_run else _engine()
    try:
        for repo in repos:
            bookmark = _summarize_repo(repo)
            if dry_run:
                console.print(f"\n[cyan]── {repo.name} ──[/cyan]")
                console.print(bookmark["description"])
            else:
                c = engine.add(
                    name=bookmark["name"],
                    description=bookmark["description"],
                    kind=NodeKind.fact,
                    source=Source.extractor,
                    confidence=0.7,
                    metadata={**bookmark["metadata"], "repo": True},
                )
                console.print(f"[green]+[/green] {c.id}  {c.name}")
    finally:
        if engine is not None:
            engine.close()


def _glob_match(name: str, pattern: str) -> bool:
    import fnmatch
    return fnmatch.fnmatchcase(name, pattern)


def _summarize_repo(repo: Path) -> dict:
    """Read README + manifest files to build a per-repo bookmark concept."""
    name = repo.name
    purpose = _read_first_paragraph(_find_readme(repo)) or ""
    stack = _detect_stack(repo)
    layout = _shallow_layout(repo)
    body = (
        f"**Purpose:** {purpose}\n\n"
        f"**Stack:** {', '.join(stack) or 'unknown'}\n\n"
        f"**Layout:**\n" + "\n".join(f"  - {p}" for p in layout) + "\n\n"
        f"**Path:** `{repo}`\n"
    )
    return {
        "name": name,
        "description": body,
        "metadata": {
            "path": str(repo),
            "stack": stack,
        },
    }


def _find_readme(repo: Path) -> Path | None:
    for cand in ("README.md", "README.MD", "Readme.md", "readme.md", "README.rst", "README.txt"):
        p = repo / cand
        if p.exists():
            return p
    return None


def _read_first_paragraph(path: Path | None, max_chars: int = 600) -> str:
    if path is None:
        return ""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    # Skip leading badges / headers; take first non-empty block of prose.
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    for p in paragraphs:
        # Skip obvious non-prose lines.
        if p.startswith("#") and len(p) < 80:
            continue
        if p.startswith("[!") or p.startswith("![]"):
            continue
        # Strip markdown headers from inside the paragraph.
        cleaned = "\n".join(
            line for line in p.splitlines() if not line.lstrip().startswith("#")
        )
        if cleaned:
            return cleaned[:max_chars]
    return paragraphs[0][:max_chars] if paragraphs else ""


def _detect_stack(repo: Path) -> list[str]:
    stack: list[str] = []
    if (repo / "package.json").exists():
        stack.append("node")
    if (repo / "pyproject.toml").exists() or (repo / "setup.py").exists():
        stack.append("python")
    if (repo / "Cargo.toml").exists():
        stack.append("rust")
    if (repo / "go.mod").exists():
        stack.append("go")
    if (repo / "pom.xml").exists() or (repo / "build.gradle").exists():
        stack.append("java")
    if (repo / "Gemfile").exists():
        stack.append("ruby")
    if (repo / "Dockerfile").exists():
        stack.append("docker")
    if (repo / "Chart.yaml").exists() or (repo / "deploy/helm").exists():
        stack.append("helm")
    if (repo / "terraform").exists() or list(repo.glob("*.tf")):
        stack.append("terraform")
    return stack


def _shallow_layout(repo: Path, limit: int = 10) -> list[str]:
    out: list[str] = []
    for child in sorted(repo.iterdir())[:limit * 2]:
        if child.name.startswith("."):
            continue
        out.append(child.name + ("/" if child.is_dir() else ""))
        if len(out) >= limit:
            break
    return out


@app.command(name="consolidate")
def consolidate(
    window: Annotated[int, typer.Option("--window", help="How many recent events to scan.")] = 500,
    min_cluster: Annotated[int, typer.Option("--min-cluster", help="Minimum events to form a summary concept.")] = 5,
    use_llm: Annotated[bool, typer.Option("--llm/--no-llm", help="Use the auto-detected LLM provider for richer summaries.")] = True,
) -> None:
    """Compress recent episodic events into durable summary concepts.

    The sleep-cycle pass. Run nightly (cron / Helm CronJob) to keep memex
    from devolving into a flat event log. With --llm and an Ollama or
    Anthropic provider configured, summaries are prose; without, they are
    templated counts.
    """
    _setup_logging()
    engine = _engine()
    try:
        llm = None
        if use_llm:
            from memex.ml.llm import build_default_llm
            provider = build_default_llm()
            if provider.is_available():
                llm = provider
                console.print(f"[dim]using LLM provider: {provider.name}[/dim]")
        result = engine.consolidate(window_events=window, min_cluster=min_cluster, llm=llm)
        console.print(
            f"[green]consolidate:[/green] events_seen={result['events_seen']} "
            f"clusters={result['clusters']} concepts_added={result['concepts_added']}"
        )
    finally:
        engine.close()


@app.command(name="reindex")
def reindex_vectors() -> None:
    """Backfill embeddings for every concept in the semantic store.

    Run this after a fresh migration (legacy store had no vectors) or after
    swapping embedding models. Idempotent — vector rows upsert by id.
    """
    _setup_logging()
    engine = _engine()
    try:
        result = engine.reindex_vectors()
        console.print(
            f"[green]reindex complete:[/green] embedded={result['embedded']} "
            f"failed={result['failed']} total={result['total']}"
        )
    except RuntimeError as e:
        err_console.print(f"[red]{e}[/red]")
        raise typer.Exit(code=1) from None
    finally:
        engine.close()


@app.command()
def version() -> None:
    """Print the memex version."""
    console.print(f"memex {__version__}")


# ---- upstream gateway --------------------------------------------------

upstream_app = typer.Typer(
    name="upstream",
    help="Manage upstream MCP servers (the gateway). Re-exposes their tools through memex with auto-perception.",
    no_args_is_help=True,
)
app.add_typer(upstream_app)


def _upstreams_path() -> Path:
    """Resolve where to write upstream config. Project-local takes precedence."""
    proj = Path.cwd() / ".memex" / "upstreams.json"
    if proj.parent.is_dir():
        return proj
    return Path.home() / ".memex" / "upstreams.json"


def _read_upstreams_file(path: Path):
    import json
    from memex.upstreams.config import UpstreamsFile

    if not path.is_file():
        return UpstreamsFile()
    return UpstreamsFile.model_validate(json.loads(path.read_text(encoding="utf-8")))


def _write_upstreams_file(path: Path, file_obj) -> None:
    import json

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(file_obj.model_dump(mode="json"), indent=2),
        encoding="utf-8",
    )


@upstream_app.command("list")
def upstream_list() -> None:
    """Show configured upstreams."""
    from memex.upstreams import load_upstreams

    cfg = load_upstreams()
    if not cfg.upstreams:
        console.print("[dim]No upstreams configured.[/dim]")
        console.print("Browse the catalog: [bold]memex upstream catalog[/bold]")
        return
    table = Table(title="upstreams")
    table.add_column("name", style="bold")
    table.add_column("type", style="cyan")
    table.add_column("target", style="dim")
    table.add_column("allow", style="green")
    for u in cfg.upstreams:
        if u.type == "http":
            target = u.url or "—"
        else:
            target = f"{u.command or ''} {' '.join(u.args)}".strip() or "—"
        table.add_row(u.name, u.type, target, ", ".join(u.allow))
    console.print(table)


@upstream_app.command("catalog")
def upstream_catalog(
    category: Annotated[str | None, typer.Option("--category", "-c")] = None,
) -> None:
    """List MCP servers from the curated catalog."""
    from memex.upstreams.catalog import by_category

    groups = by_category()
    cats = [category] if category else sorted(groups.keys())
    for cat in cats:
        entries = groups.get(cat, [])
        if not entries:
            continue
        table = Table(title=f"catalog · {cat}", show_lines=False)
        table.add_column("id", style="bold cyan")
        table.add_column("type", style="cyan")
        table.add_column("description")
        table.add_column("requires", style="yellow")
        for e in entries:
            table.add_row(e.id, e.type, e.summary, ", ".join(e.requires) or "—")
        console.print(table)
    console.print(
        "\n[dim]Show full detail with[/dim] [bold]memex upstream show <id>[/bold]"
    )
    console.print(
        "[dim]Install one with[/dim] [bold]memex upstream install <id>[/bold]"
    )


@upstream_app.command("show")
def upstream_show(catalog_id: str) -> None:
    """Show full detail for a catalog entry — including how to use it and what memex adds."""
    from memex.upstreams.catalog import find_entry

    e = find_entry(catalog_id)
    if e is None:
        err_console.print(f"[red]not in catalog: {catalog_id}[/red]")
        raise typer.Exit(code=2)

    # Header
    console.print(f"\n[bold cyan]{e.id}[/bold cyan]  ·  {e.summary}")
    console.print(f"[dim]category: {e.category}  ·  type: {e.type}  ·  homepage: {e.homepage}[/dim]\n")

    # Long description
    if e.long_description:
        console.print("[bold]What this MCP does[/bold]")
        console.print(e.long_description + "\n")

    # memex value-add — the unique pitch
    if e.memex_value:
        console.print("[bold magenta]Why use it through memex[/bold magenta]")
        console.print(e.memex_value + "\n")

    # Setup
    if e.setup_steps:
        console.print("[bold]Setup[/bold]")
        for i, step in enumerate(e.setup_steps, 1):
            console.print(f"  [cyan]{i}.[/cyan] {step}")
        console.print("")
    elif e.requires:
        console.print(f"[bold]Requires:[/bold] {', '.join(e.requires)}\n")

    # Usage examples
    if e.usage_examples:
        console.print("[bold]Common tools[/bold]")
        ut = Table(show_header=True, box=None)
        ut.add_column("tool", style="bold")
        ut.add_column("does what")
        ut.add_column("example", style="dim")
        for ex in e.usage_examples:
            ut.add_row(ex.tool, ex.what, ex.example)
        console.print(ut)
        console.print("")

    # Paired skills
    if e.paired_skills:
        console.print(
            "[bold]Paired skills:[/bold] "
            + ", ".join(f"[cyan]{s}[/cyan]" for s in e.paired_skills)
        )
        console.print(
            "[dim]Install with[/dim] [bold]memex install skill:<name>[/bold]\n"
        )

    # Transport detail (the runtime shape)
    console.print("[bold]Runtime[/bold]")
    if e.type == "http":
        console.print(f"  url:  {e.url}")
        if e.auth:
            kind = e.auth.get("kind", "none")
            console.print(f"  auth: {kind}")
            if "token_env" in e.auth:
                console.print(f"        from env: ${e.auth['token_env']}")
    else:
        console.print(f"  command: {e.command} {' '.join(e.args)}")
        if e.env:
            for k, v in e.env.items():
                console.print(f"  env:     {k}={v}")
    console.print("")
    console.print(f"[dim]Install:[/dim] [bold]memex upstream install {e.id}[/bold]")


@upstream_app.command("install")
def upstream_install(
    catalog_id: str,
    name: Annotated[str | None, typer.Option("--name", "-n", help="Override the upstream name (default = catalog name).")] = None,
) -> None:
    """Add a catalog entry to your upstreams.json."""
    from memex.upstreams.catalog import find_entry
    from memex.upstreams.config import UpstreamConfig

    e = find_entry(catalog_id)
    if e is None:
        err_console.print(f"[red]not in catalog: {catalog_id}[/red]")
        raise typer.Exit(code=2)
    rendered = e.render()
    if name:
        rendered["name"] = name
    new_cfg = UpstreamConfig.model_validate(rendered)

    path = _upstreams_path()
    file_obj = _read_upstreams_file(path)
    if any(u.name == new_cfg.name for u in file_obj.upstreams):
        err_console.print(
            f"[yellow]upstream `{new_cfg.name}` already configured in {path}[/yellow] — "
            "edit the file directly or `memex upstream remove` first."
        )
        raise typer.Exit(code=2)
    file_obj.upstreams.append(new_cfg)
    _write_upstreams_file(path, file_obj)
    console.print(f"[green]installed[/green] {new_cfg.name} -> {path}")

    # Prefer the rich setup_steps over the loose `requires` list when present.
    if e.setup_steps:
        console.print("\n[bold]Next steps:[/bold]")
        for i, step in enumerate(e.setup_steps, 1):
            console.print(f"  [cyan]{i}.[/cyan] {step}")
    elif e.requires:
        console.print(f"[yellow]requires:[/yellow] {', '.join(e.requires)}")
    if e.paired_skills:
        console.print(
            "\n[dim]Paired skills:[/dim] " + ", ".join(e.paired_skills)
        )
    console.print("\nRestart the memex MCP server (and your AI client) to pick it up.")


@upstream_app.command("add")
def upstream_add(
    name: str,
    command: Annotated[str | None, typer.Option("--command", "-c", help="(stdio) Executable to spawn.")] = None,
    args: Annotated[list[str] | None, typer.Option("--arg", "-a", help="(stdio) Repeat for multiple args.")] = None,
    env: Annotated[list[str] | None, typer.Option("--env", "-e", help="(stdio) KEY=VAL; repeat.")] = None,
    http: Annotated[str | None, typer.Option("--http", help="(http) Streamable HTTP / SSE URL of an MCP server.")] = None,
    bearer_env: Annotated[str | None, typer.Option("--bearer-env", help="(http) Env-var name holding the Bearer token.")] = None,
    header: Annotated[list[str] | None, typer.Option("--header", "-H", help="(http) KEY=VAL; repeat for extra headers.")] = None,
) -> None:
    """Manually add an upstream — stdio (default) or HTTP via --http URL."""
    from memex.upstreams.config import AuthConfig, UpstreamConfig

    if http and command:
        err_console.print(
            "[red]--http and --command are mutually exclusive — pick one transport[/red]"
        )
        raise typer.Exit(code=2)

    if http:
        # HTTP upstream
        headers_map: dict[str, str] = {}
        for kv in header or []:
            if "=" not in kv:
                err_console.print(f"[red]invalid --header {kv} (need KEY=VAL)[/red]")
                raise typer.Exit(code=2)
            k, _, v = kv.partition("=")
            headers_map[k] = v
        auth_cfg = None
        if bearer_env:
            auth_cfg = AuthConfig(kind="bearer", token_env=bearer_env)
        new_cfg = UpstreamConfig(
            name=name, type="http", url=http, auth=auth_cfg, headers=headers_map
        )
    else:
        if not command:
            err_console.print("[red]need --command (stdio) or --http (http)[/red]")
            raise typer.Exit(code=2)
        env_map: dict[str, str] = {}
        for kv in env or []:
            if "=" not in kv:
                err_console.print(f"[red]invalid --env {kv} (need KEY=VAL)[/red]")
                raise typer.Exit(code=2)
            k, _, v = kv.partition("=")
            env_map[k] = v
        new_cfg = UpstreamConfig(
            name=name, type="stdio", command=command, args=args or [], env=env_map
        )

    path = _upstreams_path()
    file_obj = _read_upstreams_file(path)
    if any(u.name == new_cfg.name for u in file_obj.upstreams):
        err_console.print(f"[yellow]upstream `{name}` already configured.[/yellow]")
        raise typer.Exit(code=2)
    file_obj.upstreams.append(new_cfg)
    _write_upstreams_file(path, file_obj)
    console.print(f"[green]added[/green] {name} ({new_cfg.type}) -> {path}")


@upstream_app.command("remove")
def upstream_remove(name: str) -> None:
    """Remove an upstream by name."""
    path = _upstreams_path()
    file_obj = _read_upstreams_file(path)
    before = len(file_obj.upstreams)
    file_obj.upstreams = [u for u in file_obj.upstreams if u.name != name]
    if len(file_obj.upstreams) == before:
        err_console.print(f"[yellow]no upstream named `{name}` in {path}[/yellow]")
        raise typer.Exit(code=2)
    _write_upstreams_file(path, file_obj)
    console.print(f"[green]removed[/green] {name}")


@upstream_app.command("test")
def upstream_test(name: str) -> None:
    """Connect to an upstream and list its tools (without keeping the conn)."""
    from memex.upstreams import Aggregator, load_upstreams

    cfg = load_upstreams()
    target = next((u for u in cfg.upstreams if u.name == name), None)
    if target is None:
        err_console.print(f"[red]no upstream named `{name}`[/red]")
        raise typer.Exit(code=2)
    agg = Aggregator([target])
    try:
        agg.start()
        conn = agg.connections.get(name)
        if conn is None or not conn.connected:
            err = conn.last_error if conn else "unknown"
            err_console.print(f"[red]upstream `{name}` failed:[/red] {err}")
            raise typer.Exit(code=1)
        table = Table(title=f"{name} — {len(conn.tools)} tool(s)")
        table.add_column("tool", style="bold")
        table.add_column("description", style="dim")
        for t in conn.tools:
            tn = getattr(t, "name", None) or t["name"]
            td = (getattr(t, "description", None) or "").splitlines()[0:1]
            table.add_row(tn, td[0] if td else "")
        console.print(table)
    finally:
        agg.stop()


# ----------------------------------------------------------------------------
# Codebase memory — `memex source` subcommands + `memex recall-code` query.
# ----------------------------------------------------------------------------


source_app = typer.Typer(
    name="source",
    help="Manage indexed codebases. memex chunks them into a typed graph "
         "(source → file → symbol) — not into fuzzy text snippets.",
    no_args_is_help=True,
)
app.add_typer(source_app)


@source_app.command("add")
def source_add(
    path: Annotated[Path, typer.Argument(
        help="Filesystem path to the codebase root.",
        exists=True, file_okay=False, dir_okay=True, resolve_path=True,
    )],
    name: Annotated[str | None, typer.Option(
        "--name", "-n", help="Display name (defaults to slug from path)."
    )] = None,
    no_index: Annotated[bool, typer.Option(
        "--no-index", help="Register the source without immediately indexing.",
    )] = False,
) -> None:
    """Register and (by default) index a codebase."""
    from memex.codebase import add_source as cb_add_source, index_source

    settings = get_settings()
    engine = Engine.build_default(settings)
    try:
        src = cb_add_source(engine, path, name=name)
        console.print(
            f"[green]registered[/green] {src.name} → [dim]{src.metadata['path']}[/dim]"
        )
        console.print(f"  id: [cyan]{src.id}[/cyan]")
        if no_index:
            console.print("[yellow]skipped indexing[/yellow] (--no-index)")
            return
        with console.status("indexing…"):
            result = index_source(engine, src.id)
        console.print(
            f"[green]indexed[/green] {result.files_indexed} files, "
            f"{result.symbols_indexed} symbols, "
            f"{result.skipped_files} skipped"
        )
        if result.languages:
            langs = ", ".join(f"{k}: {v}" for k, v in sorted(result.languages.items()))
            console.print(f"  languages: [dim]{langs}[/dim]")
    finally:
        engine.close()


@source_app.command("list")
def source_list() -> None:
    """List registered codebases."""
    from memex.codebase import list_sources

    settings = get_settings()
    engine = Engine.build_default(settings)
    try:
        sources = list_sources(engine)
        if not sources:
            console.print("[dim]no sources registered[/dim]")
            return
        table = Table(title=f"{len(sources)} source(s)")
        table.add_column("id", style="cyan")
        table.add_column("name", style="bold")
        table.add_column("path", style="dim")
        table.add_column("files")
        table.add_column("symbols")
        table.add_column("last indexed", style="dim")
        for s in sources:
            md = s.metadata
            table.add_row(
                s.id,
                s.name,
                md.get("path", "?"),
                str(md.get("indexed_files", 0)),
                str(md.get("indexed_symbols", 0)),
                md.get("last_indexed_at") or "—",
            )
        console.print(table)
    finally:
        engine.close()


@source_app.command("remove")
def source_remove(
    source_id: Annotated[str, typer.Argument(help="Source concept id (c_…)")],
) -> None:
    """Delete a source plus every file + symbol it owns."""
    from memex.codebase import remove_source

    settings = get_settings()
    engine = Engine.build_default(settings)
    try:
        deleted = remove_source(engine, source_id)
        console.print(f"[green]removed[/green] {deleted} concept(s)")
    finally:
        engine.close()


@source_app.command("reindex")
def source_reindex(
    source_id: Annotated[str, typer.Argument(help="Source concept id (c_…)")],
) -> None:
    """Drop existing chunks and re-index from disk."""
    from memex.codebase import reindex_source

    settings = get_settings()
    engine = Engine.build_default(settings)
    try:
        with console.status("re-indexing…"):
            result = reindex_source(engine, source_id)
        console.print(
            f"[green]re-indexed[/green] {result.files_indexed} files, "
            f"{result.symbols_indexed} symbols"
        )
    finally:
        engine.close()


@source_app.command("orphans")
def source_orphans(
    source: Annotated[str | None, typer.Option(
        "--source", help="Limit to a single registered source (id).",
    )] = None,
    include_private: Annotated[bool, typer.Option(
        "--include-private", help="Include `_`-prefixed symbols.",
    )] = False,
) -> None:
    """List code symbols with zero incoming `calls` / `extends` edges —
    provably unreachable within the indexed corpus. Use this to surface
    dead-code candidates. Cross-repo callers are NOT yet considered (slice B)."""
    from memex.codebase import find_orphans

    settings = get_settings()
    engine = Engine.build_default(settings)
    try:
        orphans = find_orphans(
            engine, source_id=source, include_private=include_private
        )
        if not orphans:
            console.print("[green]no orphans[/green]")
            return
        table = Table(title=f"{len(orphans)} orphan(s)")
        table.add_column("kind", style="bold")
        table.add_column("name")
        table.add_column("language", style="dim")
        table.add_column("location", style="dim")
        for c in orphans:
            md = c.metadata
            loc = f"{md.get('rel_path', '?')}:{md.get('start_line')}-{md.get('end_line')}"
            table.add_row(md.get("symbol_kind", "?"), c.name, md.get("language", ""), loc)
        console.print(table)
    finally:
        engine.close()


@app.command(name="recall-code")
def recall_code_cmd(
    query: Annotated[str, typer.Argument(help="Symbol name or signature substring.")],
    source: Annotated[str | None, typer.Option(
        "--source", help="Limit to a single registered source (id).",
    )] = None,
    kind: Annotated[str | None, typer.Option(
        "--kind", help="Filter by symbol kind: class|function|method|interface|…",
    )] = None,
    expand: Annotated[int, typer.Option(
        "--expand", help="Edges of neighborhood to surface (0=just matches).",
    )] = 1,
    limit: Annotated[int, typer.Option("--limit", help="Max matches.")] = 20,
) -> None:
    """Recall symbols by name + signature, with their typed-graph neighborhood.

    Returns matched symbols, their defining file + parent class, cross-repo
    `same_as` siblings (slice B), and any concept-memory nodes that mention
    them — NOT a fuzzy snippet list. See `memex source add` first.
    """
    from memex.codebase import recall_code as cb_recall

    settings = get_settings()
    engine = Engine.build_default(settings)
    try:
        result = cb_recall(
            engine,
            query,
            source_id=source,
            symbol_kind=kind,
            expand_hops=expand,
            limit=limit,
        )
        if not result.matches:
            console.print(f"[yellow]no matches for[/yellow] {query!r}")
            return
        table = Table(title=f"{len(result.matches)} match(es) for `{query}`")
        table.add_column("kind", style="bold")
        table.add_column("name")
        table.add_column("language", style="dim")
        table.add_column("location", style="dim")
        for m in result.matches:
            md = m.metadata
            loc = f"{md.get('rel_path', '?')}:{md.get('start_line')}-{md.get('end_line')}"
            table.add_row(md.get("symbol_kind", "?"), m.name, md.get("language", ""), loc)
        console.print(table)
        if result.neighborhood:
            console.print(
                f"\n[dim]neighborhood ({len(result.neighborhood)} nodes, "
                f"{len(result.edges)} edges):[/dim]"
            )
            for n in result.neighborhood[:10]:
                console.print(f"  [{n.kind.value}] {n.name}")
        if result.related_concepts:
            console.print(
                f"\n[dim]related decisions/constraints ({len(result.related_concepts)}):[/dim]"
            )
            for c in result.related_concepts[:5]:
                console.print(f"  [{c.kind.value}] {c.name}")
    finally:
        engine.close()


if __name__ == "__main__":
    app()
