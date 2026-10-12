param(
  [Parameter(Mandatory=$true)][string]$BuildRoot,
  [Parameter(Mandatory=$true)][string]$QtRoot,
  [Parameter(Mandatory=$true)][string]$QtPrefix,
  [Parameter(Mandatory=$true)][string]$BuildRecord,
  [Parameter(Mandatory=$true)][string]$BuildRecordSha256,
  [Parameter(Mandatory=$true)][string]$CandidateHead,
  [Parameter(Mandatory=$true)][string]$CandidateTree,
  [Parameter(Mandatory=$true)][string]$SemanticReport,
  [Parameter(Mandatory=$true)][string]$SemanticSha256,
  [Parameter(Mandatory=$true)][string]$CTestSummary,
  [Parameter(Mandatory=$true)][string]$CTestExecutionRecord,
  [Parameter(Mandatory=$true)][string]$OutputRoot,
  [string]$Source,
  [string]$Python='python',
  [string]$ModelDirectory,
  [string]$CMake='cmake',
  [ValidateSet('Release','Debug','RelWithDebInfo','MinSizeRel')][string]$Config='Release',
  [double]$ReserveGiB=3.0,
  [double]$PeakAdditionalGiB=4.0,
  [double]$RetainedAdditionalGiB=0.8,
  [double]$PostRunReserveGiB=3.0,
  [switch]$Execute
)
$ErrorActionPreference='Stop'
if (-not $Source) { $Source=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../..')) }
if (-not $ModelDirectory) { $ModelDirectory=Join-Path $BuildRoot 'models' }
if (-not $PSBoundParameters.ContainsKey('PostRunReserveGiB')) { $PostRunReserveGiB=$ReserveGiB }
$env:PYTHONUTF8='1'
$env:PYTHONIOENCODING='utf-8'
$taskCulture=[Globalization.CultureInfo]::InvariantCulture
$taskArguments=@('-B', (Join-Path $PSScriptRoot 'qcs-package-phase1.py'),
  '--source', $Source, '--build-root', $BuildRoot, '--qt-root', $QtRoot,
  '--qt-prefix', $QtPrefix, '--model-dir', $ModelDirectory,
  '--build-record', $BuildRecord, '--build-record-sha256', $BuildRecordSha256,
  '--defer-source-bundle', '--head', $CandidateHead, '--tree', $CandidateTree,
  '--semantic-report', $SemanticReport, '--semantic-sha256', $SemanticSha256,
  '--ctest-summary', $CTestSummary, '--ctest-execution-record', $CTestExecutionRecord,
  '--output-root', $OutputRoot, '--cmake', $CMake, '--config', $Config,
  '--reserve-gib', $ReserveGiB.ToString($taskCulture),
  '--peak-additional-gib', $PeakAdditionalGiB.ToString($taskCulture),
  '--retained-additional-gib', $RetainedAdditionalGiB.ToString($taskCulture),
  '--post-run-reserve-gib', $PostRunReserveGiB.ToString($taskCulture))
if ($Execute) { $taskArguments += '--execute' }
& $Python @taskArguments
exit $LASTEXITCODE
