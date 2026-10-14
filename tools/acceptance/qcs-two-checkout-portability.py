#!/usr/bin/env python3
"""Opt-in Windows two-checkout portability evidence. Default mode only prints a plan.

Writes only a fresh UUID directory below --run-parent plus the existing main
build runner's normal reports/runs. Never changes the original source ACL.
Does not execute model, service or GUI tests and does not claim those contracts.
"""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

sys.dont_write_bytecode = True
# Contract harnesses load this file by path from an unrelated working directory.
# Resolve its shared process helper from the script directory in that case too.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from qcs_owned_processes import WindowsProcesses
TARGETS = ["qcs-tests-" + suffix for suffix in ("core", "file-catalog", "model-files")]
APPLICATIONS = {"qcs-gui", "qcs-cli", "qcs-service", "qcs-worker"}
CONFIGURATIONS = ("Release", "Debug", "RelWithDebInfo")


def utc():
    return datetime.now(timezone.utc).isoformat()


def sha(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def write(path, data):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def select_variant(source, identifier=None):
    """Resolve the Windows configuration without requiring execution on Windows."""
    variants = json.loads((Path(source) / "config/build-variants.json").read_text(encoding="utf-8"))["variants"]
    identifier = identifier or os.environ.get("QCOPILOTS_SEARCH_VARIANT")
    if not identifier:
        defaults = [item["id"] for item in variants
                    if (item["os"], item["arch"], item["backend"]) == ("windows", "x64", "cpu")]
        require(len(defaults) == 1, "The build manifest must contain one default Windows x64 CPU variant")
        identifier = defaults[0]
    selected = [item for item in variants if item["id"] == identifier]
    require(len(selected) == 1 and selected[0]["os"] == "windows" and selected[0]["arch"] == "x64",
            "Two-checkout portability requires a manifest Windows x64 variant: " + identifier)
    require(re.fullmatch(r"[a-z0-9]+(?:[.-][a-z0-9]+)*", identifier), "Invalid build variant identifier")
    return identifier


def build_layout(base, variant, config):
    require(config in CONFIGURATIONS, "Invalid build configuration")
    root = Path(base).resolve() / variant
    return {"root": root, "build": root / config,
            **{name: root / name / config for name in ("stage", "pkg", "reports", "runs", "deps")}}


def snapshot(directory, *, exclude_git=True):
    """Read source bytes without following junctions/symlinks."""
    root = Path(directory).resolve()
    values = {}
    for parent, directories, files in os.walk(root, followlinks=False):
        directories[:] = sorted(d for d in directories if not (exclude_git and d == ".git")
                                and not Path(parent, d).is_symlink()
                                and not getattr(Path(parent, d), "is_junction", lambda: False)())
        for name in sorted(files):
            path = Path(parent, name)
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                values[relative] = {"link": os.readlink(path)}
            else:
                values[relative] = {"bytes": path.stat().st_size, "sha256": sha(path)}
    return values


def executable_snapshot(bin_dir, git_dll):
    files = [Path(bin_dir) / (name + ".exe") for name in TARGETS] + [Path(git_dll)]
    require(all(path.is_file() for path in files), "Missing independently built test executable or git2.dll")
    return {str(path.resolve()): {"bytes": path.stat().st_size, "sha256": sha(path)} for path in files}


def clone_executable_snapshot(build):
    # The subset build does not run the ALL deployment target that copies
    # native runtimes into bin. Use this checkout's explicitly configured nd.
    build = Path(build)
    deployed_dll = build / "bin/git2.dll"
    require(not deployed_dll.exists() and not deployed_dll.is_symlink(),
            "Unexpected bin/git2.dll would override the isolated native runtime")
    git_dll = build / "nd/libgit2/git2.dll"
    return git_dll, executable_snapshot(build / "bin", git_dll)


def command_env(base, build, git_dll, *, main_build, qt_prefix, nsis_root):
    env = dict(base)
    exclude = {(main_build / name).resolve() for name in ("bin", "lib")}
    rest = [piece for piece in env.get("PATH", "").split(os.pathsep)
            if piece and Path(piece).resolve() not in exclude]
    env["PATH"] = os.pathsep.join([str(build / "bin"), str(Path(git_dll).parent), str(qt_prefix / "bin"), str(nsis_root), *rest])
    env["NSISDIR"] = str(nsis_root)
    env["QT_PLUGIN_PATH"] = str(qt_prefix / "plugins")
    env["QCOPILOTS_SEARCH_PYTHON"] = sys.executable
    env["PATH"] = os.pathsep.join((str(nsis_root), env.get("PATH", "")))
    env["NSISDIR"] = str(nsis_root)
    require(Path(shutil.which("makensis", path=env["PATH"])).resolve() == (nsis_root / "makensis.exe").resolve(),
            "Locked NSIS compiler is not first on the prepared PATH")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["GIT_OPTIONAL_LOCKS"] = "0"
    return env


def validate_test_command(command, name, source, build, test_root):
    """Bind the isolation wrapper and its inner command to this checkout."""
    command = list(command)
    while len(command) > 1 and command[1] in {"-B", "-u"}:
        command.pop(1)
    expected_wrapper = (Path(source) / "tools/qcs-test-runner.py").resolve()
    require(len(command) >= 8 and Path(command[0]).resolve() == Path(sys.executable).resolve()
            and Path(command[1]).resolve() == expected_wrapper,
            "CTest command does not use the selected Python and this checkout's isolation wrapper: " + name)
    require(command[2:6] == ["--label", name, "--root", str(test_root)]
            or (command[2:5] == ["--label", name, "--root"]
                and Path(command[5]).resolve() == Path(test_root).resolve()),
            "CTest isolation label or writable root differs: " + name)
    separator = 7 if command[6] == "--keep-failed" else 6
    require(command[separator] == "--", "CTest isolation wrapper has unexpected arguments: " + name)
    inner = command[separator + 1:]
    if name in TARGETS:
        require(bool(inner) and inner[1:] in ([], ["-o", "-,txt"])
                and Path(inner[0]).resolve() == (Path(build) / "bin" / (name + ".exe")).resolve(),
                "CTest command does not point to its own checkout binary: " + name)
    else:
        while len(inner) > 1 and inner[1] in {"-B", "-u"}:
            inner.pop(1)
        require(name == "qcs-tests-workflow" and len(inner) == 2
                and Path(inner[0]).resolve() == Path(sys.executable).resolve()
                and Path(inner[1]).resolve() == (Path(source) / "tools/qcs-test-workflow.py").resolve(),
                "Workflow inventory does not point to this checkout script and selected Python")
    return {"path": str(expected_wrapper), "sha256": sha(expected_wrapper)}


def run_size(root):
    total = 0
    for path in root.rglob("*"):
        try:
            if path.is_file() and not path.is_symlink():
                total += path.stat().st_size
        except FileNotFoundError:
            # Ninja and tests can atomically rename their own temporary files.
            continue
    return total


ACL_COMPARE_SCRIPT = r'''function Compare-QcsAclSddl([string]$Original, [string]$Actual) {
  $before = [System.Security.AccessControl.RawSecurityDescriptor]::new($Original)
  $after = [System.Security.AccessControl.RawSecurityDescriptor]::new($Actual)
  $beforeFlags = [int]$before.ControlFlags
  $afterFlags = [int]$after.ControlFlags
  $autoInherited = [int][System.Security.AccessControl.ControlFlags]::DiscretionaryAclAutoInherited
  $originalBytes = [byte[]]::new($before.BinaryLength)
  $actualBytes = [byte[]]::new($after.BinaryLength)
  $before.GetBinaryForm($originalBytes, 0)
  $after.GetBinaryForm($actualBytes, 0)
  # NTFS may set AI while reapplying an inherited DACL. Only that 0 -> 1
  # transition is acceptable. All other flags and ordered ACE bytes remain exact.
  $added = (($beforeFlags -band $autoInherited) -eq 0) -and ($afterFlags -eq ($beforeFlags -bor $autoInherited))
  if ($added) { $after.SetFlags($before.ControlFlags) }
  $beforeBytes = [byte[]]::new($before.BinaryLength)
  $afterBytes = [byte[]]::new($after.BinaryLength)
  $before.GetBinaryForm($beforeBytes, 0)
  $after.GetBinaryForm($afterBytes, 0)
  [pscustomobject]@{
    originalSddl=$Original;actualSddl=$Actual
    originalBinaryBase64=[Convert]::ToBase64String($originalBytes)
    actualBinaryBase64=[Convert]::ToBase64String($actualBytes)
    originalControlFlags=$beforeFlags;actualControlFlags=$afterFlags
    daclAutoInheritedAdded=$added;sddlExactlyEqual=($Original -ceq $Actual)
    semanticMatch=([Convert]::ToBase64String($beforeBytes) -ceq [Convert]::ToBase64String($afterBytes))
  }
}
'''

ACL_READ_SCRIPT = r'''Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
public static class QcsAclReader {
  [DllImport("advapi32.dll", CharSet=CharSet.Unicode)]
  static extern uint GetNamedSecurityInfoW(string path, int kind, uint info,
    out IntPtr owner, out IntPtr group, out IntPtr dacl, out IntPtr sacl, out IntPtr descriptor);
  [DllImport("advapi32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
  static extern bool ConvertSecurityDescriptorToStringSecurityDescriptorW(
    IntPtr descriptor, uint revision, uint info, out IntPtr text, out uint size);
  [DllImport("kernel32.dll")] static extern IntPtr LocalFree(IntPtr memory);
  public static string Read(string path) {
    string native = path.StartsWith(@"\\?\") ? path : path.StartsWith(@"\\")
      ? @"\\?\UNC\" + path.Substring(2) : @"\\?\" + path;
    IntPtr owner, group, dacl, sacl, descriptor;
    uint error = GetNamedSecurityInfoW(native, 1, 7, out owner, out group, out dacl, out sacl, out descriptor);
    if (error != 0) throw new Win32Exception((int)error, path);
    try {
      IntPtr text; uint size;
      if (!ConvertSecurityDescriptorToStringSecurityDescriptorW(descriptor, 1, 7, out text, out size))
        throw new Win32Exception(Marshal.GetLastWin32Error(), path);
      try { return Marshal.PtrToStringUni(text); } finally { LocalFree(text); }
    } finally { LocalFree(descriptor); }
  }
}
'@
'''

ACL_SCRIPT = r'''param([string]$Source,[string]$Report,[string]$Mode)
''' + ACL_COMPARE_SCRIPT + ACL_READ_SCRIPT + r'''
$ErrorActionPreference = 'Stop'
$ownedRoot = Split-Path -Parent $PSScriptRoot
foreach ($boundary in @($ownedRoot, $PSScriptRoot, (Join-Path $ownedRoot '.qcs-owned-run.json'))) {
  if ((Get-Item -LiteralPath $boundary -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
    throw 'ACL ownership boundary must not be a reparse point'
  }
}
$marker = Get-Content -LiteralPath (Join-Path $ownedRoot '.qcs-owned-run.json') -Raw | ConvertFrom-Json
$expectedClone = [IO.Path]::GetFullPath((Join-Path $ownedRoot '中文 源码'))
$resolvedSource = [IO.Path]::GetFullPath($Source)
if ($Mode -in @('deny','restore')) {
  if ($resolvedSource -ne $expectedClone -or ((Get-Item -LiteralPath $Source).Attributes -band [IO.FileAttributes]::ReparsePoint)) {
    throw 'ACL mutation is restricted to this invocation owned clone'
  }
  if ([IO.Path]::GetFullPath($Report) -ne [IO.Path]::GetFullPath((Join-Path $PSScriptRoot 'source-original-acl.json'))) {
    throw 'ACL recovery record must belong to this invocation'
  }
} elseif ($Mode -eq 'inspect' -and $resolvedSource -ne [IO.Path]::GetFullPath($marker.source)) {
  throw 'Read-only ACL inspection must target the recorded original checkout'
}
if ($Mode -eq 'inspect') {
  $sddl = [QcsAclReader]::Read($resolvedSource)
  [pscustomobject]@{Owner=([Security.AccessControl.RawSecurityDescriptor]::new($sddl)).Owner.Value;Sddl=$sddl} | ConvertTo-Json -Compress
  exit 0
}
if ($Mode -notin @('deny','restore')) { throw 'Unknown bounded ACL operation' }
# Keep every node open without delete sharing while changing permissions. This
# prevents rename/replacement races, including parent-directory replacement.
# OPEN_REPARSE_POINT inspects links themselves, never their external targets.
Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
using Microsoft.Win32.SafeHandles;
public sealed class QcsAclNode : IDisposable {
  [StructLayout(LayoutKind.Sequential)] struct Info {
    public uint Attributes; public System.Runtime.InteropServices.ComTypes.FILETIME Created, Accessed, Written;
    public uint Volume, SizeHigh, SizeLow, Links, IndexHigh, IndexLow;
  }
  [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
  static extern SafeFileHandle CreateFileW(string path, uint access, uint share, IntPtr security, uint creation, uint flags, IntPtr template);
  [DllImport("kernel32.dll", SetLastError=true)]
  static extern bool GetFileInformationByHandle(SafeFileHandle file, out Info info);
  [DllImport("advapi32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
  static extern bool ConvertStringSecurityDescriptorToSecurityDescriptorW(string sddl, uint revision, out IntPtr descriptor, out uint size);
  [DllImport("advapi32.dll", SetLastError=true)]
  static extern bool GetSecurityDescriptorDacl(IntPtr descriptor, out bool present, out IntPtr dacl, out bool defaulted);
  [DllImport("advapi32.dll", SetLastError=true)]
  static extern bool GetSecurityDescriptorControl(IntPtr descriptor, out ushort control, out uint revision);
  [DllImport("advapi32.dll", CharSet=CharSet.Unicode)]
  static extern uint SetNamedSecurityInfoW(string path, int kind, uint info, IntPtr owner, IntPtr group, IntPtr dacl, IntPtr sacl);
  [DllImport("kernel32.dll")] static extern IntPtr LocalFree(IntPtr memory);
  SafeFileHandle handle;
  public string Identity { get; private set; }
  public bool Directory { get; private set; }
  static string NativePath(string path) {
    return path.StartsWith(@"\\?\") ? path : path.StartsWith(@"\\") ? @"\\?\UNC\" + path.Substring(2) : @"\\?\" + path;
  }
  public static QcsAclNode Open(string path) {
    var handle = CreateFileW(NativePath(path), 0, 3, IntPtr.Zero, 3, 0x02200000, IntPtr.Zero);
    if (handle.IsInvalid) { int error=Marshal.GetLastWin32Error(); handle.Dispose(); throw new Win32Exception(error, path); }
    try {
      Info info;
      if (!GetFileInformationByHandle(handle, out info)) throw new Win32Exception(Marshal.GetLastWin32Error(), path);
      if ((info.Attributes & 0x400) != 0) throw new InvalidOperationException("Reparse point in owned ACL tree: " + path);
      bool directory=(info.Attributes & 0x10) != 0;
      if (!directory && info.Links != 1) throw new InvalidOperationException("Non-private hard-linked ACL file: " + path);
      return new QcsAclNode { handle=handle, Directory=directory,
        Identity=info.Volume.ToString("x8") + ":" + info.IndexHigh.ToString("x8") + info.IndexLow.ToString("x8") };
    } catch { handle.Dispose(); throw; }
  }
  public static void SetDacl(string path, string sddl) {
    IntPtr descriptor; uint size;
    if (!ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, out descriptor, out size))
      throw new Win32Exception(Marshal.GetLastWin32Error());
    try {
      bool present, defaulted; IntPtr dacl; ushort control; uint revision;
      if (!GetSecurityDescriptorDacl(descriptor, out present, out dacl, out defaulted)
          || !GetSecurityDescriptorControl(descriptor, out control, out revision))
        throw new Win32Exception(Marshal.GetLastWin32Error());
      if (!present) throw new InvalidOperationException("Expected a recorded DACL");
      // Change only DACL and its original protection flag. In particular do not
      // request SACL, owner or group access, or enable SeSecurityPrivilege.
      uint flags = 4u | ((control & 0x1000) != 0 ? 0x80000000u : 0x20000000u);
      uint error = SetNamedSecurityInfoW(NativePath(path), 1, flags, IntPtr.Zero, IntPtr.Zero, dacl, IntPtr.Zero);
      if (error != 0) throw new Win32Exception((int)error, path);
    } finally { LocalFree(descriptor); }
  }
  public void Dispose() { if (handle != null) handle.Dispose(); }
}
'@
function Save-QcsAclRecord($Path, $Value) {
  $Value | ConvertTo-Json -Depth 15 | Set-Content -LiteralPath $Path -Encoding utf8
}
function Open-QcsAclTree {
  $pending = [Collections.Generic.Queue[string]]::new()
  $pending.Enqueue($resolvedSource)
  while ($pending.Count) {
    $path = $pending.Dequeue()
    try { $node = [QcsAclNode]::Open($path) } catch {
      if ($Mode -ne 'restore' -or $path -eq $resolvedSource) { throw }
      $errors.Add($path + ': cannot safely open ACL node: ' + $_.Exception.Message)
      continue
    }
    $handles.Add($node)
    $relative = [IO.Path]::GetRelativePath($resolvedSource, $path).Replace('\','/')
    $entry = [pscustomobject]@{relativePath=$relative;path=$path;kind=$(if ($node.Directory) {'directory'} else {'file'});fileIdentity=$node.Identity}
    $entries.Add($entry)
    if ($node.Directory) {
      try {
        foreach ($child in @(Get-ChildItem -LiteralPath $path -Force | Sort-Object Name)) { $pending.Enqueue($child.FullName) }
      } catch {
        if ($Mode -ne 'restore') { throw }
        $errors.Add($path + ': cannot enumerate ACL directory: ' + $_.Exception.Message)
      }
    }
  }
}
$handles = [Collections.Generic.List[QcsAclNode]]::new()
$entries = [Collections.Generic.List[object]]::new()
$errors = [Collections.Generic.List[string]]::new()
try {
  $handles.Add([QcsAclNode]::Open([IO.Path]::GetFullPath($ownedRoot)))
  $handles.Add([QcsAclNode]::Open([IO.Path]::GetFullPath($PSScriptRoot)))
  Open-QcsAclTree
  if ($Mode -eq 'deny') {
    if (Test-Path -LiteralPath $Report) { throw 'Refusing to overwrite existing ACL recovery evidence' }
    $sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
    $nodes = @($entries | ForEach-Object {
      # FileSystemSecurity canonicalizes explicit ACE order even when reading.
      # Record the native descriptor before constructing any managed ACL object.
      $sddl = [QcsAclReader]::Read($_.path)
      $descriptor = Compare-QcsAclSddl $sddl $sddl
      [pscustomobject]@{relativePath=$_.relativePath;kind=$_.kind;fileIdentity=$_.fileIdentity;originalSddl=$sddl;
        originalBinaryBase64=$descriptor.originalBinaryBase64;originalControlFlags=$descriptor.originalControlFlags}
    })
    # Persist all original ACLs before the first mutation, including protected
    # child DACLs and explicit Allow entries that outrank inherited Deny ACEs.
    $original = [pscustomobject]@{schemaVersion=2;source=$Source;sid=$sid.Value;sddl=$nodes[0].originalSddl;nodes=$nodes}
    Save-QcsAclRecord $Report $original
    $applied = @{}
    $denyError = $null
    try {
      foreach ($entry in $entries) {
        $acl = Get-Acl -LiteralPath $entry.path
        $rule = [Security.AccessControl.FileSystemAccessRule]::new($sid,
          [Security.AccessControl.FileSystemRights]'Write,Delete,DeleteSubdirectoriesAndFiles',
          [Security.AccessControl.InheritanceFlags]::None,
          [Security.AccessControl.PropagationFlags]::None,
          [Security.AccessControl.AccessControlType]::Deny)
        $acl.AddAccessRule($rule)
        [QcsAclNode]::SetDacl($entry.path, $acl.Sddl)
        $applied[$entry.relativePath] = $true
      }
    } catch { $denyError = $_ }
    try {
      $denyRows = @($entries | ForEach-Object {
        $entry = $_
        $row = [ordered]@{relativePath=$entry.relativePath;kind=$entry.kind;fileIdentity=$entry.fileIdentity;applied=$applied.ContainsKey($entry.relativePath)}
        try { $row.actualSddl=[QcsAclReader]::Read($entry.path) } catch { $row.error=$_.Exception.Message }
        [pscustomobject]$row
      })
      Save-QcsAclRecord (Join-Path $PSScriptRoot 'source-denied-acl.json') ([pscustomobject]@{schemaVersion=2;source=$Source;nodeCount=$nodes.Count;appliedCount=$applied.Count;nodes=$denyRows;error=$(if ($denyError) {$denyError.Exception.Message} else {$null})})
    } catch {
      if ($denyError) { Write-Warning ('Cannot save ACL denial diagnostics: ' + $_.Exception.Message) } else { throw }
    }
    if ($denyError) { throw $denyError }
  } else {
    $original = Get-Content -LiteralPath $Report -Raw | ConvertFrom-Json
    if ($original.source -ne $Source -or $original.schemaVersion -ne 2 -or -not $original.nodes.Count) {
      throw 'ACL recovery path or complete node inventory mismatch'
    }
    $actualNodes = @{}
    foreach ($entry in $entries) { $actualNodes[$entry.relativePath] = $entry }
    $originalRoots = @($original.nodes | Where-Object relativePath -EQ '.')
    if ($originalRoots.Count -ne 1 -or $originalRoots[0].fileIdentity -cne $actualNodes['.'].fileIdentity) {
      throw 'Original owned clone root identity was replaced'
    }
    $seen = @{}
    $restoreRows = [Collections.Generic.List[object]]::new()
    foreach ($old in $original.nodes) {
      $row = [ordered]@{relativePath=$old.relativePath;kind=$old.kind;fileIdentity=$old.fileIdentity;originalSddl=$old.originalSddl;restored=$false;semanticMatch=$false}
      try {
        if ($seen.ContainsKey($old.relativePath)) { throw 'Duplicate ACL recovery entry' }
        $seen[$old.relativePath] = $true
        if (-not $actualNodes.ContainsKey($old.relativePath)) { throw 'Original ACL node is missing' }
        $entry = $actualNodes[$old.relativePath]
        if ($entry.fileIdentity -cne $old.fileIdentity -or $entry.kind -cne $old.kind) { throw 'Original ACL node identity was replaced' }
        $row.beforeRestorationSddl = [QcsAclReader]::Read($entry.path)
        # Only the DACL was changed. Do not request owner/group/SACL privileges.
        # The final comparison still verifies the complete original descriptor.
        [QcsAclNode]::SetDacl($entry.path, $old.originalSddl)
        $row.restored = $true
      } catch { $row.error = $_.Exception.Message; $errors.Add($old.relativePath + ': ' + $_.Exception.Message) }
      $restoreRows.Add([pscustomobject]$row)
    }
    foreach ($entry in $entries) {
      if (-not $seen.ContainsKey($entry.relativePath)) { $errors.Add('Unexpected node in ACL recovery tree: ' + $entry.relativePath) }
    }
    # Read the entire tree again only after all parent-first restorations.
    # The native DACL setter can propagate inheritable ACEs to descendants.
    foreach ($row in $restoreRows) {
      if (-not $row.restored) { continue }
      try {
        $comparison = Compare-QcsAclSddl $row.originalSddl ([QcsAclReader]::Read($actualNodes[$row.relativePath].path))
        foreach ($property in $comparison.PSObject.Properties) { $row | Add-Member -NotePropertyName $property.Name -NotePropertyValue $property.Value -Force }
        if (-not $comparison.semanticMatch) { throw 'Original node security semantics were not restored' }
      } catch {
        $row.restored=$false; $row.semanticMatch=$false
        $row | Add-Member -NotePropertyName error -NotePropertyValue $_.Exception.Message -Force
        $errors.Add($row.relativePath + ': ' + $_.Exception.Message)
      }
    }
    $result = [ordered]@{}
    $rootRow = @($restoreRows | Where-Object relativePath -EQ '.')[0]
    if ($rootRow) { foreach ($property in $rootRow.PSObject.Properties) { $result[$property.Name] = $property.Value } }
    $result.schemaVersion=2; $result.nodeCount=$original.nodes.Count; $result.nodes=$restoreRows.ToArray(); $result.errors=$errors.ToArray()
    $result.allNodesRestored=($errors.Count -eq 0 -and $restoreRows.Count -eq $original.nodes.Count)
    $result.semanticMatch=($result.semanticMatch -and $result.allNodesRestored)
    Save-QcsAclRecord (Join-Path $PSScriptRoot 'source-restored-acl.json') $result
    $result | ConvertTo-Json -Depth 15 -Compress
    if (-not $result.allNodesRestored) { throw 'ACL restoration did not reproduce every original node' }
  }
} finally {
  foreach ($handle in $handles) { $handle.Dispose() }
}
'''

POWERSHELL_BRIDGE_SCRIPT = r'''param([Parameter(Mandatory=$true)][string]$Request)
$ErrorActionPreference = 'Stop'
$requestData = Get-Content -LiteralPath $Request -Raw -Encoding utf8 | ConvertFrom-Json
if ($requestData.script -isnot [string] -or $requestData.arguments -isnot [array]) {
  throw 'Invalid developer invocation request'
}
foreach ($argument in $requestData.arguments) {
  if ($argument -isnot [string]) { throw 'Developer arguments must be strings' }
}
# PowerShell's native -File parser splits dash-prefixed arguments at a colon.
# Deserialize literal strings and splat directly, without evaluating their text.
$scriptPath = $requestData.script
$scriptArguments = @($requestData.arguments)
& $scriptPath @scriptArguments
exit $LASTEXITCODE
'''


def powershell_command(pwsh, bridge, request, script, arguments):
    write(request, {"script": str(script), "arguments": [str(value) for value in arguments]})
    return [pwsh, "-NoProfile", "-File", bridge, "-Request", request]

ENV_SCRIPT = r'''$ErrorActionPreference = 'Stop'
$vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
$installation = & $vswhere -latest -version '[17.0,18.0)' -products '*' -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
if (-not $installation) { throw 'No installed x64 MSVC toolchain' }
Import-Module (Join-Path $installation 'Common7\Tools\Microsoft.VisualStudio.DevShell.dll')
Enter-VsDevShell -VsInstallPath $installation -SkipAutomaticLocation -DevCmdArguments '-arch=x64 -host_arch=x64' | Out-Null
Get-ChildItem Env: | Select-Object Name,Value | ConvertTo-Json -Compress
'''


def probe_write_access(path, existing):
    """Open without truncation, retaining Windows' actual access error code."""
    if os.name != "nt":
        flags = os.O_WRONLY if existing else os.O_WRONLY | os.O_CREAT | os.O_EXCL
        descriptor = os.open(path, flags, 0o600)
        os.close(descriptor)
        return
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    # Share read/write/delete so a lock cannot masquerade as an ACL refusal.
    native = os.path.abspath(path)
    if not native.startswith("\\\\?\\"):
        native = "\\\\?\\UNC\\" + native[2:] if native.startswith("\\\\") else "\\\\?\\" + native
    handle = kernel.CreateFileW(native, 0x40000000, 7, None, 3 if existing else 1, 0x80, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return
    finally:
        if not kernel.CloseHandle(handle):
            raise ctypes.WinError(ctypes.get_last_error())


def write_probes(source):
    """Return every result before the caller enforces the all-denied gate."""
    source = Path(source)
    records = []
    for relative, existing in (("qcs-write-probe.tmp", False), ("CMakeLists.txt", True),
                               ("src/core/qcs-write-probe.tmp", False)):
        path = source / relative
        try:
            probe_write_access(path, existing)
        except OSError as error:
            records.append({"path": str(path), "denied": isinstance(error, PermissionError)
                            and (os.name != "nt" or getattr(error, "winerror", None) == 5),
                            "error": str(error), "winerror": getattr(error, "winerror", None)})
        else:
            row = {"path": str(path), "denied": False}
            if not existing:
                try:
                    path.unlink()
                except OSError as error:
                    row["cleanupError"] = str(error)
            records.append(row)
    for operation in ("delete", "rename"):
        path = source / ".git" / ("qcs-acl-" + operation + ".tmp")
        try:
            if operation == "delete":
                path.unlink()
            else:
                path.rename(path.with_suffix(".renamed"))
        except OSError as error:
            records.append({"path": str(path), "operation": operation,
                            "denied": isinstance(error, PermissionError)
                            and (os.name != "nt" or getattr(error, "winerror", None) == 5),
                            "error": str(error), "winerror": getattr(error, "winerror", None)})
        else:
            records.append({"path": str(path), "operation": operation, "denied": False})
    return records


def require_readonly_probes(records):
    require(len(records) == 5 and all(item.get("denied") is True for item in records),
            "The isolated source is not effectively read-only")


def restored_write_probes(source):
    """Exercise restored access using only this invocation's disposable files."""
    source = Path(source)
    records = []
    for relative in (".", "src/core", ".git"):
        directory = source / relative
        path = directory / ("qcs-acl-r-" + uuid.uuid4().hex[:12] + ".tmp")
        renamed = path.with_suffix(".renamed")
        created = False
        row = {"directory": str(directory), "createWriteReadRenameDelete": False}
        try:
            require(directory.is_dir() and not (getattr(directory.lstat(), "st_file_attributes", 0) & 0x400)
                    and not directory.is_symlink() and directory.resolve().is_relative_to(source.resolve()),
                    "Restored write probe directory is not an owned plain directory")
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
            created = True
            try:
                os.write(descriptor, b"owned restored access probe\n")
            finally:
                os.close(descriptor)
            require(path.read_bytes() == b"owned restored access probe\n", "Restored file bytes differ")
            path.rename(renamed)
            row["createWriteReadRenameDelete"] = True
        except (OSError, RuntimeError) as error:
            row["error"] = str(error)
        finally:
            for candidate in (path, renamed):
                try:
                    if created and candidate.exists():
                        candidate.unlink()
                except OSError as error:
                    row["createWriteReadRenameDelete"] = False
                    row.setdefault("cleanupErrors", []).append(str(error))
        records.append(row)
    row = {"path": str(source / "CMakeLists.txt"), "existingFileWritable": False}
    try:
        descriptor = os.open(source / "CMakeLists.txt", os.O_WRONLY)
        os.close(descriptor)  # Open without truncation or changing the source bytes.
        row["existingFileWritable"] = True
    except OSError as error:
        row["error"] = str(error)
    records.append(row)
    for operation in ("delete", "rename"):
        path = source / ".git" / ("qcs-acl-" + operation + ".tmp")
        row = {"operation": operation, "originalProbeRestored": False}
        try:
            if operation == "rename":
                path = path.rename(path.with_suffix(".renamed"))
            path.unlink()
            row["originalProbeRestored"] = True
        except OSError as error:
            row["error"] = str(error)
        records.append(row)
    return records


def require_restored_write_probes(records):
    keys = ("createWriteReadRenameDelete", "existingFileWritable", "originalProbeRestored")
    require(len(records) == 6 and all(any(row.get(key) is True for key in keys) for row in records),
            "Not every owned restoration write probe succeeded")


def record_acl_restoration(record, returncode, stdout, source):
    """Keep a primary test failure intact while recording restoration failures."""
    record["isolatedSourceAclRestored"] = False
    try:
        result = json.loads(stdout.decode("utf-8-sig") if isinstance(stdout, bytes) else stdout)
        require(isinstance(result, dict), "ACL restoration report is not an object")
        record["isolatedSourceAclRestoration"] = result
        rows = result.get("nodes", [])
        require(isinstance(rows, list) and all(isinstance(row, dict) for row in rows),
                "ACL restoration node evidence is malformed")
        require(returncode == 0 and result.get("semanticMatch") is True
                and result.get("allNodesRestored") is True and result.get("nodeCount") == len(rows)
                and bool(rows) and not result.get("errors")
                and all(row.get("restored") is True and row.get("semanticMatch") is True for row in rows),
                "ACL restoration did not verify every original node")
        record["restoredWriteProbes"] = restored_write_probes(source)
        require_restored_write_probes(record["restoredWriteProbes"])
        record["isolatedSourceAclRestored"] = True
    except (OSError, RuntimeError, ValueError, TypeError) as error:
        record["aclRestorationError"] = str(error)


def acl_restore_timeout(node_count):
    # Inventory size bounds the per-node native ACL reads/writes and final comparison.
    # Retain a finite ten-minute ceiling even for an unexpectedly large clone.
    require(isinstance(node_count, int) and node_count > 0, "Invalid ACL inventory size")
    return min(600, 30 + (node_count + 4) // 5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ("head", "source-tree"):
        parser.add_argument("--" + flag, required=True)
    for flag in ("build-root", "build-record", "run-parent", "qt-root", "qt-prefix", "calligra-source"):
        parser.add_argument("--" + flag, type=Path, required=True)
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--variant", help="Windows x64 build variant, defaults to the environment or manifest CPU variant")
    parser.add_argument("--config", choices=CONFIGURATIONS, default="Release")
    parser.add_argument("--dependency-root", type=Path)
    parser.add_argument("--gpu-sdk-root", type=Path, default=os.environ.get("QCOPILOTS_SEARCH_GPU_SDK_ROOT"),
                        help="Existing shared GPU SDK cache, defaults to BUILD_ROOT/gpu-sdk")
    parser.add_argument("--nsis-root", type=Path, help="Existing locked private NSIS, defaults to BUILD_ROOT/VARIANT/bootstrap/nsis")
    parser.add_argument("--model-dir", type=Path)
    parser.add_argument("--sentinel-launch-record", type=Path,
                        help="Optional existing ordinary GUI/service launch record. Observer never starts or stops them")
    parser.add_argument("--launch-cwd", type=Path, default=Path.cwd())
    parser.add_argument("--jobs", type=int, default=2)
    parser.add_argument("--reserve-gib", type=float, default=3.0)
    parser.add_argument("--budget-gib", type=float, default=0.5)
    parser.add_argument("--command-timeout", type=int, default=900)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    require(re.fullmatch(r"[0-9a-f]{40}", args.head) and re.fullmatch(r"[0-9a-f]{40}", args.source_tree),
            "Head and tree must be full lower-case Git identities")
    args.variant = select_variant(args.source, args.variant)
    main_layout = build_layout(args.build_root, args.variant, args.config)
    main_build = main_layout["build"]
    nsis_root = (args.nsis_root or main_layout["root"] / "bootstrap/nsis").resolve()
    gpu_sdk_root = (args.gpu_sdk_root or args.build_root / "gpu-sdk").resolve()
    root = args.run_parent.resolve() / ("qcs-port-" + uuid.uuid4().hex[:12])
    plan = {"executeRequested": args.execute, "head": args.head, "sourceTree": args.source_tree,
            "source": str(args.source.resolve()), "buildRecord": str(args.build_record.resolve()), "uniqueRunRoot": str(root),
            "variant": args.variant, "configuration": args.config,
            "buildRoot": str(args.build_root.resolve()), "mainBuildDirectory": str(main_build),
            "newTargets": TARGETS, "readonlyCTestNames": TARGETS + ["qcs-tests-workflow"],
            "originalCTestNames": TARGETS, "scope": "Windows independent subset rebuild and actual concurrent two-checkout tests. No model inference, service startup or GUI control. Optional existing ordinary sentinel observation is reported separately.",
            "reserveGiB": args.reserve_gib, "budgetGiB": args.budget_gib,
            "nsisRoot": str(nsis_root), "gpuSdkRoot": str(gpu_sdk_root),
            "sentinelLaunchRecord": str(args.sentinel_launch_record.resolve()) if args.sentinel_launch_record else None}
    if not args.execute:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 0
    token_spec = importlib.util.spec_from_file_location("qcs_token_context", Path(__file__).with_name("qcs-token-context.py"))
    token_module = importlib.util.module_from_spec(token_spec)
    token_spec.loader.exec_module(token_module)
    child_code = token_module.reexec_if_needed(Path(__file__), sys.argv[1:])
    if child_code is not None:
        return child_code
    require(os.name == "nt" and sys.version_info >= (3, 12), "Windows and Python 3.12+ are required for junction-safe evidence")
    require(args.jobs > 0 and args.reserve_gib >= 3 and args.budget_gib > 0, "Invalid resource parameters")
    source = args.source.resolve()
    nsis_lock_path = source / "tools/nsis-package.lock.json"
    nsis_lock = json.loads(nsis_lock_path.read_text(encoding="utf-8"))
    nsis_marker_path = nsis_root / ".qcs-nsis.json"
    nsis_marker = json.loads(nsis_marker_path.read_text(encoding="utf-8"))
    require(nsis_marker["sha256"] == nsis_lock["sha256"] and nsis_marker["version"] == nsis_lock["version"]
            and sha(nsis_root / "makensis.exe") == nsis_lock["compilerSha256"], "Private NSIS does not match its locked version and compiler")
    for relative in nsis_marker["files"]:
        path = (nsis_root / relative).resolve()
        require(path.is_relative_to(nsis_root) and path.is_file(), "Private NSIS has a missing or unsafe installed member")
    nsis_before = snapshot(nsis_root)
    require(not root.is_relative_to(source) and root != root.parent, "Run root must be outside the original source")
    require(args.launch_cwd.is_dir() and args.qt_prefix.is_dir() and args.calligra_source.is_dir(), "Required directory is missing")
    os.environ["GIT_OPTIONAL_LOCKS"] = "0"
    spec = importlib.util.spec_from_file_location("qcopilots_release_evidence", source / "tools/qcs-release-evidence.py")
    evidence = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(evidence)
    original_identity = evidence.source_identity(source, args.head)
    receipt = evidence.read_json(args.build_record)
    require(original_identity == receipt["identity"] and not evidence.verified_build(args.build_record.parent, receipt), "Source and successful build receipt disagree")
    require(Path(receipt["sourceDirectory"]).resolve() == source, "Build receipt belongs to another source checkout")
    require(original_identity["sourceTree"] == args.source_tree and original_identity["checkoutCommit"] == args.head
            and not original_identity["workingTreeModified"] and set(receipt["programs"]) == APPLICATIONS,
            "The expected frozen clean four-application candidate is not available")
    original_producers = {name: sha(Path(item["originalPath"])) for name, item in receipt["programs"].items()}
    require(all(original_producers[name] == item["sha256"] for name, item in receipt["programs"].items()),
            "Actual main build producer bytes no longer match the frozen build receipt")
    require(all(Path(item["originalPath"]).resolve().parent == main_build / "bin"
                for item in receipt["programs"].values()), "Producer paths do not belong to the selected main build")
    free = shutil.disk_usage(args.run_parent).free
    require(free >= (args.reserve_gib + args.budget_gib) * 1024**3, "Insufficient disk budget with required reserve")
    # mkdtemp uses exclusive creation and retries collisions. No directory name
    # supplied in a dry-run plan is ever claimed as ownership evidence.
    root = Path(tempfile.mkdtemp(prefix="qcs-port-", dir=args.run_parent.resolve()))
    plan["uniqueRunRoot"] = str(root)
    reports = root / "reports"
    reports.mkdir()
    clone, external, caller = root / "中文 源码", root / "外置 构建", root / "任意 工作目录"
    caller.mkdir()
    clone_layout = build_layout(external, args.variant, args.config)
    clone_build = clone_layout["build"]
    plan["cloneBuildDirectory"] = str(clone_build)
    dependency = (args.dependency_root or main_layout["deps"]).resolve()
    model_dir = (args.model_dir or args.build_root / "models").resolve()
    initial_receipt_sha, runner_sha = sha(args.build_record), sha(__file__)
    process_helper_path = Path(__file__).with_name("qcs_owned_processes.py")
    process_helper_sha = sha(process_helper_path)
    preserved_process_helper = reports / process_helper_path.name
    shutil.copyfile(process_helper_path, preserved_process_helper)
    require(sha(preserved_process_helper) == process_helper_sha, "Cannot preserve the exact process helper")
    shutil.copyfile(__file__, reports / "qcs-portability-runner.py")
    require(sha(reports / "qcs-portability-runner.py") == runner_sha, "Cannot preserve this runner's exact source")
    for item in receipt["files"]:
        old = evidence.safe_file(args.build_record.parent, item["path"])
        new = reports / "main-build" / item["path"]
        new.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(old, new)
        require(sha(new) == item["sha256"], "Cannot preserve main build provenance")
    shutil.copyfile(args.build_record, reports / "main-build/build-record.json")
    write(root / ".qcs-owned-run.json", plan)
    (reports / "qcs-owned-acl.ps1").write_text(ACL_SCRIPT, encoding="utf-8")
    (reports / "qcs-vs-environment.ps1").write_text(ENV_SCRIPT, encoding="utf-8")
    bridge = reports / "qcs-dev-bridge.ps1"
    bridge.write_text(POWERSHELL_BRIDGE_SCRIPT, encoding="utf-8")
    bridge_sha = sha(bridge)
    processes = WindowsProcesses()
    pwsh = shutil.which("pwsh")
    require(pwsh is not None, "PowerShell 7 is required")
    commands, jobs, children, streams = [], [], [], []
    record = {**plan, "schemaVersion": 2, "kind": "two-checkout-portability", "passed": False, "startedAt": utc(),
              "identity": original_identity, "buildRecordSha256": initial_receipt_sha, "runnerSha256": runner_sha,
              "processHelper": {"sourcePath": str(process_helper_path), "path": preserved_process_helper.name,
                                "bytes": preserved_process_helper.stat().st_size, "sha256": process_helper_sha},
              "developerBridgeSha256": bridge_sha,
              "freeBeforeBytes": free, "commands": commands,
              "mainProducerHashesBefore": original_producers, "preservedBuildRecord": "main-build/build-record.json"}
    record["nsisTool"] = {"root": str(nsis_root), "lockPath": str(nsis_lock_path), "lockSha256": sha(nsis_lock_path),
                          "compilerSha256": sha(nsis_root / "makensis.exe"), "archiveSha256": nsis_lock["sha256"],
                          "version": nsis_lock["version"], "ownershipMarkerSha256": sha(nsis_marker_path)}
    write(reports / "nsis-files-before.json", nsis_before)
    acl_attempted = False
    before_clone = None
    before_native = None
    before_original = None
    before_original_source = None
    observed, overlap = [{}, {}], []
    sentinel, sentinel_module_path, sentinel_module_sha = None, None, None
    record["sentinelObservation"] = {"requested": bool(args.sentinel_launch_record), "status": "notRun"}

    def guard():
        size = run_size(root)
        record["peakObservedRunBytes"] = max(record.get("peakObservedRunBytes", 0), size)
        require(size <= args.budget_gib * 1024**3, "The task exceeded its configured disk budget")
        require(shutil.disk_usage(root).free >= args.reserve_gib * 1024**3, "Free-space reserve exhausted")

    def start(command, label, env, cwd):
        out, err = (reports / (label + ".stdout.log")).open("wb"), (reports / (label + ".stderr.log")).open("wb")
        streams.extend((out, err))
        child = subprocess.Popen([str(value) for value in command], cwd=cwd, env=env, stdout=out, stderr=err,
                                 creationflags=0x00000004)  # CREATE_SUSPENDED
        children.append(child)
        try:
            job = processes.job(child)
            jobs.append(job)
            processes.resume(child)
        except BaseException:
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=20)
            raise
        entry = {"label": label, "command": [str(value) for value in command], "cwd": str(cwd), "pid": child.pid,
                 "startedAt": utc(), "stdout": out.name, "stderr": err.name}
        commands.append(entry)
        write(reports / "execution-progress.json", record)
        return child, job, entry

    def finish(child, job, entry):
        deadline = time.monotonic() + args.command_timeout
        while child.poll() is None:
            require(time.monotonic() < deadline, "Command timed out: " + entry["label"])
            guard()
            try:
                child.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
        code = child.returncode
        entry.update({"exitCode": code, "finishedAt": utc()})
        until = time.monotonic() + 10
        while processes.active(job) and time.monotonic() < until:
            time.sleep(.02)
        entry["remainingOwnedProcesses"] = processes.active(job)
        require(entry["remainingOwnedProcesses"] == 0, "An owned command left child processes alive")
        require(code == 0, "Command failed: " + entry["label"])
        return entry

    def run(command, label, env, cwd):
        entry = finish(*start(command, label, env, cwd))
        guard()
        return entry

    def developer_command(checkout, label, arguments):
        request = reports / (label + "-arguments.json")
        command = powershell_command(pwsh, bridge, request, checkout / "qcs-dev.ps1", arguments)
        record.setdefault("developerInvocations", {})[label] = {
            "request": str(request), "sha256": sha(request),
            "script": str(checkout / "qcs-dev.ps1"), "arguments": list(map(str, arguments))}
        return command

    try:
        before_original = executable_snapshot(main_build / "bin", main_build / "bin/git2.dll")
        before_original_source = snapshot(source)
        record["originalBinariesBefore"] = before_original
        write(reports / "original-source-before.json", before_original_source)
        plain_env = dict(os.environ)
        run([pwsh, "-NoProfile", "-File", reports / "qcs-vs-environment.ps1"], "vs-environment", plain_env, args.launch_cwd)
        rows = json.loads((reports / "vs-environment.stdout.log").read_text(encoding="utf-8-sig"))
        env = {row["Name"].upper(): row["Value"] for row in rows}
        env["PYTHONDONTWRITEBYTECODE"], env["GIT_OPTIONAL_LOCKS"] = "1", "0"
        env["QCOPILOTS_SEARCH_PYTHON"] = sys.executable
        run([pwsh, "-NoProfile", "-File", reports / "qcs-owned-acl.ps1", "-Source", source,
             "-Mode", "inspect"], "original-source-acl-before", env, caller)
        record["originalSourceAclBefore"] = json.loads((reports / "original-source-acl-before.stdout.log").read_text(encoding="utf-8-sig"))
        run(["git", "-c", "core.autocrlf=false", "-c", "core.eol=lf", "clone", "--no-hardlinks", "--no-checkout", "--no-tags", source, clone], "clone", env, caller)
        for key, value in (("core.autocrlf", "false"), ("core.eol", "lf")):
            run(["git", "-C", clone, "config", key, value], "git-" + key, env, caller)
        run(["git", "-C", clone, "checkout", "--detach", args.head], "checkout", env, caller)
        require(evidence.source_identity(clone, args.head) == original_identity, "Cloned source identity or lock bytes differ")
        for operation in ("delete", "rename"):
            (clone / ".git" / ("qcs-acl-" + operation + ".tmp")).write_text("Owned disposable ACL probe\n", encoding="utf-8")
        before_clone = snapshot(clone)
        write(reports / "source-before.json", before_clone)
        acl_attempted = True
        run([pwsh, "-NoProfile", "-File", reports / "qcs-owned-acl.ps1", "-Source", clone,
             "-Report", reports / "source-original-acl.json", "-Mode", "deny"], "acl-deny", env, caller)
        record["isolatedSourceAclInventorySha256"] = sha(reports / "source-original-acl.json")
        record["readonlyProbesBefore"] = write_probes(clone)
        write(reports / "readonly-proof.json", record["readonlyProbesBefore"])
        require_readonly_probes(record["readonlyProbesBefore"])
        cached_sources = sorted(path for path in dependency.glob("*-src") if path.is_dir()) + [args.calligra_source.resolve()]
        record["sharedSourceRoots"] = list(map(str, cached_sources))
        before_native = {str(path): snapshot(path) for path in cached_sources}
        write(reports / "shared-source-before.json", before_native)
        common = ["--build-root", external, "--variant", args.variant, "--config", args.config,
                  "--qt-root", args.qt_root, "--model-dir", model_dir, "--gpu-sdk-root", gpu_sdk_root]
        run(developer_command(clone, "doctor", ["doctor", *common]), "doctor", env, caller)
        configure = ["configure", *common,
                     "--cmake-arg=-DQCOPILOTS_SEARCH_DEPENDENCY_ROOT=" + str(dependency),
                     "--cmake-arg=-DFETCHCONTENT_FULLY_DISCONNECTED=ON",
                     "--cmake-arg=-DFETCHCONTENT_SOURCE_DIR_QCS_CALLIGRA=" + str(args.calligra_source.resolve()),
                     "--cmake-arg=-DQCOPILOTS_SEARCH_NATIVE_BINARY_ROOT=" + str(clone_build / "nd"),
                     "--cmake-arg=-DQCOPILOTS_SEARCH_LLAMA_BINARY_DIR=" + str(clone_build / "nd/llama")]
        run(developer_command(clone, "configure", configure), "configure", env, caller)
        run(["cmake", "--build", clone_build, "--config", args.config, "--parallel", args.jobs, "--target", *TARGETS], "subset-build", env, caller)
        clone_dll, clone_binaries = clone_executable_snapshot(clone_build)
        main_dll = main_build / "bin/git2.dll"
        record["originalBinariesBefore"], record["cloneBinariesBefore"] = before_original, clone_binaries
        clone_env = command_env(env, clone_build, clone_dll, main_build=main_build, qt_prefix=args.qt_prefix, nsis_root=nsis_root)
        main_env = command_env(env, main_build, main_dll, main_build=main_build, qt_prefix=args.qt_prefix, nsis_root=nsis_root)
        record["cloneRuntime"] = {"gitDll": str(clone_dll.resolve()), "sha256": sha(clone_dll),
                                  "searchPath": clone_env["PATH"].split(os.pathsep)}
        inv_args = [("clone", clone_build, "^(core|workflow)$", clone_env, TARGETS + ["qcs-tests-workflow"]),
                    ("original", main_build, "^core$", main_env, TARGETS)]
        for label, build, regex, used_env, names in inv_args:
            run(["ctest", "--test-dir", build, "-C", args.config, "--show-only=json-v1", "-L", regex], label + "-inventory", used_env, caller)
            inventory = json.loads((reports / (label + "-inventory.stdout.log")).read_text(encoding="utf-8-sig"))
            require({row["name"] for row in inventory["tests"]} == set(names), "Selected registered CTest set differs: " + label)
            checkout = clone if label == "clone" else source
            configured_test_root = (clone_layout if label == "clone" else main_layout)["runs"]
            for row in inventory["tests"]:
                wrapper_identity = validate_test_command(row.get("command", []), row["name"], checkout,
                                                         build, configured_test_root)
                record.setdefault("testWrapperIdentities", {})[label] = wrapper_identity
            write(reports / (label + "-inventory.json"), inventory)
        # The workflow is registered first and runs much longer than core.
        # Give core a concurrent slot so both checkouts can overlap in QtTest.
        test_a = developer_command(clone, "clone-tests", ["test", *common, "--jobs", "2", "--test-label", "^(core|workflow)$", "--report-dir", reports / "clone-tests"])
        test_b = developer_command(source, "original-tests", ["test", "--build-root", args.build_root.resolve(),
                  "--variant", args.variant, "--config", args.config,
                  "--qt-root", args.qt_root, "--jobs", "1", "--test-label", "^core$", "--report-dir", reports / "original-tests"])
        if args.sentinel_launch_record:
            launch = json.loads(args.sentinel_launch_record.read_text(encoding="utf-8-sig"))
            require(launch.get("preparationComplete") is True,
                    "Sentinel launch has not completed its strict readiness checks")
            sentinel_module_path = Path(__file__).with_name("qcs-portability-sentinel.py")
            sentinel_module_sha = sha(sentinel_module_path)
            observer_spec = importlib.util.spec_from_file_location("qcopilots_portability_sentinel", sentinel_module_path)
            observer_module = importlib.util.module_from_spec(observer_spec)
            observer_spec.loader.exec_module(observer_module)
            sentinel = observer_module.SentinelObserver(args.sentinel_launch_record, reports / "sentinel", args.head, original_producers, args.qt_prefix)
            record["sentinelObserverSha256"] = sentinel_module_sha
            sentinel.begin()
        both = [start(test_a, "clone-test", clone_env, caller), start(test_b, "original-test", main_env, args.launch_cwd)]
        known = [{both[0][0].pid}, {both[1][0].pid}]
        deadline = time.monotonic() + args.command_timeout
        next_guard = time.monotonic()
        while any(item[0].poll() is None for item in both):
            require(time.monotonic() < deadline, "Concurrent subset test timeout")
            if time.monotonic() >= next_guard:
                guard()
                next_guard = time.monotonic() + 2
            state, at = processes.all(), utc()
            alive_tests, alive_ctest = [], []
            for index in range(2):
                changed = True
                while changed:
                    new = {pid for pid, row in state.items() if row["parent"] in known[index]} - known[index]
                    known[index].update(new)
                    changed = bool(new)
                active = {pid: row for pid, row in state.items() if pid in known[index] and processes.contains(both[index][1], pid)}
                for pid, row in active.items():
                    observed[index].setdefault(str(pid), {**row, "firstObservedAt": at})["lastObservedAt"] = at
                    image = processes.image(pid)
                    if image:
                        observed[index][str(pid)]["imagePath"] = image
                expected_bin = clone_build / "bin" if index == 0 else main_build / "bin"
                expected_images = {expected_bin / (name + ".exe") for name in TARGETS}
                alive_tests.append([pid for pid, row in active.items()
                                    if Path(observed[index][str(pid)].get("imagePath", "")) in expected_images])
                alive_ctest.append([pid for pid, row in active.items() if row["name"].casefold() == "ctest.exe"])
            if all(alive_tests):
                overlap.append({"observedAt": at, "qtTestPids": alive_tests, "ctestPids": alive_ctest})
            if sentinel is not None:
                sentinel.sample(processes, alive_tests)
            time.sleep(.02)
        for item in both:
            finish(*item)
        record["observedProcesses"], record["actualQtTestOverlapSamples"] = observed, overlap
        write(reports / "actual-process-overlap.json", {"processes": observed, "simultaneousQtTestSamples": overlap})
        require(bool(overlap), "No actual simultaneous QtTest processes were observed, wrapper overlap alone is insufficient")
        roots = []
        for label, layout, used_env in (("clone", clone_layout, clone_env), ("original", main_layout, main_env)):
            latest = json.loads((layout["reports"] / "latest-test-run.json").read_text(encoding="utf-8"))
            require(Path(latest["directory"]).resolve() == (reports / (label + "-tests")).resolve(), "Runner report was replaced by another invocation")
            roots.append(latest["testRoot"])
            write(reports / (label + "-run-context.json"), latest)
            run([sys.executable, source / ".github/scripts/qcs-ctest-evidence.py", "--junit", reports / (label + "-tests/ctest.xml"),
                 "--platform", "Windows", "--inventory", reports / (label + "-inventory.json"), "--output", reports / (label + "-case-evidence.json"),
                 "--require-complete"], label + "-case-check", used_env, caller)
        require(roots[0] != roots[1], "Two checkout runs unexpectedly shared a writable root")
        record["testRoots"] = roots
        record["readonlyProbesAfter"] = write_probes(clone)
        write(reports / "readonly-proof-after.json", record["readonlyProbesAfter"])
        require_readonly_probes(record["readonlyProbesAfter"])
        _, record["cloneBinariesAfter"] = clone_executable_snapshot(clone_build)
        require(record["cloneBinariesAfter"] == clone_binaries, "Clone binary bytes changed during tests")
        record["testExecutionPassed"] = True
    except BaseException as error:
        record["error"] = type(error).__name__ + ": " + str(error)
    finally:
        cleanup_errors = []
        record["observedProcesses"], record["actualQtTestOverlapSamples"] = observed, overlap
        try:
            write(reports / "actual-process-overlap.json", {"processes": observed, "simultaneousQtTestSamples": overlap})
        except BaseException as error:
            cleanup_errors.append("Cannot save partial overlap: " + str(error))
        record["activeOwnedProcessesBeforeCleanup"] = []
        record["activeOwnedProcessesAfterCleanup"] = []
        for handle in jobs:
            try:
                record["activeOwnedProcessesBeforeCleanup"].append(processes.active(handle))
                record["activeOwnedProcessesAfterCleanup"].append(processes.stop(handle))
            except BaseException as error:
                cleanup_errors.append(str(error))
            finally:
                processes.close(handle)  # owns only child processes started by this script
        for child in children:
            try:
                if child.poll() is None:
                    child.wait(timeout=20)
            except BaseException as error:
                cleanup_errors.append(str(error))
        for stream in streams:
            try:
                stream.close()
            except BaseException as error:
                cleanup_errors.append(str(error))
        record["ownedProcessesExited"] = (not cleanup_errors and all(child.poll() is not None for child in children)
                                          and not any(record["activeOwnedProcessesAfterCleanup"]))
        record["cleanupErrors"] = cleanup_errors
        if sentinel is not None:
            try:
                record["sentinelObservation"] = {"requested": True, **sentinel.finish()}
                record["sentinelObserverUnchanged"] = sha(sentinel_module_path) == sentinel_module_sha
            except BaseException as error:
                record["sentinelObservation"] = {"requested": True, "passed": False, "error": str(error)}
        try:
            if before_clone is not None:
                after_clone = snapshot(clone)
                write(reports / "source-after.json", after_clone)
                record["sourceFilesUnchanged"] = after_clone == before_clone
                record["cloneEndIdentity"] = evidence.source_identity(clone, args.head)
                record["cloneIdentityUnchanged"] = record["cloneEndIdentity"] == original_identity
            if before_original_source is not None:
                after_original_source = snapshot(source)
                write(reports / "original-source-after.json", after_original_source)
                record["originalSourceBytesUnchanged"] = after_original_source == before_original_source
            if before_native is not None:
                after_native = {path: snapshot(Path(path)) for path in before_native}
                write(reports / "shared-source-after.json", after_native)
                record["sharedSourceBytesUnchanged"] = after_native == before_native
            if before_original is not None:
                record["originalBinariesAfter"] = executable_snapshot(main_build / "bin", main_build / "bin/git2.dll")
                record["originalBinariesUnchanged"] = before_original == record["originalBinariesAfter"]
            record["endIdentity"] = evidence.source_identity(source, args.head)
            record["originalSourceIdentityUnchanged"] = record["endIdentity"] == original_identity
            record["mainProducerHashesAfter"] = {name: sha(Path(item["originalPath"])) for name, item in receipt["programs"].items()}
            record["mainProducersUnchanged"] = record["mainProducerHashesAfter"] == original_producers
            record["buildRecordUnchanged"] = sha(args.build_record) == initial_receipt_sha
            record["runnerUnchanged"] = sha(__file__) == runner_sha
            record["processHelperUnchanged"] = (sha(process_helper_path) == process_helper_sha
                                                and sha(preserved_process_helper) == process_helper_sha)
            invocations = record.get("developerInvocations", {})
            record["developerInputsUnchanged"] = (sha(bridge) == bridge_sha and len(invocations) == 4
                and all(sha(item["request"]) == item["sha256"] for item in invocations.values()))
            record["testWrappersUnchanged"] = (len(record.get("testWrapperIdentities", {})) == 2
                and all(sha(item["path"]) == item["sha256"] for item in record["testWrapperIdentities"].values()))
            nsis_after = snapshot(nsis_root)
            write(reports / "nsis-files-after.json", nsis_after)
            record["nsisToolsUnchanged"] = nsis_after == nsis_before and sha(nsis_lock_path) == record["nsisTool"]["lockSha256"]
            observed_acl = subprocess.run([pwsh, "-NoProfile", "-File", str(reports / "qcs-owned-acl.ps1"),
                "-Source", str(source), "-Mode", "inspect"], capture_output=True, cwd=caller, timeout=30)
            (reports / "original-source-acl-after.stdout.log").write_bytes(observed_acl.stdout)
            (reports / "original-source-acl-after.stderr.log").write_bytes(observed_acl.stderr)
            require(observed_acl.returncode == 0, "Cannot inspect original source ACL after run")
            record["originalSourceAclAfter"] = json.loads(observed_acl.stdout.decode("utf-8-sig"))
            record["originalSourceAclUnchanged"] = record["originalSourceAclAfter"] == record["originalSourceAclBefore"]
        except BaseException as error:
            record["finalVerificationError"] = type(error).__name__ + ": " + str(error)
        try:
            if acl_attempted and (reports / "source-original-acl.json").is_file():
                original_acl = reports / "source-original-acl.json"
                original_acl_data = json.loads(original_acl.read_text(encoding="utf-8-sig"))
                original_acl_sha = sha(original_acl)
                require(original_acl_sha == record.get("isolatedSourceAclInventorySha256", original_acl_sha),
                        "Original ACL inventory changed before restoration")
                node_count = len(original_acl_data["nodes"])
                record["isolatedSourceAclInventory"] = {"path": str(original_acl), "sha256": original_acl_sha,
                                                       "nodeCount": node_count}
                if (reports / "source-denied-acl.json").is_file():
                    record["isolatedSourceAclDeny"] = {"path": str(reports / "source-denied-acl.json"),
                                                     "sha256": sha(reports / "source-denied-acl.json")}
                timeout = min(args.command_timeout, acl_restore_timeout(node_count))
                record["aclRestorationTimeoutSeconds"] = timeout
                try:
                    restoration = subprocess.run([pwsh, "-NoProfile", "-File", str(reports / "qcs-owned-acl.ps1"),
                        "-Source", str(clone), "-Report", str(reports / "source-original-acl.json"), "-Mode", "restore"],
                        capture_output=True, cwd=caller, timeout=timeout)
                except subprocess.TimeoutExpired as error:
                    (reports / "acl-restore.stdout.log").write_bytes(error.stdout or b"")
                    (reports / "acl-restore.stderr.log").write_bytes(error.stderr or b"")
                    raise
                (reports / "acl-restore.stdout.log").write_bytes(restoration.stdout)
                (reports / "acl-restore.stderr.log").write_bytes(restoration.stderr)
                record_acl_restoration(record, restoration.returncode, restoration.stdout, clone)
                if (reports / "source-restored-acl.json").is_file():
                    record["isolatedSourceAclRestorationEvidence"] = {
                        "path": str(reports / "source-restored-acl.json"),
                        "sha256": sha(reports / "source-restored-acl.json")}
        except BaseException as error:
            record["isolatedSourceAclRestored"] = False
            record["aclRestorationError"] = str(error)
        record["finishedAt"], record["freeAfterBytes"] = utc(), shutil.disk_usage(root).free
        record["runBytes"] = run_size(root)
        required = ("testExecutionPassed", "ownedProcessesExited", "sourceFilesUnchanged", "cloneIdentityUnchanged", "sharedSourceBytesUnchanged",
                    "originalBinariesUnchanged", "originalSourceBytesUnchanged", "originalSourceIdentityUnchanged", "originalSourceAclUnchanged", "mainProducersUnchanged",
                    "buildRecordUnchanged", "runnerUnchanged", "processHelperUnchanged", "developerInputsUnchanged", "testWrappersUnchanged", "nsisToolsUnchanged", "isolatedSourceAclRestored")
        record["passed"] = all(record.get(key) is True for key in required) and not record.get("error") and not record.get("finalVerificationError")
        if args.sentinel_launch_record:
            record["passed"] = record["passed"] and record["sentinelObservation"].get("passed") is True and record.get("sentinelObserverUnchanged") is True
        write(reports / "qcs-portability-report.json", record)
        print(json.dumps({"passed": record["passed"], "report": str(reports / "qcs-portability-report.json"),
                          "error": record.get("error"), "scope": record["scope"]}, ensure_ascii=False, indent=2))
    return 0 if record["passed"] else 1


if __name__ == "__main__":
    for output in (sys.stdout, sys.stderr):
        if hasattr(output, "reconfigure"):
            output.reconfigure(encoding="utf-8", errors="backslashreplace")
    raise SystemExit(main())
