#!/usr/bin/env python3
"""Native CI orchestration. No generated files belong in the source checkout."""
from __future__ import annotations

import argparse
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

_selection_spec = importlib.util.spec_from_file_location(
    "qcs_native_artifact_selection", SOURCE / ".github/scripts/qcs-artifact-selection.py")
artifact_selection = importlib.util.module_from_spec(_selection_spec)
_selection_spec.loader.exec_module(artifact_selection)

# CMake 3.28's Ninja depfile binding loses dependencies when external object
# paths contain spaces. The official fix b11323f1e4 first ships in 3.31.
CONTAINER_CMAKE_VERSION = "3.31.10"
CONTAINER_CMAKE_WHEEL_SHA256 = "2f766bb46367e5e0559fa33184653754bce044583a06014dcaebf8e6dff8a1f1"


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

def build():
    """CI builds the exact event commit from a clean checkout."""
    head = subprocess.check_output(["git", "-C", str(SOURCE), "rev-parse", "HEAD"], text=True).strip()
    if head != candidate_head():
        raise RuntimeError("Checkout must match the workflow source commit")
    status = subprocess.check_output(["git", "-C", str(SOURCE), "status", "--porcelain"], text=True)
    if status.strip():
        raise RuntimeError("Native CI build requires a clean checkout")
    dev("build")

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

def container_toolchain():
    """Install one hash-locked CMake wheel only in the disposable CUDA job."""
    selected = variant()
    if (platform.system() != "Linux" or selected["arch"] != "x64"
            or selected["backend"] != "cuda" or not selected.get("container")
            or os.environ.get("GITHUB_ACTIONS") != "true"
            or os.environ.get("QCOPILOTS_SEARCH_CI_CONTAINER") != "1"
            or os.environ.get("QCOPILOTS_SEARCH_CI_CONTAINER_IMAGE") != selected["container"]):
        raise RuntimeError("Pinned CMake installation requires the selected GitHub Linux CUDA container")
    github_path = environment_path("GITHUB_PATH")
    bootstrap = ROOT / "bootstrap"
    bootstrap.mkdir(parents=True, exist_ok=True)
    target = bootstrap / ("cmake-" + CONTAINER_CMAKE_VERSION)
    if target.exists():
        raise RuntimeError(f"CMake installation destination must be fresh: {target}")
    requirements = bootstrap / "cmake-requirements.txt"
    requirements.write_text(
        f"cmake=={CONTAINER_CMAKE_VERSION} --hash=sha256:{CONTAINER_CMAKE_WHEEL_SHA256}\n",
        encoding="utf-8")
    run([sys.executable, "-m", "pip", "--isolated", "--disable-pip-version-check", "install",
         "--index-url", "https://pypi.org/simple", "--only-binary=:all:", "--require-hashes",
         "--no-deps", "--no-cache-dir", "--target", target, "--requirement", requirements])
    binary_directory = target / "cmake/data/bin"
    environment = os.environ.copy()
    environment["PATH"] = str(binary_directory) + os.pathsep + environment.get("PATH", "")
    records = []
    for name in ("cmake", "ctest", "cpack"):
        expected = binary_directory / name
        resolved = shutil.which(name, path=environment["PATH"])
        if (not expected.is_file() or expected.is_symlink() or not resolved
                or Path(resolved).resolve() != expected.resolve()):
            raise RuntimeError(f"{name} must resolve to the locked CMake wheel: {expected}")
        result = run([expected, "--version"], environment=environment, capture=True)
        first_line = result.stdout.splitlines()[0] if result.stdout else ""
        if first_line != f"{name} version {CONTAINER_CMAKE_VERSION}":
            raise RuntimeError(f"Unexpected {name} version: {first_line}")
        records.append({"program": name, "path": str(expected), "version": CONTAINER_CMAKE_VERSION})
    # Publish only after every tool resolves to this wheel and verifies its version.
    with github_path.open("a", encoding="utf-8") as output:
        print(binary_directory, file=output)
    print(json.dumps({"containerCMake": records, "wheelSha256": CONTAINER_CMAKE_WHEEL_SHA256}, indent=2))


def native_environment():
    result = os.environ.copy()
    kit = {"Windows": "msvc2022_64", "Darwin": "macos", "Linux": "gcc_64"}[platform.system()]
    qt_prefix = QT / "6.12.0" / kit
    if platform.system() == "Darwin" and not qt_prefix.exists():
        qt_prefix = QT / "6.12.0/clang_64"
    directories = [ROOT / "Release/bin", ROOT / "Release/lib", ROOT / "stage/Release/bin", qt_prefix / "bin"]
    result["PATH"] = os.pathsep.join(str(item) for item in directories) + os.pathsep + result.get("PATH", "")
    if platform.system() == "Linux":
        result["LD_LIBRARY_PATH"] = os.pathsep.join([str(ROOT / "Release/lib"), str(ROOT / "stage/Release/lib"), str(qt_prefix / "lib"), result.get("LD_LIBRARY_PATH", "")])
    return result

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
        runs = ROOT / "runs/Release"
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
            test_parent = ROOT / "runs/Release"
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
            ROOT / "Release/bin", ROOT / "Release/lib", prefix / "bin", ROOT / "bootstrap/nsis")) + os.pathsep + environment.get("PATH", "")
        test_parent = ROOT / "runs/Release"
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


