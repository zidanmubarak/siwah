import { expect, test } from "bun:test"

import { highlightLine, type SyntaxKind } from "../src/ui/syntax"

function kinds(line: string, filetype: string): SyntaxKind[] {
  return highlightLine(line, filetype)
    .filter((segment) => segment.text.trim().length > 0)
    .map((segment) => segment.kind)
}

test("python highlighting marks definitions, strings and comments", () => {
  expect(kinds("def add(a, b):  # sum", "python")).toEqual([
    "keyword",
    "function",
    "punctuation",
    "text",
    "punctuation",
    "text",
    "punctuation",
    "punctuation",
    "comment",
  ])
  expect(kinds('message = "hello"', "python")).toEqual(["text", "punctuation", "string"])
  expect(kinds("return 42", "python")).toEqual(["keyword", "number"])
})

test("javascript highlighting marks keywords, strings and comments", () => {
  expect(kinds("const value = `hello` // note", "typescript")).toEqual([
    "keyword",
    "text",
    "punctuation",
    "string",
    "comment",
  ])
  expect(kinds("function run() {}", "javascript")).toEqual([
    "keyword",
    "function",
    "punctuation",
    "punctuation",
    "punctuation",
    "punctuation",
  ])
})

test("unknown file types still colour strings and numbers", () => {
  expect(kinds('name = "siwah" 3', "text")).toEqual(["text", "punctuation", "string", "number"])
})
