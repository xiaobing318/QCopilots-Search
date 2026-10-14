#!/usr/bin/env python3
"""GPU packaging contract tests without a GPU, network, models or real SDK."""
import base64
import csv
from contextlib import ExitStack
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

sys.dont_write_bytecode = True


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sdk = load("qcs_gpu_sdk_tests", "qcs-gpu-sdk.py")
runtime = load("qcs_gpu_runtime_tests", "qcs-gpu-runtime.py")


def pe(*imports, delay=None):
    data = bytearray(2048)
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 60, 128)
    data[128:132] = b"PE\0\0"
    struct.pack_into("<H", data, 134, 1)
    struct.pack_into("<H", data, 148, 240)
    struct.pack_into("<H", data, 152, 0x20b)
    struct.pack_into("<I", data, 260, 16)
    struct.pack_into("<IIII", data, 400, 1536, 0x1000, 1536, 512)
    string_at = 1200
    if imports:
        struct.pack_into("<II", data, 272, 0x1000, (len(imports) + 1) * 20)
        for i, name in enumerate(imports):
            struct.pack_into("<I", data, 512 + 20 * i + 12, 0x1000 + string_at - 512)
            encoded = name.encode() + b"\0"
            data[string_at:string_at + len(encoded)] = encoded
            string_at += len(encoded)
    if delay:
        struct.pack_into("<II", data, 368, 0x1100, 64)
        struct.pack_into("<II", data, 768, 1, 0x1000 + string_at - 512)
        data[string_at:string_at + len(delay) + 1] = delay.encode() + b"\0"
    return bytes(data)


