/**
 * TUI state and SSE reducer. Components read signals; all network mutations go
 * through this class so reconnect, permission, and usage behavior stay in one
 * place.
 */

import { createSignal } from "solid-js"

import {
  ApiError,
  type AuthStatus,
  type ChatMessage,
  type Checkpoint,
  type EventEnvelope,
  errorSuggestion,
  type McpServerStatus,
  type MemoryView,
  type MessageAttachment,
  type Meta,
  type ModelCatalog,
  type PermissionChoice,
  type PlanView,
  RuntimeMissingError,
  resolveRuntime,
  type Session,
  SiwahApi,
  type SkillContent,
  type SkillList,
  SUPPORTED_PROTOCOL,
  type UpdateStatus,
  type Usage,
} from "./api/client"
import { saveTheme } from "./config"
import { resolveTheme, type Theme, type ThemeName } from "./theme"
import {
  formatAllowance,
  formatTokens,
  lineCount,
  loadPromptHistory,
  pickChatModel,
  sameProject,
  savePromptHistory,
} from "./util"

export type ToolView = {
  id: string
  name: string
  summary: string
  status: "running" | "ok" | "error" | "cancelled" | "skipped"
  output: string
  error: string | null
  durationMs: number | null
  edited?: boolean
}

export type DiffBlock = {
  path: string
  patch: string
}

export type PromptImage = MessageAttachment & { id: string; token?: string }

export type UiMessage = {
  id: string
  role: "user" | "assistant" | "tool"
  content: string
  reasoning: string
  streaming: boolean
  createdAt: string
  toolCalls: ToolView[]
  diffs: DiffBlock[]
  reasoningOpen: boolean
  images?: PromptImage[]
  unapplied?: boolean
}

export type TodoItem = {
  id: string
  content: string
  status: string
}

export type PermissionState = {
  sessionId?: string
  requestId: string
  toolCallId?: string
  tool: string
  kind: string
  preview: string
  choices: string[]
  shellCommand?: string
}

export type Effort = "minimal" | "low" | "medium" | "high" | "xhigh" | "max" | "off"

export type DialogState =
  | { type: "permission-preview"; preview: string }
  | { type: "todos" }
  | null
  | { type: "palette" }
  | { type: "effort" }
  | { type: "theme" }
  | { type: "restart"; version?: string }
  | { type: "update" }
  | { type: "models" }
  | { type: "sessions" }
  | { type: "help" }
  | { type: "cost" }
  | { type: "timing" }
  | { type: "pending" }
  | { type: "find"; query: string }
  | { type: "connect" }
  | { type: "doctor" }
  | { type: "evolution" }
  | { type: "checkpoints" }
  | { type: "changes"; scope: "turn" | "workspace"; checkpointId?: string }
  | { type: "skills" }
  | { type: "skill"; name: string }
  | { type: "memory" }
  | { type: "rewind"; checkpoint?: Checkpoint; steps?: number; label?: "undo" | "rewind" }
  | { type: "turns" }
  | { type: "history" }
  | { type: "mcp" }

export type ToastTone = "info" | "success" | "warning" | "error"

export type Toast = {
  id: number
  text: string
  tone: ToastTone
}

/** A terminal run warning that stays in the transcript after toasts expire. */
export type RunNotice = {
  id: number
  tone: "warning" | "error"
  title: string
  text: string
}

/** A provider retry that is currently waiting before the next attempt. */
export type ProviderRetry = {
  attempt: number
  max: number
  delayMs: number
  model: string
  code: string
}

/** A prompt accepted while another run is active, waiting in the server FIFO. */
export type QueuedPrompt = {
  id: string
  text: string
  attachmentCount?: number
  blockedReason?: string
}

type Connection = "offline" | "connecting" | "online" | "reconnecting"
type ContextState = {
  window: number | null
  tokens: number | null
  percent: number | null
  estimated?: boolean
}

const sleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms))
const NO_ARG = Symbol("no-arg")
const HISTORY_LIMIT = 200

type CallableSignal<T> = {
  (): T
  (value: T | ((previous: T) => T)): T
}

function callableSignal<T>(initial: T): CallableSignal<T> {
  const [get, set] = createSignal(initial)
  const apply = set as unknown as (value: T | ((previous: T) => T)) => T
  return ((value: T | ((previous: T) => T) | typeof NO_ARG = NO_ARG) => {
    if (value === NO_ARG) return get()
    return apply(value)
  }) as CallableSignal<T>
}

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" ? (value as Record<string, unknown>) : {}
}

function text(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback
}

function number(value: unknown, fallback = 0): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback
}

function runEndTitle(reason: string): string {
  switch (reason) {
    case "error":
      return "Run failed"
    case "stalled":
      return "Run stalled"
    case "max_steps":
      return "Step limit reached"
    case "allowance_exceeded":
      return "Allowance limit reached"
    default:
      return "Run ended early"
  }
}

function runEndFallback(reason: string): string {
  switch (reason) {
    case "stalled":
      return "No tool call succeeded for several steps, so the run stopped before finishing."
    case "max_steps":
      return "The step limit was reached before the run finished."
    case "allowance_exceeded":
      return "The provider allowance limit was reached before the run finished."
    default:
      return `The run ended without a final answer (${reason}).`
  }
}

export class TuiStore {
  readonly session = callableSignal<Session | null>(null)
  readonly messages = callableSignal<UiMessage[]>([])
  readonly sessions = callableSignal<Session[]>([])
  readonly running = callableSignal(false)
  readonly connection = callableSignal<Connection>("offline")
  readonly usage = callableSignal<Usage | null>(null)
  readonly contextState = callableSignal<ContextState>({
    window: null,
    tokens: null,
    percent: null,
  })
  readonly todos = callableSignal<TodoItem[]>([])
  readonly permission = callableSignal<PermissionState | null>(null)
  readonly dialog = callableSignal<DialogState>(null)
  readonly toasts = callableSignal<Toast[]>([])
  readonly runNotice = callableSignal<RunNotice | null>(null)
  readonly providerRetry = callableSignal<ProviderRetry | null>(null)
  readonly activeRunId = callableSignal<string | null>(null)
  readonly queuedPrompts = callableSignal<QueuedPrompt[]>([])
  private queueEventSeq = -1
  readonly models = callableSignal<ModelCatalog | null>(null)
  readonly meta = callableSignal<Meta | null>(null)
  readonly updateStatus = callableSignal<UpdateStatus | null>(null)
  readonly auth = callableSignal<AuthStatus | null>(null)
  readonly evolution = callableSignal<{
    tag?: string | null
    status?: string
    paused?: boolean
    events?: unknown[]
  } | null>(null)
  readonly customCommands = callableSignal<
    Array<{ name: string; description: string; body: string }>
  >([])
  readonly expandedTools = callableSignal<string[]>([])
  readonly focusMessage = callableSignal<string | null>(null)
  readonly scrollTick = callableSignal(0)
  readonly theme = callableSignal<Theme>(resolveTheme(process.env.SIWAH_THEME))
  readonly runtimeError = callableSignal<string | null>(null)
  readonly files = callableSignal<string[]>([])
  readonly promptText = callableSignal("")
  readonly draftTarget = callableSignal({ id: "", carry: false })
  readonly runPhase = callableSignal<{ label: string; since: number } | null>(null)
  private readonly drafts = new Map<string, string>()

  private selectDraft(id: string, carry = false): void {
    const previous = this.draftTarget().id
    if (previous === id) return
    if (!carry) {
      this.drafts.set(previous, this.promptText())
      this.promptText(this.drafts.get(id) ?? "")
    } else {
      this.drafts.delete(previous)
    }
    this.draftTarget({ id, carry })
  }

  private setRunPhase(label: string | null): void {
    if (this.runPhase()?.label === label) return
    this.runPhase(label ? { label, since: Date.now() } : null)
  }
  readonly promptHeight = callableSignal(1)
  readonly history = callableSignal<string[]>([])
  readonly checkpoints = callableSignal<Checkpoint[]>([])
  readonly plan = callableSignal<PlanView | null>(null)
  readonly pendingMode = callableSignal<"build" | "plan">("build")
  readonly pendingModel = callableSignal<string | null>(null)
  readonly permissionMode = callableSignal<"ask" | "auto-edit" | "yolo">("ask")
  readonly pendingEffort = callableSignal<Effort | null>(null)
  readonly showReasoning = callableSignal(true)
  readonly hiddenSessions = callableSignal<string[]>([])
  readonly todosHidden = callableSignal(false)
  readonly mcpServers = callableSignal<McpServerStatus[]>([])
  private readonly promptHistory: string[] = []
  private historyIndex = 0
  private api: SiwahApi | null = null
  private streamAbort: AbortController | null = null
  private lastSeq = 0
  private toastId = 0
  private runNoticeId = 0
  private pendingRunError: { code: string; message: string } | null = null
  private filesSession: string | null = null
  private filesLoadedAt = 0
  private sessionEpoch = 0
  private sessionLoading = false
  private submitting = false
  private pendingAdmission: {
    sessionId: string
    response: ReturnType<SiwahApi["sendMessage"]>
  } | null = null
  private readonly abortingSessions = new Set<string>()
  private readonly requestVersions = new Map<string, number>()
  private readonly seenEventIds = new Set<string>()
  private readonly decidingPermissions = new WeakSet<PermissionState>()
  private updateNotified = ""
  private streamBuffers = new Map<string, { content: string; reasoning: string }>()
  private streamTimer: ReturnType<typeof setTimeout> | null = null

