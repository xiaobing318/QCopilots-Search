#!/usr/bin/env python3
"""Snapshot fixture inputs into a plan, then copy that plan into a fresh CU run.

Copying is a dry-run unless --execute is supplied. This tool never starts an
application, modifies fixture sources or run.json, or declares CU acceptance.
The source root contains samples/ and optional negative-models/ directories.
Keep license notices under negative-models/fixture-licenses so search does not
index them. Provide a fresh canary in the source fixture set when testing edits.
Paths are canonicalized after link checks. The only linked ancestors allowed
are macOS's standard /var and /tmp aliases to /private/var and /private/tmp.
Fixture roots, fixture files, run roots and paths below them must not be links.
"""
from __future__ import annotations

import sys

sys.dont_write_bytecode = True

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import uuid

MANIFEST = "negative-models/qcs-fixture-copy.json"
FAILURE = "negative-models/qcs-fixture-copy-failure.json"
ROOTS = {"samples", "negative-models"}
SHA256 = re.compile(r"[0-9a-f]{64}")
RESERVED = re.compile(r"(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", re.IGNORECASE)


def require(value, message):
    if not value:
        raise ValueError(message)


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _system_alias(path):
    """Recognize exact OS aliases, never arbitrary user-controlled links."""
    if sys.platform != "darwin":
        return False
    canonical = {"/var": "/private/var", "/tmp": "/private/tmp"}.get(path.as_posix())
    return canonical is not None and path.resolve().as_posix() == canonical


def unlinked(path):
    path = Path(path).absolute()
    for part in (path, *path.parents):
        linked = part.is_symlink() or (hasattr(part, "is_junction") and part.is_junction())
        require(not linked or (part != path and _system_alias(part)),
                "Linked path is not permitted: " + str(part))
    return path.resolve()


def relative_parts(relative):
    require(isinstance(relative, str) and ":" not in relative and "\\" not in relative,
            "Unsafe fixture path")
    parts = relative.split("/")
    require(not PurePosixPath(relative).is_absolute() and len(parts) >= 2
            and parts[0] in ROOTS and all(part not in {"", ".", ".."} for part in parts),
            "Fixture path outside permitted roots")
    require(all(not part.endswith((" ", ".")) and not RESERVED.fullmatch(part)
                and not any(ord(char) < 32 or char in '<>"|?*' for char in part)
                for part in parts), "Fixture path is not portable")
    require(relative.casefold() not in {MANIFEST.casefold(), FAILURE.casefold()},
            "Fixture path collides with the copy report")
    return parts


def target(root, relative):
    value = unlinked(root.joinpath(*relative_parts(relative)))
    require(value.is_relative_to(root) and value != root, "Fixture resolves outside root")
    return value


def report_target(root, relative):
    # Report names are reserved against input files, but have the same boundary.
    value = unlinked(root / relative)
    require(value.is_relative_to(root) and value != root, "Report resolves outside run")
    return value


def check_file(path, size, expected):
    require(type(size) is int and size >= 0 and isinstance(expected, str)
            and SHA256.fullmatch(expected), "Invalid file size or SHA-256")
    path = unlinked(path)
    require(path.is_file() and path.stat().st_size == size, "File size differs: " + str(path))
    require(sha(path) == expected, "File SHA-256 differs: " + str(path))
    return {"path": str(path), "bytes": size, "sha256": expected}


def empty(path, required=True):
    path = unlinked(path)
    require(path.is_dir() if required else not path.exists() or path.is_dir(),
            "Expected fixture directory: " + str(path))
    require(not path.exists() or not any(path.iterdir()),
            "Refusing an already nonempty directory: " + str(path))


def write_json(path, value):
    with path.open("x", encoding="utf-8", newline="\n") as output:
        json.dump(value, output, ensure_ascii=False, indent=2)
        output.write("\n")


