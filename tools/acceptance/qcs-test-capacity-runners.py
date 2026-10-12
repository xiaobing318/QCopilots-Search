"""Portable unit checks for capacity adapters. No capacity producer is launched."""
from __future__ import annotations

import argparse
from contextlib import redirect_stdout
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("qcs_capacity_preflight", HERE / "qcs-physical-preflight.py")
PREFLIGHT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PREFLIGHT)
PROGRAMS = ("qcs", "qcs-cli", "qcs-service", "qcs-worker", "qcs-benchmark", "qcs-tests-file-capacity")
WINDOWS = os.name == "nt"
POWERSHELL = os.environ.get("QCOPILOTS_SEARCH_POWERSHELL") or shutil.which("pwsh")
CAPABILITIES = {
    "windowsAcceptanceLaunchers": {
        "status": "available" if WINDOWS else "not_applicable",
        "reason": "Real refusal checks apply to the native Windows acceptance entry points",
        "registeredTests": 1 if WINDOWS else 0,
    },
    "powershellGuardContracts": {
        "status": "available" if POWERSHELL else ("unavailable" if WINDOWS else "not_applicable"),
        "reason": "PowerShell 7 is required on Windows, optional for pure guard checks on other platforms",
        "program": POWERSHELL,
        "registeredTests": 4 if WINDOWS or POWERSHELL else 0,
    },
}


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class CapacityPreflightTests(unittest.TestCase):
    def setUp(self):
        # A missing or unwritable explicit root is an error, never a fallback.
        test_root = os.environ.get("QCOPILOTS_SEARCH_TEST_ROOT")
        if test_root and not Path(test_root).is_dir():
            raise RuntimeError("QCOPILOTS_SEARCH_TEST_ROOT must exist")
        self.temp = tempfile.TemporaryDirectory(prefix="qcs-capacity-", dir=test_root)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "源码 with space"
        self.build_root = self.root / "build with space"
        self.qt = self.root / "SDK 中文"
        self.record_root = self.root / "receipts" / "build"
        for path in (self.source / "tools", self.build_root / "release/bin", self.qt / "bin", self.record_root):
            path.mkdir(parents=True)
        (self.qt / "bin/Qt6Core.dll").write_bytes(b"unit runtime fixture")
        self.commit = "a" * 40
        self.identity = {"checkoutCommit": self.commit, "candidateHead": self.commit, "tree": "b" * 40}
        programs = {}
        for name in PROGRAMS:
            program = self.build_root / "release/bin" / (name + ".exe")
            program.write_bytes(name.encode())
            programs[name] = {"originalPath": str(program), "sha256": digest(program)}
        self.build = {"sourceDirectory": str(self.source), "identity": self.identity, "programs": programs}
        self.record = self.record_root / "build.json"
        self.save_build()
        self.args = argparse.Namespace(source=self.source, build_root=self.build_root, qt_prefix=self.qt,
                                       build_record=self.record, expected_commit=self.commit, run_record=None)
        # The existing full evidence validator is tested separately. These mocks
        # isolate adapter path, producer, runtime and candidate binding behavior.
        self.api = SimpleNamespace(
            read_json=lambda path: json.loads(Path(path).read_text(encoding="utf-8")),
            verified_build=mock.Mock(return_value=[]), source_identity=lambda source: dict(self.identity),
            valid_identity=mock.Mock(return_value=[]), digest=digest,
            inspect_local_run=mock.Mock(return_value=[]), validate_physical=mock.Mock(return_value=[]),
            safe_file=lambda root, name: root / name,
        )
        self.module_patch = mock.patch.object(PREFLIGHT.importlib.util, "module_from_spec", return_value=self.api)
        self.spec_patch = mock.patch.object(PREFLIGHT.importlib.util, "spec_from_file_location",
                                           return_value=SimpleNamespace(name="qcs_unit_evidence", loader=SimpleNamespace(exec_module=lambda mod: None)))
        self.module_patch.start()
        self.spec_patch.start()
        self.addCleanup(self.module_patch.stop)
        self.addCleanup(self.spec_patch.stop)

    def save_build(self):
        self.record.write_text(json.dumps(self.build), encoding="utf-8")

    def check(self):
        return PREFLIGHT.run_preflight(self.args)

    def assertRejected(self, text):
        result = self.check()
        self.assertFalse(result["passed"], result)
        self.assertIn(text, "\n".join(result["errors"]))

    def make_physical_record(self):
        directory = self.root / "physical evidence"
        directory.mkdir()
        self.args.run_record = directory / "run.json"
        self.raw_path = directory / "physical.json"
        self.raw = {"createdPhysicalFiles": 100000, "actualRoot": str(directory / "temporary" / "qcs-files-test")}
        self.run = {"buildRecordSha256": digest(self.record), "programName": "qcs-tests-file-capacity", "report": self.raw_path.name}
        self.save_physical_record()

    def save_physical_record(self):
        self.raw_path.write_text(json.dumps(self.raw), encoding="utf-8")
        self.args.run_record.write_text(json.dumps(self.run), encoding="utf-8")

    def test_portable_complete_candidate_readonly(self):
        before = {str(path.relative_to(self.root)) for path in self.root.rglob("*")}
        result = self.check()
        self.assertTrue(result["passed"], result)
        self.assertEqual(set(result["producers"]), set(PROGRAMS))
        self.assertGreater(result["buildEvidenceCopyBudgetBytes"], 0)
        self.assertEqual(before, {str(path.relative_to(self.root)) for path in self.root.rglob("*")})

    def test_short_commit_rejected(self):
        self.args.expected_commit = self.commit[:12]
        self.assertRejected("full immutable Git SHA")

    def test_different_candidate_rejected(self):
        self.args.expected_commit = "c" * 40
        self.assertRejected("does not match ExpectedCommit")

    def test_changed_source_rejected(self):
        self.build["identity"] = {**self.identity, "tree": "d" * 40}
        self.save_build()
        self.assertRejected("differ from the build receipt")

    def test_existing_validator_errors_propagate(self):
        self.api.verified_build.return_value = ["fixture build is unverified"]
        self.assertRejected("fixture build is unverified")

    def test_missing_producer_rejected(self):
        del self.build["programs"]["qcs-worker"]
        self.save_build()
        self.assertRejected("exactly the six")

    def test_extra_producer_rejected(self):
        self.build["programs"]["unapproved"] = {}
        self.save_build()
        self.assertRejected("exactly the six")

    def test_changed_producer_rejected(self):
        Path(self.build["programs"]["qcs-worker"]["originalPath"]).write_bytes(b"changed")
        self.assertRejected("differs from its recorded build")

    def test_outside_producer_rejected(self):
        outside = self.root / "outside.exe"
        outside.write_bytes(b"outside")
        self.build["programs"]["qcs-worker"] = {"originalPath": str(outside), "sha256": digest(outside)}
        self.save_build()
        self.assertRejected("outside the supplied BuildRoot/release")

    def test_missing_qt_rejected(self):
        (self.qt / "bin/Qt6Core.dll").unlink()
        self.assertRejected("does not contain the Windows Qt6Core runtime")

    def test_runtime_hash_recorded(self):
        before = self.check()["runtimeLibraries"]
        (self.qt / "bin/Qt6Core.dll").write_bytes(b"different runtime")
        self.assertNotEqual(before, self.check()["runtimeLibraries"])

    def test_build_under_source_rejected(self):
        self.args.build_root = self.source / "generated"
        self.assertRejected("separate from the source tree")

    def test_source_under_build_rejected(self):
        self.args.build_root = self.root
        self.assertRejected("separate from the source tree")

    def test_missing_build_root_rejected(self):
        self.args.build_root = self.root / "missing"
        self.assertRejected("must already exist")

    def test_redirected_build_root_rejected(self):
        with mock.patch.object(PREFLIGHT, "is_redirect", side_effect=lambda path: path == self.build_root):
            self.assertRejected("ancestors must not redirect")

    def test_redirected_output_root_rejected(self):
        with mock.patch.object(PREFLIGHT, "is_redirect", side_effect=lambda path: path == self.build_root / "reports"):
            self.assertRejected("Output directory redirects")

    def test_build_receipt_at_ancestor_rejected(self):
        self.args.build_record = self.root / "build.json"
        shutil.copyfile(self.record, self.args.build_record)
        self.assertRejected("own evidence directory")

    def test_clean_physical_fixture_passes(self):
        self.make_physical_record()
        result = self.check()
        self.assertTrue(result["passed"], result)
        self.assertTrue(result["actualFixtureRemoved"])
        self.api.validate_physical.assert_called_once_with(self.raw, minimum_files=100000)

    def test_physical_count_cannot_be_lowered(self):
        self.make_physical_record()
        self.raw["createdPhysicalFiles"] = 99999
        self.save_physical_record()
        self.assertRejected("exactly 100000")

    def test_fixture_cleanup_required(self):
        self.make_physical_record()
        Path(self.raw["actualRoot"]).mkdir(parents=True)
        self.assertRejected("still exists")

    def test_fixture_outside_owned_root_rejected(self):
        self.make_physical_record()
        self.raw["actualRoot"] = str(self.root / "other")
        self.save_physical_record()
        self.assertRejected("outside this execution's owned temporary root")

    def test_physical_wrong_build_rejected(self):
        self.make_physical_record()
        self.run["buildRecordSha256"] = "0" * 64
        self.save_physical_record()
        self.assertRejected("different supplied build receipt")

    def test_physical_wrong_producer_rejected(self):
        self.make_physical_record()
        self.run["programName"] = "qcs-worker"
        self.save_physical_record()
        self.assertRejected("approved capacity producer")

    def test_cli_reports_failure_without_traceback(self):
        output = io.StringIO()
        with redirect_stdout(output):
            code = PREFLIGHT.main(["--source", str(self.source), "--build-root", str(self.build_root),
                                   "--qt-prefix", str(self.qt), "--build-record", str(self.record),
                                   "--expected-commit", "invalid"])
        self.assertEqual(code, 1)
        self.assertFalse(json.loads(output.getvalue())["passed"])


