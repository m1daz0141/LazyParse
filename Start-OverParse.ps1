#requires -Version 7.0
param(
    [string]$Capture,
    [string]$Root
)
# Compatibility entry for existing shortcuts and historical instructions.
& (Join-Path $PSScriptRoot 'Start-LazyParse.ps1') @PSBoundParameters
