"""Exercise portable Python fixture roots with isolated, writable inputs."""
from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from temp_paths import temporary_root


class TemporaryPathsTests(unittest.TestCase):
    def test_empty_root_uses_system_temp_without_writing_cwd(self):
        with tempfile.TemporaryDirectory(prefix="qcs-t-cwd-") as cwd:
            previous = Path.cwd()
            try:
                os.chdir(cwd)
                with patch.dict(os.environ, {"QCOPILOTS_SEARCH_TEST_ROOT": ""}):
                    with tempfile.TemporaryDirectory(prefix="qcs-t-empty-", dir=temporary_root()) as created:
                        self.assertEqual(Path(created).resolve().parent, Path(tempfile.gettempdir()).resolve())
                        self.assertEqual(list(Path(cwd).iterdir()), [])
            finally:
                os.chdir(previous)

    def test_unset_root_uses_system_temp(self):
        with patch.dict(os.environ):
            os.environ.pop("QCOPILOTS_SEARCH_TEST_ROOT", None)
            with tempfile.TemporaryDirectory(prefix="qcs-t-unset-", dir=temporary_root()) as created:
                self.assertEqual(Path(created).resolve().parent, Path(tempfile.gettempdir()).resolve())

    def test_unicode_root_is_created_and_instances_are_unique(self):
        with tempfile.TemporaryDirectory(prefix="qcs-t-root-") as parent:
            requested = Path(parent) / "中文 空格" / "root"
            with patch.dict(os.environ, {"QCOPILOTS_SEARCH_TEST_ROOT": str(requested)}):
                resolved = temporary_root()
                self.assertEqual(resolved, requested.resolve())
                with tempfile.TemporaryDirectory(prefix="qcs-t-parallel-", dir=resolved) as first, \
                        tempfile.TemporaryDirectory(prefix="qcs-t-parallel-", dir=resolved) as second:
                    self.assertNotEqual(first, second)
                    self.assertEqual(Path(first).parent, resolved)
                    self.assertEqual(Path(second).parent, resolved)
                self.assertEqual(list(resolved.iterdir()), [])

    def test_blocked_root_fails_without_fallback(self):
        with tempfile.TemporaryDirectory(prefix="qcs-t-blocked-") as parent:
            blocked = Path(parent) / "file"
            blocked.write_bytes(b"unchanged")
            for requested in (blocked, blocked / "child"):
                with self.subTest(requested=str(requested)), \
                        patch.dict(os.environ, {"QCOPILOTS_SEARCH_TEST_ROOT": str(requested)}):
                    with self.assertRaises(OSError):
                        temporary_root()
            self.assertEqual(blocked.read_bytes(), b"unchanged")
            self.assertEqual(list(Path(parent).iterdir()), [blocked])


if __name__ == "__main__":
    unittest.main(verbosity=2)
