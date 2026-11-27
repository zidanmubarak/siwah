import { expect, test } from "bun:test"
import { testRender } from "@opentui/solid"
import type { EventEnvelope, QuestionRequest, Session } from "../src/api/client"
import { TuiStore } from "../src/state"
import { resolveTheme } from "../src/theme"
import { App } from "../src/ui/App"

const question: QuestionRequest = {
  request_id: "q1",
  question: "Which tests should Siwah run?",
  options: [
    { label: "Unit", description: "Only local unit tests" },
    { label: "All", description: "The full test suite" },
  ],
  multiple: false,
  allow_custom: true,
}
const active: Session = {
  id: "ses_question",
  cwd: "D:/fixture",
  title: "Test questions",
  model: "mock/model",
  mode: "build",
  status: "running",
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
  metadata: {},
  last_event_seq: 0,
}
type Internals = {
  api: Record<string, unknown>
  handleEvent: (event: Partial<EventEnvelope>) => void
  startStream: (sessionId: string) => void
}
const internals = (store: TuiStore) => store as unknown as Internals
const event = (store: TuiStore, seq: number, type: string, data: Record<string, unknown>) =>
  internals(store).handleEvent({ type, data, seq, id: `q-event-${seq}`, session_id: active.id })

function ready() {
  const store = new TuiStore()
  store.theme(resolveTheme("siwah-dark", false))
  store.session(active)
  return store
}

test("terminal questions own keyboard input without losing the composer draft", async () => {
  const store = ready()
  const answers: (string | null)[] = []
  internals(store).api = {
    answerQuestion: async (id: string, requestId: string, answer: string | null) => {
      expect(id).toBe(active.id)
      expect(requestId).toMatch(/^q[123]$/)
      answers.push(answer)
      return { ok: true }
    },
  }
  const ui = await testRender(() => <App store={store} initialize={false} />, {
    width: 60,
    height: 24,
  })
  try {
    await ui.renderOnce()
    await ui.mockInput.typeText("Keep this draft")
    await ui.renderOnce()

    event(store, 1, "question.request", question)
    await ui.renderOnce()
    expect(ui.captureCharFrame()).toContain(question.question)
    expect(ui.captureCharFrame()).toContain("Unit")
    expect(ui.captureCharFrame()).toContain("waiting for your answer")
    ui.mockInput.pressArrow("down")
    await ui.renderOnce()
    expect(ui.captureCharFrame()).toContain("The full test suite")
    ui.mockInput.pressEnter()
    await ui.renderOnce()
    expect(answers).toEqual(["All"])

    event(store, 2, "question.request", { ...question, request_id: "q2" })
    await ui.renderOnce()
    ui.mockInput.pressTab()
    await ui.renderOnce()
    await ui.mockInput.typeText("Run smoke tests only")
    ui.mockInput.pressEnter()
    await ui.renderOnce()
    expect(answers).toEqual(["All", "Run smoke tests only"])

    event(store, 3, "question.request", { ...question, request_id: "q3", options: [] })
    await ui.renderOnce()
    expect(ui.captureCharFrame()).toContain("Type an answer")
    ui.mockInput.pressEscape()
    await Bun.sleep(20)
    await ui.renderOnce()
    expect(answers).toEqual(["All", "Run smoke tests only", null])
    expect(store.question()).toBeNull()
    expect(store.promptText()).toBe("Keep this draft")
    await ui.mockInput.typeText("!")
    expect(store.promptText()).toBe("Keep this draft!")
  } finally {
    await ui.dispose()
  }
})

test("session reload hydrates an unanswered question and never restores a resolved request", async () => {
  const store = ready()
  let resolveQuestion: (request: QuestionRequest) => void = () => {}
  let pending = Promise.resolve(question)
  internals(store).api = {
    session: async () => active,
    messages: async () => ({ messages: [] }),
    usage: async () => ({}),
    pendingQuestion: () => pending,
    files: async () => ({ files: [] }),
    commands: async () => ({ commands: [] }),
    checkpoints: async () => ({ checkpoints: [] }),
    plan: async () => ({ exists: false }),
  }
  internals(store).startStream = () => {}
  await store.openSession(active.id)
  expect(store.question()?.request_id).toBe("q1")
  pending = new Promise<QuestionRequest>((resolve) => {
    resolveQuestion = resolve
  })
  const loading = store.openSession(active.id)
  await Bun.sleep(0)
  event(store, 1, "question.resolved", { request_id: "q1" })
  resolveQuestion(question)
  await loading
  expect(store.question()).toBeNull()
})

