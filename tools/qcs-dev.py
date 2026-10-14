#!/usr/bin/env python3
"""Portable developer workflow. Generated files always live outside the checkout."""
from __future__ import annotations

import argparse
import copy
import importlib.util
from datetime import datetime, timezone
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid
import venv
import xml.etree.ElementTree as ET

sys.dont_write_bytecode = True
SOURCE = Path(__file__).resolve().parent.parent
PRODUCT = "qcs"
GUI_EXECUTABLE = "qcs-gui"
MARKER = ".qcs-build-root.json"
OPERATION_LOCK = ".qcs-dev.lock"
GENERATED_DIRECTORIES = frozenset({"release", "debug", "relwithdebinfo", "deps", "runs", "stage", "pkg", "reports", "bootstrap"})
PRESERVED_DIRECTORIES = frozenset({"models", "sdk", "development-profile"})
SLOW_TEST_LABEL_REGEX = "^slow$"
_ACTIVE_LOG = None


def validate_owner(owner: object, root: Path):
    if (not isinstance(owner, dict) or owner.get("schemaVersion") != 1
            or not isinstance(owner.get("source"), str) or not owner["source"]
            or not Path(owner["source"]).is_absolute()):
        raise RuntimeError(f"Invalid build root ownership marker: {root}")
    if Path(owner["source"]).resolve() != SOURCE:
        raise RuntimeError(f"Build root belongs to a different checkout: {owner['source']}")


def protect_input_paths(root: Path, *paths: Path | None):
    for path in paths:
        if path is None:
            continue
        resolved = path.expanduser().resolve()
        for name in GENERATED_DIRECTORIES:
            generated = root / name
            if resolved == generated or generated in resolved.parents:
                raise RuntimeError(f"Model and SDK inputs cannot be placed in a cleanable directory: {resolved}")


def is_redirect(path: Path) -> bool:
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


@contextmanager
def root_lock(root: Path):
    """Serialize build mutations and clean without unlinking a live lock inode."""
    path = root / OPERATION_LOCK
    if path.exists() and (is_redirect(path) or not path.is_file()):
        raise RuntimeError(f"Developer lock is not an ordinary file: {path}")
    with path.open("a+b") as stream:
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        acquired = False
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
            yield
        except OSError as error:
            if not acquired:
                raise RuntimeError(f"Another developer operation owns this build root: {root}") from error
            raise
        finally:
            if acquired:
                stream.seek(0)
                if os.name == "nt":
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def verify_plain_tree(path: Path):
    """Allow internal SDK/library symlinks as entries, never as traversal roots."""
    if is_redirect(path):
        raise RuntimeError(f"Refusing linked or reparse clean target: {path}")
    owned_root = path.parent.resolve()
    def on_error(error):
        raise error
    for current, directories, files in os.walk(path, followlinks=False, onerror=on_error):
        for name in [*directories, *files]:
            candidate = Path(current) / name
            if is_redirect(candidate):
                # Framework Versions/Current and versioned Unix .so entries
                # must be unlinked without inspecting or changing their targets.
                # Junctions and unknown reparse point types remain forbidden.
                if not stat.S_ISLNK(candidate.lstat().st_mode):
                    raise RuntimeError(f"Refusing reparse generated entry: {candidate}")
                try:
                    destination = candidate.resolve()
                except (OSError, RuntimeError) as error:
                    raise RuntimeError(f"Cannot validate generated symlink: {candidate}: {error}") from error
                if destination != owned_root and owned_root not in destination.parents:
                    raise RuntimeError(f"Refusing external generated symlink: {candidate} -> {destination}")
                if name in directories:
                    directories.remove(name)


def clean_generated(root: Path, config: str, scope: str, dry_run: bool):
    # Check before opening the lock so unowned roots and dry runs remain untouched.
    assert_external(root)
    if not root.is_dir() or is_redirect(root):
        raise RuntimeError(f"Build root does not exist or is redirected: {root}")
    marker = root / MARKER
    if not marker.is_file() or is_redirect(marker):
        raise RuntimeError(f"Cannot establish build root ownership: {root}")
    validate_owner(json.loads(marker.read_bytes()), root)
    if dry_run:
        return _clean_generated(root, config, scope, True)
    with root_lock(root):
        return _clean_generated(root, config, scope, False)


