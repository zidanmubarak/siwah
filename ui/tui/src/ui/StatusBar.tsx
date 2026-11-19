import { TextAttributes } from "@opentui/core"
import { createMemo, Show } from "solid-js"
import { clipCells, gitBranch } from "../util"
import { useDimensions, useStore, useTheme } from "./context"

export function StatusBar(props: { embedded?: boolean } = {}) {
  const store = useStore()
  const theme = useTheme()
  const dimensions = useDimensions()
  const session = () => store.session()
  const cwd = () => session()?.cwd ?? process.env.SIWAH_WORKSPACE ?? process.cwd()
  const branch = createMemo(() => {
    const value = gitBranch(cwd())
    return value === "no-git" ? "" : value
  })
  const tasks = () =>
    store.todos().length > 0 && (store.todosHidden() || dimensions().width < 100)
      ? `tasks ${done()}/${store.todos().length} ▸`
      : ""
  const evolution = () =>
    dimensions().width >= 120 && store.evolution()?.tag
      ? `${store.evolution()?.tag} ${store.evolution()?.status ?? "idle"}`
      : ""
  const fixedWidth = () =>
    [connection(), effort(), tasks(), evolution()]
      .filter(Boolean)
      .reduce((sum, value) => sum + Bun.stringWidth(value) + 1, 0)
  const model = () =>
    clipCells(
      store.currentModel(),
      Math.max(
        6,
        Math.min(
          dimensions().width < 100 ? 22 : 34,
          dimensions().width - (props.embedded ? 28 : 24) - fixedWidth(),
        ),
      ),
      true,
    )
  const effort = () => store.effort() ?? "off"
  const done = () => store.todos().filter((item) => item.status === "done").length
  const online = () => store.connection() === "online"
  const connection = () => {
    if (store.connection() === "online") return ""
    if (store.connection() === "connecting") return "connecting"
    if (store.connection() === "reconnecting") return "reconnecting"
    return "offline"
  }
  const right = () => [cwd(), branch()].filter(Boolean).join("  ")
  const rightLabel = () =>
    clipCells(
      right(),
      Math.max(
        0,
        dimensions().width - (props.embedded ? 10 : 6) - fixedWidth() - Bun.stringWidth(model()),
      ),
      true,
    )

  return (
    <box
      flexDirection="row"
      justifyContent="space-between"
      width="100%"
      flexShrink={0}
      marginTop={props.embedded ? 1 : 0}
      paddingX={props.embedded ? 1 : 2}
    >
      <box flexDirection="row" gap={1} minWidth={0}>
        <Show when={connection()}>
          <text fg={online() ? theme.dim : theme.warning} wrapMode="none">
            {connection()}
          </text>
        </Show>
        <Show when={dimensions().width >= 60}>
          <text fg={theme.dim} wrapMode="none">
            {model()}
          </text>
          <text fg={theme.accentAlt} attributes={TextAttributes.BOLD} wrapMode="none">
            {effort()}
          </text>
        </Show>
        <Show when={tasks()}>
          <text
            fg={theme.warning}
            wrapMode="none"
            onMouseDown={() =>
              dimensions().width < 100 ? store.openDialog({ type: "todos" }) : store.toggleTodos()
            }
          >
            {tasks()}
          </text>
        </Show>
        <Show when={evolution()}>
          <text fg={theme.dim} wrapMode="none">
            {evolution()}
          </text>
        </Show>
      </box>
      <Show when={right().length > 0}>
        <text fg={theme.dim} wrapMode="none">
          {rightLabel()}
        </text>
      </Show>
    </box>
  )
}
