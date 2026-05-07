"""Codebase memory — typed-graph indexing of source repositories.

This package builds memex's "second brain for the architecture" surface:
every indexed repo becomes a typed graph of `source` → `file` → `symbol`
nodes connected by explicit edges (`defined_in`, `part_of`, `calls`,
`extends`, `imports`, `same_as`). Cross-repo `same_as` edges (added by
the linker pass in slice B) fuse logically-equivalent symbols across
microservices into one navigable structure.

This is NOT a vector-RAG index. Embedding is one signal among many;
the primary surface is the typed graph, queryable by symbol name +
signature + edge traversal. See the
"memex codebase memory is NOT another fuzzy/RAG search" constraint
in the memex graph for the design rationale.
"""

from memex.codebase.detect import detect_language, supported_languages
from memex.codebase.indexer import index_source, reindex_file, reindex_source
from memex.codebase.linker import link_cross_repo
from memex.codebase.recall import find_orphans, recall_code
from memex.codebase.sources import (
    add_source,
    get_source,
    list_sources,
    remove_source,
)

__all__ = [
    "add_source",
    "detect_language",
    "find_orphans",
    "get_source",
    "index_source",
    "link_cross_repo",
    "list_sources",
    "recall_code",
    "reindex_file",
    "reindex_source",
    "remove_source",
    "supported_languages",
]
