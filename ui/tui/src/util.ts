import { execFileSync } from "node:child_process"
import { chmodSync, mkdirSync, readFileSync, writeFileSync } from "node:fs"
import { homedir } from "node:os"
import { basename, dirname, join, resolve } from "node:path"

export function formatTokens(value: number | null | undefined): string {
  if (value === null || value === undefined) return "0"
  if (value >= 1_000_000) {
    const millions = value / 1_000_000
    return `${Number.isInteger(millions) ? millions : millions.toFixed(1)}M`
  }
  if (value >= 1_000) {
    const thousands = value / 1_000
    return `${Number.isInteger(thousands) ? thousands : thousands.toFixed(1)}k`
  }
  return String(value)
}

export function formatAllowance(value: number | null | undefined): string {
  if (value === null || value === undefined) return "$0.000000"
  return `$${value.toFixed(6)}`
}

const graphemes = new Intl.Segmenter(undefined, { granularity: "grapheme" })

/** Clip by terminal cells without splitting CJK, emoji or combining sequences. */
export function clipCells(value: string, width: number, tail = false): string {
  if (width <= 0) return ""
  if (Bun.stringWidth(value) <= width) return value
  const parts = Array.from(graphemes.segment(value), (part) => part.segment)
  if (tail) parts.reverse()
  let result = ""
  let used = 1 // ellipsis
  for (const part of parts) {
    const size = Bun.stringWidth(part)
    if (used + size > width) break
    result = tail ? part + result : result + part
    used += size
  }
  return tail ? `…${result}` : `${result}…`
}

export function projectName(cwd: string): string {
  return basename(cwd) || cwd
}

export function fileType(path: string): string {
  const extension = path.split(".").pop()?.toLowerCase()
  const known: Record<string, string> = {
    py: "python",
    js: "javascript",
    jsx: "javascript",
    ts: "typescript",
    tsx: "typescriptreact",
    md: "markdown",
    zig: "zig",
    json: "json",
    toml: "toml",
    sh: "bash",
    bash: "bash",
  }
  return extension ? (known[extension] ?? extension) : "text"
}

export function gitBranch(cwd: string): string {
  try {
    return execFileSync("git", ["-C", cwd, "rev-parse", "--abbrev-ref", "HEAD"], {
      encoding: "utf8",
      timeout: 1500,
      stdio: ["ignore", "pipe", "ignore"],
    }).trim()
  } catch {
    return "no-git"
  }
}

export function lineCount(value: string): number {
  return value === "" ? 0 : value.split("\n").length
}

export function clipLines(value: string, maxLines: number): string {
  const lines = value.split("\n")
  if (lines.length <= maxLines) return value
  return `${lines.slice(0, maxLines).join("\n")}\n… ${lines.length - maxLines} more lines`
}

export function ellipsis(value: string, max: number): string {
  if (max <= 1) return "…"
  if (value.length <= max) return value
  return `${value.slice(0, max - 1)}…`
}

export function previewLines(value: string, maxLines = 8): { lines: string[]; hidden: number } {
  const lines = value === "" ? [] : value.split("\n")
  if (lines.length <= maxLines) return { lines, hidden: 0 }
  return { lines: lines.slice(0, maxLines), hidden: lines.length - maxLines }
}

/** Turn a tool's raw JSON arguments into a short, human-readable label. */
export function toolCallSummary(name: string, summary: string): string {
  const raw = summary.trim()
  if (!raw.startsWith("{")) return raw
  try {
    const value = JSON.parse(raw) as Record<string, unknown>
    const path = typeof value.path === "string" ? value.path : ""
    if (["read_file", "write_file", "edit_file", "list_dir"].includes(name) && path) return path
    const pattern = typeof value.pattern === "string" ? value.pattern : ""
    if ((name === "glob" || name === "grep") && pattern) return pattern
    if (name === "repo_map" && typeof value.query === "string") return value.query
    if (name === "bash" && typeof value.command === "string") return `$ ${value.command}`
    if (typeof value.url === "string") return value.url
  } catch {
    // Keep the backend summary when it is not valid JSON.
  }
  return raw
}

/** True when tool output looks like a unified diff, not ordinary text. */
export function isDiffLike(value: string): boolean {
  return value.includes("@@") && value.split("\n").some((line) => /^[-+][^-+]/.test(line))
}

