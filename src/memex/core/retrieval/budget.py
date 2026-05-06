"""
Token-budget enforcement for retrieval results.

Memex retrievals are bounded by construction (axiom 9: retrieval cost > storage cost).
There is no "give me everything about X" — the budget forces good engineering instead
of leaving it to prompt discipline.

Token estimation uses ~4 chars per token, which is roughly right for English text and
all major BPE tokenizers; a precise count isn't needed because callers reserve headroom.
"""

from __future__ import annotations

from memex.core.schema import Concept, Edge, RecallResult


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def concept_tokens(c: Concept) -> int:
    return estimate_tokens(c.name) + estimate_tokens(c.description) + 16


def edge_tokens(e: Edge) -> int:
    return 16


def enforce_budget(
    nodes: list[Concept],
    edges: list[Edge],
    budget_tokens: int,
    strategy: str = "hybrid",
) -> RecallResult:
    """Trim nodes (and downstream edges) until total estimated tokens ≤ budget."""
    used = 0
    keep_nodes: list[Concept] = []
    for node in nodes:
        cost = concept_tokens(node)
        if used + cost > budget_tokens and keep_nodes:
            break
        keep_nodes.append(node)
        used += cost

    keep_ids = {n.id for n in keep_nodes}
    keep_edges: list[Edge] = []
    for edge in edges:
        if edge.from_id in keep_ids and edge.to_id in keep_ids:
            cost = edge_tokens(edge)
            if used + cost > budget_tokens:
                break
            keep_edges.append(edge)
            used += cost

    return RecallResult(
        nodes=keep_nodes,
        edges=keep_edges,
        tokens_used=used,
        strategy=strategy,
    )
