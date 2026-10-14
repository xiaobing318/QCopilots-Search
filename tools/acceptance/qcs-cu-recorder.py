#!/usr/bin/env python3
"""Record real Windows CU evidence. Never launches or controls an application.

Requires a package build record made when the frozen candidate was built.
It checks evidence integrity, not whether operator descriptions are truthful.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
import zipfile

sys.dont_write_bytecode = True
PRODUCT = "qcs"
GUI_EXECUTABLE = PRODUCT + "-gui"
NAMES = (GUI_EXECUTABLE, PRODUCT + "-service", PRODUCT + "-worker", PRODUCT + "-cli")
CASES = tuple(f"CU-{n:02d}" for n in range(1, 11))
LOCKS = ("dependencies.lock.json", "tools/qt-packages.lock.json", "tools/nsis-package.lock.json",
         "tools/qcs-source-packages.lock.json", "cmake/qcs-native-deps.cmake")


def now():
    return datetime.now(timezone.utc).isoformat()


def instant(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset().total_seconds() != 0:
        raise ValueError("UTC timestamp required")
    return parsed


def sha(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for data in iter(lambda: stream.read(1048576), b""):
            h.update(data)
    return h.hexdigest()


def read(path):
    path = Path(path)
    if path.stat().st_size > 32 * 1024 * 1024:
        raise ValueError(f"JSON too large: {path}")
    return json.loads(path.read_text(encoding="utf-8-sig"),
                      parse_constant=lambda x: (_ for _ in ()).throw(ValueError(x)))


def write(path, value, *, exclusive=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    if exclusive:
        with path.open("x", encoding="utf-8") as stream:
            stream.write(data)
        return
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False,
                                     prefix="qcs-write-", suffix=".tmp") as stream:
        stream.write(data)
        name = stream.name
    os.replace(name, path)


def safe(root, relative, *, exists=True):
    if not isinstance(relative, str) or not relative or "\\" in relative or ":" in relative:
        raise ValueError("Evidence path must be relative POSIX")
    parts = relative.split("/")
    if any(part in ("", ".", "..") for part in parts) or PurePosixPath(relative).is_absolute():
        raise ValueError(f"Unsafe evidence path: {relative}")
    root = Path(root).resolve()
    path = root.joinpath(*parts)
    if not path.resolve().is_relative_to(root):
        raise ValueError("Evidence path escapes root")
    for item in [path, *path.parents]:
        if item == root:
            break
        if item.is_symlink() or (hasattr(item, "is_junction") and item.is_junction()):
            raise ValueError("Evidence cannot use a symlink or junction")
    if exists and not path.is_file():
        raise ValueError(f"Missing evidence: {relative}")
    return path


def record(root, path):
    relative = Path(path).relative_to(root).as_posix()
    checked = safe(root, relative)
    return {"path": relative, "bytes": checked.stat().st_size, "sha256": sha(checked)}


def check_record(root, item):
    path = safe(root, item["path"])
    if path.stat().st_size != item["bytes"] or sha(path) != item["sha256"]:
        raise ValueError(f"Evidence changed: {item['path']}")
    return path


def image_file(path):
    with Path(path).open("rb") as stream:
        magic = stream.read(12)
    if not (magic.startswith(b"\x89PNG\r\n\x1a\n") or magic.startswith(b"\xff\xd8\xff")):
        raise ValueError(f"Expected actual PNG/JPEG screenshot: {path}")
    if Path(path).stat().st_size < 32:
        raise ValueError("Screenshot is empty or truncated")


def git(source, *args):
    return subprocess.check_output(["git", "-C", str(source), *args], encoding="utf-8").strip()


def identity(source, candidate):
    result = {"candidateHead": candidate, "checkoutCommit": git(source, "rev-parse", "HEAD"),
              "sourceTree": git(source, "rev-parse", "HEAD^{tree}"),
              "workingTreeModified": bool(git(source, "status", "--porcelain", "--untracked-files=normal")),
              "lockHashes": {name: sha(Path(source) / name) for name in LOCKS}}
    if result["workingTreeModified"] or result["checkoutCommit"] != candidate:
        raise ValueError("Candidate source must be clean and checked out at the exact frozen commit")
    if not re.fullmatch(r"[0-9a-f]{40}", candidate):
        raise ValueError("Candidate commit must be full lowercase 40-digit SHA")
    return result


def fingerprints(source):
    """Read the declarative source contracts, never infer a historical runtime value."""
    header = (Path(source) / "src/core/pipeline_manifest.h").read_text(encoding="utf-8")
    constants = dict(re.findall(r'inline constexpr char (\w+)\[\] = "([^"]+)";', header))
    result = {}
    for name in ("embedding", "extraction"):
        function = name + "PipelineContract"
        block = header.split("inline QJsonObject " + function + "()", 1)[1].split("\n}", 1)[0]
        contract = {}
        for match in re.finditer(r'\{QStringLiteral\("([^"]+)"\),\s*(?:QStringLiteral\("([^"]*)"\)|QString::fromLatin1\((\w+)\)|(\d+))\}', block):
            key, literal, constant, number = match.groups()
            contract[key] = literal if literal is not None else constants[constant] if constant else int(number)
        if not contract or len(contract) != block.count("{QStringLiteral("):
            raise ValueError("Pipeline contract syntax changed. Update recorder before accepting a candidate.")
        prefix_match = re.search(r'inline QString ' + name + r'PipelineFingerprint\(\).*?QStringLiteral\("([^"]+)"\)', header, re.S)
        if not prefix_match:
            raise ValueError("Missing pipeline fingerprint prefix")
        value = prefix_match.group(1) + ":" + hashlib.sha256(json.dumps(contract, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
        result["modelFingerprint" if name == "embedding" else "extractionFingerprint"] = value
    return result


def verify_binaries(package, app_dir, candidate):
    if sha(package) != candidate["packageSha256"]:
        raise ValueError("Candidate package SHA differs from its build record")
    executables = candidate["executables"]
    if set(executables) != set(NAMES):
        raise ValueError("Exactly four named QCopilots executables are required")
    result = {}
    with zipfile.ZipFile(package) as archive:
        for name in NAMES:
            matching = [item for item in archive.infolist() if not item.is_dir() and PurePosixPath(item.filename).name.lower() == name + ".exe"]
            if len(matching) != 1:
                raise ValueError(f"Package must contain exactly one {name}.exe")
            member = matching[0]
            if ".." in PurePosixPath(member.filename).parts or "\\" in member.filename or PurePosixPath(member.filename).is_absolute():
                raise ValueError("Unsafe ZIP executable member")
            hasher = hashlib.sha256()
            with archive.open(member) as stream:
                for data in iter(lambda: stream.read(1048576), b""):
                    hasher.update(data)
            actual = hasher.hexdigest()
            path = Path(app_dir) / (name + ".exe")
            if actual != executables[name]["sha256"] or sha(path) != actual:
                raise ValueError(f"Build record, package and extracted executable disagree: {name}")
            result[name] = {"sha256": actual, "bytes": path.stat().st_size,
                            "packageMember": member.filename, "absolutePath": str(path.resolve())}
    return result


def initialize(args):
    candidate = read(args.candidate_record)
    if candidate.get("kind") != "package-candidate" or candidate.get("schemaVersion") != 1:
        raise ValueError("Requires a schemaVersion 1 package-candidate build record, not a historical CU report")
    expected_identity = candidate["identity"]
    actual_identity = identity(args.source, expected_identity["candidateHead"])
    if expected_identity != actual_identity:
        raise ValueError("Build-time identity differs from the clean candidate source")
    if not isinstance(candidate.get("buildProvenance"), dict) or not candidate.get("recordedAt"):
        raise ValueError("Build record must retain its original buildProvenance and recordedAt")
    provenance = candidate["buildProvenance"]
    provenance_file = safe(args.candidate_record.resolve().parent, provenance["path"])
    if sha(provenance_file) != provenance["sha256"]:
        raise ValueError("Candidate build provenance hash mismatch")
    if instant(candidate["recordedAt"]) > instant(now()):
        raise ValueError("Build record is dated in the future")
    contracts = fingerprints(args.source)
    if any(candidate.get(key) != value for key, value in contracts.items()):
        raise ValueError("Candidate model/extraction contract fingerprints do not match its source")
    executable_records = verify_binaries(args.package, args.app_dir, candidate)
    native_platform = {"system": platform.system(), "architecture": "x86_64" if platform.machine().lower() in ("amd64", "x86_64") else platform.machine()}
    if native_platform != {"system": "Windows", "architecture": "x86_64"}:
        raise ValueError("This recorder requires a native Windows x64 CU run")
    parent = args.run_parent.resolve()
    source = args.source.resolve()
    if parent.is_relative_to(source):
        raise ValueError("CU runtime must be outside the source checkout")
    parent.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="qcs CU 验收 ", dir=parent))
    for folder in ("inputs", "evidence", "profile", "models", "samples", "exports"):
        (root / folder).mkdir()
    shutil.copyfile(args.candidate_record, root / "inputs/package-candidate.json")
    shutil.copyfile(provenance_file, root / "inputs/package-build-provenance.json")
    retained_provenance = [{"sourcePath": str(provenance_file), "sha256": provenance["sha256"], "retainedPath": "inputs/package-build-provenance.json"}]
    for item in candidate.get("files", []):
        original = check_record(args.candidate_record.resolve().parent, item)
        if original.resolve() == args.package.resolve():
            # The final release importer retains the large ZIP once in its package bundle.
            continue
        destination = safe(root, "inputs/package-evidence/" + item["path"], exists=False)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(original, destination)
        retained_provenance.append({"sourcePath": str(original), "sha256": item["sha256"], "retainedPath": destination.relative_to(root).as_posix()})
    shutil.copyfile(__file__, root / "inputs/qcs-cu-recorder.py")
    tracked = subprocess.check_output(["git", "-C", str(source), "ls-files", "-z"], encoding="utf-8").split("\0")
    source_files = [{"path": name, "bytes": (source / name).stat().st_size, "sha256": sha(source / name)} for name in tracked if name]
    write(root / "inputs/source-manifest.json", {"identity": actual_identity, "files": source_files}, exclusive=True)
    if args.fixture_manifest:
        shutil.copyfile(args.fixture_manifest, root / "inputs/fixture-source-manifest.json")
    state = {"schemaVersion": 1, "kind": "qcopilots-search-cu-run", "runId": str(uuid.uuid4()),
             "startedAt": now(), "identity": actual_identity, "source": str(source),
             "packagePath": str(args.package.resolve()), "packageSha256": candidate["packageSha256"],
             "appDirectory": str(args.app_dir.resolve()), "executables": candidate["executables"],
             "verifiedExecutables": executable_records, **contracts,
             "profileRoot": str(root / "profile"), "modelDirectory": str(root / "models"),
             "samplesDirectory": str(root / "samples"), "platform": native_platform,
             "candidateProvenance": retained_provenance,
             "fingerprintOrigin": "Build record verified against the exact clean source contracts. Not a runtime status observation.",
             "inputs": [record(root, path) for path in sorted((root / "inputs").rglob("*")) if path.is_file()]}
    write(root / "run.json", state, exclusive=True)
    print(json.dumps({"runRoot": str(root), "profileRoot": state["profileRoot"], "modelDirectory": state["modelDirectory"], "passed": False, "status": "initialized_no_ui_operations"}, ensure_ascii=False))


def load_run(root, *, verify=True):
    root = Path(root).resolve()
    state = read(root / "run.json")
    if state.get("kind") != "qcopilots-search-cu-run" or state.get("schemaVersion") != 1:
        raise ValueError("Not a recorder-owned run directory")
    if verify:
        for item in state["inputs"]:
            check_record(root, item)
        candidate = read(root / "inputs/package-candidate.json")
        if identity(state["source"], state["identity"]["candidateHead"]) != state["identity"]:
            raise ValueError("Source identity changed during CU run")
        current = verify_binaries(state["packagePath"], state["appDirectory"], candidate)
        if current != state["verifiedExecutables"] or candidate["executables"] != state["executables"]:
            raise ValueError("Candidate binaries changed during CU run")
    return root, state


def attempts(root, scenario):
    if scenario not in CASES:
        raise ValueError("Scenario must be CU-01 through CU-10")
    return sorted((root / "evidence" / scenario).glob("attempt-*/scenario.json"))


def active(root, scenario):
    found = attempts(root, scenario)
    if not found:
        raise ValueError("Begin the scenario before recording an observation")
    path = found[-1]
    row = read(path)
    if row["status"] != "running":
        raise ValueError("Latest attempt is already finished. Begin a new attempt to retest.")
    return path, row


def begin(args):
    root, state = load_run(args.run)
    found = attempts(root, args.scenario)
    if found and read(found[-1])["status"] == "running":
        raise ValueError("A scenario attempt is already running")
    expected = args.expected.strip()
    if not expected:
        raise ValueError("Expected behavior is required")
    folder = root / "evidence" / args.scenario / f"attempt-{len(found)+1:03d}"
    folder.mkdir(parents=True)
    row = {"id": args.scenario, "attempt": len(found)+1, "status": "running", "expected": expected,
           "startedAt": now(), "finishedAt": None, "actions": [], "actual": "", "screenshots": [],
           "operationLog": (folder / "operation-log.json").relative_to(root).as_posix(),
           "identity": state["identity"], "packageSha256": state["packageSha256"], "executables": state["executables"],
           "modelFingerprint": state["modelFingerprint"], "extractionFingerprint": state["extractionFingerprint"],
           "files": [], "operatorAttestation": None}
    write(folder / "operation-log.json", [], exclusive=True)
    write(folder / "scenario.json", row, exclusive=True)
    print(json.dumps({"scenario": args.scenario, "attempt": row["attempt"], "status": "running"}))


def observe(args):
    root, state = load_run(args.run)
    path, row = active(root, args.scenario)
    entry = read(args.entry)
    for key in ("action", "actual", "utc", "operator"):
        if not isinstance(entry.get(key), str) or not entry[key].strip():
            raise ValueError(f"Observation requires a nonempty {key}")
    if entry["operator"] not in ("computer-use", "user-assisted", "tool-observation"):
        raise ValueError("Record who actually performed the action")
    if not isinstance(entry.get("window"), dict) or not entry["window"].get("id"):
        raise ValueError("Observation requires the actual selected window identity")
    if not isinstance(entry.get("screenshots"), list) or not entry["screenshots"]:
        raise ValueError("An actual observation screenshot is required")
    logs = read(safe(root, row["operationLog"]))
    if not instant(row["startedAt"]) <= instant(entry["utc"]) <= instant(now()):
        raise ValueError("Observation is outside this scenario attempt")
    if logs and instant(entry["utc"]) < instant(logs[-1]["utc"]):
        raise ValueError("Observation time moved backwards")
    if entry.get("packageSha256") != state["packageSha256"]:
        raise ValueError("Observation must explicitly reference this candidate package SHA")
    screenshots = []
    for source in entry["screenshots"]:
        image_file(source)
    for index, source in enumerate(entry["screenshots"]):
        with Path(source).open("rb") as stream:
            extension = ".png" if stream.read(8) == b"\x89PNG\r\n\x1a\n" else ".jpg"
        dest = path.parent / f"observation-{len(logs)+1:04d}-{index+1:02d}{extension}"
        if dest.exists():
            raise ValueError("Observation screenshot already exists")
        shutil.copyfile(source, dest)
        item = record(root, dest)
        row["files"].append(item)
        screenshots.append(item["path"])
        row["screenshots"].append(item["path"])
    original = path.parent / f"observation-{len(logs)+1:04d}-source.json"
    shutil.copyfile(args.entry, original)
    row["files"].append(record(root, original))
    stored = dict(entry)
    stored["screenshots"] = screenshots
    stored["sourceRecord"] = original.relative_to(root).as_posix()
    stored["recordedAt"] = now()
    logs.append(stored)
    row["actions"].append(entry["action"])
    write(safe(root, row["operationLog"]), logs)
    write(path, row)
    print(json.dumps({"scenario": args.scenario, "observations": len(logs), "status": "running"}))


def finish(args):
    root, state = load_run(args.run)
    path, row = active(root, args.scenario)
    if not args.actual.strip():
        raise ValueError("Actual result must be recorded explicitly")
    for item in row["files"]:
        check_record(root, item)
    if args.status == "passed" and (not row["actions"] or not row["screenshots"]):
        raise ValueError("Cannot pass a scenario without actual observations")
    row.update(status=args.status, actual=args.actual.strip(), finishedAt=now(),
               operatorAttestation={"assessment": "Operator assessment of actual recorded actions, not automatically inferred from screenshots", "assessedAt": now()})
    if args.assistance:
        row["userAssistance"] = read(args.assistance)
    logs = read(safe(root, row["operationLog"]))
    if any(e["operator"] == "user-assisted" for e in logs) and not row.get("userAssistance"):
        raise ValueError("User-assisted operations require an explicit assistance record")
    row["files"].append(record(root, safe(root, row["operationLog"])))
    write(path, row)
    print(json.dumps({"scenario": args.scenario, "status": args.status}))


def summarize(args):
    root, state = load_run(args.run)
    errors = []
    selected = []
    for scenario in CASES:
        found = attempts(root, scenario)
        if not found:
            errors.append(f"{scenario}: not run")
            selected.append({"id": scenario, "status": "not_run"})
            continue
        row = read(found[-1])
        if row.get("id") != scenario:
            raise ValueError(f"Scenario identity differs from its directory: {scenario}")
        for item in row["files"]:
            check_record(root, item)
        for key in ("identity", "packageSha256", "executables", "modelFingerprint", "extractionFingerprint"):
            if row[key] != state[key]:
                raise ValueError(f"Mixed candidate evidence in {scenario}: {key}")
        if row["status"] != "passed":
            errors.append(f"{scenario}: {row['status']}")
        elif not row["actions"] or not row["actual"] or not row["screenshots"] or not row["finishedAt"]:
            raise ValueError(f"Incomplete passed scenario: {scenario}")
        ended = row["finishedAt"] or now()
        if not instant(state["startedAt"]) <= instant(row["startedAt"]) <= instant(ended) <= instant(now()):
            raise ValueError(f"Scenario timestamps escape this candidate session: {scenario}")
        operations = read(safe(root, row["operationLog"]))
        if len(operations) != len(row["actions"]):
            raise ValueError(f"Operation log does not match the recorded actions: {scenario}")
        bound_paths = {item["path"] for item in row["files"]}
        if len(bound_paths) != len(row["files"]) or (row["status"] != "running" and row["operationLog"] not in bound_paths):
            raise ValueError(f"Operation log is unbound or files contain duplicate paths: {scenario}")
        logged_images = [picture for operation in operations for picture in operation.get("screenshots", [])]
        if logged_images != row["screenshots"] or len(set(logged_images)) != len(logged_images):
            raise ValueError(f"Screenshot references do not match the operation log: {scenario}")
        if [operation.get("action") for operation in operations] != row["actions"]:
            raise ValueError(f"Action text differs from the operation log: {scenario}")
        for operation in operations:
            if operation["packageSha256"] != state["packageSha256"] or not instant(row["startedAt"]) <= instant(operation["utc"]) <= instant(ended):
                raise ValueError(f"Operation came from another candidate or time interval: {scenario}")
            if operation.get("sourceRecord") not in bound_paths:
                raise ValueError(f"Original operation JSON lacks a hash binding: {scenario}")
        for picture in row["screenshots"]:
            if picture not in bound_paths:
                raise ValueError(f"Screenshot lacks a hash binding: {scenario}")
            image_file(safe(root, picture))
        selected.append(row)
    finished = now()
    report = {"schemaVersion": 1, "kind": "windows-computer-use", "identity": state["identity"],
              "commit": state["identity"]["checkoutCommit"], "packageSha256": state["packageSha256"],
              "executables": state["executables"], "modelFingerprint": state["modelFingerprint"],
              "extractionFingerprint": state["extractionFingerprint"], "platform": state["platform"],
              "runId": state["runId"], "profileRoot": state["profileRoot"], "startedAt": state["startedAt"],
              "finishedAt": finished, "passed": not errors, "errors": errors, "scenarios": selected,
              "scope": "Exactly one frozen Windows candidate package. This report covers only the CU gate, not all release requirements.",
              "evidenceLimit": "Integrity checks cannot establish that operator-authored text is truthful. Actual Computer Use and user-assisted actions must be recorded honestly.",
              "userAssistedScenarios": [s["id"] for s in selected if s.get("userAssistance")]}
    tag = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    # The release importer roots every reference at the raw report's parent.
    # Keep this self-contained evidence unit at runRoot, not under exports/.
    destination = root / f"windows-computer-use-{tag}.json"
    files = [record(root, root / "run.json")]
    files.extend(record(root, path) for folder in ("inputs", "evidence") for path in sorted((root / folder).rglob("*")) if path.is_file())
    report["files"] = files
    write(destination, report, exclusive=True)
    manifest = {"schemaVersion": 1, "kind": "windows-computer-use-files", "identity": state["identity"],
                "report": destination.relative_to(root).as_posix(), "files": [*files, record(root, destination)], "createdAtUtc": finished}
    manifest_path = destination.with_name(destination.stem + "-files.json")
    write(manifest_path, manifest, exclusive=True)
    print(json.dumps({"report": str(destination), "filesManifest": str(manifest_path), "passed": report["passed"], "errors": errors}, ensure_ascii=False))
    return 0 if report["passed"] else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="Verify one frozen package and create empty isolated runtime directories")
    for name in ("candidate-record", "package", "app-dir", "source", "run-parent"):
        init.add_argument("--" + name, required=True, type=Path)
    init.add_argument("--fixture-manifest", type=Path)
    init.set_defaults(handler=initialize)
    for name, function in (("begin", begin), ("observe", observe), ("finish", finish), ("summarize", summarize)):
        command = commands.add_parser(name)
        command.add_argument("--run", required=True, type=Path)
        command.set_defaults(handler=function)
        if name != "summarize":
            command.add_argument("--scenario", choices=CASES, required=True)
        if name == "begin":
            command.add_argument("--expected", required=True)
        elif name == "observe":
            command.add_argument("--entry", required=True, type=Path)
        elif name == "finish":
            command.add_argument("--status", choices=("passed", "failed", "not_run", "unsupported"), required=True)
            command.add_argument("--actual", required=True)
            command.add_argument("--assistance", type=Path)
    args = parser.parse_args(argv)
    try:
        return args.handler(args) or 0
    except (OSError, ValueError, KeyError, IndexError, zipfile.BadZipFile, subprocess.CalledProcessError) as error:
        print(json.dumps({"passed": False, "error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
