import { expect, test } from "bun:test"
import {
  type Renderable,
  ScrollBoxRenderable,
  TextareaRenderable,
  TextRenderable,
} from "@opentui/core"
import { testRender } from "@opentui/solid"
import { TuiStore, type UiMessage } from "../src/state"
import { baseTheme, resolveTheme } from "../src/theme"
import { App } from "../src/ui/App"
import { setActiveStore, setActiveTheme } from "../src/ui/context"
import { Logo, siwahLine, siwahMark } from "../src/ui/Logo"
import { MessageView } from "../src/ui/MessageView"
import { ToolCard } from "../src/ui/ToolCard"

function readyStore() {
  const store = new TuiStore()
  store.theme(resolveTheme("siwah-dark", false))
  store.models({ providers: [], roles: { main: "mock/model-a" } })
  store.auth({ providers: [] })
  store.connection("online")
  store.session({
    id: "focused",
    title: "Focused revision",
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
  streaming: true,
  createdAt: "",
  toolCalls: [],
  diffs: [],
  reasoningOpen: false,
  ...overrides,
})

test("supplied logo data and material palette are exact", async () => {
  readyStore()
  expect(siwahMark).toEqual([
    ["           ", "                ▄▄▄▄▄▄▄▄▄██▀"],
    ["        ███", "▄▄▄▄▄▄█████████████████▀▀"],
    ["        █▀█", "██████████████████▀▀▀"],
    ["        ███", "████▀▀▀▀▀▀▀▀▀▀"],
    ["     ██████", "   ▄▀▀▀ ▀ █   █  ▀▀▄ █"],
    ["     ████  ", "    ▀▀▄ █ █▄▀▄█ ▄▀▀█ █▀▀▄"],
    ["   ████████", "   ▀▀▀  ▀ ▀   ▀  ▀▀▀ ▀  ▀"],
  ])
  expect(siwahLine).toBe("◆━━━━━━━━━━━━━━━━◥")
  const theme = baseTheme("siwah-dark")
  expect([theme.background, theme.text, theme.accent, theme.accentAlt]).toEqual([
    "#15151b",
    "#d7dae0",
    "#d4a63a",
    "#c2324f",
  ])
  const setup = await testRender(() => <Logo animated={false} />, { width: 80, height: 12 })
  try {
    await setup.renderOnce()
    const frame = setup.captureCharFrame()
    for (const [, rest] of siwahMark) {
      const visible = rest.trimEnd()
      if (visible) expect(frame).toContain(visible)
    }
  } finally {
    setup.renderer.destroy()
  }
})

for (const width of [60, 100, 120]) {
  test(`spaced composer stays fixed with activity below its frame at ${width} columns`, async () => {
    const store = readyStore()
    store.messages([answer()])
    const setup = await testRender(() => <App store={store} initialize={false} />, {
      width,
      height: 28,
    })
    try {
      await setup.renderOnce()
      const frame = setup.captureCharFrame()
      expect(frame.split("\n")[0]).toContain("Focused revision")
      expect(frame.split("\n")[0]).not.toContain("siwah")
      expect(frame).not.toContain("responding")
      expect(frame).not.toMatch(/^\s*siwah\s*$/m)
      expect(frame).not.toContain("Ready")
      expect(frame).not.toContain("enter send")
      const panel = nodes(setup.renderer.root).find((node) => node.id === "composer-panel")
      expect(panel).toBeDefined()
      if (!panel) throw new Error("Composer panel is missing")
      const children = nodes(panel)
      expect(children.some((node) => node instanceof TextareaRenderable)).toBe(true)
      expect(
        children.some(
          (node) => node instanceof TextRenderable && node.plainText.includes("mock/model-a"),
        ),
      ).toBe(true)
      const rows = frame.split("\n")
      const top = rows.findIndex((line) => line.includes("╭"))
      const bottom = rows.findIndex((line) => line.includes("╰"))
      expect(rows.filter((line) => line.includes("╭"))).toHaveLength(1)
      expect(rows.findIndex((line) => line.includes("Ask siwah"))).toBeGreaterThan(top)
      expect(rows.findIndex((line) => line.includes("mock/model-a"))).toBeLessThan(bottom)
      const input = children.find((node) => node instanceof TextareaRenderable)
      const provider = children.find(
        (node) => node instanceof TextRenderable && node.plainText.includes("mock/model-a"),
      )
      if (!input || !provider) throw new Error("Composer input or provider row is missing")
      expect(rows[input.y - 1]?.replace(/[│ ]/g, "")).toBe("")
      expect(provider.y - (input.y + input.height)).toBe(1)
      expect(rows[provider.y - 1]?.replace(/[│ ]/g, "")).toBe("")
      const bounds = [panel.x, panel.y, panel.width, panel.height]
      store.running(true)
      await setup.renderOnce()
      const activeRows = setup.captureCharFrame().split("\n")
      const activity = activeRows.findIndex((line) => line.includes("working ·"))
      expect(activity).toBeGreaterThan(activeRows.findIndex((line) => line.includes("╰")))
      expect(setup.captureCharFrame().match(/⠋/g)).toHaveLength(1)
      expect([panel.x, panel.y, panel.width, panel.height]).toEqual(bounds)
      expect(
        nodes(panel).some(
          (node) => node instanceof TextRenderable && node.plainText.includes("working ·"),
        ),
      ).toBe(false)
      store.running(false)
      await setup.renderOnce()
      expect(setup.captureCharFrame()).not.toContain("working ·")
      expect([panel.x, panel.y, panel.width, panel.height]).toEqual(bounds)
    } finally {
      setup.renderer.destroy()
    }
  })
}

test("autocomplete tracks the actual composer after multiline input and resize", async () => {
  const store = readyStore()
  store.files(["src/main.ts", "src/map.ts"])
  const setup = await testRender(() => <App store={store} initialize={false} />, {
    width: 100,
    height: 32,
  })
  try {
    await setup.renderOnce()
    setup.mockInput.typeText("one\nsecond\n@src/")
    for (const width of [100, 60, 120]) {
      await setup.resize(width, 32)
      // A measured layout change schedules the menu's next layout pass.
      await setup.renderOnce()
      await setup.renderOnce()
      const rows = setup.captureCharFrame().split("\n")
      const menuEnd = rows.findIndex((line) => line.includes("navigate"))
      const panelTop = rows.findIndex((line) => line.includes("╭"))
      expect(menuEnd).toBeGreaterThanOrEqual(0)
      expect(menuEnd).toBeLessThan(panelTop)
      expect(rows.findIndex((line) => line.includes("second"))).toBeGreaterThan(panelTop)
      expect(store.promptText()).toBe("one\nsecond\n@src/")
    }
  } finally {
    setup.renderer.destroy()
  }
})

test("scroll activity and jump-to-latest leave the input frame and draft unchanged", async () => {
  const store = readyStore()
  store.messages([
    answer({
      streaming: false,
      content: Array.from({ length: 100 }, (_, index) => `history line ${index}`).join("\n"),
    }),
  ])
  const setup = await testRender(() => <App store={store} initialize={false} />, {
    width: 100,
    height: 28,
  })
  try {
    await setup.renderOnce()
    setup.mockInput.typeText("keep this draft")
    await setup.renderOnce()
    const panel = setup.renderer.root.findDescendantById("composer-panel")
    const transcript = setup.renderer.root.findDescendantById("transcript")
    if (!panel || !(transcript instanceof ScrollBoxRenderable)) throw new Error("Missing layout")
    const bounds = [panel.y, panel.height]
    transcript.scrollTo(0)
    await setup.renderOnce()
    const rows = setup.captureCharFrame().split("\n")
    const activity = rows.findIndex((line) => line.includes("↓ latest"))
    expect(activity).toBeGreaterThan(panel.y + panel.height - 1)
    expect([panel.y, panel.height]).toEqual(bounds)
    expect(store.promptText()).toBe("keep this draft")
    await setup.mockMouse.click(80, activity)
    await setup.renderOnce()
    expect(transcript.scrollTop).toBeGreaterThan(0)
    expect([panel.y, panel.height]).toEqual(bounds)
    expect(store.promptText()).toBe("keep this draft")
  } finally {
    setup.renderer.destroy()
  }
})

test("stacked notifications stay above the taller multiline composer", async () => {
  const store = readyStore()
  const setup = await testRender(() => <App store={store} initialize={false} />, {
    width: 60,
    height: 24,
  })
  try {
    await setup.renderOnce()
    setup.mockInput.typeText("first\nsecond\nthird")
    for (let index = 0; index < 4; index += 1) {
      store.notify(`Notification ${index}: ${"long detail ".repeat(8)}`, "info")
    }
    await setup.renderOnce()
    await setup.renderOnce()
    const panel = setup.renderer.root.findDescendantById("composer-panel")
    const toast = nodes(setup.renderer.root).find(
      (node) =>
        node instanceof ScrollBoxRenderable &&
        nodes(node).some((child) => child.id.startsWith("toast-")),
    )
    if (!panel || !toast) throw new Error("Missing composer or notification stack")
    expect(toast.y + toast.height).toBeLessThanOrEqual(panel.y)
    expect(setup.captureCharFrame()).toContain("third")
    expect(setup.captureCharFrame()).toContain("mock/model-a")
    expect(store.promptText()).toBe("first\nsecond\nthird")
  } finally {
    setup.renderer.destroy()
  }
})

for (const name of ["bash", "edit_file"]) {
  for (const output of ["", "short output"]) {
    test(`${name} discloses a full long command with ${output ? "short" : "empty"} output`, async () => {
      const store = readyStore()
      const value = `prefix  ${"segment ".repeat(20)}last-value`
      const summary = JSON.stringify(name === "bash" ? { command: value } : { path: value })
      const label = name === "bash" ? `$ ${value}` : `edit_file  ${value}`
      const setup = await testRender(
        () => (
          <ToolCard
            tool={{ id: "long", name, summary, output, status: "ok", durationMs: 10, error: null }}
          />
        ),
        { width: 60, height: 20 },
      )
      try {
        await setup.renderOnce()
        expect(setup.captureCharFrame()).not.toContain("last-value")
        expect(setup.captureCharFrame()).toContain("full command")
        await setup.mockMouse.drag(3, 0, 20, 0)
        expect(store.isToolExpanded("long")).toBe(false)
        await setup.mockMouse.click(3, 0)
        await setup.renderOnce()
        expect(store.isToolExpanded("long")).toBe(true)
        expect(setup.captureCharFrame()).toContain("last-value")
        expect(
          nodes(setup.renderer.root).some(
            (node) => node instanceof TextRenderable && node.plainText === label,
          ),
        ).toBe(true)
        await setup.mockMouse.click(3, 0)
        await setup.renderOnce()
        expect(store.isToolExpanded("long")).toBe(false)
      } finally {
        setup.renderer.destroy()
      }
    })
  }
}

test("ordinary command spacing does not create an expansion footer or status icon", async () => {
  readyStore()
  const setup = await testRender(
    () => (
      <ToolCard
        tool={{
          id: "short",
          name: "read_file",
          summary: '{"path":"main.py"}',
          output: "",
          status: "ok",
          durationMs: 12,
          error: null,
        }}
      />
    ),
    { width: 80, height: 8 },
  )
  try {
    await setup.renderOnce()
    const frame = setup.captureCharFrame()
    expect(frame).toContain("read_file  main.py")
    expect(frame).not.toContain("click to expand")
    expect(frame).not.toMatch(/[✓◇▾▸●]/)
    expect(frame).not.toContain("done")
  } finally {
    setup.renderer.destroy()
  }
})

test("legacy tool messages disclose long headers without treating selection as a click", async () => {
  readyStore()
  const command = `$ echo  ${"segment ".repeat(20)}legacy-last`
  const setup = await testRender(
    () => <MessageView message={answer({ role: "tool", content: command })} />,
    { width: 60, height: 16 },
  )
  try {
    await setup.renderOnce()
    expect(setup.captureCharFrame()).not.toContain("legacy-last")
    await setup.mockMouse.drag(3, 0, 20, 0)
    await setup.renderOnce()
    expect(setup.captureCharFrame()).not.toContain("legacy-last")
    await setup.mockMouse.click(3, 0)
    await setup.renderOnce()
    expect(setup.captureCharFrame()).toContain("legacy-last")
    expect(
      nodes(setup.renderer.root).some(
        (node) => node instanceof TextRenderable && node.plainText === command,
      ),
    ).toBe(true)
    await setup.mockMouse.click(3, 0)
    await setup.renderOnce()
    expect(setup.captureCharFrame()).not.toContain("legacy-last")
  } finally {
    setup.renderer.destroy()
  }
})

for (const status of ["error", "cancelled"] as const) {
  test(`${status} remains readable without header symbols`, async () => {
    readyStore()
    const setup = await testRender(
      () => (
        <ToolCard
          tool={{
            id: "failed",
            name: "bash",
            summary: '{"command":"echo test"}',
            output: "",
            status,
            error: status === "error" ? "Command failed: retry the operation" : null,
            durationMs: 10,
          }}
        />
      ),
      { width: 80, height: 8 },
    )
    try {
      await setup.renderOnce()
      expect(setup.captureCharFrame()).toContain(status)
      expect(setup.captureCharFrame().split("\n")[0]).toContain("$ echo test")
      if (status === "error") expect(setup.captureCharFrame()).toContain("Command failed")
    } finally {
      setup.renderer.destroy()
    }
  })
}
