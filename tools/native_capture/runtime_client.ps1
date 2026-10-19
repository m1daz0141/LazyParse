#requires -Version 7.0
param(
    [Parameter(Mandatory)][ValidateSet('Status','Start','Stop','Shutdown')][string]$Action,
    [Parameter(Mandatory)][string]$Root,
    [string]$ExpectedProcessName = 'pso2',
    [string]$ExpectedRunName,
    [ValidateRange(0,30)][int]$WaitSeconds = 10
)
$ErrorActionPreference = 'Stop'
$rootPath = (Resolve-Path -LiteralPath $Root).Path.TrimEnd('\')
$stateFile = Join-Path $rootPath 'autostart-state.json'
function Read-SharedJson([string]$Path) {
    # An atomic publisher may briefly deny even a fully shared open. Retry only
    # sharing/lock conflicts or a transient missing file during publication;
    # malformed JSON and persistent missing files remain errors.
    for ($attempt = 0; $attempt -lt 10; $attempt++) {
        try {
            $stream = [IO.File]::Open($Path,[IO.FileMode]::Open,[IO.FileAccess]::Read,
                ([IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete))
            break
        } catch [IO.IOException] {
            $cause = $_.Exception
            while ($cause.InnerException) { $cause = $cause.InnerException }
            $code = $cause.HResult -band 0xffff
            if ($code -notin @(2,32,33) -or $attempt -eq 9) { throw }
            Start-Sleep -Milliseconds 10
        }
    }
    try {
        $reader = [IO.StreamReader]::new($stream,[Text.Encoding]::UTF8,$true)
        try { return ($reader.ReadToEnd() | ConvertFrom-Json) } finally { $reader.Dispose() }
    } finally { $stream.Dispose() }
}
function Get-RunState {
    $value = Read-SharedJson $stateFile
    if ($value.control_abi -ne 1 -or $value.run_name -notmatch '^run-[0-9]+-[0-9]+-[0-9]+$') {
        throw 'Unsupported or malformed capture runtime state.'
    }
    $expectedRun = Join-Path $rootPath $value.run_name
    if (-not [string]::Equals([IO.Path]::GetFullPath($value.run_directory),$expectedRun,[StringComparison]::OrdinalIgnoreCase)) {
        throw 'Capture run path is outside its build root.'
    }
    if ($value.capture_directory) {
        $capturePath = [IO.Path]::GetFullPath($value.capture_directory)
        $prefix = $expectedRun + '\segment-'
        if (-not $capturePath.StartsWith($prefix,[StringComparison]::OrdinalIgnoreCase) -or
            $capturePath.Substring($prefix.Length) -notmatch '^[0-9]+$') { throw 'Invalid capture segment path.' }
    }
    return $value
}
function Test-Worker($State) {
    $process = Get-Process -Id $State.pid -ErrorAction SilentlyContinue
    if (-not $process) { return $false }
    try {
        return $process.ProcessName -eq $ExpectedProcessName -and
            $process.StartTime.ToUniversalTime().ToFileTimeUtc() -le [long]$State.state_filetime
    } catch { return $false } finally { $process.Dispose() }
}
function Test-Fresh($State) {
    $age = ([DateTime]::UtcNow - [DateTime]::FromFileTimeUtc([long]$State.state_filetime)).TotalSeconds
    return $age -ge -1 -and $age -lt 5
}
if (-not (Test-Path -LiteralPath $stateFile)) {
    if ($Action -eq 'Status') { 'Runtime has not started.'; return }
    throw 'Capture runtime has not started.'
}
$state = Get-RunState
if ($ExpectedRunName -and $state.run_name -ne $ExpectedRunName) {
    throw 'Capture runtime changed before this requested command.'
}
if ($Action -eq 'Status') {
    $alive = Test-Worker $state
    $fresh = Test-Fresh $state
    $captureStatus = $null
    if ($state.capture_directory) {
        $path = Join-Path $state.capture_directory 'capture-status.json'
        if (Test-Path -LiteralPath $path) { $captureStatus = Read-SharedJson $path }
    }
    [ordered]@{
        worker_alive = $alive
        state_is_fresh = $fresh
        effective_recording = ($alive -and $fresh -and $state.recording -and $state.state -ne 'shutdown' -and
            -not $state.identity_context_error)
        runtime = $state
        capture = $captureStatus
    } | ConvertTo-Json -Depth 8
    return
}
if (-not (Test-Worker $state) -or -not (Test-Fresh $state) -or $state.state -eq 'shutdown') {
    throw 'Capture worker is absent, stopped, or its heartbeat is stale.'
}
$runPath = [IO.Path]::GetFullPath($state.run_directory)
$lockPath = Join-Path $runPath 'controller.lock'
$commandFile = Join-Path $runPath 'control.command'
$lock = [IO.File]::Open($lockPath,[IO.FileMode]::OpenOrCreate,[IO.FileAccess]::Write,[IO.FileShare]::None)
$temp = $null
try {
    $latest = Get-RunState
    if ($latest.run_name -ne $state.run_name -or -not (Test-Worker $latest) -or -not (Test-Fresh $latest)) {
        throw 'Capture worker changed while preparing the command.'
    }
    if (Test-Path -LiteralPath $commandFile) {
        $text = [IO.File]::ReadAllText($commandFile,[Text.Encoding]::ASCII)
        if ($text -match '\AOPC-CONTROL-1\n([0-9]+)\n(start|stop|shutdown)\n\z' -and
            [uint64]$Matches[1] -gt [uint64]$latest.last_command_id) { throw 'A prior command is still awaiting acknowledgement.' }
    }
    if ($Action -eq 'Start') {
        if ($latest.desired_recording) { throw 'Capture is already requested.' }
        $probe = Read-SharedJson (Join-Path $runPath 'binding-probe.json')
        if ($probe.stage -ne 'binding_ready' -or $probe.win32_error -ne 0) { throw 'Binding validation has not passed.' }
        if ($latest.PSObject.Properties.Name -contains 'identity_observing' -and
            (-not $latest.identity_observing -or $latest.identity_context_error)) {
            throw "Identity observation is not ready: Win32=$($latest.identity_context_error)."
        }
    }
    if ([uint64]$latest.last_command_id -eq [uint64]::MaxValue) { throw 'Command sequence exhausted.' }
    $nextId = [uint64]$latest.last_command_id + [uint64]1
    $temp = Join-Path $runPath ('control-' + [Guid]::NewGuid().ToString('N') + '.tmp')
    $text = "OPC-CONTROL-1`n$nextId`n$($Action.ToLowerInvariant())`n"
    [IO.File]::WriteAllText($temp,$text,[Text.Encoding]::ASCII)
    [IO.File]::Move($temp,$commandFile,$true)
    $temp = $null
} finally {
    $lock.Dispose()
    if ($temp -and (Test-Path -LiteralPath $temp)) { Remove-Item -LiteralPath $temp }
}
$deadline = [DateTime]::UtcNow.AddSeconds($WaitSeconds)
do {
    $current = Get-RunState
    if ($current.run_name -ne $state.run_name) { throw 'Capture runtime restarted before acknowledgement.' }
    if ([uint64]$current.last_command_id -eq $nextId) {
        if ($current.last_command_error) { throw "Capture command failed: Win32=$($current.last_command_error), state=$($current.state)" }
        "Acknowledged: $Action command=$nextId state=$($current.state)"
        if ($current.capture_directory) { "Capture: $($current.capture_directory)" }
        return
    }
    if ($WaitSeconds) { Start-Sleep -Milliseconds 100 }
} while ([DateTime]::UtcNow -lt $deadline)
if (-not $WaitSeconds) { "Submitted: $Action command=$nextId; check Status for acknowledgement."; return }
throw "Command $nextId is pending; check Status before submitting another command."
