import { execFile } from "node:child_process"
import { existsSync, readFileSync } from "node:fs"
import { basename } from "node:path"
import { fileURLToPath } from "node:url"

import type { TextareaRenderable } from "@opentui/core"
import { TextAttributes } from "@opentui/core"
import { useKeyboard, usePaste } from "@opentui/solid"
import { createEffect, createSignal, For, onCleanup, onMount, Show } from "solid-js"
import type { PromptImage } from "../state"
import { useDimensions, useStore, useTheme } from "./context"

export type PromptHandle = {
  setText: (value: string) => void
  clear: () => void
  focus: () => void
  blur: () => void
  value: () => string
  selection: () => string
  clearSelection: () => void
  submit: () => void
}

export function PromptBox(props: {
  embedded?: boolean
  placeholder?: string
  mode: string
  stats: string
  statsColor?: string
  onChange: (value: string) => void
  onSubmit: (
    value: string,
    images: PromptImage[],
  ) => boolean | undefined | Promise<boolean | undefined>
  onReady: (handle: PromptHandle) => void
  onHeightChange?: (height: number) => void
}) {
  const theme = useTheme()
  const store = useStore()
  const dimensions = useDimensions()
  const permission = () => store.permissionMode()
  let area: TextareaRenderable | undefined
  const [value, setValue] = createSignal("")
  const [images, setImages] = createSignal<PromptImage[]>([])
  let revision = 0
  let submitting = false
  let disposed = false
  let ownershipRevision = 0
  const available = () => !disposed && !store.dialog() && !store.permission()
  const ownsInput = () => available() && Boolean(area?.focused)
  createEffect(() => {
    store.dialog()
    store.permission()
    ownershipRevision += 1
    if (!available()) area?.blur()
  })
  onCleanup(() => {
    disposed = true
  })

  const addImage = (image: PromptImage): boolean => {
    if (images().some((item) => item.data === image.data)) return true
    if (images().length >= 4) {
      store.notify("At most 4 images per message", "warning")
      return false
    }
    if (image.data.length > 12_000_000) {
      store.notify("Image is too large (max ~9 MB)", "warning")
      return false
    }
    const token = `[Image ${images().length + 1}]`
    revision += 1
    setImages((current) => [...current, { ...image, token }])
    area?.insertText(`${token} `)
    const next = area?.plainText ?? value()
    setValue(next)
    props.onChange(next)
    return true
  }

  const removeImage = (image: PromptImage) => {
    if (!ownsInput()) return
    revision += 1
    setImages((current) => current.filter((item) => item.id !== image.id))
    if (!image.token) return
    const current = area?.plainText ?? value()
    const next = current
      .replace(image.token, "")
      .replace(/\s{2,}/g, " ")
      .trimStart()
    area?.setText(next)
    setValue(next)
    props.onChange(next)
  }

  usePaste((event) => {
    if (!ownsInput()) return
    const mime = event.metadata?.mimeType ?? ""
    if (event.metadata?.kind === "binary" && mime.startsWith("image/")) {
      event.preventDefault()
      event.stopPropagation()
      addImage({
        id: `img_${Date.now()}`,
        mime,
        data: Buffer.from(event.bytes).toString("base64"),
        name: "pasted image",
      })
      return
    }
    const pasted = new TextDecoder().decode(event.bytes).trim()
    if (!pasted) return
    const attachments: PromptImage[] = []
    for (const raw of pasted.split(/\r?\n/).filter((line) => line.trim())) {
      const path = imageFilePath(raw)
      // Convert only all-image-path pastes. Mixed instructions stay raw text.
      if (!path) return
      try {
        const data = readFileSync(path).toString("base64")
        attachments.push({
          id: `img_${Date.now()}_${attachments.length}`,
          mime: imageMime(path),
          data,
          name: basename(path),
        })
      } catch {
        // A path that cannot be read stays as ordinary pasted text.
        return
      }
    }
    if (attachments.length === 0) return
    const newImages = attachments.filter(
      (attachment) => !images().some((image) => image.data === attachment.data),
    )
    if (images().length + newImages.length > 4) {
      store.notify("At most 4 images per message", "warning")
      return
    }
    if (newImages.some((image) => image.data.length > 12_000_000)) {
      store.notify("Image is too large (max ~9 MB)", "warning")
      return
    }
    event.preventDefault()
    event.stopPropagation()
    for (const attachment of attachments) addImage(attachment)
  })

  useKeyboard((key) => {
    if (!ownsInput()) return
    if (key.name === "v" && key.ctrl && !key.shift && !key.meta) {
      const owner = ownershipRevision
      void readClipboardImage().then((image) => {
        if (image && ownsInput() && owner === ownershipRevision) addImage(image)
      })
    }
  })

  const handle: PromptHandle = {
    setText: (next: string) => {
      revision += 1
      area?.setText(next)
      setValue(next)
      props.onChange(next)
      area?.gotoBufferEnd()
      if (available()) area?.focus()
    },
    clear: () => handle.setText(""),
    focus: () => {
      if (available()) area?.focus()
    },
    blur: () => area?.blur(),
    value: () => area?.plainText ?? value(),
    selection: () => (area?.hasSelection() ? area.getSelectedText() : ""),
    clearSelection: () => {
      area?.clearSelection()
    },
    submit: () => void submit(),
  }

  onMount(() => {
    props.onReady(handle)
    handle.focus()
  })

  const submit = async () => {
    if (!ownsInput() || submitting) return
    const text = area?.plainText ?? value()
    if (text.endsWith("\\") && !text.endsWith("\\\\")) {
      const next = `${text.slice(0, -1)}\n`
      handle.setText(next)
      return
    }
    const submittedRevision = revision
    const submittedImages = images()
    submitting = true
    try {
      const accepted = await props.onSubmit(text, submittedImages)
      if (disposed || accepted === false) return
      // Clear only the submitted revision, never newer typing or attachments.
      if (
        revision === submittedRevision ||
        ((area?.plainText ?? value()) === text && images() === submittedImages)
      ) {
        setImages([])
        handle.clear()
      }
    } catch (error) {
      if (!disposed) store.notify(error instanceof Error ? error.message : String(error), "error")
    } finally {
      submitting = false
      if (!disposed) handle.focus()
    }
  }

  const height = () => {
    const usable = Math.max(20, dimensions().width - 8)
    const wrapped = value()
      .split("\n")
      .reduce((total, line) => total + Math.max(1, Math.ceil(line.length / usable)), 0)
    return Math.min(8, Math.max(1, wrapped)) + (images().length > 0 ? 1 : 0)
  }
  createEffect(() => props.onHeightChange?.(height()))

  return (
    <box
      flexDirection="column"
      width="100%"
      flexShrink={0}
      paddingX={props.embedded ? 0 : 2}
      paddingBottom={0}
    >
      <box
        flexDirection="column"
        width="100%"
        border={props.embedded ? [] : ["left"]}
        borderStyle="rounded"
        borderColor={value().length > 0 ? theme.borderFocus : theme.border}
        backgroundColor={theme.panel}
        paddingX={1}
        onMouseDown={() => handle.focus()}
      >
        <box flexDirection="row" justifyContent="space-between" gap={1} height={1}>
          <box flexDirection="row" gap={1} minWidth={0}>
            <text fg={theme.accent} attributes={TextAttributes.BOLD} wrapMode="none">
              {"›"}
            </text>
            <text
              fg={props.mode === "plan" ? theme.warning : theme.accentAlt}
              attributes={TextAttributes.BOLD}
              wrapMode="none"
            >
              {props.mode}
            </text>
            <Show
              when={permission() === "yolo"}
              fallback={
                <text fg={permission() === "auto-edit" ? theme.warning : theme.dim} wrapMode="none">
                  {permission()}
                </text>
              }
            >
              <text
                fg={theme.background}
                bg={theme.warning}
                attributes={TextAttributes.BOLD}
                wrapMode="none"
              >
                {" AUTO "}
              </text>
            </Show>
          </box>
          <Show when={dimensions().width >= 78}>
            <text fg={props.statsColor ?? theme.dim} wrapMode="none" flexShrink={0}>
              {props.stats}
            </text>
          </Show>
        </box>
        <Show when={dimensions().width < 78}>
          <text fg={props.statsColor ?? theme.dim} wrapMode="none">
            {props.stats}
          </text>
        </Show>
        <box
          flexDirection="row"
          gap={1}
          height={images().length > 0 ? 1 : 0}
          minHeight={images().length > 0 ? 1 : 0}
          flexShrink={0}
        >
          <For each={images()}>
            {(image) => (
              <box flexDirection="row" gap={1} paddingX={1} backgroundColor={theme.panelAlt}>
                <text
                  fg={theme.background}
                  bg={theme.accentAlt}
                  attributes={TextAttributes.BOLD}
                  wrapMode="none"
                >
                  {" file "}
                </text>
                <text fg={theme.muted} wrapMode="none">
                  {image.name ?? image.mime}
                </text>
                <text fg={theme.dim} wrapMode="none" onMouseDown={() => removeImage(image)}>
                  {"×"}
                </text>
              </box>
            )}
          </For>
        </box>
        <textarea
          ref={(node) => {
            area = node
          }}
          width="100%"
          height={height()}
          marginTop={props.embedded ? 1 : 0}
          minHeight={1}
          maxHeight={8}
          wrapMode="word"
          placeholder={
            props.placeholder ?? "Ask siwah to change something… (paste text or an image)"
          }
          placeholderColor={theme.dim}
          textColor={theme.text}
          focusedTextColor={theme.text}
          backgroundColor={theme.panel}
          focusedBackgroundColor={theme.panel}
          cursorColor={theme.accent}
          keyBindings={[
            { name: "n", ctrl: true, action: "newline" },
            { name: "linefeed", action: "newline" },
            { name: "return", shift: true, action: "newline" },
            { name: "return", meta: true, action: "newline" },
            { name: "return", action: "submit" },
          ]}
          onSubmit={submit}
          onContentChange={() => {
            revision += 1
            const next = area?.plainText ?? ""
            setValue(next)
            props.onChange(next)
          }}
        />
      </box>
      <Show when={!props.embedded}>
        <box height={1} flexDirection="row" gap={2} paddingLeft={1}>
          <text fg={theme.dim} wrapMode="none">
            {"enter send"}
          </text>
          <text fg={theme.dim} wrapMode="none">
            {"shift+enter newline"}
          </text>
          <Show when={dimensions().width >= 78}>
            <text fg={theme.dim} wrapMode="none">
              {"/ commands   @ files   ! shell"}
            </text>
          </Show>
        </box>
      </Show>
    </box>
  )
}

