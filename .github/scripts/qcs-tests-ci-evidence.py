#!/usr/bin/env python3
"""Regression checks for complete native tests, packaging and diagnostics."""
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest import mock
import xml.etree.ElementTree as ET

sys.dont_write_bytecode = True

spec = importlib.util.spec_from_file_location(
    "qcopilots_search_ci_evidence", Path(__file__).with_name("qcs-ctest-evidence.py"))
evidence = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evidence)
native_spec = importlib.util.spec_from_file_location(
    "qcopilots_search_native_ci", Path(__file__).with_name("qcs-native-ci.py"))
native = importlib.util.module_from_spec(native_spec)
native_spec.loader.exec_module(native)
deb_spec = importlib.util.spec_from_file_location(
    "qcopilots_search_deb_ci", Path(__file__).with_name("qcs-deb-ci.py"))
deb = importlib.util.module_from_spec(deb_spec)
deb_spec.loader.exec_module(deb)

# Synthetic fixture metadata. Production classification comes only from CTest labels.
FIXTURE_PYTHON_TESTS = {
    "qcs-tests-macos-signing", "qcs-tests-workflow", "qcs-tests-license-materials",
    "qcs-tests-acceptance-runners", "qcs-tests-acceptance-native", "qcs-tests-runner",
    "qcs-tests-path-budget", "qcs-tests-temp-paths", "qcs-tests-ci-evidence",
    "qcs-tests-release-evidence", "qcs-tests-test-policy",
    "qcs-tests-gpu-configuration", "qcs-tests-windows-package",
}
FIXTURE_NON_QT_TESTS = FIXTURE_PYTHON_TESTS | {
    "qcs-tests-semantic-manifest", "qcs-tests-semantic", "qcs-tests-inference-install-layout",
}


def fixture_test(name, extra_labels=()):
    kind = "python-unittest" if name in FIXTURE_PYTHON_TESTS else (
        "program" if name in FIXTURE_NON_QT_TESTS else "qttest")
    return {"name": name, "properties": [{"name": "LABELS", "value": [kind, *extra_labels]}]}


SIGNING_PROGRAM = "qcs-tests-macos-signing"
SIGNING_SKIP_REASON = ("Windows lacks symbolic-link creation privilege (WinError 1314). "
                       "Native CI must run this case.")
SIGNING_SYMLINK_CASES = (
    "test_directory_symlink_is_not_recursed", "test_external_directory_symlink_is_rejected",
    "test_external_file_symlink_is_rejected", "test_internal_library_symlink_is_not_signed_twice",
)
SIGNING_CASES = SIGNING_SYMLINK_CASES + (
    "test_inventory_includes_unreferenced_versioned_library_and_plugin",
    "test_framework_and_outer_app_use_bundle_units",
    "test_framework_with_multiple_concrete_versions_is_rejected_even_with_direct_binary",
    "test_nested_helper_and_framework_precede_enclosing_bundles",
    "test_missing_bundle_executable_fails_before_signing",
    "test_malformed_bundled_macho_is_rejected_before_signing",
    "test_commands_sign_every_unit_in_plan_order_without_deep_signing",
    "test_each_unit_has_strict_verification_and_final_app_has_deep",
)
WORKFLOW_PROGRAM = "qcs-tests-workflow"
WORKFLOW_SKIP_REASON = "NSIS is native to Windows"
WORKFLOW_NSIS_CASES = (
    "test_nsis_compiles_recursive_unicode_payload_beyond_max_path",
    "test_installer_guard_rejects_existing_start_menu_before_execution",
)
WORKFLOW_LINK_CASES = (
    "test_clean_unlinks_internal_framework_and_versioned_library_links",
    "test_clean_rejects_external_symlink_without_deleting_any_target",
    "test_clean_rejects_linked_top_level_target",
)
WORKFLOW_LINK_SKIP_REASON = "Windows symbolic-link creation privilege unavailable (WinError 1314), native macOS/Linux execution required"
WORKFLOW_JUNCTION_CASE = "test_clean_rejects_windows_junction_inside_generated_tree"
WORKFLOW_JUNCTION_SKIP_REASON = "Windows junction semantics require Windows"
WORKFLOW_CASES = ("test_clean_preserves_unowned_directory",
                  "test_release_package_requires_models_before_launching", *WORKFLOW_NSIS_CASES,
                  *WORKFLOW_LINK_CASES, WORKFLOW_JUNCTION_CASE)


def signing_verbose(rows=None, *, run=None, footer=None):
    """Synthetic unittest verbose output, never a signing execution report."""
    if rows is None:
        rows = [(name, "ok") for name in SIGNING_CASES]
    if footer is None:
        skipped = sum(status.startswith("skipped ") for _, status in rows)
        footer = f"OK (skipped={skipped})" if skipped else "OK"
    return ("\n".join(f"{name} (__main__.SigningTests.{name}) ... {status}" for name, status in rows)
            + f"\n\n----------------------------------------------------------------------\nRan {len(rows) if run is None else run} tests in 0.012s\n\n{footer}\n")


def workflow_verbose(rows=None):
    """Match observed workflow verbose records with interleaved JSON stdout."""
    rows = [(name, "ok") for name in WORKFLOW_CASES] if rows is None else rows
    lines = []
    for name, status in rows:
        if name == "test_release_package_requires_models_before_launching":
            # The real workflow-nsis-final.log prints this expected failure
            # report between complete unittest records. Paths here are synthetic.
            lines.append(json.dumps({"package": None, "installedRoot": "synthetic 安装 空格",
                "runId": "synthetic-workflow-parser-input", "workingDirectory": "synthetic 工作目录",
                "checks": [], "passed": False, "verificationScope": "release", "realModelRequired": True,
                "releaseComplete": False, "commit": None, "workingTreeModified": None,
                "profileDirectory": "synthetic 独立配置", "applicationRoot": "synthetic 安装 空格",
                "error": "Release package verification requires --model-dir with both locked GGUF files. "
                         "Use --keyword-only only for an explicitly partial developer check."},
                ensure_ascii=False, indent=2))
        lines.append(f"{name} (__main__.WorkflowTests.{name}) ... {status}")
    skipped = sum(status.startswith("skipped ") for _, status in rows)
    footer = f"OK (skipped={skipped})" if skipped else "OK"
    return "\n".join(lines) + f"\n\n----------------------------------------------------------------------\nRan {len(rows)} tests in 0.012s\n\n{footer}\n"


class EvidenceTests(unittest.TestCase):
    def inspect(self, output="Totals: 3 passed, 0 failed, 0 skipped, 0 blacklisted, 1ms\n",
                *, name="qcs-tests-core", system="Windows", failure=None, raw=False,
                expected=None, include_inventory=True, duplicate=False, status=None,
                inventory_entries=None):
        if not raw and name not in FIXTURE_NON_QT_TESTS:
            output = ("********* Start testing of EvidenceTests *********\n"
                      "PASS   : EvidenceTests::initTestCase()\n"
                      "PASS   : EvidenceTests::runsCase()\n"
                      "PASS   : EvidenceTests::cleanupTestCase()\n" + output +
                      "********* Finished testing of EvidenceTests *********\n")
        # No fixture writes enter the checkout or a developer's application profile.
        with tempfile.TemporaryDirectory(prefix="qcs-ci-evidence-",
                dir=os.environ.get("QCOPILOTS_SEARCH_TEST_ROOT") or None) as temporary:
            path = Path(temporary) / "ctest.xml"
            root = ET.Element("testsuite")
            case = ET.SubElement(root, "testcase", name=name)
            if status is not None:
                case.set("status", status)
            ET.SubElement(case, "system-out").text = output
            if failure:
                ET.SubElement(case, failure)
            if duplicate:
                extra = ET.SubElement(root, "testcase", name=name)
                ET.SubElement(extra, "system-out").text = output
            ET.ElementTree(root).write(path, encoding="utf-8")
            inventory = Path(temporary) / "ctest-inventory.json"
            inventory.write_text(json.dumps({"kind": "ctestInfo",
                "tests": inventory_entries if inventory_entries is not None else
                    [fixture_test(value) for value in (expected if expected is not None else [name])]}),
                encoding="utf-8")
            return evidence.inspect_junit(path, system, inventory if include_inventory else None)

    def test_complete_qt_cases_pass(self):
        result = self.inspect()
        self.assertTrue(result["releaseComplete"])
        self.assertTrue(result["testsComplete"])
        self.assertEqual(result["qtTestTotals"]["passed"], 3)
        self.assertTrue(result["inventoryVerified"])

    def test_new_python_program_uses_inventory_kind_without_a_name_registry(self):
        name = "qcs-tests-new-tool-without-parser-change"
        output = "test_new (__main__.NewTool.test_new) ... ok\nRan 1 test in 0.001s\n\nOK\n"
        entry = {"name": name, "properties": [{"name": "LABELS", "value": ["python-unittest"]}]}
        result = self.inspect(output, name=name, raw=True, inventory_entries=[entry])
        self.assertTrue(result["testsComplete"], result["errors"])
        self.assertEqual(result["pythonTestTotals"]["passed"], 1)

    def test_new_python_program_cannot_pass_with_summary_only(self):
        name = "qcs-tests-new-tool-without-parser-change"
        entry = {"name": name, "properties": [{"name": "LABELS", "value": ["python-unittest"]}]}
        result = self.inspect("Ran 2 tests in 0.001s\n\nOK\n", name=name, raw=True, inventory_entries=[entry])
        self.assertFalse(result["testsComplete"])

    def test_missing_or_conflicting_evidence_kind_cannot_pass(self):
        for labels in ([], ["unit"], ["qttest", "python-unittest"]):
            entry = {"name": "qcs-tests-core", "properties": [{"name": "LABELS", "value": labels}]}
            self.assertFalse(self.inspect(inventory_entries=[entry])["testsComplete"])

    def test_all_registered_programs_are_required_regardless_of_labels(self):
        entries = [fixture_test("qcs-tests-core"), fixture_test("qcs-tests-numerical-fixture", ["model"])]
        result = self.inspect(inventory_entries=entries)
        self.assertFalse(result["testsComplete"])
        self.assertEqual(result["registeredPrograms"], ["qcs-tests-core", "qcs-tests-numerical-fixture"])
        self.assertEqual(result["expectedPrograms"], result["registeredPrograms"])
        self.assertTrue(any("missing=['qcs-tests-numerical-fixture']" in error for error in result["errors"]))

    def test_disabled_registration_cannot_be_excluded_from_completion(self):
        entry = fixture_test("qcs-tests-core")
        entry["properties"].append({"name": "DISABLED", "value": True})
        self.assertFalse(self.inspect(inventory_entries=[entry])["testsComplete"])

    def test_subset_report_cannot_claim_full_completion(self):
        self.assertFalse(self.inspect(expected=["qcs-tests-core",
            "qcs-tests-file-catalog"])["releaseComplete"])

    def test_extra_report_program_cannot_pass(self):
        self.assertFalse(self.inspect(expected=["qcs-tests-other"])["releaseComplete"])

    def test_duplicate_report_program_cannot_pass(self):
        self.assertFalse(self.inspect(duplicate=True)["releaseComplete"])

    def test_missing_inventory_cannot_pass(self):
        self.assertFalse(self.inspect(include_inventory=False)["releaseComplete"])

    def test_qskip_with_successful_ctest_exit_is_required_not_run(self):
        result = self.inspect("SKIP   : FileCatalogTest::symbolicLinksCannotEscapeOrLoop() "
                              "This account cannot create symlinks: permission denied\n"
                              "Totals: 3 passed, 0 failed, 1 skipped, 0 blacklisted, 1ms\n",
                              name="qcs-tests-file-catalog")
        self.assertFalse(result["releaseComplete"])
        self.assertEqual(result["qtTestSkips"][0]["classification"], "requiredNotRun")

    def platform_case(self, system="Windows", reason=None):
        expected = "Non-Windows path keys are case-sensitive. Windows intentionally folds path keys."
        return self.inspect("SKIP   : IndexRepositoryRegressionTest::"
                            "rootFilterRespectsCaseSensitivePlatformKeys() " + (reason or expected) +
                            "\nTotals: 3 passed, 0 failed, 1 skipped, 0 blacklisted, 1ms\n",
                            name="qcs-tests-index_repository_regression", system=system)

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
        for name in FIXTURE_NON_QT_TESTS:
            if name in FIXTURE_PYTHON_TESTS:
                continue
            self.assertTrue(self.inspect("success\n", name=name)["releaseComplete"])

    def test_python_inference_install_layout_is_not_a_qt_test(self):
        self.assertTrue(self.inspect("Ran 8 tests\nOK\n",
            name="qcs-tests-inference-install-layout")["releaseComplete"])
        self.assertFalse(self.inspect("success\n",
            name="qcs-tests-backend-layout")["releaseComplete"])

    def test_python_macos_signing_is_not_a_qt_test(self):
        result = self.inspect(signing_verbose(), name=SIGNING_PROGRAM, raw=True)
        self.assertTrue(result["releaseComplete"])
        self.assertFalse(result["ctestPrograms"][0]["qtTest"])
        self.assertTrue(result["ctestPrograms"][0]["pythonTest"])

    def test_ctest_failure_error_or_skip_cannot_pass(self):
        for kind in ("failure", "error", "skipped"):
            self.assertFalse(self.inspect(failure=kind)["releaseComplete"])


class CompleteInventoryEvidenceTests(unittest.TestCase):
    def inspect(self, *args, **kwargs):
        return EvidenceTests.inspect(self, *args, **kwargs)

    def test_explicit_disabled_or_notrun_nonqt_case_without_skipped_node_fails(self):
        # CTest can return zero for disabled programs without a skipped element.
        for status in ("disabled", "notrun", "skipped", "", "unknown"):
            with self.subTest(status=status):
                result = self.inspect("", name="qcs-tests-semantic-manifest", raw=True, status=status)
                self.assertFalse(result["testsComplete"])
                self.assertFalse(result["releaseComplete"])
                self.assertTrue(any("did not run" in error for error in result["errors"]))

    def test_explicit_run_and_legacy_absent_status_remain_supported(self):
        for status in (None, "run"):
            with self.subTest(status=status):
                result = self.inspect("Manifest passed\n", name="qcs-tests-semantic-manifest", raw=True, status=status)
                self.assertTrue(result["testsComplete"], result["errors"])
                self.assertTrue(result["releaseComplete"])

    def test_inventory_must_be_exact_and_nonempty(self):
        for change in ({"expected": ["qcs-tests-core", "qcs-tests-file-catalog"]},
                       {"expected": ["qcs-tests-other"]}, {"expected": []},
                       {"expected": ["qcs-tests-core", "qcs-tests-core"]},
                       {"duplicate": True}, {"include_inventory": False}):
            with self.subTest(change=change):
                result = self.inspect(**change)
                self.assertFalse(result["testsComplete"])
                self.assertFalse(result["releaseComplete"])
                self.assertTrue(result["errors"])

    def test_python_failure_and_unapproved_skip_remain_failures(self):
        for status, footer in (("FAIL", "FAILED (failures=1)"),
                               ("skipped 'Missing required fixture'", "OK (skipped=1)")):
            with self.subTest(status=status):
                result = self.inspect(signing_verbose([("test_required_case", status)], footer=footer),
                                      name=SIGNING_PROGRAM, raw=True)
                self.assertFalse(result["testsComplete"])
                self.assertFalse(result["releaseComplete"])
                self.assertTrue(result["errors"])

    def invoke(self, *options, failure=False):
        with tempfile.TemporaryDirectory(prefix="qcs-ci-complete-",
                dir=os.environ.get("QCOPILOTS_SEARCH_TEST_ROOT") or None) as temporary:
            directory = Path(temporary)
            junit, inventory, output = (directory / name for name in ("ctest.xml", "inventory.json", "result.json"))
            root = ET.Element("testsuite")
            case = ET.SubElement(root, "testcase", name="qcs-tests-semantic-manifest")
            ET.SubElement(case, "system-out").text = "Manifest validation passed\n"
            if failure:
                ET.SubElement(case, "failure").text = "Manifest mismatch"
            ET.ElementTree(root).write(junit, encoding="utf-8")
            inventory.write_text(json.dumps({"kind": "ctestInfo", "tests": [
                fixture_test("qcs-tests-semantic-manifest")]}), encoding="utf-8")
            argv = [str(Path(evidence.__file__)), "--junit", str(junit), "--inventory", str(inventory),
                    "--platform", "Windows", "--output", str(output), *options]
            with mock.patch.object(sys, "argv", argv), mock.patch("sys.stdout", new_callable=io.StringIO):
                code = evidence.main()
            return code, json.loads(output.read_text(encoding="utf-8"))

    def test_cli_complete_gate_passes_complete_inventory(self):
        code, report = self.invoke("--require-complete")
        self.assertEqual(code, 0)
        self.assertTrue(report["releaseComplete"])
        self.assertTrue(report["testsComplete"])

    def test_cli_complete_gate_fails_for_incomplete_tests(self):
        code, report = self.invoke("--require-complete", failure=True)
        self.assertEqual(code, 1)
        self.assertFalse(report["testsComplete"])
        self.assertFalse(report["releaseComplete"])

    def test_diagnostic_mode_records_failure_without_changing_process_status(self):
        code, report = self.invoke(failure=True)
        self.assertEqual(code, 0)
        self.assertFalse(report["testsComplete"])
        self.assertTrue(report["errors"])


