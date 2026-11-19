/**
 * Typed local daemon client. The generated OpenAPI types are the wire contract.
 */

import { readFile } from "node:fs/promises"
import { homedir } from "node:os"
import { join } from "node:path"

import type { components } from "./schema"

type Schemas = components["schemas"]

export const SUPPORTED_PROTOCOL = "0.1"

export type Session = Schemas["SessionResponse"]
export type ChatMessage = Schemas["MessageView"]
export type Usage = Schemas["UsageResponse"]
export type Meta = Schemas["MetaResponse"]
export type ModelCatalog = Schemas["ModelCatalogResponse"]
export type ModelSummary = Schemas["ModelSummary"]
export type EventEnvelope = Schemas["EventEnvelope"]
export type PermissionChoice = "once" | "session" | "always" | "deny"
export type CreateSession = Schemas["CreateSessionRequest"]
export type UpdateSession = Schemas["UpdateSessionRequest"]
export type ShellResult = Schemas["ShellResponse"]
export type AuthStatus = Schemas["AuthStatusResponse"]
export type ForkResult = Schemas["ForkSessionResponse"]
export type SkillList = Schemas["SkillListResponse"]
export type SkillContent = Schemas["SkillContentResponse"]
export type MemoryView = Schemas["MemoryResponse"]
export type InitMemory = Schemas["InitMemoryResponse"]
export type QuickNote = Schemas["QuickNoteResponse"]
export type Checkpoint = Schemas["CheckpointSummary"]
export type CheckpointList = Schemas["CheckpointListResponse"]
export type RewindResult = Schemas["RewindResponse"]
export type CompactResult = Schemas["CompactResponse"]
export type PlanView = Schemas["PlanResponse"]
export type ToolOutput = Schemas["ToolOutputResponse"]
export type UpdateStatus = Schemas["UpdateStatusResponse"]
export type UpdateApply = Schemas["UpdateApplyResponse"]
export type EffortResponse = Schemas["EffortResponse"]
export type McpServerStatus = Schemas["McpServerStatus"]
export type McpStatus = Schemas["McpStatusResponse"]
export type MessageAttachment = Schemas["MessageAttachment"]

export type RuntimeConfig = {
  baseUrl: string
  token: string
  home: string
}

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly code: string,
    readonly details?: unknown,
  ) {
    super(message)
    this.name = "ApiError"
  }
}

export class RuntimeMissingError extends Error {
  constructor(readonly runtimeFile: string) {
    super(
      `siwah daemon is not running. Start it with \`siwah serve\`, or set SIWAH_SERVER/SIWAH_TOKEN.`,
    )
    this.name = "RuntimeMissingError"
  }
}

export async function resolveRuntime(): Promise<RuntimeConfig> {
  const explicitServer = process.env.SIWAH_SERVER
  const explicitToken = process.env.SIWAH_TOKEN
  const home = process.env.SIWAH_HOME ?? join(homedir(), ".siwah")
  if (explicitServer && explicitToken) {
    return {
      baseUrl: explicitServer.replace(/\/+$/, ""),
      token: explicitToken,
      home,
    }
  }
  const runtimeFile = join(home, "runtime.json")
  let raw: string
  try {
    raw = await readFile(runtimeFile, "utf8")
  } catch {
    throw new RuntimeMissingError(runtimeFile)
  }
  const parsed = JSON.parse(raw) as {
    host?: string
    port?: number
    token?: string
  }
  if (!parsed.host || !parsed.port || !parsed.token) {
    throw new RuntimeMissingError(runtimeFile)
  }
  const host = parsed.host === "0.0.0.0" || parsed.host === "::" ? "127.0.0.1" : parsed.host
  return {
    baseUrl: `http://${host}:${parsed.port}`,
    token: parsed.token,
    home,
  }
}

export type ParsedApiError = {
  code: string
  message: string
  details?: unknown
}

/**
 * FastAPI uses `detail` for HTTPException payloads; older siwah handlers used
 * `error`. Accept both and keep validation arrays readable.
 */
