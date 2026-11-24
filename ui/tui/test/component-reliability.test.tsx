import { expect, spyOn, test } from "bun:test"
import { mkdtempSync, rmSync, writeFileSync } from "node:fs"
import { join } from "node:path"
import {
  type Renderable,
  ScrollBoxRenderable,
  TextareaRenderable,
  TextRenderable,
} from "@opentui/core"
import { testRender } from "@opentui/solid"
import { createSignal } from "solid-js"

import { ApiError } from "../src/api/client"
import { type PromptImage, type ToolView, TuiStore, type UiMessage } from "../src/state"
import { resolveTheme } from "../src/theme"
import { App } from "../src/ui/App"
import { setActiveStore, setActiveTheme } from "../src/ui/context"
import { parseUnifiedDiff } from "../src/ui/DiffView"
import { MarkdownText } from "../src/ui/MarkdownText"
import { MessageView } from "../src/ui/MessageView"
import { PromptBox, type PromptHandle } from "../src/ui/PromptBox"
import { ToolCard } from "../src/ui/ToolCard"

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
  store.auth({
    providers: [
      { provider: "mock", scheme: "bearer", env: "MOCK_KEY", env_set: false, file_set: false },
    ],
  })
  store.evolution({ tag: "evo-0", status: "idle", events: [] })
  store.session({
    id: "ses_demo",
    title: "Test",
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
  setActiveStore(store)
  setActiveTheme(store.theme())
  return store
}

const settle = () => new Promise<void>((resolve) => setTimeout(resolve, 20))

function message(overrides: Partial<UiMessage> = {}): UiMessage {
  return {
    id: "a1",
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

function tool(id: string, path = "same.py", name = "edit_file"): ToolView {
  return {
    id,
    name,
    summary: JSON.stringify({ path }),
    status: "ok",
    output: "",
    error: null,
    durationMs: 1,
  }
}

function descendants(root: Renderable): Renderable[] {
  return root.getChildren().flatMap((child) => [child, ...descendants(child)])
}

function imageFile(): { dir: string; path: string } {
  const root = process.env.LOCALAPPDATA
    ? join(process.env.LOCALAPPDATA, "Temp", "opencode")
    : (process.env.TMPDIR ?? process.env.TEMP ?? process.cwd())
  const dir = mkdtempSync(join(root, "siwah-component-"))
  const path = join(dir, "example.png")
  writeFileSync(path, Buffer.from("image fixture"))
  return { dir, path }
}

test("a visible connect dialog owns keys even with a pending permission", async () => {
  const store = readyStore()
  const decisions: string[] = []
  store.resolvePermission = async (decision) => {
    decisions.push(decision)
  }
  store.permission({
    requestId: "p1",
    tool: "bash",
    kind: "bash",
    preview: "do not run",
    choices: ["once", "deny"],
  })
  store.openDialog({ type: "connect" })
  const setup = await testRender(() => <App store={store} initialize={false} />, {
    width: 100,
    height: 32,
  })
  try {
    await setup.renderOnce()
    setup.mockInput.pressEnter()
    await setup.renderOnce()
    setup.mockInput.typeText("sad")
    await setup.renderOnce()
    expect(decisions).toEqual([])
    expect(store.permission()?.requestId).toBe("p1")
    expect(store.promptText()).toBe("")
    expect(setup.captureCharFrame()).toContain("Key: ***")
  } finally {
    setup.renderer.destroy()
  }
})

test("mixed instructions and image paths remain intact and do not leak into modal paste", async () => {
  const store = readyStore()
  const file = imageFile()
  let handle: PromptHandle | undefined
  const setup = await testRender(
    () => (
      <PromptBox
        mode="build"
        stats="ctx"
        onReady={(next) => {
          handle = next
        }}
        onChange={(next) => store.promptText(next)}
        onSubmit={() => false}
      />
    ),
    { width: 100, height: 15 },
  )
  try {
    await setup.renderOnce()
    const mixed = `Explain this screenshot carefully:\n${file.path}\nKeep the original layout.`
    await setup.mockInput.pasteBracketedText(mixed)
    await setup.renderOnce()
    expect(handle?.value()).toBe(mixed)
    expect(setup.captureCharFrame()).not.toContain("[Image 1]")
    store.openDialog({ type: "connect" })
    handle?.blur()
    await setup.mockInput.pasteBracketedText(file.path)
    await setup.renderOnce()
    await setup.renderOnce()
    expect(handle?.value()).toBe(mixed)
    expect(setup.captureCharFrame()).not.toContain("[Image 1]")
  } finally {
    setup.renderer.destroy()
    rmSync(file.dir, { recursive: true })
  }
})

test("prompt admission preserves rejected attachments and newer edits", async () => {
  readyStore()
  let handle: PromptHandle | undefined
  let decide: ((accepted: boolean) => void) | undefined
  const received: PromptImage[][] = []
  const setup = await testRender(
    () => (
      <PromptBox
        mode="build"
        stats="ctx"
        onReady={(next) => {
          handle = next
        }}
        onChange={() => {}}
        onSubmit={(_text, images) => {
          received.push(images)
          return new Promise<boolean>((resolve) => {
            decide = resolve
          })
        }}
      />
    ),
    { width: 100, height: 15 },
  )
  const file = imageFile()
  try {
    await setup.renderOnce()
    await setup.mockInput.pasteBracketedText(file.path)
    setup.mockInput.pressEnter()
    await settle()
    expect(handle?.value()).toContain("[Image 1]")
    decide?.(false)
    await settle()
    setup.mockInput.pressEnter()
    await settle()
    expect(received[1]).toHaveLength(1)
    handle?.setText("a newer draft")
    decide?.(true)
    await settle()
    expect(handle?.value()).toBe("a newer draft")
  } finally {
    setup.renderer.destroy()
    rmSync(file.dir, { recursive: true })
  }
})

for (const draft of ["plain draft", "/review main", "/init focus", "/custom args"]) {
  test(`failed or busy submission retains ${draft}`, async () => {
    const store = readyStore()
    store.initMemory = async () => {}
    store.runCustomCommand = async () => false
    const internals = store as unknown as { api: unknown; startStream: () => void }
    internals.api = {
      sendMessage: async () => {
        throw new ApiError("Not accepted", 409, "run_conflict")
      },
    }
    internals.startStream = () => {}
    const setup = await testRender(() => <App store={store} initialize={false} />, {
      width: 100,
      height: 32,
    })
    try {
      await setup.renderOnce()
      setup.mockInput.typeText(draft)
      setup.mockInput.pressEnter()
      await settle()
      expect(store.promptText()).toBe(draft)
      expect(store.messages()).toHaveLength(0)
      store.running(true)
      setup.mockInput.pressEnter()
      await settle()
      expect(store.promptText()).toBe(draft)
    } finally {
      setup.renderer.destroy()
    }
  })
}

test("changing the theme retains composer text and attachments", async () => {
  const store = readyStore()
  const file = imageFile()
  const setup = await testRender(() => <App store={store} initialize={false} />, {
    width: 100,
    height: 32,
  })
  try {
    await setup.renderOnce()
    setup.mockInput.typeText("keep my instructions ")
    await setup.mockInput.pasteBracketedText(file.path)
    const before = store.promptText()
    store.theme(resolveTheme("siwah-light", false))
    await setup.renderOnce()
    expect(store.promptText()).toBe(before)
    expect(setup.captureCharFrame()).toContain("keep my instructions")
    expect(setup.captureCharFrame()).toContain("[Image 1]")
    expect(setup.captureCharFrame()).toContain("example.png")
  } finally {
    setup.renderer.destroy()
    rmSync(file.dir, { recursive: true })
  }
})

test("markdown initially empty renders changing block kinds before message.end", async () => {
  readyStore()
  const [content, setContent] = createSignal("")
  const setup = await testRender(() => <MarkdownText content={content()} streaming />, {
    width: 80,
    height: 20,
  })
  try {
    await setup.renderOnce()
    for (const text of [
      "first streamed token",
      "# A streamed heading",
      "```py\nprint('live code')",
      "| Name | Value |\n|---|---|\n| streamed table | yes |",
      "plain text after table",
    ]) {
      setContent(text)
      await setup.renderOnce()
      const frame = setup.captureCharFrame()
      expect(frame).toContain(
        text.includes("streamed table")
          ? "streamed table"
          : text.includes("live code")
            ? "live code"
            : text.replace(/^# /, ""),
      )
    }
  } finally {
    setup.renderer.destroy()
  }
})

for (const name of ["bash", "read_file", "write_file"]) {
  for (const expanded of [false, true]) {
    test(`10,000-line ${name} output is bounded when expanded=${expanded}`, async () => {
      const store = readyStore()
      if (expanded) store.toggleTool("large")
      const large = {
        ...tool("large", "large.py", name),
        output: Array.from({ length: 10_000 }, (_, index) => `line_${index + 1}`).join("\n"),
      }
      const setup = await testRender(() => <ToolCard tool={large} />, {
        width: 80,
        height: 12,
      })
      try {
        await setup.renderOnce()
        const texts = descendants(setup.renderer.root).filter(
          (node) => node instanceof TextRenderable,
        )
        expect(texts.length).toBeLessThanOrEqual(1010)
        if (expanded) {
          expect(texts.some((node) => node.plainText.includes("9000 lines omitted"))).toBe(true)
        }
      } finally {
        setup.renderer.destroy()
      }
    })
  }
}

test("repeated edits bind different exact-path diffs once each", async () => {
  readyStore()
  const setup = await testRender(
    () => (
      <MessageView
        message={message({
          toolCalls: [tool("first"), tool("second"), tool("other", "not-same.py")],
          diffs: [
            { path: "same.py", patch: "@@ -1 +1 @@\n-first_old\n+first_new" },
            { path: "same.py", patch: "@@ -1 +1 @@\n-second_old\n+second_new" },
            { path: "unmatched.py", patch: "@@ -1 +1 @@\n-third_old\n+third_new" },
          ],
        })}
      />
    ),
    { width: 100, height: 35 },
  )
  try {
    await setup.renderOnce()
    const frame = setup.captureCharFrame()
    expect(frame.match(/first_new/g)).toHaveLength(1)
    expect(frame.match(/second_new/g)).toHaveLength(1)
    expect(frame.match(/third_new/g)).toHaveLength(1)
  } finally {
    setup.renderer.destroy()
  }
})

test("diff hunk lines starting with header markers remain changes", () => {
  expect(
    parseUnifiedDiff("--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n--- old text\n+++ new text"),
  ).toEqual([
    { kind: "hunk", text: "@@ -1 +1 @@", oldNumber: null, newNumber: null },
    { kind: "remove", text: "-- old text", oldNumber: 1, newNumber: null },
    { kind: "add", text: "++ new text", oldNumber: null, newNumber: 1 },
  ])
})

test("long table cells show an explicit truncation marker", async () => {
  readyStore()
  const setup = await testRender(
    () => (
      <MarkdownText
        content={`| Key | Description |\n|---|---|\n| key | ${"a long cell ".repeat(100)} |`}
        streaming={false}
      />
    ),
    { width: 60, height: 18 },
  )
  try {
    await setup.renderOnce()
    expect(setup.captureCharFrame()).toContain("…")
  } finally {
    setup.renderer.destroy()
  }
})

test("palette and history accept an interactive filter", async () => {
  const store = readyStore()
  store.history(["unrelated task", "find the needle in this history"])
  const setup = await testRender(() => <App store={store} initialize={false} />, {
    width: 100,
    height: 32,
  })
  try {
    await setup.renderOnce()
    setup.mockInput.pressKey("p", { ctrl: true })
    setup.mockInput.typeText("review")
    await setup.renderOnce()
    expect(setup.captureCharFrame()).toContain("filter: review")
    expect(setup.captureCharFrame()).not.toContain("/connect")
    setup.mockInput.pressEscape()
    await settle()
    await setup.renderOnce()
    setup.mockInput.pressKey("r", { ctrl: true })
    setup.mockInput.typeText("needle")
    await setup.renderOnce()
    expect(setup.captureCharFrame()).toContain("filter: needle")
    setup.mockInput.pressEnter()
    await settle()
    expect(store.promptText()).toBe("find the needle in this history")
  } finally {
    setup.renderer.destroy()
  }
})

test("evolution toggle resumes a paused idle engine", async () => {
  const store = readyStore()
  store.evolution({ tag: "evo-0", status: "idle", paused: true, events: [] })
  const requests: boolean[] = []
  store.pauseEvolution = async (paused) => {
    requests.push(paused ?? true)
  }
  store.openDialog({ type: "evolution" })
  const setup = await testRender(() => <App store={store} initialize={false} />, {
    width: 100,
    height: 32,
  })
  try {
    await setup.renderOnce()
    setup.mockInput.typeText("p")
    expect(requests).toEqual([false])
  } finally {
    setup.renderer.destroy()
  }
})

test("prompt chrome exposes session allowance beside context", async () => {
  const store = readyStore()
  store.usage({
    input_tokens: 100,
    output_tokens: 10,
    reasoning_tokens: 0,
    cache_read_tokens: 0,
    cache_write_tokens: 0,
    allowance: 0.123456,
    cost: 0,
    list_cost: 0,
    steps: 1,
  })
  const setup = await testRender(() => <App store={store} initialize={false} />, {
    width: 100,
    height: 32,
  })
  try {
    await setup.renderOnce()
    expect(setup.captureCharFrame()).toContain("$0.123456")
  } finally {
    setup.renderer.destroy()
  }
})

test("the changes panel opens a net patch at 60 columns", async () => {
  const store = readyStore()
  const requested: (string | undefined)[] = []
  store.reviewChanges = async (_scope, _checkpoint, path) => {
    requested.push(path)
    return {
      scope: "turn",
      available: true,
      truncated: false,
      files: [
        {
          path: "example.py",
          status: "M",
          truncated: false,
          patch: path
            ? "--- a/example.py\n+++ b/example.py\n@@ -1 +1 @@\n-old text\n+new text\n"
            : "",
        },
      ],
    }
  }
  store.openDialog({ type: "changes", scope: "turn" })
  const setup = await testRender(() => <App store={store} initialize={false} />, {
    width: 60,
    height: 28,
  })
  try {
    await setup.renderOnce()
    expect(setup.captureCharFrame()).toContain("example.py")
    expect(requested).toEqual([undefined])
    setup.mockInput.pressEnter()
    await setup.renderOnce()
    expect(setup.captureCharFrame()).toContain("new text")
    expect(requested).toEqual([undefined, "example.py"])
    setup.mockInput.pressEscape()
    await settle()
    await setup.renderOnce()
    expect(setup.captureCharFrame()).toContain("latest turn changes")
  } finally {
    setup.renderer.destroy()
  }
})

test("switching draft targets restores the cursor and image attachments", async () => {
  const store = readyStore()
  const image = imageFile()
  const received: PromptImage[][] = []
  store.draftTarget({ id: "a", carry: false })
  const setup = await testRender(
    () => (
      <PromptBox
        mode="build"
        stats="ctx"
        onReady={() => {}}
        onChange={(next) => store.promptText(next)}
        onSubmit={(_text, images) => {
          received.push(images)
          return false
        }}
      />
    ),
    { width: 100, height: 15 },
  )
  try {
    await setup.renderOnce()
    await setup.mockInput.pasteBracketedText(image.path)
    await setup.renderOnce()
    const draft = store.promptText()
    expect(draft).toContain("[Image 1]")
    const input = descendants(setup.renderer.root).find(
      (node): node is TextareaRenderable => node instanceof TextareaRenderable,
    )
    if (!input) throw new Error("Composer did not mount")
    input.cursorOffset = 2
    store.promptText("draft for b")
    store.draftTarget({ id: "b", carry: false })
    await setup.renderOnce()
    setup.mockInput.pressEnter()
    await settle()
    expect(received.at(-1)).toEqual([])
    store.promptText(draft)
    store.draftTarget({ id: "a", carry: false })
    await setup.renderOnce()
    expect(input.cursorOffset).toBe(2)
    setup.mockInput.pressEnter()
    await settle()
    expect(received.at(-1)).toHaveLength(1)
  } finally {
    setup.renderer.destroy()
    rmSync(image.dir, { recursive: true })
  }
})

test("pending inputs return a queued prompt and its image to the composer", async () => {
  const store = readyStore()
  store.queuedPrompts([{ id: "queued_1", text: "Use [Image 1]", attachmentCount: 1 }])
  const attachment = { mime: "image/png", name: "fixture.png", data: "aW1hZ2U=" }
  const removed: string[] = []
  const sent: unknown[] = []
  const internals = store as unknown as { api: unknown }
  internals.api = {
    removeQueuedPrompt: async (_sessionId: string, id: string) => {
      removed.push(id)
      return { id, content: "Use [Image 1]", attachments: [attachment] }
    },
    sendMessage: async (_sessionId: string, text: string, images: unknown) => {
      sent.push({ text, images })
      return { status: "queued", run_id: "queued_2" }
    },
  }
  store.running(true)
  const setup = await testRender(() => <App store={store} initialize={false} />, {
    width: 100,
    height: 30,
  })
  try {
    await setup.renderOnce()
    setup.mockInput.typeText("/pending")
    setup.mockInput.pressEnter()
    await settle()
    await setup.renderOnce()
    expect(setup.captureCharFrame()).toContain("pending inputs")
    setup.mockInput.pressEnter()
    await setup.renderOnce()
    expect(setup.captureCharFrame()).toContain("Return to editor")
    setup.mockInput.pressEnter()
    await settle()
    await setup.renderOnce()
    expect(removed).toEqual(["queued_1"])
    expect(store.dialog()).toBeNull()
    expect(store.promptText()).toBe("Use [Image 1]")
    expect(setup.captureCharFrame()).toContain("fixture.png")
    setup.mockInput.pressEnter()
    await settle()
    expect(sent).toEqual([{ text: "Use [Image 1]", images: [attachment] }])
  } finally {
    setup.renderer.destroy()
  }
})

test("pending inputs can queue an unapplied correction explicitly", async () => {
  const store = readyStore()
  const content = "Use the other fixture."
  store.messages([
    {
      id: "late",
      role: "user",
      content,
      reasoning: "",
      streaming: false,
      createdAt: "2026-01-01T00:00:00Z",
      toolCalls: [],
      diffs: [],
      reasoningOpen: false,
      unapplied: true,
    },
  ])
  const submitted: string[] = []
  const resolved: string[] = []
  store.sendPrompt = async (prompt) => {
    submitted.push(prompt)
    return true
  }
  store.resolveCorrection = async (id) => {
    resolved.push(id)
    return true
  }
  const setup = await testRender(() => <App store={store} initialize={false} />, {
    width: 85,
    height: 29,
  })
  try {
    await setup.renderOnce()
    expect(setup.captureCharFrame()).toContain("Correction not applied")
    store.openDialog({ type: "pending" })
    await setup.renderOnce()
    setup.mockInput.pressEnter()
    await setup.renderOnce()
    expect(setup.captureCharFrame()).toContain("Send as follow-up")
    expect(submitted).toEqual([])
    setup.mockInput.pressArrow("down")
    setup.mockInput.pressEnter()
    await settle()
    expect(submitted).toEqual([content])
    expect(resolved).toEqual(["late"])
    expect(store.dialog()).toBeNull()
  } finally {
    setup.renderer.destroy()
  }
})

test("transcript search finds tool output and paths, then jumps to the selected message", async () => {
  const store = readyStore()
  store.messages([
    message({ id: "first", content: "The parser is ready." }),
    message({ id: "second", diffs: [{ path: "src/parser.ts", patch: "" }] }),
    message({
      id: "third",
      toolCalls: [
        {
          id: "tool-search",
          name: "bash",
          summary: "run tests",
          status: "error",
          output: "Test failed: parser expected token",
          error: null,
          durationMs: 10,
        },
      ],
    }),
  ])
  const setup = await testRender(() => <App store={store} initialize={false} />, {
    width: 75,
    height: 28,
  })
  try {
    await setup.renderOnce()
    setup.mockInput.typeText("/find parser")
    setup.mockInput.pressEnter()
    await setup.renderOnce()
    const frame = setup.captureCharFrame()
    expect(frame).toContain("find in session")
    expect(frame).toContain("1/3")
    setup.mockInput.pressArrow("down")
    setup.mockInput.pressArrow("down")
    await setup.renderOnce()
    expect(setup.captureCharFrame()).toContain("3/3")
    expect(setup.captureCharFrame()).toContain("parser expected token")
    const transcript = descendants(setup.renderer.root).find((node) => node.id === "transcript")
    if (!(transcript instanceof ScrollBoxRenderable)) throw new Error("Transcript not mounted")
    const jump = spyOn(transcript, "scrollChildIntoView")
    setup.mockInput.pressEnter()
    await settle()
    expect(jump).toHaveBeenCalledWith("msg-third", { align: "start" })
    expect(store.dialog()).toBeNull()
  } finally {
    setup.renderer.destroy()
  }
})
