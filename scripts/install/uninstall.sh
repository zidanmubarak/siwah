#!/usr/bin/env sh
# One-line uninstaller for siwah.
#
#   curl -fsSL https://raw.githubusercontent.com/zidanmubarak/siwah/main/scripts/install/uninstall.sh | sh
#
# Remove the uv tool install and, unless SIWAH_KEEP_DATA=1, every local siwah
# file under ~/.siwah. This includes sessions, memory, credentials, the TUI
# binary, and the editable source checkout. It does not remove uv or Bun.
set -eu

DATA_HOME=${SIWAH_HOME:-$HOME/.siwah}
KEEP_DATA=${SIWAH_KEEP_DATA:-0}

resolve_path() {
  python3 -c 'import os,sys; print(os.path.abspath(os.path.expanduser(sys.argv[1])))' "$1" 2>/dev/null || \
    python -c 'import os,sys; print(os.path.abspath(os.path.expanduser(sys.argv[1])))' "$1" 2>/dev/null || \
    printf '%s\n' "$1"
}

RESOLVED=$(resolve_path "$DATA_HOME")
if [ "$KEEP_DATA" != "1" ]; then
  case "$RESOLVED" in
    ""|"/"|"$HOME") printf 'error: refusing to remove unsafe data path: %s\n' "$RESOLVED" >&2; exit 1 ;;
  esac
fi

RUNTIME="$RESOLVED/runtime.json"
if [ -f "$RUNTIME" ]; then
  PID=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("pid",""))' "$RUNTIME" 2>/dev/null || \
        python -c 'import json,sys; print(json.load(open(sys.argv[1])).get("pid",""))' "$RUNTIME" 2>/dev/null || true)
  if [ -n "$PID" ]; then
    kill "$PID" 2>/dev/null || true
    sleep 1
    printf 'Stopped the siwah daemon (pid %s).\n' "$PID"
  fi
fi

if command -v uv >/dev/null 2>&1; then
  if uv tool uninstall siwah >/dev/null 2>&1; then
    printf 'Removed the siwah command.\n'
  else
    printf 'Could not remove the siwah command with uv.\n' >&2
  fi
else
  printf 'uv was not found; remove the siwah package with the installer you used.\n' >&2
fi

if [ "$KEEP_DATA" = "1" ]; then
  printf 'Kept local data at %s (SIWAH_KEEP_DATA=1).\n' "$RESOLVED"
else
  rm -rf -- "$RESOLVED"
  printf 'Removed local data at %s.\n' "$RESOLVED"
fi

printf 'siwah has been uninstalled.\n'
