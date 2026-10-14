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
SCHEMA = 2
# GitHub requires every completed release asset to be smaller than 2 GiB.
MAX_ASSET_BYTES = 2 * 1024**3 - 1
WORKFLOW_PATH = ".github/workflows/qcs-native.yml"
EXTENSIONS = {"NSIS": ".exe", "ZIP": ".zip", "DEB": ".deb", "TGZ": ".tar.gz", "DragNDrop": ".dmg"}


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


def selector_module():
    spec = importlib.util.spec_from_file_location("qcs_artifact_selection", SOURCE / ".github/scripts/qcs-artifact-selection.py")
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def variants():
    document = read_json(SOURCE / "config/build-variants.json")
    rows = document["variants"]
    result = {item["id"]: item for item in rows}
    if document.get("schemaVersion") != 1 or len(result) != len(rows) or not result:
        raise ValueError("Invalid shared build configuration inventory")
    for name, item in result.items():
        if not re.fullmatch(r"[a-z0-9][a-z0-9.-]+", name):
            raise ValueError("Configuration ID is not a portable package identity")
        generators = item["generators"]
        if not generators or len(set(generators)) != len(generators) or any(value not in EXTENSIONS for value in generators):
            raise ValueError("Unsupported or repeated configuration package generator")
    return result


def project_version(source):
    match = re.search(r"project\(qcs\s+VERSION\s+([0-9]+\.[0-9]+\.[0-9]+)\s", (source / "CMakeLists.txt").read_text(encoding="utf-8"))
    if not match:
        raise ValueError("Cannot establish the candidate package version")
    return match.group(1)


def package_names(configuration, version, candidate, attempt):
    positive_id(attempt)
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version) or not re.fullmatch(r"[0-9a-f]{40}", candidate):
        raise ValueError("Invalid package version or candidate identity")
    prefix = f"qcs-{version}-{configuration}-{candidate[:12]}-attempt{attempt}"
    return {prefix + EXTENSIONS[value] for value in variants()[configuration]["generators"]}


def package_name_options(configuration, version, candidate, attempt):
    names = package_names(configuration, version, candidate, attempt)
    options = [names]
    # The package builder permits this only after the real ROCm payload reaches
    # the NSIS data-block limit. CI still verifies installer and portable ZIP.
    if configuration == "windows-x64-rocm":
        installer = next(name for name in names if name.endswith(".exe"))
        options.append(names - {installer} | {installer.removesuffix(".exe") + "-setup.zip"})
    return options


def require_asset_size(size, name):
    if not isinstance(size, int) or isinstance(size, bool) or not 0 < size <= MAX_ASSET_BYTES:
        raise ValueError(f"Release asset must be nonempty and smaller than 2 GiB: {name}")


def inspect_packages(path, expected, destination=None):
    """Read the exact user-package whitelist without extracting archive paths."""
    result = {}
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        names = [info.filename for info in infos]
        options = [expected] if isinstance(expected, set) else expected
        if len(names) != len(set(name.casefold() for name in names)) or set(names) not in options:
            raise ValueError("CI artifact has missing, duplicate or extra user packages")
        for info in infos:
            name, mode = info.filename, info.external_attr >> 16
            if (info.is_dir() or PurePosixPath(name).name != name or any(c in name for c in "\\:\r\n")
                    or stat.S_IFMT(mode) not in (0, stat.S_IFREG) or info.flag_bits & 1):
                raise ValueError("CI artifact user packages must be ordinary unencrypted files")
            require_asset_size(info.file_size, name)
            value, count = hashlib.sha256(), 0
            output = (destination / name).open("xb") if destination is not None else None
            try:
                with archive.open(info) as stream:
                    for block in iter(lambda: stream.read(1024 * 1024), b""):
                        count += len(block)
                        if count > info.file_size:
                            raise ValueError("CI artifact package exceeds its advertised size")
                        value.update(block)
                        if output:
                            output.write(block)
            finally:
                if output:
                    output.close()
            if count != info.file_size:
                raise ValueError("CI artifact package was truncated")
            result[name] = {"bytes": count, "sha256": value.hexdigest()}
    return result


