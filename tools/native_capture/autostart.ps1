param(
    [Parameter(Mandatory)][ValidateSet('Install','Status','Start','Stop','Shutdown','Remove')][string]$Action,
    [string]$GameBin = (Join-Path ${env:ProgramFiles} 'PHANTASYSTARONLINE2\pso2_bin\sub'),
    [string]$BuildManifest = (Join-Path $PSScriptRoot 'bin/autostart-build.json')
)
$ErrorActionPreference = 'Stop'
$build = Get-Content -Raw -LiteralPath $BuildManifest | ConvertFrom-Json
$installFile = Join-Path $PSScriptRoot 'captures/autostart-install.json'
if ($Action -in @('Status','Start','Stop','Shutdown')) {
    & (Join-Path $PSScriptRoot 'runtime_client.ps1') -Action $Action -Root $build.session
    return
}
$running = [Diagnostics.Process]::GetProcessesByName('pso2')
try { if ($running.Length) { throw 'Exit PSO2 normally before changing the installed loader.' } }
finally { foreach ($process in $running) { $process.Dispose() } }
$gameRoot = (Resolve-Path -LiteralPath $GameBin).Path
$destination = Join-Path $gameRoot 'eos_gaming.dll'
if ($Action -eq 'Remove') {
    $install = Get-Content -Raw -LiteralPath $installFile | ConvertFrom-Json
    if (-not [string]::Equals([IO.Path]::GetFullPath($install.destination),$destination,[StringComparison]::OrdinalIgnoreCase)) {
        throw 'Installation record path mismatch.'
    }
    if ((Get-FileHash -LiteralPath $destination -Algorithm SHA256).Hash.ToLowerInvariant() -ne $install.sha256) {
        throw 'Installed file changed; refusing to remove it.'
    }
    Remove-Item -LiteralPath $destination
    Remove-Item -LiteralPath $installFile
    'Removed matching capture loader.'
    return
}
if (Test-Path -LiteralPath $destination) { throw 'eos_gaming.dll exists; refusing to overwrite it.' }
if (Test-Path -LiteralPath $installFile) { throw 'An autostart loader is already installed.' }
foreach ($item in @(@('pso2bridge.dll',$build.bridge_sha256),@('pso2classic.dll',$build.classic_sha256))) {
    if ((Get-FileHash -LiteralPath (Join-Path $gameRoot $item[0]) -Algorithm SHA256).Hash.ToLowerInvariant() -ne $item[1]) {
        throw ('Unsupported game DLL: ' + $item[0])
    }
}
if ((Get-FileHash -LiteralPath $build.loader -Algorithm SHA256).Hash.ToLowerInvariant() -ne $build.loader_sha256 -or
    (Get-FileHash -LiteralPath $build.core -Algorithm SHA256).Hash.ToLowerInvariant() -ne $build.core_sha256) {
    throw 'Built loader or capture DLL changed.'
}
# Each launch creates a new run, so previous state and stopped segments can
# remain in the build root. The worker never reuses a prior command directory.
[ordered]@{destination=$destination;sha256=$build.loader_sha256;session=$build.session} |
    ConvertTo-Json | Set-Content -LiteralPath $installFile -Encoding utf8
try { [IO.File]::Copy($build.loader,$destination,$false) }
catch { Remove-Item -LiteralPath $installFile; throw }
"Installed: $destination"
"Session: $($build.session)"