  activeMessage = () =>
    [...this.messages()].reverse().find((message) => message.role === "assistant") ?? null
  currentModel = () =>
    this.session()?.model ?? this.pendingModel() ?? this.models()?.roles?.main ?? "not configured"
  usingChatGPTPlan = () => {
    const provider = this.currentModel().split("/", 1)[0]
    return (
      this.auth()?.providers?.some(
        (item) => item.provider === provider && item.browser_method === "chatgpt" && item.file_set,
      ) ?? false
    )
  }
  currentModelInfo = () => {
    const model = this.currentModel()
    for (const provider of this.models()?.providers ?? []) {
      for (const item of provider.models ?? []) {
        if (`${provider.provider}/${item.id}` === model) return item
      }
    }
    return null
  }
  projectSessions = () => {
    const cwd = this.workspacePath()
    return this.sessions().filter((session) => sameProject(session.cwd, cwd))
  }
  visibleProjectSessions = () => {
    const hidden = new Set(this.hiddenSessions())
    const active = this.session()
    const sessions = this.projectSessions().filter((session) => !hidden.has(session.id))
    if (active && !hidden.has(active.id) && !sessions.some((item) => item.id === active.id)) {
      return [active, ...sessions]
    }
    return sessions
  }
  sessionRunning = (session: Session) =>
    session.id === this.session()?.id ? this.running() : session.status === "running"
  otherSessions = () => {
    const cwd = this.workspacePath()
    return this.sessions().filter((session) => !sameProject(session.cwd, cwd))
  }
  private contextReading() {
    const context = this.contextState()
    const usage = this.usage()
    const info = this.currentModelInfo()
    const live = context.estimated !== undefined
    const window = live
      ? context.window
      : (context.window ?? info?.context_window ?? usage?.context_window ?? null)
    const tokens = live ? context.tokens : (context.tokens ?? usage?.context_tokens ?? null)
    const percent =
      window === null
        ? null
        : (context.percent ??
          (live ? null : usage?.context_percent) ??
          (tokens !== null ? Math.min(100, (tokens / window) * 100) : null))
    return { tokens, window, percent, estimated: context.estimated }
  }
  contextSummary = (): string => {
    const { tokens, window, percent, estimated } = this.contextReading()
    const marker = estimated && tokens !== null ? "~" : ""
    if (!window) return tokens === null ? "ctx —" : `ctx ${marker}${formatTokens(tokens)}`
    const size =
      tokens !== null
        ? `${marker}${formatTokens(tokens)}/${formatTokens(window)}`
        : formatTokens(window)
    const share = percent !== null ? ` ${Math.round(percent)}%` : ""
    const compact = percent !== null && percent >= 95 ? " · compact" : ""
    return `ctx ${size}${share}${compact}`
  }
  contextTone = (): string => {
    const percent = this.contextReading().percent ?? 0
    if (percent >= 95) return this.theme().error
    if (percent >= 80) return this.theme().warning
    return this.theme().dim
  }
  usageSummary = (): string => {
    const usage = this.usage()
    const total = (usage?.input_tokens ?? 0) + (usage?.output_tokens ?? 0)
    const hasCost = (usage?.cost_steps ?? 0) > 0 || (usage?.cost ?? 0) > 0
    const hasAllowance = (usage?.allowance_steps ?? 0) > 0 || (usage?.allowance ?? 0) > 0
    const allowance = usage?.allowance ?? 0
    const cost = usage?.cost ?? 0
    const billing =
      hasAllowance && (allowance !== cost || !hasCost)
        ? `${formatAllowance(allowance)} allowance`
        : hasCost
          ? formatAllowance(cost)
          : "cost —"
    return `${formatTokens(total)} tok · ${billing}`
  }
  currentMode = () => (this.session()?.mode as "build" | "plan" | undefined) ?? this.pendingMode()
  effort = (): string | null => {
    const value = this.session()?.metadata?.effort
    if (typeof value === "string" && value.length > 0) return value === "off" ? null : value
    const pending = this.pendingEffort()
    return pending === "off" ? null : pending
  }
  effortOptions = (): Effort[] => {
    const info = this.currentModelInfo()
    const supported = (info?.efforts ?? []).filter((value) => value !== "none")
    if (supported.length > 0) return ["off", ...supported] as Effort[]
    return ["off"]
  }
  workspacePath = () => this.session()?.cwd ?? process.env.SIWAH_WORKSPACE ?? process.cwd()

  /** Async work may only publish into the session generation that started it. */
  private scope(): () => boolean {
    const epoch = this.sessionEpoch
    const sessionId = this.session()?.id
    return () => epoch === this.sessionEpoch && sessionId === this.session()?.id
  }

  private latestRequest(key: string): () => boolean {
    const current = this.scope()
    const version = (this.requestVersions.get(key) ?? 0) + 1
    this.requestVersions.set(key, version)
    return () => current() && this.requestVersions.get(key) === version
  }

  private beginSessionChange(): number {
    this.sessionEpoch += 1
    this.sessionLoading = true
    this.streamAbort?.abort()
    this.streamAbort = null
    if (this.streamTimer !== null) clearTimeout(this.streamTimer)
    this.streamTimer = null
    this.streamBuffers.clear()
    this.lastSeq = 0
    this.seenEventIds.clear()
    this.session(null)
    this.running(false)
    this.setRunPhase(null)
    this.permission(null)
    this.dialog(null)
    this.messages([])
    this.todos([])
    this.usage(null)
    this.contextState({ window: null, tokens: null, percent: null })
    this.checkpoints([])
    this.plan(null)
    this.expandedTools([])
    this.focusMessage(null)
    this.runtimeError(null)
    this.runNotice(null)
    this.providerRetry(null)
    this.pendingRunError = null
    this.queuedPrompts([])
    this.queueEventSeq = -1
    this.activeRunId(null)
    this.promptHistory.length = 0
    this.historyIndex = 0
    this.history([])
    this.files([])
    this.filesSession = null
    this.filesLoadedAt = 0
    this.customCommands([])
    this.pendingMode("build")
    this.pendingModel(null)
    return this.sessionEpoch
  }

  isToolExpanded(id: string): boolean {
    return this.expandedTools().includes(id)
  }

  toggleTool(id: string): void {
    this.expandedTools((current) =>
      current.includes(id) ? current.filter((item) => item !== id) : [...current, id],
    )
  }

  loadHistory(): void {
    const entries = loadPromptHistory(this.workspacePath())
    this.promptHistory.splice(0, this.promptHistory.length, ...entries)
    this.historyIndex = this.promptHistory.length
    this.history([...this.promptHistory])
  }

  private mergeHistory(prompts: string[]): void {
    const merged = [...this.promptHistory]
    for (const prompt of prompts) {
      if (prompt.trim().length === 0 || merged.includes(prompt)) continue
      merged.push(prompt)
    }
    this.promptHistory.splice(0, this.promptHistory.length, ...merged.slice(-HISTORY_LIMIT))
    this.historyIndex = this.promptHistory.length
    this.history([...this.promptHistory])
    savePromptHistory(this.workspacePath(), this.promptHistory)
  }

  async hydrateHistoryFromProject(): Promise<void> {
    if (!this.api || this.history().length > 0) return
    const latest = [...this.projectSessions()].sort((left, right) =>
      right.updated_at.localeCompare(left.updated_at),
    )[0]
    if (!latest) return
    const current = this.scope()
    try {
      const messages = await this.api.messages(latest.id)
      if (!current()) return
      this.mergeHistory(
        messages.messages
          .filter((message) => message.role === "user" && message.delivery !== "unapplied")
          .map((message) => message.content ?? ""),
      )
    } catch {
      // Prompt history is best-effort.
    }
  }

