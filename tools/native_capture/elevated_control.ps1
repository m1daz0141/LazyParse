param(
    [Parameter(Mandatory)][ValidateSet('Probe','Diagnose','Start','Stop')][string]$Action,
    [Parameter(Mandatory)][int]$ProcessId,
    [Parameter(Mandatory)][string]$ResultPath
)
$ErrorActionPreference = 'Stop'
$result = [ordered]@{ action = $Action; process_id = $ProcessId; success = $false; output = @(); error = $null }
try {
    if ($Action -eq 'Diagnose') {
        & (Join-Path $PSScriptRoot 'bin/DiagnoseAccess.exe') $ProcessId 2>&1 |
            ForEach-Object { $result.output += $_.ToString() }
        if ($LASTEXITCODE -ne 0) { throw 'Access diagnosis could not inspect the target.' }
    } else {
    $arguments = @{ ProcessId = $ProcessId }
    if ($Action -eq 'Probe') { $arguments.ProbeOnly = $true }
    if ($Action -eq 'Stop') { $arguments.Stop = $true }
    & (Join-Path $PSScriptRoot 'capture.ps1') @arguments 2>&1 |
        ForEach-Object { $result.output += $_.ToString() }
    }
    $result.success = $true
} catch {
    $result.error = $_.Exception.Message
} finally {
    $result | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $ResultPath -Encoding utf8
}
if (-not $result.success) { exit 1 }
