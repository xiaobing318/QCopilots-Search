"""Exercise Qt deployment after normal, stripped and CPack installation on Linux."""
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import time


def check_linux_deployment(options, source, repo, root):
    build = root / "linux-deploy" / "build"
    build.mkdir(parents=True)
    logs = root / "linux-deploy" / "logs"
    logs.mkdir()
    commands = []

    def run(arguments, *, env=None):
        result = subprocess.run([str(item) for item in arguments], cwd=root, env=env,
                                check=False, capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=120)
        name = f"{len(commands):02d}.log"
        (logs / name).write_text(result.stdout + result.stderr, encoding="utf-8")
        commands.append({"arguments": [str(item) for item in arguments], "exitCode": result.returncode})
        if result.returncode:
            raise RuntimeError(f"{arguments!r}\n{result.stdout}\n{result.stderr}")
        return result.stdout

    command = [options.cmake, "-S", source / "linux_deploy", "-B", build,
               "-G", options.generator, "-DCMAKE_BUILD_TYPE=Release",
               f"-DQCOPILOTS_SEARCH_LINUX_DEPLOY_CMAKE={repo / 'cmake/QCopilotsSearchLinuxDeploy.cmake'}"]
    if options.compiler:
        command.append(f"-DCMAKE_C_COMPILER={options.compiler}")
    if options.qt_cmake_dir:
        command.append(f"-DQt6_DIR={options.qt_cmake_dir}")
    run(command)
    run([options.cmake, "--build", build, "--config", "Release", "--parallel", "2"])
    files = [Path(path) for path in (build / "files-Release.txt").read_text(encoding="utf-8").splitlines()]
    owner, module, transitive = [path.name for path in files]
    # CMake's file(INSTALL) compares whole-second timestamps. Ensure a later
    # strip operation cannot accidentally share the build output timestamp.
    for path in files:
        os.utime(path, (time.time() - 10, time.time() - 10))
    readelf = shutil.which("readelf")
    cpack = Path(options.cmake).with_name("cpack")
    if not readelf or not cpack.is_file():
        raise RuntimeError("The Linux deploy regression requires readelf and CPack")
    environment = os.environ.copy()
    for key in ("LD_LIBRARY_PATH", "LD_PRELOAD", "QT_PLUGIN_PATH", "QT_QPA_PLATFORM_PLUGIN_PATH"):
        environment.pop(key, None)
    records = []

    def verify(stage, label):
        binary = stage / "bin/qcopilots-search-deploy-fixture"
        for name in (owner, module, transitive):
            if not (stage / "lib" / name).is_file():
                raise RuntimeError(f"Missing deployed transitive/owned library: {name}")
        for elf, expected in ((binary, "$ORIGIN/../lib"), (stage / "lib" / owner, "$ORIGIN"),
                              (stage / "lib" / module, "$ORIGIN")):
            dynamic = run([readelf, "--dynamic", elf])
            paths = re.findall(r"\((?:RUNPATH|RPATH)\).*?\[(.*?)\]", dynamic)
            if paths != [expected]:
                raise RuntimeError(f"Non-relocatable deployed ELF {elf}: {paths}")
        mapped = run([binary, stage / "lib" / module], env=environment).splitlines()
        if len(mapped) != 4 or any(not Path(path).resolve().is_relative_to(stage.resolve()) for path in mapped):
            raise RuntimeError(f"Deployed process escaped its installation: {mapped}")
        records.append({"case": label, "passed": True, "mappedLibraries": mapped})
        print(f"PASS {label}: Qt deployment preserves installed RPATH and indirect dependencies", flush=True)

    for label, strip in (("linux-install", False), ("linux-install-strip", True)):
        stage = root / "linux-deploy" / f"{label} 中文 空格"
        arguments = [options.cmake, "--install", build, "--config", "Release", "--prefix", stage]
        if strip:
            arguments.append("--strip")
        run(arguments)
        verify(stage, label)
    run([cpack, "--config", build / "CPackConfig.cmake", "-C", "Release", "-G", "TGZ", "-B", build / "packages"])
    archive = build / "packages/qcopilots-search-linux-deploy-fixture.tar.gz"
    unpack = root / "linux-deploy" / "package 中文 空格"
    unpack.mkdir()
    with tarfile.open(archive) as package:
        # This archive was generated immediately above from the local fixture.
        # Older supported Python 3.10 installations lack extraction filters.
        package.extractall(unpack, **({"filter": "data"} if hasattr(tarfile, "data_filter") else {}))
    verify(unpack / "qcopilots-search-linux-deploy-fixture", "linux-cpack-tgz")
    return records
