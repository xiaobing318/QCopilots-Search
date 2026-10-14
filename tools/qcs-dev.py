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
import re
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
GENERATED_DIRECTORIES = frozenset({"Release", "Debug", "RelWithDebInfo", "deps", "runs", "stage", "pkg", "reports", "bootstrap", "symbols", "sources"})
PRESERVED_DIRECTORIES = frozenset({"models", "sdk", "development-profile"})
_ACTIVE_LOG = None


def build_variant(identifier=None):
    """Resolve one native configuration from the shared build matrix."""
    manifest = json.loads((SOURCE / "config/build-variants.json").read_text(encoding="utf-8"))
    variants = {item["id"]: item for item in manifest["variants"]}
    system = {"Windows": "windows", "Linux": "linux", "Darwin": "macos"}.get(platform.system())
    architecture = {"amd64": "x64", "x86_64": "x64", "aarch64": "arm64", "arm64": "arm64"}.get(platform.machine().lower())
    default = f"{system}-{architecture}-" + ("metal" if system == "macos" else "cpu")
    identifier = identifier or os.environ.get("QCOPILOTS_SEARCH_VARIANT") or default
    if identifier not in variants:
        raise ValueError("Unsupported build variant: " + identifier)
    variant = variants[identifier]
    if variant["os"] != system or variant["arch"] != architecture:
        raise ValueError(f"Build variant {identifier} does not match this native host")
    return variant


def package_directory(root, config):
    return root / "pkg" / config


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
    names = {config} if scope == "build" else GENERATED_DIRECTORIES
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
        if name in {"Release", "Debug", "RelWithDebInfo"}:
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
               "--root", root, "--stage", stage, "--package-root", package_directory(root, build.name),
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


def report_package_errors(package_root: Path, started: float):
    packaging = (package_root / "_CPack_Packages").resolve()
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


def development_tessdata(build: Path) -> Path:
    """Read the configured OCR input, never infer a dependency-cache layout."""
    cache = build / "CMakeCache.txt"
    if not cache.is_file() or is_redirect(cache):
        raise RuntimeError(f"Configure this build before running the GUI: {cache}")
    lines = cache.read_text(encoding="utf-8-sig").splitlines()

    def configured_directory(name):
        entries = [line.partition("=")[2] for line in lines
                   if line.startswith(name + ":") and "=" in line]
        if len(entries) != 1 or not entries[0] or not Path(entries[0]).is_absolute():
            raise RuntimeError(f"Expected one absolute {name} directory in {cache}. Run configure again.")
        directory = Path(entries[0]).resolve()
        if not directory.is_dir():
            raise RuntimeError(f"Configured {name} directory is missing: {directory}. Run configure again.")
        return directory

    if (configured_directory("CMAKE_HOME_DIRECTORY") != SOURCE
            or configured_directory("CMAKE_CACHEFILE_DIR") != build.resolve()):
        raise RuntimeError(f"CMake cache does not belong to this checkout and build: {cache}")
    directory = configured_directory("QCOPILOTS_SEARCH_TESSDATA_DIR")
    assert_external(directory)
    for language in ("eng", "chi_sim", "chi_tra"):
        data = directory / (language + ".traineddata")
        if not data.is_file() or data.stat().st_size == 0:
            raise RuntimeError(f"Configured OCR language data is missing or invalid: {data}. Run configure again.")
    return directory


