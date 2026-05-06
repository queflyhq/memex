# Contributing to memex

Issues and PRs welcome. Brief guidelines below; full developer docs at [queflyhq.github.io/memex/contributing](https://queflyhq.github.io/memex/contributing/).

## Setup

```bash
git clone https://github.com/queflyhq/memex.git
cd memex
uv sync --all-extras
uv run pytest
```

## Architectural rules

- **Frontends call into Engine, never into stores or retrieval directly.**
- **Stores implement Protocols (`core/protocols.py`), not inherit from a base class.**
- **No module-level state in frontends — class-based, constructor-injected.**
- **Time-decay computed on read, not via background mutation.**
- **Heavy resources (embedding models) load lazily on first use.**
- **HTTP daemon refuses non-localhost binding without an auth token. No silent insecurity.**
- **Activity events on every `add` / `link` / `recall` / `validate` — the audit trail must stay complete.**

## Where to add things

| You want to add | Where |
|---|---|
| A new builtin skill bundle | `src/memex/skills/builtin/<your_skill>/` (3 files) |
| A new retrieval strategy | new module in `src/memex/core/retrieval/`, implement `RetrievalStrategy` Protocol |
| A new store backend | new module in `src/memex/core/stores/`, implement the relevant Protocol |
| A new frontend (e.g. gRPC) | new package in `src/memex/frontends/`, instantiate with an injected `Engine` |
| A new editor recipe | `docs/editors/<editor>.md` |

## License

By contributing you agree your contribution is licensed under the MIT License.
