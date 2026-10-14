#!/usr/bin/env python3
"""Small, independent regression fixtures for relevance policy calibration."""
import importlib.util
import hashlib
import itertools
import json
import math
import os
from pathlib import Path
import random
import subprocess
import sys
import tempfile
import unittest

sys.dont_write_bytecode = True
SCRIPT = Path(__file__).with_name("calibrate_relevance.py")
spec = importlib.util.spec_from_file_location("qcs_calibrate_relevance", SCRIPT)
calibrator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(calibrator)


def candidate(file, modality="text", cosine=0.8, segment_id=1):
    return {"file": file, "policyModality": modality, "cosine": cosine, "segmentId": segment_id}


def query(partition, modality, positive=True, suffix=""):
    row = {"query": f"{partition} {modality} {'positive' if positive else 'negative'} {suffix}",
           "partition": partition, "topic": f"{partition}-{modality}-{suffix}",
           "language": "en", "modality": modality, "code": modality == "code",
           "rawCandidates": [candidate("expected" if positive else "unrelated", modality,
                                        0.8 if positive else 0.2)], "keywordCandidates": []}
    if positive:
        row["expectedFile"] = "expected"
    return row


def fixture():
    return {"modelFingerprint": "frozen-model-sha256", "corpusIdentity": {"fixturesSha256": "a" * 64},
            "queries": [query(partition, modality) for partition in calibrator.PARTITIONS
                        for modality in calibrator.MODALITIES],
            "noAnswerQueries": [query(partition, modality, False) for partition in calibrator.PARTITIONS
                                for modality in calibrator.MODALITIES]}


