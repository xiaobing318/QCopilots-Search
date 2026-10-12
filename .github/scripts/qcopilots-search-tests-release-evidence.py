#!/usr/bin/env python3
"""Exercise release evidence validation with synthetic, in-memory counterexamples.

These documents are test inputs, never application acceptance reports. No model,
application process, source-tree temporary directory, or developer profile is used.
"""
from collections import Counter
import base64
from copy import deepcopy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import zipfile

sys.dont_write_bytecode = True
spec = importlib.util.spec_from_file_location(
    "qcopilots_search_release_evidence",
    Path(__file__).resolve().parents[2] / "tools" / "qcopilots-search-release-evidence.py")
evidence = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evidence)


def capacity_fixture():
    """Small validator fixture shaped like the native synthetic-capacity report."""
    status = {"annGeneration": 11, "annVectors": 10000, "files": 1000,
              "generation": 11, "pendingShards": 0, "segments": 10000,
              "vectors": 10000, "recoveryWarning": "", "maintenanceWarning": "",
              "schemaVersion": 2, "pipelineFingerprint": "synthetic-capacity-768-v1"}
    return {
        "formatVersion": 1, "benchmark": "synthetic-capacity", "passed": True,
        "dimensions": 768, "segments": 10000, "files": 1000,
        "segmentsPerFile": 10, "seed": "test-fixture-only",
        "generatorVersion": "splitmix64-v1-cluster64-noise008-uniform10percent",
        "startedUtc": "2026-01-01T00:00:00Z", "finishedUtc": "2026-01-01T00:01:00Z",
        "builtStatus": deepcopy(status), "reopenedStatus": deepcopy(status),
        "queries": [{"query": n, "distribution": "uniform" if n == 0 else "clustered",
                     "recallAt20": 1.0, "returnedFiles": 20, "sourceFile": n}
                    for n in range(10)],
        "recallAt20": 1.0, "uniformRecallAt20": 1.0, "clusteredRecallAt20": 1.0,
        "recallTarget": 0.95, "recallPassed": True,
        "oracleMethod": "SQLite read-only stream, independent FP16 decode, cosine, best segment per file, exact top-20 heaps",
        "oracleRows": 10000, "oracleMilliseconds": 1,
        "warmupQueries": 10, "timedQueries": 20,
        "latencyMilliseconds": {"p50": 20.0, "p95": 30.0, "p99": 40.0},
        "p95TargetMilliseconds": 500, "latencyPassed": True,
        "latencyIncludesQueryEncoding": False,
        "filters": [{"nominalPercent": percent, "checkedQueries": 10,
                     "corpusFiles": 1000, "oracleRows": rows, "oracleMilliseconds": 1,
                     "oracleMethod": "SQLite read-only stream, independent FP16 decode, cosine, best segment per file, exact top-20 heaps",
                     "p95Milliseconds": 30.0, "passed": True, "recallAt20": 1.0}
                    for percent, rows in ((10, 1000), (1, 100), (0.1, 10))],
        "filtersPassed": True, "unfilteredHundredPercentCoveredByBaseline": True,
        "concurrentRebuildEvaluated": True,
        "concurrentRebuild": {"overlappingQueries": 1, "p50Milliseconds": 100.0,
                              "p95Milliseconds": 100.0, "p99Milliseconds": 100.0,
                              "passed": True, "rebuildMilliseconds": 200,
                              "singleShard": True},
        "mutations": {"filesPerPhase": 1000, "vectorsPerPhase": 10000,
                      "passed": True, "addMilliseconds": 10, "updateMilliseconds": 10,
                      "deleteMilliseconds": 10},
        "recoveryFaultInjectionEvaluated": True, "annManifestInterruptionEvaluated": True,
        "publishedManifestOutboxInterruptionEvaluated": True,
        "recoveryFaults": [{"faultPoint": name, "childExitCode": 73, "passed": True}
                           for name in ("committed-before-ann", "uncommitted-sqlite",
                                        "ann-file-saved-before-manifest")]
                          + [{"faultPoint": "ann-manifest-published-before-outbox-cleanup",
                              "childExitCode": 73, "passed": True,
                              "injectedSameShardEvents": 2048, "postRecoveryWritePassed": True,
                              "postRecoveryWriteMilliseconds": 10, "stalePublishedOutboxEvents": 0}],
        "minimumFreeBytes": 3 * 1024**3, "minimumObservedFreeBytes": 5 * 1024**3,
        "directoryBudgetBytes": 7 * 1024**3, "sampledDirectoryPeakBytes": 100 * 1024**2,
        "projectedBytesIncludingTenPercentHeadroom": 110 * 1024**2,
        "pilotBytes": 80 * 1024**2, "pilotSegments": 10000,
        "diskMeasurement": "Sampled at batch and phase boundaries, not a continuous peak measurement",
        "cleanupSucceeded": True, "keep": False,
        "physicalFileTraversalEvaluated": False, "semanticQualityEvaluated": False,
    }


