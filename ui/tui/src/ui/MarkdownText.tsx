import { TextAttributes } from "@opentui/core"
import { createMemo, Index, Match, Show, Switch } from "solid-js"

import { useDimensions, useStore, useTheme } from "./context"

/**
 * Small streaming markdown renderer.
 *
 * OpenTUI 0.8.3's MarkdownRenderable does not mount reliably when inserted by
 * Solid's dynamic Show/For paths. This renderer keeps the important markdown
 * affordances (headings, nested lists, task lists, quotes, fenced/indented
 * code, inline code, bold, italic, strikethrough, links, images, autolinks,
 * rules, and pipe tables) and remains differential because it is made of Text
 * renderables.
 */
export function classifyMarkdown(content: string): { text: string; code: boolean }[] {
  let fence: string | null = null
  const rows: { text: string; code: boolean }[] = []
  for (const line of content.split("\n")) {
    const marker = /^\s*(```+|~~~+)/.exec(line)
    if (marker) {
      const kind = marker[1]?.[0] ?? "`"
      if (fence === null) {
        fence = kind
        continue
      }
      if (fence === kind) {
        fence = null
        continue
      }
    }
    rows.push({ text: line, code: fence !== null })
  }
  return rows
}

export type InlineKind =
  | "text"
  | "code"
  | "bold"
  | "italic"
  | "bolditalic"
  | "strike"
  | "link"
  | "image"

export type InlineSegment = { text: string; kind: InlineKind; href?: string }

type Align = "left" | "center" | "right"
type LineBlock = { kind: "line"; text: string; code: boolean }
type HeadingBlock = { kind: "heading"; text: string; depth: number }
type CodeBlock = { kind: "code"; lines: string[] }
type TableBlock = { kind: "table"; header: string[]; rows: string[][]; align: Align[] }
export type MarkdownBlock = LineBlock | HeadingBlock | CodeBlock | TableBlock