/** Count added and removed lines in a unified diff. */
export function diffCounts(patch: string): { added: number; removed: number } {
  let added = 0
  let removed = 0
  for (const line of patch.split("\n")) {
    if (line.startsWith("+") && !line.startsWith("+++")) added += 1
    else if (line.startsWith("-") && !line.startsWith("---")) removed += 1
  }
  return { added, removed }
}

/** Keep a selected row visible inside a fixed-size list window. */
export function windowStart(selected: number, total: number, size: number): number {
  if (total <= size) return 0
  const half = Math.floor(size / 2)
  return Math.max(0, Math.min(Math.max(0, selected - half), total - size))
}

export function maskedSecret(value: string, max = 60): string {
  if (!value) return "(empty)"
  return `${"*".repeat(Math.min(value.length, max))}${value.length > max ? "…" : ""}`
}

/** Copy text with the host clipboard utility; OSC52 remains the renderer's job. */
export function copyText(value: string): boolean {
  if (value.length === 0) return false
  const commands: Array<[string, string[]]> =
    process.platform === "win32"
      ? [["clip", []]]
      : process.platform === "darwin"
        ? [["pbcopy", []]]
        : [
            ["wl-copy", []],
            ["xclip", ["-selection", "clipboard"]],
            ["xsel", ["--clipboard", "--input"]],
          ]
  for (const [command, args] of commands) {
    try {
      execFileSync(command, args, {
        input: value,
        timeout: 2_000,
        stdio: ["pipe", "ignore", "ignore"],
      })
      return true
    } catch {
      // Try the next clipboard utility.
    }
  }
  return false
}

const HISTORY_LIMIT = 200

function projectKey(cwd: string): string {
  const resolved = resolve(cwd || process.cwd())
  return process.platform === "win32" ? resolved.toLowerCase() : resolved
}

export function sameProject(left: string, right: string): boolean {
  return projectKey(left) === projectKey(right)
}

function historyPath(home = process.env.SIWAH_HOME): string {
  return join(home ?? join(homedir(), ".siwah"), "prompt-history.json")
}

function readHistory(home?: string): Record<string, string[]> {
  try {
    const parsed = JSON.parse(readFileSync(historyPath(home), "utf8")) as unknown
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return {}
    const result: Record<string, string[]> = {}
    for (const [key, value] of Object.entries(parsed)) {
      if (Array.isArray(value)) {
        result[key] = value.filter((item): item is string => typeof item === "string")
      }
    }
    return result
  } catch {
    return {}
  }
}

/** Prompt history is scoped to the project directory and survives restarts. */
export function loadPromptHistory(cwd: string, home?: string): string[] {
  return (readHistory(home)[projectKey(cwd)] ?? []).slice(-HISTORY_LIMIT)
}

export function savePromptHistory(cwd: string, entries: string[], home?: string): void {
  try {
    const store = readHistory(home)
    const key = projectKey(cwd)
    const next = entries.filter((entry) => entry.trim().length > 0).slice(-HISTORY_LIMIT)
    if (next.length > 0) store[key] = next
    else delete store[key]
    const path = historyPath(home)
    mkdirSync(dirname(path), { recursive: true })
    writeFileSync(path, `${JSON.stringify(store, null, 2)}\n`, { encoding: "utf8", mode: 0o600 })
    chmodSync(path, 0o600)
  } catch {
    // History is a convenience; a read-only home must not break the TUI.
  }
}

const nonChatModel =
  /(embedding|whisper|tts|dall-e|moderation|audio|realtime|transcribe|image|search|free)/i

export function pickChatModel(
  provider: string,
  models: { id: string; supports_tools?: boolean | null }[],
): string | null {
  const chat = models.filter((model) => !nonChatModel.test(model.id))
  const model = chat.find((item) => item.supports_tools !== false) ?? chat[0] ?? models[0]
  return model ? `${provider}/${model.id}` : null
}

export function relativeTime(iso: string): string {
  const then = Date.parse(iso)
  if (Number.isNaN(then)) return ""
  const seconds = Math.max(0, Math.round((Date.now() - then) / 1000))
  if (seconds < 60) return "just now"
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`
  return `${Math.floor(seconds / 86400)}d ago`
}
