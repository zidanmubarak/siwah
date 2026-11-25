import type { BoxRenderable } from "@opentui/core"
import { createSignal, Index, Show } from "solid-js"
import { clipCells } from "../util"

import { useStore, useTheme } from "./context"

const statusSymbol: Record<string, string> = {
  pending: "○",
  in_progress: "◐",
  done: "✓",
  blocked: "!",
}

export function TodoPanel(props: { embedded?: boolean } = {}) {
  const store = useStore()
  const theme = useTheme()
  const done = () => store.todos().filter((item) => item.status === "done").length
  const percentage = () => Math.round((done() * 100) / Math.max(1, store.todos().length))

  return (
    <box
      id="todo-panel"
      width={props.embedded ? "100%" : 32}
      height={props.embedded ? "auto" : "100%"}
      flexShrink={0}
      border={props.embedded ? [] : ["left"]}
      borderColor={theme.border}
      paddingX={1}
      backgroundColor={theme.panel}
      flexDirection="column"
    >
      <box flexDirection="row" justifyContent="space-between" gap={1} flexShrink={0}>
        <text fg={theme.text} attributes={1}>
          {"TASKS"}
        </text>
        <box flexDirection="row" gap={1}>
          <Show when={store.todos().length > 8}>
            <text fg={theme.dim} wrapMode="none">
              {"↕"}
            </text>
          </Show>
          <text fg={theme.dim}>{`${done()}/${store.todos().length}`}</text>
          <Show when={store.todos().length > 0}>
            <text fg={theme.muted}>{`${percentage()}%`}</text>
          </Show>
          <text
            fg={theme.dim}
            wrapMode="none"
            onMouseDown={() => (props.embedded ? store.closeDialog() : store.toggleTodos())}
          >
            {"×"}
          </text>
        </box>
      </box>
      <Show when={store.todos().length > 0} fallback={<text fg={theme.dim}>No active plan</text>}>
        <Show when={!props.embedded} fallback={<TaskRows />}>
          <scrollbox
            id="todo-list"
            flexGrow={1}
            flexShrink={1}
            minHeight={0}
            width="100%"
            scrollY
            scrollX={false}
            stickyScroll={false}
            scrollbarOptions={{ visible: false }}
            horizontalScrollbarOptions={{ visible: false }}
            verticalScrollbarOptions={{ visible: false }}
          >
            <TaskRows />
          </scrollbox>
        </Show>
      </Show>
    </box>
  )
}

function TaskRows() {
  const store = useStore()
  const theme = useTheme()
  const [width, setWidth] = createSignal(29)
  let container: BoxRenderable | undefined
  return (
    <box
      flexDirection="column"
      width="100%"
      flexShrink={0}
      ref={(node) => {
        container = node
      }}
      onSizeChange={() => {
        if (container) setWidth(container.width)
      }}
    >
      <Index each={store.todos()}>
        {(item) => (
          <box
            id={`task-row-${item().id}`}
            flexDirection="row"
            width="100%"
            height={1}
            flexShrink={0}
            gap={1}
            backgroundColor={
              item().status === "in_progress" || item().status === "blocked"
                ? theme.panelAlt
                : theme.panel
            }
          >
            <text
              fg={
                item().status === "done"
                  ? theme.success
                  : item().status === "blocked"
                    ? theme.error
                    : theme.accentAlt
              }
              wrapMode="none"
            >
              {statusSymbol[item().status] ?? "○"}
            </text>
            <text
              fg={item().status === "done" ? theme.dim : theme.text}
              wrapMode="none"
              flexGrow={1}
              flexShrink={1}
              minWidth={0}
            >
              {clipCells(item().content.replace(/\s+/g, " ").trim(), Math.max(1, width() - 2))}
            </text>
          </box>
        )}
      </Index>
    </box>
  )
}
