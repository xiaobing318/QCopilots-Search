#!/usr/bin/env python3
"""Contract tests for the complete CTest inventory and evidence validation."""
import copy
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location("qcs_policy", Path(__file__).resolve().parents[2] / "tools/qcs-test-policy.py")
policy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(policy)


def inventory():
    return {"kind": "ctestInfo", "version": {"major": 1, "minor": 0}, "tests": [
        {"name": "qcs-tests-new-python-tool", "properties": [{"name": "LABELS", "value": ["unit", "python-unittest"]}]},
        {"name": "qcs-tests-any-native-contract", "properties": [{"name": "LABELS", "value": ["slow", "qttest"]}]},
        {"name": "qcs-tests-new-model", "properties": [{"name": "LABELS", "value": ["slow", "real-model", "program"]}]},
    ]}


class PolicyTests(unittest.TestCase):
    def test_inventory_preserves_every_registered_entry_and_metadata(self):
        source = inventory()
        result = policy.select_inventory(source)
        self.assertEqual(result["tests"], source["tests"])
        self.assertEqual(result["version"], source["version"])
        self.assertEqual(len(source["tests"]), 3)

    def test_all_labels_are_included(self):
        self.assertEqual(policy.select_inventory(inventory()), inventory())

    def test_report_includes_selection_and_evidence_type_for_new_tests(self):
        report = policy.describe_inventory(inventory())
        self.assertEqual(report["selectedPrograms"], report["registeredPrograms"])
        self.assertEqual(report["excludedPrograms"], [])
        self.assertEqual(report["evidenceKinds"]["qcs-tests-new-model"], "program")

    def test_missing_or_ambiguous_evidence_labels_fail(self):
        for labels in ([], ["unit"], ["qttest", "python-unittest"], "qttest", [""]):
            with self.subTest(labels=labels):
                source = inventory()
                source["tests"][0]["properties"][0]["value"] = labels
                with self.assertRaises(ValueError):
                    policy.select_inventory(source)

    def test_disabled_tests_fail(self):
        for disabled in (True, "TRUE", "ON", 1):
            source = inventory()
            source["tests"][1]["properties"].append({"name": "DISABLED", "value": disabled})
            with self.subTest(disabled=disabled), self.assertRaisesRegex(ValueError, "disabled"):
                policy.select_inventory(source)

    def test_real_model_does_not_require_a_scheduling_label(self):
        source = inventory()
        source["tests"][2]["properties"][0]["value"].remove("slow")
        self.assertEqual(policy.select_inventory(source), source)

    def test_empty_duplicate_unowned_or_malformed_inventory_fails(self):
        source = inventory()
        duplicate = copy.deepcopy(source)
        duplicate["tests"].append(duplicate["tests"][0])
        unowned = copy.deepcopy(source)
        unowned["tests"][0]["name"] = "third-party-test"
        for value in ({}, [], {"kind": "ctestInfo", "tests": []}, duplicate, unowned):
            with self.subTest(value=value), self.assertRaises(ValueError):
                policy.select_inventory(value)

if __name__ == "__main__":
    unittest.main(verbosity=2)
