#!/usr/bin/env python3
"""Verify native and final release evidence. This command never publishes a release."""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
import uuid
import zipfile
from functools import wraps

sys.dont_write_bytecode = True
SOURCE = Path(__file__).resolve().parents[1]
PLATFORMS = {"windows-x64": ("Windows", "x86_64"), "linux-x64": ("Linux", "x86_64"),
             "macos-arm64": ("Darwin", "arm64"), "macos-x64": ("Darwin", "x86_64")}
GOLDENS = {"query", "query-bn", "query-ar", "document", "document-long", "screen-280", "photo-140", "frame-70", "speech"}
LOCKS = ("dependencies.lock.json", "tools/qt-packages.lock.json", "tools/nsis-package.lock.json",
         "tools/qcopilots-search-source-packages.lock.json", "cmake/QCopilotsSearchNativeDependencies.cmake")
FAULTS = {"committed-before-ann", "uncommitted-sqlite", "ann-file-saved-before-manifest",
          "ann-manifest-published-before-outbox-cleanup"}
APPLICATIONS = {"qcopilots-search", "qcopilots-search-service", "qcopilots-search-worker", "qcopilots-search-cli"}


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


def at_least(value, minimum):
    return finite(value) and value >= minimum


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


def validate_rebuild_intervals(concurrent):
    errors = []
    check(errors, concurrent.get("timingClock") == "steady-clock-relative-nanoseconds",
          "capacity rebuild clock is missing or incompatible")
    builds, queries = concurrent.get("buildIntervals", []), concurrent.get("queryIntervals", [])
    check(errors, len(builds) == 1 and concurrent.get("observedBuilds") == len(builds)
          and integer_at_least(concurrent.get("observedBuilds"), 1),
          "capacity rebuild must observe exactly one actual graph build")
    check(errors, 1 <= len(queries) <= 500 and concurrent.get("measuredQueries") == len(queries)
          and integer_at_least(concurrent.get("measuredQueries"), 1),
          "capacity rebuild query intervals are missing or inconsistent")
    valid_builds = []
    for build in builds:
        start, end = build.get("startNanoseconds"), build.get("endNanoseconds")
        valid = integer_at_least(start, 0) and integer_at_least(end, 0) and end > start
        check(errors, valid, "capacity graph build interval is invalid")
        if valid:
            valid_builds.append((start, end))
    overlapping = []
    for query in queries:
        start, end = query.get("startNanoseconds"), query.get("endNanoseconds")
        valid = integer_at_least(start, 0) and integer_at_least(end, 0) and end > start
        check(errors, valid, "capacity rebuild query interval is invalid")
        if not valid:
            continue
        duration = (end - start) / 1000000.0
        overlaps = any(start < build_end and end > build_start for build_start, build_end in valid_builds)
        check(errors, query.get("overlapsBuild") is overlaps
              and same_number(query.get("durationMilliseconds"), duration),
              "capacity rebuild query duration or overlap disagrees with its raw interval")
        if overlaps:
            overlapping.append(duration)
    check(errors, bool(overlapping) and concurrent.get("overlappingQueries") == len(overlapping),
          "capacity rebuild has no measured overlap or its count disagrees")
    if overlapping:
        overlapping.sort()
        for name, percentile in (("p50Milliseconds", .50), ("p95Milliseconds", .95), ("p99Milliseconds", .99)):
            observed = overlapping[math.ceil(percentile * len(overlapping)) - 1]
            check(errors, same_number(concurrent.get(name), observed),
                  f"capacity rebuild {name} does not match genuinely overlapping queries")
    return errors


