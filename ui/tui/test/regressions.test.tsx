import { expect, test } from "bun:test"
import { testRender } from "@opentui/solid"

import { ApiError } from "../src/api/client"
import { hydrateMessages, TuiStore, type UiMessage } from "../src/state"
import { resolveTheme } from "../src/theme"
import { App } from "../src/ui/App"
import { turnAnchors } from "../src/ui/Dialogs"

function readyStore(): TuiStore {
  const store = new TuiStore()
  store.theme(resolveTheme("siwah-dark", false))
  store.meta({
    name: "siwah",
    version: "0.1.0",
    protocol: "0.1",
    daemon_started_at: "2026-01-01T00:00:00Z",
    capabilities: { sse_resume: true, permission_modes: ["ask"], features: [] },
  })
  store.models({ providers: [], roles: { main: "mock/model-a" } })
  store.auth({ providers: [] })
  store.sessions([])
  store.evolution({ tag: "evo-0", status: "idle", events: [] })
  store.session({
    id: "ses_demo",
    title: "debug",
    cwd: "D:/work/siwah",
    model: "mock/model-a",
    mode: "build",
    status: "idle",
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    metadata: {},
    last_event_seq: 0,
  })
  store.connection("online")
  return store
}

type StoreInternals = {
  handleEvent: (event: Record<string, unknown>) => void
  api: unknown
  runtimeError: (value?: string | null) => string | null
  startStream: (sessionId: string) => void
  flushStreams: () => void
  streamAbort?: AbortController
}

function internal(store: TuiStore): StoreInternals {
  return store as unknown as StoreInternals
}

function message(overrides: Partial<UiMessage> & { id: string }): UiMessage {
  return {
    role: "assistant",
    content: "",
    reasoning: "",
    streaming: false,
    createdAt: "2026-01-01T00:00:00Z",
    toolCalls: [],
    diffs: [],
    reasoningOpen: false,
    ...overrides,
  }
}

test("hydrated messages keep persisted edit diffs", () => {
  const messages = hydrateMessages([
    {
      id: "msg_edit",
      seq: 1,
      role: "assistant",
      content: "edited",
      reasoning: null,
      tool_calls: [],
      tool_call_id: null,
      diffs: [{ path: "hello.py", patch: "--- a/hello.py\n+++ b/hello.py\n" }],
      created_at: "2026-01-01T00:00:00Z",
    },
  ])
  expect(messages[0]?.diffs[0]?.path).toBe("hello.py")
  expect(messages[0]?.diffs[0]?.patch).toContain("+++ b/hello.py")
})

test("hydrated write_file edits keep the edit marker", () => {
  const messages = hydrateMessages([
    {
      id: "msg_write",
      seq: 1,
      role: "assistant",
      content: "wrote",
      reasoning: null,
      tool_calls: [
        {
          id: "call_write",
          name: "write_file",
          arguments: { path: "hello.py" },
          arguments_valid: true,
        },
      ],
      tool_call_id: null,
      tool_outputs: { call_write: "new line\n" },
      tool_edited: { call_write: true },
      diffs: [],
      created_at: "2026-01-01T00:00:00Z",
    },
  ])
  expect(messages[0]?.toolCalls[0]?.edited).toBe(true)
})

test("T-02 aborted run clears streaming", () => {
  const store = readyStore()
  store.messages([message({ id: "a1", content: "Half", streaming: true })])
  internal(store).handleEvent({ seq: 3, type: "run.end", data: { reason: "aborted" } })
  expect(store.messages()[0].streaming).toBe(false)
})

test("prompts submitted during a run are accepted into the queue", async () => {
  const store = readyStore()
  store.running(true)
  const sent: string[] = []
  internal(store).api = {
    sendMessage: async (_sessionId: string, content: string) => {
      sent.push(content)
      return { status: "queued" }
    },
  }
  expect(await store.sendPrompt("next task")).toBe(true)
  expect(sent).toEqual(["next task"])
  expect(store.messages()).toHaveLength(1)
  expect(store.queuedPrompts().map((item) => item.text)).toEqual(["next task"])
  internal(store).handleEvent({
    seq: 20,
    type: "message.start",
    data: { role: "user", message_id: "server-user" },
  })
  expect(store.queuedPrompts()).toHaveLength(0)
})

