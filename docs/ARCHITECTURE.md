# Architecture

siwah is split so that an agent can improve its harness and, under a strict gate,
its core code, without being able to edit the mechanism that evaluates or rolls
it back.

## Process model

```text
siwah (launcher)
  │
  ├─ supervisor                         [protected]
  │    ├─ verifies the kernel manifest
  │    ├─ starts the daemon with a one-boot token and challenge nonce
  │    ├─ verifies the HMAC proof on /v1/health
  │    └─ rolls back protected paths after two failed boots
  │
  └─ siwah daemon (FastAPI, loopback HTTP + SSE)
       ├─ session and run ownership
       ├─ tools, permissions, provider calls, accounting
       ├─ checkpoints, verifier, memory, skills, MCP, jobs
       └─ evolution actors
            ▲
            │ HTTP + SSE, local bearer token
            │
       ┌────┴──────────────┬───────────────┬──────────────┐
       │                   │               │              │
   siwah-tui         siwah run        ACP bridge     editor clients
  (OpenTUI/Solid)     (headless)       (stdio)
```

The launcher starts `siwah supervise` when no healthy daemon is running. The
supervisor starts `siwah serve`, waits for a healthy endpoint, and then launches
the TUI as a client. The TUI never calls a provider directly.

The daemon binds `127.0.0.1` on an ephemeral port by default. It writes the
actual host, port, token, and PID to `~/.siwah/runtime.json` (mode 600). If a
fixed port is busy, it falls back to a free port and retries a bind race up to
three times.

## Layers and trust boundaries

| Layer | Contents | Who may change it |
|---|---|---|
| **Kernel** | `src/siwah/kernel/`, `evals/`, `tests/kernel/`, `kernel.manifest.json`, and the CLI/bootstrap/config/auth/profile loaders needed to enforce them | Humans only; protected from agent writes |
| **Core** | The rest of `src/siwah/` | Evolution Level 2, through a worktree and the full gate |
| **Harness data** | `~/.siwah/harness/` (a git repository) | The agent and the user, through Level 0–1 evolution or direct editing |

The trust rule is simple: an agent that can edit its evaluator, its permissions,
or its rollback mechanism has no guarantee. The kernel therefore owns the path
guard, manifest, audit chain, snapshot/rollback, budget governor, gate,
supervisor, and evolution orchestration. Candidate code runs in a separate
interpreter, and promotion requires a one-use kernel approval bound to the
checked candidate and HEAD.

These are application-level controls, not an OS sandbox. A deliberately
malicious shell command can still escape the workspace unless the opt-in command
sandbox is enabled.

## Repository layout

```text
siwah/
├─ pyproject.toml
├─ src/siwah/
│  ├─ kernel/        protected guard, manifest, gate, audit, snapshot,
│  │                 budget, supervisor, canary, evolution
│  ├─ core/          agent loop, sessions, checkpoints, context, verifier,
│  │                 plan mode, subagents, trajectories
│  ├─ providers/     OpenAI-compatible adapter, streaming, accounting,
│  │                 profiles, repair, fixtures
│  ├─ tools/         read/edit/search/bash/todo/web, memory, skills, recall,
│  │                 repo map, tool output, task, jobs, MCP, syntax
│  ├─ repo/          file index, tree-sitter symbols, repo map, git, LSP
│  ├─ permissions/   path guard and permission rules
│  ├─ server/        FastAPI app, schemas, SSE bus, runtime
│  ├─ adapt/         compatibility exports into protected evolution code,
│  │                 best-of-N, observer, proposer, synthetic fixtures
│  ├─ eval/          protected fixture loader and compatibility exports
│  ├─ harness/       bootstrap, prompt composition, memory, skills
│  ├─ mcp/           stdio and HTTP JSON-RPC, generic OAuth 2.1
│  ├─ cli/           Typer commands and plain/JSON renderers
│  ├─ sandbox.py     Docker/bubblewrap command sandbox
│  ├─ acp.py         Agent Client Protocol stdio bridge
│  ├─ hooks.py       local hook runner
│  ├─ jobs.py        process-local background jobs
│  ├─ commands.py    custom slash-command loader
│  └─ plugins.py     declarative plugin bundle loader
├─ ui/tui/           OpenTUI + Solid interface and generated API types
├─ evals/            protected golden, heldout, traps, baseline
└─ tests/
```

