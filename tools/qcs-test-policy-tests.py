#!/usr/bin/env python3
"""Contract tests for CTest inventory driven profile and evidence selection."""
import copy
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location("qcs_policy", Path(__file__).with_name("qcs-test-policy.py"))
policy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(policy)


def inventory():
    return {"kind": "ctestInfo", "version": {"major": 1, "minor": 0}, "tests": [
        {"name": "qcs-tests-new-python-tool", "properties": [{"name": "LABELS", "value": ["unit", "python-unittest"]}]},
        {"name": "qcs-tests-any-native-contract", "properties": [{"name": "LABELS", "value": ["slow", "qttest"]}]},
        {"name": "qcs-tests-new-model", "properties": [{"name": "LABELS", "value": ["slow", "real-model", "program"]}]},
    ]}


class PolicyTests(unittest.TestCase):
    def test_acceptance_suites_partition_every_case_without_running_native_work(self):
        script = Path(__file__).parent / "acceptance/qcs-test-runners.py"
        aggregate_spec = importlib.util.spec_from_file_location("qcs_acceptance_policy_tests", script)
        aggregate = importlib.util.module_from_spec(aggregate_spec)
        aggregate_spec.loader.exec_module(aggregate)
        groups = {}
        for name in ("pure", "native", "all"):
            suite, _, _ = aggregate.build_suite(name)
            cases = list(aggregate.iter_cases(suite))
            groups[name] = {case.id() for case in cases}
            self.assertEqual(len(cases), len(groups[name]))
        self.assertFalse(groups["pure"] & groups["native"])
        self.assertEqual(groups["all"], groups["pure"] | groups["native"])
        self.assertEqual(groups["native"], aggregate.native_case_ids())
        self.assertTrue(groups["pure"])

    def test_fast_selects_by_labels_and_preserves_the_registered_entry(self):
        source = inventory()
        result = policy.select_inventory(source, "fast")
        self.assertEqual(result["tests"], [source["tests"][0]])
        self.assertEqual(result["version"], source["version"])
        self.assertEqual(len(source["tests"]), 3)

    def test_full_keeps_every_registered_test(self):
        self.assertEqual(policy.select_inventory(inventory()), inventory())

    def test_report_includes_selection_and_evidence_type_for_new_tests(self):
        report = policy.describe_inventory(inventory(), "fast")
        self.assertEqual(report["selectedPrograms"], ["qcs-tests-new-python-tool"])
        self.assertEqual(len(report["excludedPrograms"]), 2)
        self.assertEqual(report["evidenceKinds"]["qcs-tests-new-model"], "program")

    def test_missing_or_ambiguous_evidence_labels_fail(self):
        for labels in ([], ["unit"], ["qttest", "python-unittest"], "qttest", [""]):
            with self.subTest(labels=labels):
                source = inventory()
                source["tests"][0]["properties"][0]["value"] = labels
                with self.assertRaises(ValueError):
                    policy.select_inventory(source)

    def test_disabled_tests_fail_even_when_excluded_by_fast(self):
        for disabled in (True, "TRUE", "ON", 1):
            source = inventory()
            source["tests"][1]["properties"].append({"name": "DISABLED", "value": disabled})
            with self.subTest(disabled=disabled), self.assertRaisesRegex(ValueError, "disabled"):
                policy.select_inventory(source, "fast")

    def test_real_model_without_slow_is_invalid(self):
        source = inventory()
        source["tests"][2]["properties"][0]["value"].remove("slow")
        with self.assertRaisesRegex(ValueError, "marked slow"):
            policy.select_inventory(source, "fast")

    def test_empty_duplicate_unowned_or_malformed_inventory_fails(self):
        source = inventory()
        duplicate = copy.deepcopy(source)
        duplicate["tests"].append(duplicate["tests"][0])
        unowned = copy.deepcopy(source)
        unowned["tests"][0]["name"] = "third-party-test"
        for value in ({}, [], {"kind": "ctestInfo", "tests": []}, duplicate, unowned):
            with self.subTest(value=value), self.assertRaises(ValueError):
                policy.select_inventory(value)

    def test_empty_selection_fails(self):
        source = inventory()
        source["tests"].pop(0)
        with self.assertRaisesRegex(ValueError, "selects no"):
            policy.select_inventory(source, "fast")

    def test_unknown_profile_is_not_silently_coerced(self):
        for profile in ("FAST", "", None, "quick"):
            with self.subTest(profile=profile), self.assertRaisesRegex(ValueError, "Unknown test profile"):
                policy.select_inventory(inventory(), profile)


if __name__ == "__main__":
    unittest.main(verbosity=2)
