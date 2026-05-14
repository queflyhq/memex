# Security Policy

## Reporting a vulnerability

Please **do not** open a public GitHub issue for security vulnerabilities.

Report privately via either:

1. **GitHub Security Advisories** — preferred. Open a draft advisory at
   <https://github.com/queflyhq/memex/security/advisories/new>.
2. **Email** — `security@quefly.com`. PGP key on request.

Include:

- A clear description of the vulnerability and its impact.
- Reproduction steps or a proof-of-concept.
- The memex version (`memex version`) and your operating system.
- Whether the issue is publicly known.

## Response timeline

We aim to:

- **Acknowledge** within 3 business days.
- **Triage and confirm** within 7 business days.
- **Patch and release** within 30 days for high-severity issues.

You will be credited in the release notes unless you ask otherwise.

## Supported versions

| Version | Supported |
|---------|-----------|
| 1.x     | ✅        |
| < 1.0   | ❌        |

Pre-1.0 development releases are no longer supported. Upgrade to 1.x.

## Threat model

memex is **local-first by default** — the daemon binds to `127.0.0.1` and
your data stays on your machine. The threat model assumes:

- The user trusts their own operating system account.
- Other local OS users (on shared machines) are **not** trusted by default.
- The local daemon is authenticated with a per-machine bearer token
  (`data_dir/daemon.token`, mode `0600` on POSIX) generated on first run.
- Network access to the daemon from outside `127.0.0.1` is opt-in via
  `MEMEX_LISTEN`. Exposing it on a routable interface in production
  **requires** a non-trivial `MEMEX_AUTH_TOKEN` and is the operator's
  responsibility to put behind TLS.

Out of scope:

- Adversarial OS-level attackers with root/Administrator privileges.
- Side-channel attacks against the host kernel or hardware.
- Vulnerabilities in upstream MCP servers proxied through memex
  (report those to the respective upstream projects; we will mirror
  advisories that affect memex's recommended catalog).

## Hardening recommendations

- Keep `MEMEX_LISTEN=127.0.0.1:7777` (the default) unless you have a
  specific reason to expose it.
- Set a strong `MEMEX_AUTH_TOKEN` if you override the auto-generated one.
- Use the Docker image (`quefly/memex:latest`) for stronger process
  isolation if running on a shared host.
- Run `memex doctor` periodically to surface degraded retrieval, stale
  upstreams, or write-permission issues on the data directory.
