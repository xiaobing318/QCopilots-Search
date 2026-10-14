#!/usr/bin/env python3
"""Fast isolated contract tests. They never execute the application or CTest."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
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

    def test_full_selection_cannot_use_fast_filtered_missing_or_unexecuted_evidence(self):
        names = ["qcs-tests-unit", "qcs-tests-real-model"]
        selection = {"schemaVersion": 1, "kind": "ctest-selection", "profile": "full",
                     "labelFilter": None, "filters": [], "registeredTests": names, "selectedTests": names,
                     "excludedTests": [], "counts": {"registered": 2, "selected": 2, "excluded": 0},
                     "passed": True, "fullSuitePassed": True, "status": "passed", "exitCode": 0,
                     "releaseComplete": False}
        module.validate_full_selection(selection, names)
        for changes in ({"profile": "fast"}, {"labelFilter": "unit"}, {"filters": ["-LE", "^slow$"]},
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
                "--extraction-prefix", "qcopilots-search-extraction-v6:",
                "--source", str(self.source), "--build-root", str(output / "build"),
                "--qt-root", str(output / "qt"), "--qt-prefix", str(output / "qt/version"),
                "--launch-cwd", str(output / "cwd"), "--expected-tests", "35"]
        capture = io.StringIO()
        with contextlib.redirect_stdout(capture):
            self.assertEqual(module.main(args), 0)
        self.assertFalse(output.exists())
        self.assertFalse(json.loads(capture.getvalue())["executeRequested"])
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
        module.write_json(self.root / "semantic-quality.json", {"syntheticUnitFixture": True})
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


class PreservedSemanticContracts(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="qcs-sc-", dir=os.environ.get("QCOPILOTS_SEARCH_TEST_ROOT") or None)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.runs = self.root / "runs"
        self.preserved = self.runs / "qcs-t-wrapper" / "tmp" / "qcs-t-semantic"
        self.corpus = self.preserved / "corpus"
        self.corpus.mkdir(parents=True)
        self.original = self.corpus / "中文 example.pdf"
        self.original.write_bytes(b"synthetic unit bytes, never a real semantic fixture")
        self.report = {"syntheticTestOnly": True, "preservedRoot": str(self.preserved),
                       "files": [{"file": self.original.name, "sha256": module.digest(self.original)}]}

    def verify(self):
        return module.verify_preserved_corpus(self.report, self.runs)

    def test_original_corpus_verified_without_changing_report_or_bytes(self):
        before_report = json.dumps(self.report, ensure_ascii=False, sort_keys=True)
        before_bytes = self.original.read_bytes()
        checked = self.verify()
        self.assertTrue(checked["verified"])
        self.assertEqual(checked["fileCount"], 1)
        self.assertEqual(checked["files"][0]["sha256"], self.report["files"][0]["sha256"])
        self.assertEqual(checked["files"][0]["bytes"], len(before_bytes))
        self.assertEqual(before_report, json.dumps(self.report, ensure_ascii=False, sort_keys=True))
        self.assertEqual(before_bytes, self.original.read_bytes())

    def test_removed_wrapper_corpus_cannot_claim_preservation(self):
        self.original.unlink()
        self.corpus.rmdir()
        with self.assertRaisesRegex(ValueError, "missing or unreadable"):
            self.verify()

    def test_changed_original_bytes_are_rejected(self):
        self.original.write_bytes(b"regenerated content")
        with self.assertRaisesRegex(ValueError, "bytes changed"):
            self.verify()

    def test_missing_original_file_is_rejected(self):
        self.original.unlink()
        with self.assertRaisesRegex(ValueError, "missing or unreadable"):
            self.verify()

    def test_missing_relative_or_external_preserved_root_is_rejected(self):
        for value in (None, "", "relative", str(self.root), str(self.runs)):
            with self.subTest(value=value):
                self.report["preservedRoot"] = value
                with self.assertRaisesRegex(ValueError, "absolute directory|outside this candidate"):
                    self.verify()

    def test_escaped_or_noncanonical_manifest_paths_are_rejected(self):
        for value in ("", "../foreign", "/foreign", "a/../foreign", "a//foreign", "./foreign", "a\\foreign", "C:foreign"):
            with self.subTest(value=value):
                self.report["files"][0]["file"] = value
                with self.assertRaisesRegex(ValueError, "canonical and relative"):
                    self.verify()

    def test_empty_duplicate_or_invalid_manifest_is_rejected(self):
        entry = self.report["files"][0]
        for files in ([], None, [None], [entry, dict(entry)]):
            with self.subTest(files=files):
                self.report["files"] = files
                with self.assertRaisesRegex(ValueError, "manifest is empty|must be an object|duplicate file"):
                    self.verify()

    def test_existing_nested_files_remain_within_corpus(self):
        nested = self.corpus / "目录 with space" / "text.txt"
        nested.parent.mkdir()
        nested.write_text("original nested bytes", encoding="utf-8")
        self.report["files"].append({"file": nested.relative_to(self.corpus).as_posix(),
                                     "sha256": module.digest(nested)})
        self.assertEqual(self.verify()["fileCount"], 2)

    if os.name == "nt":
        def test_windows_case_alias_does_not_count_as_another_file(self):
            entry = dict(self.report["files"][0])
            entry["file"] = entry["file"].upper()
            self.report["files"].append(entry)
            with self.assertRaisesRegex(ValueError, "duplicate file"):
                self.verify()

    def test_complete_hash_and_optional_byte_count_are_enforced(self):
        for value in (None, "abc", "A" * 64):
            with self.subTest(hash=value):
                self.report["files"][0]["sha256"] = value
                with self.assertRaisesRegex(ValueError, "complete SHA-256"):
                    self.verify()
        self.report["files"][0]["sha256"] = module.digest(self.original)
        self.report["files"][0]["bytes"] = self.original.stat().st_size
        self.assertTrue(self.verify()["verified"])
        self.report["files"][0]["bytes"] += 1
        with self.assertRaisesRegex(ValueError, "byte count differs"):
            self.verify()

    def test_directory_cannot_replace_a_manifest_file(self):
        self.original.unlink()
        self.original.mkdir()
        with self.assertRaisesRegex(ValueError, "wrong file type"):
            self.verify()

    def test_real_directory_redirect_cannot_supply_preserved_corpus(self):
        target = self.root / "foreign corpus"
        self.corpus.rename(target)
        if os.name == "nt":
            result = subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(self.corpus), str(target)],
                                    cwd=self.root, capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.corpus.symlink_to(target, target_is_directory=True)
        try:
            with self.assertRaisesRegex(ValueError, "redirects through a link"):
                self.verify()
        finally:
            if os.name == "nt":
                os.rmdir(self.corpus)
            else:
                self.corpus.unlink()


if __name__ == "__main__":
    unittest.main(verbosity=2)
