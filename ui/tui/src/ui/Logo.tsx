import { TextAttributes } from "@opentui/core"
import { For, Show } from "solid-js"
import { useDimensions, useTheme } from "./context"
import { animationsEnabled, useReveal } from "./Motion"

const tagline = "a coding agent that learns with you"
const name = "siwah"

// Each grid row is half a terminal cell. X is the body, + is the bill.
const hornbill = [
  ".......XXX.....",
  "......XXXXX++..",
  ".....XXXX+++++.",
  ".....XX.X++++++",
  "....XXXXX....++",
  "...XXXXXX......",
  "..XXXXXXX......",
  ".XXXXXXX.......",
  "XXXX.XX........",
  "XX...XXX.......",
]
const compactHornbill = [
  "....XX....",
  "...XXX+++.",
  "...X.X++++",
  "..XXXX....",
  ".XXXXX....",
  "XX..XX....",
]
const normalWidth = hornbill[0].length
const compactWidth = compactHornbill[0].length
const gap = 2
const welcomePadding = 4

function packRows(pixels: readonly string[]) {
  return Array.from({ length: pixels.length / 2 }, (_, row) =>
    Array.from(pixels[row * 2], (top, column) => ({ top, bottom: pixels[row * 2 + 1][column] })),
  )
}

const normalRows = packRows(hornbill)
const compactRows = packRows(compactHornbill)

export function Logo(props: { compact?: boolean; animated?: boolean } = {}) {
  const theme = useTheme()
  const dimensions = useDimensions()
  const contentWidth = () => dimensions().width - welcomePadding
  const compact = () => props.compact || contentWidth() < normalWidth + gap + tagline.length
  const showMark = () => contentWidth() >= compactWidth + gap + name.length
  const monochrome = () => theme.name === "mono" || process.env.NO_COLOR !== undefined
  const allowAnimation = animationsEnabled()
  const visible = useReveal(
    normalWidth,
    () => showMark() && !compact() && !monochrome() && allowAnimation && props.animated !== false,
    240,
  )
  const color = (pixel: string) => (monochrome() || pixel === "+" ? theme.text : theme.accent)

  return (
    <box flexDirection="row" alignItems="center" flexShrink={0} gap={gap}>
      <Show when={showMark()}>
        <box flexDirection="column" flexShrink={0}>
          <For each={compact() ? compactRows : normalRows}>
            {(row) => (
              <box flexDirection="row" flexShrink={0} height={1}>
                <For each={row}>
                  {({ top, bottom }, column) => {
                    const upper = top !== "."
                    const lower = bottom !== "."
                    const split = () => upper && lower && color(top) !== color(bottom)
                    return (
                      <text
                        width={1}
                        height={1}
                        flexShrink={0}
                        wrapMode="none"
                        fg={color(upper ? top : bottom)}
                        bg={split() && column() < visible() ? color(bottom) : undefined}
                      >
                        {column() >= visible()
                          ? " "
                          : upper
                            ? lower && !split()
                              ? "█"
                              : "▀"
                            : lower
                              ? "▄"
                              : " "}
                      </text>
                    )
                  }}
                </For>
              </box>
            )}
          </For>
        </box>
      </Show>
      <box flexDirection="column" flexShrink={0}>
        <text fg={theme.text} attributes={TextAttributes.BOLD} selectable wrapMode="none">
          {name}
        </text>
        <Show when={!compact()}>
          <text fg={theme.muted} selectable wrapMode="none">
            {tagline}
          </text>
        </Show>
      </box>
    </box>
  )
}
