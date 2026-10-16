#!/usr/bin/env python3
"""Inspect CTest JUnit and QtTest case output without treating QSKIP as PASS."""
from __future__ import annotations

import argparse
import ast
import importlib.util
import json
from pathlib import Path
import re
import sys
import xml.etree.ElementTree as ET

_policy_spec = importlib.util.spec_from_file_location(
    "qcs_test_policy", Path(__file__).resolve().parents[2] / "tools/qcs-test-policy.py")
policy = importlib.util.module_from_spec(_policy_spec)
_policy_spec.loader.exec_module(policy)

TOTALS = re.compile(r"^Totals:\s+(\d+) passed,\s+(\d+) failed,\s+(\d+) skipped,\s+(\d+) blacklisted", re.MULTILINE)
SKIP = re.compile(r"^SKIP\s*:\s*(\S+)\s+(.*)$", re.MULTILINE)
TRUNCATED = re.compile(r"\[This part of the test output was removed since it exceeds the threshold of \d+ bytes\.\]")
START = re.compile(r"^\*+ Start testing of (\S+) \*+\s*$", re.MULTILINE)
FINISH = re.compile(r"^\*+ Finished testing of (\S+) \*+\s*$", re.MULTILINE)
CASE = re.compile(r"^(PASS|FAIL!|XPASS|SKIP|BPASS|BFAIL|BXPASS|BXFAIL)[ \t]*:[ \t]*(\S+)", re.MULTILINE)
PLATFORM_EXCLUSIONS = {
    ("Windows", "qcs-tests-index_repository_regression",
     "IndexRepositoryRegressionTest::rootFilterRespectsCaseSensitivePlatformKeys()"):
        "Non-Windows path keys are case-sensitive. Windows intentionally folds path keys.",
}
SIGNING_TEST = "qcs-tests-macos-signing"
WORKFLOW_TEST = "qcs-tests-workflow"
WORKFLOW_NATIVE_TEST = "qcs-tests-workflow-native"
WORKFLOW_NATIVE_CASE = "__main__.WorkflowTests.test_nsis_compiles_recursive_payload_beyond_max_path"
SIGNING_WINDOWS_SKIP_REASON = "Windows lacks symbolic-link creation privilege (WinError 1314). Native CI must run this case."
SIGNING_WINDOWS_SKIPS = {
    "__main__.SigningTests." + name for name in (
        "test_directory_symlink_is_not_recursed", "test_external_directory_symlink_is_rejected",
        "test_external_file_symlink_is_rejected", "test_internal_library_symlink_is_not_signed_twice")
}
WORKFLOW_NSIS_SKIP_REASON = "NSIS is native to Windows"
WORKFLOW_NSIS_SKIPS = {
    "__main__.WorkflowTests." + name for name in (
        "test_nsis_compiles_recursive_payload_beyond_max_path",
        "test_installer_guard_rejects_existing_start_menu_before_execution")
}
WORKFLOW_POSIX_LINK_SKIP_REASON = "Windows symbolic-link creation privilege unavailable (WinError 1314), native macOS/Linux execution required"
WORKFLOW_POSIX_LINK_CASES = {
    "__main__.WorkflowTests." + name for name in (
        "test_clean_unlinks_internal_framework_and_versioned_library_links",
        "test_clean_rejects_external_symlink_without_deleting_any_target",
        "test_clean_rejects_linked_top_level_target")
}
WORKFLOW_JUNCTION_SKIP_REASON = "Windows junction semantics require Windows"
WORKFLOW_JUNCTION_CASE = "__main__.WorkflowTests.test_clean_rejects_windows_junction_inside_generated_tree"
RUNNER_TEST = "qcs-tests-runner"
RUNNER_WINDOWS_HANDLE_CASE = "__main__.TestRunner.test_real_windows_exclusive_handle_still_fails_cleanup"
RUNNER_WINDOWS_HANDLE_SKIP_REASON = "Windows exclusive file handles require Windows"
WINDOWS_NATIVE_PROGRAM_CASES = {
    "qcs-tests-gpu-configuration": {
        "__main__.AbiTests.test_real_windows_system_image": "Uses an actual host PE library",
    },
    "qcs-tests-windows-package": {
        "__main__.NativeInstallerTests." + name: "The native NSIS installer runs on Windows"
        for name in (
            "test_install_uninstall_preserves_unowned_file",
            "test_installer_and_uninstaller_embed_product_icon",
            "test_standard_installer_and_uninstaller_embed_product_icon",
            "test_missing_payload_fails_before_installing",
            "test_overlapping_payload_destination_is_rejected",
            "test_same_length_corruption_fails_before_installing")
    },
}
UNITTEST_CASE = re.compile(r"^(test\S+) \((\S+)\) \.\.\. (ok|FAIL|ERROR|expected failure|unexpected success|skipped [^\r\n]*)\r?$", re.MULTILINE)
UNITTEST_TOTAL = re.compile(r"^Ran (\d+) tests? in [\d.]+s\r?$", re.MULTILINE)
UNITTEST_RESULT = re.compile(r"^(OK(?: \(skipped=\d+\))?|FAILED(?: \([^\r\n]*\))?)\r?$", re.MULTILINE)
REGISTRATION_PREFIX = "QCS_CASE_REGISTRATION"
SIGNING_REGISTRATION_REASON = "POSIX macOS bundle symbolic-link contract is covered on macOS/Linux. Portable resolution contracts run on Windows."
WORKFLOW_REGISTRATION_REASON = "POSIX framework and versioned-library symlink contract is covered on macOS/Linux. Windows junction boundaries run separately."