## Agent loop

The loop is deliberately boring and observable:

1. Build a stable context prefix: system prompt, tool definitions, skill index,
   project memory, and `AGENTS.md`.
2. Append the user message and any queued or steering input.
3. Call the role's model with streaming.
4. Normalize deltas, persist messages and tool calls, and emit SSE events.
5. Execute tools through the permission layer.
6. Feed compact, structured results back into context.
7. Stop on a final answer, an explicit budget stop, cancellation, a stall, or a
   hard error.

The stable prefix is deterministic and append-only during a session, which lets
provider-side prompt caching work. Large tool output is offloaded to a file and
replaced by a handle. Context pruning and structured compaction keep long
sessions within the model window.

One session has at most one active run. Additional prompts are queued in memory
(up to 16 per session) and start when the active run ends. Cancellation is
cooperative: the daemon stops scheduling steps, cancels the provider stream,
emits `run.end`, and leaves the session resumable.

## Provider layer

Providers are configuration, not code. In 1.1.0 the implemented adapter family is
`openai_chat`; other families are rejected at startup. The adapter normalizes
content, reasoning, tool calls, usage, errors, and finish events into one
internal schema, so the loop does not branch on the provider.

Design rules:

- **Config, not constants.** Model IDs, prices, limits, and endpoints come from
  config or the models endpoint. Source code never embeds a model catalog.
- **Allowance accounting.** For OpenCode Go, siwah accounts with
  `usage.opencode.allowance`, not `usage.cost` (which is zero inside the plan).
  `cost_field` makes the field configurable per provider.
- **Profiles are measured.** `siwah models probe` writes the capability profile.
  A profile is evidence from the endpoint and a probe, not a guess.
- **Retry and repair.** Backoff honors `Retry-After`, fallback roles handle
  retryable failures, and the adapter repairs truncated JSON, partial tool
  arguments, and text-embedded tool calls where possible.
- **Secrets stay local.** Keys are resolved from the environment or
  `~/.siwah/auth.json` and are never written to logs or trajectories.

## Storage

| Path | Contents |
|---|---|
| `~/.siwah/state.db` | SQLite sessions, messages, tool calls, traces, usage |
| `~/.siwah/trajectories/` | JSONL trajectory sidecars |
| `~/.siwah/checkpoints/<session-id>/` | Shadow git snapshots per session |
| `~/.siwah/tool_outputs/<session>/` | Offloaded tool output |
| `~/.siwah/harness/` | Git-backed editable harness |
| `~/.siwah/eval/` | Reports and baseline cache |
| `~/.siwah/audit.jsonl` | Hash-chained audit events |
| `~/.siwah/budget-state.json` | Evolution cycle and allowance counters |
| `~/.siwah/runtime.json` | Current daemon endpoint and token |
| `~/.siwah/logs/daemon.log` | Daemon and supervisor logs |

Sessions are global, but the TUI lists the current project by default. The
project is inferred from the session working directory.

## Server and protocol

The daemon exposes a versioned local HTTP + SSE API. The full contract is in
[PROTOCOL.md](PROTOCOL.md). Key properties:

- local bearer token created per daemon start;
- `GET /v1/meta` handshake with protocol version and capabilities;
- one active run per session, cooperative abort;
- SSE events with monotonic per-session sequence numbers and replay;
- Pydantic v2 schemas as the source of truth;
- generated TypeScript types committed under `ui/tui/src/api/schema.d.ts`;
- CI regenerates the OpenAPI document and types and fails on drift.

The protocol version is independent of the application version. At 1.1.0 the
protocol is `0.1`; changes within that minor are additive. Omission of the
`X-Siwah-Protocol` header is supported for local clients; an incompatible
supplied header returns `409`.

## Permissions and the path guard

A tool request is checked in this order:

1. The path guard resolves and confines any path. Reads and writes must stay
   inside the workspace or `~/.siwah/harness/`; writes may also use explicitly
   granted evolution worktrees. Protected and secret-like paths are denied.
2. Catastrophic shell commands are denied, even in `yolo`.
3. Configured permission rules are evaluated from last to first. A matching
   rule returns `allow`, `ask`, or `deny`.