def variants():
    return artifact_selection.variants()


def variant():
    identifier = os.environ.get("QCOPILOTS_SEARCH_CI_ID")
    if identifier not in variants():
        raise RuntimeError(f"Unknown CI build variant: {identifier!r}")
    return variants()[identifier]


def initialize_runner():
    required = ("RUNNER_TEMP", "GITHUB_ENV", "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT", "QCOPILOTS_SEARCH_CI_ID")
    for name in required:
        if not os.environ.get(name):
            raise RuntimeError(f"Required CI variable is missing: {name}")
    selected = variant()
    temporary = environment_path("RUNNER_TEMP")
    if not temporary.is_dir() or any(character in str(temporary) for character in "\r\n"):
        raise RuntimeError("Runner temporary directory is missing or invalid")
    head = candidate_head()
    if not isinstance(head, str) or not re.fullmatch(r"[0-9a-f]{40}", head):
        raise RuntimeError("Source commit must be a complete Git commit SHA")
    if os.environ.get("QCOPILOTS_SEARCH_CANDIDATE_HEAD", head) != head:
        raise RuntimeError("Workflow source differs from event commit")
    attempt = os.environ["GITHUB_RUN_ATTEMPT"]
    if not re.fullmatch(r"[1-9][0-9]*", attempt):
        raise RuntimeError("Run attempt must be a positive integer")
    parent = Path(tempfile.mkdtemp(prefix="qcs-", dir=temporary)).resolve()
    root = parent / selected["id"]
    developer_workflow().initialize_root(root)
    directories = {"QCOPILOTS_SEARCH_BUILD_ROOT": parent,
                   "QCOPILOTS_SEARCH_VARIANT_ROOT": root,
                   "QCOPILOTS_SEARCH_QT_ROOT": parent / "sdk",
                   # actions/cache includes its path in the cache version.
                   # Only immutable model files share a stable runner location.
                   "QCOPILOTS_SEARCH_MODEL_DIR": temporary / "qcs-model-cache" / "gemma",
                   "QCOPILOTS_SEARCH_TEST_ROOT": root / "runs/Release"}
    for directory in directories.values():
        directory.mkdir(parents=True, exist_ok=True)
    reports = root / "reports/Release"
    reports.mkdir(parents=True)
    identity = {"schemaVersion": 2, "candidateHead": head,
                "runId": os.environ["GITHUB_RUN_ID"], "runAttempt": attempt,
                "configuration": selected["id"], "source": str(SOURCE),
                "directories": {name: str(path) for name, path in directories.items()}}
    (reports / "runner-identity.json").write_text(json.dumps(identity, ensure_ascii=False, indent=2), encoding="utf-8")
    with Path(os.environ["GITHUB_ENV"]).open("a", encoding="utf-8") as output:
        for name, path in directories.items():
            print(f"{name}={path}", file=output)
    print(json.dumps(identity, ensure_ascii=False, indent=2))
    return parent


def dev(action: str, *arguments: str, display=False):
    if os.name == "nt":
        command = ["pwsh", "-NoProfile", "-File", SOURCE / "qcs-dev.ps1"]
    else:
        command = ["bash", SOURCE / "qcs-dev.sh"]
    command += [action, "--build-root", BUILD_PARENT, "--variant", variant()["id"],
                "--qt-root", QT, "--model-dir", MODELS, "--jobs", "3", *arguments]
    return run(command, display=display)


def find_program(name: str, *, installed: bool) -> Path:
    prefix = ROOT / ("stage/Release" if installed else "Release")
    suffix = ".exe" if os.name == "nt" else ""
    for candidate in (prefix / "bin" / (name + suffix),
                      prefix / "bin/qcs-gui.app/Contents/MacOS" / name,
                      prefix / "qcs-gui.app/Contents/MacOS" / name):
        if candidate.is_file():
            return candidate
    raise RuntimeError(f"Missing {'installed' if installed else 'built'} program: {name}")


def installers():
    dev("installers", display=True)


def test_evidence(*, require_complete=False):
    command = [sys.executable, SOURCE / ".github/scripts/qcs-ctest-evidence.py",
               "--junit", REPORTS / "tests/ctest.xml", "--platform", platform.system(),
               "--inventory", REPORTS / "tests/ctest-inventory.json",
               "--output", REPORTS / "tests/qt-test-cases.json"]
    if require_complete:
        command.append("--require-complete")
    run(command)
    return json.loads((REPORTS / "tests/qt-test-cases.json").read_text(encoding="utf-8"))


