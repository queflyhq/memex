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
import threading
from collections import OrderedDict
from typing import Any

import numpy as np

log = logging.getLogger(__name__)


class EmbeddingsUnavailableError(RuntimeError):
    """Raised when an op needs embeddings but no provider is loaded."""


def _l2_normalize(v: np.ndarray) -> np.ndarray:
    """Force unit L2 norm. Cosine similarity reduces to dot product after this,
    eliminating magnitude-bias on retrieval ranking."""
    n = float(np.linalg.norm(v))
    if n <= 1e-12:
        return v
    return v / n


class NoOpEmbeddingProvider:
    """The Tier 0 default. Reports unavailability; raises on use.

    Signatures must mirror FastEmbedProvider so callers can pass
    is_query without TypeError (the recall path always passes it).
    """

    dim: int = 384

    def is_available(self) -> bool:
        return False

    def embed(self, text: str, *, is_query: bool = False) -> np.ndarray:  # noqa: ARG002
        raise EmbeddingsUnavailableError(
            "embedding tier not installed. Install with: pip install 'memex[embed]'"
        )

    def embed_batch(
        self, texts: list[str], *, is_query: bool = False,  # noqa: ARG002
    ) -> list[np.ndarray]:
        raise EmbeddingsUnavailableError(
            "embedding tier not installed. Install with: pip install 'memex[embed]'"
        )


