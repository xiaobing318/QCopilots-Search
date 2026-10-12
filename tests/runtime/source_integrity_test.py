#!/usr/bin/env python3
"""Exercise the actual locked llama source archive without writable source fixtures."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "support"))
from temp_paths import temporary_root

ARCHIVE_SHA = "778ee590ec7447075e08f2daf6301311ae7f8d9bf810a0b849f26af3f6d10867"
PATCHED_SHA = {
    "src/llama-context.cpp": "513f5cc789180f4f094217c7397a53aa6cbda7c0a7c2aa239fe1cd0b19a2c025",
    "ggml/src/ggml-backend-reg.cpp": "bfb21d94932be91e243e2746f0d19e8822a6ad91502c67b408e0d1d4d29828c9",
}
REPO = Path(__file__).resolve().parents[2]
OPTIONS: argparse.Namespace


class SourceIntegrityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        archive = Path(OPTIONS.archive).resolve(strict=True)
        if hashlib.sha256(archive.read_bytes()).hexdigest() != ARCHIVE_SHA:
            raise AssertionError("Input archive differs from the locked llama b11461 SHA256")
        temp_root = temporary_root()
        cls.temporary = tempfile.TemporaryDirectory(prefix="qcs-source-", dir=temp_root)
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.root = Path(cls.temporary.name).resolve()
        cls.source = cls.root / "source 中文 archive"
        cls.source.mkdir()
        with tarfile.open(archive, "r:gz") as packed:
            for entry in packed:
                if entry.isdir():
                    continue
                if not entry.isfile():
                    raise AssertionError("Locked archive unexpectedly contains a link")
                parts = PurePosixPath(entry.name).parts[1:]
                if not parts or any(part in ("..", ".") for part in parts):
                    raise AssertionError("Invalid archive member path")
                destination = cls.source.joinpath(*parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                with packed.extractfile(entry) as source, destination.open("wb") as output:
                    shutil.copyfileobj(source, output)
        # The archive lives in an unrelated Git repository with no commit.
        # Neither verification nor git apply may accidentally use this repo.
        subprocess.run([OPTIONS.git, "init", "--quiet", str(cls.root)], check=True)
        cls.git_config = cls.root / "isolated.gitconfig"
        cls.git_config.write_text("[core]\n\tautocrlf = true\n", encoding="utf-8")
        cls.environment = os.environ.copy()
        cls.environment.update(GIT_CONFIG_GLOBAL=str(cls.git_config), GIT_CONFIG_NOSYSTEM="1")
        for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
            cls.environment.pop(name, None)
        cls.driver = cls.root / "verify.cmake"
        cls.driver.write_text(
            'cmake_minimum_required(VERSION 3.28)\n'
            'find_package(Git REQUIRED)\n'
            'include("${VERIFIER}")\n'
            'if(ACTION STREQUAL "prepare")\n'
            '  qcopilots_search_prepare_llama_source("${SOURCE}" "${PATCHES}")\n'
            'else()\n'
            '  qcopilots_search_verify_llama_source("${SOURCE}")\n'
            'endif()\n', encoding="utf-8")

    def check_cmake(self, *, action="verify", source=None, verifier=None,
                    patches=None, failure=None) -> None:
        result = subprocess.run([
            OPTIONS.cmake,
            f"-DGIT_EXECUTABLE={OPTIONS.git}",
            f"-DSOURCE={source or self.source}",
            f"-DVERIFIER={verifier or REPO / 'src/runtime/verify_llama_source.cmake'}",
            f"-DPATCHES={patches or REPO / 'third_party/patches'}",
            f"-DACTION={action}", "-P", str(self.driver),
        ], cwd=self.root, env=self.environment, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=180)
        details = result.stdout + result.stderr
        if failure:
            self.assertNotEqual(result.returncode, 0, details)
            self.assertIn(failure, " ".join(details.split()))
        else:
            self.assertEqual(result.returncode, 0, details)

    def test_01_pristine_archive(self):
        self.check_cmake()

    def test_02_patches_in_unrelated_repository_and_autocrlf_true(self):
        self.check_cmake(action="prepare")
        for relative, expected in PATCHED_SHA.items():
            self.assertEqual(hashlib.sha256((self.source / relative).read_bytes()).hexdigest(), expected)

    def test_03_repeated_prepare_and_autocrlf_false(self):
        self.git_config.write_text("[core]\n\tautocrlf = false\n", encoding="utf-8")
        self.check_cmake(action="prepare")
        for relative, expected in PATCHED_SHA.items():
            self.assertEqual(hashlib.sha256((self.source / relative).read_bytes()).hexdigest(), expected)

    def test_04_reject_modified_source(self):
        path = self.source / "CMakeLists.txt"
        original = path.read_bytes()
        try:
            path.write_bytes(original + b"\n# unexpected modification\n")
            self.check_cmake(failure="differs from its locked hash: CMakeLists.txt")
        finally:
            path.write_bytes(original)

    def test_05_reject_modified_patched_source(self):
        path = self.source / "ggml/src/ggml-backend-reg.cpp"
        original = path.read_bytes()
        try:
            path.write_bytes(original + b"\n// unexpected modification\n")
            self.check_cmake(failure="ggml/src/ggml-backend-reg.cpp")
        finally:
            path.write_bytes(original)

    def test_06_reject_extra_or_missing_file(self):
        extra = self.source / "unreviewed.cmake"
        try:
            extra.write_bytes(b"# extra source\n")
            self.check_cmake(failure="source file set differs")
        finally:
            extra.unlink()
        path = self.source / "CMakeLists.txt"
        original = path.read_bytes()
        try:
            path.unlink()
            self.check_cmake(failure="source file set differs")
        finally:
            path.write_bytes(original)

    def test_07_reject_modified_manifest(self):
        directory = self.root / "changed verifier"
        directory.mkdir()
        for name in ("verify_llama_source.cmake", "llama-source-manifest.sha256"):
            shutil.copyfile(REPO / "src/runtime" / name, directory / name)
        with (directory / "llama-source-manifest.sha256").open("ab") as output:
            output.write(b"\n")
        self.check_cmake(verifier=directory / "verify_llama_source.cmake",
                         failure="source manifest differs from its reviewed hash")

    def test_08_reject_modified_patch(self):
        directory = self.root / "changed patches"
        directory.mkdir()
        for path in (REPO / "third_party/patches").glob("*.patch"):
            shutil.copyfile(path, directory / path.name)
        with (directory / "llama.cpp-no-logits.patch").open("ab") as output:
            output.write(b"\n")
        self.check_cmake(action="prepare", patches=directory,
                         failure="differs from its reviewed hash")

    def test_09_reject_directory_link(self):
        link = self.source / "outside-source"
        target = self.root / "outside"
        target.mkdir()
        junction = False
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError as error:
            if os.name != "nt" or error.winerror != 1314:
                raise
            # Windows directory junctions exercise the same IS_SYMLINK check
            # without requiring the host's optional symlink privilege.
            environment = self.environment.copy()
            environment.update(QCOPILOTS_SEARCH_LINK_TEST_PATH=str(link),
                               QCOPILOTS_SEARCH_LINK_TEST_TARGET=str(target))
            subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                            "$ErrorActionPreference = 'Stop'; New-Item -ItemType Junction "
                            "-Path $env:QCOPILOTS_SEARCH_LINK_TEST_PATH "
                            "-Target $env:QCOPILOTS_SEARCH_LINK_TEST_TARGET | Out-Null"],
                           check=True, env=environment, capture_output=True, text=True)
            junction = True
        try:
            self.check_cmake(failure="archive source contains a symlink")
        finally:
            if junction:
                link.rmdir()
            else:
                link.unlink()

    def test_10_reject_wrong_git_commit(self):
        source = self.root / "foreign checkout"
        source.mkdir()
        subprocess.run([OPTIONS.git, "init", "--quiet", str(source)], check=True, env=self.environment)
        subprocess.run([OPTIONS.git, "-C", str(source), "-c", "user.name=Source integrity test",
                        "-c", "user.email=test@example.invalid", "commit", "--quiet", "--allow-empty",
                        "-m", "Unrelated source"], check=True, env=self.environment)
        self.check_cmake(source=source, failure="does not match the locked commit")


class RecordedResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.records = []
        self.started = time.monotonic()

    def startTest(self, test):
        self.started = time.monotonic()
        super().startTest(test)

    def record(self, test, status, detail=""):
        self.records.append({"case": test.id(), "status": status,
                             "seconds": round(time.monotonic() - self.started, 3), "detail": detail})

    def addSuccess(self, test):
        super().addSuccess(test)
        self.record(test, "passed")

    def addFailure(self, test, error):
        super().addFailure(test, error)
        self.record(test, "failed", self._exc_info_to_string(error, test))

    def addError(self, test, error):
        super().addError(test, error)
        self.record(test, "error", self._exc_info_to_string(error, test))

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self.record(test, "skipped", reason)


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", required=True, help="Locked llama-b11461.tar.gz")
    parser.add_argument("--cmake", default=shutil.which("cmake"))
    parser.add_argument("--git", default=shutil.which("git"))
    parser.add_argument("--report", type=Path, help="Optional JSON evidence path outside the source tree")
    OPTIONS = parser.parse_args()
    if not OPTIONS.cmake or not OPTIONS.git:
        parser.error("CMake and Git are required")
    result = unittest.TextTestRunner(verbosity=2, resultclass=RecordedResult).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(SourceIntegrityTests))
    if OPTIONS.report:
        OPTIONS.report.parent.mkdir(parents=True, exist_ok=True)
        OPTIONS.report.write_text(json.dumps({"archive_sha256": ARCHIVE_SHA,
            "tests_run": result.testsRun, "successful": result.wasSuccessful(),
            "passed": result.wasSuccessful() and result.testsRun > 0 and not result.skipped,
            "skipped": len(result.skipped), "cases": result.records}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")
    sys.exit(0 if result.wasSuccessful() else 1)
