"""Tests for the bulk_graph MCP tool — the cognitive-workflow primitive."""

from __future__ import annotations

import pytest

from memex.core.engine import Engine
from memex.frontends.mcp.server import MCPServer


@pytest.fixture
def mcp_with_engine(engine: Engine):
    """Build an MCP server with tools registered against the test engine.

    Aggregator is suppressed by patching upstream loading to return empty —
    we don't want the test to try to start subprocesses.
    """
    server = MCPServer(engine)
    return server.mcp, engine


def _call_tool(mcp, name: str, **kwargs):
    """Resolve a registered MCP tool by name and call its handler directly.

    FastMCP exposes registered tools via `_tool_manager`; the handler is the
    Python callable we bound, so we can call it without going through stdio.
    """
    tool = mcp._tool_manager.get_tool(name)
    assert tool is not None, f"tool {name!r} not registered"
    return tool.fn(**kwargs)


def test_bulk_graph_creates_nodes_and_resolves_local_ids(mcp_with_engine):
    mcp, engine = mcp_with_engine
    result = _call_tool(
        mcp,
        "bulk_graph",
        nodes=[
            {
                "local_id": "$proj",
                "name": "v0.7 push",
                "kind": "project",
                "description": "test project",
            },
            {
                "local_id": "$t1",
                "kind": "task",
                "title": "first task",
                "priority": "p1",
                "project_id": "$proj",
            },
            {
                "local_id": "$t2",
                "kind": "task",
                "title": "second task",
                "priority": "p2",
                "project_id": "$proj",
                "blocked_by": ["$t1"],
            },
        ],
        edges=[],
    )

    assert result["errors"] == []
    assert "$proj" in result["nodes"]
    assert "$t1" in result["nodes"]
    assert "$t2" in result["nodes"]
    assert len(result["all_node_ids"]) == 3

    proj_id = result["nodes"]["$proj"]
    assert engine.get(proj_id) is not None
    assert engine.get(proj_id).name == "v0.7 push"


def test_bulk_graph_creates_explicit_edges(mcp_with_engine):
    mcp, engine = mcp_with_engine
    result = _call_tool(
        mcp,
        "bulk_graph",
        nodes=[
            {"local_id": "$a", "name": "concept A", "kind": "fact"},
            {"local_id": "$b", "name": "concept B", "kind": "fact"},
            {"local_id": "$c", "name": "concept C", "kind": "fact"},
        ],
        edges=[
            {"from": "$a", "to": "$b", "kind": "depends_on"},
            {"from": "$b", "to": "$c", "kind": "implements"},
        ],
    )

    assert result["errors"] == []
    assert result["edges_created"] == 2


def test_bulk_graph_supports_real_ids_in_edges(mcp_with_engine):
    mcp, engine = mcp_with_engine

    # Pre-existing node — created the normal way.
    pre = engine.add(name="pre-existing", description="", kind="fact")

    result = _call_tool(
        mcp,
        "bulk_graph",
        nodes=[
            {"local_id": "$new", "name": "new node", "kind": "decision"},
        ],
        edges=[
            {"from": "$new", "to": pre.id, "kind": "supersedes"},
        ],
    )

    assert result["errors"] == []
    assert result["edges_created"] == 1


def test_bulk_graph_partial_success_reports_errors(mcp_with_engine):
    mcp, engine = mcp_with_engine
    result = _call_tool(
        mcp,
        "bulk_graph",
        nodes=[
            {"local_id": "$ok", "name": "valid", "kind": "fact"},
            {"local_id": "$bad", "name": "bad", "kind": "not-a-real-kind"},
        ],
        edges=[
            {"from": "$ok", "to": "c_does_not_exist", "kind": "relates_to"},
        ],
    )

    assert "$ok" in result["nodes"]
    assert "$bad" not in result["nodes"]
    # The bad node yielded a node-phase error.
    node_errors = [e for e in result["errors"] if e["phase"] == "node"]
    assert len(node_errors) >= 1


def test_bulk_graph_empty_call_is_a_noop(mcp_with_engine):
    mcp, _engine = mcp_with_engine
    result = _call_tool(mcp, "bulk_graph", nodes=[], edges=[])
    assert result == {
        "nodes": {},
        "all_node_ids": [],
        "edges_created": 0,
        "errors": [],
    }
