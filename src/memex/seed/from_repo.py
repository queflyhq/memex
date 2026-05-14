"""Scan a repository and populate memex with what's already there.

What gets extracted, in roughly decreasing signal:

  1. **The project itself** — one `kind=project` concept named after the repo dir
  2. **ADRs / decision docs** — every file under `docs/decisions/`, `docs/adrs/`,
     or matching `DECISIONS*.md` / `ADR-*.md` / `RFC-*.md` becomes one
     `kind=decision` concept
  3. **README / CONTRIBUTING / ARCHITECTURE / DESIGN** — top-level docs get
     parsed; bullet lists under headings like "Tech stack" become facts
  4. **CHANGELOG** — each version section becomes one decision dated to release
  5. **Manifest files** — pyproject.toml, package.json, Cargo.toml, go.mod,
     requirements.txt extracted as tech-stack facts ("uses fastapi 0.110+")
  6. **Git history** — every meaningful commit (`feat:` / `fix:` / `refactor:` /
     `BREAKING:`) becomes a `kind=decision` with date + author provenance.
     Trivial commits (typos, version bumps, merges) skipped.
  7. **Code symbols** — handed off to the existing ingest-repos flow

Everything links back to the project concept via `part_of` edges so the
user can navigate from project → all related facts/decisions.

Heuristic only. No LLM required. ~3-10 seconds for a typical repo.
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from memex.core.schema import Concept, Edge, EdgeKind, NodeKind, Source

if TYPE_CHECKING:
    from memex.core.engine import Engine

log = logging.getLogger(__name__)


@dataclass
class SeedReport:
    project_id: str | None = None
    decisions_added: int = 0
    facts_added: int = 0
    people_added: int = 0
    commits_scanned: int = 0
    commits_kept: int = 0
    docs_scanned: int = 0
    manifests_scanned: int = 0
    code_indexed: bool = False
    errors: list[str] = field(default_factory=list)

    def total_added(self) -> int:
        return self.decisions_added + self.facts_added + self.people_added


# ---- file matchers --------------------------------------------------------

_DECISION_FILE_PATTERNS = [
    re.compile(r"^DECISIONS?\.md$", re.I),
    re.compile(r"^ADR[-_].*\.md$", re.I),
    re.compile(r"^RFC[-_].*\.md$", re.I),
    re.compile(r"^docs/(decisions|adrs?)/.*\.md$", re.I),
]
_TOP_DOC_NAMES = {
    "README.md", "README.rst", "README.txt",
    "CONTRIBUTING.md", "ARCHITECTURE.md", "DESIGN.md",
    "OVERVIEW.md", "PHILOSOPHY.md", "PRINCIPLES.md",
}
_CHANGELOG_NAMES = {"CHANGELOG.md", "CHANGES.md", "HISTORY.md", "NEWS.md"}
_MANIFEST_NAMES = {
    "pyproject.toml", "setup.py", "setup.cfg", "requirements.txt",
    "package.json", "Cargo.toml", "go.mod", "build.gradle",
    "pom.xml", "Gemfile", "composer.json",
}

# Commit subjects we silently drop — pure noise.
_NOISE_COMMIT_PATTERNS = [
    re.compile(r"^Merge ", re.I),
    re.compile(r"^WIP", re.I),
    re.compile(r"^fix typo", re.I),
    re.compile(r"^typo", re.I),
    re.compile(r"^bump version", re.I),
    re.compile(r"^chore: release", re.I),
    re.compile(r"^Revert ", re.I),
    re.compile(r"^v?\d+\.\d+\.\d+$"),
]
_DECISION_COMMIT_RE = re.compile(
    r"^(feat|fix|BREAKING|breaking|refactor|perf|api|polish|chore|docs|test|ci|build|style)\b[:!]",
    re.I,
)
# Fallback: accept any commit whose subject is meaningful (long enough,
# starts with an English verb, no obvious noise pattern). Lets us catch
# real decisions from repos that don't use conventional-commit style.
_MEANINGFUL_VERB_RE = re.compile(
    r"^(Add|Adds|Adding|Update|Updates|Updating|Fix|Fixes|Fixing|"
    r"Remove|Removes|Removing|Delete|Deletes|Refactor|Refactors|"
    r"Implement|Implements|Migrate|Migrates|Switch|Switches|Replace|"
    r"Replaces|Rename|Renames|Move|Moves|Build|Builds|Wire|Wires|Ship|"
    r"Drop|Drops|Enable|Enables|Disable|Disables|Introduce|Introduces|"
    r"Support|Supports|Allow|Allows|Restore|Restores)\s+",
    re.I,
)


def seed_from_repo(
    engine: "Engine",
    repo_path: str | Path,
    *,
    max_commits: int = 500,
    include_code_symbols: bool = True,
    dry_run: bool = False,
) -> SeedReport:
    """Scan a repo and populate memex. See module docstring for details.

    Parameters:
      engine: live memex Engine
      repo_path: filesystem path to the repo (must be a git working tree
        for commit extraction; non-git dirs work but skip git history)
      max_commits: cap on commits scanned
      include_code_symbols: handle off to ingest-repos for AST indexing
      dry_run: report only, don't mutate the graph
    """
    repo = Path(repo_path).resolve()
    if not repo.is_dir():
        raise FileNotFoundError(f"not a directory: {repo}")

    report = SeedReport()

    project_id = _ensure_project_concept(engine, repo, report, dry_run)
    if project_id is None and not dry_run:
        return report

    _scan_docs(engine, repo, project_id, report, dry_run)
    _scan_manifests(engine, repo, project_id, report, dry_run)
    _scan_changelog(engine, repo, project_id, report, dry_run)
    _scan_git_history(engine, repo, project_id, report, max_commits, dry_run)

    if include_code_symbols and not dry_run:
        _index_code(engine, repo, project_id, report)

    return report


# ---- pipeline stages ------------------------------------------------------

def _ensure_project_concept(
    engine: "Engine", repo: Path, report: SeedReport, dry_run: bool,
) -> str | None:
    name = repo.name
    existing = engine.semantic.find_by_name_kind_source(name, NodeKind.project, Source.human)
    if existing is not None:
        report.project_id = existing.id
        return existing.id
    if dry_run:
        report.project_id = "(would create)"
        return None
    # Projects are durability=permanent — never auto-forgotten by cleanup.
    c = Concept(
        name=name, kind=NodeKind.project,
        description=f"Project rooted at {repo}",
        source=Source.human,
        metadata={
            "path": str(repo),
            "seeded_at": datetime.now(timezone.utc).isoformat(),
            "durability": "permanent",
        },
    )
    cid = engine.semantic.add_concept(c)
    report.project_id = cid
    return cid


def _link_to_project(
    engine: "Engine", project_id: str | None, child_id: str, dry_run: bool,
) -> None:
    if not project_id or dry_run or project_id == "(would create)":
        return
    try:
        engine.semantic.add_edge(Edge(
            from_id=child_id, to_id=project_id,
            kind=EdgeKind.part_of, source=Source.human,
        ))
    except Exception as e:  # noqa: BLE001
        log.debug("link_to_project failed: %s", e)


def _add_concept(
    engine: "Engine", name: str, kind: NodeKind, description: str | None,
    metadata: dict[str, Any] | None, project_id: str | None, report: SeedReport,
    dry_run: bool, counter_attr: str,
) -> str | None:
    """Dedup + insert + link to project + bump report counter."""
    # Soft dedup by name+kind+source so re-running seed is idempotent.
    existing = engine.semantic.find_by_name_kind_source(name, kind, Source.human)
    if existing is not None:
        return existing.id
    if dry_run:
        setattr(report, counter_attr, getattr(report, counter_attr) + 1)
        return None
    c = Concept(
        name=name, kind=kind, description=description,
        source=Source.human, metadata=metadata or {},
    )
    cid = engine.semantic.add_concept(c)
    _link_to_project(engine, project_id, cid, dry_run)
    setattr(report, counter_attr, getattr(report, counter_attr) + 1)
    return cid


def _scan_docs(
    engine: "Engine", repo: Path, project_id: str | None, report: SeedReport,
    dry_run: bool,
) -> None:
    """Pick up README/ARCHITECTURE/CONTRIBUTING + every ADR/decision file."""
    # Top-level overview docs -> one `fact` each, with first paragraph as desc.
    for fname in _TOP_DOC_NAMES:
        p = repo / fname
        if not p.is_file():
            continue
        report.docs_scanned += 1
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except Exception as e:  # noqa: BLE001
            report.errors.append(f"{fname}: {e}")
            continue
        first_para = _first_paragraph(text)
        _add_concept(
            engine, name=f"{p.stem}: project overview",
            kind=NodeKind.fact, description=first_para[:2000],
            metadata={"source_file": str(p.relative_to(repo))},
            project_id=project_id, report=report, dry_run=dry_run,
            counter_attr="facts_added",
        )
        # Also extract bullet lists under "Tech stack" / "Stack" / "Tools" headings.
        for fact_name, fact_desc in _extract_stack_bullets(text):
            _add_concept(
                engine, name=fact_name, kind=NodeKind.fact,
                description=fact_desc,
                metadata={"source_file": str(p.relative_to(repo))},
                project_id=project_id, report=report, dry_run=dry_run,
                counter_attr="facts_added",
            )

    # Decision docs anywhere in the repo.
    for path in _iter_repo_files(repo):
        rel = path.relative_to(repo).as_posix()
        if not any(p.match(rel) or p.match(path.name) for p in _DECISION_FILE_PATTERNS):
            continue
        report.docs_scanned += 1
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception as e:  # noqa: BLE001
            report.errors.append(f"{rel}: {e}")
            continue
        title = _doc_title(text) or path.stem.replace("-", " ").replace("_", " ").title()
        body = _first_paragraph(text)
        _add_concept(
            engine, name=title, kind=NodeKind.decision,
            description=body[:2000],
            metadata={"source_file": rel},
            project_id=project_id, report=report, dry_run=dry_run,
            counter_attr="decisions_added",
        )


def _scan_manifests(
    engine: "Engine", repo: Path, project_id: str | None, report: SeedReport,
    dry_run: bool,
) -> None:
    """Extract tech-stack facts from pyproject.toml / package.json / etc."""
    for fname in _MANIFEST_NAMES:
        p = repo / fname
        if not p.is_file():
            continue
        report.manifests_scanned += 1
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except Exception as e:  # noqa: BLE001
            report.errors.append(f"{fname}: {e}")
            continue
        facts = _extract_manifest_facts(fname, text)
        for fact_name, fact_desc in facts:
            _add_concept(
                engine, name=fact_name, kind=NodeKind.fact,
                description=fact_desc,
                metadata={"source_file": fname},
                project_id=project_id, report=report, dry_run=dry_run,
                counter_attr="facts_added",
            )


def _scan_changelog(
    engine: "Engine", repo: Path, project_id: str | None, report: SeedReport,
    dry_run: bool,
) -> None:
    """Each version section in CHANGELOG.md → one decision."""
    for fname in _CHANGELOG_NAMES:
        p = repo / fname
        if not p.is_file():
            continue
        report.docs_scanned += 1
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except Exception as e:  # noqa: BLE001
            report.errors.append(f"{fname}: {e}")
            continue
        for ver, body in _split_changelog_versions(text):
            _add_concept(
                engine, name=f"Release {ver}",
                kind=NodeKind.decision,
                description=body[:1500],
                metadata={"source_file": fname, "version": ver},
                project_id=project_id, report=report, dry_run=dry_run,
                counter_attr="decisions_added",
            )


def _scan_git_history(
    engine: "Engine", repo: Path, project_id: str | None, report: SeedReport,
    max_commits: int, dry_run: bool,
) -> None:
    """Walk git log; turn meaningful commits into decisions, harvest authors."""
    if not (repo / ".git").exists():
        return
    try:
        result = subprocess.run(
            [
                "git", "-C", str(repo), "log",
                f"-n{max_commits}",
                "--no-merges",
                "--pretty=format:%H%x1f%aI%x1f%aN%x1f%aE%x1f%s%x1f%b%x1e",
            ],
            capture_output=True, text=True, timeout=30,
            encoding="utf-8", errors="replace",
        )
    except Exception as e:  # noqa: BLE001
        report.errors.append(f"git log failed: {e}")
        return
    if result.returncode != 0:
        report.errors.append(f"git log rc={result.returncode}: {result.stderr[:200]}")
        return

    authors_seen: set[str] = set()
    for raw in result.stdout.split("\x1e"):
        raw = raw.strip()
        if not raw:
            continue
        parts = raw.split("\x1f")
        if len(parts) < 5:
            continue
        sha, when, author_name, author_email, subject = parts[:5]
        body = parts[5] if len(parts) > 5 else ""
        report.commits_scanned += 1

        # Skip noise.
        if any(p.search(subject) for p in _NOISE_COMMIT_PATTERNS):
            continue
        # Keep commits with either:
        #  (a) conventional-commit prefix (feat:, fix:, refactor:, ...)
        #  (b) imperative-verb start AND >= 25 chars (signals intent)
        if not _DECISION_COMMIT_RE.match(subject):
            if not _MEANINGFUL_VERB_RE.match(subject) or len(subject) < 25:
                continue

        report.commits_kept += 1
        clean_subject = re.sub(r"^[a-z]+(\([^)]*\))?[:!]\s*", "", subject, flags=re.I).strip()
        desc_lines = [subject]
        if body:
            desc_lines.append("")
            desc_lines.append(body.strip()[:1500])
        desc_lines.append("")
        desc_lines.append(f"— {author_name} <{author_email}> at {when} ({sha[:8]})")
        # Commit decisions are durability=sprint — they may be superseded
        # by later commits. Cleanup gives them ~3x normal TTL so they
        # survive longer than session-scoped facts but aren't permanent.
        _add_concept(
            engine, name=clean_subject[:160] or subject[:160],
            kind=NodeKind.decision,
            description="\n".join(desc_lines),
            metadata={
                "commit_sha": sha, "commit_date": when,
                "commit_author": author_name, "commit_email": author_email,
                "durability": "sprint",
            },
            project_id=project_id, report=report, dry_run=dry_run,
            counter_attr="decisions_added",
        )

        # Author → person concept (one per author).
        if author_email and author_email not in authors_seen:
            authors_seen.add(author_email)
            _add_concept(
                engine, name=author_name,
                kind=NodeKind.person,
                description=f"Git author: {author_email}",
                metadata={"email": author_email},
                project_id=project_id, report=report, dry_run=dry_run,
                counter_attr="people_added",
            )


def _index_code(
    engine: "Engine", repo: Path, project_id: str | None, report: SeedReport,
) -> None:
    """Hand off code-symbol indexing to the existing ingest pipeline."""
    try:
        from memex.codebase.sources import add_source
        add_source(engine, str(repo), name=repo.name)
        report.code_indexed = True
    except Exception as e:  # noqa: BLE001
        report.errors.append(f"code indexing failed: {e}")


# ---- text helpers ---------------------------------------------------------

def _first_paragraph(text: str) -> str:
    """Strip YAML frontmatter + return the first prose paragraph."""
    text = text.strip()
    if text.startswith("---"):
        end = text.find("\n---", 4)
        if end > 0:
            text = text[end + 4:].lstrip()
    # Skip a leading H1.
    lines = text.splitlines()
    paras: list[str] = []
    buf: list[str] = []
    for line in lines:
        if line.strip().startswith("#"):
            if buf:
                paras.append(" ".join(buf).strip())
                buf = []
            continue
        if line.strip():
            buf.append(line.strip())
        else:
            if buf:
                paras.append(" ".join(buf).strip())
                buf = []
            if paras:
                break
    if buf:
        paras.append(" ".join(buf).strip())
    for p in paras:
        if len(p) > 30:
            return p
    return paras[0] if paras else ""


def _doc_title(text: str) -> str | None:
    for line in text.splitlines()[:20]:
        s = line.strip()
        if s.startswith("# "):
            return s[2:].strip()
        if s.startswith("title:"):
            return s.split(":", 1)[1].strip().strip('"').strip("'")
    return None


def _extract_stack_bullets(text: str) -> list[tuple[str, str]]:
    """Find bullet items under headings like 'Tech stack' / 'Stack' / 'Built with'."""
    out: list[tuple[str, str]] = []
    lines = text.splitlines()
    in_stack = False
    for line in lines:
        s = line.strip()
        if s.startswith("#"):
            in_stack = bool(
                re.search(r"\b(tech\s+stack|stack|built\s+with|technology|tools)\b", s, re.I)
            )
            continue
        if not in_stack:
            continue
        m = re.match(r"^[-*]\s+(.+)$", s)
        if not m:
            continue
        item = m.group(1).strip()
        # Strip surrounding backticks / bold markers
        item = re.sub(r"[`*]", "", item).strip()
        if not item or len(item) > 200:
            continue
        # Split "name — description" or "name: description"
        sep = re.search(r"\s[—:–-]\s", item)
        if sep:
            name = item[:sep.start()].strip()
            desc = item[sep.end():].strip()
        else:
            name = item
            desc = item
        if name:
            out.append((f"Uses {name}", desc))
    return out


def _extract_manifest_facts(fname: str, text: str) -> list[tuple[str, str]]:
    """Pull tech-stack facts from a manifest file."""
    out: list[tuple[str, str]] = []
    name_lower = fname.lower()
    if name_lower == "pyproject.toml":
        for m in re.finditer(r'"([a-zA-Z0-9_\-]+)\s*([><=!~][^"]*)?"', text):
            pkg = m.group(1)
            if pkg in {"python", "name", "version", "description", "license"}:
                continue
            out.append((f"Uses {pkg} (python)", f"Python dependency: {m.group(0).strip()}"))
            if len(out) > 40:
                break
    elif name_lower == "package.json":
        for m in re.finditer(r'"([a-zA-Z0-9_\-/@.]+)"\s*:\s*"([^"]+)"', text):
            k, v = m.group(1), m.group(2)
            if not v or v.startswith(("http", "git")):
                continue
            if re.match(r"^[\^~]?\d", v) or v in {"latest", "*"}:
                out.append((f"Uses {k} (node)", f"npm dependency: {k}@{v}"))
                if len(out) > 40:
                    break
    elif name_lower == "go.mod":
        for line in text.splitlines():
            s = line.strip()
            m = re.match(r"^(\S+)\s+v?(\d[\w.\-+]*)", s)
            if m and "/" in m.group(1):
                out.append((f"Uses {m.group(1)} (go)", f"Go module: {m.group(1)} {m.group(2)}"))
            if len(out) > 40:
                break
    elif name_lower == "cargo.toml":
        for m in re.finditer(r"^([a-zA-Z0-9_\-]+)\s*=", text, re.M):
            pkg = m.group(1)
            if pkg in {"name", "version", "edition", "authors", "description", "license"}:
                continue
            out.append((f"Uses {pkg} (rust)", f"Cargo crate: {pkg}"))
            if len(out) > 40:
                break
    elif name_lower == "requirements.txt":
        for line in text.splitlines():
            s = line.strip()
            if not s or s.startswith("#") or s.startswith("-"):
                continue
            pkg = re.split(r"[<>=!~ ]", s, 1)[0]
            if pkg:
                out.append((f"Uses {pkg} (python)", f"pip dependency: {s}"))
            if len(out) > 40:
                break
    return out


def _split_changelog_versions(text: str) -> list[tuple[str, str]]:
    """Each "## v1.2.3" or "## [1.2.3]" heading starts a new version block."""
    out: list[tuple[str, str]] = []
    current_ver: str | None = None
    buf: list[str] = []
    for line in text.splitlines():
        m = re.match(r"^##+\s*\[?v?(\d+\.\d+(?:\.\d+)?)", line.strip(), re.I)
        if m:
            if current_ver:
                out.append((current_ver, "\n".join(buf).strip()))
            current_ver = m.group(1)
            buf = []
        else:
            if current_ver is not None:
                buf.append(line)
    if current_ver:
        out.append((current_ver, "\n".join(buf).strip()))
    return out


# ---- repo walker ---------------------------------------------------------

_SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "env",
    "dist", "build", "target", ".pytest_cache", ".mypy_cache", ".tox",
    ".next", ".nuxt", ".idea", ".vscode", "coverage",
}


def _iter_repo_files(repo: Path) -> Any:
    """Walk the repo, yielding files only. Skips heavy or generated dirs."""
    for path in repo.rglob("*"):
        if path.is_dir():
            continue
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        if path.stat().st_size > 1_000_000:  # 1MB — skip huge files
            continue
        yield path
