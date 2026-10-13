#!/usr/bin/env python3
"""Small Windows ACL diagnostic. Never changes token privileges or product data."""
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, required=True)
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
        # A successful observed deletion is not disguised by recreating its path.
        # Missing/replaced nodes remain strict restoration failures and are kept.
        if original.exists():
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
    return 0 if report["collectionComplete"] and report["restored"] and report["cleanup"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
