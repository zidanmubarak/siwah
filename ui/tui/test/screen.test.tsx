import { expect, test } from "bun:test"
import type { ScrollBoxRenderable } from "@opentui/core"
import { testRender } from "@opentui/solid"

import type { Session } from "../src/api/client"
import { type ToolView, TuiStore } from "../src/state"
import { resolveTheme } from "../src/theme"
import { App } from "../src/ui/App"

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
  store.models({
    providers: [
      {
        provider: "mock",
        models: [
          {
            id: "model-a",
            name: "Model A",
            context_window: 100_000,
            max_output: 8_000,
            supports_tools: true,
            supports_reasoning: true,
            native_api: "openai_chat",
          },
        ],
      },
    ],
    roles: { main: "mock/model-a" },
  })
  store.auth({
    providers: [
      {
        provider: "mock",
        scheme: "bearer",
        env: "MOCK_API_KEY",
        env_set: false,
        file_set: true,
      },
    ],
  })
  store.sessions([])
  store.evolution({ tag: "evo-0", status: "idle", events: [] })
  const session: Session = {
    id: "ses_demo",
    title: "fix parser",
    cwd: "D:/work/siwah",
    model: "mock/model-a",
    mode: "build",
    status: "idle",
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    metadata: {},
    last_event_seq: 0,
  }
  store.session(session)
  store.usage({
    input_tokens: 1200,
    output_tokens: 300,
    reasoning_tokens: 40,
    cache_read_tokens: 800,
    cache_write_tokens: 0,
    allowance: 0.000429,
    cost: 0,
    list_cost: 0.000429,
    steps: 3,
    context_window: 100_000,
    context_tokens: 2000,
    context_percent: 2,
  })
  store.connection("online")
  return store
}

function tool(): ToolView {
  return {
    id: "call_1",
    name: "read_file",
    summary: '{"path":"math_utils.py"}',
    status: "ok",
    output: "    1  def add(a, b):\n    2      return a - b",
    error: null,
    durationMs: 12,
  }
}

test("welcome screen snapshot and status bar", async () => {
  const store = readyStore()
  store.messages([])
  const setup = await testRender(() => <App store={store} initialize={false} />, {
    width: 100,
    height: 32,
  })
  try {
    await setup.renderOnce()
    const frame = setup.captureCharFrame()
    expect(frame).toContain("siwah")
    expect(frame).toContain("mock/model-a")
    expect(frame).toContain("ctx 2k/100k 2%")
    expect(frame).toMatchSnapshot("welcome-100x32")
  } finally {
    setup.renderer.destroy()
  }
})

test("welcome labels a no-auth provider as no key required", async () => {
  const store = readyStore()
  store.messages([])
  store.auth({
    providers: [
      {
        provider: "local",
        scheme: "none",
        env: "LOCAL_API_KEY",
        env_set: false,
        file_set: false,
      },
    ],
  })
  const setup = await testRender(() => <App store={store} initialize={false} />, {
    width: 100,
    height: 32,
  })
  try {
    await setup.renderOnce()
    const frame = setup.captureCharFrame()
    expect(frame).toContain("Connect a provider with /connect")
    expect(frame).not.toContain("local: missing key")
  } finally {
    setup.renderer.destroy()
  }
})

test("welcome shows an available update", async () => {
  const store = readyStore()
  store.messages([])
  store.updateStatus({
    current: "0.3.1",
    latest: "0.4.0",
    available: true,
    repo: "zidanmubarak/siwah",
    source: "pyproject",
    commits_behind: 1,
    checked_at: "2026-01-01T00:00:00Z",
    can_apply: true,
  })
  const setup = await testRender(() => <App store={store} initialize={false} />, {
    width: 100,
    height: 32,
  })
  try {
    await setup.renderOnce()
    expect(setup.captureCharFrame()).toContain("0.4.0 is available")
  } finally {
    setup.renderer.destroy()
  }
})

test("slash command palette overlays the welcome without reflow", async () => {
  const store = readyStore()
  store.messages([])
  store.promptText("/")
  const setup = await testRender(() => <App store={store} initialize={false} />, {
    width: 100,
    height: 32,
  })
  try {
    await setup.renderOnce()
    const frame = setup.captureCharFrame()
    expect(frame).toContain("COMMANDS")
    expect(frame).toContain("/connect")
    expect(frame).toContain("fix parser")
  } finally {
    setup.renderer.destroy()
  }
})

