import { TextAttributes } from "@opentui/core"
import type { JSX } from "solid-js"
import { useTheme } from "./context"

export function SectionLabel(props: { children: JSX.Element }) {
  const theme = useTheme()
  return (
    <text fg={theme.accentAlt} attributes={TextAttributes.BOLD} marginTop={1}>
      {props.children}
    </text>
  )
}

export function MetricRow(props: { label: string; value: string | number; emphasis?: boolean }) {
  const theme = useTheme()
  return (
    <box flexDirection="row" justifyContent="space-between" gap={2} width="100%" minHeight={1}>
      <text fg={theme.muted}>{props.label}</text>
      <text
        fg={props.emphasis ? theme.accent : theme.text}
        wrapMode="word"
        flexShrink={1}
        minWidth={0}
      >
        {String(props.value)}
      </text>
    </box>
  )
}

export function Notice(props: { children: JSX.Element; warning?: boolean }) {
  const theme = useTheme()
  return (
    <box
      backgroundColor={theme.panelAlt}
      border={["left"]}
      borderColor={props.warning ? theme.warning : theme.accentAlt}
      paddingX={1}
      paddingY={1}
      flexDirection="column"
      width="100%"
    >
      {props.children}
    </box>
  )
}