  async initialize(): Promise<void> {
    try {
      const runtime = await resolveRuntime()
      this.api = new SiwahApi(runtime)
      const [meta] = await Promise.all([this.api.meta(), this.api.health()])
      if (meta.protocol !== SUPPORTED_PROTOCOL) {
        throw new Error(
          `Unsupported daemon protocol ${meta.protocol}; this TUI requires ${SUPPORTED_PROTOCOL}. Restart matching daemon and TUI versions.`,
        )
      }
      this.meta(meta)
      this.pendingEffort((meta.default_effort as Effort | null | undefined) ?? null)
      this.connection("online")
      // A launch is the moment the user expects to hear about a new release;
      // do not let a stale on-disk cache hide a version published today.
      void this.checkForUpdate(true)
      await Promise.all([
        this.reloadSessions(),
        this.reloadModels(),
        this.reloadAuth(),
        this.reloadEvolution(),
        this.reloadCommands(),
        this.reloadPermissionMode(),
      ])
      void this.loadFiles()
      const requested = process.env.SIWAH_PERMISSION_MODE
      if (requested === "ask" || requested === "auto-edit" || requested === "yolo") {
        try {
          const result = await this.api.setPermissionMode(requested)
          this.permissionMode(result.mode as "ask" | "auto-edit" | "yolo")
        } catch {
          // Keep the daemon's current mode if the older daemon rejects the request.
        }
      }
      this.loadHistory()
      void this.hydrateHistoryFromProject()
      const sessionId = process.env.SIWAH_SESSION
      if (sessionId) {
        await this.openSession(sessionId)
      } else {
        const providers = this.auth()?.providers ?? []
        const ready = providers.some(
          (provider) => !provider.env || provider.env_set || provider.file_set,
        )
        const needsKey = providers.some(
          (provider) => Boolean(provider.env) && !provider.env_set && !provider.file_set,
        )
        if (this.sessions().length === 0 && !ready && needsKey) {
          this.dialog({ type: "connect" })
        }
      }
    } catch (error) {
      this.api = null
      this.runtimeError(errorSuggestion(error))
      this.connection("offline")
    }
  }

  async reloadSessions(options: { quiet?: boolean } = {}): Promise<void> {
    if (!this.api) return
    try {
      this.sessions((await this.api.sessions()).sessions)
    } catch (error) {
      if (!options.quiet) this.notify(errorSuggestion(error), "error")
    }
  }

  async reloadPermissionMode(): Promise<void> {
    if (!this.api) return
    try {
      const result = await this.api.permissionMode()
      const mode = result.mode
      if (mode === "ask" || mode === "auto-edit" || mode === "yolo") this.permissionMode(mode)
    } catch {
      // Older daemon: keep the local default.
    }
  }

  toggleReasoningVisibility(): void {
    const next = !this.showReasoning()
    this.showReasoning(next)
    this.notify(next ? "Reasoning shown" : "Reasoning hidden", "info")
  }

  async setPermissionMode(mode: "ask" | "auto-edit" | "yolo"): Promise<void> {
    if (!this.api) {
      this.permissionMode(mode)
      return
    }
    try {
      const result = await this.api.setPermissionMode(mode)
      this.permissionMode(result.mode as "ask" | "auto-edit" | "yolo")
      this.notify(
        mode === "yolo" ? "Auto bypass enabled: tools run without prompts" : `Permissions: ${mode}`,
        mode === "yolo" ? "warning" : "info",
      )
    } catch (error) {
      this.notify(errorSuggestion(error), "error")
    }
  }

  async setEffort(effort: Effort): Promise<void> {
    const active = this.session()
    const current = this.latestRequest("effort")
    this.pendingEffort(effort)
    if (!this.api) {
      this.notify(
        effort === "off" ? "Effort: provider default" : `Effort: ${effort} (next session)`,
        "info",
      )
      return
    }
    try {
      await this.api.setDefaultEffort(effort)
      if (active) {
        const updated = await this.api.updateSession(active.id, { effort })
        if (!current()) return
        this.session((session) =>
          session
            ? {
                ...session,
                metadata: { ...session.metadata, effort: updated.metadata?.effort ?? effort },
              }
            : session,
        )
      }
      if (!current()) return
      this.notify(effort === "off" ? "Effort: provider default" : `Effort: ${effort}`, "info")
    } catch (error) {
      if (!current()) return
      this.notify(errorSuggestion(error), "error")
    }
  }

  async reloadModels(): Promise<void> {
    if (!this.api) return
    try {
      const catalog = await this.api.models()
      this.models(catalog)
      if (!this.session() && !this.pendingModel() && !catalog.roles?.main) {
        for (const provider of catalog.providers ?? []) {
          const first = pickChatModel(provider.provider, provider.models ?? [])
          if (first) {
            this.pendingModel(first)
            break
          }
        }
      }
    } catch (error) {
      this.notify(errorSuggestion(error), "warning")
    }
  }

  async ensureProviders(): Promise<void> {
    if (!this.api) throw new Error("daemon unavailable")
    await this.api.ensureProviders()
    await this.reloadAuth()
    await this.reloadModels()
  }

  async login(provider: string, apiKey: string): Promise<void> {
    if (!this.api) throw new Error("daemon unavailable")
    await this.api.authLogin(provider, apiKey)
    await this.refreshConnectedProvider(provider)
  }

  async startBrowserLogin(provider: string, clientId?: string) {
    if (!this.api) throw new Error("daemon unavailable")
    return this.api.startBrowserLogin(provider, clientId)
  }

  async finishBrowserLogin(provider: string, flowId: string, code?: string): Promise<void> {
    if (!this.api) throw new Error("daemon unavailable")
    await this.api.finishBrowserLogin(provider, flowId, code)
    await this.refreshConnectedProvider(provider)
  }

  async chatGPTAccounts() {
    if (!this.api) throw new Error("daemon unavailable")
    const response = await this.api.chatGPTAccounts()
    return response.accounts
  }

  async chatGPTSignOut(clientId: string) {
    if (!this.api) throw new Error("daemon unavailable")
    const response = await this.api.chatGPTSignOut(clientId)
    await this.reloadAuth()
    await this.reloadModels()
    return response.revoked
  }

  private async refreshConnectedProvider(provider: string): Promise<void> {
    await this.reloadAuth()
    await this.reloadModels()
    const catalog = this.models()?.providers?.find((item) => item.provider === provider)
    if (catalog?.error) {
      this.notify(`Connected ${provider}; catalog unavailable: ${catalog.error}`, "warning")
      return
    }
    if (!this.session()) {
      const first = pickChatModel(provider, catalog?.models ?? [])
      if (first) this.pendingModel(first)
    }
  }

  async reloadAuth(): Promise<void> {
    if (!this.api) return
    try {
      this.auth(await this.api.authStatus())
    } catch {
      // Auth status is optional; the welcome screen still works.
    }
  }

  async reloadEvolution(): Promise<void> {
    if (!this.api) return
    try {
      this.evolution(
        (await this.api.evolution()) as {
          tag?: string | null
          status?: string
          paused?: boolean
          events?: unknown[]
        },
      )
    } catch {
      // Evolution may be unavailable; absence is normal.
    }
  }

  async checkForUpdate(refresh = false): Promise<void> {
    if (!this.api) return
    try {
      const status = await this.api.updateStatus(refresh)
      this.updateStatus(status)
      if (status.available && status.latest && this.updateNotified !== status.latest) {
        this.updateNotified = status.latest
        this.notify(`siwah ${status.latest} is available; run /update`, "info")
      }
    } catch {
      // Update checks are best effort: offline or unauthenticated remotes stay quiet.
    }
  }

  async applyUpdate(): Promise<void> {
    if (!this.api) {
      this.notify("Daemon unavailable", "error")
      return
    }
    this.openDialog({ type: "update" })
    try {
      const result = await this.api.applyUpdate()
      this.closeDialog()
      if (result.ok) {
        const version = result.to_version ?? this.meta()?.version ?? "latest"
        const status = this.updateStatus()
        if (status) {
          this.updateStatus({
            ...status,
            current: version,
            latest: version,
            available: false,
            can_apply: false,
            commits_behind: 0,
          })
        }
        if (result.restart_required) {
          this.openDialog({ type: "restart", version })
        } else {
          this.notify(result.detail || `siwah ${version} is up to date`, "success")
        }
      } else {
        this.notify(result.detail || "Update failed", "error")
      }
    } catch (error) {
      this.closeDialog()
      this.notify(errorSuggestion(error), "error")
    }
  }

  async rollbackEvolution(tag?: string | null): Promise<void> {
    if (!this.api) return
    try {
      await this.api.evolutionRollback(tag)
      await this.reloadEvolution()
      this.notify("Evolution rolled back", "success")
    } catch (error) {
      this.notify(errorSuggestion(error), "error")
    }
  }

  async pauseEvolution(paused = true): Promise<void> {
    if (!this.api) return
    try {
      await this.api.evolutionPause(paused)
      await this.reloadEvolution()
      this.notify(paused ? "Evolution paused" : "Evolution resumed", "success")
    } catch (error) {
      this.notify(errorSuggestion(error), "error")
    }
  }

  async reloadMcp(refresh = true): Promise<void> {
    if (!this.api) return
    try {
      const result = await this.api.mcpStatus(refresh)
      this.mcpServers(result.servers ?? [])
    } catch (error) {
      this.notify(errorSuggestion(error), "error")
    }
  }

  async restartMcp(name: string): Promise<void> {
    if (!this.api) return
    this.notify(`Reconnecting MCP server ${name}…`, "info")
    try {
      const server = await this.api.restartMcp(name)
      await this.reloadMcp(false)
      if (server.status === "connected") {
        this.notify(
          `${name}: connected${server.tools?.length ? ` · ${server.tools.length} tools` : ""}`,
          "success",
        )
      } else {
        this.notify(
          `${name}: ${server.status}${server.error ? ` · ${server.error}` : ""}`,
          "warning",
        )
      }
    } catch (error) {
      this.notify(errorSuggestion(error), "error")
    }
  }

