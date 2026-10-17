#!/usr/bin/env python3
"""Small package identity and backend contracts without loading any GPU SDK."""
from __future__ import annotations

import copy
from contextlib import redirect_stdout
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch


sys.dont_write_bytecode = True
SPEC = importlib.util.spec_from_file_location(
    "qcopilots_search_package_variants", Path(__file__).resolve().parents[2] / "tools/qcs-verify-package.py")
package = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(package)
CANDIDATE = "1" * 40
VARIANTS = {
    "schemaVersion": 1,
    "variants": [
        {"id": "windows-x64-cpu", "backend": "cpu", "packageVariant": "windows-x64-cpu"},
        {"id": "windows-x64-vulkan", "backend": "vulkan", "toolkitVersion": "1.4.357.0", "packageVariant": "windows-x64-vulkan-1.4.357.0"},
        {"id": "windows-x64-rocm", "backend": "hip", "toolkitVersion": "10.0.0", "packageVariant": "windows-x64-rocm-10.0.0"},
        {"id": "windows-x64-cuda-12.4", "backend": "cuda", "toolkitVersion": "12.4", "packageVariant": "windows-x64-cuda-12.4"},
        {"id": "macos-arm64-metal", "backend": "metal", "packageVariant": "macos-arm64-metal"},
    ],
}


def sha(value):
    return hashlib.sha256(value).hexdigest()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def diagnostic(identity, requested="auto", selected="cpu", loaded=True):
    return {
        "buildVariant": identity["variant"],
        "packagedBackend": identity["backend"],
        "requestedBackend": requested,
        "selectedBackend": selected,
        "loaded": loaded,
        "gpuSelected": selected not in ("cpu", ""),
        "deviceName": "Synthetic test device" if loaded else "",
        "fallbackReason": "No compatible test GPU" if selected == "cpu" else "",
    }


