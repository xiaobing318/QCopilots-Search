#!/usr/bin/env python3
"""Repeated configure must preserve unchanged native inputs and patch copies."""
from __future__ import annotations

import argparse
import hashlib
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

SOURCE = Path(__file__).resolve().parents[2]
CMAKE = shutil.which("cmake")
LIBGIT_SOURCE = None


def snapshot(paths):
    return {str(path): (path.stat().st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest())
            for path in paths}


class ConfigureIdempotencyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="qcs-configure-stable-", dir=temporary_root())
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "fixture 中文 空格"
        self.source.mkdir()
        self.build = self.root / "build"

    def run_cmake(self, *arguments):
        result = subprocess.run([CMAKE, *map(str, arguments)], cwd=self.root, capture_output=True,
                                text=True, encoding="utf-8", errors="replace", timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_repeated_configure_preserves_bytes_and_timestamps(self):
        payload = '中文;one;;two\\three\n${do_not_expand}\n@do_not_expand@\n"quotes"\n'
        (self.source / "input.txt").write_text(payload, encoding="utf-8", newline="")
        (self.source / "CMakeLists.txt").write_text(
            'cmake_minimum_required(VERSION 3.28)\nproject(configure_stable NONE)\n'
            f'include("{(SOURCE / "cmake/qcs-write-if-different.cmake").as_posix()}")\n'
            'set(do_not_expand "incorrect substitution")\n'
            'file(READ "${CMAKE_CURRENT_SOURCE_DIR}/input.txt" payload)\n'
            'qcopilots_search_write_if_different("${CMAKE_BINARY_DIR}/native.cpp" "${payload}")\n'
            'qcopilots_search_write_if_different("${CMAKE_BINARY_DIR}/find.cmake" "first;" "" "${payload}" "last;")\n'
            'qcopilots_search_write_if_different("${CMAKE_BINARY_DIR}/empty.h")\n', encoding="utf-8")
        self.run_cmake("-S", self.source, "-B", self.build)
        paths = [self.build / name for name in ("native.cpp", "find.cmake", "empty.h")]
        self.assertEqual(paths[0].read_bytes(), payload.replace("\n", os.linesep).encode())
        self.assertEqual(paths[1].read_bytes(), ("first;" + payload + "last;").replace("\n", os.linesep).encode())
        self.assertEqual(paths[2].read_bytes(), b"")
        # An old known timestamp makes accidental rewrites observable without sleeps.
        for path in paths:
            os.utime(path, ns=(1_000_000_000_000_000_000, 1_000_000_000_000_000_000))
        before = snapshot(paths)
        self.run_cmake("-S", self.source, "-B", self.build)
        self.assertEqual(snapshot(paths), before)
        (self.source / "input.txt").write_text(payload + "changed\n", encoding="utf-8", newline="")
        self.run_cmake("-S", self.source, "-B", self.build)
        self.assertEqual(paths[0].read_bytes(), (payload + "changed\n").replace("\n", os.linesep).encode())
        self.assertNotEqual(snapshot(paths)[str(paths[0])], before[str(paths[0])])
        self.assertEqual(snapshot(paths)[str(paths[2])], before[str(paths[2])])

    def test_pinned_git_preparation_preserves_all_cached_inputs(self):
        self.assertIsNotNone(LIBGIT_SOURCE, "Pass the configured pinned libgit2 source directory")
        source_paths = [LIBGIT_SOURCE / name for name in (
            "src/util/fs_path.h", "src/util/fs_path.c", "src/util/win32/posix_w32.c",
            "include/git2/sys/config.h", "src/libgit2/CMakeLists.txt")]
        original = snapshot(source_paths)
        # The preparation contract consumes these five pinned files. Keep this
        # regression fast by copying only its actual inputs, not upstream tests.
        fixture_source = self.root / "pinned-source"
        for path in source_paths:
            destination = fixture_source / path.relative_to(LIBGIT_SOURCE)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)
        script = self.root / "prepare.cmake"
        script.write_text(
            f'include("{(SOURCE / "cmake/qcs-native-git-patch.cmake").as_posix()}")\n'
            f'qcopilots_search_prepare_git_source("{fixture_source.as_posix()}" '
            f'"{self.build.as_posix()}" prepared)\n'
            f'file(WRITE "{(self.root / "prepared.txt").as_posix()}" "${{prepared}}")\n', encoding="utf-8")
        self.run_cmake("-P", script)
        prepared = Path((self.root / "prepared.txt").read_text(encoding="utf-8"))
        paths = [prepared / path.relative_to(LIBGIT_SOURCE) for path in source_paths]
        paths.append(prepared / ".qcs-patch-complete")
        patched_cmake = paths[-2].read_text(encoding="utf-8")
        self.assertIn('qcopilots_search_write_if_different("${PROJECT_BINARY_DIR}/include/', patched_cmake)
        self.assertNotIn('FILE(WRITE "${PROJECT_BINARY_DIR}/include/', patched_cmake)
        for path in paths:
            os.utime(path, ns=(1_000_000_000_000_000_000, 1_000_000_000_000_000_000))
        before = snapshot(paths)
        self.run_cmake("-P", script)
        self.assertEqual(snapshot(paths), before)
        self.assertEqual(snapshot(source_paths), original)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--cmake", default=CMAKE)
    parser.add_argument("--libgit-source", required=True, type=Path)
    options, remaining = parser.parse_known_args()
    CMAKE = options.cmake
    LIBGIT_SOURCE = options.libgit_source.resolve()
    if not CMAKE:
        parser.error("CMake is required")
    unittest.main(argv=[sys.argv[0], *remaining], verbosity=2)
