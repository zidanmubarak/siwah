# Security

siwah is a local coding agent with access to your files, shell, and provider
credentials. This document describes the threat model, the controls that exist,
and the controls that do not.

For vulnerability disclosure, see [SECURITY.md](../SECURITY.md). Do not put
secrets in a public issue.

## Supported versions

Security fixes target the latest 1.0.x release. Pre-1.0 builds are not supported.

## Threat model

### Assets

- Your source code and workspace files.
- Provider API keys and auth tokens.
- Session history, prompts, tool output, and memory.
- The integrity of the agent's evaluator, permissions, and rollback mechanism.
- The integrity of the machine outside the workspace.

### Adversaries and failure modes

| Threat | Example |
|---|---|
| **Malicious repository content** | A README or source comment contains instructions to exfiltrate a key |
| **Prompt injection** | A fetched page or MCP tool result tells the agent to ignore permissions |
| **Model mistakes** | The model proposes a destructive command or edits the wrong path |
| **Malicious MCP server** | A server returns misleading tool descriptions or hostile results |
| **Compromised provider** | A provider endpoint returns crafted tool calls or usage data |
| **Malicious evolution candidate** | A candidate weakens the verifier or edits the gate |
| **Local attacker** | Another process on the same account reads local state or the daemon token |
| **Accidental exposure** | The daemon is bound to a public interface, or a secret is pasted into an issue |

siwah is not designed to resist a local attacker with the same OS account and
full filesystem access. It is designed to keep an ordinary coding agent inside
the workspace, make risky actions visible, and prevent an evolved candidate from
silently disabling its own checks.

## Trust boundaries

```text
untrusted data                  trusted code                 operator
──────────────                  ────────────                 ────────
files, web pages,      →     agent loop and tools      →   approval
MCP results, model     →     path guard, permissions   →   config
output, task prompts   →     kernel, gate, supervisor  →   audit review
```

Model output is not trusted to enforce policy. The path guard, permission
decisions, gate, and rollback live in code outside the model's control.
Repository content, web content, MCP descriptions, and tool results are treated
as data, not instructions. The prompt includes injection warnings, but no
warning system is perfect.

## Controls

### Permissions

- Default mode is `ask`.
- `auto-edit` allows file edits inside the allowed roots but still asks for
  shell and network actions.
- `yolo` allows shell and network actions, but catastrophic commands remain
  denied.
- Rules can allow, ask, or deny per tool, path, or command. The last matching
  rule wins.
- A denied call is returned to the model as a tool error with the reason.

### Path guard

The path guard runs before a permission rule:

- paths must resolve inside the workspace or `~/.siwah/harness/`;
- writes may also use explicitly granted evolution worktrees;
- protected source and evaluation paths are denied;
- secret-like paths are denied;
- symlink and relative-path resolution is performed before the check.

Default deny paths include `.env` files, SSH keys, AWS credentials, GCloud
configuration, and `credentials.json`.

The guard is an **application-level** control in the tool layer. It does not
confine a shell command that intentionally writes elsewhere. That is why the
command sandbox exists.

### Catastrophic-command denial

Commands that target a filesystem root, format a disk, or shut down the machine
are denied even in `yolo`. This is a safety rail for common mistakes, not a
complete command policy. It does not understand every shell wrapper, alias, or
script.

### Secrets

- Provider keys are read from the environment or `~/.siwah/auth.json` (mode 600).
- `siwah auth status` reports presence, never values.
- MCP header and environment values are redacted by `siwah mcp show`.
- Logs, traces, and error details are designed not to include keys or full
  headers.
- Memory and skill writes are size-limited and reject credential-like content.
- Recall snippets are redacted. `recall summarize=true` redacts matched
  evidence, titles, and the query before sending them to the configured
  `[roles].fast` model.
- Cron jobs are read-only unless created with `--allow bash`, `--allow write`,
  or `--allow network`. Explicit deny rules still apply, each job has a
  workspace, timeout, per-run allowance, and the scheduler enforces a daily
  allowance cap.
- The agent is instructed not to write secrets into the repository.

These are best-effort pattern and entropy checks. A secret in an unusual format
can still slip through. Do not rely on siwah to classify every credential.

### Untrusted content

- File contents, web pages, MCP descriptions, and tool results are wrapped as
  data.
- Injection patterns produce warnings in the model context.
- MCP tools are called only after discovery and permission; treat their output
  as hostile input.
- The audit log records security-relevant actions for later review.

No prompt-injection defense is complete. A determined injection can still
influence a model within the permissions you granted.

### Sandbox

The command sandbox is opt-in via `[sandbox]`.

| Backend | Isolation | Notes |
|---|---|---|
| Docker | Container | Workspace mounted at `/workspace`; `sh -lc`; network off by default; timeout kills and removes the container. The image must contain the project toolchain. |
| bubblewrap | Linux namespace sandbox | Used on Linux when available; network off by default. |
| Local | **None** | Reduced environment and a timeout only. Explicitly labelled non-isolated. |

