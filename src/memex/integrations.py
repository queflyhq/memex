"""
AI-tool integrations — auto-detect and wire memex into the developer's editor.

The pain we're removing: every editor's docs tell the user a slightly different
incantation to wire memex in (`claude mcp add memex memex serve`, edit a JSON
file, click an "Add MCP" button). For someone who just installed memex, this
is the worst-possible 60 seconds. The setup wizard does it for them.

Each integration:
  - knows where its tool's MCP config lives
  - knows whether the tool is currently installed (config file exists)
  - reads + writes that config preserving all other keys
  - reports whether memex is already wired in

`memex setup` walks every supported tool, prompts the user once per detected
tool, and writes the memex block atomically into the tool's config.

We deliberately do NOT auto-restart the editor — that's the user's call.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path


# The block we write into each tool's `mcpServers` map.
def _memex_command() -> tuple[str, list[str]]:
    """Resolve a launch command that works after install.

    Preference order:
      1. `memex` on PATH (pipx / uv tool install / brew) — what the README
         says to use, and what the user expects.
      2. Fall back to `<sys.executable> -m memex.frontends.cli.main` so a
         dev-mode install (`pip install -e .`) still works without PATH.
    """
    found = shutil.which("memex")
    if found:
        return found, ["serve"]
    return sys.executable, ["-m", "memex.frontends.cli.main", "serve"]


def memex_mcp_block() -> dict:
    cmd, args = _memex_command()
    return {"command": cmd, "args": args}


@dataclass
class IntegrationStatus:
    name: str           # display name ("Claude Code", "Cursor", ...)
    config_path: Path
    config_exists: bool
    memex_present: bool
    tool_installed: bool  # heuristic: config file or config dir exists


@dataclass
class WireResult:
    name: str
    action: str         # "skipped", "added", "updated", "already-up-to-date"
    config_path: Path
    error: str | None = None


# ---- per-tool integrations -----------------------------------------------


class _Integration:
    name: str = ""

    def config_path(self) -> Path:
        raise NotImplementedError

    def status(self) -> IntegrationStatus:
        path = self.config_path()
        exists = path.is_file()
        present = False
        if exists:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                present = "memex" in (data.get("mcpServers") or {})
            except (json.JSONDecodeError, OSError):
                present = False
        # Heuristic: the parent dir existing (or config file existing) means
        # the tool has at least been launched once on this machine.
        tool_installed = exists or path.parent.is_dir()
        return IntegrationStatus(
            name=self.name,
            config_path=path,
            config_exists=exists,
            memex_present=present,
            tool_installed=tool_installed,
        )

    def wire(self) -> WireResult:
        path = self.config_path()
        block = memex_mcp_block()
        try:
            data: dict = {}
            if path.is_file():
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                except json.JSONDecodeError as e:
                    return WireResult(
                        name=self.name,
                        action="skipped",
                        config_path=path,
                        error=f"existing config is not valid JSON: {e}",
                    )
            else:
                path.parent.mkdir(parents=True, exist_ok=True)

            servers = data.setdefault("mcpServers", {})
            existing = servers.get("memex")
            if existing == block:
                return WireResult(
                    name=self.name,
                    action="already-up-to-date",
                    config_path=path,
                )
            action = "updated" if existing else "added"
            servers["memex"] = block

            # Atomic write: tmp file in the same dir, then replace.
            tmp_fd, tmp_path = tempfile.mkstemp(
                prefix=path.name + ".", suffix=".tmp", dir=path.parent
            )
            try:
                with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2, ensure_ascii=False)
                os.replace(tmp_path, path)
            except Exception:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
            return WireResult(name=self.name, action=action, config_path=path)
        except OSError as e:
            return WireResult(
                name=self.name, action="skipped", config_path=path, error=str(e)
            )


class ClaudeCode(_Integration):
    name = "Claude Code"

    def config_path(self) -> Path:
        return Path.home() / ".claude.json"


class Cursor(_Integration):
    name = "Cursor"

    def config_path(self) -> Path:
        return Path.home() / ".cursor" / "mcp.json"


class Windsurf(_Integration):
    name = "Windsurf"

    def config_path(self) -> Path:
        return Path.home() / ".codeium" / "windsurf" / "mcp_config.json"


class Cline(_Integration):
    """Cline VS Code extension — config lives in VS Code's globalStorage."""

    name = "Cline (VS Code)"
    _ext = "saoudrizwan.claude-dev"
    _file = "cline_mcp_settings.json"

    def config_path(self) -> Path:
        sysname = platform.system()
        if sysname == "Windows":
            base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
            return base / "Code" / "User" / "globalStorage" / self._ext / "settings" / self._file
        if sysname == "Darwin":
            return (
                Path.home()
                / "Library"
                / "Application Support"
                / "Code"
                / "User"
                / "globalStorage"
                / self._ext
                / "settings"
                / self._file
            )
        return Path.home() / ".config" / "Code" / "User" / "globalStorage" / self._ext / "settings" / self._file


SUPPORTED: list[_Integration] = [
    ClaudeCode(),
    Cursor(),
    Windsurf(),
    Cline(),
]


def detect_all() -> list[IntegrationStatus]:
    """Return the status of every supported AI-tool integration."""
    return [t.status() for t in SUPPORTED]


def wire_one(integration_name: str) -> WireResult:
    """Wire memex into a specific integration by display name."""
    for t in SUPPORTED:
        if t.name == integration_name:
            return t.wire()
    raise KeyError(f"unknown integration: {integration_name}")
