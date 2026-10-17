#!/usr/bin/env python3
"""Developer check orchestration contracts with isolated synthetic stages."""
import argparse
import copy
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
from unittest.mock import Mock, patch

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

    def model_response(self, identifier, *, verify=False):
        model = workflow.helper_module("tools/qcs-model-catalog.py", "qcs_test_model_contract").selected(identifier)
        result = {"modelId": identifier, "dimensions": model["dimensions"],
                  "capabilities": {"text": True, "image": model["supportsVision"],
                                   "videoFrames": model["supportsVision"], "audio": model["supportsAudio"]},
                  "artifacts": copy.deepcopy(model["artifacts"])}
        if verify:
            directory = self.root / "shared-models"
            if identifier != "embeddinggemma-2":
                directory /= identifier
            result.update(directory=str(directory), valid=True, filesVerified=True,
                          artifacts=[{"name": row["name"], "present": True, "bytes": row["size"],
                                      "expectedBytes": row["size"], "sha256": row["sha256"]}
                                     for row in model["artifacts"]])
        return {"ok": True, "result": result}

    def execute_models(self, identifiers=None):
        workflow.execute_action("models", self.args(model=identifiers, model_dir=self.root / "shared-models"),
                                self.root, self.root / "build", self.root / "stage", self.reports,
                                self.root / "SDK", self.root / "SDK/qt", self.environment)

    def test_models_validate_current_lock_before_downloading_default_and_multiple_presets(self):
        for identifiers in (None, ["embeddinggemma-2", "qwen3-vl-embedding-2b", "qwen3-vl-embedding-8b"]):
            with self.subTest(identifiers=identifiers):
                selected = identifiers or ["embeddinggemma-2"]
                events = []
                def response(command, **options):
                    action = "verify" if "verify" in command else "manifest"
                    identifier = command[command.index("--model") + 1]
                    events.append((action, identifier))
                    return subprocess.CompletedProcess(command, 0,
                        json.dumps(self.model_response(identifier, verify=action == "verify")).encode("utf-8"), b"")
                def download(command, **options):
                    self.assertIn("download", command)
                    events.append(("download", command[command.index("--model") + 1]))
                with patch.object(workflow, "executable", return_value=self.root / "qcs-cli"), \
                        patch.object(workflow.subprocess, "run", side_effect=response), \
                        patch.object(workflow, "run", side_effect=download):
                    self.execute_models(identifiers)
                self.assertEqual(events, [("manifest", identifier) for identifier in selected] +
                                 [(action, identifier) for identifier in selected for action in ("download", "verify")])
                for identifier in selected:
                    name = "models" if identifier == "embeddinggemma-2" else "models-" + identifier
                    self.assertTrue((self.reports / (name + "-manifest.json")).is_file())
                    self.assertTrue((self.reports / (name + ".json")).is_file())

    def test_stale_cli_manifest_blocks_every_selected_download_and_requests_rebuild(self):
        identifier = "qwen3-vl-embedding-8b"
        original = self.model_response(identifier)
        mutations = [
            lambda row: row["artifacts"][0].update(name="stale.gguf"),
            lambda row: row["artifacts"][0].update(url="https://example.invalid/old.gguf"),
            lambda row: row["artifacts"][0].update(size=1),
            lambda row: row["artifacts"][0].update(sha256="0" * 64),
            lambda row: row.update(modelId="embeddinggemma-2"),
            lambda row: row.update(dimensions=768),
            lambda row: row["capabilities"].update(audio=True),
            lambda row: row["capabilities"].update(text=1),
            lambda row: row["artifacts"][0].update(size=float(row["artifacts"][0]["size"])),
            lambda row: row["artifacts"].__setitem__(1, row["artifacts"][0]),
            lambda row: row["artifacts"].pop(),
        ]
        for mutation in mutations:
            changed = copy.deepcopy(original)
            mutation(changed["result"])
            with self.subTest(manifest=changed), \
                    patch.object(workflow, "executable", return_value=self.root / "qcs-cli"), \
                    patch.object(workflow.subprocess, "run", side_effect=[
                        subprocess.CompletedProcess([], 0, json.dumps(self.model_response("embeddinggemma-2")).encode(), b""),
                        subprocess.CompletedProcess([], 0, json.dumps(changed).encode(), b"")]), \
                    patch.object(workflow, "run") as download:
                with self.assertRaisesRegex(RuntimeError, "Run qcs-dev build"):
                    self.execute_models(["embeddinggemma-2", identifier])
                download.assert_not_called()

    def test_failed_or_malformed_cli_manifest_never_starts_download(self):
        for code, output in ((2, b""), (0, b"not JSON"), (0, b"[]"),
                             (0, b'{"ok":false,"result":{}}'), (0, b'{"ok":true,"result":[]}')):
            with self.subTest(code=code, output=output), \
                    patch.object(workflow, "executable", return_value=self.root / "qcs-cli"), \
                    patch.object(workflow.subprocess, "run", return_value=subprocess.CompletedProcess([], code, output, b"old CLI")), \
                    patch.object(workflow, "run") as download:
                with self.assertRaisesRegex(RuntimeError, "Run qcs-dev build"):
                    self.execute_models()
                download.assert_not_called()

    def test_exit_zero_model_verify_must_confirm_current_locked_files_and_directory(self):
        identifier = "qwen3-vl-embedding-2b"
        mutations = [
            lambda row: row.update(valid=False), lambda row: row.update(filesVerified=False),
            lambda row: row.update(modelId="embeddinggemma-2"), lambda row: row.update(dimensions=768),
            lambda row: row.update(directory=str(self.root / "shared-models")),
            lambda row: row["artifacts"][0].update(present=False),
            lambda row: row["artifacts"][0].update(bytes=1),
            lambda row: row["artifacts"][0].update(expectedBytes=1),
            lambda row: row["artifacts"][0].update(sha256="0" * 64),
            lambda row: row["artifacts"].__setitem__(1, row["artifacts"][0]),
        ]
        invalid = []
        for mutation in mutations:
            changed = self.model_response(identifier, verify=True)
            mutation(changed["result"])
            invalid.append(json.dumps(changed).encode())
        invalid += [b"not JSON", b'{"ok":false,"result":{}}', b'{"ok":true,"result":{}}']
        for output in invalid:
            with self.subTest(output=output), \
                    patch.object(workflow, "executable", return_value=self.root / "qcs-cli"), \
                    patch.object(workflow.subprocess, "run", side_effect=[
                        subprocess.CompletedProcess([], 0, json.dumps(self.model_response(identifier)).encode(), b""),
                        subprocess.CompletedProcess([], 0, output, b"")]), \
                    patch.object(workflow, "run") as download:
                with self.assertRaisesRegex(RuntimeError, "CLI model verification"):
                    self.execute_models([identifier])
                download.assert_called_once()
                self.assertEqual((self.reports / ("models-" + identifier + ".json")).read_bytes(), output)

    def test_doctor_records_running_python_and_handles_msvc_banner_exit(self):
        outputs = [("Python 3.12.10\n", "", 0),
                   ("", "Microsoft (R) C/C++ Optimizing Compiler Version 19.44.35217 for x64\n", 2)]
        def probe(command, **options):
            self.assertEqual(options["timeout"], 30)
            self.assertIs(options["env"], self.environment)
            stdout, stderr, code = outputs.pop(0)
            return subprocess.CompletedProcess(command, code, stdout, stderr)
        with patch.object(workflow.subprocess, "run", side_effect=probe) as execute:
            python = workflow.doctor_tool("python", self.environment, path=sys.executable)
            compiler = workflow.doctor_tool("cl", self.environment, path="fixture cl.exe")
        self.assertEqual(execute.call_args_list[0].args[0], [sys.executable, "--version"])
        self.assertEqual(execute.call_args_list[1].args[0], ["fixture cl.exe"])
        self.assertTrue(python["passed"])
        self.assertTrue(compiler["passed"])
        self.assertEqual(compiler["version"], "19.44.35217")

    def test_doctor_reports_missing_failed_and_timed_out_tools(self):
        with patch.object(workflow.shutil, "which", return_value=None):
            self.assertIn("missing from PATH", workflow.doctor_tool("ninja", self.environment)["error"])
        for failure in (OSError("not executable"), subprocess.TimeoutExpired("fixture", 30)):
            with self.subTest(failure=failure), patch.object(workflow.subprocess, "run", side_effect=failure):
                self.assertFalse(workflow.doctor_tool("git", self.environment, path="fixture")["passed"])
        with patch.object(workflow.subprocess, "run", return_value=subprocess.CompletedProcess("fixture", 1, "1.2.3", "broken")):
            self.assertIn("exit 1", workflow.doctor_tool("git", self.environment, path="fixture")["error"])

    def test_doctor_checks_all_required_qt_components_and_independent_pdf_version(self):
        prefix = self.root / "Qt SDK/6.12.0/msvc2022_64"
        components = ("", "Core", "Gui", "Widgets", "Network", "Sql", "Concurrent", "Test", "Xml",
                      "Svg", "Core5Compat", "Multimedia", "MultimediaWidgets", "Pdf", "PdfWidgets")
        for component in components:
            name = "Qt6" + component
            directory = prefix / "lib/cmake" / name
            directory.mkdir(parents=True)
            (directory / (name + "Config.cmake")).write_text("# fixture", encoding="utf-8")
            version = "6.140.0" if component.startswith("Pdf") else "6.12.0"
            (directory / (name + "ConfigVersionImpl.cmake")).write_text(f'set(PACKAGE_VERSION "{version}")', encoding="utf-8")
        self.assertEqual(workflow.doctor_qt(prefix)["errors"], [])
        with patch.object(workflow.platform, "system", return_value="Windows"):
            self.assertEqual(workflow.qt_prefix(self.root / "Qt SDK"), prefix)
            self.assertEqual(workflow.qt_prefix(prefix), prefix)
        (prefix / "lib/cmake/Qt6PdfWidgets/Qt6PdfWidgetsConfig.cmake").unlink()
        (prefix / "lib/cmake/Qt6Pdf/Qt6PdfConfigVersionImpl.cmake").write_text('set(PACKAGE_VERSION "6.12.0")', encoding="utf-8")
        errors = workflow.doctor_qt(prefix)["errors"]
        self.assertIn("Required Qt component Qt6PdfWidgets is missing", errors)
        self.assertIn("Qt6Pdf requires 6.140.0, found 6.12.0", errors)

    def test_doctor_gpu_preflight_uses_read_only_backend_validator(self):
        sdk = self.root / "absent GPU SDK"
        lock = self.root / "gpu-lock.json"
        lock.write_text("{}", encoding="utf-8")
        compiler = self.root / "toolkit/bin/nvcc.exe"
        module = argparse.Namespace(LOCK=lock, cuda_sdk=Mock(return_value={
            "cmakeCache": {"CUDAToolkit_ROOT": str(compiler.parent.parent)}, "environment": {}}),
            rocm_sdk=Mock(), vulkan_sdk=Mock())
        variant = {"backend": "cuda", "toolkitVersion": "12.4"}
        with patch.object(workflow, "build_variant", return_value=variant), \
                patch.object(workflow, "helper_module", return_value=module), \
                patch.object(workflow, "doctor_tool", return_value={"passed": True, "version": "12.4.99"}):
            result = workflow.doctor_gpu_sdk(self.args(gpu_sdk_root=sdk), self.root, self.environment)
        module.cuda_sdk.assert_called_once_with(variant, sdk, False, {})
        self.assertTrue(result["passed"])
        self.assertFalse(sdk.exists())
        module.rocm_sdk.assert_not_called()
        module.vulkan_sdk.assert_not_called()
        with patch.object(workflow.subprocess, "run", return_value=subprocess.CompletedProcess("fixture", 0, "value\n", "")) as probe:
            self.assertEqual(module.run(["sdk-python", "-m", "rocm_sdk", "path", "--root"], capture=True), "value")
        self.assertEqual(probe.call_args.kwargs["env"]["PYTHONDONTWRITEBYTECODE"], "1")
        self.assertEqual(probe.call_args.kwargs["timeout"], 30)

    def test_doctor_missing_gpu_sdk_does_not_create_or_download_sdk(self):
        sdk = self.root / "uninstalled SDK"
        module = workflow.helper_module("tools/qcs-gpu-sdk.py", "qcs_doctor_missing_sdk_test")
        variant = {"id": "windows-x64-cuda-12.4", "backend": "cuda", "os": "windows", "toolkitVersion": "12.4"}
        with patch.object(workflow, "build_variant", return_value=variant), \
                patch.object(workflow, "helper_module", return_value=module), \
                patch.dict(os.environ, {"CUDA_PATH": str(sdk), "CUDAToolkit_ROOT": str(sdk)}), \
                patch.object(module, "download", side_effect=AssertionError("doctor must not download")) as download:
            with self.assertRaisesRegex(RuntimeError, "CUDA 12.4 is unavailable"):
                workflow.doctor_gpu_sdk(self.args(gpu_sdk_root=sdk), self.root, self.environment)
        download.assert_not_called()
        self.assertFalse(sdk.exists())

    def test_doctor_failure_persists_aggregate_errors_before_stopping_check(self):
        def probe(name, *arguments, **options):
            version = {"python": "3.9.9", "cmake": "3.27.0", "ctest": "3.31.0", "cpack": "3.27.0"}.get(name, "1.2.3")
            return {"path": "fixture " + name, "passed": True, "version": version}
        with patch.object(workflow, "doctor_tool", side_effect=probe), \
                patch.object(workflow, "doctor_qt", return_value={"installed": False, "errors": ["Qt PDF missing"]}), \
                patch.object(workflow, "doctor_gpu_sdk", side_effect=RuntimeError("locked CUDA is missing")), \
                patch.object(workflow, "check_path_budget") as path_check, redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, "Toolchain preflight failed"):
                workflow.execute_action("doctor", self.args(), self.root, self.root / "Release",
                    self.root / "stage", self.reports, self.root / "SDK", self.root / "SDK/qt", self.environment)
        report = json.loads((self.reports / "doctor.json").read_text(encoding="utf-8"))
        self.assertFalse(report["passed"])
        self.assertEqual(len(report["errors"]), 5)
        self.assertIn("python requires 3.10+, found 3.9.9", report["errors"])
        self.assertIn("ctest 3.31.0 does not match cmake 3.27.0", report["errors"])
        self.assertIn("GPU SDK preflight failed: locked CUDA is missing", report["errors"])
        path_check.assert_called_once()

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

    def test_additional_presets_keep_complete_default_workflow_and_separate_package_reports(self):
        selected = []
        def execute(action, args, *arguments):
            selected.append((action, list(args.model), args.report_dir))
        with self.workflow_fixture(execute=execute):
            path = self.invoke_check(model=["qwen3-vl-embedding-2b", "qwen3-vl-embedding-8b"])
        document = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(document["models"], ["embeddinggemma-2", "qwen3-vl-embedding-2b", "qwen3-vl-embedding-8b"])
        self.assertEqual([action for action, _, _ in selected[:-4]], [action for action, _ in workflow.check_plan("Linux")])
        self.assertEqual(next(ids for action, ids, _ in selected if action == "models"), document["models"])
        self.assertEqual(selected[-4:], [
            (action, [identifier], path.parent if action == "test-models" else path.parent / ("package-" + identifier))
            for identifier in document["models"][1:] for action in ("test-models", "verify-package")])
        self.assertTrue(document["fullVerificationPassed"])

    def test_real_model_test_uses_requested_report_directory_and_model_roots(self):
        identifiers = ["qwen3-vl-embedding-2b", "qwen3-vl-embedding-8b"]
        source = self.root / "source"
        fixtures = source / "tests/runtime/fixtures/qwen"
        fixtures.mkdir(parents=True)
        for identifier in identifiers:
            (fixtures / (identifier + ".json")).write_text("{}", encoding="utf-8")
        destination = self.root / "selected-reports"
        arguments = self.args(model=identifiers, report_dir=destination, model_dir=self.root / "shared-models")
        binary = self.root / "build/qcs-tests-qwen-vectors"
        with patch.object(workflow, "SOURCE", source), \
                patch.object(workflow, "model_ids", return_value=identifiers), \
                patch.object(workflow, "executable", return_value=binary), \
                patch.object(workflow, "run") as execute:
            workflow.execute_action("test-models", arguments, self.root, self.root / "build",
                                    self.root / "stage", self.reports, self.root / "SDK",
                                    self.root / "SDK/qt", self.environment)
        self.assertEqual(execute.call_count, 2)
        for identifier, call in zip(identifiers, execute.call_args_list):
            command = call.args[0]
            self.assertEqual(command[command.index("--") + 1:],
                             [binary, "--model", identifier, "--model-root", arguments.model_dir,
                              "--fixtures", fixtures])
            self.assertNotIn("--validate-only", command)
            self.assertEqual(command[command.index("--timeout") + 1], "7200")
            result = json.loads((destination / ("oracle-" + identifier + ".json")).read_text(encoding="utf-8"))
            self.assertEqual(result["modelId"], identifier)
            self.assertTrue(result["passed"] and result["realModel"])
            self.assertFalse((self.reports / ("oracle-" + identifier + ".json")).exists())

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
