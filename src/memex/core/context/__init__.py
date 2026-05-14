"""
Context layer — live state.

The hot working set: which concepts are touched-recently, what the
agent is focused on. Best-effort and cheap to recompute; cognition
can run without it, just less precisely.

Layer in the 5-layer OMP architecture:
  perception → CONTEXT → cognition → memory ← action
"""

from __future__ import annotations

from memex.core.working_set import WorkingSet

__all__ = ["WorkingSet"]