@malformed_as_error
def validate_capacity(doc, *, minimum_segments, minimum_queries, minimum_timings):
    errors = []
    check(errors, doc.get("passed") is True and not doc.get("error"), "capacity did not finish successfully")
    segments = doc.get("segments", 0)
    check(errors, at_least(segments, minimum_segments), "capacity vector count is below the required scale")
    check(errors, doc.get("dimensions") == 768, "capacity dimensions must be 768")
    files, per_file = doc.get("files"), doc.get("segmentsPerFile")
    check(errors, at_least(files, 1) and at_least(per_file, 1) and files * per_file == segments,
          "capacity file/segment counts disagree")
    check(errors, doc.get("oracleRows") == segments, "capacity exact oracle did not read the full corpus")
    for name in ("builtStatus", "reopenedStatus"):
        state = doc.get(name, {})
        check(errors, state.get("vectors") == segments and state.get("segments") == segments and state.get("files") == files,
              f"capacity {name} does not prove the full corpus was built and reopened")
        check(errors, state.get("pipelineFingerprint") == "synthetic-capacity-768-v1", f"capacity {name} pipeline fingerprint is missing")
    queries = doc.get("queries", [])
    check(errors, isinstance(queries, list) and len(queries) >= minimum_queries, "capacity has too few exact query records")
    if isinstance(queries, list) and queries:
        query_ids = [row.get("query") for row in queries]
        check(errors, all(isinstance(value, int) and not isinstance(value, bool) and value >= 0 for value in query_ids)
              and len(set(query_ids)) == len(query_ids), "capacity exact query identities must be distinct nonnegative integers")
        groups = defaultdict(list)
        for row in queries:
            value = row.get("recallAt20")
            check(errors, within(value, 0, 1), "capacity query recall is invalid")
            check(errors, within(row.get("returnedFiles"), 1, 20), "capacity query returned count is invalid")
            if finite(value):
                groups[row.get("distribution")].append(value)
        values = [value for group in groups.values() for value in group]
        mean = sum(values) / len(values) if values else -1
        check(errors, same_number(doc.get("recallAt20"), mean) and mean >= .95, "capacity overall recall is inconsistent or below .95")
        for group in ("clustered", "uniform"):
            values = groups.get(group, [])
            check(errors, bool(values) and same_number(doc.get(group + "RecallAt20"), sum(values)/len(values) if values else -1),
                  f"capacity {group} query evidence is missing or inconsistent")
    check(errors, at_least(doc.get("timedQueries"), minimum_timings) and at_least(doc.get("warmupQueries"), 10),
          "capacity has too few warmed timing queries")
    check(errors, within(doc.get("latencyMilliseconds", {}).get("p95"), 0, 500), "capacity P95 exceeds 500 ms or is invalid")
    filters = doc.get("filters", [])
    check(errors, len(filters) == 3 and {item.get("nominalPercent") for item in filters} == {10, 1, .1}, "capacity filter scenarios are incomplete")
    for item in filters:
        check(errors, item.get("passed") is True and at_least(item.get("checkedQueries"), minimum_queries)
              and within(item.get("recallAt20"), .95, 1) and within(item.get("p95Milliseconds"), 0, 500),
              "capacity filtered retrieval failed or lacks measurements")
    concurrent = doc.get("concurrentRebuild", {})
    check(errors, doc.get("concurrentRebuildEvaluated") is True and concurrent.get("passed") is True
          and concurrent.get("singleShard") is True and at_least(concurrent.get("overlappingQueries"), 1)
          and within(concurrent.get("p95Milliseconds"), 0, 1000), "capacity concurrent single-shard rebuild evidence is incomplete or slow")
    errors.extend(validate_rebuild_intervals(concurrent))
    mutations = doc.get("mutations", {})
    check(errors, mutations.get("passed") is True and at_least(mutations.get("vectorsPerPhase"), min(10000, segments)),
          "capacity mutation evidence is incomplete")
    mutation_files, mutation_vectors = mutations.get("filesPerPhase"), mutations.get("vectorsPerPhase")
    check(errors, integer_at_least(mutation_files, 1) and integer_at_least(mutation_vectors, 1)
          and mutation_files * per_file == mutation_vectors,
          "capacity mutation file/vector counts disagree")
    phases = mutations.get("phases", [])
    check(errors, mutations.get("queryScope") == "filtered-exact-file-plus-global-production-search-sample"
          and [phase.get("phase") for phase in phases] == ["add", "update", "delete"],
          "capacity mutation search phases are incomplete")
    for phase in phases:
        check(errors, phase.get("passed") is True and all(
            integer_at_least(phase.get(name), 1) and phase.get(name) == mutation_files
            for name in ("filesChecked", "keywordQueries", "vectorQueries")),
            "capacity mutation did not check every changed file and its search results")
        check(errors, all(integer_at_least(phase.get(name), 0) and phase.get(name) == 0
            for name in ("staleHitCount", "retiredSegmentIdsRemaining", "globalSearchStaleHitCount")),
            "capacity mutation returned stale versions or retained retired segments")
        expected_retired = 0 if phase.get("phase") == "add" else mutation_vectors
        check(errors, integer_at_least(phase.get("retiredSegmentIdsChecked"), 0)
              and phase.get("retiredSegmentIdsChecked") == expected_retired,
              "capacity mutation did not inspect every retired segment")
        check(errors, integer_at_least(phase.get("globalSearchQueries"), 1)
              and phase.get("globalSearchQueries") == min(16, mutation_files)
              and integer_at_least(phase.get("globalSearchCheckedHits"), 1),
              "capacity mutation global production search did not return checked hits")
        global_vectors = segments + (0 if phase.get("phase") == "delete" else mutation_vectors)
        expected_backend = "ann" if global_vectors > 20000 else "exact-subset"
        check(errors, integer_at_least(phase.get("globalSearchVectorCount"), 1)
              and phase.get("globalSearchVectorCount") == global_vectors
              and phase.get("globalSearchBackend") == expected_backend,
              "capacity mutation global search backend disagrees with the actual corpus scale")
    faults = doc.get("recoveryFaults", [])
    check(errors, doc.get("recoveryFaultInjectionEvaluated") is True and len(faults) == len(FAULTS)
          and {item.get("faultPoint") for item in faults} == FAULTS, "capacity recovery fault points are incomplete")
    ordered_faults = ["committed-before-ann", "uncommitted-sqlite", "ann-file-saved-before-manifest",
                      "ann-manifest-published-before-outbox-cleanup"]
    check(errors, [item.get("faultPoint") for item in faults] == ordered_faults,
          "capacity recovery fault order is incomplete or inconsistent")
    previous_id = 0
    previous_version = previous_token = None
    committed_versions, committed_tokens = set(), set()
    for item in faults:
        check(errors, item.get("passed") is True and item.get("childExitCode") == 73, "capacity recovery child did not exercise the required interruption")
        version, token = item.get("expectedVersion"), item.get("expectedToken")
        check(errors, isinstance(version, str) and bool(version.strip()) and version == item.get("observedVersion")
              and isinstance(token, str) and bool(token.strip()) and token == item.get("observedToken"),
              "capacity recovery did not recover the intended committed version and keyword")
        observed_id = item.get("observedSegmentId")
        check(errors, integer_at_least(item.get("previousSegmentId"), 0) and item.get("previousSegmentId") == previous_id
              and integer_at_least(observed_id, 1), "capacity recovery segment lineage is incomplete")
        rollback = item.get("faultPoint") == "uncommitted-sqlite"
        if rollback:
            check(errors, observed_id == previous_id and version == previous_version and token == previous_token,
                  "capacity rollback did not preserve the previous committed identity")
        else:
            check(errors, integer_at_least(observed_id, 1) and observed_id > previous_id
                  and version not in committed_versions and token not in committed_tokens,
                  "capacity recovery reused an earlier committed identity")
            committed_versions.add(version)
            committed_tokens.add(token)
        expected_dimension = {"committed-before-ann": 700, "uncommitted-sqlite": 700,
                              "ann-file-saved-before-manifest": 702,
                              "ann-manifest-published-before-outbox-cleanup": 703}.get(item.get("faultPoint"))
        check(errors, integer_at_least(item.get("expectedVectorDimension"), 0)
              and item.get("expectedVectorDimension") == expected_dimension
              and integer_at_least(item.get("observedVectorDimension"), 0)
              and item.get("observedVectorDimension") == expected_dimension
              and within(item.get("observedVectorCosine"), .999, 1.000000001)
              and item.get("vectorMatched") is True,
              "capacity recovery did not recover the intended committed vector")
        check(errors, all(integer_at_least(item.get(name), 0) and item.get(name) == 0
              for name in ("staleVersionHits", "oldKeywordHits", "uncommittedCorruptionRows")),
              "capacity recovery retained stale search results or uncommitted table changes")
        previous_id, previous_version, previous_token = observed_id, version, token
        if item.get("faultPoint") == "ann-manifest-published-before-outbox-cleanup":
            check(errors, item.get("stalePublishedOutboxEvents") == 0 and item.get("postRecoveryWritePassed") is True,
                  "capacity recovery retained stale events or could not accept a subsequent write")
    check(errors, finite(doc.get("sampledDirectoryPeakBytes")) and finite(doc.get("directoryBudgetBytes"))
          and doc["sampledDirectoryPeakBytes"] <= doc["directoryBudgetBytes"], "capacity disk usage exceeded the configured budget or is missing")
    check(errors, finite(doc.get("minimumObservedFreeBytes")) and finite(doc.get("minimumFreeBytes"))
          and doc["minimumObservedFreeBytes"] >= doc["minimumFreeBytes"], "capacity free-space reserve was violated or not measured")
    return errors