test("abort clears queued prompts and their local echoes", async () => {
  const store = readyStore()
  store.running(true)
  internal(store).api = {
    sendMessage: async () => ({ status: "queued" }),
    abort: async () => ({ ok: true }),
  }
  await store.sendPrompt("do not run this")
  expect(store.queuedPrompts()).toHaveLength(1)
  expect(store.messages()).toHaveLength(1)
  await store.abort()
  expect(store.queuedPrompts()).toHaveLength(0)
  expect(store.messages()).toHaveLength(0)
})

test("message reset clears partial streamed reasoning before a retry", () => {
  const store = readyStore()
  store.messages([
    message({ id: "a1", content: "partial", reasoning: "thinking", streaming: true }),
  ])
  internal(store).handleEvent({
    seq: 30,
    type: "reasoning.delta",
    data: { message_id: "a1", text: " more" },
  })
  internal(store).handleEvent({
    seq: 31,
    type: "message.reset",
    data: { message_id: "a1" },
  })
  expect(store.messages()[0]).toMatchObject({
    content: "",
    reasoning: "",
    streaming: true,
    toolCalls: [],
    diffs: [],
  })
  internal(store).handleEvent({
    seq: 32,
    type: "provider.retry",
    data: {
      attempt: 1,
      max_attempts: 5,
      delay_s: 0.5,
      model: "model-a",
      code: "provider_timeout",
      message: "timed out",
    },
  })
  expect(store.providerRetry()?.code).toBe("provider_timeout")
  internal(store).handleEvent({
    seq: 33,
    type: "reasoning.delta",
    data: { message_id: "a1", text: "fresh" },
  })
  expect(store.providerRetry()).toBeNull()
})

test("update shows a progress dialog before the restart prompt", async () => {
  const store = readyStore()
  let finish:
    | ((value: { ok: boolean; restart_required: boolean; to_version: string }) => void)
    | undefined
  internal(store).api = {
    applyUpdate: () =>
      new Promise((resolve) => {
        finish = resolve as typeof finish
      }),
  }
  const pending = store.applyUpdate()
  expect(store.dialog()).toEqual({ type: "update" })
  finish?.({ ok: true, restart_required: true, to_version: "9.9.9" })
  await pending
  expect(store.dialog()).toEqual({ type: "restart", version: "9.9.9" })
})

test("abort without a live daemon task clears a stale running state", async () => {
  const store = readyStore()
  store.running(true)
  store.session((session) => (session ? { ...session, status: "running" } : session))
  store.messages([message({ id: "a1", content: "half", streaming: true })])
  internal(store).api = {
    abort: async () => ({ ok: false }),
  }
  await store.abort()
  expect(store.running()).toBe(false)
  expect(store.session()?.status).toBe("idle")
  expect(store.messages()[0].streaming).toBe(false)
})

test("T-03 conflict rolls back the local echo", async () => {
  const store = readyStore()
  internal(store).api = {
    sendMessage: async () => {
      throw new ApiError("A run is already active for this session", 409, "run_conflict")
    },
    streamEvents: async function* (_sessionId: string, _after: number, signal: AbortSignal) {
      await new Promise<void>((resolve) =>
        signal.addEventListener("abort", () => resolve(), { once: true }),
      )
    },
  }
  await store.sendPrompt("lost prompt")
  expect(store.messages()).toHaveLength(0)
  expect(store.running()).toBe(true)
  internal(store).streamAbort?.abort()
})

test("streaming deltas are batched before they hit the message list", () => {
  const store = readyStore()
  store.messages([message({ id: "a1", content: "", streaming: true })])
  internal(store).handleEvent({
    seq: 4,
    type: "message.delta",
    data: { message_id: "a1", text: "hello " },
  })
  internal(store).handleEvent({
    seq: 5,
    type: "message.delta",
    data: { message_id: "a1", text: "world" },
  })
  expect(store.messages()[0]?.content).toBe("")
  internal(store).flushStreams()
  expect(store.messages()[0]?.content).toBe("hello world")
})

test("T-04 unknown delta id becomes a new message", () => {
  const store = readyStore()
  store.messages([message({ id: "a1", content: "first" })])
  internal(store).handleEvent({
    seq: 4,
    type: "message.delta",
    data: { message_id: "a2", text: " second" },
  })
  const messages = store.messages()
  expect(messages).toHaveLength(2)
  expect(messages[0].content).toBe("first")
  expect(messages[1].content).toBe(" second")
})

