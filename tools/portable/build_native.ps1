$ErrorActionPreference = 'Stop'
$source = Join-Path $PSScriptRoot '../native_capture'
$output = Join-Path $PSScriptRoot 'bin'
$zig = Join-Path $source 'vendor/zig-x86_64-windows-0.15.2/zig.exe'
if (-not (Test-Path -LiteralPath $zig)) { throw 'Build the native prerequisites with tools/native_capture/build.ps1 -Bootstrap first.' }
New-Item -ItemType Directory -Force $output | Out-Null
$profile = Get-Content -LiteralPath "$source/profiles/classic-2026-11-18.json" -Raw | ConvertFrom-Json
foreach ($name in @('classic_sha256','bridge_sha256')) {
    if ($profile.$name -cnotmatch '^[a-f0-9]{64}$') { throw "Invalid profile hash: $name" }
}
$header = Join-Path $output 'client-profile.h'
$definitions = @('#pragma once', ('#define OPC_CLASSIC_SHA256 "' + $profile.classic_sha256 + '"'))
foreach ($name in @('callback_rva','client_global_rva','client_vtable_rva','network_vtable_rva',
    'socket_vtable_rva','listener_vtable_rva','listener_adapter_rva','damage_descriptor_rva','required_image_size')) {
    $value = $profile.layout.$name
    if ($value -cnotmatch '^0x[a-f0-9]+$') { throw "Invalid profile RVA: $name" }
    $definitions += '#define OPC_' + $name.ToUpperInvariant() + ' ' + $value + 'u'
}
$definitions | Set-Content -LiteralPath $header -Encoding ascii
$env:ZIG_GLOBAL_CACHE_DIR = Join-Path $source '.zig-cache/global'
$env:ZIG_LOCAL_CACHE_DIR = Join-Path $source '.zig-cache/local'
$common = @('cc','-target','x86_64-windows-gnu','-std=c11','-O2','-Wall','-Wextra','-Werror','-DLAZYPARSE_PORTABLE')
$profileFlags = @('-include', $header)
function Build-Native([string[]]$Arguments) {
    & $zig @Arguments
    if ($LASTEXITCODE -ne 0) { throw 'Portable native build failed.' }
}
Push-Location $source
try {
    Build-Native ($common + $profileFlags + @('-c','thread_guard.c','-o',"$output/thread_guard.o"))
    $objects = @("$output/thread_guard.o")
    Build-Native ($common + @('-shared','eos_capture_loader.c','-o',"$output/Loader.dll"))
    Build-Native ($common + @('-shared','-DOVERPARSE_AUTOSTART_TEST','eos_capture_loader.c','-o',"$output/LoaderTest.dll"))
    foreach ($mode in @('game','test')) {
        $defines = @('-DOVERPARSE_AUTOSTART','-DOVERPARSE_CALLBACK_SLOT')
        if ($mode -eq 'test') { $defines += '-DOVERPARSE_AUTOSTART_TEST' }
        $name = if ($mode -eq 'test') { 'CaptureTest.dll' } else { 'Capture.dll' }
        Build-Native ($common + $profileFlags + @('-shared') + $defines + @('capture.c','capture_autostart.c') + $objects + @('-lbcrypt','-o',"$output/$name"))
    }
    Build-Native ($common + @('-municode','autostart_test_host.c','-o',"$output/AutoCaptureTestHost.exe"))
    Build-Native ($common + $profileFlags + @('-shared','-DOVERPARSE_TEST_HOST','-DOVERPARSE_CALLBACK_SLOT','capture.c') + $objects + @('-lbcrypt','-o',"$output/CaptureBindingTest.dll"))
    Build-Native ($common + $profileFlags + @('-municode','continuity_test_host.c','-o',"$output/ContinuityTestHost.exe"))
    Build-Native ($common + $profileFlags + @('-municode','profile_reload_test_host.c','-o',"$output/ProfileReloadTestHost.exe"))
    Build-Native ($common + @('-ULAZYPARSE_PORTABLE','-DOVERPARSE_RUNTIME_TEST','-DAUTO_SESSION_DIR=L"unused"',
        '-municode','runtime_test_host.c','capture_autostart.c','-o',"$output/AutoRuntimeTestHost.exe"))
    Build-Native ($common + @('thread_guard_test.c','thread_guard.c','-o',"$output/ThreadGuardTest.exe"))
    & "$output/ThreadGuardTest.exe"
    if ($LASTEXITCODE -ne 0) { throw 'Thread guard diagnostics/rollback test failed.' }
    foreach ($version in @('legacy','current')) {
        $flags = if ($version -eq 'current') { $profileFlags } else { @() }
        $test = Join-Path $output "BindingLayout-$version.exe"
        Build-Native ($common + $flags + @('binding_layout_test.c','-o',$test))
        & $test
        if ($LASTEXITCODE -ne 0) { throw "Binding layout test failed: $version" }
    }
    [ordered]@{
        schema_version = 1
        loader_sha256 = (Get-FileHash -LiteralPath "$output/Loader.dll" -Algorithm SHA256).Hash.ToLowerInvariant()
        core_sha256 = (Get-FileHash -LiteralPath "$output/Capture.dll" -Algorithm SHA256).Hash.ToLowerInvariant()
        bridge_sha256 = $profile.bridge_sha256
        classic_sha256 = $profile.classic_sha256
        profile_id = $profile.id
        live_verified = $profile.validation.live_verified
        capture_method = 'bound_callback_slot'
        fresh_run_per_launch = $true
        legacy_loader_sha256 = @('67626b647462b0942c3acf59d3ad3c3fbd2bd2376d9ea0f3015324356389e05d')
    } | ConvertTo-Json | Set-Content -LiteralPath "$output/build.json" -Encoding utf8
} finally { Pop-Location }
