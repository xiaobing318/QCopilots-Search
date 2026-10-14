#!/usr/bin/env python3
"""Exercise a real distribution archive after relocation without the developer SDK."""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import importlib.util
import json
import math
import os
import platform
from pathlib import Path, PurePosixPath
import queue
import re
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import uuid
import wave
import zipfile

sys.dont_write_bytecode = True
PRODUCT = "qcs"
GUI_EXECUTABLE = "qcs-gui"
APPLICATION_PROGRAMS = (GUI_EXECUTABLE, "qcs-service", "qcs-worker", "qcs-cli")
WORKER_STDERR_LIMIT = 16 * 1024
WORKER_STDERR_HEAD = 4 * 1024
WINDOWS_PRODUCT_KEY = r"Software\QCopilots\qcopilots-search"
WINDOWS_UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\qcopilots-search"
SOURCE = Path(__file__).resolve().parent.parent
MODEL_ARTIFACTS = {
    "embeddinggemma-2-Q8_0.gguf": (309855456, "2188ac1deca4b77dffefd603c2776a9d76d9d74ec01841392982ebb840b09135"),
    "mmproj-embeddinggemma-2-Q8_0.gguf": (554821024, "c4a8a52691ecef40618438928bdf9e68379b854e24166f292592353db0aab64f"),
}
REFERENCE_HASHES = {
    "screen.png": "a8a0c831fe4746d00c95e3ab436d53e4cc9046a774fe4f0f6edb73d1a7d55f9b",
    "speech.wav": "bfbc029af6c0bacdbaccfa6c695d6772373453249386fa6a62a7b02a31d33e1f",
    "vectors.json": "0ec9336aa53fb0d5bafa074cd56122e2fd914cb73f080b4f98332872089f6dd8",
}


