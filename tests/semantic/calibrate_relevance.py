#!/usr/bin/env python3
"""Fit four cosine gates from a frozen semantic report, without holdout tuning.

The finite search is exact when exhausted. A search budget produces an explicit
inconclusive result, never an assertion that no feasible policy exists. Only an
accepted policy may produce a C++ header. No model inference is performed here.
"""
from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile

MODALITIES = ("text", "image", "audio", "code")
PARTITIONS = ("calibration", "holdout")
POLICY_VERSION = "qcs-relevance-v1"
REJECT_ALL = math.nextafter(1.000001, math.inf)
DEFAULT_MAX_STATES = 250000
THRESHOLD_PLACEMENT = "calibration-interval-midpoint"


def canonical_hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def _string(value, where):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{where} must be a nonempty string")
    return value


def validate_report(report):
    """Validate evidence shape and topic separation before looking at any scores."""
    if not isinstance(report, dict):
        raise ValueError("report must be an object")
    if report.get("error") or report.get("status") in ("failed", "cleanup-failed"):
        raise ValueError("failed or incomplete inference evidence cannot calibrate a policy")
    _string(report.get("modelFingerprint"), "modelFingerprint")
    identity = report.get("corpusIdentity")
    if not isinstance(identity, dict) or not identity:
        raise ValueError("corpusIdentity must be a nonempty mapping of SHA-256 identities")
    for key, digest in identity.items():
        _string(key, "corpusIdentity key")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
            raise ValueError(f"corpusIdentity.{key} must be a SHA-256 hex digest")
    topics = {}
    rows = []
    for field, positive in (("queries", True), ("noAnswerQueries", False)):
        source = report.get(field)
        if not isinstance(source, list):
            raise ValueError(f"{field} must be an array")
        for index, row in enumerate(source):
            where = f"{field}[{index}]"
            if not isinstance(row, dict):
                raise ValueError(f"{where} must be an object")
            for name in ("query", "topic", "language", "modality"):
                _string(row.get(name), f"{where}.{name}")
            partition = row.get("partition")
            if partition not in PARTITIONS:
                raise ValueError(f"{where}.partition must be calibration or holdout")
            previous = topics.setdefault(row["topic"], partition)
            if previous != partition:
                raise ValueError(f"topic leakage across partitions: {row['topic']}")
            if type(row.get("code")) is not bool:
                raise ValueError(f"{where}.code must be a boolean")
            if positive:
                _string(row.get("expectedFile"), f"{where}.expectedFile")
            elif "expectedFile" in row:
                raise ValueError(f"{where} must not contain expectedFile")
            raw = row.get("rawCandidates")
            keyword = row.get("keywordCandidates")
            if not isinstance(raw, list) or not isinstance(keyword, list):
                raise ValueError(f"{where} must contain rawCandidates and keywordCandidates arrays")
            seen = set()
            score_files = {}
            for candidate in raw:
                if not isinstance(candidate, dict):
                    raise ValueError(f"{where}.rawCandidates items must be objects")
                file = _string(candidate.get("file"), f"{where}.rawCandidates.file")
                modality = candidate.get("policyModality")
                if modality not in MODALITIES:
                    raise ValueError(f"{where}.rawCandidates.policyModality is unsupported")
                if (modality == "code") != row["code"]:
                    raise ValueError(f"{where}: code-space candidate does not match query space")
                score = candidate.get("cosine")
                if (type(score) not in (int, float) or not math.isfinite(score)
                        or not -1.000001 <= score <= 1.000001):
                    raise ValueError(f"{where}.rawCandidates.cosine must be a finite cosine")
                if "segmentId" in candidate and (type(candidate["segmentId"]) is not int
                                                   or candidate["segmentId"] < 0):
                    raise ValueError(f"{where}.rawCandidates.segmentId must be a nonnegative integer")
                key = (file, modality)
                if key in seen:
                    raise ValueError(f"{where}: duplicate best candidate for {key}")
                seen.add(key)
                score_files.setdefault(score, []).append(candidate)
            for ties in score_files.values():
                if len({c["file"] for c in ties}) > 1 and any("segmentId" not in c for c in ties):
                    raise ValueError(f"{where}: equal-cosine files require segmentId for production tie order")
            files, ranks = set(), []
            for candidate in keyword:
                if not isinstance(candidate, dict):
                    raise ValueError(f"{where}.keywordCandidates items must be objects")
                file = _string(candidate.get("file"), f"{where}.keywordCandidates.file")
                rank = candidate.get("rank")
                if type(rank) is not int or rank < 1 or file in files:
                    raise ValueError(f"{where}: keyword files must be unique with positive integer ranks")
                files.add(file)
                ranks.append(rank)
            if sorted(ranks) != list(range(1, len(ranks) + 1)):
                raise ValueError(f"{where}: keyword ranks must be contiguous from 1")
            rows.append((row, positive))
    return rows