def helper_module(filename, name):
    spec = importlib.util.spec_from_file_location(name, SOURCE / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def case_evidence(junit, inventory):
    parser = helper_module(".github/scripts/qcs-ctest-evidence.py", "qcs_case_evidence")
    return parser.inspect_junit(junit, platform.system(), inventory)


def run_tests(args, root: Path, build: Path, reports: Path, environment: dict):
    """Use CTest itself to resolve labels, then record the exact selected scope."""
    run_id = str(uuid.uuid4())
    runs = root / "runs" / args.config
    runs.mkdir(parents=True, exist_ok=True)
    test_root = Path(tempfile.mkdtemp(prefix="qcs-t-", dir=runs))
    environment["QCOPILOTS_SEARCH_TEST_ROOT"] = str(test_root)
    run_report = args.report_dir.expanduser().resolve() if args.report_dir else reports / test_root.name
    assert_external(run_report)
    run_report.mkdir(parents=True, exist_ok=True)
    latest = {"runId": run_id, "directory": str(run_report), "testRoot": str(test_root)}
    (reports / "latest-test-run.json").write_text(json.dumps(latest, indent=2), encoding="utf-8")
    filters = []
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
            raise RuntimeError("No tests match the selected label")
        if os.name == "nt" and "qcs-tests-windows-package" in selected_names:
            ensure_nsis(root, environment)
        command = [*base, "--output-on-failure", "--no-tests=error", "--parallel", str(args.jobs),
                   "--test-output-size-passed", "16777216", "--test-output-size-failed", "16777216",
                   "--test-output-truncation", "middle", "--output-junit", str(run_report / "ctest.xml"), *filters]
        if args.fail_fast:
            command.append("--stop-on-failure")
        selection["commands"].append({"purpose": "execute", "arguments": command})
        selection["status"] = "running"
        save()
        print(f"CTest: {len(selected)} selected, "
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
        evidence = case_evidence(run_report / "ctest.xml", run_report / "test-selected-inventory.json")
        (run_report / "qt-test-cases.json").write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if not evidence["testsComplete"]:
            raise RuntimeError("Selected test case evidence is incomplete: " + ", ".join(evidence["errors"]))
        selection.update(status="passed", passed=True, exitCode=0,
                         fullSuitePassed=not args.test_label and not selection["excludedTests"])
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError, ET.ParseError) as error:
        selection.update(status="failed", error=str(error),
                         exitCode=error.returncode if isinstance(error, subprocess.CalledProcessError) else 1)
        raise
    finally:
        if (run_report / "ctest.xml").is_file() and not (run_report / "qt-test-cases.json").is_file():
            diagnostic = case_evidence(run_report / "ctest.xml", run_report / "test-selected-inventory.json")
            (run_report / "qt-test-cases.json").write_text(
                json.dumps(diagnostic, ensure_ascii=False, indent=2), encoding="utf-8")
        save()
    return run_report


def cpack_setting(root, config, name):
    text = (root / config / "CPackConfig.cmake").read_text(encoding="utf-8-sig")
    values = re.findall(r'^\s*set\(' + re.escape(name) + r'\s+"([^"\r\n]+)"\)\s*$', text, re.MULTILINE)
    if len(values) != 1:
        raise RuntimeError(f"Expected one literal {name} in the current CPack configuration")
    return values[0]


def package_path(root, suffix, config):
    name = cpack_setting(root, config, "CPACK_PACKAGE_FILE_NAME")
    if not re.fullmatch(r"qcs-[A-Za-z0-9_.+-]+", name):
        raise RuntimeError("The current CPack package filename is unsafe")
    package = package_directory(root, config) / (name + suffix)
    if suffix == ".exe" and not package.is_file():
        fallback = package.with_name(name + "-setup.zip")
        if fallback.is_file():
            return fallback
    return package


def package_for_suffix(root, suffix, config="Release"):
    package = package_path(root, suffix, config)
    if not package.is_file():
        raise RuntimeError("Current candidate package is missing: " + str(package))
    return package


def candidate_packages(root, config):
    suffixes = {"NSIS": ".exe", "ZIP": ".zip", "TGZ": ".tar.gz", "DEB": ".deb", "DragNDrop": ".dmg"}
    generators = cpack_setting(root, config, "CPACK_GENERATOR").split(";")
    return [path for generator in generators
            if generator in suffixes and (path := package_path(root, suffixes[generator], config)).is_file()]


def package_generator(root, build, config, generator, environment):
    """Publish only this generator's finished candidate, then release its stage."""
    suffixes = {"ZIP": ".zip", "TGZ": ".tar.gz", "DEB": ".deb", "DragNDrop": ".dmg"}
    if generator not in suffixes:
        raise ValueError("Unsupported package generator: " + generator)
    destination = package_path(root, suffixes[generator], config)
    output = destination.parent.resolve()
    if destination.parent.absolute() != output or is_redirect(output):
        raise RuntimeError("Package output directory is redirected: " + str(destination.parent))
    temporary = Path(tempfile.mkdtemp(prefix="qcs-cpack-", dir=output)).absolute()
    identity = (temporary.stat().st_dev, temporary.stat().st_ino)

    def check_owned_directory():
        if (temporary.parent != output or temporary.resolve() != temporary or is_redirect(temporary)
                or (temporary.stat().st_dev, temporary.stat().st_ino) != identity):
            raise RuntimeError("Refusing to use or remove a changed CPack temporary directory: " + str(temporary))

    def remove_readonly(function, filename, failure):
        target = Path(filename).absolute()
        check_owned_directory()
        if (not isinstance(failure[1], PermissionError) or function not in (os.unlink, os.rmdir)
                or (target != temporary and temporary not in target.parents)
                or target.resolve() != target or is_redirect(target)):
            raise failure[1]
        target.chmod(target.stat().st_mode | stat.S_IWRITE)
        function(filename)

    started = time.time()
    try:
        check_owned_directory()
        run(["cpack", "--config", build / "CPackConfig.cmake", "-C", config,
             "-B", temporary, "-G", generator], cwd=root, env=environment)
        result = temporary / destination.name
        if not result.is_file() or is_redirect(result) or result.resolve() != result:
            raise RuntimeError("CPack did not produce the current candidate package: " + str(result))
        check_owned_directory()
        os.replace(result, destination)
        return destination
    except subprocess.CalledProcessError:
        report_package_errors(temporary, started)
        raise
    finally:
        # Never enumerate historical CPack roots or another invocation's stage.
        # Framework/library symlinks may be unlinked but must not redirect cleanup.
        check_owned_directory()
        verify_plain_tree(temporary)
        shutil.rmtree(temporary, onerror=remove_readonly)


def ensure_nsis(root, environment):
    """Prepare the pinned compiler before native tests or installer packaging."""
    python = root / "bootstrap/python/Scripts/python.exe"
    if not python.is_file():
        venv.EnvBuilder(with_pip=True).create(python.parent.parent)
    run([python, "-m", "pip", "install", "py7zr==1.1.3"], cwd=root, env=environment)
    run([python, SOURCE / "tools/qcs-bootstrap-nsis.py", "--output-dir", root / "bootstrap/nsis",
         "--cache-dir", root / "bootstrap/nsis-downloads"], cwd=root, env=environment)
    environment["QCOPILOTS_SEARCH_NSIS_EXECUTABLE"] = str(root / "bootstrap/nsis/makensis.exe")
    environment["PATH"] = str(root / "bootstrap/nsis") + os.pathsep + environment.get("PATH", "")


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


def gpu_sdk(args, root, reports, environment, *, install=False):
    variant = build_variant(args.variant)
    if variant["backend"] in ("cpu", "metal"):
        return {}
    sdk_root = getattr(args, "gpu_sdk_root", None) or root.parent / "gpu-sdk"
    sdk_root = Path(sdk_root).expanduser().resolve()
    assert_external(sdk_root)
    manifest = root / "reports" / args.config / "gpu-sdk.json"
    command = [sys.executable, "-B", SOURCE / "tools/qcs-gpu-sdk.py", "--variant", args.variant,
               "--sdk-root", sdk_root, "--build-root", root / args.config, "--output", manifest]
    if install:
        command.append("--install")
    run(command, cwd=root, env=environment)
    configuration = json.loads(manifest.read_text(encoding="utf-8"))
    if configuration.get("variant") != args.variant:
        raise RuntimeError("GPU SDK manifest belongs to another build variant")
    environment.update(configuration["environment"])
    return {**configuration["cmakeCache"], "QCOPILOTS_SEARCH_GPU_SDK_MANIFEST": str(manifest)}


def execute_action(action, args, root, build, stage, reports, sdk_root, prefix, environment):
    """The only implementation of local and CI single-stage operations."""
    packages = package_directory(root, args.config)
    if action == "bootstrap":
        tools_root = root / "bootstrap/python"
        python = tools_root / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        if not python.is_file():
            venv.EnvBuilder(with_pip=True).create(tools_root)
        run([python, "-m", "pip", "install", "py7zr==1.1.3"], cwd=root)
        run([python, SOURCE / "tools/qcs-bootstrap-qt.py", "--qt-root", sdk_root, "--cache-dir", root / "bootstrap/qt-downloads"], cwd=root)
        gpu_sdk(args, root, reports, environment, install=True)
    elif action == "doctor":
        report = {"source": str(SOURCE), "buildRoot": str(root), "qtPrefix": str(prefix), "platform": platform.platform(), "freeGiB": round(shutil.disk_usage(root).free / 1024**3, 2), "tools": {name: shutil.which(name) for name in ("cmake", "ctest", "cpack", "ninja", "git", "cl" if os.name == "nt" else "c++")}}
        report["qtInstalled"] = (prefix / "lib/cmake/Qt6/Qt6Config.cmake").is_file()
        print(json.dumps(report, indent=2, ensure_ascii=False))
        (reports / "doctor.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        check_path_budget(root, build, stage, reports, configured=False)
        if not report["qtInstalled"] or not all(report["tools"].values()):
            raise RuntimeError("Toolchain is incomplete. Install missing tools or run bootstrap.")
    elif action == "configure":
        command = ["cmake", "-S", SOURCE, "-B", build, "-G", "Ninja", f"-DCMAKE_BUILD_TYPE={args.config}", f"-DCMAKE_PREFIX_PATH={prefix}", f"-DCMAKE_INSTALL_PREFIX={stage}", f"-DCPACK_PACKAGE_DIRECTORY={packages}", f"-DQCOPILOTS_SEARCH_DEPENDENCY_ROOT={root / 'deps' / args.config}", f"-DQCOPILOTS_SEARCH_TEST_ROOT={root / 'runs' / args.config}", f"-DPython3_EXECUTABLE={sys.executable}", "-DQCOPILOTS_SEARCH_BUILD_TESTS=ON", f"-DQCOPILOTS_SEARCH_VARIANT={args.variant}"]
        for key, value in gpu_sdk(args, root, reports, environment).items():
            if key == "CMAKE_PREFIX_PATH":
                value = str(prefix) + ";" + str(value)
            command.append(f"-D{key}={value}")
        candidate = getattr(args, "candidate_commit", None) or environment.get("QCOPILOTS_SEARCH_CANDIDATE_COMMIT") or environment.get("QCOPILOTS_SEARCH_CANDIDATE_HEAD")
        attempt = getattr(args, "attempt", None) or environment.get("QCOPILOTS_SEARCH_CANDIDATE_ATTEMPT")
        if candidate:
            command.append(f"-DQCOPILOTS_SEARCH_CANDIDATE_COMMIT={candidate}")
        if attempt:
            command.append(f"-DQCOPILOTS_SEARCH_CANDIDATE_ATTEMPT={attempt}")
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
        runtime_environment = dict(environment, QCOPILOTS_SEARCH_TESSDATA_DIR=str(development_tessdata(build)))
        runtime_root = build
        if build_variant(args.variant)["backend"] != "cpu":
            # Installation deploys the GPU manifest and its complete runtime dependencies.
            run(["cmake", "--install", build, "--config", args.config, "--prefix", stage], cwd=root, env=environment)
            runtime_root = stage
        run([executable(runtime_root, GUI_EXECUTABLE), "--profile-dir", root / "development-profile", *args.program_arg], cwd=root, env=runtime_environment)
    elif action == "install":
        run(["cmake", "--install", build, "--config", args.config, "--prefix", stage], cwd=root, env=environment)
    elif action == "package":
        if os.name == "nt" and (not args.generator or "NSIS" in args.generator.split(";")):
            ensure_nsis(root, environment)
            nsis_contract(root, reports, environment)
        check_path_budget(root, build, stage, reports, configured=True)
        packages.mkdir(parents=True, exist_ok=True)
        generators = args.generator.split(";") if args.generator else build_variant(args.variant)["generators"]
        for generator in generators:
            if os.name == "nt" and generator == "NSIS":
                command = [sys.executable, "-B", SOURCE / "tools/qcs-package-windows.py",
                           "--build-dir", build, "--package-dir", packages,
                           "--config", args.config, "--variant", args.variant]
                run(command, cwd=root, env=environment)
            else:
                package_generator(root, build, args.config, generator, environment)
        checksums = []
        for package in sorted(candidate_packages(root, args.config)):
            if package.is_file() and package.suffix != ".sha256":
                with package.open("rb") as stream:
                    hasher = hashlib.sha256()
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        hasher.update(chunk)
                    digest = hasher.hexdigest()
                checksums.append(f"{digest}  {package.name}")
        (packages / "SHA256SUMS").write_text("\n".join(checksums) + "\n", encoding="utf-8")
    elif action == "symbols":
        run([sys.executable, SOURCE / "tools/qcs-package-symbols.py",
             "--manifest", build / f"qcs-symbol-inputs-{args.config}.json",
             "--output-dir", root / "symbols" / args.config], cwd=root, env=environment)
    elif action == "verify-package":
        command = [sys.executable, SOURCE / "tools/qcs-verify-package.py", "--build-root", root, "--variant", args.variant]
        if args.keyword_only:
            command.append("--keyword-only")
        elif args.model_dir:
            command.extend(["--model-dir", args.model_dir.expanduser().resolve()])
        package = args.package_file or package_for_suffix(root, {"Windows": ".zip", "Linux": ".tar.gz", "Darwin": ".dmg"}[platform.system()], args.config)
        command.extend(["--package-file", package.expanduser().resolve()])
        report_dir = args.report_dir.expanduser().resolve() if args.report_dir else reports / ("package-" + uuid.uuid4().hex)
        command.extend(["--report-dir", report_dir])
        package_environment = dict(environment, QCOPILOTS_SEARCH_TEST_ROOT=str(root / "runs" / args.config))
        run(command, cwd=root, env=package_environment)
    elif action == "models":
        models = (args.model_dir or root.parent / "models").expanduser().resolve()
        assert_external(models)
        models.mkdir(parents=True, exist_ok=True)
        common = [executable(build, "qcs-cli"), "--model-dir", models,
                  "--profile-dir", root / "runs" / args.config / "model-management"]
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
             "--qt-root", sdk_root, "--output-dir", root / "sources" / args.config, "--download"], cwd=root, env=environment)
        archives = [root / "deps/sources/llama.cpp-b11461.tar.gz", root / "deps" / args.config / "llama-b11461.tar.gz"]
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
            installer_args.package_file = package_for_suffix(root, ".exe", args.config)
            installer_args.report_dir = reports / "package-nsis"
            execute_action("verify-package", installer_args, root, build, stage, reports, sdk_root, prefix, environment)
        elif platform.system() == "Linux":
            evidence = helper_module("tools/qcs-release-evidence.py", "qcs_installer_identity")
            identity = evidence.source_identity(SOURCE)
            candidate = environment.get("QCOPILOTS_SEARCH_CANDIDATE_HEAD")
            if candidate and candidate != identity["checkoutCommit"]:
                raise RuntimeError("Installer candidate differs from the current source checkout")
            installer_environment = dict(environment, QCOPILOTS_SEARCH_CANDIDATE_HEAD=identity["checkoutCommit"])
            run([sys.executable, SOURCE / ".github/scripts/qcs-deb-ci.py", "--build-root", root,
                 "--package-file", package_for_suffix(root, ".deb", args.config), "--model-dir", args.model_dir or root.parent / "models",
                 "--report-dir", reports / "package-deb"], cwd=root, env=installer_environment)
        else:
            raise RuntimeError("This platform has no additional installer beyond its DMG package verification")