test("@ completion lists files and folders", async () => {
  const store = readyStore()
  store.messages([])
  store.promptText("@li")
  store.files(["live_demo/", "live_demo/greet.py", "hello.py"])
  const setup = await testRender(() => <App store={store} initialize={false} />, {
    width: 100,
    height: 32,
  })
  try {
    await setup.renderOnce()
    const frame = setup.captureCharFrame()
    expect(frame).toContain("WORKSPACE FILES")
    expect(frame).toContain("live_demo/")
    expect(frame).toContain("greet.py")
    expect(frame).toContain("live_demo")
  } finally {
    setup.renderer.destroy()
  }
})

test("prompt exposes the permission mode and bypass badge", async () => {
  const store = readyStore()
  store.messages([])
  store.permissionMode("yolo")
  const setup = await testRender(() => <App store={store} initialize={false} />, {
    width: 100,
    height: 32,
  })
  try {
    await setup.renderOnce()
    expect(setup.captureCharFrame()).toContain("AUTO")
  } finally {
    setup.renderer.destroy()
  }
})

test("write_file shows every line with numbers and a plus sign", async () => {
  const store = readyStore()
  store.messages([
    {
      id: "a1",
      role: "assistant",
      content: "",
      reasoning: "",
      streaming: false,
      createdAt: "2026-01-01T00:00:00Z",
      toolCalls: [
        {
          ...tool(),
          name: "write_file",
          summary: "hello.py",
          output: "line one\nline two",
        },
      ],
      diffs: [],
      reasoningOpen: false,
    },
  ])
  const setup = await testRender(() => <App store={store} initialize={false} />, {
    width: 100,
    height: 32,
  })
  try {
    await setup.renderOnce()
    const frame = setup.captureCharFrame()
    expect(frame).toContain("write_file  hello.py")
    expect(frame).toContain("1 + line one")
    expect(frame).toContain("2 + line two")
    expect(frame).not.toContain("content")
  } finally {
    setup.renderer.destroy()
  }
})

test("replacing an existing file is shown as an edit, not write_file", async () => {
  const store = readyStore()
  store.messages([
    {
      id: "a1",
      role: "assistant",
      content: "",
      reasoning: "",
      streaming: false,
      createdAt: "2026-01-01T00:00:00Z",
      toolCalls: [
        {
          ...tool(),
          name: "write_file",
          summary: "hello.py",
          output: "new line\n",
          edited: true,
        },
      ],
      diffs: [
        {
          path: "hello.py",
          patch: "--- a/hello.py\n+++ b/hello.py\n@@ -1 +1 @@\n-old line\n+new line\n",
        },
      ],
      reasoningOpen: false,
    },
  ])
  const setup = await testRender(() => <App store={store} initialize={false} />, {
    width: 100,
    height: 32,
  })
  try {
    await setup.renderOnce()
    const frame = setup.captureCharFrame()
    expect(frame).toContain("edit_file  hello.py")
    expect(frame).not.toContain("write_file  hello.py")
    expect(frame).toContain("old line")
    expect(frame).toContain("new line")
  } finally {
    setup.renderer.destroy()
  }
})

test("edit diffs keep line numbers and plus/minus signs", async () => {
  const store = readyStore()
  store.messages([
    {
      id: "a1",
      role: "assistant",
      content: "",
      reasoning: "",
      streaming: false,
      createdAt: "2026-01-01T00:00:00Z",
      toolCalls: [],
      diffs: [
        {
          path: "hello.py",
          patch:
            "--- a/hello.py\n+++ b/hello.py\n@@ -1,2 +1,2 @@\n def add(a, b):\n-    return a - b\n+    return a + b\n",
        },
      ],
      reasoningOpen: false,
    },
  ])
  const setup = await testRender(() => <App store={store} initialize={false} />, {
    width: 100,
    height: 32,
  })
  try {
    await setup.renderOnce()
    const frame = setup.captureCharFrame()
    expect(frame).toContain("-     return a - b")
    expect(frame).toContain("+     return a + b")
  } finally {
    setup.renderer.destroy()
  }
})

