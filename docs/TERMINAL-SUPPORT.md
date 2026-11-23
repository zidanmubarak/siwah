# Terminal support

siwah's TUI is built with OpenTUI and Solid. It targets modern terminals with
ANSI, Unicode, and reasonably fast rendering. This page records what was actually
verified for the 1.0.3 release and what remains untested.

“Verified” means there is release evidence for that environment: an end-to-end
PTY run, a captured terminal session, or the TUI test and snapshot suite. It is
not a guarantee that every font, multiplexer setting, or remote configuration
will behave identically.

## Matrix

| Environment | Status | Evidence |
|---|---|---|
| Windows 11 x64, compiled TUI, WinPTY | **Verified end-to-end** | Full task through the launcher: prompt, tools, diff, answer, clean exit |
| Windows Terminal 1.24 | **Verified** | Real ConPTY and Win32 console input/screen capture |
| Windows 11 x64, virtual renderer | **Verified** | TUI tests, snapshots, streaming benchmark, resize, paste, typing, 10k-line output |
| Ubuntu 26.04 WSL 2 x64, virtual renderer | **Verified** | TUI tests pass with Linux OpenTUI artifacts |
| tmux 3.6 in WSL 2 | **Verified** | Help, skills, memory, shell permission, 60x20 resize, detach/reattach, clean exit |
| SSH to WSL (OpenSSH 10.2p1) | **Verified** | `xterm-256color` and `xterm` with `NO_COLOR` |
| macOS Terminal (Terminal.app) | **Not verified** | No test hardware at release |
| iTerm2 | **Not verified** | No test hardware at release |

macOS Terminal and iTerm2 are untested, not declared unsupported. If you can run
the checklist below on a Mac, a report with the terminal name, version, font, and
a capture is welcome.

## What was exercised

- A first-run flow with a fresh home: connect a provider, choose a model, send a
  prompt, receive a final answer.
- Streaming Markdown and reasoning folds.
- Tool cards, expansion, and a 10,000-line inline output pager.
- Unified diffs below 120 columns and side-by-side diffs at 120+.
- Resize from 100x30 to 60x20 to 120x34 without losing prompt text.
- Bracketed paste and fast typing.
- Permission requests with once/session/always/deny choices.
- `NO_COLOR`, 16-color, 256-color, and truecolor detection.
- 60-column layout and a 110x38 snapshot.
- tmux detach and reattach.
- SSH with `xterm-256color` and with `NO_COLOR`.

## Display behavior

- **Width:** the layout is designed for at least 60 columns. The side panels and
  split diff need more room; below 120 columns siwah uses a unified diff.
- **Color:** siwah detects truecolor, 256-color, and 16-color terminals and
  approximates the palette when needed. `NO_COLOR` selects the monochrome theme.
- **Unicode:** the interface uses box drawing, block elements, and a pixel-art
  welcome emblem. A font with reasonable Unicode coverage is recommended.
- **Mouse:** clicking tool cards and panels depends on the terminal's mouse
  reporting. Keyboard alternatives exist for the important actions.
- **Paste:** bracketed paste is supported. Image paste depends on the terminal's
  clipboard behavior; pasting a local image path is converted to an attachment.
- **Shift+Enter:** some terminals do not report it distinctly. `Ctrl+N`,
  `Alt+Enter`, and `\` followed by Enter are documented fallbacks.

The repository includes `assets/terminal-preview.svg`, a static preview. It is
not a recording and may not match your font, theme, or terminal exactly.

## Recommended setups

- **Windows:** Windows Terminal. The legacy console host may work but is not the
  tested target. Git Bash is recommended for the `bash` tool so POSIX syntax
  behaves consistently.
- **macOS:** iTerm2 or Terminal.app are expected to work but are untested at
  1.0.3. Use a UTF-8 locale.
- **Linux:** a modern terminal with truecolor. tmux and SSH are verified, but
  nested multiplexers and unusual `TERM` values can change color and key
  behavior.
- **Remote:** SSH with `xterm-256color` is the best-tested configuration. Use
  `NO_COLOR=1` if color detection is wrong.

## Minimum expectations

- UTF-8 output.
- ANSI escape sequences and alternate-screen support (OpenTUI uses the terminal
  renderer; a bare pipe is not a TUI).
- At least 60 columns and 20 rows.
- A terminal that reports window size changes.

If the terminal does not meet these, use the non-interactive fallback:

```bash
siwah run "your task" --ui plain
```

`--ui plain` needs no Bun, no TUI binary, and no alternate screen. It is the
supported path for CI, logs, and screen-reader workflows.

## Reporting a terminal problem

Please include:

- siwah version (`siwah --version`);
- operating system and terminal name/version;
- whether you used the compiled binary or Bun source;
- `TERM`, `COLORTERM`, and `NO_COLOR` values;
- terminal size and whether resizing triggers the problem;
- a screenshot or text capture if possible;
- whether `--ui plain` works.

Open an issue using the bug report template. Do not include secrets or full
transcripts with private code.

## Known gaps

- macOS Terminal and iTerm2 are not verified.
- The Windows console host outside Windows Terminal is not a tested target.
- Some terminals do not report Shift+Enter or mouse events consistently.
- Very narrow terminals below 60 columns are not a supported layout.
- The compiled TUI is platform-specific. On musl Linux, run from Bun source.
- The wheel does not bundle the TUI binary; install it separately or use
  `--ui plain`.
