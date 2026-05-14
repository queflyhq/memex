"""
Query-side preprocessing — expansion and HyDE.

Two LLM-driven techniques applied *before* embedding to lift retrieval
precision:

* **Query expansion** — ask the LLM for 2–3 alternate phrasings of the
  user's query. The alternates are concatenated into a richer query
  string before embedding. Helps when the user's wording diverges from
  how stored concepts were phrased.
* **HyDE (Hypothetical Document Embeddings)** — ask the LLM to draft a
  short hypothetical answer, then embed *that* instead of the raw
  query. User queries and stored documents live in different
  distributional sub-spaces; HyDE moves the search vector into the
  document sub-space, often beating raw query embedding by 10+ pts on
  retrieval benchmarks.

Both are opt-in (off by default) and gated on LLM availability. Failure
is silent — the original query is returned untouched, recall continues
on the unmodified path. The latency budget for LLM calls on the recall
hot path is enforced by the caller via timeouts.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)


# Prompts kept compact — recall is latency-sensitive; we don't want long
# generations. Both prompts return free-form text that the caller sanitizes.

_EXPANSION_SYSTEM = (
    "You are a search query rewriter. Given a user question, return 2-3 "
    "alternative phrasings that a knowledge base might have used. Output "
    "ONE phrasing per line, no numbering, no commentary, no quotes."
)

_EXPANSION_USER_TEMPLATE = (
    "Original query: {query}\n\n"
    "Alternative phrasings (one per line):"
)

_HYDE_SYSTEM = (
    "You are a knowledge writer. Given a user question, draft a SHORT "
    "(2-3 sentences) hypothetical passage that would directly answer it. "
    "Write as if it were an entry in a technical knowledge base — "
    "definitive, factual, no hedging. Do not say 'I don't know'. Make "
    "your best technical guess if needed."
)

_HYDE_USER_TEMPLATE = (
    "Question: {query}\n\n"
    "Hypothetical passage:"
)


def expand_query(
    query: str,
    llm: Any,
    *,
    max_alternates: int = 3,
    max_tokens: int = 120,
    temperature: float = 0.4,
) -> str:
    """Return an expanded query string: original + LLM-generated alternates.

    The alternates are joined with ' | ' separators so BM25 picks up the
    extra terms while embedding still treats it as one document. Returns
    the original query unchanged if the LLM call fails or returns empty.
    """
    if not query.strip():
        return query
    if llm is None or not getattr(llm, "is_available", lambda: False)():
        return query
    try:
        out = llm.generate(
            prompt=_EXPANSION_USER_TEMPLATE.format(query=query),
            system=_EXPANSION_SYSTEM,
            max_tokens=max_tokens,
            temperature=temperature,
        )
    except Exception as e:  # noqa: BLE001
        log.debug("query expansion LLM call failed: %s", e)
        return query

    lines = [
        ln.strip().lstrip("-•*").strip()
        for ln in (out or "").splitlines()
        if ln.strip()
    ]
    # Drop the original line if the LLM echoed it; cap to max_alternates;
    # filter trivial dupes.
    seen = {query.strip().lower()}
    alternates: list[str] = []
    for ln in lines:
        key = ln.lower()
        if key in seen or len(ln) < 3:
            continue
        seen.add(key)
        alternates.append(ln)
        if len(alternates) >= max_alternates:
            break
    if not alternates:
        return query
    return query + " | " + " | ".join(alternates)


def hyde_document(
    query: str,
    llm: Any,
    *,
    max_tokens: int = 200,
    temperature: float = 0.3,
) -> str | None:
    """Return a hypothetical answer-document for the query, or None on failure.

    The caller embeds this document instead of the raw query. None means
    "fall back to vanilla query embedding" — never raises. Empty/whitespace
    output is treated as failure so we don't embed garbage.
    """
    if not query.strip():
        return None
    if llm is None or not getattr(llm, "is_available", lambda: False)():
        return None
    try:
        out = llm.generate(
            prompt=_HYDE_USER_TEMPLATE.format(query=query),
            system=_HYDE_SYSTEM,
            max_tokens=max_tokens,
            temperature=temperature,
        )
    except Exception as e:  # noqa: BLE001
        log.debug("HyDE LLM call failed: %s", e)
        return None
    text = (out or "").strip()
    if len(text) < 8:
        return None
    return text
