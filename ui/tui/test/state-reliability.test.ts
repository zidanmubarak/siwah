import { afterAll, beforeAll, expect, test } from "bun:test"
import { mkdtempSync, rmSync } from "node:fs"
import { tmpdir } from "node:os"
import { join } from "node:path"

import { ApiError, type EventEnvelope, type Session, type Usage } from "../src/api/client"
import { hydrateMessages, TuiStore } from "../src/state"

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason: unknown) => void
  const promise = new Promise<T>((yes, no) => {
    resolve = yes
    reject = no
  })
  return { promise, resolve, reject }
}

function session(id: string, overrides: Partial<Session> = {}): Session {
  return {
    id,
    cwd: `D:/fixture/${id}`,
    model: "mock/model",
    mode: "build",
    title: id,
    status: "idle",
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    metadata: {},
    last_event_seq: 0,
    ...overrides,
  }
}

const usage = (tokens = 0): Usage => ({
  input_tokens: tokens,
  output_tokens: 0,
  reasoning_tokens: 0,
  cache_read_tokens: 0,
  cache_write_tokens: 0,
  allowance: 0,
  cost: 0,
  list_cost: 0,
  steps: 0,
})
const plan = (content = "") => ({ path: ".siwah/plan.md", content, exists: Boolean(content) })
type Internals = {
  api: unknown
  startStream: (id: string) => void
  handleEvent: (event: Partial<EventEnvelope>) => void
  flushStreams: () => void
  streamLoop: (id: string, signal: AbortSignal) => Promise<void>
}
const internal = (store: TuiStore) => store as unknown as Internals

function ready(overrides: Record<string, unknown> = {}, initial: Session | null = session("a")) {
  const store = new TuiStore()
  internal(store).api = {
    session: async (id: string) => session(id),
    messages: async () => ({ messages: [] }),
    usage: async () => usage(),
    sessions: async () => ({ sessions: [] }),
    checkpoints: async () => ({ checkpoints: [] }),
    plan: async () => plan(),
    files: async () => ({ files: [] }),
    filesForCwd: async () => ({ files: [] }),
    commands: async () => ({ commands: [] }),
    createSession: async () => session("new"),
    ...overrides,
  }
  internal(store).startStream = () => {}
  store.session(initial)
  return store
}

function event(store: TuiStore, seq: number, type: string, data: Record<string, unknown>) {
  internal(store).handleEvent({
    seq,
    id: `event-${seq}`,
    session_id: store.session()?.id ?? "a",
    type,
    data,
  })
}

let home: string
let previousHome: string | undefined
beforeAll(() => {
  previousHome = process.env.SIWAH_HOME
  home = mkdtempSync(
    join(
      process.platform === "win32" ? "C:/Users/LENOVO/AppData/Local/Temp/opencode" : tmpdir(),
      "siwah-state-reliability-",
    ),
  )
  process.env.SIWAH_HOME = home
})
afterAll(() => {
  if (previousHome === undefined) delete process.env.SIWAH_HOME
  else process.env.SIWAH_HOME = previousHome
  rmSync(home, { recursive: true, force: true })
})

test("switch clears session-local state and restores only the selected session draft", async () => {
  const response = deferred<Session>()
  const store = ready({ session: (id: string) => (id === "b" ? response.promise : session(id)) })
  await store.openSession("a")
  store.running(true)
  event(store, 1, "permission.request", { request_id: "permission-a", choices: ["once"] })
  event(store, 2, "message.start", { message_id: "shared", role: "assistant" })
  event(store, 3, "message.delta", { message_id: "shared", text: "old buffered output" })
  store.files(["a.txt"])
  store.plan(plan("old plan"))
  store.promptText("unsent draft")
  const opening = store.openSession("b")
  expect(store.permission()).toBeNull()
  expect(store.running()).toBe(false)
  expect(store.messages()).toEqual([])
  expect(store.files()).toEqual([])
  expect(store.plan()).toBeNull()
  expect(store.promptText()).toBe("unsent draft")
  response.resolve(session("b"))
  await opening
  internal(store).flushStreams()
  expect(store.messages()).toEqual([])
  expect(store.promptText()).toBe("")
  store.promptText("draft for b")
  await store.openSession("a")
  expect(store.promptText()).toBe("unsent draft")
  await store.openSession("b")
  expect(store.promptText()).toBe("draft for b")
})

