# Install memex

Memex is a local AI memory daemon. It runs on your machine, talks to your AI editors (Claude Code, Cursor, Windsurf, Cline) over MCP, and gives them persistent memory across sessions.

This guide is for installing memex on a teammate's machine. Two commands once Python is in place.

> **Memex requires working embeddings** (fastembed + onnxruntime). Without them, "recall" is just lexical fuzzy search — it misses every semantic match ("auth flow" wouldn't find "JWT validation"). The daemon refuses to start in lexical-only mode by design.

---

## macOS

```bash
# 1. Make sure you have Python 3.10+ and pipx
brew install python@3.12 pipx
pipx ensurepath

# 2. Install memex (pulls fastembed + onnxruntime automatically; works out of the box on Mac)
pipx install memex

# 3. First-run wizard — detects your AI editors, wires hooks, installs the always-on service
memex init
```

Open <http://127.0.0.1:7777> — that's the UI.

**Mac install just works.** ONNX runtime ships ARM64 + x86_64 wheels for macOS that don't need extra system deps.

---

## Windows

Windows needs one extra prerequisite: the **Visual C++ Redistributable**. Without it, `import onnxruntime` fails with a DLL load error and the daemon won't start.

```powershell
# 1. Install Python 3.10+ (one-time)
winget install Python.Python.3.12
# (or download from https://www.python.org/downloads/windows/)

# 2. Install Microsoft Visual C++ Redistributable (one-time, required by onnxruntime)
winget install Microsoft.VCRedist.2015+.x64
# (or download directly: https://aka.ms/vs/17/release/vc_redist.x64.exe)

# 3. Install pipx
python -m pip install --user pipx
python -m pipx ensurepath
# Close + reopen PowerShell so pipx is on PATH.

# 4. Install memex with the embed extra pinned to a known-working onnxruntime
pipx install memex
pipx inject memex onnxruntime==1.19.2 --force      # pin away from broken 1.20+ versions

# 5. First-run wizard
memex init
```

Open <http://127.0.0.1:7777> — that's the UI.

### Why pin onnxruntime to 1.19.2 on Windows

onnxruntime 1.20+ broke the `SkipLayerNormalization` op that fastembed's bge-small model uses. The latest version that works with fastembed today is 1.19.2. We pin explicitly until upstream fixes ship.

If you see `DLL load failed while importing onnxruntime_pybind11_state` even after the VC++ Redist install:

```powershell
# Verify VC++ Redist installed
where vcruntime140.dll
# Should print one or more paths. If empty, install VC++ Redist again.

# Force-reinstall onnxruntime with the working version
pip install --force-reinstall onnxruntime==1.19.2
python -c "import onnxruntime; print(onnxruntime.__version__)"
# Should print: 1.19.2
```

---

## Linux

```bash
# 1. Python 3.10+ + pipx (your distro's package manager)
sudo apt install python3 python3-pip pipx        # Debian / Ubuntu
# or
sudo dnf install python3 python3-pip pipx        # Fedora

# 2. Install memex
pipx install memex

# 3. First-run wizard
memex init
```

Linux ONNX install rarely has issues — onnxruntime ships glibc-2.17+ wheels that work on every modern distro.

---

## What `memex init` does

The wizard runs three steps. Each is idempotent and skippable.

1. **AI editor hooks.** Detects Claude Code, Cursor, Windsurf, and Cline on your machine. Asks once whether to wire memex into each one's MCP config.
2. **Always-on service.** Installs a per-user service so memex starts on login:
   - macOS → launchd (`~/Library/LaunchAgents/com.quefly.memex.plist`)
   - Windows → Task Scheduler (`MemexDaemon`)
   - Linux → systemd user (`~/.config/systemd/user/memex.service`)
3. **Seed from a repo (optional).** Points at any local git repo and extracts decisions from commit history, README, ARCHITECTURE, ADRs, CHANGELOG, manifests. Solves day-1 empty graph.

After the wizard:

- Web UI: <http://127.0.0.1:7777>
- Tail what memex is doing: `memex stats`
- Add the 15 baseline safety rules (deny rm -rf, force-push, DROP TABLE, etc.): `memex install rules:safety-baseline`

---

## Optional: connect a team repo (decisions sync across teammates)

```bash
# One teammate creates the repo (locally or via GitHub)
memex team init ~/code/team-memex
cd ~/code/team-memex
git remote add origin git@github.com:your-org/team-memex.git
git push -u origin main

# Every other teammate clones it + tells memex
git clone git@github.com:your-org/team-memex.git ~/code/team-memex
# Add to your shell profile (.zshrc / .bashrc / $PROFILE):
export MEMEX_TEAM_REPO=~/code/team-memex

# Memex auto-syncs every 10 minutes. To force:
memex team push
memex team pull
```

Only durable knowledge syncs (decisions, facts, constraints, projects, tasks). Code symbols, episodic events, vectors stay local — each teammate indexes their own clones.

---

## Troubleshooting

### Daemon refuses to start: "embedding provider unavailable"

You'll see this if onnxruntime / fastembed can't load. Memex deliberately fails here rather than silently degrading recall quality.

**macOS / Linux:**
```bash
pip install --force-reinstall onnxruntime==1.19.2 fastembed
```

**Windows:**
1. Install [VC++ Redist x64](https://aka.ms/vs/17/release/vc_redist.x64.exe)
2. `pip install --force-reinstall onnxruntime==1.19.2 fastembed`
3. Restart the daemon: `memex service uninstall && memex service install`

### "memex daemon already running"

Memex runs one daemon per machine. If you see this message when starting manually, the daemon is already up:

```bash
memex service status
curl http://127.0.0.1:7777/health
```

### Hooks not wired after `memex init`

The wizard skips editors it can't detect. Launch each editor at least once (so it creates its config file), then re-run:

```bash
memex setup
```

### Uninstall

```bash
memex service uninstall    # stop the always-on service
pipx uninstall memex        # remove the package
```

Data lives at `~/.memex` (Linux/Mac) or `%LOCALAPPDATA%\Quefly\memex` (Windows). Delete manually for a clean wipe.

---

## What you get

- All your AI editor sessions share one persistent memory
- Recall works across days, machines, and editors (real semantic search — not just keyword match)
- Decisions captured from your git log, docs, prompts
- A gate that stops your AI from running force-pushes, `rm -rf /`, DROP TABLE, etc.
- Cross-repo same-symbol detection (one JWT struct appears in 5 services? memex knows)
- One-click export to JSON or Parquet — no lock-in
- Local-only, no cloud — your data stays on your machine

## What you're getting into

Memex is v1.0 but young. Known rough edges:

- Recall with semantic rerank takes 1–3 seconds; sub-500ms fast path is on the roadmap
- Team-sync is git-based; cross-instance OMP federation is v2

Find a bug? Open an issue at <https://github.com/queflyhq/memex/issues>.