def _clean_generated(root: Path, config: str, scope: str, dry_run: bool):
    """Delete an explicit inventory only after all ownership checks succeed."""
    assert_external(root)
    if not root.exists():
        raise RuntimeError(f"Build root does not exist: {root}")
    if is_redirect(root):
        raise RuntimeError(f"Refusing linked or reparse build root: {root}")
    root_info = root.stat()
    root_identity = (root_info.st_dev, root_info.st_ino)
    marker = root / MARKER
    if not marker.is_file() or is_redirect(marker):
        raise RuntimeError(f"Cannot establish build root ownership: {root}")
    marker_info = marker.stat()
    marker_identity = (marker_info.st_dev, marker_info.st_ino)
    marker_content = marker.read_bytes()
    owner = json.loads(marker_content)
    validate_owner(owner, root)

    def require_owner_unchanged():
        for path, expected in ((root, root_identity), (marker, marker_identity)):
            if is_redirect(path):
                raise RuntimeError(f"Build root ownership changed during clean: {path}")
            info = path.stat()
            if (info.st_dev, info.st_ino) != expected:
                raise RuntimeError(f"Build root ownership changed during clean: {path}")
        if marker.read_bytes() != marker_content:
            raise RuntimeError(f"Build root marker changed during clean: {marker}")
    names = {config.lower()} if scope == "build" else GENERATED_DIRECTORIES
    if scope == "generated":
        unknown = [item.name for item in root.iterdir()
                   if item.name not in GENERATED_DIRECTORIES | PRESERVED_DIRECTORIES | {MARKER, OPERATION_LOCK}]
        if unknown:
            raise RuntimeError(f"Refusing generated clean with unknown root entries: {', '.join(sorted(unknown))}")
    targets = []
    for name in sorted(names):
        target = root / name
        if not target.exists() and not target.is_symlink():
            continue
        if target.resolve().parent != root.resolve() or not target.is_dir():
            raise RuntimeError(f"Refusing unsafe clean target: {target}")
        verify_plain_tree(target)
        if name in {"release", "debug", "relwithdebinfo"}:
            cache = target / "CMakeCache.txt"
            expected = "CMAKE_HOME_DIRECTORY:INTERNAL=" + SOURCE.as_posix()
            if not cache.is_file() or expected not in cache.read_text(encoding="utf-8").splitlines():
                raise RuntimeError(f"Cannot establish ownership of build directory: {target}")
        info = target.stat()
        targets.append((target, (info.st_dev, info.st_ino)))
    result = {"scope": scope, "dryRun": dry_run, "root": str(root),
              "targets": [str(path) for path, _ in targets], "removed": []}
    for target, identity in targets:
        if not dry_run:
            require_owner_unchanged()
            if is_redirect(target) or (target.stat().st_dev, target.stat().st_ino) != identity:
                raise RuntimeError(f"Clean target changed after validation: {target}")
            verify_plain_tree(target)
            require_owner_unchanged()
            info = target.stat()
            if is_redirect(target) or (info.st_dev, info.st_ino) != identity:
                raise RuntimeError(f"Clean target changed after validation: {target}")
            def remove_readonly(function, filename, failure):
                error = failure[1]
                path = Path(filename)
                require_owner_unchanged()
                if (not isinstance(error, PermissionError) or is_redirect(path)
                        or not path.is_file() or target not in path.resolve().parents):
                    raise error
                file_info = path.stat()
                if file_info.st_mode & stat.S_IWRITE:
                    raise error
                # Windows Git/download caches may mark generated files read-only.
                # Retry only this verified ordinary file, without changing ACLs.
                path.chmod(file_info.st_mode | stat.S_IWRITE)
                function(filename)
            try:
                shutil.rmtree(target, onerror=remove_readonly)
            except OSError as error:
                result.update(passed=False, failedTarget=str(target), error=str(error))
                print(json.dumps(result, ensure_ascii=False, indent=2))
                raise
            result["removed"].append(str(target))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def check_path_budget(root: Path, build: Path, stage: Path, reports: Path, *, configured: bool):
    command = [sys.executable, SOURCE / "tools/qcs-path-budget.py", "--source", SOURCE,
               "--root", root, "--stage", stage, "--package-root", root / "pkg",
               "--report", reports / ("paths-configured.json" if configured else "paths-doctor.json")]
    if configured:
        command.extend(["--build", build])
    run(command, cwd=root)


