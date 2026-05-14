"""Tests for Layer-4 auto-approval / auto-deny policies."""

from __future__ import annotations

import pytest

from memex.core.engine import Engine
from memex.enforcement import (
    ApprovalDecision,
    add_policy,
    list_policies,
    remove_policy,
    should_approve,
)


def test_should_approve_returns_ask_with_no_policies(engine: Engine):
    match = should_approve(engine, tool_name="Bash", tool_input={"command": "ls"})
    assert match.decision == ApprovalDecision.ask


def test_auto_approve_simple_tool_pattern(engine: Engine):
    add_policy(
        engine,
        tool_pattern="Bash",
        decision=ApprovalDecision.approve,
        args_match={"command": {"prefix": "git status"}},
        reason="git status is read-only",
    )
    match = should_approve(
        engine, tool_name="Bash", tool_input={"command": "git status"}
    )
    assert match.decision == ApprovalDecision.approve
    assert "git status" in match.reason


def test_auto_deny_force_push(engine: Engine):
    add_policy(
        engine,
        tool_pattern="Bash",
        decision=ApprovalDecision.deny,
        args_match={"command": {"regex": r"git push (--force|-f)\b"}},
        reason="force-push is gated — confirm with the user explicitly",
        priority=1,  # higher priority than approve policies
    )
    match = should_approve(
        engine, tool_name="Bash", tool_input={"command": "git push --force"}
    )
    assert match.decision == ApprovalDecision.deny


def test_first_matching_policy_wins_by_priority(engine: Engine):
    add_policy(
        engine,
        tool_pattern="Bash",
        decision=ApprovalDecision.approve,
        args_match={"command": {"prefix": "ls"}},
        reason="any ls is fine",
        priority=100,
    )
    add_policy(
        engine,
        tool_pattern="Bash",
        decision=ApprovalDecision.deny,
        args_match={"command": {"regex": r"\bls\b.*-z\b"}},
        reason="custom-z flag indicates suspicious script",
        priority=1,
    )
    match = should_approve(
        engine, tool_name="Bash", tool_input={"command": "ls -lah"}
    )
    assert match.decision == ApprovalDecision.approve
    match = should_approve(
        engine, tool_name="Bash", tool_input={"command": "ls -z dump"}
    )
    assert match.decision == ApprovalDecision.deny


def test_glob_args_match(engine: Engine):
    add_policy(
        engine,
        tool_pattern="Edit",
        decision=ApprovalDecision.approve,
        args_match={"file_path": "**/*.md"},
        reason="markdown edits are reversible",
    )
    match = should_approve(
        engine, tool_name="Edit",
        tool_input={"file_path": "/repo/docs/README.md"},
    )
    assert match.decision == ApprovalDecision.approve
    # .py edit doesn't match — falls through.
    match = should_approve(
        engine, tool_name="Edit", tool_input={"file_path": "/repo/code.py"},
    )
    assert match.decision == ApprovalDecision.ask


def test_disabled_policy_does_not_fire(engine: Engine):
    p = add_policy(
        engine,
        tool_pattern="Bash",
        decision=ApprovalDecision.approve,
        args_match={"command": {"prefix": "ls"}},
        reason="ls is fine",
        enabled=False,
    )
    assert p.metadata.get("enabled") is False
    match = should_approve(
        engine, tool_name="Bash", tool_input={"command": "ls"}
    )
    assert match.decision == ApprovalDecision.ask


def test_remove_policy(engine: Engine):
    p = add_policy(
        engine,
        tool_pattern="Bash",
        decision=ApprovalDecision.approve,
        args_match={"command": {"prefix": "ls"}},
        reason="ls is fine",
    )
    assert any(pol.id == p.id for pol in list_policies(engine))
    assert remove_policy(engine, p.id) is True
    assert all(pol.id != p.id for pol in list_policies(engine))


def test_invalid_regex_in_pattern_skipped(engine: Engine):
    """A policy with broken regex shouldn't crash should_approve — it's
    skipped (logged) and other policies still get evaluated."""
    add_policy(
        engine,
        tool_pattern="Bash[(unclosed",  # invalid regex
        decision=ApprovalDecision.approve,
        reason="broken",
    )
    add_policy(
        engine,
        tool_pattern="Bash",
        decision=ApprovalDecision.approve,
        args_match={"command": {"prefix": "ls"}},
        reason="ls is fine",
        priority=200,
    )
    match = should_approve(
        engine, tool_name="Bash", tool_input={"command": "ls"}
    )
    assert match.decision == ApprovalDecision.approve


