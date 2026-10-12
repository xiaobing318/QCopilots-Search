#requires -Version 7.0
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string] $BenchmarkExe,
    [Parameter(Mandatory = $true)][string] $ExternalRoot,
    [Parameter(Mandatory = $true)][string] $QtBin,
    [ValidateSet('Validate', 'Run')][string] $Mode = 'Validate',
    [string] $Source = (Join-Path $PSScriptRoot '../..'),
    [Parameter(Mandatory = $true)][string] $ExpectedCommit,
    [Parameter(Mandatory = $true)][string] $BuildRecord,
    [Parameter(Mandatory = $true)][string] $Python,
    [ValidateRange(1, 1024)][int] $BudgetGiB = 7,
    [ValidateRange(1, 1024)][int] $ReserveGiB = 3
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if (-not $IsWindows) { throw 'This monitored native runner requires Windows PowerShell 7.' }

$GiB = [long]1073741824
$MiB = [long]1048576
$directoryBudget = [long]($BudgetGiB * $GiB)
$freeReserve = [long]($ReserveGiB * $GiB)
$directoryBuffer = 256 * $MiB
$freeBuffer = 128 * $MiB
$directoryGuard = $directoryBudget - $directoryBuffer
$freeGuard = $freeReserve + $freeBuffer
$requiredStartingFree = $directoryBudget + $freeReserve + $directoryBuffer + $freeBuffer
$sampleIntervalMs = 250
$expectedAnnEfSearch = 4096

function Resolve-ExistingPath([string] $Path, [bool] $Directory) {
    $item = Get-Item -LiteralPath $Path -ErrorAction Stop
    if ($Directory -and -not $item.PSIsContainer) { throw "Not a directory: $Path" }
    if (-not $Directory -and $item.PSIsContainer) { throw "Not a file: $Path" }
    return [System.IO.Path]::GetFullPath($item.FullName)
}

function Get-HeavyProcessState {
    $items = [System.Collections.Generic.List[object]]::new()
    $msbuildFirst = @{}
    foreach ($candidate in @(Get-Process -ErrorAction SilentlyContinue)) {
        $name = $candidate.ProcessName
        # Every product component and test owns the local acceptance slot,
        # including physical-file traversal and apparently idle GUI/services.
        if ($name -notmatch '(?i)^(cmake|ctest|ninja|cl|link|msbuild)$|^qcs(?:-|$)|semantic|benchmark') { continue }
        $cpu = $null
        $path = $null
        try { $cpu = $candidate.TotalProcessorTime.TotalSeconds } catch {}
        try { $path = $candidate.Path } catch {}
        $entry = [ordered]@{
            id = $candidate.Id
            name = $name
            executablePath = $path
            cpuSeconds = $cpu
            cpuDeltaSeconds = $null
            blocking = $true
            reason = 'Known heavy process is present'
        }
        if ($name -ieq 'msbuild') { $msbuildFirst[$candidate.Id] = $entry }
        $items.Add($entry)
    }
    if ($msbuildFirst.Count -gt 0) {
        Start-Sleep -Milliseconds 1000
        foreach ($idValue in @($msbuildFirst.Keys)) {
            $entry = $msbuildFirst[$idValue]
            $later = Get-Process -Id $idValue -ErrorAction SilentlyContinue
            if ($null -eq $later) {
                $entry.blocking = $false
                $entry.reason = 'Process exited during the read-only CPU sample'
                continue
            }
            try {
                if ($null -eq $entry.cpuSeconds) { throw 'Initial CPU reading unavailable' }
                $delta = $later.TotalProcessorTime.TotalSeconds - [double]$entry.cpuSeconds
                $entry.cpuDeltaSeconds = $delta
                $entry.blocking = $delta -gt 0.05
                $entry.reason = if ($entry.blocking) { 'MSBuild CPU delta exceeds 0.05 seconds over the one-second sample' } else { 'Idle MSBuild node reuse process, recorded without blocking' }
            } catch {
                $entry.reason = 'MSBuild CPU activity could not be verified'
                $entry.blocking = $true
            }
        }
    }
    return @($items)
}

function Get-DirectoryBytes([string] $Path) {
    if (-not [System.IO.Directory]::Exists($Path)) { return [long]0 }
    $enumeration = [System.IO.EnumerationOptions]::new()
    $enumeration.RecurseSubdirectories = $true
    $enumeration.IgnoreInaccessible = $false
    $enumeration.AttributesToSkip = [System.IO.FileAttributes]::ReparsePoint
    [long] $sum = 0
    foreach ($file in [System.IO.Directory]::EnumerateFiles($Path, '*', $enumeration)) {
        try { $sum += [System.IO.FileInfo]::new($file).Length }
        catch [System.IO.FileNotFoundException] { }
        catch [System.IO.DirectoryNotFoundException] { }
    }
    return $sum
}

function Get-PropertyValue($Object, [string] $Name) {
    if ($null -eq $Object) { return $null }
    $property = $Object.PSObject.Properties[$Name]
    if ($null -eq $property) { return $null }
    return $property.Value
}

function Get-PropertyArray($Object, [string] $Name) {
    # Emit one array object so assignment preserves empty and singleton arrays.
    # Read the property directly instead of a helper that enumerates its value.
    if ($null -eq $Object) { return ,([object[]]@()) }
    $property = $Object.PSObject.Properties[$Name]
    if ($null -eq $property -or $null -eq $property.Value) { return ,([object[]]@()) }
    return ,([object[]]@($property.Value))
}

function Test-True($Value) { return $Value -is [bool] -and $Value }

function Test-Number($Value, [double] $Minimum, [double] $Maximum) {
    if ($null -eq $Value -or $Value -is [string] -or $Value -is [bool]) { return $false }
    try {
        $number = [double]$Value
        return [double]::IsFinite($number) -and $number -ge $Minimum -and $number -le $Maximum
    } catch { return $false }
}

function Get-JsonProperty($Object, [string] $Name) {
    # Preserve the raw property shape, including scalar arrays, for schema checks.
    if ($Object -isnot [pscustomobject]) { return $null }
    $property = $Object.PSObject.Properties[$Name]
    if ($null -eq $property) { return $null }
    return ,$property.Value
}

