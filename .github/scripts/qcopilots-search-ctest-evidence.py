#!/usr/bin/env python3
"""Inspect CTest JUnit and QtTest case output without treating QSKIP as PASS."""
from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path
import re
import sys
import xml.etree.ElementTree as ET

NON_QT_TESTS = {
    "qcopilots-search-tests-semantic-manifest",
    "qcopilots-search-tests-semantic",
    "qcopilots-search-tests-workflow",
    "qcopilots-search-tests-inference-install-layout",
    "qcopilots-search-tests-macos-signing",
    "qcopilots-search-tests-license-materials",
}
TOTALS = re.compile(r"^Totals:\s+(\d+) passed,\s+(\d+) failed,\s+(\d+) skipped,\s+(\d+) blacklisted", re.MULTILINE)
SKIP = re.compile(r"^SKIP\s*:\s*(\S+)\s+(.*)$", re.MULTILINE)
TRUNCATED = re.compile(r"\[This part of the test output was removed since it exceeds the threshold of \d+ bytes\.\]")
START = re.compile(r"^\*+ Start testing of (\S+) \*+\s*$", re.MULTILINE)
FINISH = re.compile(r"^\*+ Finished testing of (\S+) \*+\s*$", re.MULTILINE)
CASE = re.compile(r"^(PASS|FAIL!|XPASS|SKIP|BPASS|BFAIL|BXPASS|BXFAIL)[ \t]*:[ \t]*(\S+)", re.MULTILINE)
PLATFORM_EXCLUSIONS = {
    ("Windows", "qcopilots-search-tests-index_repository_regression",
     "IndexRepositoryRegressionTest::rootFilterRespectsCaseSensitivePlatformKeys()"):
        "Non-Windows path keys are case-sensitive. Windows intentionally folds path keys.",
}
SIGNING_TEST = "qcopilots-search-tests-macos-signing"
WORKFLOW_TEST = "qcopilots-search-tests-workflow"
PYTHON_CASE_TESTS = {SIGNING_TEST, WORKFLOW_TEST, "qcopilots-search-tests-license-materials"}
SIGNING_WINDOWS_SKIP_REASON = "Windows lacks symbolic-link creation privilege (WinError 1314). Native CI must run this case."
SIGNING_WINDOWS_SKIPS = {
    "__main__.SigningTests." + name for name in (
        "test_directory_symlink_is_not_recursed", "test_external_directory_symlink_is_rejected",
        "test_external_file_symlink_is_rejected", "test_internal_library_symlink_is_not_signed_twice")
}
WORKFLOW_NSIS_SKIP_REASON = "NSIS is native to Windows"
WORKFLOW_NSIS_SKIPS = {
    "__main__.WorkflowTests." + name for name in (
        "test_nsis_compiles_recursive_unicode_payload_beyond_max_path",
        "test_installer_guard_rejects_existing_start_menu_before_execution")
}
UNITTEST_CASE = re.compile(r"^(test\S+) \((\S+)\) \.\.\. (ok|FAIL|ERROR|expected failure|unexpected success|skipped [^\r\n]*)\r?$", re.MULTILINE)
UNITTEST_TOTAL = re.compile(r"^Ran (\d+) tests? in [\d.]+s\r?$", re.MULTILINE)
UNITTEST_RESULT = re.compile(r"^(OK(?: \(skipped=\d+\))?|FAILED(?: \([^\r\n]*\))?)\r?$", re.MULTILINE)


