# Architecture

memex is a single perception-cognition loop. Everything in the codebase
maps to one of five layers.

```
┌────────────┐    ┌─────────────┐    ┌──────────┐    ┌──────────┐
│ PERCEPTION │ ─► │   CONTEXT   │ ─► │ COGNITION│ ─► │  ACTION  │
│  (events)  │    │ (live state)│    │ (ML/LLM) │    │ (decide) │
└────────────┘    └─────────────┘    └──────────┘    └──────────┘
       ▲                                    │              │
       │                                    ▼              │
       │                            ┌────────────┐         │
       └─────────────────────────── │   MEMORY   │ ◄───────┘
                                    │  (graph)   │
                                    └────────────┘
```

The loop is the product. A new contribution is "good" iff it improves one
of the five layers without breaking the contracts between them.

## The five layers

### 1. Perception — raw signals in

Every external observation lands here as an immutable `event` row.
Perception is append-only and cheap; it never blocks an AI tool call.

**Sources:** Claude Code hooks (PreToolUse, PostToolUse, UserPromptSubmit,
Stop), CLI commands, MCP `observe()` calls, file-watcher reindex events,
skill validation outcomes.

**Schema:** `events(id, timestamp, kind, actor, payload_json)` —
`src/memex/core/stores/episodic.py`

**Event kinds in use:** `concept_added`, `edge_added`, `recall_executed`,
`skill_validated`, `consolidation_run`, `comment_added`, `auto_approval`,
`auto_deny`, `secret_redacted`, `user_correction`, `file_reindexed`,
`tool_pre`, `tool_post`, `tool_call`, `user_prompt`, `afk_enabled`,
`turn_end`, `policy_added`, `policy_removed`, `source_indexed`,
`skill_installed`, `context_injected`, `task_updated`.

**Contract:** writes are durable before any cognition or action layer
fires. If perception fails, the rest of the loop refuses to run rather
than serving stale state.

### 2. Context — what is true right now

The live working set: which concepts are "hot" this session, what the AI
just touched, which AFK delegation is active, what tasks are open.
Context is derived from the recent perception tail plus the L1 cache.

**Implementation:** `src/memex/core/working_set.py`, plus the
`recent_events` and AFK status surfaces.

**Contract:** context is best-effort and cheap to recompute. A cold start
returns an empty context, not an error. Cognition can run without
context, just less precisely.

### 3. Memory — durable typed graph

The long-term store. Concepts (typed nodes: fact, decision, constraint,
person, task, project, milestone, file, symbol, source, …) and edges
(typed relations: supersedes, depends_on, motivated_by, blocks, part_of,
same_as, …). Vector embeddings live alongside concepts.

**Implementation:** `src/memex/core/stores/` (DuckDB backend),
`src/memex/core/schema.py`.

**Storage:** one DuckDB file. Concepts table holds the 384-d vector as a
column. BM25 index is in-memory, rebuilt lazily from concept rows on
first recall after a write. No external vector DB, no Redis, no Postgres.

**Single-writer rule:** the daemon is the only process that mutates
DuckDB. Desktop, CLI, MCP all go through the HTTP frontend.

**Memory is partitioned by source.** Each indexed codebase, document
collection, or notebook is registered as a `kind=source` concept;
files/symbols/modules belonging to it carry `part_of` edges to that
source. This makes Memory natively multi-repo: one memex node holds
many partitions and queries them together.

**Intra-memex federation: cross-source `same_as` edges.** When the same
symbol exists in two indexed repos (e.g. `Auth` defined in
`authfi-auth-service` and re-exported in `authfi-edge-router`), the
linker creates a `same_as` edge. Recall expansion crosses these edges
transparently, so a query against one repo surfaces relevant matches in
all linked repos. Run via `POST /sources/link` or
`mcp__memex__link_cross_repo_symbols`. Per-source breakdown including
shared-with-N counts is exposed at `GET /sources/{id}/stats`.

**Inter-memex federation (MMP, planned).** The next scale up: your
memex queries your team's memex queries a public-knowledge memex via
the Open Memory Protocol. Same `same_as` primitive, just across
processes. See `docs/omp-spec.md` (forthcoming).

### 4. Cognition — algorithms over memory

What turns "raw graph" into "useful answer." Five sub-systems:

