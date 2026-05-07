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


# ---- rich edges (calls, imports, extends) ------------------------------------


def _make_python_codebase_with_edges(root: Path) -> None:
    """Two-file codebase exercising imports + class inheritance + calls."""
    (root / "helpers.py").write_text(
        textwrap.dedent("""
            def normalize(s):
                return s.strip().lower()
        """).strip(),
        encoding="utf-8",
    )
    (root / "workers.py").write_text(
        textwrap.dedent("""
            from helpers import normalize

            class Base:
                def hello(self):
                    print("hi")

            class Worker(Base):
                def run(self, name):
                    return normalize(name)
        """).strip(),
        encoding="utf-8",
    )


def _edges_for_concept(engine: Engine, concept_id: str, kind: EdgeKind):
    return [e for e in engine.edges_for(concept_id) if e.kind == kind]


def _by_name(engine: Engine, name: str, kind: NodeKind):
    return next(c for c in engine.find_by_kind(kind) if c.name == name)


def test_index_creates_extends_edge(engine: Engine, tmp_path: Path):
    code_root = tmp_path / "demo-pkg"
    code_root.mkdir()
    _make_python_codebase_with_edges(code_root)
    src = add_source(engine, code_root)
    index_source(engine, src.id)

    worker = _by_name(engine, "Worker", NodeKind.symbol)
    base = _by_name(engine, "Base", NodeKind.symbol)
    extends = _edges_for_concept(engine, worker.id, EdgeKind.extends)
    assert len(extends) == 1
    assert extends[0].from_id == worker.id
    assert extends[0].to_id == base.id


def test_index_creates_calls_edge(engine: Engine, tmp_path: Path):
    code_root = tmp_path / "demo-pkg"
    code_root.mkdir()
    _make_python_codebase_with_edges(code_root)
    src = add_source(engine, code_root)
    index_source(engine, src.id)

    run = _by_name(engine, "run", NodeKind.symbol)
    normalize = _by_name(engine, "normalize", NodeKind.symbol)
    calls = _edges_for_concept(engine, run.id, EdgeKind.calls)
    assert any(e.from_id == run.id and e.to_id == normalize.id for e in calls)


def test_index_creates_imports_edge(engine: Engine, tmp_path: Path):
    code_root = tmp_path / "demo-pkg"
    code_root.mkdir()
    _make_python_codebase_with_edges(code_root)
    src = add_source(engine, code_root)
    index_source(engine, src.id)

    workers = next(
        c for c in engine.find_by_kind(NodeKind.file)
        if c.name == "workers.py"
    )
    helpers = next(
        c for c in engine.find_by_kind(NodeKind.file)
        if c.name == "helpers.py"
    )
    imports = _edges_for_concept(engine, workers.id, EdgeKind.imports)
    assert any(e.to_id == helpers.id for e in imports)


def test_find_orphans_surfaces_uncalled_symbols(engine: Engine, tmp_path: Path):
    """Symbols with no incoming `calls` / `extends` edges are orphans —
    the typed-graph differentiator vs. fuzzy search."""
    from memex.codebase import find_orphans

    code_root = tmp_path / "demo-pkg"
    code_root.mkdir()
    # `caller` calls `target`; `lonely` is uncalled. `_private` ignored
    # by default.
    (code_root / "main.py").write_text(
        textwrap.dedent("""
            def target():
                return 1

            def caller():
                return target()

            def lonely():
                return 99

            def _private():
                return "private"
        """).strip(),
        encoding="utf-8",
    )
    src = add_source(engine, code_root)
    index_source(engine, src.id)

    orphans = find_orphans(engine, source_id=src.id)
    names = {o.name for o in orphans}
    assert "lonely" in names      # no callers — orphan
    assert "caller" in names      # entry-point — also orphan (no inbound calls)
    assert "target" not in names  # called by `caller` — not orphan
    assert "_private" not in names  # underscore-prefixed excluded by default

    # include_private=True surfaces underscore symbols too.
    with_private = find_orphans(engine, source_id=src.id, include_private=True)
    assert "_private" in {o.name for o in with_private}


# ---- cross-repo linker ------------------------------------------------------


def test_link_cross_repo_creates_same_as_for_matching_symbols(
    engine: Engine, tmp_path: Path
):
    """Two sources defining `JWTClaims` with similar signatures should
    get a `same_as` edge between them after the linker runs."""
    from memex.codebase import link_cross_repo

    repo_a = tmp_path / "auth-service"
    repo_b = tmp_path / "admin-service"
    repo_a.mkdir(); repo_b.mkdir()
    (repo_a / "jwt.py").write_text(
        textwrap.dedent("""
            class JWTClaims:
                def verify(self, token: str) -> bool:
                    return True
        """).strip(),
        encoding="utf-8",
    )
    (repo_b / "auth.py").write_text(
        textwrap.dedent("""
            class JWTClaims:
                def verify(self, token: str) -> bool:
                    return True
        """).strip(),
        encoding="utf-8",
    )
    src_a = add_source(engine, repo_a, name="auth-service")
    src_b = add_source(engine, repo_b, name="admin-service")
    index_source(engine, src_a.id)
    index_source(engine, src_b.id)

    result = link_cross_repo(engine)
    # JWTClaims (class) and verify (method) both qualify since their
    # names are >= 4 chars and not in the generic-noise list.
    assert any(p.a_name == "JWTClaims" for p in result.pairs)
    # The same_as edge exists in the graph.
    a_class = next(c for c in engine.find_by_kind(NodeKind.symbol)
                   if c.name == "JWTClaims" and c.metadata["source_id"] == src_a.id)
    same_edges = [e for e in engine.edges_for(a_class.id) if e.kind == EdgeKind.same_as]
    assert len(same_edges) >= 1


