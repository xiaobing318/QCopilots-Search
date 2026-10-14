#!/usr/bin/env python3
"""Read-only observer for a separately launched ordinary GUI sentinel.

Importing this file starts nothing. The portability runner may instantiate the
observer only on its explicit --execute path. The launcher owns the GUI/service.
This observer never starts, pauses, shuts down or terminates those processes.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import time


def windows_root(environment):
    value = environment.get("SYSTEMROOT") or environment.get("WINDIR")
    require(bool(value), "Windows system directory is missing from the environment")
    root = Path(value)
    require(root.is_absolute() and root.is_dir(), "Windows system directory is invalid")
    return root


def require(ok, message):
    if not ok:
        raise RuntimeError(message)


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def write(path, doc):
    Path(path).write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


PROCESS_SCRIPT = r'''param([int]$GuiId,[int]$ServiceId)
$ErrorActionPreference = 'Stop'
$result = foreach ($requested in @($GuiId,$ServiceId)) {
  $native = Get-CimInstance Win32_Process -Filter "ProcessId = $requested"
  if (-not $native) { throw "Sentinel process no longer exists: $requested" }
  $process = Get-Process -Id $requested
  [pscustomobject]@{pid=$requested;imagePath=$process.Path;
    creationTime100ns=$process.StartTime.ToUniversalTime().ToFileTimeUtc().ToString();
    commandLine=$native.CommandLine;mainWindowHandle=$process.MainWindowHandle.ToInt64().ToString();
    responding=$process.Responding}
}
ConvertTo-Json -InputObject @($result) -Compress
'''


class SentinelObserver:
    def __init__(self, launch_record, reports, head, producer_hashes, qt_prefix):
        self.launch_record, self.reports = Path(launch_record).resolve(), Path(reports)
        self.launch_sha = digest(self.launch_record)
        self.spec = json.loads(self.launch_record.read_text(encoding="utf-8-sig"))
        spec = self.spec
        require(spec.get("_templateOnly") is not True, "A launch record template is not execution evidence")
        require(spec.get("kind") == "qcopilots-search-ordinary-gui-sentinel-launch" and spec.get("head") == head,
                "Sentinel launch record is not explicitly bound to this candidate")
        require(spec.get("testMode") is False, "Sentinel launcher must explicitly attest ordinary non-test-mode startup")
        overrides = spec.get("environmentOverrides")
        require(isinstance(overrides, dict), "Sentinel launcher must record relevant environment overrides")
        normalized_overrides = {key.upper(): value for key, value in overrides.items()}
        require(not any((key.startswith("QCOPILOTS_SEARCH_TEST") or key == "QCOPILOTS_SEARCH_KEEP_TEST_ARTIFACTS")
                        and value not in (None, "") for key, value in normalized_overrides.items()), "Sentinel launch used test-specific environment")
        require(str(normalized_overrides.get("QT_QPA_PLATFORM") or "").lower() not in {"offscreen", "minimal"}, "Sentinel must be a visible ordinary Windows GUI")
        self.profile = Path(spec["profileDir"]).resolve()
        self.model_dir = Path(spec["modelDir"]).resolve()
        self.app_dir = Path(spec["applicationDirectory"]).resolve()
        self.runtime_mode = spec.get("runtimeMode", "packaged")
        require(self.runtime_mode in {"packaged", "sdk"}, "Sentinel runtimeMode must be packaged or sdk")
        self.qt_bin = self.app_dir if self.runtime_mode == "packaged" else Path(qt_prefix).resolve() / "bin"
        self.runtime_hashes = {str(self.qt_bin / name): digest(self.qt_bin / name) for name in ("Qt6Core.dll", "Qt6Network.dll")}
        require(self.profile.is_relative_to(self.launch_record.parent) and self.profile != self.launch_record.parent,
                "Dedicated sentinel profile must be contained by the directory holding its launch record")
        require(self.profile.is_dir() and self.model_dir.is_dir(), "Prepared sentinel paths do not exist")
        self.programs = {"gui": self.app_dir / "qcs-gui.exe", "service": self.app_dir / "qcs-service.exe",
                         "cli": self.app_dir / "qcs-cli.exe"}
        self.binary_hashes = {name: digest(path) for name, path in self.programs.items()}
        for name, product in (("gui", "qcs-gui"), ("service", "qcs-service"), ("cli", "qcs-cli")):
            require(self.binary_hashes[name] == producer_hashes[product], "Sentinel executable differs from the frozen producer: " + name)
        self.ids = {name: int(spec["processes"][name]["pid"]) for name in ("gui", "service")}
        require(min(self.ids.values()) > 0 and len(set(self.ids.values())) == 2, "Expected two distinct already running sentinel processes")
        self.fixture_paths = []
        for item in spec["fixtureFiles"]:
            path = Path(item["path"]).resolve()
            require(path.is_relative_to(self.launch_record.parent) and path.is_file(), "Sentinel fixture escaped its dedicated root")
            require(digest(path) == item["sha256"], "Sentinel source fixture changed since launch")
            self.fixture_paths.append(path)
        require(bool(self.fixture_paths), "At least one real sentinel source fixture is required")
        self.reports.mkdir(exist_ok=False)
        shutil.copyfile(self.launch_record, self.reports / "launch-record.json")
        shutil.copyfile(__file__, self.reports / "observer.py")
        self.script = self.reports / "observe-existing-processes.ps1"
        self.script.write_text(PROCESS_SCRIPT, encoding="utf-8")
        self.pwsh = shutil.which("pwsh")
        require(bool(self.pwsh), "PowerShell is required for read-only process observations")
        self.env = dict(os.environ)
        for key in list(self.env):
            if key.startswith(("QT_", "QCOPILOTS_SEARCH_", "QML")):
                self.env.pop(key, None)
        system = windows_root(self.env)
        self.env["PATH"] = os.pathsep.join(map(str, (self.app_dir, self.qt_bin, system / "System32", system)))
        self.before = None
        self.samples = []
        self.started = False
        self.handles = {}
        self.k = ctypes.WinDLL("kernel32", use_last_error=True)
        self.k.OpenProcess.argtypes, self.k.OpenProcess.restype = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE
        self.k.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        self.k.GetProcessTimes.restype = wintypes.BOOL
        self.k.CloseHandle.argtypes, self.k.CloseHandle.restype = [wintypes.HANDLE], wintypes.BOOL
        self.k.WaitForSingleObject.argtypes, self.k.WaitForSingleObject.restype = [wintypes.HANDLE, wintypes.DWORD], wintypes.DWORD
        try:
            for name, pid in self.ids.items():
                handle = self.k.OpenProcess(0x00101000, False, pid)  # SYNCHRONIZE | QUERY_LIMITED_INFORMATION
                require(bool(handle), "Cannot retain existing sentinel process identity")
                self.handles[name] = handle
        except BaseException:
            for handle in self.handles.values():
                self.k.CloseHandle(handle)
            raise

    def creation_time(self, pid):
        handle = self.k.OpenProcess(0x1000, False, pid)
        require(bool(handle), "Sentinel process vanished")
        try:
            values = [wintypes.FILETIME() for _ in range(4)]
            require(self.k.GetProcessTimes(handle, *[ctypes.byref(value) for value in values]), "Cannot read sentinel process creation time")
            return str((int(values[0].dwHighDateTime) << 32) | int(values[0].dwLowDateTime))
        finally:
            self.k.CloseHandle(handle)

    def process_state(self, phase):
        result = subprocess.run([self.pwsh, "-NoProfile", "-File", str(self.script), "-GuiId", str(self.ids["gui"]),
                                 "-ServiceId", str(self.ids["service"])], cwd=self.reports, capture_output=True, timeout=20)
        (self.reports / (phase + "-process.stdout.json")).write_bytes(result.stdout)
        (self.reports / (phase + "-process.stderr.log")).write_bytes(result.stderr)
        require(result.returncode == 0, "Cannot observe existing sentinel processes")
        rows = {int(row["pid"]): row for row in json.loads(result.stdout.decode("utf-8-sig"))}
        shell = ctypes.WinDLL("shell32", use_last_error=True)
        shell.CommandLineToArgvW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
        shell.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)
        self.k.LocalFree.argtypes, self.k.LocalFree.restype = [ctypes.c_void_p], ctypes.c_void_p
        for name, pid in self.ids.items():
            row = rows[pid]
            require(Path(row["imagePath"]).resolve() == self.programs[name].resolve()
                    and row["creationTime100ns"] == str(self.spec["processes"][name]["creationTime100ns"]),
                    "Sentinel process identity changed: " + name)
            count = ctypes.c_int()
            argv = shell.CommandLineToArgvW(row["commandLine"], ctypes.byref(count))
            require(bool(argv), "Cannot parse observed sentinel command line")
            try:
                arguments = [argv[i] for i in range(count.value)]
            finally:
                self.k.LocalFree(argv)
            require(arguments.count("--profile-dir") == 1
                    and arguments.index("--profile-dir") + 1 < len(arguments)
                    and Path(arguments[arguments.index("--profile-dir") + 1]).resolve() == self.profile,
                    "Sentinel process is not using its explicitly isolated profile")
            require("--test-mode" not in arguments, "Observed sentinel command line selects test mode")
            require(arguments.count("--model-dir") == 1 and arguments.index("--model-dir") + 1 < len(arguments)
                    and Path(arguments[arguments.index("--model-dir") + 1]).resolve() == self.model_dir,
                    "Sentinel must explicitly use the launch record's model directory")
        require(int(rows[self.ids["gui"]]["mainWindowHandle"]) != 0 and rows[self.ids["gui"]]["responding"] is True,
                "The ordinary GUI has no responding top-level window")
        return rows

    def status(self, phase):
        command = [str(self.programs["cli"]), "--profile-dir", str(self.profile), "--model-dir", str(self.model_dir),
                   "--no-start", "--timeout", "10000", "status"]
        result = subprocess.run(command, cwd=self.reports, env=self.env, capture_output=True, timeout=20)
        (self.reports / (phase + "-status.stdout.json")).write_bytes(result.stdout)
        (self.reports / (phase + "-status.stderr.log")).write_bytes(result.stderr)
        reply = json.loads(result.stdout.decode("utf-8-sig"))
        require(result.returncode == 0 and reply.get("ok") is True, "Existing sentinel service did not answer its read-only status request")
        status = reply["result"]
        repository = status["repository"]
        require(status["settings"]["indexingPaused"] is True and status["progress"]["state"] == "paused",
                "Sentinel must be paused by its owner before observation")
        require(all(status[name] == 0 for name in ("queuedFiles", "queuedQueries", "activeSearches"))
                and not status["failures"] and not status["warning"] and not status["pipelineTransitionError"] and not status["watcherWarning"]
                and not repository["recoveryWarning"] and not repository["maintenanceWarning"],
                "Sentinel is not in a healthy idle state")
        require(repository["files"] >= 1 and repository["segments"] >= 1
                and Path(repository["databasePath"]).resolve() == self.profile / "index/qcs.sqlite3",
                "Sentinel must already contain its dedicated indexed fixture")
        require(status["pipelineFingerprint"] == repository["pipelineFingerprint"], "Sentinel pipeline identity is inconsistent")
        roots = [Path(path).resolve() for path in status["settings"]["roots"]]
        excludes = [Path(path).resolve() for path in status["settings"]["excludes"]]
        require(all(any(path.is_relative_to(root) for root in roots)
                    and not any(path.is_relative_to(excluded) for excluded in excludes) for path in self.fixture_paths),
                "Sentinel fixtures are outside its indexed folder rules")
        fields = ("generation", "annGeneration", "files", "segments", "vectors", "annVectors", "pendingShards",
                  "pipelineFingerprint", "databasePath", "schemaVersion")
        return {"settings": status["settings"], "repository": {key: repository[key] for key in fields},
                "pipelineFingerprint": status["pipelineFingerprint"], "modelAvailable": status["modelAvailable"]}

    def data_snapshot(self):
        # -shm is SQLite reader coordination, so ordinary read-only status may
        # legally update it. Logs, IPC auth/locks and preview cache are not data.
        files = [self.profile / "qcs-settings.json", *self.fixture_paths]
        files += sorted(path for path in (self.profile / "index").rglob("*")
                        if path.is_file() and not path.name.endswith(("-shm", ".lock"))
                        and path.name != "qcs-progress.json")
        values = {}
        for path in files:
            first = path.stat()
            value = digest(path)
            last = path.stat()
            require((first.st_size, first.st_mtime_ns) == (last.st_size, last.st_mtime_ns), "Sentinel data changed during hashing")
            values[str(path)] = {"bytes": last.st_size, "sha256": value}
        return values

    def observe(self, phase):
        return {"observedAt": datetime.now(timezone.utc).isoformat(), "processes": self.process_state(phase),
                "status": self.status(phase), "dataFiles": self.data_snapshot()}

    def begin(self):
        first = self.observe("stability-first")
        write(self.reports / "stability-first.json", first)
        time.sleep(3)
        second = self.observe("before-tests")
        write(self.reports / "before-tests.json", second)
        require(first["status"] == second["status"] and first["dataFiles"] == second["dataFiles"],
                "Sentinel still has in-flight work after pause. Owner must stabilize it before starting tests")
        self.before, self.started = second, True

    def sample(self, processes, concurrent_pids):
        row = {"observedAt": datetime.now(timezone.utc).isoformat(), "monotonicNanoseconds": time.perf_counter_ns(),
               "concurrentQtTestPids": concurrent_pids, "processes": {}}
        for name, pid in self.ids.items():
            require(self.k.WaitForSingleObject(self.handles[name], 0) == 258, "An original sentinel process handle signaled exit")
            image = processes.image(pid)
            created = self.creation_time(pid)
            require(image and Path(image).resolve() == self.programs[name].resolve()
                    and created == str(self.spec["processes"][name]["creationTime100ns"]), "Sentinel process stopped or restarted while tests ran")
            row["processes"][name] = {"pid": pid, "imagePath": image, "creationTime100ns": created}
        self.samples.append(row)

    def finish(self):
        result = {"kind": "ordinary-gui-sentinel-observation", "passed": False, "started": self.started,
                  "launchRecordSha256": self.launch_sha, "observationSamples": self.samples,
                  "runtimeMode": self.runtime_mode, "cliRuntimeDirectory": str(self.qt_bin), "cliRuntimeHashes": self.runtime_hashes,
                  "scope": "Existing ordinary GUI/service sampled liveness and stable dedicated data before/after concurrent subset tests. No uninterrupted GUI responsiveness, GUI interaction, absence of transient IPC connection, or absence of transient reverted file writes is claimed.",
                  "ordinaryModeEvidence": "Launcher-captured environment attestation plus actual production binary, command line, process creation identity and responding top-level window observations. Environment is not independently reread from a running process."}
        try:
            require(self.started and bool(self.samples), "Sentinel was not observed throughout an actual test interval")
            require(any(all(sample["concurrentQtTestPids"]) for sample in self.samples), "No sentinel sample overlaps both actual checkout test processes")
            result["maximumSampleIntervalMilliseconds"] = max(
                ((right["monotonicNanoseconds"] - left["monotonicNanoseconds"]) / 1e6
                 for left, right in zip(self.samples, self.samples[1:])), default=0)
            after = self.observe("after-tests")
            write(self.reports / "after-tests.json", after)
            result["statusUnchanged"] = self.before["status"] == after["status"]
            result["dataBytesUnchanged"] = self.before["dataFiles"] == after["dataFiles"]
            result["inputsUnchanged"] = (digest(self.launch_record) == self.launch_sha
                                         and all(digest(path) == self.binary_hashes[name] for name, path in self.programs.items())
                                         and all(digest(Path(path)) == value for path, value in self.runtime_hashes.items()))
            result["passed"] = all(result[key] for key in ("statusUnchanged", "dataBytesUnchanged", "inputsUnchanged"))
        except Exception as error:
            result["error"] = type(error).__name__ + ": " + str(error)
        finally:
            for handle in self.handles.values():
                self.k.CloseHandle(handle)
            self.handles.clear()
        write(self.reports / "sentinel-observation.json", result)
        return result