def inspect_signing_unittest(output, operating_system, program=SIGNING_TEST):
    errors, records, skips = [], [], []
    counts = {"run": 0, "passed": 0, "failed": 0, "skipped": 0, "notApplicable": 0}
    for method, identifier, status in UNITTEST_CASE.findall(output):
        record = {"case": identifier, "status": status}
        records.append(record)
        if not identifier.endswith("." + method):
            errors.append("Python unittest method and case identity disagree")
        if status == "ok":
            counts["passed"] += 1
        elif status.startswith("skipped "):
            counts["skipped"] += 1
            try:
                reason = ast.literal_eval(status.removeprefix("skipped "))
                if not isinstance(reason, str):
                    raise ValueError("Skip reason is not text")
            except (ValueError, SyntaxError):
                reason = status.removeprefix("skipped ")
                errors.append("Python unittest skip reason is malformed")
            applicable = (
                (program == SIGNING_TEST and operating_system == "Windows" and identifier in SIGNING_WINDOWS_SKIPS
                 and reason == SIGNING_WINDOWS_SKIP_REASON)
                or (program == WORKFLOW_TEST and operating_system in ("Linux", "Darwin")
                    and identifier in WORKFLOW_NSIS_SKIPS and reason == WORKFLOW_NSIS_SKIP_REASON))
            entry = {"program": program, "case": identifier, "reason": reason,
                     "classification": "notApplicable" if applicable else "requiredNotRun"}
            skips.append(entry)
            record["reason"] = reason
            record["classification"] = entry["classification"]
            if applicable:
                counts["notApplicable"] += 1
            else:
                errors.append(f"Required Python unittest case did not run: {identifier}")
        else:
            counts["failed"] += 1
    totals, conclusions = UNITTEST_TOTAL.findall(output), UNITTEST_RESULT.findall(output)
    if len(totals) != 1 or int(totals[0]) != len(records):
        errors.append("Python unittest total does not match complete individual case records")
    counts["run"] = int(totals[0]) if len(totals) == 1 else len(records)
    identifiers = [record["case"] for record in records]
    if len(set(identifiers)) != len(identifiers):
        errors.append("Python unittest contains duplicate case records")
    expected_conclusion = f"OK (skipped={counts['skipped']})" if counts["skipped"] else "OK"
    if conclusions != [expected_conclusion] or counts["failed"]:
        errors.append("Python unittest conclusion or skipped total disagrees with successful case records")
    if counts["passed"] + counts["failed"] == 0:
        errors.append("Python unittest report contains no executed cases")
    return counts, records, skips, errors


