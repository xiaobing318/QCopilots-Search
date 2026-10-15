#!/usr/bin/env python3
"""Exercise CMake variants, native package dependency analysis and Windows PE ABI."""
import argparse
import importlib.util
import io
import json
import os
from pathlib import Path, PureWindowsPath
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests/support"))
from temp_paths import temporary_root

spec = importlib.util.spec_from_file_location("qcs_gpu_abi", ROOT / "tools/qcs-gpu-abi.py")
abi = importlib.util.module_from_spec(spec)
spec.loader.exec_module(abi)
path_spec = importlib.util.spec_from_file_location("qcs_path_budget", ROOT / "tools/qcs-path-budget.py")
path_budget = importlib.util.module_from_spec(path_spec)
path_spec.loader.exec_module(path_budget)
PARSER = argparse.ArgumentParser()
PARSER.add_argument("--cmake", default="cmake")
OPTIONS, REST = PARSER.parse_known_args()


class VariantTests(unittest.TestCase):
    def configure(self, variant, os_name=None, arch=None):
        selected_os = os_name or variant["os"]
        selected_arch = arch or variant["arch"]
        system = {"windows": "Windows", "linux": "Linux", "macos": "Darwin"}[selected_os]
        processor = {"x64": "AMD64", "arm64": "arm64"}[selected_arch]
        script = f'''set(WIN32 {"TRUE" if selected_os == "windows" else "FALSE"})
set(APPLE {"TRUE" if selected_os == "macos" else "FALSE"})
set(CMAKE_SYSTEM_NAME {system})
set(CMAKE_SYSTEM_PROCESSOR {processor})
set(QCOPILOTS_SEARCH_VARIANT "{variant['id']}" CACHE STRING "")
include("{(ROOT / 'cmake/qcs-gpu-variants.cmake').as_posix()}")
qcopilots_search_select_variant()
message(STATUS "BACKEND=${{QCOPILOTS_SEARCH_GPU_BACKEND}}")
message(STATUS "TARGETS=${{QCOPILOTS_SEARCH_GPU_TARGETS}}")
'''
        with tempfile.TemporaryDirectory(prefix="qcs-variant-", dir=temporary_root()) as root:
            path = Path(root) / "variant.cmake"
            path.write_text(script, encoding="utf-8")
            return subprocess.run([OPTIONS.cmake, "-P", str(path)], capture_output=True,
                                  text=True, encoding="utf-8", errors="replace", timeout=30)

    def test_manifest_variants_resolve(self):
        variants = json.loads((ROOT / "config/build-variants.json").read_text())["variants"]
        self.assertEqual(11, len(variants))
        for variant in variants:
            with self.subTest(variant=variant["id"]):
                result = self.configure(variant)
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                self.assertIn(f"BACKEND={variant['backend']}", result.stdout)
                if variant["backend"] == "hip":
                    self.assertIn("TARGETS=gfx1200;gfx1201", result.stdout)

    def test_wrong_platform_and_arch_are_rejected(self):
        variant = {"id": "windows-x64-cuda-12.4", "os": "windows", "arch": "x64"}
        self.assertNotEqual(0, self.configure(variant, os_name="linux").returncode)
        self.assertNotEqual(0, self.configure(variant, arch="arm64").returncode)

    def test_removed_macos_intel_variant_is_rejected(self):
        result = self.configure({"id": "macos-x64-cpu", "os": "macos", "arch": "x64"})
        self.assertNotEqual(0, result.returncode)
        self.assertIn("Unknown QCS build variant", result.stderr)

    def test_windows_gpu_subbuilds_keep_isolation_within_object_budget(self):
        variants = [row for row in json.loads((ROOT / "config/build-variants.json").read_text())["variants"]
                    if row["os"] == "windows" and row["backend"] in {"cuda", "hip"}]
        self.assertEqual(3, len(variants))
        configured = set()
        with tempfile.TemporaryDirectory(prefix="qcs-gpu-path-", dir=temporary_root()) as root:
            directory = Path(root)
            source, upstream, sdk = directory / "source", directory / "upstream", directory / "sdk"
            source.mkdir()
            upstream.mkdir()
            (upstream / "CMakeLists.txt").write_text(
                "cmake_minimum_required(VERSION 3.28)\nproject(gpu-fixture LANGUAGES NONE)\n", encoding="utf-8")
            (sdk / "lib/llvm/bin").mkdir(parents=True)
            # Configuration checks the SDK layout without compiling or running it.
            (sdk / "lib/llvm/bin/clang++.exe").write_bytes(b"configuration fixture")
            (source / "CMakeLists.txt").write_text(f'''
cmake_minimum_required(VERSION 3.28)
project(qcs-gpu-path-contract LANGUAGES NONE)
set(WIN32 TRUE)
set(APPLE FALSE)
set(CMAKE_SYSTEM_NAME Windows)
set(CMAKE_SYSTEM_PROCESSOR AMD64)
set(Python3_EXECUTABLE "{Path(sys.executable).as_posix()}")
set(QCOPILOTS_SEARCH_GPU_SDK_ROOT "{sdk.as_posix()}" CACHE PATH "")
include("{(ROOT / 'cmake/qcs-output-paths.cmake').as_posix()}")
include("{(ROOT / 'cmake/qcs-gpu-variants.cmake').as_posix()}")
qcopilots_search_select_variant()
add_library(ggml-base SHARED IMPORTED GLOBAL)
set_target_properties(ggml-base PROPERTIES IMPORTED_LOCATION "{(directory / 'ggml-base.dll').as_posix()}")
include("{(ROOT / 'cmake/qcs-gpu-backend.cmake').as_posix()}")
qcopilots_search_add_windows_gpu_backend("{upstream.as_posix()}" _backend)
get_target_property(_binary qcs-gpu-backend-build _EP_BINARY_DIR)
get_target_property(_byproducts qcs-gpu-backend-build _EP_BUILD_BYPRODUCTS)
get_target_property(_arguments qcs-gpu-backend-build _EP_CMAKE_ARGS)
file(WRITE "${{CMAKE_BINARY_DIR}}/contract.txt" "binary=${{_binary}}\nbyproducts=${{_byproducts}}\narguments=${{_arguments}}\n")
foreach(_config Debug Release RelWithDebInfo MinSizeRel)
  string(TOUPPER "${{_config}}" _upper)
  get_target_property(_module ${{_backend}} IMPORTED_LOCATION_${{_upper}})
  file(APPEND "${{CMAKE_BINARY_DIR}}/contract.txt" "${{_config}}=${{_module}}\n")
endforeach()
''', encoding="utf-8")
            for variant in variants:
                for config in ("Debug", "Release"):
                    with self.subTest(variant=variant["id"], config=config):
                        build = directory / variant["id"] / config
                        result = subprocess.run(
                            [OPTIONS.cmake, "-S", str(source), "-B", str(build),
                             f"-DQCOPILOTS_SEARCH_VARIANT={variant['id']}", f"-DCMAKE_BUILD_TYPE={config}"],
                            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
                        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                        contract = dict(line.split("=", 1) for line in (build / "contract.txt").read_text().splitlines())
                        binary = Path(contract["binary"]).resolve()
                        self.assertEqual((build / "nd/gpu").resolve(), binary)
                        self.assertNotIn(binary, configured)
                        configured.add(binary)
                        backend = variant["backend"]
                        for imported_config in ("Debug", "Release", "RelWithDebInfo", "MinSizeRel"):
                            self.assertEqual(binary / "bin" / imported_config / f"ggml-{backend}.dll",
                                             Path(contract[imported_config]).resolve())
                        self.assertEqual(f"{binary.as_posix()}/bin/$<CONFIG>/ggml-{backend}.dll",
                                         contract["byproducts"])
                        if backend == "hip":
                            self.assertIn("-DAMDGPU_TARGETS=gfx1200|gfx1201", contract["arguments"])
                        else:
                            self.assertNotIn("CMAKE_CUDA_ARCHITECTURES", contract["arguments"])
                        # Use the actual CI root shape and the longest HIP template
                        # path reported by the pinned upstream build (203 before).
                        ci_binary = (PureWindowsPath("D:/a/_temp/qcs-bz1i4ln8") / variant["id"] / config
                                     / PureWindowsPath(binary.relative_to(build).as_posix()))
                        tail = (PureWindowsPath("ggml/src") / f"ggml-{backend}" / "CMakeFiles"
                                / f"ggml-{backend}.dir" / config)
                        if backend == "hip":
                            tail /= "__/ggml-cuda"
                        object_path = (ci_binary / tail / "template-instances"
                                       / "fattn-mma-f16-instance-ncols1_1-ncols2_16.cu.obj")
                        self.assertLessEqual(path_budget.path_length(object_path, "windows"),
                                             path_budget.WINDOWS_OBJECT_BUDGET)
        self.assertEqual(6, len(configured))

    def test_install_hook_uses_installed_module_and_actual_prefix(self):
        # The deployment wrapper must scan the installed module after CMake has
        # changed RPATH, and respect a late --prefix override.
        with tempfile.TemporaryDirectory(prefix="qcs-gpu-install-", dir=temporary_root()) as root:
            directory = Path(root)
            source, build, stage = directory / "source", directory / "build", directory / "安装 stage"
            (source / "tools").mkdir(parents=True)
            (source / "tools/qcs-gpu-runtime.py").write_text(
                "import json, pathlib, sys\n"
                "args = dict(zip(sys.argv[1::2], sys.argv[2::2]))\n"
                "p = pathlib.Path(args['--report'])\n"
                "p.parent.mkdir(parents=True, exist_ok=True)\n"
                "p.write_text(json.dumps(args), encoding='utf-8')\n", encoding="utf-8")
            sdk_manifest = directory / "gpu-sdk.json"
            sdk_manifest.write_text("{}", encoding="utf-8")
            module_name = "ggml-vulkan.dll" if os.name == "nt" else "libggml-vulkan.so"
            dummy = directory / module_name
            dummy.write_bytes(b"not loaded by this installation contract test")
            (source / "CMakeLists.txt").write_text(f'''
cmake_minimum_required(VERSION 3.28)
project(qcs-gpu-install-contract LANGUAGES NONE)
set(Python3_EXECUTABLE "{Path(sys.executable).as_posix()}")
set(CMAKE_INSTALL_BINDIR bin)
set(CMAKE_INSTALL_LIBDIR lib)
set(QCOPILOTS_SEARCH_GPU_BACKEND vulkan)
set(QCOPILOTS_SEARCH_VARIANT "{'windows' if os.name == 'nt' else 'linux'}-x64-vulkan")
set(QCOPILOTS_SEARCH_GPU_SDK_MANIFEST "{sdk_manifest.as_posix()}")
add_library(qcs-gpu-fixture MODULE IMPORTED GLOBAL)
set_target_properties(qcs-gpu-fixture PROPERTIES IMPORTED_LOCATION "{dummy.as_posix()}")
include("{(ROOT / 'cmake/qcs-gpu-backend.cmake').as_posix()}")
qcopilots_search_install_gpu_dependencies(qcs-gpu-fixture)
''', encoding="utf-8")
            for command in ([OPTIONS.cmake, "-S", str(source), "-B", str(build),
                             f"-DCMAKE_INSTALL_PREFIX={directory / 'unused-configure-prefix'}"],
                            [OPTIONS.cmake, "--install", str(build), "--prefix", str(stage)]):
                result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                                        errors="replace", timeout=30)
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            args = json.loads((stage / "share/qcs/gpu-runtime.json").read_text())
            self.assertEqual(stage.resolve(), Path(args["--stage-root"]).resolve())
            expected = stage / ("bin" if os.name == "nt" else "lib") / module_name
            self.assertEqual(expected.resolve(), Path(args["--backend-library"]).resolve())
            self.assertEqual(sdk_manifest.resolve(), Path(args["--sdk-manifest"]).resolve())


