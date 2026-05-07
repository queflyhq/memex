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

from memex.codebase.chunker import SymbolChunk, chunk_file
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


def index_source(
    engine: "Engine",
    source_id: str,
    *,
    progress: bool = False,
) -> IndexResult:
    """Index every supported file under a registered source.

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

    file_count = 0
    symbol_count = 0
    skipped = 0
    by_language: dict[str, int] = {}

    files = _walk_files(root)
    if progress:
        log.info("indexing %d files under %s", len(files), root)

    for path in files:
        language = detect_language(path)
        if language is None:
            skipped += 1
            continue

        try:
            chunks = chunk_file(path)
        except Exception as e:  # noqa: BLE001
            log.warning("chunk %s failed: %s", path, e)
            skipped += 1
            continue

        if not chunks:
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

        # Per-file: symbol concepts + edges. Two-pass: create all symbols
        # for the file first (so parent->child edges resolve), then wire
        # parent_symbol edges using the in-file name map.
        local_symbols: dict[str, Concept] = {}
        for chunk in chunks:
            sym = _store_symbol(engine, chunk, file_concept.id, source_id)
            local_symbols[chunk.name] = sym

        for chunk in chunks:
            if chunk.parent_symbol and chunk.parent_symbol in local_symbols:
                engine.link(
                    from_id=local_symbols[chunk.name].id,
                    to_id=local_symbols[chunk.parent_symbol].id,
                    kind=EdgeKind.part_of,
                    source=SourceActor.agent,
                )

        file_count += 1
        symbol_count += len(chunks)
        by_language[language] = by_language.get(language, 0) + 1

    mark_indexed(engine, source_id, file_count, symbol_count)
    return IndexResult(
        source_id=source_id,
        files_indexed=file_count,
        symbols_indexed=symbol_count,
        languages=by_language,
        skipped_files=skipped,
    )


def _store_symbol(
    engine: "Engine",
    chunk: SymbolChunk,
    file_id: str,
    source_id: str,
) -> Concept:
    description = chunk.signature
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
