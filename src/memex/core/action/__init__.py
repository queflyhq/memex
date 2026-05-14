"""
Action layer — decisions out.

What memex emits to the outside world: recalls served, auto-approve /
auto-deny verdicts, context injection into the AI's prompt, validate-
before-act gating against stored constraints. The Action layer is
what makes memex *load-bearing* rather than passive — its outputs
gate real tool calls.

Layer in the 5-layer OMP architecture:
  perception → context → cognition → memory → ACTION → (back to perception)

Re-exports — physical files live in:
  - memex.enforcement.policies (auto-approve / auto-deny rules)
  - memex.core.engine.Engine.check_action (validate-before-act)
  - memex.core.engine.Engine.calibrate_confidence (closing-edge feedback)
"""

from __future__ import annotations

from memex.enforcement.policies import (
    ApprovalDecision,
    ApprovalPolicy,
    PolicyMatch,
    add_policy,
    list_policies,
    remove_policy,
    should_approve,
    enable_afk_mode,
    disable_afk_mode,
)

__all__ = [
    "ApprovalDecision",
    "ApprovalPolicy",
    "PolicyMatch",
    "add_policy",
    "list_policies",
    "remove_policy",
    "should_approve",
    "enable_afk_mode",
    "disable_afk_mode",
]
