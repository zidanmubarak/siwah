import { expect, spyOn, test } from "bun:test"
import { testRender } from "@opentui/solid"
import { createRoot, createSignal } from "solid-js"
import { animationsEnabled, Pulse, useReveal } from "../src/ui/Motion"

const wait = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms))

test("logo reveal is bounded, completes and cleans up its timer", async () => {
  const intervals = spyOn(globalThis, "setInterval")
  const clears = spyOn(globalThis, "clearInterval")
  let dispose = () => {}
  try {
    const visible = createRoot((cleanup) => {
      dispose = cleanup
      return useReveal(28, () => true)
    })
    expect(visible()).toBe(0)
    await wait(100)
    expect(visible()).toBeGreaterThan(0)
    expect(visible()).toBeLessThan(28)
    await wait(580)
    expect(visible()).toBe(28)
    const index = intervals.mock.calls.findIndex((call) => call[1] === 30)
    expect(index).toBeGreaterThanOrEqual(0)
    const handle = intervals.mock.results[index]?.value
    expect(clears).toHaveBeenCalledWith(handle)
    expect(intervals.mock.calls.filter((call) => call[1] === 30)).toHaveLength(1)
  } finally {
    dispose()
    intervals.mockRestore()
    clears.mockRestore()
  }
})

test("unmounting a reveal early clears its timer; disabled reveal allocates none", () => {
  const intervals = spyOn(globalThis, "setInterval")
  const clears = spyOn(globalThis, "clearInterval")
  let dispose = () => {}
  try {
    createRoot((cleanup) => {
      dispose = cleanup
      useReveal(28, () => true)
    })
    const handle = intervals.mock.results[0]?.value
    dispose()
    expect(clears).toHaveBeenCalledWith(handle)
    const count = intervals.mock.calls.length
    createRoot((cleanup) => {
      dispose = cleanup
      expect(useReveal(28, () => false)()).toBe(28)
    })
    expect(intervals.mock.calls.length).toBe(count)
  } finally {
    dispose()
    intervals.mockRestore()
    clears.mockRestore()
  }
})

test("one working pulse advances, stops when inactive and cleans up on unmount", async () => {
  const intervals = spyOn(globalThis, "setInterval")
  const clears = spyOn(globalThis, "clearInterval")
  const [active, setActive] = createSignal(true)
  const setup = await testRender(() => <Pulse active={active()} color="#ffffff" animated />, {
    width: 5,
    height: 2,
  })
  try {
    await setup.renderOnce()
    const initial = setup.captureCharFrame()
    await wait(110)
    await setup.renderOnce()
    expect(setup.captureCharFrame()).not.toBe(initial)
    const index = intervals.mock.calls.findIndex((call) => call[1] === 90)
    expect(index).toBeGreaterThanOrEqual(0)
    setActive(false)
    expect(clears).toHaveBeenCalledWith(intervals.mock.results[index]?.value)
    const stopped = setup.captureCharFrame()
    await wait(110)
    await setup.renderOnce()
    expect(setup.captureCharFrame()).toBe(stopped)
    setActive(true)
  } finally {
    setup.renderer.destroy()
    const last = intervals.mock.calls.map((call) => call[1]).lastIndexOf(90)
    expect(clears).toHaveBeenCalledWith(intervals.mock.results[last]?.value)
    intervals.mockRestore()
    clears.mockRestore()
  }
})

test("motion respects NO_COLOR and the deterministic test environment", () => {
  expect(animationsEnabled()).toBe(false)
  const previous = process.env.NO_COLOR
  process.env.NO_COLOR = "1"
  try {
    expect(animationsEnabled()).toBe(false)
  } finally {
    if (previous === undefined) delete process.env.NO_COLOR
    else process.env.NO_COLOR = previous
  }
})
