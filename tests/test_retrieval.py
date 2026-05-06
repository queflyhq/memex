"""Retrieval pipeline tests — BM25 + budget enforcement + RRF."""

from __future__ import annotations

from memex.core.retrieval.bm25 import BM25Index, tokenize
from memex.core.retrieval.budget import enforce_budget
from memex.core.schema import Concept


def test_tokenize_alphanumeric():
    assert tokenize("Hello, World! 123") == ["hello", "world", "123"]


def test_bm25_returns_relevant_first():
    concepts = [
        Concept(name="postgres", description="JSONB native"),
        Concept(name="mysql", description="popular database"),
        Concept(name="redis", description="key value store"),
    ]
    idx = BM25Index()
    idx.rebuild(concepts)
    hits = idx.search("postgres jsonb", limit=3)
    assert hits
    assert hits[0][0].name == "postgres"


def test_bm25_empty_corpus():
    idx = BM25Index()
    idx.rebuild([])
    assert idx.search("anything") == []


def test_budget_truncates_when_exceeded():
    nodes = [
        Concept(name=f"node-{i}", description="x" * 200)
        for i in range(20)
    ]
    result = enforce_budget(nodes, edges=[], budget_tokens=200)
    assert len(result.nodes) < len(nodes)
    assert result.tokens_used <= 200 + 60  # small allowance for trailing edge


def test_budget_keeps_at_least_one():
    """A single huge node still gets returned — budget never drops everything."""
    nodes = [Concept(name="huge", description="x" * 5000)]
    result = enforce_budget(nodes, edges=[], budget_tokens=100)
    assert len(result.nodes) == 1