test("long user prompts are collapsed", async () => {
  const store = readyStore()
  store.messages([
    {
      id: "u-long",
      role: "user",
      content: "line one\nline two\nline three\nline four\nline five",
      reasoning: "",
      streaming: false,
      createdAt: "2026-01-01T00:00:00Z",
      toolCalls: [],
      diffs: [],
      reasoningOpen: false,
    },
  ])
  const setup = await testRender(() => <App store={store} initialize={false} />, {
    width: 100,
    height: 32,
  })
  try {
    await setup.renderOnce()
    const frame = setup.captureCharFrame()
    expect(frame).toContain("[pasted ~5 lines]")
    expect(frame).not.toContain("click to expand")
  } finally {
    setup.renderer.destroy()
  }
})

test("transcript, tool card and diff snapshot", async () => {
  const store = readyStore()
  store.messages([
    {
      id: "u1",
      role: "user",
      content: "Fix the failing test in math_utils.py",
      reasoning: "",
      streaming: false,
      createdAt: "2026-01-01T00:00:00Z",
      toolCalls: [],
      diffs: [],
      reasoningOpen: false,
    },
    {
      id: "a1",
      role: "assistant",
      content: "## Plan\n\nRead the implementation and fix the subtraction bug.",
      reasoning: "The test expects addition; the implementation subtracts.",
      streaming: false,
      createdAt: "2026-01-01T00:00:00Z",
      toolCalls: [tool()],
      diffs: [
        {
          path: "math_utils.py",
          patch:
            "--- a/math_utils.py\n+++ b/math_utils.py\n@@ -1,2 +1,2 @@\n def add(a, b):\n-    return a - b\n+    return a + b\n",
        },
      ],
      reasoningOpen: false,
    },
  ])
  store.todos([
    { id: "1", content: "read implementation", status: "done" },
    { id: "2", content: "fix subtraction", status: "in_progress" },
  ])
  const setup = await testRender(() => <App store={store} initialize={false} />, {
    width: 120,
    height: 34,
  })
  try {
    await setup.renderOnce()
    const frame = setup.captureCharFrame()
    expect(frame).toContain("Fix the failing test")
    expect(frame).toContain("math_utils.py")
    expect(frame).toContain("fix subtraction")
    expect(frame).toContain("return a + b")
    expect(frame).toMatchSnapshot("transcript-120x34")
  } finally {
    setup.renderer.destroy()
  }
})

test("permission dialog snapshot", async () => {
  const store = readyStore()
  store.messages([])
  store.permission({
    requestId: "perm_1",
    toolCallId: "call_1",
    tool: "bash",
    kind: "bash",
    preview: "$ pytest -q",
    choices: ["once", "session", "always", "deny"],
  })
  const setup = await testRender(() => <App store={store} initialize={false} />, {
    width: 100,
    height: 30,
  })
  try {
    await setup.renderOnce()
    const permission = setup.captureCharFrame()
    expect(permission).toContain("permission required")
    expect(permission).toContain("allow once")
    expect(permission).toMatchSnapshot("permission-100x30")
  } finally {
    setup.renderer.destroy()
  }
})

test("resize, paste and fast typing do not throw", async () => {
  const store = readyStore()
  store.messages([])
  const setup = await testRender(() => <App store={store} initialize={false} />, {
    width: 100,
    height: 30,
  })
  try {
    await setup.renderOnce()
    await setup.resize(60, 20)
    await setup.renderOnce()
    expect(setup.captureCharFrame()).toContain("siwah")
    setup.mockInput.pasteBracketedText("x".repeat(20_000))
    await setup.renderOnce()
    setup.mockInput.typeText("hello")
    await setup.renderOnce()
    expect(setup.captureCharFrame()).toContain("hello")
  } finally {
    setup.renderer.destroy()
  }
})

test("large tool output is bounded inline", async () => {
  const store = readyStore()
  store.messages([
    {
      id: "a1",
      role: "assistant",
      content: "",
      reasoning: "",
      streaming: false,
      createdAt: "2026-01-01T00:00:00Z",
      toolCalls: [
        {
          ...tool(),
          output: Array.from({ length: 10_000 }, (_, index) => `line ${index + 1}`).join("\n"),
        },
      ],
      diffs: [],
      reasoningOpen: false,
    },
  ])
  const setup = await testRender(() => <App store={store} initialize={false} />, {
    width: 100,
    height: 30,
  })
  try {
    await setup.renderOnce()
    const frame = setup.captureCharFrame()
    expect(frame).toContain("▸ read_file")
    expect(frame).not.toContain("line 1")
    expect(frame).not.toContain("line 10000")
    expect(frame).toMatchSnapshot("tool-inline-100x30")
  } finally {
    setup.renderer.destroy()
  }
})

