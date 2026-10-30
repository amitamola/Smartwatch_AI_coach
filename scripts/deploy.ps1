param(
    [Parameter(Mandatory = $true)][string]$DataDir,
    [Parameter(Mandatory = $true)][string]$PythonExe,
    [string]$TaskName = "AgBot-TelegramBridge",
    [string]$Revision = "HEAD",
    [string]$BeforeStartScript,
    [string]$CopilotExe = $(if ($env:COPILOT_EXE) { $env:COPILOT_EXE } else { "copilot" }),
    [switch]$AcknowledgeAdditiveMigrations
)

$ErrorActionPreference = "Stop"
# Deployment contract: migrations must be additive and readable by the previous release.
# Rollback restores matching configuration, NOT databases: restoring a pre-cutover DB
# could erase acknowledged updates and domain effects. Backups are retained for manual recovery.
function Test-BridgeReady {
    param($Health, [string]$Release, [switch]$AllowDraining)
    if (-not $Health -or $Health.status -notin @('ready', 'busy') -or
        $Health.ready -ne $true -or ($Release -and $Health.release -ne $Release)) { return $false }
    if (-not $AllowDraining -and $Health.draining -eq $true) { return $false }
    if (-not $Health.components) { return $false }
    $components = @($Health.components.PSObject.Properties)
    if (-not $components.Count) { return $false }
    foreach ($component in $components) {
        if ($component.Value -ne $true) { return $false }
    }
    return $true
}

function Test-BridgeIdle {
    param($Health, [switch]$Legacy)
    if (-not $Health -or $Health.status -ne 'ready') { return $false }
    if ($Legacy) {
        # Only the /drain 404 path may use the pre-admission-gate health contract.
        if ($null -eq $Health.worker_busy_seconds -or $Health.worker_busy_seconds -ne 0 -or
            $Health.worker_busy -eq $true -or $Health.scheduled_jobs_busy -eq $true) { return $false }
    } else {
        if (-not (Test-BridgeReady -Health $Health -AllowDraining) -or
            $Health.draining -ne $true) { return $false }
        if ($Health.worker_busy -ne $false -or $Health.scheduled_jobs_busy -ne $false) { return $false }
    }
    foreach ($queue in @('inbox', 'outbox')) {
        if (-not $Health.queues -or -not $Health.queues.$queue) { return $false }
        foreach ($status in @('pending', 'processing')) {
            if ($Health.queues.$queue.$status -gt 0) { return $false }
        }
    }
    return $true
}

function Enable-BridgeDrain {
    [CmdletBinding()] param()
    try {
        Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:49517/drain" -TimeoutSec 5 | Out-Null
        return $true
    } catch {
        if ($_.Exception.Response -and [int]$_.Exception.Response.StatusCode -eq 404) {
            Write-Warning "Old bridge has no /drain endpoint. Using legacy health and child-process idle checks; admission cannot be gated until this first upgrade completes."
            return $false
        }
        throw
    }
}

function Resume-BridgeAdmission {
    Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:49517/resume" -TimeoutSec 5 | Out-Null
}

function Get-BridgeProcessTree {
    param($CapturedBridge)
    $processes = @(Get-CimInstance Win32_Process)
    $bridge = $processes | Where-Object {
        $_.ProcessId -eq $CapturedBridge.ProcessId -and
        $_.CreationDate -eq $CapturedBridge.CreationDate
    }
    if (-not $bridge) { throw "Captured bridge exited or changed identity before cutover." }
    $ownedProcesses = @($bridge)
    for ($index = 0; $index -lt $ownedProcesses.Count; $index++) {
        $knownIds = @($ownedProcesses.ProcessId)
        $ownedProcesses += @($processes | Where-Object {
            $_.ParentProcessId -eq $ownedProcesses[$index].ProcessId -and
            $_.ProcessId -notin $knownIds
        })
    }
    return $ownedProcesses
}

function Wait-BridgeDrain {
    param($CapturedBridge, [switch]$Legacy)
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        $health = Invoke-RestMethod -Uri "http://127.0.0.1:49517/health" -TimeoutSec 2
        $processTree = @(Get-BridgeProcessTree -CapturedBridge $CapturedBridge)
        if ((Test-BridgeIdle -Health $health -Legacy:$Legacy) -and $processTree.Count -eq 1) {
            return $health
        }
        Start-Sleep -Seconds 2
    }
    throw "Bridge did not drain worker, scheduled jobs, child processes and delivery queues."
}

