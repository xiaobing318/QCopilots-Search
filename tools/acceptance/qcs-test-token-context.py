"""Owned child token isolation, with real Windows process and descendant checks."""
from __future__ import annotations

import copy
import contextlib
import ctypes
from ctypes import wintypes
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

sys.dont_write_bytecode = True
DIRECTORY = Path(__file__).resolve().parent
WINDOWS_SKIP_REASON = "Windows native process token and Job Object contract"
WINDOWS_NATIVE_CASES = frozenset({
    "qcs_test_token_context.TokenContextContracts.test_windows_owned_child_preserves_parent_and_contains_descendants"})
CAPABILITIES = {"nativeProcessToken": "Windows only", "changesParentToken": False,
                "enablesPrivileges": False, "nativeMissingPrivileges": "Observe absence without skipping"}


def is_windows_platform_skip(test, reason, platform):
    return platform != "nt" and test.id() in WINDOWS_NATIVE_CASES and reason == WINDOWS_SKIP_REASON


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), DIRECTORY / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


context = load("qcs-token-context")
REMOVED = {"SeBackupPrivilege", "SeRestorePrivilege"}
NATIVE_RECORDS = []

NATIVE_FIXTURE = r'''
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time

mode, root_text, helper_text = sys.argv[1:]
root = Path(root_text)
spec = importlib.util.spec_from_file_location("qcs_native_child_context", helper_text)
context = importlib.util.module_from_spec(spec)
spec.loader.exec_module(context)
api = context.WindowsTokenContext()
api.ensure_no_thread_token()
handle = api.open_process_token(api.current_process())
try:
    snapshot = api.snapshot(handle)
finally:
    api.close(handle)
if context.has_backup_restore(snapshot):
    raise RuntimeError("The child inherited backup or restore privileges")
if context.reexec_if_needed(Path(__file__), sys.argv[1:]) is not None:
    raise RuntimeError("An already restricted descendant restarted unexpectedly")
print("QCS_TOKEN_UTF8 中文输出 " + mode, flush=True)
(root / (mode + ".json")).write_text(json.dumps({"pid": os.getpid(), "token": snapshot,
    "utf8Mode": sys.flags.utf8_mode, "stdoutEncoding": sys.stdout.encoding}), encoding="utf-8")
if mode == "normal":
    subprocess.run([sys.executable, "-X", "utf8", "-B", __file__, "grand-normal", str(root), helper_text], check=True)
elif mode == "nonzero":
    raise SystemExit(23)
elif mode == "crash":
    os._exit(37)
elif mode == "descendant":
    child = subprocess.Popen([sys.executable, "-X", "utf8", "-B", __file__, "grand-sleep", str(root), helper_text],
                             creationflags=0x08000000)
    until = time.monotonic() + 10
    while not (root / "grand-sleep.json").is_file() and child.poll() is None and time.monotonic() < until:
        time.sleep(.01)
    if not (root / "grand-sleep.json").is_file():
        raise RuntimeError("Owned grandchild did not report its actual token")
elif mode == "grand-sleep":
    time.sleep(60)
'''


def token(identity, privileges=("SeBackupPrivilege", "SeRestorePrivilege", "SeChangeNotifyPrivilege")):
    return {"tokenId": {"low": identity, "high": 0}, "modifiedId": {"low": identity + 10, "high": 0},
            "tokenType": "primary", "user": {"sid": "S-1-5-21-10-20-30-1001", "attributes": 0},
            "owner": "S-1-5-21-10-20-30-1001", "primaryGroup": "S-1-5-21-10-20-30-513",
            "groups": [{"sid": "S-1-5-32-545", "attributes": 7}], "restrictedSids": [],
            "privileges": [{"name": name, "luid": "00000000:" + format(index + 20, "08x"),
                            "attributes": 0 if name == "SeBackupPrivilege" else 2}
                           for index, name in enumerate(privileges)],
            "defaultDacl": "01020304", "integrity": {"sid": "S-1-16-8192", "attributes": 96},
            "sessionId": 1, "elevationType": 3, "elevated": 0, "mandatoryPolicy": 1}


