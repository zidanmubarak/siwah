import { expect, test } from "bun:test"

import { classifyMarkdown, markdownBlocks, parseInline } from "../src/ui/MarkdownText"

test("code fences toggle deterministically without leaking state", () => {
  const rows = classifyMarkdown("before\n```ts\nconst x = 1\n```\nafter\n")
  expect(rows[0]).toEqual({ text: "before", code: false })
  expect(rows[1]).toEqual({ text: "const x = 1", code: true })
  expect(rows[2]).toEqual({ text: "after", code: false })

  const second = classifyMarkdown("plain\n")
  expect(second[0]?.code).toBe(false)
})

test("pipe tables become table blocks", () => {
  const blocks = markdownBlocks(
    classifyMarkdown("| Requirement | Status |\n|---|---|\n| Server | OK |\n| Client | OK |\n"),
  )
  expect(blocks[0]).toEqual({
    kind: "table",
    header: ["Requirement", "Status"],
    rows: [
      ["Server", "OK"],
      ["Client", "OK"],
    ],
    align: ["left", "left"],
  })
})

test("table alignment markers and separator-less body rows", () => {
  const blocks = markdownBlocks(
    classifyMarkdown("Name | Score | Note\n:--- | :---: | ---:\nA | 10 | ok\nB | 9 | fine\n"),
  )
  expect(blocks[0]).toEqual({
    kind: "table",
    header: ["Name", "Score", "Note"],
    rows: [
      ["A", "10", "ok"],
      ["B", "9", "fine"],
    ],
    align: ["left", "center", "right"],
  })
})

test("fenced code groups into one code block", () => {
  const blocks = markdownBlocks(classifyMarkdown("before\n```py\na = 1\nb = 2\n```\nafter\n"))
  expect(blocks).toEqual([
    { kind: "line", text: "before", code: false },
    { kind: "code", lines: ["a = 1", "b = 2"] },
    { kind: "line", text: "after", code: false },
    { kind: "line", text: "", code: false },
  ])
})

test("setext headings are recognized", () => {
  const blocks = markdownBlocks(classifyMarkdown("Title\n=====\n\nSubtitle\n--------\n"))
  expect(blocks[0]).toEqual({ kind: "heading", text: "Title", depth: 1 })
  expect(blocks[2]).toEqual({ kind: "heading", text: "Subtitle", depth: 2 })
})

test("inline markdown handles emphasis, code, strike and links", () => {
  expect(parseInline("*harness data*")).toEqual([{ text: "harness data", kind: "italic" }])
  expect(parseInline("**bold**")).toEqual([{ text: "bold", kind: "bold" }])
  expect(parseInline("`code`")).toEqual([{ text: "code", kind: "code" }])
  expect(parseInline("~~gone~~")).toEqual([{ text: "gone", kind: "strike" }])
  expect(parseInline("[docs](https://example.test)")).toEqual([
    { text: "docs", kind: "link", href: "https://example.test" },
  ])
})

test("inline markdown handles images, autolinks and bare urls", () => {
  expect(parseInline("![diagram](https://example.test/a.png)")).toEqual([
    { text: "diagram", kind: "image", href: "https://example.test/a.png" },
  ])
  expect(parseInline("<https://example.test>")).toEqual([
    { text: "https://example.test", kind: "link", href: "https://example.test" },
  ])
  expect(parseInline("see https://example.test/docs now")).toEqual([
    { text: "see ", kind: "text" },
    { text: "https://example.test/docs", kind: "link", href: "https://example.test/docs" },
    { text: " now", kind: "text" },
  ])
})

test("inline markdown tolerates missing text", () => {
  expect(parseInline(undefined)).toEqual([])
  expect(parseInline(null)).toEqual([])
})

test("inline markdown keeps surrounding text", () => {
  expect(parseInline("Level 0-1 changes *harness data* only.")).toEqual([
    { text: "Level 0-1 changes ", kind: "text" },
    { text: "harness data", kind: "italic" },
    { text: " only.", kind: "text" },
  ])
  expect(parseInline("plain text")).toEqual([{ text: "plain text", kind: "text" }])
})
