# Development

This guide is for working on siwah itself. For installing and using siwah, see
[Getting started](GETTING-STARTED.md).

## Prerequisites

- Python 3.11+
- [uv](https://docs.astral.sh/uv/)
- [Bun](https://bun.sh/) 1.3.14+ (CI uses 1.4.4)
- Git
- Optional: Docker Desktop for the Docker sandbox test
- Optional: a provider key for live smoke tests

## Set up

```bash
git clone https://github.com/zidanmubarak/siwah.git
cd siwah
uv sync --extra dev
cd ui/tui
bun install --frozen-lockfile
cd ../..
uv run siwah --version
```

Run siwah from the checkout:

```bash
uv run siwah                 # TUI
uv run siwah serve           # daemon only
uv run siwah run "..." --ui plain
```

The launcher uses `ui/tui/dist/siwah-tui[.exe]` when it exists, otherwise Bun
source. Build the binary after changing TUI code:

```bash
cd ui/tui
bun run build
```

The build must use `Bun.build` with `@opentui/solid/bun-plugin`; a plain
`bun build --compile` breaks Solid reactivity. The build bundles the current
platform's OpenTUI native package and externalizes the others.

## Checks

Run the same checks as CI:

```bash
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run pytest
```

Python style is enforced by Ruff with a 100-column line length, double quotes,
and the `E`, `F`, `I`, `UP`, `B`, and `SIM` rule sets. Pyright runs in basic mode
over `src` and `tests`.

TUI checks:

```bash
cd ui/tui
bun run lint
bun run typecheck
bun run test
bun run build
```

`bun run test` includes snapshots and resize, paste, typing, and large-output
coverage. `bun run bench` reports streaming, echo, and resize metrics.

### OpenAPI drift

The OpenAPI document and generated TypeScript types are committed. Regenerate
both after changing a server schema:

```bash
uv run siwah openapi > ui/tui/openapi.json
cd ui/tui
bun run generate:types
cd ../..
git diff --exit-code -- ui/tui/openapi.json ui/tui/src/api/schema.d.ts
```

CI runs the same generation and fails when the committed files drift.

### Sandbox integration

The Docker test pulls the image once and verifies the Linux shell, workspace
mount, and timeout cleanup:

```bash
SIWAH_TEST_DOCKER=1 uv run pytest tests/test_sandbox.py -k docker_sandbox
```

It requires Docker. It does not prove that a user-supplied image contains a
project's full toolchain.

### Optional LSP support

For environments without system Node, install the bundled Node runtime:

```bash
uv sync --extra dev --extra lsp
```

`refs` detects pyright, TypeScript, gopls, rust-analyzer, or clangd when
available and falls back to git grep/ripgrep. Set `SIWAH_LSP=off` to force the
fallback.

## Build artifacts

```bash
uv run python scripts/build-release.py
```

This writes to `dist/` (gitignored):

- `siwah-<version>-py3-none-any.whl`;
- `siwah-tui-<platform>-<arch>[.exe]` when Bun is available;
- `SHA256SUMS`.

The script never uploads anything. To create a shareable bundle with installers:

```bash
uv run python scripts/package-bundle.py
```

This writes `dist/siwah-<version>-<platform>-<arch>.zip` with the wheel, TUI
binary, checksums, and installers.

See [Releasing](RELEASING.md) before publishing anything.

## Code standards

- Keep changes consistent with the surrounding modules and naming.
- Prefer small, testable functions over new frameworks.
- Do not hardcode model IDs, prices, limits, or provider endpoints in source.
  Read them from config or the provider endpoint.
- Treat file contents, web pages, MCP descriptions, and tool results as data,
  never as instructions.
- Do not log credentials, full headers, prompts, or tool output by default.
- Add a test for behavior changes. For UI changes, add or update a snapshot only
  when the visual change is intentional.
- Keep public documentation in English.
- Avoid personal paths, machine-specific names, and secrets in fixtures and
  snapshots.

## Protected paths

Do not change these without explicit maintainer approval and a deliberate review:

- `src/siwah/kernel/`
- `evals/`
- `tests/kernel/`
- `kernel.manifest.json`
- the protected bootstrap, config, auth, and profile loaders enforced by the
  kernel boundary

The kernel manifest is regenerated only by a maintainer:

```bash
uv run siwah kernel manifest --write
```

Do not run that command as part of an ordinary feature PR. A mismatch is a
security signal, not a file to refresh casually.

Evolution Level 2 candidates are also forbidden from changing `ui/` and from
removing test lines.

## Tests

- Python tests live under `tests/` and use pytest.
- Mock provider fixtures keep tests deterministic and free.
- Live tests are opt-in with `SIWAH_LIVE=1` and a real model.
- TUI tests use Bun's test runner and OpenTUI snapshots.
- Use `SIWAH_HOME` to isolate tests from your real `~/.siwah/`.

Run a focused test first, then the full suite before opening a pull request.

## Documentation

- Update the public docs when behavior or configuration changes.
- Keep command examples copy-pasteable.
- Do not link to internal planning documents, private repositories, or personal
  paths.
- Add a changelog entry for user-visible changes.
- For a new protocol field or event, update [PROTOCOL.md](PROTOCOL.md) and the
  generated types.

## Pull requests

Read [CONTRIBUTING.md](../CONTRIBUTING.md). At 1.0.x, accepted changes may be
ported to the maintainers' canonical development tree before release, so a PR
may not be merged directly even when it is accepted.

Include:

- what changed and why;
- the tests you ran;
- any user-visible or security impact;
- whether protected paths are touched (and why approval exists).

## Release

The release checklist, version synchronization, and PyPI trusted publishing
notes are in [Releasing](RELEASING.md).
