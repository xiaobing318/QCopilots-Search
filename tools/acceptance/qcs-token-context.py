"""Run acceptance in an owned Windows process without backup/restore privileges."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

REMOVED_PRIVILEGES = frozenset(("SeBackupPrivilege", "SeRestorePrivilege"))


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


class Luid(ctypes.Structure):
    _fields_ = [("low", wintypes.DWORD), ("high", wintypes.LONG)]


class Privilege(ctypes.Structure):
    _fields_ = [("luid", Luid), ("attributes", wintypes.DWORD)]


class SidAttributes(ctypes.Structure):
    _fields_ = [("sid", ctypes.c_void_p), ("attributes", wintypes.DWORD)]


class Statistics(ctypes.Structure):
    _fields_ = [("tokenId", Luid), ("authenticationId", Luid), ("expiration", ctypes.c_int64),
                ("tokenType", wintypes.DWORD), ("impersonationLevel", wintypes.DWORD),
                ("dynamicCharged", wintypes.DWORD), ("dynamicAvailable", wintypes.DWORD),
                ("groupCount", wintypes.DWORD), ("privilegeCount", wintypes.DWORD), ("modifiedId", Luid)]


def luid_text(value):
    return f"{value.high & 0xffffffff:08x}:{value.low:08x}"


class WindowsTokenContext:
    def __init__(self):
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.security = ctypes.WinDLL("advapi32", use_last_error=True)
        signatures = (
            (self.kernel, "GetCurrentProcess", [], wintypes.HANDLE),
            (self.kernel, "GetCurrentThread", [], wintypes.HANDLE),
            (self.kernel, "CloseHandle", [wintypes.HANDLE], wintypes.BOOL),
            (self.kernel, "LocalFree", [ctypes.c_void_p], ctypes.c_void_p),
            (self.security, "OpenProcessToken", [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)], wintypes.BOOL),
            (self.security, "OpenThreadToken", [wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL, ctypes.POINTER(wintypes.HANDLE)], wintypes.BOOL),
            (self.security, "GetTokenInformation", [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
            (self.security, "LookupPrivilegeNameW", [wintypes.LPCWSTR, ctypes.POINTER(Luid), wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
            (self.security, "ConvertSidToStringSidW", [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)], wintypes.BOOL),
            (self.security, "AdjustTokenPrivileges", [wintypes.HANDLE, wintypes.BOOL, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p], wintypes.BOOL),
        )
        for library, name, arguments, result in signatures:
            function = getattr(library, name)
            function.argtypes, function.restype = arguments, result

    def current_process(self):
        return self.kernel.GetCurrentProcess()

    def close(self, handle):
        if not self.kernel.CloseHandle(handle):
            raise ctypes.WinError(ctypes.get_last_error())

    def open_process_token(self, process, adjust=False):
        handle = wintypes.HANDLE()
        if not self.security.OpenProcessToken(process, 0x8 | (0x20 if adjust else 0), ctypes.byref(handle)):
            raise ctypes.WinError(ctypes.get_last_error())
        return handle.value

    def ensure_no_thread_token(self):
        handle = wintypes.HANDLE()
        if self.security.OpenThreadToken(self.kernel.GetCurrentThread(), 0x8, True, ctypes.byref(handle)):
            self.close(handle.value)
            raise RuntimeError("An existing Windows thread token is unsupported. The current identity was not changed")
        error = ctypes.get_last_error()
        if error != 1008:  # ERROR_NO_TOKEN
            raise ctypes.WinError(error)

    def information(self, handle, kind):
        size = wintypes.DWORD()
        self.security.GetTokenInformation(handle, kind, None, 0, ctypes.byref(size))
        require(ctypes.get_last_error() == 122 and size.value > 0, "Cannot size Windows token information")
        buffer = ctypes.create_string_buffer(size.value)
        if not self.security.GetTokenInformation(handle, kind, buffer, size.value, ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        return buffer

    def dword_information(self, handle, kind):
        # Fixed DWORD queries (notably TokenElevation) can return
        # ERROR_BAD_LENGTH instead of a sizing result for a zero-byte buffer.
        value, size = wintypes.DWORD(), wintypes.DWORD()
        if not self.security.GetTokenInformation(handle, kind, ctypes.byref(value), ctypes.sizeof(value), ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        require(size.value == ctypes.sizeof(value), "Invalid DWORD token information size")
        return int(value.value)

    def sid(self, address):
        if not address:
            return None
        text = ctypes.c_void_p()
        if not self.security.ConvertSidToStringSidW(address, ctypes.byref(text)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return ctypes.wstring_at(text.value)
        finally:
            require(not self.kernel.LocalFree(text), "Cannot release the Windows SID string")

    def sid_attributes(self, value):
        return {"sid": self.sid(value.sid), "attributes": int(value.attributes)}

    def groups(self, handle, kind):
        class Groups(ctypes.Structure):
            _fields_ = [("count", wintypes.DWORD), ("items", SidAttributes * 1)]
        buffer = self.information(handle, kind)
        count = wintypes.DWORD.from_buffer(buffer).value
        if not count:
            return []
        offset = Groups.items.offset
        require(offset + count * ctypes.sizeof(SidAttributes) <= len(buffer), "Invalid token group buffer")
        return [self.sid_attributes(SidAttributes.from_buffer(buffer, offset + index * ctypes.sizeof(SidAttributes)))
                for index in range(count)]

    def privilege_rows(self, handle):
        buffer = self.information(handle, 3)  # TokenPrivileges
        count = wintypes.DWORD.from_buffer(buffer).value
        offset = ctypes.sizeof(wintypes.DWORD)
        require(offset + count * ctypes.sizeof(Privilege) <= len(buffer), "Invalid token privilege buffer")
        rows = []
        for index in range(count):
            item = Privilege.from_buffer(buffer, offset + index * ctypes.sizeof(Privilege))
            size = wintypes.DWORD()
            self.security.LookupPrivilegeNameW(None, ctypes.byref(item.luid), None, ctypes.byref(size))
            require(ctypes.get_last_error() == 122 and size.value > 0, "Cannot size Windows privilege name")
            name = ctypes.create_unicode_buffer(size.value + 1)
            size.value += 1
            if not self.security.LookupPrivilegeNameW(None, ctypes.byref(item.luid), name, ctypes.byref(size)):
                raise ctypes.WinError(ctypes.get_last_error())
            rows.append((name.value, item))
        return rows, buffer

    def snapshot(self, handle):
        stats = Statistics.from_buffer_copy(self.information(handle, 10))
        user = self.information(handle, 1)
        integrity = self.information(handle, 25)
        owner = self.information(handle, 4)
        primary_group = self.information(handle, 5)
        default_dacl = self.information(handle, 6)
        acl = ctypes.c_void_p.from_buffer(default_dacl).value
        acl_bytes = None
        if acl:
            size = ctypes.c_ushort.from_address(acl + 2).value
            require(size >= 8, "Invalid token default DACL")
            acl_bytes = ctypes.string_at(acl, size).hex()
        rows, storage = self.privilege_rows(handle)
        result = {
            "tokenId": luid_text(stats.tokenId), "modifiedId": luid_text(stats.modifiedId),
            "authenticationId": luid_text(stats.authenticationId),
            "tokenType": {1: "primary", 2: "impersonation"}.get(stats.tokenType, "unknown"),
            "user": self.sid_attributes(SidAttributes.from_buffer(user)),
            "owner": self.sid(ctypes.c_void_p.from_buffer(owner).value),
            "primaryGroup": self.sid(ctypes.c_void_p.from_buffer(primary_group).value),
            "groups": self.groups(handle, 2), "restrictedSids": self.groups(handle, 11),
            "privileges": [{"name": name, "luid": luid_text(item.luid), "attributes": int(item.attributes)} for name, item in rows],
            "defaultDacl": acl_bytes, "integrity": self.sid_attributes(SidAttributes.from_buffer(integrity)),
        }
        for name, kind in (("sessionId", 12), ("elevationType", 18), ("elevated", 20), ("mandatoryPolicy", 27)):
            result[name] = self.dword_information(handle, kind)
        return result

    def remove_backup_restore(self, handle):
        rows, storage = self.privilege_rows(handle)
        selected = [(name, item) for name, item in rows if name in REMOVED_PRIVILEGES]
        if not selected:
            return []
        class Changes(ctypes.Structure):
            _fields_ = [("count", wintypes.DWORD), ("items", Privilege * len(selected))]
        changes = Changes()
        changes.count = len(selected)
        for index, (_, item) in enumerate(selected):
            changes.items[index].luid = item.luid
            changes.items[index].attributes = 4  # SE_PRIVILEGE_REMOVED, not merely disabled
        ctypes.set_last_error(0)
        succeeded = self.security.AdjustTokenPrivileges(handle, False, ctypes.byref(changes), 0, None, None)
        error = ctypes.get_last_error()
        if not succeeded or error:
            raise ctypes.WinError(error)
        return [name for name, _ in selected]


def has_backup_restore(snapshot):
    return any(row["name"] in REMOVED_PRIVILEGES for row in snapshot["privileges"])


def validate_child_identity(child, parent):
    require(child["tokenType"] == parent["tokenType"] == "primary", "Expected independent primary process tokens")
    require(child["tokenId"] != parent["tokenId"], "The child token is not independent of the parent token")
    require(child["user"] == parent["user"], "The child token belongs to a different user")


def validate_child_change(before, after, parent):
    validate_child_identity(before, parent)
    require(not has_backup_restore(after), "The child still holds backup or restore privileges")
    expected = {key: value for key, value in before.items() if key not in ("privileges", "modifiedId")}
    actual = {key: value for key, value in after.items() if key not in ("privileges", "modifiedId")}
    require(actual == expected, "Restricting the child changed its identity or other token attributes")
    def privilege_map(rows):
        result = {}
        for row in rows:
            key = (row["name"], row["luid"])
            require(key not in result, "The token contains duplicate privilege identities")
            result[key] = row["attributes"]
        return result
    original = privilege_map(before["privileges"])
    remaining = {key: attributes for key, attributes in original.items() if key[0] not in REMOVED_PRIVILEGES}
    # SE_PRIVILEGE_REMOVED may reorder the surviving entries.
    require(privilege_map(after["privileges"]) == remaining, "Restricting the child changed unrelated privileges")


def native_processes():
    path = Path(__file__).with_name("qcs_owned_processes.py")
    spec = importlib.util.spec_from_file_location("qcs_token_owned_processes", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    class CheckedProcesses(module.WindowsProcesses):
        def close(self, handle):
            if not self.k.CloseHandle(handle):
                raise ctypes.WinError(ctypes.get_last_error())
    return CheckedProcesses()


def run_owned_child(script, arguments, *, token_api=None, processes=None, popen=None, report=None):
    """Restrict only a newly created suspended process, before any child code runs."""
    api = token_api or WindowsTokenContext()
    jobs = processes or native_processes()
    launch = popen or subprocess.Popen
    record = report if report is not None else {}
    record.update({"schemaVersion": 1, "kind": "qcs-process-token-context", "passed": False, "resumed": False})
    parent_token = child_token = child = job = None
    primary = None
    exit_code = None
    cleanup_errors = []
    try:
        api.ensure_no_thread_token()
        parent_token = api.open_process_token(api.current_process())
        parent = api.snapshot(parent_token)
        record["parentBefore"] = parent
        command = [sys.executable, "-X", "utf8", "-B", str(Path(script).resolve()), *map(str, arguments)]
        for stream in (sys.stdout, sys.stderr):
            stream.flush()
        child = launch(command, creationflags=0x00000004)
        record.update({"command": command, "childPid": child.pid})
        job = jobs.job(child)
        child_token = api.open_process_token(int(child._handle), adjust=True)
        before = api.snapshot(child_token)
        record["childBefore"] = before
        validate_child_identity(before, parent)
        record["removedPrivileges"] = api.remove_backup_restore(child_token)
        after = api.snapshot(child_token)
        record["childAfter"] = after
        validate_child_change(before, after, parent)
        record["parentAfterRestriction"] = api.snapshot(parent_token)
        require(record["parentAfterRestriction"] == parent, "Restricting the child changed the parent token")
        api.close(child_token)
        child_token = None
        jobs.resume(child)
        record["resumed"] = True
        exit_code = child.wait()
        record["childExitCode"] = exit_code
        require(jobs.active(job) == 0, "The entry process left owned descendant processes alive")
    except BaseException as error:
        primary = error
        record["error"] = type(error).__name__ + ": " + str(error)
    finally:
        if child_token is not None:
            try:
                api.close(child_token)
            except BaseException as error:
                cleanup_errors.append("Child token: " + repr(error))
        if job is not None:
            try:
                require(jobs.stop(job) == 0, "Owned child processes did not exit")
            except BaseException as error:
                cleanup_errors.append("Owned job: " + repr(error))
        if child is not None:
            try:
                if child.poll() is None:
                    child.terminate()
                child.wait(timeout=20)
                record["childExited"] = child.poll() is not None
            except BaseException as error:
                cleanup_errors.append("Owned child: " + repr(error))
        if job is not None:
            try:
                jobs.close(job)
            except BaseException as error:
                cleanup_errors.append("Job handle: " + repr(error))
        if parent_token is not None:
            try:
                record["parentAfter"] = api.snapshot(parent_token)
                record["parentUnchanged"] = record["parentAfter"] == record.get("parentBefore")
                require(record["parentUnchanged"], "The parent token changed during acceptance")
            except BaseException as error:
                cleanup_errors.append("Parent verification: " + repr(error))
            finally:
                try:
                    api.close(parent_token)
                except BaseException as error:
                    cleanup_errors.append("Parent token: " + repr(error))
        record["cleanupErrors"] = cleanup_errors
        if cleanup_errors:
            note = "Token context cleanup failed: " + "; ".join(cleanup_errors)
            if primary is None:
                primary = RuntimeError(note)
            elif hasattr(primary, "add_note"):
                primary.add_note(note)
        record["passed"] = primary is None and exit_code == 0 and record.get("parentUnchanged") is True
    if primary is not None:
        raise primary
    return exit_code


def reexec_if_needed(script, arguments, *, token_api=None, processes=None, popen=None, report=None, platform=None):
    if (os.name if platform is None else platform) != "nt":
        return None
    api = token_api or WindowsTokenContext()
    api.ensure_no_thread_token()
    token = api.open_process_token(api.current_process())
    try:
        snapshot = api.snapshot(token)
        require(snapshot["tokenType"] == "primary", "Expected a primary process token")
        needed = has_backup_restore(snapshot)
    finally:
        api.close(token)
    if not needed:
        return None
    record = report if report is not None else {}
    primary = None
    try:
        return run_owned_child(script, arguments, token_api=api, processes=processes, popen=popen, report=record)
    except BaseException as error:
        primary = error
        raise
    finally:
        # This runs only around the whole entry point, after its output ends.
        # The reusable child function never interrupts unittest case framing.
        try:
            print("QCS_TOKEN_CONTEXT " + json.dumps(record, ensure_ascii=False), file=sys.stderr, flush=True)
        except BaseException as error:
            if primary is None:
                raise
            if hasattr(primary, "add_note"):
                primary.add_note("Token context output also failed: " + repr(error))
