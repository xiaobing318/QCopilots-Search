#!/usr/bin/env python3
"""Developer check orchestration contracts with isolated synthetic stages."""
import argparse
from contextlib import contextmanager, redirect_stdout
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
spec = importlib.util.spec_from_file_location("qcs_check_workflow_tests", SOURCE / "tools/qcs-dev.py")
workflow = importlib.util.module_from_spec(spec)
spec.loader.exec_module(workflow)
HEAD = "a" * 40


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

    def args(self, **overrides):
        values = dict(config="Release", variant="linux-x64-cpu", test_label=None, keyword_only=False,
                      generator=None, package_file=None, report_dir=None, model_dir=None)
        values.update(overrides)
        return argparse.Namespace(**values)

    @contextmanager
    def workflow_fixture(self, *, dirty=False, execute=None, snapshots=None):
        state = {"commit": HEAD, "workingTreeModified": dirty, "status": [" M example.cpp"] if dirty else []}
        with patch.object(workflow.platform, "system", return_value="Linux"), \
                patch.object(workflow, "working_tree_state", return_value=state), \
                patch.object(workflow, "source_snapshot", side_effect=snapshots or [{"source": "same"}, {"source": "same"}]), \
                patch.object(workflow, "execute_action", side_effect=execute or self.record_stage), \
                redirect_stdout(io.StringIO()):
            yield

    def record_stage(self, action, args, root, build, stage, reports, sdk, prefix, environment):
        self.calls.append((action, args.generator, args.report_dir, args.model_dir, environment.copy()))

    def invoke_check(self, **overrides):
        return workflow.run_check(self.args(**overrides), self.root, self.root / "Release",
            self.root / "stage", self.reports, self.root / "sdk", self.root / "sdk/qt", self.environment)

    def last_report(self):
        latest = json.loads((self.reports / "latest-check.json").read_text(encoding="utf-8"))
        path = Path(latest["report"])
        return path, json.loads(path.read_text(encoding="utf-8"))

    def test_gpu_run_installs_selected_configuration_before_launching_stage_gui(self):
        for system, backend, relative_gui in (
                ("windows", "rocm", "bin/qcs-gui.exe"),
                ("windows", "vulkan", "bin/qcs-gui.exe"),
                ("windows", "cuda", "bin/qcs-gui.exe"),
                ("linux", "rocm", "bin/qcs-gui"),
                ("linux", "vulkan", "bin/qcs-gui"),
                ("linux", "cuda", "bin/qcs-gui"),
                ("macos", "metal", "qcs-gui.app/Contents/MacOS/qcs-gui")):
            for config in ("Debug", "Release"):
                with self.subTest(system=system, backend=backend, config=config):
                    variant = f"{system}-{'arm64' if system == 'macos' else 'x64'}-{backend}"
                    root = self.root / variant
                    build, stage = root / config, root / "stage" / config
                    gui = stage / relative_gui
                    environment = {"PATH": "original toolchain", "USER_SETTING": "unchanged"}
                    arguments = self.args(variant=variant, config=config, model_dir=self.root / "模型 cache",
                                          program_arg=["--custom=value with spaces"])
                    calls = []

                    def execute(command, **options):
                        calls.append((command, options))
                        if command[0] == "cmake":
                            gui.parent.mkdir(parents=True, exist_ok=True)
                            gui.write_bytes(b"synthetic installed GUI")
                        else:
                            self.assertTrue(gui.is_file(), "The install must finish before GUI discovery")

                    with patch.object(workflow, "build_variant", return_value={"backend": backend}), \
                            patch.object(workflow, "development_tessdata", return_value=self.root / "OCR"), \
                            patch.object(workflow, "os", argparse.Namespace(name="nt" if system == "windows" else "posix")), \
                            patch.object(workflow, "run", side_effect=execute):
                        workflow.execute_action("run", arguments, root, build, stage, self.reports,
                                                self.root / "SDK", self.root / "SDK/qt", environment)
                    self.assertEqual(len(calls), 2)
                    self.assertEqual(calls[0][0], ["cmake", "--install", build, "--config", config, "--prefix", stage])
                    self.assertIs(calls[0][1]["env"], environment)
                    self.assertEqual(calls[1][0], [gui, "--profile-dir", root / "development-profile",
                                                  "--model-dir", arguments.model_dir, *arguments.program_arg])
                    self.assertEqual(calls[1][1]["cwd"], root)
                    self.assertIsNot(calls[1][1]["env"], environment)
                    self.assertEqual(environment, {"PATH": "original toolchain", "USER_SETTING": "unchanged"})

    def test_failed_gpu_install_never_launches_gui(self):
        arguments = self.args(variant="windows-x64-rocm", program_arg=[])
        with patch.object(workflow, "build_variant", return_value={"backend": "rocm"}), \
                patch.object(workflow, "development_tessdata", return_value=self.root / "OCR"), \
                patch.object(workflow, "run", side_effect=subprocess.CalledProcessError(1, "cmake")) as execute:
            with self.assertRaises(subprocess.CalledProcessError):
                workflow.execute_action("run", arguments, self.root, self.root / "Release",
                                        self.root / "stage/Release", self.reports,
                                        self.root / "SDK", self.root / "SDK/qt", self.environment)
        execute.assert_called_once()
        self.assertEqual(execute.call_args.args[0][0], "cmake")

    def test_cpu_run_keeps_build_gui_without_installing(self):
        arguments = self.args(variant="windows-x64-cpu", program_arg=["--custom=one two"])
        build = self.root / "Debug"
        gui = workflow.executable(build, workflow.GUI_EXECUTABLE)
        gui.parent.mkdir(parents=True)
        gui.write_bytes(b"synthetic built GUI")
        with patch.object(workflow, "build_variant", return_value={"backend": "cpu"}), \
                patch.object(workflow, "development_tessdata", return_value=self.root / "OCR"), \
                patch.object(workflow, "run") as execute:
            workflow.execute_action("run", arguments, self.root, build, self.root / "stage/Debug",
                                    self.reports, self.root / "SDK", self.root / "SDK/qt", self.environment)
        execute.assert_called_once()
        self.assertEqual(execute.call_args.args[0][0].parent, build / "bin")
        self.assertEqual(execute.call_args.args[0][1:], ["--profile-dir", self.root / "development-profile",
                                                       "--model-dir", self.root.parent / "models", *arguments.program_arg])

    def test_source_collection_uses_selected_configuration_dependency_cache(self):
        for config in ("Release", "Debug"):
            with self.subTest(config=config):
                archive = self.root / "locked-llama.tar.gz"
                source_tool = argparse.Namespace(llama_entry=lambda: {"name": "llama.cpp"})
                calls = []
                def get_archive(*arguments, **options):
                    self.assertEqual(options, {"dependency_root": self.root / "deps" / config})
                    return archive
                source_tool.get_archive = get_archive
                def run(command, **options):
                    calls.append(command)
                    if "--report" in command:
                        Path(command[command.index("--report") + 1]).write_text('{"passed":true}', encoding="utf-8")
                with patch.object(workflow, "helper_module", return_value=source_tool), patch.object(workflow, "run", side_effect=run):
                    workflow.execute_action("sources", self.args(config=config), self.root, self.root / config,
                        self.root / "stage", self.reports, self.root / "SDK", self.root / "SDK/qt", self.environment)
                self.assertEqual(calls[0][calls[0].index("--dependency-root") + 1], self.root / "deps" / config)
                self.assertEqual(calls[1][calls[1].index("--archive") + 1], archive)

    def test_full_plan_includes_platform_packages_and_installers(self):
        for system, generators, installers in (("Windows", ["NSIS", "ZIP"], True),
                ("Linux", ["TGZ", "DEB"], True), ("Darwin", ["DragNDrop"], False)):
            plan = workflow.check_plan(system)
            self.assertEqual([action for action, _ in plan[:6]], ["doctor", "configure", "build", "models", "test", "install"])
            self.assertEqual([generator for action, generator in plan if action == "package"], generators)
            self.assertEqual(plan[-1][0], "installers" if installers else "verify-package")

    def test_dirty_checkout_can_complete_every_stage(self):
        with self.workflow_fixture(dirty=True):
            path = self.invoke_check()
        report = json.loads(path.read_text(encoding="utf-8"))
        self.assertTrue(report["passed"])
        self.assertTrue(report["fullVerificationPassed"])
        self.assertTrue(report["source"]["workingTreeModified"])
        self.assertEqual([(action, generator) for action, generator, *_ in self.calls], workflow.check_plan("Linux"))
        self.assertTrue(all(stage["status"] == "passed" and stage["exitCode"] == 0 for stage in report["stages"]))
        self.assertTrue(all((path.parent / stage["log"]).is_file() for stage in report["stages"]))
        self.assertNotIn("releaseComplete", report)

    def test_models_and_report_directories_propagate_to_stages(self):
        models = self.root / "shared-models"
        with self.workflow_fixture():
            path = self.invoke_check(model_dir=models)
        for action, generator, report, model_dir, environment in self.calls:
            self.assertEqual(model_dir, models)
            self.assertEqual(environment["QCOPILOTS_SEARCH_MODEL_DIR"], str(models))
            self.assertEqual(report, path.parent / ("tests" if action == "test" else "package"))
        self.assertNotIn("QCOPILOTS_SEARCH_MODEL_DIR", self.environment)

    def test_partial_options_rejected_before_creating_report(self):
        for option, value in (("test_label", "unit"), ("keyword_only", True),
                              ("generator", "TGZ"), ("package_file", Path("example.tar.gz"))):
            with self.subTest(option=option), self.workflow_fixture():
                with self.assertRaisesRegex(ValueError, "entire test suite"):
                    self.invoke_check(**{option: value})
        self.assertEqual(list(self.reports.iterdir()), [])
        self.assertEqual(self.calls, [])

    def test_failure_preserves_exit_code_output_and_stops_following_stages(self):
        def execute(action, *arguments):
            self.calls.append(action)
            if action == "build":
                workflow.run([sys.executable, "-B", "-c",
                    "import sys; print('build stdout', flush=True); print('build stderr', file=sys.stderr); sys.exit(7)"], cwd=self.root)
        with self.workflow_fixture(execute=execute):
            with self.assertRaises(subprocess.CalledProcessError):
                self.invoke_check()
        path, report = self.last_report()
        self.assertEqual(self.calls, ["doctor", "configure", "build"])
        self.assertFalse(report["fullVerificationPassed"])
        self.assertEqual(report["stages"][2]["exitCode"], 7)
        self.assertTrue(all(stage["status"] == "notRun" for stage in report["stages"][3:]))
        log = (path.parent / report["stages"][2]["log"]).read_text(encoding="utf-8")
        self.assertIn("build stdout", log)
        self.assertIn("build stderr", log)
        self.assertIsNone(workflow._ACTIVE_LOG)

    def test_source_change_during_check_prevents_success(self):
        with self.workflow_fixture(snapshots=[{"source": "before"}, {"source": "after"}]):
            with self.assertRaisesRegex(RuntimeError, "Source files changed"):
                self.invoke_check()
        _, report = self.last_report()
        self.assertFalse(report["sourceUnchanged"])
        self.assertFalse(report["fullVerificationPassed"])

    def test_nonempty_report_directory_is_preserved(self):
        target = self.reports / "existing"
        target.mkdir()
        original = target / "keep.txt"
        original.write_text("keep", encoding="utf-8")
        with self.workflow_fixture(), self.assertRaisesRegex(RuntimeError, "empty, unique"):
            self.invoke_check(report_dir=target)
        self.assertEqual(original.read_text(encoding="utf-8"), "keep")

    def test_report_directory_cannot_escape_owned_build_root(self):
        with self.workflow_fixture(), self.assertRaisesRegex(ValueError, "inside this external"):
            self.invoke_check(report_dir=self.root.parent / "qcs-unowned-report")

    def test_linux_installer_receives_actual_commit_without_clean_requirement(self):
        with patch.object(workflow.platform, "system", return_value="Linux"), \
                patch.object(workflow.os, "name", "posix"), \
                patch.object(workflow, "working_tree_state", return_value={"commit": HEAD, "workingTreeModified": True}), \
                patch.object(workflow, "package_for_suffix", return_value=self.root / "fixture.deb"), \
                patch.object(workflow, "run") as execute:
            workflow.execute_action("installers", self.args(), self.root, self.root / "Release",
                self.root / "stage", self.reports, self.root / "sdk", self.root / "sdk/qt", self.environment)
        self.assertEqual(execute.call_args.kwargs["env"]["QCOPILOTS_SEARCH_CANDIDATE_HEAD"], HEAD)
        self.assertNotIn("QCOPILOTS_SEARCH_CANDIDATE_HEAD", self.environment)


if __name__ == "__main__":
    unittest.main(verbosity=2)