def check_plan(system):
    stages = [(action, None) for action in ("doctor", "configure", "build", "models", "test", "install")]
    stages.extend(("package", generator) for generator in {
        "Windows": ("NSIS", "ZIP"), "Linux": ("TGZ", "DEB"), "Darwin": ("DragNDrop",)}[system])
    stages.append(("verify-package", None))
    if system in ("Windows", "Linux"):
        stages.append(("installers", None))
    return stages


def file_snapshot(paths):
    records = {}
    for name, path in sorted(paths.items()):
        with path.open("rb") as stream:
            hasher = hashlib.sha256()
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                hasher.update(block)
        records[name] = {"bytes": path.stat().st_size, "sha256": hasher.hexdigest()}
    return records


def source_snapshot():
    listing = subprocess.check_output(["git", "-C", str(SOURCE), "ls-files", "--cached", "--others", "--exclude-standard", "-z"])
    names = set(listing.decode("utf-8").split("\0")) - {""}
    return file_snapshot({name: SOURCE / name for name in names if (SOURCE / name).is_file()})


def executable_snapshot(build):
    return file_snapshot({name: executable(build, name) for name in (GUI_EXECUTABLE, "qcs-service", "qcs-worker", "qcs-cli")})


def runtime_snapshot(build):
    paths = {}
    for folder in (build / "bin", build / "lib"):
        if folder.is_dir():
            for path in folder.rglob("*"):
                if path.is_file() and (path.suffix.lower() in (".dll", ".dylib") or re.search(r"\.so(?:\.\d+)*$", path.name)):
                    paths[path.relative_to(build).as_posix()] = path
    return file_snapshot(paths)