class PythonSigningEvidenceTests(unittest.TestCase):
    def inspect(self, output=None, *, system="Windows"):
        return EvidenceTests.inspect(self, signing_verbose() if output is None else output,
                                     name=SIGNING_PROGRAM, system=system, raw=True)

    def skipped_rows(self, *, reason=SIGNING_SKIP_REASON):
        return [(name, "skipped " + repr(reason) if name in SIGNING_SYMLINK_CASES else "ok")
                for name in SIGNING_CASES]

    def test_python_310_and_311_verbose_ids_have_identical_normalized_cases(self):
        modern = signing_verbose()
        legacy = modern
        for name in SIGNING_CASES:
            legacy = legacy.replace(f"(__main__.SigningTests.{name})", "(__main__.SigningTests)")
        modern_result, legacy_result = self.inspect(modern), self.inspect(legacy)
        self.assertTrue(modern_result["releaseComplete"], modern_result["errors"])
        self.assertTrue(legacy_result["releaseComplete"], legacy_result["errors"])
        self.assertEqual(modern_result["ctestPrograms"][0]["cases"],
                         legacy_result["ctestPrograms"][0]["cases"])
        self.assertEqual(modern_result["pythonTestTotals"], legacy_result["pythonTestTotals"])

    def test_mismatched_explicit_method_cannot_be_treated_as_legacy_class(self):
        original = signing_verbose()
        output = original.replace(f"(__main__.SigningTests.{SIGNING_CASES[0]})",
                                  "(__main__.SigningTests.test_forged_identity)", 1)
        result = self.inspect(output)
        self.assertFalse(result["releaseComplete"])
        self.assertTrue(any("method and case identity disagree" in error for error in result["errors"]))

    def test_mixed_legacy_and_modern_duplicate_ids_remain_duplicates(self):
        rows = [(name, "ok") for name in SIGNING_CASES]
        rows[-1] = rows[0]
        output = signing_verbose(rows).replace(
            f"(__main__.SigningTests.{rows[0][0]})", "(__main__.SigningTests)", 1)
        result = self.inspect(output)
        self.assertFalse(result["releaseComplete"])
        self.assertTrue(any("duplicate case records" in error for error in result["errors"]))

    def test_legacy_ids_still_apply_exact_skip_identity_and_reason(self):
        output = signing_verbose(self.skipped_rows())
        for name in SIGNING_CASES:
            output = output.replace(f"(__main__.SigningTests.{name})", "(__main__.SigningTests)")
        result = self.inspect(output)
        self.assertTrue(result["releaseComplete"], result["errors"])
        self.assertEqual(result["pythonTestTotals"]["notApplicable"], 4)
        self.assertEqual(result["pythonTestTotals"]["passed"], 8)

    def test_complete_verbose_pass_records_have_independent_python_totals(self):
        expected = {"run": 12, "passed": 12, "failed": 0, "skipped": 0, "notApplicable": 0}
        for system in ("Windows", "Darwin", "Linux"):
            with self.subTest(system=system):
                result = self.inspect(system=system)
                self.assertTrue(result["releaseComplete"])
                self.assertEqual(result["pythonTestTotals"], expected)
                self.assertEqual(result["ctestPrograms"][0]["pythonTestTotals"], expected)
                self.assertEqual(len(result["ctestPrograms"][0]["cases"]), 12)
                self.assertEqual(result["qtTestTotals"]["passed"], 0)

    def test_four_exact_windows_symlink_skips_are_not_counted_as_passed(self):
        result = self.inspect(signing_verbose(self.skipped_rows()))
        self.assertTrue(result["releaseComplete"])
        self.assertEqual(result["pythonTestTotals"],
                         {"run": 12, "passed": 8, "failed": 0, "skipped": 4, "notApplicable": 4})
        self.assertEqual(len(result["pythonTestSkips"]), 4)
        self.assertEqual({row["case"] for row in result["pythonTestSkips"]},
                         {"__main__.SigningTests." + name for name in SIGNING_SYMLINK_CASES})
        for row in result["pythonTestSkips"]:
            self.assertEqual(row["program"], SIGNING_PROGRAM)
            self.assertEqual(row["reason"], SIGNING_SKIP_REASON)
            self.assertEqual(row["classification"], "notApplicable")

    def test_unknown_windows_skip_id_remains_required_not_run(self):
        rows = self.skipped_rows()
        rows[0] = ("test_unknown_symlink_operation", rows[0][1])
        result = self.inspect(signing_verbose(rows))
        self.assertFalse(result["releaseComplete"])
        self.assertEqual(result["pythonTestSkips"][0]["classification"], "requiredNotRun")

    def test_changed_windows_skip_reason_is_not_allowlisted(self):
        result = self.inspect(signing_verbose(self.skipped_rows(reason="Permission denied")))
        self.assertFalse(result["releaseComplete"])
        self.assertTrue(all(row["classification"] == "requiredNotRun" for row in result["pythonTestSkips"]))

    def test_windows_skip_exception_cannot_apply_on_native_macos_or_linux(self):
        for system in ("Darwin", "Linux"):
            result = self.inspect(signing_verbose(self.skipped_rows()), system=system)
            self.assertFalse(result["releaseComplete"])
            self.assertEqual(result["pythonTestTotals"]["notApplicable"], 0)
            self.assertTrue(all(row["classification"] == "requiredNotRun" for row in result["pythonTestSkips"]))

    def test_success_text_or_summary_without_records_cannot_pass(self):
        for output in ("success\n", "Ran 12 tests in 0.012s\n\nOK\n", ""):
            with self.subTest(output=output):
                self.assertFalse(self.inspect(output)["releaseComplete"])

    def test_verbose_records_require_one_complete_summary(self):
        complete = signing_verbose()
        for output in (complete.partition("\n\n----------------------------------------------------------------------")[0],
                       complete.replace("\nOK\n", "\n"), complete + "\nRan 12 tests in 0.012s\n\nOK\n"):
            self.assertFalse(self.inspect(output)["releaseComplete"])

    def test_run_count_and_skipped_summary_must_match_verbose_records(self):
        self.assertFalse(self.inspect(signing_verbose(run=13))["releaseComplete"])
        self.assertFalse(self.inspect(signing_verbose(self.skipped_rows(), footer="OK (skipped=3)"))["releaseComplete"])

    def test_duplicate_verbose_case_is_not_an_additional_executed_case(self):
        rows = [(name, "ok") for name in SIGNING_CASES]
        rows[-1] = rows[0]
        self.assertFalse(self.inspect(signing_verbose(rows))["releaseComplete"])

    def test_zero_executed_cases_cannot_pass_even_with_allowlisted_skips(self):
        rows = [(name, "skipped " + repr(SIGNING_SKIP_REASON)) for name in SIGNING_SYMLINK_CASES]
        self.assertFalse(self.inspect(signing_verbose(rows))["releaseComplete"])
        self.assertFalse(self.inspect(signing_verbose([]))["releaseComplete"])

    def test_fail_and_error_records_fail_despite_ctest_success(self):
        for status, footer in (("FAIL", "FAILED (failures=1)"), ("ERROR", "FAILED (errors=1)")):
            rows = [(name, "ok") for name in SIGNING_CASES]
            rows[0] = (rows[0][0], status)
            result = self.inspect(signing_verbose(rows, footer=footer))
            self.assertFalse(result["releaseComplete"])
            self.assertEqual(result["pythonTestTotals"]["failed"], 1)
            self.assertFalse(self.inspect(signing_verbose(rows, footer="OK"))["releaseComplete"])

    def test_expected_failure_and_unexpected_success_are_not_release_passes(self):
        for status, footer in (("expected failure", "OK (expected failures=1)"),
                               ("unexpected success", "FAILED (unexpected successes=1)")):
            rows = [(name, "ok") for name in SIGNING_CASES]
            rows[0] = (rows[0][0], status)
            self.assertFalse(self.inspect(signing_verbose(rows, footer=footer))["releaseComplete"])


class PythonCaseRegistrationTests(unittest.TestCase):
    def document(self):
        return {"suite": "all", "selected": ["__main__.SigningTests." + name
                for name in SIGNING_CASES if name not in SIGNING_SYMLINK_CASES],
                "notApplicable": [{"case": "__main__.SigningTests." + name,
                    "reason": evidence.SIGNING_REGISTRATION_REASON} for name in SIGNING_SYMLINK_CASES]}

    def inspect(self, document=None, *, registration=None, legacy=False, system="Windows"):
        selected = [name for name in SIGNING_CASES if name not in SIGNING_SYMLINK_CASES]
        output = signing_verbose([(name, "ok") for name in selected])
        if legacy:
            for name in selected:
                output = output.replace(f"(__main__.SigningTests.{name})", "(__main__.SigningTests)")
        line = registration if registration is not None else json.dumps(
            self.document() if document is None else document)
        output = evidence.REGISTRATION_PREFIX + " " + line + "\n" + output
        return EvidenceTests.inspect(self, output, name=SIGNING_PROGRAM, system=system, raw=True)

    def test_registration_exclusions_are_not_executed_passes_or_runtime_skips(self):
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                result = self.inspect(legacy=legacy)
                self.assertTrue(result["releaseComplete"], result["errors"])
                self.assertEqual(result["pythonTestTotals"],
                    {"run": 8, "passed": 8, "failed": 0, "skipped": 0, "notApplicable": 0})
                excluded = result["pythonRegisteredNotApplicable"]
                self.assertEqual(len(excluded), 4)
                self.assertTrue(all(item["classification"] == "notApplicable" and
                    item["stage"] == "registration" for item in excluded))
                self.assertEqual(result["ctestPrograms"][0]["caseRegistration"], self.document())

    def test_selected_registration_requires_exact_observed_ordered_cases(self):
        for change in (lambda rows: rows[:-1],
                       lambda rows: rows + ["__main__.SigningTests.test_unobserved"],
                       lambda rows: rows[::-1], lambda rows: rows + rows[:1]):
            document = self.document()
            document["selected"] = change(document["selected"])
            result = self.inspect(document)
            self.assertFalse(result["releaseComplete"])
            self.assertTrue(any("registration" in error for error in result["errors"]))

    def test_exclusions_cannot_hide_selected_unknown_or_other_platform_cases(self):
        for change in (lambda document: document["notApplicable"].append(document["notApplicable"][0]),
                       lambda document: document["notApplicable"][0].update(case=document["selected"][0]),
                       lambda document: document["notApplicable"][0].update(case="__main__.SigningTests.test_unknown"),
                       lambda document: document["notApplicable"][0].update(reason="not supported")):
            document = self.document()
            change(document)
            result = self.inspect(document)
            self.assertFalse(result["releaseComplete"])
            self.assertEqual(result["pythonRegisteredNotApplicable"], [])
        for system in ("Linux", "Darwin"):
            self.assertFalse(self.inspect(system=system)["releaseComplete"])

    def test_registration_missing_fields_malformed_or_duplicate_records_fail(self):
        for registration in ("", "{", json.dumps(self.document())[:-1], "[]",
                             json.dumps({"suite": "all", "selected": []}),
                             json.dumps(self.document()) + "\n" + evidence.REGISTRATION_PREFIX + " " + json.dumps(self.document())):
            with self.subTest(registration=registration):
                result = self.inspect(registration=registration)
                self.assertFalse(result["releaseComplete"])
                self.assertTrue(any("registration" in error for error in result["errors"]))

    def test_registered_workflow_native_case_uses_the_same_exclusion_contract(self):
        name = evidence.WORKFLOW_NATIVE_CASE
        output = (evidence.REGISTRATION_PREFIX + " " + json.dumps({"suite": "native",
            "selected": [name], "notApplicable": [{"case": value,
                "reason": evidence.WORKFLOW_REGISTRATION_REASON}
                for value in sorted(evidence.WORKFLOW_POSIX_LINK_CASES)]}) + "\n"
            + name.rsplit(".", 1)[-1] + " (" + name + ") ... ok\nRan 1 test in 0.001s\n\nOK\n")
        counts, records, skips, errors = evidence.inspect_signing_unittest(output, "Windows", evidence.WORKFLOW_NATIVE_TEST)
        self.assertEqual(errors, [])
        self.assertEqual(counts["passed"], 1)
        self.assertEqual(counts["notApplicable"], 0)


