import { TextAttributes } from "@opentui/core"
import { For, Show } from "solid-js"
import { useDimensions, useTheme } from "./context"
import { animationsEnabled, useReveal } from "./Motion"

// User-supplied half-block silhouette: gold hilt, white-iron blade/wordmark.
// This is a description-based mark, not a verified historical weapon drawing.
export const siwahMark: [string, string][] = [
  ["           ", "                ▄▄▄▄▄▄▄▄▄██▀"],
  ["        ███", "▄▄▄▄▄▄█████████████████▀▀"],
  ["        █▀█", "██████████████████▀▀▀"],
  ["        ███", "████▀▀▀▀▀▀▀▀▀▀"],
  ["     ██████", "   ▄▀▀▀ ▀ █   █  ▀▀▄ █"],
  ["     ████  ", "    ▀▀▄ █ █▄▀▄█ ▄▀▀█ █▀▀▄"],
  ["   ████████", "   ▀▀▀  ▀ ▀   ▀  ▀▀▀ ▀  ▀"],
]

export const siwahLine = "◆━━━━━━━━━━━━━━━━◥"
const markWidth = Math.max(...siwahMark.map(([hilt, rest]) => hilt.length + rest.length))
const bladeWidth = Math.max(...siwahMark.map(([, rest]) => rest.length))

export function Logo(props: { compact?: boolean; animated?: boolean } = {}) {
  const theme = useTheme()
  const dimensions = useDimensions()
  const large = () => !props.compact && dimensions().width >= markWidth + 4
  const allowed = animationsEnabled()
  const visible = useReveal(
    bladeWidth,
    () =>
      large() &&
      theme.name !== "mono" &&
      process.env.NO_COLOR === undefined &&
      (props.animated ?? allowed),
  )
  return (
    <box flexDirection="column" alignItems="center" flexShrink={0}>
      <Show
        when={large()}
        fallback={
          <box flexDirection="column" alignItems="center">
            <box flexDirection="row">
              <text fg={theme.accentAlt}>{"◆"}</text>
              <text fg={theme.text}>{siwahLine.slice(1)}</text>
            </box>
            <text fg={theme.accent} attributes={TextAttributes.BOLD}>
              {"siwah"}
            </text>
          </box>
        }
      >
        <box flexDirection="column" flexShrink={0}>
          <For each={siwahMark}>
            {([hilt, rest]) => (
              <box flexDirection="row" flexShrink={0} height={1}>
                <text fg={theme.accent} attributes={TextAttributes.BOLD} selectable wrapMode="none">
                  {hilt}
                </text>
                <text fg={theme.text} attributes={TextAttributes.BOLD} selectable wrapMode="none">
                  {rest.slice(0, visible()).padEnd(rest.length)}
                </text>
              </box>
            )}
          </For>
        </box>
      </Show>
      <Show when={!props.compact}>
        <text fg={theme.dim} marginTop={1} selectable>
          {"a coding agent that learns with you"}
        </text>
      </Show>
    </box>
  )
}