test("verifier and hook phases stay active until the matching completion", () => {
  const store = ready()
  store.running(true)
  event(store, 1, "verifier", { status: "baseline" })
  expect(store.runPhase()?.label).toBe("checking baseline")
  event(store, 2, "verifier", { status: "running" })
  expect(store.runPhase()?.label).toBe("running checks")
  event(store, 3, "verifier", { status: "passed" })
  expect(store.runPhase()).toBeNull()
  event(store, 4, "status", { state: "running", phase: "finishing_hooks" })
  expect(store.runPhase()?.label).toBe("finishing hooks")
  event(store, 5, "run.end", { reason: "completed" })
  expect(store.runPhase()).toBeNull()
})

test("active steering uses the run ID and delivery does not dequeue the next task", async () => {
  const submitted: string[][] = []
  const store = ready({
    steer: async (sessionId: string, runId: string, content: string) => {
      submitted.push([sessionId, runId, content])
      return { status: "steering", run_id: runId, message_id: "correction", event_id: 0 }
    },
  })
  store.running(true)
  store.activeRunId("active")
  store.queuedPrompts([{ id: "next", text: "Next task." }])
  expect(await store.steerPrompt("Use the read-only path.")).toBe(true)
  expect(submitted).toEqual([["a", "active", "Use the read-only path."]])
  expect(store.messages()).toHaveLength(0)
  event(store, 1, "steering.applied", {
    message_id: "correction",
    content: "Use the read-only path.",
  })
  expect(store.messages()[0]?.content).toBe("Use the read-only path.")
  expect(store.queuedPrompts()).toHaveLength(1)
})

test("newest session navigation wins even when the first response arrives last", async () => {
  const old = deferred<Session>()
  const store = ready({ session: (id: string) => (id === "a" ? old.promise : session(id)) })
  const first = store.openSession("a")
  await store.openSession("b")
  old.resolve(session("a"))
  await first
  expect(store.session()?.id).toBe("b")
})

test("late creation cannot undo a newer navigation", async () => {
  const response = deferred<Session>()
  const store = ready({ createSession: () => response.promise })
  const creating = store.createSession()
  await store.openSession("b")
  response.resolve(session("new"))
  expect(await creating).toBeNull()
  expect(store.session()?.id).toBe("b")
})

test("creating a session drops the previous run and approval", async () => {
  const store = ready()
  store.running(true)
  event(store, 1, "permission.request", { request_id: "permission-a" })
  await store.createSession()
  expect(store.session()?.id).toBe("new")
  expect(store.running()).toBe(false)
  expect(store.permission()).toBeNull()
})

test("permission decisions cannot be rebound to a different current session", async () => {
  const calls: string[] = []
  const store = ready({
    decidePermission: async (id: string) => calls.push(id),
  })
  event(store, 1, "permission.request", { request_id: "permission-a", choices: ["once"] })
  store.session(session("b"))
  await store.resolvePermission("once")
  expect(calls).toEqual([])
})

test("shell approvals cannot run in the workspace of a different session", async () => {
  const calls: Array<[string, boolean]> = []
  const store = ready({
    shell: async (id: string, _command: string, approved: boolean) => {
      calls.push([id, approved])
      throw new ApiError("approval needed", 409, "permission_required")
    },
  })
  await store.runShell("fixture-command")
  store.session(session("b"))
  await store.resolvePermission("once")
  expect(calls).toEqual([["a", false]])
})

test("late shell failures cannot create approvals in another session", async () => {
  const response = deferred<never>()
  const store = ready({ shell: () => response.promise })
  const shell = store.runShell("fixture-command")
  await store.openSession("b")
  response.reject(new ApiError("approval needed", 409, "permission_required"))
  await shell
  expect(store.permission()).toBeNull()
})

test("a resolved permission response does not clear a newer approval", async () => {
  const response = deferred<unknown>()
  const store = ready({ decidePermission: () => response.promise })
  event(store, 1, "permission.request", { request_id: "first", choices: ["once"] })
  const resolving = store.resolvePermission("once")
  event(store, 2, "permission.request", { request_id: "second", choices: ["once"] })
  response.resolve({})
  await resolving
  expect(store.permission()?.requestId).toBe("second")
})

