"""
Repo bootstrap — auto-discover and import skill bundles from a directory.

Two patterns supported:

  1. Directory bundles: any subdirectory at `<root>/.memex/skills/<name>/` that
     contains a `manifest.json` is treated as a skill bundle and installed.

  2. Single-file skills: any file matching `*.memex.json` or `*.memex.jsonl`
     anywhere under `<root>` is loaded as a skill. The file's stem (minus the
     `.memex` suffix) becomes the skill name; the JSON/JSONL contents become
     the concepts list.

Bootstrap is opt-in: call `memex bootstrap <path>` explicitly, or set
`MEMEX_AUTO_BOOTSTRAP=true` to scan the cwd at daemon startup.

Idempotent: a skill already installed (concept `metadata.skill == name`) is
skipped, so re-running bootstrap doesn't duplicate.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from memex.skills.loader import Skill, install_skill

if TYPE_CHECKING:
    from memex.core.engine import Engine

log = logging.getLogger(__name__)


_DIR_GLOB = ".memex/skills"
_FILE_SUFFIXES = (".memex.json", ".memex.jsonl")


@dataclass(slots=True)
class BootstrapResult:
    skills_installed: list[str]
    skills_skipped: list[str]
    concepts_added: int

    def to_dict(self) -> dict[str, object]:
        return {
            "skills_installed": self.skills_installed,
            "skills_skipped": self.skills_skipped,
            "concepts_added": self.concepts_added,
        }


def discover_bundles(root: Path) -> list[Path]:
    """Return directories under `<root>/.memex/skills/` that look like bundles."""
    base = root / _DIR_GLOB
    if not base.is_dir():
        return []
    out: list[Path] = []
    for child in sorted(base.iterdir()):
        if child.is_dir() and (child / "manifest.json").is_file():
            out.append(child)
    return out


def discover_files(root: Path) -> list[Path]:
    """Return single-file skills (`*.memex.json` / `*.memex.jsonl`) under `<root>`.

    Skips common ignore directories (.git, node_modules, .venv, dist, build, site).
    """
    skip_dirs = {".git", "node_modules", ".venv", "venv", "dist", "build", "site", "__pycache__"}
    out: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in skip_dirs for part in path.parts):
            continue
        if any(path.name.endswith(suffix) for suffix in _FILE_SUFFIXES):
            out.append(path)
    return sorted(out)


def _bundle_already_installed(engine: Engine, skill_name: str) -> bool:
    """Detect prior install by scanning concepts for the skill's provenance stamp.

    Brute-force scan is fine at v0.1 scale (<100K nodes); a metadata index can
    be added later behind the SemanticStore Protocol if N grows.
    """
    for concept in engine.semantic.all_concepts():
        if (concept.metadata or {}).get("skill") == skill_name:
            return True
    return False


def _load_file_skill(path: Path) -> Skill:
    """Load a single-file skill (`<name>.memex.json` or `<name>.memex.jsonl`)."""
    text = path.read_text(encoding="utf-8")
    if path.name.endswith(".memex.jsonl"):
        concepts = [json.loads(line) for line in text.splitlines() if line.strip()]
        manifest = {"name": path.name[: -len(".memex.jsonl")], "version": "0.0.0", "description": ""}
    else:
        data = json.loads(text)
        if isinstance(data, list):
            concepts = data
            manifest = {"name": path.name[: -len(".memex.json")], "version": "0.0.0", "description": ""}
        elif isinstance(data, dict) and "concepts" in data:
            concepts = data["concepts"]
            manifest = {
                "name": data.get("name") or path.name[: -len(".memex.json")],
                "version": data.get("version", "0.0.0"),
                "description": data.get("description", ""),
            }
        else:
            raise ValueError(
                f"{path}: unrecognized .memex.json shape (expected list or "
                "object with `concepts` key)"
            )
    return Skill(
        name=manifest["name"],
        version=manifest["version"],
        description=manifest["description"],
        concepts=concepts,
        edges=[],
    )


def bootstrap(engine: Engine, root: Path | str = ".") -> BootstrapResult:
    """Discover and install all skills found under `root`."""
    root = Path(root).resolve()
    installed: list[str] = []
    skipped: list[str] = []
    total_concepts = 0

    # 1. Directory bundles
    for bundle_dir in discover_bundles(root):
        from memex.skills.loader import _load_skill_dir  # internal re-use

        skill = _load_skill_dir(bundle_dir)
        if _bundle_already_installed(engine, skill.name):
            log.info("skill already installed, skipping: %s", skill.name)
            skipped.append(skill.name)
            continue
        counts = install_skill(engine, skill)
        installed.append(skill.name)
        total_concepts += counts.get("concepts", 0)

    # 2. Single-file skills
    for path in discover_files(root):
        try:
            skill = _load_file_skill(path)
        except Exception as e:
            log.warning("failed to load %s: %s", path, e)
            continue
        if _bundle_already_installed(engine, skill.name):
            log.info("skill already installed, skipping: %s", skill.name)
            skipped.append(skill.name)
            continue
        counts = install_skill(engine, skill)
        installed.append(skill.name)
        total_concepts += counts.get("concepts", 0)

    return BootstrapResult(
        skills_installed=installed,
        skills_skipped=skipped,
        concepts_added=total_concepts,
    )
