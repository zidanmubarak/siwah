import { expect, spyOn, test } from "bun:test"

import { SiwahApi } from "../src/api/client"
import { TuiStore } from "../src/state"

const runtime = { baseUrl: "http://fixture.invalid", token: "fixture-token", home: "" }
function mockFetch(implementation: (...args: Parameters<typeof fetch>) => Promise<Response>) {
  return spyOn(globalThis, "fetch").mockImplementation(
    Object.assign(implementation, { preconnect: fetch.preconnect }),
  )
}
const envelope = (seq: number) => ({
  v: 1,
  id: `event-${seq}`,
  seq,
  type: "message.delta",
  session_id: "fixture",
  ts: "2026-01-01T00:00:00Z",
  data: { message_id: "m", text: "🙂" },
})

test("JSON and SSE requests advertise the supported protocol and preserve Accept", async () => {
  const requests: Headers[] = []
  const fetchMock = mockFetch(async (_url, init) => {
    const headers = new Headers(init?.headers)
    requests.push(headers)
    return headers.get("Accept") === "text/event-stream"
      ? new Response(`data: ${JSON.stringify(envelope(1))}\n\n`)
      : Response.json({})
  })
  try {
    const api = new SiwahApi(runtime)
    await api.meta()
    for await (const _event of api.streamEvents("fixture")) {
      // Consume the fixture stream.
    }
    expect(requests.map((headers) => headers.get("X-Siwah-Protocol"))).toEqual(["0.1", "0.1"])
    expect(requests.map((headers) => headers.get("Accept"))).toEqual([
      "application/json",
      "text/event-stream",
    ])
  } finally {
    fetchMock.mockRestore()
  }
})

test("SSE CRLF and UTF-8 boundaries survive every individual byte split", async () => {
  const bytes = new TextEncoder().encode(
    `: heartbeat\r\n\r\ndata: ${JSON.stringify(envelope(1))}\r\n\r\ndata: ${JSON.stringify(envelope(2))}\r\n\r\n`,
  )
  const fetchMock = mockFetch(
    async () =>
      new Response(
        new ReadableStream<Uint8Array>({
          start(controller) {
            for (const byte of bytes) controller.enqueue(new Uint8Array([byte]))
            controller.close()
          },
        }),
      ),
  )
  try {
    const events = []
    for await (const event of new SiwahApi(runtime).streamEvents("fixture")) events.push(event)
    expect(events).toEqual([envelope(1), envelope(2)])
  } finally {
    fetchMock.mockRestore()
  }
})

test("breaking stream iteration cancels its reader", async () => {
  let cancelled = false
  const fetchMock = mockFetch(
    async () =>
      new Response(
        new ReadableStream<Uint8Array>({
          start(controller) {
            controller.enqueue(new TextEncoder().encode(`data: ${JSON.stringify(envelope(1))}\n\n`))
          },
          cancel() {
            cancelled = true
          },
        }),
      ),
  )
  try {
    for await (const _event of new SiwahApi(runtime).streamEvents("fixture")) break
    expect(cancelled).toBe(true)
  } finally {
    fetchMock.mockRestore()
  }
})

test("incompatible handshake refuses before loading other daemon endpoints", async () => {
  const previousServer = process.env.SIWAH_SERVER
  const previousToken = process.env.SIWAH_TOKEN
  process.env.SIWAH_SERVER = runtime.baseUrl
  process.env.SIWAH_TOKEN = runtime.token
  const requests: string[] = []
  const fetchMock = mockFetch(async (input) => {
    const path = new URL(String(input)).pathname
    requests.push(path)
    return Response.json(
      path === "/v1/meta"
        ? {
            name: "siwah",
            version: "fixture",
            protocol: "9.9",
            daemon_started_at: "",
            capabilities: {},
          }
        : { ok: true },
    )
  })
  try {
    const store = new TuiStore()
    await store.initialize()
    expect(store.connection()).toBe("offline")
    expect(store.runtimeError()).toContain("protocol")
    expect(requests.filter((path) => path !== "/v1/meta" && path !== "/v1/health")).toEqual([])
    expect(store.meta()).toBeNull()
  } finally {
    fetchMock.mockRestore()
    if (previousServer === undefined) delete process.env.SIWAH_SERVER
    else process.env.SIWAH_SERVER = previousServer
    if (previousToken === undefined) delete process.env.SIWAH_TOKEN
    else process.env.SIWAH_TOKEN = previousToken
  }
})