const inlinePattern =
  /(!\[[^\]]*\]\([^)]+\)|`[^`]+`|\*\*\*[^*]+\*\*\*|\*\*[^*]+\*\*|__[^_]+__|~~[^~]+~~|\*[^*\n]+\*|_[^_\n]+_|\[[^\]]+\]\([^)]+\)|<https?:\/\/[^>]+>|https?:\/\/[^\s<>()]+)/g

export function parseInline(value: string | null | undefined): InlineSegment[] {
  if (!value) return []
  const segments: InlineSegment[] = []
  let last = 0
  inlinePattern.lastIndex = 0
  for (const match of value.matchAll(inlinePattern)) {
    const token = match[0]
    const index = match.index ?? 0
    if (index > last) segments.push({ text: value.slice(last, index), kind: "text" })
    if (token.startsWith("![") && token.includes("](")) {
      const image = /^!\[([^\]]*)\]\(([^)]+)\)$/.exec(token)
      if (image) segments.push({ text: image[1] ?? "", kind: "image", href: image[2] })
      else segments.push({ text: token, kind: "text" })
    } else if (token.startsWith("`") && token.endsWith("`")) {
      segments.push({ text: token.slice(1, -1), kind: "code" })
    } else if (token.startsWith("***") && token.endsWith("***")) {
      segments.push({ text: token.slice(3, -3), kind: "bolditalic" })
    } else if (token.startsWith("**") && token.endsWith("**")) {
      segments.push({ text: token.slice(2, -2), kind: "bold" })
    } else if (token.startsWith("__") && token.endsWith("__")) {
      segments.push({ text: token.slice(2, -2), kind: "bold" })
    } else if (token.startsWith("~~") && token.endsWith("~~")) {
      segments.push({ text: token.slice(2, -2), kind: "strike" })
    } else if (token.startsWith("*") && token.endsWith("*")) {
      segments.push({ text: token.slice(1, -1), kind: "italic" })
    } else if (token.startsWith("_") && token.endsWith("_")) {
      segments.push({ text: token.slice(1, -1), kind: "italic" })
    } else if (token.startsWith("<") && token.endsWith(">")) {
      segments.push({ text: token.slice(1, -1), kind: "link", href: token.slice(1, -1) })
    } else if (/^https?:\/\//.test(token)) {
      segments.push({ text: token, kind: "link", href: token })
    } else {
      const link = /^\[([^\]]+)\]\(([^)]+)\)$/.exec(token)
      if (link) segments.push({ text: link[1] ?? "", kind: "link", href: link[2] })
      else segments.push({ text: token, kind: "text" })
    }
    last = index + token.length
  }
  if (last < value.length) segments.push({ text: value.slice(last), kind: "text" })
  return segments
}

function tableCells(line: string): string[] {
  return line
    .trim()
    .replace(/^\|/, "")
    .replace(/\|$/, "")
    .split("|")
    .map((cell) => cell.trim())
}

function isTableSeparator(line: string): boolean {
  const trimmed = line.trim()
  if (!trimmed.includes("|")) return false
  const cells = trimmed.replace(/^\|/, "").replace(/\|$/, "").split("|")
  return cells.length > 0 && cells.every((cell) => /^\s*:?-{1,}:?\s*$/.test(cell))
}

function tableAlignments(separator: string, columns: number): Align[] {
  const cells = tableCells(separator)
  return Array.from({ length: columns }, (_, index) => {
    const cell = cells[index] ?? ""
    const left = cell.startsWith(":")
    const right = cell.endsWith(":")
    if (left && right) return "center"
    if (right) return "right"
    return "left"
  })
}

export function markdownBlocks(rows: { text: string; code: boolean }[]): MarkdownBlock[] {
  const blocks: MarkdownBlock[] = []
  let index = 0
  while (index < rows.length) {
    const row = rows[index]
    if (row.code) {
      const lines: string[] = []
      let next = index
      while (next < rows.length && rows[next].code) {
        lines.push(rows[next].text)
        next += 1
      }
      blocks.push({ kind: "code", lines })
      index = next
      continue
    }
    if (
      row.text.includes("|") &&
      index + 1 < rows.length &&
      !rows[index + 1].code &&
      isTableSeparator(rows[index + 1].text)
    ) {
      const header = tableCells(row.text)
      const align = tableAlignments(rows[index + 1].text, header.length)
      const body: string[][] = []
      let next = index + 2
      while (
        next < rows.length &&
        !rows[next].code &&
        rows[next].text.includes("|") &&
        rows[next].text.trim().length > 0
      ) {
        body.push(tableCells(rows[next].text))
        next += 1
      }
      blocks.push({ kind: "table", header, rows: body, align })
      index = next
      continue
    }
    if (
      row.text.trim().length > 0 &&
      index + 1 < rows.length &&
      !rows[index + 1].code &&
      /^=+\s*$/.test(rows[index + 1].text)
    ) {
      blocks.push({ kind: "heading", text: row.text, depth: 1 })
      index += 2
      continue
    }
    if (
      row.text.trim().length > 0 &&
      index + 1 < rows.length &&
      !rows[index + 1].code &&
      /^-{2,}\s*$/.test(rows[index + 1].text) &&
      !row.text.includes("|")
    ) {
      blocks.push({ kind: "heading", text: row.text, depth: 2 })
      index += 2
      continue
    }
    blocks.push({ kind: "line", text: row.text, code: false })
    index += 1
  }
  return blocks
}

function plainCell(value: string): string {
  return value
    .replace(/!\[([^\]]*)\]\([^)]*\)/g, "$1")
    .replace(/\[([^\]]+)\]\([^)]*\)/g, "$1")
    .replace(/<([^>]+)>/g, "$1")
    .replace(/`([^`]*)`/g, "$1")
    .replace(/\*\*\*([^*]+)\*\*\*/g, "$1")
    .replace(/\*\*([^*]+)\*\*/g, "$1")
    .replace(/__([^_]+)__/g, "$1")
    .replace(/~~([^~]+)~~/g, "$1")
    .replace(/\*([^*]+)\*/g, "$1")
    .replace(/_([^_]+)_/g, "$1")
}

function useContentWidth() {
  const store = useStore()
  const dimensions = useDimensions()
  return () => {
    const total = dimensions().width
    const panel = store.todos().length > 0 && !store.todosHidden() && total >= 100 ? 34 : 0
    return Math.max(24, total - panel - 10)
  }
}

function wrapCell(value: string, width: number): string[] {
  const text = plainCell(value).replace(/\s+/g, " ").trim()
  if (!text) return [""]
  const words = text.split(" ")
  const lines: string[] = []
  let current = ""
  for (const word of words) {
    if (!current) current = word
    else if (current.length + 1 + word.length <= width) current += ` ${word}`
    else {
      lines.push(current)
      current = word
    }
  }
  if (current) lines.push(current)
  const out: string[] = []
  for (const line of lines) {
    if (line.length <= width) out.push(line)
    else {
      for (let start = 0; start < line.length; start += width) {
        out.push(line.slice(start, start + width))
      }
    }
  }
  if (out.length > 4) {
    const visible = out.slice(0, 4)
    visible[3] = `${(visible[3] ?? "").slice(0, Math.max(0, width - 1))}…`
    return visible
  }
  return out.length > 0 ? out : [""]
}

function padLine(value: string, width: number, align: Align): string {
  const text = value.length > width ? value.slice(0, width) : value
  const remaining = Math.max(0, width - text.length)
  const left = align === "right" ? remaining : align === "center" ? Math.floor(remaining / 2) : 0
  return `${" ".repeat(left)}${text}${" ".repeat(remaining - left)}`
}

