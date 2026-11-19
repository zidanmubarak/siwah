import { expect, test } from "bun:test"
import { testRender } from "@opentui/solid"

import { TuiStore } from "../src/state"
import { resolveTheme } from "../src/theme"
import { setActiveStore, setActiveTheme } from "../src/ui/context"
import { PromptBox } from "../src/ui/PromptBox"

test("ctrl+n inserts a newline in the prompt", async () => {
  setActiveTheme(resolveTheme("siwah-dark", false))
  setActiveStore(new TuiStore())
  let value = ""
  const setup = await testRender(
    () => (
      <PromptBox
        mode="build"
        stats="ctx 1% · 1k tok · $0.000100"
        onChange={(next) => {
          value = next
        }}
        onSubmit={() => false}
        onReady={() => {}}
      />
    ),
    { width: 80, height: 10 },
  )
  try {
    await setup.renderOnce()
    setup.mockInput.typeText("line one")
    setup.mockInput.pressKey("n", { ctrl: true })
    setup.mockInput.typeText("line two")
    await setup.renderOnce()
    expect(value).toContain("line one")
    expect(value).toContain("line two")
    expect(value).toContain("\n")
  } finally {
    setup.renderer.destroy()
  }
})
