param([string]$GameData = (Join-Path ${env:ProgramFiles} 'PHANTASYSTARONLINE2\pso2_bin\data\win32'))
$ErrorActionPreference = 'Stop'
$dependencyRoot = Join-Path $PSScriptRoot 'research'
$zipDll = Join-Path $dependencyRoot 'sharpziplib-1.4.2/lib/net6.0/ICSharpCode.SharpZipLib.dll'
Add-Type -LiteralPath $zipDll
$sourceFiles = Get-ChildItem -LiteralPath (Join-Path $dependencyRoot 'ZamboniLib') -Recurse -Filter '*.cs' | ForEach-Object FullName
$frameworkReferences = Get-ChildItem -LiteralPath (Join-Path $PSHOME 'ref') -Filter '*.dll' | ForEach-Object FullName
Add-Type -Path $sourceFiles -ReferencedAssemblies (@($zipDll) + $frameworkReferences) -IgnoreWarnings -WarningAction SilentlyContinue
$destination = Join-Path $dependencyRoot 'extracted'
New-Item -ItemType Directory -Force $destination | Out-Null
$counts = @{}
$evidence = @()
foreach ($archiveName in @('37198b9320a0371599550672f865205e','63f00646baf995cbdefd82dfb571fa0d','6add43e55e591db5c689d5819ca60d6d','bedfcb62d45a4694e9276376e3801594','ad26a5e8cc0e2e14f2709a3d07a6ea94','1a1ee6d8592412ac83a255ac496f4226')) {
    $archivePath = Join-Path $GameData $archiveName
    $beforeHash = (Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash
    $archive = [Zamboni.IceFile]::LoadIceFile([IO.File]::OpenRead($archivePath))
    $archiveDirectory = Join-Path $destination $archiveName
    New-Item -ItemType Directory -Force $archiveDirectory | Out-Null
    foreach ($member in @($archive.groupOneFiles) + @($archive.groupTwoFiles)) {
        if ($null -eq $member) { continue }
        $memberName = [Zamboni.IceFile]::getFileName($member)
        if ($memberName -notmatch '^(quest_\d+|quest_common|quest_counter|ui_quest)\.text$') { continue }
        $memberPath = Join-Path $archiveDirectory $memberName
        if (Test-Path -LiteralPath $memberPath) {
            $existingBytes = [IO.File]::ReadAllBytes($memberPath)
            if (-not [Linq.Enumerable]::SequenceEqual[byte]($existingBytes,$member)) { throw "Refusing to overwrite changed $memberName" }
        } else { [IO.File]::WriteAllBytes($memberPath,$member) }
        $counts[$archiveName]++
    }
    if ((Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash -ne $beforeHash) { throw 'Source archive changed' }
    $evidence += @{archive=$archiveName;sha256=$beforeHash.ToLowerInvariant();members=$counts[$archiveName];source_unchanged=$true}
}
$evidence | ConvertTo-Json | Set-Content (Join-Path $dependencyRoot 'local-archive-evidence.json') -Encoding utf8
$counts | ConvertTo-Json
