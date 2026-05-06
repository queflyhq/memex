# Claude Code

Claude Code speaks MCP natively. Setup is one line.

## Install memex

```bash
pipx install memex
# or
uv tool install memex
```

## Wire memex into Claude Code

```bash
claude mcp add memex memex serve
```

That's it. The next Claude Code session has these tools available:

- `recall(query, budget_tokens, kind, expand_hops)` — retrieve a budget-bounded subgraph
- `add_node(name, description, kind, source, confidence, verification)` — add a concept
- `link(from_id, to_id, kind, source)` — create a typed edge
- `observe(kind, actor, payload)` — emit an episodic event
- `validate(skill, actor)` — get the structured `approach + checks` for a skill
- `progress(actor)` — what's been done in this store
- `list_skills()` — list installed skill bundles
- `install_skill(name)` — install a builtin skill bundle
- `stats()` — storage stats and tier status

## Bootstrap your install with the meta-skills

```bash
memex install skill:using-memex
memex install skill:core-validations
```

The first skill teaches Claude Code how to use memex. The second gives it validation rules for every sensitive domain (security, money, concurrency, crypto, SQL, untrusted input).

## Verify

In a Claude Code session, ask:

> What memex tools do you have available? Run `stats()` to confirm.

Claude Code should list the tools above and return your memex storage stats.

## Tips

- **Always recall first.** Tell Claude Code (in your project `CLAUDE.md` or your prompt) to call `recall(<topic>)` before generating code in non-trivial contexts.
- **Always validate sensitive domains.** Tell Claude Code to call `validate(<approach>)` before generating subprocess, money, async, crypto, SQL, or HTTP-input code.
- **Always observe decisions.** Tell Claude Code to call `observe(kind="decision", payload={...})` whenever it makes a non-trivial choice. The activity log builds up automatically.
- **Persistent across sessions.** Memex's storage is at `~/.memex/` (or `%LOCALAPPDATA%/memex/` on Windows) — what one session adds, the next session can recall, even after Claude Code restarts.

## Multi-tool: also wire Cursor / Windsurf

Memex storage is shared across all editors that point at the same daemon. Cursor / Windsurf MCP configs:

```json
{
  "mcpServers": {
    "memex": {
      "command": "memex",
      "args": ["serve"]
    }
  }
}
```

Drop this in `~/.cursor/mcp.json` (Cursor) or your Windsurf settings. Multiple editors can run their own `memex serve` — they all read/write the same on-disk graph.
