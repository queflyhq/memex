"""Approval-policy storage + matching.

Each policy is a `Concept(kind=constraint)` with `metadata.policy_type =
"approval"`. The shape:

    {
      tool_pattern: "Bash" | "Edit|Write" | "*"  # regex over tool name
      args_match:   {file_path_glob?: "**/*.py", command_prefix?: "npm "}
      decision:     "approve" | "deny"
      reason:       short string surfaced to the user when applied
      enabled:      bool (lets users disable without deletion)
    }

Matching is conservative:
  - Tool name must match `tool_pattern` (regex).
  - Every key in `args_match` must match the corresponding tool input.
  - The first applicable policy wins (sorted by priority then created_at).

This is the v0.7 baseline. Future passes:
  - CEL/jq expressions for richer arg matching
  - Time-window policies ("auto-approve npm only before 6pm")
  - Trust-decay (auto-approval expires after N days unless re-confirmed)
"""

from __future__ import annotations

import fnmatch
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import TYPE_CHECKING, Any

from memex.core.schema import Concept, NodeKind, Source as SourceActor

if TYPE_CHECKING:
    from memex.core.engine import Engine

log = logging.getLogger(__name__)


# ---- Hard-deny patterns ------------------------------------------------------
# These are NEVER auto-approved, even in AFK mode. Each entry is a
# (tool_pattern_regex, args_match_dict, reason) triple. The user can
# extend the list at runtime by storing `kind=constraint,
# metadata.policy_type="hard_deny"` policies — `_collect_hard_deny`
# merges those with this baseline.
_HARD_DENY_BASELINE: tuple[tuple[str, dict[str, Any], str], ...] = (
    # Catastrophic filesystem wipes. Path char negative-lookahead so
    # `rm -rf /home/user/cache/foo` doesn't trigger but `rm -rf /` does.
    ("Bash", {"command": {"regex": r"\brm\s+-r[a-z]*\s+(/|~|\$HOME)(?![A-Za-z0-9._/-])"}},
     "rm -rf on root/home — catastrophic, never auto-approved"),
    ("Bash", {"command": {"regex": r"\brm\s+-r[a-z]*\s+\.(?![A-Za-z0-9._/-])"}},
     "rm -rf . wipes the working tree — never auto-approved"),
    # Force-push to protected branches. ONLY matches when an explicit
    # force flag is present — routine fast-forward `git push origin main`
    # is permitted (catching that as well was a v0.6 over-correction).
    # If you want stricter "no direct push to main", set up a per-repo
    # action_constraint that targets your branch protection scheme.
    ("Bash", {"command": {"regex": r"git\s+push\s+.*?(--force\b|--force-with-lease\b|(?:^|\s)-f(?:\s|$))"
                                    r".*\b(main|master|production|prod|release)\b"}},
     "force-push to protected branch — never auto-approved"),
    # Database destruction.
    ("Bash", {"command": {"regex": r"\bDROP\s+(DATABASE|TABLE|SCHEMA)\b",
                          "regex_flags": "i"}},
     "DROP DATABASE/TABLE — never auto-approved"),
    # Kubernetes destructive ops on production.
    ("Bash", {"command": {"regex": r"kubectl\s+delete\s+.+--namespace[= ]?(prod|production)"}},
     "kubectl delete on production namespace — never auto-approved"),
    # Skip git hooks.
    ("Bash", {"command": {"regex": r"git\s+(commit|push|merge).*--no-verify"}},
     "skipping git hooks — never auto-approved (per CLAUDE.md)"),
)


_AFK_FLAG_NAME = "memex.afk_mode"


class ApprovalDecision(str, Enum):
    """Three-valued outcome of `should_approve`."""
    approve = "approve"   # auto-approve; do not prompt user
    deny = "deny"         # auto-deny; do not prompt user
    ask = "ask"           # no policy applied; default behavior (prompt)


