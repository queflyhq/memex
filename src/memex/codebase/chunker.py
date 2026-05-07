"""Tree-sitter-driven typed-graph chunker.

Reads a source file and emits typed symbol chunks (class, function, method,
struct, interface, …). The output is consumed by `indexer.py` which stores
each chunk as a `kind=symbol` Concept linked to its parent file via
`defined_in` and to its parent symbol (for methods) via `part_of`.

This is NOT a text-chunker. We don't slice files into N-line windows and
embed them. Every chunk is a typed AST declaration with a name, a
signature, and explicit relations. See the
`memex codebase memory is NOT another fuzzy/RAG search` constraint for the
design rationale.

Per-language config drives extraction: each entry maps tree-sitter node
types to memex symbol kinds. Most modern grammars expose a `name` field
on declaration nodes, so the default name extractor (`child_by_field_name`)
works for the vast majority. Languages with quirks (YAML K8s manifests,
Svelte SFCs) override the extractor.
"""

from __future__ import annotations

import importlib
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from memex.codebase.detect import detect_language, get_language_spec

log = logging.getLogger(__name__)


# ---- Symbol record -----------------------------------------------------------


@dataclass(slots=True)
class SymbolChunk:
    """One typed AST declaration extracted from a source file.

    Stored downstream as `Concept(kind="symbol")` with these fields landing
    in `metadata`. The `body` is also embedded into the vector store for
    similarity-based ranking, but the primary surface is the typed graph.
    """

    name: str
    symbol_kind: str  # class|function|method|interface|struct|enum|const|...
    signature: str
    start_line: int        # 1-indexed
    end_line: int          # 1-indexed (inclusive)
    body: str
    parent_symbol: str | None = None  # name of parent class/struct, if nested
    language: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


# ---- Per-language rules ------------------------------------------------------


# Canonical memex symbol kinds. Keep this list small — the typed-graph
# pitch breaks if we proliferate kinds. Map per-language node types to one
# of these.
_SYMBOL_KINDS = frozenset({
    "class", "function", "method", "interface", "struct",
    "enum", "type_alias", "const", "variable", "component",
    "resource", "module_block", "manifest", "task", "test",
})


@dataclass(slots=True)
class LanguageRules:
    name: str
    # tree-sitter node type -> memex symbol kind (must be in _SYMBOL_KINDS).
    # When the same node type can be a class member or top-level (e.g.
    # function_definition is method-when-inside-class, function-when-not),
    # the default rule applies; the recursive walk passes parent context.
    symbol_node_types: dict[str, str]
    # Optional override for name extraction. Default uses field name `name`.
    extract_name: Callable[[Any, bytes], str | None] | None = None
    # Optional override for signature extraction (the line(s) up to body).
    extract_signature: Callable[[Any, bytes], str] | None = None


def _default_extract_name(node: Any, source: bytes) -> str | None:
    """Most tree-sitter grammars expose a `name` field on declarations."""
    n = node.child_by_field_name("name")
    if n is None:
        # Some node types use `identifier` as a direct child.
        for child in node.children:
            if child.type in ("identifier", "type_identifier", "name"):
                return source[child.start_byte:child.end_byte].decode(
                    "utf-8", errors="replace"
                )
        return None
    return source[n.start_byte:n.end_byte].decode("utf-8", errors="replace")


def _default_extract_signature(node: Any, source: bytes) -> str:
    """Signature = the source up to the start of the body, or first line."""
    body = node.child_by_field_name("body")
    if body is not None:
        sig_bytes = source[node.start_byte:body.start_byte]
    else:
        # No body — take the first line of the node.
        text = source[node.start_byte:node.end_byte].decode(
            "utf-8", errors="replace"
        )
        sig_bytes = text.split("\n", 1)[0].encode("utf-8")
    sig = sig_bytes.decode("utf-8", errors="replace").strip()
    # Collapse whitespace to keep signatures readable.
    return " ".join(sig.split())


