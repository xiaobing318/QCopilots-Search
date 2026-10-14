#!/usr/bin/env python3
"""Isolated contract tests. They never execute the application or CTest."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.dont_write_bytecode = True
spec = importlib.util.spec_from_file_location("qcs_candidate", Path(__file__).with_name("qcs-candidate-ctest-run.py"))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class CandidateContracts(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="qcs-ct-test-", dir=os.environ.get("QCOPILOTS_SEARCH_TEST_ROOT") or None)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.source = self.root / "源码 with spaces"
        (self.source / "tools").mkdir(parents=True)
        self.launcher = self.source / "tools/qcs-test-runner.py"
        self.launcher.write_text("# isolation launcher fixture\n", encoding="utf-8")
        self.binary = self.root / "qcs-tests-fixture.exe"
        self.binary.write_bytes(b"not executed")
        self.binaries = {str(self.binary): module.file_info(self.binary)}

    def command(self, payload):
        return {"name": "qcs-tests-fixture", "command": [sys.executable, str(self.launcher),
                "--label", "qcs-tests-fixture", "--root", str(self.root / "runs"), "--", *payload]}

    def test_complete_selection_rejects_filtered_missing_or_unexecuted_evidence(self):
        names = ["qcs-tests-unit", "qcs-tests-real-model"]
        selection = {"schemaVersion": 1, "kind": "ctest-selection", "labelFilter": None, "filters": [], "registeredTests": names, "selectedTests": names,
                     "excludedTests": [], "counts": {"registered": 2, "selected": 2, "excluded": 0},
                     "passed": True, "fullSuitePassed": True, "status": "passed", "exitCode": 0,
                     "releaseComplete": False}
        module.validate_full_selection(selection, names)
        for changes in ({"labelFilter": "unit"}, {"filters": ["-LE", "^slow$"]},
                        {"selectedTests": names[:1]}, {"registeredTests": names[:1]},
                        {"excludedTests": names[1:]}, {"counts": {"registered": 2, "selected": 1, "excluded": 1}},
                        {"passed": False}, {"fullSuitePassed": False}, {"status": "running"},
                        {"exitCode": 1}, {"releaseComplete": True}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                module.validate_full_selection({**selection, **changes}, names)
        with self.assertRaises(ValueError):
            module.validate_full_selection({}, names)

    def test_native_wrapper_binds_interpreter_launcher_and_exact_binary(self):
        bound = {}
        item = module.inventory_binding(self.command([str(self.binary)]), self.source, self.binaries, bound)
        self.assertEqual(item["program"], str(self.binary))
        self.assertEqual(item["sha256"], module.digest(self.binary))
        self.assertEqual(bound[str(self.launcher)], module.digest(self.launcher))
        self.assertEqual(len(bound), 2)

    def test_native_payload_outside_snapshot_rejected(self):
        with self.assertRaisesRegex(ValueError, "outside the selected binary"):
            module.inventory_binding(self.command([str(self.binary)]), self.source, {}, {})

    def test_python_flags_before_wrapper_and_qt_output_options(self):
        command = self.command([str(self.binary), "-o", "-,txt"])
        command["command"].insert(1, "-B")
        item = module.inventory_binding(command, self.source, self.binaries, {})
        self.assertEqual(item["payloadCommand"], [str(self.binary), "-o", "-,txt"])

    def test_python_payload_and_tool_arguments_all_bound(self):
        test = self.source / "tools/qcs-test-fixture.py"
        target = self.source / "tools/qcs-target.py"
        test.write_text("# test", encoding="utf-8")
        target.write_text("# target", encoding="utf-8")
        bound = {}
        item = module.inventory_binding(self.command([sys.executable, str(test), "--tool", str(target)]), self.source, {}, bound)
        self.assertEqual(item["script"], str(test))
        self.assertIn(str(target), bound)

    def test_python_payload_outside_checkout_rejected(self):
        test = self.root / "foreign.py"
        test.write_text("# no execution", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "outside the selected source"):
            module.inventory_binding(self.command([sys.executable, str(test)]), self.source, {}, {})

    def test_wrapper_label_and_separator_required(self):
        command = self.command([str(self.binary)])
        command["command"][3] = "qcs-tests-other"
        with self.assertRaisesRegex(ValueError, "label"):
            module.inventory_binding(command, self.source, self.binaries, {})
        command = self.command([str(self.binary)])
        command["command"].remove("--")
        with self.assertRaisesRegex(ValueError, "separator"):
            module.inventory_binding(command, self.source, self.binaries, {})

    def test_configured_data_path_is_read_from_cache(self):
        cache = "QCOPILOTS_SEARCH_TESSDATA_DIR:INTERNAL=" + str(self.source) + "\n"
        self.assertEqual(module.cache_directory(cache, "QCOPILOTS_SEARCH_TESSDATA_DIR"), self.source)
        with self.assertRaisesRegex(ValueError, "identify one"):
            module.cache_directory(cache + cache, "QCOPILOTS_SEARCH_TESSDATA_DIR")
        with self.assertRaisesRegex(ValueError, "unavailable"):
            module.cache_directory("QCOPILOTS_SEARCH_TESSDATA_DIR:PATH=" + str(self.root / "missing"), "QCOPILOTS_SEARCH_TESSDATA_DIR")

    def test_dry_run_creates_no_paths_and_requires_full_identity(self):
        output = self.root / "not created"
        args = ["--head", "a" * 40, "--source-tree", "b" * 40,
                "--build-record", str(output / "build.json"), "--report-parent", str(output),
                "--variant", "windows-x64-cpu",
                "--source", str(self.source), "--build-root", str(output / "build"),
                "--qt-root", str(output / "qt"), "--qt-prefix", str(output / "qt/version"),
                "--launch-cwd", str(output / "cwd"), "--expected-tests", "35"]
        capture = io.StringIO()
        with contextlib.redirect_stdout(capture):
            self.assertEqual(module.main(args), 0)
        self.assertFalse(output.exists())
        plan = json.loads(capture.getvalue())
        self.assertFalse(plan["executeRequested"])
        self.assertEqual(Path(plan["buildDirectory"]), output / "build/windows-x64-cpu/Release")
        debug = module.arguments([*args, "--config", "Debug"])
        self.assertEqual(debug.build_dir, output / "build/windows-x64-cpu/Debug")
        invalid_variant = list(args)
        invalid_variant[invalid_variant.index("--variant") + 1] = "../other-build"
        with self.assertRaisesRegex(ValueError, "supported Windows variant"):
            module.arguments(invalid_variant)
        args[1] = "abcdef"
        with self.assertRaisesRegex(ValueError, "full lower-case Git SHA"):
            module.arguments(args)

    def test_archiving_refuses_overwrite_and_captures_bytes(self):
        destination = self.root / "archive.exe"
        records = []
        module.archive_file(self.binary, destination, self.root, records, origin="fixture")
        self.assertEqual(records[0]["sha256"], module.digest(self.binary))
        with self.assertRaisesRegex(ValueError, "replace archived evidence"):
            module.archive_file(self.binary, destination, self.root, records, origin="fixture")

    def test_incomplete_execution_summary_cannot_pass(self):
        summary = module.summarize_execution(self.root, {"passed": False, "errors": ["stopped"]})
        self.assertFalse(summary["passed"])
        self.assertEqual(summary["passedPrograms"], 0)
        self.assertEqual(summary["errors"], ["stopped"])

    def completed_fixture(self):
        (self.root / "tests").mkdir()
        (self.root / "derived").mkdir()
        (self.root / "tests/ctest.xml").write_text('<testsuite><testcase name="qcs-tests-a"/><testcase name="qcs-tests-b"/></testsuite>', encoding="utf-8")
        module.write_json(self.root / "tests/ctest-inventory.json", {"tests": [{"name": "qcs-tests-a"}, {"name": "qcs-tests-b"}]})
        module.write_json(self.root / "derived/qt-test-cases.json", {"releaseComplete": True, "errors": []})
        module.write_json(self.root / "semantic-functional.json", {"syntheticUnitFixture": True})
        record = {"passed": True, "ctestCount": 2, "archives": [], "identity": {"syntheticUnitFixture": True}}
        for flag in ("sourceUnchanged", "binariesUnchanged", "runnerUnchanged", "buildRecordUnchanged"):
            record[flag] = True
        module.write_json(self.root / "tests/execution-record.json", record)
        for filename in ("command.stdout.log", "command.stderr.log"):
            (self.root / "tests" / filename).write_text("synthetic fixture", encoding="utf-8")
        return record

    def test_completion_summary_captures_exact_original_evidence_for_packaging(self):
        record = self.completed_fixture()
        summary = module.summarize_execution(self.root, record)
        self.assertTrue(summary["passed"])
        self.assertEqual(summary["registeredPrograms"], 2)
        self.assertEqual(summary["passedPrograms"], 2)
        files = summary["rawEvidence"] + [summary["semantic"]["rawReport"], summary["caseEvidence"]]
        for item in files:
            self.assertEqual(item["sha256"], module.digest(self.root / item["path"]))
        self.assertIn("tests/execution-record.json", {item["path"] for item in files})

    def test_completion_summary_rejects_incomplete_original_junit(self):
        record = self.completed_fixture()
        (self.root / "tests/ctest.xml").write_text('<testsuite><testcase name="qcs-tests-a"/></testsuite>', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "inventory differs"):
            module.summarize_execution(self.root, record)

    def test_completion_summary_rejects_missing_individual_cases(self):
        record = self.completed_fixture()
        module.write_json(self.root / "derived/qt-test-cases.json", {"releaseComplete": False, "errors": ["missing"]})
        with self.assertRaisesRegex(ValueError, "complete individual-case"):
            module.summarize_execution(self.root, record)




if __name__ == "__main__":
    unittest.main(verbosity=2)
