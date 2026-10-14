"""Exercise real unittest entry points with tiny diagnostics fixtures in one OS pipe."""
from __future__ import annotations

import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.dont_write_bytecode = True
DIRECTORY = Path(__file__).resolve().parent
SCENARIOS = (
    "inherited", "explicit-file-allow", "protected-directory", "protected-file",
    "parent-delete-child", "leaf-delete", "readonly-object", "raw-descriptor-order",
    "stale-inheritance-order", "missing-node", "replaced-node", "unexpected-node", "sharing-conflict",
)
SPEC = importlib.util.spec_from_file_location(
    "qcs_diagnostics_evidence", DIRECTORY.parents[1] / ".github/scripts/qcs-ctest-evidence.py")
EVIDENCE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(EVIDENCE)

HARNESS = r'''
import importlib.util
import json
from pathlib import Path
import runpy
import sys
import unittest
from unittest import mock

sys.dont_write_bytecode = True
entry, mode, directory_text, scenarios_text = sys.argv[1:]
directory = Path(directory_text)
scenarios = json.loads(scenarios_text)
queue = None

class PortabilityContracts(unittest.TestCase):
    def test_windows_acl_restoration_restores_effective_access(self):
        for index, scenario in enumerate(scenarios):
            payload = {"scenario": scenario, "fixtureOnly": True, "details": "中文 路径" * 4096}
            queue("QCS_ACL_SCENARIO", payload)
            payload["scenario"] = "mutated after enqueue"
            with self.subTest(scenario=scenario):
                if mode == "failed-subtest" and index == 5:
                    self.fail("intentional diagnostic fixture failure")
        queue("QCS_ACL_FIXTURE_CLEANUP", {"scenario": "fixture", "cleaned": True, "fixtureOnly": True})

    def test_windows_acl_comparison_rejects_semantic_changes(self):
        pass

    def test_windows_powershell_bridge_preserves_developer_arguments(self):
        pass

PortabilityContracts.__module__ = "qcs_test_portability_runners"

class TokenContextContracts(unittest.TestCase):
    def test_windows_owned_child_preserves_parent_and_contains_descendants(self):
        pass

TokenContextContracts.__module__ = "qcs_test_token_context"

def load(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), directory / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module

# Apply the real entry boundary before replacing any test classes. Restarting
# an already mocked aggregate would lose this small fixture and run its suite.
token_context = load("qcs-token-context")
child_exit = token_context.reexec_if_needed(Path(__file__), sys.argv[1:])
if child_exit is not None:
    raise SystemExit(child_exit)

if entry == "aggregate":
    aggregate = load("qcs-test-runners")
    portability = load("qcs-test-portability-runners")
    token_tests = load("qcs-test-token-context")
    queue = portability.queue_diagnostic
    portability.PortabilityContracts = PortabilityContracts
    token_tests.TokenContextContracts = TokenContextContracts
    aggregate.SCRIPTS = ("qcs-test-portability-runners.py", "qcs-test-token-context.py")
    original_load = aggregate.load_script
    fixtures = {"qcs-test-portability-runners.py": portability, "qcs-test-token-context.py": token_tests}
    aggregate.load_script = lambda name: fixtures[name] if name in fixtures else original_load(name)
    aggregate.RecorderIntegration = type("EmptyFixtureIntegration", (unittest.TestCase,), {})
    raise SystemExit(aggregate.main([]))
elif entry == "standalone":
    original_main = unittest.main
    def fixture_main(*arguments, **keywords):
        global queue
        module = sys.modules["__main__"]
        queue = module.queue_diagnostic
        module.PortabilityContracts = PortabilityContracts
        module.TokenContextContracts = TokenContextContracts
        if keywords.get("testRunner") is not module.DiagnosticTestRunner:
            raise AssertionError("Standalone entry did not use the diagnostic runner")
        return original_main(*arguments, **keywords)
    unittest.main = fixture_main
    sys.argv = [str(directory / "qcs-test-portability-runners.py")]
    runpy.run_path(sys.argv[0], run_name="__main__")
elif entry == "runner-exception":
    portability = load("qcs-test-portability-runners")
    portability.queue_diagnostic("QCS_ACL_SCENARIO", {"scenario": "interrupted", "fixtureOnly": True})
    failure_type = KeyboardInterrupt if mode == "keyboard-interrupt" else RuntimeError
    with mock.patch.object(unittest.TextTestRunner, "run", side_effect=failure_type("intentional runner interruption")):
        portability.DiagnosticTestRunner(verbosity=2).run(unittest.TestSuite())
elif entry == "two-runs":
    portability = load("qcs-test-portability-runners")
    queue = portability.queue_diagnostic
    portability.DiagnosticTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(PortabilityContracts))
    print("QCS_SECOND_RUN", file=sys.stderr, flush=True)
    portability.DiagnosticTestRunner(verbosity=2).run(unittest.TestSuite())
else:
    raise AssertionError(entry)
'''


