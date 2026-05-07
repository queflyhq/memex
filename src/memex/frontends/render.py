"""Markdown renderers for memex tool results.

Used by the MCP frontend so tool outputs blend into the LLM-and-user
conversation as readable markdown instead of raw Pydantic JSON dumps.

Pure functions, no I/O. Take engine output, produce strings.
"""

from __future__ import annotations

import re
from typing import Iterable

from memex.core.schema import Concept

_PRIORITY_ICON = {"p0": "🚨", "p1": "🔥", "p2": "•", "p3": "·"}
_STATUS_ICON = {
    "pending": "⏳",
    "in_progress": "🚧",
    "completed": "✓",
    "blocked": "🚫",
    "cancelled": "✗",
}

# Phase prefix on task names like "A1 — Foo", "A2a — Bar", "B5 — Baz".
_PHASE_RE = re.compile(r"^([A-Z])\d")


def _task_meta(t: Concept) -> tuple[str, str, str, str]:
    m = t.metadata or {}
    return (
        m.get("priority", "p2"),
        m.get("status", "pending"),
        m.get("due") or "—",
        m.get("owner") or "—",
    )


def _first_line(s: str, limit: int = 120) -> str:
    if not s:
        return ""
    head = s.strip().splitlines()[0].strip()
    return head[: limit - 1] + "…" if len(head) > limit else head


def task_summary(t: Concept) -> dict:
    """Slim projection used when callers want structured data, not the full body."""
    pri, status, due, owner = _task_meta(t)
    return {
        "id": t.id,
        "name": t.name,
        "status": status,
        "priority": pri,
        "due": due if due != "—" else None,
        "owner": owner if owner != "—" else None,
        "headline": _first_line(t.description, 200),
    }


def render_task_list(
    tasks: Iterable[Concept],
    blockers: dict[str, list[str]] | None = None,
    *,
    title: str | None = None,
) -> str:
    """Markdown table of tasks with priority/status/blocker columns."""
    tasks = list(tasks)
    if not tasks:
        return f"_{title or 'no matching tasks'}_"
    blockers = blockers or {}
    lines: list[str] = []
    if title:
        lines.append(f"**{title}** ({len(tasks)} task{'s' if len(tasks) != 1 else ''})")
        lines.append("")
    lines.append("| pri | status | task | id | blocked by |")
    lines.append("| --- | --- | --- | --- | --- |")
    for t in tasks:
        pri, status, _due, _owner = _task_meta(t)
        blocker_ids = blockers.get(t.id, [])
        block_cell = ", ".join(f"`{b[-8:]}`" for b in blocker_ids) if blocker_ids else "—"
        lines.append(
            f"| {_PRIORITY_ICON.get(pri, '·')} {pri} "
            f"| {_STATUS_ICON.get(status, '?')} {status} "
            f"| {t.name} "
            f"| `{t.id}` "
            f"| {block_cell} |"
        )
    return "\n".join(lines)


def render_next_actions(
    tasks: Iterable[Concept],
    *,
    downstream: dict[str, int] | None = None,
) -> str:
    """Bulleted list of next actions; shows how many downstream tasks each one gates."""
    tasks = list(tasks)
    if not tasks:
        return "_nothing actionable right now — every task is blocked or done_"
    downstream = downstream or {}
    parts: list[str] = ["**Next actions:**", ""]
    for t in tasks:
        pri, status, due, _owner = _task_meta(t)
        gates = downstream.get(t.id, 0)
        gate_label = f"  ·  gates {gates} downstream" if gates else ""
        parts.append(
            f"- {_PRIORITY_ICON.get(pri, '·')} **{t.name}**  `{t.id}`"
        )
        meta_line = f"  · {_STATUS_ICON.get(status, '?')} {status} · {pri}"
        if due != "—":
            meta_line += f" · due {due}"
        meta_line += gate_label
        parts.append(meta_line)
        body = _first_line(t.description, 160)
        if body and body != t.name:
            parts.append(f"  · {body}")
    return "\n".join(parts)


