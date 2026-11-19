import { expect, test } from "bun:test"
import { testRender } from "@opentui/solid"
import { createSignal, Show } from "solid-js"

test("native JSX and conditional branches react to client signals", async () => {
  const [value, setValue] = createSignal("")
  const setup = await testRender(
    () => (
      <box>
        <text>{value()}</text>
        <Show when={value()}>
          <text>visible</text>
        </Show>
      </box>
    ),
    { width: 60, height: 5 },
  )
  try {
    await setup.renderOnce()
    setValue("new value")
    await setup.renderOnce()
    expect(setup.captureCharFrame()).toContain("new value")
    expect(setup.captureCharFrame()).toContain("visible")
  } finally {
    setup.renderer.destroy()
  }
})
