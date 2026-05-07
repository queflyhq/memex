"""
Upstream MCP gateway.

memex acts as an MCP server to AI clients (Claude Code, Cursor, custom agents)
and *also* as an MCP client to upstream MCP servers the user owns. Upstream
tools are aggregated and re-exposed alongside memex's native tools, with each
proxied call automatically captured as an episodic event.

This is the single perception primitive that makes memex an "agentic memory"
layer rather than a notebook the agent has to choose to write in: every tool
the agent calls flows through here and is observed for free.
"""

from __future__ import annotations

from memex.upstreams.aggregator import Aggregator, ProxiedTool, UpstreamConnection
from memex.upstreams.catalog import CatalogEntry, by_category, find_entry, load_catalog
from memex.upstreams.config import UpstreamConfig, UpstreamsFile, load_upstreams

__all__ = [
    "Aggregator",
    "CatalogEntry",
    "ProxiedTool",
    "UpstreamConfig",
    "UpstreamConnection",
    "UpstreamsFile",
    "by_category",
    "find_entry",
    "load_catalog",
    "load_upstreams",
]
