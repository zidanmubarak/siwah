# Configuration

siwah is configured with a TOML file under the harness directory. The first run
creates it automatically, so a basic setup needs no manual editing: start the
TUI and run `/connect`, then `/models`.

## File locations

| Location | Purpose |
|---|---|
| `~/.siwah/harness/config.toml` | Main configuration |
| `~/.siwah/config.toml` | Legacy location; migrated on first load if the harness file is absent |
| `SIWAH_CONFIG` | Explicit config path (disables the legacy migration path) |
| `SIWAH_HOME` | Alternate root instead of `~/.siwah` |
| `~/.siwah/harness/tui.json` | TUI theme and keybindings |
| `~/.siwah/harness/mcp.json` | MCP servers |
| `~/.siwah/harness/hooks/*.json` | Hooks |
| `~/.siwah/harness/skills/<name>/SKILL.md` | Global skills |
| `~/.siwah/harness/subagents/*.md` | Global subagents |
| `~/.siwah/harness/commands/*.md` | Global slash commands |
| `~/.siwah/harness/prompts/system.md` | Base system prompt |
| `~/.siwah/harness/tool_guidance/*.md` | Appended tool guidance |
| `~/.siwah/harness/profiles/<provider>__<model>/` | Capability profiles and overlays |

Project-local files under `<repo>/.siwah/` override global entries with the same
name. `AGENTS.md` is read from the workspace and every parent directory.

Restart siwah after editing provider, role, server, or sandbox settings. The TUI
can change the permission mode and reasoning effort at runtime; the daemon reads
most other settings at startup.

## First-run file

The generated file contains two provider templates and conservative defaults:

```toml
[providers.opencode-go]
api = "openai_chat"
base_url = "https://opencode.ai/inference/go/openai/v1"
models_url = "https://opencode.ai/inference/go/v1/models"
auth = { scheme = "bearer", env = "OPENCODE_API_KEY" }
headers = { "User-Agent" = "siwah/{version}", "x-opencode-session" = "{session_id}" }
stream_usage = true
cost_field = "usage.opencode.allowance"

[providers.openai]
api = "openai_chat"
base_url = "https://api.openai.com/v1"
models_url = "https://api.openai.com/v1/models"
auth = { scheme = "bearer", env = "OPENAI_API_KEY" }
stream_usage = true
cost_field = "usage.cost"

[permissions]
mode = "ask"
```

Endpoints are defaults, not model facts. Model IDs, context limits, prices, and
reasoning capabilities come from the provider's live models endpoint.

## Providers

A provider is configuration, not code. In 1.0.0 the implemented adapter family is
`openai_chat`, which covers OpenAI Chat Completions-compatible endpoints.
Other API family names are rejected at startup.

```toml
[providers.example]
api = "openai_chat"
base_url = "https://api.example.com/v1"
models_url = "https://api.example.com/v1/models"
auth = { scheme = "bearer", env = "EXAMPLE_API_KEY" }
headers = { "User-Agent" = "siwah/{version}", "x-session" = "{session_id}" }
stream_usage = true
cost_field = "usage.cost"
max_retries = 2
timeout_s = 120
connect_timeout_s = 15
```

| Field | Default | Meaning |
|---|---|---|
| `api` | `openai_chat` | Adapter family. Only `openai_chat` is implemented in 1.0.0. |
| `base_url` | required | Base URL for chat completions; trailing slashes are stripped. |
| `models_url` | `null` | Models catalog endpoint. Without it, the provider cannot be listed or probed. |
| `auth.scheme` | `bearer` | `bearer` sends `Authorization: Bearer <key>`; `none` sends no auth. |
| `auth.env` | `null` | Environment variable consulted before the auth file. |
| `headers` | `{}` | Extra request headers. `{version}` and `{session_id}` are substituted. |
| `stream_usage` | `true` | Request usage in the final stream chunk when supported. |
| `cost_field` | `usage.opencode.allowance` | Dotted path to the allowance value in the usage object. |
| `max_retries` | `2` | Provider retry budget before surfacing an error. |
| `timeout_s` | `120` | Request timeout in seconds. |
| `connect_timeout_s` | `15` | Connection timeout in seconds. |

### Credentials

