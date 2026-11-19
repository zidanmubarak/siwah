import { TextAttributes } from "@opentui/core"
import { For, Show } from "solid-js"
import { overlayBackground } from "../theme"
import { clipCells, windowStart } from "../util"
import { useDimensions, useTheme } from "./context"

export type InlineItem = { label: string; detail?: string; value: string }

const visibleRows = 7

export function InlineMenu(props: {
  title: string
  items: InlineItem[]
  selected: number
  onSelect: (item: InlineItem) => void
  maxRows?: number
}) {
  const theme = useTheme()
  const dimensions = useDimensions()
  const showDetail = () => dimensions().width >= 72
  const width = () => Math.max(36, dimensions().width - 4)
  const labelWidth = () => Math.max(18, Math.floor(width() * 0.42))
  const rows = () =>
    Math.max(
      1,
      Math.min(
        visibleRows,
        props.maxRows ?? visibleRows,
        props.items.length,
        Math.max(3, dimensions().height - 16),
      ),
    )
  const start = () => windowStart(props.selected, props.items.length, rows())
  const visible = () => props.items.slice(start(), start() + rows())
  const counter = () => {
    if (props.items.length <= rows()) return ""
    return `  ${Math.min(props.selected + 1, props.items.length)}/${props.items.length}`
  }

  return (
    <box
      border={["top", "left", "bottom"]}
      borderColor={theme.borderFocus}
      backgroundColor={overlayBackground(theme)}
      flexDirection="column"
      flexShrink={0}
      paddingX={1}
      marginBottom={0}
      width={width()}
      height={rows() + 4}
    >
      <box flexDirection="row" gap={1} height={1}>
        <text fg={theme.accent} attributes={TextAttributes.BOLD}>
          {props.title.toUpperCase()}
        </text>
        <text fg={theme.dim}>{counter()}</text>
      </box>
      <For each={visible()}>
        {(item, index) => {
          const row = () => start() + index()
          const active = () => row() === props.selected
          return (
            <box
              flexDirection="row"
              gap={1}
              height={1}
              backgroundColor={active() ? theme.selection : undefined}
              onMouseDown={() => props.onSelect(item)}
            >
              <box width={labelWidth()} flexShrink={0}>
                <text fg={active() ? theme.accent : theme.muted} wrapMode="none">
                  {`${active() ? "› " : "  "}${clipCells(item.label, Math.max(1, labelWidth() - 2))}`}
                </text>
              </box>
              <Show when={item.detail && showDetail()}>
                <text fg={theme.dim} wrapMode="none">
                  {clipCells(item.detail ?? "", Math.max(8, width() - labelWidth() - 5))}
                </text>
              </Show>
            </box>
          )
        }}
      </For>
      <text fg={theme.dim} wrapMode="none">
        {"↑↓ navigate · enter select · esc dismiss"}
      </text>
    </box>
  )
}
