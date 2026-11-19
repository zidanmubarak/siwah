import { expect, test } from "bun:test"
import { mkdtempSync, writeFileSync } from "node:fs"
import { tmpdir } from "node:os"
import { join } from "node:path"

import { loadTuiConfig, saveTheme } from "../src/config"

test("missing config keeps the default keybinds", () => {
  const config = loadTuiConfig(join(tmpdir(), `siwah-missing-${Date.now()}.json`))
  expect(config.keybinds.palette).toBe("ctrl+p")
  expect(config.keybinds.mode).toBe("tab")
  expect(config.theme).toBeUndefined()
  expect(config.animations).toBe(true)
})

test("reduced-motion config survives saving a theme", () => {
  const directory = mkdtempSync(join(tmpdir(), "siwah-tui-motion-"))
  const path = join(directory, "tui.json")
  writeFileSync(path, JSON.stringify({ animations: false }), "utf8")
  saveTheme("solarized", path)
  expect(loadTuiConfig(path).animations).toBe(false)
})

test("theme selection is persisted without dropping keybinds", () => {
  const path = join(tmpdir(), `siwah-tui-${Date.now()}.json`)
  writeFileSync(path, JSON.stringify({ keybinds: { palette: "ctrl+k" } }), "utf8")
  saveTheme("solarized", path)
  const config = loadTuiConfig(path)
  expect(config.theme).toBe("solarized")
  expect(config.keybinds.palette).toBe("ctrl+k")
})

test("config overrides keybinds and theme", () => {
  const directory = mkdtempSync(join(tmpdir(), "siwah-tui-config-"))
  const path = join(directory, "tui.json")
  writeFileSync(
    path,
    JSON.stringify({ theme: "solarized", keybinds: { palette: "ctrl+k", bogus: "x" } }),
    "utf8",
  )
  const config = loadTuiConfig(path)
  expect(config.theme).toBe("solarized")
  expect(config.keybinds.palette).toBe("ctrl+k")
  expect(config.keybinds.mode).toBe("tab")
})

test("invalid config values fall back safely", () => {
  const directory = mkdtempSync(join(tmpdir(), "siwah-tui-config-"))
  const path = join(directory, "tui.json")
  writeFileSync(path, JSON.stringify({ keybinds: { palette: 42, history: "  " } }), "utf8")
  const config = loadTuiConfig(path)
  expect(config.keybinds.palette).toBe("ctrl+p")
  expect(config.keybinds.history).toBe("ctrl+r")
})
