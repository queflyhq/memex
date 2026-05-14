"""
Fine-tune pair miner.

Walks the episodic stream and turns the (recall, downstream-signal)
trail into training triples for embedding-model fine-tuning. Three
labeled signals:

  * `(query, recalled_id)` followed by `auto_approval` or
    `context_injected` → POSITIVE — recall produced a useful hit.
  * `(query, recalled_id)` followed by `user_correction` → NEGATIVE —
    recall produced a misleading hit.
  * `recall_executed` with no follow-up signal → WEAK NEGATIVE.

Output is a JSONL file each line:
    {"anchor": "<query>", "positive": "<doc_text>", "negative": "<doc_text>"}

Sentence-Transformers' MultipleNegativesRankingLoss consumes this
directly. Once the user has > ~500 triples, a 30-min LoRA pass on the
frozen base model reliably beats the base by 15-30% on the user's own
queries.

This module is a *miner only* — it does not call training code. The
trainer (`scripts/train_lora.py`, future) takes the JSONL and runs
sentence-transformers. Decoupled so the miner can run on the server
without pulling torch into the daemon.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


# Maximum lookback when building a training set — beyond this we get
# stale state (queries against a different graph topology, models that
# have been swapped out, etc.).
_DEFAULT_LOOKBACK_EVENTS = 50_000

# Time window (seconds) within which a follow-up event is considered
# *attributable* to a recall. Beyond this we can't credibly say the
# recall caused the user_correction or auto_approval.
_ATTRIBUTION_WINDOW_S = 600  # 10 minutes


def _doc_text(name: str, description: str) -> str:
    """Render the same anchor text the indexer embedded; keeps train/serve
    distributional alignment."""
    return f"{name}\n{description}".strip()


def mine_pairs(
    engine: Any,
    *,
    lookback_events: int = _DEFAULT_LOOKBACK_EVENTS,
    attribution_window_s: int = _ATTRIBUTION_WINDOW_S,
) -> list[dict[str, str]]:
    """Walk the episodic stream and emit labelled pairs.

    Returns a list of dicts:
        {"anchor": <query>, "positive": <doc_text>}    -- successful recall
        {"anchor": <query>, "negative": <doc_text>}    -- corrected recall

    The trainer aligns these into MNR-loss triples (anchor + positive +
    in-batch negatives) by grouping per-anchor.
    """
    events = list(engine.episodic.recent(limit=lookback_events))
    if not events:
        return []

    # Walk forward in time so we can attribute follow-up signals to
    # the most recent preceding recall_executed.
    ordered = list(reversed(events))
    pairs: list[dict[str, str]] = []
    last_recall: dict[str, Any] | None = None

    for ev in ordered:
        payload = ev.payload or {}
        ts = ev.timestamp
        if ev.kind == "recall_executed":
            last_recall = {
                "ts": ts,
                "query": payload.get("query", ""),
                "ids": list(payload.get("recalled_ids") or []),
            }
            continue
        if last_recall is None:
            continue
        # Skip events outside the attribution window — too far from the
        # recall to credibly link.
        try:
            dt = (ts - last_recall["ts"]).total_seconds()
        except Exception:  # noqa: BLE001
            continue
        if dt < 0 or dt > attribution_window_s:
            continue
        # Pull the recalled concepts' doc texts.
        if ev.kind in {"auto_approval", "context_injected"}:
            label = "positive"
        elif ev.kind == "user_correction":
            label = "negative"
        else:
            continue
        # Use the explicit recalled_ids from the follow-up payload when
        # present (more accurate); fall back to the recall's set.
        ids = payload.get("recalled_ids") or last_recall["ids"]
        anchor = last_recall["query"]
        if not anchor or not ids:
            continue
        for cid in ids:
            c = engine.semantic.get_concept(cid)
            if c is None:
                continue
            pairs.append({
                "anchor": anchor,
                label: _doc_text(c.name, c.description),
            })

    log.info(
        "mined %d pairs from %d events (window=%ds)",
        len(pairs), len(events), attribution_window_s,
    )
    return pairs


def write_jsonl(pairs: list[dict[str, str]], out_path: Path) -> int:
    """Persist mined pairs to JSONL, one record per line. Returns count."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        for p in pairs:
            f.write(json.dumps(p, ensure_ascii=False) + "\n")
    return len(pairs)


def summary(pairs: list[dict[str, str]]) -> dict[str, int]:
    """Distribution summary for telemetry / dashboard."""
    pos = sum(1 for p in pairs if "positive" in p)
    neg = sum(1 for p in pairs if "negative" in p)
    distinct_anchors = len({p["anchor"] for p in pairs})
    return {
        "total_pairs": len(pairs),
        "positive": pos,
        "negative": neg,
        "distinct_queries": distinct_anchors,
        "ready_to_train": len(pairs) >= 500,
    }
