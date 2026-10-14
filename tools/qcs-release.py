#!/usr/bin/env python3
"""Freeze verified release materials and explicitly upload an unpublished GitHub draft."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import http.client
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile

sys.dont_write_bytecode = True
SOURCE = Path(__file__).resolve().parents[1]
REPOSITORY = "xiaobing318/QCopilots-Search"
MANIFEST = "qcs-release-manifest.json"
SCHEMA = 1
# GitHub requires every release asset to be smaller than 2 GiB. Keep room for
# ZIP metadata and worst-case DEFLATE overhead when partitioning raw evidence.
MAX_ASSET_BYTES = 2 * 1024**3 - 1
EVIDENCE_PART_BYTES = MAX_ASSET_BYTES - 32 * 1024**2


def module(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), SOURCE / "tools" / (name + ".py"))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def digest(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def read_json(path):
    if path.stat().st_size > 32 * 1024 * 1024:
        raise ValueError("Release JSON exceeds the 32 MiB limit")
    return json.loads(path.read_text(encoding="utf-8-sig"),
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError("Non-finite JSON number")))


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")


def redirected(path):
    return path.is_symlink() or bool(getattr(path.lstat(), "st_file_attributes", 0)
                                    & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def safe_file(root, relative):
    if not isinstance(relative, str):
        raise ValueError("Release path must be a string")
    name = PurePosixPath(relative)
    if (not name.parts or name.is_absolute()
            or relative != name.as_posix() or any(part in (".", "..") for part in name.parts)
            or any(character in relative for character in "\\:\r\n")):
        raise ValueError("Unsafe release file path")
    root = root.resolve()
    path = root.joinpath(*name.parts)
    for item in (root.joinpath(*name.parts[:index]) for index in range(1, len(name.parts) + 1)):
        if redirected(item):
            raise ValueError(f"Release paths cannot use links or junctions: {relative}")
    if not path.is_file() or not path.resolve().is_relative_to(root):
        raise ValueError(f"Release file is absent or escapes its root: {relative}")
    return path


def external(path, source, inputs=()):
    path = path.expanduser().absolute()
    for parent in (path, *path.parents):
        if parent.exists() and redirected(parent):
            raise ValueError("Release output cannot use links or junctions")
    path = path.resolve()
    if path == Path(path.anchor) or path == source or source in path.parents or path in source.parents:
        raise ValueError("Release output must be outside and must not contain the source checkout")
    for entry in inputs:
        entry = entry.resolve()
        if path == entry or path in entry.parents or entry in path.parents:
            raise ValueError("Release output must not overlap its input evidence")
    return path


def require_passed(result, label):
    if result.get("passed") is not True or result.get("errors"):
        errors = result.get("errors") or ["No successful complete result"]
        raise ValueError(label + ": " + " | ".join(str(item) for item in errors))


def identity_for(source, candidate, evidence):
    if not re.fullmatch(r"[0-9a-f]{40}", candidate):
        raise ValueError("The candidate must be a full immutable Git SHA")
    if source.resolve() != SOURCE.resolve():
        raise ValueError("Run the release tool from the candidate's own source checkout")
    identity = evidence.source_identity(source, candidate)
    errors = evidence.valid_identity(identity)
    if errors or identity["checkoutCommit"] != candidate:
        raise ValueError("Release source must be the clean candidate checkout: " + " | ".join(errors))
    return identity


def record(root, path, **metadata):
    relative = path.relative_to(root).as_posix()
    path = safe_file(root, relative)
    return {"path": relative, "bytes": path.stat().st_size, "sha256": digest(path), **metadata}


def copy_bound(evidence, source, manifest_name, target):
    manifest = read_json(safe_file(source, manifest_name))
    errors = evidence.verify_manifest(source, manifest)
    if errors:
        raise ValueError("Cannot collect changed evidence: " + " | ".join(errors))
    names = [item["path"] for item in manifest["files"]] + [manifest_name]
    if len(set(names)) != len(names):
        raise ValueError("Evidence manifest cannot contain itself")
    for relative in names:
        original = safe_file(source, relative)
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(original, destination)
    errors = evidence.verify_manifest(target, read_json(target / manifest_name))
    if errors:
        raise ValueError("Evidence changed during collection: " + " | ".join(errors))


def copy_selection(evidence, source, target, candidate, selection):
    """Preserve the raw API snapshots needed to reselect retry artifacts offline."""
    if evidence.load_native_selection(source, candidate) != selection:
        raise ValueError("Native artifact selection changed during collection")
    selection_path = evidence.native_selection_path(source)
    selection_root = selection_path.parent
    records = list(selection["files"]) + [record(selection_root, selection_path)]
    if len({item["path"].casefold() for item in records}) != len(records):
        raise ValueError("Native selection attachments contain colliding paths")
    for item in records:
        original = safe_file(selection_root, item["path"])
        if original.stat().st_size != item["bytes"] or digest(original) != item["sha256"]:
            raise ValueError("Native selection attachment changed before collection")
        destination = target / item["path"]
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(original, destination)
        if destination.stat().st_size != item["bytes"] or digest(destination) != item["sha256"]:
            raise ValueError("Native selection attachment changed during collection")
    if evidence.load_native_selection(target, candidate) != selection:
        raise ValueError("Collected native artifact selection differs from the accepted input")


def zip_files(destination, root, names):
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for relative in sorted(names):
            original = safe_file(root, relative)
            info = zipfile.ZipInfo(relative, (1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            with original.open("rb") as source, archive.open(info, "w", force_zip64=True) as target:
                shutil.copyfileobj(source, target, 1024 * 1024)


def evidence_archive_groups(root, names, budget=EVIDENCE_PART_BYTES):
    groups, current, size = [], [], 0
    for name in sorted(names):
        # Account for both local and central ZIP headers, long UTF-8 names and
        # DEFLATE expansion. This bounds each part before expensive compression.
        source_size = safe_file(root, name).stat().st_size
        charge = source_size + source_size // 500 + 1024 + 2 * len(name.encode("utf-8"))
        if charge > budget:
            raise ValueError(f"Evidence member is too large for a GitHub release asset: {name}")
        if current and size + charge > budget:
            groups.append(current)
            current, size = [], 0
        current.append(name)
        size += charge
    if current:
        groups.append(current)
    return groups


def require_asset_size(size, name):
    if not isinstance(size, int) or isinstance(size, bool) or not 0 < size <= MAX_ASSET_BYTES:
        raise ValueError(f"Release asset must be nonempty and smaller than 2 GiB: {name}")


def validate_symbols(path, root, platform_id, evidence):
    build = read_json(safe_file(root, "reports/build-provenance.json"))
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)) or archive.getinfo("qcs-symbols-manifest.json").file_size > 8 * 1024 * 1024:
            raise ValueError("Invalid symbol archive member list")
        document = json.loads(archive.read("qcs-symbols-manifest.json"))
        system, architecture = evidence.PLATFORMS[platform_id]
        if document.get("platform") != system or normalized_arch(document.get("architecture")) != normalized_arch(architecture):
            raise ValueError("Symbols belong to a different native platform")
        programs = document.get("programs", [])
        if len(programs) != 4 or {item.get("name") for item in programs} != evidence.APPLICATIONS:
            raise ValueError("Symbols must contain all four application programs")
        for program in programs:
            if program.get("unstrippedBinarySha256") != build["programs"][program["name"]]["sha256"]:
                raise ValueError("Symbols do not match their recorded native build")
        files = document.get("files", [])
        expected = {item["path"] for item in files}
        if len(expected) != len(files) or set(names) != expected | {"qcs-symbols-manifest.json"} or not files:
            raise ValueError("Symbol archive contents differ from its manifest")
        for program in evidence.APPLICATIONS:
            present = (program + ".pdb" in expected if system == "Windows" else
                       program + ".debug" in expected if system == "Linux" else
                       any(name.startswith(program + ".dSYM/") for name in expected))
            if not present:
                raise ValueError(f"Symbol archive has no debug information for {program}")
        for item in files:
            name = PurePosixPath(item["path"])
            if name.is_absolute() or ".." in name.parts or any(c in item["path"] for c in "\\:\r\n"):
                raise ValueError("Unsafe symbol archive path")
            value = hashlib.sha256()
            with archive.open(item["path"]) as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    value.update(block)
            if archive.getinfo(item["path"]).file_size != item.get("bytes") or value.hexdigest() != item.get("sha256"):
                raise ValueError("Symbol archive member checksum mismatch")


def normalized_arch(value):
    return str(value).lower().replace("amd64", "x86_64").replace("aarch64", "arm64")


def git_blob_identity(source, relative, stream):
    """Compare committed identities after Git's per-path clean rules, including -text.

    A clean Windows checkout can still contain CRLF while the matching Linux
    archive contains LF. Git applies .gitattributes without writing an object.
    """
    command = ["git", "-C", str(source), "-c", "core.safecrlf=false", "hash-object", "--stdin", "--path=" + relative]
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        shutil.copyfileobj(stream, process.stdin, 1024 * 1024)
        process.stdin.close()
        process.stdin = None
        output, _ = process.communicate(timeout=120)
        if process.returncode or not re.fullmatch(rb"[0-9a-f]{40}\s*", output):
            raise ValueError(f"Cannot derive committed source identity for {relative}")
        return output.decode("ascii").strip()
    except BaseException:
        if process.poll() is None:
            process.kill()
        process.wait()
        raise


def validate_materials(root, platform_id, identity, source, evidence):
    manifest = read_json(safe_file(root, "reports/native-evidence.json"))
    names = [item["path"] for item in manifest["files"]]
    bundles = [name for name in names if name.startswith("pkg/") and "-source-materials-" in name and name.endswith(".zip")]
    symbols = [name for name in names if name.startswith("pkg/") and "-symbols-" in name and name.endswith(".zip")]
    if len(bundles) != 1 or len(symbols) != 1:
        raise ValueError("Each native platform requires one unambiguous source archive and symbol archive")
    sources = module("qcs-source-bundle")
    bundle = safe_file(root, bundles[0])
    require_passed(sources.verify_bundle(bundle), "Corresponding source verification failed")
    sidecar = read_json(safe_file(root, "pkg/qcs-source-manifest.json"))
    with zipfile.ZipFile(bundle) as archive:
        if json.loads(archive.read("source-manifest.json")) != sidecar:
            raise ValueError("Source archive and native source manifest disagree")
    if sidecar.get("commit") != identity["checkoutCommit"] or sidecar.get("workingTreeModified") is not False:
        raise ValueError("Source archive belongs to a modified or different checkout")
    system, architecture = evidence.PLATFORMS[platform_id]
    # Windows platform.machine() reports AMD64, while CMake reports x86_64.
    if sidecar.get("platform") != system or normalized_arch(sidecar.get("architecture")) != normalized_arch(architecture):
        raise ValueError("Source archive belongs to a different native platform")
    locked = {item["name"]: item for item in sources.entries()}
    supplied = {item["name"]: item for item in sidecar["packages"]}
    if len(supplied) != len(sidecar["packages"]) or set(supplied) != set(locked):
        raise ValueError("Source archive dependency set differs from the locked recipes")
    for name, item in locked.items():
        if any(supplied[name].get(key) != value for key, value in item.items()):
            raise ValueError(f"Corresponding source lock mismatch: {name}")
    tracked = {}
    tree = subprocess.check_output(["git", "-C", str(source), "ls-tree", "-r", "-z", identity["checkoutCommit"]])
    for entry in tree.split(b"\0"):
        if not entry:
            continue
        metadata, name = entry.split(b"\t", 1)
        mode, kind, object_id = metadata.split()
        if mode not in (b"100644", b"100755") or kind != b"blob":
            raise ValueError("Corresponding source contains an unsupported tracked file type")
        tracked[name.decode("utf-8")] = object_id.decode("ascii")
    project = {item["path"]: item for item in sidecar["projectFiles"]}
    if len(project) != len(sidecar["projectFiles"]) or set(project) != set(tracked):
        raise ValueError("Source archive project files differ from the immutable checkout")
    with zipfile.ZipFile(bundle) as archive:
        for name in project:
            with archive.open("project/" + name) as stream:
                if git_blob_identity(source, name, stream) != tracked[name]:
                    raise ValueError(f"Source archive file differs from the candidate: {name}")
    validate_symbols(safe_file(root, symbols[0]), root, platform_id, evidence)


def asset_name(platform_id, original):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.+-]*", original):
        raise ValueError("Release asset names must use portable filename characters")
    return "qcs-" + platform_id + "-" + original.removeprefix("qcs-")


def inspect_full(report_path, candidate, identity):
    result = module("qcs-dev").inspect_check(report_path, candidate)
    require_passed(result, "Local full check failed")
    if result.get("identity") != identity:
        raise ValueError("Local full check belongs to a different candidate identity")
    return result


def copy_full(report_path, target, candidate, identity):
    result = inspect_full(report_path, candidate, identity)
    report = read_json(report_path)
    relative = report.get("reportPath", "")
    name = PurePosixPath(relative)
    if not name.parts or name.is_absolute() or ".." in name.parts or "\\" in relative:
        raise ValueError("Local full check has no portable report path")
    root = report_path.resolve().parents[len(name.parts) - 1]
    if safe_file(root, relative) != report_path.resolve():
        raise ValueError("Local full check report path does not match its actual location")
    records = result["files"]
    for item in records:
        original = safe_file(root, item["path"])
        destination = target / item["path"]
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(original, destination)
    destination = target / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(report_path, destination)
    inspect_full(destination, candidate, identity)
    return relative


def prepare(source, candidate, native_parent, local_root, full_report, output):
    evidence = module("qcs-release-evidence")
    identity = identity_for(source, candidate, evidence)
    output = external(output, source, (native_parent, local_root, full_report.parent))
    if output.exists():
        raise ValueError("Release output already exists. Use a new directory or upload its existing frozen manifest")
    selection = evidence.load_native_selection(native_parent, candidate)
    selection_path = evidence.native_selection_path(native_parent)
    roots = evidence.discover_native(native_parent)
    gate = evidence.preflight(roots, local_root, candidate, selection_path=selection_path)
    require_passed(gate, "Release preflight failed")
    inspect_full(full_report, candidate, identity)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".qcs-release-", dir=output.parent) as temporary:
        stage = Path(temporary) / "candidate"
        stage.mkdir()
        copy_selection(evidence, native_parent, stage / "evidence/native", candidate, selection)
        for platform_id, root in sorted(roots.items()):
            artifact_name = selection["selected"][platform_id]["artifactName"]
            copy_bound(evidence, root, "reports/native-evidence.json", stage / "evidence/native" / artifact_name)
        copy_bound(evidence, local_root, "manifest.json", stage / "evidence/local")
        full_relative = copy_full(full_report, stage / "evidence/local-full", candidate, identity)
        staged_roots = evidence.discover_native(stage / "evidence/native")
        require_passed(evidence.preflight(staged_roots, stage / "evidence/local", candidate,
                                         selection_path=stage / "evidence/native/selection.json"),
                       "Collected release preflight failed")
        assets = []
        (stage / "assets").mkdir()

        def add(original, name, role, platform_id, provenance):
            require_asset_size(original.stat().st_size, name)
            destination = stage / "assets" / name
            if destination.exists():
                raise ValueError(f"Duplicate release asset name: {name}")
            shutil.copyfile(original, destination)
            assets.append(record(stage, destination, name=name, role=role, platformId=platform_id,
                                 provenance=provenance))

        for platform_id, root in sorted(staged_roots.items()):
            validate_materials(root, platform_id, identity, source, evidence)
            manifest = read_json(root / "reports/native-evidence.json")
            for item in manifest["files"]:
                name = item["path"]
                if not name.startswith("pkg/") or name.endswith(".sha256"):
                    continue
                original = safe_file(root, name)
                role = ("sources" if "-source-materials-" in name else "symbols" if "-symbols-" in name
                        else "source-manifest" if name.endswith("qcs-source-manifest.json") else "native-package")
                add(original, asset_name(platform_id, original.name), role, platform_id,
                    original.relative_to(stage).as_posix())
            licenses = [item["path"] for item in manifest["files"] if "/runtime-license-materials/" in item["path"]]
            if not licenses:
                raise ValueError(f"No bound runtime license materials for {platform_id}")
            license_zip = stage / "assets" / f"qcs-{platform_id}-licenses.zip"
            zip_files(license_zip, root, licenses)
            assets.append(record(stage, license_zip, name=license_zip.name, role="licenses", platformId=platform_id,
                                 provenance=root.relative_to(stage).as_posix()))
        local = read_json(stage / "evidence/local/manifest.json")
        candidate_path = safe_file(stage / "evidence/local", local["units"]["candidate"])
        candidate_doc = read_json(candidate_path)
        package = safe_file(candidate_path.parent, candidate_doc["package"])
        add(package, asset_name("local-windows-x64", package.name), "computer-use-candidate", "windows-x64",
            package.relative_to(stage).as_posix())
        for name in ("LICENSE", "THIRD_PARTY_NOTICES.md"):
            add(safe_file(source, name), "qcs-" + name, "application-notice", "all", "source/" + name)
        write_json(stage / "evidence/release-preflight.json", gate)
        evidence_files = [path.relative_to(stage).as_posix() for path in (stage / "evidence").rglob("*") if path.is_file()]
        for index, group in enumerate(evidence_archive_groups(stage, evidence_files), 1):
            archive = stage / "assets" / f"qcs-release-evidence-{candidate[:12]}-part{index:03d}.zip"
            zip_files(archive, stage, group)
            require_asset_size(archive.stat().st_size, archive.name)
            assets.append(record(stage, archive, name=archive.name, role="acceptance-evidence", platformId="all",
                                 provenance="evidence"))
        checksum = stage / "assets/SHA256SUMS"
        checksum.write_text("".join(f"{item['sha256']}  {item['name']}\n" for item in sorted(assets, key=lambda x: x["name"])), encoding="utf-8")
        assets.append(record(stage, checksum, name=checksum.name, role="checksums", platformId="all", provenance="generated"))
        if len(assets) + 1 > 1000:
            raise ValueError("A GitHub release cannot contain more than 1,000 assets including the freeze manifest")
        for item in assets:
            require_asset_size(item["bytes"], item["name"])
        if identity_for(source, candidate, evidence) != identity:
            raise ValueError("Source identity changed during release preparation")
        document = {"schemaVersion": SCHEMA, "kind": "release-freeze", "repository": REPOSITORY,
                    "identity": identity, "createdAtUtc": datetime.now(timezone.utc).isoformat(),
                    "assets": assets, "freezeManifest": {"nativeEvidence": "evidence/native", "localEvidence": "evidence/local",
                    "nativeSelection": "evidence/native/selection.json",
                    "localFullReport": "evidence/local-full/" + full_relative,
                    "preflight": "evidence/release-preflight.json", "files": [record(stage, path) for path in sorted(stage.rglob("*")) if path.is_file()]},
                    "published": False, "draftOnly": True}
        write_json(stage / MANIFEST, document)
        stage.rename(output)
    return output / MANIFEST


def verify_freeze(manifest_path, source):
    evidence = module("qcs-release-evidence")
    root = manifest_path.parent.resolve()
    document = read_json(safe_file(root, manifest_path.name))
    if (document.get("schemaVersion") != SCHEMA or document.get("kind") != "release-freeze"
            or document.get("repository") != REPOSITORY or document.get("draftOnly") is not True
            or document.get("published") is not False):
        raise ValueError("Unsupported or non-draft release freeze manifest")
    identity = document["identity"]
    if identity_for(source, identity["candidateHead"], evidence) != identity:
        raise ValueError("Frozen release source differs from the current candidate")
    freeze = document["freezeManifest"]
    if (freeze.get("nativeEvidence"), freeze.get("localEvidence"), freeze.get("preflight")) != (
            "evidence/native", "evidence/local", "evidence/release-preflight.json"):
        raise ValueError("Unexpected frozen evidence layout")
    if freeze.get("nativeSelection") != "evidence/native/selection.json":
        raise ValueError("The frozen candidate has no native artifact selection")
    errors = evidence.verify_manifest(root, {"identity": identity, "files": freeze["files"]})
    if errors:
        raise ValueError("Frozen release content changed: " + " | ".join(errors))
    for item in freeze["files"]:
        safe_file(root, item["path"])
    files = {item["path"]: item for item in freeze["files"]}
    assets = document.get("assets", [])
    if not assets or len(assets) + 1 > 1000 or len({item["name"].casefold() for item in assets}) != len(assets):
        raise ValueError("Release assets are missing or have colliding names")
    for item in assets:
        require_asset_size(item.get("bytes"), item.get("name"))
        if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.+-]*", item["name"])
                or item["path"] != "assets/" + item["name"] or item["bytes"] <= 0
                or any(item.get(key) != files.get(item["path"], {}).get(key) for key in ("bytes", "sha256"))):
            raise ValueError("Release asset is not bound to the frozen file manifest")
    expected = "".join(f"{item['sha256']}  {item['name']}\n" for item in sorted(assets, key=lambda x: x["name"]) if item["name"] != "SHA256SUMS")
    if safe_file(root, "assets/SHA256SUMS").read_text(encoding="utf-8") != expected:
        raise ValueError("Release checksums differ from the actual upload names")
    roots = evidence.discover_native(root / freeze["nativeEvidence"])
    selection_path = safe_file(root, freeze["nativeSelection"])
    selection = evidence.load_native_selection(selection_path.parent, identity["candidateHead"])
    for relative in ["selection.json", *(item["path"] for item in selection["files"])]:
        if "evidence/native/" + relative not in files:
            raise ValueError("Native selection attachment is not bound to the frozen file manifest")
    require_passed(evidence.preflight(roots, root / freeze["localEvidence"], identity["candidateHead"],
                                     selection_path=selection_path), "Upload preflight failed")
    for platform_id, native in roots.items():
        validate_materials(native, platform_id, identity, source, evidence)
    full_report = freeze.get("localFullReport", "")
    if not full_report.startswith("evidence/local-full/"):
        raise ValueError("The frozen candidate has no local full check report")
    inspect_full(safe_file(root, full_report), identity["candidateHead"], identity)
    validate_asset_origins(root, source, document, roots)
    return document


def verify_archive_copies(path, root, names):
    with zipfile.ZipFile(path) as archive:
        listed = archive.namelist()
        if len(listed) != len(set(listed)) or set(listed) != set(names):
            raise ValueError("Release evidence archive has missing, duplicate or extra members")
        for name in names:
            original = safe_file(root, name)
            value = hashlib.sha256()
            with archive.open(name) as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    value.update(block)
            if archive.getinfo(name).file_size != original.stat().st_size or value.hexdigest() != digest(original):
                raise ValueError("Release evidence archive differs from the bound original files")


def validate_asset_origins(root, source, document, roots):
    """Derive the complete upload set from accepted inputs, not user-editable asset claims."""
    expected = {}
    for platform_id, native in sorted(roots.items()):
        manifest = read_json(safe_file(native, "reports/native-evidence.json"))
        for item in manifest["files"]:
            if item["path"].startswith("pkg/") and not item["path"].endswith(".sha256"):
                original = safe_file(native, item["path"])
                expected[asset_name(platform_id, original.name)] = original
        licenses = [item["path"] for item in manifest["files"] if "/runtime-license-materials/" in item["path"]]
        name = f"qcs-{platform_id}-licenses.zip"
        verify_archive_copies(safe_file(root, "assets/" + name), native, licenses)
        expected[name] = None
    local = read_json(safe_file(root, "evidence/local/manifest.json"))
    candidate_path = safe_file(root / "evidence/local", local["units"]["candidate"])
    original = safe_file(candidate_path.parent, read_json(candidate_path)["package"])
    expected[asset_name("local-windows-x64", original.name)] = original
    for name in ("LICENSE", "THIRD_PARTY_NOTICES.md"):
        expected["qcs-" + name] = safe_file(source, name)
    evidence_names = [item["path"] for item in document["freezeManifest"]["files"] if item["path"].startswith("evidence/")]
    for index, group in enumerate(evidence_archive_groups(root, evidence_names), 1):
        name = "qcs-release-evidence-" + document["identity"]["candidateHead"][:12] + f"-part{index:03d}.zip"
        verify_archive_copies(safe_file(root, "assets/" + name), root, group)
        expected[name] = None
    expected["SHA256SUMS"] = None
    assets = {item["name"]: item for item in document["assets"]}
    if set(assets) != set(expected):
        raise ValueError("Frozen upload asset set omits or adds accepted release materials")
    for name, original in expected.items():
        if original and digest(original) != assets[name]["sha256"]:
            raise ValueError(f"Release asset does not match its accepted provenance: {name}")


class ApiError(RuntimeError):
    def __init__(self, status):
        super().__init__(f"GitHub API request failed (HTTP {status})")
        self.status = status


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, url):
        return None


class GitHub:
    """Fixed-host API transport. Authentication never enters a URL, argv or report."""
    def __init__(self, token):
        self._token = token
        self._opener = urllib.request.build_opener(NoRedirect)

    def headers(self):
        return {"Authorization": "Bearer " + self._token, "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "QCopilots-Search-release"}

    def request(self, method, endpoint, data=None):
        if not endpoint.startswith("/repos/" + REPOSITORY + "/") and endpoint != "/repos/" + REPOSITORY:
            raise ValueError("GitHub endpoint is outside the authorized repository")
        payload = json.dumps(data).encode("utf-8") if data is not None else None
        headers = self.headers()
        if payload is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request("https://api.github.com" + endpoint, data=payload, headers=headers, method=method)
        try:
            with self._opener.open(request, timeout=120) as response:
                content = response.read(16 * 1024 * 1024 + 1)
                if len(content) > 16 * 1024 * 1024:
                    raise ValueError("GitHub response is too large")
                return json.loads(content)
        except urllib.error.HTTPError as error:
            raise ApiError(error.code) from None

    def asset_digest(self, asset):
        supplied = asset.get("digest", "")
        if isinstance(supplied, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", supplied):
            return supplied.removeprefix("sha256:")
        asset_id = positive_id(asset.get("id"))
        headers = self.headers()
        headers["Accept"] = "application/octet-stream"
        request = urllib.request.Request(f"https://api.github.com/repos/{REPOSITORY}/releases/assets/{asset_id}", headers=headers)
        try:
            response = self._opener.open(request, timeout=120)
        except urllib.error.HTTPError as error:
            location = error.headers.get("Location", "")
            parsed = urllib.parse.urlsplit(location)
            if (error.code not in (301, 302, 303, 307, 308) or parsed.scheme != "https"
                    or parsed.hostname not in ("release-assets.githubusercontent.com", "objects.githubusercontent.com")
                    or parsed.port not in (None, 443) or parsed.username or parsed.password):
                raise ApiError(error.code) from None
            # Never forward the API bearer token to the signed download URL.
            try:
                response = self._opener.open(urllib.request.Request(location, headers={"User-Agent": "QCopilots-Search-release"}), timeout=120)
            except urllib.error.HTTPError as download_error:
                raise ApiError(download_error.code) from None
        value, count = hashlib.sha256(), 0
        with response:
            for block in iter(lambda: response.read(1024 * 1024), b""):
                count += len(block)
                if count > asset["size"]:
                    raise ValueError("Remote release asset exceeds its recorded size")
                value.update(block)
        if count != asset["size"]:
            raise ValueError("Remote release asset was truncated")
        return value.hexdigest()

    def upload(self, release_id, path, name):
        endpoint = f"/repos/{REPOSITORY}/releases/{positive_id(release_id)}/assets?" + urllib.parse.urlencode({"name": name})
        connection = http.client.HTTPSConnection("uploads.github.com", timeout=300)
        try:
            headers = self.headers()
            headers.update({"Content-Type": "application/octet-stream", "Content-Length": str(path.stat().st_size)})
            connection.putrequest("POST", endpoint)
            for key, value in headers.items():
                connection.putheader(key, value)
            connection.endheaders()
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    connection.send(block)
            response = connection.getresponse()
            payload = response.read(16 * 1024 * 1024 + 1)
            if response.status != 201:
                raise ApiError(response.status)
            if len(payload) > 16 * 1024 * 1024:
                raise ValueError("GitHub upload response is too large")
            return json.loads(payload)
        finally:
            connection.close()


def positive_id(value):
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError("Invalid GitHub object ID")
    return value


def authentication():
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        executable = shutil.which("gh")
        if not executable:
            raise ValueError("Set GH_TOKEN/GITHUB_TOKEN or authenticate GitHub CLI for github.com")
        completed = subprocess.run([executable, "auth", "token", "--hostname", "github.com"],
                                   capture_output=True, text=True, timeout=30, check=False)
        if completed.returncode:
            raise ValueError("GitHub CLI could not provide authentication for github.com")
        token = completed.stdout.strip()
    if not token or any(character.isspace() for character in token):
        raise ValueError("Invalid GitHub authentication value")
    return token


def paginated(api, endpoint):
    result = []
    for page in range(1, 101):
        rows = api.request("GET", endpoint + f"?per_page=100&page={page}")
        if not isinstance(rows, list):
            raise ValueError("GitHub returned an invalid paginated response")
        result.extend(rows)
        if len(rows) < 100:
            return result
    raise ValueError("GitHub pagination exceeded its explicit 10,000-item limit")


def validate_tag(api, tag, candidate):
    if (not tag or tag.startswith("-") or any(character.isspace() for character in tag)
            or subprocess.run(["git", "check-ref-format", "refs/tags/" + tag], capture_output=True, check=False).returncode):
        raise ValueError("Invalid release tag")
    prefix = "/repos/" + REPOSITORY
    try:
        reference = api.request("GET", prefix + "/git/ref/tags/" + urllib.parse.quote(tag, safe=""))["object"]
    except ApiError as error:
        if error.status == 404:
            return
        raise
    for _ in range(16):
        if reference.get("type") == "commit":
            if reference.get("sha") != candidate:
                raise ValueError("Release tag already points to a different commit")
            return
        if reference.get("type") != "tag" or not re.fullmatch(r"[0-9a-f]{40}", reference.get("sha", "")):
            raise ValueError("Release tag does not resolve to a commit")
        reference = api.request("GET", prefix + "/git/tags/" + reference["sha"])["object"]
    raise ValueError("Release tag nesting exceeds the supported depth")


def validate_draft(release, tag, candidate):
    if release.get("draft") is not True or release.get("published_at") is not None:
        raise ValueError("Existing release is published. This command only operates on unpublished drafts")
    if release.get("tag_name") != tag or release.get("target_commitish") != candidate:
        raise ValueError("Existing draft is not pinned to this exact tag and candidate commit")
    return positive_id(release.get("id"))


def check_remote_asset(api, remote, expected):
    if (remote.get("name") != expected["name"] or remote.get("state") != "uploaded"
            or remote.get("size") != expected["bytes"] or api.asset_digest(remote) != expected["sha256"]):
        raise ValueError(f"Remote asset differs from the frozen release: {expected['name']}. No asset was overwritten")


def upload_draft(manifest_path, source, tag, title=None, api=None):
    document = verify_freeze(manifest_path, source)
    root, candidate = manifest_path.parent.resolve(), document["identity"]["candidateHead"]
    manifest_hash = digest(manifest_path)
    expected = {item["name"]: item for item in document["assets"]}
    expected[manifest_path.name] = {"name": manifest_path.name, "path": manifest_path.name,
                                    "bytes": manifest_path.stat().st_size, "sha256": manifest_hash}
    if len(expected) > 1000:
        raise ValueError("A GitHub release cannot contain more than 1,000 assets")
    for item in expected.values():
        require_asset_size(item["bytes"], item["name"])
    api = api or GitHub(authentication())
    prefix = "/repos/" + REPOSITORY
    repository = api.request("GET", prefix)
    if repository.get("full_name") != REPOSITORY or repository.get("archived") is not False:
        raise ValueError("GitHub repository name or writable state does not match the authorized repository")
    positive_id(repository.get("id"))
    if api.request("GET", prefix + "/commits/" + candidate).get("sha") != candidate:
        raise ValueError("The immutable candidate commit is not present in the authorized remote repository")
    validate_tag(api, tag, candidate)
    releases = [release for release in paginated(api, prefix + "/releases") if release.get("tag_name") == tag]
    if len(releases) > 1:
        raise ValueError("Multiple releases use the requested tag")
    if releases:
        release = releases[0]
    else:
        release = api.request("POST", prefix + "/releases", {"tag_name": tag, "target_commitish": candidate,
            "name": title or tag, "draft": True, "prerelease": False, "generate_release_notes": False,
            "body": f"QCopilots Search 候选 {candidate}\n\n冻结清单 SHA-256: {manifest_hash}\n\n此记录仅为未公开的草稿。原始验收证据和校验和随资产保存。"})
    release_id = validate_draft(release, tag, candidate)
    def remote_assets():
        rows = paginated(api, prefix + f"/releases/{release_id}/assets")
        found = {}
        for row in rows:
            name = row.get("name")
            if name in found or name not in expected:
                raise ValueError("Draft contains duplicate or unexpected assets. No asset was changed")
            check_remote_asset(api, row, expected[name])
            found[name] = row
        return found

    existing = remote_assets()
    uploaded, reused = [], sorted(existing)
    for name, item in expected.items():
        if name in existing:
            continue
        validate_draft(api.request("GET", prefix + f"/releases/{release_id}"), tag, candidate)
        path = safe_file(root, item["path"])
        if path.stat().st_size != item["bytes"] or digest(path) != item["sha256"]:
            raise ValueError("Local asset changed after upload preflight")
        remote = api.upload(release_id, path, name)
        check_remote_asset(api, remote, item)
        uploaded.append(name)
    final = remote_assets()
    if set(final) != set(expected):
        raise ValueError("Draft upload is incomplete. Repeat this command to resume missing assets")
    validate_draft(api.request("GET", prefix + f"/releases/{release_id}"), tag, candidate)
    validate_tag(api, tag, candidate)
    if digest(manifest_path) != manifest_hash:
        raise ValueError("Freeze manifest changed during upload")
    if identity_for(source, candidate, module("qcs-release-evidence")) != document["identity"]:
        raise ValueError("Source changed while uploading the frozen candidate")
    return {"schemaVersion": 1, "repository": REPOSITORY, "repositoryId": repository["id"], "releaseId": release_id,
            "candidateCommit": candidate, "tag": tag, "draft": True, "published": False,
            "uploaded": uploaded, "reused": reused,
            "assets": [{"name": name, "id": positive_id(final[name]["id"]), "bytes": expected[name]["bytes"],
                        "sha256": expected[name]["sha256"]} for name in sorted(expected)]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    prepare_parser = commands.add_parser("prepare", help="Collect only complete, immutable release evidence and assets")
    prepare_parser.add_argument("--source-dir", type=Path, default=SOURCE)
    prepare_parser.add_argument("--candidate-commit", required=True)
    prepare_parser.add_argument("--native-artifacts-dir", type=Path, required=True, help="Four immediate child directories containing reports/native-evidence.json")
    prepare_parser.add_argument("--local-evidence-dir", type=Path, required=True, help="Collected local evidence directory containing manifest.json")
    prepare_parser.add_argument("--local-full-report", type=Path, required=True, help="Portable check --test-profile full report for this candidate")
    prepare_parser.add_argument("--output-dir", type=Path, required=True, help="New external directory for the frozen candidate")
    upload_parser = commands.add_parser("upload-draft", help="Explicitly create/resume an unpublished draft, never overwrite assets")
    upload_parser.add_argument("--source-dir", type=Path, default=SOURCE)
    upload_parser.add_argument("--manifest", type=Path, required=True)
    upload_parser.add_argument("--tag", required=True)
    upload_parser.add_argument("--title")
    upload_parser.add_argument("--repository", choices=(REPOSITORY,), default=REPOSITORY)
    args = parser.parse_args(argv)
    try:
        if args.action == "prepare":
            path = prepare(args.source_dir.resolve(), args.candidate_commit, args.native_artifacts_dir.resolve(),
                           args.local_evidence_dir.resolve(), args.local_full_report.resolve(), args.output_dir)
            result = {"manifest": str(path), "sha256": digest(path), "published": False, "draftOnly": True}
        else:
            result = upload_draft(args.manifest.resolve(), args.source_dir.resolve(), args.tag, args.title)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except urllib.error.URLError:
        print("qcs release: GitHub transport failed. Retry the same draft command after checking connectivity", file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.SubprocessError,
            zipfile.BadZipFile, http.client.HTTPException) as error:
        # HTTP responses, credentials and subprocess stderr are never included.
        print("qcs release: " + str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
