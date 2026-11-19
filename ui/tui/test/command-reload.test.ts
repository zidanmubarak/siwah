import { expect, test } from "bun:test"

import { TuiStore } from "../src/state"

class ReloadingStore extends TuiStore {
  reloads = 0
  sent = ""

  override async reloadCommands(): Promise<void> {
    this.reloads += 1
    this.customCommands([
      {
        name: "bx-stats",
        description: "Count files and lines",
        body: "Count $ARGUMENTS",
      },
    ])
  }

  override async sendPrompt(input: string): Promise<boolean> {
    this.sent = input
    return true
  }
}

test("a command written during the session is reloaded before execution", async () => {
  const store = new ReloadingStore()
  expect(store.customCommands()).toEqual([])
  await store.runCustomCommand("/bx-stats", "src out.txt")
  expect(store.reloads).toBe(1)
  expect(store.sent).toBe("Count src out.txt")
})

test("a genuinely unknown command still reports a warning", async () => {
  const store = new ReloadingStore()
  await store.runCustomCommand("/missing", "")
  expect(store.reloads).toBe(1)
  expect(store.sent).toBe("")
  expect(store.toasts().some((toast) => toast.text.includes("Unknown command"))).toBe(true)
})
