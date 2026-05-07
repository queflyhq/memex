"""
Daemon detection and auto-spawn for the MCP stdio process.

The Kuzu graph DB acquires an exclusive directory lock — two processes
opening the same DB collide. So when the user opens N parallel chat
sessions in VSCode/Cursor/Claude Code, we want all of them to proxy
to a single long-lived daemon process that holds the DB open, instead
of each spawning its own engine.

Flow on MCP stdio startup:
  1. Probe the daemon URL (HEAD on /health, sub-second).
  2. If alive — done, return the URL.
  3. If dead and a stale PID file exists — clean it up.
  4. Acquire a spawn-lock (atomic file create) — only one MCP wins.
     Other concurrent MCPs poll until the winner's daemon is up.
  5. Winner forks `memex daemon` as a detached background process,
     polls /health until it answers, then releases the spawn-lock.
"""

from __future__ import annotations

import contextlib
import logging
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx

from memex.config import Settings, get_settings

log = logging.getLogger(__name__)

PROBE_TIMEOUT_S = 0.5
SPAWN_WAIT_S = 20.0
SPAWN_POLL_INTERVAL_S = 0.1
SPAWN_LOCK_STALE_S = 30.0


def daemon_files(settings: Settings) -> tuple[Path, Path, Path]:
    """Returns (pid_file, lock_file, log_file) for the daemon."""
    base = settings.data_dir
    return base / "daemon.pid", base / "daemon.lock", base / "daemon.log"


def parse_listen(listen: str) -> tuple[str, int]:
    host, _, port_s = listen.partition(":")
    return host or "127.0.0.1", int(port_s)


def daemon_url(settings: Settings) -> str:
    """Compute the URL the daemon listens on, honoring an override env var."""
    if settings.daemon_url:
        return settings.daemon_url.rstrip("/")
    host, port = parse_listen(settings.listen)
    # 0.0.0.0 binds everywhere but clients still talk to it via 127.0.0.1.
    if host == "0.0.0.0":
        host = "127.0.0.1"
    return f"http://{host}:{port}"


def is_daemon_alive(url: str, auth_token: str | None = None) -> bool:
    """Cheap probe: HEAD on /health, fail fast."""
    headers = {"Authorization": f"Bearer {auth_token}"} if auth_token else {}
    try:
        r = httpx.get(f"{url}/health", headers=headers, timeout=PROBE_TIMEOUT_S)
        return r.status_code == 200
    except (httpx.HTTPError, OSError):
        return False


def _port_in_use(host: str, port: int) -> bool:
    """Is something bound to host:port? Used to detect non-memex squatters."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.2)
        try:
            s.connect((host, port))
            return True
        except (OSError, socket.timeout):
            return False


def _read_pid(pid_file: Path) -> int | None:
    try:
        return int(pid_file.read_text().strip())
    except (FileNotFoundError, ValueError):
        return None


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        # `tasklist` is slow; use OpenProcess via ctypes for sub-ms check.
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid
        )
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


@contextlib.contextmanager
def _spawn_lock(lock_file: Path):
    """File-based mutex: exactly one MCP gets to spawn the daemon at a time.

    Other concurrent MCPs see the lock and wait — they don't try to spawn.
    Stale locks (older than SPAWN_LOCK_STALE_S) are reclaimed.
    """
    deadline = time.monotonic() + SPAWN_WAIT_S
    held = False
    while time.monotonic() < deadline:
        try:
            fd = os.open(str(lock_file), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            held = True
            break
        except FileExistsError:
            try:
                age = time.time() - lock_file.stat().st_mtime
            except FileNotFoundError:
                continue
            if age > SPAWN_LOCK_STALE_S:
                with contextlib.suppress(FileNotFoundError):
                    lock_file.unlink()
                continue
            time.sleep(SPAWN_POLL_INTERVAL_S)
    try:
        yield held
    finally:
        if held:
            with contextlib.suppress(FileNotFoundError):
                lock_file.unlink()


def _spawn_detached(settings: Settings, pid_file: Path, log_file: Path) -> int:
    """Launch `memex daemon` as a fully detached background process."""
    settings.ensure_dirs()
    log_file.parent.mkdir(parents=True, exist_ok=True)
    log_fp = open(log_file, "ab", buffering=0)
    cmd = [sys.executable, "-m", "memex", "daemon"]
    kwargs: dict = {
        "stdin": subprocess.DEVNULL,
        "stdout": log_fp,
        "stderr": subprocess.STDOUT,
        "close_fds": True,
        "cwd": str(settings.data_dir),
    }
    if sys.platform == "win32":
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP — survive parent exit,
        # don't inherit stdio handles from the MCP stdio pipe.
        kwargs["creationflags"] = 0x00000008 | 0x00000200
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(cmd, **kwargs)
    pid_file.write_text(str(proc.pid))
    return proc.pid


def ensure_daemon(settings: Settings | None = None) -> str:
    """Make sure a memex daemon is reachable; return its URL.

    Idempotent and concurrency-safe: parallel callers will all see the same
    daemon. Raises RuntimeError only if spawning fails or the daemon doesn't
    come up within SPAWN_WAIT_S.
    """
    settings = settings or get_settings()
    url = daemon_url(settings)
    auth = settings.auth_token

    # Fast path: already alive.
    if is_daemon_alive(url, auth):
        return url

    pid_file, lock_file, log_file = daemon_files(settings)

    # Stale PID cleanup before we try to spawn.
    pid = _read_pid(pid_file)
    if pid is not None and not _pid_alive(pid):
        with contextlib.suppress(FileNotFoundError):
            pid_file.unlink()

    # Refuse to spawn if a non-memex process holds the port — fail loud
    # rather than crash later with a port-conflict on uvicorn startup.
    host, port = parse_listen(settings.listen)
    probe_host = "127.0.0.1" if host == "0.0.0.0" else host
    if _port_in_use(probe_host, port) and not is_daemon_alive(url, auth):
        raise RuntimeError(
            f"port {probe_host}:{port} is bound but /health did not respond — "
            "another process is squatting on the memex port. Set MEMEX_LISTEN "
            "or stop the offending process."
        )

    with _spawn_lock(lock_file) as we_won:
        # Re-check inside the lock — peer may have spawned while we waited.
        if is_daemon_alive(url, auth):
            return url
        if not we_won:
            raise RuntimeError(
                f"daemon spawn-lock held longer than {SPAWN_WAIT_S}s; aborting."
            )
        log.info("spawning memex daemon (logs: %s)", log_file)
        _spawn_detached(settings, pid_file, log_file)

        deadline = time.monotonic() + SPAWN_WAIT_S
        while time.monotonic() < deadline:
            if is_daemon_alive(url, auth):
                return url
            time.sleep(SPAWN_POLL_INTERVAL_S)

    raise RuntimeError(
        f"memex daemon did not become reachable at {url} within {SPAWN_WAIT_S}s — "
        f"see {log_file} for startup errors."
    )
