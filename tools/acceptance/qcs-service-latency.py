"""Explicitly scheduled, real packaged CLI -> IPC -> service -> query-worker timing.

This portable evidence runner does not alter application source. Without
--execute it only emits or verifies a read-only plan. Schedule after final
package Computer Use, before million-vector acceptance, with exclusive resources.
"""
import argparse
from collections import defaultdict, deque
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import zipfile

sys.dont_write_bytecode = True
from qcs_owned_processes import WindowsProcesses


APPLICATIONS = {"qcs", "qcs-service", "qcs-worker", "qcs-cli"}
CREATE_SUSPENDED = 0x00000004  # Windows flag, not exported by every Python subprocess build.


def contained_file(root, relative):
    member = PurePosixPath(relative)
    if member.is_absolute() or not member.parts or any(part in (".", "..") or ":" in part for part in member.parts):
        raise RuntimeError(f"Invalid relative candidate path: {relative}")
    path = root.joinpath(*member.parts).resolve()
    if not path.is_relative_to(root.resolve()):
        raise RuntimeError(f"Candidate path escapes its evidence directory: {relative}")
    return path


def resolve_candidate(candidate_path, candidate, app_dir, semantic):
    if candidate.get("kind") != "package-candidate" or set(candidate.get("executables", {})) != APPLICATIONS:
        raise RuntimeError("A new record-package candidate containing all four application identities is required")
    for key in ("modelFingerprint", "extractionFingerprint"):
        if candidate.get(key) != semantic.get(key):
            raise RuntimeError(f"The candidate and semantic evidence disagree on {key}")
    if candidate["identity"].get("workingTreeModified") is not False:
        raise RuntimeError("The candidate must identify an unmodified checkout")
    suffix = ".exe" if os.name == "nt" else ""
    members = {name: PurePosixPath(record["packageMember"]) for name, record in candidate["executables"].items()}
    if any(member.name != name + suffix for name, member in members.items()) or len({str(member.parent) for member in members.values()}) != 1:
        raise RuntimeError("Candidate executable names or common directory are invalid")
    member_bin = next(iter(members.values())).parent
    for member in members.values():
        contained_file(candidate_path.parent, str(member))  # Validate even though members are inside the archive.
    package = contained_file(candidate_path.parent, candidate["package"])
    build_record = contained_file(candidate_path.parent, candidate["buildProvenance"]["path"])
    package_bin = None
    if app_dir is not None:
        app_dir = app_dir.resolve()
        choices = [directory for directory in (app_dir, app_dir / "bin") if (directory / ("qcs-cli" + suffix)).is_file()]
        if len(choices) != 1:
            raise RuntimeError("--app-dir must identify one extracted package root or its bin directory")
        package_bin = choices[0]
    return {"archive": package, "buildRecord": build_record, "memberBin": member_bin, "packageBin": package_bin}


