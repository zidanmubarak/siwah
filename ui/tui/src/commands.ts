/** Slash commands, fuzzy matching, and keybinding descriptions. */

export type Command = {
  name: string
  description: string
}

export const commands: Command[] = [
  { name: "/connect", description: "Connect a provider and store an API key" },
  { name: "/models", description: "Choose the active model" },
  {
    name: "/sessions",
    description: "Browse current-project sessions (Tab: all)",
  },
  { name: "/new", description: "Start a new session" },
  { name: "/turns", description: "Jump to the start of a previous chat" },
  { name: "/fork", description: "Fork the current session" },
  { name: "/undo", description: "Restore the previous checkpoint" },
  {
    name: "/rewind",
    description: "Restore a checkpoint (optional step count)",
  },
  { name: "/plan", description: "Switch to read-only plan mode" },
  { name: "/build", description: "Switch to build mode" },
  {
    name: "/effort",
    description: "Choose reasoning effort for this model",
  },
  {
    name: "/permissions",
    description: "Cycle ask, auto-edit, and yolo permission modes (AUTO badge in yolo)",
  },
  { name: "/reasoning", description: "Show or hide model reasoning" },
  { name: "/todos", description: "Show or hide the task plan" },
  {
    name: "/tools",
    description: "Expand or collapse the latest tool group's output",
  },
  { name: "/execute-plan", description: "Read .siwah/plan.md and execute it" },
  { name: "/compact", description: "Compact the session context" },
  { name: "/cost", description: "Show session token and allowance usage" },
  { name: "/skills", description: "List harness skills" },
  { name: "/memory", description: "Show layered memory" },
  { name: "/mcp", description: "List MCP servers and reconnect one" },
  { name: "/evolve", description: "Show the evolution timeline" },
  {
    name: "/update",
    description: "Check GitHub and install a newer siwah version",
  },
  {
    name: "/rollback",
    description: "Roll back the harness to a previous tag",
  },
  { name: "/pause", description: "Pause automatic evolution" },
  { name: "/resume", description: "Resume automatic evolution" },
  { name: "/theme", description: "Choose a theme" },
  {
    name: "/init",
    description: "Create or update AGENTS.md with the agent",
  },
  {
    name: "/review",
    description: "Review uncommitted changes, a commit, branch, or PR",
  },
  { name: "/doctor", description: "Show daemon and provider status" },
  { name: "/help", description: "Show keybindings and commands" },
  { name: "/quit", description: "Exit siwah" },
]

export function fuzzyMatch(query: string, value: string): number {
  const needle = query.toLowerCase().trim()
  if (!needle) return 1
  const haystack = value.toLowerCase()
  let score = 0
  let cursor = 0
  let streak = 0
  for (const char of needle) {
    const found = haystack.indexOf(char, cursor)
    if (found < 0) return -1
    score += 1
    if (found === cursor) {
      streak += 1
      score += streak * 2
    } else {
      streak = 0
    }
    if (found === 0 || haystack[found - 1] === "/" || haystack[found - 1] === "-") {
      score += 3
    }
    cursor = found + 1
  }
  return score + Math.max(0, 10 - haystack.length) / 10
}

export function filterCommands(query: string, extra: Command[] = []): Command[] {
  const clean = query.trim().replace(/^\/+/, "")
  const all = [...commands, ...extra]
  if (!clean) return all
  return all
    .map((command) => ({
      command,
      score: fuzzyMatch(clean, command.name.replace(/^\//, "")),
    }))
    .filter((item) => item.score >= 0)
    .sort((left, right) => right.score - left.score)
    .map((item) => item.command)
}

export function filterFiles(query: string, files: string[], limit = 12): string[] {
  return files
    .map((file) => ({ file, score: fuzzyMatch(query, file) }))
    .filter((item) => item.score >= 0)
    .sort((left, right) => right.score - left.score)
    .slice(0, limit)
    .map((item) => item.file)
}

export const helpText = `siwah TUI

Prompt
  Enter          send
  Shift+Enter    newline (Ctrl+N is the portable fallback; Alt+Enter and \\+Enter also work)
  Esc            cancel generation / close dialog
  Esc Esc        rewind to the previous checkpoint
  Tab            build <-> plan mode
  Ctrl+P         command palette
  Ctrl+R         prompt history
  Ctrl+C         copy selection; twice to exit
  ?              help (empty prompt)
  /effort        choose reasoning effort for the current model
  /init          agent reads the repo and updates AGENTS.md
  /review        review uncommitted changes, a commit, branch, or PR
  /permissions   cycle ask -> auto-edit -> yolo (AUTO badge in yolo)
  /reasoning     show or hide model reasoning
  /update        check GitHub and install a newer version

Prompt prefixes
  /              command palette
  @              fuzzy file/directory completion
  !              run a shell command directly
  #              append a quick memory note

Sessions
  /sessions      current project; Tab in the list toggles all projects
  /new           start a session in the current project
  /turns         jump to the start of a previous chat
  Ctrl+End       jump to the latest message
  PageUp/Down    scroll transcript without leaving the prompt
  Ctrl+O         expand/collapse the latest tool group's output (/tools)

Panels
  /sessions /models /cost /evolve /checkpoints
  /skills /memory /mcp /connect /doctor /theme

Evolution
  /pause /resume  stop or resume automatic evolution
  /rollback [tag] restore the harness to a previous tag`

export type KeyAction =
  | "palette"
  | "history"
  | "help"
  | "mode"
  | "cancel"
  | "exit"
  | "jumpLatest"
  | "pageUp"
  | "pageDown"
  | "toolDetails"

export const defaultKeybinds: Record<KeyAction, string> = {
  pageUp: "pageup",
  pageDown: "pagedown",
  toolDetails: "ctrl+o",
  palette: "ctrl+p",
  history: "ctrl+r",
  help: "?",
  mode: "tab",
  cancel: "escape",
  exit: "ctrl+c",
  jumpLatest: "ctrl+end",
}

export function bindingMatches(
  key: { name: string; ctrl?: boolean; shift?: boolean; meta?: boolean },
  binding: string,
): boolean {
  const parts = binding.toLowerCase().split("+")
  const name = parts.pop() ?? ""
  const ctrl = parts.includes("ctrl")
  const shift = parts.includes("shift")
  const alt = parts.includes("alt") || parts.includes("meta")
  return (
    key.name.toLowerCase() === name &&
    Boolean(key.ctrl) === ctrl &&
    Boolean(key.shift) === shift &&
    Boolean(key.meta) === alt
  )
}