class GpuTests(unittest.TestCase):
    def setUp(self):
        root = os.environ.get("QCOPILOTS_SEARCH_TEST_ROOT") or None
        if root:
            Path(root).mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="qcs-gpu-", dir=root)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def file(self, name, content=b"test"):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def wheel(self, entries, corrupt=False):
        entries = {**entries, "fixture-10.0.0.dist-info/METADATA": b"Metadata-Version: 2.1\nName: fixture\nVersion: 10.0.0\n"}
        archive = self.root / "fixture.whl"
        record = io.StringIO()
        writer = csv.writer(record, lineterminator="\n")
        for name, data in entries.items():
            h = hashlib.sha256(data if not corrupt else b"wrong").digest()
            writer.writerow([name, "sha256=" + base64.urlsafe_b64encode(h).decode().rstrip("="), len(data)])
        writer.writerow(["fixture-10.0.0.dist-info/RECORD", "", ""])
        with zipfile.ZipFile(archive, "w") as package:
            for name, data in entries.items():
                package.writestr(name, data)
            package.writestr("fixture-10.0.0.dist-info/RECORD", record.getvalue())
        return archive

    def test_archive_paths_cannot_escape(self):
        for name in ("../bad", "a/../../bad", "C:/bad", "a\\..\\bad", "//host/share", "/absolute", "a:stream"):
            with self.subTest(name=name), self.assertRaises(RuntimeError):
                sdk.safe_relative(name)
        self.assertEqual(sdk.safe_relative("abc/汉字 file"), Path("abc/汉字 file"))

    def test_source_tree_is_not_a_build_root(self):
        with self.assertRaises(RuntimeError):
            sdk.external(sdk.SOURCE / "build")

    def test_device_selection_excludes_other_architectures_and_components(self):
        targets = ["gfx1200", "gfx1201"]
        self.assertTrue(sdk.is_rocm_device_resource("_rocm_sdk_libraries/.kpack/blas_lib_gfx1200.kpack", targets))
        self.assertTrue(sdk.is_rocm_device_resource("_rocm_sdk_libraries/lib/hipblaslt/library/gfx1201/kernel.co", targets))
        for path in ("_rocm_sdk_libraries/.kpack/blas_lib_gfx1100.kpack", "_rocm_sdk_libraries/.kpack/fft_lib_gfx1200.kpack", "_rocm_sdk_libraries/bin/MIOpenCKGroupedConv_gfx1200.dll", "_rocm_sdk_core/lib/llvm/clang"):
            self.assertFalse(sdk.is_rocm_device_resource(path, targets), path)

    def test_device_wheel_extracts_only_reviewed_resources(self):
        selected = "_rocm_sdk_libraries/.kpack/blas_lib_gfx1200.kpack"
        entries = {selected: b"kernel", "_rocm_sdk_libraries/.kpack/blas_lib_gfx1100.kpack": b"excluded", "_rocm_sdk_libraries/bin/MIOpenCKGroupedConv_gfx1200.dll": b"excluded"}
        archive = self.wheel(entries)
        destination = self.root / "resources"
        destination.mkdir()
        with patch.object(sdk, "disk_check"):
            files = sdk.extract_device_wheel(archive, destination, ["gfx1200", "gfx1201"])
        self.assertEqual([f["path"] for f in files], [selected])
        self.assertEqual((destination / selected).read_bytes(), b"kernel")

    def test_corrupt_wheel_record_is_rejected_and_payload_removed(self):
        name = "_rocm_sdk_libraries/.kpack/blas_lib_gfx1200.kpack"
        archive = self.wheel({name: b"kernel"}, corrupt=True)
        destination = self.root / "resources"
        destination.mkdir()
        with patch.object(sdk, "disk_check"), self.assertRaisesRegex(RuntimeError, "RECORD hash mismatch"):
            sdk.extract_device_wheel(archive, destination, ["gfx1200", "gfx1201"])
        self.assertFalse((destination / name).exists())

    def test_wrong_device_wheel_fails(self):
        archive = self.wheel({"_rocm_sdk_libraries/.kpack/blas_lib_gfx1100.kpack": b"wrong arch"})
        with self.assertRaisesRegex(RuntimeError, "no BLAS resources"):
            sdk.extract_device_wheel(archive, self.root, ["gfx1200", "gfx1201"])

    def test_pe_import_and_delay_import_parse(self):
        path = self.file("backend.dll", pe("ggml-base.dll", "KERNEL32.dll", delay="amdhip64_7.dll"))
        self.assertEqual(runtime.pe_dependencies(path), ["amdhip64_7.dll", "ggml-base.dll", "KERNEL32.dll"])

    def test_invalid_native_image_fails(self):
        with self.assertRaises(RuntimeError):
            runtime.pe_dependencies(self.file("bad.dll", b"not an executable"))

    def test_rocm_llvm_non_numeric_sonames_are_indexed(self):
        llvm = self.file("core/lib/llvm/lib/libLLVM.so.23.0git", b"ELF fixture")
        clang = self.file("core/lib/llvm/lib/libclang-cpp.so.23.0git", b"ELF fixture")
        index = runtime.native_index([self.root / "core"], False)
        self.assertEqual(index[llvm.name], [llvm])
        self.assertEqual(index[clang.name], [clang])

    def test_vulkan_runtime_license_filename_is_recognized(self):
        self.assertTrue(sdk.is_license("VulkanRT-License.txt"))

    def test_cuda_bootstrap_repairs_partial_install_after_nvcc(self):
        self.file("sdk/cuda-12.4/bin/nvcc.exe", b"compiler")
        component = {"name": "libcublas", "sha256": "a" * 64, "url": "https://example.invalid/cublas.zip"}
        archive = self.root / "cublas.zip"
        with zipfile.ZipFile(archive, "w") as package:
            package.writestr("component/bin/cublas64_12.dll", b"BLAS")
        variant = {"os": "windows", "toolkitVersion": "12.4"}
        lock = {"cuda": {"windows": {"12.4": {"compilerVersion": "12.4.131", "runtimeLicenseSha256": "b" * 64, "components": [component]}}}}
        with patch.dict(os.environ, {"CUDA_PATH": "", "CUDAToolkit_ROOT": ""}), patch.object(sdk, "run", return_value="release 12.4, V12.4.131"), patch.object(sdk, "disk_check"), patch.object(sdk, "download", return_value=(archive, {"sha256": "a" * 64})) as download:
            sdk.cuda_sdk(variant, self.root / "sdk", True, lock)
        download.assert_called_once()
        self.assertTrue((self.root / "sdk/cuda-12.4/bin/cublas64_12.dll").is_file())
        with patch.dict(os.environ, {"CUDA_PATH": "", "CUDAToolkit_ROOT": ""}), patch.object(sdk, "run", return_value="release 12.4, V12.4.131"), patch.object(sdk, "download") as download:
            sdk.cuda_sdk(variant, self.root / "sdk", True, lock)
        download.assert_not_called()

    def test_closure_orders_dependencies_and_defers_only_microsoft_crt(self):
        first = self.file("sdk/first.dll", pe("second.dll", "vcruntime140.dll"))
        second = self.file("sdk/second.dll", pe("kernel32.dll"))
        index = runtime.native_index([self.root / "sdk"], True)
        ordered, system, application = runtime.closure([first], index, True, [])
        self.assertEqual(ordered, [second, first])
        self.assertEqual(system, ["kernel32.dll"])
        self.assertEqual(application, ["vcruntime140.dll"])

    def test_missing_vendor_dependency_cannot_be_treated_as_system(self):
        first = self.file("sdk/first.dll", pe("rocsolver.dll"))
        with self.assertRaisesRegex(RuntimeError, "Unresolved GPU dependency rocsolver.dll"):
            runtime.closure([first], runtime.native_index([self.root / "sdk"], True), True, [])

    def test_closure_preserves_soname_aliases(self):
        actual = self.file("sdk/libsample.so.1", b"ELF fixture")
        alias = self.file("sdk/libsample.so", b"ELF fixture")
        # Model identical symlink resolution without requiring the Windows
        # CreateSymbolicLink privilege in the test environment.
        with patch.object(Path, "resolve", return_value=actual), patch.object(runtime, "dependencies", return_value=[]):
            ordered, _, _ = runtime.closure([alias, actual], runtime.native_index([actual.parent], False), False, [])
        self.assertEqual(ordered, [alias, actual])

    def test_ambiguous_library_names_are_rejected(self):
        self.file("sdk/one/runtime.dll", pe())
        self.file("sdk/two/runtime.dll", pe("kernel32.dll"))
        with self.assertRaisesRegex(RuntimeError, "Ambiguous"):
            runtime.choose_library("runtime.dll", runtime.native_index([self.root / "sdk"], True), True)

    def test_cpu_manifest_never_claims_gpu_execution(self):
        variant = {"id": "cpu", "backend": "cpu", "os": "windows", "toolkitVersion": "", "gpuTargets": []}
        result = runtime.stage_runtime(variant, {"schemaVersion": 1, "variant": "cpu", **{k:v for k,v in variant.items() if k != "id"}}, self.root / "stage")
        self.assertFalse(result["verification"]["gpuExecution"])
        self.assertEqual(result["verification"]["dependencyClosure"], "not-applicable")

    def test_variant_manifest_mismatch_fails_before_copying(self):
        variant = {"id": "cuda", "backend": "cuda", "os": "windows", "toolkitVersion": "12.4", "gpuTargets": []}
        with self.assertRaisesRegex(RuntimeError, "does not match"):
            runtime.stage_runtime(variant, {"schemaVersion": 1, "variant": "other", "backend": "cuda"}, self.root / "stage")

    def test_cuda_stage_includes_backend_runtime_hashes_and_notices(self):
        toolkit = self.root / "cuda"
        for name in ("cudart64_12.dll", "cublas64_12.dll", "cublasLt64_12.dll"):
            self.file("cuda/bin/" + name, pe("kernel32.dll", "vcruntime140.dll"))
        self.file("cuda/LICENSE", b"Original CUDA redistribution terms\n")
        backend = self.file("stage/bin/ggml-cuda.dll", pe("cudart64_12.dll"))
        variant = {"id": "cuda", "backend": "cuda", "os": "windows", "toolkitVersion": "12.4", "gpuTargets": []}
        manifest = {"schemaVersion": 1, "variant": "cuda", "backend": "cuda", "toolkitVersion": "12.4", "gpuTargets": [], "runtimeRoots": [str(toolkit)]}
        report = runtime.stage_runtime(variant, manifest, self.root / "stage", backend)
        self.assertEqual(len(report["files"]), 4)
        self.assertEqual(report["verification"]["dependencyClosure"], "verified")
        self.assertIn("vcruntime140.dll", report["applicationDependencies"])
        self.assertEqual(len(report["resources"]), 1)
        for item in report["files"]:
            self.assertEqual(sdk.digest(self.root / "stage" / item["path"]), item["sha256"])
        self.assertIn("bin/ggml-cuda.dll", [item["path"] for item in report["files"]])

    def test_installed_gpu_stage_requires_original_license(self):
        with self.assertRaisesRegex(RuntimeError, "No original GPU runtime license"):
            runtime.license_resources([self.root], {"toolkitVersion": "12"}, self.root / "stage")

    def test_additional_rocm_notices_preserve_bytes_and_are_relocatable(self):
        notice = self.file("sources/HIP-LICENSE.txt", b"Official HIP license\n")
        resource = {"name": "rocm-hip", "version": "10.0.0", "files": [{"path": "LICENSE.txt", "source": str(notice), "sha256": sdk.digest(notice)}]}
        report = runtime.license_resources([], {"toolkitVersion": "10.0.0", "additionalLicenseResources": [resource]}, self.root / "stage")
        entry = report[0]["files"][0]
        self.assertFalse(Path(entry["source"]).is_absolute())
        self.assertEqual((self.root / "stage/bin" / entry["source"]).read_bytes(), notice.read_bytes())
        notice.write_bytes(b"Changed source")
        with self.assertRaisesRegex(RuntimeError, "does not match its lock"):
            runtime.license_resources([], {"toolkitVersion": "10.0.0", "additionalLicenseResources": [resource]}, self.root / "stage2")

    def test_locked_variants_use_only_two_rocm_targets(self):
        variants = json.loads(sdk.VARIANTS.read_text(encoding="utf-8"))["variants"]
        self.assertEqual(len(variants), 11)
        for item in variants:
            if item["backend"] == "hip":
                self.assertEqual(sdk.variant_by_id(item["id"])["gpuTargets"], ["gfx1200", "gfx1201"])

    def linux_vulkan_mocks(self, *, package_version=None, header_version=313, compiler_hash=None, uid=0):
        specification = json.loads(sdk.LOCK.read_text(encoding="utf-8"))["vulkan"]["linux"]
        versions = {p["name"]: p["version"] for p in specification["apt"]["packages"]}

        def command(args, **kwargs):
            if args[0] == "dpkg-query":
                return "install ok installed\t" + (package_version or versions[args[-1]])
            if args[-1] == "--version":
                return specification["glslcEmbeddedVersion"] + "\nspirv-tools v2025.2 unknown hash, 2025-04-29"
            return ""

        def downloaded(entry, cache):
            return cache / entry["url"].rsplit("/", 1)[-1], {"url": entry["url"], "sha256": entry["sha256"]}

        contexts = ExitStack()
        contexts.enter_context(patch.object(sdk.platform, "freedesktop_os_release", return_value={"ID": "ubuntu", "VERSION_ID": "22.04"}))
        contexts.enter_context(patch.object(sdk.platform, "machine", return_value="x86_64"))
        contexts.enter_context(patch.object(sdk.os, "geteuid", return_value=uid, create=True))
        contexts.enter_context(patch.object(sdk.shutil, "which", return_value="/usr/bin/sudo"))
        contexts.enter_context(patch.object(Path, "is_file", return_value=True))
        contexts.enter_context(patch.object(Path, "read_text", return_value=f"#define VK_HEADER_VERSION {header_version}\n"))
        contexts.enter_context(patch.object(sdk, "digest", return_value=compiler_hash or specification["glslcSha256"]))
        execute = contexts.enter_context(patch.object(sdk, "run", side_effect=command))
        download = contexts.enter_context(patch.object(sdk, "download", side_effect=downloaded))
        return contexts, specification, execute, download

    def test_linux_vulkan_install_verifies_local_packages_and_uses_scoped_key(self):
        contexts, specification, execute, downloaded = self.linux_vulkan_mocks()
        with contexts:
            result = sdk.linux_vulkan_sdk({"toolkitVersion": "1.4.313.0"}, self.root / "sdk", True, specification)
        commands = [call.args[0] for call in execute.call_args_list]
        installs = [args for args in commands if "apt-get" in args and "install" in args]
        self.assertEqual(len(installs), 1)
        self.assertEqual(len([p for p in installs[0] if p.endswith(".deb")]), 6)
        self.assertEqual(downloaded.call_count, 7)
        for call in downloaded.call_args_list:
            self.assertRegex(call.args[0]["sha256"], r"^[0-9a-f]{64}$")
        source = (self.root / "sdk/vulkan-jammy.list").read_text(encoding="utf-8")
        self.assertIn("signed-by=/usr/share/keyrings/qcs-lunarg.asc", source)
        self.assertIn("https://packages.lunarg.com/vulkan/1.4.313 jammy main", source)
        self.assertFalse(any("apt-key" in args for args in commands))
        self.assertEqual(len(result["systemPackages"]), 6)
        self.assertEqual(result["cmakeCache"]["Vulkan_INCLUDE_DIR"], "/usr/include")
        self.assertEqual(result["runtimeRoots"], ["/usr/lib/x86_64-linux-gnu"])

    def test_linux_vulkan_probe_does_not_install_or_download(self):
        contexts, specification, execute, downloaded = self.linux_vulkan_mocks()
        with contexts:
            sdk.linux_vulkan_sdk({"toolkitVersion": "1.4.313.0"}, self.root / "sdk", False, specification)
        downloaded.assert_not_called()
        self.assertFalse(any("apt-get" in call.args[0] for call in execute.call_args_list))

    def test_linux_vulkan_non_root_uses_noninteractive_sudo(self):
        contexts, specification, execute, _ = self.linux_vulkan_mocks(uid=1000)
        with contexts:
            sdk.linux_vulkan_sdk({"toolkitVersion": "1.4.313.0"}, self.root / "sdk", True, specification)
        commands = [call.args[0] for call in execute.call_args_list if "apt-get" in call.args[0]]
        self.assertTrue(commands)
        self.assertTrue(all(args[:3] == ["/usr/bin/sudo", "-n", "--"] for args in commands))

    def test_linux_vulkan_rejects_other_distributions_before_system_changes(self):
        contexts, specification, execute, downloaded = self.linux_vulkan_mocks()
        with contexts, patch.object(sdk.platform, "freedesktop_os_release", return_value={"ID": "ubuntu", "VERSION_ID": "24.04"}), self.assertRaisesRegex(RuntimeError, "Ubuntu 22.04"):
            sdk.linux_vulkan_sdk({"toolkitVersion": "1.4.313.0"}, self.root / "sdk", True, specification)
        execute.assert_not_called()
        downloaded.assert_not_called()

    def test_linux_vulkan_unprivileged_install_fails_before_system_changes(self):
        contexts, specification, execute, downloaded = self.linux_vulkan_mocks(uid=1000)
        with contexts, patch.object(sdk.shutil, "which", return_value=None), self.assertRaisesRegex(RuntimeError, "root or non-interactive sudo"):
            sdk.linux_vulkan_sdk({"toolkitVersion": "1.4.313.0"}, self.root / "sdk", True, specification)
        execute.assert_not_called()
        downloaded.assert_not_called()

    def test_linux_vulkan_wrong_package_version_is_rejected(self):
        contexts, specification, _, _ = self.linux_vulkan_mocks(package_version="1.3.204.1-2")
        with contexts, self.assertRaisesRegex(RuntimeError, "must be"):
            sdk.linux_vulkan_sdk({"toolkitVersion": "1.4.313.0"}, self.root / "sdk", False, specification)

    def test_linux_vulkan_wrong_headers_are_rejected(self):
        contexts, specification, _, _ = self.linux_vulkan_mocks(header_version=204)
        with contexts, self.assertRaisesRegex(RuntimeError, "headers do not match"):
            sdk.linux_vulkan_sdk({"toolkitVersion": "1.4.313.0"}, self.root / "sdk", False, specification)

    def test_linux_vulkan_replaced_glslc_is_rejected_before_execution(self):
        contexts, specification, execute, _ = self.linux_vulkan_mocks(compiler_hash="f" * 64)
        with contexts, self.assertRaisesRegex(RuntimeError, "executable differs"):
            sdk.linux_vulkan_sdk({"toolkitVersion": "1.4.313.0"}, self.root / "sdk", False, specification)
        self.assertFalse(any(call.args[0][-1] == "--version" for call in execute.call_args_list))

    def test_linux_cpu_prepare_never_installs_vulkan(self):
        variant = {"id": "linux-x64-cpu", "os": "linux", "backend": "cpu", "toolkitVersion": "", "gpuTargets": []}
        with patch.object(sdk, "linux_vulkan_sdk") as vulkan:
            sdk.prepare(variant, self.root / "sdk", True, {"upstreamCommit": "fixed"})
        vulkan.assert_not_called()

    def test_lock_checksums_and_upstream_identity_are_well_formed(self):
        lock = json.loads(sdk.LOCK.read_text(encoding="utf-8"))
        self.assertEqual(lock["upstreamCommit"], json.loads(sdk.VARIANTS.read_text(encoding="utf-8"))["upstream"]["commit"])
        for version in lock["cuda"]["windows"].values():
            for component in version["components"]:
                self.assertRegex(component["sha256"], r"^[a-f0-9]{64}$")
                self.assertTrue(component["url"].startswith("https://developer.download.nvidia.com/compute/cuda/redist/"))


if __name__ == "__main__":
    unittest.main()
