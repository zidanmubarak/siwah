#!/usr/bin/env sh
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
wheel=$(find "$root" -maxdepth 1 -name 'siwah-*.whl' | head -n 1)
tui=$(find "$root" -maxdepth 1 -name 'siwah-tui-*' ! -name '*.exe' | head -n 1)
if [ -z "$wheel" ]; then
  echo "siwah wheel not found next to install.sh" >&2
  exit 1
fi
if ! command -v uv >/dev/null 2>&1; then
  echo "uv is required. Install it from https://docs.astral.sh/uv/ and reopen the terminal." >&2
  exit 1
fi

uv tool install --force "$wheel"
if [ -n "$tui" ]; then
  data_home=${SIWAH_HOME:-$HOME/.siwah}
  mkdir -p "$data_home/bin"
  cp "$tui" "$data_home/bin/siwah-tui"
  chmod +x "$data_home/bin/siwah-tui"
  echo "TUI binary installed to $data_home/bin/siwah-tui"
fi

echo "siwah installed. Check with: siwah --version"
echo "If the command is not found, run: uv tool update-shell"