def unittest_case_identifier(method, identifier):
    """Normalize Python 3.10's class-only and 3.11+'s full verbose IDs."""
    if not re.fullmatch(r"test\w*", method) or not re.fullmatch(r"(?:\w+\.)+\w+", identifier):
        raise ValueError("Python unittest method or case identity is malformed")
    tail = identifier.rsplit(".", 1)[-1]
    if tail == method:
        return identifier
    if tail.startswith("test"):
        # An explicit but different test method must not be reinterpreted as
        # a Python 3.10 class name and accepted under a synthetic identity.
        raise ValueError("Python unittest method and case identity disagree")
    return identifier + "." + method


def inspect_unittest_registration(output, records, operating_system, program):
    """Optional source-emitted suite inventory, distinct from executed passes."""
    lines = [line for line in output.splitlines() if line.startswith(REGISTRATION_PREFIX)]
    if not lines:
        return None, []
    errors = []
    if len(lines) != 1:
        return None, ["Python unittest has duplicate case registration records"]
    try:
        document = json.loads(lines[0][len(REGISTRATION_PREFIX):].strip())
        if (not isinstance(document, dict) or set(document) != {"suite", "selected", "notApplicable"}
                or not isinstance(document["suite"], str) or not document["suite"].strip()):
            raise ValueError("Python unittest case registration schema is invalid")
        selected, excluded = document["selected"], document["notApplicable"]
        if (not isinstance(selected, list) or not selected
                or any(not isinstance(name, str) or not re.fullmatch(r"(?:\w+\.)+test\w*", name) for name in selected)
                or len(set(selected)) != len(selected)):
            raise ValueError("Python unittest selected case registration is invalid or duplicated")
        if selected != [record["case"] for record in records]:
            errors.append("Python unittest selected case registration differs from the observed ordered case records")
        if not isinstance(excluded, list):
            raise ValueError("Python unittest notApplicable registration must be a list")
        excluded_names = []
        for item in excluded:
            if (not isinstance(item, dict) or set(item) != {"case", "reason"}
                    or not isinstance(item["case"], str) or not isinstance(item["reason"], str)
                    or not item["reason"].strip()):
                raise ValueError("Python unittest notApplicable case registration is invalid")
            name = item["case"]
            excluded_names.append(name)
            accepted = operating_system == "Windows" and (
                (program == SIGNING_TEST and name in SIGNING_WINDOWS_SKIPS and item["reason"] == SIGNING_REGISTRATION_REASON)
                or (program in (WORKFLOW_TEST, WORKFLOW_NATIVE_TEST) and name in WORKFLOW_POSIX_LINK_CASES
                    and item["reason"] == WORKFLOW_REGISTRATION_REASON))
            if not accepted:
                errors.append("Python unittest registration excludes an applicable or unknown case: " + name)
        if len(set(excluded_names)) != len(excluded_names) or set(selected) & set(excluded_names):
            errors.append("Python unittest registered exclusions are duplicated or overlap selected cases")
        return document, errors
    except (ValueError, KeyError, TypeError) as error:
        return None, ["Malformed or truncated Python unittest case registration: " + str(error)]