test("reentrant permission decisions submit only once", async () => {
  const response = deferred<unknown>()
  let decisions = 0
  const store = ready({
    decidePermission: () => {
      decisions += 1
      return response.promise
    },
  })
  event(store, 1, "permission.request", { request_id: "first", choices: ["once"] })
  const first = store.resolvePermission("once")
  await store.resolvePermission("once")
  expect(decisions).toBe(1)
  response.resolve({})
  await first
})

test("late usage cannot overwrite the newly selected session", async () => {
  const response = deferred<Usage>()
  const store = ready({ usage: (id: string) => (id === "a" ? response.promise : usage(2)) })
  const refreshing = store.refreshUsage()
  await store.openSession("b")
  response.resolve(usage(1))
  await refreshing
  expect(store.usage()?.input_tokens).toBe(2)
})

test("newest same-session usage request wins", async () => {
  const response = deferred<Usage>()
  let calls = 0
  const store = ready({ usage: () => (++calls === 1 ? response.promise : usage(2)) })
  const first = store.refreshUsage()
  await store.refreshUsage()
  response.resolve(usage(1))
  await first
  expect(store.usage()?.input_tokens).toBe(2)
})

for (const field of ["model", "mode", "effort"] as const) {
  test(`late ${field} update cannot replace the newly selected session`, async () => {
    const response = deferred<Session>()
    const store = ready({
      updateSession: () => response.promise,
      setDefaultEffort: async () => ({}),
    })
    const changing =
      field === "model"
        ? store.selectModel("mock/other")
        : field === "mode"
          ? store.setMode("plan")
          : store.setEffort("high")
    await store.openSession("b")
    response.resolve(session("a", { [field]: field === "mode" ? "plan" : "mock/other" }))
    await changing
    expect(store.session()?.id).toBe("b")
    expect(store.currentMode()).toBe("build")
    expect(store.currentModel()).toBe("mock/model")
  })
}

test("late plan, checkpoints, files and commands cannot enter another session", async () => {
  const oldPlan = deferred<ReturnType<typeof plan>>()
  const oldCheckpoints = deferred<{ checkpoints: [] }>()
  const oldFiles = deferred<{ files: string[] }>()
  const oldCommands = deferred<{
    commands: Array<{ name: string; description: string; body: string }>
  }>()
  const store = ready({
    plan: (id: string) => (id === "a" ? oldPlan.promise : plan("b")),
    checkpoints: (id: string) => (id === "a" ? oldCheckpoints.promise : { checkpoints: [] }),
    files: (id: string) => (id === "a" ? oldFiles.promise : { files: ["b.txt"] }),
    commands: (cwd: string) => (cwd.endsWith("/a") ? oldCommands.promise : { commands: [] }),
  })
  const loading = Promise.all([
    store.loadPlan(),
    store.reloadCheckpoints(),
    store.loadFiles(),
    store.reloadCommands(),
  ])
  await store.openSession("b")
  oldPlan.resolve(plan("a"))
  oldCheckpoints.resolve({ checkpoints: [] })
  oldFiles.resolve({ files: ["a.txt"] })
  oldCommands.resolve({ commands: [{ name: "a", description: "a", body: "a" }] })
  await loading
  expect(store.plan()?.content).toBe("b")
  expect(store.files()).toEqual(["b.txt"])
  expect(store.customCommands()).toEqual([])
})

test("opening existing sessions never applies the default effort to their metadata", async () => {
  const calls: unknown[] = []
  const store = ready({
    session: async (id: string) => session(id, { metadata: { effort: "off" } }),
    updateSession: async (...args: unknown[]) => {
      calls.push(args)
      return session("b")
    },
  })
  store.pendingEffort("high")
  await store.openSession("b")
  expect(calls).toEqual([])
  expect(store.session()?.metadata?.effort).toBe("off")
  expect(store.effort()).toBeNull()
})

test("prompt admission is latched before a first-title request", async () => {
  const response = deferred<Session>()
  let sends = 0
  const store = ready(
    {
      updateSession: () => response.promise,
      sendMessage: async () => {
        sends += 1
        return {}
      },
    },
    session("a", { title: null }),
  )
  const first = store.sendPrompt("first")
  const second = store.sendPrompt("second")
  response.resolve(session("a", { title: "first" }))
  expect(await first).toBe(true)
  expect(await second).toBe(false)
  expect(sends).toBe(1)
})

