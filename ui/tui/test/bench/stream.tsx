/**
 * Render-only benchmark: streaming updates, input echo and resize.
 * Does not measure provider tokens, SSE/reducer latency, or native terminals.
 * Run with `bun run bench` (the script uses --conditions=browser).
 */

import { LinearScrollAccel, ScrollBoxRenderable } from "@opentui/core"
import { testRender } from "@opentui/solid"

import type { Session } from "../../src/api/client"
import { TuiStore } from "../../src/state"
import { resolveTheme } from "../../src/theme"
import { App } from "../../src/ui/App"

const streamText =
  "## Plan\n\nInspect the failing test, fix the subtraction, and run pytest.\n\n" +
  "- read math_utils.py\n- run pytest\n\n" +
  "The implementation returns a - b while the test expects a + b.\n".repeat(12)

function setupStore(): TuiStore {
  const store = new TuiStore()
  store.theme(resolveTheme("siwah-dark", false))
  const session: Session = {
    id: "ses_bench",
    title: "stream benchmark",
    cwd: "D:/work/siwah",
    model: "mock/model-a",
    mode: "build",
    status: "running",
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    metadata: {},
    last_event_seq: 0,
  }
  store.session(session)
  store.connection("online")
  store.messages([
    {
      id: "assistant",
      role: "assistant",
      content: "",
      reasoning: "",
      streaming: true,
      createdAt: "2026-01-01T00:00:00Z",
      toolCalls: [],
      diffs: [],
      reasoningOpen: false,
    },
  ])
  return store
}

function percentile(values: number[], p: number): number {
  if (values.length === 0) return 0
  const sorted = [...values].sort((left, right) => left - right)
  const index = Math.min(sorted.length - 1, Math.ceil((p / 100) * sorted.length) - 1)
  return sorted[index] ?? 0
}

const started = performance.now()
const store = setupStore()
const setup = await testRender(() => <App store={store} initialize={false} />, {
  width: 120,
  height: 34,
})
await setup.renderOnce()
const startupMs = performance.now() - started

const updateTimes: number[] = []
const changedCells: number[] = []
const chunkSize = 4
const chunks = Math.ceil(streamText.length / chunkSize)
let content = ""
for (let index = 0; index < streamText.length; index += chunkSize) {
  content += streamText.slice(index, index + chunkSize)
  const before = performance.now()
  store.messages((current) =>
    current.map((message) => (message.id === "assistant" ? { ...message, content } : message)),
  )
  await setup.renderOnce()
  updateTimes.push(performance.now() - before)
  changedCells.push(setup.getNativeStats().cellsUpdated)
}
const streamMs = updateTimes.reduce((sum, value) => sum + value, 0)

const streamFrameContainsText = setup.captureCharFrame().includes("implementation")
if (!streamFrameContainsText) {
  setup.renderer.destroy()
  throw new Error("Streaming text did not appear before message.end")
}
const output10kStart = performance.now()
store.messages([
  {
    id: "assistant",
    role: "assistant",
    content: "Tool output is summarized; the pager handles the rest.",
    reasoning: "",
    streaming: false,
    createdAt: "2026-01-01T00:00:00Z",
    toolCalls: [
      {
        id: "call_big",
        name: "bash",
        summary: '{"command":"generate 10000 lines"}',
        status: "ok",
        output: Array.from({ length: 10_000 }, (_, index) => `line ${index + 1}`).join("\n"),
        error: null,
        durationMs: 20,
      },
    ],
    diffs: [],
    reasoningOpen: false,
  },
])
await setup.renderOnce()
const output10kMs = performance.now() - output10kStart
const expandedStart = performance.now()
store.toggleTool("call_big")
await setup.renderOnce()
const expanded10kMs = performance.now() - expandedStart
// Restore a deterministic collapsed state before measuring composer input.
// Bounded expanded content is asserted separately in component regression tests.
store.toggleTool("call_big")
await setup.renderOnce()

const echoStart = performance.now()
setup.mockInput.typeText("hello")
await setup.renderOnce()
const echoMs = performance.now() - echoStart

