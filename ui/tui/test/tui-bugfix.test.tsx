import { expect, test } from "bun:test"
import { type BoxRenderable, type Renderable, TextRenderable } from "@opentui/core"
import { testRender } from "@opentui/solid"
import { TuiStore, type UiMessage } from "../src/state"
import { resolveTheme } from "../src/theme"
import { App } from "../src/ui/App"
import { setActiveStore, setActiveTheme } from "../src/ui/context"
import { ToolCard } from "../src/ui/ToolCard"

function readyStore() {
  const store = new TuiStore()
  store.theme(resolveTheme("siwah-dark", false))
  store.models({ providers: [], roles: { main: "mock/model-a" } })
  store.auth({ providers: [] })
  store.connection("online")
  store.session({
    id: "bugfix",
    title: "Benchmark fixes",
    cwd: "D:/work/siwah",
    model: "mock/model-a",
    mode: "build",
    status: "idle",
    created_at: "",
    updated_at: "",
    metadata: {},
    last_event_seq: 0,
  })
  setActiveStore(store)
  setActiveTheme(store.theme())
  return store
}

function nodes(root: Renderable): Renderable[] {
  return root.getChildren().flatMap((child) => [child, ...nodes(child)])
}

const answer = (overrides: Partial<UiMessage> = {}): UiMessage => ({
  id: "answer",
  role: "assistant",
  content: "The requested answer.",
  reasoning: "",
  streaming: false,
  createdAt: "",
  toolCalls: [],
  diffs: [],
  reasoningOpen: false,
  ...overrides,
})

function internal(store: TuiStore) {
  return store as unknown as {
    handleEvent: (event: Record<string, unknown>) => void
  }
}

test("an unfinished run leaves a durable transcript notice after toasts disappear", async () => {
  const store = readyStore()
  store.messages([answer({ content: "", streaming: true })])
  store.running(true)
  const setup = await testRender(() => <App store={store} initialize={false} />, {
    width: 100,
    height: 30,
  })
  try {
    await setup.renderOnce()
    const message = "No tool call succeeded for 3 consecutive steps; stopping."
    internal(store).handleEvent({
      seq: 10,
      type: "error",
      data: { code: "stalled", message, retryable: false },
    })
    internal(store).handleEvent({ seq: 11, type: "run.end", data: { reason: "stalled" } })
    // Simulate the short-lived toast expiring; the transcript notice remains.
    store.toasts([])
    await setup.renderOnce()
    expect(store.running()).toBe(false)
    expect(setup.captureCharFrame()).toContain("Run failed")
    expect(setup.captureCharFrame()).toContain(message)
    expect(setup.captureCharFrame()).not.toContain("working ·")
  } finally {
    setup.renderer.destroy()
  }
})

test("a run ending at the step limit explains itself without an error event", async () => {
  const store = readyStore()
  store.messages([answer({ content: "Partial answer" })])
  store.running(true)
  const setup = await testRender(() => <App store={store} initialize={false} />, {
    width: 100,
    height: 30,
  })
  try {
    await setup.renderOnce()
    internal(store).handleEvent({ seq: 4, type: "run.end", data: { reason: "max_steps" } })
    await setup.renderOnce()
    expect(store.runNotice()?.title).toBe("Step limit reached")
    expect(setup.captureCharFrame()).toContain("Step limit reached")
    expect(setup.captureCharFrame()).toContain("step limit was reached")
  } finally {
    setup.renderer.destroy()
  }
})

test("a completed run does not add a warning", async () => {
  const store = readyStore()
  store.messages([answer({ content: "Done" })])
  store.running(true)
  const setup = await testRender(() => <App store={store} initialize={false} />, {
    width: 100,
    height: 24,
  })
  try {
    await setup.renderOnce()
    internal(store).handleEvent({ seq: 4, type: "run.end", data: { reason: "completed" } })
    await setup.renderOnce()
    expect(store.runNotice()).toBeNull()
    expect(setup.captureCharFrame()).not.toContain("Run ended early")
  } finally {
    setup.renderer.destroy()
  }
})

test("provider backoff is visible in the activity line and clears on stream start", async () => {
  const store = readyStore()
  store.messages([answer({ content: "" })])
  store.running(true)
  const setup = await testRender(() => <App store={store} initialize={false} />, {
    width: 100,
    height: 24,
  })
  try {
    await setup.renderOnce()
    internal(store).handleEvent({
      seq: 2,
      type: "provider.retry",
      data: {
        attempt: 1,
        max_attempts: 3,
        delay_s: 1.2,
        model: "model-a",
        code: "provider_rate_limited",
        message: "rate limited",
      },
    })
    await setup.renderOnce()
    const frame = setup.captureCharFrame()
    expect(frame).toContain("retrying in 1.2s")
    expect(frame).toContain("attempt 1/3")
    expect(frame).toContain("provider rate limited")
    internal(store).handleEvent({
      seq: 3,
      type: "message.start",
      data: { role: "assistant", message_id: "answer" },
    })
    await setup.renderOnce()
    expect(setup.captureCharFrame()).not.toContain("retrying in")
  } finally {
    setup.renderer.destroy()
  }
})