When a sandbox is configured, background commands fail closed instead of falling
back to an unsandboxed host job. Verifier commands also use the sandbox, but they
require their own shell approval, even in `auto-edit`.

Docker and bubblewrap use `sh -lc`, including on Windows hosts. The host `cmd` is
used only by the local fallback.

### Kernel and supervisor

- `src/siwah/kernel/`, `evals/`, `tests/kernel/`, `kernel.manifest.json`, and
  the protected bootstrap/config/auth/profile loaders are not agent-writable.
- The kernel manifest is verified at boot and before promotion.
- A healthy boot creates an HMAC-signed `kernel-healthy-<hash12>` tag. Unsigned
  or forged tags are ignored.
- The supervisor verifies a one-boot challenge proof on `/v1/health`. The nonce
  is never written to `runtime.json`.
- After two consecutive failed boots, the supervisor rolls back protected paths
  and stops the failed daemon so a stale process cannot hold the port.
- Audit events are hash-chained.

### Evolution

- Candidate execution is separated from grading.
- Level 2 patches are reviewed in an isolated worktree.
- The gate rejects protected paths, large diffs, removed test lines,
  credential-like content, and guard relaxation.
- Promotion requires a one-use approval bound to the checked candidate and HEAD.
- A dirty repository is refused rather than overwritten.
- Budgets and kill switches limit unattended spend.
- Live reflection requires a model profile that explicitly says
  `training=false`. An unmarked model is refused. The operator records this
  assertion with `siwah models privacy`; it is a policy decision, not a
  cryptographic guarantee from the provider.

The gate is heuristic. It is not a formal proof that a candidate is safe.

### MCP OAuth

- OAuth discovery accepts HTTPS endpoints; plain HTTP is limited to loopback
  development servers.
- The browser flow uses authorization code + PKCE (S256), a random state value,
  and a loopback listener bound to `127.0.0.1` on an ephemeral port.
- Device flow is available for headless/SSH sessions.
- Access and refresh tokens are stored in `~/.siwah/mcp-auth/<server>.json` with
  mode 600. They are never written to `mcp.json`, status output, or logs.
- Tokens are refreshed before expiry; a failed refresh marks the server as
  requiring login instead of silently using an expired token.
- Dynamic client registration is used when offered. A client secret, when
  needed, is read from an environment variable named by `client_secret_env`.

### Plugins

- Plugins are declarative bundles; siwah does not import or execute Python code
  from them.
- Hooks still execute their configured commands, so installing a plugin is a
  trust decision. Install only from sources you trust.
- Plugin MCP servers use the same permission rules as user-configured servers.
- Zip traversal, symlinks, oversized bundles, and invalid names are rejected.
- Installing or removing a plugin does not modify protected source or evaluation
  paths.

### Updates

- The checkout updater uses the normal git credential helper. It never embeds a
  GitHub token.
- A dirty checkout is refused.
- A uv tool install checks the PyPI JSON API and applies updates with
  `uv tool upgrade siwah`; it does not download or execute arbitrary release
  assets outside the verified TUI download flow.
- `siwah uninstall --purge` deletes credentials under `~/.siwah` permanently.
  Use `--keep-data` or the interactive confirmation to avoid that.

## What siwah does not protect against

- A malicious `bash` command that writes outside the workspace when no sandbox
  is enabled.
- A local attacker with the same OS account who can read `~/.siwah/`.
- A compromised provider that sees your prompts and returns hostile output.
- A malicious dependency, editor, terminal, or MCP server outside siwah's
  control.
- Data remanence: deleted files may remain on disk.
- Encryption at rest: siwah does not encrypt `state.db`, trajectories, or
  checkpoints.
- Denial of service: a huge repository, a runaway command, or a provider outage
  can still consume time and allowance.
- Prompt injection in arbitrary content. Warnings reduce risk; they do not
  eliminate it.
- A user who grants `yolo` to an untrusted task.

## Hardening checklist

- Keep the daemon on `127.0.0.1`. Do not expose it to a network.
- Use the `ask` permission mode for unfamiliar repositories.
- Enable Docker or bubblewrap for untrusted code, and use a project-specific
  image with the toolchain you need.
- Keep `auto-edit` and `yolo` for work you are watching.
- Review permission previews, especially shell commands and URLs.
- Do not paste keys into prompts, issues, or logs.
- Use a dedicated OS account or container for high-risk experiments.
- Keep `~/.siwah/` permissions tight; credentials already use mode 600.
- Run `siwah doctor` and `siwah kernel verify` after a suspicious change.
- Review evolution candidates and audit events before enabling unattended
  promotion.
- Pin dependencies and review lockfile changes in pull requests.
- Use full-disk encryption if local state confidentiality matters.

## Reporting a vulnerability

Follow [SECURITY.md](../SECURITY.md). Report privately, include the version and
a minimal reproduction, and do not include real API keys or personal data.
