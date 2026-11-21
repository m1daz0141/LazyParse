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