def semantic_fixture():
    """Build 200 labelled validator records without executing or claiming inference."""
    modalities = ("text", "ocr", "image", "pdf", "speech", "environment", "video", "code")
    files = [{"file": f"fixture-{i}.txt", "modality": modality, "segments": 125,
              "sha256": f"{i + 1:064x}", "extractedModalities": {"text": 125},
              "extractedTextCharacters": 1000}
             for i, modality in enumerate(modalities)]
    queries = []
    counts = Counter()
    for n in range(200):
        i = n % len(modalities)
        language = "en" if (n // len(modalities)) % 2 == 0 else "zh"
        name = files[i]["file"]
        queries.append({"query": f"validator fixture {n}", "language": language,
                        "modality": modalities[i], "expectedFile": name, "rank": 1,
                        "encodingMilliseconds": 1, "retrievalMilliseconds": 2,
                        "productionTop10": [{"file": name, "score": 1 / 61, "source": "semantic"}],
                        "exactSemanticTop10": [{"file": name, "cosine": 0.9}]})
        counts["overall"] += 1
        counts[f"language:{language}"] += 1
        counts[f"modality:{modalities[i]}"] += 1
    metrics = {key: {"queries": count, "hits": count, "recallAt10": 1.0,
                     "exactSemanticRecallAt10": 1.0, "passed": True,
                     "requiredRecallAt10": 0.9 if key == "overall" else 0.8}
               for key, count in counts.items()}
    return {
        "schema": 1, "mode": "release", "status": "passed", "passed": True,
        "actualEmbeddedSegments": 1000, "completedFiles": len(files), "fixtures": len(files),
        "files": files, "queries": queries, "positiveQueries": 200, "metrics": metrics,
        "modelFingerprint": "qcopilots-search-embedding-v1:" + "1" * 64,
        "extractionFingerprint": "qcopilots-search-extraction-v3:" + "2" * 64,
        "fixtureManifestSha256": "3" * 64, "generatorSha256": "4" * 64,
        "topicManifestSha256": "5" * 64,
        "annotationProtocol": "Synthetic validator fixture, not acceptance evidence.",
        "corpusScope": "Synthetic validator fixture, not an executed corpus.",
        "noAnswerInterpretation": "Ranked candidates do not assert calibrated rejection.",
        "noAnswerQueries": [{"partition": partition, "query": f"absent fixture {partition} {n}",
                             "returnedCount": 1,
                             "exactSemanticTop10": [{"file": files[0]["file"], "cosine": 0.2}]}
                            for partition in ("calibration", "holdout") for n in range(10)],
        "noAnswerSummary": {partition: {"queries": 10, "queriesReturningCandidates": 10,
                                         "minimumTopCosine": 0.2, "maximumTopCosine": 0.2,
                                         "meanTopCosine": 0.2}
                            for partition in ("calibration", "holdout")},
        "startedAt": "2026-01-01T00:00:00Z", "finishedAt": "2026-01-01T00:01:00Z",
    }


def physical_fixture():
    return {"schema": 1, "scenario": "physical-file-capacity", "passed": True,
            "createdPhysicalFiles": 100000, "remainingPhysicalFiles": 100000,
            "generatedFixture": True, "cleanupVerified": True, "staleResults": 0,
            "added": 100, "modified": 100, "deleted": 100,
            "cancelledAfterDiscoveredFiles": 100, "createMs": 1000, "scanMs": 200,
            "exclusionCancellationAndReconcileMs": 300, "payloadBytes": 1000000,
            "timestampUtc": "2026-01-01T00:00:00Z"}


class CapacityEvidenceTests(unittest.TestCase):
    def inspect(self, doc, **thresholds):
        limits = {"minimum_segments": 10000, "minimum_queries": 10, "minimum_timings": 20}
        limits.update(thresholds)
        result = evidence.validate_capacity(doc, **limits)
        self.assertIsInstance(result, list)
        self.assertTrue(all(isinstance(error, str) for error in result))
        return result

    def test_complete_small_fixture_meets_only_its_explicit_thresholds(self):
        self.assertEqual(self.inspect(capacity_fixture()), [])

    def test_smoke_cannot_claim_million_segment_acceptance(self):
        self.assertTrue(self.inspect(capacity_fixture(), minimum_segments=1000000))

    def test_too_few_oracle_queries_rejected(self):
        self.assertTrue(self.inspect(capacity_fixture(), minimum_queries=100))

    def test_too_few_timed_queries_rejected(self):
        self.assertTrue(self.inspect(capacity_fixture(), minimum_timings=500))

    def test_passed_alone_is_not_evidence(self):
        self.assertTrue(self.inspect({"passed": True}))

    def test_missing_filters_concurrency_and_recovery_rejected(self):
        for field in ("filters", "concurrentRebuild", "recoveryFaults"):
            with self.subTest(field=field):
                doc = capacity_fixture()
                del doc[field]
                self.assertTrue(self.inspect(doc))

    def test_missing_or_repeated_filter_selectivity_rejected(self):
        for replacement in ([], [capacity_fixture()["filters"][0]] * 3):
            doc = capacity_fixture()
            doc["filters"] = replacement
            self.assertTrue(self.inspect(doc))

    def test_no_query_overlap_cannot_claim_concurrent_rebuild(self):
        doc = capacity_fixture()
        doc["concurrentRebuild"]["overlappingQueries"] = 0
        self.assertTrue(self.inspect(doc))

    def test_unexecuted_recovery_fault_rejected(self):
        doc = capacity_fixture()
        doc["recoveryFaults"][0]["childExitCode"] = 0
        self.assertTrue(self.inspect(doc))

    def test_stale_outbox_or_failed_post_recovery_write_rejected(self):
        for field, value in (("stalePublishedOutboxEvents", 1), ("postRecoveryWritePassed", False)):
            doc = capacity_fixture()
            doc["recoveryFaults"][-1][field] = value
            self.assertTrue(self.inspect(doc))

    def test_nonfinite_metric_values_rejected(self):
        for value in (float("nan"), float("inf"), float("-inf")):
            for container, field in ((None, "recallAt20"), ("latencyMilliseconds", "p95"),
                                     ("concurrentRebuild", "p95Milliseconds")):
                with self.subTest(value=value, container=container, field=field):
                    doc = capacity_fixture()
                    (doc if container is None else doc[container])[field] = value
                    self.assertTrue(self.inspect(doc))

    def test_filtered_query_metrics_are_required(self):
        for field, value in (("checkedQueries", 9), ("recallAt20", 0.94),
                             ("p95Milliseconds", 501), ("recallAt20", float("nan"))):
            with self.subTest(field=field, value=value):
                doc = capacity_fixture()
                doc["filters"][0][field] = value
                self.assertTrue(self.inspect(doc))

    def test_each_recovery_fault_must_be_distinct_and_present(self):
        doc = capacity_fixture()
        doc["recoveryFaults"][1] = deepcopy(doc["recoveryFaults"][0])
        self.assertTrue(self.inspect(doc))

    def test_disk_budget_and_reserve_cannot_be_ignored(self):
        for field, value in (("sampledDirectoryPeakBytes", 8 * 1024**3),
                             ("minimumObservedFreeBytes", 2 * 1024**3)):
            doc = capacity_fixture()
            doc[field] = value
            self.assertTrue(self.inspect(doc))

    def test_query_recall_must_match_summary(self):
        doc = capacity_fixture()
        doc["queries"][0]["recallAt20"] = 0.0
        self.assertTrue(self.inspect(doc))

    def test_oracle_query_ids_must_be_distinct_valid_integers(self):
        for value in (1, None, "", True, -1, 1.5):
            with self.subTest(value=value):
                doc = capacity_fixture()
                doc["queries"][0]["query"] = value
                self.assertTrue(self.inspect(doc))

    def test_repeating_small_oracle_set_cannot_satisfy_larger_query_minimum(self):
        doc = capacity_fixture()
        doc["queries"] *= 10
        for filtered in doc["filters"]:
            filtered["checkedQueries"] = 100
        self.assertTrue(self.inspect(doc, minimum_queries=100))

    def test_reloaded_segment_count_must_match_written_count(self):
        doc = capacity_fixture()
        doc["reopenedStatus"]["segments"] -= 1
        self.assertTrue(self.inspect(doc))

    def test_exact_oracle_must_cover_every_written_segment(self):
        doc = capacity_fixture()
        doc["oracleRows"] -= 1
        self.assertTrue(self.inspect(doc))

    def test_unexpected_document_and_nested_shapes_return_errors(self):
        for value in (None, [], "passed", True, 1):
            with self.subTest(root=value):
                self.assertTrue(self.inspect(value))
        for field in ("builtStatus", "reopenedStatus", "latencyMilliseconds", "concurrentRebuild", "mutations"):
            for value in (None, [], "bad-shape"):
                with self.subTest(field=field, value=value):
                    doc = capacity_fixture()
                    doc[field] = value
                    self.assertTrue(self.inspect(doc))
        for field in ("queries", "filters", "recoveryFaults"):
            for value in (None, {}, "bad-shape", [None]):
                with self.subTest(field=field, value=value):
                    doc = capacity_fixture()
                    doc[field] = value
                    self.assertTrue(self.inspect(doc))

    def test_latency_cannot_raise_its_own_release_target(self):
        doc = capacity_fixture()
        doc["latencyMilliseconds"]["p95"] = 700
        doc["latencyMilliseconds"]["p99"] = 800
        doc["p95TargetMilliseconds"] = 1000
        self.assertTrue(self.inspect(doc))

    def test_boolean_or_string_is_not_a_numeric_observation(self):
        for value in (True, "10000"):
            doc = capacity_fixture()
            doc["segments"] = value
            self.assertTrue(self.inspect(doc))


class SemanticEvidenceTests(unittest.TestCase):
    def test_complete_synthetic_validation_fixture(self):
        self.assertEqual(evidence.validate_semantic(semantic_fixture()), [])

    def test_passed_alone_is_not_model_quality_evidence(self):
        self.assertTrue(evidence.validate_semantic({"passed": True}))

    def test_corpus_and_query_minimums_are_enforced(self):
        for field, value in (("actualEmbeddedSegments", 999), ("positiveQueries", 199)):
            with self.subTest(field=field):
                doc = semantic_fixture()
                doc[field] = value
                self.assertTrue(evidence.validate_semantic(doc))

    def test_query_rank_must_match_actual_returned_candidates(self):
        doc = semantic_fixture()
        doc["queries"][0]["productionTop10"] = []
        self.assertTrue(evidence.validate_semantic(doc))

    def test_summary_hits_must_match_query_records(self):
        doc = semantic_fixture()
        doc["metrics"]["overall"]["hits"] = 199
        self.assertTrue(evidence.validate_semantic(doc))

    def test_summary_query_count_must_match_records(self):
        doc = semantic_fixture()
        doc["queries"].pop()
        self.assertTrue(evidence.validate_semantic(doc))

    def test_positive_query_text_must_be_nonempty_and_distinct(self):
        for value in ("validator fixture 1", "", "  ", None, True):
            with self.subTest(value=value):
                doc = semantic_fixture()
                doc["queries"][0]["query"] = value
                self.assertTrue(evidence.validate_semantic(doc))

    def test_missing_language_or_modality_cannot_hide_a_group(self):
        for key in ("language:zh", "modality:ocr", "modality:environment", "modality:code"):
            doc = semantic_fixture()
            del doc["metrics"][key]
            self.assertTrue(evidence.validate_semantic(doc))

    def test_no_answer_evidence_is_required(self):
        doc = semantic_fixture()
        doc["noAnswerQueries"] = []
        self.assertTrue(evidence.validate_semantic(doc))

    def test_no_answer_summary_must_match_records(self):
        doc = semantic_fixture()
        doc["noAnswerSummary"]["holdout"]["queries"] = 1
        self.assertTrue(evidence.validate_semantic(doc))

    def test_nonfinite_recall_and_cosine_rejected(self):
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value):
                doc = semantic_fixture()
                doc["metrics"]["overall"]["recallAt10"] = value
                self.assertTrue(evidence.validate_semantic(doc))
                doc = semantic_fixture()
                doc["queries"][0]["exactSemanticTop10"][0]["cosine"] = value
                self.assertTrue(evidence.validate_semantic(doc))

    def test_exact_summary_must_match_independent_oracle_records(self):
        doc = semantic_fixture()
        doc["queries"][0]["exactSemanticTop10"] = []
        self.assertTrue(evidence.validate_semantic(doc))

    def test_group_recall_cannot_be_hidden_by_overall_success(self):
        doc = semantic_fixture()
        for row in doc["queries"]:
            if row["modality"] == "environment":
                row["productionTop10"] = []
                row["rank"] = 0
        # Even internally consistent summaries cannot override the fixed 0.8 group gate.
        for key, metric in doc["metrics"].items():
            rows = [row for row in doc["queries"] if key == "overall" or
                    key == "language:" + row["language"] or key == "modality:" + row["modality"]]
            metric["hits"] = sum(row["rank"] > 0 for row in rows)
            metric["recallAt10"] = metric["hits"] / len(rows)
            metric["requiredRecallAt10"] = 0
        self.assertTrue(evidence.validate_semantic(doc))

    def test_unexpected_document_and_nested_shapes_return_errors(self):
        for value in (None, [], "passed", True, 1):
            with self.subTest(root=value):
                self.assertTrue(evidence.validate_semantic(value))
        for field in ("queries", "noAnswerQueries"):
            for value in (None, {}, "bad-shape", [None]):
                with self.subTest(field=field, value=value):
                    doc = semantic_fixture()
                    doc[field] = value
                    self.assertTrue(evidence.validate_semantic(doc))
        for field in ("metrics", "noAnswerSummary"):
            doc = semantic_fixture()
            doc[field] = None
            self.assertTrue(evidence.validate_semantic(doc))
        for field in ("productionTop10", "exactSemanticTop10"):
            for value in (None, {}, "bad-shape", [None]):
                with self.subTest(query_field=field, value=value):
                    doc = semantic_fixture()
                    doc["queries"][0][field] = value
                    self.assertTrue(evidence.validate_semantic(doc))


