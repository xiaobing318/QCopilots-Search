"""Exercise the real macOS bundle overwrite, deployment and relocation sequence."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time


def check_macos_deployment(options, source, repo, root):
    if sys.platform != "darwin":
        raise RuntimeError("The macOS deployment regression requires native macOS")
    fixture_root = (root / "macos-deploy").resolve()
    build = fixture_root / "build"
    logs = fixture_root / "logs"
    logs.mkdir(parents=True)
    commands = []
    records = []

    def run(arguments, *, env=None):
        arguments = [str(item) for item in arguments]
        result = subprocess.run(arguments, cwd=root, env=env, check=False, capture_output=True,
                                text=True, encoding="utf-8", errors="replace", timeout=90)
        name = f"{len(commands):02d}.log"
        (logs / name).write_text(result.stdout + result.stderr, encoding="utf-8")
        commands.append({"arguments": arguments, "exitCode": result.returncode, "log": name})
        (logs / "commands.json").write_text(json.dumps(commands, ensure_ascii=False, indent=2) + "\n",
                                             encoding="utf-8")
        if result.returncode:
            raise RuntimeError(f"{arguments!r}\n{result.stdout}\n{result.stderr}")
        return result.stdout

    def verify(stage, label, names):
        # Relocate before inspection and execution. Runtime paths must not rely
        # on either the original staging directory or the fixture build tree.
        relocated = fixture_root / f"{label}-relocated"
        stage.rename(relocated)
        bundle = relocated / "qcs-deploy-fixture.app"
        frameworks = bundle / "Contents/Frameworks"
        binary = bundle / "Contents/MacOS/qcs-deploy-fixture"
        evidence = bundle / "Contents/Resources/qcs-before-runtime-finalization.txt"
        if str(build) not in evidence.read_text(encoding="utf-8"):
            raise RuntimeError("Missing evidence that development installation overwrote the library")
        aliases = []
        for full_name, soname in names:
            full, alias = frameworks / full_name, frameworks / soname
            if not full.is_file() or not alias.is_file():
                raise RuntimeError(f"Missing versioned library or SONAME alias: {full}, {alias}")
            aliases.append({"versioned": full_name, "soname": soname, "sameFile": full.samefile(alias)})
        macho = {binary.resolve()}
        magics = {b"\xfe\xed\xfa\xce", b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xcf",
                  b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca",
                  b"\xca\xfe\xba\xbf", b"\xbf\xba\xfe\xca"}
        for path in frameworks.rglob("*"):
            if path.is_file():
                resolved = path.resolve()
                if not resolved.is_relative_to(bundle.resolve()):
                    raise RuntimeError(f"Framework symlink escapes the installed bundle: {path}")
                with path.open("rb") as stream:
                    if stream.read(4) in magics:
                        macho.add(resolved)
        inspections = []
        for path in sorted(macho):
            load_commands = run(["/usr/bin/otool", "-l", path])
            rpaths = re.findall(r"cmd LC_RPATH\s+cmdsize \d+\s+path (.*?) \(offset", load_commands)
            if any(not value.startswith(("@loader_path", "@executable_path")) for value in rpaths):
                raise RuntimeError(f"Non-relocatable Mach-O RPATH in {path}: {rpaths}")
            libraries = run(["/usr/bin/otool", "-L", path])
            dependencies = re.findall(r"^\s+(.*?) \(compatibility version", libraries, flags=re.MULTILINE)
            if any(not value.startswith(("@rpath/", "@loader_path/", "@executable_path/",
                                         "/System/Library/", "/usr/lib/")) for value in dependencies):
                raise RuntimeError(f"External non-system Mach-O dependency in {path}: {dependencies}")
            inspections.append({"file": path.relative_to(bundle).as_posix(), "rpaths": rpaths,
                                "dependencies": dependencies})
        # macdeployqt deliberately leaves signing to the final production pass.
        # Apple Silicon requires valid ad-hoc seals after install_name_tool edits.
        run([sys.executable, "-B", repo / "tools/qcs-sign-macos.py", "--bundle", bundle,
             "--identity=-", "--report", logs / f"{label}-signing.json"])
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith(("DYLD_", "LD_", "QT_", "QML"))}
        environment["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin"
        hidden = fixture_root / "build-unavailable"
        build.rename(hidden)
        try:
            mapped = run([binary], env=environment).splitlines()
        finally:
            hidden.rename(build)
        if len(mapped) != 3 or any(not Path(path).resolve().is_relative_to(bundle.resolve()) for path in mapped):
            raise RuntimeError(f"Relocated process did not load its owned libraries and Qt from the bundle: {mapped}")
        records.append({"case": label, "passed": True, "mappedLibraries": mapped,
                        "aliases": aliases, "machO": inspections})
        print(f"PASS {label}: actual Qt deployment, all Mach-O paths and relocated execution verified", flush=True)

    try:
        command = [options.cmake, "-S", source / "macos_deploy", "-B", build,
                   "-G", options.generator, "-DCMAKE_BUILD_TYPE=Release",
                   f"-DQCOPILOTS_SEARCH_MACOS_DEPLOY_CMAKE={repo / 'cmake/qcs-macos-deploy.cmake'}"]
        if options.compiler:
            command.append(f"-DCMAKE_C_COMPILER={options.compiler}")
        if options.qt_cmake_dir:
            command.append(f"-DQt6_DIR={options.qt_cmake_dir}")
        run(command)
        run([options.cmake, "--build", build, "--config", "Release", "--parallel", "2"])
        owner, transitive, owner_soname, transitive_soname, development_bundle = (
            build / "files-Release.txt").read_text(encoding="utf-8").splitlines()
        names = ((Path(owner).name, owner_soname), (Path(transitive).name, transitive_soname))
        # Give originals and development copies exactly the same past-second
        # timestamp. After the bundle overwrites the first target installation,
        # the final target install must fix RPATH even when copying is skipped
        # as up to date. The install observer also asserts this precondition.
        stamp = int(time.time()) - 20
        for original in (Path(owner), Path(transitive)):
            copied = Path(development_bundle) / "Contents/Frameworks" / original.name
            os.utime(original, (stamp, stamp))
            os.utime(copied, (stamp, stamp))
        stage = fixture_root / "install-original"
        run([options.cmake, "--install", build, "--config", "Release", "--prefix", stage])
        verify(stage, "macos-install", names)
        cpack = Path(options.cmake).with_name("cpack")
        if not cpack.is_file():
            raise RuntimeError("The macOS deploy regression requires CPack beside CMake")
        run([cpack, "--config", build / "CPackConfig.cmake", "-C", "Release", "-G", "TGZ",
             "-B", build / "packages"])
        archive = build / "packages/qcs-macos-deploy-fixture.tar.gz"
        unpack = fixture_root / "package-original"
        unpack.mkdir()
        with tarfile.open(archive) as package:
            package.extractall(unpack, **({"filter": "data"} if hasattr(tarfile, "data_filter") else {}))
        verify(unpack / "qcs-macos-deploy-fixture", "macos-cpack-tgz-strip", names)
        return records
    except Exception:
        # The parent runner owns and removes its temporary directory. Keep only
        # bounded diagnostic logs outside it when native CI requests retention.
        if os.environ.get("QCOPILOTS_SEARCH_KEEP_FAILED_TESTS") == "1":
            retained = Path(tempfile.mkdtemp(prefix="qcs-macos-deploy-failed-", dir=root.parent))
            shutil.copytree(logs, retained / "logs")
            print(f"Preserved macOS deployment diagnostics: {retained}", file=sys.stderr, flush=True)
        raise
