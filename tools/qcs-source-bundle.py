#!/usr/bin/env python3
"""Collect pinned corresponding sources, recipes and notices without unpacking SDK sources."""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")

SOURCE = Path(__file__).resolve().parent.parent
PRODUCT = "qcs"
FETCH = re.compile(r'qcopilots_search_fetch\(\s*([A-Za-z0-9_]+)\s+"(https://[^"]+)"\s+"([0-9a-f]{64})"\s*\)', re.MULTILINE)
LLAMA = {"name": "llama.cpp", "version": "b11461", "kind": "upstream-source",
         "url": "https://codeload.github.com/ggml-org/llama.cpp/tar.gz/4d756bc72bf00a4aacf410ae15a2d315f3db400d",
         "filename": "llama.cpp-b11461.tar.gz",
         "sha256": "778ee590ec7447075e08f2daf6301311ae7f8d9bf810a0b849f26af3f6d10867"}


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def refresh_package_checksums(output: Path, produced: list[Path]):
    """Retain verified package entries and add this invocation's completed outputs."""
    output = output.resolve()
    destination = output / "SHA256SUMS"
    if destination.is_symlink():
        raise RuntimeError("Refusing a package checksum symlink")
    updated = {}
    for path in produced:
        if path.parent.resolve() != output or path.is_symlink() or not path.is_file():
            raise RuntimeError(f"Invalid completed source output: {path}")
        if not path.name.startswith(PRODUCT + "-") or path.suffix == ".sha256":
            raise RuntimeError(f"Invalid source checksum entry: {path.name}")
        updated[path.name] = digest(path)
    if not updated:
        raise RuntimeError("No completed source outputs were provided")
    retained = {}
    if destination.exists():
        for line in destination.read_text(encoding="utf-8").splitlines():
            match = re.fullmatch(r"([0-9a-f]{64})  (.+)", line)
            if not match:
                raise RuntimeError("Malformed existing package checksum entry")
            expected, name = match.groups()
            if (not name.startswith(PRODUCT + "-") or "/" in name or "\\" in name
                    or ":" in name or name.endswith(".sha256") or name in retained):
                raise RuntimeError(f"Invalid existing package checksum name: {name}")
            path = output / name
            if path.is_symlink() or not path.is_file():
                raise RuntimeError(f"Existing package checksum refers to a missing or linked file: {name}")
            if name not in updated and digest(path) != expected:
                raise RuntimeError(f"Existing package changed since checksum creation: {name}")
            retained[name] = expected
    retained.update(updated)
    descriptor, temporary_name = tempfile.mkstemp(prefix="." + PRODUCT + "-checksums-", dir=output)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            for name in sorted(retained):
                stream.write(f"{retained[name]}  {name}\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def external(path: Path) -> Path:
    path = path.expanduser().resolve()
    if path == SOURCE or SOURCE in path.parents or path == Path(path.anchor):
        raise RuntimeError(f"Output must be an external non-root directory: {path}")
    return path


def guard(path: Path, reserve: int, additional: int = 0):
    while not path.exists():
        path = path.parent
    available = shutil.disk_usage(path).free
    if available < reserve + additional:
        raise RuntimeError(f"Source materials would breach the free-space reserve: available={available}, required={reserve + additional}")


def entries():
    found = {}
    for source in sorted((SOURCE / "cmake").glob("*.cmake")):
        for name, url, sha256 in FETCH.findall(source.read_text(encoding="utf-8")):
            filename = name.removeprefix("qcs_") + "-" + sha256[:12]
            if url.endswith(".zip"):
                filename += ".zip"
            elif url.endswith(".tar.xz"):
                filename += ".tar.xz"
            else:
                filename += ".tar.gz"
            value = {"name": name, "kind": "upstream-source", "url": url,
                     "filename": filename, "sha256": sha256,
                     "recipe": source.relative_to(SOURCE).as_posix()}
            if name in found and found[name] != value:
                raise RuntimeError(f"Conflicting source locks for {name}")
            found[name] = value
    for required in ("sqlite", "hnswlib", "cppjieba", "limonp", "miniz", "libgit2", "leptonica", "tesseract", "libde265", "libheif", "libraw", "ffmpeg", "calligra"):
        if "qcs_" + required not in found:
            raise RuntimeError(f"Cannot establish pinned source contract for {required}")
    qt_lock = SOURCE / "tools/qcs-source-packages.lock.json"
    package_lock = json.loads(qt_lock.read_text(encoding="utf-8"))
    for package in package_lock["packages"]:
        name = package["name"]
        if name in found:
            raise RuntimeError(f"Duplicate source package {name}")
        if not re.fullmatch(r"[0-9a-f]{64}", package["sha256"]):
            raise RuntimeError(f"Invalid source hash for {name}")
        if Path(package["filename"]).name != package["filename"]:
            raise RuntimeError(f"Invalid source filename for {name}")
        found[name] = {**package, "kind": "upstream-source"}
    locked_modules = {package.get("module") for package in package_lock["packages"]}
    for module in ("qtbase", "qtsvg", "qt5compat", "qtimageformats", "qtmultimedia", "qttranslations", "qtwebengine"):
        if module not in locked_modules:
            raise RuntimeError(f"Corresponding Qt source module is missing: {module}")
    gpu_license_lock = json.loads((SOURCE / "config/gpu-license-sources.lock.json").read_text(encoding="utf-8"))
    for release in gpu_license_lock["rocm"].values():
        for package in release["sourceArchives"]:
            name = package["name"]
            if (name in found or not re.fullmatch(r"[0-9a-f]{64}", package["sha256"])
                    or Path(package["filename"]).name != package["filename"]):
                raise RuntimeError(f"Invalid or duplicate GPU source package: {name}")
            found[name] = package
    found["llama.cpp"] = LLAMA
    return [found[name] for name in sorted(found)]


def get_archive(entry, root: Path, cache: Path, reserve: int, download: bool) -> Path:
    target = cache / entry["filename"]
    candidates = [target, root / "deps/llama-b11461.tar.gz"] if entry["name"] == "llama.cpp" else [target]
    cached_directory = root / "deps/dl" / entry["name"].removeprefix("qcs_")
    if cached_directory.is_dir():
        candidates.extend(sorted(path for path in cached_directory.iterdir() if path.is_file()))
    for candidate in candidates:
        if candidate.is_file() and not candidate.is_symlink():
            if entry.get("size") and candidate.stat().st_size != entry["size"]:
                continue
            if digest(candidate) == entry["sha256"]:
                return candidate
    if not download:
        raise RuntimeError(f"Verified source archive missing for {entry['name']}. Run with --download to fetch the locked URL.")
    guard(cache, reserve, entry.get("size", 128 * 1024 * 1024))
    temporary = target.with_name(target.name + ".part")
    if temporary.is_symlink() or target.is_symlink():
        raise RuntimeError(f"Refusing source cache symlink: {temporary}")
    print(f"Downloading source: {entry['name']}", flush=True)
    request = urllib.request.Request(entry["url"], headers={"User-Agent": PRODUCT + "/source-materials"})
    actual_hash = hashlib.sha256()
    size = 0
    with urllib.request.urlopen(request, timeout=120) as response, temporary.open("wb") as output:
        if response.status != 200:
            raise RuntimeError(f"Unexpected source download status: {response.status}")
        for block in iter(lambda: response.read(1024 * 1024), b""):
            guard(cache, reserve, len(block))
            size += len(block)
            if entry.get("size") and size > entry["size"]:
                raise RuntimeError(f"Source archive exceeds the locked size: {entry['name']}")
            output.write(block)
            actual_hash.update(block)
    if actual_hash.hexdigest() != entry["sha256"] or (entry.get("size") and size != entry["size"]):
        raise RuntimeError(f"Source archive verification failed: {entry['name']}")
    os.replace(temporary, target)
    return target


def project_files():
    result = subprocess.run(["git", "-C", str(SOURCE), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                            capture_output=True, check=True)
    paths = sorted(set(item.decode("utf-8") for item in result.stdout.split(b"\0") if item))
    for relative in paths:
        path = SOURCE / relative
        if path.is_symlink() or not path.is_file() or SOURCE not in path.resolve().parents:
            raise RuntimeError(f"Unexpected source file type: {relative}")
        yield relative, path


def checkout_identity(candidate_commit: str | None = None):
    """Bind release materials to one clean commit before and after copying bytes."""
    if candidate_commit is not None and not re.fullmatch(r"[0-9a-f]{40}", candidate_commit):
        raise RuntimeError("--candidate-commit must be a full lowercase commit SHA")
    def git(*arguments):
        return subprocess.check_output(["git", "-C", str(SOURCE), *arguments], text=True).strip()
    identity = {"commit": git("rev-parse", "HEAD"), "sourceTree": git("rev-parse", "HEAD^{tree}"),
                "workingTreeModified": bool(git("status", "--porcelain", "--untracked-files=all"))}
    if candidate_commit is not None:
        if identity["commit"] != candidate_commit:
            raise RuntimeError("The source checkout does not match --candidate-commit")
        if identity["workingTreeModified"]:
            raise RuntimeError("Candidate source materials require a clean checkout")
    return identity


def verify_source_snapshot(identity, source_files, candidate_commit=None):
    if checkout_identity(candidate_commit) != identity:
        raise RuntimeError("The source checkout identity changed during bundle creation")
    current = dict(project_files())
    if set(current) != {entry["path"] for entry in source_files}:
        raise RuntimeError("The source file inventory changed during bundle creation")
    for entry in source_files:
        path = current[entry["path"]]
        if path.stat().st_size != entry["size"] or digest(path) != entry["sha256"]:
            raise RuntimeError(f"Source changed during bundle creation: {entry['path']}")


def dependency_locks(source_files):
    return [entry for entry in source_files
            if entry["path"] in {"dependencies.lock.json", "config/build-variants.json", "cmake/qcs-native-deps.cmake"}
            or entry["path"].endswith((".lock.json", "llama-source-manifest.sha256"))]


def add_file(archive: zipfile.ZipFile, relative: str, path: Path, expected_hash: str):
    info = zipfile.ZipInfo(relative, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_STORED
    info.external_attr = (0o100755 if relative.endswith(".sh") else 0o100644) << 16
    with path.open("rb") as source, archive.open(info, "w", force_zip64=True) as destination:
        hasher = hashlib.sha256()
        for block in iter(lambda: source.read(1024 * 1024), b""):
            hasher.update(block)
            destination.write(block)
        if hasher.hexdigest() != expected_hash:
            raise RuntimeError(f"Source changed during bundle creation: {relative}")


def add_text(archive: zipfile.ZipFile, relative: str, text: str):
    info = zipfile.ZipInfo(relative, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_STORED
    info.external_attr = 0o100644 << 16
    archive.writestr(info, text.encode("utf-8"))


def ffmpeg_metadata(qt_root: Path):
    patterns = {"Windows": "6.12.0/*/bin/avcodec-*.dll",
                "Darwin": "6.12.0/*/lib/libavcodec.*.dylib",
                "Linux": "6.12.0/*/lib/libavcodec.so.*"}
    candidates = sorted(path for path in qt_root.glob(patterns[platform.system()]) if path.is_file())
    if not candidates:
        raise RuntimeError("Cannot record the actual Qt FFmpeg build configuration")
    path = candidates[0].resolve()
    directory_handle = os.add_dll_directory(str(path.parent)) if os.name == "nt" else None
    try:
        if platform.system() == "Linux":
            # Qt's avutil needs RAND_bytes from avformat's SSL stub dependency
            # group. Keep that group alive and local, matching media_extractor.
            dependency_path = path.parent / "libavformat.so.63"
            if not dependency_path.is_file():
                raise RuntimeError("The selected Qt FFmpeg directory lacks libavformat.so.63")
            dependency_group = ctypes.CDLL(str(dependency_path.resolve()), mode=ctypes.RTLD_LOCAL)
            library = ctypes.CDLL(str(path), mode=ctypes.RTLD_LOCAL)
        else:
            library = ctypes.CDLL(str(path))
        library.avcodec_configuration.restype = ctypes.c_char_p
        library.avcodec_license.restype = ctypes.c_char_p
        library.avcodec_version.restype = ctypes.c_uint
        configuration = library.avcodec_configuration().decode("utf-8")
        license_name = library.avcodec_license().decode("utf-8")
        if "--enable-gpl" in configuration or "--enable-nonfree" in configuration:
            raise RuntimeError("The SDK FFmpeg build has incompatible GPL/nonfree features enabled")
        version = library.avcodec_version()
        return {"binary": path.name, "sha256": digest(path), "versionInteger": version,
                "configuration": configuration, "license": license_name}
    finally:
        if directory_handle:
            directory_handle.close()


def verify_bundle(path: Path, expected_sha256: str | None = None, candidate_commit: str | None = None):
    checksum = digest(path)
    if expected_sha256 and checksum != expected_sha256.lower():
        raise RuntimeError("The source bundle SHA-256 does not match the expected value")
    checked = 0
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        names = [info.filename for info in infos]
        if len(names) != len(set(names)):
            raise RuntimeError("The source bundle contains duplicate archive names")
        if archive.getinfo("source-manifest.json").file_size > 16 * 1024 * 1024:
            raise RuntimeError("The source manifest is unexpectedly large")
        manifest = json.loads(archive.read("source-manifest.json"))
        if manifest.get("schemaVersion") != 2 or manifest.get("product") != PRODUCT:
            raise RuntimeError("Unsupported source manifest schema or product")
        if manifest.get("platform") != "source" or manifest.get("architecture") != "any":
            raise RuntimeError("The source bundle must be platform neutral")
        if candidate_commit is not None:
            if not re.fullmatch(r"[0-9a-f]{40}", candidate_commit) or manifest.get("commit") != candidate_commit:
                raise RuntimeError("The source bundle does not match --candidate-commit")
            if manifest.get("workingTreeModified") is not False or manifest.get("qtSdkMetadata"):
                raise RuntimeError("Candidate source materials must be clean and independent of a local SDK")
        if not re.fullmatch(r"[0-9a-f]{40}", str(manifest.get("sourceTree", ""))):
            raise RuntimeError("Source tree identity is absent or invalid")
        if manifest.get("dependencyLocks") != dependency_locks(manifest["projectFiles"]):
            raise RuntimeError("Source dependency locks differ from the included project files")
        expected = {entry["archivePath"]: entry for entry in manifest["packages"]}
        expected.update({"project/" + entry["path"]: entry for entry in manifest["projectFiles"]})
        expected.update({"sdk-metadata/" + entry["path"]: entry for entry in manifest.get("qtSdkMetadata", [])})
        if set(names) != set(expected) | {"source-manifest.json", "README.txt"}:
            raise RuntimeError("The source bundle contents do not match its manifest")
        for name, entry in expected.items():
            if "size" in entry and archive.getinfo(name).file_size != entry["size"]:
                raise RuntimeError(f"Source bundle entry size mismatch: {name}")
            hasher = hashlib.sha256()
            with archive.open(name) as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    hasher.update(block)
            if hasher.hexdigest() != entry["sha256"]:
                raise RuntimeError(f"Source bundle entry SHA-256 mismatch: {name}")
            checked += 1
    return {"passed": True, "bundle": str(path), "sha256": checksum,
            "verifiedEntries": checked, "sourcePackages": len(manifest["packages"]),
            "workingTreeModified": manifest["workingTreeModified"], "commit": manifest["commit"],
            "sourceTree": manifest["sourceTree"], "platform": manifest["platform"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-root", type=Path)
    parser.add_argument("--qt-root", type=Path)
    parser.add_argument("--cache-dir", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--download", action="store_true")
    parser.add_argument("--manifest-only", action="store_true")
    parser.add_argument("--verify-bundle", type=Path)
    parser.add_argument("--expected-sha256")
    parser.add_argument("--candidate-commit", help="Require the exact clean release candidate SHA")
    parser.add_argument("--reserve-gib", type=float, default=3.0)
    args = parser.parse_args()
    if args.verify_bundle:
        print(json.dumps(verify_bundle(args.verify_bundle.expanduser().resolve(), args.expected_sha256,
                                       args.candidate_commit), ensure_ascii=False, indent=2))
        return
    if args.candidate_commit and args.qt_root:
        parser.error("--qt-root is developer metadata and cannot be added to a platform-neutral release candidate")
    if not args.build_root:
        parser.error("--build-root is required when collecting sources")
    root = external(args.build_root)
    cache = external(args.cache_dir or root / "deps/sources")
    output = external(args.output_dir or root / "pkg")
    if args.reserve_gib < 0:
        raise RuntimeError("Free-space reserve must not be negative")
    reserve = int(args.reserve_gib * 1024**3)
    cache.mkdir(parents=True, exist_ok=True)
    output.mkdir(parents=True, exist_ok=True)
    if args.manifest_only:
        print(json.dumps({"schemaVersion": 1, "packages": entries()}, ensure_ascii=False, indent=2))
        return
    identity = checkout_identity(args.candidate_commit)
    commit = identity["commit"]
    packages = entries()
    archives = []
    for entry in packages:
        path = get_archive(entry, root, cache, reserve, args.download)
        archives.append((entry, path))
    application = list(project_files())
    source_files = [{"path": relative, "sha256": digest(path), "size": path.stat().st_size} for relative, path in application]
    manifest = {"schemaVersion": 2, "product": PRODUCT, **identity,
                "platform": "source", "architecture": "any",
                "packages": [{**entry, "size": path.stat().st_size, "archivePath": "upstream/" + entry["filename"]} for entry, path in archives],
                "projectFiles": source_files,
                "dependencyLocks": dependency_locks(source_files),
                "archiveFormat": "ZIP_STORED with fixed timestamps, upstream source archives remain compressed",
                "modelsIncluded": False,
                "scope": "Sources for configured runtime dependencies, project recipes, patches, notices and SDK build metadata"}
    sdk_metadata = []
    if args.qt_root:
        qt_root = args.qt_root.expanduser().resolve()
        # Include only public SDK build/license metadata. No binaries, credentials, user
        # configuration or whole SDK directory is copied into source materials.
        for pattern in ("6.12.0/*/mkspecs/qconfig.pri", "6.12.0/*/mkspecs/qmodule.pri", "6.12.0/*/lib/Qt6*.prl",
                        "6.12.0/*/sbom/*.spdx.json"):
            for path in sorted(qt_root.glob(pattern)):
                if path.is_file() and not path.is_symlink():
                    sdk_metadata.append((path.relative_to(qt_root).as_posix(), path))
        manifest["qtSdkMetadata"] = [{"path": name, "sha256": digest(path)} for name, path in sdk_metadata]
        manifest["ffmpegRuntime"] = ffmpeg_metadata(qt_root)
    content = json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    bundle = output / f"{PRODUCT}-source-materials-{commit[:12]}.zip"
    if bundle.is_symlink():
        raise RuntimeError("Refusing a source output symlink")
    total = sum(path.stat().st_size for _, path in archives + application + sdk_metadata)
    guard(output, reserve, total + 16 * 1024 * 1024)
    temporary_handle, temporary_name = tempfile.mkstemp(prefix=PRODUCT + "-source-", suffix=".zip.part", dir=output)
    os.close(temporary_handle)
    temporary = Path(temporary_name)
    try:
        with zipfile.ZipFile(temporary, "w", allowZip64=True) as archive:
            add_text(archive, "source-manifest.json", content)
            add_text(archive, "README.txt", "QCopilots Search corresponding source materials\n\nUpstream archives are intentionally retained compressed. Verify source-manifest.json before extraction.\nProject recipes and patches: project/cmake, project/third_party/patches and project/tools.\nBuild instructions: project/docs/development.md and project/docs/source-materials.md.\nIndividual licenses are retained in upstream archives and project/THIRD_PARTY_NOTICES.md.\nThis archive does not claim that a modified working tree is an immutable release commit.\n")
            # Copy checked project files promptly. Large immutable archives are
            # streamed afterwards, retaining change detection for every entry.
            for (relative, path), entry in zip(application, source_files):
                add_file(archive, "project/" + relative, path, entry["sha256"])
            for (relative, path), entry in zip(sdk_metadata, manifest.get("qtSdkMetadata", [])):
                add_file(archive, "sdk-metadata/" + relative, path, entry["sha256"])
            for entry, path in archives:
                add_file(archive, "upstream/" + entry["filename"], path, entry["sha256"])
        verify_source_snapshot(identity, source_files, args.candidate_commit)
        os.replace(temporary, bundle)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    manifest_path = output / f"{PRODUCT}-source-manifest.json"
    if manifest_path.is_symlink():
        raise RuntimeError("Refusing a source manifest symlink")
    manifest_path.write_text(content, encoding="utf-8")
    checksum = digest(bundle)
    bundle.with_suffix(".zip.sha256").write_text(checksum + "  " + bundle.name + "\n", encoding="utf-8")
    refresh_package_checksums(output, [bundle, manifest_path])
    print(json.dumps({"ok": True, "bundle": str(bundle), "sha256": checksum, "size": bundle.stat().st_size,
                      "sourcePackages": len(archives), "manifest": str(manifest_path)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.CalledProcessError, zipfile.BadZipFile) as error:
        print(f"{PRODUCT}: {error}", file=sys.stderr)
        sys.exit(1)