def verify_candidate_inputs(candidate_path, candidate, layout, semantic, corpus):
    package_bin = layout["packageBin"]
    if package_bin is None:
        raise RuntimeError("--app-dir is required to verify or execute against a deployed package")
    if sha256(layout["archive"]) != candidate["packageSha256"]:
        raise RuntimeError("The recorded candidate archive has changed")
    if sha256(layout["buildRecord"]) != candidate["buildProvenance"]["sha256"]:
        raise RuntimeError("The recorded build provenance has changed")
    build = json.loads(layout["buildRecord"].read_text(encoding="utf-8-sig"))
    if not build.get("passed") or build.get("identity") != candidate["identity"]:
        raise RuntimeError("Candidate build provenance does not match its identity")
    member_bin = layout["memberBin"]
    # Resolve the extraction container from the known common binary directory.
    # Every archive member, including DLLs/plugins/resources, must match the deployed layout.
    extraction_root = package_bin
    for _ in member_bin.parts:
        extraction_root = extraction_root.parent
    deployed = {}
    with zipfile.ZipFile(layout["archive"]) as archive:
        names = set()
        for item in archive.infolist():
            if item.is_dir():
                continue
            if item.filename in names:
                raise RuntimeError("Duplicate archive member")
            names.add(item.filename)
            path = contained_file(extraction_root, item.filename)
            if not path.is_file() or path.stat().st_size != item.file_size:
                raise RuntimeError(f"Missing or changed deployed package file: {path}")
            with archive.open(item) as stream:
                archive_hash = hashlib.file_digest(stream, "sha256").hexdigest()
            if sha256(path) != archive_hash:
                raise RuntimeError(f"Deployed package differs from the candidate archive: {path}")
            deployed[item.filename] = {"path": str(path), "sha256": archive_hash, "bytes": item.file_size}
    for name, record in candidate["executables"].items():
        if deployed.get(record["packageMember"], {}).get("sha256") != record["sha256"] or build.get("programs", {}).get(name, {}).get("sha256") != record["sha256"]:
            raise RuntimeError(f"Candidate executable differs from its build or archive: {name}")
    if not corpus.is_dir():
        raise RuntimeError("Preserved semantic corpus is missing")
    corpus_hashes = {}
    for file in semantic["files"]:
        path = contained_file(corpus, file["file"])
        if not path.is_file() or sha256(path) != file["sha256"]:
            raise RuntimeError(f"Preserved corpus changed or is missing: {path}")
        corpus_hashes[str(path)] = file["sha256"]
    return {"candidateSha256": sha256(candidate_path), "archiveSha256": candidate["packageSha256"],
            "buildRecordSha256": candidate["buildProvenance"]["sha256"], "deployedFiles": deployed,
            "corpusFiles": corpus_hashes}


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def write_json(path, data):
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def load_evidence_api(source):
    path = source / "tools/qcs-release-evidence.py"
    spec = importlib.util.spec_from_file_location("qcopilots_search_latency_evidence", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module, path


def verify_provenance(evidence, candidate_path, candidate, source, expected_commit):
    if not re.fullmatch(r"[0-9a-f]{40}", expected_commit):
        raise RuntimeError("--expected-commit must be the new candidate's full immutable Git SHA")
    errors = evidence.inspect_candidate(candidate_path.parent, candidate)
    identity = evidence.source_identity(source)
    errors.extend(evidence.valid_identity(identity))
    if identity != candidate.get("identity") or identity.get("checkoutCommit") != expected_commit:
        errors.append("Current clean source does not match the explicit final candidate")
    if errors:
        raise RuntimeError("Candidate provenance rejected: " + "; ".join(errors))
    return identity


def check_working_directory(path):
    path = path.resolve()
    if not path.is_dir() or (os.name == "nt" and len(str(path).encode("utf-16-le")) // 2 > 258):
        raise RuntimeError("An existing Windows-compatible short --working-directory is required")
    return path


def verify_fingerprints(semantic, candidate):
    if not semantic.get("passed"):
        raise RuntimeError("A successful full semantic report is required")
    for key, prefix in (("extractionFingerprint", "extraction"), ("modelFingerprint", "embedding")):
        value = semantic.get(key, "")
        if not re.fullmatch(r"qcopilots-search-" + prefix + r"-v[1-9][0-9]*:[0-9a-f]{64}", value):
            raise RuntimeError(f"Invalid {key} in semantic report")
        if candidate.get(key) != value:
            raise RuntimeError(f"The new candidate and semantic report disagree on {key}")


def capture_inputs(run, candidate_path, candidate, semantic_path, semantic, corpus, evidence_path, reserve_gib=3.0):
    """Copy small provenance/raw inputs, never the ZIP, model cache or source bundle."""
    copies = [(Path(__file__), "inputs/runner.py"),
              (Path(__file__).with_name("qcs_owned_processes.py"), "inputs/qcs_owned_processes.py"),
              (evidence_path, "inputs/release-evidence-api.py"),
              (candidate_path, "inputs/package-candidate.json"),
              (semantic_path, "inputs/semantic-report.json")]
    external = []
    for item in candidate["files"]:
        path = PurePosixPath(item["path"])
        if item["path"] != candidate["package"] and (path.parts[0] == "build" or path.suffix.lower() in {".json", ".log", ".txt", ".md"}) and item["bytes"] <= 32 * 1024 * 1024:
            copies.append((contained_file(candidate_path.parent, item["path"]), "inputs/candidate-materials/" + item["path"]))
        else:
            external.append({"path": item["path"], "bytes": item["bytes"], "sha256": item["sha256"],
                             "scope": "Retained in the final candidate archive, not duplicated in this latency bundle"})
    for item in semantic["files"]:
        copies.append((contained_file(corpus, item["file"]), "inputs/corpus/" + item["file"]))
    budget = sum(path.stat().st_size for path, _ in copies) + 256 * 1024 * 1024
    if shutil.disk_usage(run).free <= reserve_gib * 1024**3 + budget:
        raise RuntimeError("Cannot preserve the configured free-space reserve while capturing latency inputs and an isolated index")
    records = []
    for source, relative in copies:
        target = contained_file(run, relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        if sha256(target) != sha256(source):
            raise RuntimeError("Evidence input changed while being captured")
        records.append({"path": relative, "bytes": target.stat().st_size, "sha256": sha256(target)})
    write_json(run / "input-manifest.json", {"files": records, "externalArtifactBindings": external,
               "packageSha256": candidate["packageSha256"], "modelWeightsCopied": False,
               "sourceBundlesCopied": False, "capturedBytes": sum(item["bytes"] for item in records)})
    return records, external


def finalize_manifest(run):
    records = []
    for path in sorted(run.rglob("*")):
        relative = path.relative_to(run).as_posix()
        if not path.is_file() or path.is_symlink() or relative.startswith("profile ") or relative == "manifest.json":
            continue
        records.append({"path": relative, "bytes": path.stat().st_size, "sha256": sha256(path)})
    write_json(run / "manifest.json", {"schemaVersion": 1, "kind": "packaged-cli-ipc-latency-evidence", "files": records,
               "profileExcluded": True, "externalArtifactBindings": "input-manifest.json"})


def captured_command(argv, working_directory, environment, flags, logs, number, timeout):
    logs.mkdir(exist_ok=True)
    stdout_path, stderr_path = logs / f"{number:04d}.stdout.bin", logs / f"{number:04d}.stderr.bin"
    record = {"argv": argv, "cwd": str(working_directory),
              "stdout": stdout_path.relative_to(logs.parent).as_posix(),
              "stderr": stderr_path.relative_to(logs.parent).as_posix()}
    start = time.perf_counter_ns()
    try:
        completed = subprocess.run(argv, cwd=working_directory, env=environment, capture_output=True,
                                   timeout=timeout, creationflags=flags)
        elapsed = (time.perf_counter_ns() - start) / 1_000_000
    except subprocess.TimeoutExpired as error:
        record.update(wallMilliseconds=(time.perf_counter_ns() - start) / 1_000_000, exitCode=None, timedOut=True)
        stdout_path.write_bytes(error.output or b"")
        stderr_path.write_bytes(error.stderr or b"")
        record.update(stdoutSha256=sha256(stdout_path), stderrSha256=sha256(stderr_path))
        error.evidence_record = record
        raise
    stdout_path.write_bytes(completed.stdout)
    stderr_path.write_bytes(completed.stderr)
    record.update(wallMilliseconds=elapsed, exitCode=completed.returncode,
                  stdoutSha256=sha256(stdout_path), stderrSha256=sha256(stderr_path))
    return completed, record


def percentiles(samples):
    samples = sorted(samples)
    return {
        "count": len(samples), "unit": "ms", "method": "nearest rank, ceil(p*N)",
        "p50": samples[math.ceil(.50 * len(samples)) - 1],
        "p95": samples[math.ceil(.95 * len(samples)) - 1],
        "p99": samples[math.ceil(.99 * len(samples)) - 1],
        "minimum": samples[0], "maximum": samples[-1], "mean": statistics.mean(samples),
    }


def choose_queries(queries, count=50):
    # The order and relevance labels are fixed by the existing corpus report.
    # Round-robin modalities prevents the dominant text group from hiding media.
    groups = defaultdict(deque)
    seen = set()
    for query in queries:
        if query["query"] in seen:
            continue
        seen.add(query["query"])
        groups[query["modality"]].append(query)
    chosen = []
    while len(chosen) < count:
        advanced = False
        for name in sorted(groups):
            if groups[name] and len(chosen) < count:
                chosen.append(groups[name].popleft())
                advanced = True
        if not advanced:
            raise RuntimeError("Not enough distinct labelled corpus queries")
    return chosen


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--semantic-report", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--corpus", type=Path, help="Relocated semantic corpus. Defaults to preservedRoot/corpus from the report.")
    parser.add_argument("--reserve-gib", type=float, default=3.0, help="Minimum free disk space to preserve during execution.")
    parser.add_argument("--expected-commit", required=True)
    parser.add_argument("--working-directory", type=Path, required=True, help="Existing short parent for a unique per-run working directory.")
    parser.add_argument("--package-candidate", type=Path, required=True)
    parser.add_argument("--app-dir", type=Path, help="Already extracted final package root or its bin directory. Required for --execute/--verify-inputs.")
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--max-run-seconds", type=int, default=3600)
    parser.add_argument("--execute", action="store_true", help="Only in the explicitly granted exclusive execution window")
    parser.add_argument("--verify-inputs", action="store_true", help="Read-only archive, deployed files and corpus hash verification. Starts no process without --execute.")
    args = parser.parse_args()
    if not math.isfinite(args.reserve_gib) or args.reserve_gib < 0:
        raise RuntimeError("--reserve-gib must be finite and nonnegative")
    if args.max_run_seconds < 60:
        raise RuntimeError("--max-run-seconds must allow at least 60 seconds")
    for name in ("source", "semantic_report", "package_candidate", "model_dir", "artifact_root", "report"):
        setattr(args, name, getattr(args, name).resolve())
    args.working_directory = check_working_directory(args.working_directory)
    os.environ["GIT_OPTIONAL_LOCKS"] = "0"
    evidence, evidence_path = load_evidence_api(args.source)
    semantic_raw = args.semantic_report.read_bytes()
    semantic = json.loads(semantic_raw)
    candidate = json.loads(args.package_candidate.read_text(encoding="utf-8-sig"))
    verify_fingerprints(semantic, candidate)
    identity = verify_provenance(evidence, args.package_candidate, candidate, args.source, args.expected_commit)
    corpus = args.corpus.resolve() if args.corpus else Path(semantic["preservedRoot"]) / "corpus"
    layout = resolve_candidate(args.package_candidate.resolve(), candidate, args.app_dir, semantic)
    package_bin = layout["packageBin"]
    queries = choose_queries(semantic["queries"])
    plan = {
        "kind": "packaged-cli-ipc-query-latency", "executeRequested": args.execute,
        "workingDirectoryParent": str(args.working_directory), "expectedCommit": args.expected_commit,
        "maxRunSeconds": args.max_run_seconds, "reserveGiB": args.reserve_gib,
        "packageBin": str(package_bin) if package_bin else None, "sourceCorpus": str(corpus),
        "candidateIdentity": candidate["identity"], "packageArchive": str(layout["archive"]),
        "packageSha256": candidate["packageSha256"], "executables": candidate["executables"],
        "sourceSemanticVectors": semantic["actualEmbeddedSegments"],
        "reuseExistingDatabase": False,
        "reasonForReindex": "Direct harness profile uses different pipeline/version metadata from the service. Rebuild through public production IPC, never rewrite validation metadata.",
        "warmupQueries": 10, "measuredQueries": len(queries),
        "scope": "Wall clock immediately before subprocess.run until process exit and stdout/stderr collection complete, including Python process-launch/wait overhead, CLI startup, IPC handshake, service dispatch, warm query-worker encoding, repository search and service/CLI JSON serialization. Excludes subsequent Python JSON parsing/rank calculation, GUI/rendering and indexing setup. Actual production vector count is recorded separately, never represented as a million-vector measurement.",
        "capacityComparison": "The separate capacity report measures at least 500 warmed repository retrieval calls over one million synthetic vectors, excluding query encoding, IPC and CLI startup. This 50-query run uses real corpus/model embeddings at its recorded production size and does not substitute for that capacity gate.",
    }
    initial_inputs = None
    if args.verify_inputs or args.execute:
        initial_inputs = verify_candidate_inputs(args.package_candidate.resolve(), candidate, layout, semantic, corpus)
        plan["inputVerification"] = {"passed": True, "deployedFileCount": len(initial_inputs["deployedFiles"]),
                                     "corpusFileCount": len(initial_inputs["corpusFiles"])}
    if not args.execute:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 0
    if os.name != "nt":
        raise RuntimeError("This external acceptance runner currently requires Windows owned-process jobs")
    if args.report.exists():
        raise RuntimeError("Refusing to overwrite an existing latency report")
    args.artifact_root.mkdir(parents=True, exist_ok=True)
    run = Path(tempfile.mkdtemp(prefix="qcs-ipc-latency-", dir=args.artifact_root))
    profile = run / "profile 中文 space"
    profile.mkdir()
    args.report.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "schema": 1, "passed": False, **plan, "startedAt": datetime.now(timezone.utc).isoformat(),
        "invocation": sys.argv,
        "runRoot": str(run), "profile": str(profile), "sourceSemanticReport": str(args.semantic_report),
        "sourceSemanticReportSha256": hashlib.sha256(semantic_raw).hexdigest(),
        "sourcePackageCandidate": str(args.package_candidate), "binarySourceCommit": candidate["identity"]["checkoutCommit"],
        "sourcePackageCandidateSha256": initial_inputs["candidateSha256"], "buildProvenance": candidate["buildProvenance"],
        "runnerSha256": sha256(__file__), "inputIdentityBefore": initial_inputs,
        "processHelperSha256": sha256(Path(__file__).with_name("qcs_owned_processes.py")),
        "modelFingerprint": semantic["modelFingerprint"], "extractionFingerprint": semantic["extractionFingerprint"],
        "warmup": [], "measurements": [], "setup": [], "commands": [],
        "loadCondition": "Explicit exclusive window after final-package Computer Use and before million-vector acceptance. No competing workload is launched by this runner.",
    }
    service = None
    processes = WindowsProcesses()
    service_job = None
    service_stdout = service_stderr = None
    cwd_context = None
    working_directory = None
    env = dict(os.environ)
    # Test the deployed layout rather than selecting SDK plugins or resource paths.
    for key in list(env):
        if key.startswith("QT_") or key.startswith("QCOPILOTS_SEARCH_") or key in {"QML2_IMPORT_PATH", "QML_IMPORT_PATH"}:
            env.pop(key, None)
    if os.name == "nt":
        if not env.get("SystemRoot"):
            raise RuntimeError("Windows SystemRoot is unavailable")
        system_root = Path(env["SystemRoot"])
        env["PATH"] = os.pathsep.join(map(str, (package_bin, system_root / "System32", system_root)))
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    cli = package_bin / ("qcs-cli.exe" if os.name == "nt" else "qcs-cli")
    service_exe = package_bin / ("qcs-service.exe" if os.name == "nt" else "qcs-service")
    overall_deadline = time.monotonic() + args.max_run_seconds

    def command(arguments, timeout=120, cleanup=False):
        if not cleanup:
            remaining = overall_deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError("The bounded latency execution window expired")
            timeout = min(timeout, remaining)
        argv = [str(cli), "--profile-dir", str(profile), "--model-dir", str(args.model_dir),
                "--no-start", "--timeout", str(int(timeout * 1000)), *arguments]
        number = len(report["commands"]) + 1
        try:
            completed, record = captured_command(argv, working_directory, env, flags, run / "commands", number, timeout + 10)
        except subprocess.TimeoutExpired as error:
            record = error.evidence_record
            record["arguments"] = arguments
            report["commands"].append(record)
            report["lastFailedCommand"] = record
            raise
        record["arguments"] = arguments
        report["commands"].append(record)
        try:
            envelope = json.loads(completed.stdout.decode("utf-8-sig"))
        except Exception:
            record["invalidJson"] = True
            report["lastFailedCommand"] = record
            raise RuntimeError("CLI did not emit a JSON response")
        if completed.returncode or not envelope.get("ok"):
            record["response"] = envelope
            report["lastFailedCommand"] = record
            raise RuntimeError(f"CLI operation failed: {envelope}")
        return envelope["result"], record

    def timed_query(query, phase, number):
        arguments = ["search", query["query"], "--limit", "10"]
        if query["modality"] == "code":
            arguments.append("--code")
        response, record = command(arguments)
        record.update({"number": number, "query": query["query"], "language": query["language"],
                       "modality": query["modality"], "expectedFile": query["expectedFile"],
                       "semantic": response.get("semantic"), "warning": response.get("warning"),
                       "returnedCount": len(response.get("hits", []))})
        record["rank"] = next((i + 1 for i, hit in enumerate(response.get("hits", []))
                               if Path(hit["path"]).name == query["expectedFile"]), 0)
        report[phase].append(record)
        if not response.get("semantic") or response.get("warning"):
            raise RuntimeError("Query did not complete through a healthy real semantic worker")
        return record

    try:
        cwd_context = tempfile.TemporaryDirectory(prefix="qcs-cwd-", dir=args.working_directory)
        working_directory = check_working_directory(Path(cwd_context.name))
        report["workingDirectory"] = str(working_directory)
        report["capturedInputs"], report["externalArtifactBindings"] = capture_inputs(
            run, args.package_candidate, candidate, args.semantic_report, semantic, corpus, evidence_path, args.reserve_gib)
        report["binaryHashes"] = candidate["executables"]
        manifest, manifest_record = command(["model", "manifest"])
        verified_models, verify_record = command(["model", "verify"])
        if not verified_models.get("valid"):
            raise RuntimeError("Packaged CLI did not verify the pinned model bytes")
        report["modelVerificationBefore"] = verified_models
        report["setup"].append(verify_record)
        material = "qcopilots-search-pipeline-v1:" + semantic["extractionFingerprint"] + ":" + semantic["modelFingerprint"]
        for artifact in manifest["artifacts"]:
            material += ":" + artifact["sha256"]
        expected_pipeline = hashlib.sha256(material.encode("utf-8")).hexdigest()
        report["expectedServicePipeline"] = expected_pipeline
        report["setup"].append(manifest_record)
        service_stdout = (run / "service-stdout.log").open("wb")
        service_stderr = (run / "service-stderr.log").open("wb")
        service = subprocess.Popen([str(service_exe), "--profile-dir", str(profile), "--model-dir", str(args.model_dir)],
                                   cwd=working_directory, env=env, stdout=service_stdout, stderr=service_stderr,
                                   creationflags=flags | CREATE_SUSPENDED)
        report["servicePid"] = service.pid
        service_job = processes.job(service)
        processes.resume(service)
        report["ownedServiceJob"] = {"assignedBeforeResume": True, "servicePid": service.pid}
        # Model verification happens before listen, so use the isolated token as
        # readiness evidence, then retry status briefly until the socket listens.
        deadline = min(time.monotonic() + 120, overall_deadline)
        token = profile / "qcs-ipc-token"
        while not token.is_file():
            if service.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError("Owned service did not finish startup")
            time.sleep(.1)
        initial = None
        for _ in range(20):
            try:
                initial, record = command(["status"], 10)
                report["setup"].append(record)
                report.pop("lastFailedCommand", None)
                break
            except RuntimeError:
                if service.poll() is not None:
                    raise
                time.sleep(.1)
        if initial is None or not initial["modelAvailable"] or initial["pipelineFingerprint"] != expected_pipeline:
            raise RuntimeError("Service model/contract fingerprint does not match the verified semantic model")
        report["initialStatus"] = initial
        _, record = command(["index", str(corpus), "--code", "--wait"], 1800)
        report["setup"].append(record)
        _, record = command(["pause"])
        report["setup"].append(record)
        status, record = command(["status"])
        report["setup"].append(record)
        report["indexedStatus"] = status
        if (status["repository"]["vectors"] < 1000 or status["repository"]["files"] != len(semantic["files"])
                or status["failures"] or status["queuedFiles"]):
            raise RuntimeError("Production corpus indexing was incomplete")
        if (status["progress"]["state"] != "paused" or status["settings"]["indexingPaused"] is not True
                or status.get("warning") or status.get("pipelineTransitionError")
                or status["repository"]["pipelineFingerprint"] != expected_pipeline):
            raise RuntimeError("Production corpus was not paused in the expected healthy model space")
        report["actualProductionSegments"] = status["repository"]["segments"]
        report["actualProductionVectors"] = status["repository"]["vectors"]
        report["actualProductionFiles"] = status["repository"]["files"]
        report["ownedProcessesBeforeMeasurement"] = {str(pid): row for pid, row in processes.all().items()
            if processes.contains(service_job, pid)}
        for i, query in enumerate(queries[:10], 1):
            timed_query(query, "warmup", i)
        for i, query in enumerate(queries, 1):
            record = timed_query(query, "measurements", i)
            print(f"Measured {i}/50: {record['wallMilliseconds']:.3f} ms {query['language']} {query['modality']}", flush=True)
        report["cliIpcEndToEnd"] = percentiles([q["wallMilliseconds"] for q in report["measurements"]])
        report["queryWorkerThreads"] = 4
        report["warmupCount"] = len(report["warmup"])
        report["measuredCount"] = len(report["measurements"])
        report["qualityNote"] = "Latencies include every selected query, including any relevance miss. Relevance remains governed by the separate 222-query quality report. Semantic fallback or worker warnings fail this measurement."
        verified_models_after, verify_record = command(["model", "verify"])
        report["setup"].append(verify_record)
        report["modelVerificationAfter"] = verified_models_after
        if verified_models_after != verified_models:
            raise RuntimeError("Pinned model verification changed during measurement")
        final_status, record = command(["status"])
        report["setup"].append(record)
        report["postMeasurementStatus"] = final_status
        stable_fields = ("files", "segments", "vectors", "generation", "annGeneration", "pipelineFingerprint")
        report["repositoryUnchanged"] = all(final_status["repository"][key] == status["repository"][key] for key in stable_fields)
        report["settingsUnchanged"] = final_status["settings"] == status["settings"]
        if (not report["repositoryUnchanged"] or not report["settingsUnchanged"] or final_status["progress"]["state"] != "paused"
                or not final_status["modelAvailable"] or final_status.get("warning") or final_status.get("pipelineTransitionError")
                or final_status["queuedFiles"] or final_status["failures"]):
            raise RuntimeError("Service state changed or became unhealthy during query measurement")
        report["passed"] = True
    except Exception as error:
        report["error"] = str(error)
    finally:
        report["shutdownSucceeded"] = service is None
        if service is not None:
            try:
                if service.poll() is None:
                    _, shutdown_record = command(["shutdown"], 30, cleanup=True)
                    report["setup"].append(shutdown_record)
                service.wait(timeout=25)
                report["shutdownSucceeded"] = True
            except Exception as error:
                report["passed"] = False
                report["shutdownError"] = str(error)
                # This handle belongs only to the process spawned above.
                try:
                    if service.poll() is None:
                        service.terminate()
                        service.wait(timeout=10)
                except Exception as termination_error:
                    report["terminateError"] = str(termination_error)
            report["serviceExitCode"] = service.poll()
            if service.returncode != 0:
                report["passed"] = False
        if service_job is not None:
            try:
                report["ownedActiveAfterServiceExit"] = processes.active(service_job)
                if report["ownedActiveAfterServiceExit"]:
                    report["passed"] = False
                    report["ownedForcedCleanup"] = True
                    report["ownedResidualAfterCleanup"] = processes.stop(service_job)
                else:
                    report["ownedForcedCleanup"] = False
                    report["ownedResidualAfterCleanup"] = 0
                if report["ownedResidualAfterCleanup"]:
                    report["passed"] = False
            except Exception as error:
                report["passed"] = False
                report["ownedCleanupError"] = str(error)
            finally:
                processes.close(service_job)
        for stream in (service_stdout, service_stderr):
            if stream is not None:
                stream.close()
        try:
            final_inputs = verify_candidate_inputs(args.package_candidate.resolve(), candidate, layout, semantic, corpus)
            report["inputsUnchanged"] = final_inputs == initial_inputs
            report["semanticReportUnchanged"] = sha256(args.semantic_report) == report["sourceSemanticReportSha256"]
            report["runnerUnchanged"] = sha256(__file__) == report["runnerSha256"]
            report["processHelperUnchanged"] = sha256(Path(__file__).with_name("qcs_owned_processes.py")) == report["processHelperSha256"]
            report["sourceIdentityUnchanged"] = verify_provenance(
                evidence, args.package_candidate, candidate, args.source, args.expected_commit) == identity
            if not all(report[key] for key in ("inputsUnchanged", "semanticReportUnchanged", "runnerUnchanged", "processHelperUnchanged", "sourceIdentityUnchanged")):
                report["passed"] = False
        except Exception as error:
            report["passed"] = False
            report["inputVerificationError"] = str(error)
        if cwd_context is not None:
            try:
                cwd_context.cleanup()
                report["workingDirectoryCleaned"] = True
            except OSError as error:
                report["passed"] = False
                report["workingDirectoryCleaned"] = False
                report["workingDirectoryCleanupError"] = str(error)
        report["finishedAt"] = datetime.now(timezone.utc).isoformat()
        report["profileRetained"] = True
        report["portableEvidence"] = "Copy this run directory excluding the profile. manifest.json binds all retained inputs, raw command replies, service logs and report. Final ZIP and model bytes are referenced by verified hashes, not duplicated."
        report["freeBytesAfter"] = shutil.disk_usage(run).free
        if report["freeBytesAfter"] < args.reserve_gib * 1024**3:
            report["passed"] = False
            report["diskReserveError"] = "The run did not preserve the configured free-space reserve"
        write_json(args.report, report)
        write_json(run / "report.json", report)
        finalize_manifest(run)
    print(json.dumps({"passed": report["passed"], "report": str(args.report), "error": report.get("error"),
                      "actualProductionVectors": report.get("actualProductionVectors"),
                      "cliIpcEndToEnd": report.get("cliIpcEndToEnd")}, ensure_ascii=False))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