export function parseApiError(
  status: number,
  statusText: string,
  payload: unknown,
): ParsedApiError {
  let code = `http_${status}`
  let message = statusText || `HTTP ${status}`
  let details: unknown
  if (payload && typeof payload === "object") {
    const body = payload as { error?: unknown; detail?: unknown }
    const detail = body.error ?? body.detail
    if (typeof detail === "string") {
      message = detail
    } else if (Array.isArray(detail)) {
      const first = detail[0] as { msg?: unknown; loc?: unknown } | undefined
      message = typeof first?.msg === "string" ? first.msg : "Validation failed"
      details = detail
    } else if (detail && typeof detail === "object") {
      const record = detail as { code?: unknown; message?: unknown }
      if (typeof record.code === "string") code = record.code
      if (typeof record.message === "string") message = record.message
      details = detail
    }
  }
  return { code, message, details }
}

export class SiwahApi {
  constructor(readonly runtime: RuntimeConfig) {}

  private headers(extra?: HeadersInit): Headers {
    const headers = new Headers(extra)
    headers.set("Authorization", `Bearer ${this.runtime.token}`)
    headers.set("X-Siwah-Protocol", SUPPORTED_PROTOCOL)
    if (!headers.has("Accept")) headers.set("Accept", "application/json")
    return headers
  }

  private async request<T>(path: string, init?: RequestInit): Promise<T> {
    const response = await fetch(`${this.runtime.baseUrl}${path}`, {
      ...init,
      headers: this.headers(init?.headers),
    })
    if (!response.ok) {
      let payload: unknown
      try {
        payload = await response.json()
      } catch {
        // Keep the HTTP fallback text.
      }
      const parsed = parseApiError(response.status, response.statusText, payload)
      throw new ApiError(parsed.message, response.status, parsed.code, parsed.details)
    }
    if (response.status === 204) return undefined as T
    return (await response.json()) as T
  }

  meta() {
    return this.request<Schemas["MetaResponse"]>("/v1/meta")
  }

  updateStatus(refresh = false) {
    return this.request<UpdateStatus>(`/v1/update${refresh ? "?refresh=true" : ""}`)
  }

  applyUpdate() {
    return this.request<UpdateApply>("/v1/update", { method: "POST" })
  }

  health() {
    return this.request<Schemas["OkResponse"]>("/v1/health")
  }

  sessions() {
    return this.request<Schemas["SessionListResponse"]>("/v1/sessions")
  }

  session(sessionId: string) {
    return this.request<Session>(`/v1/sessions/${encodeURIComponent(sessionId)}`)
  }

