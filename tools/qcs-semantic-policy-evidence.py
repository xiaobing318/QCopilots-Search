#!/usr/bin/env python3
"""Validate the frozen grouped-CV policy without refitting or running a model.

The caller binds the snapshot bytes to the compiled header and verifies the
source manifests. This module checks the snapshot's identities, complete query
sets, grouped-fold isolation and independently recomputed quality summaries.
Historical rankings may differ from a later native-platform production run.
Their query identities must agree, but their top-ten ordering need not agree.
"""
from __future__ import annotations

from collections import defaultdict
import hashlib
import json
import math
import re


PROTOCOL = "grouped-cv-v2-quarter"
MODALITIES = ("text", "image", "audio", "code")
OBJECTIVE = ["minimum-language-or-policy-modality-recall", "overall-recall", "fewer-negative-returns",
             "larger-minimum-midpoint-reference-margin", "larger-total-midpoint-reference-margin", "lower-thresholds"]


def _need(condition, message):
    if not condition:
        raise ValueError(message)


def _object(value, where):
    _need(isinstance(value, dict), where + " must be an object")
    return value


def _array(value, where):
    _need(isinstance(value, list), where + " must be an array")
    return value


def _text(value, where):
    _need(isinstance(value, str) and bool(value.strip()), where + " must be a nonempty string")
    return value


def _integer(value, where, minimum=0):
    _need(type(value) is int and value >= minimum, where + f" must be an integer >= {minimum}")
    return value


def _number(value, where):
    _need(type(value) in (int, float) and math.isfinite(value), where + " must be a finite number")
    return value


def _same_number(actual, expected, where):
    _need(math.isclose(_number(actual, where), expected, rel_tol=1e-12, abs_tol=1e-12),
          where + " disagrees with recomputed evidence")


def _sha(value, where):
    _need(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None,
          where + " must be a lowercase SHA-256 digest")
    return value


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def _thresholds(value, where):
    value = _object(value, where)
    _need(set(value) == set(MODALITIES), where + " must contain exactly the four policy channels")
    for modality, threshold in value.items():
        _need(-1.00001 <= _number(threshold, where + "." + modality) <= 1.00001,
              where + "." + modality + " must be a cosine boundary")
    return value


def _report_records(report):
    """Index immutable query metadata independently of historical rankings."""
    records, texts, locations, files = {}, set(), {}, {}
    for field, positive in (("queries", True), ("noAnswerQueries", False)):
        for index, source in enumerate(_array(report.get(field), "report." + field)):
            where = f"report.{field}[{index}]"
            row = _object(source, where)
            query = _text(row.get("query"), where + ".query")
            _need(query not in texts, where + " duplicates a positive or no-answer query")
            texts.add(query)
            for name in ("topic", "family", "language", "modality"):
                _text(row.get(name), where + "." + name)
            _need(type(row.get("code")) is bool, where + ".code must be a boolean")
            _need(row["code"] == (row["modality"] == "code"),
                  where + " uses the wrong ordinary/code query space for its modality")
            partition = row.get("partition")
            _need(partition in ("development", "holdout"), where + " has an unsupported partition")
            if partition == "development":
                fold = _integer(row.get("fold"), where + ".fold")
                _need(fold in (0, 1, 2), where + " has an unsupported development fold")
            else:
                _need("fold" not in row, where + " assigns a development fold to final holdout")
                fold = None
            location = (partition, fold)
            for axis in ("topic", "family"):
                key = (axis, row[axis])
                _need(locations.setdefault(key, location) == location,
                      where + " leaks a " + axis + " across partitions or folds")
            if positive:
                expected = _text(row.get("expectedFile"), where + ".expectedFile")
                _need(row.get("policyModality") in MODALITIES
                      and (row["policyModality"] == "code") == row["code"],
                      where + " has an invalid policy modality for its query space")
                file_group = (row["family"], location)
                _need(files.setdefault((row["code"], expected), file_group) == file_group,
                      where + " leaks an expected file across families, partitions or folds")
            records[(positive, query)] = row
    return records


