#!/usr/bin/env python3
"""Regression checks for release evidence that can otherwise falsely pass."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
import xml.etree.ElementTree as ET

sys.dont_write_bytecode = True

spec = importlib.util.spec_from_file_location(
    "qcopilots_search_ci_evidence", Path(__file__).with_name("qcopilots-search-ctest-evidence.py"))
evidence = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evidence)
native_spec = importlib.util.spec_from_file_location(
    "qcopilots_search_native_ci", Path(__file__).with_name("qcopilots-search-native-ci.py"))
native = importlib.util.module_from_spec(native_spec)
native_spec.loader.exec_module(native)


class EvidenceTests(unittest.TestCase):
    def inspect(self, output="Totals: 3 passed, 0 failed, 0 skipped, 0 blacklisted, 1ms\n",
                *, name="qcopilots-search-tests-core", system="Windows", failure=None, raw=False,
                expected=None, include_inventory=True, duplicate=False):
        if not raw and name not in evidence.NON_QT_TESTS:
            output = ("********* Start testing of EvidenceTests *********\n"
                      "PASS   : EvidenceTests::initTestCase()\n"
                      "PASS   : EvidenceTests::runsCase()\n"
                      "PASS   : EvidenceTests::cleanupTestCase()\n" + output +
                      "********* Finished testing of EvidenceTests *********\n")
        # No fixture writes enter the checkout or a developer's application profile.
        with tempfile.TemporaryDirectory(prefix="qcopilots-search-ci-evidence-",
                dir=os.environ.get("QCOPILOTS_SEARCH_TEST_ROOT")) as temporary:
            path = Path(temporary) / "ctest.xml"
            root = ET.Element("testsuite")
            case = ET.SubElement(root, "testcase", name=name)
            ET.SubElement(case, "system-out").text = output
            if failure:
                ET.SubElement(case, failure)
            if duplicate:
                extra = ET.SubElement(root, "testcase", name=name)
                ET.SubElement(extra, "system-out").text = output
            ET.ElementTree(root).write(path, encoding="utf-8")
            inventory = Path(temporary) / "ctest-inventory.json"
            inventory.write_text(json.dumps({"kind": "ctestInfo",
                "tests": [{"name": value} for value in (expected if expected is not None else [name])]}),
                encoding="utf-8")
            return evidence.inspect_junit(path, system, inventory if include_inventory else None)

    def test_complete_qt_cases_pass(self):
        result = self.inspect()
        self.assertTrue(result["releaseComplete"])
        self.assertEqual(result["qtTestTotals"]["passed"], 3)
        self.assertTrue(result["inventoryVerified"])

    def test_subset_report_cannot_claim_full_completion(self):
        self.assertFalse(self.inspect(expected=["qcopilots-search-tests-core",
            "qcopilots-search-tests-file-catalog"])["releaseComplete"])

    def test_extra_report_program_cannot_pass(self):
        self.assertFalse(self.inspect(expected=["qcopilots-search-tests-other"])["releaseComplete"])

    def test_duplicate_report_program_cannot_pass(self):
        self.assertFalse(self.inspect(duplicate=True)["releaseComplete"])

    def test_missing_inventory_cannot_pass(self):
        self.assertFalse(self.inspect(include_inventory=False)["releaseComplete"])

    def test_qskip_with_successful_ctest_exit_is_required_not_run(self):
        result = self.inspect("SKIP   : FileCatalogTest::symbolicLinksCannotEscapeOrLoop() "
                              "This account cannot create symlinks: permission denied\n"
                              "Totals: 3 passed, 0 failed, 1 skipped, 0 blacklisted, 1ms\n",
                              name="qcopilots-search-tests-file-catalog")
        self.assertFalse(result["releaseComplete"])
        self.assertEqual(result["qtTestSkips"][0]["classification"], "requiredNotRun")

    def platform_case(self, system="Windows", reason=None):
        expected = "Non-Windows path keys are case-sensitive. Windows intentionally folds path keys."
        return self.inspect("SKIP   : IndexRepositoryRegressionTest::"
                            "rootFilterRespectsCaseSensitivePlatformKeys() " + (reason or expected) +
                            "\nTotals: 3 passed, 0 failed, 1 skipped, 0 blacklisted, 1ms\n",
                            name="qcopilots-search-tests-index_repository_regression", system=system)

    def test_exact_windows_platform_case_is_not_counted_as_passed(self):
        result = self.platform_case()
        self.assertTrue(result["releaseComplete"])
        self.assertEqual(result["qtTestTotals"]["notApplicable"], 1)
        self.assertEqual(result["qtTestTotals"]["passed"], 3)

    def test_platform_case_is_required_on_linux(self):
        self.assertFalse(self.platform_case("Linux")["releaseComplete"])

    def test_changed_skip_reason_is_not_silently_allowed(self):
        self.assertFalse(self.platform_case(reason="Temporary fixture unavailable")["releaseComplete"])

    def test_missing_qt_stdout_cannot_pass(self):
        self.assertFalse(self.inspect("", raw=True)["releaseComplete"])

    def test_zero_case_summary_cannot_pass(self):
        self.assertFalse(self.inspect("Totals: 0 passed, 0 failed, 0 skipped, 0 blacklisted, 1ms\n",
                                      raw=True)["releaseComplete"])

    def test_summary_without_case_records_cannot_pass(self):
        self.assertFalse(self.inspect("********* Start testing of EvidenceTests *********\n"
            "Totals: 250 passed, 0 failed, 0 skipped, 0 blacklisted, 1ms\n"
            "********* Finished testing of EvidenceTests *********\n", raw=True)["releaseComplete"])

    def test_missing_finish_cannot_pass(self):
        self.assertFalse(self.inspect("********* Start testing of EvidenceTests *********\n"
            "PASS   : EvidenceTests::runsCase()\n"
            "Totals: 1 passed, 0 failed, 0 skipped, 0 blacklisted, 1ms\n", raw=True)["releaseComplete"])

    def test_summary_without_case_skip_evidence_cannot_pass(self):
        self.assertFalse(self.inspect("Totals: 3 passed, 0 failed, 1 skipped, 0 blacklisted, 1ms\n")
                         ["releaseComplete"])

    def test_truncated_middle_cannot_pass_even_with_intact_totals(self):
        self.assertFalse(self.inspect(
            "[This part of the test output was removed since it exceeds the threshold of 1024 bytes.]\n"
            "Totals: 3 passed, 0 failed, 0 skipped, 0 blacklisted, 1ms\n")["releaseComplete"])

    def test_blacklisted_cases_cannot_pass(self):
        self.assertFalse(self.inspect("Totals: 3 passed, 0 failed, 0 skipped, 1 blacklisted, 1ms\n")
                         ["releaseComplete"])

    def test_non_qt_programs_do_not_require_qt_totals(self):
        for name in evidence.NON_QT_TESTS:
            self.assertTrue(self.inspect("success\n", name=name)["releaseComplete"])

    def test_ctest_failure_error_or_skip_cannot_pass(self):
        for kind in ("failure", "error", "skipped"):
            self.assertFalse(self.inspect(failure=kind)["releaseComplete"])


class SignatureEvidenceTests(unittest.TestCase):
    def inspect(self, *, verify_code=0, display_code=0, details="Signature=adhoc\n",
                verify_error="", exists=True, system="Darwin", error=None):
        with tempfile.TemporaryDirectory(prefix="qcopilots-search-signature-",
                dir=os.environ.get("QCOPILOTS_SEARCH_TEST_ROOT")) as temporary:
            stage = Path(temporary)
            if exists:
                (stage / "qcopilots-search.app").mkdir()
            outcomes = [native.subprocess.CompletedProcess([], verify_code, "", verify_error),
                        native.subprocess.CompletedProcess([], display_code, "", details)]
            with mock.patch.object(native.subprocess, "run", side_effect=error or outcomes) as run:
                result = native.signature_evidence(stage, system=system)
                if exists and system == "Darwin" and not error:
                    self.assertEqual(run.call_count, 2)
                    self.assertIn("--strict", run.call_args_list[0].args[0])
                    self.assertIn("--deep", run.call_args_list[0].args[0])
                return result

    def test_verified_ad_hoc_is_not_developer_id_or_notarization(self):
        result = self.inspect()
        self.assertEqual(result["status"], "ad-hoc")
        self.assertTrue(result["verified"])
        self.assertFalse(result["developerIdSignatureVerified"])
        self.assertEqual(result["notarization"], "not-assessed")

    def test_developer_id_requires_successful_verification(self):
        details = "Authority=Developer ID Application: Example (TEAM)\nAuthority=Apple Root CA\n"
        self.assertEqual(self.inspect(details=details)["status"], "developer-id")
        result = self.inspect(details=details, verify_code=1, verify_error="a sealed resource is missing")
        self.assertEqual(result["status"], "invalid")
        self.assertFalse(result["developerIdSignatureVerified"])

    def test_explicit_unsigned_diagnostic(self):
        result = self.inspect(verify_code=1, display_code=1,
                              details="app: code object is not signed at all")
        self.assertEqual(result["status"], "unsigned")
        self.assertFalse(result["verified"])

    def test_missing_bundle_does_not_claim_unsigned_or_signed(self):
        self.assertEqual(self.inspect(exists=False)["status"], "unknown")

    def test_unavailable_or_timed_out_codesign_remains_unknown(self):
        for error in (FileNotFoundError("codesign"), native.subprocess.TimeoutExpired("codesign", 60)):
            self.assertEqual(self.inspect(error=error)["status"], "unknown")

    def test_unavailable_identity_remains_unknown(self):
        self.assertEqual(self.inspect(display_code=1)["status"], "unknown")
        self.assertEqual(self.inspect(details="Identifier=org.example.app")["status"], "unknown")

    def test_other_certificate_is_distinct(self):
        result = self.inspect(details="Authority=Apple Development: Example (TEAM)\n")
        self.assertEqual(result["status"], "signed-other")
        self.assertFalse(result["developerIdSignatureVerified"])

    def test_non_macos_is_uninspected(self):
        result = self.inspect(system="Windows")
        self.assertEqual(result["status"], "unknown")
        self.assertFalse(result["commands"])


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    unittest.main()
