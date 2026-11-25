import { expect, test } from "bun:test"
import type { BoxRenderable, ScrollBoxRenderable } from "@opentui/core"
import { testRender } from "@opentui/solid"
import type { Session } from "../src/api/client"
import { TuiStore } from "../src/state"
import { baseTheme } from "../src/theme"
import { App } from "../src/ui/App"
import { setActiveStore, setActiveTheme } from "../src/ui/context"

function session(id: string, title: string): Session {
  return {
    id,
    title,
    cwd: "D:/work/siwah",
    model: "mock/model-a",
    mode: "build",
    status: "idle",
    created_at: "",
    updated_at: "",
    metadata: {},
    last_event_seq: 0,
  }
}

test("session tabs have equal widths and remain reachable when they overflow", async () => {
  const store = new TuiStore()
  const theme = baseTheme("siwah-dark")
  store.theme(theme)
  store.models({ providers: [], roles: { main: "mock/model-a" } })
  store.auth({ providers: [] })
  store.connection("online")
  const sessions = Array.from({ length: 12 }, (_, index) =>
    session(`s${index}`, `session ${index + 1}`),
  )
  store.session(sessions[0] ?? null)
  store.sessions(sessions)
  setActiveStore(store)
  setActiveTheme(theme)

  const setup = await testRender(() => <App store={store} initialize={false} />, {
    width: 80,
    height: 16,
  })
  try {
    await setup.renderOnce()
    const tabs = setup.renderer.root.findDescendantById("session-tabs") as
      | ScrollBoxRenderable
      | undefined
    if (!tabs) throw new Error("Session tab strip is missing")
    const widths = sessions.map((item) => {
      const tab = setup.renderer.root.findDescendantById(`tab-${item.id}`) as
        | BoxRenderable
        | undefined
      if (!tab) throw new Error(`Tab ${item.id} is missing`)
      return tab.width
    })
    expect(new Set(widths).size).toBe(1)
    const frame = setup.captureCharFrame()
    expect(frame).toContain("‹")
    expect(frame).toContain("›")
    const row = frame.split("\n")[0] ?? ""
    const right = row.lastIndexOf("›")
    expect(right).toBeGreaterThan(0)
    await setup.mockMouse.click(right, 0)
    await setup.renderOnce()
    expect(tabs.scrollLeft).toBeGreaterThan(0)
    expect(setup.captureCharFrame()).toContain("session")
  } finally {
    setup.renderer.destroy()
  }
})

test("the inactive tab marks a pending permission and keeps only the active close action", async () => {
  const store = new TuiStore()
  const first = session("first", "design")
  const second = {
    ...session("second", "checks"),
    status: "running",
    metadata: { waiting_permission: true },
  }
  store.session(first)
  store.sessions([first, second])
  const setup = await testRender(() => <App store={store} initialize={false} />, {
    width: 60,
    height: 20,
  })
  try {
    await setup.renderOnce()
    const tabs = setup.captureCharFrame().split("\n")[0]
    expect(tabs).toContain("! checks")
    expect(tabs.match(/×/g)).toHaveLength(1)
  } finally {
    setup.renderer.destroy()
  }
})
