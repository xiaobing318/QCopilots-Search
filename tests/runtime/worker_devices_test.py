"""Exercise real worker discovery and selection protocol without model files."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


PARSER = argparse.ArgumentParser()
PARSER.add_argument("--worker", required=True, type=Path)
ARGS, TEST_ARGS = PARSER.parse_known_args()


class WorkerDevicesTest(unittest.TestCase):
    def setUp(self):
        configured = os.environ.get("QCOPILOTS_SEARCH_TEST_ROOT")
        if configured:
            Path(configured).mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="qcs-worker-devices-", dir=configured)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def invoke(self, request, *arguments):
        environment = dict(os.environ, QCOPILOTS_SEARCH_BACKEND="cpu", QT_QPA_PLATFORM="offscreen")
        return subprocess.run(
            [str(ARGS.worker), "--profile-dir", str(self.root / "profile"),
             "--model-dir", str(self.root / "absent-models"), *arguments],
            input=json.dumps(request) + "\n", capture_output=True, text=True, encoding="utf-8",
            cwd=self.root, env=environment, timeout=45, check=False,
        )

    def test_discovery_requires_no_models_and_is_available_in_every_mode(self):
        for mode in ("full", "extract", "query"):
            with self.subTest(mode=mode):
                result = self.invoke({"id": "discovery", "operation": "devices"}, "--mode", mode)
                self.assertEqual(result.returncode, 0, result.stderr)
                frame = json.loads(result.stdout)
                self.assertEqual(frame["id"], "discovery")
                self.assertTrue(frame["ok"], frame)
                discovered = frame["result"]
                self.assertEqual(discovered["requestedMode"], "cpu")
                self.assertEqual(discovered["gpuLoadError"], "")
                self.assertTrue(discovered["packagedBackend"])
                self.assertTrue(discovered["devices"])
                for device in discovered["devices"]:
                    self.assertTrue(device["cpu"])
                    self.assertFalse(device["gpu"])
                    self.assertIsInstance(device["deviceId"], str)
                    self.assertIsInstance(device["name"], str)
                    self.assertIsInstance(device["description"], str)
                    self.assertIsInstance(device["available"], bool)
                    self.assertGreaterEqual(device["memoryTotal"], 0)
                    self.assertGreaterEqual(device["memoryFree"], 0)
                    self.assertEqual(device["available"], not device["unavailableReason"])

    def test_selector_is_recorded_without_loading_models(self):
        selector = {"backend": "cpu", "deviceId": "", "name": "CPU"}
        result = self.invoke({"id": 2, "operation": "diagnostics"}, "--device", json.dumps(selector))
        self.assertEqual(result.returncode, 0, result.stderr)
        frame = json.loads(result.stdout)
        self.assertTrue(frame["ok"], frame)
        runtime = frame["result"]["runtime"]
        self.assertEqual(runtime["requestedDevice"], selector)
        self.assertTrue(runtime["deviceBindingExplicit"])
        self.assertFalse(runtime["loaded"])
        self.assertFalse(runtime["deviceFailure"])

    def test_invalid_selectors_fail_before_processing(self):
        for selector in ("[]", "{}", "{", '{"backend":"cuda"}',
                         '{"backend":"cuda","deviceId":1}',
                         '{"backend":"unknown","name":"GPU0"}'):
            with self.subTest(selector=selector):
                result = self.invoke({"id": 3, "operation": "devices"}, "--device", selector)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("Invalid device selector", result.stderr)
                self.assertEqual(result.stdout, "")


if __name__ == "__main__":
    unittest.main(argv=[__file__, *TEST_ARGS])