def collect_pages(api, run_id, kind):
    selector = selector_module()
    pages = []
    for page in range(1, selector.MAX_PAGES + 1):
        url = selector.page_url(REPOSITORY, run_id, kind, page)
        document = api.request("GET", url.removeprefix(selector.API_ROOT))
        pages.append(document)
        total = document.get("total_count")
        if type(total) is not int or not 0 <= total <= selector.MAX_PAGES * selector.PAGE_SIZE:
            raise ValueError("GitHub inventory total_count is invalid")
        if page * selector.PAGE_SIZE >= total:
            return selector.unpack_pages(pages, kind)
    raise ValueError("GitHub inventory exceeds the pagination limit")


def validate_context(context, candidate, run_id, attempt):
    repository, run, requested, workflow = (context[key] for key in ("repository", "run", "attempt", "workflow"))
    positive_id(repository.get("id"))
    if repository.get("full_name") != REPOSITORY or repository.get("archived") is not False:
        raise ValueError("CI source repository does not match the authorized repository")
    workflow_id = positive_id(workflow.get("id"))
    if workflow.get("path") != WORKFLOW_PATH:
        raise ValueError("CI run belongs to a different workflow")
    for item in (run, requested):
        if (str(item.get("id")) != str(run_id) or item.get("head_sha") != candidate
                or item.get("workflow_id") != workflow_id or item.get("run_attempt") != attempt
                or item.get("path", "").split("@", 1)[0] != WORKFLOW_PATH
                or item.get("repository", {}).get("id") != repository["id"]
                or item.get("repository", {}).get("full_name") != REPOSITORY):
            raise ValueError("CI run, latest attempt, workflow or candidate identity differs")
        if item.get("status") != "completed" or item.get("conclusion") != "success":
            raise ValueError("The latest workflow attempt must have completed successfully")


def collect_ci(api, candidate, run_id, attempt):
    selector = selector_module()
    selector.arguments(REPOSITORY, str(run_id), candidate, attempt)
    prefix = f"/repos/{REPOSITORY}"
    run = api.request("GET", prefix + f"/actions/runs/{run_id}")
    workflow_id = positive_id(run.get("workflow_id"))
    context = {"repository": api.request("GET", prefix), "run": run,
               "attempt": api.request("GET", prefix + f"/actions/runs/{run_id}/attempts/{attempt}"),
               "workflow": api.request("GET", prefix + f"/actions/workflows/{workflow_id}")}
    validate_context(context, candidate, run_id, attempt)
    jobs = collect_pages(api, str(run_id), "jobs")
    artifacts = collect_pages(api, str(run_id), "artifacts")
    selection = selector.select(REPOSITORY, str(run_id), candidate, attempt, jobs, artifacts)
    require_passed(selection, "CI package selection failed")
    if set(selection["selected"]) != set(variants()):
        raise ValueError("CI selection differs from the shared configuration inventory")
    # A retry must not start while inventory pages are being collected.
    latest = api.request("GET", prefix + f"/actions/runs/{run_id}")
    validate_context({**context, "run": latest}, candidate, run_id, attempt)
    return {"context": context, "jobs": jobs, "artifacts": artifacts, "selection": selection}


def validate_sources(bundle, identity, source):
    sources = module("qcs-source-bundle")
    result = sources.verify_bundle(bundle, candidate_commit=identity["candidateHead"])
    require_passed(result, "Corresponding source verification failed")
    with zipfile.ZipFile(bundle) as archive:
        manifest = json.loads(archive.read("source-manifest.json"))
        if manifest.get("commit") != identity["checkoutCommit"] or manifest.get("workingTreeModified") is not False:
            raise ValueError("Corresponding sources belong to another or modified candidate")
        locked = {item["name"]: item for item in sources.entries()}
        supplied = {item["name"]: item for item in manifest["packages"]}
        if len(supplied) != len(manifest["packages"]) or set(supplied) != set(locked):
            raise ValueError("Corresponding source dependency set differs from the candidate locks")
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
        project = {item["path"]: item for item in manifest["projectFiles"]}
        if len(project) != len(manifest["projectFiles"]) or set(project) != set(tracked):
            raise ValueError("Corresponding source files differ from the immutable candidate")
        for name in project:
            with archive.open("project/" + name) as stream:
                if git_blob_identity(source, name, stream) != tracked[name]:
                    raise ValueError(f"Source archive file differs from the candidate: {name}")


