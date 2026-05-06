# Docker daemon

Run memex as a long-lived HTTP daemon in Docker. Best for: machines without Python, team servers, CI environments, or when you want all your editors to share the same memex without each spawning its own process.

## One-liner

```bash
docker run -d --name memex \
  -v $HOME/.memex:/data \
  -p 127.0.0.1:7777:7777 \
  quefly/memex:latest
```

This binds **only to localhost** (the `127.0.0.1:` prefix on `-p`). To share across machines, see the [auth section](#expose-on-a-team-server).

## docker-compose

```yaml
services:
  memex:
    image: quefly/memex:latest
    restart: unless-stopped
    volumes:
      - ./memex-data:/data
    ports:
      - "127.0.0.1:7777:7777"
    environment:
      - MEMEX_DATA_DIR=/data
```

Bring it up:

```bash
docker compose up -d
```

## Verify

```bash
curl http://127.0.0.1:7777/health
```

## Bootstrap with skills

```bash
curl -X POST http://127.0.0.1:7777/skills/install \
  -H 'Content-Type: application/json' \
  -d '{"name": "using-memex"}'

curl -X POST http://127.0.0.1:7777/skills/install \
  -H 'Content-Type: application/json' \
  -d '{"name": "core-validations"}'
```

## Wire editors at the daemon

For Claude Code with a long-running daemon (instead of spawning per-session), run a small MCP-stdio shim that proxies to the HTTP daemon. (Coming in v0.2.) For v0.1, point HTTP-capable tools directly at the daemon — see [HTTP recipes](http.md).

## Expose on a team server

Binding to `0.0.0.0` requires an auth token — the daemon refuses to start otherwise.

```yaml
services:
  memex:
    image: quefly/memex:latest
    restart: unless-stopped
    volumes:
      - ./memex-data:/data
    ports:
      - "0.0.0.0:7777:7777"
    environment:
      - MEMEX_DATA_DIR=/data
      - MEMEX_AUTH_TOKEN=${MEMEX_AUTH_TOKEN:?set MEMEX_AUTH_TOKEN}
    command: ["daemon", "--listen", "0.0.0.0:7777"]
```

Generate a token: `openssl rand -hex 32`. Distribute to authorized clients via your secrets manager.

## Roadmap

- **v0.6**: Team-mode authentication via [AuthFI](https://authfi.app) — SSO + member identity automatically. Every node carries `actor=<authfi_user_id>` so the AI knows who decided what.
