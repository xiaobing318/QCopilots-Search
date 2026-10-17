#!/usr/bin/env python3
"""Synthetic recovery contracts, never a substitute for native release acceptance."""
from copy import deepcopy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import stat
import sys
import types
import unittest
from unittest import mock
import urllib.parse
import zipfile

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("qcs_release_test_fixtures", Path(__file__).with_name("qcs-test-release.py"))
fixtures = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixtures)
release = fixtures.release
freeze = release.module("qcs-release-freeze")
CANDIDATE, IDENTITY, VERSION, TAG = fixtures.CANDIDATE, fixtures.IDENTITY, fixtures.VERSION, fixtures.TAG
PLENTY = types.SimpleNamespace(free=100 * 1024**3)


class FakeFreezeCI(fixtures.FakeCI):
    def __init__(self):
        super().__init__()
        self.freeze_run = {"id": 456, "head_sha": CANDIDATE, "workflow_id": 88, "run_attempt": 1,
            "path": freeze.WORKFLOW_PATH, "repository": deepcopy(self.repository), "head_branch": "main",
            "event": "workflow_dispatch", "status": "completed", "conclusion": "failure"}
        self.freeze_workflow = {"id": 88, "path": freeze.WORKFLOW_PATH}
        self.freeze_attempts = {1: deepcopy(self.freeze_run)}
        first, last = "2026-01-01T03:00:00Z", "2026-01-01T03:10:00Z"
        self.freeze_jobs = [{"id": 9000, "run_id": 456, "run_attempt": 1, "head_sha": CANDIDATE,
            "head_branch": "main", "workflow_name": "qcs release", "name": freeze.JOB_NAME,
            "status": "completed", "conclusion": "failure", "created_at": first, "started_at": first,
            "completed_at": last, "runner_id": 81, "runner_name": "fixture-release", "labels": ["ubuntu-24.04"],
            "steps": [{"number": 1, "name": "Prepare release", "status": "completed", "conclusion": "success",
                       "started_at": first, "completed_at": "2026-01-01T03:02:00Z"},
                      {"number": 2, "name": freeze.UPLOAD_STEP, "status": "completed", "conclusion": "success",
                       "started_at": "2026-01-01T03:02:00Z", "completed_at": "2026-01-01T03:03:00Z"},
                      {"number": 3, "name": "Upload draft", "status": "completed", "conclusion": "failure",
                       "started_at": "2026-01-01T03:03:00Z", "completed_at": last}]}]
        self.freeze_artifacts = []
        self.before_download = None

    def archive(self, entries):
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, content in entries:
                archive.writestr(name, content)
        payload = data.getvalue()
        self.downloads[8000] = payload
        self.freeze_artifacts = [{"id": 8000, "name": freeze.artifact_name(CANDIDATE, "123", 1),
            "size_in_bytes": len(payload), "digest": "sha256:" + hashlib.sha256(payload).hexdigest(),
            "expired": False, "created_at": "2026-01-01T03:02:30Z",
            "workflow_run": {"id": 456, "head_sha": CANDIDATE, "head_branch": "main"}}]

    def copied_retry(self):
        self.freeze_run["run_attempt"] = 2
        self.freeze_run.update(status="in_progress", conclusion=None)
        self.freeze_attempts[2] = deepcopy(self.freeze_run)
        copied = deepcopy(self.freeze_jobs[0])
        copied.update(id=9001, run_attempt=2, created_at="2026-01-01T04:00:00Z")
        self.freeze_jobs.append(copied)

    def request(self, method, endpoint, data=None):
        prefix = "/repos/" + release.REPOSITORY
        response = None
        if endpoint == prefix + "/actions/runs/456":
            response = self.freeze_run
        elif endpoint == prefix + "/actions/workflows/88":
            response = self.freeze_workflow
        elif endpoint.startswith(prefix + "/actions/runs/456/attempts/"):
            response = self.freeze_attempts[int(endpoint.rsplit("/", 1)[1])]
        elif endpoint.startswith(prefix + "/actions/jobs/"):
            response = next(job for job in self.freeze_jobs if job["id"] == int(endpoint.rsplit("/", 1)[1]))
        else:
            for kind in ("jobs", "artifacts"):
                if endpoint.startswith(prefix + "/actions/runs/456/" + kind + "?"):
                    rows = getattr(self, "freeze_" + kind)
                    query = urllib.parse.parse_qs(urllib.parse.urlsplit(endpoint).query)
                    if kind == "jobs":
                        assert query["filter"] == ["all"]
                    page = int(query["page"][0])
                    response = {"total_count": len(rows), kind: rows[(page - 1) * 100:page * 100]}
        if response is not None:
            self.calls.append((method, endpoint, data))
            assert method == "GET", "Recovery must not write GitHub"
            return deepcopy(response)
        return super().request(method, endpoint, data)

    def download_artifact(self, artifact_id, destination, expected_digest, *, max_bytes):
        if self.before_download:
            self.before_download(artifact_id, destination)
        super().download_artifact(artifact_id, destination, expected_digest, max_bytes=max_bytes)


