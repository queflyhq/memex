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

    `calls`, `extends`, and the file-level `imports` are RAW textual names
    captured at chunk time — the indexer resolves them against the rest of
    the source's symbol set to create concrete `EdgeKind.calls` /
    `EdgeKind.extends` / `EdgeKind.imports` edges. Names that don't resolve
    (external libraries, builtins) are left dangling so cross-repo linker
    passes (slice B) can pick them up later.
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
    # Raw reference names — resolved to edges by the indexer.
    calls: list[str] = field(default_factory=list)        # identifier names invoked in body
    extends: list[str] = field(default_factory=list)      # base class / interface names
    # Docs + annotations — captured at chunk time for "full picture" recall.
    docstring: str | None = None  # docstring / JavaDoc / JSDoc / godoc body
    annotations: list[dict[str, Any]] = field(default_factory=list)
    # ^ each annotation: {name: str, args: str, line: int}
    #   covers Python decorators, Java annotations, TS decorators


@dataclass(slots=True)
class FileImports:
    """File-level imports — feeds `EdgeKind.imports` edges in the indexer.

    Each entry is the raw module/file name as it appeared in the source
    (`import os.path` → `"os.path"`; `from foo.bar import baz` → `"foo.bar"`;
    Go `"github.com/x/y"` → `"github.com/x/y"`). The indexer resolves
    relative imports to in-source files and external imports stay dangling.
    """

    file_path: str
    raw_imports: list[str] = field(default_factory=list)


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
    # Per-language reference extractors — produce raw identifier names
    # that the indexer resolves to edges. Each takes (declaration_node,
    # source_bytes) and returns a list of names. None = no extraction
    # (slice A.0 fallback for languages we haven't taught yet).
    extract_calls: Callable[[Any, bytes], list[str]] | None = None
    extract_extends: Callable[[Any, bytes], list[str]] | None = None
    # File-level — takes (root_node, source_bytes) returning module names
    # imported by this file. Slice A.1 wires these into `imports` edges.
    extract_file_imports: Callable[[Any, bytes], list[str]] | None = None
    # Doc + annotation extractors — pulled per-declaration. Output goes
    # into SymbolChunk.docstring / .annotations.
    extract_docstring: Callable[[Any, bytes], str | None] | None = None
    extract_annotations: Callable[[Any, bytes], list[dict[str, Any]]] | None = None


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


# ---- Reference extractors (per language) ------------------------------------


def _node_text(node: Any, source: bytes) -> str:
    return source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _walk_for_node_types(node: Any, types: frozenset[str]) -> list[Any]:
    """Iterative DFS collecting every descendant whose `.type` is in `types`."""
    out: list[Any] = []
    stack = [node]
    while stack:
        n = stack.pop()
        if n.type in types:
            out.append(n)
        # Walk children in reverse so output is left-to-right.
        for c in reversed(n.children):
            stack.append(c)
    return out


# --- Python ---


def _python_extract_calls(node: Any, source: bytes) -> list[str]:
    """Identifiers invoked inside a function/method body. Returns simple
    names (`foo`) and dotted heads (`obj.method` → `method`) — the indexer
    resolves these against in-source symbol names; unresolved stay
    dangling and feed cross-repo linker hints."""
    calls: list[str] = []
    seen: set[str] = set()
    body = node.child_by_field_name("body")
    if body is None:
        return calls
    for call in _walk_for_node_types(body, frozenset({"call"})):
        fn = call.child_by_field_name("function")
        if fn is None:
            continue
        if fn.type == "identifier":
            name = _node_text(fn, source)
        elif fn.type == "attribute":
            attr = fn.child_by_field_name("attribute")
            name = _node_text(attr, source) if attr else _node_text(fn, source)
        else:
            name = _node_text(fn, source).split(".")[-1]
        if name and name not in seen:
            seen.add(name)
            calls.append(name)
    return calls


def _python_extract_extends(node: Any, source: bytes) -> list[str]:
    """Class superclasses — Python `class Foo(Bar, Mixin):`."""
    out: list[str] = []
    sclasses = node.child_by_field_name("superclasses")
    if sclasses is None:
        return out
    for child in sclasses.children:
        if child.type == "identifier":
            out.append(_node_text(child, source))
        elif child.type == "attribute":
            out.append(_node_text(child, source).split(".")[-1])
    return out