def file_digest(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def worker_stderr_diagnostics(report_dir):
    """Include bounded, actual worker output without interpreting a failure's cause."""
    diagnostics = []
    for filename, mode in (("worker.log", "auto"), ("backend-cpu.log", "cpu"), ("backend-gpu.log", "gpu")):
        entry = {"source": filename, "process": "qcs-worker", "stream": "stderr", "requestedBackend": mode}
        try:
            with (report_dir / filename).open("rb") as stream:
                total = os.fstat(stream.fileno()).st_size
                if total <= WORKER_STDERR_LIMIT:
                    head, tail = stream.read(total), b""
                else:
                    head = stream.read(WORKER_STDERR_HEAD)
                    stream.seek(total - (WORKER_STDERR_LIMIT - WORKER_STDERR_HEAD))
                    tail = stream.read(WORKER_STDERR_LIMIT - WORKER_STDERR_HEAD)
            captured = len(head) + len(tail)
            entry.update(available=True, totalBytes=total, capturedBytes=captured,
                         omittedBytes=total - captured, truncated=total > captured,
                         encoding="utf-8", decodingErrors="replace",
                         headText=head.decode("utf-8", errors="replace"),
                         tailText=tail.decode("utf-8", errors="replace"))
        except FileNotFoundError:
            # A preceding failure may have prevented this worker from starting.
            continue
        except OSError as error:
            entry.update(available=False, readError=str(error))
        diagnostics.append(entry)
    return diagnostics


def write_verification_report(report, report_dir):
    # Called after cleanup, including when backend verification never returned.
    # Diagnostics must not change the existing pass/fail or hardware-test result.
    report["workerStderrDiagnostics"] = worker_stderr_diagnostics(report_dir)
    encoded = json.dumps(report, indent=2, ensure_ascii=False)
    (report_dir / "report.json").write_text(encoded, encoding="utf-8")
    # Preserve JSON values while preventing actual stderr from becoming a native
    # GitHub Actions command, including the legacy command syntax.
    print(encoded.replace("::", r"\u003a\u003a").replace("##[", r"\u0023\u0023["), flush=True)


def installed_program_inventory(binary, application, identity, package_sha256, installed_files=None):
    """Read final installed files independently of any application response."""
    application = application.resolve()
    suffix = ".exe" if identity["variant"].startswith("windows-") else ""
    programs = {}
    for name in APPLICATION_PROGRAMS:
        path = binary / (name + suffix)
        resolved = path.resolve()
        if (not path.is_file() or path.is_symlink() or application not in resolved.parents
                or (installed_files is not None and resolved not in installed_files)):
            raise RuntimeError(f"Installed program is outside the verified package files: {path}")
        size = resolved.stat().st_size
        if size < 1:
            raise RuntimeError(f"Installed program is empty: {path}")
        programs[name] = {"path": resolved.relative_to(application).as_posix(),
                          "bytes": size, "sha256": file_digest(resolved)}
    return {"schemaVersion": 1, "kind": "qcs-installed-programs",
            "candidateCommit": identity.get("candidateCommit"), "variant": identity["variant"],
            "packageSha256": package_sha256, "applicationRoot": str(application), "programs": programs}


def verify_program_stability(inventory, binary, application, identity, package, installed_files=None):
    current = installed_program_inventory(binary, application, identity, inventory["packageSha256"], installed_files)
    if current != inventory:
        raise RuntimeError("Installed program bytes changed during package verification")
    if package is not None and file_digest(package) != inventory["packageSha256"]:
        raise RuntimeError("Original package bytes changed during package verification")


def validate_installed_package_source(package, installed_root, installed_manifest):
    if installed_root is None:
        return
    if (not sys.platform.startswith("linux") or package is None or package.suffix.lower() != ".deb"
            or not package.is_file() or package.is_symlink()
            or installed_manifest is None or not installed_manifest.is_file()):
        raise RuntimeError("Installed-tree verification requires Linux, the original DEB and its package-manager file manifest")


def model_verification(result, directory):
    """The installed CLI hashes its read-only cache against its compiled lock."""
    if result.get("valid") is not True or Path(result.get("directory", "")).resolve() != directory.resolve():
        raise RuntimeError("Installed CLI did not verify the explicit model cache")
    artifacts = result.get("artifacts", [])
    if len(artifacts) != len(MODEL_ARTIFACTS):
        raise RuntimeError("Installed model manifest does not contain both locked GGUF files")
    for artifact in artifacts:
        expected = MODEL_ARTIFACTS.get(artifact.get("name"))
        if (not expected or artifact.get("present") is not True or
                (artifact.get("bytes"), artifact.get("sha256")) != expected or
                artifact.get("expectedBytes") != expected[0]):
            raise RuntimeError(f"Installed model manifest differs from the locked artifacts: {artifact}")
    if {artifact["name"] for artifact in artifacts} != set(MODEL_ARTIFACTS):
        raise RuntimeError("Installed model manifest repeats a GGUF artifact")
    return result


def reference_inputs(destination):
    source = SOURCE / "tests/runtime/fixtures"
    destination.mkdir()
    for name, expected in REFERENCE_HASHES.items():
        if file_digest(source / name) != expected:
            raise RuntimeError(f"Immutable reference fixture changed: {name}")
        # The existing extractor assigns screenshot budget 280 by filename.
        # This immutable RGB PNG fits the 1536 pixel bound without rescaling.
        target = "Screenshot golden.png" if name == "screen.png" else name
        shutil.copyfile(source / name, destination / target)
    references = json.loads((destination / "vectors.json").read_text(encoding="utf-8"))
    if references.get("vector_version") != 1:
        raise RuntimeError("Unsupported immutable reference vector version")
    return references["vectors"]


def validate_vector(embedding, expected):
    def norm(values):
        if (not isinstance(values, list) or len(values) != 768 or
                any(type(value) not in (int, float) or not math.isfinite(value) for value in values)):
            raise RuntimeError("Packaged inference must produce 768 finite numeric components")
        length = math.sqrt(math.fsum(value * value for value in values))
        if abs(length - 1.0) > 0.0001:
            raise RuntimeError(f"Packaged inference vector is not L2 normalized: {length}")
        return length
    actual_norm, expected_norm = norm(embedding), norm(expected)
    cosine = math.fsum(a * b for a, b in zip(embedding, expected)) / (actual_norm * expected_norm)
    if cosine < 0.999:
        raise RuntimeError(f"Packaged inference differs from the immutable reference: cosine {cosine}")
    return {"dimensions": 768, "l2Norm": actual_norm, "cosine": cosine,
            "embedding": embedding, "expectedEmbedding": expected}


def inference_library_role(path):
    name = Path(path).name.lower()
    if re.fullmatch(r"(?:lib)?ggml-(?:cuda|hip|vulkan|metal)(?:\.dll|\.so(?:\.[0-9.]+)?|(?:\.[0-9.]+)?\.dylib)", name):
        return "gpu-backend"
    for role in ("cpu-backend", "ggml-base", "ggml", "llama", "mtmd"):
        stem = "ggml-cpu" if role == "cpu-backend" else role
        pattern = (r"^(?:lib)?ggml-cpu(?:-[a-z0-9_-]+)?(?:\.dll|\.so(?:\.[0-9.]+)?|(?:\.[0-9.]+)?\.dylib)$"
                   if role == "cpu-backend" else rf"^(?:lib)?{re.escape(stem)}(?:\.dll|\.so(?:\.[0-9.]+)?|(?:\.[0-9.]+)?\.dylib)$")
        if re.fullmatch(pattern, name):
            return role
    return None


def inference_runtime_origins(worker, executable, application, installed_files=None, diagnostics=None):
    if (application.resolve() not in executable.resolve().parents or
            (installed_files is not None and executable.resolve() not in installed_files)):
        raise RuntimeError("Inference worker executable is outside the installed package layout")
    libraries = verify_library_origins(worker, application, installed_files)
    mapped = []
    seen = set()
    for name in libraries:
        path = Path(name).resolve()
        role = inference_library_role(path)
        if role and path not in seen:
            seen.add(path)
            mapped.append({"path": str(path), "bytes": path.stat().st_size,
                           "sha256": file_digest(path), "role": role})
    architecture = platform.machine().lower()
    if architecture in ("x86_64", "amd64"):
        linkage = "shared-cpu-dispatch"
        required = {"llama", "mtmd", "ggml", "ggml-base", "cpu-backend"}
        if not required.issubset({entry["role"] for entry in mapped}):
            raise RuntimeError(f"Installed x64 worker did not map the complete inference runtime: {mapped}")
        if sys.platform == "darwin":
            plugin_root = executable.parent.parent / "PlugIns/ggml"
            if any(entry["role"] == "cpu-backend" and Path(entry["path"]).parent != plugin_root.resolve() for entry in mapped):
                raise RuntimeError("Installed macOS CPU backend did not load from Contents/PlugIns/ggml")
    elif sys.platform == "darwin" and architecture in ("arm64", "aarch64"):
        if diagnostics is None:
            linkage = "static-cpu"
            if mapped:
                raise RuntimeError("Apple Silicon package unexpectedly loaded shared inference libraries")
        else:
            linkage = "shared-metal" if mapped else "static-metal"
            if mapped and not {"llama", "mtmd", "ggml", "ggml-base"}.issubset({entry["role"] for entry in mapped}):
                raise RuntimeError("Apple Silicon package has an incomplete shared inference runtime")
    else:
        raise RuntimeError(f"No verified inference packaging contract for {sys.platform}/{architecture}")
    if diagnostics is not None:
        linkage = "shared-backend-dispatch"
        if not {"llama", "mtmd", "ggml", "ggml-base", "cpu-backend"}.issubset({entry["role"] for entry in mapped}):
            raise RuntimeError("Installed worker did not map all shared inference runtime roles")
        if diagnostics.get("gpuSelected") and "gpu-backend" not in {entry["role"] for entry in mapped}:
            raise RuntimeError("GPU inference did not load its packaged backend module")
    return {"linkage": linkage, "architecture": architecture, "cpuBaselineForced": False,
            "cpuDispatch": {"x64": architecture in ("x86_64", "amd64"), "arm64": False},
            "diagnostics": diagnostics,
            "workerExecutable": {"path": str(executable.resolve()), "bytes": executable.stat().st_size,
                                 "sha256": file_digest(executable)}, "libraries": mapped}


def worker_request(worker, replies, request, report_dir, timeout=180, accept_failure=False):
    worker.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
    worker.stdin.flush()
    try:
        raw = replies.get(timeout=timeout)
    except queue.Empty as error:
        raise RuntimeError(f"Packaged worker response timed out: {request['id']}") from error
    if raw is None:
        raise RuntimeError(f"Packaged worker exited without a response: {request['id']}")
    reply = json.loads(raw)
    path = report_dir / (request["id"] + "-reply.json")
    path.write_text(raw, encoding="utf-8")
    if reply.get("id") != request["id"] or (not accept_failure and reply.get("ok") is not True):
        raise RuntimeError(f"Packaged worker request failed: {reply}")
    return (reply if accept_failure else reply["result"]), {"path": path.name, "sha256": file_digest(path)}


def read_worker_replies(worker, replies):
    try:
        while True:
            line = worker.stdout.readline(16 * 1024 * 1024 + 2)
            if not line:
                break
            if len(line.encode("utf-8")) > 16 * 1024 * 1024 + 1 or not line.endswith("\n"):
                replies.put(None)
                return
            replies.put(line)
    finally:
        replies.put(None)


def stop_worker(worker, replies, reader, report_dir):
    if worker.poll() is not None:
        raise RuntimeError(f"Packaged worker exited unexpectedly: {worker.returncode}")
    response, _ = worker_request(worker, replies, {"id": "worker-shutdown", "operation": "shutdown"}, report_dir, timeout=10)
    if response.get("stopping") is not True:
        raise RuntimeError("Packaged worker did not acknowledge shutdown")
    worker.stdin.close()
    worker.wait(timeout=20)
    reader.join(timeout=5)
    if reader.is_alive() or worker.returncode != 0:
        raise RuntimeError(f"Packaged worker shutdown failed: {worker.returncode}")
    worker.stdout.close()


def force_stop(process):
    """Bound cleanup of our child without preventing the other children exiting."""
    try:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
    except Exception as error:
        return str(error)
    return None


def real_model_smoke(worker, replies, executable, application, work, report_dir, verified_models, installed_files=None,
                     timeout=600, build_identity=None):
    fixtures = work / "model-references"
    references = reference_inputs(fixtures)
    vectors = []
    query, raw = worker_request(worker, replies, {"id": "model-query", "operation": "query",
                                                 "query": "payment declined error"}, report_dir, timeout=timeout)
    vectors.append({"name": "query", "modality": "text", "rawReply": raw,
                    "modelFingerprint": query.get("modelFingerprint"),
                    "extractionFingerprint": query.get("extractionFingerprint"),
                    **validate_vector(query.get("embedding"), references["query"])})
    for name, modality, filename in (("screen-280", "image", "Screenshot golden.png"), ("speech", "audio", "speech.wav")):
        result, raw = worker_request(worker, replies, {"id": "model-" + name, "operation": "extract",
            "path": str(fixtures / filename), "embed": True, "enableOcr": False, "maxSegments": 8}, report_dir, timeout=timeout)
        segments = [segment for segment in result.get("segments", []) if segment.get("modality") == modality]
        if (result.get("complete") is not True or result.get("state") != "complete" or
                result.get("embedded") is not True or result.get("error") or
                result.get("embeddingErrors") or len(segments) != 1 or len(result.get("segments", [])) != 1):
            raise RuntimeError(f"Packaged multimodal inference did not complete: {name}: {result}")
        if modality == "image" and segments[0].get("imageBudget") != 280:
            raise RuntimeError("Packaged screenshot extractor changed the reference image budget")
        if modality == "audio" and (segments[0].get("startMs"), segments[0].get("endMs")) != (0, 4000):
            raise RuntimeError("Packaged audio extractor changed the reference time window")
        vectors.append({"name": name, "modality": modality, "rawReply": raw,
                        "modelFingerprint": result.get("modelFingerprint"),
                        "extractionFingerprint": result.get("extractionFingerprint"),
                        **validate_vector(segments[0].get("embedding"), references[name])})
    for key in ("modelFingerprint", "extractionFingerprint"):
        fingerprints = {vector[key] for vector in vectors}
        if len(fingerprints) != 1 or not next(iter(fingerprints)):
            raise RuntimeError(f"Installed query and multimodal worker disagree on {key}")
    diagnostic_result, _ = worker_request(worker, replies, {"id": "runtime-diagnostics", "operation": "diagnostics"}, report_dir)
    diagnostics = diagnostic_result.get("runtime")
    if build_identity:
        validate_backend_diagnostics(diagnostics, build_identity, "auto")
    return {"passed": True, "dimensions": 768, "minimumCosine": 0.999,
            "modelDirectory": verified_models["directory"], "modelVerification": verified_models,
            "referenceHashes": REFERENCE_HASHES, "vectors": vectors,
            "runtime": inference_runtime_origins(worker, executable, application, installed_files, diagnostics)}


def verify_license_materials(binary, application, report_dir, installed_files=None):
    filename = "qcs-license-manifest.json"
    candidates = [root / "licenses/qcs-runtime" / filename for root in (
        binary.parent / "share/qcs", binary.parent / "Resources/qcs")]
    candidates = [path for path in candidates if path.is_file()]
    if len(candidates) != 1:
        raise RuntimeError("Distribution must contain one complete runtime license manifest")
    manifest = candidates[0]
    materials = manifest.parent
    for path in (materials, *materials.rglob("*")):
        if application.resolve() not in path.resolve().parents:
            raise RuntimeError(f"Runtime license material escapes the installed package: {path}")
        if installed_files is not None and path.is_file() and path.resolve() not in installed_files:
            raise RuntimeError(f"Runtime license material is absent from the package manager manifest: {path}")
    spec = importlib.util.spec_from_file_location("qcopilots_search_license_materials", SOURCE / "tools/qcs-license-materials.py")
    verifier = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(verifier)
    summary = verifier.verify_materials(materials)
    retained = report_dir / "runtime-license-materials"
    shutil.copytree(materials, retained)
    verifier.verify_materials(retained)
    return {**summary, "manifest": (retained / filename).relative_to(report_dir).as_posix(),
            "manifestSha256": file_digest(retained / filename), "installedManifestPath": str(manifest)}


class NsisInstallation:
    """Only exercise the real installer when it cannot replace a user install."""
    def __init__(self, package, destination):
        self.mutex = None
        if os.name != "nt":
            raise RuntimeError("NSIS installation can only be verified on Windows")
        from ctypes import wintypes
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateMutexW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
        self.kernel.CreateMutexW.restype = wintypes.HANDLE
        self.kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        self.kernel.ReleaseMutex.argtypes = (wintypes.HANDLE,)
        self.kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = self.kernel.CreateMutexW(None, False, r"Local\qcs-package-installer-tests")
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        if self.kernel.WaitForSingleObject(handle, 0) not in (0, 0x80):
            self.kernel.CloseHandle(handle)
            raise RuntimeError("Another NSIS package test is active in this Windows session")
        self.mutex = handle
        import winreg
        self.registry = winreg
        self.destination = destination.resolve()
        self.started = False
        self.start_menu = Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs/QCopilots Search"
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            for view in (winreg.KEY_WOW64_32KEY, winreg.KEY_WOW64_64KEY):
                for key in (WINDOWS_PRODUCT_KEY, WINDOWS_UNINSTALL_KEY):
                    try:
                        with winreg.OpenKey(hive, key, 0, winreg.KEY_READ | view):
                            raise RuntimeError("An existing QCopilots Search installation was found. Run installer verification in a clean Windows account or CI runner.")
                    except FileNotFoundError:
                        pass
        if self.start_menu.exists() or self.destination.exists():
            raise RuntimeError("Installer verification refuses an existing install or Start Menu directory")
        self.package = package

    def close(self):
        if self.mutex:
            self.kernel.ReleaseMutex(self.mutex)
            self.kernel.CloseHandle(self.mutex)
            self.mutex = None

    def __del__(self):
        self.close()

    def install(self):
        self.started = True
        # NSIS requires /D as the last argument, without quotes even with spaces.
        command = subprocess.list2cmdline([str(self.package), "/S"]) + " /D=" + str(self.destination)
        subprocess.run(command, check=True, timeout=180)
        with self.registry.OpenKey(self.registry.HKEY_CURRENT_USER, WINDOWS_UNINSTALL_KEY) as key:
            location = self.registry.QueryValueEx(key, "InstallLocation")[0]
        if Path(location).resolve() != self.destination:
            raise RuntimeError(f"Installer ignored the isolated destination: {location}")

    def uninstall(self):
        if not self.started:
            return
        uninstaller = self.destination / "Uninstall.exe"
        if not uninstaller.is_file():
            raise RuntimeError("Installer did not leave its owned uninstaller for cleanup")
        # _?= runs this exact uninstaller synchronously rather than spawning a
        # temporary copy. Its executable is the only file we remove afterward.
        command = subprocess.list2cmdline([str(uninstaller), "/S"]) + " _?=" + str(self.destination)
        subprocess.run(command, check=True, timeout=120)
        for key_name in (WINDOWS_PRODUCT_KEY, WINDOWS_UNINSTALL_KEY):
            try:
                with self.registry.OpenKey(self.registry.HKEY_CURRENT_USER, key_name):
                    raise RuntimeError(f"Uninstaller retained its registration: {key_name}")
            except FileNotFoundError:
                pass
        if self.start_menu.exists():
            raise RuntimeError("Uninstaller retained its Start Menu directory")
        remaining = [path for path in self.destination.rglob("*") if path.is_file() and path != uninstaller]
        if remaining:
            raise RuntimeError(f"Uninstaller retained installed payload: {remaining[:5]}")
        if uninstaller.exists():
            uninstaller.unlink()



def loaded_libraries(process):
    """Inspect our child process, so SDK fallback paths cannot hide missing files."""
    if os.name == "nt":
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        psapi.EnumProcessModules.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.HMODULE), wintypes.DWORD, ctypes.POINTER(wintypes.DWORD))
        psapi.GetModuleFileNameExW.argtypes = (wintypes.HANDLE, wintypes.HMODULE, wintypes.LPWSTR, wintypes.DWORD)
        handle = kernel.OpenProcess(0x0400 | 0x0010, False, process.pid)
        if not handle: raise ctypes.WinError(ctypes.get_last_error())
        try:
            capacity = 256
            while True:
                modules = (wintypes.HMODULE * capacity)()
                needed = wintypes.DWORD()
                if not psapi.EnumProcessModules(handle, modules, ctypes.sizeof(modules), ctypes.byref(needed)):
                    raise ctypes.WinError(ctypes.get_last_error())
                if needed.value <= ctypes.sizeof(modules):
                    break
                capacity = needed.value // ctypes.sizeof(wintypes.HMODULE) + 128
                if capacity > 32768:
                    raise RuntimeError("Loaded module inventory exceeds the verification limit")
            paths = []
            for index in range(needed.value // ctypes.sizeof(wintypes.HMODULE)):
                buffer = ctypes.create_unicode_buffer(32768)
                length = psapi.GetModuleFileNameExW(handle, modules[index], buffer, len(buffer))
                if not length:
                    raise ctypes.WinError(ctypes.get_last_error())
                if length >= len(buffer) - 1:
                    raise RuntimeError("A loaded module path was truncated")
                paths.append(buffer.value)
            return paths
        finally:
            kernel.CloseHandle(handle)
    if sys.platform.startswith("linux"):
        return sorted({line.split(maxsplit=5)[-1].strip() for line in Path(f"/proc/{process.pid}/maps").read_text().splitlines() if "/" in line})
    # Darwin lsof's txt descriptors enumerate mapped images. Ordinary open
    # files must never satisfy the inference-library presence requirements.
    result = subprocess.run(["/usr/sbin/lsof", "-a", "-p", str(process.pid), "-d", "txt", "-F0fn"],
                            capture_output=True, text=True, check=True, timeout=20)
    if "region info error" in result.stderr.lower():
        raise RuntimeError("macOS could not enumerate all mapped package images")
    return mapped_macos_paths(result.stdout)


def mapped_macos_paths(output):
    if "region info error" in output.lower():
        raise RuntimeError("macOS could not enumerate all mapped package images")
    paths, descriptor = [], None
    for field in output.split("\0"):
        field = field.lstrip("\n")
        if field.startswith("f"):
            descriptor = field[1:]
        elif descriptor == "txt" and field.startswith("n/"):
            paths.append(field[1:])
    return paths


def verify_library_origins(process, application, installed_files=None):
    names = ("qt6", "qtcore", "qtgui", "qtwidgets", "qtmultimedia", "qtpdf", "avcodec", "avformat", "avutil", "swscale", "swresample", "tesseract", "leptonica", "heif", "libraw", "de265", "ggml", "llama", "mtmd", "git2",
             "cudart", "cublas", "nvrtc", "nvjitlink", "amdhip", "hipblas", "rocblas", "rocm_kpack", "amd_comgr")
    checked = []
    for library in loaded_libraries(process):
        path = Path(library)
        qt_framework = path.name.startswith("Qt") and any(part.endswith(".framework") for part in path.parts)
        if qt_framework or any(name in path.name.lower() for name in names):
            if application.resolve() not in path.resolve().parents:
                raise RuntimeError(f"Packaged process loaded a development/system application dependency: {library}")
            if installed_files is not None and path.resolve() not in installed_files:
                raise RuntimeError(f"Loaded application dependency is absent from package manifest: {library}")
            checked.append(library)
    if not checked:
        raise RuntimeError("Cannot verify loaded package library origins")
    return checked


def isolated_environment():
    environment = os.environ.copy()
    for key in tuple(environment):
        if (key.startswith(("QT_", "QML_", "QML2_", "QCOPILOTS_SEARCH_", "CUDA_", "CUDACXX", "HIP_", "HIPCXX", "ROCM_", "ROCBLAS_", "HIPBLASLT_", "VULKAN_", "VK_"))
                or key in ("LD_LIBRARY_PATH", "LD_PRELOAD", "DYLD_LIBRARY_PATH", "DYLD_FRAMEWORK_PATH", "DYLD_INSERT_LIBRARIES", "LIBRARY_PATH", "LIB", "INCLUDE")):
            environment.pop(key, None)
    environment["PATH"] = os.pathsep.join((str(Path(os.environ["SystemRoot"]) / "System32"), os.environ["SystemRoot"])) if os.name == "nt" else "/usr/bin:/bin"
    return environment


def validate_build_identity(binary, application, source_identity, expected_variant=None):
    roots = (binary.parent / "share/qcs", binary.parent / "Resources/qcs")
    candidates = [root / "build-identity.json" for root in roots if (root / "build-identity.json").is_file()]
    if len(candidates) != 1:
        raise RuntimeError("Distribution must contain exactly one build identity")
    path = candidates[0]
    if application.resolve() not in path.resolve().parents:
        raise RuntimeError("Packaged build identity escapes the installed application")
    identity = json.loads(path.read_text(encoding="utf-8"))
    manifest = path.with_name("build-variants.json")
    variants = json.loads(manifest.read_text(encoding="utf-8"))["variants"]
    matching = [entry for entry in variants if entry["id"] == identity.get("variant")]
    if (type(identity.get("schemaVersion")) is not int or identity["schemaVersion"] != 1 or len(matching) != 1
            or matching[0]["backend"] != identity.get("backend")
            or type(identity.get("attempt")) is not int or identity["attempt"] < 1
            or not isinstance(identity.get("version"), str) or not identity["version"]):
        raise RuntimeError("Packaged build identity has an unknown variant or invalid metadata")
    if expected_variant and identity["variant"] != expected_variant:
        raise RuntimeError("Packaged variant differs from the requested verification variant")
    if file_digest(manifest) != identity.get("variantsSha256"):
        raise RuntimeError("Packaged variant manifest differs from its build identity")
    for filename, field in (("dependencies.lock.json", "dependencyLockSha256"),
                            ("gpu-sdk.lock.json", "gpuSdkLockSha256"),
                            ("gpu-license-sources.lock.json", "gpuLicenseLockSha256")):
        if file_digest(path.with_name(filename)) != identity.get(field):
            raise RuntimeError(f"Packaged dependency lock differs from its build identity: {filename}")
    commit = identity.get("candidateCommit")
    if commit is not None and (not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit)):
        raise RuntimeError("Packaged candidate identity is not a complete Git SHA")
    if source_identity.get("commit") and commit != source_identity["commit"]:
        raise RuntimeError("Packaged candidate differs from the verifier source checkout")
    if identity.get("workingTreeModified") is not None and type(identity["workingTreeModified"]) is not bool:
        raise RuntimeError("Packaged source modification state is invalid")
    return identity


