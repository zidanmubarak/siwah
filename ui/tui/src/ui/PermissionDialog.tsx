import { TextAttributes } from "@opentui/core"
import { useKeyboard, usePaste } from "@opentui/solid"
import { createEffect, createSignal, For, Show } from "solid-js"
import type { PermissionChoice } from "../api/client"
import { clipCells, clipLines } from "../util"
import { useDimensions, useStore, useTheme } from "./context"

export function PermissionDialog() {
  const store = useStore()
  const theme = useTheme()
  const dimensions = useDimensions()
  const permission = () => store.permission()
  const [denying, setDenying] = createSignal(false)
  const [reason, setReason] = createSignal("")
  createEffect(() => {
    permission()?.requestId
    setDenying(false)
    setReason("")
  })
  const choose = (decision: PermissionChoice) => {
    if (!permission()?.choices.includes(decision)) return
    if (decision === "deny") setDenying(true)
    else void store.resolvePermission(decision)
  }
  usePaste((event) => {
    if (!permission() || store.dialog() || !denying()) return
    event.preventDefault()
    event.stopPropagation()
    if (event.metadata?.kind !== "binary") {
      setReason((value) => value + new TextDecoder().decode(event.bytes).replace(/[\r\n]/g, " "))
    }
  })
  useKeyboard((key) => {
    if (!permission() || store.dialog()) return
    // A modified shortcut is never an approval alias (Ctrl+O is tool output).
    if (key.ctrl || key.meta || (key.shift && !denying())) return
    const name = key.name.toLowerCase()
    if (denying()) {
      key.stopPropagation()
      if (name === "escape") setDenying(false)
      else if (name === "return")
        void store.resolvePermission("deny", reason().trim() || "Denied by user")
      else if (name === "backspace" || name === "delete")
        setReason((value) => Array.from(value).slice(0, -1).join(""))
      else if (
        key.sequence &&
        !key.sequence.startsWith("\x1b") &&
        Array.from(key.sequence).every(
          (char) => char.charCodeAt(0) >= 32 && char.charCodeAt(0) !== 127,
        )
      ) {
        setReason((value) => value + key.sequence)
      }
      return
    }
    if (name === "v") {
      key.stopPropagation()
      store.openDialog({ type: "permission-preview", preview: permission()?.preview ?? "" })
      return
    }
    const choice =
      name === "return" || name === "o" || name === "1"
        ? "once"
        : name === "s" || name === "2"
          ? "session"
          : name === "a" || name === "3"
            ? "always"
            : name === "d" || name === "4"
              ? "deny"
              : null
    if (name === "escape") {
      key.stopPropagation()
      if (permission()?.choices.includes("deny"))
        void store.resolvePermission("deny", "Denied by user")
    } else if (choice) {
      key.stopPropagation()
      choose(choice)
    }
  })
  const choices: { value: PermissionChoice; label: string; key: string }[] = [
    { value: "once", label: "allow once", key: "o" },
    { value: "session", label: "allow session", key: "s" },
    { value: "always", label: "always allow", key: "a" },
    { value: "deny", label: "deny + reason", key: "d" },
  ]
  return (
    <Show when={permission()}>
      {(request) => (
        <box
          border={["left", "top", "bottom"]}
          borderColor={theme.warning}
          backgroundColor={theme.panel}
          flexDirection="column"
          flexShrink={0}
          marginX={2}
          marginBottom={1}
          paddingX={1}
        >
          <box flexDirection="row" gap={1} height={1}>
            <text fg={theme.warning} attributes={TextAttributes.BOLD}>
              {"! permission required"}
            </text>
            <text fg={theme.text} flexGrow={1} minWidth={0} wrapMode="none">
              {request().tool}
            </text>
            <text
              fg={theme.text}
              flexShrink={0}
              wrapMode="none"
              onMouseDown={() =>
                store.openDialog({
                  type: "permission-preview",
                  preview: request().preview,
                })
              }
            >
              {"[v] view full"}
            </text>
          </box>
          <box
            backgroundColor={theme.panelAlt}
            paddingX={1}
            marginY={1}
            maxHeight={dimensions().height < 26 ? 3 : 6}
          >
            <text fg={theme.text} wrapMode="word">
              {clipLines(request().preview, dimensions().height < 26 ? 2 : 4)}
            </text>
          </box>
          <Show
            when={denying()}
            fallback={
              <box flexDirection="row" flexWrap="wrap" gap={1}>
                <For each={choices.filter((choice) => request().choices.includes(choice.value))}>
                  {(choice) => (
                    <text
                      fg={choice.value === "deny" ? theme.error : theme.text}
                      bg={theme.panelAlt}
                      wrapMode="none"
                      onMouseDown={() => choose(choice.value)}
                    >
                      {` [${choice.key}] ${choice.label} `}
                    </text>
                  )}
                </For>
              </box>
            }
          >
            <text
              fg={theme.text}
              wrapMode="word"
            >{`Reason: ${clipCells(reason(), Math.max(10, dimensions().width - 18), true) || "(optional; type here)"}`}</text>
            <text fg={theme.dim}>{"Enter deny · Esc back"}</text>
          </Show>
        </box>
      )}
    </Show>
  )
}