test("click expands only the chosen tool", async () => {
  const store = readyStore()
  const output = Array.from({ length: 12 }, (_, index) => `line ${index + 1}`).join("\n")
  store.messages([
    {
      id: "a1",
      role: "assistant",
      content: "",
      reasoning: "",
      streaming: false,
      createdAt: "2026-01-01T00:00:00Z",
      toolCalls: [
        { ...tool(), output },
        {
          ...tool(),
          id: "call_2",
          name: "glob",
          summary: '{"pattern":"**/*.py"}',
          output,
        },
      ],
      diffs: [],
      reasoningOpen: false,
    },
  ])
  const setup = await testRender(() => <App store={store} initialize={false} />, {
    width: 100,
    height: 60,
  })
  try {
    await setup.renderOnce()
    const frame = setup.captureCharFrame()
    const rows = frame.split("\n")
    const row = rows.findIndex((line) => line.includes("math_utils.py"))
    expect(row).toBeGreaterThanOrEqual(0)
    await setup.mockMouse.click(8, row)
    expect(store.isToolExpanded("call_1")).toBe(true)
    expect(store.isToolExpanded("call_2")).toBe(false)
    await setup.renderOnce()
    const bodyRow = setup
      .captureCharFrame()
      .split("\n")
      .findIndex((line) => line.includes("line 3"))
    expect(bodyRow).toBeGreaterThanOrEqual(0)
    await setup.mockMouse.drag(8, bodyRow, 30, bodyRow)
    expect(store.isToolExpanded("call_1")).toBe(true)
    await setup.mockMouse.click(8, bodyRow)
    expect(store.isToolExpanded("call_1")).toBe(false)
  } finally {
    setup.renderer.destroy()
  }
})

test("expanded tool output keeps the prompt and status visible", async () => {
  const store = readyStore()
  const output = Array.from({ length: 1_000 }, (_, index) => `line ${index + 1}`).join("\n")
  store.messages([
    {
      id: "a1",
      role: "assistant",
      content: "",
      reasoning: "",
      streaming: false,
      createdAt: "2026-01-01T00:00:00Z",
      toolCalls: [{ ...tool(), output }],
      diffs: [],
      reasoningOpen: false,
    },
  ])
  store.toggleTool("call_1")
  const setup = await testRender(() => <App store={store} initialize={false} />, {
    width: 100,
    height: 30,
  })
  try {
    await setup.renderOnce()
    const frame = setup.captureCharFrame()
    expect(frame).toContain("line 1")
    expect(frame).toContain("Ask siwah")
    expect(frame).toContain("mock/model-a")
    expect(frame).toContain("ctx 2k/100k 2%")
  } finally {
    setup.renderer.destroy()
  }
})

test("assistant markdown renders emphasis without literal markers", async () => {
  const store = readyStore()
  store.messages([
    {
      id: "a1",
      role: "assistant",
      content:
        "Level 0-1 changes *harness data* only. **Bold** text, `code`, ~~gone~~, and [docs](https://example.test).",
      reasoning: "",
      streaming: false,
      createdAt: "2026-01-01T00:00:00Z",
      toolCalls: [],
      diffs: [],
      reasoningOpen: false,
    },
  ])
  const setup = await testRender(() => <App store={store} initialize={false} />, {
    width: 100,
    height: 30,
  })
  try {
    await setup.renderOnce()
    const frame = setup.captureCharFrame()
    expect(frame).toContain("harness data")
    expect(frame).toContain("Bold")
    expect(frame).toContain("code")
    expect(frame).toContain("gone")
    expect(frame).toContain("docs")
    expect(frame).not.toContain("*harness")
    expect(frame).not.toContain("**Bold**")
    expect(frame).not.toContain("](https://example.test)")
  } finally {
    setup.renderer.destroy()
  }
})

