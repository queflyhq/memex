"""
MCP stdio frontend (Model Context Protocol).

Class-based wrapper around the official `mcp` Python SDK's FastMCP. Tool
implementations delegate to the engine façade — same shapes as the HTTP
endpoints, so behaviour is consistent across frontends.

Beyond memex's native tools (recall / add_node / link / observe / validate /
progress / list_skills / install_skill / stats), this server *also* exposes
two gateway tools that re-export tools from upstream MCP servers the user
configures in `~/.memex/upstreams.json` or `./.memex/upstreams.json`:

    list_upstream_tools()              → catalog of every proxied tool
    call_upstream(upstream, tool, args) → dispatches; auto-`observe()`s

This is what makes memex an "agentic memory" layer rather than a notebook:
every upstream tool call flows through here and is captured for free.
"""

from __future__ import annotations

import base64
import logging
from importlib import resources
from typing import TYPE_CHECKING, Any, Union

from memex.core.engine import Engine
from memex.core.schema import EdgeKind, NodeKind, Source
from memex.frontends.render import (
    render_added_node,
    render_added_task,
    render_next_actions,
    render_progress,
    render_project_view,
    render_recall,
    render_task_list,
    render_updated_task,
)
from memex.upstreams import Aggregator, load_upstreams

if TYPE_CHECKING:
    from memex.frontends.mcp.client import MemexClient

# Backend protocol: either an in-process Engine or a thin HTTP MemexClient.
# Both expose the same recall/add/link/observe/validate/progress/stats surface
# this module needs, so the tool registrations don't care which one they got.
Backend = Union[Engine, "MemexClient"]

log = logging.getLogger(__name__)


def _brand_icon() -> Any:
    """Build the MCP `Icon` for the memex server.

    Loads the theme-adaptive Quefly bird mark from the bundled package
    asset and returns it as a base64 data URI. MCP clients (Claude Code,
    Cursor, Windsurf) display this icon in their server list and beside
    each tool call so the user can see at a glance which server a call
    is coming from.

    The SVG itself uses `prefers-color-scheme: dark` to swap to a white
    silhouette on dark backgrounds — full color on light backgrounds.
    Renderers that don't honor the media query fall through to the color
    treatment, no regression.
    """
    from mcp.types import Icon

    svg_bytes = (
        resources.files("memex.assets").joinpath("logo-bird.svg").read_bytes()
    )
    data_uri = "data:image/svg+xml;base64," + base64.b64encode(svg_bytes).decode("ascii")
    return Icon(src=data_uri, mimeType="image/svg+xml", sizes=["any"])


# Server-level prompt every connecting LLM sees during the MCP handshake.
# Frame memex as the model's own persistent memory — not an external tool —
# so it forms the right habit: recall before claiming, remember after learning.
_SERVER_INSTRUCTIONS = """\
memex is YOUR persistent memory across sessions. Treat it as long-term cognition,
not a tool you optionally consult.

Habits you should form:

  RECALL FIRST. Before saying "I don't know X" or re-reading the same files
  you read yesterday, call `recall(query=...)`. The thing you're about to
  research may already be there — saving you tokens, time, and the user's
  patience.

  REMEMBER AFTER. When you learn something the next session should know —
  a decision the user made, a constraint the codebase has, a pattern that
  worked, a fact about the user's stack — call `add_node(...)`. If you don't
  remember it, future-you will repeat the same mistake or ask the same
  question.

  CONNECT KNOWLEDGE. When two concepts relate (one supersedes another, one
  depends on another, two are conflicting) call `link(from_id, to_id, kind)`.
  A pile of disconnected nodes is a notebook; a connected graph is memory.

  OBSERVE EVENTS. When something happens that matters (a decision was made,
  a tool was called, an error occurred, a user correction was received) call
  `observe(kind, payload)`. The episodic stream is what makes "what did we
  do yesterday" answerable.

  VALIDATE BEFORE GENERATING. When the request involves security, money,
  concurrency, crypto, SQL, or untrusted input, call `validate(skill=...)`
  first. The skills system is your safety check — ignoring it is what got
  you in trouble last time.

The graph survives across editor sessions, machines (when daemon-mode), and
weeks. What you put in is what you get back.
"""


def _build_blocker_maps(engine, tasks):
    """Walk the given tasks and return (blockers_for, downstream_for).

    blockers_for[task_id] = list of task ids that block this task.
    downstream_for[task_id] = list of task ids that this task blocks.

    Both keyed by the task's id. Tasks with no relevant edge are absent
    from the dicts (renderers default to []).
    """
    blockers: dict[str, list[str]] = {}
    downstream: dict[str, list[str]] = {}
    # In-process Engine exposes .semantic.neighbors; the HTTP client backend
    # doesn't (yet). When unavailable, return empty maps so renderers degrade
    # to "—" / no annotation rather than crashing the tool call.
    semantic = getattr(engine, "semantic", None)
    if semantic is None or not hasattr(semantic, "neighbors"):
        return blockers, downstream
    for t in tasks:
        try:
            _, edges = semantic.neighbors(t.id, depth=1)
        except Exception:  # noqa: BLE001 — render path must not raise
            continue
        for e in edges:
            if e.kind == EdgeKind.blocks and e.from_id == t.id:
                downstream.setdefault(t.id, []).append(e.to_id)
                blockers.setdefault(e.to_id, []).append(t.id)
    return blockers, downstream