def validate_backend_diagnostics(diagnostics, identity, requested, loaded=True):
    if not isinstance(diagnostics, dict):
        raise RuntimeError("Packaged worker did not return runtime backend diagnostics")
    if (diagnostics.get("buildVariant") != identity["variant"]
            or diagnostics.get("packagedBackend") != identity["backend"]
            or diagnostics.get("requestedBackend") != requested
            or diagnostics.get("loaded") is not loaded
            or type(diagnostics.get("gpuSelected")) is not bool):
        raise RuntimeError(f"Packaged runtime diagnostics differ from the installed identity or request: {diagnostics}")
    if not loaded:
        if diagnostics["gpuSelected"] or diagnostics.get("selectedBackend"):
            raise RuntimeError("Failed strict GPU initialization reported an active inference backend")
        return diagnostics
    selected = diagnostics.get("selectedBackend")
    if selected not in ("cpu", identity["backend"]) or not diagnostics.get("deviceName"):
        raise RuntimeError("Packaged runtime did not identify its actual selected device")
    if diagnostics["gpuSelected"] is not (selected != "cpu"):
        raise RuntimeError("Packaged runtime backend and GPU state disagree")
    if requested == "cpu" and selected != "cpu":
        raise RuntimeError("Explicit CPU inference unexpectedly used a GPU")
    if requested == "gpu" and (selected == "cpu" or not diagnostics["gpuSelected"]):
        raise RuntimeError("Strict GPU inference silently fell back to CPU")
    if requested == "auto" and selected == "cpu" and identity["backend"] != "cpu" and not diagnostics.get("fallbackReason"):
        raise RuntimeError("Automatic CPU fallback did not report its reason")
    return diagnostics