def test_link_cross_repo_skips_generic_names(engine: Engine, tmp_path: Path):
    """Names like `parse` shouldn't auto-link across services."""
    from memex.codebase import link_cross_repo

    repo_a = tmp_path / "svc-a"; repo_b = tmp_path / "svc-b"
    repo_a.mkdir(); repo_b.mkdir()
    (repo_a / "x.py").write_text("def parse(s): return s\n", encoding="utf-8")
    (repo_b / "x.py").write_text("def parse(s): return s\n", encoding="utf-8")
    src_a = add_source(engine, repo_a, name="svc-a")
    src_b = add_source(engine, repo_b, name="svc-b")
    index_source(engine, src_a.id)
    index_source(engine, src_b.id)

    result = link_cross_repo(engine)
    assert all(p.a_name != "parse" for p in result.pairs)
    assert result.skipped_generic >= 2


def test_link_cross_repo_dry_run_writes_no_edges(engine: Engine, tmp_path: Path):
    from memex.codebase import link_cross_repo

    repo_a = tmp_path / "a"; repo_b = tmp_path / "b"
    repo_a.mkdir(); repo_b.mkdir()
    (repo_a / "core.py").write_text(
        "class TenantConfig:\n    def hydrate(self, slug): pass\n", encoding="utf-8"
    )
    (repo_b / "core.py").write_text(
        "class TenantConfig:\n    def hydrate(self, slug): pass\n", encoding="utf-8"
    )
    src_a = add_source(engine, repo_a)
    src_b = add_source(engine, repo_b)
    index_source(engine, src_a.id)
    index_source(engine, src_b.id)

    result = link_cross_repo(engine, dry_run=True)
    assert len(result.pairs) >= 1
    # No same_as edges actually written.
    a_cls = next(c for c in engine.find_by_kind(NodeKind.symbol)
                 if c.name == "TenantConfig" and c.metadata["source_id"] == src_a.id)
    same_edges = [e for e in engine.edges_for(a_cls.id) if e.kind == EdgeKind.same_as]
    assert same_edges == []


# ---- docstrings + annotations ------------------------------------------------


def test_python_docstring_and_decorator_extraction(engine: Engine, tmp_path: Path):
    code_root = tmp_path / "demo-pkg"
    code_root.mkdir()
    (code_root / "api.py").write_text(
        textwrap.dedent('''
            from fastapi import APIRouter
            router = APIRouter()

            @router.post("/v1/auth/login")
            def login(payload):
                """Authenticate a user and return a JWT token."""
                return {"ok": True}
        ''').strip(),
        encoding="utf-8",
    )
    src = add_source(engine, code_root)
    index_source(engine, src.id)

    login = _by_name(engine, "login", NodeKind.symbol)
    assert login.metadata["docstring"] is not None
    assert "Authenticate a user" in login.metadata["docstring"]
    annot_names = login.metadata["annotation_names"]
    assert any("router.post" in n for n in annot_names)
    annots = login.metadata["annotations"]
    assert any('"/v1/auth/login"' in a["args"] for a in annots)


def test_java_javadoc_and_annotation_extraction(engine: Engine, tmp_path: Path):
    """Java JavaDoc + @RestController-style annotations land on the symbol."""
    code_root = tmp_path / "demo-pkg"
    code_root.mkdir()
    (code_root / "Greeter.java").write_text(
        textwrap.dedent("""
            package com.example;

            /**
             * Greets users by name.
             */
            @RestController
            public class Greeter {
                /** Says hello. */
                @GetMapping("/hello")
                public String hello() {
                    return "hi";
                }
            }
        """).strip(),
        encoding="utf-8",
    )
    src = add_source(engine, code_root)
    index_source(engine, src.id)

    greeter = _by_name(engine, "Greeter", NodeKind.symbol)
    assert greeter.metadata["docstring"] is not None
    assert "Greets users by name" in greeter.metadata["docstring"]
    assert "RestController" in greeter.metadata["annotation_names"]

    hello = _by_name(engine, "hello", NodeKind.symbol)
    assert hello.metadata["docstring"] is not None
    assert "Says hello" in hello.metadata["docstring"]
    assert "GetMapping" in hello.metadata["annotation_names"]


def test_unresolved_calls_dont_pollute_graph(engine: Engine, tmp_path: Path):
    """Calls to external/builtin names (e.g. `print`, `len`) shouldn't
    create dangling edges — they're left for cross-repo linker pass."""
    code_root = tmp_path / "demo-pkg"
    code_root.mkdir()
    (code_root / "only.py").write_text(
        textwrap.dedent("""
            def shout(s):
                return s.upper()
        """).strip(),
        encoding="utf-8",
    )
    src = add_source(engine, code_root)
    index_source(engine, src.id)

    shout = _by_name(engine, "shout", NodeKind.symbol)
    calls = _edges_for_concept(engine, shout.id, EdgeKind.calls)
    # `upper` isn't a symbol in the source — no edge created.
    assert calls == []


