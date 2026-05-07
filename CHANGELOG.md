# Changelog

All notable changes to memex are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.6.0.dev0] — unreleased

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