def _metric_records(metric, expected, where, fold_policy=None):
    records = {}
    for field, positive in (("queries", True), ("noAnswerQueries", False)):
        for index, source in enumerate(_array(metric.get(field), where + "." + field)):
            path = f"{where}.{field}[{index}]"
            row = _object(source, path)
            key = (positive, _text(row.get("query"), path + ".query"))
            _need(key in expected and key not in records, path + " is duplicate or outside its expected query set")
            original = expected[key]
            fields = ("topic", "family", "language", "modality")
            if positive:
                fields += ("expectedFile", "policyModality")
            for name in fields:
                _need(row.get(name) == original.get(name), path + "." + name + " differs from query metadata")
            if "fold" in original:
                _need(type(row.get("fold")) is int and row["fold"] == original["fold"],
                      path + ".fold differs from the frozen development assignment")
            else:
                _need("fold" not in row, path + " assigns a development fold to final holdout")
            if fold_policy is not None:
                _need(row.get("foldPolicySha256") == fold_policy, path + " uses another fold policy")
            else:
                _need("foldPolicySha256" not in row, path + " unexpectedly uses an out-of-fold policy")
            top = _array(row.get("top10"), path + ".top10")
            for value in top:
                _text(value, path + ".top10 file")
            _need(len(top) <= 10 and len(set(top)) == len(top), path + ".top10 is oversized or contains duplicates")
            if positive:
                rank = top.index(original["expectedFile"]) + 1 if original["expectedFile"] in top else None
                _need("rank" in row and (row.get("rank") is None if rank is None else type(row.get("rank")) is int)
                      and row.get("rank") == rank, path + ".rank disagrees with its top10")
            else:
                _need(type(row.get("returned")) is bool and row["returned"] == bool(top),
                      path + ".returned disagrees with its top10")
            records[key] = row
    _need(set(records) == set(expected), where + " does not contain the complete expected query set")
    return records


def _validate_groups(actual, expected, where, *, include_passed):
    actual = _object(actual, where)
    _need(set(actual) == set(expected), where + " does not contain exactly the observed groups")
    for name, (count, hits) in expected.items():
        group = _object(actual[name], where + "." + name)
        _need(type(group.get("queries")) is int and group["queries"] == count
              and type(group.get("hits")) is int and group["hits"] == hits,
              where + "." + name + " counts disagree with query records")
        _same_number(group.get("recallAt10"), hits / count, where + "." + name + ".recallAt10")
        if include_passed:
            _need(group.get("passed") is (hits * 5 >= count * 4),
                  where + "." + name + ".passed disagrees with the 80% group gate")