class FastEmbedProvider:
    """ONNX embeddings via fastembed. Model loaded lazily on first call.

    Adds production-quality concerns over the bare fastembed wrapper:

    * **L2 normalization** of every output vector.
    * **Instruction prefixes** so query and document encodings live in
      the same retrieval sub-space.
    * **In-process LRU cache** so repeated `embed(same_text)` is one ONNX call.
    * **Session pool** for concurrent recalls. Each ONNX InferenceSession
      has a single intra-op thread pool — when multiple threads call .embed()
      on the same session, they contend and serialize. Pooling N sessions
      lets N requests run in parallel.

    Pool size defaults to min(4, os.cpu_count() // 2) to balance RAM
    (~150MB per session) vs concurrency. Override via MEMEX_EMBED_POOL_SIZE.
    """

    def __init__(
        self,
        model_name: str = "sentence-transformers/all-MiniLM-L6-v2",
        dim: int = 384,
        query_prefix: str = "",
        doc_prefix: str = "",
        l2_normalize: bool = True,
        cache_size: int = 1024,
        pool_size: int | None = None,
    ):
        import os as _os
        self.model_name = model_name
        self.dim = dim
        self.query_prefix = query_prefix
        self.doc_prefix = doc_prefix
        self.l2_normalize = l2_normalize
        # Pool sizing: default to min(4, cpu//2). Bigger pool = more RAM
        # (~150MB per BGE-small session) but more parallel throughput.
        env_pool = _os.environ.get("MEMEX_EMBED_POOL_SIZE")
        if pool_size is not None:
            self._pool_size = max(1, int(pool_size))
        elif env_pool:
            self._pool_size = max(1, int(env_pool))
        else:
            cpu = _os.cpu_count() or 4
            self._pool_size = max(1, min(4, cpu // 2))
        self._pool: list[Any] = []  # list of fastembed TextEmbedding instances
        self._pool_available: list[bool] = []  # parallel array, True = idle
        self._pool_lock = threading.Lock()
        self._pool_cond = threading.Condition(self._pool_lock)
        self._load_lock = threading.Lock()
        self._cache: OrderedDict[str, np.ndarray] = OrderedDict()
        self._cache_lock = threading.Lock()
        self._cache_size = max(0, cache_size)

    def is_available(self) -> bool:
        # Broad except — fastembed can fail to import for reasons OTHER than
        # "package not installed" (e.g. an onnxruntime version mismatch
        # raises AttributeError at class-definition time). Treat any import-
        # time failure as "not available" so the daemon falls back to NoOp
        # cleanly. The exception is logged once at INFO so install/runtime
        # bugs aren't silent.
        try:
            import fastembed  # noqa: F401
        except Exception as e:  # noqa: BLE001
            log.info("fastembed not usable for embeddings: %s", e)
            return False
        return True

    def _build_one(self) -> Any:
        """Construct one fastembed TextEmbedding instance. Each instance
        owns its own ONNX InferenceSession — that's the unit of concurrency.
        """
        try:
            from fastembed import TextEmbedding
        except Exception as e:  # noqa: BLE001
            raise EmbeddingsUnavailableError(f"fastembed not usable: {e}") from e
        import os as _os
        from pathlib import Path as _Path
        local_root = (
            _os.environ.get("MEMEX_MODEL_ROOT")
            or str(
                _Path(_os.environ.get(
                    "LOCALAPPDATA",
                    str(_Path.home() / ".local" / "share"),
                )) / "Quefly" / "memex" / "models"
            )
        )
        local_path = _Path(local_root) / self.model_name.replace("/", "--")
        try:
            if local_path.is_dir():
                return TextEmbedding(
                    model_name=self.model_name,
                    cache_dir=str(_Path(local_root)),
                )
            return TextEmbedding(model_name=self.model_name)
        except Exception as e:  # noqa: BLE001
            log.warning("embed instance build failed (%s); fallback default cache", e)
            return TextEmbedding(model_name=self.model_name)

    def _ensure_pool(self) -> None:
        """Build the session pool on first use. Lazy — first request pays
        the boot cost. Pre-warm via `engine.embedding_provider.embed("warmup")`
        from the daemon's startup thread."""
        if self._pool:
            return
        with self._load_lock:
            if self._pool:
                return
            log.info(
                "loading embedding model: %s  (pool of %d for concurrency)",
                self.model_name, self._pool_size,
            )
            for _ in range(self._pool_size):
                self._pool.append(self._build_one())
                self._pool_available.append(True)
            log.info("embedding model pool loaded: %d instances", len(self._pool))

    def _checkout(self) -> tuple[int, Any]:
        """Block until an idle pool slot is available; return (idx, instance)."""
        self._ensure_pool()
        with self._pool_cond:
            while True:
                for i, avail in enumerate(self._pool_available):
                    if avail:
                        self._pool_available[i] = False
                        return i, self._pool[i]
                self._pool_cond.wait()

    def _checkin(self, idx: int) -> None:
        with self._pool_cond:
            self._pool_available[idx] = True
            self._pool_cond.notify()

    def _load(self) -> Any:
        """Back-compat: return a single instance. Equivalent to checkout
        without checkin — kept for API compatibility but new code should
        use _checkout/_checkin around the actual embed call."""
        self._ensure_pool()
        return self._pool[0]

    def _post(self, v: Any) -> np.ndarray:
        arr = np.asarray(v, dtype=np.float32)
        return _l2_normalize(arr) if self.l2_normalize else arr

    def _cache_get(self, key: str) -> np.ndarray | None:
        if self._cache_size == 0:
            return None
        with self._cache_lock:
            v = self._cache.get(key)
            if v is None:
                return None
            self._cache.move_to_end(key)
            return v

    def _cache_put(self, key: str, value: np.ndarray) -> None:
        if self._cache_size == 0:
            return
        with self._cache_lock:
            self._cache[key] = value
            self._cache.move_to_end(key)
            while len(self._cache) > self._cache_size:
                self._cache.popitem(last=False)

    def embed(self, text: str, *, is_query: bool = False) -> np.ndarray:
        # Prefix is part of the cache key — query and document encodings of
        # the same string live in different sub-spaces and must not collide.
        prefix = self.query_prefix if is_query else self.doc_prefix
        cache_key = f"q::{text}" if is_query else f"d::{text}"
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached
        prefixed = (prefix + text) if prefix else text
        idx, model = self._checkout()
        try:
            vectors = list(model.embed([prefixed]))
        finally:
            self._checkin(idx)
        if not vectors:
            raise RuntimeError("fastembed returned empty result")
        out = self._post(vectors[0])
        self._cache_put(cache_key, out)
        return out

    def embed_batch(
        self, texts: list[str], *, is_query: bool = False
    ) -> list[np.ndarray]:
        if not texts:
            return []
        model = self._load()
        prefix = self.query_prefix if is_query else self.doc_prefix
        prefixed = [(prefix + t) if prefix else t for t in texts]
        return [self._post(v) for v in model.embed(prefixed)]


def build_default_provider(
    model_name: str = "sentence-transformers/all-MiniLM-L6-v2",
    dim: int = 384,
    query_prefix: str = "",
    doc_prefix: str = "",
    l2_normalize: bool = True,
    cache_size: int = 1024,
):
    """Return FastEmbed if installed, NoOp otherwise — never raises."""
    fe = FastEmbedProvider(
        model_name=model_name,
        dim=dim,
        query_prefix=query_prefix,
        doc_prefix=doc_prefix,
        l2_normalize=l2_normalize,
        cache_size=cache_size,
    )
    if fe.is_available():
        return fe
    log.info("fastembed not installed — using NoOp embedding provider (Tier 0)")
    return NoOpEmbeddingProvider()
