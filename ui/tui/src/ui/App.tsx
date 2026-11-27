import { type BoxRenderable, MacOSScrollAccel, type ScrollBoxRenderable } from "@opentui/core"
import { useKeyboard, useRenderer, useTerminalDimensions } from "@opentui/solid"
import { createEffect, createMemo, createSignal, Index, onCleanup, onMount, Show } from "solid-js"
import { bindingMatches, filterCommands, filterFiles } from "../commands"
import { loadTuiConfig } from "../config"
import { initPrompt, learnPrompt, reviewPrompt } from "../prompts"
import type { PromptImage, TuiStore } from "../state"
import type { Theme } from "../theme"
import { copyText } from "../util"
import { ActivityBar } from "./ActivityBar"
import {
  DimensionsContext,
  StoreContext,
  setActiveStore,
  setActiveTheme,
  ThemeContext,
  useDimensions,
  useStore,
  useTheme,
} from "./context"
import { DialogHost } from "./Dialogs"
import { InlineHost } from "./InlineHost"
import { MessageView } from "./MessageView"
import { PermissionDialog } from "./PermissionDialog"
import type { PromptHandle } from "./PromptBox"
import { PromptBox } from "./PromptBox"
import { QuestionPanel } from "./QuestionPanel"
import { RunNotice } from "./RunNotice"
import { StatusBar } from "./StatusBar"
import { ToastStack } from "./ToastStack"
import { TodoPanel } from "./TodoPanel"
import { TopBar } from "./TopBar"
import { Welcome } from "./Welcome"

class TranscriptScrollAcceleration extends MacOSScrollAccel {
  override tick(now?: number): number {
    return Math.max(2, super.tick(now))
  }
}

export function App(props: { store: TuiStore; initialize?: boolean }) {
  const renderer = useRenderer()
  const dimensions = useTerminalDimensions()
  renderer.setMaxListeners(100)
  // Components retain this object; each property reads the current theme.
  // Theme changes must not remount the composer and lose its draft.
  const theme = new Proxy({} as Theme, {
    get: (_target, key) => Reflect.get(props.store.theme(), key),
  })
  onMount(() => {
    if (props.initialize !== false) void props.store.initialize()
  })
  setActiveStore(props.store)
  setActiveTheme(theme)
  return (
    <StoreContext.Provider value={props.store}>
      <ThemeContext.Provider value={theme}>
        <DimensionsContext.Provider value={dimensions}>
          <Shell />
        </DimensionsContext.Provider>
      </ThemeContext.Provider>
    </StoreContext.Provider>
  )
}