def render_project_view(
    project: Concept,
    tasks: list[Concept],
    blockers: dict[str, list[str]],
    blocks_what: dict[str, list[str]],
) -> str:
    """Project header + tasks grouped by phase prefix + dependency hints."""
    parts: list[str] = [f"# 🏗  {project.name}", ""]
    desc_head = _first_line(project.description, 240)
    if desc_head:
        parts.append(f"_{desc_head}_")
        parts.append("")

    if not tasks:
        parts.append("_no tasks linked to this project_")
        return "\n".join(parts)

    counts: dict[str, int] = {}
    for t in tasks:
        s = (t.metadata or {}).get("status", "pending")
        counts[s] = counts.get(s, 0) + 1
    order = ["pending", "in_progress", "blocked", "completed", "cancelled"]
    summary_bits = [
        f"{_STATUS_ICON.get(k, '?')} {counts[k]} {k}" for k in order if counts.get(k)
    ]
    parts.append(f"**Progress:** {' · '.join(summary_bits)}  ·  {len(tasks)} total")
    parts.append("")

    phases: dict[str, list[Concept]] = {}
    for t in tasks:
        m = _PHASE_RE.match(t.name)
        bucket = m.group(1) if m else "Other"
        phases.setdefault(bucket, []).append(t)

    name_by_id = {t.id: t.name for t in tasks}

    for phase in sorted(phases):
        parts.append(f"## Phase {phase}")
        for t in sorted(phases[phase], key=lambda x: x.name):
            pri, status, _due, _owner = _task_meta(t)
            block_ids = blockers.get(t.id, [])
            if block_ids:
                names = [
                    name_by_id.get(b, b[-8:]).split(" — ")[0] for b in block_ids
                ]
                block_label = f"  ← needs {', '.join(names)}"
            else:
                block_label = ""
            gates = blocks_what.get(t.id, [])
            gate_label = f"  → gates {len(gates)}" if gates else ""
            parts.append(
                f"- {_STATUS_ICON.get(status, '?')} **{t.name}**  "
                f"`{t.id}` · {pri}{block_label}{gate_label}"
            )
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"


def render_added_task(t: Concept) -> str:
    pri, status, due, owner = _task_meta(t)
    bits = [f"**+ added** `{t.id}`  {t.name}", f"_status_ {status} · _pri_ {pri}"]
    if due != "—":
        bits.append(f"_due_ {due}")
    if owner != "—":
        bits.append(f"_owner_ {owner}")
    return "  ·  ".join(bits)


_KIND_ICON = {
    "fact": "📄",
    "decision": "🎯",
    "constraint": "🛡",
    "pattern": "🧩",
    "person": "👤",
    "opinion": "💭",
    "question": "❓",
    "rejected": "✗",
    "approach": "📐",
    "module": "📦",
    "endpoint": "🔌",
    "task": "✅",
    "project": "🏗",
}

# Edges with these kinds are "low-signal" — they're the default linkage and
# usually clutter the rendered subgraph. We still surface them, but after
# the more meaningful ones.
_LOW_SIGNAL_EDGES = {"relates_to"}


def render_added_node(c: Concept) -> str:
    """One-line confirmation for a freshly stored kind=node concept."""
    kind = c.kind.value if hasattr(c.kind, "value") else str(c.kind)
    icon = _KIND_ICON.get(kind, "•")
    head = _first_line(c.description, 160)
    bits = [f"{icon} **+ added** `{c.id}`  {c.name}"]
    extras = [f"_kind_ {kind}"]
    if c.confidence < 1.0:
        extras.append(f"_conf_ {c.confidence:.2f}")
    if head and head != c.name:
        bits.append(f"  · {head}")
    return "  ·  ".join([bits[0], *extras]) + ("\n" + bits[1] if len(bits) > 1 else "")


