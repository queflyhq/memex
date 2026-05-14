# Open Memory Protocol (OMP)

The OMP specification has moved to a dedicated public repository.

**📜 Canonical spec:** <https://github.com/queflyhq/open-memory-protocol>
**🌐 Landing page:** <https://quefly.com/oss/open-memory-protocol>
**📋 Latest version:** v0.1 (Editor's Draft)
**📄 Licence:** [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/)

---

memex is the **reference implementation** of OMP v0.1. The spec lives independently so that other implementations don't have to depend on or vendor any memex-specific assumptions.

When the spec changes, memex follows. To compare memex's behaviour against the spec, read [`omp-v0.1.md`](https://github.com/queflyhq/open-memory-protocol/blob/main/spec/omp-v0.1.md) in the canonical repo.

## Why a separate repo

- **Spec text is CC BY 4.0**; memex code is Apache 2.0. Different licences want different repository scopes.
- **Other implementations** can clone the spec repo without pulling 90 MB of embedding model + DuckDB engine that comes with memex.
- **Conformance test suite** evolves under the spec, not under any single implementation.
- **Standards bodies** (W3C / IETF / OASIS) prefer specs in their own repos when considering adoption.

## Reporting spec bugs / proposing changes

File issues at <https://github.com/queflyhq/open-memory-protocol/issues>. See the [contributing guide](https://github.com/queflyhq/open-memory-protocol/blob/main/CONTRIBUTING.md) for the review process.