@dataclass(slots=True)
class ApprovalPolicy:
    """One stored policy, materialized from a Concept."""
    id: str
    tool_pattern: str
    args_match: dict[str, Any]
    decision: ApprovalDecision
    reason: str
    priority: int = 100   # lower runs first
    enabled: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_concept(cls, c: Concept) -> ApprovalPolicy | None:
        md = c.metadata or {}
        if md.get("policy_type") != "approval":
            return None
        try:
            decision = ApprovalDecision(md.get("decision", "approve"))
        except ValueError:
            log.warning("policy %s has invalid decision %r — skipping", c.id,
                        md.get("decision"))
            return None
        return cls(
            id=c.id,
            tool_pattern=md.get("tool_pattern", "*"),
            args_match=md.get("args_match", {}),
            decision=decision,
            reason=md.get("reason", c.name),
            priority=int(md.get("priority", 100)),
            enabled=bool(md.get("enabled", True)),
            metadata=md,
        )


@dataclass(slots=True)
class PolicyMatch:
    """A policy that applied to a particular request."""
    decision: ApprovalDecision
    policy_id: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision.value,
            "policy_id": self.policy_id,
            "reason": self.reason,
        }


# ---- Public API --------------------------------------------------------------


def add_policy(
    engine: "Engine",
    *,
    tool_pattern: str,
    decision: ApprovalDecision | str = ApprovalDecision.approve,
    args_match: dict[str, Any] | None = None,
    reason: str = "",
    priority: int = 100,
    enabled: bool = True,
) -> Concept:
    """Persist a new approval policy."""
    decision_val = (
        decision.value if isinstance(decision, ApprovalDecision)
        else ApprovalDecision(decision).value
    )
    name = (
        f"{decision_val}: {tool_pattern}"
        + (f" {args_match}" if args_match else "")
    )
    description = reason or f"auto-{decision_val} {tool_pattern} requests"
    metadata = {
        "policy_type": "approval",
        "tool_pattern": tool_pattern,
        "args_match": args_match or {},
        "decision": decision_val,
        "reason": reason or description,
        "priority": priority,
        "enabled": enabled,
    }
    return engine.add(
        name=name,
        description=description,
        kind=NodeKind.constraint,
        source=SourceActor.human,
        metadata=metadata,
    )


def list_policies(engine: "Engine") -> list[ApprovalPolicy]:
    """Every active approval policy, sorted by priority + creation time."""
    out: list[ApprovalPolicy] = []
    for c in engine.find_by_kind(NodeKind.constraint):
        p = ApprovalPolicy.from_concept(c)
        if p is None or not p.enabled:
            continue
        out.append(p)
    out.sort(key=lambda p: (p.priority, p.id))
    return out


def remove_policy(engine: "Engine", policy_id: str) -> bool:
    """Hard-delete a policy. Returns True when the policy existed."""
    c = engine.get(policy_id)
    if c is None or c.metadata.get("policy_type") != "approval":
        return False
    return engine.delete(policy_id)


def should_approve(
    engine: "Engine",
    *,
    tool_name: str,
    tool_input: dict[str, Any] | None = None,
    context: dict[str, Any] | None = None,
) -> PolicyMatch:
    """Consult stored approval policies. Returns one of approve/deny/ask.

    Resolution order:
      1. Hard-deny patterns (built-in + user-stored kind=hard_deny). These
         catastrophic-action checks ALWAYS run first and bypass AFK mode.
      2. AFK mode active → approve (unless caught by hard-deny above).
      3. User-stored approval policies (`policy_type="approval"`).
      4. Default → ask (no auto-action; user prompt fires).

    Side-effect: when a policy or AFK fires, observes an `auto_approval`
    / `auto_deny` event so the user can audit later.
    """
    tool_input = tool_input or {}

    # 1. Hard-deny — always runs first.
    hard = _check_hard_deny(engine, tool_name, tool_input)
    if hard is not None:
        _record_decision(engine, hard, tool_name, tool_input, afk=False)
        return hard

    # 2. AFK mode — approve everything else.
    afk = _afk_state(engine)
    if afk is not None:
        match = PolicyMatch(
            decision=ApprovalDecision.approve,
            policy_id=afk.id,
            reason=(
                f"AFK mode active (note: {afk.metadata.get('note', '')!r}); "
                f"expires {afk.metadata.get('expires_at', 'never')}"
            ),
        )
        _record_decision(engine, match, tool_name, tool_input, afk=True)
        return match

    # 3. Walk user-stored approval policies.
    for policy in list_policies(engine):
        if _policy_applies(policy, tool_name, tool_input):
            match = PolicyMatch(
                decision=policy.decision,
                policy_id=policy.id,
                reason=policy.reason,
            )
            _record_decision(engine, match, tool_name, tool_input, afk=False)
            return match

    # 4. Default — fall through to user prompt.
    return PolicyMatch(
        decision=ApprovalDecision.ask,
        policy_id="",
        reason="no applicable policy",
    )


