"""
Cross-encoder rerankers.

The hybrid retriever produces ~50 candidates via BM25 + vector RRF. Those
ranks are noisy — a cross-encoder that scores (query, candidate) pairs jointly
is the single biggest precision win available without changing the storage
layer. We rerank top-50 → top-K and hand only the survivors to budget
enforcement.

Two providers ship:

  - NoOpReranker — always-available, returns input untouched. The Tier 0
    default; reranking adds ~30 ms / 50-doc page so it should be a
    deliberate opt-in until the user has confirmed fastembed runs cleanly
    in their environment.
  - FastEmbedReranker — lazy-loads BAAI/bge-reranker-base via fastembed's
    TextCrossEncoder. Model is ~250 MB ONNX, runs on CPU.

Same loud-failure-or-graceful-fallback discipline as embeddings.py.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from memex.core.schema import Concept

log = logging.getLogger(__name__)


class RerankUnavailableError(RuntimeError):
    """Raised when a reranker is required but no provider is loaded."""


class NoOpReranker:
    """Tier 0 default — passes through, reports unavailable."""

    def is_available(self) -> bool:
        return False

    def rerank(self, query: str, candidates: list[Concept], top_k: int = 10) -> list[Concept]:
        return candidates[:top_k]


class FastEmbedReranker:
    """ONNX cross-encoder via fastembed. Model loaded lazily."""

    def __init__(self, model_name: str = "Xenova/ms-marco-MiniLM-L-6-v2"):
        self.model_name = model_name
        self._model: Any | None = None
        self._load_lock = threading.Lock()

    def is_available(self) -> bool:
        # Broad except — fastembed can fail to import for reasons other than
        # ImportError (onnxruntime version mismatches throw at class-def
        # time). Any import-time failure means "not available"; daemon
        # falls back to NoOpReranker.
        try:
            from fastembed.rerank.cross_encoder import TextCrossEncoder  # noqa: F401
        except Exception as e:  # noqa: BLE001
            log.info("fastembed not usable for rerank: %s", e)
            return False
        return True

    def _load(self) -> Any:
        if self._model is not None:
            return self._model
        with self._load_lock:
            if self._model is not None:
                return self._model
            try:
                from fastembed.rerank.cross_encoder import TextCrossEncoder
            except Exception as e:  # noqa: BLE001
                raise RerankUnavailableError(
                    f"fastembed not usable: {e}"
                ) from e
            log.info("loading reranker model: %s", self.model_name)
            self._model = TextCrossEncoder(model_name=self.model_name)
            return self._model

    def rerank(self, query: str, candidates: list[Concept], top_k: int = 10) -> list[Concept]:
        if not candidates:
            return []
        model = self._load()
        docs = [_doc_text(c) for c in candidates]
        # rerank() returns a generator of floats — one score per doc, in input order.
        scores = list(model.rerank(query, docs))
        ranked = sorted(zip(candidates, scores), key=lambda p: p[1], reverse=True)
        return [c for c, _ in ranked[:top_k]]


def build_default_reranker(model_name: str = "Xenova/ms-marco-MiniLM-L-6-v2"):
    """Return FastEmbed if installed, NoOp otherwise — never raises.

    Honors <data_dir>/active_reranker.txt — when present and non-empty,
    its contents override `model_name`. This is how `memex train-reranker
    --apply` flips the live model without code changes. Restart the
    daemon to pick up a new active reranker.
    """
    try:
        from memex.config import get_settings
        from pathlib import Path as _Path
        active = _Path(str(get_settings().data_dir)) / "active_reranker.txt"
        if active.is_file():
            override = active.read_text(encoding="utf-8").strip()
            if override:
                log.info("reranker override active: %s", override)
                model_name = override
    except Exception:  # noqa: BLE001
        pass  # defensive — never break daemon startup over a config file
    fe = FastEmbedReranker(model_name=model_name)
    if fe.is_available():
        return fe
    log.info("fastembed reranker not available — using NoOp (no reranking)")
    return NoOpReranker()


def _doc_text(c: Concept) -> str:
    return f"{c.name}\n{c.description}".strip()
