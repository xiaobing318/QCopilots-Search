#!/usr/bin/env python3
"""Fast contract tests for frozen semantic calibration evidence validation."""
from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
import sys
import unittest

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


evidence = load("qcs_semantic_policy_evidence_tested", ROOT / "tools/qcs-semantic-policy-evidence.py")
calibration = load("qcs_semantic_policy_calibration_fixture", ROOT / "tests/semantic/calibrate_relevance_v2.py")


def fixture():
    """Use the real pure-Python formatter on tiny synthetic score grids.

    No embeddings, assets, processes or files are created. Counts match the
    frozen protocol so these tests exercise the exact production schema.
    """
    report = {
        "calibrationProtocol": evidence.PROTOCOL,
        "modelFingerprint": "fixture-model",
        "corpusIdentity": {name: str(index) * 64 for index, name in enumerate(
            ("fixtureManifestSha256", "topicManifestSha256", "generatorSha256", "relevanceManifestSha256",
             "groupPlanSha256", "v2TextCodeManifestSha256", "v2MediaManifestSha256"), 1)},
        "queries": [],
        "noAnswerQueries": [],
    }
    for fold, positives, negatives in ((0, 74, 30), (1, 82, 26), (2, 82, 40), (None, 80, 80)):
        partition = "holdout" if fold is None else "development"
        group = "holdout" if fold is None else str(fold)
        for positive, count, field in ((True, positives, "queries"), (False, negatives, "noAnswerQueries")):
            for index in range(count):
                topic = f"topic-{group}-{index // 2}"
                modality = ("text", "image", "audio", "code")[(index // 2) % 4]
                file = f"file-{group}-{index // 2}"
                row = {"query": f"{'positive' if positive else 'negative'}-{group}-{index}",
                       "topic": topic, "family": "family-" + topic, "partition": partition,
                       "language": "en" if index % 2 == 0 else "zh",
                       "modality": "speech" if modality == "audio" else modality, "code": modality == "code",
                       "keywordCandidates": [],
                       "rawCandidates": [{"file": file, "policyModality": modality,
                                          "cosine": .8 if positive else .2, "segmentId": index}],
                       "rawUnfilteredTop10": [file]}
                if fold is not None:
                    row["fold"] = fold
                if positive:
                    row["expectedFile"] = file
                    row["policyModality"] = modality
                report[field].append(row)
    snapshot = calibration.calibrate_report(report, report_sha256="a" * 64, max_states=25000)
    assert snapshot["accepted"], snapshot
    policy = {key: copy.deepcopy(snapshot[key]) for key in
              ("version", "modelFingerprint", "corpusSha256", "calibrated", "thresholds")}
    policy["rawExportSha256"] = snapshot["reportSha256"]
    policy["calibrationReportSha256"] = "b" * 64
    report.update(mode="release", passed=True, status="passed", semanticCandidateMode="matches-only",
                  metricsScope="all-production-queries", finalHoldoutEvaluated=True,
                  relevancePolicy=copy.deepcopy(policy))
    for row in report["queries"]:
        row["productionTop10"] = [{"file": row["expectedFile"], "score": 1 / 61, "source": "semantic"}]
        row["rank"] = 1
    for row in report["noAnswerQueries"]:
        row["productionTop10"] = []
        row["returnedCount"] = 0
    # Saved JSON has independent row objects even when the calibration helper
    # reused a dictionary while assembling its in-memory OOF output.
    return json.loads(json.dumps([snapshot, policy, report]))


def recompute_metric(metric, divisor):
    """Repair claimed summaries after deliberately changing record outcomes."""
    positives = metric["queries"]
    groups, policy_groups = {}, {}
    hits = 0
    for row in positives:
        hit = row["rank"] is not None
        hits += hit
        for target, axes in ((groups, ("language", "modality")), (policy_groups, ("language", "policyModality"))):
            for axis in axes:
                group = target.setdefault(axis + ":" + row[axis], {"queries": 0, "hits": 0})
                group["queries"] += 1
                group["hits"] += hit
    for target, with_passed in ((groups, True), (policy_groups, False)):
        for group in target.values():
            group["recallAt10"] = group["hits"] / group["queries"]
            if with_passed:
                group["passed"] = group["hits"] * 5 >= group["queries"] * 4
    positive_passed = hits * 10 >= len(positives) * 9 and all(group["passed"] for group in groups.values())
    metric["positive"] = {"queries": len(positives), "hits": hits, "recallAt10": hits / len(positives),
                          "groups": groups, "policyGroups": policy_groups, "passed": positive_passed}
    negatives = metric["noAnswerQueries"]
    returns = sum(row["returned"] for row in negatives)
    negative_passed = returns * divisor <= len(negatives)
    metric["noAnswer"] = {"queries": len(negatives), "returned": returns, "falseReturnRate": returns / len(negatives),
                          "maximumFalseReturnRate": 1 / divisor, "passed": negative_passed}
    metric["passed"] = positive_passed and negative_passed


class CalibrationSnapshotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.valid = fixture()

    def setUp(self):
        self.snapshot, self.policy, self.report = copy.deepcopy(self.valid)

    def errors(self):
        return evidence.validate_calibration_snapshot(self.snapshot, self.policy, self.report)

    def rejects(self, fragment):
        result = self.errors()
        self.assertTrue(result, "invalid evidence was accepted")
        self.assertIn(fragment, " ".join(result))

    def test_accepts_actual_calibrator_schema(self):
        self.assertEqual(self.errors(), [])

    def test_allows_platform_top10_order_and_rank_differences(self):
        row = next(row for row in self.report["queries"] if row["partition"] == "holdout")
        row["productionTop10"].insert(0, {"file": "platform-tied-distractor", "score": 1 / 61, "source": "semantic"})
        row["rank"] = 2
        self.assertEqual(self.errors(), [])

    def test_rejects_changed_protocol_or_threshold_config(self):
        for name, value in (("protocol", "another-protocol"), ("folds", True), ("thresholdInteriorFraction", .5),
                            ("trainingMaximumFalseReturnRate", .1), ("evaluationMaximumFalseReturnRate", .2)):
            with self.subTest(name=name):
                self.snapshot = copy.deepcopy(self.valid[0])
                self.snapshot["config"][name] = value
                self.rejects("configuration" if name in ("protocol", "folds") else "snapshot.config." + name)

    def test_rejects_uncalibrated_or_unaccepted_snapshot(self):
        for name in ("accepted", "calibrated"):
            with self.subTest(name=name):
                self.snapshot = copy.deepcopy(self.valid[0])
                self.snapshot[name] = False
                self.rejects("not an accepted")

    def test_rejects_identity_and_threshold_mismatch(self):
        for name, value in (("rawExportSha256", "c" * 64), ("modelFingerprint", "other-model"),
                            ("corpusSha256", "d" * 64), ("version", "other-version"),
                            ("thresholds", dict(text=.99, image=.99, audio=.99, code=.99))):
            with self.subTest(name=name):
                self.policy = copy.deepcopy(self.valid[1])
                self.policy[name] = value
                self.rejects("differ")

    def test_rejects_fabricated_selection_identity(self):
        self.snapshot["selectionSha256"] = "f" * 64
        self.rejects("selection identity")

    def test_rejects_incomplete_raw_replay(self):
        self.snapshot["rawReplay"]["holdout"]["validatedQueries"] = 159
        self.rejects("raw replay")

    def test_rejects_incomplete_cv_and_duplicate_fold(self):
        self.snapshot["crossValidation"]["complete"] = False
        self.rejects("incomplete")
        self.snapshot = copy.deepcopy(self.valid[0])
        self.snapshot["crossValidation"]["folds"][2]["fold"] = 1
        self.rejects("duplicate fold")

    def test_rejects_omitted_training_validation_and_oof_rows(self):
        for path in (("folds", 0, "training", "metrics"), ("folds", 0, "validation"), ("outOfFold",)):
            with self.subTest(path=path):
                self.snapshot = copy.deepcopy(self.valid[0])
                metric = self.snapshot["crossValidation"]
                for key in path:
                    metric = metric[key]
                metric["queries"].pop()
                self.rejects("complete" if path[0] == "folds" else "every development")

    def test_rejects_training_using_held_out_rows(self):
        fold = self.snapshot["crossValidation"]["folds"][0]
        fold["training"]["metrics"]["queries"][0] = copy.deepcopy(fold["validation"]["queries"][0])
        self.rejects("outside its expected query set")

    def test_rejects_changed_fold_policy_hash(self):
        self.snapshot["crossValidation"]["folds"][1]["validation"]["queries"][0]["foldPolicySha256"] = "e" * 64
        self.rejects("another fold policy")

    def test_rejects_fabricated_oof_record(self):
        row = self.snapshot["crossValidation"]["outOfFold"]["queries"][0]
        row["top10"] = ["other-file", row["expectedFile"]]
        row["rank"] = 2
        self.rejects("differs from its held-out fold")

    def test_rejects_oof_boolean_and_numeric_type_substitutions(self):
        for field, name, value in (("queries", "rank", True), ("queries", "rank", 1.0),
                                   ("noAnswerQueries", "returned", 0), ("queries", "fold", False)):
            with self.subTest(field=field, name=name, value=value):
                self.snapshot = copy.deepcopy(self.valid[0])
                self.snapshot["crossValidation"]["outOfFold"][field][0][name] = value
                self.rejects("differs from its held-out fold")

    def test_rejects_stale_rank_and_duplicate_results(self):
        self.snapshot["holdout"]["queries"][0]["rank"] = 2
        self.rejects("rank disagrees")
        self.snapshot = copy.deepcopy(self.valid[0])
        row = self.snapshot["holdout"]["queries"][0]
        row["top10"] *= 2
        self.rejects("duplicates")

    def test_rejects_invented_group_summary(self):
        self.snapshot["holdout"]["positive"]["groups"]["language:en"]["hits"] = 39
        self.rejects("counts disagree")

    def test_rejects_missing_group_evidence(self):
        del self.snapshot["holdout"]["positive"]["groups"]["modality:image"]
        self.rejects("observed groups")

    def test_rejects_final_holdout_recall_failure_even_with_consistent_false_status(self):
        metric = self.snapshot["holdout"]
        for row in metric["queries"][:9]:
            row["top10"], row["rank"] = [], None
        recompute_metric(metric, 10)
        self.rejects("did not satisfy")

    def test_rejects_false_return_failure_despite_consistent_summary(self):
        metric = self.snapshot["holdout"]
        for row in metric["noAnswerQueries"][:9]:
            row["top10"], row["returned"] = ["unexpected"], True
        recompute_metric(metric, 10)
        self.rejects("did not satisfy")

    def test_rejects_group_recall_failure_when_overall_passes(self):
        metric = self.snapshot["holdout"]
        for row in [row for row in metric["queries"] if row["modality"] == "code"][:5]:
            row["top10"], row["rank"] = [], None
        recompute_metric(metric, 10)
        self.assertGreaterEqual(metric["positive"]["recallAt10"], .9)
        self.rejects("did not satisfy")

    def test_accepts_exact_final_recall_and_false_return_boundaries(self):
        metric = self.snapshot["holdout"]
        for row in metric["queries"][:8]:
            row["top10"], row["rank"] = [], None
        for row in metric["noAnswerQueries"][:8]:
            row["top10"], row["returned"] = ["unexpected"], True
        recompute_metric(metric, 10)
        self.assertEqual(metric["positive"]["recallAt10"], .9)
        self.assertEqual(metric["noAnswer"]["falseReturnRate"], .1)
        self.assertEqual(self.errors(), [])

    def test_training_false_return_gate_is_five_percent(self):
        metric = self.snapshot["development"]["metrics"]
        for row in metric["noAnswerQueries"][:5]:
            row["top10"], row["returned"] = ["unexpected"], True
        recompute_metric(metric, 20)
        self.rejects("did not satisfy")

    def test_allows_a_failed_fold_when_pooled_oof_passes(self):
        fold = self.snapshot["crossValidation"]["folds"][0]["validation"]
        # A 13.33% negative-return rate fails this individual validation fold.
        # The pooled 4/96 rate is below 10%, as required by the fixed protocol.
        for row in fold["noAnswerQueries"][:4]:
            row["top10"], row["returned"] = ["unexpected"], True
        recompute_metric(fold, 10)
        oof = self.snapshot["crossValidation"]["outOfFold"]
        replacements = {row["query"]: row for row in fold["noAnswerQueries"]}
        oof["noAnswerQueries"] = [copy.deepcopy(replacements.get(row["query"], row)) for row in oof["noAnswerQueries"]]
        recompute_metric(oof, 10)
        self.assertFalse(fold["passed"])
        self.assertEqual(self.errors(), [])

    def test_rejects_topic_family_or_file_leakage(self):
        for field in ("topic", "family", "expectedFile"):
            with self.subTest(field=field):
                self.report = copy.deepcopy(self.valid[2])
                first = self.report["queries"][0]
                other = next(row for row in self.report["queries"] if row.get("fold") == 1)
                other[field] = first[field]
                self.rejects("leaks")

    def test_rejects_holdout_with_development_fold(self):
        next(row for row in self.report["queries"] if row["partition"] == "holdout")["fold"] = 0
        self.rejects("assigns a development fold")

    def test_rejects_mismatched_query_metadata(self):
        self.snapshot["holdout"]["queries"][0]["topic"] = "invented-topic"
        self.rejects("differs from query metadata")

    def test_rejects_missing_holdout_and_false_coverage_claim(self):
        self.snapshot["holdout"]["queries"].pop()
        self.rejects("complete expected query set")
        self.snapshot = copy.deepcopy(self.valid[0])
        self.snapshot["coverage"]["missingHoldoutGroups"] = ["modality:image"]
        self.rejects("coverage")

    def test_rejects_fabricated_optimality_but_accepts_honest_budget_limit(self):
        search = self.snapshot["development"]["search"]
        search["remainingBoxes"] = 2
        self.rejects("optimality")
        search["optimalityProven"] = False
        search["status"] = "feasible-search-budget-exhausted"
        search["visitedBoxes"] = search["maxStates"]
        self.assertEqual(self.errors(), [])

    def test_rejects_fabricated_quarter_placement_and_objective(self):
        placement = self.snapshot["crossValidation"]["folds"][0]["training"]["thresholdSelection"]
        placement["intervals"]["text"]["acceptedMinimum"] = .9
        self.rejects("fixed quarter")
        self.snapshot = copy.deepcopy(self.valid[0])
        self.snapshot["development"]["search"]["objective"]["overallRecallAt10"] = .99
        self.rejects("objective.overallRecallAt10")

    def test_rejects_missing_rank_even_for_a_valid_miss(self):
        row = self.snapshot["holdout"]["queries"][0]
        row["top10"] = []
        del row["rank"]
        self.rejects("rank disagrees")

    def test_rejects_out_of_range_observed_interval_boundaries(self):
        for name, value in (("rejectedMaximum", -1.1), ("acceptedMinimum", 1.1)):
            with self.subTest(name=name):
                placement = copy.deepcopy(self.snapshot["development"]["thresholdSelection"])
                placement["intervals"]["text"][name] = value
                with self.assertRaisesRegex(ValueError, "observed cosine range"):
                    evidence._validate_placement(placement, self.snapshot["thresholds"], "placement")

    def test_rejects_top_level_selection_type_substitution(self):
        self.snapshot["thresholdSelection"]["calibrationAcceptanceEquivalent"] = 1
        self.rejects("final threshold selection")

    def test_rejects_negative_query_space_flip(self):
        for code in (True, False):
            with self.subTest(code=code):
                self.report = copy.deepcopy(self.valid[2])
                row = next(row for row in self.report["noAnswerQueries"] if row["code"] == code)
                row["code"] = not code
                self.rejects("wrong ordinary/code query space")

    def test_returns_errors_for_malformed_json_shapes_and_numbers(self):
        for value in (None, [], {"accepted": True}, {"calibrationProtocol": evidence.PROTOCOL}):
            with self.subTest(value=value):
                self.assertTrue(evidence.validate_calibration_snapshot(value, self.policy, self.report))
        self.snapshot["holdout"]["positive"]["recallAt10"] = float("nan")
        self.rejects("finite number")


if __name__ == "__main__":
    unittest.main(verbosity=2)