test("prompt admission is latched before session creation", async () => {
  const response = deferred<Session>()
  let creates = 0
  let sends = 0
  const store = ready(
    {
      createSession: () => {
        creates += 1
        return response.promise
      },
      sendMessage: async () => {
        sends += 1
        return {}
      },
    },
    null,
  )
  const first = store.sendPrompt("first")
  const second = store.sendPrompt("second")
  response.resolve(session("new"))
  expect(await first).toBe(true)
  expect(await second).toBe(false)
  expect(creates).toBe(1)
  expect(sends).toBe(1)
})

test("prompt rejected by the daemon returns false and preserves the draft", async () => {
  const store = ready({
    sendMessage: async () => {
      throw new ApiError("already running", 409, "run_conflict")
    },
  })
  store.promptText("keep this draft")
  expect(await store.sendPrompt("keep this draft")).toBe(false)
  expect(store.promptText()).toBe("keep this draft")
  expect(store.messages()).toEqual([])
  expect(store.running()).toBe(true)
})

test("a pending title cannot restore the source session or submit after navigation", async () => {
  const response = deferred<Session>()
  let sends = 0
  const store = ready(
    {
      updateSession: () => response.promise,
      sendMessage: async () => {
        sends += 1
      },
    },
    session("a", { title: null }),
  )
  const sending = store.sendPrompt("from a")
  await store.openSession("b")
  store.promptText("b draft")
  response.resolve(session("a", { title: "from a" }))
  expect(await sending).toBe(false)
  expect(store.session()?.id).toBe("b")
  expect(store.promptText()).toBe("b draft")
  expect(sends).toBe(0)
})

test("late send rejection cannot remove a new session's messages or running state", async () => {
  const response = deferred<never>()
  const store = ready({ sendMessage: () => response.promise })
  const sending = store.sendPrompt("from a")
  await store.openSession("b")
  store.running(true)
  event(store, 1, "message.start", { message_id: "b-message", role: "assistant" })
  response.reject(new Error("connection lost"))
  expect(await sending).toBe(false)
  expect(store.session()?.id).toBe("b")
  expect(store.running()).toBe(true)
  expect(store.messages().map((message) => message.id)).toEqual(["b-message"])
})

test("custom command returns the admission result and does not cross navigation during reload", async () => {
  const store = ready({
    sendMessage: async () => {
      throw new ApiError("conflict", 409, "run_conflict")
    },
  })
  store.customCommands([{ name: "fixture", body: "body", description: "" }])
  expect(await store.runCustomCommand("fixture", "")).toBe(false)

  const response = deferred<{
    commands: Array<{ name: string; body: string; description: string }>
  }>()
  const other = ready({
    commands: (cwd: string) => (cwd.endsWith("/a") ? response.promise : { commands: [] }),
  })
  const running = other.runCustomCommand("fixture", "")
  await other.openSession("b")
  response.resolve({ commands: [{ name: "fixture", body: "body", description: "" }] })
  expect(await running).toBe(false)
  expect(other.messages()).toEqual([])
})

test("failed abort does not finish a run or tool that may still be executing", async () => {
  const store = ready({
    abort: async () => {
      throw new Error("connection lost")
    },
  })
  store.running(true)
  event(store, 1, "message.start", { message_id: "m", role: "assistant" })
  event(store, 2, "tool.start", { message_id: "m", tool_call_id: "call", name: "bash" })
  await store.abort()
  expect(store.running()).toBe(true)
  expect(store.messages()[0]?.streaming).toBe(true)
  expect(store.messages()[0]?.toolCalls[0]?.status).toBe("running")
})

test("successful abort waits for a final event before declaring completion", async () => {
  const store = ready({ abort: async () => ({ ok: true }) })
  store.running(true)
  await store.abort()
  expect(store.running()).toBe(true)
  event(store, 1, "run.end", { reason: "aborted" })
  expect(store.running()).toBe(false)
})

test("abort waits for in-flight admission and coalesces repeated presses", async () => {
  const admission = deferred<{ status: "running"; run_id: string }>()
  let abortRequests = 0
  const store = ready({
    sendMessage: () => admission.promise,
    abort: async () => {
      abortRequests += 1
      return { ok: true }
    },
  })
  const submitted = store.sendPrompt("start working")
  expect(store.running()).toBe(true)
  const aborted = store.abort()
  await store.abort()
  expect(abortRequests).toBe(0)
  admission.resolve({ status: "running", run_id: "run" })
  expect(await submitted).toBe(true)
  await aborted
  expect(abortRequests).toBe(1)
  event(store, 1, "run.end", { reason: "aborted" })
  expect(store.running()).toBe(false)
})

