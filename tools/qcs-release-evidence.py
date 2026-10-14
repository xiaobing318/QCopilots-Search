#!/usr/bin/env python3
"""Capture and verify local candidate and Windows Computer Use evidence. Never publish."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from functools import wraps

sys.dont_write_bytecode = True
SOURCE = Path(__file__).resolve().parents[1]
LOCKS = ("dependencies.lock.json", "tools/qt-packages.lock.json", "tools/nsis-package.lock.json",
         "tools/qcs-source-packages.lock.json", "cmake/qcs-native-deps.cmake",
         "config/build-variants.json", "config/gpu-sdk.lock.json", "config/gpu-license-sources.lock.json")
GUI_EXECUTABLE = "qcs-gui"
APPLICATIONS = {GUI_EXECUTABLE, "qcs-service", "qcs-worker", "qcs-cli"}
PACKAGE_GOLDENS = {"query": "text", "screen-280": "image", "speech": "audio"}


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for data in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(data)
    return value.hexdigest()


def read_json(path: Path):
    if path.stat().st_size > 32 * 1024 * 1024:
        raise ValueError(f"Evidence JSON is too large: {path}")
    return json.loads(path.read_text(encoding="utf-8-sig"),
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError(f"Non-finite JSON number: {value}")))


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def check(errors, condition, message):
    if not condition:
        errors.append(message)


def within(value, minimum, maximum):
    return finite(value) and minimum <= value <= maximum


def same_number(left, right):
    return finite(left) and finite(right) and math.isclose(left, right, rel_tol=1e-9, abs_tol=1e-9)


def integer_at_least(value, minimum):
    return isinstance(value, int) and not isinstance(value, bool) and value >= minimum


def malformed_as_error(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except (TypeError, ValueError, AttributeError, KeyError, OverflowError) as error:
            return [f"Malformed {function.__name__} evidence: {error}"]
    return wrapped


@malformed_as_error
def validate_nsis_contract(doc, output):
    """Recompute the package-stage NSIS contract from its preserved raw output."""
    spec = importlib.util.spec_from_file_location("qcs_nsis_release_cases", SOURCE / ".github/scripts/qcs-ctest-evidence.py")
    parser = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(parser)
    counts, records, skips, errors = parser.inspect_signing_unittest(output, "Windows", parser.WORKFLOW_NATIVE_TEST)
    check(errors, not parser.TRUNCATED.search(output), "NSIS contract output was truncated")
    check(errors, counts == {"run": 1, "passed": 1, "failed": 0, "skipped": 0, "notApplicable": 0}
          and len(records) == 1 and not skips, "NSIS contract did not execute exactly one successful native case")
    check(errors, doc.get("schemaVersion") == 1 and doc.get("passed") is True
          and type(doc.get("exitCode")) is int and doc["exitCode"] == 0
          and doc.get("log") == "nsis-contract.log", "NSIS contract summary or child exit is incomplete")
    check(errors, doc.get("counts") == counts and doc.get("cases") == records and doc.get("errors") == [],
          "NSIS contract summary disagrees with its original case records")
    return errors


def safe_file(root: Path, relative: str) -> Path:
    if not isinstance(relative, str):
        raise ValueError("Evidence path must be a string")
    name = PurePosixPath(relative)
    if relative != name.as_posix() or name.is_absolute() or not name.parts or any(part in ("..", ".") for part in name.parts) or "\\" in relative or ":" in relative:
        raise ValueError(f"Unsafe evidence path: {relative}")
    root = root.resolve()
    path = root.joinpath(*name.parts)
    inside = [root.joinpath(*name.parts[:index]) for index in range(1, len(name.parts) + 1)]
    if not path.resolve().is_relative_to(root) or any(item.is_symlink() for item in inside):
        raise ValueError(f"Evidence escapes its root or uses a symbolic link: {relative}")
    if not path.is_file():
        raise ValueError(f"Evidence file is missing: {relative}")
    return path


def source_identity(source: Path, candidate_head: str | None = None):
    def git(*arguments):
        return subprocess.check_output(["git", "-C", str(source), *arguments], text=True, encoding="utf-8").strip()
    commit = git("rev-parse", "HEAD")
    return {"candidateHead": candidate_head or commit, "checkoutCommit": commit,
            "sourceTree": git("rev-parse", "HEAD^{tree}"),
            "workingTreeModified": bool(git("status", "--porcelain", "--untracked-files=normal")),
            "lockHashes": {name: digest(source / name) for name in LOCKS}}


@malformed_as_error
def valid_identity(identity):
    errors = []
    for key in ("candidateHead", "checkoutCommit", "sourceTree"):
        check(errors, isinstance(identity.get(key), str) and re.fullmatch(r"[0-9a-f]{40}", identity[key]) is not None,
              f"Source identity {key} is absent or invalid")
    check(errors, identity.get("workingTreeModified") is False, "Source identity is modified or unknown")
    locks = identity.get("lockHashes", {})
    check(errors, set(locks) == set(LOCKS) and all(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) for value in locks.values()),
          "Source dependency-lock fingerprints are incomplete")
    return errors


def write_json(path: Path, document):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def file_record(root: Path, path: Path):
    relative = path.relative_to(root).as_posix()
    checked = safe_file(root, relative)
    return {"path": relative, "bytes": checked.stat().st_size, "sha256": digest(checked), "sourcePath": str(path.resolve())}


def verified_build(root: Path, receipt):
    errors = verify_manifest(root, receipt)
    check(errors, receipt.get("kind") == "build-execution" and receipt.get("passed") is True
          and receipt.get("execution", {}).get("exitCode") == 0, "Build execution was not successful")
    execution = receipt.get("execution", {})
    check(errors, timestamp(execution.get("startedAt")) < timestamp(execution.get("finishedAt")), "Build execution time range is invalid")
    names = {item["path"] for item in receipt.get("files", [])}
    for name in (execution.get("stdout"), execution.get("stderr"), "begin.json"):
        check(errors, name in names, "Build execution has no bound original command/output")
    begin = read_json(safe_file(root, "begin.json"))
    check(errors, begin.get("identity") == receipt.get("identity") and begin.get("command") == execution.get("command"),
          "Build begin record differs from its completion receipt")
    programs = receipt.get("programs", {})
    check(errors, bool(programs), "Build record has no produced executables")
    for item in programs.values():
        check(errors, item.get("path") in names and digest(safe_file(root, item["path"])) == item.get("sha256"),
              "Build executable does not match its captured bytes")
    return errors


@malformed_as_error
def verify_manifest(root: Path, manifest):
    errors = valid_identity(manifest.get("identity", {}))
    seen = set()
    for entry in manifest.get("files", []):
        name = entry.get("path", "")
        check(errors, name not in seen, f"Duplicate evidence path: {name}")
        seen.add(name)
        try:
            path = safe_file(root, name)
            check(errors, path.stat().st_size == entry.get("bytes") and digest(path) == entry.get("sha256"), f"Evidence content changed: {name}")
        except (OSError, ValueError) as error:
            errors.append(str(error))
    check(errors, bool(seen), "The evidence manifest has no files")
    return errors


def package_contract():
    """Share immutable pins, independently verify the recorded measurements."""
    spec = importlib.util.spec_from_file_location("qcopilots_search_package_contract",
        SOURCE / "tools/qcs-verify-package.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    path = SOURCE / "tests/runtime/fixtures/vectors.json"
    if digest(path) != module.REFERENCE_HASHES["vectors.json"]:
        raise ValueError("Immutable package reference vectors have changed")
    return module.MODEL_ARTIFACTS, module.REFERENCE_HASHES, read_json(path)["vectors"]


def package_evidence_paths(doc, root: Path):
    paths = [item["rawReply"]["path"] for item in doc.get("modelSmoke", {}).get("vectors", [])]
    manifest = doc.get("licenseMaterials", {}).get("manifest")
    if isinstance(manifest, str):
        paths.append(manifest)
        content = read_json(safe_file(root, manifest))
        paths.extend((PurePosixPath(manifest).parent / entry["path"]).as_posix() for entry in content["files"])
    return paths


@malformed_as_error
def validate_package_release(doc, root: Path, *, model_fingerprint=None,
                             extraction_fingerprint=None, platform_id=None, worker_sha256=None):
    errors = []
    check(errors, doc.get("passed") is True and doc.get("releaseComplete") is True
          and doc.get("verificationScope") == "release" and doc.get("realModelRequired") is True,
          "Package verification is not a complete release acceptance")
    variants = {item["id"]: item for item in read_json(SOURCE / "config/build-variants.json")["variants"]}
    variant_id = doc.get("variant")
    variant = variants.get(variant_id, {})
    check(errors, bool(variant) and (platform_id is None or variant_id == platform_id),
          "Package build variant is missing or differs from the requested configuration")
    build_identity = doc.get("buildIdentity", {})
    check(errors, build_identity.get("schemaVersion") == 1
          and build_identity.get("variant") == variant_id and build_identity.get("backend") == variant.get("backend")
          and build_identity.get("configuration") in ("Debug", "Release", "RelWithDebInfo", "MinSizeRel")
          and build_identity.get("workingTreeModified") is False
          and re.fullmatch(r"[0-9a-f]{40}", build_identity.get("candidateCommit", "")) is not None,
          "Package build identity is incomplete or differs from its variant")
    if doc.get("commit") is not None:
        check(errors, build_identity.get("candidateCommit") == doc["commit"],
              "Package build identity differs from its verification candidate")
    for field, relative in (("dependencyLockSha256", "dependencies.lock.json"),
                            ("variantsSha256", "config/build-variants.json")):
        check(errors, build_identity.get(field) == digest(SOURCE / relative),
              f"Package build identity used different {relative}")
    check(errors, doc.get("licenseMaterials", {}).get("passed") is True,
          "Package license materials were not verified")
    try:
        licenses = doc.get("licenseMaterials", {})
        manifest_path = safe_file(root, licenses.get("manifest", ""))
        check(errors, digest(manifest_path) == licenses.get("manifestSha256"), "Package license manifest content changed")
        spec = importlib.util.spec_from_file_location("qcopilots_search_license_evidence",
            SOURCE / "tools/qcs-license-materials.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        summary = module.verify_materials(manifest_path.parent)
        check(errors, all(licenses.get(key) == value for key, value in summary.items()),
              "Package license summary does not match its preserved materials")
    except (OSError, ValueError, KeyError) as error:
        errors.append("Package license materials are incomplete: " + str(error))
    smoke = doc.get("modelSmoke", {})
    check(errors, smoke.get("passed") is True and smoke.get("dimensions") == 768
          and smoke.get("minimumCosine") == .999, "Package real-model acceptance is missing")
    expected_models, references, goldens = package_contract()
    check(errors, smoke.get("referenceHashes") == references, "Package golden inputs differ from the immutable reference")
    verified = smoke.get("modelVerification", {})
    artifacts = verified.get("artifacts", [])
    check(errors, verified.get("valid") is True and verified.get("directory") == smoke.get("modelDirectory")
          and bool(smoke.get("modelDirectory")), "Package model verification directory is missing or inconsistent")
    check(errors, len(artifacts) == len(expected_models)
          and {item.get("name") for item in artifacts} == set(expected_models), "Package model artifacts are incomplete or repeated")
    for item in artifacts:
        expected = expected_models.get(item.get("name"))
        check(errors, expected is not None and item.get("present") is True
              and (item.get("bytes"), item.get("sha256")) == expected
              and item.get("expectedBytes") == expected[0], "Package model artifact does not match its pinned content")
    vectors = smoke.get("vectors", [])
    check(errors, len(vectors) == 3 and {item.get("name") for item in vectors} == set(PACKAGE_GOLDENS),
          "Package requires each text, image and audio golden vector exactly once")
    for field, expected in (("modelFingerprint", model_fingerprint), ("extractionFingerprint", extraction_fingerprint)):
        values = {item.get(field) for item in vectors}
        check(errors, len(values) == 1 and all(isinstance(value, str) and value for value in values)
              and (expected is None or values == {expected}), "Package vectors disagree on " + field)
    for item in vectors:
        name = item.get("name")
        check(errors, item.get("modality") == PACKAGE_GOLDENS.get(name) and item.get("dimensions") == 768,
              "Package vector modality or dimensionality is wrong")
        actual, expected = item.get("embedding", []), item.get("expectedEmbedding", [])
        if (not isinstance(actual, list) or len(actual) != 768 or not all(finite(v) for v in actual)
                or expected != goldens.get(name)):
            errors.append("Package raw vector is invalid or its reference was replaced")
            continue
        actual_norm = math.sqrt(math.fsum(value * value for value in actual))
        expected_norm = math.sqrt(math.fsum(value * value for value in expected))
        check(errors, abs(actual_norm - 1) <= .0001 and abs(expected_norm - 1) <= .0001,
              "Package raw vector is not L2 normalized")
        if actual_norm <= 0 or expected_norm <= 0:
            continue
        cosine = math.fsum(a * b for a, b in zip(actual, expected)) / (actual_norm * expected_norm)
        check(errors, within(cosine, .999, 1.00001) and same_number(item.get("l2Norm"), actual_norm)
              and same_number(item.get("cosine"), cosine), "Package vector measurements do not match their raw components")
        try:
            raw = item["rawReply"]
            path = safe_file(root, raw["path"])
            check(errors, digest(path) == raw.get("sha256"), "Package worker reply content changed")
            reply = read_json(path)
            result = reply.get("result", {})
            check(errors, reply.get("ok") is True and reply.get("id") == "model-" + name,
                  "Package worker reply is unsuccessful or belongs to another request")
            if name == "query":
                recorded_vector = result.get("embedding")
            else:
                segments = [segment for segment in result.get("segments", []) if segment.get("modality") == item.get("modality")]
                check(errors, result.get("complete") is True and result.get("state") == "complete"
                      and result.get("embedded") is True and not result.get("error")
                      and not result.get("embeddingErrors") and len(segments) == 1
                      and len(result.get("segments", [])) == 1,
                      "Package worker did not complete the multimodal inference")
                if len(segments) == 1 and name == "screen-280":
                    check(errors, segments[0].get("imageBudget") == 280,
                          "Package screenshot extraction changed its reference image budget")
                if len(segments) == 1 and name == "speech":
                    check(errors, (segments[0].get("startMs"), segments[0].get("endMs")) == (0, 4000),
                          "Package audio extraction changed its reference time window")
                recorded_vector = segments[0].get("embedding") if len(segments) == 1 else None
            check(errors, recorded_vector == actual and all(result.get(field) == item.get(field)
                  for field in ("modelFingerprint", "extractionFingerprint")), "Package vector differs from its raw worker reply")
        except (OSError, ValueError) as error:
            errors.append(str(error))
    semantic = smoke.get("serviceSemantic", {})
    check(errors, semantic.get("semantic") is True and not semantic.get("warning") and bool(semantic.get("hits")),
          "Installed service did not complete semantic retrieval")
    runtime = smoke.get("runtime", {})
    linkage, architecture = runtime.get("linkage"), runtime.get("architecture")
    expected_arch = variant.get("arch")
    check(errors, architecture in ("x86_64", "amd64", "arm64", "aarch64")
          and expected_arch in ("x64", "arm64")
          and architecture in (("x86_64", "amd64") if expected_arch == "x64" else ("arm64", "aarch64")),
          "Package inference runtime architecture differs from the native platform")
    diagnostics = runtime.get("diagnostics", {})
    check(errors, diagnostics.get("buildVariant") == variant_id
          and diagnostics.get("packagedBackend") == variant.get("backend")
          and diagnostics.get("loaded") is True
          and diagnostics.get("selectedBackend") in ("cpu", variant.get("backend"))
          and diagnostics.get("gpuSelected") is (diagnostics.get("selectedBackend") != "cpu")
          and bool(diagnostics.get("deviceName")), "Package runtime backend diagnostics are incomplete or inconsistent")
    if variant.get("backend") != "cpu" and diagnostics.get("selectedBackend") == "cpu":
        check(errors, isinstance(diagnostics.get("fallbackReason"), str) and bool(diagnostics["fallbackReason"].strip()),
              "Package automatic CPU fallback has no explicit reason")
    verification = doc.get("backendVerification", {})
    cpu = verification.get("cpu", {})
    cpu_diagnostics = cpu.get("diagnostics", {})
    check(errors, cpu.get("passed") is True and cpu_diagnostics.get("loaded") is True
          and cpu_diagnostics.get("buildVariant") == variant_id and cpu_diagnostics.get("requestedBackend") == "cpu"
          and cpu_diagnostics.get("selectedBackend") == "cpu" and cpu_diagnostics.get("gpuSelected") is False,
          "Package did not prove explicit CPU fallback")
    strict = verification.get("strictGpu", {})
    strict_diagnostics = strict.get("diagnostics", {})
    check(errors, strict.get("passed") is True and isinstance(strict.get("hardwareTested"), bool),
          "Package strict GPU outcome or hardware coverage is missing")
    if strict.get("hardwareTested") is True:
        check(errors, variant.get("backend") != "cpu" and strict_diagnostics.get("loaded") is True
              and strict_diagnostics.get("gpuSelected") is True and strict_diagnostics.get("requestedBackend") == "gpu"
              and strict_diagnostics.get("selectedBackend") == variant.get("backend")
              and strict_diagnostics.get("buildVariant") == variant_id,
              "Package strict GPU test did not use the packaged GPU backend")
    else:
        check(errors, strict_diagnostics.get("loaded") is False and strict_diagnostics.get("gpuSelected") is False
              and strict_diagnostics.get("requestedBackend") == "gpu"
              and isinstance(strict.get("reason"), str) and bool(strict["reason"].strip()),
              "Package unavailable GPU must reject strict mode and record why hardware was not tested")
    check(errors, runtime.get("cpuBaselineForced") is False, "Package smoke CPU dispatch scope is missing or misreported")
    windows = PureWindowsPath(doc.get("applicationRoot", "")).is_absolute()
    path_type = PureWindowsPath if windows else PurePosixPath
    application = path_type(doc.get("applicationRoot", ""))
    check(errors, application.is_absolute() and ".." not in application.parts, "Package application root is not an absolute path")
    worker = runtime.get("workerExecutable", {})
    libraries = runtime.get("libraries", [])
    for record in [worker, *libraries]:
        path = path_type(record.get("path", ""))
        check(errors, path.is_absolute() and path != application and ".." not in path.parts and path.is_relative_to(application)
              and integer_at_least(record.get("bytes"), 1) and bool(re.fullmatch(r"[0-9a-f]{64}", record.get("sha256", ""))),
              "Package inference executable or library has no valid installed origin and content hash")
    check(errors, path_type(worker.get("path", "")).name in ("qcs-worker", "qcs-worker.exe")
          and (worker_sha256 is None or worker.get("sha256") == worker_sha256), "Package inference worker differs from the recorded executable")
    check(errors, linkage == "shared-backend-dispatch"
          and {"llama", "mtmd", "ggml", "ggml-base", "cpu-backend"}.issubset({item.get("role") for item in libraries}),
          "Package shared backend runtime libraries are incomplete")
    dispatch = runtime.get("cpuDispatch")
    check(errors, isinstance(dispatch, dict) and set(dispatch) == {"x64", "arm64"}
          and all(type(value) is bool for value in dispatch.values())
          and dispatch == {"x64": expected_arch == "x64", "arm64": False},
          "Package CPU dispatch selection differs from its architecture")
    check(errors, len({path_type(item.get("path", "")) for item in libraries}) == len(libraries),
          "Package inference runtime repeats a loaded file")
    return errors


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError("Execution timestamp is absent")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Execution timestamp has no timezone")
    return parsed


@malformed_as_error
def validate_cu(doc, root: Path, *, identity: dict, package_sha256: str, model_fingerprint: str):
    errors = []
    check(errors, doc.get("passed") is True and doc.get("kind") == "windows-computer-use", "Computer Use is incomplete")
    check(errors, doc.get("identity") == identity and doc.get("commit") == identity.get("checkoutCommit"), "Computer Use source identity is stale or unbound")
    check(errors, doc.get("packageSha256") == package_sha256 and doc.get("modelFingerprint") == model_fingerprint,
          "Computer Use package or model fingerprint differs from the candidate")
    start, end = timestamp(doc.get("startedAt")), timestamp(doc.get("finishedAt"))
    check(errors, end > start, "Computer Use execution time range is invalid")
    scenarios = doc.get("scenarios", [])
    check(errors, len(scenarios) == 10 and {item.get("id") for item in scenarios} == {f"CU-{n:02}" for n in range(1, 11)},
          "Computer Use must contain each of the ten scenarios exactly once")
    for item in scenarios:
        check(errors, item.get("status") == "passed" and all(isinstance(item.get(key), str) and item[key].strip() for key in ("expected", "actual"))
              and isinstance(item.get("actions"), list) and bool(item["actions"]), "Computer Use scenario lacks actual successful operations")
        check(errors, item.get("packageSha256") == package_sha256 and item.get("executables") == doc.get("executables"),
              "Computer Use scenario mixes candidate packages or executables")
        check(errors, start <= timestamp(item.get("startedAt")) < timestamp(item.get("finishedAt")) <= end,
              "Computer Use scenario time is outside its execution session")
        screenshots = item.get("screenshots", [])
        check(errors, isinstance(screenshots, list) and bool(screenshots), "Computer Use scenario has no screenshot")
        for name in screenshots:
            try:
                picture = safe_file(root, name)
                with picture.open("rb") as stream:
                    header = stream.read(16)
                check(errors, picture.stat().st_size > 64 and (header.startswith(b"\x89PNG\r\n\x1a\n") or header.startswith(b"\xff\xd8\xff")),
                      "Computer Use screenshot is empty or not PNG/JPEG")
            except (OSError, ValueError) as error:
                errors.append(str(error))
        try:
            check(errors, safe_file(root, item.get("operationLog", "")).stat().st_size > 0, "Computer Use operation log is empty")
        except (OSError, ValueError) as error:
            errors.append(str(error))
    return errors


def fresh_directory(parent: Path, prefix: str):
    parent = parent.expanduser().resolve()
    if parent == SOURCE or SOURCE in parent.parents:
        raise ValueError("Execution evidence must be outside the source checkout")
    if not re.fullmatch(r"[a-z0-9-]+", prefix):
        raise ValueError("Execution evidence directory kind is invalid")
    parent.mkdir(parents=True, exist_ok=True)
    # The receipt retains the complete execution identity. Do not repeat a full
    # UUID in the filesystem path, and let mkdtemp create it atomically.
    label = prefix.removesuffix("-evidence")[:16]
    return Path(tempfile.mkdtemp(prefix="qcs-" + label + "-", dir=parent))


def windows_short_directory(directory: Path):
    """Ask the filesystem for an existing alias, without creating a link."""
    import ctypes
    function = ctypes.WinDLL("kernel32", use_last_error=True).GetShortPathNameW
    function.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
    function.restype = ctypes.c_uint32
    size = function(str(directory), None, 0)
    if not size:
        return None
    buffer = ctypes.create_unicode_buffer(size)
    written = function(str(directory), buffer, size)
    return Path(buffer.value) if 0 < written < size else None


def fits_windows_cwd(path: Path | str):
    # Reserve the trailing separator and NUL in CreateProcessW's MAX_PATH buffer.
    return len(str(path).encode("utf-16-le")) // 2 <= 258


def execution_working_directory(root: Path, working_directory: Path | None = None):
    requested = (working_directory if working_directory is not None else root).expanduser().resolve()
    if not requested.is_dir():
        raise ValueError(f"Execution working directory does not exist: {requested}")
    actual = requested
    # CreateProcessW still limits cwd even when ordinary file operations allow long paths.
    if sys.platform == "win32" and not fits_windows_cwd(requested):
        candidate = windows_short_directory(requested)
        try:
            verified = candidate is not None and fits_windows_cwd(candidate) and os.path.samefile(requested, candidate)
        except OSError:
            verified = False
        if not verified:
            raise ValueError(
                f"Windows cannot launch a process with this working directory "
                f"({len(str(requested).encode('utf-16-le')) // 2} UTF-16 characters): {requested}. "
                "No verified short representation is available. Choose an existing shorter directory "
                "with --working-directory. This explicitly changes the base for relative command paths, "
                "while evidence and report paths remain absolute.")
        actual = candidate
    return requested, actual


def execute_record(command, root: Path, *, environment=None, working_directory: Path | None = None):
    requested, actual = execution_working_directory(root, working_directory)
    started = datetime.now(timezone.utc).isoformat()
    with (root / "stdout.log").open("wb") as stdout, (root / "stderr.log").open("wb") as stderr:
        completed = subprocess.run(command, cwd=actual, env=environment, stdout=stdout, stderr=stderr, check=False)
    return {"command": [str(item) for item in command], "startedAt": started,
            "finishedAt": datetime.now(timezone.utc).isoformat(), "exitCode": completed.returncode,
            "requestedWorkingDirectory": str(requested), "actualWorkingDirectory": str(actual),
            "stdout": "stdout.log", "stderr": "stderr.log"}


def record_build(source: Path, parent: Path, programs: dict[str, Path], command: list[str], *,
                 working_directory: Path | None = None):
    if not command or not programs:
        raise ValueError("A build command and explicit output programs are required")
    root = fresh_directory(parent, "build-evidence")
    before = source_identity(source)
    if valid_identity(before):
        raise ValueError("Build evidence requires a clean immutable source checkout")
    write_json(root / "begin.json", {"identity": before, "programs": {name: str(path) for name, path in programs.items()}, "command": command})
    execution = execute_record(command, root, working_directory=working_directory)
    after = source_identity(source)
    entries = {}
    if execution["exitCode"] == 0 and before == after:
        for name, path in programs.items():
            if not re.fullmatch(r"qcs(?:-[a-z0-9-]+)?", name):
                raise ValueError("Build program names must use the product prefix")
            destination = root / "programs" / (name + path.suffix)
            destination.parent.mkdir(exist_ok=True)
            shutil.copyfile(path, destination)
            entries[name] = {"originalPath": str(path.resolve()), **file_record(root, destination)}
    receipt = {"schemaVersion": 1, "kind": "build-execution", "identity": before,
               "sourceDirectory": str(source.resolve()), "execution": execution, "programs": entries,
               "passed": execution["exitCode"] == 0 and before == after and bool(entries)}
    receipt["files"] = [file_record(root, path) for path in root.rglob("*") if path.is_file()]
    write_json(root / "build-record.json", receipt)
    return root / "build-record.json", receipt


def record_package(build_record: Path, package: Path, verification: Path, parent: Path,
                   model_fingerprint: str, extraction_fingerprint: str):
    build = read_json(build_record)
    errors = verified_build(build_record.parent, build)
    checked = read_json(verification)
    package_hash = digest(package)
    if errors or build.get("passed") is not True or build.get("kind") != "build-execution":
        raise ValueError("The package requires a bound successful build record")
    if checked.get("passed") is not True or checked.get("commit") != build["identity"]["checkoutCommit"] or checked.get("workingTreeModified") is not False or checked.get("packageSha256") != package_hash:
        raise ValueError("Package verification is stale, modified or unsuccessful")
    errors.extend(validate_package_release(checked, verification.parent, model_fingerprint=model_fingerprint,
        extraction_fingerprint=extraction_fingerprint, platform_id="windows-x64-cpu",
        worker_sha256=build.get("programs", {}).get("qcs-worker", {}).get("sha256")))
    if errors:
        raise ValueError("Package release evidence is incomplete: " + "; ".join(errors))
    executables = {}
    with zipfile.ZipFile(package) as archive:
        for name in sorted(APPLICATIONS):
            matches = [item for item in archive.infolist() if PurePosixPath(item.filename).name == name + ".exe"]
            if len(matches) != 1:
                raise ValueError(f"The candidate ZIP does not contain one {name} executable")
            with archive.open(matches[0]) as stream:
                value = hashlib.sha256()
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    value.update(block)
            if value.hexdigest() != build.get("programs", {}).get(name, {}).get("sha256"):
                raise ValueError(f"Packaged executable differs from its recorded build: {name}")
            executables[name] = {"sha256": value.hexdigest(), "packageMember": matches[0].filename}
    root = fresh_directory(parent, "candidate-evidence")
    shutil.copytree(build_record.parent, root / "build")
    shutil.copyfile(package, root / package.name)
    shutil.copyfile(verification, root / "package-verification.json")
    for relative in package_evidence_paths(checked, verification.parent):
        source = safe_file(verification.parent, relative)
        destination = root.joinpath(*PurePosixPath(relative).parts)
        if destination.exists():
            raise ValueError("Package evidence would overwrite an existing candidate file")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
    candidate = {"schemaVersion": 1, "kind": "package-candidate", "identity": build["identity"],
                 "package": package.name, "packageSha256": package_hash, "executables": executables,
                 "modelFingerprint": model_fingerprint, "extractionFingerprint": extraction_fingerprint,
                 "recordedAt": datetime.now(timezone.utc).isoformat(), "verification": "package-verification.json",
                 "buildProvenance": {"path": "build/" + build_record.name, "sha256": digest(build_record)}}
    candidate["files"] = [file_record(root, path) for path in root.rglob("*") if path.is_file()]
    write_json(root / "package-candidate.json", candidate)
    return root / "package-candidate.json", candidate


def inspect_candidate(root: Path, candidate):
    errors = verify_manifest(root, candidate)
    check(errors, candidate.get("kind") == "package-candidate", "The candidate is not a captured package")
    bound = {item["path"] for item in candidate["files"]}
    provenance = candidate["buildProvenance"]
    for name in (provenance["path"], candidate["package"], candidate["verification"]):
        check(errors, name in bound, "Candidate package/build/verification bytes are not bound")
    build_path = safe_file(root, provenance["path"])
    build = read_json(build_path)
    errors.extend(verified_build(build_path.parent, build))
    check(errors, digest(build_path) == provenance["sha256"] and build.get("identity") == candidate.get("identity"),
          "Candidate package has stale executable build provenance")
    package = safe_file(root, candidate["package"])
    check(errors, digest(package) == candidate["packageSha256"], "Candidate archive bytes changed")
    verification = read_json(safe_file(root, candidate["verification"]))
    check(errors, verification.get("passed") is True and verification.get("commit") == candidate["identity"]["checkoutCommit"]
          and verification.get("workingTreeModified") is False and verification.get("packageSha256") == candidate["packageSha256"]
          and len(verification.get("checks", [])) >= 9, "Candidate package verification is incomplete or bound to different bytes")
    for relative in package_evidence_paths(verification, root):
        check(errors, relative in bound, "Candidate inference or license raw evidence is not bound")
    errors.extend(validate_package_release(verification, root, model_fingerprint=candidate["modelFingerprint"],
        extraction_fingerprint=candidate["extractionFingerprint"], platform_id="windows-x64-cpu",
        worker_sha256=candidate.get("executables", {}).get("qcs-worker", {}).get("sha256")))
    check(errors, set(candidate.get("executables", {})) == APPLICATIONS, "Candidate must contain all four application executable identities")
    with zipfile.ZipFile(package) as archive:
        for name in APPLICATIONS:
            entries = [item for item in archive.infolist() if PurePosixPath(item.filename).name == name + ".exe"]
            if len(entries) != 1:
                errors.append(f"Candidate ZIP executable count is invalid: {name}")
                continue
            with archive.open(entries[0]) as stream:
                value = hashlib.sha256()
                for data in iter(lambda: stream.read(1024 * 1024), b""):
                    value.update(data)
            expected = candidate.get("executables", {}).get(name, {})
            check(errors, expected.get("packageMember") == entries[0].filename
                  and expected.get("sha256") == value.hexdigest() == build.get("programs", {}).get(name, {}).get("sha256"),
                  f"Candidate executable is not the recorded build output: {name}")
    return errors


def collect_local(records: dict[str, Path], parent: Path):
    if set(records) != {"candidate", "computerUse"}:
        raise ValueError("Candidate and Computer Use raw evidence units are both required")
    root = fresh_directory(parent, "local-evidence")
    units = {}
    for name, path in records.items():
        record = read_json(path)
        errors = verify_manifest(path.parent, record)
        if errors:
            raise ValueError(f"Cannot import {name}: " + "; ".join(errors))
        unit_root = root / name
        unit_root.mkdir()
        for item in record["files"]:
            source = safe_file(path.parent, item["path"])
            target = unit_root / item["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
        shutil.copyfile(path, unit_root / path.name)
        units[name] = name + "/" + path.name
    identity = read_json(records["candidate"])["identity"]
    manifest = {"schemaVersion": 1, "kind": "local-evidence", "identity": identity, "units": units,
                "files": [file_record(root, path) for path in root.rglob("*") if path.is_file()]}
    write_json(root / "manifest.json", manifest)
    return root / "manifest.json", manifest


def pairs(values):
    result = {}
    for value in values:
        name, separator, item = value.partition("=")
        if not separator or not name or not item or name in result:
            raise ValueError("Expected distinct NAME=VALUE arguments")
        result[name] = item
    return result


SEMANTIC_FIXTURES = {"text", "pdf", "code", "illustration", "photo", "ocr-en", "ocr-zh",
                     "speech", "environment", "tone", "video-generated", "video-natural"}


@malformed_as_error
def validate_semantic(doc):
    """Validate extraction, real inference, persistence and retrieval, with no ranking gate."""
    errors = []
    check(errors, doc.get("schema") == 2 and doc.get("test") == "multimodal-functional"
          and doc.get("mode") == "functional" and doc.get("passed") is True,
          "Multimodal functional execution did not complete successfully")
    for field in ("realModel", "persisted", "reloaded"):
        check(errors, doc.get(field) is True, f"Multimodal functional execution did not prove {field}")
    for field in ("modelFingerprint", "extractionFingerprint"):
        check(errors, isinstance(doc.get(field), str) and bool(doc[field].strip()),
              f"Multimodal functional execution lacks {field}")
    for field, relative in (("fixtureManifestSha256", "tests/acceptance/fixtures/assets.json"),
                            ("generatorSha256", "tests/semantic/semantic_acceptance.cpp")):
        try:
            check(errors, doc.get(field) == digest(SOURCE / relative),
                  f"Multimodal functional execution used different {relative}")
        except OSError as error:
            errors.append(str(error))
    files = doc.get("files", [])
    check(errors, isinstance(files, list) and len(files) == len(SEMANTIC_FIXTURES)
          and {row.get("id") for row in files} == SEMANTIC_FIXTURES
          and doc.get("fixtures") == len(files) and doc.get("completedFiles") == len(files),
          "Multimodal functional fixtures are incomplete or repeated")
    by_id = {}
    source_files = set()
    segments = 0
    for row in files:
        name = row.get("file", "")
        relative = PurePosixPath(name)
        check(errors, isinstance(name, str) and name == relative.as_posix() and bool(relative.parts)
              and relative.parts[0] == row.get("id") and name not in source_files
              and not relative.is_absolute() and ".." not in relative.parts and "\\" not in name and ":" not in name,
              "Multimodal functional file path is not a safe relative path")
        source_files.add(name)
        modalities = row.get("extractedModalities")
        check(errors, integer_at_least(row.get("segments"), 1)
              and isinstance(modalities, dict) and bool(modalities)
              and set(modalities).issubset({"text", "image", "audio", "video"})
              and all(integer_at_least(count, 1) for count in modalities.values())
              and sum(modalities.values()) == row["segments"]
              and isinstance(row.get("sha256"), str) and re.fullmatch(r"[0-9a-f]{64}", row["sha256"]) is not None,
              "Multimodal functional file has no bound extraction and embedding output")
        by_id[row.get("id")] = name
        if integer_at_least(row.get("segments"), 1):
            segments += row["segments"]
    check(errors, doc.get("actualEmbeddedSegments") == segments and segments >= len(files),
          "Multimodal embedded segment count differs from its files")
    queries = doc.get("queries", [])
    expected_queries = {(fixture, language) for fixture in SEMANTIC_FIXTURES for language in ("en", "zh")}
    check(errors, isinstance(queries, list) and len(queries) == len(expected_queries)
          and {(row.get("fixtureId"), row.get("language")) for row in queries} == expected_queries
          and doc.get("queryCount") == len(queries) and doc.get("completedQueries") == len(queries),
          "Multimodal functional query coverage is incomplete or repeated")
    for row in queries:
        check(errors, row.get("id") == f"{row.get('fixtureId')}-{row.get('language')}"
              and isinstance(row.get("query"), str) and bool(row["query"].strip()),
              "Multimodal functional query identity or text is invalid")
        check(errors, row.get("source") == "semantic" and row.get("passed") is True
              and row.get("locationPassed") is True and row.get("returnedFile") == by_id.get(row.get("fixtureId")),
              "Multimodal production semantic retrieval did not return its source with valid location metadata")
    return errors


def local_preflight(local_root: Path, candidate_head: str):
    """Verify only the local final package and observed Windows UI session.

    CI artifact provenance is verified independently by qcs-release.py. This
    result never represents remote jobs or GPU execution that was not observed.
    """
    errors = []
    result = {"schemaVersion": 2, "kind": "local-release-preflight", "candidateHead": candidate_head,
              "localGates": {}, "passed": False, "errors": errors, "published": False}
    try:
        manifest = read_json(local_root / "manifest.json")
        errors.extend(verify_manifest(local_root, manifest))
        identity = manifest["identity"]
        check(errors, identity.get("candidateHead") == candidate_head and identity.get("checkoutCommit") == candidate_head,
              "Local evidence must execute the clean immutable candidate head")
        units = manifest["units"]
        check(errors, set(units) == {"candidate", "computerUse"}, "Required local evidence units are incomplete")
        candidate_path = safe_file(local_root, units["candidate"])
        candidate = read_json(candidate_path)
        failures = inspect_candidate(candidate_path.parent, candidate)
        check(failures, candidate.get("identity") == identity, "Local candidate package is unbound")
        result["localGates"]["candidate"] = {"passed": not failures, "errors": failures}
        errors.extend(failures)
        cu_path = safe_file(local_root, units["computerUse"])
        cu = read_json(cu_path)
        failures = verify_manifest(cu_path.parent, cu)
        failures.extend(validate_cu(cu, cu_path.parent, identity=identity,
            package_sha256=candidate["packageSha256"], model_fingerprint=candidate["modelFingerprint"]))
        cu_bound = {item["path"] for item in cu.get("files", [])}
        for scene in cu.get("scenarios", []):
            check(failures, scene.get("operationLog") in cu_bound and all(name in cu_bound for name in scene.get("screenshots", [])),
                  "Computer Use screenshot or operation log is not content-bound")
        check(failures, cu.get("executables") == candidate.get("executables")
              and cu.get("extractionFingerprint") == candidate.get("extractionFingerprint"),
              "Computer Use does not bind the same final packaged executables and extraction pipeline")
        result["localGates"]["computerUse"] = {"passed": not failures, "errors": failures}
        errors.extend(failures)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        errors.append(f"Local evidence is missing, stale or unbound: {error}")
    result["passed"] = not errors
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    validate = commands.add_parser("validate-local", help="Validate the final Windows package and observed UI evidence")
    validate.add_argument("--candidate-commit", required=True)
    validate.add_argument("--local-evidence-dir", required=True, type=Path)
    validate.add_argument("--output", required=True, type=Path)
    build = commands.add_parser("record-build")
    build.add_argument("--source", type=Path, default=SOURCE)
    build.add_argument("--output-root", type=Path, required=True)
    build.add_argument("--program", action="append", required=True, help="NAME=absolute executable path")
    build.add_argument("--working-directory", type=Path, help="Existing cwd for relative command paths")
    build.add_argument("command", nargs=argparse.REMAINDER)
    package = commands.add_parser("record-package")
    for name in ("build-record", "package", "verification", "output-root"):
        package.add_argument("--" + name, type=Path, required=True)
    package.add_argument("--model-fingerprint", required=True)
    package.add_argument("--extraction-fingerprint", required=True)
    collect = commands.add_parser("collect-local")
    for name in ("candidate", "computer-use", "output-root"):
        collect.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.action == "validate-local":
            if not re.fullmatch(r"[0-9a-f]{40}", args.candidate_commit):
                raise ValueError("The candidate must be a full immutable Git SHA")
            result = local_preflight(args.local_evidence_dir.resolve(), args.candidate_commit)
            write_json(args.output, result)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["passed"] else 1
        if args.action == "record-build":
            command = args.command[1:] if args.command[:1] == ["--"] else args.command
            if not command:
                raise ValueError("An actual command after -- is required")
            path, result = record_build(args.source.resolve(), args.output_root,
                {name: Path(value).resolve() for name, value in pairs(args.program).items()}, command,
                working_directory=args.working_directory)
        elif args.action == "record-package":
            path, result = record_package(args.build_record.resolve(), args.package.resolve(), args.verification.resolve(),
                args.output_root, args.model_fingerprint, args.extraction_fingerprint)
        else:
            path, result = collect_local({"candidate": args.candidate.resolve(),
                "computerUse": args.computer_use.resolve()}, args.output_root)
        print(json.dumps({"record": str(path), "kind": result["kind"], "passed": result.get("passed"),
            "note": "Captured provenance is not a release acceptance result"}, ensure_ascii=False, indent=2))
        return 0 if result.get("passed", True) else 1
    except (OSError, ValueError, KeyError, TypeError, AttributeError, subprocess.SubprocessError, zipfile.BadZipFile) as error:
        result = {"schemaVersion": 2, "kind": args.action, "passed": False, "errors": [str(error)], "published": False}
        if args.action == "validate-local":
            write_json(args.output, result)
        print(json.dumps(result, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
