# siwah TUI

OpenTUI + Solid terminal interface for the siwah daemon.

## Run

```bash
# from the repository root
uv run siwah                 # launcher: starts the daemon if needed, then the TUI
# or with an existing daemon
uv run siwah serve           # in one terminal
SIWAH_SERVER=http://127.0.0.1:8765 SIWAH_TOKEN=<token> \
  bun --conditions=browser ui/tui/src/index.tsx
```

The launcher uses `ui/tui/dist/siwah-tui[.exe]` when it exists, otherwise Bun
source. It requires Bun 1.3.14+ (or a compiled binary) and a terminal. Use
`siwah run "<prompt>" --ui plain` when no terminal is available.

Build the compiled binary with `bun run build` (see `build.ts`). It must use
`Bun.build` with `@opentui/solid/bun-plugin`; plain `bun build --compile`
breaks Solid reactivity because Bun's JSX transform evaluates expressions once.
The build bundles the current platform's OpenTUI native package and externalizes
the others, so compiled output targets Windows, macOS and glibc Linux. On musl,
run the Bun source instead.

An installed wheel does not include the TUI binary. The launcher searches
`SIWAH_TUI_BINARY`, `~/.siwah/bin/siwah-tui[.exe]`, the executable directory,
PATH, and the repository `ui/tui/dist`; place the release binary in
`~/.siwah/bin` or set the override. The wheel-only fallback is
`siwah run "..." --ui plain`.

Environment:

| Variable | Purpose |
|---|---|
| `SIWAH_SERVER`, `SIWAH_TOKEN` | Explicit daemon endpoint and local token |
| `SIWAH_HOME` | Isolated `~/.siwah` directory (tests and demos) |
| `SIWAH_SESSION` | Open this session at startup |
| `SIWAH_WORKSPACE` | Workspace for sessions created by the TUI |
| `SIWAH_THEME` | `siwah-dark` (default) or `siwah-light` |
| `NO_COLOR` | Selects the monochrome theme |

## Features

- Streaming markdown (headings, ordered/unordered/task lists, quotes, fenced and
  inline code, bold, italic, strikethrough, links) with reasoning folds and
  inline tool cards: a short command/path summary, an 8-line preview, and
  click-anywhere expansion per card (up to 1,000 lines). Tool headers are bold;
  `read_file` output uses syntax-highlighted code. One-line reasoning is plain
  text without a fold.
- Custom motion language: a `▁▂▃▄` dune pulse for running tools and reasoning,
  and a moving sand wave beside the model in the status bar while a run is
  active.
- Status bar: model (plus connection when not online) and project/branch.
  Context, tokens, and allowance live in the prompt's top-right stats.
- Multi-line prompt; Enter send; Ctrl+N newline (Shift+Enter where the terminal
  reports it, plus Alt+Enter and `\`+Enter fallbacks); `↑` recalls the last
  prompt and Ctrl+R opens the full history;
  `/` fuzzy command palette; `@` workspace file completion;
  `!` direct shell; `#` quick memory note; Tab build/plan; Ctrl+P command palette;
  Ctrl+R history; Esc cancel; Ctrl+C copies a transcript or prompt selection,
  then twice exits when nothing is selected. Click anywhere in a tool card to
  expand or collapse it.
- Inline permission panel above the prompt with command/diff preview and
  once/session/always/deny choices; it does not cover the transcript.
- Two-column model and session lists with ellipsis and no mid-word wrapping.
  Model rows show the model id plus provider/context/output/tools/privacy/pricing;
  session rows show relative time, model, and workspace name. Sessions are
  project-scoped by default; Tab in `/sessions` toggles all projects.
- Prompt history is persisted per project, so Ctrl+R survives restarts and
  imports prompts from the last opened project session.
- Unified diff below 120 columns, side-by-side at 120+ columns.
- `@` file completion works before the first session; the launcher pins the
  workspace to the directory where `siwah` was invoked.
- Live todo/plan panel when terminal width is at least 100.
- `/connect` lists configured providers, explains where provider config lives,
  and masks typed or pasted API keys as `*` (the key is never rendered).
- Panels: sessions, models, cost, evolve, checkpoints, skills, memory, connect,
  doctor, theme. Model lists include context/output, tool support, privacy and
  pricing hints; providers that fail are shown as unavailable instead of
  silently disappearing.
- Five themes including a NO_COLOR mono theme; information always has a text or
  symbol channel, not only color. The default `siwah-dark` uses desert amber
  `#E0A35C` with oasis teal `#7FB3A6` on a deep `#080706` background.

`/skills` lists harness skills and opens their full body; `/memory` shows user,
project, and quick memory; `/init` creates `<repo>/.siwah/project.md`; and `#`
appends a quick note through the daemon. `/checkpoints` lists the shadow-git
snapshots; Enter opens a confirmation, `f` forks the session from that
checkpoint, and `/undo` or `/rewind` restores an earlier state. `/compact`
persists a structured context summary. `/plan` switches to read-only plan mode;
the run writes `<repo>/.siwah/plan.md`, which the user can edit, and
`/execute-plan` sends that file back for build-mode execution.

## Providers

On first run the daemon writes `~/.siwah/harness/config.toml` with `opencode-go` and
`openai` templates. `/connect` lists those providers; selecting one stores the
key in `~/.siwah/auth.json` and models are read from the live endpoint. No
manual TOML edit is needed. If an already-running daemon has an empty provider
map, open `/connect` and press Enter to create the defaults.

## Configuration

Optional `~/.siwah/harness/tui.json` (or `SIWAH_TUI_CONFIG`) overrides keybinds
and the startup theme:

```json
{
  "theme": "siwah-dark",
  "keybinds": {
    "palette": "ctrl+p",
    "history": "ctrl+r",
    "help": "?",
    "mode": "tab",
    "cancel": "escape",
    "exit": "ctrl+c"
  }
}
```

Missing or invalid values fall back to the defaults. `NO_COLOR` still selects
the mono theme, and 256/16-color terminals get palette-approximated colors.

## Tests and benchmarks

```bash
cd ui/tui
bun install
bun run typecheck
bun run lint
bun run test                 # snapshots + resize/paste/typing + 10k inline output
bun run build                # compiled binary; needs the OpenTUI Solid plugin
bun run bench                # streaming frame/echo/resize metrics
```

Runtime warnings are written to
`$SIWAH_HOME/logs/tui-warnings.log` (default `~/.siwah/logs/tui-warnings.log`)
instead of stderr, so they cannot overwrite the prompt.

`docs/TUI_MATRIX.md` records the terminal coverage that could be verified on the
development machines. `docs/tui/demo.cast` is an asciinema v2 recording of a
full task through the real `siwah` launcher in a Windows PTY: prompt, tool
cards, colored diff, final answer, and clean exit. It uses the local mock
provider, not a paid live model. The recording predates the current inline
transcript, so it still shows the original click-to-open pager.
