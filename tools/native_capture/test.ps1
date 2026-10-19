$ErrorActionPreference = 'Stop'
$taskRoot = $PSScriptRoot
$binDir = Join-Path $taskRoot 'bin'
$captureRoot = Join-Path $taskRoot 'captures'
New-Item -ItemType Directory -Force $captureRoot | Out-Null
$atomicRun = Join-Path $captureRoot ('atomic-publication-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $atomicRun | Out-Null
& (Join-Path $binDir 'AtomicFileTestHost.exe') $atomicRun
if ($LASTEXITCODE -ne 0) { throw 'Atomic publication sharing/retry test failed' }
& (Join-Path $binDir 'ThreadGuardTest.exe')
if ($LASTEXITCODE -ne 0) { throw 'Thread lifetime guard failed' }
& (Join-Path $binDir 'MappedProtectTest.exe')
if ($LASTEXITCODE -ne 0) { throw 'Mapped protection compatibility fixture failed' }
& (Join-Path $binDir 'MappedProtectTest.exe') --immutable
if ($LASTEXITCODE -ne 0) { throw 'Immutable mapping fixture failed' }
& (Join-Path $binDir 'BindingLayoutTest.exe')
if ($LASTEXITCODE -ne 0) { throw 'Binding layout validation fixture failed' }
& py -3 -m unittest discover -s $taskRoot -p test_capture.py -v
if ($LASTEXITCODE -ne 0) { throw 'Native capture integration failed' }
& py -3 -m unittest discover -s $taskRoot -p test_runtime.py -v
if ($LASTEXITCODE -ne 0) { throw 'Capture runtime control failed' }
$runDir = Join-Path $captureRoot ('remote-loader-' + [Guid]::NewGuid().ToString('N'))
$fixture = Start-Process -FilePath (Join-Path $binDir 'CaptureRemoteTestHost.exe') -WindowStyle Hidden -PassThru
try {
    & (Join-Path $binDir 'CaptureLoaderTest.exe') probe $fixture.Id
    if ($LASTEXITCODE -ne 0) { throw 'Remote test preflight failed' }
    & (Join-Path $binDir 'CaptureLoader.exe') probe $fixture.Id
    if ($LASTEXITCODE -eq 0) { throw 'Production loader accepted a non-game process' }
    & (Join-Path $binDir 'CaptureLoaderTest.exe') attach $fixture.Id (Join-Path $binDir 'CaptureLoaderFixture.dll') $runDir
    if ($LASTEXITCODE -ne 0) { throw 'Remote LoadLibraryW/export call failed' }
    if ((Get-Content -Raw -LiteralPath (Join-Path $runDir 'fixture-started.txt')).Trim() -ne 'synthetic remote loader test') {
        throw 'Remote config did not reach fixture DLL'
    }
    & (Join-Path $binDir 'CaptureLoaderTest.exe') stop $fixture.Id
    if ($LASTEXITCODE -ne 0) { throw 'Remote stop export failed' }
    if (-not $fixture.WaitForExit(5000) -or $fixture.ExitCode -ne 0) { throw 'Remote test host did not finish normally' }
    'PASS: remote DLL load, config transfer, export calls, production target guard'
} finally {
    # This PID belongs only to the fixture started above.
    if (-not $fixture.HasExited) { Stop-Process -Id $fixture.Id }
    $fixture.Dispose()
}
