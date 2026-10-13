#!/usr/bin/env python3
"""Small Windows ACL diagnostic. Optional intervention changes only a duplicate token."""
from __future__ import annotations

import argparse
import base64
import ctypes as C
from ctypes import wintypes as W
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import traceback

sys.dont_write_bytecode = True


def save(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load_runner():
    path = Path(__file__).with_name("qcs-two-checkout-portability.py")
    spec = importlib.util.spec_from_file_location("qcs_acl_probe_original_runner", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module, path


class SID_ATTRIBUTES(C.Structure):
    _fields_ = [("Sid", C.c_void_p), ("Attributes", W.DWORD)]


class TOKEN_GROUPS(C.Structure):
    _fields_ = [("Count", W.DWORD), ("Items", SID_ATTRIBUTES * 1)]


class LUID(C.Structure):
    _fields_ = [("LowPart", W.DWORD), ("HighPart", W.LONG)]


class LUID_ATTRIBUTES(C.Structure):
    _fields_ = [("Luid", LUID), ("Attributes", W.DWORD)]


class TOKEN_PRIVILEGES(C.Structure):
    _fields_ = [("Count", W.DWORD), ("Items", LUID_ATTRIBUTES * 1)]


class TOKEN_STATISTICS(C.Structure):
    _fields_ = [("TokenId", LUID), ("AuthenticationId", LUID), ("ExpirationTime", C.c_longlong),
                ("TokenType", W.DWORD), ("ImpersonationLevel", W.DWORD),
                ("DynamicCharged", W.DWORD), ("DynamicAvailable", W.DWORD),
                ("GroupCount", W.DWORD), ("PrivilegeCount", W.DWORD), ("ModifiedId", LUID)]


class Native:
    def __init__(self):
        self.k = C.WinDLL("kernel32", use_last_error=True)
        self.a = C.WinDLL("advapi32", use_last_error=True)
        signatures = [
            (self.k, "GetCurrentProcess", [], W.HANDLE),
            (self.k, "GetCurrentThread", [], W.HANDLE),
            (self.k, "CloseHandle", [W.HANDLE], W.BOOL),
            (self.k, "LocalFree", [C.c_void_p], C.c_void_p),
            (self.k, "CreateFileW", [W.LPCWSTR, W.DWORD, W.DWORD, C.c_void_p, W.DWORD, W.DWORD, W.HANDLE], W.HANDLE),
            (self.k, "DeleteFileW", [W.LPCWSTR], W.BOOL),
            (self.k, "MoveFileExW", [W.LPCWSTR, W.LPCWSTR, W.DWORD], W.BOOL),
            (self.a, "OpenProcessToken", [W.HANDLE, W.DWORD, C.POINTER(W.HANDLE)], W.BOOL),
            (self.a, "OpenThreadToken", [W.HANDLE, W.DWORD, W.BOOL, C.POINTER(W.HANDLE)], W.BOOL),
            (self.a, "DuplicateTokenEx", [W.HANDLE, W.DWORD, C.c_void_p, C.c_int, C.c_int, C.POINTER(W.HANDLE)], W.BOOL),
            (self.a, "SetThreadToken", [C.POINTER(W.HANDLE), W.HANDLE], W.BOOL),
            (self.a, "LookupPrivilegeValueW", [W.LPCWSTR, W.LPCWSTR, C.POINTER(LUID)], W.BOOL),
            (self.a, "AdjustTokenPrivileges", [W.HANDLE, W.BOOL, C.POINTER(TOKEN_PRIVILEGES), W.DWORD, C.c_void_p, C.c_void_p], W.BOOL),
            (self.a, "GetTokenInformation", [W.HANDLE, C.c_int, C.c_void_p, W.DWORD, C.POINTER(W.DWORD)], W.BOOL),
            (self.a, "LookupPrivilegeNameW", [W.LPCWSTR, C.POINTER(LUID), W.LPWSTR, C.POINTER(W.DWORD)], W.BOOL),
            (self.a, "ConvertSidToStringSidW", [C.c_void_p, C.POINTER(C.c_void_p)], W.BOOL),
            (self.a, "GetNamedSecurityInfoW", [W.LPCWSTR, C.c_int, W.DWORD] + [C.POINTER(C.c_void_p)] * 5, W.DWORD),
            (self.a, "ConvertSecurityDescriptorToStringSecurityDescriptorW", [C.c_void_p, W.DWORD, W.DWORD, C.POINTER(C.c_void_p), C.POINTER(W.DWORD)], W.BOOL),
            (self.a, "GetSecurityDescriptorLength", [C.c_void_p], W.DWORD),
        ]
        for library, name, arguments, result in signatures:
            function = getattr(library, name)
            function.argtypes, function.restype = arguments, result

    @staticmethod
    def path(path):
        value = str(Path(path).absolute())
        return value if value.startswith("\\\\?\\") else ("\\\\?\\UNC\\" + value[2:] if value.startswith("\\\\") else "\\\\?\\" + value)

    def close(self, handle):
        if not self.k.CloseHandle(handle):
            raise C.WinError(C.get_last_error())

    def sid(self, pointer):
        text = C.c_void_p()
        if not self.a.ConvertSidToStringSidW(pointer, C.byref(text)):
            raise C.WinError(C.get_last_error())
        try:
            return C.wstring_at(text)
        finally:
            self.k.LocalFree(text)

    def information(self, handle, kind, fixed_size=None):
        size = W.DWORD(fixed_size or 0)
        if fixed_size is None:
            sized = self.a.GetTokenInformation(handle, kind, None, 0, C.byref(size))
            error = C.get_last_error()
            if sized or error != 122:
                raise RuntimeError(f"Unexpected token information size query: kind={kind}, success={sized}, winerror={error}, size={size.value}")
        buffer = C.create_string_buffer(size.value)
        if not self.a.GetTokenInformation(handle, kind, buffer, size, C.byref(size)):
            raise C.WinError(C.get_last_error())
        return buffer

    def token(self, handle):
        user = self.information(handle, 1)
        record = {"user": self.sid(SID_ATTRIBUTES.from_buffer(user).Sid)}
        for label, kind in (("defaultOwner", 4), ("primaryGroup", 5)):
            buffer = self.information(handle, kind)
            record[label] = self.sid(C.c_void_p.from_buffer(buffer).value)
        for label, kind in (("groups", 2), ("restrictedSids", 11)):
            buffer = self.information(handle, kind)
            count = W.DWORD.from_buffer(buffer).value
            offset = TOKEN_GROUPS.Items.offset
            if count and offset + count * C.sizeof(SID_ATTRIBUTES) > len(buffer):
                raise RuntimeError("Malformed native group buffer")
            values = []
            for index in range(count):
                item = SID_ATTRIBUTES.from_buffer(buffer, offset + index * C.sizeof(SID_ATTRIBUTES))
                values.append({"sid": self.sid(item.Sid), "attributes": item.Attributes,
                               "enabled": bool(item.Attributes & 4), "denyOnly": bool(item.Attributes & 16)})
            record[label] = values
        buffer = self.information(handle, 3)
        count = W.DWORD.from_buffer(buffer).value
        offset = TOKEN_PRIVILEGES.Items.offset
        if offset + count * C.sizeof(LUID_ATTRIBUTES) > len(buffer):
            raise RuntimeError("Malformed native privilege buffer")
        values = []
        for index in range(count):
            item = LUID_ATTRIBUTES.from_buffer(buffer, offset + index * C.sizeof(LUID_ATTRIBUTES))
            size = W.DWORD(256)
            name = C.create_unicode_buffer(size.value)
            if not self.a.LookupPrivilegeNameW(None, C.byref(item.Luid), name, C.byref(size)):
                raise C.WinError(C.get_last_error())
            values.append({"name": name.value, "attributes": item.Attributes,
                           "enabled": bool(item.Attributes & 2), "enabledByDefault": bool(item.Attributes & 1),
                           "removed": bool(item.Attributes & 4), "usedForAccess": bool(item.Attributes & 0x80000000)})
        record["privileges"] = values
        for label, kind in (("type", 8), ("elevationType", 18), ("elevated", 20), ("hasRestrictions", 21)):
            record[label] = W.DWORD.from_buffer(self.information(handle, kind, C.sizeof(W.DWORD))).value
        integrity = self.information(handle, 25)
        item = SID_ATTRIBUTES.from_buffer(integrity)
        record["integrity"] = {"sid": self.sid(item.Sid), "attributes": item.Attributes}
        if record["type"] == 2:
            record["impersonationLevel"] = W.DWORD.from_buffer(self.information(handle, 9, C.sizeof(W.DWORD))).value
        statistics = TOKEN_STATISTICS.from_buffer(self.information(handle, 10, C.sizeof(TOKEN_STATISTICS)))
        record["identity"] = {label: {"low": getattr(statistics, field).LowPart,
                                      "high": getattr(statistics, field).HighPart}
                              for label, field in (("tokenId", "TokenId"), ("authenticationId", "AuthenticationId"), ("modifiedId", "ModifiedId"))}
        return record

    def tokens(self):
        process = W.HANDLE()
        if not self.a.OpenProcessToken(self.k.GetCurrentProcess(), 8, C.byref(process)):
            raise C.WinError(C.get_last_error())
        try:
            result = {"process": self.token(process)}
        finally:
            self.close(process)
        thread = W.HANDLE()
        if self.a.OpenThreadToken(self.k.GetCurrentThread(), 8, True, C.byref(thread)):
            try:
                result["thread"] = self.token(thread)
                result["effective"] = "thread"
            finally:
                self.close(thread)
        else:
            error = C.get_last_error()
            if error != 1008:
                raise C.WinError(error)
            result.update(thread=None, threadOpenWinerror=error, effective="process")
        return result

    def acl(self, path):
        owner, group, dacl, sacl, descriptor = [C.c_void_p() for _ in range(5)]
        error = self.a.GetNamedSecurityInfoW(self.path(path), 1, 7, C.byref(owner), C.byref(group),
                                           C.byref(dacl), C.byref(sacl), C.byref(descriptor))
        if error:
            raise C.WinError(error)
        try:
            text, size = C.c_void_p(), W.DWORD()
            if not self.a.ConvertSecurityDescriptorToStringSecurityDescriptorW(descriptor, 1, 7, C.byref(text), C.byref(size)):
                raise C.WinError(C.get_last_error())
            try:
                stat = Path(path).stat(follow_symlinks=False)
                return {"path": str(path), "sddl": C.wstring_at(text),
                        "binaryBase64": base64.b64encode(C.string_at(descriptor, self.a.GetSecurityDescriptorLength(descriptor))).decode("ascii"),
                        "fileIdentity": {"device": stat.st_dev, "inode": stat.st_ino},
                        "attributes": stat.st_file_attributes, "owner": self.sid(owner), "group": self.sid(group)}
            finally:
                self.k.LocalFree(text)
        finally:
            self.k.LocalFree(descriptor)

    def open_file(self, path, access, flags):
        handle = self.k.CreateFileW(self.path(path), access, 7, None, 3, flags, None)
        if handle == C.c_void_p(-1).value:
            raise C.WinError(C.get_last_error())
        self.close(handle)

    def delete(self, path):
        if not self.k.DeleteFileW(str(path)):
            raise C.WinError(C.get_last_error())

    def rename(self, path):
        if not self.k.MoveFileExW(str(path), str(path.with_suffix(".renamed")), 0):
            raise C.WinError(C.get_last_error())


class ProbeToken:
    """Own only a duplicate. Preserve an existing thread token by its handle."""
    def __init__(self, native, mode, reports):
        self.native, self.mode, self.reports = native, mode, reports
        self.original_thread = W.HANDLE()
        self.process = W.HANDLE()
        self.duplicate = W.HANDLE()
        self.active = False
        self.before = None
        self.record = {"mode": mode, "scope": "duplicate-thread-token", "privilegesChanged": False,
                       "threadTokenRestored": False, "processTokenUnchanged": False}

    def write(self):
        save(self.reports / "token-intervention.json", self.record)

    def activate(self):
        native = self.native
        self.before = native.tokens()
        self.record["before"] = self.before
        self.write()
        # QUERY | DUPLICATE | IMPERSONATE lets us restore this exact original
        # thread token. Failure other than ERROR_NO_TOKEN is not a fallback.
        if native.a.OpenThreadToken(native.k.GetCurrentThread(), 0x0e, True, C.byref(self.original_thread)):
            source = self.original_thread
        else:
            error = C.get_last_error()
            if error != 1008:
                raise C.WinError(error)
            if not native.a.OpenProcessToken(native.k.GetCurrentProcess(), 0x0a, C.byref(self.process)):
                raise C.WinError(C.get_last_error())
            source = self.process
        # New non-inheritable impersonation token, SecurityImpersonation level.
        # QUERY | ADJUST_PRIVILEGES | IMPERSONATE applies only to this copy.
        if not native.a.DuplicateTokenEx(source, 0x2c, None, 2, 2, C.byref(self.duplicate)):
            raise C.WinError(C.get_last_error())
        duplicate_before = native.token(self.duplicate)
        self.record["duplicateBefore"] = duplicate_before
        source_record = self.before[self.before["effective"]]
        self.record["copyDiff"] = {key: {"before": source_record.get(key), "after": duplicate_before.get(key)}
                                  for key in set(source_record) | set(duplicate_before)
                                  if source_record.get(key) != duplicate_before.get(key)}
        copied_fields = set(source_record) | set(duplicate_before)
        copied_fields -= {"type", "impersonationLevel", "identity"}
        self.record["copyPreservedAccessFields"] = all(source_record.get(key) == duplicate_before.get(key) for key in copied_fields)
        self.write()
        if not self.record["copyPreservedAccessFields"]:
            raise RuntimeError("Duplicate token did not preserve the source access fields")
        if self.mode == "disable-restore":
            matches = [item for item in duplicate_before["privileges"] if item["name"] == "SeRestorePrivilege"]
            if len(matches) != 1 or not matches[0]["enabled"]:
                raise RuntimeError("Causal intervention requires an already-enabled SeRestorePrivilege, never enables it")
            change = TOKEN_PRIVILEGES()
            change.Count = 1
            if not native.a.LookupPrivilegeValueW(None, "SeRestorePrivilege", C.byref(change.Items[0].Luid)):
                raise C.WinError(C.get_last_error())
            change.Items[0].Attributes = 0
            C.set_last_error(0)
            success = native.a.AdjustTokenPrivileges(self.duplicate, False, C.byref(change), 0, None, None)
            error = C.get_last_error()
            self.record["adjustResult"] = {"success": bool(success), "winerror": error}
            self.write()
            if not success or error != 0:  # includes ERROR_NOT_ALL_ASSIGNED (1300)
                raise C.WinError(error)
            self.record["privilegesChanged"] = True
        duplicate_after = native.token(self.duplicate)
        self.record["duplicateAfter"] = duplicate_after
        self.record["duplicateDiff"] = {key: {"before": duplicate_before.get(key), "after": duplicate_after.get(key)}
                                        for key in set(duplicate_before) | set(duplicate_after)
                                        if duplicate_before.get(key) != duplicate_after.get(key)}
        expected = json.loads(json.dumps(duplicate_before))
        if self.mode == "disable-restore":
            for privilege in expected["privileges"]:
                if privilege["name"] == "SeRestorePrivilege":
                    privilege["attributes"] &= ~2
                    privilege["enabled"] = False
            # Windows advances ModifiedId when changing a token. Its token and
            # authentication identities must still refer to the same copy.
            expected["identity"]["modifiedId"] = duplicate_after["identity"]["modifiedId"]
        self.record["onlyRequestedChange"] = expected == duplicate_after
        self.write()
        if not self.record["onlyRequestedChange"]:
            raise RuntimeError("Duplicate token has unexpected changes")
        if not native.a.SetThreadToken(None, self.duplicate):
            raise C.WinError(C.get_last_error())
        self.active = True
        effective = native.tokens()
        self.record["effective"] = effective
        self.write()
        if effective["process"] != self.before["process"] or effective["thread"] != duplicate_after:
            raise RuntimeError("Unexpected effective token or process token mutation")

    def restore(self):
        error = None
        try:
            if self.active:
                if not self.native.a.SetThreadToken(None, self.original_thread if self.original_thread.value else None):
                    raise C.WinError(C.get_last_error())
                self.active = False
            after = self.native.tokens()
            self.record["afterRestoration"] = after
            self.record["processTokenUnchanged"] = self.before is not None and self.before["process"] == after["process"]
            self.record["threadTokenRestored"] = self.before is not None and self.before == after
            self.write()
            if not self.record["threadTokenRestored"]:
                raise RuntimeError("Original effective token identity or state was not restored")
        except BaseException as caught:
            error = caught
            self.record["restorationError"] = repr(caught)
        finally:
            close_errors = []
            for handle in (self.duplicate, self.process, self.original_thread):
                if handle.value:
                    try:
                        self.native.close(handle)
                    except BaseException as caught:
                        close_errors.append(repr(caught))
                    handle.value = None
            if close_errors:
                self.record["closeErrors"] = close_errors
                if error is None:
                    error = RuntimeError("Token handle cleanup failed")
            self.write()
        if error is not None:
            raise error


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--probe-token", choices=("original", "duplicate-control", "disable-restore"), default="original")
    args = parser.parse_args()
    if os.name != "nt":
        parser.error("This diagnostic requires Windows")
    output = args.output_root.absolute()
    output.mkdir(parents=True, exist_ok=True)
    if output.is_symlink() or output.is_junction():
        raise RuntimeError("Output root must be an ordinary directory")
    summary_path = output / "summary.json"
    if summary_path.exists():
        raise RuntimeError("Refusing to overwrite prior diagnostic evidence")
    owned = Path(tempfile.mkdtemp(prefix="qcs-a-", dir=output))
    owned_identity = (owned.stat().st_dev, owned.stat().st_ino)
    source = owned / "中文 源码"
    source.mkdir()
    reports = owned / "reports"
    reports.mkdir()
    original = reports / "source-original-acl.json"
    report = {"schemaVersion": 1, "diagnosticOnly": True, "privilegesChanged": False,
              "probeTokenMode": args.probe_token, "tokenRestored": args.probe_token == "original",
              "startedAt": datetime.now(timezone.utc).isoformat(), "ownedRoot": str(owned),
              "python": sys.version, "executable": sys.executable, "platform": platform.platform(),
              "pid": os.getpid(), "parentPid": os.getppid(),
              "scriptSha256": sha(__file__), "environment": {name: os.environ.get(name) for name in
              ("GITHUB_SHA", "GITHUB_RUN_ID", "ImageOS", "ImageVersion", "RUNNER_OS")},
              "collectionComplete": False, "restored": False, "cleanup": False}
    save(summary_path, report)
    native = Native()
    runner, runner_path = load_runner()
    report["runnerSha256"] = sha(runner_path)
    pwsh = shutil.which("pwsh")
    if not pwsh:
        raise RuntimeError("PowerShell 7 is required")
    script = reports / "qcs-owned-acl.ps1"
    script.write_text(runner.ACL_SCRIPT, encoding="utf-8")
    save(owned / ".qcs-owned-run.json", {"source": str(source), "diagnosticOnly": True})

    def ps(mode):
        command = [pwsh, "-NoProfile", "-File", str(script), "-Source", str(source),
                   "-Report", str(original), "-Mode", mode]
        completed = subprocess.run(command, cwd=owned, capture_output=True, timeout=45)
        (reports / (mode + ".stdout.log")).write_bytes(completed.stdout)
        (reports / (mode + ".stderr.log")).write_bytes(completed.stderr)
        record = {"argv": command, "exitCode": completed.returncode}
        save(reports / (mode + ".command.json"), record)
        return completed

    primary = None
    probe_token = None
    try:
        save(reports / "token-before.json", native.tokens())
        (source / "src/core").mkdir(parents=True)
        git = source / ".git"
        git.mkdir()
        (source / "CMakeLists.txt").write_bytes(b"owned source bytes\n")
        names = ("delete", "rename", "native-delete", "native-rename", "delete-open", "delete-open-backup", "write-open-backup")
        for name in names:
            (git / ("qcs-acl-" + name + ".tmp")).write_bytes(b"owned disposable\n")
        before = runner.snapshot(source)
        save(reports / "source-bytes-before.json", before)
        denied = ps("deny")
        if denied.returncode:
            raise RuntimeError("ACL deny subprocess failed, see raw logs")
        saved = json.loads(original.read_text(encoding="utf-8-sig"))
        applied = json.loads((reports / "source-denied-acl.json").read_text(encoding="utf-8-sig"))
        if applied["nodeCount"] != len(saved["nodes"]) or applied["appliedCount"] != len(saved["nodes"]) or not all(n["applied"] for n in applied["nodes"]):
            raise RuntimeError("Incomplete ACL denial inventory")
        save(reports / "native-applied-before-probes.json", [native.acl(source / item["relativePath"]) for item in saved["nodes"]])
        save(reports / "token-before-probes.json", native.tokens())
        if args.probe_token != "original":
            probe_token = ProbeToken(native, args.probe_token, reports)
            probe_token.activate()
            report["privilegesChanged"] = probe_token.record["privilegesChanged"]
        save(reports / "token-effective-probes.json", native.tokens())
        # Execute the original five probes without substituting weaker APIs.
        probes = runner.write_probes(source)
        save(reports / "original-five-probes.json", probes)
        comparisons = []
        cases = [
            ("native-delete", "DeleteFileW", native.delete),
            ("native-rename", "MoveFileExW(flags=0)", native.rename),
            ("delete-open", "CreateFileW(DELETE, FILE_ATTRIBUTE_NORMAL)", lambda path: native.open_file(path, 0x10000, 0x80)),
            ("delete-open-backup", "CreateFileW(DELETE, FILE_FLAG_BACKUP_SEMANTICS)", lambda path: native.open_file(path, 0x10000, 0x02000000)),
            ("write-open-backup", "CreateFileW(GENERIC_WRITE, FILE_FLAG_BACKUP_SEMANTICS)", lambda path: native.open_file(path, 0x40000000, 0x02000000)),
        ]
        for name, api, action in cases:
            path = git / ("qcs-acl-" + name + ".tmp")
            row = {"operation": name, "api": api, "beforeLeaf": native.acl(path), "beforeParent": native.acl(git)}
            # Persist the native state before any operation can remove its leaf.
            save(reports / (name + "-before.json"), row)
            try:
                action(path)
                row.update(succeeded=True, winerror=0)
            except OSError as error:
                row.update(succeeded=False, winerror=error.winerror, error=str(error))
            row["sourceExistsAfter"] = path.exists()
            row["renamedExistsAfter"] = path.with_suffix(".renamed").exists()
            comparisons.append(row)
            save(reports / "native-comparisons.json", comparisons)
        save(reports / "token-after-probes.json", native.tokens())
        report["originalFiveAllDenied"] = all(item["denied"] and item["winerror"] == 5 for item in probes)
        report["collectionComplete"] = True
    except BaseException as error:
        primary = error
        report["primaryError"] = repr(error)
        (reports / "primary-error.log").write_text(traceback.format_exc(), encoding="utf-8")
    finally:
        # End impersonation before starting any restoration subprocess. Never
        # proceed under a token whose restoration failed or was unverified.
        if probe_token is not None:
            try:
                probe_token.restore()
                report["tokenRestored"] = True
                report["privilegesChanged"] = probe_token.record["privilegesChanged"]
            except BaseException as error:
                report["tokenRestorationError"] = repr(error)
        elif args.probe_token != "original":
            report["tokenRestored"] = True  # no impersonation was attempted
        # A successful observed deletion is not disguised by recreating its path.
        # Missing/replaced nodes remain strict restoration failures and are kept.
        if original.exists() and report["tokenRestored"]:
            try:
                restored = ps("restore")
                result_path = reports / "source-restored-acl.json"
                result = json.loads(result_path.read_text(encoding="utf-8-sig")) if result_path.exists() else {}
                report["restored"] = restored.returncode == 0 and result.get("allNodesRestored") is True and result.get("semanticMatch") is True
                report["restorationExitCode"] = restored.returncode
            except BaseException as error:
                report["restorationError"] = repr(error)
        if report["restored"] and primary is None:
            try:
                if owned.is_symlink() or owned.is_junction() or (owned.stat().st_dev, owned.stat().st_ino) != owned_identity:
                    raise RuntimeError("Owned directory identity changed")
                if source.resolve().parent != owned.resolve() or source.is_symlink() or source.is_junction():
                    raise RuntimeError("Source cleanup boundary changed")
                if runner.snapshot(source) != before:
                    raise RuntimeError("Source contents changed, retaining diagnostic fixture")
                for path in source.rglob("*"):
                    stat = path.stat(follow_symlinks=False)
                    if stat.st_file_attributes & 0x400 or (path.is_file() and stat.st_nlink != 1):
                        raise RuntimeError("Unsafe node in source cleanup tree")
                # This exact freshly created source is the only recursive target.
                shutil.rmtree(source)
                report["cleanup"] = not source.exists()
            except BaseException as error:
                report["cleanupError"] = repr(error)
        report["fixtureRetained"] = source.exists()
        report["finishedAt"] = datetime.now(timezone.utc).isoformat()
        save(summary_path, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["collectionComplete"] and report["tokenRestored"] and report["restored"] and report["cleanup"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
