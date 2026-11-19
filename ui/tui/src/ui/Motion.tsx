import { createEffect, createSignal, onCleanup } from "solid-js"
import { loadTuiConfig } from "../config"

const pulseFrames = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

export function animationsEnabled(): boolean {
  if (process.env.NO_COLOR !== undefined) return false
  if (["off", "false", "0"].includes(process.env.SIWAH_TUI_ANIMATIONS ?? "")) return false
  return loadTuiConfig().animations
}

/** A finite reveal: reserve the full layout and stop its timer after completion. */
export function useReveal(width: number, enabled: () => boolean, duration = 600) {
  const [visible, setVisible] = createSignal(width)
  let completed = false
  createEffect(() => {
    if (!enabled() || completed) {
      setVisible(width)
      completed = true
      return
    }
    setVisible(0)
    const started = performance.now()
    const timer = setInterval(() => {
      const next = Math.min(width, Math.ceil((width * (performance.now() - started)) / duration))
      setVisible(next)
      if (next >= width) {
        completed = true
        clearInterval(timer)
      }
    }, 30)
    onCleanup(() => clearInterval(timer))
  })
  return visible
}

function useFrames(active: () => boolean, length: number, interval: number) {
  const [frame, setFrame] = createSignal(0)
  createEffect(() => {
    if (!active()) return
    const timer = setInterval(() => setFrame((value) => (value + 1) % length), interval)
    onCleanup(() => clearInterval(timer))
  })
  return frame
}

/** A compact, low-noise spinner for inline running indicators. */
export function Pulse(props: { active: boolean; color: string; animated?: boolean }) {
  const allowed = animationsEnabled()
  const frame = useFrames(
    () => props.active && process.env.NO_COLOR === undefined && (props.animated ?? allowed),
    pulseFrames.length,
    90,
  )
  return <text fg={props.color}>{pulseFrames[frame()]}</text>
}
