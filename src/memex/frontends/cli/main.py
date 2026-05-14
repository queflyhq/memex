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
import os
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

# Windows fix: cp1252 (the default code page on most US/EU Windows
# terminals) can't encode arrows, em-dashes, etc. that we use freely
# in help text. Force stdout/stderr to UTF-8 before Rich grabs them,
# and tell Rich to emit no-color fallback if the terminal still
# can't render. Without this, `memex --help` crashes on Windows.
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass

app = typer.Typer(
    name="memex",
    help="Persistent cognitive memory for AI coding tools.",
    no_args_is_help=True,
    add_completion=False,
)

# `safe_box=False` lets Rich use Unicode box characters; we already
# reconfigured stdout to utf-8 so they render correctly.
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
    elif kind == "rules":
        if name == "safety-baseline":
            from memex.enforcement.baseline_rules import install_baseline_rules
            eng = _engine()
            counts = install_baseline_rules(eng)
            console.print(
                f"[green]installed[/green] safety-baseline rules: "
                f"{counts['added']} added, {counts['skipped']} skipped "
                f"(already present), {counts['total']} total in pack"
            )
        else:
            err_console.print(f"[red]unknown rules pack `{name}`[/red] — known: safety-baseline")
            raise typer.Exit(code=2)
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
def init(
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip prompts; install everything detected.")] = False,
    skip_service: Annotated[bool, typer.Option("--skip-service", help="Don't install the always-on user service.")] = False,
    skip_hooks: Annotated[bool, typer.Option("--skip-hooks", help="Don't wire AI editor hooks.")] = False,
    seed_repo: Annotated[str | None, typer.Option("--seed", help="Seed memex from this repo path on first run.")] = None,
) -> None:
    """First-run wizard. Detects installed AI editors, wires hooks, installs
    the always-on service, optionally seeds from a repo. Idempotent.

    Run once after installing memex. Safe to re-run after upgrades.
    """
    from rich.prompt import Confirm
    console.rule("[bold]memex init[/bold]")
    console.print("Welcome. This wizard wires memex into your machine in three steps:\n"
                  "  1. Wire AI editor hooks (so memex captures prompts + tool calls)\n"
                  "  2. Install the always-on service (so memex starts on login)\n"
                  "  3. Optionally seed memex with a repo (so day-1 isn't empty)\n")

    # ---- 1. Detect + wire AI editors ----
    if not skip_hooks:
        console.rule("[dim]1/3  AI editor hooks[/dim]")
        from memex.integrations import ClaudeCode, Cursor, Windsurf, Cline
        detected = []
        for cls in (ClaudeCode, Cursor, Windsurf, Cline):
            integ = cls()
            status = integ.status()
            if status.tool_installed:
                detected.append((integ, status))
        if not detected:
            console.print("[yellow]No AI editors detected on this machine.[/yellow]")
            console.print("Run [bold]memex setup[/bold] later once an editor is installed.")
        else:
            console.print(f"Detected {len(detected)} editor{'' if len(detected) == 1 else 's'}:")
            for integ, status in detected:
                wired = "✓ already wired" if status.memex_present else "would wire"
                console.print(f"  · {integ.name}  [dim]{wired}[/dim]")
            if yes or Confirm.ask("Wire memex into all detected editors?", default=True):
                for integ, status in detected:
                    if status.memex_present:
                        console.print(f"  [dim]· {integ.name} — already up to date[/dim]")
                        continue
                    r = integ.wire()
                    if r.error:
                        console.print(f"  [red]· {integ.name} — {r.error}[/red]")
                    else:
                        console.print(f"  [green]· {integ.name} — {r.action}[/green]")

    # ---- 2. Install always-on service ----
    if not skip_service:
        console.rule("[dim]2/3  Always-on service[/dim]")
        from memex.service import install_service, service_status
        cur = service_status()
        if cur.get("installed"):
            console.print(f"[green]Service already installed.[/green] Running: {cur.get('running')}")
        elif yes or Confirm.ask("Install memex as an always-on service (starts on login)?", default=True):
            info = install_service()
            if info.get("ok"):
                console.print("[green]Service installed.[/green]")
                for k, v in info.items():
                    if k != "ok" and v:
                        console.print(f"  [dim]{k}[/dim]  {v}")
            else:
                console.print(f"[red]Service install failed: {info.get('error') or info.get('stderr')}[/red]")
                console.print("[dim]Continuing — you can run `memex daemon` manually.[/dim]")

    # ---- 3. Seed from a repo (optional) ----
    console.rule("[dim]3/3  Seed memex from a repo (optional)[/dim]")
    if seed_repo is None and not yes:
        if Confirm.ask("Seed memex from the current directory? (extracts decisions from git log + docs)", default=False):
            seed_repo = "."
    if seed_repo:
        from memex.seed import seed_from_repo
        eng = _engine()
        console.print(f"[dim]scanning {seed_repo}…[/dim]")
        report = seed_from_repo(eng, seed_repo, include_code_symbols=False, dry_run=False)
        console.print(
            f"[green]seeded[/green] {report.total_added()} concepts from {seed_repo}: "
            f"{report.decisions_added} decisions, {report.facts_added} facts, "
            f"{report.people_added} people  "
            f"[dim]({report.commits_kept} of {report.commits_scanned} commits kept)[/dim]"
        )

    # ---- summary ----
    console.rule("[bold green]Setup complete[/bold green]")
    console.print("\nWhat's next:")
    console.print("  · Open the web UI: [bold]http://127.0.0.1:7777[/bold]")
    console.print("  · Tail what memex is doing: [bold]memex stats[/bold]")
    console.print("  · Run safety baseline rules: [bold]memex install rules:safety-baseline[/bold]")
    console.print("  · Connect a team repo: [bold]memex team init <path>[/bold]")
    console.print()


service_app = typer.Typer(name="service", help="Install / uninstall memex as an always-on user service.")
app.add_typer(service_app)


@service_app.command("install")
def service_install(
    listen: Annotated[str, typer.Option("--listen", help="HOST:PORT to bind. Default 127.0.0.1:7777.")] = "127.0.0.1:7777",
    team_repo: Annotated[str | None, typer.Option("--team-repo", help="Path to a team git-sync repo (sets MEMEX_TEAM_REPO).")] = None,
) -> None:
    """Make memex always-on for this user. Mac=launchd, Win=Task Scheduler, Linux=systemd-user."""
    from memex.service import install_service
    info = install_service(listen=listen, team_repo=team_repo)
    if info.get("ok"):
        console.print(f"[green]memex service installed[/green]")
        for k, v in info.items():
            if k != "ok" and v:
                console.print(f"  [dim]{k}[/dim]  {v}")
    else:
        err_console.print(f"[red]install failed[/red]: {info.get('error') or info.get('stderr')}")
        raise typer.Exit(code=1)


@service_app.command("uninstall")
def service_uninstall() -> None:
    """Remove the always-on service (does not delete data)."""
    from memex.service import uninstall_service
    info = uninstall_service()
    if info.get("ok"):
        console.print("[green]memex service uninstalled[/green]")
    else:
        err_console.print(f"[red]uninstall failed[/red]: {info.get('error')}")


@service_app.command("status")
def service_status_cmd() -> None:
    """Is the always-on service installed and running?"""
    from memex.service import service_status
    info = service_status()
    table = Table(show_header=False, box=None)
    table.add_row("[dim]installed[/dim]", "yes" if info.get("installed") else "[red]no[/red]")
    table.add_row("[dim]running[/dim]",   "yes" if info.get("running") else "[red]no[/red]")
    for k, v in info.items():
        if k not in {"installed", "running"} and v:
            table.add_row(f"[dim]{k}[/dim]", str(v))
    console.print(table)


team_app = typer.Typer(name="team", help="Team-mode git-sync of the memex graph.")
app.add_typer(team_app)


@team_app.command("init")
def team_init(
    path: Annotated[str, typer.Argument(help="Path to the team git repo (e.g. ~/code/team-memex).")],
) -> None:
    """Initialize a team-shared memex repo. Idempotent — safe to re-run."""
    from memex.team import init_team_repo
    info = init_team_repo(path)
    console.print(f"[green]team repo ready[/green] at {info['repo']}")
    console.print(f"  graph root: {info['graph_root']}")
    console.print()
    console.print("Next steps:")
    console.print("  1. Set the remote: [bold]git -C {} remote add origin <url>[/bold]".format(info['repo']))
    console.print("  2. Tell memex: [bold]export MEMEX_TEAM_REPO={}[/bold]".format(info['repo']))
    console.print("  3. The scheduler will sync every 10 min, or run [bold]memex team push[/bold]")


@team_app.command("push")
def team_push() -> None:
    """Push local durable concepts/edges to the team repo + git push."""
    import os
    from memex.team import sync_push
    repo = os.environ.get("MEMEX_TEAM_REPO")
    if not repo:
        err_console.print("[red]MEMEX_TEAM_REPO not set[/red] — run `memex team init <path>` first.")
        raise typer.Exit(code=2)
    eng = _engine()
    report = sync_push(eng, repo)
    table = Table()
    table.add_column("what"); table.add_column("count", justify="right")
    table.add_row("concepts pushed", str(report.pushed_concepts))
    table.add_row("edges pushed", str(report.pushed_edges))
    table.add_row("skipped (private)", str(report.skipped_private))
    table.add_row("elapsed", f"{report.elapsed_ms} ms")
    console.print(table)
    if report.errors:
        err_console.print(f"[yellow]{len(report.errors)} non-fatal errors[/yellow]")
        for e in report.errors[:3]:
            err_console.print(f"  [dim]{e}[/dim]")


@team_app.command("pull")
def team_pull() -> None:
    """Pull from the team repo and merge inbound concepts into local."""
    import os
    from memex.team import sync_pull
    repo = os.environ.get("MEMEX_TEAM_REPO")
    if not repo:
        err_console.print("[red]MEMEX_TEAM_REPO not set[/red] — run `memex team init <path>` first.")
        raise typer.Exit(code=2)
    eng = _engine()
    report = sync_pull(eng, repo)
    table = Table()
    table.add_column("what"); table.add_column("count", justify="right")
    table.add_row("concepts pulled", str(report.pulled_concepts))
    table.add_row("edges pulled", str(report.pulled_edges))
    table.add_row("conflicts (ours-latest)", str(report.conflicts_resolved))
    table.add_row("elapsed", f"{report.elapsed_ms} ms")
    console.print(table)
    if report.errors:
        err_console.print(f"[yellow]{len(report.errors)} non-fatal errors[/yellow]")
        for e in report.errors[:3]:
            err_console.print(f"  [dim]{e}[/dim]")