Credentials are resolved in this order:

1. the provider's `auth.env` environment variable;
2. the key stored in `~/.siwah/auth.json` (mode 600) by `/connect` or
   `siwah auth login`.

`siwah auth status` reports whether each mechanism is set without printing a
secret. `siwah auth logout <provider>` removes a stored key. Keys never appear in
logs, traces, trajectories, or the repository.

### Model references

A model reference is always `provider/model-id`, for example
`opencode-go/deepseek-v4.1-flash`. Slashes inside the model ID are allowed after
the first separator.

### Capability profiles

Profiles are written by:

```bash
siwah models probe opencode-go/<model-id>
```

They live under `~/.siwah/harness/profiles/<provider>__<model>/` and record the
API family, tool support, parallel tool calls, reasoning field, context and
output limits, edit format, cache mode, privacy metadata, allowance behavior, and
quirks. A profile may also contain a `system_append.md` overlay. Probe before
relying on an overlay, especially for model IDs containing slashes or dots.

## Roles

Roles map work to model references:

```toml
[roles]
main = "opencode-go/<model-id>"
plan = "opencode-go/<model-id>"
fast = "opencode-go/<model-id>"
reflector = "opencode-go/<model-id>"
eval_screen = "opencode-go/<model-id>"
fallback = ["opencode-go/<model-id>", "openai/<model-id>"]
```

| Role | Used for |
|---|---|
| `main` | Normal agent runs and the default for other roles |
| `plan` | Plan-oriented work |
| `fast` | Lightweight or summarization work |
| `reflector` | Evolution reflection and proposal |
| `eval_screen` | Screening evaluation before a larger gate |
| `fallback` | Ordered fallback list for retryable provider failures |

`main` is required for `siwah run` unless `--model` is passed. The TUI can choose
a model per session even when `main` is unset.

## Permissions

```toml
[permissions]
mode = "ask"

[[permissions.rules]]
tool = "bash"
command = "pytest*"
decision = "allow"

[[permissions.rules]]
path = "**/secrets/**"
decision = "deny"
```

| Mode | Reads | Writes | Shell and network |
|---|---|---|---|
| `ask` | allow inside allowed roots | prompt | prompt |
| `auto-edit` | allow inside allowed roots | allow | prompt |
| `yolo` | allow inside allowed roots | allow | allow |

Rules match a tool name, a path glob, a command glob, or a combination. The last
matching rule wins, so a specific deny can override an earlier broad allow.
Decisions are `allow`, `ask`, or `deny`.

The path guard always applies first:

- reads and writes must stay inside the workspace or `~/.siwah/harness/`;
- writes may also use explicitly granted evolution worktrees;
- protected source and evaluation paths are denied;
- secret-like paths are denied before a rule is considered.

Default `deny_paths`:

```toml
deny_paths = [
  ".env", "**/.env", ".env.*", "**/.env.*",
  "**/.ssh/**", "**/id_rsa", "**/id_ed25519",
  "**/.aws/credentials", "**/.config/gcloud/**",
  "**/credentials.json",
]
```

Override `deny_paths` only if you understand the exposure. Adding a path to the
rule list does not bypass the path guard; the guard runs first.

In `yolo`, catastrophic commands that target a filesystem root, format a disk,
or shut down the machine are still denied. This is an application-level control,
not an OS sandbox. See [Security](SECURITY.md).

## Agent behavior

```toml
[agent]
max_steps = 50
interactive_max_steps = 0
step_extension = 50
max_steps_ceiling = 300
stall_limit = 8
max_completion_nudges = 2
self_check_min_steps = 12
tool_output_limit = 100000
bash_timeout_s = 120
web_timeout_s = 30
max_retries = 2
# effort = "high"
```

