import { expect, test } from "bun:test"
import { createRenderEffect, createRoot, createSignal } from "solid-js"

test("TUI tests use a reactive Solid runtime rather than the server runtime", () => {
  const seen: number[] = []
  const dispose = createRoot((dispose) => {
    const [value, setValue] = createSignal(0)
    createRenderEffect(() => seen.push(value()))
    setValue(1)
    return dispose
  })
  expect(seen).toEqual([0, 1])
  dispose()
})
