# PowerShell 5.1+ entry point. Python is a build-time tool only.
$ErrorActionPreference = 'Stop'
if ($IsWindows -or $env:OS -eq 'Windows_NT') {
    if (-not (Get-Command cl.exe -ErrorAction SilentlyContinue)) {
        $vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
        if (Test-Path -LiteralPath $vswhere) {
            $installation = & $vswhere -latest -products '*' -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
            if ($installation) {
                Import-Module (Join-Path $installation 'Common7\Tools\Microsoft.VisualStudio.DevShell.dll')
                Enter-VsDevShell -VsInstallPath $installation -SkipAutomaticLocation -DevCmdArguments '-arch=x64 -host_arch=x64' | Out-Null
            }
        }
    }
}
$pythonCommand = $env:QCOPILOTS_SEARCH_PYTHON
if (-not $pythonCommand) {
    foreach ($candidate in @('python3', 'python', 'py')) {
        $resolved = Get-Command $candidate -ErrorAction SilentlyContinue
        if ($resolved -and (& $resolved.Source -c 'import sys; print(sys.version_info >= (3, 10))' 2>$null) -eq 'True') {
            $pythonCommand = $resolved.Source
            break
        }
    }
}
if (-not $pythonCommand) { throw 'Python 3.10+ is required for developer scripts. Set QCOPILOTS_SEARCH_PYTHON to its executable.' }
& $pythonCommand (Join-Path $PSScriptRoot 'tools\qcopilots-search-dev.py') @args
exit $LASTEXITCODE