def verify_gpu_payload(binary, application, identity, installed_files=None):
    manifest = binary / "qcs-gpu-runtime.json"
    if identity["backend"] in ("cpu", "metal"):
        return {"passed": True, "required": False}
    if not manifest.is_file():
        raise RuntimeError("GPU distribution has no runtime dependency manifest")
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    if (type(payload.get("schemaVersion")) is not int or payload["schemaVersion"] != 1 or payload.get("variant") != identity["variant"]
            or payload.get("backend") != identity["backend"] or not payload.get("files")):
        raise RuntimeError("GPU dependency manifest differs from the installed variant")
    if payload.get("verification", {}).get("dependencyClosure") != "verified":
        raise RuntimeError("Packaged GPU dependency closure was not verified during deployment")
    # The manifest is next to the executable. Its paths are relative to the
    # installation prefix, which may be nested inside a portable archive.
    prefix = binary.parent.resolve()
    seen = set()
    for record in payload["files"]:
        relative = checked_relative(record["path"])
        path = (prefix / relative).resolve()
        if prefix not in path.parents or path in seen or not path.is_file():
            raise RuntimeError(f"GPU runtime dependency has an invalid path: {record['path']}")
        if (type(record.get("size")) is not int or record["size"] < 1
                or path.stat().st_size != record["size"] or file_digest(path) != record["sha256"]):
            raise RuntimeError(f"GPU runtime dependency bytes differ: {record['path']}")
        if installed_files is not None and path not in installed_files:
            raise RuntimeError(f"GPU dependency is absent from the package manager manifest: {path}")
        seen.add(path)
    for name in payload.get("applicationDependencies", []):
        relative = checked_relative(name)
        if len(relative.parts) != 1:
            raise RuntimeError("GPU application dependency must be a library filename")
        candidates = [root / relative for root in (prefix / "bin", prefix / "lib")]
        resolved = [path.resolve() for path in candidates if path.is_file() and prefix in path.resolve().parents]
        if not resolved or (installed_files is not None and not any(path in installed_files for path in resolved)):
            raise RuntimeError(f"GPU runtime is missing its application-local dependency: {name}")
    if identity["backend"] == "hip" and set(payload.get("gpuTargets", [])) != {"gfx1200", "gfx1201"}:
        raise RuntimeError("ROCm package does not preserve the two required RDNA4 targets")
    resources = payload.get("resources", [])
    components = [item.get("name") for item in resources if isinstance(item, dict)]
    if (not components or len(components) != len(resources)
            or any(not isinstance(name, str) or not name for name in components)
            or len(set(components)) != len(components)):
        raise RuntimeError("GPU runtime manifest does not identify its actual license resources")
    return {"passed": True, "required": True, "manifestSha256": file_digest(manifest), "files": len(seen),
            "runtimeComponents": sorted(components)}


