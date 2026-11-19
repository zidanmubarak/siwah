import { expect, test } from "bun:test"
import { mkdtempSync, writeFileSync } from "node:fs"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { testRender } from "@opentui/solid"
import { TuiStore } from "../src/state"
import { resolveTheme } from "../src/theme"
import { setActiveStore, setActiveTheme } from "../src/ui/context"
import { PromptBox } from "../src/ui/PromptBox"

test("pasted image path becomes an image token", async () => {
  const dir = mkdtempSync(join(tmpdir(), "siwah-img-"))
  const file = join(dir, "Screenshot 2026-11-16 114848.png")
  writeFileSync(
    file,
    Buffer.from(
      "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=",
      "base64",
    ),
  )
  setActiveTheme(resolveTheme("siwah-dark", false))
  setActiveStore(new TuiStore())
  let value = ""
  const setup = await testRender(
    () => (
      <PromptBox
        mode="build"
        stats="ctx"
        onChange={(next) => {
          value = next
        }}
        onSubmit={() => false}
        onReady={() => {}}
      />
    ),
    { width: 100, height: 12 },
  )
  try {
    await setup.renderOnce()
    await setup.mockInput.pasteBracketedText(file)
    await setup.renderOnce()
    const frame = setup.captureCharFrame()
    expect(value).toContain("[Image 1]")
    expect(value).not.toContain(file)
    expect(frame).toContain("[Image 1]")
  } finally {
    setup.renderer.destroy()
  }
})