class PowerShellContractTests(unittest.TestCase):
    def powershell(self):
        if not POWERSHELL:
            self.fail("PowerShell 7 is required for the registered native Windows guard contracts")
        return POWERSHELL

    def check_guard_contract(self, name):
        pwsh = self.powershell()
        temp_root = os.environ.get("QCOPILOTS_SEARCH_TEST_ROOT")
        if temp_root and not Path(temp_root).is_dir():
            self.fail("QCOPILOTS_SEARCH_TEST_ROOT must exist")
        with tempfile.TemporaryDirectory(prefix="qcs-cwd-", dir=temp_root) as root:
            cwd = Path(root) / "其他 cwd"
            cwd.mkdir()
            result = subprocess.run([pwsh, "-NoProfile", "-NonInteractive", "-File",
                                     str(HERE / f"qcs-test-million-guard-{name}.ps1")],
                                    cwd=cwd, capture_output=True, text=True, encoding="utf-8", timeout=90)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            report = json.loads(result.stdout)
            self.assertTrue(report["passed"], report)
            self.assertFalse(report["guardBodyExecuted"], report)
            self.assertEqual(list(cwd.iterdir()), [])

    if WINDOWS or POWERSHELL:
        def test_array_contract_from_unrelated_cwd(self):
            self.check_guard_contract("array")

        def test_numeric_contract_from_unrelated_cwd(self):
            self.check_guard_contract("contract")

        def test_evidence_contract_from_unrelated_cwd(self):
            self.check_guard_contract("evidence")

        def test_process_contract_from_unrelated_cwd(self):
            self.check_guard_contract("process")

    if WINDOWS:
        def test_invalid_candidate_cannot_launch_or_create_outputs(self):
            pwsh = self.powershell()
            with tempfile.TemporaryDirectory(prefix="qcs-refuse-", dir=os.environ.get("QCOPILOTS_SEARCH_TEST_ROOT")) as root_name:
                root = Path(root_name)
                source, build, qt = (root / part for part in ("source space", "build 中文", "Qt SDK"))
                for directory in (source, build, qt / "bin"):
                    directory.mkdir(parents=True)
                executable = build / "qcs-benchmark.exe"
                executable.write_bytes(b"This is deliberately not an executable and must never launch")
                (qt / "bin/Qt6Core.dll").write_bytes(b"unit runtime placeholder")
                before = {str(path.relative_to(root)) for path in root.rglob("*")}
                common = ["-Source", str(source), "-ExpectedCommit", "not-a-commit", "-BuildRecord", str(build / "missing.json"),
                          "-Python", sys.executable]
                invocations = (
                    ("qcs-run-million.ps1", ["-BenchmarkExe", str(executable), "-ExternalRoot", str(build),
                                             "-QtBin", str(qt / "bin"), "-Mode", "Run"]),
                    ("qcs-run-physical.ps1", ["-BuildRoot", str(build), "-QtPrefix", str(qt), "-Execute"]),
                )
                for name, arguments in invocations:
                    with self.subTest(runner=name):
                        result = subprocess.run([pwsh, "-NoProfile", "-NonInteractive", "-File", str(HERE / name),
                                                 *arguments, *common], cwd=root, capture_output=True, text=True,
                                                encoding="utf-8", timeout=30)
                        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                        self.assertIn("preflight failed", (result.stdout + result.stderr).lower())
                        self.assertEqual(before, {str(path.relative_to(root)) for path in root.rglob("*")})


if __name__ == "__main__":
    unittest.main()
