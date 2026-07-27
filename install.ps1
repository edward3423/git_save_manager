# Git Save Manager - installer and updater for Windows.
#
#   irm https://raw.githubusercontent.com/edward3423/git_save_manager/main/install.ps1 | iex
#
# Running it again updates an existing install in place. Nothing is read from the console
# and nothing prompts, so it is safe to pipe.
#
# Environment overrides (all optional, set before running):
#   $env:GSM_PREFIX       install root, default $env:LOCALAPPDATA\Programs
#                         -> checkout at <prefix>\git-save-manager
#                         -> launcher at <prefix>\bin\gsm.cmd
#   $env:GSM_HOME         override the checkout location on its own
#   $env:GSM_REPO         clone URL, default the public GitHub repository
#   $env:GSM_REF          branch or tag to track, default main
#   $env:GSM_NO_SHORTCUT  set to 1 to skip the Start Menu shortcut
#   $env:GSM_NO_PATH      set to 1 to leave the user PATH untouched

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Say  { param([string]$m) Write-Host "==> $m" }
function Warn { param([string]$m) Write-Warning $m }
function Die  { param([string]$m) Write-Host "error: $m" -ForegroundColor Red; exit 1 }

function Env-Or { param([string]$name, [string]$fallback)
    $v = [Environment]::GetEnvironmentVariable($name)
    if ([string]::IsNullOrWhiteSpace($v)) { return $fallback } else { return $v }
}

# --- platform ---------------------------------------------------------------

if ($PSVersionTable.PSVersion.Major -lt 5) {
    Die "PowerShell 5 or newer is required. Found $($PSVersionTable.PSVersion)."
}
if ($PSVersionTable.PSEdition -eq 'Core' -and -not $IsWindows) {
    Die "This script is for Windows. On Linux and macOS use install.sh."
}

$RepoDefault = 'https://github.com/edward3423/git_save_manager.git'
$Prefix   = Env-Or 'GSM_PREFIX' (Join-Path $env:LOCALAPPDATA 'Programs')
$GsmHome  = Env-Or 'GSM_HOME'   (Join-Path $Prefix 'git-save-manager')
$GsmRepo  = Env-Or 'GSM_REPO'   $RepoDefault
$GsmRef   = Env-Or 'GSM_REF'    'main'
$BinDir   = Join-Path $Prefix 'bin'
$Launcher = Join-Path $BinDir 'gsm.cmd'

Say "Installing to $GsmHome"

# --- prerequisites ----------------------------------------------------------
#
# Unlike Linux there are no Qt or keyring system packages to chase: PyQt6's wheels ship
# the Windows platform plugin, and the token goes to the built-in Windows Credential
# Manager. Only git has to be present. We never install it for you.

Say "Checking system dependencies"
$git = Get-Command git -ErrorAction SilentlyContinue
if (-not $git) {
    Warn "git was not found on PATH."
    Write-Host "    fix: winget install --id Git.Git -e --source winget"
    Write-Host "    then open a new terminal and re-run this command."
    Die "git is required."
}
Say "System dependencies look fine"

# --- uv ---------------------------------------------------------------------

function Resolve-Uv {
    $cmd = Get-Command uv -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    foreach ($candidate in @(
        (Join-Path $env:USERPROFILE '.local\bin\uv.exe'),
        (Join-Path $env:USERPROFILE '.cargo\bin\uv.exe')
    )) {
        if (Test-Path $candidate) { return $candidate }
    }
    return $null
}

$Uv = Resolve-Uv
if (-not $Uv) {
    Say "Installing uv (no uv found on PATH)"
    $prev = $env:INSTALLER_NO_MODIFY_PATH
    try {
        Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
    } finally {
        $env:INSTALLER_NO_MODIFY_PATH = $prev
    }
    $Uv = Resolve-Uv
    if (-not $Uv) { Die "uv installation finished but uv.exe was not found." }
    Say "Installed uv at $Uv"
} else {
    Say "Found uv at $Uv"
}

