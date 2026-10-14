#!/usr/bin/env python3
"""Exercise the real CMake variant contract and Windows PE ABI verification."""
import argparse
import importlib.util
import json
import os
from pathlib import Path, PureWindowsPath
import subprocess
import sys
import tempfile
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