| Sub-system | What it does | File |
|---|---|---|
| Embedding | text → 384-d vector via `all-MiniLM-L6-v2` ONNX | `ml/embeddings.py` |
| Lexical retrieval | BM25Okapi over concept text | `core/retrieval/bm25.py` |
| Hybrid fusion | RRF (K=60) merges BM25 + vector ranks | `core/retrieval/hybrid.py` |
| Recency + confidence | rank weighted by `confidence × recency` (per-kind half-life) | `core/lifecycle/decay.py` |
| Cross-encoder rerank | `ms-marco-MiniLM-L-6-v2` ONNX, top-50 → top-k | `ml/rerank.py` |
| Graph expand | 1-hop neighbors of top-N seeds | `core/retrieval/hybrid.py` |
| Budget | greedy token-bound trim | `core/retrieval/budget.py` |
| Consolidation | scheduled pass: episodic patterns → constraint nodes | `core/lifecycle/consolidation.py` |
| Decay | scheduled pass: confidence drift over time | `core/lifecycle/decay.py` |
| Forgetting | scheduled pass: prune low-value nodes | `core/lifecycle/forgetting.py` |
| Verification | scheduled pass: re-confirm old high-confidence claims | `core/lifecycle/verification.py` |
| LLM (optional) | Ollama (local) or Anthropic API; consolidation + query expansion | `ml/llm.py` |

**Contract:** every recall is budget-bounded. Cognition never blocks on
LLM availability — if the LLM provider is down, expansion is skipped and
recall continues.

### 5. Action — decisions back to the user / agent

What memex actually emits to the outside world.

- **Recalls** served via MCP `recall()` and HTTP `/recall`
- **Auto-approve / auto-deny** of tool calls based on stored policies
- **Context injection** — `additionalContext` written by hooks, fed to
  the AI on next prompt
- **Validate gating** — `validate(skill=…)` checks the constraint
  subgraph before allowing risky tool calls (security, money, crypto,
  SQL, concurrency, untrusted input)

**Implementation:** `src/memex/enforcement/`,
`src/memex/frontends/http/server.py`.

**Contract:** action is the only layer that talks back. If perception
through cognition is healthy but action fails, the loop is a passive
observer — useful but not load-bearing.

## What flows between layers

```
Perception → Context: "this just happened, update the working set"
Context → Cognition: "here's the hot subset, prefer it in ranking"
Cognition → Memory: reads via retrieval; writes via consolidation
Memory → Cognition: provides the substrate
Cognition → Action: "best k concepts under T tokens"
Action → Perception: every action emits an event (closes the loop)
```

The closing edge — Action emits a Perception event — is what makes
memex a *learning* system rather than a static memory store. A user
correction event today reweights confidence; a saved-prompt event
reinforces the recall path that produced it.

## Defensible IP claims

These are the three architectural claims worth filing as defensive
patents and publishing under memex's OSS license. Defensive filing
prevents another vendor from patenting the same idea and forcing memex
contributors to license back what they originated.

**1. Topology-aware budget-bounded hybrid recall.** [shipped]
RRF fusion of BM25 + dense vector + per-kind recency half-life +
per-concept confidence + 1-hop graph expansion + cross-encoder rerank,
all enforced under a single token budget at recall time. Most "AI
memory" products stop at vector + LLM rerank; the typed-graph topology
plus per-kind decay plus single-pass budget enforcement is uncommon.
See `core/retrieval/hybrid.py` and the per-kind half-life in
`config.py:decay_half_life_*_days`.

**2. Episodic-to-durable consolidation promotion.** [shipped]
The promoter that watches the episodic stream for recurring patterns
and writes them into the typed graph as durable rules:
`Engine.promote_patterns()`. Repeated `user_correction` events
targeting the same recalled concept become an `avoid:<name>` constraint
node; repeated `auto_approval` events become a `prefer:<name>`
decision node. Idempotent — re-running won't duplicate. This is the
*learning* mechanism that distinguishes memex from a static memory
store. Closed-loop with the calibration pipeline: every recall →
correction triple both down-weights the concept's confidence AND
contributes to a future promotion.