class FakeBoundary:
    """Keep parent, child token, process and job identities deliberately distinct."""
    def __init__(self, *, failure=None, exit_code=0, descendants=0):
        self.events = []
        self.failure = failure
        self.exit_code = exit_code
        self.descendants = descendants
        self.parent = token(1)
        self.child_before = token(2)
        self.child_after = copy.deepcopy(self.child_before)
        self.removed = False
        self.parent_changed = False
        self.process = FakeProcess(self)
        self.job_active = 0
        self.argv = self.kwargs = None
        self.closed_tokens = []

    def event(self, name):
        self.events.append(name)
        if self.failure == name:
            raise RuntimeError("injected " + name)

    def current_process(self):
        return 101

    def ensure_no_thread_token(self):
        self.event("check-thread")

    def open_process_token(self, process, adjust=False):
        name = "parent" if int(process) == 101 else "child"
        self.event("open-" + name)
        if name == "parent" and adjust:
            raise AssertionError("Parent token must never be opened for adjustment")
        return name + "-token"

    def snapshot(self, handle):
        name = "parent" if handle == "parent-token" else "child"
        self.event("snapshot-" + name + ("-after" if self.removed else "-before"))
        if name == "parent":
            value = copy.deepcopy(self.parent)
            if self.parent_changed and self.removed:
                value["owner"] = "unexpected-parent-owner"
            return value
        return copy.deepcopy(self.child_after if self.removed else self.child_before)

    def remove_backup_restore(self, handle):
        if handle != "child-token":
            raise AssertionError("Only the owned child's token may be changed")
        self.event("remove")
        self.removed = True
        names = [item["name"] for item in self.child_before["privileges"] if item["name"] in REMOVED]
        self.child_after["privileges"] = [item for item in self.child_after["privileges"] if item["name"] not in REMOVED]
        if names:
            self.child_after["modifiedId"] = {"low": 99, "high": 0}
        return names

    def close(self, handle):
        self.event("close-" + str(handle))
        self.closed_tokens.append(handle)

    def popen(self, argv, **kwargs):
        self.event("popen")
        self.argv, self.kwargs = argv, kwargs
        return self.process

    def job(self, process):
        if process is not self.process:
            raise AssertionError("Job must contain only this owned process")
        self.event("job")
        self.job_active = 1
        return "owned-job"

    def resume(self, process):
        if process is not self.process:
            raise AssertionError("Only this owned child may resume")
        self.event("resume")

    def active(self, job):
        if job != "owned-job":
            raise AssertionError("Only the owned job may be observed")
        self.event("active")
        return self.job_active

    def stop(self, job):
        if job != "owned-job":
            raise AssertionError("Only the owned job may be stopped")
        self.event("stop")
        self.job_active = 0
        if self.process.returncode is None:
            self.process.returncode = 1
        return 0


class FakeProcess:
    def __init__(self, boundary):
        self.boundary = boundary
        self._handle = 202
        self.pid = 987654
        self.returncode = None

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.boundary.event("wait")
        if self.returncode is None:
            self.returncode = self.boundary.exit_code
            self.boundary.job_active = self.boundary.descendants
        return self.returncode

    def terminate(self):
        self.boundary.event("terminate")
        self.returncode = 1


