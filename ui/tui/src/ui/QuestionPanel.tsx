import { type ScrollBoxRenderable, TextAttributes } from "@opentui/core"
import { useKeyboard } from "@opentui/solid"
import { createEffect, createSignal, For, Show } from "solid-js"
import { bindingMatches } from "../commands"
import type { QuestionState } from "../state"
import { clipCells } from "../util"
import { useDimensions, useStore, useTheme } from "./context"

export function QuestionPanel(props: { cancelKey: string }) {
  const store = useStore()
  return (
    <Show when={store.question()} keyed>
      {(request) => <QuestionContent request={request} cancelKey={props.cancelKey} />}
    </Show>
  )
}

function QuestionContent(props: { request: QuestionState; cancelKey: string }) {
  const store = useStore()
  const theme = useTheme()
  const dimensions = useDimensions()
  const options = props.request.options
  const multiple = () => props.request.multiple === true
  const [selected, setSelected] = createSignal(0)
  const [chosen, setChosen] = createSignal<string[]>([])
  const [custom, setCustom] = createSignal(options.length === 0 && props.request.allow_custom)
  let customDraft = ""
  const available = () => !store.dialog() && !store.permission()
  const enabled = () => available() && !store.questionSubmitting()
  const visibleCount = () => (dimensions().height < 28 ? 3 : 5)
  const startIndex = () => Math.max(0, Math.min(selected(), options.length - visibleCount()))
  const visibleOptions = () => options.slice(startIndex(), startIndex() + visibleCount())
  let preview: ScrollBoxRenderable | undefined
  const answer = (value: string | null) => {
    if (enabled()) void store.answerQuestion(value)
  }
  const toggle = (label: string) => {
    setChosen((current) =>
      current.includes(label) ? current.filter((item) => item !== label) : [...current, label],
    )
  }
  const submit = (customValue?: string) => {
    if (!multiple()) {
      if (customValue !== undefined) {
        if (customValue.trim()) answer(customValue)
        return
      }
      const option = options[selected()]
      if (option) answer(option.label)
      return
    }
    const values = [...chosen()]
    const extra = customValue?.trim()
    if (extra) values.push(extra)
    if (values.length > 0) answer(values.join("\n"))
  }
  const showCustom = () => {
    if (enabled() && props.request.allow_custom) setCustom(true)
  }
  createEffect(() => {
    selected()
    preview?.scrollBy({ x: 0, y: -preview.scrollHeight })
  })

  useKeyboard((key) => {
    if (!available()) return
    if (bindingMatches(key, props.cancelKey)) {
      key.stopPropagation()
      answer(null)
      return
    }
    if (key.ctrl || key.meta) return
    const name = key.name.toLowerCase()
    if (name === "pageup" || name === "pagedown") {
      key.stopPropagation()
      preview?.scrollBy((name === "pageup" ? -1 : 1) * (preview.height || 1))
      return
    }
    if (custom()) {
      if (name === "tab" && options.length) {
        key.stopPropagation()
        setCustom(false)
      }
      return
    }
    if (name === "up" || name === "down") {
      key.stopPropagation()
      setSelected((value) =>
        Math.max(0, Math.min(options.length - 1, value + (name === "up" ? -1 : 1))),
      )
    } else if (name === "tab" && props.request.allow_custom) {
      key.stopPropagation()
      showCustom()
    } else if (multiple() && (name === "space" || name === " ")) {
      key.stopPropagation()
      const option = options[selected()]
      if (option) toggle(option.label)
    } else if (name === "return") {
      key.stopPropagation()
      submit()
    } else if (/^[1-8]$/.test(name)) {
      const option = options[Number(name) - 1]
      if (option) {
        key.stopPropagation()
        if (multiple()) toggle(option.label)
        else answer(option.label)
      }
    }
  })

  return (
    <box
      id="question-panel"
      visible={available()}
      border={["left", "top", "bottom"]}
      borderColor={theme.accent}
      backgroundColor={theme.panel}
      flexDirection="column"
      flexShrink={0}
      marginX={2}
      marginBottom={1}
      paddingX={1}
    >
      <text fg={theme.accent} attributes={TextAttributes.BOLD}>
        {props.request.header || "siwah needs your input"}
      </text>
      <scrollbox
        ref={(node) => {
          preview = node
        }}
        maxHeight={dimensions().height < 28 ? 4 : 7}
        flexShrink={0}
        scrollX={false}
        verticalScrollbarOptions={{ visible: false }}
      >
        <text fg={theme.text} wrapMode="word">
          {props.request.question}
        </text>
        <Show when={!custom() && options[selected()]?.description}>
          {(description) => (
            <text fg={theme.muted} wrapMode="word">
              {description()}
            </text>
          )}
        </Show>
      </scrollbox>
      <Show
        when={custom()}
        fallback={
          <For each={visibleOptions()}>
            {(option, index) => {
              const position = () => startIndex() + index()
              const active = () => position() === selected()
              return (
                <text
                  fg={active() ? theme.accent : theme.text}
                  bg={active() ? theme.panelAlt : undefined}
                  wrapMode="none"
                  onMouseOver={() => setSelected(position())}
                  onMouseDown={() => (multiple() ? toggle(option.label) : answer(option.label))}
                >
                  {clipCells(
                    `${active() ? ">" : " "} ${
                      multiple() ? (chosen().includes(option.label) ? "[x]" : "[ ]") : " "
                    } ${position() + 1}. ${option.label}`,
                    Math.max(16, dimensions().width - 9),
                    true,
                  )}
                </text>
              )
            }}
          </For>
        }
      >
        <input
          id="question-custom-input"
          focused={available()}
          maxLength={4000}
          value={customDraft}
          onInput={(value) => {
            customDraft = value
          }}
          textColor={theme.text}
          placeholder="Type an answer"
          onSubmit={() => submit(customDraft)}
        />
      </Show>
      <Show
        when={store.questionSubmitting()}
        fallback={
          <box flexDirection="row" flexWrap="wrap" gap={2}>
            <text fg={theme.dim}>
              {custom()
                ? "Enter send"
                : multiple()
                  ? "Space toggle · Enter answer"
                  : "↑↓ select · Enter answer"}
            </text>
            <Show when={props.request.allow_custom && options.length > 0}>
              <text fg={theme.accent} onMouseDown={() => enabled() && setCustom((value) => !value)}>
                {custom() ? "Tab choices" : "Tab custom"}
              </text>
            </Show>
            <text fg={theme.muted} onMouseDown={() => answer(null)}>
              Esc dismiss
            </text>
          </box>
        }
      >
        <text fg={theme.dim}>Sending answer…</text>
      </Show>
    </box>
  )
}