class PhysicalEvidenceTests(unittest.TestCase):
    def test_complete_synthetic_validation_fixture(self):
        self.assertEqual(evidence.validate_physical(physical_fixture()), [])

    def test_passed_alone_is_not_physical_traversal_evidence(self):
        self.assertTrue(evidence.validate_physical({"passed": True}))

    def test_physical_file_minimum_is_enforced(self):
        doc = physical_fixture()
        doc["createdPhysicalFiles"] = 99999
        self.assertTrue(evidence.validate_physical(doc))

    def test_custom_minimum_is_explicit(self):
        doc = physical_fixture()
        doc["createdPhysicalFiles"] = doc["remainingPhysicalFiles"] = 1000
        self.assertEqual(evidence.validate_physical(doc, minimum_files=1000), [])

    def test_stale_result_cleanup_and_unexecuted_mutations_rejected(self):
        for field, value in (("staleResults", 1), ("cleanupVerified", False),
                             ("added", 0), ("modified", 0), ("deleted", 0),
                             ("cancelledAfterDiscoveredFiles", 0)):
            with self.subTest(field=field):
                doc = physical_fixture()
                doc[field] = value
                self.assertTrue(evidence.validate_physical(doc))

    def test_nonfinite_or_non_numeric_counts_rejected(self):
        for value in (float("nan"), float("inf"), True, "100000"):
            doc = physical_fixture()
            doc["createdPhysicalFiles"] = value
            self.assertTrue(evidence.validate_physical(doc))

    def test_unexpected_document_shape_returns_errors(self):
        for value in (None, [], "passed", True, 1):
            with self.subTest(value=value):
                self.assertTrue(evidence.validate_physical(value))