class TokenContextContracts(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(
            prefix="qcs-tk-", dir=os.environ.get("QCOPILOTS_SEARCH_TEST_ROOT") or None)
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()
        self.assertFalse(self.root.exists(), "Token fixture cleanup failed")

    def run_fake(self, boundary, *, reexec=False, report=None):
        function = context.reexec_if_needed if reexec else context.run_owned_child
        kwargs = {"token_api": boundary, "processes": boundary, "popen": boundary.popen,
                  "report": {} if report is None else report}
        if reexec:
            kwargs["platform"] = "nt"
        # The real entry prints after its child suite finishes. This unit calls
        # it inside a case, so capture and parse that output separately.
        output = io.StringIO()
        try:
            with contextlib.redirect_stderr(output):
                return function(self.root / "入口 中文.py", ["space argument", "中文"], **kwargs)
        finally:
            self.last_boundary_output = output.getvalue()

    def test_only_exact_non_windows_native_case_is_not_applicable(self):
        exact = mock.Mock()
        exact.id.return_value = next(iter(WINDOWS_NATIVE_CASES))
        for platform in ("nt", "posix"):
            for reason in (WINDOWS_SKIP_REASON, "missing Python", ""):
                with self.subTest(platform=platform, reason=reason):
                    self.assertEqual(is_windows_platform_skip(exact, reason, platform),
                                     platform == "posix" and reason == WINDOWS_SKIP_REASON)
        unknown = mock.Mock()
        unknown.id.return_value = "qcs_test_token_context.TokenContextContracts.unknown"
        self.assertFalse(is_windows_platform_skip(unknown, WINDOWS_SKIP_REASON, "posix"))

    def test_non_windows_entry_does_not_open_native_handles(self):
        boundary = FakeBoundary()
        self.assertIsNone(context.reexec_if_needed(self.root / "entry.py", [],
                         token_api=boundary, processes=boundary, popen=boundary.popen, platform="posix"))
        self.assertEqual(boundary.events, [])

    def test_absent_privileges_are_noop_after_thread_check_even_with_spoofed_environment(self):
        boundary = FakeBoundary()
        boundary.parent["privileges"] = [row for row in boundary.parent["privileges"] if row["name"] not in REMOVED]
        with mock.patch.dict(os.environ, {"QCOPILOTS_SEARCH_TOKEN_CONTEXT": "restricted", "QCS_TOKEN_REEXEC": "1"}):
            self.assertIsNone(self.run_fake(boundary, reexec=True))
        self.assertEqual(boundary.events[0], "check-thread")
        self.assertNotIn("popen", boundary.events)
        self.assertNotIn("remove", boundary.events)
        self.assertIn("parent-token", boundary.closed_tokens)

    def test_disabled_privilege_still_requires_actual_removal(self):
        boundary = FakeBoundary()
        for row in boundary.parent["privileges"] + boundary.child_before["privileges"] + boundary.child_after["privileges"]:
            row["attributes"] = 0
        with mock.patch.dict(os.environ, {"QCOPILOTS_SEARCH_TOKEN_CONTEXT": "restricted", "QCS_TOKEN_REEXEC": "1"}):
            self.assertEqual(self.run_fake(boundary, reexec=True), 0)
        self.assertIn("remove", boundary.events)
        self.assertFalse(REMOVED.intersection(row["name"] for row in boundary.child_after["privileges"]))
        lines = self.last_boundary_output.splitlines()
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("QCS_TOKEN_CONTEXT "))
        self.assertTrue(json.loads(lines[0].removeprefix("QCS_TOKEN_CONTEXT "))["passed"])

    def test_suspended_child_is_verified_and_assigned_before_single_resume(self):
        boundary = FakeBoundary(exit_code=23)
        parent = copy.deepcopy(boundary.parent)
        self.assertEqual(self.run_fake(boundary), 23)
        self.assertTrue(boundary.kwargs["creationflags"] & 4)
        self.assertEqual(boundary.argv[1:4], ["-X", "utf8", "-B"])
        self.assertEqual(boundary.argv[-2:], ["space argument", "中文"])
        self.assertEqual(boundary.events.count("resume"), 1)
        for prior in ("job", "remove", "snapshot-child-after", "snapshot-parent-after"):
            self.assertLess(boundary.events.index(prior), boundary.events.index("resume"))
        self.assertEqual(boundary.parent, parent)
        self.assertEqual(boundary.closed_tokens.count("parent-token"), 1)
        self.assertEqual(boundary.closed_tokens.count("child-token"), 1)
        self.assertEqual(boundary.closed_tokens.count("owned-job"), 1)

    def test_same_token_identity_is_rejected_before_adjustment(self):
        boundary = FakeBoundary()
        boundary.child_before["tokenId"] = copy.deepcopy(boundary.parent["tokenId"])
        with self.assertRaises(RuntimeError):
            self.run_fake(boundary)
        self.assertNotIn("remove", boundary.events)
        self.assertNotIn("resume", boundary.events)
        self.assertIsNotNone(boundary.process.returncode)

    def test_preparation_failures_never_resume_and_reap_only_owned_child(self):
        for failure in ("check-thread", "open-parent", "snapshot-parent-before", "popen", "job",
                        "open-child", "snapshot-child-before", "remove", "snapshot-child-after",
                        "snapshot-parent-after"):
            with self.subTest(failure=failure):
                boundary = FakeBoundary(failure=failure)
                with self.assertRaisesRegex(RuntimeError, "injected " + failure):
                    self.run_fake(boundary)
                self.assertNotIn("resume", boundary.events)
                if "popen" in boundary.events and failure != "popen":
                    self.assertIsNotNone(boundary.process.returncode)
                else:
                    self.assertNotIn("terminate", boundary.events)
                if "open-child" in boundary.events and failure != "open-child":
                    self.assertEqual(boundary.closed_tokens.count("child-token"), 1)

    def test_child_identity_and_non_target_access_changes_fail_closed(self):
        fields = {"user": {"sid": "S-1-5-18", "attributes": 0}, "owner": "unexpected-owner",
                  "defaultDacl": "changed", "groups": [], "integrity": {"sid": "S-1-16-12288", "attributes": 96},
                  "tokenType": "impersonation", "tokenId": {"low": 55, "high": 0}}
        for field, value in fields.items():
            with self.subTest(field=field):
                boundary = FakeBoundary()
                boundary.child_after[field] = value
                with self.assertRaises(RuntimeError):
                    self.run_fake(boundary)
                self.assertNotIn("resume", boundary.events)

    def test_parent_token_change_prevents_resume(self):
        boundary = FakeBoundary()
        boundary.parent_changed = True
        with self.assertRaises(RuntimeError):
            self.run_fake(boundary)
        self.assertNotIn("resume", boundary.events)

    def test_validation_requires_absence_not_disabled_attributes(self):
        parent, before = token(1), token(2)
        after = copy.deepcopy(before)
        for row in after["privileges"]:
            row["attributes"] = 0
        with self.assertRaises(RuntimeError):
            context.validate_child_change(before, after, parent)

    def test_validation_rejects_removing_unrelated_privilege(self):
        parent, before = token(1), token(2)
        after = copy.deepcopy(before)
        after["privileges"] = []
        with self.assertRaises(RuntimeError):
            context.validate_child_change(before, after, parent)

    def test_validation_allows_only_requested_removal_and_modified_identity(self):
        parent, before = token(1), token(2)
        after = copy.deepcopy(before)
        after["privileges"] = [row for row in after["privileges"] if row["name"] not in REMOVED]
        after["modifiedId"] = {"low": 777, "high": 0}
        context.validate_child_change(before, after, parent)

    def test_validation_accepts_reordered_remaining_privileges_without_relaxing_attributes(self):
        parent = token(1)
        before = token(2, ("SeBackupPrivilege", "SeRestorePrivilege", "SeChangeNotifyPrivilege", "SeImpersonatePrivilege"))
        after = copy.deepcopy(before)
        after["privileges"] = list(reversed([row for row in after["privileges"] if row["name"] not in REMOVED]))
        context.validate_child_change(before, after, parent)
        after["privileges"][0]["attributes"] ^= 2
        with self.assertRaises(RuntimeError):
            context.validate_child_change(before, after, parent)

    def test_validation_rejects_duplicate_privilege_identity(self):
        parent, before = token(1), token(2)
        after = copy.deepcopy(before)
        after["privileges"] = [row for row in after["privileges"] if row["name"] not in REMOVED]
        for side in ("before", "after"):
            with self.subTest(side=side):
                first, second = copy.deepcopy(before), copy.deepcopy(after)
                target = first if side == "before" else second
                target["privileges"].append(copy.deepcopy(target["privileges"][-1]))
                with self.assertRaises(RuntimeError):
                    context.validate_child_change(first, second, parent)

    def test_opened_thread_owner_is_verified_before_resume_in_both_job_implementations(self):
        class Thread(ctypes.Structure):
            _fields_ = [("size", wintypes.DWORD), ("tid", wintypes.DWORD), ("owner", wintypes.DWORD)]

        for module_name in ("qcs_owned_processes", "qcs-two-checkout-portability"):
            for actual_owner in (0, 456, 123):
                with self.subTest(module=module_name, actualOwner=actual_owner):
                    module = load(module_name)
                    jobs = module.WindowsProcesses.__new__(module.WindowsProcesses)
                    jobs.Thread = Thread
                    def first(snapshot, pointer):
                        row = ctypes.cast(pointer, ctypes.POINTER(Thread)).contents
                        row.tid, row.owner = 789, 123
                        return True
                    jobs.k = SimpleNamespace(
                        CreateToolhelp32Snapshot=mock.Mock(return_value=333),
                        Thread32First=mock.Mock(side_effect=first), Thread32Next=mock.Mock(return_value=False),
                        CloseHandle=mock.Mock(return_value=True), OpenThread=mock.Mock(return_value=444),
                        GetProcessIdOfThread=mock.Mock(return_value=actual_owner), ResumeThread=mock.Mock(return_value=1))
                    if actual_owner == 123:
                        jobs.resume(SimpleNamespace(pid=123))
                        jobs.k.ResumeThread.assert_called_once_with(444)
                    else:
                        with self.assertRaises(RuntimeError):
                            jobs.resume(SimpleNamespace(pid=123))
                        jobs.k.ResumeThread.assert_not_called()
                    jobs.k.OpenThread.assert_called_once_with(0x0802, False, 789)
                    self.assertEqual(jobs.k.CloseHandle.call_args_list, [mock.call(333), mock.call(444)])

    def test_live_descendants_are_stopped_before_job_close(self):
        boundary = FakeBoundary(descendants=1)
        with self.assertRaisesRegex(RuntimeError, "descendant"):
            self.run_fake(boundary)
        self.assertIn("stop", boundary.events)
        self.assertLess(boundary.events.index("stop"), boundary.events.index("close-owned-job"))
        self.assertEqual(boundary.job_active, 0)

    def test_resume_or_wait_failure_still_stops_and_reaps_owned_process(self):
        for failure in ("resume", "wait"):
            with self.subTest(failure=failure):
                boundary = FakeBoundary(failure=failure)
                with self.assertRaisesRegex(RuntimeError, "injected " + failure):
                    self.run_fake(boundary)
                self.assertIn("stop", boundary.events)
                self.assertIn("close-owned-job", boundary.events)

    def test_cleanup_failure_never_returns_a_successful_exit(self):
        for failure in ("stop", "close-owned-job", "close-child-token", "close-parent-token"):
            with self.subTest(failure=failure):
                boundary = FakeBoundary(failure=failure, descendants=1)
                with self.assertRaises(Exception):
                    self.run_fake(boundary)

    def test_primary_failure_is_preserved_when_cleanup_also_fails(self):
        boundary = FakeBoundary(failure="remove")
        def failing_stop(job):
            boundary.events.append("stop")
            raise OSError("secondary stop failure")
        boundary.stop = failing_stop
        report = {}
        with self.assertRaisesRegex(RuntimeError, "injected remove") as caught:
            self.run_fake(boundary, report=report)
        self.assertIn("injected remove", report["error"])
        self.assertTrue(any("secondary stop failure" in value for value in report["cleanupErrors"]))
        self.assertFalse(report["passed"])
        self.assertTrue(any("secondary stop failure" in note for note in getattr(caught.exception, "__notes__", [])))
        self.assertNotIn("resume", boundary.events)

    def test_native_adjustment_requests_removal_including_disabled_privileges(self):
        api = context.WindowsTokenContext.__new__(context.WindowsTokenContext)
        rows = []
        for index, name in enumerate(("SeBackupPrivilege", "SeRestorePrivilege", "SeChangeNotifyPrivilege")):
            item = context.Privilege()
            item.luid.low, item.attributes = index + 20, 0 if index == 0 else 2
            rows.append((name, item))
        api.privilege_rows = mock.Mock(return_value=(rows, object()))
        calls = []
        def adjust(handle, all_privileges, pointer, size, previous, length):
            address = ctypes.cast(pointer, ctypes.c_void_p).value
            count = wintypes.DWORD.from_address(address).value
            values = [context.Privilege.from_address(address + ctypes.sizeof(wintypes.DWORD)
                       + index * ctypes.sizeof(context.Privilege)) for index in range(count)]
            calls.append((handle, all_privileges, [(item.luid.low, item.attributes) for item in values]))
            return 1
        api.security = mock.Mock(AdjustTokenPrivileges=adjust)
        with mock.patch.object(ctypes, "set_last_error", create=True), mock.patch.object(ctypes, "get_last_error", return_value=0, create=True):
            self.assertEqual(api.remove_backup_restore(222), ["SeBackupPrivilege", "SeRestorePrivilege"])
        self.assertEqual(calls, [(222, False, [(20, 4), (21, 4)])])

    def test_native_adjustment_failure_and_not_all_assigned_are_both_failures(self):
        for succeeded, last_error in ((False, 5), (True, 1300)):
            with self.subTest(succeeded=succeeded, winerror=last_error):
                api = context.WindowsTokenContext.__new__(context.WindowsTokenContext)
                item = context.Privilege()
                item.luid.low = 20
                api.privilege_rows = mock.Mock(return_value=([("SeRestorePrivilege", item)], object()))
                api.security = mock.Mock()
                api.security.AdjustTokenPrivileges.return_value = succeeded
                with mock.patch.object(ctypes, "set_last_error", create=True), \
                     mock.patch.object(ctypes, "get_last_error", return_value=last_error, create=True), \
                     mock.patch.object(ctypes, "WinError", side_effect=lambda code: OSError("native failure " + str(code)), create=True):
                    with self.assertRaisesRegex(OSError, str(last_error)):
                        api.remove_backup_restore(222)

    def test_fixed_dword_queries_do_not_depend_on_zero_length_sizing(self):
        for size_delta in (0, -1):
            with self.subTest(returnedSizeDelta=size_delta):
                api = context.WindowsTokenContext.__new__(context.WindowsTokenContext)
                def information(handle, kind, output, size, returned):
                    self.assertEqual((handle, kind, size), (222, 20, ctypes.sizeof(wintypes.DWORD)))
                    self.assertIsNotNone(output)
                    ctypes.cast(output, ctypes.POINTER(wintypes.DWORD)).contents.value = 1
                    ctypes.cast(returned, ctypes.POINTER(wintypes.DWORD)).contents.value = size + size_delta
                    return True
                api.security = SimpleNamespace(GetTokenInformation=mock.Mock(side_effect=information))
                if size_delta:
                    with self.assertRaises(RuntimeError):
                        api.dword_information(222, 20)
                else:
                    self.assertEqual(api.dword_information(222, 20), 1)
                self.assertEqual(api.security.GetTokenInformation.call_count, 1)

    def test_empty_restricted_sids_accepts_count_only_buffer_but_nonempty_truncation_fails(self):
        api = context.WindowsTokenContext.__new__(context.WindowsTokenContext)
        buffer = ctypes.create_string_buffer(ctypes.sizeof(wintypes.DWORD))
        api.information = mock.Mock(return_value=buffer)
        self.assertEqual(api.groups(222, 11), [])
        wintypes.DWORD.from_buffer(buffer).value = 1
        with self.assertRaises(RuntimeError):
            api.groups(222, 11)

    @unittest.skipUnless(os.name == "nt", WINDOWS_SKIP_REASON)
    def test_windows_owned_child_preserves_parent_and_contains_descendants(self):
        NATIVE_RECORDS.clear()
        api = context.WindowsTokenContext()
        api.ensure_no_thread_token()
        parent_handle = api.open_process_token(api.current_process())
        try:
            parent_before = api.snapshot(parent_handle)
            fixture = self.root / "child 中文 space.py"
            fixture.write_text(NATIVE_FIXTURE, encoding="utf-8")
            helper = DIRECTORY / "qcs-token-context.py"
            def captured_launch(root):
                def launch(command, **keywords):
                    environment = {key: value for key, value in os.environ.items()
                                   if key not in ("PYTHONUTF8", "PYTHONIOENCODING")}
                    with (root / "child-output.log").open("wb") as output:
                        return subprocess.Popen(command, stdout=output, stderr=subprocess.STDOUT,
                                                env=environment, **keywords)
                return launch
            for mode, expected_exit in (("normal", 0), ("nonzero", 23), ("crash", 37)):
                with self.subTest(scenario=mode):
                    root = self.root / (mode + " 中文")
                    root.mkdir()
                    report = {}
                    result = context.run_owned_child(fixture, [mode, str(root), str(helper)], report=report,
                                                     popen=captured_launch(root))
                    self.assertEqual(result, expected_exit)
                    self.assertTrue(report["resumed"] and report["childExited"] and report["parentUnchanged"])
                    self.assertEqual(report["cleanupErrors"], [])
                    self.assertEqual(report["passed"], expected_exit == 0)
                    self.assertEqual(report["parentBefore"], parent_before)
                    self.assertEqual(report["parentAfter"], parent_before)
                    self.assertNotEqual(report["childAfter"]["tokenId"], parent_before["tokenId"])
                    self.assertEqual(set(report["removedPrivileges"]),
                                     {row["name"] for row in report["childBefore"]["privileges"] if row["name"] in REMOVED})
                    names = [mode, "grand-normal"] if mode == "normal" else [mode]
                    actual_tokens = {}
                    for name in names:
                        actual = json.loads((root / (name + ".json")).read_text(encoding="utf-8"))
                        self.assertFalse(context.has_backup_restore(actual["token"]))
                        self.assertEqual(actual["token"]["user"], parent_before["user"])
                        self.assertEqual(actual["utf8Mode"], 1)
                        self.assertEqual(actual["stdoutEncoding"].lower(), "utf-8")
                        actual_tokens[name] = actual
                    output = (root / "child-output.log").read_text(encoding="utf-8", errors="strict")
                    for name in names:
                        self.assertIn("QCS_TOKEN_UTF8 中文输出 " + name, output)
                    NATIVE_RECORDS.append({"scenario": mode, "boundary": report, "actual": actual_tokens})

            with self.subTest(scenario="owned-grandchild-cleanup"):
                root = self.root / "descendant 中文"
                root.mkdir()
                sentinel = subprocess.Popen([sys.executable, "-B", "-c", "import time; time.sleep(60)"],
                                            creationflags=subprocess.CREATE_NO_WINDOW)
                report = {}
                try:
                    with self.assertRaisesRegex(RuntimeError, "descendant"):
                        context.run_owned_child(fixture, ["descendant", str(root), str(helper)], report=report,
                                                popen=captured_launch(root))
                    self.assertFalse(report["passed"])
                    self.assertTrue(report["childExited"] and report["parentUnchanged"])
                    self.assertEqual(report["cleanupErrors"], [])
                    grandchild = json.loads((root / "grand-sleep.json").read_text(encoding="utf-8"))
                    self.assertFalse(context.has_backup_restore(grandchild["token"]))
                    self.assertIsNone(context.native_processes().image(grandchild["pid"]))
                    self.assertIsNone(sentinel.poll(), "Cleanup must not terminate the separate owned sentinel")
                    NATIVE_RECORDS.append({"scenario": "owned-grandchild-cleanup", "boundary": report,
                                           "grandchild": grandchild, "sentinelSurvived": True})
                finally:
                    if sentinel.poll() is None:
                        sentinel.terminate()
                    sentinel.wait(timeout=10)
                    self.assertIsNotNone(sentinel.poll())

            with self.subTest(scenario="existing-thread-identity-preserved"):
                security = api.security
                security.DuplicateTokenEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p,
                                                     ctypes.c_int, ctypes.c_int, ctypes.POINTER(wintypes.HANDLE)]
                security.DuplicateTokenEx.restype = wintypes.BOOL
                security.SetThreadToken.argtypes = [ctypes.POINTER(wintypes.HANDLE), wintypes.HANDLE]
                security.SetThreadToken.restype = wintypes.BOOL
                source, duplicate = wintypes.HANDLE(), wintypes.HANDLE()
                active = False
                try:
                    self.assertTrue(security.OpenProcessToken(api.current_process(), 0x0a, ctypes.byref(source)))
                    self.assertTrue(security.DuplicateTokenEx(source, 0x0c, None, 2, 2, ctypes.byref(duplicate)))
                    original = api.snapshot(duplicate)
                    self.assertTrue(security.SetThreadToken(None, duplicate))
                    active = True
                    launched = mock.Mock(side_effect=AssertionError("Existing thread identity must prevent child creation"))
                    with self.assertRaisesRegex(RuntimeError, "existing Windows thread token"):
                        context.reexec_if_needed(fixture, [], token_api=api, popen=launched)
                    launched.assert_not_called()
                    actual_thread = wintypes.HANDLE()
                    self.assertTrue(security.OpenThreadToken(api.kernel.GetCurrentThread(), 8, True,
                                                             ctypes.byref(actual_thread)))
                    try:
                        self.assertEqual(api.snapshot(actual_thread), original)
                    finally:
                        api.close(actual_thread)
                    NATIVE_RECORDS.append({"scenario": "existing-thread-identity-preserved", "token": original,
                                           "childCreated": False, "identityPreserved": True})
                finally:
                    if active:
                        self.assertTrue(security.SetThreadToken(None, None), "Test could not restore its original thread state")
                    for handle in (duplicate, source):
                        if handle.value:
                            api.close(handle)
            api.ensure_no_thread_token()
            self.assertEqual(api.snapshot(parent_handle), parent_before)
        finally:
            api.close(parent_handle)
            producer = sys.modules.get("qcs_test_portability_runners")
            if producer is not None:
                for row in NATIVE_RECORDS:
                    producer.queue_diagnostic("QCS_TOKEN_SCENARIO", row)


if __name__ == "__main__":
    producer = load("qcs-test-portability-runners")
    unittest.main(verbosity=2, testRunner=producer.DiagnosticTestRunner)