def _python_extract_imports(root: Any, source: bytes) -> list[str]:
    """Module-level imports: `import x`, `from x import y` → ["x"]."""
    out: list[str] = []
    seen: set[str] = set()
    for child in root.children:
        if child.type == "import_statement":
            # `import x.y` or `import x as a`
            for n in child.children:
                if n.type == "dotted_name":
                    name = _node_text(n, source)
                    if name and name not in seen:
                        seen.add(name); out.append(name)
        elif child.type == "import_from_statement":
            mod = child.child_by_field_name("module_name")
            if mod is not None:
                name = _node_text(mod, source)
                if name and name not in seen:
                    seen.add(name); out.append(name)
    return out


# --- Go ---


def _go_extract_calls(node: Any, source: bytes) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    body = node.child_by_field_name("body")
    if body is None:
        return out
    for call in _walk_for_node_types(body, frozenset({"call_expression"})):
        fn = call.child_by_field_name("function")
        if fn is None:
            continue
        if fn.type == "identifier":
            name = _node_text(fn, source)
        elif fn.type == "selector_expression":
            field = fn.child_by_field_name("field")
            name = _node_text(field, source) if field else _node_text(fn, source).split(".")[-1]
        else:
            name = _node_text(fn, source).split(".")[-1]
        if name and name not in seen:
            seen.add(name); out.append(name)
    return out


def _go_extract_imports(root: Any, source: bytes) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for spec in _walk_for_node_types(root, frozenset({"import_spec"})):
        path_node = spec.child_by_field_name("path")
        if path_node is None:
            for c in spec.children:
                if c.type == "interpreted_string_literal":
                    path_node = c
                    break
        if path_node is None:
            continue
        text = _node_text(path_node, source).strip('"')
        if text and text not in seen:
            seen.add(text); out.append(text)
    return out


# --- JS / TS ---


def _js_extract_calls(node: Any, source: bytes) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    body = node.child_by_field_name("body")
    if body is None:
        return out
    for call in _walk_for_node_types(body, frozenset({"call_expression"})):
        fn = call.child_by_field_name("function")
        if fn is None:
            continue
        if fn.type == "identifier":
            name = _node_text(fn, source)
        elif fn.type == "member_expression":
            prop = fn.child_by_field_name("property")
            name = _node_text(prop, source) if prop else _node_text(fn, source).split(".")[-1]
        else:
            name = _node_text(fn, source).split(".")[-1]
        if name and name not in seen:
            seen.add(name); out.append(name)
    return out


def _js_extract_extends(node: Any, source: bytes) -> list[str]:
    out: list[str] = []
    for child in node.children:
        if child.type == "class_heritage":
            for n in child.children:
                if n.type == "identifier":
                    out.append(_node_text(n, source))
                elif n.type in ("member_expression", "type_identifier"):
                    out.append(_node_text(n, source).split(".")[-1])
    return out


def _js_extract_imports(root: Any, source: bytes) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for stmt in _walk_for_node_types(root, frozenset({"import_statement"})):
        src_node = stmt.child_by_field_name("source")
        if src_node is None:
            for c in stmt.children:
                if c.type == "string":
                    src_node = c; break
        if src_node is None:
            continue
        text = _node_text(src_node, source).strip("'\"")
        if text and text not in seen:
            seen.add(text); out.append(text)
    return out


# --- Java ---


def _java_extract_calls(node: Any, source: bytes) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    body = node.child_by_field_name("body")
    if body is None:
        return out
    for call in _walk_for_node_types(body, frozenset({"method_invocation"})):
        name_node = call.child_by_field_name("name")
        if name_node is None:
            continue
        name = _node_text(name_node, source)
        if name and name not in seen:
            seen.add(name); out.append(name)
    return out


def _java_extract_extends(node: Any, source: bytes) -> list[str]:
    out: list[str] = []
    super_node = node.child_by_field_name("superclass")
    if super_node is not None:
        for c in super_node.children:
            if c.type == "type_identifier":
                out.append(_node_text(c, source))
    iface_node = node.child_by_field_name("interfaces")
    if iface_node is not None:
        for c in _walk_for_node_types(iface_node, frozenset({"type_identifier"})):
            out.append(_node_text(c, source))
    return out


