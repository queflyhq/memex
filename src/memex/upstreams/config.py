"""
Configuration for upstream MCP servers.

Discovery order (first match wins; project-level overrides user-level):
  1. $MEMEX_UPSTREAMS_FILE if set
  2. ./.memex/upstreams.json   (project)
  3. ~/.memex/upstreams.json   (user)

Schema example:
  {
    "upstreams": [
      {
        "name": "slack",
        "type": "stdio",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-slack"],
        "env": {"SLACK_BOT_TOKEN": "xoxb-..."},
        "allow": ["*"],
        "deny":  ["chat.delete*"],
        "prefix": "{name}__"
      },
      {
        "name": "github-remote",
        "type": "http",
        "url": "https://api.githubcopilot.com/mcp/",
        "auth": {"kind": "bearer", "token_env": "GITHUB_TOKEN"},
        "headers": {"X-Memex-Client": "memex"}
      }
    ]
  }
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

log = logging.getLogger(__name__)


class AuthConfig(BaseModel):
    """How an HTTP upstream authenticates.

    `kind`:
      - `bearer`   — Authorization: Bearer <token>
      - `header`   — arbitrary single header (use `header_name`)
      - `oauth2`   — OAuth2 token retrieved out-of-band; we read it from
                     the OS keychain or env at connect time. The desktop
                     app handles the browser dance and token storage.
      - `none`     — no auth (default for purely public servers)

    Token resolution order: `token` (literal — discouraged) → `token_env`
    (env-var name) → `token_keychain` (OS keychain key, desktop only).
    """

    kind: Literal["bearer", "header", "oauth2", "none"] = "none"
    header_name: str | None = None  # for kind=header

    # token resolution
    token: str | None = None  # literal — discouraged; convenient for dev
    token_env: str | None = None  # env-var name
    token_keychain: str | None = None  # OS keychain key (desktop)

    @model_validator(mode="after")
    def _check_shape(self) -> AuthConfig:
        if self.kind in ("bearer", "header", "oauth2"):
            sources = [self.token, self.token_env, self.token_keychain]
            if not any(sources):
                raise ValueError(
                    f"auth.kind={self.kind} requires one of: token, token_env, "
                    "token_keychain"
                )
        if self.kind == "header" and not self.header_name:
            raise ValueError("auth.kind=header requires `header_name`")
        return self

    def resolve_token(self) -> str | None:
        """Resolve the token from env or keychain. Loud failure if missing."""
        if self.token:
            return self.token
        if self.token_env:
            val = os.environ.get(self.token_env)
            if not val:
                raise RuntimeError(
                    f"auth requires env var `{self.token_env}` but it's not set"
                )
            return val
        if self.token_keychain:
            try:
                import keyring
            except ImportError as e:
                raise RuntimeError(
                    "token_keychain set but `keyring` is not installed. "
                    "Install with: pip install keyring"
                ) from e
            val = keyring.get_password("memex", self.token_keychain)
            if not val:
                raise RuntimeError(
                    f"auth requires keychain entry `memex/{self.token_keychain}` "
                    "but it's empty"
                )
            return val
        return None


class UpstreamConfig(BaseModel):
    """One upstream MCP server.

    `name` is the short identifier used in tool prefixes and logs.
    `type` is one of:
      - `stdio` — long-lived subprocess, MCP over stdin/stdout
      - `http`  — remote MCP over Streamable HTTP transport (per the
                  modelcontextprotocol spec). SSE is also supported via
                  the same `url` if the server only speaks SSE.

    `allow` / `deny` are simple glob patterns matched against the upstream's
    tool names (without prefix). Default `["*"]` allows everything.

    `prefix` is a Python format string with `{name}` substituted. Use `""`
    for flat re-export (with collision risk) or `{name}__` for namespaced.
    """

    name: str
    type: Literal["stdio", "http"] = "stdio"

    # stdio
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str | None = None

    # http
    url: str | None = None
    auth: AuthConfig | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    timeout_seconds: float = 60.0

    # filtering
    allow: list[str] = Field(default_factory=lambda: ["*"])
    deny: list[str] = Field(default_factory=list)

    # naming
    prefix: str = "{name}__"

    @field_validator("name")
    @classmethod
    def _name_safe(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("upstream name must be non-empty")
        # Tool names are typically [a-zA-Z0-9_]; keep the prefix safe.
        if not all(c.isalnum() or c in "-_" for c in v):
            raise ValueError(f"upstream name `{v}` must be alphanumeric/hyphen/underscore")
        return v

    @model_validator(mode="after")
    def _check_type_shape(self) -> UpstreamConfig:
        if self.type == "stdio":
            if not self.command:
                raise ValueError(f"upstream `{self.name}` (stdio) requires `command`")
        elif self.type == "http":
            if not self.url:
                raise ValueError(f"upstream `{self.name}` (http) requires `url`")
            if not (self.url.startswith("http://") or self.url.startswith("https://")):
                raise ValueError(
                    f"upstream `{self.name}` url must be http:// or https://"
                )
        return self

    def render_prefix(self) -> str:
        return self.prefix.format(name=self.name)


class UpstreamsFile(BaseModel):
    upstreams: list[UpstreamConfig] = Field(default_factory=list)


def _candidate_paths() -> list[Path]:
    paths: list[Path] = []
    env_override = os.environ.get("MEMEX_UPSTREAMS_FILE")
    if env_override:
        paths.append(Path(env_override))
    paths.append(Path.cwd() / ".memex" / "upstreams.json")
    paths.append(Path.home() / ".memex" / "upstreams.json")
    return paths


def load_upstreams(explicit_path: Path | None = None) -> UpstreamsFile:
    """Load upstream config; returns an empty UpstreamsFile if none found.

    Loading never raises on missing files (gateway is opt-in). Malformed
    JSON or schema violations *do* raise — config errors should be loud.
    """
    paths = [explicit_path] if explicit_path else _candidate_paths()
    for p in paths:
        if p and p.is_file():
            log.info("loading upstream config from %s", p)
            data = json.loads(p.read_text(encoding="utf-8"))
            return UpstreamsFile.model_validate(data)
    return UpstreamsFile()
