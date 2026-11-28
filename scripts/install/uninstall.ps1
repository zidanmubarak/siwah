# One-line uninstaller for siwah.
#
#   irm https://raw.githubusercontent.com/zidanmubarak/siwah/main/scripts/install/uninstall.ps1 | iex
#
# Remove the uv tool install and, unless SIWAH_KEEP_DATA=1, every local siwah
# file under ~/.siwah. This includes sessions, memory, credentials, the TUI
# binary, and the editable source checkout. It does not remove uv or Bun.

$ErrorActionPreference = "Stop"

$DataHome = if ($env:SIWAH_HOME) { $env:SIWAH_HOME } else { Join-Path $HOME ".siwah" }
$KeepData = $env:SIWAH_KEEP_DATA -eq "1"
$resolved = [IO.Path]::GetFullPath($DataHome)
$homePath = [IO.Path]::GetFullPath($HOME)
if (-not $KeepData -and ($resolved -eq $homePath -or $resolved -eq [IO.Path]::GetPathRoot($resolved))) {
    throw "Refusing to remove unsafe data path: $resolved"
}

$Runtime = Join-Path $resolved "runtime.json"
if (Test-Path $Runtime) {
    try {
        $state = Get-Content -LiteralPath $Runtime -Raw | ConvertFrom-Json
        $pidValue = [int]$state.pid
        if ($pidValue -gt 0) {
            & taskkill /PID $pidValue /T /F *> $null
            Write-Host "Stopped the siwah daemon (pid $pidValue)."
        }
    }
    catch {
        # A stale or partial runtime file should not block uninstall.
    }
}

$Uv = Get-Command uv -ErrorAction SilentlyContinue
if ($Uv) {
    $Source = $Uv.Source
    if (-not $Source) { $Source = $Uv.Path }
    try {
        & $Source tool uninstall siwah *> $null
        if ($LASTEXITCODE -eq 0) {
            Write-Host "Removed the siwah command."
        }
        else {
            Write-Host "Could not remove the siwah command with uv." -ForegroundColor Yellow
        }
    }
    catch {
        Write-Host "Could not remove the siwah command with uv." -ForegroundColor Yellow
    }
}
else {
    Write-Host "uv was not found; remove the siwah package with the installer you used." -ForegroundColor Yellow
}

if ($KeepData) {
    Write-Host "Kept local data at $resolved (SIWAH_KEEP_DATA=1)."
}
else {
    if (Test-Path $resolved) {
        Remove-Item -LiteralPath $resolved -Recurse -Force
        Write-Host "Removed local data at $resolved."
    }
}

Write-Host "siwah has been uninstalled." -ForegroundColor Green
