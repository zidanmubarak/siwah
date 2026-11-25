import { type ScrollBoxRenderable, TextAttributes } from "@opentui/core"
import { createEffect, createMemo, For, onCleanup, Show } from "solid-js"
import { clipCells } from "../util"
import { useDimensions, useStore, useTheme } from "./context"

export function TopBar() {
  const store = useStore()
  const theme = useTheme()
  const dimensions = useDimensions()
  const sessions = () => store.visibleProjectSessions()
  const activeId = () => store.session()?.id
  const gap = 1
  const minTabWidth = 14
  const maxTabWidth = 24
  const count = () => sessions().length
  const arrows = () => count() > 0 && count() * (minTabWidth + gap) > dimensions().width - 4 - 6 - 2
  const available = () =>
    Math.max(0, dimensions().width - 4 - 6 - (arrows() ? 2 : 0) - gap * Math.max(0, count() - 1))
  const desiredTabWidth = () => {
    if (count() === 0) return maxTabWidth
    const longest = Math.max(
      ...sessions().map((session) => Bun.stringWidth(session.title?.trim() || "untitled")),
    )
    return Math.max(minTabWidth, Math.min(maxTabWidth, longest + 6))
  }
  const tabWidth = () => {
    if (count() === 0) return maxTabWidth
    const fit = Math.floor(available() / count())
    return Math.max(minTabWidth, Math.min(desiredTabWidth(), fit))
  }
  const divider = () => "─".repeat(Math.max(1, dimensions().width - 4))
  let tabs: ScrollBoxRenderable | undefined
  const measuredTabWidth = createMemo(tabWidth)
  const backgroundRunning = createMemo(() =>
    sessions().some((session) => session.id !== activeId() && store.sessionRunning(session)),
  )

  const scrollTabs = (direction: -1 | 1) => {
    tabs?.scrollBy({ x: direction * Math.max(2, tabWidth()) * 2, y: 0 })
  }

  createEffect(() => {
    const id = activeId()
    measuredTabWidth()
    dimensions().width
    if (!id) return
    queueMicrotask(() => tabs?.scrollChildIntoView(`tab-${id}`, { align: "nearest" }))
  })
  createEffect(() => {
    if (store.connection() !== "online" || !backgroundRunning()) return
    let cancelled = false
    let timer: ReturnType<typeof setTimeout> | undefined
    // Schedule the next poll only after the previous one settles. A slow
    // daemon must not accumulate overlapping session-list requests.
    const poll = async () => {
      try {
        await store.reloadSessions({ quiet: true })
      } finally {
        if (!cancelled) timer = setTimeout(poll, 2000)
      }
    }
    timer = setTimeout(poll, 2000)
    onCleanup(() => {
      cancelled = true
      if (timer !== undefined) clearTimeout(timer)
    })
  })

  return (
    <box flexDirection="column" width="100%" flexShrink={0} paddingX={2}>
      <box flexDirection="row" width="100%" height={1} gap={1} alignItems="center">
        <Show when={arrows()}>
          <text fg={theme.muted} wrapMode="none" onMouseDown={() => scrollTabs(-1)}>
            {"‹"}
          </text>
        </Show>
        <scrollbox
          id="session-tabs"
          ref={(node) => {
            tabs = node as ScrollBoxRenderable
          }}
          flexGrow={1}
          flexBasis={0}
          width="auto"
          flexShrink={1}
          height={1}
          minWidth={0}
          scrollX
          scrollY={false}
          scrollbarOptions={{ visible: false }}
          horizontalScrollbarOptions={{ visible: false }}
          verticalScrollbarOptions={{ visible: false }}
        >
          <box flexDirection="row" gap={gap} height={1}>
            <Show
              when={sessions().length > 0}
              fallback={
                <box paddingX={1} height={1} onMouseDown={() => void store.createSession()}>
                  <text fg={theme.muted} wrapMode="none">
                    {"new session"}
                  </text>
                </box>
              }
            >
              <For each={sessions()}>
                {(session) => {
                  const active = () => session.id === activeId()
                  const waiting = () =>
                    active()
                      ? Boolean(store.permission())
                      : store.sessionRunning(session) &&
                        session.metadata?.waiting_permission === true
                  const marker = () => (waiting() ? "!" : store.sessionRunning(session) ? "●" : "")
                  const labelWidth = () =>
                    Math.max(1, tabWidth() - 2 - (active() ? 2 : 0) - (marker() ? 2 : 0))
                  return (
                    <box
                      id={`tab-${session.id}`}
                      flexDirection="row"
                      flexShrink={0}
                      width={tabWidth()}
                      gap={1}
                      height={1}
                      paddingX={1}
                      backgroundColor={active() ? theme.selection : undefined}
                      onMouseDown={() => void store.switchSession(session.id)}
                    >
                      <Show when={marker()}>
                        <text fg={waiting() ? theme.warning : theme.muted}>{marker()}</text>
                      </Show>
                      <text
                        fg={active() ? theme.accent : theme.muted}
                        attributes={active() ? TextAttributes.BOLD : 0}
                        wrapMode="none"
                        flexGrow={1}
                        minWidth={0}
                      >
                        {clipCells(session.title?.trim() || "untitled", labelWidth())}
                      </text>
                      <Show when={active()}>
                        <text
                          fg={theme.muted}
                          wrapMode="none"
                          onMouseDown={(event) => {
                            event.stopPropagation()
                            void store.closeSessionTab(session.id)
                          }}
                        >
                          {"×"}
                        </text>
                      </Show>
                    </box>
                  )
                }}
              </For>
            </Show>
          </box>
        </scrollbox>
        <Show when={arrows()}>
          <text fg={theme.muted} wrapMode="none" onMouseDown={() => scrollTabs(1)}>
            {"›"}
          </text>
        </Show>
        <text
          width={6}
          flexShrink={0}
          fg={theme.muted}
          wrapMode="none"
          onMouseDown={() => void store.createSession()}
        >
          {"＋ new"}
        </text>
      </box>
      <text fg={theme.border} wrapMode="none">
        {divider()}
      </text>
    </box>
  )
}