test("duplicate event sequences and ids are applied only once", () => {
  const store = ready()
  event(store, 1, "message.start", { message_id: "m", role: "assistant" })
  event(store, 2, "message.delta", { message_id: "m", text: "one" })
  event(store, 2, "message.delta", { message_id: "m", text: "one" })
  internal(store).handleEvent({
    seq: 3,
    id: "event-2",
    session_id: "a",
    type: "message.delta",
    data: { message_id: "m", text: "one" },
  })
  internal(store).flushStreams()
  expect(store.messages()[0]?.content).toBe("one")
})

test("an event from another session cannot mutate or advance the active cursor", () => {
  const store = ready()
  internal(store).handleEvent({
    seq: 100,
    id: "foreign",
    session_id: "b",
    type: "message.delta",
    data: { message_id: "foreign", text: "wrong session" },
  })
  event(store, 1, "message.delta", { message_id: "a", text: "right session" })
  expect(store.messages().map((message) => message.content)).toEqual(["right session"])
})

test("late stream callbacks and frames cannot mutate a replacement session", async () => {
  const gate = deferred<void>()
  const done = deferred<void>()
  const store = ready({
    streamEvents: async function* (
      _id: string,
      _after: number,
      _signal: AbortSignal,
      onOpen: () => void,
    ) {
      await gate.promise
      onOpen()
      try {
        yield {
          seq: 1,
          id: "late",
          session_id: "a",
          type: "message.delta",
          data: { message_id: "a", text: "late text" },
        }
      } finally {
        done.resolve()
      }
    },
  })
  const streaming = internal(store).streamLoop("a", new AbortController().signal)
  await store.openSession("b")
  store.runtimeError("b error")
  store.connection("offline")
  gate.resolve()
  await done.promise
  await streaming
  expect(store.runtimeError()).toBe("b error")
  expect(store.connection()).toBe("offline")
  expect(store.messages()).toEqual([])
})

test("an expired SSE cursor rehydrates rather than retrying forever", async () => {
  let hydrations = 0
  const store = ready({
    streamEvents: async function* () {
      yield await Promise.reject(new ApiError("cursor expired", 409, "http_409"))
    },
    session: async (id: string) => {
      hydrations += 1
      return session(id, { last_event_seq: 7 })
    },
  })
  store.promptText("draft survives resync")
  await internal(store).streamLoop("a", new AbortController().signal)
  expect(hydrations).toBe(1)
  expect(store.session()?.id).toBe("a")
  expect(store.promptText()).toBe("draft survives resync")
})

test("replayed tool.start upserts a hydrated tool without resetting its result", () => {
  const store = ready()
  store.messages(
    hydrateMessages([
      {
        id: "m",
        seq: 1,
        role: "assistant",
        content: "",
        created_at: "2026-01-01T00:00:00Z",
        tool_calls: [{ id: "call", name: "read_file", arguments: {}, arguments_valid: true }],
        tool_outputs: { call: "persisted output" },
      },
    ]),
  )
  event(store, 1, "tool.start", { message_id: "m", tool_call_id: "call", name: "read_file" })
  expect(store.messages()[0]?.toolCalls).toHaveLength(1)
  expect(store.messages()[0]?.toolCalls[0]?.output).toBe("persisted output")
  expect(store.messages()[0]?.toolCalls[0]?.status).toBe("ok")
})

test("run.end clears obsolete permissions and refreshes completion files", async () => {
  let files = ["before.txt"]
  const limits: number[] = []
  const store = ready({
    files: async (_id: string, limit: number) => {
      limits.push(limit)
      return { files }
    },
  })
  await store.loadFiles()
  event(store, 1, "permission.request", { request_id: "old" })
  files = ["after.txt"]
  event(store, 2, "run.end", { reason: "completed" })
  await Promise.resolve()
  expect(store.permission()).toBeNull()
  expect(store.files()).toEqual(["after.txt"])
  expect(limits).toEqual([5000, 5000])
})