  createSession(body: CreateSession) {
    return this.request<Session>("/v1/sessions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    })
  }

  messages(sessionId: string) {
    return this.request<Schemas["MessageListResponse"]>(
      `/v1/sessions/${encodeURIComponent(sessionId)}/messages`,
    )
  }

  usage(sessionId: string) {
    return this.request<Usage>(`/v1/sessions/${encodeURIComponent(sessionId)}/usage`)
  }

  updateSession(sessionId: string, body: UpdateSession) {
    return this.request<Session>(`/v1/sessions/${encodeURIComponent(sessionId)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    })
  }

  forkSession(sessionId: string, title?: string, checkpointId?: string) {
    return this.request<ForkResult>(`/v1/sessions/${encodeURIComponent(sessionId)}/fork`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title, checkpoint_id: checkpointId }),
    })
  }

  checkpoints(sessionId: string) {
    return this.request<CheckpointList>(`/v1/sessions/${encodeURIComponent(sessionId)}/checkpoints`)
  }

  createCheckpoint(sessionId: string, label?: string) {
    return this.request<Checkpoint>(`/v1/sessions/${encodeURIComponent(sessionId)}/checkpoints`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ label }),
    })
  }

  rewind(sessionId: string, body: Schemas["RewindRequest"]) {
    return this.request<RewindResult>(`/v1/sessions/${encodeURIComponent(sessionId)}/rewind`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    })
  }

  compact(sessionId: string) {
    return this.request<CompactResult>(`/v1/sessions/${encodeURIComponent(sessionId)}/compact`, {
      method: "POST",
    })
  }

  plan(sessionId: string) {
    return this.request<PlanView>(`/v1/sessions/${encodeURIComponent(sessionId)}/plan`)
  }

  toolOutput(sessionId: string, toolCallId: string) {
    return this.request<ToolOutput>(
      `/v1/sessions/${encodeURIComponent(sessionId)}/tool_calls/${encodeURIComponent(toolCallId)}`,
    )
  }

  permissionMode() {
    return this.request<{ mode: string }>("/v1/permissions/mode")
  }

  setPermissionMode(mode: "ask" | "auto-edit" | "yolo") {
    return this.request<{ mode: string }>("/v1/permissions/mode", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode }),
    })
  }

  mcpStatus(refresh = true) {
    return this.request<McpStatus>(`/v1/mcp?refresh=${refresh ? "true" : "false"}`)
  }

  restartMcp(name: string) {
    return this.request<McpServerStatus>(`/v1/mcp/${encodeURIComponent(name)}/restart`, {
      method: "POST",
    })
  }

  sendMessage(sessionId: string, content: string, attachments: MessageAttachment[] = []) {
    return this.request<Schemas["SendMessageResponse"]>(
      `/v1/sessions/${encodeURIComponent(sessionId)}/messages`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ content, attachments }),
      },
    )
  }

  abort(sessionId: string) {
    return this.request<Schemas["OkResponse"]>(
      `/v1/sessions/${encodeURIComponent(sessionId)}/abort`,
      { method: "POST" },
    )
  }

  decidePermission(
    sessionId: string,
    requestId: string,
    decision: PermissionChoice,
    reason?: string,
  ) {
    return this.request<Schemas["PermissionDecisionResponse"]>(
      `/v1/sessions/${encodeURIComponent(sessionId)}/permissions/${encodeURIComponent(requestId)}`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ decision, reason }),
      },
    )
  }

  models(provider?: string) {
    const suffix = provider ? `?provider=${encodeURIComponent(provider)}` : ""
    return this.request<ModelCatalog>(`/v1/models${suffix}`)
  }

  files(sessionId: string, limit = 500) {
    return this.request<Schemas["FileListResponse"]>(
      `/v1/files?session_id=${encodeURIComponent(sessionId)}&limit=${limit}`,
    )
  }

  filesForCwd(cwd: string, limit = 500) {
    return this.request<Schemas["FileListResponse"]>(
      `/v1/files?cwd=${encodeURIComponent(cwd)}&limit=${limit}`,
    )
  }

  authStatus() {
    return this.request<AuthStatus>("/v1/auth/status")
  }

  skills(cwd?: string) {
    return this.request<SkillList>(
      `/v1/harness/skills${cwd ? `?cwd=${encodeURIComponent(cwd)}` : ""}`,
    )
  }

  skill(name: string, cwd?: string) {
    return this.request<SkillContent>(
      `/v1/harness/skills/${encodeURIComponent(name)}${cwd ? `?cwd=${encodeURIComponent(cwd)}` : ""}`,
    )
  }

  memory(cwd: string) {
    return this.request<MemoryView>(`/v1/harness/memory?cwd=${encodeURIComponent(cwd)}`)
  }

  initMemory(cwd: string) {
    return this.request<InitMemory>("/v1/harness/init", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ cwd }),
    })
  }

  quickNote(content: string) {
    return this.request<QuickNote>("/v1/harness/note", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content }),
    })
  }

  ensureProviders() {
    return this.request<AuthStatus>("/v1/providers/defaults", { method: "POST" })
  }

  authLogin(provider: string, apiKey: string) {
    return this.request<Schemas["OkResponse"]>("/v1/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ provider, api_key: apiKey }),
    })
  }

  setDefaultEffort(effort: string) {
    return this.request<EffortResponse>("/v1/agent/effort", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ effort }),
    })
  }

  shell(sessionId: string, command: string, approved = false, timeoutS = 120) {
    return this.request<ShellResult>(`/v1/sessions/${encodeURIComponent(sessionId)}/shell`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ command, approved, timeout_s: timeoutS }),
    })
  }

  evolution() {
    return this.request<Schemas["EvolutionSummary"]>("/v1/evolution")
  }

  evolutionPause(paused = true) {
    return this.request<{ ok: boolean }>("/v1/evolution/pause", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ paused }),
    })
  }

  evolutionRollback(tag?: string | null) {
    return this.request<{ ok: boolean; tag?: string | null; commit?: string | null }>(
      "/v1/evolution/rollback",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ tag: tag ?? null }),
      },
    )
  }

  commands(cwd?: string) {
    return this.request<{ commands: Array<{ name: string; description: string; body: string }> }>(
      `/v1/commands${cwd ? `?cwd=${encodeURIComponent(cwd)}` : ""}`,
    )
  }

  /**
   * SSE is a fetch stream, not EventSource, because EventSource cannot send the
   * local bearer token or a Last-Event-ID header on all platforms.
   */
  async *streamEvents(
    sessionId: string,
    after = 0,
    signal?: AbortSignal,
    onOpen?: () => void,
  ): AsyncGenerator<EventEnvelope> {
    const response = await fetch(
      `${this.runtime.baseUrl}/v1/sessions/${encodeURIComponent(sessionId)}/events?after=${after}`,
      {
        headers: this.headers({
          Accept: "text/event-stream",
          ...(after > 0 ? { "Last-Event-ID": String(after) } : {}),
        }),
        signal,
      },
    )
    if (!response.ok || !response.body) {
      let payload: unknown
      try {
        payload = await response.json()
      } catch {
        // Keep the HTTP fallback text.
      }
      const parsed = parseApiError(response.status, response.statusText, payload)
      throw new ApiError(parsed.message, response.status, parsed.code, parsed.details)
    }
    onOpen?.()
    const reader = response.body.getReader()
    const decoder = new TextDecoder()
    let buffer = ""
    let pendingCr = false
    let finished = false
    try {
      while (true) {
        const { done, value } = await reader.read()
        if (done) {
          finished = true
          break
        }
        // CRLF may straddle reads, just as a UTF-8 code point may. Normalize
        // incrementally, including the standalone CR accepted by SSE.
        const chunk = decoder.decode(value, { stream: true })
        for (const character of chunk) {
          if (pendingCr && character === "\n") {
            pendingCr = false
            continue
          }
          pendingCr = character === "\r"
          buffer += pendingCr ? "\n" : character
        }
        let boundary = buffer.indexOf("\n\n")
        while (boundary >= 0) {
          const frame = buffer.slice(0, boundary)
          buffer = buffer.slice(boundary + 2)
          const event = parseFrame(frame)
          if (event) yield event
          boundary = buffer.indexOf("\n\n")
        }
      }
    } finally {
      try {
        if (!finished) await reader.cancel()
      } finally {
        reader.releaseLock()
      }
    }
  }
}

function parseFrame(frame: string): EventEnvelope | null {
  const data: string[] = []
  for (const line of frame.split("\n")) {
    if (line.startsWith(":")) continue
    if (line.startsWith("data:")) data.push(line.slice(5).trimStart())
  }
  if (data.length === 0) return null
  try {
    return JSON.parse(data.join("\n")) as EventEnvelope
  } catch {
    return null
  }
}

export function errorSuggestion(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 429 || error.code === "provider_rate_limited") {
      return "Allowance or rate limit reached. Check /cost or switch model with /models."
    }
    if (error.status === 401) return "The local daemon token is invalid. Restart `siwah serve`."
    if (error.status === 409) return "A run or permission is already pending. Resolve it first."
    if (error.status >= 500) return "The daemon or provider failed. Retry, or run `siwah doctor`."
    return error.message
  }
  if (error instanceof RuntimeMissingError) {
    return "Start the daemon with `siwah serve`, then reopen the TUI."
  }
  return error instanceof Error ? error.message : String(error)
}
