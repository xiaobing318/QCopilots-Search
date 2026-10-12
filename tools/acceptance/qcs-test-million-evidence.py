"""Synthetic execution-to-evidence contracts, never a real capacity acceptance.

Only Git identity discovery is replaced. The real build recorder, subprocess
execution, receipt capture, hashes and complete release validators are exercised.
Every synthetic document lives under this test's isolated temporary directory.
"""
from __future__ import annotations

import copy
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location(
    "qcs_million_evidence_contract", HERE.parent / "qcs-release-evidence.py")
EVIDENCE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(EVIDENCE)


def synthetic_capacity():
    """Complete schema fixture, with explicitly synthetic counts and timings."""
    segments, files, per_file = 1_000_000, 100_000, 10
    state = {"vectors": segments, "segments": segments, "files": files,
             "pipelineFingerprint": "synthetic-capacity-768-v1"}
    phases = []
    for phase in ("add", "update", "delete"):
        phases.append({
            "phase": phase, "passed": True, "filesChecked": 1000,
            "keywordQueries": 1000, "vectorQueries": 1000,
            "staleHitCount": 0, "retiredSegmentIdsRemaining": 0,
            "globalSearchStaleHitCount": 0,
            "retiredSegmentIdsChecked": 0 if phase == "add" else 10000,
            "globalSearchQueries": 16, "globalSearchCheckedHits": 16,
            "globalSearchVectorCount": segments + (0 if phase == "delete" else 10000),
            "globalSearchBackend": "ann",
        })
    faults = []
    previous_id = 0
    for point, observed_id, dimension in (
        ("committed-before-ann", 1, 700),
        ("uncommitted-sqlite", 1, 700),
        ("ann-file-saved-before-manifest", 2, 702),
        ("ann-manifest-published-before-outbox-cleanup", 3, 703),
    ):
        faults.append({
            "faultPoint": point, "passed": True, "childExitCode": 73,
            "expectedVersion": f"fixture-version-{observed_id}",
            "observedVersion": f"fixture-version-{observed_id}",
            "expectedToken": f"fixture-token-{observed_id}",
            "observedToken": f"fixture-token-{observed_id}",
            "previousSegmentId": previous_id, "observedSegmentId": observed_id,
            "expectedVectorDimension": dimension, "observedVectorDimension": dimension,
            "observedVectorCosine": 1.0, "vectorMatched": True,
            "staleVersionHits": 0, "oldKeywordHits": 0,
            "uncommittedCorruptionRows": 0, "stalePublishedOutboxEvents": 0,
            "postRecoveryWritePassed": True,
        })
        previous_id = observed_id
    return {
        "syntheticTestOnly": True, "passed": True,
        "segments": segments, "files": files, "segmentsPerFile": per_file,
        "dimensions": 768, "oracleRows": segments,
        "builtStatus": copy.deepcopy(state), "reopenedStatus": copy.deepcopy(state),
        "queries": [{"query": index, "distribution": "clustered" if index % 2 else "uniform",
                     "recallAt20": 1.0, "returnedFiles": 20} for index in range(100)],
        "recallAt20": 1.0, "clusteredRecallAt20": 1.0, "uniformRecallAt20": 1.0,
        "timedQueries": 500, "warmupQueries": 10,
        "latencyMilliseconds": {"p95": 1.0},
        "syntheticTimingMilliseconds": [1.0] * 500,
        "filters": [{"nominalPercent": percent, "passed": True, "checkedQueries": 100,
                     "recallAt20": 1.0, "p95Milliseconds": 1.0}
                    for percent in (10, 1, .1)],
        "concurrentRebuildEvaluated": True,
        "concurrentRebuild": {
            "passed": True, "singleShard": True, "observedBuilds": 1,
            "measuredQueries": 1, "overlappingQueries": 1,
            "timingClock": "steady-clock-relative-nanoseconds",
            "buildIntervals": [{"startNanoseconds": 0, "endNanoseconds": 3_000_000}],
            "queryIntervals": [{"startNanoseconds": 1_000_000, "endNanoseconds": 2_000_000,
                                "durationMilliseconds": 1.0, "overlapsBuild": True}],
            "p50Milliseconds": 1.0, "p95Milliseconds": 1.0, "p99Milliseconds": 1.0,
        },
        "mutations": {"passed": True, "filesPerPhase": 1000, "vectorsPerPhase": 10000,
                      "queryScope": "filtered-exact-file-plus-global-production-search-sample",
                      "phases": phases},
        "recoveryFaultInjectionEvaluated": True, "recoveryFaults": faults,
        "sampledDirectoryPeakBytes": 20, "directoryBudgetBytes": 100,
        "minimumObservedFreeBytes": 1000, "minimumFreeBytes": 100,
    }


