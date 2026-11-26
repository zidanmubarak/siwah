# Daemon protocol

Status: **stable for siwah 1.0.5**. Protocol version: `0.1`.

The protocol version is independent of the application version. Within protocol
`0.1`, changes are additive: new optional fields and endpoints may appear, but
existing fields and event names are not repurposed. A client must refuse a
protocol version it does not understand.

This document is the contract for the siwah daemon and its clients. Anything not
described here is an implementation detail.

## 1. Transport

- Local only: loopback TCP on an ephemeral port, or a fixed port from `[server]`.
  Unix sockets are not implemented.
- A bearer token is created when the daemon starts. Every request must send
  `Authorization: Bearer <token>`.
- The token, host, port, and PID are written to `~/.siwah/runtime.json` with mode
  600. The supervisor's one-boot nonce is never written there.
- No CORS. No remote binding by default. Binding to a non-loopback interface is
  possible with `--host`, but there is no TLS and no user model; do not expose
  the daemon to a network you do not trust.
- Request bodies are JSON (`application/json`). Event streams are
  `text/event-stream`.
- The API version is carried in the handshake and in the path (`/v1/...`).
- Tokens, prompts, tool output, and provider keys are redacted from logs.

## 2. Handshake

`GET /v1/meta` returns:

```json
{
  "name": "siwah",
  "version": "1.0.5",
  "protocol": "0.1",
  "daemon_started_at": "2026-11-19T00:00:00Z",
  "default_effort": null,
  "capabilities": {
    "sse_resume": true,
    "permission_modes": ["ask", "auto-edit", "yolo"],
    "features": [
      "sessions", "permissions", "openapi", "files", "fork", "shell",
      "auth", "providers", "skills", "memory"
    ]
  }
}
```

The TUI sends `X-Siwah-Protocol: 0.1` on HTTP and SSE requests. If a supplied
header is incompatible, the daemon returns `409` with `code=protocol_mismatch`.
Header omission remains supported for local clients.

`/v1/health` returns the daemon health state. When the daemon was started by the
supervisor and the request carries `X-Siwah-Challenge`, the response includes
`X-Siwah-Proof`, an HMAC of the challenge with the one-boot nonce.