# --- Docstring / annotation extractors ---------------------------------------


def _strip_doc_markers(text: str) -> str:
    """Clean a raw doc string: strip Python triple-quotes, JSDoc / JavaDoc
    /** ... */ delimiters, leading * line markers, and excess whitespace.
    Returns one logical doc body."""
    s = text.strip()
    # Triple-quoted Python
    for q in ('"""', "'''"):
        if s.startswith(q) and s.endswith(q) and len(s) >= 6:
            s = s[3:-3]
            break
    # JSDoc / JavaDoc: /** ... */
    if s.startswith("/**") and s.endswith("*/"):
        s = s[3:-2]
    elif s.startswith("/*") and s.endswith("*/"):
        s = s[2:-2]
    # Strip leading-* lines (JSDoc/JavaDoc convention).
    lines = []
    for line in s.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("* "):
            lines.append(stripped[2:])
        elif stripped == "*":
            lines.append("")
        elif stripped.startswith("///") or stripped.startswith("//"):
            # Go-style godoc and C# doc comments.
            after = stripped.lstrip("/").strip()
            lines.append(after)
        elif stripped.startswith("#"):
            # Bash / Python comment line within a comment block.
            lines.append(stripped.lstrip("#").strip())
        else:
            lines.append(line)
    out = "\n".join(lines).strip()
    # Collapse 3+ blank lines to 2.
    while "\n\n\n" in out:
        out = out.replace("\n\n\n", "\n\n")
    return out


def _python_extract_docstring(node: Any, source: bytes) -> str | None:
    """Python docstring: first string literal in the function/class body."""
    body = node.child_by_field_name("body")
    if body is None:
        return None
    for child in body.children:
        if child.type != "expression_statement":
            continue
        for sub in child.children:
            if sub.type == "string":
                raw = _node_text(sub, source)
                cleaned = _strip_doc_markers(raw)
                return cleaned or None
        # Stop after the first non-comment statement.
        return None
    return None


def _python_extract_annotations(node: Any, source: bytes) -> list[dict[str, Any]]:
    """Python decorators — siblings before the def/class via decorated_definition,
    or `decorator` children inside the wrapping node."""
    out: list[dict[str, Any]] = []
    parent = node.parent
    candidates: list[Any] = []
    if parent is not None and parent.type == "decorated_definition":
        for sib in parent.children:
            if sib.type == "decorator":
                candidates.append(sib)
    for dec in candidates:
        # decorator → @ + (identifier | call | attribute)
        body = None
        for c in dec.children:
            if c.type in ("identifier", "attribute", "call"):
                body = c
                break
        if body is None:
            continue
        if body.type == "call":
            fn = body.child_by_field_name("function")
            name = _node_text(fn, source) if fn else _node_text(body, source)
            args_node = body.child_by_field_name("arguments")
            args = _node_text(args_node, source) if args_node else "()"
        else:
            name = _node_text(body, source)
            args = ""
        out.append({
            "name": name,
            "args": args,
            "line": dec.start_point[0] + 1,
        })
    return out


def _go_extract_docstring(node: Any, source: bytes) -> str | None:
    """Godoc convention: comments immediately preceding the declaration."""
    lines: list[str] = []
    sib = node.prev_named_sibling
    while sib is not None and sib.type == "comment":
        # Walk backwards collecting contiguous comments.
        lines.insert(0, _node_text(sib, source))
        sib = sib.prev_named_sibling
    if not lines:
        return None
    return _strip_doc_markers("\n".join(lines)) or None


def _js_extract_docstring(node: Any, source: bytes) -> str | None:
    """JSDoc: a /** ... */ block immediately preceding the declaration."""
    sib = node.prev_named_sibling
    if sib is None or sib.type != "comment":
        return None
    text = _node_text(sib, source)
    if not text.startswith("/**"):
        return None
    return _strip_doc_markers(text) or None