SYNTHETIC_PRODUCER = r'''"""Unit-test document producer. Does not index or query anything."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

root, fixture, producer = map(Path, sys.argv[1:4])
assert root.is_dir()
def now():
    return datetime.now(timezone.utc).isoformat()
def write(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()
started = now()
benchmark = root / "fixture.benchmark.json"
samples = root / "fixture.samples.jsonl"
stdout = root / "fixture.stdout.log"
stderr = root / "fixture.stderr.log"
write(benchmark, json.loads(fixture.read_text(encoding="utf-8")))
rows = [{"syntheticTestOnly": True, "utc": now(), "recursiveDirectoryBytes": amount,
         "volumeAvailableFreeBytes": free} for amount, free in ((10, 1100), (20, 1000))]
samples.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
stdout.write_text("Synthetic contract fixture, no capacity workload.\n", encoding="utf-8")
stderr.write_text("", encoding="utf-8")
producer_hash = digest(producer)
write(root / "fixture.monitor.json", {
    "syntheticTestOnly": True, "kind": "qcopilots-search-million-independent-monitor",
    "passed": True, "processExitCode": 0,
    "plan": {"benchmarkSha256": producer_hash, "benchmarkReport": str(benchmark.resolve()),
             "sampleLog": str(samples.resolve()), "stdoutLog": str(stdout.resolve()),
             "stderrLog": str(stderr.resolve()), "directoryBudgetBytes": 100,
             "freeReserveBytes": 100},
    "benchmarkReportSha256": digest(benchmark), "sampleCount": len(rows),
    "maximumObservedDirectoryBytes": 20, "minimumObservedFreeBytes": 1000,
    "stdoutComplete": True, "stderrComplete": True,
    "gates": [{"name": "executed-binary-unchanged", "passed": True, "actual": producer_hash}],
    "startedUtc": started, "completedUtc": now(),
})
print("Synthetic contract completed. This is not capacity acceptance.")
'''