def run_check(args, root, build, stage, reports, sdk_root, prefix, environment):
    global _ACTIVE_LOG
    if args.test_label or args.keyword_only or args.generator or args.package_file:
        raise ValueError("check requires the entire test suite. Use individual actions for filtered work.")
    directory = args.report_dir.expanduser().resolve() if args.report_dir else Path(tempfile.mkdtemp(prefix="check-", dir=reports))
    assert_external(directory)
    if root not in directory.parents:
        raise ValueError("check report directory must be inside this external build root for portable evidence")
    if directory.exists() and any(directory.iterdir()):
        raise RuntimeError("check requires an empty, unique report directory")
    directory.mkdir(parents=True, exist_ok=True)
    evidence = helper_module("tools/qcs-release-evidence.py", "qcs_check_release")
    identity = evidence.source_identity(SOURCE)
    original_sources = source_snapshot()
    document = {"schemaVersion": 2, "kind": "developer-check", "variant": args.variant, "config": args.config,
                "system": platform.system(), "buildRoot": str(root), "identity": identity,
                "passed": False, "releaseComplete": False, "startedAt": datetime.now(timezone.utc).isoformat(),
                "stages": [{"action": action, "generator": generator, "status": "notRun"}
                           for action, generator in check_plan(platform.system())], "files": [],
                "sourceFiles": original_sources, "executables": {}, "runtimeFiles": {}, "packages": {}, "stability": {}}
    report_path = directory / "check-report.json"
    document["reportPath"] = report_path.relative_to(root).as_posix()
    evidence.write_json(reports / "latest-check.json", {"report": str(report_path), "variant": args.variant})
    local_args = copy.copy(args)
    local_args.model_dir = args.model_dir or root.parent / "models"
    environment = dict(environment, QCOPILOTS_SEARCH_MODEL_DIR=str(local_args.model_dir.resolve()),
                       QCOPILOTS_SEARCH_SEMANTIC_REPORT=str(directory / "semantic-functional.json"))
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
                if record["action"] == "build":
                    document["executables"] = executable_snapshot(build)
                    document["runtimeFiles"] = runtime_snapshot(build)
                if record["action"] == "package":
                    suffix = {"ZIP": ".zip", "NSIS": ".exe", "TGZ": ".tar.gz", "DEB": ".deb", "DragNDrop": ".dmg"}[record["generator"]]
                    package = package_for_suffix(root, suffix, args.config)
                    paths = {name: root / name for name in document["packages"]}
                    paths[package.relative_to(root).as_posix()] = package
                    for name, fingerprint in file_snapshot(paths).items():
                        if name in document["packages"] and document["packages"][name] != fingerprint:
                            raise RuntimeError("An earlier package changed during a later packaging stage: " + name)
                        document["packages"].setdefault(name, fingerprint)
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
        document["stability"] = {
            "sourceUnchanged": source_snapshot() == original_sources,
            "binariesUnchanged": executable_snapshot(build) == document["executables"],
            "runtimeUnchanged": runtime_snapshot(build) == document["runtimeFiles"],
            "packagesUnchanged": file_snapshot({name: root / name for name in document["packages"]}) == document["packages"],
        }
        if not all(document["stability"].values()):
            raise RuntimeError("Source, executable or package bytes changed during the developer check")
        document["passed"] = True
    finally:
        _ACTIVE_LOG = None
        document["finishedAt"] = datetime.now(timezone.utc).isoformat()
        paths = [path for path in directory.rglob("*") if path.is_file() and path != report_path]
        paths.extend(root / name for name in document["packages"] if (root / name).is_file())
        sdk_manifest = root / "reports" / args.config / "gpu-sdk.json"
        if sdk_manifest.is_file():
            paths.append(sdk_manifest)
        if (package_directory(root, args.config) / "SHA256SUMS").is_file():
            paths.append(package_directory(root, args.config) / "SHA256SUMS")
        document["files"] = [evidence.file_record(root, path) for path in sorted(set(paths))]
        evidence.write_json(report_path, document)
        print(f"Developer check report: {report_path}")
    return report_path


