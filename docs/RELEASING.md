# Releasing

This page is for maintainers. Releases are explicit: the build script never
publishes, and no release is rebuilt or retagged after publication.

The 1.0.2 release uses this order:

1. source bootstrap (primary install);
2. TestPyPI upload for package verification;
3. final PyPI publication after verification.

## Version synchronization

Update every version location together:

- `pyproject.toml` (`[project].version`);
- `src/siwah/version.py` (`__version__`);
- `ui/tui/package.json` (`version`);
- `ui/tui/src/index.tsx` (`siwah-tui X.Y.Z`);
- `uv.lock` (the editable `siwah` package version);
- `bun.lock` (the `ui/tui` workspace version);
- `ui/tui/openapi.json` (`info.version`, generated from the Python app);
- current install examples that name a version.

Then refresh the lockfiles:

```bash
uv lock
cd ui/tui && bun install
```

`pyproject.toml` and `src/siwah/version.py` are part of the protected inventory.
A version bump requires a deliberate human update of the kernel manifest:

```bash
uv run siwah kernel manifest --write
uv run siwah kernel verify
```

Only a maintainer performs this. Do not change kernel implementation or
evaluation tasks for a version bump. Restore any temporary local edit permission
afterward and keep the permission change out of the commit.

## Pre-release gate

Run the full gate from a clean checkout:

```bash
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run pytest

cd ui/tui
bun run lint
bun run typecheck
bun run test
bun run build
cd ../..

uv run siwah kernel verify
```

Regenerate the API contract and confirm it is clean:

```bash
uv run siwah openapi > ui/tui/openapi.json
cd ui/tui && bun run generate:types && cd ../..
git diff --exit-code -- ui/tui/openapi.json ui/tui/src/api/schema.d.ts
```

Run a packaging smoke test in a clean environment before tagging. At minimum:

```bash
uv build --wheel --out-dir dist
uv venv .tmp-release --clear
uv pip install --python .tmp-release/Scripts/python.exe dist/siwah-*.whl
.tmp-release/Scripts/siwah.exe --version
```

Use `.tmp-release/bin/python` and `.tmp-release/bin/siwah` on Unix. The wheel
does not contain the TUI binary; verify `--ui plain` works without it.

## Commit and tag

```bash
git add -A
git commit -m "release: siwah 1.0.2"
git tag -a v1.0.2 -m "siwah 1.0.2"
git push origin main
git push origin v1.0.2
```

The update checker reads `pyproject.toml` on `origin/main` and falls back to
semver tags matching `vX.Y.Z`. A release without a version bump is not announced
in the TUI. GitHub Releases are useful for humans and release notes; `/update`
does not require a Release object.

## Build artifacts

```bash
uv run python scripts/build-release.py
```

Artifacts are written to `dist/` (gitignored):

- `siwah-<version>-py3-none-any.whl` — the Python package;
- `siwah-tui-<platform>-<arch>[.exe]` — the compiled TUI for the current
  platform, when Bun is available;
- `SHA256SUMS` — SHA-256 for every artifact.

The wheel is platform-independent. The TUI binary is not: build it on each
supported platform (or in a matching CI runner) and attach the matching binary to
the release. Do not advertise a binary for a platform you did not build.

To create a shareable test bundle:

```bash
uv run python scripts/package-bundle.py
```

This writes `dist/siwah-<version>-<platform>-<arch>.zip` with the wheel, TUI
binary, `SHA256SUMS`, `install.ps1`, `install.sh`, and a short README.

## GitHub Release

```bash
gh release create v1.0.2 \
  --title "siwah 1.0.2" \
  --notes-file CHANGELOG.md \
  dist/siwah-1.0.2-py3-none-any.whl \
  dist/siwah-tui-windows-amd64.exe \
  dist/siwah-tui-linux-x86_64 \
  dist/siwah-tui-darwin-arm64 \
  dist/SHA256SUMS
```

Adjust the binary names to the artifacts you actually built. Attach checksums and
keep the release notes honest about known limitations.

## TestPyPI verification

Upload the wheel to TestPyPI first:

```bash
uvx twine upload --repository testpypi dist/siwah-1.0.2-py3-none-any.whl
```

Use `TWINE_USERNAME=__token__` and a TestPyPI token in `TWINE_PASSWORD` from a
secret manager. Never commit a token or put one in a command that is captured in
logs.

Verify in a clean environment with dependencies resolved from PyPI:

```bash
uv venv .tmp-testpypi --clear
uv pip install --python .tmp-testpypi/Scripts/python.exe \
  --index-url https://test.pypi.org/simple/ \
  --extra-index-url https://pypi.org/simple/ \
  "siwah==1.0.2"
.tmp-testpypi/Scripts/siwah.exe --version
.tmp-testpypi/Scripts/siwah.exe run "Reply with OK" --ui plain --model <provider>/<model-id>
```

Use the Unix paths on macOS/Linux. TestPyPI can lag or fail to mirror
dependencies; retry once before treating it as a packaging failure.

TestPyPI is a verification channel, not the supported user install.

## Final PyPI publication

After TestPyPI verification passes:

1. Confirm the version is still unclaimed and the wheel hash matches the tested
   artifact.
2. Publish the same wheel to PyPI. Do not rebuild it.
3. Verify `pip`/`uv` install in a clean environment.
4. Confirm the GitHub Release and the source bootstrap point at the same version.

Recommended authentication: **PyPI trusted publishing**. Configure the PyPI
project with the GitHub owner, repository, workflow filename, and a protected
environment such as `pypi`. Then use `pypa/gh-action-pypi-publish` with
`id-token: write`; no long-lived PyPI token is stored.

The repository includes `.github/workflows/publish.yml`, a manual
`workflow_dispatch` job with `testpypi` and `pypi` targets. Configure matching
GitHub environments and a trusted publisher for each index before using it. A
maintainer may also upload explicitly with `twine` and a short-lived API token
from a secret manager. In either case:

- never commit a PyPI token;
- never reuse a version that is already published;
- never replace an artifact after publication;
- keep the TestPyPI and PyPI wheels byte-identical when possible.

## Post-release

- Verify `siwah update` sees the new version from `origin/main`.
- Verify a clean source checkout can run `/update` and rebuild the TUI.
- Verify the wheel install and the TUI binary install.
- Check that the GitHub Release assets and `SHA256SUMS` agree.
- Update the changelog and close the release issue.
- Announce known limitations, especially any unverified platform or opt-in
  feature.

## Release checklist

- [ ] Version synced in every location.
- [ ] `uv lock` and `bun install` refreshed.
- [ ] Kernel manifest regenerated by a maintainer and verified.
- [ ] Ruff, Ruff format, Pyright, and pytest pass.
- [ ] TUI lint, typecheck, tests, and build pass.
- [ ] OpenAPI types are in sync.
- [ ] Wheel smoke test passes.
- [ ] Changelog is updated.
- [ ] Tag `vX.Y.Z` is created and pushed.
- [ ] Artifacts and checksums are attached to the GitHub Release.
- [ ] TestPyPI wheel is uploaded and verified.
- [ ] PyPI wheel is published and verified.
- [ ] Update feed and install paths are checked.
