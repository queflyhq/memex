"""Tests for memex.frontends.render — the markdown helpers used by the MCP frontend."""

from __future__ import annotations

from memex.core.schema import Concept, NodeKind, Source
from memex.frontends.render import (
    render_added_task,
    render_next_actions,
    render_project_view,
    render_task_list,
    task_summary,
)


def _task(id_: str, name: str, status: str = "pending", priority: str = "p1") -> Concept:
    return Concept(
        id=id_,
        name=name,
        description=f"Body for {name}",
        kind=NodeKind.task,
        source=Source.agent,
        confidence=0.5,
        metadata={"status": status, "priority": priority, "owner": "piyush"},
    )


def _project(id_: str, name: str) -> Concept:
    return Concept(
        id=id_,
        name=name,
        description="Project description",
        kind=NodeKind.project,
        source=Source.claude_code,
        confidence=1.0,
    )


def test_render_task_list_empty_returns_dim_message() -> None:
    out = render_task_list([])
    assert "no matching tasks" in out


def test_render_task_list_emits_table_with_ids() -> None:
    tasks = [_task("c_a1", "A1 — proto"), _task("c_a2", "A2 — server")]
    out = render_task_list(tasks)
    assert "| pri | status | task | id |" in out
    assert "`c_a1`" in out and "`c_a2`" in out
    assert "A1 — proto" in out


def test_render_task_list_shows_blockers() -> None:
    tasks = [_task("c_a1", "A1"), _task("c_a2", "A2")]
    blockers = {"c_a2": ["c_a1"]}
    out = render_task_list(tasks, blockers=blockers)
    # Last 8 chars of c_a1 are present in the blocker cell.
    assert "c_a1"[-8:] in out


def test_render_next_actions_includes_downstream_count() -> None:
    tasks = [_task("c_a1", "A1 — proto")]
    out = render_next_actions(tasks, downstream={"c_a1": 4})
    assert "gates 4 downstream" in out
    assert "`c_a1`" in out


def test_render_next_actions_empty() -> None:
    assert "nothing actionable" in render_next_actions([])


def test_render_project_view_groups_by_phase() -> None:
    proj = _project("c_proj", "Reach homelab demo")
    tasks = [
        _task("c_a1", "A1 — proto"),
        _task("c_a2a", "A2a — server"),
        _task("c_b5", "B5 — verify cluster"),
        _task("c_c7", "C7 — demo", status="blocked"),
    ]
    blockers = {"c_a2a": ["c_a1"], "c_c7": ["c_a2a", "c_b5"]}
    blocks_what = {"c_a1": ["c_a2a"], "c_a2a": ["c_c7"], "c_b5": ["c_c7"]}
    out = render_project_view(proj, tasks, blockers, blocks_what)
    assert "Reach homelab demo" in out
    assert "## Phase A" in out
    assert "## Phase B" in out
    assert "## Phase C" in out
    assert "← needs A1" in out  # blocker name resolution
    assert "→ gates 1" in out  # downstream count for c_a1


def test_render_added_task_short_confirm() -> None:
    out = render_added_task(_task("c_x1", "Foo"))
    assert "+ added" in out
    assert "`c_x1`" in out
    assert "Foo" in out


def test_task_summary_projection_drops_full_body() -> None:
    t = _task("c_a1", "A1 — proto")
    s = task_summary(t)
    assert s["id"] == "c_a1"
    assert s["status"] == "pending"
    assert s["headline"] == "Body for A1 — proto"
    assert "description" not in s  # full body dropped


# --- recall / add_node / update_task / progress renders -----------------

from memex.frontends.render import (
    render_added_node,
    render_progress,
    render_recall,
    render_updated_task,
)


def test_render_recall_groups_by_kind_and_shows_degraded_banner() -> None:
    result = {
        "nodes": [
            {"id": "c_d1", "name": "Use Postgres", "kind": "decision", "description": "long\nbody", "confidence": 1.0},
            {"id": "c_c1", "name": "No --no-verify", "kind": "constraint", "description": "rule", "confidence": 1.0},
            {"id": "c_f1", "name": "Tenant routing", "kind": "fact", "description": "via D1", "confidence": 0.7},
        ],
        "edges": [
            {"from_id": "c_d1", "to_id": "c_f1", "kind": "supersedes"},
            {"from_id": "c_d1", "to_id": "c_c1", "kind": "relates_to"},
        ],
        "tokens_used": 800,
        "strategy": "bm25",
        "degraded": True,
        "degraded_reason": "embed tier missing",
    }
    out = render_recall(result)
    assert "Degraded recall" in out
    assert "embed tier missing" in out
    assert "🎯 decision (1)" in out
    assert "🛡 constraint (1)" in out
    assert "📄 fact (1)" in out
    assert "Use Postgres" in out
    assert "`c_d1`" in out
    # Meaningful edges (supersedes) appear before relates_to.
    assert out.index("supersedes") < out.index("relates_to")
    assert "_conf_ 0.70" in out  # low-confidence node shows score
    assert "tokens: 800" in out


def test_render_recall_empty() -> None:
    assert "no concepts matched" in render_recall({"nodes": [], "edges": []})


def test_render_added_node_includes_kind_icon_and_id() -> None:
    c = Concept(
        id="c_x1",
        name="Foo",
        description="Some body line",
        kind=NodeKind.decision,
        source=Source.agent,
        confidence=0.8,
    )
    out = render_added_node(c)
    assert "🎯" in out
    assert "`c_x1`" in out
    assert "Foo" in out
    assert "_conf_ 0.80" in out


def test_render_updated_task_shows_status_diff() -> None:
    before = _task("c_a1", "A1", status="pending", priority="p1")
    after = _task("c_a1", "A1", status="completed", priority="p1")
    out = render_updated_task(before, after)
    assert "↻ updated" in out
    assert "pending → ✓ completed" in out


def test_render_updated_task_no_changes() -> None:
    t = _task("c_a1", "A1")
    out = render_updated_task(t, t)
    assert "no metadata changed" in out


def test_render_progress_includes_skill_list() -> None:
    snap = {
        "validated_skills": ["task-decomposition", "blocker-resolution"],
        "validations_count": 5,
        "concepts_total": 42,
        "events_total": 100,
        "actor_filter": "agent",
    }
    out = render_progress(snap)
    assert "concepts: **42**" in out
    assert "events: **100**" in out
    assert "validations: **5**" in out
    assert "task-decomposition" in out
    assert "agent" in out