class PythonWorkflowEvidenceTests(unittest.TestCase):
    def inspect(self, output=None, *, system="Windows", program=WORKFLOW_PROGRAM):
        return EvidenceTests.inspect(self, workflow_verbose() if output is None else output,
                                     name=program, system=system, raw=True)

    def skipped_rows(self, *, reason=WORKFLOW_SKIP_REASON):
        return [(name, "skipped " + repr(reason) if name in WORKFLOW_NSIS_CASES else "ok")
                for name in WORKFLOW_CASES]

    def test_exact_non_windows_nsis_skips_are_counted_separately_from_passes(self):
        expected = {"run": 8, "passed": 6, "failed": 0, "skipped": 2, "notApplicable": 2}
        for system in ("Linux", "Darwin"):
            with self.subTest(system=system):
                result = self.inspect(workflow_verbose(self.skipped_rows()), system=system)
                self.assertTrue(result["releaseComplete"], result["errors"])
                self.assertEqual(result["pythonTestTotals"], expected)
                program = result["ctestPrograms"][0]
                self.assertTrue(program["pythonTest"])
                self.assertFalse(program["qtTest"])
                self.assertEqual(program["pythonTestTotals"], expected)
                self.assertEqual(len(program["cases"]), 8)
                self.assertEqual({item["case"] for item in result["pythonTestSkips"]},
                                 {"__main__.WorkflowTests." + name for name in WORKFLOW_NSIS_CASES})
                self.assertTrue(all(item["classification"] == "notApplicable" for item in result["pythonTestSkips"]))

    def test_windows_execution_counts_all_cases_despite_expected_failure_json_stdout(self):
        result = self.inspect()
        self.assertTrue(result["releaseComplete"], result["errors"])
        self.assertEqual(result["pythonTestTotals"],
                         {"run": 8, "passed": 8, "failed": 0, "skipped": 0, "notApplicable": 0})
        self.assertEqual(len(result["ctestPrograms"][0]["cases"]), 8)
        self.assertEqual(result["pythonTestSkips"], [])
        self.assertEqual(result["qtTestTotals"]["passed"], 0)

    def test_windows_nsis_skip_is_required_not_run(self):
        result = self.inspect(workflow_verbose(self.skipped_rows()))
        self.assertFalse(result["releaseComplete"])
        self.assertEqual(result["pythonTestTotals"],
                         {"run": 8, "passed": 6, "failed": 0, "skipped": 2, "notApplicable": 0})
        self.assertTrue(all(item["classification"] == "requiredNotRun" for item in result["pythonTestSkips"]))

    def test_unknown_nsis_method_or_skip_reason_is_not_allowlisted(self):
        unknown = self.skipped_rows()
        unknown_index = WORKFLOW_CASES.index(WORKFLOW_NSIS_CASES[0])
        unknown[unknown_index] = ("test_unrecognized_nsis_case", unknown[unknown_index][1])
        for rows in (unknown, self.skipped_rows(reason="NSIS unavailable")):
            for system in ("Linux", "Darwin"):
                with self.subTest(rows=rows, system=system):
                    result = self.inspect(workflow_verbose(rows), system=system)
                    self.assertFalse(result["releaseComplete"])
                    self.assertTrue(any(item["classification"] == "requiredNotRun" for item in result["pythonTestSkips"]))

    def test_nsis_exception_cannot_apply_to_another_python_program(self):
        for program in (SIGNING_PROGRAM, "qcs-tests-license-materials"):
            with self.subTest(program=program):
                result = self.inspect(workflow_verbose(self.skipped_rows()), system="Linux", program=program)
                self.assertFalse(result["releaseComplete"])
                self.assertEqual(result["pythonTestTotals"]["notApplicable"], 0)
                self.assertTrue(all(item["classification"] == "requiredNotRun" for item in result["pythonTestSkips"]))

    def test_workflow_requires_complete_individual_records_and_matching_summary(self):
        complete = workflow_verbose()
        for output in ("success\n", "Ran 4 tests in 0.012s\n\nOK\n",
                       complete.replace("Ran 8 tests", "Ran 9 tests"), complete.replace("\nOK\n", "\n")):
            with self.subTest(output=output):
                self.assertFalse(self.inspect(output)["releaseComplete"])


class PythonCleanupPlatformEvidenceTests(unittest.TestCase):
    inspect = PythonWorkflowEvidenceTests.inspect
    def link_skip_rows(self, reason=WORKFLOW_LINK_SKIP_REASON):
        return [(name, "skipped " + repr(reason) if name in WORKFLOW_LINK_CASES else "ok")
                for name in WORKFLOW_CASES]

    def test_exact_windows_link_permission_skips_remain_separate_and_require_native_coverage(self):
        result = self.inspect(workflow_verbose(self.link_skip_rows()))
        self.assertTrue(result["releaseComplete"], result["errors"])
        self.assertEqual(result["pythonTestTotals"],
                         {"run": 8, "passed": 5, "failed": 0, "skipped": 3, "notApplicable": 3})
        self.assertEqual({entry["case"] for entry in result["pythonTestSkips"]},
                         {"__main__.WorkflowTests." + name for name in WORKFLOW_LINK_CASES})
        for entry in result["pythonTestSkips"]:
            self.assertEqual(entry["reason"], WORKFLOW_LINK_SKIP_REASON)
            self.assertEqual(entry["classification"], "notApplicable")
            self.assertEqual(entry["requiredNativePlatforms"], ["Linux", "Darwin"])
        for record in result["ctestPrograms"][0]["cases"]:
            if record["case"].rsplit(".", 1)[-1] in WORKFLOW_LINK_CASES:
                self.assertEqual(record["status"], "skipped " + repr(WORKFLOW_LINK_SKIP_REASON))

    def test_posix_platforms_must_execute_all_three_link_cases(self):
        rows = self.link_skip_rows()
        missing = [(name, status) for name, status in rows if name != WORKFLOW_LINK_CASES[0]]
        for system in ("Linux", "Darwin"):
            for records in (rows, missing):
                with self.subTest(system=system, rows=records):
                    result = self.inspect(workflow_verbose(records), system=system)
                    self.assertFalse(result["releaseComplete"])
                    self.assertEqual(result["pythonTestTotals"]["notApplicable"], 0)
                    self.assertTrue(any("Required native cleanup case" in error for error in result["errors"]))

    def test_link_permission_reason_and_method_must_match_exactly(self):
        wrong_name = self.link_skip_rows()
        index = WORKFLOW_CASES.index(WORKFLOW_LINK_CASES[0])
        wrong_name[index] = ("test_clean_unrecognized_link_case", wrong_name[index][1])
        for rows in (wrong_name, self.link_skip_rows("Windows symbolic links unavailable"),
                     self.link_skip_rows(WORKFLOW_LINK_SKIP_REASON + " (different cause)")):
            with self.subTest(rows=rows):
                result = self.inspect(workflow_verbose(rows))
                self.assertFalse(result["releaseComplete"])
                self.assertTrue(any(entry["classification"] == "requiredNotRun" for entry in result["pythonTestSkips"]))

    def test_link_exception_cannot_apply_to_another_python_program(self):
        for program in (SIGNING_PROGRAM, "qcs-tests-license-materials", "qcs-tests-acceptance-runners"):
            result = self.inspect(workflow_verbose(self.link_skip_rows()), program=program)
            self.assertFalse(result["releaseComplete"])
            self.assertEqual(result["pythonTestTotals"]["notApplicable"], 0)

    def test_only_posix_can_classify_the_exact_windows_junction_case_as_not_applicable(self):
        rows = [(name, "skipped " + repr(WORKFLOW_JUNCTION_SKIP_REASON)
                 if name == WORKFLOW_JUNCTION_CASE else "ok") for name in WORKFLOW_CASES]
        for system in ("Linux", "Darwin"):
            result = self.inspect(workflow_verbose(rows), system=system)
            self.assertTrue(result["releaseComplete"], result["errors"])
            self.assertEqual(result["pythonTestTotals"]["notApplicable"], 1)
            self.assertEqual(result["pythonTestSkips"][0]["reason"], WORKFLOW_JUNCTION_SKIP_REASON)
        result = self.inspect(workflow_verbose(rows), system="Windows")
        self.assertFalse(result["releaseComplete"])
        self.assertEqual(result["pythonTestTotals"]["notApplicable"], 0)

    def test_windows_junction_execution_cannot_be_omitted(self):
        rows = [(name, status) for name, status in self.link_skip_rows() if name != WORKFLOW_JUNCTION_CASE]
        result = self.inspect(workflow_verbose(rows))
        self.assertFalse(result["releaseComplete"])
        self.assertTrue(any("Required native cleanup case" in error for error in result["errors"]))


class PythonWindowsNativeEvidenceTests(unittest.TestCase):
    CASES = {
        "qcs-tests-gpu-configuration": ("AbiTests", ["test_real_windows_system_image"],
            "Uses an actual host PE library", "tests/runtime/gpu_configuration_test.py"),
        "qcs-tests-windows-package": ("NativeInstallerTests", [
            "test_install_uninstall_preserves_unowned_file", "test_missing_payload_fails_before_installing",
            "test_overlapping_payload_destination_is_rejected", "test_same_length_corruption_fails_before_installing"],
            "The native NSIS installer runs on Windows", "tools/qcs-package-windows-tests.py"),
    }

    def inspect(self, program, *, system="Linux", omitted=False, status=None, reason=None, changed_case=False,
                reported_program=None):
        class_name, methods, expected_reason, _ = self.CASES[program]
        rows = [("test_portable_contract", "__main__.PortableTests.test_portable_contract", "ok")]
        for index, method in enumerate(methods):
            if omitted and index == 0:
                continue
            identifier = f"__main__.{class_name}.{method}"
            if changed_case and index == 0:
                identifier = f"__main__.WrongClass.{method}"
            result = status or ("ok" if system == "Windows" else "skipped " + repr(reason or expected_reason))
            rows.append((method, identifier, result))
        skipped = sum(result.startswith("skipped ") for _, _, result in rows)
        output = "\n".join(f"{method} ({identifier}) ... {result}" for method, identifier, result in rows)
        output += f"\nRan {len(rows)} tests in 0.01s\n\nOK" + (f" (skipped={skipped})" if skipped else "") + "\n"
        return EvidenceTests.inspect(self, output, name=reported_program or program, system=system, raw=True)

    def test_exact_non_windows_skips_record_required_native_platform_without_passes(self):
        for program, (_, methods, reason, _) in self.CASES.items():
            for system in ("Linux", "Darwin"):
                with self.subTest(program=program, system=system):
                    result = self.inspect(program, system=system)
                    self.assertTrue(result["testsComplete"], result["errors"])
                    self.assertEqual(result["pythonTestTotals"]["passed"], 1)
                    self.assertEqual(result["pythonTestTotals"]["notApplicable"], len(methods))
                    for skipped in result["pythonTestSkips"]:
                        self.assertEqual(skipped["classification"], "notApplicable")
                        self.assertEqual(skipped["reason"], reason)
                        self.assertEqual(skipped["requiredNativePlatforms"], ["Windows"])

    def test_windows_must_execute_all_native_cases(self):
        for program, (_, methods, reason, _) in self.CASES.items():
            with self.subTest(program=program):
                result = self.inspect(program, system="Windows")
                self.assertTrue(result["testsComplete"], result["errors"])
                self.assertEqual(result["pythonTestTotals"]["passed"], 1 + len(methods))
                self.assertEqual(result["pythonTestTotals"]["notApplicable"], 0)
                result = self.inspect(program, system="Windows", status="skipped " + repr(reason))
                self.assertFalse(result["testsComplete"])
                self.assertTrue(all(item["classification"] == "requiredNotRun" for item in result["pythonTestSkips"]))

    def test_missing_native_records_cannot_hide_behind_matching_totals(self):
        for program in self.CASES:
            for system in ("Windows", "Linux", "Darwin"):
                with self.subTest(program=program, system=system):
                    result = self.inspect(program, system=system, omitted=True)
                    self.assertFalse(result["testsComplete"])
                    self.assertTrue(any("Required Windows case is missing" in error for error in result["errors"]))

    def test_skip_requires_exact_case_reason_and_program(self):
        for program in self.CASES:
            for changes in ({"reason": "Required runtime is missing"}, {"changed_case": True},
                            {"reported_program": "qcs-tests-license-materials"}):
                with self.subTest(program=program, changes=changes):
                    result = self.inspect(program, **changes)
                    self.assertFalse(result["testsComplete"])
                    self.assertTrue(any(item["classification"] == "requiredNotRun" for item in result["pythonTestSkips"]))

    def test_registered_native_cases_and_reasons_match_real_source_declarations(self):
        import ast
        for program, (class_name, methods, reason, relative) in self.CASES.items():
            with self.subTest(program=program):
                tree = ast.parse((native.SOURCE / relative).read_text(encoding="utf-8"))
                test_class = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name)
                declared = {node.name: node for node in test_class.body if isinstance(node, ast.FunctionDef)}
                self.assertTrue(set(methods) <= set(declared))
                self.assertEqual(evidence.WINDOWS_NATIVE_PROGRAM_CASES[program],
                    {f"__main__.{class_name}.{method}": reason for method in methods})
                for method in methods:
                    decorators = test_class.decorator_list + declared[method].decorator_list
                    reasons = [node.args[1].value for node in decorators
                        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "skipUnless" and len(node.args) == 2
                        and isinstance(node.args[1], ast.Constant)]
                    self.assertEqual(reasons, [reason])


class NativeTestFlowTests(unittest.TestCase):
    def run_test_action(self, failure=None, *, collection_complete=True):
        with tempfile.TemporaryDirectory(prefix="qcs-ci-test-flow-",
                dir=os.environ.get("QCOPILOTS_SEARCH_TEST_ROOT") or None) as temporary:
            parent = Path(temporary)
            root = parent / "windows-x64-cpu"
            reports = root / "reports/Release"
            inventory = json.dumps({"kind": "ctestInfo", "tests": [{"name": "qcs-tests-core"}]})
            with mock.patch.object(native.sys, "argv", ["synthetic-native-ci", "test"]), \
                    mock.patch.object(native, "ROOT", root, create=True), \
                    mock.patch.object(native, "REPORTS", reports, create=True), \
                    mock.patch.object(native.platform, "machine", return_value="x86_64"), \
                    mock.patch.dict(native.os.environ, {"QCOPILOTS_SEARCH_CI_ID": "windows-x64-cpu",
                        "QCOPILOTS_SEARCH_BUILD_ROOT": str(parent), "QCOPILOTS_SEARCH_QT_ROOT": str(root / "sdk"),
                        "QCOPILOTS_SEARCH_MODEL_DIR": str(root / "models")}), \
                    mock.patch.object(native, "run", return_value=native.subprocess.CompletedProcess([], 0, stdout=inventory)), \
                    mock.patch.object(native, "dev", side_effect=failure) as dev, \
                    mock.patch.object(native, "collect_test_wrappers", return_value={"collectionComplete": collection_complete}) as wrappers, \
                    mock.patch.object(native, "test_evidence") as acceptance, \
                    mock.patch.object(native, "gui_failure_diagnostics", return_value={"attempts": []}) as gui_diagnostics, \
                    mock.patch.object(native, "linux_failure_diagnostics", return_value={"commands": []}) as linux_diagnostics, \
                    mock.patch.object(native.subprocess, "Popen") as spawn:
                if failure is None:
                    if collection_complete:
                        native.main()
                        acceptance.assert_called_once_with(require_complete=True)
                    else:
                        with self.assertRaisesRegex(RuntimeError, "diagnostic collection is incomplete"):
                            native.main()
                        acceptance.assert_not_called()
                    gui_diagnostics.assert_not_called()
                    linux_diagnostics.assert_not_called()
                else:
                    with self.assertRaises(native.subprocess.CalledProcessError) as caught:
                        native.main()
                    self.assertIs(caught.exception, failure)
                    acceptance.assert_not_called()
                    gui_diagnostics.assert_called_once_with(reports / "tests/ctest.xml", reports / "mac-gui-debug")
                    linux_diagnostics.assert_called_once_with(reports / "tests/ctest.xml", reports / "linux-loader")
                dev.assert_called_once()
                arguments = dev.call_args.args
                self.assertEqual(arguments[0], "test")
                self.assertNotIn("--fail-fast", arguments)
                self.assertIn("--keep-failed-tests", arguments)
                self.assertEqual(arguments[arguments.index("--report-dir") + 1], str(reports / "tests"))
                self.assertTrue(dev.call_args.kwargs["display"])
                wrappers.assert_called_once_with()
                spawn.assert_not_called()
            self.assertEqual((reports / "tests/ctest-inventory.json").read_text(encoding="utf-8"), inventory)

    def test_complete_suite_invocation_still_requires_complete_evidence_after_success(self):
        self.run_test_action()

    def test_complete_suite_failure_preserves_original_error_without_acceptance_success(self):
        self.run_test_action(native.subprocess.CalledProcessError(17, ["synthetic-dev", "test"]))

    def test_diagnostic_failure_preserves_original_test_failure(self):
        self.run_test_action(native.subprocess.CalledProcessError(17, ["synthetic-dev", "test"]),
                             collection_complete=False)

    def test_successful_test_cannot_hide_incomplete_wrapper_collection(self):
        self.run_test_action(collection_complete=False)

    def test_evidence_parser_receives_complete_gate_and_inventory(self):
        with tempfile.TemporaryDirectory(prefix="qcs-evidence-command-",
                dir=os.environ.get("QCOPILOTS_SEARCH_TEST_ROOT") or None) as temporary:
            reports = Path(temporary)
            (reports / "tests").mkdir()
            (reports / "tests/qt-test-cases.json").write_text('{"testsComplete": true}', encoding="utf-8")
            with mock.patch.object(native, "REPORTS", reports, create=True), \
                    mock.patch.object(native, "run") as run:
                self.assertTrue(native.test_evidence(require_complete=True)["testsComplete"])
            command = run.call_args.args[0]
            self.assertIn("--require-complete", command)
            self.assertEqual(command[command.index("--inventory") + 1], reports / "tests/ctest-inventory.json")

    def test_evidence_parser_failure_propagates_to_native_job(self):
        failure = native.subprocess.CalledProcessError(1, ["synthetic-case-parser"])
        with mock.patch.object(native, "REPORTS", Path("synthetic reports"), create=True), \
                mock.patch.object(native, "run", side_effect=failure):
            with self.assertRaises(native.subprocess.CalledProcessError) as raised:
                native.test_evidence(require_complete=True)
            self.assertIs(raised.exception, failure)


class WrapperDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="qcs-wrap-中文 ",
            dir=os.environ.get("QCOPILOTS_SEARCH_TEST_ROOT") or None)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.reports = self.root / "reports/Release"
        self.selected = self.root / "runs/Release/qcs-t-fixture"
        self.inputs = self.selected / "reports"
        self.inputs.mkdir(parents=True)
        self.reports.mkdir(parents=True)
        for name, value in (("ROOT", self.root), ("REPORTS", self.reports)):
            patcher = mock.patch.object(native, name, value, create=True)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.latest = self.reports / "latest-test-run.json"
        self.latest.write_text(json.dumps({"runId": "fixture-run-identity",
            "testRoot": str(self.selected)}), encoding="utf-8")

    def wrapper(self, label="qcs-tests-core", *, suffix="fixture", cleanup_error=None):
        run_id = "qcs-t-" + suffix
        path = self.inputs / (run_id + ".json")
        path.write_text(json.dumps({"schemaVersion": 1, "kind": "qcs-test-run", "label": label,
            "runId": run_id, "root": str(self.selected), "childExitCode": 0,
            "exitCode": 125 if cleanup_error else 0, "passed": not cleanup_error,
            "cleanup": {"attempted": True, "succeeded": not cleanup_error, "error": cleanup_error}},
            ensure_ascii=False, indent=3) + "\n", encoding="utf-8")
        return path

    def junit(self, *labels):
        directory = self.reports / "tests"
        directory.mkdir(exist_ok=True)
        suite = ET.Element("testsuite")
        for label in labels:
            ET.SubElement(suite, "testcase", name=label, status="run")
        ET.ElementTree(suite).write(directory / "ctest.xml", encoding="utf-8")

    def collect(self):
        with mock.patch("sys.stderr", new=io.StringIO()):
            result = native.collect_test_wrappers()
        self.assertEqual(json.loads((self.reports / "wrappers/index.json").read_text(encoding="utf-8")), result)
        return result

    def test_success_and_cleanup_failure_preserve_raw_bytes_and_hashes(self):
        sources = [self.wrapper(suffix="ok"), self.wrapper("qcs-tests-ipc", suffix="bad",
            cleanup_error="Refusing cleanup across a link: 中文")]
        self.junit("qcs-tests-core", "qcs-tests-ipc")
        result = self.collect()
        self.assertTrue(result["collectionComplete"])
        self.assertEqual(len(result["files"]), 2)
        for record in result["files"]:
            raw = (self.root / record["source"]).read_bytes()
            self.assertEqual((self.reports / record["path"]).read_bytes(), raw)
            self.assertEqual(record["sha256"], native.hashlib.sha256(raw).hexdigest())
            self.assertEqual(record["bytes"], len(raw))
        failed = next(record for record in result["files"] if record["exitCode"] == 125)
        self.assertEqual(failed["childExitCode"], 0)
        self.assertIn("Refusing cleanup", failed["cleanup"]["error"])
        self.assertTrue(all(path.is_file() for path in sources))
        self.assertEqual(self.collect(), result)
        self.assertEqual(len(list((self.reports / "wrappers").iterdir())), 3)

    def test_only_direct_current_run_reports_are_copied(self):
        self.wrapper()
        for directory in (self.inputs / "nested", self.selected / "models", self.selected / "retained",
                          self.root / "runs/Release/qcs-t-other/reports"):
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "qcs-t-unrelated.json").write_text("unrelated content", encoding="utf-8")
        (self.inputs / "index.sqlite3").write_bytes(b"not diagnostic JSON")
        self.junit("qcs-tests-core")
        result = self.collect()
        self.assertTrue(result["collectionComplete"])
        self.assertEqual(len(result["files"]), 1)
        self.assertEqual(result["missingLabels"], [])

    def test_missing_executed_wrapper_is_explicit_without_inventing_raw_report(self):
        self.wrapper()
        self.junit("qcs-tests-core", "qcs-tests-ipc")
        result = self.collect()
        self.assertFalse(result["collectionComplete"])
        self.assertEqual(result["missingLabels"], ["qcs-tests-ipc"])
        self.assertEqual(len(result["files"]), 1)

    def test_malformed_json_is_archived_with_explicit_validation_error(self):
        path = self.inputs / "qcs-t-broken.json"
        path.write_bytes(b'{"interrupted":')
        result = self.collect()
        self.assertFalse(result["collectionComplete"])
        self.assertEqual(len(result["files"]), 1)
        self.assertEqual((self.reports / result["files"][0]["path"]).read_bytes(), path.read_bytes())
        self.assertTrue(result["errors"])

    def test_missing_test_invocation_is_not_reported_as_collected(self):
        self.latest.unlink()
        result = self.collect()
        self.assertFalse(result["collectionComplete"])
        self.assertEqual(result["files"], [])

    def test_root_outside_this_jobs_runs_directory_is_rejected(self):
        self.latest.write_text(json.dumps({"runId": "fixture", "testRoot": str(self.root)}), encoding="utf-8")
        result = self.collect()
        self.assertFalse(result["collectionComplete"])
        self.assertIn("outside", str(result["errors"]))

    def test_link_or_reparse_diagnostic_is_rejected_before_reading(self):
        path = self.wrapper()
        real_stat = Path.lstat

        def reparse(candidate, *args, **kwargs):
            result = real_stat(candidate, *args, **kwargs)
            if candidate == path:
                result = mock.Mock(st_mode=result.st_mode, st_file_attributes=0x400)
            return result

        with mock.patch.object(Path, "lstat", reparse):
            result = self.collect()
        self.assertFalse(result["collectionComplete"])
        self.assertEqual(result["files"], [])
        self.assertIn("Not an ordinary file", str(result["errors"]))

    def test_oversize_report_is_rejected_without_copying_data(self):
        path = self.inputs / "qcs-t-large.json"
        with path.open("wb") as output:
            output.truncate(1024 * 1024 + 1)
        result = self.collect()
        self.assertFalse(result["collectionComplete"])
        self.assertEqual(result["files"], [])
        self.assertIn("limit", str(result["errors"]))

    def test_junit_parent_link_is_rejected_and_explicitly_removed(self):
        self.wrapper()
        link = self.reports / "tests"
        with tempfile.TemporaryDirectory(prefix="qcs-outside-中文 ", dir=self.root.parent) as temporary:
            target = Path(temporary)
            xml = target / "ctest.xml"
            original = b'<testsuite><testcase name="qcs-tests-external" /></testsuite>'
            xml.write_bytes(original)
            try:
                if os.name == "nt":
                    environment = os.environ.copy()
                    environment.update(QCOPILOTS_SEARCH_FIXTURE_LINK=str(link),
                                       QCOPILOTS_SEARCH_FIXTURE_TARGET=str(target))
                    native.subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                        "$ErrorActionPreference = 'Stop'; "
                        "$null = New-Item -ItemType Junction -Path $env:QCOPILOTS_SEARCH_FIXTURE_LINK "
                        "-Target $env:QCOPILOTS_SEARCH_FIXTURE_TARGET"],
                        env=environment, capture_output=True, text=True, encoding="utf-8", errors="replace",
                        timeout=30, check=True)
                else:
                    link.symlink_to(target, target_is_directory=True)
                with mock.patch.object(native.ET, "fromstring") as parse_xml:
                    result = self.collect()
                self.assertFalse(result["collectionComplete"])
                self.assertIn("Not an ordinary directory", str(result["errors"]))
                parse_xml.assert_not_called()
                self.assertEqual(xml.read_bytes(), original)
            finally:
                # Remove only the redirect itself. Never let the outer test
                # runner encounter a retained junction or symbolic link.
                if link.exists() or link.is_symlink():
                    link.rmdir() if os.name == "nt" else link.unlink()
            self.assertFalse(link.exists() or link.is_symlink())
            self.assertEqual(xml.read_bytes(), original)

    def test_real_test_runner_report_is_collected_after_child_exit(self):
        environment = os.environ.copy()
        environment["QCOPILOTS_SEARCH_TEST_ROOT"] = str(self.selected)
        result = native.subprocess.run([sys.executable, "-B", str(native.SOURCE / "tools/qcs-test-runner.py"),
            "--root", str(self.selected), "--label", "qcs-tests-collector", "--",
            sys.executable, "-B", "-c", "print('real wrapper fixture')"],
            cwd=self.root, env=environment, capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.junit("qcs-tests-collector")
        collected = self.collect()
        self.assertTrue(collected["collectionComplete"])
        self.assertEqual(collected["files"][0]["cleanup"], {"attempted": True, "succeeded": True, "error": None})


class PackageModelFlowTests(unittest.TestCase):
    def test_native_installers_share_the_local_runner_and_its_model_arguments(self):
        root = Path("synthetic build 中文 空格")
        models = root / "shared models 中文"
        with mock.patch.object(native, "BUILD_PARENT", root, create=True), \
                mock.patch.object(native, "REPORTS", root / "reports", create=True), \
                mock.patch.object(native, "MODELS", models, create=True), \
                mock.patch.object(native, "QT", root / "sdk", create=True), \
                mock.patch.object(native.os, "name", "posix"), \
                mock.patch.object(native.platform, "system", return_value="Linux"), \
                mock.patch.dict(native.os.environ, {"QCOPILOTS_SEARCH_CI_ID": "linux-x64-cpu"}), \
                mock.patch.object(native, "run") as run:
            native.installers()
        command = run.call_args.args[0]
        self.assertIn("installers", command)
        self.assertEqual(command[command.index("--build-root") + 1], root)
        self.assertEqual(command[command.index("--variant") + 1], "linux-x64-cpu")
        self.assertEqual(command.count("--model-dir"), 1)
        self.assertEqual(command[command.index("--model-dir") + 1], models)
        self.assertTrue(run.call_args.kwargs["display"])

    def test_common_and_nsis_verification_keep_shared_model_argument(self):
        root = Path("synthetic build 中文 空格")
        models = root / "shared models 中文"
        for extra in ((), ("--package-file", str(root / "pkg/installer with spaces.exe"))):
            with self.subTest(extra=extra), \
                    mock.patch.object(native, "BUILD_PARENT", root, create=True), \
                    mock.patch.object(native, "QT", root / "Qt SDK", create=True), \
                    mock.patch.object(native, "MODELS", models, create=True), \
                    mock.patch.dict(native.os.environ, {"QCOPILOTS_SEARCH_CI_ID": "linux-x64-cpu"}), \
                    mock.patch.object(native, "run") as run:
                native.dev("verify-package", *extra, display=True)
                command = run.call_args.args[0]
                self.assertEqual(command.count("--model-dir"), 1)
                self.assertEqual(command[command.index("--model-dir") + 1], models)
                if extra:
                    self.assertEqual(command[command.index("--package-file") + 1], extra[1])
                self.assertTrue(run.call_args.kwargs["display"])

    def invoke_deb_outer(self, root, model_argument):
        package = root / "package 中文 空格.deb"
        package.write_bytes(b"synthetic package, never installed")
        argv = ["synthetic-deb-ci", "--build-root", str(root / "build with spaces"),
                "--package-file", str(package)]
        if model_argument is not None:
            argv += ["--model-dir", str(model_argument)]
        with mock.patch.object(deb.sys, "argv", argv), \
                mock.patch.dict(deb.os.environ, {"QCOPILOTS_SEARCH_VARIANT": "linux-x64-cpu",
                    "QCOPILOTS_SEARCH_CI_ID": "linux-x64-cpu"}, clear=True), \
                mock.patch.object(deb.platform, "system", return_value="Linux"), \
                mock.patch.object(deb.os, "geteuid", return_value=1000, create=True), \
                mock.patch.object(deb.os, "getegid", return_value=1000, create=True), \
                mock.patch.object(deb.os, "getuid", return_value=1000, create=True), \
                mock.patch.object(deb.os, "getgid", return_value=1000, create=True), \
                mock.patch.object(deb, "run", return_value=deb.subprocess.CompletedProcess([], 0, "synthetic stdout", "")) as run, \
                mock.patch.object(deb.subprocess, "Popen") as spawn, \
                mock.patch.object(deb.sys, "stderr", io.StringIO()):
            try:
                deb.main()
            except (SystemExit, RuntimeError):
                run.assert_not_called()
                spawn.assert_not_called()
                self.assertFalse((root / "build with spaces").exists())
                raise
            spawn.assert_not_called()
            run.assert_called_once()
            return run.call_args.args[0]

    def test_deb_host_mounts_existing_models_readonly_without_splitting_spaces(self):
        with tempfile.TemporaryDirectory(prefix="qcs-ci-deb-",
                dir=os.environ.get("QCOPILOTS_SEARCH_TEST_ROOT") or None) as temporary:
            root = Path(temporary)
            models = root / "models 中文 空格"
            models.mkdir()
            command = self.invoke_deb_outer(root, models)
            self.assertEqual(command[:3], ["docker", "run", "--rm"])
            mounts = [command[i + 1] for i, item in enumerate(command) if item == "--mount"]
            model_mounts = [item for item in mounts if "target=/models" in item]
            self.assertEqual(model_mounts, [f"type=bind,source={models.resolve()},target=/models,readonly"])

    def test_deb_host_rejects_missing_or_invalid_models_before_docker_and_output(self):
        for case in ("argument absent", "directory absent", "regular file"):
            with self.subTest(case=case), tempfile.TemporaryDirectory(prefix="qcs-ci-deb-missing-",
                    dir=os.environ.get("QCOPILOTS_SEARCH_TEST_ROOT") or None) as temporary:
                root = Path(temporary)
                models = None if case == "argument absent" else root / "models 中文 空格"
                if case == "regular file":
                    models.write_text("not a model directory", encoding="utf-8")
                if models is None:
                    with self.assertRaises(SystemExit) as caught:
                        self.invoke_deb_outer(root, models)
                    self.assertEqual(caught.exception.code, 2)
                else:
                    with self.assertRaisesRegex(RuntimeError, "Required real-model directory does not exist"):
                        self.invoke_deb_outer(root, models)

    def inspect_container_smoke(self, smoke):
        with tempfile.TemporaryDirectory(prefix="qcs-ci-deb-container-",
                dir=os.environ.get("QCOPILOTS_SEARCH_TEST_ROOT") or None) as temporary:
            root = Path(temporary)
            for relative in ("incoming", "reports/smoke", "work/user-profile", "etc", "usr/bin"):
                (root / relative).mkdir(parents=True, exist_ok=True)
            (root / ".dockerenv").touch()
            (root / "incoming/qcs.deb").write_bytes(b"synthetic DEB fixture")
            (root / "etc/os-release").write_text("synthetic container", encoding="utf-8")
            (root / "reports/smoke/report.json").write_text(json.dumps(smoke), encoding="utf-8")
            payload = root / "usr/bin/qcs-gui"
            payload.write_bytes(b"synthetic payload")
            commands = []
            installed_queries = 0

            def container_path(value):
                self.assertTrue(str(value).startswith("/"))
                return root / str(value).lstrip("/")

            def fake_run(command, **kwargs):
                nonlocal installed_queries
                parts = [str(item) for item in command]
                commands.append(parts)
                result = deb.subprocess.CompletedProcess(parts, 0, "", "")
                if parts[:2] == ["dpkg-deb", "--field"]:
                    result.stdout = "qcs" if parts[-1] == "Package" else "0.1.0"
                elif parts[:2] == ["dpkg-query", "--show"]:
                    installed_queries += 1
                    result.returncode = 1 if installed_queries == 1 else 0
                    result.stdout = "" if installed_queries == 1 else "ii "
                elif parts[:2] == ["dpkg-query", "--listfiles"]:
                    result.stdout = "/usr/bin/qcs-gui\n"
                elif parts[:2] == ["dpkg", "--remove"]:
                    payload.unlink()
                return result

            complete = (smoke.get("passed") is True and smoke.get("releaseComplete") is True
                        and smoke.get("modelSmoke", {}).get("passed") is True)
            with mock.patch.object(deb, "Path", side_effect=container_path), \
                    mock.patch.object(deb.platform, "system", return_value="Linux"), \
                    mock.patch.object(deb.os, "geteuid", return_value=0, create=True), \
                    mock.patch.dict(deb.os.environ, {"QCOPILOTS_SEARCH_DEB_CONTAINER": "1",
                        "QCOPILOTS_SEARCH_VARIANT": "linux-x64-cpu", "QCOPILOTS_SEARCH_CI_ID": "linux-x64-cpu",
                        "QCOPILOTS_SEARCH_HOST_UID": "1000", "QCOPILOTS_SEARCH_HOST_GID": "1000"}, clear=True), \
                    mock.patch.dict(sys.modules, {"grp": mock.Mock()}), \
                    mock.patch.object(deb, "run", side_effect=fake_run), \
                    mock.patch.object(deb.subprocess, "Popen") as spawn, \
                    mock.patch("builtins.print"):
                if complete:
                    deb.inside_container()
                else:
                    with self.assertRaisesRegex(RuntimeError, "complete package and real-model smoke"):
                        deb.inside_container()
                spawn.assert_not_called()
            report = json.loads((root / "reports/report.json").read_text(encoding="utf-8"))
            verifier = [command for command in commands if command[0] == "runuser"]
            self.assertEqual(len(verifier), 1)
            self.assertEqual(verifier[0][verifier[0].index("--model-dir") + 1], str(root / "models"))
            self.assertEqual(verifier[0].count("--model-dir"), 1)
            return report

    def test_deb_container_verifier_receives_models_and_complete_smoke_can_pass(self):
        report = self.inspect_container_smoke({"passed": True, "releaseComplete": True,
                                              "modelSmoke": {"passed": True}})
        self.assertTrue(report["passed"])
        self.assertTrue(report["releaseComplete"])
        self.assertEqual(report["retainedProfileFiles"], 1)

    def test_deb_container_partial_smoke_cannot_claim_release_completion(self):
        for smoke in ({"passed": True}, {"passed": True, "releaseComplete": False,
                       "modelSmoke": {"passed": True}}, {"passed": True, "releaseComplete": True},
                      {"passed": True, "releaseComplete": True, "modelSmoke": {"passed": False}}):
            with self.subTest(smoke=smoke):
                report = self.inspect_container_smoke(smoke)
                self.assertFalse(report["passed"])
                self.assertFalse(report["releaseComplete"])

    def test_deb_simulations_isolate_the_enclosing_build_variant_and_container(self):
        for enclosing_variant in ("windows-x64-cpu", "linux-x64-cuda-13.4"):
            with self.subTest(variant=enclosing_variant), mock.patch.dict(os.environ, {
                    "QCOPILOTS_SEARCH_VARIANT": enclosing_variant,
                    "QCOPILOTS_SEARCH_CI_ID": enclosing_variant,
                    "QCOPILOTS_SEARCH_CI_CONTAINER": "1"}), tempfile.TemporaryDirectory(
                    prefix="qcs-ci-deb-env-", dir=os.environ.get("QCOPILOTS_SEARCH_TEST_ROOT") or None) as temporary:
                root = Path(temporary)
                models = root / "models"
                models.mkdir()
                command = self.invoke_deb_outer(root, models)
                self.assertEqual(command[:3], ["docker", "run", "--rm"])
                self.assertIn("QCOPILOTS_SEARCH_VARIANT=linux-x64-cpu", command)
                report = self.inspect_container_smoke({"passed": True, "releaseComplete": True,
                                                       "modelSmoke": {"passed": True}})
                self.assertTrue(report["releaseComplete"])


class RunnerIsolationTests(unittest.TestCase):
    def environment(self, temporary):
        return {"RUNNER_TEMP": str(temporary), "GITHUB_ENV": str(temporary / "environment"),
                "GITHUB_RUN_ID": "12345678901234567890", "GITHUB_RUN_ATTEMPT": "123",
                "QCOPILOTS_SEARCH_CI_ID": "windows-x64-cpu", "GITHUB_EVENT_PATH": "",
                "GITHUB_SHA": "a" * 40, "QCOPILOTS_SEARCH_CANDIDATE_HEAD": "a" * 40}

    def test_short_roots_are_unique_and_keep_full_runner_identity(self):
        with tempfile.TemporaryDirectory(prefix="qcs-ci-中文 ",
                dir=os.environ.get("QCOPILOTS_SEARCH_TEST_ROOT") or None) as directory:
            temporary = Path(directory)
            environment = self.environment(temporary)
            with mock.patch.dict(os.environ, environment, clear=True):
                roots = [native.initialize_runner(), native.initialize_runner()]
            self.assertNotEqual(*roots)
            for parent in roots:
                self.assertEqual(parent.parent, temporary.resolve())
                self.assertLessEqual(len(parent.name), 20)
                root = parent / "windows-x64-cpu"
                identity = json.loads((root / "reports/Release/runner-identity.json").read_text(encoding="utf-8"))
                self.assertEqual(identity["candidateHead"], "a" * 40)
                self.assertEqual(identity["runId"], environment["GITHUB_RUN_ID"])
                self.assertEqual(identity["runAttempt"], environment["GITHUB_RUN_ATTEMPT"])
                self.assertEqual(identity["configuration"], "windows-x64-cpu")
                for path in (parent / "sdk", parent / "models", root / "runs/Release"):
                    self.assertTrue(path.is_dir())
            exported = (temporary / "environment").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(exported), 10)
            self.assertEqual(sum(line.startswith("QCOPILOTS_SEARCH_TEST_ROOT=") for line in exported), 2)

    def test_initialized_ci_root_is_accepted_by_real_developer_bootstrap_guard(self):
        with tempfile.TemporaryDirectory(prefix="qcs-owner-中文 ",
                dir=os.environ.get("QCOPILOTS_SEARCH_TEST_ROOT") or None) as directory:
            temporary = Path(directory)
            with mock.patch.dict(os.environ, self.environment(temporary), clear=True):
                root = native.initialize_runner() / "windows-x64-cpu"
            developer = native.developer_workflow()
            self.assertEqual(developer.SOURCE, native.SOURCE)
            marker = root / developer.MARKER
            self.assertTrue(marker.is_file())
            before = marker.read_bytes()
            self.assertEqual(json.loads(before), {"source": str(native.SOURCE), "schemaVersion": 1})
            # This is the actual qcs-dev guard, not a mocked or copied rule.
            # All CI subdirectories already exist when bootstrap reaches it.
            developer.initialize_root(root)
            self.assertEqual(marker.read_bytes(), before)
            self.assertEqual({path.name for path in root.iterdir()},
                             {developer.MARKER, "runs", "reports"})

    def test_real_developer_guard_rejects_ci_root_with_replaced_owner(self):
        with tempfile.TemporaryDirectory(prefix="qcs-owner-",
                dir=os.environ.get("QCOPILOTS_SEARCH_TEST_ROOT") or None) as directory:
            temporary = Path(directory)
            with mock.patch.dict(os.environ, self.environment(temporary), clear=True):
                root = native.initialize_runner() / "windows-x64-cpu"
            developer = native.developer_workflow()
            marker = root / developer.MARKER
            marker.write_text(json.dumps({"source": str(temporary / "another checkout"), "schemaVersion": 1}),
                              encoding="utf-8")
            before = marker.read_bytes()
            with self.assertRaisesRegex(RuntimeError, "different checkout"):
                developer.initialize_root(root)
            self.assertEqual(marker.read_bytes(), before)

    def test_initialize_cli_accepts_arbitrary_cwd_before_build_paths_exist(self):
        with tempfile.TemporaryDirectory(prefix="qcs-cli-中文 ",
                dir=os.environ.get("QCOPILOTS_SEARCH_TEST_ROOT") or None) as directory:
            temporary = Path(directory)
            environment = os.environ.copy()
            for name in ("QCOPILOTS_SEARCH_BUILD_ROOT", "QCOPILOTS_SEARCH_QT_ROOT",
                         "QCOPILOTS_SEARCH_MODEL_DIR", "QCOPILOTS_SEARCH_TEST_ROOT"):
                environment.pop(name, None)
            environment.update(self.environment(temporary))
            result = native.subprocess.run([sys.executable, "-B", str(Path(native.__file__).resolve()), "initialize"],
                cwd=temporary, env=environment, capture_output=True, text=True, encoding="utf-8", timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            exports = dict(line.split("=", 1) for line in (temporary / "environment").read_text(encoding="utf-8").splitlines())
            created = Path(exports["QCOPILOTS_SEARCH_BUILD_ROOT"])
            self.assertEqual(created.parent, temporary.resolve())
            variant_root = Path(exports["QCOPILOTS_SEARCH_VARIANT_ROOT"])
            self.assertEqual(variant_root, created / "windows-x64-cpu")
            self.assertTrue((variant_root / "reports/Release/runner-identity.json").is_file())

    def test_missing_required_environment_does_not_create_directories(self):
        with tempfile.TemporaryDirectory(prefix="qcs-ci-",
                dir=os.environ.get("QCOPILOTS_SEARCH_TEST_ROOT") or None) as directory:
            temporary = Path(directory)
            for name in ("RUNNER_TEMP", "GITHUB_ENV", "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT", "QCOPILOTS_SEARCH_CI_ID"):
                environment = self.environment(temporary)
                environment.pop(name)
                with self.subTest(name=name), mock.patch.dict(os.environ, environment, clear=True), \
                        mock.patch.object(native.tempfile, "mkdtemp") as create:
                    with self.assertRaisesRegex(RuntimeError, "Required CI variable"):
                        native.initialize_runner()
                    create.assert_not_called()
            self.assertEqual(list(temporary.iterdir()), [])

    def test_missing_or_mismatched_candidate_fails_before_creating_output(self):
        with tempfile.TemporaryDirectory(prefix="qcs-ci-",
                dir=os.environ.get("QCOPILOTS_SEARCH_TEST_ROOT") or None) as directory:
            temporary = Path(directory)
            for actual, declared in (("abc", "abc"), ("", ""), ("a" * 40, "b" * 40)):
                environment = self.environment(temporary)
                environment.update(GITHUB_SHA=actual, QCOPILOTS_SEARCH_CANDIDATE_HEAD=declared)
                with self.subTest(actual=actual, declared=declared), \
                        mock.patch.dict(os.environ, environment, clear=True), \
                        mock.patch.object(native.tempfile, "mkdtemp") as create:
                    with self.assertRaises(RuntimeError):
                        native.initialize_runner()
                    create.assert_not_called()

    def test_unknown_variant_fails_before_creating_output(self):
        with tempfile.TemporaryDirectory(prefix="qcs-ci-variant-",
                dir=os.environ.get("QCOPILOTS_SEARCH_TEST_ROOT") or None) as directory:
            temporary = Path(directory)
            environment = {**self.environment(temporary), "QCOPILOTS_SEARCH_CI_ID": "unknown-variant"}
            with mock.patch.dict(os.environ, environment, clear=True), \
                    mock.patch.object(native.tempfile, "mkdtemp") as create:
                with self.assertRaisesRegex(RuntimeError, "Unknown CI build variant"):
                    native.initialize_runner()
                create.assert_not_called()
            self.assertEqual(list(temporary.iterdir()), [])

    def test_source_tree_cannot_be_used_as_runner_temporary_root(self):
        environment = self.environment(native.SOURCE)
        with mock.patch.dict(os.environ, environment, clear=True), \
                mock.patch.object(native.tempfile, "mkdtemp") as create:
            with self.assertRaisesRegex(RuntimeError, "inside the checkout"):
                native.initialize_runner()
            create.assert_not_called()

    def test_temporary_creation_failure_is_explicit(self):
        with tempfile.TemporaryDirectory(prefix="qcs-ci-",
                dir=os.environ.get("QCOPILOTS_SEARCH_TEST_ROOT") or None) as directory:
            temporary = Path(directory)
            with mock.patch.dict(os.environ, self.environment(temporary), clear=True), \
                    mock.patch.object(native.tempfile, "mkdtemp", side_effect=PermissionError("creation denied")):
                with self.assertRaisesRegex(PermissionError, "creation denied"):
                    native.initialize_runner()
            self.assertEqual(list(temporary.iterdir()), [])

    def test_native_build_rejects_merge_or_stale_checkout_before_compiling(self):
        validator = mock.Mock()
        validator.source_identity.return_value = {"candidateHead": "a" * 40, "checkoutCommit": "b" * 40}
        validator.valid_identity.return_value = []
        with mock.patch.object(native, "release_evidence", return_value=validator), \
                mock.patch.object(native, "candidate_head", return_value="a" * 40), \
                mock.patch.object(native, "dev") as compile_app:
            with self.assertRaisesRegex(RuntimeError, "exact candidate head"):
                native.build()
            compile_app.assert_not_called()
            validator.write_json.assert_not_called()


class NativeIdentityTests(unittest.TestCase):
    def test_pr_candidate_head_is_distinct_from_merge_checkout(self):
        with tempfile.TemporaryDirectory(prefix="qcs-event-",
                dir=os.environ.get("QCOPILOTS_SEARCH_TEST_ROOT") or None) as directory:
            path = Path(directory) / "event.json"
            path.write_text(json.dumps({"pull_request": {"head": {"sha": "a" * 40}}}), encoding="utf-8")
            with mock.patch.dict(os.environ, {"GITHUB_EVENT_PATH": str(path), "GITHUB_SHA": "b" * 40}):
                self.assertEqual(native.candidate_head(), "a" * 40)

    def test_non_pr_candidate_uses_actual_trigger_commit(self):
        with mock.patch.dict(os.environ, {"GITHUB_EVENT_PATH": "", "GITHUB_SHA": "b" * 40}):
            self.assertEqual(native.candidate_head(), "b" * 40)




class GuiDiagnosticsTests(unittest.TestCase):
    def replay(self, *, failed=True, system="Darwin", crash=False, timeout=False, exited=True):
        with tempfile.TemporaryDirectory(prefix="qcs-debug-test-",
                dir=os.environ.get("QCOPILOTS_SEARCH_TEST_ROOT") or None) as temporary:
            root = Path(temporary)
            junit = root / "ctest.xml"
            xml = ET.Element("testsuite")
            case = ET.SubElement(xml, "testcase", name="qcs-tests-gui")
            if failed:
                ET.SubElement(case, "failure")
            ET.ElementTree(xml).write(junit, encoding="utf-8")
            original = junit.read_bytes()
            processes = []

            def spawn(command, **arguments):
                text = ("Process 5678 launched: fixture\n" +
                        ("stop reason = EXC_BAD_ACCESS (code=1)\nframe #0: fixture\n" if crash else "") +
                        ("Process 5678 exited with status = 0\n" if exited else ""))
                arguments["stdout"].write(text.encode("utf-8"))
                process = mock.Mock(pid=12345)
                process.wait.side_effect = ([native.subprocess.TimeoutExpired(command, 45), -9, -9]
                                            if timeout else [0, 0])
                processes.append((command, arguments, process))
                return process

            with mock.patch.object(native.platform, "system", return_value=system), \
                    mock.patch.object(native, "ROOT", root, create=True), \
                    mock.patch.object(native, "find_program", return_value=root / "fixture-program"), \
                    mock.patch.object(native, "native_environment", side_effect=lambda: {}), \
                    mock.patch.object(native.subprocess, "Popen", side_effect=spawn), \
                    mock.patch.object(native.os, "killpg", create=True) as kill_group, \
                    mock.patch.object(native.signal, "SIGKILL", 9, create=True):
                result = native.gui_failure_diagnostics(junit, root / "reports")
            self.assertEqual(junit.read_bytes(), original)
            for command, arguments, process in processes:
                self.assertTrue(arguments["start_new_session"])
                self.assertTrue(Path(arguments["cwd"]).is_relative_to(root / "runs"))
                self.assertEqual(arguments["env"]["QCOPILOTS_SEARCH_TEST_ROOT"], str(arguments["cwd"]))
                self.assertIn("--one-line-on-crash", command)
                self.assertIn("thread backtrace all", command)
                self.assertEqual(process.wait.call_args_list[0].kwargs["timeout"], 45)
            self.assertEqual(kill_group.call_count, len(processes))
            return result

    def test_successful_original_or_non_macos_does_not_replay(self):
        self.assertEqual(self.replay(failed=False)["attempts"], [])
        self.assertEqual(self.replay(system="Windows")["attempts"], [])

    def test_original_failure_is_preserved_and_crash_stack_stops_replay(self):
        result = self.replay(crash=True)
        self.assertEqual(len(result["attempts"]), 1)
        self.assertTrue(result["attempts"][0]["backtraceObserved"])
        self.assertNotIn("passed", result)

    def test_unreproduced_failure_runs_only_two_isolated_sequences(self):
        result = self.replay()
        self.assertEqual(len(result["attempts"]), 2)
        self.assertNotEqual(result["attempts"][0]["testRoot"], result["attempts"][1]["testRoot"])
        self.assertEqual(result["attempts"][1]["cases"],
                         ["pdfHitPageIsSelected", "imageAndMissingFileReleaseResources"])

    def test_timeout_is_diagnostic_and_child_group_is_terminated(self):
        result = self.replay(timeout=True)
        self.assertEqual(len(result["attempts"]), 2)
        self.assertTrue(all(attempt["timedOut"] for attempt in result["attempts"]))

    def test_unconfirmed_inferior_exit_prevents_another_replay(self):
        result = self.replay(exited=False)
        self.assertEqual(len(result["attempts"]), 1)
        self.assertFalse(result["attempts"][0]["inferiorCleanupConfirmed"])
        self.assertIn("cleanupUnknown", result["attempts"][0])


class LinuxDiagnosticsTests(unittest.TestCase):
    def diagnose(self, *, system="Linux", failed=True, present=True):
        with tempfile.TemporaryDirectory(prefix="qcs-linux-diagnostic-",
                dir=os.environ.get("QCOPILOTS_SEARCH_TEST_ROOT") or None) as temporary:
            root = Path(temporary)
            qt = root / "qt"
            prefix = qt / "6.12.0/gcc_64"
            program = root / "release/bin/qcs-tests-gui"
            targets = [program, prefix / "lib/libavutil.so.61",
                       prefix / "plugins/platforms/libqxcb.so", prefix / "lib/libQt6XcbQpa.so.6",
                       prefix / "lib/libQt6FFmpegStub-ssl.so.6",
                       prefix / "lib/libQt6FFmpegStub-crypto.so.6.12.0"]
            for target in targets:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"Synthetic loader diagnostic fixture, not an executable")
            junit = root / "ctest.xml"
            document = ET.Element("testsuite")
            case = ET.SubElement(document, "testcase", name="qcs-tests-gui")
            if failed:
                ET.SubElement(case, "failure")
            if present:
                ET.ElementTree(document).write(junit, encoding="utf-8")
            original = junit.read_bytes() if present else None
            observed = []

            def capture(command, log, environment, **limits):
                entry = {"command": [str(item) for item in command], "log": log.name}
                observed.append({**entry, "environment": environment.copy(), "limits": limits})
                return entry

            inherited = {"PATH": "original-tool-path", "LD_LIBRARY_PATH": "original-loader-path",
                         "QT_QPA_PLATFORM": "xcb", "QT_PLUGIN_PATH": "original-plugin-path",
                         "QCOPILOTS_SEARCH_TEST_ROOT": "original-test-root"}
            with mock.patch.object(native.platform, "system", return_value=system), \
                    mock.patch.object(native, "ROOT", root, create=True), \
                    mock.patch.object(native, "QT", qt, create=True), \
                    mock.patch.object(native, "MODELS", root / "models", create=True), \
                    mock.patch.object(native, "find_program", return_value=program), \
                    mock.patch.dict(native.os.environ, inherited), \
                    mock.patch.object(native, "bounded_linux_diagnostic", side_effect=capture), \
                    mock.patch.object(native.subprocess, "Popen") as spawn:
                result = native.linux_failure_diagnostics(junit, root / "reports/linux-loader")
                spawn.assert_not_called()
            self.assertEqual(junit.read_bytes() if present else None, original)
            self.assertNotIn("passed", result)
            self.assertTrue(all("passed" not in entry for entry in result["commands"]))
            return result, observed, root, prefix, targets

    def test_non_linux_or_absent_failed_junit_never_spawns_diagnostics(self):
        for arguments in ({"system": "Windows"}, {"failed": False}, {"present": False}):
            with self.subTest(arguments=arguments):
                result, commands, *_ = self.diagnose(**arguments)
                self.assertEqual(result["commands"], [])
                self.assertEqual(commands, [])

    def test_loader_environment_is_preserved_with_an_isolated_xvfb_probe(self):
        result, commands, root, prefix, targets = self.diagnose()
        expected_path = os.pathsep.join(str(path) for path in (
            root / "Release/bin", root / "Release/lib", prefix / "bin", root / "bootstrap/nsis"))
        expected_path += os.pathsep + "original-tool-path"
        for command in commands:
            environment = command["environment"]
            self.assertEqual(environment["PATH"], expected_path)
            self.assertEqual(environment["LD_LIBRARY_PATH"], "original-loader-path")
            self.assertEqual(environment["QT_PLUGIN_PATH"], "original-plugin-path")
            self.assertEqual(environment["QT_QPA_PLATFORM"], "xcb")
            self.assertTrue(Path(environment["QCOPILOTS_SEARCH_TEST_ROOT"]).is_relative_to(root / "runs"))
            self.assertNotIn("offscreen", command["command"])
        probe = commands[-1]
        self.assertEqual(probe["command"], ["xvfb-run", "--auto-servernum",
            "--server-args=-screen 0 1600x1000x24", str(targets[0]), "-functions"])
        self.assertEqual(probe["environment"]["QT_DEBUG_PLUGINS"], "1")
        self.assertEqual(probe["limits"]["timeout"], 20)
        self.assertEqual(result["environment"]["LD_LIBRARY_PATH"], "original-loader-path")

    def test_elf_commands_cover_gui_qt_ffmpeg_and_discovered_tls_stubs(self):
        _, commands, _, _, targets = self.diagnose()
        self.assertEqual(len(commands), 2 * len(targets) + 1)
        for target in targets:
            self.assertIn(["ldd", "-r", str(target)], [entry["command"] for entry in commands])
            self.assertIn(["readelf", "--dynamic", "--version-info", str(target)],
                          [entry["command"] for entry in commands])

    def bounded(self, *, oversized=False):
        with tempfile.TemporaryDirectory(prefix="qcs-linux-bounded-",
                dir=os.environ.get("QCOPILOTS_SEARCH_TEST_ROOT") or None) as temporary:
            root = Path(temporary)
            log = root / "synthetic-diagnostic.log"
            payload = b"x" * (128 if oversized else 16)
            process = mock.Mock(pid=24680)
            process.poll.return_value = None
            process.wait.return_value = -9

            def spawn(command, **arguments):
                self.assertTrue(arguments["start_new_session"])
                self.assertEqual(arguments["cwd"], root)
                self.assertEqual(arguments["env"], {"PATH": "fixture-path"})
                self.assertEqual(arguments["stderr"], native.subprocess.STDOUT)
                arguments["stdout"].write(payload)
                arguments["stdout"].flush()
                return process

            with mock.patch.object(native, "ROOT", root, create=True), \
                    mock.patch.object(native.subprocess, "Popen", side_effect=spawn), \
                    mock.patch.object(native.time, "monotonic", side_effect=[100, 100.1 if oversized else 111]), \
                    mock.patch.object(native.time, "sleep") as sleep, \
                    mock.patch.object(native.os, "killpg", create=True) as kill_group, \
                    mock.patch.object(native.signal, "SIGKILL", 9, create=True):
                result = native.bounded_linux_diagnostic(["synthetic-fixture-tool", "--diagnose"],
                    log, {"PATH": "fixture-path"}, timeout=10, output_limit=64)
                kill_group.assert_called_once_with(process.pid, 9)
                process.wait.assert_called_once_with(timeout=5)
                sleep.assert_not_called()
            self.assertNotIn("passed", result)
            self.assertTrue(result["stoppedAtLimit"])
            self.assertEqual(result["observedBytes"], len(payload))
            self.assertEqual(result["retainedBytes"], log.stat().st_size)
            self.assertLessEqual(log.stat().st_size, 64)
            self.assertEqual(log.read_bytes(), payload[:64])
            return result

    def test_timeout_terminates_only_the_owned_process_group(self):
        result = self.bounded()
        self.assertEqual(result["exitCode"], -9)
        self.assertFalse(result.get("outputTruncated", False))

    def test_size_limit_terminates_owned_group_and_records_bounded_log(self):
        result = self.bounded(oversized=True)
        self.assertTrue(result["outputTruncated"])
        self.assertEqual(result["retainedBytes"], 64)

    def sdk(self, *, system="Linux", present=("qxcb", "xcbqpa")):
        with tempfile.TemporaryDirectory(prefix="qcs-sdk-diagnostic-",
                dir=os.environ.get("QCOPILOTS_SEARCH_TEST_ROOT") or None) as temporary:
            root = Path(temporary)
            qt = root / "qt"
            prefix = qt / "6.12.0/gcc_64"
            targets = {"qxcb": prefix / "plugins/platforms/libqxcb.so",
                       "xcbqpa": prefix / "lib/libQt6XcbQpa.so.6"}
            for name in present:
                targets[name].parent.mkdir(parents=True, exist_ok=True)
                targets[name].write_bytes(b"Synthetic SDK target")
            unrelated = prefix / "lib/libavutil.so.61"
            unrelated.parent.mkdir(parents=True, exist_ok=True)
            unrelated.write_bytes(b"SDK fixture outside this diagnostic scope")
            observed = []

            def capture(command, log, environment, **limits):
                entry = {"command": [str(item) for item in command], "log": log.name}
                observed.append({**entry, "environment": environment.copy()})
                return entry

            with mock.patch.object(native.platform, "system", return_value=system), \
                    mock.patch.object(native, "ROOT", root, create=True), \
                    mock.patch.object(native, "QT", qt, create=True), \
                    mock.patch.dict(native.os.environ, {"LD_LIBRARY_PATH": "inherited-sdk-loader-path"}), \
                    mock.patch.object(native, "bounded_linux_diagnostic", side_effect=capture), \
                    mock.patch.object(native, "find_program") as find_program, \
                    mock.patch.object(native.subprocess, "Popen") as spawn:
                result = native.linux_sdk_diagnostics(root / "reports/linux-sdk")
                find_program.assert_not_called()
                spawn.assert_not_called()
            self.assertNotIn("passed", result)
            return result, observed, targets

    def test_sdk_diagnostic_uses_only_existing_qt_targets_without_gui_build(self):
        for present in (("qxcb", "xcbqpa"), ("qxcb",), ()):
            with self.subTest(present=present):
                _, observed, targets = self.sdk(present=present)
                self.assertEqual(len(observed), len(present) * 2)
                commands = [entry["command"] for entry in observed]
                for name in present:
                    self.assertIn(["ldd", "-r", str(targets[name])], commands)
                    self.assertIn(["readelf", "--dynamic", "--version-info", str(targets[name])], commands)

    def test_sdk_diagnostic_preserves_original_loader_path(self):
        _, observed, _ = self.sdk()
        self.assertTrue(observed)
        for entry in observed:
            self.assertEqual(entry["environment"]["LD_LIBRARY_PATH"], "inherited-sdk-loader-path")

    def test_non_linux_sdk_diagnostic_does_not_run_commands(self):
        result, observed, _ = self.sdk(system="Windows")
        self.assertEqual(result["commands"], [])
        self.assertEqual(observed, [])

    def doctor_with_diagnostic_error(self, dev_error=None):
        with tempfile.TemporaryDirectory(prefix="qcs-doctor-diagnostic-",
                dir=os.environ.get("QCOPILOTS_SEARCH_TEST_ROOT") or None) as temporary:
            reports = Path(temporary)
            with mock.patch.object(native.platform, "system", return_value="Linux"), \
                    mock.patch.object(native, "REPORTS", reports, create=True), \
                    mock.patch.object(native, "dev", side_effect=dev_error) as dev, \
                    mock.patch.object(native, "linux_sdk_diagnostics", side_effect=RuntimeError("Synthetic diagnostics failure")) as diagnose, \
                    mock.patch.object(native, "print", create=True), \
                    mock.patch.object(native.subprocess, "Popen") as spawn:
                if dev_error is None:
                    native.doctor()
                else:
                    with self.assertRaises(type(dev_error)) as caught:
                        native.doctor()
                    self.assertIs(caught.exception, dev_error)
                dev.assert_called_once_with("doctor")
                diagnose.assert_called_once()
                spawn.assert_not_called()

    def test_doctor_success_survives_sdk_diagnostic_exception(self):
        self.doctor_with_diagnostic_error()

    def test_doctor_rethrows_original_failure_after_sdk_diagnostic_exception(self):
        self.doctor_with_diagnostic_error(native.subprocess.CalledProcessError(23, ["fixture-dev", "doctor"]))




