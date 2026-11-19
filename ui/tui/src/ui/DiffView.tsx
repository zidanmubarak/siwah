import type { MouseEvent } from "@opentui/core"
import { createMemo, createSignal, For, Index, Show } from "solid-js"
import { clipCells, diffCounts, fileType } from "../util"
import { useDimensions, useStore, useTheme } from "./context"
import { highlightLine, type SyntaxKind } from "./syntax"

export type DiffRow = {
  kind: "add" | "remove" | "context" | "hunk" | "meta"
  text: string
  oldNumber: number | null
  newNumber: number | null
}

export type SplitRow = { left?: DiffRow; right?: DiffRow; meta?: DiffRow }

/** Align each replacement run without inventing context or losing unmatched lines. */
export function splitDiffRows(rows: DiffRow[]): SplitRow[] {
  const result: SplitRow[] = []
  let removed: DiffRow[] = []
  let added: DiffRow[] = []
  const flush = () => {
    for (let i = 0; i < Math.max(removed.length, added.length); i += 1) {
      result.push({ left: removed[i], right: added[i] })
    }
    removed = []
    added = []
  }
  for (const row of rows) {
    if (row.kind === "remove") removed.push(row)
    else if (row.kind === "add") added.push(row)
    else {
      flush()
      result.push(row.kind === "context" ? { left: row, right: row } : { meta: row })
    }
  }
  flush()
  return result
}

const MAX_ROWS = 600
const COLLAPSED_ROWS = 2

/**
 * Parse a unified diff into display rows.
 *
 * OpenTUI 0.8.3's native `<diff>` only paints the gutter with the added and
 * removed backgrounds, so the edited line itself stays on the terminal
 * background. A plain Text row per diff line lets us paint the entire row and
 * keeps the colors consistent across terminals.
 */
export function parseUnifiedDiff(patch: string): DiffRow[] {
  const rows: DiffRow[] = []
  let oldLine = 0
  let newLine = 0
  let inHunk = false
  for (const raw of patch.replace(/\r/g, "").split("\n")) {
    if (raw.startsWith("diff --git ")) inHunk = false
    if (!inHunk && (raw.startsWith("--- ") || raw.startsWith("+++ "))) continue
    if (raw.startsWith("@@")) {
      const match = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/.exec(raw)
      if (match) {
        inHunk = true
        oldLine = Number(match[1])
        newLine = Number(match[2])
      }
      rows.push({ kind: "hunk", text: raw, oldNumber: null, newNumber: null })
      continue
    }
    if (raw.startsWith("\\")) {
      rows.push({ kind: "meta", text: raw, oldNumber: null, newNumber: null })
      continue
    }
    const sign = raw[0]
    if (sign === "+") {
      rows.push({ kind: "add", text: raw.slice(1), oldNumber: null, newNumber: newLine })
      newLine += 1
    } else if (sign === "-") {
      rows.push({ kind: "remove", text: raw.slice(1), oldNumber: oldLine, newNumber: null })
      oldLine += 1
    } else if (sign === " ") {
      rows.push({ kind: "context", text: raw.slice(1), oldNumber: oldLine, newNumber: newLine })
      oldLine += 1
      newLine += 1
    } else if (raw.trim().length > 0) {
      rows.push({ kind: "meta", text: raw, oldNumber: null, newNumber: null })
    }
  }
  return rows
}

