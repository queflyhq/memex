"""Per-OS service installation. User-scope (no admin/root needed)."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


def install_service(*, listen: str = "127.0.0.1:7777", team_repo: str | None = None) -> dict[str, Any]:
    """Install memex as a per-user always-on service for this OS."""
    if sys.platform == "darwin":
        return _install_launchd(listen, team_repo)
    if sys.platform == "win32":
        return _install_windows_task(listen, team_repo)
    if sys.platform.startswith("linux"):
        return _install_systemd_user(listen, team_repo)
    return {"ok": False, "error": f"unsupported platform: {sys.platform}"}


def uninstall_service() -> dict[str, Any]:
    if sys.platform == "darwin":
        return _uninstall_launchd()
    if sys.platform == "win32":
        return _uninstall_windows_task()
    if sys.platform.startswith("linux"):
        return _uninstall_systemd_user()
    return {"ok": False, "error": f"unsupported platform: {sys.platform}"}


def service_status() -> dict[str, Any]:
    if sys.platform == "darwin":
        return _status_launchd()
    if sys.platform == "win32":
        return _status_windows_task()
    if sys.platform.startswith("linux"):
        return _status_systemd_user()
    return {"installed": False, "running": False, "platform": sys.platform}


# ---- macOS launchd -------------------------------------------------------

_MACOS_LABEL = "com.quefly.memex"


def _macos_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{_MACOS_LABEL}.plist"


def _install_launchd(listen: str, team_repo: str | None) -> dict[str, Any]:
    memex_bin = _find_memex_binary()
    if memex_bin is None:
        return {"ok": False, "error": "couldn't find memex CLI on $PATH"}
    plist_path = _macos_plist_path()
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    env_vars = ""
    if team_repo:
        env_vars = (
            "    <key>EnvironmentVariables</key>\n"
            "    <dict>\n"
            f"      <key>MEMEX_TEAM_REPO</key><string>{team_repo}</string>\n"
            "    </dict>\n"
        )
    plist = f'''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
  <dict>
    <key>Label</key><string>{_MACOS_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
      <string>{memex_bin}</string>
      <string>daemon</string>
      <string>--listen</string><string>{listen}</string>
    </array>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key><string>{Path.home()}/Library/Logs/memex.log</string>
    <key>StandardErrorPath</key><string>{Path.home()}/Library/Logs/memex.log</string>
{env_vars}  </dict>
</plist>
'''
    plist_path.write_text(plist, encoding="utf-8")
    # Unload first (idempotent), then load.
    subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True)
    result = subprocess.run(
        ["launchctl", "load", str(plist_path)],
        capture_output=True, text=True,
    )
    return {
        "ok": result.returncode == 0,
        "plist": str(plist_path),
        "label": _MACOS_LABEL,
        "stderr": result.stderr.strip() if result.returncode else "",
    }


def _uninstall_launchd() -> dict[str, Any]:
    plist_path = _macos_plist_path()
    if plist_path.is_file():
        subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True)
        plist_path.unlink()
        return {"ok": True, "removed": str(plist_path)}
    return {"ok": True, "removed": None}


def _status_launchd() -> dict[str, Any]:
    plist_path = _macos_plist_path()
    if not plist_path.is_file():
        return {"installed": False, "running": False}
    result = subprocess.run(
        ["launchctl", "list", _MACOS_LABEL],
        capture_output=True, text=True,
    )
    return {
        "installed": True,
        "running": result.returncode == 0,
        "plist": str(plist_path),
    }


# ---- Windows Task Scheduler ----------------------------------------------

_WIN_TASK_NAME = "MemexDaemon"


def _install_windows_task(listen: str, team_repo: str | None) -> dict[str, Any]:
    memex_bin = _find_memex_binary()
    if memex_bin is None:
        return {"ok": False, "error": "couldn't find memex CLI on PATH"}
    # Wrap memex in a tiny .cmd so we can inject MEMEX_TEAM_REPO + capture logs.
    log_path = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "Quefly" / "memex" / "logs"
    log_path.mkdir(parents=True, exist_ok=True)
    wrapper = log_path / "memex-service.cmd"
    env_line = f'set MEMEX_TEAM_REPO={team_repo}' if team_repo else 'rem (no team repo)'
    wrapper.write_text(
        f'@echo off\r\n{env_line}\r\n"{memex_bin}" daemon --listen {listen} >> "{log_path}\\memex.log" 2>&1\r\n',
        encoding="utf-8",
    )
    # Delete existing task (idempotent) then create.
    subprocess.run(
        ["schtasks", "/Delete", "/TN", _WIN_TASK_NAME, "/F"],
        capture_output=True,
    )
    result = subprocess.run(
        [
            "schtasks", "/Create",
            "/TN", _WIN_TASK_NAME,
            "/SC", "ONLOGON",
            "/RL", "LIMITED",
            "/TR", str(wrapper),
            "/F",
        ],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return {"ok": False, "stderr": result.stderr.strip()}
    # Start immediately too.
    subprocess.run(
        ["schtasks", "/Run", "/TN", _WIN_TASK_NAME],
        capture_output=True,
    )
    return {
        "ok": True,
        "task": _WIN_TASK_NAME,
        "wrapper": str(wrapper),
        "log": str(log_path / "memex.log"),
    }


def _uninstall_windows_task() -> dict[str, Any]:
    result = subprocess.run(
        ["schtasks", "/Delete", "/TN", _WIN_TASK_NAME, "/F"],
        capture_output=True, text=True,
    )
    return {"ok": result.returncode == 0, "task": _WIN_TASK_NAME}


def _status_windows_task() -> dict[str, Any]:
    result = subprocess.run(
        ["schtasks", "/Query", "/TN", _WIN_TASK_NAME, "/FO", "LIST"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return {"installed": False, "running": False}
    # Naive parse — look for "Status" line value.
    running = "Running" in result.stdout
    return {"installed": True, "running": running, "task": _WIN_TASK_NAME}


# ---- Linux systemd user unit ---------------------------------------------

_LINUX_UNIT = "memex.service"


def _linux_unit_path() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / _LINUX_UNIT


def _install_systemd_user(listen: str, team_repo: str | None) -> dict[str, Any]:
    memex_bin = _find_memex_binary()
    if memex_bin is None:
        return {"ok": False, "error": "couldn't find memex CLI on $PATH"}
    unit_path = _linux_unit_path()
    unit_path.parent.mkdir(parents=True, exist_ok=True)
    env_line = f"Environment=MEMEX_TEAM_REPO={team_repo}\n" if team_repo else ""
    unit = (
        "[Unit]\n"
        "Description=Memex memory daemon\n"
        "After=network.target\n\n"
        "[Service]\n"
        f"ExecStart={memex_bin} daemon --listen {listen}\n"
        "Restart=always\n"
        "RestartSec=5\n"
        f"{env_line}"
        "\n[Install]\n"
        "WantedBy=default.target\n"
    )
    unit_path.write_text(unit, encoding="utf-8")
    for cmd in [
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", _LINUX_UNIT],
        ["systemctl", "--user", "restart", _LINUX_UNIT],
    ]:
        subprocess.run(cmd, capture_output=True)
    return {"ok": True, "unit": str(unit_path)}


def _uninstall_systemd_user() -> dict[str, Any]:
    for cmd in [
        ["systemctl", "--user", "stop", _LINUX_UNIT],
        ["systemctl", "--user", "disable", _LINUX_UNIT],
    ]:
        subprocess.run(cmd, capture_output=True)
    unit_path = _linux_unit_path()
    if unit_path.is_file():
        unit_path.unlink()
    subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
    return {"ok": True, "unit": str(unit_path)}


def _status_systemd_user() -> dict[str, Any]:
    result = subprocess.run(
        ["systemctl", "--user", "is-active", _LINUX_UNIT],
        capture_output=True, text=True,
    )
    return {
        "installed": _linux_unit_path().is_file(),
        "running": result.stdout.strip() == "active",
        "unit": str(_linux_unit_path()),
    }


# ---- common helpers -------------------------------------------------------


def _find_memex_binary() -> str | None:
    """Locate the memex CLI executable for the current install. Prefers
    whatever brought us here so the service uses the same Python env.
    """
    # Look for `memex` on PATH first — typical pipx / uv tool install.
    found = shutil.which("memex")
    if found:
        return found
    # Fall back to scripts dir of the active interpreter.
    for parent in [Path(sys.executable).parent, Path(sys.executable).parent.parent / "bin"]:
        candidate = parent / ("memex.exe" if sys.platform == "win32" else "memex")
        if candidate.is_file():
            return str(candidate)
    return None
