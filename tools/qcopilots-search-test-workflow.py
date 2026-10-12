#!/usr/bin/env python3
"""Safety and relocation contracts for the developer workflow."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.dont_write_bytecode = True
SCRIPT = Path(__file__).resolve().with_name("qcopilots-search-dev.py")
spec = importlib.util.spec_from_file_location("qcopilots_search_dev", SCRIPT)
workflow = importlib.util.module_from_spec(spec)
spec.loader.exec_module(workflow)
package_spec = importlib.util.spec_from_file_location("qcopilots_search_verify_package", SCRIPT.with_name("qcopilots-search-verify-package.py"))
package_verifier = importlib.util.module_from_spec(package_spec)
package_spec.loader.exec_module(package_verifier)


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        configured = os.environ.get("QCOPILOTS_SEARCH_TEST_ROOT")
        if configured:
            Path(configured).mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="qcopilots-search-workflow-", dir=configured)
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def invoke(self, *arguments):
        return subprocess.run([sys.executable, str(SCRIPT), *map(str, arguments)], cwd=self.root,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                              encoding="utf-8", errors="replace", timeout=30)

    def test_source_tree_and_descendant_rejected(self):
        for path in (workflow.SOURCE, workflow.SOURCE / "nested output"):
            with self.subTest(path=path), self.assertRaises(RuntimeError):
                workflow.assert_external(path)

    def test_volume_root_rejected(self):
        with self.assertRaises(RuntimeError):
            workflow.assert_external(Path(self.root.anchor))

    def test_qt_deploy_rejects_zero_exit_error_diagnostics(self):
        helper = workflow.SOURCE / "cmake/QCopilotsSearchCheckedDeploy.cmake"
        deployment = self.root / "deploy script.cmake"
        wrapper = self.root / "checked deployment.cmake"
        prefix = self.root / "安装 空格"
        wrapper.write_text(
            f'include("{helper.as_posix()}")\n'
            f'qcopilots_search_checked_deploy("{deployment.as_posix()}")\n',
            encoding="utf-8")
        cases = [
            ('message(STATUS "Deployment complete")\n', True),
            ('execute_process(COMMAND "${CMAKE_COMMAND}" -E echo "ERROR: unresolved library")\n', False),
            ('message("ERROR: unresolved library")\n', False),
            ('message(FATAL_ERROR "Tool returned a failure")\n', False),
        ]
        for program, expected in cases:
            with self.subTest(program=program):
                deployment.write_text(
                    f'if(NOT CMAKE_INSTALL_PREFIX STREQUAL "{prefix.as_posix()}" OR '
                    'NOT CMAKE_INSTALL_CONFIG_NAME STREQUAL "Release")\n'
                    'message(FATAL_ERROR "Installation context was lost")\nendif()\n' + program,
                    encoding="utf-8")
                result = subprocess.run(
                    ["cmake", f"-DCMAKE_INSTALL_PREFIX={prefix.as_posix()}",
                     "-DCMAKE_INSTALL_CONFIG_NAME=Release", "-P", str(wrapper)],
                    cwd=self.root, capture_output=True, text=True, encoding="utf-8", timeout=30)
                self.assertEqual(result.returncode == 0, expected, result.stdout + result.stderr)
                if not expected:
                    self.assertIn("Qt deployment failed", result.stderr)

    def test_distribution_archive_traversal_rejected_before_write(self):
        import zipfile
        package = self.root / "qcopilots-search-test.zip"
        for member in ("../escaped.txt", "C:/escaped.txt", "/escaped.txt", "nested/../../escaped.txt"):
            with self.subTest(member=member):
                with zipfile.ZipFile(package, "w") as archive:
                    archive.writestr(member, "must not be written")
                destination = self.root / ("unpack-" + str(len(list(self.root.iterdir()))))
                with self.assertRaises(RuntimeError):
                    package_verifier.unpack(package, destination)
                self.assertFalse((self.root / "escaped.txt").exists())

    @unittest.skipUnless(os.name == "nt", "NSIS is native to Windows")
    def test_installer_guard_rejects_existing_start_menu_before_execution(self):
        from unittest.mock import patch
        appdata = self.root / "test AppData"
        (appdata / "Microsoft/Windows/Start Menu/Programs/QCopilots Search").mkdir(parents=True)
        with patch.dict(os.environ, {"APPDATA": str(appdata)}), self.assertRaises(RuntimeError):
            package_verifier.NsisInstallation(self.root / "installer.exe", self.root / "install")
        self.assertFalse((self.root / "install").exists())

    def test_two_checkouts_cannot_share_build_root(self):
        build = self.root / "build"
        workflow.initialize_root(build)
        (build / workflow.MARKER).write_text(json.dumps({"source": str(self.root / "other checkout")}), encoding="utf-8")
        with self.assertRaises(RuntimeError):
            workflow.initialize_root(build)

    def test_help_from_arbitrary_working_directory(self):
        result = self.invoke("--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--build-root", result.stdout)

    def test_clean_preserves_unowned_directory(self):
        build = self.root / "build 中文 空间"
        release = build / "release"
        release.mkdir(parents=True)
        sentinel = release / "user-data.txt"
        sentinel.write_text("preserve", encoding="utf-8")
        result = self.invoke("clean", "--build-root", build)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "preserve")

    def test_clean_removes_only_owned_configuration(self):
        build = self.root / "owned output"
        release = build / "release"
        release.mkdir(parents=True)
        (release / "CMakeCache.txt").write_text("CMAKE_HOME_DIRECTORY:INTERNAL=" + workflow.SOURCE.as_posix() + "\n", encoding="utf-8")
        preserved = build / "development-profile"
        preserved.mkdir()
        result = self.invoke("clean", "--build-root", build)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(release.exists())
        self.assertTrue(preserved.is_dir())

    def test_disk_guard_reports_failure(self):
        with self.assertRaises(RuntimeError):
            workflow.disk_guard(self.root, 1e12)

    def test_qt_sdk_relocation_with_unicode(self):
        sdk = self.root / "SDK 中文 空格"
        kit = {"Windows": "msvc2022_64", "Darwin": "macos", "Linux": "gcc_64"}[workflow.platform.system()]
        prefix = sdk / "6.12.0" / kit
        cmake = prefix / "lib/cmake/Qt6"
        cmake.mkdir(parents=True)
        (cmake / "Qt6Config.cmake").write_text("# fixture", encoding="utf-8")
        self.assertEqual(workflow.qt_prefix(sdk), prefix.resolve())
        self.assertEqual(workflow.qt_prefix(prefix), prefix.resolve())

    def test_unicode_diagnostic_works_with_redirected_cp1252_environment(self):
        build = self.root / "构建 中文 空格"
        environment = os.environ.copy()
        environment["PYTHONIOENCODING"] = "cp1252"
        result = subprocess.run([sys.executable, str(SCRIPT), "doctor", "--build-root", str(build),
                                 "--qt-root", str(self.root / "不存在的 SDK")], cwd=self.root,
                                env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, 1)
        self.assertIn("构建 中文 空格", result.stdout)
        self.assertNotIn("UnicodeEncodeError", result.stderr)

    def test_relocated_checkout_configure_and_clean_with_unicode(self):
        # A language-free CMake fixture exercises the real wrappers and CMake
        # path handling without duplicating the application's native build.
        checkout = self.root / "源码 中文 空格"
        tools = checkout / "tools"
        tools.mkdir(parents=True)
        import shutil
        shutil.copy2(SCRIPT, tools / SCRIPT.name)
        (checkout / "CMakeLists.txt").write_text("cmake_minimum_required(VERSION 3.28)\nproject(qcopilots-search-relocation-test NONE)\n", encoding="utf-8")
        environment = os.environ.copy()
        environment["QCOPILOTS_SEARCH_PYTHON"] = sys.executable
        if os.name == "nt":
            wrapper = checkout / "qcopilots-search-dev.ps1"
            shutil.copy2(workflow.SOURCE / wrapper.name, wrapper)
            command = [shutil.which("pwsh") or shutil.which("powershell"), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(wrapper)]
        else:
            wrapper = checkout / "qcopilots-search-dev.sh"
            shutil.copy2(workflow.SOURCE / wrapper.name, wrapper)
            command = ["sh", str(wrapper)]
        build = self.root / "构建 output"
        result = subprocess.run([*command, "configure", "--build-root", str(build), "--qt-root", str(self.root / "SDK 空格")],
                                cwd=self.root, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, encoding="utf-8", timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(str(checkout), result.stdout)
        self.assertTrue((build / "release/CMakeCache.txt").is_file())
        result = subprocess.run([sys.executable, str(tools / SCRIPT.name), "clean", "--build-root", str(build)],
                                cwd=self.root, env=environment, capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((build / "release").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
