"""SecretsStore — OS-keychain-backed secret vault with portable listing.

The handle URI scheme is `secret://<provider>/<name>` where `provider` is
a namespace (`github`, `openai`, `aws`, `tenant.acme`) and `name` is the
secret's identifier within that namespace. Provider names use dots as
sub-namespace separators; names use `/` only for grouping in the URI but
the keychain key is the full path joined with `:`.

The index file (`<data_dir>/secrets.json`) records `{provider, name,
created_at, last_resolved_at, last_resolved_by}` so:
  - `memex secret list` works without backend-specific iteration
  - audit trail captures WHEN and BY WHOM each secret was resolved
  - the index NEVER holds the literal value
"""

from __future__ import annotations

import json
import logging
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


_HANDLE_RE = re.compile(r"^secret://(?P<provider>[A-Za-z0-9_.-]+)/(?P<name>[A-Za-z0-9_./-]+)$")


@dataclass(frozen=True, slots=True)
class SecretHandle:
    """A reference to a stored secret. Stringifies as `secret://provider/name`."""
    provider: str
    name: str

    def __str__(self) -> str:
        return f"secret://{self.provider}/{self.name}"

    @property
    def keychain_service(self) -> str:
        """Service name used by `keyring`. memex namespaces every secret
        so no collision with other apps' credentials in the OS vault."""
        return f"memex:{self.provider}"


class SecretNotFoundError(LookupError):
    """Raised when a handle has no value in the OS keychain."""


def parse_handle(s: str) -> SecretHandle | None:
    """Parse a `secret://provider/name` URI. Returns None if it doesn't match."""
    if not s or not isinstance(s, str):
        return None
    m = _HANDLE_RE.match(s.strip())
    if not m:
        return None
    return SecretHandle(provider=m.group("provider"), name=m.group("name"))


@dataclass(slots=True)
class SecretIndex:
    """A row in the index file."""
    provider: str
    name: str
    created_at: str
    last_resolved_at: str | None = None
    last_resolved_by: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "name": self.name,
            "created_at": self.created_at,
            "last_resolved_at": self.last_resolved_at,
            "last_resolved_by": self.last_resolved_by,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> SecretIndex:
        return cls(
            provider=d["provider"],
            name=d["name"],
            created_at=d["created_at"],
            last_resolved_at=d.get("last_resolved_at"),
            last_resolved_by=d.get("last_resolved_by"),
            metadata=d.get("metadata", {}),
        )


class SecretsStore:
    """Owns the OS-keychain interactions plus the portable index file.

    Thread-safe: a single RLock guards the index file (the keyring backend
    has its own locking). The store is lazy — keyring is only imported on
    first use so test environments without a keychain backend don't
    explode at import time.
    """

    def __init__(self, data_dir: str | Path):
        self._data_dir = Path(data_dir)
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._index_path = self._data_dir / "secrets.json"
        self._lock = threading.RLock()

    # ---- index file --------------------------------------------------------

    def _load_index(self) -> dict[str, SecretIndex]:
        if not self._index_path.is_file():
            return {}
        try:
            data = json.loads(self._index_path.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            log.warning("secrets index corrupt — starting empty (%s): %s",
                        self._index_path, e)
            return {}
        out: dict[str, SecretIndex] = {}
        for row in data.get("secrets", []):
            try:
                rec = SecretIndex.from_dict(row)
            except Exception as e:  # noqa: BLE001
                log.warning("skipping malformed index row %r: %s", row, e)
                continue
            out[f"{rec.provider}/{rec.name}"] = rec
        return out

    def _save_index(self, index: dict[str, SecretIndex]) -> None:
        payload = {
            "version": 1,
            "secrets": [r.to_dict() for r in sorted(
                index.values(), key=lambda r: (r.provider, r.name)
            )],
        }
        tmp = self._index_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(self._index_path)

    # ---- public API --------------------------------------------------------

    def put(
        self,
        provider: str,
        name: str,
        value: str,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> SecretHandle:
        """Store a secret. Returns the handle (no value)."""
        if not value:
            raise ValueError("secret value cannot be empty")
        handle = SecretHandle(provider=provider, name=name)
        import keyring  # lazy
        with self._lock:
            keyring.set_password(handle.keychain_service, name, value)
            index = self._load_index()
            key = f"{provider}/{name}"
            now = datetime.now(timezone.utc).isoformat()
            existing = index.get(key)
            if existing is None:
                index[key] = SecretIndex(
                    provider=provider, name=name,
                    created_at=now, metadata=metadata or {},
                )
            else:
                if metadata:
                    existing.metadata.update(metadata)
            self._save_index(index)
        return handle

    def get(
        self,
        handle: SecretHandle | str,
        *,
        actor: str = "agent",
    ) -> str:
        """Resolve a handle to its literal value. Records audit info in
        the index. Raises SecretNotFoundError if the handle is unknown."""
        h = handle if isinstance(handle, SecretHandle) else parse_handle(handle)
        if h is None:
            raise ValueError(f"invalid secret handle: {handle!r}")
        import keyring  # lazy
        with self._lock:
            value = keyring.get_password(h.keychain_service, h.name)
            if value is None:
                raise SecretNotFoundError(f"no secret stored for {h}")
            index = self._load_index()
            key = f"{h.provider}/{h.name}"
            now = datetime.now(timezone.utc).isoformat()
            rec = index.get(key)
            if rec is not None:
                rec.last_resolved_at = now
                rec.last_resolved_by = actor
                self._save_index(index)
        return value

    def delete(self, handle: SecretHandle | str) -> bool:
        """Remove a secret from both the keychain and the index."""
        h = handle if isinstance(handle, SecretHandle) else parse_handle(handle)
        if h is None:
            raise ValueError(f"invalid secret handle: {handle!r}")
        import keyring  # lazy
        from keyring.errors import PasswordDeleteError
        with self._lock:
            removed_kc = False
            try:
                keyring.delete_password(h.keychain_service, h.name)
                removed_kc = True
            except PasswordDeleteError:
                # Already absent — fine; we still scrub the index row.
                pass
            index = self._load_index()
            key = f"{h.provider}/{h.name}"
            removed_idx = index.pop(key, None) is not None
            if removed_idx:
                self._save_index(index)
        return removed_kc or removed_idx

    def list(self) -> list[SecretIndex]:
        """Every secret currently registered. NEVER returns values."""
        with self._lock:
            return sorted(
                self._load_index().values(),
                key=lambda r: (r.provider, r.name),
            )

    def has(self, provider: str, name: str) -> bool:
        h = SecretHandle(provider=provider, name=name)
        import keyring  # lazy
        with self._lock:
            return keyring.get_password(h.keychain_service, h.name) is not None