## 3. Endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/v1/meta` | Handshake and capabilities |
| `GET` | `/v1/health` | Health and optional supervisor proof |
| `GET` | `/v1/models` | Model catalog and capability profiles |
| `GET` | `/v1/events/schema` | Event envelope schema |
| `GET` | `/v1/openapi.json` | OpenAPI 3.1 document (inspected by tooling and tests) |
| `POST` | `/v1/sessions` | Create a session (`cwd`, `model`, `mode`, `title`) |
| `GET` | `/v1/sessions` | List sessions |
| `GET` | `/v1/sessions/{session_id}` | Session metadata and messages |
| `PATCH` | `/v1/sessions/{session_id}` | Update model, mode, title, or effort |
| `GET` | `/v1/sessions/{session_id}/messages` | Message history |
| `POST` | `/v1/sessions/{session_id}/messages` | Admit a user message and start a run |
| `GET` | `/v1/sessions/{session_id}/usage` | Token, allowance, and context summary |
| `POST` | `/v1/sessions/{session_id}/abort` | Cancel the active run |
| `GET` | `/v1/sessions/{session_id}/events` | SSE stream (`after` / `Last-Event-ID`) |
| `POST` | `/v1/sessions/{session_id}/fork` | Copy a session, optionally restore a checkpoint |
| `POST` | `/v1/sessions/{session_id}/shell` | Direct shell command; `409` when approval is needed |
| `POST` | `/v1/sessions/{session_id}/permissions/{request_id}` | Approve or deny a pending request |
| `GET` | `/v1/sessions/{session_id}/checkpoints` | List checkpoints |
| `GET` | `/v1/sessions/{id}/changes` | List changes or load one patch with `file_path` |
| `GET` | `/v1/sessions/{id}/timing` | Latest daemon task elapsed times in milliseconds |
| `DELETE` | `/v1/sessions/{id}/corrections/{message_id}` | Dismiss a recovered unapplied correction |
| `POST` | `/v1/sessions/{session_id}/checkpoints` | Create a manual checkpoint |
| `POST` | `/v1/sessions/{session_id}/rewind` | Restore a checkpoint (`confirm=true` required) |
| `POST` | `/v1/sessions/{session_id}/compact` | Persist a structured context summary |
| `GET` | `/v1/sessions/{session_id}/plan` | Read `<repo>/.siwah/plan.md` |
| `GET` | `/v1/sessions/{session_id}/tool_calls/{tool_call_id}` | Reopen a tool result |
| `GET` | `/v1/files` | Workspace files for `@` completion (`session_id` or `cwd`) |
| `GET` | `/v1/commands` | Custom slash commands and bodies |
| `GET` | `/v1/harness/skills` | Skill names and descriptions |
| `GET` | `/v1/harness/skills/{name}` | Full skill body |
| `GET` | `/v1/harness/memory` | User, project, and quick memory |
| `POST` | `/v1/harness/init` | Create project memory and an `AGENTS.md` scan |
| `POST` | `/v1/harness/note` | Append a quick note |
| `GET` | `/v1/permissions/mode` | Current daemon permission mode |
| `POST` | `/v1/permissions/mode` | Set `ask`, `auto-edit`, or `yolo` |
| `POST` | `/v1/agent/effort` | Persist the default reasoning effort |
| `GET` | `/v1/auth/status` | Credential presence, never values |
| `POST` | `/v1/auth/login` | Store a provider key |
| `POST` | `/v1/auth/browser/{provider}` | Start ChatGPT or OpenRouter browser sign-in |
| `POST` | `/v1/auth/browser/{provider}/complete` | Finish browser sign-in |
| `GET` | `/v1/auth/chatgpt/accounts` | List saved ChatGPT accounts, never tokens |
| `POST` | `/v1/auth/chatgpt/sign-out` | Sign out a saved ChatGPT account |
| `POST` | `/v1/providers/defaults` | Add missing built-in provider presets and reload |
| `GET` | `/v1/mcp` | MCP server status |
| `POST` | `/v1/mcp/{name}/restart` | Restart one MCP server |
| `GET` | `/v1/evolution` | Evolution timeline and status |
| `POST` | `/v1/evolution/pause` | Pause or resume evolution |
| `POST` | `/v1/evolution/rollback` | Roll back a tag or the previous harness revision |
| `GET` | `/v1/update` | Check the upstream branch; `refresh=true` bypasses the cache |
| `POST` | `/v1/update` | Fast-forward a source checkout and rebuild the TUI |

Notes:

- `PATCH /v1/sessions/{id}` accepts `model`, `mode` (`build` or `plan`), `title`,
  and `effort` (`minimal`, `low`, `medium`, `high`, `xhigh`, `max`, or `off`).
  Session effort overrides `[agent].effort` and is forwarded as
  `reasoning_effort` when the provider supports it.
- The permission mode is daemon-wide. `yolo` is the auto-bypass mode and still
  keeps the path guard and catastrophic-command denial. The TUI shows an `AUTO`
  badge while it is active.
- Session responses include `last_event_seq`. A client that hydrates messages
  from the database should start its SSE stream after that sequence; replaying
  older deltas on top of hydrated messages duplicates text and tool cards.
- `GET /v1/models` without `provider` enumerates every configured provider.
  A provider that fails includes an `error` field instead of failing the whole
  catalog. A provider with no credential is reported as missing without a
  network request.
- `POST /v1/providers/defaults` appends missing built-in provider presets to the
  config without replacing existing provider tables, then reloads the registry.
  It is safe to call when providers already exist.
- `/v1/update` reads the version from `pyproject.toml` on the checkout's upstream
  branch and falls back to semver tags. It caches a successful check for six
  hours. `POST` refuses a dirty checkout, performs `git merge --ff-only`,
  rebuilds the TUI when Bun is available, and returns `restart_required`. It
  never embeds a GitHub token and relies on the normal git credential helper.