def build_sources(source, candidate, work, cache):
    destination = work / "sources"
    subprocess.run([sys.executable, str(source / "tools/qcs-source-bundle.py"),
                    "--build-root", str(work / "source-build"), "--cache-dir", str(cache),
                    "--output-dir", str(destination), "--candidate-commit", candidate, "--download"], check=True)
    return safe_file(destination, f"qcs-source-materials-{candidate[:12]}.zip")


def prepare(source, candidate, run_id, attempt, output, source_cache=None, api=None):
    identity = identity_for(source, candidate, module("qcs-release-evidence"))
    output = external(output, source)
    cache = external(source_cache or output.parent / "qcs-source-cache", source, (output,))
    if output.exists():
        raise ValueError("Release output already exists. Use a new directory or upload its existing frozen manifest")
    api = api or GitHub(authentication())
    collected = collect_ci(api, candidate, str(run_id), attempt)
    selection = collected["selection"]
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".qcs-release-", dir=output.parent) as temporary:
        work, assets = Path(temporary), []
        stage = work / "candidate"
        (stage / "assets").mkdir(parents=True)
        (stage / "provenance/artifacts").mkdir(parents=True)
        write_json(stage / "provenance/github.json", collected)
        version, packages = project_version(source), {}
        for configuration, selected in sorted(selection["selected"].items()):
            artifact = stage / "provenance/artifacts" / f"{selected['artifactId']}.zip"
            api.download_artifact(selected["artifactId"], artifact, selected["artifactDigest"],
                                  max_bytes=len(variants()[configuration]["generators"]) * MAX_ASSET_BYTES + 16 * 1024**2)
            if digest(artifact) != selected["artifactDigest"].removeprefix("sha256:"):
                raise ValueError("Downloaded CI artifact SHA-256 differs from GitHub")
            expected = package_name_options(configuration, version, candidate, selected["sourceAttempt"])
            contents = inspect_packages(artifact, expected, stage / "assets")
            packages[configuration] = {**selected, "packages": []}
            for name, metadata in sorted(contents.items()):
                assets.append(record(stage, stage / "assets" / name, name=name, role="user-package", configuration=configuration))
                packages[configuration]["packages"].append({"filename": name, **metadata})
        source_bundle = build_sources(source, candidate, work, cache)
        validate_sources(source_bundle, identity, source)
        require_asset_size(source_bundle.stat().st_size, source_bundle.name)
        frozen_source = stage / "assets" / source_bundle.name
        shutil.copyfile(source_bundle, frozen_source)
        assets.append(record(stage, frozen_source, name=frozen_source.name, role="sources", configuration="all"))
        checksum = stage / "assets/SHA256SUMS"
        checksum.write_text("".join(f"{item['sha256']}  {item['name']}\n" for item in sorted(assets, key=lambda item: item["name"])), encoding="utf-8")
        assets.append(record(stage, checksum, name=checksum.name, role="checksums", configuration="all"))
        if identity_for(source, candidate, module("qcs-release-evidence")) != identity:
            raise ValueError("Source identity changed during release preparation")
        document = {"schemaVersion": SCHEMA, "kind": "release-freeze", "repository": REPOSITORY,
                    "identity": identity, "createdAtUtc": datetime.now(timezone.utc).isoformat(),
                    "runId": str(run_id), "requestedAttempt": attempt, "version": version,
                    "configurations": packages, "assets": assets,
                    "files": [record(stage, path) for path in sorted(stage.rglob("*")) if path.is_file()],
                    "published": False, "draftOnly": True}
        write_json(stage / MANIFEST, document)
        verify_freeze(stage / MANIFEST, source)
        stage.rename(output)
    return output / MANIFEST


