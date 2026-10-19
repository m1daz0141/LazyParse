param(
    [Parameter(Mandatory)][ValidateSet('Status','Install','Remove')][string]$Action,
    [string]$GameBin = (Join-Path ${env:ProgramFiles} 'PHANTASYSTARONLINE2\pso2_bin\sub')
)
$ErrorActionPreference = 'Stop'
$captureRoot = Join-Path $PSScriptRoot 'captures'
$buildFile = Join-Path $PSScriptRoot 'bin/eos-probe-build.json'
$installFile = Join-Path $captureRoot 'eos-probe-install.json'
if (-not (Test-Path -LiteralPath $buildFile)) { throw 'Run build_eos_probe.ps1 first.' }
$build = Get-Content -Raw -LiteralPath $buildFile | ConvertFrom-Json
if ($Action -eq 'Status') {
    if (-not (Test-Path -LiteralPath $build.marker)) {
        'No load attempt marker has been recorded. This does not distinguish an unused code path from a rejected DLL.'
        return
    }
    $bytes = [IO.File]::ReadAllBytes($build.marker)
    if ($bytes.Length -ne 24 -or [Text.Encoding]::ASCII.GetString($bytes,0,8) -ne "OPCEOS1`0" -or
        [BitConverter]::ToUInt32($bytes,8) -ne 1) { throw 'Invalid probe marker.' }
    [pscustomobject]@{
        ProcessId = [BitConverter]::ToUInt32($bytes,12)
        UTC = [DateTime]::FromFileTimeUtc([BitConverter]::ToInt64($bytes,16)).ToString('o')
        Marker = $build.marker
    }
    return
}
$running = [Diagnostics.Process]::GetProcessesByName('pso2')
try { if ($running.Length) { throw 'Exit PSO2 normally before installing or removing the probe.' } }
finally { foreach ($process in $running) { $process.Dispose() } }
$gameRoot = (Resolve-Path -LiteralPath $GameBin).Path
$destination = Join-Path $gameRoot 'eos_gaming.dll'
if ($Action -eq 'Remove') {
    if (-not (Test-Path -LiteralPath $installFile)) { throw 'No probe installation record.' }
    $install = Get-Content -Raw -LiteralPath $installFile | ConvertFrom-Json
    if (-not [string]::Equals([IO.Path]::GetFullPath($install.destination), $destination,
                              [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Installation record does not match the selected game directory.'
    }
    if (-not (Test-Path -LiteralPath $install.destination)) { throw 'Installed probe is missing; inspect the installation record.' }
    if ((Get-FileHash -LiteralPath $install.destination -Algorithm SHA256).Hash.ToLowerInvariant() -ne $install.sha256) {
        throw 'Installed file changed; refusing to remove a different DLL.'
    }
    Remove-Item -LiteralPath $install.destination
    Remove-Item -LiteralPath $installFile
    'Removed the matching probe DLL.'
    return
}
if (Test-Path -LiteralPath $destination) { throw 'eos_gaming.dll already exists; refusing to overwrite it.' }
if (Test-Path -LiteralPath $installFile) { throw 'An installation record already exists.' }
if (Test-Path -LiteralPath $build.marker) { throw 'A marker already exists; build a fresh probe before installing.' }
if ((Get-FileHash -LiteralPath (Join-Path $gameRoot 'pso2bridge.dll') -Algorithm SHA256).Hash.ToLowerInvariant() -ne $build.bridge_sha256) {
    throw 'Unsupported bridge version.'
}
if ((Get-FileHash -LiteralPath $build.dll -Algorithm SHA256).Hash.ToLowerInvariant() -ne $build.sha256) {
    throw 'Built probe DLL changed.'
}
$record = [ordered]@{ destination = $destination; sha256 = $build.sha256; marker = $build.marker }
$record | ConvertTo-Json | Set-Content -LiteralPath $installFile -Encoding utf8
try { [IO.File]::Copy($build.dll,$destination,$false) }
catch {
    Remove-Item -LiteralPath $installFile
    throw
}
"Installed load marker probe: $destination"
'Launch normally to the title screen, then exit normally. Login and combat are unnecessary for this probe.'
