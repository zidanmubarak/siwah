import { expect, test } from "bun:test"
import { mkdtempSync, rmSync } from "node:fs"
import { tmpdir } from "node:os"
import { join } from "node:path"

import {
  diffCounts,
  ellipsis,
  formatTokens,
  isDiffLike,
  loadPromptHistory,
  previewLines,
  savePromptHistory,
  toolCallSummary,
} from "../src/util"

test("ellipsis never exceeds the requested width", () => {
  expect(ellipsis("short", 10)).toBe("short")
  expect(ellipsis("abcdefghij", 5)).toHaveLength(5)
  expect(ellipsis("abcdefghij", 5)).toBe("abcd…")
  expect(ellipsis("abcdefghij", 1)).toBe("…")
})

test("previewLines bounds long output and reports hidden lines", () => {
  const output = Array.from({ length: 10_000 }, (_, index) => `line ${index + 1}`).join("\n")
  const compact = previewLines(output, 8)
  expect(compact.lines).toHaveLength(8)
  expect(compact.lines[0]).toBe("line 1")
  expect(compact.lines[7]).toBe("line 8")
  expect(compact.hidden).toBe(9_992)
  const expanded = previewLines(output, 200)
  expect(expanded.lines).toHaveLength(200)
  expect(expanded.hidden).toBe(9_800)
})

test("previewLines keeps short output intact", () => {
  const preview = previewLines("one\ntwo\nthree", 8)
  expect(preview.lines).toEqual(["one", "two", "three"])
  expect(preview.hidden).toBe(0)
  expect(previewLines("", 8)).toEqual({ lines: [], hidden: 0 })
})

test("diff helpers detect unified diffs and count changes", () => {
  const patch = [
    "--- a/file.py",
    "+++ b/file.py",
    "@@ -1,2 +1,2 @@",
    "-old",
    "+new",
    " context",
  ].join("\n")
  expect(isDiffLike(patch)).toBe(true)
  expect(isDiffLike("plain output\n@@ not a diff")).toBe(false)
  expect(diffCounts(patch)).toEqual({ added: 1, removed: 1 })
})

test("formatTokens keeps round values compact", () => {
  expect(formatTokens(999)).toBe("999")
  expect(formatTokens(1_500)).toBe("1.5k")
  expect(formatTokens(100_000)).toBe("100k")
  expect(formatTokens(1_000_000)).toBe("1M")
  expect(formatTokens(1_250_000)).toBe("1.3M")
  expect(formatTokens(null)).toBe("0")
})

test("toolCallSummary extracts readable fields", () => {
  expect(toolCallSummary("read_file", '{"path":"math_utils.py"}')).toBe("math_utils.py")
  expect(toolCallSummary("edit_file", '{"path":"src/app.py","old_string":"a"}')).toBe("src/app.py")
  expect(toolCallSummary("glob", '{"pattern":"**/*.ts"}')).toBe("**/*.ts")
  expect(toolCallSummary("bash", '{"command":"pytest -q"}')).toBe("$ pytest -q")
  expect(toolCallSummary("repo_map", '{"query":"hello","budget_tokens":2000}')).toBe("hello")
  expect(toolCallSummary("web_fetch", '{"url":"https://example.test"}')).toBe(
    "https://example.test",
  )
  expect(toolCallSummary("custom", "plain summary")).toBe("plain summary")
  expect(toolCallSummary("custom", "not json {")).toBe("not json {")
})

test("prompt history persists per project", () => {
  const home = mkdtempSync(join(tmpdir(), "siwah-history-"))
  const projectA = join(home, "project-a")
  const projectB = join(home, "project-b")
  try {
    savePromptHistory(projectA, ["first prompt", "second prompt"], home)
    savePromptHistory(projectB, ["other project prompt"], home)
    expect(loadPromptHistory(projectA, home)).toEqual(["first prompt", "second prompt"])
    expect(loadPromptHistory(projectB, home)).toEqual(["other project prompt"])
    savePromptHistory(projectA, ["second prompt", "third prompt"], home)
    expect(loadPromptHistory(projectA, home)).toEqual(["second prompt", "third prompt"])
  } finally {
    rmSync(home, { recursive: true, force: true })
  }
})