def _validate_metrics(metric, expected, where, divisor, *, require_passed, fold_policy=None,
                      out_of_fold=None):
    metric = _object(metric, where)
    # OOF combines three validation policies, so verify each row against its own
    # fold before doing the aggregate quality check.
    if out_of_fold is None:
        records = _metric_records(metric, expected, where, fold_policy)
    else:
        records = {}
        for field, positive in (("queries", True), ("noAnswerQueries", False)):
            for index, row in enumerate(_array(metric.get(field), where + "." + field)):
                row = _object(row, f"{where}.{field}[{index}]")
                key = (positive, _text(row.get("query"), where + ".query"))
                _need(key in out_of_fold and key not in records,
                      where + " duplicates or invents a validation query")
                # JSON identity preserves boolean/integer/float distinctions.
                # Python dict equality alone would accept rank=true as rank=1.
                _need(_hash(row) == _hash(out_of_fold[key]),
                      where + " row differs from its held-out fold validation record")
                records[key] = row
        _need(set(records) == set(expected), where + " does not cover every development query exactly once")
    positive = _object(metric.get("positive"), where + ".positive")
    positives = [(key, row) for key, row in records.items() if key[0]]
    negatives = [row for key, row in records.items() if not key[0]]
    _need(bool(positives) and bool(negatives), where + " needs both positive and no-answer evidence")
    groups, policy_groups = defaultdict(lambda: [0, 0]), defaultdict(lambda: [0, 0])
    hits = 0
    for key, row in positives:
        hit = row["rank"] is not None
        hits += hit
        for target, axes in ((groups, ("language", "modality")),
                             (policy_groups, ("language", "policyModality"))):
            for axis in axes:
                counts = target[axis + ":" + expected[key][axis]]
                counts[0] += 1
                counts[1] += hit
    _need(type(positive.get("queries")) is int and positive["queries"] == len(positives)
          and type(positive.get("hits")) is int and positive["hits"] == hits,
          where + ".positive counts disagree with query records")
    _same_number(positive.get("recallAt10"), hits / len(positives), where + ".positive.recallAt10")
    _validate_groups(positive.get("groups"), groups, where + ".positive.groups", include_passed=True)
    _validate_groups(positive.get("policyGroups"), policy_groups, where + ".positive.policyGroups", include_passed=False)
    recall_passed = hits * 10 >= len(positives) * 9 and all(hit * 5 >= count * 4 for count, hit in groups.values())
    _need(positive.get("passed") is recall_passed, where + ".positive.passed disagrees with the 90%/80% gates")
    no_answer = _object(metric.get("noAnswer"), where + ".noAnswer")
    returned = sum(row["returned"] for row in negatives)
    _need(type(no_answer.get("queries")) is int and no_answer["queries"] == len(negatives)
          and type(no_answer.get("returned")) is int and no_answer["returned"] == returned,
          where + ".noAnswer counts disagree with query records")
    _same_number(no_answer.get("falseReturnRate"), returned / len(negatives), where + ".noAnswer.falseReturnRate")
    _same_number(no_answer.get("maximumFalseReturnRate"), 1 / divisor, where + ".noAnswer.maximumFalseReturnRate")
    negative_passed = returned * divisor <= len(negatives)
    _need(no_answer.get("passed") is negative_passed, where + ".noAnswer.passed disagrees with its false-return gate")
    passed = recall_passed and negative_passed
    _need(metric.get("passed") is passed, where + ".passed disagrees with recomputed quality")
    _need(not require_passed or passed, where + " did not satisfy the required recall and false-return gates")
    return records


def _validate_placement(placement, thresholds, where):
    """Recompute the quarter boundaries instead of accepting method labels."""
    intervals = _object(placement.get("intervals"), where + ".intervals")
    _need(set(intervals) == set(MODALITIES), where + " does not describe every policy interval")
    search_thresholds = _thresholds(placement.get("searchThresholds"), where + ".searchThresholds")
    reference_margins, deployment_margins = [], []
    for modality in MODALITIES:
        path = where + ".intervals." + modality
        interval = _object(intervals[modality], path)
        _need("rejectedMaximum" in interval and "acceptedMinimum" in interval,
              path + " omits an interval boundary")
        rejected, retained = interval["rejectedMaximum"], interval["acceptedMinimum"]
        _need(rejected is not None or retained is not None, path + " has no observed training scores")
        if rejected is not None:
            _need(-1.000001 <= _number(rejected, path + ".rejectedMaximum") <= 1.000001,
                  path + ".rejectedMaximum is outside the observed cosine range")
        if retained is not None:
            _need(-1.000001 <= _number(retained, path + ".acceptedMinimum") <= 1.000001,
                  path + ".acceptedMinimum is outside the observed cosine range")
        if retained is None:
            threshold = reference = math.nextafter(1.000001, math.inf)
        elif rejected is None:
            threshold = reference = retained
        else:
            _need(rejected < retained, path + " is not an ordered acceptance interval")
            threshold = min(retained, max(math.nextafter(rejected, math.inf), rejected + (retained - rejected) * .25))
            reference = min(retained, max(math.nextafter(rejected, math.inf), rejected + (retained - rejected) / 2))
        _need(thresholds[modality] == threshold, path + " threshold is not the fixed quarter representative")
        _need((rejected is None or search_thresholds[modality] > rejected)
              and (retained is None or search_thresholds[modality] <= retained),
              path + " search boundary does not preserve the declared training acceptance interval")
        _same_number(interval.get("midpointReferenceThreshold"), reference, path + ".midpointReferenceThreshold")
        reference_values = []
        for field, boundary, margin, reference_margin in (
                ("rejectedMargin", rejected, threshold - rejected if rejected is not None else None,
                 reference - rejected if rejected is not None else None),
                ("acceptedMargin", retained, retained - threshold if retained is not None else None,
                 retained - reference if retained is not None else None)):
            _need(field in interval, path + " omits its " + field)
            if boundary is None:
                _need(interval[field] is None, path + "." + field + " exists without its boundary")
            else:
                _same_number(interval[field], margin, path + "." + field)
                deployment_margins.append(margin)
                reference_values.append(reference_margin)
        reference_margins.append(min(reference_values))
        _same_number(interval.get("midpointReferenceMargin"), reference_margins[-1], path + ".midpointReferenceMargin")
    _same_number(placement.get("minimumDeploymentMargin"), min(deployment_margins), where + ".minimumDeploymentMargin")
    return reference_margins


