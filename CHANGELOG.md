# Changelog

All notable changes to memex are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] — 2026-05-13 (revised)

Major delta on top of the 2026-05-08 cut. The gate that was the central
quality issue is fully reworked; OMP v0.1 is fully conformant; the
cognitive view, code regeneration, and team-rollout CLI are now in place.

### Added (delta from 2026-05-08)

**OMP v0.1 conformance**
- `GET /version` for federation negotiation
- `POST /remember` + `/omp/remember` — idempotent on `(name, kind, source)`, preserves prior descriptions in `metadata.previous_descriptions`, emits `concept_revised` events
- `POST /validate_action` + `/omp/validate` — OMP §4.5 names for the action gate
- Canonical error envelope `{error, message, retry_after, details}` (§9)
- Recall strategy enum restricted to spec values (§4.1)
- Per-verb p99 latency histograms in `/stats.latency` (§6)

**Gate rework**
- New `kind=action_constraint` — only kind consulted by the gate; plain `constraint` nodes never gate
- `applies_to`, `match_pattern`, `verdict`, `scope`, `project` metadata
- Project-scoped constraints (global vs per-project)
- `step_up` verdict path — surfaces "user confirmation required" to AI
- Richer deny payload (id + name + snippet + confidence)
- AFK respect + `MEMEX_DISABLE_CHECK_ACTION` kill switch
- 5 baseline action_constraints seeded

**Recall, cognition, lifecycle**
- `GET /recall_bundle` — cognitive view (primary + defined_in + same_as + callers + callees + decisions + constraints + tests + notes)
- `POST /code/regenerate` — recall_bundle + LLM heavy tier drafts unified diffs (LLM-gated)
- `GET /review/next-due` — spaced repetition queue
- Real consolidation pass (was a stub) — name-dedup + co-occurrence
- Retrieval-induced forgetting (opt-in `MEMEX_RIF=1`)
- Autonomous discovery worker scaffold (opt-in `MEMEX_DISCOVERY=1`)
- Episodic rollup (`/maintenance/episodic-rollup`) for GB-scale stores

**LLM hook (opt-in)**
- Anthropic / OpenAI / Ollama providers with heavy/light model tiers
- `MEMEX_LLM` explicit selector
- NoOp fallback — memex never blocks on missing LLM

**CLI**
- `memex doctor` — 8-check diagnostic, teammate-onboarding command
- `memex backup` — checkpoint-aware snapshot
- `memex export` / `memex import` — project-filtered JSONL bundles
- `memex ingest-md` — recursive markdown ingestion
- `memex setup-models` — local embedding model bundle
- `memex train-reranker` — fine-tune cross-encoder on memex's own pairs

**Provenance**
- User identity auto-capture (`_user_id_from_context`)
- Ticket auto-detection from git branch (`AUTH-123`)
- Project auto-detection from cwd / `.memex.json` / env / project nodes
- User-prompt events now carry user_id + project + ticket metadata

**Schema**
- New kinds: `action_constraint`, `ephemeral`
- New Source: `system`
- `find_by_name_kind_source` lookup
- Branded short IDs: `mx_<7 hex>` (10 chars total)

**Auto-promote**
- Every successful `call_upstream` saves the result as `kind=fact` (idempotent on call hash)

### Fixed (delta from 2026-05-08)
- `/upstreams/catalog` was returning HTTP 500 — `CatalogEntry` is `@dataclass`, switched to `dataclasses.asdict`
- MCP auth-token disk fallback — `get_settings()` now resolves from `daemon.token` when env var unset
- Constraint gate misfire — prose principles were gating tool calls; fixed by introducing `action_constraint` kind
- TodoWrite-sync now idempotent (was creating dupes)
- recall_bundle name lookup expanded to all kinds
- Consolidation no longer a literal stub

### Migrated (delta from 2026-05-08)
- 58 file-memory entries from `~/.claude/projects/.../memory/` → memex
- 31 workspace projects seeded with paths + workspace
- 5 action_constraint rules baseline
- Ayush project scaffolding (microservice topology)

