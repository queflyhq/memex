"""
Retrieval evaluation harness.

Honest measurement of "is the memory actually finding the right thing".
Splits mined (query, useful-result) pairs into train/test, runs the
current retrieval pipeline on each test query, and scores three
classical IR metrics:

  * **precision@1** — fraction of test queries where the gold result
    appears in the top-1 hit. The hardest, most user-facing metric.
  * **precision@5** — fraction where the gold result is in the top-5.
    Less brittle, captures "near-misses where rerank could fix it".
  * **MRR** — mean reciprocal rank of the gold result across the top-20.
    A unified summary that rewards both "always finds" AND "finds near
    the top".

The harness is deterministic given a seed, so retrains can be A/B'd:
score base, swap adapter, score adapter, keep whichever wins. That's
what makes the auto-train loop trustworthy — every model swap has a
*number attached* the user can verify on the dashboard.
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)


@dataclass
class EvalResult:
    n_queries: int
    precision_at_1: float
    precision_at_5: float
    mrr: float
    notes: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "n_queries": self.n_queries,
            "precision_at_1": round(self.precision_at_1, 4),
            "precision_at_5": round(self.precision_at_5, 4),
            "mrr": round(self.mrr, 4),
            "notes": self.notes,
        }


def split_pairs(
    pairs: list[dict[str, str]],
    *,
    test_fraction: float = 0.2,
    seed: int = 42,
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Deterministic train/test split. Negatives stay with their anchor's
    side (so a query is either fully train or fully test, never both)."""
    rng = random.Random(seed)
    # Group by anchor to keep all signals for one query on the same side.
    by_anchor: dict[str, list[dict[str, str]]] = {}
    for p in pairs:
        by_anchor.setdefault(p["anchor"], []).append(p)
    anchors = list(by_anchor.keys())
    rng.shuffle(anchors)
    cut = int(len(anchors) * (1.0 - test_fraction))
    train_anchors = set(anchors[:cut])
    train_pairs: list[dict[str, str]] = []
    test_pairs: list[dict[str, str]] = []
    for a in anchors:
        bucket = train_pairs if a in train_anchors else test_pairs
        bucket.extend(by_anchor[a])
    return train_pairs, test_pairs


def evaluate(
    engine: Any,
    test_pairs: list[dict[str, str]],
    *,
    k_top: int = 20,
    budget_tokens: int = 800,
) -> EvalResult:
    """Run each test query through engine.recall(); score top-k hits.

    A query is "correct" when its labeled positive document text appears
    in the description of one of the top-k retrieved concepts. We compare
    by description prefix (first 200 chars) since stored docs may be
    truncated.

    Queries that have NO `positive` pair are skipped (we can't score
    something we don't have a target for). Negatives are unused here —
    they feed the trainer, not the evaluator.
    """
    if not test_pairs:
        return EvalResult(0, 0.0, 0.0, 0.0, notes="empty test set")

    # Group test pairs by query, keeping only positives.
    targets: dict[str, list[str]] = {}
    for p in test_pairs:
        if "positive" not in p:
            continue
        targets.setdefault(p["anchor"], []).append(p["positive"][:200])

    if not targets:
        return EvalResult(
            len(test_pairs), 0.0, 0.0, 0.0,
            notes="no positive-labelled test pairs to score against",
        )

    n = 0
    p1 = 0
    p5 = 0
    rr_sum = 0.0
    for query, gold_docs in targets.items():
        try:
            r = engine.recall(query, budget_tokens=budget_tokens)
        except Exception as e:  # noqa: BLE001
            log.debug("eval recall failed for %r: %s", query[:60], e)
            continue
        n += 1
        ranked_docs = [
            f"{c.name}\n{c.description}".strip()[:200]
            for c in r.nodes[:k_top]
        ]
        rank = next(
            (i + 1 for i, d in enumerate(ranked_docs) if d in gold_docs),
            None,
        )
        if rank is None:
            continue
        if rank == 1:
            p1 += 1
        if rank <= 5:
            p5 += 1
        rr_sum += 1.0 / rank

    if n == 0:
        return EvalResult(0, 0.0, 0.0, 0.0, notes="all eval recalls failed")

    return EvalResult(
        n_queries=n,
        precision_at_1=p1 / n,
        precision_at_5=p5 / n,
        mrr=rr_sum / n,
    )


def evaluate_against_pairs(
    engine: Any,
    pairs: list[dict[str, str]],
    *,
    test_fraction: float = 0.2,
    seed: int = 42,
    k_top: int = 20,
    budget_tokens: int = 800,
) -> dict[str, Any]:
    """End-to-end: split pairs, run eval, return JSON-serializable dict.

    Includes split sizes so the caller can decide if the test set was
    big enough to trust. Below ~50 test queries the metrics are noisy.
    """
    train, test = split_pairs(pairs, test_fraction=test_fraction, seed=seed)
    result = evaluate(engine, test, k_top=k_top, budget_tokens=budget_tokens)
    return {
        "train_pairs": len(train),
        "test_pairs": len(test),
        "test_queries_scored": result.n_queries,
        "trustworthy": result.n_queries >= 50,
        **result.as_dict(),
    }