function Test-JsonNumber($Value, [decimal] $Minimum, [decimal] $Maximum, [bool] $Integer = $false) {
    if ($Value -isnot [byte] -and $Value -isnot [sbyte] -and
        $Value -isnot [int16] -and $Value -isnot [uint16] -and
        $Value -isnot [int32] -and $Value -isnot [uint32] -and
        $Value -isnot [int64] -and $Value -isnot [uint64] -and
        $Value -isnot [single] -and $Value -isnot [double] -and $Value -isnot [decimal]) { return $false }
    try {
        if ($Value -is [single] -or $Value -is [double]) {
            $floating = [double]$Value
            if (-not [double]::IsFinite($floating)) { return $false }
            if (-not $Integer) { return $floating -ge [double]$Minimum -and $floating -le [double]$Maximum }
            # Check before decimal conversion can round a tiny fraction to zero.
            if ($floating -ne [Math]::Truncate($floating)) { return $false }
            $number = [decimal]([System.Numerics.BigInteger]::new($floating))
        } else { $number = [decimal]$Value }
        return $number -ge $Minimum -and $number -le $Maximum -and
            (-not $Integer -or $number -eq [decimal]::Truncate($number))
    } catch { return $false }
}

function Test-JsonArray($Value) {
    return $Value -is [array] -and $Value.Rank -eq 1
}

function Assert-ReportContract([bool] $Condition, [string] $Message) {
    if (-not $Condition) { throw $Message }
}

function Test-CloseReportNumber($Value, [double] $Expected) {
    if (-not (Test-JsonNumber $Value 0 ([decimal]::MaxValue))) { return $false }
    # Allow floating-point serialization roundoff, not a different measured duration.
    return [Math]::Abs([double]$Value - $Expected) -le 1e-9 * [Math]::Max(1.0, [Math]::Abs($Expected))
}

function Test-ConcurrentRebuildContract($Result) {
    try {
        $data = Get-JsonProperty $Result 'concurrentRebuild'
        Assert-ReportContract ($data -is [pscustomobject]) 'concurrentRebuild must be an object'
        Assert-ReportContract ((Get-JsonProperty $data 'timingClock') -is [string] -and
            (Get-JsonProperty $data 'timingClock') -ceq 'steady-clock-relative-nanoseconds') 'Unexpected rebuild timing clock'
        Assert-ReportContract (Test-True (Get-JsonProperty $data 'singleShard')) 'singleShard must be true'
        Assert-ReportContract (Test-True (Get-JsonProperty $data 'passed')) 'concurrentRebuild.passed must be true'
        Assert-ReportContract (Test-JsonNumber (Get-JsonProperty $data 'observedBuilds') 1 1 $true) 'observedBuilds must be exactly one'
        $builds = Get-JsonProperty $data 'buildIntervals'
        Assert-ReportContract ((Test-JsonArray $builds) -and $builds.Count -eq 1) 'Exactly one build interval is required'
        $build = $builds[0]
        $buildStart = Get-JsonProperty $build 'startNanoseconds'
        $buildEnd = Get-JsonProperty $build 'endNanoseconds'
        Assert-ReportContract ((Test-JsonNumber $buildStart 0 ([long]::MaxValue) $true) -and
            (Test-JsonNumber $buildEnd 0 ([long]::MaxValue) $true)) 'Build timestamps must be nonnegative integers'
        Assert-ReportContract ([decimal]$buildEnd -gt [decimal]$buildStart) 'Build interval must have positive duration'
        $queries = Get-JsonProperty $data 'queryIntervals'
        Assert-ReportContract ((Test-JsonArray $queries) -and $queries.Count -ge 1 -and $queries.Count -le 500) 'One to 500 query intervals are required'
        Assert-ReportContract (Test-JsonNumber (Get-JsonProperty $data 'measuredQueries') $queries.Count $queries.Count $true) 'measuredQueries must equal the query interval count'
        $overlapDurations = [System.Collections.Generic.List[double]]::new()
        foreach ($interval in $queries) {
            $queryStart = Get-JsonProperty $interval 'startNanoseconds'
            $queryEnd = Get-JsonProperty $interval 'endNanoseconds'
            Assert-ReportContract ((Test-JsonNumber $queryStart 0 ([long]::MaxValue) $true) -and
                (Test-JsonNumber $queryEnd 0 ([long]::MaxValue) $true)) 'Query timestamps must be nonnegative integers'
            Assert-ReportContract ([decimal]$queryEnd -gt [decimal]$queryStart) 'Query interval must have positive duration'
            $duration = [double](([decimal]$queryEnd - [decimal]$queryStart) / 1000000)
            Assert-ReportContract (Test-CloseReportNumber (Get-JsonProperty $interval 'durationMilliseconds') $duration) 'Query duration does not match its timestamps'
            $overlaps = [decimal]$queryStart -lt [decimal]$buildEnd -and [decimal]$queryEnd -gt [decimal]$buildStart
            $reportedOverlap = Get-JsonProperty $interval 'overlapsBuild'
            Assert-ReportContract ($reportedOverlap -is [bool] -and $reportedOverlap -eq $overlaps) 'Query overlap flag disagrees with its interval'
            if ($overlaps) { $overlapDurations.Add($duration) }
        }
        Assert-ReportContract ($overlapDurations.Count -ge 1) 'At least one query must truly overlap the build'
        Assert-ReportContract (Test-JsonNumber (Get-JsonProperty $data 'overlappingQueries') $overlapDurations.Count $overlapDurations.Count $true) 'overlappingQueries does not match the intervals'
        $sorted = @($overlapDurations | Sort-Object)
        $recomputed = [ordered]@{}
        foreach ($quantile in @(@{ name = 'p50Milliseconds'; fraction = .50 }, @{ name = 'p95Milliseconds'; fraction = .95 }, @{ name = 'p99Milliseconds'; fraction = .99 })) {
            $rank = [int][Math]::Ceiling($quantile.fraction * $sorted.Count) - 1
            $value = [double]$sorted[$rank]
            Assert-ReportContract (Test-CloseReportNumber (Get-JsonProperty $data $quantile.name) $value) ('Incorrect overlap-only nearest-rank ' + $quantile.name)
            $recomputed[$quantile.name] = $value
        }
        Assert-ReportContract ($recomputed.p95Milliseconds -le 1000) 'Recomputed overlapping-query P95 exceeds 1000 ms'
        return [pscustomobject]@{ passed = $true; observedBuilds = 1; measuredQueries = $queries.Count; overlappingQueries = $overlapDurations.Count; recomputed = $recomputed }
    } catch { return [pscustomobject]@{ passed = $false; error = $_.Exception.Message } }
}

