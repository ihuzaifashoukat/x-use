<#
.SYNOPSIS
    One-click installer for x-use on Windows.

.DESCRIPTION
    1. Preflight: git and Python >= 3.10 must be present.
    2. Clone the repo (or reuse/update an existing clone, or install in
       place when run from inside the repo).
    3. Bootstrap uv locally when needed and synchronize .venv from uv.lock.
    4. Install Chromium for the configured Patchright or Playwright backend.
    5. Create missing sample config and run doctor; existing config is retained.

    Re-running reuses the checkout and environment. No global Python packages
    or administrator changes are required.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File install.ps1

.EXAMPLE
    iex "& { $(irm https://raw.githubusercontent.com/ihuzaifashoukat/x-use/main/install.ps1) }"

.EXAMPLE
    .\install.ps1 -Dir C:\tools\x-use -Dev -Update
#>
[CmdletBinding()]
param(
    # Install directory. Defaults to .\x-use, or the current directory when
    # run from inside the repo. Env: XUSE_INSTALL_DIR (used by irm|iex).
    [string]$Dir = $(if ($env:XUSE_INSTALL_DIR) { $env:XUSE_INSTALL_DIR } else { "" }),
    # Install with dev extras (pytest) for contributors.
    [switch]$Dev,
    # Reuse a browser already installed for the selected browser-driver version.
    [switch]$SkipBrowser,
    # git pull --ff-only an existing checkout before installing.
    [switch]$Update,
    # Show usage and exit.
    [switch]$Help
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$RepoUrl = if ($env:XUSE_REPO_URL) { $env:XUSE_REPO_URL } else { "https://github.com/ihuzaifashoukat/x-use.git" }
$MinPythonMajor = 3
$MinPythonMinor = 10

function Write-Info([string]$Msg) { Write-Host "==> $Msg" -ForegroundColor Green }
function Write-Warn([string]$Msg) { Write-Host "warn: $Msg" -ForegroundColor Yellow }
function Fail([string]$Msg) { Write-Host "error: $Msg" -ForegroundColor Red; exit 1 }

# Native commands do not throw on failure; check the exit code ourselves.
function Invoke-Native([string]$File, [string[]]$CmdArgs, [string]$What) {
    & $File @CmdArgs
    if ($LASTEXITCODE -ne 0) { Fail "$What failed (exit code $LASTEXITCODE)." }
}

if ($Help) {
    Get-Help $MyInvocation.MyCommand.Path
    exit 0
}

# --- preflight ------------------------------------------------------------------

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Fail "git is not installed (or not on PATH). Get it from https://git-scm.com/download/win"
}

$Python = $null
$candidates = @()
if (Get-Command py -ErrorAction SilentlyContinue) { $candidates += , @("py", @("-3")) }
if (Get-Command python -ErrorAction SilentlyContinue) { $candidates += , @("python", @()) }
foreach ($candidate in $candidates) {
    $exe = $candidate[0]; $exeArgs = $candidate[1]
    & $exe @exeArgs -c "import sys; raise SystemExit(0 if sys.version_info >= ($MinPythonMajor, $MinPythonMinor) else 1)" 2>$null
    if ($LASTEXITCODE -eq 0) { $Python = $candidate; break }
}
if (-not $Python) {
    Fail "Python $MinPythonMajor.$MinPythonMinor+ not found. Install it from https://www.python.org/downloads/ (tick 'Add python.exe to PATH') and re-run."
}
$pyExe = $Python[0]; $pyArgs = $Python[1]
# No embedded double quotes in -c strings: PowerShell strips them when
# passing arguments to native commands.
$pyVersion = (& $pyExe @pyArgs -c 'import sys; print(sys.version.split()[0])')
$gitVersion = (git --version) -replace '^git version ', ''
Write-Info "git $gitVersion, python $pyVersion - preflight OK."

# --- repo: clone / update / in-place ---------------------------------------------

