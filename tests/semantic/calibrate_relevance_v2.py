#!/usr/bin/env python3
"""Grouped-CV relevance calibration with a previously unseen final holdout.

The v1 protocol and snapshots remain unchanged. This protocol fits only on
development rows, validates the fixed selection procedure with three grouped
folds, refits all development rows, and only then inspects final-holdout scores.
Search budgets are explicit. A feasible policy at budget exhaustion is evidence
of measured quality, not a proof of optimality.
"""
from __future__ import annotations

import argparse
import bisect
from fractions import Fraction
import hashlib
import heapq
import importlib.util
import json
import math
from pathlib import Path
import sys

sys.dont_write_bytecode = True
_spec = importlib.util.spec_from_file_location("qcs_relevance_v1_helpers", Path(__file__).with_name("calibrate_relevance.py"))
v1 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(v1)

PROTOCOL = "grouped-cv-v2"
POLICY_VERSION = "qcs-relevance-v2"
MODALITIES = v1.MODALITIES
DEFAULT_MAX_STATES = 25000
TRAIN_NEGATIVE_DIVISOR = 20
EVALUATION_NEGATIVE_DIVISOR = 10
OBJECTIVE = ["minimum-language-or-policy-modality-recall", "overall-recall", "fewer-negative-returns",
             "larger-minimum-calibration-margin", "larger-total-calibration-margin", "lower-thresholds"]


def validate_metadata(report):
    """Validate frozen grouping without consulting any final-holdout score."""
    if not isinstance(report, dict) or report.get("calibrationProtocol") != PROTOCOL:
        raise ValueError(f"calibrationProtocol must be {PROTOCOL}")
    families, topics, files = {}, {}, {}
    rows = []
    for field, positive in (("queries", True), ("noAnswerQueries", False)):
        if not isinstance(report.get(field), list):
            raise ValueError(f"{field} must be an array")
        for index, row in enumerate(report[field]):
            where = f"{field}[{index}]"
            if not isinstance(row, dict):
                raise ValueError(f"{where} must be an object")
            for name in ("query", "topic", "family", "language", "modality"):
                v1._string(row.get(name), f"{where}.{name}")
            partition = row.get("partition")
            if partition not in ("development", "holdout"):
                raise ValueError(f"{where}.partition must be development or holdout")
            if partition == "development":
                fold = row.get("fold")
                if type(fold) is not int or fold not in (0, 1, 2):
                    raise ValueError(f"{where}.fold must be the preassigned integer 0, 1 or 2")
            else:
                if "fold" in row:
                    raise ValueError(f"{where}: final holdout must not have a development fold")
                fold = None
            location = (partition, fold)
            for groups, key, label in ((families, row["family"], "family"), (topics, row["topic"], "topic")):
                if groups.setdefault(key, location) != location:
                    raise ValueError(f"{label} leakage across partitions or folds: {key}")
            if type(row.get("code")) is not bool:
                raise ValueError(f"{where}.code must be a boolean")
            if positive:
                file = v1._string(row.get("expectedFile"), f"{where}.expectedFile")
                file_group = (row["family"], location)
                if files.setdefault((row["code"], file), file_group) != file_group:
                    raise ValueError(f"file leakage across semantic families, partitions or folds: {file}")
                modality = row.get("policyModality")
                if modality not in MODALITIES or (modality == "code") != row["code"]:
                    raise ValueError(f"{where}.policyModality must be a fixed target modality matching query space")
            rows.append((row, positive))
    development = [(row, positive) for row, positive in rows if row["partition"] == "development"]
    for fold in range(3):
        for positive in (True, False):
            if not any(row["fold"] == fold and flag == positive for row, flag in development):
                raise ValueError(f"development fold {fold} needs both positive and negative queries")
    if not any(row["partition"] == "holdout" and positive for row, positive in rows):
        raise ValueError("final holdout needs positive queries")
    if not any(row["partition"] == "holdout" and not positive for row, positive in rows):
        raise ValueError("final holdout needs negative queries")
    return rows


def _validate_scores(report, rows):
    adapted = dict(report)
    adapted["queries"] = [dict(row, partition="calibration", topic=row["family"])
                          for row, positive in rows if positive]
    adapted["noAnswerQueries"] = [dict(row, partition="calibration", topic=row["family"])
                                  for row, positive in rows if not positive]
    v1.validate_report(adapted)