function MarkdownTable(props: {
  header: string[]
  rows: string[][]
  align: Align[]
  width: number
}) {
  const theme = useTheme()
  const contentWidth = () => props.width
  const columns = () => Math.max(1, props.header.length, ...props.rows.map((row) => row.length))
  const natural = () =>
    Array.from({ length: columns() }, (_, index) =>
      Math.max(
        3,
        plainCell(props.header[index] ?? "").length,
        ...props.rows.map((row) => plainCell(row[index] ?? "").length),
      ),
    )
  const widths = createMemo(() => {
    const count = columns()
    const budget = Math.max(count * 4, contentWidth() - 2 * Math.max(0, count - 1))
    const naturalWidths = natural()
    const minimum = naturalWidths.map((value, index) =>
      Math.min(value, Math.max(4, Math.min(14, plainCell(props.header[index] ?? "").length))),
    )
    const result = naturalWidths.slice()
    let total = result.reduce((sum, value) => sum + value, 0)
    while (total > budget) {
      let index = -1
      let largest = 0
      for (let column = 0; column < result.length; column += 1) {
        const value = result[column] ?? 0
        if (value > (minimum[column] ?? 4) && value > largest) {
          largest = value
          index = column
        }
      }
      if (index < 0) break
      result[index] = (result[index] ?? 0) - 1
      total -= 1
    }
    while (total > budget) {
      let index = -1
      let largest = 0
      for (let column = 0; column < result.length; column += 1) {
        const value = result[column] ?? 0
        if (value > 3 && value > largest) {
          largest = value
          index = column
        }
      }
      if (index < 0) break
      result[index] = (result[index] ?? 0) - 1
      total -= 1
    }
    return result
  })
  const tableWidth = () =>
    widths().reduce((sum, width) => sum + width, 0) + 2 * Math.max(0, columns() - 1)
  const rowLines = (cells: string[], tone: "header" | "body") => {
    const wrapped = Array.from({ length: columns() }, (_, index) =>
      wrapCell(cells[index] ?? "", widths()[index] ?? 4),
    )
    const height = Math.max(1, ...wrapped.map((lines) => lines.length))
    const result: { text: string; tone: "header" | "body" | "rule" }[] = []
    for (let line = 0; line < height; line += 1) {
      const text = wrapped
        .map((lines, index) =>
          padLine(lines[line] ?? "", widths()[index] ?? 4, props.align[index] ?? "left"),
        )
        .join("  ")
      result.push({ text, tone })
    }
    return result
  }
  const lines = createMemo(() => {
    const result: { text: string; tone: "header" | "body" | "rule" }[] = [
      { text: " ", tone: "rule" },
      ...rowLines(props.header, "header"),
      { text: "─".repeat(tableWidth()), tone: "rule" },
    ]
    for (const cells of props.rows) result.push(...rowLines(cells, "body"))
    result.push({ text: " ", tone: "rule" })
    return result
  })
  return (
    <Index each={lines()}>
      {(line) => (
        <text
          fg={
            line().tone === "rule"
              ? theme.border
              : line().tone === "header"
                ? theme.accent
                : theme.text
          }
          wrapMode="none"
        >
          {line().text}
        </text>
      )}
    </Index>
  )
}

function CodeBlockView(props: { lines: string[] }) {
  const theme = useTheme()
  return (
    <box
      flexDirection="column"
      width="100%"
      backgroundColor={theme.panel}
      border={["left"]}
      borderColor={theme.border}
      paddingLeft={1}
    >
      <Index each={props.lines}>
        {(line) => (
          <text fg={theme.user} wrapMode="word">
            {line() || " "}
          </text>
        )}
      </Index>
    </box>
  )
}

function HeadingView(props: { text: string; depth: number }) {
  const theme = useTheme()
  return (
    <text
      fg={props.depth <= 2 ? theme.accent : theme.accentAlt}
      attributes={TextAttributes.BOLD}
      wrapMode="word"
    >
      {inline(props.text, theme)}
    </text>
  )
}