class PackageVariantTests(unittest.TestCase):
    def setUp(self):
        configured = os.environ.get("QCOPILOTS_SEARCH_TEST_ROOT") or None
        if configured:
            Path(configured).mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="qcs-package-variants-", dir=configured)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.application = self.root / "relocated-application"
        self.binary = self.application / "bin"
        self.binary.mkdir(parents=True)
        self.materials = self.application / "share/qcs"
        write_json(self.materials / "build-variants.json", VARIANTS)
        write_json(self.materials / "dependencies.lock.json", {"synthetic": "package test only"})
        write_json(self.materials / "gpu-sdk.lock.json", {"synthetic": "GPU SDK test only"})
        write_json(self.materials / "gpu-license-sources.lock.json", {"synthetic": "GPU licenses test only"})
        self.identity = {
            "schemaVersion": 1,
            "variant": "windows-x64-cuda-12.4",
            "packageVariant": "windows-x64-cuda-12.4",
            "toolkitVersion": "12.4",
            "backend": "cuda",
            "version": "0.1.0",
            "attempt": 2,
            "candidateCommit": CANDIDATE,
            "workingTreeModified": False,
            "variantsSha256": package.file_digest(self.materials / "build-variants.json"),
            "dependencyLockSha256": package.file_digest(self.materials / "dependencies.lock.json"),
            "gpuSdkLockSha256": package.file_digest(self.materials / "gpu-sdk.lock.json"),
            "gpuLicenseLockSha256": package.file_digest(self.materials / "gpu-license-sources.lock.json"),
        }
        self.write_identity()

    def write_identity(self):
        write_json(self.materials / "build-identity.json", self.identity)

    def test_worker_stderr_preserves_actual_output_and_safe_json_values(self):
        report_dir = self.root / "stderr-report"
        report_dir.mkdir()
        messages = {}
        for filename, mode in (("worker.log", "auto"), ("backend-cpu.log", "cpu"), ("backend-gpu.log", "gpu")):
            message = (mode.encode() + b" synthetic child stderr\r\n\x00\x1b[31m\xff\n"
                       b"::error::raw diagnostic\n::stop-commands::token\n##[error]legacy diagnostic")
            with (report_dir / filename).open("wb") as stream:
                child = subprocess.run([sys.executable, "-c",
                    "import os, sys; os.write(2, " + repr(message) + "); sys.exit(73)"],
                    cwd=self.root, stderr=stream, stdout=subprocess.DEVNULL, timeout=10)
            self.assertEqual(child.returncode, 73)
            messages[filename] = message
        report = {"passed": False, "fullVerificationPassed": False, "error": "Original verification failure"}
        output = io.StringIO()
        with redirect_stdout(output):
            package.write_verification_report(report, report_dir)
        text = output.getvalue()
        self.assertNotIn("::", text)
        self.assertNotIn("##[", text)
        decoded = json.loads(text)
        self.assertEqual(decoded, json.loads((report_dir / "report.json").read_text(encoding="utf-8")))
        self.assertFalse(decoded["passed"])
        self.assertFalse(decoded["fullVerificationPassed"])
        self.assertEqual(decoded["error"], "Original verification failure")
        for item, mode in zip(decoded["workerStderrDiagnostics"], ("auto", "cpu", "gpu")):
            original = messages[item["source"]]
            self.assertEqual((item["process"], item["stream"], item["requestedBackend"]), ("qcs-worker", "stderr", mode))
            self.assertTrue(item["available"])
            self.assertFalse(item["truncated"])
            self.assertEqual(item["totalBytes"], len(original))
            self.assertEqual(item["capturedBytes"], len(original))
            self.assertEqual(item["omittedBytes"], 0)
            self.assertEqual(item["decodingErrors"], "replace")
            self.assertEqual(item["headText"], original.decode("utf-8", errors="replace"))
            self.assertEqual(item["tailText"], "")

    def test_worker_stderr_limits_each_fixed_source_and_keeps_head_and_tail(self):
        data = b"module discovery start\n" + b"m" * (package.WORKER_STDERR_LIMIT * 8) + b"\nbackend initialization failed"
        for name in ("worker.log", "backend-cpu.log", "backend-gpu.log", "unrelated.log"):
            (self.root / name).write_bytes(data)
        real_open = Path.open
        reads = []
        def bounded_open(path, *args, **kwargs):
            stream = real_open(path, *args, **kwargs)
            wrapper = Mock(wraps=stream)
            wrapper.__enter__ = Mock(return_value=wrapper)
            wrapper.__exit__ = Mock(side_effect=lambda *unused: stream.close())
            def bounded_read(size=-1):
                self.assertGreaterEqual(size, 0)
                self.assertLessEqual(size, package.WORKER_STDERR_LIMIT)
                reads.append(size)
                return stream.read(size)
            wrapper.read.side_effect = bounded_read
            return wrapper
        with patch.object(Path, "open", bounded_open):
            entries = package.worker_stderr_diagnostics(self.root)
        self.assertEqual(len(entries), 3)
        self.assertEqual(sum(reads), 3 * package.WORKER_STDERR_LIMIT)
        for item in entries:
            self.assertTrue(item["truncated"])
            self.assertEqual(item["totalBytes"], len(data))
            self.assertEqual(item["capturedBytes"], package.WORKER_STDERR_LIMIT)
            self.assertEqual(item["omittedBytes"], len(data) - package.WORKER_STDERR_LIMIT)
            self.assertTrue(item["headText"].startswith("module discovery start\n"))
            self.assertTrue(item["tailText"].endswith("\nbackend initialization failed"))

    def test_worker_stderr_missing_empty_and_unreadable_sources_do_not_change_result(self):
        self.assertEqual(package.worker_stderr_diagnostics(self.root), [])
        (self.root / "worker.log").write_bytes(b"")
        empty = package.worker_stderr_diagnostics(self.root)[0]
        self.assertEqual((empty["totalBytes"], empty["capturedBytes"], empty["omittedBytes"]), (0, 0, 0))
        self.assertFalse(empty["truncated"])
        real_open = Path.open
        def unavailable(path, *args, **kwargs):
            if path.name == "worker.log":
                raise PermissionError("Synthetic stderr read denial")
            return real_open(path, *args, **kwargs)
        report = {"passed": True, "fullVerificationPassed": True, "backendVerification": {
            "strictGpu": {"passed": True, "hardwareTested": False, "reason": "Strict GPU unavailable"}}}
        baseline = copy.deepcopy(report)
        with patch.object(Path, "open", unavailable), redirect_stdout(io.StringIO()):
            package.write_verification_report(report, self.root)
        item = report.pop("workerStderrDiagnostics")[0]
        self.assertEqual(report, baseline)
        self.assertFalse(item["available"])
        self.assertIn("stderr read denial", item["readError"])

    def test_outer_verification_failure_keeps_worker_stderr_in_final_output(self):
        report_dir = self.root / "failed-verification"
        archive = self.root / "synthetic.zip"
        archive.write_bytes(b"synthetic package test input")
        def fail_with_existing_logs(*unused):
            (report_dir / "worker.log").write_bytes(b"auto module initialization diagnostic")
            (report_dir / "backend-gpu.log").write_bytes(b"strict module initialization diagnostic")
            raise RuntimeError("Original worker response failure")
        output = io.StringIO()
        with patch.object(package, "package_source_identity", return_value={"commit": CANDIDATE}), \
                patch.object(package, "unpack", side_effect=fail_with_existing_logs), \
                patch.dict(os.environ, {"QCOPILOTS_SEARCH_TEST_ROOT": str(self.root / "runs")}), \
                redirect_stdout(output), self.assertRaisesRegex(RuntimeError, "Original worker response failure"):
            package.verify(archive, self.root / "build", explicit_report=report_dir, model_dir=self.root)
        report = json.loads(output.getvalue())
        self.assertFalse(report["passed"])
        self.assertFalse(report["fullVerificationPassed"])
        self.assertEqual(report["error"], "Original worker response failure")
        self.assertEqual([item["requestedBackend"] for item in report["workerStderrDiagnostics"]], ["auto", "gpu"])
        self.assertEqual(report["workerStderrDiagnostics"][1]["headText"], "strict module initialization diagnostic")

    def test_strict_gpu_rejection_keeps_diagnostics_without_claiming_hardware(self):
        def launch_child(*args, **kwargs):
            mode = kwargs["env"]["QCOPILOTS_SEARCH_BACKEND"]
            kwargs["stderr"].write(mode + " synthetic protocol worker stderr\n")
            if mode == "cpu":
                reply = {"id": "backend-cpu", "ok": True, "result": {
                    "runtime": diagnostic(self.identity, "cpu", "cpu"), "embedding": [1.0] + [0.0] * 767}}
            else:
                reply = {"id": "backend-gpu", "ok": False, "error": {
                    "runtime": diagnostic(self.identity, "gpu", "", loaded=False), "message": "Strict GPU unavailable"}}
            shutdown = {"id": "worker-shutdown", "ok": True, "result": {"stopping": True}}
            child = Mock()
            child.stdin = io.StringIO()
            child.stdout = io.StringIO(json.dumps(reply) + "\n" + json.dumps(shutdown) + "\n")
            child.poll.return_value = None
            child.returncode = 0
            return child
        with patch.object(package.subprocess, "Popen", side_effect=launch_child), \
                patch.object(package, "inference_runtime_origins", return_value={}), \
                patch.object(package, "force_stop", return_value=None):
            outcomes = package.verify_backend_modes(self.binary / "qcs-worker.exe", self.application,
                self.root, self.root, {}, [], self.identity, [1.0] + [0.0] * 767, None, 5)
        self.assertTrue(outcomes["strictGpu"]["passed"])
        self.assertFalse(outcomes["strictGpu"]["hardwareTested"])
        report = {"passed": True, "fullVerificationPassed": True, "backendVerification": outcomes}
        with redirect_stdout(io.StringIO()):
            package.write_verification_report(report, self.root)
        self.assertTrue(report["passed"])
        self.assertTrue(report["fullVerificationPassed"])
        self.assertFalse(report["backendVerification"]["strictGpu"]["hardwareTested"])
        self.assertEqual(report["workerStderrDiagnostics"][1]["requestedBackend"], "gpu")
        self.assertEqual(report["workerStderrDiagnostics"][1]["headText"], "gpu synthetic protocol worker stderr" + os.linesep)

    def test_failed_gpu_initialization_keeps_response_and_remains_a_failure(self):
        identity = dict(self.identity, variant="macos-arm64-metal", backend="metal")
        failed = diagnostic(identity, "gpu", "metal", loaded=False)
        failed.update(deviceName="MTL0", deviceDescription="Synthetic Metal fixture",
                      gpuSelfTestPassed=False, textWarmupCompleted=False)
        reason = "Selected GPU does not support the compute self-test operation"
        children = []

        def launch_child(*args, **kwargs):
            mode = kwargs["env"]["QCOPILOTS_SEARCH_BACKEND"]
            if mode == "cpu":
                reply = {"id": "backend-cpu", "ok": True, "result": {
                    "runtime": diagnostic(identity, "cpu", "cpu"), "embedding": [1.0] + [0.0] * 767}}
            else:
                reply = {"id": "backend-gpu", "ok": False, "error": {"runtime": failed, "message": reason}}
            shutdown = {"id": "worker-shutdown", "ok": True, "result": {"stopping": True}}
            child = Mock()
            child.stdin = io.StringIO()
            child.stdout = io.StringIO(json.dumps(reply) + "\n" + json.dumps(shutdown) + "\n")
            child.poll.return_value = None
            child.returncode = 0
            children.append(child)
            return child

        report = {"passed": False, "fullVerificationPassed": False, "backendVerification": {}}
        with patch.object(package.subprocess, "Popen", side_effect=launch_child), \
                patch.object(package, "inference_runtime_origins", return_value={}), \
                patch.object(package, "force_stop", return_value=None), \
                self.assertRaisesRegex(RuntimeError, "Failed strict GPU initialization reported an active"):
            package.verify_backend_modes(self.binary / "qcs-worker", self.application,
                self.root, self.root, {}, [], identity, [1.0] + [0.0] * 767, None, 5,
                outcomes=report["backendVerification"])
        self.assertTrue(report["backendVerification"]["cpu"]["passed"])
        observed = report["backendVerification"]["strictGpu"]
        self.assertFalse(observed["passed"])
        self.assertEqual(observed["diagnostics"], failed)
        self.assertEqual(observed["reason"], reason)
        self.assertNotIn("hardwareTested", observed)
        self.assertEqual(observed["rawReply"]["sha256"],
                         package.file_digest(self.root / observed["rawReply"]["path"]))
        self.assertEqual(len(children), 2)
        self.assertTrue(all(child.stdin.closed and child.stdout.closed for child in children))
        output = io.StringIO()
        with redirect_stdout(output):
            package.write_verification_report(report, self.root)
        printed = json.loads(output.getvalue())
        self.assertFalse(printed["passed"])
        self.assertFalse(printed["fullVerificationPassed"])
        self.assertEqual(printed["backendVerification"]["strictGpu"], observed)

    def validate_identity(self, **overrides):
        return package.validate_build_identity(
            self.binary, self.application, {"commit": CANDIDATE}, **overrides)

    def gpu_payload(self, backend="cuda"):
        variant = {"cuda": "windows-x64-cuda-12.4", "hip": "windows-x64-rocm",
                   "vulkan": "windows-x64-vulkan"}[backend]
        self.identity.update(variant=variant, backend=backend)
        names = {
            "cuda": ("bin/ggml-cuda.dll", "bin/cudart64_12.dll", "bin/cublas64_12.dll", "bin/cublasLt64_12.dll"),
            "hip": ("bin/ggml-hip.dll", "bin/amdhip64_7.dll", "bin/rocblas/library/fixture_gfx1200.co",
                    "bin/rocblas/library/fixture_gfx1201.co"),
            "vulkan": ("bin/ggml-vulkan.dll", "bin/vulkan-1.dll"),
        }[backend]
        files = []
        for name in names:
            path = self.application / name
            path.parent.mkdir(parents=True, exist_ok=True)
            contents = ("SYNTHETIC PACKAGE TEST ONLY " + name).encode("utf-8")
            path.write_bytes(contents)
            files.append({"path": name, "size": len(contents), "sha256": sha(contents)})
        result = {"schemaVersion": 1, "variant": variant, "backend": backend, "files": files,
                  "resources": [{"name": backend, "version": "synthetic test only"}],
                  "verification": {"dependencyClosure": "verified"},
                  "gpuTargets": ["gfx1200", "gfx1201"] if backend == "hip" else []}
        self.write_payload(result)
        return result

    def write_payload(self, value):
        write_json(self.binary / "qcs-gpu-runtime.json", value)

    def verify_payload(self, **kwargs):
        return package.verify_gpu_payload(self.binary, self.application, self.identity, **kwargs)

    def final_programs(self, binary, variant):
        binary.mkdir(parents=True, exist_ok=True)
        suffix = ".exe" if variant.startswith("windows-") else ""
        paths = {}
        for name in package.APPLICATION_PROGRAMS:
            path = binary / (name + suffix)
            path.write_bytes(("SYNTHETIC FINAL INSTALLED PROGRAM " + name).encode())
            paths[name] = path
        return paths

    def test_final_program_inventory_reads_each_platform_installed_files_before_execution(self):
        for variant, layout in (("windows-x64-cpu", "bin"), ("linux-x64-cpu", "bin"),
                                ("macos-arm64-metal", "qcs-gui.app/Contents/MacOS")):
            with self.subTest(variant=variant):
                application = self.root / variant
                binary = application / layout
                programs = self.final_programs(binary, variant)
                identity = {**self.identity, "variant": variant}
                with patch.object(package.subprocess, "Popen") as start, patch.object(package.subprocess, "run") as run:
                    inventory = package.installed_program_inventory(binary, application, identity, "a" * 64)
                start.assert_not_called()
                run.assert_not_called()
                self.assertEqual(inventory["candidateCommit"], CANDIDATE)
                self.assertEqual(inventory["variant"], variant)
                self.assertEqual(inventory["packageSha256"], "a" * 64)
                self.assertEqual(set(inventory["programs"]), set(package.APPLICATION_PROGRAMS))
                for name, path in programs.items():
                    self.assertEqual(inventory["programs"][name], {
                        "path": path.relative_to(application).as_posix(), "bytes": path.stat().st_size,
                        "sha256": package.file_digest(path)})

    def test_final_program_inventory_requires_all_four_package_manager_owned_programs(self):
        programs = self.final_programs(self.binary, self.identity["variant"])
        installed = {path.resolve() for path in programs.values()}
        for name, path in programs.items():
            with self.subTest(name=name):
                with self.assertRaisesRegex(RuntimeError, "verified package files"):
                    package.installed_program_inventory(self.binary, self.application, self.identity, "a" * 64,
                                                        installed - {path.resolve()})
        contents = programs["qcs-cli"].read_bytes()
        programs["qcs-cli"].write_bytes(b"")
        with self.assertRaisesRegex(RuntimeError, "empty"):
            package.installed_program_inventory(self.binary, self.application, self.identity, "a" * 64)
        programs["qcs-cli"].write_bytes(contents)
        programs["qcs-worker"].unlink()
        with self.assertRaisesRegex(RuntimeError, "verified package files"):
            package.installed_program_inventory(self.binary, self.application, self.identity, "a" * 64)

    def test_final_program_inventory_rejects_executables_outside_application(self):
        binary = self.root / "outside-programs"
        self.final_programs(binary, self.identity["variant"])
        with self.assertRaisesRegex(RuntimeError, "verified package files"):
            package.installed_program_inventory(binary, self.application, self.identity, "a" * 64)

    def test_final_program_stability_rejects_each_changed_program_and_original_package(self):
        programs = self.final_programs(self.binary, self.identity["variant"])
        archive = self.root / "synthetic-package.zip"
        archive.write_bytes(b"SYNTHETIC ORIGINAL PACKAGE BYTES")
        inventory = package.installed_program_inventory(self.binary, self.application, self.identity,
                                                        package.file_digest(archive))
        package.verify_program_stability(inventory, self.binary, self.application, self.identity, archive)
        for name, path in programs.items():
            with self.subTest(name=name):
                original = path.read_bytes()
                path.write_bytes(original + b"changed during verification")
                with self.assertRaisesRegex(RuntimeError, "program bytes changed"):
                    package.verify_program_stability(inventory, self.binary, self.application, self.identity, archive)
                path.write_bytes(original)
        archive.write_bytes(b"CHANGED PACKAGE BYTES")
        with self.assertRaisesRegex(RuntimeError, "Original package bytes changed"):
            package.verify_program_stability(inventory, self.binary, self.application, self.identity, archive)

    def test_installed_source_combination_only_allows_linux_deb_and_package_manager_manifest(self):
        archive, manifest = self.root / "original.deb", self.root / "dpkg-files.txt"
        archive.write_bytes(b"SYNTHETIC DEB SOURCE IDENTITY")
        manifest.write_text("/usr/bin/qcs-worker\n", encoding="utf-8")
        with patch.object(package.sys, "platform", "linux"):
            package.validate_installed_package_source(archive, self.application, manifest)
            for source, listing in ((None, manifest), (archive, None),
                                    (self.root / "absent.deb", manifest),
                                    (archive, self.root / "absent-manifest.txt")):
                with self.subTest(source=source, listing=listing), self.assertRaisesRegex(RuntimeError, "original DEB"):
                    package.validate_installed_package_source(source, self.application, listing)
            for suffix in (".zip", ".exe", ".tar.gz", ".dmg"):
                wrong = self.root / ("wrong" + suffix)
                wrong.write_bytes(b"NOT A DEB")
                with self.subTest(suffix=suffix), self.assertRaisesRegex(RuntimeError, "original DEB"):
                    package.validate_installed_package_source(wrong, self.application, manifest)
        for platform in ("win32", "darwin"):
            with patch.object(package.sys, "platform", platform), self.assertRaisesRegex(RuntimeError, "requires Linux"):
                package.validate_installed_package_source(archive, self.application, manifest)

    def test_relocated_identity_preserves_candidate_variant_and_manifest_hashes(self):
        result = self.validate_identity(expected_variant="windows-x64-cuda-12.4")
        self.assertEqual(result, self.identity)
        self.assertEqual(result["candidateCommit"], CANDIDATE)

    def test_identity_rejects_wrong_candidate_or_requested_variant(self):
        with self.assertRaisesRegex(RuntimeError, "variant"):
            self.validate_identity(expected_variant="windows-x64-rocm")
        self.identity["candidateCommit"] = "2" * 40
        self.write_identity()
        with self.assertRaisesRegex(RuntimeError, "candidate"):
            self.validate_identity()

    def test_identity_rejects_noncanonical_metadata(self):
        baseline = copy.deepcopy(self.identity)
        cases = (("candidateCommit", "1234567"), ("candidateCommit", "A" * 40),
                 ("attempt", True), ("attempt", 0), ("attempt", "2"),
                 ("version", ""), ("schemaVersion", 2), ("schemaVersion", True),
                 ("variant", "missing-variant"), ("packageVariant", "windows-x64-rocm"),
                 ("toolkitVersion", "12.8"),
                 ("backend", "hip"), ("workingTreeModified", "false"),
                 ("workingTreeModified", 1), ("workingTreeModified", 0))
        for field, value in cases:
            with self.subTest(field=field, value=value):
                self.identity = {**baseline, field: value}
                self.write_identity()
                with self.assertRaises(RuntimeError):
                    self.validate_identity()

    def test_sdk_package_identity_and_outer_name_are_bound(self):
        for selected in VARIANTS["variants"]:
            identity = {**self.identity, "variant": selected["id"], "backend": selected["backend"],
                        "packageVariant": selected["packageVariant"], "toolkitVersion": selected.get("toolkitVersion", "")}
            self.identity = identity
            self.write_identity()
            self.validate_identity()
            suffix = ".dmg" if selected["id"].startswith("macos-") else ".zip"
            name = "qcs-0.1.0-" + identity["packageVariant"] + suffix
            package.validate_package_name(Path(name), identity)
            if selected["id"] == "windows-x64-rocm":
                package.validate_package_name(Path("qcs-0.1.0-" + identity["packageVariant"] + "-setup.zip"), identity)
            for wrong in (name.replace("0.1.0", "0.3.0", 1), name.replace(identity["packageVariant"], "other"), name + ".bak"):
                with self.subTest(name=wrong), self.assertRaisesRegex(RuntimeError, "filename"):
                    package.validate_package_name(Path(wrong), identity)
            if selected["backend"] in ("hip", "vulkan"):
                with self.assertRaisesRegex(RuntimeError, "filename"):
                    package.validate_package_name(Path("qcs-0.1.0-" + selected["id"] + suffix), identity)
                self.identity["toolkitVersion"] = "0.0.0"
                self.write_identity()
                with self.assertRaisesRegex(RuntimeError, "SDK"):
                    self.validate_identity()

    def test_identity_rejects_missing_or_ambiguous_identity(self):
        other = self.application / "Resources/qcs/build-identity.json"
        write_json(other, self.identity)
        with self.assertRaisesRegex(RuntimeError, "exactly one"):
            self.validate_identity()
        other.unlink()
        (self.materials / "build-identity.json").unlink()
        with self.assertRaisesRegex(RuntimeError, "exactly one"):
            self.validate_identity()

    def test_identity_rejects_changed_manifest_and_dependency_bytes(self):
        for name in ("build-variants.json", "dependencies.lock.json", "gpu-sdk.lock.json", "gpu-license-sources.lock.json"):
            with self.subTest(name=name):
                path = self.materials / name
                original = path.read_bytes()
                path.write_bytes(original + b"\n")
                with self.assertRaisesRegex(RuntimeError, "differs"):
                    self.validate_identity()
                path.write_bytes(original)

    def test_identity_rejects_duplicate_variant_even_with_matching_hash(self):
        variants = copy.deepcopy(VARIANTS)
        variants["variants"].append(copy.deepcopy(variants["variants"][3]))
        write_json(self.materials / "build-variants.json", variants)
        self.identity["variantsSha256"] = package.file_digest(self.materials / "build-variants.json")
        self.write_identity()
        with self.assertRaisesRegex(RuntimeError, "unknown variant"):
            self.validate_identity()

    def test_identity_supports_macos_bundle_resources(self):
        binary = self.application / "qcs-gui.app/Contents/MacOS"
        resources = binary.parent / "Resources/qcs"
        binary.mkdir(parents=True)
        for filename in ("build-variants.json", "dependencies.lock.json", "gpu-sdk.lock.json", "gpu-license-sources.lock.json"):
            resources.mkdir(parents=True, exist_ok=True)
            (resources / filename).write_bytes((self.materials / filename).read_bytes())
        self.identity.update(variant="macos-arm64-metal", backend="metal",
                             packageVariant="macos-arm64-metal", toolkitVersion="")
        write_json(resources / "build-identity.json", self.identity)
        self.assertEqual(package.validate_build_identity(binary, self.application, {"commit": CANDIDATE}),
                         self.identity)

    def test_gpu_payload_hashes_every_installed_dependency_after_relocation(self):
        for backend in ("cuda", "hip", "vulkan"):
            with self.subTest(backend=backend):
                payload = self.gpu_payload(backend)
                installed = {(self.application / row["path"]).resolve() for row in payload["files"]}
                result = self.verify_payload(installed_files=installed)
                self.assertTrue(result["passed"])
                self.assertTrue(result["required"])
                self.assertEqual(result["files"], len(installed))
                self.assertEqual(result["manifestSha256"], package.file_digest(self.binary / "qcs-gpu-runtime.json"))

    def test_gpu_payload_rejects_missing_manifest_and_wrong_variant(self):
        with self.assertRaisesRegex(RuntimeError, "no runtime dependency manifest"):
            self.verify_payload()
        payload = self.gpu_payload()
        payload["variant"] = "windows-x64-rocm"
        self.write_payload(payload)
        with self.assertRaisesRegex(RuntimeError, "variant"):
            self.verify_payload()

    def test_gpu_payload_rejects_boolean_schema_and_byte_count(self):
        payload = self.gpu_payload()
        self.write_payload({**payload, "schemaVersion": True})
        with self.assertRaises(RuntimeError):
            self.verify_payload()
        dependency = self.application / payload["files"][0]["path"]
        dependency.write_bytes(b"x")
        payload["files"][0].update(size=True, sha256=sha(b"x"))
        self.write_payload(payload)
        with self.assertRaises(RuntimeError):
            self.verify_payload()

    def test_gpu_payload_requires_unambiguous_license_component_identities(self):
        payload = self.gpu_payload()
        for resources in ([], [{"name": "cuda"}, {"name": "cuda"}]):
            with self.subTest(resources=resources):
                self.write_payload({**payload, "resources": resources})
                with self.assertRaises(RuntimeError):
                    self.verify_payload()

    def test_gpu_payload_requires_verified_dependency_closure(self):
        payload = self.gpu_payload()
        del payload["verification"]
        self.write_payload(payload)
        with self.assertRaises(RuntimeError):
            self.verify_payload()
        for value in (None, "skipped", "failed", True):
            with self.subTest(dependencyClosure=value):
                self.write_payload({**payload, "verification": {"dependencyClosure": value}})
                with self.assertRaises(RuntimeError):
                    self.verify_payload()

    def test_gpu_runtime_requires_application_local_compiler_dependencies(self):
        payload = self.gpu_payload()
        payload["applicationDependencies"] = ["vcruntime140_1.dll"]
        self.write_payload(payload)
        with self.assertRaisesRegex(RuntimeError, "application-local dependency"):
            self.verify_payload()
        runtime = self.binary / "vcruntime140_1.dll"
        runtime.write_bytes(b"compiler runtime fixture")
        self.assertTrue(self.verify_payload()["passed"])
        installed = {(self.application / row["path"]).resolve() for row in payload["files"]}
        with self.assertRaisesRegex(RuntimeError, "application-local dependency"):
            self.verify_payload(installed_files=installed)
        installed.add(runtime.resolve())
        self.assertTrue(self.verify_payload(installed_files=installed)["passed"])
        payload["applicationDependencies"] = ["../vcruntime140_1.dll"]
        self.write_payload(payload)
        with self.assertRaises(RuntimeError):
            self.verify_payload()

    def test_gpu_payload_rejects_changed_missing_or_uninstalled_file(self):
        payload = self.gpu_payload()
        dependency = self.application / payload["files"][-1]["path"]
        original = dependency.read_bytes()
        dependency.write_bytes(original[::-1])
        with self.assertRaisesRegex(RuntimeError, "bytes differ"):
            self.verify_payload()
        dependency.write_bytes(original)
        with self.assertRaisesRegex(RuntimeError, "package manager manifest"):
            self.verify_payload(installed_files=set())
        dependency.unlink()
        with self.assertRaisesRegex(RuntimeError, "invalid path"):
            self.verify_payload()

    def test_gpu_payload_rejects_duplicate_and_escaping_paths(self):
        baseline = self.gpu_payload()
        for name in ("../outside.dll", "/outside.dll", "C:/sdk/driver.dll", "bin/../../outside.dll",
                     "bin\\..\\..\\outside.dll"):
            with self.subTest(path=name):
                payload = copy.deepcopy(baseline)
                payload["files"][0]["path"] = name
                self.write_payload(payload)
                with self.assertRaises(RuntimeError):
                    self.verify_payload()
        baseline["files"].append(copy.deepcopy(baseline["files"][0]))
        self.write_payload(baseline)
        with self.assertRaisesRegex(RuntimeError, "invalid path"):
            self.verify_payload()

    def test_rocm_payload_keeps_exactly_both_rdna4_targets(self):
        baseline = self.gpu_payload("hip")
        for targets in ([], ["gfx1200"], ["gfx1201"], ["gfx1100", "gfx1200", "gfx1201"],
                        ["gfx1100", "gfx1101"]):
            with self.subTest(targets=targets):
                payload = {**baseline, "gpuTargets": targets}
                self.write_payload(payload)
                with self.assertRaisesRegex(RuntimeError, "RDNA4"):
                    self.verify_payload()

    def test_cpu_and_system_metal_need_no_external_sdk_payload(self):
        for variant, backend in (("windows-x64-cpu", "cpu"), ("macos-arm64-metal", "metal")):
            with self.subTest(backend=backend):
                self.identity.update(variant=variant, backend=backend)
                self.assertEqual(self.verify_payload(), {"passed": True, "required": False})

    def setup_payload(self):
        directory = self.root / "rocm-offline-setup"
        directory.mkdir()
        (directory / "setup.exe").write_bytes(b"SYNTHETIC SETUP TEST ONLY")
        records = []
        for name in ("bin/qcs-gui.exe", "bin/ggml-hip.dll", "bin/rocblas/library/gfx1200.co",
                     "bin/rocblas/library/gfx1201.co"):
            path = directory / "payload" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            contents = ("SYNTHETIC OFFLINE PAYLOAD " + name).encode("utf-8")
            path.write_bytes(contents)
            records.append({"path": name, "bytes": len(contents), "sha256": sha(contents)})
        manifest = {"schemaVersion": 1, "variant": "windows-x64-rocm", "files": records,
                    "totalBytes": sum(record["bytes"] for record in records)}
        write_json(directory / "payload-manifest.json", manifest)
        return directory, manifest

    def test_offline_rocm_setup_accepts_complete_hashed_payload(self):
        directory, manifest = self.setup_payload()
        self.assertEqual(package.validate_setup_payload(directory), manifest)

    def test_offline_rocm_setup_rejects_tampered_payload_hash_and_total(self):
        directory, manifest = self.setup_payload()
        path = directory / "payload" / manifest["files"][0]["path"]
        original = path.read_bytes()
        path.write_bytes(original[::-1])
        with self.assertRaisesRegex(RuntimeError, "changed"):
            package.validate_setup_payload(directory)
        path.write_bytes(original)
        changed = copy.deepcopy(manifest)
        changed["files"][0]["sha256"] = "0" * 64
        write_json(directory / "payload-manifest.json", changed)
        with self.assertRaisesRegex(RuntimeError, "changed"):
            package.validate_setup_payload(directory)
        write_json(directory / "payload-manifest.json", {**manifest, "totalBytes": manifest["totalBytes"] + 1})
        with self.assertRaisesRegex(RuntimeError, "inventory"):
            package.validate_setup_payload(directory)

    def test_offline_rocm_setup_rejects_extra_or_missing_payload(self):
        directory, manifest = self.setup_payload()
        extra = directory / "unexpected-file.txt"
        extra.write_bytes(b"extra")
        with self.assertRaisesRegex(RuntimeError, "contain only"):
            package.validate_setup_payload(directory)
        extra.unlink()
        extra = directory / "payload/extra.dll"
        extra.write_bytes(b"extra")
        with self.assertRaisesRegex(RuntimeError, "inventory"):
            package.validate_setup_payload(directory)
        extra.unlink()
        (directory / "payload" / manifest["files"][0]["path"]).unlink()
        with self.assertRaisesRegex(RuntimeError, "missing"):
            package.validate_setup_payload(directory)

    def test_offline_setup_is_reserved_for_rocm_and_rejects_ambiguous_paths(self):
        directory, manifest = self.setup_payload()
        write_json(directory / "payload-manifest.json", {**manifest, "variant": "windows-x64-cpu"})
        with self.assertRaisesRegex(RuntimeError, "ROCm"):
            package.validate_setup_payload(directory)
        duplicate = copy.deepcopy(manifest)
        duplicate["files"].append(copy.deepcopy(duplicate["files"][0]))
        write_json(directory / "payload-manifest.json", duplicate)
        with self.assertRaisesRegex(RuntimeError, "ambiguous"):
            package.validate_setup_payload(directory)
        escape = copy.deepcopy(manifest)
        escape["files"][0]["path"] = "../setup.exe"
        write_json(directory / "payload-manifest.json", escape)
        with self.assertRaisesRegex(RuntimeError, "escapes"):
            package.validate_setup_payload(directory)

    def test_automatic_gpu_and_explicit_cpu_diagnostics_report_actual_device(self):
        for backend in ("cuda", "hip", "vulkan", "metal"):
            identity = {"variant": "synthetic-" + backend, "backend": backend}
            for mode, selected in (("auto", backend), ("gpu", backend), ("cpu", "cpu"), ("auto", "cpu")):
                with self.subTest(backend=backend, mode=mode, selected=selected):
                    result = diagnostic(identity, mode, selected)
                    self.assertIs(package.validate_backend_diagnostics(result, identity, mode), result)

    def test_strict_gpu_cannot_report_successful_cpu_fallback(self):
        result = diagnostic(self.identity, "gpu", "cpu")
        with self.assertRaisesRegex(RuntimeError, "silently fell back"):
            package.validate_backend_diagnostics(result, self.identity, "gpu")
        result = diagnostic(self.identity, "cpu", "cuda")
        with self.assertRaisesRegex(RuntimeError, "unexpectedly used a GPU"):
            package.validate_backend_diagnostics(result, self.identity, "cpu")

    def test_absent_gpu_requires_failed_load_without_active_backend(self):
        result = diagnostic(self.identity, "gpu", "", loaded=False)
        self.assertIs(package.validate_backend_diagnostics(result, self.identity, "gpu", loaded=False), result)
        with self.assertRaisesRegex(RuntimeError, "identity or request"):
            package.validate_backend_diagnostics(result, self.identity, "gpu", loaded=True)
        for changed in ({"gpuSelected": True}, {"selectedBackend": "cpu"}, {"selectedBackend": "cuda"}):
            with self.subTest(changed=changed), self.assertRaisesRegex(RuntimeError, "active inference backend"):
                package.validate_backend_diagnostics({**result, **changed}, self.identity, "gpu", loaded=False)

    def test_fallback_requires_reason_and_diagnostics_match_requested_identity(self):
        baseline = diagnostic(self.identity)
        for changed in ({"fallbackReason": ""}, {"deviceName": ""}, {"selectedBackend": "vulkan"},
                        {"gpuSelected": True}, {"gpuSelected": 0}, {"loaded": 1},
                        {"buildVariant": "windows-x64-rocm"}, {"packagedBackend": "hip"},
                        {"requestedBackend": "cpu"}):
            with self.subTest(changed=changed), self.assertRaises(RuntimeError):
                package.validate_backend_diagnostics({**baseline, **changed}, self.identity, "auto")
        for result in (None, [], "cpu"):
            with self.subTest(result=result), self.assertRaises(RuntimeError):
                package.validate_backend_diagnostics(result, self.identity, "auto")

    def test_isolated_environment_removes_sdk_and_runtime_overrides(self):
        contaminated = {
            "SystemRoot": str(self.root / "Windows"), "PATH": str(self.root / "SDK/bin"),
            "CUDA_PATH": "cuda sdk", "CUDA_PATH_V12_4": "versioned sdk", "CUDACXX": "nvcc",
            "HIP_PATH": "hip sdk", "HIPCXX": "clang", "ROCM_PATH": "rocm sdk",
            "ROCBLAS_TENSILE_LIBPATH": "external kernels", "HIPBLASLT_TENSILE_LIBPATH": "external kernels",
            "VULKAN_SDK": "vulkan sdk", "VK_DRIVER_FILES": "external driver", "VK_LAYER_PATH": "external layer",
            "QT_PLUGIN_PATH": "development qt", "QML_IMPORT_PATH": "development qml",
            "QML2_IMPORT_PATH": "development qml2", "QCOPILOTS_SEARCH_BACKEND": "cpu",
            "LD_LIBRARY_PATH": "sdk lib", "LD_PRELOAD": "injected library",
            "DYLD_LIBRARY_PATH": "sdk dylib", "DYLD_FRAMEWORK_PATH": "sdk framework",
            "DYLD_INSERT_LIBRARIES": "injected dylib", "LIBRARY_PATH": "link libs",
            "LIB": "msvc libs", "INCLUDE": "msvc headers", "TEMP": str(self.root / "temp"),
            "LANG": "zh_CN.UTF-8",
        }
        with patch.dict(os.environ, contaminated, clear=True):
            before = dict(os.environ)
            result = package.isolated_environment()
            self.assertEqual(dict(os.environ), before)
        self.assertEqual({key.upper() for key in result}, {"SYSTEMROOT", "PATH", "TEMP", "LANG"})
        if os.name == "nt":
            expected = os.pathsep.join((str(self.root / "Windows/System32"), str(self.root / "Windows")))
        else:
            expected = "/usr/bin:/bin"
        self.assertEqual(result["PATH"], expected)
        self.assertNotIn("SDK", result["PATH"])

    def arm_runtime(self, gpu=True):
        executable = self.application / "qcs-gui.app/Contents/MacOS/qcs-worker"
        executable.parent.mkdir(parents=True, exist_ok=True)
        executable.write_bytes(b"SYNTHETIC TEST EXECUTABLE ONLY")
        names = ["libllama.0.dylib", "libmtmd.0.dylib", "libggml.0.dylib", "libggml-base.0.dylib",
                 "libggml-cpu.dylib"]
        if gpu:
            names.append("libggml-metal.dylib")
        libraries = []
        for name in names:
            path = executable.parent.parent / "Frameworks" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(("SYNTHETIC " + name).encode())
            libraries.append(str(path))
        identity = {"variant": "macos-arm64-metal", "backend": "metal"}
        return executable, libraries, diagnostic(identity, selected="metal" if gpu else "cpu")

    def test_apple_silicon_verifies_shared_inference_and_metal_roles(self):
        executable, libraries, diagnostics = self.arm_runtime()
        with patch.object(package.platform, "machine", return_value="arm64"), \
                patch.object(package.sys, "platform", "darwin"), \
                patch.object(package, "verify_library_origins", return_value=libraries):
            result = package.inference_runtime_origins(object(), executable, self.application, diagnostics=diagnostics)
        self.assertEqual(result["linkage"], "shared-backend-dispatch")
        self.assertEqual(result["architecture"], "arm64")
        self.assertEqual({row["role"] for row in result["libraries"]},
                         {"llama", "mtmd", "ggml", "ggml-base", "cpu-backend", "gpu-backend"})
        self.assertEqual(result["workerExecutable"]["sha256"], package.file_digest(executable))

    def test_apple_silicon_rejects_missing_cpu_or_selected_gpu_module(self):
        executable, libraries, diagnostics = self.arm_runtime()
        for missing in ("libggml-cpu.dylib", "libggml-metal.dylib", "libggml-base.0.dylib"):
            mapped = [name for name in libraries if Path(name).name != missing]
            with self.subTest(missing=missing), \
                    patch.object(package.platform, "machine", return_value="arm64"), \
                    patch.object(package.sys, "platform", "darwin"), \
                    patch.object(package, "verify_library_origins", return_value=mapped), \
                    self.assertRaises(RuntimeError):
                package.inference_runtime_origins(object(), executable, self.application, diagnostics=diagnostics)

    def test_apple_silicon_cpu_fallback_still_requires_shared_runtime(self):
        executable, libraries, diagnostics = self.arm_runtime(gpu=False)
        with patch.object(package.platform, "machine", return_value="arm64"), \
                patch.object(package.sys, "platform", "darwin"), \
                patch.object(package, "verify_library_origins", return_value=libraries):
            result = package.inference_runtime_origins(object(), executable, self.application, diagnostics=diagnostics)
        self.assertEqual(result["linkage"], "shared-backend-dispatch")
        self.assertNotIn("gpu-backend", {row["role"] for row in result["libraries"]})


if __name__ == "__main__":
    unittest.main(verbosity=2)
