"""
Curated catalog of upstream MCP servers.

The catalog is shipped as `catalog.json` next to this module. It contains
templates for popular MCP servers (filesystem, github, slack, linear, …)
that pair well with memex. Templates use simple substitutions:

  {{cwd}}                  → current working directory at install time
  {{secret:VAR_NAME}}      → reference an env-var; `memex upstream install`
                              keeps the placeholder so the user can fill it
                              in via shell environment, never written to disk

Each entry teaches three things:

  1. What this MCP does (`summary` + `long_description`)
  2. How to set it up + how to use it (`setup_steps` + `usage_examples`)
  3. Why memex makes it better (`memex_value`) — the auto-capture and
     consolidation angle that's unique to running this MCP behind the
     memex gateway, plus any `paired_skills` that activate alongside it.

CLI surface:
  memex upstream catalog              → list all entries grouped by category
  memex upstream show <id>            → full entry detail (rich)
  memex upstream install <id>         → render template + append to upstreams.json
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any, Literal


@dataclass
class UsageExample:
    """One example tool call to teach the user what's available."""

    tool: str
    what: str            # one-line description of what the call does
    example: str = ""    # optional: example call shape

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> UsageExample:
        return cls(
            tool=d["tool"],
            what=d.get("what", ""),
            example=d.get("example", ""),
        )


@dataclass
class CatalogEntry:
    id: str
    name: str
    category: str
    summary: str          # one-liner shown in lists; was `description` pre-0.7
    homepage: str

    # transport
    type: Literal["stdio", "http"] = "stdio"

    # stdio fields
    command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)

    # http fields
    url: str = ""
    auth: dict[str, Any] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)

    # rich teaching fields (all optional — older entries still load)
    long_description: str = ""             # markdown body
    memex_value: str = ""                  # what memex uniquely adds
    setup_steps: list[str] = field(default_factory=list)
    usage_examples: list[UsageExample] = field(default_factory=list)
    paired_skills: list[str] = field(default_factory=list)

    # legacy + indexing
    requires: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> CatalogEntry:
        # Support pre-0.7 entries that used `description` for the one-liner.
        summary = d.get("summary") or d.get("description") or ""
        return cls(
            id=d["id"],
            name=d["name"],
            category=d.get("category", "misc"),
            summary=summary,
            homepage=d.get("homepage", ""),
            type=d.get("type", "stdio"),
            command=d.get("command", ""),
            args=list(d.get("args", [])),
            env=dict(d.get("env", {})),
            url=d.get("url", ""),
            auth=dict(d.get("auth", {})),
            headers=dict(d.get("headers", {})),
            long_description=d.get("long_description", ""),
            memex_value=d.get("memex_value", ""),
            setup_steps=list(d.get("setup_steps", [])),
            usage_examples=[UsageExample.from_dict(x) for x in d.get("usage_examples", [])],
            paired_skills=list(d.get("paired_skills", [])),
            requires=list(d.get("requires", [])),
            tags=list(d.get("tags", [])),
        )

    # Back-compat property — old code paths that read `entry.description`.
    @property
    def description(self) -> str:
        return self.summary

    def render(self, *, cwd: Path | None = None) -> dict[str, Any]:
        """Materialize the template into a concrete UpstreamConfig-shaped dict.

        `{{cwd}}` is substituted to the given cwd or `Path.cwd()`.
        `{{secret:VAR}}` placeholders are LEFT IN PLACE so the user can
        replace them with real env-var references in their shell init,
        rather than having secrets baked into upstreams.json.
        """
        cwd_str = str(cwd or Path.cwd())

        def sub(s: str) -> str:
            return s.replace("{{cwd}}", cwd_str)

        if self.type == "http":
            out: dict[str, Any] = {
                "name": self.name,
                "type": "http",
                "url": sub(self.url),
            }
            if self.auth:
                out["auth"] = dict(self.auth)
            if self.headers:
                out["headers"] = {k: sub(v) for k, v in self.headers.items()}
            return out

        # stdio (default)
        return {
            "name": self.name,
            "type": "stdio",
            "command": sub(self.command),
            "args": [sub(a) for a in self.args],
            "env": {k: sub(v) for k, v in self.env.items()},
        }


def load_catalog() -> list[CatalogEntry]:
    """Load and parse the bundled catalog.json."""
    raw = resources.files("memex.upstreams").joinpath("catalog.json").read_text(encoding="utf-8")
    data = json.loads(raw)
    return [CatalogEntry.from_dict(e) for e in data.get("entries", [])]


def find_entry(catalog_id: str) -> CatalogEntry | None:
    for e in load_catalog():
        if e.id == catalog_id:
            return e
    return None


def by_category() -> dict[str, list[CatalogEntry]]:
    out: dict[str, list[CatalogEntry]] = {}
    for e in load_catalog():
        out.setdefault(e.category, []).append(e)
    return out