test("T-06 reconnect clears the sticky runtime error", async () => {
  const store = readyStore()
  internal(store).runtimeError("daemon went away")
  internal(store).api = {
    streamEvents: async function* (
      _sessionId: string,
      _after: number,
      _signal: AbortSignal,
      onOpen: () => void,
    ) {
      onOpen()
      await new Promise((resolve) => setTimeout(resolve, 200))
    },
  }
  internal(store).startStream("ses_demo")
  await new Promise((resolve) => setTimeout(resolve, 100))
  expect(internal(store).runtimeError()).toBeNull()
  internal(store).streamAbort?.abort()
})

test("T-01 long diff collapses", async () => {
  const store = readyStore()
  const patch = [
    "@@ -1,1 +1,41 @@",
    " header",
    ...Array.from({ length: 40 }, (_, index) => `+added line ${index + 1}`),
  ].join("\n")
  store.messages([message({ id: "a1", diffs: [{ path: "big.py", patch }] })])
  const setup = await testRender(() => <App store={store} initialize={false} />, {
    width: 100,
    height: 70,
  })
  try {
    await setup.renderOnce()
    const frame = setup.captureCharFrame()
    expect(frame).toContain("more lines")
    expect(frame).not.toContain("added line 35")
  } finally {
    setup.renderer.destroy()
  }
})

test("T-05 shell output collapses", async () => {
  const store = readyStore()
  const content = [
    "$ printf rows",
    ...Array.from({ length: 15 }, (_, index) => `row ${index + 1}`),
  ].join("\n")
  store.messages([message({ id: "t1", role: "tool", content })])
  const setup = await testRender(() => <App store={store} initialize={false} />, {
    width: 100,
    height: 40,
  })
  try {
    await setup.renderOnce()
    const frame = setup.captureCharFrame()
    expect(frame).toContain("row 1")
    expect(frame).not.toContain("row 15")
    expect(frame).toContain("more lines")
  } finally {
    setup.renderer.destroy()
  }
})

test("effort options follow the selected model capabilities", () => {
  const store = readyStore()
  store.models({
    providers: [
      {
        provider: "mock",
        models: [{ id: "model-a", efforts: ["low", "high", "max"], supports_reasoning: true }],
      },
    ],
    roles: { main: "mock/model-a" },
  })
  expect(store.effortOptions()).toEqual(["off", "low", "high", "max"])
})

test("/init sends the AGENTS.md prompt as a normal chat message", async () => {
  const store = readyStore()
  let sent = ""
  internal(store).api = {
    sendMessage: async (_id: string, text: string) => {
      sent = text
      return { status: "running" }
    },
  }
  internal(store).startStream = () => {}
  const setup = await testRender(() => <App store={store} initialize={false} />, {
    width: 100,
    height: 32,
  })
  try {
    await setup.renderOnce()
    setup.mockInput.typeText("/init")
    setup.mockInput.pressEnter()
    await new Promise((resolve) => setTimeout(resolve, 150))
    expect(sent).toContain("Create or update `AGENTS.md`")
    expect(store.messages()[0]?.content).toBe(
      "Create or update AGENTS.md and .siwah/project.md for this repository.",
    )
  } finally {
    setup.renderer.destroy()
  }
})

