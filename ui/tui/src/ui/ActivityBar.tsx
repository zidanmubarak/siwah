import { createMemo, Show } from "solid-js"
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
  const activity = createMemo(() => {
    if (store.permission()) return "permission required"
    if (!store.running()) return ""
    const retry = store.providerRetry()
    if (retry) {
      const delay = (retry.delayMs / 1000).toFixed(1)
      return `retrying in ${delay}s · attempt ${retry.attempt}/${retry.max} · ${retry.code.replace(/_/g, " ")}`
    }
    const messages = store.messages()
    const active = messages.filter((message) => message.role === "assistant").at(-1)
    const tools = active?.toolCalls.filter((tool) => tool.status === "running") ?? []
    if (tools.length > 0)
      return `running ${tools[0]?.name}${tools.length > 1 ? ` +${tools.length - 1}` : ""}`
    if (active?.streaming && active.content) return "writing response"
    if (active?.streaming && active.reasoning) return "thinking"
    return "waiting for model"
  })
  return (
    <box flexDirection="row" height={1} flexShrink={0} paddingX={2} gap={1}>
      <Show when={store.running()}>
        <Pulse active={store.running()} color={theme.accentAlt} />
      </Show>
      <text
        fg={store.permission() ? theme.warning : theme.muted}
        wrapMode="none"
        flexGrow={1}
        minWidth={0}
      >
        {clipCells(
          `${activity() ? `${store.running() ? "working · " : ""}${activity()}` : ""}${
            store.queuedPrompts().length > 0 ? ` · ${store.queuedPrompts().length} queued` : ""
          }`,
          Math.max(12, dimensions().width - (props.away ? 33 : 20)),
        )}
      </text>
      <Show
        when={props.away}
        fallback={
          <Show when={store.running()}>
            <text fg={theme.dim} wrapMode="none">{`${props.cancelKey} cancel`}</text>
          </Show>
        }
      >
        <text fg={theme.accentAlt} wrapMode="none" onMouseDown={props.onJump}>
          {`↓ latest · ${props.jumpKey}`}
        </text>
      </Show>
    </box>
  )
}
