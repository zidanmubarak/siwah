# Releasing

This page is for maintainers. Releases are explicit: the build script never
publishes, and no release is rebuilt or retagged after publication.

Keep the public branch at the last published release. Develop and commit
unreleased work in the private checkout, and sync/push the public repository
only as part of a versioned release.

The 1.2.0 release uses this order:

1. source bootstrap (primary install);
2. PyPI publication for package installs.

## Commit boundaries

Keep feature and fix commits separate from release metadata. A normal commit may
contain code, tests, generated contracts, and the kernel manifest. Changelog
entries belong in a separate `docs(changelog)` commit. Version fields,
lockfiles, and the final changelog section belong in the approved
`release: siwah X.Y.Z` commit.

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
git commit -m "release: siwah 1.2.0"
git tag -a v1.2.0 -m "siwah 1.2.0"
git push origin main
git push origin v1.2.0
```

The update checker reads the highest release tag matching `vX.Y.Z` and falls
back to `pyproject.toml` on `origin/main` only when no version tags exist. An
ordinary commit is not announced in the TUI. GitHub Releases are useful for
humans and release notes; `/update` does not require a Release object.

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

Each release page contains only that version's changelog section. `CHANGELOG.md`
keeps the full project history; the release workflow extracts the matching
section with:

```bash
python scripts/release_notes.py v1.2.0 > release-notes.md
```

Then create the release with that file, not the whole changelog:

```bash
gh release create v1.2.0 \
  --title "siwah 1.2.0" \
  --notes-file release-notes.md \
  dist/siwah-1.2.0-py3-none-any.whl \
  dist/siwah-tui-windows-amd64.exe \
  dist/siwah-tui-linux-x86_64 \
  dist/siwah-tui-darwin-arm64 \
  dist/SHA256SUMS
```

Adjust the binary names to the artifacts you actually built. Attach checksums and
keep the release notes honest about known limitations. A release fails if the
version has no matching `## [x.y.z]` section, so add the changelog entry before
tagging.

## PyPI publication

1. Confirm the version is still unclaimed and the wheel hash matches the tested
   artifact.
2. Publish the wheel to PyPI. Do not rebuild it:

   ```bash
   uvx twine upload dist/siwah-X.Y.Z-py3-none-any.whl
   ```

   Use `TWINE_USERNAME=__token__` and a PyPI token in `TWINE_PASSWORD` from a
   secret manager. Never commit a token or put one in a command captured in logs.
3. Verify `pip`/`uv` install in a clean environment. The first interactive
   launch downloads the matching TUI binary and verifies its checksum.
4. Confirm the GitHub Release and the source bootstrap point at the same version.

Recommended authentication: **PyPI trusted publishing**. Configure the PyPI
project with the GitHub owner, repository, workflow filename, and a protected
environment such as `pypi`. Then use `pypa/gh-action-pypi-publish` with
`id-token: write`; no long-lived PyPI token is stored.

The repository includes `.github/workflows/publish.yml`, a manual
`workflow_dispatch` job for PyPI. Configure a matching GitHub `pypi`
environment and a trusted publisher before using it. A maintainer may also
upload explicitly with `twine` and a short-lived API token from a secret
manager. In either case:

- never commit a PyPI token;
- never reuse a version that is already published;
- never replace an artifact after publication.

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
- [ ] Changelog is updated and `scripts/release_notes.py vX.Y.Z` returns only
      that version's section.
- [ ] Tag `vX.Y.Z` is created and pushed.
- [ ] Artifacts and checksums are attached to the GitHub Release.
- [ ] PyPI wheel is published and verified.
- [ ] Update feed and install paths are checked.