if sys.platform.startswith("linux"):
    class LinuxDebDriverTests(unittest.TestCase):
        def test_linked_ancestors_of_new_analysis_or_payload_paths_are_rejected_before_helper(self):
            cmake = shutil.which(OPTIONS.cmake)
            self.assertIsNotNone(cmake, "Linux DEB boundary fixture requires CMake")
            boundaries = ("CPACK_QCS_INSTALL_PREFIX", "CPACK_PACKAGE_DIRECTORY",
                          "CPACK_TOPLEVEL_DIRECTORY", "CPACK_TEMPORARY_DIRECTORY")
            tail = Path("qcs-deb-analysis/linux-x64-cuda-12.8/Release")
            deadline = time.monotonic() + 20
            with tempfile.TemporaryDirectory(prefix="qcs-deb-boundary-", dir=temporary_root()) as root:
                directory = Path(root)
                source = directory / "source"
                (source / "tools").mkdir(parents=True)
                marker = directory / "helper-was-called"
                # The real CPack integration must reject the path before
                # invoking this sentinel in place of its SDK preparation tool.
                (source / "tools/qcs-gpu-runtime.py").write_text(
                    "from pathlib import Path\n"
                    f"Path({str(marker)!r}).write_text('unexpected helper call', encoding='utf-8')\n"
                    "raise SystemExit('boundary fixture helper must not execute')\n", encoding="utf-8")
                manifest = directory / "gpu-sdk.json"
                manifest.write_text("{}", encoding="utf-8")
                for boundary in boundaries:
                    for linked_side in ("analysis", "payload"):
                        with self.subTest(boundary=boundary, linked_side=linked_side):
                            case = directory / f"{boundary}-{linked_side}"
                            case.mkdir()
                            physical = case / "physical"
                            physical.mkdir()
                            alias = case / "alias"
                            alias.symlink_to(physical, target_is_directory=True)
                            self.assertTrue(alias.is_symlink())
                            if linked_side == "analysis":
                                build, payload = alias, physical
                            else:
                                build, payload = physical, alias / tail / "payload"
                            analysis = build / tail
                            if linked_side == "analysis":
                                self.assertTrue(analysis.resolve().is_relative_to(payload.resolve()))
                            else:
                                self.assertTrue(payload.resolve().is_relative_to(analysis.resolve()))
                                self.assertFalse(payload.exists())
                            self.assertFalse(analysis.exists())
                            self.assertFalse((physical / tail).exists())
                            script = case / "boundary.cmake"
                            script.write_text(f'''
cmake_minimum_required(VERSION 3.28)
set(CPACK_GENERATOR DEB)
set(CPACK_QCS_SOURCE_DIR "{source.as_posix()}")
set(CPACK_QCS_PYTHON_EXECUTABLE "{Path(sys.executable).as_posix()}")
set(CPACK_QCS_VARIANT linux-x64-cuda-12.8)
set(CPACK_QCS_SYSTEM_NAME Linux)
set(CPACK_QCS_GPU_BACKEND cuda)
set(CPACK_QCS_GPU_SDK_MANIFEST "{manifest.as_posix()}")
set(CPACK_QCS_BUILD_DIR "{build.as_posix()}")
set(CPACK_QCS_BUILD_TYPE Release)
set({boundary} "{payload.as_posix()}")
include("{(ROOT / 'cmake/qcs-cpack.cmake').as_posix()}")
''', encoding="utf-8")
                            remaining = deadline - time.monotonic()
                            self.assertGreater(remaining, 0, "DEB boundary fixture exhausted its 20-second watchdog")
                            command = [cmake, "-P", str(script)]
                            try:
                                result = subprocess.run(command, cwd=case, capture_output=True,
                                                        timeout=min(5, remaining))
                            except subprocess.TimeoutExpired as error:
                                streams = [part.decode("utf-8", errors="replace") if isinstance(part, bytes)
                                           else part or "" for part in (error.stdout, error.stderr)]
                                self.fail(f"Boundary command timed out: {command!r}\ncwd={case}\n"
                                          f"stdout:\n{streams[0]}\nstderr:\n{streams[1]}")
                            output = (result.stdout + result.stderr).decode("utf-8", errors="replace")
                            self.assertNotEqual(0, result.returncode, output)
                            self.assertIn(f"CUDA driver analysis directory overlaps {boundary}",
                                          " ".join(output.split()))
                            self.assertFalse(marker.exists(), "Overlapping paths reached SDK preparation")
                            self.assertFalse(analysis.exists(), "Rejected analysis path was created")
                            self.assertFalse((physical / tail).exists())

        def test_driver_analysis_does_not_ship_driver_or_hide_other_missing_dependencies(self):
            # These tiny native libraries are packaging fixtures. No CUDA code
            # is loaded or executed, and no GPU execution is claimed.
            required = {name: shutil.which(name) for name in
                        (OPTIONS.cmake, "ninja", "cc", "readelf", "dpkg-deb", "dpkg-shlibdeps", "file")}
            for name, executable in required.items():
                self.assertIsNotNone(executable, f"Linux DEB fixture requires {name}")
            cmake = Path(required[OPTIONS.cmake]).resolve()
            cpack = cmake.with_name("cpack")
            self.assertTrue(cpack.is_file(), f"CPack must accompany the selected CMake: {cpack}")
            environment = {key: value for key, value in os.environ.items()
                           if key.upper() not in {"CMAKE_GENERATOR", "CMAKE_GENERATOR_PLATFORM",
                                                  "CMAKE_GENERATOR_TOOLSET", "CMAKE_GENERATOR_INSTANCE",
                                                  "CMAKE_TOOLCHAIN_FILE", "DESTDIR"}}
            environment["LC_ALL"] = "C"
            # Keep this fixture inside the existing 120-second suite watchdog.
            # Individual commands remain bounded, including a failing CPack.
            deadline = time.monotonic() + 80

            with tempfile.TemporaryDirectory(prefix="qcs-deb-driver-", dir=temporary_root()) as root:
                directory = Path(root)

                def run(command, *, success=True):
                    remaining = deadline - time.monotonic()
                    self.assertGreater(remaining, 0, "Linux DEB fixture exhausted its 80-second watchdog")
                    try:
                        result = subprocess.run([str(part) for part in command], cwd=directory,
                                                env=environment, capture_output=True,
                                                timeout=min(30, remaining))
                    except subprocess.TimeoutExpired as error:
                        streams = [part.decode("utf-8", errors="replace") if isinstance(part, bytes)
                                   else part or "" for part in (error.stdout, error.stderr)]
                        self.fail(f"Fixture command timed out: {command!r}\ncwd={directory}\n"
                                  f"stdout:\n{streams[0]}\nstderr:\n{streams[1]}")
                    output = (result.stdout + result.stderr).decode("utf-8", errors="replace")
                    if success:
                        self.assertEqual(0, result.returncode, f"{command!r}\n{output}")
                    else:
                        self.assertNotEqual(0, result.returncode, f"{command!r}\n{output}")
                    return result, output

                source, build, sdk = (directory / name for name in ("source", "build", "sdk"))
                source.mkdir()
                (sdk / "lib64/stubs").mkdir(parents=True)
                variant = next(row for row in json.loads(
                    (ROOT / "config/build-variants.json").read_text(encoding="utf-8"))["variants"]
                               if row["id"] == "linux-x64-cuda-12.8")
                manifest = directory / "gpu-sdk.json"
                manifest.write_text(json.dumps({
                    "schemaVersion": 1, "variant": variant["id"], "backend": "cuda", "os": "linux",
                    "toolkitVersion": variant["toolkitVersion"], "gpuTargets": variant["gpuTargets"],
                    "cmakeCache": {"CUDAToolkit_ROOT": str(sdk)}, "runtimeRoots": [str(sdk)],
                }), encoding="utf-8")
                (source / "driver.c").write_text(
                    "/* Synthetic link/analysis stub, never executed. */\n"
                    "int cuInit(unsigned int flags) { return (int)flags; }\n", encoding="utf-8")
                (source / "backend.c").write_text(
                    "#include <stdio.h>\nextern int cuInit(unsigned int);\n"
                    "int qcs_backend_fixture(void) { puts(\"packaging fixture\"); return cuInit(0); }\n",
                    encoding="utf-8")
                (source / "unrelated.c").write_text(
                    "int qcs_unrelated_fixture(void) { return 7; }\n", encoding="utf-8")
                (source / "unresolved.c").write_text(
                    "#include <stdio.h>\nextern int cuInit(unsigned int);\n"
                    "extern int qcs_unrelated_fixture(void);\n"
                    "int qcs_unresolved_fixture(void) { puts(\"missing dependency fixture\"); "
                    "return cuInit(0) + qcs_unrelated_fixture(); }\n", encoding="utf-8")
                (source / "CMakeLists.txt").write_text(f'''
cmake_minimum_required(VERSION 3.28)
project(qcs_deb_driver_fixture VERSION 1.0.0 LANGUAGES C)
add_library(qcs-driver-fixture SHARED driver.c)
set_target_properties(qcs-driver-fixture PROPERTIES OUTPUT_NAME cuda NO_SONAME TRUE
  LIBRARY_OUTPUT_DIRECTORY "{(sdk / 'lib64/stubs').as_posix()}")
target_link_options(qcs-driver-fixture PRIVATE "-Wl,-soname,libcuda.so.1")
add_library(qcs-backend-fixture SHARED backend.c)
target_link_libraries(qcs-backend-fixture PRIVATE qcs-driver-fixture)
add_library(qcs-unrelated-fixture SHARED unrelated.c)
set_target_properties(qcs-unrelated-fixture PROPERTIES SOVERSION 1)
add_library(qcs-unresolved-fixture SHARED unresolved.c)
target_link_libraries(qcs-unresolved-fixture PRIVATE qcs-driver-fixture qcs-unrelated-fixture)
set_target_properties(qcs-backend-fixture qcs-unresolved-fixture PROPERTIES
  INSTALL_RPATH "$ORIGIN" INSTALL_RPATH_USE_LINK_PATH FALSE)
option(QCS_FIXTURE_UNRESOLVED "Install the negative dependency fixture" OFF)
if(QCS_FIXTURE_UNRESOLVED)
  install(TARGETS qcs-unresolved-fixture LIBRARY DESTINATION lib)
else()
  install(TARGETS qcs-backend-fixture LIBRARY DESTINATION lib)
endif()
set(CPACK_GENERATOR DEB)
set(CPACK_PACKAGE_NAME qcs-deb-driver-fixture)
set(CPACK_PACKAGE_VERSION 1.0.0)
set(CPACK_PACKAGE_FILE_NAME qcs-deb-driver-fixture)
set(CPACK_PACKAGE_DESCRIPTION_SUMMARY "Synthetic CUDA driver analysis fixture")
set(CPACK_DEBIAN_PACKAGE_MAINTAINER "QCS test fixture")
set(CPACK_DEBIAN_PACKAGE_SHLIBDEPS ON)
set(CPACK_QCS_SOURCE_DIR "{ROOT.as_posix()}")
set(CPACK_QCS_PYTHON_EXECUTABLE "{Path(sys.executable).as_posix()}")
set(CPACK_QCS_VARIANT "{variant['id']}")
set(CPACK_QCS_SYSTEM_NAME Linux)
set(CPACK_QCS_GPU_BACKEND cuda)
set(CPACK_QCS_GPU_SDK_MANIFEST "{manifest.as_posix()}")
set(CPACK_QCS_BUILD_DIR "${{CMAKE_BINARY_DIR}}")
set(CPACK_QCS_INSTALL_PREFIX "${{CMAKE_INSTALL_PREFIX}}")
set(CPACK_QCS_BUILD_TYPE Release)
set(CPACK_PROJECT_CONFIG_FILE "{(ROOT / 'cmake/qcs-cpack.cmake').as_posix()}")
include(CPack)
''', encoding="utf-8")
                configure = [cmake, "-S", source, "-B", build, "-G", "Ninja",
                             f"-DCMAKE_MAKE_PROGRAM={required['ninja']}",
                             f"-DCMAKE_C_COMPILER={required['cc']}", "-DCMAKE_BUILD_TYPE=Release",
                             f"-DCMAKE_INSTALL_PREFIX={directory / 'stage'}"]
                run(configure)
                run([cmake, "--build", build, "--parallel", "2"])
                _, dynamic = run([required["readelf"], "--dynamic", build / "libqcs-backend-fixture.so"])
                self.assertRegex(dynamic, r"\(NEEDED\).*\[libcuda\.so\.1\]")
                self.assertRegex(dynamic, r"\(NEEDED\).*\[libc\.so\.6\]")
                packages = directory / "packages"
                pack = [cpack, "--config", build / "CPackConfig.cmake", "-G", "DEB", "-C", "Release"]
                run([*pack, "-B", packages])
                archives = list(packages.glob("*.deb"))
                self.assertEqual(1, len(archives))
                _, depends = run([required["dpkg-deb"], "--field", archives[0], "Depends"])
                self.assertRegex(depends, r"\blibc6\b")
                self.assertNotRegex(depends.lower(), r"cuda|nvidia|sdk")
                payload, _ = run([required["dpkg-deb"], "--fsys-tarfile", archives[0]])
                packaged_backend = directory / "packaged-backend.so"
                with tarfile.open(fileobj=io.BytesIO(payload.stdout), mode="r:") as archive:
                    members = archive.getmembers()
                    backend_members = [item for item in members
                                       if Path(item.name).name == "libqcs-backend-fixture.so"]
                    self.assertEqual(1, len(backend_members))
                    self.assertTrue(backend_members[0].isfile())
                    packaged_backend.write_bytes(archive.extractfile(backend_members[0]).read())
                    self.assertFalse(any("libcuda" in item.name.lower() or "stubs" in Path(item.name).parts
                                         or "sdk" in Path(item.name).parts for item in members))
                    for item in members:
                        if item.isfile():
                            self.assertNotIn(str(sdk).encode(), archive.extractfile(item).read(),
                                             f"SDK path leaked into package member {item.name}")
                _, dynamic = run([required["readelf"], "--dynamic", packaged_backend])
                self.assertRegex(dynamic, r"\(NEEDED\).*\[libcuda\.so\.1\]")
                self.assertRegex(dynamic, r"\(NEEDED\).*\[libc\.so\.6\]")
                analysis = build / "qcs-deb-analysis" / variant["id"] / "Release"
                self.assertTrue((analysis / "libcuda.so.1").is_file(),
                                "Production DEB hook must create its analysis-only driver alias")
                run([*configure, "-DQCS_FIXTURE_UNRESOLVED=ON"])
                _, failure = run([*pack, "-B", directory / "missing-packages"], success=False)
                self.assertIn("libqcs-unrelated-fixture.so.1", failure)
                self.assertIn("cannot find library", failure)


