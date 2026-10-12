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

SIGNING_PROGRAM = "qcopilots-search-tests-macos-signing"
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


def signing_verbose(rows=None, *, run=None, footer=None):
    """Synthetic unittest verbose output, never a signing execution report."""
    if rows is None:
        rows = [(name, "ok") for name in SIGNING_CASES]
    if footer is None:
        skipped = sum(status.startswith("skipped ") for _, status in rows)
        footer = f"OK (skipped={skipped})" if skipped else "OK"
    return ("\n".join(f"{name} (__main__.SigningTests.{name}) ... {status}" for name, status in rows)
            + f"\n\n----------------------------------------------------------------------\nRan {len(rows) if run is None else run} tests in 0.012s\n\n{footer}\n")


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
            if name == SIGNING_PROGRAM:
                continue
            self.assertTrue(self.inspect("success\n", name=name)["releaseComplete"])

    def test_python_inference_install_layout_is_not_a_qt_test(self):
        self.assertTrue(self.inspect("Ran 8 tests\nOK\n",
            name="qcopilots-search-tests-inference-install-layout")["releaseComplete"])
        self.assertFalse(self.inspect("success\n",
            name="qcopilots-search-tests-backend-layout")["releaseComplete"])

    def test_python_macos_signing_is_not_a_qt_test(self):
        result = self.inspect(signing_verbose(), name=SIGNING_PROGRAM, raw=True)
        self.assertTrue(result["releaseComplete"])
        self.assertFalse(result["ctestPrograms"][0]["qtTest"])
        self.assertTrue(result["ctestPrograms"][0]["pythonTest"])

    def test_ctest_failure_error_or_skip_cannot_pass(self):
        for kind in ("failure", "error", "skipped"):
            self.assertFalse(self.inspect(failure=kind)["releaseComplete"])


class PythonSigningEvidenceTests(unittest.TestCase):
    def inspect(self, output=None, *, system="Windows"):
        return EvidenceTests.inspect(self, signing_verbose() if output is None else output,
                                     name=SIGNING_PROGRAM, system=system, raw=True)

    def skipped_rows(self, *, reason=SIGNING_SKIP_REASON):
        return [(name, "skipped " + repr(reason) if name in SIGNING_SYMLINK_CASES else "ok")
                for name in SIGNING_CASES]

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


class NativeTestFlowTests(unittest.TestCase):
    def run_test_action(self, failure=None):
        with tempfile.TemporaryDirectory(prefix="qcopilots-search-ci-test-flow-",
                dir=os.environ.get("QCOPILOTS_SEARCH_TEST_ROOT")) as temporary:
            root = Path(temporary)
            reports = root / "reports"
            inventory = json.dumps({"kind": "ctestInfo", "tests": [{"name": "qcopilots-search-tests-core"}]})
            with mock.patch.object(native.sys, "argv", ["synthetic-native-ci", "test"]), \
                    mock.patch.object(native, "ROOT", root, create=True), \
                    mock.patch.object(native, "REPORTS", reports, create=True), \
                    mock.patch.object(native.platform, "machine", return_value="x86_64"), \
                    mock.patch.dict(native.os.environ, {"QCOPILOTS_SEARCH_CI_ARCH": "x86_64"}), \
                    mock.patch.object(native, "run", return_value=native.subprocess.CompletedProcess([], 0, stdout=inventory)), \
                    mock.patch.object(native, "dev", side_effect=failure) as dev, \
                    mock.patch.object(native, "test_evidence") as acceptance, \
                    mock.patch.object(native, "gui_failure_diagnostics", return_value={"attempts": []}) as gui_diagnostics, \
                    mock.patch.object(native, "linux_failure_diagnostics", return_value={"commands": []}) as linux_diagnostics, \
                    mock.patch.object(native.subprocess, "Popen") as spawn:
                if failure is None:
                    native.main()
                    acceptance.assert_called_once_with(require_complete=True)
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
                self.assertEqual(arguments.count("--fail-fast"), 1)
                self.assertIn("--keep-failed-tests", arguments)
                self.assertEqual(arguments[arguments.index("--report-dir") + 1], str(reports / "tests"))
                self.assertTrue(dev.call_args.kwargs["display"])
                spawn.assert_not_called()
            self.assertEqual((reports / "tests/ctest-inventory.json").read_text(encoding="utf-8"), inventory)

    def test_fail_fast_invocation_still_requires_complete_evidence_after_success(self):
        self.run_test_action()

    def test_fail_fast_failure_preserves_original_error_without_acceptance_success(self):
        self.run_test_action(native.subprocess.CalledProcessError(17, ["synthetic-dev", "test"]))


