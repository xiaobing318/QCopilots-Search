#!/usr/bin/env python3
"""Exercise CI artifact selection using synthetic API history, never a CI run."""
import argparse
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

sys.dont_write_bytecode = True
SCRIPT = Path(__file__).resolve().with_name("qcs-artifact-selection.py")
spec = importlib.util.spec_from_file_location("qcs_artifact_selection_tests", SCRIPT)
selection = importlib.util.module_from_spec(spec)
spec.loader.exec_module(selection)
HEAD = "a" * 40
RUN = "1234567"
REPOSITORY = "fixture-owner/QCopilots-Search"


def iso(value):
    return value.isoformat().replace("+00:00", "Z")


def job(platform, identifier, attempt=1, hour=1):
    start = datetime(2026, 1, 1, hour, 0, tzinfo=timezone.utc)
    return {"id": identifier, "run_id": int(RUN), "run_attempt": attempt,
            "head_sha": HEAD, "head_branch": "fixture-branch", "workflow_name": "qcs native acceptance",
            "name": "qcs " + platform, "status": "completed", "conclusion": "success",
            "created_at": iso(start - timedelta(seconds=2)), "started_at": iso(start),
            "completed_at": iso(start + timedelta(minutes=10)), "runner_id": 100 + identifier,
            "runner_name": "fixture-runner-" + str(identifier), "runner_group_id": 0,
            "runner_group_name": "GitHub Actions", "labels": ["fixture-native-runner"],
            "steps": [{"number": 1, "name": "Build", "status": "completed", "conclusion": "success",
                       "started_at": iso(start + timedelta(minutes=1)), "completed_at": iso(start + timedelta(minutes=2))},
                      {"number": 2, "name": "Upload packages", "status": "completed", "conclusion": "success",
                       "started_at": iso(start + timedelta(minutes=9)), "completed_at": iso(start + timedelta(minutes=10))}]}


def artifact(source, identifier):
    platform = source["name"].removeprefix("qcs ")
    return {"id": identifier,
            "name": selection.artifact_name(platform, HEAD, source["run_attempt"]),
            "expired": False, "digest": "sha256:" + f"{identifier:064x}",
            "created_at": source["steps"][-1]["started_at"],
            "workflow_run": {"id": int(RUN), "head_sha": HEAD}}


def clone(source, identifier, attempt):
    copied = deepcopy(source)
    copied.update(id=identifier, run_attempt=attempt, runner_group_id=None,
                  created_at=iso(datetime(2026, 1, 1, attempt + 1, 0, tzinfo=timezone.utc)),
                  node_id="copied-node", url="copied-url", html_url="copied-html", check_run_url="copied-check")
    return copied


