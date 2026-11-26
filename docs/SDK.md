# SDK and integration surfaces

siwah exposes three integration surfaces:

1. a newline-delimited JSON runner for scripts and CI;
2. a local HTTP + SSE daemon with an OpenAPI schema;
3. an Agent Client Protocol (ACP) v1 stdio bridge for editors.

There is no separately packaged Python or TypeScript client library yet. The
OpenAPI document and the NDJSON contract are the interfaces.

## NDJSON runner

```bash
siwah run "Summarize this repository" --json
```

`--json` (or `--ui json`) writes one JSON object per line to stdout. Each line has
this shape:

```json
{"event": "message.delta", "data": {"message_id": "msg_01...", "text": "Hello"}}
```

The event names and payloads are the same logical events used by the daemon, but
the runner envelope is `{"event", "data"}` rather than the daemon's `EventEnvelope`.
Do not assume the daemon envelope when parsing `siwah run --json`.

```text
message.start
message.delta
message.end
reasoning.delta
tool.start
tool.progress
tool.end
permission.request
usage
checkpoint
context
verifier
plan
error
run.end
```

Properties:

- One line is one complete JSON object; flush happens per line.
- `run.end` carries the terminal reason and is the last event.
- The process exit code is authoritative: `0` success, `1` run error, `2` usage
  or configuration error, `130` interrupted.
- The runner uses the configured permission mode. JSON mode cannot prompt: a
  permission request is denied unless the mode or a rule already allows it.
  Pass `--permission auto-edit` or `--permission yolo` deliberately.
- `ask_user` is unavailable in JSON mode. Design prompts so the agent does not
  need interactive input.

Example consumer:

```bash
siwah run "Run the tests and summarize failures" \
  --json --permission auto-edit | jq -c 'select(.event == "tool.end" or .event == "run.end")'
```

## Local daemon API

Start the daemon:

```bash
siwah serve
```

The daemon prints its endpoint and writes `~/.siwah/runtime.json` (mode 600):

```json
{
  "host": "127.0.0.1",
  "port": 53421,
  "token": "...",
  "pid": 12345
}
```

Every request sends the bearer token:

```bash
curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:53421/v1/meta
```

### Minimal session flow

1. `POST /v1/sessions` with `{"cwd": "/path/to/repo", "model": "provider/model-id"}`.
2. `POST /v1/sessions/{id}/messages` with `{"content": "..."}`.
3. `GET /v1/sessions/{id}/events?after=0` for the SSE stream.
4. Resolve permission requests with
   `POST /v1/sessions/{id}/permissions/{request_id}`.
5. `POST /v1/sessions/{id}/abort` to cancel.

Python example:

```python
import json
from pathlib import Path

import httpx

runtime = json.loads((Path.home() / ".siwah" / "runtime.json").read_text())
base = f"http://{runtime['host']}:{runtime['port']}/v1"
headers = {"Authorization": f"Bearer {runtime['token']}", "X-Siwah-Protocol": "0.1"}

with httpx.Client(headers=headers, timeout=30) as client:
    session = (
        client.post(
            f"{base}/sessions",
            json={
                "cwd": str(Path.cwd()),
                "model": "opencode-go/<model-id>",
            },
        )
        .raise_for_status()
        .json()
    )

    client.post(
        f"{base}/sessions/{session['id']}/messages", json={"content": "List the failing tests."}
    ).raise_for_status()

    with client.stream(
        "GET", f"{base}/sessions/{session['id']}/events", params={"after": 0}
    ) as response:
        for line in response.iter_lines():
            if line.startswith("data: "):
                event = json.loads(line[6:])
                print(event["type"], event.get("data"))
```

### SSE and resume

Use `Last-Event-ID` or `?after=<seq>` after a reconnect. The daemon replays from
its in-memory retention window. If the cursor is unavailable, it returns `409`
with `code=replay_unavailable` and `last_seq`; hydrate message history from
`GET /v1/sessions/{id}/messages` and resume after that sequence.

A session response includes `last_event_seq`. Start the stream after that value
when you have already hydrated the message list, otherwise streamed deltas will
duplicate the hydrated text.

### Authentication and transport

- Loopback TCP only. There is no TLS and no multi-user authentication.
- The token is local and per daemon start. Do not log it or commit it.
- Binding to a non-loopback host exposes the API; do not do that without an
  external security layer.
- The server sends no CORS headers.

### Contract and generated types

```bash
siwah openapi > openapi.json
```

The daemon serves the same document at `/v1/openapi.json`. The TUI's types are
generated into `ui/tui/src/api/schema.d.ts` and CI fails on drift. The full
endpoint and event contract is in [PROTOCOL.md](PROTOCOL.md).

## ACP bridge

```bash
siwah acp
```

This is an Agent Client Protocol v1 bridge over stdin/stdout, using JSON-RPC 2.0.
It supports:

- `initialize`;
- `session/new`;
- `session/prompt`;
- `session/cancel`;
- `session/update` notifications.

It advertises text prompt content. Session paths must be absolute. A prompt
returns a `stopReason` such as `end_turn` or `cancelled`.

Current limitations:

- No client permission dialogs. A request that needs `ask` approval is denied;
  explicitly allowed policy operations still run.
- No `loadSession`.
- No client filesystem or terminal methods.
- No rich content blocks beyond text.
- No MCP-over-ACP configuration.

If your editor needs any of those, use the daemon API instead.

## Choosing a surface

| Surface | Best for | Main limitation |
|---|---|---|
| `siwah run --json` | CI, scripts, one-shot tasks | No interactive permission prompts |
| Daemon HTTP + SSE | Long-lived integrations, custom UIs, multi-session clients | Local bearer-token API, no TLS |
| ACP | Editors that already speak ACP | Minimal feature set, no permission dialogs |

## Versioning

The application version and the daemon protocol version are independent. At
siwah 1.0.5 the daemon protocol is `0.1`. Within that minor, fields and events are
added compatibly; existing meanings are not repurposed. Clients should ignore
unknown event types and optional fields, and should reject an unsupported
protocol version.

The NDJSON runner does not send a protocol header. Treat unknown event names as
optional additions and rely on `run.end` plus the exit code for completion.

## Known gaps

- No official client SDK packages.
- No webhook or callback delivery; use SSE.
- No remote daemon mode with TLS, OAuth, or multi-user isolation.
- ACP remains a baseline bridge, not a full editor integration.
- Event retention is in memory; a daemon restart starts a new bus epoch.