function Test-MutationReportContract($Result) {
    try {
        $data = Get-JsonProperty $Result 'mutations'
        Assert-ReportContract ($data -is [pscustomobject]) 'mutations must be an object'
        Assert-ReportContract (Test-True (Get-JsonProperty $data 'passed')) 'mutations.passed must be true'
        Assert-ReportContract ((Get-JsonProperty $data 'queryScope') -is [string] -and
            (Get-JsonProperty $data 'queryScope') -ceq 'filtered-exact-file-plus-global-production-search-sample') 'Unexpected mutation query scope'
        $fileCount = Get-JsonProperty $data 'filesPerPhase'
        $segmentCount = Get-JsonProperty $Result 'segmentsPerFile'
        $vectorCount = Get-JsonProperty $data 'vectorsPerPhase'
        Assert-ReportContract (Test-JsonNumber $fileCount 1000 1000 $true) 'filesPerPhase must be 1000'
        Assert-ReportContract (Test-JsonNumber $segmentCount 10 10 $true) 'segmentsPerFile must be 10'
        Assert-ReportContract ((Test-JsonNumber $vectorCount 10000 10000 $true) -and
            [decimal]$vectorCount -eq [decimal]$fileCount * [decimal]$segmentCount) 'vectorsPerPhase must equal filesPerPhase times segmentsPerFile and be 10000'
        $phases = Get-JsonProperty $data 'phases'
        Assert-ReportContract ((Test-JsonArray $phases) -and $phases.Count -eq 3) 'Exactly three mutation phases are required'
        $names = @('add', 'update', 'delete')
        for ($index = 0; $index -lt $names.Count; ++$index) {
            $phase = $phases[$index]
            Assert-ReportContract ((Get-JsonProperty $phase 'phase') -is [string] -and
                (Get-JsonProperty $phase 'phase') -ceq $names[$index]) 'Mutation phases must be ordered add, update, delete'
            Assert-ReportContract (Test-True (Get-JsonProperty $phase 'passed')) ('Mutation phase failed: ' + $names[$index])
            foreach ($field in @('filesChecked', 'keywordQueries', 'vectorQueries')) {
                Assert-ReportContract (Test-JsonNumber (Get-JsonProperty $phase $field) $fileCount $fileCount $true) ($names[$index] + '.' + $field + ' must equal filesPerPhase')
            }
            foreach ($field in @('staleHitCount', 'retiredSegmentIdsRemaining', 'globalSearchStaleHitCount')) {
                Assert-ReportContract (Test-JsonNumber (Get-JsonProperty $phase $field) 0 0 $true) ($names[$index] + '.' + $field + ' must be zero')
            }
            $retired = if ($index -eq 0) { 0 } else { 10000 }
            Assert-ReportContract (Test-JsonNumber (Get-JsonProperty $phase 'retiredSegmentIdsChecked') $retired $retired $true) ('Incorrect retired segment coverage for ' + $names[$index])
            Assert-ReportContract (Test-JsonNumber (Get-JsonProperty $phase 'globalSearchQueries') 16 16 $true) ('Exactly 16 global production search queries are required for ' + $names[$index])
            Assert-ReportContract (Test-JsonNumber (Get-JsonProperty $phase 'globalSearchCheckedHits') 1 ([long]::MaxValue) $true) ('At least one global production search hit must be checked for ' + $names[$index])
            $globalVectorCount = if ($index -lt 2) { 1010000 } else { 1000000 }
            Assert-ReportContract (Test-JsonNumber (Get-JsonProperty $phase 'globalSearchVectorCount') $globalVectorCount $globalVectorCount $true) ('Incorrect global production search vector count for ' + $names[$index])
            Assert-ReportContract ((Get-JsonProperty $phase 'globalSearchBackend') -is [string] -and
                (Get-JsonProperty $phase 'globalSearchBackend') -ceq 'ann') ('Million-vector global production search must use ANN for ' + $names[$index])
        }
        return [pscustomobject]@{ passed = $true; checkedPhases = 3; filesPerPhase = $fileCount; vectorsPerPhase = $vectorCount }
    } catch { return [pscustomobject]@{ passed = $false; error = $_.Exception.Message } }
}

