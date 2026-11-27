import { homedir } from "node:os"
import { join } from "node:path"
import { useKeyboard, usePaste, useRenderer } from "@opentui/solid"
import type { JSX } from "solid-js"
import {
  createEffect,
  createMemo,
  createResource,
  createSignal,
  For,
  Show,
  untrack,
} from "solid-js"
import {
  ApiError,
  type ChatGPTAccount,
  type Checkpoint,
  type MessageAttachment,
  type Session,
} from "../api/client"
import { commands, fuzzyMatch, helpText } from "../commands"
import type { DialogState, Effort, UiMessage } from "../state"
import { overlayBackground, resolveTheme, type ThemeName, themeNames } from "../theme"
import {
  clipCells,
  ellipsis,
  formatTokens,
  maskedSecret,
  projectName,
  relativeTime,
  windowStart,
} from "../util"
import { useDimensions, useStore, useTheme } from "./context"
import { DiffView } from "./DiffView"
import { Modal } from "./Modal"
import { Pulse } from "./Motion"
import { MetricRow, Notice, SectionLabel } from "./Surface"
import { TodoPanel } from "./TodoPanel"

function ChangesDialog(props: {
  scope: "turn" | "workspace"
  checkpointId?: string
  onClose: () => void
}) {
  const store = useStore()
  const theme = useTheme()
  const [selected, setSelected] = createSignal<string | null>(null)
  const [data] = createResource(
    () => props.scope,
    (scope) => store.reviewChanges(scope, props.checkpointId).catch(() => null),
  )
  const current = () => data()?.files.find((file) => file.path === selected())
  const [detail] = createResource(current, (file) =>
    store
      .reviewChanges(
        props.scope,
        data()?.base ?? props.checkpointId,
        file.path,
        data()?.end ?? undefined,
      )
      .then((response) => response?.files.find((item) => item.path === file.path) ?? null)
      .catch(() => null),
  )
  const title = () =>
    props.scope === "workspace"
      ? "workspace changes"
      : props.checkpointId
        ? "turn changes"
        : "latest turn changes"
  return (
    <Show
      when={current()}
      fallback={
        <Show
          when={!data.loading && data()?.available && (data()?.files.length ?? 0) > 0}
          fallback={
            <>
              <Modal title={title()} width={76} height={7}>
                <text fg={theme.muted}>
                  {data.loading
                    ? "Loading changes..."
                    : data()?.available
                      ? "No visible changes."
                      : "Changes are unavailable."}
                </text>
              </Modal>
              <EscapeHint onClose={props.onClose} />
            </>
          }
        >
          <ListDialog
            title={`${title()}${data()?.truncated ? " - limited to 100 files" : ""}`}
            searchable
            items={(data()?.files ?? []).map((file) => ({
              label: `${file.status}  ${file.path}`,
              detail: file.old_path ? `from ${file.old_path}` : "",
              value: file.path,
            }))}
            onSelect={setSelected}
            onClose={props.onClose}
          />
        </Show>
      }
    >
      {(file) => (
        <>
          <Modal title={file().path} width={118} height={28} escapeCloses={false}>
            <Show
              when={!detail.loading && detail()?.patch}
              fallback={
                <text fg={theme.muted}>
                  {detail.loading ? "Loading patch..." : "No text patch available for this file."}
                </text>
              }
            >
              <DiffView
                patch={detail()?.patch ?? ""}
                path={file().path}
                expanded
                disclosure={false}
              />
            </Show>
            <Show when={!detail.loading && detail()?.truncated}>
              <text fg={theme.warning}>Patch truncated.</text>
            </Show>
          </Modal>
          <EscapeHint onClose={() => setSelected(null)} />
        </>
      )}
    </Show>
  )
}

function TimingDialog(props: { onClose: () => void }) {
  const store = useStore()
  const [timing] = createResource(() => store.latestTiming().catch(() => null))
  const phases = [
    ["total", "Total elapsed"],
    ["model", "Model and provider retries"],
    ["permission", "Waiting for permission"],
    ["tool", "Tool work"],
    ["verifier", "Verification"],
    ["checkpoint", "Checkpoints"],
    ["hook", "Hooks"],
    ["other", "Other session work"],
  ] as const
  const seconds = (ms: number) => `${(ms / 1000).toFixed(2)} s`
  return (
    <Modal title="latest task timing" width={68} height={16}>
      <Show
        when={timing()}
        fallback={
          <text>
            {timing.loading ? "Loading timing..." : "No timing recorded for this session."}
          </text>
        }
      >
        {(data) => (
          <For each={phases}>
            {([key, label]) => (
              <MetricRow label={label} value={seconds(data().durations[key] ?? 0)} />
            )}
          </For>
        )}
      </Show>
      <EscapeHint onClose={props.onClose} />
    </Modal>
  )
}

type RestorePromptProps = {
  canRestorePrompt: () => boolean
  onRestorePrompt: (text: string, attachments: MessageAttachment[]) => void
}

function PendingDialog(props: RestorePromptProps & { onClose: () => void }) {
  const store = useStore()
  const [selected, setSelected] = createSignal<string | null>(null)
  const [busy, setBusy] = createSignal(false)
  const corrections = () => store.messages().filter((message) => message.unapplied)
  const correction = () => corrections().find((entry) => entry.id === selected())
  const queued = () => store.queuedPrompts().find((entry) => entry.id === selected())
  const preview = () => {
    const item = queued()
    return (
      correction()?.content ??
      ([item?.blockedReason, item?.text].filter(Boolean).join(" · ") || "Image message")
    )
  }
  const back = () => {
    if (!busy()) setSelected(null)
  }
  const perform = async (action: string) => {
    if (busy()) return
    const note = correction()
    const item = queued()
    if (action === "edit" && !props.canRestorePrompt()) {
      store.notify("Clear or send the current draft before restoring a prompt.", "warning")
      return
    }
    setBusy(true)
    try {
      if (note) {
        if (action === "retry") {
          if (await store.sendPrompt(note.content)) {
            await store.resolveCorrection(note.id)
            props.onClose()
          }
        } else if (await store.resolveCorrection(note.id)) {
          props.onClose()
          props.onRestorePrompt(note.content, [])
        }
      } else if (item) {
        const removed = await store.removeQueuedPrompt(item.id)
        if (!removed) return
        if (action === "edit") {
          props.onClose()
          props.onRestorePrompt(removed.content, removed.attachments)
        } else {
          store.notify("Queued prompt cancelled", "info")
          setSelected(null)
        }
      }
    } finally {
      setBusy(false)
    }
  }
  return (
    <Show
      when={correction() || queued() || busy()}
      fallback={
        <ListDialog
          title="pending inputs"
          searchable
          items={[
            ...store
              .queuedPrompts()
              .filter((item) => item.id.startsWith("queued_"))
              .map((item, index) => ({
                label: `${item.blockedReason ? "Blocked" : "Queued"} ${index + 1}  ${item.text || "Image message"}`,
                detail: [
                  item.blockedReason,
                  item.text,
                  item.attachmentCount ? `${item.attachmentCount} attachment(s)` : "",
                ]
                  .filter(Boolean)
                  .join(" · "),
                value: item.id,
              })),
            ...corrections().map((item) => ({
              label: "Unapplied correction",
              detail: item.content,
              value: item.id,
            })),
          ]}
          onSelect={setSelected}
          onClose={props.onClose}
        />
      }
    >
      <ListDialog
        title={
          correction()
            ? "unapplied correction"
            : queued()?.blockedReason
              ? "blocked prompt"
              : "queued prompt"
        }
        items={[
          { label: "Return to editor", detail: preview(), value: "edit" },
          correction()
            ? { label: "Send as follow-up", detail: preview(), value: "retry" }
            : { label: "Cancel queued prompt", detail: preview(), value: "cancel" },
        ]}
        onSelect={(action) => void perform(action)}
        onClose={back}
        onDismiss={back}
      />
    </Show>
  )
}

