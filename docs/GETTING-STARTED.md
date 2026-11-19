# Getting started

This guide takes you from a clean machine to a working siwah session. It assumes
you are comfortable with a terminal and have a provider API key.

## Requirements

- Python 3.11 or newer
- [uv](https://docs.astral.sh/uv/) for Python environments and the `siwah` tool
- Git
- [Bun](https://bun.sh/) 1.3.14+ if you run the TUI from source or build it
- An OpenAI Chat Completions-compatible provider, such as OpenCode Go or OpenAI

A compiled `siwah-tui` binary does not need Bun at runtime. A wheel install does
not include that binary, so `--ui plain` is the fallback when no TUI is present.

## Install

### Bootstrap

Windows PowerShell:

```powershell
irm https://raw.githubusercontent.com/zidanmubarak/siwah/main/scripts/install/bootstrap.ps1 | iex
```

macOS/Linux:

```bash
curl -fsSL https://raw.githubusercontent.com/zidanmubarak/siwah/main/scripts/install/bootstrap.sh | bash
```

The bootstrap:

1. installs `uv` and Bun when they are missing;
2. clones the repository to `~/.siwah/src`;
3. installs the editable `siwah` tool;
4. builds the TUI binary into the source tree and installs it for the launcher.

Open a new terminal so the tool directory is on `PATH`, then check the install:

```bash
siwah --version
siwah doctor
```

If `siwah` is not found, run `uv tool update-shell` and open a new terminal.

### Manual checkout

Use this when you want to control the checkout location or work on siwah itself:

```bash
git clone https://github.com/zidanmubarak/siwah.git
cd siwah
uv sync --extra dev
uv tool install --force --editable .
cd ui/tui
bun install --frozen-lockfile
bun run build
cd ../..
siwah --version
```

`--editable` matters. The launcher keeps the TUI source discoverable, and
`/update` fast-forwards this checkout and rebuilds the binary. A wheel-only
install can show an update notice but cannot apply it.

### TestPyPI verification

The 1.0.0 wheel is also uploaded to TestPyPI to verify the packaged build. It is
not the recommended install. See [Releasing](RELEASING.md) for the verification
steps.

## First run

Start the terminal interface:

```bash
siwah
```

The launcher starts the supervisor and daemon if they are not running, waits for
a healthy endpoint, then opens the TUI. On first use siwah creates:

- `~/.siwah/harness/config.toml` with provider templates;
- `~/.siwah/harness/` as a git-backed editable harness;
- `~/.siwah/state.db` for sessions and messages.

### Connect a provider

In the TUI, run:

```text
/connect
```

Choose `opencode-go`, `openai`, or another provider from the config, then paste
the key. The key is written to `~/.siwah/auth.json` (mode 600) and is never
rendered in the transcript. If `/connect` is not convenient, the CLI equivalent
is:

```bash
siwah auth login opencode-go
siwah auth status
```

Credentials are resolved from the provider's configured environment variable
first, then from the auth file. For example, `OPENCODE_API_KEY` takes precedence
over a key stored by `/connect`.

### Choose a model

Run `/models` and pick a model. siwah reads the catalog from the provider's
models endpoint; model IDs, context limits, prices, and reasoning support are not
hardcoded. You can inspect the same catalog from the CLI:

```bash
siwah models list --provider opencode-go
siwah models probe opencode-go/<model-id>
```

`models probe` writes a capability profile under
`~/.siwah/harness/profiles/<provider>__<model>/`. The profile records tool
support, parallel tool calls, the reasoning field, context and output limits,
cache behavior, and privacy metadata. Probe a model before relying on reasoning
effort or a model-specific overlay.

### Send your first task

Open a project directory first; the TUI uses the directory where you launched
`siwah` as the workspace:

```bash
cd ~/projects/example
siwah
```

Then send something concrete:

```text
Read the README, find the test command, run it, and fix the first failure.
```

What you will see:

- streamed Markdown and tool cards;
- a permission panel when an edit or shell command needs approval;
- a task list when the agent plans multiple steps;
- a verifier result when the project defines or auto-detects checks;
- a final summary with token and allowance usage.

Permission choices are `once`, `session`, `always`, and `deny` (with an optional
reason). A denied call is returned to the model as a tool error, so the agent can
adjust instead of silently failing.

## Headless use

`siwah run` is non-interactive:

```bash
siwah run "Summarize this repository" --ui plain
siwah run "Fix the failing test" --permission auto-edit
siwah run "Run the test suite and report failures" --permission yolo
siwah run "Summarize the project" --json
```

`siwah run` uses the `ask` mode by default. In CI or a pipe there is no one to
answer, so choose `--permission auto-edit` for edits and
`--permission yolo` for shell commands. The path guard and catastrophic-command
denial still apply.

Other useful flags:

```bash
siwah run "..." --model opencode-go/<model-id>
siwah run "..." --effort high
siwah run "..." --max-steps 20
siwah run "..." --cwd /path/to/project
siwah run "..." --ultra 3      # best-of-N in git worktrees; requires a clean repo
```

Exit codes are documented in the [CLI reference](CLI.md). In short: `0` success,
`1` run error, `2` configuration or usage error, and `130` interrupted.

## Set up a project

siwah reads `AGENTS.md` from the workspace and every parent directory. To create
or refresh project instructions and project memory, run:

```text
/init
```

This creates `<repo>/.siwah/project.md` and guides the agent to update
`AGENTS.md`. Project-local extensions override global harness entries with the
same name:

```text
my-project/
├─ .siwah/
│  ├─ skills/<name>/SKILL.md
│  ├─ subagents/<name>.md
│  ├─ commands/<name>.md
│  ├─ project.md
│  └─ plan.md
├─ AGENTS.md
└─ ...
```

Use project-local entries for repository-specific procedures. Use the global
harness under `~/.siwah/harness/` for reusable defaults.

## Where siwah keeps data

| Path | Purpose |
|---|---|
| `~/.siwah/harness/config.toml` | Providers, roles, permissions, budgets, and runtime settings |
| `~/.siwah/auth.json` | Provider credentials (mode 600) |
| `~/.siwah/harness/` | Editable prompts, guidance, skills, commands, subagents, profiles, hooks |
| `~/.siwah/state.db` | Sessions, messages, tool calls, and traces |
| `~/.siwah/checkpoints/<session-id>/` | Shadow-git snapshots |
| `~/.siwah/tool_outputs/<session>/` | Offloaded large tool output |
| `~/.siwah/eval/` | Evaluation reports and baseline cache |
| `~/.siwah/logs/daemon.log` | Daemon and supervisor logs |
| `~/.siwah/runtime.json` | Current daemon host, port, token, and PID (mode 600) |
| `~/.siwah/audit.jsonl` | Hash-chained audit events |
| `~/.siwah/budget-state.json` | Evolution budget counters |

Set `SIWAH_HOME` to use a different root, which is useful for tests and isolated
demos.

## Update

The TUI checks the upstream branch after startup and shows `↑ vX.Y.Z` when a
newer version is available. To apply an update:

```text
/update
```

or:

```bash
siwah update
siwah update --apply
siwah update --refresh
```

Applying an update requires the editable source checkout created by the
bootstrap. A checkout with local changes is refused, so your edits are never
overwritten. The new TUI binary is staged as `siwah-tui[.exe].next` and activated
on the next launch, because Windows cannot replace a running executable.

## Evaluation and evolution need a source checkout

The public repository includes the evaluation runner and 30 golden tasks, but a
wheel install does not include the `evals/` tree. To run evaluation or evolution:

- use a source checkout (the bootstrap provides one at `~/.siwah/src`), or
- pass `--eval-dir <path>` to `siwah eval`, or
- set `SIWAH_EVAL_DIR` to a complete evaluation tree.

The public repository ships three heldout samples. The full heldout suite is
distributed separately; point `SIWAH_HELDOUT_DIR` at it when you have it. See
[Evaluation](EVALUATION.md) for the suite layout.

## Next steps

- [Configuration](CONFIGURATION.md) — providers, roles, permissions, sandbox,
  budgets, and environment variables.
- [CLI reference](CLI.md) — every command and option.
- [Architecture](ARCHITECTURE.md) — how the daemon, supervisor, and kernel fit
  together.
- [Security](SECURITY.md) — the threat model and the limits of the path guard.
- [Troubleshooting](TROUBLESHOOTING.md) — PATH, provider, daemon, and TUI
  problems.