  async reloadCommands(): Promise<void> {
    if (!this.api) return
    const current = this.latestRequest("commands")
    try {
      const response = await this.api.commands(this.workspacePath())
      if (!current()) return
      this.customCommands(response.commands ?? [])
    } catch {
      if (current()) this.customCommands([])
    }
  }

  async runCustomCommand(name: string, argumentsText: string): Promise<boolean> {
    const current = this.scope()
    const target = name.trim().replace(/^\//, "").toLowerCase()
    const find = () => this.customCommands().find((item) => item.name.toLowerCase() === target)
    let command = find()
    if (!command) {
      // The agent may have written commands/<name>.md during this session.
      // Reload once before rejecting the name.
      await this.reloadCommands()
      if (!current()) return false
      command = find()
    }
    if (!command) {
      this.notify(`Unknown command: /${target}`, "warning")
      return false
    }
    const body = command.body.includes("$ARGUMENTS")
      ? command.body.split("$ARGUMENTS").join(argumentsText)
      : argumentsText
        ? `${command.body}\n\n${argumentsText}`
        : command.body
    return this.sendPrompt(body)
  }

  async reloadCheckpoints(): Promise<void> {
    const active = this.session()
    const current = this.latestRequest("checkpoints")
    if (!this.api || !active) {
      this.checkpoints([])
      return
    }
    try {
      const response = await this.api.checkpoints(active.id)
      if (current()) this.checkpoints(response.checkpoints ?? [])
    } catch {
      if (current()) this.checkpoints([])
    }
  }

  async loadPlan(): Promise<void> {
    const active = this.session()
    const current = this.latestRequest("plan")
    if (!this.api || !active) {
      this.plan(null)
      return
    }
    try {
      const response = await this.api.plan(active.id)
      if (current()) this.plan(response)
    } catch {
      if (current()) this.plan(null)
    }
  }

  async createCheckpoint(label?: string): Promise<void> {
    const active = this.session()
    const current = this.scope()
    if (!this.api || !active) {
      this.notify("Start a session first", "warning")
      return
    }
    try {
      const checkpoint = await this.api.createCheckpoint(active.id, label)
      if (!current()) return
      await this.reloadCheckpoints()
      if (!current()) return
      this.notify(`Checkpoint saved: ${checkpoint.label ?? checkpoint.id}`, "success")
    } catch (error) {
      if (!current()) return
      this.notify(errorSuggestion(error), "error")
    }
  }

  async rewind(checkpointId?: string, steps = 1): Promise<void> {
    const active = this.session()
    const current = this.scope()
    if (!this.api || !active) {
      this.notify("Start a session first", "warning")
      return
    }
    try {
      const result = await this.api.rewind(active.id, {
        checkpoint_id: checkpointId,
        steps,
        confirm: true,
      })
      if (!current()) return
      await this.openSession(active.id)
      if (this.session()?.id === active.id)
        this.notify(`Rewound to ${result.checkpoint.label ?? result.checkpoint.id}`, "success")
    } catch (error) {
      if (current()) this.notify(errorSuggestion(error), "error")
    }
  }

  async compact(): Promise<void> {
    const active = this.session()
    const current = this.scope()
    if (!this.api || !active) {
      this.notify("Start a session first", "warning")
      return
    }
    try {
      const result = await this.api.compact(active.id)
      if (!current()) return
      await this.refreshUsage()
      if (!current()) return
      const saved = result.before_tokens - result.after_tokens
      const detail = saved > 0 ? `saved ${saved} tokens` : "already compact"
      this.notify(
        `Context compacted: ${result.before_tokens} -> ${result.after_tokens} tokens ` +
          `(${detail}); transcript unchanged`,
        "success",
      )
    } catch (error) {
      if (!current()) return
      this.notify(errorSuggestion(error), "error")
    }
  }

  async executePlan(): Promise<void> {
    const active = this.session()
    const current = this.scope()
    if (!this.api || !active) {
      this.notify("Start a session first", "warning")
      return
    }
    await this.loadPlan()
    if (!current()) return
    if (!this.plan()?.exists) {
      this.notify("No plan.md yet; run /plan first", "warning")
      return
    }
    await this.setMode("build")
    if (!current() || this.currentMode() !== "build") return
    await this.sendPrompt(
      "Read .siwah/plan.md and execute the plan step by step. Keep todos updated, " +
        "run the verifier after edits, and report any deviation before changing the plan.",
    )
  }

  async createSession(
    cwd = process.env.SIWAH_WORKSPACE ?? process.cwd(),
    model?: string,
    carryDraft = false,
  ): Promise<Session | null> {
    if (!this.api) return null
    const selectedModel = model ?? this.pendingModel() ?? this.session()?.model ?? undefined
    const mode = this.pendingMode()
    const effort = this.pendingEffort()
    const epoch = this.beginSessionChange()
    try {
      const session = await this.api.createSession({
        cwd,
        model: selectedModel,
        mode,
      })
      if (epoch !== this.sessionEpoch) return null
      let active = session
      if (effort) {
        try {
          active = await this.api.updateSession(session.id, { effort })
        } catch (error) {
          if (epoch !== this.sessionEpoch) return null
          this.notify(errorSuggestion(error), "warning")
        }
      }
      if (epoch !== this.sessionEpoch) return null
      this.selectDraft(active.id, carryDraft)
      this.session(active)
      this.sessionLoading = false
      this.running(active.status === "running")
      this.pendingMode((active.mode as "build" | "plan") ?? "build")
      this.pendingModel(null)
      this.loadHistory()
      this.lastSeq = active.last_event_seq ?? 0
      this.startStream(session.id)
      void this.loadFiles()
      void this.reloadCommands()
      void this.refreshUsage()
      void this.reloadSessions()
      return active
    } catch (error) {
      if (epoch !== this.sessionEpoch) return null
      this.sessionLoading = false
      this.notify(errorSuggestion(error), "error")
      return null
    }
  }

  async switchSession(sessionId: string): Promise<void> {
    if (sessionId === this.session()?.id) return
    await this.openSession(sessionId)
  }

  async closeSessionTab(sessionId: string): Promise<void> {
    const active = this.session()
    const target =
      this.sessions().find((session) => session.id === sessionId) ??
      (active?.id === sessionId ? active : null)
    const running = sessionId === active?.id ? this.running() : target?.status === "running"
    if (running) {
      this.notify("Stop the run before closing this tab", "warning")
      return
    }
    this.hiddenSessions((current) =>
      current.includes(sessionId) ? current : [...current, sessionId],
    )
    if (active?.id !== sessionId) return
    const next = this.visibleProjectSessions().find((session) => session.id !== sessionId)
    if (next) {
      await this.openSession(next.id)
      return
    }
    this.beginSessionChange()
    this.selectDraft("")
    this.sessionLoading = false
    this.loadHistory()
    void this.loadFiles()
    void this.reloadCommands()
  }

  toggleTodos(): void {
    this.todosHidden((value) => !value)
  }

  async openSession(sessionId: string, options: { after?: number } = {}): Promise<void> {
    if (!this.api) return
    const sameSession = this.session()?.id === sessionId
    const epoch = this.beginSessionChange()
    try {
      const [session, messageList, usage] = await Promise.all([
        this.api.session(sessionId),
        this.api.messages(sessionId),
        this.api.usage(sessionId),
      ])
      if (epoch !== this.sessionEpoch) return
      const active = session
      this.hiddenSessions((current) => current.filter((item) => item !== sessionId))
      this.selectDraft(active.id, sameSession)
      this.session(active)
      this.sessionLoading = false
      this.running(active.status === "running")
      this.pendingMode((active.mode as "build" | "plan") ?? "build")
      this.pendingModel(null)
      this.activeRunId(
        active.status === "running" ? text(active.metadata?.active_run_id) || null : null,
      )
      const phase = active.metadata?.run_phase
      if (
        active.status === "running" &&
        phase &&
        typeof phase === "object" &&
        !Array.isArray(phase)
      ) {
        const label = "label" in phase ? text(phase.label) : ""
        const since = "since" in phase ? Date.parse(text(phase.since)) : NaN
        if (label && Number.isFinite(since)) this.runPhase({ label, since })
      }
      this.messages(hydrateMessages(messageList.messages, active.status === "running"))
      if (active.prompt_queue) {
        this.applyQueue(active.prompt_queue.event_seq, active.prompt_queue.items)
      }
      this.scrollTick((value) => value + 1)
      this.usage(usage)
      this.contextState({
        window: usage.context_window ?? null,
        tokens: usage.context_tokens ?? null,
        percent: usage.context_percent ?? null,
      })
      this.todos(todosFromSession(session))
      this.checkpoints([])
      this.plan(null)
      this.expandedTools([])
      this.loadHistory()
      this.mergeHistory(
        messageList.messages
          .filter((message) => message.role === "user" && message.delivery !== "unapplied")
          .map((message) => message.content ?? ""),
      )
      this.lastSeq = options.after ?? session.last_event_seq ?? 0
      this.startStream(session.id)
      void this.loadFiles()
      void this.reloadCommands()
      await Promise.all([this.reloadCheckpoints(), this.loadPlan()])
    } catch (error) {
      if (epoch !== this.sessionEpoch) return
      this.sessionLoading = false
      this.notify(errorSuggestion(error), "error")
    }
  }

  async sendPrompt(
    input: string,
    options: { display?: string; images?: PromptImage[] } = {},
  ): Promise<boolean> {
    const prompt = input.trim()
    const images = options.images ?? []
    if ((!prompt && images.length === 0) || !this.api) return false
    if (this.submitting || this.sessionLoading) {
      this.notify("A prompt is already being submitted; wait a moment.", "warning")
      return false
    }
    const queued = this.running()
    // Claim admission before any await (creation and cosmetic title updates included).
    this.submitting = true
    try {
      if (!this.session()) {
        const created = await this.createSession(undefined, undefined, true)
        if (!created) return false
      }
      const session = this.session()
      if (!session) return false
      const current = this.scope()
      if (!session.title) {
        const title = prompt.split(/\r?\n/)[0]?.trim().slice(0, 80)
        if (title) {
          this.session({ ...session, title })
          this.setSessionTitle(session.id, title)
          try {
            const updated = await this.api.updateSession(session.id, { title })
            if (!current()) return false
            this.session((active) => (active ? { ...active, title: updated.title } : active))
          } catch {
            // A title is cosmetic; never block the prompt on it.
          }
        }
      }
      if (!current()) return false
      const localId = `local_${crypto.randomUUID()}`
      this.messages((messages) => [
        ...messages,
        {
          id: localId,
          role: "user",
          content: options.display ?? (prompt || "Image message"),
          reasoning: "",
          streaming: false,
          createdAt: new Date().toISOString(),
          toolCalls: [],
          diffs: [],
          reasoningOpen: false,
          images,
        },
      ])
      if (!queued) {
        this.running(true)
        this.setSessionStatus(session.id, "running")
        this.runNotice(null)
        this.providerRetry(null)
        this.pendingRunError = null
        this.connection((connection) => (connection === "online" ? "online" : "connecting"))
        this.startStream(session.id)
      }
      try {
        const admission = this.api.sendMessage(
          session.id,
          prompt,
          images.map(({ mime, data, name }) => ({ mime, data, name })),
        )
        if (!queued) this.pendingAdmission = { sessionId: session.id, response: admission }
        const response = await admission
        if (!current()) return false
        if (response.status === "queued" && this.queueEventSeq >= 0) {
          this.messages((current) => current.filter((message) => message.id !== localId))
        } else if (response.status === "queued") {
          // The server owns the FIFO queue. Keep the local echo and show a
          // compact count in the activity line until the next run starts.
          this.queuedPrompts((current) => [
            ...current,
            { id: localId, text: options.display ?? (prompt || "Image message") },
          ])
        } else if (response.status === "running" && this.running() && !this.activeRunId()) {
          this.activeRunId(response.run_id)
        }
        if (prompt) this.pushHistory(options.display ?? prompt)
        return true
      } catch (error) {
        if (current()) {
          this.messages((messages) => messages.filter((message) => message.id !== localId))
          const conflict = error instanceof ApiError && error.code === "run_conflict"
          this.running(queued || conflict)
          this.setSessionStatus(session.id, queued || conflict ? "running" : "idle")
          this.notify(errorSuggestion(error), "error")
        }
        return false
      }
    } finally {
      if (!queued) this.pendingAdmission = null
      this.submitting = false
    }
  }

  async steerPrompt(input: string): Promise<boolean> {
    const session = this.session()
    const runId = this.activeRunId()
    const content = input.trim()
    if (!session || !this.api || !this.running() || !runId || !content) {
      this.notify("Use /steer followed by a correction during an active task.", "warning")
      return false
    }
    const current = this.scope()
    try {
      await this.api.steer(session.id, runId, content)
      if (!current()) return false
      this.notify("Correction sent to the active task", "info")
      return true
    } catch (error) {
      if (current()) this.notify(errorSuggestion(error), "warning")
      return false
    }
  }

  async runShell(command: string, approved = false): Promise<void> {
    const session = this.session()
    if (!this.api || !session) return
    const current = this.scope()
    try {
      const result = await this.api.shell(session.id, command, approved)
      if (!current()) return
      if (!result.ok) {
        this.notify(result.output || "Command failed", "error")
      } else if (lineCount(result.output) > 20) {
        const id = `shell_${Date.now()}`
        this.messages((current) => [
          ...current,
          {
            id,
            role: "assistant",
            content: "",
            reasoning: "",
            streaming: false,
            createdAt: new Date().toISOString(),
            toolCalls: [
              {
                id: `${id}_call`,
                name: "bash",
                summary: `$ ${command}`,
                status: "ok",
                output: result.output,
                error: null,
                durationMs: null,
              },
            ],
            diffs: [],
            reasoningOpen: false,
          },
        ])
      } else {
        this.messages((current) => [
          ...current,
          {
            id: `shell_${Date.now()}`,
            role: "tool",
            content: `$ ${command}\n${result.output}`,
            reasoning: "",
            streaming: false,
            createdAt: new Date().toISOString(),
            toolCalls: [],
            diffs: [],
            reasoningOpen: false,
          },
        ])
      }
    } catch (error) {
      if (!current()) return
      if (error instanceof ApiError && error.code === "permission_required") {
        const details = record(error.details)
        this.permission({
          sessionId: session.id,
          requestId: "shell",
          tool: "bash",
          kind: "shell",
          preview: text(details.preview, `$ ${command}`),
          choices: ["once", "deny"],
          shellCommand: command,
        })
      } else {
        this.notify(errorSuggestion(error), "error")
      }
    }
  }

  async resolvePermission(decision: PermissionChoice, reason?: string): Promise<void> {
    const permission = this.permission()
    const session = this.session()
    if (!permission || !session || !this.api) return
    if (permission.sessionId !== session.id || !permission.choices.includes(decision)) return
    if (this.decidingPermissions.has(permission)) return
    const current = this.scope()
    if (permission.shellCommand) {
      this.permission(null)
      if (decision !== "deny") await this.runShell(permission.shellCommand, true)
      return
    }
    this.decidingPermissions.add(permission)
    try {
      await this.api.decidePermission(permission.sessionId, permission.requestId, decision, reason)
      if (current() && this.permission() === permission) this.permission(null)
    } catch (error) {
      if (current()) this.notify(errorSuggestion(error), "error")
    } finally {
      this.decidingPermissions.delete(permission)
    }
  }

  async abort(): Promise<void> {
    const session = this.session()
    if (!this.api || !session || this.abortingSessions.has(session.id)) return
    const current = this.scope()
    this.abortingSessions.add(session.id)
    this.notify("Cancelling run…", "warning")
    try {
      // Esc can arrive after the local echo but before the daemon admits the run.
      const admission = this.pendingAdmission
      if (admission?.sessionId === session.id && !this.activeRunId()) {
        await admission.response.catch(() => undefined)
        if (!current() || !this.running()) return
      }
      const result = await this.api.abort(session.id)
      if (!current() || !this.running()) return
      this.clearQueuedPrompts()
      if (result.ok) return
      // The daemon has no live task, but a persisted/streamed status can still
      // say running after a restart or lost terminal event. Clear the stale UI.
      this.running(false)
      this.permission(null)
      this.providerRetry(null)
      this.pendingRunError = null
      const active = this.session()
      if (active) {
        this.session({ ...active, status: "idle" })
        this.setSessionStatus(active.id, "idle")
      }
      this.finishStreaming()
      this.notify("No active run; session state cleared", "warning")
    } catch (error) {
      if (current()) this.notify(errorSuggestion(error), "error")
    } finally {
      this.abortingSessions.delete(session.id)
    }
  }

  private clearQueuedPrompts(): void {
    const ids = new Set(this.queuedPrompts().map((item) => item.id))
    if (ids.size > 0) {
      this.messages((current) => current.filter((message) => !ids.has(message.id)))
    }
    this.queuedPrompts([])
  }

  private applyQueue(seq: number, items: unknown): void {
    if (seq <= this.queueEventSeq || !Array.isArray(items)) return
    if (this.queueEventSeq === -1) this.clearQueuedPrompts()
    this.queueEventSeq = seq
    this.queuedPrompts(
      items.flatMap((entry) => {
        const item = record(entry)
        const id = text(item.id)
        if (!id) return []
        const blockedReason = text(item.blocked_reason)
        return [
          {
            id,
            text: text(item.preview),
            attachmentCount: number(item.attachment_count, 0),
            ...(blockedReason ? { blockedReason } : {}),
          },
        ]
      }),
    )
  }

  async removeQueuedPrompt(id: string) {
    const session = this.session()
    if (!session || !this.api) return null
    const current = this.scope()
    try {
      const item = await this.api.removeQueuedPrompt(session.id, id)
      if (!current()) return null
      this.queuedPrompts((items) => items.filter((entry) => entry.id !== id))
      return item
    } catch (error) {
      if (current()) this.notify(errorSuggestion(error), "warning")
      return null
    }
  }

  async resolveCorrection(id: string): Promise<boolean> {
    const session = this.session()
    if (!session || !this.api) return false
    const current = this.scope()
    try {
      await this.api.resolveCorrection(session.id, id)
      if (!current()) return false
      this.messages((items) => items.filter((message) => message.id !== id))
      return true
    } catch (error) {
      if (current()) this.notify(errorSuggestion(error), "warning")
      return false
    }
  }

  finishStreaming(): void {
    this.flushStreams()
    this.messages((current) =>
      current.map((message) => (message.streaming ? { ...message, streaming: false } : message)),
    )
  }

  private queueDelta(id: string, field: "content" | "reasoning", value: string): void {
    if (!id || !value) return
    // Fresh stream data means the retry wait is over; do not keep the
    // "retrying" activity line after the next attempt has started writing.
    this.providerRetry(null)
    if (!this.messages().some((message) => message.id === id)) {
      this.updateMessage(id, (message) => ({ ...message, [field]: message[field] + value }))
      return
    }
    const buffer = this.streamBuffers.get(id) ?? { content: "", reasoning: "" }
    buffer[field] += value
    this.streamBuffers.set(id, buffer)
    if (this.streamTimer !== null) return
    this.streamTimer = setTimeout(() => {
      this.streamTimer = null
      this.flushStreams()
    }, 50)
  }

  private flushStreams(): void {
    if (this.streamTimer !== null) {
      clearTimeout(this.streamTimer)
      this.streamTimer = null
    }
    if (this.streamBuffers.size === 0) return
    const buffers = this.streamBuffers
    this.streamBuffers = new Map()
    this.messages((current) =>
      current.map((message) => {
        const buffer = buffers.get(message.id)
        if (!buffer) return message
        return {
          ...message,
          content: message.content + buffer.content,
          reasoning: message.reasoning + buffer.reasoning,
        }
      }),
    )
  }

  cancelRunningTools(): void {
    this.messages((current) =>
      current.map((message) =>
        message.toolCalls.some((tool) => tool.status === "running")
          ? {
              ...message,
              toolCalls: message.toolCalls.map((tool) =>
                tool.status === "running"
                  ? { ...tool, status: "cancelled" as const, error: null }
                  : tool,
              ),
            }
          : message,
      ),
    )
  }

  async selectModel(model: string): Promise<void> {
    const active = this.session()
    const current = this.latestRequest("model")
    if (!active) this.pendingModel(model)
    if (!this.api || !active) {
      const effort = this.effort()
      if (effort && !this.effortOptions().includes(effort as Effort)) {
        await this.setEffort("off")
        this.notify(`Effort reset: ${model} has no ${effort}`, "info")
      }
      this.notify(`Model: ${model} (next session)`, "success")
      return
    }
    try {
      const session = await this.api.updateSession(active.id, { model })
      if (!current()) return
      this.session((active) => (active ? { ...active, model: session.model } : active))
      this.pendingModel(null)
      this.contextState({ window: null, tokens: null, percent: null })
      this.usage((usage) =>
        usage
          ? { ...usage, context_window: null, context_tokens: null, context_percent: null }
          : usage,
      )
      void this.refreshUsage()
      const effort = this.effort()
      if (effort && !this.effortOptions().includes(effort as Effort)) {
        await this.setEffort("off")
        if (!current()) return
        this.notify(`Effort reset: ${model} has no ${effort}`, "info")
      }
      this.notify(`Model: ${model}`, "success")
    } catch (error) {
      if (!current()) return
      this.pendingModel(null)
      this.notify(errorSuggestion(error), "error")
    }
  }

  async setMode(mode: "build" | "plan"): Promise<void> {
    const active = this.session()
    const current = this.latestRequest("mode")
    if (!this.api || !active) {
      this.pendingMode(mode)
      this.notify(`Mode: ${mode} (next session)`, "info")
      return
    }
    try {
      const updated = await this.api.updateSession(active.id, { mode })
      if (!current()) return
      this.session((session) => (session ? { ...session, mode: updated.mode } : session))
      this.pendingMode((updated.mode as "build" | "plan") ?? "build")
      this.notify(`Mode: ${mode}`, "info")
    } catch (error) {
      if (!current()) return
      this.pendingMode((active.mode as "build" | "plan") ?? "build")
      this.notify(errorSuggestion(error), "error")
    }
  }

  async forkSession(): Promise<void> {
    const active = this.session()
    const current = this.scope()
    if (!this.api || !active) {
      this.notify("Start a session first", "warning")
      return
    }
    try {
      const result = await this.api.forkSession(active.id)
      if (!current()) return
      await this.reloadSessions()
      if (!current()) return
      await this.openSession(result.session.id)
      if (this.session()?.id === result.session.id) this.notify("Session forked", "success")
    } catch (error) {
      if (current()) this.notify(errorSuggestion(error), "error")
    }
  }

  async forkCheckpoint(checkpointId: string): Promise<void> {
    const active = this.session()
    const current = this.scope()
    if (!this.api || !active) {
      this.notify("Start a session first", "warning")
      return
    }
    try {
      const result = await this.api.forkSession(active.id, undefined, checkpointId)
      if (!current()) return
      await this.reloadSessions()
      if (!current()) return
      await this.openSession(result.session.id)
      if (this.session()?.id === result.session.id)
        this.notify("Session forked from checkpoint", "success")
    } catch (error) {
      if (current()) this.notify(errorSuggestion(error), "error")
    }
  }

  async refreshUsage(): Promise<void> {
    const active = this.session()
    if (!this.api || !active) return
    const current = this.latestRequest("usage")
    try {
      const response = await this.api.usage(active.id)
      if (current()) this.usage(response)
    } catch {
      // Status bar keeps the last known values.
    }
  }

  async loadFiles(force = false): Promise<void> {
    if (!this.api) return
    const active = this.session()
    const cwd = active?.cwd ?? process.env.SIWAH_WORKSPACE ?? process.cwd()
    const key = active?.id ?? cwd
    if (!force && this.filesSession === key && Date.now() - this.filesLoadedAt < 30_000) return
    const current = this.latestRequest("files")
    try {
      const result = active
        ? await this.api.files(active.id, 5000)
        : await this.api.filesForCwd(cwd, 5000)
      if (!current()) return
      const files = result.files ?? []
      const dirs = new Set<string>()
      for (const file of files) {
        const parts = file.split("/")
        for (let index = 1; index < parts.length; index += 1) {
          dirs.add(`${parts.slice(0, index).join("/")}/`)
        }
      }
      this.files([...dirs].sort().concat(files))
      this.filesSession = key
      this.filesLoadedAt = Date.now()
    } catch {
      if (current()) {
        this.files([])
        this.filesSession = null
      }
    }
  }

  toggleReasoning(messageId: string): void {
    this.messages((current) =>
      current.map((message) =>
        message.id === messageId ? { ...message, reasoningOpen: !message.reasoningOpen } : message,
      ),
    )
  }

  setTheme(name: ThemeName): void {
    this.theme(resolveTheme(name))
    try {
      saveTheme(name)
    } catch {
      // A read-only config must not take down the TUI.
    }
  }

  async latestTiming() {
    const session = this.session()
    if (!session || !this.api) return null
    const current = this.scope()
    const timing = await this.api.timing(session.id)
    return current() ? timing : null
  }

  async reviewChanges(
    scope: "turn" | "workspace",
    checkpointId?: string,
    filePath?: string,
    end?: string,
  ) {
    const session = this.session()
    if (!this.api || !session) return null
    const current = this.scope()
    const response = await this.api.changes(session.id, scope, checkpointId, filePath, end)
    return current() ? response : null
  }

  openDialog(dialog: DialogState): void {
    this.dialog(dialog)
  }

  closeDialog(): void {
    this.dialog(null)
  }

  notify(message: string, tone: ToastTone = "info"): void {
    const toast = { id: ++this.toastId, text: message, tone }
    this.toasts((current) => [...current.slice(-3), toast])
    setTimeout(() => {
      this.toasts((current) => current.filter((item) => item.id !== toast.id))
    }, 4500)
  }

  async quickNote(note: string): Promise<void> {
    if (!this.api) {
      this.notify("Daemon unavailable", "error")
      return
    }
    try {
      const result = await this.api.quickNote(note)
      this.notify(result.changed ? "Saved to memory inbox" : "Note already saved", "success")
    } catch (error) {
      this.notify(errorSuggestion(error), "error")
    }
  }

  async listSkills(): Promise<SkillList> {
    if (!this.api) return { skills: [] }
    try {
      return await this.api.skills(this.workspacePath())
    } catch (error) {
      this.notify(errorSuggestion(error), "error")
      return { skills: [] }
    }
  }

  async skill(name: string): Promise<SkillContent | null> {
    if (!this.api) return null
    try {
      return await this.api.skill(name, this.workspacePath())
    } catch (error) {
      this.notify(errorSuggestion(error), "error")
      return null
    }
  }

  async memory(): Promise<MemoryView | null> {
    if (!this.api) return null
    try {
      return await this.api.memory(this.workspacePath())
    } catch (error) {
      this.notify(errorSuggestion(error), "error")
      return null
    }
  }

  async initMemory(): Promise<void> {
    if (!this.api) {
      this.notify("Daemon unavailable", "error")
      return
    }
    try {
      const result = await this.api.initMemory(this.workspacePath())
      this.notify(
        result.created ? "Created AGENTS.md" : "AGENTS.md already exists",
        result.created ? "success" : "info",
      )
    } catch (error) {
      this.notify(errorSuggestion(error), "error")
    }
  }

  pushHistory(prompt: string): void {
    if (this.promptHistory[this.promptHistory.length - 1] !== prompt)
      this.promptHistory.push(prompt)
    if (this.promptHistory.length > HISTORY_LIMIT)
      this.promptHistory.splice(0, this.promptHistory.length - HISTORY_LIMIT)
    this.historyIndex = this.promptHistory.length
    this.history([...this.promptHistory])
    savePromptHistory(this.workspacePath(), this.promptHistory)
  }

  historyPrevious(): string | null {
    if (this.promptHistory.length === 0) return null
    this.historyIndex = Math.max(0, this.historyIndex - 1)
    return this.promptHistory[this.historyIndex] ?? null
  }

  historyNext(): string | null {
    if (this.promptHistory.length === 0) return null
    this.historyIndex = Math.min(this.promptHistory.length, this.historyIndex + 1)
    return this.promptHistory[this.historyIndex] ?? ""
  }

  private setSessionTitle(sessionId: string, title: string): void {
    this.sessions((current) =>
      current.map((session) => (session.id === sessionId ? { ...session, title } : session)),
    )
  }

  private setSessionStatus(sessionId: string, status: string): void {
    this.sessions((current) =>
      current.map((session) => (session.id === sessionId ? { ...session, status } : session)),
    )
  }

  private startStream(sessionId: string): void {
    if (!this.api) return
    this.flushStreams()
    this.streamAbort?.abort()
    const controller = new AbortController()
    this.streamAbort = controller
    void this.streamLoop(sessionId, controller.signal)
  }

  private async streamLoop(sessionId: string, signal: AbortSignal): Promise<void> {
    if (!this.api) return
    const current = this.scope()
    const ownsStream = () => !signal.aborted && current() && this.session()?.id === sessionId
    while (ownsStream()) {
      try {
        this.connection(this.lastSeq === 0 ? "connecting" : "reconnecting")
        for await (const event of this.api.streamEvents(sessionId, this.lastSeq, signal, () => {
          if (!ownsStream()) return
          this.connection("online")
          this.runtimeError(null)
        })) {
          if (!ownsStream()) return
          this.connection("online")
          this.handleEvent(event)
        }
      } catch (error) {
        if (!ownsStream()) return
        if (error instanceof ApiError && error.status === 409) {
          // The retained replay window expired or the daemon restarted. Reload
          // stored messages, then resume after the bus's advertised snapshot so
          // old deltas are not replayed over hydrated content.
          const details = error.details as { last_seq?: unknown } | undefined
          const after = typeof details?.last_seq === "number" ? details.last_seq : 0
          await this.openSession(sessionId, { after })
          return
        }
        this.connection("reconnecting")
        this.runtimeError(errorSuggestion(error))
        await sleep(800)
      }
      if (!ownsStream()) return
      // A stream that ends cleanly still needs a pause; otherwise the reconnect
      // loop spins on a fast-closing daemon.
      await sleep(200)
    }
  }

  private handleEvent(event: EventEnvelope): void {
    if (event.session_id && event.session_id !== this.session()?.id) return
    if (Number.isFinite(event.seq)) {
      if (event.seq <= this.lastSeq) return
      this.lastSeq = event.seq
    }
    if (event.id) {
      if (this.seenEventIds.has(event.id)) return
      this.seenEventIds.add(event.id)
      if (this.seenEventIds.size > 4096) {
        const oldest = this.seenEventIds.values().next().value
        if (oldest !== undefined) this.seenEventIds.delete(oldest)
      }
    }
    const data = record(event.data)
    switch (event.type) {
      case "queue.changed": {
        this.applyQueue(event.seq, data.items)
        break
      }
      case "steering.applied":
      case "steering.dropped": {
        const id = text(data.message_id)
        if (!id || this.messages().some((message) => message.id === id)) break
        this.messages((messages) => [
          ...messages,
          {
            id,
            role: "user",
            content: text(data.content),
            reasoning: "",
            streaming: false,
            createdAt: event.ts,
            toolCalls: [],
            diffs: [],
            reasoningOpen: false,
            unapplied: event.type === "steering.dropped",
          },
        ])
        this.scrollTick((tick) => tick + 1)
        break
      }
      case "message.start": {
        this.providerRetry(null)
        if (text(data.role) === "user") {
          const id = text(data.message_id)
          if (
            id &&
            data.queued_prompt_id &&
            !this.messages().some((message) => message.id === id)
          ) {
            this.messages((current) => [
              ...current,
              {
                id,
                role: "user",
                content: text(data.content) || "Image message",
                reasoning: "",
                streaming: false,
                createdAt: event.ts,
                toolCalls: [],
                diffs: [],
                reasoningOpen: false,
              },
            ])
            this.scrollTick((tick) => tick + 1)
          } else if (this.queueEventSeq === -1 && this.queuedPrompts().length > 0) {
            this.queuedPrompts((current) => current.slice(1))
          }
        }
        if (text(data.role) !== "assistant" || !text(data.message_id)) break
        const id = text(data.message_id)
        if (this.messages().some((message) => message.id === id)) break
        this.streamBuffers.delete(id)
        this.messages((current) => [
          ...current,
          {
            id,
            role: "assistant",
            content: "",
            reasoning: "",
            streaming: true,
            createdAt: new Date().toISOString(),
            toolCalls: [],
            diffs: [],
            reasoningOpen: false,
          },
        ])
        break
      }
      case "message.delta": {
        this.queueDelta(text(data.message_id), "content", text(data.text))
        break
      }
      case "reasoning.delta": {
        this.queueDelta(text(data.message_id), "reasoning", text(data.text))
        break
      }
      case "message.end": {
        this.flushStreams()
        const id = text(data.message_id)
        if (!id || !this.messages().some((message) => message.id === id)) break
        this.updateMessage(id, (message) => ({ ...message, streaming: false }))
        break
      }
      case "message.reset": {
        const id = text(data.message_id)
        if (!id) break
        this.streamBuffers.delete(id)
        this.updateMessage(id, (message) => ({
          ...message,
          content: "",
          reasoning: "",
          toolCalls: [],
          diffs: [],
          streaming: true,
        }))
        break
      }
      case "tool.start": {
        this.flushStreams()
        this.providerRetry(null)
        const tool: ToolView = {
          id: text(data.tool_call_id, `tool_${Date.now()}`),
          name: text(data.name, "tool"),
          summary: text(data.arguments_summary),
          status: "running",
          output: "",
          error: null,
          durationMs: null,
        }
        // A hydrated call already owns its persisted output/status. Replaying
        // tool.start must not duplicate it or turn a completed tool into running.
        if (
          this.messages().some((message) => message.toolCalls.some((call) => call.id === tool.id))
        )
          break
        const messageId = text(data.message_id) || this.activeMessage()?.id || `tool_${tool.id}`
        this.updateMessage(messageId, (message) => ({
          ...message,
          toolCalls: [...message.toolCalls, tool],
        }))
        break
      }
      case "tool.progress": {
        this.updateTool(text(data.tool_call_id), (tool) => ({
          ...tool,
          output: tool.output + text(data.chunk),
        }))
        break
      }
      case "tool.end": {
        const id = text(data.tool_call_id)
        const finish = (tool: ToolView): ToolView => ({
          ...tool,
          status:
            data.status === "ok" || data.status === "skipped" || data.status === "cancelled"
              ? data.status
              : "error",
          output: text(data.output),
          error: data.error ? text(data.error) : null,
          durationMs: number(data.duration_ms),
          edited: data.edited === true,
        })
        const messageId = text(data.message_id)
        if (data.status === "skipped" && id && messageId) {
          this.updateMessage(messageId, (message) => {
            const known = message.toolCalls.some((tool) => tool.id === id)
            return {
              ...message,
              toolCalls: known
                ? message.toolCalls.map((tool) => (tool.id === id ? finish(tool) : tool))
                : [
                    ...message.toolCalls,
                    finish({
                      id,
                      name: text(data.name, "tool"),
                      summary: text(data.arguments_summary),
                      status: "skipped",
                      output: "",
                      error: null,
                      durationMs: null,
                    }),
                  ],
            }
          })
        } else {
          this.updateTool(id, finish)
        }
        break
      }
      case "diff": {
        this.flushStreams()
        const block = { path: text(data.path), patch: text(data.patch) }
        const message = this.activeMessage()
        if (message)
          this.updateMessage(message.id, (item) => ({ ...item, diffs: [...item.diffs, block] }))
        break
      }
      case "todo.update": {
        const items = Array.isArray(data.items) ? data.items : []
        this.todos(
          items.map((item, index) => {
            const row = record(item)
            return {
              id: text(row.id, String(index + 1)),
              content: text(row.content),
              status: text(row.status, "pending"),
            }
          }),
        )
        break
      }
      case "permission.request": {
        this.permission({
          sessionId: this.session()?.id,
          requestId: text(data.request_id),
          toolCallId: text(data.tool_call_id) || undefined,
          tool: text(data.tool),
          kind: text(data.kind),
          preview: text(data.preview),
          choices: Array.isArray(data.choices)
            ? data.choices.map((choice) => String(choice))
            : ["once", "deny"],
        })
        break
      }
      case "permission.resolved": {
        if (this.permission()?.requestId === text(data.request_id)) this.permission(null)
        break
      }
      case "provider.retry": {
        this.providerRetry({
          attempt: number(data.attempt, 1),
          max: number(data.max_attempts, 1),
          delayMs: Math.max(0, number(data.delay_s) * 1000),
          model: text(data.model),
          code: text(data.code, "retry"),
        })
        break
      }
      case "usage": {
        void this.refreshUsage()
        break
      }
      case "status": {
        const state = text(data.state)
        if (data.phase === "finishing_hooks") this.setRunPhase("finishing hooks")
        const activeId = this.session()?.id
        if (state === "running" || state === "waiting_permission") {
          if (event.turn_id) this.activeRunId(event.turn_id)
          this.running(true)
          if (activeId) this.setSessionStatus(activeId, "running")
        }
        if (state === "idle") {
          this.setRunPhase(null)
          this.running(false)
          if (activeId) this.setSessionStatus(activeId, "idle")
        }
        break
      }
      case "run.end": {
        this.activeRunId(null)
        this.setRunPhase(null)
        const reason = text(data.reason, "completed")
        this.providerRetry(null)
        this.running(false)
        this.permission(null)
        const activeId = this.session()?.id
        if (activeId) this.setSessionStatus(activeId, "idle")
        this.cancelRunningTools()
        this.finishStreaming()
        if (reason !== "completed" && reason !== "aborted") {
          const pending = this.pendingRunError
          const detail = pending?.message || text(data.error) || runEndFallback(reason)
          this.runNotice({
            id: ++this.runNoticeId,
            tone: reason === "stalled" || reason === "max_steps" ? "warning" : "error",
            title: pending ? "Run failed" : runEndTitle(reason),
            text: detail,
          })
        }
        this.pendingRunError = null
        if (reason === "aborted") this.clearQueuedPrompts()
        void this.refreshUsage()
        void this.reloadSessions()
        void this.loadFiles(true)
        break
      }
      case "cancelled": {
        this.providerRetry(null)
        this.pendingRunError = null
        this.clearQueuedPrompts()
        this.running(false)
        this.permission(null)
        const activeId = this.session()?.id
        if (activeId) this.setSessionStatus(activeId, "idle")
        this.cancelRunningTools()
        this.finishStreaming()
        void this.reloadSessions()
        void this.loadFiles(true)
        break
      }
      case "error": {
        const code = text(data.code, "error")
        const message = text(data.message, "The run failed without an error message.")
        this.pendingRunError = { code, message }
        this.providerRetry(null)
        this.runNotice({
          id: ++this.runNoticeId,
          tone: "error",
          title: "Run failed",
          text: message,
        })
        this.notify(message, "error")
        const activeId = this.session()?.id
        if (activeId) this.setSessionStatus(activeId, "error")
        this.cancelRunningTools()
        this.finishStreaming()
        break
      }
      case "checkpoint": {
        void this.reloadCheckpoints()
        break
      }
      case "context": {
        this.contextState({
          window: typeof data.context_window === "number" ? data.context_window : null,
          tokens: typeof data.tokens === "number" ? data.tokens : null,
          percent: typeof data.percent === "number" ? data.percent : null,
          estimated: data.estimated === true,
        })
        break
      }
      case "verifier": {
        const state = text(data.status)
        const verdict = text(data.verdict)
        if (state === "baseline") this.setRunPhase("checking baseline")
        else if (state === "running") this.setRunPhase("running checks")
        else if (state === "retry") this.setRunPhase("repairing failed checks")
        else {
          this.setRunPhase(null)
          if (verdict === "pre_existing")
            this.notify("Checks still have pre-existing failures", "warning")
          else if (verdict === "unverified") this.notify("Some checks could not finish", "warning")
          else if (state === "failed") this.notify("Verification failed", "warning")
        }
        break
      }
      case "budget.extended": {
        const next = number(data.new_limit)
        if (next > 0) this.notify(`Step budget extended to ${next} steps`, "info")
        break
      }
      case "completion.check": {
        const status = text(data.status)
        if (status === "retry" && text(data.reason) === "todos")
          this.notify("Reconciling the remaining todo items before finishing", "info")
        if (status === "unfinished") this.notify("Run ended with unfinished items", "warning")
        break
      }
      case "plan": {
        this.notify(`Plan saved to ${text(data.path, ".siwah/plan.md")}`, "success")
        void this.loadPlan()
        break
      }
      case "evolution.event": {
        this.notify(`siwah learned: ${text(data.kind, "update")}`, "info")
        void this.reloadEvolution()
        break
      }
      default:
        break
    }
  }

  private updateMessage(id: string, update: (message: UiMessage) => UiMessage): void {
    if (!id) return
    this.messages((current) => {
      const index = current.findIndex((message) => message.id === id)
      if (index < 0) {
        // A delta for an id that was never hydrated belongs to a new message;
        // never append it to an unrelated earlier one.
        const created: UiMessage = {
          id,
          role: "assistant",
          content: "",
          reasoning: "",
          streaming: true,
          createdAt: new Date().toISOString(),
          toolCalls: [],
          diffs: [],
          reasoningOpen: false,
        }
        return [...current, update(created)]
      }
      return current.map((message, messageIndex) =>
        messageIndex === index ? update(message) : message,
      )
    })
  }

  private updateTool(id: string, update: (tool: ToolView) => ToolView): void {
    this.messages((current) =>
      current.map((message) => ({
        ...message,
        toolCalls: message.toolCalls.map((tool) => (tool.id === id ? update(tool) : tool)),
      })),
    )
  }
}

export function hydrateMessages(records: ChatMessage[], running = false): UiMessage[] {
  const messages: UiMessage[] = []
  const byToolId = new Map<string, ToolView>()
  const knownOutcomes = new Set<string>()
  const activeId = running
    ? records.filter((record) => record.role === "assistant").at(-1)?.id
    : null
  for (const record of records) {
    if (record.role === "user") {
      messages.push({
        id: record.id,
        role: "user",
        content: record.content ?? "",
        reasoning: "",
        streaming: false,
        createdAt: record.created_at,
        toolCalls: [],
        diffs: [],
        reasoningOpen: false,
        unapplied: record.delivery === "unapplied",
        images: (record.attachments ?? []).map((item, index) => ({
          ...item,
          id: `${record.id}_img_${index}`,
        })),
      })
      continue
    }
    if (record.role === "assistant") {
      const tools: ToolView[] = (record.tool_calls ?? []).flatMap((call) => {
        if (byToolId.has(call.id)) return []
        const outcome = record.tool_outcomes?.[call.id]
        const fullOutput = record.tool_outputs?.[call.id]
        if (outcome) knownOutcomes.add(call.id)
        const tool: ToolView = {
          id: call.id,
          name: call.name,
          summary: JSON.stringify(call.arguments),
          status:
            outcome?.status ??
            (fullOutput !== undefined ? "ok" : record.id === activeId ? "running" : "cancelled"),
          output: fullOutput ?? "",
          error: outcome?.error ?? null,
          durationMs: outcome?.duration_ms ?? null,
          edited: record.tool_edited?.[call.id] ?? false,
        }
        byToolId.set(tool.id, tool)
        return [tool]
      })
      messages.push({
        id: record.id,
        role: "assistant",
        content: record.content ?? "",
        reasoning: record.reasoning ?? "",
        streaming: false,
        createdAt: record.created_at,
        toolCalls: tools,
        diffs: (record.diffs ?? []).map((diff) => ({ path: diff.path, patch: diff.patch })),
        reasoningOpen: false,
      })
      continue
    }
    if (record.role === "tool" && record.tool_call_id) {
      const tool = byToolId.get(record.tool_call_id)
      if (tool) {
        if (!tool.output) tool.output = record.content ?? ""
        if (!knownOutcomes.has(tool.id)) tool.status = "ok"
      }
    }
  }
  return messages
}

function todosFromSession(session: Session): TodoItem[] {
  const raw = session.metadata?.todos
  if (!Array.isArray(raw)) return []
  return raw.map((item, index) => {
    const row = record(item)
    return {
      id: text(row.id, String(index + 1)),
      content: text(row.content),
      status: text(row.status, "pending"),
    }
  })
}

export { RuntimeMissingError }