class CalibrationTests(unittest.TestCase):
    def test_boundary_is_inclusive_and_nextafter_rejects(self):
        row = query("calibration", "text")
        thresholds = {m: 1.0 for m in calibrator.MODALITIES}
        thresholds["text"] = 0.8
        self.assertEqual(calibrator.rank_query(row, thresholds), ["expected"])
        thresholds["text"] = math.nextafter(0.8, math.inf)
        self.assertEqual(calibrator.rank_query(row, thresholds), [])

    def test_accepts_independent_holdout_and_binds_evidence(self):
        report = fixture()
        policy = calibrator.calibrate_report(report)
        self.assertTrue(policy["accepted"])
        self.assertTrue(policy["calibrated"])
        self.assertEqual(policy["search"]["status"], "feasible")
        self.assertEqual(policy["thresholds"], {m: 0.5 for m in calibrator.MODALITIES})
        self.assertEqual(policy["reportSha256"], calibrator.canonical_hash(report))
        self.assertEqual(policy["corpusSha256"], calibrator.canonical_hash(report["corpusIdentity"]))
        self.assertIn("inline constexpr bool calibrated = true", calibrator.cpp_header(policy))

    def test_published_midpoints_preserve_calibration_acceptance_sets(self):
        report = fixture()
        policy = calibrator.calibrate_report(report)
        selection = policy["thresholdSelection"]
        self.assertTrue(selection["calibrationAcceptanceEquivalent"])
        self.assertEqual(selection["method"], "calibration-interval-midpoint")
        for modality in calibrator.MODALITIES:
            self.assertEqual(selection["searchThresholds"][modality], math.nextafter(0.2, math.inf))
            interval = selection["intervals"][modality]
            self.assertEqual(interval["rejectedMaximum"], 0.2)
            self.assertEqual(interval["acceptedMinimum"], 0.8)
            self.assertAlmostEqual(interval["rejectedMargin"], 0.3)
            self.assertAlmostEqual(interval["acceptedMargin"], 0.3)
        for row, _ in calibrator.validate_report(report):
            if row["partition"] == "calibration":
                for raw in row["rawCandidates"]:
                    modality = raw["policyModality"]
                    self.assertEqual(raw["cosine"] >= selection["searchThresholds"][modality],
                                     raw["cosine"] >= policy["thresholds"][modality])
                self.assertEqual(calibrator.rank_query(row, selection["searchThresholds"]),
                                 calibrator.rank_query(row, policy["thresholds"]))

    def test_midpoint_uses_no_holdout_scores_inside_calibration_interval(self):
        report = fixture()
        original = calibrator.calibrate_report(report)
        for row in report["queries"]:
            if row["partition"] == "holdout":
                row["rawCandidates"][0]["cosine"] = 0.4
        changed = calibrator.calibrate_report(report)
        self.assertFalse(changed["accepted"])
        self.assertEqual(changed["thresholds"], original["thresholds"])
        self.assertEqual(changed["thresholdSelection"], original["thresholdSelection"])
        self.assertEqual(changed["calibrationSha256"], original["calibrationSha256"])

    def test_adjacent_doubles_have_zero_margin_without_changing_acceptance(self):
        report = fixture()
        retained = math.nextafter(0.2, math.inf)
        for row in report["queries"]:
            row["rawCandidates"][0]["cosine"] = retained
        policy = calibrator.calibrate_report(report)
        self.assertTrue(policy["accepted"])
        self.assertEqual(policy["thresholds"], {m: retained for m in calibrator.MODALITIES})
        for interval in policy["thresholdSelection"]["intervals"].values():
            self.assertEqual(interval["acceptedMargin"], 0.0)
            self.assertGreater(interval["rejectedMargin"], 0.0)

    def test_interval_endpoints_handle_all_accepted_and_all_rejected(self):
        rows = [(query("calibration", modality), True) for modality in calibrator.MODALITIES]
        search = {m: (0.8 if m in ("text", "image") else math.nextafter(0.8, math.inf))
                  for m in calibrator.MODALITIES}
        thresholds, placement = calibrator._representative_thresholds(rows, search)
        self.assertEqual(thresholds["text"], 0.8)
        self.assertEqual(thresholds["image"], 0.8)
        self.assertEqual(thresholds["audio"], calibrator.REJECT_ALL)
        self.assertEqual(thresholds["code"], calibrator.REJECT_ALL)
        self.assertIsNone(placement["intervals"]["text"]["rejectedMaximum"])
        self.assertIsNone(placement["intervals"]["audio"]["acceptedMinimum"])

    def test_topic_cannot_leak_between_partitions(self):
        report = fixture()
        report["queries"][4]["topic"] = report["queries"][0]["topic"]
        with self.assertRaisesRegex(ValueError, "topic leakage"):
            calibrator.calibrate_report(report)

    def test_missing_negatives_does_not_certify_zero_false_returns(self):
        for partition in calibrator.PARTITIONS:
            report = fixture()
            report["noAnswerQueries"] = [row for row in report["noAnswerQueries"]
                                        if row["partition"] != partition]
            policy = calibrator.calibrate_report(report)
            self.assertFalse(policy["accepted"])
            self.assertFalse(policy[partition]["noAnswer"]["passed"])
            self.assertIsNone(policy[partition]["noAnswer"]["falseReturnRate"])
            with self.assertRaisesRegex(ValueError, "independent holdout"):
                calibrator.cpp_header(policy)

    def test_missing_policy_modality_remains_uncalibrated(self):
        report = fixture()
        for field in ("queries", "noAnswerQueries"):
            report[field] = [row for row in report[field] if row["modality"] != "audio"]
        policy = calibrator.calibrate_report(report)
        self.assertFalse(policy["calibrated"])
        self.assertEqual(policy["search"]["unobservedModalities"], ["audio"])

    def test_holdout_cannot_silently_omit_a_language_or_modality(self):
        report = fixture()
        report["queries"] = [row for row in report["queries"]
                             if not (row["partition"] == "holdout" and row["modality"] == "audio")]
        policy = calibrator.calibrate_report(report)
        self.assertTrue(policy["calibrated"])
        self.assertTrue(policy["holdout"]["passed"])
        self.assertFalse(policy["accepted"])
        self.assertEqual(policy["coverage"]["missingHoldoutGroups"], ["modality:audio"])

    def test_new_holdout_only_category_is_evaluated_without_tuning(self):
        report = fixture()
        extra = query("holdout", "image", suffix="unseen-category")
        extra["modality"] = "video-natural"
        report["queries"].append(extra)
        policy = calibrator.calibrate_report(report)
        self.assertTrue(policy["accepted"])
        self.assertEqual(policy["coverage"]["additionalHoldoutGroups"], ["modality:video-natural"])
        self.assertEqual(policy["thresholds"], calibrator.calibrate_report(fixture())["thresholds"])

    def test_inseparable_calibration_is_proved_infeasible(self):
        report = fixture()
        report["noAnswerQueries"][0]["rawCandidates"][0]["cosine"] = 0.9
        policy = calibrator.calibrate_report(report)
        self.assertFalse(policy["accepted"])
        self.assertFalse(policy["calibrated"])
        self.assertEqual(policy["status"], "infeasible")

    def test_holdout_scores_never_change_threshold_selection(self):
        report = fixture()
        original = calibrator.calibrate_report(report)
        for row in report["noAnswerQueries"]:
            if row["partition"] == "holdout":
                row["rawCandidates"][0]["cosine"] = 0.99
        changed = calibrator.calibrate_report(report)
        self.assertTrue(changed["calibrated"])
        self.assertFalse(changed["accepted"])
        self.assertEqual(changed["status"], "holdout-failed")
        self.assertEqual(changed["thresholds"], original["thresholds"])
        self.assertEqual(changed["calibrationSha256"], original["calibrationSha256"])
        self.assertEqual(changed["version"], original["version"])
        self.assertNotEqual(changed["reportSha256"], original["reportSha256"])

    def test_keyword_is_preserved_and_fused_not_thresholded(self):
        row = query("calibration", "text")
        row["rawCandidates"] = [candidate("semantic", cosine=0.9),
                                candidate("hybrid", cosine=0.8, segment_id=2)]
        row["keywordCandidates"] = [{"file": "keyword", "rank": 1}, {"file": "hybrid", "rank": 2}]
        thresholds = {m: 0.5 for m in calibrator.MODALITIES}
        self.assertEqual(calibrator.rank_query(row, thresholds), ["hybrid", "keyword", "semantic"])
        thresholds["text"] = 1.0
        self.assertEqual(calibrator.rank_query(row, thresholds), ["keyword", "hybrid"])

    def test_keyword_negative_cannot_be_fixed_by_semantic_threshold(self):
        report = fixture()
        report["noAnswerQueries"][0]["keywordCandidates"] = [{"file": "keyword", "rank": 1}]
        policy = calibrator.calibrate_report(report)
        self.assertFalse(policy["calibrated"])
        self.assertEqual(policy["search"]["status"], "infeasible")
        self.assertIn("keyword-only", policy["search"]["reason"])

    def test_segment_ties_and_multiple_modalities_match_production_order(self):
        row = query("calibration", "text")
        row["rawCandidates"] = [candidate("z-first", cosine=0.8, segment_id=1),
                                candidate("a-second", cosine=0.8, segment_id=2),
                                candidate("z-first", "image", 0.9, 3)]
        thresholds = {m: 0.0 for m in calibrator.MODALITIES}
        self.assertEqual(calibrator.rank_query(row, thresholds), ["z-first", "a-second"])
        thresholds["image"] = 1.0
        self.assertEqual(calibrator.rank_query(row, thresholds), ["z-first", "a-second"])

    def test_ambiguous_exact_score_ties_require_segment_ids(self):
        report = fixture()
        row = report["queries"][0]
        row["rawCandidates"].append({"file": "other", "policyModality": "text", "cosine": 0.8})
        with self.assertRaisesRegex(ValueError, "production tie order"):
            calibrator.calibrate_report(report)

    def test_semantic_rrf_budget_is_applied_after_filtering(self):
        row = query("calibration", "text")
        row["rawCandidates"] = [candidate(f"file-{i:03d}", cosine=0.9 - i / 1000, segment_id=i)
                                for i in range(257)]
        row["keywordCandidates"] = [{"file": "file-256", "rank": 1}]
        thresholds = {m: 0.0 for m in calibrator.MODALITIES}
        ranking = calibrator.rank_query(row, thresholds)
        # Rank 257 gets only its keyword vote, and ties the first semantic hit.
        self.assertEqual(ranking[:2], ["file-000", "file-256"])

    def test_search_can_raise_distractor_gate_to_recover_recall(self):
        report = fixture()
        for row in report["queries"]:
            if row["modality"] == "image":
                row["rawCandidates"][0]["cosine"] = 0.9
            elif row["modality"] == "text":
                row["rawCandidates"][0]["cosine"] = 0.6
                row["rawCandidates"].extend(candidate(f"distractor-{i}", "image", 0.8, i + 2)
                                             for i in range(11))
        policy = calibrator.calibrate_report(report)
        self.assertTrue(policy["accepted"])
        self.assertGreater(policy["thresholds"]["image"], 0.8)
        self.assertLessEqual(policy["thresholds"]["text"], 0.6)

    def test_invalid_scores_and_keyword_ranks_are_rejected(self):
        for score in (float("nan"), float("inf"), True, "0.8", 1.2):
            report = fixture()
            report["queries"][0]["rawCandidates"][0]["cosine"] = score
            with self.assertRaisesRegex(ValueError, "finite cosine"):
                calibrator.calibrate_report(report)
        report = fixture()
        report["queries"][0]["keywordCandidates"] = [{"file": "a", "rank": 2}]
        with self.assertRaisesRegex(ValueError, "contiguous"):
            calibrator.calibrate_report(report)

    def test_incomplete_inference_report_is_rejected(self):
        report = fixture()
        report["status"] = "failed"
        with self.assertRaisesRegex(ValueError, "incomplete inference"):
            calibrator.calibrate_report(report)

    def test_query_level_negative_rate_includes_ten_percent_boundary(self):
        positives = [(query("calibration", "text"), True)]
        negatives = []
        for i in range(10):
            row = query("calibration", "text", False, str(i))
            row["rawCandidates"] = []
            if i == 0:
                row["keywordCandidates"] = [{"file": "a", "rank": 1}, {"file": "b", "rank": 2}]
            negatives.append((row, False))
        result = calibrator.evaluate(positives + negatives, {m: 0.5 for m in calibrator.MODALITIES})
        self.assertTrue(result["passed"])
        self.assertEqual(result["noAnswer"]["returned"], 1)
        self.assertEqual(result["noAnswer"]["falseReturnRate"], 0.1)

    def test_search_budget_is_inconclusive_not_infeasible(self):
        report = fixture()
        report["noAnswerQueries"] = []
        for i in range(10):
            row = query("calibration", "text", False, str(i))
            row["rawCandidates"] = []
            if i < 2:
                row["rawCandidates"] = [candidate("distractor", ("text", "image")[i], 0.4)]
            report["noAnswerQueries"].append(row)
        rows = [(row, positive) for row, positive in calibrator.validate_report(report)
                if row["partition"] == "calibration"]
        thresholds, stats = calibrator._threshold_search(rows, 1)
        self.assertIsNone(thresholds)
        self.assertEqual(stats["status"], "search-budget-exhausted")
        thresholds, stats = calibrator._threshold_search(rows, 100)
        self.assertIsNotNone(thresholds)
        self.assertEqual(stats["status"], "feasible")

    def test_search_is_exact_against_small_exhaustive_grid(self):
        randomizer = random.Random(414)
        for case in range(16):
            report = fixture()
            rows = [(row, positive) for row, positive in calibrator.validate_report(report)
                    if row["partition"] == "calibration"]
            for row, positive in rows:
                row["rawCandidates"][0]["cosine"] = randomizer.choice((0.2, 0.5, 0.8))
                if positive and row["modality"] != "code":
                    # Cross-modality distractors exercise coupled thresholds.
                    other = randomizer.choice(("text", "image", "audio"))
                    row["rawCandidates"].append(candidate("distractor", other,
                                                          randomizer.choice((0.2, 0.5, 0.8)), 2))
            grid = []
            for modality in calibrator.MODALITIES:
                values = sorted({c["cosine"] for row, _ in rows for c in row["rawCandidates"]
                                 if c["policyModality"] == modality})
                grid.append([values[0]] + [math.nextafter(value, math.inf) for value in values])
            brute = any(calibrator.evaluate(rows, dict(zip(calibrator.MODALITIES, point)))["passed"]
                        for point in itertools.product(*grid))
            fitted, stats = calibrator._threshold_search(rows, 10000)
            self.assertEqual(fitted is not None, brute, (case, stats))
            self.assertNotEqual(stats["status"], "search-budget-exhausted")

    def test_cli_failed_holdout_preserves_existing_header(self):
        configured = os.environ.get("QCOPILOTS_SEARCH_TEST_ROOT") or None
        if configured:
            Path(configured).mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="qcs-calibration-", dir=configured) as temporary:
            root = Path(temporary)
            report_path, output, header = root / "report.json", root / "policy.json", root / "policy.h"
            report = fixture()
            report["queries"][4]["rawCandidates"] = []
            report_path.write_text(json.dumps(report), encoding="utf-8")
            header.write_text("old accepted header\n", encoding="utf-8")
            result = subprocess.run([sys.executable, str(SCRIPT), str(report_path), "--output", str(output),
                                     "--header", str(header)], capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertEqual(header.read_text(encoding="utf-8"), "old accepted header\n")
            self.assertFalse(json.loads(output.read_text(encoding="utf-8"))["accepted"])

    def test_generated_header_binds_actual_raw_and_policy_file_bytes(self):
        configured = os.environ.get("QCOPILOTS_SEARCH_TEST_ROOT") or None
        if configured:
            Path(configured).mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="qcs-calibration-hashes-", dir=configured) as temporary:
            root = Path(temporary)
            report_path, output, header = root / "raw.json", root / "policy.json", root / "policy.h"
            report = fixture()
            report["modelFingerprint"] = "模型-fingerprint"
            # A BOM and intentional whitespace prove that the binding is to
            # the immutable input bytes, not a parsed/canonicalized object.
            raw = b"\xef\xbb\xbf" + (json.dumps(report, ensure_ascii=False, indent=3) + "\r\n").encode("utf-8")
            report_path.write_bytes(raw)
            result = subprocess.run([sys.executable, str(SCRIPT), str(report_path), "--output", str(output),
                                     "--header", str(header)], capture_output=True, text=True,
                                    encoding="utf-8", timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            policy_bytes = output.read_bytes()
            policy = json.loads(policy_bytes)
            raw_hash = hashlib.sha256(raw).hexdigest()
            policy_hash = hashlib.sha256(policy_bytes).hexdigest()
            self.assertEqual(policy["reportSha256"], raw_hash)
            self.assertNotEqual(raw_hash, calibrator.canonical_hash(report))
            self.assertEqual(policy_bytes, calibrator.serialize_policy(policy).encode("utf-8"))
            generated = header.read_text(encoding="utf-8")
            self.assertIn(f'inline constexpr const char *rawExportSha256 = "{raw_hash}";', generated)
            self.assertIn(f'inline constexpr const char *calibrationReportSha256 = "{policy_hash}";', generated)
            self.assertNotIn("calibrationReportSha256", policy)

    def test_cli_rejects_generated_report_inside_source_repository(self):
        output = SCRIPT.parent / "qcs-calibration-report-must-not-be-written.json"
        self.assertFalse(output.exists())
        result = subprocess.run([sys.executable, str(SCRIPT), "unused-report.json", "--output", str(output)],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("outside the source repository", result.stderr)
        self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