test("the daemon queue rehydrates per session and ignores older queue events", async () => {
  const item = { id: "queued_a", preview: "Continue later", attachment_count: 1 }
  const store = ready({
    session: async (id: string) =>
      session(id, {
        last_event_seq: 3,
        prompt_queue: { event_seq: 8, items: id === "a" ? [item] : [] },
      }),
    removeQueuedPrompt: async () => ({ id: item.id, content: item.preview, attachments: [] }),
  })
  await store.openSession("a")
  expect(store.queuedPrompts()).toEqual([{ id: item.id, text: item.preview, attachmentCount: 1 }])
  event(store, 5, "queue.changed", { items: [] })
  expect(store.queuedPrompts()).toHaveLength(1)
  await store.openSession("b")
  expect(store.queuedPrompts()).toEqual([])
  await store.openSession("a")
  expect(store.queuedPrompts()).toHaveLength(1)
  expect((await store.removeQueuedPrompt(item.id))?.content).toBe(item.preview)
  event(store, 9, "queue.changed", { items: [] })
  event(store, 10, "message.start", {
    role: "user",
    message_id: "started-b",
    queued_prompt_id: "queued_b",
    content: "Queued from another tab",
  })
  expect(store.queuedPrompts()).toEqual([])
  expect(store.messages()).toMatchObject([{ id: "started-b", content: "Queued from another tab" }])
})

test("late corrections are visible after hydration and are not duplicated by replay", async () => {
  const correction = {
    id: "correction-a",
    seq: 2,
    role: "user",
    delivery: "unapplied",
    content: "Use the other fixture.",
    created_at: "2026-01-01T00:00:00Z",
  }
  const store = ready({ messages: async () => ({ messages: [correction] }) })
  await store.openSession("a")
  event(store, 1, "steering.dropped", { message_id: correction.id, content: correction.content })
  expect(store.messages()).toMatchObject([
    { id: correction.id, content: correction.content, unapplied: true },
  ])
})

test("a successful sign-in survives a provider catalog error", async () => {
  const store = ready({
    authLogin: async () => ({ ok: true }),
    authStatus: async () => ({
      providers: [
        {
          provider: "chatgpt",
          scheme: "bearer",
          env: null,
          env_set: false,
          file_set: true,
          browser_login: true,
          browser_method: "chatgpt",
        },
      ],
    }),
    models: async () => ({
      providers: [{ provider: "chatgpt", models: [], error: "catalog unavailable" }],
    }),
  })
  await expect(store.login("chatgpt", "secret")).resolves.toBeUndefined()
  expect(store.toasts().some((toast) => toast.text.includes("catalog unavailable"))).toBe(true)
})

test("opening a session restores tool activity and older replay cannot roll it back", async () => {
  const child = { task_call_id: "delegate", session_id: "child", subagent: "explore" }
  const job = { tool_call_id: "shell", id: "job", status: "running" }
  const store = ready({
    session: async () =>
      session("a", {
        last_event_seq: 3,
        metadata: {
          tool_activity: [
            { seq: 8, type: "job.progress", data: { ...job, lines_read: 10 } },
            { seq: 9, type: "subagent.end", data: { ...child, status: "completed" } },
          ],
        },
      }),
    messages: async () => ({
      messages: [
        {
          id: "answer",
          role: "assistant",
          content: "",
          created_at: "",
          tool_calls: [
            { id: "shell", name: "bash", arguments: {} },
            { id: "delegate", name: "task", arguments: {} },
          ],
          tool_outputs: { shell: "started job", delegate: "done" },
        },
      ],
    }),
  })

  await store.openSession("a")
  const tools = () => store.messages()[0].toolCalls
  expect(tools()[0].backgroundJob?.lines).toBe(10)
  expect(tools()[1].children?.[0].status).toBe("completed")

  event(store, 4, "job.started", { ...job, lines_read: 0 })
  event(store, 5, "subagent.start", child)
  expect(tools()[0].backgroundJob?.lines).toBe(10)
  expect(tools()[1].children?.[0].status).toBe("completed")

  event(store, 10, "job.completed", { ...job, status: "completed", returncode: 0, lines_read: 12 })
  expect(tools()[0].backgroundJob?.status).toBe("completed")
  expect(tools()[0].backgroundJob?.lines).toBe(12)
})