- `/v1/meta` includes `default_effort` from `[agent].effort`. Model summaries
  include `efforts`, the values advertised by the provider.

## 4. SSE stream

SSE frames use standard `id:`, `event:`, and `data:` lines. `event` is the event
type; `data` is a JSON envelope.

```text
id: 42
event: message.delta
data: {"v":1,"id":"evt_01...","seq":42,"type":"message.delta","session_id":"ses_01...","turn_id":"trn_01...","ts":"2026-11-19T00:00:01.234Z","data":{"message_id":"msg_01...","text":"Hello"}}
```

The stream emits a `heartbeat` event after 15 seconds without another event.
Clients should ignore it for display purposes.

Clients reconnect with `Last-Event-ID: 42` or `?after=42`. The server replays
durable events after that sequence, or returns `409` when the event is no longer
retained or the cursor is ahead of the current bus epoch. The `409` detail
includes `code=replay_unavailable` and the bus's current `last_seq`; the client
should hydrate stored messages and resume after that snapshot.

Event IDs are opaque strings for clients, even though the current implementation
uses the sequence number. `seq` is monotonic per session.

## 5. Event types

| Type | Data |
|---|---|
| `status` | `{state: "idle" \| "running" \| "waiting_permission" \| "aborting", phase?: "finishing_hooks"}` |
| `message.start` | `{message_id, role}` |
| `message.delta` | `{message_id, text}` |
| `message.end` | `{message_id, finish_reason}` |
| `message.reset` | `{message_id}` — clear transient output before a retry |
| `reasoning.delta` | `{message_id, text}` |
| `provider.retry` | `{provider, model, attempt, max_attempts, delay_s, code, message}` |
| `tool.start` | `{tool_call_id, name, arguments_summary, started_at}` |
| `tool.progress` | `{tool_call_id, chunk}` |
| `tool.end` | `{tool_call_id, status, exit_code?, duration_ms, output_summary, output_handle?}` |
| `permission.request` | `{request_id, tool_call_id, kind, preview, patterns, choices}` |
| `permission.resolved` | `{request_id, decision, reason?}` |
| `diff` | `{path, patch, view_hint}` |
| `todo.update` | `{items: [{id, text, state}]}` |
| `usage` | `{step_id, model, input, output, reasoning, cache_read, allowance, cost?, allowance_reported?, cost_reported?, billing_source}` |
| `checkpoint` | `{id, label?, commit, created_at}` |
| `context` | `{tokens, percent, context_window, estimated?, pruned, compacted}` |
| `verifier` | `{status, verdict?, summary?, attempt?, new_failures?}` |
| `plan` | `{path, content}` |
| `evolution.event` | `{kind, candidate_id?, tag?, score?, status}` |
| `error` | `{code, message, retryable, details?}` |
| `run.end` | `{run_id, reason}` |
| `heartbeat` | `{state: "heartbeat"}` |

Deltas are append-only. A client must tolerate duplicate IDs (apply
idempotently) and out-of-order reconnects. The server does not emit a delta after
`message.end` for that message.

## 6. Permission flow

`permission.request` includes a human-readable preview and the available choices:

```json
{
  "request_id": "perm_01...",
  "tool_call_id": "call_01...",
  "kind": "bash",
  "preview": {"command": "pytest -q", "cwd": "/work/repo"},
  "patterns": ["bash:pytest*"],
  "choices": ["once", "session", "always", "deny"]
}
```

The client responds with:

```json
{"decision": "once", "reason": null}
```

The server emits `permission.resolved`, then continues or returns a tool error.
A missing response keeps the run in `waiting_permission`; it never auto-approves.
A deny decision is fed back to the model as a tool error with the user's reason,
not as a silent failure. Pending daemon approvals expire as denial after 300
seconds.

