import { appendFileSync, mkdirSync } from "node:fs"
import { homedir } from "node:os"
import { dirname, join } from "node:path"
import { render } from "@opentui/solid"

import { loadTuiConfig } from "./config"
import { TuiStore } from "./state"
import { resolveTheme } from "./theme"
import { App } from "./ui/App"

function installWarningLog(): void {
  const home = process.env.SIWAH_HOME ?? join(homedir(), ".siwah")
  const path = join(home, "logs", "tui-warnings.log")
  process.removeAllListeners("warning")
  process.on("warning", (warning: Error) => {
    try {
      mkdirSync(dirname(path), { recursive: true })
      appendFileSync(path, `${new Date().toISOString()} ${warning.stack ?? warning.message}\n`)
    } catch {
      // A warning log must never take down the TUI.
    }
  })
}

installWarningLog()

if (process.argv.includes("--version")) {
  console.log("siwah-tui 1.0.1")
  process.exit(0)
}

const store = new TuiStore()
store.theme(resolveTheme(loadTuiConfig().theme))

await render(() => <App store={store} />, {
  exitOnCtrlC: false,
  targetFps: 60,
  backgroundColor: store.theme().background,
})