const resizeStart = performance.now()
await setup.resize(60, 20)
await setup.renderOnce()
const resizeMs = performance.now() - resizeStart

// Exercise the old 240-line boundary without throwing away the prefix. Keep the
// benchmark separate from model-token throughput and provider/network timings.
const longAnswerLines = 600
let longContent = Array.from(
  { length: longAnswerLines },
  (_, index) => `Paragraph ${index + 1}: stable completed content.`,
).join("\n")
store.messages([
  {
    id: "long-answer",
    role: "assistant",
    content: longContent,
    reasoning: "",
    streaming: true,
    createdAt: "",
    toolCalls: [],
    diffs: [],
    reasoningOpen: false,
  },
])
await setup.renderOnce()
const longUpdateTimes: number[] = []
for (let index = 0; index < 60; index += 1) {
  longContent += " next"
  const before = performance.now()
  store.messages((current) => current.map((message) => ({ ...message, content: longContent })))
  await setup.renderOnce()
  longUpdateTimes.push(performance.now() - before)
}
const completionStart = performance.now()
store.messages((current) => current.map((message) => ({ ...message, streaming: false })))
await setup.renderOnce()
const longCompletionMs = performance.now() - completionStart

const finishedMessage = store.messages()[0]
const historyTurns = 250
store.messages(
  Array.from({ length: historyTurns }, (_, turn) => ({
    ...finishedMessage,
    id: `history-${turn}`,
    content: `Turn ${turn + 1}\n\n${streamText}`,
  })),
)
await setup.renderOnce()
const transcript = setup.renderer.root.findDescendantById("transcript")
if (!(transcript instanceof ScrollBoxRenderable)) throw new Error("Transcript was not rendered")
async function measureScroll(box: ScrollBoxRenderable) {
  box.scrollTo(Infinity)
  await setup.renderOnce()
  const start = box.scrollTop
  const times: number[] = []
  for (let frame = 0; frame < 60; frame += 1) {
    const before = performance.now()
    await setup.mockMouse.scroll(5, 5, "up")
    await setup.renderOnce()
    times.push(performance.now() - before)
  }
  return { rows: start - box.scrollTop, p95Ms: Math.round(percentile(times, 95) * 100) / 100 }
}
const scroll = await measureScroll(transcript)
transcript.scrollAcceleration = new LinearScrollAccel()
const linearScroll = await measureScroll(transcript)
if (scroll.rows <= linearScroll.rows) throw new Error("Transcript scroll did not accelerate")

const result = {
  measurement: "virtual-buffer rendering only; character chunks are not model tokens",
  screen: "120x34 -> 60x20",
  startupToFirstFrameMs: Math.round(startupMs * 100) / 100,
  updates: updateTimes.length,
  streamChars: streamText.length,
  streamMs: Math.round(streamMs * 100) / 100,
  charsPerSecond: Math.round((streamText.length / streamMs) * 1000 * 100) / 100,
  p50Ms: Math.round(percentile(updateTimes, 50) * 1000) / 1000,
  p95Ms: Math.round(percentile(updateTimes, 95) * 1000) / 1000,
  p99Ms: Math.round(percentile(updateTimes, 99) * 1000) / 1000,
  maxMs: Math.round(Math.max(...updateTimes) * 1000) / 1000,
  averageCellsUpdated:
    Math.round((changedCells.reduce((sum, value) => sum + value, 0) / changedCells.length) * 100) /
    100,
  maxCellsUpdated: Math.max(...changedCells),
  inputEchoMs: Math.round(echoMs * 100) / 100,
  output10kMs: Math.round(output10kMs * 100) / 100,
  expanded10kMs: Math.round(expanded10kMs * 100) / 100,
  resizeMs: Math.round(resizeMs * 100) / 100,
  longAnswerLines,
  longAnswerScreen: "60x20",
  longAnswerP95Ms: Math.round(percentile(longUpdateTimes, 95) * 1000) / 1000,
  longCompletionMs: Math.round(longCompletionMs * 100) / 100,
  historyTurns,
  scroll,
  linearScroll,
  chunks,
}

setup.renderer.destroy()
console.log(JSON.stringify({ ...result, streamFrameContainsText }, null, 2))
