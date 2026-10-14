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

    def args(self, **overrides):
        values = dict(config="Release", variant="linux-x64-cpu", test_label=None, keyword_only=False,
                      generator=None, package_file=None, report_dir=None, model_dir=None)
        values.update(overrides)
        return argparse.Namespace(**values)

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
                    arguments = self.args(variant=variant, config=config,
                                          program_arg=["--model-dir", "模型 cache", "--custom=value with spaces"])
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
                                                  *arguments.program_arg])
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
        with patch.object(workflow, "build_variant", return_value={"backend": "cpu"}), \
                patch.object(workflow, "development_tessdata", return_value=self.root / "OCR"), \
                patch.object(workflow, "run") as execute:
            workflow.execute_action("run", arguments, self.root, build, self.root / "stage/Debug",
                                    self.reports, self.root / "SDK", self.root / "SDK/qt", self.environment)
        execute.assert_called_once()
        self.assertEqual(execute.call_args.args[0][0].parent, build / "bin")
        self.assertEqual(execute.call_args.args[0][1:], ["--profile-dir", self.root / "development-profile",
                                                       *arguments.program_arg])

    def test_local_linux_installer_binds_real_commit_without_ci_environment(self):
        environment = dict(self.environment)
        with patch.object(workflow.platform, "system", return_value="Linux"), \
                patch.object(workflow.os, "name", "posix"), \
                patch.object(workflow, "helper_module", side_effect=self.helper), \
                patch.object(evidence, "source_identity", return_value=deepcopy(IDENTITY)), \
                patch.object(workflow, "package_for_suffix", return_value=self.root / "pkg/qcs-fixture.deb"), \
                patch.object(workflow, "run") as execute:
            workflow.execute_action("installers", self.args(), self.root, self.root / "Release",
                self.root / "stage", self.reports, self.root / "sdk", self.root / "sdk/qt", environment)
        self.assertEqual(execute.call_args.kwargs["env"]["QCOPILOTS_SEARCH_CANDIDATE_HEAD"], HEAD)
        self.assertNotIn("QCOPILOTS_SEARCH_CANDIDATE_HEAD", environment)

    def test_linux_installer_rejects_candidate_mismatch_before_launch(self):
        environment = dict(self.environment, QCOPILOTS_SEARCH_CANDIDATE_HEAD="d" * 40)
        with patch.object(workflow.platform, "system", return_value="Linux"), \
                patch.object(workflow.os, "name", "posix"), \
                patch.object(workflow, "helper_module", side_effect=self.helper), \
                patch.object(evidence, "source_identity", return_value=deepcopy(IDENTITY)), \
                patch.object(workflow, "run") as execute:
            with self.assertRaisesRegex(RuntimeError, "candidate differs"):
                workflow.execute_action("installers", self.args(), self.root, self.root / "Release",
                    self.root / "stage", self.reports, self.root / "sdk", self.root / "sdk/qt", environment)
        execute.assert_not_called()

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
        if action == "build":
            self.cpack_fixture({"Windows": "NSIS;ZIP", "Linux": "TGZ;DEB", "Darwin": "DragNDrop"}[workflow.platform.system()])
        elif action == "package":
            suffix = {"ZIP": ".zip", "NSIS": ".exe", "TGZ": ".tar.gz", "DEB": ".deb", "DragNDrop": ".dmg"}[args.generator]
            package = root / "pkg" / args.config / ("qcs-example" + suffix)
            package.parent.mkdir(parents=True, exist_ok=True)
            package.write_bytes(("SYNTHETIC package " + args.generator).encode())

    def cpack_fixture(self, generators):
        build = self.root / "Release"
        build.mkdir(parents=True, exist_ok=True)
        (build / "CPackConfig.cmake").write_text(
            'set(CPACK_PACKAGE_FILE_NAME "qcs-example")\nset(CPACK_GENERATOR "' + generators + '")\n', encoding="utf-8")

    @contextmanager
    def runtime(self, system="Linux", execute=None, identities=None):
        with patch.object(workflow, "helper_module", side_effect=self.helper), \
                patch.object(workflow.platform, "system", return_value=system), \
                patch.object(evidence, "source_identity", side_effect=identities,
                             return_value=deepcopy(IDENTITY)), \
                patch.object(workflow, "execute_action", side_effect=execute or self.record_stage), \
                patch.object(workflow, "source_snapshot", return_value={"main.cpp": {"bytes": 1, "sha256": "f" * 64}}), \
                patch.object(workflow, "executable_snapshot", return_value={name: {"bytes": 1, "sha256": "e" * 64} for name in ("qcs-gui", "qcs-service", "qcs-worker", "qcs-cli")}), \
                redirect_stdout(io.StringIO()):
            yield

    def invoke(self, args):
        return workflow.run_check(args, self.root, self.root / "Release", self.root / "stage",
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

    def test_complete_plan_includes_each_platform_package_and_installer(self):
        common = [(name, None) for name in ("doctor", "configure", "build", "models", "test", "install")]
        platform_stages = {
            "Windows": [("package", "NSIS"), ("package", "ZIP"), ("verify-package", None),
                        ("installers", None)],
            "Linux": [("package", "TGZ"), ("package", "DEB"), ("verify-package", None),
                      ("installers", None)],
            "Darwin": [("package", "DragNDrop"), ("verify-package", None)],
        }
        for system, stages in platform_stages.items():
            with self.subTest(system=system):
                self.assertEqual(workflow.check_plan(system), common + stages)

    def test_complete_run_forwards_generators_models_and_unique_reports(self):
        for system in ("Windows", "Linux", "Darwin"):
            with self.subTest(system=system):
                self.calls.clear()
                args = self.args()
                with self.runtime(system):
                    path = self.invoke(args)
                document = evidence.read_json(path)
                self.assertEqual([(call["action"], call["args"].generator) for call in self.calls],
                                 workflow.check_plan(system))
                for call in self.calls:
                    self.assertEqual(call["args"].model_dir, self.root.parent / "models")
                    self.assertEqual(call["environment"]["QCOPILOTS_SEARCH_MODEL_DIR"], str(self.root.parent / "models"))
                    self.assertEqual(call["reports"], path.parent)
                    suffix = "tests" if call["action"] == "test" else "package"
                    self.assertEqual(call["args"].report_dir, path.parent / suffix)
                self.assertTrue(all(call["environment"]["QCOPILOTS_SEARCH_SEMANTIC_REPORT"] == str(path.parent / "semantic-functional.json") for call in self.calls))
                self.assertNotIn("QCOPILOTS_SEARCH_SEMANTIC_REPORT", self.environment)
                self.assertTrue(document["passed"])
                self.assertFalse(document["releaseComplete"])
                self.assertIsNone(args.model_dir, "A run must not rewrite caller arguments")
                self.assertIsNone(args.generator)
                self.assertIsNone(args.report_dir)
                self.assert_bound_logs(document, len(self.calls))

    def test_complete_run_preserves_explicit_model_directory(self):
        model_dir = self.root / "用户指定 模型"
        with self.runtime():
            self.invoke(self.args(model_dir=model_dir))
        self.assertTrue(all(call["args"].model_dir == model_dir for call in self.calls))
        self.assertTrue(all(call["environment"]["QCOPILOTS_SEARCH_MODEL_DIR"] == str(model_dir) for call in self.calls))

    def test_failed_command_preserves_stdout_stderr_and_does_not_run_downstream(self):
        def execute(action, *arguments):
            self.record_stage(action, *arguments)
            if action == "build":
                workflow.run([sys.executable, "-B", "-c",
                              "import sys; print('raw stdout fixture', flush=True); "
                              "print('raw stderr fixture', file=sys.stderr, flush=True); sys.exit(7)"], cwd=self.root)

        with self.runtime(execute=execute), self.assertRaises(subprocess.CalledProcessError) as failure:
            self.invoke(self.args())
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

    def test_complete_test_failure_never_reaches_install_or_package(self):
        def execute(action, *arguments):
            self.record_stage(action, *arguments)
            if action == "test":
                raise RuntimeError("synthetic missing case-level evidence")

        with self.runtime(execute=execute), self.assertRaisesRegex(RuntimeError, "case-level evidence"):
            self.invoke(self.args())
        _, document = self.latest()
        self.assertEqual(self.calls[-1]["action"], "test")
        test_index = next(index for index, item in enumerate(document["stages"]) if item["action"] == "test")
        self.assertEqual(document["stages"][test_index]["status"], "failed")
        self.assertTrue(all(item["status"] == "notRun" for item in document["stages"][test_index + 1:]))
        self.assert_bound_logs(document, test_index + 1)

    def test_check_rejects_partial_scope_options_before_creating_reports(self):
        for option, value in (("test_label", "unit"), ("keyword_only", True), ("generator", "ZIP"),
                              ("package_file", self.root / "selected.zip")):
            with self.subTest(option=option), self.runtime(), \
                    self.assertRaisesRegex(ValueError, "entire test suite"):
                self.invoke(self.args(**{option: value}))
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
        self.assert_bound_logs(document, len(workflow.check_plan("Linux")))

    def test_changed_source_bytes_fail_even_if_git_identity_is_unchanged(self):
        with self.runtime(), patch.object(workflow, "source_snapshot", side_effect=[
                {"main.cpp": {"bytes": 1, "sha256": "1" * 64}},
                {"main.cpp": {"bytes": 1, "sha256": "2" * 64}}]), \
                self.assertRaisesRegex(RuntimeError, "bytes changed"):
            self.invoke(self.args())
        _, document = self.latest()
        self.assertFalse(document["passed"])
        self.assertFalse(document["stability"]["sourceUnchanged"])

    def test_runtime_library_changes_after_build_fail_the_check(self):
        runtime = self.root / "Release/bin/qcs-runtime.dll"
        def execute(action, *arguments):
            self.record_stage(action, *arguments)
            if action == "build":
                runtime.parent.mkdir(parents=True)
                runtime.write_bytes(b"original runtime")
            elif action == "install":
                runtime.write_bytes(b"changed runtime")
        with self.runtime(execute=execute), self.assertRaisesRegex(RuntimeError, "bytes changed"):
            self.invoke(self.args())
        _, document = self.latest()
        self.assertFalse(document["stability"]["runtimeUnchanged"])

    def test_later_packaging_cannot_replace_an_earlier_package_baseline(self):
        package = self.root / "pkg/Release/qcs-example.exe"
        def execute(action, args, *arguments):
            self.record_stage(action, args, *arguments)
            if action == "package":
                package.parent.mkdir(parents=True, exist_ok=True)
                package.write_bytes(b"first package" if args.generator == "NSIS" else b"changed package")
        with self.runtime(system="Windows", execute=execute), \
                self.assertRaisesRegex(RuntimeError, "earlier package changed"):
            self.invoke(self.args(variant="windows-x64-cpu"))
        _, document = self.latest()
        self.assertFalse(document["passed"])
        self.assertEqual(document["stages"][7]["status"], "failed")
        self.assertNotEqual(document["packages"]["pkg/Release/qcs-example.exe"]["sha256"], evidence.digest(package))

    def test_package_changes_during_installer_verification_fail_the_check(self):
        package = self.root / "pkg/Release/qcs-example.tar.gz"
        def execute(action, *arguments):
            self.record_stage(action, *arguments)
            if action == "package" and not package.exists():
                package.parent.mkdir(parents=True, exist_ok=True)
                package.write_bytes(b"original package")
            elif action == "installers":
                package.write_bytes(b"changed package")
        with self.runtime(execute=execute), self.assertRaisesRegex(RuntimeError, "bytes changed"):
            self.invoke(self.args())
        _, document = self.latest()
        self.assertFalse(document["stability"]["packagesUnchanged"])

    def test_configure_binds_variant_candidate_attempt_and_config_outputs(self):
        args = self.args(cmake_arg=[], candidate_commit=HEAD, attempt=3)
        with patch.object(workflow, "gpu_sdk", return_value={"CMAKE_PREFIX_PATH": "/locked/rocm/cmake"}), \
                patch.object(workflow, "check_path_budget"), patch.object(workflow, "run") as execute:
            workflow.execute_action("configure", args, self.root, self.root / "Release",
                self.root / "stage/Release", self.reports, self.root / "sdk", self.root / "sdk/qt", self.environment)
        command = list(map(str, execute.call_args.args[0]))
        self.assertIn("-DQCOPILOTS_SEARCH_VARIANT=linux-x64-cpu", command)
        self.assertIn("-DQCOPILOTS_SEARCH_CANDIDATE_COMMIT=" + HEAD, command)
        self.assertIn("-DQCOPILOTS_SEARCH_CANDIDATE_ATTEMPT=3", command)
        self.assertIn("-DCPACK_PACKAGE_DIRECTORY=" + str(self.root / "pkg/Release"), command)
        self.assertIn("-DQCOPILOTS_SEARCH_DEPENDENCY_ROOT=" + str(self.root / "deps/Release"), command)
        prefixes = [item for item in command if item.startswith("-DCMAKE_PREFIX_PATH=")]
        self.assertEqual(prefixes[-1], "-DCMAKE_PREFIX_PATH=" + str(self.root / "sdk/qt") + ";/locked/rocm/cmake")

    def test_installer_selection_keeps_rocm_setup_separate_from_portable_zip(self):
        self.cpack_fixture("NSIS;ZIP")
        packages = self.root / "pkg/Release"
        packages.mkdir(parents=True)
        portable, installer = packages / "qcs-example.zip", packages / "qcs-example-setup.zip"
        portable.write_bytes(b"portable")
        installer.write_bytes(b"installer")
        self.assertEqual(workflow.package_for_suffix(self.root, ".zip"), portable)
        self.assertEqual(workflow.package_for_suffix(self.root, ".exe"), installer)

    def test_old_candidate_packages_remain_on_disk_but_are_not_selected_or_bound(self):
        packages = self.root / "pkg/Release"
        packages.mkdir(parents=True)
        old = [packages / "qcs-0.1.0-linux-x64-cpu-0123456789ab-attempt1.tar.gz",
               packages / "qcs-0.1.0-linux-x64-cpu-0123456789ab-attempt1.deb"]
        for path in old:
            path.write_bytes(b"earlier candidate")
        with self.runtime():
            report = self.invoke(self.args())
        document = evidence.read_json(report)
        expected = {"pkg/Release/qcs-example.tar.gz", "pkg/Release/qcs-example.deb"}
        self.assertEqual(set(document["packages"]), expected)
        self.assertEqual({path.relative_to(self.root).as_posix() for path in workflow.candidate_packages(self.root, "Release")}, expected)
        self.assertEqual(workflow.package_for_suffix(self.root, ".tar.gz"), packages / "qcs-example.tar.gz")
        bound = {item["path"] for item in document["files"]}
        self.assertTrue(expected.issubset(bound))
        for path in old:
            self.assertEqual(path.read_bytes(), b"earlier candidate")
            self.assertNotIn(path.relative_to(self.root).as_posix(), bound)

    def test_inspection_rejects_stage_success_without_original_test_evidence(self):
        with self.runtime():
            path = self.invoke(self.args())
            result = workflow.inspect_check(path, HEAD)
        self.assertTrue(evidence.read_json(path)["passed"], "Synthetic stage success alone is not acceptance")
        self.assertFalse(result["passed"])
        self.assertTrue(any("test-selection.json" in error or "original evidence" in error for error in result["errors"]),
                        result["errors"])

    def test_inspection_rejects_report_moved_without_evidence_tree(self):
        with self.runtime():
            path = self.invoke(self.args())
            moved = self.root / "unrelated" / "check-report.json"
            moved.parent.mkdir()
            shutil.copyfile(path, moved)
            result = workflow.inspect_check(moved, HEAD)
        self.assertFalse(result["passed"])
        self.assertTrue(any("moved without its evidence tree" in error for error in result["errors"]), result["errors"])

    def test_inspection_rejects_escaping_report_path(self):
        with self.runtime():
            path = self.invoke(self.args())
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
