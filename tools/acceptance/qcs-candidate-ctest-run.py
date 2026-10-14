#!/usr/bin/env python3
"""Prepare, or explicitly execute, a fresh full CTest candidate evidence run.

Planning is the default and does not create directories, import repository code,
or start processes. Pass --execute to run a frozen candidate. This runner never
builds or rebinds evidence. The native Windows runner retains all CPU baseline,
model, binary and individual test-case acceptance requirements.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

sys.dont_write_bytecode = True
GOLDENS = {"query", "query-bn", "query-ar", "document", "document-long",
           "screen-280", "photo-140", "frame-70", "speech"}
REFERENCE = "qcs-tests-reference-vectors"
SEMANTIC = "qcs-tests-semantic"
PRODUCERS = {"qcs-gui", "qcs-service", "qcs-worker", "qcs-cli"}


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for data in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(data)
    return value.hexdigest()


def write_json(path, value):
    # Parents are created only by the explicit execution branch.
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2,
                                    allow_nan=False) + "\n", encoding="utf-8")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def validate_full_selection(selection, names):
    require(isinstance(selection, dict) and selection.get("schemaVersion") == 1
            and selection.get("kind") == "ctest-selection", "Missing CTest selection evidence")
    require(not selection.get("labelFilter") and selection.get("filters") == [],
            "Candidate CTest requires the complete unfiltered inventory")
    require(selection.get("registeredTests") == names and selection.get("selectedTests") == names
            and selection.get("excludedTests") == []
            and selection.get("counts") == {"registered": len(names), "selected": len(names), "excluded": 0},
            "Full CTest selection differs from the frozen registered inventory")
    require(selection.get("passed") is True and selection.get("fullSuitePassed") is True
            and selection.get("status") == "passed" and selection.get("exitCode") == 0
            and selection.get("releaseComplete") is False,
            "Complete CTest inventory did not finish or incorrectly claims complete release acceptance")


def cache_directory(cache, key):
    """Use the configured native data location, including explicit overrides."""
    matches = re.findall(r"^" + re.escape(key) + r":[^=]+=(.+)$", cache, re.MULTILINE)
    require(len(matches) == 1, f"Build cache must identify one {key}")
    result = Path(matches[0].strip()).resolve()
    require(result.is_dir(), f"Configured data directory is unavailable: {key}={result}")
    return result


def inventory_binding(test, source, binaries, bound_inputs):
    """Bind both the isolation launcher and its real native or Python payload."""
    command = test.get("command", [])
    require(bool(command), "Registered test has no execution command: " + test["name"])
    original = list(command)
    launcher = source / "tools" / "qcs-test-runner.py"
    outer = Path(command[0]).resolve()
    wrapper = None
    launcher_positions = [index for index, value in enumerate(command[1:], start=1)
                          if Path(value).resolve() == launcher.resolve()]
    if launcher_positions:
        require(len(launcher_positions) == 1, "Multiple isolation launchers in test command")
        launcher_index = launcher_positions[0]
        require(outer.stem.lower().startswith("python") and outer.is_file(),
                "Invalid isolation launcher interpreter")
        require(all(flag in {"-B", "-u"} for flag in command[1:launcher_index]),
                "Unexpected isolation launcher interpreter flags")
        require("--" in command[launcher_index + 1:], "Isolation launcher has no payload separator")
        separator = command.index("--", launcher_index + 1)
        flags = command[launcher_index + 1:separator]
        require("--label" in flags and flags.index("--label") + 1 < len(flags)
                and flags[flags.index("--label") + 1] == test["name"],
                "Isolation launcher label does not match CTest name")
        for path in (outer, launcher):
            require(path.is_file(), f"Isolation launcher input is missing: {path}")
            bound_inputs[str(path)] = digest(path)
        wrapper = {"interpreter": str(outer), "script": str(launcher),
                   "scriptSha256": digest(launcher)}
        command = command[separator + 1:]
    require(bool(command), "Registered test payload is empty")
    executable = Path(command[0]).resolve()
    result = {"name": test["name"], "command": original, "payloadCommand": command}
    if wrapper:
        result["isolationLauncher"] = wrapper
    if executable.name.startswith("qcs"):
        require(str(executable) in binaries,
                "Native test command is outside the selected binary snapshot: " + test["name"])
        result.update(program=str(executable), **binaries[str(executable)])
    else:
        require(executable.stem.lower().startswith("python") and executable.is_file(),
                "Unexpected non-native CTest interpreter: " + test["name"])
        # The first argument is the script. Later .py arguments may be explicit
        # tool fixtures and must also be inside the selected source checkout.
        scripts = [Path(value).resolve() for value in command[1:] if value.lower().endswith(".py")]
        require(bool(scripts) and all(path.is_file() and path.is_relative_to(source) for path in scripts),
                "Python test script is outside the selected source checkout: " + test["name"])
        for path in (executable, *scripts):
            bound_inputs[str(path)] = digest(path)
        result.update(interpreter=str(executable), script=str(scripts[0]), scriptSha256=digest(scripts[0]))
    return result


def file_info(path):
    return {"bytes": path.stat().st_size, "sha256": digest(path)}


def binary_snapshot(directory):
    require(directory.is_dir(), f"Missing build binary directory: {directory}")
    result = {str(path.resolve()): file_info(path)
              for path in sorted(directory.rglob("*"))
              if path.is_file() and path.suffix.lower() in {".exe", ".dll"}}
    require(bool(result), "The Windows EXE/DLL binary snapshot is empty")
    return result


def load_module(path):
    spec = importlib.util.spec_from_file_location("qcopilots_candidate_evidence", path)
    require(spec is not None and spec.loader is not None, "Cannot import evidence verifier")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def archive_file(source, destination, root, archives, *, origin, classification="raw"):
    require(source.is_file(), f"Evidence file is missing: {source}")
    require(not destination.exists(), f"Refusing to replace archived evidence: {destination}")
    before = file_info(source)
    shutil.copyfile(source, destination)
    require(file_info(destination) == before and file_info(source) == before,
            f"Evidence changed during archival: {source}")
    item = {"path": destination.relative_to(root).as_posix(), **before,
            "classification": classification, "origin": origin}
    archives.append(item)
    return item


def archive_reference_output(junit, inventory_names, root, archives):
    """Keep decoded captured stdout separately, with the original JUnit authoritative."""
    cases = list(ET.parse(junit).getroot().iter("testcase"))
    expected = {name for name in inventory_names if name.startswith(REFERENCE)}
    require(REFERENCE in expected and REFERENCE + "-baseline" in expected,
            "Both Windows default and baseline CPU reference tests must be registered")
    summary = {"classification": "derived", "source": "raw/ctest.xml",
               "description": "Cosines parsed from the original CTest JUnit captured stdout",
               "programs": [], "errors": []}
    for name in sorted(expected):
        matches = [case for case in cases if case.get("name") == name]
        require(len(matches) == 1, f"Expected one JUnit reference test: {name}")
        case = matches[0]
        stdout_elements = list(case.findall("system-out"))
        require(len(stdout_elements) == 1, f"Expected one captured stdout section: {name}")
        output = stdout_elements[0].text or ""
        destination = root / "raw" / (name + ".stdout.log")
        # XML entity/newline decoding is explicit. No log lines are synthesized.
        destination.write_bytes(output.encode("utf-8"))
        archives.append({"path": destination.relative_to(root).as_posix(),
                         **file_info(destination), "classification": "captured-text",
                         "origin": {"path": "raw/ctest.xml", "testcase": name,
                                    "element": "system-out", "transform": "XML text decoding, then UTF-8"},
                         "note": "Original JUnit bytes are authoritative. This is not a new execution or directly captured child byte stream."})
        values = re.findall(r"(\S+) reference cosine ([0-9.]+)", output)
        valid = (len(values) == 9 and {key for key, _ in values} == GOLDENS
                 and all(math.isfinite(float(value)) and .999 <= float(value) <= 1.00001
                         for _, value in values))
        row = {"name": name, "stdout": destination.relative_to(root).as_posix(),
               "passed": valid, "measurements": [{"name": key, "cosine": float(value)}
                                                    for key, value in values]}
        summary["programs"].append(row)
        if not valid:
            summary["errors"].append(f"{name}: nine distinct required cosine measurements >= .999 are missing")
    summary["passed"] = not summary["errors"]
    summary_path = root / "derived" / "reference-summary.json"
    write_json(summary_path, summary)
    archives.append({"path": summary_path.relative_to(root).as_posix(), **file_info(summary_path),
                     "classification": "derived", "origin": "raw/ctest.xml and captured reference stdout"})
    return summary


def summarize_execution(root, record):
    """Build the package handoff from original captured bytes, never manual counts."""
    def capture(relative):
        path = root / relative
        require(path.is_file(), f"Completion evidence is missing: {relative}")
        return {"path": relative, **file_info(path)}

    summary = {"schemaVersion": 1, "kind": "local-full-ctest-summary",
               "passed": False, "identity": record.get("identity"),
               "startedAt": record.get("startedAt"), "finishedAt": record.get("finishedAt"),
               "registeredPrograms": record.get("ctestCount", 0), "passedPrograms": 0,
               "sourceAndBinaryChecks": {name: record.get(name, False) for name in
                   ("sourceUnchanged", "binariesUnchanged", "runnerUnchanged", "buildRecordUnchanged")},
               "rawEvidence": [], "capturedInputFiles": [], "errors": list(record.get("errors", []))}
    if record.get("passed") is not True:
        write_json(root / "summary.json", summary)
        return summary
    cases = list(ET.parse(root / "tests/ctest.xml").getroot().iter("testcase"))
    passed = [case for case in cases if not any(list(case.iter(tag)) for tag in ("failure", "error", "skipped"))]
    inventory = json.loads((root / "tests/ctest-inventory.json").read_text(encoding="utf-8"))
    expected = {item["name"] for item in inventory["tests"]}
    require(len(cases) == len(expected) == record["ctestCount"]
            and {case.get("name") for case in cases} == expected and len(passed) == len(cases),
            "Completion summary inventory differs from successful original CTest output")
    case_evidence = json.loads((root / "derived/qt-test-cases.json").read_text(encoding="utf-8"))
    require(case_evidence.get("releaseComplete") is True and not case_evidence.get("errors"),
            "Completion summary requires complete individual-case evidence")
    summary.update(passed=True, passedPrograms=len(passed),
                   caseEvidence=capture("derived/qt-test-cases.json"),
                   semantic={"rawReport": capture("semantic-functional.json")},
                   rawEvidence=[capture(path) for path in
                       ("tests/execution-record.json", "tests/ctest.xml", "tests/ctest-inventory.json",
                        "tests/command.stdout.log", "tests/command.stderr.log")],
                   capturedInputFiles=[dict(item) for item in record["archives"]],
                   qtTestTotals=case_evidence.get("qtTestTotals"),
                   qtTestSkips=case_evidence.get("qtTestSkips"),
                   pythonTestTotals=case_evidence.get("pythonTestTotals"),
                   pythonTestSkips=case_evidence.get("pythonTestSkips"))
    write_json(root / "summary.json", summary)
    return summary


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--head", required=True, help="Frozen full Git commit SHA")
    parser.add_argument("--source-tree", required=True, help="Frozen full Git tree SHA")
    parser.add_argument("--build-record", type=Path, required=True)
    parser.add_argument("--report-parent", type=Path, required=True)
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--build-root", type=Path, required=True)
    parser.add_argument("--variant", required=True, help="Explicit Windows build variant")
    parser.add_argument("--config", choices=("Debug", "Release", "RelWithDebInfo"), default="Release")
    parser.add_argument("--model-dir", type=Path, help="Defaults to BUILD_ROOT/models")
    parser.add_argument("--qt-root", type=Path, required=True)
    parser.add_argument("--qt-prefix", type=Path, required=True)
    parser.add_argument("--launch-cwd", type=Path, required=True)
    parser.add_argument("--expected-tests", type=int, required=True, help="Frozen full inventory count, including acceptance runner tests")
    parser.add_argument("--jobs", type=int, default=2)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    for key in ("head", "source_tree"):
        require(re.fullmatch(r"[0-9a-f]{40}", getattr(args, key)) is not None,
                f"--{key.replace('_', '-')} must be a full lower-case Git SHA")
    require(args.expected_tests > 0 and args.jobs > 0, "Expected test count and jobs must be positive")
    variants = json.loads((Path(__file__).resolve().parents[2] / "config/build-variants.json").read_text(encoding="utf-8"))
    supported = {item["id"] for item in variants["variants"] if item["os"] == "windows"}
    require(args.variant in supported, "The candidate runner requires a supported Windows variant")
    for name in ("source", "build_root", "build_record", "report_parent", "qt_root", "qt_prefix", "launch_cwd"):
        setattr(args, name, getattr(args, name).expanduser().resolve())
    args.model_dir = (args.model_dir or args.build_root / "models").expanduser().resolve()
    args.build_dir = args.build_root / args.variant / args.config
    return args


def execute(args, plan):
    require(platform.system() == "Windows", "This native runner records Windows EXE/DLL and CPU baseline evidence")
    require(not args.report_parent.is_relative_to(args.source), "Evidence output must be outside the source tree")
    # UUID allocation and all mutations happen only after --execute.
    args.report_parent.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="qcs-ct-", dir=args.report_parent))
    for name in ("tests", "raw", "derived"):
        (root / name).mkdir()
    tests = root / "tests"
    record = {"schemaVersion": 2, "kind": "full-ctest-execution", "classification": "execution-record",
              "passed": False, "startedAt": now(), "reportRoot": str(root),
              "plan": plan, "errors": [], "archives": []}
    evidence = None
    before = initial_binaries = None
    bin_dir = args.build_dir / "bin"
    receipt_digest = runner_digest = None
    bound_inputs = {}
    print(json.dumps({"reportRoot": str(root), "startedAt": record["startedAt"]}), flush=True)
    try:
        evidence_path = args.source / "tools/qcs-release-evidence.py"
        checker_path = args.source / ".github/scripts/qcs-ctest-evidence.py"
        require(args.launch_cwd.is_dir(), "Explicit launch cwd does not exist")
        require(args.model_dir.is_dir(), "Model directory does not exist")
        require((args.qt_prefix / "plugins").is_dir(), "Qt plugin directory does not exist")
        evidence = load_module(evidence_path)
        receipt_digest = digest(args.build_record)
        runner_digest = digest(Path(__file__))
        receipt = evidence.read_json(args.build_record)
        before = evidence.source_identity(args.source, args.head)
        record["identity"] = before
        require(not evidence.valid_identity(before), "Source identity is incomplete or modified")
        require(before["checkoutCommit"] == args.head and before["sourceTree"] == args.source_tree,
                "Current source does not match the explicitly frozen commit and tree")
        require(before == receipt.get("identity"), "Source identity differs from the original build receipt")
        require(Path(receipt["sourceDirectory"]).resolve() == args.source,
                "Build receipt belongs to a different checkout")
        require(not evidence.verified_build(args.build_record.parent, receipt), "Original build receipt integrity failed")
        require(set(before["lockHashes"]) == set(evidence.LOCKS) and set(receipt["programs"]) == PRODUCERS,
                "Expected current dependency locks and four original application producers")
        # Keep the entire original receipt bundle locally verifiable after the
        # original build-evidence directory is removed. No receipt fields change.
        archived_build = root / "raw" / "build"
        archived_build.mkdir()
        for item in receipt["files"]:
            original = evidence.safe_file(args.build_record.parent, item["path"])
            destination = archived_build / item["path"]
            require(destination.resolve().is_relative_to(archived_build.resolve()),
                    "Build receipt archive path escapes its dedicated directory")
            destination.parent.mkdir(parents=True, exist_ok=True)
            archive_file(original, destination, root, record["archives"],
                         origin=str(original), classification="raw-build-capture")
        archive_file(args.build_record, archived_build / "build-record.json", root, record["archives"],
                     origin=str(args.build_record), classification="raw-build-capture")
        archived_receipt = evidence.read_json(archived_build / "build-record.json")
        archived_build_errors = evidence.verified_build(archived_build, archived_receipt)
        record["archivedBuildValidationErrors"] = archived_build_errors
        record["archivedBuildPassed"] = not archived_build_errors and archived_receipt == receipt
        require(record["archivedBuildPassed"], "Archived original build receipt bundle failed verification")
        initial_binaries = binary_snapshot(bin_dir)
        for name, producer in receipt["programs"].items():
            path = str(Path(producer["originalPath"]).resolve())
            require(path in initial_binaries, f"Producer is outside the selected build binary snapshot: {name}")
            require(initial_binaries[path]["sha256"] == producer["sha256"]
                    and initial_binaries[path]["bytes"] == producer["bytes"],
                    f"Producer changed after the recorded build: {name}")
        for name, path in {"runner.py": Path(__file__), "build-record.json": args.build_record,
                           "release-evidence.py": evidence_path, "ctest-evidence.py": checker_path,
                           "temp_paths.py": args.source / "tests/support/temp_paths.py"}.items():
            bound_inputs[str(path.resolve())] = digest(path)
            archive_file(path, root / "raw" / name, root, record["archives"], origin=str(path.resolve()))
        reference_inputs = root / "raw" / "reference-inputs"
        reference_inputs.mkdir()
        for name, path in {
            "vectors.json": args.source / "tests/runtime/fixtures/vectors.json",
            "README.md": args.source / "tests/runtime/fixtures/README.md",
            "LICENSE-DigUp": args.source / "tests/runtime/fixtures/LICENSE-DigUp",
            "reference_vectors_test.cpp": args.source / "tests/runtime/reference_vectors_test.cpp",
        }.items():
            bound_inputs[str(path.resolve())] = digest(path)
            archive_file(path, reference_inputs / name, root, record["archives"],
                         origin=str(path.resolve()), classification="input-not-measurement")
        cache_path = args.build_dir / "CMakeCache.txt"
        cache = cache_path.read_text(encoding="utf-8-sig")
        source_matches = re.findall(r"^CMAKE_HOME_DIRECTORY:[^=]+=(.+)$", cache, re.MULTILINE)
        require(len(source_matches) == 1 and Path(source_matches[0].strip()).resolve() == args.source,
                "Selected build cache belongs to a different source checkout")
        for key, expected in (("QCOPILOTS_SEARCH_VARIANT", args.variant), ("CMAKE_BUILD_TYPE", args.config)):
            values = re.findall(r"^" + re.escape(key) + r":[^=]+=(.+)$", cache, re.MULTILINE)
            require(values == [expected], f"Selected build cache differs from {key}={expected}")
        make_matches = re.findall(r"^CMAKE_MAKE_PROGRAM:[^=]+=(.+)$", cache, re.MULTILINE)
        require(len(make_matches) == 1, "The selected CMake cache must identify one Ninja executable")
        ninja = Path(make_matches[0].strip()).resolve()
        require(ninja.is_file() and ninja.stem.lower() == "ninja", "The selected build must use an existing Ninja executable")
        for path in (cache_path, ninja):
            bound_inputs[str(path.resolve())] = digest(path)
        test_definitions = [args.build_dir / "CTestTestfile.cmake",
                            *sorted((args.build_dir / "tests").rglob("CTestTestfile.cmake"))]
        require(all(path.is_file() for path in test_definitions), "Generated test definitions are missing")
        for path in test_definitions:
            bound_inputs[str(path.resolve())] = digest(path)
        archive_file(cache_path, root / "raw" / "CMakeCache.txt", root, record["archives"], origin=str(cache_path))
        pwsh, ctest = shutil.which("pwsh"), shutil.which("ctest")
        require(pwsh is not None and ctest is not None, "PowerShell 7 and CTest must be available on PATH")
        command = [pwsh, "-NoProfile", "-File", str(args.source / "qcs-dev.ps1"),
                   "test", "--variant", args.variant, "--config", args.config,
                   "--build-root", str(args.build_root), "--qt-root", str(args.qt_root),
                   "--model-dir", str(args.model_dir), "--jobs", str(args.jobs),
                   "--report-dir", str(tests), "--keep-failed-tests"]
        environment = os.environ.copy()
        # The wrapper initializes MSVC when needed. Also preserve the exact
        # configured Ninja directory for workflow/install-layout fixture builds.
        # Never add cl.exe alone, which would bypass the wrapper's full VS setup.
        path_prefix = [str(ninja.parent), str(Path(ctest).parent), str(args.qt_prefix / "bin")]
        environment["PATH"] = os.pathsep.join(path_prefix + [environment.get("PATH", "")])
        environment.update(QCOPILOTS_SEARCH_PYTHON=sys.executable,
                           QCOPILOTS_SEARCH_MODEL_DIR=str(args.model_dir),
                           QCOPILOTS_SEARCH_SEMANTIC_REPORT=str(root / "semantic-functional.json"),
                           QCOPILOTS_SEARCH_TESSDATA_DIR=str(cache_directory(cache, "QCOPILOTS_SEARCH_TESSDATA_DIR")),
                           QCOPILOTS_SEARCH_CPPJIEBA_DICT_DIR=str(cache_directory(cache, "QCOPILOTS_SEARCH_CPPJIEBA_DICT_DIR")),
                           QT_PLUGIN_PATH=str(args.qt_prefix / "plugins"))
        environment.pop("QCOPILOTS_SEARCH_CPU_BASELINE", None)
        record.update(buildRecord=str(args.build_record), buildRecordSha256=receipt_digest,
                      runnerSha256=runner_digest, binaries=initial_binaries,
                      producers=receipt["programs"], boundInputs=bound_inputs)
        begin = {**record, "identity": before, "buildRecord": str(args.build_record),
                 "buildRecordSha256": receipt_digest, "runnerSha256": runner_digest,
                 "boundInputs": bound_inputs, "binaries": initial_binaries,
                 "producers": receipt["programs"], "command": command,
                 "toolchain": {"makeProgram": str(ninja), "makeProgramSha256": digest(ninja),
                               "pathPrefix": path_prefix, "msvcEnvironment": "Preserved inherited environment, with the PowerShell entry point initializing full VS when cl.exe is absent"},
                 "semanticReport": environment["QCOPILOTS_SEARCH_SEMANTIC_REPORT"],
                 "launchCwd": str(args.launch_cwd),
                 "environmentOverrides": {key: environment[key] for key in
                     ("QCOPILOTS_SEARCH_PYTHON", "QCOPILOTS_SEARCH_MODEL_DIR", "QCOPILOTS_SEARCH_SEMANTIC_REPORT",
                      "QCOPILOTS_SEARCH_TESSDATA_DIR", "QCOPILOTS_SEARCH_CPPJIEBA_DICT_DIR", "QT_PLUGIN_PATH")},
                 "loadCondition": "Exclusive heavy-work scheduling is controlled externally. This runner launches only CTest and validators, with the registered RUN_SERIAL properties."}
        write_json(tests / "execution-begin.json", begin)
        inventory_command = [ctest, "--test-dir", str(args.build_dir), "-C", args.config, "--show-only=json-v1"]
        inventory = subprocess.run(inventory_command, capture_output=True, cwd=args.launch_cwd, env=environment)
        (tests / "ctest-inventory.json").write_bytes(inventory.stdout)
        (tests / "inventory.stderr.log").write_bytes(inventory.stderr)
        record["inventoryCommand"] = inventory_command
        require(inventory.returncode == 0, "CTest inventory failed")
        registered = json.loads(inventory.stdout)
        names = [test["name"] for test in registered["tests"]]
        require(registered.get("kind") == "ctestInfo" and len(names) == args.expected_tests
                and len(set(names)) == len(names)
                and all(name.startswith("qcs-tests-") for name in names),
                "Registered CTest count or unique product names differ from frozen expectation")
        require({SEMANTIC, REFERENCE, REFERENCE + "-baseline"}.issubset(names), "Required real-model tests are missing")
        inventory_bindings = []
        for test in registered["tests"]:
            inventory_bindings.append(inventory_binding(test, args.source, initial_binaries, bound_inputs))
            if test["name"] not in {SEMANTIC, REFERENCE, REFERENCE + "-baseline"}:
                continue
            properties = {item["name"]: item["value"] for item in test.get("properties", [])}
            require(properties.get("RUN_SERIAL") is True, "Real-model test lost RUN_SERIAL: " + test["name"])
            for setting in properties.get("ENVIRONMENT", []):
                if setting.startswith("QCOPILOTS_SEARCH_MODEL_DIR="):
                    configured_models = setting.partition("=")[2]
                    require(bool(configured_models) and Path(configured_models).resolve() == args.model_dir,
                            "CTest model-directory override differs from the explicitly selected model directory")
        binding_path = tests / "inventory-binding.json"
        write_json(binding_path, {"createdAt": now(), "programs": inventory_bindings, "boundInputs": bound_inputs})
        record["inventoryBinding"] = {"path": "tests/inventory-binding.json", **file_info(binding_path)}
        record["ctestCount"] = len(names)
        record["command"] = command
        print(f"BEGIN {now()} {args.head}: {len(names)} registered tests", flush=True)
        with (tests / "command.stdout.log").open("xb") as stdout, (tests / "command.stderr.log").open("xb") as stderr:
            completed = subprocess.run(command, cwd=args.launch_cwd, env=environment, stdout=stdout, stderr=stderr)
        record["exitCode"] = completed.returncode
        if completed.returncode != 0:
            record["errors"].append(f"Full CTest command exited {completed.returncode}")
        for filename in ("ctest.xml", "ctest-inventory.json", "command.stdout.log", "command.stderr.log",
                         "test-selection.json", "test-inventory.json", "test-selected-inventory.json"):
            archive_file(tests / filename, root / "raw" / filename, root, record["archives"],
                         origin="tests/" + filename)
        try:
            validate_full_selection(evidence.read_json(tests / "test-selection.json"), names)
            record["completeSelectionPassed"] = True
        except ValueError as error:
            record["completeSelectionPassed"] = False
            record["errors"].append(str(error))
        checker = [sys.executable, str(checker_path), "--junit", str(tests / "ctest.xml"),
                   "--platform", "Windows", "--inventory", str(tests / "ctest-inventory.json"),
                   "--output", str(tests / "qt-test-cases.json"), "--require-complete"]
        with (tests / "case-evidence.stdout.log").open("xb") as stdout, (tests / "case-evidence.stderr.log").open("xb") as stderr:
            checked = subprocess.run(checker, cwd=args.launch_cwd, env=environment, stdout=stdout, stderr=stderr)
        record["caseEvidenceCommand"] = checker
        record["caseEvidenceExitCode"] = checked.returncode
        cases = evidence.read_json(tests / "qt-test-cases.json")
        record["caseEvidencePassed"] = (checked.returncode == 0 and cases.get("releaseComplete") is True
                                       and cases.get("inventoryVerified") is True and not cases.get("errors"))
        if not record["caseEvidencePassed"]:
            record["errors"].append("Strict JUnit, inventory or individual-case evidence failed")
        archive_file(tests / "qt-test-cases.json", root / "derived" / "qt-test-cases.json", root,
                     record["archives"], origin="tests/qt-test-cases.json", classification="derived")
        references = archive_reference_output(tests / "ctest.xml", names, root, record["archives"])
        record["referencePassed"] = references["passed"]
        record["errors"].extend(references["errors"])
        semantic_path = root / "semantic-functional.json"
        semantic_archive = archive_file(semantic_path, root / "raw" / "semantic-functional.json", root,
                                       record["archives"], origin="Original semantic executable JSON, written during this run")
        semantic = evidence.read_json(semantic_path)
        semantic_errors = evidence.validate_semantic(semantic)
        semantic_summary = {"classification": "derived", "source": "raw/semantic-functional.json",
                            "sourceSha256": semantic_archive["sha256"], "passed": not semantic_errors,
                            "validationErrors": semantic_errors, "queries": len(semantic.get("queries", [])),
                            "actualEmbeddedSegments": semantic.get("actualEmbeddedSegments"),
                            "modelFingerprint": semantic.get("modelFingerprint"),
                            "extractionFingerprint": semantic.get("extractionFingerprint")}
        semantic_summary_path = root / "derived" / "semantic-summary.json"
        write_json(semantic_summary_path, semantic_summary)
        record["archives"].append({"path": semantic_summary_path.relative_to(root).as_posix(),
                                    **file_info(semantic_summary_path), "classification": "derived",
                                    "origin": "raw/semantic-functional.json"})
        record.update(semanticPassed=not semantic_errors, semanticQueries=semantic_summary["queries"],
                      semanticSegments=semantic_summary["actualEmbeddedSegments"],
                      extractionFingerprint=semantic_summary["extractionFingerprint"],
                      semanticReportSha256=semantic_archive["sha256"], semanticValidationErrors=semantic_errors)
        record["errors"].extend(semantic_errors)
    except Exception as error:
        record["errors"].append(f"{type(error).__name__}: {error}")
    finally:
        # On early validator failures, still retain any original outputs which
        # were actually produced. Missing output is never replaced or invented.
        for source_path, filename in [(root / "semantic-functional.json", "semantic-functional.json"),
                                      *[(tests / name, name) for name in
                                        ("ctest.xml", "ctest-inventory.json", "command.stdout.log", "command.stderr.log")]]:
            destination = root / "raw" / filename
            if source_path.is_file() and not destination.exists():
                try:
                    archive_file(source_path, destination, root, record["archives"],
                                 origin=str(source_path), classification="raw")
                except Exception as error:
                    record["errors"].append(f"Failure-output archival: {error}")
        # Preserve actual end identities even after a test or validator failure.
        if evidence is not None and before is not None:
            try:
                after = evidence.source_identity(args.source, args.head)
                record["endIdentity"] = after
                record["sourceUnchanged"] = after == before
                require(record["sourceUnchanged"], "Source identity changed during execution")
            except Exception as error:
                record["errors"].append(f"End source verification: {error}")
        if initial_binaries is not None:
            try:
                final_binaries = binary_snapshot(bin_dir)
                record["endBinaries"] = final_binaries
                record["binariesUnchanged"] = final_binaries == initial_binaries
                require(record["binariesUnchanged"], "One or more EXE/DLL files changed during execution")
            except Exception as error:
                record["errors"].append(f"End binary verification: {error}")
        for path, expected in bound_inputs.items():
            try:
                require(digest(Path(path)) == expected, f"Bound input changed: {path}")
            except Exception as error:
                record["errors"].append(str(error))
        try:
            record["runnerUnchanged"] = runner_digest is not None and digest(Path(__file__)) == runner_digest
            record["buildRecordUnchanged"] = receipt_digest is not None and digest(args.build_record) == receipt_digest
            for item in record["archives"]:
                require(file_info(root / item["path"]) == {"bytes": item["bytes"], "sha256": item["sha256"]},
                        "Archived evidence changed: " + item["path"])
            if record.get("inventoryBinding"):
                item = record["inventoryBinding"]
                require(file_info(root / item["path"]) == {"bytes": item["bytes"], "sha256": item["sha256"]},
                        "Pre-execution inventory binding changed")
        except Exception as error:
            record["errors"].append(f"End input/archive verification: {error}")
        record["finishedAt"] = now()
        record["passed"] = (not record["errors"] and record.get("exitCode") == 0
                            and all(record.get(key) is True for key in
                                    ("sourceUnchanged", "binariesUnchanged", "runnerUnchanged", "buildRecordUnchanged",
                                     "archivedBuildPassed", "caseEvidencePassed", "referencePassed", "semanticPassed",
                                     "completeSelectionPassed")))
        try:
            write_json(tests / "execution-record.json", record)
            summarize_execution(root, record)
        except Exception as error:
            record["passed"] = False
            record["errors"].append(f"Cannot save completion record: {error}")
            try:
                write_json(tests / "execution-record.json", record)
            except OSError:
                pass
            print(json.dumps(record, ensure_ascii=False, indent=2, allow_nan=False), file=sys.stderr, flush=True)
    print(json.dumps({"reportRoot": str(root), **{key: record.get(key) for key in
          ("passed", "exitCode", "sourceUnchanged", "binariesUnchanged", "ctestCount",
           "caseEvidenceExitCode", "referencePassed", "semanticPassed", "semanticQueries", "semanticSegments", "errors")}},
          ensure_ascii=False, indent=2), flush=True)
    return 0 if record["passed"] else 1


def main(argv=None):
    args = arguments(argv)
    plan = {"executeRequested": args.execute, "head": args.head, "sourceTree": args.source_tree,
            "source": str(args.source), "buildRoot": str(args.build_root), "buildRecord": str(args.build_record),
            "reportParent": str(args.report_parent), "directoryPattern": "qcs-ct-<atomic random suffix>",
            "expectedTests": args.expected_tests, "variant": args.variant, "config": args.config,
            "buildDirectory": str(args.build_dir), "modelDir": str(args.model_dir),
            "qtRoot": str(args.qt_root), "qtPrefix": str(args.qt_prefix),
            "launchCwd": str(args.launch_cwd), "jobs": args.jobs,
            "scope": "Windows full unfiltered CTest only. No build, repair, retry, baseline replacement or evidence rebinding.",
            "processOwnershipLimit": "Uses subprocess.run without a Windows Job Object or process-tree supervisor. The coordinator must independently verify that processes owned by this run have exited, especially after interruption.",
            "executionAuthorization": "Pass --execute to run the explicit candidate inputs."}
    if not args.execute:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 0
    return execute(args, plan)


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
