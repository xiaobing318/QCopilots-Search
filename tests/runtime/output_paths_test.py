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
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("must be outside the source tree", result.stderr)

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
                                    capture_output=True, text=True, timeout=30)
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
        result = subprocess.run([CMAKE, "-S", str(self.source), "-B", str(self.build)],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_install_prefix_override_is_checked_before_payload(self):
        self.configure_install_fixture()
        result = subprocess.run([CMAKE, "--install", str(self.build), "--prefix", str(self.source / "forbidden")],
                                capture_output=True, text=True, timeout=30)
        self.assert_rejected(result)
        self.assertFalse((self.source / "forbidden/payload.txt").exists())

    def test_install_preserves_external_prefix_and_literal_directory(self):
        directory = "literal-]]-bin"
        self.configure_install_fixture(destination="${CMAKE_INSTALL_BINDIR}",
                                       settings=f'set(CMAKE_INSTALL_BINDIR [=[{directory}]=])')
        prefix = self.root / "allowed stage"
        result = subprocess.run([CMAKE, "--install", str(self.build), "--prefix", str(prefix)],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((prefix / directory / "payload.txt").read_text(), "fixture")

    def test_install_destdir_uses_captured_absolute_directory(self):
        directory = "/" + self.source.name + "/forbidden"
        self.configure_install_fixture(destination="${CMAKE_INSTALL_BINDIR}",
                                       settings=f'set(CMAKE_INSTALL_BINDIR "{directory}")')
        environment = os.environ.copy()
        environment["DESTDIR"] = self.root.as_posix()
        result = subprocess.run([CMAKE, "--install", str(self.build), "--prefix", "/outside-prefix"],
                                env=environment, capture_output=True, text=True, timeout=30)
        self.assert_rejected(result)
        self.assertIn("DESTDIR/CMAKE_INSTALL_BINDIR", result.stderr)
        self.assertFalse((self.source / "forbidden").exists())


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--cmake", default=CMAKE)
    options, remaining = parser.parse_known_args()
    CMAKE = options.cmake
    if not CMAKE:
        parser.error("CMake is required")
    unittest.main(argv=[sys.argv[0], *remaining], verbosity=2)