export function DialogHost(
  props: RestorePromptProps & {
    onCommand: (name: string) => void
    onHistory: (text: string) => void
  },
) {
  const store = useStore()
  const dialog = () => store.dialog()
  const theme = useTheme()
  const close = () => store.closeDialog()

  const content = (current: Exclude<DialogState, null>) => {
    switch (current.type) {
      case "palette":
        return (
          <ListDialog
            title="commands"
            searchable
            items={commands.map((command) => ({
              label: command.name,
              detail: command.description,
              value: command.name,
            }))}
            onSelect={(value) => {
              close()
              props.onCommand(value)
            }}
            onClose={close}
          />
        )
      case "effort":
        return <EffortDialog onClose={close} />
      case "theme":
        return <ThemeDialog onClose={close} />
      case "restart":
        return <RestartDialog version={current.version} onClose={close} />
      case "update":
        return <UpdateDialog />
      case "models":
        return <ModelDialog onClose={close} />
      case "sessions":
        return <SessionDialog onClose={close} />
      case "help":
        return (
          <>
            <Modal title="help" width={82} height={24}>
              <text fg={theme.text} wrapMode="word">
                {helpText}
              </text>
            </Modal>
            <EscapeHint onClose={close} />
          </>
        )
      case "cost":
        return <CostDialog onClose={close} />
      case "timing":
        return <TimingDialog onClose={close} />
      case "pending":
        return <PendingDialog {...props} onClose={close} />
      case "find":
        return <TranscriptSearchDialog query={current.query} onClose={close} />
      case "permission-preview":
        return (
          <Modal title="permission request · read only" width={90} height={26}>
            <text fg={theme.text} wrapMode="word">
              {current.preview}
            </text>
          </Modal>
        )
      case "todos":
        return (
          <>
            <Modal title="tasks" width={64} height={24}>
              <TodoPanel embedded />
            </Modal>
            <EscapeHint onClose={close} />
          </>
        )
      case "doctor":
        return <DoctorDialog onClose={close} />
      case "evolution":
        return <EvolutionDialog onClose={close} />
      case "changes":
        return (
          <ChangesDialog
            scope={current.scope}
            checkpointId={current.checkpointId}
            onClose={close}
          />
        )
      case "checkpoints":
        return <CheckpointsDialog onClose={close} />
      case "rewind":
        return (
          <RewindDialog
            checkpoint={current.checkpoint}
            steps={current.steps}
            label={current.label}
            onClose={close}
          />
        )
      case "turns":
        return <TurnsDialog onClose={close} />
      case "connect":
        return <ConnectDialog onClose={close} />
      case "skills":
        return <SkillsDialog onClose={close} />
      case "learned":
        return <LearnedDialog onClose={close} />
      case "mcp":
        return <McpDialog onClose={close} />
      case "skill":
        return <SkillContentDialog name={current.name} onClose={close} />
      case "memory":
        return <MemoryDialog onClose={close} />
      case "history":
        return <HistoryDialog onClose={close} onPick={props.onHistory} />
      default:
        return null
    }
  }

  const rendered = createMemo(() => {
    const value = dialog()
    return value ? untrack(() => content(value)) : null
  })
  return <>{rendered()}</>
}

type ListItem = { label: string; detail?: string; value: string }

