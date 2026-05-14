"""
Token-budget enforcement for retrieval results.

Memex retrievals are bounded by construction (axiom 9: retrieval cost > storage cost).
There is no "give me everything about X" — the budget forces good engineering instead
of leaving it to prompt discipline.

Token estimation uses tiktoken's cl100k_base encoding when available (matches
GPT-4/Claude tokenization), falling back to a max(words×1.3, chars/4) heuristic
otherwise. The naive chars/4 estimate undercounts code (more punctuation per
token) and CJK (1 char ≈ 1+ tokens); the heuristic compensates without pulling
in a hard dependency.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from memex.core.schema import Concept, Edge, RecallResult

log = logging.getLogger(__name__)


# Lazy singleton — tiktoken loads its encoding on first use (~50ms once).
_TIKTOKEN_ENC: Any | None = None
_TIKTOKEN_TRIED = False
_TIKTOKEN_LOCK = threading.Lock()


def _get_tiktoken_encoder() -> Any | None:
    """Return a cl100k_base tiktoken encoder, or None if tiktoken isn't
    installed or fails to load. Cached after first call."""
    global _TIKTOKEN_ENC, _TIKTOKEN_TRIED
    if _TIKTOKEN_TRIED:
        return _TIKTOKEN_ENC
    with _TIKTOKEN_LOCK:
        if _TIKTOKEN_TRIED:
            return _TIKTOKEN_ENC
        try:
            import tiktoken
            _TIKTOKEN_ENC = tiktoken.get_encoding("cl100k_base")
        except Exception as e:  # noqa: BLE001
            log.debug("tiktoken not usable, using heuristic: %s", e)
            _TIKTOKEN_ENC = None
        _TIKTOKEN_TRIED = True
        return _TIKTOKEN_ENC


def estimate_tokens(text: str) -> int:
    """Return an estimate of how many BPE tokens `text` will encode to.

    Prefers tiktoken (precise); falls back to max(words×1.3, chars/4) which
    out-performs naive chars/4 on code and short strings.
    """
    if not text:
        return 1
    enc = _get_tiktoken_encoder()
    if enc is not None:
        try:
            return max(1, len(enc.encode(text)))
        except Exception as e:  # noqa: BLE001
            log.debug("tiktoken encode failed for len=%d: %s", len(text), e)
    # Heuristic fallback: words×1.3 captures prose; chars/4 captures
    # punctuation-heavy code; max() picks the realistic upper bound.
    words = max(1, len(text.split()))
    chars = max(1, len(text))
    return max(1, max(int(words * 1.3), chars // 4))


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
