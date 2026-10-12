#requires -Version 7.0
[CmdletBinding()]
param(
    [string] $GuardPath = (Join-Path $PSScriptRoot 'qcs-run-million.ps1')
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# Parse the guard and load only this pure helper. Never invoke the guard body,
# Validate mode, process preflight, benchmark, filesystem monitoring, or cleanup.
$tokens = $null
$parseErrors = $null
$guardAst = [System.Management.Automation.Language.Parser]::ParseFile(
    $GuardPath, [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count -ne 0) { throw ($parseErrors | Out-String) }
$helperAst = @($guardAst.FindAll({
    param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
        $node.Name -ceq 'Get-PropertyArray'
}, $false))
if ($helperAst.Count -ne 1) { throw 'Expected exactly one Get-PropertyArray helper' }
. ([scriptblock]::Create($helperAst[0].Extent.Text))

$singleObject = [pscustomobject]@{ id = 17 }
$multiple = [object[]]@('first', $singleObject, 23)
$nested = [object[]]@(,([object[]]@('nested-a', 'nested-b')))
$cases = @(
    @{ name = 'null-object'; object = $null; expected = [object[]]@() }
    @{ name = 'missing-property'; object = [pscustomobject]@{}; expected = [object[]]@() }
    @{ name = 'null-property'; object = [pscustomobject]@{ items = $null }; expected = [object[]]@() }
    @{ name = 'empty-array'; object = [pscustomobject]@{ items = [object[]]@() }; expected = [object[]]@() }
    @{ name = 'scalar-string'; object = [pscustomobject]@{ items = 'one' }; expected = [object[]]@('one') }
    @{ name = 'singleton-string-array'; object = [pscustomobject]@{ items = [object[]]@('one') }; expected = [object[]]@('one') }
    @{ name = 'singleton-object-array'; object = [pscustomobject]@{ items = [object[]]@($singleObject) }; expected = [object[]]@($singleObject) }
    @{ name = 'multiple-array'; object = [pscustomobject]@{ items = $multiple }; expected = $multiple }
    @{ name = 'array-containing-null'; object = [pscustomobject]@{ items = [object[]]@($null) }; expected = [object[]]@($null) }
    @{ name = 'nested-array'; object = [pscustomobject]@{ items = $nested }; expected = $nested }
    @{ name = 'json-empty'; object = ('{"items":[]}' | ConvertFrom-Json); expected = [object[]]@() }
    @{ name = 'json-singleton'; object = ('{"items":["one"]}' | ConvertFrom-Json); expected = [object[]]@('one') }
    @{ name = 'json-multiple'; object = ('{"items":["one","two"]}' | ConvertFrom-Json); expected = [object[]]@('one', 'two') }
)

$results = [System.Collections.Generic.List[object]]::new()
foreach ($case in $cases) {
    $actual = Get-PropertyArray $case.object 'items'
    if ($actual -isnot [object[]]) { throw "$($case.name): assignment did not retain object[]" }
    if ($actual.Count -ne $case.expected.Count) { throw "$($case.name): wrong assignment count $($actual.Count)" }
    for ($index = 0; $index -lt $actual.Count; $index += 1) {
        $expectedItem = $case.expected[$index]
        $actualItem = $actual[$index]
        if ($expectedItem -is [pscustomobject] -or $expectedItem -is [array]) {
            if (-not [object]::ReferenceEquals($expectedItem, $actualItem)) {
                throw "$($case.name): array element reference changed"
            }
        } elseif (-not [object]::Equals($expectedItem, $actualItem)) {
            throw "$($case.name): array element value changed"
        }
    }
    $pipelineItems = @(Get-PropertyArray $case.object 'items')
    if ($pipelineItems.Count -ne 1 -or $pipelineItems[0] -isnot [object[]] -or
        $pipelineItems[0].Count -ne $case.expected.Count) {
        throw "$($case.name): helper did not emit one intact array to the pipeline"
    }
    $results.Add([pscustomobject]@{
        name = $case.name
        passed = $true
        assignmentType = $actual.GetType().FullName
        count = $actual.Count
        pipelineObjectCount = $pipelineItems.Count
    })
}

# Exercise each actual report field, including the absent recoveryFaults case.
foreach ($field in @('queries', 'filters', 'recoveryFaults', 'allowedRoots')) {
    $missing = Get-PropertyArray ([pscustomobject]@{}) $field
    $one = Get-PropertyArray ([pscustomobject]@{ $field = [object[]]@('one') }) $field
    if ($missing.Count -ne 0 -or $one.Count -ne 1) { throw "$field consumer normalization failed" }
}

[pscustomobject]@{
    passed = $true
    guardAstParsed = $true
    guardBodyExecuted = $false
    guardSha256 = (Get-FileHash -LiteralPath $GuardPath -Algorithm SHA256).Hash.ToLowerInvariant()
    caseCount = $results.Count
    cases = @($results)
    reportFieldsChecked = @('queries', 'filters', 'recoveryFaults', 'allowedRoots')
} | ConvertTo-Json -Depth 6