function ListDialog(props: {
  title: string
  items: ListItem[]
  onSelect: (value: string) => void
  onClose: () => void
  onDismiss?: () => void
  hint?: string
  initial?: number
  searchable?: boolean
  initialQuery?: string
  searchItems?: (query: string) => ListItem[]
  preview?: (value: string) => JSX.Element
}) {
  const theme = useTheme()
  const dimensions = useDimensions()
  const showDetail = () => dimensions().width >= 72
  const contentWidth = () => Math.max(24, Math.min(78, dimensions().width - 4) - 6)
  const labelWidth = () => Math.max(18, Math.floor(contentWidth() * 0.46))
  const detailWidth = () => Math.max(8, contentWidth() - labelWidth() - 1)
  const [selected, setSelected] = createSignal(props.initial ?? 0)
  const [query, setQuery] = createSignal(props.initialQuery ?? "")
  const rowBudget = () => Math.max(1, Math.min(10, dimensions().height - (props.preview ? 17 : 14)))
  const items = createMemo(() => {
    if (props.searchItems) return props.searchItems(query())
    if (!props.searchable || !query()) return props.items
    return props.items
      .map((item) => ({ item, score: fuzzyMatch(query(), `${item.label} ${item.value}`) }))
      .filter((entry) => entry.score >= 0)
      .sort((left, right) => right.score - left.score)
      .map((entry) => entry.item)
  })
  const updateQuery = (text: string) => {
    setQuery(text)
    setSelected(0)
  }
  const last = () => Math.max(0, items().length - 1)
  createEffect(() => {
    const total = items().length
    setSelected((value) => Math.min(value, Math.max(0, total - 1)))
  })
  const visible = createMemo(() => {
    const start = windowStart(selected(), items().length, rowBudget())
    return items()
      .slice(start, start + rowBudget())
      .map((item, index) => ({ ...item, index: start + index }))
  })
  useKeyboard((key) => {
    if (key.name === "escape") {
      key.stopPropagation()
      props.onClose()
    } else if (key.name === "up" || (!props.searchable && key.name === "k")) {
      key.stopPropagation()
      setSelected((value) => Math.max(0, value - 1))
    } else if (key.name === "down" || (!props.searchable && key.name === "j")) {
      key.stopPropagation()
      setSelected((value) => Math.min(last(), value + 1))
    } else if (key.name === "pageup") {
      key.stopPropagation()
      setSelected((value) => Math.max(0, value - 10))
    } else if (key.name === "pagedown") {
      key.stopPropagation()
      setSelected((value) => Math.min(last(), value + 10))
    } else if (key.name === "home") {
      key.stopPropagation()
      setSelected(0)
    } else if (key.name === "end") {
      key.stopPropagation()
      setSelected(last())
    } else if (key.name === "return" && items().length > 0) {
      key.stopPropagation()
      const item = items()[selected()]
      if (item) props.onSelect(item.value)
    } else if (props.searchable && (key.name === "backspace" || key.name === "delete")) {
      key.stopPropagation()
      updateQuery(query().slice(0, -1))
    } else if (props.searchable && !key.ctrl && !key.meta) {
      const text = key.sequence ?? ""
      if (
        text &&
        Array.from(text).every(
          (character) => character.charCodeAt(0) >= 32 && character.charCodeAt(0) !== 127,
        )
      ) {
        key.stopPropagation()
        updateQuery(query() + text)
      }
    }
  })
  usePaste((event) => {
    if (!props.searchable || event.metadata?.kind === "binary") return
    event.preventDefault()
    event.stopPropagation()
    updateQuery(query() + new TextDecoder().decode(event.bytes).replace(/[\r\n]/g, " "))
  })
  return (
    <Modal
      title={props.title}
      width={78}
      height={Math.max(10, Math.min(items().length, rowBudget()) + (props.preview ? 13 : 10))}
      scrollable={false}
      escapeCloses={props.onDismiss ? false : undefined}
      onDismiss={props.onDismiss}
    >
      <box flexDirection="column" flexGrow={1} minHeight={0} width="100%">
        <Show when={props.searchable} fallback={null}>
          <text fg={theme.text} bg={theme.panelAlt} paddingX={1}>{`filter: ${query()}`}</text>
        </Show>
        <Show when={items().length === 0} fallback={null}>
          <text fg={theme.dim}>
            {props.searchItems && !query().trim()
              ? "Type to search this transcript."
              : "No matching entries."}
          </text>
        </Show>
        <For each={visible()}>
          {(item) => (
            <box
              flexDirection="row"
              gap={1}
              height={1}
              backgroundColor={item.index === selected() ? theme.selection : undefined}
              paddingX={1}
              onMouseDown={() => props.onSelect(item.value)}
            >
              <box width={labelWidth()} flexShrink={0}>
                <text fg={item.index === selected() ? theme.accent : theme.text} wrapMode="none">
                  {`${item.index === selected() ? "› " : "  "}${clipCells(item.label, Math.max(1, labelWidth() - 2))}`}
                </text>
              </box>
              <Show when={item.detail && showDetail()}>
                <text fg={theme.muted} wrapMode="none">
                  {clipCells(item.detail ?? "", detailWidth())}
                </text>
              </Show>
            </box>
          )}
        </For>
        <box flexGrow={1} />
        <Show when={props.preview && items()[selected()]}>
          {(item) => props.preview?.(item().value)}
        </Show>
        <Show when={items()[selected()]?.detail}>
          <text fg={theme.muted} wrapMode="word">
            {clipCells(items()[selected()]?.detail ?? "", contentWidth() * 2 - 2)}
          </text>
        </Show>
        <text fg={theme.muted} wrapMode="word">
          {`${props.searchItems && items().length ? `${selected() + 1}/${items().length} · ` : props.searchable ? "type to filter · " : ""}${props.hint ? `${props.hint} · ` : ""}↑/↓ move · PgUp/PgDn jump · Enter select · Esc close`}
        </text>
      </box>
    </Modal>
  )
}

function UpdateDialog() {
  const theme = useTheme()
  return (
    <Modal title="update siwah" width={62} height={9} escapeCloses={false}>
      <box flexDirection="column" gap={1}>
        <box flexDirection="row" gap={1}>
          <Pulse active color={theme.accentAlt} />
          <text fg={theme.text}>Checking, fetching, and rebuilding siwah…</text>
        </box>
        <text fg={theme.dim} wrapMode="word">
          Keep this window open. The restart prompt appears when the update finishes.
        </text>
      </box>
    </Modal>
  )
}

function RestartDialog(props: { version?: string; onClose: () => void }) {
  const store = useStore()
  const theme = useTheme()
  const renderer = useRenderer()
  useKeyboard((key) => {
    if (key.name === "escape") {
      key.stopPropagation()
      store.notify("Update installed; restart later with `siwah`", "info")
      props.onClose()
      return
    }
    if (key.name === "return") {
      key.stopPropagation()
      renderer.destroy()
      process.exit(75)
    }
  })
  return (
    <Modal title="restart siwah" width={64} height={10} escapeCloses={false}>
      <box flexDirection="column" gap={1}>
        <text fg={theme.success}>{`siwah ${props.version ?? "latest"} is installed.`}</text>
        <text fg={theme.text} wrapMode="word">
          The TUI and daemon need to restart to load the new code.
        </text>
        <text fg={theme.dim}>Enter restart now · Esc keep working, restart manually later</text>
      </box>
    </Modal>
  )
}

function ThemeDialog(props: { onClose: () => void }) {
  const store = useStore()
  const items = () =>
    themeNames.map((name) => ({
      label: name,
      detail: name === store.theme().name ? "current theme" : "select",
      value: name,
    }))
  return (
    <ListDialog
      title="theme"
      items={items()}
      initial={Math.max(0, themeNames.indexOf(store.theme().name))}
      preview={(value) => <ThemePreview name={value} />}
      onSelect={(value) => {
        store.setTheme(value as ThemeName)
        store.notify(`Theme: ${value}`, "info")
        props.onClose()
      }}
      onClose={props.onClose}
    />
  )
}

function ThemePreview(props: { name: string }) {
  const theme = createMemo(() => resolveTheme(props.name))
  return (
    <box backgroundColor={overlayBackground(theme(), "background")} paddingX={1}>
      <text fg={theme().text} wrapMode="none">
        Answer text
      </text>
      <text fg={theme().accent} bg={theme().panel} wrapMode="none">
        › Focused prompt
      </text>
      <box flexDirection="row" gap={2}>
        <text fg={theme().warning} wrapMode="none">
          ! Permission
        </text>
        <text fg={theme().success} bg={theme().diffAddedBg} wrapMode="none">
          + Added
        </text>
        <text fg={theme().error} wrapMode="none">
          × Failed
        </text>
      </box>
    </box>
  )
}

function EffortDialog(props: { onClose: () => void }) {
  const store = useStore()
  const options = () => store.effortOptions()
  const current = () => store.effort() ?? "off"
  const initial = () => Math.max(0, options().indexOf(current() as Effort))
  const items = () =>
    options().map((value) => ({
      label: value === "off" ? "provider default" : value,
      detail:
        value === "off"
          ? "let the provider choose"
          : `${store.currentModelInfo()?.name ?? store.currentModel()}`,
      value,
    }))
  return (
    <ListDialog
      title="reasoning effort"
      items={items()}
      initial={initial()}
      hint={`current: ${current()}`}
      onSelect={(value) => {
        props.onClose()
        void store.setEffort(value as Effort)
      }}
      onClose={props.onClose}
    />
  )
}