@malformed_as_error
def validate_semantic(doc):
    errors = []
    check(errors, doc.get("passed") is True and doc.get("mode") == "release", "semantic acceptance was not a successful full run")
    check(errors, at_least(doc.get("actualEmbeddedSegments"), 1000), "semantic corpus has fewer than 1000 embedded segments")
    queries = doc.get("queries", [])
    check(errors, isinstance(queries, list) and len(queries) >= 200 and len(queries) == doc.get("positiveQueries"), "semantic positive query records are incomplete")
    texts = [row.get("query") for row in queries]
    check(errors, all(isinstance(value, str) and value.strip() for value in texts)
          and len(set(texts)) == len(texts), "semantic query texts must be nonempty and distinct")
    totals = defaultdict(lambda: [0, 0, 0])
    for row in queries:
        expected = row.get("expectedFile")
        production = [item.get("file") for item in row.get("productionTop10", [])]
        exact = [item.get("file") for item in row.get("exactSemanticTop10", [])]
        check(errors, len(exact) <= 10 and len(set(exact)) == len(exact)
              and all(within(item.get("cosine"), -1.00001, 1.00001) for item in row.get("exactSemanticTop10", [])),
              "semantic exact result list contains invalid cosines or duplicates")
        rank = production.index(expected) + 1 if expected in production else 0
        check(errors, bool(expected) and len(production) <= 10 and len(set(production)) == len(production)
              and row.get("rank") == rank, "semantic query rank disagrees with its raw result list")
        for key in ("overall", "language:" + str(row.get("language")), "modality:" + str(row.get("modality"))):
            totals[key][0] += 1
            totals[key][1] += int(rank > 0)
            totals[key][2] += int(expected in exact)
    required = {"overall", "language:en", "language:zh"} | {"modality:" + value for value in
                ("text", "ocr", "image", "pdf", "speech", "environment", "video", "code")}
    check(errors, required.issubset(totals), "semantic language or modality groups are missing")
    for key, (count, hits, exact_hits) in totals.items():
        metric = doc.get("metrics", {}).get(key, {})
        recall = hits / count
        check(errors, metric.get("queries") == count and metric.get("hits") == hits
              and same_number(metric.get("recallAt10"), recall)
              and same_number(metric.get("exactSemanticRecallAt10"), exact_hits / count)
              and recall >= (.9 if key == "overall" else .8), f"semantic {key} counts or recall do not satisfy acceptance")
    for key in ("modelFingerprint", "extractionFingerprint", "fixtureManifestSha256", "topicManifestSha256", "generatorSha256"):
        check(errors, isinstance(doc.get(key), str) and bool(doc[key]), f"semantic {key} is missing")
    negatives = doc.get("noAnswerQueries", [])
    check(errors, bool(negatives) and {row.get("partition") for row in negatives} == {"calibration", "holdout"},
          "semantic independent absent-query evidence is missing")
    for partition in ("calibration", "holdout"):
        rows = [row for row in negatives if row.get("partition") == partition]
        summary = doc.get("noAnswerSummary", {}).get(partition, {})
        check(errors, summary.get("queries") == len(rows) and len(rows) > 0
              and summary.get("queriesReturningCandidates") == sum(row.get("returnedCount", 0) > 0 for row in rows),
              "semantic absent-query counts disagree with raw records")
        for row in rows:
            check(errors, within(row.get("returnedCount"), 0, 10)
                  and all(within(item.get("cosine"), -1.00001, 1.00001) for item in row.get("exactSemanticTop10", [])),
                  "semantic absent-query similarity evidence is invalid")
    return errors


