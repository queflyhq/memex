"""Tests for codebase memory — typed-graph chunking + recall.

Slice A end-to-end: chunk Python files, store as typed nodes (`source` →
`file` → `symbol`) with explicit edges (`defined_in`, `part_of`), recall by
symbol name, walk the neighborhood, surface knowledge concepts that
mention the symbol.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from memex.codebase import add_source
from memex.codebase.chunker import chunk_file
from memex.codebase.indexer import index_source, reindex_source
from memex.codebase.recall import recall_code
from memex.codebase.sources import (
    get_source,
    list_sources,
    remove_source,
)
from memex.core.engine import Engine
from memex.core.schema import EdgeKind, NodeKind


# ---- chunker ------------------------------------------------------------------


def test_chunker_python_extracts_classes_and_methods(tmp_path: Path):
    src = tmp_path / "demo.py"
    src.write_text(
        textwrap.dedent("""
            class Foo:
                def bar(self, x: int) -> int:
                    return x + 1

                def baz(self):
                    pass

            def standalone():
                return 42
        """).strip(),
        encoding="utf-8",
    )
    chunks = chunk_file(src)
    by_kind: dict[str, list[str]] = {}
    for c in chunks:
        by_kind.setdefault(c.symbol_kind, []).append(c.name)

    assert "class" in by_kind and "Foo" in by_kind["class"]
    assert "method" in by_kind
    assert {"bar", "baz"} <= set(by_kind["method"])
    assert "function" in by_kind and "standalone" in by_kind["function"]

    # Methods carry parent_symbol = class name
    methods = [c for c in chunks if c.symbol_kind == "method"]
    assert all(m.parent_symbol == "Foo" for m in methods)


def test_chunker_unsupported_extension_returns_empty(tmp_path: Path):
    src = tmp_path / "binary.bin"
    src.write_bytes(b"\x00\x01\x02\x03")
    assert chunk_file(src) == []


def test_chunker_yaml_k8s_aware(tmp_path: Path):
    src = tmp_path / "deploy.yaml"
    src.write_text(
        textwrap.dedent("""
            apiVersion: apps/v1
            kind: Deployment
            metadata:
              name: admin-console
              namespace: prod
            spec:
              replicas: 3
        """).strip(),
        encoding="utf-8",
    )
    chunks = chunk_file(src)
    assert len(chunks) == 1
    assert chunks[0].symbol_kind == "manifest"
    assert chunks[0].name == "Deployment/admin-console"
    assert chunks[0].metadata.get("k8s_kind") == "Deployment"


# ---- sources + indexer + recall (full end-to-end) ---------------------------


def _make_python_codebase(root: Path) -> None:
    (root / "registry.py").write_text(
        textwrap.dedent("""
            class RegistryClient:
                def __init__(self, owner_repo: str):
                    self.owner_repo = owner_repo

                def fetch_index(self) -> list[dict]:
                    return []

                def find_entry(self, name: str):
                    for entry in self.fetch_index():
                        if entry.get("name") == name:
                            return entry
                    return None
        """).strip(),
        encoding="utf-8",
    )
    (root / "helpers.py").write_text(
        textwrap.dedent("""
            def normalize_name(s: str) -> str:
                return s.strip().lower()

            def parse_target(target: str):
                return target.split("/")
        """).strip(),
        encoding="utf-8",
    )


def test_index_python_source_creates_typed_graph(engine: Engine, tmp_path: Path):
    code_root = tmp_path / "demo-pkg"
    code_root.mkdir()
    _make_python_codebase(code_root)

    src = add_source(engine, code_root, name="demo-pkg")
    assert src.kind == NodeKind.source

    result = index_source(engine, src.id)
    assert result.files_indexed == 2
    assert result.symbols_indexed >= 5  # 1 class + 3 methods + 2 funcs

    # Source list returns it.
    sources = list_sources(engine)
    assert any(s.id == src.id for s in sources)

    # Get-by-path works.
    again = get_source(engine, code_root)
    assert again is not None and again.id == src.id

    # File concepts exist with source_id metadata.
    files = [c for c in engine.find_by_kind(NodeKind.file)
             if c.metadata.get("source_id") == src.id]
    assert len(files) == 2
    rel_paths = sorted(f.metadata["rel_path"] for f in files)
    assert rel_paths == ["helpers.py", "registry.py"]

    # Symbols carry symbol_kind, signature, line range.
    symbols = [c for c in engine.find_by_kind(NodeKind.symbol)
               if c.metadata.get("source_id") == src.id]
    by_name = {s.name: s for s in symbols}
    assert "RegistryClient" in by_name
    rc = by_name["RegistryClient"]
    assert rc.metadata["symbol_kind"] == "class"
    assert rc.metadata["language"] == "python"
    assert rc.metadata["start_line"] >= 1


def test_recall_code_returns_typed_neighborhood(engine: Engine, tmp_path: Path):
    code_root = tmp_path / "demo-pkg"
    code_root.mkdir()
    _make_python_codebase(code_root)
    src = add_source(engine, code_root, name="demo-pkg")
    index_source(engine, src.id)

    result = recall_code(engine, "RegistryClient", expand_hops=1)
    assert len(result.matches) == 1
    rc = result.matches[0]
    assert rc.kind == NodeKind.symbol
    assert rc.metadata["symbol_kind"] == "class"

    # Neighborhood: defining file + methods of the class.
    neighbor_kinds = {n.kind for n in result.neighborhood}
    assert NodeKind.file in neighbor_kinds
    assert NodeKind.symbol in neighbor_kinds  # methods
    method_names = {n.name for n in result.neighborhood if n.kind == NodeKind.symbol}
    assert {"__init__", "fetch_index", "find_entry"} & method_names

    # Edges include defined_in + part_of (symbol ↔ file, method ↔ class).
    edge_kinds = {e.kind for e in result.edges}
    assert EdgeKind.defined_in in edge_kinds
    assert EdgeKind.part_of in edge_kinds


def test_recall_code_surfaces_related_decisions(engine: Engine, tmp_path: Path):
    """Unified-memory-model: a decision concept that mentions a symbol
    by name is surfaced as a related concept in recall_code."""
    code_root = tmp_path / "demo-pkg"
    code_root.mkdir()
    _make_python_codebase(code_root)
    src = add_source(engine, code_root, name="demo-pkg")
    index_source(engine, src.id)

    # Stand up a decision concept that mentions RegistryClient.
    engine.add(
        name="why we built RegistryClient",
        description=(
            "RegistryClient was introduced to fetch skills over HTTPS without "
            "a git client dependency. Stateless by design — each call does "
            "its own fetch."
        ),
        kind=NodeKind.decision,
    )

    result = recall_code(engine, "RegistryClient")
    related_names = [c.name for c in result.related_concepts]
    assert any("RegistryClient" in n for n in related_names)
    decision_kinds = {c.kind for c in result.related_concepts}
    assert NodeKind.decision in decision_kinds


def test_recall_code_no_match_returns_empty(engine: Engine, tmp_path: Path):
    code_root = tmp_path / "demo-pkg"
    code_root.mkdir()
    _make_python_codebase(code_root)
    src = add_source(engine, code_root, name="demo-pkg")
    index_source(engine, src.id)

    result = recall_code(engine, "DoesNotExist")
    assert result.matches == []
    assert result.neighborhood == []


def test_remove_source_cascades(engine: Engine, tmp_path: Path):
    code_root = tmp_path / "demo-pkg"
    code_root.mkdir()
    _make_python_codebase(code_root)
    src = add_source(engine, code_root, name="demo-pkg")
    result = index_source(engine, src.id)

    deleted = remove_source(engine, src.id)
    # 1 source + 2 files + N symbols
    assert deleted == 1 + result.files_indexed + result.symbols_indexed

    # Source no longer reachable.
    assert engine.get(src.id) is None
    assert get_source(engine, code_root) is None
    leftover_files = [c for c in engine.find_by_kind(NodeKind.file)
                      if c.metadata.get("source_id") == src.id]
    leftover_syms = [c for c in engine.find_by_kind(NodeKind.symbol)
                     if c.metadata.get("source_id") == src.id]
    assert leftover_files == []
    assert leftover_syms == []


def test_reindex_source_replaces_chunks(engine: Engine, tmp_path: Path):
    code_root = tmp_path / "demo-pkg"
    code_root.mkdir()
    _make_python_codebase(code_root)
    src = add_source(engine, code_root, name="demo-pkg")
    first = index_source(engine, src.id)

    # Add a third file then reindex.
    (code_root / "extras.py").write_text(
        "def extra(): return 'hi'\n", encoding="utf-8"
    )
    second = reindex_source(engine, src.id)
    assert second.files_indexed == 3
    assert second.symbols_indexed > first.symbols_indexed

    # No duplicates — symbol named `extra` exists exactly once.
    symbols = [c for c in engine.find_by_kind(NodeKind.symbol)
               if c.metadata.get("source_id") == src.id and c.name == "extra"]
    assert len(symbols) == 1


def test_add_source_idempotent(engine: Engine, tmp_path: Path):
    code_root = tmp_path / "demo-pkg"
    code_root.mkdir()
    _make_python_codebase(code_root)
    a = add_source(engine, code_root)
    b = add_source(engine, code_root)
    assert a.id == b.id


