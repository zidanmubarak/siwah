# Troubleshooting

Start with:

```bash
siwah --version
siwah doctor
```

`doctor` reports the config and harness paths, kernel status, credentials,
artifacts, MCP servers, Bun, and the TUI binary. It exits 1 when it finds a
problem.

## Installation and PATH

### `siwah: command not found`

`uv tool install` puts the shim in the uv tool bin directory. Add it to `PATH`:

```bash
uv tool update-shell
uv tool dir --bin
```

Open a new terminal. On Windows the directory is usually
`%USERPROFILE%\.local\bin`; on Unix it is usually `~/.local/bin`. If you use a
different shell, add that directory to the shell's startup file manually.

### `uv: command not found`

Install uv from <https://docs.astral.sh/uv/>, then reopen the terminal. The
bootstrap tries to install it, but an existing shell will not see the new
`PATH` until it is restarted.

### `bun: command not found`

The TUI from source needs Bun 1.3.14+. Install it from <https://bun.sh/> and open
a new terminal. A compiled `siwah-tui` binary does not need Bun. If neither is
available, use:

```bash
siwah run "your task" --ui plain
```

### The wrong Python or siwah version is used

Check which executable is first on `PATH`:

```bash
which -a siwah        # macOS/Linux
where siwah           # Windows PowerShell
siwah --version
```

Remove duplicate installs or put the intended tool directory first. A source
checkout run through `uv run siwah` can differ from an installed `siwah` shim.

## Providers

### HTTP 401 or “credential missing”

- Run `siwah auth status` to see whether the environment variable or auth file
  is set.
- Remember that the environment variable takes precedence over the auth file. An
  empty or stale exported key will shadow a good stored key.
- Re-run `/connect` or `siwah auth login <provider>`.
- Check `base_url` and the auth scheme. A `none` scheme sends no authorization
  header, which is only correct for a local endpoint.
- A key may be valid for one endpoint and invalid for another. Confirm the base
  URL from the provider's documentation.

### HTTP 429 or allowance exhausted

A 429 usually means a rate limit or a plan window, not a broken config.

- siwah retries with backoff and honors `Retry-After` where the provider sends
  it.
- Configure `[roles].fallback` if you have another model or provider.
- Lower `--effort` or the step budget for cheaper runs.
- Wait for the provider's window to reset. OpenCode Go plans have shared limits
  across clients; another tool may be consuming the same allowance.
- Do not retry a paid request in a loop. Check `siwah trace <session-id>` and the
  provider console.

### Model not found or 400 “model unavailable”

The model ID must be one the endpoint actually reports. Run:

```bash
siwah models list --provider <provider>
siwah models probe <provider>/<model-id>
```

Do not assume a model from a public catalog is callable from your plan or
endpoint.

### The model list is empty or slow

`siwah models list` reads the provider's network endpoint. Check the credential,
`models_url`, and connectivity. A provider failure is reported per provider; one
bad provider should not hide the others.

## Windows and shells

### `bash` behaves differently from Git Bash

The `bash` tool prefers Git Bash on Windows so POSIX syntax is consistent.
`cmd.exe` is only a fallback. Install Git for Windows and make sure `bash.exe`
is on `PATH`, or set up the tool environment so Git Bash is discoverable.

If a command uses PowerShell syntax, run it through `powershell -NoProfile -Command
"..."` explicitly; do not assume the `bash` tool will interpret it.

### Paths with spaces or backslashes fail