function Test-XUseCheckout([string]$Path) {
    $pyproject = Join-Path $Path "pyproject.toml"
    if (-not (Test-Path $pyproject)) { return $false }
    return [bool](Select-String -Path $pyproject -Pattern '^name = "x-use' -Quiet)
}

if (-not $Dir -and (Test-XUseCheckout (Get-Location).Path)) {
    # Running from inside the repo: install in place.
    $Dir = (Get-Location).Path
}
if (-not $Dir) { $Dir = "x-use" }

if (Test-XUseCheckout $Dir) {
    if ($Update) {
        Write-Info "Updating existing clone in $Dir ..."
        git -C $Dir pull --ff-only
        if ($LASTEXITCODE -ne 0) { Fail "git pull failed; the existing checkout was not updated." }
    } else {
        Write-Info "Existing x-use checkout found in $Dir - reusing it (pass -Update to pull latest)."
    }
} else {
    if (Test-Path $Dir) {
        Fail "'$Dir' exists but is not an x-use checkout. Pick another -Dir or remove it."
    }
    Write-Info "Cloning $RepoUrl -> $Dir ..."
    Invoke-Native git @("clone", "--depth", "1", $RepoUrl, $Dir) "git clone"
    if (-not (Test-XUseCheckout $Dir)) {
        Fail "the clone does not look like x-use v2 (missing pyproject.toml). The default branch may predate the v2 merge - clone the right branch or set XUSE_REPO_URL."
    }
}
$Dir = (Resolve-Path $Dir).Path
if (-not $env:X_USE_HOME) {
    if (-not $env:LOCALAPPDATA) { Fail "LOCALAPPDATA is unavailable; set X_USE_HOME to an absolute data directory." }
    $env:X_USE_HOME = Join-Path $env:LOCALAPPDATA "x-use"
}
$dataHome = $env:X_USE_HOME

# --- shared uv/browser setup -----------------------------------------------------------

$setupScript = Join-Path $Dir "scripts\setup_uv.py"
if (-not (Test-Path -LiteralPath $setupScript)) { Fail "the checkout is missing scripts\setup_uv.py." }
$setupArgs = $pyArgs + @($setupScript)
if ($Dev) { $setupArgs += "--dev" }
if ($SkipBrowser) { $setupArgs += "--skip-browser" }
Write-Info "Setting up uv, x-use and the configured browser ..."
Invoke-Native $pyExe $setupArgs "x-use setup"
$binDir = Join-Path $Dir ".venv\Scripts"
$xuseBin = Join-Path $binDir "x-use.exe"
if (-not (Test-Path -LiteralPath $xuseBin)) { Fail "setup finished but x-use.exe was not found in $binDir." }

# --- done -------------------------------------------------------------------------------

Write-Host ""
Write-Host "x-use is installed." -ForegroundColor White
$quotedDir = $Dir.Replace("'", "''")
$quotedBin = $xuseBin.Replace("'", "''")
$configScript = Join-Path $Dir "scripts\mcp_config.py"
$configArgs = $pyArgs + @($configScript, $xuseBin, $dataHome)
$mcpConfig = (& $pyExe @configArgs | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or -not $mcpConfig) { Fail "could not render the MCP client configuration." }
$quotedDataHome = $dataHome.Replace("'", "''")
Write-Host @"

Next steps:
  1. Set-Location -LiteralPath '$quotedDir'
  2. `$env:X_USE_HOME = '$quotedDataHome'
  3. & '$quotedBin' init      # interactive wizard: account, cookies, LLM keys
  4. & '$quotedBin' doctor    # re-check until every row is PASS/SKIP
  5. & '$quotedBin' run       # or connect an MCP client (below)

MCP client config (claude_desktop_config.json):
$mcpConfig

Docs: README.md, BEST_PRACTICES.md, docs\CONFIG_REFERENCE.md
"@

# Exit 0 explicitly: doctor's non-zero exit (unconfigured setup is expected on
# first install) must not make a successful install look failed to callers.
exit 0