test("ctrl+c copies a transcript selection", async () => {
  const store = readyStore()
  store.messages([
    {
      id: "a1",
      role: "assistant",
      content: "copy this answer text for the clipboard",
      reasoning: "",
      streaming: false,
      createdAt: "2026-01-01T00:00:00Z",
      toolCalls: [],
      diffs: [],
      reasoningOpen: false,
    },
  ])
  const setup = await testRender(() => <App store={store} initialize={false} />, {
    width: 100,
    height: 30,
  })
  try {
    await setup.renderOnce()
    const frameLines = setup.captureCharFrame().split("\n")
    const row = frameLines.findIndex((line) => line.includes("copy this"))
    expect(row).toBeGreaterThanOrEqual(0)
    const start = Math.max(0, frameLines[row]?.indexOf("copy this") ?? 4)
    await setup.mockMouse.drag(start, row, start + 30, row)
    setup.mockInput.pressKey("c", { ctrl: true })
    await setup.renderOnce()
    expect(
      store
        .toasts()
        .some((toast) => toast.text.startsWith("Copied") || toast.text.startsWith("Copy failed")),
    ).toBe(true)
  } finally {
    setup.renderer.destroy()
  }
})

test("prompt text can be typed and deleted", async () => {
  const store = readyStore()
  store.messages([])
  const setup = await testRender(() => <App store={store} initialize={false} />, {
    width: 100,
    height: 30,
  })
  try {
    await setup.renderOnce()
    setup.mockInput.typeText("hello")
    await setup.renderOnce()
    expect(setup.captureCharFrame()).toContain("hello")
    for (let index = 0; index < 5; index += 1) setup.mockInput.pressBackspace()
    await setup.renderOnce()
    expect(setup.captureCharFrame()).not.toContain("hello")
  } finally {
    setup.renderer.destroy()
  }
})

test("prompt box shows the context window from the model catalog", async () => {
  const store = readyStore()
  store.messages([])
  store.usage({
    input_tokens: 1200,
    output_tokens: 300,
    reasoning_tokens: 40,
    cache_read_tokens: 800,
    cache_write_tokens: 0,
    allowance: 0.000429,
    cost: 0,
    list_cost: 0.000429,
    steps: 3,
    context_window: null,
    context_tokens: 2000,
    context_percent: null,
  })
  const setup = await testRender(() => <App store={store} initialize={false} />, {
    width: 100,
    height: 30,
  })
  try {
    await setup.renderOnce()
    const frame = setup.captureCharFrame()
    expect(frame).toContain("ctx 2k/100k 2%")
  } finally {
    setup.renderer.destroy()
  }
})

test("running prompt shows the working spinner", async () => {
  const store = readyStore()
  store.messages([])
  store.running(true)
  const setup = await testRender(() => <App store={store} initialize={false} />, {
    width: 100,
    height: 32,
  })
  try {
    await setup.renderOnce()
    const frame = setup.captureCharFrame()
    expect(frame).toContain("working")
    expect(frame).toContain("mock/model-a")
  } finally {
    setup.renderer.destroy()
  }
})

test("reasoning is collapsed behind a label", async () => {
  const store = readyStore()
  store.messages([
    {
      id: "a1",
      role: "assistant",
      content: "done",
      reasoning: "A single line of reasoning.",
      streaming: false,
      createdAt: "2026-01-01T00:00:00Z",
      toolCalls: [],
      diffs: [],
      reasoningOpen: false,
    },
  ])
  const setup = await testRender(() => <App store={store} initialize={false} />, {
    width: 100,
    height: 30,
  })
  try {
    await setup.renderOnce()
    const frame = setup.captureCharFrame()
    expect(frame).toContain("reasoning")
    expect(frame).toContain("▸")
    expect(frame).not.toContain("A single line of reasoning.")
  } finally {
    setup.renderer.destroy()
  }
})

test("long commands truncate when collapsed and disclose the complete command on expand", async () => {
  const store = readyStore()
  const command =
    "echo alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu nu xi omicron pi rho sigma tau upsilon phi chi psi omega"
  store.messages([
    {
      id: "a1",
      role: "assistant",
      content: "",
      reasoning: "",
      streaming: false,
      createdAt: "2026-01-01T00:00:00Z",
      toolCalls: [
        {
          ...tool(),
          name: "bash",
          summary: JSON.stringify({ command }),
          output: "alpha\r\n\r\nbeta\r\n",
        },
      ],
      diffs: [],
      reasoningOpen: false,
    },
  ])
  const setup = await testRender(() => <App store={store} initialize={false} />, {
    width: 100,
    height: 30,
  })
  try {
    await setup.renderOnce()
    const collapsed = setup.captureCharFrame()
    expect(collapsed).not.toContain("omega")
    expect(collapsed).toContain("full command")
    const row = collapsed.split("\n").findIndex((line) => line.includes("echo"))
    await setup.mockMouse.click(8, row)
    await setup.renderOnce()
    const frame = setup.captureCharFrame()
    expect(frame).toContain("omega")
    expect(frame).toContain("alpha")
    expect(frame).toContain("beta")
    expect(frame.split("\n").find((line) => line.includes("echo"))).not.toContain("…")
  } finally {
    setup.renderer.destroy()
  }
})