function LineView(props: { text: string; code: boolean; width: number }) {
  const theme = useTheme()
  // Parse data, not JSX: appending to a paragraph must update the same Text
  // renderable, preserving selection and the viewport's layout anchor.
  const line = createMemo(() => {
    const normal = { body: props.text, prefix: "", tone: "text", bold: false }
    if (props.code) return { ...normal, tone: "code" }
    const heading = /^(#{1,6})\s+(.*)$/.exec(props.text)
    if (heading) return { ...normal, body: heading[2] ?? "", tone: "heading", bold: true }
    const bullet = /^(\s*)([-*+])\s+(.*)$/.exec(props.text)
    if (bullet) {
      const depth = Math.min(4, Math.floor((bullet[1]?.length ?? 0) / 2))
      const indent = "  ".repeat(depth)
      const marker = depth === 0 ? "•" : depth === 1 ? "◦" : "▪"
      const task = /^\[( |x|X)\]\s+(.*)$/.exec(bullet[3] ?? "")
      return {
        ...normal,
        prefix: `${indent}${task ? ((task[1] ?? "").toLowerCase() === "x" ? "☑" : "☐") : marker} `,
        body: task ? (task[2] ?? "") : (bullet[3] ?? ""),
      }
    }
    const ordered = /^(\s*)(\d+)[.)]\s+(.*)$/.exec(props.text)
    if (ordered) {
      const depth = Math.min(4, Math.floor((ordered[1]?.length ?? 0) / 2))
      return { ...normal, prefix: `${"  ".repeat(depth)}${ordered[2]}. `, body: ordered[3] ?? "" }
    }
    const quote = /^(\s*)((?:>\s?)+)(.*)$/.exec(props.text)
    if (quote) {
      const depth = (quote[2]?.match(/>/g) ?? []).length
      return { ...normal, prefix: "│ ".repeat(depth), body: quote[3] ?? "", tone: "quote" }
    }
    if (/^(-{3,}|\*{3,}|_{3,})$/.test(props.text.trim())) {
      return { ...normal, body: "─".repeat(props.width), tone: "rule" }
    }
    if (/^( {4}|\t)/.test(props.text)) {
      return { ...normal, body: props.text.replace(/^( {4}|\t)/, ""), tone: "code" }
    }
    return normal
  })
  const color = () => {
    if (line().tone === "heading") return theme.accent
    if (line().tone === "code") return theme.user
    if (line().tone === "quote") return theme.muted
    if (line().tone === "rule") return theme.border
    return theme.assistant
  }
  return (
    <text
      fg={color()}
      bg={line().tone === "code" ? theme.panel : undefined}
      attributes={line().bold ? TextAttributes.BOLD : 0}
      wrapMode="word"
    >
      <span style={{ fg: theme.dim }}>{line().prefix}</span>
      {line().tone === "code" ? line().body || " " : inline(line().body || " ", theme)}
    </text>
  )
}

function BlockView(props: { block: MarkdownBlock; width: number }) {
  const table = () => props.block as TableBlock
  const code = () => props.block as CodeBlock
  const heading = () => props.block as HeadingBlock
  const line = () => props.block as LineBlock
  return (
    <Switch>
      <Match when={props.block.kind === "table"}>
        <MarkdownTable
          header={table().header}
          rows={table().rows}
          align={table().align}
          width={props.width}
        />
      </Match>
      <Match when={props.block.kind === "code"}>
        <CodeBlockView lines={code().lines} />
      </Match>
      <Match when={props.block.kind === "heading"}>
        <HeadingView text={heading().text} depth={heading().depth} />
      </Match>
      <Match when={props.block.kind === "line"}>
        <LineView text={line().text} code={line().code} width={props.width} />
      </Match>
    </Switch>
  )
}

export function MarkdownText(props: { content: string; streaming: boolean }) {
  const rows = createMemo(() => classifyMarkdown(props.content))
  const blocks = createMemo(() => markdownBlocks(rows()))
  const contentWidth = useContentWidth()

  return (
    <Show when={props.content.length > 0 || props.streaming}>
      <box flexDirection="column" width="100%" minWidth={0} flexShrink={1}>
        <Index each={blocks()}>
          {(block) => <BlockView block={block()} width={contentWidth()} />}
        </Index>
      </box>
    </Show>
  )
}

function inline(value: string | null | undefined, theme: ReturnType<typeof useTheme>) {
  return parseInline(value ?? "").map((segment) => {
    switch (segment.kind) {
      case "code":
        return <span style={{ fg: theme.user, bg: theme.panelAlt }}>{` ${segment.text} `}</span>
      case "bold":
        return (
          <span style={{ fg: theme.text, attributes: TextAttributes.BOLD }}>{segment.text}</span>
        )
      case "italic":
        return (
          <span style={{ fg: theme.text, attributes: TextAttributes.ITALIC }}>{segment.text}</span>
        )
      case "bolditalic":
        return (
          <span
            style={{
              fg: theme.text,
              attributes: TextAttributes.BOLD | TextAttributes.ITALIC,
            }}
          >
            {segment.text}
          </span>
        )
      case "strike":
        return (
          <span
            style={{
              fg: theme.dim,
              attributes: TextAttributes.STRIKETHROUGH,
            }}
          >
            {segment.text}
          </span>
        )
      case "image":
        return <span style={{ fg: theme.accentAlt }}>{`▧ ${segment.text || "image"}`}</span>
      case "link":
        return (
          <span
            style={{
              fg: theme.accent,
              attributes: TextAttributes.UNDERLINE,
            }}
          >
            {segment.text}
          </span>
        )
      default:
        return <span>{segment.text}</span>
    }
  })
}