const imageExtensions: Record<string, string> = {
  ".png": "image/png",
  ".jpg": "image/jpeg",
  ".jpeg": "image/jpeg",
  ".webp": "image/webp",
  ".gif": "image/gif",
  ".bmp": "image/bmp",
}

function imageMime(path: string): string {
  const extension = path.slice(path.lastIndexOf(".")).toLowerCase()
  return imageExtensions[extension] ?? "image/png"
}

function imageFilePath(value: string): string | null {
  let path = value
    .trim()
    .replace(/^["']|["']$/g, "")
    .trim()
  if (path.startsWith("file://")) {
    try {
      path = fileURLToPath(path)
    } catch {
      return null
    }
  }
  const extension = path.slice(path.lastIndexOf(".")).toLowerCase()
  if (!(extension in imageExtensions)) return null
  return existsSync(path) ? path : null
}

function readClipboardImage(): Promise<PromptImage | null> {
  if (process.platform !== "win32") return Promise.resolve(null)
  const script =
    "Add-Type -AssemblyName System.Windows.Forms; " +
    "Add-Type -AssemblyName System.Drawing; " +
    "$name = 'clipboard.png'; $mime = 'image/png'; $data = $null; " +
    "if ([Windows.Forms.Clipboard]::ContainsFileDropList()) { " +
    "$file = [Windows.Forms.Clipboard]::GetFileDropList() | Select-Object -First 1; " +
    "if ($file -and (Test-Path $file)) { " +
    "$ext = [IO.Path]::GetExtension($file).ToLower(); " +
    "$map = @{'.png'='image/png';'.jpg'='image/jpeg';'.jpeg'='image/jpeg';'.webp'='image/webp';'.gif'='image/gif';'.bmp'='image/bmp'}; " +
    "if ($map.ContainsKey($ext)) { $mime = $map[$ext]; $name = [IO.Path]::GetFileName($file); $data = [Convert]::ToBase64String([IO.File]::ReadAllBytes($file)) } } }; " +
    "if (-not $data -and [Windows.Forms.Clipboard]::ContainsImage()) { " +
    "$img = [Windows.Forms.Clipboard]::GetImage(); $ms = New-Object System.IO.MemoryStream; " +
    "$img.Save($ms, [System.Drawing.Imaging.ImageFormat]::Png); $data = [Convert]::ToBase64String($ms.ToArray()) }; " +
    "if ($data) { @{ name = $name; mime = $mime; data = $data } | ConvertTo-Json -Compress }"
  return new Promise((resolve) => {
    execFile(
      "powershell",
      ["-NoProfile", "-STA", "-Command", script],
      { encoding: "utf8", timeout: 5000, maxBuffer: 16 * 1024 * 1024 },
      (error, stdout) => {
        const raw = stdout?.trim() ?? ""
        if (error || !raw) {
          resolve(null)
          return
        }
        try {
          const parsed = JSON.parse(raw) as { name?: string; mime?: string; data?: string }
          if (!parsed.data) {
            resolve(null)
            return
          }
          resolve({
            id: `img_${Date.now()}`,
            mime: parsed.mime ?? "image/png",
            data: parsed.data,
            name: parsed.name ?? "clipboard.png",
          })
        } catch {
          resolve(null)
        }
      },
    )
  })
}
