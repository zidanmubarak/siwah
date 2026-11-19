# Contributing to siwah

Thanks for taking the time to improve siwah. This project is a local coding
agent with access to files, shells, and provider credentials, so changes are
reviewed with reliability and security in mind.

## Before you start

- Search existing issues and pull requests.
- For a small fix, open a pull request directly.
- For a new feature, a protocol change, or a behavior change, open an issue
  first so the design can be discussed.
- For a security issue, follow [SECURITY.md](SECURITY.md), not a public issue.

## Development setup

See [Development](docs/DEVELOPMENT.md) for prerequisites, setup, checks, and
build commands. The short version:

```bash
git clone https://github.com/zidanmubarak/siwah.git
cd siwah
uv sync --extra dev
cd ui/tui && bun install --frozen-lockfile && cd ../..
uv run pytest
```

## Coding standards

- Follow the style and structure of the surrounding code.
- Python: Ruff with a 100-column line length, double quotes, and the configured
  rule set; Pyright in basic mode.
- TypeScript: Biome and the existing TUI conventions.
- Keep functions small and observable. Prefer explicit data flow over hidden
  global state.
- Do not hardcode model IDs, prices, limits, provider endpoints, or credentials.
  Read them from config or a provider endpoint.
- Treat file contents, web pages, MCP descriptions, and tool results as
  untrusted data, never as instructions.
- Do not log secrets, full headers, or private prompts by default.
- Add tests for behavior changes. Add or update a TUI snapshot only when the
  visual change is intentional.
- Update the public docs when a command, option, config field, event, or
  limitation changes.
- Keep examples copy-pasteable and free of personal paths.

## Protected paths

Do not modify these without explicit maintainer approval:

- `src/siwah/kernel/`
- `evals/`
- `tests/kernel/`
- `kernel.manifest.json`
- the protected bootstrap, config, auth, and profile loaders enforced by the
  kernel boundary

Do not regenerate `kernel.manifest.json` as part of an ordinary change. A
manifest mismatch is a security signal, not a formatting problem. If your change
legitimately requires a protected edit, explain why in the issue first and keep
the diff minimal.

Evolution Level 2 candidates are also forbidden from changing `ui/` or removing
test lines. Human pull requests may change UI code, but expect a careful review.

## Tests

Run the relevant checks before opening a pull request:

```bash
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run pytest

cd ui/tui
bun run lint
bun run typecheck
bun run test
```

If you change a server schema, regenerate the OpenAPI document and TypeScript
types and commit both:

```bash
uv run siwah openapi > ui/tui/openapi.json
cd ui/tui && bun run generate:types
```

The Docker sandbox test is optional and requires Docker:

```bash
SIWAH_TEST_DOCKER=1 uv run pytest tests/test_sandbox.py -k docker_sandbox
```

Use `SIWAH_HOME` for any test that should not touch your real `~/.siwah/`.

## Pull requests

1. Fork the repository and create a focused branch.
2. Keep the change scoped. Separate refactors from behavior changes.
3. Include a clear description: problem, approach, tests, and user-visible
   impact.
4. Fill in the pull request template.
5. Link the issue when one exists.
6. Be responsive to review. Small follow-up commits are fine; do not force-push
   away review context unless asked.

During 1.0.x, accepted changes may be ported to the maintainers' canonical
development tree before a release. This means an accepted PR may land in a later
sync rather than as a direct merge commit. The maintainers will tell you when
that happens. It does not change the MIT licensing of your contribution.

## Commit messages

Use a short imperative subject and explain the why in the body when it is not
obvious. Reference the issue when practical. Avoid mixing unrelated changes in
one commit.

## Documentation

Public documentation lives under `docs/` and in the root README. Keep it in
English. Do not link to internal planning documents, private repositories, or
personal paths. If a feature is partial, document the limitation instead of
describing an aspiration as shipped behavior.

## Secrets and private data

- Never commit API keys, tokens, cookies, or private keys.
- Never paste real credentials into an issue, pull request, test, or screenshot.
- Use placeholder values such as `<model-id>` and `example.invalid`.
- If you accidentally expose a secret, rotate it immediately and tell the
  maintainers.

## License

By contributing, you agree that your contribution is licensed under the MIT
License, the same license as the project. See [LICENSE](LICENSE).
