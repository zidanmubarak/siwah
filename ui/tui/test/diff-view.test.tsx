import { expect, test } from "bun:test"
import { type Renderable, RGBA, TextRenderable } from "@opentui/core"
import { testRender } from "@opentui/solid"

import { TuiStore } from "../src/state"
import { baseTheme } from "../src/theme"
import { setActiveStore, setActiveTheme } from "../src/ui/context"
import { DiffView } from "../src/ui/DiffView"

function nodes(root: Renderable): Renderable[] {
  return root.getChildren().flatMap((child) => [child, ...nodes(child)])
}

for (const width of [80, 140]) {
  test(`diff has padding, muted row backgrounds and icon-free headers at ${width} columns`, async () => {
    const store = new TuiStore()
    const theme = baseTheme("siwah-dark")
    store.theme(theme)
    setActiveStore(store)
    setActiveTheme(theme)
    const setup = await testRender(
      () => (
        <DiffView
          path="math_utils.py"
          patch={
            "--- a/math_utils.py\n+++ b/math_utils.py\n@@ -1 +1 @@\n-return a - b\n+return a + b\n"
          }
        />
      ),
      { width, height: 12 },
    )
    try {
      await setup.renderOnce()
      const frame = setup.captureCharFrame()
      expect(frame).toContain(width >= 120 ? "split" : "unified")
      const header = frame.split("\n").find((line) => line.includes("math_utils.py"))
      expect(header).not.toMatch(/[✓◇▾▸●]/)
      const spans = setup.captureSpans().lines.flatMap((line) => line.spans)
      for (const [signColor, bg] of [
        [theme.success, theme.diffAddedBg],
        [theme.error, theme.diffRemovedBg],
      ]) {
        // The row is highlighted; the code keeps syntax colors rather than
        // turning every character into the add/remove foreground.
        const code = spans.filter(
          (item) => item.bg.equals(RGBA.fromHex(bg)) && /[a-z]/.test(item.text),
        )
        expect(code.map((item) => item.text).join("")).toContain("return")
        expect(code.every((item) => item.fg.equals(RGBA.fromHex(theme.text)))).toBe(true)
        const sign = spans.find(
          (item) => item.fg.equals(RGBA.fromHex(signColor)) && /^[+-] /.test(item.text),
        )
        expect(sign).toBeDefined()
      }
      const panel = setup.renderer.root.getChildren()[0]
      if (!panel) throw new Error("Diff panel is missing")
      const content = nodes(panel).filter(
        (node): node is TextRenderable => node instanceof TextRenderable,
      )
      for (const text of content) {
        expect(text.x).toBeGreaterThan(panel.x)
        expect(text.y).toBeGreaterThan(panel.y)
        expect(text.x + text.width).toBeLessThan(panel.x + panel.width)
        expect(text.y + text.height).toBeLessThan(panel.y + panel.height)
      }
    } finally {
      setup.renderer.destroy()
    }
  })
}

test("added and removed diff lines paint the whole row", async () => {
  const store = new TuiStore()
  const theme = baseTheme("siwah-dark")
  store.theme(theme)
  setActiveStore(store)
  setActiveTheme(theme)
  const setup = await testRender(
    () => (
      <DiffView
        path="math_utils.py"
        patch={
          "--- a/math_utils.py\n+++ b/math_utils.py\n@@ -1,2 +1,2 @@\n def add(a, b):\n-    return a - b\n+    return a + b\n"
        }
      />
    ),
    { width: 80, height: 12 },
  )
  try {
    await setup.renderOnce()
    const frame = setup.captureSpans()
    const lines = frame.lines.map((line) => line.spans.map((span) => span.text).join(""))
    const added = frame.lines[lines.findIndex((line) => line.includes("return a + b"))]
    const removed = frame.lines[lines.findIndex((line) => line.includes("return a - b"))]
    expect(added).toBeDefined()
    expect(removed).toBeDefined()
    expect(added?.spans.some((span) => span.bg.equals(RGBA.fromHex(theme.diffAddedBg)))).toBe(true)
    expect(removed?.spans.some((span) => span.bg.equals(RGBA.fromHex(theme.diffRemovedBg)))).toBe(
      true,
    )
  } finally {
    setup.renderer.destroy()
  }
})