class PythonLicenseEvidenceTests(unittest.TestCase):
    program = "qcs-tests-license-materials"

    def inspect(self, output, system="Windows"):
        return EvidenceTests.inspect(self, output, name=self.program, system=system, raw=True)

    def test_verbose_license_cases_are_counted_without_qt_totals(self):
        output = ("test_materials (__main__.LicenseMaterialsTests.test_materials) ... ok\n"
                  "Ran 1 test in 0.001s\n\nOK\n")
        for system in ("Windows", "Linux", "Darwin"):
            with self.subTest(system=system):
                result = self.inspect(output, system)
                self.assertTrue(result["releaseComplete"], result["errors"])
                self.assertEqual(result["pythonTestTotals"]["passed"], 1)
                self.assertFalse(result["ctestPrograms"][0]["qtTest"])

    def test_license_summary_without_case_records_is_incomplete(self):
        self.assertFalse(self.inspect("Ran 32 tests in 0.001s\n\nOK\n")["releaseComplete"])

    def test_signing_skip_exemption_does_not_apply_to_license_program(self):
        output = signing_verbose([(SIGNING_SYMLINK_CASES[0], "skipped " + repr(SIGNING_SKIP_REASON))])
        result = self.inspect(output)
        self.assertFalse(result["releaseComplete"])
        self.assertEqual(result["pythonTestSkips"][0]["program"], self.program)
        self.assertEqual(result["pythonTestSkips"][0]["classification"], "requiredNotRun")


