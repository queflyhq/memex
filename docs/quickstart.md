# Quickstart

60 seconds from install to first recall.

## 1. Install

=== "pipx (recommended)"

    ```bash
    pipx install memex
    ```

=== "uv"

    ```bash
    uv tool install memex
    ```

=== "Docker"

    ```bash
    docker run -d --name memex \
      -v $HOME/.memex:/data \
      -p 127.0.0.1:7777:7777 \
      quefly/memex:latest
    ```

Tier 0 is ~50 MB and works without any model downloads. Tier 1 (vector retrieval) is `pipx install --force 'memex[embed]'` and adds ~80 MB.

## 2. Sanity check

```bash
memex doctor
```

You should see the data directory path, Python version, and whether the embedding tier is installed.

## 3. Install the bootstrap meta-skill

The `using-memex` skill teaches any AI tool how to interact with memex (when to recall, when to validate, when to observe, when to add). The `core-validations` skill ships globally-applicable engineering principles every AI should self-attest against before generating sensitive code.

```bash
memex install skill:using-memex
memex install skill:core-validations
```

## 4. Add your first memory

```bash
memex add "Use Postgres over MySQL for JSONB" --kind decision
```

## 5. Recall it

```bash
memex recall "database choice"
```

You'll see the decision returned in a table with id, kind, name, confidence, and source.

## 6. Validate against an approach

```bash
memex validate secure-subprocess
```

You get the structured `approach + checks + examples`. Memex auto-records this as an episodic event.

## 7. See what's been done

```bash
memex progress
```

Shows total concepts, total events, validated skills, and other activity stats.

## 8. Wire it into Claude Code

```bash
claude mcp add memex memex serve
```

That's it. From the next Claude Code session, `recall`, `add_node`, `validate`, `observe`, and `progress` are exposed as MCP tools the agent can call directly.

## 9. (Optional) Run as a daemon for any HTTP-capable tool

```bash
memex daemon --listen 127.0.0.1:7777
```

Then any tool can hit `http://127.0.0.1:7777/recall?q=...` or `POST /nodes` etc. See [HTTP recipes](editors/http.md).

## What next

- **[Concepts](concepts.md)** — why the architecture is shaped the way it is.
- **[Skills](skills.md)** — author your own skill bundles.
- **[Editors](editors/claude-code.md)** — wire memex into your AI tools.
