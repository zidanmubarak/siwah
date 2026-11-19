/** Local TUI configuration: keybind overrides and theme. */

import { mkdirSync, readFileSync, writeFileSync } from "node:fs"
import { homedir } from "node:os"
import { dirname, join } from "node:path"

import { defaultKeybinds, type KeyAction } from "./commands"

export type TuiConfig = {
  theme?: string
  animations: boolean
  keybinds: Record<KeyAction, string>
}

const actions = Object.keys(defaultKeybinds) as KeyAction[]

export function configPath(): string {
  if (process.env.SIWAH_TUI_CONFIG) return process.env.SIWAH_TUI_CONFIG
  const home = process.env.SIWAH_HOME ?? join(homedir(), ".siwah")
  return join(home, "harness", "tui.json")
}

export function loadTuiConfig(path = configPath()): TuiConfig {
  const keybinds: Record<KeyAction, string> = { ...defaultKeybinds }
  let theme: string | undefined
  let animations = true
  try {
    const parsed = JSON.parse(readFileSync(path, "utf8")) as {
      theme?: unknown
      animations?: unknown
      keybinds?: Record<string, unknown>
    }
    if (typeof parsed.theme === "string" && parsed.theme.length > 0) theme = parsed.theme
    if (typeof parsed.animations === "boolean") animations = parsed.animations
    if (parsed.keybinds && typeof parsed.keybinds === "object") {
      for (const action of actions) {
        const value = parsed.keybinds[action]
        if (typeof value === "string" && value.trim().length > 0) keybinds[action] = value.trim()
      }
    }
  } catch {
    // Missing or invalid config keeps the defaults.
  }
  return { theme, animations, keybinds }
}

export function saveTheme(theme: string, path = configPath()): void {
  let current: Record<string, unknown> = {}
  try {
    const parsed = JSON.parse(readFileSync(path, "utf8"))
    if (parsed && typeof parsed === "object") current = parsed as Record<string, unknown>
  } catch {
    // A missing file starts from an empty object.
  }
  current.theme = theme
  mkdirSync(dirname(path), { recursive: true })
  writeFileSync(path, `${JSON.stringify(current, null, 2)}\n`, "utf8")
}
