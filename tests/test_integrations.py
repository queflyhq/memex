"""
Tests for AI-tool integrations (the `memex setup` wizard's machinery).

Real integration writes against `~/.claude.json` etc. would clobber the
developer's config, so the tests use a temporary HOME and exercise:

  - detect_all() reports all supported tools
  - wire() preserves existing keys in a target config (the Claude case
    where a 23K config has tons of state besides mcpServers)
  - wire() creates the parent dir for tools whose config dir doesn't exist
  - wire() is idempotent (action='already-up-to-date' on second call)
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from memex.integrations import ClaudeCode, Cursor, detect_all, memex_mcp_block


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect Path.home() to a tmp dir so tests don't touch the real home."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    # On Windows the integration consults APPDATA for the Cline path — point
    # it inside the temp dir too.
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData" / "Roaming"))
    return tmp_path


def test_detect_all_returns_one_per_supported(fake_home: Path) -> None:
    statuses = detect_all()
    names = {s.name for s in statuses}
    assert "Claude Code" in names
    assert "Cursor" in names
    assert "Windsurf" in names
    assert "Cline (VS Code)" in names


def test_wire_preserves_existing_claude_keys(fake_home: Path) -> None:
    """Claude Code's user-global config has tons of state besides mcpServers."""
    claude_path = fake_home / ".claude.json"
    existing = {
        "userID": "abc-123",
        "firstStartTime": "2025-01-01T00:00:00Z",
        "mcpServers": {"existing-server": {"command": "x", "args": []}},
        "toolUsage": {"some": "data"},
    }
    claude_path.write_text(json.dumps(existing), encoding="utf-8")

    result = ClaudeCode().wire()
    assert result.error is None
    assert result.action == "added"

    after = json.loads(claude_path.read_text(encoding="utf-8"))
    # All non-mcpServers state preserved
    assert after["userID"] == "abc-123"
    assert after["firstStartTime"] == "2025-01-01T00:00:00Z"
    assert after["toolUsage"] == {"some": "data"}
    # Existing mcpServer entry preserved
    assert after["mcpServers"]["existing-server"] == {"command": "x", "args": []}
    # memex added
    assert "memex" in after["mcpServers"]
    assert after["mcpServers"]["memex"] == memex_mcp_block()


def test_wire_creates_parent_dir_when_missing(fake_home: Path) -> None:
    """Cursor's config dir may not exist yet on a fresh machine."""
    target = fake_home / ".cursor" / "mcp.json"
    assert not target.parent.exists()

    result = Cursor().wire()
    assert result.error is None
    assert result.action == "added"
    assert target.is_file()

    data = json.loads(target.read_text(encoding="utf-8"))
    assert "memex" in data["mcpServers"]


def test_wire_is_idempotent(fake_home: Path) -> None:
    target = fake_home / ".cursor" / "mcp.json"
    assert Cursor().wire().action == "added"
    second = Cursor().wire()
    assert second.action == "already-up-to-date"


def test_wire_updates_when_block_differs(fake_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """If memex is wired with an old/wrong block, wire() rewrites it."""
    target = fake_home / ".cursor" / "mcp.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({
        "mcpServers": {
            "memex": {"command": "old-stale-path", "args": ["bad"]}
        }
    }), encoding="utf-8")

    result = Cursor().wire()
    assert result.error is None
    assert result.action == "updated"

    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["mcpServers"]["memex"] == memex_mcp_block()


def test_wire_skips_on_invalid_existing_json(fake_home: Path) -> None:
    target = fake_home / ".cursor" / "mcp.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("not valid json {", encoding="utf-8")

    result = Cursor().wire()
    assert result.action == "skipped"
    assert result.error is not None
    assert "valid JSON" in result.error
