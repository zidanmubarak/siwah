## Summary

<!-- What does this change do, and why? -->

## Related issue

<!-- Link the issue, or write "none". -->

## Type of change

- [ ] Bug fix
- [ ] New feature
- [ ] Refactor
- [ ] Documentation
- [ ] Test or evaluation fixture
- [ ] Build, packaging, or release
- [ ] Protocol or configuration change

## What changed

<!-- A short tour of the diff. Call out user-visible behavior. -->

## How it was tested

<!-- Commands and results. Include the OS/terminal for TUI changes. -->

```text
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run pytest
```

## Checklist

- [ ] I read `CONTRIBUTING.md` and `AGENTS.md`.
- [ ] I added or updated tests for the behavior change.
- [ ] I ran the relevant Python and TUI checks.
- [ ] I regenerated `ui/tui/openapi.json` and the TypeScript types if a schema changed.
- [ ] I updated the public documentation and `CHANGELOG.md` for user-visible changes.
- [ ] I did not touch `src/siwah/kernel/`, `evals/`, `tests/kernel/`, or `kernel.manifest.json` without maintainer approval.
- [ ] I did not add secrets, private code, or personal paths.
- [ ] I did not weaken a permission, sandbox, path-guard, or evolution-gate default.

## UI evidence

<!-- For TUI changes: a screenshot or text capture, and the terminal/size used. -->

## Notes for reviewers

<!-- Known gaps, follow-ups, or decisions that need discussion. -->

> During 1.0.x, accepted changes may be ported to the maintainers' canonical
> development tree before a release. An accepted PR may land in a later sync
> rather than as a direct merge.