def inspect_check(report_path: Path, expected_head: str):
    """Revalidate the original complete developer check and bound evidence."""
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
        if (document.get("kind") != "developer-check" or document.get("schemaVersion") != 2
                or document.get("passed") is not True or document.get("releaseComplete") is not False
                or document["identity"].get("candidateHead") != expected_head
                or document["identity"].get("checkoutCommit") != expected_head):
            errors.append("Local check is not a successful complete check of the immutable candidate")
        errors.extend(evidence.valid_identity(document["identity"]))
        if (not document.get("sourceFiles") or set(document.get("executables", {})) != {GUI_EXECUTABLE, "qcs-service", "qcs-worker", "qcs-cli"}
                or not document.get("packages")
                or "runtimeFiles" not in document
                or document.get("stability") != {"sourceUnchanged": True, "binariesUnchanged": True, "runtimeUnchanged": True, "packagesUnchanged": True}):
            errors.append("Local check source, executable and package stability evidence is incomplete")
        expected = check_plan(document["system"])
        stages = document.get("stages", [])
        if [(item.get("action"), item.get("generator")) for item in stages] != expected:
            errors.append("Local check stages are incomplete or changed")
        bound = {item["path"] for item in document["files"]}
        required = [report_path.parent / "tests" / name for name in (
            "test-selection.json", "test-inventory.json", "test-selected-inventory.json", "ctest.xml", "qt-test-cases.json")]
        required += [report_path.parent / name for name in (
            "models.json", "models-manifest.json", "semantic-functional.json", "package/report.json")]
        if document["system"] == "Windows":
            required += [report_path.parent / name for name in ("nsis-contract.json", "nsis-contract.log", "package-nsis/report.json")]
        if document["system"] == "Linux":
            required.append(report_path.parent / "package-deb/report.json")
        if not {path.relative_to(root).as_posix() for path in required}.issubset(bound):
            errors.append("Local check omits required bound original evidence")
        for item in stages:
            if (item.get("status") != "passed" or item.get("exitCode") != 0
                    or item.get("log") not in bound or item.get("elapsedSeconds", -1) < 0):
                errors.append("Local stage failed, did not run, or lacks its bound raw log")
        selected = evidence.read_json(report_path.parent / "tests/test-selection.json")
        if selected.get("fullSuitePassed") is not True or selected.get("excludedTests"):
            errors.append("Local CTest selection is incomplete")
        parser = helper_module(".github/scripts/qcs-ctest-evidence.py", "qcs_complete_cases")
        cases = parser.inspect_junit(report_path.parent / "tests/ctest.xml", document["system"],
                                     report_path.parent / "tests/test-inventory.json")
        errors.extend(cases["errors"])
        models = evidence.read_json(report_path.parent / "models.json")
        model_manifest = evidence.read_json(report_path.parent / "models-manifest.json")
        actual_models = models.get("result", {}).get("artifacts", [])
        expected_models = model_manifest.get("result", {}).get("artifacts", [])
        pins = evidence.package_contract()[0]
        if (models.get("ok") is not True or models.get("result", {}).get("valid") is not True
                or len(actual_models) != len(pins) or len(expected_models) != len(pins)
                or {item.get("name") for item in actual_models} != set(pins)):
            errors.append("Local model verification is incomplete")
        for item in actual_models:
            if item.get("present") is not True or (item.get("bytes"), item.get("sha256")) != pins.get(item.get("name")):
                errors.append("Local model bytes differ from the immutable model lock")
        for item in expected_models:
            if (item.get("size"), item.get("sha256")) != pins.get(item.get("name")):
                errors.append("Local model manifest differs from the immutable model lock")
        semantic = evidence.read_json(report_path.parent / "semantic-functional.json")
        errors.extend(evidence.validate_semantic(semantic))
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
                errors.append("Local package result is incomplete, stale or unbound")
            smoke_path = package_path.parent / "smoke/report.json" if "package-deb" in str(package_path) else package_path
            if smoke_path.relative_to(root).as_posix() not in bound:
                errors.append("Local package smoke report is not bound")
            smoke = evidence.read_json(smoke_path)
            for name in evidence.package_evidence_paths(smoke, smoke_path.parent):
                if (smoke_path.parent / name).relative_to(root).as_posix() not in bound:
                    errors.append("Local package raw reply or license material is not bound")
            errors.extend(evidence.validate_package_release(smoke, smoke_path.parent,
                model_fingerprint=semantic.get("modelFingerprint"),
                extraction_fingerprint=semantic.get("extractionFingerprint"), platform_id=document["variant"],
                worker_sha256=document.get("executables", {}).get("qcs-worker", {}).get("sha256")))
        if document["system"] == "Windows":
            native = evidence.read_json(report_path.parent / "nsis-contract.json")
            raw = (report_path.parent / "nsis-contract.log").read_text(encoding="utf-8")
            errors.extend(evidence.validate_nsis_contract(native, raw))
        result["errors"] = errors
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, ET.ParseError) as error:
        result["errors"].append(f"Cannot inspect local check: {error}")
    result["passed"] = not result["errors"]
    return result


