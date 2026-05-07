"""Tests for the secrets vault — handles, keychain backend, auto-redaction.

The keyring backend is replaced by a fake in-memory dict so tests don't
write to the real OS Credential Manager / Keychain.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import keyring
import pytest

from memex.secrets import (
    SecretHandle,
    SecretNotFoundError,
    SecretsStore,
    parse_handle,
    redact,
)


class _FakeKeyring:
    """In-memory keyring stand-in. Matches the bits of the keyring API
    memex actually uses — set_password / get_password / delete_password."""

    def __init__(self) -> None:
        self._d: dict[tuple[str, str], str] = {}

    def set_password(self, service: str, username: str, password: str) -> None:
        self._d[(service, username)] = password

    def get_password(self, service: str, username: str) -> str | None:
        return self._d.get((service, username))

    def delete_password(self, service: str, username: str) -> None:
        if (service, username) not in self._d:
            from keyring.errors import PasswordDeleteError
            raise PasswordDeleteError("absent")
        del self._d[(service, username)]


@pytest.fixture
def fake_keyring(monkeypatch: pytest.MonkeyPatch) -> _FakeKeyring:
    """Replace the real keyring backend with the fake for the test."""
    fake = _FakeKeyring()
    monkeypatch.setattr(keyring, "set_password", fake.set_password)
    monkeypatch.setattr(keyring, "get_password", fake.get_password)
    monkeypatch.setattr(keyring, "delete_password", fake.delete_password)
    return fake


# ---- handle parsing ---------------------------------------------------------


def test_parse_handle_roundtrip():
    h = parse_handle("secret://github/api-token")
    assert h is not None
    assert h.provider == "github"
    assert h.name == "api-token"
    assert str(h) == "secret://github/api-token"


def test_parse_handle_rejects_garbage():
    assert parse_handle("not-a-secret") is None
    assert parse_handle("") is None
    assert parse_handle("secret://") is None
    assert parse_handle("secret://just-provider") is None


def test_parse_handle_allows_dotted_provider_and_path_name():
    h = parse_handle("secret://tenant.acme/admin/jwt-key")
    assert h is not None
    assert h.provider == "tenant.acme"
    assert h.name == "admin/jwt-key"


# ---- store put / get / delete / list -----------------------------------------


def test_secrets_store_put_and_get(tmp_path: Path, fake_keyring):
    store = SecretsStore(tmp_path)
    handle = store.put("github", "ci-token", "ghp_abcdef123456")
    assert str(handle) == "secret://github/ci-token"
    # Get returns the value.
    assert store.get(handle) == "ghp_abcdef123456"
    # Get accepts handle string too.
    assert store.get("secret://github/ci-token") == "ghp_abcdef123456"


def test_secrets_store_get_missing_raises(tmp_path: Path, fake_keyring):
    store = SecretsStore(tmp_path)
    with pytest.raises(SecretNotFoundError):
        store.get("secret://github/never-stored")


def test_secrets_store_get_invalid_handle_raises(tmp_path: Path, fake_keyring):
    store = SecretsStore(tmp_path)
    with pytest.raises(ValueError):
        store.get("not-a-handle-at-all")


def test_secrets_store_delete(tmp_path: Path, fake_keyring):
    store = SecretsStore(tmp_path)
    store.put("openai", "demo", "sk-abc")
    assert store.delete("secret://openai/demo") is True
    with pytest.raises(SecretNotFoundError):
        store.get("secret://openai/demo")
    # Second delete is a clean no-op.
    assert store.delete("secret://openai/demo") is False


def test_secrets_store_list_does_not_leak_values(tmp_path: Path, fake_keyring):
    store = SecretsStore(tmp_path)
    store.put("github", "ci", "ghp_aaaa")
    store.put("openai", "default", "sk-bbbb")
    rows = store.list()
    assert len(rows) == 2
    # The list payload must NOT contain any secret values, only metadata.
    payload = [r.to_dict() for r in rows]
    flat = repr(payload)
    assert "ghp_aaaa" not in flat
    assert "sk-bbbb" not in flat


def test_secrets_store_get_records_audit_trail(tmp_path: Path, fake_keyring):
    store = SecretsStore(tmp_path)
    store.put("github", "ci", "ghp_abc123")
    rows = store.list()
    assert rows[0].last_resolved_at is None  # never resolved yet
    store.get("secret://github/ci", actor="claude_code")
    rows = store.list()
    assert rows[0].last_resolved_at is not None
    assert rows[0].last_resolved_by == "claude_code"


def test_index_persists_across_instances(tmp_path: Path, fake_keyring):
    s1 = SecretsStore(tmp_path)
    s1.put("a", "x", "value-1")
    s2 = SecretsStore(tmp_path)
    rows = s2.list()
    assert len(rows) == 1 and rows[0].provider == "a" and rows[0].name == "x"


# ---- auto-redaction ---------------------------------------------------------


def test_redact_replaces_github_pat(tmp_path: Path, fake_keyring):
    store = SecretsStore(tmp_path)
    text = "headers = {'Authorization': 'token ghp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'}"
    result = redact(text, store)
    assert result.changed
    assert "ghp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" not in result.redacted_text
    assert "secret://github/" in result.redacted_text


def test_redact_preserves_handles_when_already_redacted(tmp_path: Path, fake_keyring):
    """Re-running redact on an already-redacted string is a no-op."""
    store = SecretsStore(tmp_path)
    once = redact(
        "key = ghp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", store
    )
    twice = redact(once.redacted_text, store)
    assert twice.redacted_text == once.redacted_text
    assert twice.events == []


def test_redact_handles_multiple_secrets_in_one_string(tmp_path: Path, fake_keyring):
    store = SecretsStore(tmp_path)
    text = textwrap.dedent("""
        OPENAI_KEY = sk-aaaaaaaaaaaaaaaaaaaaaaaaaaaa
        GITHUB_PAT = ghp_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
    """).strip()
    result = redact(text, store)
    assert len(result.events) >= 2
    assert "sk-aaaaaaaaaaaaaaaaaaaaaaaaaaaa" not in result.redacted_text
    assert "ghp_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb" not in result.redacted_text


def test_redact_jwt_pattern(tmp_path: Path, fake_keyring):
    store = SecretsStore(tmp_path)
    text = (
        "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.aFKeRPo3NMwnj"
    )
    result = redact(text, store)
    assert result.changed
    assert "eyJhbGciOiJIUzI1NiJ9" not in result.redacted_text


def test_redact_empty_text_is_noop(tmp_path: Path, fake_keyring):
    store = SecretsStore(tmp_path)
    result = redact("", store)
    assert not result.changed
    assert result.redacted_text == ""


def test_redacted_value_is_resolvable_via_handle(tmp_path: Path, fake_keyring):
    """Round-trip: redact a secret, then resolve the handle to recover it."""
    store = SecretsStore(tmp_path)
    secret_value = "ghp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    result = redact(f"key = {secret_value}", store)
    assert result.changed
    handle_str = result.events[0].handle
    assert store.get(handle_str) == secret_value