4. Session approvals are applied.
5. The mode default applies: reads are allowed; writes are allowed in
   `auto-edit` and `yolo`; shell and network are allowed only in `yolo`.

The guard is an application-level control inside the tool layer. It is not a
kernel sandbox and does not constrain a shell command that intentionally writes
elsewhere. See [Security](SECURITY.md).

## Kernel, supervisor, and rollback

The kernel owns:

- the protected path inventory and path guard;
- the SHA-256 kernel manifest (CRLF/LF stable);
- the hash-chained audit log;
- snapshot and rollback primitives;
- the allowance budget governor;
- the evolution gate and promotion authority;
- the supervisor.

A healthy boot creates an annotated `kernel-healthy-<hash12>` tag signed with an
HMAC key at `~/.siwah/supervisor.key`. Unsigned or forged tags are ignored.
Rollback checks out only the protected paths and leaves the rest of the
workspace untouched.

The supervisor passes a one-boot token and challenge nonce to the daemon and
verifies an HMAC proof on `/v1/health`. `runtime.json` never contains the nonce.
After two consecutive failed boots, the supervisor rolls back protected paths
and stops the failed daemon so a stale process cannot hold the port.

`siwah kernel manifest --write` is a human-only operation. It is not available to
the agent and should only run after a deliberate, reviewed change to protected
code.

## Evolution

The evolution loop is Propose → Probe → Compose → Promote, with a separate
low-priority process and a hard allowance governor.

- **Observe** recent sessions after redaction.
- **Reflect** with a model whose profile says it does not train on the data.
- **Propose** a typed candidate: level, kind, scope, target paths, diff,
  rationale, expected metric, and risk.
- **Probe** in tiers: T0 structure, T1 small screening subset, T2 relevant tasks,
  T3 full golden plus heldout for Level 2 or final promotion.
- **Compose** non-conflicting passing candidates.
- **Promote** with a git commit and a tag. Level 0–1 activates on the next
  session; Level 2 restarts through the supervisor.
- **Canary** the first configured sessions and record error, correction, and
  allowance signals.

Level 2 candidates are reviewed in an isolated git worktree. The gate rejects
protected paths, UI changes, kernel tests, removed test lines, credentials, and
guard relaxation; limits a candidate to five files and 200 changed lines; and
runs tests, lint, type checks, a CLI smoke boot, and T0–T3 evaluation.

The canary monitor persists its state, but production session sampling is not
connected end-to-end. Do not treat automatic post-session rollback as a
guarantee. See [Evolution](EVOLUTION.md).

## TUI

The TUI is a TypeScript client built with OpenTUI and Solid. It can run as a
compiled binary or from Bun source with `--conditions=browser`. It consumes only
the generated API client and does not hand-write HTTP calls.

The launcher searches for the binary in this order:

1. `SIWAH_TUI_BINARY`;
2. `$SIWAH_HOME/bin/siwah-tui[.exe]`;
3. `~/.siwah/bin/siwah-tui[.exe]`;
4. the directory containing the `siwah` executable;
5. `PATH`;
6. the repository's `ui/tui/dist/`.

If no binary and no Bun source are available, `siwah` exits with a clear message.
`siwah run ... --ui plain` does not need the TUI.

## Design principles

- **Local-first.** No hosted gateway, no account service, no telemetry.
- **Config over constants.** Provider facts are data, not source code.
- **Data, not instructions.** File contents, web pages, MCP descriptions, and
  tool results are untrusted data.
- **Append-only events.** SSE deltas are not rewritten; clients tolerate
  duplicate IDs and reconnects.
- **Safe defaults.** `ask` permissions, loopback binding, no network in the
  sandbox, dirty-repository refusal for promotion and updates.
- **Honest limits.** Application-level guards are documented as such.

## Known architectural gaps

- Only the `openai_chat` provider family is implemented.
- The daemon speaks loopback TCP; unix sockets are not implemented.
- Evolution canary sampling and automatic rollback are not end-to-end.
- Level 2 recovery from a delayed daemon boot failure is not fully journaled.
- ACP lacks interactive permission dialogs, `loadSession`, and filesystem or
  terminal methods.
- Workspace reservations are in-process; there is no cross-process evolution
  idle lease.
- State is not encrypted at rest. Use full-disk encryption and appropriate file
  permissions if that matters for your threat model.