class MCPServer:
    """MCP server wrapping a memex Engine or an HTTP client to a daemon.

    Run via `serve_stdio()`. If `aggregator` is provided (or auto-loaded from
    upstream config), its tools are exposed via the gateway meta-tools.
    """

    def __init__(
        self,
        engine: Backend,
        *,
        name: str = "MemeX",
        aggregator: Aggregator | None = None,
    ):
        try:
            from mcp.server.fastmcp import FastMCP
            from mcp.types import Icon
        except ImportError as e:  # pragma: no cover
            raise RuntimeError(
                "mcp Python SDK not installed. This is a required dependency — "
                "reinstall memex via `pipx install --force memex`."
            ) from e
        self.engine = engine
        # `name` is the display name MCP clients (Claude Code, Cursor, …) show
        # in their server list and (where supported) beside each tool call.
        # The tool prefix the AI sees ("memex__recall", "memex__observe", …)
        # is determined by the user's mcpServers config key, not by this
        # string — so we can ship a clean display name here.
        self.mcp = FastMCP(
            name,
            instructions=_SERVER_INSTRUCTIONS,
            website_url="https://github.com/queflyhq/memex",
            icons=[_brand_icon()],
        )
        self.aggregator = aggregator
        if self.aggregator is None:
            cfg = load_upstreams()
            if cfg.upstreams:
                self.aggregator = Aggregator(cfg.upstreams)
                try:
                    self.aggregator.start()
                except Exception as e:
                    log.warning("aggregator failed to start: %s — gateway disabled", e)
                    self.aggregator = None
        self._register_tools()

    def _register_tools(self) -> None:
        engine = self.engine
        mcp = self.mcp

        @mcp.tool()
        def recall(
            query: str,
            budget_tokens: int = 2000,
            kind: str | None = None,
            expand_hops: int = 1,
        ) -> str:
            """Recall what you (the model) already know about a topic.

            This searches your persistent memory across all past sessions. Returns
            a budget-bounded subgraph of relevant concepts and the edges between
            them. Use it INSTEAD OF re-reading files, re-grepping, or claiming
            ignorance.

            WHEN TO CALL:
              - Before answering "do you know about X" / "what is X"
              - At the start of any non-trivial task ("recall what we know about
                this codebase / project / user")
              - Before re-investigating something that feels familiar
              - When the user references prior work ("the thing we did last week")

            WHEN NOT TO CALL:
              - For trivial factual questions answerable from training data
                (e.g. "what's the capital of France")
              - When the user has already given you all the context you need

            PARAMETERS:
              query (str): natural-language description of what you want to know.
                Phrase as a question or topic, not a keyword. Example:
                "what did we decide about authentication" not "auth".
              budget_tokens (int, default=2000): max tokens of result content.
                Bump to 4000 for deep-dives; lower to 800 for quick lookups.
              kind (str, optional): filter to a specific node kind. One of:
                fact, decision, pattern, constraint, person, opinion, question,
                rejected, approach, module, endpoint. Omit to search all kinds.
              expand_hops (int, default=1, max=5): how many graph hops to
                include around each direct hit. Higher = more context, more
                tokens.

            RETURNS:
              dict with keys:
                nodes: list of concept dicts (id, name, description, kind,
                  confidence, ...)
                edges: list of edge dicts connecting the nodes
                tokens_used: int — actual tokens consumed
                strategy: str — "bm25" / "hybrid" / "hybrid+rerank"
                degraded: bool — if true, results are lexical-only (no vectors).
                  Treat as incomplete and consider rephrasing.
                degraded_reason: str | None — explanation when degraded.

            EXAMPLE:
              recall(query="how does the daemon detect existing instances",
                     budget_tokens=2000, kind="decision")
            """
            kind_enum = NodeKind(kind) if kind else None
            result = engine.recall(
                query=query,
                budget_tokens=budget_tokens,
                kind=kind_enum,
                expand_hops=expand_hops,
            ).to_dict()
            return render_recall(result)

        @mcp.tool()
        def add_node(
            name: str,
            description: str = "",
            kind: str = "fact",
            source: str = "agent",
            confidence: float = 1.0,
            verification: str | None = None,
            links: list[dict[str, str]] | None = None,
        ) -> str:
            """Remember something. Saves a concept to your persistent memory.

            Use this any time you learn something the next session should know.
            The next time you (or another session) calls recall(), this concept
            can come back. If you don't add_node it, it's lost when the session
            ends.

            WHEN TO CALL:
              - The user makes a decision ("we'll use Postgres, not Mongo")
              - You discover a non-obvious constraint ("the prod DB has 50M rows
                so migrations need a backfill plan")
              - You confirm a fact about the user's stack, role, or preference
              - You identify a pattern that worked / didn't work
              - The user corrects you ("don't suggest X — we tried that, it
                broke Y")

            WHEN NOT TO CALL:
              - To save the answer to a question you just answered (the user
                read it; it's in their session). Save the *insight*, not the
                response.
              - For ephemeral session state (current cursor position, todos
                for this turn) — those go in TodoWrite, not memex.
              - For things already in CLAUDE.md or repo docs — those are
                already discoverable.

            PARAMETERS:
              name (str): short, specific identifier. "AuthFI uses Cloudflare
                Workers + D1 for tenant routing", NOT "tenant routing".
              description (str): the body. For `kind=decision` or `constraint`,
                structure as: "<rule>\\n\\n**Why:** <reason>\\n**How to apply:**
                <when this kicks in>". The Why lets future-you judge edge cases.
              kind (str, default="fact"): one of:
                fact         — a verifiable claim about the world or the codebase
                decision     — a choice that was made and why (use **Why:** body)
                constraint   — a rule that must hold (use **Why:** + **How:** body)
                pattern      — a reusable approach that worked
                person       — info about a human (the user, a teammate)
                opinion      — a preference or judgment (less durable than fact)
                question     — an open question to revisit
                rejected     — an option that was considered and ruled out
                approach     — validation entry for a skill (rarely added by hand)
                module       — a code module with notable behavior
                endpoint     — an API endpoint with notable contract
              source (str, default="agent"): who is asserting this. One of:
                human, claude_code, cursor, windsurf, cline, agent, skill,
                extractor. Use "human" when the user explicitly told you;
                "agent" when you inferred or observed.
              confidence (float, 0.0–1.0, default=1.0): how sure you are.
                1.0 = the user explicitly said it. 0.7 = strong inference.
                0.4 = guess from one signal. Below 0.5 will decay quickly.
              verification (str, optional): a falsifiability primitive — a
                way to re-check this claim later (a regex, a Cypher query, an
                HTTP probe URL, a shell command). Without it, the lifecycle
                pass downgrades to kind=opinion.
              links (list[dict], optional): edges to create in the same call —
                no separate `link()` round-trip. Each item is one of:
                  {"to":   "<concept-id>", "kind": "<edge-kind>"}  — outgoing
                  {"from": "<concept-id>", "kind": "<edge-kind>"}  — incoming
                Edge kinds: supersedes, depends_on, implements, motivated_by,
                rejected_due_to, conflicts_with, same_as, relates_to, calls,
                blocks, part_of, spawned_from. Default kind: relates_to.
                Bad edges are logged and skipped — they don't fail the add.

            RETURNS:
              Markdown confirmation: kind icon + id + name + first-line of
              the body + count of edges wired. The id appears verbatim as
              a backticked span so subsequent calls can extract it.

            EXAMPLE:
              add_node(
                name="memex daemon mode is auto-spawn by default",
                description=(
                  "When `memex serve` is invoked and MEMEX_DAEMON_URL is unset,"
                  " it auto-spawns a daemon and proxies through it.\\n\\n"
                  "**Why:** Kuzu and DuckDB are single-writer; spawning a daemon"
                  " avoids cross-session lock collisions.\\n"
                  "**How to apply:** rely on the default; set MEMEX_DAEMON_URL"
                  " only when pointing at a remote/K8s daemon."
                ),
                kind="decision",
                source="agent",
                confidence=1.0,
                links=[{"to": "c_olddaemon", "kind": "supersedes"}],
              )
            """
            c = engine.add(
                name=name,
                description=description,
                kind=NodeKind(kind),
                source=Source(source),
                confidence=confidence,
                verification=verification,
            )
            wired = 0
            for spec in links or []:
                try:
                    edge_kind = EdgeKind(spec.get("kind") or "relates_to")
                    if "to" in spec and spec["to"]:
                        engine.link(
                            from_id=c.id,
                            to_id=spec["to"],
                            kind=edge_kind,
                            source=Source(source),
                        )
                        wired += 1
                    elif "from" in spec and spec["from"]:
                        engine.link(
                            from_id=spec["from"],
                            to_id=c.id,
                            kind=edge_kind,
                            source=Source(source),
                        )
                        wired += 1
                    else:
                        log.warning("add_node link spec missing 'to'/'from': %r", spec)
                except Exception as e:  # noqa: BLE001
                    log.warning("add_node link %r failed: %s", spec, e)
            out = render_added_node(c)
            if wired:
                out += f"\n  ·  🔗 wired **{wired}** edge{'s' if wired != 1 else ''}"
            return out

        @mcp.tool()
        def link(
            from_id: str,
            to_id: str,
            kind: str = "relates_to",
            source: str = "agent",
        ) -> dict[str, str]:
            """Connect two memories with a typed relationship.

            A pile of disconnected concepts is a notebook; a graph is memory.
            Edges turn isolated facts into navigable knowledge — recall on one
            node walks the edges to surface the right neighbors.

            WHEN TO CALL:
              - Right after add_node when the new concept relates to one you
                already remember (link them in the same turn so it's never
                orphaned)
              - When you discover that two existing concepts are related
                (`recall` returned them both, you noticed the connection)
              - When a new decision supersedes an old one (use `supersedes`)
              - When implementing a pattern (use `implements`)

            WHEN NOT TO CALL:
              - When the relationship is implicit in `kind` or `metadata`
                (don't link a concept to itself)
              - When you only "kinda think" they're related — link with
                purpose, not as a habit

            PARAMETERS:
              from_id (str): id of the source concept (returned from add_node
                or recall). Format: "c_<hex>".
              to_id (str): id of the target concept.
              kind (str, default="relates_to"): the edge type. One of:
                relates_to       — generic association (default; use sparingly)
                depends_on       — A needs B to exist / function
                implements       — A is an implementation of pattern/spec B
                supersedes       — A replaces B (B is obsolete; use this on
                                   decision changes)
                conflicts_with   — A and B contradict each other (flag for
                                   resolution)
                motivated_by     — A exists because of B (links decisions to
                                   the constraint or fact that drove them)
                rejected_due_to  — A was rejected because of B (decision
                                   archaeology)
                same_as          — A and B are the same concept (use sparingly;
                                   prefer to merge)
                calls            — A invokes B at runtime (code-level)
              source (str, default="agent"): who asserted this link. Same
                vocabulary as add_node: human, claude_code, cursor, agent, etc.

            RETURNS:
              {"status": "ok"} on success. Idempotent — relinking the same
              triple is a no-op.

            EXAMPLE:
              link(from_id="c_a1b2c3d4",
                   to_id="c_e5f6g7h8",
                   kind="supersedes",
                   source="human")
            """
            engine.link(
                from_id=from_id,
                to_id=to_id,
                kind=EdgeKind(kind),
                source=Source(source),
            )
            return {"status": "ok"}

        @mcp.tool()
        def observe(
            kind: str,
            actor: str = "agent",
            payload: dict[str, Any] | None = None,
        ) -> dict[str, str]:
            """Record that something happened. Time-indexed episodic memory.

            add_node stores durable knowledge ("Postgres is the DB"). observe
            stores events ("a query failed", "the user asked about Y", "I called
            tool X"). The episodic stream answers temporal questions:
              - "what did we do yesterday?"
              - "when did this break?"
              - "how often does this come up?"

            Events are how memex perceives the world; concepts are how it
            understands the world. You need both.

            WHEN TO CALL:
              - A decision was made (kind="decision_made")
              - An error or unexpected behavior occurred (kind="error" /
                "unexpected_behavior")
              - The user explicitly corrected you (kind="user_correction")
              - A milestone was reached (kind="milestone")
              - A meaningful tool was called whose result the next session
                might want to know (kind="tool_call" — though the gateway
                does this automatically)
              - Anything where "and then this happened" is the right framing

            WHEN NOT TO CALL:
              - For internal thinking / draft outputs — events should be
                meaningful in the next session, not noise
              - For tool calls already captured by the gateway (would
                duplicate)

            PARAMETERS:
              kind (str): a short snake_case label categorizing the event.
                Pick consistent verbs/nouns so future queries find the family:
                "decision_made", "error", "user_correction", "tool_call",
                "milestone", "feedback_received". Avoid one-off names.
              actor (str, default="agent"): who caused this. Same vocabulary
                as add_node: human, claude_code, cursor, agent, etc.
              payload (dict, optional): structured context. Keep it small —
                strings >2KB get truncated. Include the few fields that make
                this event findable later (which file, which command, what
                the user said).

            RETURNS:
              {"id": "ev_<hex>"} — the event id, useful for linking events
              to concepts via spawned_from edges.

            EXAMPLE:
              observe(kind="user_correction",
                      actor="human",
                      payload={
                        "rule": "never use --no-verify on commits",
                        "context": "i corrected claude after it bypassed hooks",
                      })
            """
            ev = engine.observe(kind=kind, actor=Source(actor), payload=payload or {})
            return {"id": ev.id}

        @mcp.tool()
        def validate(skill: str, actor: str = "agent") -> dict[str, Any]:
            """Pull the safety contract for a high-stakes context. Call BEFORE
            generating code where mistakes have real consequences.

            A skill is a structured `approach + checks + examples_good +
            examples_bad` payload. The contract is: you self-attest each check
            passes BEFORE producing the code. If any check would fail, refuse
            to generate and ask the user instead.

            WHEN TO CALL (always, before generating):
              - Authentication / authorization code
              - Cryptography (keys, hashing, signing, TLS)
              - SQL with user input
              - Concurrency primitives (locks, channels, transactions)
              - Money / billing logic
              - Untrusted-input parsing (deserialization, regex on user data)
              - File / shell command execution
              - Anything with "DELETE", "DROP", or "rm" in scope

            WHEN NOT TO CALL:
              - For pure refactors with full test coverage
              - For UI / presentation code
              - For documentation

            PARAMETERS:
              skill (str): the skill name. Run list_skills() to discover what's
                installed. Common names: "secrets-handling", "sql-injection",
                "concurrency-primitives", "untrusted-input".
              actor (str, default="agent"): who is invoking the validation.

            RETURNS:
              On success: dict with keys:
                ok: bool
                approach: str — the prescribed pattern
                checks: list[str] — explicit pass/fail criteria you must
                  self-attest before generating
                examples_good: list — patterns to follow
                examples_bad: list — patterns to avoid
                triggers: list[str] — when this skill applies
              On failure: {"ok": false, "error": "skill `X` is not installed"}
              — in which case use install_skill() or fall back to your default
              caution.

            EXAMPLE:
              validate(skill="sql-injection")
              # then attest: "I will use parameterized queries; user input
              #  never reaches string concatenation; checks 1, 2, 3 pass"
              # before writing the SQL.
            """
            return engine.validate(skill, actor=Source(actor))

        @mcp.tool()
        def progress(actor: str | None = None) -> str:
            """What have you done in this store? Self-audit summary.

            Returns counts and recent activity — useful for orienting at the
            start of a session ("what's the state of memex?") or for closing
            out a session ("did I capture what I learned?").

            WHEN TO CALL:
              - Start of session, alongside recall()
              - When the user asks "what have we been working on"
              - Before claiming "I don't have context" — progress shows you
                what context is available

            PARAMETERS:
              actor (str, optional): filter to a specific actor's activity
                (human, claude_code, etc.). Omit to see everything.

            RETURNS:
              dict with:
                concepts_total: int — total concepts in the graph
                events_total: int — total episodic events
                validated_skills: list[str] — skills you've called validate()
                  on (the safety trail)
                validations_count: int

            EXAMPLE:
              progress()  # or progress(actor="human")
            """
            return render_progress(
                engine.progress(actor=Source(actor) if actor else None)
            )

        @mcp.tool()
        def list_skills() -> list[dict[str, Any]]:
            """List installable skill bundles (safety contracts and approaches).

            Each skill is a curated bundle of validation rules, examples, and
            triggers — the "what's the right way to do X" knowledge the
            community has agreed on. Call this to discover what's available
            BEFORE assuming a skill is or isn't installed.

            WHEN TO CALL:
              - User asks "what skills are available"
              - Before validate() if you're not sure of the exact skill name
              - When orienting in a fresh memex install

            RETURNS:
              list of dicts: [{name, version, description}, ...]
            """
            from memex.skills import list_builtin_skills

            return [
                {"name": s.name, "version": s.version, "description": s.description}
                for s in list_builtin_skills()
            ]

        @mcp.tool()
        def install_skill(name: str) -> dict[str, Any]:
            """Install a skill bundle by name. Adds its approaches as concepts
            so validate() can find them.

            WHEN TO CALL:
              - User explicitly asks to install a skill
              - validate() returned "skill not installed" and the user wants
                that protection going forward
              - Setting up a fresh memex for a new project

            WHEN NOT TO CALL:
              - Without the user's awareness — installing a skill changes what
                validate() will require going forward; that's a contract
                change, not a silent op

            PARAMETERS:
              name (str): the bundle name from list_skills(). Common starters:
                "core-validations" (security/SQL/crypto baseline).

            RETURNS:
              {"ok": true, "concepts_added": int, "edges_added": int}
                on success
              {"ok": false, "error": "skill `X` not found"} otherwise
            """
            from memex.skills import find_builtin_skill
            from memex.skills import install_skill as _install

            skill = find_builtin_skill(name)
            if skill is None:
                return {"ok": False, "error": f"skill `{name}` not found"}
            counts = _install(engine, skill)
            return {"ok": True, **counts}

        # ---- project management tools -----------------------------------
        # Tasks/milestones/projects ARE concepts (kind=task etc.) with workflow
        # state in metadata. Same graph, queryable cross-session. Survives
        # editor restarts and machine moves; the in-session TodoWrite list
        # becomes the *interface*, memex becomes the *durable store*.

        @mcp.tool()
        def add_task(
            title: str,
            description: str = "",
            status: str = "pending",
            priority: str = "p2",
            due: str | None = None,
            project_id: str | None = None,
            blocked_by: list[str] | None = None,
            owner: str | None = None,
        ) -> str:
            """Create a new task in your persistent TODO graph.

            Tasks are concepts (kind=task) with workflow status in metadata.
            They survive across sessions, are queryable cross-repo, and link
            to the projects/milestones they belong to.

            WHEN TO CALL:
              - User asks you to track a TODO that should outlive this session
              - You commit to a follow-up that the next session must remember
              - A bug is discovered but can't be fixed right now — file it
              - Decomposing a large goal into 3hr-or-less actions

            WHEN NOT TO CALL:
              - For ephemeral within-turn TODOs (use TodoWrite — and it
                auto-syncs here via the todowrite-sync hook)
              - For "tasks" that are actually decisions or facts (use add_node
                with the appropriate kind)

            PARAMETERS:
              title (str): one-line task summary. Imperative form: "wire X
                into Y", "fix Z", "investigate W".
              description (str): the body. Include acceptance criteria.
              status (str, default="pending"): one of:
                pending | in_progress | completed | blocked | cancelled
              priority (str, default="p2"): p0 (urgent, drop everything) /
                p1 (this week) / p2 (this sprint) / p3 (someday).
              due (str, optional): ISO date 'YYYY-MM-DD' or natural ('next
                friday'). Stored verbatim in metadata.
              project_id (str, optional): id of a kind=project concept this
                task belongs to. Auto-creates a part_of edge.
              blocked_by (list[str], optional): ids of tasks blocking this
                one. Auto-creates blocks edges (each blocker → this task).
              owner (str, optional): person responsible. Free-form string
                (e.g. "piyush") — not enforced.

            RETURNS:
              A short markdown confirmation including the new task's `id`.
              The id appears verbatim (`c_xxxxxxxx`) so subsequent calls
              (update_task, link, list_tasks) can extract it.

            EXAMPLE:
              add_task(
                title="install fastembed[embed] and run memex reindex",
                priority="p1",
                due="2026-05-08",
                project_id="c_abc123def456",
              )
            """
            c = engine.add_task(
                title=title,
                description=description,
                status=status,
                priority=priority,
                due=due,
                project_id=project_id,
                blocked_by=blocked_by,
                owner=owner,
            )
            return render_added_task(c)

        @mcp.tool()
        def update_task(
            id: str,
            status: str | None = None,
            priority: str | None = None,
            due: str | None = None,
            owner: str | None = None,
            description: str | None = None,
        ) -> str:
            """Update a task's status / priority / due / owner.

            Re-asserts the concept with patched metadata. Status changes bump
            `last_confirmed_at` (= "last status update"). Move tasks through
            the workflow lifecycle: pending → in_progress → completed.

            WHEN TO CALL:
              - User says "I finished X" — set status="completed"
              - You start work on a task — set status="in_progress"
              - Priority changes — update accordingly
              - Task is blocked on external work — set status="blocked"
                AND link the blocker via link(blocker, this_task,
                kind="blocks")

            PARAMETERS:
              id (str): task concept id (returned from add_task or list_tasks).
              status (str, optional): one of pending | in_progress |
                completed | blocked | cancelled. Omit to leave unchanged.
              priority (str, optional): p0 / p1 / p2 / p3.
              due (str, optional): ISO date or natural string.
              owner (str, optional): responsible person.
              description (str, optional): replace the body.

            RETURNS:
              Markdown diff showing what changed (status / priority / due /
              owner / body). If the id doesn't resolve, returns a short
              error message instead.

            EXAMPLE:
              update_task(id="c_a1b2c3d4", status="completed")
            """
            before = engine.get(id)
            updated = engine.update_task(
                task_id=id,
                status=status,
                priority=priority,
                due=due,
                owner=owner,
                description=description,
            )
            if updated is None:
                return f"_task `{id}` not found_"
            if before is None:
                # Edge case: task existed in update_task but not in get() — render
                # without diff.
                return f"**↻ updated** `{updated.id}`  {updated.name}"
            return render_updated_task(before, updated)

        @mcp.tool()
        def list_tasks(
            status: str = "pending",
            project_id: str | None = None,
            owner: str | None = None,
            limit: int = 50,
        ) -> str:
            """List tasks, filtered by workflow state / project / owner.

            Returns a markdown table ordered by priority then due date. The
            everyday "what do I need to do" query — call at the start of any
            session to orient.

            WHEN TO CALL:
              - Start of session ("what's open?")
              - User asks "what's pending"
              - Before adding a task — check whether it already exists

            PARAMETERS:
              status (str, default="pending"): which workflow state to filter
                to. Pass "*" or "" to include every status.
              project_id (str, optional): only tasks part_of this project.
              owner (str, optional): only tasks with this owner.
              limit (int, default=50): max number of tasks to return.

            RETURNS:
              Markdown table: priority · status · task name · id · blocker
              ids. Task ids appear inline as `c_xxxxxxxx` and can be passed
              to update_task / link / project_view.
            """
            tasks = engine.list_tasks(
                status=status, project_id=project_id, owner=owner, limit=limit
            )
            blockers, _ = _build_blocker_maps(engine, tasks)
            title = None
            if project_id:
                proj = engine.get(project_id)
                title = f"Tasks in {proj.name}" if proj else f"Tasks in `{project_id}`"
            elif status and status != "*":
                title = f"{status} tasks"
            return render_task_list(tasks, blockers=blockers, title=title)

        @mcp.tool()
        def next_actions(limit: int = 5) -> str:
            """Return the top N tasks you should pick up RIGHT NOW.

            Filters out blocked tasks (any task with an inbound `blocks` edge
            from a non-completed task is excluded), then ranks by priority +
            due date. The "what should I do next" answer.

            WHEN TO CALL:
              - User asks "what next" / "what should I work on"
              - Start of a session, after recall(), to pick a task
              - When idle and looking for productive work

            PARAMETERS:
              limit (int, default=5): max actions to return.

            RETURNS:
              Markdown bulleted list. Each item shows status, priority, due,
              and how many downstream tasks it gates (`gates N downstream`).
              Task ids are inline as `c_xxxxxxxx`.
            """
            tasks = engine.next_actions(limit=limit)
            # Compute "downstream" — for each next-action task, how many
            # tasks have a blocks-edge from it (i.e. how much work it unblocks).
            _, downstream_lists = _build_blocker_maps(
                engine,
                # Need every task to compute downstream, not just next_actions —
                # a next-action may gate tasks that aren't in `tasks`.
                engine.list_tasks(status="*", limit=10_000),
            )
            downstream = {tid: len(v) for tid, v in downstream_lists.items()}
            return render_next_actions(tasks, downstream=downstream)

        @mcp.tool()
        def project_view(project_id: str) -> str:
            """Render a project as a tree: phases → tasks → dependency hints.

            Shows progress (counts by status), groups tasks by phase prefix
            (A1/A2a/B5/C7 → phases A, B, C), and inline-annotates each task
            with what blocks it (`← needs X, Y`) and what it gates
            (`→ gates N`). The single-shot view of "where are we on this
            project."

            WHEN TO CALL:
              - User asks "where are we on <project>" / "show the project"
              - Start of a session working on a known project
              - After completing a task, to see what unblocked

            PARAMETERS:
              project_id (str): id of the kind=project concept (returned
                from add_node when the project was created).

            RETURNS:
              Markdown tree of the project's tasks with status icons and
              dependency annotations. Empty tree message if the id doesn't
              resolve or has no tasks linked via part_of.
            """
            proj = engine.get(project_id)
            if proj is None:
                return f"_no concept with id `{project_id}`_"
            if proj.kind != NodeKind.project:
                return (
                    f"_`{project_id}` is kind={proj.kind.value}, not a project — "
                    f"call recall() to look it up_"
                )
            tasks = engine.list_tasks(status="*", project_id=project_id, limit=500)
            blockers, downstream_lists = _build_blocker_maps(engine, tasks)
            return render_project_view(proj, tasks, blockers, downstream_lists)

        @mcp.tool()
        def stats() -> dict[str, Any]:
            """Storage stats and tier status — diagnostic snapshot of memex itself.

            Use to answer "is memex healthy?" / "is the embedding tier on?" /
            "how much memory have we accumulated?" Calls do NOT mutate state
            and are safe to run any time.

            RETURNS:
              dict with:
                concepts: int — total nodes
                vectors: int — total embedded concepts (== concepts when
                  embedding tier is on and reindex is current; less if not)
                events: int — total episodic events
                embed_tier_available: bool — false means recall is in
                  degraded BM25-only mode
                embed_model: str | None — active embedding model name
                data_dir: str — where everything lives on disk
            """
            return engine.stats()

        # ---- transparent upstream re-registration -----------------------
        # Every upstream tool is exposed as a first-class memex tool, so the
        # LLM sees a flat tool list (github__create_issue, slack__post_message)
        # and calls them like any built-in. Each call is auto-captured into
        # episodic memory.
        aggregator = self.aggregator
        if aggregator is not None:
            self._register_proxied_tools(mcp, aggregator, engine)

        # ---- gateway meta-tools (always available when an aggregator is live)
        # These are still useful: list_upstream_tools is the discovery surface
        # for the catalog UI; call_upstream is the escape hatch for tools
        # whose names collide or whose schemas don't introspect cleanly.
        if aggregator is not None:

            @mcp.tool()
            def list_upstream_tools() -> dict[str, Any]:
                """Discover every tool re-exported from upstream MCP servers
                (github, linear, slack, postgres, k8s, etc.) — your full
                external-system reach in one catalog.

                Use this when you need to interact with an external service and
                want to see what's available. memex acts as a gateway: any
                upstream tool you call via `call_upstream` is automatically
                captured as an episodic event, so you build memory of what
                you've done across systems for free.

                WHEN TO CALL:
                  - User asks for an action that touches an external service
                    ("create an issue", "deploy", "post to slack")
                  - You're not sure if a capability is available
                  - At the start of complex multi-system tasks

                RETURNS:
                  dict with:
                    tools: list of {upstream, upstream_tool, exposed_name,
                      description, input_schema}
                    count: int — total tools available

                  `upstream` is the short server name (e.g. "github").
                  `upstream_tool` is the original tool name on that server
                  (use this in call_upstream's `tool` parameter).
                """
                tools = [
                    {
                        "upstream": t.upstream,
                        "upstream_tool": t.upstream_tool,
                        "exposed_name": t.exposed_name,
                        "description": t.description,
                        "input_schema": t.input_schema,
                    }
                    for t in aggregator.proxied_tools()
                ]
                return {"tools": tools, "count": len(tools)}

            @mcp.tool()
            def call_upstream(
                upstream: str,
                tool: str,
                arguments: dict[str, Any] | None = None,
            ) -> dict[str, Any]:
                """Invoke a tool on an upstream MCP server (github, slack,
                postgres, etc.) through the memex gateway.

                Every call is automatically recorded as an episodic event
                (kind=tool_call) — memex remembers WHAT you did across all
                external systems without you having to think about it. This is
                what makes "what did I do on github yesterday" answerable later.

                WHEN TO CALL:
                  - You found the tool you need via list_upstream_tools()
                  - You want the call captured into memex (almost always)

                WHEN NOT TO CALL:
                  - When the user explicitly asked you NOT to use that
                    upstream
                  - When the action is destructive and you haven't confirmed
                    with the user (this tool doesn't add a confirmation
                    layer; that's still on you)

                PARAMETERS:
                  upstream (str): short name from list_upstream_tools (e.g.
                    "github", "slack", "k8s").
                  tool (str): the original tool name on the upstream — NOT the
                    `exposed_name` field. From list_upstream_tools, use the
                    `upstream_tool` value.
                  arguments (dict, optional): the tool's args dict. Must match
                    the upstream's `input_schema`. Large blobs in arguments
                    (>2KB strings) get truncated in the captured event but
                    pass through to the upstream intact.

                RETURNS:
                  On success: {"ok": true, "result": <upstream's response>}
                  On failure: {"ok": false, "error": "<exception message>"}
                  Either way, the attempt is recorded in episodic memory.

                EXAMPLE:
                  call_upstream(
                    upstream="github",
                    tool="create_issue",
                    arguments={
                      "owner": "queflyhq",
                      "repo": "memex",
                      "title": "switch storage to DuckDB",
                      "body": "Unify Kuzu+SQLite into one .duckdb file."
                    }
                  )
                """
                args = arguments or {}
                ok = True
                error: str | None = None
                result_payload: Any = None
                try:
                    result = aggregator.call(upstream, tool, args)
                    # mcp result objects expose .content (list[TextContent|...]) or .model_dump()
                    if hasattr(result, "model_dump"):
                        result_payload = result.model_dump(mode="json")
                    else:
                        result_payload = result
                except Exception as e:
                    ok = False
                    error = f"{type(e).__name__}: {e}"
                    log.warning("upstream call failed (%s.%s): %s", upstream, tool, e)
                # Always observe — success or failure both inform memory.
                engine.observe(
                    kind="tool_call",
                    actor=Source.agent,
                    payload={
                        "upstream": upstream,
                        "tool": tool,
                        "arguments": _scrub_for_event(args),
                        "ok": ok,
                        "error": error,
                    },
                )
                if not ok:
                    return {"ok": False, "error": error}
                return {"ok": True, "result": result_payload}

    def _register_proxied_tools(self, mcp: Any, aggregator: Aggregator, engine: Backend) -> None:
        """Register every upstream tool as a flat first-class MCP tool.

        Without this step, an LLM has to discover tools via list_upstream_tools()
        and dispatch via call_upstream() — awkward UX that LLMs reliably forget.
        With this, github/slack/k8s tools just appear in the LLM's tool list
        with `<upstream>__<tool>` names and call cleanly.
        """
        for pt in aggregator.proxied_tools():
            self._register_one_proxy(mcp, aggregator, engine, pt)

    def _register_one_proxy(
        self, mcp: Any, aggregator: Aggregator, engine: Backend, pt: Any
    ) -> None:
        upstream_name = pt.upstream
        upstream_tool = pt.upstream_tool
        exposed_name = pt.exposed_name
        description = _format_proxy_description(pt)

        # Closure binding by argument so each tool gets its own upstream/tool.
        def proxy_fn(arguments: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
            args = dict(arguments) if isinstance(arguments, dict) else {}
            # Allow callers to pass either arguments={...} or flat kwargs.
            args.update(kwargs)
            ok = True
            err: str | None = None
            payload: Any = None
            try:
                result = aggregator.call(upstream_name, upstream_tool, args)
                if hasattr(result, "model_dump"):
                    payload = result.model_dump(mode="json")
                else:
                    payload = result
            except Exception as e:  # noqa: BLE001
                ok = False
                err = f"{type(e).__name__}: {e}"
                log.warning("proxy call failed (%s.%s): %s", upstream_name, upstream_tool, e)
            engine.observe(
                kind="tool_call",
                actor=Source.agent,
                payload={
                    "upstream": upstream_name,
                    "tool": upstream_tool,
                    "exposed_name": exposed_name,
                    "arguments": _scrub_for_event(args),
                    "ok": ok,
                    "error": err,
                },
            )
            if not ok:
                return {"ok": False, "error": err}
            return {"ok": True, "result": payload}

        proxy_fn.__name__ = exposed_name.replace(".", "_").replace("-", "_")
        proxy_fn.__doc__ = description
        try:
            mcp.add_tool(proxy_fn, name=exposed_name, description=description)
        except Exception as e:  # noqa: BLE001
            log.warning("could not register proxy tool %s: %s", exposed_name, e)

    def serve_stdio(self) -> None:
        """Run the stdio MCP server. Blocks for the editor's lifetime."""
        try:
            self.mcp.run()
        finally:
            if self.aggregator is not None:
                try:
                    self.aggregator.stop()
                except Exception as e:
                    log.warning("aggregator stop error: %s", e)


def _format_proxy_description(pt: Any) -> str:
    """Build an LLM-friendly description for a proxied upstream tool.

    The MCP protocol surfaces an inputSchema separately, but providing the
    schema in prose inside the description makes the tool usable even when
    a client renders schemas poorly.
    """
    base = pt.description or ""
    schema = pt.input_schema or {}
    props = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    lines = [
        base.strip(),
        "",
        f"UPSTREAM: {pt.upstream}.{pt.upstream_tool}",
        "Auto-captured to memex episodic memory on every call.",
    ]
    if props:
        lines.append("")
        lines.append("PARAMETERS:")
        for pname, pinfo in props.items():
            ptype = pinfo.get("type", "any")
            pdesc = pinfo.get("description", "").strip()
            req = "required" if pname in required else "optional"
            entry = f"  {pname} ({ptype}, {req})"
            if pdesc:
                entry += f": {pdesc}"
            lines.append(entry)
    return "\n".join(lines).strip()


def _scrub_for_event(args: dict[str, Any]) -> dict[str, Any]:
    """Truncate large args before storing in episodic events.

    Prevents the episodic store from ballooning when an agent passes large
    blobs (file contents, base64, etc.) as tool arguments.
    """
    MAX = 2000
    out: dict[str, Any] = {}
    for k, v in args.items():
        if isinstance(v, str) and len(v) > MAX:
            out[k] = v[:MAX] + f"…(+{len(v) - MAX} chars)"
        else:
            out[k] = v
    return out


def run_stdio(engine: Engine | None = None) -> None:
    """Convenience entry: build engine if not provided, then run stdio MCP."""
    engine = engine or Engine.build_default()
    MCPServer(engine).serve_stdio()


def run_stdio_via_daemon(auto_spawn: bool = True) -> None:
    """Run the MCP stdio server as a thin client to the daemon.

    Each MCP stdio process opens its own Kuzu DB, and Kuzu acquires an
    exclusive directory lock — so two parallel chat sessions collide. This
    entry point makes the MCP a thin HTTP client to the daemon instead, so
    N sessions all share one engine. If auto_spawn is True (the default),
    we'll start the daemon ourselves when no instance is running.
    """
    from memex.config import get_settings
    from memex.frontends.mcp.client import MemexClient
    from memex.frontends.mcp.daemon import (
        daemon_url,
        ensure_daemon,
        is_daemon_alive,
    )

    settings = get_settings()
    url = daemon_url(settings)
    # If MEMEX_DAEMON_URL was set explicitly, the user is pointing at a remote
    # daemon (e.g. one in K8s) — auto-spawning a local one would be wrong.
    # Fail loud instead so the misconfig is obvious.
    if settings.daemon_url:
        auto_spawn = False
    if auto_spawn:
        url = ensure_daemon(settings)
    elif not is_daemon_alive(url, settings.auth_token):
        raise RuntimeError(
            f"memex daemon not reachable at {url}. "
            f"{'Check MEMEX_DAEMON_URL / MEMEX_AUTH_TOKEN.' if settings.daemon_url else 'Start it with: memex daemon'}"
        )
    client = MemexClient(base_url=url, auth_token=settings.auth_token)
    MCPServer(client).serve_stdio()
