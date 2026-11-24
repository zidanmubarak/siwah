import { type ScrollBoxRenderable, TextAttributes } from "@opentui/core"
import { useKeyboard } from "@opentui/solid"
import type { JSX } from "solid-js"
import { overlayBackground } from "../theme"
import { clipCells } from "../util"

import { useDimensions, useStore, useTheme } from "./context"

export function Modal(props: {
  title: string
  children: JSX.Element
  width?: number
  height?: number
  scrollable?: boolean
  escapeCloses?: boolean
  onDismiss?: () => void
}) {
  const theme = useTheme()
  const store = useStore()
  const dimensions = useDimensions()
  const width = () => Math.max(40, Math.min(props.width ?? 76, dimensions().width - 4))
  const height = () => Math.max(8, Math.min(props.height ?? 22, dimensions().height - 4))
  const left = () => Math.max(0, Math.floor((dimensions().width - width()) / 2))
  const top = () => Math.max(0, Math.floor((dimensions().height - height()) / 2))
  let body: ScrollBoxRenderable | undefined
  useKeyboard((key) => {
    if (!store.dialog()) return
    if (key.name === "escape" && props.escapeCloses !== false) {
      key.stopPropagation()
      store.closeDialog()
    } else if (props.scrollable !== false && (key.name === "pageup" || key.name === "pagedown")) {
      key.stopPropagation()
      body?.scrollBy(
        (key.name === "pageup" ? -1 : 1) *
          Math.max(1, (body?.verticalScrollBar.viewportSize ?? 8) - 1),
      )
    }
  })

  return (
    <>
      <box
        position="absolute"
        left={0}
        top={0}
        width="100%"
        height="100%"
        zIndex={99}
        backgroundColor={overlayBackground(theme, "background")}
      />
      <box
        position="absolute"
        left={left()}
        top={top()}
        width={width()}
        height={height()}
        zIndex={100}
        border
        borderStyle="rounded"
        borderColor={theme.border}
        backgroundColor={overlayBackground(theme)}
        flexDirection="column"
        paddingX={1}
      >
        <box
          flexDirection="row"
          height={2}
          flexShrink={0}
          justifyContent="space-between"
          paddingX={1}
        >
          <text
            fg={theme.accent}
            attributes={TextAttributes.BOLD}
            flexGrow={1}
            minWidth={0}
            wrapMode="none"
          >
            {clipCells(props.title, width() - 15)}
          </text>
          <text
            fg={theme.dim}
            width={5}
            flexShrink={0}
            onMouseDown={() => (props.onDismiss ?? (() => store.closeDialog()))()}
          >
            {"esc ×"}
          </text>
        </box>
        <scrollbox
          id="dialog-body"
          ref={(node) => {
            body = node
          }}
          flexGrow={1}
          minHeight={0}
          width="100%"
          scrollX={false}
          scrollbarOptions={{ visible: false }}
          contentOptions={{ paddingX: 1 }}
          scrollY={props.scrollable !== false}
        >
          {props.children}
        </scrollbox>
      </box>
    </>
  )
}