Quote paths in shell commands. Prefer forward slashes in tool arguments where
possible. A path that starts with `C:\` must be quoted in a POSIX shell. The path
guard resolves relative paths against the session workspace.

### The TUI starts in the wrong directory

The launcher pins the workspace to the directory where `siwah` was invoked. Run
it from the project root, or set `SIWAH_WORKSPACE` when launching a client
directly.

## Daemon and port

### `Starting siwah daemon…` appears on launch

On a cold launch, `siwah` starts the local daemon through the supervisor and
waits for its health check before opening the TUI. The launcher reuses a healthy
daemon if one is already running. A daemon started by the TUI launcher stops
when that launcher exits. To keep a daemon running for multiple TUI launches,
run `siwah serve` in a separate terminal.

### “Could not start the daemon” or the port is busy

The daemon prefers an ephemeral port (`[server].port = 0`). If a fixed port is
busy, it falls back to a free port, writes the actual endpoint to
`~/.siwah/runtime.json`, and retries a bind race up to three times.

Check:

```bash
cat ~/.siwah/runtime.json
tail -n 100 ~/.siwah/logs/daemon.log
```

If a stale process is holding a port, stop siwah and, only if the recorded PID is
no longer running, remove the stale `runtime.json`. Do not kill a process you do
not recognize.

### The TUI cannot connect to an existing daemon

Confirm that `runtime.json` has the host, port, and token of the daemon you
expect. For a manually started daemon, pass the endpoint explicitly:

```bash
siwah serve
SIWAH_SERVER=http://127.0.0.1:<port> SIWAH_TOKEN=<token> siwah
```

Do not paste the token into an issue or a log.

### A run is stuck in “waiting permission”

A permission request waits for a client decision. In the TUI, answer the panel.
A pending daemon approval expires as denial after 300 seconds. In a headless
run, use `--permission auto-edit` or `--permission yolo` deliberately; JSON mode
cannot prompt.

## TUI

### “The TUI needs a compiled siwah-tui binary”

The launcher searches, in order:

1. `SIWAH_TUI_BINARY`;
2. `$SIWAH_HOME/bin/siwah-tui[.exe]`;
3. `~/.siwah/bin/siwah-tui[.exe]`;
4. the directory containing the `siwah` executable;
5. `PATH`;
6. `ui/tui/dist/` in a source checkout.

Fix it by building and installing the binary:

```bash
cd ui/tui
bun install --frozen-lockfile
bun run build
mkdir -p ~/.siwah/bin
cp dist/siwah-tui ~/.siwah/bin/siwah-tui
```

Use `siwah-tui.exe` on Windows. A wheel does not include the binary. As a
fallback, use `siwah run "..." --ui plain`.

The source installer and updater place the managed TUI binary in
`~/.siwah/bin/`. A second `siwah-tui[.exe]` under `~/.siwah/src/ui/tui/dist/`
is a development or older installer build. The managed copy takes precedence
unless you set `SIWAH_TUI_BINARY`. The separate `siwah` command is the Python
entry point installed by `uv tool install`.

### The screen is garbled or colors are wrong

- Set `NO_COLOR=1` to select the monochrome theme.
- Check `TERM` and `COLORTERM`.
- Use at least 60 columns and 20 rows.
- Prefer Windows Terminal on Windows.
- On SSH, `xterm-256color` is the best-tested configuration.
- If resizing leaves artifacts, resize once more or restart the TUI; include the
  terminal and size in a report.

macOS Terminal and iTerm2 are not verified for 1.0.4. See
[Terminal support](TERMINAL-SUPPORT.md).

### The TUI exits immediately

Run `siwah` from a real terminal; it requires an interactive TTY. In CI, use
`siwah run ... --ui plain`. Check `~/.siwah/logs/tui-warnings.log` for runtime
warnings.

## Updates

### “Update refused: checkout has local changes”

`/update` and `siwah update --apply` refuse a dirty checkout so your work is not
overwritten. Commit or stash your changes, then retry:

```bash
git status
git stash push -m "before update"
siwah update --apply
```

If you do not want to manage a checkout, reinstall from a bundle or a new source
bootstrap. A wheel-only install can see an update notice but cannot apply it.

### The version did not change after an update

The new TUI binary is staged as `siwah-tui[.exe].next` and activated on the next
launch, because Windows cannot replace a running executable. Exit all siwah
processes and start `siwah` again. Check `siwah --version` and
`siwah update --refresh`.

## Evaluation and evolution

### “Eval directory not found”

Evaluation needs a source checkout or an explicit directory:

```bash
siwah eval --suite golden --eval-dir /path/to/siwah/evals
SIWAH_EVAL_DIR=/path/to/siwah/evals siwah eval --suite golden
```

A wheel install does not contain `evals/`.

### The heldout suite is missing

The public repository ships three heldout samples. The full suite is distributed
separately:

```bash
SIWAH_HELDOUT_DIR=/path/to/heldout siwah eval --suite heldout
```

### Live evaluation refuses to start

- Export `SIWAH_LIVE=1` in the same process.
- Pass `--live`.
- Pass `--model`, or configure `[roles].main`; `--live` never uses the mock
  harness.
- Pass a positive `--max-allowance`.
- Configure a credential.

### Evolution refuses to run

- `--seed` is required for a synthetic cycle.
- `--live` requires `SIWAH_LIVE=1` and a positive `--max-allowance`.
- Check the pause state: `siwah evolve --resume`.
- `SIWAH_EVOLVE=off` disables evolution.
- A dirty repository blocks promotion and rollback.
- `siwah kernel verify` must pass before a cycle starts.

### `--live` still says `SIWAH_LIVE=1`

PowerShell does not use leading `NAME=value` assignments:

```powershell
$env:SIWAH_LIVE = "1"
siwah evolve --live --model opencode-go/<model-id> --max-allowance 0.05
```

bash and zsh:

```bash
export SIWAH_LIVE=1
siwah evolve --live --model opencode-go/<model-id> --max-allowance 0.05
```

The variable must be set in the same shell/process, and the model must also pass
the privacy check below.

### Evolution is privacy-blocked

The reflector refuses a model that is not explicitly marked as not training:

```bash
siwah models probe opencode-go/<model-id>
siwah models privacy opencode-go/<model-id> --training false
```

Only use `training=false` when the provider contract confirms it. `models probe`
shows `training=unmarked` when the provider catalog does not expose privacy
metadata. A privacy preflight does not consume a cycle or start a cooldown.

### Evolution is in cooldown

A cooldown normally follows a cycle that performed real work. After a failed
privacy or configuration preflight, older releases may have left a cooldown
behind:

```bash
siwah evolve --reset-cooldown
```

This clears only the cooldown timestamp. It does not reset the daily cycle count
or recorded allowance. Do not use it to bypass deliberate paid-evaluation
limits.

### Evolution candidate is invalid

The proposer normalizes common model drift such as `scope: "agent"` or a single
string in `evidence`. If the response is missing JSON, truncated, or still fails
schema validation, it asks for one repair with the exact schema. Proposal calls
use the model profile's `max_output` by default, so full-file changes are not
cut off by an artificial 16k cap. If the provider rejects a very large
`max_tokens`, set `SIWAH_EVOLVE_MAX_TOKENS` to `none` or a lower positive value.
If the cycle still fails, inspect the audit log:

```bash
siwah evolve log
```

No harness change is applied when validation fails. Report the candidate shape
if the error persists; do not edit evaluation tasks to make it pass.

### Why are there two `siwah-tui` binaries?

The bootstrap builds `~/.siwah/src/ui/tui/dist/siwah-tui[.exe]` and copies it to
`~/.siwah/bin/siwah-tui[.exe]`. The launcher prefers `~/.siwah/bin/`; the source
build output is a fallback and is rebuilt by `/update`. Both files are expected.
You normally do not need to manage them. A PyPI install has no source checkout,
so only the binary you place in `~/.siwah/bin/` exists.

### Kernel manifest mismatch

Do not regenerate the manifest to make the error disappear. A mismatch means a
protected file changed. Inspect the diff, restore the protected path, or use
`siwah rollback` if the change is not intentional. Regeneration is a
maintainer-only action after a reviewed change.

## MCP, sandbox, and jobs

### An MCP server does not connect

```bash
siwah mcp status
siwah mcp test <name>
siwah mcp show <name>
```

Check the command, URL, environment, and working directory. For stdio servers,
the executable must be on the daemon's `PATH`, which may differ from your shell's
`PATH`. For HTTP servers, check headers and the endpoint. A timeout is not
necessarily a configuration error.

### MCP says OAuth authentication required

The server is configured with `"auth": "oauth"`, but no valid token exists:

```bash
siwah mcp list                 # oauth=missing/expired/refreshable
siwah mcp auth <name>          # browser + loopback callback
siwah mcp auth <name> --device # headless/SSH
siwah mcp auth <name> --no-browser
```

Tokens are stored in `~/.siwah/mcp-auth/<name>.json` with mode 600. Use
`siwah mcp logout <name>` to remove them. If the server does not support dynamic
client registration, set `client_id` on the server config (and
`client_secret_env` if a secret is required).

### OAuth browser callback fails

The callback listener binds `127.0.0.1` on an ephemeral port. A container, WSL
network boundary, firewall, or browser running on another machine can prevent
the callback. Use `--device` for headless/SSH, or `--no-browser` and open the
printed URL on a machine that can reach the listener. Do not paste tokens into
chat or commit `~/.siwah/mcp-auth/`.

Some authorization servers advertise a device endpoint but do not grant
`urn:ietf:params:oauth:grant-type:device_code` to a dynamically registered
client. In that case `--device` reports `invalid_grant`; use the browser flow or
pre-register a client with device-flow support.

### Plugin content does not appear

```bash
siwah plugin list
siwah plugin info <name>
siwah plugin enable <name>
```

Loaders read plugins at session start. Start a new session or restart the daemon
after installing/enabling a plugin. Project files win over plugins, and a user
`mcp.json` server wins over a plugin server with the same name.

### Plugin install is rejected

Only directories and `.zip` files are accepted. The bundle needs `plugin.toml`
at its root with a lowercase name, at most 5,000 files, and at most 20 MB.
Symlinks and zip entries that escape the plugin directory are rejected. Hooks in
a plugin still execute commands; only install trusted bundles.

### The sandbox fails closed

With `[sandbox].enabled = true`, a missing Docker or bubblewrap backend is an
error, not a reason to run unsandboxed. Background commands with a configured
sandbox fail closed. Install the backend, choose `local` only if you accept that
it is not isolation, or disable the sandbox for that run.

The Docker image must contain the project's toolchain. A minimal `alpine` image
will not have Python, Node, or your test dependencies.

### Background jobs disappear

Jobs are process-local and do not survive a daemon restart. Start the job again,
or use a terminal multiplexer for work that must outlive siwah.

## Local data

### Where are the logs?

- `~/.siwah/logs/daemon.log` — daemon and supervisor.
- `~/.siwah/logs/tui-warnings.log` — TUI runtime warnings.
- `~/.siwah/audit.jsonl` — hash-chained audit events.

### Disk usage is growing

Check orphaned session artifacts:

```bash
siwah doctor
siwah clean --sessions --dry-run
siwah clean --sessions --yes
siwah clean --logs --dry-run
```

`siwah clean` removes artifacts for sessions that no longer exist in
`state.db`. It does not delete your project files.

### I need a clean environment for a test

Set a separate home:

```bash
SIWAH_HOME=/tmp/siwah-test siwah
```

This creates an isolated config, harness, state database, and auth file. Do not
delete your real `~/.siwah/` to troubleshoot; use an isolated home instead.

## Getting help

If the problem persists, include:

- siwah version and install method;
- OS, terminal, and shell;
- the exact command and exit code;
- relevant lines from `~/.siwah/logs/daemon.log`;
- whether `--ui plain` reproduces it;
- a minimal reproduction with secrets removed.

Open a bug report with the issue template. For security issues, use
[SECURITY.md](../SECURITY.md), not a public issue.
