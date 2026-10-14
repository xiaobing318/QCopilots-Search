#!/usr/bin/env python3
"""Developer check orchestration contracts using isolated synthetic stage inputs.

These fixtures never configure, build, install or run QCopilots Search. A tiny
Python child verifies that command output survives a failing orchestration stage.
"""
from contextlib import contextmanager, redirect_stdout
from copy import deepcopy
import argparse
import importlib.util
import io
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
SOURCE = Path(__file__).resolve().parents[1]


def load_helper(relative, name):
    spec = importlib.util.spec_from_file_location(name, SOURCE / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


workflow = load_helper("tools/qcs-dev.py", "qcs_check_workflow_tests")
evidence = load_helper("tools/qcs-release-evidence.py", "qcs_check_evidence_tests")
HEAD = "a" * 40
IDENTITY = {"candidateHead": HEAD, "checkoutCommit": HEAD, "sourceTree": "b" * 40,
            "workingTreeModified": False,
            "lockHashes": {name: "c" * 64 for name in evidence.LOCKS}}


class CheckTests(unittest.TestCase):
    def setUp(self):
        configured = os.environ.get("QCOPILOTS_SEARCH_TEST_ROOT") or None
        if configured:
            Path(configured).mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix="qcs-check-中文 ", dir=configured)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.reports = self.root / "reports"
        self.reports.mkdir()
        self.environment = {"QCS_SYNTHETIC_FIXTURE": "1"}
        self.calls = []
        self.real_helper = workflow.helper_module

    def args(self, profile="fast", **overrides):
        values = dict(test_profile=profile, test_label=None, keyword_only=False,
                      generator=None, package_file=None, report_dir=None, model_dir=None)
        values.update(overrides)
        return argparse.Namespace(**values)

    def helper(self, relative, name):
        if relative == "tools/qcs-release-evidence.py":
            return evidence
        return self.real_helper(relative, name)

    def record_stage(self, action, args, root, build, stage, reports, sdk, prefix, environment):
        self.calls.append({"action": action, "args": deepcopy(args),
                           "environment": dict(environment), "reports": reports})
        self.assertEqual(root, self.root)
        self.assertTrue(workflow._ACTIVE_LOG.is_file())
        workflow._ACTIVE_LOG.write_text("SYNTHETIC stage: " + action + "\n", encoding="utf-8")

    @contextmanager
    def runtime(self, system="Linux", execute=None, identities=None):
        with patch.object(workflow, "helper_module", side_effect=self.helper), \
                patch.object(workflow.platform, "system", return_value=system), \
                patch.object(evidence, "source_identity", side_effect=identities,
                             return_value=deepcopy(IDENTITY)), \
                patch.object(workflow, "execute_action", side_effect=execute or self.record_stage), \
                redirect_stdout(io.StringIO()):
            yield

    def invoke(self, args):
        return workflow.run_check(args, self.root, self.root / "release", self.root / "stage",
                                  self.reports, self.root / "sdk", self.root / "sdk/qt", self.environment)

    def latest(self):
        reference = evidence.read_json(self.reports / "latest-check.json")
        path = Path(reference["report"])
        return path, evidence.read_json(path)

    def assert_bound_logs(self, document, count):
        logs = [stage["log"] for stage in document["stages"] if "log" in stage]
        self.assertEqual(len(logs), count)
        self.assertEqual(len(set(logs)), count)
        records = {entry["path"]: entry for entry in document["files"]}
        for name in logs:
            self.assertIn(name, records)
            self.assertGreater(records[name]["bytes"], 0)
            self.assertEqual(records[name]["sha256"], evidence.digest(self.root / name))
        self.assertEqual(evidence.verify_manifest(self.root, document), [])

    def test_fast_plan_is_only_doctor_configure_build_test_on_every_platform(self):
        expected = [(name, None) for name in ("doctor", "configure", "build", "test")]
        for system in ("Windows", "Linux", "Darwin"):
            with self.subTest(system=system):
                self.assertEqual(workflow.check_plan("fast", system), expected)

    def test_full_plan_includes_each_platform_package_and_installer(self):
        common = [(name, None) for name in ("doctor", "configure", "build", "models", "test", "install")]
        platform_stages = {
            "Windows": [("package", "ZIP"), ("package", "NSIS"), ("verify-package", None),
                        ("installers", None), ("sources", None)],
            "Linux": [("package", "TGZ"), ("package", "DEB"), ("verify-package", None),
                      ("installers", None), ("sources", None)],
            "Darwin": [("package", "DragNDrop"), ("verify-package", None), ("sources", None)],
        }
        for system, stages in platform_stages.items():
            with self.subTest(system=system):
                self.assertEqual(workflow.check_plan("full", system), common + stages)

    def test_fast_run_does_not_download_install_package_or_claim_release(self):
        original_environment = dict(self.environment)
        with self.runtime():
            path = self.invoke(self.args())
        document = evidence.read_json(path)
        self.assertEqual([call["action"] for call in self.calls], ["doctor", "configure", "build", "test"])
        self.assertTrue(document["passed"])
        self.assertFalse(document["releaseComplete"])
        self.assertEqual(document["testProfile"], "fast")
        self.assertEqual(self.environment, original_environment)
        self.assertTrue(all(call["args"].model_dir is None for call in self.calls))
        self.assertFalse((self.root / "models").exists())
        self.assertFalse((self.root / "stage").exists())
        self.assertFalse((self.root / "pkg").exists())
        self.assert_bound_logs(document, 4)

    def test_full_run_forwards_generators_models_and_unique_reports(self):
        for system in ("Windows", "Linux", "Darwin"):
            with self.subTest(system=system):
                self.calls.clear()
                args = self.args("full")
                with self.runtime(system):
                    path = self.invoke(args)
                document = evidence.read_json(path)
                self.assertEqual([(call["action"], call["args"].generator) for call in self.calls],
                                 workflow.check_plan("full", system))
                for call in self.calls:
                    self.assertEqual(call["args"].test_profile, "full")
                    self.assertEqual(call["args"].model_dir, self.root / "models")
                    self.assertEqual(call["environment"]["QCOPILOTS_SEARCH_MODEL_DIR"], str(self.root / "models"))
                    self.assertEqual(call["reports"], path.parent)
                    suffix = "tests" if call["action"] == "test" else "package"
                    self.assertEqual(call["args"].report_dir, path.parent / suffix)
                self.assertEqual(self.environment["QCOPILOTS_SEARCH_SEMANTIC_REPORT"], str(path.parent / "semantic-quality.json"))
                self.assertEqual(self.environment["QCOPILOTS_SEARCH_PHYSICAL_FILE_REPORT"], str(path.parent / "physical-files.json"))
                self.assertTrue(document["passed"])
                self.assertFalse(document["releaseComplete"])
                self.assertIsNone(args.model_dir, "A run must not rewrite caller arguments")
                self.assertIsNone(args.generator)
                self.assertIsNone(args.report_dir)
                self.assert_bound_logs(document, len(self.calls))

    def test_full_run_preserves_explicit_model_directory(self):
        model_dir = self.root / "用户指定 模型"
        with self.runtime():
            self.invoke(self.args("full", model_dir=model_dir))
        self.assertTrue(all(call["args"].model_dir == model_dir for call in self.calls))
        self.assertEqual(self.environment["QCOPILOTS_SEARCH_MODEL_DIR"], str(model_dir))

    def test_failed_command_preserves_stdout_stderr_and_does_not_run_downstream(self):
        def execute(action, *arguments):
            self.record_stage(action, *arguments)
            if action == "build":
                workflow.run([sys.executable, "-B", "-c",
                              "import sys; print('raw stdout fixture', flush=True); "
                              "print('raw stderr fixture', file=sys.stderr, flush=True); sys.exit(7)"], cwd=self.root)

        with self.runtime(execute=execute), self.assertRaises(subprocess.CalledProcessError) as failure:
            self.invoke(self.args("full"))
        self.assertEqual(failure.exception.returncode, 7)
        path, document = self.latest()
        self.assertFalse(document["passed"])
        self.assertFalse(document["releaseComplete"])
        self.assertEqual([call["action"] for call in self.calls], ["doctor", "configure", "build"])
        stages = document["stages"]
        self.assertEqual([item["status"] for item in stages[:3]], ["passed", "passed", "failed"])
        self.assertEqual(stages[2]["exitCode"], 7)
        self.assertTrue(all(item["status"] == "notRun" and "log" not in item for item in stages[3:]))
        raw = (self.root / stages[2]["log"]).read_text(encoding="utf-8")
        self.assertIn("raw stdout fixture", raw)
        self.assertIn("raw stderr fixture", raw)
        self.assertIn("-B -c", raw)
        self.assertIn("finishedAt", document)
        self.assertGreaterEqual(stages[2]["elapsedSeconds"], 0)
        self.assertIsNone(workflow._ACTIVE_LOG)
        self.assert_bound_logs(document, 3)
        self.assertEqual(path.relative_to(self.root).as_posix(), document["reportPath"])

    def test_full_test_failure_never_reaches_install_or_package(self):
        def execute(action, *arguments):
            self.record_stage(action, *arguments)
            if action == "test":
                raise RuntimeError("synthetic missing case-level evidence")

        with self.runtime(execute=execute), self.assertRaisesRegex(RuntimeError, "case-level evidence"):
            self.invoke(self.args("full"))
        _, document = self.latest()
        self.assertEqual(self.calls[-1]["action"], "test")
        test_index = next(index for index, item in enumerate(document["stages"]) if item["action"] == "test")
        self.assertEqual(document["stages"][test_index]["status"], "failed")
        self.assertTrue(all(item["status"] == "notRun" for item in document["stages"][test_index + 1:]))
        self.assert_bound_logs(document, test_index + 1)

    def test_check_rejects_partial_scope_options_before_creating_reports(self):
        for option, value in (("test_label", "unit"), ("keyword_only", True), ("generator", "ZIP"),
                              ("package_file", self.root / "selected.zip")):
            for profile in ("fast", "full"):
                with self.subTest(option=option, profile=profile), self.runtime(), \
                        self.assertRaisesRegex(ValueError, "entire selected profile"):
                    self.invoke(self.args(profile, **{option: value}))
                self.assertEqual(self.calls, [])
                self.assertEqual(list(self.reports.iterdir()), [])

    def test_existing_report_directory_is_not_overwritten(self):
        directory = self.reports / "previous"
        directory.mkdir()
        sentinel = directory / "check-report.json"
        sentinel.write_text("original immutable evidence", encoding="utf-8")
        with self.runtime(), self.assertRaisesRegex(RuntimeError, "empty, unique"):
            self.invoke(self.args(report_dir=directory))
        self.assertEqual(self.calls, [])
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "original immutable evidence")

    def test_report_directory_must_stay_inside_its_external_build_root(self):
        outside = tempfile.TemporaryDirectory(prefix="qcs-check-other-",
                                             dir=os.environ.get("QCOPILOTS_SEARCH_TEST_ROOT") or None)
        self.addCleanup(outside.cleanup)
        for directory in (self.root, Path(outside.name).resolve()):
            with self.subTest(directory=directory), self.runtime(), \
                    self.assertRaisesRegex(ValueError, "inside this external build root"):
                self.invoke(self.args(report_dir=directory))
        self.assertEqual(self.calls, [])
        self.assertEqual(list(Path(outside.name).iterdir()), [])
        self.assertEqual(list(self.reports.iterdir()), [])

    def test_source_changes_prevent_success_even_after_all_stages_finish(self):
        changed = dict(IDENTITY, checkoutCommit="d" * 40)
        with self.runtime(identities=[deepcopy(IDENTITY), changed]), \
                self.assertRaisesRegex(RuntimeError, "Source identity changed"):
            self.invoke(self.args())
        _, document = self.latest()
        self.assertFalse(document["passed"])
        self.assertTrue(all(item["status"] == "passed" for item in document["stages"]))
        self.assertEqual(document["identity"], IDENTITY)
        self.assert_bound_logs(document, 4)

    def test_inspection_rejects_full_success_without_original_test_evidence(self):
        with self.runtime():
            path = self.invoke(self.args("full"))
            result = workflow.inspect_check(path, HEAD)
        self.assertTrue(evidence.read_json(path)["passed"], "Synthetic stage success alone is not acceptance")
        self.assertFalse(result["passed"])
        self.assertTrue(any("test-selection.json" in error or "original evidence" in error for error in result["errors"]),
                        result["errors"])

    def test_inspection_rejects_report_moved_without_evidence_tree(self):
        with self.runtime():
            path = self.invoke(self.args("full"))
            moved = self.root / "unrelated" / "check-report.json"
            moved.parent.mkdir()
            shutil.copyfile(path, moved)
            result = workflow.inspect_check(moved, HEAD)
        self.assertFalse(result["passed"])
        self.assertTrue(any("moved without its evidence tree" in error for error in result["errors"]), result["errors"])

    def test_inspection_rejects_escaping_report_path(self):
        with self.runtime():
            path = self.invoke(self.args("full"))
            document = evidence.read_json(path)
            document["reportPath"] = "../check-report.json"
            evidence.write_json(path, document)
            result = workflow.inspect_check(path, HEAD)
        self.assertFalse(result["passed"])
        self.assertTrue(any("not relative" in error for error in result["errors"]), result["errors"])


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    unittest.main(verbosity=2)
