#!/usr/bin/env python3
"""Protocol, leakage, ranking and exact-search checks for grouped CV v2."""
import copy
import hashlib
import importlib.util
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
from unittest.mock import patch

sys.dont_write_bytecode = True
SCRIPT = Path(__file__).with_name("calibrate_relevance_v2.py")
spec = importlib.util.spec_from_file_location("qcs_calibrate_relevance_v2", SCRIPT)
v2 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(v2)


def candidate(file, modality="text", score=0.8, segment=1):
    return {"file": file, "policyModality": modality, "cosine": score, "segmentId": segment}


def query(partition, modality, positive, fold=None, suffix=""):
    identity = f"{partition}-{fold}-{modality}-{suffix}"
    row = {"query": f"{identity}-{'positive' if positive else 'negative'}", "topic": identity,
           "family": identity, "partition": partition, "language": "en", "modality": modality,
           "code": modality == "code", "keywordCandidates": [],
           "rawCandidates": [candidate(f"{identity}-file", modality, 0.8 if positive else 0.2)]}
    if fold is not None:
        row["fold"] = fold
    if positive:
        row["expectedFile"] = f"{identity}-file"
        row["policyModality"] = modality
    row["rawUnfilteredTop10"] = [f"{identity}-file"]
    return row


def fixture():
    report = {"calibrationProtocol": v2.PROTOCOL, "modelFingerprint": "model-v2",
              "corpusIdentity": {"groupPlanSha256": "a" * 64, "fixturesSha256": "b" * 64},
              "queries": [], "noAnswerQueries": []}
    for partition, fold in [("development", 0), ("development", 1), ("development", 2), ("holdout", None)]:
        for modality in v2.MODALITIES:
            report["queries"].append(query(partition, modality, True, fold))
            report["noAnswerQueries"].append(query(partition, modality, False, fold))
    return report


def replay_update(row):
    row["rawUnfilteredTop10"] = v2.v1.rank_query(row, {m: -1.000001 for m in v2.MODALITIES})