# --- checkout ---------------------------------------------------------------
#
# All runtime state lives in the gitignored data\ directory inside the checkout, so an
# update must never re-clone over an existing install. Fetch and hard-reset instead:
# data\ is untracked and is left alone.

function Git-Run { param([string[]]$GitArgs)
    & git @GitArgs
    if ($LASTEXITCODE -ne 0) { Die "git $($GitArgs -join ' ') failed with exit code $LASTEXITCODE" }
}

if (Test-Path (Join-Path $GsmHome '.git')) {
    Say "Updating existing install at $GsmHome"
    Git-Run @('-C', $GsmHome, 'remote', 'set-url', 'origin', $GsmRepo)
    Git-Run @('-C', $GsmHome, 'fetch', '--quiet', 'origin', $GsmRef)
    Git-Run @('-C', $GsmHome, 'checkout', '--quiet', '-B', $GsmRef, "origin/$GsmRef")
    Git-Run @('-C', $GsmHome, 'reset', '--quiet', '--hard', "origin/$GsmRef")
} elseif ((Test-Path $GsmHome) -and (Get-ChildItem -Force $GsmHome | Select-Object -First 1)) {
    Die "$GsmHome exists and is not a Git checkout. Move it aside and re-run."
} else {
    Say "Cloning $GsmRepo into $GsmHome"
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $GsmHome) | Out-Null
    Git-Run @('clone', '--quiet', '--branch', $GsmRef, $GsmRepo, $GsmHome)
}

$version = (& git -C $GsmHome rev-parse --short HEAD).Trim()
Say "Installed version: $version"

# --- dependencies -----------------------------------------------------------

Say "Syncing Python dependencies with uv"
& $Uv sync --project $GsmHome --quiet
if ($LASTEXITCODE -ne 0) { Die "uv sync failed with exit code $LASTEXITCODE" }

# --- launcher ---------------------------------------------------------------

Say "Writing launcher $Launcher"
New-Item -ItemType Directory -Force -Path $BinDir | Out-Null
$cmd = @"
@echo off
REM Generated by the Git Save Manager installer. Re-run install.ps1 to regenerate.
"$Uv" run --project "$GsmHome" --directory "$GsmHome" python main.py %*
"@
Set-Content -Path $Launcher -Value $cmd -Encoding ASCII

# --- PATH -------------------------------------------------------------------

if ((Env-Or 'GSM_NO_PATH' '0') -ne '1') {
    $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
    if (-not $userPath) { $userPath = '' }
    $entries = $userPath.Split(';') | Where-Object { $_ -ne '' }
    if ($entries -notcontains $BinDir) {
        Say "Adding $BinDir to your user PATH"
        $newPath = if ($userPath.TrimEnd(';') -eq '') { $BinDir } else { $userPath.TrimEnd(';') + ';' + $BinDir }
        [Environment]::SetEnvironmentVariable('Path', $newPath, 'User')
        Warn "Open a new terminal before the 'gsm' command is available."
    }
} elseif (($env:Path -split ';') -notcontains $BinDir) {
    Warn "$BinDir is not on your PATH. Add it manually to use the 'gsm' command."
}

# --- Start Menu shortcut ----------------------------------------------------
#
# The shortcut runs pythonw rather than python so no console window appears behind
# the Qt window.

if ((Env-Or 'GSM_NO_SHORTCUT' '0') -ne '1') {
    $startMenu = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'
    $lnk = Join-Path $startMenu 'Git Save Manager.lnk'
    Say "Writing Start Menu shortcut $lnk"
    New-Item -ItemType Directory -Force -Path $startMenu | Out-Null
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($lnk)
    $shortcut.TargetPath = $Uv
    $shortcut.Arguments = "run --project `"$GsmHome`" --directory `"$GsmHome`" pythonw main.py"
    $shortcut.WorkingDirectory = $GsmHome
    $shortcut.Description = 'Version game saves and application settings in a private Git repository'
    $shortcut.WindowStyle = 7
    $shortcut.Save()
}

# --- done -------------------------------------------------------------------

Say "Done."
Say "Launch with: gsm   (or the Start Menu entry)"
Say "Update later by re-running the same install command."