function ModelDialog(props: { onClose: () => void }) {
  const store = useStore()
  const items = () => {
    const catalog = store.models()
    if (!catalog) return []
    return (catalog.providers ?? []).flatMap((provider) => {
      if (provider.error) {
        return [
          {
            label: `${provider.provider} · unavailable`,
            detail: provider.error,
            value: `error:${provider.provider}`,
          },
        ]
      }
      return (provider.models ?? []).map((model) => {
        const privacy = model.privacy ?? {}
        const training =
          privacy.training === true
            ? "trains"
            : privacy.training === false
              ? "no-train"
              : "privacy?"
        const retention =
          typeof privacy.retention_days === "number" ? ` ${privacy.retention_days}d` : ""
        const pricing = model.pricing && Object.keys(model.pricing).length > 0 ? "priced" : "price?"
        const tokens = (value: number | null | undefined) =>
          value === null || value === undefined ? "?" : formatTokens(value)
        return {
          label: model.id,
          detail: `${provider.provider} · ctx ${tokens(model.context_window)} · out ${tokens(model.max_output)} · tools ${model.supports_tools ? "yes" : "?"} · ${training}${retention} · ${pricing}`,
          value: `${provider.provider}/${model.id}`,
        }
      })
    })
  }
  return (
    <ListDialog
      title="models"
      searchable
      items={items()}
      onSelect={(value) => {
        if (value.startsWith("error:")) return
        void store.selectModel(value)
        props.onClose()
      }}
      onClose={props.onClose}
    />
  )
}

function SessionDialog(props: { onClose: () => void }) {
  const store = useStore()
  const [showAll, setShowAll] = createSignal(false)
  const sessions = () => (showAll() ? store.sessions() : store.projectSessions())
  const waiting = (session: Session) =>
    session.id === store.session()?.id
      ? Boolean(store.permission())
      : store.sessionRunning(session) && session.metadata?.waiting_permission === true
  const items = () => [
    { label: "+ new session", detail: "start in the current project", value: "__new__" },
    ...sessions().map((session) => ({
      label: session.title ?? session.id,
      detail: [
        waiting(session)
          ? "Waiting for permission"
          : store.sessionRunning(session)
            ? "Running"
            : "",
        relativeTime(session.updated_at),
        session.model,
        projectName(session.cwd),
      ]
        .filter(Boolean)
        .join(" · "),
      value: session.id,
    })),
  ]
  useKeyboard((key) => {
    if (key.name === "tab") {
      key.stopPropagation()
      setShowAll((value) => !value)
    }
  })
  const title = () =>
    showAll() ? "sessions · all projects" : `sessions · ${projectName(store.workspacePath())}`
  return (
    <ListDialog
      title={title()}
      searchable
      items={items()}
      hint="Tab all/current"
      onSelect={(value) => {
        props.onClose()
        if (value === "__new__") void store.createSession()
        else void store.openSession(value)
      }}
      onClose={props.onClose}
    />
  )
}

function CostDialog(props: { onClose: () => void }) {
  const store = useStore()
  const theme = useTheme()
  const usage = () => store.usage()
  const amount = (value?: number, reportedCount?: number | null) => {
    const reported = reportedCount ?? 0
    if (reported <= 0 && !value) return "—"
    const partial = reported > 0 && reported < (usage()?.steps ?? 0)
    return `$${(value ?? 0).toFixed(6)}${partial ? " (partial)" : ""}`
  }
  useKeyboard((key) => {
    if (key.name === "escape") {
      key.stopPropagation()
      props.onClose()
    }
  })
  return (
    <Modal title="cost" width={66} height={16}>
      <box flexDirection="column" width="100%">
        <Notice>
          <MetricRow
            label="Provider-reported cost"
            value={amount(usage()?.cost, usage()?.cost_steps)}
            emphasis
          />
          <MetricRow
            label="Session allowance"
            value={amount(usage()?.allowance, usage()?.allowance_steps)}
          />
        </Notice>
        <SectionLabel>Token usage</SectionLabel>
        <MetricRow label="input" value={usage()?.input_tokens ?? 0} />
        <MetricRow label="output" value={usage()?.output_tokens ?? 0} />
        <MetricRow label="reasoning" value={usage()?.reasoning_tokens ?? 0} />
        <MetricRow label="cache read" value={usage()?.cache_read_tokens ?? 0} />
        <MetricRow label="steps" value={usage()?.steps ?? 0} />
        <Show when={store.usingChatGPTPlan()}>
          <text fg={theme.accent} wrapMode="word">
            Manage usage · https://chatgpt.com/settings/usage
          </text>
        </Show>
        <text fg={theme.dim}>Esc closes</text>
      </box>
    </Modal>
  )
}

function DoctorDialog(props: { onClose: () => void }) {
  const store = useStore()
  const theme = useTheme()
  useKeyboard((key) => {
    if (key.name === "escape") {
      key.stopPropagation()
      props.onClose()
    }
  })
  return (
    <Modal title="doctor" width={76} height={18}>
      <box flexDirection="column">
        <text
          fg={theme.text}
        >{`siwah ${store.meta()?.version ?? "?"} · protocol ${store.meta()?.protocol ?? "?"}`}</text>
        <SectionLabel>Runtime</SectionLabel>
        <MetricRow label="connection" value={store.connection()} />
        <MetricRow label="model" value={store.currentModel()} />
        <MetricRow label="mode" value={store.currentMode()} />
        <SectionLabel>Provider credentials</SectionLabel>
        <For each={store.auth()?.providers ?? []}>
          {(provider) => (
            <text fg={theme.muted}>
              {`${provider.provider}: env=${provider.env_set ? "set" : "unset"} file=${provider.file_set ? "set" : "unset"}`}
            </text>
          )}
        </For>
        <text fg={theme.dim}>Esc closes</text>
      </box>
    </Modal>
  )
}

function evolutionRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" ? (value as Record<string, unknown>) : {}
}

function evolutionText(value: unknown): string {
  return typeof value === "string" ? value : ""
}

function summarizeEvolution(event: unknown): string {
  const row = evolutionRecord(event)
  const kind = evolutionText(row.event)
  const data = evolutionRecord(row.data)
  const candidate = evolutionText(data.candidate)
  const tag = evolutionText(data.tag)
  const errors = Array.isArray(data.errors) ? data.errors.map(String).join("; ") : ""
  let text = kind || "evolution event"
  if (kind === "evolve.level2.rejected") {
    text = `Level 2 rejected · ${candidate || "candidate"}: ${errors || "gate failed"}`
  } else if (kind === "evolve.rejected") {
    text = `Rejected · ${evolutionText(data.stage) || "proposal"}: ${errors || "no candidate"}`
  } else if (kind === "evolve.level2.promoted") {
    text = `Level 2 promoted · ${tag || candidate || "candidate"}`
  } else if (kind === "evolve.promoted") {
    text = `Promoted · ${tag || candidate || "candidate"}`
  } else if (kind === "evolve.canary") {
    text = `Canary · ${tag || candidate || "candidate"}`
  } else if (kind === "evolve.level2.rollback") {
    text = `Level 2 rolled back · ${tag || candidate || "candidate"}`
  } else if (kind === "evolve.rollback_manual") {
    text = `Manual rollback · ${tag || "previous revision"} ${data.ok === false ? "failed" : "ok"}`
  } else if (kind === "evolve.paused" || kind === "evolve.resumed") {
    text = `${kind === "evolve.paused" ? "Paused" : "Resumed"} · ${evolutionText(data.by) || "manual"}`
  }
  return ellipsis(text, 110)
}

