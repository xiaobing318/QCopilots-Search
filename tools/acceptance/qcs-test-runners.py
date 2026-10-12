#!/usr/bin/env python3
"""Run lightweight acceptance-tool contracts with one complete unittest report.

These tests use synthetic data and temporary files. They do not execute the
product, a model, a compiler, CTest, an installer or a Computer Use scenario.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import unittest

sys.dont_write_bytecode = True
DIRECTORY = Path(__file__).resolve().parent
sys.path.insert(0, str(DIRECTORY))
SCRIPTS = (
    "qcs-test-candidate-runner.py",
    "qcs-test-source-checksums.py",
    "qcs-test-capacity-runners.py",
    "qcs-test-package-runners.py",
    "qcs-test-portability-runners.py",
    "qcs-test-latency-preparation.py",
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


def main():
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    capabilities = {}
    for filename in SCRIPTS:
        module = load_script(filename)
        if hasattr(module, "CAPABILITIES"):
            capabilities[filename] = module.CAPABILITIES
        tests = loader.loadTestsFromModule(module)
        if tests.countTestCases() == 0:
            raise RuntimeError(f"Required script registered no tests: {filename}")
        suite.addTests(tests)
    suite.addTests(loader.loadTestsFromTestCase(RecorderIntegration))
    print("QCS_ACCEPTANCE_TEST_CAPABILITIES " + json.dumps(capabilities, ensure_ascii=False),
          file=sys.stderr, flush=True)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    # Required skips are not successes, matching the release evidence parser.
    return 0 if result.wasSuccessful() and not result.skipped else 1


if __name__ == "__main__":
    raise SystemExit(main())