export function DiffView(props: {
  patch: string
  path?: string
  embedded?: boolean
  expanded?: boolean
  onToggle?: () => void
  disclosure?: boolean
}) {
  const theme = useTheme()
  const store = useStore()
  const dimensions = useDimensions()
  const contentWidth = () =>
    dimensions().width -
    (store.todos().length > 0 && !store.todosHidden() && dimensions().width >= 100 ? 35 : 0) -
    12
  const split = () => dimensions().width >= 120 && contentWidth() >= 100
  const counts = createMemo(() => diffCounts(props.patch))
  const [localExpanded, setExpanded] = createSignal(false)
  const expanded = () => props.expanded ?? localExpanded()
  let down: { x: number; y: number } | null = null
  const click = (event: MouseEvent) => {
    if (!down || event.button !== 0) return false
    return Math.abs(event.x - down.x) <= 1 && Math.abs(event.y - down.y) <= 1
  }
  const filetype = () => fileType(props.path ?? "")
  const rows = createMemo(() => {
    const parsed = parseUnifiedDiff(props.patch)
    if (parsed.length <= MAX_ROWS) return parsed
    return [
      ...parsed.slice(0, MAX_ROWS),
      {
        kind: "meta" as const,
        text: `… ${parsed.length - MAX_ROWS} more lines`,
        oldNumber: null,
        newNumber: null,
      },
    ]
  })
  const shouldCollapse = () => rows().length > 4
  const changedRows = () => rows().filter((row) => row.kind === "add" || row.kind === "remove")
  const visibleRows = () => {
    if (expanded() || !shouldCollapse()) return rows()
    const changed = changedRows()
    return changed.length > 0 ? changed.slice(0, COLLAPSED_ROWS) : rows().slice(0, COLLAPSED_ROWS)
  }
  const paired = createMemo(() => splitDiffRows(rows()))
  const visiblePairs = () => {
    if (expanded() || !shouldCollapse()) return paired()
    const changed = paired().filter(
      (row) => row.left?.kind === "remove" || row.right?.kind === "add",
    )
    return (changed.length > 0 ? changed : paired()).slice(0, COLLAPSED_ROWS)
  }
  const hiddenLines = () =>
    rows().length -
    (split()
      ? new Set(visiblePairs().flatMap((row) => [row.left, row.right, row.meta].filter(Boolean)))
          .size
      : visibleRows().length)
  const background = (row: DiffRow) => {
    if (row.kind === "add") return theme.diffAddedBg
    if (row.kind === "remove") return theme.diffRemovedBg
    if (row.kind === "hunk") return theme.panelAlt
    return undefined
  }
  const signColor = (row: DiffRow) => {
    if (row.kind === "add") return theme.success
    if (row.kind === "remove") return theme.error
    return theme.dim
  }
  const sign = (row: DiffRow) => {
    if (row.kind === "add") return "+"
    if (row.kind === "remove") return "-"
    return " "
  }
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
  const cell = (row: DiffRow | undefined, side: "left" | "right") => (
    <box
      width="50%"
      minWidth={0}
      flexShrink={1}
      paddingX={1}
      backgroundColor={row ? background(row) : theme.panel}
    >
      <text wrapMode="word">
        <span
          style={{ fg: theme.dim }}
        >{`${String((side === "left" ? row?.oldNumber : row?.newNumber) ?? "").padStart(3)} `}</span>
        <span style={{ fg: row ? signColor(row) : theme.dim }}>{`${row ? sign(row) : " "} `}</span>
        <For each={highlightLine(row?.text || " ", filetype())}>
          {(segment) => (
            <span
              style={{
                fg:
                  row?.kind === "add" || row?.kind === "remove"
                    ? theme.text
                    : syntaxColor(segment.kind),
              }}
            >
              {segment.text}
            </span>
          )}
        </For>
      </text>
    </box>
  )

  return (
    <Show when={props.patch.trim().length > 0}>
      <box
        flexDirection="column"
        width="100%"
        border={props.embedded ? [] : ["left", "bottom"]}
        borderColor={theme.border}
        backgroundColor={theme.panel}
        padding={1}
        marginTop={props.embedded ? 1 : 0}
        marginBottom={props.embedded ? 0 : 1}
        onMouseDown={(event) => {
          if (event.button === 0) down = { x: event.x, y: event.y }
        }}
        onMouseUp={(event) => {
          if (click(event)) {
            event.stopPropagation()
            if (props.onToggle) props.onToggle()
            else setExpanded((value) => !value)
          }
          down = null
        }}
      >
        <box flexDirection="row" gap={1} backgroundColor={theme.panelAlt} paddingX={1}>
          <text fg={theme.text} wrapMode="none" flexGrow={1} minWidth={0}>
            {clipCells(
              props.embedded ? "Changes" : (props.path ?? "Changes"),
              Math.max(12, contentWidth() - 28),
              true,
            )}
          </text>
          <text fg={theme.success} wrapMode="none">{`+${counts().added}`}</text>
          <text fg={theme.error} wrapMode="none">{`-${counts().removed}`}</text>
          <text fg={theme.dim} wrapMode="none">
            {split() ? "split" : "unified"}
          </text>
        </box>
        <Show when={split()}>
          <box flexDirection="row" backgroundColor={theme.panelAlt}>
            <text width="50%" fg={theme.muted}>
              {"  Before"}
            </text>
            <text width="50%" fg={theme.muted}>
              {"  After"}
            </text>
          </box>
        </Show>
        <Show
          when={split()}
          fallback={
            <Index each={visibleRows()}>
              {(value) => {
                const row = () => value()
                return (
                  <Show
                    when={
                      row().kind === "add" || row().kind === "remove" || row().kind === "context"
                    }
                    fallback={
                      <box width="100%" paddingX={1} backgroundColor={background(row())}>
                        <text fg={row().kind === "hunk" ? theme.muted : theme.dim} wrapMode="word">
                          {row().text || " "}
                        </text>
                      </box>
                    }
                  >
                    <box
                      flexDirection="row"
                      width="100%"
                      paddingX={1}
                      backgroundColor={background(row())}
                    >
                      <text wrapMode="word">
                        <span style={{ fg: theme.dim }}>
                          {`${String(row().oldNumber ?? "").padStart(3)} ${String(row().newNumber ?? "").padStart(3)} `}
                        </span>
                        <span style={{ fg: signColor(row()) }}>{`${sign(row())} `}</span>
                        <For each={highlightLine(row().text || " ", filetype())}>
                          {(segment) => (
                            <span
                              style={{
                                fg:
                                  row().kind === "add" || row().kind === "remove"
                                    ? theme.text
                                    : syntaxColor(segment.kind),
                              }}
                            >
                              {segment.text}
                            </span>
                          )}
                        </For>
                      </text>
                    </box>
                  </Show>
                )
              }}
            </Index>
          }
        >
          <Index each={visiblePairs()}>
            {(row) => (
              <Show
                when={!row().meta}
                fallback={
                  <text fg={theme.dim} bg={theme.panelAlt} wrapMode="word">
                    {row().meta?.text}
                  </text>
                }
              >
                <box flexDirection="row" width="100%" alignItems="stretch">
                  {cell(row().left, "left")}
                  {cell(row().right, "right")}
                </box>
              </Show>
            )}
          </Index>
        </Show>
        <Show when={props.disclosure !== false && shouldCollapse()}>
          <text fg={theme.dim} wrapMode="none">
            {expanded()
              ? `… click to collapse (${rows().length} lines)`
              : `… ${hiddenLines()} more lines · click to expand`}
          </text>
        </Show>
      </box>
    </Show>
  )
}