def rank_query(row, thresholds, limit=10):
    """Apply >= gates before file aggregation, then production RRF(k=60).

    segmentId resolves exact-cosine ties as in storage. Reports predating that
    optional field use file order, explicitly identified in the output policy.
    File identifiers must preserve the production path's lexicographic order.
    """
    accepted = [candidate for candidate in row["rawCandidates"]
                if candidate["cosine"] >= thresholds[candidate["policyModality"]]]
    accepted.sort(key=lambda c: (-c["cosine"], c.get("segmentId", 0), c["file"]))
    semantic, visited = [], set()
    for candidate in accepted:
        if candidate["file"] not in visited:
            visited.add(candidate["file"])
            semantic.append(candidate["file"])
    candidate_limit = min(max(limit * 16, 256), 16000)
    scores = {}
    for candidate in sorted(row["keywordCandidates"], key=lambda c: c["rank"]):
        if candidate["rank"] > candidate_limit:
            break
        scores[candidate["file"]] = 1.0 / (60.0 + candidate["rank"])
    for rank, file in enumerate(semantic[:candidate_limit], 1):
        scores[file] = scores.get(file, 0.0) + 1.0 / (60.0 + rank)
    return sorted(scores, key=lambda file: (-scores[file], file))[:limit]


def _recall_gates(positives, hit_flags):
    groups = {}
    for row, hit in zip(positives, hit_flags):
        for key in (f"language:{row['language']}", f"modality:{row['modality']}"):
            group = groups.setdefault(key, {"queries": 0, "hits": 0})
            group["queries"] += 1
            group["hits"] += int(hit)
    for group in groups.values():
        group["recallAt10"] = group["hits"] / group["queries"]
        group["passed"] = group["hits"] * 5 >= group["queries"] * 4
    hits = sum(hit_flags)
    return {"queries": len(positives), "hits": hits,
            "recallAt10": hits / len(positives) if positives else None,
            "groups": dict(sorted(groups.items())),
            "passed": bool(positives) and hits * 10 >= len(positives) * 9
            and all(group["passed"] for group in groups.values())}


def evaluate(rows, thresholds):
    positives = [row for row, positive in rows if positive]
    negatives = [row for row, positive in rows if not positive]
    positive_results, negative_results = [], []
    flags = []
    for row in positives:
        top = rank_query(row, thresholds)
        hit = row["expectedFile"] in top
        flags.append(hit)
        positive_results.append({"query": row["query"], "topic": row["topic"],
                                 "language": row["language"], "modality": row["modality"],
                                 "expectedFile": row["expectedFile"], "top10": top,
                                 "rank": top.index(row["expectedFile"]) + 1 if hit else None})
    for row in negatives:
        top = rank_query(row, thresholds)
        negative_results.append({"query": row["query"], "topic": row["topic"],
                                 "language": row["language"], "modality": row["modality"],
                                 "top10": top, "returned": bool(top)})
    recall = _recall_gates(positives, flags)
    returned = sum(result["returned"] for result in negative_results)
    no_answer = {"queries": len(negatives), "returned": returned,
                 "falseReturnRate": returned / len(negatives) if negatives else None,
                 "passed": bool(negatives) and returned * 10 <= len(negatives)}
    return {"passed": recall["passed"] and no_answer["passed"], "positive": recall,
            "noAnswer": no_answer, "queries": positive_results,
            "noAnswerQueries": negative_results}


