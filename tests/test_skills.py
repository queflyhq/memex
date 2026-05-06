"""Skill bundle tests — load, install, validate."""

from __future__ import annotations

from memex.core.engine import Engine
from memex.skills import find_builtin_skill, install_skill, list_builtin_skills


def test_three_builtin_skills_ship():
    skills = {s.name for s in list_builtin_skills()}
    assert "using-memex" in skills
    assert "core-validations" in skills
    assert "python-stdlib" in skills


def test_install_core_validations_then_validate(engine: Engine):
    skill = find_builtin_skill("core-validations")
    assert skill is not None
    counts = install_skill(engine, skill)
    assert counts["concepts"] >= 5

    result = engine.validate("secure-subprocess")
    assert result["ok"] is True
    assert "checks" in result
    assert isinstance(result["checks"], list)
    assert len(result["checks"]) >= 3


def test_install_using_memex_meta_skill(engine: Engine):
    skill = find_builtin_skill("using-memex")
    assert skill is not None
    counts = install_skill(engine, skill)
    assert counts["concepts"] >= 1

    result = engine.validate("memex-protocol")
    assert result["ok"] is True
    assert "approach" in result
    # The meta-skill should mention the four core operations.
    approach_text = result["approach"].lower()
    assert "recall" in approach_text
    assert "validate" in approach_text


def test_skill_bundle_concepts_carry_provenance(engine: Engine):
    skill = find_builtin_skill("core-validations")
    install_skill(engine, skill)
    # Find one of the installed concepts and check provenance.
    concepts = engine.semantic.search_lexical("secure-subprocess", limit=5)
    assert any(c.metadata.get("skill") == "core-validations" for c in concepts)
