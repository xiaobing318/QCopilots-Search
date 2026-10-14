#requires -Version 7.0
[CmdletBinding()]
param([string] $GuardPath = (Join-Path $PSScriptRoot 'qcs-run-million.ps1'))

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$tokens = $null
$parseErrors = $null
$guardAst = [System.Management.Automation.Language.Parser]::ParseFile($GuardPath, [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count -ne 0) { throw ($parseErrors | Out-String) }
$helper = @($guardAst.FindAll({ param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
        $node.Name -ceq 'Get-HeavyProcessState'
}, $false))
if ($helper.Count -ne 1) { throw 'Expected exactly one read-only process detector' }
if ($helper[0].Extent.Text -match '\b(?:Stop-Process|Start-Process)\b|\.Kill\s*\(') {
    throw 'The launch detector must not start or terminate processes'
}

# Only import the detector. Mock every external operation before calling it.
# No guard body, process enumeration, sleeping, benchmark or cleanup is run.
. ([scriptblock]::Create($helper[0].Extent.Text))
$script:mockProcesses = @()
$script:laterProcess = $null
$script:sleepCalls = 0
function Get-Process {
    [CmdletBinding()]
    param([int] $Id)
    if ($PSBoundParameters.ContainsKey('Id')) { return $script:laterProcess }
    return $script:mockProcesses
}
function Start-Sleep {
    param([int] $Milliseconds)
    if ($Milliseconds -ne 1000) { throw 'Unexpected process-sampling interval' }
    $script:sleepCalls += 1
}
function ProcessFixture([string] $Name, [int] $Identifier, $CpuSeconds) {
    $cpu = if ($null -eq $CpuSeconds) { $null } else { [TimeSpan]::FromSeconds($CpuSeconds) }
    return [pscustomobject]@{ ProcessName = $Name; Id = $Identifier;
        Path = (Join-Path ([IO.Path]::GetTempPath()) ('qcs-synthetic-' + $Name + '.exe')); TotalProcessorTime = $cpu }
}

$results = [System.Collections.Generic.List[object]]::new()
$blockingNames = @('cmake', 'ctest', 'ninja', 'cl', 'link', 'qcs-gui',
    'qcs-service', 'qcs-worker', 'qcs-cli',
    'qcs-benchmark', 'qcs-tests-file-capacity',
    'qcs-tests-reference-vectors', 'qcs-tests-semantic',
    'qcs-tests-gui', 'qcs-custom-component',
    'QCS-SERVICE', 'CTEST', 'other-semantic-job', 'other-benchmark-job')
$ignoredNames = @('Code', 'chrome', 'pwsh', 'python', 'explorer', 'qcser')
$identifier = 500
foreach ($name in @($blockingNames + $ignoredNames)) {
    $identifier += 1
    $script:mockProcesses = @(ProcessFixture $name $identifier 0)
    $script:sleepCalls = 0
    $observed = @(Get-HeavyProcessState)
    $expected = $blockingNames -contains $name
    if ($observed.Count -ne [int]$expected) { throw "Wrong process classification: $name" }
    if ($expected -and ($observed[0].id -ne $identifier -or $observed[0].name -cne $name -or
                       $observed[0].blocking -ne $true -or -not $observed[0].reason)) {
        throw "Blocking process identity or explanation missing: $name"
    }
    if ($script:sleepCalls -ne 0) { throw 'Non-MSBuild process incurred a CPU sampling delay' }
    $results.Add([pscustomobject]@{ name = $name; blockingExpected = $expected; passed = $true })
}
foreach ($case in @(
    @{ name = 'idle-msbuild'; initial = 12; later = 12; exists = $true; blocking = $false },
    @{ name = 'active-msbuild'; initial = 12; later = 13; exists = $true; blocking = $true },
    @{ name = 'exited-msbuild'; initial = 12; later = 12; exists = $false; blocking = $false },
    @{ name = 'unavailable-msbuild-cpu'; initial = $null; later = 12; exists = $true; blocking = $true }
)) {
    $script:mockProcesses = @(ProcessFixture 'MSBuild' 902 $case.initial)
    $script:laterProcess = if ($case.exists) { ProcessFixture 'MSBuild' 902 $case.later } else { $null }
    $script:sleepCalls = 0
    $observed = @(Get-HeavyProcessState)
    if ($observed.Count -ne 1 -or $observed[0].id -ne 902 -or
        $observed[0].blocking -ne $case.blocking -or $script:sleepCalls -ne 1) {
        throw "MSBuild sampling behavior changed: $($case.name)"
    }
    $results.Add([pscustomobject]@{ name = $case.name; blockingExpected = $case.blocking; passed = $true })
}

[pscustomobject]@{
    passed = $true
    guardAstParsed = $true
    guardBodyExecuted = $false
    realProcessEnumerationExecuted = $false
    processCreationExecuted = $false
    processTerminationExecuted = $false
    guardSha256 = (Get-FileHash -LiteralPath $GuardPath -Algorithm SHA256).Hash.ToLowerInvariant()
    caseCount = $results.Count
    cases = @($results)
} | ConvertTo-Json -Depth 6