def _threshold_search(rows, max_states):
    """Find a feasible grid point, pruning only provably impossible boxes.

    Thresholds immediately above each observed cosine enumerate every possible
    acceptance set under >=. Bounds use potential expected-file membership and
    unavoidable negative-query returns, not an unsafe monotonic recall claim.
    Ranking may improve or worsen as a threshold rises. Holdout rows never enter
    this function. Finding a point proves feasibility, exhausting boxes proves
    infeasibility, reaching the budget proves neither.
    """
    positives = [row for row, positive in rows if positive]
    negatives = [row for row, positive in rows if not positive]
    values = {modality: sorted({float(c["cosine"]) for row, _ in rows
                               for c in row["rawCandidates"]
                               if c["policyModality"] == modality}) for modality in MODALITIES}
    missing = [modality for modality in MODALITIES if not values[modality]]
    stats = {"algorithm": "exact-filter-set-branch-and-bound", "visitedBoxes": 0,
             "evaluatedPolicies": 0, "prunedBoxes": 0, "maxStates": max_states,
             "gridSizes": {}, "unobservedModalities": missing}
    if not positives or not negatives or missing:
        stats["status"] = "insufficient-calibration-evidence"
        return None, stats
    grid = {m: [values[m][0]] + [math.nextafter(value, math.inf) for value in values[m]]
            for m in MODALITIES}
    stats["gridSizes"] = {m: len(grid[m]) for m in MODALITIES}
    negative_maxima = []
    expected_maxima = []
    for row in negatives:
        negative_maxima.append((bool(row["keywordCandidates"]),
                                {m: max((c["cosine"] for c in row["rawCandidates"]
                                         if c["policyModality"] == m), default=-math.inf)
                                 for m in MODALITIES}))
    for row in positives:
        expected_maxima.append((any(c["file"] == row["expectedFile"]
                                    for c in row["keywordCandidates"]),
                                {m: max((c["cosine"] for c in row["rawCandidates"]
                                         if c["policyModality"] == m
                                         and c["file"] == row["expectedFile"]), default=-math.inf)
                                 for m in MODALITIES}))
    budget = len(negatives) // 10
    fixed_negatives = sum(keyword for keyword, _ in negative_maxima)
    if fixed_negatives > budget:
        stats["status"] = "infeasible"
        stats["reason"] = "keyword-only negative returns exceed the allowed query rate"
        return None, stats
    # Necessary per-axis negative constraint eliminates most irrelevant grid
    # cuts before the four-dimensional search. Unions are checked per box.
    low = []
    for m in MODALITIES:
        maxima = sorted((scores[m] for keyword, scores in negative_maxima
                         if not keyword), reverse=True)
        allowance = budget - fixed_negatives
        minimum = math.nextafter(maxima[allowance], math.inf) if allowance < len(maxima) else -math.inf
        low.append(bisect.bisect_left(grid[m], minimum))
    high = [len(grid[m]) - 1 for m in MODALITIES]
    pending = [(tuple(low), tuple(high))]
    evaluated = set()
    while pending:
        if stats["visitedBoxes"] >= max_states:
            stats["status"] = "search-budget-exhausted"
            stats["remainingBoxes"] = len(pending)
            return None, stats
        lower, upper = pending.pop()
        stats["visitedBoxes"] += 1
        thresholds_low = {m: grid[m][lower[i]] for i, m in enumerate(MODALITIES)}
        thresholds_high = {m: grid[m][upper[i]] for i, m in enumerate(MODALITIES)}
        possible_hits = [keyword or any(scores[m] >= thresholds_low[m] for m in MODALITIES)
                         for keyword, scores in expected_maxima]
        unavoidable = sum(keyword or any(scores[m] >= thresholds_high[m] for m in MODALITIES)
                          for keyword, scores in negative_maxima)
        if not _recall_gates(positives, possible_hits)["passed"] or unavoidable > budget:
            stats["prunedBoxes"] += 1
            continue
        if lower not in evaluated:
            evaluated.add(lower)
            stats["evaluatedPolicies"] += 1
            negative_returns = sum(keyword or any(scores[m] >= thresholds_low[m] for m in MODALITIES)
                                   for keyword, scores in negative_maxima)
            if negative_returns <= budget and _recall_gates(positives, [
                    row["expectedFile"] in rank_query(row, thresholds_low) for row in positives])["passed"]:
                stats["status"] = "feasible"
                return thresholds_low, stats
        if lower == upper:
            continue
        axis = max(range(len(MODALITIES)), key=lambda i: (upper[i] - lower[i], -i))
        middle = (lower[axis] + upper[axis]) // 2
        left_upper, right_lower = list(upper), list(lower)
        left_upper[axis], right_lower[axis] = middle, middle + 1
        pending.append((tuple(right_lower), upper))
        pending.append((lower, tuple(left_upper)))
    stats["status"] = "infeasible"
    return None, stats


