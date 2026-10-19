$ErrorActionPreference = 'Stop'
$zig = Join-Path $PSScriptRoot 'vendor/zig-x86_64-windows-0.15.2/zig.exe'
if (-not (Test-Path -LiteralPath $zig)) { throw 'Run build.ps1 -Bootstrap first.' }
$binDir = Join-Path $PSScriptRoot 'bin'
$captureRoot = Join-Path $PSScriptRoot 'captures'
New-Item -ItemType Directory -Force $binDir,$captureRoot | Out-Null
if (Test-Path -LiteralPath (Join-Path $captureRoot 'eos-probe-install.json')) {
    throw 'Remove the installed probe before rebuilding it.'
}
$marker = Join-Path $captureRoot ('eos-probe-' + [Guid]::NewGuid().ToString('N') + '.marker')
$header = Join-Path $binDir 'eos-probe-path.h'
('#define PROBE_OUTPUT_PATH L"' + $marker.Replace('\','\\') + '"') |
    Set-Content -LiteralPath $header -Encoding utf8
$env:ZIG_GLOBAL_CACHE_DIR = Join-Path $PSScriptRoot '.zig-cache/global'
$env:ZIG_LOCAL_CACHE_DIR = Join-Path $PSScriptRoot '.zig-cache/local'
$dll = Join-Path $binDir 'EosLoadProbe.dll'
$hostExe = Join-Path $binDir 'EosProbeTestHost.exe'
Push-Location $PSScriptRoot
try {
    & $zig cc -target x86_64-windows-gnu -std=c11 -O2 -Wall -Wextra -Werror -shared -include $header eos_load_probe.c -o $dll
    if ($LASTEXITCODE -ne 0) { throw 'Probe DLL build failed.' }
    & $zig cc -target x86_64-windows-gnu -std=c11 -O2 -Wall -Wextra -Werror -municode eos_probe_test_host.c -o $hostExe
    if ($LASTEXITCODE -ne 0) { throw 'Probe test host build failed.' }
    & $hostExe $dll $marker
    if ($LASTEXITCODE -ne 0) { throw 'Probe load test failed.' }
    # The exact marker created by our fixture, under the verified output root.
    $resolvedMarker = [IO.Path]::GetFullPath($marker)
    if (-not $resolvedMarker.StartsWith(([IO.Path]::GetFullPath($captureRoot) + '\'),[StringComparison]::OrdinalIgnoreCase)) {
        throw 'Marker is outside the capture directory.'
    }
    Remove-Item -LiteralPath $resolvedMarker
    [ordered]@{
        dll = $dll
        sha256 = (Get-FileHash -LiteralPath $dll -Algorithm SHA256).Hash.ToLowerInvariant()
        marker = $marker
        bridge_sha256 = 'd20e83107079a4d65d490e27d272fd1e195a22235f5ee860cc9f9e1160e6cf9c'
        mock_load_test_passed = $true
        game_load_tested = $false
    } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $binDir 'eos-probe-build.json') -Encoding utf8
    Get-Content -LiteralPath (Join-Path $binDir 'eos-probe-build.json')
} finally { Pop-Location }
