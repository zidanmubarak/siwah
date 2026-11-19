# Install siwah from this bundle.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$wheel = Get-ChildItem $root -Filter "siwah-*.whl" | Select-Object -First 1
$tui = Get-ChildItem $root -Filter "siwah-tui-*.exe" | Select-Object -First 1
if (-not $wheel) { throw "siwah wheel not found next to install.ps1" }
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw "uv is required. Install it from https://docs.astral.sh/uv/ and reopen the terminal."
}

uv tool install --force $wheel.FullName
if ($tui) {
    $dataHome = if ($env:SIWAH_HOME) { $env:SIWAH_HOME } else { Join-Path $HOME ".siwah" }
    $bin = Join-Path $dataHome "bin"
    New-Item -ItemType Directory -Force $bin | Out-Null
    Copy-Item $tui.FullName (Join-Path $bin "siwah-tui.exe") -Force
    Write-Host "TUI binary installed to $bin\siwah-tui.exe"
}

Write-Host "siwah installed. Check with: siwah --version"
Write-Host "If the command is not found, run: uv tool update-shell"
