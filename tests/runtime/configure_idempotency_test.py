#!/usr/bin/env python3
"""Repeated configure preserves native source bytes and validates pinned caches."""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import tarfile
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

    def test_source_integrity_detects_changes_without_writing_upstream(self):
        self.assertIsNotNone(LIBGIT_SOURCE, "Pass the configured pinned libgit2 source directory")
        source_paths = [LIBGIT_SOURCE / name for name in (
            "src/util/fs_path.h", "src/util/fs_path.c", "src/util/win32/posix_w32.c",
            "include/git2/sys/config.h", "src/libgit2/CMakeLists.txt")]
        original = snapshot(source_paths)
        fixture_source = self.root / "pinned-source"
        for path in source_paths:
            destination = fixture_source / path.relative_to(LIBGIT_SOURCE)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)
        manifest = self.root / "metadata" / "source.manifest"
        script = self.root / "verify.cmake"
        script.write_text(
            f'include("{(SOURCE / "cmake/qcs-native-source-integrity.cmake").as_posix()}")\n'
            f'if(NOT EXISTS "{manifest.as_posix()}")\n'
            f'  qcopilots_search_source_manifest("{fixture_source.as_posix()}" content)\n'
            f'  file(WRITE "{manifest.as_posix()}" "${{content}}")\nendif()\n'
            f'qcopilots_search_verify_source("{fixture_source.as_posix()}" "{manifest.as_posix()}")\n',
            encoding="utf-8")
        self.run_cmake("-P", script)
        paths = [fixture_source / path.relative_to(LIBGIT_SOURCE) for path in source_paths]
        before = snapshot(paths + [manifest])
        self.run_cmake("-P", script)
        self.assertEqual(snapshot(paths + [manifest]), before)
        self.assertEqual(snapshot(source_paths), original)
        paths[0].write_bytes(paths[0].read_bytes() + b"\nmodified\n")
        result = subprocess.run([CMAKE, "-P", str(script)], capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=60)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("differs from its pinned archive", result.stdout + result.stderr)
        shutil.copy2(source_paths[0], paths[0])
        (fixture_source / "unexpected.h").write_text("addition", encoding="utf-8")
        result = subprocess.run([CMAKE, "-P", str(script)], capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=60)
        self.assertNotEqual(result.returncode, 0)

    def test_fetch_uses_digest_cache_and_validates_overrides(self):
        upstream = self.root / "archive-source"
        upstream.mkdir()
        (upstream / "file.h").write_text("pinned bytes\n", encoding="utf-8")
        archive = self.root / "source.tar.gz"
        with tarfile.open(archive, "w:gz") as handle:
            handle.add(upstream, arcname="package")
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        cache = self.root / "cache"
        old_cache = cache / "qcs_fixture-src"
        old_cache.mkdir(parents=True)
        (old_cache / "file.h").write_text("old patched content", encoding="utf-8")
        # FetchContent can leave an empty source directory after a failed download.
        (cache / f"qcs_fixture-{digest[:12]}-src").mkdir()
        (self.source / "CMakeLists.txt").write_text(
            'cmake_minimum_required(VERSION 3.28)\nproject(fetch_integrity NONE)\n'
            f'list(PREPEND CMAKE_MODULE_PATH "{(SOURCE / "cmake").as_posix()}")\n'
            f'set(Python3_EXECUTABLE "{Path(sys.executable).as_posix()}")\n'
            f'set(FETCHCONTENT_BASE_DIR "{cache.as_posix()}")\n'
            'include(qcs-native-deps)\n'
            f'qcopilots_search_fetch(qcs_fixture "{archive.as_posix()}" "{digest}")\n'
            'cmake_language(DEFER CALL qcopilots_search_finalize_native_source_records)\n'
            f'qcopilots_search_fetch(qcs_late_fixture "{archive.as_posix()}" "{digest}")\n'
            'file(WRITE "${CMAKE_BINARY_DIR}/source.txt" "${qcs_fixture_SOURCE_DIR}")\n',
            encoding="utf-8")
        self.run_cmake("-S", self.source, "-B", self.build)
        cached = Path((self.build / "source.txt").read_text(encoding="utf-8"))
        self.assertEqual(cached.name, f"qcs_fixture-{digest[:12]}-src")
        self.assertEqual((cached / "file.h").read_bytes(), (upstream / "file.h").read_bytes())
        self.assertEqual([p.name for p in cached.iterdir()], ["file.h"])
        records = (self.build / "nd/qcs-source-records.cmake").read_text(encoding="utf-8")
        self.assertEqual(records.count("qcopilots_search_verify_source("), 2)
        self.assertIn(f"qcs_late_fixture-{digest[:12]}-src", records)
        before = snapshot([cached / "file.h"])
        self.run_cmake("-S", self.source, "-B", self.build)
        self.assertEqual(snapshot([cached / "file.h"]), before)
        override = self.root / "override"
        shutil.copytree(upstream, override)
        override_option = f"-DFETCHCONTENT_SOURCE_DIR_QCS_FIXTURE={override.as_posix()}"
        self.run_cmake("-S", self.source, "-B", self.build, override_option)
        self.assertEqual(Path((self.build / "source.txt").read_text(encoding="utf-8")), override)
        (override / "file.h").write_text("modified", encoding="utf-8")
        result = subprocess.run([CMAKE, "-S", str(self.source), "-B", str(self.build), override_option],
                                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("differs from its pinned archive", result.stdout + result.stderr)


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
