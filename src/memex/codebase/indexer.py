"""Index a registered source: walk its tree, chunk every supported file,
store typed symbols + edges in memex.

Edge model (slice A):
  source —(part_of, reverse)→ files     [actually: file —part_of→ source]
  file   —(part_of, reverse)→ symbols   [symbol —part_of→ file]
  symbol —defined_in→ file              [redundant with part_of for queryability]
  symbol —part_of→ parent_symbol        [methods → their class]

Slice A.1 (next): add `imports` (file → file) and `calls` (symbol → symbol).
Slice B: add cross-repo `same_as` linker pass.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from memex.codebase.chunker import SymbolChunk, analyze_file
from memex.codebase.detect import detect_language
from memex.codebase.sources import mark_indexed
from memex.core.schema import Concept, EdgeKind, NodeKind, Source as SourceActor

if TYPE_CHECKING:
    from memex.core.engine import Engine

log = logging.getLogger(__name__)


@dataclass(slots=True)
class IndexResult:
    source_id: str
    files_indexed: int
    symbols_indexed: int
    languages: dict[str, int]   # language → file count
    skipped_files: int           # unsupported language or read errors


# Directories we never walk into.
_IGNORE_DIRS = frozenset({
    ".git", "node_modules", ".venv", "venv", "env", ".env",
    "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    "dist", "build", "target", "out", ".next", ".svelte-kit",
    "vendor", ".idea", ".vscode", ".tox", ".cache",
    "site",  # MkDocs build output
})


def _walk_files(root: Path) -> list[Path]:
    out: list[Path] = []
    for p in root.rglob("*"):
        if p.is_dir():
            continue
        if any(part in _IGNORE_DIRS for part in p.parts):
            continue
        out.append(p)
    return out


@dataclass(slots=True)
class _PendingFile:
    """Per-file state captured in pass 1 so pass 2 can resolve cross-file edges."""
    file_concept: Concept
    rel_path: str
    language: str
    symbols: dict[str, Concept]      # name → concept (last-wins for duplicates)
    chunks: list[SymbolChunk]
    raw_imports: list[str]


def index_source(
    engine: "Engine",
    source_id: str,
    *,
    progress: bool = False,
) -> IndexResult:
    """Index every supported file under a registered source.

    Two-pass over the tree:
      Pass 1 — chunk every file, create file + symbol concepts + the
        always-resolvable edges (defined_in, part_of for methods).
      Pass 2 — resolve raw call/extends/import names against the source-
        wide name index and create `calls`, `extends`, and `imports` edges.

    Pre-condition: the source must already exist (use `add_source` first).
    Idempotency: this call ADDS chunks. To re-index from scratch, call
    `reindex_source` (which deletes old chunks first).
    """
    source = engine.get(source_id)
    if source is None or source.kind != NodeKind.source:
        raise ValueError(f"source {source_id!r} not found or wrong kind")

    root = Path(source.metadata["path"])
    if not root.exists():
        raise FileNotFoundError(f"source path missing: {root}")

    files = _walk_files(root)
    if progress:
        log.info("indexing %d files under %s", len(files), root)

    pending: list[_PendingFile] = []
    skipped = 0
    by_language: dict[str, int] = {}

    # ----- Pass 1: per-file analysis + symbol creation ----------------------
    for path in files:
        language = detect_language(path)
        if language is None:
            skipped += 1
            continue

        try:
            analysis = analyze_file(path)
        except Exception as e:  # noqa: BLE001
            log.warning("analyze %s failed: %s", path, e)
            skipped += 1
            continue

        if not analysis.chunks:
            skipped += 1
            continue

        rel = str(path.relative_to(root)).replace("\\", "/")
        file_concept = engine.add(
            name=rel,
            description=f"{language} file in {source.name}",
            kind=NodeKind.file,
            source=SourceActor.agent,
            metadata={
                "source_id": source_id,
                "language": language,
                "abs_path": str(path),
                "rel_path": rel,
            },
        )
        engine.link(
            from_id=file_concept.id,
            to_id=source_id,
            kind=EdgeKind.part_of,
            source=SourceActor.agent,
        )

        local_symbols: dict[str, Concept] = {}
        for chunk in analysis.chunks:
            sym = _store_symbol(engine, chunk, file_concept.id, source_id)
            local_symbols[chunk.name] = sym

        # Method → parent class via part_of (within the same file).
        for chunk in analysis.chunks:
            if chunk.parent_symbol and chunk.parent_symbol in local_symbols:
                engine.link(
                    from_id=local_symbols[chunk.name].id,
                    to_id=local_symbols[chunk.parent_symbol].id,
                    kind=EdgeKind.part_of,
                    source=SourceActor.agent,
                )

        pending.append(_PendingFile(
            file_concept=file_concept,
            rel_path=rel,
            language=language,
            symbols=local_symbols,
            chunks=analysis.chunks,
            raw_imports=analysis.imports,
        ))
        by_language[language] = by_language.get(language, 0) + 1

    # ----- Pass 2: cross-file edge resolution -------------------------------
    # Build a source-wide name index { symbol_name: [Concept] } so calls/
    # extends/imports resolve to concrete edges.
    by_name: dict[str, list[Concept]] = {}
    by_relpath: dict[str, Concept] = {pf.rel_path: pf.file_concept for pf in pending}
    for pf in pending:
        for chunk in pf.chunks:
            by_name.setdefault(chunk.name, []).append(pf.symbols[chunk.name])

    edges_created = 0
    for pf in pending:
        # File-level imports → `imports` edges between files.
        for raw_import in pf.raw_imports:
            target = _resolve_import_to_file(raw_import, pf, by_relpath)
            if target is None:
                continue
            engine.link(
                from_id=pf.file_concept.id,
                to_id=target.id,
                kind=EdgeKind.imports,
                source=SourceActor.agent,
            )
            edges_created += 1

        # Symbol-level calls + extends.
        for chunk in pf.chunks:
            sym = pf.symbols[chunk.name]
            for callee in chunk.calls:
                if callee == chunk.name:  # skip self-recursion
                    continue
                targets = by_name.get(callee, [])
                if not targets:
                    continue
                # Disambiguate: same-file callee preferred over external.
                same_file = [t for t in targets
                             if t.metadata.get("file_id") == pf.file_concept.id]
                pick = same_file or targets
                for t in pick:
                    if t.id == sym.id:
                        continue
                    engine.link(
                        from_id=sym.id,
                        to_id=t.id,
                        kind=EdgeKind.calls,
                        source=SourceActor.agent,
                    )
                    edges_created += 1
            for parent in chunk.extends:
                targets = by_name.get(parent, [])
                if not targets:
                    continue
                for t in targets:
                    engine.link(
                        from_id=sym.id,
                        to_id=t.id,
                        kind=EdgeKind.extends,
                        source=SourceActor.agent,
                    )
                    edges_created += 1

    file_count = len(pending)
    symbol_count = sum(len(pf.chunks) for pf in pending)
    if progress:
        log.info(
            "indexed %d files / %d symbols / %d resolved edges (calls + imports + extends)",
            file_count, symbol_count, edges_created,
        )

    mark_indexed(engine, source_id, file_count, symbol_count)
    return IndexResult(
        source_id=source_id,
        files_indexed=file_count,
        symbols_indexed=symbol_count,
        languages=by_language,
        skipped_files=skipped,
    )


def _resolve_import_to_file(
    raw_import: str,
    pf: _PendingFile,
    by_relpath: dict[str, Concept],
) -> Concept | None:
    """Map a raw import name to a file Concept in the same source, or None
    if the import is external (third-party / stdlib / unresolvable).

    Heuristics per language:
      python: `foo.bar` → `foo/bar.py` or `foo/bar/__init__.py`
      javascript/typescript: `./foo` → `<dir>/foo.{ts,js,svelte}` (and
        index variants). External `react` etc. return None.
      java: `com.example.Foo` → `com/example/Foo.java`
      go: `github.com/x/y` is external; relative imports rare. Return None.
    """
    if not raw_import:
        return None
    lang = pf.language

    if lang == "python":
        # foo.bar → foo/bar.py | foo/bar/__init__.py
        path_part = raw_import.replace(".", "/")
        for cand in (f"{path_part}.py", f"{path_part}/__init__.py"):
            if cand in by_relpath:
                return by_relpath[cand]
        # Could be a relative import from a deeper package — skip for now.
        return None

    if lang in ("javascript", "typescript"):
        if not raw_import.startswith((".", "/")):
            return None  # external module
        from_dir = "/".join(pf.rel_path.split("/")[:-1])
        # Resolve `./foo` and `../bar/baz` against from_dir.
        rel = _normalize_relative(from_dir, raw_import)
        for ext in (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".svelte",
                    "/index.ts", "/index.tsx", "/index.js", "/index.jsx"):
            cand = f"{rel}{ext}"
            if cand in by_relpath:
                return by_relpath[cand]
        return None

    if lang == "java":
        # com.example.Foo → com/example/Foo.java (drop trailing .* / static)
        clean = raw_import.split(".*")[0].strip()
        path_part = clean.replace(".", "/")
        cand = f"{path_part}.java"
        if cand in by_relpath:
            return by_relpath[cand]
        # Java imports often refer to classes rather than files; the .Foo
        # tail is a class. Try dropping it.
        if "/" in path_part:
            head = path_part.rsplit("/", 1)[0]
            cand = f"{head}.java"
            if cand in by_relpath:
                return by_relpath[cand]
        return None

    if lang == "go":
        # Go imports are package paths; in-source relative imports are
        # uncommon. Skip — slice B's same-package linker handles intra-
        # source Go cohesion.
        return None

    return None


def _normalize_relative(from_dir: str, rel: str) -> str:
    """Normalize a JS/TS relative path like `./foo` or `../bar/baz` against
    a source-relative directory."""
    parts = from_dir.split("/") if from_dir else []
    for seg in rel.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            if parts:
                parts.pop()
            continue
        parts.append(seg)
    return "/".join(parts)


def _store_symbol(
    engine: "Engine",
    chunk: SymbolChunk,
    file_id: str,
    source_id: str,
) -> Concept:
    # Description prefers the docstring (richer recall + embeddings) and
    # falls back to the signature when the symbol has no doc.
    description = chunk.docstring.strip() if chunk.docstring else chunk.signature
    metadata = {
        "source_id": source_id,
        "file_id": file_id,
        "symbol_kind": chunk.symbol_kind,
        "signature": chunk.signature,
        "start_line": chunk.start_line,
        "end_line": chunk.end_line,
        "language": chunk.language,
        "parent_symbol_name": chunk.parent_symbol,
        "body": chunk.body[:8000],  # cap for sane storage; full body re-readable from disk
        # Doc + annotations — populated when the per-language extractor
        # produces them. Empty list / None when not available.
        "docstring": chunk.docstring,
        "annotations": chunk.annotations,
        # Surface annotation names as flat list for quick filtering — e.g.
        # "all symbols with @RestController" without parsing the dicts.
        "annotation_names": [a["name"] for a in chunk.annotations],
        **chunk.metadata,
    }
    sym = engine.add(
        name=chunk.name,
        description=description,
        kind=NodeKind.symbol,
        source=SourceActor.agent,
        metadata=metadata,
    )
    engine.link(
        from_id=sym.id,
        to_id=file_id,
        kind=EdgeKind.defined_in,
        source=SourceActor.agent,
    )
    return sym


def reindex_source(
    engine: "Engine",
    source_id: str,
    *,
    progress: bool = False,
) -> IndexResult:
    """Delete previously-indexed files + symbols for this source, then
    re-index from disk. Use this after the codebase has changed."""
    # Drop existing children — symbols first (lower in the part_of chain).
    for kind in (NodeKind.symbol, NodeKind.file):
        for c in engine.find_by_kind(kind):
            if c.metadata.get("source_id") == source_id:
                engine.delete(c.id)
    return index_source(engine, source_id, progress=progress)