**3. Memory-as-runtime-safety-check via check_action gating.** [shipped]
`Engine.check_action(intent)` queries the constraint subgraph before
allowing a sensitive tool call to proceed and returns
`{decision: allow|step_up|deny, reasons: [...]}`. A PreToolUse hook
calls this with the AI's stated intent; deny verdicts feed back into
the AI's context as tool errors, prompting self-correction.
Memory becomes enforcement, not documentation. See
`Engine.check_action` and `POST /check_action`.

The three claims compose: claim 1 produces the constraint matches
that claim 3 enforces; claim 2 grows the constraint set the others
operate on. This is the closed-loop architecture worth filing.

## Stack

Everything runs on:

- **DuckDB** — single-file embedded SQL database. Concepts, edges,
  events, vectors-as-arrays.
- **MiniLM-L6-v2** ONNX, 384-d — embeddings, local, no API.
- **MS-MARCO MiniLM-L-6-v2** ONNX — cross-encoder rerank, local,
  optional (set `MEMEX_RERANK_ENABLED=true`).
- **Ollama or Anthropic API** — optional LLM for consolidation + query
  expansion. Loop runs without it.

No Postgres, no Redis, no Qdrant, no external vector service. `pip
install memex && memex daemon` is the entire setup. Air-gapped
deployment is supported when an Ollama instance is reachable on
localhost.

## Workflow verticals

Some user-facing workflows aren't a single layer — they cut all five.
Treat them as named verticals with explicit per-layer touchpoints.

### Project management

Tasks, projects, milestones live in Memory as typed concepts;
dependencies live as typed edges. The vertical exists end-to-end:

| Layer | Touchpoint |
|---|---|
| Perception | `task_updated`, `comment_added` events on every status change |
| Context | the *active task* — what the user is currently working on (planned: surfaced as a first-class Context primitive) |
| Memory | `kind=task` / `kind=project` / `kind=milestone` concepts; `blocks` / `part_of` edges |
| Cognition | `next-actions` ranks pending tasks by priority × due × blocker count |
| Action | `GET /tasks`, `GET /next-actions`, `PATCH /tasks/{id}`, dashboard PM tiles |

This is the canonical example of a workflow vertical: each new vertical
(say, code review, or incident response) should expose the same five
touchpoints rather than collapse into one layer.

### Code-source intelligence

Indexed codebases are first-class. Multi-repo support, cross-repo
linking, and source-scoped recall all live here.

| Layer | Touchpoint |
|---|---|
| Perception | `source_indexed`, `file_reindexed` events |
| Context | "current source" hint when a recall query is scoped to a directory |
| Memory | `kind=source` concepts; `part_of` edges; `same_as` edges across sources |
| Cognition | `recall_code` filtered by source; symbol-aware embedding (planned: code-specific encoder) |
| Action | `POST /sources/add`, `POST /sources/link`, `GET /sources/{id}/stats`, MCP `recall_code` |

## Layer namespaces

The 5-layer architecture is reflected in the package structure. Each
layer is a re-export namespace under `memex.core`:

```python
from memex.core.perception import EpisodicEvent, SqliteEpisodicStore
from memex.core.context    import WorkingSet
from memex.core.cognition  import HybridRetriever, evaluate_against_pairs, build_default_provider
from memex.core.memory     import Concept, NodeKind, DuckDBStores
from memex.core.action     import ApprovalDecision, should_approve, enable_afk_mode
```

Physical files still live in their domain-specific subdirs
(`core/retrieval/`, `core/lifecycle/`, `core/stores/`,
`enforcement/`, `ml/`). The layer namespaces re-export the relevant
public surface so calling code can speak in the architecture's
vocabulary without depending on file layout.

New code SHOULD import via the layer namespace. Existing code keeps
working with its current paths — the layer namespaces don't break
anything; they augment.

## How to extend memex

Pick a layer and add a capability. Don't cross layers.

- **New perception source?** Add an event kind, write a hook or CLI
  command that emits it, document the payload shape.
- **New cognition algorithm?** Add a function in `core/retrieval/` or
  `core/lifecycle/`, wire it into the recall path or a scheduler.
- **New action?** Add an HTTP route or MCP tool in `frontends/`,
  document its event-emission contract.
- **New skill?** Add a bundle under `src/memex/skills/` with the
  validate questions + scoring rubric.

Cross-cutting changes (e.g. "let cognition write directly to perception
to fake events") break the loop and won't be merged.
