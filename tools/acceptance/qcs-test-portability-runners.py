#!/usr/bin/env python3
"""Fast contract tests for the opt-in Windows portability acceptance runners."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shutil
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
WINDOWS_SKIP_REASON = "Windows native PowerShell and NTFS ACL contract"
WINDOWS_NATIVE_CASES = frozenset(
    "qcs_test_portability_runners.PortabilityContracts." + name for name in (
        "test_windows_powershell_bridge_preserves_developer_arguments",
        "test_windows_acl_restoration_restores_effective_access",
        "test_windows_acl_comparison_rejects_semantic_changes"))


def is_windows_platform_skip(test, reason, platform):
    return platform != "nt" and test.id() in WINDOWS_NATIVE_CASES and reason == WINDOWS_SKIP_REASON


class PortabilityContracts(unittest.TestCase):
    def setUp(self):
        # A caller-supplied root must already exist. An invalid root fails here.
        self.temporary = tempfile.TemporaryDirectory(prefix="qcs-pr-", dir=os.environ.get("QCOPILOTS_SEARCH_TEST_ROOT") or None)
        self.root = Path(self.temporary.name)

    def tearDown(self):
        # TemporaryDirectory.cleanup propagates errors, including retained handles.
        self.temporary.cleanup()

    def powershell(self, script, *arguments):
        executable = shutil.which("pwsh")
        self.assertIsNotNone(executable, "The Windows contract requires PowerShell 7")
        return subprocess.run([executable, "-NoProfile", "-File", str(script), *map(str, arguments)],
                              cwd=self.root, capture_output=True, encoding="utf-8", errors="replace", timeout=45)

    def test_only_exact_non_windows_native_cases_are_not_applicable(self):
        aggregate = load("qcs-test-runners")
        for name in WINDOWS_NATIVE_CASES:
            case = mock.Mock()
            case.id.return_value = name
            for platform, reason, expected in (("posix", WINDOWS_SKIP_REASON, True),
                                               ("nt", WINDOWS_SKIP_REASON, False),
                                               ("posix", "PowerShell missing", False)):
                with self.subTest(case=name, platform=platform, reason=reason):
                    self.assertEqual(is_windows_platform_skip(case, reason, platform), expected)
                    result = mock.Mock(skipped=[(case, reason)])
                    self.assertEqual(aggregate.unexpected_skips(result, platform), [] if expected else result.skipped)
        unknown = mock.Mock()
        unknown.id.return_value = "qcs_test_portability_runners.PortabilityContracts.unknown"
        self.assertFalse(is_windows_platform_skip(unknown, WINDOWS_SKIP_REASON, "posix"))
        result = mock.Mock(skipped=[(unknown, WINDOWS_SKIP_REASON)])
        self.assertEqual(aggregate.unexpected_skips(result, "posix"), result.skipped)

    @unittest.skipUnless(os.name == "nt", WINDOWS_SKIP_REASON)
    def test_windows_powershell_bridge_preserves_developer_arguments(self):
        checkout = self.root / "中文 源码"
        (checkout / "tools").mkdir(parents=True)
        # Execute the actual public entry point's bytes, replacing only its
        # Python consumer with a fixture that records literal argv and exits.
        shutil.copyfile(DIRECTORY.parents[1] / "qcs-dev.ps1", checkout / "qcs-dev.ps1")
        (checkout / "tools/qcs-dev.py").write_text(
            "import json, os, sys\nfrom pathlib import Path\n"
            "Path(os.environ['QCS_ARGV_RESULT']).write_text(json.dumps(sys.argv[1:], ensure_ascii=False), encoding='utf-8')\n"
            "sys.exit(int(os.environ.get('QCS_ARGV_EXIT', '0')))\n", encoding="utf-8")
        bridge = self.root / "bridge.ps1"
        bridge.write_text(runner.POWERSHELL_BRIDGE_SCRIPT, encoding="utf-8")
        result_path = self.root / "argv.json"
        arguments = ["configure", "--build-root", str(self.root / "外置 构建"),
                     "--cmake-arg=-DROOT=" + str(self.root / "中文 空格"),
                     "--cmake-arg=-DROOT:PATH=" + str(self.root / "中文 空格"),
                     "--cmake-arg=-DURL=https://example.test/a=b=c", "--cmake-arg=-DEMPTY=", "",
                     "literal $([IO.File]::WriteAllText('injected.txt','bad')) ` \" quotes", "tail\\"]
        executable = shutil.which("pwsh")
        self.assertIsNotNone(executable, "The Windows contract requires PowerShell 7")
        command = runner.powershell_command(executable, bridge, self.root / "request.json",
                                            checkout / "qcs-dev.ps1", arguments)
        environment = dict(os.environ, QCOPILOTS_SEARCH_PYTHON=sys.executable,
                           QCS_ARGV_RESULT=str(result_path), PYTHONDONTWRITEBYTECODE="1")
        for code in (0, 23):
            with self.subTest(exitCode=code):
                result = subprocess.run(list(map(str, command)), cwd=self.root,
                                        env=dict(environment, QCS_ARGV_EXIT=str(code)), capture_output=True,
                                        encoding="utf-8", errors="replace", timeout=45)
                self.assertEqual(result.returncode, code, result.stdout + result.stderr)
                self.assertEqual(json.loads(result_path.read_text(encoding="utf-8")), arguments)
                self.assertFalse((self.root / "injected.txt").exists())
        # Also pass the original failure shape through the real Python parser
        # and CMake. A language-free project needs no product/dependency build.
        for filename in ("qcs-dev.py", "qcs-path-budget.py"):
            shutil.copyfile(DIRECTORY.parent / filename, checkout / "tools" / filename)
        (checkout / "CMakeLists.txt").write_text(
            "cmake_minimum_required(VERSION 3.28)\nproject(qcs-argument-fixture NONE)\n", encoding="utf-8")
        build, supplied = self.root / "外置 构建", self.root / "中文 values"
        actual_arguments = ["configure", "--build-root", str(build), "--qt-root", str(self.root / "SDK 空格"),
                            "--cmake-arg=-DQCS_TYPED:PATH=" + str(supplied),
                            "--cmake-arg=-DQCS_RAW:STRING=" + str(supplied),
                            "--cmake-arg=-DQCS_URL:STRING=https://example.test/a=b=c",
                            "--cmake-arg=-DQCS_EMPTY:STRING="]
        command = runner.powershell_command(executable, bridge, self.root / "configure.json",
                                            checkout / "qcs-dev.ps1", actual_arguments)
        result = subprocess.run(list(map(str, command)), cwd=self.root, env=environment, capture_output=True,
                                encoding="utf-8", errors="replace", timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        cache = dict(line.split("=", 1) for line in (build / "release/CMakeCache.txt").read_text(encoding="utf-8").splitlines()
                     if line.startswith("QCS_"))
        self.assertEqual(Path(cache["QCS_TYPED:PATH"]), supplied)
        self.assertEqual(cache["QCS_RAW:STRING"], str(supplied))
        self.assertEqual(cache["QCS_URL:STRING"], "https://example.test/a=b=c")
        self.assertEqual(cache["QCS_EMPTY:STRING"], "")

    @unittest.skipUnless(os.name == "nt", WINDOWS_SKIP_REASON)
    def test_windows_acl_comparison_rejects_semantic_changes(self):
        base = "O:SYG:BAD:(A;OICIID;FA;;;SY)(A;OICIID;FA;;;BA)"
        cases = [(base, base, True), (base, base.replace("D:", "D:AI"), True),
                 (base.replace("D:", "D:AI"), base, False)]
        for old, new in (("O:SY", "O:BA"), ("G:BA", "G:SY"), ("D:", "D:P"), ("D:", "D:AR"),
                         ("OICIID", "CIID"), ("OICIID", "OIID"), ("OICIID", "OICI"),
                         ("OICIID", "OICIIOID"), ("OICIID", "OICINPID"), (";FA;", ";FR;"),
                         ("(A;OICIID;FA;;;BA)", ""), ("D:", "D:(D;;FW;;;WD)")):
            cases.append((base, base.replace(old, new, 1), False))
        cases.append((base, "O:SYG:BAD:(A;OICIID;FA;;;BA)(A;OICIID;FA;;;SY)", False))
        cases.append((base, base + "(A;;FR;;;WD)", False))
        cases.extend([("O:SYG:BAD:", "O:SYG:BAD:NO_ACCESS_CONTROL", False),
                      (base + "S:(AU;SA;FR;;;WD)", base + "S:AI(AU;SA;FR;;;WD)", False)])
        runner.write(self.root / "cases.json", cases)
        script = self.root / "compare.ps1"
        script.write_text(runner.ACL_COMPARE_SCRIPT + "\n$rows = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'cases.json') -Raw | ConvertFrom-Json\n"
                          "$results = @($rows | ForEach-Object { Compare-QcsAclSddl $_[0] $_[1] })\n"
                          "ConvertTo-Json -InputObject $results -Depth 5 -Compress\n", encoding="utf-8")
        result = self.powershell(script)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        reports = json.loads(result.stdout)
        self.assertEqual(len(reports), len(cases))
        for case, report in zip(cases, reports):
            with self.subTest(original=case[0], actual=case[1]):
                self.assertEqual(report["semanticMatch"], case[2])
                self.assertEqual(report["originalSddl"], case[0])
                self.assertEqual(report["actualSddl"], case[1])
        self.assertTrue(reports[1]["daclAutoInheritedAdded"])
        self.assertFalse(reports[1]["sddlExactlyEqual"])

    @unittest.skipUnless(os.name == "nt", WINDOWS_SKIP_REASON)
    def test_windows_acl_restoration_restores_effective_access(self):
        source, reports = self.root / "中文 源码", self.root / "reports"
        (source / "src/core").mkdir(parents=True)
        (source / ".git").mkdir()
        reports.mkdir()
        (source / "CMakeLists.txt").write_bytes(b"owned source bytes\n")
        for operation in ("delete", "rename"):
            (source / ".git" / ("qcs-acl-" + operation + ".tmp")).write_bytes(b"owned disposable\n")
        before = runner.snapshot(source)
        runner.write(self.root / ".qcs-owned-run.json", {"source": str(source)})
        script, original = reports / "qcs-owned-acl.ps1", reports / "source-original-acl.json"
        script.write_text(runner.ACL_SCRIPT, encoding="utf-8")
        try:
            denied = self.powershell(script, "-Source", source, "-Report", original, "-Mode", "deny")
            self.assertEqual(denied.returncode, 0, denied.stdout + denied.stderr)
            self.assertTrue(all(item["denied"] for item in runner.write_probes(source)))
        finally:
            if original.is_file():
                restored = self.powershell(script, "-Source", source, "-Report", original, "-Mode", "restore")
                self.assertEqual(restored.returncode, 0, restored.stdout + restored.stderr)
                report = json.loads(restored.stdout)
                self.assertTrue(report["semanticMatch"])
                self.assertEqual(len(runner.restored_write_probes(source)), 6)
        self.assertEqual(before, runner.snapshot(source))

    def wrapper(self, name="qcs-tests-core", *, workflow=False):
        source, build, tests = self.root / "中文 源码", self.root / "独立 build", self.root / "自定 temp"
        (source / "tools").mkdir(parents=True, exist_ok=True)
        (source / "tools/qcs-test-runner.py").write_text("# owned fixture\n", encoding="utf-8")
        inner = ([sys.executable, str(source / "tools/qcs-test-workflow.py")] if workflow
                 else [str(build / "bin" / (name + ".exe"))])
        command = [sys.executable, str(source / "tools/qcs-test-runner.py"), "--label", name,
                   "--root", str(tests), "--", *inner]
        return command, name, source, build, tests

    def native_build_fixture(self):
        clone = self.root / "克隆 构建/release"
        main = self.root / "主构建"
        qt, nsis, global_dlls = (self.root / name for name in ("Qt SDK", "私有 NSIS", "全局 DLL"))
        for directory in (clone / "bin", clone / "nd/libgit2", main / "release/bin",
                          main / "release/lib", qt / "bin", qt / "plugins", nsis, global_dlls):
            directory.mkdir(parents=True, exist_ok=True)
        for name in runner.TARGETS:
            (clone / "bin" / (name + ".exe")).write_bytes(("independent " + name).encode())
        native = clone / "nd/libgit2/git2.dll"
        native.write_bytes(b"independently built native DLL")
        for directory in (main / "release/bin", global_dlls):
            (directory / "git2.dll").write_bytes(("decoy " + str(directory)).encode("utf-8"))
        (nsis / "makensis.exe").write_bytes(b"locked NSIS lookup fixture, never executed")
        return clone, native, main, qt, nsis, global_dlls

    def test_clone_snapshot_records_exact_native_dll_and_independent_executables(self):
        clone, native, *_ = self.native_build_fixture()
        selected, actual = runner.clone_executable_snapshot(clone)
        self.assertEqual(selected, native)
        expected = [clone / "bin" / (name + ".exe") for name in runner.TARGETS] + [native]
        self.assertEqual(set(actual), {str(path.resolve()) for path in expected})
        for path in expected:
            with self.subTest(file=path.name):
                self.assertEqual(actual[str(path.resolve())],
                                 {"bytes": path.stat().st_size, "sha256": runner.sha(path)})
        native.write_bytes(b"different native DLL bytes")
        _, changed = runner.clone_executable_snapshot(clone)
        self.assertNotEqual(actual[str(native.resolve())], changed[str(native.resolve())])

    def test_clone_snapshot_missing_native_dll_rejects_bin_main_and_path_decoys(self):
        clone, native, main, _, _, global_dlls = self.native_build_fixture()
        native.unlink()
        with mock.patch.dict(os.environ, {"PATH": os.pathsep.join(map(str, (
                clone / "bin", main / "release/bin", global_dlls)))}):
            for with_bin in (False, True):
                with self.subTest(binDecoy=with_bin):
                    if with_bin:
                        (clone / "bin/git2.dll").write_bytes(b"deployed decoy cannot replace the missing native DLL")
                    before = runner.snapshot(self.root)
                    with self.assertRaisesRegex(RuntimeError, "git2.dll"):
                        runner.clone_executable_snapshot(clone)
                    self.assertFalse(native.exists())
                    self.assertEqual(before, runner.snapshot(self.root))

    def test_clone_snapshot_rejects_bin_dll_that_shadows_native(self):
        clone, native, *_ = self.native_build_fixture()
        deployed = clone / "bin/git2.dll"
        for content in (b"different DLL that Windows would load first", native.read_bytes()):
            with self.subTest(identical=content == native.read_bytes()):
                deployed.write_bytes(content)
                before = runner.snapshot(self.root)
                with self.assertRaisesRegex(RuntimeError, "bin/git2.dll.*override"):
                    runner.clone_executable_snapshot(clone)
                self.assertEqual(before, runner.snapshot(self.root))
        deployed.unlink()
        selected, _ = runner.clone_executable_snapshot(clone)
        self.assertEqual(selected, native)

    def test_clone_snapshot_requires_every_independently_built_executable(self):
        clone, _, *_ = self.native_build_fixture()
        for name in runner.TARGETS:
            path = clone / "bin" / (name + ".exe")
            content = path.read_bytes()
            path.unlink()
            try:
                with self.subTest(target=name), self.assertRaisesRegex(RuntimeError, "Missing independently built"):
                    runner.clone_executable_snapshot(clone)
            finally:
                path.write_bytes(content)

    def test_clone_command_environment_uses_selected_native_dll_directory(self):
        clone, native, main, qt, nsis, global_dlls = self.native_build_fixture()
        selected, _ = runner.clone_executable_snapshot(clone)
        base = {"PATH": os.pathsep.join(map(str, (main / "release/bin", main / "release/lib", global_dlls))),
                "UNCHANGED_VALUE": "preserve"}
        before = dict(base)
        # Only the unrelated Windows NSIS command lookup is stubbed. DLL
        # selection, native/decoy files, hashes and PATH construction are real.
        with mock.patch.object(runner.shutil, "which", return_value=str(nsis / "makensis.exe")) as lookup:
            actual = runner.command_env(base, clone, selected, main_build=main, qt_prefix=qt, nsis_root=nsis)
        paths = actual["PATH"].split(os.pathsep)
        self.assertIn(str(native.parent), paths)
        self.assertLess(paths.index(str(native.parent)), paths.index(str(global_dlls)))
        self.assertNotIn(str(main / "release/bin"), paths)
        self.assertNotIn(str(main / "release/lib"), paths)
        self.assertIn(str(clone / "bin"), paths)
        self.assertEqual(actual["QT_PLUGIN_PATH"], str(qt / "plugins"))
        self.assertEqual(actual["NSISDIR"], str(nsis))
        self.assertEqual(actual["QCOPILOTS_SEARCH_PYTHON"], sys.executable)
        self.assertEqual(actual["UNCHANGED_VALUE"], "preserve")
        self.assertEqual(base, before)
        lookup.assert_called_once_with("makensis", path=actual["PATH"])

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