@malformed_as_error
def validate_physical(doc, *, minimum_files=100000):
    errors = []
    check(errors, doc.get("passed") is True and doc.get("scenario") == "physical-file-capacity", "physical file run did not pass")
    check(errors, at_least(doc.get("createdPhysicalFiles"), minimum_files), "physical file count is below required scale")
    for name in ("added", "modified", "deleted", "cancelledAfterDiscoveredFiles"):
        check(errors, at_least(doc.get(name), 1), f"physical file scenario {name} did not execute")
    check(errors, doc.get("staleResults") == 0 and doc.get("cleanupVerified") is True, "physical file run retained stale results or temporary files")
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


def capture_native(root: Path, identity: dict, platform_id: str):
    files = []
    for path in sorted((root / "reports").rglob("*")):
        if path.is_file() and path.name not in ("native-evidence.json", "native-gate.json"):
            files.append(file_record(root, path))
    for path in sorted((root / "packages").glob("qcopilots-search-*")):
        if path.is_file():
            files.append(file_record(root, path))
    manifest = {"schemaVersion": 1, "kind": "native", "platformId": platform_id,
                "identity": identity, "files": files,
                "createdAtUtc": datetime.now(timezone.utc).isoformat()}
    write_json(root / "reports/native-evidence.json", manifest)
    return manifest


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


