import { useTerminalDimensions } from "@opentui/solid"
import { createContext, useContext } from "solid-js"

import type { TuiStore } from "../state"
import type { Theme } from "../theme"

export const StoreContext = createContext<TuiStore>()
export const ThemeContext = createContext<Theme>()
export const DimensionsContext = createContext<ReturnType<typeof useTerminalDimensions>>()

/** Share App's resize subscription; isolated components can own their own. */
export function useDimensions(): ReturnType<typeof useTerminalDimensions> {
  return useContext(DimensionsContext) ?? useTerminalDimensions()
}

// The OpenTUI test renderer runs components through Solid's server build in
// some Bun test configurations; context providers are not reliably visible in
// that path. These fallbacks are set by App before rendering and keep the same
// values in the production renderer (where the providers are also present).
let fallbackStore: TuiStore | undefined
let fallbackTheme: Theme | undefined

export function setActiveStore(store: TuiStore): void {
  fallbackStore = store
}

export function setActiveTheme(theme: Theme): void {
  fallbackTheme = theme
}

export function useStore(): TuiStore {
  const store = useContext(StoreContext) ?? fallbackStore
  if (!store) throw new Error("StoreContext is missing")
  return store
}

export function useTheme(): Theme {
  const theme = useContext(ThemeContext) ?? fallbackTheme
  if (!theme) throw new Error("ThemeContext is missing")
  return theme
}