class MillionExecutionEvidenceTests(unittest.TestCase):
    def setUp(self):
        requested = os.environ.get("QCOPILOTS_SEARCH_TEST_ROOT") or None
        if requested and not Path(requested).is_dir():
            raise RuntimeError("QCOPILOTS_SEARCH_TEST_ROOT must already be a directory")
        self.temporary = tempfile.TemporaryDirectory(prefix="qcs-me-", dir=requested)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "源码 with space"
        self.source.mkdir()
        self.cwd = self.root / "任意工作目录 with space"
        self.cwd.mkdir()
        self.reports = self.root / "reports"
        self.reports.mkdir()
        self.producer = self.root / "release" / "qcs-benchmark.py"
        self.producer.parent.mkdir()
        self.fixture = self.root / "synthetic-capacity.json"
        EVIDENCE.write_json(self.fixture, synthetic_capacity())
        self.identity = {
            "candidateHead": "a" * 40, "checkoutCommit": "a" * 40,
            "sourceTree": "b" * 40, "workingTreeModified": False,
            "lockHashes": {name: "c" * 64 for name in EVIDENCE.LOCKS},
        }
        patcher = mock.patch.object(EVIDENCE, "source_identity", return_value=self.identity)
        patcher.start()
        self.addCleanup(patcher.stop)
        # Produce the tiny executable fixture through the actual recorded command.
        build_command = [sys.executable, "-B", "-c",
                         "from pathlib import Path; import sys; "
                         "Path(sys.argv[1]).write_text(sys.argv[2], encoding='utf-8')",
                         str(self.producer), SYNTHETIC_PRODUCER]
        self.build_path, self.build = EVIDENCE.record_build(
            self.source, self.reports, {"qcs-benchmark": self.producer},
            build_command, working_directory=self.cwd)
        self.assertEqual([], EVIDENCE.verified_build(self.build_path.parent, self.build))

    def execute(self, report="*.monitor.json"):
        command = [sys.executable, "-B", "{program}", "{evidence}", str(self.fixture), "{program}"]
        self.receipt_path, self.receipt = EVIDENCE.run_local(
            self.build_path, self.reports, "million", "qcs-benchmark", report,
            command, {}, inputs=[self.fixture], working_directory=self.cwd)
        self.unit = self.receipt_path.parent
        return self.receipt

    def inspect(self):
        return EVIDENCE.inspect_local_run(self.unit, self.receipt, "million", self.identity)

    def rebind(self, path):
        """Keep the outer hash valid so inner semantic checks are exercised."""
        relative = path.relative_to(self.unit).as_posix()
        record = EVIDENCE.file_record(self.unit, path)
        self.receipt["files"] = [record if item["path"] == relative else item
                                 for item in self.receipt["files"]]

    def rewrite_monitor(self, mutate):
        path = self.unit / "fixture.monitor.json"
        document = EVIDENCE.read_json(path)
        mutate(document)
        EVIDENCE.write_json(path, document)
        self.rebind(path)

    def test_real_recorders_and_full_validator_accept_synthetic_contract(self):
        self.execute()
        self.assertTrue(self.receipt["passed"])
        self.assertEqual([], self.inspect())
        self.assertEqual("million-execution", self.receipt["kind"])
        self.assertEqual("fixture.monitor.json", self.receipt["report"])
        self.assertEqual(str(self.cwd.resolve()), self.receipt["execution"]["requestedWorkingDirectory"])
        self.assertTrue(EVIDENCE.read_json(self.unit / "fixture.benchmark.json")["syntheticTestOnly"])

    def test_all_five_raw_files_are_created_inside_and_bound_to_fresh_unit(self):
        self.execute()
        names = {"fixture.monitor.json", "fixture.benchmark.json", "fixture.samples.jsonl",
                 "fixture.stdout.log", "fixture.stderr.log"}
        bound = {item["path"]: item for item in self.receipt["files"]}
        self.assertTrue(names.issubset(bound))
        for name in names:
            self.assertEqual(str((self.unit / name).resolve()), bound[name]["sourcePath"])
            self.assertEqual(EVIDENCE.digest(self.unit / name), bound[name]["sha256"])
        self.assertEqual([], self.inspect())

    def test_unbound_companion_is_rejected_even_if_bytes_still_exist(self):
        self.execute()
        self.receipt["files"] = [item for item in self.receipt["files"]
                                 if item["path"] != "fixture.stderr.log"]
        with self.assertRaisesRegex(ValueError, "original stderrLog.*not included"):
            self.inspect()

    def test_deleted_companion_is_rejected(self):
        self.execute()
        (self.unit / "fixture.samples.jsonl").unlink()
        with self.assertRaisesRegex(ValueError, "Evidence file is missing"):
            self.inspect()

    def test_wrong_original_source_path_is_rejected(self):
        self.execute()
        for item in self.receipt["files"]:
            if item["path"] == "fixture.benchmark.json":
                item["sourcePath"] = str(self.root / "outside.benchmark.json")
        with self.assertRaisesRegex(ValueError, "original benchmarkReport.*not included"):
            self.inspect()

    def test_monitor_using_different_producer_is_rejected(self):
        self.execute()
        self.rewrite_monitor(lambda doc: doc["plan"].update(benchmarkSha256="d" * 64))
        self.assertIn("Million-vector monitor used a different executable", self.inspect())

    def test_receipt_producer_must_match_build(self):
        self.execute()
        self.receipt["producerSha256"] = "e" * 64
        self.assertIn("million producer does not match its recorded build", self.inspect())

    def test_sample_outside_recorded_execution_is_rejected(self):
        self.execute()
        path = self.unit / "fixture.samples.jsonl"
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        rows[0]["utc"] = "2000-01-01T00:00:00+00:00"
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        self.rebind(path)
        self.assertIn("Million-vector sample time is outside its execution", self.inspect())

    def test_modified_companion_is_rejected_by_outer_hash(self):
        self.execute()
        (self.unit / "fixture.stdout.log").write_text("tampered", encoding="utf-8")
        self.assertIn("Evidence content changed: fixture.stdout.log", self.inspect())

    def test_rebound_benchmark_still_requires_monitor_hash(self):
        self.execute()
        path = self.unit / "fixture.benchmark.json"
        doc = EVIDENCE.read_json(path)
        doc["syntheticExtraField"] = "changed after monitor capture"
        EVIDENCE.write_json(path, doc)
        self.rebind(path)
        self.assertIn("Million-vector raw report hash differs from monitor", self.inspect())

    def test_complete_validator_rejects_weakened_capacity_scale(self):
        fixture = synthetic_capacity()
        fixture["queries"] = fixture["queries"][:99]
        fixture["timedQueries"] = 499
        EVIDENCE.write_json(self.fixture, fixture)
        self.execute()
        errors = self.inspect()
        self.assertIn("capacity has too few exact query records", errors)
        self.assertIn("capacity has too few warmed timing queries", errors)

    def test_report_glob_cannot_escape_execution_directory(self):
        with self.assertRaisesRegex(ValueError, "canonical relative POSIX path"):
            self.execute("../*.monitor.json")
        self.assertFalse(list(self.reports.glob("*/fixture.monitor.json")))

    def test_missing_report_glob_cannot_claim_success(self):
        self.execute("missing-*.monitor.json")
        self.assertFalse(self.receipt["passed"])
        with self.assertRaisesRegex(ValueError, "Evidence file is missing"):
            self.inspect()

    def test_producer_changed_after_build_is_rejected_before_execution(self):
        self.producer.write_text("raise RuntimeError('must not execute')", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "producer or source changed"):
            self.execute()
        self.assertFalse(list(self.reports.glob("*/fixture.monitor.json")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
