#requires -Version 7.0
[CmdletBinding()]
param(
    [string] $GuardPath = (Join-Path $PSScriptRoot 'qcs-run-million.ps1')
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$tokens = $null
$parseErrors = $null
$guardAst = [System.Management.Automation.Language.Parser]::ParseFile(
    $GuardPath, [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count -ne 0) { throw ($parseErrors | Out-String) }

# Load only pure numeric/property helpers and the two numeric gate loops.
# No guard body, Validate, preflight, process, monitor, or report write runs here.
foreach ($helperName in @('Get-PropertyValue', 'Test-Number')) {
    $helper = @($guardAst.FindAll({
        param($node)
        $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
            $node.Name -ceq $helperName
    }, $false))
    if ($helper.Count -ne 1) { throw "Expected one $helperName helper" }
    . ([scriptblock]::Create($helper[0].Extent.Text))
}
$numericLoops = @($guardAst.FindAll({
    param($node)
    $node -is [System.Management.Automation.Language.ForEachStatementAst] -and
        $node.Variable.VariablePath.UserPath -ceq 'expected'
}, $false))
$statusLoop = @($guardAst.FindAll({
    param($node)
    $node -is [System.Management.Automation.Language.ForEachStatementAst] -and
        $node.Variable.VariablePath.UserPath -ceq 'stateName'
}, $false))
if ($numericLoops.Count -ne 2 -or $statusLoop.Count -ne 1) { throw 'Unexpected numeric gate structure' }
$constant = @($guardAst.FindAll({
    param($node)
    $node -is [System.Management.Automation.Language.AssignmentStatementAst] -and
        $node.Left -is [System.Management.Automation.Language.VariableExpressionAst] -and
        $node.Left.VariablePath.UserPath -ceq 'expectedAnnEfSearch'
}, $false))
if ($constant.Count -ne 1 -or $constant[0].Right.Extent.Text -cne '4096') {
    throw 'Expected a single numeric annEfSearch constant of 4096'
}
$expectedAnnEfSearch = 4096
$planFields = @($guardAst.FindAll({
    param($node)
    $node -is [System.Management.Automation.Language.HashtableAst]
}, $false) | ForEach-Object { $_.KeyValuePairs } | Where-Object {
    $_.Item1.Extent.Text -ceq 'expectedAnnEfSearch' -and
        $_.Item2.Extent.Text -ceq '$expectedAnnEfSearch'
})
if ($planFields.Count -ne 1) { throw 'Plan must record the expected annEfSearch' }

function Add-Gate([string] $Name, [bool] $Passed, $Actual) {
    $gates.Add([pscustomobject]@{ name = $Name; passed = $Passed; actual = $Actual })
}
$cases = @(
    @{ name = 'integer'; value = 4096; expected = $true }
    @{ name = 'floating-point'; value = 4096.0; expected = $true }
    @{ name = 'old-default'; value = 512; expected = $false }
    @{ name = 'below'; value = 4095; expected = $false }
    @{ name = 'above'; value = 4097; expected = $false }
    @{ name = 'string'; value = '4096'; expected = $false }
    @{ name = 'null'; value = $null; expected = $false }
    @{ name = 'boolean'; value = $true; expected = $false }
    @{ name = 'missing'; value = $null; expected = $false }
)
$checked = 0
foreach ($scope in @('raw', 'builtStatus', 'reopenedStatus')) {
    foreach ($case in $cases) {
        $result = [pscustomobject]@{
            files = 100000; segmentsPerFile = 10; segments = 1000000
            dimensions = 768; timedQueries = 500; warmupQueries = 10; annEfSearch = 4096
            builtStatus = [pscustomobject]@{ files = 100000; segments = 1000000; vectors = 1000000; annVectors = 1000000; annEfSearch = 4096 }
            reopenedStatus = [pscustomobject]@{ files = 100000; segments = 1000000; vectors = 1000000; annVectors = 1000000; annEfSearch = 4096 }
        }
        $target = if ($scope -ceq 'raw') { $result } else { $result.$scope }
        if ($case.name -ceq 'missing') { $target.PSObject.Properties.Remove('annEfSearch') }
        else { $target.annEfSearch = $case.value }
        $gates = [System.Collections.Generic.List[object]]::new()
        . ([scriptblock]::Create($numericLoops[0].Extent.Text))
        . ([scriptblock]::Create($statusLoop[0].Extent.Text))
        $annGates = @($gates | Where-Object { $_.name -match '(^|-)annEfSearch$' })
        if ($annGates.Count -ne 3) { throw 'Expected three annEfSearch gates' }
        $targetName = if ($scope -ceq 'raw') { 'annEfSearch' } else { $scope + '-annEfSearch' }
        foreach ($gate in $annGates) {
            $expectedPass = if ($gate.name -ceq $targetName) { $case.expected } else { $true }
            if ($gate.passed -ne $expectedPass) { throw "$scope/$($case.name): unexpected $($gate.name) result" }
        }
        $checked += 1
    }
}
[pscustomobject]@{
    passed = $true
    guardAstParsed = $true
    guardBodyExecuted = $false
    expectedAnnEfSearch = $expectedAnnEfSearch
    casesChecked = $checked
    numericGatesChecked = @('annEfSearch', 'builtStatus-annEfSearch', 'reopenedStatus-annEfSearch')
    planFieldChecked = 'expectedAnnEfSearch'
    guardSha256 = (Get-FileHash -LiteralPath $GuardPath -Algorithm SHA256).Hash.ToLowerInvariant()
} | ConvertTo-Json -Depth 5
