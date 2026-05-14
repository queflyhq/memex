# memex usage rules

These rules tell your AI editor to use memex as its persistent memory.
Drop this file at the right path for your editor:

- **Cursor**: copy to `.cursor/rules/memex.mdc` or paste contents into `.cursorrules`
- **Windsurf**: copy to `.windsurfrules` at the project root
- **Cline**: paste into Cline's "Custom Instructions" settings
- **Claude Code**: already wired via hooks — you don't need this file (memex captures automatically)

---

You have access to a local AI memory daemon called **memex** via MCP tools
prefixed `mcp__memex__*`. memex persists knowledge across sessions, repos,
and editors. Treat it as your long-term cognition, not an optional cache.

## Habits you should form

### 1. RECALL before researching

Before saying "I don't know X" or re-reading files you read yesterday, call:

```
mcp__memex__recall(query="<topic>")
```

If memex has it, you save the user tokens + your context window. Memex
typically returns in <1 second.

### 2. REMEMBER after learning

When you learn something the next session should know — a decision the user
made, a constraint the codebase has, a pattern that worked, a fact about
the stack — call:

```
mcp__memex__add_node(name="<short label>", kind="decision|fact|constraint",
                     description="<details>", source="agent")
```

If you don't, future-you will repeat the same question.

### 3. CONNECT related concepts

When two concepts relate (one supersedes another, one depends on another),
call:

```
mcp__memex__link(from_id="...", to_id="...", kind="supersedes|depends_on|relates_to")
```

A pile of disconnected nodes is a notebook; a connected graph is memory.

### 4. OBSERVE meaningful events

When something happens that matters — a decision was made, an error
occurred, a user correction was received — call:

```
mcp__memex__observe(kind="<event_kind>", payload={...})
```

The episodic stream is what makes "what did we do yesterday" answerable.

### 5. VALIDATE before generating

When the request involves security, money, concurrency, crypto, SQL, or
untrusted input, call:

```
mcp__memex__validate(skill="<skill_name>")
```

The skills system is your safety check before generating risky code.

## When to use TASK tools instead of TodoWrite

If you're tracking persistent work that should survive across sessions,
use memex's task system, NOT the built-in TodoWrite (which is session-local):

```
mcp__memex__add_task(name="...", priority="high|medium|low", project="...")
mcp__memex__list_tasks(status="pending|in_progress")
mcp__memex__update_task(task_id="...", status="completed")
mcp__memex__next_actions(limit=5)
```

## Quick recipe — start of every session

When the user gives you their first prompt of a session:

1. `mcp__memex__recall(query="<extract topic from the prompt>")` — pull
   relevant context
2. `mcp__memex__next_actions(limit=5)` — see what's already queued
3. Then answer with full context in hand

This makes you behave the way Claude Code does (which auto-captures via
hooks) even though your editor has no hook system.