@app.command()
def seed(
    path: Annotated[str, typer.Argument(help="Path to the repo to scan (defaults to cwd).")] = ".",
    max_commits: Annotated[int, typer.Option("--max-commits", help="Cap on commits scanned.")] = 500,
    skip_code: Annotated[bool, typer.Option("--skip-code", help="Skip AST symbol indexing.")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Report what would be added without writing.")] = False,
) -> None:
    """Seed memex from a repo. Solves day-1 empty-graph problem.

    Scans: git history, README/ARCHITECTURE/DECISIONS/ADRs, CHANGELOG,
    manifest files (pyproject.toml/package.json/...), and code symbols.
    Each meaningful commit, doc, and dependency becomes a typed concept
    linked back to a parent project node.

    Idempotent: re-running on the same repo updates rather than duplicating.
    """
    from memex.seed import seed_from_repo
    eng = _engine()
    console.print(f"[dim]scanning {path}…[/dim]")
    report = seed_from_repo(
        eng, path,
        max_commits=max_commits,
        include_code_symbols=not skip_code,
        dry_run=dry_run,
    )
    table = Table(title="seed report" + (" (dry run)" if dry_run else ""))
    table.add_column("what", style="cyan")
    table.add_column("count", style="green", justify="right")
    table.add_row("decisions added", str(report.decisions_added))
    table.add_row("facts added", str(report.facts_added))
    table.add_row("people added", str(report.people_added))
    table.add_row("commits scanned", str(report.commits_scanned))
    table.add_row("commits kept", str(report.commits_kept))
    table.add_row("docs scanned", str(report.docs_scanned))
    table.add_row("manifests scanned", str(report.manifests_scanned))
    table.add_row("code indexed", "yes" if report.code_indexed else "no")
    console.print(table)
    if report.errors:
        err_console.print(f"[yellow]{len(report.errors)} non-fatal errors[/yellow]")
        for e in report.errors[:5]:
            err_console.print(f"  [dim]{e}[/dim]")
    console.print(
        f"[green]total {report.total_added()} concepts[/green] linked to project "
        f"[bold]{report.project_id or '(none)'}[/bold]"
    )


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

    # Single-daemon guard: if a healthy daemon already responds at this URL,
    # exit cleanly instead of starting a second instance that loses the port
    # race and zombies. Multi-process scenarios (Claude Code via .venv +
    # another caller via uv) used to leave both processes running, only
    # one bound. Now the second one bows out.
    from memex.frontends.mcp.daemon import is_daemon_alive, daemon_url
    probe_host = "127.0.0.1" if host == "0.0.0.0" else host
    probe_url = f"http://{probe_host}:{int(port_s)}"
    if is_daemon_alive(probe_url, settings.auth_token):
        err_console.print(
            f"[yellow]memex daemon already running at {probe_url} — "
            f"exiting cleanly. Use `--listen HOST:PORT` to run a second one.[/yellow]"
        )
        raise typer.Exit(code=0)

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

    # POST directly to /remember (idempotent on name+kind+source) so the
    # same content+session pair upserts instead of duplicating. The old
    # path called c.add (POST /nodes) which is non-idempotent — every
    # TodoWrite write produced N new task nodes.
    import urllib.request, urllib.error
    headers = {
        "Authorization": f"Bearer {settings.auth_token}",
        "Content-Type": "application/json",
    }
    user_id = _user_id_from_context()
    project = _project_from_context(data)
    for t in todos:
        content = (t.get("content") or "").strip()
        if not content:
            continue
        status = t.get("status", "pending")
        active_form = (t.get("activeForm") or "").strip()
        body = _json.dumps({
            "name": content[:200],
            "description": content if not active_form else f"{content}\n\n[active form] {active_form}",
            "kind": "task",
            "source": "claude_code",
            "confidence": 1.0,
            "metadata": {
                "status": status,
                "session_id": session_id,
                "active_form": active_form,
                "project": project,
                "user_id": user_id,
            },
        }).encode("utf-8")
        try:
            req = urllib.request.Request(
                url + "/remember", data=body, method="POST", headers=headers,
            )
            urllib.request.urlopen(req, timeout=2.0).read()
        except (urllib.error.URLError, OSError) as e:  # noqa: BLE001
            log = logging.getLogger(__name__)
            log.warning("todowrite-sync remember failed: %s", e)


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


@app.command(name="ui")
def ui_cmd(
    no_browser: Annotated[bool, typer.Option("--no-browser", help="Print the URL but don't open a browser tab")] = False,
    port: Annotated[int, typer.Option("--port", help="Daemon port (default 7777)")] = 7777,
) -> None:
    """Open the memex web UI in your default browser.

    Starts the daemon if it isn't already running, then opens
    http://127.0.0.1:<port>/app/ in your default browser. The UI talks
    to the same local daemon over HTTP — no separate Wails native app,
    no platform-specific binaries, no code signing. Just `pip install
    memex` and you have the full UI.

    Authentication: the served page is injected with the daemon's
    bearer token so JS fetch calls authenticate transparently. The
    token never leaves your machine because the daemon binds to
    127.0.0.1 only.
    """
    import webbrowser
    settings = get_settings()
    from memex.frontends.mcp.daemon import (
        daemon_url, ensure_daemon, is_daemon_alive,
    )
    url = daemon_url(settings)
    if not is_daemon_alive(url, settings.auth_token):
        console.print("[dim]starting daemon…[/dim]")
        try:
            url = ensure_daemon(settings)
        except RuntimeError as e:
            err_console.print(f"[red]daemon failed to start:[/red] {e}")
            raise typer.Exit(code=1) from None
    ui_url = url.rstrip("/") + "/app/"
    console.print(f"[green]memex UI:[/green] {ui_url}")
    if no_browser:
        return
    try:
        webbrowser.open(ui_url)
    except Exception as e:  # noqa: BLE001
        err_console.print(
            f"[yellow]couldn't open a browser automatically:[/yellow] {e}\n"
            f"open {ui_url} manually"
        )


@app.command(name="doctor")
def doctor(
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
    fix: Annotated[bool, typer.Option("--fix", help="Attempt safe auto-fixes for failing checks")] = False,
) -> None:
    """One-shot diagnostic for memex installation health.

    Runs 8 checks and reports green/yellow/red for each. Designed to be
    the FIRST command a teammate runs after `pip install memex` — and
    the first thing they share when something breaks. Outputs in plain
    text so the result can be pasted into chat.

    Exit code: 0 if all green, 1 if any red, 2 if internal error.
    """
    import json as _json
    import os as _os
    import socket as _socket
    import sys as _sys
    from datetime import datetime as _dt, timezone as _tz

    rows: list[tuple[str, str, str, str]] = []  # (status, name, detail, fix_hint)
    GREEN, YELLOW, RED = "[green]OK[/green]", "[yellow]WARN[/yellow]", "[red]FAIL[/red]"

    def _add(status: str, name: str, detail: str, hint: str = "") -> None:
        rows.append((status, name, detail, hint))

    # 1. Data dir
    settings = get_settings()
    ddir = Path(str(settings.data_dir))
    if ddir.is_dir():
        _add(GREEN, "data dir", str(ddir))
    else:
        _add(RED, "data dir", f"missing: {ddir}", "run any memex command to auto-create")
        if fix:
            ddir.mkdir(parents=True, exist_ok=True)

    # 2. Daemon reachable
    daemon_url_resolved = settings.daemon_url or "http://127.0.0.1:7777"
    daemon_alive = False
    health: dict[str, Any] | None = None
    try:
        import httpx as _httpx
        r = _httpx.get(daemon_url_resolved + "/health", timeout=1.5)
        if r.status_code == 200:
            daemon_alive = True
            health = r.json()
    except Exception:  # noqa: BLE001
        pass
    if daemon_alive and health:
        _add(GREEN, "daemon", f"{daemon_url_resolved} v{health.get('version','?')} concepts={health.get('concepts',0)}")
    else:
        _add(RED, "daemon", f"unreachable at {daemon_url_resolved}", "run `memex daemon` (or restart if it was running)")

    # 3. Auth token resolvable
    from memex.runtime_state import read_auth_token
    token = settings.auth_token or read_auth_token(settings)
    if token:
        _add(GREEN, "auth token", f"loaded ({len(token)} bytes) — from {'env' if settings.auth_token else 'daemon.token disk'}")
    else:
        _add(YELLOW, "auth token", "no MEMEX_AUTH_TOKEN env var and no daemon.token file yet",
             "daemon will write one on first start; not required for local-loopback /health")

    # 4. OMP /version conformance
    if daemon_alive:
        try:
            v = _httpx.get(daemon_url_resolved + "/version", timeout=1.0).json()
            mmp = v.get("omp_version", "?")
            verbs = v.get("verbs") or []
            if mmp == "0.1" and len(verbs) >= 5:
                _add(GREEN, "OMP version", f"v{mmp}, {len(verbs)} verbs exposed")
            else:
                _add(YELLOW, "OMP version", f"v{mmp}, verbs={verbs}", "/version endpoint older than expected")
        except Exception as e:  # noqa: BLE001
            _add(YELLOW, "OMP version", f"could not probe: {e}")

    # 5. Embedding model — degraded vs healthy
    if daemon_alive and token:
        try:
            r = _httpx.get(
                daemon_url_resolved + "/recall",
                params={"q": "memex doctor probe", "budget": 300},
                headers={"Authorization": f"Bearer {token}"},
                timeout=5.0,
            )
            if r.status_code == 200:
                rd = r.json()
                strategy = rd.get("strategy", "?")
                degraded = rd.get("degraded", False)
                if degraded:
                    reason = (rd.get("degraded_reason") or "?")[:120]
                    _add(YELLOW, "embeddings", f"degraded ({strategy}): {reason}",
                         "run `memex setup-models` to re-download the embedding model, or pin fastembed/onnxruntime versions")
                else:
                    _add(GREEN, "embeddings", f"healthy (strategy={strategy})")
            else:
                _add(YELLOW, "embeddings", f"recall probe returned HTTP {r.status_code}")
        except Exception as e:  # noqa: BLE001
            _add(YELLOW, "embeddings", f"probe failed: {e}")

    # 6. LLM hook
    llm_env = []
    if _os.environ.get("ANTHROPIC_API_KEY"):
        llm_env.append("anthropic")
    if _os.environ.get("OPENAI_API_KEY"):
        llm_env.append("openai")
    if _os.environ.get("OLLAMA_HOST") or _os.path.exists("/tmp/.ollama"):
        try:
            r = _httpx.get(
                (_os.environ.get("OLLAMA_HOST") or "http://127.0.0.1:11434") + "/api/tags",
                timeout=0.5,
            )
            if r.status_code == 200:
                llm_env.append("ollama")
        except Exception:  # noqa: BLE001
            pass
    if llm_env:
        _add(GREEN, "LLM hook", f"available providers: {', '.join(llm_env)}")
    else:
        _add(YELLOW, "LLM hook", "no LLM configured",
             "set ANTHROPIC_API_KEY / OPENAI_API_KEY / run Ollama to enable code-regen, consolidation, discovery")

    # 7. Claude Code hook installed?
    home = Path(_os.path.expanduser("~"))
    settings_json = home / ".claude" / "settings.json"
    if settings_json.is_file():
        try:
            data = _json.loads(settings_json.read_text(encoding="utf-8"))
            hooks = (data or {}).get("hooks", {}) or {}
            wired_events: list[str] = []
            for ev, handlers in hooks.items():
                if not isinstance(handlers, list):
                    continue
                for h in handlers:
                    for c in (h or {}).get("hooks", []) or []:
                        cmd = (c or {}).get("command", "") or ""
                        if "memex" in cmd:
                            wired_events.append(ev)
                            break
            if wired_events:
                _add(GREEN, "Claude Code hooks", f"wired: {', '.join(sorted(set(wired_events)))}")
            else:
                _add(YELLOW, "Claude Code hooks", "settings.json present but no memex hooks",
                     "run `memex hooks-install --apply` to wire PreToolUse / PostToolUse / UserPromptSubmit")
        except Exception as e:  # noqa: BLE001
            _add(YELLOW, "Claude Code hooks", f"settings.json parse error: {e}")
    else:
        _add(YELLOW, "Claude Code hooks", f"{settings_json} not found",
             "Claude Code not installed, or first-run; hooks-install will create it")

    # 8. Upstream MCPs
    if daemon_alive and token:
        try:
            ur = _httpx.get(
                daemon_url_resolved + "/upstreams",
                headers={"Authorization": f"Bearer {token}"},
                timeout=2.0,
            ).json()
            installed = ur.get("upstreams") or []
            if installed:
                _add(GREEN, "upstream MCPs",
                     f"{len(installed)} installed: {', '.join(u.get('name','?') for u in installed[:5])}")
            else:
                _add(YELLOW, "upstream MCPs", "none installed",
                     "run `memex upstream install fetch` (no token needed) to start")
        except Exception as e:  # noqa: BLE001
            _add(YELLOW, "upstream MCPs", f"probe failed: {e}")

    # ---- render ----
    console.print(f"\n[bold]memex doctor[/bold] — {_dt.now(_tz.utc).isoformat()}")
    console.print(f"  host:   {_socket.gethostname()}")
    console.print(f"  user:   {_user_id_from_context() or '(unknown)'}")
    console.print(f"  python: {_sys.version_info.major}.{_sys.version_info.minor}.{_sys.version_info.micro}\n")

    any_red = False
    for status, name, detail, hint in rows:
        console.print(f"  {status} {name:<22} {detail}")
        if hint and (verbose or "FAIL" in status):
            console.print(f"      [dim]→ {hint}[/dim]")
        if "FAIL" in status:
            any_red = True

    console.print("")
    if any_red:
        console.print("[red]doctor: at least one critical check failed.[/red] Address red items above.")
        raise typer.Exit(code=1)
    yellows = sum(1 for s, *_ in rows if "WARN" in s)
    if yellows:
        console.print(f"[yellow]doctor: ok with {yellows} warning(s).[/yellow] Run with -v for fix hints.")
    else:
        console.print("[green]doctor: all green. memex is ready for the team.[/green]")


@app.command(name="backup")
def backup_cmd(
    out: Annotated[Path | None, typer.Option("--out", "-o", help="Output directory (default <data_dir>/backups/<timestamp>/)")] = None,
    via_daemon: Annotated[bool, typer.Option("--via-daemon/--direct", help="When the daemon is running, ask it to CHECKPOINT first so the .wal is folded into the .duckdb. Direct mode runs without daemon coordination.")] = True,
) -> None:
    """Snapshot the memex store so a future migration / experiment can roll back.

    Copies `memex.duckdb` + the secrets index + the active reranker
    pointer into `<out>/`. When the daemon is running, sends it a
    CHECKPOINT first so the WAL is folded into the main DB file (no
    write loss). Returns the backup path on stdout for scripting.
    """
    import shutil as _sh
    import time as _t
    settings = get_settings()
    ts = _t.strftime("%Y%m%d-%H%M%S")
    out_dir = out or (Path(str(settings.data_dir)) / "backups" / ts)
    out_dir.mkdir(parents=True, exist_ok=True)

    if via_daemon:
        try:
            import httpx as _httpx
            from memex.frontends.mcp.daemon import daemon_url, is_daemon_alive
            url = daemon_url(settings)
            if is_daemon_alive(url, settings.auth_token):
                headers = {"Authorization": f"Bearer {settings.auth_token}"} if settings.auth_token else {}
                # Ask the daemon to flush its WAL via a checkpoint.
                # /maintenance/checkpoint will be added next session;
                # for now, observe a flush_requested event — the daemon's
                # next idle cycle picks it up. Best effort: doesn't block
                # the backup if the endpoint isn't present.
                try:
                    _httpx.post(
                        url + "/observe",
                        json={"kind": "backup_requested", "actor": "human",
                              "payload": {"out_dir": str(out_dir)}},
                        headers=headers, timeout=1.0,
                    )
                except Exception:  # noqa: BLE001
                    pass
        except Exception:  # noqa: BLE001
            pass

    src_files = [
        ("memex.duckdb", True),
        ("memex.duckdb.wal", False),
        ("daemon.token", False),
        ("daemon.url", False),
        ("active_reranker.txt", False),
    ]
    ddir = Path(str(settings.data_dir))
    copied = 0
    for fname, required in src_files:
        sp = ddir / fname
        if not sp.is_file():
            if required:
                err_console.print(f"[red]missing required file:[/red] {sp}")
                raise typer.Exit(code=2)
            continue
        _sh.copy2(sp, out_dir / fname)
        copied += 1
    # Secrets index — a directory tree, not a file
    secrets_dir = ddir / "secrets"
    if secrets_dir.is_dir():
        _sh.copytree(secrets_dir, out_dir / "secrets", dirs_exist_ok=True)
        copied += 1

    size_mb = sum(
        f.stat().st_size for f in out_dir.rglob("*") if f.is_file()
    ) / 1024 / 1024
    console.print(
        f"[green]backed up[/green] {copied} entries ({size_mb:.1f} MB) "
        f"→ {out_dir}"
    )
    # Print the bare path on the final line so scripts can capture it.
    print(str(out_dir))


@app.command(name="train-reranker")
def train_reranker(
    base: Annotated[str, typer.Option("--base", help="HuggingFace model id of the cross-encoder to fine-tune")] = "cross-encoder/ms-marco-MiniLM-L-6-v2",
    pairs: Annotated[Path | None, typer.Option("--pairs", help="Path to a pairs JSONL produced by `memex maintenance mine-pairs --write`. Defaults to the latest in <data_dir>/training/")] = None,
    out: Annotated[Path | None, typer.Option("--out", help="Output directory. Defaults to <data_dir>/models/<base>-tuned-<timestamp>/")] = None,
    epochs: Annotated[int, typer.Option("--epochs")] = 3,
    batch_size: Annotated[int, typer.Option("--batch-size")] = 16,
    min_pairs: Annotated[int, typer.Option("--min-pairs", help="Refuse to train if fewer pairs than this — embeddings need data")] = 200,
    apply: Annotated[bool, typer.Option("--apply", help="On success, write the tuned model's path to <data_dir>/active_reranker.txt so the daemon picks it up on next restart")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Print what would happen without installing/training")] = False,
) -> None:
    """Fine-tune the reranker on memex's own (query → useful concept) pairs.

    Workflow:
      1. Read pairs JSONL (each line: {"query": "...", "positive": "...", "negative": "..."})
      2. Wrap in a CrossEncoder MultipleNegativesRankingLoss training run
      3. Save to <data_dir>/models/<base>-tuned-<timestamp>/
      4. (--apply) Flip the active reranker to the new path

    Heavy dependencies: torch, sentence-transformers. Gate behind
    `pip install memex[train]` to keep the base wheel small. The wheel
    extras_require entry isn't yet added — for now: `pip install
    sentence-transformers` before running.

    Pair source: `memex maintenance mine-pairs --write` extracts pairs
    from the episodic stream (see Engine.mine_pairs / automl_interval).
    """
    import json as _json
    from datetime import datetime as _dt
    settings = get_settings()

    # Resolve pairs file.
    if pairs is None:
        train_dir = Path(str(settings.data_dir)) / "training"
        if not train_dir.is_dir():
            err_console.print(
                f"[red]no training data found at {train_dir}[/red]\n"
                f"run `memex maintenance mine-pairs --write` first"
            )
            raise typer.Exit(code=2)
        candidates = sorted(train_dir.glob("pairs-*.jsonl"), reverse=True)
        if not candidates:
            err_console.print(f"[red]no pairs-*.jsonl in {train_dir}[/red]")
            raise typer.Exit(code=2)
        pairs = candidates[0]
        console.print(f"[dim]using latest pairs: {pairs}[/dim]")

    # Count pairs first so we can refuse loudly.
    rows: list[dict[str, Any]] = []
    with pairs.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(_json.loads(line))
            except _json.JSONDecodeError:
                continue
    if len(rows) < min_pairs:
        err_console.print(
            f"[red]only {len(rows)} pairs — minimum is {min_pairs}[/red]\n"
            f"keep using memex; the automl loop mines more pairs over time"
        )
        raise typer.Exit(code=2)
    console.print(f"[green]ready to train on {len(rows)} pairs[/green]")

    if dry_run:
        console.print("[yellow]--dry-run; skipping install + train[/yellow]")
        return

    # Heavy deps are imported lazily so the base CLI stays light.
    try:
        from sentence_transformers import CrossEncoder
        from sentence_transformers.cross_encoder.losses import (
            BinaryCrossEntropyLoss as _BCELoss,  # type: ignore[import-untyped]
        )
        from torch.utils.data import DataLoader as _DL
        from sentence_transformers import InputExample as _IE
    except ImportError as e:
        err_console.print(
            f"[red]missing train extras:[/red] {e}\n"
            f"install with: pip install sentence-transformers torch"
        )
        raise typer.Exit(code=2) from None

    # Build training set: each pair becomes a (query, positive, 1.0)
    # row + a contrastive (query, hard-negative, 0.0) row when we have one.
    examples: list[Any] = []
    for r in rows:
        q = (r.get("query") or "").strip()
        pos = (r.get("positive") or "").strip()
        neg = (r.get("negative") or "").strip()
        if not q or not pos:
            continue
        examples.append(_IE(texts=[q, pos], label=1.0))
        if neg:
            examples.append(_IE(texts=[q, neg], label=0.0))
    if len(examples) < min_pairs:
        err_console.print(
            f"[red]only {len(examples)} usable rows after filter[/red]"
        )
        raise typer.Exit(code=2)

    out_dir = out or (
        Path(str(settings.data_dir))
        / "models"
        / f"{base.replace('/', '--')}-tuned-{_dt.now().strftime('%Y%m%d-%H%M%S')}"
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    console.print(f"[cyan]training → {out_dir}[/cyan]")
    model = CrossEncoder(base)
    loader = _DL(examples, batch_size=batch_size, shuffle=True)
    # NOTE: API surface here is sentence-transformers v3+. Older versions
    # use `model.fit(...)`. The try/except picks the one available.
    try:
        model.fit(
            train_dataloader=loader,
            epochs=epochs,
            output_path=str(out_dir),
            warmup_steps=max(10, len(examples) // 10),
        )
    except AttributeError:
        # v3+ CrossEncoderTrainer path
        from sentence_transformers.cross_encoder import CrossEncoderTrainer  # type: ignore[import-untyped]
        trainer = CrossEncoderTrainer(
            model=model,
            train_dataset=examples,
            loss=_BCELoss(model=model),
        )
        trainer.train()
        model.save_pretrained(str(out_dir))

    console.print(f"[green]done.[/green] tuned model at {out_dir}")

    if apply:
        active = Path(str(settings.data_dir)) / "active_reranker.txt"
        active.write_text(str(out_dir), encoding="utf-8")
        console.print(
            f"[green]active reranker switched.[/green]\n"
            f"restart daemon for the change to take effect."
        )


@app.command(name="setup-models")
def setup_models(
    model: Annotated[str, typer.Option("--model", help="HuggingFace model id (default: BAAI/bge-small-en-v1.5)")] = "BAAI/bge-small-en-v1.5",
    force: Annotated[bool, typer.Option("--force", help="Re-download even if a working copy exists")] = False,
) -> None:
    """Pre-download the embedding model into <data_dir>/models/ so memex
    never needs network access at recall time.

    The default model is the same one memex uses by default for vector
    search. After running this once, set MEMEX_MODEL_ROOT to the same
    path on a new machine to copy memex completely offline.

    Note: this command depends on fastembed's downloader; it doesn't pin
    the ONNX runtime version. If you hit an ONNXRuntimeError after
    download, the issue is fastembed↔onnxruntime version compatibility
    — pin both in pyproject.toml and reinstall. See docs/troubleshooting.md.
    """
    settings = get_settings()
    target_root = Path(str(settings.data_dir)) / "models"
    target_root.mkdir(parents=True, exist_ok=True)
    model_dir = target_root / model.replace("/", "--")
    if model_dir.exists() and not force:
        console.print(
            f"[yellow]already present[/yellow] {model_dir} "
            f"(use --force to re-download)"
        )
        return
    try:
        from fastembed import TextEmbedding
    except ImportError:
        err_console.print("[red]fastembed not installed[/red] — run `pip install fastembed`")
        raise typer.Exit(code=2) from None
    console.print(f"[cyan]downloading {model} → {target_root}[/cyan]")
    try:
        _ = TextEmbedding(model_name=model, cache_dir=str(target_root))
    except Exception as e:  # noqa: BLE001
        err_console.print(f"[red]download failed:[/red] {e}")
        raise typer.Exit(code=2) from None
    console.print(f"[green]ready[/green] {model_dir}")
    console.print(
        f"[dim]set MEMEX_MODEL_ROOT={target_root} on other machines "
        f"to use this checkout without re-downloading[/dim]"
    )


@app.command(name="export")
def export_bundle(
    out: Annotated[Path, typer.Option("--out", "-o", help="Output JSONL path")] = Path("memex-bundle.jsonl"),
    project: Annotated[str | None, typer.Option("--project", help="Filter to concepts whose metadata.project matches this slug")] = None,
    kinds: Annotated[str | None, typer.Option("--kinds", help="Comma-separated NodeKind filter, e.g. 'fact,decision,project,action_constraint'")] = None,
    include_edges: Annotated[bool, typer.Option("--edges/--no-edges")] = True,
    include_events: Annotated[bool, typer.Option("--events/--no-events", help="Include episodic events too; defaults to False (events are noisy + machine-specific)")] = False,
) -> None:
    """Export a portable bundle of concepts (+ edges, optionally events).

    Bundle format: one JSON object per line, each with a `_type` field
    of `concept` / `edge` / `event`. Imported via `memex import` —
    idempotent on (name, kind, source) via /remember semantics.

    Use this to share project context with teammates, archive memory
    before a wipe, or move between machines.
    """
    import json as _json
    settings = get_settings()
    engine = Engine.build_default(settings)
    try:
        kind_set: set[NodeKind] | None = None
        if kinds:
            kind_set = {NodeKind(k.strip()) for k in kinds.split(",") if k.strip()}

        with out.open("w", encoding="utf-8") as f:
            n_c = n_e = n_ev = 0
            # Concepts
            all_c = engine.semantic.all_concepts()
            kept_ids: set[str] = set()
            for c in all_c:
                if kind_set is not None and c.kind not in kind_set:
                    continue
                if project is not None:
                    md_proj = (c.metadata or {}).get("project")
                    md_ws = (c.metadata or {}).get("workspace")
                    if md_proj != project and md_ws != project:
                        continue
                f.write(_json.dumps({"_type": "concept", **c.model_dump(mode="json")}) + "\n")
                kept_ids.add(c.id)
                n_c += 1
            # Edges — only between kept concepts
            if include_edges:
                for cid in kept_ids:
                    for e in engine.semantic.edges_for(cid):
                        if e.from_id in kept_ids and e.to_id in kept_ids:
                            f.write(_json.dumps({"_type": "edge", **e.model_dump(mode="json")}) + "\n")
                            n_e += 1
            # Events — opt-in; big and machine-specific
            if include_events:
                for ev in engine.episodic.recent(limit=100_000):
                    f.write(_json.dumps({"_type": "event", **ev.model_dump(mode="json")}) + "\n")
                    n_ev += 1
        console.print(f"[green]exported[/green] concepts={n_c} edges={n_e} events={n_ev} → {out}")
    finally:
        engine.close()


@app.command(name="import")
def import_bundle(
    src: Annotated[Path, typer.Argument(help="Input JSONL path")],
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    via_daemon: Annotated[bool, typer.Option("--via-daemon/--direct")] = True,
) -> None:
    """Import a bundle produced by `memex export`. Idempotent on
    (name, kind, source) — re-running just refreshes last_confirmed_at.
    """
    import json as _json
    if not src.is_file():
        err_console.print(f"[red]no such file:[/red] {src}")
        raise typer.Exit(code=2)

    settings = get_settings()
    headers = (
        {"Authorization": f"Bearer {settings.auth_token}",
         "Content-Type": "application/json"}
        if settings.auth_token else {"Content-Type": "application/json"}
    )

    if via_daemon:
        from memex.frontends.mcp.daemon import (
            daemon_url, ensure_daemon, is_daemon_alive,
        )
        url = daemon_url(settings)
        if not settings.daemon_url and not is_daemon_alive(url, settings.auth_token):
            url = ensure_daemon(settings)
        import urllib.request
        n_c = n_e = skipped = 0
        with src.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                obj = _json.loads(line)
                t = obj.pop("_type", None)
                if t == "concept":
                    if dry_run:
                        n_c += 1
                        continue
                    payload = {
                        k: obj.get(k) for k in
                        ("name", "description", "kind", "source", "confidence",
                         "metadata", "verification")
                        if k in obj
                    }
                    req = urllib.request.Request(
                        url + "/remember",
                        data=_json.dumps(payload).encode("utf-8"),
                        method="POST",
                        headers=headers,
                    )
                    try:
                        urllib.request.urlopen(req, timeout=5.0).read()
                        n_c += 1
                    except Exception as e:  # noqa: BLE001
                        err_console.print(f"[yellow]concept skipped:[/yellow] {obj.get('name')} ({e})")
                        skipped += 1
                elif t == "edge":
                    # Edges depend on the target concepts existing; the
                    # idempotent /remember above ensures they do for any
                    # node in the bundle. Cross-bundle edges into
                    # missing nodes are skipped.
                    if dry_run:
                        n_e += 1
                        continue
                    req = urllib.request.Request(
                        url + "/edges",
                        data=_json.dumps(obj).encode("utf-8"),
                        method="POST",
                        headers=headers,
                    )
                    try:
                        urllib.request.urlopen(req, timeout=5.0).read()
                        n_e += 1
                    except Exception:  # noqa: BLE001
                        skipped += 1
                # events: ignored on import — they're machine-specific
        console.print(f"[green]imported[/green] concepts={n_c} edges={n_e} skipped={skipped} (dry_run={dry_run})")
        return
    err_console.print("[red]--direct import not yet implemented[/red]")
    raise typer.Exit(code=2)


@app.command(name="ingest-md")
def ingest_md(
    root: Annotated[Path, typer.Argument(help="Directory to scan recursively")],
    project: Annotated[str | None, typer.Option("--project", help="Tag every ingested doc with this project slug")] = None,
    glob: Annotated[str, typer.Option("--glob", help="Filename pattern (default: '*.md')")] = "*.md",
    max_chars: Annotated[int, typer.Option("--max-chars", help="Truncate body at N chars (default 4000, same as memex description cap)")] = 3800,
    skip_dirs: Annotated[str, typer.Option("--skip-dirs", help="Comma-separated dir basenames to skip")] = "node_modules,.venv,venv,.git,build,dist,target,.next,.svelte-kit,__pycache__",
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
) -> None:
    """Recursively ingest .md files into memex as kind=fact nodes.

    Filename → node name, body → description (truncated). Idempotent
    via /remember. Skips node_modules / .venv / build dirs by default.
    Use --project to tag ownership.
    """
    import json as _json
    import urllib.request
    if not root.is_dir():
        err_console.print(f"[red]not a directory:[/red] {root}")
        raise typer.Exit(code=2)
    skip_set = {s.strip() for s in skip_dirs.split(",") if s.strip()}
    settings = get_settings()
    from memex.frontends.mcp.daemon import (
        daemon_url, ensure_daemon, is_daemon_alive,
    )
    url = daemon_url(settings)
    if not settings.daemon_url and not is_daemon_alive(url, settings.auth_token):
        url = ensure_daemon(settings)
    headers = {
        "Authorization": f"Bearer {settings.auth_token}",
        "Content-Type": "application/json",
    }
    ingested = skipped = 0
    for p in root.rglob(glob):
        if any(part in skip_set for part in p.parts):
            skipped += 1
            continue
        try:
            body = p.read_text(encoding="utf-8")[:max_chars]
        except Exception:  # noqa: BLE001
            skipped += 1
            continue
        if not body.strip():
            skipped += 1
            continue
        name = f"{p.stem} ({p.parent.name})"
        md: dict[str, Any] = {"source_path": str(p), "doc_type": "md"}
        if project:
            md["project"] = project
        payload = {
            "name": name,
            "description": body,
            "kind": "fact",
            "source": "human",
            "confidence": 0.8,
            "metadata": md,
        }
        if dry_run:
            ingested += 1
            continue
        try:
            req = urllib.request.Request(
                url + "/remember",
                data=_json.dumps(payload).encode("utf-8"),
                method="POST",
                headers=headers,
            )
            urllib.request.urlopen(req, timeout=5.0).read()
            ingested += 1
        except Exception:  # noqa: BLE001
            skipped += 1
    console.print(f"[green]ingest-md[/green] ingested={ingested} skipped={skipped} root={root}")


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
    import shutil as _shutil
    import sys as _sys

    # On Windows, Claude Code runs hook commands via bash (Git Bash). Bare
    # `memex` only resolves if it's on PATH; absolute Windows paths with
    # backslashes get their backslashes eaten by bash's escape parser
    # (so `C:\Users\…\memex.bat` becomes `C:UsersAdminmemex.bat` — silent
    # 'command not found'). The reliable form is the forward-slash absolute
    # path to memex.bat, which bash treats as a literal path.
    memex_cmd = "memex"
    if _sys.platform == "win32":
        if _shutil.which("memex") is None:
            # Best-effort: locate memex.bat next to the source tree.
            candidate = (Path(__file__).resolve().parents[3] / "memex.bat")
            if candidate.is_file():
                memex_cmd = candidate.as_posix()  # forward slashes for bash
            else:
                # Fall back to `python -m memex` via the active interpreter.
                memex_cmd = f'"{_sys.executable}" -m memex'

    def _c(*args: str) -> str:
        return f"{memex_cmd} {' '.join(args)}"

    block = {
        "hooks": {
            "SessionStart": [
                {
                    "matcher": "*",
                    "hooks": [
                        # One-shot per session: inject memex preamble so Claude
                        # boots with full awareness of installed skills, active
                        # constraints, AFK state, recent corrections, and the
                        # 'use mcp__memex__add_task not TodoWrite' protocol.
                        {"type": "command", "command": _c("hook", "session-start")}
                    ],
                }
            ],
            "UserPromptSubmit": [
                {
                    "matcher": "*",
                    "hooks": [
                        # Active guidance: recall-driven context injection for the turn.
                        {"type": "command", "command": _c("hook", "user-prompt", "--actor=human")}
                    ],
                }
            ],
            "PreToolUse": [
                {
                    "matcher": "*",
                    "hooks": [
                        # Layer-4: consult policies + AFK + hard-deny.
                        # Emits permissionDecision when a policy fires;
                        # otherwise silent (default prompt path runs).
                        {"type": "command", "command": _c("hook", "pre-tool-gate")},
                        {"type": "command", "command": _c("observe-event", "tool_pre")},
                    ],
                }
            ],
            "PostToolUse": [
                {
                    "matcher": "TodoWrite",
                    "hooks": [
                        {"type": "command", "command": _c("todowrite-sync")}
                    ],
                },
                {
                    "matcher": "Edit|Write|MultiEdit",
                    "hooks": [
                        # Keep codebase memory current — re-chunk the edited file.
                        {"type": "command", "command": _c("hook", "post-edit")}
                    ],
                },
                {
                    "matcher": "*",
                    "hooks": [
                        {"type": "command", "command": _c("observe-event", "tool_post")}
                    ],
                },
            ],
            "Stop": [
                {
                    "matcher": "*",
                    "hooks": [
                        {"type": "command", "command": _c("observe-event", "turn_end")}
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


# ----------------------------------------------------------------------------
# Secrets vault — `memex secret` subcommands.
# ----------------------------------------------------------------------------


secret_app = typer.Typer(
    name="secret",
    help="OS-keychain-backed secrets vault. memex stores secrets locally; "
         "the AI layer only ever sees `secret://provider/name` handles.",
    no_args_is_help=True,
)
app.add_typer(secret_app)


def _secrets_store():
    from memex.secrets import SecretsStore
    settings = get_settings()
    return SecretsStore(Path(str(settings.data_dir)) / "secrets")


@secret_app.command("put")
def secret_put_cmd(
    provider: Annotated[str, typer.Argument(help="Provider namespace (e.g. github, openai, tenant.acme).")],
    name: Annotated[str, typer.Argument(help="Secret name within the provider.")],
    value: Annotated[str | None, typer.Option(
        "--value", "-v", help="Secret value. Omit to read from stdin (recommended).",
    )] = None,
) -> None:
    """Store a secret in the OS keychain. Returns just the handle — the
    value never echoes to the terminal or any logs."""
    if value is None:
        # Read from stdin so the value doesn't appear in shell history.
        value = sys.stdin.readline().strip()
        if not value:
            err_console.print("[red]no value provided[/red]")
            raise typer.Exit(code=1)
    store = _secrets_store()
    handle = store.put(provider, name, value)
    console.print(f"[green]stored[/green] {handle}")


@secret_app.command("get")
def secret_get_cmd(
    handle: Annotated[str, typer.Argument(
        help="Either a `secret://provider/name` handle or `provider/name`.",
    )],
    actor: Annotated[str, typer.Option("--actor", help="Audit actor.")] = "human",
) -> None:
    """Resolve a handle to its literal value. Audit trail records who
    asked + when. Use sparingly — the value goes to stdout."""
    from memex.secrets import SecretNotFoundError
    h = handle if handle.startswith("secret://") else f"secret://{handle}"
    store = _secrets_store()
    try:
        value = store.get(h, actor=actor)
    except SecretNotFoundError:
        err_console.print(f"[red]not found:[/red] {h}")
        raise typer.Exit(code=1)
    # Print to stdout WITHOUT a label or trailing newline beyond the value
    # so callers can pipe it directly: `memex secret get foo/bar | curl …`
    sys.stdout.write(value)


@secret_app.command("delete")
def secret_delete_cmd(
    handle: Annotated[str, typer.Argument(help="`secret://provider/name`.")],
) -> None:
    """Remove a secret from the keychain + the index."""
    h = handle if handle.startswith("secret://") else f"secret://{handle}"
    store = _secrets_store()
    removed = store.delete(h)
    if removed:
        console.print(f"[green]deleted[/green] {h}")
    else:
        console.print(f"[yellow]not present[/yellow] {h}")


@secret_app.command("list")
def secret_list_cmd() -> None:
    """List every secret in the vault. NEVER prints values — just the
    metadata (provider, name, created/last-resolved timestamps)."""
    store = _secrets_store()
    rows = store.list()
    if not rows:
        console.print("[dim]vault is empty[/dim]")
        return
    table = Table(title=f"{len(rows)} secret(s)")
    table.add_column("handle", style="cyan")
    table.add_column("created", style="dim")
    table.add_column("last resolved", style="dim")
    table.add_column("by", style="dim")
    for r in rows:
        table.add_row(
            f"secret://{r.provider}/{r.name}",
            r.created_at,
            r.last_resolved_at or "—",
            r.last_resolved_by or "—",
        )
    console.print(table)


@secret_app.command("redact")
def secret_redact_cmd(
    text: Annotated[str | None, typer.Argument(
        help="Text to scan + redact. Omit to read from stdin.",
    )] = None,
    auto_store: Annotated[bool, typer.Option(
        "--auto-store/--preview",
        help="Default: actually move detected secrets into the vault. "
             "--preview just shows what would change without storing.",
    )] = True,
) -> None:
    """Scan text for known secret patterns and replace each match with a
    `secret://provider/name` handle. By default the secret is moved into
    the vault; `--preview` reports without storing.

    Use this for cleaning up logs, transcripts, or any text you're about
    to commit to memex memory.
    """
    from memex.secrets import redact

    if text is None:
        text = sys.stdin.read()
    if auto_store:
        store = _secrets_store()
    else:
        # Preview mode — use a throw-away store under tmp.
        import tempfile
        store = _secrets_store()  # still need the store; preview just reports changes
    result = redact(text, store)
    console.print(result.redacted_text)
    if result.events:
        err_console.print(
            f"[yellow]redacted {len(result.events)} secret(s)[/yellow] — "
            + ", ".join(f"{e.pattern_name} → {e.handle}" for e in result.events)
        )


# ----------------------------------------------------------------------------
# Hook commands — invoked by Claude Code's hook handlers. Each reads the
# event JSON on stdin and emits hook-output JSON on stdout (or exits 0 with
# nothing). Latency budget: <50ms — these run on the request hot path.
# ----------------------------------------------------------------------------


hook_app = typer.Typer(
    name="hook",
    help="Hook handlers for Claude Code (and other AI tools). Each command "
         "reads the hook's JSON payload on stdin and emits the response on stdout.",
    no_args_is_help=True,
)
app.add_typer(hook_app)


def _read_hook_stdin() -> dict:
    """Read + parse the hook's JSON event payload from stdin. Returns
    an empty dict on parse failure — hooks must never break the session."""
    import json as _json
    import sys as _sys
    raw = _sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        return _json.loads(raw)
    except _json.JSONDecodeError as e:
        # Loud-but-non-fatal: log to stderr, return empty so Claude Code
        # falls through to its default behavior.
        err_console.print(f"[red]hook: invalid JSON on stdin:[/red] {e}")
        return {}


def _emit_hook_output(payload: dict) -> None:
    """Write the JSON response Claude Code reads from the hook's stdout."""
    import json as _json
    import sys as _sys
    _sys.stdout.write(_json.dumps(payload))
    _sys.stdout.write("\n")
    _sys.stdout.flush()


@hook_app.command("user-prompt")
def hook_user_prompt(
    actor: Annotated[str, typer.Option("--actor")] = "human",
    budget_tokens: Annotated[int, typer.Option(
        "--budget", help="Token budget for the recall call.",
    )] = 600,
    expand_hops: Annotated[int, typer.Option(
        "--expand-hops", help="Edges of neighborhood to walk in recall.",
    )] = 1,
) -> None:
    """UserPromptSubmit hook — observe the prompt + inject relevant memex
    recall as `additionalContext` so Claude sees prior decisions /
    constraints / corrections / matching code symbols before responding.

    This is the active-guidance entry point: each turn, memex pulls
    what it knows about the prompt's topic into Claude's context for free.
    """
    import json as _json

    event = _read_hook_stdin()
    prompt_text = (
        event.get("prompt")
        or event.get("user_prompt")
        or event.get("text")
        or ""
    )
    if not isinstance(prompt_text, str):
        prompt_text = str(prompt_text)
    prompt_text = prompt_text.strip()
    if not prompt_text:
        return

    # Observe the prompt with full provenance — user identity + project
    # + ticket (when detectable). Lets "what did user X work on in project
    # Y last week" be a single recall query, not an LLM scan of every
    # prompt's free text.
    settings = get_settings()
    payload = _scrub_event_payload(event)
    payload["user_id"] = _user_id_from_context()
    payload["project"] = _project_from_context(event)
    payload["ticket"] = _ticket_from_context(event)
    additional_context: str | None = None

    try:
        from memex.frontends.mcp.client import MemexClient
        from memex.frontends.mcp.daemon import (
            daemon_url, ensure_daemon, is_daemon_alive,
        )
        url = daemon_url(settings)
        if not settings.daemon_url and not is_daemon_alive(url, settings.auth_token):
            url = ensure_daemon(settings)
        with MemexClient(base_url=url, auth_token=settings.auth_token, timeout=3.0) as c:
            c.observe(kind="user_prompt", actor=actor, payload=payload)
            # Auto-classify corrections — the original `user_correction`
            # counter only ticked when an agent explicitly observed it,
            # which never happens. Pattern-match common corrective
            # phrasings so the dashboard reflects real friction.
            if _looks_like_correction(prompt_text):
                c.observe(
                    kind="user_correction",
                    actor=actor,
                    payload={**payload, "auto_detected": True, "snippet": prompt_text[:240]},
                )
            recall_result = c.recall(
                query=prompt_text,
                budget_tokens=budget_tokens,
                expand_hops=expand_hops,
            )
            additional_context = _format_recall_for_context(recall_result)
    except Exception as e:  # noqa: BLE001
        log = logging.getLogger(__name__)
        log.warning("hook user-prompt: recall failed: %s", e)
        return

    if additional_context:
        # Episodic stamp so the dashboard can show 'tokens auto-injected
        # this session' — closes the loop on 'are we saving tokens'.
        try:
            with MemexClient(base_url=url, auth_token=settings.auth_token, timeout=2.0) as c2:
                c2.observe(
                    kind="context_injected",
                    actor=actor,
                    payload={
                        "source": "user-prompt",
                        "chars": len(additional_context),
                        "est_tokens": len(additional_context) // 4,
                    },
                )
        except Exception:  # noqa: BLE001
            pass
        _emit_hook_output({"additionalContext": additional_context})


_CORRECTION_PATTERNS = (
    # Negation + directive
    "don't ", "dont ", "do not ",
    "stop ", "no don", "no, don",
    "never ",
    "instead of ", "not that", "not like that", "not like this",
    "wrong", " incorrect", "that's wrong", "thats wrong",
    "that's not", "thats not",
    # Course-correction
    "actually ",
    "i said ", "i told you ", "i meant ",
    "no — ", "no - ", "no, ",
    # Negative quality
    "badly done", "is not done", "is not working",
    "broken", "is broken", "doesn't work", "doesnt work",
    "not working", "still not", "still asking",
    # User feedback / preference
    "should be ", "use ", "from now on", "going forward",
    "prefer ", "rather than",
    # Direct corrections of agent
    "you should", "you shouldn't", "you should not",
    "fix this", "fix that", "this is wrong",
)


def _looks_like_correction(text: str) -> bool:
    """Heuristic — does this user prompt read like a correction or a
    rule-from-now-on? Triggers on common negation / directive phrasings.

    Conservative on purpose: false positives are cheap (one extra event
    in the stream), false negatives are what we want to avoid since the
    whole point of the dashboard counter is showing real friction. If the
    prompt is short and contains *any* trigger word it counts; if it's
    long, we require a stronger signal."""
    if not text:
        return False
    t = text.lower().strip()
    # Very short "no" / "stop" / "wrong" responses are clearly corrective.
    if len(t) <= 12 and t in {
        "no", "nope", "nah", "stop", "wrong", "not right",
        "incorrect", "no don't", "no dont",
    }:
        return True
    for needle in _CORRECTION_PATTERNS:
        if needle in t:
            return True
    return False


def _format_recall_for_context(result: dict | object) -> str:
    """Render a recall result as compact prose suitable for
    additionalContext injection. Empty when there's nothing relevant."""
    if hasattr(result, "to_dict"):
        d = result.to_dict()
    elif isinstance(result, dict):
        d = result
    else:
        return ""
    nodes = d.get("nodes") or []
    if not nodes:
        return ""
    lines = ["[memex recall — relevant prior knowledge]"]
    for n in nodes[:8]:
        kind = n.get("kind", "fact")
        name = n.get("name", "")
        desc = (n.get("description") or "").strip()
        first_line = desc.split("\n", 1)[0][:200] if desc else ""
        lines.append(f"- ({kind}) {name}" + (f": {first_line}" if first_line else ""))
    if d.get("degraded"):
        reason = d.get("degraded_reason") or ""
        lines.append(f"[memex degraded: {reason}]")
    return "\n".join(lines)


@hook_app.command("session-start")
def hook_session_start(
    budget_tokens: Annotated[int, typer.Option(
        "--budget", help="Soft cap on injected preamble length.",
    )] = 1200,
) -> None:
    """SessionStart hook — inject a memex-aware preamble at the top of
    every Claude Code session so the model knows: which tools memex
    exposes, which skills are installed, which constraints/corrections
    govern this user, whether AFK is on, and the must-follow protocol
    (recall first, observe corrections, use memex add_task not TodoWrite).

    Wires via:
      "SessionStart": [{"matcher": "*",
                         "hooks": [{"type": "command",
                                    "command": "memex hook session-start"}]}]
    """
    import json as _json
    from datetime import datetime as _dt, timezone as _tz

    settings = get_settings()
    try:
        from memex.frontends.mcp.client import MemexClient
        from memex.frontends.mcp.daemon import (
            daemon_url, ensure_daemon, is_daemon_alive,
        )
        url = daemon_url(settings)
        if not settings.daemon_url and not is_daemon_alive(url, settings.auth_token):
            url = ensure_daemon(settings)
    except Exception as e:  # noqa: BLE001
        log = logging.getLogger(__name__)
        log.warning("hook session-start: daemon unreachable: %s", e)
        return

    parts: list[str] = ["[memex session preamble — your persistent memory is active]"]

    # ---- 1. Protocol -----------------------------------------------------
    # Hard rules the model should treat as ambient instructions every turn.
    parts.append(
        "\nProtocol:\n"
        "  • RECALL FIRST: before saying 'I don't know X', call mcp__memex__recall(query=...).\n"
        "  • REMEMBER AFTER: when the user states a decision, constraint, or fact, "
        "call mcp__memex__add_node(name, kind='decision'|'constraint'|'fact', description).\n"
        "  • OBSERVE CORRECTIONS: when the user corrects you, call "
        "mcp__memex__observe(kind='user_correction', payload={snippet, why}).\n"
        "  • USE TASKS, NOT TodoWrite: track persistent work via mcp__memex__add_task / "
        "update_task — they survive across sessions.\n"
        "  • VALIDATE BEFORE GENERATING: when work touches a registered skill domain, "
        "call mcp__memex__validate(skill=<name>) and self-attest the checks."
    )

    # ---- 2. AFK + enforcement state -------------------------------------
    try:
        with MemexClient(base_url=url, auth_token=settings.auth_token, timeout=2.5) as c:
            try:
                afk = c._http.get("/afk").json()
            except Exception:  # noqa: BLE001
                afk = {"active": False}
            if afk.get("active"):
                until = afk.get("expires_at") or "—"
                note = afk.get("note") or ""
                parts.append(
                    f"\nAFK MODE IS ON until {until}. {note}\n"
                    "  → Tool calls auto-approve via the PreToolUse hook unless they hit a hard-deny pattern.\n"
                    "  → Don't ask the user before running standard read / search / edit / git tools — proceed."
                )
            else:
                parts.append("\nAFK is off — defer to standard PreToolUse policy / user prompts.")

            # ---- 3. Active corrections + constraints ------------------
            recent_corr = c.recall(
                query="user correction rule from now on",
                budget_tokens=300, expand_hops=0,
            )
            corr_nodes = []
            try:
                corr_d = recent_corr.to_dict() if hasattr(recent_corr, "to_dict") else recent_corr
                corr_nodes = corr_d.get("nodes", []) if isinstance(corr_d, dict) else []
            except Exception:  # noqa: BLE001
                pass
            constraint_nodes = c.find_by_kind("constraint")
            if constraint_nodes:
                parts.append("\nActive constraints (must hold):")
                for n in constraint_nodes[:10]:
                    desc = (n.description or "").strip().split("\n", 1)[0][:200]
                    parts.append(f"  • {n.name} — {desc}")

            # ---- 4. Recent decisions ----------------------------------
            decision_nodes = c.find_by_kind("decision")
            if decision_nodes:
                # Prefer most-recently-confirmed / created.
                decision_nodes_sorted = sorted(
                    decision_nodes,
                    key=lambda d: (d.last_confirmed_at or d.created_at or _dt.now(_tz.utc)),
                    reverse=True,
                )
                parts.append("\nRecent decisions (architectural commitments):")
                for n in decision_nodes_sorted[:6]:
                    desc = (n.description or "").strip().split("\n", 1)[0][:200]
                    parts.append(f"  • {n.name} — {desc}")

            # ---- 5. User profile + skills installed -------------------
            people = c.find_by_kind("person")
            if people:
                p = people[0]
                desc = (p.description or "").strip().split("\n", 1)[0][:200]
                parts.append(f"\nUser: {p.name} — {desc}")

            try:
                skills = c.list_skills()
                if skills:
                    parts.append("\nInstalled skills (call validate(skill=…) when relevant):")
                    for s in skills[:12]:
                        bundle = s.get("name") or s.get("skill") or "?"
                        approaches = s.get("approaches") or []
                        if approaches:
                            parts.append(
                                f"  • {bundle} → " + ", ".join(a.get("name", "?") for a in approaches[:4])
                            )
                        else:
                            parts.append(f"  • {bundle}")
            except Exception:  # noqa: BLE001
                pass

            # ---- 6. Fresh corrections (from recall semantic match) ---
            if corr_nodes:
                parts.append(
                    "\nRecent feedback the user has given (don't repeat the same mistakes):"
                )
                for n in corr_nodes[:5]:
                    name = n.get("name") if isinstance(n, dict) else getattr(n, "name", "?")
                    desc = (
                        n.get("description") if isinstance(n, dict)
                        else getattr(n, "description", "")
                    )
                    desc = (desc or "").strip().split("\n", 1)[0][:200]
                    parts.append(f"  • {name}: {desc}")

    except Exception as e:  # noqa: BLE001
        log = logging.getLogger(__name__)
        log.warning("hook session-start: build preamble failed: %s", e)

    text = "\n".join(parts)
    # Soft cap so we don't dump 10k tokens of constraints into every session.
    if len(text) > budget_tokens * 4:  # ~4 chars per token, conservative
        text = text[: budget_tokens * 4] + "\n[truncated — open the desktop app for full memory]"

    if text.strip():
        try:
            with MemexClient(base_url=url, auth_token=settings.auth_token, timeout=2.0) as c2:
                c2.observe(
                    kind="context_injected",
                    actor="agent",
                    payload={
                        "source": "session-start",
                        "chars": len(text),
                        "est_tokens": len(text) // 4,
                    },
                )
        except Exception:  # noqa: BLE001
            pass
        _emit_hook_output({"additionalContext": text})


@hook_app.command("post-edit")
def hook_post_edit() -> None:
    """PostToolUse hook (matcher Edit|Write|MultiEdit) — re-chunk the
    edited file in memex's codebase memory so subsequent recalls see
    the new structure. Idempotent + cheap (single-file, <100ms typical).
    No output — observation-only.
    """
    event = _read_hook_stdin()
    tool_input = event.get("tool_input") or {}
    file_path = (
        tool_input.get("file_path")
        or tool_input.get("path")
        or event.get("file_path")
    )
    if not file_path or not isinstance(file_path, str):
        return

    settings = get_settings()
    try:
        from memex.codebase import reindex_file as _reindex_file
        engine = Engine.build_default(settings)
    except Exception as e:  # noqa: BLE001
        log = logging.getLogger(__name__)
        log.warning("hook post-edit: engine init failed: %s", e)
        return
    try:
        result = _reindex_file(engine, file_path)
        if result is None:
            # Path wasn't under any registered source — silent ignore.
            return
        engine.observe(
            kind="file_reindexed",
            actor=Source.agent,
            payload={
                "rel_path": result.rel_path,
                "source_id": result.source_id,
                "symbols_before": result.symbols_before,
                "symbols_after": result.symbols_after,
                "skipped": result.skipped,
                "skip_reason": result.skip_reason,
            },
        )
    except Exception as e:  # noqa: BLE001
        log = logging.getLogger(__name__)
        log.warning("hook post-edit: reindex_file failed: %s", e)
    finally:
        engine.close()


# ----------------------------------------------------------------------------
# Layer-4 enforcement — `memex policy` and `memex afk` subcommands.
# ----------------------------------------------------------------------------


policy_app = typer.Typer(
    name="policy",
    help="Approval policies — auto-approve or auto-deny tool calls memex sees "
         "via the PreToolUse hook. Conservative by default; user opts in.",
    no_args_is_help=True,
)
app.add_typer(policy_app)


@policy_app.command("add")
def policy_add_cmd(
    tool_pattern: Annotated[str, typer.Argument(
        help="Regex matching the tool name (e.g. 'Bash', 'Edit|Write', '.*').",
    )],
    decision: Annotated[str, typer.Option(
        "--decision", help="approve | deny",
    )] = "approve",
    args_json: Annotated[str, typer.Option(
        "--args", help="JSON object of args matchers, e.g. "
                       "'{\"command\": {\"prefix\": \"npm \"}}'.",
    )] = "{}",
    reason: Annotated[str, typer.Option("--reason", help="Why this policy.")] = "",
    priority: Annotated[int, typer.Option("--priority")] = 100,
    hard_deny: Annotated[bool, typer.Option(
        "--hard-deny", help="Mark as hard-deny — bypasses AFK mode.",
    )] = False,
) -> None:
    """Add an approval policy."""
    import json as _json
    from memex.enforcement import ApprovalDecision, add_policy

    try:
        args_match = _json.loads(args_json)
    except _json.JSONDecodeError as e:
        err_console.print(f"[red]invalid --args JSON:[/red] {e}")
        raise typer.Exit(code=2) from None

    settings = get_settings()
    engine = Engine.build_default(settings)
    try:
        if hard_deny:
            # Use the constraint kind directly with policy_type=hard_deny so
            # _check_hard_deny picks it up.
            from memex.core.schema import NodeKind, Source as SrcActor
            engine.add(
                name=f"hard-deny: {tool_pattern} {args_match}",
                description=reason or "user-defined hard deny",
                kind=NodeKind.constraint,
                source=SrcActor.human,
                metadata={
                    "policy_type": "hard_deny",
                    "tool_pattern": tool_pattern,
                    "args_match": args_match,
                    "reason": reason,
                    "enabled": True,
                },
            )
            console.print(f"[red]hard-deny[/red] {tool_pattern} {args_match}")
            return
        c = add_policy(
            engine,
            tool_pattern=tool_pattern,
            decision=ApprovalDecision(decision),
            args_match=args_match,
            reason=reason,
            priority=priority,
        )
        console.print(f"[green]added policy[/green] [cyan]{c.id}[/cyan] "
                      f"({decision} {tool_pattern})")
    finally:
        engine.close()


@policy_app.command("list")
def policy_list_cmd() -> None:
    """List active approval policies (sorted by priority)."""
    from memex.enforcement import list_policies

    settings = get_settings()
    engine = Engine.build_default(settings)
    try:
        rows = list_policies(engine)
        if not rows:
            console.print("[dim]no policies registered[/dim]")
            return
        table = Table(title=f"{len(rows)} policy/policies")
        table.add_column("id", style="cyan")
        table.add_column("decision", style="bold")
        table.add_column("tool")
        table.add_column("args matcher", style="dim")
        table.add_column("reason", style="dim")
        for p in rows:
            colour = "green" if p.decision.value == "approve" else "red"
            table.add_row(
                p.id, f"[{colour}]{p.decision.value}[/{colour}]",
                p.tool_pattern, str(p.args_match), p.reason,
            )
        console.print(table)
    finally:
        engine.close()


@policy_app.command("remove")
def policy_remove_cmd(
    policy_id: Annotated[str, typer.Argument(help="Policy concept id (c_…)")],
) -> None:
    from memex.enforcement import remove_policy

    settings = get_settings()
    engine = Engine.build_default(settings)
    try:
        ok = remove_policy(engine, policy_id)
        if ok:
            console.print(f"[green]removed[/green] {policy_id}")
        else:
            console.print(f"[yellow]not found[/yellow] {policy_id}")
    finally:
        engine.close()


afk_app = typer.Typer(
    name="afk",
    help="AFK mode — auto-approve every tool call (except hard-deny) for a "
         "set duration so the AI works unattended; user audits on return.",
    no_args_is_help=True,
)
app.add_typer(afk_app)


@afk_app.command("on")
def afk_on_cmd(
    hours: Annotated[float, typer.Option(
        "--for", help="Duration in hours.",
    )] = 4.0,
    note: Annotated[str, typer.Option(
        "--note", help="Why AFK is on (so the user remembers later).",
    )] = "",
) -> None:
    """Enable AFK mode."""
    from memex.enforcement import enable_afk_mode

    settings = get_settings()
    engine = Engine.build_default(settings)
    try:
        flag = enable_afk_mode(engine, duration_hours=hours, note=note)
        console.print(
            f"[green]AFK mode on[/green] (expires "
            f"{flag.metadata.get('expires_at')})\n"
            f"[dim]auto-approves every tool call EXCEPT hard-deny patterns[/dim]"
        )
    finally:
        engine.close()


@afk_app.command("off")
def afk_off_cmd() -> None:
    from memex.enforcement import disable_afk_mode

    settings = get_settings()
    engine = Engine.build_default(settings)
    try:
        if disable_afk_mode(engine):
            console.print("[green]AFK mode off[/green]")
        else:
            console.print("[dim]AFK mode was not active[/dim]")
    finally:
        engine.close()


@afk_app.command("status")
def afk_status_cmd() -> None:
    from memex.enforcement import afk_status

    settings = get_settings()
    engine = Engine.build_default(settings)
    try:
        st = afk_status(engine)
        if st is None:
            console.print("[dim]AFK mode is off[/dim]")
            return
        console.print(
            f"[green]AFK mode active[/green] until {st['expires_at']}\n"
            f"  note: [yellow]{st['note']}[/yellow]"
        )
    finally:
        engine.close()


def _user_id_from_context() -> str | None:
    """Resolve the human's identity for event provenance.

    Precedence:
      1. $MEMEX_USER env var (explicit override; lets teams set a
         canonical id even when git config is per-machine)
      2. `git config --global user.email` — most reliable on a dev box
      3. $USER / $USERNAME — final fallback

    Cached across hook invocations via _USER_ID_CACHE so we don't fork
    git on every tool call.
    """
    global _USER_ID_CACHE
    if _USER_ID_CACHE is not None:
        return _USER_ID_CACHE or None  # empty-string cache means "tried, none"
    v = os.environ.get("MEMEX_USER", "").strip()
    if v:
        _USER_ID_CACHE = v
        return v
    try:
        import subprocess
        r = subprocess.run(
            ["git", "config", "--global", "user.email"],
            capture_output=True, text=True, timeout=1.0,
        )
        if r.returncode == 0:
            v = (r.stdout or "").strip()
            if v:
                _USER_ID_CACHE = v
                return v
    except Exception:  # noqa: BLE001
        pass
    v = (
        os.environ.get("USER")
        or os.environ.get("USERNAME")
        or ""
    ).strip()
    _USER_ID_CACHE = v
    return v or None


_USER_ID_CACHE: str | None = None


def _ticket_from_context(event: dict) -> str | None:
    """Resolve the active ticket id for prompt/task provenance.

    Precedence:
      1. $MEMEX_TICKET env var (explicit override)
      2. `.memex.json` in cwd with `{ticket: "..."}`
      3. Current git branch parsed for a JIRA-style ticket (AUTH-123,
         AYUSH-7, MEM-42, etc.). Pattern: [A-Z]+-\\d+
      4. None — work isn't tagged to a ticket, that's fine

    Cheap: cached across hook invocations.
    """
    global _TICKET_CACHE
    if _TICKET_CACHE is not None:
        return _TICKET_CACHE or None
    v = os.environ.get("MEMEX_TICKET", "").strip()
    if v:
        _TICKET_CACHE = v
        return v
    cwd = (event or {}).get("cwd") or os.getcwd()
    try:
        cfg = Path(cwd) / ".memex.json"
        if cfg.is_file():
            import json as _json
            data = _json.loads(cfg.read_text(encoding="utf-8"))
            v = data.get("ticket")
            if isinstance(v, str) and v:
                _TICKET_CACHE = v
                return v
    except Exception:  # noqa: BLE001
        pass
    try:
        import subprocess
        r = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=1.0,
        )
        if r.returncode == 0:
            branch = (r.stdout or "").strip()
            import re as _re
            m = _re.search(r"\b([A-Z]+-\d+)\b", branch)
            if m:
                _TICKET_CACHE = m.group(1)
                return _TICKET_CACHE
    except Exception:  # noqa: BLE001
        pass
    _TICKET_CACHE = ""
    return None


_TICKET_CACHE: str | None = None


def _project_from_context(event: dict) -> str | None:
    """Resolve the current project slug for the action_constraint gate.

    Precedence:
      1. MEMEX_PROJECT env var (explicit override)
      2. ./.memex.json or ./memex.toml in the hook's cwd has {project: ...}
      3. cwd directory name (basename of `cwd`) — covers the common case
         where each project lives in its own checkout
      4. None — global rules still fire; project-scoped rules don't

    Keeps gate behavior predictable: same cwd → same project resolution.
    """
    p = os.environ.get("MEMEX_PROJECT")
    if p:
        return p
    cwd = (event or {}).get("cwd") or os.getcwd()
    try:
        # Honor a per-project memex marker file when present.
        cfg = Path(cwd) / ".memex.json"
        if cfg.is_file():
            import json as _json
            data = _json.loads(cfg.read_text(encoding="utf-8"))
            v = data.get("project")
            if isinstance(v, str) and v:
                return v
    except Exception:  # noqa: BLE001
        pass
    try:
        base = Path(cwd).name
        if base and base != "/":
            return base
    except Exception:  # noqa: BLE001
        pass
    return None


def _intent_from_tool_call(tool_name: str, tool_input: dict) -> str:
    """Construct a natural-language intent string for check_action.

    Picks the most signal-bearing field per tool so the constraint
    recall has something to match against. Falls back to the raw
    payload's first value when the tool is unknown.
    """
    if not tool_name:
        return ""
    n = tool_name.lower()
    if n in {"bash", "shell"}:
        cmd = tool_input.get("command") or ""
        return f"run shell command: {cmd}"[:400]
    if n in {"edit", "write", "multiedit", "notebookedit"}:
        path = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
        return f"{tool_name} the file: {path}"[:400]
    if n == "read":
        path = tool_input.get("file_path") or ""
        return f"read the file: {path}"[:400]
    if n == "webfetch":
        url = tool_input.get("url") or ""
        return f"fetch web URL: {url}"[:400]
    if n == "websearch":
        q = tool_input.get("query") or ""
        return f"web search: {q}"[:400]
    # Unknown tool — concatenate first ~3 string fields as a fallback
    # signal. Order isn't stable but that's fine; recall is robust to
    # token order.
    parts: list[str] = [tool_name]
    for k, v in (tool_input or {}).items():
        if isinstance(v, str) and v:
            parts.append(f"{k}={v[:80]}")
            if len(parts) >= 4:
                break
    return " ".join(parts)[:400]


@hook_app.command("pre-tool-gate")
def hook_pre_tool_gate() -> None:
    """PreToolUse hook — consult should_approve and emit Claude Code's
    permissionDecision JSON. This is what gates dangerous tools without
    bothering the user when memex has a policy stored, AND blocks
    catastrophic patterns even when the user is AFK.

    Output shapes:
      approve → {"permissionDecision": "allow", "permissionDecisionReason": "..."}
      deny    → {"permissionDecision": "deny",  "permissionDecisionReason": "..."}
      ask     → no output (default user prompt fires)
    """
    event = _read_hook_stdin()
    tool_name = event.get("tool_name")
    if not tool_name or not isinstance(tool_name, str):
        return
    tool_input = event.get("tool_input") or {}

    settings = get_settings()
    decision = "ask"
    reason = ""
    # Route through the daemon's HTTP /should-approve endpoint — opening
    # our own Engine here contests the DuckDB write lock, fails silently,
    # and Claude Code falls through to a user prompt even in AFK mode.
    try:
        import httpx as _httpx
        from memex.frontends.mcp.daemon import (
            daemon_url, ensure_daemon, is_daemon_alive,
        )
        url = daemon_url(settings)
        if not settings.daemon_url and not is_daemon_alive(url, settings.auth_token):
            url = ensure_daemon(settings)
        headers = (
            {"Authorization": f"Bearer {settings.auth_token}"}
            if settings.auth_token else {}
        )
        # First gate: semantic constraint check (IP claim 3 / check_action).
        # Builds an intent string from tool_name + the most signal-bearing
        # fields of tool_input, then asks the daemon if any high-confidence
        # constraint in memory matches. Hard-deny verdicts here win over
        # the hard-deny pattern gate below — semantic constraints carry
        # the user's stated rules, not just heuristics.
        #
        # AFK mode bypasses this gate. The check_action recall is semantic
        # and can mis-fire on prose principles classified as `constraint`;
        # under AFK the user has explicitly accepted that risk for the
        # session. should_approve below still runs and enforces hard-deny.
        # Also honors MEMEX_DISABLE_CHECK_ACTION=1 as a manual kill switch.
        skip_check_action = (
            os.environ.get("MEMEX_DISABLE_CHECK_ACTION", "").lower()
            in {"1", "true", "yes"}
        )
        if not skip_check_action:
            try:
                afk_r = _httpx.get(
                    url + "/afk", headers=headers, timeout=1.0,
                )
                if afk_r.status_code == 200 and afk_r.json():
                    skip_check_action = True
            except Exception:  # noqa: BLE001
                pass  # fall through; better to gate than to fail-open silently
        intent = _intent_from_tool_call(tool_name, tool_input)
        # Resolve project context once; both gates use it (action_constraint
        # rules can be global or project-scoped). Cheap — cwd + env read.
        project = _project_from_context(event)
        if intent and not skip_check_action:
            try:
                rc = _httpx.post(
                    url + "/check_action",
                    json={"intent": intent, "project": project},
                    headers=headers,
                    timeout=2.0,  # tighter than should-approve; falls through quietly
                )
                if rc.status_code == 200:
                    cdata = rc.json()
                    cdecision = cdata.get("decision")
                    if cdecision in ("deny", "step_up"):
                        # step_up = "memex isn't sure, ask the user".
                        # Claude Code has three permissionDecision values:
                        # allow / deny / (no output = ask). For step_up we
                        # emit deny with a reason that explicitly invites
                        # confirmation — the user can re-approve via the
                        # next prompt. This is the "active questioning"
                        # path: memex interrupts the AI, hands the choice
                        # back to the human, and the human's response
                        # becomes a calibration signal.
                        decision = "deny" if cdecision == "deny" else "step_up"
                        reasons = cdata.get("reasons") or []
                        if reasons:
                            top = reasons[0]
                            name = top.get("name", "")
                            cid = top.get("constraint_id", "")
                            conf = top.get("confidence", 0.0)
                            snippet = (top.get("snippet") or "")[:160]
                            verb = "matched" if cdecision == "deny" else "needs confirmation for"
                            reason = (
                                f"action_constraint {verb}: \"{name}\""
                                f" (id={cid}, conf={conf:.2f})"
                            )
                            if snippet:
                                reason += f" — {snippet}"
                            if cdecision == "step_up":
                                reason += (
                                    " — memex requests user confirmation; "
                                    "re-prompt the user to approve or update memory"
                                )
                            if len(reasons) > 1:
                                reason += f" (+{len(reasons)-1} more)"
                        else:
                            reason = f"action_constraint {cdecision} with no reason returned"
            except Exception as e:  # noqa: BLE001
                # Soft-fail: a check_action timeout/error must not block
                # tool calls. The pattern-based should-approve gate runs next.
                logging.getLogger(__name__).debug(
                    "check_action skipped: %s", e
                )

        # Second gate: pattern-based should-approve (existing behavior).
        # Only consulted when check_action didn't already issue a deny.
        if decision != "deny":
            r = _httpx.post(
                url + "/should-approve",
                json={"tool_name": tool_name, "tool_input": tool_input},
                headers=headers,
                timeout=3.0,
            )
            if r.status_code == 200:
                data = r.json()
                decision = data.get("decision", "ask")
                reason = data.get("reason", "")
    except Exception as e:  # noqa: BLE001
        log = logging.getLogger(__name__)
        log.warning("hook pre-tool-gate: daemon call failed: %s", e)
        return

    # Claude Code's documented PreToolUse hook output shape — emit BOTH
    # the wrapped `hookSpecificOutput` form AND the flat top-level
    # `permissionDecision` so different Claude Code versions both honor
    # the call. The wrapped form is the canonical 2025+ shape.
    if decision == "approve":
        _emit_hook_output({
            "permissionDecision": "allow",
            "permissionDecisionReason": f"memex: {reason}",
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
                "permissionDecisionReason": f"memex: {reason}",
            },
        })
    elif decision == "deny":
        _emit_hook_output({
            "permissionDecision": "deny",
            "permissionDecisionReason": f"memex blocked: {reason}",
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": f"memex blocked: {reason}",
            },
        })
    elif decision == "step_up":
        # Translate step_up to Claude Code's deny + a reason that asks
        # the user for confirmation. The user's next prompt that approves
        # the action becomes a calibration signal — memex observes the
        # user_correction → reduces the constraint's confidence. This is
        # the active-questioning loop in practice.
        _emit_hook_output({
            "permissionDecision": "deny",
            "permissionDecisionReason": f"memex step_up: {reason}",
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": f"memex step_up: {reason}",
            },
        })
    # ask → no output (default user-prompt behavior fires)


if __name__ == "__main__":
    app()
