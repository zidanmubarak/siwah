# Changelog

All notable public changes are recorded here. The format follows Keep a
Changelog, and the project uses semantic versioning.

## [1.0.1] — 2026-11-19

Patch release for the public install and release pipeline.

### Fixed

- The Windows and Unix bootstraps detect a pre-public checkout with unrelated
  history and print a clear migration message instead of a raw fast-forward
  error.
- Release checksum generation no longer includes `SHA256SUMS` in its own hash
  list.

### Changed

- The README terminal preview inlines SVG text styling so it renders
  consistently on GitHub and image proxies.

## [1.0.0] — 2026-11-19

First public release.

### Added

- A local Python daemon with a FastAPI HTTP + SSE API, SQLite session storage,
  and a versioned protocol.
- An OpenTUI + Solid terminal interface with streaming Markdown, tool cards,
  unified and split diffs, task progress, session tabs, image paste, persisted
  prompt history, five themes, and a `NO_COLOR` mode.
- A headless `siwah run` command with plain and NDJSON output.
- An Agent Client Protocol v1 stdio bridge for editors.
- Configuration-driven providers over OpenAI Chat Completions-compatible
  endpoints, with live model discovery, capability probes, role routing,
  fallback models, and per-step allowance accounting.
- File, edit, search, shell, web, todo, repository-map, symbol, reference,
  memory, skill, recall, tool-output, subagent, job, and MCP tools.
- Layered memory and an editable, git-backed harness for prompts, tool guidance,
  skills, commands, subagents, hooks, profiles, and MCP configuration.
- Project-local overrides under `<repo>/.siwah/` and `AGENTS.md` discovery.
- Permission modes (`ask`, `auto-edit`, `yolo`), per-tool/path/command rules, a
  default secret deny list, and catastrophic-command denial.
- An application-level path guard with protected source and evaluation paths.
- An opt-in Docker or bubblewrap command sandbox with an explicitly
  non-isolated local fallback.
- Shadow-git checkpoints, `/undo`, `/rewind`, fork-from-checkpoint, and a
  checkpoint browser.
- A project verifier with language auto-detection and a bounded repair loop.
- Read-only plan mode and `/execute-plan`.
- Context pruning, large-output offload, structured compaction, and a live
  context meter.
- Parallel best-of-N attempts in git worktrees (`siwah run --ultra`).
- Evaluation suites: 30 golden tasks, 3 public heldout samples, and 3 traps;
  mock and live modes; T0–T3 tiers; baseline comparison; report export.
- Evolution Levels 0–2 with a safety gate, worktree review for core patches,
  diff limits, budget governor, canary monitor, tags, rollback, and a
  hash-chained audit trail.
- A protected kernel manifest, a supervisor with HMAC boot proofing, healthy
  boot tags, and automatic rollback after two failed boots.
- Source bootstrap installers for Windows and macOS/Linux, a compiled TUI
  binary, a wheel, checksums, and a TestPyPI verification path.
- Public documentation for setup, configuration, the CLI, architecture, the
  protocol, the SDK surfaces, evaluation, evolution, security, terminal support,
  development, releasing, and troubleshooting.

### Changed

- The public install path uses `github.com/zidanmubarak/siwah` on `main` and does
  not require a GitHub login.
- The source bootstrap keeps an editable checkout so `/update` can fast-forward
  and rebuild the TUI.
- Version `1.0.0` is the first public package; TestPyPI is used for release
  verification before final PyPI publication.

### Security

- Provider keys are stored in `~/.siwah/auth.json` with mode 600 and are read
  from the environment first.
- The daemon writes a per-start bearer token to `~/.siwah/runtime.json` with mode
  600 and binds loopback by default.
- The supervisor's one-boot nonce is never written to `runtime.json`.
- Memory and skill writes reject credential-like content; recall results are
  redacted.
- Updates use the normal git credential helper, refuse dirty checkouts, and never
  embed a GitHub token.

### Known limitations

- The path guard is application-level. A malicious `bash` command can escape the
  workspace unless the opt-in sandbox is enabled.
- Only the OpenAI Chat Completions-compatible provider family is implemented.
- Evolution canary sampling and automatic post-session rollback are not
  end-to-end.
- Level 2 recovery from a delayed daemon boot failure is not fully journaled.
- ACP is minimal: text prompts only, no client permission dialogs, no
  `loadSession`, no filesystem or terminal methods.
- macOS Terminal and iTerm2 are not verified.
- The full heldout suite is distributed separately from the public repository.
- Some model-specific harness overlays may not resolve consistently through the
  gate and loader.
- A wheel install does not include the TUI binary or evaluation fixtures.

## 0.9.x — pre-release series

The 0.9.x line was the final pre-1.0 development series. It introduced:

- the Python core loop, session storage, provider streaming, tool calls, and
  accounting;
- the OpenTUI + Solid terminal interface, including the streaming transcript,
  tool cards, diffs, panels, themes, and compiled TUI binary;
- checkpoints, the verifier, plan mode, context compaction, and tool-output
  offload;
- subagents, MCP, hooks, background jobs, custom commands, memory, and skills;
- the command sandbox, best-of-N worktrees, and the ACP bridge;
- evaluation suites and the evolution gate, canary, budget, and rollback work;
- kernel protection, the manifest, audit chain, and supervisor;
- release artifacts and a TestPyPI-first packaging flow.

These builds were distributed for testing and are superseded by 1.0.0. They are
not supported for new installs.
