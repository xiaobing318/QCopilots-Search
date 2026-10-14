#!/usr/bin/env python3
"""Run acceptance-tool contracts with one complete unittest report.

These tests use synthetic data and temporary files. They do not execute the
product, a model, a compiler, CTest, an installer or a Computer Use scenario.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import sys
import unittest

sys.dont_write_bytecode = True
DIRECTORY = Path(__file__).resolve().parent
sys.path.insert(0, str(DIRECTORY))
SCRIPTS = (
    "qcs-test-candidate-runner.py",
    "qcs-test-source-checksums.py",
    "qcs-test-portability-runners.py",
    "qcs-test-diagnostics.py",
    "qcs-test-token-context.py",
    "qcs-cu-recorder-tests.py",
    "qcs-test-copy-cu-fixtures.py",
)


def load_script(filename):
    path = DIRECTORY / filename
    if not path.is_file():
        raise RuntimeError(f"Required acceptance test script is missing: {path}")
    name = path.stem.replace("-", "_")
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load acceptance test script: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class RecorderIntegration(unittest.TestCase):
    def test_synthetic_record_import_preserves_candidate_and_artifact_binding(self):
        module = load_script("qcs-cu-release-integration-test.py")
        result = module.integration(DIRECTORY / "qcs-cu-recorder.py",
                                    DIRECTORY.parent / "qcs-release-evidence.py")
        self.assertTrue(result["testPassed"])
        self.assertFalse(result["realAcceptancePerformed"])
        self.assertTrue(result["temporaryEvidenceDeleted"])


def unexpected_skips(result, platform=None):
    portability = load_script("qcs-test-portability-runners.py")
    tokens = load_script("qcs-test-token-context.py")
    return [(test, reason) for test, reason in result.skipped
            if not any(module.is_windows_platform_skip(test, reason, os.name if platform is None else platform)
                       for module in (portability, tokens))]


def iter_cases(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from iter_cases(item)
        else:
            yield item


def native_case_ids():
    portability = load_script("qcs-test-portability-runners.py")
    tokens = load_script("qcs-test-token-context.py")
    return set(portability.WINDOWS_NATIVE_CASES | tokens.WINDOWS_NATIVE_CASES)


def build_suite(selection="all"):
    if selection not in ("pure", "native", "all"):
        raise ValueError(f"Unknown acceptance suite: {selection}")
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    capabilities = {}
    portability = None
    for filename in SCRIPTS:
        module = load_script(filename)
        if filename == "qcs-test-portability-runners.py":
            portability = module
        if hasattr(module, "CAPABILITIES"):
            capabilities[filename] = module.CAPABILITIES
        tests = loader.loadTestsFromModule(module)
        if tests.countTestCases() == 0:
            raise RuntimeError(f"Required script registered no tests: {filename}")
        suite.addTests(tests)
    suite.addTests(loader.loadTestsFromTestCase(RecorderIntegration))
    cases = list(iter_cases(suite))
    native_ids = native_case_ids()
    actual_ids = {case.id() for case in cases}
    if not native_ids.issubset(actual_ids):
        raise RuntimeError("Native acceptance case registration is incomplete")
    chosen = [case for case in cases if selection == "all" or
              ((case.id() in native_ids) == (selection == "native"))]
    if not chosen:
        raise RuntimeError("Acceptance suite selected no cases")
    return unittest.TestSuite(chosen), capabilities, portability


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", choices=("pure", "native", "all"), default="all")
    options = parser.parse_args(argv)
    suite, capabilities, portability = build_suite(options.suite)
    print("QCS_ACCEPTANCE_TEST_CAPABILITIES " + json.dumps(capabilities, ensure_ascii=False),
          file=sys.stderr, flush=True)
    if portability is None:
        raise RuntimeError("Required portability diagnostic producer was not loaded")
    result = portability.DiagnosticTestRunner(verbosity=2).run(suite)
    # Only the registered exact Windows-native contracts are inapplicable elsewhere.
    # Missing tools on Windows and every other skip remain required failures.
    return 0 if result.wasSuccessful() and not unexpected_skips(result) else 1


if __name__ == "__main__":
    # Pure contracts never open or alter a native process token.
    pure = "--suite" in sys.argv and sys.argv[sys.argv.index("--suite") + 1:][:1] == ["pure"]
    child_code = None if pure else load_script("qcs-token-context.py").reexec_if_needed(Path(__file__), sys.argv[1:])
    if child_code is not None:
        raise SystemExit(child_code)
    raise SystemExit(main())