def validate_raw_replay(rows):
    """Compare exact raw-RRF replay to the production-exported ranking."""
    thresholds = {modality: -1.000001 for modality in MODALITIES}
    for row, _ in rows:
        exported = row.get("rawUnfilteredTop10")
        if not isinstance(exported, list):
            raise ValueError(f"rawUnfilteredTop10 is required: {row['query']}")
        files = []
        for item in exported:
            file = item.get("file") if isinstance(item, dict) else item
            files.append(v1._string(file, "rawUnfilteredTop10.file"))
        expected = v1.rank_query(row, thresholds)
        if files != expected:
            raise ValueError(f"production raw RRF replay mismatch: {row['query']}")
        # File order is mandatory. Existing object exports also provide score
        # and source, which are checked instead of silently discarding them.
        scores, sources = {}, {}
        for item in row["keywordCandidates"]:
            if item["rank"] <= 256:
                scores[item["file"]] = 1.0 / (60.0 + item["rank"])
                sources[item["file"]] = "keyword"
        visited = set()
        for candidate in sorted(row["rawCandidates"], key=lambda c: (-c["cosine"], c.get("segmentId", 0), c["file"])):
            file = candidate["file"]
            if file in visited:
                continue
            visited.add(file)
            if len(visited) > 256:
                break
            scores[file] = scores.get(file, 0.0) + 1.0 / (60.0 + len(visited))
            sources[file] = "hybrid" if file in sources else "semantic"
        for item, file in zip(exported, files):
            if not isinstance(item, dict):
                continue
            if "score" in item and (type(item["score"]) not in (int, float)
                                    or not math.isfinite(item["score"])
                                    or not math.isclose(item["score"], scores[file], rel_tol=1e-10, abs_tol=1e-12)):
                raise ValueError(f"production raw RRF score mismatch: {row['query']}")
            if "source" in item and item["source"] != sources[file]:
                raise ValueError(f"production raw RRF source mismatch: {row['query']}")
    return {"status": "passed", "validatedQueries": len(rows)}


def _group_counts(positives, flags, axes):
    counts = {}
    for row, hit in zip(positives, flags):
        for axis in axes:
            group = counts.setdefault(f"{axis}:{row[axis]}", {"queries": 0, "hits": 0})
            group["queries"] += 1
            group["hits"] += int(hit)
    for group in counts.values():
        group["recallAt10"] = group["hits"] / group["queries"]
    return dict(sorted(counts.items()))


def _metrics(rows, positive_results, negative_results, negative_divisor):
    positives = [row for row, positive in rows if positive]
    flags = [result["rank"] is not None for result in positive_results]
    recall = v1._recall_gates(positives, flags)
    recall["policyGroups"] = _group_counts(positives, flags, ("language", "policyModality"))
    returned = sum(result["returned"] for result in negative_results)
    negatives = len(negative_results)
    no_answer = {"queries": negatives, "returned": returned,
                 "falseReturnRate": returned / negatives if negatives else None,
                 "maximumFalseReturnRate": 1.0 / negative_divisor,
                 "passed": bool(negatives) and returned * negative_divisor <= negatives}
    return {"passed": recall["passed"] and no_answer["passed"], "positive": recall,
            "noAnswer": no_answer, "queries": positive_results, "noAnswerQueries": negative_results}


def evaluate(rows, thresholds, negative_divisor=EVALUATION_NEGATIVE_DIVISOR):
    result = v1.evaluate(rows, thresholds)
    for positive, field in ((True, "queries"), (False, "noAnswerQueries")):
        source = [row for row, flag in rows if flag == positive]
        for row, item in zip(source, result[field]):
            item["family"] = row["family"]
            if "fold" in row:
                item["fold"] = row["fold"]
            if positive:
                item["policyModality"] = row["policyModality"]
    return _metrics(rows, result["queries"], result["noAnswerQueries"], negative_divisor)


