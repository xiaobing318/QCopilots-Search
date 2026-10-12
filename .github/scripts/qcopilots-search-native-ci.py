#!/usr/bin/env python3
"""Native CI orchestration. No generated files belong in the source checkout."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import platform
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")

SOURCE = Path(__file__).resolve().parents[2]
PRODUCT = "qcopilots-search"
sys.dont_write_bytecode = True


def release_evidence():
    spec = importlib.util.spec_from_file_location("qcopilots_search_release_evidence",
                                                SOURCE / "tools/qcopilots-search-release-evidence.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def candidate_head():
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    event = json.loads(Path(event_path).read_text(encoding="utf-8")) if event_path else {}
    return event.get("pull_request", {}).get("head", {}).get("sha") or os.environ.get("GITHUB_SHA")


def build():
    evidence = release_evidence()
    identity = evidence.source_identity(SOURCE, candidate_head())
    errors = evidence.valid_identity(identity)
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
                  prefix / "bin/qcopilots-search.app/Contents/MacOS" / name,
                  prefix / "qcopilots-search.app/Contents/MacOS" / name]
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
    MODELS.mkdir(parents=True, exist_ok=True)
    cli = find_program(PRODUCT + "-cli", installed=True)
    common = [cli, "--model-dir", MODELS, "--profile-dir", ROOT / "ci-model-profile"]
    manifest = run([*common, "models", "manifest"], environment=native_environment(), capture=True)
    (REPORTS / "models-manifest.json").write_text(manifest.stdout, encoding="utf-8")
    run([*common, "--timeout", "1800000", "--progress", "models", "download"], environment=native_environment())
    verified = run([*common, "models", "verify"], environment=native_environment(), capture=True)
    print(verified.stdout)
    (REPORTS / "models.json").write_text(verified.stdout, encoding="utf-8")


def capacity():
    run([find_program(PRODUCT + "-benchmark", installed=False),
         "--profile-dir", ROOT / "ci-capacity", "--files", "3000",
         "--segments-per-file", "10", "--queries", "10", "--timings", "20",
         "--report", REPORTS / "capacity-ci.json"], environment=native_environment())


def package_for_suffix(suffix: str):
    candidates = [path for path in (ROOT / "packages").glob(PRODUCT + "-*" + suffix)
                  if "-source-materials-" not in path.name and "-symbols-" not in path.name]
    if len(candidates) != 1:
        raise RuntimeError(f"Expected one final {suffix} package, found {len(candidates)}")
    return candidates[0]


def installers():
    if os.name == "nt":
        dev("verify-package", "--package-file", str(package_for_suffix(".exe")),
            "--report-dir", str(REPORTS / "package-nsis"), display=True)
    elif platform.system() == "Linux":
        run([sys.executable, SOURCE / ".github/scripts/qcopilots-search-deb-ci.py",
             "--build-root", ROOT, "--package-file", package_for_suffix(".deb"),
             "--report-dir", REPORTS / "package-deb"])
    else:
        raise RuntimeError("There is no additional installer acceptance for this platform")


def test_evidence(*, require_complete=False):
    command = [sys.executable, SOURCE / ".github/scripts/qcopilots-search-ctest-evidence.py",
               "--junit", REPORTS / "tests/ctest.xml", "--platform", platform.system(),
               "--inventory", REPORTS / "tests/ctest-inventory.json",
               "--output", REPORTS / "tests/qt-test-cases.json"]
    if require_complete:
        command.append("--require-complete")
    run(command)
    return json.loads((REPORTS / "tests/qt-test-cases.json").read_text(encoding="utf-8"))


def signature_evidence(stage: Path, *, system: str | None = None):
    """Inspect the installed app. codesign does not establish notarization."""
    result = {"status": "unknown", "scope": "staging",
              "verified": False, "developerIdSignatureVerified": False,
              "notarization": "not-assessed", "commands": []}
    if (system or platform.system()) != "Darwin":
        result["reason"] = "Native signing verification is not configured for this platform"
        return result
    target = stage / "qcopilots-search.app"
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
    result = {"scope": "qcopilots-search processes since this build began", "files": [], "unavailable": []}
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
            test_parent = ROOT / "test-runs"
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
        test_parent = ROOT / "test-runs"
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


def report():
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
                "commit": os.environ.get("GITHUB_SHA"),
                "runId": os.environ.get("GITHUB_RUN_ID"),
                "runAttempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
                "platform": platform.platform(), "architecture": platform.machine(),
                "expectedArchitecture": os.environ.get("QCOPILOTS_SEARCH_CI_ARCH"),
                "signature": signature["status"], "signatureEvidence": signature, "published": False,
                "crashDiagnostics": diagnostics,
                "testReportPresent": (REPORTS / "tests/ctest.xml").is_file(),
                "testCasesComplete": case_evidence["releaseComplete"],
                "testInventoryVerified": case_evidence["inventoryVerified"],
                "qtTestTotals": case_evidence["qtTestTotals"],
                "qtTestSkips": case_evidence["qtTestSkips"],
                "testEvidenceErrors": case_evidence["errors"],
                "modelsReportPresent": (REPORTS / "models.json").is_file(),
                "capacityReportPresent": (REPORTS / "capacity-ci.json").is_file(),
                "packageReportPresent": (REPORTS / "package/report.json").is_file(),
                "installerReportPresent": (REPORTS / ("package-nsis/report.json" if os.name == "nt" else "package-deb/report.json")).is_file() if platform.system() in ("Windows", "Linux") else None,
                "sourceManifestPresent": (ROOT / "packages/qcopilots-search-source-manifest.json").is_file(),
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
    parser.add_argument("action", choices=["bootstrap", "doctor", "configure", "build", "install", "models", "test", "capacity", "package", "installers", "sources", "report", "preflight"])
    action = parser.parse_args().action
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
        inventory = run(["ctest", "--test-dir", ROOT / "release", "--show-only=json-v1"], capture=True)
        (REPORTS / "tests/ctest-inventory.json").write_text(inventory.stdout, encoding="utf-8")
        os.environ["QCOPILOTS_SEARCH_SEMANTIC_REPORT"] = str(REPORTS / "semantic-quality.json")
        os.environ["QCOPILOTS_SEARCH_PHYSICAL_FILE_REPORT"] = str(REPORTS / "physical-files-ci.json")
        try:
            dev(action, "--keep-failed-tests", "--report-dir", str(REPORTS / "tests"), display=True)
        except subprocess.CalledProcessError:
            diagnostics = gui_failure_diagnostics(REPORTS / "tests/ctest.xml", REPORTS / "mac-gui-debug")
            (REPORTS / "gui-failure-diagnostics.json").write_text(
                json.dumps(diagnostics, ensure_ascii=False, indent=2), encoding="utf-8")
            diagnostics = linux_failure_diagnostics(REPORTS / "tests/ctest.xml", REPORTS / "linux-loader")
            (REPORTS / "linux-loader-diagnostics.json").write_text(
                json.dumps(diagnostics, ensure_ascii=False, indent=2), encoding="utf-8")
            raise
        test_evidence(require_complete=True)
    elif action == "capacity":
        capacity()
    elif action == "package":
        for generator in os.environ["QCOPILOTS_SEARCH_CI_GENERATORS"].split(","):
            dev("package", "--generator", generator)
        dev("verify-package", "--report-dir", str(REPORTS / "package"), display=True)
    elif action == "sources":
        run([sys.executable, SOURCE / "tools/qcopilots-search-source-bundle.py", "--build-root", ROOT,
             "--qt-root", QT, "--download"])
        archives = [ROOT / "source-cache/llama.cpp-b11461.tar.gz", ROOT / "dependencies/llama-b11461.tar.gz"]
        archive = next((path for path in archives if path.is_file()), None)
        if archive is None:
            raise RuntimeError("The collected llama source archive is unavailable for rebuild verification")
        run([sys.executable, SOURCE / "tests/runtime/source_integrity_test.py", "--archive", archive,
             "--report", REPORTS / "source-integrity.json"])
        integrity = json.loads((REPORTS / "source-integrity.json").read_text(encoding="utf-8"))
        if integrity.get("passed") is not True:
            raise RuntimeError("Required source-integrity cases failed or did not execute")
    elif action == "installers":
        installers()
    elif action == "report":
        report()
    elif action == "preflight":
        preflight()


if __name__ == "__main__":
    try:
        ROOT = environment_path("QCOPILOTS_SEARCH_BUILD_ROOT")
        QT = environment_path("QCOPILOTS_SEARCH_QT_ROOT")
        MODELS = environment_path("QCOPILOTS_SEARCH_MODEL_DIR")
        REPORTS = ROOT / "reports"
        main()
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"{PRODUCT}: {error}", file=sys.stderr)
        sys.exit(1)