function groupedEvolution(events: unknown[]): { text: string; count: number }[] {
  const grouped: { text: string; count: number }[] = []
  for (const event of events) {
    const text = summarizeEvolution(event)
    const last = grouped[grouped.length - 1]
    if (last && last.text === text) last.count += 1
    else grouped.push({ text, count: 1 })
  }
  return grouped.slice(-8)
}

function EvolutionDialog(props: { onClose: () => void }) {
  const store = useStore()
  const theme = useTheme()
  useKeyboard((key) => {
    if (key.name === "escape") {
      key.stopPropagation()
      props.onClose()
    }
    if (key.name === "r") {
      key.stopPropagation()
      void store.rollbackEvolution()
    }
    if (key.name === "p") {
      key.stopPropagation()
      void store.pauseEvolution(!store.evolution()?.paused)
    }
  })
  const events = () => store.evolution()?.events ?? []
  const status = () =>
    store.evolution()?.paused ? "paused" : (store.evolution()?.status ?? "idle")
  return (
    <Modal title="evolve" width={80} height={20}>
      <box flexDirection="column" gap={1}>
        <Notice>
          <text fg={theme.text}>
            {`tag ${store.evolution()?.tag ?? "evo-0"} · ${status()} · auto evolution`}
          </text>
        </Notice>
        <text fg={theme.dim}>
          Propose → gate → canary → promote. r rolls back the harness; p pauses or resumes.
        </text>
        <Show when={events().length === 0} fallback={null}>
          <text fg={theme.dim}>No evolution cycles yet.</text>
        </Show>
        <For each={groupedEvolution(events())}>
          {(event) => (
            <box border={["left"]} borderColor={theme.border} paddingLeft={1}>
              <text fg={theme.muted} wrapMode="word">
                {event.count > 1 ? `${event.text} ×${event.count}` : event.text}
              </text>
            </box>
          )}
        </For>
        <box flexGrow={1} />
        <text fg={theme.dim}>r rollback · p pause/resume · Esc closes</text>
      </box>
    </Modal>
  )
}

function SimpleDialog(props: { title: string; lines: string[]; onClose: () => void }) {
  const theme = useTheme()
  useKeyboard((key) => {
    if (key.name === "escape") {
      key.stopPropagation()
      props.onClose()
    }
  })
  return (
    <Modal title={props.title} width={68} height={12}>
      <box flexDirection="column">
        <For each={props.lines}>{(line) => <text fg={theme.text}>{line}</text>}</For>
        <text fg={theme.dim}>Esc closes</text>
      </box>
    </Modal>
  )
}

export function turnAnchors(messages: UiMessage[]) {
  return messages
    .filter((message) => message.role === "user" && !message.unapplied)
    .map((message, turn) => ({
      label: `Turn ${turn + 1}: ${ellipsis(message.content.split("\n")[0] ?? "", 58)}`,
      detail: relativeTime(message.createdAt),
      value: message.id,
    }))
}

function TurnsDialog(props: { onClose: () => void }) {
  const store = useStore()
  return (
    <ListDialog
      title="turns"
      items={turnAnchors(store.messages())}
      onSelect={(id) => {
        store.focusMessage(id)
        props.onClose()
      }}
      onClose={props.onClose}
    />
  )
}

function TranscriptSearchDialog(props: { query: string; onClose: () => void }) {
  const store = useStore()
  const search = (query: string): ListItem[] => {
    const needle = query.trim().toLowerCase()
    if (!needle) return []
    return store.messages().flatMap((message) => {
      const sources = [
        { label: message.role, content: message.content },
        ...message.diffs.map((diff) => ({ label: "diff", content: `${diff.path}\n${diff.patch}` })),
        ...message.toolCalls.flatMap((tool) => [
          { label: tool.name, content: `${tool.name} ${tool.summary}` },
          { label: tool.name, content: tool.error ?? "" },
          { label: tool.name, content: tool.output },
        ]),
      ]
      for (const source of sources) {
        const index = source.content.toLowerCase().indexOf(needle)
        if (index < 0) continue
        const excerpt = source.content
          .slice(Math.max(0, index - 12), index + needle.length + 76)
          .replace(/\s+/g, " ")
          .trim()
        return [{ label: `${source.label} · ${excerpt}`, detail: excerpt, value: message.id }]
      }
      return []
    })
  }
  return (
    <ListDialog
      title="find in session"
      searchable
      initialQuery={props.query}
      searchItems={search}
      items={[]}
      onSelect={(id) => {
        store.focusMessage(id)
        props.onClose()
      }}
      onClose={props.onClose}
    />
  )
}

function HistoryDialog(props: { onClose: () => void; onPick: (text: string) => void }) {
  const store = useStore()
  const items = () =>
    store.history().map((text, index) => ({
      label: text.split("\n")[0] ?? "",
      detail: `prompt ${index + 1}`,
      value: text,
    }))
  return (
    <ListDialog
      title="prompt history"
      searchable
      items={items()}
      onSelect={(text) => {
        props.onClose()
        props.onPick(text)
      }}
      onClose={props.onClose}
    />
  )
}