def _js_extract_annotations(node: Any, source: bytes) -> list[dict[str, Any]]:
    """TS decorators: `decorator` nodes inside the declaration."""
    out: list[dict[str, Any]] = []
    for child in node.children:
        if child.type != "decorator":
            continue
        text = _node_text(child, source).lstrip("@").strip()
        # `@Injectable()` → name="Injectable", args="()"
        if "(" in text:
            name, _, rest = text.partition("(")
            args = "(" + rest
        else:
            name, args = text, ""
        out.append({"name": name.strip(), "args": args.strip(), "line": child.start_point[0] + 1})
    return out


def _java_extract_docstring(node: Any, source: bytes) -> str | None:
    """JavaDoc: /** ... */ immediately preceding declaration. Tree-sitter
    Java emits these as `block_comment` siblings."""
    sib = node.prev_named_sibling
    if sib is None:
        return None
    if sib.type not in ("block_comment", "comment"):
        return None
    text = _node_text(sib, source)
    if not text.startswith("/**"):
        return None
    return _strip_doc_markers(text) or None


def _java_extract_annotations(node: Any, source: bytes) -> list[dict[str, Any]]:
    """Java annotations: marker_annotation, annotation in modifiers field."""
    out: list[dict[str, Any]] = []
    modifiers = node.child_by_field_name("modifiers")
    if modifiers is None:
        for c in node.children:
            if c.type == "modifiers":
                modifiers = c
                break
    if modifiers is None:
        return out
    for child in modifiers.children:
        if child.type not in ("marker_annotation", "annotation"):
            continue
        name_node = child.child_by_field_name("name")
        name = _node_text(name_node, source) if name_node else ""
        args_node = child.child_by_field_name("arguments")
        args = _node_text(args_node, source) if args_node else ""
        if name:
            out.append({"name": name, "args": args, "line": child.start_point[0] + 1})
    return out


def _java_extract_imports(root: Any, source: bytes) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for child in root.children:
        if child.type != "import_declaration":
            continue
        text = _node_text(child, source).strip()
        # Strip leading "import" + trailing ";"
        text = text.removeprefix("import").strip().rstrip(";").strip()
        text = text.removeprefix("static").strip()
        if text and text not in seen:
            seen.add(text); out.append(text)
    return out


