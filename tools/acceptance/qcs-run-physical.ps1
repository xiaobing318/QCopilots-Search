#requires -Version 7.0
[CmdletBinding()]
param(
    [switch]$Execute,
    [string]$ExpectedCommit = '',
    [string]$BuildRecord = '',
    [string]$Source = (Join-Path $PSScriptRoot '../..'),
    [Parameter(Mandatory = $true)][string]$BuildRoot,
    [Parameter(Mandatory = $true)][string]$QtPrefix,
    [Parameter(Mandatory = $true)][string]$Python,
    [string]$WorkingDirectory = ([IO.Path]::GetTempPath()),
    [ValidateRange(1, 1024)][double]$ReserveGiB = 3
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$Source = [IO.Path]::GetFullPath($Source)
$BuildRoot = [IO.Path]::GetFullPath($BuildRoot)
$QtPrefix = [IO.Path]::GetFullPath($QtPrefix)
$WorkingDirectory = [IO.Path]::GetFullPath($WorkingDirectory)
if ($BuildRecord) { $BuildRecord = [IO.Path]::GetFullPath($BuildRecord) }
$ExpectedCommit = $ExpectedCommit.ToLowerInvariant()
if (-not $IsWindows) { throw 'This monitored native runner requires Windows PowerShell 7. Use the native CI evidence runner on other platforms.' }
$Python = (Get-Command $Python -CommandType Application -ErrorAction Stop).Source
$tool = Join-Path $Source 'tools/qcs-release-evidence.py'
$preflight = Join-Path $PSScriptRoot 'qcs-physical-preflight.py'
$reserveBytes = [long]($ReserveGiB * 1GB)
$priorEnvironment = @{}
foreach ($name in @('PATH','PYTHONUTF8','PYTHONIOENCODING','PYTHONDONTWRITEBYTECODE','GIT_OPTIONAL_LOCKS')) {
    $priorEnvironment[$name] = [Environment]::GetEnvironmentVariable($name,'Process')
}
$env:PATH = ((Join-Path $BuildRoot 'release/bin'),(Join-Path $BuildRoot 'release/lib'),(Join-Path $QtPrefix 'bin'),$env:PATH) -join [IO.Path]::PathSeparator
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:GIT_OPTIONAL_LOCKS = '0'
$arguments = @('-B',$tool,'run-local','--build-record',$BuildRecord,
    '--output-root',(Join-Path $BuildRoot 'release-evidence'),
    '--kind','physical','--program-name','qcs-tests-file-capacity',
    '--report','physical.json','--working-directory',$WorkingDirectory,
    '--input',$PSCommandPath,'--input',$preflight,
    '--env','QCOPILOTS_SEARCH_PHYSICAL_FILE_COUNT=100000',
    '--env','QCOPILOTS_SEARCH_PHYSICAL_FILE_REPORT={report}',
    '--env','QCOPILOTS_SEARCH_TEST_ROOT={evidence}/temporary',
    '--env','QCOPILOTS_SEARCH_KEEP_TEST_ARTIFACTS=0',
    '--env',('QCOPILOTS_SEARCH_DISK_RESERVE_BYTES='+$reserveBytes),
    '--env','QT_FORCE_STDERR_LOGGING=1','--','{program}','-o','-,txt')
$preflightArguments = @('-B',$preflight,'--source',$Source,'--build-root',$BuildRoot,'--qt-prefix',$QtPrefix,
    '--build-record',$BuildRecord,'--expected-commit',$ExpectedCommit)
$executionReport = $null
$exitCode = 1
$runnerPid = $null
function Save-Json([string]$Name,$Value) {
    $Value | ConvertTo-Json -Depth 16 | Set-Content -LiteralPath (Join-Path $executionReport $Name) -Encoding utf8
}
try {
    $plan = [ordered]@{status='plan-only';execute=$false;expectedCommit=$ExpectedCommit;buildRecord=$BuildRecord;
        source=$Source;buildRoot=$BuildRoot;qtPrefix=$QtPrefix;workingDirectory=$WorkingDirectory;
        physicalFiles=100000;reserveBytes=$reserveBytes;program=$python;arguments=$arguments;
        provenanceApi='verified_build + source_identity + digest of six live producers, run-local, inspect_local_run + validate_physical';
        createsFiles=$false;note='Pass -Execute to run the explicitly requested 100000-file acceptance. Without it, only read-only preflight is performed.'}
    if (-not $Execute) {
        if ($ExpectedCommit -and $BuildRecord) {
            $raw = & $python @preflightArguments
            $code = $LASTEXITCODE
            $plan.preflight = ($raw -join "`n") | ConvertFrom-Json
            $plan.candidateReady = $code -eq 0
        } else { $plan.candidateReady = $false; $plan.preflight = 'Deferred until ExpectedCommit and BuildRecord are supplied.' }
        $plan | ConvertTo-Json -Depth 16
        $exitCode = if ($ExpectedCommit -and $BuildRecord -and -not $plan.candidateReady) { 1 } else { 0 }
    } else {
        if (-not $ExpectedCommit -or -not $BuildRecord) { throw '-Execute requires ExpectedCommit and BuildRecord.' }
        $reportRoot = Join-Path $BuildRoot 'reports'
        # Preflight checks the output root and ancestors before any write.
        $raw = & $python @preflightArguments
        $code = $LASTEXITCODE
        $preflightResult = ($raw -join "`n") | ConvertFrom-Json
        if ($code -ne 0 -or -not $preflightResult.passed) { throw 'Candidate/build/six-producer preflight failed. No output was created.' }
        [IO.Directory]::CreateDirectory($reportRoot) | Out-Null
        for ($attempt = 0; $attempt -lt 16; $attempt++) {
            $candidate = Join-Path $reportRoot ('qcs-physical-' + [Guid]::NewGuid().ToString('N').Substring(0,12))
            try {
                New-Item -ItemType Directory -Path $candidate -ErrorAction Stop | Out-Null
                $executionReport = $candidate
                break
            } catch {
                if (-not (Test-Path -LiteralPath $candidate)) { throw }
            }
        }
        if (-not $executionReport) { throw 'Could not reserve a unique physical evidence directory.' }
        $raw = & $python @preflightArguments
        $code = $LASTEXITCODE
        $preflightResult = ($raw -join "`n") | ConvertFrom-Json
        Save-Json 'preflight.json' $preflightResult
        if ($code -ne 0 -or -not $preflightResult.passed) { throw 'Candidate/build/six-producer preflight failed. No physical test was launched.' }
        if (-not (Test-Path -LiteralPath $WorkingDirectory -PathType Container) -or $WorkingDirectory.Length -gt 258) {
            throw 'An existing legal Windows process working directory is required.'
        }
        $volume = [IO.DriveInfo]::new([IO.Path]::GetPathRoot($BuildRoot))
        $copyBudget = [long]$preflightResult.buildEvidenceCopyBudgetBytes + 64MB
        if (-not $volume.IsReady -or $volume.AvailableFreeSpace -le ($reserveBytes + [long]100000 * 8192 + $copyBudget)) {
            throw ('The physical fixture cannot preserve the configured '+$ReserveGiB+' GiB free-space reserve.')
        }
        Save-Json 'invocation.json' ([ordered]@{status='executing';startedAt=[DateTimeOffset]::UtcNow.ToString('o');
            expectedCommit=$ExpectedCommit;buildRecord=$BuildRecord;buildRecordSha256=$preflightResult.buildRecordSha256;
            source=$Source;buildRoot=$BuildRoot;qtPrefix=$QtPrefix;workingDirectory=$WorkingDirectory;
            freeBytesBefore=$volume.AvailableFreeSpace;reserveBytes=$reserveBytes;buildEvidenceCopyBudgetBytes=$copyBudget;
            program=$python;arguments=$arguments;
            wrapperSha256=(Get-FileHash -LiteralPath $PSCommandPath).Hash;preflightSha256=(Get-FileHash -LiteralPath $preflight).Hash})
        $start = [Diagnostics.ProcessStartInfo]::new()
        $start.FileName = $python
        $start.WorkingDirectory = $WorkingDirectory
        $start.UseShellExecute = $false
        $start.CreateNoWindow = $true
        $start.RedirectStandardOutput = $true
        $start.RedirectStandardError = $true
        $start.StandardOutputEncoding = [Text.Encoding]::UTF8
        $start.StandardErrorEncoding = [Text.Encoding]::UTF8
        foreach ($argument in $arguments) { $start.ArgumentList.Add($argument) }
        $process = [Diagnostics.Process]::new()
        $process.StartInfo = $start
        if (-not $process.Start()) { throw 'Cannot launch run-local.' }
        $runnerPid = $process.Id
        Save-Json 'owned-process.json' ([ordered]@{runnerPid=$runnerPid;program=$python;startedAt=[DateTimeOffset]::UtcNow.ToString('o')})
        $stdout = $process.StandardOutput.ReadToEndAsync()
        $stderr = $process.StandardError.ReadToEndAsync()
        $process.WaitForExit()
        [IO.File]::WriteAllText((Join-Path $executionReport 'result.json'),$stdout.GetAwaiter().GetResult(),[Text.UTF8Encoding]::new($false))
        [IO.File]::WriteAllText((Join-Path $executionReport 'runner-stderr.log'),$stderr.GetAwaiter().GetResult(),[Text.UTF8Encoding]::new($false))
        $code = $process.ExitCode
        $process.Dispose()
        if ($code -ne 0) { throw ('Physical run-local failed with exit '+$code+'. All new evidence is retained.') }
        $result = Get-Content -LiteralPath (Join-Path $executionReport 'result.json') -Raw | ConvertFrom-Json
        if (-not $result.passed) { throw 'Physical run-local did not produce a passing execution record.' }
        $postRaw = & $python @preflightArguments '--run-record' $result.record
        $postCode = $LASTEXITCODE
        $post = ($postRaw -join "`n") | ConvertFrom-Json
        Save-Json 'postflight.json' $post
        if ($postCode -ne 0 -or -not $post.passed) { throw 'Physical report, cleanup, current source or six-producer postflight failed.' }
        if ($post.buildRecordSha256 -ne $preflightResult.buildRecordSha256 -or
            ($post.producers | ConvertTo-Json -Depth 8 -Compress) -ne
            ($preflightResult.producers | ConvertTo-Json -Depth 8 -Compress)) {
            throw 'The build receipt or six bound producer paths/hashes changed during the physical run.'
        }
        if (($post.runtimeLibraries | ConvertTo-Json -Depth 8 -Compress) -ne
            ($preflightResult.runtimeLibraries | ConvertTo-Json -Depth 8 -Compress)) {
            throw 'A runtime DLL in the selected build/Qt PATH changed during the physical run.'
        }
        $owned = @(Get-CimInstance Win32_Process -Filter ('ParentProcessId='+$runnerPid) | Select-Object Name,ProcessId,ParentProcessId,ExecutablePath)
        Save-Json 'owned-processes-after.json' $owned
        if ($owned.Count) { throw 'The run-local process still has an owned child after completion.' }
        $volume = [IO.DriveInfo]::new($volume.Name)
        if ($volume.AvailableFreeSpace -lt $reserveBytes) { throw 'The completed run did not retain the configured free-space reserve.' }
        Save-Json 'summary.json' ([ordered]@{status='completed';finishedAt=[DateTimeOffset]::UtcNow.ToString('o');passed=$true;
            expectedCommit=$ExpectedCommit;record=$post.record;report=$post.report;identity=$post.identity;
            createdPhysicalFiles=$post.physical.createdPhysicalFiles;modified=$post.physical.modified;deleted=$post.physical.deleted;
            added=$post.physical.added;cancelledAfterDiscoveredFiles=$post.physical.cancelledAfterDiscoveredFiles;
            staleResults=$post.physical.staleResults;cleanupVerified=$post.physical.cleanupVerified;
            actualFixtureRemoved=$post.actualFixtureRemoved;residualOwnedProcesses=$owned;freeBytesAfter=$volume.AvailableFreeSpace;
            rawExecutionAndLogs='Stored and hashed by run-local in the record directory';sixProducerBindingsVerified=$true;
            runtimeLibrariesUnchanged=$true})
        Write-Output $post.record
        $exitCode = 0
    }
} catch {
    if ($executionReport) {
        $_ | Out-String | Set-Content -LiteralPath (Join-Path $executionReport 'wrapper-error.log') -Encoding utf8
        Save-Json 'summary.json' ([ordered]@{status='failed';passed=$false;finishedAt=[DateTimeOffset]::UtcNow.ToString('o');
            expectedCommit=$ExpectedCommit;buildRecord=$BuildRecord;runnerPid=$runnerPid;error=$_.Exception.Message;preservedNewEvidence=$true})
    }
    Write-Error -Message $_ -ErrorAction Continue
} finally {
    foreach ($name in $priorEnvironment.Keys) { [Environment]::SetEnvironmentVariable($name,$priorEnvironment[$name],'Process') }
}
exit $exitCode
