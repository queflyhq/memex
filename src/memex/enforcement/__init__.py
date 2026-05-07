"""Layer-4 enforcement — memory-backed gates that auto-approve or block
PreToolUse calls before the user is prompted.

The model:
  Stored policies (`kind=approval_policy` concepts) describe patterns
  the user has explicitly trusted (auto-approve) or banned (auto-deny).
  When Claude Code fires PreToolUse, `should_approve(tool, args, ctx)`
  consults the policy set and returns one of:
    - approve: user is NOT prompted; the tool runs.
    - deny:    user is NOT prompted; the tool is rejected.
    - ask:     no policy applies; default behavior (user prompt) fires.

Every approve/deny is observed (`auto_approval` / `auto_deny` events)
so the user can audit "what did memex approve while I wasn't looking"
via `memex progress` or the desktop Inbox.

Policies are conservative by default — memex ships with NO policies.
The user opts in per pattern via `memex policy add` or by promoting a
"you keep approving X — promote?" Inbox suggestion (Layer 5).
"""

from memex.enforcement.policies import (
    ApprovalDecision,
    ApprovalPolicy,
    PolicyMatch,
    add_policy,
    afk_status,
    disable_afk_mode,
    enable_afk_mode,
    list_policies,
    remove_policy,
    should_approve,
)

__all__ = [
    "ApprovalDecision",
    "ApprovalPolicy",
    "PolicyMatch",
    "add_policy",
    "afk_status",
    "disable_afk_mode",
    "enable_afk_mode",
    "list_policies",
    "remove_policy",
    "should_approve",
]
