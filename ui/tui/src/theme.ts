/**
 * TUI themes. Colors are only one channel of information: every status also has
 * a text label and a symbol. NO_COLOR selects the mono theme.
 */

export type ThemeName = "siwah-dark" | "siwah-light" | "tokyo-night" | "solarized" | "mono"

export type Theme = {
  name: ThemeName
  dark: boolean
  background: string
  panel: string
  panelAlt: string
  border: string
  borderFocus: string
  text: string
  muted: string
  dim: string
  accent: string
  accentAlt: string
  success: string
  warning: string
  error: string
  user: string
  assistant: string
  tool: string
  diffAddedBg: string
  diffRemovedBg: string
  selection: string
}

/** Overlays must erase underlying glyphs even when the terminal theme is transparent. */
export function overlayBackground(
  theme: Theme,
  layer: "background" | "panel" | "panelAlt" = "panel",
): string {
  const color = theme[layer]
  return color === "transparent" ? (theme.dark ? "#000000" : "#ffffff") : color
}

const themes: Record<ThemeName, Theme> = {
  "siwah-dark": {
    name: "siwah-dark",
    dark: true,
    background: "#15151b",
    panel: "#151B24",
    panelAlt: "#1C2532",
    border: "#2D3A4B",
    borderFocus: "#d4a63a",
    text: "#d7dae0",
    muted: "#9CAAC0",
    dim: "#73839C",
    accent: "#d4a63a",
    accentAlt: "#c2324f",
    success: "#8ED3A0",
    warning: "#F0B35E",
    error: "#F07C86",
    user: "#8CB9F6",
    assistant: "#d7dae0",
    tool: "#d4a63a",
    diffAddedBg: "#1d3329",
    diffRemovedBg: "#352029",
    selection: "#28364B",
  },
  "siwah-light": {
    name: "siwah-light",
    dark: false,
    background: "#F6F7FA",
    panel: "#FFFFFF",
    panelAlt: "#EEF1F6",
    border: "#D6DEE9",
    borderFocus: "#B45309",
    text: "#17202B",
    muted: "#556274",
    dim: "#667385",
    accent: "#B45309",
    accentAlt: "#0E766E",
    success: "#177245",
    warning: "#9A6700",
    error: "#B42318",
    user: "#1D4ED8",
    assistant: "#17202B",
    tool: "#8A5A00",
    diffAddedBg: "#e2edf5",
    diffRemovedBg: "#f4e1e4",
    selection: "#DCE6F5",
  },
  "tokyo-night": {
    name: "tokyo-night",
    dark: true,
    background: "#1a1b26",
    panel: "#202330",
    panelAlt: "#292e42",
    border: "#414868",
    borderFocus: "#7aa2f7",
    text: "#c0caf5",
    muted: "#a9b1d6",
    dim: "#565f89",
    accent: "#7aa2f7",
    accentAlt: "#bb9af7",
    success: "#9ece6a",
    warning: "#e0af68",
    error: "#f7768e",
    user: "#7dcfff",
    assistant: "#c0caf5",
    tool: "#e0af68",
    diffAddedBg: "#12351f",
    diffRemovedBg: "#3a1620",
    selection: "#2f3b63",
  },
  solarized: {
    name: "solarized",
    dark: true,
    background: "#002b36",
    panel: "#073642",
    panelAlt: "#0a4350",
    border: "#586e75",
    borderFocus: "#268bd2",
    text: "#eee8d5",
    muted: "#93a1a1",
    dim: "#657b83",
    accent: "#268bd2",
    accentAlt: "#6c71c4",
    success: "#859900",
    warning: "#b58900",
    error: "#dc322f",
    user: "#2aa198",
    assistant: "#eee8d5",
    tool: "#b58900",
    diffAddedBg: "#0b3b2e",
    diffRemovedBg: "#4a1f20",
    selection: "#17505e",
  },
  mono: {
    name: "mono",
    dark: true,
    background: "transparent",
    panel: "transparent",
    panelAlt: "transparent",
    border: "#888888",
    borderFocus: "#ffffff",
    text: "#ffffff",
    muted: "#bbbbbb",
    dim: "#777777",
    accent: "#ffffff",
    accentAlt: "#dddddd",
    success: "#ffffff",
    warning: "#dddddd",
    error: "#ffffff",
    user: "#ffffff",
    assistant: "#ffffff",
    tool: "#dddddd",
    diffAddedBg: "transparent",
    diffRemovedBg: "transparent",
    selection: "#444444",
  },
}

