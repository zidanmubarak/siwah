import { expect, test } from "bun:test"
import type { components, paths } from "../src/api/schema"

test("generated schema exposes the session protocol", () => {
  const path: keyof paths = "/v1/sessions/{session_id}/events"
  expect(path).toBe("/v1/sessions/{session_id}/events")
})

test("generated schema exposes session and event types", () => {
  const session: components["schemas"]["SessionResponse"] = {
    id: "ses_1",
    cwd: ".",
    mode: "build",
    status: "idle",
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    metadata: {},
    last_event_seq: 0,
  }
  const event: components["schemas"]["EventEnvelope"] = {
    v: 1,
    id: "evt_1",
    seq: 1,
    type: "status",
    session_id: session.id,
    ts: "2026-01-01T00:00:00Z",
    data: {},
  }
  expect(event.session_id).toBe(session.id)
})
