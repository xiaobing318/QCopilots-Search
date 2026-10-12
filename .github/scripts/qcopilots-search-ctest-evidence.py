#!/usr/bin/env python3
"""Inspect CTest JUnit and QtTest case output without treating QSKIP as PASS."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
import xml.etree.ElementTree as ET

NON_QT_TESTS = {
    "qcopilots-search-tests-semantic-manifest",
    "qcopilots-search-tests-semantic",
    "qcopilots-search-tests-workflow",
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


def inspect_junit(path: Path, operating_system: str, inventory: Path | None = None) -> dict:
    document = {"schemaVersion": 1, "platform": operating_system, "source": str(path),
                "releaseComplete": False, "ctestPrograms": [], "qtTestSkips": [],
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
        is_qt_test = name not in NON_QT_TESTS or "Start testing of " in output
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
