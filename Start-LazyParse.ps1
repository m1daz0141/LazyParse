#requires -Version 7.0
param(
    [string]$Capture,
    [string]$Root
)
$ErrorActionPreference = 'Stop'
$interpreter = (& py -3 -c 'import sys; print(sys.executable)').Trim()
if ($LASTEXITCODE -ne 0) { throw 'Python 3 is required.' }
$windowedInterpreter = Join-Path (Split-Path -Parent $interpreter) 'pythonw.exe'
if (-not (Test-Path -LiteralPath $windowedInterpreter)) { throw 'pythonw.exe was not found.' }
$launch = [Diagnostics.ProcessStartInfo]::new()
$launch.FileName = $windowedInterpreter
$launch.WorkingDirectory = $PSScriptRoot
$launch.UseShellExecute = $false
$launch.CreateNoWindow = $true
foreach ($argument in @('-m', 'tools.gui')) { $launch.ArgumentList.Add($argument) }
if ($Capture) {
    $launch.ArgumentList.Add('--capture')
    $launch.ArgumentList.Add((Resolve-Path -LiteralPath $Capture).Path)
}
if ($Root) {
    $launch.ArgumentList.Add('--root')
    $launch.ArgumentList.Add((Resolve-Path -LiteralPath $Root).Path)
}
$process = [Diagnostics.Process]::Start($launch)
try { "LazyParse started (PID $($process.Id))." } finally { $process.Dispose() }