class _Evidence:
    """Precompute rank-invariant pieces and safe objective upper bounds."""
    def __init__(self, rows):
        self.positives = [row for row, positive in rows if positive]
        self.negatives = [row for row, positive in rows if not positive]
        self.hard_groups, self.objective_groups = {}, {}
        self.rankers, self.expected, self.negative = [], [], []
        for index, row in enumerate(self.positives):
            for group, axes in ((self.hard_groups, ("language", "modality")),
                                (self.objective_groups, ("language", "policyModality"))):
                for axis in axes:
                    group.setdefault((axis, row[axis]), []).append(index)
            keyword = {c["file"]: 1.0 / (60.0 + c["rank"])
                       for c in row["keywordCandidates"] if c["rank"] <= 256}
            raw = [(c["file"], MODALITIES.index(c["policyModality"]), c["cosine"])
                   for c in sorted(row["rawCandidates"], key=lambda c: (-c["cosine"], c.get("segmentId", 0), c["file"]))]
            self.rankers.append((row["expectedFile"], keyword, raw))
            maxima = tuple(max((c["cosine"] for c in row["rawCandidates"]
                                if c["file"] == row["expectedFile"] and c["policyModality"] == modality),
                               default=-math.inf) for modality in MODALITIES)
            self.expected.append((row["expectedFile"] in keyword, maxima))
        for row in self.negatives:
            maxima = tuple(max((c["cosine"] for c in row["rawCandidates"] if c["policyModality"] == modality),
                               default=-math.inf) for modality in MODALITIES)
            self.negative.append((bool(row["keywordCandidates"]), maxima))

    def quality(self, flags):
        total, hits = len(flags), sum(flags)
        passed = bool(total) and hits * 10 >= total * 9 and all(
            sum(flags[i] for i in members) * 5 >= len(members) * 4 for members in self.hard_groups.values())
        worst = min((Fraction(sum(flags[i] for i in members), len(members))
                     for members in self.objective_groups.values()), default=Fraction(0))
        return passed, worst, Fraction(hits, total) if total else Fraction(0)

    def flags(self, thresholds):
        result = []
        for expected, keyword, raw in self.rankers:
            scores = dict(keyword)
            visited = set()
            for file, axis, score in raw:
                if score < thresholds[axis] or file in visited:
                    continue
                visited.add(file)
                scores[file] = scores.get(file, 0.0) + 1.0 / (60.0 + len(visited))
                if len(visited) == 256:
                    break
            score = scores.get(expected)
            result.append(score is not None and sum(
                value > score or (value == score and file < expected) for file, value in scores.items()) < 10)
        return result

    def negative_returns(self, thresholds):
        return sum(keyword or any(score >= threshold for score, threshold in zip(maxima, thresholds))
                   for keyword, maxima in self.negative)


class _Axis:
    def __init__(self, values):
        self.values = values
        self.boundaries = [values[0]] + [math.nextafter(value, math.inf) for value in values]
        self.thresholds = [values[0]]
        for rejected, retained in zip(values, values[1:]):
            middle = rejected + (retained - rejected) / 2.0
            self.thresholds.append(min(retained, math.nextafter(rejected, math.inf) if middle <= rejected else middle))
        self.thresholds.append(v1.REJECT_ALL)
        self.margins = [0.0] + [min(threshold - values[i - 1], values[i] - threshold)
                               for i, threshold in enumerate(self.thresholds[1:-1], 1)]
        self.margins.append(v1.REJECT_ALL - values[-1])
        self.maximum = [self.margins]
        step = 1
        while step * 2 <= len(self.margins):
            previous = self.maximum[-1]
            self.maximum.append([max(previous[i], previous[i + step])
                                 for i in range(len(self.margins) - step * 2 + 1)])
            step *= 2

    def maximum_margin(self, lower, upper):
        power = (upper - lower + 1).bit_length() - 1
        return max(self.maximum[power][lower], self.maximum[power][upper - (1 << power) + 1])


def _objective_json(objective):
    return {"minimumLanguageOrPolicyRecallAt10": float(objective[0]),
            "overallRecallAt10": float(objective[1]), "noAnswerReturns": -objective[2],
            "minimumCalibrationMargin": objective[3], "totalCalibrationMargin": objective[4]}