function Shell() {
  const store = useStore()
  const renderer = useRenderer()
  const theme = useTheme()
  const keybinds = loadTuiConfig().keybinds
  const dimensions = useDimensions()
  const promptText = () => store.promptText()
  const [inlineSelected, setInlineSelected] = createSignal(0)
  const [promptFocused, setPromptFocused] = createSignal(false)
  let welcomeShown = false
  const renderWelcome = () => {
    const animated = !welcomeShown
    welcomeShown = true
    return <Welcome animated={animated} />
  }
  let promptHandle: PromptHandle | undefined
  let lastEscape = 0
  let lastCtrlC = 0

  const mention = createMemo(() => {
    const text = promptText()
    const match = /(?:^|\s)@([^\s]*)$/.exec(text)
    if (!match) return null
    const at = text.lastIndexOf("@")
    return { at, query: match[1] ?? "" }
  })
  const slash = createMemo(() => {
    const text = promptText()
    if (!text.startsWith("/") || text.includes("\n") || /\s/.test(text.slice(1))) return null
    return text
  })
  const inlineKind = () => (mention() ? "files" : slash() ? "commands" : null)
  const inlineItems = createMemo(() => {
    if (mention()) {
      return filterFiles(mention()?.query ?? "", store.files()).map((file) => ({
        label: file,
        detail: "file",
        value: file,
      }))
    }
    if (slash()) {
      const custom = store.customCommands().map((command) => ({
        name: `/${command.name}`,
        description: command.description,
      }))
      return filterCommands(slash() ?? "", custom).map((command) => ({
        label: command.name,
        detail: command.description,
        value: command.name,
      }))
    }
    return []
  })

  createEffect(() => {
    if (mention()) void store.loadFiles()
  })

  createEffect(() => {
    const modal = Boolean(store.permission() || store.question() || store.dialog())
    queueMicrotask(() => {
      if (!promptHandle) return
      if (modal) promptHandle.blur()
      else promptHandle.focus()
    })
  })

  let transcript: ScrollBoxRenderable | undefined
  const scrollAcceleration = new TranscriptScrollAcceleration()
  const [awayFromLatest, setAwayFromLatest] = createSignal(false)
  const trackScroll = (node: ScrollBoxRenderable) => {
    transcript = node
    const update = () => {
      if (node.isDestroyed) return
      setAwayFromLatest(
        node.scrollTop + node.verticalScrollBar.viewportSize < node.scrollHeight - 1,
      )
    }
    node.verticalScrollBar.on("change", update)
    node.content.on("resize", update)
    node.viewport.on("resize", update)
    node.afterLayout(update)
    onCleanup(() => {
      node.verticalScrollBar.off("change", update)
      node.content.off("resize", update)
      node.viewport.off("resize", update)
      if (transcript === node) transcript = undefined
    })
  }
  const scrollToLatest = () => {
    queueMicrotask(() => {
      transcript?.scrollTo({ x: 0, y: Infinity })
    })
  }
  // Only session hydration requests an unconditional jump. Completion leaves
  // native sticky-scroll ownership alone, including while reading history.
  createEffect(() => {
    const tick = store.scrollTick()
    if (tick > 0) scrollToLatest()
  })
  createEffect(() => {
    const id = store.focusMessage()
    if (!id) return
    queueMicrotask(() => {
      transcript?.scrollChildIntoView(`msg-${id}`, { align: "start" })
      store.focusMessage(null)
    })
  })

  const clearPrompt = () => {
    promptHandle?.clear()
    store.promptText("")
    setInlineSelected(0)
  }

  const insertMention = (file: string) => {
    const token = mention()
    if (!token) return
    const text = promptText()
    const next = `${text.slice(0, token.at)}@${file} `
    promptHandle?.setText(next)
  }

  const chooseInline = (value: string) => {
    if (inlineKind() === "files") insertMention(value)
    else {
      promptHandle?.setText(value)
      promptHandle?.submit()
    }
  }

  const submit = async (text: string, images: PromptImage[] = []): Promise<boolean> => {
    const value = text.trim()
    if (!value && images.length === 0) return true
    if (images.length === 0 && inlineKind()) {
      const item = inlineItems()[inlineSelected()]
      if (item) {
        if (inlineKind() === "files") {
          insertMention(item.value)
          return false
        }
        return executeCommand(item.value)
      }
      // No suggestion: fall through so unknown commands and plain @ text still work.
    }
    if (images.length === 0 && value.startsWith("/")) {
      return executeCommand(value)
    }
    if (images.length === 0 && value.startsWith("!")) {
      void (async () => {
        if (!store.session()) await store.createSession()
        await store.runShell(value.slice(1).trim())
      })()
      return true
    }
    if (images.length === 0 && value.startsWith("#")) {
      void store.quickNote(value.slice(1).trim())
      return true
    }
    return store.sendPrompt(value, { images })
  }

  const toggleToolDetails = () => {
    const latest = [...store.messages()].reverse().find((message) => message.toolCalls.length > 0)
    if (!latest) return
    const ids = latest.toolCalls.map((tool) => tool.id)
    const collapse = ids.every((id) => store.isToolExpanded(id))
    store.expandedTools((current) =>
      collapse ? current.filter((id) => !ids.includes(id)) : [...new Set([...current, ...ids])],
    )
  }

  const executeCommand = async (raw: string): Promise<boolean> => {
    try {
      return (await dispatchCommand(raw)) !== false
    } catch (error) {
      store.notify(error instanceof Error ? error.message : String(error), "error")
      return false
    }
  }

  const dispatchCommand = async (raw: string): Promise<boolean | undefined> => {
    const name = raw.trim().split(/\s+/)[0] ?? raw.trim()
    switch (name.toLowerCase()) {
      case "/help":
        store.openDialog({ type: "help" })
        return
      case "/connect":
        await store.reloadAuth()
        store.openDialog({ type: "connect" })
        return
      case "/models":
        await store.reloadModels()
        store.openDialog({ type: "models" })
        return
      case "/sessions":
        await store.reloadSessions()
        store.openDialog({ type: "sessions" })
        return
      case "/new":
        await store.createSession()
        store.notify("New session", "success")
        return
      case "/turns":
        store.openDialog({ type: "turns" })
        return
      case "/fork":
        await store.forkSession()
        return
      case "/plan":
        await store.setMode("plan")
        return
      case "/build":
        await store.setMode("build")
        return
      case "/effort":
        store.openDialog({ type: "effort" })
        return
      case "/permissions": {
        const order = ["ask", "auto-edit", "yolo"] as const
        const index = order.indexOf(store.permissionMode())
        await store.setPermissionMode(order[(index + 1) % order.length] ?? "ask")
        return
      }
      case "/reasoning":
        store.toggleReasoningVisibility()
        return
      case "/tools":
        toggleToolDetails()
        return
      case "/todos":
        if (dimensions().width < 100) store.openDialog({ type: "todos" })
        else store.toggleTodos()
        return
      case "/quit":
        renderer.destroy()
        process.exit(0)
        return
      case "/cost":
        await store.refreshUsage()
        store.openDialog({ type: "cost" })
        return
      case "/evolve":
        await store.reloadEvolution()
        store.openDialog({ type: "evolution" })
        return
      case "/checkpoints":
        if (!store.session()) store.notify("Start a session first", "warning")
        else {
          await store.reloadCheckpoints()
          store.openDialog({ type: "checkpoints" })
        }
        return
      case "/skills":
        store.openDialog({ type: "skills" })
        return
      case "/learned":
        store.openDialog({ type: "learned" })
        return
      case "/mcp":
        await store.reloadMcp()
        store.openDialog({ type: "mcp" })
        return
      case "/memory":
        store.openDialog({ type: "memory" })
        return
      case "/doctor":
        await store.reloadAuth()
        store.openDialog({ type: "doctor" })
        return
      case "/theme":
        store.openDialog({ type: "theme" })
        return
      case "/rewind": {
        if (!store.session()) store.notify("Start a session first", "warning")
        else {
          const value = Number.parseInt(raw.trim().split(/\s+/)[1] ?? "1", 10)
          store.openDialog({
            type: "rewind",
            steps: Number.isFinite(value) ? value : 1,
            label: "rewind",
          })
        }
        return
      }
      case "/undo":
        if (!store.session()) store.notify("Start a session first", "warning")
        else store.openDialog({ type: "rewind", steps: 1, label: "undo" })
        return
      case "/compact":
        await store.compact()
        return
      case "/execute-plan":
        await store.executePlan()
        return
      case "/update":
        await store.applyUpdate()
        return
      case "/rollback":
        await store.rollbackEvolution(raw.trim().split(/\s+/)[1] ?? null)
        return
      case "/pause":
        await store.pauseEvolution(true)
        return
      case "/resume":
        await store.pauseEvolution(false)
        return
      case "/init": {
        const focus = raw.trim().slice(name.length).trim()
        if (store.running()) {
          store.notify("A run is already active; wait or cancel it first", "warning")
          return false
        }
        await store.initMemory()
        return store.sendPrompt(initPrompt(store.workspacePath(), focus), {
          display: "Create or update AGENTS.md and .siwah/project.md for this repository.",
        })
      }
      case "/timing":
        store.openDialog({ type: "timing" })
        return true
      case "/steer":
        return store.steerPrompt(raw.trim().slice(name.length).trim())
      case "/queue":
        return store.sendPrompt(raw.trim().slice(name.length).trim())
      case "/pending":
        store.openDialog({ type: "pending" })
        return true
      case "/find":
        store.openDialog({ type: "find", query: raw.trim().slice(name.length).trim() })
        return true
      case "/changes": {
        const target = raw.trim().slice(name.length).trim()
        store.openDialog({
          type: "changes",
          scope: target === "workspace" ? "workspace" : "turn",
          checkpointId: target && target !== "workspace" ? target : undefined,
        })
        return true
      }
      case "/review": {
        const target = raw.trim().slice(name.length).trim()
        return store.sendPrompt(reviewPrompt(store.workspacePath(), target), {
          display: target ? `Review changes: ${target}` : "Review uncommitted changes",
        })
      }
      case "/learn": {
        const target = raw.trim().slice(name.length).trim()
        return store.sendPrompt(learnPrompt(target), {
          display: target ? `Learn from this session: ${target}` : "Learn from this session",
        })
      }
      default:
        return store.runCustomCommand(name, raw.trim().slice(name.length).trim())
    }
  }

  const hasTextSelection = () => (renderer.getSelection()?.getSelectedText() ?? "").length > 0

  useKeyboard((key) => {
    // The modal is above the inline permission card. Never resolve a hidden
    // request from text intended for a dialog (for example an API key).
    if (store.dialog()) return
    // The question owns ordinary keys. Its configured cancel binding dismisses
    // the question; the configured exit binding still aborts the run.
    if (store.question() && !store.permission()) {
      if (bindingMatches(key, keybinds.cancel)) {
        key.stopPropagation()
        void store.answerQuestion(null)
        return
      }
      if (!bindingMatches(key, keybinds.exit)) return
    }
    if (store.running() && bindingMatches(key, keybinds.cancel)) {
      key.stopPropagation()
      if (hasTextSelection()) {
        renderer.clearSelection()
        return
      }
      void store.abort()
      return
    }
    if (store.permission()) return

    if (!inlineKind() && promptText() === "" && (key.name === "up" || key.name === "down")) {
      const value = key.name === "up" ? store.historyPrevious() : store.historyNext()
      if (value !== null) {
        key.stopPropagation()
        promptHandle?.setText(value)
        return
      }
    }

    if (bindingMatches(key, keybinds.palette)) {
      key.stopPropagation()
      store.openDialog({ type: "palette" })
      return
    }
    if (bindingMatches(key, keybinds.history)) {
      key.stopPropagation()
      store.openDialog({ type: "history" })
      return
    }
    if (bindingMatches(key, keybinds.jumpLatest)) {
      key.stopPropagation()
      scrollToLatest()
      return
    }
    if (bindingMatches(key, keybinds.toolDetails)) {
      key.stopPropagation()
      toggleToolDetails()
      return
    }
    if (bindingMatches(key, keybinds.exit)) {
      key.stopPropagation()
      const selected = promptHandle?.selection() ?? ""
      const transcript =
        selected.length > 0 ? "" : (renderer.getSelection()?.getSelectedText() ?? "")
      const text = selected.length > 0 ? selected : transcript
      if (text.length > 0) {
        let copied = false
        try {
          if (renderer.isOsc52Supported()) copied = renderer.copyToClipboardOSC52(text)
        } catch {
          copied = false
        }
        if (!copied) copied = copyText(text)
        if (copied) {
          store.notify(`Copied ${text.length} characters`, "success")
          if (selected.length > 0) promptHandle?.clearSelection()
          else renderer.clearSelection()
        } else {
          store.notify("Copy failed; select the text and try again", "warning")
        }
        return
      }
      if (store.running()) {
        void store.abort()
        const now = Date.now()
        if (now - lastCtrlC < 1200) {
          renderer.destroy()
          process.exit(0)
        }
        lastCtrlC = now
        store.notify("Cancelling. Press Ctrl+C again to exit.", "warning")
        return
      }
      const now = Date.now()
      if (now - lastCtrlC < 1200) {
        renderer.destroy()
        process.exit(0)
      } else {
        lastCtrlC = now
        store.notify("Press Ctrl+C twice to exit", "warning")
      }
      return
    }
    if (bindingMatches(key, keybinds.mode)) {
      key.stopPropagation()
      void store.setMode(store.currentMode() === "build" ? "plan" : "build")
      return
    }
    if (bindingMatches(key, keybinds.help) && promptText().length === 0) {
      key.stopPropagation()
      store.openDialog({ type: "help" })
      return
    }

    if (inlineKind()) {
      const last = () => Math.max(0, inlineItems().length - 1)
      if (key.name === "up") {
        key.stopPropagation()
        setInlineSelected((value) => Math.max(0, value - 1))
      } else if (key.name === "down") {
        key.stopPropagation()
        setInlineSelected((value) => Math.min(last(), value + 1))
      } else if (key.name === "pageup") {
        key.stopPropagation()
        setInlineSelected((value) => Math.max(0, value - 7))
      } else if (key.name === "pagedown") {
        key.stopPropagation()
        setInlineSelected((value) => Math.min(last(), value + 7))
      } else if (key.name === "home") {
        key.stopPropagation()
        setInlineSelected(0)
      } else if (key.name === "end") {
        key.stopPropagation()
        setInlineSelected(last())
      } else if (key.name === "return" && inlineKind() === "files" && inlineItems().length > 0) {
        key.stopPropagation()
        const item = inlineItems()[inlineSelected()]
        if (item) chooseInline(item.value)
      } else if (bindingMatches(key, keybinds.cancel)) {
        key.stopPropagation()
        clearPrompt()
      }
      return
    }

    if (bindingMatches(key, keybinds.pageUp) || bindingMatches(key, keybinds.pageDown)) {
      key.stopPropagation()
      transcript?.scrollBy(
        (bindingMatches(key, keybinds.pageUp) ? -1 : 1) *
          Math.max(1, (transcript?.verticalScrollBar.viewportSize ?? 10) - 2),
      )
      return
    }

    if (bindingMatches(key, keybinds.cancel)) {
      key.stopPropagation()
      if (hasTextSelection()) {
        renderer.clearSelection()
      } else if (promptText().length > 0) {
        clearPrompt()
      } else {
        const now = Date.now()
        if (now - lastEscape < 500) store.openDialog({ type: "rewind" })
        lastEscape = now
      }
    }
  })

  const messages = () => store.messages()
  const showTodoPanel = () =>
    dimensions().width >= 100 && store.todos().length > 0 && !store.todosHidden()
  const small = () => dimensions().width < 60
  const [footerHeight, setFooterHeight] = createSignal(8)
  let footer: BoxRenderable | undefined
  const activityVisible = () =>
    store.running() ||
    store.queuedPrompts().length > 0 ||
    Boolean(store.permission()) ||
    Boolean(store.question()) ||
    (messages().length > 0 && awayFromLatest())

  return (
    <box flexDirection="column" width="100%" height="100%" backgroundColor={theme.background}>
      <Show when={small()}>
        <text fg={theme.warning} paddingX={2}>
          Terminal narrower than 60 columns; some controls are clipped.
        </text>
      </Show>
      <TopBar />
      <box flexDirection="row" flexGrow={1} minHeight={0} width="100%">
        <box
          flexDirection="column"
          flexGrow={1}
          minWidth={0}
          minHeight={0}
          onMouseDown={() => promptHandle?.focus()}
        >
          <Show when={messages().length === 0} fallback={null}>
            {renderWelcome()}
          </Show>
          <Show when={messages().length > 0} fallback={null}>
            <scrollbox
              id="transcript"
              ref={trackScroll}
              flexGrow={1}
              flexShrink={1}
              minHeight={0}
              width="100%"
              stickyScroll
              stickyStart="bottom"
              scrollX={false}
              viewportCulling
              scrollAcceleration={scrollAcceleration}
              paddingX={2}
              paddingTop={1}
              scrollbarOptions={{ visible: false }}
              verticalScrollbarOptions={{ visible: false }}
            >
              <Index each={messages()}>{(message) => <MessageView message={message()} />}</Index>
              <RunNotice />
            </scrollbox>
          </Show>
        </box>
        <Show when={showTodoPanel()}>
          <box marginLeft={1} marginRight={2} flexShrink={0}>
            <TodoPanel />
          </box>
        </Show>
      </box>

      <Show when={!store.question()}>
        <InlineHost
          selected={inlineSelected()}
          bottom={footerHeight()}
          onSelect={(item) => chooseInline(item.value)}
        />
      </Show>

      <PermissionDialog cancelKey={keybinds.cancel} />
      <QuestionPanel cancelKey={keybinds.cancel} />
      <box
        id="composer-dock"
        ref={(node) => {
          footer = node
        }}
        onSizeChange={() => {
          if (footer) setFooterHeight(footer.height)
        }}
        flexDirection="column"
        width="100%"
        flexShrink={0}
      >
        <box width="100%" flexShrink={0} paddingX={2}>
          <box
            id="composer-panel"
            flexDirection="column"
            width="100%"
            flexShrink={0}
            border
            borderStyle="rounded"
            borderColor={promptFocused() ? theme.borderFocus : theme.border}
            backgroundColor={theme.panel}
          >
            <PromptBox
              embedded
              mode={store.currentMode()}
              stats={`${store.contextSummary()} · ${store.usageSummary()}`}
              statsColor={store.contextTone()}
              onHeightChange={(height) => store.promptHeight(height)}
              onFocusChange={setPromptFocused}
              placeholder={
                store.runtimeError()
                  ? "Daemon unavailable — start `siwah serve`"
                  : "Ask siwah to change something…"
              }
              onChange={(value) => {
                store.promptText(value)
                setInlineSelected(0)
              }}
              onSubmit={submit}
              onReady={(handle) => {
                promptHandle = handle
                handle.focus()
              }}
            />
            <StatusBar embedded />
          </box>
        </box>
        <box id="activity-slot" width="100%" height={1} flexShrink={0}>
          <Show when={activityVisible()}>
            <ActivityBar
              away={messages().length > 0 && awayFromLatest()}
              jumpKey={keybinds.jumpLatest}
              cancelKey={keybinds.cancel}
              exitKey={keybinds.exit}
              onJump={scrollToLatest}
            />
          </Show>
        </box>
      </box>
      <DialogHost
        onCommand={(name) => void executeCommand(name)}
        onHistory={(text) => {
          promptHandle?.setText(text)
        }}
        canRestorePrompt={() => promptHandle?.empty() ?? false}
        onRestorePrompt={(text, attachments) => promptHandle?.restore(text, attachments)}
      />
      <ToastStack bottom={footerHeight()} />
    </box>
  )
}