export const themeNames = Object.keys(themes) as ThemeName[]

const ansi16 = [
  "#000000",
  "#800000",
  "#008000",
  "#808000",
  "#000080",
  "#800080",
  "#008080",
  "#c0c0c0",
  "#808080",
  "#ff0000",
  "#00ff00",
  "#ffff00",
  "#0000ff",
  "#ff00ff",
  "#00ffff",
  "#ffffff",
]

function xterm256(): string[] {
  const palette = [...ansi16]
  const levels = [0, 95, 135, 175, 215, 255]
  for (const red of levels) {
    for (const green of levels) {
      for (const blue of levels) {
        palette.push(
          `#${[red, green, blue].map((value) => value.toString(16).padStart(2, "0")).join("")}`,
        )
      }
    }
  }
  for (let index = 0; index < 24; index += 1) {
    const value = 8 + index * 10
    const hex = value.toString(16).padStart(2, "0")
    palette.push(`#${hex}${hex}${hex}`)
  }
  return palette
}

const palettes = { "256": xterm256(), "16": ansi16 } as const

function rgb(color: string): [number, number, number] | null {
  const match = /^#([0-9a-f]{6})$/i.exec(color)
  if (!match) return null
  const value = Number.parseInt(match[1] ?? "", 16)
  return [(value >> 16) & 0xff, (value >> 8) & 0xff, value & 0xff]
}

/** Map a hex color to the nearest color a 256/16-color terminal can show. */
export function degradeColor(color: string, depth: "256" | "16"): string {
  const source = rgb(color)
  if (!source) return color
  let best = color
  let distance = Number.POSITIVE_INFINITY
  for (const candidate of palettes[depth]) {
    const target = rgb(candidate)
    if (!target) continue
    const delta =
      (source[0] - target[0]) ** 2 + (source[1] - target[1]) ** 2 + (source[2] - target[2]) ** 2
    if (delta < distance) {
      distance = delta
      best = candidate
    }
  }
  return best
}

export function degradeTheme(theme: Theme, depth: "truecolor" | "256" | "16"): Theme {
  if (depth === "truecolor") return theme
  const result: Record<string, unknown> = { ...theme }
  for (const [key, value] of Object.entries(theme)) {
    if (typeof value === "string" && value.startsWith("#")) {
      result[key] = degradeColor(value, depth)
    }
  }
  // Preserve semantic row highlights when subtle truecolor tints quantize gray.
  // Keep other presets' intentional palettes untouched.
  if (theme.name === "siwah-dark" || theme.name === "siwah-light") {
    // Limited-color terminals cannot render pale tints; use a dark blue/red
    // that still reads as a highlighted row without a solid bright block.
    result.diffAddedBg = depth === "16" ? "#008000" : theme.dark ? "#005f5f" : "#dcefe3"
    result.diffRemovedBg = depth === "16" ? "#800000" : theme.dark ? "#5f0000" : "#f2e0e3"
  }
  return result as Theme
}

export function resolveTheme(name?: string, noColor = process.env.NO_COLOR !== undefined): Theme {
  if (noColor) return themes.mono
  const base =
    name && name in themes
      ? themes[name as ThemeName]
      : process.env.SIWAH_THEME === "light"
        ? themes["siwah-light"]
        : themes["siwah-dark"]
  return degradeTheme(base, colorDepth())
}

/** Un-degraded palette for tests and palette inspection. */
export function baseTheme(name: ThemeName): Theme {
  return themes[name]
}

export function colorDepth(): "truecolor" | "256" | "16" {
  if (process.env.NO_COLOR !== undefined) return "16"
  const colorterm = (process.env.COLORTERM ?? "").toLowerCase()
  if (colorterm === "truecolor" || colorterm === "24bit") return "truecolor"
  const term = (process.env.TERM ?? "").toLowerCase()
  if (term === "dumb" || term === "") return "256"
  if (term.includes("256color") || term.includes("xterm")) return "256"
  return "16"
}
