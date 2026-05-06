# memex

**Persistent cognitive memory for AI coding tools.**

Your AI coding tool has amnesia. memex fixes that. Local-first, MCP-native, ships with curated **skills** that any AI can install and validate against.

## In one diagram

```
              ┌─────────────────────────────────────┐
              │         memex core engine            │
              │  storage + retrieval + lifecycle     │
              │  + working set + ML — protocol-blind │
              └─────────────────────────────────────┘
                   ▲          ▲          ▲
                   │          │          │
        ┌──────────┴──────┐ ┌─┴────┐ ┌───┴────┐
        │   MCP frontend  │ │ HTTP │ │  CLI   │
        │   (stdio)       │ │ API  │ │        │
        └─────────────────┘ └──────┘ └────────┘
```

## In three sentences

memex is a local daemon that gives Claude Code, Cursor, Windsurf, Cline, and any HTTP-capable AI tool a **shared persistent memory** — graph-structured, budget-bounded, provenance-stamped. It also ships **skills**: installable validation bundles (security, money, concurrency, crypto, SQL, untrusted input) that the AI must self-attest against before generating sensitive code. Every action is logged, every confidence decays over time, every retrieval is bounded by tokens — modeled jointly on RAM hardware (cells, banks, refresh, cache hierarchy) and human memory (encoding, consolidation, reconsolidation, forgetting).

## Where to go next

- **[Quickstart](quickstart.md)** — 60 seconds from install to first recall.
- **[Concepts](concepts.md)** — the RAM ↔ human-memory ↔ memex mapping and the axiomatic derivation of the architecture.
- **[Skills](skills.md)** — what skills are, the three builtin bundles, how to author your own.
- **[Editors](editors/claude-code.md)** — recipes for Claude Code, plus the universal HTTP / Docker recipes.

## Status

**v0.1 — alpha.** APIs may change. File issues on [github.com/queflyhq/memex](https://github.com/queflyhq/memex/issues).

## License

MIT.

---

Made by [Quefly](https://github.com/queflyhq).