def _validate_fit(fit, expected, where, max_states):
    fit = _object(fit, where)
    _need(fit.get("passed") is True, where + " did not produce a feasible training policy")
    thresholds = _thresholds(fit.get("thresholds"), where + ".thresholds")
    placement = _object(fit.get("thresholdSelection"), where + ".thresholdSelection")
    _need(placement.get("method") == "calibration-interval-quarter"
          and placement.get("calibrationAcceptanceEquivalent") is True,
          where + " did not retain the fixed quarter training acceptance rule")
    _same_number(placement.get("interiorFraction"), .25, where + ".thresholdSelection.interiorFraction")
    reference_margins = _validate_placement(placement, thresholds, where + ".thresholdSelection")
    search = _object(fit.get("search"), where + ".search")
    _need(search.get("algorithm") == "exact-objective-branch-and-bound"
          and search.get("objectiveOrder") == OBJECTIVE, where + " uses another optimization procedure")
    _need(type(search.get("maxStates")) is int and search["maxStates"] == max_states,
          where + " search budget differs from the frozen configuration")
    visited = _integer(search.get("visitedBoxes"), where + ".search.visitedBoxes")
    _need(visited <= max_states, where + " exceeded its search budget")
    for name in ("evaluatedPolicies", "prunedBoxes"):
        _integer(search.get(name), where + ".search." + name)
    _need(search["evaluatedPolicies"] > 0, where + " has no evaluated feasible policy")
    grid = _object(search.get("gridSizes"), where + ".search.gridSizes")
    _need(set(grid) == set(MODALITIES), where + " search grids omit a policy channel")
    for modality, count in grid.items():
        _integer(count, where + ".search.gridSizes." + modality, 2)
    remaining = _integer(search.get("remainingBoxes"), where + ".search.remainingBoxes")
    optimal = remaining == 0
    _need(search.get("optimalityProven") is optimal and search.get("status") ==
          ("optimal" if optimal else "feasible-search-budget-exhausted"),
          where + " search status falsely claims or contradicts optimality")
    _need(optimal or visited == max_states, where + " claims budget exhaustion before using its search budget")
    _same_number(search.get("trainingMaximumFalseReturnRate"), .05, where + ".search.trainingMaximumFalseReturnRate")
    _validate_metrics(fit.get("metrics"), expected, where + ".metrics", 20, require_passed=True)
    objective = _object(search.get("objective"), where + ".search.objective")
    metrics = fit["metrics"]
    _same_number(objective.get("minimumLanguageOrPolicyRecallAt10"),
                 min(group["recallAt10"] for group in metrics["positive"]["policyGroups"].values()),
                 where + ".search.objective.minimumLanguageOrPolicyRecallAt10")
    _same_number(objective.get("overallRecallAt10"), metrics["positive"]["recallAt10"],
                 where + ".search.objective.overallRecallAt10")
    _need(type(objective.get("noAnswerReturns")) is int and objective["noAnswerReturns"] == metrics["noAnswer"]["returned"],
          where + " objective false returns disagree with training records")
    _same_number(objective.get("minimumMidpointReferenceMargin"), min(reference_margins),
                 where + ".search.objective.minimumMidpointReferenceMargin")
    _same_number(objective.get("totalMidpointReferenceMargin"), sum(reference_margins),
                 where + ".search.objective.totalMidpointReferenceMargin")
    return thresholds


