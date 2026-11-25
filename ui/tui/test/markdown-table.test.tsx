import { expect, test } from "bun:test"
import { RGBA } from "@opentui/core"
import { testRender } from "@opentui/solid"

import { TuiStore } from "../src/state"
import { resolveTheme } from "../src/theme"
import { setActiveStore, setActiveTheme } from "../src/ui/context"
import { MarkdownText } from "../src/ui/MarkdownText"

test("markdown uses the available width and colors inline code without a background", async () => {
  const store = new TuiStore()
  const theme = resolveTheme("siwah-dark", false)
  store.theme(theme)
  setActiveStore(store)
  setActiveTheme(theme)
  const prefix = `Use ${"word ".repeat(17)}`
  const setup = await testRender(
    () => (
      <box backgroundColor={theme.background} width="100%" height="100%">
        <MarkdownText content={`${prefix}\`work/\` and \`../hidden/\`.`} streaming={false} />
      </box>
    ),
    { width: 120, height: 10 },
  )
  try {
    await setup.renderOnce()
    expect(setup.captureCharFrame().split("\n")[0]).toContain(`${prefix}work/ and ../hidden/.`)
    const spans = setup.captureSpans().lines[0].spans
    const background = spans.find((span) => span.text.includes("Use "))?.bg
    expect(background).toBeDefined()
    for (const token of ["work/", "../hidden/"]) {
      const span = spans.find((entry) => entry.text === token)
      expect(span?.fg.equals(RGBA.fromHex(theme.user))).toBe(true)
      expect(background && span?.bg.equals(background)).toBe(true)
    }
    await setup.resize(60, 10)
    await setup.renderOnce()
    const compact = setup.captureCharFrame().split("\n")
    expect(compact[0]).not.toContain("../hidden/")
    expect(compact[1]).toContain("work/ and ../hidden/.")
  } finally {
    setup.renderer.destroy()
  }
})

test("markdown headings use the available transcript width", async () => {
  const store = new TuiStore()
  const theme = resolveTheme("siwah-dark", false)
  store.theme(theme)
  setActiveStore(store)
  setActiveTheme(theme)
  const heading = `# ${"heading ".repeat(14)}TAIL`
  const setup = await testRender(
    () => (
      <box backgroundColor={theme.background} width="100%" height="100%">
        <MarkdownText content={heading} streaming={false} />
      </box>
    ),
    { width: 140, height: 10 },
  )
  try {
    await setup.renderOnce()
    expect(setup.captureCharFrame().split("\n")[0]).toContain("TAIL")
  } finally {
    setup.renderer.destroy()
  }
})

for (const name of ["siwah-light", "mono"] as const) {
  test(`inline code keeps the message background in ${name}`, async () => {
    const store = new TuiStore()
    const theme = resolveTheme(name, false)
    store.theme(theme)
    setActiveStore(store)
    setActiveTheme(theme)
    const setup = await testRender(
      () => (
        <box
          backgroundColor={theme.background === "transparent" ? undefined : theme.background}
          width="100%"
          height="100%"
        >
          <MarkdownText content={"Use `work/` now."} streaming={false} />
        </box>
      ),
      { width: 80, height: 6 },
    )
    try {
      await setup.renderOnce()
      expect(setup.captureCharFrame().split("\n")[0]).toContain("Use work/ now.")
      const spans = setup.captureSpans().lines[0].spans
      const parent = spans.find((span) => span.text.includes("Use "))
      const code = spans.find((span) => span.text === "work/")
      // Mono has no accent color, so the renderer may merge the inline code
      // with the surrounding text into one span.
      if (name !== "mono") expect(code).toBeDefined()
      if (code) {
        expect(code.fg.equals(RGBA.fromHex(theme.user))).toBe(true)
        const sameBackground =
          code.bg && parent?.bg ? code.bg.equals(parent.bg) : code.bg === parent?.bg
        expect(sameBackground).toBe(true)
      }
    } finally {
      setup.renderer.destroy()
    }
  })
}

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
