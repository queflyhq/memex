"""
Core data model for memex.

Every node and every edge carries provenance + confidence + timestamps.
This is forced by the multi-agent multi-session reality — without it,
the graph rots when more than one agent writes to the same store.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator


class NodeKind(str, Enum):
    pattern = "pattern"
    decision = "decision"
    constraint = "constraint"
    module = "module"
    endpoint = "endpoint"
    person = "person"
    fact = "fact"
    opinion = "opinion"
    question = "question"
    rejected = "rejected"
    approach = "approach"  # validation entry: carries `checks` list in metadata
    # Project-management primitives — workflow state lives in `metadata.status`
    # (pending|in_progress|completed|blocked|cancelled) so we don't need new
    # schema columns. `confidence` doubles as completion certainty (1.0 = done).
    task = "task"
    milestone = "milestone"
    project = "project"
    # Codebase-memory primitives — typed nodes (NOT opaque text chunks).
    # `metadata` carries language, signature, line range, parent symbol, etc.
    source = "source"  # a registered codebase / repo
    file = "file"      # a source file inside a registered source
    symbol = "symbol"  # a typed AST declaration: class|function|method|...
    # Note — comment / instruction attached to a task (or any concept).
    # `metadata.comment_on` carries the parent concept id; the node body
    # is the comment text. Queryable via recall so an AI working on the
    # task sees comments as LLM-readable instructions.
    note = "note"


class EdgeKind(str, Enum):
    implements = "implements"
    supersedes = "supersedes"
    conflicts_with = "conflicts_with"
    depends_on = "depends_on"
    motivated_by = "motivated_by"
    rejected_due_to = "rejected_due_to"
    same_as = "same_as"
    relates_to = "relates_to"
    calls = "calls"
    # PM edges — wire tasks to projects/milestones and to each other.
    blocks = "blocks"              # task A blocks task B
    part_of = "part_of"            # task is part of milestone is part of project
    spawned_from = "spawned_from"  # task created from an episodic event
    # Codebase-memory edges — explicit AST relations, not embedding similarity.
    defined_in = "defined_in"      # symbol → file (its definition site)
    imports = "imports"            # file → file (or file → external module)
    extends = "extends"            # symbol → symbol (class inheritance)


class Source(str, Enum):
    human = "human"
    claude_code = "claude_code"
    cursor = "cursor"
    windsurf = "windsurf"
    cline = "cline"
    agent = "agent"
    skill = "skill"
    extractor = "extractor"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class Concept(BaseModel):
    """A node in the semantic graph.

    `verification` carries the falsifiability primitive: a string predicate
    (Cypher, regex, HTTP probe URL, etc.) that can re-check the claim against
    ground truth. Nodes without verification are downgraded to `kind=opinion`
    by the lifecycle pass.
    """

    id: str = Field(default_factory=lambda: _new_id("c"))
    name: str
    description: str = ""
    kind: NodeKind = NodeKind.fact
    source: Source = Source.human
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    created_at: datetime = Field(default_factory=_now)
    last_confirmed_at: datetime = Field(default_factory=_now)
    metadata: dict[str, Any] = Field(default_factory=dict)
    verification: str | None = None

    @field_validator("name")
    @classmethod
    def _name_nonempty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("name must be non-empty")
        return v.strip()


class Edge(BaseModel):
    """A typed, provenance-stamped relation between two concepts."""

    from_id: str
    to_id: str
    kind: EdgeKind
    source: Source = Source.human
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    created_at: datetime = Field(default_factory=_now)
    last_confirmed_at: datetime = Field(default_factory=_now)
    metadata: dict[str, Any] = Field(default_factory=dict)


class EpisodicEvent(BaseModel):
    """Time-indexed event in the episodic store.

    Episodic memory is *what happened*. Periodic consolidation (v0.4) promotes
    high-frequency episodic events into semantic facts.
    """

    id: str = Field(default_factory=lambda: _new_id("e"))
    timestamp: datetime = Field(default_factory=_now)
    kind: str
    actor: Source = Source.human
    payload: dict[str, Any] = Field(default_factory=dict)


class RecallResult(BaseModel):
    """Subgraph returned by a `recall` call, bounded by token budget.

    `degraded` is True when retrieval ran without semantic vectors (e.g.
    embed tier missing or vector store empty). Agents should treat
    degraded results as lexical-only and consider re-asking with
    different phrasing rather than trusting completeness. `degraded_reason`
    carries a short human-readable explanation when degraded.
    """

    nodes: list[Concept]
    edges: list[Edge]
    tokens_used: int = 0
    strategy: str = "bm25"
    degraded: bool = False
    degraded_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "nodes": [n.model_dump(mode="json") for n in self.nodes],
            "edges": [e.model_dump(mode="json") for e in self.edges],
            "tokens_used": self.tokens_used,
            "strategy": self.strategy,
            "degraded": self.degraded,
            "degraded_reason": self.degraded_reason,
        }
