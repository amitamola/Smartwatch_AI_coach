# run_bridge.ps1 - supervise the Telegram coach bridge (persistent poller).
# Run by hand for testing, or from a scheduled task at logon. A localhost port
# lock inside the Python process guarantees only one instance ever polls.
#
# Paths are resolved RELATIVE to this script, so the repo can live anywhere.

param(
    [string]$DataDir = $env:AGBOT_DATA_DIR,
    [string]$PythonExe = $env:AGBOT_PYTHON,
    [string]$Release = "development",
    [ValidateRange(1, 60)][int]$RestartDelaySeconds = 15
)

$ErrorActionPreference = "Stop"
$root   = Split-Path -Parent $PSScriptRoot          # repo root (parent of scripts/)
$script = Join-Path $root "telegram_bridge.py"

$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8       = "1"

# --- Optional: pin the Copilot CLI model + reasoning effort (else CLI defaults). ---
# Usual place for these is .env, but you can also uncomment here:
# $env:AGBOT_MODEL            = "gpt-5.6-luna"   # any id from `/model`, or "auto"
# $env:AGBOT_REASONING_EFFORT = "max"           # none|minimal|low|medium|high|xhigh|max
# $env:AGBOT_LLM_TIMEOUT      = "600"            # raise for slow high-reasoning models

# Load .env (simple KEY=VALUE lines) if present, so tokens/keys are available.
if (-not $DataDir) { $DataDir = $root }
$DataDir = [IO.Path]::GetFullPath($DataDir)
$env:AGBOT_DATA_DIR = $DataDir
$env:AGBOT_RELEASE = $Release
$logDir = Join-Path $DataDir "logs"
New-Item -ItemType Directory -Path $logDir -Force | Out-Null
$launcherLog = Join-Path $logDir "launcher.log"

function Write-LauncherLog {
    param([string]$Message)
    $ErrorActionPreference = 'Stop'
    $safe = $Message -replace '(?:bot)?\d{6,}:[A-Za-z0-9_-]{20,}', '[REDACTED]'
    $safe = $safe -replace '\b(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+)\b', '[REDACTED]'
    $safe = $safe -replace '(?i)\bBearer\s+\S+', 'Bearer [REDACTED]'
    if ((Test-Path -LiteralPath $launcherLog) -and
        (Get-Item -LiteralPath $launcherLog).Length -ge 5000000) {
        for ($index = 3; $index -ge 1; $index--) {
            $previous = "$launcherLog.$index"
            if (Test-Path -LiteralPath $previous) {
                Move-Item -LiteralPath $previous -Destination "$launcherLog.$($index + 1)" -Force
            }
        }
        Move-Item -LiteralPath $launcherLog -Destination "$launcherLog.1" -Force
    }
    $line = "$(Get-Date -Format o) $safe"
    Add-Content -LiteralPath $launcherLog -Value $line -Encoding UTF8
    Write-Output $line
}

Write-LauncherLog "stage=launcher started pid=$PID release=$Release"
try {
    $envFile = Join-Path $DataDir ".env"
    if (Test-Path $envFile) {
        Get-Content $envFile | ForEach-Object {
            if ($_ -match '^\s*([^#=][^=]*)=(.*)$') {
                $key = $matches[1].Trim()
                $value = $matches[2].Trim()
                if ($value.Length -gt 1 -and (($value.StartsWith('"') -and $value.EndsWith('"')) -or
                                            ($value.StartsWith("'") -and $value.EndsWith("'")))) {
                    $value = $value.Substring(1, $value.Length - 2)
                }
                if ($value) { [Environment]::SetEnvironmentVariable($key, $value, "Process") }
            }
        }
        $env:AGBOT_DATA_DIR = $DataDir
        $env:AGBOT_RELEASE = $Release
    }

    # An explicitly selected interpreter must not silently fall back to a different environment.
    if ($PythonExe) {
        if (-not (Test-Path -LiteralPath $PythonExe)) { throw "Configured Python interpreter not found." }
        $py = $PythonExe
    } else {
        $py = Join-Path $root ".venv\Scripts\python.exe"
        if (-not (Test-Path -LiteralPath $py)) { $py = (Get-Command python -ErrorAction Stop).Source }
    }
    if (-not (Test-Path -LiteralPath $script)) { throw "Bridge script not found." }

    # A scheduled task may have a lean PATH.
    if (($env:AGBOT_LLM -eq $null) -or ($env:AGBOT_LLM -eq "copilot")) {
        if (-not $env:COPILOT_EXE) {
            $copilot = (Get-Command copilot -ErrorAction SilentlyContinue).Source
            if ($copilot) { $env:COPILOT_EXE = $copilot }
        }
    }

    $delay = $RestartDelaySeconds
    while ($true) {
        Write-LauncherLog "stage=launcher child_start release=$Release"
        $uptime = [Diagnostics.Stopwatch]::StartNew()
        $global:LASTEXITCODE = $null
        try {
            # Windows PowerShell 5 wraps native stderr as ErrorRecord objects.
            # Capture those lines rather than letting ErrorActionPreference terminate supervision.
            $ErrorActionPreference = 'Continue'
            & $py -u $script 2>&1 | ForEach-Object { Write-LauncherLog $_.ToString() }
            $childExit = $LASTEXITCODE
        } finally {
            $ErrorActionPreference = 'Stop'
        }
        if ($null -eq $childExit) { $childExit = 1 }
        Write-LauncherLog "stage=launcher child_exit code=$childExit"
        if ($childExit -in @(0, 130, -1073741510)) {
            exit $childExit
        }
        if ($uptime.Elapsed.TotalMinutes -ge 5) { $delay = $RestartDelaySeconds }
        Write-LauncherLog "stage=launcher restarting delay_seconds=$delay"
        Start-Sleep -Seconds $delay
        $delay = [Math]::Min(60, $delay * 2)
    }
} catch {
    Write-LauncherLog ("stage=launcher fatal=" + $_.Exception.Message)
    exit 1
}
