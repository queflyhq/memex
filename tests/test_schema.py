"""Schema validation tests — ensure Pydantic boundary enforcement works."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from memex.core.schema import Concept, Edge, EdgeKind, EpisodicEvent, NodeKind, Source


def test_concept_requires_nonempty_name():
    with pytest.raises(ValidationError):
        Concept(name="", description="x")


def test_concept_strips_whitespace_from_name():
    c = Concept(name="  hello  ")
    assert c.name == "hello"


def test_concept_default_kind_is_fact():
    c = Concept(name="x")
    assert c.kind is NodeKind.fact


def test_concept_id_has_prefix():
    c = Concept(name="x")
    assert c.id.startswith("c_")


def test_concept_confidence_clamped():
    with pytest.raises(ValidationError):
        Concept(name="x", confidence=1.5)
    with pytest.raises(ValidationError):
        Concept(name="x", confidence=-0.1)


def test_edge_construction():
    e = Edge(from_id="c_a", to_id="c_b", kind=EdgeKind.relates_to, source=Source.human)
    assert e.from_id == "c_a"
    assert e.kind is EdgeKind.relates_to


def test_episodic_event_id_has_prefix():
    ev = EpisodicEvent(kind="test", actor=Source.agent)
    assert ev.id.startswith("e_")


def test_node_kind_includes_approach():
    """approach is the kind for skill validation entries with `checks` lists."""
    assert NodeKind.approach.value == "approach"


def test_source_includes_skill():
    """skill is the source-attribution for nodes installed from a skill bundle."""
    assert Source.skill.value == "skill"