Decisions must use the originating top-level session endpoint. An unrelated
session cannot resolve a pending request, including a forwarded child request.
Workspace-changing operations (direct shell, checkpoints, rewind, restoring a
fork) reserve overlapping workspaces until completion and return
`409 session_busy` while a run or conflicting operation owns that workspace.
Compaction is rejected while the session has an active run.

The ACP bridge has no interactive permission RPC and denies requests that need
approval. Configuring a shell sandbox never enables an unsandboxed background
fallback; sandboxed background jobs are currently unsupported.

## 7. Errors

HTTP errors use:

```json
{
  "error": {
    "code": "provider_rate_limited",
    "message": "provider returned 429",
    "retryable": true,
    "details": {"retry_after_s": 20, "model": "provider/model"}
  }
}
```

Provider errors are classified, not passed through raw. Error codes are stable;
new codes are added rather than changing an existing meaning. Secrets and full
headers are never included in error details.

## 8. Schema and code generation

- Pydantic v2 models are the source of truth for request, response, and event
  payloads.
- The daemon exposes OpenAPI 3.1 at `/v1/openapi.json`.
- CI regenerates `ui/tui/openapi.json` and `ui/tui/src/api/schema.d.ts` and fails
  when the committed files drift.
- The TUI consumes only the generated client. It must not hand-write HTTP calls.
- Contract tests validate event schemas, request schemas, version mismatch
  handling, and SSE resume after a dropped connection.

## 9. Cancellation and lifecycle

- `POST /abort` clears queued prompts and immediately acknowledges that the
  active task was asked to cancel. The SSE `run.end` with
  `reason = "aborted"` confirms the stop after active tool or stream cleanup.
  Session-end hooks may still finish under the daemon's workspace guard.
- Closing an SSE connection does not abort the run. The run continues and the
  client can resume from the last event ID.
- Daemon shutdown aborts active runs, persists their state, and emits an audit
  entry. The supervisor restarts the daemon; a session can be resumed.

## 10. Compatibility policy

Within protocol `0.1`:

- add optional fields rather than changing existing types;
- add endpoints rather than repurposing paths;
- add event types; clients must ignore unknown event types;
- keep error codes stable;
- keep SSE sequence semantics per session.

Breaking changes require a protocol minor or major bump and a client-side refusal
of unsupported versions. The current protocol does not yet use a content
negotiation header beyond `X-Siwah-Protocol`.

## 11. Known gaps

- Unix sockets and Windows named pipes are not implemented.
- Remote deployment has no authentication beyond a local bearer token and no TLS.
- ACP is a separate stdio contract; the daemon HTTP API does not proxy it.
- Event retention is in memory per session (`[server].event_retention`, default
  2000). A restart creates a new bus epoch; clients must hydrate from the
  database and resume from the returned sequence.
- The OpenAPI document is a tooling and SDK contract, not a promise that every
  endpoint is stable outside the compatibility policy above.


A message submission can include `steer_run_id` and text to target the
active run. The server returns 409 for a stale run ID. A successful submission
acknowledges receipt. The `steering.applied` event identifies the durable
user message. `steering.dropped` identifies a correction the run ended
before applying. The messages endpoint includes it as a transcript-only
record with `delivery: "unapplied"`. Steering preserves the next-task FIFO.

The session detail includes a `prompt_queue` snapshot with the current
`event_seq` and ordered prompt summaries. Each summary has a stable ID,
a short preview, an attachment count, and a blocked reason when applicable.
`queue.changed` supplies the updated summaries. A queued run's first user
`message.start` includes its `queued_prompt_id` and content. An authenticated
`DELETE /v1/sessions/{session_id}/queue/{prompt_id}` cancels a pending
entry and returns its text and attachments for optional draft recovery.
An authenticated
`DELETE /v1/sessions/{session_id}/corrections/{message_id}` removes an
unapplied correction after the user sends it as a follow-up or restores it
to the editor.

Message records can include `tool_outcomes` keyed by tool-call ID. An
outcome carries the final status, an optional error, and an optional
duration in milliseconds. Live `tool.end` events use `skipped` when
steering supersedes a call before dispatch and include its argument summary.
