#requires -Version 7.0
[CmdletBinding()]
param([string] $GuardPath = (Join-Path $PSScriptRoot 'qcs-run-million.ps1'))

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$tokens = $null
$parseErrors = $null
$guardAst = [System.Management.Automation.Language.Parser]::ParseFile($GuardPath, [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count -ne 0) { throw ($parseErrors | Out-String) }

# Import only named pure functions, never the script body, preflight, or monitor.
$pureNames = @('Get-JsonProperty', 'Test-JsonNumber', 'Test-JsonArray', 'Test-True',
    'Assert-ReportContract', 'Test-CloseReportNumber', 'Test-ConcurrentRebuildContract',
    'Test-MutationReportContract', 'Test-RecoveryReportContract')
foreach ($helperName in $pureNames) {
    $helper = @($guardAst.FindAll({ param($node)
        $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -ceq $helperName
    }, $false))
    if ($helper.Count -ne 1) { throw "Expected exactly one pure helper $helperName" }
    . ([scriptblock]::Create($helper[0].Extent.Text))
}

# Check and execute only the new six gate-wiring statements on synthetic data.
$wiring = [System.Collections.Generic.List[object]]::new()
$assignmentNames = @('concurrentContract', 'mutationContract', 'recoveryContract')
$gateNames = @('concurrent-rebuild-observed-interval-contract', 'crud-query-and-retired-segment-contract', 'recovery-version-vector-and-rollback-contract')
foreach ($node in $guardAst.FindAll({ param($candidate)
    $candidate -is [System.Management.Automation.Language.AssignmentStatementAst] -or
    $candidate -is [System.Management.Automation.Language.PipelineAst]
}, $false)) {
    if ($node -is [System.Management.Automation.Language.AssignmentStatementAst] -and
        $node.Left -is [System.Management.Automation.Language.VariableExpressionAst] -and
        $node.Left.VariablePath.UserPath -cin $assignmentNames) { $wiring.Add($node) }
    if ($node -is [System.Management.Automation.Language.PipelineAst] -and $node.PipelineElements.Count -eq 1) {
        $command = $node.PipelineElements[0]
        if ($command -is [System.Management.Automation.Language.CommandAst] -and
            $command.GetCommandName() -ceq 'Add-Gate' -and $command.CommandElements.Count -eq 4 -and
            $command.CommandElements[1] -is [System.Management.Automation.Language.StringConstantExpressionAst] -and
            $command.CommandElements[1].Value -cin $gateNames) { $wiring.Add($node) }
    }
}
if ($wiring.Count -ne 6) { throw 'Expected exactly six isolated evidence gate statements' }
$gateCode = ($wiring | Sort-Object { $_.Extent.StartOffset } | ForEach-Object { $_.Extent.Text }) -join "`n"

$fixtureJson = [ordered]@{
    segmentsPerFile = 10
    concurrentRebuild = [ordered]@{
        passed = $true; singleShard = $true; timingClock = 'steady-clock-relative-nanoseconds'
        observedBuilds = 1; measuredQueries = 5; overlappingQueries = 3
        p50Milliseconds = 20; p95Milliseconds = 30; p99Milliseconds = 30
        buildIntervals = @(@{ startNanoseconds = 100000000; endNanoseconds = 4000000000 })
        queryIntervals = @(
            @{ startNanoseconds = 0; endNanoseconds = 100000000; durationMilliseconds = 100; overlapsBuild = $false }
            @{ startNanoseconds = 100000000; endNanoseconds = 110000000; durationMilliseconds = 10; overlapsBuild = $true }
            @{ startNanoseconds = 200000000; endNanoseconds = 220000000; durationMilliseconds = 20; overlapsBuild = $true }
            @{ startNanoseconds = 300000000; endNanoseconds = 330000000; durationMilliseconds = 30; overlapsBuild = $true }
            @{ startNanoseconds = 4000000000; endNanoseconds = 6000000000; durationMilliseconds = 2000; overlapsBuild = $false }
        )
    }
    mutations = [ordered]@{
        passed = $true; queryScope = 'filtered-exact-file-plus-global-production-search-sample'; filesPerPhase = 1000; vectorsPerPhase = 10000
        phases = @(
            @{ phase = 'add'; passed = $true; filesChecked = 1000; keywordQueries = 1000; vectorQueries = 1000; staleHitCount = 0; retiredSegmentIdsChecked = 0; retiredSegmentIdsRemaining = 0; globalSearchQueries = 16; globalSearchCheckedHits = 20; globalSearchStaleHitCount = 0; globalSearchVectorCount = 1010000; globalSearchBackend = 'ann' }
            @{ phase = 'update'; passed = $true; filesChecked = 1000; keywordQueries = 1000; vectorQueries = 1000; staleHitCount = 0; retiredSegmentIdsChecked = 10000; retiredSegmentIdsRemaining = 0; globalSearchQueries = 16; globalSearchCheckedHits = 20; globalSearchStaleHitCount = 0; globalSearchVectorCount = 1010000; globalSearchBackend = 'ann' }
            @{ phase = 'delete'; passed = $true; filesChecked = 1000; keywordQueries = 1000; vectorQueries = 1000; staleHitCount = 0; retiredSegmentIdsChecked = 10000; retiredSegmentIdsRemaining = 0; globalSearchQueries = 16; globalSearchCheckedHits = 20; globalSearchStaleHitCount = 0; globalSearchVectorCount = 1000000; globalSearchBackend = 'ann' }
        )
    }
    recoveryFaults = @(
        @{ faultPoint = 'committed-before-ann'; childExitCode = 73; passed = $true; expectedVersion = 'commit-v1'; observedVersion = 'commit-v1'; expectedToken = 'firstcommittedtoken'; observedToken = 'firstcommittedtoken'; expectedVectorDimension = 700; observedVectorDimension = 700; observedVectorCosine = 1.0; vectorMatched = $true; previousSegmentId = 0; observedSegmentId = 1000010; staleVersionHits = 0; oldKeywordHits = 0; uncommittedCorruptionRows = 0 }
        @{ faultPoint = 'uncommitted-sqlite'; childExitCode = 73; passed = $true; expectedVersion = 'commit-v1'; observedVersion = 'commit-v1'; expectedToken = 'firstcommittedtoken'; observedToken = 'firstcommittedtoken'; expectedVectorDimension = 700; observedVectorDimension = 700; observedVectorCosine = 1.0; vectorMatched = $true; previousSegmentId = 1000010; observedSegmentId = 1000010; staleVersionHits = 0; oldKeywordHits = 0; uncommittedCorruptionRows = 0 }
        @{ faultPoint = 'ann-file-saved-before-manifest'; childExitCode = 73; passed = $true; expectedVersion = 'file-v2'; observedVersion = 'file-v2'; expectedToken = 'annfiletoken'; observedToken = 'annfiletoken'; expectedVectorDimension = 702; observedVectorDimension = 702; observedVectorCosine = 1.0; vectorMatched = $true; previousSegmentId = 1000010; observedSegmentId = 1000011; staleVersionHits = 0; oldKeywordHits = 0; uncommittedCorruptionRows = 0 }
        @{ faultPoint = 'ann-manifest-published-before-outbox-cleanup'; childExitCode = 73; passed = $true; expectedVersion = 'outbox-v3'; observedVersion = 'outbox-v3'; expectedToken = 'outboxtoken'; observedToken = 'outboxtoken'; expectedVectorDimension = 703; observedVectorDimension = 703; observedVectorCosine = 1.0; vectorMatched = $true; previousSegmentId = 1000011; observedSegmentId = 1000012; staleVersionHits = 0; oldKeywordHits = 0; uncommittedCorruptionRows = 0; injectedSameShardEvents = 2048; stalePublishedOutboxEvents = 0; postRecoveryWritePassed = $true }
    )
} | ConvertTo-Json -Depth 30 -Compress

$script:checked = 0
$script:positiveCases = 0
function Invoke-SchemaCase([string] $Name, [string] $Validator, [scriptblock] $Mutator, [bool] $ShouldPass = $false) {
    $fixture = $fixtureJson | ConvertFrom-Json -Depth 30
    if ($null -ne $Mutator) { & $Mutator $fixture | Out-Null }
    $verdict = & $Validator $fixture
    if ($verdict -isnot [pscustomobject] -or $verdict.passed -isnot [bool] -or $verdict.passed -ne $ShouldPass) {
        throw "$Name returned unexpected verdict: $($verdict | ConvertTo-Json -Depth 8 -Compress)"
    }
    if (-not $ShouldPass -and ([string]::IsNullOrWhiteSpace($verdict.error))) { throw "$Name must explain its rejection" }
    $script:checked += 1
    if ($ShouldPass) { $script:positiveCases += 1 }
}

function Set-FixtureValue($Object, [string] $Path, $Value, [bool] $Remove = $false) {
    $parts = $Path.Split('.')
    $container = $Object
    for ($partIndex = 0; $partIndex -lt $parts.Count - 1; ++$partIndex) {
        if ($parts[$partIndex] -match '^\d+$') { $container = $container[[int]$parts[$partIndex]] }
        else { $container = $container.PSObject.Properties[$parts[$partIndex]].Value }
    }
    if ($Remove) { $container.PSObject.Properties.Remove($parts[-1]) }
    else { $container.PSObject.Properties[$parts[-1]].Value = $Value }
}

$concurrent = 'Test-ConcurrentRebuildContract'
$mutation = 'Test-MutationReportContract'
$recovery = 'Test-RecoveryReportContract'
foreach ($validator in @($concurrent, $mutation, $recovery)) { Invoke-SchemaCase "baseline-$validator" $validator $null $true }

$missingFields = @{
    $concurrent = @('concurrentRebuild', 'concurrentRebuild.passed', 'concurrentRebuild.singleShard', 'concurrentRebuild.timingClock', 'concurrentRebuild.observedBuilds', 'concurrentRebuild.measuredQueries', 'concurrentRebuild.overlappingQueries', 'concurrentRebuild.p50Milliseconds', 'concurrentRebuild.p95Milliseconds', 'concurrentRebuild.p99Milliseconds', 'concurrentRebuild.buildIntervals', 'concurrentRebuild.buildIntervals.0.startNanoseconds', 'concurrentRebuild.buildIntervals.0.endNanoseconds', 'concurrentRebuild.queryIntervals', 'concurrentRebuild.queryIntervals.0.startNanoseconds', 'concurrentRebuild.queryIntervals.0.endNanoseconds', 'concurrentRebuild.queryIntervals.0.durationMilliseconds', 'concurrentRebuild.queryIntervals.0.overlapsBuild')
    $mutation = @('segmentsPerFile', 'mutations', 'mutations.passed', 'mutations.queryScope', 'mutations.filesPerPhase', 'mutations.vectorsPerPhase', 'mutations.phases')
    $recovery = @('recoveryFaults')
}
foreach ($phaseIndex in 0..2) {
    foreach ($field in @('phase', 'passed', 'filesChecked', 'keywordQueries', 'vectorQueries', 'staleHitCount', 'retiredSegmentIdsChecked', 'retiredSegmentIdsRemaining', 'globalSearchQueries', 'globalSearchCheckedHits', 'globalSearchStaleHitCount', 'globalSearchVectorCount', 'globalSearchBackend')) {
        $missingFields[$mutation] += "mutations.phases.$phaseIndex.$field"
    }
}
foreach ($phaseIndex in 0..3) {
    foreach ($field in @('faultPoint', 'childExitCode', 'passed', 'expectedVersion', 'observedVersion', 'expectedToken', 'observedToken', 'expectedVectorDimension', 'observedVectorDimension', 'observedVectorCosine', 'vectorMatched', 'previousSegmentId', 'observedSegmentId', 'staleVersionHits', 'oldKeywordHits', 'uncommittedCorruptionRows')) {
        $missingFields[$recovery] += "recoveryFaults.$phaseIndex.$field"
    }
}
$missingFields[$recovery] += @('recoveryFaults.3.injectedSameShardEvents', 'recoveryFaults.3.stalePublishedOutboxEvents', 'recoveryFaults.3.postRecoveryWritePassed')
foreach ($validator in @($concurrent, $mutation, $recovery)) {
    foreach ($fieldPath in $missingFields[$validator]) {
        Invoke-SchemaCase "missing-$fieldPath" $validator { param($r) Set-FixtureValue $r $fieldPath $null $true }
        Invoke-SchemaCase "null-$fieldPath" $validator { param($r) Set-FixtureValue $r $fieldPath $null }
    }
}

# Scalar, nested-array, fractional, string, and boolean numeric values are invalid.
$numericFields = @{
    $concurrent = @('concurrentRebuild.observedBuilds', 'concurrentRebuild.measuredQueries', 'concurrentRebuild.overlappingQueries', 'concurrentRebuild.buildIntervals.0.startNanoseconds', 'concurrentRebuild.queryIntervals.1.startNanoseconds')
    $mutation = @('mutations.filesPerPhase', 'mutations.vectorsPerPhase', 'mutations.phases.1.filesChecked', 'mutations.phases.1.keywordQueries', 'mutations.phases.1.vectorQueries', 'mutations.phases.1.staleHitCount', 'mutations.phases.1.retiredSegmentIdsChecked', 'mutations.phases.1.retiredSegmentIdsRemaining', 'mutations.phases.1.globalSearchQueries', 'mutations.phases.1.globalSearchCheckedHits', 'mutations.phases.1.globalSearchStaleHitCount', 'mutations.phases.1.globalSearchVectorCount')
    $recovery = @('recoveryFaults.2.childExitCode', 'recoveryFaults.2.expectedVectorDimension', 'recoveryFaults.2.observedVectorDimension', 'recoveryFaults.2.previousSegmentId', 'recoveryFaults.2.observedSegmentId', 'recoveryFaults.2.staleVersionHits', 'recoveryFaults.2.oldKeywordHits', 'recoveryFaults.2.uncommittedCorruptionRows')
}
foreach ($validator in @($concurrent, $mutation, $recovery)) {
    foreach ($fieldPath in $numericFields[$validator]) {
        foreach ($invalidValue in @('0', $false, 0.5, 1e-50, -1e-50, [double]::NaN, [double]::PositiveInfinity)) {
            Invoke-SchemaCase "invalid-number-$fieldPath-$invalidValue" $validator { param($r) Set-FixtureValue $r $fieldPath $invalidValue }
        }
        Invoke-SchemaCase "array-number-$fieldPath" $validator { param($r)
            $parts = $fieldPath.Split('.')
            $value = $r
            foreach ($part in $parts) { $value = if ($part -match '^\d+$') { $value[[int]$part] } else { $value.PSObject.Properties[$part].Value } }
            Set-FixtureValue $r $fieldPath ([object[]]@($value))
        }
    }
}

foreach ($shape in @('scalar', 'empty', 'nested')) {
    foreach ($entry in @(@{ validator = $concurrent; path = 'concurrentRebuild.buildIntervals' }, @{ validator = $concurrent; path = 'concurrentRebuild.queryIntervals' }, @{ validator = $mutation; path = 'mutations.phases' }, @{ validator = $recovery; path = 'recoveryFaults' })) {
        Invoke-SchemaCase "array-shape-$($entry.path)-$shape" $entry.validator { param($r)
            $value = [pscustomobject]@{ invalid = $true }
            if ($shape -eq 'empty') { $value = [object[]]@() }
            if ($shape -eq 'nested') { $value = [object[]]@(,[object[]]@([pscustomobject]@{ invalid = $true })) }
            Set-FixtureValue $r $entry.path $value
        }
    }
}

Invoke-SchemaCase 'wrong-clock' $concurrent { param($r) $r.concurrentRebuild.timingClock = 'wall-clock' }
Invoke-SchemaCase 'two-builds' $concurrent { param($r) $r.concurrentRebuild.observedBuilds = 2; $r.concurrentRebuild.buildIntervals += $r.concurrentRebuild.buildIntervals[0] }
Invoke-SchemaCase 'no-build-duration' $concurrent { param($r) $r.concurrentRebuild.buildIntervals[0].endNanoseconds = 100000000 }
Invoke-SchemaCase 'backward-build' $concurrent { param($r) $r.concurrentRebuild.buildIntervals[0].endNanoseconds = 1 }
Invoke-SchemaCase 'negative-query-start' $concurrent { param($r) $r.concurrentRebuild.queryIntervals[0].startNanoseconds = -1 }
Invoke-SchemaCase 'negative-build-start' $concurrent { param($r) $r.concurrentRebuild.buildIntervals[0].startNanoseconds = -1 }
Invoke-SchemaCase 'no-query-duration' $concurrent { param($r) $r.concurrentRebuild.queryIntervals[0].endNanoseconds = 0 }
Invoke-SchemaCase 'duration-mismatch' $concurrent { param($r) $r.concurrentRebuild.queryIntervals[1].durationMilliseconds = 9.9 }
Invoke-SchemaCase 'fake-start-touch-overlap' $concurrent { param($r) $r.concurrentRebuild.queryIntervals[0].overlapsBuild = $true }
Invoke-SchemaCase 'fake-end-touch-overlap' $concurrent { param($r) $r.concurrentRebuild.queryIntervals[4].overlapsBuild = $true }
Invoke-SchemaCase 'hidden-overlap' $concurrent { param($r) $r.concurrentRebuild.queryIntervals[1].overlapsBuild = $false }
Invoke-SchemaCase 'string-overlap' $concurrent { param($r) $r.concurrentRebuild.queryIntervals[1].overlapsBuild = 'true' }
Invoke-SchemaCase 'wrong-measured-count' $concurrent { param($r) $r.concurrentRebuild.measuredQueries = 3 }
Invoke-SchemaCase 'wrong-overlap-count' $concurrent { param($r) $r.concurrentRebuild.overlappingQueries = 5 }
Invoke-SchemaCase 'wrong-p50' $concurrent { param($r) $r.concurrentRebuild.p50Milliseconds = 10 }
Invoke-SchemaCase 'wrong-p95' $concurrent { param($r) $r.concurrentRebuild.p95Milliseconds = 20 }
Invoke-SchemaCase 'wrong-p99' $concurrent { param($r) $r.concurrentRebuild.p99Milliseconds = 2000 }
Invoke-SchemaCase 'no-true-overlap' $concurrent { param($r)
    $r.concurrentRebuild.queryIntervals = @($r.concurrentRebuild.queryIntervals[0], $r.concurrentRebuild.queryIntervals[4])
    $r.concurrentRebuild.measuredQueries = 2; $r.concurrentRebuild.overlappingQueries = 0
}
Invoke-SchemaCase 'overlap-p95-too-slow' $concurrent { param($r)
    $r.concurrentRebuild.queryIntervals = @([pscustomobject]@{ startNanoseconds = 100000000; endNanoseconds = 1101000000; durationMilliseconds = 1001; overlapsBuild = $true })
    $r.concurrentRebuild.measuredQueries = 1; $r.concurrentRebuild.overlappingQueries = 1
    $r.concurrentRebuild.p50Milliseconds = 1001; $r.concurrentRebuild.p95Milliseconds = 1001; $r.concurrentRebuild.p99Milliseconds = 1001
}
Invoke-SchemaCase '501-queries' $concurrent { param($r)
    $r.concurrentRebuild.queryIntervals = @(1..501 | ForEach-Object { $r.concurrentRebuild.queryIntervals[1] })
    $r.concurrentRebuild.measuredQueries = 501; $r.concurrentRebuild.overlappingQueries = 501
}
Invoke-SchemaCase 'exact-1000ms-boundary' $concurrent { param($r)
    $r.concurrentRebuild.queryIntervals = @([pscustomobject]@{ startNanoseconds = 100000000; endNanoseconds = 1100000000; durationMilliseconds = 1000; overlapsBuild = $true })
    $r.concurrentRebuild.measuredQueries = 1; $r.concurrentRebuild.overlappingQueries = 1
    $r.concurrentRebuild.p50Milliseconds = 1000; $r.concurrentRebuild.p95Milliseconds = 1000; $r.concurrentRebuild.p99Milliseconds = 1000
} $true

Invoke-SchemaCase 'wrong-mutation-scope' $mutation { param($r) $r.mutations.queryScope = 'global-only' }
Invoke-SchemaCase 'wrong-file-vector-product' $mutation { param($r) $r.segmentsPerFile = 20 }
Invoke-SchemaCase 'mutation-phase-order' $mutation { param($r) $r.mutations.phases = @($r.mutations.phases[1], $r.mutations.phases[0], $r.mutations.phases[2]) }
foreach ($entry in @(@{ field = 'filesChecked'; value = 999 }, @{ field = 'keywordQueries'; value = 999 }, @{ field = 'vectorQueries'; value = 999 }, @{ field = 'staleHitCount'; value = 1 }, @{ field = 'retiredSegmentIdsChecked'; value = 9999 }, @{ field = 'retiredSegmentIdsRemaining'; value = 1 }, @{ field = 'globalSearchQueries'; value = 15 }, @{ field = 'globalSearchCheckedHits'; value = 0 }, @{ field = 'globalSearchStaleHitCount'; value = 1 })) {
    Invoke-SchemaCase "mutation-incomplete-$($entry.field)" $mutation { param($r) Set-FixtureValue $r ('mutations.phases.1.' + $entry.field) $entry.value }
}
Invoke-SchemaCase 'add-retired-not-zero' $mutation { param($r) $r.mutations.phases[0].retiredSegmentIdsChecked = 10000 }
foreach ($phaseIndex in 0..2) {
    Invoke-SchemaCase "incorrect-global-vector-count-$phaseIndex" $mutation { param($r) $r.mutations.phases[$phaseIndex].globalSearchVectorCount = 100 }
    Invoke-SchemaCase "incorrect-global-backend-$phaseIndex" $mutation { param($r) $r.mutations.phases[$phaseIndex].globalSearchBackend = 'exact-subset' }
    Invoke-SchemaCase "global-backend-is-array-$phaseIndex" $mutation { param($r) $r.mutations.phases[$phaseIndex].globalSearchBackend = @('ann') }
}
Invoke-SchemaCase 'add-count-for-delete-phase' $mutation { param($r) $r.mutations.phases[2].globalSearchVectorCount = 1010000 }

Invoke-SchemaCase 'fault-phase-order' $recovery { param($r) $r.recoveryFaults = @($r.recoveryFaults[0], $r.recoveryFaults[1], $r.recoveryFaults[3], $r.recoveryFaults[2]) }
Invoke-SchemaCase 'version-case-mismatch' $recovery { param($r) $r.recoveryFaults[0].observedVersion = 'COMMIT-V1' }
Invoke-SchemaCase 'token-case-mismatch' $recovery { param($r) $r.recoveryFaults[0].observedToken = 'FIRSTCOMMITTEDTOKEN' }
foreach ($stem in @('Version', 'Token')) {
    Invoke-SchemaCase "whitespace-$stem" $recovery { param($r) Set-FixtureValue $r "recoveryFaults.2.expected$stem" ' '; Set-FixtureValue $r "recoveryFaults.2.observed$stem" ' ' }
    Invoke-SchemaCase "nonadjacent-repeated-$stem" $recovery { param($r)
        $first = $r.recoveryFaults[0].PSObject.Properties[('expected' + $stem)].Value
        Set-FixtureValue $r "recoveryFaults.3.expected$stem" $first; Set-FixtureValue $r "recoveryFaults.3.observed$stem" $first
    }
    Invoke-SchemaCase "rollback-replaced-$stem" $recovery { param($r) Set-FixtureValue $r "recoveryFaults.1.expected$stem" 'newrollbackvalue'; Set-FixtureValue $r "recoveryFaults.1.observed$stem" 'newrollbackvalue' }
}
Invoke-SchemaCase 'first-previous-not-zero' $recovery { param($r) $r.recoveryFaults[0].previousSegmentId = 1 }
Invoke-SchemaCase 'broken-segment-chain' $recovery { param($r) $r.recoveryFaults[2].previousSegmentId = 1000000 }
Invoke-SchemaCase 'rollback-new-id' $recovery { param($r) $r.recoveryFaults[1].observedSegmentId = 1000011 }
Invoke-SchemaCase 'write-reuses-id' $recovery { param($r) $r.recoveryFaults[2].observedSegmentId = 1000010 }
Invoke-SchemaCase 'write-id-zero' $recovery { param($r) $r.recoveryFaults[0].observedSegmentId = 0 }
Invoke-SchemaCase 'wrong-vector-dimension' $recovery { param($r) $r.recoveryFaults[2].expectedVectorDimension = 700; $r.recoveryFaults[2].observedVectorDimension = 700 }
foreach ($value in @(.998999, 1.000002, [double]::BitDecrement(.999), [double]::BitIncrement(1.000001), [double]::NaN, [double]::PositiveInfinity, '1', $true, [object[]]@(1))) {
    Invoke-SchemaCase "invalid-cosine-$value" $recovery { param($r) $r.recoveryFaults[2].observedVectorCosine = $value }
}
foreach ($value in @(.999, 1.000001)) {
    Invoke-SchemaCase "valid-cosine-boundary-$value" $recovery { param($r) $r.recoveryFaults[2].observedVectorCosine = $value } $true
}
foreach ($fieldPath in @('concurrentRebuild.singleShard', 'concurrentRebuild.passed', 'mutations.passed', 'mutations.phases.0.passed', 'recoveryFaults.0.passed', 'recoveryFaults.0.vectorMatched', 'recoveryFaults.3.postRecoveryWritePassed')) {
    $validator = if ($fieldPath.StartsWith('concurrent')) { $concurrent } elseif ($fieldPath.StartsWith('mutations')) { $mutation } else { $recovery }
    foreach ($value in @($false, 'true', 1, [object[]]@($true))) {
        Invoke-SchemaCase "invalid-boolean-$fieldPath-$value" $validator { param($r) Set-FixtureValue $r $fieldPath $value }
    }
}
foreach ($entry in @(@{ field = 'staleVersionHits'; value = 1 }, @{ field = 'oldKeywordHits'; value = 1 }, @{ field = 'uncommittedCorruptionRows'; value = 1 }, @{ field = 'injectedSameShardEvents'; value = 2047 }, @{ field = 'stalePublishedOutboxEvents'; value = 1 })) {
    Invoke-SchemaCase "recovery-failed-$($entry.field)" $recovery { param($r) Set-FixtureValue $r ('recoveryFaults.3.' + $entry.field) $entry.value }
}

function Add-Gate([string] $Name, [bool] $Passed, $Actual) { $gates.Add([pscustomobject]@{ name = $Name; passed = $Passed; actual = $Actual }) }
foreach ($breakContract in @('', 'concurrentRebuild', 'mutations', 'recoveryFaults')) {
    $result = $fixtureJson | ConvertFrom-Json -Depth 30
    if ($breakContract) { $result.PSObject.Properties.Remove($breakContract) }
    $gates = [System.Collections.Generic.List[object]]::new()
    . ([scriptblock]::Create($gateCode))
    if ($gates.Count -ne 3) { throw 'Expected three evidence gates' }
    foreach ($gate in $gates) {
        $shouldFail = ($breakContract -ceq 'concurrentRebuild' -and $gate.name -ceq $gateNames[0]) -or
            ($breakContract -ceq 'mutations' -and $gate.name -ceq $gateNames[1]) -or
            ($breakContract -ceq 'recoveryFaults' -and $gate.name -ceq $gateNames[2])
        if ($gate.passed -eq $shouldFail) { throw "Evidence gate wiring failed for $breakContract/$($gate.name)" }
        $script:checked += 1
    }
}

[pscustomobject]@{
    passed = $true; guardAstParsed = $true; guardBodyExecuted = $false
    syntheticCasesChecked = $script:checked; positiveCases = $script:positiveCases
    pureFunctionsLoaded = $pureNames; gateWiringChecked = $gateNames
    guardSha256 = (Get-FileHash -LiteralPath $GuardPath -Algorithm SHA256).Hash.ToLowerInvariant()
} | ConvertTo-Json -Depth 5
