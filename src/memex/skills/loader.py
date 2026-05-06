"""
Skill bundles — installable units of curated knowledge.

Terminology matches industry usage (Anthropic Claude Skills, Microsoft
Semantic Kernel, OpenAI Assistants/skills). A skill bundle ships:

  - manifest.json   — metadata (name, version, description, …)
  - concepts.jsonl  — one Concept per line (JSON)
  - edges.jsonl     — optional, one Edge per line

Builtin skills ship inside the wheel under
`memex.skills.builtin.<bundle_dir>`. Registry skills (v0.2) will be cloned
to `<data_dir>/skills/<name>/` from the `queflyhq/memex-skills` repo.

Validation entries inside a skill use `kind=approach` plus a
`metadata.checks: list[str]` field — these are what AI agents call
`validate(name)` against to self-attest before acting.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from memex.core.schema import EdgeKind, NodeKind, Source

if TYPE_CHECKING:
    from memex.core.engine import Engine

log = logging.getLogger(__name__)


@dataclass(slots=True)
class Skill:
    name: str
    version: str
    description: str
    concepts: list[dict[str, Any]] = field(default_factory=list)
    edges: list[dict[str, Any]] = field(default_factory=list)


def _read(node: Traversable | Path, name: str) -> str:
    if isinstance(node, Path):
        path = node / name
        return path.read_text(encoding="utf-8") if path.exists() else ""
    child = node / name
    return child.read_text(encoding="utf-8") if child.is_file() else ""


def _load_skill_dir(root: Traversable | Path) -> Skill:
    manifest = json.loads(_read(root, "manifest.json"))
    concepts_text = _read(root, "concepts.jsonl")
    edges_text = _read(root, "edges.jsonl")
    return Skill(
        name=manifest["name"],
        version=manifest.get("version", "0.0.0"),
        description=manifest.get("description", ""),
        concepts=[json.loads(line) for line in concepts_text.splitlines() if line.strip()],
        edges=[json.loads(line) for line in edges_text.splitlines() if line.strip()],
    )


def list_builtin_skills() -> list[Skill]:
    """List skill bundles bundled inside the memex wheel."""
    base: Traversable = files("memex.skills.builtin")
    out: list[Skill] = []
    for child in base.iterdir():
        if not child.is_dir():
            continue
        manifest = child / "manifest.json"
        if not manifest.is_file():
            continue
        try:
            out.append(_load_skill_dir(child))
        except Exception as e:
            log.warning("failed to read builtin skill at %s: %s", child, e)
    return out


def find_builtin_skill(name: str) -> Skill | None:
    for skill in list_builtin_skills():
        if skill.name == name:
            return skill
    return None


def install_skill(engine: Engine, skill: Skill) -> dict[str, int]:
    """Add a skill bundle's concepts and edges to the engine."""
    nodes_added = 0
    for raw in skill.concepts:
        raw = dict(raw)
        raw_metadata = dict(raw.get("metadata") or {})
        raw_metadata.update({"skill": skill.name, "skill_version": skill.version})
        engine.add(
            name=raw["name"],
            description=raw.get("description", ""),
            kind=NodeKind(raw.get("kind", NodeKind.fact.value)),
            source=Source.skill,
            confidence=float(raw.get("confidence", 1.0)),
            verification=raw.get("verification"),
            metadata=raw_metadata,
        )
        nodes_added += 1

    edges_added = 0
    for raw in skill.edges:
        engine.link(
            from_id=raw["from_id"],
            to_id=raw["to_id"],
            kind=EdgeKind(raw.get("kind", EdgeKind.relates_to.value)),
            source=Source.skill,
            confidence=float(raw.get("confidence", 1.0)),
            metadata={"skill": skill.name},
        )
        edges_added += 1

    log.info(
        "installed skill %s@%s: %d concepts, %d edges",
        skill.name,
        skill.version,
        nodes_added,
        edges_added,
    )
    return {"concepts": nodes_added, "edges": edges_added}


def install_from_path(engine: Engine, path: Path) -> dict[str, int]:
    """Install a skill bundle from a local filesystem directory."""
    if not path.is_dir():
        raise FileNotFoundError(f"skill directory not found: {path}")
    return install_skill(engine, _load_skill_dir(path))
