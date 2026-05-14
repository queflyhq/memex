"""
External-MCP enrichment.

The existing upstream system *proxies* tool calls — an AI tool
connected to memeX can call `linear.list_issues` and the result
flows through. Enrichment turns that around: it pulls *facts* from
upstream MCPs into memeX's own concept graph, so the AI can recall
"the Linear issues we discussed last week" via standard `recall()`,
without having to hit Linear's MCP each time.

Mapping shape — declarative, one entry per source:

    {
      "upstream": "linear",        # name from upstreams.json
      "tool": "list_issues",       # MCP tool to call
      "arguments": {"team": "AI"}, # args dict (literal)
      "items_path": "issues",      # dot-path into result for the list
      "concept": {
        "kind": "task",            # NodeKind to assign
        "name_field": "title",     # field on each item used as concept name
        "desc_field": "description",
        "id_prefix": "linear",     # all minted concepts get id = "{prefix}::{external_id}"
        "id_field": "id"
      }
    }

The mapping is intentionally small. Complex transforms (ETL on the
result) belong in a future v0.2 — for now the assumption is the
upstream returns a list of records with `name`-shaped and
`description`-shaped fields. Items already in memeX (matched by id)
get their `last_confirmed_at` refreshed; items new to memeX become
new Concepts.

Run-time wiring is best-effort — connection failures per upstream
log a warning and skip; the rest proceed.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


@dataclass
class EnrichmentSource:
    upstream: str
    tool: str
    arguments: dict[str, Any]
    items_path: str
    concept_kind: str
    concept_name_field: str
    concept_desc_field: str
    concept_id_prefix: str
    concept_id_field: str

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "EnrichmentSource":
        c = d.get("concept", {})
        return cls(
            upstream=d["upstream"],
            tool=d["tool"],
            arguments=dict(d.get("arguments") or {}),
            items_path=str(d.get("items_path", "")),
            concept_kind=str(c.get("kind", "fact")),
            concept_name_field=str(c.get("name_field", "name")),
            concept_desc_field=str(c.get("desc_field", "description")),
            concept_id_prefix=str(c.get("id_prefix", "ext")),
            concept_id_field=str(c.get("id_field", "id")),
        )


def load_enrichment_config(path: Path) -> list[EnrichmentSource]:
    """Read sources from a JSON file. Returns [] if missing or empty.

    Schema: {"sources": [<EnrichmentSource dict>, ...]}
    """
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        log.warning("enrichment config %s parse failed: %s", path, e)
        return []
    out: list[EnrichmentSource] = []
    for item in raw.get("sources") or []:
        try:
            out.append(EnrichmentSource.from_dict(item))
        except Exception as e:  # noqa: BLE001
            log.warning("enrichment source skipped: %s — %s", item, e)
    return out


def _walk_path(obj: Any, path: str) -> Any:
    """Resolve a dot-path on a nested dict/list. Empty path = identity."""
    if not path:
        return obj
    cur = obj
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return cur


def run_enrichment(
    engine: Any,
    aggregator: Any,
    sources: list[EnrichmentSource],
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Pull from each configured upstream and ingest as concepts.

    Idempotent: re-running upserts by id (`{id_prefix}::{external_id}`).
    Returns counts per source + an aggregate summary.
    """
    from memex.core.schema import NodeKind, Source

    per_source: list[dict[str, Any]] = []
    total_added = 0
    total_refreshed = 0
    total_failed = 0

    for src in sources:
        record: dict[str, Any] = {
            "upstream": src.upstream,
            "tool": src.tool,
            "added": 0,
            "refreshed": 0,
            "failed": 0,
            "error": None,
        }
        try:
            result = aggregator.call(src.upstream, src.tool, src.arguments)
        except Exception as e:  # noqa: BLE001
            record["error"] = f"call failed: {e}"
            log.warning(
                "enrichment %s.%s failed to call: %s",
                src.upstream, src.tool, e,
            )
            total_failed += 1
            per_source.append(record)
            continue

        items = _walk_path(result, src.items_path)
        if not isinstance(items, list):
            record["error"] = (
                f"items_path {src.items_path!r} did not resolve to a list"
            )
            total_failed += 1
            per_source.append(record)
            continue

        try:
            kind = NodeKind(src.concept_kind)
        except ValueError:
            record["error"] = f"unknown concept_kind {src.concept_kind!r}"
            total_failed += 1
            per_source.append(record)
            continue

        for item in items:
            if not isinstance(item, dict):
                continue
            ext_id = str(item.get(src.concept_id_field) or "").strip()
            if not ext_id:
                continue
            concept_id = f"{src.concept_id_prefix}::{ext_id}"
            name = str(item.get(src.concept_name_field) or "")[:120]
            desc = str(item.get(src.concept_desc_field) or "")[:4000]
            if not name:
                continue
            existed = engine.get(concept_id) is not None
            if dry_run:
                record["refreshed" if existed else "added"] += 1
                continue
            try:
                # Engine.put handles upsert by id; we use it directly here
                # because we want a stable external id rather than a hashed
                # internal one.
                from memex.core.schema import Concept as _Concept
                c = _Concept(
                    id=concept_id,
                    name=name,
                    description=desc,
                    kind=kind,
                    source=Source.extractor,
                    confidence=0.85,
                    metadata={
                        "external_source": src.upstream,
                        "external_tool": src.tool,
                        "external_id": ext_id,
                    },
                )
                engine.put(c)
                if existed:
                    record["refreshed"] += 1
                    total_refreshed += 1
                else:
                    record["added"] += 1
                    total_added += 1
            except Exception as e:  # noqa: BLE001
                record["failed"] += 1
                log.warning(
                    "enrichment %s.%s ingest failed for %s: %s",
                    src.upstream, src.tool, ext_id, e,
                )

        per_source.append(record)

    return {
        "sources_run": len(sources),
        "added": total_added,
        "refreshed": total_refreshed,
        "failed": total_failed,
        "dry_run": dry_run,
        "per_source": per_source,
    }