class RestoreTests(fixtures.ExternalTest):
    def setUp(self):
        super().setUp()
        self.api = FakeFreezeCI()
        self.output = self.root / "restored"
        source = self.file(f"sources/qcs-source-materials-{CANDIDATE[:12]}.zip", b"SYNTHETIC ORIGINAL SOURCE ZIP\n")
        for patch in (mock.patch.object(release, "identity_for", return_value=deepcopy(IDENTITY)),
                      mock.patch.object(release, "validate_sources"), mock.patch.object(freeze.shutil, "disk_usage", return_value=PLENTY)):
            patch.start()
            self.addCleanup(patch.stop)
        with mock.patch.object(release, "build_sources", return_value=source):
            manifest = release.prepare(release.SOURCE, CANDIDATE, "123", 1, self.root / "original", api=self.api)
        document = release.read_json(manifest)
        self.original = manifest.parent
        self.original_document = document
        self.entries = [(release.MANIFEST, manifest.read_bytes()),
                        ("assets/SHA256SUMS", (self.original / "assets/SHA256SUMS").read_bytes()),
                        ("assets/" + source.name, (self.original / "assets" / source.name).read_bytes())]
        self.api.archive(self.entries)
        self.api.download_calls.clear()
        self.api.calls.clear()

    def restore(self, **arguments):
        values = {"candidate": CANDIDATE, "run_id": "123", "attempt": 1, "tag": TAG,
                  "freeze_run_id": "456", "output": self.output, "api": self.api}
        values.update(arguments)
        return freeze.restore(release, release.SOURCE, **values)

    def assert_clean_failure(self):
        self.assertFalse(self.output.exists())
        self.assertFalse(list(self.root.glob(".qcs-release-restore-*")))
        self.assertTrue(all(method == "GET" for method, _, _ in self.api.calls))

    def test_failed_release_restores_twenty_one_raw_packages_and_original_three_bytes(self):
        with mock.patch.object(release, "build_sources", side_effect=AssertionError("Restore must never rebuild sources")):
            result = self.restore()
        self.assertEqual(result["restoredPackageCount"], 21)
        self.assertEqual(result["origin"]["sourceAttempt"], 1)
        self.assertEqual(result["origin"]["sourceJobId"], 9000)
        self.assertEqual(result["manifestPath"], str(self.output / release.MANIFEST))
        for name, content in self.entries:
            self.assertEqual((self.output / name).read_bytes(), content)
        self.assertEqual(len(self.api.download_calls), 22)
        self.assertEqual(self.api.download_calls[0], 8000)
        self.assertEqual(release.verify_freeze(self.output / release.MANIFEST, release.SOURCE), self.original_document)
        self.assertTrue(all(method == "GET" for method, _, _ in self.api.calls))
        self.assertFalse(list(self.root.glob(".qcs-release-restore-*")))

    def test_cancelled_after_successful_upload_is_recoverable(self):
        self.api.freeze_run["conclusion"] = "cancelled"
        self.api.freeze_attempts[1]["conclusion"] = "cancelled"
        self.api.freeze_jobs[0]["conclusion"] = "cancelled"
        self.api.freeze_jobs[0]["steps"][-1].update(conclusion="cancelled", started_at=None, completed_at=None)
        self.assertEqual(self.restore()["restoredPackageCount"], 21)

    def test_running_later_step_does_not_invalidate_successful_upload(self):
        self.api.freeze_run.update(status="in_progress", conclusion=None)
        self.api.freeze_attempts[1].update(status="in_progress", conclusion=None)
        self.api.freeze_jobs[0].update(status="in_progress", conclusion=None, completed_at=None)
        self.api.freeze_jobs[0]["steps"][-1].update(status="in_progress", conclusion=None, completed_at=None)
        self.assertEqual(self.restore()["origin"]["sourceJobId"], 9000)

    def test_copied_retry_uses_original_attempt_and_job(self):
        self.api.copied_retry()
        result = self.restore()
        self.assertEqual(result["origin"]["sourceAttempt"], 1)
        self.assertEqual(result["origin"]["sourceJobId"], 9000)

    def test_crlf_checksums_and_manifest_bytes_are_preserved(self):
        document = deepcopy(self.original_document)
        checksum = self.entries[1][1].replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
        for asset in document["assets"]:
            if asset["name"] == "SHA256SUMS":
                asset.update(bytes=len(checksum), sha256=hashlib.sha256(checksum).hexdigest())
        manifest = json.dumps(document, ensure_ascii=False, indent=1).encode() + b"\r\n"
        self.api.archive([(release.MANIFEST, manifest), ("assets/SHA256SUMS", checksum), self.entries[2]])
        self.restore()
        self.assertEqual((self.output / release.MANIFEST).read_bytes(), manifest)
        self.assertEqual((self.output / "assets/SHA256SUMS").read_bytes(), checksum)

    def test_retry_skipping_existing_upload_still_uses_original(self):
        self.api.copied_retry()
        retry = self.api.freeze_jobs[1]
        retry.update(created_at="2026-01-01T04:00:00Z", started_at="2026-01-01T04:00:00Z", completed_at=None)
        retry["steps"][1].update(status="completed", conclusion="skipped", started_at=None, completed_at=None)
        self.assertEqual(self.restore()["origin"]["sourceAttempt"], 1)

    def test_different_copied_execution_prefix_is_rejected(self):
        self.api.copied_retry()
        self.api.freeze_jobs[1]["steps"][0]["name"] = "A different execution"
        with self.assertRaisesRegex(ValueError, "copied job"):
            self.restore()
        self.assertEqual(self.api.download_calls, [])
        self.assert_clean_failure()

    def test_duplicate_original_or_same_attempt_is_rejected(self):
        self.api.copied_retry()
        self.api.freeze_jobs[1]["created_at"] = self.api.freeze_jobs[1]["started_at"]
        with self.assertRaisesRegex(ValueError, "unique original"):
            self.restore()
        self.assert_clean_failure()

    def test_freeze_repository_workflow_event_branch_sha_and_attempt_checked(self):
        cases = [("freeze_workflow", "path", ".github/workflows/other.yml"),
                 ("freeze_workflow", "id", 89), ("freeze_run", "event", "push"),
                 ("freeze_run", "head_branch", "feature"), ("freeze_run", "head_sha", "2" * 40),
                 ("freeze_run", "path", ".github/workflows/other.yml"),
                 ("freeze_run", "run_attempt", 0)]
        for scope, key, value in cases:
            with self.subTest(scope=scope, key=key):
                target = getattr(self.api, scope)
                previous = deepcopy(target[key])
                target[key] = value
                with self.assertRaises(ValueError):
                    self.restore()
                target[key] = previous
                self.assertEqual(self.api.download_calls, [])
                self.assert_clean_failure()

    def test_source_attempt_and_job_attempt_must_match(self):
        self.api.freeze_attempts[1]["run_attempt"] = 2
        with self.assertRaisesRegex(ValueError, "source attempt"):
            self.restore()
        self.assert_clean_failure()

    def test_wrong_job_attempt_or_upload_step_is_rejected_before_download(self):
        for key, value in (("name", "Not Release"), ("run_attempt", 2), ("head_branch", "feature")):
            with self.subTest(key=key):
                previous = self.api.freeze_jobs[0][key]
                self.api.freeze_jobs[0][key] = value
                with self.assertRaises(ValueError):
                    self.restore()
                self.api.freeze_jobs[0][key] = previous
        self.api.freeze_jobs[0]["steps"][1]["conclusion"] = "failure"
        with self.assertRaisesRegex(ValueError, "unique original"):
            self.restore()
        self.assertEqual(self.api.download_calls, [])
        self.assert_clean_failure()

    def test_expired_wrong_digest_wrong_run_and_outside_upload_time_rejected(self):
        cases = [("expired", True), ("digest", "sha256:bad"), ("size_in_bytes", freeze.MAX_FREEZE_BYTES + 1),
                 ("workflow_run", {"id": 999, "head_sha": CANDIDATE}),
                 ("created_at", "2026-01-01T03:04:00Z")]
        for key, value in cases:
            with self.subTest(key=key):
                previous = deepcopy(self.api.freeze_artifacts[0][key])
                self.api.freeze_artifacts[0][key] = value
                with self.assertRaises(ValueError):
                    self.restore()
                self.api.freeze_artifacts[0][key] = previous
                self.assertEqual(self.api.download_calls, [])
                self.assert_clean_failure()

    def test_downloaded_freeze_hash_and_exact_size_checked(self):
        self.api.downloads[8000] += b"tamper"
        with self.assertRaisesRegex(ValueError, "archive size or SHA-256"):
            self.restore()
        self.assertEqual(self.api.download_calls, [8000])
        self.assert_clean_failure()

    def test_archive_rejects_extra_duplicate_traversal_directory_and_link_entries(self):
        bad_names = ["extra.txt", "../escape", "assets/../escape", "assets\\escape", "C:/escape", "assets/"]
        symlink = zipfile.ZipInfo(self.entries[2][0])
        symlink.create_system = 3
        symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
        cases = [self.entries + [(name, b"extra")] for name in bad_names]
        cases.append(self.entries[:2] + [(self.entries[0][0], b"duplicate")])
        cases.append(self.entries[:2] + [(symlink, b"elsewhere")])
        for index, entries in enumerate(cases):
            with self.subTest(index=index):
                with mock.patch("warnings.warn"):
                    self.api.archive(entries)
                with self.assertRaises(ValueError):
                    self.restore()
                self.assert_clean_failure()

    def test_oversized_entry_is_rejected_before_extraction(self):
        self.api.archive([(name, b"X" * (freeze.MAX_CHECKSUM_BYTES + 1) if name.endswith("SHA256SUMS") else data)
                          for name, data in self.entries])
        with self.assertRaisesRegex(ValueError, "oversized"):
            self.restore()
        self.assert_clean_failure()

    def test_manifest_identity_schema_version_tag_run_and_attempt_rejected(self):
        for key, value in (("schemaVersion", 3), ("version", "999.0.0"), ("runId", "999"),
                           ("requestedAttempt", 2), ("identity", {**IDENTITY, "candidateHead": "2" * 40})):
            with self.subTest(key=key):
                document = deepcopy(self.original_document)
                document[key] = value
                self.api.archive([(name, json.dumps(document).encode() if name == release.MANIFEST else data)
                                  for name, data in self.entries])
                with self.assertRaisesRegex(ValueError, "manifest differs"):
                    self.restore()
                self.assert_clean_failure()

    def test_source_and_checksums_must_match_original_manifest(self):
        for index in (1, 2):
            with self.subTest(index=index):
                entries = list(self.entries)
                entries[index] = (entries[index][0], entries[index][1] + b"tamper")
                self.api.archive(entries)
                with self.assertRaisesRegex(ValueError, "original manifest bytes"):
                    self.restore()
                self.assert_clean_failure()

    def test_native_selection_change_or_package_tamper_rejected(self):
        self.api.downloads[1010] += b"tamper"
        with self.assertRaisesRegex(ValueError, "Restored CI package"):
            self.restore()
        self.assert_clean_failure()

    def test_source_validation_failure_prevents_package_download_and_output(self):
        with mock.patch.object(release, "validate_sources", side_effect=ValueError("Corresponding source verification failed")) as validate:
            with self.assertRaisesRegex(ValueError, "Corresponding source verification"):
                self.restore()
            self.assertEqual(validate.call_count, 1)
        self.assertEqual(self.api.download_calls, [8000])
        self.assert_clean_failure()

    def test_wrong_package_manifest_cannot_replace_remote_artifact(self):
        document = deepcopy(self.original_document)
        selected = next(iter(document["configurations"].values()))
        selected["artifacts"][0]["artifactId"] = 9999
        self.api.archive([(name, json.dumps(document).encode() if name == release.MANIFEST else data)
                          for name, data in self.entries])
        with self.assertRaisesRegex(ValueError, "selection differs"):
            self.restore()
        self.assertEqual(self.api.download_calls, [8000])
        self.assert_clean_failure()

    def test_outer_zip_removed_before_package_download_and_exact_disk_budget(self):
        required = sum(item["bytes"] for item in self.original_document["assets"] if item["role"] == "user-package")
        def inspect(identifier, destination):
            if identifier != 8000:
                self.assertFalse((destination.parents[2] / "freeze.zip").exists())
        self.api.before_download = inspect
        with mock.patch.object(freeze.shutil, "disk_usage", side_effect=[PLENTY, types.SimpleNamespace(free=required + freeze.RESERVE_BYTES)]):
            self.assertEqual(self.restore()["restoredPackageBytes"], required)

    def test_insufficient_package_space_preserves_three_gib_and_never_downloads_packages(self):
        required = sum(item["bytes"] for item in self.original_document["assets"] if item["role"] == "user-package")
        with mock.patch.object(freeze.shutil, "disk_usage", side_effect=[PLENTY, types.SimpleNamespace(free=required + freeze.RESERVE_BYTES - 1)]):
            with self.assertRaisesRegex(ValueError, "3 GiB"):
                self.restore()
        self.assertEqual(self.api.download_calls, [8000])
        self.assert_clean_failure()

    def test_main_change_native_failure_and_existing_output_rejected_before_download(self):
        self.api.main_sha = "9" * 40
        with self.assertRaisesRegex(ValueError, "Remote main"):
            self.restore()
        self.api.main_sha = CANDIDATE
        self.api.run["conclusion"] = "failure"
        with self.assertRaisesRegex(ValueError, "successfully"):
            self.restore()
        self.api.run["conclusion"] = "success"
        self.output.mkdir()
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.restore()
        self.assertEqual(self.api.download_calls, [])

    def test_remote_change_at_final_check_rejects_final_directory(self):
        original = release.verify_ci_packages
        def move_main(api, document):
            original(api, document)
            api.main_sha = "9" * 40
        with mock.patch.object(release, "verify_ci_packages", side_effect=move_main):
            with self.assertRaisesRegex(ValueError, "Remote main"):
                self.restore()
        self.assert_clean_failure()

    def test_artifact_origin_change_during_download_rejects_final_directory(self):
        def change_origin(identifier, destination):
            if identifier != 8000:
                self.api.freeze_artifacts[0]["created_at"] = "2026-01-01T03:02:31Z"
        self.api.before_download = change_origin
        with self.assertRaisesRegex(ValueError, "provenance changed"):
            self.restore()
        self.assert_clean_failure()

    def test_original_job_details_rechecked_before_download(self):
        original = self.api.request
        def change_job(method, endpoint, data=None):
            result = original(method, endpoint, data)
            if endpoint.endswith("/actions/jobs/9000"):
                result["steps"][0]["name"] = "Changed execution prefix"
            return result
        with mock.patch.object(self.api, "request", side_effect=change_job):
            with self.assertRaisesRegex(ValueError, "original job changed"):
                self.restore()
        self.assertEqual(self.api.download_calls, [])
        self.assert_clean_failure()

    def test_artifact_removed_or_duplicated_is_not_recoverable(self):
        artifact = deepcopy(self.api.freeze_artifacts[0])
        for values in ([], [artifact, {**artifact, "id": 8001}]):
            with self.subTest(count=len(values)):
                self.api.freeze_artifacts = values
                with self.assertRaisesRegex(ValueError, "exactly one immutable artifact"):
                    self.restore()
                self.assert_clean_failure()


if __name__ == "__main__":
    unittest.main()