class JsonEvidenceTests(unittest.TestCase):
    def test_nonfinite_json_constants_are_not_accepted(self):
        with tempfile.TemporaryDirectory(prefix="qcopilots-search-evidence-中文 ",
                dir=os.environ.get("QCOPILOTS_SEARCH_TEST_ROOT")) as temporary:
            path = Path(temporary) / "qcopilots-search-fixture.json"
            for constant in ("NaN", "Infinity", "-Infinity"):
                with self.subTest(constant=constant):
                    path.write_text('{"value":' + constant + '}', encoding="utf-8")
                    with self.assertRaises(ValueError):
                        evidence.read_json(path)

    def test_unicode_and_bom_are_portable_from_an_explicit_path(self):
        with tempfile.TemporaryDirectory(prefix="qcopilots-search-evidence-中文 ",
                dir=os.environ.get("QCOPILOTS_SEARCH_TEST_ROOT")) as temporary:
            path = Path(temporary) / "带空格 fixture.json"
            path.write_text('{"note":"测试证据"}', encoding="utf-8-sig")
            self.assertEqual(evidence.read_json(path), {"note": "测试证据"})


class ManifestEvidenceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="qcopilots-search-manifest-中文 ",
            dir=os.environ.get("QCOPILOTS_SEARCH_TEST_ROOT"))
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "reports" / "evidence 中文.json"
        self.path.parent.mkdir()
        self.payload = b'{"note":"synthetic validator input"}\n'
        self.path.write_bytes(self.payload)
        self.manifest = {
            "schemaVersion": 1, "kind": "native", "platformId": "windows-x64",
            "identity": {"candidateHead": "1" * 40, "checkoutCommit": "2" * 40,
                         "sourceTree": "3" * 40, "workingTreeModified": False,
                         "lockHashes": {name: "4" * 64 for name in evidence.LOCKS}},
            "files": [{"path": self.path.relative_to(self.root).as_posix(),
                       "bytes": len(self.payload), "sha256": hashlib.sha256(self.payload).hexdigest()}],
        }

    def inspect(self):
        result = evidence.verify_manifest(self.root, self.manifest)
        self.assertIsInstance(result, list)
        self.assertTrue(all(isinstance(error, str) for error in result))
        return result

    def test_bound_content_and_clean_source_identity_pass(self):
        self.assertEqual(self.inspect(), [])

    def test_equal_size_content_tampering_is_detected(self):
        # Preserve the size so a length-only integrity check cannot pass.
        self.path.write_bytes(self.payload.replace(b"input", b"other"))
        self.assertTrue(self.inspect())

    def test_wrong_size_is_detected_even_with_correct_digest(self):
        self.manifest["files"][0]["bytes"] += 1
        self.assertTrue(self.inspect())

    def test_missing_report_cannot_reuse_stored_success(self):
        self.path.unlink()
        self.assertTrue(self.inspect())

    def test_empty_or_duplicate_file_records_rejected(self):
        original = deepcopy(self.manifest["files"])
        for files in ([], original * 2):
            self.manifest["files"] = files
            self.assertTrue(self.inspect())

    def test_modified_or_unknown_source_tree_is_not_release_evidence(self):
        for value in (True, None, 0, "false"):
            with self.subTest(value=value):
                self.manifest["identity"]["workingTreeModified"] = value
                self.assertTrue(self.inspect())

    def test_each_dependency_lock_fingerprint_is_required(self):
        original = deepcopy(self.manifest["identity"]["lockHashes"])
        for missing in evidence.LOCKS:
            with self.subTest(missing=missing):
                self.manifest["identity"]["lockHashes"] = deepcopy(original)
                del self.manifest["identity"]["lockHashes"][missing]
                self.assertTrue(self.inspect())

    def test_invalid_commit_tree_or_lock_digest_is_rejected(self):
        for field in ("candidateHead", "checkoutCommit", "sourceTree"):
            original = self.manifest["identity"][field]
            self.manifest["identity"][field] = "not-a-commit"
            self.assertTrue(self.inspect())
            self.manifest["identity"][field] = original
        self.manifest["identity"]["lockHashes"][evidence.LOCKS[0]] = "0" * 63
        self.assertTrue(self.inspect())

    def test_path_traversal_absolute_and_drive_paths_are_rejected(self):
        for path in ("../outside.json", "/absolute.json", "C:/absolute.json",
                     "reports/../../outside.json", "reports\\evidence 中文.json",
                     "reports/evidence 中文.json:stream"):
            with self.subTest(path=path):
                self.manifest["files"][0]["path"] = path
                self.assertTrue(self.inspect())
                with self.assertRaises(ValueError):
                    evidence.safe_file(self.root, path)

    def test_noncanonical_relative_aliases_are_rejected(self):
        for path in ("reports/./evidence 中文.json", "reports//evidence 中文.json",
                     "./reports/evidence 中文.json", "reports/evidence 中文.json/"):
            with self.subTest(path=path):
                with self.assertRaises(ValueError):
                    evidence.safe_file(self.root, path)

    def test_symlinks_are_rejected_without_requiring_host_privilege(self):
        # Simulate the filesystem type observation, not the checker or its result.
        original = Path.is_symlink
        target = self.path.resolve()
        with mock.patch.object(Path, "is_symlink", lambda path: path == target or original(path)):
            with self.assertRaises(ValueError):
                evidence.safe_file(self.root, self.path.relative_to(self.root).as_posix())
            self.assertTrue(self.inspect())

    def test_system_ancestor_link_is_outside_the_evidence_boundary(self):
        # macOS temporary roots may themselves live beneath the system /var link.
        # Only links within the selected evidence root can redirect an evidence name.
        original = Path.is_symlink
        ancestor = self.root.parent.parent
        with mock.patch.object(Path, "is_symlink", lambda path: path == ancestor or original(path)):
            self.assertEqual(evidence.safe_file(self.root, self.path.relative_to(self.root).as_posix()),
                             self.path.resolve())

    def test_malformed_manifests_return_errors(self):
        for malformed in (None, [], {"identity": None}, {"identity": [], "files": []},
                          {"identity": deepcopy(self.manifest["identity"]), "files": [None]}):
            with self.subTest(manifest=malformed):
                self.assertTrue(evidence.verify_manifest(self.root, malformed))


class ComputerUseEvidenceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="qcopilots-search-cu-validator-中文 ",
            dir=os.environ.get("QCOPILOTS_SEARCH_TEST_ROOT"))
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        # A tiny synthetic PNG is only a validator fixture, not a captured application.
        self.picture = self.root / "synthetic-validator-fixture.png"
        self.picture.write_bytes(base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jR1cAAAAASUVORK5CYII="))
        self.log = self.root / "synthetic-validator-operations.log"
        self.log.write_text("Synthetic validator fixture. No user operation occurred.\n", encoding="utf-8")
        self.identity = {"candidateHead": "1" * 40, "checkoutCommit": "2" * 40,
                         "sourceTree": "3" * 40, "workingTreeModified": False,
                         "lockHashes": {name: "4" * 64 for name in evidence.LOCKS}}
        self.package = "5" * 64
        self.fingerprint = "qcopilots-search-embedding-v1:" + "6" * 64
        self.executables = {name: {"sha256": f"{n + 1:064x}", "packageMember": name + ".exe"}
                            for n, name in enumerate(("qcopilots-search", "qcopilots-search-service",
                                                      "qcopilots-search-worker", "qcopilots-search-cli"))}
        self.doc = {
            "schemaVersion": 1, "kind": "windows-computer-use", "passed": True,
            "identity": deepcopy(self.identity), "commit": self.identity["checkoutCommit"],
            "packageSha256": self.package, "modelFingerprint": self.fingerprint,
            "executables": deepcopy(self.executables),
            "startedAt": "2026-01-01T00:00:00Z", "finishedAt": "2026-01-01T00:30:00Z",
            "scenarios": [{"id": f"CU-{n:02}", "status": "passed",
                           "expected": "Synthetic validator expectation",
                           "actual": "Synthetic validator observation",
                           "actions": ["Synthetic validator action"],
                           "packageSha256": self.package, "executables": deepcopy(self.executables),
                           "startedAt": f"2026-01-01T00:{n:02}:00Z",
                           "finishedAt": f"2026-01-01T00:{n:02}:30Z",
                           "screenshots": [self.picture.name], "operationLog": self.log.name}
                          for n in range(1, 11)],
        }

    def inspect(self):
        result = evidence.validate_cu(self.doc, self.root, identity=self.identity,
                                      package_sha256=self.package, model_fingerprint=self.fingerprint)
        self.assertIsInstance(result, list)
        self.assertTrue(all(isinstance(error, str) for error in result))
        return result

    def test_complete_validator_fixture_has_bound_scenarios(self):
        self.assertEqual(self.inspect(), [])

    def test_passed_flag_without_operations_is_incomplete(self):
        self.doc = {"passed": True}
        self.assertTrue(self.inspect())

    def test_all_ten_distinct_scenarios_are_required(self):
        for scenarios in (self.doc["scenarios"][:-1], [self.doc["scenarios"][0]] * 10):
            self.doc["scenarios"] = scenarios
            self.assertTrue(self.inspect())

    def test_non_passed_and_empty_actual_operations_are_rejected(self):
        original = deepcopy(self.doc)
        for field, value in (("status", "not-run"), ("expected", ""),
                             ("actual", "  "), ("actions", [])):
            self.doc = deepcopy(original)
            self.doc["scenarios"][0][field] = value
            self.assertTrue(self.inspect())

    def test_stale_commit_model_or_package_is_rejected(self):
        original = deepcopy(self.doc)
        for field, value in (("commit", "0" * 40), ("packageSha256", "0" * 64),
                             ("modelFingerprint", "different-model")):
            self.doc = deepcopy(original)
            self.doc[field] = value
            self.assertTrue(self.inspect())

    def test_source_identity_must_match_candidate(self):
        self.doc["identity"]["sourceTree"] = "0" * 40
        self.assertTrue(self.inspect())

    def test_scene_cannot_mix_another_package_or_executable(self):
        original = deepcopy(self.doc)
        self.doc["scenarios"][0]["packageSha256"] = "0" * 64
        self.assertTrue(self.inspect())
        self.doc = original
        self.doc["scenarios"][0]["executables"]["qcopilots-search"]["sha256"] = "0" * 64
        self.assertTrue(self.inspect())

    def test_session_and_scene_timestamp_ranges_are_enforced(self):
        original = deepcopy(self.doc)
        for field, value in (("startedAt", "2025-12-31T23:59:00Z"),
                             ("finishedAt", "2026-01-01T00:31:00Z"),
                             ("finishedAt", "2026-01-01T00:01:00Z"),
                             ("startedAt", "2026-01-01T00:01:00")):
            self.doc = deepcopy(original)
            self.doc["scenarios"][0][field] = value
            self.assertTrue(self.inspect())
        self.doc = original
        self.doc["finishedAt"] = self.doc["startedAt"]
        self.assertTrue(self.inspect())

    def test_each_scene_requires_an_existing_screenshot(self):
        original = deepcopy(self.doc)
        self.doc["scenarios"][0]["screenshots"] = []
        self.assertTrue(self.inspect())
        self.doc = original
        self.picture.unlink()
        self.assertTrue(self.inspect())

    def test_empty_or_wrong_format_screenshot_is_rejected(self):
        for payload in (b"", b"not a screenshot" * 10):
            self.picture.write_bytes(payload)
            self.assertTrue(self.inspect())

    def test_screenshot_cannot_escape_the_evidence_root(self):
        self.doc["scenarios"][0]["screenshots"] = ["../other.png"]
        self.assertTrue(self.inspect())

    def test_missing_or_empty_operation_log_rejected(self):
        self.log.write_bytes(b"")
        self.assertTrue(self.inspect())
        self.log.unlink()
        self.assertTrue(self.inspect())

    def test_malformed_scenarios_return_errors(self):
        original = deepcopy(self.doc)
        for value in (None, {}, [None], "passed"):
            with self.subTest(value=value):
                self.doc = deepcopy(original)
                self.doc["scenarios"] = value
                self.assertTrue(self.inspect())


class NativeAggregateEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.candidate = "1" * 40
        self.identity = {"candidateHead": self.candidate, "checkoutCommit": "2" * 40,
                         "sourceTree": "3" * 40, "workingTreeModified": False,
                         "lockHashes": {name: "4" * 64 for name in evidence.LOCKS}}
        # No filesystem access is needed to exercise aggregation of already inspected units.
        self.roots = {name: Path(name) for name in evidence.PLATFORMS}
        self.gates = {name: {"passed": True, "errors": [], "platformId": name,
                             "identity": deepcopy(self.identity), "modelFingerprint": "model-fingerprint",
                             "extractionFingerprint": "extraction-fingerprint"}
                      for name in self.roots}

    def inspect(self):
        def inspect_platform(path, *, expected_head):
            self.assertEqual(expected_head, self.candidate)
            return deepcopy(self.gates[path.name])
        with mock.patch.object(evidence, "inspect_native", side_effect=inspect_platform):
            return evidence.aggregate_native(self.roots, self.candidate)

    def test_four_complete_consistent_platforms_pass(self):
        self.assertTrue(self.inspect()["passed"])

    def test_missing_platform_cannot_pass(self):
        del self.roots["macos-x64"]
        self.assertFalse(self.inspect()["passed"])

    def test_duplicate_platform_payload_cannot_fill_a_different_slot(self):
        self.roots["macos-x64"] = Path("macos-arm64")
        self.assertFalse(self.inspect()["passed"])

    def test_single_platform_errors_are_preserved(self):
        self.gates["linux-x64"]["passed"] = False
        self.gates["linux-x64"]["errors"] = ["required scenario not run"]
        result = self.inspect()
        self.assertFalse(result["passed"])
        self.assertTrue(any("required scenario not run" in error for error in result["errors"]))

    def test_mismatched_source_and_dependency_fingerprints_rejected(self):
        original = deepcopy(self.gates)
        for field, value in (("candidateHead", "0" * 40), ("checkoutCommit", "0" * 40),
                             ("sourceTree", "0" * 40), ("lockHashes", {})):
            with self.subTest(field=field):
                self.gates = deepcopy(original)
                self.gates["linux-x64"]["identity"][field] = value
                self.assertFalse(self.inspect()["passed"])

    def test_mismatched_model_or_extraction_fingerprints_rejected(self):
        original = deepcopy(self.gates)
        for field in ("modelFingerprint", "extractionFingerprint"):
            self.gates = deepcopy(original)
            self.gates["macos-arm64"][field] = "different-fingerprint"
            self.assertFalse(self.inspect()["passed"])


class ExecutionProvenanceTests(unittest.TestCase):
    """Run tiny fixture commands in a fresh Git repository, never the application."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="qcopilots-search-provenance-中文 ",
            dir=os.environ.get("QCOPILOTS_SEARCH_TEST_ROOT"))
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "synthetic source 中文"
        self.source.mkdir()
        self.hooks = self.root / "empty-git-hooks"
        self.hooks.mkdir()
        self.environment = os.environ.copy()
        for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"):
            self.environment.pop(name, None)
        self.git("init", "--quiet", "--initial-branch=main")
        for name in evidence.LOCKS:
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("synthetic validator dependency lock\n", encoding="utf-8")
        self.git("add", "--all")
        self.git("commit", "--quiet", "-m", "Synthetic validator fixture")
        self.output = self.root / "synthetic programs"
        self.output.mkdir()
        self.programs = {name: self.output / (name + ".exe") for name in (
            "qcopilots-search", "qcopilots-search-service", "qcopilots-search-worker", "qcopilots-search-cli")}
        self.build_command = [sys.executable, "-c",
            "import pathlib,sys\nfor name in sys.argv[1:]:\n p=pathlib.Path(name)\n p.write_bytes(b'SYNTHETIC VALIDATOR FIXTURE '+p.name.encode())",
            *[str(path) for path in self.programs.values()]]

    def git(self, *arguments):
        return subprocess.run(["git", "-c", "user.name=Validator fixture",
            "-c", "user.email=validator@example.invalid", "-c", "commit.gpgsign=false",
            "-c", "core.autocrlf=false", "-c", "core.fsmonitor=false",
            "-c", f"core.hooksPath={self.hooks}", "-C", str(self.source), *arguments],
            env=self.environment, capture_output=True, text=True, encoding="utf-8", check=True)

    def build(self):
        return evidence.record_build(self.source, self.root / "recorded builds",
                                     self.programs, self.build_command)

    def candidate(self):
        build_path, build = self.build()
        package = self.root / "qcopilots-search-synthetic-validator.zip"
        with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_STORED) as archive:
            for name, path in self.programs.items():
                archive.write(path, "bin/" + name + ".exe")
        verification = self.root / "synthetic-package-verification.json"
        verification.write_text(json.dumps({"passed": True,
            "commit": build["identity"]["checkoutCommit"], "workingTreeModified": False,
            "packageSha256": hashlib.sha256(package.read_bytes()).hexdigest(),
            "checks": [{"name": f"synthetic-validator-{n}", "passed": True} for n in range(9)]}),
            encoding="utf-8")
        return evidence.record_package(build_path, package, verification,
            self.root / "recorded candidates", "synthetic-model-fingerprint", "synthetic-extraction-fingerprint")

    def local_command(self, suffix="", *extra_arguments):
        return [sys.executable, "-c",
            "import datetime,json,pathlib,sys\n"
            "report=json.loads(sys.argv[2])\n"
            "report['timestampUtc']=datetime.datetime.now(datetime.timezone.utc).isoformat()\n"
            "pathlib.Path(sys.argv[1]).write_text(json.dumps(report),encoding='utf-8')\n" + suffix,
            "{report}", json.dumps(physical_fixture()), *extra_arguments]

    def run_local(self, build_path, command=None):
        return evidence.run_local(build_path, self.root / "recorded executions", "physical",
            "qcopilots-search-cli", "synthetic-physical-validator.json", command or self.local_command(), {})

    def test_clean_fixture_build_and_package_provenance_are_inspectable(self):
        path, candidate = self.candidate()
        self.assertEqual(evidence.inspect_candidate(path.parent, candidate), [])
        build_path = path.parent / candidate["buildProvenance"]["path"]
        self.assertEqual(evidence.verified_build(build_path.parent, evidence.read_json(build_path)), [])

    def test_candidate_archive_tampering_is_detected(self):
        path, candidate = self.candidate()
        with (path.parent / candidate["package"]).open("ab") as stream:
            stream.write(b"Synthetic tamper")
        self.assertTrue(evidence.inspect_candidate(path.parent, candidate))

    def test_stale_build_hash_and_unbound_candidate_files_are_rejected(self):
        path, candidate = self.candidate()
        stale = deepcopy(candidate)
        stale["buildProvenance"]["sha256"] = "0" * 64
        self.assertTrue(evidence.inspect_candidate(path.parent, stale))
        for name in (candidate["package"], candidate["verification"], candidate["buildProvenance"]["path"]):
            unbound = deepcopy(candidate)
            unbound["files"] = [item for item in unbound["files"] if item["path"] != name]
            self.assertTrue(evidence.inspect_candidate(path.parent, unbound))

    def test_dirty_checkout_before_build_is_rejected_before_execution(self):
        (self.source / evidence.LOCKS[0]).write_text("changed before build", encoding="utf-8")
        with mock.patch.object(evidence, "execute_record") as execute:
            with self.assertRaises(ValueError):
                self.build()
            execute.assert_not_called()

    def test_source_change_during_build_cannot_produce_success_receipt(self):
        self.build_command = [sys.executable, "-c",
            "import pathlib,sys\npathlib.Path(sys.argv[1]).write_text('changed during fixture command')",
            str(self.source / evidence.LOCKS[0])]
        path, receipt = self.build()
        self.assertFalse(receipt["passed"])
        self.assertTrue(evidence.verified_build(path.parent, receipt))

    def test_local_fixture_execution_has_bound_build_and_report(self):
        build_path, build = self.build()
        path, receipt = self.run_local(build_path)
        self.assertTrue(receipt["passed"])
        self.assertEqual(evidence.inspect_local_run(path.parent, receipt, "physical", build["identity"]), [])

    def test_changed_producer_before_local_run_is_rejected(self):
        build_path, _ = self.build()
        self.programs["qcopilots-search-cli"].write_bytes(b"changed producer")
        with mock.patch.object(evidence, "execute_record") as execute:
            with self.assertRaises(ValueError):
                self.run_local(build_path)
            execute.assert_not_called()

    def test_changed_source_before_local_run_is_rejected(self):
        build_path, _ = self.build()
        (self.source / evidence.LOCKS[0]).write_text("changed after build", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.run_local(build_path)

    def test_source_or_producer_change_during_local_run_cannot_pass(self):
        for target in (self.source / evidence.LOCKS[0], self.programs["qcopilots-search-cli"]):
            with self.subTest(target=target.name):
                original = target.read_bytes() if target.exists() else None
                build_path, _ = self.build()
                command = self.local_command("pathlib.Path(sys.argv[3]).write_bytes(b'changed during run')\n", str(target))
                _, receipt = self.run_local(build_path, command)
                self.assertFalse(receipt["passed"])
                if original is not None:
                    target.write_bytes(original)

    def test_missing_native_artifacts_exit_nonzero_with_json_failure(self):
        output = self.root / "cli-failure.json"
        result = subprocess.run([sys.executable, str(Path(evidence.__file__)), "aggregate-native",
            "--candidate-commit", "1" * 40, "--native-artifacts-dir", str(self.root / "absent artifacts"),
            "--output", str(output)], cwd=self.root, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 1, result.stderr)
        report = json.loads(output.read_text(encoding="utf-8"))
        self.assertFalse(report["passed"])
        self.assertTrue(report["errors"])
        self.assertEqual(json.loads(result.stdout), report)


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    unittest.main()
