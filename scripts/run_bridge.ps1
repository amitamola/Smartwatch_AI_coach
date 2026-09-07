# run_bridge.ps1 - launch the Telegram coach bridge (persistent poller).
# Run by hand for testing, or from a scheduled task at logon. A localhost port
# lock inside the Python process guarantees only one instance ever polls.
#
# Paths are resolved RELATIVE to this script, so the repo can live anywhere.

param(
    [string]$DataDir = $env:AGBOT_DATA_DIR,
    [string]$PythonExe = $env:AGBOT_PYTHON,
    [string]$Release = "development"
)

$ErrorActionPreference = "Stop"
$root   = Split-Path -Parent $PSScriptRoot          # repo root (parent of scripts/)
$script = Join-Path $root "telegram_bridge.py"

# Prefer a local virtualenv if present, else fall back to python on PATH.
$py = if ($PythonExe) { $PythonExe } else { Join-Path $root ".venv\Scripts\python.exe" }
if (-not (Test-Path $py)) { $py = "python" }

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

# For the default "copilot" backend, resolve copilot.exe robustly (a scheduled
# task may have a lean PATH). Harmless if you use another backend.
if (($env:AGBOT_LLM -eq $null) -or ($env:AGBOT_LLM -eq "copilot")) {
    if (-not $env:COPILOT_EXE) {
        $copilot = (Get-Command copilot -ErrorAction SilentlyContinue).Source
        if ($copilot) { $env:COPILOT_EXE = $copilot }
    }
}

Write-Output "Starting coach bridge: $script"
& $py $script
exit $LASTEXITCODE
