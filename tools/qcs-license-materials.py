#!/usr/bin/env python3
"""Collect actual Qt SDK notices and pinned source licenses for runtime packages."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import shutil
import tarfile
import tempfile
import urllib.request
import zipfile

SOURCE = Path(__file__).resolve().parent.parent
MANIFEST = "qcs-license-manifest.json"
KIND = "runtime-license-materials"
QT_MODULES = {name: "6.12.0" for name in
              ("qtbase", "qt5compat", "qtsvg", "qtimageformats", "qtmultimedia", "qttranslations")}
QT_MODULES.update({"qtpdf": "6.140.0", "qtwebengine-chromium-pdf": "6.140.0"})
SOURCE_ARCHIVES = ("qtbase", "qtsvg", "qtimageformats", "qtmultimedia")
VENDORED_HEADERS = ("vendor/stb/stb_image.h", "vendor/miniaudio/miniaudio.h", "vendor/sheredom/subprocess.h")
MAX_FILE = 16 * 1024 * 1024
MAX_MATERIALS = 64 * 1024 * 1024
GPU_LICENSE_LOCK = SOURCE / "config/gpu-license-sources.lock.json"


def digest(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def attribution_object(pairs):
    result = {}
    for key, value in pairs:
        # Some pinned upstream descriptions repeat Comment. File references
        # must remain unambiguous even though non-operative metadata is kept.
        if key in result and key in {"LicenseFile", "LicenseFiles", "NoticeFile"}:
            raise ValueError(f"Duplicate attribution file reference: {key}")
        result[key] = value
    return result


def read_json(path):
    path = Path(path)
    if path.stat().st_size > MAX_FILE:
        raise ValueError(f"License JSON exceeds size limit: {path}")
    return json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=unique_object)


def relative_path(name):
    if not isinstance(name, str) or not name or "\\" in name or ":" in name:
        raise ValueError(f"Unsafe license path: {name}")
    parts = name.split("/")
    if any(part in ("", ".", "..") for part in parts) or PurePosixPath(name).is_absolute():
        raise ValueError(f"Unsafe license path: {name}")
    return PurePosixPath(name)


def safe_file(root, name):
    path = Path(root).joinpath(*relative_path(name).parts)
    if not path.resolve().is_relative_to(Path(root).resolve()):
        raise ValueError(f"License path escapes its root: {name}")
    for item in (path, *path.parents):
        if item == Path(root):
            break
        if item.is_symlink() or (hasattr(item, "is_junction") and item.is_junction()):
            raise ValueError(f"Linked license material: {name}")
    return path


def identifiers(document):
    standard, custom = set(), set()
    keys = {"licenseConcluded", "licenseDeclared", "licenseInfoInFile", "licenseInfoInFiles", "licenseInfoFromFiles", "dataLicense"}
    def visit(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in keys:
                    for expression in ([item] if isinstance(item, str) else item if isinstance(item, list) else []):
                        if not isinstance(expression, str):
                            raise ValueError("Non-string SPDX license expression")
                        # External document references remain in the unchanged SDK SBOM.
                        expression = re.sub(r"DocumentRef-[A-Za-z0-9.+-]+:LicenseRef-[A-Za-z0-9.+-]+", "", expression)
                        for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9.+-]*", expression):
                            if token not in {"AND", "OR", "WITH", "NONE", "NOASSERTION"}:
                                (custom if token.startswith("LicenseRef-") else standard).add(token)
                else:
                    visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)
    visit(document)
    return standard, custom


def parse_sbom(path, module):
    document = read_json(path)
    if not isinstance(document, dict) or not str(document.get("spdxVersion", "")).startswith("SPDX-"):
        raise ValueError(f"Invalid SDK SPDX document: {module}")
    if not isinstance(document.get("packages"), list) or not document["packages"]:
        raise ValueError(f"SDK SPDX packages are missing: {module}")
    standard, custom = identifiers(document)
    texts = {}
    for entry in document.get("hasExtractedLicensingInfos", []):
        identifier, content = entry.get("licenseId", ""), entry.get("extractedText", "")
        if not re.fullmatch(r"LicenseRef-[A-Za-z0-9.+-]+", identifier) or identifier in texts:
            raise ValueError(f"Invalid or duplicate custom license ID in {module}: {identifier}")
        if not isinstance(content, str) or not content.strip():
            raise ValueError(f"Custom license has no text in {module}: {identifier}")
        texts[identifier] = content
    if custom - texts.keys():
        raise ValueError(f"Undefined custom licenses in {module}: {sorted(custom - texts.keys())}")
    return document, standard, texts


def archive_file(entry, cache, download):
    name = entry["filename"]
    if relative_path(name).name != name or not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"]):
        raise ValueError("Invalid source archive lock")
    target = safe_file(cache, name)
    def verified(path):
        return path.is_file() and path.stat().st_size == entry["size"] and digest(path) == entry["sha256"]
    if target.exists():
        if not verified(target):
            raise ValueError(f"Locked license archive size/SHA mismatch: {name}")
        return target
    if not download:
        raise ValueError(f"Missing locked license archive: {target}")
    if not str(entry["url"]).startswith("https://"):
        raise ValueError("License sources require the locked HTTPS URL")
    Path(cache).mkdir(parents=True, exist_ok=True)
    handle, filename = tempfile.mkstemp(prefix="qcs-license-", suffix=".part", dir=cache)
    temporary = Path(filename)
    try:
        request = urllib.request.Request(entry["url"], headers={"User-Agent": "qcopilots-search/license-materials"})
        with os.fdopen(handle, "wb") as output, urllib.request.urlopen(request, timeout=120) as response:
            size = 0
            for block in iter(lambda: response.read(1024 * 1024), b""):
                size += len(block)
                if size > entry["size"]:
                    raise ValueError(f"License archive exceeds locked size: {name}")
                output.write(block)
        if not verified(temporary):
            raise ValueError(f"Downloaded license archive size/SHA mismatch: {name}")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def source_materials(archive, entry):
    """Read bounded license/notice files, never extract a source tree."""
    result, standards = {}, {}
    prefix = relative_path(entry["sourceArchiveRoot"]).as_posix() + "/"
    with tarfile.open(archive, "r:*") as source:
        members = {}
        for member in source:
            name = member.name.rstrip("/")
            relative_path(name)
            if name in members:
                raise ValueError(f"Duplicate source archive member: {name}")
            members[name] = member
        if len(members) > 200000:
            raise ValueError("License source archive has too many entries")
        def take(name):
            member = members.get(name)
            if not member or not member.isfile() or not name.startswith(prefix):
                raise ValueError(f"Missing, linked or invalid source license: {name}")
            relative = name[len(prefix):]
            if member.size <= 0 or member.size > MAX_FILE:
                raise ValueError(f"Source license size invalid: {name}")
            if relative not in result:
                data = source.extractfile(member).read(MAX_FILE + 1)
                if len(data) != member.size:
                    raise ValueError(f"Truncated source license: {name}")
                result[relative] = data
            return result[relative]
        for name, member in members.items():
            if not name.startswith(prefix) or member.isdir():
                continue
            relative = name[len(prefix):]
            leaf = PurePosixPath(relative).name
            if (relative.startswith("LICENSES/") or leaf.startswith(("LICENSE", "COPYING", "NOTICE"))
                    or (leaf == "qt_attribution.json" and relative.startswith("src/"))):
                take(name)
        for relative, data in list(result.items()):
            if PurePosixPath(relative).name != "qt_attribution.json":
                continue
            # Qt's pinned attribution format permits literal newlines inside
            # description strings. Preserve those source bytes unchanged.
            declarations = json.loads(data.decode("utf-8-sig"), object_pairs_hook=attribution_object, strict=False)
            for attribution in declarations if isinstance(declarations, list) else [declarations]:
                for key in ("LicenseFile", "LicenseFiles", "NoticeFile"):
                    value = attribution.get(key, [])
                    for reference in [value] if isinstance(value, str) else value:
                        if not reference:
                            continue
                        if PurePosixPath(reference).is_absolute() or "\\" in reference or ":" in reference:
                            raise ValueError(f"Unsafe attribution license reference: {reference}")
                        name = posixpath.normpath(prefix + str(PurePosixPath(relative).parent) + "/" + reference)
                        take(name)
        for name in result:
            path = PurePosixPath(name)
            if len(path.parts) == 2 and path.parts[0] == "LICENSES" and path.suffix == ".txt":
                standards[path.stem] = name
            if name.startswith("src/3rdparty/ffmpeg/LICENSE.") and path.suffix == ".txt":
                standards[path.stem.removeprefix("LICENSE.")] = name
    if sum(map(len, result.values())) > MAX_MATERIALS:
        raise ValueError("Source license materials exceed size limit")
    return result, standards


def runtime_license_materials(manifests):
    """Read the exact redistribution notices selected by the runtime deployer."""
    resources, files = [], []
    seen = set()
    for manifest_path in manifests:
        manifest_path = Path(manifest_path).resolve()
        document = read_json(manifest_path)
        if (not isinstance(document, dict) or document.get("schemaVersion") != 1
                or not isinstance(document.get("resources"), list)):
            raise ValueError("Invalid runtime license selection manifest")
        for resource in document["resources"]:
            if not isinstance(resource, dict):
                raise ValueError("Invalid runtime license component record")
            name, version = resource.get("name", ""), resource.get("version", "")
            if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9][a-z0-9._+-]*", name) or name in seen:
                raise ValueError(f"Invalid or duplicate runtime license component: {name}")
            if not isinstance(version, str) or not version.strip() or len(version) > 128:
                raise ValueError(f"Invalid runtime license component version: {name}")
            seen.add(name)
            selected, names = [], set()
            declarations = resource.get("files")
            if not isinstance(declarations, list) or not declarations:
                raise ValueError(f"Runtime component has no redistribution notices: {name}")
            for entry in declarations:
                if not isinstance(entry, dict) or not isinstance(entry.get("source"), str) or not entry["source"]:
                    raise ValueError(f"Invalid runtime license file record: {name}")
                relative = relative_path(entry["path"]).as_posix()
                if relative in names:
                    raise ValueError(f"Duplicate runtime license path: {name}/{relative}")
                names.add(relative)
                expected = entry.get("sha256", "")
                if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
                    raise ValueError(f"Invalid runtime license SHA-256: {name}/{relative}")
                source = Path(entry["source"])
                source = source if source.is_absolute() else safe_file(manifest_path.parent, source.as_posix())
                for item in (source, *source.parents):
                    if item.is_symlink() or (hasattr(item, "is_junction") and item.is_junction()):
                        raise ValueError(f"Linked runtime license source: {source}")
                if not source.is_file() or not 0 < source.stat().st_size <= MAX_FILE:
                    raise ValueError(f"Missing or oversized runtime license source: {source}")
                data = source.read_bytes()
                if hashlib.sha256(data).hexdigest() != expected:
                    raise ValueError(f"Runtime license SHA-256 differs: {source}")
                destination = f"runtime/{name}/{relative}"
                selected.append({"path": destination, "sha256": expected})
                files.append((destination, data, {"origin": "selected-runtime-redistribution-notice",
                              "component": name, "version": version, "source": str(source.resolve()),
                              "selectionManifest": str(manifest_path)}))
            resources.append({"name": name, "version": version, "files": selected})
    return resources, files


def fetch_gpu_license_sources(backend, version, cache, download=False):
    """Fetch pinned ROCm redistribution originals missing from its binary wheels."""
    if backend not in {"hip", "rocm"}:
        return {"schemaVersion": 1, "resources": []}
    lock = read_json(GPU_LICENSE_LOCK)
    if lock.get("schemaVersion") != 1 or version not in lock.get("rocm", {}):
        raise ValueError(f"No reviewed runtime license sources for {backend} {version}")
    selection = lock["rocm"][version]
    cache = Path(cache).expanduser().resolve()
    if cache == SOURCE or cache.is_relative_to(SOURCE) or cache == Path(cache.anchor):
        raise ValueError("GPU license cache must be outside the checkout and filesystem root")
    cache.mkdir(parents=True, exist_ok=True)

    def fetch(entry, maximum):
        expected, size, url = entry.get("sha256", ""), entry.get("size"), entry.get("url", "")
        if (not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected)
                or not isinstance(size, int) or not 0 < size <= maximum
                or not isinstance(url, str) or not url.startswith("https://")):
            raise ValueError("Invalid pinned GPU license source record")
        destination = safe_file(cache, expected)
        if destination.exists():
            if not destination.is_file() or destination.stat().st_size != size or digest(destination) != expected:
                raise ValueError(f"Cached GPU license source SHA-256/size differs: {url}")
            return destination
        if not download:
            raise ValueError(f"Verified GPU license source is missing: {url}")
        descriptor, filename = tempfile.mkstemp(prefix="qcs-gpu-license-", suffix=".part", dir=cache)
        temporary = Path(filename)
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "qcopilots-search/license-materials"})
            with os.fdopen(descriptor, "wb") as output, urllib.request.urlopen(request, timeout=120) as response:
                received = 0
                for block in iter(lambda: response.read(1024 * 1024), b""):
                    received += len(block)
                    if received > size:
                        raise ValueError(f"GPU license source exceeds locked size: {url}")
                    output.write(block)
            if temporary.stat().st_size != size or digest(temporary) != expected:
                raise ValueError(f"Downloaded GPU license source SHA-256/size differs: {url}")
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
        return destination

    archives = {entry["name"]: entry for entry in selection.get("sourceArchives", [])}
    resources = []
    for resource in selection["resources"]:
        files = []
        for entry in resource["files"]:
            relative_path(entry["path"])
            if "archive" in entry:
                if entry["archive"] not in archives:
                    raise ValueError(f"GPU license archive is not locked: {entry['archive']}")
                archive_path = fetch(archives[entry["archive"]], 128 * 1024 * 1024)
                member = relative_path(entry["member"]).as_posix()
                size, expected = entry.get("size"), entry.get("sha256", "")
                if (not isinstance(size, int) or not 0 < size <= MAX_FILE
                        or not re.fullmatch(r"[0-9a-f]{64}", expected)):
                    raise ValueError("Invalid GPU license archive member lock")
                if zipfile.is_zipfile(archive_path):
                    with zipfile.ZipFile(archive_path) as archive:
                        matches = [info for info in archive.infolist() if info.filename == member]
                        if (len(matches) != 1 or matches[0].file_size != size
                                or (matches[0].external_attr >> 16) & 0o170000 == 0o120000):
                            raise ValueError(f"Invalid GPU license ZIP member: {member}")
                        data = archive.read(matches[0])
                else:
                    with tarfile.open(archive_path, "r:*") as archive:
                        matches = [info for info in archive.getmembers() if info.name == member]
                        if len(matches) != 1 or not matches[0].isfile() or matches[0].size != size:
                            raise ValueError(f"Invalid GPU license tar member: {member}")
                        data = archive.extractfile(matches[0]).read(MAX_FILE + 1)
                if len(data) != size or hashlib.sha256(data).hexdigest() != expected:
                    raise ValueError(f"GPU license archive member SHA-256 differs: {member}")
                source = safe_file(cache, expected)
                if source.exists() and source.read_bytes() != data:
                    raise ValueError(f"Cached GPU license original differs: {member}")
                if not source.exists():
                    source.write_bytes(data)
            else:
                source = fetch(entry, MAX_FILE)
            files.append({"path": entry["path"], "source": str(source), "sha256": entry["sha256"]})
        resources.append({"name": resource["name"], "version": resource["version"], "files": files})
    return {"schemaVersion": 1, "resources": resources}


def collect(qt_prefix, llama_source, cache, output, source_lock, download=False, runtime_license_manifests=()):
    qt_prefix, llama_source, cache, output = map(Path, (qt_prefix, llama_source, cache, output))
    if output.resolve() == SOURCE or output.resolve().is_relative_to(SOURCE) or output.resolve() == Path(output.anchor):
        raise ValueError("License output must be outside the source checkout and filesystem root")
    if output.is_symlink() or (hasattr(output, "is_junction") and output.is_junction()):
        raise ValueError("License output cannot be linked")
    if output.exists() and (not output.is_dir() or (any(output.iterdir()) and read_json(output / MANIFEST).get("kind") != KIND)):
        raise ValueError("License output is not an owned materials directory")
    packages = read_json(source_lock)["packages"]
    locked = {entry["name"]: entry for entry in packages}
    if len(locked) != len(packages) or any(name not in locked for name in SOURCE_ARCHIVES):
        raise ValueError("Required Qt source license locks are missing or duplicate")
    materials, sources, standards, customs, coverage = {}, [], {}, {}, {}
    def add(name, data, origin):
        relative_path(name)
        if name in materials:
            raise ValueError(f"Duplicate license material: {name}")
        if not data or len(data) > MAX_FILE:
            raise ValueError(f"Empty or oversized license material: {name}")
        materials[name] = data
        sources.append({"path": name, **origin})
    add("qt/source-packages.lock.json", Path(source_lock).read_bytes(), {"origin": "project-source-lock"})
    for module, version in QT_MODULES.items():
        name = f"{module}-{version}.spdx.json"
        source = safe_file(qt_prefix / "sbom", name)
        _, required, texts = parse_sbom(source, module)
        add("qt/sbom/" + name, source.read_bytes(), {"origin": "installed-qt-sdk", "source": str(source.resolve())})
        coverage[module] = sorted(required)
        for identifier, content in texts.items():
            relative = f"qt/extracted/{module}/{identifier}.txt"
            add(relative, content.encode("utf-8"), {"origin": "sdk-spdx-extractedText", "source": name, "licenseId": identifier})
            customs[f"{module}:{identifier}"] = relative
    archives = []
    for module in SOURCE_ARCHIVES:
        entry = locked[module]
        path = archive_file(entry, cache, download)
        documents, available = source_materials(path, entry)
        for name, content in documents.items():
            add(f"qt/sources/{module}/{name}", content,
                {"origin": "locked-qt-source", "archive": entry["filename"], "archiveSha256": entry["sha256"], "member": entry["sourceArchiveRoot"] + "/" + name})
        for identifier, name in available.items():
            standards.setdefault(identifier, f"qt/sources/{module}/{name}")
        archives.append({key: entry[key] for key in ("name", "url", "filename", "size", "sha256")})
    required = set().union(*(set(ids) for ids in coverage.values()))
    if required - standards.keys():
        missing = {module: sorted(set(ids) - standards.keys()) for module, ids in coverage.items() if set(ids) - standards.keys()}
        raise ValueError(f"Qt SDK standard license texts missing by module: {missing}")
    llama = safe_file(llama_source, "LICENSE")
    add("llama/LICENSE", llama.read_bytes(), {"origin": "verified-llama-source", "source": str(llama.resolve())})
    # mtmd links these header libraries. Their complete notices live inside
    # the original headers, so preserve the bounded files without rewriting
    # or trying to infer license paragraph boundaries.
    for relative in VENDORED_HEADERS:
        path = safe_file(llama_source, relative)
        if not path.is_file() or not 0 < path.stat().st_size <= MAX_FILE:
            raise ValueError(f"Required vendored license header is absent or oversized: {relative}")
        with path.open("rb") as stream:
            content = stream.read(MAX_FILE + 1)
        add("llama/" + relative, content, {"origin": "verified-llama-vendored-header", "source": relative})
    for folder in ("licenses", "vendor"):
        for path in sorted((llama_source / folder).rglob("*")):
            if path.is_file() and path.name.startswith(("LICENSE", "COPYING", "NOTICE")):
                relative = path.relative_to(llama_source).as_posix()
                safe_file(llama_source, relative)
                add("llama/" + relative, path.read_bytes(), {"origin": "verified-llama-source", "source": relative})
    runtime_resources, runtime_files = runtime_license_materials(runtime_license_manifests)
    for name, data, origin in runtime_files:
        add(name, data, origin)
    notice = ("QCopilots Search runtime license materials\n\n"
              "llama/LICENSE is the unchanged pinned llama.cpp upstream MIT license, including its vendored ggml.\n"
              "llama/vendor preserves original stb_image, miniaudio and subprocess headers containing their distinct full license notices.\n"
              "qt/sbom retains the installed Qt module SPDX documents unchanged, including copyrights and license expressions.\n"
              "These module-level documents may describe optional or undeployed SDK features and external references. They are not a complete runtime dependency graph.\n"
              "qt/extracted retains their custom license texts. qt/sources contains verified upstream license/notice and attribution originals.\n"
              "Preserving alternative license texts does not change the selected distribution terms or enable optional GPL/nonfree codecs.\n"
              "runtime contains the unchanged SDK redistribution notices selected for the deployed GPU runtime components, when present.\n"
              "Corresponding sources and build recipes are distributed in the separate qcs-source-materials archive.\n")
    add("README.txt", notice.encode(), {"origin": "project-generated-scope"})
    if sum(map(len, materials.values())) > MAX_MATERIALS:
        raise ValueError("Runtime license materials exceed size limit")
    manifest = {"schemaVersion": 1, "kind": KIND, "qtModules": QT_MODULES,
                "standardLicenseIds": sorted(required), "standardLicenseFiles": {key: standards[key] for key in sorted(required)},
                "customLicenseFiles": customs, "moduleCoverage": coverage, "sourceArchives": archives,
                "runtimeResources": runtime_resources,
                "sources": sources, "files": [{"path": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()} for name, data in sorted(materials.items())]}
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix="qcs-license-", dir=output.parent))
    try:
        for name, data in materials.items():
            destination = safe_file(temporary, name)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
        (temporary / MANIFEST).write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        verify_materials(temporary)
        if output.exists():
            # Only the explicitly selected, marker-checked owned output is replaced.
            if output.resolve().parent != temporary.resolve().parent:
                raise ValueError("License output boundary changed")
            shutil.rmtree(output)
        os.replace(temporary, output)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return manifest


def verify_materials(root):
    root = Path(root)
    manifest = read_json(safe_file(root, MANIFEST))
    if manifest.get("schemaVersion") != 1 or manifest.get("kind") != KIND or manifest.get("qtModules") != QT_MODULES:
        raise ValueError("Runtime license manifest version/modules do not match required dependencies")
    records = manifest.get("files", [])
    names = [item["path"] for item in records]
    if len(set(names)) != len(names) or not records:
        raise ValueError("Runtime license manifest contains duplicate or missing files")
    actual = {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}
    if actual != set(names) | {MANIFEST}:
        raise ValueError("Runtime license files differ from manifest")
    for entry in records:
        path = safe_file(root, entry["path"])
        if path.stat().st_size != entry["bytes"] or digest(path) != entry["sha256"]:
            raise ValueError(f"Runtime license file size/SHA mismatch: {entry['path']}")
    if "llama/LICENSE" not in names or "README.txt" not in names:
        raise ValueError("Required llama/ggml license or scope notice is missing")
    for relative in VENDORED_HEADERS:
        name = "llama/" + relative
        if name not in names or not 0 < safe_file(root, name).stat().st_size <= MAX_FILE:
            raise ValueError(f"Required vendored license header is missing or oversized: {relative}")
    if "qt/source-packages.lock.json" not in names:
        raise ValueError("Qt source archive lock is missing")
    locked_entries = read_json(safe_file(root, "qt/source-packages.lock.json"))["packages"]
    locked = {entry["name"]: entry for entry in locked_entries}
    if len(locked) != len(locked_entries) or any(name not in locked for name in SOURCE_ARCHIVES):
        raise ValueError("Qt source archive lock has missing or duplicate modules")
    expected_archives = [{key: locked[module][key] for key in ("name", "url", "filename", "size", "sha256")}
                         for module in SOURCE_ARCHIVES]
    if manifest.get("sourceArchives") != expected_archives:
        raise ValueError("Qt source archive provenance differs from the preserved lock")
    required = set()
    custom_keys = set()
    for module, version in QT_MODULES.items():
        path = f"qt/sbom/{module}-{version}.spdx.json"
        if path not in names:
            raise ValueError(f"Required installed Qt SDK SBOM is missing: {module}")
        _, ids, texts = parse_sbom(safe_file(root, path), module)
        if manifest.get("moduleCoverage", {}).get(module) != sorted(ids):
            raise ValueError(f"SDK license coverage differs: {module}")
        required.update(ids)
        for identifier, content in texts.items():
            key = f"{module}:{identifier}"
            custom_keys.add(key)
            relative = manifest.get("customLicenseFiles", {}).get(key)
            if relative not in names or safe_file(root, relative).read_bytes() != content.encode("utf-8"):
                raise ValueError(f"SDK custom license text differs: {key}")
    if set(manifest.get("customLicenseFiles", {})) != custom_keys:
        raise ValueError("Custom license manifest differs from SDK")
    mapped = manifest.get("standardLicenseFiles", {})
    if manifest.get("standardLicenseIds") != sorted(required) or set(mapped) != required:
        raise ValueError("Standard license coverage differs from SDK")
    for identifier, name in mapped.items():
        allowed = {f"qt/sources/{module}/LICENSES/{identifier}.txt" for module in SOURCE_ARCHIVES}
        allowed.add(f"qt/sources/qtmultimedia/src/3rdparty/ffmpeg/LICENSE.{identifier}.txt")
        if name not in allowed or name not in names or safe_file(root, name).stat().st_size < 16:
            raise ValueError(f"Required standard license text is absent: {identifier}")
    runtime_files, runtime_names = set(), set()
    for resource in manifest.get("runtimeResources", []):
        component = resource.get("name", "")
        if (not isinstance(component, str) or not re.fullmatch(r"[a-z0-9][a-z0-9._+-]*", component)
                or component in runtime_names or not isinstance(resource.get("version"), str)
                or not resource["version"].strip() or not resource.get("files")):
            raise ValueError("Invalid or duplicate deployed runtime license component")
        runtime_names.add(component)
        for entry in resource["files"]:
            name = relative_path(entry["path"]).as_posix()
            if (name not in names or name in runtime_files or not name.startswith(f"runtime/{component}/")
                    or entry.get("sha256") != digest(safe_file(root, name))):
                raise ValueError(f"Deployed runtime license differs: {name}")
            runtime_files.add(name)
    if runtime_files != {name for name in names if name.startswith("runtime/")}:
        raise ValueError("Deployed runtime notices differ from component manifest")
    return {"passed": True, "files": len(records), "qtModules": list(QT_MODULES), "standardLicenses": len(required),
            "runtimeComponents": sorted(runtime_names)}


def validate_runtime_license_directory(root):
    """Package-verifier entry point. The manifest and all referenced bytes stay unchanged."""
    return verify_materials(root)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qt-prefix", type=Path)
    parser.add_argument("--llama-source", type=Path)
    parser.add_argument("--cache-dir", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--source-lock", type=Path, default=SOURCE / "tools/qcs-source-packages.lock.json")
    parser.add_argument("--download", action="store_true")
    parser.add_argument("--runtime-license-manifest", type=Path, action="append", default=[])
    parser.add_argument("--verify", type=Path)
    args = parser.parse_args()
    if args.verify:
        result = verify_materials(args.verify)
    else:
        if any(value is None for value in (args.qt_prefix, args.llama_source, args.cache_dir, args.output_dir)):
            parser.error("--qt-prefix, --llama-source, --cache-dir and --output-dir are required")
        manifest = collect(args.qt_prefix, args.llama_source, args.cache_dir, args.output_dir, args.source_lock,
                           args.download, args.runtime_license_manifest)
        result = {"output": str(args.output_dir.resolve()), "files": len(manifest["files"]), "standardLicenses": len(manifest["standardLicenseIds"])}
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, tarfile.TarError) as error:
        raise SystemExit(f"qcs license materials: {error}")
