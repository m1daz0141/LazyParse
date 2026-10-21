$ErrorActionPreference = 'Stop'
$catalogRoot = $PSScriptRoot
$nativeRoot = Join-Path $catalogRoot '../native_capture'
$nativeRoot = (Resolve-Path -LiteralPath $nativeRoot).Path
if (Test-Path -LiteralPath (Join-Path $nativeRoot 'captures/autostart-install.json')) {
    throw 'Remove the installed loader before building a catalog capture.'
}
if ([Diagnostics.Process]::GetProcessesByName('pso2').Length) { throw 'Exit PSO2 normally before building.' }
$binDir = Join-Path $catalogRoot 'bin'
New-Item -ItemType Directory -Force $binDir | Out-Null
$zig = Join-Path $nativeRoot 'vendor/zig-x86_64-windows-0.15.2/zig.exe'
$nonce = [Guid]::NewGuid().ToString('N')
$session = Join-Path $nativeRoot ('captures/quest-catalog-auto-' + $nonce)
New-Item -ItemType Directory -Force $session | Out-Null
$env:ZIG_GLOBAL_CACHE_DIR = Join-Path $nativeRoot '.zig-cache/global'
$env:ZIG_LOCAL_CACHE_DIR = Join-Path $nativeRoot '.zig-cache/local'
$common = @('cc','-target','x86_64-windows-gnu','-std=c11','-O2','-Wall','-Wextra','-Werror',
    '-I',$nativeRoot,'-I',(Join-Path $nativeRoot 'vendor/minhook/include'))
