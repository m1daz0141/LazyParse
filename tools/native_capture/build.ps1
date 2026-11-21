param([switch]$Bootstrap)
$ErrorActionPreference = 'Stop'
$taskRoot = $PSScriptRoot
$vendorDir = Join-Path $taskRoot 'vendor'
$zigPath = Join-Path $vendorDir 'zig-x86_64-windows-0.15.2/zig.exe'
$minhookDir = Join-Path $vendorDir 'minhook'
$minhookCommit = 'c3fcafdc10146beb5919319d0683e44e3c30d537'
if ($Bootstrap) {
    New-Item -ItemType Directory -Force $vendorDir | Out-Null
    if (-not (Test-Path -LiteralPath $zigPath)) {
        $archive = Join-Path $vendorDir 'zig-0.15.2.zip'
        Invoke-WebRequest -UseBasicParsing 'https://ziglang.org/download/0.15.2/zig-x86_64-windows-0.15.2.zip' -OutFile $archive
        if ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant() -ne '3a0ed1e8799a2f8ce2a6e6290a9ff22e6906f8227865911fb7ddedc3cc14cb0c') {
            throw 'Zig archive checksum mismatch'
        }
        Expand-Archive -LiteralPath $archive -DestinationPath $vendorDir -Force
    }
    if (-not (Test-Path -LiteralPath $minhookDir)) {
        & git clone --depth 1 --branch v1.3.4 https://github.com/TsudaKageyu/minhook.git $minhookDir
        if ($LASTEXITCODE -ne 0) { throw 'MinHook clone failed' }
    }
}
if (-not (Test-Path -LiteralPath $zigPath)) { throw 'Run build.ps1 -Bootstrap to download the portable toolchain.' }
$actualCommit = & git -C $minhookDir rev-parse HEAD
if ($LASTEXITCODE -ne 0 -or $actualCommit -ne $minhookCommit) { throw 'Unexpected MinHook revision' }
if (& git -C $minhookDir status --porcelain --untracked-files=no) { throw 'MinHook sources have local changes' }
$binDir = Join-Path $taskRoot 'bin'
New-Item -ItemType Directory -Force $binDir | Out-Null
$env:ZIG_GLOBAL_CACHE_DIR = Join-Path $taskRoot '.zig-cache/global'
$env:ZIG_LOCAL_CACHE_DIR = Join-Path $taskRoot '.zig-cache/local'
$common = @('cc','-target','x86_64-windows-gnu','-std=c11','-O2','-Wall','-Wextra', '-Werror',
    '-I', (Join-Path $minhookDir 'include'))
$hookSources = @('src/hook.c','src/buffer.c','src/trampoline.c','src/hde/hde64.c') | ForEach-Object { Join-Path $minhookDir $_ }
function Invoke-NativeBuild([string[]]$CompilerArgs) {
    & $zigPath @CompilerArgs
    if ($LASTEXITCODE -ne 0) { throw 'Native build failed' }
}
Push-Location $taskRoot
try {
    # Third-party sources are compiled separately, retaining their own warnings.
    $hookObjects = @()
    foreach ($source in $hookSources) {
        $object = Join-Path $binDir (([IO.Path]::GetFileNameWithoutExtension($source)) + '.o')
        $traceArgs = @()
        if ([IO.Path]::GetFileName($source) -eq 'hook.c') {
            $traceArgs = @('-DOPC_INTERPOSE_PROTECT','-include',(Join-Path $taskRoot 'memory_protect_trace.h'))
        }
        Invoke-NativeBuild (@('cc','-target','x86_64-windows-gnu','-O2','-I',(Join-Path $minhookDir 'include')) + $traceArgs + @('-c',$source,'-o',$object))
        $hookObjects += $object
    }
    Invoke-NativeBuild ($common + @('-c','thread_guard.c','-o',(Join-Path $binDir 'thread_guard.o')))
    $captureObjects = $hookObjects + @((Join-Path $binDir 'thread_guard.o'))
    Invoke-NativeBuild ($common + @('-shared','capture.c') + $captureObjects + @('-lbcrypt','-o',(Join-Path $binDir 'OverParseCapture.dll')))
    Invoke-NativeBuild ($common + @('-shared','-DOVERPARSE_TEST_HOST','capture.c') + $captureObjects + @('-lbcrypt','-o',(Join-Path $binDir 'OverParseCaptureTest.dll')))
    Invoke-NativeBuild ($common + @('test_host.c','-municode','-o',(Join-Path $binDir 'CaptureTestHost.exe')))
    Invoke-NativeBuild ($common + @('continuity_test_host.c','-municode','-lbcrypt','-o',(Join-Path $binDir 'ContinuityTestHost.exe')))
    Invoke-NativeBuild ($common + @('identity_test_host.c','-municode','-o',(Join-Path $binDir 'IdentityTestHost.exe')))
    Invoke-NativeBuild ($common + @('observer_test_host.c','-municode','-o',(Join-Path $binDir 'ObserverTestHost.exe')))
    Invoke-NativeBuild ($common + @('atomic_file_test_host.c','-municode','-o',(Join-Path $binDir 'AtomicFileTestHost.exe')))
    Invoke-NativeBuild ($common + @('mapped_protect_test.c','-o',(Join-Path $binDir 'MappedProtectTest.exe')))
    Invoke-NativeBuild ($common + @('binding_layout_test.c','-lbcrypt','-o',(Join-Path $binDir 'BindingLayoutTest.exe')))
    Invoke-NativeBuild ($common + @('thread_guard.c','thread_guard_test.c','-o',(Join-Path $binDir 'ThreadGuardTest.exe')))
    Invoke-NativeBuild ($common + @('-DOVERPARSE_RUNTIME_TEST','-DAUTO_SESSION_DIR=L"unused"',
        'runtime_test_host.c','capture_autostart.c','-municode','-lbcrypt','-o',(Join-Path $binDir 'AutoRuntimeTestHost.exe')))
    Copy-Item -LiteralPath (Join-Path $minhookDir 'LICENSE.txt') -Destination (Join-Path $binDir 'MinHook-LICENSE.txt')
    Get-FileHash -Algorithm SHA256 (Join-Path $binDir '*.dll'),(Join-Path $binDir '*.exe') | Select-Object Path,Hash
} finally { Pop-Location }
