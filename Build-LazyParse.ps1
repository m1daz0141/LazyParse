#requires -Version 7.0
param([switch]$InstallBuildTools)
$ErrorActionPreference = 'Stop'
$python = Join-Path $PSScriptRoot '.venv-build/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    & py -3 -m venv (Join-Path $PSScriptRoot '.venv-build')
    if ($LASTEXITCODE -ne 0) { throw 'Cannot create the build environment.' }
}
if ($InstallBuildTools) {
    & $python -m pip install -r (Join-Path $PSScriptRoot 'tools/portable/requirements-build.txt')
    if ($LASTEXITCODE -ne 0) { throw 'Build dependencies could not be installed.' }
}
& (Join-Path $PSScriptRoot 'tools/portable/build_native.ps1')
if (-not $?) { throw 'Portable native build failed.' }
$env:PYINSTALLER_CONFIG_DIR = Join-Path $PSScriptRoot 'build/pyinstaller-cache'
& $python -m PyInstaller --noconfirm --distpath (Join-Path $PSScriptRoot 'dist') --workpath (Join-Path $PSScriptRoot 'build/portable') (Join-Path $PSScriptRoot 'tools/portable/LazyParse.spec')
if ($LASTEXITCODE -ne 0) { throw 'LazyParse executable build failed.' }
Get-Item -LiteralPath (Join-Path $PSScriptRoot 'dist/LazyParse.exe') | Select-Object FullName,Length
