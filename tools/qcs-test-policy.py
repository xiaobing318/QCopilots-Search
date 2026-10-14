#!/usr/bin/env python3
"""Select and classify owned tests from the actual CTest registration inventory."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

EVIDENCE_KINDS = frozenset(("qttest", "python-unittest", "program"))


def labels(entry: dict) -> set[str]:
    properties = entry.get("properties", [])
    if not isinstance(properties, list):
        raise ValueError("CTest properties must be a list")
    matches = [item.get("value") for item in properties if item.get("name") == "LABELS"]
    if len(matches) != 1 or not isinstance(matches[0], list):
        raise ValueError(f"CTest test {entry.get('name')!r} needs one LABELS list")
    if any(not isinstance(value, str) or not value for value in matches[0]):
        raise ValueError("CTest labels must be nonempty strings")
    return set(matches[0])


def evidence_kind(entry: dict) -> str:
    kinds = labels(entry) & EVIDENCE_KINDS
    if len(kinds) != 1:
        raise ValueError(f"CTest test {entry.get('name')!r} needs exactly one evidence kind label")
    return next(iter(kinds))


def validate_inventory(inventory: dict) -> list[dict]:
    if not isinstance(inventory, dict) or inventory.get("kind") != "ctestInfo":
        raise ValueError("Expected ctest --show-only=json-v1 output")
    entries = inventory.get("tests")
    if not isinstance(entries, list) or not entries:
        raise ValueError("Registered CTest inventory must contain tests")
    names = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("CTest test must be an object")
        name = entry.get("name")
        if not isinstance(name, str) or not name.startswith("qcs-tests-"):
            raise ValueError("Owned CTest names must use the qcs-tests- prefix")
        names.append(name)
        evidence_kind(entry)
        test_labels = labels(entry)
        if "real-model" in test_labels and "slow" not in test_labels:
            raise ValueError(f"Real-model test must be marked slow: {name}")
        for prop in entry.get("properties", []):
            if prop.get("name") == "DISABLED" and prop.get("value") not in (False, "FALSE", "OFF", "0", 0):
                raise ValueError(f"Required registered CTest is disabled: {name}")
    if len(set(names)) != len(names):
        raise ValueError("Registered CTest inventory contains duplicate names")
    return entries


def select_inventory(inventory: dict, profile: str = "full") -> dict:
    if profile not in ("fast", "full"):
        raise ValueError(f"Unknown test profile: {profile!r}")
    entries = validate_inventory(inventory)
    selected = [entry for entry in entries if profile == "full" or "slow" not in labels(entry)]
    if not selected:
        raise ValueError(f"The {profile} test profile selects no registered tests")
    return {**inventory, "tests": selected}


def describe_inventory(inventory: dict, profile: str = "full") -> dict:
    selected = select_inventory(inventory, profile)
    registered_names = sorted(entry["name"] for entry in inventory["tests"])
    selected_names = sorted(entry["name"] for entry in selected["tests"])
    return {"schemaVersion": 1, "testProfile": profile,
            "registeredPrograms": registered_names, "selectedPrograms": selected_names,
            "excludedPrograms": sorted(set(registered_names) - set(selected_names)),
            "evidenceKinds": {entry["name"]: evidence_kind(entry) for entry in inventory["tests"]}}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--test-profile", choices=("fast", "full"), default="fast")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--report", type=Path)
    options = parser.parse_args()
    inventory = json.loads(options.inventory.read_text(encoding="utf-8-sig"))
    selected = select_inventory(inventory, options.test_profile)
    report = describe_inventory(inventory, options.test_profile)
    for path, document in ((options.output, selected), (options.report, report)):
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if options.report is None:
        print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
