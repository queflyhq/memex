"""Seed memex from a repository.

Solves the cold-start problem: an empty graph is useless. The seed
pipeline scans a repo and populates the graph with:

  - One `kind=project` parent concept for the repo
  - Decisions extracted from git history (conventional commits, ADRs)
  - Facts from README / CONTRIBUTING / ARCHITECTURE / DESIGN docs
  - Tech-stack facts from manifest files (pyproject.toml, package.json, ...)
  - Code symbols via the existing `ingest-repos` flow

Day 1 a user runs `memex seed` and gets ~50-200 real concepts about their
project instead of an empty graph that takes weeks to fill.

Heuristic only for v0 — no LLM required. LLM enrichment can layer on top
later for better summaries.
"""

from memex.seed.from_repo import seed_from_repo, SeedReport

__all__ = ["seed_from_repo", "SeedReport"]