class DiagnosticOutputContracts(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(
            prefix="qcs-di-", dir=os.environ.get("QCOPILOTS_SEARCH_TEST_ROOT") or None)
        self.root = Path(self.temporary.name)
        self.harness = self.root / "fixture 中文.py"
        self.harness.write_text(HARNESS, encoding="utf-8")

    def tearDown(self):
        self.temporary.cleanup()
        self.assertFalse(self.root.exists(), "Diagnostic fixture cleanup failed")

    def run_fixture(self, entry, mode="success"):
        environment = dict(os.environ, PYTHONIOENCODING="utf-8")
        return subprocess.run(
            [sys.executable, "-B", str(self.harness), entry, mode, str(DIRECTORY), json.dumps(SCENARIOS)],
            cwd=self.root, env=environment, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            encoding="utf-8", errors="strict", timeout=30)

    def inspect(self, output):
        return EVIDENCE.inspect_signing_unittest(output, "Windows", EVIDENCE.ACCEPTANCE_NATIVE_TEST)

    def assert_diagnostics(self, output, conclusion):
        lines = output.splitlines()
        position = next(index for index, line in enumerate(lines) if line.startswith(conclusion))
        diagnostics = [(index, json.loads(line.removeprefix("QCS_ACL_SCENARIO ")))
                       for index, line in enumerate(lines) if line.startswith("QCS_ACL_SCENARIO ")]
        self.assertEqual([value["scenario"] for _, value in diagnostics], list(SCENARIOS))
        self.assertTrue(all(index > position for index, _ in diagnostics))
        self.assertTrue(all(value["fixtureOnly"] and len(value["details"]) > 20000 for _, value in diagnostics))
        cleanup = [json.loads(line.removeprefix("QCS_ACL_FIXTURE_CLEANUP "))
                   for line in lines if line.startswith("QCS_ACL_FIXTURE_CLEANUP ")]
        self.assertEqual(cleanup, [{"scenario": "fixture", "cleaned": True, "fixtureOnly": True}])

    def test_aggregate_and_standalone_keep_complete_cases_and_thirteen_diagnostics(self):
        for entry in ("aggregate", "standalone"):
            with self.subTest(entry=entry):
                result = self.run_fixture(entry)
                self.assertEqual(result.returncode, 0, result.stdout[-4000:])
                counts, records, skips, errors = self.inspect(result.stdout)
                self.assertEqual(errors, [], result.stdout[-4000:])
                self.assertEqual((counts["run"], counts["passed"], counts["failed"], counts["skipped"]), (4, 4, 0, 0))
                self.assertEqual({record["case"] for record in records}, EVIDENCE.ACCEPTANCE_WINDOWS_CASES | {
                    "qcs_test_token_context.TokenContextContracts.test_windows_owned_child_preserves_parent_and_contains_descendants"})
                self.assertEqual(skips, [])
                self.assert_diagnostics(result.stdout, "OK")

    def test_failed_subtest_preserves_diagnostics_without_becoming_a_pass(self):
        for entry in ("aggregate", "standalone"):
            with self.subTest(entry=entry):
                result = self.run_fixture(entry, "failed-subtest")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("intentional diagnostic fixture failure", result.stdout)
                self.assertIn("FAILED (failures=1)", result.stdout)
                self.assertTrue(self.inspect(result.stdout)[3])
                self.assert_diagnostics(result.stdout, "FAILED")

    def test_missing_case_record_is_still_rejected_by_strict_parser(self):
        result = self.run_fixture("aggregate")
        self.assertEqual(result.returncode, 0, result.stdout[-4000:])
        output = "\n".join(line for line in result.stdout.splitlines()
                           if not line.startswith("test_windows_acl_restoration_restores_effective_access ("))
        errors = self.inspect(output)[3]
        self.assertIn("Python unittest total does not match complete individual case records", errors)
        self.assertTrue(any("Required native portability case is missing" in error for error in errors))

    def test_runner_exception_emits_queued_diagnostics_and_remains_failure(self):
        for mode in ("runtime-error", "keyboard-interrupt"):
            with self.subTest(mode=mode):
                result = self.run_fixture("runner-exception", mode)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("intentional runner interruption", result.stdout)
                values = [json.loads(line.removeprefix("QCS_ACL_SCENARIO ")) for line in result.stdout.splitlines()
                          if line.startswith("QCS_ACL_SCENARIO ")]
                self.assertEqual(values, [{"scenario": "interrupted", "fixtureOnly": True}])
                self.assertTrue(self.inspect(result.stdout)[3])

    def test_emitted_diagnostics_are_not_replayed_by_a_second_run(self):
        result = self.run_fixture("two-runs")
        self.assertEqual(result.returncode, 0, result.stdout[-4000:])
        first, second = result.stdout.split("QCS_SECOND_RUN", 1)
        self.assert_diagnostics(first, "OK")
        self.assertNotIn("QCS_ACL_SCENARIO", second)
        self.assertNotIn("QCS_ACL_FIXTURE_CLEANUP", second)

    def test_flush_failure_preserves_primary_error_and_retains_unwritten_queue(self):
        class BrokenFlush(io.StringIO):
            def flush(self):
                raise OSError("intentional diagnostic flush failure")

        for interrupted in (False, True):
            with self.subTest(interrupted=interrupted):
                spec = importlib.util.spec_from_file_location(
                    "qcs_isolated_diagnostic_flush", DIRECTORY / "qcs-test-portability-runners.py")
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                module.queue_diagnostic("QCS_ACL_SCENARIO", {"scenario": "flush-fixture"})
                primary = RuntimeError("original runner interruption") if interrupted else None
                with mock.patch.object(unittest.TextTestRunner, "run", side_effect=primary, return_value=object()):
                    with self.assertRaises(RuntimeError if interrupted else OSError) as caught:
                        module.DiagnosticTestRunner(stream=BrokenFlush()).run(unittest.TestSuite())
                if interrupted:
                    self.assertIs(caught.exception, primary)
                    if hasattr(primary, "add_note"):
                        self.assertTrue(any("Diagnostic output also failed" in note for note in primary.__notes__))
                else:
                    self.assertIn("intentional diagnostic flush failure", str(caught.exception))
                recovered = io.StringIO()
                module.flush_diagnostics(recovered)
                self.assertEqual(recovered.getvalue().strip(), 'QCS_ACL_SCENARIO {"scenario": "flush-fixture"}')
                recovered.seek(0)
                recovered.truncate(0)
                module.flush_diagnostics(recovered)
                self.assertEqual(recovered.getvalue(), "")

    def test_external_except_does_not_hide_current_flush_failure(self):
        class BrokenFlush(io.StringIO):
            def flush(self):
                raise OSError("current diagnostic flush failure")

        spec = importlib.util.spec_from_file_location(
            "qcs_external_except_diagnostic_flush", DIRECTORY / "qcs-test-portability-runners.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.queue_diagnostic("QCS_ACL_SCENARIO", {"scenario": "outer-exception-fixture"})
        try:
            raise RuntimeError("unrelated caller exception")
        except RuntimeError as unrelated:
            with mock.patch.object(unittest.TextTestRunner, "run", return_value=object()):
                with self.assertRaisesRegex(OSError, "current diagnostic flush failure"):
                    module.DiagnosticTestRunner(stream=BrokenFlush()).run(unittest.TestSuite())
            self.assertFalse(getattr(unrelated, "__notes__", []))
        recovered = io.StringIO()
        module.flush_diagnostics(recovered)
        self.assertEqual(recovered.getvalue().strip(),
                         'QCS_ACL_SCENARIO {"scenario": "outer-exception-fixture"}')


if __name__ == "__main__":
    unittest.main(verbosity=2)