def _record_decision(
    engine: "Engine",
    match: PolicyMatch,
    tool_name: str,
    tool_input: dict[str, Any],
    *,
    afk: bool,
) -> None:
    event_kind = (
        "auto_approval" if match.decision == ApprovalDecision.approve
        else "auto_deny"
    )
    try:
        engine.observe(
            kind=event_kind,
            actor=SourceActor.agent,
            payload={
                "policy_id": match.policy_id,
                "tool_name": tool_name,
                "tool_input_keys": sorted((tool_input or {}).keys()),
                "reason": match.reason,
                "afk": afk,
            },
        )
    except Exception as e:  # noqa: BLE001
        log.warning("should_approve: audit observe failed: %s", e)


# ---- AFK mode ----------------------------------------------------------------


def enable_afk_mode(
    engine: "Engine",
    *,
    duration_hours: float = 4.0,
    note: str = "",
) -> Concept:
    """Flip memex into AFK mode for `duration_hours`. Auto-approves every
    tool call (except hard-deny) and audit-logs everything.

    Idempotent — re-calling replaces any active flag with the new
    settings (so the user can extend the window or update the note).
    """
    # Drop any existing AFK flag first so there's only ever one.
    for c in engine.find_by_kind(NodeKind.fact):
        if c.metadata.get("signal") == "afk_mode":
            engine.delete(c.id)
    started = datetime.now(timezone.utc)
    expires = started + timedelta(hours=duration_hours)
    flag = engine.add(
        name=_AFK_FLAG_NAME,
        description=(
            f"AFK mode active until {expires.isoformat()}. "
            f"Auto-approves every tool call except hard-deny patterns. "
            f"Note: {note}"
        ),
        kind=NodeKind.fact,
        source=SourceActor.human,
        metadata={
            "signal": "afk_mode",
            "started_at": started.isoformat(),
            "expires_at": expires.isoformat(),
            "duration_hours": duration_hours,
            "note": note,
        },
    )
    engine.observe(
        kind="afk_enabled",
        actor=SourceActor.human,
        payload={
            "duration_hours": duration_hours,
            "note": note,
            "expires_at": expires.isoformat(),
        },
    )
    return flag


def disable_afk_mode(engine: "Engine") -> bool:
    """Turn off AFK mode. Returns True when something was actually
    disabled (was active)."""
    removed = False
    for c in engine.find_by_kind(NodeKind.fact):
        if c.metadata.get("signal") == "afk_mode":
            engine.delete(c.id)
            removed = True
    if removed:
        engine.observe(kind="afk_disabled", actor=SourceActor.human, payload={})
    return removed


def afk_status(engine: "Engine") -> dict[str, Any] | None:
    """Return the active AFK flag's state, or None when inactive.
    Auto-cleans expired flags."""
    flag = _afk_state(engine)
    if flag is None:
        return None
    return {
        "id": flag.id,
        "started_at": flag.metadata.get("started_at"),
        "expires_at": flag.metadata.get("expires_at"),
        "note": flag.metadata.get("note"),
        "duration_hours": flag.metadata.get("duration_hours"),
    }


def _afk_state(engine: "Engine") -> Concept | None:
    """Return the active, non-expired AFK flag (deletes expired ones)."""
    now = datetime.now(timezone.utc)
    active: Concept | None = None
    for c in engine.find_by_kind(NodeKind.fact):
        if c.metadata.get("signal") != "afk_mode":
            continue
        expires_str = c.metadata.get("expires_at")
        if expires_str:
            try:
                expires = datetime.fromisoformat(expires_str)
            except ValueError:
                expires = now + timedelta(hours=1)
            if expires <= now:
                engine.delete(c.id)
                engine.observe(
                    kind="afk_expired", actor=SourceActor.agent,
                    payload={"flag_id": c.id, "expired_at": expires.isoformat()},
                )
                continue
        active = c  # last write wins
    return active


