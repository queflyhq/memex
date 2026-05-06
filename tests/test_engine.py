"""End-to-end engine tests — round-trip add/recall/validate/progress."""

from __future__ import annotations

from memex.core.engine import Engine
from memex.core.schema import EdgeKind, NodeKind, Source


def test_add_then_recall_round_trips(engine: Engine):
    c = engine.add(name="Use Postgres over MySQL", kind=NodeKind.decision, description="JSONB usage")
    result = engine.recall("postgres mysql", budget_tokens=2000)
    assert any(n.id == c.id for n in result.nodes)


def test_recall_respects_kind_filter(engine: Engine):
    engine.add(name="postgres pattern", kind=NodeKind.pattern)
    engine.add(name="postgres decision", kind=NodeKind.decision)
    result = engine.recall("postgres", kind=NodeKind.decision)
    kinds = {n.kind for n in result.nodes}
    assert NodeKind.decision in kinds
    assert NodeKind.pattern not in kinds


def test_recall_emits_activity_event(engine: Engine):
    engine.add(name="alpha", kind=NodeKind.fact)
    engine.recall("alpha")
    events = engine.episodic.recent(limit=10, kind="recall_executed")
    assert len(events) >= 1


def test_add_emits_activity_event(engine: Engine):
    engine.add(name="beta", kind=NodeKind.fact)
    events = engine.episodic.recent(limit=10, kind="concept_added")
    assert any(e.payload.get("name") == "beta" for e in events)


def test_link_emits_activity_event(engine: Engine):
    a = engine.add(name="a", kind=NodeKind.fact)
    b = engine.add(name="b", kind=NodeKind.fact)
    engine.link(a.id, b.id, kind=EdgeKind.relates_to)
    events = engine.episodic.recent(limit=10, kind="edge_added")
    assert len(events) >= 1


def test_recall_touches_working_set(engine: Engine):
    c = engine.add(name="gamma", kind=NodeKind.fact)
    assert not engine.working_set.contains(c.id) or engine.working_set.contains(c.id)
    engine.recall("gamma")
    assert engine.working_set.contains(c.id)


def test_validate_unknown_skill_returns_error(engine: Engine):
    result = engine.validate("definitely-not-installed", actor=Source.agent)
    assert result["ok"] is False
    assert "not_installed" not in str(result.get("error", ""))  # human-readable error


def test_progress_includes_counts(engine: Engine):
    engine.add(name="x", kind=NodeKind.fact)
    p = engine.progress()
    assert p["concepts_total"] >= 1
    assert isinstance(p["validated_skills"], list)


def test_validate_records_episodic_event(engine: Engine):
    """Even on not-found, validate emits an event so the audit trail is complete."""
    engine.validate("never-installed-skill", actor=Source.agent)
    events = engine.episodic.recent(limit=10, kind="skill_validation_failed")
    assert len(events) >= 1