class ArtifactSelectionTests(unittest.TestCase):
    def setUp(self):
        configured = os.environ.get("QCOPILOTS_SEARCH_TEST_ROOT") or None
        if configured:
            Path(configured).mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix="qcs-artifact-selection-", dir=configured)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.jobs = [job(platform, index + 1) for index, platform in enumerate(selection.CONFIGURATIONS)]
        self.artifacts = [artifact(source, index + 1001) for index, source in enumerate(self.jobs)]

    def inspect(self, attempt=1):
        return selection.select(REPOSITORY, RUN, HEAD, attempt, self.jobs, self.artifacts)

    def partial_retry(self, *, cloned=True):
        original = list(self.jobs)
        retried = job("windows-x64-cpu", 101, attempt=2, hour=2)
        self.jobs.append(retried)
        self.artifacts.append(artifact(retried, 2001))
        if cloned:
            self.jobs.extend(clone(source, index + 102, 2) for index, source in enumerate(original[1:]))
        return retried

    def pages(self, rows, kind):
        return [{"total_count": len(rows), kind: rows[index:index + selection.PAGE_SIZE]}
                for index in range(0, max(1, len(rows)), selection.PAGE_SIZE)]

    def write_snapshot(self, directory=None, attempt=1):
        directory = directory or self.root / "evidence"
        (directory / "selection-inputs").mkdir(parents=True)
        document = self.inspect(attempt)
        document.update(files=[], collectionErrors=[])
        for kind, rows in (("jobs", self.jobs), ("artifacts", self.artifacts)):
            for number, page in enumerate(self.pages(rows, kind), 1):
                data = json.dumps(page).encode("utf-8")
                relative = f"selection-inputs/{kind}-{number:04d}.json"
                (directory / relative).write_bytes(data)
                document["files"].append({"kind": kind, "page": number, "path": relative,
                                          "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                                          "url": selection.page_url(REPOSITORY, RUN, kind, number)})
        path = directory / "selection.json"
        self.write_document(path, document)
        return path

    def write_document(self, path, document):
        path.write_text(json.dumps(document), encoding="utf-8")

    def test_first_attempt_selects_exact_ids_and_digest_for_all_platforms(self):
        result = self.inspect()
        self.assertTrue(result["passed"], result["errors"])
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["excluded"], [])
        for index, platform in enumerate(selection.CONFIGURATIONS):
            entry = result["selected"][platform]
            self.assertEqual(entry["sourceAttempt"], 1)
            self.assertEqual(entry["sourceJobId"], index + 1)
            self.assertEqual(entry["observedJobId"], index + 1)
            self.assertEqual(entry["artifactId"], index + 1001)
            self.assertEqual(entry["artifactDigest"], self.artifacts[index]["digest"])

    def test_shared_manifest_defines_all_eleven_configurations(self):
        manifest = json.loads((SCRIPT.parents[2] / "config/build-variants.json").read_text(encoding="utf-8"))
        self.assertEqual(selection.CONFIGURATIONS, tuple(row["id"] for row in manifest["variants"]))
        self.assertEqual(len(selection.CONFIGURATIONS), 11)
        self.assertEqual(selection.PLATFORMS, selection.CONFIGURATIONS)
        self.assertFalse(any(identifier.startswith("macos-x64") for identifier in selection.CONFIGURATIONS))

    def test_only_packages_artifact_names_can_satisfy_gate(self):
        expected = f"qcs-packages-windows-x64-cpu-{HEAD}-attempt1"
        self.assertEqual(self.artifacts[0]["name"], expected)
        self.artifacts[0]["name"] = expected.replace("qcs-packages-", "qcs-native-")
        self.assertFalse(self.inspect()["passed"])
        self.artifacts[0]["name"] = expected
        result = self.inspect()
        self.assertTrue(result["passed"], result["errors"])
        self.assertEqual(result["schemaVersion"], 2)
        self.assertEqual(set(result), {"schemaVersion", "kind", "repository", "runId", "aggregateAttempt",
                                      "candidateHead", "passed", "errors", "selected", "excluded"})

    def test_every_configuration_requires_its_job_and_package_artifact(self):
        original_jobs = list(self.jobs)
        original_artifacts = list(self.artifacts)
        for index, configuration in enumerate(selection.CONFIGURATIONS):
            for missing in ("job", "artifact"):
                with self.subTest(configuration=configuration, missing=missing):
                    self.jobs = [row for number, row in enumerate(original_jobs)
                                 if missing != "job" or number != index]
                    self.artifacts = [row for number, row in enumerate(original_artifacts)
                                      if missing != "artifact" or number != index]
                    result = self.inspect()
                    self.assertFalse(result["passed"])
                    self.assertNotIn(configuration, result["selected"])
                    self.assertEqual(len(result["selected"]), len(selection.CONFIGURATIONS) - 1)

    def test_partial_retry_reuses_original_execution_of_copied_jobs(self):
        self.partial_retry()
        result = self.inspect(2)
        self.assertTrue(result["passed"], result["errors"])
        self.assertEqual(result["selected"]["windows-x64-cpu"]["sourceAttempt"], 2)
        self.assertEqual(result["selected"]["windows-x64-cpu"]["artifactId"], 2001)
        for identifier, platform in enumerate(selection.CONFIGURATIONS[1:], 2):
            entry = result["selected"][platform]
            self.assertEqual(entry["sourceAttempt"], 1)
            self.assertEqual(entry["sourceJobId"], identifier)
            self.assertNotEqual(entry["observedJobId"], identifier)
        self.assertEqual([item["artifactId"] for item in result["excluded"]], [1001])

    def test_aggregate_only_retry_and_chained_copies_keep_true_source_attempt(self):
        self.partial_retry()
        latest = [max((row for row in self.jobs if row["name"] == "qcs " + platform),
                      key=lambda row: row["run_attempt"]) for platform in selection.CONFIGURATIONS]
        self.jobs.extend(clone(source, index + 201, 3) for index, source in enumerate(latest))
        result = self.inspect(3)
        self.assertTrue(result["passed"], result["errors"])
        self.assertEqual([result["selected"][platform]["sourceAttempt"] for platform in selection.CONFIGURATIONS],
                         [2] + [1] * (len(selection.CONFIGURATIONS) - 1))

    def test_old_success_never_replaces_failed_or_running_latest_job(self):
        retried = self.partial_retry()
        for status, conclusion in (("completed", "failure"), ("in_progress", None), ("completed", "cancelled")):
            with self.subTest(status=status, conclusion=conclusion):
                retried.update(status=status, conclusion=conclusion)
                result = self.inspect(2)
                self.assertFalse(result["passed"])
                self.assertNotIn("windows-x64-cpu", result["selected"])
                self.assertTrue(any("Earlier successful artifacts cannot replace" in error for error in result["errors"]))

    def test_successful_latest_job_without_its_artifact_cannot_fall_back(self):
        self.partial_retry()
        self.artifacts.pop()
        result = self.inspect(2)
        self.assertFalse(result["passed"])
        self.assertNotIn("windows-x64-cpu", result["selected"])
        self.assertTrue(any("attempt2" in error for error in result["errors"]))

    def test_a_copy_cannot_borrow_an_original_with_different_steps(self):
        self.partial_retry()
        self.jobs[-1]["steps"][0]["completed_at"] = "2026-01-01T01:02:01Z"
        result = self.inspect(2)
        self.assertFalse(result["passed"])
        self.assertTrue(any("unique original execution" in error for error in result["errors"]))

    def test_new_execution_on_same_runner_uses_new_attempt(self):
        retried = self.partial_retry()
        retried["runner_id"] = self.jobs[0]["runner_id"]
        retried["runner_name"] = self.jobs[0]["runner_name"]
        result = self.inspect(2)
        self.assertTrue(result["passed"], result["errors"])
        self.assertEqual(result["selected"]["windows-x64-cpu"]["sourceAttempt"], 2)

    def test_missing_current_attempt_observations_cannot_reuse_old_success(self):
        for attempt in (2, 999):
            with self.subTest(attempt=attempt):
                result = self.inspect(attempt)
                self.assertFalse(result["passed"])
                self.assertEqual(result["selected"], {})
                self.assertEqual(len(result["errors"]), len(selection.CONFIGURATIONS))
                self.assertTrue(all("Current aggregate attempt" in error for error in result["errors"]))
        self.partial_retry()
        self.jobs = [row for row in self.jobs if not (row["name"] == "qcs linux-x64-cpu" and row["run_attempt"] == 2)]
        result = self.inspect(2)
        self.assertFalse(result["passed"])
        self.assertNotIn("linux-x64-cpu", result["selected"])

    def test_current_copy_cannot_bypass_a_newer_real_execution(self):
        retried = self.partial_retry()
        originals = self.jobs[:len(selection.CONFIGURATIONS)]
        self.jobs.extend(clone(source, index + 201, 3) for index, source in enumerate(originals))
        for conclusion in ("failure", "success"):
            with self.subTest(conclusion=conclusion):
                retried["conclusion"] = conclusion
                result = self.inspect(3)
                self.assertFalse(result["passed"])
                self.assertNotIn("windows-x64-cpu", result["selected"])
                self.assertTrue(any("bypasses a newer actual execution" in error for error in result["errors"]), result["errors"])

    def test_new_successful_execution_after_failure_can_supply_current_copy(self):
        retried = self.partial_retry()
        retried["conclusion"] = "failure"
        recovered = job("windows-x64-cpu", 201, attempt=3, hour=3)
        self.jobs.append(recovered)
        self.artifacts.append(artifact(recovered, 3001))
        copied = [clone(source, index + 202, 3)
                  for index, source in enumerate(self.jobs[1:len(selection.CONFIGURATIONS)])]
        self.jobs.extend(copied)
        current = [recovered, *copied]
        self.jobs.extend(clone(source, index + 301, 4) for index, source in enumerate(current))
        result = self.inspect(4)
        self.assertTrue(result["passed"], result["errors"])
        self.assertEqual(result["selected"]["windows-x64-cpu"]["sourceAttempt"], 3)
        self.assertEqual(result["selected"]["windows-x64-cpu"]["artifactId"], 3001)

    def test_copy_without_original_and_ambiguous_same_attempt_jobs_fail(self):
        self.partial_retry()
        self.jobs = [row for row in self.jobs if row["id"] != 2]
        self.assertFalse(self.inspect(2)["passed"])
        duplicate = deepcopy(self.jobs[-1])
        duplicate["id"] = 999
        self.jobs.append(duplicate)
        result = self.inspect(2)
        self.assertTrue(any("ambiguous" in error for error in result["errors"]))

    def test_incomplete_execution_fingerprint_is_not_accepted(self):
        original = deepcopy(self.jobs[0])
        for field in ("started_at", "completed_at", "runner_id", "steps"):
            with self.subTest(field=field):
                self.jobs[0] = deepcopy(original)
                del self.jobs[0][field]
                self.assertFalse(self.inspect()["passed"])

    def test_historical_source_attempt_with_two_jobs_is_ambiguous(self):
        self.partial_retry()
        extra = deepcopy(self.jobs[1])
        extra.update(id=909, runner_id=999, runner_name="another-native-runner")
        self.jobs.append(extra)
        result = self.inspect(2)
        self.assertFalse(result["passed"])
        self.assertTrue(any("history is ambiguous" in error for error in result["errors"]))

    def test_creation_inside_execution_does_not_count_as_a_copy(self):
        self.jobs[0]["created_at"] = "2026-01-01T01:05:00Z"
        self.assertFalse(self.inspect()["passed"])

    def test_future_native_attempt_rejects_stale_aggregate_snapshot(self):
        retried = self.partial_retry()
        for status, conclusion in (("completed", "success"), ("completed", "failure"), ("in_progress", None)):
            with self.subTest(status=status, conclusion=conclusion):
                retried.update(status=status, conclusion=conclusion)
                result = self.inspect(1)
                self.assertFalse(result["passed"])
                self.assertEqual(result["selected"], {})
                self.assertTrue(any("newer than the requested aggregate attempt" in error for error in result["errors"]))

    def test_artifact_run_candidate_expiry_digest_and_execution_time_are_strict(self):
        original = deepcopy(self.artifacts[0])
        mutations = [{"expired": True}, {"digest": "sha256:abcd"}, {"digest": None},
                     {"workflow_run": {"id": 999, "head_sha": HEAD}},
                     {"workflow_run": {"id": int(RUN), "head_sha": "b" * 40}},
                     {"created_at": "2026-01-01T04:00:00Z"}]
        for change in mutations:
            with self.subTest(change=change):
                self.artifacts[0] = dict(deepcopy(original), **change)
                self.assertFalse(self.inspect()["passed"])

    def test_legacy_artifact_is_preserved_as_excluded_but_cannot_satisfy_gate(self):
        self.artifacts[0]["name"] = self.artifacts[0]["name"].removesuffix("-attempt1")
        result = self.inspect()
        self.assertFalse(result["passed"])
        self.assertTrue(any(item["artifactId"] == 1001 for item in result["excluded"]))

    def test_duplicate_artifact_names_and_duplicate_api_ids_are_rejected(self):
        duplicate = deepcopy(self.artifacts[0])
        duplicate["id"] = 9000
        self.artifacts.append(duplicate)
        self.assertFalse(self.inspect()["passed"])
        duplicate["id"] = 1001
        result = self.inspect()
        self.assertTrue(any("duplicate IDs" in error for error in result["errors"]))

    def test_load_recomputes_selection_and_allows_complete_tree_relocation(self):
        self.partial_retry()
        path = self.write_snapshot(attempt=2)
        first = selection.load_selection(path, HEAD, RUN)
        moved = self.root / "moved"
        shutil.move(str(path.parent), moved)
        second = selection.load_selection(moved / "selection.json", HEAD, RUN)
        self.assertEqual(first, second)

    def test_load_rejects_changed_selection_even_when_snapshots_are_unchanged(self):
        path = self.write_snapshot()
        document = json.loads(path.read_text(encoding="utf-8"))
        document["selected"]["windows-x64-cpu"]["sourceAttempt"] = 7
        self.write_document(path, document)
        with self.assertRaisesRegex(ValueError, "preserved API execution history"):
            selection.load_selection(path)

    def test_load_rejects_incompatible_schema_versions(self):
        path = self.write_snapshot()
        document = json.loads(path.read_text(encoding="utf-8"))
        for version in (1, 3, True, 2.0):
            with self.subTest(version=version):
                document["schemaVersion"] = version
                self.write_document(path, document)
                with self.assertRaisesRegex(ValueError, "Unsupported artifact selection document"):
                    selection.load_selection(path)

    def test_load_rejects_boolean_or_float_substitution_for_integer_identities(self):
        path = self.write_snapshot()
        original = json.loads(path.read_text(encoding="utf-8"))
        for key, value in (("sourceJobId", True), ("sourceAttempt", True), ("artifactId", 1001.0)):
            with self.subTest(key=key):
                document = deepcopy(original)
                document["selected"]["windows-x64-cpu"][key] = value
                self.write_document(path, document)
                with self.assertRaisesRegex(ValueError, "preserved API execution history"):
                    selection.load_selection(path)

    def test_load_rejects_changed_snapshot_and_unexpected_requested_identity(self):
        path = self.write_snapshot()
        for kwargs in ({"expected_head": "b" * 40}, {"expected_run": "9"}):
            with self.subTest(kwargs=kwargs), self.assertRaisesRegex(ValueError, "requested identity"):
                selection.load_selection(path, **kwargs)
        data = path.parent / "selection-inputs/jobs-0001.json"
        data.write_bytes(data.read_bytes() + b" ")
        with self.assertRaisesRegex(ValueError, "SHA-256 changed"):
            selection.load_selection(path)

    def test_load_rejects_snapshot_escape_and_missing_page(self):
        path = self.write_snapshot()
        original = json.loads(path.read_text(encoding="utf-8"))
        for change in ("escape", "missing"):
            with self.subTest(change=change):
                document = deepcopy(original)
                if change == "escape":
                    document["files"][0]["path"] = "../jobs.json"
                else:
                    document["files"].pop()
                self.write_document(path, document)
                with self.assertRaises(ValueError):
                    selection.load_selection(path)

    def test_complete_pagination_includes_entries_past_first_hundred(self):
        self.jobs = [job("unrelated", index + 100) for index in range(105)] + self.jobs
        path = self.write_snapshot()
        self.assertTrue(selection.load_selection(path)["passed"])

    def test_pagination_rejects_missing_extra_duplicate_and_changed_total(self):
        rows = [job("unrelated", index + 100) for index in range(105)]
        original = self.pages(rows, "jobs")
        for change in ("missing", "extra", "duplicate", "count", "short"):
            with self.subTest(change=change):
                pages = deepcopy(original)
                if change == "missing":
                    pages.pop()
                elif change == "extra":
                    pages.append({"total_count": 105, "jobs": []})
                elif change == "duplicate":
                    pages[1]["jobs"][0] = pages[0]["jobs"][0]
                elif change == "count":
                    pages[1]["total_count"] = 106
                else:
                    pages[0]["jobs"].pop()
                with self.assertRaises(ValueError):
                    selection.unpack_pages(pages, "jobs")

    def test_collector_preserves_raw_pages_and_emits_only_exact_artifact_ids(self):
        args = argparse.Namespace(output_dir=self.root / "collected", repository=REPOSITORY,
                                  run_id=RUN, candidate_head=HEAD, attempt=1)
        output = self.root / "github-output"
        self.jobs = [job("unrelated", index + 100) for index in range(105)] + self.jobs
        calls = []

        def fetch(url, token):
            self.assertEqual(token, "synthetic-token-never-log")
            calls.append(url)
            parsed = urlparse(url)
            kind = parsed.path.rsplit("/", 1)[1]
            query = parse_qs(parsed.query)
            page = int(query["page"][0])
            if kind == "jobs":
                self.assertEqual(query["filter"], ["all"])
            rows = self.jobs if kind == "jobs" else self.artifacts
            return json.dumps(self.pages(rows, kind)[page - 1]).encode("utf-8")

        with patch.dict(os.environ, {"GITHUB_TOKEN": "synthetic-token-never-log", "GITHUB_OUTPUT": str(output)}), \
                patch.object(selection, "fetch_page", side_effect=fetch):
            path = selection.collect(args)
        document = selection.load_selection(path)
        self.assertEqual(len(calls), 3)
        self.assertEqual(len(document["files"]), 3)
        expected_ids = ",".join(str(1001 + index) for index in range(len(selection.CONFIGURATIONS)))
        self.assertEqual(output.read_text(encoding="utf-8"), "artifact_ids=" + expected_ids + "\n")
        self.assertNotIn("synthetic-token-never-log", path.read_text(encoding="utf-8"))

    def test_collector_failure_preserves_report_and_never_emits_artifact_ids(self):
        args = argparse.Namespace(output_dir=self.root / "failed", repository=REPOSITORY,
                                  run_id=RUN, candidate_head=HEAD, attempt=1)
        output = self.root / "github-output"
        with patch.dict(os.environ, {"GITHUB_TOKEN": "synthetic-token", "GITHUB_OUTPUT": str(output)}), \
                patch.object(selection, "fetch_page", side_effect=ValueError("synthetic API unavailable")), \
                self.assertRaisesRegex(ValueError, "Preserved report"):
            selection.collect(args)
        path = args.output_dir / "selection.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        self.assertFalse(document["passed"])
        self.assertIn("synthetic API unavailable", document["collectionErrors"])
        self.assertFalse(output.exists())
        with self.assertRaises(ValueError):
            selection.load_selection(path)

    def test_collector_non_object_api_reply_still_preserves_failure_report(self):
        args = argparse.Namespace(output_dir=self.root / "malformed", repository=REPOSITORY,
                                  run_id=RUN, candidate_head=HEAD, attempt=1)
        with patch.dict(os.environ, {"GITHUB_TOKEN": "synthetic-token"}), \
                patch.object(selection, "fetch_page", return_value=b"[]"), \
                self.assertRaisesRegex(ValueError, "Preserved report"):
            selection.collect(args)
        document = json.loads((args.output_dir / "selection.json").read_text(encoding="utf-8"))
        self.assertFalse(document["passed"])
        self.assertEqual(len(document["files"]), 1)
        self.assertIn("not an object", document["collectionErrors"][0])

    def test_malformed_token_and_header_errors_never_echo_credentials(self):
        for token in ("synthetic\nNOT-A-TOKEN", "synthetic\tNOT-A-TOKEN", "synthetic 中文 token"):
            with self.subTest(token=repr(token)), patch.object(selection.urllib.request, "build_opener") as opener:
                with self.assertRaises(ValueError) as error:
                    selection.fetch_page("https://api.github.com/repos/fixture/repo", token)
                self.assertNotIn("synthetic", str(error.exception))
                opener.assert_not_called()
        with patch.object(selection.urllib.request, "Request", side_effect=ValueError("synthetic-token-secret")):
            with self.assertRaises(ValueError) as error:
                selection.fetch_page("https://api.github.com/repos/fixture/repo", "synthetic-token-secret")
            self.assertNotIn("synthetic-token-secret", str(error.exception))


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    unittest.main(verbosity=2)
