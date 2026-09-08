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
        with tempfile.TemporaryDirectory() as folder:
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


if __name__ == "__main__":
    unittest.main()
