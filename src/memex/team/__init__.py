"""Team mode — git-sync of the memex graph through a shared repo.

The pitch: 5 devs on the same product all run their own local memex
daemon. They configure a shared "team memex" git repo. Memex pushes
its concept + edge graph (as JSON files, one per concept) to the repo
on a cadence. Memex pulls from the repo and merges new/updated entries
into its local graph. Conflicts: ours-latest unless `metadata.team_locked`.

Why git (not federation): every team already has a git workflow, knows
how to grant access, can review diffs, can roll back. OMP §10 federation
is the eventual goal; git-sync is what works today.

What gets synced:
  - `kind in {decision, fact, constraint, action_constraint, project,
              milestone, person, pattern, approach}` — the durable
    knowledge layer. Code symbols + files + episodic events stay local
    (they're machine-derived and high-volume).

What does NOT sync:
  - episodic events (per-user activity stream)
  - vectors (re-computed locally)
  - secrets (OS keychain only)
  - any concept with `metadata.private = true`

File layout in the team repo:
    memex-graph/
      concepts/
        c_<id>.json     # one file per concept (idempotent merge)
      edges/
        e_<id>.json     # one file per edge
      manifest.json     # sync timestamp + counts + memex version
      README.md         # auto-generated docs for non-memex viewers

Idempotent. Re-running on the same state is a no-op (git diff is empty).
"""

from memex.team.git_sync import (
    sync_push,
    sync_pull,
    init_team_repo,
    TeamSyncReport,
)

__all__ = ["sync_push", "sync_pull", "init_team_repo", "TeamSyncReport"]
