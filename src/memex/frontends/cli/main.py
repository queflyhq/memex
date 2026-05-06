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
def doctor() -> None:
    """Diagnose installation, paths, and tier availability."""
    settings = get_settings()
    settings.ensure_dirs()
    from memex.ml.embeddings import build_default_provider

    provider = build_default_provider(model_name=settings.embed_model, dim=settings.embed_dim)
    embed_ok = provider.is_available()

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
        "[green]installed[/green]" if embed_ok else "[dim]not installed[/dim]",
    )
    table.add_row("embed model", settings.embed_model)
    console.print(table)
    if not embed_ok:
        console.print(
            "\n[dim]Tip: install Tier 1 embeddings for vector search:[/dim]\n"
            "  [bold]pipx install --force 'memex[embed]'[/bold]"
        )


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
def serve() -> None:
    """Run the MCP stdio server (for AI editors that pipe stdio)."""
    _setup_logging()
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
def version() -> None:
    """Print the memex version."""
    console.print(f"memex {__version__}")


if __name__ == "__main__":
    app()
