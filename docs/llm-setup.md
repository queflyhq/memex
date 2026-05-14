# LLM setup for memex

memex's core retrieval (BM25 + vector + RRF + rerank) runs without any
LLM. Generation features — query expansion, HyDE, richer consolidation
summaries — are opt-in and turn on the moment a provider becomes
available. This doc walks you through wiring one in.

## What an LLM unlocks

| Feature | Without LLM | With LLM |
|---|---|---|
| Recall | BM25 + vector + RRF + rerank | + query expansion + HyDE |
| Consolidation | Templated event-cluster summary | LLM prose summary that captures the *pattern*, not just the count |
| Pattern promoter | Heuristic name-and-link | (planned) LLM-paraphrased rule body |

For the user with a heavy memex (many concepts, many recalls per day),
**HyDE alone routinely lifts top-1 retrieval accuracy by 10+ points**
because user queries and stored documents live in different
distributional sub-spaces. Adding an LLM is the single biggest jump
once your graph has > ~1k concepts.

## Provider order

memex's `build_default_llm()` checks providers in this order, picks the
first available, and never falls back at runtime:

1. **Ollama** at `$OLLAMA_HOST` or `http://127.0.0.1:11434`
2. **Anthropic API** when `$ANTHROPIC_API_KEY` is set
3. **NoOp** — generation features stay dark (this is the OSS default)

## Option 1 — Ollama (recommended; local, free, offline)

Why Ollama: no API costs, no data leaves your machine, no key
rotation, fast on a laptop. The latency hit on the recall hot path is
~300–800ms with a small model — acceptable for HyDE; flip off if
you're rate-limited.

```powershell
# 1. Install Ollama (Windows native installer)
winget install Ollama.Ollama
# or download from https://ollama.com/download/windows

# 2. Pull a small fast model
ollama pull llama3.2:3b      # 2 GB, ~80 tokens/sec on CPU
# or for better expansion quality:
ollama pull qwen2.5:7b       # 4.5 GB, ~30 tokens/sec on CPU

# 3. Verify it's running
curl http://127.0.0.1:11434/api/tags
```

memex auto-detects Ollama on startup. No config needed beyond having
the Ollama daemon running. To pin a specific model:

```powershell
$env:MEMEX_OLLAMA_MODEL = "llama3.2:3b"
```

Then turn on the features that use it:

```powershell
$env:MEMEX_QUERY_EXPANSION_ENABLED = "true"
$env:MEMEX_HYDE_ENABLED = "true"
```

Restart the daemon. You should see in logs:

```
INFO memex.ml.llm: ollama detected; auto-selected model: llama3.2:3b
```

The dashboard's "Recall efficiency" should climb within a few sessions
as HyDE-augmented recalls produce better hits.

## Option 2 — Anthropic API (cloud, paid, highest quality)

Best when:
- You want top-quality consolidation summaries (Claude Haiku is fast
  and cheap; Claude Sonnet is excellent)
- You can't run Ollama locally (low RAM, no GPU, locked-down machine)
- You're OK with queries leaving the machine

```powershell
$env:ANTHROPIC_API_KEY = "sk-ant-..."
$env:MEMEX_ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"  # default
$env:MEMEX_QUERY_EXPANSION_ENABLED = "true"
$env:MEMEX_HYDE_ENABLED = "true"
```

memex never sends a query to Anthropic unless one of the LLM features
fires. With expansion + HyDE on, every recall makes one Anthropic call
(~80–200ms p50 for Haiku). At ~100 recalls/day that's < $1/month even
with Sonnet.

## Verifying it works

After restart:

```powershell
curl -H "Authorization: Bearer <token>" http://127.0.0.1:7777/health
# Look at server logs:
#   INFO memex.ml.llm: ollama detected; auto-selected model: llama3.2:3b
# or
#   INFO memex.ml.llm: using Anthropic LLM provider (model=claude-haiku-...)
```

A recall after enabling expansion should show a longer effective query
in the daemon log. HyDE silently replaces the embedded text — verify by
running an ambiguous query and comparing top-5 hit overlap before/after.

## Turning it off

```powershell
$env:MEMEX_QUERY_EXPANSION_ENABLED = "false"
$env:MEMEX_HYDE_ENABLED = "false"
```

These flags can be flipped per-environment without restarting Ollama
or rotating keys. The provider detection happens once at engine
startup; toggling the flags only changes whether the provider is
*invoked*.

## Why generation isn't on by default

memex's correctness contract is: every recall returns under the
configured token budget within p99 < 50 ms when nothing changes. Once
HyDE / query expansion is on, the budget covers an LLM call too — that
moves the p99 to the LLM's latency floor (200 ms+ even local). That's
a deliberate trade we let users make, not a default we impose.

If you don't notice a precision problem, leave the LLM off. If you're
seeing low recall efficiency on the dashboard, turn it on.
