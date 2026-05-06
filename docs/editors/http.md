# HTTP / any editor

For any AI tool or editor that doesn't speak MCP — Aider, JetBrains plugins, custom agents, Neovim plugins, shell scripts — drive memex via its HTTP API.

## Start the daemon

```bash
memex daemon --listen 127.0.0.1:7777
```

By default memex binds only to localhost. To expose externally, you must pass `--auth-token`:

```bash
memex daemon --listen 0.0.0.0:7777 --auth-token "$(openssl rand -hex 32)"
```

(Refusing to start without a token on a non-localhost address is intentional — silent insecurity is worse than a loud failure.)

## Endpoints

OpenAPI spec is auto-generated at `http://127.0.0.1:7777/openapi.json`. Swagger UI at `http://127.0.0.1:7777/docs`.

| Method | Path | Description |
|---|---|---|
| GET | `/health` | Server status, version, storage stats |
| GET | `/recall?q=<query>&budget=2000&kind=&expand=1` | Hybrid retrieval, returns subgraph |
| POST | `/nodes` | Add a concept |
| GET | `/nodes/{id}` | Fetch a concept by id |
| POST | `/edges` | Create a typed edge |
| POST | `/observe` | Emit an episodic event |
| POST | `/validate` | Get a skill's `approach + checks + examples` |
| GET | `/progress` | Activity summary |
| GET | `/skills` | List builtin skill bundles |
| POST | `/skills/install` | Install a builtin skill into the engine |

## Examples

### Recall

```bash
curl 'http://127.0.0.1:7777/recall?q=database+choice&budget=2000'
```

### Add a concept

```bash
curl -X POST http://127.0.0.1:7777/nodes \
  -H 'Content-Type: application/json' \
  -d '{
    "name": "Use Postgres over MySQL",
    "description": "JSONB usage drove the decision.",
    "kind": "decision",
    "source": "human",
    "confidence": 1.0
  }'
```

### Validate against an approach

```bash
curl -X POST http://127.0.0.1:7777/validate \
  -H 'Content-Type: application/json' \
  -d '{"skill": "secure-subprocess", "actor": "agent"}'
```

### Authenticated request

```bash
curl 'http://127.0.0.1:7777/recall?q=auth' \
  -H 'Authorization: Bearer <your-token>'
```

## Python client (any language works the same)

```python
import httpx

class Memex:
    def __init__(self, base="http://127.0.0.1:7777", token=None):
        self.client = httpx.Client(
            base_url=base,
            headers={"Authorization": f"Bearer {token}"} if token else {},
        )

    def recall(self, query, budget=2000, kind=None):
        params = {"q": query, "budget": budget}
        if kind:
            params["kind"] = kind
        return self.client.get("/recall", params=params).json()

    def add(self, name, description="", kind="fact", source="agent"):
        return self.client.post("/nodes", json={
            "name": name, "description": description,
            "kind": kind, "source": source,
        }).json()

    def validate(self, skill, actor="agent"):
        return self.client.post("/validate", json={
            "skill": skill, "actor": actor,
        }).json()
```

## Polyglot

The same HTTP surface works from any language: Go (`net/http`), Rust (`reqwest`), Node (`fetch`), Java (`java.net.http`), Ruby (`net/http`), shell (`curl`). The OpenAPI spec autogenerates client SDKs for every major language.
