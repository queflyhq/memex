"""Starter pack of action_constraints — the rules every memex user should
have installed before letting an AI touch their tools.

End users will never write `git\s+push\s+.*--force\b` themselves. Without
a default set, the Rules page is empty and `validate` is a no-op. This
module ships a curated baseline that catches the most common ways an AI
can wreck a developer's day.

Categories:
  - VCS    : history rewrites, force pushes, destructive resets
  - Files  : unbounded deletion, system-path writes
  - Secrets: writing keys/tokens into files
  - DB    : DROP TABLE, TRUNCATE without WHERE
  - Cloud : `kubectl delete` on prod, force-replace resources
  - Network: rm -rf on root paths

Each rule sets:
  - verdict: "deny" (hard-block) | "step_up" (ask the human)
  - match_pattern: regex over the AI's tool intent string
  - applies_to: ["Bash"] usually — most regex rules only meaningful on shell
  - description: human-readable reason

Install via `memex install rules:safety-baseline` (CLI) or the UI button
on the Work page. Idempotent — re-running won't duplicate.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from memex.core.schema import Concept, NodeKind, Source

if TYPE_CHECKING:
    from memex.core.engine import Engine

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class BaselineRule:
    name: str
    description: str
    match_pattern: str
    verdict: str           # "deny" or "step_up"
    applies_to: list[str]  # tool names; ["Bash"] for shell rules


BASELINE_RULES: list[BaselineRule] = [
    # ----- VCS: history rewrites ------------------------------------------
    BaselineRule(
        name="No force-push to protected branches",
        description="Force-push rewrites history. On main/master/release branches "
                    "this loses teammate work and breaks CI on other clones.",
        match_pattern=r"\bgit\s+push\s+.*(?:--force|--force-with-lease|-f)\b.*\b(main|master|release|prod|production)\b",
        verdict="deny",
        applies_to=["Bash"],
    ),
    BaselineRule(
        name="No history-rewriting rebase on shared branches",
        description="`git rebase` or `git filter-branch` on a pushed branch invalidates "
                    "every collaborator's local copy.",
        match_pattern=r"\bgit\s+(?:rebase\s+-i|filter-branch|filter-repo)\b",
        verdict="step_up",
        applies_to=["Bash"],
    ),
    BaselineRule(
        name="No `git reset --hard` on uncommitted work",
        description="--hard is unrecoverable. Even with reflog, untracked changes are lost.",
        match_pattern=r"\bgit\s+reset\s+--hard\b",
        verdict="step_up",
        applies_to=["Bash"],
    ),
    BaselineRule(
        name="No `git push --no-verify`",
        description="--no-verify skips pre-commit + pre-push hooks. If a hook is failing, "
                    "fix the underlying issue rather than bypassing.",
        match_pattern=r"\bgit\s+(?:commit|push)\s+.*--no-verify\b",
        verdict="step_up",
        applies_to=["Bash"],
    ),

    # ----- Filesystem: unbounded deletion ---------------------------------
    BaselineRule(
        name="No rm -rf on root or home",
        description="`rm -rf /` or `rm -rf ~` will destroy the filesystem. Hard block.",
        match_pattern=r"\brm\s+-rf?\s+(?:/|~|\$HOME|\$USERPROFILE)(?:\s|$|\\)",
        verdict="deny",
        applies_to=["Bash"],
    ),
    BaselineRule(
        name="No rm on .git/.env/.aws/.ssh",
        description="Deleting .git destroys version history. Deleting .env or .aws or .ssh "
                    "destroys credentials and config.",
        match_pattern=r"\brm\s+(?:-[a-z]+\s+)?.*\.(git|env|aws|ssh|kube|gnupg)(/|\s|$)",
        verdict="deny",
        applies_to=["Bash"],
    ),
    BaselineRule(
        name="No mass file deletion via find -delete",
        description="`find ... -delete` is easy to mistype and runs irreversibly. "
                    "Prefer `find ... -print` first to verify what would be deleted.",
        match_pattern=r"\bfind\s+\S+.*-delete\b",
        verdict="step_up",
        applies_to=["Bash"],
    ),

    # ----- Secrets --------------------------------------------------------
    BaselineRule(
        name="No writing API keys into files",
        description="Cleartext API keys / PATs / OAuth tokens in committed files "
                    "is a credential exposure. Use the OS keychain via `memex secret put`.",
        match_pattern=r"(sk-[A-Za-z0-9]{32,}|ghp_[A-Za-z0-9]{30,}|xoxb-[A-Za-z0-9-]{20,}|AKIA[A-Z0-9]{16})",
        verdict="deny",
        applies_to=["Write", "Edit", "MultiEdit"],
    ),

    # ----- Database: destructive operations --------------------------------
    BaselineRule(
        name="No DROP TABLE / DROP DATABASE / TRUNCATE without WHERE",
        description="These commands have no WHERE clause and erase entire tables. "
                    "Always require explicit human confirmation.",
        match_pattern=r"\b(DROP\s+(TABLE|DATABASE|SCHEMA)|TRUNCATE\s+TABLE)\b",
        verdict="step_up",
        applies_to=["Bash"],
    ),
    BaselineRule(
        name="No DELETE/UPDATE SQL without a WHERE clause",
        description="An unbounded DELETE or UPDATE rewrites the whole table.",
        match_pattern=r"\b(DELETE\s+FROM|UPDATE)\s+\w+\s*(?!.*\bWHERE\b)",
        verdict="step_up",
        applies_to=["Bash"],
    ),

    # ----- Cloud / Kubernetes --------------------------------------------
    BaselineRule(
        name="No `kubectl delete` against prod context",
        description="Deleting resources in a production cluster context is rarely the "
                    "right move from an AI. Step up so a human confirms the kubectl context.",
        match_pattern=r"\bkubectl\s+delete\b.*(--context\s+(prod|production)|--all\b)",
        verdict="deny",
        applies_to=["Bash"],
    ),
    BaselineRule(
        name="No `terraform apply -auto-approve` on prod",
        description="Auto-approve in production skips human review of the plan. "
                    "Step up so the diff gets read.",
        match_pattern=r"\bterraform\s+(?:apply|destroy)\s+.*-auto-approve\b",
        verdict="step_up",
        applies_to=["Bash"],
    ),
    BaselineRule(
        name="No `aws s3 rm --recursive` on production buckets",
        description="Recursive S3 deletion is irreversible and quota-blind. Confirm bucket name.",
        match_pattern=r"\baws\s+s3\s+rm\s+s3://[\w.-]+/?\s+--recursive\b",
        verdict="step_up",
        applies_to=["Bash"],
    ),

    # ----- Package / dependency --------------------------------------------
    BaselineRule(
        name="No `npm publish` without explicit confirmation",
        description="Publishing to npm is irreversible (you can't truly unpublish "
                    "after 72h). Confirm the version + package name.",
        match_pattern=r"\bnpm\s+publish\b",
        verdict="step_up",
        applies_to=["Bash"],
    ),
    BaselineRule(
        name="No `pip install` with arbitrary URLs",
        description="Installing from `pip install https://...` or git+ssh URLs bypasses "
                    "the PyPI trust model. Step up so the source is reviewed.",
        match_pattern=r"\bpip\s+install\s+(https?|git\+)",
        verdict="step_up",
        applies_to=["Bash"],
    ),
]


def install_baseline_rules(
    engine: "Engine",
    *,
    overwrite: bool = False,
) -> dict[str, int]:
    """Install the baseline rules into the graph. Idempotent — rules with
    matching names are skipped unless overwrite=True.

    Returns counts: {added, skipped, total}.
    """
    added = 0
    skipped = 0
    for r in BASELINE_RULES:
        existing = engine.semantic.find_by_name_kind_source(
            r.name, NodeKind.action_constraint, Source.human,
        )
        if existing is not None and not overwrite:
            skipped += 1
            continue
        if existing is not None and overwrite:
            try:
                engine.semantic.delete_concept(existing.id)
            except Exception:  # noqa: BLE001
                pass
        c = Concept(
            name=r.name,
            kind=NodeKind.action_constraint,
            description=r.description,
            source=Source.human,
            confidence=0.95,
            metadata={
                "verdict": r.verdict,
                "match_pattern": r.match_pattern,
                "applies_to": r.applies_to,
                "scope": "all",
                "baseline": True,  # marks as part of the starter pack
            },
        )
        try:
            engine.semantic.add_concept(c)
            added += 1
        except Exception as e:  # noqa: BLE001
            log.warning("baseline rule install failed for %s: %s", r.name, e)
    return {"added": added, "skipped": skipped, "total": len(BASELINE_RULES)}