def test_should_approve_observes_audit_event(engine: Engine):
    add_policy(
        engine,
        tool_pattern="Bash",
        decision=ApprovalDecision.approve,
        args_match={"command": {"prefix": "ls"}},
        reason="ls",
    )
    should_approve(engine, tool_name="Bash", tool_input={"command": "ls"})
    events = engine.episodic.recent(limit=5)
    auto_evs = [e for e in events if e.kind == "auto_approval"]
    assert auto_evs
    assert auto_evs[0].payload.get("tool_name") == "Bash"


# ---- hard-deny baseline -----------------------------------------------------


def test_hard_deny_blocks_rm_rf_root(engine: Engine):
    match = should_approve(
        engine, tool_name="Bash", tool_input={"command": "rm -rf /"}
    )
    assert match.decision == ApprovalDecision.deny
    assert match.policy_id == "builtin:hard_deny"


def test_hard_deny_blocks_force_push_to_main(engine: Engine):
    match = should_approve(
        engine, tool_name="Bash",
        tool_input={"command": "git push --force origin main"},
    )
    assert match.decision == ApprovalDecision.deny


def test_hard_deny_blocks_drop_database(engine: Engine):
    match = should_approve(
        engine, tool_name="Bash",
        tool_input={"command": "psql -c 'DROP DATABASE prod'"},
    )
    assert match.decision == ApprovalDecision.deny


def test_hard_deny_blocks_no_verify_commit(engine: Engine):
    """CLAUDE.md: never skip git hooks. Hard-deny verifies it."""
    match = should_approve(
        engine, tool_name="Bash",
        tool_input={"command": "git commit --no-verify -m 'wip'"},
    )
    assert match.decision == ApprovalDecision.deny


def test_hard_deny_runs_before_user_approval(engine: Engine):
    """Even if the user has a generous approval policy, hard-deny still
    fires for catastrophic patterns."""
    add_policy(
        engine,
        tool_pattern="Bash",
        decision=ApprovalDecision.approve,
        args_match={"command": {"prefix": "rm"}},
        reason="user trusts rm in general",
        priority=10,
    )
    match = should_approve(
        engine, tool_name="Bash", tool_input={"command": "rm -rf /"}
    )
    assert match.decision == ApprovalDecision.deny


# ---- AFK mode ---------------------------------------------------------------


def test_afk_mode_approves_everything_except_hard_deny(engine: Engine):
    from memex.enforcement import enable_afk_mode, disable_afk_mode, afk_status

    enable_afk_mode(engine, duration_hours=1.0, note="working through plan X")
    status = afk_status(engine)
    assert status is not None
    assert "plan X" in (status["note"] or "")

    # Anything benign auto-approved.
    match = should_approve(
        engine, tool_name="Bash", tool_input={"command": "ls"}
    )
    assert match.decision == ApprovalDecision.approve
    assert "AFK" in match.reason

    # Hard-deny still wins.
    match = should_approve(
        engine, tool_name="Bash", tool_input={"command": "rm -rf /"}
    )
    assert match.decision == ApprovalDecision.deny

    disable_afk_mode(engine)
    assert afk_status(engine) is None
    # After disable, ls reverts to ask.
    match = should_approve(
        engine, tool_name="Bash", tool_input={"command": "ls"}
    )
    assert match.decision == ApprovalDecision.ask


def test_afk_mode_audit_marks_afk_true(engine: Engine):
    from memex.enforcement import enable_afk_mode

    enable_afk_mode(engine, duration_hours=1.0, note="afk")
    should_approve(engine, tool_name="Bash", tool_input={"command": "ls"})
    events = engine.episodic.recent(limit=10)
    auto_evs = [e for e in events if e.kind == "auto_approval"]
    assert auto_evs
    assert auto_evs[0].payload.get("afk") is True


def test_afk_mode_idempotent_re_enable(engine: Engine):
    from memex.enforcement import enable_afk_mode, afk_status

    enable_afk_mode(engine, duration_hours=1.0, note="first")
    enable_afk_mode(engine, duration_hours=2.0, note="second")
    status = afk_status(engine)
    assert status is not None
    assert status["note"] == "second"
    assert status["duration_hours"] == 2.0


def test_afk_mode_expired_flag_auto_cleared(engine: Engine, monkeypatch):
    from memex.enforcement import enable_afk_mode, afk_status
    from datetime import datetime, timezone, timedelta

    enable_afk_mode(engine, duration_hours=0.01, note="quick")  # 36 sec
    # Manually rewind expires_at to the past.
    flag_id = afk_status(engine)["id"]
    flag = engine.get(flag_id)
    flag.metadata["expires_at"] = (
        datetime.now(timezone.utc) - timedelta(hours=1)
    ).isoformat()
    engine.put(flag)

    # Status call sees it's expired and clears it.
    assert afk_status(engine) is None