def inspect_signing_unittest(output, operating_system, program=SIGNING_TEST):
    errors, records, skips = [], [], []
    counts = {"run": 0, "passed": 0, "failed": 0, "skipped": 0, "notApplicable": 0}
    for method, identifier, status in UNITTEST_CASE.findall(output):
        try:
            identifier = unittest_case_identifier(method, identifier)
        except ValueError as error:
            errors.append(str(error))
        record = {"case": identifier, "status": status}
        records.append(record)
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
                    and identifier in WORKFLOW_NSIS_SKIPS and reason == WORKFLOW_NSIS_SKIP_REASON)
                or (program == WORKFLOW_TEST and operating_system == "Windows"
                    and identifier in WORKFLOW_POSIX_LINK_CASES and reason == WORKFLOW_POSIX_LINK_SKIP_REASON)
                or (program == WORKFLOW_TEST and operating_system in ("Linux", "Darwin")
                    and identifier == WORKFLOW_JUNCTION_CASE and reason == WORKFLOW_JUNCTION_SKIP_REASON)
                or (program == RUNNER_TEST and operating_system in ("Linux", "Darwin")
                    and identifier == RUNNER_WINDOWS_HANDLE_CASE and reason == RUNNER_WINDOWS_HANDLE_SKIP_REASON)
                or (operating_system in ("Linux", "Darwin")
                    and WINDOWS_NATIVE_PROGRAM_CASES.get(program, {}).get(identifier) == reason))
            entry = {"program": program, "case": identifier, "reason": reason,
                     "classification": "notApplicable" if applicable else "requiredNotRun"}
            if applicable and program == WORKFLOW_TEST and identifier in WORKFLOW_POSIX_LINK_CASES:
                entry["requiredNativePlatforms"] = ["Linux", "Darwin"]
            if applicable and program == RUNNER_TEST and identifier == RUNNER_WINDOWS_HANDLE_CASE:
                entry["requiredNativePlatforms"] = ["Windows"]
            if applicable and identifier in WINDOWS_NATIVE_PROGRAM_CASES.get(program, {}):
                entry["requiredNativePlatforms"] = ["Windows"]
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
    _, registration_errors = inspect_unittest_registration(output, records, operating_system, program)
    errors.extend(registration_errors)
    if len(set(identifiers)) != len(identifiers):
        errors.append("Python unittest contains duplicate case records")
    expected_conclusion = f"OK (skipped={counts['skipped']})" if counts["skipped"] else "OK"
    if conclusions != [expected_conclusion] or counts["failed"]:
        errors.append("Python unittest conclusion or skipped total disagrees with successful case records")
    if counts["passed"] + counts["failed"] == 0:
        errors.append("Python unittest report contains no executed cases")
    if program == WORKFLOW_TEST:
        required = (WORKFLOW_POSIX_LINK_CASES if operating_system in ("Linux", "Darwin")
                    else {WORKFLOW_JUNCTION_CASE} if operating_system == "Windows" else set())
        executed = {record["case"] for record in records if record["status"] == "ok"}
        for identifier in sorted(required - executed):
            errors.append(f"Required native cleanup case did not pass: {identifier}")
    if program == WORKFLOW_NATIVE_TEST:
        if operating_system != "Windows" or not any(record["case"] == WORKFLOW_NATIVE_CASE and record["status"] == "ok" for record in records):
            errors.append("The Windows NSIS compiler/install contract did not pass")
    if program == RUNNER_TEST and operating_system == "Windows":
        if not any(record["case"] == RUNNER_WINDOWS_HANDLE_CASE and record["status"] == "ok" for record in records):
            errors.append(f"Required native cleanup case did not pass: {RUNNER_WINDOWS_HANDLE_CASE}")
    native_cases = set(WINDOWS_NATIVE_PROGRAM_CASES.get(program, {}))
    for identifier in sorted(native_cases - set(identifiers)):
        errors.append(f"Required Windows case is missing: {identifier}")
    if operating_system == "Windows":
        executed = {record["case"] for record in records if record["status"] == "ok"}
        for identifier in sorted(native_cases - executed):
            errors.append(f"Required Windows case did not pass: {identifier}")
    return counts, records, skips, errors


def inspect_junit(path: Path, operating_system: str, inventory: Path | None = None) -> dict:
    document = {"schemaVersion": 1, "platform": operating_system, "source": str(path),
                "testsComplete": False,
                "ctestPrograms": [], "qtTestSkips": [], "pythonTestSkips": [], "pythonRegisteredNotApplicable": [],
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
    kinds = {}
    if inventory is None:
        document["errors"].append("The registered CTest inventory is missing")
    else:
        try:
            registered = json.loads(inventory.read_text(encoding="utf-8"))
            selected = policy.select_inventory(registered)
            selection = policy.describe_inventory(registered)
            document["registeredPrograms"] = selection["registeredPrograms"]
            kinds = selection["evidenceKinds"]
            expected = [entry["name"] for entry in selected["tests"]]
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
        # CTest may exit zero for DISABLED tests and emit only status="disabled",
        # without a <skipped> child. Such a program did not execute.
        if case.get("status") not in (None, "run"):
            errors.append(f"CTest program did not run: status={case.get('status')!r}")
        failures = sum(len(list(case.iter(tag))) for tag in ("failure", "error", "skipped"))
        if failures:
            errors.append("CTest program contains failure, error or skipped evidence")
        output = "\n".join(element.text or "" for tag in ("system-out", "system-err")
                           for element in case.iter(tag))
        if TRUNCATED.search(output):
            errors.append("CTest truncated captured output, so individual case evidence is incomplete")
        totals = [tuple(map(int, match)) for match in TOTALS.findall(output)]
        skips = SKIP.findall(output)
        kind = kinds.get(name)
        if kind is None:
            errors.append("CTest program has no verified registered evidence kind")
        is_qt_test = kind == "qttest"
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
        elif kind == "python-unittest":
            counts, records, python_skips, python_errors = inspect_signing_unittest(output, operating_system, name)
            summary.update({"pythonTest": True, "pythonTestTotals": counts, "cases": records})
            registration, registration_errors = inspect_unittest_registration(output, records, operating_system, name)
            if registration is not None:
                summary["caseRegistration"] = registration
            if registration is not None and not registration_errors:
                document["pythonRegisteredNotApplicable"].extend(
                    {"program": name, **item, "classification": "notApplicable", "stage": "registration"}
                    for item in registration["notApplicable"])
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
    document["testsComplete"] = not document["errors"]
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
    failed = arguments.require_complete and not result["testsComplete"]
    return 1 if failed else 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
