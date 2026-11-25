import { createMemo, Show } from "solid-js"
import { filterCommands, filterFiles } from "../commands"
import { useDimensions, useStore } from "./context"
import { type InlineItem, InlineMenu } from "./InlineMenu"

export function InlineHost(props: {
  selected: number
  bottom?: number
  onSelect: (item: InlineItem) => void
}) {
  const store = useStore()
  const dimensions = useDimensions()
  const mention = createMemo(() => {
    const text = store.promptText()
    const match = /(?:^|\s)@([^\s]*)$/.exec(text)
    if (!match) return null
    return { at: text.lastIndexOf("@"), query: match[1] ?? "" }
  })
  const slash = createMemo(() => {
    const text = store.promptText()
    if (!text.startsWith("/") || text.includes("\n") || /\s/.test(text.slice(1))) return null
    return text
  })
  const kind = () => (mention() ? "files" : slash() ? "commands" : null)
  const bottom = () => props.bottom ?? store.promptHeight() + (dimensions().width < 78 ? 5 : 4)
  const maxRows = () =>
    Math.min(store.messages().length === 0 ? 5 : 7, Math.max(1, dimensions().height - bottom() - 6))
  const items = createMemo<InlineItem[]>(() => {
    if (mention()) {
      return filterFiles(mention()?.query ?? "", store.files()).map((file) => {
        const directory = /[\\/]$/.test(file)
        const parts = file.replace(/[\\/]+$/, "").split(/[\\/]/)
        const label = parts.pop() || file
        return {
          label: directory ? `${label}/` : label,
          detail: parts.join("/") || ".",
          value: file,
        }
      })
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

  return (
    <Show when={items().length > 0}>
      <box
        position="absolute"
        left={2}
        bottom={bottom()}
        width={dimensions().width - 4}
        height={Math.min(maxRows(), items().length) + 4}
        zIndex={50}
      >
        <InlineMenu
          title={kind() === "files" ? "workspace files" : "commands"}
          items={items()}
          selected={props.selected}
          maxRows={maxRows()}
          showCompactDetail={kind() === "files"}
          onSelect={props.onSelect}
        />
      </box>
    </Show>
  )
}
