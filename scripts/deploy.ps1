param(
    [Parameter(Mandatory = $true)][string]$DataDir,
    [Parameter(Mandatory = $true)][string]$PythonExe,
    [string]$TaskName = "AgBot-TelegramBridge",
    [string]$Revision = "HEAD",
    [string]$BeforeStartScript
)

$ErrorActionPreference = "Stop"
function Stop-CapturedProcess {
    param($Captured)
    $current = Get-CimInstance Win32_Process -Filter "ProcessId=$($Captured.ProcessId)"
    if (-not $current -or $current.CreationDate -ne $Captured.CreationDate) { return }
    $stopErrors = @()
    Stop-Process -Id $Captured.ProcessId -Force -ErrorAction SilentlyContinue -ErrorVariable stopErrors
    if ($stopErrors.Count) {
        $remaining = Get-CimInstance Win32_Process -Filter "ProcessId=$($Captured.ProcessId)"
        if ($remaining -and $remaining.CreationDate -eq $Captured.CreationDate) {
            throw $stopErrors[0]
        }
        Write-Verbose "Captured process exited before termination completed."
    }
}

$root = Split-Path -Parent $PSScriptRoot
$DataDir = [IO.Path]::GetFullPath($DataDir)
if ($DataDir.TrimEnd('\') -eq $root.TrimEnd('\') -or $DataDir.StartsWith($root.TrimEnd('\') + '\')) {
    throw "Production private data must be outside the source repository."
}
if (-not (Test-Path -LiteralPath (Join-Path $DataDir "profile.md"))) {
    throw "A private profile.md is required in DataDir."
}
if (-not (Test-Path -LiteralPath $PythonExe)) { throw "Python interpreter not found." }
if ($BeforeStartScript -and -not (Test-Path -LiteralPath $BeforeStartScript)) {
    throw "Private preparation script not found."
}
$sha = & git -C $root rev-parse --verify "$Revision^{commit}"
if ($LASTEXITCODE -ne 0) { throw "Cannot resolve deployment revision." }
$sha = $sha.Trim()
$releaseDir = Join-Path $DataDir "releases\$sha"
$backup = Join-Path $DataDir ("backups\deploy-" + (Get-Date -Format "yyyyMMdd-HHmmss"))
New-Item -ItemType Directory -Path $backup -Force | Out-Null
$archive = Join-Path $backup "source.zip"
& git -C $root archive --format=zip "--output=$archive" $sha
if ($LASTEXITCODE -ne 0) { throw "Could not archive release." }
if (-not (Test-Path -LiteralPath $releaseDir)) {
    Expand-Archive -LiteralPath $archive -DestinationPath $releaseDir
}
& $PythonExe -m unittest discover -s (Join-Path $releaseDir "tests") -t $releaseDir
if ($LASTEXITCODE -ne 0) { throw "Release regression suite failed; running bot unchanged." }
$task = Get-ScheduledTask -TaskName $TaskName
$oldAction = $task.Actions[0]
[pscustomobject]@{ Execute = $oldAction.Execute; Arguments = $oldAction.Arguments;
                  WorkingDirectory = $oldAction.WorkingDirectory } |
    ConvertTo-Json | Set-Content -LiteralPath (Join-Path $backup "previous-task.json")

# Capture only the process subtree of this bridge, never other Python/Copilot sessions.
$listener = Get-NetTCPConnection -LocalPort 49517 -State Listen -ErrorAction SilentlyContinue
$owned = @()
if ($listener) {
    $processes = @(Get-CimInstance Win32_Process)
    $bridge = $processes | Where-Object ProcessId -eq $listener[0].OwningProcess
    if ($bridge.CommandLine -notmatch 'telegram_bridge\.py') {
        throw "Port 49517 belongs to another application; refusing to stop it."
    }
    $owned = @($bridge)
    for ($index = 0; $index -lt $owned.Count; $index++) {
        $owned += @($processes | Where-Object ParentProcessId -eq $owned[$index].ProcessId)
    }
    if ($owned | Where-Object {
        $_.Name -match 'copilot|claude' -or $_.CommandLine -match '\\(?:copilot|claude)\.(?:cmd|js|exe)\b'
    }) {
        throw "The current bot is generating a reply. Retry deployment when it is idle."
    }
}
$prepared = $false
try {
    Stop-ScheduledTask -TaskName $TaskName
    foreach ($process in ($owned | Sort-Object CreationDate -Descending)) {
        Stop-CapturedProcess -Captured $process
    }
    Copy-Item -LiteralPath (Join-Path $DataDir "state") -Destination (Join-Path $backup "state") -Recurse
    Copy-Item -LiteralPath (Join-Path $DataDir "profile.md") -Destination $backup
    $envPath = Join-Path $DataDir ".env"
    $hadEnv = Test-Path -LiteralPath $envPath
    if ($hadEnv) { Copy-Item -LiteralPath $envPath -Destination (Join-Path $backup ".env") }
    if ($BeforeStartScript) {
        $prepared = $true
        & $BeforeStartScript -DataDir $DataDir -ReleaseDir $releaseDir -PythonExe $PythonExe
    }
    $launcher = Join-Path $releaseDir "scripts\run_bridge.ps1"
    $arguments = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$launcher`" " +
                 "-DataDir `"$DataDir`" -PythonExe `"$PythonExe`" -Release $sha"
    $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $arguments
    Set-ScheduledTask -TaskName $TaskName -Action $action | Out-Null
    Start-ScheduledTask -TaskName $TaskName
    $ready = $false
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        Start-Sleep -Seconds 2
        try {
            $health = Invoke-RestMethod -Uri "http://127.0.0.1:49517/health" -TimeoutSec 2
            if ($health.release -eq $sha) { $ready = $true; break }
        } catch [System.Net.Http.HttpRequestException] {
            # Startup is still in progress; the bounded readiness loop handles failure.
        } catch [System.Threading.Tasks.TaskCanceledException] {
        } catch [System.Net.WebException] {
        }
    }
    if (-not $ready) { throw "New release did not become ready. Private backup: $backup" }
    [pscustomobject]@{ release = $sha; path = $releaseDir; backup = $backup;
                      model = $health.model; status = $health.status } | ConvertTo-Json
} catch {
    $deploymentFailure = $_
    try {
        # Capture children before stopping their scheduled parent, which can orphan them.
        $processes = @(Get-CimInstance Win32_Process)
        $releaseScript = [regex]::Escape((Join-Path $releaseDir "telegram_bridge.py"))
        $newOwned = @($processes | Where-Object {
            $_.Name -match '^pythonw?\.exe$' -and $_.CommandLine -match $releaseScript
        })
        for ($index = 0; $index -lt $newOwned.Count; $index++) {
            $knownIds = @($newOwned.ProcessId)
            $newOwned += @($processes | Where-Object {
                $_.ParentProcessId -eq $newOwned[$index].ProcessId -and $_.ProcessId -notin $knownIds
            })
        }
        Stop-ScheduledTask -TaskName $TaskName
        foreach ($process in ($newOwned | Sort-Object CreationDate -Descending)) {
            Stop-CapturedProcess -Captured $process
        }
    } finally {
        try {
            if ($prepared) {
                Copy-Item -LiteralPath (Join-Path $backup "profile.md") -Destination $DataDir -Force
                if ($hadEnv) {
                    Copy-Item -LiteralPath (Join-Path $backup ".env") -Destination $envPath -Force
                } elseif (Test-Path -LiteralPath $envPath) {
                    Remove-Item -LiteralPath $envPath
                }
            }
        } finally {
            Set-ScheduledTask -TaskName $TaskName -Action $oldAction | Out-Null
            Start-ScheduledTask -TaskName $TaskName
        }
    }
    throw $deploymentFailure
}