### Known limitations (delta from 2026-05-08)
- Embedding tier degrades to BM25 due to fastembed/onnxruntime weight-tensor mismatch — pin a known-good pair
- ID migration of 8K legacy `c_xxx` IDs not yet run (forward-only)
- `recall_bundle.blame` requires `git` upstream MCP installed
- Task sequencing (priority/due_at/order) not yet honored in `/tasks` sort
- Cross-microservice change propagation view not built

---

## [1.0.0] — 2026-05-08

**memex is generally available.** MIT licensed, local-first, zero telemetry. The pre-1.0 development line consolidates into this stable cut. All public APIs (CLI, HTTP, MCP) follow semantic versioning from here on.

### Added

- **Per-machine auth token bootstrap** (`src/memex/runtime_state.py`). On first daemon start memex generates a secure random token at `<data_dir>/daemon.token` (mode `0o600` on POSIX), reads it on subsequent runs, and exposes a discovery file `<data_dir>/daemon.url`. The Wails desktop app and CLI clients pick these up automatically — no env var required for local use. See [SECURITY.md](SECURITY.md) for the trust model.
- **Cross-platform Wails desktop builds** (`.github/workflows/desktop.yml`) — Windows / macOS-universal / Linux artifacts on every tag, attached to the GitHub release. Unsigned in v1.0; code signing tracked separately.
- **PyPI trusted-publishing workflow** (`.github/workflows/pypi.yml`) — wheel + sdist on every `v*.*.*` tag via OIDC, no API tokens stored in the repo.
- **Cosign-signed Docker images + CycloneDX SBOM attestations** (`.github/workflows/docker.yml`). Verify with `cosign verify quefly/memex:1.0.0 --certificate-identity-regexp "https://github.com/queflyhq/memex/.+" --certificate-oidc-issuer https://token.actions.githubusercontent.com`.
- **Coverage gate in CI** — `pytest --cov-fail-under=70` on every push.
- **Issue + PR templates** (`.github/ISSUE_TEMPLATE/{bug,feature,config}.yml`, `.github/PULL_REQUEST_TEMPLATE.md`).
- **`SECURITY.md`** — vulnerability reporting policy + response timeline + threat model.
- **`PRIVACY.md`** — explicit "no telemetry" statement, data-dir layout, opt-in topology for the future team mode.
- **`CODE_OF_CONDUCT.md`** — adopts Contributor Covenant 2.1 by reference; reports go to `conduct@quefly.com`.
- **`.pre-commit-config.yaml`** — ruff lint + format + standard hygiene hooks.
- **Documentation site at [quefly.com/docs/memex](https://quefly.com/docs/memex)** — quickstart, concepts, skills, editor recipes, and a full HTTP API reference.

### Changed

- Version bumped from `0.6.0.dev0` to `1.0.0`. PyPI classifier updated to `Development Status :: 5 - Production/Stable`.
- Roadmap repositioned around v1.0 GA: consolidation (v1.1), self-curation (v1.2), code-verified confidence (v1.3), team mode + AuthFI (v2.0).
- Desktop app's `spawnDaemon` no longer flashes a Windows console window — `SysProcAttr{HideWindow: true, CreationFlags: CREATE_NO_WINDOW}` set on the child process.

### Migration notes

- `0.x` users upgrade in place. No data migration required.
- The first daemon start after upgrading mints `<data_dir>/daemon.token` if `MEMEX_AUTH_TOKEN` is unset. Existing scripts that hit the daemon over HTTP without a token continue to work for now (the daemon honors the token only when it has one), but should switch to reading the file or setting `MEMEX_AUTH_TOKEN` for forward compatibility.
- `mkdocs` site at `docs/` is superseded by [quefly.com/docs/memex](https://quefly.com/docs/memex). The `docs/` directory in the repo remains as the source-of-truth markdown until v1.1.

## [0.6.0.dev0] — superseded by 1.0.0

This release reframes memex from "Claude Code memory" to **centralized agentic memory** — the single store any LLM agent (Claude Code, Cursor, Windsurf, Cline, custom Python/TS agents, autonomous loops) connects to. The headline addition is the **MCP gateway**: memex now re-exports tools from upstream MCP servers the user owns, and automatically captures every proxied call as an episodic event. That auto-capture is the perception primitive future consolidation passes (v0.7) will turn into semantic facts.

### Added

- **MCP gateway** (`src/memex/upstreams/`):
  - `Aggregator` runs upstream MCP clients on a dedicated background event loop (anyio `BlockingPortal`) so synchronous FastMCP tool handlers can dispatch to async upstream sessions cleanly.
  - `UpstreamConnection` lifecycle: stdio subprocess + MCP `ClientSession` + tool listing. Failures per-upstream are logged and skipped — a dead upstream cannot take memex down.
  - Two MCP tools registered alongside native ones when upstreams are configured:
    - `list_upstream_tools()` — full catalog of every proxied tool with schemas.
    - `call_upstream(upstream, tool, arguments)` — dispatches and auto-`observe()`s the call as `kind="tool_call"` (with arguments scrubbed for size).
- **Curated catalog** (`memex.upstreams.catalog` + `catalog.json`) — 18 popular MCP servers (filesystem, git, github, slack, linear, postgres, kubernetes, sentry, …). Browse with `memex upstream catalog`; install with `memex upstream install <id>`.
- **CLI surface**:
  - `memex upstream catalog [--category <c>]`
  - `memex upstream show <id>`
  - `memex upstream install <id> [--name <override>]`
  - `memex upstream add <name> --command … --arg … --env KEY=VAL`
  - `memex upstream list`
  - `memex upstream test <name>` — connect, list tools, disconnect
  - `memex upstream remove <name>`
- **Loud-failure recall**: `RecallResult` now carries `degraded: bool` and `degraded_reason: str | None`. Set when retrieval ran without semantic vectors (embed tier missing, vector store empty, query embedding failed). Honors the project-wide "no silent fallbacks" principle.
- **Honest `memex doctor`**: distinguishes between *missing* fastembed, *failed* native lib load (Windows VC++ redist), and *missing* onnxruntime. Suggests the right fix per cause. Also shows configured upstreams and a hint to browse the catalog.
- **HTTP upstream support** (`src/memex/upstreams/`): the gateway now connects to remote MCP servers, not just stdio subprocesses.
  - New `UpstreamHTTPConnection` (alongside the renamed `UpstreamStdioConnection`) uses the MCP SDK's Streamable HTTP client. Both share a common lifecycle interface (`open` / `call` / `close`).
  - `UpstreamConfig` schema extended: `type: "stdio" | "http"`, `url`, `headers`, `timeout_seconds`, plus a new `auth: AuthConfig` block. Auth kinds: `bearer`, `header`, `oauth2`, `none`. Tokens resolve from `token` (literal — discouraged), `token_env` (env var — recommended), or `token_keychain` (OS keychain via `keyring` — desktop). Loud failure when the env var or keychain entry is missing.
  - `UpstreamConnection` retained as a back-compat alias for `UpstreamStdioConnection` so external callers don't break.
  - `Aggregator.start()` dispatches to the correct connection class by `type`. A failing connection (subprocess crash, HTTP 401, network unreachable) is logged + skipped — gateway uptime is preserved.
- **Catalog: rich teaching fields** (`CatalogEntry`): each entry can now carry `long_description` (markdown body), `memex_value` (what the gateway uniquely adds for this MCP), `setup_steps` (ordered onboarding), `usage_examples` (`{tool, what, example}` triples), and `paired_skills` (skill IDs that activate alongside the MCP). Old entries with `description` keep loading via a back-compat `summary` alias.
- **`github-remote` catalog entry** as the first HTTP exemplar — GitHub's hosted remote MCP at `https://api.githubcopilot.com/mcp/` with bearer auth from `GITHUB_TOKEN`. Same tool surface as the local `github` server, zero local subprocess.
- **`memex upstream show`** rewritten to render the rich teaching fields beautifully — long description, memex value-add, numbered setup steps, common-tools table, paired skills, and runtime detail (URL + auth for HTTP entries; command + env for stdio).
- **`memex upstream add --http <url> --bearer-env <ENV>`** — manually register a remote HTTP upstream from the CLI without going through the catalog.
- **`memex setup` wizard** (`src/memex/integrations.py`) — auto-detects Claude Code (`~/.claude.json`), Cursor (`~/.cursor/mcp.json`), Windsurf (`~/.codeium/windsurf/mcp_config.json`), and Cline (VS Code globalStorage). For each detected tool it offers to wire memex into the tool's `mcpServers` map with a single confirmation. Supports `--yes` (no prompts), `--only "<name>"` (one tool), and `--dry-run` (preview the diff without writing). Atomic writes preserve every other key in the target config — Claude's 23 KB user config goes untouched outside `mcpServers.memex`. This is the cross-tools-continuity surface: install once, every AI tool the developer uses points at the same memex.

### Changed

- Description in `pyproject.toml` rephrased to "Centralized agentic memory for any LLM/agent".
- README rewritten lead paragraph; new "MCP gateway" section with catalog table; roadmap repositioned (gateway → consolidation → team mode).
- `Engine.recall` now reports the *reason* recall is degraded rather than just falling back silently.
- `memex upstream catalog` and `memex doctor` now show a `type` column and a `target` column (URL for HTTP, command for stdio) instead of assuming stdio command shape.

### Known limitations

- **Single-writer lock**: while the memex MCP server is running, the CLI cannot also write (DuckDB allows one writer). Works around by stopping the MCP server before running `memex add` from the CLI. The proper fix — daemon-as-arbiter — is queued for v0.8.
- **Per-tool registration not exposed yet**: upstream tools surface via the meta-tools `list_upstream_tools` + `call_upstream`, not as one MCP tool per upstream tool. FastMCP's schema generation is signature-based; per-tool exposure requires the lower-level `Server` API and lands in v0.7.
- **Upstream auto-reconnect**: a crashed upstream subprocess (or HTTP server) is not restarted automatically. Restart memex to recover. Auto-reconnect is a v0.7 concern.
- **OAuth flow for HTTP upstreams is desktop-only** (v0.6.5+): the daemon resolves tokens from env vars or the OS keychain at connect time, but the *acquisition* of those tokens (browser-redirect dance for HTTP MCPs that require OAuth2) lives in the upcoming desktop app. CLI-only users can still use HTTP upstreams that accept long-lived bearer tokens.
- **Catalog enrichment is partial**: the rich teaching fields (`long_description`, `memex_value`, `setup_steps`, `usage_examples`, `paired_skills`) are populated for `github` and `github-remote` as the new pattern. Other catalog entries still load with their existing minimal data — a follow-on authoring pass fills the rest.

### Migration notes

- Existing memex installs upgrade in place. No data migration required.
- The `RecallResult` JSON shape gains two optional fields (`degraded`, `degraded_reason`); existing consumers that only read `nodes` / `edges` are unaffected.
- AI clients should be restarted (so the MCP server picks up new code) and the upstream config (`~/.memex/upstreams.json`) created via `memex upstream install …` before the gateway tools appear.
- Existing `upstreams.json` files keep working unchanged — `type: "stdio"` remains the default. Adding HTTP upstreams is purely additive.
- Code that imported `UpstreamConnection` from `memex.upstreams` still works; the symbol is preserved as an alias for the renamed `UpstreamStdioConnection`.

## [0.1.0] — 2026-04

Initial alpha. Engine façade, Kuzu graph store, SQLite episodic store, NumPy vector store, BM25 hybrid retrieval, working set, time-decay lifecycle, MCP stdio + HTTP daemon + CLI frontends, three skill bundles (`using-memex`, `core-validations`, `python-stdlib`), `memex install / bootstrap / doctor / progress / validate`.