# Per-language rule tables. Languages absent from this dict fall back to
# whole-file chunking (one symbol per file).
_RULES: dict[str, LanguageRules] = {
    "python": LanguageRules(
        name="python",
        symbol_node_types={
            "class_definition": "class",
            "function_definition": "function",  # promoted to "method" when inside a class
        },
        extract_calls=_python_extract_calls,
        extract_extends=_python_extract_extends,
        extract_file_imports=_python_extract_imports,
        extract_docstring=_python_extract_docstring,
        extract_annotations=_python_extract_annotations,
    ),
    "go": LanguageRules(
        name="go",
        symbol_node_types={
            "function_declaration": "function",
            "method_declaration": "method",
            "type_declaration": "type_alias",  # struct/interface differentiated below
        },
        extract_calls=_go_extract_calls,
        # Go doesn't have classical inheritance — embedded interfaces are
        # handled by the linker's structural-equivalence pass, not here.
        extract_extends=None,
        extract_file_imports=_go_extract_imports,
        extract_docstring=_go_extract_docstring,
        # Go has no annotations; struct tags surface via `metadata` if needed.
        extract_annotations=None,
    ),
    "javascript": LanguageRules(
        name="javascript",
        symbol_node_types={
            "function_declaration": "function",
            "class_declaration": "class",
            "method_definition": "method",
            "lexical_declaration": "const",  # filtered to functions/classes only below
        },
        extract_calls=_js_extract_calls,
        extract_extends=_js_extract_extends,
        extract_file_imports=_js_extract_imports,
        extract_docstring=_js_extract_docstring,
        # JS-no-decorators — most TS decorators land via the typescript rules.
        extract_annotations=None,
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
        extract_calls=_js_extract_calls,
        extract_extends=_js_extract_extends,
        extract_file_imports=_js_extract_imports,
        extract_docstring=_js_extract_docstring,
        extract_annotations=_js_extract_annotations,
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
        extract_calls=_java_extract_calls,
        extract_extends=_java_extract_extends,
        extract_file_imports=_java_extract_imports,
        extract_docstring=_java_extract_docstring,
        extract_annotations=_java_extract_annotations,
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


@dataclass(slots=True)
class FileAnalysis:
    """Result of chunking one file: typed symbols plus file-level imports."""

    chunks: list[SymbolChunk]
    imports: list[str]
    language: str | None


def chunk_file(path: str | Path, content: bytes | None = None) -> list[SymbolChunk]:
    """Parse a file and return its typed symbol chunks. See `analyze_file`
    for the richer return shape that includes file-level imports."""
    return analyze_file(path, content).chunks


def analyze_file(path: str | Path, content: bytes | None = None) -> FileAnalysis:
    """Parse a source file and return its typed symbol chunks + file-level
    imports. The single entry point preferred by the indexer.

    Returns an empty result if the language is unsupported or the parser
    fails. A parsing failure is logged but not raised — codebase indexing
    must keep going across thousands of files.
    """
    p = Path(path)
    language = detect_language(p)
    if language is None:
        return FileAnalysis(chunks=[], imports=[], language=None)
    if content is None:
        try:
            content = p.read_bytes()
        except OSError as e:
            log.warning("read %s failed: %s", p, e)
            return FileAnalysis(chunks=[], imports=[], language=language)

    # Custom extractors (YAML, Svelte) bypass the AST walk.
    if language in _CUSTOM_EXTRACTORS:
        chunks = _CUSTOM_EXTRACTORS[language](p, content, language)
        return FileAnalysis(chunks=chunks, imports=[], language=language)

    rules = _RULES.get(language)
    if rules is None:
        # Fallback: file-as-one-chunk (lets unsupported langs still appear
        # in recall — they just don't have rich symbol structure).
        text = content.decode("utf-8", errors="replace")
        chunks = [
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
        return FileAnalysis(chunks=chunks, imports=[], language=language)

    parser = _get_parser(language)
    if parser is None:
        return FileAnalysis(chunks=[], imports=[], language=language)
    try:
        tree = parser.parse(content)
    except Exception as e:  # noqa: BLE001
        log.warning("tree-sitter parse %s failed: %s", p, e)
        return FileAnalysis(chunks=[], imports=[], language=language)

    chunks_out: list[SymbolChunk] = []
    _walk(tree.root_node, content, rules, parent_name=None, parent_kind=None,
          out=chunks_out)

    file_imports: list[str] = []
    if rules.extract_file_imports is not None:
        try:
            file_imports = rules.extract_file_imports(tree.root_node, content)
        except Exception as e:  # noqa: BLE001
            log.warning("import extraction %s failed: %s", p, e)

    return FileAnalysis(chunks=chunks_out, imports=file_imports, language=language)


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
                # Per-language reference extraction. Calls live on
                # function/method bodies; extends on class/interface
                # declarations. Failures are logged + tolerated — a bad
                # extractor must not abort indexing.
                calls_out: list[str] = []
                extends_out: list[str] = []
                docstring_out: str | None = None
                annotations_out: list[dict[str, Any]] = []
                if rules.extract_calls is not None and sym_kind in (
                    "function", "method"
                ):
                    try:
                        calls_out = rules.extract_calls(node, source)
                    except Exception as e:  # noqa: BLE001
                        log.warning("calls extraction failed on %s: %s", name, e)
                if rules.extract_extends is not None and sym_kind in (
                    "class", "interface"
                ):
                    try:
                        extends_out = rules.extract_extends(node, source)
                    except Exception as e:  # noqa: BLE001
                        log.warning("extends extraction failed on %s: %s", name, e)
                if rules.extract_docstring is not None:
                    try:
                        docstring_out = rules.extract_docstring(node, source)
                    except Exception as e:  # noqa: BLE001
                        log.warning("docstring extraction failed on %s: %s", name, e)
                if rules.extract_annotations is not None:
                    try:
                        annotations_out = rules.extract_annotations(node, source)
                    except Exception as e:  # noqa: BLE001
                        log.warning("annotation extraction failed on %s: %s", name, e)
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
                        calls=calls_out,
                        extends=extends_out,
                        docstring=docstring_out,
                        annotations=annotations_out,
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
