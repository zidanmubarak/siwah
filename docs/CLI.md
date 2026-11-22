# CLI reference

The `siwah` command is a Typer application. Running it without a subcommand
starts the TUI when the terminal is interactive.

```bash
siwah                                  # TUI (starts the daemon if needed)
siwah --version
siwah --ui plain                       # hint only; use `siwah run ... --ui plain`
siwah --allow-all                      # TUI with yolo permissions
```

## Global options

| Option | Values | Meaning |
|---|---|---|
| `--version` | flag | Print `siwah <version>` and exit. |
| `--ui` | `tui`, `plain` | Select the interface when no subcommand is given. `plain` without a subcommand only prints a hint. |
| `--allow-all`, `--bypass` | flag | Start the TUI with auto-approved tools, equivalent to `/permissions yolo`. |

## Command index

| Command | Purpose |
|---|---|
| [`run`](#run) | Run one task non-interactively |
| [`serve`](#serve) | Start the daemon and local HTTP + SSE API |
| [`acp`](#acp) | Serve the Agent Client Protocol over stdio |
| [`evolve`](#evolve) | Run, pause, or inspect evolution |
| [`rollback`](#rollback) | Restore protected paths or a harness tag |
| [`audit`](#audit) | Show kernel audit events |
| [`trace`](#trace) | Show per-step traces for a session |
| [`eval`](#eval) | Run an evaluation suite |
| [`doctor`](#doctor) | Check local readiness |
| [`clean`](#clean) | Remove stale local artifacts |
| [`update`](#update) | Check for or apply an update |
| [`openapi`](#openapi) | Print the OpenAPI schema |
| [`auth`](#auth) | Manage provider credentials |
| [`models`](#models) | List and probe models |
| [`skills`](#skills) | List and view harness skills |
| [`memory`](#memory) | Inspect layered memory |
| [`mcp`](#mcp) | Manage MCP servers |
| [`kernel`](#kernel) | Verify or regenerate the kernel manifest |

## `run`

```text
siwah run PROMPT [OPTIONS]
```

Run one task and exit. The default UI is `plain`; `--json` (or `--ui json`)
emits newline-delimited JSON events instead of human-readable output.

| Option | Default | Meaning |
|---|---|---|
| `--json` | off | Emit NDJSON events. |
| `--model` | role `main` | `provider/model-id` override. |
| `--permission` | config (`ask`) | `ask`, `auto-edit`, or `yolo`. |
| `--auto`, `--bypass` | off | Alias for `--permission yolo`. |
| `--effort` | config | `minimal`, `low`, `medium`, `high`, `xhigh`, or `max`. |
| `--cwd` | current directory | Workspace directory. |
| `--max-steps` | config | Hard model-step limit; `0` disables the cap for this run. |
| `--ultra` | off | Run 2–5 best-of-N attempts in git worktrees. Requires a clean repository. |
| `--show-reasoning` | off | Print reasoning deltas in plain mode. |
| `--ui` | `plain` | `plain` or `json`. |

Examples:

```bash
siwah run "Fix the failing test in this repo" --permission auto-edit
siwah run "Plan the refactor" --effort max --auto
siwah run "Summarize the project" --json
siwah run "..." --ui plain
siwah run "..." --model opencode-go/<model-id>
siwah run "..." --ultra 3
```

`--ultra` creates parallel worktrees, applies the first passing attempt to the
main worktree, and exits nonzero if no attempt passes. It refuses a dirty
repository.

## `serve`

```text
siwah serve [--host HOST] [--port PORT]
```

Start the daemon in the foreground. The default host and port come from
`[server]` (`127.0.0.1` and an ephemeral port). If a fixed port is busy, the
daemon falls back to a free port and prints it. The actual endpoint, token, and
PID are written to `~/.siwah/runtime.json` with mode 600.

```bash
siwah serve
siwah serve --host 127.0.0.1 --port 8765
```

Use `siwah serve` for debugging. Normal use goes through the launcher and the
supervisor.

## `acp`

```text
siwah acp
```

Start an Agent Client Protocol v1 bridge over stdin/stdout. It supports
`initialize`, `session/new`, `session/prompt`, `session/cancel`, and
`session/update`. Text prompts only. Requests requiring interactive approval are
denied, because client permission dialogs are not implemented. See
[SDK](SDK.md#acp-bridge).

## `evolve`

```text
siwah evolve [log] [OPTIONS]
```

Run one Level 0–1 cycle, pause or resume evolution, or show the audit log. With
no `--seed` or `--live`, the command refuses to run.

| Option | Meaning |
|---|---|
| `log` | Show the latest evolution audit events instead of running a cycle. |
| `--pause` | Pause before the next cycle. |
| `--resume` | Resume cycles. |
| `--reset-cooldown` | Clear only the cooldown timestamp after a failed preflight; daily cycles and allowance remain. |
| `--seed` | Run the deterministic cold-start synthetic cycle. |
| `--live` | Probe with the real evaluation suite. Requires `SIWAH_LIVE=1`, a positive `--max-allowance`, and a model marked `training=false`. |
| `--max-allowance` | Per-probe allowance cap for `--live`. |
| `--probe-limit` | Limit tasks per live probe (minimum 1). |
| `--model` | `provider/model-id` for live reflection and probing. The model needs a privacy profile; see [Evolution](EVOLUTION.md#before-a-live-cycle). |
| `--patch-file` | Run the Level 2 gate for a unified diff. |
| `--harness-dir` | Override the editable harness directory. |
| `--weekly-allowance` | Weekly allowance budget; falls back to `SIWAH_WEEKLY_ALLOWANCE`. |
| `--limit` | Number of log events (1–500, default 50). |

Examples:

```powershell
$env:SIWAH_LIVE = "1"
siwah models probe opencode-go/<model-id>
siwah models privacy opencode-go/<model-id> --training false
siwah evolve --live --model opencode-go/<model-id> --max-allowance 0.05
```

```bash
siwah evolve --seed
siwah evolve --live --seed --probe-limit 2
siwah evolve --patch-file fix-edit-file.diff
siwah evolve --pause
siwah evolve --resume
siwah evolve --reset-cooldown
siwah evolve log
```

`--patch-file` cannot be combined with `--seed` or `--live`. `--pause` and
`--resume` are mutually exclusive. `--reset-cooldown` is a standalone recovery
action after a privacy or configuration preflight; it does not bypass the daily
cycle or allowance limits. A Level 2 patch exits 0 only when it is promoted.

Proposal output defaults to the model profile's `max_output`. Override it with
`SIWAH_EVOLVE_MAX_TOKENS`: a positive integer sets an explicit cap, while `none`
or `0` omits `max_tokens`. See [Evolution](EVOLUTION.md#proposal-output-budget).

## `rollback`

```text
siwah rollback [TAG]
```

Restore protected paths from a healthy tag, or restore the harness when the tag
starts with `evo-`. With no tag, the latest healthy tag is used. Dirty or
conflicting rollbacks are refused rather than discarding your edits.

```bash
siwah rollback
siwah rollback kernel-healthy-<hash12>
siwah rollback evo-12
```

## `audit`

```text
siwah audit [--limit N]
```

Print the newest hash-chained audit events as JSON, one per line. `--limit`
accepts 1–500 and defaults to 20.

## `trace`

```text
siwah trace SESSION_ID [--limit N] [--json]
```

Show per-step model, latency, tokens, allowance, and tool names for a session.
`--limit` accepts 1–10000 and defaults to 200. An unknown session exits 2.

## `eval`

```text
siwah eval [OPTIONS]
```

Run an evaluation suite and print aggregate metrics.

| Option | Default | Meaning |
|---|---|---|
| `--suite` | `golden` | `golden`, `heldout`, or `traps`. |
| `--harness` | `siwah` | `siwah` or `naive`. |
| `--tier` | `t3` | `t0` structural, `t1` screen, `t2` relevant tasks, `t3` full suite. |
| `--model` | unset | Live model reference; defaults to the configured main model with `--live`. Without `--live`, the deterministic mock harness is used. |
| `--seed` | `0` | Task seed. |
| `--runs` | `1` | Runs per task (1–10). |
| `--max-steps` | `8` | Step budget per task (1–100). |
| `--limit` | unset | Limit the number of tasks. |
| `--only` | unset | Comma-separated task IDs. |
| `--baseline` | off | Also run the fixed naive baseline. Only valid with `--harness siwah`. |
| `--live` | off | Use the real provider and model; requires `SIWAH_LIVE=1` and a positive `--max-allowance`. |
| `--max-allowance` | unset | Allowance cap for a live run. |
| `--no-cache` | off | Ignore the baseline cache. |
| `--write-budget` | off | Write a calibrated `[budget]` table; requires live, non-T0 data. |
| `--json` | off | Print the report as JSON. |
| `--output` | unset | Report path. |
| `--eval-dir` | unset | Evaluation fixture root. |

```bash
siwah eval --suite golden
siwah eval --suite golden --baseline
siwah eval --suite golden --tier t0
siwah eval --suite golden --only golden-verifier-01 --baseline --no-cache
siwah eval --suite traps
SIWAH_LIVE=1 siwah eval --suite golden --live --model opencode-go/<model-id> --max-allowance 0.05
```

Without `--live`, the command runs the deterministic mock harness and prints a
notice to stderr. With `--live` and no `--model`, it uses the configured
`[roles].main` model and the configured provider credentials. The mock harness
is never used for a live run.

`--write-budget` refuses mock measurements, T0 runs, and runs stopped at the
allowance cap. Evaluation needs a source checkout or an explicit eval directory;
see [Evaluation](EVALUATION.md).

## `doctor`

```text
siwah doctor
```

Report the version, home, config, kernel manifest, budget, evolution settings,
auth file, harness, project-local counts, providers, state database, artifacts,
logs, TUI binary, Bun, and main model. Exit 1 when a problem is detected.

## `clean`

```text
siwah clean [OPTIONS]
```

Remove stale local artifacts under `~/.siwah`.

| Option | Default | Meaning |
|---|---|---|
| `--sessions`, `--no-sessions` | sessions on | Remove artifacts for sessions that no longer exist in `state.db`. |
| `--logs` | off | Rotate `daemon.log` when it exceeds the threshold. |
| `--dry-run` | off | Show what would be removed. |
| `--yes` | off | Do not ask for confirmation. |

Run `siwah clean --sessions --dry-run` first.

## `update`

```text
siwah update [OPTIONS]
```

Check the upstream branch for a newer version, or apply it.

| Option | Meaning |
|---|---|
| `--apply` | Fast-forward the editable checkout and rebuild the TUI. |
| `--refresh` | Ignore the six-hour check cache. |
| `--json` | Print JSON. |

Applying requires a source checkout. A dirty checkout is refused. A wheel-only
install can see the notification but cannot apply it.

## `openapi`

```text
siwah openapi
```

Print the OpenAPI 3.1 schema used to generate TypeScript types. Redirect it to a
file when you need an SDK contract snapshot.

## `auth`

```text
siwah auth login [PROVIDER]
siwah auth status
siwah auth logout PROVIDER
```

- `login` prompts for the provider if omitted and reads the key with hidden
  input. The key is stored in `~/.siwah/auth.json`.
- `status` shows whether each provider's environment variable and auth-file
  credential are set. It never prints a secret.
- `logout` removes a stored key.

## `models`

```text
siwah models list [--provider NAME]
siwah models probe [MODEL_REF] [--provider NAME] [--model ID]
siwah models privacy MODEL_REF --training false|true|unknown [--retention-days N]
```

- `list` reads the live catalog. Without `--provider`, all configured providers
  are queried. Providers without credentials are reported and skipped. The
  `training` column shows the privacy assertion stored in the model profile
  (`false`, `true`, or `?` when unmarked).
- `probe` writes a capability profile under
  `~/.siwah/harness/profiles/<provider>__<model>/` and reports
  `training=unmarked` when the provider catalog has no privacy metadata.
- `privacy` records a human assertion in an existing profile. Live evolution
  requires `--training false`; only set it when the provider contract confirms
  that the model does not train on the data. Re-probing preserves an existing
  privacy assertion.

## `skills`

```text
siwah skills list
siwah skills view NAME
```

`list` prints the name and description of global and project skills. `view`
prints the full Markdown body.

## `memory`

```text
siwah memory show [--cwd PATH]
```

Print user, project, and quick memory with their file paths. `--cwd` selects the
workspace used to resolve project memory.

## `mcp`

```text
siwah mcp path
siwah mcp list [--tools]
siwah mcp add NAME (--command CMD | --url URL) [OPTIONS]
siwah mcp remove NAME
siwah mcp show NAME
siwah mcp status [SERVER]
siwah mcp tools [SERVER]
siwah mcp test SERVER
```

`add` options:

| Option | Meaning |
|---|---|
| `--command` | stdio command line, for example `"npx -y server"`. |
| `--arg` | Extra stdio argument; repeatable. |
| `--url` | Streamable HTTP endpoint. |
| `--header` | HTTP header `Name=Value`; repeatable. Only with `--url`. |
| `--env` | stdio environment `Name=Value`; repeatable. |
| `--cwd` | stdio working directory. |
| `--timeout` | Request timeout in seconds (default 30). |
| `--description` | Short server description. |
| `--transport` | `stdio` or `http`; must match the command or URL. |
| `--force` | Replace an existing server. |

`--arg`, `--env`, and `--cwd` are only valid with `--command`. `--header` is only
valid with `--url`. `show` redacts header and environment values. `status`
prints connected/error, tool count, and latency. `list --tools`, `tools`, and
`test` exit 1 when a server fails to connect.

## `kernel`

```text
siwah kernel verify
siwah kernel manifest [--write]
```

- `verify` checks the committed SHA-256 manifest. Exit 0 when it matches, 1 on a
  mismatch, and 2 when no manifest is present.
- `manifest` prints the manifest path and status. `--write` regenerates it and
  is a human-only operation. Do not run it as part of a normal agent task.

## Hidden command

`siwah supervise` is hidden and launcher-managed. It verifies the kernel
manifest, starts the daemon with a one-boot token and challenge nonce, checks the
HMAC proof on `/v1/health`, and rolls back protected paths after two consecutive
failed boots. Use `siwah` rather than calling it directly.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Success. For `eval`, every selected task passed and validation was clean. |
| `1` | Operation failed: run error, eval failure, update failure, doctor problem, TUI launch failure, or an explicit negative result. |
| `2` | Usage, configuration, or validation error; also used for an unknown session and missing prerequisites. |
| `130` | Interrupted (Ctrl+C / `KeyboardInterrupt`). |

For `--ultra`, exit 1 means no attempt was promoted. For a Level 2
`--patch-file`, exit 1 means the patch was rejected. For `siwah update --apply`,
exit 1 means the update could not be applied.