def verify_backend_modes(executable, application, work, report_dir, environment, model_arguments,
                         identity, reference, installed_files, timeout):
    outcomes = {}
    for mode in ("cpu", "gpu"):
        current_environment = dict(environment, QT_QPA_PLATFORM="offscreen", QCOPILOTS_SEARCH_BACKEND=mode)
        with (report_dir / ("backend-" + mode + ".log")).open("w", encoding="utf-8") as log:
            child = subprocess.Popen([str(executable), "--mode", "full", "--profile-dir", str(work / ("backend-" + mode)),
                                      *model_arguments], cwd=work, env=current_environment, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=log, text=True, encoding="utf-8")
            replies = queue.Queue()
            reader = threading.Thread(target=read_worker_replies, args=(child, replies), daemon=True)
            reader.start()
            try:
                reply, raw = worker_request(child, replies, {"id": "backend-" + mode, "operation": "query",
                    "query": "payment declined error"}, report_dir, timeout, accept_failure=True)
                if reply.get("ok") is True:
                    result = reply.get("result", {})
                    diagnostics = validate_backend_diagnostics(result.get("runtime"), identity, mode)
                    numerical = validate_vector(result.get("embedding"), reference)
                    origins = inference_runtime_origins(child, executable, application, installed_files, diagnostics)
                    outcome = {"passed": True, "diagnostics": diagnostics, "rawReply": raw,
                               "numericalCosine": numerical["cosine"], "runtime": origins}
                    if mode == "gpu":
                        outcome["hardwareTested"] = True
                elif mode == "gpu":
                    error = reply.get("error", {})
                    diagnostics = validate_backend_diagnostics(error.get("runtime"), identity, mode, loaded=False)
                    reason = error.get("message", "")
                    if not reason or "strict gpu" not in reason.lower() or reply.get("result", {}).get("embedding"):
                        raise RuntimeError(f"Strict GPU request failed without an explicit backend rejection: {reply}")
                    outcome = {"passed": True, "hardwareTested": False, "diagnostics": diagnostics,
                               "reason": reason, "rawReply": raw}
                else:
                    raise RuntimeError(f"Explicit CPU fallback failed: {reply}")
                outcomes["strictGpu" if mode == "gpu" else "cpu"] = outcome
            finally:
                try:
                    stop_worker(child, replies, reader, report_dir)
                finally:
                    cleanup_error = force_stop(child)
                    if child.stdin and not child.stdin.closed:
                        child.stdin.close()
                    reader.join(timeout=5)
                    if child.stdout:
                        child.stdout.close()
                    if cleanup_error or reader.is_alive():
                        raise RuntimeError("Backend verification child could not be cleaned up: " + str(cleanup_error))
    return outcomes


def checked_relative(name):
    value = PurePosixPath(name.replace("\\", "/"))
    if value.is_absolute() or ".." in value.parts or any(":" in part for part in value.parts):
        raise RuntimeError(f"Archive path escapes extraction root: {name}")
    return Path(*value.parts)


def unpack(package, destination):
    destination.mkdir(parents=True)
    if package.suffix == ".zip":
        with zipfile.ZipFile(package) as archive:
            for entry in archive.infolist():
                checked_relative(entry.filename)
                if (entry.external_attr >> 16) & 0o170000 == 0o120000:
                    raise RuntimeError("ZIP packages must not contain symbolic links")
            archive.extractall(destination)
    elif package.name.endswith((".tar.gz", ".tgz")):
        with tarfile.open(package) as archive:
            for entry in archive.getmembers():
                relative = checked_relative(entry.name)
                if entry.isdev() or entry.isfifo():
                    raise RuntimeError("Device nodes and FIFOs are not distribution files")
                if entry.issym() or entry.islnk():
                    if Path(entry.linkname).is_absolute():
                        raise RuntimeError("Absolute archive link rejected")
                    resolved = (destination / relative.parent / entry.linkname).resolve()
                    if destination.resolve() not in resolved.parents:
                        raise RuntimeError("Archive link escapes extraction root")
            archive.extractall(destination, **({"filter": "data"} if sys.version_info >= (3, 12) else {}))
    elif package.suffix == ".dmg" and sys.platform == "darwin":
        with tempfile.TemporaryDirectory(prefix=PRODUCT + "-mount-", dir=destination.parent) as mount:
            subprocess.run(["hdiutil", "attach", "-readonly", "-nobrowse", "-mountpoint", mount, str(package)], check=True)
            try:
                for item in Path(mount).iterdir():
                    if item.is_dir() and not item.is_symlink():
                        shutil.copytree(item, destination / item.name, symlinks=True)
            finally:
                subprocess.run(["hdiutil", "detach", mount], check=True)
    else:
        raise RuntimeError("Verification expects a ZIP, TGZ, native macOS DMG or Windows NSIS distribution")


def validate_setup_payload(directory):
    if {entry.name for entry in directory.iterdir()} != {"setup.exe", "payload-manifest.json", "payload"}:
        raise RuntimeError("External-payload installer must contain only setup.exe, its manifest and payload")
    manifest = json.loads((directory / "payload-manifest.json").read_text(encoding="utf-8"))
    if (type(manifest.get("schemaVersion")) is not int or manifest["schemaVersion"] != 1
            or manifest.get("variant") != "windows-x64-rocm"
            or type(manifest.get("totalBytes")) is not int or manifest["totalBytes"] < 1
            or not isinstance(manifest.get("files"), list) or not manifest["files"]):
        raise RuntimeError("External-payload installer has an invalid ROCm manifest")
    payload = directory / "payload"
    seen, total = set(), 0
    for record in manifest["files"]:
        relative = checked_relative(record["path"])
        name = relative.as_posix()
        path = (payload / relative).resolve()
        if (name != record["path"] or name.casefold() in seen or not path.is_file()
                or payload.resolve() not in path.parents or type(record.get("bytes")) is not int
                or record["bytes"] < 0 or path.stat().st_size != record["bytes"]
                or file_digest(path) != record.get("sha256")):
            raise RuntimeError(f"External installer payload is missing, changed or ambiguous: {record['path']}")
        seen.add(name.casefold())
        total += record["bytes"]
    actual = {path.relative_to(payload).as_posix().casefold() for path in payload.rglob("*") if path.is_file()}
    if actual != seen or total != manifest["totalBytes"] or "bin/qcs-gui.exe" not in seen:
        raise RuntimeError("External installer payload inventory differs from its manifest")
    return manifest


