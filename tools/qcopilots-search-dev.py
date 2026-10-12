#!/usr/bin/env python3
"""Portable developer workflow. Generated files always live outside the checkout."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import uuid
import venv

SOURCE = Path(__file__).resolve().parent.parent
PRODUCT = "qcopilots-search"
MARKER = ".qcopilots-search-build-root.json"


def run(command, *, cwd=None, env=None):
    command = [str(item) for item in command]
    print("+ " + subprocess.list2cmdline(command), flush=True)
    subprocess.run(command, cwd=cwd, env=env, check=True)


def assert_external(path: Path):
    resolved = path.resolve()
    if resolved == SOURCE or SOURCE in resolved.parents:
        raise RuntimeError(f"Generated output must be outside the source tree: {resolved}")
    if resolved == Path(resolved.anchor):
        raise RuntimeError("A filesystem root cannot be used as a build root")


def initialize_root(root: Path):
    assert_external(root)
    root.mkdir(parents=True, exist_ok=True)
    marker = root / MARKER
    if marker.exists():
        original = json.loads(marker.read_text(encoding="utf-8"))
        if Path(original["source"]).resolve() != SOURCE:
            raise RuntimeError(f"Build root belongs to a different checkout: {original['source']}")
    else:
        marker.write_text(json.dumps({"source": str(SOURCE), "schemaVersion": 1}, indent=2), encoding="utf-8")


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
                  directory / "bin/qcopilots-search.app/Contents/MacOS" / name,
                  directory / "qcopilots-search.app/Contents/MacOS" / name]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["bootstrap", "doctor", "configure", "build", "test", "run", "install", "package", "verify-package", "clean"])
    default_root = SOURCE.parent.parent / (SOURCE.parent.name + "-compilations") / SOURCE.name
    parser.add_argument("--build-root", type=Path, default=os.environ.get("QCOPILOTS_SEARCH_BUILD_ROOT", default_root))
    parser.add_argument("--qt-root", type=Path, default=os.environ.get("QCOPILOTS_SEARCH_QT_ROOT"))
    parser.add_argument("--config", choices=["Release", "Debug", "RelWithDebInfo"], default="Release")
    parser.add_argument("--jobs", type=int, default=min(4, os.cpu_count() or 2))
    parser.add_argument("--model-dir", type=Path, default=os.environ.get("QCOPILOTS_SEARCH_MODEL_DIR"))
    parser.add_argument("--test-label")
    parser.add_argument("--generator", help="CPack generator, for example ZIP, NSIS, TGZ, DEB or DragNDrop")
    parser.add_argument("--package-file", type=Path, help="Specific distribution archive to verify")
    parser.add_argument("--report-dir", type=Path, help="Explicit unique report directory for this test or package verification run")
    parser.add_argument("--cmake-arg", action="append", default=[])
    parser.add_argument("--reserve-gib", type=float, default=3.0)
    parser.add_argument("--keep-failed-tests", action="store_true")
    parser.add_argument("--program-arg", action="append", default=[])
    args = parser.parse_args()
    root = args.build_root.expanduser().resolve()
    initialize_root(root)
    sdk_root = (args.qt_root or root / "sdk").expanduser().resolve()
    prefix = qt_prefix(sdk_root)
    build = root / args.config.lower()
    stage = root / "stage"
    reports = root / "reports"
    reports.mkdir(exist_ok=True)
    (root / "test-runs").mkdir(exist_ok=True)
    environment = os.environ.copy()
    environment["QCOPILOTS_SEARCH_BUILD_ROOT"] = str(root)
    environment["QCOPILOTS_SEARCH_QT_PREFIX"] = str(prefix)
    environment["QCOPILOTS_SEARCH_TEST_ROOT"] = str(root / "test-runs")
    environment["PATH"] = os.pathsep.join((str(build / "bin"), str(build / "lib"), str(prefix / "bin"), str(root / "bootstrap/nsis"), environment.get("PATH", "")))
    if args.model_dir:
        environment["QCOPILOTS_SEARCH_MODEL_DIR"] = str(args.model_dir.expanduser().resolve())
    if args.keep_failed_tests:
        environment["QCOPILOTS_SEARCH_KEEP_TEST_ARTIFACTS"] = "1"
    if args.action not in ("doctor", "clean", "verify-package", "run"):
        disk_guard(root, args.reserve_gib)
    if args.action == "bootstrap":
        tools_root = root / "bootstrap/python"
        python = tools_root / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        if not python.is_file():
            venv.EnvBuilder(with_pip=True).create(tools_root)
        run([python, "-m", "pip", "install", "py7zr==1.1.3"], cwd=root)
        run([python, SOURCE / "tools/qcopilots-search-bootstrap-qt.py", "--qt-root", sdk_root, "--cache-dir", root / "bootstrap/qt-downloads"], cwd=root)
        if os.name == "nt":
            run([python, SOURCE / "tools/qcopilots-search-bootstrap-nsis.py", "--output-dir", root / "bootstrap/nsis", "--cache-dir", root / "bootstrap/nsis-downloads"], cwd=root)
    elif args.action == "doctor":
        report = {"source": str(SOURCE), "buildRoot": str(root), "qtPrefix": str(prefix), "platform": platform.platform(), "freeGiB": round(shutil.disk_usage(root).free / 1024**3, 2), "tools": {name: shutil.which(name) for name in ("cmake", "ctest", "cpack", "ninja", "git", "cl" if os.name == "nt" else "c++")}}
        report["qtInstalled"] = (prefix / "lib/cmake/Qt6/Qt6Config.cmake").is_file()
        print(json.dumps(report, indent=2, ensure_ascii=False))
        (reports / "doctor.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        if not report["qtInstalled"] or not all(report["tools"].values()):
            raise RuntimeError("Toolchain is incomplete. Install missing tools or run bootstrap.")
    elif args.action == "configure":
        command = ["cmake", "-S", SOURCE, "-B", build, "-G", "Ninja", f"-DCMAKE_BUILD_TYPE={args.config}", f"-DCMAKE_PREFIX_PATH={prefix}", f"-DCMAKE_INSTALL_PREFIX={stage}", f"-DQCOPILOTS_SEARCH_DEPENDENCY_ROOT={root / 'dependencies'}", f"-DQCOPILOTS_SEARCH_TEST_ROOT={root / 'test-runs'}", f"-DPython3_EXECUTABLE={sys.executable}", "-DQCOPILOTS_SEARCH_BUILD_TESTS=ON"]
        if args.model_dir:
            command.append(f"-DQCOPILOTS_SEARCH_MODEL_DIR={args.model_dir.resolve()}")
        command.extend(args.cmake_arg)
        run(command, cwd=root, env=environment)
    elif args.action == "build":
        run(["cmake", "--build", build, "--config", args.config, "--parallel", args.jobs], cwd=root, env=environment)
    elif args.action == "test":
        run_id = str(uuid.uuid4())
        test_root = root / "test-runs" / run_id
        test_root.mkdir()
        environment["QCOPILOTS_SEARCH_TEST_ROOT"] = str(test_root)
        run_report = args.report_dir.expanduser().resolve() if args.report_dir else reports / run_id
        assert_external(run_report)
        run_report.mkdir(parents=True, exist_ok=True)
        (reports / "latest-test-run.json").write_text(json.dumps({"runId": run_id, "directory": str(run_report), "testRoot": str(test_root)}, indent=2), encoding="utf-8")
        command = ["ctest", "--test-dir", build, "-C", args.config, "--output-on-failure", "--no-tests=error", "--parallel", args.jobs,
                   "--test-output-size-passed", "16777216", "--test-output-size-failed", "16777216", "--test-output-truncation", "middle",
                   "--output-junit", run_report / "ctest.xml"]
        if args.test_label:
            command.extend(["-L", args.test_label])
        run(command, cwd=root, env=environment)
    elif args.action == "run":
        run([executable(build, PRODUCT), "--profile-dir", root / "development-profile", *args.program_arg], cwd=root, env=environment)
    elif args.action == "install":
        run(["cmake", "--install", build, "--config", args.config, "--prefix", stage], cwd=root, env=environment)
    elif args.action == "package":
        run([sys.executable, SOURCE / "tools/qcopilots-search-package-symbols.py",
             "--manifest", build / f"qcopilots-search-symbol-inputs-{args.config}.json",
             "--output-dir", root / "packages"], cwd=root, env=environment)
        command = ["cpack", "--config", build / "CPackConfig.cmake", "-C", args.config]
        if args.generator:
            command.extend(["-G", args.generator])
        run(command, cwd=root, env=environment)
        checksums = []
        for package in sorted((root / "packages").glob("qcopilots-search-*")):
            if package.is_file() and package.suffix != ".sha256":
                with package.open("rb") as stream:
                    hasher = hashlib.sha256()
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        hasher.update(chunk)
                    digest = hasher.hexdigest()
                checksums.append(f"{digest}  {package.name}")
        (root / "packages/SHA256SUMS").write_text("\n".join(checksums) + "\n", encoding="utf-8")
    elif args.action == "verify-package":
        command = [sys.executable, SOURCE / "tools/qcopilots-search-verify-package.py", "--build-root", root]
        if args.package_file:
            command.extend(["--package-file", args.package_file.expanduser().resolve()])
        if args.report_dir:
            command.extend(["--report-dir", args.report_dir.expanduser().resolve()])
        run(command, cwd=root)
    elif args.action == "clean":
        # Only one known build configuration is removed, never dependencies,
        # installed SDKs, profiles, arbitrary paths or the whole build root.
        target = build.resolve()
        if target.parent != root or target.name not in ("release", "debug", "relwithdebinfo") or build.is_symlink():
            raise RuntimeError(f"Refusing unsafe clean target: {target}")
        if target.exists():
            cache = target / "CMakeCache.txt"
            expected = "CMAKE_HOME_DIRECTORY:INTERNAL=" + SOURCE.as_posix()
            if not cache.is_file() or expected not in cache.read_text(encoding="utf-8").splitlines():
                raise RuntimeError(f"Cannot establish ownership of build directory: {target}")
            print(f"Removing verified generated build directory: {target}")
            shutil.rmtree(target)


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"{PRODUCT}: {error}", file=sys.stderr)
        sys.exit(1)