def optimize(rows, max_states=DEFAULT_MAX_STATES):
    """Optimize a fixed lexicographic objective with safe branch-and-bound.

    Every calibration acceptance set is represented. Expected-file membership
    bounds recall optimistically, negative unions bound false returns, and
    interval range maxima bound margin. Consequently only exhausted search
    proves optimality or infeasibility. A budget-limited incumbent is retained
    with its actual, unproven-optimal search status.
    """
    evidence = _Evidence(rows)
    values = [sorted({float(c["cosine"]) for row, _ in rows for c in row["rawCandidates"]
                      if c["policyModality"] == modality}) for modality in MODALITIES]
    stats = {"algorithm": "exact-objective-branch-and-bound", "objectiveOrder": OBJECTIVE,
             "maxStates": max_states, "visitedBoxes": 0, "evaluatedPolicies": 0,
             "prunedBoxes": 0, "optimalityProven": False,
             "trainingMaximumFalseReturnRate": 0.05,
             "gridSizes": {modality: len(scores) + 1 for modality, scores in zip(MODALITIES, values)}}
    if any(not scores for scores in values) or not evidence.positives or not evidence.negatives:
        stats["status"] = "insufficient-training-evidence"
        return None, stats
    axes = [_Axis(scores) for scores in values]
    negative_budget = len(evidence.negatives) // TRAIN_NEGATIVE_DIVISOR
    fixed_negatives = sum(keyword for keyword, _ in evidence.negative)
    if fixed_negatives > negative_budget:
        stats.update(status="infeasible", optimalityProven=True,
                     reason="keyword-only negative returns exceed the training budget")
        return None, stats
    lower = []
    for axis, grid in enumerate(axes):
        maxima = sorted((scores[axis] for keyword, scores in evidence.negative if not keyword), reverse=True)
        allowance = negative_budget - fixed_negatives
        minimum = math.nextafter(maxima[allowance], math.inf) if allowance < len(maxima) else -math.inf
        lower.append(bisect.bisect_left(grid.boundaries, minimum))
    upper = tuple(len(grid.thresholds) - 1 for grid in axes)
    incumbent, incumbent_objective, incumbent_indices = None, None, None
    pending, evaluated = [], set()
    serial = 0

    def push(low, high):
        nonlocal serial
        lo = tuple(axes[i].thresholds[low[i]] for i in range(4))
        hi = tuple(axes[i].thresholds[high[i]] for i in range(4))
        possible = [keyword or any(score >= threshold for score, threshold in zip(maxima, lo))
                    for keyword, maxima in evidence.expected]
        passed, worst, overall = evidence.quality(possible)
        unavoidable = evidence.negative_returns(hi)
        margins = [axes[i].maximum_margin(low[i], high[i]) for i in range(4)]
        bound = (worst, overall, -unavoidable, min(margins), sum(margins), tuple(-value for value in lo))
        if not passed or unavoidable > negative_budget or (incumbent_objective is not None and bound <= incumbent_objective):
            stats["prunedBoxes"] += 1
            return
        priority = (-worst, -overall, unavoidable, -min(margins), -sum(margins), lo)
        heapq.heappush(pending, (priority, serial, low, high, bound))
        serial += 1

    def inspect(indices):
        nonlocal incumbent, incumbent_objective, incumbent_indices
        if indices in evaluated:
            return
        evaluated.add(indices)
        stats["evaluatedPolicies"] += 1
        thresholds = tuple(axes[i].thresholds[indices[i]] for i in range(4))
        returned = evidence.negative_returns(thresholds)
        if returned > negative_budget:
            return
        passed, worst, overall = evidence.quality(evidence.flags(thresholds))
        if not passed:
            return
        margins = [axes[i].margins[indices[i]] for i in range(4)]
        objective = (worst, overall, -returned, min(margins), sum(margins), tuple(-value for value in thresholds))
        if incumbent_objective is None or objective > incumbent_objective:
            incumbent, incumbent_objective, incumbent_indices = thresholds, objective, indices

    push(tuple(lower), upper)
    while pending and stats["visitedBoxes"] < max_states:
        _, _, low, high, bound = heapq.heappop(pending)
        stats["visitedBoxes"] += 1
        if incumbent_objective is not None and bound <= incumbent_objective:
            stats["prunedBoxes"] += 1
            continue
        inspect(low)
        if low == high:
            continue
        # Midpoint and maximum-margin representatives obtain useful incumbents
        # early. They do not replace exhaustive, safely bounded subdivision.
        inspect(tuple((low[i] + high[i]) // 2 for i in range(4)))
        axis = max(range(4), key=lambda i: (high[i] - low[i], -i))
        middle = (low[axis] + high[axis]) // 2
        left_high, right_low = list(high), list(low)
        left_high[axis], right_low[axis] = middle, middle + 1
        push(low, tuple(left_high))
        push(tuple(right_low), high)
    stats["optimalityProven"] = not pending
    stats["remainingBoxes"] = len(pending)
    if incumbent is None:
        stats["status"] = "infeasible" if not pending else "search-budget-exhausted-no-feasible-policy"
        return None, stats
    stats["status"] = "optimal" if not pending else "feasible-search-budget-exhausted"
    stats["objective"] = _objective_json(incumbent_objective)
    boundaries = {modality: axes[i].boundaries[incumbent_indices[i]] for i, modality in enumerate(MODALITIES)}
    thresholds, placement = v1._representative_thresholds(rows, boundaries)
    if tuple(thresholds[m] for m in MODALITIES) != incumbent:
        raise ValueError("internal error: optimized midpoint representation changed")
    return {"thresholds": thresholds, "thresholdSelection": placement}, stats


def _fit(rows, max_states):
    policy, search = optimize(rows, max_states)
    if policy is None:
        return {"passed": False, "search": search, "thresholds": None, "metrics": None}
    metrics = evaluate(rows, policy["thresholds"], TRAIN_NEGATIVE_DIVISOR)
    if not metrics["passed"]:
        raise ValueError("internal error: optimized policy failed training revalidation")
    return {"passed": True, "search": search, **policy, "metrics": metrics}


def calibrate_report(report, *, report_sha256=None, max_states=DEFAULT_MAX_STATES):
    """Production callers bind raw bytes. Canonical fallback is for unit fixtures."""
    if type(max_states) is not int or max_states < 1:
        raise ValueError("max_states must be a positive integer")
    rows = validate_metadata(report)
    development = [(row, positive) for row, positive in rows if row["partition"] == "development"]
    holdout = [(row, positive) for row, positive in rows if row["partition"] == "holdout"]
    _validate_scores(report, development)
    replay = {"development": validate_raw_replay(development),
              "holdout": {"status": "not-evaluated", "validatedQueries": 0}}
    config = {"protocol": PROTOCOL, "folds": 3, "maxStatesPerFit": max_states,
              "trainingMaximumFalseReturnRate": 0.05, "evaluationMaximumFalseReturnRate": 0.1,
              "objectiveOrder": OBJECTIVE, "thresholdPlacement": v1.THRESHOLD_PLACEMENT}
    development_identity = {"config": config, "modelFingerprint": report["modelFingerprint"],
                            "corpusIdentity": report["corpusIdentity"],
                            "rows": [{"positive": positive, **row} for row, positive in development]}
    development_hash = v1.canonical_hash(development_identity)
    folds, out_rows, out_positive, out_negative = [], [], [], []
    for fold in range(3):
        training = [(row, positive) for row, positive in development if row["fold"] != fold]
        validation = [(row, positive) for row, positive in development if row["fold"] == fold]
        fitted = _fit(training, max_states)
        fold_result = {"fold": fold, "training": fitted, "validation": None}
        if fitted["passed"]:
            validated = evaluate(validation, fitted["thresholds"])
            selection_hash = v1.canonical_hash({"developmentSha256": development_hash, "fold": fold,
                                                "thresholds": fitted["thresholds"]})
            for item in validated["queries"] + validated["noAnswerQueries"]:
                item["foldPolicySha256"] = selection_hash
            fold_result["validation"] = validated
            out_rows.extend(validation)
            out_positive.extend(validated["queries"])
            out_negative.extend(validated["noAnswerQueries"])
        folds.append(fold_result)
    out_of_fold = _metrics(out_rows, out_positive, out_negative, EVALUATION_NEGATIVE_DIVISOR)
    complete_cv = all(fold["training"]["passed"] for fold in folds)
    cv_passed = complete_cv and out_of_fold["passed"]
    cross_validation = {"passed": cv_passed, "complete": complete_cv, "folds": folds,
                        "outOfFold": out_of_fold}
    final = _fit(development, max_states) if cv_passed else None
    calibrated = final is not None and final["passed"]
    thresholds = final["thresholds"] if calibrated else {m: v1.REJECT_ALL for m in MODALITIES}
    selection_hash = v1.canonical_hash({"developmentSha256": development_hash, "thresholds": thresholds})
    holdout_result = {"passed": False, "status": "not-evaluated",
                      "reason": "development and grouped-CV gates must pass before the final holdout is inspected"}
    coverage = {"passed": False, "status": "not-evaluated"}
    # Final thresholds and their selection identity are frozen before any
    # final-holdout score is validated, replayed or evaluated.
    if calibrated:
        _validate_scores(report, holdout)
        replay["holdout"] = validate_raw_replay(holdout)
        holdout_result = evaluate(holdout, thresholds)
        development_groups = set(final["metrics"]["positive"]["groups"])
        holdout_groups = set(holdout_result["positive"]["groups"])
        coverage = {"missingHoldoutGroups": sorted(development_groups - holdout_groups),
                    "additionalHoldoutGroups": sorted(holdout_groups - development_groups)}
        coverage["passed"] = not coverage["missingHoldoutGroups"]
    accepted = calibrated and holdout_result["passed"] and coverage["passed"]
    status = ("accepted" if accepted else "holdout-failed" if calibrated else
              "development-fit-failed" if cv_passed else "cross-validation-failed")
    return {"version": f"{POLICY_VERSION}-{selection_hash[:12]}", "calibrationProtocol": PROTOCOL,
            "modelFingerprint": report["modelFingerprint"], "corpusIdentity": report["corpusIdentity"],
            "corpusSha256": v1.canonical_hash(report["corpusIdentity"]),
            "reportSha256": report_sha256 or v1.canonical_hash(report),
            "developmentSha256": development_hash, "selectionSha256": selection_hash,
            "thresholds": thresholds,
            "thresholdSelection": final.get("thresholdSelection") if calibrated else None,
            "calibrated": calibrated, "accepted": accepted, "status": status,
            "config": config, "rawReplay": replay, "crossValidation": cross_validation,
            "development": final, "holdout": holdout_result, "coverage": coverage,
            "criteria": {"rankLimit": 10, "rrfConstant": 60,
                         "semanticFileCandidateLimit": 256, "keywordFileCandidateLimit": 256,
                         "overallRecallAt10Minimum": 0.9, "groupRecallAt10Minimum": 0.8,
                         "trainingNoAnswerFalseReturnRateMaximum": 0.05,
                         "noAnswerFalseReturnRateMaximum": 0.1,
                         "groupAxes": ["language", "modality"], "comparison": ">="}}


def cpp_header(policy):
    if policy.get("calibrationProtocol") != PROTOCOL or not policy.get("crossValidation", {}).get("passed"):
        raise ValueError("v2 headers require passed grouped cross-validation")
    return v1.cpp_header(policy).replace("Generated by tests/semantic/calibrate_relevance.py",
                                         "Generated by tests/semantic/calibrate_relevance_v2.py")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--header", type=Path)
    parser.add_argument("--max-states", type=int, default=DEFAULT_MAX_STATES,
                        help="Maximum boxes per fold fit and final fit, not a claim of optimality")
    args = parser.parse_args(argv)
    try:
        if args.output.resolve().is_relative_to(Path(__file__).resolve().parents[2]):
            raise ValueError("generated policy reports must be outside the source repository")
        paths = [args.report.resolve(), args.output.resolve()]
        if args.header:
            paths.append(args.header.resolve())
        if len(set(paths)) != len(paths):
            raise ValueError("report, policy output and header paths must be distinct")
        raw = args.report.read_bytes()
        report = json.loads(raw.decode("utf-8-sig"))
        policy = calibrate_report(report, report_sha256=hashlib.sha256(raw).hexdigest(), max_states=args.max_states)
        v1._write_atomic(args.output, v1.serialize_policy(policy))
        if args.header and policy["accepted"]:
            v1._write_atomic(args.header, cpp_header(policy))
        print(json.dumps({"status": policy["status"], "calibrated": policy["calibrated"],
                          "accepted": policy["accepted"], "output": str(args.output),
                          "headerWritten": bool(args.header and policy["accepted"])}, ensure_ascii=False))
        return 0 if policy["accepted"] else 1
    except (ValueError, OSError) as error:
        print(f"v2 relevance calibration failed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
