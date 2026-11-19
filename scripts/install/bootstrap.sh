#!/usr/bin/env sh
# One-line installer for the public siwah repository.
#
#   curl -fsSL https://raw.githubusercontent.com/zidanmubarak/siwah/main/scripts/install/bootstrap.sh | bash
#
# The script installs uv and Bun when missing, keeps an editable checkout in
# ~/.siwah/src, installs the `siwah` command, and builds the TUI binary. It uses
# a normal HTTPS git clone; no GitHub CLI or account is required.
set -eu

REPO=${SIWAH_REPO:-zidanmubarak/siwah}
REPO_URL=${SIWAH_REPO_URL:-https://github.com/$REPO.git}
BRANCH=${SIWAH_BRANCH:-main}
DATA_HOME=${SIWAH_HOME:-$HOME/.siwah}
SOURCE=${SIWAH_SOURCE:-$DATA_HOME/src}
BIN_DIR=$DATA_HOME/bin
TUI_NAME=siwah-tui

step() {
  printf '==> %s\n' "$1"
}

fail() {
  printf 'error: %s\n' "$1" >&2
  exit 1
}

add_local_path() {
  for candidate in "$HOME/.local/bin" "$HOME/.cargo/bin" "$HOME/.bun/bin"; do
    if [ -d "$candidate" ]; then
      case ":$PATH:" in
        *":$candidate:"*) ;;
        *) PATH="$candidate:$PATH" ;;
      esac
    fi
  done
  export PATH
}

step "Checking prerequisites"
GIT=$(command -v git || true)
[ -n "$GIT" ] || fail "git is required. Install it and reopen the terminal."
add_local_path

if ! command -v uv >/dev/null 2>&1; then
  step "Installing uv"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  add_local_path
fi
UV=$(command -v uv || true)
if [ -z "$UV" ]; then
  for candidate in "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv"; do
    [ -x "$candidate" ] && UV=$candidate && break
  done
fi
[ -n "$UV" ] || fail "uv is required. Install it from https://docs.astral.sh/uv/."

BUN=""
if [ "${SIWAH_SKIP_TUI:-0}" != "1" ]; then
  if ! command -v bun >/dev/null 2>&1; then
    step "Installing Bun"
    curl -fsSL https://bun.sh/install | bash
    add_local_path
  fi
  BUN=$(command -v bun || true)
  if [ -z "$BUN" ] && [ -x "$HOME/.bun/bin/bun" ]; then
    BUN=$HOME/.bun/bin/bun
  fi
  [ -n "$BUN" ] || fail "Bun is required. Install it from https://bun.sh/."
fi

needs_clone=0
if [ -d "$SOURCE/.git" ]; then
  step "Updating $SOURCE"
  "$GIT" -C "$SOURCE" fetch --tags --prune origin
  "$GIT" -C "$SOURCE" merge --ff-only "origin/$BRANCH"
else
  if [ -e "$SOURCE" ]; then
    if [ -n "$(ls -A "$SOURCE" 2>/dev/null)" ]; then
      fail "$SOURCE exists but is not a git checkout. Move it aside or set SIWAH_SOURCE."
    fi
    rmdir "$SOURCE"
  fi
  needs_clone=1
fi

if [ "$needs_clone" = "1" ]; then
  step "Cloning $REPO into $SOURCE"
  mkdir -p "$(dirname "$SOURCE")"
  "$GIT" clone --branch "$BRANCH" "$REPO_URL" "$SOURCE"
fi

step "Installing the editable siwah tool"
"$UV" tool install --force --editable "$SOURCE"

if [ "${SIWAH_SKIP_TUI:-0}" != "1" ]; then
  step "Building the TUI"
  (
    cd "$SOURCE/ui/tui"
    "$BUN" install --frozen-lockfile
    "$BUN" run build
  )
  built="$SOURCE/ui/tui/dist/$TUI_NAME"
  [ -f "$built" ] || fail "TUI build did not produce $built."
  mkdir -p "$BIN_DIR"
  cp "$built" "$BIN_DIR/$TUI_NAME"
  chmod +x "$BIN_DIR/$TUI_NAME"
  printf 'TUI binary installed to %s\n' "$BIN_DIR/$TUI_NAME"
fi

step "Making the siwah command available"
if [ "${SIWAH_SKIP_SHELL:-0}" != "1" ]; then
  "$UV" tool update-shell >/dev/null 2>&1 || true
fi
TOOL_BIN=$("$UV" tool dir --bin)
PATH="$TOOL_BIN:$PATH"
export PATH
"$TOOL_BIN/siwah" --version

printf '\n'
printf 'siwah is installed. Open a new terminal and run: siwah\n'
printf 'The source checkout is %s; /update and siwah update --apply use it.\n' "$SOURCE"
