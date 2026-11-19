# One-line installer for the public siwah repository.
#
#   irm https://raw.githubusercontent.com/zidanmubarak/siwah/main/scripts/install/bootstrap.ps1 | iex
#
# The script installs uv and Bun when missing, keeps an editable checkout in
# ~/.siwah/src, installs the `siwah` command, and builds the TUI binary. It uses
# a normal HTTPS git clone; no GitHub CLI or account is required.

$ErrorActionPreference = "Stop"

$Repo = if ($env:SIWAH_REPO) { $env:SIWAH_REPO } else { "zidanmubarak/siwah" }
$RepoUrl = if ($env:SIWAH_REPO_URL) { $env:SIWAH_REPO_URL } else { "https://github.com/$Repo.git" }
$Branch = if ($env:SIWAH_BRANCH) { $env:SIWAH_BRANCH } else { "main" }
$DataHome = if ($env:SIWAH_HOME) { $env:SIWAH_HOME } else { Join-Path $HOME ".siwah" }
$Source = if ($env:SIWAH_SOURCE) { $env:SIWAH_SOURCE } else { Join-Path $DataHome "src" }
$BinDir = Join-Path $DataHome "bin"
$TuiName = "siwah-tui.exe"

# Bun's installer rewrites the process PATH from the user registry, which drops
# machine-level entries such as C:\Program Files\Git\cmd. Keep every original
# source and merge it back after each installer.
$script:BasePath = @(
    $env:Path,
    [Environment]::GetEnvironmentVariable("Path", "Machine"),
    [Environment]::GetEnvironmentVariable("Path", "User")
) -join ";"

function Write-Step([string]$Message) {
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Merge-Path {
    $parts = New-Object System.Collections.Generic.List[string]
    $seen = @{}
    foreach ($block in @($script:BasePath, $env:Path)) {
        if (-not $block) { continue }
        foreach ($entry in ($block -split ";")) {
            if ($entry -and -not $seen.ContainsKey($entry)) {
                $seen[$entry] = $true
                $parts.Add($entry)
            }
        }
    }
    $env:Path = $parts -join ";"
}

function Add-LocalPath {
    foreach ($candidate in @((Join-Path $HOME ".local\bin"), (Join-Path $HOME ".bun\bin"))) {
        if ((Test-Path $candidate) -and ($env:Path -notlike "*$candidate*")) {
            $env:Path = "$candidate;$env:Path"
        }
    }
}

function Get-CommandPath([string]$Name) {
    $command = Get-Command $Name -ErrorAction SilentlyContinue
    if (-not $command) { return $null }
    if ($command.Source) { return $command.Source }
    return $command.Path
}

function Find-Git {
    $found = Get-CommandPath "git"
    if ($found) { return $found }
    foreach ($candidate in @(
        "$env:ProgramFiles\Git\cmd\git.exe",
        "${env:ProgramFiles(x86)}\Git\cmd\git.exe",
        "$env:LOCALAPPDATA\Programs\Git\cmd\git.exe"
    )) {
        if ($candidate -and (Test-Path $candidate)) { return $candidate }
    }
    throw "git is required. Install Git from https://git-scm.com/ and reopen the terminal."
}

Write-Step "Checking prerequisites"
Merge-Path
$Git = Find-Git

$UvCommand = Get-CommandPath "uv"
if (-not $UvCommand) {
    Write-Step "Installing uv"
    Invoke-RestMethod "https://astral.sh/uv/install.ps1" | Invoke-Expression
    Add-LocalPath
    Merge-Path
    $UvCommand = Get-CommandPath "uv"
}
if (-not $UvCommand) {
    throw "uv is required. Install it from https://docs.astral.sh/uv/ and reopen the terminal."
}

$BunCommand = $null
if ($env:SIWAH_SKIP_TUI -ne "1") {
    $BunCommand = Get-CommandPath "bun"
    if (-not $BunCommand) {
        Write-Step "Installing Bun"
        Invoke-RestMethod "https://bun.sh/install.ps1" | Invoke-Expression
        Add-LocalPath
        Merge-Path
        $BunCommand = Get-CommandPath "bun"
    }
    if (-not $BunCommand) {
        throw "Bun is required. Install it from https://bun.sh/ and reopen the terminal."
    }
}

$needsClone = $false
if (Test-Path (Join-Path $Source ".git")) {
    Write-Step "Updating $Source"
    & $Git -C $Source fetch --tags --prune origin
    if ($LASTEXITCODE -ne 0) { throw "Could not fetch $Repo. Check your network or git credentials." }
    & $Git -C $Source merge --ff-only "origin/$Branch"
    if ($LASTEXITCODE -ne 0) { throw "Could not fast-forward $Source. Commit or stash local changes first." }
}
else {
    if (Test-Path $Source) {
        if (@(Get-ChildItem -LiteralPath $Source -Force).Count -gt 0) {
            throw "$Source exists but is not a git checkout. Move it aside or set SIWAH_SOURCE."
        }
        Remove-Item -LiteralPath $Source -Force
    }
    $needsClone = $true
}

if ($needsClone) {
    Write-Step "Cloning $Repo into $Source"
    New-Item -ItemType Directory -Force (Split-Path $Source) | Out-Null
    & $Git clone --branch $Branch $RepoUrl $Source
    if ($LASTEXITCODE -ne 0) { throw "Could not clone $Repo. Check your network or git credentials." }
}

Write-Step "Installing the editable siwah tool"
& $UvCommand tool install --force --editable $Source
if ($LASTEXITCODE -ne 0) { throw "uv tool install failed." }

if ($env:SIWAH_SKIP_TUI -ne "1") {
    Write-Step "Building the TUI"
    Push-Location (Join-Path $Source "ui\tui")
    try {
        & $BunCommand install --frozen-lockfile
        if ($LASTEXITCODE -ne 0) { throw "bun install failed." }
        & $BunCommand run build
        if ($LASTEXITCODE -ne 0) { throw "TUI build failed." }
    }
    finally {
        Pop-Location
    }
    $built = Join-Path $Source "ui\tui\dist\$TuiName"
    if (-not (Test-Path $built)) { throw "TUI build did not produce $built." }
    New-Item -ItemType Directory -Force $BinDir | Out-Null
    Copy-Item $built (Join-Path $BinDir $TuiName) -Force
    Write-Host "TUI binary installed to $(Join-Path $BinDir $TuiName)"
}

Write-Step "Making the siwah command available"
if ($env:SIWAH_SKIP_SHELL -ne "1") {
    # uv writes the PATH update notice to stderr; Windows PowerShell 5.1 turns
    # native stderr into a terminating error when ErrorActionPreference is Stop.
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & $UvCommand tool update-shell *> $null
    }
    finally {
        $ErrorActionPreference = $previous
    }
}
$toolBin = (& $UvCommand tool dir --bin).Trim()
$env:Path = "$toolBin;$env:Path"
$siwah = Join-Path $toolBin "siwah.exe"
if (-not (Test-Path $siwah)) { throw "siwah was installed but its shim was not found in $toolBin." }
& $siwah --version

Write-Host ""
Write-Host "siwah is installed. Open a new terminal and run: siwah" -ForegroundColor Green
Write-Host "The source checkout is $Source; /update and 'siwah update --apply' use it."