class PythonAcceptanceRunnerEvidenceTests(PythonLicenseEvidenceTests):
    program = "qcs-tests-acceptance-native"

    def test_verbose_license_cases_are_counted_without_qt_totals(self):
        rows = [f"{identifier.rsplit('.', 1)[-1]} ({identifier}) ... ok"
                for identifier in sorted(evidence.ACCEPTANCE_WINDOWS_CASES)]
        output = "\n".join(rows) + f"\nRan {len(rows)} tests in 0.001s\n\nOK\n"
        for system in ("Windows", "Linux", "Darwin"):
            with self.subTest(system=system):
                result = self.inspect(output, system)
                self.assertTrue(result["releaseComplete"], result["errors"])
                self.assertEqual(result["pythonTestTotals"]["passed"], len(rows))
                self.assertFalse(result["ctestPrograms"][0]["qtTest"])


class AcceptanceNativePortabilityEvidenceTests(unittest.TestCase):
    prefix = "qcs_test_portability_runners.PortabilityContracts."
    methods = (
        "test_windows_powershell_bridge_preserves_developer_arguments",
        "test_windows_acl_restoration_restores_effective_access",
        "test_windows_acl_comparison_rejects_semantic_changes",
    )
    reason = "Windows native PowerShell and NTFS ACL contract"

    def inspect(self, *, system="Linux", omitted=(), changed=None,
                program="qcs-tests-acceptance-native"):
        rows = [(evidence.ACCEPTANCE_WINDOWS_TOKEN_CASE, "ok")]
        for method in self.methods:
            if method in omitted:
                continue
            identifier = self.prefix + method
            status = "ok" if system == "Windows" else "skipped " + repr(self.reason)
            identifier, status = (changed or {}).get(method, (identifier, status))
            rows.append((identifier, status))
        skipped = sum(status.startswith("skipped ") for _, status in rows)
        output = "\n".join(f"{identifier.rsplit('.', 1)[-1]} ({identifier}) ... {status}"
                           for identifier, status in rows)
        output += f"\nRan {len(rows)} tests in 0.012s\n\n"
        output += (f"OK (skipped={skipped})" if skipped else "OK") + "\n"
        return EvidenceTests.inspect(self, output, name=program, system=system, raw=True)

    def test_exact_posix_skips_keep_raw_status_and_required_native_platform(self):
        for system in ("Linux", "Darwin"):
            with self.subTest(system=system):
                result = self.inspect(system=system)
                self.assertTrue(result["releaseComplete"], result["errors"])
                self.assertEqual(result["pythonTestTotals"],
                    {"run": 4, "passed": 1, "failed": 0, "skipped": 3, "notApplicable": 3})
                self.assertEqual({item["case"] for item in result["pythonTestSkips"]},
                                 {self.prefix + method for method in self.methods})
                for entry in result["pythonTestSkips"]:
                    self.assertEqual(entry["reason"], self.reason)
                    self.assertEqual(entry["classification"], "notApplicable")
                    self.assertEqual(entry["requiredNativePlatforms"], ["Windows"])
                for row in result["ctestPrograms"][0]["cases"][1:]:
                    self.assertEqual(row["status"], "skipped " + repr(self.reason))

    def test_windows_requires_all_three_actual_passes(self):
        result = self.inspect(system="Windows")
        self.assertTrue(result["releaseComplete"], result["errors"])
        self.assertEqual(result["pythonTestTotals"]["passed"], 4)
        for method in self.methods:
            for status in ("skipped " + repr(self.reason), "FAIL", "ERROR"):
                with self.subTest(method=method, status=status):
                    result = self.inspect(system="Windows", changed={method: (self.prefix + method, status)})
                    self.assertFalse(result["releaseComplete"])
                    self.assertEqual(result["pythonTestTotals"]["notApplicable"], 0)
                    self.assertTrue(any("Required native portability case did not pass" in error
                                        for error in result["errors"]))

    def test_omitted_platform_case_cannot_hide_behind_consistent_totals(self):
        for system in ("Windows", "Linux", "Darwin"):
            for method in self.methods:
                with self.subTest(system=system, method=method):
                    result = self.inspect(system=system, omitted=(method,))
                    self.assertFalse(result["releaseComplete"])
                    self.assertTrue(any("Required native portability case is missing" in error
                                        for error in result["errors"]))

    def test_changed_reason_module_class_or_method_is_required_not_run(self):
        method = self.methods[0]
        identifier = self.prefix + method
        variants = [(identifier, "skipped " + repr(self.reason + " unavailable")),
                    (identifier, "skipped 'Windows only'"),
                    ("__main__.PortabilityContracts." + method, "skipped " + repr(self.reason)),
                    ("qcs_test_portability_runners.OtherTests." + method, "skipped " + repr(self.reason)),
                    (self.prefix + "test_windows_other_contract", "skipped " + repr(self.reason))]
        for system in ("Linux", "Darwin"):
            for changed in variants:
                with self.subTest(system=system, changed=changed):
                    result = self.inspect(system=system, changed={method: changed})
                    self.assertFalse(result["releaseComplete"])
                    self.assertEqual(result["pythonTestTotals"]["notApplicable"], 2)
                    self.assertTrue(any(item["classification"] == "requiredNotRun"
                                        for item in result["pythonTestSkips"]))

    def test_platform_exception_cannot_apply_to_another_program(self):
        for system in ("Linux", "Darwin"):
            for program in ("qcs-tests-runner", "qcs-tests-path-budget", "qcs-tests-license-materials"):
                with self.subTest(system=system, program=program):
                    result = self.inspect(system=system, program=program)
                    self.assertFalse(result["releaseComplete"])
                    self.assertEqual(result["pythonTestTotals"]["notApplicable"], 0)

    def test_parser_registry_matches_real_acceptance_suite_case_ids(self):
        directory = Path(__file__).resolve().parents[2] / "tools" / "acceptance"
        spec = importlib.util.spec_from_file_location("qcs_ci_acceptance_registry", directory / "qcs-test-runners.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        # Import definitions and enumerate the suite without executing native ACL operations.
        contracts = module.load_script("qcs-test-portability-runners.py")
        actual_ids = {item.id() for item in unittest.defaultTestLoader.loadTestsFromTestCase(
            contracts.PortabilityContracts)}
        expected = {self.prefix + name for name in self.methods}
        self.assertTrue(expected <= actual_ids)
        self.assertEqual(evidence.ACCEPTANCE_WINDOWS_CASES,
                         expected | {evidence.ACCEPTANCE_WINDOWS_TOKEN_CASE})
        self.assertEqual(contracts.WINDOWS_NATIVE_CASES, expected)
        self.assertEqual(contracts.WINDOWS_SKIP_REASON, self.reason)
        self.assertEqual(evidence.ACCEPTANCE_WINDOWS_SKIP_REASON, self.reason)


class AcceptanceTokenContextEvidenceTests(unittest.TestCase):
    case = ("qcs_test_token_context.TokenContextContracts."
            "test_windows_owned_child_preserves_parent_and_contains_descendants")
    reason = "Windows native process token and Job Object contract"

    def inspect(self, *, system="Linux", identifier=None, status=None, omitted=False,
                duplicate=False, program="qcs-tests-acceptance-native"):
        rows = [(case, "ok") for case in sorted(evidence.ACCEPTANCE_WINDOWS_CASES - {self.case})]
        if not omitted:
            case_status = status if status is not None else (
                "ok" if system == "Windows" else "skipped " + repr(self.reason))
            row = (identifier or self.case, case_status)
            rows.append(row)
            if duplicate:
                rows.append(row)
        skipped = sum(value.startswith("skipped ") for _, value in rows)
        output = "\n".join(f"{case.rsplit('.', 1)[-1]} ({case}) ... {value}" for case, value in rows)
        output += f"\nRan {len(rows)} tests in 0.012s\n\n"
        output += (f"OK (skipped={skipped})" if skipped else "OK") + "\n"
        return EvidenceTests.inspect(self, output, name=program, system=system, raw=True)

    def test_exact_posix_token_skip_retains_raw_reason_and_requires_windows(self):
        for system in ("Linux", "Darwin"):
            with self.subTest(system=system):
                result = self.inspect(system=system)
                self.assertTrue(result["releaseComplete"], result["errors"])
                self.assertEqual(result["pythonTestTotals"],
                    {"run": 4, "passed": 3, "failed": 0, "skipped": 1, "notApplicable": 1})
                self.assertEqual(result["pythonTestSkips"], [{
                    "program": "qcs-tests-acceptance-native", "case": self.case,
                    "reason": self.reason, "classification": "notApplicable",
                    "requiredNativePlatforms": ["Windows"]}])
                self.assertEqual(result["ctestPrograms"][0]["cases"][-1]["status"],
                                 "skipped " + repr(self.reason))

    def test_windows_token_case_requires_actual_pass_even_when_other_native_cases_pass(self):
        result = self.inspect(system="Windows")
        self.assertTrue(result["releaseComplete"], result["errors"])
        self.assertEqual(result["pythonTestTotals"]["passed"], 4)
        for status in ("skipped " + repr(self.reason), "skipped 'required Windows API unavailable'", "FAIL", "ERROR"):
            with self.subTest(status=status):
                result = self.inspect(system="Windows", status=status)
                self.assertFalse(result["releaseComplete"])
                self.assertEqual(result["pythonTestTotals"]["notApplicable"], 0)
                self.assertTrue(any(error.endswith("Required native portability case did not pass: " + self.case)
                                    for error in result["errors"]))

    def test_missing_or_duplicate_token_case_is_not_hidden_by_matching_totals(self):
        for system in ("Windows", "Linux", "Darwin"):
            for alteration in ({"omitted": True}, {"duplicate": True}):
                with self.subTest(system=system, alteration=alteration):
                    result = self.inspect(system=system, **alteration)
                    self.assertFalse(result["releaseComplete"])
                    self.assertTrue(any("missing: " + self.case in error or "duplicate case records" in error
                                        for error in result["errors"]))

    def test_token_skip_requires_exact_case_reason_platform_and_program(self):
        for system in ("Linux", "Darwin"):
            for changes in ({"status": "skipped " + repr(self.reason + " unavailable")},
                            {"status": "skipped " + repr(evidence.ACCEPTANCE_WINDOWS_SKIP_REASON)},
                            {"identifier": self.case.replace("qcs_test_token_context", "__main__")},
                            {"identifier": self.case.replace("TokenContextContracts", "OtherContracts")},
                            {"identifier": self.case + "_other"},
                            {"program": "qcs-tests-path-budget"}):
                with self.subTest(system=system, changes=changes):
                    result = self.inspect(system=system, **changes)
                    self.assertFalse(result["releaseComplete"])
                    self.assertEqual(result["pythonTestTotals"]["notApplicable"], 0)
                    self.assertTrue(any(row["classification"] == "requiredNotRun"
                                        for row in result["pythonTestSkips"]))
        result = self.inspect(system="FreeBSD", status="skipped " + repr(self.reason))
        self.assertFalse(result["releaseComplete"])
        self.assertEqual(result["pythonTestTotals"]["notApplicable"], 0)

    def test_token_registry_matches_real_native_method_without_executing_it(self):
        directory = Path(__file__).resolve().parents[2] / "tools" / "acceptance"
        spec = importlib.util.spec_from_file_location("qcs_ci_token_registry", directory / "qcs-test-runners.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertIn("qcs-test-token-context.py", module.SCRIPTS)
        contracts = module.load_script("qcs-test-token-context.py")
        actual_ids = {item.id() for item in unittest.defaultTestLoader.loadTestsFromTestCase(
            contracts.TokenContextContracts)}
        self.assertIn(self.case, actual_ids)
        self.assertEqual(evidence.ACCEPTANCE_WINDOWS_TOKEN_CASE, self.case)
        self.assertEqual(evidence.ACCEPTANCE_WINDOWS_TOKEN_SKIP_REASON, self.reason)
        self.assertEqual(evidence.ACCEPTANCE_WINDOWS_SKIP_REASONS[self.case], self.reason)
        self.assertEqual(contracts.WINDOWS_NATIVE_CASES, {self.case})
        self.assertEqual(contracts.WINDOWS_SKIP_REASON, self.reason)


class PythonTestRunnerEvidenceTests(PythonLicenseEvidenceTests):
    program = "qcs-tests-runner"

    def test_verbose_license_cases_are_counted_without_qt_totals(self):
        output = ("test_real_windows_exclusive_handle_still_fails_cleanup "
                  "(__main__.TestRunner.test_real_windows_exclusive_handle_still_fails_cleanup) ... ok\n"
                  "Ran 1 test in 0.001s\n\nOK\n")
        for system in ("Windows", "Linux", "Darwin"):
            with self.subTest(system=system):
                result = self.inspect(output, system)
                self.assertTrue(result["releaseComplete"], result["errors"])
                self.assertEqual(result["pythonTestTotals"]["passed"], 1)
                self.assertFalse(result["ctestPrograms"][0]["qtTest"])


class RunnerHandlePlatformEvidenceTests(unittest.TestCase):
    case = "__main__.TestRunner.test_real_windows_exclusive_handle_still_fails_cleanup"
    reason = "Windows exclusive file handles require Windows"

    def inspect(self, *, system="Linux", reason=None, identifier=None, status=None,
                omitted=False, program="qcs-tests-runner"):
        identifier = identifier or self.case
        status = status or "skipped " + repr(self.reason if reason is None else reason)
        rows = ["test_other (__main__.TestRunner.test_other) ... ok"]
        if not omitted:
            rows.append(f"{identifier.rsplit('.', 1)[-1]} ({identifier}) ... {status}")
        skipped = int(not omitted and status.startswith("skipped "))
        output = "\n".join(rows) + f"\nRan {len(rows)} tests in 0.012s\n\n"
        output += (f"OK (skipped={skipped})" if skipped else "OK") + "\n"
        return EvidenceTests.inspect(self, output, name=program, system=system, raw=True)

    def test_exact_posix_skip_is_separate_and_keeps_the_raw_reason(self):
        for system in ("Linux", "Darwin"):
            with self.subTest(system=system):
                result = self.inspect(system=system)
                self.assertTrue(result["releaseComplete"], result["errors"])
                self.assertEqual(result["pythonTestTotals"],
                    {"run": 2, "passed": 1, "failed": 0, "skipped": 1, "notApplicable": 1})
                self.assertEqual(result["pythonTestSkips"], [{"program": "qcs-tests-runner",
                    "case": self.case, "reason": self.reason, "classification": "notApplicable",
                    "requiredNativePlatforms": ["Windows"]}])
                record = result["ctestPrograms"][0]["cases"][1]
                self.assertEqual(record["status"], "skipped " + repr(self.reason))

    def test_windows_requires_actual_pass_instead_of_skip_or_omission(self):
        for omitted in (False, True):
            with self.subTest(omitted=omitted):
                result = self.inspect(system="Windows", omitted=omitted)
                self.assertFalse(result["releaseComplete"])
                self.assertEqual(result["pythonTestTotals"]["notApplicable"], 0)
                self.assertTrue(any("Required native cleanup case" in error for error in result["errors"]))
        result = self.inspect(system="Windows", status="ok")
        self.assertTrue(result["releaseComplete"], result["errors"])
        self.assertEqual(result["pythonTestTotals"]["passed"], 2)

    def test_changed_skip_reason_or_identifier_is_required_not_run(self):
        for arguments in ({"reason": "Windows handles unavailable"},
                          {"reason": self.reason + " (unrecognized reason)"},
                          {"identifier": "__main__.TestRunner.test_other_windows_handle"},
                          {"identifier": "__main__.OtherTest.test_real_windows_exclusive_handle_still_fails_cleanup"}):
            for system in ("Linux", "Darwin"):
                with self.subTest(arguments=arguments, system=system):
                    result = self.inspect(system=system, **arguments)
                    self.assertFalse(result["releaseComplete"])
                    self.assertEqual(result["pythonTestTotals"]["notApplicable"], 0)
                    self.assertEqual(result["pythonTestSkips"][0]["classification"], "requiredNotRun")

    def test_exception_cannot_apply_to_other_programs(self):
        for program in ("qcs-tests-acceptance-native", "qcs-tests-path-budget", "qcs-tests-license-materials"):
            for system in ("Linux", "Darwin"):
                result = self.inspect(system=system, program=program)
                self.assertFalse(result["releaseComplete"])
                self.assertEqual(result["pythonTestTotals"]["notApplicable"], 0)


class PythonPathBudgetEvidenceTests(PythonLicenseEvidenceTests):
    program = "qcs-tests-path-budget"


class PythonTempPathsEvidenceTests(PythonLicenseEvidenceTests):
    program = "qcs-tests-temp-paths"


class NativeStageDependencyTests(unittest.TestCase):
    """Verify workflow ordering and default failure propagation before upload."""

    ACTIONS = ("initialize", "bootstrap", "doctor", "configure", "build", "models",
               "test", "install", "package", "installers", "products")

    def setUp(self):
        self.workflow = (native.SOURCE / ".github/workflows/qcs-native.yml").read_text(encoding="utf-8")
        self.native_job = self.workflow.split("  native:\n", 1)[1].split("  required:\n", 1)[0]
        self.steps = self.native_job.split("      - name: ")[1:]

    def step(self, action):
        return next(item for item in self.steps if f"qcs-native-ci.py {action}\n" in item)

    def test_native_steps_run_in_order_and_inherit_failure_propagation(self):
        observed = re.findall(r"run: python \.github/scripts/qcs-native-ci\.py (\w+)", self.native_job)
        self.assertEqual(observed, list(self.ACTIONS))
        self.assertNotIn("continue-on-error:", self.native_job)
        always_steps = [step.splitlines()[0] for step in self.steps if "always()" in step]
        self.assertEqual(always_steps, ["Record Windows ROCm disk space after SDK setup"])
        self.assertNotIn("!cancelled()", self.native_job)
        for action in self.ACTIONS:
            with self.subTest(action=action):
                conditions = re.findall(r"^        if: (.+)$", self.step(action), re.MULTILINE)
                self.assertEqual(conditions, ["runner.os == 'Windows' || runner.os == 'Linux'"]
                                 if action == "installers" else [])

    def test_upload_is_only_after_all_real_model_and_package_checks(self):
        uploads = [step for step in self.steps if "uses: actions/upload-artifact@" in step]
        self.assertEqual(len(uploads), 1)
        upload = uploads[0]
        self.assertGreater(self.steps.index(upload), self.steps.index(self.step("products")))
        self.assertIsNone(re.search(r"^        if:", upload, re.MULTILINE), "Upload must inherit success()")
        self.assertIn("path: ${{ steps.packages.outputs.paths }}", upload)
        self.assertIn("id: packages", self.step("products"))
        self.assertIn("overwrite: false", upload)
        self.assertIn("if-no-files-found: error", upload)
        self.assertIn("qcs-packages-${{ matrix.id }}-${{ env.QCOPILOTS_SEARCH_CANDIDATE_HEAD }}-attempt${{ github.run_attempt }}", upload)
        self.assertNotIn("/reports", upload)
        self.assertNotIn("/pkg/", upload)

    def test_matrix_comes_from_shared_manifest_and_keeps_independent_variants(self):
        self.assertIn("run: python3 .github/scripts/qcs-native-ci.py matrix", self.workflow)
        self.assertIn("matrix: ${{ fromJSON(needs.matrix.outputs.configurations) }}", self.native_job)
        self.assertIn("container: ${{ matrix.container }}", self.native_job)
        self.assertIn("fail-fast: false", self.native_job)
        self.assertIn("QCOPILOTS_SEARCH_CI_ID: ${{ matrix.id }}", self.native_job)
        with mock.patch.object(native.sys, "argv", ["synthetic-native-ci", "matrix"]), \
                mock.patch.dict(native.os.environ, {"GITHUB_OUTPUT": ""}), \
                mock.patch("sys.stdout", new_callable=io.StringIO) as output:
            native.main()
        rows = json.loads(output.getvalue())["include"]
        self.assertEqual({row["id"] for row in rows}, set(native.variants()))
        self.assertTrue(all("container" in row for row in rows))

    def test_required_job_checks_native_result_and_immutable_artifacts(self):
        required = self.workflow.split("  required:\n", 1)[1]
        self.assertIn("if: always()", required)
        self.assertIn("needs: [matrix, native]", required)
        self.assertIn('test "$MATRIX_RESULT" = success && test "$NATIVE_RESULT" = success', required)
        self.assertIn("qcs-artifact-selection.py collect", required)
        self.assertIn(' --attempt "$GITHUB_RUN_ATTEMPT"', required)
        self.assertNotIn("actions/upload-artifact@", required)
        self.assertNotIn("continue-on-error:", required)


class NativePackageProductsTests(unittest.TestCase):
    def package_set(self, root, identifier="windows-x64-cpu", head="a" * 40, attempt="2"):
        selected = native.variants()[identifier]
        suffixes = {"NSIS": ".exe", "ZIP": ".zip", "DEB": ".deb", "TGZ": ".tar.gz", "DragNDrop": ".dmg"}
        files = []
        for generator in selected["generators"]:
            path = root / f"qcs-0.1.0-{identifier}-{head[:12]}-attempt{attempt}{suffixes[generator]}"
            path.write_bytes(b"synthetic package bytes for output contract only")
            files.append(path)
        return selected, files

    def test_every_variant_requires_its_installer_and_portable_formats(self):
        for identifier in native.variants():
            with self.subTest(identifier=identifier), tempfile.TemporaryDirectory(prefix="qcs-package-set-",
                    dir=os.environ.get("QCOPILOTS_SEARCH_TEST_ROOT") or None) as temporary:
                root = Path(temporary)
                selected, files = self.package_set(root, identifier)
                self.assertEqual(set(native.package_files(root, selected, "a" * 40, "2")), set(files))
                files[0].unlink()
                with self.assertRaisesRegex(RuntimeError, "Package formats differ"):
                    native.package_files(root, selected, "a" * 40, "2")

    def test_wrong_candidate_attempt_backend_or_extra_format_is_rejected(self):
        for mutation in ("candidate", "attempt", "backend", "extra", "sources", "symbols", "checksums", "duplicate", "empty"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory(prefix="qcs-package-invalid-",
                    dir=os.environ.get("QCOPILOTS_SEARCH_TEST_ROOT") or None) as temporary:
                root = Path(temporary)
                selected, files = self.package_set(root)
                if mutation == "candidate":
                    files[0].rename(files[0].with_name(files[0].name.replace("a" * 12, "b" * 12)))
                elif mutation == "attempt":
                    files[0].rename(files[0].with_name(files[0].name.replace("attempt2", "attempt1")))
                elif mutation == "backend":
                    files[0].rename(files[0].with_name(files[0].name.replace("-cpu-", "-vulkan-")))
                elif mutation == "extra":
                    (root / "qcs-report.json").write_text("{}", encoding="utf-8")
                elif mutation in ("sources", "symbols", "checksums"):
                    name = {"sources": "qcs-sources.tar.gz", "symbols": "qcs-symbols.zip",
                            "checksums": "qcs-SHA256SUMS"}[mutation]
                    (root / name).write_bytes(b"not a user package")
                elif mutation == "duplicate":
                    files[0].with_name(files[0].name.replace("0.1.0", "0.2.0")).write_bytes(b"duplicate")
                else:
                    files[0].write_bytes(b"")
                with self.assertRaises(RuntimeError):
                    native.package_files(root, selected, "a" * 40, "2")

    def test_rocm_setup_archive_replaces_only_the_installer(self):
        with tempfile.TemporaryDirectory(prefix="qcs-rocm-setup-",
                dir=os.environ.get("QCOPILOTS_SEARCH_TEST_ROOT") or None) as temporary:
            root = Path(temporary)
            selected, files = self.package_set(root, "windows-x64-rocm")
            installer = next(path for path in files if path.suffix == ".exe")
            portable = next(path for path in files if path.suffix == ".zip")
            setup = installer.with_name(installer.stem + "-setup.zip")
            installer.rename(setup)
            self.assertEqual(set(native.package_files(root, selected, "a" * 40, "2")), {setup, portable})
            installer.write_bytes(b"a second installer must not be uploaded")
            with self.assertRaisesRegex(RuntimeError, "Package formats differ"):
                native.package_files(root, selected, "a" * 40, "2")
            installer.unlink()
            portable.unlink()
            with self.assertRaisesRegex(RuntimeError, "Package formats differ"):
                native.package_files(root, selected, "a" * 40, "2")

    def test_setup_archive_is_rejected_for_every_other_variant(self):
        for identifier in set(native.variants()) - {"windows-x64-rocm"}:
            with self.subTest(identifier=identifier), tempfile.TemporaryDirectory(prefix="qcs-setup-scope-",
                    dir=os.environ.get("QCOPILOTS_SEARCH_TEST_ROOT") or None) as temporary:
                root = Path(temporary)
                selected, files = self.package_set(root, identifier)
                if any(path.suffix == ".exe" for path in files):
                    next(path for path in files if path.suffix == ".exe").unlink()
                setup = root / f"qcs-0.1.0-{identifier}-{'a' * 12}-attempt2-setup.zip"
                setup.write_bytes(b"unsupported installer fallback")
                with self.assertRaisesRegex(RuntimeError, "Unexpected package output"):
                    native.package_files(root, selected, "a" * 40, "2")

    def test_installer_and_portable_versions_cannot_be_mixed(self):
        for fallback in (False, True):
            with self.subTest(fallback=fallback), tempfile.TemporaryDirectory(prefix="qcs-version-pair-",
                    dir=os.environ.get("QCOPILOTS_SEARCH_TEST_ROOT") or None) as temporary:
                root = Path(temporary)
                selected, files = self.package_set(root, "windows-x64-rocm")
                installer = next(path for path in files if path.suffix == ".exe")
                name = installer.name.replace("0.1.0", "0.2.0")
                if fallback:
                    name = name.removesuffix(".exe") + "-setup.zip"
                installer.rename(installer.with_name(name))
                with self.assertRaisesRegex(RuntimeError, "same version and candidate identity"):
                    native.package_files(root, selected, "a" * 40, "2")

    def test_two_gib_limit_is_strict_without_allocating_large_files(self):
        with tempfile.TemporaryDirectory(prefix="qcs-package-size-",
                dir=os.environ.get("QCOPILOTS_SEARCH_TEST_ROOT") or None) as temporary:
            root = Path(temporary)
            selected, files = self.package_set(root)
            real_stat = Path.stat
            for size in (2 * 1024**3 - 1, 2 * 1024**3, 2 * 1024**3 + 1):
                def sized_stat(path, *args, **kwargs):
                    actual = real_stat(path, *args, **kwargs)
                    if path == files[0]:
                        values = list(actual)
                        values[6] = size
                        return os.stat_result(values)
                    return actual
                with self.subTest(size=size), mock.patch.object(Path, "stat", sized_stat):
                    if size < 2 * 1024**3:
                        self.assertEqual(set(native.package_files(root, selected, "a" * 40, "2")), set(files))
                    else:
                        with self.assertRaisesRegex(RuntimeError, "below 2 GiB"):
                            native.package_files(root, selected, "a" * 40, "2")

    def test_symbolic_package_cannot_enter_upload_list(self):
        with tempfile.TemporaryDirectory(prefix="qcs-package-link-",
                dir=os.environ.get("QCOPILOTS_SEARCH_TEST_ROOT") or None) as temporary:
            root = Path(temporary)
            selected, files = self.package_set(root)
            real_is_symlink = Path.is_symlink
            with mock.patch.object(Path, "is_symlink", lambda path: path == files[0] or real_is_symlink(path)):
                with self.assertRaisesRegex(RuntimeError, "Not an ordinary package file"):
                    native.package_files(root, selected, "a" * 40, "2")

    def test_cpack_working_tree_and_unrelated_side_files_never_enter_upload_list(self):
        with tempfile.TemporaryDirectory(prefix="qcs-package-whitelist-",
                dir=os.environ.get("QCOPILOTS_SEARCH_TEST_ROOT") or None) as temporary:
            root = Path(temporary)
            selected, files = self.package_set(root)
            working_tree = root / "_CPack_Packages"
            working_tree.mkdir()
            (working_tree / "transient-output").write_bytes(b"CPack working data")
            (root / "SHA256SUMS").write_bytes(b"diagnostic checksums")
            self.assertEqual(set(native.package_files(root, selected, "a" * 40, "2")), set(files))

    def test_products_emits_only_verified_paths_and_hashes_to_native_logs(self):
        with tempfile.TemporaryDirectory(prefix="qcs-products-",
                dir=os.environ.get("QCOPILOTS_SEARCH_TEST_ROOT") or None) as temporary:
            root = Path(temporary)
            packages = root / "pkg/Release"
            packages.mkdir(parents=True)
            selected, files = self.package_set(packages)
            output_path, summary_path = root / "github-output", root / "summary"
            with mock.patch.object(native, "ROOT", root, create=True), \
                    mock.patch.object(native, "variant", return_value=selected), \
                    mock.patch.object(native, "candidate_head", return_value="a" * 40), \
                    mock.patch.dict(native.os.environ, {"GITHUB_RUN_ATTEMPT": "2",
                        "GITHUB_OUTPUT": str(output_path), "GITHUB_STEP_SUMMARY": str(summary_path)}), \
                    mock.patch("sys.stdout", new_callable=io.StringIO) as output:
                emitted = native.products()
            self.assertEqual(set(emitted), set(files))
            document = json.loads(output.getvalue())
            self.assertEqual(document["configuration"], selected["id"])
            self.assertEqual({record["name"] for record in document["packages"]}, {path.name for path in files})
            for record in document["packages"]:
                raw = (packages / record["name"]).read_bytes()
                self.assertEqual(record["bytes"], len(raw))
                self.assertEqual(record["sha256"], native.hashlib.sha256(raw).hexdigest())
                self.assertIn(record["sha256"], summary_path.read_text(encoding="utf-8"))
            self.assertEqual(output_path.read_text(encoding="utf-8").splitlines(),
                             ["paths<<QCS_PACKAGE_PATHS", *[str(path) for path in emitted], "QCS_PACKAGE_PATHS"])


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    unittest.main()