# ---- Hard-deny ---------------------------------------------------------------


def _check_hard_deny(
    engine: "Engine",
    tool_name: str,
    tool_input: dict[str, Any],
) -> PolicyMatch | None:
    """Walk the baseline + user-extended hard-deny list."""
    for tool_pat, args_match, reason in _HARD_DENY_BASELINE:
        if _matches_hard(tool_pat, args_match, tool_name, tool_input):
            return PolicyMatch(
                decision=ApprovalDecision.deny,
                policy_id="builtin:hard_deny",
                reason=reason,
            )
    # User-extended hard-deny policies (concepts with policy_type=hard_deny).
    for c in engine.find_by_kind(NodeKind.constraint):
        md = c.metadata or {}
        if md.get("policy_type") != "hard_deny" or not md.get("enabled", True):
            continue
        tool_pat = md.get("tool_pattern", "*")
        args_match = md.get("args_match", {})
        if _matches_hard(tool_pat, args_match, tool_name, tool_input):
            return PolicyMatch(
                decision=ApprovalDecision.deny,
                policy_id=c.id,
                reason=md.get("reason", "user-defined hard deny"),
            )
    return None


def _matches_hard(
    tool_pattern: str,
    args_match: dict[str, Any],
    tool_name: str,
    tool_input: dict[str, Any],
) -> bool:
    try:
        if not re.fullmatch(tool_pattern, tool_name):
            return False
    except re.error:
        return False
    for key, criterion in (args_match or {}).items():
        actual = tool_input.get(key)
        if not _arg_matches(criterion, actual):
            return False
    return True


# ---- Internals ---------------------------------------------------------------


def _policy_applies(
    policy: ApprovalPolicy,
    tool_name: str,
    tool_input: dict[str, Any],
) -> bool:
    """All criteria must hold: tool_pattern regex match + every args_match key."""
    if not policy.enabled:
        return False
    try:
        if not re.fullmatch(policy.tool_pattern, tool_name):
            return False
    except re.error:
        log.warning("policy %s has invalid regex %r — skipping",
                    policy.id, policy.tool_pattern)
        return False

    for key, criterion in (policy.args_match or {}).items():
        actual = tool_input.get(key)
        if not _arg_matches(criterion, actual):
            return False
    return True


def _arg_matches(criterion: Any, actual: Any) -> bool:
    """Match a single args_match key.

    - String criterion → glob (`**/*.py`) when actual is a string,
      else equality.
    - Dict criterion supports `{"prefix": "npm "}` / `{"regex": "..."}`
      / `{"glob": "**/*.test.*"}` for richer matching.
    - List criterion → any-match.
    - Anything else → equality.
    """
    if criterion is None:
        return True
    if isinstance(criterion, str):
        if isinstance(actual, str):
            if any(ch in criterion for ch in "*?["):
                return fnmatch.fnmatch(actual, criterion)
            return actual == criterion
        return False
    if isinstance(criterion, dict):
        if "prefix" in criterion and isinstance(actual, str):
            if not actual.startswith(criterion["prefix"]):
                return False
        if "regex" in criterion and isinstance(actual, str):
            flags = 0
            flag_str = (criterion.get("regex_flags") or "").lower()
            if "i" in flag_str:
                flags |= re.IGNORECASE
            if "s" in flag_str:
                flags |= re.DOTALL
            try:
                if not re.search(criterion["regex"], actual, flags):
                    return False
            except re.error:
                return False
        if "glob" in criterion and isinstance(actual, str):
            if not fnmatch.fnmatch(actual, criterion["glob"]):
                return False
        if "equals" in criterion:
            if actual != criterion["equals"]:
                return False
        return True
    if isinstance(criterion, (list, tuple)):
        return any(_arg_matches(c, actual) for c in criterion)
    return criterion == actual