$objects = @('hook.o','buffer.o','trampoline.o','hde64.o','thread_guard.o') | ForEach-Object { Join-Path $nativeRoot "bin/$_" }
function Invoke-CatalogBuild([string[]]$CompilerArguments) {
    & $zig @CompilerArguments
    if ($LASTEXITCODE -ne 0) { throw 'Quest catalog build failed.' }
}
$core = Join-Path $binDir 'QuestCatalogCaptureAuto.dll'
$loader = Join-Path $binDir 'QuestCatalogLoader.dll'
$testCore = Join-Path $binDir 'QuestCatalogCaptureTest.dll'
$header = Join-Path $binDir 'catalog-paths.h'
@(
    ('#define AUTO_CAPTURE_DLL_PATH L"' + $core.Replace('\','\\') + '"'),
    ('#define AUTO_SESSION_DIR L"' + $session.Replace('\','\\') + '"'),
    ('#define AUTO_GUARD_NAME L"Local\\LazyParseQuestCatalog-' + $nonce + '"')
) | Set-Content -LiteralPath $header -Encoding utf8
Push-Location $nativeRoot
try {
    Invoke-CatalogBuild ($common + @('-shared','-DOVERPARSE_QUEST_CATALOG_CAPTURE','-DOVERPARSE_TEST_HOST','capture.c') + $objects + @('-lbcrypt','-o',$testCore))
    Invoke-CatalogBuild ($common + @('-municode',(Join-Path $catalogRoot 'capture_test_host.c'),'-o',(Join-Path $binDir 'QuestCatalogTestHost.exe')))
    $testDirectory = Join-Path $catalogRoot ('results/native-' + $nonce)
    New-Item -ItemType Directory -Force $testDirectory | Out-Null
    & (Join-Path $binDir 'QuestCatalogTestHost.exe') $testCore (Join-Path $testDirectory 'identity-context')
    if ($LASTEXITCODE -ne 0) { throw 'Quest catalog callback test failed.' }
    $normalCore = Join-Path $binDir 'NormalCaptureTest.dll'
    Invoke-CatalogBuild ($common + @('-shared','-DOVERPARSE_TEST_HOST','capture.c') + $objects + @('-lbcrypt','-o',$normalCore))
    $observerHost = Join-Path $binDir 'ObserverTestHost.exe'
    Invoke-CatalogBuild ($common + @('-municode','observer_test_host.c','-o',$observerHost))
    foreach ($variant in @('normal','catalog')) {
        $observerRoot = Join-Path $testDirectory ('observer-' + $variant)
        & $observerHost $(if ($variant -eq 'normal') {$normalCore} else {$testCore}) $observerRoot --quest-notices
        if ($LASTEXITCODE -ne 0) { throw "Observer continuity test failed: $variant" }
        if ($variant -eq 'normal' -and (Test-Path -LiteralPath (Join-Path $observerRoot 'identity-context/quest-catalog.opcat'))) {
            throw 'Normal build unexpectedly captured catalog bodies.'
        }
    }
    Invoke-CatalogBuild ($common + @('-shared','-include',$header,'-DOVERPARSE_QUEST_CATALOG_CAPTURE',
        '-DOVERPARSE_AUTOSTART','-DOVERPARSE_CALLBACK_SLOT','capture.c','capture_autostart.c') + $objects + @('-lbcrypt','-o',$core))
    Invoke-CatalogBuild ($common + @('-shared','-include',$header,'eos_capture_loader.c','-o',$loader))
    $autoTestCore = Join-Path $binDir 'QuestCatalogAutoTest.dll'
    $autoTestLoader = Join-Path $binDir 'QuestCatalogAutoLoaderTest.dll'
    $autoTestSession = Join-Path $testDirectory 'autostart-test'
    New-Item -ItemType Directory -Force $autoTestSession | Out-Null
    $autoTestHeader = Join-Path $binDir 'catalog-test-paths.h'
    @(
        ('#define AUTO_CAPTURE_DLL_PATH L"' + $autoTestCore.Replace('\','\\') + '"'),
        ('#define AUTO_SESSION_DIR L"' + $autoTestSession.Replace('\','\\') + '"'),
        ('#define AUTO_GUARD_NAME L"Local\\LazyParseQuestCatalogTest-' + $nonce + '"')
    ) | Set-Content -LiteralPath $autoTestHeader -Encoding utf8
    Invoke-CatalogBuild ($common + @('-shared','-include',$autoTestHeader,'-DOVERPARSE_QUEST_CATALOG_CAPTURE',
        '-DOVERPARSE_AUTOSTART','-DOVERPARSE_CALLBACK_SLOT','-DOVERPARSE_AUTOSTART_TEST',
        'capture.c','capture_autostart.c') + $objects + @('-lbcrypt','-o',$autoTestCore))
    Invoke-CatalogBuild ($common + @('-shared','-include',$autoTestHeader,'eos_capture_loader.c','-o',$autoTestLoader))
    $autoHost = Join-Path $binDir 'AutoCaptureTestHost.exe'
    Invoke-CatalogBuild ($common + @('-municode','autostart_test_host.c','-o',$autoHost))
    for ($attempt = 0; $attempt -lt 2; ++$attempt) {
        & $autoHost $autoTestLoader $autoTestSession
        if ($LASTEXITCODE -ne 0) { throw 'Quest catalog autostart/relaunch test failed.' }
    }
    [ordered]@{
        loader=$loader; loader_sha256=(Get-FileHash -LiteralPath $loader).Hash.ToLowerInvariant()
        core=$core; core_sha256=(Get-FileHash -LiteralPath $core).Hash.ToLowerInvariant()
        session=$session
        bridge_sha256='d20e83107079a4d65d490e27d272fd1e195a22235f5ee860cc9f9e1160e6cf9c'
        classic_sha256='b2a87ec9000caa7667677d22f51859c449f4bb686f10b876370be7b861243332'
        capture_method='bound_callback_slot'; requires_start_signal=$true; control_abi=1; fresh_run_per_launch=$true
        quest_catalog_capture=$true; quest_catalog_max_packet_bytes=262144
        test_directory=$testDirectory; callback_mock_test_passed=$true
        observer_continuity_both_builds_passed=$true; bootstrap_mock_test_passed=$true; same_build_relaunch_passed=$true
    } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $binDir 'autostart-build.json') -Encoding utf8
    Get-Content -LiteralPath (Join-Path $binDir 'autostart-build.json')
} finally { Pop-Location }
