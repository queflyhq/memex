"""
Cognition layer — algorithms over memory.

The ML / retrieval pipeline plus all the lifecycle algorithms (decay,
forgetting, verification, consolidation). Cognition is what turns the
durable graph into useful answers and what lets memory *learn* over
time.

Layer in the 5-layer OMP architecture:
  perception → context → COGNITION → memory ← action

Re-exports — physical files live in:
  - memex.core.retrieval.{bm25,hybrid,budget,query}
  - memex.core.lifecycle.{decay,forgetting,verification,consolidation}
  - memex.ml.{embeddings,rerank,llm,finetune,eval}
"""

from __future__ import annotations

# Retrieval — the hot path of recall.
from memex.core.retrieval.hybrid import HybridRetriever
from memex.core.retrieval.bm25 import BM25Index
from memex.core.retrieval.budget import enforce_budget, estimate_tokens
from memex.core.retrieval.query import expand_query, hyde_document

# Lifecycle — the slow path of memory hygiene.
from memex.core.lifecycle.decay import (
    decayed_confidence,
    half_life_for_kind,
    DEFAULT_HALF_LIFE_DAYS,
)
from memex.core.lifecycle.forgetting import run_forgetting

# ML — embeddings, rerank, LLM, fine-tune, evaluation.
from memex.ml.embeddings import (
    NoOpEmbeddingProvider,
    FastEmbedProvider,
    build_default_provider,
    EmbeddingsUnavailableError,
)
from memex.ml.rerank import (
    NoOpReranker,
    FastEmbedReranker,
    build_default_reranker,
)
from memex.ml.llm import (
    NoOpLLMProvider,
    OllamaProvider,
    AnthropicProvider,
    build_default_llm,
)
from memex.ml.finetune import mine_pairs, write_jsonl, summary as pairs_summary
from memex.ml.eval import (
    EvalResult,
    split_pairs,
    evaluate,
    evaluate_against_pairs,
)

__all__ = [
    # retrieval
    "HybridRetriever",
    "BM25Index",
    "enforce_budget",
    "estimate_tokens",
    "expand_query",
    "hyde_document",
    # lifecycle
    "decayed_confidence",
    "half_life_for_kind",
    "DEFAULT_HALF_LIFE_DAYS",
    "run_forgetting",
    # ML / embeddings
    "NoOpEmbeddingProvider",
    "FastEmbedProvider",
    "build_default_provider",
    "EmbeddingsUnavailableError",
    # ML / rerank
    "NoOpReranker",
    "FastEmbedReranker",
    "build_default_reranker",
    # ML / LLM
    "NoOpLLMProvider",
    "OllamaProvider",
    "AnthropicProvider",
    "build_default_llm",
    # ML / fine-tune + eval
    "mine_pairs",
    "write_jsonl",
    "pairs_summary",
    "EvalResult",
    "split_pairs",
    "evaluate",
    "evaluate_against_pairs",
]
