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
    # An *action-gating* constraint: only this kind is consulted by
    # check_action / OMP §4.5 validate. Use for rules that must block or
    # step-up tool calls. metadata fields honored by the gate:
    #   applies_to: list[str]   — tool names this rule targets (empty = any)
    #   match_pattern: str|null — regex over intent string; null = semantic only
    #   verdict: "deny"|"step_up" (default "deny")
    # Plain `constraint` nodes are knowledge / preferences — NOT gating rules —
    # and stay out of the gate's recall. This split is what prevents the
    # historical mis-fires where a prose principle like "memex is small,
    # run-anywhere; no bundled LLM" was treated as an action-deny rule.
    action_constraint = "action_constraint"
    module = "module"
    endpoint = "endpoint"
    person = "person"
    fact = "fact"
    opinion = "opinion"
    question = "question"
    rejected = "rejected"
    approach = "approach"  # validation entry: carries `checks` list in metadata
    # Ephemeral working-memory: TTL'd nodes that live between session and
    # durable. metadata.expires_at (ISO 8601) is honored by the lifecycle
    # cleanup pass. Use for "scratch context" that should not pollute
    # long-term recall but is useful within a session or work-week.
    ephemeral = "ephemeral"
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
    # Relation — synthetic node that wraps a high-signal edge (supersedes,
    # depends_on, motivated_by, blocks, same_as, conflicts_with) so the
    # *relationship itself* is embeddable. Recall queries like "what
    # supersedes the old auth design" hit these directly. Carries
    # `metadata.{from_id, to_id, edge_kind}` pointing back at the edge.
    relation = "relation"


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
    # Change provenance — connect work (tasks, prompts, decisions) to the
    # code it touched. Surfaces "last edited by task X" on symbols and
    # "touched these 5 files" on tasks. Populated by PostToolUse hook
    # after each Edit/Write call.
    touches = "touches"            # task / change → file or symbol
    landed_in = "landed_in"        # change → commit concept


class Source(str, Enum):
    human = "human"
    claude_code = "claude_code"
    cursor = "cursor"
    windsurf = "windsurf"
    cline = "cline"
    agent = "agent"
    skill = "skill"
    extractor = "extractor"
    # OMP §5 — `system` indicates an automated provenance with no
    # particular agent identity (cron, migration, internal lifecycle).
    # Use this for events emitted by the daemon's own background loops
    # (consolidation, cleanup, episodic rollup).
    system = "system"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _new_id(prefix: str = "c", kind: str | None = None) -> str:
    """Generate a branded memex id: `mx_<7 lowercase hex>` = 10 chars total.

    Examples: mx_a3c9f4b, mx_1bc38d7, mx_72a6aff, mx_5d72072.

    Space: 16^7 ≈ 268M. Birthday-50% collision at ~16K ids; SemanticStore's
    `add_concept` performs an insert-retry on conflict so the effective
    space stays unbounded. For team-scale stores (10K-100K concepts) the
    collision rate is observable but harmless.

    Legacy `prefix` / `kind` args are accepted for backward compat and
    ignored — the kind already lives in the concept's `kind` field; the
    branded `mx_` prefix marks every memex-issued id at a glance.
    """
    _ = (prefix, kind)
    return f"mx_{uuid.uuid4().hex[:7]}"


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