def create_plan(source_root, plan_path, excluded_relative=None):
    """Hash current fixtures, without embedding any candidate or historical run."""
    source_root, plan_path = unlinked(source_root), unlinked(plan_path)
    require(source_root.is_dir(), "Fixture source root does not exist")
    require(not plan_path.is_relative_to(source_root), "Write the plan outside the read-only fixture root")
    files = []
    for name in sorted(ROOTS):
        subtree = source_root / name
        if not subtree.exists():
            continue
        require(unlinked(subtree).is_dir(), "Fixture subtree is not a directory")
        for path in sorted(subtree.rglob("*")):
            unlinked(path)
            if path.is_dir():
                continue
            require(path.is_file(), "Fixture source is not a regular file")
            relative = path.relative_to(source_root).as_posix()
            relative_parts(relative)
            size = path.stat().st_size
            # Negative model fixtures may be deliberately invalid tiny GGUFs.
            # Real model weights belong in the recorder's model workflow.
            require(path.suffix.lower() != ".gguf" or size <= 1024 * 1024,
                    "Real model files must not be copied as CU fixtures")
            files.append({"sourceRelativePath": relative, "targetRelativePath": relative,
                          "bytes": size, "sha256": sha(path),
                          "role": "sample" if name == "samples" else "negative-or-license"})
    require(any(row["targetRelativePath"].startswith("samples/") for row in files),
            "At least one sample fixture is required")
    require(len({row["targetRelativePath"].casefold() for row in files}) == len(files),
            "Case-insensitive fixture destination collision")
    if excluded_relative is not None:
        excluded = target(source_root, excluded_relative)
        require(excluded_relative.startswith("samples/") and excluded.is_dir(),
                "Excluded fixture directory must exist inside samples")
    plan = {"schemaVersion": 1, "kind": "qcopilots-search-cu-fixture-copy-plan",
            "sourceRoot": str(source_root), "files": files,
            "excludedRelativePath": excluded_relative}
    write_json(plan_path, plan)
    return {"plan": str(plan_path), "planSha256": sha(plan_path), "files": len(files)}


def prepare(run_path, plan_path, source_root=None):
    run_path, plan_path = unlinked(run_path), unlinked(plan_path)
    require(run_path.name == "run.json", "Supply the recorder-created run.json")
    root = run_path.parent
    state = json.loads(run_path.read_text(encoding="utf-8-sig"))
    require(state.get("schemaVersion") == 1 and state.get("kind") == "qcopilots-search-cu-run",
            "Not a recorder CU run")
    uuid.UUID(state["runId"])
    require({path.name for path in root.iterdir()} <=
            {"run.json", "inputs", "evidence", "profile", "models", "samples", "exports", "negative-models"},
            "Run already contains post-initialization artifacts")
    identity = state["identity"]
    require(re.fullmatch("[0-9a-f]{40}", identity["candidateHead"])
            and identity["candidateHead"] == identity["checkoutCommit"]
            and identity["workingTreeModified"] is False, "CU run candidate identity is not clean")
    for key, relative in (("samplesDirectory", "samples"), ("profileRoot", "profile"), ("modelDirectory", "models")):
        require(unlinked(state[key]) == unlinked(root / relative), "Recorder runtime path differs: " + key)
    for relative in ("samples", "profile", "models", "evidence", "exports"):
        empty(root / relative)
    empty(root / "negative-models", False)
    candidate_path = unlinked(root / "inputs/package-candidate.json")
    candidates = [item for item in state["inputs"] if item["path"] == "inputs/package-candidate.json"]
    require(len(candidates) == 1, "Recorder does not bind its package candidate input")
    check_file(candidate_path, candidates[0]["bytes"], candidates[0]["sha256"])
    candidate = json.loads(candidate_path.read_text(encoding="utf-8-sig"))
    require(candidate.get("kind") == "package-candidate" and candidate["identity"] == identity
            and candidate["packageSha256"] == state["packageSha256"], "Recorder candidate provenance differs")
    plan = json.loads(plan_path.read_text(encoding="utf-8-sig"))
    require(plan.get("schemaVersion") == 1 and plan.get("kind") == "qcopilots-search-cu-fixture-copy-plan"
            and isinstance(plan.get("files"), list) and plan["files"], "Invalid fixture copy plan")
    source_root = unlinked(source_root if source_root is not None else plan["sourceRoot"])
    require(source_root.is_dir() and not root.is_relative_to(source_root) and not source_root.is_relative_to(root),
            "Fixture source and new run must be separate directories")
    excluded_relative = plan.get("excludedRelativePath")
    if excluded_relative is not None:
        require(excluded_relative.startswith("samples/") and target(source_root, excluded_relative).is_dir(),
                "Excluded fixture directory must exist inside samples")
    rows = []
    for item in plan["files"]:
        relative = item["targetRelativePath"]
        destination = target(root, relative)
        require(not destination.exists(), "Fixture destination already exists: " + str(destination))
        source = target(source_root, item["sourceRelativePath"])
        source_record = check_file(source, item["bytes"], item["sha256"])
        require(source.suffix.lower() != ".gguf" or item["bytes"] <= 1024 * 1024,
                "Real model files must not be copied as CU fixtures")
        rows.append({"source": source_record, "targetPath": str(destination), "targetRelativePath": relative,
                     "expectedBytes": item["bytes"], "expectedSha256": item["sha256"], "role": item["role"]})
    require(len({row["targetRelativePath"].casefold() for row in rows}) == len(rows), "Duplicate destination names")
    require(any(row["targetRelativePath"].startswith("samples/") for row in rows), "At least one sample is required")
    return root, state, rows, excluded_relative