test("multi-select questions toggle choices and submit newline-joined answers", async () => {
  const store = ready()
  const answers: (string | null)[] = []
  internals(store).api = {
    answerQuestion: async (_id: string, _requestId: string, answer: string | null) => {
      answers.push(answer)
      return { ok: true }
    },
  }
  event(store, 1, "question.request", {
    ...question,
    request_id: "qm",
    header: "Scope",
    multiple: true,
    allow_custom: false,
  })
  const ui = await testRender(() => <App store={store} initialize={false} />, {
    width: 80,
    height: 29,
  })
  try {
    await ui.renderOnce()
    expect(store.question()?.multiple).toBe(true)
    expect(store.question()?.options?.length).toBe(2)
    expect(ui.captureCharFrame()).toContain("Scope")
    expect(ui.captureCharFrame()).toContain("[ ] 1. Unit")
    ui.mockInput.pressKey(" ")
    ui.mockInput.pressArrow("down")
    ui.mockInput.pressKey(" ")
    ui.mockInput.pressEnter()
    await ui.renderOnce()
    expect(answers).toEqual(["Unit\nAll"])
  } finally {
    await ui.dispose()
  }
})

test("background results render as notices and raise a completion toast", async () => {
  const store = ready()
  const ui = await testRender(() => <App store={store} initialize={false} />, {
    width: 100,
    height: 30,
  })
  try {
    await ui.renderOnce()
    event(store, 1, "message.start", {
      message_id: "sys_background",
      role: "system",
      content: "Background result: child found the README",
    })
    await ui.renderOnce()
    expect(ui.captureCharFrame()).toContain("background result")
    expect(ui.captureCharFrame()).toContain("child found the README")
    event(store, 2, "background.result", {
      kind: "subagent",
      actor_id: "child",
      status: "completed",
      title: "explore the README",
    })
    await ui.renderOnce()
    expect(store.toasts().some((toast) => toast.text.includes("Subagent completed"))).toBe(true)
  } finally {
    await ui.dispose()
  }
})

test("the exit binding still cancels a run waiting for an answer", async () => {
  const store = ready()
  store.running(true)
  let aborts = 0
  store.abort = async () => {
    aborts += 1
  }
  event(store, 1, "question.request", question)
  const ui = await testRender(() => <App store={store} initialize={false} />, {
    width: 80,
    height: 29,
  })
  try {
    await ui.renderOnce()
    ui.mockInput.pressKey("c", { ctrl: true })
    await Bun.sleep(20)
    expect(aborts).toBe(1)
  } finally {
    await ui.dispose()
  }
})

test("a simultaneous permission prompt retains priority and its run cancel shortcut", async () => {
  const store = ready()
  store.running(true)
  let aborts = 0
  let answers = 0
  store.abort = async () => {
    aborts += 1
  }
  store.answerQuestion = async () => {
    answers += 1
  }
  event(store, 1, "question.request", question)
  event(store, 2, "permission.request", {
    request_id: "p1",
    tool: "bash",
    kind: "shell",
    preview: "Run checks",
    choices: ["once", "deny"],
  })
  const ui = await testRender(() => <App store={store} initialize={false} />, {
    width: 80,
    height: 29,
  })
  try {
    await ui.renderOnce()
    expect(ui.captureCharFrame()).toContain("permission required")
    expect(ui.captureCharFrame()).not.toContain(question.question)
    ui.mockInput.pressEscape()
    await Bun.sleep(20)
    expect(aborts).toBe(1)
    expect(answers).toBe(0)
  } finally {
    await ui.dispose()
  }
})