test("cancelling a run clears running tool pulses", () => {
  const store = readyStore()
  store.messages([
    {
      id: "a1",
      role: "assistant",
      content: "",
      reasoning: "",
      streaming: true,
      createdAt: "2026-01-01T00:00:00Z",
      toolCalls: [{ ...tool(), status: "running", output: "" }],
      diffs: [],
      reasoningOpen: false,
    },
  ])
  store.cancelRunningTools()
  expect(store.messages()[0]?.toolCalls[0]?.status).toBe("cancelled")
})

test("60-column layout stays usable", async () => {
  const store = readyStore()
  store.messages([
    {
      id: "u1",
      role: "user",
      content: "Fix the failing test in math_utils.py",
      reasoning: "",
      streaming: false,
      createdAt: "2026-01-01T00:00:00Z",
      toolCalls: [],
      diffs: [],
      reasoningOpen: false,
    },
    {
      id: "a1",
      role: "assistant",
      content: "## Plan\n\nApply the minimal fix.",
      reasoning: "",
      streaming: false,
      createdAt: "2026-01-01T00:00:00Z",
      toolCalls: [tool()],
      diffs: [],
      reasoningOpen: false,
    },
  ])
  const setup = await testRender(() => <App store={store} initialize={false} />, {
    width: 60,
    height: 20,
  })
  try {
    await setup.renderOnce()
    const frame = setup.captureCharFrame()
    expect(frame).toContain("math_utils.py")
    expect(frame).toContain("Ask siwah")
    expect(frame).toMatchSnapshot("narrow-60x20")
    const transcript = setup.renderer.root.findDescendantById("transcript") as ScrollBoxRenderable
    transcript.scrollTo(0)
    await setup.renderOnce()
    expect(setup.captureCharFrame()).toContain("Fix the failing test")
  } finally {
    setup.renderer.destroy()
  }
})

test("connect wizard lists configured providers", async () => {
  const store = readyStore()
  store.messages([])
  store.auth({
    providers: [
      {
        provider: "opencode-go",
        scheme: "bearer",
        env: "OPENCODE_API_KEY",
        env_set: false,
        file_set: false,
      },
    ],
  })
  store.openDialog({ type: "connect" })
  const setup = await testRender(() => <App store={store} initialize={false} />, {
    width: 100,
    height: 30,
  })
  try {
    await setup.renderOnce()
    const frame = setup.captureCharFrame()
    expect(frame).toContain("opencode-go")
    expect(frame).toContain("needs OPENCODE_API_KEY")
    expect(frame).toContain("Select a configured provider")
  } finally {
    setup.renderer.destroy()
  }
})

test("model dialog lists models from the daemon catalog", async () => {
  const store = readyStore()
  store.messages([])
  store.models({
    providers: [
      {
        provider: "mock",
        models: [
          {
            id: "model-a",
            name: "Model A",
            context_window: 100_000,
            max_output: 8_000,
            supports_tools: true,
            supports_reasoning: true,
            native_api: "openai_chat",
          },
          {
            id: "model-b",
            name: "Model B",
            context_window: 200_000,
            max_output: 16_000,
            supports_tools: true,
            supports_reasoning: true,
            native_api: "openai_chat",
          },
        ],
      },
    ],
    roles: { main: "mock/model-a" },
  })
  store.openDialog({ type: "models" })
  const setup = await testRender(() => <App store={store} initialize={false} />, {
    width: 100,
    height: 30,
  })
  try {
    await setup.renderOnce()
    const frame = setup.captureCharFrame()
    expect(frame).toContain("model-a")
    expect(frame).toContain("model-b")
    expect(frame).toContain("mock · ctx")
  } finally {
    setup.renderer.destroy()
  }
})

