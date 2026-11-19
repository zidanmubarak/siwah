import { expect, test } from "bun:test"
import { testRender } from "@opentui/solid"

import { TuiStore } from "../src/state"
import { resolveTheme } from "../src/theme"
import { setActiveStore, setActiveTheme } from "../src/ui/context"
import { MarkdownText } from "../src/ui/MarkdownText"

test("markdown tables render as bordered boxes", async () => {
  const store = new TuiStore()
  const theme = resolveTheme("siwah-dark", false)
  store.theme(theme)
  setActiveStore(store)
  setActiveTheme(theme)
  const setup = await testRender(
    () => (
      <MarkdownText
        content={"| Requirement | Status |\n|---|---|\n| Server | OK |\n| Client | OK |"}
        streaming={false}
      />
    ),
    { width: 100, height: 20 },
  )
  try {
    await setup.renderOnce()
    const frame = setup.captureCharFrame()
    expect(frame).toContain("─")
    expect(frame).toContain("Requirement")
    expect(frame).toContain("Server")
    expect(frame).not.toContain("|---|")
  } finally {
    setup.renderer.destroy()
  }
})

test("markdown tables wrap cells instead of clipping the border", async () => {
  const store = new TuiStore()
  const theme = resolveTheme("siwah-dark", false)
  store.theme(theme)
  setActiveStore(store)
  setActiveTheme(theme)
  const setup = await testRender(
    () => (
      <MarkdownText
        content={
          "| Key | Description |\n|---|---|\n| alpha | A long description that must wrap inside the table cell rather than push the right border off screen. |"
        }
        streaming={false}
      />
    ),
    { width: 60, height: 20 },
  )
  try {
    await setup.renderOnce()
    const frame = setup.captureCharFrame()
    expect(frame).toContain("alpha")
    expect(frame).toContain("─")
    for (const line of frame.split("\n")) {
      expect(line.length).toBeLessThanOrEqual(60)
    }
  } finally {
    setup.renderer.destroy()
  }
})

test("common markdown forms render without literal markers", async () => {
  const store = new TuiStore()
  const theme = resolveTheme("siwah-dark", false)
  store.theme(theme)
  setActiveStore(store)
  setActiveTheme(theme)
  const content = [
    "# Heading",
    "Setext title",
    "============",
    "",
    "> quoted text",
    "",
    "- first",
    "  - nested",
    "- [x] done",
    "",
    "![diagram](https://example.test/a.png)",
    "",
    "`inline code` and **bold** and ~~gone~~",
    "",
    "```py",
    "print('hello')",
    "```",
    "",
    "---",
    "",
    "| Left | Center | Right |",
    "|:---|:---:|---:|",
    "| a | b | c |",
  ].join("\n")
  const setup = await testRender(() => <MarkdownText content={content} streaming={false} />, {
    width: 80,
    height: 32,
  })
  try {
    await setup.renderOnce()
    const frame = setup.captureCharFrame()
    expect(frame).toContain("Heading")
    expect(frame).toContain("Setext title")
    expect(frame).toContain("quoted text")
    expect(frame).toContain("nested")
    expect(frame).toContain("☑")
    expect(frame).toContain("▧ diagram")
    expect(frame).toContain("print('hello')")
    expect(frame).toContain("Left")
    expect(frame).not.toContain("|---|")
    expect(frame).not.toContain("**bold**")
    expect(frame).not.toContain("~~gone~~")
  } finally {
    setup.renderer.destroy()
  }
})
