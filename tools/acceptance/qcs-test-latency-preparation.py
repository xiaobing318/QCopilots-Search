"""Light validation only: pure evidence data and owned Python subprocesses."""
import contextlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import sqlite3
import tempfile
import time
import unittest
from unittest import mock

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

    def test_corpus_index_plan_preserves_all_186_space_memberships(self):
        with tempfile.TemporaryDirectory(prefix="qcs-latency-plan-", dir=os.environ.get("QCOPILOTS_SEARCH_TEST_ROOT") or None) as temporary:
            corpus = Path(temporary) / "corpus 中文"
            corpus.mkdir()
            normal = [{"file": f"normal-{i:03d}.txt", "modality": "text"} for i in range(160)]
            code = [{"file": f"code-{i:03d}.py", "modality": "code"} for i in range(26)]
            plan = latency.corpus_index_plan({"files": normal + code}, corpus)
            self.assertEqual(plan["counts"], {"normal": 160, "code": 26, "memberships": 186, "physicalFiles": 186})
            self.assertEqual(plan["settings"]["roots"], [str(corpus)])
            self.assertEqual(plan["settings"]["codeRoots"], [str(corpus)])
            self.assertTrue(plan["settings"]["includeCode"])
            self.assertEqual(set(plan["settings"]["excludes"]), {str(corpus / row["file"]) for row in code})
            self.assertEqual(set(plan["settings"]["codeExcludes"]), {str(corpus / row["file"]) for row in normal})
            expected = [{"file": row["file"], "code": row["modality"] == "code"} for row in normal + code]
            self.assertCountEqual(plan["memberships"], expected)

    def test_corpus_index_plan_keeps_shared_path_in_both_spaces(self):
        with tempfile.TemporaryDirectory(prefix="qcs-latency-shared-", dir=os.environ.get("QCOPILOTS_SEARCH_TEST_ROOT") or None) as temporary:
            corpus = Path(temporary) / "corpus"
            corpus.mkdir()
            semantic = {"files": [{"file": "shared.txt", "modality": "text"},
                                  {"file": "shared.txt", "modality": "code"},
                                  {"file": "ordinary.txt", "modality": "text"},
                                  {"file": "script.py", "modality": "code"}]}
            plan = latency.corpus_index_plan(semantic, corpus)
            self.assertEqual(plan["counts"], {"normal": 2, "code": 2, "memberships": 4, "physicalFiles": 3})
            self.assertEqual([row for row in plan["memberships"] if row["file"] == "shared.txt"],
                             [{"file": "shared.txt", "code": False}, {"file": "shared.txt", "code": True}])
            self.assertEqual(plan["settings"]["excludes"], [str(corpus / "script.py")])
            self.assertEqual(plan["settings"]["codeExcludes"], [str(corpus / "ordinary.txt")])

    def test_corpus_index_plan_does_not_enable_an_empty_space(self):
        with tempfile.TemporaryDirectory(prefix="qcs-latency-one-space-", dir=os.environ.get("QCOPILOTS_SEARCH_TEST_ROOT") or None) as temporary:
            corpus = Path(temporary)
            normal = latency.corpus_index_plan({"files": [{"file": "ordinary.txt", "modality": "text"}]}, corpus)
            self.assertEqual(normal["settings"]["roots"], [str(corpus)])
            self.assertEqual(normal["settings"]["codeRoots"], [])
            self.assertFalse(normal["settings"]["includeCode"])
            code = latency.corpus_index_plan({"files": [{"file": "code.py", "modality": "code"}]}, corpus)
            self.assertEqual(code["settings"]["roots"], [])
            self.assertEqual(code["settings"]["codeRoots"], [str(corpus)])
            self.assertTrue(code["settings"]["includeCode"])

    def test_corpus_index_plan_rejects_empty_and_repeated_space_members(self):
        with tempfile.TemporaryDirectory(prefix="qcs-latency-invalid-plan-", dir=os.environ.get("QCOPILOTS_SEARCH_TEST_ROOT") or None) as temporary:
            corpus = Path(temporary)
            fixtures = [{"files": []},
                        {"files": [{"file": "same.txt", "modality": "text"},
                                   {"file": "same.txt", "modality": "text"}]},
                        {"files": [{"file": "same.py", "modality": "code"},
                                   {"file": "same.py", "modality": "code"}]},
                        {"files": [{"modality": "text"}]},
                        {"files": [{"file": "missing-modality.txt"}]},
                        {"files": ["not an object"]}]
            for semantic in fixtures:
                with self.subTest(semantic=semantic), self.assertRaises(RuntimeError):
                    latency.corpus_index_plan(semantic, corpus)

    def test_indexed_memberships_are_checked_read_only_and_keep_shared_paths(self):
        with tempfile.TemporaryDirectory(prefix="qcs-latency-db-", dir=os.environ.get("QCOPILOTS_SEARCH_TEST_ROOT") or None) as temporary:
            root = Path(temporary)
            profile, corpus = root / "profile", root / "corpus"
            profile.mkdir()
            corpus.mkdir()
            database = profile / "index.sqlite"
            memberships = [{"file": "shared.txt", "code": False}, {"file": "shared.txt", "code": True}]
            with contextlib.closing(sqlite3.connect(database)) as connection, connection:
                connection.execute("CREATE TABLE files(path TEXT NOT NULL, code INTEGER NOT NULL)")
                connection.executemany("INSERT INTO files(path,code) VALUES(?,?)",
                                       [(str(corpus / row["file"]), int(row["code"])) for row in memberships])
            before = (latency.sha256(database), database.stat().st_mtime_ns)
            with mock.patch("sqlite3.connect", wraps=sqlite3.connect) as connect:
                counts = latency.verify_indexed_memberships(database, profile, corpus, memberships)
            self.assertEqual(counts, {"normal": 1, "code": 1, "memberships": 2, "physicalFiles": 1})
            self.assertTrue(connect.called)
            for call in connect.call_args_list:
                self.assertIn("mode=ro", str(call.args[0]))
                self.assertTrue(call.kwargs.get("uri"))
            self.assertEqual((latency.sha256(database), database.stat().st_mtime_ns), before)
            self.assertFalse(Path(str(database) + "-journal").exists())

    def test_indexed_memberships_reject_same_counts_with_wrong_paths_or_spaces(self):
        with tempfile.TemporaryDirectory(prefix="qcs-latency-wrong-members-", dir=os.environ.get("QCOPILOTS_SEARCH_TEST_ROOT") or None) as temporary:
            root = Path(temporary)
            profile, corpus = root / "profile", root / "corpus"
            profile.mkdir()
            corpus.mkdir()
            expected = [{"file": "ordinary.txt", "code": False}, {"file": "code.py", "code": True}]
            cases = {"wrong-path": [("wrong.txt", 0), ("code.py", 1)],
                     "wrong-spaces": [("ordinary.txt", 1), ("code.py", 0)]}
            for name, rows in cases.items():
                with self.subTest(name=name):
                    database = profile / (name + ".sqlite")
                    with contextlib.closing(sqlite3.connect(database)) as connection, connection:
                        connection.execute("CREATE TABLE files(path TEXT NOT NULL, code INTEGER NOT NULL)")
                        connection.executemany("INSERT INTO files(path,code) VALUES(?,?)",
                                               [(str(corpus / file), code) for file, code in rows])
                    before = (latency.sha256(database), database.stat().st_mtime_ns)
                    with self.assertRaises(RuntimeError):
                        latency.verify_indexed_memberships(database, profile, corpus, expected)
                    self.assertEqual((latency.sha256(database), database.stat().st_mtime_ns), before)

    def test_indexed_memberships_reject_database_outside_the_dedicated_profile(self):
        with tempfile.TemporaryDirectory(prefix="qcs-latency-outside-db-", dir=os.environ.get("QCOPILOTS_SEARCH_TEST_ROOT") or None) as temporary:
            root = Path(temporary)
            profile, corpus = root / "profile", root / "corpus"
            profile.mkdir()
            corpus.mkdir()
            database = root / "outside.sqlite"
            with contextlib.closing(sqlite3.connect(database)) as connection, connection:
                connection.execute("CREATE TABLE files(path TEXT, code INTEGER)")
            with self.assertRaises(RuntimeError):
                latency.verify_indexed_memberships(database, profile, corpus, [{"file": "ordinary.txt", "code": False}])

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
            semantic = {"files": [{"file": "source.txt", "modality": modality,
                                    "sha256": latency.sha256(corpus / "source.txt")}
                                   for modality in ("text", "code")]}
            semantic_path = root / "semantic.json"
            latency.write_json(semantic_path, semantic)
            records, external = latency.capture_inputs(run, candidate_path, candidate, semantic_path, semantic, corpus,
                                                       ROOT / "qcs-test-latency-preparation.py", reserve_gib=0)
            names = {record["path"] for record in records}
            self.assertIn("inputs/corpus/source.txt", names)
            self.assertEqual(sum(record["path"] == "inputs/corpus/source.txt" for record in records), 1)
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
