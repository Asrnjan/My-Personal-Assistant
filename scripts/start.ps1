# Start Atlas on Windows, no Linux needed. Double-click "Start Atlas.bat" in the main
# folder, or run:   powershell -ExecutionPolicy Bypass -File scripts\start.ps1
#
# The first run finds Python (or offers to install it), installs the backend, creates the
# config files and .env with a fresh sign-in secret, and sets up the database. Every run
# then starts the assistant and opens http://127.0.0.1:8000/ in your browser.
#
#   -Port 9000      use another port
#   -BindHost 0.0.0.0   reachable from other devices on your Wi-Fi
#   -SetupOnly      set everything up, then stop (used by CI)
#   -NoBrowser      don't open the browser
#
# Works in Windows PowerShell 5.1 and PowerShell 7. Keep this file plain ASCII.
param(
    [int]$Port = 8000,
    [string]$BindHost = "127.0.0.1",
    [switch]$SetupOnly,
    [switch]$NoBrowser
)

# Programs like pip and alembic log to stderr, which Windows PowerShell 5.1 turns into
# errors under "Stop"; failures are checked with $LASTEXITCODE instead.
$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$Url = "http://127.0.0.1:$Port"

function Say([string]$Text, [string]$Color = "Gray") { Write-Host $Text -ForegroundColor $Color }

function Stop-WithMessage([string]$Text) {
    Write-Host ""
    Write-Host $Text -ForegroundColor Red
    exit 1
}

function Test-Running {
    try {
        $r = Invoke-WebRequest -Uri "$Url/health" -UseBasicParsing -TimeoutSec 2
        return $r.StatusCode -eq 200
    } catch {
        return $false
    }
}

# Returns @(exe, args...) for a Python 3.11 or newer, or $null.
function Find-Python {
    $candidates = @(
        @("py", "-3.13"), @("py", "-3.12"), @("py", "-3.11"), @("py", "-3"),
        @("python"), @("python3")
    )
    foreach ($c in $candidates) {
        if (-not (Get-Command $c[0] -ErrorAction SilentlyContinue)) { continue }
        $rest = @()
        if ($c.Count -gt 1) { $rest = $c[1..($c.Count - 1)] }
        try {
            $out = & $c[0] @rest -c "import sys; print(sys.version_info[0] * 100 + sys.version_info[1])" 2>$null
        } catch {
            continue
        }
        # The Microsoft Store placeholder "python" prints nothing and fails.
        if ($LASTEXITCODE -ne 0 -or -not $out) { continue }
        $version = 0
        # The comma keeps a one-item array ("python") from being unrolled into a string.
        if ([int]::TryParse(("$out").Trim(), [ref]$version) -and $version -ge 311) { return ,$c }
    }
    return $null
}

function Update-PathFromRegistry {
    $machine = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $user = [Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = "$machine;$user"
}

function Install-Python {
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) { return $false }
    Say ""
    Say "Atlas needs Python, which is not on this computer yet." "Yellow"
    $answer = Read-Host "Install Python 3.12 now? It is free and takes a few minutes. Type Y and press Enter"
    if ($answer -notmatch "^[Yy]") { return $false }
    Say "Installing Python. A window may ask for permission; click Yes." "Cyan"
    & winget install --exact --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements
    Update-PathFromRegistry
    return $true
}

$Host.UI.RawUI.WindowTitle = "Atlas - keep this window open"
Say ""
Say "  Atlas - your personal assistant" "Cyan"
Say "  Keep this window open while you use Atlas. Close it to switch Atlas off." "DarkGray"
Say ""

if (-not $SetupOnly -and (Test-Running)) {
    Say "Atlas is already running." "Green"
    if (-not $NoBrowser) { Start-Process $Url }
    exit 0
}

# ---------------------------------------------------------------- Python
$python = @(Find-Python)
if ($python.Count -eq 0 -or -not $python[0]) { $python = $null }
if (-not $python) {
    if (Install-Python) {
        $python = @(Find-Python)
        if ($python.Count -eq 0 -or -not $python[0]) { $python = $null }
    }
}
if (-not $python) {
    Stop-WithMessage ("Python 3.11 or newer is needed.`n" +
        "Install it from https://www.python.org/downloads/ and tick 'Add python.exe to PATH'" +
        " on the first screen of the installer.`nThen double-click Start Atlas again.")
}
$pyRest = @()
if ($python.Count -gt 1) { $pyRest = $python[1..($python.Count - 1)] }

