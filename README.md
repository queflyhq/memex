# memex

**Persistent cognitive memory for AI coding tools.**
Your AI coding tool has amnesia. memex fixes that. Local-first, MCP-native, ships with curated **skills** that any AI can install and validate against.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)

---

## Why memex

Every new session in Claude Code, Cursor, Windsurf, Cline, and others starts cold. Project conventions, decisions, prior failures, and architectural rationale evaporate. `CLAUDE.md` and `.cursorrules` fight back, but they auto-load *every* turn — so they cost tokens whether relevant or not, and they don't sync between tools.

memex is a local daemon with a graph-structured memory the agent retrieves from on demand. The agent calls `recall("auth flow")` and gets back a budget-bounded, confidence-weighted subgraph — only what's relevant, only when needed. It also ships **skills** — installable validation bundles — so the AI must self-attest checks before generating security-, money-, or concurrency-sensitive code.

## What's different

- **Cognitive architecture, not just RAG.** Multi-store (episodic / semantic / procedural), provenance on every node, falsifiable claims, time-decay, working set. Modeled on both RAM hardware (cells, banks, refresh, ECC, cache hierarchy) and human memory (encoding, consolidation, reconsolidation, forgetting curve). See [docs/concepts.md](docs/concepts.md).
- **Skills with validation.** `memex install skill:core-validations` ships globally-applicable engineering principles (secure subprocess, money-decimal precision, structured concurrency, crypto-secrets, SQL parameterization, …) — language-agnostic. `validate("secure-subprocess")` returns the structured `approach + checks` the AI must self-attest before generating code.
- **Watchful, not passive.** Every `add` / `link` / `recall` / `validate` auto-emits an episodic event. `progress()` exposes the full activity log. The AI can introspect what it's already done and avoid repeating itself.
- **Local-first, runs anywhere.** Tier 0 install is ~50 MB, no model downloads, no GPU, BM25 retrieval out of the box. Tier 1 adds embeddings (~80 MB). Tier 2+ optional.
- **MCP + HTTP + CLI.** One daemon, every editor. Claude Code, Cursor, Windsurf, Cline via MCP; everything else via HTTP at `localhost:7777`. Polyglot-friendly: any language can drive memex over HTTP.
- **Self-hosted by default.** No cloud, no telemetry, no API keys. Your memory never leaves your machine.

## Install

```bash
# Recommended (pipx — works on any Python 3.10+ system)
pipx install memex

# Or with uv
uv tool install memex

# Or Docker (no Python required)
docker run -d --name memex \
  -v $HOME/.memex:/data \
  -p 127.0.0.1:7777:7777 \
  quefly/memex:latest
```

## 60-second quickstart

```bash
# 1. Install the bootstrap meta-skill so any AI knows how to use memex
memex install skill:using-memex
memex install skill:core-validations

# 2. Add your first memory
memex add "AuthFI uses Reach for tunneling, not WireGuard" --kind decision

# 3. Recall it
memex recall "how does auth tunnel work?"

# 4. Validate against an approach before generating sensitive code
memex validate secure-subprocess

# 5. See what's been done
memex progress

# 6. Wire it into Claude Code
claude mcp add memex memex serve

# 7. (Optional) Upgrade to vector retrieval
pipx install --force 'memex[embed]'
```

That's it. Claude Code can now `recall`, `validate`, `observe`, and `add` against your persistent graph.

## Editor setup

| Editor | One-line setup |
|---|---|
| Claude Code | `claude mcp add memex memex serve` |
| Cursor | [docs/editors/cursor.md](docs/editors/cursor.md) |
| Windsurf | [docs/editors/windsurf.md](docs/editors/windsurf.md) |
| Cline (VS Code) | [docs/editors/cline.md](docs/editors/cline.md) |
| Any HTTP-capable client | [docs/editors/http.md](docs/editors/http.md) |
| Docker daemon | [docs/editors/docker.md](docs/editors/docker.md) |

## Architecture