function Test-RecoveryReportContract($Result) {
    try {
        $phases = Get-JsonProperty $Result 'recoveryFaults'
        $names = @('committed-before-ann', 'uncommitted-sqlite', 'ann-file-saved-before-manifest', 'ann-manifest-published-before-outbox-cleanup')
        $dimensions = @(700, 700, 702, 703)
        Assert-ReportContract ((Test-JsonArray $phases) -and $phases.Count -eq 4) 'Exactly four recovery phases are required'
        $versions = [System.Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
        $tokensSeen = [System.Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
        [decimal] $previousId = 0
        $firstVersion = $null
        $firstToken = $null
        for ($index = 0; $index -lt $names.Count; ++$index) {
            $phase = $phases[$index]
            Assert-ReportContract ((Get-JsonProperty $phase 'faultPoint') -is [string] -and
                (Get-JsonProperty $phase 'faultPoint') -ceq $names[$index]) 'Recovery phases are missing or out of order'
            Assert-ReportContract (Test-True (Get-JsonProperty $phase 'passed')) ('Recovery phase failed: ' + $names[$index])
            Assert-ReportContract (Test-JsonNumber (Get-JsonProperty $phase 'childExitCode') 73 73 $true) 'Recovery child must exit at fault point 73'
            $version = Get-JsonProperty $phase 'expectedVersion'
            $observedVersion = Get-JsonProperty $phase 'observedVersion'
            $tokenValue = Get-JsonProperty $phase 'expectedToken'
            $observedToken = Get-JsonProperty $phase 'observedToken'
            Assert-ReportContract ($version -is [string] -and -not [string]::IsNullOrWhiteSpace($version) -and
                $observedVersion -is [string] -and $version -ceq $observedVersion) 'Expected and observed recovery versions must match and be nonempty'
            Assert-ReportContract ($tokenValue -is [string] -and -not [string]::IsNullOrWhiteSpace($tokenValue) -and
                $observedToken -is [string] -and $tokenValue -ceq $observedToken) 'Expected and observed recovery tokens must match and be nonempty'
            if ($index -eq 1) {
                Assert-ReportContract ($version -ceq $firstVersion -and $tokenValue -ceq $firstToken) 'Rollback must preserve the first committed version and token'
            } else {
                Assert-ReportContract ($versions.Add($version) -and $tokensSeen.Add($tokenValue)) 'Each writing recovery phase needs a distinct version and token'
                if ($index -eq 0) { $firstVersion = $version; $firstToken = $tokenValue }
            }
            Assert-ReportContract (Test-JsonNumber (Get-JsonProperty $phase 'expectedVectorDimension') $dimensions[$index] $dimensions[$index] $true) 'Unexpected recovery vector dimension'
            Assert-ReportContract (Test-JsonNumber (Get-JsonProperty $phase 'observedVectorDimension') $dimensions[$index] $dimensions[$index] $true) 'Observed recovery vector dimension differs from the expected value'
            Assert-ReportContract (Test-JsonNumber (Get-JsonProperty $phase 'observedVectorCosine') .999 1.000001) 'Recovery vector cosine is outside tolerance'
            Assert-ReportContract (Test-True (Get-JsonProperty $phase 'vectorMatched')) 'Recovery vectorMatched must be true'
            Assert-ReportContract (Test-JsonNumber (Get-JsonProperty $phase 'previousSegmentId') $previousId $previousId $true) 'Recovery previousSegmentId chain is broken'
            $observedId = Get-JsonProperty $phase 'observedSegmentId'
            Assert-ReportContract (Test-JsonNumber $observedId 1 ([long]::MaxValue) $true) 'Observed segment ID must be a positive integer'
            if ($index -eq 1) { Assert-ReportContract ([decimal]$observedId -eq $previousId) 'Rollback must preserve the segment ID' }
            else { Assert-ReportContract ([decimal]$observedId -gt $previousId) 'Writing recovery phase must create a newer segment ID' }
            $previousId = [decimal]$observedId
            foreach ($field in @('staleVersionHits', 'oldKeywordHits', 'uncommittedCorruptionRows')) {
                Assert-ReportContract (Test-JsonNumber (Get-JsonProperty $phase $field) 0 0 $true) ($field + ' must be zero in every recovery phase')
            }
            if ($index -eq 3) {
                Assert-ReportContract (Test-JsonNumber (Get-JsonProperty $phase 'injectedSameShardEvents') 2048 2048 $true) 'Exactly 2048 same-shard events must be injected'
                Assert-ReportContract (Test-JsonNumber (Get-JsonProperty $phase 'stalePublishedOutboxEvents') 0 0 $true) 'Published outbox events must be removed'
                Assert-ReportContract (Test-True (Get-JsonProperty $phase 'postRecoveryWritePassed')) 'Post-recovery write verification must pass'
            }
        }
        return [pscustomobject]@{ passed = $true; checkedPhases = 4 }
    } catch { return [pscustomobject]@{ passed = $false; error = $_.Exception.Message } }
}

function Write-JsonFile([string] $Path, $Value) {
    $text = $Value | ConvertTo-Json -Depth 15
    $temporaryFile = $Path + '.writing'
    [System.IO.File]::WriteAllText($temporaryFile, $text, [System.Text.UTF8Encoding]::new($false))
    [System.IO.File]::Move($temporaryFile, $Path, $true)
}

$resolvedExe = Resolve-ExistingPath $BenchmarkExe $false
$resolvedExternalRoot = Resolve-ExistingPath $ExternalRoot $true
$resolvedQtBin = Resolve-ExistingPath $QtBin $true
if ([System.IO.Path]::GetFileName($resolvedExe) -ine 'qcs-benchmark.exe') {
    throw 'BenchmarkExe must identify qcs-benchmark.exe'
}
if (-not [System.IO.File]::Exists([System.IO.Path]::Combine($resolvedQtBin, 'Qt6Core.dll'))) {
    throw 'QtBin does not contain Qt6Core.dll'
}
if (([System.IO.DirectoryInfo]::new($resolvedExternalRoot).Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
    throw 'ExternalRoot must be a real directory so its volume budget can be measured unambiguously'
}
$ancestor = [System.IO.DirectoryInfo]::new($resolvedExternalRoot)
while ($null -ne $ancestor) {
    if (($ancestor.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw 'ExternalRoot ancestors must not redirect writes to another volume through a reparse point'
    }
    if ([System.IO.File]::Exists([System.IO.Path]::Combine($ancestor.FullName, 'CMakeLists.txt')) -and
        [System.IO.Directory]::Exists([System.IO.Path]::Combine($ancestor.FullName, 'src'))) {
        throw 'ExternalRoot must be outside a source tree'
    }
    $ancestor = $ancestor.Parent
}
foreach ($category in @('runs', 'reports')) {
    $categoryPath = [System.IO.Path]::Combine($resolvedExternalRoot, $category)
    if ([System.IO.Directory]::Exists($categoryPath) -and
        (([System.IO.DirectoryInfo]::new($categoryPath).Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0)) {
        throw "Existing $category must not redirect the run or its evidence through a reparse point"
    }
}

$volume = [System.IO.DriveInfo]::new([System.IO.Path]::GetPathRoot($resolvedExternalRoot))
if (-not $volume.IsReady) { throw 'ExternalRoot volume is not ready' }
$runId = 'qcs-million-' + [Guid]::NewGuid().ToString('N').Substring(0, 12)
$runParent = [System.IO.Path]::Combine($resolvedExternalRoot, 'runs', $runId)
$reportsDir = [System.IO.Path]::Combine($resolvedExternalRoot, 'reports')
$benchmarkReport = [System.IO.Path]::Combine($reportsDir, $runId + '.benchmark.json')
$monitorReport = [System.IO.Path]::Combine($reportsDir, $runId + '.monitor.json')
$sampleLog = [System.IO.Path]::Combine($reportsDir, $runId + '.samples.jsonl')
$stdoutLog = [System.IO.Path]::Combine($reportsDir, $runId + '.stdout.log')
$stderrLog = [System.IO.Path]::Combine($reportsDir, $runId + '.stderr.log')
$arguments = @(
    '--profile-dir', $runParent,
    '--files', '100000',
    '--segments-per-file', '10',
    '--queries', '100',
    '--timings', '500',
    '--seed', '20261011',
    '--enforce-folder-rules', 'true',
    '--keep',
    '--min-free-gib', $ReserveGiB.ToString([Globalization.CultureInfo]::InvariantCulture),
    '--budget-gib', $BudgetGiB.ToString([Globalization.CultureInfo]::InvariantCulture),
    '--report', $benchmarkReport,
    '--load-note', 'Isolated million-vector run. No known active heavy process was present at launch. Idle MSBuild node-reuse processes are recorded separately.'
)

$Python = (Get-Command $Python -CommandType Application -ErrorAction Stop).Source
$preflightTool = Join-Path $PSScriptRoot 'qcs-physical-preflight.py'
$preflightArguments = @('-B', $preflightTool, '--source', $Source, '--build-root', $resolvedExternalRoot,
    '--qt-prefix', ([IO.Directory]::GetParent($resolvedQtBin).FullName),
    '--build-record', $BuildRecord, '--expected-commit', $ExpectedCommit)
function Get-CandidatePreflight {
    $raw = & $Python @preflightArguments
    $code = $LASTEXITCODE
    $checked = ($raw -join "`n") | ConvertFrom-Json -Depth 30
    if ($code -ne 0 -or -not $checked.passed) { throw ('Candidate provenance preflight failed: ' + ($checked.errors -join ', ')) }
    $producer = $checked.producers.'qcs-benchmark'
    if ([IO.Path]::GetFullPath($producer.path) -ine $resolvedExe -or
        $producer.actualSha256 -cne (Get-FileHash -LiteralPath $resolvedExe -Algorithm SHA256).Hash.ToLowerInvariant()) {
        throw 'BenchmarkExe is not the producer bound by the verified candidate build receipt.'
    }
    return $checked
}
$candidate = Get-CandidatePreflight

$heavyProcesses = @(Get-HeavyProcessState)
$blockers = @($heavyProcesses | Where-Object { $_.blocking })
$availableAtValidation = $volume.AvailableFreeSpace
$binaryHash = (Get-FileHash -LiteralPath $resolvedExe -Algorithm SHA256).Hash.ToLowerInvariant()
$plan = [ordered]@{
    formatVersion = 1
    mode = $Mode
    runId = $runId
    checkedUtc = [DateTime]::UtcNow.ToString('o')
    machine = [ordered]@{
        name = [Environment]::MachineName
        os = [System.Runtime.InteropServices.RuntimeInformation]::OSDescription
        architecture = [System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString()
        logicalProcessors = [Environment]::ProcessorCount
        powershellVersion = $PSVersionTable.PSVersion.ToString()
    }
    benchmarkExe = $resolvedExe
    benchmarkSha256 = $binaryHash
    candidate = $candidate
    wrapperSha256 = (Get-FileHash -LiteralPath $PSCommandPath -Algorithm SHA256).Hash.ToLowerInvariant()
    preflightSha256 = (Get-FileHash -LiteralPath $preflightTool -Algorithm SHA256).Hash.ToLowerInvariant()
    expectedAnnEfSearch = $expectedAnnEfSearch
    externalRoot = $resolvedExternalRoot
    qtBin = $resolvedQtBin
    runParent = $runParent
    arguments = $arguments
    benchmarkReport = $benchmarkReport
    monitorReport = $monitorReport
    sampleLog = $sampleLog
    stdoutLog = $stdoutLog
    stderrLog = $stderrLog
    directoryBudgetBytes = $directoryBudget
    directoryStopThresholdBytes = $directoryGuard
    directoryBufferBytes = $directoryBuffer
    freeReserveBytes = $freeReserve
    freeStopThresholdBytes = $freeGuard
    freeBufferBytes = $freeBuffer
    requiredStartingFreeBytes = $requiredStartingFree
    availableFreeBytes = $availableAtValidation
    requestedSampleIntervalMilliseconds = $sampleIntervalMs
    heavyProcesses = $heavyProcesses
    blockingProcesses = $blockers
    readyToRun = ($blockers.Count -eq 0 -and $availableAtValidation -ge $requiredStartingFree)
    validationScope = 'Read-only launch preflight. This does not execute the benchmark or evaluate any final acceptance gate.'
    finalAcceptanceEvaluatedByValidate = $false
    deletesAnyDirectory = $false
    createsBenchmarkProcessInValidateMode = $false
}

if ($Mode -eq 'Validate') {
    $plan | ConvertTo-Json -Depth 12
    if ($plan.readyToRun) { exit 0 } else { exit 2 }
}

# Recheck immediately before creating this run's files. Persistent idle MSBuild
# nodes do not block, but they remain visible in the saved preflight evidence.
$launchProcesses = @(Get-HeavyProcessState)
$launchBlockers = @($launchProcesses | Where-Object { $_.blocking })
$startingFree = $volume.AvailableFreeSpace
if ($launchBlockers.Count -ne 0 -or $startingFree -lt $requiredStartingFree) {
    $plan.readyToRun = $false
    $plan.blockingProcesses = $launchBlockers
    $plan.availableFreeBytes = $startingFree
    $plan | ConvertTo-Json -Depth 12
    throw 'Run refused before creating a profile: active heavy processes or insufficient free space. No process was terminated.'
}
$plan.heavyProcesses = $launchProcesses
$plan.availableFreeBytes = $startingFree
# Atomically reserve a new run directory. The identifier may be retried but
# existing profiles/evidence are never opened for writes or overwritten.
$runBase = Join-Path $resolvedExternalRoot 'runs'
[System.IO.Directory]::CreateDirectory($runBase) | Out-Null
[System.IO.Directory]::CreateDirectory($reportsDir) | Out-Null
$reserved = $false
for ($attempt = 0; $attempt -lt 16; $attempt++) {
    if ($attempt -gt 0) {
        $runId = 'qcs-million-' + [Guid]::NewGuid().ToString('N').Substring(0, 12)
        $runParent = Join-Path $runBase $runId
    }
    $benchmarkReport = Join-Path $reportsDir ($runId + '.benchmark.json')
    $monitorReport = Join-Path $reportsDir ($runId + '.monitor.json')
    $sampleLog = Join-Path $reportsDir ($runId + '.samples.jsonl')
    $stdoutLog = Join-Path $reportsDir ($runId + '.stdout.log')
    $stderrLog = Join-Path $reportsDir ($runId + '.stderr.log')
    $collisions = @($benchmarkReport, $monitorReport, $sampleLog, $stdoutLog, $stderrLog) |
        Where-Object { Test-Path -LiteralPath $_ }
    if ($collisions) { continue }
    try {
        New-Item -ItemType Directory -Path $runParent -ErrorAction Stop | Out-Null
        $reserved = $true
        break
    } catch {
        if (-not (Test-Path -LiteralPath $runParent)) { throw }
    }
}
if (-not $reserved) { throw 'Cannot reserve a unique million-vector run directory.' }
$arguments[1] = $runParent
$arguments[[Array]::IndexOf($arguments, '--report') + 1] = $benchmarkReport
foreach ($entry in @{runId=$runId; runParent=$runParent; arguments=$arguments; benchmarkReport=$benchmarkReport;
    monitorReport=$monitorReport; sampleLog=$sampleLog; stdoutLog=$stdoutLog; stderrLog=$stderrLog}.GetEnumerator()) {
    $plan[$entry.Key] = $entry.Value
}

$summary = [ordered]@{
    formatVersion = 1
    kind = 'qcopilots-search-million-independent-monitor'
    plan = $plan
    startedUtc = [DateTime]::UtcNow.ToString('o')
    completedUtc = $null
    passed = $false
    processId = $null
    processExitCode = $null
    guardViolation = $null
    failure = $null
    sampleCount = 0
    maximumObservedDirectoryBytes = [long]0
    minimumObservedFreeBytes = [long]::MaxValue
    maximumSampleGapMilliseconds = [double]0
    intervalsLongerThanTwiceRequested = 0
    maximumObservedWorkingSetBytes = [long]0
    maximumObservedCpuSeconds = [double]0
    measurementScope = '250 ms requested recursive file-size samples of the unique run parent. Samples are not a continuous peak guarantee. CPU and RSS describe the directly launched benchmark process. Existing pilot directories and unrelated allocations remain included in volume free space.'
    stdoutComplete = $false
    stderrComplete = $false
    artifactsRetained = $true
    gates = @()
}
Write-JsonFile $monitorReport $summary

$process = [System.Diagnostics.Process]::new()
$start = [System.Diagnostics.ProcessStartInfo]::new()
$start.FileName = $resolvedExe
$start.UseShellExecute = $false
$start.CreateNoWindow = $true
$start.RedirectStandardOutput = $true
$start.RedirectStandardError = $true
$start.WorkingDirectory = $runParent
$start.Environment['PATH'] = $resolvedQtBin + [System.IO.Path]::PathSeparator + [Environment]::GetEnvironmentVariable('PATH')
$start.Environment['QT_COMMAND_LINE_PARSER_NO_GUI_MESSAGE_BOXES'] = '1'
foreach ($argument in $arguments) { $start.ArgumentList.Add([string]$argument) }
$process.StartInfo = $start
$startedByThisInvocation = $false
$sampleWriter = $null
$stdoutTask = $null
$stderrTask = $null
$clock = [System.Diagnostics.Stopwatch]::StartNew()
$lastSampleMs = [double]0
$lastCpuSeconds = [double]0
$nextProgressMs = [double]0

try {
    $sampleWriter = [System.IO.StreamWriter]::new($sampleLog, $false, [System.Text.UTF8Encoding]::new($false))
    $sampleWriter.AutoFlush = $true
    if (-not $process.Start()) { throw 'The benchmark process could not be started' }
    $startedByThisInvocation = $true
    $summary.processId = $process.Id
    $stdoutTask = $process.StandardOutput.ReadToEndAsync()
    $stderrTask = $process.StandardError.ReadToEndAsync()
    Write-JsonFile $monitorReport $summary

    while ($true) {
        $iterationStartMs = $clock.Elapsed.TotalMilliseconds
        $bytes = Get-DirectoryBytes $runParent
        $free = $volume.AvailableFreeSpace
        $cpuSeconds = $null
        $workingSet = $null
        try {
            $process.Refresh()
            $cpuSeconds = $process.TotalProcessorTime.TotalSeconds
            $workingSet = $process.WorkingSet64
        } catch { }
        $nowMs = $clock.Elapsed.TotalMilliseconds
        $gapMs = if ($summary.sampleCount -eq 0) { 0.0 } else { $nowMs - $lastSampleMs }
        $cpuDelta = if ($null -ne $cpuSeconds) { [Math]::Max(0.0, $cpuSeconds - $lastCpuSeconds) } else { $null }
        $cpuPercent = if ($gapMs -gt 0 -and $null -ne $cpuDelta) { 100000.0 * $cpuDelta / $gapMs } else { $null }
        $sample = [ordered]@{
            utc = [DateTime]::UtcNow.ToString('o')
            elapsedMilliseconds = $nowMs
            intervalGapMilliseconds = $gapMs
            recursiveDirectoryBytes = $bytes
            volumeAvailableFreeBytes = $free
            processId = $summary.processId
            processCpuSeconds = $cpuSeconds
            processCpuDeltaSeconds = $cpuDelta
            processCpuPercentOfOneCore = $cpuPercent
            processWorkingSetBytes = $workingSet
        }
        $sampleWriter.WriteLine(($sample | ConvertTo-Json -Compress -Depth 4))
        $summary.sampleCount += 1
        $summary.maximumObservedDirectoryBytes = [Math]::Max($summary.maximumObservedDirectoryBytes, $bytes)
        $summary.minimumObservedFreeBytes = [Math]::Min($summary.minimumObservedFreeBytes, $free)
        $summary.maximumSampleGapMilliseconds = [Math]::Max($summary.maximumSampleGapMilliseconds, $gapMs)
        if ($gapMs -gt 2 * $sampleIntervalMs) { $summary.intervalsLongerThanTwiceRequested += 1 }
        if ($null -ne $workingSet) { $summary.maximumObservedWorkingSetBytes = [Math]::Max($summary.maximumObservedWorkingSetBytes, $workingSet) }
        if ($null -ne $cpuSeconds) {
            $summary.maximumObservedCpuSeconds = [Math]::Max($summary.maximumObservedCpuSeconds, $cpuSeconds)
            $lastCpuSeconds = $cpuSeconds
        }
        $lastSampleMs = $nowMs
        if ($bytes -ge $directoryGuard -or $free -le $freeGuard) {
            $summary.guardViolation = [ordered]@{
                utc = [DateTime]::UtcNow.ToString('o')
                reason = if ($bytes -ge $directoryGuard) { 'Directory reached the budget minus the 256 MiB safety buffer' } else { 'Available space reached the reserve plus the 128 MiB safety buffer' }
                directoryBytes = $bytes
                availableFreeBytes = $free
            }
            # The Process object is exclusively the process started above.
            # Never enumerate and terminate external processes by name or PID.
            if (-not $process.HasExited) {
                $process.Kill($true)
                [void]$process.WaitForExit(10000)
            }
            break
        }
        if ($nowMs -ge $nextProgressMs) {
            Write-Host ('Million monitor: {0:n0}s, run {1:n2} GiB, free {2:n2} GiB, RSS {3:n2} GiB, samples {4}' -f ($nowMs / 1000), ($bytes / $GiB), ($free / $GiB), ([double]$workingSet / $GiB), $summary.sampleCount)
            $nextProgressMs = $nowMs + 5000
            Write-JsonFile $monitorReport $summary
        }
        if ($process.HasExited) { break }
        $remainingMs = $sampleIntervalMs - ($clock.Elapsed.TotalMilliseconds - $iterationStartMs)
        if ($remainingMs -gt 0) { Start-Sleep -Milliseconds ([int][Math]::Ceiling($remainingMs)) }
    }
    if (-not $process.HasExited) { throw 'The owned benchmark process did not exit after termination' }
    $process.WaitForExit()
    $summary.processExitCode = $process.ExitCode
    # Close the sampling/exit race: the process may have completed its final
    # writes between the last loop sample and HasExited becoming true.
    $finalBytes = Get-DirectoryBytes $runParent
    $finalFree = $volume.AvailableFreeSpace
    $finalMs = $clock.Elapsed.TotalMilliseconds
    $finalGapMs = $finalMs - $lastSampleMs
    $finalCpuSeconds = $null
    try { $finalCpuSeconds = $process.TotalProcessorTime.TotalSeconds } catch { }
    $sampleWriter.WriteLine(([ordered]@{
        utc = [DateTime]::UtcNow.ToString('o')
        phase = 'after-process-exit'
        elapsedMilliseconds = $finalMs
        intervalGapMilliseconds = $finalGapMs
        recursiveDirectoryBytes = $finalBytes
        volumeAvailableFreeBytes = $finalFree
        processId = $summary.processId
        processExitCode = $summary.processExitCode
        processCpuSeconds = $finalCpuSeconds
        processWorkingSetBytes = $null
    } | ConvertTo-Json -Compress -Depth 4))
    $summary.sampleCount += 1
    $summary.maximumObservedDirectoryBytes = [Math]::Max($summary.maximumObservedDirectoryBytes, $finalBytes)
    $summary.minimumObservedFreeBytes = [Math]::Min($summary.minimumObservedFreeBytes, $finalFree)
    $summary.maximumSampleGapMilliseconds = [Math]::Max($summary.maximumSampleGapMilliseconds, $finalGapMs)
    if ($finalGapMs -gt 2 * $sampleIntervalMs) { $summary.intervalsLongerThanTwiceRequested += 1 }
    if ($null -ne $finalCpuSeconds) { $summary.maximumObservedCpuSeconds = [Math]::Max($summary.maximumObservedCpuSeconds, $finalCpuSeconds) }
    if (($finalBytes -ge $directoryGuard -or $finalFree -le $freeGuard) -and $null -eq $summary.guardViolation) {
        $summary.guardViolation = [ordered]@{
            utc = [DateTime]::UtcNow.ToString('o')
            reason = 'The mandatory post-exit sample exceeded a directory or free-space guard'
            directoryBytes = $finalBytes
            availableFreeBytes = $finalFree
        }
    }
} catch {
    $summary.failure = $_.Exception.Message
} finally {
    if ($startedByThisInvocation) {
        try {
            if (-not $process.HasExited) {
                $process.Kill($true)
                [void]$process.WaitForExit(10000)
                if ($null -eq $summary.failure) { $summary.failure = 'Monitor interrupted. Only its own benchmark process tree was terminated.' }
            }
            if ($process.HasExited) { $summary.processExitCode = $process.ExitCode }
        } catch {
            $summary.failure = 'Could not confirm termination of the owned process: ' + $_.Exception.Message
        }
    }
    if ($null -ne $sampleWriter) { $sampleWriter.Dispose() }
    foreach ($output in @(
        @{ task = $stdoutTask; file = $stdoutLog; key = 'stdoutComplete' },
        @{ task = $stderrTask; file = $stderrLog; key = 'stderrComplete' }
    )) {
        if ($null -eq $output.task) { continue }
        try {
            if ($output.task.Wait(5000)) {
                [System.IO.File]::WriteAllText($output.file, $output.task.Result, [System.Text.UTF8Encoding]::new($false))
                $summary[$output.key] = $true
            }
        } catch {
            $summary.failure = 'Output capture failed: ' + $_.Exception.Message
        }
    }
    $process.Dispose()
}

$gates = [System.Collections.Generic.List[object]]::new()
function Add-Gate([string] $Name, [bool] $Passed, $Actual) {
    $gates.Add([ordered]@{ name = $Name; passed = $Passed; actual = $Actual })
}

Add-Gate 'process-exit-zero' ($summary.processExitCode -ceq 0) $summary.processExitCode
Add-Gate 'monitor-completed-without-error' ($null -eq $summary.failure) $summary.failure
Add-Gate 'monitor-directory-and-free-space-budgets' ($summary.sampleCount -gt 0 -and $null -eq $summary.guardViolation -and $summary.maximumObservedDirectoryBytes -lt $directoryGuard -and $summary.minimumObservedFreeBytes -gt $freeGuard) $summary.guardViolation
Add-Gate 'stdout-and-stderr-captured' ($summary.stdoutComplete -and $summary.stderrComplete) @($summary.stdoutComplete, $summary.stderrComplete)

try {
    $result = Get-Content -LiteralPath $benchmarkReport -Raw | ConvertFrom-Json -Depth 30
    Add-Gate 'raw-benchmark-passed' (Test-True (Get-PropertyValue $result 'passed')) (Get-PropertyValue $result 'passed')
    foreach ($expected in @(
        @{ name = 'files'; value = 100000 }, @{ name = 'segmentsPerFile'; value = 10 },
        @{ name = 'segments'; value = 1000000 }, @{ name = 'dimensions'; value = 768 },
        @{ name = 'timedQueries'; value = 500 }, @{ name = 'warmupQueries'; value = 10 },
        @{ name = 'annEfSearch'; value = $expectedAnnEfSearch }
    )) {
        $actual = Get-PropertyValue $result $expected.name
        Add-Gate $expected.name (Test-Number $actual $expected.value $expected.value) $actual
    }
    $seedValue = Get-PropertyValue $result 'seed'
    Add-Gate 'seed' ($seedValue -is [string] -and $seedValue -ceq '20261011') $seedValue
    foreach ($stateName in @('builtStatus', 'reopenedStatus')) {
        $state = Get-PropertyValue $result $stateName
        foreach ($expected in @(
            @{ name = 'files'; value = 100000 }, @{ name = 'segments'; value = 1000000 },
            @{ name = 'vectors'; value = 1000000 }, @{ name = 'annVectors'; value = 1000000 },
            @{ name = 'annEfSearch'; value = $expectedAnnEfSearch }
        )) {
            $actual = Get-PropertyValue $state $expected.name
            Add-Gate ($stateName + '-' + $expected.name) (Test-Number $actual $expected.value $expected.value) $actual
        }
    }
    foreach ($requiredTrue in @('cancellationTokenEnabled', 'enforceFolderRules', 'keep', 'filtersPassed',
                               'concurrentRebuildEvaluated', 'recoveryFaultInjectionEvaluated',
                               'annManifestInterruptionEvaluated', 'publishedManifestOutboxInterruptionEvaluated')) {
        $actual = Get-PropertyValue $result $requiredTrue
        Add-Gate $requiredTrue (Test-True $actual) $actual
    }
    $oracleQueries = Get-PropertyArray $result 'queries'
    Add-Gate 'one-hundred-oracle-queries' ($oracleQueries.Count -eq 100) $oracleQueries.Count
    Add-Gate 'recall-at20-at-least-095' (Test-Number (Get-PropertyValue $result 'recallAt20') 0.95 1) (Get-PropertyValue $result 'recallAt20')
    $latency = Get-PropertyValue $result 'latencyMilliseconds'
    Add-Gate 'warm-p95-at-most-500ms' (Test-Number (Get-PropertyValue $latency 'p95') 0 500) (Get-PropertyValue $latency 'p95')
    $filterResults = Get-PropertyArray $result 'filters'
    Add-Gate 'three-filter-proportions' ($filterResults.Count -eq 3) $filterResults.Count
    foreach ($proportion in @(10.0, 1.0, 0.1)) {
        $matching = @($filterResults | Where-Object { Test-Number (Get-PropertyValue $_ 'nominalPercent') $proportion $proportion })
        $valid = $matching.Count -eq 1
        if ($valid) {
            $valid = (Test-True (Get-PropertyValue $matching[0] 'passed')) -and
                (Test-Number (Get-PropertyValue $matching[0] 'checkedQueries') 100 100) -and
                (Test-Number (Get-PropertyValue $matching[0] 'recallAt20') 0.95 1) -and
                (Test-Number (Get-PropertyValue $matching[0] 'p95Milliseconds') 0 500)
        }
        Add-Gate ("filter-{0}-percent" -f $proportion) $valid $matching
    }
    $background = Get-PropertyValue $result 'concurrentRebuild'
    Add-Gate 'single-shard-background-rebuild-p95-at-most-1000ms' ((Test-True (Get-PropertyValue $background 'passed')) -and
        (Test-True (Get-PropertyValue $background 'singleShard')) -and
        (Test-Number (Get-PropertyValue $background 'overlappingQueries') 1 500) -and
        (Test-Number (Get-PropertyValue $background 'p95Milliseconds') 0 1000)) $background
    $faults = Get-PropertyArray $result 'recoveryFaults'
    Add-Gate 'four-fault-results' ($faults.Count -eq 4) $faults.Count
    foreach ($boundary in @('committed-before-ann', 'uncommitted-sqlite', 'ann-file-saved-before-manifest', 'ann-manifest-published-before-outbox-cleanup')) {
        $matching = @($faults | Where-Object { (Get-PropertyValue $_ 'faultPoint') -ceq $boundary })
        $valid = $matching.Count -eq 1
        if ($valid) { $valid = (Test-True (Get-PropertyValue $matching[0] 'passed')) -and (Test-Number (Get-PropertyValue $matching[0] 'childExitCode') 73 73) }
        if ($valid -and $boundary -ceq 'ann-manifest-published-before-outbox-cleanup') {
            $valid = (Test-Number (Get-PropertyValue $matching[0] 'injectedSameShardEvents') 2048 2048) -and
                (Test-Number (Get-PropertyValue $matching[0] 'stalePublishedOutboxEvents') 0 0) -and
                (Test-True (Get-PropertyValue $matching[0] 'postRecoveryWritePassed'))
        }
        Add-Gate ('fault-' + $boundary) $valid $matching
    }
    $mutations = Get-PropertyValue $result 'mutations'
    Add-Gate 'crud-ten-thousand-vectors-per-phase' ((Test-True (Get-PropertyValue $mutations 'passed')) -and (Test-Number (Get-PropertyValue $mutations 'vectorsPerPhase') 10000 10000)) $mutations
    $concurrentContract = Test-ConcurrentRebuildContract $result
    Add-Gate 'concurrent-rebuild-observed-interval-contract' $concurrentContract.passed $concurrentContract
    $mutationContract = Test-MutationReportContract $result
    Add-Gate 'crud-query-and-retired-segment-contract' $mutationContract.passed $mutationContract
    $recoveryContract = Test-RecoveryReportContract $result
    Add-Gate 'recovery-version-vector-and-rollback-contract' $recoveryContract.passed $recoveryContract
    $profile = Get-PropertyValue $result 'profileDir'
    $insideRun = $profile -is [string] -and [System.IO.Path]::GetFullPath($profile).StartsWith($runParent + [System.IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)
    Add-Gate 'profile-belongs-to-this-unique-run' $insideRun $profile
    $allowedRoots = Get-PropertyArray $result 'allowedRoots'
    $allowedRootMatches = $insideRun -and $allowedRoots.Count -eq 1 -and $allowedRoots[0] -is [string]
    if ($allowedRootMatches) {
        $separators = [char[]]@([System.IO.Path]::DirectorySeparatorChar, [System.IO.Path]::AltDirectorySeparatorChar)
        $normalizedAllowed = [System.IO.Path]::GetFullPath($allowedRoots[0]).TrimEnd($separators)
        $normalizedProfile = [System.IO.Path]::GetFullPath($profile).TrimEnd($separators)
        $allowedRootMatches = [string]::Equals($normalizedAllowed, $normalizedProfile, [StringComparison]::OrdinalIgnoreCase)
    }
    Add-Gate 'one-allowed-root-equals-this-profile' $allowedRootMatches $allowedRoots
    $summary['benchmarkReportSha256'] = (Get-FileHash -LiteralPath $benchmarkReport -Algorithm SHA256).Hash.ToLowerInvariant()
} catch {
    Add-Gate 'benchmark-report-readable-and-complete' $false $_.Exception.Message
}

try {
    $finalBinaryHash = (Get-FileHash -LiteralPath $resolvedExe -Algorithm SHA256).Hash.ToLowerInvariant()
    Add-Gate 'executed-binary-unchanged' ($finalBinaryHash -ceq $binaryHash) $finalBinaryHash
} catch { Add-Gate 'executed-binary-unchanged' $false $_.Exception.Message }

try {
    $postCandidate = Get-CandidatePreflight
    $candidateUnchanged = ($candidate | ConvertTo-Json -Depth 30 -Compress) -ceq ($postCandidate | ConvertTo-Json -Depth 30 -Compress)
    Add-Gate 'candidate-build-and-runtime-unchanged' $candidateUnchanged $postCandidate
    $wrapperUnchanged = $plan.wrapperSha256 -ceq (Get-FileHash -LiteralPath $PSCommandPath -Algorithm SHA256).Hash.ToLowerInvariant()
    $preflightUnchanged = $plan.preflightSha256 -ceq (Get-FileHash -LiteralPath $preflightTool -Algorithm SHA256).Hash.ToLowerInvariant()
    Add-Gate 'acceptance-runner-unchanged' ($wrapperUnchanged -and $preflightUnchanged) @{ wrapper=$wrapperUnchanged; preflight=$preflightUnchanged }
} catch { Add-Gate 'candidate-build-and-runtime-unchanged' $false $_.Exception.Message }

$summary.gates = @($gates)
$summary['monitorQuality'] = [ordered]@{
    requestedIntervalMilliseconds = $sampleIntervalMs
    maximumObservedGapMilliseconds = $summary.maximumSampleGapMilliseconds
    recommendedMaximumGapMilliseconds = 1000
    observedGapsWithinRecommendation = $summary.maximumSampleGapMilliseconds -le 1000
    gapRecommendationIsAcceptanceGate = $false
}
$failedGates = @($gates | Where-Object { -not $_.passed })
$summary.passed = $failedGates.Count -eq 0
$summary.completedUtc = [DateTime]::UtcNow.ToString('o')
Write-JsonFile $monitorReport $summary
Write-Host "Benchmark report: $benchmarkReport"
Write-Host "Independent monitor report: $monitorReport"
Write-Host "Sample log: $sampleLog"
if ($summary.passed) { exit 0 }
$failedGates | ConvertTo-Json -Depth 8 | Write-Output
exit 2
