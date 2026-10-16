#!/usr/bin/env python3
"""Exercise direct CMake output guards before any generated payload is written."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "support"))
from temp_paths import temporary_root

MODULE = Path(__file__).resolve().parents[2] / "cmake/qcs-output-paths.cmake"
CMAKE = shutil.which("cmake")


class OutputPathTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="qcs-output-paths-", dir=temporary_root())
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source 中文 空格"
        self.source.mkdir()
        self.build = self.root / "build"
        self.build.mkdir()

    def probe(self, code, *, environment=None):
        script = self.build / "probe.cmake"
        script.write_text(
            f'set(CMAKE_BINARY_DIR "{self.build.as_posix()}")\n'
            f'set(CMAKE_SOURCE_DIR "{self.source.as_posix()}")\n'
            f'set(Python3_EXECUTABLE "{Path(sys.executable).as_posix()}")\n'
            f'include("{MODULE.as_posix()}")\n' + code + '\n', encoding="utf-8")
        return subprocess.run([CMAKE, "-P", str(script)], cwd=self.build,
                              env=environment, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=30)

    def assert_rejected(self, result):
        self.assertNotEqual(result.returncode, 0, (result.stdout or "") + (result.stderr or ""))
        self.assertIn("must be outside the source tree", result.stderr or "")

    def run_cmake_fixture(self, arguments, *, configure=False, environment=None):
        selectors = {"CMAKE_GENERATOR", "CMAKE_GENERATOR_PLATFORM", "CMAKE_GENERATOR_TOOLSET",
                     "CMAKE_GENERATOR_INSTANCE", "CMAKE_TOOLCHAIN_FILE"}
        inherited = os.environ if environment is None else environment
        fixture_env = {name: value for name, value in inherited.items() if name.upper() not in selectors}
        cmake = shutil.which(CMAKE, path=fixture_env.get("PATH"))
        self.assertIsNotNone(cmake, "Output path fixture requires CMake")
        command = [str(Path(cmake).resolve()), *arguments]
        if configure:
            ninja = shutil.which("ninja", path=fixture_env.get("PATH"))
            self.assertIsNotNone(ninja, "Output path fixture requires Ninja")
            command.extend(["-G", "Ninja", f"-DCMAKE_MAKE_PROGRAM:FILEPATH={Path(ninja).resolve()}"])
        timeout = 60 if configure else 30
        try:
            return subprocess.run(command, cwd=self.root, env=fixture_env, capture_output=True,
                                  text=True, encoding="utf-8", errors="replace", timeout=timeout)
        except subprocess.TimeoutExpired as error:
            def output_text(value):
                return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value or ""
            self.fail(
                f"Output path fixture timed out after {timeout} seconds\n"
                f"Resolved command: {command!r}\nWorking directory: {self.root}\n"
                f"stdout:\n{output_text(error.stdout)}\nstderr:\n{output_text(error.stderr)}")

    def test_external_and_same_prefix_sibling_are_allowed(self):
        for output in (self.root / "runs", self.root / (self.source.name + "-build")):
            result = self.probe(f'qcopilots_search_require_external_path(test "{output.as_posix()}")')
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertFalse(output.exists(), "Validation must not create outputs")

    def test_source_and_uncreated_descendant_are_rejected(self):
        for output in (self.source, self.source / "new/dependency/cache"):
            self.assert_rejected(self.probe(f'qcopilots_search_require_external_path(test "{output.as_posix()}")'))
            self.assertFalse((self.source / "new").exists())

    def test_relative_and_normalized_descendants_are_rejected(self):
        for output in (f"../{self.source.name}/runs", f"{self.source.as_posix()}/existing/../runs"):
            self.assert_rejected(self.probe(f'qcopilots_search_require_external_path(test "{output}")'))

    def test_empty_output_is_rejected(self):
        result = self.probe('qcopilots_search_require_external_path(test "")')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must name an external output directory", result.stderr)

    def test_case_insensitive_windows_source_boundary(self):
        output = self.source.as_posix().upper() if os.name == "nt" else self.source.as_posix()
        self.assert_rejected(self.probe(f'qcopilots_search_require_external_path(test "{output}/runs")'))

    def test_linked_existing_parent_cannot_hide_source_descendant(self):
        link = self.root / "linked source"
        if os.name == "nt":
            result = subprocess.run(["cmd.exe", "/c", "mklink", "/J", str(link), str(self.source)],
                                    capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.addCleanup(os.rmdir, link)
        else:
            link.symlink_to(self.source, target_is_directory=True)
            self.addCleanup(link.unlink)
        self.assert_rejected(self.probe(f'qcopilots_search_require_external_path(test "{link.as_posix()}/new/cache")'))
        self.assertFalse((self.source / "new").exists())

    def test_absolute_and_relative_install_destinations_are_rejected(self):
        prefix = self.root / "stage"
        for destination in (self.source.as_posix() + "/bin", "../" + self.source.name + "/bin"):
            self.assert_rejected(self.probe(
                f'set(CMAKE_INSTALL_PREFIX "{prefix.as_posix()}")\n'
                f'set(CMAKE_INSTALL_BINDIR "{destination}")\n'
                'qcopilots_search_validate_install_paths("${CMAKE_SOURCE_DIR}")'))

    def test_destdir_cannot_redirect_an_install_into_source(self):
        environment = os.environ.copy()
        environment["DESTDIR"] = self.source.as_posix()
        self.assert_rejected(self.probe(
            'set(CMAKE_INSTALL_PREFIX "/qcs-install")\n'
            'set(CMAKE_INSTALL_BINDIR "bin")\n'
            'qcopilots_search_validate_install_paths("${CMAKE_SOURCE_DIR}")', environment=environment))

    def configure_install_fixture(self, *, destination=".", settings=""):
        fixture = self.source / "CMakeLists.txt"
        fixture.write_text(
            'cmake_minimum_required(VERSION 3.28)\nproject(output_guard NONE)\n'
            f'set(Python3_EXECUTABLE "{Path(sys.executable).as_posix()}")\n'
            f'include("{MODULE.as_posix()}")\n' + settings + '\n'
            'qcopilots_search_install_output_guard("${CMAKE_SOURCE_DIR}")\n'
            'file(WRITE "${CMAKE_BINARY_DIR}/payload.txt" "fixture")\n'
            f'install(FILES "${{CMAKE_BINARY_DIR}}/payload.txt" DESTINATION "{destination}")\n', encoding="utf-8")
        result = self.run_cmake_fixture(["-S", str(self.source), "-B", str(self.build)], configure=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_install_prefix_override_is_checked_before_payload(self):
        self.configure_install_fixture()
        result = self.run_cmake_fixture(["--install", str(self.build), "--prefix", str(self.source / "forbidden")])
        self.assert_rejected(result)
        self.assertFalse((self.source / "forbidden/payload.txt").exists())

    def test_install_preserves_external_prefix_and_literal_directory(self):
        directory = "literal-]]-bin"
        self.configure_install_fixture(destination="${CMAKE_INSTALL_BINDIR}",
                                       settings=f'set(CMAKE_INSTALL_BINDIR [=[{directory}]=])')
        prefix = self.root / "allowed-stage"
        result = self.run_cmake_fixture(["--install", str(self.build), "--prefix", str(prefix)])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((prefix / directory / "payload.txt").read_text(), "fixture")

    def test_install_destdir_uses_captured_absolute_directory(self):
        directory = "/" + self.source.name + "/forbidden"
        self.configure_install_fixture(destination="${CMAKE_INSTALL_BINDIR}",
                                       settings=f'set(CMAKE_INSTALL_BINDIR "{directory}")')
        environment = os.environ.copy()
        environment["DESTDIR"] = self.root.as_posix()
        result = self.run_cmake_fixture(["--install", str(self.build), "--prefix", "/outside-prefix"],
                                        environment=environment)
        self.assert_rejected(result)
        self.assertIn("DESTDIR/CMAKE_INSTALL_BINDIR", result.stderr)
        self.assertFalse((self.source / "forbidden").exists())

    def test_install_diagnostics_do_not_use_cp1252_default(self):
        # U+5E90 contains UTF-8 byte 0x90, which is undefined in CP1252. Exercise
        # both real install failures under the hosted Windows default encoding.
        unicode_source = self.root / "source 中文 庐"
        self.source.rename(unicode_source)
        self.source = unicode_source
        with mock.patch.object(subprocess, "_text_encoding", return_value="cp1252") as default_encoding:
            self.test_install_prefix_override_is_checked_before_payload()
            self.test_install_destdir_uses_captured_absolute_directory()
        default_encoding.assert_not_called()

    def test_nsis_icon_path_survives_cpack_generation_and_parsing(self):
        # Execute the production setting through both CMake parsing stages,
        # then let CPack consume the generated config without requiring NSIS.
        package_module = MODULE.with_name("qcs-package.cmake")
        settings = [line.strip() for line in package_module.read_text(encoding="utf-8").splitlines()
                    if line.strip().startswith("set(CPACK_NSIS_INSTALLED_ICON_NAME ")]
        self.assertEqual(len(settings), 1)
        (self.source / "CMakeLists.txt").write_text(
            'cmake_minimum_required(VERSION 3.28)\nproject(qcs_icon_fixture NONE)\n'
            + settings[0] + '\n'
            'set(CPACK_PACKAGE_VERSION 1.0.0)\nset(CPACK_PACKAGE_FILE_NAME qcs-icon-fixture)\n'
            'set(CPACK_GENERATOR ZIP)\n'
            'file(WRITE "${CMAKE_BINARY_DIR}/payload.txt" "fixture")\n'
            'install(FILES "${CMAKE_BINARY_DIR}/payload.txt" DESTINATION bin)\n'
            'include(CPack)\n', encoding="utf-8")
        result = self.run_cmake_fixture(["-S", str(self.source), "-B", str(self.build)], configure=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        parse = self.build / "parse-config.cmake"
        parse.write_text(
            'cmake_minimum_required(VERSION 3.28)\n'
            f'include("{(self.build / "CPackConfig.cmake").as_posix()}")\n'
            f'file(WRITE "{(self.build / "decoded-icon.txt").as_posix()}" "${{CPACK_NSIS_INSTALLED_ICON_NAME}}")\n',
            encoding="utf-8")
        result = self.run_cmake_fixture(["-P", str(parse)])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.build / "decoded-icon.txt").read_text(encoding="utf-8"), r"bin\qcs-gui.exe")
        cpack = Path(CMAKE).with_name("cpack.exe" if os.name == "nt" else "cpack")
        result = subprocess.run([str(cpack), "--config", str(self.build / "CPackConfig.cmake"),
                                 "-B", str(self.root / "packages"), "-G", "ZIP"],
                                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("Invalid escape sequence", result.stdout + result.stderr)
        self.assertTrue((self.root / "packages/qcs-icon-fixture.zip").is_file())


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--cmake", default=CMAKE)
    options, remaining = parser.parse_known_args()
    CMAKE = options.cmake
    if not CMAKE:
        parser.error("CMake is required")
    unittest.main(argv=[sys.argv[0], *remaining], verbosity=2)