def inspect_junit(path: Path, operating_system: str, inventory: Path | None = None) -> dict:
    document = {"schemaVersion": 1, "platform": operating_system, "source": str(path),
                "releaseComplete": False, "ctestPrograms": [], "qtTestSkips": [], "pythonTestSkips": [],
                "pythonTestTotals": {"run": 0, "passed": 0, "failed": 0, "skipped": 0, "notApplicable": 0},
                "inventoryVerified": False, "expectedPrograms": [],
                "errors": [], "qtTestTotals": {"passed": 0, "failed": 0,
                    "skipped": 0, "blacklisted": 0, "notApplicable": 0}}
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError) as error:
        document["errors"].append(f"Cannot read CTest JUnit: {error}")
        return document
    cases = list(root.iter("testcase"))
    if not cases:
        document["errors"].append("CTest JUnit contains no test programs")
    if inventory is None:
        document["errors"].append("The registered CTest inventory is missing")
    else:
        try:
            registered = json.loads(inventory.read_text(encoding="utf-8"))
            if registered.get("kind") != "ctestInfo" or not isinstance(registered.get("tests"), list):
                raise ValueError("Expected ctest --show-only=json-v1 output")
            expected = [entry["name"] for entry in registered["tests"]]
            if not expected or any(not isinstance(name, str) or not name for name in expected):
                raise ValueError("Registered CTest names must be nonempty strings")
            if len(set(expected)) != len(expected):
                raise ValueError("Registered CTest inventory contains duplicate names")
            document["expectedPrograms"] = sorted(expected)
            actual = [case.get("name", "") for case in cases]
            missing = sorted(set(expected) - set(actual))
            extra = sorted(set(actual) - set(expected))
            duplicate = sorted(name for name in set(actual) if actual.count(name) > 1)
            if missing or extra or duplicate:
                document["errors"].append(f"CTest program set differs from the registered inventory: "
                    f"missing={missing}, extra={extra}, duplicate={duplicate}")
            else:
                document["inventoryVerified"] = True
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
            document["errors"].append(f"Cannot verify registered CTest inventory: {error}")
    for case in cases:
        name = case.get("name", "")
        errors = []
        failures = sum(len(list(case.iter(tag))) for tag in ("failure", "error", "skipped"))
        if failures:
            errors.append("CTest program contains failure, error or skipped evidence")
        output = "\n".join(element.text or "" for tag in ("system-out", "system-err")
                           for element in case.iter(tag))
        if TRUNCATED.search(output):
            errors.append("CTest truncated captured output, so individual case evidence is incomplete")
        totals = [tuple(map(int, match)) for match in TOTALS.findall(output)]
        skips = SKIP.findall(output)
        is_qt_test = name not in PYTHON_CASE_TESTS and (name not in NON_QT_TESTS or "Start testing of " in output)
        summary = {"name": name, "qtTest": is_qt_test, "errors": errors}
        if is_qt_test:
            if not totals:
                errors.append("QtTest case totals are missing from captured output")
            starts, finishes = START.findall(output), FINISH.findall(output)
            if not starts or starts != finishes or len(starts) != len(totals):
                errors.append("QtTest start, totals and finish records are incomplete or inconsistent")
            records = [{"case": identifier, "status": status} for status, identifier in CASE.findall(output)]
            summary["cases"] = records
            counts = dict(zip(("passed", "failed", "skipped", "blacklisted"),
                              [sum(values[index] for values in totals) for index in range(4)]))
            summary["qtTestTotals"] = counts
            for key, value in counts.items():
                document["qtTestTotals"][key] += value
            if counts["skipped"] != len(skips):
                errors.append("QtTest skipped total does not match individual SKIP lines")
            observed = {"passed": sum(item["status"] == "PASS" for item in records),
                        "failed": sum(item["status"] in ("FAIL!", "XPASS") for item in records),
                        "skipped": sum(item["status"] == "SKIP" for item in records),
                        "blacklisted": sum(item["status"].startswith("B") for item in records)}
            if counts != observed:
                errors.append("QtTest totals do not match individual case records")
            if counts["passed"] + counts["failed"] == 0:
                errors.append("QtTest report contains no executed cases")
            if counts["failed"] or counts["blacklisted"]:
                errors.append("QtTest contains failed or blacklisted cases")
            for identifier, reason in skips:
                expected_reason = PLATFORM_EXCLUSIONS.get((operating_system, name, identifier))
                applicable = expected_reason is not None and reason.strip() == expected_reason
                entry = {"program": name, "case": identifier, "reason": reason.strip(),
                         "classification": "notApplicable" if applicable else "requiredNotRun"}
                document["qtTestSkips"].append(entry)
                if applicable:
                    document["qtTestTotals"]["notApplicable"] += 1
                else:
                    errors.append(f"Required QtTest case did not run: {identifier}")
        elif name in PYTHON_CASE_TESTS:
            counts, records, python_skips, python_errors = inspect_signing_unittest(output, operating_system, name)
            summary.update({"pythonTest": True, "pythonTestTotals": counts, "cases": records})
            errors.extend(python_errors)
            document["pythonTestSkips"].extend(python_skips)
            for key, value in counts.items():
                document["pythonTestTotals"][key] += value
            if totals or skips:
                errors.append("Unexpected QtTest output in a Python unittest program")
        elif totals or skips:
            errors.append("Unexpected QtTest output in a non-QtTest program")
        document["ctestPrograms"].append(summary)
        document["errors"].extend(f"{name}: {error}" for error in errors)
    document["releaseComplete"] = not document["errors"]
    return document


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--junit", type=Path, required=True)
    parser.add_argument("--inventory", type=Path, help="Saved ctest --show-only=json-v1 output")
    parser.add_argument("--platform", choices=("Windows", "Linux", "Darwin"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--require-complete", action="store_true")
    arguments = parser.parse_args()
    result = inspect_junit(arguments.junit, arguments.platform, arguments.inventory)
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if arguments.require_complete and not result["releaseComplete"] else 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