def render_recall(result: dict) -> str:
    """Subgraph render: degraded banner → nodes grouped by kind → edges → footer."""
    nodes = result.get("nodes") or []
    edges = result.get("edges") or []
    parts: list[str] = []

    if result.get("degraded"):
        reason = result.get("degraded_reason") or "no reason given"
        parts.append(
            f"> ⚠️  **Degraded recall** — {reason}. Results are lexical-only; "
            "consider rephrasing or treat as incomplete."
        )
        parts.append("")

    if not nodes:
        parts.append("_no concepts matched this query_")
        return "\n".join(parts)

    # Group by kind so the user can scan "decisions" / "constraints" at a glance.
    by_kind: dict[str, list[dict]] = {}
    for n in nodes:
        by_kind.setdefault(n.get("kind", "fact"), []).append(n)

    # Stable order: most actionable first.
    kind_order = [
        "decision", "constraint", "pattern", "fact", "module", "endpoint",
        "person", "approach", "opinion", "question", "rejected", "task", "project",
    ]
    seen_kinds = [k for k in kind_order if k in by_kind] + [
        k for k in by_kind if k not in kind_order
    ]

    for kind in seen_kinds:
        icon = _KIND_ICON.get(kind, "•")
        kind_nodes = by_kind[kind]
        parts.append(f"## {icon} {kind} ({len(kind_nodes)})")
        for n in kind_nodes:
            conf = n.get("confidence", 1.0)
            conf_label = f" · _conf_ {conf:.2f}" if conf < 1.0 else ""
            parts.append(f"### {n.get('name','?')}  `{n.get('id','?')}`{conf_label}")
            desc = (n.get("description") or "").strip()
            if desc:
                # Indent as a blockquote so long bodies stay visually grouped
                # under their concept header.
                parts.append("")
                for line in desc.splitlines():
                    parts.append(f"> {line}" if line.strip() else ">")
            parts.append("")

    if edges:
        # Index nodes for pretty edge labels.
        name_by_id = {n.get("id"): n.get("name", "?") for n in nodes}
        meaningful = [e for e in edges if e.get("kind") not in _LOW_SIGNAL_EDGES]
        relates = [e for e in edges if e.get("kind") in _LOW_SIGNAL_EDGES]
        if meaningful or relates:
            parts.append("## 🔗 connections")
            for e in meaningful + relates:
                a = name_by_id.get(e.get("from_id"), e.get("from_id", "?"))
                b = name_by_id.get(e.get("to_id"), e.get("to_id", "?"))
                parts.append(f"- {a}  —`{e.get('kind','relates_to')}`→  {b}")
            parts.append("")

    footer_bits = []
    if (s := result.get("strategy")):
        footer_bits.append(f"strategy: `{s}`")
    if (t := result.get("tokens_used")):
        footer_bits.append(f"tokens: {t}")
    if footer_bits:
        parts.append("---")
        parts.append("_" + " · ".join(footer_bits) + "_")
    return "\n".join(parts).rstrip() + "\n"


def render_updated_task(before: Concept, after: Concept) -> str:
    """Show a diff line for what changed on the task."""
    b_pri, b_status, b_due, b_owner = _task_meta(before)
    a_pri, a_status, a_due, a_owner = _task_meta(after)
    diffs: list[str] = []
    if b_status != a_status:
        diffs.append(f"_status_ {b_status} → {_STATUS_ICON.get(a_status,'?')} {a_status}")
    if b_pri != a_pri:
        diffs.append(f"_pri_ {b_pri} → {a_pri}")
    if b_due != a_due:
        diffs.append(f"_due_ {b_due} → {a_due}")
    if b_owner != a_owner:
        diffs.append(f"_owner_ {b_owner} → {a_owner}")
    if (before.description or "") != (after.description or ""):
        diffs.append("_body_ updated")
    head = f"**↻ updated** `{after.id}`  {after.name}"
    if not diffs:
        return head + "  ·  _(no metadata changed)_"
    return head + "\n" + "  ·  ".join(diffs)


def render_progress(snap: dict) -> str:
    """Session-orient summary for engine.progress()."""
    parts = [f"**Memex progress**"]
    parts.append("")
    parts.append(f"- 📚 concepts: **{snap.get('concepts_total', 0)}**")
    parts.append(f"- 📜 events: **{snap.get('events_total', 0)}**")
    parts.append(f"- ✅ skill validations: **{snap.get('validations_count', 0)}**")
    actor = snap.get("actor_filter")
    if actor:
        parts.append(f"- 🎭 actor filter: `{actor}`")
    skills = snap.get("validated_skills") or []
    if skills:
        parts.append("")
        parts.append("**Skills validated this window:**")
        for s in skills:
            parts.append(f"- `{s}`")
    return "\n".join(parts)