def create_fixtures(directory):
    directory.mkdir()
    (directory / "中文 package notes.txt").write_text("中文验收 qcopilots_package_4317 portable document search.\n" * 30, encoding="utf-8")
    # Minimal standards-conforming PDF with real text and a correct xref table.
    content = b"BT /F1 12 Tf 50 740 Td " + b"(Package PDF 4317 portable searchable document.) Tj 0 -20 Td " * 10 + b"ET"
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
               b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
               b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>", b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream"]
    pdf = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, obj in enumerate(objects, 1):
        offsets.append(len(pdf)); pdf.extend(f"{number} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref = len(pdf)
    pdf.extend(f"xref\n0 {len(objects)+1}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        pdf.extend(f"{offset:010d} 00000 n \n".encode())
    pdf.extend(f"trailer << /Size 6 /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    (directory / "package.pdf").write_bytes(pdf)
    # Fixed bitmap glyphs avoid platform font dependencies. OCR is checked below.
    font = {"P": [30,17,17,30,16,16,16], "A": [14,17,17,31,17,17,17], "C": [14,17,16,16,16,17,14],
            "K": [17,18,20,24,20,18,17], "G": [14,17,16,23,17,17,15], "E": [31,16,16,30,16,16,31],
            "4": [2,6,10,18,31,2,2], "3": [30,1,1,14,1,1,30], "1": [4,12,4,4,4,4,14], "7": [31,1,2,4,8,8,8], " ": [0]*7}
    width, height, scale = 640, 160, 7
    pixels = bytearray([255]) * (width * height * 3)
    for index, character in enumerate("PACKAGE 4317"):
        for row, bits in enumerate(font[character]):
            for column in range(5):
                if bits & (1 << (4-column)):
                    for dy in range(scale):
                        for dx in range(scale):
                            position = ((height - 1 - (45 + row*scale + dy))*width + 40 + index*6*scale + column*scale + dx)*3
                            pixels[position:position+3] = b"\0\0\0"
    bmp = b"BM" + struct.pack("<IHHI", 54+len(pixels),0,0,54) + struct.pack("<IiiHHIIiiII",40,width,height,1,24,0,len(pixels),2835,2835,0,0) + pixels
    (directory / "Screenshot package.bmp").write_bytes(bmp)
    with wave.open(str(directory / "package.wav"), "wb") as audio:
        audio.setparams((1,2,16000,0,"NONE","not compressed"))
        audio.writeframes(b"".join(struct.pack("<h",int(12000*math.sin(2*math.pi*440*i/16000))) for i in range(48000)))
    def chunk(kind, body):
        return kind + struct.pack("<I",len(body)) + body + (b"\0" if len(body)%2 else b"")
    size, count = 16*16*3, 60
    avih = struct.pack("<14I",100000,size*10,0,0,count,0,1,size,16,16,0,0,0,0)
    strh = b"vidsDIB " + struct.pack("<IHH8I4h",0,0,0,0,1,10,0,count,size,0xffffffff,0,0,0,16,16)
    strf = struct.pack("<IiiHHIIiiII",40,16,16,1,24,0,size,0,0,0,0)
    headers = chunk(b"LIST", b"hdrl"+chunk(b"avih",avih)+chunk(b"LIST",b"strl"+chunk(b"strh",strh)+chunk(b"strf",strf)))
    frames = b"".join(chunk(b"00db",bytes([255 if (x//3%16 < 8 if i<30 else x//48<8) else 0 for x in range(size)])) for i in range(count))
    (directory / "package.avi").write_bytes(chunk(b"RIFF",b"AVI "+headers+chunk(b"LIST",b"movi"+frames)))


def package_source_identity(source=None, environment=None):
    """Report this checkout, never a PR workflow's synthetic merge commit.

    Standalone/archive verification remains usable without Git. Its provenance
    is explicitly unavailable and cannot satisfy the native release gate.
    A declared source or candidate commit always requires real Git validation.
    """
    source = (Path(source) if source is not None else SOURCE).resolve()
    environment = os.environ if environment is None else environment
    declarations = {name: environment[name] for name in
                    ("QCOPILOTS_SEARCH_SOURCE_COMMIT", "QCOPILOTS_SEARCH_CANDIDATE_HEAD", "QCOPILOTS_SEARCH_CANDIDATE_COMMIT")
                    if name in environment}
    for name, value in declarations.items():
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}", value):
            raise RuntimeError(f"{name} must be a complete Git commit SHA")
    git = shutil.which("git")
    # Do not infer provenance from an unrelated repository enclosing a copied
    # source archive. Both normal checkouts and linked worktrees have .git.
    if git is None or not (source / ".git").exists():
        if declarations:
            raise RuntimeError("Cannot verify declared source commit without this checkout's Git metadata and Git executable")
        return {"commit": None, "workingTreeModified": None,
                "sourceIdentityStatus": "unavailable"}

    def checked(*arguments):
        completed = subprocess.run([git, *arguments], cwd=source, capture_output=True,
                                   text=True, encoding="utf-8", errors="replace", timeout=30)
        if completed.returncode:
            raise RuntimeError("Cannot inspect package verifier checkout: " + completed.stderr.strip())
        return completed.stdout.strip()

    if Path(checked("rev-parse", "--show-toplevel")).resolve() != source:
        raise RuntimeError("Git checkout root differs from the package verifier source directory")
    commit = checked("rev-parse", "--verify", "HEAD")
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise RuntimeError("Package verifier checkout has no complete HEAD commit")
    for name, declared in declarations.items():
        if declared != commit:
            raise RuntimeError(f"{name} differs from the actual package verifier checkout HEAD")
    return {"commit": commit, "workingTreeModified": bool(checked("status", "--porcelain")),
            "sourceIdentityStatus": "git-checkout"}


def verify(package, build_root, explicit_report=None, installed_root=None, explicit_profile=None, installed_manifest=None,
           model_dir=None, keyword_only=False, model_timeout=600, expected_variant=None):
    source_identity = package_source_identity()
    run_id = str(uuid.uuid4())
    if explicit_report:
        report_dir = explicit_report
        report_dir.mkdir(parents=True)
    else:
        report_root = build_root / "reports"
        report_root.mkdir(parents=True, exist_ok=True)
        report_dir = Path(tempfile.mkdtemp(prefix="qcs-pkg-", dir=report_root))
    run_root = Path(os.environ.get("QCOPILOTS_SEARCH_TEST_ROOT") or build_root / "runs").expanduser().resolve()
    if run_root == SOURCE or SOURCE in run_root.parents or run_root == Path(run_root.anchor):
        raise RuntimeError("Package verification test root must be a dedicated directory outside the source checkout")
    run_root.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="qcs-pkg-", dir=run_root))
    report = {"package":str(package) if package else None, "installedRoot":str(installed_root) if installed_root else None,
              "runId":run_id, "workingDirectory":str(work), "checks":[], "passed":False,
              "verificationScope": "keyword-only" if keyword_only else "release",
              "realModelRequired": not keyword_only, "releaseComplete": False}
    report.update(source_identity)
    if package:
        package_hash = hashlib.sha256()
        with package.open("rb") as package_stream:
            for block in iter(lambda: package_stream.read(1024 * 1024), b""):
                package_hash.update(block)
        report["packageSha256"] = package_hash.hexdigest()
    gui = worker = service = None
    replies = reader = None
    installer = None
    service_started = False
    cli = None
    program_inventory = None
    logs = []
    environment = isolated_environment()
    profile = explicit_profile or work / "profile"
    report["profileDirectory"] = str(profile)
    if explicit_profile and profile.exists() and (not profile.is_dir() or any(profile.iterdir())):
        raise RuntimeError("Explicit package-test profile must be a new or empty dedicated directory")
    application = installed_root or work/"application"
    report["applicationRoot"] = str(application)
    installed_files = {Path(line).resolve() for line in installed_manifest.read_text(encoding="utf-8").splitlines()
                       if line.startswith("/")} if installed_manifest else None
    def invoke(*arguments):
        completed = subprocess.run([str(cli),"--profile-dir",str(profile), *model_arguments,
                                    "--no-start",*map(str,arguments)],cwd=work,env=environment,capture_output=True,text=True,encoding="utf-8",timeout=180)
        (report_dir / (str(len(report["checks"]))+"-cli.log")).write_text(completed.stdout+completed.stderr,encoding="utf-8")
        if completed.returncode:
            raise RuntimeError(f"CLI failed: {arguments}: {completed.stderr} {completed.stdout}")
        result=json.loads(completed.stdout)
        if not result.get("ok"):
            raise RuntimeError(f"CLI error: {result}")
        return result["result"]
    try:
        if not keyword_only and (model_dir is None or not model_dir.is_dir()):
            raise RuntimeError("Release package verification requires --model-dir with both locked GGUF files. Use --keyword-only only for an explicitly partial developer check.")
        validate_installed_package_source(package, installed_root, installed_manifest)
        model_arguments = ["--model-dir", str(model_dir)] if model_dir and not keyword_only else []
        if installed_root:
            report["checks"].append("validating the actual installed package tree")
        elif package.suffix.lower() == ".exe":
            installer = NsisInstallation(package, work/"application")
            installer.install()
            report["checks"].append("NSIS installed for this user into an isolated installation directory")
        else:
            unpack(package,work/"application")
            if package.name.endswith("-setup.zip"):
                setup = work / "application/setup.exe"
                validate_setup_payload(work / "application")
                installer = NsisInstallation(setup, work / "installed-application")
                installer.install()
                application = work / "installed-application"
                report["applicationRoot"] = str(application)
                report["checks"].append("external-payload NSIS installed from its complete offline setup archive")
        suffix=".exe" if os.name=="nt" else ""
        candidates = [candidate for candidate in (
            application/"bin"/(PRODUCT+"-cli"+suffix),
            application/f"{GUI_EXECUTABLE}.app/Contents/MacOS"/(PRODUCT+"-cli"+suffix),
            application/"Contents/MacOS"/(PRODUCT+"-cli"+suffix)) if candidate.is_file()]
        if not candidates and not installed_root:
            candidates=list(application.rglob(PRODUCT+"-cli"+suffix))
        if len(candidates)!=1:
            raise RuntimeError(f"Expected exactly one installed CLI, found {len(candidates)}")
        cli=candidates[0]
        binary=cli.parent
        report["buildIdentity"] = validate_build_identity(binary, application, source_identity, expected_variant)
        expected_attempt = os.environ.get("QCOPILOTS_SEARCH_CANDIDATE_ATTEMPT") or os.environ.get("QCOPILOTS_SEARCH_ATTEMPT")
        if expected_attempt and (not re.fullmatch(r"[1-9][0-9]*", expected_attempt)
                                 or report["buildIdentity"]["attempt"] != int(expected_attempt)):
            raise RuntimeError("Packaged build attempt differs from the requested candidate attempt")
        report["variant"] = report["buildIdentity"]["variant"]
        program_inventory = installed_program_inventory(binary, application, report["buildIdentity"],
                                                         report.get("packageSha256"), installed_files)
        inventory_path = report_dir / "installed-programs.json"
        inventory_path.write_text(json.dumps(program_inventory, indent=2, ensure_ascii=False), encoding="utf-8")
        report["programInventory"] = {"path": inventory_path.name, "sha256": file_digest(inventory_path), "stable": False}
        report["gpuPayload"] = verify_gpu_payload(binary, application, report["buildIdentity"], installed_files)
        report["licenseMaterials"] = verify_license_materials(binary, application, report_dir, installed_files)
        if (report["gpuPayload"].get("required") and
                report["gpuPayload"]["runtimeComponents"] != report["licenseMaterials"].get("runtimeComponents")):
            raise RuntimeError("Packaged GPU runtime and collected license component inventories differ")
        report["checks"].append("complete packaged runtime license materials and SBOM coverage verified")
        if os.name == "nt":
            for runtime in ("msvcp140.dll", "vcruntime140.dll", "vcomp140.dll"):
                if not (binary/runtime).is_file():
                    raise RuntimeError(f"Portable Windows distribution lacks app-local compiler runtime: {runtime}")
        data_root = application/"share/qcs" if installed_root else application
        for language in ("eng", "chi_sim", "chi_tra"):
            if not list(data_root.rglob(language+".traineddata")):
                raise RuntimeError(f"OCR language is missing from distribution: {language}")
        if not list(data_root.rglob("jieba.dict.utf8")):
            raise RuntimeError("Chinese tokenizer dictionary is missing from distribution")
        for name in APPLICATION_PROGRAMS:
            completed=subprocess.run([str(binary/(name+suffix)),"--version"],cwd=work,env=environment,capture_output=True,text=True,timeout=30)
            if completed.returncode:
                raise RuntimeError(f"Packaged program failed to load: {name}: {completed.stderr}")
        report["checks"].append("all four relocated executables load without SDK environment")
        verified_models = model_verification(invoke("model", "verify"), model_dir) if not keyword_only else None
        service_log = (report_dir/"service.log").open("w",encoding="utf-8"); logs.append(service_log)
        service = subprocess.Popen([str(binary/(PRODUCT+"-service"+suffix)), "--profile-dir", str(profile), *model_arguments],
                                   cwd=work, env=environment, stdout=service_log, stderr=service_log)
        readiness_deadline = time.monotonic() + 30
        while True:
            if service.poll() is not None:
                raise RuntimeError(f"Packaged service exited before IPC readiness: {service.returncode}")
            try:
                invoke("status", "--timeout", "1000")
                service_started = True
                break
            except RuntimeError:
                if time.monotonic() >= readiness_deadline:
                    raise
                time.sleep(0.1)
        report["checks"].append("isolated service IPC status")
        fixtures=work/"samples"
        create_fixtures(fixtures)
        error_log=(report_dir/"worker.log").open("w",encoding="utf-8"); logs.append(error_log)
        worker_environment=environment.copy(); worker_environment["QT_QPA_PLATFORM"]="offscreen"
        worker=subprocess.Popen([str(binary/(PRODUCT+"-worker"+suffix)),"--mode","extract" if keyword_only else "full",
                                 "--profile-dir",str(work/"worker-profile"), *model_arguments],cwd=work,env=worker_environment,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=error_log,text=True,encoding="utf-8")
        replies=queue.Queue()
        reader=threading.Thread(target=read_worker_replies,args=(worker,replies),daemon=True)
        reader.start()
        for index, fixture in enumerate(sorted(fixtures.iterdir())):
            request={"id":"extract-"+str(index),"operation":"extract","path":str(fixture),"embed":False,"enableOcr":True}
            result, _ = worker_request(worker, replies, request, report_dir, timeout=90)
            if not result.get("complete") or not result.get("segments"):
                raise RuntimeError(f"Packaged extractor failed for {fixture.name}: {result}")
            if fixture.suffix in (".bmp", ".pdf") and "4317" not in " ".join(segment.get("text","") for segment in result["segments"]):
                raise RuntimeError(f"Packaged text/OCR extraction lost expected content: {fixture.name}: {result}")
            report["checks"].append("packaged extraction "+fixture.name)
        report["workerLibraries"]=verify_library_origins(worker,application,installed_files)
        if not keyword_only:
            report["modelSmoke"] = real_model_smoke(worker, replies, binary/(PRODUCT+"-worker"+suffix),
                application, work, report_dir, verified_models, installed_files, model_timeout, report["buildIdentity"])
            report["checks"].append("installed inference runtime matched immutable text, image and audio reference vectors")
        stop_worker(worker, replies, reader, report_dir)
        worker = None
        if not keyword_only:
            reference = json.loads((work / "model-references/vectors.json").read_text(encoding="utf-8"))["vectors"]["query"]
            report["backendVerification"] = verify_backend_modes(binary / (PRODUCT + "-worker" + suffix),
                application, work, report_dir, environment, model_arguments, report["buildIdentity"], reference,
                installed_files, model_timeout)
            report["checks"].append("explicit CPU inference and strict GPU acceptance or rejection verified")
        invoke("index",fixtures,"--wait","--timeout","120000")
        hits=invoke("search","qcopilots_package_4317","--keyword-only").get("hits",[])
        if not any(hit.get("path","").endswith("中文 package notes.txt") for hit in hits):
            raise RuntimeError("Packaged index/search did not retrieve its text fixture")
        report["checks"].append("packaged service index and exact keyword retrieval")
        if not keyword_only:
            # None of these terms occurs in the fixture text. Require a real
            # stored-vector candidate, without imposing a relevance threshold.
            semantic = invoke("search", "A coastal lighthouse beneath moonlit clouds", "--root", fixtures,
                              "--timeout", "120000")
            semantic_hits = semantic.get("hits", [])
            if (semantic.get("semantic") is not True or semantic.get("warning") or not semantic_hits
                    or any(hit.get("matchSource") != "semantic" or fixtures.resolve() not in Path(hit.get("path", "")).resolve().parents
                           for hit in semantic_hits)):
                raise RuntimeError(f"Packaged service query encoder did not provide semantic retrieval: {semantic}")
            report["modelSmoke"]["serviceSemantic"] = semantic
            report["checks"].append("installed service completed semantic retrieval with its isolated model directory")
        if service.poll() is not None:
            raise RuntimeError("The explicitly started package-test service exited unexpectedly")
        report["serviceLibraries"]=verify_library_origins(service,application,installed_files)
        gui_log=(report_dir/"gui.log").open("w",encoding="utf-8"); logs.append(gui_log)
        gui=subprocess.Popen([str(binary/(GUI_EXECUTABLE+suffix)),"--profile-dir",str(profile),*model_arguments,
                              "--no-onboarding","--no-tray"],cwd=work,env=environment,stdout=gui_log,stderr=gui_log)
        time.sleep(3)
        if gui.poll() is not None:
            raise RuntimeError(f"Packaged GUI exited during startup: {gui.returncode}")
        report["guiLibraries"]=verify_library_origins(gui,application,installed_files)
        report["checks"].append("native GUI startup with relocated profile (interactive flows covered separately)")
        report["passed"]=True
    except Exception as error:
        report["error"]=str(error)
    finally:
        for process in (gui,worker):
            if process is None:
                continue
            try:
                if process is worker:
                    stop_worker(worker, replies, reader, report_dir)
                elif process.poll() is None:
                    process.terminate()
                    process.wait(timeout=10)
            except Exception as error:
                report.setdefault("cleanupErrors", []).append(str(error))
                report["passed"] = False
            finally:
                cleanup_error = force_stop(process)
                if cleanup_error:
                    report.setdefault("cleanupErrors", []).append(cleanup_error)
                    report["passed"] = False
                if process is worker and process.poll() is not None:
                    try:
                        process.stdin.close()
                        if reader is not None:
                            reader.join(timeout=5)
                            if reader.is_alive():
                                raise RuntimeError("Packaged worker output reader did not stop")
                        process.stdout.close()
                    except Exception as error:
                        report.setdefault("cleanupErrors", []).append(str(error))
                        report["passed"] = False
        if cli is not None and service_started:
            try: invoke("shutdown","--timeout","10000")
            except Exception as error:
                report["cleanupError"]=str(error)
                report["passed"]=False
        if service is not None:
            try:
                service.wait(timeout=20)
                if service.returncode != 0:
                    raise RuntimeError(f"Packaged service shutdown failed: {service.returncode}")
            except Exception as error:
                report["cleanupError"]=str(error)
                report["passed"]=False
                cleanup_error = force_stop(service)
                if cleanup_error:
                    report.setdefault("cleanupErrors", []).append(cleanup_error)
        for log in logs: log.close()
        if program_inventory is not None:
            try:
                verify_program_stability(program_inventory, binary, application, report["buildIdentity"],
                                         package, installed_files)
                report["programInventory"]["stable"] = True
            except Exception as error:
                report["cleanupError"] = str(error)
                report["passed"] = False
        if installer is not None:
            try:
                # User data lives outside the install tree and must survive
                # removal of the actual package payload and registrations.
                sentinel = profile / "qcs-uninstall-preservation.txt"
                sentinel.parent.mkdir(parents=True, exist_ok=True)
                sentinel.write_text(run_id, encoding="utf-8")
                installer.uninstall()
                if sentinel.read_text(encoding="utf-8") != run_id:
                    raise RuntimeError("NSIS uninstall modified the isolated user profile")
                report["checks"].append("NSIS uninstall removed payload, registry and shortcuts while retaining user data")
            except Exception as error:
                report["cleanupError"]=str(error)
                report["passed"]=False
            finally:
                installer.close()
        if report["passed"]:
            try:
                shutil.rmtree(work)
            except OSError as error:
                report["cleanupError"] = str(error)
                report["passed"] = False
        report["releaseComplete"] = (report["passed"] and not keyword_only and
            report.get("licenseMaterials", {}).get("passed") is True and report.get("modelSmoke", {}).get("passed") is True
            and report.get("buildIdentity", {}).get("workingTreeModified") is False
            and report.get("programInventory", {}).get("stable") is True
            and report.get("backendVerification", {}).get("cpu", {}).get("passed") is True
            and report.get("backendVerification", {}).get("strictGpu", {}).get("passed") is True)
        write_verification_report(report, report_dir)
    if not report["passed"]:
        raise RuntimeError(report.get("error", report.get("cleanupError", "Package verification failed")))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-root",required=True,type=Path)
    parser.add_argument("--package-file",type=Path,help="Distribution to verify, or original DEB identity with --installed-root")
    parser.add_argument("--report-dir",type=Path)
    parser.add_argument("--installed-root",type=Path,help="Verify an already installed package in place, without deleting it")
    parser.add_argument("--installed-manifest",type=Path,help="Absolute file list from the OS package manager, such as dpkg -L")
    parser.add_argument("--profile-dir",type=Path,help="Fresh dedicated test profile retained after verification")
    parser.add_argument("--model-dir",type=Path,help="Read-only cache of both locked GGUF artifacts, required for release verification")
    parser.add_argument("--keyword-only",action="store_true",help="Explicit partial developer verification without real model inference, insufficient for release")
    parser.add_argument("--model-timeout",type=int,default=600,help="Seconds allowed for each real-model worker request (default 600)")
    parser.add_argument("--variant", help="Require the downloaded package to match this build variant")
    args=parser.parse_args()
    root=args.build_root.resolve()
    source=Path(__file__).resolve().parent.parent
    if root==source or source in root.parents or root==Path(root.anchor):
        raise RuntimeError("Package verification requires a dedicated external build root")
    package=args.package_file
    if args.installed_root and not args.installed_manifest:
        raise RuntimeError("Installed-tree verification requires --installed-manifest from the package manager")
    if package is None and not args.installed_root:
        suffix=".zip" if os.name=="nt" else ".dmg" if sys.platform=="darwin" else ".tar.gz"
        packages=[path for path in (root/"pkg").glob(PRODUCT+"-*"+suffix)
                  if "-source-materials-" not in path.name and "-symbols-" not in path.name and not path.name.endswith("-setup.zip")]
        if not packages: raise RuntimeError("No distribution archive exists. Run package first.")
        package=max(packages,key=lambda value:value.stat().st_mtime)
    report=args.report_dir.expanduser().resolve() if args.report_dir else None
    if report is not None and (report==source or source in report.parents or report==Path(report.anchor)):
        raise RuntimeError("Package report directory must be outside the checkout")
    installed = args.installed_root.expanduser().resolve() if args.installed_root else None
    profile = args.profile_dir.expanduser().resolve() if args.profile_dir else None
    if profile and (profile == source or source in profile.parents or profile == Path(profile.anchor)):
        raise RuntimeError("Package-test profile must be an isolated directory outside the checkout")
    manifest = args.installed_manifest.expanduser().resolve() if args.installed_manifest else None
    model_dir = args.model_dir.expanduser().resolve() if args.model_dir else None
    if args.keyword_only and args.model_dir:
        raise RuntimeError("Choose full verification with --model-dir or partial --keyword-only, not both")
    if args.model_timeout < 1 or args.model_timeout > 3600:
        raise RuntimeError("--model-timeout must be between 1 and 3600 seconds")
    verify(package.resolve() if package else None,root,report,installed,profile,manifest,model_dir,args.keyword_only,args.model_timeout,args.variant)


if __name__=="__main__":
    for stream in (sys.stdout,sys.stderr):
        if hasattr(stream,"reconfigure"):
            stream.reconfigure(encoding="utf-8",errors="backslashreplace")
    try: main()
    except Exception as error:
        print(f"{PRODUCT} package verification failed: {error}",file=sys.stderr)
        sys.exit(1)