test("tasks use one row each and align with the composer's right edge", async () => {
  const store = readyStore()
  store.todos(
    Array.from({ length: 10 }, (_, index) => ({
      id: `task-${index + 1}`,
      content: `G${index + 1}: PASS [${index + 12}/${index + 12}]`,
      status: index < 8 ? "done" : "in_progress",
    })),
  )
  store.messages([answer({ content: "Working" })])
  const setup = await testRender(() => <App store={store} initialize={false} />, {
    width: 120,
    height: 36,
  })
  try {
    await setup.renderOnce()
    const panel = setup.renderer.root.findDescendantById("todo-panel") as BoxRenderable | undefined
    const composer = setup.renderer.root.findDescendantById("composer-panel") as
      | BoxRenderable
      | undefined
    expect(panel).toBeDefined()
    expect(composer).toBeDefined()
    if (!panel || !composer) throw new Error("Task or composer panel is missing")
    expect(panel.x + panel.width).toBe(composer.x + composer.width)
    for (let index = 1; index <= 10; index += 1) {
      const row = setup.renderer.root.findDescendantById(`task-row-task-${index}`) as
        | BoxRenderable
        | undefined
      expect(row?.height).toBe(1)
    }
    const frame = setup.captureCharFrame()
    expect(frame).toContain("G1: PASS [12/12]")
    expect(frame).toContain("G10: PASS [21/21]")
    expect(frame).not.toMatch(/\b\d{2} · (done|in progress|blocked|pending)\b/)
  } finally {
    setup.renderer.destroy()
  }
})

test("long task labels stay on one row and are clipped by terminal cells", async () => {
  const store = readyStore()
  const label = `检查 👨‍👩‍👧 ${"very-long-task ".repeat(8)}`
  store.todos([{ id: "long", content: label, status: "in_progress" }])
  store.messages([answer({ content: "Working" })])
  const setup = await testRender(() => <App store={store} initialize={false} />, {
    width: 100,
    height: 24,
  })
  try {
    await setup.renderOnce()
    const row = setup.renderer.root.findDescendantById("task-row-long") as BoxRenderable | undefined
    expect(row?.height).toBe(1)
    expect(setup.captureCharFrame()).toContain("检查 👨‍👩‍👧")
  } finally {
    setup.renderer.destroy()
  }
})

test("a long transcript shares one resize subscription", async () => {
  const store = readyStore()
  store.messages(
    Array.from({ length: 40 }, (_, index) =>
      answer({
        id: `m${index}`,
        content: `answer ${index}`,
        toolCalls: [
          {
            id: `tool-${index}`,
            name: index % 2 === 0 ? "read_file" : "bash",
            summary: index % 2 === 0 ? '{"path":"main.py"}' : '{"command":"echo hi"}',
            output: "line",
            status: "ok",
            durationMs: 1,
            error: null,
          },
        ],
      }),
    ),
  )
  const setup = await testRender(() => <App store={store} initialize={false} />, {
    width: 100,
    height: 32,
  })
  try {
    await setup.renderOnce()
    const renderer = setup.renderer as unknown as { listenerCount: (event: string) => number }
    const count = renderer.listenerCount("resize")
    expect(count).toBeGreaterThan(0)
    expect(count).toBeLessThanOrEqual(4)
    await setup.resize(80, 28)
    await setup.renderOnce()
    expect(renderer.listenerCount("resize")).toBeLessThanOrEqual(4)
  } finally {
    setup.renderer.destroy()
  }
})

test("an expanded edit diff has exactly one disclosure footer", async () => {
  readyStore()
  const patch = [
    "--- a/long.py",
    "+++ b/long.py",
    "@@ -1,8 +1,8 @@",
    ...Array.from({ length: 8 }, (_, index) => `-old line ${index}`),
    ...Array.from({ length: 8 }, (_, index) => `+new line ${index}`),
  ].join("\n")
  const setup = await testRender(
    () => (
      <ToolCard
        tool={{
          id: "edit",
          name: "edit_file",
          summary: '{"path":"long.py"}',
          output: "",
          status: "ok",
          durationMs: 10,
          error: null,
        }}
        diff={{ path: "long.py", patch }}
      />
    ),
    { width: 100, height: 30 },
  )
  try {
    await setup.renderOnce()
    const collapsed = setup.captureCharFrame()
    expect(collapsed.match(/click to expand/g)).toHaveLength(1)
    expect(collapsed).not.toContain("click to collapse (")
    await setup.mockMouse.click(3, 1)
    await setup.renderOnce()
    const expanded = setup.captureCharFrame()
    expect(expanded.match(/click to collapse/g)).toHaveLength(1)
    expect(expanded).not.toContain("click to expand")
    expect(
      nodes(setup.renderer.root).some(
        (node) => node instanceof TextRenderable && node.plainText.includes("new line 7"),
      ),
    ).toBe(true)
  } finally {
    setup.renderer.destroy()
  }
})