test("TUI /effort and /permissions update the runtime settings", async () => {
  const store = readyStore()
  store.models({
    providers: [
      {
        provider: "mock",
        models: [{ id: "model-a", efforts: ["low", "high", "max"], supports_reasoning: true }],
      },
    ],
    roles: { main: "mock/model-a" },
  })
  const calls: Array<Record<string, unknown>> = []
  const events: string[] = []
  internal(store).api = {
    updateSession: async (_id: string, body: Record<string, unknown>) => {
      calls.push(body)
      const current = store.session()
      if (!current) throw new Error("session missing")
      return { ...current, metadata: { effort: body.effort } }
    },
    setDefaultEffort: async (effort: string) => ({ effort }),
    setPermissionMode: async (mode: string) => ({ mode }),
    evolution: async () => ({ tag: "evo-1", status: "idle", events: [] }),
    evolutionPause: async (paused: boolean) => {
      events.push(`pause:${paused}`)
      return { ok: true }
    },
    evolutionRollback: async (tag?: string | null) => {
      events.push(`rollback:${tag ?? ""}`)
      return { ok: true, tag: tag ?? "evo-0", commit: "c" }
    },
  }
  const setup = await testRender(() => <App store={store} initialize={false} />, {
    width: 110,
    height: 30,
  })
  try {
    await setup.renderOnce()
    setup.mockInput.typeText("/effort")
    setup.mockInput.pressEnter()
    await new Promise((resolve) => setTimeout(resolve, 100))
    setup.mockInput.pressArrow("down")
    setup.mockInput.pressEnter()
    await new Promise((resolve) => setTimeout(resolve, 100))
    expect(calls[0]?.effort).toBe("low")
    expect(store.effort()).toBe("low")

    setup.mockInput.typeText("/permissions")
    setup.mockInput.pressEnter()
    await new Promise((resolve) => setTimeout(resolve, 100))
    expect(store.permissionMode()).toBe("auto-edit")

    setup.mockInput.typeText("/permissions")
    setup.mockInput.pressEnter()
    await new Promise((resolve) => setTimeout(resolve, 100))
    expect(store.permissionMode()).toBe("yolo")

    setup.mockInput.typeText("/permissions")
    setup.mockInput.pressEnter()
    await new Promise((resolve) => setTimeout(resolve, 100))
    expect(store.permissionMode()).toBe("ask")

    setup.mockInput.typeText("/reasoning")
    setup.mockInput.pressEnter()
    await new Promise((resolve) => setTimeout(resolve, 100))
    expect(store.showReasoning()).toBe(false)

    setup.mockInput.typeText("/pause")
    setup.mockInput.pressEnter()
    await new Promise((resolve) => setTimeout(resolve, 100))
    expect(events).toContain("pause:true")

    setup.mockInput.typeText("/rollback evo-1")
    setup.mockInput.pressEnter()
    await new Promise((resolve) => setTimeout(resolve, 100))
    expect(events).toContain("rollback:evo-1")
  } finally {
    setup.renderer.destroy()
  }
})

test("turn anchors point at the user message that starts each chat", () => {
  const message = (id: string, role: "user" | "assistant", content: string) => ({
    id,
    role,
    content,
    reasoning: "",
    streaming: false,
    createdAt: "2026-01-01T00:00:00Z",
    toolCalls: [],
    diffs: [],
    reasoningOpen: false,
  })
  const anchors = turnAnchors([
    message("u1", "user", "first question"),
    message("a1", "assistant", "first answer"),
    message("u2", "user", "second question"),
    message("a2", "assistant", "second answer"),
  ])
  expect(anchors.map((anchor) => anchor.value)).toEqual(["u1", "u2"])
  expect(anchors[0]?.label).toContain("first question")
  expect(anchors[1]?.label).toContain("second question")
})

test("reasoning toggle does not bump the scroll tick", () => {
  const store = new TuiStore()
  store.messages([
    {
      id: "a1",
      role: "assistant",
      content: "answer",
      reasoning: "thinking",
      streaming: false,
      createdAt: "2026-01-01T00:00:00Z",
      toolCalls: [],
      diffs: [],
      reasoningOpen: false,
    },
  ])
  const before = store.scrollTick()
  store.toggleReasoning("a1")
  expect(store.scrollTick()).toBe(before)
  expect(store.messages()[0]?.reasoningOpen).toBe(true)
})

test("context summary shows the model window and compact threshold", () => {
  const store = new TuiStore()
  store.models({
    providers: [
      {
        provider: "mock",
        models: [
          {
            id: "model-a",
            name: "Model A",
            context_window: 1_000_000,
            max_output: 8_000,
            supports_tools: true,
            supports_reasoning: true,
          },
        ],
      },
    ],
    roles: { main: "mock/model-a" },
  })
  store.usage({
    input_tokens: 10,
    output_tokens: 2,
    reasoning_tokens: 0,
    cache_read_tokens: 0,
    cache_write_tokens: 0,
    allowance: 0,
    cost: 0,
    list_cost: 0,
    steps: 1,
    context_window: 1_000_000,
    context_tokens: 9_600,
    context_percent: 0.96,
  })
  expect(store.contextSummary()).toBe("ctx 9.6k/1M 1%")
  store.usage((current) => (current ? { ...current, context_percent: 96 } : current))
  expect(store.contextSummary()).toContain("compact")
})

