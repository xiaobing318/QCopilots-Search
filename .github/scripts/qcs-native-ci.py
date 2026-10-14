#!/usr/bin/env python3
"""Native CI orchestration. No generated files belong in the source checkout."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")

SOURCE = Path(__file__).resolve().parents[2]
PRODUCT = "qcs"
sys.dont_write_bytecode = True


def test_profile():
    profile = os.environ.get("QCOPILOTS_SEARCH_TEST_PROFILE", "fast")
    if profile not in ("fast", "full"):
        raise ValueError(f"Unknown CI test profile: {profile!r}")
    return profile


def release_evidence():
    spec = importlib.util.spec_from_file_location("qcopilots_search_release_evidence",
                                                SOURCE / "tools/qcs-release-evidence.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def artifact_selection():
    spec = importlib.util.spec_from_file_location("qcs_artifact_selection",
                                                SOURCE / ".github/scripts/qcs-artifact-selection.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def developer_workflow():
    """Share the developer tool's ownership contract before creating outputs."""
    spec = importlib.util.spec_from_file_location("qcopilots_search_dev",
                                                SOURCE / "tools/qcs-dev.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def candidate_head():
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    event = json.loads(Path(event_path).read_text(encoding="utf-8")) if event_path else {}
    return event.get("pull_request", {}).get("head", {}).get("sha") or os.environ.get("GITHUB_SHA")


def initialize_runner():
    """Use an exclusively created short root, retaining full CI identity in JSON."""
    profile = test_profile()
    required = ("RUNNER_TEMP", "GITHUB_ENV", "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT",
                "QCOPILOTS_SEARCH_CI_ID")
    for name in required:
        if not os.environ.get(name):
            raise RuntimeError(f"Required CI variable is missing: {name}")
    temporary = environment_path("RUNNER_TEMP")
    if "\n" in str(temporary) or "\r" in str(temporary):
        raise RuntimeError("Runner directories may not contain line breaks")
    if not temporary.is_dir():
        raise RuntimeError(f"Runner temporary directory is absent: {temporary}")
    head = candidate_head()
    if not isinstance(head, str) or not re.fullmatch(r"[0-9a-f]{40}", head):
        raise RuntimeError("Candidate head must be a complete Git commit SHA")
    declared = os.environ.get("QCOPILOTS_SEARCH_CANDIDATE_HEAD")
    if declared is not None and declared != head:
        raise RuntimeError("Workflow candidate differs from event candidate")
    root = Path(tempfile.mkdtemp(prefix="qcs-", dir=temporary)).resolve()
    developer_workflow().initialize_root(root)
    directories = {"QCOPILOTS_SEARCH_BUILD_ROOT": root,
                   "QCOPILOTS_SEARCH_QT_ROOT": root / "sdk",
                   "QCOPILOTS_SEARCH_MODEL_DIR": root / "models",
                   "QCOPILOTS_SEARCH_TEST_ROOT": root / "runs"}
    for directory in directories.values():
        directory.mkdir(exist_ok=True)
    report = root / "reports"
    report.mkdir()
    (report / "runner-identity.json").write_text(json.dumps({
        "schemaVersion": 1, "candidateHead": head, "testProfile": profile,
        "workflowCommit": os.environ.get("GITHUB_SHA"),
        "runId": os.environ["GITHUB_RUN_ID"], "runAttempt": os.environ["GITHUB_RUN_ATTEMPT"],
        "platformId": os.environ["QCOPILOTS_SEARCH_CI_ID"], "source": str(SOURCE),
        "directories": {name: str(path) for name, path in directories.items()}},
        ensure_ascii=False, indent=2), encoding="utf-8")
    with Path(os.environ["GITHUB_ENV"]).open("a", encoding="utf-8") as output:
        for name, path in directories.items():
            print(f"{name}={path}", file=output)
    return root


def build():
    evidence = release_evidence()
    identity = evidence.source_identity(SOURCE, candidate_head())
    errors = evidence.valid_identity(identity)
    if identity.get("checkoutCommit") != identity.get("candidateHead"):
        errors.append("Checkout must be the exact candidate head, not a merge or stale commit")
    if errors:
        raise RuntimeError("Native build requires clean immutable source: " + ", ".join(errors))
    started = datetime.now(timezone.utc).isoformat()
    evidence.write_json(REPORTS / "build-begin.json", {"identity": identity, "startedAt": started, "action": "build"})
    dev("build")
    if evidence.source_identity(SOURCE, candidate_head()) != identity:
        raise RuntimeError("Source changed during native build")
    programs = {name: {"sha256": evidence.digest(find_program(name, installed=False))}
                for name in sorted(evidence.APPLICATIONS | {PRODUCT + "-benchmark"})}
    evidence.write_json(REPORTS / "build-provenance.json", {"identity": identity, "completed": True,
                        "startedAt": started, "finishedAt": datetime.now(timezone.utc).isoformat(), "programs": programs})


def environment_path(name: str) -> Path:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Required CI variable is missing: {name}")
    result = Path(value).resolve()
    if result == SOURCE or SOURCE in result.parents:
        raise RuntimeError(f"CI output may not be inside the checkout: {result}")
    return result


def run(command: list[str | Path], *, environment=None, display=False, capture=False):
    command = [str(part) for part in command]
    if display and platform.system() == "Linux":
        command = ["xvfb-run", "--auto-servernum", "--server-args=-screen 0 1600x1000x24", *command]
    print("+ " + subprocess.list2cmdline(command), flush=True)
    return subprocess.run(command, cwd=ROOT, env=environment, check=True,
                          text=True, encoding="utf-8", capture_output=capture)


def dev(action: str, *arguments: str, display=False):
    # The Windows wrapper establishes the installed MSVC environment. Other
    # systems use the same Python implementation through the shell wrapper.
    if os.name == "nt":
        command = ["pwsh", "-NoProfile", "-File", SOURCE / f"{PRODUCT}-dev.ps1"]
    else:
        command = ["bash", SOURCE / f"{PRODUCT}-dev.sh"]
    command += [action, "--build-root", ROOT, "--qt-root", QT,
                "--model-dir", MODELS, "--jobs", "3", *arguments]
    return run(command, display=display)


def find_program(name: str, *, installed: bool) -> Path:
    prefix = ROOT / ("stage" if installed else "release")
    suffix = ".exe" if os.name == "nt" else ""
    candidates = [prefix / "bin" / (name + suffix),
                  prefix / "bin/qcs-gui.app/Contents/MacOS" / name,
                  prefix / "qcs-gui.app/Contents/MacOS" / name]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise RuntimeError(f"Missing {'installed' if installed else 'built'} program: {name}")


def native_environment():
    result = os.environ.copy()
    kit = {"Windows": "msvc2022_64", "Darwin": "macos", "Linux": "gcc_64"}[platform.system()]
    qt_prefix = QT / "6.12.0" / kit
    if platform.system() == "Darwin" and not qt_prefix.exists():
        qt_prefix = QT / "6.12.0/clang_64"
    directories = [ROOT / "release/bin", ROOT / "release/lib", ROOT / "stage/bin", qt_prefix / "bin"]
    result["PATH"] = os.pathsep.join(str(item) for item in directories) + os.pathsep + result.get("PATH", "")
    if platform.system() == "Linux":
        result["LD_LIBRARY_PATH"] = os.pathsep.join([str(ROOT / "release/lib"), str(ROOT / "stage/lib"), str(qt_prefix / "lib"), result.get("LD_LIBRARY_PATH", "")])
    return result


def models():
    dev("models")


def capacity():
    run([find_program(PRODUCT + "-benchmark", installed=False),
         "--profile-dir", ROOT / "ci-capacity", "--files", "3000",
         "--segments-per-file", "10", "--queries", "10", "--timings", "20",
         "--report", REPORTS / "capacity-ci.json"], environment=native_environment())


def package_for_suffix(suffix: str):
    candidates = [path for path in (ROOT / "pkg").glob(PRODUCT + "-*" + suffix)
                  if "-source-materials-" not in path.name and "-symbols-" not in path.name]
    if len(candidates) != 1:
        raise RuntimeError(f"Expected one final {suffix} package, found {len(candidates)}")
    return candidates[0]


def installers():
    dev("installers", display=True)


def test_evidence(*, require_complete=False, profile="full"):
    inventory_name = "ctest-inventory.json"
    command = [sys.executable, SOURCE / ".github/scripts/qcs-ctest-evidence.py",
               "--junit", REPORTS / "tests/ctest.xml", "--platform", platform.system(),
               "--inventory", REPORTS / "tests" / inventory_name, "--test-profile", profile,
               "--output", REPORTS / "tests/qt-test-cases.json"]
    if require_complete:
        command.append("--require-selected-tests" if profile == "fast" else "--require-complete")
    run(command)
    return json.loads((REPORTS / "tests/qt-test-cases.json").read_text(encoding="utf-8"))


def collect_test_wrappers():
    """Archive this CTest invocation's bounded raw wrapper reports, never its data."""
    result = {"schemaVersion": 1, "kind": "qcs-test-wrapper-diagnostics",
              "scope": "Current CTest invocation only. Diagnostics do not replace test acceptance.",
              "files": [], "errors": [], "missingLabels": [], "bytes": 0, "collectionComplete": False}
    destination = REPORTS / "wrappers"
    directory_ids = {}

    def ordinary(path, *, directory):
        info = path.lstat()
        expected_type = stat.S_ISDIR if directory else stat.S_ISREG
        if (not expected_type(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & 0x400):
            raise RuntimeError(f"Not an ordinary {'directory' if directory else 'file'}: {path}")
        return info

    def remember(path):
        info = ordinary(path, directory=True)
        directory_ids[path] = (info.st_dev, info.st_ino)

    def unchanged():
        for path, identity in directory_ids.items():
            info = ordinary(path, directory=True)
            if (info.st_dev, info.st_ino) != identity:
                raise RuntimeError(f"Diagnostic directory identity changed: {path}")

    def bounded_read(path, limit):
        unchanged()
        before = ordinary(path, directory=False)
        if before.st_size > limit:
            raise RuntimeError(f"Diagnostic exceeds {limit}-byte limit: {path}")
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                raise RuntimeError(f"Diagnostic file identity changed: {path}")
            raw = stream.read(limit + 1)
        unchanged()
        after = ordinary(path, directory=False)
        if ((after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
                != (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                or len(raw) > limit):
            raise RuntimeError(f"Diagnostic file changed while reading: {path}")
        return raw

    try:
        remember(ROOT)
        remember(REPORTS)
        destination.mkdir(exist_ok=True)
        remember(destination)
        latest = json.loads(bounded_read(REPORTS / "latest-test-run.json", 65536))
        result["runId"] = latest["runId"]
        runs = ROOT / "runs"
        remember(runs)
        selected = Path(latest["testRoot"])
        if not selected.is_absolute():
            raise RuntimeError("Wrapper test root must be absolute")
        # Canonical system ancestors such as macOS /var are permitted, but the
        # selected run directory itself must never be a link or reparse point.
        ordinary(selected, directory=True)
        selected = selected.resolve()
        if selected.parent != runs or not selected.name.startswith("qcs-t-"):
            raise RuntimeError("Wrapper test root is outside this job's direct runs directory")
        remember(selected)
        report_directory = selected / "reports"
        remember(report_directory)
        result["testRoot"] = selected.relative_to(ROOT).as_posix()
        candidates = sorted(report_directory.glob("qcs-t-*.json"))
        if len(candidates) > 512:
            raise RuntimeError("Wrapper report count exceeds the 512-file collection limit")
        if not candidates:
            raise RuntimeError("No wrapper reports were produced by this test invocation")
        for path in candidates:
            source = path.relative_to(ROOT).as_posix()
            try:
                raw = bounded_read(path, 1024 * 1024)
                if result["bytes"] + len(raw) > 8 * 1024 * 1024:
                    raise RuntimeError("Wrapper reports exceed the 8 MiB collection limit")
                digest = hashlib.sha256(raw).hexdigest()
                target = destination / (digest + ".json")
                unchanged()
                if target.exists() or target.is_symlink():
                    if bounded_read(target, 1024 * 1024) != raw:
                        raise RuntimeError("Archived wrapper content differs from its hash identity")
                else:
                    with target.open("xb") as output:
                        output.write(raw)
                    unchanged()
                record = {"source": source, "path": target.relative_to(REPORTS).as_posix(),
                          "bytes": len(raw), "sha256": digest}
                result["files"].append(record)
                result["bytes"] += len(raw)
                # Keep bounded raw diagnostics even when interrupted writes left
                # invalid JSON, while making the failed validation explicit.
                document = json.loads(raw)
                if (not isinstance(document, dict) or document.get("schemaVersion") != 1
                        or document.get("kind") != "qcs-test-run"
                        or not isinstance(document.get("label"), str)
                        or not document["label"].startswith("qcs-tests-")
                        or document.get("runId") != path.stem
                        or Path(document.get("root", "")).resolve() != selected
                        or not isinstance(document.get("cleanup"), dict)):
                    raise RuntimeError("Wrapper report identity or schema does not match its source")
                record.update({"label": document["label"],
                    "runId": document["runId"], "exitCode": document.get("exitCode"),
                    "childExitCode": document.get("childExitCode"), "cleanup": document["cleanup"],
                    "error": document.get("error"), "reportError": document.get("reportError")})
            except (OSError, ValueError, TypeError, RuntimeError) as error:
                result["errors"].append({"source": source, "error": str(error)})
        junit = REPORTS / "tests/ctest.xml"
        if junit.parent.exists() or junit.parent.is_symlink():
            remember(junit.parent)
        if junit.is_file():
            executed = {case.get("name") for case in ET.fromstring(bounded_read(junit, 32 * 1024 * 1024)).iter("testcase")
                        if case.find("skipped") is None and case.get("status") != "notrun" and case.get("name")}
            collected = {record.get("label") for record in result["files"]}
            result["missingLabels"] = sorted(executed - collected)
            if result["missingLabels"]:
                result["errors"].append({"source": "tests/ctest.xml",
                    "error": "Executed tests have no archived wrapper JSON. See raw CTest output for report-write errors."})
        result["collectionComplete"] = not result["errors"]
    except (OSError, ValueError, TypeError, KeyError, RuntimeError, ET.ParseError) as error:
        result["errors"].append({"error": str(error)})
    try:
        # A best-effort index must survive failure before any test was launched.
        unchanged()
        ordinary(destination, directory=True)
        descriptor, temporary = tempfile.mkstemp(prefix=".qcs-", suffix=".json", dir=destination)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
            json.dump(result, output, ensure_ascii=False, indent=2)
            output.write("\n")
        unchanged()
        os.replace(temporary, destination / "index.json")
    except (OSError, RuntimeError) as error:
        result["collectionComplete"] = False
        result["errors"].append({"error": f"Could not write wrapper diagnostic index: {error}"})
    if result["errors"]:
        print("Wrapper diagnostic collection: " + json.dumps(result["errors"], ensure_ascii=False), file=sys.stderr)
    return result


def signature_evidence(stage: Path, *, system: str | None = None):
    """Inspect the installed app. codesign does not establish notarization."""
    result = {"status": "unknown", "scope": "staging",
              "verified": False, "developerIdSignatureVerified": False,
              "notarization": "not-assessed", "commands": []}
    if (system or platform.system()) != "Darwin":
        result["reason"] = "Native signing verification is not configured for this platform"
        return result
    target = stage / "qcs-gui.app"
    result["target"] = str(target)
    if not target.is_dir():
        result["reason"] = "The installed application bundle is absent"
        return result
    commands = [["/usr/bin/codesign", "--verify", "--deep", "--strict", "--verbose=2", str(target)],
                ["/usr/bin/codesign", "--display", "--verbose=4", str(target)]]
    for command in commands:
        try:
            completed = subprocess.run(command, check=False, capture_output=True,
                                       text=True, encoding="utf-8", errors="replace", timeout=60)
        except (OSError, subprocess.TimeoutExpired) as error:
            result["commands"].append({"command": command, "error": str(error)})
            result["reason"] = "codesign could not complete"
            return result
        result["commands"].append({"command": command, "exitCode": completed.returncode,
                                   "stdout": completed.stdout, "stderr": completed.stderr})
    verification, details = result["commands"]
    output = details["stdout"] + "\n" + details["stderr"]
    result["authorities"] = [line.partition("=")[2] for line in output.splitlines()
                             if line.startswith("Authority=")]
    if verification["exitCode"] != 0:
        combined = output + "\n" + verification["stdout"] + "\n" + verification["stderr"]
        result["status"] = "unsigned" if "is not signed at all" in combined else "invalid"
        result["reason"] = "The installed bundle did not pass codesign verification"
        return result
    result["verified"] = True
    if details["exitCode"] != 0:
        result["reason"] = "Signature verification succeeded but identity inspection failed"
    elif "Signature=adhoc" in output.splitlines():
        result["status"] = "ad-hoc"
    elif any(value.startswith("Developer ID Application:") for value in result["authorities"]):
        result["status"] = "developer-id"
        result["developerIdSignatureVerified"] = True
    elif result["authorities"]:
        result["status"] = "signed-other"
    else:
        result["reason"] = "codesign did not report a recognized signing identity"
    return result


def collect_crash_reports(directories: list[Path], destination: Path, started_at: float):
    """Collect bounded supplementary OS diagnostics for this run's product processes."""
    result = {"scope": "qcs processes since this build began", "files": [], "unavailable": []}
    total = 0
    for index, directory in enumerate(directories):
        try:
            candidates = sorted(directory.glob(PRODUCT + "*"))
            for path in candidates:
                if path.suffix.lower() not in (".ips", ".crash") or path.is_symlink() or not path.is_file():
                    continue
                metadata = path.stat()
                if metadata.st_mtime < started_at:
                    continue
                if metadata.st_size > 4 * 1024**2 or total + metadata.st_size > 16 * 1024**2 or len(result["files"]) >= 20:
                    result["unavailable"].append({"name": path.name, "reason": "Diagnostic exceeds the bounded collection budget"})
                    continue
                destination.mkdir(parents=True, exist_ok=True)
                target = destination / (str(index) + "-" + path.name)
                shutil.copyfile(path, target)
                total += metadata.st_size
                result["files"].append({"name": target.name, "bytes": target.stat().st_size})
        except OSError as error:
            result["unavailable"].append({"directory": str(directory), "reason": str(error)})
    result["bytes"] = total
    if not result["files"]:
        result["note"] = "No matching OS crash report was available. This does not prove that no crash occurred."
    return result


def gui_failure_diagnostics(junit: Path, destination: Path):
    """Replay only the known GUI crash, without replacing its original CTest result."""
    result = {"scope": "diagnostic replay, never acceptance evidence", "attempts": []}
    if platform.system() != "Darwin":
        result["reason"] = "LLDB replay is limited to macOS"
        return result
    try:
        failures = [case for case in ET.parse(junit).getroot().iter("testcase")
                    if case.get("name") == PRODUCT + "-tests-gui"
                    and (case.find("failure") is not None or case.find("error") is not None)]
        if not failures:
            result["reason"] = "The original GUI CTest did not report failure"
            return result
        program = find_program(PRODUCT + "-tests-gui", installed=False)
        destination.mkdir(parents=True, exist_ok=True)
        selections = [["imageAndMissingFileReleaseResources"],
                      ["pdfHitPageIsSelected", "imageAndMissingFileReleaseResources"]]
        for index, cases in enumerate(selections, 1):
            test_parent = ROOT / "runs"
            test_parent.mkdir(parents=True, exist_ok=True)
            test_root = Path(tempfile.mkdtemp(prefix=PRODUCT + "-lldb-", dir=test_parent))
            environment = native_environment()
            environment["QCOPILOTS_SEARCH_TEST_ROOT"] = str(test_root)
            environment["QCOPILOTS_SEARCH_KEEP_TEST_ARTIFACTS"] = "1"
            environment["QT_FORCE_STDERR_LOGGING"] = "1"
            command = ["/usr/bin/lldb", "--batch", "--no-lldbinit", "--one-line",
                       "settings set target.disable-aslr false", "--one-line", "settings set auto-confirm true",
                       "--one-line", "run", "--one-line", "thread backtrace all", "--one-line", "process kill",
                       "--one-line-on-crash", "thread backtrace all", "--one-line-on-crash", "process kill",
                       "--", str(program), *cases, "-o", "-,txt"]
            log = destination / f"attempt-{index}.log"
            attempt = {"command": command, "cases": cases, "testRoot": str(test_root),
                       "log": log.name, "timeoutSeconds": 45, "timedOut": False}
            result["attempts"].append(attempt)
            with log.open("wb") as output:
                process = subprocess.Popen(command, cwd=test_root, env=environment,
                                           stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
                try:
                    attempt["debuggerExitCode"] = process.wait(timeout=45)
                except subprocess.TimeoutExpired:
                    attempt["timedOut"] = True
                    # LLDB's inferior may have its own process group. Interrupt
                    # the debugger first so its queued process-kill command runs.
                    process.send_signal(signal.SIGINT)
                    try:
                        attempt["debuggerExitCode"] = process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        attempt["debuggerDidNotRespondToInterrupt"] = True
                finally:
                    # Last-resort cleanup covers this fresh debugger group only.
                    # It cannot prove termination of a separately grouped target.
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait(timeout=5)
            with log.open("rb") as output:
                text = output.read(4 * 1024**2).decode("utf-8", errors="replace")
            attempt["logBytes"] = log.stat().st_size
            attempt["crashObserved"] = bool(re.search(r"stop reason = (?:EXC_|signal SIG(?:BUS|SEGV|ABRT|ILL))", text))
            attempt["backtraceObserved"] = attempt["crashObserved"] and bool(re.search(r"frame #\d+:", text))
            launched = re.search(r"Process (\d+) launched:", text)
            attempt["inferiorCleanupConfirmed"] = bool(launched and re.search(
                r"Process " + launched.group(1) + r" exited with status", text))
            if not attempt["inferiorCleanupConfirmed"]:
                attempt["cleanupUnknown"] = "No matching inferior exit was observed. Do not start another replay."
            if attempt["backtraceObserved"] or not attempt["inferiorCleanupConfirmed"]:
                break
    except (OSError, ET.ParseError, RuntimeError, subprocess.SubprocessError) as error:
        result["error"] = str(error)
    return result


def bounded_linux_diagnostic(command, log: Path, environment, *, timeout=10, output_limit=512 * 1024):
    """Record an owned diagnostic process with bounded time and retained output."""
    result = {"command": [str(part) for part in command], "log": log.name,
              "timeoutSeconds": timeout, "outputLimitBytes": output_limit}
    try:
        with log.open("wb") as output:
            process = subprocess.Popen(result["command"], cwd=ROOT, env=environment,
                                       stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
            deadline = time.monotonic() + timeout
            while process.poll() is None:
                if time.monotonic() >= deadline or log.stat().st_size > output_limit:
                    result["stoppedAtLimit"] = True
                    break
                time.sleep(.05)
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            result["exitCode"] = process.wait(timeout=5)
        size = log.stat().st_size
        if size > output_limit:
            with log.open("r+b") as output:
                output.truncate(output_limit)
            result["outputTruncated"] = True
        result["observedBytes"] = size
        result["retainedBytes"] = log.stat().st_size
    except (OSError, subprocess.SubprocessError) as error:
        result["error"] = str(error)
    return result


def linux_failure_diagnostics(junit: Path, destination: Path):
    result = {"scope": "Linux loader diagnostics only, never acceptance evidence", "commands": []}
    if platform.system() != "Linux":
        result["reason"] = "ELF diagnostics are limited to Linux"
        return result
    try:
        if not any(case.find("failure") is not None or case.find("error") is not None
                   for case in ET.parse(junit).getroot().iter("testcase")):
            result["reason"] = "The original CTest report contains no failed program"
            return result
        destination.mkdir(parents=True, exist_ok=True)
        prefix = QT / "6.12.0/gcc_64"
        # Match the developer runner's child environment. In particular, retain
        # inherited LD_LIBRARY_PATH so the faulty loader search is not repaired
        # accidentally by the diagnostics themselves.
        environment = os.environ.copy()
        environment["QCOPILOTS_SEARCH_BUILD_ROOT"] = str(ROOT)
        environment["QCOPILOTS_SEARCH_QT_PREFIX"] = str(prefix)
        environment["QCOPILOTS_SEARCH_MODEL_DIR"] = str(MODELS)
        environment["PATH"] = os.pathsep.join(str(path) for path in (
            ROOT / "release/bin", ROOT / "release/lib", prefix / "bin", ROOT / "bootstrap/nsis")) + os.pathsep + environment.get("PATH", "")
        test_parent = ROOT / "runs"
        test_parent.mkdir(parents=True, exist_ok=True)
        test_root = Path(tempfile.mkdtemp(prefix=PRODUCT + "-loader-", dir=test_parent))
        environment["QCOPILOTS_SEARCH_TEST_ROOT"] = str(test_root)
        environment["QT_FORCE_STDERR_LOGGING"] = "1"
        result["environment"] = {name: environment.get(name) for name in (
            "PATH", "LD_LIBRARY_PATH", "QT_PLUGIN_PATH", "QT_QPA_PLATFORM", "QT_QPA_PLATFORM_PLUGIN_PATH",
            "QCOPILOTS_SEARCH_BUILD_ROOT", "QCOPILOTS_SEARCH_QT_PREFIX", "QCOPILOTS_SEARCH_TEST_ROOT")}
        program = find_program(PRODUCT + "-tests-gui", installed=False)
        targets = [program, prefix / "lib/libavutil.so.61", prefix / "plugins/platforms/libqxcb.so",
                   prefix / "lib/libQt6XcbQpa.so.6"]
        for name in ("ssl", "crypto"):
            targets.extend(sorted((prefix / "lib").glob(f"libQt6FFmpegStub-{name}.so*")))
        seen = set()
        for target in targets:
            if not target.is_file() or target.resolve() in seen:
                continue
            seen.add(target.resolve())
            for arguments in (["ldd", "-r", target], ["readelf", "--dynamic", "--version-info", target]):
                log = destination / f"{len(result['commands']) + 1:02d}-{arguments[0]}.log"
                result["commands"].append(bounded_linux_diagnostic(arguments, log, environment))
        environment["QT_DEBUG_PLUGINS"] = "1"
        command = ["xvfb-run", "--auto-servernum", "--server-args=-screen 0 1600x1000x24",
                   program, "-functions"]
        result["commands"].append(bounded_linux_diagnostic(command, destination / "qt-plugin-loader.log",
                                                           environment, timeout=20))
    except (OSError, ET.ParseError, RuntimeError) as error:
        result["error"] = str(error)
    return result


def linux_sdk_diagnostics(destination: Path):
    """Inspect the installed SDK before any application binary exists."""
    result = {"scope": "Linux SDK loader diagnostics only, never acceptance evidence", "commands": []}
    if platform.system() != "Linux":
        result["reason"] = "ELF diagnostics are limited to Linux"
        return result
    try:
        destination.mkdir(parents=True, exist_ok=True)
        environment = os.environ.copy()
        result["environment"] = {name: environment.get(name) for name in (
            "PATH", "LD_LIBRARY_PATH", "LD_PRELOAD", "LD_AUDIT", "QT_PLUGIN_PATH", "QT_QPA_PLATFORM",
            "QT_QPA_PLATFORM_PLUGIN_PATH", "QT_LOGGING_RULES", "QT_LOGGING_CONF")}
        prefix = QT / "6.12.0/gcc_64"
        for target in (prefix / "plugins/platforms/libqxcb.so", prefix / "lib/libQt6XcbQpa.so.6"):
            if not target.is_file():
                result.setdefault("missingTargets", []).append(str(target))
                continue
            for arguments in (["ldd", "-r", target], ["readelf", "--dynamic", "--version-info", target]):
                log = destination / f"{len(result['commands']) + 1:02d}-{arguments[0]}.log"
                result["commands"].append(bounded_linux_diagnostic(arguments, log, environment))
    except (OSError, RuntimeError) as error:
        result["error"] = str(error)
    return result


def doctor():
    try:
        dev("doctor")
    finally:
        # Loader diagnostics must neither hide an original doctor failure nor
        # turn a successful doctor invocation into an acceptance failure.
        if platform.system() == "Linux":
            try:
                diagnostics = linux_sdk_diagnostics(REPORTS / "linux-sdk-loader")
                (REPORTS / "linux-sdk-diagnostics.json").write_text(
                    json.dumps(diagnostics, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception as error:
                print(f"SDK loader diagnostics unavailable: {error}", file=sys.stderr)


def fast_selection(root: Path, architecture: str):
    """Recompute the exact fast scope from the preserved full CTest graph."""
    evidence = release_evidence()
    directory = root / "reports/tests"
    registered = evidence.read_json(directory / "ctest-inventory.json")
    selected = evidence.read_json(directory / "test-selected-inventory.json")
    selection = evidence.read_json(directory / "test-selection.json")
    policy = developer_workflow().helper_module("tools/qcs-test-policy.py", "qcs_native_policy")
    scope = policy.describe_inventory(registered, "fast")
    all_names, selected_names = set(scope["registeredPrograms"]), set(scope["selectedPrograms"])
    slow = set(scope["excludedPrograms"])
    actual_selected = policy.describe_inventory(selected, "full")["selectedPrograms"]
    if set(actual_selected) != selected_names:
        raise ValueError("Fast CTest selection differs from the registered slow-label policy")
    if evidence.read_json(directory / "test-inventory.json") != registered:
        raise ValueError("CTest registration changed between native and developer invocations")
    expected = {"profile": "fast", "kind": "ctest-selection", "status": "passed", "passed": True,
                "fullSuitePassed": False, "releaseComplete": False, "exitCode": 0,
                "filters": ["-LE", "^slow$"], "labelFilter": None,
                "counts": {"registered": len(all_names), "selected": len(selected_names), "excluded": len(slow)}}
    if (any(selection.get(key) != value for key, value in expected.items())
            or selection.get("passed") is not True or selection.get("releaseComplete") is not False
            or selection.get("fullSuitePassed") is not False or type(selection.get("exitCode")) is not int):
        raise ValueError("Fast test execution was unsuccessful or has an unexpected selection contract")
    for key, wanted in (("registeredTests", all_names), ("selectedTests", selected_names), ("excludedTests", slow)):
        observed = selection.get(key, [])
        if len(observed) != len(wanted) or set(observed) != wanted:
            raise ValueError(f"Fast selection {key} differs from the actual inventories")
    return selection


def inspect_fast(root: Path, *, expected_head: str, expected_run: str, expected_attempt: str):
    evidence = release_evidence()
    result = {"schemaVersion": 1, "kind": "native-fast-gate", "testProfile": "fast",
              "passed": False, "releaseComplete": False, "errors": []}
    errors = result["errors"]
    try:
        manifest = evidence.read_json(root / "reports/fast-evidence.json")
        errors.extend(evidence.verify_manifest(root, manifest))
        identity, platform_id = manifest["identity"], manifest["platformId"]
        result.update(identity=identity, platformId=platform_id)
        if (manifest.get("kind") != "native-fast" or platform_id not in evidence.PLATFORMS
                or identity.get("candidateHead") != expected_head or identity.get("checkoutCommit") != expected_head):
            errors.append("Fast manifest profile, platform or candidate identity is invalid")
        bound = {entry["path"] for entry in manifest["files"]}
        required = {"reports/" + name for name in ("fast-acceptance.json", "runner-identity.json",
            "build-provenance.json", "wrappers/index.json", "tests/ctest.xml", "tests/ctest-inventory.json",
            "tests/test-inventory.json", "tests/test-selected-inventory.json", "tests/test-selection.json")}
        if not required.issubset(bound):
            errors.append("Fast evidence omits required bound raw reports")
        runner = evidence.read_json(root / "reports/runner-identity.json")
        if (runner.get("candidateHead") != expected_head or runner.get("platformId") != platform_id
                or runner.get("runId") != expected_run or runner.get("runAttempt") != expected_attempt
                or runner.get("testProfile") != "fast"):
            errors.append("Fast runner identity differs from this run, attempt, platform or profile")
        build_record = evidence.read_json(root / "reports/build-provenance.json")
        if (build_record.get("identity") != identity or build_record.get("completed") is not True
                or not evidence.APPLICATIONS.issubset(build_record.get("programs", {}))):
            errors.append("Fast executable build provenance is incomplete")
        summary = evidence.read_json(root / "reports/fast-acceptance.json")
        if (summary.get("sourceIdentity") != identity or summary.get("testProfile") != "fast"
                or summary.get("releaseComplete") is not False or summary.get("kind") != "native-fast-acceptance"
                or summary.get("testCasesComplete") is not True or summary.get("testEvidenceErrors") != []
                or summary.get("wrapperCollectionComplete") is not True):
            errors.append("Fast acceptance identity or scope is invalid")
        system, architecture = evidence.PLATFORMS[platform_id]
        selection = fast_selection(root, architecture)
        spec = importlib.util.spec_from_file_location("qcs_fast_cases", SOURCE / ".github/scripts/qcs-ctest-evidence.py")
        parser = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(parser)
        cases = parser.inspect_junit(root / "reports/tests/ctest.xml", system,
                                     root / "reports/tests/ctest-inventory.json", "fast")
        errors.extend(cases["errors"])
        if cases.get("testsComplete") is not True or cases.get("releaseComplete") is not False:
            errors.append("Fast selected test cases are incomplete or falsely claim release completion")
        wrappers = evidence.read_json(root / "reports/wrappers/index.json")
        if wrappers.get("collectionComplete") is not True or wrappers.get("errors") or wrappers.get("missingLabels"):
            errors.append("Fast wrapper diagnostics are incomplete")
        expected_programs = set(selection["selectedTests"])
        if (len(wrappers.get("files", [])) != len(expected_programs)
                or {item.get("label") for item in wrappers.get("files", [])} != expected_programs):
            errors.append("Fast wrapper programs differ from the required selected tests")
        for item in wrappers.get("files", []):
            path = "reports/" + item["path"]
            if path not in bound or evidence.digest(evidence.safe_file(root, path)) != item["sha256"]:
                errors.append("Fast wrapper raw report is unbound or changed")
            if (type(item.get("exitCode")) is not int or item["exitCode"] != 0
                    or type(item.get("childExitCode")) is not int or item["childExitCode"] != 0
                    or item.get("error") or item.get("reportError")):
                errors.append("Fast wrapper child or cleanup did not complete successfully")
            raw = evidence.read_json(evidence.safe_file(root, path))
            if any(raw.get(key) != item.get(key) for key in ("label", "runId", "exitCode", "childExitCode", "cleanup", "error", "reportError")):
                errors.append("Fast wrapper index differs from its raw execution report")
    except (OSError, ValueError, KeyError, TypeError, AttributeError, ET.ParseError) as error:
        errors.append(f"Cannot inspect fast evidence: {error}")
    result["passed"] = not errors
    return result


def report_fast():
    evidence = release_evidence()
    wrappers = collect_test_wrappers()
    cases = test_evidence(profile="fast")
    identity = evidence.source_identity(SOURCE, candidate_head())
    document = {"schemaVersion": 1, "kind": "native-fast-acceptance", "testProfile": "fast",
                "sourceIdentity": identity, "releaseComplete": False,
                "testCasesComplete": cases["testsComplete"], "testEvidenceErrors": cases["errors"],
                "wrapperCollectionComplete": wrappers["collectionComplete"],
                "notRun": ["real-model", "semantic-quality", "physical-file-capacity", "vector-capacity",
                           "package", "installer", "source-bundle", "computer-use"],
                "scope": "Daily build and selected fast tests only. Full release acceptance remains required."}
    evidence.write_json(REPORTS / "fast-acceptance.json", document)
    files = [evidence.file_record(ROOT, path) for path in sorted(REPORTS.rglob("*"))
             if path.is_file() and path.name not in ("fast-evidence.json", "fast-gate.json")]
    evidence.write_json(REPORTS / "fast-evidence.json", {
        "schemaVersion": 1, "kind": "native-fast", "identity": identity,
        "platformId": os.environ["QCOPILOTS_SEARCH_CI_ID"], "files": files})


def aggregate_fast(artifacts: Path, output: Path):
    evidence = release_evidence()
    gates, errors, selection = {}, [], None
    try:
        selection = artifact_selection().load_selection(artifacts / "selection.json",
            expected_head=candidate_head(), expected_profile="fast", expected_run=os.environ["GITHUB_RUN_ID"])
        if selection["aggregateAttempt"] != int(os.environ["GITHUB_RUN_ATTEMPT"]):
            raise ValueError("Artifact selection belongs to another aggregate attempt")
        selected = selection["selected"]
        if set(selected) != set(evidence.PLATFORMS):
            raise ValueError("Artifact selection must identify exactly four native platforms")
        expected_roots = {artifacts / value["artifactName"] for value in selected.values()}
        observed_roots = {path.parent.parent for path in artifacts.glob("*/reports/fast-evidence.json")}
        if expected_roots != observed_roots:
            raise ValueError("Downloaded fast evidence differs from the exact selected artifact directories")
        for name, chosen in sorted(selected.items()):
            gate = inspect_fast(artifacts / chosen["artifactName"], expected_head=candidate_head(),
                expected_run=selection["runId"], expected_attempt=str(chosen["sourceAttempt"]))
            if gate.get("platformId") != name:
                gate["errors"].append("Selected artifact platform differs from its required slot")
                gate["passed"] = False
            gate["selection"] = chosen
            gates[name] = gate
            errors.extend(f"{name}: {message}" for message in gate["errors"])
    except (OSError, ValueError, KeyError, TypeError) as error:
        errors.append(f"Cannot bind fast evidence to its immutable artifact selection: {error}")
    if set(gates) != set(evidence.PLATFORMS):
        errors.append("Exactly four distinct fast native platform combinations are required")
    identities = [gate.get("identity") for gate in gates.values()]
    if identities and any(identity != identities[0] for identity in identities):
        errors.append("Fast executions disagree on immutable source identity")
    result = {"schemaVersion": 1, "kind": "native-fast-aggregate", "testProfile": "fast",
              "candidateHead": candidate_head(), "runId": os.environ["GITHUB_RUN_ID"],
              "runAttempt": os.environ["GITHUB_RUN_ATTEMPT"], "passed": not errors,
              "releaseComplete": False, "errors": errors, "gates": gates,
              "artifactSelection": selection}
    evidence.write_json(output, result)
    if errors:
        raise RuntimeError("Fast native aggregation failed: " + ", ".join(errors))


def report():
    # Run before any other report parser so failed or interrupted tests retain
    # their cleanup diagnosis even when subsequent acceptance evidence is absent.
    wrappers = collect_test_wrappers()
    case_evidence = test_evidence()
    signature = signature_evidence(ROOT / "stage")
    evidence = release_evidence()
    identity = evidence.source_identity(SOURCE, candidate_head())
    diagnostics = {"scope": "not collected on this operating system"}
    if platform.system() == "Darwin":
        begin_path = REPORTS / "build-begin.json"
        if begin_path.is_file():
            started = evidence.timestamp(evidence.read_json(begin_path)["startedAt"]).timestamp()
            diagnostics = collect_crash_reports([Path.home() / "Library/Logs/DiagnosticReports",
                                                 Path("/Library/Logs/DiagnosticReports")],
                                                REPORTS / "mac-crash-reports", started)
        else:
            diagnostics = {"scope": "not collected because this job never began a build"}
        evidence.write_json(REPORTS / "mac-crash-reports.json", diagnostics)
    document = {"schemaVersion": 1, "product": PRODUCT,
                "sourceIdentity": identity, "system": platform.system(),
                "commit": identity["checkoutCommit"],
                "workflowCommit": os.environ.get("GITHUB_SHA"),
                "runId": os.environ.get("GITHUB_RUN_ID"),
                "runAttempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
                "platform": platform.platform(), "architecture": platform.machine(),
                "expectedArchitecture": os.environ.get("QCOPILOTS_SEARCH_CI_ARCH"),
                "signature": signature["status"], "signatureEvidence": signature, "published": False,
                "crashDiagnostics": diagnostics,
                "testReportPresent": (REPORTS / "tests/ctest.xml").is_file(),
                "testWrapperDiagnostics": {"index": "wrappers/index.json",
                    "collectionComplete": wrappers["collectionComplete"],
                    "files": len(wrappers["files"]), "errors": wrappers["errors"]},
                "testCasesComplete": case_evidence["releaseComplete"],
                "testInventoryVerified": case_evidence["inventoryVerified"],
                "qtTestTotals": case_evidence["qtTestTotals"],
                "qtTestSkips": case_evidence["qtTestSkips"],
                "testEvidenceErrors": case_evidence["errors"],
                "modelsReportPresent": (REPORTS / "models.json").is_file(),
                "capacityReportPresent": (REPORTS / "capacity-ci.json").is_file(),
                "packageReportPresent": (REPORTS / "package/report.json").is_file(),
                "installerReportPresent": (REPORTS / ("package-nsis/report.json" if os.name == "nt" else "package-deb/report.json")).is_file() if platform.system() in ("Windows", "Linux") else None,
                "sourceManifestPresent": (ROOT / "pkg/qcs-source-manifest.json").is_file(),
                "sourceIntegrityReportPresent": (REPORTS / "source-integrity.json").is_file(),
                "capacityScope": "30,000 synthetic vectors exercising global ANN, not the local million-vector acceptance",
                "computerUse": "Requires a separate Windows desktop evidence report",
                "timestampUtc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    (REPORTS / "native-acceptance.json").write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(document, ensure_ascii=False, indent=2))
    platform_id = next(name for name, expected in evidence.PLATFORMS.items()
                       if expected == (platform.system(), os.environ.get("QCOPILOTS_SEARCH_CI_ARCH")))
    evidence.capture_native(ROOT, identity, platform_id)


def preflight():
    evidence = release_evidence()
    result = evidence.inspect_native(ROOT, expected_head=candidate_head())
    evidence.write_json(REPORTS / "native-gate.json", result)
    if not result["passed"]:
        raise RuntimeError("Native evidence gate failed: " + ", ".join(result["errors"]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["initialize", "bootstrap", "doctor", "configure", "build", "install", "models", "test", "capacity", "package", "installers", "sources", "report", "preflight", "aggregate-fast"])
    parser.add_argument("--artifacts-dir", type=Path)
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    action, profile = arguments.action, test_profile()
    if action == "aggregate-fast":
        if profile != "fast" or arguments.artifacts_dir is None or arguments.output is None:
            parser.error("aggregate-fast requires the fast profile, --artifacts-dir and --output")
        aggregate_fast(arguments.artifacts_dir.resolve(), arguments.output.resolve())
        return
    if arguments.artifacts_dir is not None or arguments.output is not None:
        parser.error("Artifact aggregate arguments are only valid for aggregate-fast")
    if profile == "fast" and action in ("install", "models", "capacity", "package", "installers", "sources"):
        raise RuntimeError(f"{action} requires QCOPILOTS_SEARCH_TEST_PROFILE=full")
    if action == "initialize":
        initialize_runner()
        return
    global ROOT, QT, MODELS, REPORTS
    ROOT = environment_path("QCOPILOTS_SEARCH_BUILD_ROOT")
    QT = environment_path("QCOPILOTS_SEARCH_QT_ROOT")
    MODELS = environment_path("QCOPILOTS_SEARCH_MODEL_DIR")
    REPORTS = ROOT / "reports"
    ROOT.mkdir(parents=True, exist_ok=True)
    REPORTS.mkdir(parents=True, exist_ok=True)
    expected = os.environ.get("QCOPILOTS_SEARCH_CI_ARCH")
    actual = platform.machine().lower()
    normalized = {"amd64": "x86_64", "aarch64": "arm64"}.get(actual, actual)
    if expected and normalized != expected:
        raise RuntimeError(f"Expected native architecture {expected}, got {actual}")
    if action == "configure":
        dev(action, "--cmake-arg=-DGGML_NATIVE=OFF", "--cmake-arg=-DGGML_CUDA=OFF", "--cmake-arg=-DGGML_METAL=OFF", "--cmake-arg=-DQCOPILOTS_SEARCH_REQUIRE_FULL_FEATURES=ON")
    elif action == "doctor":
        doctor()
    elif action in ("bootstrap", "install"):
        dev(action)
    elif action == "build":
        build()
    elif action == "models":
        models()
    elif action == "test":
        (REPORTS / "tests").mkdir(parents=True, exist_ok=True)
        inventory = run(["ctest", "--test-dir", ROOT / "release", "-C", "Release", "--show-only=json-v1"], capture=True)
        (REPORTS / "tests/ctest-inventory.json").write_text(inventory.stdout, encoding="utf-8")
        os.environ["QCOPILOTS_SEARCH_SEMANTIC_REPORT"] = str(REPORTS / "semantic-quality.json")
        os.environ["QCOPILOTS_SEARCH_PHYSICAL_FILE_REPORT"] = str(REPORTS / "physical-files-ci.json")
        try:
            dev(action, "--test-profile", profile, "--keep-failed-tests", "--report-dir", str(REPORTS / "tests"), display=True)
        except subprocess.CalledProcessError:
            diagnostics = gui_failure_diagnostics(REPORTS / "tests/ctest.xml", REPORTS / "mac-gui-debug")
            (REPORTS / "gui-failure-diagnostics.json").write_text(
                json.dumps(diagnostics, ensure_ascii=False, indent=2), encoding="utf-8")
            diagnostics = linux_failure_diagnostics(REPORTS / "tests/ctest.xml", REPORTS / "linux-loader")
            (REPORTS / "linux-loader-diagnostics.json").write_text(
                json.dumps(diagnostics, ensure_ascii=False, indent=2), encoding="utf-8")
            raise
        finally:
            wrappers = collect_test_wrappers()
        if not wrappers["collectionComplete"]:
            raise RuntimeError("Test wrapper diagnostic collection is incomplete. See reports/wrappers/index.json")
        test_evidence(require_complete=True, profile=profile)
    elif action == "capacity":
        capacity()
    elif action == "package":
        for generator in os.environ["QCOPILOTS_SEARCH_CI_GENERATORS"].split(","):
            dev("package", "--generator", generator)
        dev("verify-package", "--report-dir", str(REPORTS / "package"), display=True)
    elif action == "sources":
        dev("sources")
    elif action == "installers":
        installers()
    elif action == "report":
        report_fast() if profile == "fast" else report()
    elif action == "preflight":
        if profile == "full":
            preflight()
        else:
            gate = inspect_fast(ROOT, expected_head=candidate_head(), expected_run=os.environ["GITHUB_RUN_ID"],
                                expected_attempt=os.environ["GITHUB_RUN_ATTEMPT"])
            release_evidence().write_json(REPORTS / "fast-gate.json", gate)
            if not gate["passed"]:
                raise RuntimeError("Fast evidence gate failed: " + ", ".join(gate["errors"]))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"{PRODUCT}: {error}", file=sys.stderr)
        sys.exit(1)