| Setting | Default | Meaning |
|---|---|---|
| `max_steps` | `50` | Hard step budget for explicit and non-interactive runs, evaluation, and subagents. `--max-steps 0` disables the cap for `run`. |
| `interactive_max_steps` | `0` | Initial interactive budget. `0` means no cap. |
| `step_extension` | `50` | Steps added when an interactive run keeps making progress. |
| `max_steps_ceiling` | `300` | Upper bound for progress-based extensions. |
| `stall_limit` | `8` | Repeated unproductive steps before the stall guard stops a run. |
| `max_completion_nudges` | `2` | Completion nudges for memory cues and final checks. |
| `self_check_min_steps` | `12` | Minimum steps before a self-check nudge. |
| `tool_output_limit` | `100000` | Characters before tool output is offloaded to a file handle. |
| `bash_timeout_s` | `120` | Default shell command timeout. |
| `web_timeout_s` | `30` | Default web fetch timeout. |
| `max_retries` | `2` | Model retry budget. |
| `effort` | unset | Default reasoning effort: `minimal`, `low`, `medium`, `high`, `xhigh`, or `max`. |

Interactive runs have no step cap by default. They continue until the agent
finishes, you cancel, the provider allowance is exhausted, or the stall guard
stops the run. An explicit `--max-steps`, evaluation budget, or subagent budget
is always a hard limit.

## Verifier

Put `verify.toml` at the workspace root to override auto-detection:

```toml
[commands]
test = "pytest -q"
lint = "ruff check ."
typecheck = "pyright"
```

```toml
[verify]
auto = true
timeout_s = 120
max_repair_steps = 3
retry_flaky = true
keep_output_chars = 6000
```

Without `verify.toml`, siwah detects Python, JavaScript/TypeScript, Go, and Rust
projects. Failure output is fed back to the model, and a new failure can trigger
a bounded repair loop. Set `auto = false` to disable the verifier. Verifier
commands require their own shell approval, even in `auto-edit` mode.

## Context and checkpoints

```toml
[context]
keep_recent_tool_results = 50
auto_compact_percent = 95
# max_context_tokens = 200000
summary_max_chars = 32000

[checkpoints]
enabled = true
```

- `keep_recent_tool_results` controls how many recent tool results stay inline
  before older ones are pruned to stubs.
- `auto_compact_percent` is the context threshold at which siwah compacts
  automatically. The TUI shows `compact` and a warning color at that point.
- `max_context_tokens` overrides the model's advertised window. Leave it unset
  unless you need a lower limit.
- Checkpoints use a shadow git repository under
  `~/.siwah/checkpoints/<session-id>/`; your project `.git` is not modified.

## Server

```toml
[server]
host = "127.0.0.1"
port = 0
event_retention = 2000
```

`port = 0` asks the operating system for a free port. If a fixed port is busy,
the daemon falls back to a free port and records it in `~/.siwah/runtime.json`.
The daemon retries a bind race up to three times. `event_retention` is the
in-memory SSE replay window per session.

Bind to `127.0.0.1` unless you have a specific reason not to. There is no TLS
and no user model; a non-loopback bind exposes the bearer-token API to the
network.

## Sandbox

```toml
[sandbox]
enabled = false
backend = "auto"      # auto, docker, bwrap, or local
image = ""            # required for Docker
network = false
```

| Backend | Behavior |
|---|---|
| `auto` | Docker when an image is set and Docker is available; bubblewrap on Linux when available; otherwise local |
| `docker` | Requires Docker and an image. Fails if unavailable. |
| `bwrap` | Requires bubblewrap. Linux only. |
| `local` | Runs on the host with a reduced environment and a timeout. **Not isolation.** |

Docker and bubblewrap disable network by default and run commands with
`sh -lc`, including on Windows hosts. The image must contain the project's
toolchain. A timed-out Docker command is killed and its container removed.
Background commands with a configured sandbox fail closed rather than running
unsandboxed.

## Evolution policy

```toml
[budget]
max_allowance_per_cycle = 1.0
max_cycles_per_day = 3
reserve_weekly_pct = 40
max_concurrency = 2
cooldown_minutes = 60
max_wall_minutes = 45

[gate]
golden_regression_tolerance = 0.0
heldout_noninferiority = 0.02
min_gain = "pass_rate_up OR (steps_or_cost_down >= 0.10 with equal pass_rate)"
max_cost_ratio = 1.2
runs_per_task = 3

[canary]
sessions = 10
max_error_rate_increase = 0.15
max_correction_rate_increase = 0.15

[evolution]
auto = true
min_sessions = 3
cooldown_minutes = 60
```