def main():
    if len(sys.argv) > 1 and sys.argv[1] in ("release-prepare", "release-upload-draft"):
        action = "prepare" if sys.argv[1] == "release-prepare" else "upload-draft"
        run([sys.executable, SOURCE / "tools/qcs-release.py", action, *sys.argv[2:]], cwd=SOURCE)
        return
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["bootstrap", "doctor", "configure", "build", "test", "run", "install", "package", "verify-package", "models", "sources", "symbols", "installers", "check", "release-prepare", "release-upload-draft", "clean"])
    default_root = SOURCE.parent.parent / (SOURCE.parent.name + "-compilations") / PRODUCT
    parser.add_argument("--build-root", type=Path, default=os.environ.get("QCOPILOTS_SEARCH_BUILD_ROOT") or default_root)
    parser.add_argument("--variant", default=os.environ.get("QCOPILOTS_SEARCH_VARIANT") or None,
                        help="Native configuration from config/build-variants.json")
    parser.add_argument("--qt-root", type=Path, default=os.environ.get("QCOPILOTS_SEARCH_QT_ROOT") or None)
    parser.add_argument("--gpu-sdk-root", type=Path, default=os.environ.get("QCOPILOTS_SEARCH_GPU_SDK_ROOT") or None)
    parser.add_argument("--config", choices=["Release", "Debug", "RelWithDebInfo"], default="Release")
    parser.add_argument("--jobs", type=int, default=min(4, os.cpu_count() or 2))
    parser.add_argument("--model-dir", type=Path, default=os.environ.get("QCOPILOTS_SEARCH_MODEL_DIR") or None)
    parser.add_argument("--keyword-only", action="store_true", help="Partial verify-package without models, insufficient for release")
    parser.add_argument("--test-label", help="CTest label regex for a partial developer run, never a complete check")
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
    parser.add_argument("--attempt", type=int)
    args = parser.parse_args()
    args.variant = build_variant(args.variant)["id"]
    if args.candidate_commit and not re.fullmatch(r"[0-9a-f]{40}", args.candidate_commit):
        parser.error("--candidate-commit must be a full Git commit SHA")
    if args.attempt is not None and args.attempt < 1:
        parser.error("--attempt must be a positive integer")
    selected_root = args.build_root.expanduser().absolute()
    if selected_root.exists() and is_redirect(selected_root):
        raise RuntimeError(f"Build root cannot be a symlink or junction: {selected_root}")
    parent = selected_root.resolve()
    assert_external(parent)
    root = parent / args.variant
    args.model_dir = (args.model_dir or parent / "models").expanduser().resolve()
    protect_input_paths(root, args.qt_root, args.model_dir, args.gpu_sdk_root)
    if args.action == "clean":
        clean_generated(root, args.config, args.scope, args.dry_run)
        return
    if args.scope != "build" or args.dry_run:
        parser.error("--scope and --dry-run apply only to clean")
    initialize_root(root)
    with root_lock(root):
        sdk_root = (args.qt_root or parent / "sdk").expanduser().resolve()
        prefix = qt_prefix(sdk_root)
        build = root / args.config
        stage = root / "stage" / args.config
        reports = root / "reports" / args.config
        reports.mkdir(parents=True, exist_ok=True)
        (root / "runs" / args.config).mkdir(parents=True, exist_ok=True)
        environment = os.environ.copy()
        environment["QCOPILOTS_SEARCH_BUILD_ROOT"] = str(parent)
        environment["QCOPILOTS_SEARCH_VARIANT_ROOT"] = str(root)
        environment["QCOPILOTS_SEARCH_VARIANT"] = args.variant
        environment["QCOPILOTS_SEARCH_QT_PREFIX"] = str(prefix)
        environment["QCOPILOTS_SEARCH_TEST_ROOT"] = str(root / "runs" / args.config)
        if args.candidate_commit:
            environment["QCOPILOTS_SEARCH_CANDIDATE_COMMIT"] = args.candidate_commit
        if args.attempt:
            environment["QCOPILOTS_SEARCH_CANDIDATE_ATTEMPT"] = str(args.attempt)
        environment["PATH"] = os.pathsep.join((str(build / "bin"), str(build / "lib"), str(prefix / "bin"), str(root / "bootstrap/nsis"), environment.get("PATH", "")))
        if build_variant(args.variant)["backend"] not in ("cpu", "metal") and args.action not in ("bootstrap", "configure", "doctor", "check"):
            sdk_manifest = reports / "gpu-sdk.json"
            if not sdk_manifest.is_file():
                raise RuntimeError("Configure this variant first to capture its GPU SDK environment")
            sdk_configuration = json.loads(sdk_manifest.read_text(encoding="utf-8"))
            if sdk_configuration.get("variant") != args.variant:
                raise RuntimeError("GPU SDK environment belongs to another variant")
            environment.update(sdk_configuration["environment"])
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
