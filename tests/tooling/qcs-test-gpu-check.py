#!/usr/bin/env python3
"""Synthetic GPU-check contracts. These tests never verify physical GPU hardware."""
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
SOURCE = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("qcs_gpu_check_contracts", SOURCE / "tools/qcs-check-gpus.py")
gpu_check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gpu_check)


class GpuCheckContracts(unittest.TestCase):
    def setUp(self):
        configured = os.environ.get("QCOPILOTS_SEARCH_TEST_ROOT") or None
        if configured:
            Path(configured).mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix="qcs-gpu-contract-中文 ", dir=configured)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.devices = [self.device(number) for number in (1, 2)]
        self.slots = [self.slot(device) for device in self.devices]
        self.status = {
            "modelAvailable": True,
            "progress": {"state": "complete", "total": 8, "completed": 8, "failed": 0, "segments": 24},
            "indexDevices": deepcopy(self.slots),
            "indexDeviceCounts": {"discovered": 2, "ready": 2, "busy": 0, "failed": 0},
            "queuedFiles": 0, "catalogReady": True, "failures": [], "warning": "", "pipelineTransitionError": "",
        }
        self.evidence = {gpu_check.device_key(slot): deepcopy(slot) for slot in self.slots}

    @staticmethod
    def device(number):
        return {"backend": "hip", "deviceId": f"0000:0{number}:00.0", "name": "AMD Radeon identical model",
                "description": f"Synthetic device {number}", "gpu": True, "available": True}

    @staticmethod
    def slot(device):
        return {**device, "state": "ready", "completedTasks": 4, "activePath": "", "error": "",
                "runtime": {"gpuSelected": True, "gpuSelfTestPassed": True, "textWarmupCompleted": True,
                            "loaded": True, "fallbackReason": "", "error": "", "requestedBackend": "gpu",
                            "deviceBindingExplicit": True, "selectedBackend": device["backend"],
                            "deviceId": device["deviceId"], "deviceName": device["name"]}}

    def validate(self):
        return gpu_check.validate_run(self.status, self.devices, 8, self.evidence)

    def test_complete_synthetic_status_validates_without_claiming_hardware_verification(self):
        result = self.validate()
        self.assertEqual(result["files"], 8)
        self.assertEqual(result["segments"], 24)
        self.assertEqual(len(result["devices"]), 2)
        self.assertNotIn("hardwareVerified", result)

    def test_probe_uses_all_available_devices_from_packaged_backend(self):
        unavailable = {**self.device(3), "available": False, "unavailableReason": "synthetic driver failure"}
        cpu = {"backend": "cpu", "name": "CPU", "gpu": False, "available": True}
        probe = {"packagedBackend": "HIP", "devices": [cpu, self.devices[0], unavailable, self.devices[1]]}
        self.assertEqual(gpu_check.selected_devices(probe), self.devices)

    def test_probe_rejects_missing_compatible_gpu_and_cpu_only_fallback(self):
        probes = [{}, {"devices": []},
                  {"devices": [{"backend": "cpu", "gpu": False, "available": True, "name": "CPU"}]},
                  {"packagedBackend": "hip", "devices": [{**self.device(1), "available": False}]}]
        for probe in probes:
            with self.subTest(probe=probe), self.assertRaises(RuntimeError):
                gpu_check.selected_devices(probe)

    def test_probe_rejects_duplicate_identity_even_when_display_names_differ(self):
        duplicate = {**self.devices[0], "name": "A different label"}
        with self.assertRaises(RuntimeError):
            gpu_check.selected_devices({"packagedBackend": "hip", "devices": [self.devices[0], duplicate]})

    def test_probe_rejects_device_from_another_backend(self):
        mixed = [self.devices[0], {**self.devices[1], "backend": "cuda"}]
        with self.assertRaises(RuntimeError):
            gpu_check.selected_devices({"packagedBackend": "hip", "devices": mixed})

    def test_unique_names_are_used_only_when_device_id_is_unavailable(self):
        first = {**self.devices[0], "deviceId": "", "name": "Vulkan0"}
        second = {**self.devices[1], "deviceId": "", "name": "Vulkan1"}
        self.assertEqual(gpu_check.device_key(first), ("hip", "Vulkan0"))
        self.assertEqual(gpu_check.selected_devices({"packagedBackend": "hip", "devices": [first, second]}), [first, second])
        with self.assertRaises(RuntimeError):
            gpu_check.selected_devices({"packagedBackend": "hip", "devices": [first, {**second, "name": "Vulkan0"}]})

    def test_identity_rejects_cpu_and_missing_identity(self):
        for device in ({"backend": "cpu", "deviceId": "cpu0"}, {"backend": "hip"}, {"name": "GPU"}):
            with self.subTest(device=device), self.assertRaises(RuntimeError):
                gpu_check.device_key(device)

    def test_selector_contains_only_explicit_binding_fields(self):
        self.assertEqual(gpu_check.selector(self.devices[0]), {
            key: self.devices[0][key] for key in ("backend", "deviceId", "name")})

    def test_runtime_requires_actual_loaded_model_selftest_and_warmup(self):
        for name in ("gpuSelected", "gpuSelfTestPassed", "textWarmupCompleted", "loaded", "deviceBindingExplicit"):
            for value in (False, None, 1, "true"):
                with self.subTest(name=name, value=value):
                    entry = deepcopy(self.slots[0])
                    entry["runtime"][name] = value
                    with self.assertRaises(RuntimeError):
                        gpu_check.gpu_evidence(entry)

    def test_runtime_rejects_fallback_error_and_non_strict_backend(self):
        for name, value in (("fallbackReason", "GPU out of memory, retried on CPU"),
                            ("error", "synthetic failure"), ("requestedBackend", "auto"),
                            ("requestedBackend", "cpu")):
            with self.subTest(name=name, value=value):
                entry = deepcopy(self.slots[0])
                entry["runtime"][name] = value
                with self.assertRaises(RuntimeError):
                    gpu_check.gpu_evidence(entry)

    def test_runtime_device_and_backend_must_match_slot(self):
        for name, value in (("deviceId", self.devices[1]["deviceId"]), ("deviceId", ""),
                            ("selectedBackend", "cuda"), ("selectedBackend", "cpu")):
            with self.subTest(name=name, value=value):
                entry = deepcopy(self.slots[0])
                entry["runtime"][name] = value
                with self.assertRaises(RuntimeError):
                    gpu_check.gpu_evidence(entry)

    def test_probe_without_execution_evidence_never_passes(self):
        self.evidence = {}
        with self.assertRaises(RuntimeError):
            self.validate()

    def test_evidence_cannot_be_reassigned_from_another_device(self):
        key = gpu_check.device_key(self.devices[0])
        self.evidence[key] = deepcopy(self.slots[1])
        with self.assertRaises(RuntimeError):
            self.validate()

    def test_initialization_without_a_completed_task_is_not_execution_evidence(self):
        key = gpu_check.device_key(self.devices[0])
        self.evidence[key]["completedTasks"] = 0
        with self.assertRaises(RuntimeError):
            self.validate()

    def test_idle_released_workers_keep_previously_observed_completion_evidence(self):
        for slot in self.status["indexDevices"]:
            slot["state"] = "idle"
            slot["runtime"]["loaded"] = False
        self.assertEqual(self.validate()["files"], 8)

    def test_each_selected_gpu_must_complete_work(self):
        for count in (0, -1):
            with self.subTest(count=count):
                self.status["indexDevices"][1]["completedTasks"] = count
                with self.assertRaises(RuntimeError):
                    self.validate()

    def test_exact_device_set_rejects_missing_additional_and_duplicate_slots(self):
        for slots in (self.slots[:1], [*self.slots, self.slot(self.device(3))], [self.slots[0], self.slots[0]]):
            with self.subTest(slots=slots):
                self.status["indexDevices"] = slots
                with self.assertRaises(RuntimeError):
                    self.validate()

    def test_duplicate_requested_devices_are_rejected(self):
        self.devices = [*self.devices, self.devices[0]]
        with self.assertRaises(RuntimeError):
            self.validate()

    def test_cpu_slot_cannot_replace_a_missing_gpu(self):
        self.status["indexDevices"][1] = {"backend": "cpu", "name": "CPU", "completedTasks": 4, "state": "ready"}
        with self.assertRaises(RuntimeError):
            self.validate()

    def test_indexing_must_complete_exactly_the_full_corpus(self):
        for field, value in (("total", 7), ("total", 9), ("completed", 7), ("completed", 9)):
            with self.subTest(field=field, value=value):
                progress = self.status["progress"]
                original = progress[field]
                progress[field] = value
                with self.assertRaises(RuntimeError):
                    self.validate()
                progress[field] = original

    def test_failed_partial_paused_and_active_statuses_are_not_success(self):
        for state in ("failed", "error", "partial", "paused", "indexing", "scanning", ""):
            with self.subTest(state=state):
                self.status["progress"]["state"] = state
                with self.assertRaises(RuntimeError):
                    self.validate()

    def test_progress_failure_or_error_is_rejected(self):
        for field, value in (("failed", 1), ("error", "synthetic extraction failure")):
            with self.subTest(field=field):
                self.status["progress"][field] = value
                with self.assertRaises(RuntimeError):
                    self.validate()
                self.status["progress"].pop(field)

    def test_models_are_required(self):
        self.status["modelAvailable"] = False
        with self.assertRaises(RuntimeError):
            self.validate()

    def test_slot_failure_is_not_hidden_by_earlier_successful_execution(self):
        for field, value in (("state", "failed"), ("error", "GPU disappeared")):
            with self.subTest(field=field):
                self.status["indexDevices"] = deepcopy(self.slots)
                self.status["indexDevices"][1][field] = value
                with self.assertRaises(RuntimeError):
                    self.validate()

    def test_incomplete_work_is_not_hidden_by_complete_progress(self):
        for mutation in (lambda state: state.update(queuedFiles=1),
                         lambda state: state["indexDeviceCounts"].update(busy=1),
                         lambda state: state["indexDeviceCounts"].update(failed=1),
                         lambda state: state["indexDevices"][0].update(state="busy"),
                         lambda state: state["indexDevices"][0].update(activePath="pending.txt")):
            with self.subTest(mutation=mutation):
                original = deepcopy(self.status)
                mutation(self.status)
                with self.assertRaises(RuntimeError):
                    self.validate()
                self.status = original

    def test_service_errors_are_not_hidden_by_complete_progress(self):
        for field, value in (("pipelineTransitionError", "model changed"), ("warning", "repository unavailable"),
                            ("failures", [{"path": "document-00001.txt", "error": "failed"}])):
            with self.subTest(field=field):
                original = self.status[field]
                self.status[field] = value
                with self.assertRaises(RuntimeError):
                    self.validate()
                self.status[field] = original

    def test_corpus_is_identical_across_separate_roots_and_hashes_match_bytes(self):
        first = self.root / "first corpus"
        second = self.root / "second 语料"
        manifest = gpu_check.create_corpus(first, 4, 3)
        self.assertEqual(gpu_check.create_corpus(second, 4, 3), manifest)
        self.assertEqual(len(manifest), 4)
        self.assertEqual(len({item["sha256"] for item in manifest}), 4)
        for item in manifest:
            content = (first / item["name"]).read_bytes()
            self.assertEqual(item["bytes"], len(content))
            self.assertEqual(item["sha256"], hashlib.sha256(content).hexdigest())
            self.assertIn(gpu_check.TOKEN.encode(), content)
            self.assertNotIn(b"\r\n", content)
            self.assertEqual(content, (second / item["name"]).read_bytes())

    def test_corpus_does_not_overwrite_an_existing_directory(self):
        corpus = self.root / "existing"
        corpus.mkdir()
        sentinel = corpus / "user-data.txt"
        sentinel.write_text("keep", encoding="utf-8")
        with self.assertRaises(FileExistsError):
            gpu_check.create_corpus(corpus, 4, 3)
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")

    def test_output_paths_must_resolve_outside_repository(self):
        self.assertEqual(gpu_check.require_external(self.root / "new child"), self.root / "new child")
        for path in (SOURCE, SOURCE / "work", SOURCE / "tools" / ".." / "generated"):
            with self.subTest(path=path), self.assertRaises(RuntimeError):
                gpu_check.require_external(path)

    def test_child_environment_clears_profile_and_fake_worker_overrides(self):
        forbidden = ("QCS_HOME", "QCOPILOTS_SEARCH_PROFILE_DIR", "QCOPILOTS_SEARCH_TEST_WORKER",
                     "QCOPILOTS_SEARCH_TEST_SERVICE", "QCOPILOTS_SEARCH_INDEX_DEVICE")
        values = {name: "must not inherit" for name in forbidden}
        with patch.dict(os.environ, {**values, "QCOPILOTS_SEARCH_BACKEND": "cpu", "QCS_KEEP_ME": "kept"}):
            environment = gpu_check.child_environment()
        self.assertEqual(environment["QCOPILOTS_SEARCH_BACKEND"], "gpu")
        self.assertEqual(environment["QT_QPA_PLATFORM"], "offscreen")
        self.assertEqual(environment["QCS_KEEP_ME"], "kept")
        for name in forbidden:
            self.assertNotIn(name, environment)

    def test_different_single_and_multi_card_results_cannot_mark_hardware_verified(self):
        # Process entry points are mocked only to reach the report rejection path.
        # No synthetic run is ever presented as physical GPU verification.
        binaries = self.root / "synthetic-bin"
        binaries.mkdir()
        suffix = ".exe" if os.name == "nt" else ""
        for name in ("cli", "service", "worker"):
            (binaries / f"qcs-{name}{suffix}").write_bytes(b"synthetic fixture, not executable")
        models = self.root / "models"
        models.mkdir()
        work = self.root / "reports"
        results = [{"files": 8, "segments": count, "secondsIncludingInitialization": 1, "label": label}
                   for label, count in (("all-gpus", 24), ("single-gpu-1", 23), ("single-gpu-2", 24))]
        with patch.object(gpu_check, "invoke", return_value={"verified": True}), \
                patch.object(gpu_check, "probe_devices", return_value={"packagedBackend": "hip", "devices": self.devices}), \
                patch.object(gpu_check, "run_index", side_effect=results), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = gpu_check.main(["--bin-dir", str(binaries), "--model-dir", str(models),
                                   "--work-root", str(work), "--files", "8", "--paragraphs", "1"])
        reports = list(work.glob("qcs-gpu-check-*/report.json"))
        self.assertEqual(len(reports), 1)
        report = json.loads(reports[0].read_text(encoding="utf-8"))
        self.assertEqual(code, 1)
        self.assertIs(report["hardwareVerified"], False)
        self.assertIn("different file or segment counts", report["error"])


if __name__ == "__main__":
    unittest.main()
