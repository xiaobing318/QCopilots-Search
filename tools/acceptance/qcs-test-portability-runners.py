#!/usr/bin/env python3
"""Fast contract tests for the opt-in Windows portability acceptance runners."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.dont_write_bytecode = True
DIRECTORY = Path(__file__).resolve().parent


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), DIRECTORY / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


runner = load("qcs-two-checkout-portability")
observer = load("qcs-portability-sentinel")
launcher = load("qcs-launch-portability-sentinel")


class PortabilityContracts(unittest.TestCase):
    def setUp(self):
        # A caller-supplied root must already exist. An invalid root fails here.
        self.temporary = tempfile.TemporaryDirectory(prefix="qcs-pr-", dir=os.environ.get("QCOPILOTS_SEARCH_TEST_ROOT"))
        self.root = Path(self.temporary.name)

    def tearDown(self):
        # TemporaryDirectory.cleanup propagates errors, including retained handles.
        self.temporary.cleanup()

    def wrapper(self, name="qcs-tests-core", *, workflow=False):
        source, build, tests = self.root / "中文 源码", self.root / "独立 build", self.root / "自定 temp"
        (source / "tools").mkdir(parents=True, exist_ok=True)
        (source / "tools/qcs-test-runner.py").write_text("# owned fixture\n", encoding="utf-8")
        inner = ([sys.executable, str(source / "tools/qcs-test-workflow.py")] if workflow
                 else [str(build / "bin" / (name + ".exe"))])
        command = [sys.executable, str(source / "tools/qcs-test-runner.py"), "--label", name,
                   "--root", str(tests), "--", *inner]
        return command, name, source, build, tests

    def test_owned_wrapper_and_binary_are_bound(self):
        values = self.wrapper()
        identity = runner.validate_test_command(*values)
        self.assertEqual(identity["sha256"], runner.sha(values[2] / "tools/qcs-test-runner.py"))

    def test_workflow_uses_its_checkout_and_python(self):
        values = self.wrapper("qcs-tests-workflow", workflow=True)
        runner.validate_test_command(*values)
        values[0][-1] = str(self.root / "unrelated/qcs-test-workflow.py")
        with self.assertRaisesRegex(RuntimeError, "Workflow inventory"):
            runner.validate_test_command(*values)

    def test_preserve_failed_argument_is_supported(self):
        values = self.wrapper()
        values[0].insert(6, "--keep-failed")
        runner.validate_test_command(*values)

    def test_explicit_no_bytecode_python_option_is_supported(self):
        values = self.wrapper("qcs-tests-workflow", workflow=True)
        values[0].insert(1, "-B")
        values[0].insert(1, "-u")
        values[0].insert(-1, "-B")
        runner.validate_test_command(*values)

    def test_qt_stdout_logging_allowed_but_test_filter_rejected(self):
        values = self.wrapper()
        values[0].extend(["-o", "-,txt"])
        runner.validate_test_command(*values)
        values[0].append("singleTestOnly")
        with self.assertRaisesRegex(RuntimeError, "own checkout binary"):
            runner.validate_test_command(*values)

    def test_wrong_wrapper_root_label_and_binary_fail(self):
        for index, replacement in ((0, "unexpected-python"), (1, "unowned-wrapper.py"),
                                   (3, "qcs-tests-other"), (5, str(self.root / "shared-root")),
                                   (6, "--unexpected"), (7, str(self.root / "wrong.exe"))):
            with self.subTest(argument=index):
                values = self.wrapper()
                values[0][index] = replacement
                with self.assertRaises(RuntimeError):
                    runner.validate_test_command(*values)

    def test_unwrapped_or_truncated_commands_fail(self):
        values = self.wrapper()
        for command in ([], values[0][-1:], values[0][:6], values[0][:7]):
            with self.subTest(command=command), self.assertRaises(RuntimeError):
                runner.validate_test_command(command, *values[1:])

    def test_snapshot_detects_source_edits_and_excludes_git(self):
        (self.root / ".git").mkdir()
        (self.root / ".git/internal").write_text("ignored", encoding="utf-8")
        item = self.root / "中文 document.txt"
        item.write_text("before", encoding="utf-8")
        before = runner.snapshot(self.root)
        self.assertEqual(set(before), {item.name})
        item.write_text("after", encoding="utf-8")
        self.assertNotEqual(before, runner.snapshot(self.root))

    def test_sentinel_environment_removes_test_overrides(self):
        environment = {"SYSTEMROOT": str(self.root), "PATH": "unrelated", "QT_QPA_PLATFORM": "offscreen",
                       "QCOPILOTS_SEARCH_TEST_ROOT": "unrelated", "QCOPILOTS_SEARCH_KEEP_TEST_ARTIFACTS": "1",
                       "QML_IMPORT_PATH": "unrelated"}
        with mock.patch.dict(os.environ, environment, clear=True):
            actual, recorded = launcher.environment(self.root / "app", self.root / "sdk", "packaged")
        self.assertFalse(any(key.startswith(("QT_", "QML", "QCOPILOTS_SEARCH_")) for key in actual))
        self.assertIsNone(recorded["QT_QPA_PLATFORM"])
        self.assertNotIn("unrelated", actual["PATH"])

    def test_missing_system_root_fails_instead_of_using_fixed_drive(self):
        for module in (launcher, observer):
            with self.subTest(module=module.__name__), self.assertRaisesRegex(RuntimeError, "missing"):
                module.windows_root({})
            with self.assertRaisesRegex(RuntimeError, "invalid"):
                module.windows_root({"SYSTEMROOT": "relative-system"})

    def test_sentinel_snapshot_excludes_coordination_but_not_database(self):
        sentinel = observer.SentinelObserver.__new__(observer.SentinelObserver)
        sentinel.profile, sentinel.fixture_paths = self.root, []
        (self.root / "qcs-settings.json").write_text("{}", encoding="utf-8")
        index = self.root / "index"
        index.mkdir()
        for name in ("qcs.sqlite3", "qcs.sqlite3-wal", "qcs.sqlite3-shm", "qcs-progress.json", "qcs.lock"):
            (index / name).write_bytes(name.encode())
        snapshot = sentinel.data_snapshot()
        self.assertEqual({Path(path).name for path in snapshot}, {"qcs-settings.json", "qcs.sqlite3", "qcs.sqlite3-wal"})

    def test_help_and_plan_from_arbitrary_working_directory_are_read_only(self):
        before = runner.snapshot(self.root)
        for name in ("qcs-two-checkout-portability", "qcs-launch-portability-sentinel"):
            result = subprocess.run([sys.executable, "-B", str(DIRECTORY / (name + ".py")), "--help"],
                                    cwd=self.root, capture_output=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", errors="replace"))
        command = [sys.executable, "-B", str(DIRECTORY / "qcs-two-checkout-portability.py"),
                   "--head", "a" * 40, "--source-tree", "b" * 40]
        for flag in ("build-root", "build-record", "run-parent", "qt-root", "qt-prefix", "calligra-source"):
            command.extend(["--" + flag, str(self.root / flag)])
        result = subprocess.run(command, cwd=self.root, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", errors="replace"))
        plan = json.loads(result.stdout)
        self.assertFalse(plan["executeRequested"])
        self.assertEqual(Path(plan["source"]), DIRECTORY.parents[1])
        self.assertTrue(Path(plan["uniqueRunRoot"]).name.startswith("qcs-port-"))
        self.assertEqual(before, runner.snapshot(self.root))

    def test_invalid_candidate_identity_fails_before_writing(self):
        argv = [str(DIRECTORY / "qcs-two-checkout-portability.py"), "--head", "abbreviated", "--source-tree", "b" * 40]
        for flag in ("build-root", "build-record", "run-parent", "qt-root", "qt-prefix", "calligra-source"):
            argv.extend(["--" + flag, str(self.root / flag)])
        with mock.patch.object(sys, "argv", argv), self.assertRaisesRegex(RuntimeError, "full lower-case"):
            runner.main()
        self.assertEqual(list(self.root.iterdir()), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
