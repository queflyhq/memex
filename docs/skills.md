# Skills

A **skill** is the top-level installable unit in memex. The terminology matches industry usage (Anthropic Claude Skills, Microsoft Semantic Kernel). A skill bundle ships a `manifest.json` + a `concepts.jsonl` (one concept per line) + an optional `edges.jsonl`.

## Why skills

Two problems memex solves with skills:

1. **Cold start.** A fresh memex install is empty. Skills give you immediate value — the AI knows AWS gotchas, Python idioms, secure-subprocess validation, etc., from the moment you install.
2. **Forced validation.** Some skills carry `kind=approach` concepts with structured `checks` lists. The AI calls `validate(<approach-name>)` before generating sensitive code (security, money, concurrency, …) and self-attests each check passes.

## The three builtin skills

### `using-memex` — the bootstrap meta-skill
Teaches any AI tool how to interact with memex itself. Every memex install ships with this — an AI's first `recall("how to use memex")` returns the protocol for when to recall, when to validate, when to observe, when to add.

```bash
memex install skill:using-memex
```

### `core-validations` — globally-applicable engineering principles
Language-agnostic validation approaches every AI should self-attest against. Covers: secure-subprocess, money-decimal-precision, structured-concurrency-async, crypto-secrets-not-random, user-input-untrusted-by-default, sql-parameterized-queries, error-handling-fail-loud, logging-structured-and-no-secrets.

```bash
memex install skill:core-validations
```

After install, the AI can call:

```python
validate("secure-subprocess")
# Returns:
# {
#   "ok": true,
#   "skill": "secure-subprocess",
#   "approach": "Pass arguments as a list, never as a string. Avoid shell=True...",
#   "checks": [
#     "arguments are a list (not a single string)",
#     "shell=False (or omitted)",
#     ...
#   ],
#   "examples_good": [...],
#   "examples_bad": [...]
# }
```

### `python-stdlib` — Python language-specific gotchas
Idioms and version-specific behaviors in datetime, asyncio, subprocess, logging, decimal, secrets, functools, contextlib, argparse, pathlib, tempfile, concurrent.futures, etc.

```bash
memex install skill:python-stdlib
```

## Skill bundle format

```
my-skill/
├── manifest.json     # name, version, description, license, scope
├── concepts.jsonl    # one Concept per line (JSON)
└── edges.jsonl       # optional, one Edge per line
```

### `manifest.json`

```json
{
  "name": "my-skill",
  "version": "0.1.0",
  "description": "What this skill teaches.",
  "license": "MIT",
  "homepage": "https://github.com/yourorg/your-skill",
  "min_memex": "0.1.0",
  "scope": "global:my-domain"
}
```

### `concepts.jsonl` — one concept per line

Each line is a JSON `Concept`:

```json
{"name": "secure-subprocess", "kind": "approach", "description": "...", "metadata": {"approach": "...", "checks": ["...", "..."], "examples_good": [...], "examples_bad": [...], "triggers": [...]}, "confidence": 1.0}
```

`kind` can be: `pattern`, `decision`, `constraint`, `module`, `endpoint`, `person`, `fact`, `opinion`, `question`, `rejected`, `approach`.

For validation entries (`kind=approach`), put structured fields in `metadata`:

- `approach` (string) — how to think about this domain
- `triggers` (list[string]) — keywords / contexts that should fire this skill
- `checks` (list[string]) — what must be true; AI self-attests each
- `examples_good` (list[string]) — code that passes
- `examples_bad` (list[string]) — code that fails

## Installing your own skill

From a local path:

```bash
memex install-from-path /path/to/my-skill/
```

From a remote git URL (v0.2):

```bash
memex install skill:org/my-skill@v1
```

## Roadmap

- **v0.1**: 3 builtin skills bundled inside the wheel.
- **v0.2**: External `queflyhq/memex-skills` repo as the registry; community contributions via PR.
- **v0.3**: Auto-suggest skills based on what the AI is working on (file paths, languages detected).
- **v0.4**: Skill versioning + diff — see what changed between versions.
- **v0.5**: Skill verification — run `verification` predicates on each skill's checks against actual code.