def _representative_thresholds(rows, search_thresholds):
    """Move grid cuts inside their calibration-equivalent score intervals.

    The search's nextafter cuts specify accepted sets exactly, but leave almost
    no numerical margin above a rejected observation. An interior midpoint
    preserves those sets and gives equal margin to the nearest rejected and
    retained calibration scores. No holdout observation selects the midpoint.
    Adjacent doubles may have no representable interior value, in which case
    the accepted endpoint is the only valid threshold and its margin is zero.
    """
    thresholds, intervals = {}, {}
    for modality in MODALITIES:
        values = sorted({float(c["cosine"]) for row, _ in rows for c in row["rawCandidates"]
                         if c["policyModality"] == modality})
        cut = bisect.bisect_left(values, search_thresholds[modality])
        rejected = values[cut - 1] if cut else None
        retained = values[cut] if cut < len(values) else None
        if retained is None:
            threshold = REJECT_ALL
        elif rejected is None:
            threshold = retained
        else:
            threshold = rejected + (retained - rejected) / 2.0
            if threshold <= rejected:
                threshold = math.nextafter(rejected, math.inf)
            threshold = min(threshold, retained)
        if any((value >= search_thresholds[modality]) != (value >= threshold) for value in values):
            raise ValueError("internal error: midpoint changed the calibration acceptance set")
        thresholds[modality] = threshold
        intervals[modality] = {
            "rejectedMaximum": rejected, "acceptedMinimum": retained,
            "rejectedMargin": threshold - rejected if rejected is not None else None,
            "acceptedMargin": retained - threshold if retained is not None else None}
    return thresholds, {"method": THRESHOLD_PLACEMENT, "calibrationAcceptanceEquivalent": True,
                        "searchThresholds": search_thresholds, "intervals": intervals}


def calibrate_report(report, *, report_sha256=None, max_states=DEFAULT_MAX_STATES):
    """Fit a policy. Production callers provide the hash of raw report bytes.

    The canonical-object fallback exists for in-memory unit fixtures only. The
    CLI always hashes the original bytes, including whitespace and any BOM.
    """
    if type(max_states) is not int or max_states < 1:
        raise ValueError("max_states must be a positive integer")
    rows = validate_report(report)
    calibration = [(row, positive) for row, positive in rows if row["partition"] == "calibration"]
    holdout = [(row, positive) for row, positive in rows if row["partition"] == "holdout"]
    calibration_identity = {"version": POLICY_VERSION, "thresholdPlacement": THRESHOLD_PLACEMENT,
                            "modelFingerprint": report["modelFingerprint"],
                            "corpusIdentity": report["corpusIdentity"],
                            "rows": [{"positive": positive, **row} for row, positive in calibration]}
    calibration_hash = canonical_hash(calibration_identity)
    thresholds, search = _threshold_search(calibration, max_states)
    calibrated = thresholds is not None
    placement = {"method": THRESHOLD_PLACEMENT, "calibrationAcceptanceEquivalent": None,
                 "searchThresholds": None, "intervals": {}}
    if calibrated:
        thresholds, placement = _representative_thresholds(calibration, thresholds)
    else:
        thresholds = {m: REJECT_ALL for m in MODALITIES}
    calibration_result = evaluate(calibration, thresholds)
    if calibrated and not calibration_result["passed"]:
        raise ValueError("internal error: midpoint policy failed calibration revalidation")
    # Holdout is first consulted only after threshold selection is complete.
    holdout_result = evaluate(holdout, thresholds)
    calibration_groups = set(calibration_result["positive"]["groups"])
    holdout_groups = set(holdout_result["positive"]["groups"])
    coverage = {"missingHoldoutGroups": sorted(calibration_groups - holdout_groups),
                "additionalHoldoutGroups": sorted(holdout_groups - calibration_groups)}
    coverage["passed"] = not coverage["missingHoldoutGroups"]
    accepted = (calibrated and calibration_result["passed"] and holdout_result["passed"]
                and coverage["passed"])
    return {"version": f"{POLICY_VERSION}-{calibration_hash[:12]}",
            "modelFingerprint": report["modelFingerprint"],
            "corpusIdentity": report["corpusIdentity"],
            "corpusSha256": canonical_hash(report["corpusIdentity"]),
            "reportSha256": report_sha256 or canonical_hash(report),
            "calibrationSha256": calibration_hash,
            "thresholds": thresholds, "thresholdSelection": placement,
            "calibrated": calibrated, "accepted": accepted,
            "status": "accepted" if accepted else (
                "holdout-failed" if calibrated else search["status"]),
            "criteria": {"rankLimit": 10, "rrfConstant": 60,
                         "semanticFileCandidateLimit": 256, "keywordFileCandidateLimit": 256,
                         "overallRecallAt10Minimum": 0.9, "groupRecallAt10Minimum": 0.8,
                         "noAnswerFalseReturnRateMaximum": 0.1,
                         "groupAxes": ["language", "modality"], "comparison": ">="},
            "tiePolicy": "cosine descending, segmentId ascending, file ascending, RRF then file ascending",
            "missingSegmentIds": sum("segmentId" not in candidate for row, _ in rows
                                     for candidate in row["rawCandidates"]),
            "search": search, "coverage": coverage,
            "calibration": calibration_result, "holdout": holdout_result}


