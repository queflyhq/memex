"""
Runtime state files written to the data dir on daemon startup.

Two files matter to local clients (the desktop app, the CLI's auto-spawn
shim, third-party HTTP clients):

- ``daemon.url``  — the URL the daemon is listening on (e.g.
  ``http://127.0.0.1:7777``). Lets clients pick up a non-default
  ``MEMEX_LISTEN`` without re-discovery.
- ``daemon.token`` — a per-machine bearer token. Generated on first run
  via :func:`secrets.token_urlsafe`. Mode ``0o600`` on POSIX. Honors
  ``MEMEX_AUTH_TOKEN`` if set (in which case the file mirrors that
  value rather than minting a fresh one).

Why a file instead of an env var:

- The desktop app and CLI need to discover the token without the user
  setting an env var per shell. A file in the data dir is the
  least-bad option.
- Permissions ``0o600`` keep other local OS users from reading it. On
  shared machines, that's the boundary we promise; rooting the box is
  out of scope (see SECURITY.md).
- On Windows we rely on default user-restricted ACLs in
  ``%LOCALAPPDATA%`` rather than POSIX modes. NTFS inheritance gives
  us per-user isolation; explicit DACL hardening can land in 1.x as
  needed without changing this contract.
"""

from __future__ import annotations

import os
import secrets
import sys
from pathlib import Path

from memex.config import Settings

_TOKEN_FILE = "daemon.token"
_URL_FILE = "daemon.url"


def _restrict_perms(path: Path) -> None:
    """Best-effort tighten file perms to owner-only-read/write."""
    if sys.platform == "win32":
        # NTFS inheritance in %LOCALAPPDATA% already restricts to user.
        # Explicit DACL hardening (icacls /inheritance:r /grant:r) is a
        # follow-on if shared-machine policy ever demands it.
        return
    try:
        os.chmod(path, 0o600)
    except OSError:
        # Best-effort. If the FS doesn't support chmod (e.g. some FAT
        # mounts), there's nothing useful we can do — the user picked
        # this data dir.
        pass


def bootstrap_auth_token(settings: Settings) -> str:
    """
    Resolve the daemon's auth token, persisting it on first call.

    Precedence:

    1. ``settings.auth_token`` (set via ``MEMEX_AUTH_TOKEN`` env or CLI
       ``--auth-token``). Mirrored to disk so other local clients can
       discover it.
    2. Existing ``<data_dir>/daemon.token`` from a prior run.
    3. Freshly minted token, written to ``<data_dir>/daemon.token``
       with mode ``0o600``.

    Returns the resolved token. The daemon's auth middleware should
    call this once at startup and treat the return value as the
    canonical token for the lifetime of the process.
    """
    settings.ensure_dirs()
    path = settings.data_dir / _TOKEN_FILE

    # Source 1: explicit override wins.
    if settings.auth_token:
        token = settings.auth_token
        _write_token(path, token)
        return token

    # Source 2: re-use existing token.
    if path.is_file():
        try:
            token = path.read_text(encoding="utf-8").strip()
        except OSError:
            token = ""
        if token:
            return token

    # Source 3: mint a fresh token.
    token = secrets.token_urlsafe(32)
    _write_token(path, token)
    return token


def _write_token(path: Path, token: str) -> None:
    """Atomically write the token file with restrictive perms."""
    tmp = path.with_suffix(".token.tmp")
    tmp.write_text(token, encoding="utf-8")
    _restrict_perms(tmp)
    os.replace(tmp, path)
    _restrict_perms(path)


def write_daemon_url(settings: Settings, url: str) -> None:
    """
    Persist the URL the daemon is listening on for client discovery.

    Idempotent: writes only if the value would change.
    """
    settings.ensure_dirs()
    path = settings.data_dir / _URL_FILE
    url = url.rstrip("/")
    try:
        existing = path.read_text(encoding="utf-8").strip() if path.is_file() else ""
    except OSError:
        existing = ""
    if existing == url:
        return
    tmp = path.with_suffix(".url.tmp")
    tmp.write_text(url, encoding="utf-8")
    os.replace(tmp, path)


def read_auth_token(settings: Settings) -> str | None:
    """
    Client-side helper: read the per-machine token from disk.

    Used by the CLI when talking to its own daemon, and by the Wails
    desktop app indirectly via the same file.
    """
    path = settings.data_dir / _TOKEN_FILE
    if not path.is_file():
        return None
    try:
        token = path.read_text(encoding="utf-8").strip()
        return token or None
    except OSError:
        return None
