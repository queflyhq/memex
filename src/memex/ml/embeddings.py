"""
Embedding providers.

Implements the `EmbeddingProvider` Protocol. Two providers ship with v0.1:

  - NoOpEmbeddingProvider: always-available; reports `is_available() == False`,
    raises on actual embedding calls. The Tier 0 default.
  - FastEmbedProvider: lazy-loads a fastembed ONNX model on first use.
    Activated when the optional `embed` extra is installed.

Engine selects the provider via `build_default_provider()` which never raises:
if fastembed is missing, you get NoOp and a log line. This is the loud-failure
discipline applied to a graceful-degradation context.
"""

from __future__ import annotations

import logging
from typing import Any

import numpy as np

log = logging.getLogger(__name__)


class EmbeddingsUnavailableError(RuntimeError):
    """Raised when an op needs embeddings but no provider is loaded."""


class NoOpEmbeddingProvider:
    """The Tier 0 default. Reports unavailability; raises on use."""

    dim: int = 384

    def is_available(self) -> bool:
        return False

    def embed(self, text: str) -> np.ndarray:
        raise EmbeddingsUnavailableError(
            "embedding tier not installed. Install with: pip install 'memex[embed]'"
        )

    def embed_batch(self, texts: list[str]) -> list[np.ndarray]:
        raise EmbeddingsUnavailableError(
            "embedding tier not installed. Install with: pip install 'memex[embed]'"
        )


class FastEmbedProvider:
    """ONNX embeddings via fastembed. Model loaded lazily on first call."""

    def __init__(
        self,
        model_name: str = "sentence-transformers/all-MiniLM-L6-v2",
        dim: int = 384,
    ):
        self.model_name = model_name
        self.dim = dim
        self._model: Any | None = None

    def is_available(self) -> bool:
        try:
            import fastembed  # noqa: F401
        except ImportError:
            return False
        return True

    def _load(self) -> Any:
        if self._model is not None:
            return self._model
        try:
            from fastembed import TextEmbedding
        except ImportError as e:
            raise EmbeddingsUnavailableError("fastembed not installed") from e
        log.info("loading embedding model: %s", self.model_name)
        self._model = TextEmbedding(model_name=self.model_name)
        return self._model

    def embed(self, text: str) -> np.ndarray:
        model = self._load()
        vectors = list(model.embed([text]))
        if not vectors:
            raise RuntimeError("fastembed returned empty result")
        return np.asarray(vectors[0], dtype=np.float32)

    def embed_batch(self, texts: list[str]) -> list[np.ndarray]:
        if not texts:
            return []
        model = self._load()
        return [np.asarray(v, dtype=np.float32) for v in model.embed(texts)]


def build_default_provider(
    model_name: str = "sentence-transformers/all-MiniLM-L6-v2",
    dim: int = 384,
):
    """Return FastEmbed if installed, NoOp otherwise — never raises."""
    fe = FastEmbedProvider(model_name=model_name, dim=dim)
    if fe.is_available():
        return fe
    log.info("fastembed not installed — using NoOp embedding provider (Tier 0)")
    return NoOpEmbeddingProvider()
