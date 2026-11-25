import { TextAttributes } from "@opentui/core"
import { Show } from "solid-js"
import { useDimensions, useStore, useTheme } from "./context"
import { Logo } from "./Logo"

export function Welcome(props: { animated?: boolean } = {}) {
  const store = useStore()
  const theme = useTheme()
  const dimensions = useDimensions()
  const compact = () => dimensions().height < 26 || Boolean(store.permission())
  const update = () => store.updateStatus()
  const providers = () => store.auth()?.providers ?? []
  const needsKey = () => {
    const configured = providers()
    const activeProvider = store.currentModel().split("/")[0]
    const selected = configured.find((provider) => provider.provider === activeProvider)
    const missing = (provider: (typeof configured)[number]) =>
      Boolean(provider.env) && !provider.env_set && !provider.file_set
    return selected ? missing(selected) : configured.length > 0 && configured.every(missing)
  }
  const noModel = () => !store.currentModel() || store.currentModel() === "not configured"
  const action = () => {
    if (store.runtimeError()) return null
    if (needsKey()) return { label: "Connect a provider with /connect", type: "connect" } as const
    if (noModel()) return { label: "Choose a model with /models", type: "models" } as const
    if (store.projectSessions().length > 0)
      return { label: "/sessions  resume", type: "sessions" } as const
    return { label: "/help  explore", type: "help" } as const
  }

  return (
    <box
      flexGrow={1}
      flexDirection="column"
      alignItems="center"
      justifyContent="center"
      paddingX={2}
      paddingTop={compact() ? 0 : 2}
      paddingBottom={compact() ? 0 : 1}
      gap={1}
      minHeight={0}
      overflow="hidden"
    >
      <Logo compact={compact()} animated={props.animated} />
      <Show when={action()}>
        {(item) => (
          <text
            fg={theme.muted}
            wrapMode="none"
            onMouseDown={() => store.openDialog({ type: item().type })}
          >
            {item().label}
          </text>
        )}
      </Show>

      <Show when={update()?.available}>
        <text fg={theme.accent} wrapMode="none">
          {`↑ siwah ${update()?.latest ?? "update"} is available · /update`}
        </text>
      </Show>

      <Show when={store.runtimeError()}>
        <box
          marginTop={1}
          border={["left"]}
          borderColor={theme.error}
          paddingLeft={1}
          flexDirection="column"
          maxWidth={76}
        >
          <text fg={theme.error} attributes={TextAttributes.BOLD}>
            daemon unavailable
          </text>
          <text fg={theme.muted} wrapMode="word">
            {store.runtimeError()}
          </text>
          <text fg={theme.dim} wrapMode="word">
            Start `siwah serve` in another terminal, then restart this TUI. Headless: `siwah run
            "..." --ui plain`.
          </text>
        </box>
      </Show>
    </box>
  )
}