function Assert-CopilotFlags {
    param([string]$Executable)
    $helpText = (& $Executable --help 2>&1 | Out-String)
    if ($LASTEXITCODE -ne 0) { throw "Copilot CLI help preflight failed." }
    foreach ($flag in @('--no-ask-user', '--available-tools', '--no-custom-instructions',
                       '--disable-builtin-mcps', '--disable-mcp-server', '--sync-session',
                       '--reasoning-effort', '--context', '--model', '--attachment', '--no-color')) {
        if ($helpText -notmatch ([regex]::Escape($flag) + '(?=[\s=,\[]|$)')) {
            throw "Copilot CLI lacks required flag: $flag"
        }
    }
}

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
if (-not $AcknowledgeAdditiveMigrations) {
    throw "Review migrations and pass -AcknowledgeAdditiveMigrations only if the previous release can read the upgraded state."
}
Assert-CopilotFlags -Executable $CopilotExe
$sha = & git -C $root rev-parse --verify "$Revision^{commit}"
if ($LASTEXITCODE -ne 0) { throw "Cannot resolve deployment revision." }
$sha = $sha.Trim()
$releaseDir = Join-Path $DataDir "releases\$sha"
$backup = Join-Path $DataDir ("backups\deploy-" + (Get-Date -Format "yyyyMMdd-HHmmssfff") + "-" + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $backup -Force | Out-Null
$archive = Join-Path $backup "source.zip"
& git -C $root archive --format=zip "--output=$archive" $sha
if ($LASTEXITCODE -ne 0) { throw "Could not archive release." }
if (-not (Test-Path -LiteralPath $releaseDir)) {
    Expand-Archive -LiteralPath $archive -DestinationPath $releaseDir
}
& $PythonExe -B -c "import pathlib,sys; [compile(p.read_bytes(),str(p),'exec') for p in pathlib.Path(sys.argv[1]).rglob('*.py')]" $releaseDir
if ($LASTEXITCODE -ne 0) { throw "Release compile preflight failed; running bot unchanged." }
& $PythonExe -B -m unittest discover -s (Join-Path $releaseDir "tests") -t $releaseDir
if ($LASTEXITCODE -ne 0) { throw "Release regression suite failed; running bot unchanged." }
$task = Get-ScheduledTask -TaskName $TaskName
$oldAction = $task.Actions[0]
[pscustomobject]@{ Execute = $oldAction.Execute; Arguments = $oldAction.Arguments;
                  WorkingDirectory = $oldAction.WorkingDirectory } |
    ConvertTo-Json | Set-Content -LiteralPath (Join-Path $backup "previous-task.json")

# Capture only the process subtree of this bridge, never other Python/Copilot sessions.
$listener = Get-NetTCPConnection -LocalPort 49517 -State Listen -ErrorAction SilentlyContinue
$owned = @()
$oldHealth = $null
$drainAttempted = $false
$shutdownStarted = $false
$legacyDrain = $false
if ($listener) {
    $bridge = Get-CimInstance Win32_Process -Filter "ProcessId=$($listener[0].OwningProcess)"
    if (-not $bridge -or $bridge.CommandLine -notmatch 'telegram_bridge\.py') {
        throw "Port 49517 belongs to another application; refusing to stop it."
    }
}
$prepared = $false
try {
    if ($listener) {
        $drainAttempted = $true
        $drainSupported = Enable-BridgeDrain
        $drainAttempted = $drainSupported
        $legacyDrain = -not $drainSupported
        $oldHealth = Wait-BridgeDrain -CapturedBridge $bridge -Legacy:$legacyDrain
        # Recheck both snapshots immediately before shutdown, especially for legacy admission.
        $oldHealth = Invoke-RestMethod -Uri "http://127.0.0.1:49517/health" -TimeoutSec 2
        $owned = @(Get-BridgeProcessTree -CapturedBridge $bridge)
        if (-not (Test-BridgeIdle -Health $oldHealth -Legacy:$legacyDrain) -or $owned.Count -ne 1) {
            throw "Bridge became busy before cutover; retry after drain."
        }
    }
    Stop-ScheduledTask -TaskName $TaskName
    $shutdownStarted = $true
    foreach ($process in ($owned | Sort-Object CreationDate -Descending)) {
        Stop-CapturedProcess -Captured $process
    }
    Copy-Item -LiteralPath (Join-Path $DataDir "state") -Destination (Join-Path $backup "state") -Recurse
    Copy-Item -LiteralPath (Join-Path $DataDir "profile.md") -Destination $backup
    $envPath = Join-Path $DataDir ".env"
    $hadEnv = Test-Path -LiteralPath $envPath
    if ($hadEnv) { Copy-Item -LiteralPath $envPath -Destination (Join-Path $backup ".env") }
    $policyPath = Join-Path $DataDir "training_policy.json"
    $hadPolicy = Test-Path -LiteralPath $policyPath
    if ($hadPolicy) { Copy-Item -LiteralPath $policyPath -Destination (Join-Path $backup "training_policy.json") }
    [pscustomobject]@{
        target_release = $sha; previous_release = $oldHealth.release
        migration_contract = 'additive-backward-compatible'; database_restore = 'manual-only'
        had_env = $hadEnv; had_training_policy = $hadPolicy
    } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $backup "backup-manifest.json")
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
            if (Test-BridgeReady -Health $health -Release $sha) { $ready = $true; break }
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
    if (-not $shutdownStarted) {
        if ($drainAttempted) {
            try { Resume-BridgeAdmission } catch {
                Write-Warning "Could not clear the old bridge admission gate; POST /resume must be retried locally."
            }
        }
        throw $deploymentFailure
    }
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
                if ($hadPolicy) {
                    Copy-Item -LiteralPath (Join-Path $backup "training_policy.json") -Destination $policyPath -Force
                } elseif (Test-Path -LiteralPath $policyPath) {
                    Remove-Item -LiteralPath $policyPath
                }
            }
        } finally {
            Set-ScheduledTask -TaskName $TaskName -Action $oldAction | Out-Null
            Start-ScheduledTask -TaskName $TaskName
        }
    }
    Write-Warning "Database state was NOT restored; additive migration compatibility is required. Matching backup: $backup"
    throw $deploymentFailure
}
