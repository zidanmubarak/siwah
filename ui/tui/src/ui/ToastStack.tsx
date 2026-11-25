import { For, Show } from "solid-js"
import { overlayBackground } from "../theme"

import { useDimensions, useStore, useTheme } from "./context"

export function ToastStack(props: { bottom?: number } = {}) {
  const store = useStore()
  const theme = useTheme()
  const dimensions = useDimensions()

  return (
    <Show when={store.toasts().length > 0}>
      <scrollbox
        position="absolute"
        right={2}
        top={3}
        width={Math.min(52, dimensions().width - 4)}
        maxHeight={Math.max(
          1,
          dimensions().height - (props.bottom ?? store.promptHeight() + 5) - 3,
        )}
        scrollX={false}
        stickyScroll
        stickyStart="bottom"
        scrollbarOptions={{ visible: false }}
        zIndex={80}
        contentOptions={{ gap: 1 }}
      >
        <For each={store.toasts()}>
          {(toast) => {
            const color = () =>
              toast.tone === "error"
                ? theme.error
                : toast.tone === "warning"
                  ? theme.warning
                  : toast.tone === "success"
                    ? theme.success
                    : theme.accent
            const glyph =
              toast.tone === "error"
                ? "✗"
                : toast.tone === "warning"
                  ? "!"
                  : toast.tone === "success"
                    ? "✓"
                    : "·"
            return (
              <box
                id={`toast-${toast.id}`}
                border={["left", "bottom"]}
                borderColor={color()}
                backgroundColor={overlayBackground(theme, "panelAlt")}
                paddingX={1}
                flexDirection="column"
              >
                <box flexDirection="row" gap={1} justifyContent="space-between">
                  <text fg={color()}>{`${glyph} ${toast.tone.toUpperCase()}`}</text>
                  <text
                    fg={theme.muted}
                    onMouseDown={(event) => {
                      event.stopPropagation()
                      store.toasts((current) => current.filter((item) => item.id !== toast.id))
                    }}
                  >
                    {"×"}
                  </text>
                </box>
                <text fg={theme.text} wrapMode="word">
                  {toast.text}
                </text>
              </box>
            )
          }}
        </For>
      </scrollbox>
    </Show>
  )
}
