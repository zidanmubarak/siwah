import { expect, test } from "bun:test"
import { type Renderable, ScrollBoxRenderable, TextRenderable } from "@opentui/core"
import { testRender } from "@opentui/solid"
import { createSignal } from "solid-js"
import { TuiStore, type UiMessage } from "../src/state"
import { resolveTheme } from "../src/theme"
import { App } from "../src/ui/App"
import { setActiveStore, setActiveTheme } from "../src/ui/context"
import { DiffView, parseUnifiedDiff, splitDiffRows } from "../src/ui/DiffView"
import { MessageView } from "../src/ui/MessageView"
import { clipCells } from "../src/util"

function readyStore() {
  const store = new TuiStore()
  store.theme(resolveTheme("mono", false))
  store.models({ providers: [], roles: { main: "mock/model-a" } })
  store.auth({ providers: [] })
  store.connection("online")
  store.session({
    id: "ses_redesign",
    title: "Redesign",
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

function message(overrides: Partial<UiMessage> = {}): UiMessage {
  return {
    id: "answer",
    role: "assistant",
    content: "",
    reasoning: "",
    streaming: true,
    createdAt: "",
    toolCalls: [],
    diffs: [],
    reasoningOpen: false,
    ...overrides,
  }
}

function nodes(root: Renderable): Renderable[] {
  return root.getChildren().flatMap((child) => [child, ...nodes(child)])
}

const settle = () => new Promise<void>((resolve) => setTimeout(resolve, 20))

test("streamed paragraphs and code retain renderable identity through the final delta", async () => {
  readyStore()
  const [value, setValue] = createSignal(
    message({ content: "A stable paragraph\n```py\nprint('first')" }),
  )
  const setup = await testRender(() => <MessageView message={value()} />, {
    width: 100,
    height: 25,
  })
  try {
    await setup.renderOnce()
    const text = (prefix: string) =>
      nodes(setup.renderer.root).find(
        (node) => node instanceof TextRenderable && node.plainText.startsWith(prefix),
      )
    const paragraph = text("A stable")
    const code = text("print(")
    expect(paragraph).toBeDefined()
    expect(code).toBeDefined()
    for (const suffix of [" with more", " with more words", " with more words finally"]) {
      setValue(
        message({ content: `A stable paragraph${suffix}\n\`\`\`py\nprint('first plus last')` }),
      )
      await setup.renderOnce()
      expect(text("A stable")).toBe(paragraph)
      expect(text("print(")).toBe(code)
    }
    setValue((current) => ({ ...current, streaming: false, content: `${current.content}\n\`\`\`` }))
    await setup.renderOnce()
    expect(text("A stable")).toBe(paragraph)
    expect(text("print(")).toBe(code)
    expect(setup.captureCharFrame()).toContain("first plus last")
    expect(setup.captureCharFrame()).not.toContain("responding")
    expect(setup.captureCharFrame()).not.toContain("▍")
  } finally {
    setup.renderer.destroy()
  }
})

test("welcome uses the supplied silhouette and compact blade line", async () => {
  const store = readyStore()
  const setup = await testRender(() => <App store={store} initialize={false} />, {
    width: 100,
    height: 32,
  })
  try {
    await setup.renderOnce()
    expect(setup.captureCharFrame()).toContain("▄▄▄▄▄▄▄▄▄██▀")
    expect(setup.captureCharFrame()).toContain("███▄▄▄▄▄▄█████████████████▀▀")
    expect(setup.captureCharFrame()).toContain("a coding agent")
    await setup.resize(60, 20)
    await setup.renderOnce()
    expect(setup.captureCharFrame()).toContain("◆━━━━━━━━━━━━━━━━◥")
    expect(setup.captureCharFrame()).toContain("Ask siwah")
  } finally {
    setup.renderer.destroy()
  }
})

test("streaming beyond 240 lines retains the prefix and does not restore it on completion", async () => {
  readyStore()
  const [value, setValue] = createSignal(
    message({
      content: Array.from({ length: 320 }, (_, index) => `answer-line-${index}`).join("\n"),
    }),
  )
  const setup = await testRender(() => <MessageView message={value()} />, { width: 80, height: 20 })
  try {
    await setup.renderOnce()
    const first = nodes(setup.renderer.root).find(
      (node) => node instanceof TextRenderable && node.plainText === "answer-line-0",
    )
    expect(first).toBeDefined()
    if (!first) throw new Error("Streaming prefix was not mounted")
    expect(setup.captureCharFrame()).toContain("answer-line-0")
    setValue((current) => ({ ...current, streaming: false }))
    await setup.renderOnce()
    expect(nodes(setup.renderer.root)).toContain(first)
    expect(setup.captureCharFrame()).toContain("answer-line-0")
  } finally {
    setup.renderer.destroy()
  }
})

test("tool progress preserves card identity, expansion and readable terminal statuses", async () => {
  const store = readyStore()
  const tool = {
    id: "call",
    name: "bash",
    summary: '{"command":"test"}',
    output: "1\n2\n3\n4\n5",
    status: "running" as const,
    durationMs: null,
    error: null,
  }
  store.messages([message({ toolCalls: [tool] })])
  const setup = await testRender(() => <App store={store} initialize={false} />, {
    width: 100,
    height: 32,
  })
  try {
    await setup.renderOnce()
    const card = setup.renderer.root.findDescendantById("tool-call")
    expect(card).toBeDefined()
    store.toggleTool("call")
    for (const status of ["running", "ok", "error", "cancelled"] as const) {
      store.messages([
        message({
          toolCalls: [{ ...tool, status, durationMs: 1500, output: `${tool.output}\n6` }],
        }),
      ])
      await setup.renderOnce()
      expect(setup.renderer.root.findDescendantById("tool-call")).toBe(card)
      expect(store.isToolExpanded("call")).toBe(true)
      expect(setup.captureCharFrame()).toContain(status === "ok" ? "done" : status)
      if (status !== "running") expect(setup.captureCharFrame()).toContain("1.5s")
    }
  } finally {
    setup.renderer.destroy()
  }
})

test("manual scroll owns the viewport through delta, completion, and explicit jump", async () => {
  const store = readyStore()
  store.messages([
    message({ content: Array.from({ length: 100 }, (_, i) => `history_${i}`).join("\n") }),
  ])
  store.running(true)
  const setup = await testRender(() => <App store={store} initialize={false} />, {
    width: 100,
    height: 25,
  })
  try {
    await setup.renderOnce()
    await setup.renderOnce()
    const scroll = setup.renderer.root.findDescendantById("transcript")
    expect(scroll).toBeInstanceOf(ScrollBoxRenderable)
    const transcript = scroll as ScrollBoxRenderable
    transcript.scrollTo(Infinity)
    await setup.renderOnce()
    expect(setup.captureCharFrame()).not.toContain("↓ latest")
    setup.mockInput.typeText("keep draft")
    setup.mockInput.pressKey("\x1b[5~")
    await setup.renderOnce()
    expect(setup.captureCharFrame()).toContain("↓ latest")
    const offset = transcript.scrollTop
    store.messages((current) =>
      current.map((item) => ({ ...item, content: `${item.content}\nlast_delta` })),
    )
    await setup.renderOnce()
    expect(transcript.scrollTop).toBe(offset)
    const before = store.scrollTick()
    ;(store as unknown as { handleEvent: (event: unknown) => void }).handleEvent({
      seq: 1,
      type: "run.end",
      data: { reason: "completed" },
    })
    await settle()
    await setup.renderOnce()
    expect(store.scrollTick()).toBe(before)
    expect(transcript.scrollTop).toBe(offset)
    expect(store.promptText()).toBe("keep draft")
    setup.mockInput.pressKey("END", { ctrl: true })
    await settle()
    await setup.renderOnce()
    expect(transcript.scrollTop + transcript.verticalScrollBar.viewportSize).toBeGreaterThanOrEqual(
      transcript.scrollHeight - 1,
    )
    expect(setup.captureCharFrame()).not.toContain("↓ latest")
    expect(store.promptText()).toBe("keep draft")
    expect(setup.captureCharFrame()).toContain("last_delta")
  } finally {
    setup.renderer.destroy()
  }
})

test("permission and dialogs keep transcript navigation out of their keyboard scope", async () => {
  const store = readyStore()
  store.messages([message({ content: "line\n".repeat(100) })])
  const setup = await testRender(() => <App store={store} initialize={false} />, {
    width: 100,
    height: 25,
  })
  try {
    await setup.renderOnce()
    const scroll = setup.renderer.root.findDescendantById("transcript") as ScrollBoxRenderable
    scroll.scrollTo(Infinity)
    await setup.renderOnce()
    const before = scroll.scrollTop
    store.permission({
      requestId: "permission",
      tool: "bash",
      kind: "bash",
      preview: "test",
      choices: ["once", "deny"],
    })
    await setup.renderOnce()
    const permissionOffset = scroll.scrollTop
    setup.mockInput.pressKey("\x1b[5~")
    await setup.renderOnce()
    expect(scroll.scrollTop).toBe(permissionOffset)
    store.openDialog({ type: "help" })
    await setup.renderOnce()
    setup.mockInput.pressKey("\x1b[5~")
    await setup.renderOnce()
    expect(scroll.scrollTop).toBe(permissionOffset)
    expect(before).toBeGreaterThan(0)
  } finally {
    setup.renderer.destroy()
  }
})

test("the fixed activity line explains reasoning, writing, tools and idle", async () => {
  const store = readyStore()
  store.running(true)
  store.messages([message()])
  const setup = await testRender(() => <App store={store} initialize={false} />, {
    width: 100,
    height: 25,
  })
  try {
    await setup.renderOnce()
    expect(setup.captureCharFrame()).toContain("waiting for model")
    store.messages([message({ reasoning: "private reasoning" })])
    await setup.renderOnce()
    expect(setup.captureCharFrame()).toContain("working · thinking")
    expect(setup.captureCharFrame()).not.toContain("private reasoning")
    store.messages([message({ content: "answer", reasoning: "private reasoning" })])
    await setup.renderOnce()
    expect(setup.captureCharFrame()).toContain("writing response")
    store.running(false)
    await setup.renderOnce()
    expect(setup.captureCharFrame()).not.toContain("Ready")
    expect(setup.captureCharFrame()).not.toContain("working ·")
  } finally {
    setup.renderer.destroy()
  }
})

test("cell clipping preserves graphemes and budgets CJK/emoji by terminal width", () => {
  for (const value of ["界面/非常长的目录/文件.ts", "👨‍👩‍👧 family/e\u0301/source.ts"]) {
    for (let width = 1; width < 25; width += 1) {
      for (const tail of [false, true]) {
        const clipped = clipCells(value, width, tail)
        expect(Bun.stringWidth(clipped)).toBeLessThanOrEqual(width)
        expect(clipped).not.toMatch(/[\uD800-\uDBFF]$/)
        expect(clipped).not.toMatch(/^[\uDC00-\uDFFF]/)
      }
    }
  }
  expect(clipCells("a very long/file.ts", 8, true)).toBe("…file.ts")
  expect(clipCells("none", 0)).toBe("")
})

test("resize with long Unicode metadata retains the draft and critical footer fields", async () => {
  const store = readyStore()
  store.session((current) =>
    current
      ? {
          ...current,
          title: "修复 👨‍👩‍👧 界面",
          cwd: "D:/界面/非常长的目录/非常长的目录/workspace",
          model: "mock/非常长的模型名字非常长的模型名字",
        }
      : null,
  )
  store.connection("reconnecting")
  const setup = await testRender(() => <App store={store} initialize={false} />, {
    width: 120,
    height: 34,
  })
  try {
    await setup.renderOnce()
    setup.mockInput.typeText("preserve this draft")
    for (const width of [60, 120, 60]) {
      await setup.resize(width, 25)
      await setup.renderOnce()
      const frame = setup.captureCharFrame()
      expect(frame).toContain("preserve this draft")
      expect(frame).toContain("reconnecting")
      expect(frame).toContain("$0.000000")
      expect(frame).toContain("ctx")
      expect(store.promptText()).toBe("preserve this draft")
    }
  } finally {
    setup.renderer.destroy()
  }
})

test("keyboard tool disclosure works without losing the draft or bypassing permission", async () => {
  const store = readyStore()
  store.messages([
    message({
      toolCalls: [
        {
          id: "write",
          name: "write_file",
          summary: '{"path":"new.py"}',
          status: "ok",
          output: "first\nsecond\nthird\nfourth\nfifth\nlast",
          durationMs: 10,
          error: null,
        },
      ],
    }),
  ])
  const setup = await testRender(() => <App store={store} initialize={false} />, {
    width: 100,
    height: 32,
  })
  try {
    await setup.renderOnce()
    expect(setup.captureCharFrame()).not.toContain("fifth")
    setup.mockInput.typeText("retain me")
    setup.mockInput.pressKey("o", { ctrl: true })
    await setup.renderOnce()
    expect(store.isToolExpanded("write")).toBe(true)
    expect(setup.captureCharFrame()).toContain("fifth")
    expect(store.promptText()).toBe("retain me")
    setup.mockInput.pressKey("o", { ctrl: true })
    await setup.renderOnce()
    expect(store.isToolExpanded("write")).toBe(false)
    store.permission({
      requestId: "permission",
      tool: "bash",
      kind: "bash",
      preview: "test",
      choices: ["once", "deny"],
    })
    await setup.renderOnce()
    const decisions: string[] = []
    store.resolvePermission = async (decision) => {
      decisions.push(decision)
    }
    setup.mockInput.pressKey("o", { ctrl: true })
    await setup.renderOnce()
    expect(store.isToolExpanded("write")).toBe(false)
    expect(store.promptText()).toBe("retain me")
    expect(decisions).toEqual([])
    setup.mockInput.typeText("o")
    expect(decisions).toEqual(["once"])
  } finally {
    setup.renderer.destroy()
  }
})

test("split diff pairs unequal replacements without losing signs, line numbers or metadata", () => {
  const rows = parseUnifiedDiff(
    "--- a/x\n+++ b/x\n@@ -3,3 +3,2 @@\n-old one\n-old two\n-old three\n+new one\n+new two\n\\ No newline at end of file",
  )
  const split = splitDiffRows(rows)
  expect(split.filter((row) => row.left?.kind === "remove").map((row) => row.left?.text)).toEqual([
    "old one",
    "old two",
    "old three",
  ])
  expect(split.filter((row) => row.right?.kind === "add").map((row) => row.right?.text)).toEqual([
    "new one",
    "new two",
  ])
  expect(split.at(-2)?.left?.oldNumber).toBe(5)
  expect(split.at(-2)?.right).toBeUndefined()
  expect(split.at(-1)?.meta?.text).toContain("No newline")
})

test("diff changes between split and unified on resize, retaining content", async () => {
  readyStore()
  const setup = await testRender(
    () => <DiffView path="src/demo.py" patch={"@@ -1 +1 @@\n-before_value\n+after_value"} />,
    { width: 140, height: 15 },
  )
  try {
    await setup.renderOnce()
    expect(setup.captureCharFrame()).toContain("Before")
    expect(setup.captureCharFrame()).toContain("After")
    expect(setup.captureCharFrame()).toContain("split")
    expect(setup.captureCharFrame()).toContain("- before_value")
    expect(setup.captureCharFrame()).toContain("+ after_value")
    await setup.resize(60, 15)
    await setup.renderOnce()
    expect(setup.captureCharFrame()).toContain("unified")
    expect(setup.captureCharFrame()).toContain("- before_value")
    expect(setup.captureCharFrame()).toContain("+ after_value")
    await setup.resize(140, 15)
    await setup.renderOnce()
    expect(setup.captureCharFrame()).toContain("split")
  } finally {
    setup.renderer.destroy()
  }
})

test("denial editor captures a reason, ignores modified approval aliases and respects modal ownership", async () => {
  const store = readyStore()
  store.permission({
    requestId: "p1",
    tool: "bash",
    kind: "bash",
    preview: "$ command",
    choices: ["once", "deny"],
  })
  const decisions: Array<[string, string | undefined]> = []
  store.resolvePermission = async (decision, reason) => {
    decisions.push([decision, reason])
  }
  const setup = await testRender(() => <App store={store} initialize={false} />, {
    width: 80,
    height: 28,
  })
  try {
    await setup.renderOnce()
    expect(setup.captureCharFrame()).not.toContain("allow session")
    setup.mockInput.pressKey("o", { ctrl: true })
    setup.mockInput.pressKey("s")
    expect(decisions).toEqual([])
    setup.mockInput.typeText("d")
    await setup.mockInput.pasteBracketedText("Keep My Files Safe")
    await setup.renderOnce()
    expect(setup.captureCharFrame()).toContain("Keep My Files Safe")
    expect(store.promptText()).toBe("")
    store.openDialog({ type: "help" })
    await setup.renderOnce()
    setup.mockInput.pressEnter()
    expect(decisions).toEqual([])
    store.closeDialog()
    await setup.renderOnce()
    setup.mockInput.pressEnter()
    expect(decisions).toEqual([["deny", "Keep My Files Safe"]])
    store.permission({
      requestId: "p2",
      tool: "bash",
      kind: "bash",
      preview: "$ next",
      choices: ["once", "deny"],
    })
    await setup.renderOnce()
    expect(setup.captureCharFrame()).not.toContain("Keep My Files Safe")
  } finally {
    setup.renderer.destroy()
  }
})

test("toast cards remain bounded above the composer and can be dismissed with the mouse", async () => {
  const store = readyStore()
  store.notify("A useful notification", "success")
  const setup = await testRender(() => <App store={store} initialize={false} />, {
    width: 60,
    height: 22,
  })
  try {
    await setup.renderOnce()
    const rows = setup.captureCharFrame().split("\n")
    expect(rows.join("\n")).toContain("SUCCESS")
    expect(rows.join("\n")).toContain("A useful notification")
    const panel = setup.renderer.root.findDescendantById("composer-panel")
    if (!panel) throw new Error("Composer panel is missing")
    expect(rows.slice(panel.y, panel.y + panel.height).join("\n")).toContain("Ask siwah")
    const toast = setup.renderer.root.findDescendantById(`toast-${store.toasts()[0]?.id}`)
    expect(toast).toBeDefined()
    if (toast) expect(toast.y + toast.height).toBeLessThanOrEqual(panel.y)
    const row = rows.findIndex((line) => line.includes("SUCCESS"))
    const column = rows[row]?.lastIndexOf("×") ?? -1
    expect(column).toBeGreaterThan(0)
    await setup.mockMouse.click(column, row)
    await setup.renderOnce()
    expect(store.toasts()).toHaveLength(0)
    expect(setup.captureCharFrame()).not.toContain("A useful notification")
  } finally {
    setup.renderer.destroy()
  }
})

test("task progress and blocked states remain accessible at 60 columns", async () => {
  const store = readyStore()
  store.todos([
    { id: "1", content: "Read implementation", status: "done" },
    { id: "2", content: "Run checks", status: "blocked" },
    { id: "3", content: "Apply fix", status: "in_progress" },
  ])
  const setup = await testRender(() => <App store={store} initialize={false} />, {
    width: 120,
    height: 32,
  })
  try {
    await setup.renderOnce()
    expect(setup.captureCharFrame()).toContain("33%")
    expect(setup.captureCharFrame()).toContain("✓ Read implementation")
    expect(setup.captureCharFrame()).toContain("! Run checks")
    expect(setup.captureCharFrame()).toContain("◐ Apply fix")
    await setup.resize(60, 24)
    await setup.renderOnce()
    expect(setup.captureCharFrame()).toContain("tasks 1/3")
    store.openDialog({ type: "todos" })
    await setup.renderOnce()
    expect(setup.captureCharFrame()).toContain("Run checks")
    expect(setup.captureCharFrame()).toContain("Apply fix")
    setup.mockInput.pressEscape()
    await settle()
    await setup.renderOnce()
    expect(store.dialog()).toBeNull()
  } finally {
    setup.renderer.destroy()
  }
})

test("help uses a scrollable dialog and PageDown reaches content below the fold", async () => {
  const store = readyStore()
  store.openDialog({ type: "help" })
  const setup = await testRender(() => <App store={store} initialize={false} />, {
    width: 60,
    height: 22,
  })
  try {
    await setup.renderOnce()
    const body = setup.renderer.root.findDescendantById("dialog-body") as ScrollBoxRenderable
    expect(body.scrollTop).toBe(0)
    setup.mockInput.pressKey("\x1b[6~")
    await setup.renderOnce()
    expect(body.scrollTop).toBeGreaterThan(0)
    setup.mockInput.pressKey("\x1b[5~")
    await setup.renderOnce()
    expect(body.scrollTop).toBe(0)
    expect(store.promptText()).toBe("")
  } finally {
    setup.renderer.destroy()
  }
})

test("full permission preview is read-only and cannot resolve a hidden request", async () => {
  const store = readyStore()
  const decisions: string[] = []
  store.resolvePermission = async (decision) => {
    decisions.push(decision)
  }
  store.permission({
    requestId: "long",
    tool: "bash",
    kind: "bash",
    choices: ["once", "deny"],
    preview: Array.from({ length: 100 }, (_, i) => `command-line-${i}`).join("\n"),
  })
  const setup = await testRender(() => <App store={store} initialize={false} />, {
    width: 100,
    height: 30,
  })
  try {
    await setup.renderOnce()
    setup.mockInput.typeText("v")
    await setup.renderOnce()
    expect(store.dialog()?.type).toBe("permission-preview")
    expect(setup.captureCharFrame()).toContain("read only")
    setup.mockInput.typeText("osad")
    expect(decisions).toEqual([])
    const body = setup.renderer.root.findDescendantById("dialog-body") as ScrollBoxRenderable
    body.scrollTo(Infinity)
    await setup.renderOnce()
    expect(setup.captureCharFrame()).toContain("command-line-99")
    setup.mockInput.pressEscape()
    await settle()
    await setup.renderOnce()
    expect(store.permission()?.requestId).toBe("long")
    expect(store.dialog()).toBeNull()
    expect(store.promptText()).toBe("")
  } finally {
    setup.renderer.destroy()
  }
})

test("keyboard tool disclosure also expands its associated edit diff", async () => {
  const store = readyStore()
  store.messages([
    message({
      toolCalls: [
        {
          id: "edit",
          name: "edit_file",
          summary: "file.py",
          status: "ok",
          output: "",
          error: null,
          durationMs: 3,
        },
      ],
      diffs: [{ path: "file.py", patch: "@@ -1,3 +1,3 @@\n keep_context\n-old\n+new\n keep_tail" }],
    }),
  ])
  const setup = await testRender(() => <App store={store} initialize={false} />, {
    width: 100,
    height: 28,
  })
  try {
    await setup.renderOnce()
    expect(setup.captureCharFrame()).not.toContain("keep_context")
    setup.mockInput.pressKey("o", { ctrl: true })
    await setup.renderOnce()
    expect(setup.captureCharFrame()).toContain("keep_context")
    expect(setup.captureCharFrame()).toContain("keep_tail")
    setup.mockInput.pressKey("o", { ctrl: true })
    await setup.renderOnce()
    expect(setup.captureCharFrame()).not.toContain("keep_context")
  } finally {
    setup.renderer.destroy()
  }
})

for (const surface of ["cost", "tasks", "toast", "split"] as const) {
  test(`${surface} redesigned surface snapshot`, async () => {
    const store = readyStore()
    if (surface === "cost") store.openDialog({ type: "cost" })
    if (surface === "tasks") {
      store.todos([
        { id: "1", content: "Inspect the implementation", status: "done" },
        { id: "2", content: "Apply the smallest fix", status: "in_progress" },
        { id: "3", content: "Run the verifier", status: "blocked" },
      ])
      store.openDialog({ type: "todos" })
    }
    if (surface === "toast") store.notify("Tests passed. The workspace is ready.", "success")
    if (surface === "split") {
      store.messages([
        message({
          streaming: false,
          content: "Implemented the requested fix.",
          diffs: [
            {
              path: "math_utils.py",
              patch: "@@ -1 +1 @@\n-return a - b\n+return a + b",
            },
          ],
        }),
      ])
    }
    const setup = await testRender(() => <App store={store} initialize={false} />, {
      width: surface === "split" ? 140 : 80,
      height: 28,
    })
    try {
      await setup.renderOnce()
      expect(setup.captureCharFrame()).toMatchSnapshot(`${surface}-workbench`)
    } finally {
      setup.renderer.destroy()
    }
  })
}

for (const type of [
  "palette",
  "models",
  "sessions",
  "help",
  "cost",
  "doctor",
  "evolution",
  "checkpoints",
  "rewind",
  "turns",
  "connect",
  "skills",
  "memory",
  "history",
  "effort",
  "theme",
  "mcp",
  "todos",
] as const) {
  test(`${type} dialog mounts and closes by keyboard at 60 columns without taking the draft`, async () => {
    const store = readyStore()
    const setup = await testRender(() => <App store={store} initialize={false} />, {
      width: 60,
      height: 24,
    })
    try {
      await setup.renderOnce()
      setup.mockInput.typeText("keep draft")
      store.openDialog({ type })
      await settle()
      await setup.renderOnce()
      expect(setup.captureCharFrame()).toContain("esc ×")
      expect(setup.captureCharFrame()).not.toContain("▄█▀")
      expect(store.dialog()?.type).toBe(type)
      setup.mockInput.pressEscape()
      await settle()
      await setup.renderOnce()
      expect(store.dialog()).toBeNull()
      expect(store.promptText()).toBe("keep draft")
    } finally {
      setup.renderer.destroy()
    }
  })
}
