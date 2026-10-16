#!/usr/bin/env python3
"""Exercise locked llama.cpp source verification using isolated temporary copies."""
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

REPO = Path(__file__).resolve().parents[2]
LOCK = json.loads((REPO / "dependencies.lock.json").read_text(encoding="utf-8"))["llama.cpp"]
ARCHIVE_SHA = LOCK["sha256"]
OPTIONS: argparse.Namespace


class SourceIntegrityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        temp_root = temporary_root()
        cls.temporary = tempfile.TemporaryDirectory(prefix="qcs-source-", dir=temp_root)
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.root = Path(cls.temporary.name).resolve()
        cls.source = cls.root / "source 中文 archive"
        if OPTIONS.source:
            source = Path(OPTIONS.source).resolve(strict=True)
            shutil.copytree(source, cls.source, symlinks=True,
                            ignore=lambda directory, names: [".git"] if Path(directory) == source else [])
        else:
            archive = Path(OPTIONS.archive).resolve(strict=True)
            if hashlib.sha256(archive.read_bytes()).hexdigest() != ARCHIVE_SHA:
                raise AssertionError("Input archive differs from the locked llama.cpp SHA256")
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
        # Verification must not accidentally use this repository.
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
            'qcopilots_search_verify_llama_source("${SOURCE}")\n', encoding="utf-8")

    def copy_verifier(self, name):
        project = self.root / name
        directory = project / "src/runtime"
        directory.mkdir(parents=True)
        for filename in ("verify_llama_source.cmake", "verify_llama_source.py", "llama-source-manifest.sha256"):
            shutil.copyfile(REPO / "src/runtime" / filename, directory / filename)
        shutil.copyfile(REPO / "dependencies.lock.json", project / "dependencies.lock.json")
        return project, directory / "verify_llama_source.cmake"

    def check_cmake(self, *, source=None, verifier=None, failure=None) -> None:
        result = subprocess.run([
            OPTIONS.cmake,
            f"-DGIT_EXECUTABLE={OPTIONS.git}",
            f"-DPython3_EXECUTABLE={sys.executable}",
            f"-DSOURCE={source or self.source}",
            f"-DVERIFIER={verifier or REPO / 'src/runtime/verify_llama_source.cmake'}",
            "-P", str(self.driver),
        ], cwd=self.root, env=self.environment, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=180)
        details = result.stdout + result.stderr
        if failure:
            self.assertNotEqual(result.returncode, 0, details)
            self.assertIn(failure, " ".join(details.split()))
        else:
            self.assertEqual(result.returncode, 0, details)

    def test_01_pristine_archive(self):
        if OPTIONS.source:
            self.check_cmake(source=Path(OPTIONS.source).resolve(strict=True))
        self.check_cmake()

    def test_02_repeated_verification_does_not_write_source(self):
        files = sorted(path for path in self.source.rglob("*") if path.is_file())
        before = {path: (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
                  for path in files}
        try:
            for path in files:
                path.chmod(0o444)
            self.check_cmake()
            self.git_config.write_text("[core]\n\tautocrlf = false\n", encoding="utf-8")
            self.check_cmake()
            after = {path: (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
                     for path in self.source.rglob("*") if path.is_file()}
            self.assertEqual(before, after)
        finally:
            for path in files:
                path.chmod(0o644)

    def test_04_reject_modified_source(self):
        path = self.source / "CMakeLists.txt"
        original = path.read_bytes()
        try:
            path.write_bytes(original + b"\n# unexpected modification\n")
            self.check_cmake(failure="differs from its locked hash: CMakeLists.txt")
        finally:
            path.write_bytes(original)

    def test_05_reject_modified_backend_source(self):
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
        _, verifier = self.copy_verifier("changed verifier")
        with (verifier.parent / "llama-source-manifest.sha256").open("ab") as output:
            output.write(b"\n")
        self.check_cmake(verifier=verifier, failure="source manifest differs from its reviewed hash")

    def test_08_build_checks_the_actual_compiler_source_before_and_after(self):
        fixture = self.root / "build guard"
        fixture.mkdir()
        build = self.root / "guard-build"
        compiled = build / "compiled-source.txt"
        source_file = self.source / "src/llama-context.cpp"
        original = source_file.read_bytes()
        (fixture / "consume.cmake").write_text(
            'file(COPY_FILE "${SOURCE_FILE}" "${OUTPUT_FILE}")\n'
            'if(MUTATE_SOURCE)\n'
            '  file(APPEND "${SOURCE_FILE}" "\\n// build changed upstream source\\n")\n'
            'endif()\n', encoding="utf-8")
        (fixture / "CMakeLists.txt").write_text(
            'cmake_minimum_required(VERSION 3.28)\n'
            'project(source_guard NONE)\n'
            f'include("{(REPO / "src/runtime/guard_llama_source.cmake").as_posix()}")\n'
            'add_custom_target(source_consumer\n'
            f'  COMMAND "${{CMAKE_COMMAND}}" "-DSOURCE_FILE={source_file.as_posix()}"\n'
            '    "-DOUTPUT_FILE=${CMAKE_BINARY_DIR}/compiled-source.txt"\n'
            '    "-DMUTATE_SOURCE=${MUTATE_SOURCE}" -P "${CMAKE_CURRENT_SOURCE_DIR}/consume.cmake" VERBATIM)\n'
            'add_custom_target(application ALL)\n'
            f'qcopilots_search_guard_llama_source("{self.source.as_posix()}"\n'
            '  SOURCE_TARGETS source_consumer BUILT_TARGETS source_consumer CONSUMER_TARGETS application)\n',
            encoding="utf-8")
        configure = [OPTIONS.cmake, "-S", str(fixture), "-B", str(build),
                     f"-DPython3_EXECUTABLE={sys.executable}", f"-DGIT_EXECUTABLE={OPTIONS.git}"]
        if OPTIONS.generator:
            configure += ["-G", OPTIONS.generator]
        if OPTIONS.make_program:
            configure += [f"-DCMAKE_MAKE_PROGRAM={OPTIONS.make_program}"]
        environment = {key: value for key, value in self.environment.items()
                       if not key.startswith("CMAKE_GENERATOR")}

        def run(arguments, expected_success):
            result = subprocess.run(arguments, env=environment, capture_output=True, text=True,
                                    encoding="utf-8", errors="replace", timeout=180)
            details = result.stdout + result.stderr
            self.assertEqual(result.returncode == 0, expected_success, details)
            return details

        command = [OPTIONS.cmake, "--build", str(build), "--config", "Release", "--target", "application"]
        run(configure, True)
        run(command, True)
        self.assertEqual(compiled.read_bytes(), original)
        compiled.unlink()
        try:
            source_file.write_bytes(original + b"\n// changed after configure\n")
            self.assertIn("differs from its locked hash", run(command, False))
            self.assertFalse(compiled.exists(), "The source consumer ran before integrity verification")
            source_file.write_bytes(original)
            run(configure + ["-DMUTATE_SOURCE=ON"], True)
            self.assertIn("differs from its locked hash", run(command, False))
            self.assertTrue(compiled.exists(), "The fixture did not reach the source-consuming build")
        finally:
            source_file.write_bytes(original)

    def test_09_reject_directory_link(self):
        link = self.source / "outside-source"
        target = self.root / "outside"
        target.mkdir()
        marker = target / "marker.txt"
        marker.write_bytes(b"Preserve the external link target")
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
            self.check_cmake(failure="source contains a symlink")
        finally:
            if junction:
                link.rmdir()
            else:
                link.unlink()
            self.assertFalse(link.exists())
            self.assertFalse(link.is_symlink())
            self.assertEqual(marker.read_bytes(), b"Preserve the external link target")

    def test_10_reject_wrong_git_commit(self):
        source = self.root / "foreign checkout"
        source.mkdir()
        subprocess.run([OPTIONS.git, "init", "--quiet", str(source)], check=True, env=self.environment)
        subprocess.run([OPTIONS.git, "-C", str(source), "-c", "user.name=Source integrity test",
                        "-c", "user.email=test@example.invalid", "commit", "--quiet", "--allow-empty",
                        "-m", "Unrelated source"], check=True, env=self.environment)
        self.check_cmake(source=source, failure="does not match the locked commit")

    def test_11_matching_git_commit_still_verifies_every_file(self):
        # Use an isolated local commit and lock fixture to exercise the same Git
        # path without a network clone or modifying the reviewed production lock.
        project, verifier = self.copy_verifier("git verifier")
        subprocess.run([OPTIONS.git, "init", "--quiet", str(self.source)], check=True, env=self.environment)
        subprocess.run([OPTIONS.git, "-C", str(self.source), "-c", "core.autocrlf=false",
                        "-c", "core.longpaths=true", "add", "--all"],
                       check=True, env=self.environment, capture_output=True)
        subprocess.run([OPTIONS.git, "-C", str(self.source), "-c", "user.name=Source integrity test",
                        "-c", "core.longpaths=true",
                        "-c", "user.email=test@example.invalid", "commit", "--quiet", "-m", "Pristine archive"],
                       check=True, env=self.environment, capture_output=True)
        commit = subprocess.check_output([OPTIONS.git, "-C", str(self.source), "rev-parse", "HEAD"],
                                         env=self.environment, text=True).strip()
        lock_path = project / "dependencies.lock.json"
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        lock["llama.cpp"]["commit"] = commit
        lock_path.write_text(json.dumps(lock), encoding="utf-8")
        self.check_cmake(verifier=verifier)
        path = self.source / "src/llama-context.cpp"
        original = path.read_bytes()
        try:
            path.write_bytes(original + b"\n// unreviewed source modification\n")
            self.check_cmake(verifier=verifier, failure="differs from its locked hash: src/llama-context.cpp")
        finally:
            path.write_bytes(original)
        extra = self.source / "untracked.cmake"
        try:
            extra.write_text("# untracked build logic\n", encoding="utf-8")
            self.check_cmake(verifier=verifier, failure="source file set differs")
        finally:
            extra.unlink()



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
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--archive", help="Locked llama-v0.7.0.tar.gz")
    inputs.add_argument("--source", help="Pristine upstream directory copied into an isolated test root")
    parser.add_argument("--cmake", default=shutil.which("cmake"))
    parser.add_argument("--git", default=shutil.which("git"))
    parser.add_argument("--generator", help="CMake generator for the build-order regression")
    parser.add_argument("--make-program", help="Generator's build tool, when explicitly configured")
    parser.add_argument("--report", type=Path, help="Optional JSON evidence path outside the source tree")
    OPTIONS = parser.parse_args()
    if not OPTIONS.cmake or not OPTIONS.git:
        parser.error("CMake and Git are required")
    result = unittest.TextTestRunner(verbosity=2, resultclass=RecordedResult).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(SourceIntegrityTests))
    if OPTIONS.report:
        OPTIONS.report.parent.mkdir(parents=True, exist_ok=True)
        OPTIONS.report.write_text(json.dumps({"archive_sha256": ARCHIVE_SHA, "input": "source" if OPTIONS.source else "archive",
            "tests_run": result.testsRun, "successful": result.wasSuccessful(),
            "passed": result.wasSuccessful() and result.testsRun > 0 and not result.skipped,
            "skipped": len(result.skipped), "cases": result.records}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")
    sys.exit(0 if result.wasSuccessful() else 1)