def serialize_policy(policy):
    """Exact UTF-8 JSON text written to --output and bound by the C++ header."""
    return json.dumps(policy, ensure_ascii=False, indent=2, allow_nan=False) + "\n"


def cpp_header(policy):
    if not policy.get("accepted") or not policy.get("calibrated"):
        raise ValueError("C++ policy headers require accepted calibration and independent holdout evidence")
    lines = ["// Generated by tests/semantic/calibrate_relevance.py from accepted evidence.",
             "#pragma once", "", "namespace qcopilots_search::relevance_data {",
             "inline constexpr bool calibrated = true;"]
    for field in ("version", "modelFingerprint", "corpusSha256"):
        lines.append(f"inline constexpr const char *{field} = {json.dumps(policy[field])};")
    report_hash = hashlib.sha256(serialize_policy(policy).encode("utf-8")).hexdigest()
    lines.append(f"inline constexpr const char *rawExportSha256 = {json.dumps(policy['reportSha256'])};")
    lines.append(f"inline constexpr const char *calibrationReportSha256 = {json.dumps(report_hash)};")
    for modality in MODALITIES:
        lines.append(f"inline constexpr double {modality}Minimum = {policy['thresholds'][modality]!r};")
    lines.extend(["} // namespace qcopilots_search::relevance_data", ""])
    return "\n".join(lines)


def _write_atomic(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                         prefix=path.name + ".", suffix=".tmp",
                                         dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path, help="Frozen semantic calibration-export JSON report")
    parser.add_argument("--output", type=Path, required=True, help="Policy and evaluation JSON output")
    parser.add_argument("--header", type=Path, help="Write C++ policy header only if accepted")
    parser.add_argument("--max-states", type=int, default=DEFAULT_MAX_STATES)
    args = parser.parse_args(argv)
    try:
        source_root = Path(__file__).resolve().parents[2]
        if args.output.resolve().is_relative_to(source_root):
            raise ValueError("generated policy reports must be outside the source repository")
        paths = [args.report.resolve(), args.output.resolve()]
        if args.header:
            paths.append(args.header.resolve())
        if len(set(paths)) != len(paths):
            raise ValueError("report, policy output and header paths must be distinct")
        raw = args.report.read_bytes()
        report = json.loads(raw.decode("utf-8-sig"))
        policy = calibrate_report(report, report_sha256=hashlib.sha256(raw).hexdigest(),
                                  max_states=args.max_states)
        _write_atomic(args.output, serialize_policy(policy))
        if args.header and policy["accepted"]:
            _write_atomic(args.header, cpp_header(policy))
        print(json.dumps({"status": policy["status"], "calibrated": policy["calibrated"],
                          "accepted": policy["accepted"], "output": str(args.output),
                          "headerWritten": bool(args.header and policy["accepted"])}, ensure_ascii=False))
        return 0 if policy["accepted"] else 1
    except (ValueError, OSError) as error:
        print(f"relevance calibration failed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