test("session dialog keeps long titles and paths in columns", async () => {
  const store = readyStore()
  const cwd = "C:\\Users\\LENOVO\\projects\\siwah\\a-very-long-workspace-name"
  const previous = process.env.SIWAH_WORKSPACE
  process.env.SIWAH_WORKSPACE = cwd
  store.messages([])
  store.sessions([
    {
      id: "session_with_a_long_identifier",
      title: "Session with a very long title that must not wrap into the detail column",
      cwd,
      mode: "build",
      model: "mock/model-a",
      status: "active",
      created_at: "2026-01-01T00:00:00Z",
      updated_at: new Date(Date.now() - 2 * 60 * 60 * 1000).toISOString(),
    },
    {
      id: "other_project",
      title: "Other project session",
      cwd: "C:\\Users\\LENOVO\\projects\\another-repo",
      mode: "build",
      model: "mock/model-a",
      status: "active",
      created_at: "2026-01-01T00:00:00Z",
      updated_at: "2026-01-01T00:00:00Z",
    },
  ])
  store.openDialog({ type: "sessions" })
  const active = store.session()
  if (active) store.session({ ...active, cwd })
  const setup = await testRender(() => <App store={store} initialize={false} />, {
    width: 100,
    height: 30,
  })
  try {
    await setup.renderOnce()
    const frame = setup.captureCharFrame()
    expect(frame).toContain("Session with a very")
    expect(frame).toContain("2h ago · mock/model-a")
    expect(frame).not.toContain("Other project session")
    expect(store.projectSessions()).toHaveLength(1)
    expect(store.otherSessions()).toHaveLength(1)
  } finally {
    setup.renderer.destroy()
    if (previous === undefined) delete process.env.SIWAH_WORKSPACE
    else process.env.SIWAH_WORKSPACE = previous
  }
})

test("connect wizard offers to create the default provider catalog", async () => {
  const store = readyStore()
  store.messages([])
  store.auth({ providers: [] })
  store.openDialog({ type: "connect" })
  const setup = await testRender(() => <App store={store} initialize={false} />, {
    width: 100,
    height: 30,
  })
  try {
    await setup.renderOnce()
    const frame = setup.captureCharFrame()
    expect(frame).toContain("No providers are configured")
    expect(frame).toContain("Press Enter to create the default provider catalog")
  } finally {
    setup.renderer.destroy()
  }
})

test("checkpoint dialog lists a checkpoint", async () => {
  const store = readyStore()
  store.messages([])
  store.checkpoints([
    {
      id: "cp_1",
      label: "before edit",
      commit: "abc123",
      created_at: "2026-01-01T00:00:00Z",
    },
  ])
  store.openDialog({ type: "checkpoints" })
  const setup = await testRender(() => <App store={store} initialize={false} />, {
    width: 100,
    height: 32,
  })
  try {
    await setup.renderOnce()
    expect(setup.captureCharFrame()).toContain("before edit")
  } finally {
    setup.renderer.destroy()
  }
})

test("rewind dialog warns before restoring", async () => {
  const store = readyStore()
  store.messages([])
  store.openDialog({
    type: "rewind",
    checkpoint: {
      id: "cp_1",
      label: "before edit",
      commit: "abc123",
      created_at: "2026-01-01T00:00:00Z",
    },
  })
  const setup = await testRender(() => <App store={store} initialize={false} />, {
    width: 100,
    height: 32,
  })
  try {
    await setup.renderOnce()
    const frame = setup.captureCharFrame()
    expect(frame).toContain("Checkpoint: before edit")
    expect(frame).toContain("Enter restores")
  } finally {
    setup.renderer.destroy()
  }
})

test("top bar shows project session tabs with a close control", async () => {
  const store = readyStore()
  const cwd = "D:/work/siwah"
  store.sessions([
    {
      id: "ses_demo",
      title: "fix parser",
      cwd,
      model: "mock/model-a",
      mode: "build",
      status: "idle",
      created_at: "2026-01-01T00:00:00Z",
      updated_at: "2026-01-01T00:00:00Z",
      metadata: {},
      last_event_seq: 0,
    },
    {
      id: "ses_second",
      title: "second chat",
      cwd,
      model: "mock/model-a",
      mode: "build",
      status: "running",
      created_at: "2026-01-01T00:01:00Z",
      updated_at: "2026-01-01T00:01:00Z",
      metadata: {},
      last_event_seq: 0,
    },
  ])
  store.messages([])
  const setup = await testRender(() => <App store={store} initialize={false} />, {
    width: 100,
    height: 20,
  })
  try {
    await setup.renderOnce()
    const frame = setup.captureCharFrame()
    expect(frame).toContain("fix parser")
    expect(frame).toContain("second chat")
    expect(frame).toContain("×")
    expect(frame).toContain("siwah │")
  } finally {
    setup.renderer.destroy()
  }
})

