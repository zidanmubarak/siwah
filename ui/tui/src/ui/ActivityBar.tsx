import { createEffect, createMemo, createSignal, onCleanup, Show } from "solid-js"
import { clipCells } from "../util"
import { useDimensions, useStore, useTheme } from "./context"
import { Pulse } from "./Motion"

/** One fixed activity line: stream updates never add cursor/spinner rows. */
export function ActivityBar(props: {
  away: boolean
  jumpKey: string
  cancelKey: string
  onJump: () => void
}) {
  const store = useStore()
  const theme = useTheme()
  const dimensions = useDimensions()
  const [now, setNow] = createSignal(Date.now())
  const spinning = () => store.running() && !store.permission() && !store.providerRetry()
  const queueLabel = () => {
    const queue = store.queuedPrompts()
    return queue.length
      ? `${queue.length} queued${queue[0]?.blockedReason ? " (blocked)" : ""}`
      : ""
  }
  const shortcut = () =>
    props.away ? `↓ latest · ${props.jumpKey}` : store.running() ? `${props.cancelKey} cancel` : ""
  const activityWidth = () => {
    const labels = [queueLabel(), shortcut()].filter(Boolean)
    const reserved = labels.reduce((width, label) => width + Bun.stringWidth(label) + 1, 0)
    return Math.max(0, dimensions().width - 4 - reserved - (spinning() ? 2 : 0))
  }
  createEffect(() => {
    if (!store.runPhase()) return
    setNow(Date.now())
    const timer = setInterval(() => setNow(Date.now()), 1000)
    onCleanup(() => clearInterval(timer))
  })
  const activity = createMemo(() => {
    if (store.permission()) return "permission required"
    if (!store.running()) return ""
    const retry = store.providerRetry()
    if (retry) {
      const delay = (retry.delayMs / 1000).toFixed(1)
      if (dimensions().width < 80) return `retry ${retry.attempt}/${retry.max} · ${delay}s`
      return `retrying in ${delay}s · attempt ${retry.attempt}/${retry.max} · ${retry.code.replace(/_/g, " ")}`
    }
    const phase = store.runPhase()
    if (phase) return `${phase.label} - ${Math.max(0, Math.floor((now() - phase.since) / 1000))}s`
    const messages = store.messages()
    let index = messages.length - 1
    while (index >= 0 && messages[index].role !== "assistant") index -= 1
    const active = messages[index]
    const tools = active?.toolCalls.filter((tool) => tool.status === "running") ?? []
    if (tools.length > 0)
      return `running ${tools[0]?.name}${tools.length > 1 ? ` +${tools.length - 1}` : ""}`
    if (active?.streaming && active.content) return "writing response"
    if (active?.streaming && active.reasoning) return "thinking"
    return "waiting for model"
  })
  return (
    <box flexDirection="row" height={1} flexShrink={0} paddingX={2} gap={1}>
      <Show when={spinning()}>
        <Pulse active={spinning()} color={theme.accent} />
      </Show>
      <text
        fg={store.permission() ? theme.warning : theme.muted}
        wrapMode="none"
        flexGrow={1}
        minWidth={0}
      >
        {clipCells(
          activity() ? `${store.running() ? "working · " : ""}${activity()}` : "",
          activityWidth(),
        )}
      </text>
      <Show when={queueLabel()}>
        <text
          fg={store.queuedPrompts()[0]?.blockedReason ? theme.warning : theme.accent}
          wrapMode="none"
          flexShrink={0}
          onMouseDown={() => {
            if (!store.dialog() && !store.permission()) store.openDialog({ type: "pending" })
          }}
        >
          {queueLabel()}
        </text>
      </Show>
      <Show
        when={props.away}
        fallback={
          <Show when={store.running()}>
            <text fg={theme.dim} wrapMode="none" flexShrink={0}>
              {shortcut()}
            </text>
          </Show>
        }
      >
        <text fg={theme.accent} wrapMode="none" flexShrink={0} onMouseDown={props.onJump}>
          {shortcut()}
        </text>
      </Show>
    </box>
  )
}