class FakeImage:
    def __init__(self, name, exports, imports=None, machine=0x8664):
        self.path, self.data, self.machine = Path(name), b"fixture", machine
        self._exports, self._imports = set(exports), imports or {}

    def exports(self):
        return self._exports

    def imports(self):
        return self._imports


class AbiTests(unittest.TestCase):
    def test_matching_c_exports_do_not_claim_gpu_execution(self):
        base = FakeImage("ggml-base.dll", ["ggml_backend_buffer_init"])
        module = FakeImage("ggml-hip.dll", ["ggml_backend_init"],
                           {"ggml-base.dll": {"ggml_backend_buffer_init"}})
        report = abi.verify(module, base)
        self.assertTrue(report["passed"])
        self.assertFalse(report["gpuExecutionTested"])

    def test_mismatched_base_is_rejected(self):
        base = FakeImage("ggml-base.dll", ["older_function"])
        module = FakeImage("ggml-cuda.dll", ["ggml_backend_init"],
                           {"ggml-base.dll": {"newer_function"}})
        with self.assertRaisesRegex(ValueError, "absent"):
            abi.verify(module, base)

    def test_x86_module_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "x64"):
            abi.verify(FakeImage("module", [], machine=0x14c), FakeImage("base", []))

    @unittest.skipUnless(os.name == "nt", "Uses an actual host PE library")
    def test_real_windows_system_image(self):
        image = abi.PE(Path(os.environ["SystemRoot"]) / "System32/kernel32.dll")
        self.assertEqual(0x8664, image.machine)
        self.assertIn("LoadLibraryExW", image.exports())
        self.assertIsInstance(image.imports(), dict)


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]] + REST)