test("user messages show pasted image chips", async () => {
  const store = readyStore()
  store.messages([
    {
      id: "u1",
      role: "user",
      content: "describe this",
      reasoning: "",
      streaming: false,
      createdAt: "2026-01-01T00:00:00Z",
      toolCalls: [],
      diffs: [],
      reasoningOpen: false,
      images: [{ id: "img_1", mime: "image/png", data: "YWJj", name: "pasted.png" }],
    },
  ])
  const setup = await testRender(() => <App store={store} initialize={false} />, {
    width: 100,
    height: 20,
  })
  try {
    await setup.renderOnce()
    const frame = setup.captureCharFrame()
    expect(frame).toContain("file")
    expect(frame).toContain("pasted.png")
    expect(frame).toContain("describe this")
  } finally {
    setup.renderer.destroy()
  }
})

test("mcp dialog lists servers and reconnect hint", async () => {
  const store = readyStore()
  store.mcpServers([
    {
      name: "bxnotes",
      transport: "stdio",
      description: "Benchmark notes",
      enabled: true,
      valid: true,
      status: "connected",
      tools: ["lookup_code"],
      latency_ms: 89,
    },
  ])
  store.messages([])
  store.openDialog({ type: "mcp" })
  const setup = await testRender(() => <App store={store} initialize={false} />, {
    width: 100,
    height: 24,
  })
  try {
    await setup.renderOnce()
    const frame = setup.captureCharFrame()
    expect(frame).toContain("mcp servers")
    expect(frame).toContain("bxnotes")
    expect(frame).toContain("connected")
    expect(frame).toContain("Enter reconnects")
  } finally {
    setup.renderer.destroy()
  }
})

test("task panel can be hidden and restored", async () => {
  const store = readyStore()
  store.todos(
    Array.from({ length: 20 }, (_, index) => ({
      id: String(index + 1),
      content: `task ${index + 1}`,
      status: index < 3 ? "done" : "pending",
    })),
  )
  store.messages([
    {
      id: "u1",
      role: "user",
      content: "work",
      reasoning: "",
      streaming: false,
      createdAt: "2026-01-01T00:00:00Z",
      toolCalls: [],
      diffs: [],
      reasoningOpen: false,
    },
  ])
  const setup = await testRender(() => <App store={store} initialize={false} />, {
    width: 120,
    height: 30,
  })
  try {
    await setup.renderOnce()
    const frame = setup.captureCharFrame()
    expect(frame).toContain("TASKS")
    expect(frame).toContain("task 1")
    const list = setup.renderer.root.findDescendantById("todo-list") as ScrollBoxRenderable
    list.scrollTo(Infinity)
    await setup.renderOnce()
    expect(setup.captureCharFrame()).toContain("task 20")
    store.toggleTodos()
    await setup.renderOnce()
    expect(setup.captureCharFrame()).not.toContain("TASKS")
    expect(setup.captureCharFrame()).toContain("tasks 3/20")
    expect(store.todosHidden()).toBe(true)
    store.toggleTodos()
    await setup.renderOnce()
    expect(setup.captureCharFrame()).toContain("TASKS")
    expect(store.todosHidden()).toBe(false)
  } finally {
    setup.renderer.destroy()
  }
})

test("edit_file renders one card with the diff embedded", async () => {
  const store = readyStore()
  store.messages([
    {
      id: "a1",
      role: "assistant",
      content: "",
      reasoning: "",
      streaming: false,
      createdAt: "2026-01-01T00:00:00Z",
      toolCalls: [
        {
          ...tool(),
          name: "edit_file",
          summary: "hello.py",
          output: "",
        },
      ],
      diffs: [
        {
          path: "hello.py",
          patch:
            "--- a/hello.py\n+++ b/hello.py\n@@ -1,2 +1,2 @@\n def add(a, b):\n-    return a - b\n+    return a + b\n",
        },
      ],
      reasoningOpen: false,
    },
  ])
  const setup = await testRender(() => <App store={store} initialize={false} />, {
    width: 100,
    height: 32,
  })
  try {
    await setup.renderOnce()
    const frame = setup.captureCharFrame()
    expect(frame).toContain("edit_file  hello.py")
    expect(frame.match(/hello\.py/g)?.length ?? 0).toBe(1)
    expect(frame).toContain("return a + b")
    expect(frame).not.toContain("write_file")
  } finally {
    setup.renderer.destroy()
  }
})
