#!/usr/bin/env python3
"""Package completeness and isolation contracts for developer and CI builds."""
from __future__ import annotations

import copy
from contextlib import ExitStack, redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
SOURCE = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("qcs_package_verification", SOURCE / "tools/qcs-verify-package.py")
package = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(package)


def complete_report():
    return {
        "passed": True,
        "verificationScope": "full",
        "workingTreeModified": False,
        "buildIdentity": {"candidateCommit": "a" * 40, "workingTreeModified": False},
        "licenseMaterials": {"passed": True},
        "modelSmoke": {"passed": True},
        "programInventory": {"stable": True},
        "backendVerification": {
            "cpu": {"passed": True},
            "strictGpu": {"passed": True, "hardwareTested": False},
        },
    }


class FullPackageVerificationTests(unittest.TestCase):
    def test_package_model_check_uses_base_and_writes_only_versioned_settings(self):
        configured = os.environ.get("QCOPILOTS_SEARCH_TEST_ROOT") or None
        if configured:
            Path(configured).mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="qcs-package-profile-", dir=configured) as temporary:
            root = Path(temporary)
            archive = root / "qcs-0.3.0-windows-x64-cpu.zip"
            archive.write_bytes(b"synthetic package, never executed")
            profile_base = None
            legacy = b'{"version":3,"legacyMarker":"preserve"}'
            identity = {"variant": "windows-x64-cpu", "packageVariant": "windows-x64-cpu",
                        "toolkitVersion": "", "version": "0.3.0", "attempt": 1}
            def unpack(unused, application):
                nonlocal profile_base
                binary = application / "bin"
                binary.mkdir(parents=True)
                suffix = ".exe" if os.name == "nt" else ""
                (binary / ("qcs-cli" + suffix)).write_bytes(b"synthetic")
                for name in ("msvcp140.dll", "vcruntime140.dll", "vcomp140.dll", "eng.traineddata",
                             "chi_sim.traineddata", "chi_tra.traineddata", "jieba.dict.utf8"):
                    (binary / name).write_bytes(b"synthetic")
                profile_base = application.parent / "profile"
                profile_base.mkdir()
                (profile_base / "qcs-settings.json").write_bytes(legacy)
            def run(command, **kwargs):
                if "--version" in command:
                    return subprocess.CompletedProcess(command, 0, "0.3.0", "")
                self.assertEqual(command[command.index("--profile-dir") + 1], str(profile_base))
                self.assertNotIn("--model", command)
                result = {"valid": True, "directory": str(root), "modelId": "embeddinggemma-2", "dimensions": 768,
                          "artifacts": [{"name": name, "present": True, "bytes": size,
                                         "expectedBytes": size, "sha256": digest}
                                        for name, (size, digest) in package.MODEL_ARTIFACTS.items()]}
                return subprocess.CompletedProcess(command, 0, json.dumps({"ok": True, "result": result}), "")
            def stop_before_inference(unused):
                self.assertEqual((profile_base / "qcs-settings.json").read_bytes(), legacy)
                self.assertEqual(json.loads((profile_base / "profiles/v4/qcs-settings.json").read_text(encoding="utf-8")),
                                 {"version": 4})
                self.assertFalse((profile_base / "profiles/v4/profiles").exists())
                raise RuntimeError("synthetic stop before inference")
            with ExitStack() as stack:
                for name, replacement in {
                    "package_source_identity": lambda: {}, "unpack": unpack,
                    "validate_build_identity": lambda *args: identity,
                    "installed_program_inventory": lambda *args: {},
                    "verify_gpu_payload": lambda *args: {}, "verify_license_materials": lambda *args: {},
                    "verify_program_stability": lambda *args: None, "create_fixtures": stop_before_inference,
                }.items():
                    stack.enter_context(patch.object(package, name, replacement))
                stack.enter_context(patch.object(package.subprocess, "run", side_effect=run))
                start = stack.enter_context(patch.object(package.subprocess, "Popen"))
                stack.enter_context(patch.dict(os.environ, {"QCOPILOTS_SEARCH_TEST_ROOT": str(root / "runs")}))
                stack.enter_context(redirect_stdout(io.StringIO()))
                with self.assertRaisesRegex(RuntimeError, "synthetic stop before inference"):
                    package.verify(archive, root, explicit_report=root / "report", model_dir=root)
                start.assert_not_called()

    def test_uncommitted_package_can_pass_full_functional_verification(self):
        report = complete_report()
        report["workingTreeModified"] = True
        report["buildIdentity"]["workingTreeModified"] = True
        before = copy.deepcopy(report)
        self.assertTrue(package.full_verification_passed(report))
        self.assertEqual(report, before, "Functional validation must preserve the source state for release policy")

    def test_archive_without_git_provenance_can_pass_functional_verification(self):
        report = complete_report()
        report.update(commit=None, workingTreeModified=None, sourceIdentityStatus="unavailable")
        self.assertTrue(package.full_verification_passed(report))

    def test_keyword_only_never_counts_as_full_verification(self):
        report = complete_report()
        report["verificationScope"] = "keyword-only"
        self.assertFalse(package.full_verification_passed(report))

    def test_failed_execution_or_cleanup_invalidates_full_verification(self):
        report = complete_report()
        report.update(passed=False, cleanupError="Could not remove the isolated test directory")
        self.assertFalse(package.full_verification_passed(report))

    def test_runtime_model_license_and_program_failures_remain_blocking(self):
        for field, outcome in (("modelSmoke", "passed"), ("licenseMaterials", "passed"),
                               ("programInventory", "stable")):
            with self.subTest(field=field):
                report = complete_report()
                report[field][outcome] = False
                self.assertFalse(package.full_verification_passed(report))
        for mode in ("cpu", "strictGpu"):
            with self.subTest(mode=mode):
                report = complete_report()
                report["backendVerification"][mode]["passed"] = False
                self.assertFalse(package.full_verification_passed(report))

    def test_explicit_gpu_rejection_preserves_unavailable_hardware_status(self):
        report = complete_report()
        self.assertTrue(package.full_verification_passed(report))
        self.assertFalse(report["backendVerification"]["strictGpu"]["hardwareTested"])

    def test_truthy_incomplete_status_is_not_a_pass(self):
        report = complete_report()
        report["passed"] = "true"
        self.assertFalse(package.full_verification_passed(report))

    def test_environment_cannot_inherit_user_runtime_or_developer_sdk_paths(self):
        inherited = {
            "QCS_HOME": "/real-user-data",
            "QCOPILOTS_SEARCH_PROFILE_DIR": "/legacy-user-data",
            "QCOPILOTS_SEARCH_MODEL_DIR": "/developer-models",
            "QT_PLUGIN_PATH": "/developer-qt",
            "LD_LIBRARY_PATH": "/developer-libraries",
            "PATH": "/developer-bin",
            "QCS_PACKAGE_TEST_MARKER": "preserved",
        }
        # Keep SystemRoot on Windows so the real sanitization path is exercised.
        with patch.dict(os.environ, inherited):
            sanitized = package.isolated_environment()
        for name in inherited.keys() - {"PATH", "QCS_PACKAGE_TEST_MARKER"}:
            with self.subTest(variable=name):
                self.assertNotIn(name, sanitized)
        self.assertNotIn("/developer-bin", sanitized["PATH"])
        self.assertEqual(sanitized["QCS_PACKAGE_TEST_MARKER"], "preserved")

    def test_doc_and_encoding_runtime_must_come_from_the_installed_package(self):
        application = SOURCE.parent / "synthetic-application"
        for name in ("qcs-native-iconv.dll", "libqcs-native-iconv.2.dylib", "libqcs-native-wv2.so.0"):
            with self.subTest(library=name), patch.object(package, "loaded_libraries", return_value=[
                    str(SOURCE.parent / "developer-libraries" / name)]):
                with self.assertRaisesRegex(RuntimeError, "development/system application dependency"):
                    package.verify_library_origins(None, application)

    def test_system_iconv_is_not_confused_with_the_bundled_gnu_wrapper(self):
        application = SOURCE.parent / "synthetic-application"
        owned = application / "lib/libqcs-native-iconv.2.dylib"
        system = SOURCE.parent / "synthetic-system/libiconv.2.dylib"
        with patch.object(package, "loaded_libraries", return_value=[str(owned), str(system)]):
            self.assertEqual(package.verify_library_origins(None, application), [str(owned)])


if __name__ == "__main__":
    unittest.main()
