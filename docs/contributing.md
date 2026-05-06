# Contributing

Issues and PRs welcome — [github.com/queflyhq/memex](https://github.com/queflyhq/memex).

## Dev setup

```bash
git clone https://github.com/queflyhq/memex.git
cd memex
uv sync --all-extras
uv run pytest
uv run memex --help
```

## Running tests

```bash
uv run pytest -v
```

## Linting

```bash
uv run ruff check .
uv run ruff format .
```

## Where to add things

- **A new builtin skill bundle**: `src/memex/skills/builtin/<your_skill>/` with `__init__.py`, `manifest.json`, `concepts.jsonl`. The directory name must be a valid Python identifier (use underscores); the `name` field in `manifest.json` is the display name (use hyphens).
- **A new retrieval strategy**: implement `RetrievalStrategy` Protocol in a new module under `src/memex/core/retrieval/`; wire it via `Engine.build_default()` or pass it directly.
- **A new store backend**: implement the relevant Protocol from `core/protocols.py` (e.g. `SemanticStore`, `VectorStore`); pass it to `Engine(...)` directly.
- **A new frontend (e.g. gRPC)**: add `src/memex/frontends/grpc/server.py`, instantiate with an injected `Engine`.

## Architectural rules

- **Frontends call into Engine, never into stores or retrieval directly.**
- **Stores implement Protocols, not inherit from a base class.**
- **No module-level state in frontends — class-based, constructor-injected.**
- **Time-decay computed on read, not via background mutation.**
- **Heavy resources (embedding models) load lazily.**
- **HTTP daemon refuses non-localhost binding without an auth token. No silent insecurity.**

## License

By contributing you agree your contribution is licensed under the MIT License.