def validate_calibration_snapshot(snapshot: dict, policy: dict, report: dict) -> list[str]:
    """Return explicit validation errors. Never load code or trust supplied paths.

    Byte-level snapshot/raw identities and fixed manifest membership are the
    caller's responsibility. This does not replay optimization or claim that
    the historical raw candidate stream has been independently regenerated.
    """
    try:
        snapshot = _object(snapshot, "snapshot")
        policy = _object(policy, "policy")
        report = _object(report, "report")
        _need(snapshot.get("calibrationProtocol") == PROTOCOL and report.get("calibrationProtocol") == PROTOCOL,
              "snapshot and production report must use " + PROTOCOL)
        _need(snapshot.get("accepted") is True and snapshot.get("calibrated") is True
              and snapshot.get("status") == "accepted" and policy.get("calibrated") is True,
              "snapshot is not an accepted calibrated production policy")
        for name in ("version", "modelFingerprint", "corpusSha256"):
            _text(snapshot.get(name), "snapshot." + name)
            _need(snapshot[name] == policy.get(name), "snapshot." + name + " differs from the compiled policy")
        _need(snapshot["modelFingerprint"] == report.get("modelFingerprint"), "snapshot uses another production model")
        _need(_sha(snapshot.get("reportSha256"), "snapshot.reportSha256") == policy.get("rawExportSha256"),
              "snapshot reportSha256 differs from the compiled raw export identity")
        identity = _object(snapshot.get("corpusIdentity"), "snapshot.corpusIdentity")
        _need(identity == report.get("corpusIdentity") and _hash(identity) == snapshot["corpusSha256"],
              "snapshot corpus identity differs from the production corpus or its canonical hash")
        thresholds = _thresholds(snapshot.get("thresholds"), "snapshot.thresholds")
        _need(thresholds == _thresholds(policy.get("thresholds"), "policy.thresholds"),
              "snapshot thresholds differ from the compiled policy")
        config = _object(snapshot.get("config"), "snapshot.config")
        _need(config.get("protocol") == PROTOCOL and type(config.get("folds")) is int
              and config["folds"] == 3 and config.get("objectiveOrder") == OBJECTIVE
              and config.get("thresholdPlacement") == "calibration-interval-quarter",
              "snapshot configuration is not the fixed three-fold quarter protocol")
        for name, expected in (("thresholdInteriorFraction", .25), ("trainingMaximumFalseReturnRate", .05),
                               ("evaluationMaximumFalseReturnRate", .1)):
            _same_number(config.get(name), expected, "snapshot.config." + name)
        max_states = _integer(config.get("maxStatesPerFit"), "snapshot.config.maxStatesPerFit", 1)
        criteria = _object(snapshot.get("criteria"), "snapshot.criteria")
        for name, expected in (("rankLimit", 10), ("rrfConstant", 60), ("semanticFileCandidateLimit", 256),
                               ("keywordFileCandidateLimit", 256), ("overallRecallAt10Minimum", .9),
                               ("groupRecallAt10Minimum", .8), ("trainingNoAnswerFalseReturnRateMaximum", .05),
                               ("noAnswerFalseReturnRateMaximum", .1)):
            _same_number(criteria.get(name), expected, "snapshot.criteria." + name)
        _need(criteria.get("groupAxes") == ["language", "modality"] and criteria.get("comparison") == ">=",
              "snapshot uses another grouping or cosine comparison")
        development_hash = _sha(snapshot.get("developmentSha256"), "snapshot.developmentSha256")
        selection_hash = _hash({"developmentSha256": development_hash, "thresholds": thresholds})
        _need(snapshot.get("selectionSha256") == selection_hash
              and snapshot["version"] == "qcs-relevance-v2-quarter-" + selection_hash[:12],
              "snapshot selection identity or version disagrees with its frozen thresholds")
        records = _report_records(report)
        development = {key: row for key, row in records.items() if row["partition"] == "development"}
        holdout = {key: row for key, row in records.items() if row["partition"] == "holdout"}
        for name, rows, positive_count, negative_count in (("development", development, 238, 96),
                                                          ("holdout", holdout, 80, 80)):
            _need(sum(key[0] for key in rows) == positive_count and sum(not key[0] for key in rows) == negative_count,
                  "snapshot " + name + " query coverage differs from the frozen protocol counts")
            replay = _object(_object(snapshot.get("rawReplay"), "snapshot.rawReplay").get(name), "snapshot.rawReplay." + name)
            _need(replay.get("status") == "passed" and type(replay.get("validatedQueries")) is int
                  and replay["validatedQueries"] == len(rows), "snapshot raw replay did not cover all " + name + " queries")
        cv = _object(snapshot.get("crossValidation"), "snapshot.crossValidation")
        _need(cv.get("complete") is True and cv.get("passed") is True, "snapshot grouped cross-validation is incomplete or failed")
        folds = _array(cv.get("folds"), "snapshot.crossValidation.folds")
        _need(len(folds) == 3, "snapshot must contain exactly three grouped folds")
        seen, validation_records = set(), {}
        for entry in folds:
            entry = _object(entry, "snapshot.crossValidation.fold")
            fold = _integer(entry.get("fold"), "snapshot.crossValidation.fold")
            _need(fold in (0, 1, 2) and fold not in seen, "snapshot contains an invalid or duplicate fold")
            seen.add(fold)
            training = {key: row for key, row in development.items() if row["fold"] != fold}
            validation = {key: row for key, row in development.items() if row["fold"] == fold}
            where = f"snapshot.crossValidation.fold[{fold}]"
            fold_thresholds = _validate_fit(entry.get("training"), training, where + ".training", max_states)
            fold_hash = _hash({"developmentSha256": development_hash, "fold": fold, "thresholds": fold_thresholds})
            validated = _validate_metrics(entry.get("validation"), validation, where + ".validation", 10,
                                          require_passed=False, fold_policy=fold_hash)
            validation_records.update(validated)
        _validate_metrics(cv.get("outOfFold"), development, "snapshot.crossValidation.outOfFold", 10,
                          require_passed=True, out_of_fold=validation_records)
        fitted = _validate_fit(snapshot.get("development"), development, "snapshot.development", max_states)
        _need(fitted == thresholds, "snapshot final development fit uses another threshold policy")
        _need(_hash(snapshot.get("thresholdSelection")) == _hash(snapshot["development"]["thresholdSelection"]),
              "snapshot final threshold selection differs from its development fit")
        _validate_metrics(snapshot.get("holdout"), holdout, "snapshot.holdout", 10, require_passed=True)
        development_groups = set(snapshot["development"]["metrics"]["positive"]["groups"])
        holdout_groups = set(snapshot["holdout"]["positive"]["groups"])
        missing, additional = sorted(development_groups - holdout_groups), sorted(holdout_groups - development_groups)
        coverage = _object(snapshot.get("coverage"), "snapshot.coverage")
        _need(coverage.get("missingHoldoutGroups") == missing and coverage.get("additionalHoldoutGroups") == additional
              and not missing and coverage.get("passed") is True,
              "snapshot holdout language/modality coverage is incomplete or contradicts its group records")
        return []
    except (ValueError, KeyError, TypeError, OverflowError) as error:
        return ["semantic calibration snapshot: " + str(error)]
