"""Extract durable facts from user_prompt episodic events.

Memex's `user_prompt` event captures every message a user sends to AI.
Over a week you accumulate hundreds. Most are throwaway, but a meaningful
fraction contain durable facts — "we use JWT", "always test with real DB",
"never push to main", "remember that X depends on Y".

Without an LLM, we can still catch a useful subset heuristically:

  - "remember X" / "remember that X" → fact
  - "we use X" / "we're using X" → fact (tech stack)
  - "always Y" / "never Y" / "must Y" / "must not Y" → constraint
  - "the rule is X" / "policy: X" → constraint

False positive rate is real but bounded by short window + low confidence.
LLM-based extraction can layer on top later when MEMEX_LLM is configured.

Idempotent: dedups by normalized fact text within a 7-day window.
"""

from __future__ import annotations

import logging
import re
import time
from typing import TYPE_CHECKING, Any

from memex.core.schema import Concept, NodeKind, Source

if TYPE_CHECKING:
    from memex.core.engine import Engine

log = logging.getLogger(__name__)


# (pattern, capture_group_index, target_kind, prefix_for_name)
_PATTERNS: list[tuple[re.Pattern[str], int, NodeKind, str]] = [
    # "remember X" / "remember that X is Y"
    (re.compile(r"\bremember(?:\s+that)?\s+(.{8,200}?)(?:[.!?\n]|$)", re.I), 1, NodeKind.fact, ""),
    # "we use X" / "we're using X" / "the stack is X"
    (re.compile(r"\b(?:we\s+(?:use|are\s+using|chose|picked)|stack\s+is)\s+(.{4,120}?)(?:[.!?\n]|$)", re.I), 1, NodeKind.fact, "Uses "),
    # "always X" / "must X"  → constraint
    (re.compile(r"\b(?:always|must|need to)\s+(.{8,150}?)(?:[.!?\n]|$)", re.I), 1, NodeKind.constraint, "Must "),
    # "never X" / "must not X" / "don't X"
    (re.compile(r"\b(?:never|must\s+not|don'?t|do\s+not)\s+(.{6,150}?)(?:[.!?\n]|$)", re.I), 1, NodeKind.constraint, "Must not "),
    # "the rule is X" / "policy is X"
    (re.compile(r"\b(?:the\s+rule|policy)\s+is\s+(.{8,150}?)(?:[.!?\n]|$)", re.I), 1, NodeKind.constraint, "Rule: "),
]

# Skip prompts that look like routine code requests rather than facts.
_NOISE_PROMPT_PATTERNS = [
    re.compile(r"^/\w+", re.I),                  # slash commands
    re.compile(r"^(can you|please|help|fix|add|run|show|list)\b", re.I),
    re.compile(r"^(yes|no|ok|sure|continue|next)", re.I),
]


def extract_prompt_facts(
    engine: "Engine",
    *,
    lookback_events: int = 100,
    max_extract: int = 20,
) -> dict[str, Any]:
    """Walk recent user_prompt events; create fact/constraint concepts.

    Returns counts. Safe to call repeatedly — dedup by name+kind+source.
    """
    t0 = time.time()
    events = list(engine.episodic.recent(limit=lookback_events) or [])
    prompts = [
        e for e in events
        if getattr(e, "kind", "") == "user_prompt"
    ]
    facts_added = 0
    constraints_added = 0
    matched_prompts = 0
    for e in prompts:
        if facts_added + constraints_added >= max_extract:
            break
        payload = getattr(e, "payload", {}) or {}
        text = (
            payload.get("prompt")
            or payload.get("text")
            or payload.get("content")
            or ""
        )
        if not isinstance(text, str) or len(text) < 12:
            continue
        # Skip routine code-request prompts.
        if any(p.match(text.strip()) for p in _NOISE_PROMPT_PATTERNS):
            continue
        extracted = _extract_from_text(text)
        if not extracted:
            continue
        matched_prompts += 1
        for name, kind in extracted:
            if facts_added + constraints_added >= max_extract:
                break
            existing = engine.semantic.find_by_name_kind_source(
                name, kind, Source.human,
            )
            if existing is not None:
                # Bump confirmation timestamp so it doesn't decay.
                try:
                    engine.put(existing)
                except Exception:  # noqa: BLE001
                    pass
                continue
            try:
                engine.semantic.add_concept(Concept(
                    name=name, kind=kind,
                    description=text[:500].strip(),
                    source=Source.human,
                    confidence=0.7,  # heuristic; not LLM-verified
                    metadata={
                        "extracted_from": "user_prompt",
                        "event_id": getattr(e, "id", None),
                    },
                ))
                if kind == NodeKind.fact:
                    facts_added += 1
                else:
                    constraints_added += 1
            except Exception as exc:  # noqa: BLE001
                log.debug("prompt-fact add_concept failed: %s", exc)
    return {
        "prompts_scanned": len(prompts),
        "prompts_matched": matched_prompts,
        "facts_added": facts_added,
        "constraints_added": constraints_added,
        "elapsed_ms": round((time.time() - t0) * 1000.0, 1),
    }


def _extract_from_text(text: str) -> list[tuple[str, NodeKind]]:
    """Apply each heuristic pattern; return (concept_name, kind) tuples."""
    out: list[tuple[str, NodeKind]] = []
    seen_names: set[str] = set()
    for regex, grp, kind, prefix in _PATTERNS:
        for m in regex.finditer(text):
            captured = m.group(grp).strip().rstrip(",;: ")
            if len(captured) < 6 or len(captured) > 120:
                continue
            # Strip wrapping quotes / backticks
            captured = re.sub(r"^[`\"']+|[`\"']+$", "", captured).strip()
            if not captured:
                continue
            name = (prefix + captured).strip()
            if name in seen_names:
                continue
            seen_names.add(name)
            out.append((name[:160], kind))
    return out
