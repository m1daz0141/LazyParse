param(
    [int]$ProcessId = 0,
    [switch]$ProbeOnly,
    [switch]$Stop
)
$ErrorActionPreference = 'Stop'
$loader = Join-Path $PSScriptRoot 'bin/CaptureLoader.exe'
if (-not (Test-Path -LiteralPath $loader)) { throw 'Run build.ps1 first.' }
if (-not $ProcessId) {
    $targets = @(Get-Process -Name pso2 -ErrorAction SilentlyContinue)
    if ($targets.Count -ne 1) {
        & $loader list
        throw 'Exactly one PSO2 process is required; otherwise specify -ProcessId.'
    }
    $ProcessId = $targets[0].Id
}
if ($Stop) {
    & $loader stop $ProcessId
    if ($LASTEXITCODE -ne 0) { throw 'CaptureStop failed. See the Win32 error above.' }
    return
}
& $loader probe $ProcessId
if ($LASTEXITCODE -ne 0) { throw 'Read-only preflight failed. No DLL was loaded.' }
if ($ProbeOnly) { return }
$captureRoot = Join-Path $PSScriptRoot 'captures'
New-Item -ItemType Directory -Force $captureRoot | Out-Null
$sessionName = 'classic-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + [Guid]::NewGuid().ToString('N').Substring(0,8)
$sessionDir = Join-Path $captureRoot $sessionName
$controlLog = Join-Path $captureRoot ($sessionName + '-control.txt')
& $loader attach $ProcessId (Join-Path $PSScriptRoot 'bin/OverParseCapture.dll') $sessionDir 2>&1 |
    Tee-Object -FilePath $controlLog
if ($LASTEXITCODE -ne 0) { throw "CaptureStart failed. Details: $controlLog" }
"Session: $sessionDir"
"Stop: & '$PSCommandPath' -ProcessId $ProcessId -Stop"
