# Privacy

memex is local-first by design. The daemon runs on your machine, your memory graph stays in a directory you own, and nothing is sent to Quefly or anyone else by default.

## What memex stores

| Data | Where | Sent to Quefly? |
|------|-------|-----------------|
| Concepts, edges, embeddings, episodic events | `<data_dir>/memex.duckdb` | No |
| Auth token | `<data_dir>/daemon.token` (mode 0600 on POSIX) | No |
| Skill bundles | Bundled in the wheel + `<data_dir>/skills/` | No |
| Upstream MCP server config | `~/.memex/upstreams.json` or `./.memex/upstreams.json` | No |
| Daemon logs | `<data_dir>/daemon.log` | No |

`<data_dir>` defaults to:

| OS | Path |
|---|---|
| Linux | `~/.local/share/memex/` |
| macOS | `~/Library/Application Support/memex/` |
| Windows | `%LOCALAPPDATA%\Quefly\memex\` |

Override with `MEMEX_DATA_DIR=/your/path`.

## Telemetry

**There is none.** memex does not phone home, does not collect anonymized usage stats, does not transmit your concepts, and has no analytics SDK. The HTTP daemon binds to `127.0.0.1` by default and refuses to start on a non-localhost interface unless you explicitly set `MEMEX_AUTH_TOKEN`.

You can confirm this yourself:

```bash
# Watch all outbound network attempts from the daemon
sudo lsof -p "$(pgrep -f 'memex daemon')" -i

# Or: run the daemon offline.
sudo unshare -n memex daemon   # Linux network-namespace isolation
```

## Updates

`pipx install --upgrade memex` and `docker pull quefly/memex` are explicit user actions. memex itself does not auto-update or check for updates in the background.

## Crash reports

memex does **not** send crash reports. If the daemon panics, the traceback is written to `<data_dir>/daemon.log` and stays there. Attach the log voluntarily when filing an issue.

## Third-party MCP upstreams

When you wire upstream MCP servers via `memex upstream install <id>`, those upstreams may send data to *their* respective providers (GitHub's MCP server talks to GitHub, Slack's talks to Slack, etc.). memex acts as a local pass-through and auto-`observe()`s the call as an episodic event. The upstream's own privacy policy applies to whatever data the upstream itself receives. memex does not relay any of this to Quefly.

## Embeddings

If you install the optional embedding tier (`memex[embed]`), the embedding model (default: `BAAI/bge-small-en-v1.5`) downloads to `<data_dir>/models/` on first use. Inference runs on your CPU locally — no cloud API.

## Team mode (future)

A future opt-in "team mode" deploys the daemon on a server you control and uses [AuthFI](https://authfi.app) for member identity. In team mode, every concept carries an `actor` field. This is opt-in by deployment topology — single-user installs are unaffected.

## Reporting concerns

For privacy-related concerns, email **privacy@quefly.com** or file a [security advisory](https://github.com/queflyhq/memex/security/advisories/new) if it's also a vulnerability.

## Updates to this policy

Material changes to this policy will be announced in the [CHANGELOG](CHANGELOG.md) and noted in release notes. Last updated: 2026-05-08.