class V2CalibrationTests(unittest.TestCase):
    def test_three_folds_refit_and_independent_holdout_pass(self):
        result = v2.calibrate_report(fixture(), max_states=1000)
        self.assertTrue(result["accepted"])
        self.assertTrue(result["crossValidation"]["passed"])
        self.assertTrue(result["crossValidation"]["complete"])
        self.assertEqual(result["crossValidation"]["outOfFold"]["positive"]["queries"], 12)
        self.assertEqual(result["crossValidation"]["outOfFold"]["noAnswer"]["queries"], 12)
        self.assertEqual(result["thresholds"], {m: 0.5 for m in v2.MODALITIES})
        self.assertEqual(result["rawReplay"]["holdout"]["validatedQueries"], 8)
        self.assertEqual(result["development"]["search"]["status"], "optimal")

    def test_family_topic_and_same_file_cannot_cross_folds(self):
        for field in ("family", "topic", "expectedFile"):
            report = fixture()
            report["queries"][4][field] = report["queries"][0][field]
            with self.assertRaisesRegex(ValueError, "leakage"):
                v2.calibrate_report(report)

    def test_family_cannot_cross_final_holdout_boundary(self):
        report = fixture()
        report["queries"][-4]["family"] = report["queries"][0]["family"]
        with self.assertRaisesRegex(ValueError, "family leakage"):
            v2.calibrate_report(report)

    def test_fold_must_be_preassigned_and_holdout_has_none(self):
        for change in ("missing", True, 3):
            report = fixture()
            if change == "missing":
                del report["queries"][0]["fold"]
            else:
                report["queries"][0]["fold"] = change
            with self.assertRaisesRegex(ValueError, "preassigned"):
                v2.calibrate_report(report)
        report = fixture()
        report["queries"][-1]["fold"] = 0
        with self.assertRaisesRegex(ValueError, "must not have"):
            v2.calibrate_report(report)

    def test_production_raw_replay_checks_order_score_and_source(self):
        report = fixture()
        row = report["queries"][0]
        file = row["expectedFile"]
        row["rawUnfilteredTop10"] = [{"file": file, "score": 1.0 / 61.0, "source": "semantic"}]
        self.assertEqual(v2.validate_raw_replay([(row, True)])["status"], "passed")
        for field, bad in (("file", "wrong"), ("score", 0.123), ("source", "keyword")):
            invalid = copy.deepcopy(row)
            invalid["rawUnfilteredTop10"][0][field] = bad
            with self.assertRaisesRegex(ValueError, "raw RRF"):
                v2.validate_raw_replay([(invalid, True)])

    def test_raw_replay_failure_prevents_any_fitting(self):
        report = fixture()
        report["queries"][0]["rawUnfilteredTop10"] = []
        with patch.object(v2, "optimize") as optimizer:
            with self.assertRaisesRegex(ValueError, "raw RRF replay mismatch"):
                v2.calibrate_report(report)
            optimizer.assert_not_called()

    def test_training_five_percent_is_stricter_than_validation_ten_percent(self):
        positives = [(query("development", m, True, 0), True) for m in v2.MODALITIES]
        negatives = []
        for index in range(10):
            row = query("development", "text", False, 0, str(index))
            row["rawCandidates"] = []
            if index == 0:
                row["keywordCandidates"] = [{"file": "keyword-negative", "rank": 1}]
            replay_update(row)
            negatives.append((row, False))
        thresholds = {m: 0.5 for m in v2.MODALITIES}
        self.assertTrue(v2.evaluate(positives + negatives, thresholds)["passed"])
        self.assertFalse(v2.evaluate(positives + negatives, thresholds, 20)["passed"])
        policy, search = v2.optimize(positives + negatives)
        self.assertIsNone(policy)
        self.assertEqual(search["status"], "infeasible")

    def test_all_content_modality_groups_remain_hard_gates(self):
        rows = [(query("development", "text", True, 0, str(i)), True) for i in range(10)]
        rows[0][0]["modality"] = "pdf"
        rows[0][0]["rawCandidates"] = []
        rows.append((query("development", "text", False, 0), False))
        result = v2.evaluate(rows, {m: 0.5 for m in v2.MODALITIES})
        self.assertEqual(result["positive"]["recallAt10"], 0.9)
        self.assertEqual(result["positive"]["policyGroups"]["policyModality:text"]["recallAt10"], 0.9)
        self.assertFalse(result["passed"])
        self.assertFalse(result["positive"]["groups"]["modality:pdf"]["passed"])

    def test_optimizer_improves_on_first_feasible_policy(self):
        rows = [(query("development", "text", True, 0, str(i)), True) for i in range(10)]
        rows += [(query("development", m, True, 0), True) for m in ("image", "audio", "code")]
        rows += [(query("development", m, False, 0), False) for m in v2.MODALITIES]
        rows[0][0]["rawCandidates"][0]["cosine"] = 0.6
        rows[0][0]["rawCandidates"].extend(candidate(f"distractor-{i}", "image", 0.8, i + 2)
                                           for i in range(11))
        rows[10][0]["rawCandidates"][0]["cosine"] = 0.95
        initial, _ = v2.v1._threshold_search(rows, 1000)
        self.assertIsNotNone(initial)
        self.assertLess(v2.v1.evaluate(rows, initial)["positive"]["recallAt10"], 1.0)
        improved, search = v2.optimize(rows, 1000)
        self.assertEqual(search["status"], "optimal")
        self.assertEqual(v2.evaluate(rows, improved["thresholds"])["positive"]["recallAt10"], 1.0)
        self.assertGreater(improved["thresholds"]["image"], 0.8)

    def test_optimizer_exact_objective_matches_small_exhaustive_search(self):
        randomizer = random.Random(701)
        for _ in range(8):
            rows = [(query("development", m, True, 0), True) for m in v2.MODALITIES]
            rows += [(query("development", m, False, 0), False) for m in v2.MODALITIES]
            for row, positive in rows:
                row["rawCandidates"][0]["cosine"] = randomizer.choice((0.2, 0.5, 0.8))
            axes = [v2._Axis(sorted({c["cosine"] for row, _ in rows for c in row["rawCandidates"]
                                    if c["policyModality"] == modality})) for modality in v2.MODALITIES]
            evidence = v2._Evidence(rows)
            best = None
            for indices in itertools.product(*(range(len(axis.thresholds)) for axis in axes)):
                thresholds = tuple(axes[i].thresholds[indices[i]] for i in range(4))
                passed, worst, overall = evidence.quality(evidence.flags(thresholds))
                returned = evidence.negative_returns(thresholds)
                if not passed or returned:
                    continue
                margins = [axes[i].margins[indices[i]] for i in range(4)]
                objective = (worst, overall, -returned, min(margins), sum(margins), tuple(-x for x in thresholds))
                best = max(best, objective) if best is not None else objective
            policy, search = v2.optimize(rows, 10000)
            self.assertTrue(search["optimalityProven"])
            self.assertEqual(policy is not None, best is not None)
            if best is not None:
                self.assertEqual(tuple(policy["thresholds"][m] for m in v2.MODALITIES), tuple(-x for x in best[-1]))

    def test_budget_exhaustion_does_not_claim_optimality(self):
        rows = [(query("development", "text", True, 0, str(i)), True) for i in range(10)]
        rows += [(query("development", m, True, 0), True) for m in ("image", "audio", "code")]
        rows += [(query("development", m, False, 0), False) for m in v2.MODALITIES]
        rows[0][0]["rawCandidates"][0]["cosine"] = 0.6
        rows[0][0]["rawCandidates"].extend(candidate(f"distractor-{i}", "image", 0.8, i + 2)
                                           for i in range(11))
        rows[10][0]["rawCandidates"][0]["cosine"] = 0.95
        policy, search = v2.optimize(rows, 1)
        self.assertIsNotNone(policy)
        self.assertEqual(search["status"], "feasible-search-budget-exhausted")
        self.assertFalse(search["optimalityProven"])

    def test_fitting_never_receives_holdout_and_evaluates_it_once_after_freeze(self):
        fits, heldout_evaluations = [], []
        original_optimize, original_evaluate = v2.optimize, v2.evaluate

        def optimizing(rows, *args, **kwargs):
            self.assertTrue(all(row["partition"] == "development" for row, _ in rows))
            fits.append({row["fold"] for row, _ in rows})
            return original_optimize(rows, *args, **kwargs)

        def evaluating(rows, *args, **kwargs):
            if any(row["partition"] == "holdout" for row, _ in rows):
                self.assertEqual(len(fits), 4)
                heldout_evaluations.append(args[0])
            return original_evaluate(rows, *args, **kwargs)

        with patch.object(v2, "optimize", side_effect=optimizing), patch.object(v2, "evaluate", side_effect=evaluating):
            policy = v2.calibrate_report(fixture(), max_states=1000)
        self.assertTrue(policy["accepted"])
        self.assertEqual(fits, [{1, 2}, {0, 2}, {0, 1}, {0, 1, 2}])
        self.assertEqual(len(heldout_evaluations), 1)
        self.assertEqual(heldout_evaluations[0], policy["thresholds"])

    def test_holdout_scores_cannot_change_fitted_policy_or_cv(self):
        report = fixture()
        original = v2.calibrate_report(report, max_states=1000)
        for row in report["queries"]:
            if row["partition"] == "holdout":
                row["rawCandidates"][0]["cosine"] = 0.4
        changed = v2.calibrate_report(report, max_states=1000)
        self.assertFalse(changed["accepted"])
        self.assertEqual(changed["status"], "holdout-failed")
        for field in ("thresholds", "developmentSha256", "selectionSha256", "crossValidation", "development"):
            self.assertEqual(changed[field], original[field])

    def test_failed_cv_does_not_validate_replay_or_evaluate_holdout_scores(self):
        report = fixture()
        for row in report["noAnswerQueries"]:
            if row["partition"] == "development":
                row["rawCandidates"][0]["cosine"] = 0.9
        for row in report["queries"]:
            if row["partition"] == "holdout":
                row["rawCandidates"] = "uninspected final scores"
                del row["rawUnfilteredTop10"]
        policy = v2.calibrate_report(report, report_sha256="c" * 64, max_states=1000)
        self.assertFalse(policy["accepted"])
        self.assertEqual(policy["status"], "cross-validation-failed")
        self.assertIsNone(policy["development"])
        self.assertEqual(policy["holdout"]["status"], "not-evaluated")
        self.assertEqual(policy["rawReplay"]["holdout"]["validatedQueries"], 0)

    def test_cli_binds_bytes_and_preserves_header_on_failure(self):
        configured = os.environ.get("QCOPILOTS_SEARCH_TEST_ROOT") or None
        if configured:
            Path(configured).mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="qcs-calibration-v2-", dir=configured) as temporary:
            root = Path(temporary)
            raw_path, output, header = root / "raw.json", root / "policy.json", root / "policy.h"
            report = fixture()
            raw = b"\xef\xbb\xbf" + json.dumps(report, indent=3).encode("utf-8")
            raw_path.write_bytes(raw)
            command = [sys.executable, str(SCRIPT), str(raw_path), "--output", str(output), "--header", str(header),
                       "--max-states", "1000"]
            result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            header_bytes = header.read_bytes()
            self.assertIn(hashlib.sha256(raw).hexdigest().encode(), header_bytes)
            self.assertIn(hashlib.sha256(output.read_bytes()).hexdigest().encode(), header_bytes)
            for row in report["queries"]:
                if row["partition"] == "holdout":
                    row["rawCandidates"][0]["cosine"] = 0.4
            raw_path.write_text(json.dumps(report), encoding="utf-8")
            result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", timeout=20)
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertEqual(header.read_bytes(), header_bytes)

    def test_cli_rejects_output_inside_source_tree(self):
        output = SCRIPT.with_name("qcs-v2-must-not-create.json")
        self.assertFalse(output.exists())
        result = subprocess.run([sys.executable, str(SCRIPT), "unused.json", "--output", str(output)],
                                capture_output=True, text=True, encoding="utf-8", timeout=10)
        self.assertEqual(result.returncode, 2)
        self.assertIn("outside the source repository", result.stderr)
        self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
