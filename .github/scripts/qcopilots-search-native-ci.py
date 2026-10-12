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
import shutil
import subprocess
import sys
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
         "--profile-dir", ROOT / "ci-capacity", "--files", "1000",
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


def report():
    case_evidence = test_evidence()
    signature = signature_evidence(ROOT / "stage")
    evidence = release_evidence()
    identity = evidence.source_identity(SOURCE, candidate_head())
    document = {"schemaVersion": 1, "product": PRODUCT,
                "sourceIdentity": identity, "system": platform.system(),
                "commit": os.environ.get("GITHUB_SHA"),
                "runId": os.environ.get("GITHUB_RUN_ID"),
                "runAttempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
                "platform": platform.platform(), "architecture": platform.machine(),
                "expectedArchitecture": os.environ.get("QCOPILOTS_SEARCH_CI_ARCH"),
                "signature": signature["status"], "signatureEvidence": signature, "published": False,
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
                "capacityScope": "10,000 synthetic vectors, not the local million-vector acceptance",
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
    elif action in ("bootstrap", "doctor", "install"):
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
        dev(action, "--keep-failed-tests", "--report-dir", str(REPORTS / "tests"), display=True)
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
