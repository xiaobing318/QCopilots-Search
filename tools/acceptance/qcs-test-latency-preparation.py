"""Light validation only: pure evidence data and owned Python subprocesses."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

sys.dont_write_bytecode = True
CAPABILITIES = {"windowsOwnedJobs": {"status": "registered" if os.name == "nt" else "not_applicable",
                                    "reason": "Windows JobObject execution helper"}}
ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("latency", ROOT / "qcs-service-latency.py")
latency = importlib.util.module_from_spec(spec)
spec.loader.exec_module(latency)


class PreparationTests(unittest.TestCase):
    def test_new_fingerprint_revision_is_dynamic_but_exact(self):
        semantic = {"passed": True, "extractionFingerprint": "qcopilots-search-extraction-v6:" + "a" * 64,
                    "modelFingerprint": "qcopilots-search-embedding-v1:" + "b" * 64}
        latency.verify_fingerprints(semantic, dict(semantic))
        candidate = dict(semantic)
        candidate["extractionFingerprint"] = "qcopilots-search-extraction-v7:" + "a" * 64
        with self.assertRaisesRegex(RuntimeError, "disagree"):
            latency.verify_fingerprints(semantic, candidate)
        semantic["passed"] = False
        with self.assertRaisesRegex(RuntimeError, "successful"):
            latency.verify_fingerprints(semantic, candidate)

    def test_selected_queries_keep_fifty_distinct_inputs(self):
        queries = [{"query": f"query-{i}", "modality": "code" if i % 2 else "text"} for i in range(60)]
        selected = latency.choose_queries(queries)
        self.assertEqual(len(selected), 50)
        self.assertEqual(len({q["query"] for q in selected}), 50)
        self.assertEqual(len(selected[:10]), 10)

    def test_duplicate_queries_cannot_satisfy_distinct_measurement_gate(self):
        queries = [{"query": "same query", "modality": "text"}] * 60
        with self.assertRaisesRegex(RuntimeError, "distinct"):
            latency.choose_queries(queries)

    def test_candidate_rejects_legacy_executable_names(self):
        candidate = {"kind": "package-candidate", "executables": {"old-cli": {}}}
        with self.assertRaisesRegex(RuntimeError, "all four"):
            latency.resolve_candidate(Path("candidate.json"), candidate, None, {})

    def test_missing_working_directory_fails_without_creating_it(self):
        with tempfile.TemporaryDirectory(prefix="qcs-latency-", dir=os.environ.get("QCOPILOTS_SEARCH_TEST_ROOT") or None) as temporary:
            missing = Path(temporary) / "missing cwd 中文"
            with self.assertRaisesRegex(RuntimeError, "working-directory"):
                latency.check_working_directory(missing)
            self.assertFalse(missing.exists())

    def test_contained_paths_reject_escape(self):
        with tempfile.TemporaryDirectory(prefix="qcs-latency-validation-", dir=os.environ.get("QCOPILOTS_SEARCH_TEST_ROOT") or None) as temporary:
            root = Path(temporary)
            self.assertEqual(latency.contained_file(root, "input/file.txt"), root / "input/file.txt")
            for path in ("../outside.txt", "/absolute.txt", "C:/drive.txt"):
                with self.assertRaises(RuntimeError):
                    latency.contained_file(root, path)

    def test_captured_cli_success_preserves_raw_reply_and_short_cwd(self):
        with tempfile.TemporaryDirectory(prefix="qcs-latency-validation-", dir=os.environ.get("QCOPILOTS_SEARCH_TEST_ROOT") or None) as temporary:
            root = Path(temporary)
            cwd = Path(sys.executable).parent
            result, record = latency.captured_command(
                [sys.executable, "-B", "-c", "import sys; print('{\"ok\":true,\"result\":{\"hits\":[]}}'); print('diagnostic',file=sys.stderr)"],
                cwd, dict(os.environ), getattr(subprocess, "CREATE_NO_WINDOW", 0), root / "commands", 1, 10)
            self.assertEqual(result.returncode, 0)
            self.assertTrue(json.loads((root / record["stdout"]).read_bytes())["ok"])
            self.assertEqual((root / record["stderr"]).read_text().strip(), "diagnostic")
            self.assertEqual(record["cwd"], str(cwd))
            self.assertEqual(record["stdoutSha256"], latency.sha256(root / record["stdout"]))

    def test_captured_cli_timeout_preserves_partial_output(self):
        with tempfile.TemporaryDirectory(prefix="qcs-latency-validation-", dir=os.environ.get("QCOPILOTS_SEARCH_TEST_ROOT") or None) as temporary:
            root = Path(temporary)
            with self.assertRaises(subprocess.TimeoutExpired) as caught:
                latency.captured_command([sys.executable, "-B", "-c", "import time; print('partial',flush=True); time.sleep(30)"],
                    Path(sys.executable).parent, dict(os.environ), getattr(subprocess, "CREATE_NO_WINDOW", 0), root / "commands", 1, 1)
            record = caught.exception.evidence_record
            self.assertTrue(record["timedOut"])
            self.assertIn(b"partial", (root / record["stdout"]).read_bytes())
            self.assertEqual(record["stderrSha256"], latency.sha256(root / record["stderr"]))

    def test_capture_and_manifest_are_portable_and_exclude_large_artifacts(self):
        with tempfile.TemporaryDirectory(prefix="qcs-latency-validation-", dir=os.environ.get("QCOPILOTS_SEARCH_TEST_ROOT") or None) as temporary:
            root = Path(temporary)
            candidate_root, corpus, run = root / "candidate", root / "corpus", root / "run"
            for path in (candidate_root / "build", corpus, run):
                path.mkdir(parents=True)
            candidate_files = []
            for name in ("build/build-record.json", "build/service.exe", "final.zip", "sources.tar.gz"):
                path = candidate_root / name
                path.write_bytes(b"fixture")
                candidate_files.append({"path": name, "bytes": path.stat().st_size, "sha256": latency.sha256(path)})
            candidate = {"package": "final.zip", "packageSha256": latency.sha256(candidate_root / "final.zip"), "files": candidate_files}
            candidate_path = candidate_root / "package-candidate.json"
            latency.write_json(candidate_path, candidate)
            (corpus / "source.txt").write_text("genuine fixture bytes", encoding="utf-8")
            semantic = {"files": [{"file": "source.txt", "sha256": latency.sha256(corpus / "source.txt")}]}
            semantic_path = root / "semantic.json"
            latency.write_json(semantic_path, semantic)
            records, external = latency.capture_inputs(run, candidate_path, candidate, semantic_path, semantic, corpus,
                                                       ROOT / "qcs-test-latency-preparation.py", reserve_gib=0)
            names = {record["path"] for record in records}
            self.assertIn("inputs/corpus/source.txt", names)
            self.assertIn("inputs/candidate-materials/build/service.exe", names)
            self.assertEqual({item["path"] for item in external}, {"final.zip", "sources.tar.gz"})
            profile = run / "profile 中文 space"
            profile.mkdir()
            (profile / "database.sqlite").write_bytes(b"not archived")
            latency.write_json(run / "report.json", {"passed": True})
            latency.finalize_manifest(run)
            manifest = json.loads((run / "manifest.json").read_text())
            self.assertFalse(any(item["path"].startswith("profile") for item in manifest["files"]))
            for item in manifest["files"]:
                self.assertEqual(latency.sha256(run / item["path"]), item["sha256"])

    if os.name == "nt":
        def test_windows_job_cleans_its_descendant_and_preserves_unrelated_process(self):
            processes = latency.WindowsProcesses()
            flags = subprocess.CREATE_NO_WINDOW
            sentinel = subprocess.Popen([sys.executable, "-B", "-c", "import time; time.sleep(60)"], creationflags=flags)
            owned = job = None
            try:
                owned = subprocess.Popen([sys.executable, "-B", "-c",
                    "import subprocess,sys,time; subprocess.Popen([sys.executable,'-B','-c','import time; time.sleep(60)']); time.sleep(60)"],
                    creationflags=flags | latency.CREATE_SUSPENDED)
                job = processes.job(owned)
                processes.resume(owned)
                deadline = time.monotonic() + 10
                while processes.active(job) < 2 and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertEqual(processes.active(job), 2)
                self.assertTrue(processes.contains(job, owned.pid))
                self.assertFalse(processes.contains(job, sentinel.pid))
                self.assertEqual(processes.stop(job), 0)
                owned.wait(timeout=10)
                self.assertIsNone(sentinel.poll())
            finally:
                if job is not None:
                    processes.stop(job)
                    processes.close(job)
                if owned is not None and owned.poll() is None:
                    owned.terminate()
                    owned.wait(timeout=10)
                if sentinel.poll() is None:
                    sentinel.terminate()
                    sentinel.wait(timeout=10)


if __name__ == "__main__":
    print(json.dumps({"testCapabilities": CAPABILITIES}, ensure_ascii=False), file=sys.stderr)
    unittest.main(verbosity=2)
