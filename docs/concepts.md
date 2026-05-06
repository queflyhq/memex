# Concepts

The architecture isn't designed — it's *derived*. Start from a small set of axioms about what memory fundamentally is, and the shape of memex is forced by them.

## Axioms

1. **The LLM's effective state is bounded; the work it reasons about is unbounded.** Memory exists to bridge this gap.
2. **Memory has multiple types.** Episodic (events: "on Tuesday I fixed X"), semantic (facts: "we use Postgres"), procedural (how-to: "we deploy via this pipeline"). Collapsing them into one store is the original sin of every RAG system.
3. **Memory has a lifecycle.** Encode → store → retrieve → consolidate → reconsolidate → forget. Most systems implement only the first three.
4. **Truth has a source and a timestamp.** A fact without provenance is a poison pill in a multi-agent system — one bad memory contaminates every downstream retrieval.
5. **A claim that can't be falsified isn't knowledge — it's opinion.** If a fact can't be expressed as a query against ground truth, it must be marked accordingly.
6. **Cognition has a working set.** The brain has global workspace (active attention) + long-term store. Retrieval without a working set is amnesia.
7. **Repetition creates primitives.** Patterns that recur become atomic units (chunking).
8. **Time erodes truth.** Decisions get superseded, services get replaced, APIs change. Stale memory is worse than no memory.
9. **Memory's value is retrieval cost, not storage cost.** Hoarding has negative utility. The right metric is "tokens-per-correct-answer," not "how much do you remember."

## The dual metaphor: RAM hardware + human memory

memex is shaped jointly on the structure of RAM (random-access memory chips) and on what cognitive science says about human memory.

| RAM hardware | Human memory | memex |
|---|---|---|
| Cell (smallest addressable unit) | Engram | `Concept` node |
| Bank (parallel storage region) | Memory system | `stores/episodic`, `stores/semantic`, `stores/vector` |
| Row addressing | Cued recall | `recall(query)` |
| Refresh cycle (DRAM autorefresh) | Reconsolidation | `last_confirmed_at` + working-set `touch()` on retrieval |
| ECC (error correction) | Pattern separation | NLI / conflict detection (v0.5) |
| Cache hierarchy (L1/L2/L3) | Working memory | `WorkingSet` (LRU cache) |
| Bus bandwidth | Attention budget | token-budget enforcement |
| Read/write ports (parallel) | Sensory channels | MCP + HTTP + CLI frontends |
| Wear leveling | Consolidation (episodic → semantic) | `lifecycle/consolidation.py` |
| DMA (direct CPU bypass) | Priming | MCP stdio (zero-copy editor → engine) |
| Page table (address translation) | Naming/aliasing | concept IDs + `same_as` edges |

## What's forced by the axioms

### Multi-store, not single graph (axiom 2)
Three separate stores with different schemas, different decay rates, different retrieval defaults:

- **Episodic**: time-indexed event log (sessions, conversations, incidents) — sqlite with btree on timestamp
- **Semantic**: typed property graph (concepts, decisions, contracts) — Kuzu
- **Procedural**: ordered workflows (deploys, runbooks) — sqlite (v0.4)

### Multi-encoding per fact (axiom 3)
Every node stored as: text, embedding, graph node, **and** an executable predicate. Retrieval picks encoding by query type.

### Provenance-or-die (axiom 4)
Every node and every edge carries `source`, `created_at`, `last_confirmed_at`, `confidence`. The schema rejects nodes without these.

### Falsifiability requirement (axiom 5)
Every node has an optional `verification` predicate. Nodes without verification are downgraded over time. This prevents drift.

### Working set as first-class structure (axiom 6)
`WorkingSet` is a bounded LRU cache that biases retrieval ranking — recently-touched concepts get higher rank (priming). Modeled on CPU L1 cache and human working memory simultaneously.

### Chunking via co-retrieval (axiom 7)
Track which nodes are retrieved together. When co-retrieval frequency crosses a threshold, the subgraph compresses into a chunk node. Knowledge compresses without losing detail. (v0.4)

### Time-aware confidence + active forgetting (axiom 8)
Confidence decays on an Ebbinghaus exponential (default half-life 90 days) unless re-confirmed. Active forgetting (v0.4) deletes nodes that meet stale-and-orphaned criteria.

### Retrieval is budget-bounded by construction (axiom 9)
Every retrieval is `(query, token_budget) → minimal subgraph`. There's no "give me everything." The API doesn't allow unbounded retrieval.

## Foundational data structures

Each layer has a CS-foundational data structure underneath:

- **Property graph** (Kuzu) — typed nodes + typed edges, primary key indexing
- **Inverted index** (rank_bm25) — token → docs mapping for keyword retrieval
- **Brute-force k-NN with cosine** (numpy) — at v0.1 scale; HNSW upgrade is a Protocol-conforming swap
- **LRU cache** (`OrderedDict`) — `WorkingSet`, the L1 layer
- **Time-indexed btree** (sqlite) — episodic events, ordered by timestamp
- **Reciprocal Rank Fusion** — combining BM25 + vector ranks; provably parameter-stable (`k=60`)
- **Exponential decay** — Ebbinghaus forgetting curve, `confidence(t) = base * 0.5^(days/half_life)`

## Software architecture: SOLID with Protocols

The foundations:

- **Repository pattern** with **Protocol-based interfaces** (`core/protocols.py`) — every store defined as a Protocol so v0.2 swaps don't require core changes
- **Constructor dependency injection** — `Engine` takes its stores + retriever + provider as constructor args; never instantiates them
- **Factory method** — `Engine.build_default()` for the conventional wiring; tests bypass it with fakes
- **Strategy pattern** — retrieval strategies (BM25, hybrid) interchangeable via `RetrievalStrategy` Protocol
- **Provider pattern** — embeddings as `NoOpEmbeddingProvider` + `FastEmbedProvider`, swappable at runtime
- **Façade** — `Engine` as the single entry point hiding store/ml/retrieval orchestration; frontends never reach into stores
- **Layered architecture** — frontends → engine → core → stores; never the reverse
- **Lazy loading** — heavy resources (embedding models) load on first use, not at import
- **Fail loud** — HTTP daemon refuses non-localhost binding without auth (no silent insecurity)

## Polyglot by design

The Protocol-based interfaces mean any layer can be reimplemented in any language and dropped into the same architecture. v0.1 ships all-Python because that's the fastest path to correctness; future hot paths (vector cosine SIMD, graph traversal) can be replaced module-by-module without touching anything else.

The HTTP API is the universal cross-language boundary. Any client in any language can drive memex over `http://localhost:7777`. OpenAPI spec at `/openapi.json` autogenerates bindings.

## Watchful, not passive

memex isn't a database the agent queries — it's an active observer:

- Every `add` / `link` / `recall` / `validate` auto-emits an episodic event
- `progress()` exposes the full activity log
- The AI can introspect *what it's already done* and avoid repeating itself
- Future versions detect contradictions between memories and surface them

## Roadmap

| Version | Theme |
|---|---|
| v0.1 | Foundations — graph + BM25 + MCP/HTTP/CLI + 3 builtin skills |
| v0.2 | Skills registry — community skills, external repo |
| v0.3 | Passive distillation — sessions become memory automatically |
| v0.4 | Self-curation — consolidation, decay, dedup, contradiction detection |
| v0.5 | Code-verified confidence — verification pass against actual code state |
| v0.6 | Team mode + AuthFI — server-deployed, identity-aware |
| v1.0 | Counterfactual reasoning — simulate consequences of edits |
