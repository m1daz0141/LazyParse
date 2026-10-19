$ErrorActionPreference = 'Stop'
$binDir = Join-Path $PSScriptRoot 'bin'
$captureRoot = Join-Path $PSScriptRoot 'captures'
if (Test-Path -LiteralPath (Join-Path $captureRoot 'autostart-install.json')) {
    throw 'Remove the installed autostart loader before rebuilding.'
}
& (Join-Path $PSScriptRoot 'build.ps1') | Out-Null
if (-not $?) { throw 'Base native build failed.' }
$zig = Join-Path $PSScriptRoot 'vendor/zig-x86_64-windows-0.15.2/zig.exe'
$nonce = [Guid]::NewGuid().ToString('N')
$testSession = Join-Path $captureRoot ('autostart-test-' + $nonce)
$gameSession = Join-Path $captureRoot ('classic-auto-' + $nonce)
New-Item -ItemType Directory -Force $testSession,$gameSession | Out-Null
$env:ZIG_GLOBAL_CACHE_DIR = Join-Path $PSScriptRoot '.zig-cache/global'
$env:ZIG_LOCAL_CACHE_DIR = Join-Path $PSScriptRoot '.zig-cache/local'
$common = @('cc','-target','x86_64-windows-gnu','-std=c11','-O2','-Wall','-Wextra','-Werror',
    '-I',(Join-Path $PSScriptRoot 'vendor/minhook/include'))
$objects = @('hook.o','buffer.o','trampoline.o','hde64.o','thread_guard.o') | ForEach-Object { Join-Path $binDir $_ }
function Invoke-Build([string[]]$Arguments) {
    & $zig @Arguments
    if ($LASTEXITCODE -ne 0) { throw 'Autostart build failed.' }
}
Push-Location $PSScriptRoot
try {
    foreach ($mode in @('test','game')) {
        $isTest = $mode -eq 'test'
        $core = Join-Path $binDir $(if ($isTest) {'OverParseCaptureAutoTest.dll'} else {'OverParseCaptureAuto.dll'})
        $loader = Join-Path $binDir $(if ($isTest) {'EosCaptureLoaderTest.dll'} else {'EosCaptureLoader.dll'})
        $session = if ($isTest) { $testSession } else { $gameSession }
        $header = Join-Path $binDir ('autostart-' + $mode + '-paths.h')
        @(
            ('#define AUTO_CAPTURE_DLL_PATH L"' + $core.Replace('\','\\') + '"'),
            ('#define AUTO_SESSION_DIR L"' + $session.Replace('\','\\') + '"'),
            ('#define AUTO_GUARD_NAME L"Local\\OverParseAuto-' + $mode + '-' + $nonce + '"')
        ) | Set-Content -LiteralPath $header -Encoding utf8
        $defines = @('-DOVERPARSE_AUTOSTART','-DOVERPARSE_CALLBACK_SLOT')
        if ($isTest) { $defines += '-DOVERPARSE_AUTOSTART_TEST' }
        Invoke-Build ($common + @('-shared','-include',$header) + $defines + @('capture.c','capture_autostart.c') + $objects + @('-lbcrypt','-o',$core))
        Invoke-Build ($common + @('-shared','-include',$header,'eos_capture_loader.c','-o',$loader))
    }
    $hostExe = Join-Path $binDir 'AutoCaptureTestHost.exe'
    Invoke-Build ($common + @('-municode','autostart_test_host.c','-o',$hostExe))
    & $hostExe (Join-Path $binDir 'EosCaptureLoaderTest.dll') $testSession
    if ($LASTEXITCODE -ne 0) { throw 'Bootstrap unload / worker lifetime test failed.' }
    & $hostExe (Join-Path $binDir 'EosCaptureLoaderTest.dll') $testSession
    if ($LASTEXITCODE -ne 0) { throw 'Same build relaunch test failed.' }
    [ordered]@{
        loader = (Join-Path $binDir 'EosCaptureLoader.dll')
        loader_sha256 = (Get-FileHash -LiteralPath (Join-Path $binDir 'EosCaptureLoader.dll') -Algorithm SHA256).Hash.ToLowerInvariant()
        core = (Join-Path $binDir 'OverParseCaptureAuto.dll')
        core_sha256 = (Get-FileHash -LiteralPath (Join-Path $binDir 'OverParseCaptureAuto.dll') -Algorithm SHA256).Hash.ToLowerInvariant()
        session = $gameSession
        bridge_sha256 = 'd20e83107079a4d65d490e27d272fd1e195a22235f5ee860cc9f9e1160e6cf9c'
        classic_sha256 = 'b2a87ec9000caa7667677d22f51859c449f4bb686f10b876370be7b861243332'
        bootstrap_mock_test_passed = $true
        capture_method = 'bound_callback_slot'
        requires_start_signal = $true
        control_abi = 1
        fresh_run_per_launch = $true
    } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $binDir 'autostart-build.json') -Encoding utf8
    Get-Content -LiteralPath (Join-Path $binDir 'autostart-build.json')
} finally { Pop-Location }