```
              ┌─────────────────────────────────────┐
              │         memex core engine            │
              │  (storage + retrieval + lifecycle    │
              │   + working set + ML — protocol-blind)│
              └─────────────────────────────────────┘
                   ▲          ▲          ▲
                   │          │          │
        ┌──────────┴──────┐ ┌─┴────┐ ┌───┴────┐
        │   MCP frontend  │ │ HTTP │ │  CLI   │
        │   (stdio)       │ │ API  │ │        │
        └─────────────────┘ └──────┘ └────────┘
```

- **Graph store**: Kuzu, embedded — provenance + confidence + time-decay built in.
- **Vector store**: sqlite + numpy brute-force at v0.1. HNSW upgrade is a Protocol-conforming swap.
- **Retrieval**: hybrid BM25 + vector + graph traversal, budget-bounded, with reciprocal rank fusion.
- **Working set**: bounded LRU cache (RAM-style L1) — recently-touched concepts get retrieval bias.
- **Frontends**: MCP stdio for AI editors, HTTP for everything else, CLI for shells.
- **Skills**: installable validation bundles. AI calls `validate(skill)` and self-attests checks.

The architecture is built on **SOLID + foundational data structures**: protocol-based interfaces (Repository pattern), constructor dependency injection, strategy pattern for retrieval, provider pattern for embeddings, factory method for default wiring. Every layer has a CS-foundational data structure: property graph (Kuzu), inverted index (BM25), brute-force k-NN, LRU cache, time-indexed btree (sqlite), exponential time decay (Ebbinghaus). Polyglot-friendly: any layer can be reimplemented in any language behind its Protocol.

See [docs/concepts.md](docs/concepts.md) for the full RAM-hardware ↔ human-memory ↔ memex mapping and axiomatic derivation of the architecture.

## Roadmap

| Version | Theme | What's new |
|---|---|---|
| **v0.1** | Foundations | Graph + BM25 + MCP/HTTP/CLI + skills (using-memex, core-validations, python-stdlib) + working set + activity log |
| **v0.2** | Skills registry + repo bootstrap | `queflyhq/memex-skills` external repo + community skills (aws-iam, gcp-essentials, postgres-ops, go-idioms, …); auto-import any repo's `.memex/skills/<name>/` directory or `*.memex.{yml,json,jsonl}` files on first scan — drop knowledge files in your repo and memex picks them up |
| **v0.3** | Passive distillation + intent tracking | Sessions become semantic memory automatically (episodic → semantic consolidation); memex remembers user instructions + AI outcomes + flags when AI deviates from instructed approach |
| **v0.4** | Self-curation | Background consolidation, decay, dedup, contradiction detection |
| **v0.5** | Code-verified confidence | Memory grounded in actual code state — falsifiability checked by background pass |
| **v0.6** | **Team mode + AuthFI** | `memex daemon` deployed on a team server; AuthFI handles SSO + member identity; every node carries `actor=<authfi_user_id>`; AI knows who decided what, when, why |
| **v1.0** | Counterfactual reasoning | Memory that simulates consequences of edits before they happen |

## Team mode (v0.6)

When deployed on a server, memex becomes a team knowledge base. Every concept, decision, and validation is attributed to a user. AI tools can answer:

- *"What is Alice working on?"* — query episodic events filtered by actor
- *"Who decided we'd use Postgres over MySQL?"* — query semantic graph for the decision node, read its `source` and provenance metadata
- *"What approaches has Bob already validated this week?"* — `progress(actor="bob@team.com")`

Auth is handled via [AuthFI](https://authfi.app) — every memex deployment gets an AuthFI tenant for free, so identity, SSO, and member roles are managed without rolling your own. Bring your team's directory or stay invite-only.

## Status

**v0.1 — alpha.** APIs may change. Use it, file issues, send PRs.

## License

MIT — see [LICENSE](LICENSE).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Issues and PRs welcome.

---

Made by [Quefly](https://github.com/queflyhq).
