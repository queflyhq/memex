<!-- Thanks for sending a PR. A few prompts to help reviewers move quickly. -->

## Summary

<!-- One or two sentences. What does this change and why? -->

## Type of change

- [ ] Bug fix
- [ ] New feature
- [ ] Refactor / cleanup (no behavior change)
- [ ] Docs
- [ ] CI / build / tooling

## Linked issue

<!-- Closes #N, or leave empty. -->

## Test plan

<!-- How did you verify this works? -->
- [ ] `uv run pytest` passes
- [ ] `uv run ruff check .` passes
- [ ] `uv run memex --help` still works
- [ ] (If touching the daemon) Started a local daemon and verified `/health` + a representative endpoint
- [ ] (If a new public surface) Added or updated docs under `quefly/src/routes/docs/memex/`

## Breaking changes

<!-- If this changes a public API / CLI flag / config / on-disk format, call it out here and add a CHANGELOG entry. -->

## CHANGELOG

<!-- For user-visible changes, add a bullet under the unreleased section of CHANGELOG.md. -->
