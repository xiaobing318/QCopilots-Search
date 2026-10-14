#!/usr/bin/env python3
"""Stage a GPU dependency closure and device data without copying an SDK.

ROCm vendor ELF files retain their original bytes and RPATH. Its two wheel
package directories remain adjacent. Missing native dependencies, missing
licenses and missing RDNA4 resources are packaging errors, never warnings.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import mmap
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys

sys.dont_write_bytecode = True
SPEC = importlib.util.spec_from_file_location("qcs_gpu_sdk", Path(__file__).with_name("qcs-gpu-sdk.py"))
sdk = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sdk)

# OS/driver libraries remain supplied by the OS or installed display driver.
WINDOWS_SYSTEM = {
    "kernel32.dll", "kernelbase.dll", "ntdll.dll", "user32.dll", "advapi32.dll", "shell32.dll",
    "ole32.dll", "oleaut32.dll", "combase.dll", "rpcrt4.dll", "ws2_32.dll", "bcrypt.dll",
    "bcryptprimitives.dll", "crypt32.dll", "secur32.dll", "version.dll", "winmm.dll",
    "shlwapi.dll", "gdi32.dll", "gdi32full.dll", "setupapi.dll", "cfgmgr32.dll", "psapi.dll",
    "dbghelp.dll", "dbgcore.dll", "imagehlp.dll", "iphlpapi.dll", "userenv.dll", "wtsapi32.dll",
    "powrprof.dll", "dxgi.dll", "d3d12.dll", "dxcore.dll", "imm32.dll", "wintrust.dll",
    "msvcrt.dll", "ucrtbase.dll", "normaliz.dll", "winhttp.dll", "nvcuda.dll", "nvml.dll",
}
LINUX_SYSTEM = re.compile(r"^(?:ld-linux-x86-64\.so\.2|lib(?:c|m|dl|pthread|rt|resolv|util|gcc_s|stdc\+\+|"
                          r"drm|drm_amdgpu|numa|z|tinfo|ncurses|pciaccess|elf|ffi|expat|X11|xcb|wayland-client)"
                          r"\.so(?:\.\d+)*|libcuda\.so(?:\.\d+)*|libnvidia-[\w-]+\.so(?:\.\d+)*)$")
WINDOWS_CRT = re.compile(r"^(?:msvcp140(?:_[a-z0-9_]+)?|vcruntime140(?:_1)?|concrt140)\.dll$", re.IGNORECASE)


def pe_dependencies(path: Path) -> list[str]:
    """Read PE import and delay-import names without dumpbin or executing code."""
    with path.open("rb") as stream, mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as data:
        if data[:2] != b"MZ" or len(data) < 64:
            raise RuntimeError(f"Not a PE image: {path}")
        pe = struct.unpack_from("<I", data, 60)[0]
        if data[pe:pe + 4] != b"PE\0\0":
            raise RuntimeError(f"Invalid PE signature: {path}")
        sections_count = struct.unpack_from("<H", data, pe + 6)[0]
        optional_size = struct.unpack_from("<H", data, pe + 20)[0]
        optional = pe + 24
        magic = struct.unpack_from("<H", data, optional)[0]
        if magic not in (0x10b, 0x20b):
            raise RuntimeError(f"Unsupported PE optional header: {path}")
        directory = optional + (112 if magic == 0x20b else 96)
        count = struct.unpack_from("<I", data, directory - 4)[0]
        sections = []
        for i in range(sections_count):
            start = optional + optional_size + 40 * i
            virtual_size, virtual, size, raw = struct.unpack_from("<IIII", data, start + 8)
            sections.append((virtual, max(virtual_size, size), raw))

        def offset(rva: int) -> int:
            for virtual, size, raw in sections:
                if virtual <= rva < virtual + size:
                    result = raw + rva - virtual
                    if result >= len(data):
                        break
                    return result
            raise RuntimeError(f"Invalid PE RVA {rva:x}: {path}")

        def name_at(rva: int) -> str:
            start = offset(rva)
            end = data.find(b"\0", start, min(start + 4096, len(data)))
            if end < 0:
                raise RuntimeError(f"Unterminated PE import name: {path}")
            result = data[start:end].decode("ascii")
            if Path(result).name != result or "/" in result or "\\" in result:
                raise RuntimeError(f"Unexpected PE dependency path: {result}")
            return result

        imports = []
        for index, record_size, name_offset in ((1, 20, 12), (13, 32, 4)):
            if count <= index:
                continue
            rva, size = struct.unpack_from("<II", data, directory + index * 8)
            if not rva or not size:
                continue
            start = offset(rva)
            for at in range(start, min(start + size, len(data)), record_size):
                record = data[at:at + record_size]
                if not any(record):
                    break
                if len(record) != record_size:
                    raise RuntimeError(f"Truncated PE import descriptor: {path}")
                name_rva = struct.unpack_from("<I", record, name_offset)[0]
                if index == 13 and not (struct.unpack_from("<I", record, 0)[0] & 1):
                    raise RuntimeError(f"Unsupported VA-based delay import: {path}")
                imports.append(name_at(name_rva))
        return sorted(set(imports), key=str.lower)


def elf_details(path: Path) -> tuple[list[str], list[str]]:
    reader = shutil.which("readelf") or shutil.which("llvm-readelf")
    if not reader:
        raise RuntimeError("readelf or llvm-readelf is required to verify the Linux GPU dependency closure")
    env = dict(os.environ, LC_ALL="C")
    result = subprocess.run([reader, "--dynamic", str(path)], text=True, capture_output=True, check=True, env=env)
    needed = re.findall(r"\(NEEDED\).*?\[([^\]]+)\]", result.stdout)
    rpaths = re.findall(r"\((?:RUNPATH|RPATH)\).*?\[([^\]]*)\]", result.stdout)
    return needed, [part for line in rpaths for part in line.split(":") if part]


def dependencies(path: Path, windows: bool) -> list[str]:
    return pe_dependencies(path) if windows else elf_details(path)[0]


def system_dependency(name: str, windows: bool) -> bool:
    return (name.lower() in WINDOWS_SYSTEM or name.lower().startswith(("api-ms-win-", "ext-ms-win-"))) if windows else bool(LINUX_SYSTEM.fullmatch(name))


def native_library(path: Path, windows: bool) -> bool:
    # ROCm 10 includes LLVM SONAMEs such as libLLVM.so.23.0git.
    return path.name.lower().endswith(".dll") if windows else bool(re.search(r"\.so(?:\.[A-Za-z0-9_+-]+)*$", path.name))


def native_index(roots: list[Path], windows: bool) -> dict[str, list[Path]]:
    result = {}
    for root in roots:
        if not root.is_dir():
            raise RuntimeError(f"GPU runtime root missing: {root}")
        for path in root.rglob("*"):
            if path.is_file() and native_library(path, windows):
                key = path.name.lower() if windows else path.name
                result.setdefault(key, []).append(path)
    return result


def choose_library(name: str, index: dict[str, list[Path]], windows: bool) -> Path | None:
    entries = index.get(name.lower() if windows else name, [])
    if not entries:
        return None
    entries = sorted(entries, key=lambda p: (len(p.parts), str(p)))
    if len(entries) > 1 and len({sdk.digest(p) for p in entries}) != 1:
        raise RuntimeError(f"Ambiguous SDK library {name}: " + ", ".join(str(p) for p in entries))
    return entries[0]


def closure(seeds: list[Path], index: dict, windows: bool, application_dirs: list[Path]) -> tuple[list[Path], list[str], list[str]]:
    ordered, visiting, done, system, application = [], set(), set(), set(), set()

    def visit(path: Path) -> None:
        # Preserve distinct SONAME aliases even when both symlinks resolve to
        # the same bytes. The loader asks for filenames, not inode identities.
        key = str(path.absolute())
        if windows:
            key = key.lower()
        if key in done or key in visiting:
            return
        visiting.add(key)
        for name in dependencies(path, windows):
            if system_dependency(name, windows):
                system.add(name)
                continue
            # Qt's deployment hook runs after this GPU hook and installs the
            # Microsoft runtime. Final package verification must find these.
            if windows and WINDOWS_CRT.fullmatch(name):
                application.add(name)
                continue
            candidate = choose_library(name, index, windows)
            if candidate:
                visit(candidate)
            elif any((directory / name).is_file() for directory in application_dirs):
                application.add(name)
            else:
                raise RuntimeError(f"Unresolved GPU dependency {name} required by {path}")
        visiting.remove(key)
        done.add(key)
        ordered.append(path)

    for seed in seeds:
        visit(seed)
    return ordered, sorted(system), sorted(application)


def require_pattern(index: dict, expression: str, description: str) -> list[Path]:
    names = sorted(name for name in index if re.fullmatch(expression, name, re.IGNORECASE))
    if not names:
        raise RuntimeError(f"Missing required GPU runtime component: {description}")
    return [choose_library(name, index, name.lower().endswith(".dll")) for name in names]


def license_resources(roots: list[Path], sdk_manifest: dict, destination: Path) -> list[dict]:
    """Keep only original bounded text notices, with deterministic unique paths."""
    resources = []
    for index, root in enumerate(roots):
        candidates = []
        if root.is_dir():
            candidates = [p for p in root.rglob("*") if p.is_file() and sdk.is_license(p.name)]
        # Wheel notices may be in dist-info instead of the platform payload.
        if root.name.startswith("_rocm_sdk_"):
            module = root.name.lstrip("_")
            for info in root.parent.glob(module + "-*.dist-info"):
                candidates += [p for p in info.rglob("*") if p.is_file() and sdk.is_license(p.name)]
        if not candidates:
            continue
        name = "gpu-" + re.sub(r"[^a-z0-9._+-]", "-", root.name.lower()).strip("-_.")
        if any(r["name"] == name for r in resources):
            name += "-" + str(index)
        files = []
        for original in sorted(set(candidates)):
            if original.stat().st_size > 4 * 1024 * 1024:
                raise RuntimeError(f"Unexpectedly large GPU license document: {original}")
            # Check it is text before declaring it a redistributable notice.
            content = original.read_bytes()
            if b"\0" in content:
                raise RuntimeError(f"GPU license candidate is not a text file: {original}")
            rel = original.relative_to(root if root in original.parents else root.parent)
            target = destination / "bin" / "qcs-gpu-licenses" / name / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(original, target)
            files.append({"path": rel.as_posix(), "source": target.relative_to(destination / "bin").as_posix(), "sha256": sdk.digest(target)})
        resources.append({"name": name, "version": sdk_manifest["toolkitVersion"], "files": files})
    for resource in sdk_manifest.get("additionalLicenseResources", []):
        name = resource.get("name", "")
        if not re.fullmatch(r"[a-z0-9][a-z0-9._+-]*", name) or any(r["name"] == name for r in resources):
            raise RuntimeError(f"Invalid or duplicate additional GPU license component: {name}")
        if not resource.get("version") or not resource.get("files"):
            raise RuntimeError(f"Additional GPU license component is incomplete: {name}")
        files = []
        seen = set()
        for entry in resource["files"]:
            relative = sdk.safe_relative(entry["path"])
            if relative.as_posix() in seen:
                raise RuntimeError(f"Duplicate additional GPU notice: {name}/{relative}")
            seen.add(relative.as_posix())
            original = Path(entry["source"])
            expected = entry["sha256"]
            if not original.is_absolute() or not original.is_file() or original.is_symlink():
                raise RuntimeError(f"Missing or linked additional GPU license: {original}")
            if not 0 < original.stat().st_size <= 4 * 1024 * 1024 or sdk.digest(original) != expected:
                raise RuntimeError(f"Additional GPU license content does not match its lock: {original}")
            if b"\0" in original.read_bytes():
                raise RuntimeError(f"Additional GPU notice is not text: {original}")
            target = destination / "bin" / "qcs-gpu-licenses" / name / relative
            if destination not in target.resolve().parents:
                raise RuntimeError(f"Additional GPU notice destination escapes installation: {target}")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(original, target)
            files.append({"path": relative.as_posix(), "source": target.relative_to(destination / "bin").as_posix(), "sha256": expected})
        resources.append({"name": name, "version": resource["version"], "files": files})
    if not resources:
        raise RuntimeError("No original GPU runtime license documents found. Refusing an unattributed package.")
    return resources


def stage_runtime(variant: dict, manifest: dict, destination: Path, backend_library: Path | None = None) -> dict:
    if manifest.get("schemaVersion") != 1 or manifest.get("variant") != variant["id"] or manifest.get("backend") != variant["backend"]:
        raise RuntimeError("SDK manifest does not match the selected build variant")
    if manifest.get("gpuTargets") != variant["gpuTargets"] or manifest.get("toolkitVersion") != variant["toolkitVersion"]:
        raise RuntimeError("SDK manifest GPU targets/toolkit version differ from the selected variant")
    destination = sdk.external(destination)
    destination.mkdir(parents=True, exist_ok=True)
    backend, windows = variant["backend"], variant["os"] == "windows"
    if backend == "hip" and not manifest.get("additionalLicenseResources"):
        raise RuntimeError("ROCm SDK manifest lacks the reviewed runtime redistribution notices. Run bootstrap again.")
    report = {"schemaVersion": 1, "variant": variant["id"], "backend": backend,
              "toolkitVersion": variant["toolkitVersion"], "gpuTargets": variant["gpuTargets"],
              "files": [], "libraryDirectories": [], "preloadLibraries": [], "systemDependencies": [],
              "applicationDependencies": [], "resources": [], "downloads": manifest.get("downloads", []),
              "verification": {"gpuExecution": False, "dependencyClosure": "not-applicable"}}
    if backend in ("cpu", "metal"):
        return report
    roots = [Path(p) for p in manifest["runtimeRoots"]]
    index = native_index(roots, windows)
    seeds = []
    if backend == "cuda":
        major = variant["toolkitVersion"].split(".")[0]
        patterns = [(r"cudart64_\d+\.dll", "CUDA runtime"), (r"cublas64_\d+\.dll", "cuBLAS"), (r"cublaslt64_\d+\.dll", "cuBLAS Lt")] if windows else [(rf"lib{name}\.so\.{major}", name) for name in ("cudart", "cublas", "cublasLt")]
    elif backend == "vulkan":
        patterns = [(r"vulkan-1\.dll" if windows else r"libvulkan\.so\.1", "Vulkan loader")]
    else:
        patterns = [(r"amdhip64_\d+\.dll", "HIP runtime"), (r"amd_comgr\.dll", "AMD compiler support"),
                    (r"rocm_kpack\.dll", "ROCm kernel pack runtime"), (r"hipblas\.dll", "hipBLAS"),
                    (r"rocblas\.dll", "rocBLAS"), (r"libhipblaslt\.dll", "hipBLAS Lt")] if windows else [
                    (r"libamdhip64\.so\.\d+", "HIP runtime"), (r"libamd_comgr\.so\.\d+", "AMD compiler support"),
                    (r"librocm_kpack\.so(?:\.\d+)?", "ROCm kernel pack runtime"), (r"libhipblas\.so\.\d+", "hipBLAS"),
                    (r"librocblas\.so\.\d+", "rocBLAS"), (r"libhipblaslt\.so\.\d+", "hipBLAS Lt")]
    for pattern, description in patterns:
        seeds.extend(require_pattern(index, pattern, description))
    if backend_library:
        if not backend_library.is_file():
            raise RuntimeError(f"GPU backend library is missing: {backend_library}")
        seeds.append(backend_library)
    ordered, system, application = closure(seeds, index, windows, [destination / "bin", destination / "lib"])
    report["systemDependencies"] = system
    report["applicationDependencies"] = application
    directories = set()
    mapped = {}

    def copy(source: Path, relative: Path, role: str) -> None:
        relative = sdk.safe_relative(relative.as_posix())
        target = destination / relative
        if destination not in target.resolve().parents:
            raise RuntimeError(f"GPU stage destination escapes installation root: {target}")
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.resolve() != target.resolve():
            shutil.copy2(source, target, follow_symlinks=True)
        report["files"].append({"path": relative.as_posix(), "sha256": sdk.digest(target),
                                "size": target.stat().st_size, "role": role})
        mapped[source] = target

    for source in ordered:
        if backend_library and source == backend_library:
            # CMake owns backend placement, this script records and verifies it.
            relative = Path("bin" if windows else "lib") / source.name
            copy(source, relative, "backend")
        elif backend == "hip":
            owning = next((r for r in roots if r == source or r in source.parents), None)
            if owning is None:
                raise RuntimeError(f"GPU closure escaped SDK roots: {source}")
            relative = Path("bin" if windows else "lib") / "rocm" / owning.name / source.relative_to(owning)
            copy(source, relative, "runtime-library")
        else:
            relative = Path("bin" if windows else "lib") / source.name
            copy(source, relative, "runtime-library")
        directories.add(os.path.relpath((destination / relative).parent, destination / "bin").replace("\\", "/"))
        if windows and not (backend_library and source == backend_library):
            report["preloadLibraries"].append(os.path.relpath(destination / relative, destination / "bin").replace("\\", "/"))
    if backend == "hip":
        resource_root = Path(manifest["resourceRoot"])
        for target in variant["gpuTargets"]:
            resource = resource_root / "_rocm_sdk_libraries" / ".kpack" / f"blas_lib_{target}.kpack"
            if not resource.is_file():
                raise RuntimeError(f"Missing ROCm device resource: {resource}")
        for resource in sorted(resource_root.rglob("*")):
            if resource.is_file() and sdk.is_rocm_device_resource(resource.relative_to(resource_root).as_posix(), variant["gpuTargets"]):
                copy(resource, Path("bin" if windows else "lib") / "rocm" / resource.relative_to(resource_root), "device-resource")
    # Do not mutate vendor ELF files. Validate their original relative paths in
    # the actual destination, which preserves the adjacent wheel directories.
    if not windows:
        for original, installed in mapped.items():
            if not native_library(installed, False):
                continue
            needed, paths = elf_details(installed)
            for path in paths:
                if path.startswith("/"):
                    raise RuntimeError(f"GPU runtime contains an absolute RPATH: {installed}: {path}")
            for name in needed:
                if system_dependency(name, False):
                    continue
                candidates = [installed.parent / name]
                for path in paths:
                    expanded = path.replace("${ORIGIN}", str(installed.parent)).replace("$ORIGIN", str(installed.parent))
                    if "$" in expanded:
                        raise RuntimeError(f"Unsupported GPU runtime RPATH token: {path}")
                    candidates.append(Path(expanded) / name)
                if not any(p.is_file() for p in candidates):
                    raise RuntimeError(f"Installed GPU runtime cannot resolve {name} via its preserved RPATH: {installed}")
    licenses = roots + [Path(p) for p in manifest.get("licenseRoots", [])]
    report["resources"] = license_resources(licenses, manifest, destination)
    actual_license_hashes = {entry["sha256"] for resource in report["resources"] for entry in resource["files"]}
    for expected in manifest.get("requiredLicenseHashes", []):
        if expected not in actual_license_hashes:
            raise RuntimeError(f"Missing reviewed GPU redistribution terms with SHA256 {expected}")
    report["libraryDirectories"] = sorted(directories)
    report["files"] = sorted(report["files"], key=lambda item: item["path"])
    report["verification"]["dependencyClosure"] = "verified"
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--variant", required=True)
    parser.add_argument("--sdk-manifest", type=Path)
    parser.add_argument("--sdk-root", type=Path, help="SDK root containing gpu-sdk.json, when --sdk-manifest is omitted")
    parser.add_argument("--stage-root", "--destination", required=True, type=Path)
    parser.add_argument("--backend-library", type=Path)
    parser.add_argument("--report", "--output", type=Path)
    args = parser.parse_args()
    source = args.sdk_manifest or (args.sdk_root / "gpu-sdk.json" if args.sdk_root else None)
    if source is None:
        parser.error("--sdk-manifest or --sdk-root is required")
    manifest = json.loads(source.read_text(encoding="utf-8"))
    variant = sdk.variant_by_id(args.variant)
    report = stage_runtime(variant, manifest, args.stage_root, args.backend_library)
    destination = sdk.external(args.stage_root)
    sdk.write_json(destination / "bin" / "qcs-gpu-runtime.json", report)
    if args.report:
        sdk.write_json(args.report, report)
    print(json.dumps({"variant": variant["id"], "files": len(report["files"]), "report": str(destination / "bin" / "qcs-gpu-runtime.json")}))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, RuntimeError, struct.error, subprocess.CalledProcessError) as error:
        print(f"qcs GPU runtime: {error}", file=sys.stderr)
        raise SystemExit(1)
