param(
  [Parameter(Mandatory=$true)][string]$Phase1Summary,
  [Parameter(Mandatory=$true)][string]$Phase1SummarySha256,
  [string]$Python='python',
  [double]$ReserveGiB=3.0,
  [double]$PeakAdditionalGiB=1.2,
  [switch]$Execute
)
$ErrorActionPreference='Stop'
$env:PYTHONUTF8='1'
$env:PYTHONIOENCODING='utf-8'
$taskCulture=[Globalization.CultureInfo]::InvariantCulture
$taskArguments=@('-B', (Join-Path $PSScriptRoot 'qcs-package-phase2.py'),
  '--phase1-summary', $Phase1Summary, '--phase1-summary-sha256', $Phase1SummarySha256,
  '--reserve-gib', $ReserveGiB.ToString($taskCulture),
  '--peak-additional-gib', $PeakAdditionalGiB.ToString($taskCulture))
if ($Execute) { $taskArguments += '--execute' }
& $Python @taskArguments
exit $LASTEXITCODE
