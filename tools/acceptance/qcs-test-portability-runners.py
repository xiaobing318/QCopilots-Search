#!/usr/bin/env python3
"""Fast contract tests for the opt-in Windows portability acceptance runners."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.dont_write_bytecode = True
DIRECTORY = Path(__file__).resolve().parent
_DIAGNOSTICS: list[str] = []


def queue_diagnostic(prefix: str, payload: dict) -> None:
    # Freeze each record before fixtures are restored or removed.
    _DIAGNOSTICS.append(prefix + " " + json.dumps(payload, ensure_ascii=False))


def flush_diagnostics(stream) -> None:
    if not _DIAGNOSTICS:
        return
    count = len(_DIAGNOSTICS)
    stream.write("\n")
    for line in _DIAGNOSTICS[:count]:
        stream.write(line + "\n")
    stream.flush()
    del _DIAGNOSTICS[:count]


class DiagnosticTestRunner(unittest.TextTestRunner):
    def run(self, test):
        primary = None
        try:
            return super().run(test)
        except BaseException as error:
            primary = error
            raise
        finally:
            try:
                # unittest and diagnostics share one stream after the summary.
                # CTest merging stdout/stderr must not split case lines or JSON.
                flush_diagnostics(self.stream)
            except BaseException as error:
                if primary is None:
                    raise
                if hasattr(primary, "add_note"):
                    primary.add_note("Diagnostic output also failed: " + repr(error))


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), DIRECTORY / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


runner = load("qcs-two-checkout-portability")
observer = load("qcs-portability-sentinel")
launcher = load("qcs-launch-portability-sentinel")
WINDOWS_SKIP_REASON = "Windows native PowerShell and NTFS ACL contract"
WINDOWS_NATIVE_CASES = frozenset(
    "qcs_test_portability_runners.PortabilityContracts." + name for name in (
        "test_windows_powershell_bridge_preserves_developer_arguments",
        "test_windows_acl_restoration_restores_effective_access",
        "test_windows_acl_comparison_rejects_semantic_changes"))

FIXTURE_ACL_SCRIPT = r'''Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Security.AccessControl;
public static class QcsFixtureAcl {
  [DllImport("advapi32.dll", CharSet=CharSet.Unicode)]
  static extern uint SetNamedSecurityInfoW(string name, int type, uint info,
    IntPtr owner, IntPtr group, byte[] dacl, IntPtr sacl);
  [DllImport("advapi32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
  static extern bool SetFileSecurityW(string name, uint info, byte[] descriptor);
  public static void Initialize(string path) {
    var descriptor = new RawSecurityDescriptor("D:PAI(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;FA;;;OW)");
    var dacl = new byte[descriptor.DiscretionaryAcl.BinaryLength];
    descriptor.DiscretionaryAcl.GetBinaryForm(dacl, 0);
    uint error = SetNamedSecurityInfoW(path, 1, 0x80000004,
      IntPtr.Zero, IntPtr.Zero, dacl, IntPtr.Zero);
    if (error != 0) throw new Win32Exception((int)error);
  }
  public static void SimulateLegacyInheritedOrder(string path) {
    // This obsolete API models a descriptor written by an older application.
    // It is deliberately confined to test fixture creation, never recovery.
    var descriptor = new RawSecurityDescriptor("D:(A;ID;FA;;;OW)(A;ID;FA;;;SY)(A;ID;FA;;;BA)");
    var bytes = new byte[descriptor.BinaryLength];
    descriptor.GetBinaryForm(bytes, 0);
    if (!SetFileSecurityW(path, 4, bytes)) throw new Win32Exception(Marshal.GetLastWin32Error());
  }
}
'@
'''


def is_windows_platform_skip(test, reason, platform):
    return platform != "nt" and test.id() in WINDOWS_NATIVE_CASES and reason == WINDOWS_SKIP_REASON


class PortabilityContracts(unittest.TestCase):
    def setUp(self):
        # A caller-supplied root must already exist. An invalid root fails here.
        self.temporary = tempfile.TemporaryDirectory(prefix="qcs-pr-", dir=os.environ.get("QCOPILOTS_SEARCH_TEST_ROOT") or None)
        self.root = Path(self.temporary.name)

    def tearDown(self):
        # TemporaryDirectory.cleanup propagates errors, including retained handles.
        self.temporary.cleanup()

    def powershell(self, script, *arguments):
        executable = shutil.which("pwsh")
        self.assertIsNotNone(executable, "The Windows contract requires PowerShell 7")
        return subprocess.run([executable, "-NoProfile", "-File", str(script), *map(str, arguments)],
                              cwd=self.root, capture_output=True, encoding="utf-8", errors="replace", timeout=45)

    @staticmethod
    def retain_primary_failure(primary, secondary):
        if primary is None:
            return secondary
        note = "Independent fixture cleanup failure: " + repr(secondary)
        if hasattr(primary, "add_note"):
            primary.add_note(note)
        else:
            _DIAGNOSTICS.append(note)
        return primary

    def test_only_exact_non_windows_native_cases_are_not_applicable(self):
        aggregate = load("qcs-test-runners")
        for name in WINDOWS_NATIVE_CASES:
            case = mock.Mock()
            case.id.return_value = name
            for platform, reason, expected in (("posix", WINDOWS_SKIP_REASON, True),
                                               ("nt", WINDOWS_SKIP_REASON, False),
                                               ("posix", "PowerShell missing", False)):
                with self.subTest(case=name, platform=platform, reason=reason):
                    self.assertEqual(is_windows_platform_skip(case, reason, platform), expected)
                    result = mock.Mock(skipped=[(case, reason)])
                    self.assertEqual(aggregate.unexpected_skips(result, platform), [] if expected else result.skipped)
        unknown = mock.Mock()
        unknown.id.return_value = "qcs_test_portability_runners.PortabilityContracts.unknown"
        self.assertFalse(is_windows_platform_skip(unknown, WINDOWS_SKIP_REASON, "posix"))
        result = mock.Mock(skipped=[(unknown, WINDOWS_SKIP_REASON)])
        self.assertEqual(aggregate.unexpected_skips(result, "posix"), result.skipped)

    @unittest.skipUnless(os.name == "nt", WINDOWS_SKIP_REASON)
    def test_windows_powershell_bridge_preserves_developer_arguments(self):
        checkout = self.root / "中文 源码"
        (checkout / "tools").mkdir(parents=True)
        # Execute the actual public entry point's bytes, replacing only its
        # Python consumer with a fixture that records literal argv and exits.
        shutil.copyfile(DIRECTORY.parents[1] / "qcs-dev.ps1", checkout / "qcs-dev.ps1")
        (checkout / "tools/qcs-dev.py").write_text(
            "import json, os, sys\nfrom pathlib import Path\n"
            "Path(os.environ['QCS_ARGV_RESULT']).write_text(json.dumps(sys.argv[1:], ensure_ascii=False), encoding='utf-8')\n"
            "sys.exit(int(os.environ.get('QCS_ARGV_EXIT', '0')))\n", encoding="utf-8")
        bridge = self.root / "bridge.ps1"
        bridge.write_text(runner.POWERSHELL_BRIDGE_SCRIPT, encoding="utf-8")
        result_path = self.root / "argv.json"
        arguments = ["configure", "--build-root", str(self.root / "外置 构建"),
                     "--cmake-arg=-DROOT=" + str(self.root / "中文 空格"),
                     "--cmake-arg=-DROOT:PATH=" + str(self.root / "中文 空格"),
                     "--cmake-arg=-DURL=https://example.test/a=b=c", "--cmake-arg=-DEMPTY=", "",
                     "literal $([IO.File]::WriteAllText('injected.txt','bad')) ` \" quotes", "tail\\"]
        executable = shutil.which("pwsh")
        self.assertIsNotNone(executable, "The Windows contract requires PowerShell 7")
        command = runner.powershell_command(executable, bridge, self.root / "request.json",
                                            checkout / "qcs-dev.ps1", arguments)
        environment = dict(os.environ, QCOPILOTS_SEARCH_PYTHON=sys.executable,
                           QCS_ARGV_RESULT=str(result_path), PYTHONDONTWRITEBYTECODE="1")
        for code in (0, 23):
            with self.subTest(exitCode=code):
                result = subprocess.run(list(map(str, command)), cwd=self.root,
                                        env=dict(environment, QCS_ARGV_EXIT=str(code)), capture_output=True,
                                        encoding="utf-8", errors="replace", timeout=45)
                self.assertEqual(result.returncode, code, result.stdout + result.stderr)
                self.assertEqual(json.loads(result_path.read_text(encoding="utf-8")), arguments)
                self.assertFalse((self.root / "injected.txt").exists())
        # Also pass the original failure shape through the real Python parser
        # and CMake. A language-free project needs no product/dependency build.
        for filename in ("qcs-dev.py", "qcs-path-budget.py"):
            shutil.copyfile(DIRECTORY.parent / filename, checkout / "tools" / filename)
        (checkout / "CMakeLists.txt").write_text(
            "cmake_minimum_required(VERSION 3.28)\nproject(qcs-argument-fixture NONE)\n", encoding="utf-8")
        build, supplied = self.root / "外置 构建", self.root / "中文 values"
        actual_arguments = ["configure", "--build-root", str(build), "--qt-root", str(self.root / "SDK 空格"),
                            "--cmake-arg=-DQCS_TYPED:PATH=" + str(supplied),
                            "--cmake-arg=-DQCS_RAW:STRING=" + str(supplied),
                            "--cmake-arg=-DQCS_URL:STRING=https://example.test/a=b=c",
                            "--cmake-arg=-DQCS_EMPTY:STRING="]
        command = runner.powershell_command(executable, bridge, self.root / "configure.json",
                                            checkout / "qcs-dev.ps1", actual_arguments)
        result = subprocess.run(list(map(str, command)), cwd=self.root, env=environment, capture_output=True,
                                encoding="utf-8", errors="replace", timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        cache = dict(line.split("=", 1) for line in (build / "release/CMakeCache.txt").read_text(encoding="utf-8").splitlines()
                     if line.startswith("QCS_"))
        self.assertEqual(Path(cache["QCS_TYPED:PATH"]), supplied)
        self.assertEqual(cache["QCS_RAW:STRING"], str(supplied))
        self.assertEqual(cache["QCS_URL:STRING"], "https://example.test/a=b=c")
        self.assertEqual(cache["QCS_EMPTY:STRING"], "")

    @unittest.skipUnless(os.name == "nt", WINDOWS_SKIP_REASON)
    def test_windows_acl_comparison_rejects_semantic_changes(self):
        base = "O:SYG:BAD:(A;OICIID;FA;;;SY)(A;OICIID;FA;;;BA)"
        cases = [(base, base, True), (base, base.replace("D:", "D:AI"), True),
                 (base.replace("D:", "D:AI"), base, False)]
        for old, new in (("O:SY", "O:BA"), ("G:BA", "G:SY"), ("D:", "D:P"), ("D:", "D:AR"),
                         ("OICIID", "CIID"), ("OICIID", "OIID"), ("OICIID", "OICI"),
                         ("OICIID", "OICIIOID"), ("OICIID", "OICINPID"), (";FA;", ";FR;"),
                         ("(A;OICIID;FA;;;BA)", ""), ("D:", "D:(D;;FW;;;WD)")):
            cases.append((base, base.replace(old, new, 1), False))
        cases.append((base, "O:SYG:BAD:(A;OICIID;FA;;;BA)(A;OICIID;FA;;;SY)", False))
        cases.append((base, base + "(A;;FR;;;WD)", False))
        cases.extend([("O:SYG:BAD:", "O:SYG:BAD:NO_ACCESS_CONTROL", False),
                      (base + "S:(AU;SA;FR;;;WD)", base + "S:AI(AU;SA;FR;;;WD)", False)])
        runner.write(self.root / "cases.json", cases)
        script = self.root / "compare.ps1"
        script.write_text(runner.ACL_COMPARE_SCRIPT + "\n$rows = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'cases.json') -Raw | ConvertFrom-Json\n"
                          "$results = @($rows | ForEach-Object { Compare-QcsAclSddl $_[0] $_[1] })\n"
                          "ConvertTo-Json -InputObject $results -Depth 5 -Compress\n", encoding="utf-8")
        result = self.powershell(script)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        reports = json.loads(result.stdout)
        self.assertEqual(len(reports), len(cases))
        for case, report in zip(cases, reports):
            with self.subTest(original=case[0], actual=case[1]):
                self.assertEqual(report["semanticMatch"], case[2])
                self.assertEqual(report["originalSddl"], case[0])
                self.assertEqual(report["actualSddl"], case[1])
        self.assertTrue(reports[1]["daclAutoInheritedAdded"])
        self.assertFalse(reports[1]["sddlExactlyEqual"])

    @unittest.skipUnless(os.name == "nt", WINDOWS_SKIP_REASON)
    def test_windows_acl_restoration_restores_effective_access(self):
        scenarios = ("inherited", "explicit-file-allow", "protected-directory",
                     "protected-file", "parent-delete-child", "leaf-delete", "readonly-object",
                     "raw-descriptor-order", "stale-inheritance-order")
        for scenario in scenarios:
            with self.subTest(scenario=scenario):
                self.check_native_acl_scenario(scenario)
        for scenario in ("missing-node", "replaced-node", "unexpected-node"):
            with self.subTest(scenario=scenario):
                self.check_native_acl_recovery_rejects_changed_inventory(scenario)
        with self.subTest(scenario="sharing-conflict"):
            self.check_native_sharing_conflict_is_not_a_permission_denial()

    def probe_fixture(self, parent=None, *, controlled_inheritance=False):
        source = (parent or self.root) / "中文 源码"
        source.mkdir(parents=True)
        if controlled_inheritance:
            # Give the isolated tree a reproducible inherited ACE order before
            # creating children. This does not depend on the runner account's
            # default DACL or alter any directory outside this fixture.
            script = source.parent / "fixture-acl.ps1"
            script.write_text(r'''param([string]$Source)
$ErrorActionPreference = 'Stop'
''' + runner.ACL_READ_SCRIPT + FIXTURE_ACL_SCRIPT + r'''
[QcsFixtureAcl]::Initialize($Source)
[QcsAclReader]::Read([IO.Path]::GetFullPath($Source))
''', encoding="utf-8")
            result = self.powershell(script, "-Source", source)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            # The expected ACE order comes from the explicit native write above,
            # not from reusing the reader to invent an expected descriptor.
            self.assertTrue(result.stdout.strip().endswith(
                'D:PAI(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;FA;;;OW)'), result.stdout)
        (source / "src/core").mkdir(parents=True)
        (source / ".git").mkdir()
        (source / "CMakeLists.txt").write_bytes(b"owned source bytes\n")
        for operation in ("delete", "rename"):
            (source / ".git" / ("qcs-acl-" + operation + ".tmp")).write_bytes(b"owned disposable\n")
        return source

    def check_native_acl_scenario(self, scenario):
        owned = self.root / scenario
        stale_order = scenario == "stale-inheritance-order"
        source = self.probe_fixture(owned, controlled_inheritance=scenario in (
            "raw-descriptor-order", "stale-inheritance-order"))
        readonly = source / ".git/objects/owned-object" if scenario == "readonly-object" else None
        if readonly is not None:
            readonly.parent.mkdir()
            readonly.write_bytes(b"owned readonly Git-like object\n")
        reports = owned / "reports"
        reports.mkdir()
        prepare = reports / "prepare.ps1"
        prepare.write_text(r'''param([string]$Source,[string]$Scenario)
$ErrorActionPreference = 'Stop'
''' + runner.ACL_READ_SCRIPT + FIXTURE_ACL_SCRIPT + r'''
$sid = [Security.Principal.WindowsIdentity]::GetCurrent().User
function Add-Rule($Path, $Rights, $Type) {
  $acl = Get-Acl -LiteralPath $Path
  $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($sid,
    [Security.AccessControl.FileSystemRights]$Rights,
    [Security.AccessControl.AccessControlType]$Type))
  Set-Acl -LiteralPath $Path -AclObject $acl
}
$git = Join-Path $Source '.git'
$files = @((Join-Path $Source 'CMakeLists.txt'),
  (Join-Path $git 'qcs-acl-delete.tmp'), (Join-Path $git 'qcs-acl-rename.tmp'))
function Read-FixtureAcls {
  $items = @((Get-Item -LiteralPath $Source -Force)) + @(Get-ChildItem -LiteralPath $Source -Force -Recurse)
  @($items | ForEach-Object {
    [pscustomobject]@{relativePath=[IO.Path]::GetRelativePath($Source,$_.FullName).Replace('\','/');sddl=[QcsAclReader]::Read($_.FullName)}
  })
}
$before = @(Read-FixtureAcls)
switch ($Scenario) {
  'explicit-file-allow' { foreach ($file in $files) { Add-Rule $file 'Write,Delete' 'Allow' } }
  'protected-directory' {
    foreach ($file in $files[1..2]) { [IO.File]::Delete($file) }
    $acl = Get-Acl -LiteralPath $git
    $acl.SetAccessRuleProtection($true, $false)
    $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($sid,
      [Security.AccessControl.FileSystemRights]::FullControl,
      [Security.AccessControl.InheritanceFlags]'ContainerInherit,ObjectInherit',
      [Security.AccessControl.PropagationFlags]::None,
      [Security.AccessControl.AccessControlType]::Allow))
    Set-Acl -LiteralPath $git -AclObject $acl
    foreach ($file in $files[1..2]) { [IO.File]::WriteAllBytes($file,[Text.Encoding]::UTF8.GetBytes("owned disposable`n")) }
  }
  'raw-descriptor-order' {
    $acl = Get-Acl -LiteralPath $git
    $acl.SetAccessRuleProtection($true, $true)
    $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($sid,
      [Security.AccessControl.FileSystemRights]::FullControl,
      [Security.AccessControl.AccessControlType]::Allow))
    Set-Acl -LiteralPath $git -AclObject $acl
  }
  'stale-inheritance-order' {
    foreach ($file in $files[1..2]) { [QcsFixtureAcl]::SimulateLegacyInheritedOrder($file) }
  }
  'protected-file' {
    $acl = Get-Acl -LiteralPath $files[0]
    $acl.SetAccessRuleProtection($true, $true)
    $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($sid,
      [Security.AccessControl.FileSystemRights]::FullControl,
      [Security.AccessControl.AccessControlType]::Allow))
    Set-Acl -LiteralPath $files[0] -AclObject $acl
  }
  'parent-delete-child' {
    Add-Rule $git 'Write,DeleteSubdirectoriesAndFiles' 'Allow'
    foreach ($file in $files[1..2]) { Add-Rule $file 'Delete' 'Deny' }
  }
  'leaf-delete' {
    Add-Rule $git 'DeleteSubdirectoriesAndFiles' 'Deny'
    foreach ($file in $files[1..2]) { Add-Rule $file 'Write,Delete' 'Allow' }
  }
}
ConvertTo-Json -InputObject @{before=$before;prepared=@(Read-FixtureAcls)} -Depth 5 -Compress
''', encoding="utf-8")
        prepared = self.powershell(prepare, "-Source", source, "-Scenario", scenario)
        self.assertEqual(prepared.returncode, 0, prepared.stdout + prepared.stderr)
        fixture_acls = json.loads(prepared.stdout)
        expected = {item["relativePath"]: item["sddl"] for item in fixture_acls["prepared"]}
        original_mode, readonly_attributes = None, None
        if readonly is not None:
            original_mode = readonly.stat().st_mode
            readonly.chmod(stat.S_IREAD)
            readonly_attributes = readonly.stat().st_file_attributes
            self.assertTrue(readonly_attributes & stat.FILE_ATTRIBUTE_READONLY)
        before = runner.snapshot(source)
        runner.write(owned / ".qcs-owned-run.json", {"source": str(source)})
        script, original = reports / "qcs-owned-acl.ps1", reports / "source-original-acl.json"
        script.write_text(runner.ACL_SCRIPT, encoding="utf-8")
        primary = None
        try:
            denied = self.powershell(script, "-Source", source, "-Report", original, "-Mode", "deny")
            self.assertEqual(denied.returncode, 0, denied.stdout + denied.stderr)
            saved = json.loads(original.read_text(encoding="utf-8-sig"))
            self.assertEqual(saved["schemaVersion"], 2)
            self.assertEqual({item["relativePath"]: item["originalSddl"] for item in saved["nodes"]}, expected)
            applied = json.loads((reports / "source-denied-acl.json").read_text(encoding="utf-8-sig"))
            self.assertEqual(applied["nodeCount"], len(expected))
            self.assertEqual(applied["appliedCount"], len(expected))
            self.assertTrue(all(item["applied"] for item in applied["nodes"]))
            self.assertEqual({item["relativePath"]: item["fileIdentity"] for item in applied["nodes"]},
                             {item["relativePath"]: item["fileIdentity"] for item in saved["nodes"]})
            probes = runner.write_probes(source)
            self.assertEqual(len(probes), 5)
            # Handles used during the ACL transaction must already be closed.
            # Sharing violations (32) are not effective ACL denials (5).
            self.assertTrue(all(item["denied"] and item["winerror"] == 5 for item in probes), probes)
            runner.require_readonly_probes(probes)
        except BaseException as error:
            primary = error
        finally:
            if original.is_file():
                try:
                    restored = self.powershell(script, "-Source", source, "-Report", original, "-Mode", "restore")
                    report = json.loads(restored.stdout)
                    self.assertEqual(report["nodeCount"], len(expected))
                    self.assertEqual({item["relativePath"]: item["originalSddl"] for item in report["nodes"]}, expected)
                    restored_probes = []
                    if stale_order:
                        self.assertNotEqual(restored.returncode, 0)
                        self.assertFalse(report["semanticMatch"])
                        self.assertFalse(report["allNodesRestored"])
                        changed = [item for item in report["nodes"] if not item["semanticMatch"]]
                        self.assertEqual({item["relativePath"] for item in changed},
                                         {".git/qcs-acl-delete.tmp", ".git/qcs-acl-rename.tmp"})
                        self.assertTrue(all(item["originalSddl"].endswith(
                            '(A;ID;FA;;;OW)(A;ID;FA;;;SY)(A;ID;FA;;;BA)') for item in changed))
                        self.assertTrue(all(item["actualSddl"].endswith(
                            '(A;ID;FA;;;SY)(A;ID;FA;;;BA)(A;ID;FA;;;OW)') for item in changed))
                        self.assertTrue(all("security semantics were not restored" in item for item in report["errors"]))
                        diagnostics = {"error": "original readonly failure"}
                        runner.record_acl_restoration(diagnostics, restored.returncode, restored.stdout, source)
                        self.assertEqual(diagnostics["error"], "original readonly failure")
                        self.assertFalse(diagnostics["isolatedSourceAclRestored"])
                        self.assertNotIn("restoredWriteProbes", diagnostics)
                    else:
                        self.assertEqual(restored.returncode, 0, restored.stdout + restored.stderr)
                        self.assertTrue(report["semanticMatch"])
                        self.assertTrue(report["allNodesRestored"])
                        self.assertEqual(report["errors"], [])
                        self.assertTrue(all(item["restored"] and item["semanticMatch"] for item in report["nodes"]))
                        restored_probes = runner.restored_write_probes(source)
                        self.assertEqual(len(restored_probes), 6)
                        runner.require_restored_write_probes(restored_probes)
                    queue_diagnostic("QCS_ACL_SCENARIO", {"scenario": scenario,
                          "expectedFailureObserved": stale_order,
                          "original": saved, "denied": applied, "readonlyProbes": probes,
                          "restoration": report, "restoredWriteProbes": restored_probes,
                          "readonlyFileAttributes": readonly_attributes})
                except BaseException as error:
                    primary = self.retain_primary_failure(primary, error)
                finally:
                    if stale_order:
                        try:
                            # Restore the complete, consistent fixture state
                            # captured before changing the parent's inheritance.
                            # Keep the tested recovery record and its failure
                            # untouched in the original reports directory.
                            cleanup_reports = owned / "fixture-cleanup"
                            cleanup_reports.mkdir()
                            baseline = json.loads(original.read_text(encoding="utf-8-sig"))
                            before_acls = {item["relativePath"]: item["sddl"] for item in fixture_acls["before"]}
                            for item in baseline["nodes"]:
                                item["originalSddl"] = before_acls[item["relativePath"]]
                            baseline["sddl"] = before_acls["."]
                            cleanup_original = cleanup_reports / "source-original-acl.json"
                            runner.write(cleanup_original, baseline)
                            cleanup_script = cleanup_reports / "restore-fixture.ps1"
                            cleanup_script.write_text(runner.ACL_SCRIPT, encoding="utf-8")
                            cleanup = self.powershell(cleanup_script, "-Source", source,
                                                      "-Report", cleanup_original, "-Mode", "restore")
                            self.assertEqual(cleanup.returncode, 0, cleanup.stdout + cleanup.stderr)
                            cleanup_result = json.loads(cleanup.stdout)
                            self.assertTrue(cleanup_result["allNodesRestored"])
                            self.assertTrue(all(item["semanticMatch"] and item["restored"] for item in cleanup_result["nodes"]))
                            cleanup_probes = runner.restored_write_probes(source)
                            runner.require_restored_write_probes(cleanup_probes)
                            queue_diagnostic("QCS_ACL_FIXTURE_CLEANUP", {"scenario": scenario,
                                  "preScenarioRestoration": cleanup_result, "writeProbes": cleanup_probes})
                        except BaseException as error:
                            primary = self.retain_primary_failure(primary, error)
            if readonly is not None:
                try:
                    self.assertEqual(readonly.stat().st_file_attributes, readonly_attributes)
                except BaseException as error:
                    primary = self.retain_primary_failure(primary, error)
                finally:
                    try:
                        readonly.chmod(original_mode)
                    except BaseException as error:
                        primary = self.retain_primary_failure(primary, error)
        if primary is not None:
            raise primary
        self.assertEqual(before, runner.snapshot(source))

    def check_native_acl_recovery_rejects_changed_inventory(self, scenario):
        owned = self.root / scenario
        source = self.probe_fixture(owned)
        reports = owned / "reports"
        reports.mkdir()
        runner.write(owned / ".qcs-owned-run.json", {"source": str(source)})
        script, original = reports / "qcs-owned-acl.ps1", reports / "source-original-acl.json"
        script.write_text(runner.ACL_SCRIPT, encoding="utf-8")
        # First restore every node normally, then alter real owned files. This
        # tests recovery against stale identities without fabricating a manifest
        # or leaving intentionally denied files for TemporaryDirectory cleanup.
        primary = None
        try:
            denied = self.powershell(script, "-Source", source, "-Report", original, "-Mode", "deny")
            self.assertEqual(denied.returncode, 0, denied.stdout + denied.stderr)
        except BaseException as error:
            primary = error
        finally:
            if original.exists():
                try:
                    initial = self.powershell(script, "-Source", source, "-Report", original, "-Mode", "restore")
                    self.assertEqual(initial.returncode, 0, initial.stdout + initial.stderr)
                    self.assertTrue(json.loads(initial.stdout)["allNodesRestored"])
                except BaseException as error:
                    primary = self.retain_primary_failure(primary, error)
        if primary is not None:
            raise primary
        target = source / ".git/qcs-acl-delete.tmp"
        if scenario == "missing-node":
            target.unlink()
            expected_error = "Original ACL node is missing"
        elif scenario == "replaced-node":
            # Keep the original inode alive, so the replacement cannot reuse it.
            target.rename(reports / "retained-original.tmp")
            target.write_bytes(b"different owned replacement\n")
            expected_error = "Original ACL node identity was replaced"
        else:
            (source / "unexpected.txt").write_bytes(b"new owned node\n")
            expected_error = "Unexpected node"
        before = runner.snapshot(source, exclude_git=False)
        result = self.powershell(script, "-Source", source, "-Report", original, "-Mode", "restore")
        self.assertNotEqual(result.returncode, 0)
        restoration = json.loads(result.stdout)
        self.assertFalse(restoration["allNodesRestored"])
        self.assertFalse(restoration["semanticMatch"])
        self.assertTrue(any(expected_error in item for item in restoration["errors"]), restoration)
        failed = [item for item in restoration["nodes"] if not item["restored"]]
        self.assertEqual(len(failed), 0 if scenario == "unexpected-node" else 1)
        self.assertTrue(all(item["semanticMatch"] for item in restoration["nodes"] if item["restored"]))
        record = {"error": "original readonly failure"}
        runner.record_acl_restoration(record, result.returncode, result.stdout, source)
        self.assertEqual(record["error"], "original readonly failure")
        self.assertFalse(record["isolatedSourceAclRestored"])
        self.assertEqual(record["isolatedSourceAclRestoration"], restoration)
        self.assertNotIn("restoredWriteProbes", record)
        self.assertEqual(before, runner.snapshot(source, exclude_git=False))
        queue_diagnostic("QCS_ACL_SCENARIO", {"scenario": scenario,
              "expectedFailureObserved": True, "restoration": restoration, "diagnostics": record})

    def check_native_sharing_conflict_is_not_a_permission_denial(self):
        import ctypes
        from ctypes import wintypes
        source = self.probe_fixture(self.root / "sharing-conflict")
        target = source / "CMakeLists.txt"
        original = target.read_bytes()
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                      ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        handle = kernel.CreateFileW(str(target), 0x80000000, 0, None, 3, 0x80, None)
        self.assertNotEqual(handle, ctypes.c_void_p(-1).value, ctypes.get_last_error())
        primary = None
        try:
            records = runner.write_probes(source)
            self.assertEqual(len(records), 5)
            self.assertEqual(records[1]["winerror"], 32)
            self.assertFalse(records[1]["denied"])
            with self.assertRaises(RuntimeError):
                runner.require_readonly_probes(records)
        except BaseException as error:
            primary = error
        finally:
            if not kernel.CloseHandle(handle):
                primary = self.retain_primary_failure(primary, ctypes.WinError(ctypes.get_last_error()))
        if primary is not None:
            raise primary
        runner.probe_write_access(target, True)
        self.assertEqual(target.read_bytes(), original)
        queue_diagnostic("QCS_ACL_SCENARIO", {"scenario": "sharing-conflict",
              "expectedFailureObserved": True, "readonlyProbes": records,
              "handleReleasedAndFileUnchanged": True})

    def test_writable_probes_keep_every_result_and_fail_closed(self):
        source = self.probe_fixture()
        content = (source / "CMakeLists.txt").read_bytes()
        records = runner.write_probes(source)
        self.assertEqual(len(records), 5)
        self.assertTrue(all(item["denied"] is False for item in records))
        self.assertEqual([item.get("operation") for item in records[-2:]], ["delete", "rename"])
        self.assertFalse((source / ".git/qcs-acl-delete.tmp").exists())
        self.assertFalse((source / ".git/qcs-acl-rename.tmp").exists())
        self.assertTrue((source / ".git/qcs-acl-rename.renamed").is_file())
        self.assertEqual((source / "CMakeLists.txt").read_bytes(), content)
        with self.assertRaisesRegex(RuntimeError, "not effectively read-only"):
            runner.require_readonly_probes(records)
        restored = runner.restored_write_probes(source)
        self.assertEqual(len(restored), 6)
        self.assertTrue(all(item["createWriteReadRenameDelete"] for item in restored[:3]))
        self.assertTrue(restored[3]["existingFileWritable"])
        self.assertTrue(all(not item["originalProbeRestored"] and item["error"] for item in restored[-2:]))
        with self.assertRaisesRegex(RuntimeError, "Not every owned restoration"):
            runner.require_restored_write_probes(restored)
        self.assertEqual((source / "CMakeLists.txt").read_bytes(), content)

    def test_probe_missing_files_are_errors_without_losing_later_results(self):
        source = self.probe_fixture()
        (source / "CMakeLists.txt").unlink()
        (source / ".git/qcs-acl-delete.tmp").unlink()
        records = runner.write_probes(source)
        self.assertEqual(len(records), 5)
        for index in (1, 3):
            self.assertFalse(records[index]["denied"])
            self.assertIn("error", records[index])
        self.assertFalse(records[4]["denied"])
        self.assertTrue((source / ".git/qcs-acl-rename.renamed").exists())
        with self.assertRaises(RuntimeError):
            runner.require_readonly_probes(records)

    def test_probe_keeps_native_error_codes_and_permission_classification(self):
        source = self.probe_fixture()
        before = runner.snapshot(source, exclude_git=False)
        for code in (5, 32, None):
            error = PermissionError("controlled Windows access result")
            error.winerror = code
            # A POSIX PermissionError has no Windows status contract. Windows
            # requires the exact native ACCESS_DENIED code, never None or 32.
            denied = code == 5 if os.name == "nt" else True
            with self.subTest(winerror=code), mock.patch.object(runner, "probe_write_access", side_effect=error), \
                    mock.patch.object(Path, "unlink", side_effect=error), \
                    mock.patch.object(Path, "rename", side_effect=error):
                records = runner.write_probes(source)
            self.assertEqual(len(records), 5)
            self.assertTrue(all(item["winerror"] == code and item["denied"] == denied for item in records))
            if denied:
                runner.require_readonly_probes(records)
            else:
                with self.assertRaises(RuntimeError):
                    runner.require_readonly_probes(records)
        self.assertEqual(before, runner.snapshot(source, exclude_git=False))

    def test_restoration_diagnostics_preserve_primary_error_and_missing_probes(self):
        source = self.probe_fixture()
        records = runner.write_probes(source)
        with self.assertRaises(RuntimeError) as failure:
            runner.require_readonly_probes(records)
        original_error = "RuntimeError: " + str(failure.exception)
        record = {"error": original_error, "readonlyProbesBefore": records}
        # A successful ACL response alone cannot turn already deleted probes
        # into successful restoration. The real temporary files remain absent.
        response = {"semanticMatch": True, "allNodesRestored": True, "nodeCount": 1,
                    "nodes": [{"relativePath": ".", "restored": True, "semanticMatch": True}], "errors": []}
        runner.record_acl_restoration(record, 0, json.dumps(response), source)
        self.assertEqual(record["error"], original_error)
        self.assertEqual(record["readonlyProbesBefore"], records)
        self.assertFalse(record["isolatedSourceAclRestored"])
        self.assertEqual(len(record["restoredWriteProbes"]), 6)
        self.assertIn("Not every owned restoration", record["aclRestorationError"])
        self.assertFalse((source / ".git/qcs-acl-delete.tmp").exists())

    def test_restoration_diagnostics_preserve_failed_node_inventory(self):
        source = self.probe_fixture()
        before = runner.snapshot(source, exclude_git=False)
        node = {"relativePath": ".git/qcs-acl-delete.tmp", "restored": False,
                "semanticMatch": False, "error": "Original ACL node is missing"}
        response = {"semanticMatch": False, "allNodesRestored": False, "nodeCount": 1,
                    "nodes": [node], "errors": [node["error"]]}
        outputs = ((1, json.dumps(response)), (0, json.dumps(response)), (1, "invalid JSON"),
                   (0, "null"), (0, "[]"), (0, '{"nodes": [null]}'))
        for exit_code, output in outputs:
            with self.subTest(exitCode=exit_code, validJson=output.startswith("{")):
                record = {"error": "original readonly failure"}
                runner.record_acl_restoration(record, exit_code, output, source)
                self.assertEqual(record["error"], "original readonly failure")
                self.assertFalse(record["isolatedSourceAclRestored"])
                self.assertIn("aclRestorationError", record)
                self.assertNotIn("restoredWriteProbes", record)
                if output == json.dumps(response):
                    self.assertEqual(record["isolatedSourceAclRestoration"], response)
        self.assertEqual(before, runner.snapshot(source, exclude_git=False))

    def wrapper(self, name="qcs-tests-core", *, workflow=False):
        source, build, tests = self.root / "中文 源码", self.root / "独立 build", self.root / "自定 temp"
        (source / "tools").mkdir(parents=True, exist_ok=True)
        (source / "tools/qcs-test-runner.py").write_text("# owned fixture\n", encoding="utf-8")
        inner = ([sys.executable, str(source / "tools/qcs-test-workflow.py")] if workflow
                 else [str(build / "bin" / (name + ".exe"))])
        command = [sys.executable, str(source / "tools/qcs-test-runner.py"), "--label", name,
                   "--root", str(tests), "--", *inner]
        return command, name, source, build, tests

    def native_build_fixture(self):
        clone = self.root / "克隆 构建/release"
        main = self.root / "主构建"
        qt, nsis, global_dlls = (self.root / name for name in ("Qt SDK", "私有 NSIS", "全局 DLL"))
        for directory in (clone / "bin", clone / "nd/libgit2", main / "release/bin",
                          main / "release/lib", qt / "bin", qt / "plugins", nsis, global_dlls):
            directory.mkdir(parents=True, exist_ok=True)
        for name in runner.TARGETS:
            (clone / "bin" / (name + ".exe")).write_bytes(("independent " + name).encode())
        native = clone / "nd/libgit2/git2.dll"
        native.write_bytes(b"independently built native DLL")
        for directory in (main / "release/bin", global_dlls):
            (directory / "git2.dll").write_bytes(("decoy " + str(directory)).encode("utf-8"))
        (nsis / "makensis.exe").write_bytes(b"locked NSIS lookup fixture, never executed")
        return clone, native, main, qt, nsis, global_dlls

    def test_clone_snapshot_records_exact_native_dll_and_independent_executables(self):
        clone, native, *_ = self.native_build_fixture()
        selected, actual = runner.clone_executable_snapshot(clone)
        self.assertEqual(selected, native)
        expected = [clone / "bin" / (name + ".exe") for name in runner.TARGETS] + [native]
        self.assertEqual(set(actual), {str(path.resolve()) for path in expected})
        for path in expected:
            with self.subTest(file=path.name):
                self.assertEqual(actual[str(path.resolve())],
                                 {"bytes": path.stat().st_size, "sha256": runner.sha(path)})
        native.write_bytes(b"different native DLL bytes")
        _, changed = runner.clone_executable_snapshot(clone)
        self.assertNotEqual(actual[str(native.resolve())], changed[str(native.resolve())])

    def test_clone_snapshot_missing_native_dll_rejects_bin_main_and_path_decoys(self):
        clone, native, main, _, _, global_dlls = self.native_build_fixture()
        native.unlink()
        with mock.patch.dict(os.environ, {"PATH": os.pathsep.join(map(str, (
                clone / "bin", main / "release/bin", global_dlls)))}):
            for with_bin in (False, True):
                with self.subTest(binDecoy=with_bin):
                    if with_bin:
                        (clone / "bin/git2.dll").write_bytes(b"deployed decoy cannot replace the missing native DLL")
                    before = runner.snapshot(self.root)
                    with self.assertRaisesRegex(RuntimeError, "git2.dll"):
                        runner.clone_executable_snapshot(clone)
                    self.assertFalse(native.exists())
                    self.assertEqual(before, runner.snapshot(self.root))

    def test_clone_snapshot_rejects_bin_dll_that_shadows_native(self):
        clone, native, *_ = self.native_build_fixture()
        deployed = clone / "bin/git2.dll"
        for content in (b"different DLL that Windows would load first", native.read_bytes()):
            with self.subTest(identical=content == native.read_bytes()):
                deployed.write_bytes(content)
                before = runner.snapshot(self.root)
                with self.assertRaisesRegex(RuntimeError, "bin/git2.dll.*override"):
                    runner.clone_executable_snapshot(clone)
                self.assertEqual(before, runner.snapshot(self.root))
        deployed.unlink()
        selected, _ = runner.clone_executable_snapshot(clone)
        self.assertEqual(selected, native)

    def test_clone_snapshot_requires_every_independently_built_executable(self):
        clone, _, *_ = self.native_build_fixture()
        for name in runner.TARGETS:
            path = clone / "bin" / (name + ".exe")
            content = path.read_bytes()
            path.unlink()
            try:
                with self.subTest(target=name), self.assertRaisesRegex(RuntimeError, "Missing independently built"):
                    runner.clone_executable_snapshot(clone)
            finally:
                path.write_bytes(content)

    def test_clone_command_environment_uses_selected_native_dll_directory(self):
        clone, native, main, qt, nsis, global_dlls = self.native_build_fixture()
        selected, _ = runner.clone_executable_snapshot(clone)
        base = {"PATH": os.pathsep.join(map(str, (main / "release/bin", main / "release/lib", global_dlls))),
                "UNCHANGED_VALUE": "preserve"}
        before = dict(base)
        # Only the unrelated Windows NSIS command lookup is stubbed. DLL
        # selection, native/decoy files, hashes and PATH construction are real.
        with mock.patch.object(runner.shutil, "which", return_value=str(nsis / "makensis.exe")) as lookup:
            actual = runner.command_env(base, clone, selected, main_build=main, qt_prefix=qt, nsis_root=nsis)
        paths = actual["PATH"].split(os.pathsep)
        self.assertIn(str(native.parent), paths)
        self.assertLess(paths.index(str(native.parent)), paths.index(str(global_dlls)))
        self.assertNotIn(str(main / "release/bin"), paths)
        self.assertNotIn(str(main / "release/lib"), paths)
        self.assertIn(str(clone / "bin"), paths)
        self.assertEqual(actual["QT_PLUGIN_PATH"], str(qt / "plugins"))
        self.assertEqual(actual["NSISDIR"], str(nsis))
        self.assertEqual(actual["QCOPILOTS_SEARCH_PYTHON"], sys.executable)
        self.assertEqual(actual["UNCHANGED_VALUE"], "preserve")
        self.assertEqual(base, before)
        lookup.assert_called_once_with("makensis", path=actual["PATH"])

    def test_owned_wrapper_and_binary_are_bound(self):
        values = self.wrapper()
        identity = runner.validate_test_command(*values)
        self.assertEqual(identity["sha256"], runner.sha(values[2] / "tools/qcs-test-runner.py"))

    def test_workflow_uses_its_checkout_and_python(self):
        values = self.wrapper("qcs-tests-workflow", workflow=True)
        runner.validate_test_command(*values)
        values[0][-1] = str(self.root / "unrelated/qcs-test-workflow.py")
        with self.assertRaisesRegex(RuntimeError, "Workflow inventory"):
            runner.validate_test_command(*values)

    def test_preserve_failed_argument_is_supported(self):
        values = self.wrapper()
        values[0].insert(6, "--keep-failed")
        runner.validate_test_command(*values)

    def test_explicit_no_bytecode_python_option_is_supported(self):
        values = self.wrapper("qcs-tests-workflow", workflow=True)
        values[0].insert(1, "-B")
        values[0].insert(1, "-u")
        values[0].insert(-1, "-B")
        runner.validate_test_command(*values)

    def test_qt_stdout_logging_allowed_but_test_filter_rejected(self):
        values = self.wrapper()
        values[0].extend(["-o", "-,txt"])
        runner.validate_test_command(*values)
        values[0].append("singleTestOnly")
        with self.assertRaisesRegex(RuntimeError, "own checkout binary"):
            runner.validate_test_command(*values)

    def test_wrong_wrapper_root_label_and_binary_fail(self):
        for index, replacement in ((0, "unexpected-python"), (1, "unowned-wrapper.py"),
                                   (3, "qcs-tests-other"), (5, str(self.root / "shared-root")),
                                   (6, "--unexpected"), (7, str(self.root / "wrong.exe"))):
            with self.subTest(argument=index):
                values = self.wrapper()
                values[0][index] = replacement
                with self.assertRaises(RuntimeError):
                    runner.validate_test_command(*values)

    def test_unwrapped_or_truncated_commands_fail(self):
        values = self.wrapper()
        for command in ([], values[0][-1:], values[0][:6], values[0][:7]):
            with self.subTest(command=command), self.assertRaises(RuntimeError):
                runner.validate_test_command(command, *values[1:])

    def test_snapshot_detects_source_edits_and_excludes_git(self):
        (self.root / ".git").mkdir()
        (self.root / ".git/internal").write_text("ignored", encoding="utf-8")
        item = self.root / "中文 document.txt"
        item.write_text("before", encoding="utf-8")
        before = runner.snapshot(self.root)
        self.assertEqual(set(before), {item.name})
        item.write_text("after", encoding="utf-8")
        self.assertNotEqual(before, runner.snapshot(self.root))

    def test_sentinel_environment_removes_test_overrides(self):
        environment = {"SYSTEMROOT": str(self.root), "PATH": "unrelated", "QT_QPA_PLATFORM": "offscreen",
                       "QCOPILOTS_SEARCH_TEST_ROOT": "unrelated", "QCOPILOTS_SEARCH_KEEP_TEST_ARTIFACTS": "1",
                       "QML_IMPORT_PATH": "unrelated"}
        with mock.patch.dict(os.environ, environment, clear=True):
            actual, recorded = launcher.environment(self.root / "app", self.root / "sdk", "packaged")
        self.assertFalse(any(key.startswith(("QT_", "QML", "QCOPILOTS_SEARCH_")) for key in actual))
        self.assertIsNone(recorded["QT_QPA_PLATFORM"])
        self.assertNotIn("unrelated", actual["PATH"])

    def test_missing_system_root_fails_instead_of_using_fixed_drive(self):
        for module in (launcher, observer):
            with self.subTest(module=module.__name__), self.assertRaisesRegex(RuntimeError, "missing"):
                module.windows_root({})
            with self.assertRaisesRegex(RuntimeError, "invalid"):
                module.windows_root({"SYSTEMROOT": "relative-system"})

    def test_sentinel_snapshot_excludes_coordination_but_not_database(self):
        sentinel = observer.SentinelObserver.__new__(observer.SentinelObserver)
        sentinel.profile, sentinel.fixture_paths = self.root, []
        (self.root / "qcs-settings.json").write_text("{}", encoding="utf-8")
        index = self.root / "index"
        index.mkdir()
        for name in ("qcs.sqlite3", "qcs.sqlite3-wal", "qcs.sqlite3-shm", "qcs-progress.json", "qcs.lock"):
            (index / name).write_bytes(name.encode())
        snapshot = sentinel.data_snapshot()
        self.assertEqual({Path(path).name for path in snapshot}, {"qcs-settings.json", "qcs.sqlite3", "qcs.sqlite3-wal"})

    def test_help_and_plan_from_arbitrary_working_directory_are_read_only(self):
        before = runner.snapshot(self.root)
        for name in ("qcs-two-checkout-portability", "qcs-launch-portability-sentinel"):
            result = subprocess.run([sys.executable, "-B", str(DIRECTORY / (name + ".py")), "--help"],
                                    cwd=self.root, capture_output=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", errors="replace"))
        command = [sys.executable, "-B", str(DIRECTORY / "qcs-two-checkout-portability.py"),
                   "--head", "a" * 40, "--source-tree", "b" * 40]
        for flag in ("build-root", "build-record", "run-parent", "qt-root", "qt-prefix", "calligra-source"):
            command.extend(["--" + flag, str(self.root / flag)])
        result = subprocess.run(command, cwd=self.root, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", errors="replace"))
        plan = json.loads(result.stdout)
        self.assertFalse(plan["executeRequested"])
        self.assertEqual(Path(plan["source"]), DIRECTORY.parents[1])
        self.assertTrue(Path(plan["uniqueRunRoot"]).name.startswith("qcs-port-"))
        self.assertEqual(before, runner.snapshot(self.root))

    def test_invalid_candidate_identity_fails_before_writing(self):
        argv = [str(DIRECTORY / "qcs-two-checkout-portability.py"), "--head", "abbreviated", "--source-tree", "b" * 40]
        for flag in ("build-root", "build-record", "run-parent", "qt-root", "qt-prefix", "calligra-source"):
            argv.extend(["--" + flag, str(self.root / flag)])
        with mock.patch.object(sys, "argv", argv), self.assertRaisesRegex(RuntimeError, "full lower-case"):
            runner.main()
        self.assertEqual(list(self.root.iterdir()), [])


if __name__ == "__main__":
    unittest.main(verbosity=2, testRunner=DiagnosticTestRunner)
