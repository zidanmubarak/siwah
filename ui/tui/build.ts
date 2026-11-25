/**
 * Compiled TUI build.
 *
 * `bun build --compile` alone uses Bun's plain JSX transform, which evaluates
 * JSX expressions once. OpenTUI's Solid output needs its Babel-based plugin so
 * signals remain reactive; the package documents Bun.build with this plugin
 * for compiled binaries.
 */

import solidPlugin from "@opentui/solid/bun-plugin"
import type { BunPlugin } from "bun"

const outfile =
  process.env.SIWAH_TUI_OUTFILE ?? (process.platform === "win32" ? "siwah-tui.exe" : "siwah-tui")

// OpenTUI imports a native package per platform/arch. Bundle only the current
// one; the rest (including musl variants) are not installed by Bun and would
// make the build fail. A compiled binary is glibc/macOS/Windows; musl users
// run the Bun source instead.
const nativePackages: Record<string, string> = {
  "darwin-arm64": "@opentui/core-darwin-arm64",
  "darwin-x64": "@opentui/core-darwin-x64",
  "linux-arm64": "@opentui/core-linux-arm64",
  "linux-x64": "@opentui/core-linux-x64",
  "win32-arm64": "@opentui/core-win32-arm64",
  "win32-x64": "@opentui/core-win32-x64",
}
const currentNative = nativePackages[`${process.platform}-${process.arch}`]
const externalNative: BunPlugin = {
  name: "siwah-external-native",
  setup(build) {
    build.onResolve({ filter: /^@opentui\/core-(linux|darwin|win32)-/ }, (args) => {
      if (args.path === currentNative) return undefined
      return { path: args.path, external: true }
    })
  },
}

const result = await Bun.build({
  entrypoints: ["./src/index.tsx"],
  target: "bun",
  outdir: "dist",
  plugins: [solidPlugin, externalNative],
  compile: {
    outfile,
    autoloadBunfig: false,
  },
})

if (!result.success) {
  for (const log of result.logs) console.error(log)
  process.exit(1)
}

console.log(`built ${result.outputs[0]?.path ?? outfile}`)