def package_files(directory: Path, selected: dict):
    version = artifact_selection.project_version(SOURCE)
    alternatives = artifact_selection.package_name_options(selected["id"], version)
    allowed = set().union(*alternatives)
    found = {}
    for path in sorted(directory.glob("qcs-*")):
        if not path.is_file() or path.is_symlink():
            raise RuntimeError(f"Not an ordinary package file: {path.name}")
        if path.name not in allowed:
            raise RuntimeError(f"Unexpected package output: {path.name}")
        if not 0 < path.stat().st_size <= artifact_selection.MAX_ASSET_BYTES:
            raise RuntimeError(f"Package must be nonempty and below 2 GiB: {path.name}")
        found[path.name] = path
    if set(found) not in alternatives:
        raise RuntimeError(f"Package formats differ: expected one of {[sorted(item) for item in alternatives]}, found {sorted(found)}")
    return [found[name] for name in sorted(found)]


def products():
    paths = package_files(ROOT / "pkg/Release", variant())
    if not 1 <= len(paths) <= 2:
        raise RuntimeError("Native variants must produce one or two user packages")
    if any(any(character in str(path) for character in "\r\n") for path in paths):
        raise RuntimeError("Package paths cannot contain newlines")
    records = []
    for path in paths:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        records.append({"name": path.name, "bytes": path.stat().st_size, "sha256": digest.hexdigest()})
    print(json.dumps({"configuration": variant()["id"], "packages": records}, indent=2))
    if os.environ.get("GITHUB_OUTPUT"):
        with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
            for index in range(2):
                path = paths[index] if index < len(paths) else None
                print(f"package_{index + 1}_name={path.name if path else ''}", file=output)
                print(f"package_{index + 1}_path={path if path else ''}", file=output)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as summary:
            print("| Package | Bytes | SHA-256 |\n|---|---:|---|", file=summary)
            for record in records:
                print(f"| {record['name']} | {record['bytes']} | `{record['sha256']}` |", file=summary)
    return paths


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["matrix", "initialize", "toolchain", "bootstrap", "doctor", "configure", "build",
                        "install", "models", "test", "package", "installers", "products"])
    args = parser.parse_args()
    if args.action == "matrix":
        rows = [{**item, "container": item.get("container", "")} for item in variants().values()]
        encoded = json.dumps({"include": rows}, separators=(",", ":"))
        print(encoded)
        if os.environ.get("GITHUB_OUTPUT"):
            with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
                print("matrix=" + encoded, file=output)
        return
    if args.action == "initialize":
        initialize_runner()
        return
    global BUILD_PARENT, ROOT, QT, MODELS, REPORTS
    BUILD_PARENT = environment_path("QCOPILOTS_SEARCH_BUILD_ROOT")
    selected = variant()
    ROOT = BUILD_PARENT / selected["id"]
    QT = environment_path("QCOPILOTS_SEARCH_QT_ROOT")
    MODELS = environment_path("QCOPILOTS_SEARCH_MODEL_DIR")
    REPORTS = ROOT / "reports/Release"
    REPORTS.mkdir(parents=True, exist_ok=True)
    expected = selected["arch"]
    actual = platform.machine().lower()
    normalized = {"amd64": "x64", "x86_64": "x64", "aarch64": "arm64"}.get(actual, actual)
    if normalized != expected:
        raise RuntimeError(f"Expected native architecture {expected}, got {actual}")
    action = args.action
    if action == "toolchain":
        container_toolchain()
    elif action == "configure":
        dev(action, "--cmake-arg=-DQCOPILOTS_SEARCH_REQUIRE_FULL_FEATURES=ON",
            "--cmake-arg=-DQCOPILOTS_SEARCH_CANDIDATE_COMMIT=" + candidate_head(),
            "--cmake-arg=-DQCOPILOTS_SEARCH_CANDIDATE_ATTEMPT=" + os.environ["GITHUB_RUN_ATTEMPT"])
    elif action == "doctor":
        doctor()
    elif action == "build":
        build()
    elif action == "test":
        (REPORTS / "tests").mkdir(parents=True, exist_ok=True)
        inventory = run(["ctest", "--test-dir", ROOT / "Release", "-C", "Release", "--show-only=json-v1"], capture=True)
        (REPORTS / "tests/ctest-inventory.json").write_text(inventory.stdout, encoding="utf-8")
        os.environ["QCOPILOTS_SEARCH_SEMANTIC_REPORT"] = str(REPORTS / "semantic-functional.json")
        try:
            dev(action, "--keep-failed-tests", "--report-dir", str(REPORTS / "tests"), display=True)
        except subprocess.CalledProcessError:
            print(json.dumps(gui_failure_diagnostics(REPORTS / "tests/ctest.xml", REPORTS / "mac-gui-debug"), ensure_ascii=False))
            print(json.dumps(linux_failure_diagnostics(REPORTS / "tests/ctest.xml", REPORTS / "linux-loader"), ensure_ascii=False))
            raise
        finally:
            wrappers = collect_test_wrappers()
        if not wrappers["collectionComplete"]:
            raise RuntimeError("Test wrapper diagnostic collection is incomplete")
        test_evidence(require_complete=True)
    elif action == "package":
        for generator in selected["generators"]:
            dev("package", "--generator", generator)
        dev("verify-package", "--report-dir", str(REPORTS / "package"), display=True)
    elif action == "installers":
        installers()
    elif action == "products":
        products()
    else:
        dev(action)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"{PRODUCT}: {error}", file=sys.stderr)
        sys.exit(1)
