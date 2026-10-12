#!/usr/bin/env python3
"""Portable isolation and cleanup regression tests for the CTest wrapper."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock


sys.dont_write_bytecode = True
RUNNER = Path(__file__).resolve().with_name("qcs-test-runner.py")
SPEC = importlib.util.spec_from_file_location("qcs_test_runner", RUNNER)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class TestRunner(unittest.TestCase):
    def setUp(self) -> None:
        parent = os.environ.get("QCOPILOTS_SEARCH_TEST_ROOT") or None
        self.context = tempfile.TemporaryDirectory(prefix="qcs-runner-", dir=parent)
        self.base = Path(self.context.name).resolve()
        self.root = self.base / "tests 中文 space"
        self.env = os.environ.copy()
        for name in ("QCOPILOTS_SEARCH_TEST_ROOT", "QCOPILOTS_SEARCH_KEEP_TEST_ARTIFACTS",
                     "QCOPILOTS_SEARCH_KEEP_FAILED_TESTS"):
            self.env.pop(name, None)

    def tearDown(self) -> None:
        # A failed cleanup here fails the case instead of becoming a warning.
        self.context.cleanup()

    def invoke(self, code: str, *, root: Path | None = None,
               env: dict[str, str] | None = None, keep: bool = False,
               label: str = "portable 中文", cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        command = [sys.executable, str(RUNNER), "--label", label,
                   "--root", str(root or self.root)]
        if keep:
            command.append("--keep-failed")
        command.extend(["--", sys.executable, "-c", code])
        return subprocess.run(command, cwd=cwd or self.base, env=env or self.env,
                              encoding="utf-8", errors="replace", capture_output=True,
                              timeout=30, check=False)

    def reports(self, root: Path | None = None) -> list[dict]:
        return [json.loads(item.read_text(encoding="utf-8"))
                for item in sorted(((root or self.root) / "reports").glob("*.json"))]

    def test_success_is_cleaned_and_reported(self) -> None:
        result = self.invoke("from pathlib import Path; Path('output').write_text('test')")
        self.assertEqual(result.returncode, 0, result.stderr)
        report, = self.reports()
        self.assertTrue(report["passed"])
        self.assertEqual(report["childExitCode"], 0)
        self.assertTrue(report["cleanup"]["succeeded"])
        self.assertFalse(report["retained"])
        self.assertFalse(Path(report["runDirectory"]).exists())
        self.assertEqual(Path(report["workingDirectory"]).name, "cwd")

    def test_child_failure_is_propagated_and_cleaned(self) -> None:
        result = self.invoke("raise SystemExit(23)")
        self.assertEqual(result.returncode, 23, result.stderr)
        report, = self.reports()
        self.assertFalse(report["passed"])
        self.assertTrue(report["cleanup"]["succeeded"])

    def test_keep_failed_switch_preserves_artifacts(self) -> None:
        result = self.invoke("from pathlib import Path; Path('evidence').write_text('failure'); "
                             "raise SystemExit(2)", keep=True)
        self.assertEqual(result.returncode, 2, result.stderr)
        report, = self.reports()
        self.assertTrue(report["retained"])
        self.assertFalse(report["cleanup"]["attempted"])
        self.assertEqual((Path(report["workingDirectory"]) / "evidence").read_text(), "failure")

    def test_both_keep_failed_environment_names(self) -> None:
        for name in ("QCOPILOTS_SEARCH_KEEP_TEST_ARTIFACTS", "QCOPILOTS_SEARCH_KEEP_FAILED_TESTS"):
            with self.subTest(name=name):
                root = self.base / name
                result = self.invoke("raise SystemExit(4)", root=root, env={**self.env, name: "1"})
                self.assertEqual(result.returncode, 4, result.stderr)
                report, = self.reports(root)
                self.assertTrue(report["retained"])

    def test_success_is_cleaned_even_when_keep_failed_requested(self) -> None:
        result = self.invoke("pass", keep=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        report, = self.reports()
        self.assertTrue(report["cleanup"]["succeeded"])

    def test_environment_root_takes_precedence(self) -> None:
        override = self.base / "override 中文"
        result = self.invoke("pass", env={**self.env, "QCOPILOTS_SEARCH_TEST_ROOT": str(override)})
        self.assertEqual(result.returncode, 0, result.stderr)
        report, = self.reports(override)
        self.assertEqual(Path(report["root"]), override)
        self.assertFalse(self.root.exists())

    def test_temporary_environment_is_inside_run(self) -> None:
        code = ("import os,tempfile; from pathlib import Path; "
                "root=Path(os.environ['QCOPILOTS_SEARCH_TEST_ROOT']).resolve(); "
                "assert root.parent==Path.cwd().parent; "
                "assert Path(tempfile.gettempdir()).resolve()==root; "
                "assert all(Path(os.environ[n]).resolve()==root for n in ('TMP','TEMP','TMPDIR')); "
                "assert root.parent==Path(os.environ['QCOPILOTS_SEARCH_TEST_RUN_DIR'])")
        result = self.invoke(code)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_concurrent_same_label_has_distinct_cwd_and_test_root(self) -> None:
        code = "import time; from pathlib import Path; Path('owned').write_text('one'); time.sleep(.2)"
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.invoke, code, label="same-label") for _ in range(2)]
            results = [future.result() for future in futures]
        for result in results:
            self.assertEqual(result.returncode, 0, result.stderr)
        reports = self.reports()
        self.assertEqual(len(reports), 2)
        self.assertEqual(len({report["workingDirectory"] for report in reports}), 2)
        self.assertEqual(len({report["testRoot"] for report in reports}), 2)

    def test_arbitrary_working_directory_and_long_label(self) -> None:
        unrelated = self.base / "arbitrary cwd 中文"
        unrelated.mkdir()
        label = "../very long test label " * 20
        result = self.invoke("pass", label=label, cwd=unrelated)
        self.assertEqual(result.returncode, 0, result.stderr)
        report, = self.reports()
        self.assertEqual(report["label"], label)
        self.assertLessEqual(len(report["runId"]), len("qcs-t-") + 16 + 1 + 8)
        self.assertEqual(list(unrelated.iterdir()), [])

    def test_root_creation_failure_does_not_fallback(self) -> None:
        blocked = self.base / "file-not-directory"
        blocked.write_text("preserve")
        result = self.invoke("raise RuntimeError('must not run')", root=blocked / "test")
        self.assertEqual(result.returncode, MODULE.FAILURE, result.stderr)
        self.assertIn('"passed": false', result.stderr)
        self.assertEqual(blocked.read_text(), "preserve")
        self.assertEqual(sorted(path.name for path in self.base.iterdir()), [blocked.name])

    def test_child_start_failure_is_reported(self) -> None:
        result = subprocess.run([sys.executable, str(RUNNER), "--label", "missing",
                                 "--root", str(self.root), "--", str(self.base / "absent-program")],
                                env=self.env, encoding="utf-8", errors="replace", capture_output=True,
                                timeout=30, check=False)
        self.assertEqual(result.returncode, MODULE.FAILURE, result.stderr)
        report, = self.reports()
        self.assertIsNone(report["childExitCode"])
        self.assertTrue(report["cleanup"]["succeeded"])

    def test_cleanup_error_converts_passing_child_to_failure(self) -> None:
        args = argparse.Namespace(root=str(self.root), label="cleanup-fail", keep_failed=False,
                                  command=[sys.executable, "-c", "pass"])
        with mock.patch.dict(os.environ, self.env, clear=True), \
                mock.patch.object(MODULE.shutil, "rmtree", side_effect=PermissionError("held file")), \
                contextlib.redirect_stderr(io.StringIO()):
            result = MODULE.run_test(args)
        self.assertEqual(result, MODULE.FAILURE)
        report, = self.reports()
        self.assertEqual(report["childExitCode"], 0)
        self.assertFalse(report["passed"])
        self.assertTrue(report["retained"])
        self.assertIn("held file", report["cleanup"]["error"])

    def test_replaced_run_directory_is_not_deleted(self) -> None:
        self.root.mkdir()
        root_id = MODULE.identity(self.root)
        run = self.root / "qcs-t-owned"
        run.mkdir()
        run_id = MODULE.identity(run)
        run.rename(self.root / "original")
        run.mkdir()
        (run / "foreign").write_text("preserve")
        with self.assertRaisesRegex(RuntimeError, "identity changed"):
            MODULE.cleanup_run(self.root, root_id, run, run_id)
        self.assertTrue((run / "foreign").exists())

    def test_replaced_root_is_not_deleted_or_written(self) -> None:
        self.root.mkdir()
        root_id = MODULE.identity(self.root)
        run = self.root / "qcs-t-owned"
        run.mkdir()
        run_id = MODULE.identity(run)
        self.root.rename(self.base / "original")
        run.mkdir(parents=True)
        with self.assertRaisesRegex(RuntimeError, "identity changed"):
            MODULE.cleanup_run(self.root, root_id, run, run_id)
        with self.assertRaisesRegex(RuntimeError, "identity changed"):
            MODULE.write_report(self.root, root_id, {"runId": "replacement"})
        self.assertFalse((self.root / "reports").exists())

    def test_cleanup_boundary_rejects_sibling(self) -> None:
        self.root.mkdir()
        foreign = self.base / "qcs-t-foreign"
        foreign.mkdir()
        with self.assertRaisesRegex(RuntimeError, "boundary"):
            MODULE.cleanup_run(self.root, MODULE.identity(self.root), foreign, MODULE.identity(foreign))
        self.assertTrue(foreign.is_dir())

    def test_symbolic_link_cannot_redirect_cleanup(self) -> None:
        self.root.mkdir()
        root_id = MODULE.identity(self.root)
        run = self.root / "qcs-t-owned"
        run.mkdir()
        run_id = MODULE.identity(run)
        foreign = self.base / "foreign"
        foreign.mkdir()
        (foreign / "preserve").write_text("safe")
        link = run / "escape"
        try:
            link.symlink_to(foreign, target_is_directory=True)
        except OSError:
            if os.name != "nt":
                raise
            # Directory junctions cover the native reparse-point case without requiring
            # Windows Developer Mode or an elevated symlink privilege.
            result = subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(foreign)],
                                    capture_output=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
        try:
            with self.assertRaisesRegex(RuntimeError, "link or reparse"):
                MODULE.cleanup_run(self.root, root_id, run, run_id)
            self.assertEqual((foreign / "preserve").read_text(), "safe")
        finally:
            if os.name == "nt" and not link.is_symlink():
                os.rmdir(link)
            else:
                link.unlink()

    def test_report_error_is_a_failure(self) -> None:
        self.root.mkdir()
        (self.root / "reports").write_text("not a directory")
        result = self.invoke("pass")
        self.assertEqual(result.returncode, MODULE.FAILURE, result.stderr)
        self.assertIn('"reportError"', result.stderr)
        self.assertEqual((self.root / "reports").read_text(), "not a directory")
        self.assertEqual(len(list(self.root.iterdir())), 1)

    def test_interruption_stops_real_child_and_cleans_run(self) -> None:
        args = argparse.Namespace(root=str(self.root), label="interruption", keep_failed=False,
                                  command=[sys.executable, "-c", "import time; time.sleep(60)"])
        original_wait = subprocess.Popen.wait
        injected = []

        def interrupted_wait(process: subprocess.Popen, timeout: float | None = None) -> int:
            if process.args == args.command and not injected:
                injected.append(process)
                raise KeyboardInterrupt
            return original_wait(process, timeout=timeout)

        with mock.patch.dict(os.environ, self.env, clear=True), \
                mock.patch.object(subprocess.Popen, "wait", new=interrupted_wait), \
                contextlib.redirect_stderr(io.StringIO()):
            result = MODULE.run_test(args)
        self.assertEqual(result, 128 + signal.SIGINT)
        self.assertIsNotNone(injected[0].poll())
        report, = self.reports()
        self.assertTrue(report["cleanup"]["succeeded"])

    def test_signal_handler_routes_to_interruption(self) -> None:
        # Console control delivery is unavailable on headless Windows runners.
        # Verify registration/routing here and real child teardown separately.
        args = argparse.Namespace(root=str(self.root), label="signal-handler", keep_failed=False,
                                  command=[sys.executable, "-c", "pass"])
        handlers = {}

        def register(number, handler):
            if callable(handler):
                handlers[number] = handler
            return signal.SIG_DFL

        def deliver(*_args, **_kwargs):
            handlers[signal.SIGTERM](signal.SIGTERM, None)

        with mock.patch.dict(os.environ, self.env, clear=True), \
                mock.patch.object(MODULE.signal, "signal", side_effect=register), \
                mock.patch.object(MODULE.subprocess, "Popen", side_effect=deliver), \
                contextlib.redirect_stderr(io.StringIO()):
            result = MODULE.run_test(args)
        self.assertEqual(result, 128 + signal.SIGTERM)
        report, = self.reports()
        self.assertEqual(report["signal"], signal.SIGTERM)
        self.assertTrue(report["cleanup"]["succeeded"])

    def test_posix_signal_delivery_or_windows_group_termination(self) -> None:
        if os.name == "nt":
            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                                     creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
            try:
                MODULE.stop_child(child)
                self.assertIsNotNone(child.poll())
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=5)
            return
        ready = self.base / "ready"
        code = f"from pathlib import Path; import time; Path({str(ready)!r}).touch(); time.sleep(60)"
        child = subprocess.Popen([sys.executable, str(RUNNER), "--label", "signal",
                                  "--root", str(self.root), "--", sys.executable, "-c", code],
                                 env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 encoding="utf-8")
        try:
            deadline = time.monotonic() + 10
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertTrue(ready.exists())
            child.send_signal(signal.SIGTERM)
            _, stderr = child.communicate(timeout=15)
            self.assertEqual(child.returncode, 128 + signal.SIGTERM, stderr)
            report, = self.reports()
            self.assertTrue(report["cleanup"]["succeeded"])
        finally:
            if child.poll() is None:
                child.kill()
                child.communicate()

    def test_utf8_report_with_legacy_redirected_encoding(self) -> None:
        result = self.invoke("pass", env={**self.env, "PYTHONIOENCODING": "cp1252"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("中文 space", result.stderr)
        self.assertNotIn("UnicodeEncodeError", result.stderr)

    def test_selected_root_link_is_rejected_before_writing(self) -> None:
        target = self.base / "foreign"
        target.mkdir()
        link = self.base / "linked-root"
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError:
            if os.name != "nt":
                raise
            result = subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
                                    capture_output=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
        try:
            result = self.invoke("pass", root=link)
            self.assertEqual(result.returncode, MODULE.FAILURE, result.stderr)
            self.assertEqual(list(target.iterdir()), [])
        finally:
            if os.name == "nt" and not link.is_symlink():
                os.rmdir(link)
            else:
                link.unlink()


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    unittest.main(verbosity=2)
