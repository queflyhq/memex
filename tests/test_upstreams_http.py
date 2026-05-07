"""
Tests for HTTP upstream support — schema validation + config rendering.

Live HTTP connection tests are out of scope here (would need a running MCP
server). The plumbing is exercised by `memex upstream test <name>` once
configured; what we test in CI is that:

  - the schema validates HTTP config shape correctly
  - missing fields raise loudly
  - the catalog can render HTTP entries to a valid UpstreamConfig
  - AuthConfig resolves tokens from env vars and surfaces missing-env loudly
"""

from __future__ import annotations

import os

import pytest

from memex.upstreams.catalog import CatalogEntry, find_entry
from memex.upstreams.config import AuthConfig, UpstreamConfig


def test_stdio_config_requires_command() -> None:
    with pytest.raises(ValueError, match="requires `command`"):
        UpstreamConfig(name="bad", type="stdio")


def test_http_config_requires_url() -> None:
    with pytest.raises(ValueError, match="requires `url`"):
        UpstreamConfig(name="bad", type="http")


def test_http_config_rejects_bare_url() -> None:
    with pytest.raises(ValueError, match="must be http:// or https://"):
        UpstreamConfig(name="bad", type="http", url="example.com/mcp")


def test_http_config_minimal_no_auth() -> None:
    cfg = UpstreamConfig(name="public", type="http", url="https://example.com/mcp/")
    assert cfg.type == "http"
    assert cfg.url == "https://example.com/mcp/"
    assert cfg.auth is None
    # Defaults preserved
    assert cfg.allow == ["*"]
    assert cfg.prefix == "{name}__"


def test_http_config_with_bearer_env_auth() -> None:
    cfg = UpstreamConfig(
        name="github-remote",
        type="http",
        url="https://api.githubcopilot.com/mcp/",
        auth=AuthConfig(kind="bearer", token_env="GITHUB_TOKEN"),
    )
    assert cfg.auth.kind == "bearer"
    assert cfg.auth.token_env == "GITHUB_TOKEN"


def test_authconfig_bearer_requires_token_source() -> None:
    with pytest.raises(ValueError, match="requires one of"):
        AuthConfig(kind="bearer")  # no token, token_env, or token_keychain


def test_authconfig_header_requires_header_name() -> None:
    with pytest.raises(ValueError, match="header_name"):
        AuthConfig(kind="header", token_env="X")


def test_authconfig_resolves_token_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEMEX_TEST_TOKEN", "secret-value")
    auth = AuthConfig(kind="bearer", token_env="MEMEX_TEST_TOKEN")
    assert auth.resolve_token() == "secret-value"


def test_authconfig_loud_failure_when_env_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MEMEX_NOT_SET", raising=False)
    auth = AuthConfig(kind="bearer", token_env="MEMEX_NOT_SET")
    with pytest.raises(RuntimeError, match="env var `MEMEX_NOT_SET`"):
        auth.resolve_token()


def test_catalog_loads_http_entry() -> None:
    e = find_entry("github-remote")
    assert e is not None
    assert e.type == "http"
    assert e.url.startswith("https://")
    assert e.auth.get("kind") == "bearer"
    assert "GITHUB_TOKEN" in e.auth.get("token_env", "")


def test_catalog_renders_http_entry_to_valid_config() -> None:
    e = find_entry("github-remote")
    rendered = e.render()
    cfg = UpstreamConfig.model_validate(rendered)
    assert cfg.type == "http"
    assert cfg.url == "https://api.githubcopilot.com/mcp/"
    assert cfg.auth.kind == "bearer"
    assert cfg.auth.token_env == "GITHUB_TOKEN"


def test_catalog_renders_stdio_entry_to_valid_config() -> None:
    e = find_entry("github")
    rendered = e.render()
    cfg = UpstreamConfig.model_validate(rendered)
    assert cfg.type == "stdio"
    assert cfg.command == "npx"
    assert "@modelcontextprotocol/server-github" in " ".join(cfg.args)


def test_catalog_rich_fields_optional() -> None:
    """Old entries without rich teaching fields still parse."""
    e = CatalogEntry.from_dict({
        "id": "minimal",
        "name": "minimal",
        "category": "test",
        "summary": "minimal entry",
        "homepage": "",
        "command": "echo",
    })
    assert e.long_description == ""
    assert e.memex_value == ""
    assert e.setup_steps == []
    assert e.usage_examples == []
    assert e.paired_skills == []


def test_catalog_back_compat_description_to_summary() -> None:
    """Entries that used the old `description` key still load — value is
    surfaced as both `summary` and `description` (back-compat property)."""
    e = CatalogEntry.from_dict({
        "id": "legacy",
        "name": "legacy",
        "category": "test",
        "description": "old one-liner",
        "homepage": "",
        "command": "echo",
    })
    assert e.summary == "old one-liner"
    assert e.description == "old one-liner"


def test_catalog_rich_entry_fully_populated() -> None:
    """The github exemplar should carry every rich field."""
    e = find_entry("github")
    assert e.long_description != ""
    assert e.memex_value != ""
    assert len(e.setup_steps) >= 3
    assert len(e.usage_examples) >= 3
    assert "github-pr-hygiene" in e.paired_skills