class NativeIdentityTests(unittest.TestCase):
    def test_pr_candidate_head_is_distinct_from_merge_checkout(self):
        with tempfile.TemporaryDirectory(prefix="qcopilots-search-event-") as directory:
            path = Path(directory) / "event.json"
            path.write_text(json.dumps({"pull_request": {"head": {"sha": "a" * 40}}}), encoding="utf-8")
            with mock.patch.dict(os.environ, {"GITHUB_EVENT_PATH": str(path), "GITHUB_SHA": "b" * 40}):
                self.assertEqual(native.candidate_head(), "a" * 40)

    def test_non_pr_candidate_uses_actual_trigger_commit(self):
        with mock.patch.dict(os.environ, {"GITHUB_EVENT_PATH": "", "GITHUB_SHA": "b" * 40}):
            self.assertEqual(native.candidate_head(), "b" * 40)

    def test_crash_collection_excludes_other_apps_old_files_and_large_dumps(self):
        with tempfile.TemporaryDirectory(prefix="qcopilots-search-crash-") as temporary:
            root = Path(temporary)
            source = root / "diagnostics"
            source.mkdir()
            for name in ("qcopilots-search-tests-gui.ips", "qcopilots-search-old.crash", "other-application.ips", "qcopilots-search.core"):
                (source / name).write_text("synthetic diagnostic fixture", encoding="utf-8")
            os.utime(source / "qcopilots-search-old.crash", (1, 1))
            with (source / "qcopilots-search-large.ips").open("wb") as stream:
                stream.truncate(4 * 1024**2 + 1)
            result = native.collect_crash_reports([source], root / "collected", 2)
            self.assertEqual([item["name"] for item in result["files"]], ["0-qcopilots-search-tests-gui.ips"])
            self.assertEqual(len(result["unavailable"]), 1)
            self.assertEqual(len(list((root / "collected").iterdir())), 1)

    def test_absent_crash_report_is_not_a_success_claim(self):
        with tempfile.TemporaryDirectory(prefix="qcopilots-search-crash-") as temporary:
            result = native.collect_crash_reports([Path(temporary) / "missing"], Path(temporary) / "collected", 0)
            self.assertEqual(result["files"], [])
            self.assertIn("does not prove", result["note"])


class GuiDiagnosticsTests(unittest.TestCase):
    def replay(self, *, failed=True, system="Darwin", crash=False, timeout=False, exited=True):
        with tempfile.TemporaryDirectory(prefix="qcopilots-search-debug-test-") as temporary:
            root = Path(temporary)
            junit = root / "ctest.xml"
            xml = ET.Element("testsuite")
            case = ET.SubElement(xml, "testcase", name="qcopilots-search-tests-gui")
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
                self.assertTrue(Path(arguments["cwd"]).is_relative_to(root / "test-runs"))
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
        with tempfile.TemporaryDirectory(prefix="qcopilots-search-linux-diagnostic-",
                dir=os.environ.get("QCOPILOTS_SEARCH_TEST_ROOT")) as temporary:
            root = Path(temporary)
            qt = root / "qt"
            prefix = qt / "6.12.0/gcc_64"
            program = root / "release/bin/qcopilots-search-tests-gui"
            targets = [program, prefix / "lib/libavutil.so.61",
                       prefix / "plugins/platforms/libqxcb.so", prefix / "lib/libQt6XcbQpa.so.6",
                       prefix / "lib/libQt6FFmpegStub-ssl.so.6",
                       prefix / "lib/libQt6FFmpegStub-crypto.so.6.12.0"]
            for target in targets:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"Synthetic loader diagnostic fixture, not an executable")
            junit = root / "ctest.xml"
            document = ET.Element("testsuite")
            case = ET.SubElement(document, "testcase", name="qcopilots-search-tests-gui")
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
            root / "release/bin", root / "release/lib", prefix / "bin", root / "bootstrap/nsis"))
        expected_path += os.pathsep + "original-tool-path"
        for command in commands:
            environment = command["environment"]
            self.assertEqual(environment["PATH"], expected_path)
            self.assertEqual(environment["LD_LIBRARY_PATH"], "original-loader-path")
            self.assertEqual(environment["QT_PLUGIN_PATH"], "original-plugin-path")
            self.assertEqual(environment["QT_QPA_PLATFORM"], "xcb")
            self.assertTrue(Path(environment["QCOPILOTS_SEARCH_TEST_ROOT"]).is_relative_to(root / "test-runs"))
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
        with tempfile.TemporaryDirectory(prefix="qcopilots-search-linux-bounded-",
                dir=os.environ.get("QCOPILOTS_SEARCH_TEST_ROOT")) as temporary:
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
        with tempfile.TemporaryDirectory(prefix="qcopilots-search-sdk-diagnostic-",
                dir=os.environ.get("QCOPILOTS_SEARCH_TEST_ROOT")) as temporary:
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
        with tempfile.TemporaryDirectory(prefix="qcopilots-search-doctor-diagnostic-",
                dir=os.environ.get("QCOPILOTS_SEARCH_TEST_ROOT")) as temporary:
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