test("collapsed long diffs still show changed rows with backgrounds", async () => {
  const store = new TuiStore()
  const theme = baseTheme("siwah-dark")
  store.theme(theme)
  setActiveStore(store)
  setActiveTheme(theme)
  const patch = [
    "--- a/big.py",
    "+++ b/big.py",
    "@@ -1,4 +1,6 @@",
    " context one",
    "-old line",
    "+new line one",
    "+new line two",
    "+new line three",
    " context two",
  ].join("\n")
  const setup = await testRender(() => <DiffView path="big.py" patch={patch} />, {
    width: 100,
    height: 20,
  })
  try {
    await setup.renderOnce()
    const frame = setup.captureSpans()
    const text = frame.lines.map((line) => line.spans.map((span) => span.text).join(""))
    expect(text.join("\n")).toContain("more lines")
    const added = frame.lines[text.findIndex((line) => line.includes("new line one"))]
    const removed = frame.lines[text.findIndex((line) => line.includes("old line"))]
    expect(added?.spans.some((span) => span.bg.equals(RGBA.fromHex(theme.diffAddedBg)))).toBe(true)
    expect(removed?.spans.some((span) => span.bg.equals(RGBA.fromHex(theme.diffRemovedBg)))).toBe(
      true,
    )
  } finally {
    setup.renderer.destroy()
  }
})

test("expanded diffs navigate hunks without closing the patch", async () => {
  const store = new TuiStore()
  const theme = baseTheme("siwah-dark")
  store.theme(theme)
  setActiveStore(store)
  setActiveTheme(theme)
  const setup = await testRender(
    () => (
      <scrollbox id="patch-scroll" width="100%" height="100%">
        <DiffView
          path="parser.ts"
          expanded
          patch={"@@ -1 +1 @@\n-old one\n+new one\n@@ -7 +7 @@\n-old two\n+new two"}
        />
      </scrollbox>
    ),
    { width: 90, height: 24 },
  )
  try {
    await setup.renderOnce()
    const initial = setup.captureCharFrame().split("\n")
    const y = initial.findIndex((line) => line.includes("Hunk 1/2"))
    expect(y).toBeGreaterThanOrEqual(0)
    await setup.mockMouse.click(initial[y].indexOf("] next") + 1, y)
    await setup.renderOnce()
    expect(setup.captureCharFrame()).toContain("Hunk 2/2")
    setup.mockInput.pressKey("[")
    await setup.renderOnce()
    expect(setup.captureCharFrame()).toContain("Hunk 1/2")
    expect(setup.captureCharFrame()).toContain("+ new two")
  } finally {
    setup.renderer.destroy()
  }
})

test("expanded split diffs navigate hunks", async () => {
  const store = new TuiStore()
  const theme = baseTheme("siwah-dark")
  store.theme(theme)
  setActiveStore(store)
  setActiveTheme(theme)
  const setup = await testRender(
    () => (
      <scrollbox id="patch-scroll" width="100%" height="100%">
        <DiffView
          path="parser.ts"
          expanded
          patch={"@@ -1 +1 @@\n-old one\n+new one\n@@ -7 +7 @@\n-old two\n+new two"}
        />
      </scrollbox>
    ),
    { width: 140, height: 24 },
  )
  try {
    await setup.renderOnce()
    const initial = setup.captureCharFrame()
    expect(initial).toContain("split")
    const lines = initial.split("\n")
    const y = lines.findIndex((line) => line.includes("Hunk 1/2"))
    expect(y).toBeGreaterThanOrEqual(0)
    await setup.mockMouse.click(lines[y].indexOf("] next") + 1, y)
    await setup.renderOnce()
    expect(setup.captureCharFrame()).toContain("Hunk 2/2")
    setup.mockInput.pressKey("[")
    await setup.renderOnce()
    expect(setup.captureCharFrame()).toContain("Hunk 1/2")
  } finally {
    setup.renderer.destroy()
  }
})
