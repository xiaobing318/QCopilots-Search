#requires -Version 7.0
[CmdletBinding()]
param([string] $GuardPath = (Join-Path $PSScriptRoot 'qcs-run-million.ps1'))

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$tokens = $null
$parseErrors = $null
$guardAst = [Management.Automation.Language.Parser]::ParseFile($GuardPath, [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count) { throw ($parseErrors | Out-String) }
$helper = @($guardAst.FindAll({ param($node)
    $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -ceq 'Resolve-ReportRoot'
}, $false))
if ($helper.Count -ne 1) { throw 'Expected exactly one Resolve-ReportRoot helper' }
if ($helper[0].Extent.Text -match '\b(?:New-Item|Remove-Item|Set-Content|Start-Process|Stop-Process)\b|\.CreateDirectory\s*\(') {
    throw 'Report root validation must remain read-only'
}
. ([scriptblock]::Create($helper[0].Extent.Text))
$rootAssignments = @($guardAst.FindAll({ param($node)
    $node -is [Management.Automation.Language.AssignmentStatementAst] -and
        $node.Left -is [Management.Automation.Language.VariableExpressionAst] -and
        $node.Left.VariablePath.UserPath -ceq 'reportsDir'
}, $false))
if ($rootAssignments.Count -ne 1 -or $rootAssignments[0].Right.Extent.Text -cne 'Resolve-ReportRoot $resolvedExternalRoot $ReportRoot') {
    throw 'All report names must originate from the single validated selected report root'
}

$testParent = [Environment]::GetEnvironmentVariable('QCOPILOTS_SEARCH_TEST_ROOT', 'Process')
if ([string]::IsNullOrWhiteSpace($testParent)) { $testParent = [IO.Path]::GetTempPath() }
$testParent = [IO.Path]::GetFullPath($testParent)
if (-not [IO.Directory]::Exists($testParent)) { throw 'The configured temporary root must already exist' }
$ownedRoot = $null
$links = [Collections.Generic.List[string]]::new()
$results = [Collections.Generic.List[object]]::new()

function Assert-RootCase([string] $Name, [string] $External, [string] $Selected, [string] $Expected, [string] $Reject = '') {
    $observed = $null
    $failure = $null
    try { $observed = Resolve-ReportRoot $External $Selected } catch { $failure = $_.Exception.Message }
    if ($Reject) {
        if (-not $failure -or $failure -notlike ('*' + $Reject + '*')) {
            throw "$Name did not reject the invalid root as expected. Actual result: $observed / $failure"
        }
    } elseif ($failure -or $observed -cne $Expected) {
        throw "$Name did not resolve the expected root. Actual result: $observed / $failure"
    }
    $results.Add([pscustomobject]@{ name = $Name; passed = $true; rejected = [bool]$Reject })
}

try {
    for ($attempt = 0; $attempt -lt 16; $attempt++) {
        $candidate = Join-Path $testParent ('qcs-report-test-' + [Guid]::NewGuid().ToString('N').Substring(0, 12))
        try {
            New-Item -ItemType Directory -Path $candidate -ErrorAction Stop | Out-Null
            $ownedRoot = $candidate
            break
        } catch {
            if (-not (Test-Path -LiteralPath $candidate)) { throw }
        }
    }
    if (-not $ownedRoot) { throw 'Could not reserve a unique test directory' }
    $external = Join-Path $ownedRoot 'build with space'
    $boundary = Join-Path $external 'reports'
    [IO.Directory]::CreateDirectory($external) | Out-Null
    Assert-RootCase 'default-missing-is-read-only' $external '' $boundary
    if (Test-Path -LiteralPath $boundary) { throw 'Read-only default validation created a report directory' }
    Assert-RootCase 'explicit-missing-reports' $external $boundary '' 'must already exist'
    [IO.Directory]::CreateDirectory($boundary) | Out-Null
    Assert-RootCase 'default-existing-reports' $external '' $boundary
    Assert-RootCase 'explicit-reports-boundary' $external $boundary $boundary
    $inside = Join-Path $boundary '本轮 evidence'
    [IO.Directory]::CreateDirectory($inside) | Out-Null
    Assert-RootCase 'inside-chinese-space' $external $inside $inside
    Assert-RootCase 'canonical-inside' $external (Join-Path $inside '../本轮 evidence/') $inside
    Assert-RootCase 'missing-descendant' $external (Join-Path $inside 'missing') '' 'must already exist'
    Assert-RootCase 'external-root-rejected' $external $external '' 'inside ExternalRoot/reports'
    Assert-RootCase 'filesystem-root-rejected' $external ([IO.Path]::GetPathRoot($external)) '' 'inside ExternalRoot/reports'
    $sibling = Join-Path $external 'reports-other'
    [IO.Directory]::CreateDirectory($sibling) | Out-Null
    Assert-RootCase 'prefix-sibling-rejected' $external $sibling '' 'inside ExternalRoot/reports'
    Assert-RootCase 'parent-traversal-rejected' $external (Join-Path $boundary '../reports-other') '' 'inside ExternalRoot/reports'
    $source = Join-Path $ownedRoot 'source'
    [IO.Directory]::CreateDirectory((Join-Path $source 'src')) | Out-Null
    [IO.File]::WriteAllText((Join-Path $source 'CMakeLists.txt'), 'project(unit_fixture)')
    Assert-RootCase 'source-root-rejected' $external $source '' 'inside ExternalRoot/reports'
    Assert-RootCase 'missing-external-root' (Join-Path $ownedRoot 'absent') '' '' 'ExternalRoot must already exist'
    $file = Join-Path $boundary 'file.json'
    [IO.File]::WriteAllText($file, '{}')
    Assert-RootCase 'file-rejected' $external $file '' 'directory'
    $target = Join-Path $ownedRoot 'redirect target'
    $targetLeaf = Join-Path $target 'leaf'
    [IO.Directory]::CreateDirectory($targetLeaf) | Out-Null
    $link = Join-Path $boundary 'redirect'
    $type = if ([OperatingSystem]::IsWindows()) { 'Junction' } else { 'SymbolicLink' }
    New-Item -ItemType $type -Path $link -Target $target -ErrorAction Stop | Out-Null
    $links.Add($link)
    Assert-RootCase 'redirected-root-rejected' $external $link '' 'reparse point'
    Assert-RootCase 'redirected-ancestor-rejected' $external (Join-Path $link 'leaf') '' 'reparse point'
    if (Test-Path -LiteralPath (Join-Path $external 'runs')) { throw 'Pure root validation created a profile container' }
} finally {
    # Remove links themselves first. Never recursively traverse a redirect during cleanup.
    foreach ($link in $links) {
        $item = Get-Item -LiteralPath $link -Force -ErrorAction Stop
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -eq 0) { throw 'Test link changed identity before cleanup' }
        Remove-Item -LiteralPath $link -Force -ErrorAction Stop
    }
    if ($ownedRoot) {
        $full = [IO.Path]::GetFullPath($ownedRoot)
        $parent = [IO.Path]::GetFullPath($testParent).TrimEnd([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar)
        if (-not $full.StartsWith($parent + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase) -or
            [IO.Path]::GetFileName($full) -notmatch '^qcs-report-test-[0-9a-f]{12}$') { throw 'Cleanup target is outside this test run' }
        $item = Get-Item -LiteralPath $full -Force -ErrorAction Stop
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'Test root changed identity before cleanup' }
        Remove-Item -LiteralPath $full -Recurse -Force -ErrorAction Stop
        if (Test-Path -LiteralPath $full) { throw 'Test root cleanup failed' }
    }
}

[pscustomobject]@{
    passed = $true
    guardAstParsed = $true
    guardBodyExecuted = $false
    benchmarkExecuted = $false
    realCandidateProvenanceExecuted = $false
    reportRootValidationReadOnly = $true
    cleanupVerified = $true
    caseCount = $results.Count
    cases = @($results)
    guardSha256 = (Get-FileHash -LiteralPath $GuardPath -Algorithm SHA256).Hash.ToLowerInvariant()
} | ConvertTo-Json -Depth 6