def inspect_native(root: Path, *, expected_head: str | None = None):
    errors = []
    result = {"schemaVersion": 1, "kind": "native-gate", "passed": False, "errors": errors}
    try:
        manifest = read_json(root / "reports/native-evidence.json")
        errors.extend(verify_manifest(root, manifest))
        identity = manifest["identity"]
        platform_id = manifest.get("platformId")
        result.update(platformId=platform_id, identity=identity)
        check(errors, platform_id in PLATFORMS, "Unknown native platform combination")
        if expected_head:
            check(errors, identity.get("candidateHead") == expected_head, "Native candidate head does not match the requested candidate")
        paths = {entry["path"] for entry in manifest.get("files", [])}
        def document(name):
            check(errors, name in paths, f"Raw report is not bound to the native evidence manifest: {name}")
            return read_json(safe_file(root, name))
        for name in ("reports/tests/ctest.xml", "reports/tests/ctest-inventory.json"):
            check(errors, name in paths, f"Missing bound CTest raw evidence: {name}")
        spec = importlib.util.spec_from_file_location("qcopilots_search_ctest_evidence", SOURCE / ".github/scripts/qcopilots-search-ctest-evidence.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        system, architecture = PLATFORMS.get(platform_id, ("unknown", "unknown"))
        native = document("reports/native-acceptance.json")
        check(errors, native.get("sourceIdentity") == identity and native.get("system") == system
              and native.get("expectedArchitecture") == architecture, "Native runner source or platform identity differs from its evidence unit")
        build = document("reports/build-provenance.json")
        check(errors, build.get("identity") == identity and build.get("completed") is True
              and APPLICATIONS.issubset(build.get("programs", {})), "Native executable build provenance is missing")
        case_evidence = module.inspect_junit(root / "reports/tests/ctest.xml", system,
                                             root / "reports/tests/ctest-inventory.json")
        errors.extend(case_evidence["errors"])
        check(errors, case_evidence["releaseComplete"], "Native CTest case evidence is incomplete")
        required_tests = {"qcopilots-search-tests-" + name for name in
                          ("reference-vectors", "semantic", "gui", "gui-service", "cli", "worker", "service",
                           "extraction-document", "extraction-image", "extraction-media", "extraction-legacy_doc")}
        if architecture == "x86_64":
            required_tests.add("qcopilots-search-tests-reference-vectors-baseline")
        check(errors, required_tests.issubset(case_evidence["expectedPrograms"]), "Native inventory omits required real-model or application tests")
        junit = ET.parse(root / "reports/tests/ctest.xml").getroot()
        for case in junit.iter("testcase"):
            if "reference-vectors" not in case.attrib.get("name", ""):
                continue
            values = re.findall(r"(\S+) reference cosine ([0-9.]+)", case.findtext("system-out") or "")
            check(errors, len(values) == 9 and {name for name, _ in values} == GOLDENS
                  and all(within(float(value), .999, 1.00001) for _, value in values), "Nine raw golden-vector cosine measurements are missing or below .999")
        models = document("reports/models.json")
        model_manifest = document("reports/models-manifest.json")
        expected_models = model_manifest.get("result", {}).get("artifacts", [])
        actual_models = models.get("result", {}).get("artifacts", [])
        check(errors, models.get("ok") is True and models.get("result", {}).get("valid") is True
              and len(expected_models) == len(actual_models) == 2, "Verified model artifact evidence is incomplete")
        for expected in expected_models:
            actual = next((item for item in actual_models if item.get("name") == expected.get("name")), {})
            check(errors, actual.get("present") is True and actual.get("sha256") == expected.get("sha256")
                  and actual.get("bytes") == expected.get("size"), "Verified model content does not match its pinned manifest")
        semantic = document("reports/semantic-quality.json")
        errors.extend(validate_semantic(semantic))
        result["modelFingerprint"] = semantic.get("modelFingerprint")
        result["extractionFingerprint"] = semantic.get("extractionFingerprint")
        errors.extend(validate_capacity(document("reports/capacity-ci.json"), minimum_segments=30000, minimum_queries=10, minimum_timings=20))
        errors.extend(validate_physical(document("reports/physical-files-ci.json"), minimum_files=1000))
        package_reports = ["reports/package/report.json"]
        if system == "Windows":
            package_reports.append("reports/package-nsis/report.json")
        elif system == "Linux":
            package_reports.append("reports/package-deb/report.json")
        package_hashes = {item["sha256"] for item in manifest["files"] if item["path"].startswith("packages/")}
        for name in package_reports:
            package = document(name)
            check(errors, package.get("passed") is True and package.get("commit") == identity["checkoutCommit"]
                  and package.get("workingTreeModified", False) is False and package.get("packageSha256") in package_hashes,
                  f"Native package result is missing, stale or unbound: {name}")
            check(errors, len(package.get("checks", [])) >= (3 if "package-deb" in name else 9), f"Native package scenarios are incomplete: {name}")
        integrity = document("reports/source-integrity.json")
        check(errors, integrity.get("passed") is True and integrity.get("tests_run", 0) >= 10
              and integrity.get("skipped") == 0 and len(integrity.get("cases", [])) == integrity.get("tests_run")
              and all(item.get("status") == "passed" for item in integrity.get("cases", [])), "Source rebuild-integrity tests are incomplete")
        sources = document("packages/qcopilots-search-source-manifest.json")
        check(errors, sources.get("commit") == identity["checkoutCommit"] and sources.get("workingTreeModified") is False,
              "Corresponding source materials do not belong to the clean native checkout")
        check(errors, any("-source-materials-" in name and name.endswith(".zip") for name in paths)
              and any("-symbols-" in name and name.endswith(".zip") for name in paths), "Corresponding source archive or debug symbols are absent")
    except (OSError, ValueError, KeyError, TypeError, AttributeError, ET.ParseError) as error:
        errors.append(f"Cannot inspect native evidence: {error}")
    result["passed"] = not errors
    return result


def aggregate_native(roots: dict[str, Path], candidate_head: str):
    errors = []
    check(errors, set(roots) == set(PLATFORMS), "Exactly four distinct native platform combinations are required")
    gates = {name: inspect_native(path, expected_head=candidate_head) for name, path in roots.items()}
    identities = [gate.get("identity", {}) for gate in gates.values()]
    for name, gate in gates.items():
        errors.extend(f"{name}: {message}" for message in gate["errors"])
        check(errors, gate.get("platformId") == name, f"Native artifact platform does not match its required slot: {name}")
    if identities:
        for identity in identities[1:]:
            for key in ("candidateHead", "checkoutCommit", "sourceTree", "lockHashes"):
                check(errors, identity.get(key) == identities[0].get(key), f"Native executions disagree on {key}")
        for key in ("modelFingerprint", "extractionFingerprint"):
            check(errors, len({gate.get(key) for gate in gates.values()}) == 1, f"Native executions disagree on {key}")
    return {"schemaVersion": 1, "kind": "native-aggregate", "candidateHead": candidate_head,
            "passed": not errors, "errors": errors, "gates": gates}


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
    root = parent / ("qcopilots-search-" + prefix + "-" + uuid.uuid4().hex)
    root.mkdir(parents=True, exist_ok=False)
    return root


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
            if not re.fullmatch(r"qcopilots-search(?:-[a-z0-9-]+)?", name):
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


def run_local(build_record: Path, parent: Path, kind: str, program_name: str, report_relative: str,
              command: list[str], extra_environment: dict[str, str], inputs: list[Path] | None = None, *,
              working_directory: Path | None = None):
    build = read_json(build_record)
    errors = verified_build(build_record.parent, build)
    if errors or build.get("kind") != "build-execution" or build.get("passed") is not True:
        raise ValueError("Local run requires a verified successful build execution record: " + "; ".join(errors))
    program = build["programs"][program_name]
    producer = Path(program["originalPath"])
    source = Path(build["sourceDirectory"])
    identity = source_identity(source)
    if identity != build["identity"] or digest(producer) != program["sha256"]:
        raise ValueError("The local producer or source changed after its recorded build")
    root = fresh_directory(parent, kind + "-evidence")
    # The command receives a fresh evidence directory. A report cannot be imported
    # after the fact to impersonate an execution bound to this source and binary.
    planned = PurePosixPath(report_relative)
    if report_relative != planned.as_posix() or planned.is_absolute() or ".." in planned.parts or "\\" in report_relative or ":" in report_relative:
        raise ValueError("The report must be a canonical relative POSIX path")
    report = root.joinpath(*planned.parts)
    if report.is_absolute() and not report.resolve().is_relative_to(root):
        raise ValueError("The planned local report must remain in its new evidence directory")
    environment = os.environ.copy()
    environment.update({name: value.replace("{evidence}", str(root)).replace("{report}", str(report)) for name, value in extra_environment.items()})
    expanded = [value.replace("{evidence}", str(root)).replace("{report}", str(report)).replace("{program}", str(producer)) for value in command]
    begin = {"schemaVersion": 1, "kind": kind + "-execution", "identity": identity,
             "buildRecordSha256": digest(build_record), "programName": program_name,
             "producerSha256": program["sha256"], "plannedReport": report_relative,
             "command": expanded, "startedAt": datetime.now(timezone.utc).isoformat()}
    input_hashes = {}
    for index, path in enumerate(inputs or []):
        destination = root / "inputs" / f"{index}-{path.name}"
        destination.parent.mkdir(exist_ok=True)
        shutil.copyfile(path, destination)
        input_hashes[str(path.resolve())] = digest(path)
    begin["inputHashes"] = input_hashes
    write_json(root / "begin.json", begin)
    execution = execute_record(expanded, root, environment=environment, working_directory=working_directory)
    unchanged = source_identity(source) == identity and digest(producer) == program["sha256"]
    unchanged = unchanged and all(digest(Path(path)) == value for path, value in input_hashes.items())
    matches = list(root.glob(report_relative))
    if len(matches) == 1 and matches[0].is_file():
        report = matches[0]
        report_relative = report.relative_to(root).as_posix()
    receipt = {**begin, "execution": execution, "report": report_relative,
               "passed": execution["exitCode"] == 0 and unchanged and report.is_file()}
    # Preserve the exact build provenance and program that existed before launch.
    build_copy = root / "build"
    shutil.copytree(build_record.parent, build_copy)
    receipt["buildRecord"] = "build/" + build_record.name
    receipt["files"] = [file_record(root, path) for path in root.rglob("*") if path.is_file()
                        and (build_copy in path.parents or root / "inputs" in path.parents
                             or path.suffix.lower() in (".json", ".jsonl", ".log", ".xml", ".txt"))]
    write_json(root / "execution-record.json", receipt)
    return root / "execution-record.json", receipt


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
    executables = {}
    with zipfile.ZipFile(package) as archive:
        for name in ("qcopilots-search", "qcopilots-search-service", "qcopilots-search-worker", "qcopilots-search-cli"):
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
    if set(records) != {"candidate", "million", "physical", "computerUse"}:
        raise ValueError("Candidate, million, physical and Computer Use raw evidence units are all required")
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


def inspect_local_run(root: Path, receipt, kind: str, identity: dict):
    errors = verify_manifest(root, receipt)
    check(errors, receipt.get("kind") == kind + "-execution" and receipt.get("passed") is True,
          f"{kind} has no successful recorded execution")
    check(errors, receipt.get("identity") == identity, f"{kind} was executed from a different source identity")
    execution = receipt.get("execution", {})
    check(errors, execution.get("exitCode") == 0, f"{kind} process did not exit successfully")
    start, end = timestamp(execution.get("startedAt")), timestamp(execution.get("finishedAt"))
    check(errors, end > start, f"{kind} process time range is invalid")
    build_path = safe_file(root, receipt["buildRecord"])
    build = read_json(build_path)
    errors.extend(verified_build(build_path.parent, build))
    check(errors, digest(build_path) == receipt.get("buildRecordSha256") and build.get("passed") is True
          and build.get("identity") == identity and build.get("execution", {}).get("exitCode") == 0,
          f"{kind} executable build provenance is missing or stale")
    producer = build.get("programs", {}).get(receipt.get("programName"), {})
    check(errors, producer.get("sha256") == receipt.get("producerSha256"), f"{kind} producer does not match its recorded build")
    names = {item["path"] for item in receipt["files"]}
    for name in (execution.get("stdout"), execution.get("stderr"), receipt.get("report"), receipt.get("buildRecord")):
        check(errors, name in names, f"{kind} required raw execution evidence is not bound: {name}")
    raw = read_json(safe_file(root, receipt["report"]))
    if kind == "physical":
        errors.extend(validate_physical(raw))
        check(errors, start <= timestamp(raw.get("timestampUtc")) <= end, "Physical report is outside its recorded execution")
    else:
        check(errors, raw.get("kind") == "qcopilots-search-million-independent-monitor" and raw.get("passed") is True
              and raw.get("processExitCode") == 0, "Million-vector independent monitor did not pass")
        plan = raw.get("plan", {})
        check(errors, plan.get("benchmarkSha256") == receipt.get("producerSha256"), "Million-vector monitor used a different executable")
        lookup = {item.get("sourcePath", "").replace("\\", "/"): item["path"] for item in receipt["files"]}
        def original(name):
            path = lookup.get(str(plan.get(name, "")).replace("\\", "/"))
            if not path:
                raise ValueError(f"Million-vector original {name} is not included in the immutable evidence unit")
            return safe_file(root, path)
        benchmark_path = original("benchmarkReport")
        check(errors, digest(benchmark_path) == raw.get("benchmarkReportSha256"), "Million-vector raw report hash differs from monitor")
        benchmark = read_json(benchmark_path)
        errors.extend(validate_capacity(benchmark, minimum_segments=1000000, minimum_queries=100, minimum_timings=500))
        for key in ("sampleLog", "stdoutLog", "stderrLog"):
            original(key)
        samples = []
        with original("sampleLog").open(encoding="utf-8-sig") as stream:
            for line in stream:
                if line.strip():
                    samples.append(json.loads(line))
        check(errors, len(samples) == raw.get("sampleCount") and len(samples) > 1,
              "Million-vector independent samples are incomplete")
        if samples:
            sizes = [item.get("recursiveDirectoryBytes") for item in samples]
            free = [item.get("volumeAvailableFreeBytes") for item in samples]
            valid = all(finite(value) and value >= 0 for value in sizes + free)
            check(errors, valid, "Million-vector independent disk samples are invalid")
            if valid:
                check(errors, max(sizes) == raw.get("maximumObservedDirectoryBytes") and min(free) == raw.get("minimumObservedFreeBytes")
                      and max(sizes) <= plan.get("directoryBudgetBytes", -1) and min(free) >= plan.get("freeReserveBytes", math.inf),
                      "Million-vector monitor summaries or budgets disagree with raw disk samples")
            check(errors, all(start <= timestamp(item.get("utc")) <= end for item in samples),
                  "Million-vector sample time is outside its execution")
        check(errors, raw.get("stdoutComplete") is True and raw.get("stderrComplete") is True,
              "Million-vector process output was not completely recorded")
        gates = raw.get("gates", [])
        check(errors, bool(gates) and all(item.get("passed") is True for item in gates), "Million-vector monitor has incomplete or failed gates")
        unchanged = next((item for item in gates if item.get("name") == "executed-binary-unchanged"), {})
        check(errors, unchanged.get("actual") == receipt.get("producerSha256"), "Million-vector producer changed during execution")
        check(errors, start <= timestamp(raw.get("startedUtc")) <= timestamp(raw.get("completedUtc")) <= end,
              "Million-vector monitor time is outside its execution receipt")
    return errors


def preflight(roots: dict[str, Path], local_root: Path, candidate_head: str):
    native = aggregate_native(roots, candidate_head)
    errors = list(native["errors"])
    result = {"schemaVersion": 1, "kind": "release-preflight", "candidateHead": candidate_head,
              "native": native, "localGates": {}, "passed": False, "errors": errors, "published": False}
    try:
        manifest = read_json(local_root / "manifest.json")
        errors.extend(verify_manifest(local_root, manifest))
        identity = manifest["identity"]
        check(errors, identity.get("candidateHead") == candidate_head and identity.get("checkoutCommit") == candidate_head,
              "Local evidence must execute the clean immutable candidate head")
        for gate in native["gates"].values():
            for key in ("candidateHead", "sourceTree", "lockHashes"):
                check(errors, gate.get("identity", {}).get(key) == identity.get(key), f"Local and native {key} differ")
        units = manifest["units"]
        check(errors, set(units) == {"candidate", "million", "physical", "computerUse"}, "Required local evidence units are incomplete")
        candidate_path = safe_file(local_root, units["candidate"])
        candidate = read_json(candidate_path)
        errors.extend(inspect_candidate(candidate_path.parent, candidate))
        check(errors, candidate.get("identity") == identity and candidate.get("kind") == "package-candidate", "Local candidate package is unbound")
        package = safe_file(candidate_path.parent, candidate["package"])
        check(errors, digest(package) == candidate.get("packageSha256"), "Final Windows package content changed")
        for key in ("modelFingerprint", "extractionFingerprint"):
            check(errors, all(gate.get(key) == candidate.get(key) for gate in native["gates"].values()), f"Final candidate {key} differs from native acceptance")
        for kind in ("million", "physical"):
            try:
                path = safe_file(local_root, units[kind])
                failures = inspect_local_run(path.parent, read_json(path), kind, identity)
            except (OSError, ValueError, TypeError, KeyError, AttributeError) as error:
                failures = [f"{kind} source-bound execution is missing: {error}"]
            result["localGates"][kind] = {"passed": not failures, "errors": failures}
            errors.extend(failures)
        cu_path = safe_file(local_root, units["computerUse"])
        cu = read_json(cu_path)
        failures = verify_manifest(cu_path.parent, cu)
        failures.extend(validate_cu(cu, cu_path.parent, identity=identity, package_sha256=candidate["packageSha256"], model_fingerprint=candidate["modelFingerprint"]))
        cu_bound = {item["path"] for item in cu.get("files", [])}
        for scene in cu.get("scenarios", []):
            check(failures, scene.get("operationLog") in cu_bound and all(name in cu_bound for name in scene.get("screenshots", [])),
                  "Computer Use screenshot or operation log is not content-bound")
        check(failures, cu.get("executables") == candidate.get("executables") and cu.get("extractionFingerprint") == candidate.get("extractionFingerprint"),
              "Computer Use does not bind the same final packaged executables and extraction pipeline")
        result["localGates"]["computerUse"] = {"passed": not failures, "errors": failures}
        errors.extend(failures)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        errors.append(f"Local evidence is missing, stale or unbound: {error}")
    result["passed"] = not errors
    return result


def discover_native(parent: Path):
    roots = {}
    for manifest in sorted(parent.glob("*/reports/native-evidence.json")):
        platform_id = read_json(manifest).get("platformId")
        if platform_id not in PLATFORMS or platform_id in roots:
            raise ValueError(f"Unknown or duplicate native artifact combination: {platform_id}")
        roots[platform_id] = manifest.parent.parent
    return roots


def pairs(values):
    result = {}
    for value in values:
        name, separator, item = value.partition("=")
        if not separator or not name or not item or name in result:
            raise ValueError("Expected distinct NAME=VALUE arguments")
        result[name] = item
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    for action in ("validate-native", "aggregate-native", "preflight"):
        command = commands.add_parser(action)
        command.add_argument("--candidate-commit", required=True)
        command.add_argument("--output", required=True, type=Path)
        if action == "validate-native":
            command.add_argument("--artifact-dir", required=True, type=Path)
        else:
            command.add_argument("--native-artifacts-dir", required=True, type=Path)
        if action == "preflight":
            command.add_argument("--local-evidence-dir", required=True, type=Path)
    build = commands.add_parser("record-build")
    build.add_argument("--source", type=Path, default=SOURCE)
    build.add_argument("--output-root", type=Path, required=True)
    build.add_argument("--program", action="append", required=True, help="NAME=absolute executable path, repeat for every producer")
    build.add_argument("--working-directory", type=Path,
                       help="Explicit existing cwd for relative command paths, separate from the evidence directory")
    build.add_argument("command", nargs=argparse.REMAINDER)
    local = commands.add_parser("run-local")
    local.add_argument("--build-record", type=Path, required=True)
    local.add_argument("--output-root", type=Path, required=True)
    local.add_argument("--kind", choices=("million", "physical"), required=True)
    local.add_argument("--program-name", required=True)
    local.add_argument("--report", required=True, help="Fresh relative output path or a glob with exactly one report")
    local.add_argument("--env", action="append", default=[])
    local.add_argument("--input", type=Path, action="append", default=[], help="Capture an external runner script before execution")
    local.add_argument("--working-directory", type=Path,
                       help="Explicit existing cwd for relative command paths, separate from the evidence directory")
    local.add_argument("command", nargs=argparse.REMAINDER)
    package = commands.add_parser("record-package")
    for name in ("build-record", "package", "verification", "output-root"):
        package.add_argument("--" + name, type=Path, required=True)
    package.add_argument("--model-fingerprint", required=True)
    package.add_argument("--extraction-fingerprint", required=True)
    collect = commands.add_parser("collect-local")
    for name in ("candidate", "million", "physical", "computer-use", "output-root"):
        collect.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args(argv)
    gate = args.action in ("validate-native", "aggregate-native", "preflight")
    try:
        if gate:
            if not re.fullmatch(r"[0-9a-f]{40}", args.candidate_commit):
                raise ValueError("The candidate must be a full immutable Git SHA")
            if args.action == "validate-native":
                result = inspect_native(args.artifact_dir.resolve(), expected_head=args.candidate_commit)
            else:
                roots = discover_native(args.native_artifacts_dir.resolve())
                result = (aggregate_native(roots, args.candidate_commit) if args.action == "aggregate-native" else
                          preflight(roots, args.local_evidence_dir.resolve(), args.candidate_commit))
            write_json(args.output, result)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["passed"] else 1
        if args.action in ("record-build", "run-local"):
            command = args.command[1:] if args.command[:1] == ["--"] else args.command
            if not command:
                raise ValueError("An actual command after -- is required")
        if args.action == "record-build":
            path, result = record_build(args.source.resolve(), args.output_root,
                                        {name: Path(value).resolve() for name, value in pairs(args.program).items()}, command,
                                        working_directory=args.working_directory)
        elif args.action == "run-local":
            path, result = run_local(args.build_record.resolve(), args.output_root, args.kind, args.program_name,
                                     args.report, command, pairs(args.env), [path.resolve() for path in args.input],
                                     working_directory=args.working_directory)
        elif args.action == "record-package":
            path, result = record_package(args.build_record.resolve(), args.package.resolve(), args.verification.resolve(),
                                          args.output_root, args.model_fingerprint, args.extraction_fingerprint)
        else:
            path, result = collect_local({"candidate": args.candidate.resolve(), "million": args.million.resolve(),
                                          "physical": args.physical.resolve(), "computerUse": args.computer_use.resolve()}, args.output_root)
        print(json.dumps({"record": str(path), "kind": result["kind"], "passed": result.get("passed"),
                          "note": "Captured provenance is not a release acceptance result"}, ensure_ascii=False, indent=2))
        return 0 if result.get("passed", True) else 1
    except (OSError, ValueError, KeyError, TypeError, AttributeError, subprocess.SubprocessError, zipfile.BadZipFile) as error:
        result = {"schemaVersion": 1, "kind": args.action, "passed": False, "errors": [str(error)], "published": False}
        if gate:
            write_json(args.output, result)
        print(json.dumps(result, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
