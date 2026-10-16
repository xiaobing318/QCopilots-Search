#!/usr/bin/env python3
"""Package completeness and isolation contracts for developer and CI builds."""
from __future__ import annotations

import copy
import importlib.util
import os
from pathlib import Path
import sys
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