def materialize(run_path, plan_path, execute=False, source_root=None):
    run_sha, plan_sha = sha(unlinked(run_path)), sha(unlinked(plan_path))
    root, state, rows, excluded_relative = prepare(run_path, plan_path, source_root)
    require(sha(run_path) == run_sha and sha(plan_path) == plan_sha, "Run or source plan changed during validation")
    report = {"schemaVersion": 1, "kind": "qcopilots-search-cu-fixture-copy",
              "executed": execute, "complete": False, "runId": state["runId"], "identity": state["identity"],
              "packageSha256": state["packageSha256"], "modelFingerprint": state["modelFingerprint"],
              "extractionFingerprint": state["extractionFingerprint"], "runJsonSha256": run_sha,
              "planPath": str(Path(plan_path).resolve()), "planSha256": plan_sha, "filesToCopy": len(rows),
              "sampleRoot": str(root / "samples"),
              "excludedRoot": str(root / excluded_relative) if excluded_relative else None,
              "negativeModelDirectory": str(root / "negative-models"), "manifestRelativePath": MANIFEST,
              "files": rows, "acceptancePassed": False}
    if not execute:
        return report
    written = []
    try:
        for row in rows:
            destination = target(root, row["targetRelativePath"])
            source = Path(row["source"]["path"])
            check_file(source, row["source"]["bytes"], row["source"]["sha256"])
            destination.parent.mkdir(parents=True, exist_ok=True)
            target(root, row["targetRelativePath"])
            with source.open("rb") as incoming, destination.open("xb") as outgoing:
                shutil.copyfileobj(incoming, outgoing, 1024 * 1024)
            row["verifiedTarget"] = check_file(destination, row["expectedBytes"], row["expectedSha256"])
            written.append(row["targetRelativePath"])
        require(sha(run_path) == run_sha and sha(plan_path) == plan_sha, "Run or source plan changed during copying")
        for row in rows:
            check_file(row["source"]["path"], row["source"]["bytes"], row["source"]["sha256"])
            check_file(row["targetPath"], row["expectedBytes"], row["expectedSha256"])
        report.update({"complete": True, "finishedAt": datetime.now(timezone.utc).isoformat(),
                       "sourceFilesUnchanged": True, "verifiedFileCount": len(rows)})
        manifest = report_target(root, MANIFEST)
        manifest.parent.mkdir(exist_ok=True)
        report_target(root, MANIFEST)
        write_json(manifest, report)
        return {"executed": True, "complete": True, "acceptancePassed": False, "files": len(rows),
                "manifest": str(manifest), "manifestSha256": sha(manifest), "runRoot": str(root)}
    except BaseException as error:
        report.update({"complete": False, "error": type(error).__name__ + ": " + str(error), "completedFiles": written})
        try:
            failure = report_target(root, FAILURE)
            failure.parent.mkdir(exist_ok=True)
            report_target(root, FAILURE)
            write_json(failure, report)
        except (OSError, ValueError) as record_error:
            error.add_note("Could not preserve failure report: " + str(record_error))
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-json", type=Path)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, help="Fixture root, or override for a relocated plan")
    parser.add_argument("--excluded-relative", help="Optional source-relative samples directory to exclude")
    parser.add_argument("--create-plan", action="store_true")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if args.create_plan:
        if args.source_root is None or args.run_json is not None or args.execute:
            parser.error("--create-plan requires --source-root and forbids --run-json/--execute")
        result = create_plan(args.source_root, args.plan, args.excluded_relative)
    else:
        if args.run_json is None or args.excluded_relative is not None:
            parser.error("Copying requires --run-json and forbids --excluded-relative")
        result = materialize(args.run_json, args.plan, args.execute, args.source_root)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, TypeError) as error:
        print("Fixture copy failed: " + str(error), file=sys.stderr)
        raise SystemExit(2) from error
