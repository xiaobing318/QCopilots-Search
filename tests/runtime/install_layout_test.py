#!/usr/bin/env python3
"""Test actual CMake installation of native stubs without loading a model or Qt GUI."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "support"))
from temp_paths import temporary_root


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cmake", default=shutil.which("cmake"))
    parser.add_argument("--generator", default="Ninja")
    parser.add_argument("--compiler")
    parser.add_argument("--qt-cmake-dir")
    parser.add_argument("--report", type=Path)
    options = parser.parse_args()
    if not options.cmake:
        parser.error("CMake is required")
    source = Path(__file__).resolve().parent
    repo = source.parents[1]
    fixture_root = temporary_root()
    records = []

    def run(arguments):
        result = subprocess.run(arguments, check=False, capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=180)
        if result.returncode:
            raise RuntimeError(f"{arguments!r}\n{result.stdout}\n{result.stderr}")

    with tempfile.TemporaryDirectory(prefix="qcs-install-layout-", dir=fixture_root) as root:
        for apple in (False, True):
            name = "mac-bundle" if apple else "non-bundle"
            build = Path(root) / name / "build"
            stage = Path(root) / name / "安装 空格"
            command = [options.cmake, "-S", str(source / "install_layout"), "-B", str(build),
                       "-G", options.generator, "-DCMAKE_BUILD_TYPE=Release",
                       f"-DQCOPILOTS_SEARCH_TEST_APPLE_LAYOUT={'ON' if apple else 'OFF'}",
                       f"-DQCOPILOTS_SEARCH_RUNTIME_CMAKE={repo / 'src/runtime/install_inference_runtime.cmake'}"]
            if options.compiler:
                command.append(f"-DCMAKE_C_COMPILER={options.compiler}")
            run(command)
            run([options.cmake, "--build", str(build), "--config", "Release", "--parallel", "2"])
            run([options.cmake, "--install", str(build), "--config", "Release", "--prefix", str(stage)])
            base, cpu = (build / "files.txt").read_text(encoding="utf-8").splitlines()
            if apple:
                base_dir = Path("qcs-gui.app/Contents/Frameworks")
                cpu_dir = Path("qcs-gui.app/Contents/PlugIns/ggml")
                assert (build / "cpu-rpath.txt").read_text() == "@loader_path/../../Frameworks"
                assert not (stage / base_dir / cpu).exists(), "CPU module remained in Frameworks"
                assert not (stage / "qcs-gui.app/Contents/MacOS" / cpu).exists()
            else:
                base_dir = Path("bin" if os.name == "nt" else "lib")
                cpu_dir = Path("lib")
                assert (build / "cpu-rpath.txt").read_text() == ""
            assert (stage / base_dir / base).is_file(), "Missing installed base library"
            assert (stage / cpu_dir / cpu).is_file(), "Missing installed CPU module"
            installed = {path.relative_to(stage).as_posix() for path in stage.rglob("*") if path.is_file()}
            assert installed == {(base_dir / base).as_posix(), (cpu_dir / cpu).as_posix()}, installed
            records.append({"case": name, "passed": True, "installed": sorted(installed)})
            print(f"PASS {name}: actual cmake --install files and CPU RPATH verified", flush=True)
        if sys.platform.startswith("linux"):
            from linux_deploy_test import check_linux_deployment
            records.extend(check_linux_deployment(options, source, repo, Path(root)))
        if sys.platform == "darwin":
            from macos_deploy_test import check_macos_deployment
            records.extend(check_macos_deployment(options, source, repo, Path(root)))
    if options.report:
        options.report.parent.mkdir(parents=True, exist_ok=True)
        options.report.write_text(json.dumps({"passed": True, "cases": records}, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    main()