def verify_freeze(manifest_path, source):
    evidence = module("qcs-release-evidence")
    root = manifest_path.parent.resolve()
    if manifest_path.name != MANIFEST:
        raise ValueError("Unexpected release freeze manifest filename")
    document = read_json(safe_file(root, manifest_path.name))
    if (document.get("schemaVersion") != SCHEMA or document.get("kind") != "release-freeze"
            or document.get("repository") != REPOSITORY or document.get("draftOnly") is not True
            or document.get("published") is not False):
        raise ValueError("Unsupported or non-draft release freeze manifest")
    identity = document["identity"]
    candidate = identity["candidateHead"]
    if identity_for(source, candidate, evidence) != identity or document["version"] != project_version(source):
        raise ValueError("Frozen release source differs from the current candidate")
    records = document["files"]
    if not records or len({item["path"].casefold() for item in records}) != len(records):
        raise ValueError("Frozen release files are missing or duplicated")
    files = {item["path"]: item for item in records}
    actual = {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}
    if actual != set(files) | {MANIFEST}:
        raise ValueError("Frozen release contains missing or extra files")
    for relative, item in files.items():
        path = safe_file(root, relative)
        if item["bytes"] != path.stat().st_size or item["sha256"] != digest(path):
            raise ValueError("Frozen release content changed: " + relative)
    collected = read_json(safe_file(root, "provenance/github.json"))
    run_id, attempt = document["runId"], document["requestedAttempt"]
    validate_context(collected["context"], candidate, run_id, attempt)
    selected = selector_module().select(REPOSITORY, run_id, candidate, attempt, collected["jobs"], collected["artifacts"])
    require_passed(selected, "Frozen CI package selection failed")
    if selected != collected["selection"] or set(selected["selected"]) != set(variants()):
        raise ValueError("Frozen CI selection differs from its complete execution history")
    expected_assets, packages = {}, {}
    provenance = {"provenance/github.json"}
    for configuration, selected_artifact in sorted(selected["selected"].items()):
        relative = f"provenance/artifacts/{selected_artifact['artifactId']}.zip"
        provenance.add(relative)
        artifact = safe_file(root, relative)
        if digest(artifact) != selected_artifact["artifactDigest"].removeprefix("sha256:"):
            raise ValueError("Frozen CI artifact SHA-256 differs from GitHub")
        contents = inspect_packages(artifact, package_name_options(configuration, document["version"], candidate, selected_artifact["sourceAttempt"]))
        packages[configuration] = {**selected_artifact, "packages": []}
        for name, metadata in sorted(contents.items()):
            expected_assets[name] = {**metadata, "role": "user-package", "configuration": configuration}
            packages[configuration]["packages"].append({"filename": name, **metadata})
    if packages != document["configurations"]:
        raise ValueError("Frozen package identities differ from their CI artifacts")
    source_name = f"qcs-source-materials-{candidate[:12]}.zip"
    source_bundle = safe_file(root, "assets/" + source_name)
    validate_sources(source_bundle, identity, source)
    expected_assets[source_name] = {"bytes": source_bundle.stat().st_size, "sha256": digest(source_bundle), "role": "sources", "configuration": "all"}
    expected_checksums = "".join(f"{item['sha256']}  {name}\n" for name, item in sorted(expected_assets.items()))
    checksum = safe_file(root, "assets/SHA256SUMS")
    if checksum.read_text(encoding="utf-8") != expected_checksums:
        raise ValueError("Release checksums differ from the accepted upload bytes")
    expected_assets[checksum.name] = {"bytes": checksum.stat().st_size, "sha256": digest(checksum), "role": "checksums", "configuration": "all"}
    assets = document["assets"]
    if len(assets) != len(expected_assets) or len({item["name"].casefold() for item in assets}) != len(assets):
        raise ValueError("Release assets are missing, duplicated or extra")
    for item in assets:
        name = item["name"]
        require_asset_size(item["bytes"], name)
        expected = {"path": "assets/" + name, "name": name, **expected_assets.get(name, {})}
        if item != expected or any(item[key] != files.get(item["path"], {}).get(key) for key in ("bytes", "sha256")):
            raise ValueError("Release asset differs from its accepted source or CI provenance")
    if set(files) != provenance | {"assets/" + name for name in expected_assets}:
        raise ValueError("Frozen release contains unexpected provenance or assets")
    return document


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

    def download_artifact(self, artifact_id, destination, expected_digest, *, max_bytes):
        artifact_id = positive_id(artifact_id)
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", expected_digest):
            raise ValueError("CI artifact has no immutable SHA-256 digest")
        headers = self.headers()
        headers["Accept"] = "application/vnd.github+json"
        request = urllib.request.Request(
            f"https://api.github.com/repos/{REPOSITORY}/actions/artifacts/{artifact_id}/zip", headers=headers)
        response = None
        for _ in range(4):
            try:
                response = self._opener.open(request, timeout=120)
                break
            except urllib.error.HTTPError as error:
                location = error.headers.get("Location", "")
                parsed = urllib.parse.urlsplit(location)
                host = parsed.hostname or ""
                allowed = (host in ("objects.githubusercontent.com", "release-assets.githubusercontent.com")
                           or host.endswith(".blob.core.windows.net") or host.endswith(".actions.githubusercontent.com"))
                if (error.code not in (301, 302, 303, 307, 308) or parsed.scheme != "https" or not allowed
                        or parsed.port not in (None, 443) or parsed.username or parsed.password):
                    raise ApiError(error.code) from None
                request = urllib.request.Request(location, headers={"User-Agent": "QCopilots-Search-release"})
        if response is None:
            raise ValueError("GitHub artifact download exceeded the redirect limit")
        value, count, created = hashlib.sha256(), 0, False
        try:
            with response:
                with destination.open("xb") as stream:
                    created = True
                    for block in iter(lambda: response.read(1024 * 1024), b""):
                        count += len(block)
                        if count > max_bytes:
                            raise ValueError("CI artifact exceeds the allowed user-package size budget")
                        value.update(block)
                        stream.write(block)
            if not count or value.hexdigest() != expected_digest.removeprefix("sha256:"):
                raise ValueError("Downloaded CI artifact SHA-256 differs from GitHub")
        except BaseException:
            if created:
                destination.unlink(missing_ok=True)
            raise

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
    current = collect_ci(api, candidate, document["runId"], document["requestedAttempt"])
    frozen = read_json(safe_file(root, "provenance/github.json"))
    if current["selection"] != frozen["selection"]:
        raise ValueError("Remote CI execution or artifact identity changed after release preparation")
    validate_tag(api, tag, candidate)
    releases = [release for release in paginated(api, prefix + "/releases") if release.get("tag_name") == tag]
    if len(releases) > 1:
        raise ValueError("Multiple releases use the requested tag")
    if releases:
        release = releases[0]
    else:
        release = api.request("POST", prefix + "/releases", {"tag_name": tag, "target_commitish": candidate,
            "name": title or tag, "draft": True, "prerelease": False, "generate_release_notes": False,
            "body": f"QCopilots Search 候选 {candidate}\n\n冻结清单 SHA-256: {manifest_hash}\n\n此记录仅为未公开的草稿。安装包、对应源码、冻结清单和校验和随资产保存。"})
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
    prepare_parser = commands.add_parser("prepare", help="Download verified CI user packages and freeze corresponding sources")
    prepare_parser.add_argument("--source-dir", type=Path, default=SOURCE)
    prepare_parser.add_argument("--candidate-commit", required=True)
    prepare_parser.add_argument("--run-id", required=True)
    prepare_parser.add_argument("--attempt", type=int, required=True)
    prepare_parser.add_argument("--source-cache-dir", type=Path)
    prepare_parser.add_argument("--output-dir", type=Path, required=True, help="New external directory for the frozen candidate")
    prepare_parser.add_argument("--repository", choices=(REPOSITORY,), default=REPOSITORY)
    upload_parser = commands.add_parser("upload-draft", help="Explicitly create/resume an unpublished draft, never overwrite assets")
    upload_parser.add_argument("--source-dir", type=Path, default=SOURCE)
    upload_parser.add_argument("--manifest", type=Path, required=True)
    upload_parser.add_argument("--tag", required=True)
    upload_parser.add_argument("--title")
    upload_parser.add_argument("--repository", choices=(REPOSITORY,), default=REPOSITORY)
    args = parser.parse_args(argv)
    try:
        if args.action == "prepare":
            path = prepare(args.source_dir.resolve(), args.candidate_commit, args.run_id, args.attempt,
                           args.output_dir, args.source_cache_dir)
            result = {"manifest": str(path), "sha256": digest(path), "published": False, "draftOnly": True}
        else:
            result = upload_draft(args.manifest.resolve(), args.source_dir.resolve(), args.tag, args.title)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except urllib.error.URLError:
        print("qcs release: GitHub transport failed. Retry after checking connectivity", file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.SubprocessError,
            zipfile.BadZipFile, http.client.HTTPException) as error:
        print("qcs release: " + str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
