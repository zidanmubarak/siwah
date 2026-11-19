import { expect, test } from "bun:test"

import { degradeColor, degradeTheme, resolveTheme } from "../src/theme"

test("256-color and 16-color degradation returns palette colors", () => {
  expect(degradeColor("#ff0000", "256")).toBe("#ff0000")
  const ansi = degradeColor("#123456", "16")
  expect(ansi.startsWith("#")).toBe(true)
  expect(ansi.length).toBe(7)
  expect(degradeColor("transparent", "256")).toBe("transparent")
})

test("degraded themes keep the theme identity and semantic diff backgrounds", () => {
  const theme = resolveTheme("siwah-dark", false)
  const degraded = degradeTheme(theme, "256")
  expect(degraded.name).toBe("siwah-dark")
  expect(degraded.text.startsWith("#")).toBe(true)
  expect(degraded.background.startsWith("#")).toBe(true)
  expect(degraded.diffAddedBg).toBe("#005f5f")
  expect(degraded.diffRemovedBg).toBe("#5f0000")
  expect(degradeTheme(theme, "16").diffAddedBg).not.toBe(degradeTheme(theme, "16").diffRemovedBg)
  expect(degradeTheme(resolveTheme("mono", false), "256").diffAddedBg).toBe(
    resolveTheme("mono", false).diffAddedBg,
  )
})
