import { TextAttributes } from "@opentui/core"
import { Show } from "solid-js"
import { useDimensions, useStore, useTheme } from "./context"
import { Logo } from "./Logo"

export function Welcome() {
  const store = useStore()
  const theme = useTheme()
  const dimensions = useDimensions()
  const compact = () => dimensions().height < 26 || Boolean(store.permission())
  const update = () => store.updateStatus()
  const providers = () => store.auth()?.providers ?? []
  const needsKey = () =>
    providers().some((provider) => Boolean(provider.env) && !provider.env_set && !provider.file_set)
  const noModel = () => !store.currentModel() || store.currentModel() === "not configured"
  const hint = () => {
    if (store.runtimeError()) return null
    if (needsKey()) return "Connect a provider with /connect"
    if (noModel()) return "Choose a model with /models"
    return null
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
      <Logo compact={compact()} />
      <Show when={!compact() && !store.runtimeError()}>
        <box flexDirection="column" alignItems="center" gap={1} marginTop={1}>
          <text fg={theme.text} wrapMode="none">
            {"Build something. Understand anything."}
          </text>
          <box flexDirection="row" gap={3}>
            <text fg={theme.muted} onMouseDown={() => store.openDialog({ type: "sessions" })}>
              {"/sessions  resume"}
            </text>
            <text fg={theme.muted} onMouseDown={() => store.openDialog({ type: "help" })}>
              {"/help  explore"}
            </text>
          </box>
        </box>
      </Show>

      <Show when={!compact() && hint()}>
        <text fg={theme.muted} wrapMode="none">
          {hint()}
        </text>
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

      <Show when={compact() && hint()}>
        <text fg={theme.dim} wrapMode="none">
          {hint()}
        </text>
      </Show>
    </box>
  )
}