test("closing a tab hides that session from the project tab list", async () => {
  const store = new TuiStore()
  const base = {
    cwd: "D:/work/siwah",
    model: "mock/model-a",
    mode: "build",
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    metadata: {},
    last_event_seq: 0,
  }
  store.sessions([
    { ...base, id: "ses_a", title: "A", status: "idle" },
    { ...base, id: "ses_b", title: "B", status: "idle" },
  ])
  store.session({ ...base, id: "ses_a", title: "A", status: "idle" })
  await store.closeSessionTab("ses_b")
  expect(store.hiddenSessions()).toContain("ses_b")
  expect(store.visibleProjectSessions().map((session) => session.id)).toEqual(["ses_a"])
})

test("session tab title updates as soon as the first prompt is sent", async () => {
  const store = new TuiStore()
  const base = {
    id: "ses_title",
    title: null,
    cwd: "D:/work/siwah",
    model: "mock/model-a",
    mode: "build",
    status: "idle",
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    metadata: {},
    last_event_seq: 0,
  }
  store.sessions([base])
  store.session(base)
  const api = {
    updateSession: async (_id: string, body: { title?: string }) => ({ ...base, ...body }),
    sendMessage: async () => ({ session_id: base.id, run_id: "run", event_id: 1 }),
    streamEvents: async function* () {},
  }
  ;(store as unknown as { api: typeof api }).api = api
  await store.sendPrompt("rename this session")
  expect(store.session()?.title).toBe("rename this session")
  expect(store.sessions()[0]?.title).toBe("rename this session")
})

test("context events are not overwritten by usage refreshes", () => {
  const store = new TuiStore()
  const base = {
    input_tokens: 1,
    output_tokens: 1,
    reasoning_tokens: 0,
    cache_read_tokens: 0,
    cache_write_tokens: 0,
    allowance: 0,
    cost: 0,
    list_cost: 0,
    steps: 1,
    context_window: 1_000_000,
    context_tokens: 20_000,
    context_percent: 2,
  }
  store.usage(base)
  internal(store).handleEvent({
    seq: 1,
    type: "context",
    data: { tokens: 10_000, percent: 1, context_window: 1_000_000 },
  })
  expect(store.contextSummary()).toBe("ctx 10k/1M 1%")
  store.usage({ ...base, context_tokens: 20_000, context_percent: 2 })
  expect(store.contextSummary()).toBe("ctx 10k/1M 1%")
})

test("hydration keeps recorded tool outcomes, durations and full output", () => {
  const base = { created_at: "2026-11-24T00:00:00Z", seq: 1 }
  const messages = hydrateMessages([
    {
      ...base,
      id: "answer",
      role: "assistant",
      tool_calls: ["failed", "skipped", "unfinished"].map((id) => ({
        id,
        name: "edit_file",
        arguments: {},
        arguments_valid: true,
      })),
      tool_outcomes: {
        failed: { status: "error", error: "edit failed", duration_ms: 37 },
        skipped: { status: "skipped", duration_ms: 0 },
      },
      tool_outputs: { failed: "full error output" },
    },
    { ...base, id: "row1", role: "tool", tool_call_id: "failed", content: "short error" },
    { ...base, id: "row2", role: "tool", tool_call_id: "skipped", content: "superseded" },
  ])
  expect(messages[0].toolCalls).toMatchObject([
    { status: "error", error: "edit failed", durationMs: 37, output: "full error output" },
    { status: "skipped", durationMs: 0, output: "superseded" },
    { status: "cancelled", durationMs: null },
  ])
})

test("a skipped live tool gets a card even without a tool start event", () => {
  const store = readyStore()
  store.messages([message({ id: "answer" })])
  internal(store).handleEvent({
    seq: 1,
    type: "tool.end",
    data: {
      message_id: "answer",
      tool_call_id: "skip",
      name: "edit_file",
      status: "skipped",
      output: "superseded",
      arguments_summary: "src/parser.ts",
    },
  })
  expect(store.messages()[0].toolCalls).toMatchObject([
    { id: "skip", status: "skipped", summary: "src/parser.ts", output: "superseded" },
  ])
})
