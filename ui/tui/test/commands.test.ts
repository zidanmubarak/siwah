import { expect, test } from "bun:test"

import { filterCommands, fuzzyMatch } from "../src/commands"
import { maskedSecret, pickChatModel, windowStart } from "../src/util"

test("slash filtering keeps matching commands while typing", () => {
  for (const query of ["/", "/c", "/co", "/con", "/connect", "/th", "/theme"]) {
    expect(filterCommands(query).length).toBeGreaterThan(0)
  }
  expect(filterCommands("/con").map((command) => command.name)).toContain("/connect")
  expect(filterCommands("/theme").map((command) => command.name)).toContain("/theme")
  for (const name of [
    "/effort",
    "/permissions",
    "/reasoning",
    "/pause",
    "/resume",
    "/update",
    "/rollback",
    "/quit",
  ]) {
    expect(filterCommands("").map((command) => command.name)).toContain(name)
  }
})

test("fuzzy matching ignores the leading slash", () => {
  expect(fuzzyMatch("co", "connect")).toBeGreaterThan(0)
  expect(fuzzyMatch("zzz", "connect")).toBe(-1)
})

test("list windows keep the selected row visible", () => {
  expect(windowStart(0, 20, 7)).toBe(0)
  expect(windowStart(10, 20, 7)).toBe(7)
  expect(windowStart(19, 20, 7)).toBe(13)
  expect(windowStart(3, 5, 7)).toBe(0)
})

test("secrets are masked for display", () => {
  expect(maskedSecret("")).toBe("(empty)")
  expect(maskedSecret("sk-test-key")).toBe("***********")
  expect(maskedSecret("x".repeat(70))).toContain("…")
})

test("auto-selection skips non-chat models", () => {
  expect(
    pickChatModel("openai", [
      { id: "text-embedding-3-small" },
      { id: "whisper-1" },
      { id: "gpt-4o", supports_tools: true },
    ]),
  ).toBe("openai/gpt-4o")
  expect(
    pickChatModel("mock", [
      { id: "chat-no-tools", supports_tools: false },
      { id: "chat-tools", supports_tools: true },
    ]),
  ).toBe("mock/chat-tools")
  expect(pickChatModel("mock", [])).toBeNull()
})
