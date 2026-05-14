# memex — Quickstart

Persistent AI memory + safety gate. Works with Claude Code, Cursor, Windsurf, Cline — any MCP-compatible AI coding tool.

## Install (5 minutes)

### Option A — pip (Python developers)

```bash
pip install memex
memex doctor          # verify install
memex hooks-install --apply   # wire into Claude Code / Cursor / Windsurf
```

### Option B — Desktop app (everyone else)

Download for your platform from [quefly.com/memex/download](https://quefly.com/memex/download):
- **Windows** — `memex-desktop-windows.exe` (10 MB)
- **macOS** — `memex-desktop-macos.dmg` (12 MB)
- **Linux** — `memex-desktop-linux.AppImage` (11 MB)

Double-click to launch. The desktop bundles the daemon — no separate install.

## First run

Once installed, verify:

```bash
memex doctor
```

You should see something like:

```
memex doctor — 2026-05-13T15:24:54Z
  host:   your-machine
  user:   you@yourcompany.com
  python: 3.12.13

  OK data dir               C:\Users\you\AppData\Local\Quefly\memex
  OK daemon                 http://127.0.0.1:7777 v1.0.0 concepts=0
  OK auth token             loaded
  OK OMP version            v0.1, 5 verbs exposed
  WARN embeddings           degraded — run `memex setup-models`
  WARN LLM hook             no LLM configured (optional)
  OK Claude Code hooks      wired
  OK upstream MCPs          0 installed

doctor: ok with 2 warning(s).
```

Two warnings are expected on first run:

1. **embeddings degraded** — fix with `memex setup-models` (downloads the embedding model, one-time).
2. **LLM hook** — set `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / run Ollama to unlock code generation, autonomous discovery, query expansion. memex works fully without it; these are power features.

## Onboard your team

When a teammate is starting:

1. They install (same `pip install memex` or the desktop binary).
2. You export your project context:
   ```bash
   memex export --project authfi -o authfi-bundle.jsonl
   ```
3. They import:
   ```bash
   memex import authfi-bundle.jsonl
   ```
4. They run `memex doctor` to confirm — and they're operational. Their AI now sees the same project context yours does.

## Common commands

```bash
# Wire memex into your AI tool (run once per machine)
memex hooks-install --apply

# Pre-install useful MCP servers (no tokens required)
memex upstream install fetch
memex upstream install filesystem

# Backup before any migration
memex backup
# → C:\Users\you\AppData\Local\Quefly\memex\backups\20260513-152454\

# Save current project's docs into memex
memex ingest-md ./docs --project myproject

# See what tasks need review
memex review next-due

# Run consolidation (dedup similar concepts)
curl -X POST http://127.0.0.1:7777/maintenance/consolidate \
     -H "Authorization: Bearer $(cat $LOCALAPPDATA/Quefly/memex/daemon.token)" \
     -H "Content-Type: application/json" -d '{}'
```

## Architecture in one paragraph

memex runs a local daemon (`127.0.0.1:7777`) that's both an MCP server (your AI tool connects to it) and an HTTP API. It stores everything in **one DuckDB file** under your data dir — concepts, edges, episodic events, and 384-dim vectors. Five core verbs (`recall`, `remember`, `link`, `observe`, `validate`) match the Open Memory Protocol (OMP v0.1). Every tool call your AI makes flows through memex's PreToolUse hook: action_constraint nodes can block, step_up, or allow each call. Nothing leaves your machine unless you explicitly configure an LLM provider.

## Privacy & data

- **Local-first.** All data stays in `<data_dir>/memex.duckdb`. No phone-home.
- **LLM calls** only happen when you explicitly set `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, or run Ollama. The default install has no external dependencies at runtime.
- **Secrets** never enter the concept graph — memex auto-redacts AWS keys, OpenAI keys, GitHub PATs, JWTs, PEM blocks before they're stored. Values live in the OS keychain; the graph holds `secret://provider/name` handles.

## Troubleshooting

| Symptom | Run |
|---|---|
| Hook blocks a tool call you didn't expect | `memex doctor` to see the rule; PATCH the constraint via `/nodes/{id}` to lower confidence, or delete it |
| Recall returns nothing useful | `memex setup-models` (likely embeddings degraded) |
| 401 errors from MCP | Restart the AI client; `memex doctor` will show "auth token" green once `daemon.token` is on disk |
| Daemon won't start | `memex daemon --listen 127.0.0.1:7777 --foreground` for full logs |
| Need to start fresh | `memex backup` then delete `<data_dir>/memex.duckdb`; daemon will recreate |

## Where to go next

- Add an LLM provider for code regeneration: see "Optional LLM" section
- Install more MCPs: `memex upstream catalog` to see what's available
- Set up team sharing: `memex export` / `memex import` (project-filtered)
- Customize action_constraints: `POST /remember` with `kind=action_constraint`

## Optional LLM

memex stays small and offline by default. Activate code generation, autonomous discovery, and query expansion by setting one of:

```bash
export ANTHROPIC_API_KEY=sk-ant-...         # Claude (recommended)
export OPENAI_API_KEY=sk-...                # GPT-4o
export OLLAMA_HOST=http://127.0.0.1:11434   # local Llama / Mistral / Qwen
```

Restart the daemon. `/code/regenerate`, `memex next-task`, and the discovery worker activate automatically.

## License

memex is Apache 2.0. The Open Memory Protocol spec (docs/omp-spec.md) is CC BY 4.0. See `LICENSE` for the full text.
