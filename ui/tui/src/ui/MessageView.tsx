import { type MouseEvent, TextAttributes } from "@opentui/core"
import { createMemo, createSignal, For, Show } from "solid-js"
import type { DiffBlock, ToolView, UiMessage } from "../state"
import { clipCells, ellipsis, lineCount, previewLines, toolCallSummary } from "../util"
import { useDimensions, useStore, useTheme } from "./context"
import { DiffView } from "./DiffView"
import { MarkdownText } from "./MarkdownText"
import { MAX_TOOL_LINES, ToolCard } from "./ToolCard"

export function MessageView(props: { message: UiMessage }) {
  const store = useStore()
  const theme = useTheme()
  const dimensions = useDimensions()
  const message = () => props.message
  let userDown: { x: number; y: number } | null = null
  let reasoningDown: { x: number; y: number } | null = null
  let toolDown: { x: number; y: number } | null = null
  const isClick = (down: { x: number; y: number } | null, event: MouseEvent): boolean => {
    if (!down || event.button !== 0) return false
    return Math.abs(event.x - down.x) <= 1 && Math.abs(event.y - down.y) <= 1
  }
  const toolContent = () => {
    const lines = message().content.split("\n")
    return { head: lines[0] ?? "", body: lines.slice(1).join("\n") }
  }
  const [toolExpanded, setToolExpanded] = createSignal(false)
  const toolPreview = () => previewLines(toolContent().body, toolExpanded() ? MAX_TOOL_LINES : 4)
  const collapsedToolHead = () =>
    clipCells(toolContent().head, Math.max(20, dimensions().width - 12))
  const toolHeadTruncated = () => collapsedToolHead() !== toolContent().head
  const toolHead = () => (toolExpanded() ? toolContent().head : collapsedToolHead())
  const canExpandTool = () => toolHeadTruncated() || lineCount(toolContent().body) > 4
  const normalizePath = (path: string) => path.replace(/\\/g, "/").replace(/^\.\//, "")
  const toolPath = (tool: ToolView) => normalizePath(toolCallSummary(tool.name, tool.summary))
  const isEditTool = (tool: ToolView) => tool.name === "edit_file" || tool.edited === true
  const diffBindings = createMemo(() => {
    const available = new Set(message().diffs)
    const bindings = new Map<string, DiffBlock>()
    const editTools = message().toolCalls.filter(isEditTool)
    // Prefer explicit call identity, regardless of event ordering.
    for (const tool of editTools) {
      const diff = [...available].find(
        (item) => (item as DiffBlock & { tool_call_id?: string }).tool_call_id === tool.id,
      )
      if (diff) {
        bindings.set(tool.id, diff)
        available.delete(diff)
      }
    }
    // Older sessions only have paths. Consume exact matches once in call order;
    // suffix matching confuses names such as a.py and not-a.py.
    for (const tool of editTools) {
      if (bindings.has(tool.id)) continue
      const diff = [...available].find(
        (item) =>
          !(item as DiffBlock & { tool_call_id?: string }).tool_call_id &&
          normalizePath(item.path) === toolPath(tool),
      )
      if (diff) {
        bindings.set(tool.id, diff)
        available.delete(diff)
      }
    }
    return { bindings, remaining: [...available] }
  })
  const diffFor = (tool: ToolView) => diffBindings().bindings.get(tool.id)
  const remainingDiffs = () => diffBindings().remaining
  const toolIds = createMemo(() => message().toolCalls.map((tool) => tool.id))
  const toolsById = createMemo(() => new Map(message().toolCalls.map((tool) => [tool.id, tool])))
  const [expanded, setExpanded] = createSignal(false)
  const longUserMessage = () =>
    message().role === "user" &&
    (lineCount(message().content) > 3 || message().content.length > 600)

  return (
    <box id={`msg-${message().id}`} flexDirection="column" width="100%" marginBottom={1}>
      <Show when={message().unapplied}>
        <text fg={theme.warning} paddingX={1}>
          Correction not applied · recover with /pending
        </text>
      </Show>
      <Show when={message().role === "system"}>
        <box border={["left"]} borderColor={theme.accentAlt} paddingX={2} flexDirection="column">
          <text fg={theme.muted} attributes={TextAttributes.BOLD} wrapMode="none">
            background result
          </text>
          <text fg={theme.muted} wrapMode="word">
            {message().content}
          </text>
        </box>
      </Show>
      <Show when={message().role === "user"}>
        <box
          flexDirection="row"
          paddingX={1}
          paddingY={dimensions().width < 78 ? 0 : 1}
          backgroundColor={dimensions().width >= 78 ? theme.panelAlt : undefined}
          border={["left"]}
          borderColor={theme.user}
          gap={1}
          onMouseDown={(event) => {
            if (event.button === 0) userDown = { x: event.x, y: event.y }
          }}
          onMouseUp={(event) => {
            if (isClick(userDown, event) && longUserMessage()) {
              setExpanded((value) => !value)
            }
            userDown = null
          }}
        >
          <Show
            when={longUserMessage() && !expanded()}
            fallback={
              <text fg={theme.text} wrapMode="word">
                {message().content}
              </text>
            }
          >
            <box flexDirection="column" minWidth={0}>
              <text fg={theme.muted} wrapMode="none">
                {`[pasted ~${lineCount(message().content)} lines]`}
              </text>
              <text fg={theme.dim} wrapMode="word">
                {ellipsis(message().content.split("\n")[0] ?? "", 110)}
              </text>
            </box>
          </Show>
        </box>
      </Show>

      <Show when={message().role === "user" && (message().images?.length ?? 0) > 0}>
        <box flexDirection="row" gap={1} paddingX={2} marginTop={1}>
          <For each={message().images}>
            {(image) => (
              <box flexDirection="row" gap={1} paddingX={1} backgroundColor={theme.panelAlt}>
                <text
                  fg={theme.background}
                  bg={theme.accentAlt}
                  attributes={TextAttributes.BOLD}
                  wrapMode="none"
                >
                  {" file "}
                </text>
                <text fg={theme.muted} wrapMode="none">
                  {image.name ?? image.mime}
                </text>
              </box>
            )}
          </For>
        </box>
      </Show>

      <Show when={message().role === "tool"}>
        <box
          border={["left"]}
          borderColor={theme.border}
          paddingLeft={1}
          paddingX={2}
          flexDirection="column"
          onMouseDown={(event) => {
            if (event.button === 0) toolDown = { x: event.x, y: event.y }
          }}
          onMouseUp={(event) => {
            if (isClick(toolDown, event) && canExpandTool()) setToolExpanded((value) => !value)
            toolDown = null
          }}
        >
          <text fg={theme.tool} attributes={TextAttributes.BOLD} wrapMode="word">
            {toolHead()}
          </text>
          <For each={toolPreview().lines}>
            {(line) => (
              <text fg={theme.muted} wrapMode="word">
                {line || " "}
              </text>
            )}
          </For>
          <Show when={canExpandTool()}>
            <box>
              <text fg={theme.dim} wrapMode="none">
                {toolExpanded()
                  ? toolPreview().hidden > 0
                    ? `… ${toolPreview().hidden} lines omitted (display limit ${MAX_TOOL_LINES}) · click to collapse`
                    : "… click to collapse"
                  : toolPreview().hidden > 0
                    ? `… ${toolPreview().hidden} more lines · click to expand`
                    : "… full command · click to expand"}
              </text>
            </box>
          </Show>
        </box>
      </Show>

      <Show when={message().role === "assistant"}>
        <box flexDirection="column" paddingX={1} width="100%" minWidth={0} flexShrink={0}>
          <Show when={store.showReasoning() && message().reasoning.length > 0}>
            <box flexDirection="column">
              <box
                flexDirection="row"
                gap={1}
                onMouseDown={(event) => {
                  if (event.button === 0) reasoningDown = { x: event.x, y: event.y }
                }}
                onMouseUp={(event) => {
                  if (isClick(reasoningDown, event)) store.toggleReasoning(message().id)
                  reasoningDown = null
                }}
              >
                <text fg={theme.muted} wrapMode="none">
                  {message().reasoningOpen ? "▾" : "▸"}
                </text>
                <text fg={theme.muted} wrapMode="none">
                  {message().streaming ? "thinking…" : "reasoning"}
                </text>
              </box>
              <Show when={message().reasoningOpen}>
                <box paddingLeft={2}>
                  <text fg={theme.muted} wrapMode="word">
                    {message().reasoning}
                  </text>
                </box>
              </Show>
            </box>
          </Show>

          <box flexDirection="column" width="100%" minWidth={0}>
            <MarkdownText content={message().content} streaming={message().streaming} />
          </box>

          <Show when={message().toolCalls.length > 0}>
            <box flexDirection="column" marginTop={message().content ? 1 : 0}>
              <For each={toolIds()}>
                {(id) => {
                  const initial = toolsById().get(id)
                  if (!initial) return null
                  const tool = () => toolsById().get(id) ?? initial
                  return <ToolCard tool={tool()} diff={diffFor(tool())} />
                }}
              </For>
            </box>
          </Show>

          <Show when={remainingDiffs().length > 0}>
            <box flexDirection="column" gap={1} marginTop={1}>
              <For each={remainingDiffs()}>
                {(diff) => <DiffView patch={diff.patch} path={diff.path} />}
              </For>
            </box>
          </Show>
        </box>
      </Show>
    </box>
  )
}
