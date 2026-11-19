import { TextAttributes } from "@opentui/core"
import { Show } from "solid-js"
import { useStore, useTheme } from "./context"

/**
 * Durable run outcome. Toasts expire after a few seconds, but a run that
 * stalled, hit a limit, or failed must remain visible in the transcript.
 */
export function RunNotice() {
  const store = useStore()
  const theme = useTheme()
  const notice = () => store.runNotice()

  return (
    <Show when={notice()}>
      {(value) => {
        const color = () => (value().tone === "error" ? theme.error : theme.warning)
        return (
          <box
            id="run-notice"
            flexDirection="column"
            width="100%"
            flexShrink={0}
            marginTop={1}
            border={["left"]}
            borderColor={color()}
            backgroundColor={theme.panel}
            paddingLeft={1}
            paddingRight={1}
          >
            <box flexDirection="row" justifyContent="space-between" gap={1}>
              <text fg={color()} attributes={TextAttributes.BOLD} wrapMode="none" flexGrow={1}>
                {value().title}
              </text>
              <text fg={theme.dim} wrapMode="none" onMouseDown={() => store.runNotice(null)}>
                {"×"}
              </text>
            </box>
            <text fg={theme.text} wrapMode="word">
              {value().text}
            </text>
          </box>
        )
      }}
    </Show>
  )
}
