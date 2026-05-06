"""Repo bootstrap tests — directory + file scanning, idempotency."""

from __future__ import annotations

import json
from pathlib import Path

from memex.core.engine import Engine
from memex.skills import bootstrap, discover_bundles, discover_files


def _make_dir_bundle(root: Path, name: str, concepts: list[dict]) -> Path:
    bundle = root / ".memex" / "skills" / name
    bundle.mkdir(parents=True, exist_ok=True)
    (bundle / "manifest.json").write_text(
        json.dumps({"name": name, "version": "0.1.0", "description": f"{name} bundle"}),
        encoding="utf-8",
    )
    (bundle / "concepts.jsonl").write_text(
        "\n".join(json.dumps(c) for c in concepts),
        encoding="utf-8",
    )
    return bundle


def test_discover_bundles_empty(tmp_path: Path):
    assert discover_bundles(tmp_path) == []


def test_discover_bundles_finds_one(tmp_path: Path):
    _make_dir_bundle(tmp_path, "team-philosophy", [{"name": "fail-fast", "kind": "pattern"}])
    found = discover_bundles(tmp_path)
    assert len(found) == 1
    assert found[0].name == "team-philosophy"


def test_discover_files_finds_jsonl(tmp_path: Path):
    target = tmp_path / "devops.memex.jsonl"
    target.write_text(
        json.dumps({"name": "deploy-on-friday-rule", "kind": "constraint"}) + "\n",
        encoding="utf-8",
    )
    assert discover_files(tmp_path) == [target]


def test_discover_files_skips_ignored_dirs(tmp_path: Path):
    (tmp_path / ".git").mkdir()
    sneaky = tmp_path / ".git" / "secret.memex.json"
    sneaky.write_text("[]", encoding="utf-8")
    assert discover_files(tmp_path) == []


def test_bootstrap_installs_dir_bundle(engine: Engine, tmp_path: Path):
    _make_dir_bundle(
        tmp_path,
        "team-conventions",
        [
            {"name": "pr-must-have-tests", "kind": "constraint", "description": "no exceptions"},
            {"name": "fail-fast-not-fallback", "kind": "pattern"},
        ],
    )
    result = bootstrap(engine, tmp_path)
    assert "team-conventions" in result.skills_installed
    assert result.concepts_added == 2

    # Idempotency: second run skips.
    again = bootstrap(engine, tmp_path)
    assert "team-conventions" in again.skills_skipped
    assert again.concepts_added == 0


def test_bootstrap_installs_file_skill_jsonl(engine: Engine, tmp_path: Path):
    target = tmp_path / "philosophy.memex.jsonl"
    target.write_text(
        json.dumps({"name": "no-investors-rule", "kind": "decision"}) + "\n",
        encoding="utf-8",
    )
    result = bootstrap(engine, tmp_path)
    assert "philosophy" in result.skills_installed
    assert result.concepts_added == 1


def test_bootstrap_installs_file_skill_json_list(engine: Engine, tmp_path: Path):
    target = tmp_path / "guidelines.memex.json"
    target.write_text(
        json.dumps([{"name": "always-cache-prompts", "kind": "pattern"}]),
        encoding="utf-8",
    )
    result = bootstrap(engine, tmp_path)
    assert "guidelines" in result.skills_installed


def test_bootstrap_installs_file_skill_json_object(engine: Engine, tmp_path: Path):
    target = tmp_path / "anything.memex.json"
    target.write_text(
        json.dumps(
            {
                "name": "repo-conventions",
                "version": "1.0.0",
                "concepts": [{"name": "use-async-everywhere", "kind": "pattern"}],
            }
        ),
        encoding="utf-8",
    )
    result = bootstrap(engine, tmp_path)
    assert "repo-conventions" in result.skills_installed
