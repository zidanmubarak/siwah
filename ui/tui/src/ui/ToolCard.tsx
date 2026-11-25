import type { MouseEvent } from "@opentui/core"
import { createMemo, For, Index, Show } from "solid-js"
import type { DiffBlock, ToolView } from "../state"
import { clipCells, clipLines, fileType, toolCallSummary } from "../util"

import { useDimensions, useStore, useTheme } from "./context"
import { DiffView, parseUnifiedDiff } from "./DiffView"
import { highlightLine, type SyntaxKind } from "./syntax"

const COLLAPSED_LINES = 4
export const MAX_TOOL_LINES = 1000

export function ToolCard(props: { tool: ToolView; diff?: DiffBlock }) {
  const store = useStore()
  const theme = useTheme()
  const dimensions = useDimensions()
  let down: { x: number; y: number } | null = null
  const isBash = () => props.tool.name === "bash" || props.tool.summary.trimStart().startsWith("$")
  const isEdit = () => props.tool.name === "edit_file" || props.tool.edited === true
  const isWrite = () => props.tool.name === "write_file" && !isEdit()
  const isCode = () => props.tool.name === "read_file" || isWrite()
  const displayName = () => (isEdit() ? "edit_file" : props.tool.name)
  const label = () => {
    const summary = toolCallSummary(props.tool.name, props.tool.summary)
    if (!summary) return displayName()
    if (props.tool.name === "bash" || summary.startsWith("$")) return summary
    return `${displayName()}  ${summary}`
  }
  const sidebarWidth = () =>
    store.todos().length > 0 && !store.todosHidden() && dimensions().width >= 100 ? 35 : 0
  const labelWidth = () => Math.max(20, dimensions().width - sidebarWidth() - 12)
  const bodyWidth = () => Math.max(20, dimensions().width - sidebarWidth() - 10)
  const collapsedLabel = () => clipCells(label().replace(/[\r\n\t]/g, " "), labelWidth())
  const labelTruncated = () => collapsedLabel() !== label()
  const labelText = () => (expanded() ? label() : collapsedLabel())
  const outputLines = createMemo(() => {
    const output = (props.tool.output || "").replace(/\r/g, "").replace(/\n+$/, "")
    return output ? output.split("\n") : []
  })
  const collapsedLines = () => (dimensions().width < 78 ? 2 : COLLAPSED_LINES)
  const diffRows = createMemo(() => (props.diff ? parseUnifiedDiff(props.diff.patch).length : 0))
  const compactOutput = () => !isWrite() && !isBash()
  const hasError = () => props.tool.status === "error" && Boolean(props.tool.error?.trim())
  const canExpand = () =>
    labelTruncated() ||
    diffRows() > 4 ||
    (!isEdit() && outputLines().length > (compactOutput() ? 0 : collapsedLines())) ||
    hasError()
  const expanded = () => store.isToolExpanded(props.tool.id)
  const toggle = () => {
    if (canExpand()) store.toggleTool(props.tool.id)
  }
  const click = (event: MouseEvent) => {
    if (!down || event.button !== 0) return false
    return Math.abs(event.x - down.x) <= 1 && Math.abs(event.y - down.y) <= 1
  }
  const lines = createMemo(() => {
    if (isEdit() || (compactOutput() && !expanded())) return []
    if (expanded()) return outputLines().slice(0, MAX_TOOL_LINES)
    return outputLines().slice(0, collapsedLines())
  })
  const statusColor = () => {
    if (props.tool.status === "running") return theme.accent
    if (props.tool.status === "error") return theme.error
    return theme.muted
  }
  const duration = () => {
    const value = props.tool.durationMs
    if (
      value === null ||
      value === undefined ||
      props.tool.status === "running" ||
      props.tool.status === "skipped"
    )
      return ""
    return value < 1000 ? `${Math.round(value)}ms` : `${(value / 1000).toFixed(1)}s`
  }
  const metadata = () => {
    const status = props.tool.status
    return [status === "ok" ? (expanded() ? "done" : "") : status, duration()]
      .filter(Boolean)
      .join(" · ")
  }
  const codeFiletype = () => (isCode() ? fileType(label()) : null)
  const syntaxSegments = (line: string) => highlightLine(line || " ", codeFiletype() ?? "text")
  const syntaxColor = (kind: SyntaxKind) => {
    switch (kind) {
      case "keyword":
        return theme.accent
      case "string":
        return theme.success
      case "comment":
        return theme.dim
      case "number":
        return theme.warning
      case "function":
        return theme.user
      case "type":
        return theme.accentAlt
      case "punctuation":
        return theme.muted
      default:
        return theme.text
    }
  }

  return (
    <box
      id={`tool-${props.tool.id}`}
      flexDirection="column"
      width="100%"
      border={["left"]}
      borderColor={props.tool.status === "error" ? theme.error : theme.border}
      backgroundColor={expanded() || isWrite() ? theme.panel : undefined}
      paddingLeft={1}
      onMouseDown={(event) => {
        if (event.button === 0) down = { x: event.x, y: event.y }
      }}
      onMouseUp={(event) => {
        if (click(event)) toggle()
        down = null
      }}
    >
      <box flexDirection="row" gap={1} minWidth={0}>
        <Show when={canExpand() && compactOutput() && !props.diff && !labelTruncated()}>
          <text fg={theme.muted} wrapMode="none">
            {expanded() ? "▾" : "▸"}
          </text>
        </Show>
        <text
          fg={theme.text}
          wrapMode={expanded() ? "word" : "none"}
          flexGrow={1}
          flexShrink={1}
          minWidth={0}
        >
          {labelText()}
        </text>
        <Show when={metadata()}>
          <text fg={statusColor()} wrapMode="none" flexShrink={0}>
            {metadata()}
          </text>
        </Show>
      </box>
      <Show when={props.diff}>
        <DiffView
          patch={props.diff?.patch ?? ""}
          path={props.diff?.path}
          embedded
          disclosure={false}
          expanded={expanded()}
          onToggle={toggle}
        />
      </Show>
      <Show when={lines().length > 0}>
        <box flexDirection="column" paddingLeft={1} marginTop={1} width="100%">
          <Show
            when={isWrite()}
            fallback={
              <Show
                when={isCode()}
                fallback={
                  <Index each={lines()}>
                    {(line) => (
                      <text fg={theme.muted} wrapMode="word">
                        {line() || " "}
                      </text>
                    )}
                  </Index>
                }
              >
                <For each={lines()}>
                  {(line) => (
                    <text wrapMode="word">
                      <For each={syntaxSegments(line)}>
                        {(segment) => (
                          <span style={{ fg: syntaxColor(segment.kind) }}>{segment.text}</span>
                        )}
                      </For>
                    </text>
                  )}
                </For>
              </Show>
            }
          >
            <For each={lines()}>
              {(line, index) => (
                <box backgroundColor={theme.diffAddedBg} width="100%">
                  <text wrapMode="word">
                    <span style={{ fg: theme.dim }}>{`${String(index() + 1).padStart(3)} `}</span>
                    <span style={{ fg: theme.success }}>{"+ "}</span>
                    <For each={syntaxSegments(line)}>
                      {(segment) => (
                        <span style={{ fg: syntaxColor(segment.kind) }}>{segment.text}</span>
                      )}
                    </For>
                  </text>
                </box>
              )}
            </For>
          </Show>
        </box>
      </Show>
      <Show when={expanded() && !isEdit() && outputLines().length > MAX_TOOL_LINES}>
        <text fg={theme.dim} paddingLeft={1} wrapMode="none">
          {`… ${outputLines().length - MAX_TOOL_LINES} lines omitted (display limit ${MAX_TOOL_LINES})`}
        </text>
      </Show>
      <Show
        when={canExpand() && (!compactOutput() || labelTruncated() || diffRows() > 4 || hasError())}
      >
        <text fg={theme.dim} paddingLeft={1} wrapMode="none">
          {expanded()
            ? "… click to collapse"
            : diffRows() > 4
              ? `… ${diffRows()} diff lines · click to expand`
              : outputLines().length > collapsedLines()
                ? `… ${outputLines().length - collapsedLines()} more lines · click to expand`
                : "… full command · click to expand"}
        </text>
      </Show>
      <Show when={props.tool.status === "error" && props.tool.error}>
        <text fg={theme.error} wrapMode="word" paddingLeft={1}>
          {expanded()
            ? clipLines(props.tool.error ?? "", MAX_TOOL_LINES)
            : clipCells(props.tool.error?.split(/\r?\n/)[0] ?? "", bodyWidth())}
        </text>
      </Show>
    </box>
  )
}
