"""Language detection for codebase memory.

Slice A: extension-based dispatch. Content-sniffing (shebangs, modelines)
can be added later if extension is ambiguous.

Each entry pairs a file extension with the tree-sitter language module
that exposes a `language()` callable returning the parser pointer.
Languages beyond Python are loaded lazily so the base install only
requires `tree-sitter-python` — see pyproject.toml `code-*` extras.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class LanguageSpec:
    """One supported language and the tree-sitter module that parses it."""

    name: str           # canonical key: "python", "go", "typescript", ...
    extensions: tuple[str, ...]
    module: str         # importable name, e.g. "tree_sitter_python"


# Order matters only when extensions conflict (none today).
# Baseline ship in the base install (see pyproject.toml dependencies):
#   Languages: Python, Go, JavaScript, TypeScript, Java
#   Configs / scripting: Bash, YAML (K8s manifests), HCL (Terraform), Svelte
# Additional languages (Rust, C++, C#, Ruby) ship as `code-<lang>` extras
# and are loaded lazily — `supported_languages()` reports what's importable.
_LANGUAGES: tuple[LanguageSpec, ...] = (
    LanguageSpec("python", (".py", ".pyi"), "tree_sitter_python"),
    LanguageSpec("go", (".go",), "tree_sitter_go"),
    LanguageSpec("typescript", (".ts", ".tsx"), "tree_sitter_typescript"),
    LanguageSpec("javascript", (".js", ".jsx", ".mjs", ".cjs"), "tree_sitter_javascript"),
    LanguageSpec("java", (".java",), "tree_sitter_java"),
    LanguageSpec("bash", (".sh", ".bash"), "tree_sitter_bash"),
    LanguageSpec("yaml", (".yaml", ".yml"), "tree_sitter_yaml"),
    LanguageSpec("hcl", (".tf", ".hcl", ".tfvars"), "tree_sitter_hcl"),
    LanguageSpec("svelte", (".svelte",), "tree_sitter_svelte"),
    LanguageSpec("rust", (".rs",), "tree_sitter_rust"),
    LanguageSpec("cpp", (".cpp", ".cc", ".cxx", ".hpp", ".h"), "tree_sitter_cpp"),
    LanguageSpec("csharp", (".cs",), "tree_sitter_c_sharp"),
    LanguageSpec("ruby", (".rb",), "tree_sitter_ruby"),
)

_BY_EXTENSION: dict[str, LanguageSpec] = {
    ext: spec for spec in _LANGUAGES for ext in spec.extensions
}


def detect_language(path: str | Path) -> str | None:
    """Return the canonical language name for a path, or None if unsupported."""
    ext = Path(path).suffix.lower()
    spec = _BY_EXTENSION.get(ext)
    return spec.name if spec else None


def supported_languages() -> list[str]:
    """List languages whose tree-sitter module is importable in this env."""
    available: list[str] = []
    for spec in _LANGUAGES:
        try:
            importlib.import_module(spec.module)
        except ImportError:
            continue
        available.append(spec.name)
    return available


def get_language_spec(name: str) -> LanguageSpec | None:
    for spec in _LANGUAGES:
        if spec.name == name:
            return spec
    return None
