import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


@unittest.skipUnless(os.name == "nt" and shutil.which("powershell"), "Windows launcher")
class LauncherTests(unittest.TestCase):
    def test_explicit_deployment_values_override_private_env(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as folder:
            data = Path(folder)
            interpreter = data / "fake-python.ps1"
            interpreter.write_text(
                "$global:LASTEXITCODE = 0\n"
                "@{data=$env:AGBOT_DATA_DIR;release=$env:AGBOT_RELEASE;"
                "user=$env:AGBOT_USER_NAME} | ConvertTo-Json -Compress | "
                "Set-Content -LiteralPath $env:AGBOT_LAUNCHER_TEST_RESULT -Encoding UTF8\n",
                encoding="ascii")
            (data / ".env").write_text(
                "AGBOT_DATA_DIR=wrong-directory\nAGBOT_RELEASE=wrong-release\n"
                "AGBOT_USER_NAME=\n", encoding="ascii")
            output = data / "result.json"
            environment = dict(os.environ, AGBOT_USER_NAME="synthetic-user",
                               AGBOT_LAUNCHER_TEST_RESULT=str(output))
            subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                 str(root / "scripts" / "run_bridge.ps1"), "-DataDir", str(data),
                 "-PythonExe", str(interpreter), "-Release", "expected-release"],
                env=environment, capture_output=True, text=True, timeout=30, check=True)
            values = json.loads(output.read_text(encoding="utf-8-sig"))
            self.assertTrue(Path(values["data"]).samefile(data))
            self.assertEqual(values["release"], "expected-release")
            self.assertEqual(values["user"], "synthetic-user")

    def test_captured_process_exit_race_is_safe_but_real_failure_surfaces(self):
        root = Path(__file__).resolve().parents[1]
        script = r"""
$ErrorActionPreference = 'Stop'
$tokens = $null
$errorsFound = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    $env:AGBOT_DEPLOY_TEST_SCRIPT, [ref]$tokens, [ref]$errorsFound)
if ($errorsFound.Count) { throw 'Deployment script did not parse.' }
$function = $ast.Find({
    param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
        $node.Name -eq 'Stop-CapturedProcess'
}, $true)
Invoke-Expression $function.Extent.Text
$script:captured = [pscustomobject]@{ProcessId=123;CreationDate=[datetime]'2000-01-01'}
$script:calls = 0
function Get-CimInstance {
    param($ClassName, $Filter)
    $script:calls++
    if ($script:calls -eq 1) { $script:captured }
}
function Stop-Process {
    [CmdletBinding()] param($Id, [switch]$Force)
    Write-Error 'Synthetic process-stop failure.'
}
Stop-CapturedProcess -Captured $script:captured
if ($script:calls -ne 2) { throw 'The termination postcondition was not inspected.' }
function Get-CimInstance { param($ClassName, $Filter) $script:captured }
$caught = $false
try { Stop-CapturedProcess -Captured $script:captured } catch { $caught = $true }
if (-not $caught) { throw 'A real stop failure was swallowed.' }
$stops = $ast.FindAll({
    param($node)
    $node -is [System.Management.Automation.Language.CommandAst] -and
        $node.GetCommandName() -eq 'Stop-ScheduledTask'
}, $true)
foreach ($stop in $stops) {
    $parent = $stop.Parent
    while ($parent -and $parent -isnot [System.Management.Automation.Language.TryStatementAst]) {
        $parent = $parent.Parent
    }
    if (-not $parent) { throw 'Task cutover is outside rollback protection.' }
}
"""
        environment = dict(os.environ, AGBOT_DEPLOY_TEST_SCRIPT=
                           str(root / "scripts" / "deploy.ps1"))
        subprocess.run(["powershell", "-NoProfile", "-Command", script],
                       env=environment, capture_output=True, text=True,
                       timeout=30, check=True)

    def test_readiness_drain_and_cli_preflight_fail_closed(self):
        root = Path(__file__).resolve().parents[1]
        script = r"""
$ErrorActionPreference = 'Stop'
$tokens = $null
$errorsFound = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    $env:AGBOT_DEPLOY_TEST_SCRIPT, [ref]$tokens, [ref]$errorsFound)
if ($errorsFound.Count) { throw 'Deployment script did not parse.' }
foreach ($name in @('Test-BridgeReady', 'Test-BridgeIdle', 'Assert-CopilotFlags')) {
    $function = $ast.Find({
        param($node)
        $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
            $node.Name -eq $name
    }, $true)
    Invoke-Expression $function.Extent.Text
}
$health = [pscustomobject]@{
    release='new-sha'; status='ready'; ready=$true; draining=$false
    components=[pscustomobject]@{poller=$true;worker=$true;outbox=$true}
    worker_busy=$false; scheduled_jobs_busy=$false
    queues=[pscustomobject]@{
        inbox=[pscustomobject]@{done=2};outbox=[pscustomobject]@{sent=3}
    }
}
if (-not (Test-BridgeReady $health 'new-sha')) { throw 'Ready release rejected.' }
if (Test-BridgeReady $health 'old-sha') { throw 'Wrong SHA accepted.' }
if (Test-BridgeIdle $health) { throw 'Missing admission gate accepted for cutover.' }
$health.draining = $true
if (-not (Test-BridgeIdle $health)) { throw 'Idle release rejected.' }
if (Test-BridgeReady $health 'new-sha') { throw 'Draining new release accepted.' }
$health.draining = $false
$health.status = 'busy'
if (-not (Test-BridgeReady $health 'new-sha')) { throw 'Healthy busy release rejected.' }
if (Test-BridgeIdle $health) { throw 'Busy status accepted for cutover.' }
$health.status = 'ready'
$health.draining = $true
$health.scheduled_jobs_busy = $true
if (Test-BridgeIdle $health) { throw 'Scheduled work was not drained.' }
$health.scheduled_jobs_busy = $null
if (Test-BridgeIdle $health) { throw 'Missing busy status accepted.' }
$health.scheduled_jobs_busy = $false
$health.worker_busy = $true
if (Test-BridgeIdle $health) { throw 'Worker activity accepted.' }
$health.worker_busy = $false
$health.queues.inbox = [pscustomobject]@{pending=1}
if (Test-BridgeIdle $health) { throw 'Pending queue accepted.' }
$health.components.worker = $false
if (Test-BridgeReady $health 'new-sha' -AllowDraining) { throw 'Dead component accepted.' }
$health.components.worker = $true
$health.ready = $false
if (Test-BridgeReady $health 'new-sha' -AllowDraining) { throw 'Unready release accepted.' }
function Fake-Copilot {
    $global:LASTEXITCODE = 0
    '--model --available-tools --context'
}
$caught = $false
try { Assert-CopilotFlags Fake-Copilot } catch { $caught = $true }
if (-not $caught) { throw 'CLI missing flags accepted.' }
function Fake-Copilot {
    $global:LASTEXITCODE = 0
    '--no-ask-user --available-tools= --no-custom-instructions --disable-builtin-mcps --disable-mcp-server --sync-session --reasoning-effort --context --model --attachment --no-color'
}
Assert-CopilotFlags Fake-Copilot
"""
        environment = dict(os.environ, AGBOT_DEPLOY_TEST_SCRIPT=
                           str(root / "scripts" / "deploy.ps1"))
        subprocess.run(["powershell", "-NoProfile", "-Command", script],
                       env=environment, capture_output=True, text=True,
                       timeout=30, check=True)

    def test_drain_protocol_legacy_upgrade_and_abort_resume(self):
        root = Path(__file__).resolve().parents[1]
        script = r"""
$ErrorActionPreference = 'Stop'
$tokens = $null
$errorsFound = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    $env:AGBOT_DEPLOY_TEST_SCRIPT, [ref]$tokens, [ref]$errorsFound)
if ($errorsFound.Count) { throw 'Deployment script did not parse.' }
foreach ($name in @('Test-BridgeReady', 'Test-BridgeIdle', 'Enable-BridgeDrain',
                   'Resume-BridgeAdmission', 'Wait-BridgeDrain', 'Get-BridgeProcessTree')) {
    $function = $ast.Find({
        param($node)
        $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
            $node.Name -eq $name
    }, $true)
    Invoke-Expression $function.Extent.Text
}
class SyntheticHttpFailure : System.Exception {
    [object]$Response
    SyntheticHttpFailure([int]$code) : base('synthetic HTTP failure') {
        $this.Response = [pscustomobject]@{StatusCode=$code}
    }
}
$script:code = 200
$script:requests = @()
$script:health = [pscustomobject]@{
    release='old-sha'; status='ready'; worker_busy_seconds=0
    queues=[pscustomobject]@{
        inbox=[pscustomobject]@{done=2};outbox=[pscustomobject]@{sent=3}
    }
}
function Invoke-RestMethod {
    param($Method, $Uri, $TimeoutSec)
    $script:requests += "$Method $Uri"
    if ($script:code -ne 200) { throw [SyntheticHttpFailure]::new($script:code) }
    if ($Uri.EndsWith('/health')) { return $script:health }
}
function Start-Sleep { param($Seconds) }
$script:bridge = [pscustomobject]@{ProcessId=123;ParentProcessId=1;CreationDate=1}
$script:child = [pscustomobject]@{ProcessId=456;ParentProcessId=123;CreationDate=2}
$script:hasChild = $false
function Get-CimInstance {
    param($ClassName)
    $script:bridge
    if ($script:hasChild) { $script:child }
}
if (-not (Enable-BridgeDrain)) { throw 'Drain POST failed.' }
if ($script:requests[-1] -ne 'Post http://127.0.0.1:49517/drain') { throw 'Drain was not POSTed.' }
$script:code = 404
$warnings = @()
if (Enable-BridgeDrain -WarningVariable warnings) { throw '404 did not select legacy path.' }
if (-not $warnings.Count) { throw 'Legacy admission warning missing.' }
$script:code = 500
$caught = $false
try { Enable-BridgeDrain } catch { $caught = $true }
if (-not $caught) { throw 'Non-404 HTTP failure was treated as legacy.' }
$script:code = 200
if (-not (Test-BridgeIdle -Health $script:health -Legacy)) { throw 'Old health was rejected.' }
if (Test-BridgeIdle -Health $script:health) { throw 'Old health bypassed modern drain checks.' }
if (Test-BridgeReady -Health $script:health -Release 'old-sha') { throw 'Old health bypassed new-release readiness.' }
$null = Wait-BridgeDrain -CapturedBridge $script:bridge -Legacy
$script:hasChild = $true
$caught = $false
try { Wait-BridgeDrain -CapturedBridge $script:bridge -Legacy } catch { $caught = $true }
if (-not $caught) { throw 'Legacy child activity was accepted.' }
$script:hasChild = $false
$script:health.worker_busy_seconds = 1
if (Test-BridgeIdle -Health $script:health -Legacy) { throw 'Old busy worker accepted.' }
$script:health.worker_busy_seconds = 0
$script:health.queues.inbox = [pscustomobject]@{pending=1}
if (Test-BridgeIdle -Health $script:health -Legacy) { throw 'Old pending inbox accepted.' }

# Exercise the actual pre-shutdown catch branch without running deployment commands.
$deploymentTry = @($ast.EndBlock.Statements | Where-Object {
    $_ -is [System.Management.Automation.Language.TryStatementAst]
})[0]
$catchScript = "try { throw 'synthetic abort' } " + $deploymentTry.CatchClauses[0].Extent.Text
$shutdownStarted = $false
$drainAttempted = $true
$script:requests = @()
$caught = $false
try { Invoke-Expression $catchScript } catch { $caught = $_.Exception.Message -eq 'synthetic abort' }
if (-not $caught) { throw 'Original admission-abort failure was not preserved.' }
if ($script:requests.Count -ne 1 -or $script:requests[0] -ne 'Post http://127.0.0.1:49517/resume') {
    throw 'Admission abort did not POST resume.'
}
$drainAttempted = $false
$script:requests = @()
try { Invoke-Expression $catchScript } catch {}
if ($script:requests.Count) { throw 'Legacy abort sent an unsupported resume request.' }
exit 0
"""
        environment = dict(os.environ, AGBOT_DEPLOY_TEST_SCRIPT=
                           str(root / "scripts" / "deploy.ps1"))
        result = subprocess.run(["powershell", "-NoProfile", "-Command", script],
                                env=environment, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