def run(command, *, cwd=None, env=None):
    command = [str(item) for item in command]
    print("+ " + subprocess.list2cmdline(command), flush=True)
    if _ACTIVE_LOG is None:
        return subprocess.run(command, cwd=cwd, env=env, check=True)
    with _ACTIVE_LOG.open("ab") as log:
        log.write(("+ " + subprocess.list2cmdline(command) + "\n").encode("utf-8"))
        log.flush()
        process = subprocess.Popen(command, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        for line in iter(process.stdout.readline, b""):
            log.write(line)
            log.flush()
            print(line.decode("utf-8", errors="replace"), end="", flush=True)
        process.stdout.close()
        status = process.wait()
        if status:
            raise subprocess.CalledProcessError(status, command)
        return subprocess.CompletedProcess(command, status)


def report_package_errors(root: Path, started: float):
    packaging = (root / "pkg/_CPack_Packages").resolve()
    if not packaging.is_dir():
        return
    for name in ("NSISOutput.log", "CPackError.log"):
        for log in sorted(packaging.rglob(name)):
            resolved = log.resolve()
            if packaging not in resolved.parents or log.stat().st_mtime < started - 2:
                continue
            # Surface the generator's actual diagnostic even when CPack prints
            # only a temporary path that will disappear with a CI runner.
            with log.open("rb") as stream:
                stream.seek(max(0, log.stat().st_size - 65536))
                diagnostic = stream.read().decode("utf-8", errors="replace")
            print(f"Package generator diagnostic ({log}):\n{diagnostic}", file=sys.stderr, flush=True)


def assert_external(path: Path):
    resolved = path.resolve()
    if resolved == SOURCE or SOURCE in resolved.parents or resolved in SOURCE.parents:
        raise RuntimeError(f"Generated output must be outside the source tree: {resolved}")
    if resolved == Path(resolved.anchor):
        raise RuntimeError("A filesystem root cannot be used as a build root")


def initialize_root(root: Path):
    assert_external(root)
    if root.exists() and is_redirect(root):
        raise RuntimeError(f"Build root cannot be a symlink or junction: {root}")
    root.mkdir(parents=True, exist_ok=True)
    marker = root / MARKER
    if marker.exists():
        if is_redirect(marker) or not marker.is_file():
            raise RuntimeError(f"Build root marker must be an ordinary file: {marker}")
        original = json.loads(marker.read_text(encoding="utf-8"))
        validate_owner(original, root)
    else:
        # Do not adopt unknown output as ours merely because the user selected
        # it as a build root. A model-only root supports verified offline import.
        unknown = [item.name for item in root.iterdir() if item.name != "models"]
        if unknown:
            raise RuntimeError(f"Unowned nonempty build root: {root} ({', '.join(sorted(unknown))})")
        try:
            with marker.open("x", encoding="utf-8") as stream:
                json.dump({"source": str(SOURCE), "schemaVersion": 1}, stream, indent=2)
        except FileExistsError:
            # Another checkout won the exclusive claim. Read its marker and
            # validate instead of replacing it or adopting its directories.
            initialize_root(root)


def disk_guard(path: Path, reserve_gib: float):
    probe = path
    while not probe.exists():
        probe = probe.parent
    free = shutil.disk_usage(probe).free
    if free < reserve_gib * 1024**3:
        raise RuntimeError(f"Insufficient free disk space: {free / 1024**3:.2f} GiB, reserve {reserve_gib:.2f} GiB")


def qt_prefix(root: Path):
    kit = {"Windows": "msvc2022_64", "Darwin": "macos", "Linux": "gcc_64"}[platform.system()]
    candidates = [root, root / "6.12.0" / kit]
    if platform.system() == "Darwin":
        candidates.append(root / "6.12.0" / "clang_64")
    for candidate in candidates:
        if (candidate / "lib/cmake/Qt6/Qt6Config.cmake").is_file():
            return candidate.resolve()
    return candidates[1].resolve()


def executable(directory: Path, name: str):
    suffix = ".exe" if os.name == "nt" else ""
    candidates = [directory / "bin" / (name + suffix),
                  directory / f"bin/{GUI_EXECUTABLE}.app/Contents/MacOS" / name,
                  directory / f"{GUI_EXECUTABLE}.app/Contents/MacOS" / name]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


def helper_module(filename, name):
    spec = importlib.util.spec_from_file_location(name, SOURCE / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def case_evidence(junit, inventory, profile):
    parser = helper_module(".github/scripts/qcs-ctest-evidence.py", "qcs_case_evidence")
    return parser.inspect_junit(junit, platform.system(), inventory, profile)


def run_tests(args, root: Path, build: Path, reports: Path, environment: dict):
    """Use CTest itself to resolve labels, then record the exact selected scope."""
    run_id = str(uuid.uuid4())
    test_root = Path(tempfile.mkdtemp(prefix="qcs-t-", dir=root / "runs"))
    environment["QCOPILOTS_SEARCH_TEST_ROOT"] = str(test_root)
    run_report = args.report_dir.expanduser().resolve() if args.report_dir else reports / test_root.name
    assert_external(run_report)
    run_report.mkdir(parents=True, exist_ok=True)
    latest = {"runId": run_id, "directory": str(run_report), "testRoot": str(test_root),
              "profile": args.test_profile}
    (reports / "latest-test-run.json").write_text(json.dumps(latest, indent=2), encoding="utf-8")
    filters = []
    if args.test_profile == "fast":
        filters.extend(["-LE", SLOW_TEST_LABEL_REGEX])
    if args.test_label:
        filters.extend(["-L", args.test_label])
    base = ["ctest", "--test-dir", str(build), "-C", args.config]
    selection = {"schemaVersion": 1, "kind": "ctest-selection", **latest,
                 "labelFilter": args.test_label, "filters": filters,
                 "registeredTests": [], "selectedTests": [], "excludedTests": [],
                 "commands": [], "status": "preparing", "passed": False,
                 "fullSuitePassed": False, "releaseComplete": False, "exitCode": None}

    def save():
        (run_report / "test-selection.json").write_text(
            json.dumps(selection, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def inventory(name, extra):
        command = [*base, "--show-only=json-v1", *extra]
        selection["commands"].append({"purpose": name, "arguments": command})
        save()
        result = subprocess.run(command, cwd=root, env=environment, capture_output=True)
        (run_report / (name + ".json")).write_bytes(result.stdout)
        (run_report / (name + ".stderr.log")).write_bytes(result.stderr)
        if result.returncode:
            raise subprocess.CalledProcessError(result.returncode, command, result.stdout, result.stderr)
        data = json.loads(result.stdout)
        tests = data.get("tests")
        if data.get("kind") != "ctestInfo" or not isinstance(tests, list):
            raise RuntimeError("CTest returned an invalid test inventory")
        names = [test.get("name") for test in tests]
        if any(not isinstance(name, str) or not name for name in names) or len(set(names)) != len(names):
            raise RuntimeError("CTest inventory contains invalid or duplicate test names")
        return tests

    save()
    try:
        registered = inventory("test-inventory", [])
        shutil.copyfile(run_report / "test-inventory.json", run_report / "ctest-inventory.json")
        selection["registeredTests"] = [test["name"] for test in registered]
        selected = inventory("test-selected-inventory", filters)
        selected_names = [test["name"] for test in selected]
        selection["selectedTests"] = selected_names
        selection["excludedTests"] = [name for name in selection["registeredTests"] if name not in selected_names]
        selection["counts"] = {"registered": len(registered), "selected": len(selected),
                               "excluded": len(selection["excludedTests"])}
        if not set(selected_names).issubset(selection["registeredTests"]):
            raise RuntimeError("CTest registration changed while selecting tests")
        if not selected:
            raise RuntimeError("No tests match the selected profile and label. Use --test-profile full to include slow tests.")
        if args.test_profile == "fast":
            slow = [test["name"] for test in selected if "slow" in next(
                (prop["value"] for prop in test.get("properties", []) if prop["name"] == "LABELS"), [])]
            if slow:
                # CTest may automatically include fixture setup/cleanup despite
                # label filters. Never execute that slow dependency implicitly.
                raise RuntimeError("Fast tests require slow fixtures. Use --test-profile full: " + ", ".join(slow))
        command = [*base, "--output-on-failure", "--no-tests=error", "--parallel", str(args.jobs),
                   "--test-output-size-passed", "16777216", "--test-output-size-failed", "16777216",
                   "--test-output-truncation", "middle", "--output-junit", str(run_report / "ctest.xml"), *filters]
        if args.fail_fast:
            command.append("--stop-on-failure")
        selection["commands"].append({"purpose": "execute", "arguments": command})
        selection["status"] = "running"
        save()
        print(f"CTest profile {args.test_profile}: {len(selected)} selected, "
              f"{len(selection['excludedTests'])} excluded. Release acceptance is incomplete.", flush=True)
        run(command, cwd=root, env=environment)
        cases = list(ET.parse(run_report / "ctest.xml").getroot().iter("testcase"))
        actual_names = [case.get("name") for case in cases]
        if len(actual_names) != len(selected_names) or set(actual_names) != set(selected_names):
            raise RuntimeError("Executed CTest programs differ from the selected inventory")
        if any(case.get("status") != "run" or case.find("failure") is not None
               or case.find("error") is not None or case.find("skipped") is not None for case in cases):
            raise RuntimeError("Selected CTest programs include failed or unexecuted tests")
        # The same parser is used locally and by native CI. CTest exit zero
        # alone cannot prove that QtTest ran every required case.
        evidence = case_evidence(run_report / "ctest.xml", run_report / "test-selected-inventory.json",
                                 args.test_profile)
        (run_report / "qt-test-cases.json").write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if not evidence["testsComplete"]:
            raise RuntimeError("Selected test case evidence is incomplete: " + ", ".join(evidence["errors"]))
        selection.update(status="passed", passed=True, exitCode=0,
                         fullSuitePassed=args.test_profile == "full" and not args.test_label
                         and not selection["excludedTests"])
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError, ET.ParseError) as error:
        selection.update(status="failed", error=str(error),
                         exitCode=error.returncode if isinstance(error, subprocess.CalledProcessError) else 1)
        raise
    finally:
        if (run_report / "ctest.xml").is_file() and not (run_report / "qt-test-cases.json").is_file():
            diagnostic = case_evidence(run_report / "ctest.xml", run_report / "test-selected-inventory.json",
                                       args.test_profile)
            (run_report / "qt-test-cases.json").write_text(
                json.dumps(diagnostic, ensure_ascii=False, indent=2), encoding="utf-8")
        save()
    return run_report


def package_for_suffix(root, suffix):
    candidates = [path for path in (root / "pkg").glob(PRODUCT + "-*" + suffix)
                  if "-source-materials-" not in path.name and "-symbols-" not in path.name]
    if len(candidates) != 1:
        raise RuntimeError(f"Expected one final {suffix} package, found {len(candidates)}")
    return candidates[0]


def nsis_contract(root, reports, environment):
    """Run the real compiler/install contract only in the NSIS package stage."""
    command = [sys.executable, "-B", SOURCE / "tools/qcs-test-workflow.py", "--suite", "native"]
    result = subprocess.run([str(item) for item in command], cwd=root, env=environment,
                            capture_output=True, text=True, encoding="utf-8", errors="replace")
    output = result.stdout + result.stderr
    (reports / "nsis-contract.log").write_text(output, encoding="utf-8")
    print(output, end="")
    parser = helper_module(".github/scripts/qcs-ctest-evidence.py", "qcs_nsis_cases")
    counts, records, skips, errors = parser.inspect_signing_unittest(output, platform.system(), "qcs-tests-workflow-native")
    if result.returncode:
        errors.append(f"NSIS compiler contract exited {result.returncode}")
    if counts["passed"] != 1 or counts["skipped"] or len(records) != 1:
        errors.append("The native NSIS compiler/install contract did not execute exactly once")
    document = {"schemaVersion": 1, "passed": not errors, "counts": counts, "cases": records,
                "errors": errors, "exitCode": result.returncode, "log": "nsis-contract.log"}
    (reports / "nsis-contract.json").write_text(json.dumps(document, indent=2), encoding="utf-8")
    if errors:
        raise RuntimeError("NSIS compiler contract failed: " + ", ".join(errors))


def execute_action(action, args, root, build, stage, reports, sdk_root, prefix, environment):
    """The only implementation of local and CI single-stage operations."""
    if action == "bootstrap":
        tools_root = root / "bootstrap/python"
        python = tools_root / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        if not python.is_file():
            venv.EnvBuilder(with_pip=True).create(tools_root)
        run([python, "-m", "pip", "install", "py7zr==1.1.3"], cwd=root)
        run([python, SOURCE / "tools/qcs-bootstrap-qt.py", "--qt-root", sdk_root, "--cache-dir", root / "bootstrap/qt-downloads"], cwd=root)
    elif action == "doctor":
        report = {"source": str(SOURCE), "buildRoot": str(root), "qtPrefix": str(prefix), "platform": platform.platform(), "freeGiB": round(shutil.disk_usage(root).free / 1024**3, 2), "tools": {name: shutil.which(name) for name in ("cmake", "ctest", "cpack", "ninja", "git", "cl" if os.name == "nt" else "c++")}}
        report["qtInstalled"] = (prefix / "lib/cmake/Qt6/Qt6Config.cmake").is_file()
        print(json.dumps(report, indent=2, ensure_ascii=False))
        (reports / "doctor.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        check_path_budget(root, build, stage, reports, configured=False)
        if not report["qtInstalled"] or not all(report["tools"].values()):
            raise RuntimeError("Toolchain is incomplete. Install missing tools or run bootstrap.")
    elif action == "configure":
        command = ["cmake", "-S", SOURCE, "-B", build, "-G", "Ninja", f"-DCMAKE_BUILD_TYPE={args.config}", f"-DCMAKE_PREFIX_PATH={prefix}", f"-DCMAKE_INSTALL_PREFIX={stage}", f"-DQCOPILOTS_SEARCH_DEPENDENCY_ROOT={root / 'deps'}", f"-DQCOPILOTS_SEARCH_TEST_ROOT={root / 'runs'}", f"-DPython3_EXECUTABLE={sys.executable}", "-DQCOPILOTS_SEARCH_BUILD_TESTS=ON"]
        if args.model_dir:
            command.append(f"-DQCOPILOTS_SEARCH_MODEL_DIR={args.model_dir.resolve()}")
        command.extend(args.cmake_arg)
        run(command, cwd=root, env=environment)
        check_path_budget(root, build, stage, reports, configured=True)
    elif action == "build":
        run(["cmake", "--build", build, "--config", args.config, "--parallel", args.jobs], cwd=root, env=environment)
    elif action == "test":
        run_tests(args, root, build, reports, environment)
    elif action == "run":
        run([executable(build, GUI_EXECUTABLE), "--profile-dir", root / "development-profile", *args.program_arg], cwd=root, env=environment)
    elif action == "install":
        run(["cmake", "--install", build, "--config", args.config, "--prefix", stage], cwd=root, env=environment)
    elif action == "package":
        if os.name == "nt" and (not args.generator or "NSIS" in args.generator.split(";")):
            python = root / "bootstrap/python/Scripts/python.exe"
            if not python.is_file():
                venv.EnvBuilder(with_pip=True).create(python.parent.parent)
            run([python, "-m", "pip", "install", "py7zr==1.1.3"], cwd=root, env=environment)
            run([python, SOURCE / "tools/qcs-bootstrap-nsis.py", "--output-dir", root / "bootstrap/nsis",
                 "--cache-dir", root / "bootstrap/nsis-downloads"], cwd=root, env=environment)
            nsis_contract(root, reports, environment)
        check_path_budget(root, build, stage, reports, configured=True)
        run([sys.executable, SOURCE / "tools/qcs-package-symbols.py",
             "--manifest", build / f"qcs-symbol-inputs-{args.config}.json",
             "--output-dir", root / "pkg"], cwd=root, env=environment)
        command = ["cpack", "--config", build / "CPackConfig.cmake", "-C", args.config, "-B", root / "pkg"]
        if args.generator:
            command.extend(["-G", args.generator])
        started = time.time()
        try:
            run(command, cwd=root, env=environment)
        except subprocess.CalledProcessError:
            report_package_errors(root, started)
            raise
        checksums = []
        for package in sorted((root / "pkg").glob("qcs-*")):
            if package.is_file() and package.suffix != ".sha256":
                with package.open("rb") as stream:
                    hasher = hashlib.sha256()
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        hasher.update(chunk)
                    digest = hasher.hexdigest()
                checksums.append(f"{digest}  {package.name}")
        (root / "pkg/SHA256SUMS").write_text("\n".join(checksums) + "\n", encoding="utf-8")
    elif action == "verify-package":
        command = [sys.executable, SOURCE / "tools/qcs-verify-package.py", "--build-root", root]
        if args.keyword_only:
            command.append("--keyword-only")
        elif args.model_dir:
            command.extend(["--model-dir", args.model_dir.expanduser().resolve()])
        if args.package_file:
            command.extend(["--package-file", args.package_file.expanduser().resolve()])
        if args.report_dir:
            command.extend(["--report-dir", args.report_dir.expanduser().resolve()])
        run(command, cwd=root, env=environment)
    elif action == "models":
        models = (args.model_dir or root / "models").expanduser().resolve()
        assert_external(models)
        models.mkdir(parents=True, exist_ok=True)
        common = [executable(build, "qcs-cli"), "--model-dir", models,
                  "--profile-dir", root / "runs/model-management"]
        manifest = subprocess.run([str(item) for item in [*common, "models", "manifest"]], cwd=root,
                                  env=environment, capture_output=True, check=True)
        (reports / "models-manifest.json").write_bytes(manifest.stdout)
        run([*common, "--timeout", "1800000", "--progress", "models", "download"], cwd=root, env=environment)
        result = subprocess.run([str(item) for item in [*common, "models", "verify"]], cwd=root,
                                env=environment, capture_output=True)
        (reports / "models.json").write_bytes(result.stdout)
        (reports / "models.stderr.log").write_bytes(result.stderr)
        if result.returncode:
            raise subprocess.CalledProcessError(result.returncode, common, result.stdout, result.stderr)
    elif action == "sources":
        run([sys.executable, SOURCE / "tools/qcs-source-bundle.py", "--build-root", root,
             "--qt-root", sdk_root, "--download"], cwd=root, env=environment)
        archives = [root / "deps/sources/llama.cpp-b11461.tar.gz", root / "deps/llama-b11461.tar.gz"]
        archive = next((path for path in archives if path.is_file()), None)
        if archive is None:
            raise RuntimeError("Collected llama source archive is unavailable for rebuild verification")
        run([sys.executable, SOURCE / "tests/runtime/source_integrity_test.py", "--archive", archive,
             "--report", reports / "source-integrity.json"], cwd=root, env=environment)
        if json.loads((reports / "source-integrity.json").read_text(encoding="utf-8")).get("passed") is not True:
            raise RuntimeError("Required source-integrity cases failed or did not execute")
    elif action == "installers":
        if os.name == "nt":
            installer_args = copy.copy(args)
            installer_args.package_file = package_for_suffix(root, ".exe")
            installer_args.report_dir = reports / "package-nsis"
            execute_action("verify-package", installer_args, root, build, stage, reports, sdk_root, prefix, environment)
        elif platform.system() == "Linux":
            run([sys.executable, SOURCE / ".github/scripts/qcs-deb-ci.py", "--build-root", root,
                 "--package-file", package_for_suffix(root, ".deb"), "--model-dir", args.model_dir or root / "models",
                 "--report-dir", reports / "package-deb"], cwd=root, env=environment)
        else:
            raise RuntimeError("This platform has no additional installer beyond its DMG package verification")


def check_plan(profile, system):
    stages = [(action, None) for action in ("doctor", "configure", "build")]
    if profile == "full":
        stages.append(("models", None))
    stages.append(("test", None))
    if profile == "full":
        stages.append(("install", None))
        stages.extend(("package", generator) for generator in {
            "Windows": ("ZIP", "NSIS"), "Linux": ("TGZ", "DEB"), "Darwin": ("DragNDrop",)}[system])
        stages.append(("verify-package", None))
        if system in ("Windows", "Linux"):
            stages.append(("installers", None))
        stages.append(("sources", None))
    return stages


def run_check(args, root, build, stage, reports, sdk_root, prefix, environment):
    global _ACTIVE_LOG
    if args.test_label or args.keyword_only or args.generator or args.package_file:
        raise ValueError("check requires the entire selected profile. Use individual actions for filtered work.")
    directory = args.report_dir.expanduser().resolve() if args.report_dir else Path(tempfile.mkdtemp(prefix="check-", dir=reports))
    assert_external(directory)
    if root not in directory.parents:
        raise ValueError("check report directory must be inside this external build root for portable evidence")
    if directory.exists() and any(directory.iterdir()):
        raise RuntimeError("check requires an empty, unique report directory")
    directory.mkdir(parents=True, exist_ok=True)
    evidence = helper_module("tools/qcs-release-evidence.py", "qcs_check_release")
    identity = evidence.source_identity(SOURCE)
    document = {"schemaVersion": 1, "kind": "developer-check", "testProfile": args.test_profile,
                "system": platform.system(), "buildRoot": str(root), "identity": identity,
                "passed": False, "releaseComplete": False, "startedAt": datetime.now(timezone.utc).isoformat(),
                "stages": [{"action": action, "generator": generator, "status": "notRun"}
                           for action, generator in check_plan(args.test_profile, platform.system())], "files": []}
    report_path = directory / "check-report.json"
    document["reportPath"] = report_path.relative_to(root).as_posix()
    evidence.write_json(reports / "latest-check.json", {"report": str(report_path), "testProfile": args.test_profile})
    local_args = copy.copy(args)
    if args.test_profile == "full":
        local_args.model_dir = args.model_dir or root / "models"
        environment["QCOPILOTS_SEARCH_MODEL_DIR"] = str(local_args.model_dir.resolve())
        environment["QCOPILOTS_SEARCH_SEMANTIC_REPORT"] = str(directory / "semantic-quality.json")
        environment["QCOPILOTS_SEARCH_PHYSICAL_FILE_REPORT"] = str(directory / "physical-files.json")
    try:
        for index, record in enumerate(document["stages"]):
            local_args.generator = record["generator"]
            local_args.report_dir = directory / ("tests" if record["action"] == "test" else "package")
            log = directory / f"{index + 1:02d}-{record['action']}.log"
            log.write_text("", encoding="utf-8")
            record.update(status="running", startedAt=datetime.now(timezone.utc).isoformat(),
                          log=log.relative_to(root).as_posix())
            evidence.write_json(report_path, document)
            started = time.monotonic()
            _ACTIVE_LOG = log
            try:
                execute_action(record["action"], local_args, root, build, stage, directory, sdk_root, prefix, environment)
                record.update(status="passed", exitCode=0)
            except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError, ET.ParseError) as error:
                record.update(status="failed", error=str(error),
                              exitCode=error.returncode if isinstance(error, subprocess.CalledProcessError) else 1)
                raise
            finally:
                _ACTIVE_LOG = None
                record.update(elapsedSeconds=round(time.monotonic() - started, 3),
                              finishedAt=datetime.now(timezone.utc).isoformat())
                evidence.write_json(report_path, document)
        if evidence.source_identity(SOURCE) != identity:
            raise RuntimeError("Source identity changed during the developer check")
        document["passed"] = True
    finally:
        _ACTIVE_LOG = None
        document["finishedAt"] = datetime.now(timezone.utc).isoformat()
        paths = [path for path in directory.rglob("*") if path.is_file() and path != report_path]
        if args.test_profile == "full":
            paths.extend(path for path in (root / "pkg").glob("qcs-*") if path.is_file())
            if (root / "pkg/SHA256SUMS").is_file():
                paths.append(root / "pkg/SHA256SUMS")
        document["files"] = [evidence.file_record(root, path) for path in sorted(set(paths))]
        evidence.write_json(report_path, document)
        print(f"Developer {args.test_profile} check report: {report_path}")
    return report_path


def inspect_check(report_path: Path, expected_head: str):
    """Revalidate original local full evidence for release preparation."""
    evidence = helper_module("tools/qcs-release-evidence.py", "qcs_check_release")
    result = {"passed": False, "errors": [], "files": []}
    try:
        document = evidence.read_json(report_path)
        relative_report = Path(document["reportPath"])
        if relative_report.is_absolute() or ".." in relative_report.parts:
            raise ValueError("Local check report path is not relative")
        root = report_path.resolve().parents[len(relative_report.parts) - 1]
        if root.joinpath(relative_report).resolve() != report_path.resolve():
            raise ValueError("Local check report was moved without its evidence tree")
        errors = evidence.verify_manifest(root, document)
        result.update(identity=document["identity"], files=document["files"], buildRoot=str(root))
        if (document.get("kind") != "developer-check" or document.get("testProfile") != "full"
                or document.get("passed") is not True or document.get("releaseComplete") is not False
                or document["identity"].get("candidateHead") != expected_head
                or document["identity"].get("checkoutCommit") != expected_head):
            errors.append("Local check is not a successful full check of the immutable candidate")
        expected = check_plan("full", document["system"])
        stages = document.get("stages", [])
        if [(item.get("action"), item.get("generator")) for item in stages] != expected:
            errors.append("Local full stages are incomplete or changed")
        bound = {item["path"] for item in document["files"]}
        required = [report_path.parent / "tests" / name for name in (
            "test-selection.json", "test-inventory.json", "test-selected-inventory.json", "ctest.xml", "qt-test-cases.json")]
        required += [report_path.parent / name for name in (
            "models.json", "models-manifest.json", "semantic-quality.json", "physical-files.json",
            "source-integrity.json", "package/report.json")]
        if document["system"] == "Windows":
            required += [report_path.parent / name for name in ("nsis-contract.json", "nsis-contract.log", "package-nsis/report.json")]
        if document["system"] == "Linux":
            required.append(report_path.parent / "package-deb/report.json")
        if not {path.relative_to(root).as_posix() for path in required}.issubset(bound):
            errors.append("Local full check omits required bound original evidence")
        for item in stages:
            if (item.get("status") != "passed" or item.get("exitCode") != 0
                    or item.get("log") not in bound or item.get("elapsedSeconds", -1) < 0):
                errors.append("Local full stage failed, did not run, or lacks its bound raw log")
        selected = evidence.read_json(report_path.parent / "tests/test-selection.json")
        if selected.get("fullSuitePassed") is not True or selected.get("excludedTests"):
            errors.append("Local full CTest selection is incomplete")
        parser = helper_module(".github/scripts/qcs-ctest-evidence.py", "qcs_full_cases")
        cases = parser.inspect_junit(report_path.parent / "tests/ctest.xml", document["system"],
                                     report_path.parent / "tests/test-inventory.json", "full")
        errors.extend(cases["errors"])
        models = evidence.read_json(report_path.parent / "models.json")
        model_manifest = evidence.read_json(report_path.parent / "models-manifest.json")
        actual_models = models.get("result", {}).get("artifacts", [])
        expected_models = model_manifest.get("result", {}).get("artifacts", [])
        pins = evidence.package_contract()[0]
        if (models.get("ok") is not True or models.get("result", {}).get("valid") is not True
                or len(actual_models) != len(pins) or len(expected_models) != len(pins)
                or {item.get("name") for item in actual_models} != set(pins)):
            errors.append("Local full model verification is incomplete")
        for item in actual_models:
            if item.get("present") is not True or (item.get("bytes"), item.get("sha256")) != pins.get(item.get("name")):
                errors.append("Local full model bytes differ from the immutable model lock")
        for item in expected_models:
            if (item.get("size"), item.get("sha256")) != pins.get(item.get("name")):
                errors.append("Local full model manifest differs from the immutable model lock")
        semantic = evidence.read_json(report_path.parent / "semantic-quality.json")
        errors.extend(evidence.validate_semantic(semantic))
        errors.extend(evidence.validate_physical(evidence.read_json(report_path.parent / "physical-files.json"), minimum_files=1000))
        package_reports = [report_path.parent / "package/report.json"]
        if document["system"] == "Windows":
            package_reports.append(report_path.parent / "package-nsis/report.json")
        elif document["system"] == "Linux":
            package_reports.append(report_path.parent / "package-deb/report.json")
        package_hashes = {item["sha256"] for item in document["files"] if item["path"].startswith("pkg/")}
        for package_path in package_reports:
            package = evidence.read_json(package_path)
            if (package.get("passed") is not True or package.get("releaseComplete") is not True
                    or package.get("commit") != expected_head or package.get("workingTreeModified", False) is not False
                    or package.get("packageSha256") not in package_hashes
                    or len(package.get("checks", [])) < (3 if "package-deb" in str(package_path) else 9)):
                errors.append("Local full package result is incomplete, stale or unbound")
            smoke_path = package_path.parent / "smoke/report.json" if "package-deb" in str(package_path) else package_path
            if smoke_path.relative_to(root).as_posix() not in bound:
                errors.append("Local full package smoke report is not bound")
            smoke = evidence.read_json(smoke_path)
            for name in evidence.package_evidence_paths(smoke, smoke_path.parent):
                if (smoke_path.parent / name).relative_to(root).as_posix() not in bound:
                    errors.append("Local full package raw reply or license material is not bound")
            errors.extend(evidence.validate_package_release(smoke, smoke_path.parent,
                model_fingerprint=semantic.get("modelFingerprint"),
                extraction_fingerprint=semantic.get("extractionFingerprint")))
        integrity = evidence.read_json(report_path.parent / "source-integrity.json")
        if (integrity.get("passed") is not True or integrity.get("tests_run", 0) < 10
                or integrity.get("skipped") != 0 or len(integrity.get("cases", [])) != integrity.get("tests_run")
                or any(item.get("status") != "passed" for item in integrity.get("cases", []))):
            errors.append("Local full source-integrity cases are incomplete")
        source_manifest = evidence.read_json(evidence.safe_file(root, "pkg/qcs-source-manifest.json"))
        if (source_manifest.get("commit") != expected_head or source_manifest.get("workingTreeModified") is not False
                or "pkg/qcs-source-manifest.json" not in bound
                or not any("-source-materials-" in name and name.endswith(".zip") for name in bound)
                or not any("-symbols-" in name and name.endswith(".zip") for name in bound)):
            errors.append("Local full source archive or debug symbols are missing or stale")
        if document["system"] == "Windows":
            native = evidence.read_json(report_path.parent / "nsis-contract.json")
            raw = (report_path.parent / "nsis-contract.log").read_text(encoding="utf-8")
            errors.extend(evidence.validate_nsis_contract(native, raw))
        result["errors"] = errors
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, ET.ParseError) as error:
        result["errors"].append(f"Cannot inspect local full check: {error}")
    result["passed"] = not result["errors"]
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["bootstrap", "doctor", "configure", "build", "test", "run", "install", "package", "verify-package", "models", "sources", "installers", "check", "release-prepare", "release-upload-draft", "clean"])
    default_root = SOURCE.parent.parent / (SOURCE.parent.name + "-compilations") / PRODUCT
    parser.add_argument("--build-root", type=Path, default=os.environ.get("QCOPILOTS_SEARCH_BUILD_ROOT") or default_root)
    parser.add_argument("--qt-root", type=Path, default=os.environ.get("QCOPILOTS_SEARCH_QT_ROOT") or None)
    parser.add_argument("--config", choices=["Release", "Debug", "RelWithDebInfo"], default="Release")
    parser.add_argument("--jobs", type=int, default=min(4, os.cpu_count() or 2))
    parser.add_argument("--model-dir", type=Path, default=os.environ.get("QCOPILOTS_SEARCH_MODEL_DIR") or None)
    parser.add_argument("--keyword-only", action="store_true", help="Partial verify-package without models, insufficient for release")
    parser.add_argument("--test-profile", choices=("fast", "full"), default="fast",
                        help="Test scope: fast excludes slow tests by default, full keeps all registered tests")
    parser.add_argument("--test-label", help="CTest label regex, intersected with the selected test profile")
    parser.add_argument("--fail-fast", action="store_true",
                        help="Stop scheduling further tests after the first failure")
    parser.add_argument("--generator", help="CPack generator, for example ZIP, NSIS, TGZ, DEB or DragNDrop")
    parser.add_argument("--package-file", type=Path, help="Specific distribution archive to verify")
    parser.add_argument("--report-dir", type=Path, help="Explicit unique report directory for this test or package verification run")
    parser.add_argument("--cmake-arg", action="append", default=[])
    parser.add_argument("--reserve-gib", type=float, default=3.0)
    parser.add_argument("--keep-failed-tests", action="store_true")
    parser.add_argument("--scope", choices=("build", "generated"), default="build", help="clean scope, models and SDKs are always preserved")
    parser.add_argument("--dry-run", action="store_true", help="Validate and list clean targets without deleting or creating files")
    parser.add_argument("--program-arg", action="append", default=[])
    parser.add_argument("--candidate-commit")
    parser.add_argument("--native-artifacts-dir", type=Path)
    parser.add_argument("--local-evidence-dir", type=Path)
    parser.add_argument("--local-full-report", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--tag")
    parser.add_argument("--title")
    args = parser.parse_args()
    if args.action in ("release-prepare", "release-upload-draft"):
        command = [sys.executable, SOURCE / "tools/qcs-release.py",
                   "prepare" if args.action == "release-prepare" else "upload-draft"]
        fields = (("candidate_commit", "native_artifacts_dir", "local_evidence_dir", "local_full_report", "output_dir")
                  if args.action == "release-prepare" else ("manifest", "tag"))
        for field in fields:
            if getattr(args, field) is None:
                parser.error("--" + field.replace("_", "-") + " is required for " + args.action)
            command.extend(["--" + field.replace("_", "-"), getattr(args, field)])
        if args.action == "release-upload-draft" and args.title:
            command.extend(["--title", args.title])
        run(command, cwd=SOURCE)
        return
    selected_root = args.build_root.expanduser().absolute()
    if selected_root.exists() and is_redirect(selected_root):
        raise RuntimeError(f"Build root cannot be a symlink or junction: {selected_root}")
    root = selected_root.resolve()
    protect_input_paths(root, args.qt_root, args.model_dir)
    if args.action == "clean":
        clean_generated(root, args.config, args.scope, args.dry_run)
        return
    if args.scope != "build" or args.dry_run:
        parser.error("--scope and --dry-run apply only to clean")
    initialize_root(root)
    with root_lock(root):
        sdk_root = (args.qt_root or root / "sdk").expanduser().resolve()
        prefix = qt_prefix(sdk_root)
        build = root / args.config.lower()
        stage = root / "stage"
        reports = root / "reports"
        reports.mkdir(exist_ok=True)
        (root / "runs").mkdir(exist_ok=True)
        environment = os.environ.copy()
        environment["QCOPILOTS_SEARCH_BUILD_ROOT"] = str(root)
        environment["QCOPILOTS_SEARCH_QT_PREFIX"] = str(prefix)
        environment["QCOPILOTS_SEARCH_TEST_ROOT"] = str(root / "runs")
        environment["PATH"] = os.pathsep.join((str(build / "bin"), str(build / "lib"), str(prefix / "bin"), str(root / "bootstrap/nsis"), environment.get("PATH", "")))
        if args.model_dir:
            environment["QCOPILOTS_SEARCH_MODEL_DIR"] = str(args.model_dir.expanduser().resolve())
        if args.keep_failed_tests:
            environment["QCOPILOTS_SEARCH_KEEP_TEST_ARTIFACTS"] = "1"
        if args.action not in ("doctor", "clean", "verify-package", "run"):
            disk_guard(root, args.reserve_gib)
        if args.action == "check":
            run_check(args, root, build, stage, reports, sdk_root, prefix, environment)
        else:
            execute_action(args.action, args, root, build, stage, reports, sdk_root, prefix, environment)


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError, ET.ParseError) as error:
        print(f"{PRODUCT}: {error}", file=sys.stderr)
        sys.exit(1)
