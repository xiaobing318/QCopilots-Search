"""Package and verify one clean Windows candidate. No work without --execute.

Phase 1 deliberately leaves corresponding source materials deferred and never
claims the final deliverables gate. Finish with qcs-package-phase2.py.
"""
import argparse
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile

sys.dont_write_bytecode = True
for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="backslashreplace")
APPLICATIONS = {"qcs", "qcs-service", "qcs-worker", "qcs-cli"}
SOURCE = Path(__file__).resolve().parents[2]


def digest(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write(path, document):
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def load_tool(source, filename, module_name):
    spec = importlib.util.spec_from_file_location(module_name, source / "tools" / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def validate_external(path, source):
    """Disallow checkout writes, ancestor roots, and filesystem-root outputs."""
    if (path == source or source in path.parents or path in source.parents
            or path == Path(path.anchor)):
        raise ValueError("Generated output must be outside the checkout and below a volume root: " + str(path))


def validate_budget(reserve, peak, retained=0.0, post_reserve=None):
    values = (reserve, peak, retained) + (() if post_reserve is None else (post_reserve,))
    if not all(math.isfinite(value) for value in values):
        raise ValueError("Disk budgets must be finite numbers")
    if reserve < 3 or peak < 1 or retained < 0 or (post_reserve is not None and post_reserve < reserve):
        raise ValueError("Reserve must be at least 3 GiB, peak at least 1 GiB, and post-run reserve cannot be lower")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=SOURCE)
    parser.add_argument("--model-dir", type=Path)
    for name in ("build-root", "qt-root", "qt-prefix", "build-record", "semantic-report", "ctest-summary", "ctest-execution-record", "output-root"):
        parser.add_argument("--" + name, type=Path, required=True)
    for name in ("head", "tree", "semantic-sha256"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--cmake", default="cmake")
    parser.add_argument("--config", default="Release")
    parser.add_argument("--reserve-gib", type=float, default=3.0)
    parser.add_argument("--peak-additional-gib", type=float, default=4.0)
    parser.add_argument("--retained-additional-gib", type=float, default=0.8)
    parser.add_argument("--post-run-reserve-gib", type=float,
                        help="Required final free space, defaults to --reserve-gib")
    parser.add_argument("--build-record-sha256", required=True)
    parser.add_argument("--defer-source-bundle", action="store_true", required=True)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    if args.model_dir is None:
        args.model_dir = args.build_root / "models"
    if args.post_run_reserve_gib is None:
        args.post_run_reserve_gib = args.reserve_gib
    for key in ("source", "build_root", "qt_root", "qt_prefix", "model_dir", "build_record", "semantic_report", "ctest_summary", "ctest_execution_record", "output_root"):
        setattr(args, key, getattr(args, key).expanduser().resolve())
    for key, length in (("head", 40), ("tree", 40), ("semantic_sha256", 64), ("build_record_sha256", 64)):
        if not re.fullmatch("[0-9a-f]{" + str(length) + "}", getattr(args, key)):
            raise ValueError("Expected a full immutable hexadecimal " + key)
    validate_external(args.output_root, args.source)
    validate_external(args.build_root, args.source)
    if args.config not in {"Release", "Debug", "RelWithDebInfo", "MinSizeRel"}:
        raise ValueError("A supported CMake configuration is required")
    validate_budget(args.reserve_gib, args.peak_additional_gib,
                    args.retained_additional_gib, args.post_run_reserve_gib)
    return args


def validate_ctest_bindings(summary, execution, identity, build_sha256, semantic_sha256):
    """A partial or stale test run cannot be used to produce a candidate package."""
    registered = summary.get("registeredPrograms")
    if (summary.get("kind") != "local-full-ctest-summary" or summary.get("passed") is not True
            or summary.get("identity") != identity or type(registered) is not int or registered <= 0
            or summary.get("passedPrograms") != registered
            or not all(summary.get("sourceAndBinaryChecks", {}).get(name) is True
                       for name in ("sourceUnchanged", "binariesUnchanged", "runnerUnchanged", "buildRecordUnchanged"))):
        raise RuntimeError("Full CTest summary is absent, unsuccessful or from another candidate")
    if (execution.get("kind") != "full-ctest-execution" or execution.get("passed") is not True
            or execution.get("identity") != identity or execution.get("endIdentity") != identity
            or execution.get("buildRecordSha256") != build_sha256
            or execution.get("exitCode") != 0 or execution.get("caseEvidenceExitCode") != 0
            or execution.get("semanticPassed") is not True or execution.get("semanticValidationErrors") != []
            or execution.get("semanticReportSha256") != semantic_sha256
            or not all(execution.get(name) is True for name in (
                "sourceUnchanged", "binariesUnchanged", "runnerUnchanged", "buildRecordUnchanged"))):
        raise RuntimeError("Original CTest execution does not bind this build and successful semantic report")


def main(argv=None):
    args = parse_args(argv)
    if not args.execute:
        print(json.dumps({"preparedOnly": True, "head": args.head, "tree": args.tree,
            "buildRecord": str(args.build_record), "outputRoot": str(args.output_root),
            "ctestSummary": str(args.ctest_summary), "ctestExecutionRecord": str(args.ctest_execution_record),
            "steps": ["verify clean identity and receipt", "install", "ZIP", "ZIP full model verification",
                      "NSIS", "NSIS full model verification", "symbols and build hash comparison",
                      "source bundle DEFERRED until phase 2", "record-package and offline gate",
                      "retained CU extraction, no GUI launch", "final source, binary and package stability"],
            "reserveGiB": args.reserve_gib, "peakAdditionalGiB": args.peak_additional_gib,
            "postRunReserveGiB": args.post_run_reserve_gib,
            "retainedAdditionalGiB": args.retained_additional_gib}, indent=2))
        return 0
    if os.name != "nt":
        raise RuntimeError("This prepared ZIP/NSIS candidate orchestration targets Windows only")
    for required in (args.build_record, args.semantic_report, args.ctest_summary, args.ctest_execution_record):
        if not required.is_file():
            raise RuntimeError("Required immutable acceptance evidence is missing: " + str(required))

    source = args.source
    external = args.build_root
    build = external / args.config.lower()
    evidence = load_tool(source, "qcs-release-evidence.py", "qcopilots_clean_package_evidence")
    workflow = load_tool(source, "qcs-dev.py", "qcopilots_clean_package_workflow")
    bundle_tool = load_tool(source, "qcs-source-bundle.py", "qcopilots_clean_package_source_bundle")
    verifier = load_tool(source, "qcs-verify-package.py", "qcopilots_clean_package_verifier")
    workflow.assert_external(args.output_root)
    workflow.assert_external(external)
    reserve = int(args.reserve_gib * 1024 ** 3)
    additional = int(args.peak_additional_gib * 1024 ** 3)
    post_reserve = int(args.post_run_reserve_gib * 1024 ** 3)

    def disk_free():
        result = {}
        for path in (external, args.output_root):
            current = path
            while not current.exists():
                current = current.parent
            result[str(path)] = shutil.disk_usage(current).free
        return result

    admission = max(reserve + additional, post_reserve + int(args.retained_additional_gib * 1024 ** 3))
    if min(disk_free().values()) < admission:
        raise RuntimeError("Insufficient disk for the explicit additional peak plus free-space reserve")
    if digest(args.build_record) != args.build_record_sha256:
        raise RuntimeError("The explicitly bound build receipt changed")
    receipt = read(args.build_record)
    errors = evidence.verified_build(args.build_record.parent, receipt)
    if errors or receipt.get("passed") is not True:
        raise RuntimeError("Unverified build record: " + repr(errors))
    identity = evidence.source_identity(source)
    if identity != receipt["identity"] or identity["checkoutCommit"] != args.head or identity["sourceTree"] != args.tree:
        raise RuntimeError("Current clean source identity does not match the requested build/head/tree")
    if evidence.valid_identity(identity):
        raise RuntimeError("A clean immutable source identity is required")
    if digest(args.semantic_report) != args.semantic_sha256:
        raise RuntimeError("Semantic report hash differs from the explicitly supplied record")
    ctest_summary = read(args.ctest_summary)
    ctest_record = read(args.ctest_execution_record)
    validate_ctest_bindings(ctest_summary, ctest_record, identity,
                            digest(args.build_record), args.semantic_sha256)
    ctest_inputs = list(ctest_summary["rawEvidence"]) + [ctest_summary["semantic"]["rawReport"], ctest_summary["caseEvidence"]]
    ctest_inputs += list(ctest_summary.get("capturedInputFiles", []))
    ctest_files = {}
    for item in ctest_inputs:
        path = evidence.safe_file(args.ctest_summary.parent, item["path"])
        if path.stat().st_size != item["bytes"] or digest(path) != item["sha256"]:
            raise RuntimeError("Original CTest evidence changed: " + item["path"])
        ctest_files[path.resolve()] = item
    if args.ctest_execution_record not in ctest_files or args.semantic_report not in ctest_files:
        raise RuntimeError("Full CTest manifest must capture the supplied original execution and semantic reports")
    semantic = read(args.semantic_report)
    if semantic.get("passed") is not True:
        raise RuntimeError("Semantic acceptance is not successful")
    model_fingerprint = semantic["modelFingerprint"]
    extraction_fingerprint = semantic["extractionFingerprint"]
    before = {name: digest(build / "bin" / (name + ".exe")) for name in APPLICATIONS}
    if any(before[name] != receipt["programs"][name]["sha256"] for name in APPLICATIONS):
        raise RuntimeError("Main build binaries differ from the recorded build")
    tracked = subprocess.check_output(["git", "-C", str(source), "ls-files", "-z"]).decode("utf-8").split("\0")
    source_before = {name: digest(source / name) for name in tracked if name and (source / name).is_file()}
    args.output_root.mkdir(parents=True, exist_ok=True)
    run = Path(tempfile.mkdtemp(prefix="qcs-pkg-", dir=args.output_root))
    run_identity = (run.stat().st_dev, run.stat().st_ino)
    packages = run / "pkg"
    packages.mkdir()
    write(run / "source-before.json", source_before)
    shutil.copyfile(args.semantic_report, run / "semantic-quality.json")
    captured_ctest = run / "ctest-provenance"
    captured_ctest.mkdir()
    shutil.copyfile(args.ctest_summary, captured_ctest / "summary.json")
    for path, item in ctest_files.items():
        target = captured_ctest / item["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
    summary = {"kind": "clean-candidate-binary-package-execution", "passed": False,
        "phase1Passed": False, "deliverablesComplete": False, "sourceBundleStatus": "deferred",
        "pendingSteps": ["Run phase 2 to create and verify corresponding source materials. Capacity, CI and Computer Use require their own acceptance evidence"],
        "inputs": {key: str(value) if isinstance(value, Path) else value for key, value in vars(args).items()},
        "originalCtestFiles": [{"path": str(path), "bytes": path.stat().st_size, "sha256": item["sha256"]} for path, item in ctest_files.items()],
        "runnerPath": str(Path(__file__).resolve()), "identity": identity,
        "buildRecord": str(args.build_record), "buildRecordSha256": digest(args.build_record),
        "semanticSha256": args.semantic_sha256, "runnerSha256": digest(Path(__file__)),
        "ctestSummarySha256": digest(args.ctest_summary), "ctestExecutionRecordSha256": digest(args.ctest_execution_record),
        "binaryHashesBefore": before, "steps": [], "startedAt": time.time(), "cuLaunched": False,
        "reserveGiB": args.reserve_gib, "peakAdditionalGiB": args.peak_additional_gib,
        "postRunReserveGiB": args.post_run_reserve_gib, "retainedAdditionalGiB": args.retained_additional_gib,
        "initialFreeBytesByPath": disk_free()}
    write(run / "begin.json", summary)
    env = os.environ.copy()
    env.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1",
               QCOPILOTS_SEARCH_SOURCE_COMMIT=args.head)
    env["PATH"] = os.pathsep.join(map(str, (build / "bin", build / "lib", args.qt_prefix / "bin", external / "bootstrap/nsis"))) + os.pathsep + env.get("PATH", "")
    cmake = Path(shutil.which(args.cmake) or args.cmake).resolve()
    cpack = cmake.with_name("cpack.exe" if os.name == "nt" else "cpack")

    def command(label, arguments, timeout=1800):
        if min(disk_free().values()) < reserve:
            raise RuntimeError("Free-space reserve reached before " + label)
        print(label, flush=True)
        started = time.monotonic()
        with (run / (label + ".stdout.log")).open("w", encoding="utf-8") as stdout, (run / (label + ".stderr.log")).open("w", encoding="utf-8") as stderr:
            result = subprocess.run(list(map(str, arguments)), cwd=external, env=env,
                                    stdout=stdout, stderr=stderr, timeout=timeout)
        summary["steps"].append({"name": label, "command": list(map(str, arguments)),
            "exitCode": result.returncode, "elapsedSeconds": time.monotonic() - started})
        write(run / "summary.json", summary)
        if result.returncode:
            raise RuntimeError(label + " failed. Raw logs: " + str(run))

    verified_assets = {}
    try:
        command("install", [cmake, "--install", build, "--config", args.config, "--prefix", run / "stage"])
        distributions = {}
        for generator, suffix in (("ZIP", ".zip"), ("NSIS", ".exe")):
            command("package-" + generator.lower(), [cpack, "--config", build / "CPackConfig.cmake",
                "-C", args.config, "-G", generator, "-B", packages])
            found = list(packages.glob("qcs-*" + suffix))
            if len(found) != 1:
                raise RuntimeError("Expected exactly one fresh distribution for " + generator)
            package = found[0]
            report_dir = run / (generator.lower() + "-report")
            command("verify-" + generator.lower(), [sys.executable, "-B", source / "tools/qcs-verify-package.py",
                "--build-root", external, "--package-file", package, "--model-dir", args.model_dir, "--report-dir", report_dir])
            report = read(report_dir / "report.json")
            errors = evidence.validate_package_release(report, report_dir,
                model_fingerprint=model_fingerprint, extraction_fingerprint=extraction_fingerprint,
                platform_id="windows-x64", worker_sha256=before["qcs-worker"])
            if (errors or report.get("workingTreeModified") is not False or report.get("commit") != args.head
                    or report.get("packageSha256") != digest(package)):
                raise RuntimeError("Unbound or incomplete package evidence: " + repr(errors))
            distributions[generator] = {"path": str(package), "sha256": digest(package),
                "bytes": package.stat().st_size, "report": str(report_dir / "report.json")}
            verified_assets[str(package)] = distributions[generator]["sha256"]
            write(run / (generator.lower() + "-offline-gate.json"), {"passed": True, "errors": errors})
        summary["packages"] = distributions
        command("symbols", [sys.executable, "-B", source / "tools/qcs-package-symbols.py",
            "--manifest", build / f"qcs-symbol-inputs-{args.config}.json", "--output-dir", packages])
        symbol_zip, = packages.glob("qcs-symbols-*.zip")
        with zipfile.ZipFile(symbol_zip) as archive:
            manifest = json.loads(archive.read("qcs-symbols-manifest.json"))
            values = {item["name"]: item["unstrippedBinarySha256"] for item in manifest["programs"]}
            if len(manifest["programs"]) != 4 or values != before:
                raise RuntimeError("Symbol archive binaries do not match all four recorded build outputs")
            if set(archive.namelist()) != {item["path"] for item in manifest["files"]} | {"qcs-symbols-manifest.json"}:
                raise RuntimeError("Symbol archive file inventory mismatch")
            for item in manifest["files"]:
                data = archive.read(item["path"])
                if len(data) != item["bytes"] or hashlib.sha256(data).hexdigest() != item["sha256"]:
                    raise RuntimeError("Symbol content mismatch: " + item["path"])
        write(run / "symbol-build-binding.json", {"passed": True, "buildRecordSha256": summary["buildRecordSha256"], "programs": values})
        verified_assets[str(symbol_zip)] = read(run / "symbols.stdout.log")["sha256"]
        if digest(symbol_zip) != verified_assets[str(symbol_zip)]:
            raise RuntimeError("Symbol archive changed after generation and content verification")
        bundle_tool.refresh_package_checksums(packages, [Path(value["path"]) for value in distributions.values()] + [symbol_zip])
        shutil.copyfile(packages / "SHA256SUMS", run / "phase1-SHA256SUMS")
        command("record-package", [sys.executable, "-B", source / "tools/qcs-release-evidence.py", "record-package",
            "--build-record", args.build_record, "--package", distributions["ZIP"]["path"],
            "--verification", distributions["ZIP"]["report"], "--output-root", run / "candidate-records",
            "--model-fingerprint", model_fingerprint, "--extraction-fingerprint", extraction_fingerprint])
        candidate_path = Path(read(run / "record-package.stdout.log")["record"])
        errors = evidence.inspect_candidate(candidate_path.parent, read(candidate_path))
        write(run / "candidate-offline-gate.json", {"passed": not errors, "errors": errors})
        if errors:
            raise RuntimeError("Candidate capture validation failed: " + repr(errors))
        summary["candidateRecord"] = str(candidate_path)
        cu_root = run / "cu 解包"
        verifier.unpack(Path(distributions["ZIP"]["path"]), cu_root)
        matches = list(cu_root.rglob("qcs.exe"))
        if len(matches) != 1:
            raise RuntimeError("Expected exactly one retained GUI executable")
        for name, expected in before.items():
            if digest(matches[0].parent / (name + ".exe")) != expected:
                raise RuntimeError("Retained CU executable does not match candidate: " + name)
        summary["appBin"] = str(matches[0].parent)
        summary["guiExecutable"] = str(matches[0])
        summary["assets"] = [{"path": str(path), "bytes": path.stat().st_size, "sha256": digest(path)}
            for path in sorted(packages.glob("qcs-*")) if path.is_file() and path.suffix != ".sha256"]
        # These two paths were created under this invocation's fresh UUID root.
        # Keep original logs, packages and CU extraction. Never clean older runs.
        removed = []
        for transient in (run / "stage", packages / "_CPack_Packages"):
            if (workflow.is_redirect(run)
                    or (run.stat().st_dev, run.stat().st_ino) != run_identity):
                raise RuntimeError("Invocation directory changed before cleanup: " + str(run))
            resolved = transient.resolve()
            if transient.is_symlink() or run.resolve() not in resolved.parents:
                raise RuntimeError("Transient cleanup escapes this invocation: " + str(transient))
            if transient.exists():
                workflow.verify_plain_tree(transient)
                shutil.rmtree(transient)
                removed.append(str(transient))
        summary["removedOwnTransientPaths"] = removed
        summary["passed"] = True
    except Exception as error:
        summary["error"] = str(error)
    finally:
        source_after = {name: digest(source / name) for name in source_before if (source / name).is_file()}
        after = {name: digest(build / "bin" / (name + ".exe")) for name in APPLICATIONS}
        summary["sourceUnchanged"] = source_before == source_after and evidence.source_identity(source) == identity
        summary["binariesUnchanged"] = before == after
        summary["buildRecordUnchanged"] = digest(args.build_record) == summary["buildRecordSha256"]
        summary["semanticUnchanged"] = digest(args.semantic_report) == args.semantic_sha256
        summary["ctestEvidenceUnchanged"] = (digest(args.ctest_summary) == summary["ctestSummarySha256"]
            and digest(args.ctest_execution_record) == summary["ctestExecutionRecordSha256"]
            and all(digest(path) == item["sha256"] for path, item in ctest_files.items()))
        summary["runnerUnchanged"] = digest(Path(__file__)) == summary["runnerSha256"]
        summary["packagesUnchanged"] = all(digest(Path(item["path"])) == item["sha256"]
            for item in summary.get("packages", {}).values())
        summary["verifiedAssetsUnchanged"] = all(digest(Path(path)) == expected for path, expected in verified_assets.items())
        summary["freeBytesAfterByPath"] = disk_free()
        summary["diskReserveMet"] = min(summary["freeBytesAfterByPath"].values()) >= post_reserve
        summary["finishedAt"] = time.time()
        if not all(summary[name] for name in ("sourceUnchanged", "binariesUnchanged", "buildRecordUnchanged", "semanticUnchanged", "ctestEvidenceUnchanged", "runnerUnchanged", "packagesUnchanged", "verifiedAssetsUnchanged", "diskReserveMet")):
            summary["passed"] = False
        write(run / "source-after.json", source_after)
        summary["phase1Passed"] = summary["passed"]
        summary["passed"] = False
        artifacts = []
        for path in sorted(run.rglob("*")):
            if workflow.is_redirect(path):
                raise RuntimeError("A retained artifact is a symbolic link or junction: " + str(path))
            if path.is_file() and path.name not in {"summary.json", "phase1-artifacts.json"}:
                artifacts.append({"path": path.relative_to(run).as_posix(), "bytes": path.stat().st_size, "sha256": digest(path)})
        # Include nested summary.json evidence. Only the phase summary itself is mutable here.
        for path in sorted(run.rglob("summary.json")):
            if path != run / "summary.json":
                artifacts.append({"path": path.relative_to(run).as_posix(), "bytes": path.stat().st_size, "sha256": digest(path)})
        write(run / "phase1-artifacts.json", {"kind": "phase1-retained-artifacts", "files": artifacts})
        summary["retainedArtifacts"] = {"path": "phase1-artifacts.json", "bytes": (run / "phase1-artifacts.json").stat().st_size, "sha256": digest(run / "phase1-artifacts.json")}
        write(run / "summary.json", summary)
    print(json.dumps({"phase1Passed": summary["phase1Passed"], "passed": False, "deliverablesComplete": False, "sourceBundleStatus": "deferred", "summary": str(run / "summary.json"), "error": summary.get("error")}, ensure_ascii=False, indent=2))
    return 0 if summary["phase1Passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