function ConnectDialog(props: { onClose: () => void }) {
  const store = useStore()
  const theme = useTheme()
  const providers = () => store.auth()?.providers ?? []
  const [stage, setStage] = createSignal<"provider" | "key" | "browser" | "account" | "confirmed">(
    "provider",
  )
  const [browserFlow, setBrowserFlow] = createSignal<{ flow_id: string; url: string } | null>(null)
  const [accounts, setAccounts] = createSignal<ChatGPTAccount[]>([])
  const [accountIndex, setAccountIndex] = createSignal(0)
  const account = () => accounts()[accountIndex() - 1]
  const accountStart = () => windowStart(accountIndex(), accounts().length + 1, 7)
  const [selected, setSelected] = createSignal(0)
  const [secret, setSecret] = createSignal("")
  const [error, setError] = createSignal<string | null>(null)
  const [busy, setBusy] = createSignal(false)
  const current = () => providers()[selected()]
  const chatGPT = () => current()?.browser_method === "chatgpt"
  const last = () => Math.max(0, providers().length - 1)
  const start = () => windowStart(selected(), providers().length, 8)
  const visible = () => providers().slice(start(), start() + 8)
  const providerState = (p: {
    scheme?: string
    env_set: boolean
    file_set: boolean
    env?: string | null
    browser_login?: boolean | null
    browser_method?: string | null
  }) => {
    if (p.scheme === "none") return "no key required"
    if (p.env_set) return `env ${p.env ?? "set"}`
    if (p.browser_method === "chatgpt") {
      return p.file_set ? "ChatGPT plan connected" : "Continue with ChatGPT"
    }
    if (p.file_set) return "auth file saved"
    if (p.browser_login) return "browser sign-in"
    if (p.env) return `needs ${p.env}`
    return "API key required"
  }
  const bullets = () => maskedSecret(secret())
  const createDefaults = async () => {
    if (busy()) return
    setBusy(true)
    setError(null)
    try {
      await store.ensureProviders()
      if ((store.auth()?.providers ?? []).length === 0) {
        setError("No providers were created. Restart siwah once and reopen /connect.")
      } else {
        store.notify("Provider presets available", "success")
      }
    } catch (err) {
      setError(
        `${err instanceof Error ? err.message : String(err)}. Restart siwah once if this daemon is older.`,
      )
    } finally {
      setBusy(false)
    }
  }
  const startBrowser = async (provider: string, clientId?: string) => {
    setBusy(true)
    setError(null)
    try {
      const flow = await store.startBrowserLogin(provider, clientId)
      if (!flow.flow_id || !flow.url) throw new Error("Browser sign-in is unavailable.")
      setBrowserFlow({ flow_id: flow.flow_id, url: flow.url })
      setStage("browser")
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }
  const chooseProvider = async (apiKey = false) => {
    const provider = current()
    if (!provider) return
    if (provider.env_set) {
      store.notify(
        `${provider.provider} already uses ${provider.env ?? "its environment key"}`,
        "info",
      )
      props.onClose()
      return
    }
    if (provider.scheme === "none") {
      store.notify(`${provider.provider} does not require a key`, "info")
      props.onClose()
      return
    }
    setError(null)
    if (provider.browser_method === "chatgpt") {
      setBusy(true)
      try {
        setAccounts(await store.chatGPTAccounts())
        setAccountIndex(0)
        setStage("account")
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err))
      } finally {
        setBusy(false)
      }
      return
    }
    if (!provider.browser_login || apiKey) {
      setStage("key")
      return
    }
    await startBrowser(provider.provider)
  }
  const signOutChatGPT = async () => {
    const selectedAccount = account()
    if (!selectedAccount?.connected) return
    setBusy(true)
    setError(null)
    try {
      const revoked = await store.chatGPTSignOut(selectedAccount.client_id)
      setAccounts(await store.chatGPTAccounts())
      store.notify(
        revoked
          ? "Signed out of ChatGPT"
          : "Signed out locally. Check connected apps in ChatGPT Settings.",
        revoked ? "success" : "warning",
      )
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }
  const submit = async () => {
    const provider = current()
    if (!provider) {
      setError("Select a configured provider first.")
      return
    }
    if (!chatGPT() && !secret().trim()) {
      setError(stage() === "browser" ? "The sign-in code is empty." : "The API key is empty.")
      return
    }
    if (busy()) return
    setBusy(true)
    setError(null)
    try {
      const flow = browserFlow()
      if (stage() === "browser" && flow) {
        await store.finishBrowserLogin(
          provider.provider,
          flow.flow_id,
          chatGPT() ? undefined : secret().trim(),
        )
      } else {
        await store.login(provider.provider, secret().trim())
      }
      if (chatGPT() && accounts().length === 0) {
        setStage("confirmed")
      } else {
        store.notify(`Connected ${provider.provider}`, "success")
        props.onClose()
      }
    } catch (err) {
      if (stage() === "browser" && !(err instanceof ApiError && err.code === "auth_pending")) {
        setStage(chatGPT() ? "account" : "provider")
        setBrowserFlow(null)
        setSecret("")
      }
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }
  usePaste((event) => {
    if (stage() === "provider" || stage() === "account" || chatGPT() || busy()) return
    event.preventDefault()
    event.stopPropagation()
    if (event.metadata?.kind === "binary") return
    setSecret((value) => value + new TextDecoder().decode(event.bytes).replace(/[\r\n]/g, ""))
  })
  useKeyboard((key) => {
    if (busy()) return
    if (stage() === "confirmed") {
      if (key.name === "return" || key.name === "escape") {
        key.stopPropagation()
        props.onClose()
      }
      return
    }
    if (key.name === "escape") {
      key.stopPropagation()
      if (stage() !== "provider") {
        setStage(stage() === "browser" && chatGPT() ? "account" : "provider")
        setBrowserFlow(null)
        setSecret("")
        setError(null)
      } else {
        props.onClose()
      }
      return
    }
    if (stage() === "account") {
      if (key.name === "up" || key.name === "down") {
        const direction = key.name === "up" ? -1 : 1
        key.stopPropagation()
        setAccountIndex((value) => Math.max(0, Math.min(accounts().length, value + direction)))
      } else if (key.name === "return") {
        key.stopPropagation()
        const provider = current()
        if (provider) void startBrowser(provider.provider, account()?.client_id)
      } else if (key.name === "d") {
        key.stopPropagation()
        void signOutChatGPT()
      }
      return
    }
    if (stage() === "provider") {
      if (key.name === "up" || key.name === "k") {
        key.stopPropagation()
        setSelected((value) => Math.max(0, value - 1))
      } else if (key.name === "down" || key.name === "j") {
        key.stopPropagation()
        setSelected((value) => Math.min(last(), value + 1))
      } else if (key.name === "pageup") {
        key.stopPropagation()
        setSelected((value) => Math.max(0, value - 6))
      } else if (key.name === "pagedown") {
        key.stopPropagation()
        setSelected((value) => Math.min(last(), value + 6))
      } else if (key.name === "home") {
        key.stopPropagation()
        setSelected(0)
      } else if (key.name === "end") {
        key.stopPropagation()
        setSelected(last())
      } else if (key.name === "a") {
        key.stopPropagation()
        void createDefaults()
      } else if (key.name === "return" || key.name === "p") {
        key.stopPropagation()
        if (providers().length === 0) void createDefaults()
        else void chooseProvider(key.name === "p")
      }
      return
    }
    if (key.name === "return") {
      key.stopPropagation()
      void submit()
      return
    }
    if (chatGPT()) return
    if (key.name === "backspace" || key.name === "delete") {
      key.stopPropagation()
      setSecret((value) => value.slice(0, -1))
      return
    }
    if (key.ctrl || key.meta) return
    const text = key.sequence?.replace(/[\r\n]/g, "") ?? ""
    if (text.length > 0 && !text.startsWith("\x1b")) {
      key.stopPropagation()
      setSecret((value) => value + text)
    }
  })
  const home = process.env.SIWAH_HOME ?? join(homedir(), ".siwah")
  const configFile = process.env.SIWAH_CONFIG ?? join(home, "harness", "config.toml")
  return (
    <Modal title="connect provider" width={80} height={22} scrollable={false} escapeCloses={false}>
      <box flexDirection="column" flexGrow={1}>
        <Show
          when={providers().length > 0}
          fallback={
            <box flexDirection="column">
              <text fg={theme.warning}>No providers are configured.</text>
              <text fg={theme.text} wrapMode="word">
                Press Enter to add the built-in provider presets to config.toml.
              </text>
              <text fg={theme.dim}>{`Config: ${configFile}`}</text>
            </box>
          }
        >
          <Show
            when={stage() === "provider"}
            fallback={
              <Show
                when={stage() !== "confirmed"}
                fallback={
                  <box flexDirection="column">
                    <SectionLabel>You're using your ChatGPT plan</SectionLabel>
                    <text fg={theme.text} wrapMode="word">
                      Eligible requests use your ChatGPT plan. Manage usage in /cost or at
                      https://chatgpt.com/settings/usage.
                    </text>
                    <text fg={theme.accent} onMouseDown={props.onClose}>
                      Got it
                    </text>
                  </box>
                }
              >
                <Show
                  when={stage() !== "account"}
                  fallback={
                    <box flexDirection="column">
                      <SectionLabel>ChatGPT accounts</SectionLabel>
                      <For each={[null, ...accounts()].slice(accountStart(), accountStart() + 7)}>
                        {(item, index) => {
                          const row = () => accountStart() + index()
                          return (
                            <text fg={row() === accountIndex() ? theme.accent : theme.text}>
                              {`${row() === accountIndex() ? "› " : "  "}${item?.label ?? "Add a ChatGPT account"}${item?.active ? " · active" : item?.connected ? "" : item ? " · signed out" : ""}`}
                            </text>
                          )
                        }}
                      </For>
                      <text fg={theme.dim} wrapMode="word">
                        Enter signs in · D signs out · Esc goes back
                      </text>
                    </box>
                  }
                >
                  <box flexDirection="column">
                    <SectionLabel>{`Provider: ${current()?.provider ?? ""}`}</SectionLabel>
                    <Show
                      when={stage() === "browser"}
                      fallback={
                        <text fg={theme.dim}>
                          {current()?.env
                            ? `Credential env: ${current()?.env}`
                            : "Enter your provider API key."}
                        </text>
                      }
                    >
                      <text fg={theme.dim} wrapMode="word">
                        {chatGPT()
                          ? "Finish signing in with ChatGPT in your browser, then press Enter."
                          : "Sign in with OpenRouter in your browser, then paste the one-time code here."}
                      </text>
                      <text fg={theme.accent} wrapMode="char">
                        {browserFlow()?.url}
                      </text>
                    </Show>
                    <Show when={!chatGPT()}>
                      <box backgroundColor={theme.panelAlt} paddingX={1} marginY={1}>
                        <text
                          fg={theme.text}
                        >{`${stage() === "browser" ? "Code" : "Key"}: ${bullets()}`}</text>
                      </box>
                      <text fg={theme.dim}>Input is hidden. Press Enter to submit.</text>
                    </Show>
                  </box>
                </Show>
              </Show>
            }
          >
            <text fg={theme.dim}>
              Select a provider from the daemon config. Names and keys come from config.toml.
            </text>
            <For each={visible()}>
              {(provider, index) => {
                const row = () => start() + index()
                return (
                  <box
                    flexDirection="row"
                    justifyContent="space-between"
                    backgroundColor={row() === selected() ? theme.selection : undefined}
                    paddingX={1}
                    onMouseDown={() => {
                      setSelected(row())
                      void chooseProvider()
                    }}
                  >
                    <text fg={row() === selected() ? theme.accent : theme.text}>
                      {`${row() === selected() ? "› " : "  "}${provider.provider}`}
                    </text>
                    <text fg={theme.dim}>{providerState(provider)}</text>
                  </box>
                )
              }}
            </For>
            <text fg={theme.dim}>↑/↓ move · Enter connect · P use API key</text>
          </Show>
        </Show>
        <Show when={error()}>
          <text fg={theme.error} wrapMode="word">
            {error()}
          </text>
        </Show>
        <box flexGrow={1} />
        <text fg={theme.dim}>
          {busy()
            ? providers().length === 0
              ? "Creating default providers…"
              : "Connecting…"
            : stage() === "confirmed"
              ? "Enter continues"
              : stage() === "account"
                ? "↑/↓ select an account"
                : stage() !== "provider"
                  ? "Enter submits · Esc goes back"
                  : providers().length === 0
                    ? "Enter creates defaults · Esc closes"
                    : "A add provider presets · Esc closes"}
        </text>
      </box>
    </Modal>
  )
}

function McpDialog(props: { onClose: () => void }) {
  const store = useStore()
  const items = () =>
    store.mcpServers().map((server) => ({
      label: server.name,
      detail: [
        server.status,
        server.tools?.length ? `${server.tools.length} tools` : "",
        server.latency_ms !== null && server.latency_ms !== undefined
          ? `${server.latency_ms} ms`
          : "",
        server.error ?? "",
      ]
        .filter(Boolean)
        .join(" · "),
      value: server.name,
    }))
  return (
    <Show
      when={items().length > 0}
      fallback={
        <SimpleDialog
          title="mcp servers"
          lines={[
            "No MCP servers configured.",
            "Add one with `siwah mcp add <name> --command ...` or `--url ...`.",
          ]}
          onClose={props.onClose}
        />
      }
    >
      <ListDialog
        title="mcp servers"
        items={items()}
        hint="Enter reconnects the selected server"
        onSelect={(name) => void store.restartMcp(name)}
        onClose={props.onClose}
      />
    </Show>
  )
}

function SkillsDialog(props: { onClose: () => void }) {
  const store = useStore()
  const theme = useTheme()
  const [skills] = createResource(() => store.listSkills())
  const items = () =>
    (skills()?.skills ?? []).map((skill) => ({
      label: skill.name,
      detail: skill.description,
      value: skill.name,
    }))
  return (
    <Show
      when={skills()}
      fallback={
        <Modal title="skills" width={78} height={12}>
          <text fg={theme.dim}>Loading skills…</text>
        </Modal>
      }
    >
      <Show
        when={items().length > 0}
        fallback={
          <SimpleDialog
            title="skills"
            lines={[
              "No skills yet.",
              "The agent can create one with skill_manage; it appears in the next session.",
            ]}
            onClose={props.onClose}
          />
        }
      >
        <ListDialog
          title="skills"
          items={items()}
          onSelect={(name) => store.openDialog({ type: "skill", name })}
          onClose={props.onClose}
        />
      </Show>
    </Show>
  )
}

function LearnedDialog(props: { onClose: () => void }) {
  const store = useStore()
  const theme = useTheme()
  const [graph] = createResource(() => store.learning())
  useKeyboard((key) => {
    if (key.name === "escape") {
      key.stopPropagation()
      props.onClose()
    }
  })
  return (
    <Modal title="learned" width={88} height={28}>
      <box flexDirection="column" flexGrow={1} gap={1}>
        <Show when={graph()} fallback={<text fg={theme.dim}>Loading learning state…</text>}>
          {(value) => (
            <>
              <Notice>
                <text fg={theme.accent}>
                  {`${value().skills?.length ?? 0} skills · ${value().lessons?.length ?? 0} lessons · ${value().evolution_tag ?? "evo-0"} · ${value().evolution_status ?? "idle"}`}
                </text>
              </Notice>
              <SectionLabel>skills</SectionLabel>
              <For
                each={(value().skills ?? []).slice(0, 10)}
                fallback={<text fg={theme.dim}>No skills yet.</text>}
              >
                {(skill) => (
                  <text fg={skill.state === "archived" ? theme.dim : theme.muted} wrapMode="word">
                    {`${skill.name} [${skill.state}] · used ${skill.use_count}× · ${skill.description}`}
                  </text>
                )}
              </For>
              <SectionLabel>lessons</SectionLabel>
              <For
                each={(value().lessons ?? []).slice(0, 8)}
                fallback={<text fg={theme.dim}>No lessons yet.</text>}
              >
                {(lesson) => (
                  <text fg={theme.muted} wrapMode="word">
                    {`[${lesson.confidence}] ${lesson.cue}: ${lesson.lesson}`}
                  </text>
                )}
              </For>
            </>
          )}
        </Show>
        <box flexGrow={1} />
        <text fg={theme.dim}>Esc closes</text>
      </box>
    </Modal>
  )
}

function SkillContentDialog(props: { name: string; onClose: () => void }) {
  const store = useStore()
  const theme = useTheme()
  const [skill] = createResource(() => store.skill(props.name))
  useKeyboard((key) => {
    if (key.name === "escape") {
      key.stopPropagation()
      props.onClose()
    }
  })
  return (
    <Modal title={`skill · ${props.name}`} width={90} height={26}>
      <box flexDirection="column" flexGrow={1}>
        <Show when={skill()} fallback={<text fg={theme.dim}>Loading skill…</text>}>
          {(value) => (
            <>
              <Notice>
                <text fg={theme.accent}>{value().description}</text>
              </Notice>
              <text fg={theme.text} wrapMode="word">
                {value().content}
              </text>
            </>
          )}
        </Show>
        <box flexGrow={1} />
        <text fg={theme.dim}>Esc closes</text>
      </box>
    </Modal>
  )
}

function CheckpointsDialog(props: { onClose: () => void }) {
  const store = useStore()
  const items = () =>
    store.checkpoints().map((checkpoint) => ({
      label: checkpoint.label ?? checkpoint.id,
      detail: checkpoint.created_at,
      value: checkpoint.id,
    }))
  return (
    <Show
      when={items().length > 0}
      fallback={
        <SimpleDialog
          title="checkpoints"
          lines={[
            "No checkpoints yet.",
            "A checkpoint is created automatically before the first step and after file changes.",
          ]}
          onClose={props.onClose}
        />
      }
    >
      <ListDialog
        title="checkpoints"
        items={items()}
        onSelect={(id) => {
          const checkpoint = store.checkpoints().find((item) => item.id === id)
          store.openDialog({ type: "rewind", checkpoint })
        }}
        onClose={props.onClose}
      />
    </Show>
  )
}

function RewindDialog(props: {
  checkpoint?: Checkpoint
  steps?: number
  label?: "undo" | "rewind"
  onClose: () => void
}) {
  const store = useStore()
  const theme = useTheme()
  useKeyboard((key) => {
    if (key.name === "escape") {
      key.stopPropagation()
      props.onClose()
    } else if (key.name === "return") {
      key.stopPropagation()
      const checkpoint = props.checkpoint
      const steps = props.steps ?? 1
      props.onClose()
      void store.rewind(checkpoint?.id, steps)
    } else if (key.name === "f" && props.checkpoint) {
      key.stopPropagation()
      const checkpoint = props.checkpoint
      props.onClose()
      void store.forkCheckpoint(checkpoint.id)
    }
  })
  const undo = () => props.label === "undo"
  return (
    <Modal title={undo() ? "undo" : "rewind"} width={78} height={14}>
      <box flexDirection="column" flexGrow={1} gap={1}>
        <Notice warning>
          <text fg={theme.warning}>
            {undo()
              ? "This undoes the latest checkpoint and restores workspace files."
              : "This restores workspace files from a checkpoint."}
          </text>
        </Notice>
        <text fg={theme.text}>
          {props.checkpoint
            ? `Checkpoint: ${props.checkpoint.label ?? props.checkpoint.id} (${props.checkpoint.created_at})`
            : `Target: the checkpoint ${props.steps ?? 1} step(s) before the latest.`}
        </text>
        <text fg={theme.dim}>Uncommitted files are overwritten by the shadow-git reset.</text>
        <text fg={theme.dim}>
          Enter restores · f forks from this checkpoint (and restores the workspace) · Esc cancels.
        </text>
      </box>
    </Modal>
  )
}

function MemoryDialog(props: { onClose: () => void }) {
  const store = useStore()
  const theme = useTheme()
  const [memory] = createResource(() => store.memory())
  useKeyboard((key) => {
    if (key.name === "escape") {
      key.stopPropagation()
      props.onClose()
    }
  })
  return (
    <Modal title="memory" width={90} height={28}>
      <box flexDirection="column" flexGrow={1}>
        <Show
          when={memory()}
          fallback={<text fg={theme.dim}>Loading memory… (start the daemon and try again)</text>}
        >
          {(value) => (
            <>
              <SectionLabel>{`user · ${value().user.path}`}</SectionLabel>
              <text fg={theme.muted} wrapMode="word">
                {value().user.content || "(empty)"}
              </text>
              <SectionLabel>{`project · ${value().project.path}`}</SectionLabel>
              <text fg={theme.muted} wrapMode="word">
                {value().project.content || "(empty; run /init or edit .siwah/project.md)"}
              </text>
              <SectionLabel>{`quick · ${value().quick.path}`}</SectionLabel>
              <text fg={theme.muted} wrapMode="word">
                {value().quick.content || "(empty; use # note)"}
              </text>
            </>
          )}
        </Show>
        <box flexGrow={1} />
        <text fg={theme.dim}>
          Esc closes · /init fills project memory · # quick note · ask the agent to remember
        </text>
      </box>
    </Modal>
  )
}

function EscapeHint(props: { onClose: () => void }) {
  useKeyboard((key) => {
    if (key.name === "escape") {
      key.stopPropagation()
      props.onClose()
    }
  })
  return null
}