- `budget` is enforced in allowance units, not list price. `reserve_weekly_pct`
  stops evolution before it consumes the reserve. The weekly limit is supplied
  by the caller (`--weekly-allowance` or `SIWAH_WEEKLY_ALLOWANCE`), because it is
  a provider fact.
- `gate` controls promotion: no golden regression, heldout non-inferiority
  within two percentage points, a defined gain condition, a cost ratio cap, and
  the number of runs per task.
- `canary` sets the sample count and the tolerated increase in error and
  correction rates.
- `evolution.auto` controls the post-session trigger, subject to
  `min_sessions` and `cooldown_minutes`.

See [Evolution](EVOLUTION.md) for how these values are used and what is not
guaranteed.

## Subagents, jobs, escalation, and traces

```toml
[subagents]
enabled = true
max_steps = 8
max_allowance = 0.2
max_depth = 1

[jobs]
max_jobs = 8
output_limit = 20000
tail_lines = 200

[escalation]
enabled = false
after_failures = 2
# model = "opencode-go/<model-id>"

[trace]
enabled = true
max_events = 2000
```

- `subagents.max_depth = 1` means children cannot launch further children.
- Jobs are process-local. They do not survive a daemon restart.
- Escalation is opt-in and emits a visible event when it switches models.
- Traces store per-step model, latency, token, allowance, and tool data. View
  them with `siwah trace <session-id>`.

## Environment variables

| Variable | Effect |
|---|---|
| `SIWAH_HOME` | Alternate data root; default `~/.siwah` |
| `SIWAH_CONFIG` | Explicit config file path |
| `SIWAH_SERVER` / `SIWAH_TOKEN` | Daemon endpoint and token for the TUI or a client |
| `SIWAH_SESSION` | Session to open at TUI startup |
| `SIWAH_WORKSPACE` | Workspace for sessions created by the TUI |
| `SIWAH_THEME` | `siwah-dark` or `siwah-light` |
| `SIWAH_TUI_CONFIG` | Alternate `tui.json` path |
| `SIWAH_TUI_BINARY` | Explicit compiled TUI binary |
| `NO_COLOR` | Select the monochrome theme |
| `SIWAH_PERMISSION_MODE` | Permission mode set by the launcher (`--allow-all`) |
| `SIWAH_LIVE` | Set to `1` to enable live evaluation and evolution |
| `SIWAH_LIVE_MODEL` | Default model for live evaluation when `--model` is omitted |
| `SIWAH_EVOLVE` | Set to `off` to disable evolution |
| `SIWAH_EVOLVE_AUTO` | Set to `0` to disable the automatic post-session trigger |
| `SIWAH_WEEKLY_ALLOWANCE` | Weekly allowance budget for evolution |
| `SIWAH_SUPERVISOR_KEY` | HMAC key path override; default `~/.siwah/supervisor.key` |
| `SIWAH_LSP` | Set to `off` to force the grep/ripgrep fallback for `refs` |
| `SIWAH_EVAL_DIR` | Complete evaluation tree (`golden/`, `heldout/`, `traps/`) |
| `SIWAH_HELDOUT_DIR` | Separately distributed heldout suite directory |
| `SIWAH_TEST_DOCKER` | Set to `1` to run Docker sandbox tests |

Internal daemon variables `SIWAH_RUNTIME_TOKEN` and `SIWAH_SUPERVISOR_NONCE` are
set by the supervisor or by `siwah serve`. Do not set them by hand except when
debugging a specific supervisor flow.

The bootstrap accepts `SIWAH_SOURCE`, `SIWAH_REPO`, `SIWAH_REPO_URL`,
`SIWAH_BRANCH`, `SIWAH_SKIP_TUI`, and `SIWAH_SKIP_SHELL` for non-default
installs.

## Minimal custom configuration

```toml
[providers.local]
api = "openai_chat"
base_url = "http://127.0.0.1:11434/v1"
models_url = "http://127.0.0.1:11434/v1/models"
auth = { scheme = "none" }

[roles]
main = "local/<model-id>"

[permissions]
mode = "ask"

[sandbox]
enabled = true
backend = "docker"
image = "python:3.12-slim"
network = false
```

Use a model ID that the local endpoint actually reports. siwah does not validate
or hardcode third-party model names.