# Per-language rule tables. Languages absent from this dict fall back to
# whole-file chunking (one symbol per file).
_RULES: dict[str, LanguageRules] = {
    "python": LanguageRules(
        name="python",
        symbol_node_types={
            "class_definition": "class",
            "function_definition": "function",  # promoted to "method" when inside a class
        },
    ),
    "go": LanguageRules(
        name="go",
        symbol_node_types={
            "function_declaration": "function",
            "method_declaration": "method",
            "type_declaration": "type_alias",  # struct/interface differentiated below
        },
    ),
    "javascript": LanguageRules(
        name="javascript",
        symbol_node_types={
            "function_declaration": "function",
            "class_declaration": "class",
            "method_definition": "method",
            "lexical_declaration": "const",  # filtered to functions/classes only below
        },
    ),
    "typescript": LanguageRules(
        name="typescript",
        symbol_node_types={
            "function_declaration": "function",
            "class_declaration": "class",
            "method_definition": "method",
            "interface_declaration": "interface",
            "type_alias_declaration": "type_alias",
            "enum_declaration": "enum",
            "lexical_declaration": "const",
        },
    ),
    "java": LanguageRules(
        name="java",
        symbol_node_types={
            "class_declaration": "class",
            "interface_declaration": "interface",
            "method_declaration": "method",
            "constructor_declaration": "method",
            "enum_declaration": "enum",
        },
    ),
    "bash": LanguageRules(
        name="bash",
        symbol_node_types={
            "function_definition": "function",
        },
    ),
    "hcl": LanguageRules(
        name="hcl",
        symbol_node_types={
            "block": "module_block",  # resource, data, module, variable, output, etc.
        },
    ),
}


# Languages that need a custom whole-file extractor (no AST walk).
def _yaml_extract(path: Path, content: bytes, language: str) -> list[SymbolChunk]:
    """K8s-aware YAML chunking: one symbol per top-level document, named by
    `kind/metadata.name` when present (matching K8s convention).

    Pure tree-sitter walking of YAML produces a noisy graph (every key is a
    pair node). For codebase memory we want one symbol per K8s resource —
    `Deployment/admin-console` rather than 500 key/value pairs. Implemented
    via a lightweight string scan of `---`-separated documents.
    """
    text = content.decode("utf-8", errors="replace")
    chunks: list[SymbolChunk] = []
    docs = text.split("\n---\n") if "\n---\n" in text else [text]
    line_offset = 1
    for doc in docs:
        lines = doc.splitlines()
        kind_val: str | None = None
        name_val: str | None = None
        for line in lines[:30]:  # K8s manifests have kind+name in the first ~20 lines
            stripped = line.strip()
            if stripped.startswith("kind:"):
                kind_val = stripped.split(":", 1)[1].strip().strip('"\'')
            elif stripped.startswith("name:") and not name_val:
                name_val = stripped.split(":", 1)[1].strip().strip('"\'')
            if kind_val and name_val:
                break
        if kind_val:
            symbol_name = f"{kind_val}/{name_val}" if name_val else kind_val
            kind_label = "manifest"
        else:
            symbol_name = path.stem
            kind_label = "manifest"
        chunks.append(
            SymbolChunk(
                name=symbol_name,
                symbol_kind=kind_label,
                signature=lines[0] if lines else "",
                start_line=line_offset,
                end_line=line_offset + len(lines) - 1,
                body=doc,
                language=language,
                metadata={"k8s_kind": kind_val} if kind_val else {},
            )
        )
        line_offset += len(lines) + 1  # +1 for the `---` separator
    return chunks


def _svelte_extract(path: Path, content: bytes, language: str) -> list[SymbolChunk]:
    """Svelte SFC: emit one component symbol per file (named by basename).

    Slice A keeps Svelte simple. The script-section JS/TS extraction is a
    follow-up — needs a sub-language pass over the `<script>` block.
    """
    text = content.decode("utf-8", errors="replace")
    return [
        SymbolChunk(
            name=path.stem,
            symbol_kind="component",
            signature=f"<{path.stem} component>",
            start_line=1,
            end_line=text.count("\n") + 1,
            body=text,
            language=language,
            metadata={"svelte_sfc": True},
        )
    ]


_CUSTOM_EXTRACTORS: dict[str, Callable[[Path, bytes, str], list[SymbolChunk]]] = {
    "yaml": _yaml_extract,
    "svelte": _svelte_extract,
}


# ---- Tree-sitter parser cache ------------------------------------------------


_PARSER_CACHE: dict[str, Any] = {}


def _get_parser(language: str) -> Any | None:
    """Return a cached tree-sitter Parser for `language`, or None if missing."""
    if language in _PARSER_CACHE:
        return _PARSER_CACHE[language]
    spec = get_language_spec(language)
    if spec is None:
        return None
    try:
        from tree_sitter import Language, Parser
        mod = importlib.import_module(spec.module)
    except ImportError as e:
        log.warning(
            "tree-sitter parser for %r not importable (%s) — skipping",
            language, e,
        )
        return None
    # tree-sitter-typescript exposes both ts and tsx parsers; pick ts by default.
    if hasattr(mod, "language_typescript"):
        ts_lang = Language(mod.language_typescript())
    elif hasattr(mod, "language"):
        ts_lang = Language(mod.language())
    else:
        log.warning("module %s has no language() callable", spec.module)
        return None
    parser = Parser(ts_lang)
    _PARSER_CACHE[language] = parser
    return parser