# ---------------------------------------------------------------- the backend's own Python
$venv = Join-Path (Join-Path $Root "backend") ".venv"
$venvPython = Join-Path (Join-Path $venv "Scripts") "python.exe"
if (-not (Test-Path $venvPython)) { $venvPython = Join-Path (Join-Path $venv "bin") "python" }
if (-not (Test-Path $venvPython)) {
    Say "Setting up Atlas for the first time. This takes a few minutes..." "Cyan"
    & $python[0] @pyRest -m venv $venv
    $venvPython = Join-Path (Join-Path $venv "Scripts") "python.exe"
    if (-not (Test-Path $venvPython)) { $venvPython = Join-Path (Join-Path $venv "bin") "python" }
    if (-not (Test-Path $venvPython)) { Stop-WithMessage "Could not create backend\.venv with Python." }
}

# Install (again) when it never finished or the backend's requirements changed.
$pyproject = Join-Path (Join-Path $Root "backend") "pyproject.toml"
$marker = Join-Path $venv "atlas-installed.txt"
$wanted = (Get-FileHash $pyproject -Algorithm SHA256).Hash
$have = ""
if (Test-Path $marker) { $have = (Get-Content $marker -Raw).Trim() }
if ($have -ne $wanted) {
    Say "Installing the parts Atlas needs (only the first time, or after an update)..." "Cyan"
    & $venvPython -m pip install --quiet --disable-pip-version-check --upgrade pip
    & $venvPython -m pip install --quiet --disable-pip-version-check -e (Join-Path $Root "backend")
    if ($LASTEXITCODE -ne 0) {
        Stop-WithMessage "Installing failed. Check your internet connection and double-click Start Atlas again."
    }
    Set-Content -Path $marker -Value $wanted -Encoding ASCII
}

# ---------------------------------------------------------------- config, .env and the secret
$hadEnv = Test-Path (Join-Path $Root ".env")
$lines = @(& $venvPython (Join-Path (Join-Path $Root "scripts") "setup_local.py"))
if ($LASTEXITCODE -ne 0 -or $lines.Count -eq 0) { Stop-WithMessage "Could not create the settings file (.env)." }
if ($lines.Count -gt 1) { $lines[0..($lines.Count - 2)] | ForEach-Object { Say $_ } }
$secret = $lines[-1]

# ---------------------------------------------------------------- database
Push-Location (Join-Path $Root "backend")
try {
    & $venvPython -m alembic upgrade head 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) { Stop-WithMessage "Could not set up the database." }
} finally {
    Pop-Location
}

Say ""
Say "  Your sign-in secret (paste it in the app under the gear icon):" "Yellow"
Say "  $secret" "White"
if (-not $hadEnv) {
    try {
        Set-Clipboard -Value $secret
        Say "  It has also been copied for you. Paste it with Ctrl+V." "DarkGray"
    } catch {
        # No clipboard here (e.g. CI); the secret is printed above and saved in .env.
    }
}
Say ""

if ($SetupOnly) {
    Say "Setup finished." "Green"
    exit 0
}

# ---------------------------------------------------------------- run
if (-not $NoBrowser) {
    # Open the app as soon as it answers.
    Start-Job -ArgumentList $Url -ScriptBlock {
        param($u)
        for ($i = 0; $i -lt 90; $i++) {
            try {
                Invoke-WebRequest -Uri "$u/health" -UseBasicParsing -TimeoutSec 2 | Out-Null
                Start-Process $u
                return
            } catch {
                Start-Sleep -Seconds 1
            }
        }
    } | Out-Null
}

Say "  Atlas is starting. The app opens in your browser at $Url/" "Green"
Say "  To switch Atlas off, close this window (or press Ctrl+C)." "DarkGray"
Say ""
Push-Location (Join-Path $Root "backend")
try {
    & $venvPython -m uvicorn app.main:create_app --factory --host $BindHost --port $Port
} finally {
    Pop-Location
}