# ---- Public API --------------------------------------------------------------


def chunk_file(path: str | Path, content: bytes | None = None) -> list[SymbolChunk]:
    """Parse a source file and return its typed symbol chunks.

    Returns an empty list if the language is unsupported or the parser
    fails. A parsing failure is logged but not raised — codebase indexing
    must keep going across thousands of files.
    """
    p = Path(path)
    language = detect_language(p)
    if language is None:
        return []
    if content is None:
        try:
            content = p.read_bytes()
        except OSError as e:
            log.warning("read %s failed: %s", p, e)
            return []

    # Custom extractors (YAML, Svelte) bypass the AST walk.
    if language in _CUSTOM_EXTRACTORS:
        return _CUSTOM_EXTRACTORS[language](p, content, language)

    rules = _RULES.get(language)
    if rules is None:
        # Fallback: file-as-one-chunk (lets unsupported langs still appear
        # in recall — they just don't have rich symbol structure).
        text = content.decode("utf-8", errors="replace")
        return [
            SymbolChunk(
                name=p.stem,
                symbol_kind="function",  # generic — matches "search by name"
                signature=text.split("\n", 1)[0] if text else "",
                start_line=1,
                end_line=text.count("\n") + 1,
                body=text,
                language=language,
            )
        ]

    parser = _get_parser(language)
    if parser is None:
        return []
    try:
        tree = parser.parse(content)
    except Exception as e:  # noqa: BLE001
        log.warning("tree-sitter parse %s failed: %s", p, e)
        return []

    chunks: list[SymbolChunk] = []
    _walk(tree.root_node, content, rules, parent_name=None, parent_kind=None,
          out=chunks)
    return chunks


def _walk(
    node: Any,
    source: bytes,
    rules: LanguageRules,
    parent_name: str | None,
    parent_kind: str | None,
    out: list[SymbolChunk],
) -> None:
    """Recursive AST walk. Emits a chunk when the node type is a known
    declaration; recurses into bodies so methods inside classes get
    captured with `parent_symbol` set."""
    target_kind = rules.symbol_node_types.get(node.type)
    next_parent_name = parent_name
    next_parent_kind = parent_kind

    if target_kind is not None:
        extract_name = rules.extract_name or _default_extract_name
        extract_sig = rules.extract_signature or _default_extract_signature
        name = extract_name(node, source)
        if name:
            # Promote function_definition → "method" when inside a class.
            sym_kind = target_kind
            if sym_kind == "function" and parent_kind == "class":
                sym_kind = "method"
            # Filter JS/TS lexical_declaration to only those binding a
            # function or class — `const x = 1;` shouldn't pollute.
            if node.type == "lexical_declaration":
                if not _is_function_lexical(node, source):
                    target_kind = None  # skip
                    sym_kind = "skip"
            if sym_kind != "skip":
                signature = extract_sig(node, source)
                body_text = source[node.start_byte:node.end_byte].decode(
                    "utf-8", errors="replace"
                )
                out.append(
                    SymbolChunk(
                        name=name,
                        symbol_kind=sym_kind,
                        signature=signature,
                        start_line=node.start_point[0] + 1,
                        end_line=node.end_point[0] + 1,
                        body=body_text,
                        parent_symbol=parent_name,
                        language=rules.name,
                        metadata={"node_type": node.type},
                    )
                )
                # Methods inside this class get `parent_name=name`.
                if sym_kind in ("class", "interface", "struct", "enum"):
                    next_parent_name = name
                    next_parent_kind = sym_kind

    for child in node.children:
        _walk(child, source, rules, next_parent_name, next_parent_kind, out)


def _is_function_lexical(node: Any, source: bytes) -> bool:
    """Detect `const foo = () => …` / `const Bar = function() {}` patterns.

    Without this filter, every `const x = 1;` gets a symbol chunk, which
    pollutes recall with non-callable bindings.
    """
    for child in node.children:
        if child.type != "variable_declarator":
            continue
        value = child.child_by_field_name("value")
        if value is None:
            continue
        if value.type in (
            "arrow_function",
            "function_expression",
            "function",
            "class_expression",
            "class",
        ):
            return True
    return False
