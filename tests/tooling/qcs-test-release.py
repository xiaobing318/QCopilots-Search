#!/usr/bin/env python3
"""Release package and upload contracts. Synthetic fixtures never represent release acceptance."""
from copy import deepcopy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock
import urllib.error
import zipfile

sys.dont_write_bytecode = True
spec = importlib.util.spec_from_file_location("qcs_release", Path(__file__).resolve().parents[2] / "tools/qcs-release.py")
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)
VERSION = release.project_version(release.SOURCE)
TAG = "v" + VERSION
CANDIDATE = "1" * 40
IDENTITY = {"candidateHead": CANDIDATE, "checkoutCommit": CANDIDATE, "sourceTree": "2" * 40,
            "workingTreeModified": False}


class ExternalTest(unittest.TestCase):
    def setUp(self):
        parent = os.environ.get("QCOPILOTS_SEARCH_TEST_ROOT")
        if parent:
            root = Path(parent).resolve()
            if root == release.SOURCE or release.SOURCE in root.parents:
                raise RuntimeError("Test root must be outside the source checkout")
            root.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="qcs-release-test-", dir=parent)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def file(self, relative, content=b"SYNTHETIC TOOL TEST DATA\n"):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path


class FakeCI:
    """Synthetic GitHub JSON and raw package bytes exercise the real eleven-job selector."""
    def __init__(self):
        self.repository = {"id": 42, "full_name": release.REPOSITORY, "archived": False}
        self.workflow = {"id": 71, "path": release.WORKFLOW_PATH}
        self.run = {"id": 123, "head_sha": CANDIDATE, "workflow_id": 71, "run_attempt": 1,
                    "path": release.WORKFLOW_PATH, "repository": deepcopy(self.repository),
                    "status": "completed", "conclusion": "success"}
        self.attempt = deepcopy(self.run)
        self.jobs, self.artifacts, self.downloads, self.calls = [], [], {}, []
        self.download_calls = []
        self.main_sha = CANDIDATE
        for index, configuration in enumerate(release.variants(), 1):
            self.add_job(configuration, index, 1)

    def add_job(self, configuration, identifier, attempt):
        hour = attempt
        first = f"2026-01-01T{hour:02d}:00:00Z"
        last = f"2026-01-01T{hour:02d}:10:00Z"
        job = {"id": identifier, "run_id": 123, "run_attempt": attempt, "head_sha": CANDIDATE,
               "head_branch": "fixture-branch", "workflow_name": "qcs native acceptance", "name": "qcs " + configuration,
               "status": "completed", "conclusion": "success", "created_at": first, "started_at": first,
               "completed_at": last, "runner_id": identifier + 100, "runner_name": "fixture-" + str(identifier),
               "labels": ["fixture-runner"], "steps": [{"number": 1, "name": "Build, test and upload packages",
               "status": "completed", "conclusion": "success", "started_at": first, "completed_at": last}]}
        self.jobs.append(job)
        for index, name in enumerate(sorted(release.package_names(configuration, release.project_version(release.SOURCE)))):
            content = b"SYNTHETIC USER PACKAGE: " + name.encode() + str(attempt).encode()
            artifact_id = identifier * 10 + index + 1000
            self.downloads[artifact_id] = content
            # upload-artifact overwrite:true replaces a rerun's exact name.
            self.artifacts[:] = [item for item in self.artifacts if item["name"] != name]
            self.artifacts.append({"id": artifact_id, "name": name,
                "expired": False, "digest": "sha256:" + hashlib.sha256(content).hexdigest(), "size_in_bytes": len(content),
                "created_at": last, "workflow_run": {"id": 123, "head_sha": CANDIDATE}})
        return job

    def partial_retry(self):
        originals = deepcopy(self.jobs)
        self.run["run_attempt"] = self.attempt["run_attempt"] = 2
        self.add_job("windows-x64-cpu", 101, 2)
        for index, source in enumerate(originals[1:], 102):
            source.update(id=index, run_attempt=2, created_at="2026-01-01T03:00:00Z")
            self.jobs.append(source)

    def request(self, method, endpoint, data=None):
        self.calls.append((method, endpoint, data))
        prefix = "/repos/" + release.REPOSITORY
        if endpoint == prefix:
            return deepcopy(self.repository)
        if endpoint == prefix + "/actions/runs/123":
            return deepcopy(self.run)
        if endpoint == prefix + "/actions/workflows/71":
            return deepcopy(self.workflow)
        if endpoint == prefix + "/commits/" + CANDIDATE:
            return {"sha": CANDIDATE}
        if endpoint == prefix + "/git/ref/heads/main":
            return {"object": {"type": "commit", "sha": self.main_sha}}
        if endpoint.startswith(prefix + "/actions/runs/123/attempts/"):
            return deepcopy(self.attempt)
        for kind in ("jobs", "artifacts"):
            if endpoint.startswith(prefix + "/actions/runs/123/" + kind + "?"):
                rows = getattr(self, kind)
                page = int(urllib.parse.parse_qs(urllib.parse.urlsplit(endpoint).query)["page"][0])
                return {"total_count": len(rows), kind: deepcopy(rows[(page - 1) * 100:page * 100])}
        raise AssertionError(endpoint)

    def download_artifact(self, artifact_id, destination, expected_digest, *, max_bytes):
        self.download_calls.append(artifact_id)
        destination.write_bytes(self.downloads[artifact_id])


class FreezeTests(ExternalTest):
    def setUp(self):
        super().setUp()
        self.api = FakeCI()
        self.output = self.root / "frozen"
        self.source_bundle = self.file(f"sources/qcs-source-materials-{CANDIDATE[:12]}.zip", b"SYNTHETIC CORRESPONDING SOURCES")
        for patch in (mock.patch.object(release, "identity_for", return_value=IDENTITY),
                      mock.patch.object(release, "build_sources", return_value=self.source_bundle),
                      mock.patch.object(release, "validate_sources")):
            patch.start()
            self.addCleanup(patch.stop)

    def prepare(self, attempt=1):
        return release.prepare(release.SOURCE, CANDIDATE, "123", attempt, self.output, api=self.api)

    def test_all_eleven_configurations_freeze_twenty_one_packages_and_one_source(self):
        manifest = self.prepare()
        relocated = self.root / "relocated"
        self.output.rename(relocated)
        document = release.verify_freeze(relocated / manifest.name, release.SOURCE)
        self.assertEqual(len(document["configurations"]), 11)
        self.assertEqual(sum(item["role"] == "user-package" for item in document["assets"]), 21)
        self.assertEqual(sum(item["role"] == "sources" for item in document["assets"]), 1)
        self.assertEqual(len(document["assets"]), 23)
        self.assertTrue(all(item["path"].startswith("assets/") for item in document["assets"]))
        self.assertFalse(any("provenance" in item["path"] for item in document["assets"]))
        self.assertEqual({path.name for path in relocated.iterdir()}, {"assets", release.MANIFEST})
        self.assertFalse(list(self.root.glob(".qcs-release-*")))
        self.assertEqual(document["schemaVersion"], 4)
        self.assertNotIn("published", document)
        self.assertNotIn("draftOnly", document)
        self.assertEqual(len(self.api.download_calls), 21)

    def test_partial_retry_keeps_source_attempt_and_stable_package_names(self):
        self.api.partial_retry()
        document = release.read_json(self.prepare(attempt=2))
        self.assertEqual(document["configurations"]["windows-x64-cpu"]["sourceAttempt"], 2)
        self.assertEqual(document["configurations"]["linux-x64-cpu"]["sourceAttempt"], 1)
        package = document["configurations"]["linux-x64-cpu"]["packages"][0]["filename"]
        self.assertEqual(package, f"qcs-{VERSION}-linux-x64-cpu.deb")

    def test_failed_latest_job_cannot_borrow_old_success(self):
        self.api.partial_retry()
        self.api.jobs[11]["conclusion"] = "failure"
        with self.assertRaisesRegex(ValueError, "Latest native job"):
            self.prepare(attempt=2)
        self.assertFalse(self.output.exists())

    def test_missing_configuration_rejected(self):
        self.api.jobs.pop()
        with self.assertRaisesRegex(ValueError, "No native job"):
            self.prepare()

    def test_expired_artifact_rejected(self):
        self.api.artifacts[0]["expired"] = True
        with self.assertRaisesRegex(ValueError, "expired"):
            self.prepare()

    def test_downloaded_digest_must_match_github(self):
        self.api.downloads[1010] += b"changed"
        with self.assertRaisesRegex(ValueError, "SHA-256 differs"):
            self.prepare()
        self.assertFalse(self.output.exists())

    def test_invalid_context_rejected_before_download(self):
        for key, value in (("head_sha", "2" * 40), ("run_attempt", 2), ("id", 99), ("workflow_id", 99)):
            with self.subTest(key=key):
                api = FakeCI()
                api.run[key] = value
                with self.assertRaises((ValueError, AssertionError)):
                    release.prepare(release.SOURCE, CANDIDATE, "123", 1, self.output, api=api)
        self.assertFalse(self.output.exists())

    def test_wrong_workflow_repository_or_failed_run_rejected(self):
        for scope, key, value in (("workflow", "path", ".github/workflows/other.yml"),
                                  ("repository", "full_name", "another/repository"),
                                  ("run", "conclusion", "failure")):
            with self.subTest(scope=scope):
                api = FakeCI()
                getattr(api, scope)[key] = value
                with self.assertRaises(ValueError):
                    release.prepare(release.SOURCE, CANDIDATE, "123", 1, self.output, api=api)

    def test_package_byte_mutation_is_rejected(self):
        manifest = self.prepare()
        document = release.read_json(manifest)
        (manifest.parent / document["assets"][0]["path"]).write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "content changed"):
            release.verify_freeze(manifest, release.SOURCE)

    def test_asset_metadata_cannot_replace_prepared_configuration(self):
        manifest = self.prepare()
        document = release.read_json(manifest)
        item = document["assets"][0]
        path = manifest.parent / item["path"]
        path.write_bytes(b"self-consistent replacement")
        item.update(bytes=path.stat().st_size, sha256=release.digest(path))
        release.write_json(manifest, document)
        with self.assertRaisesRegex(ValueError, "prepared package metadata"):
            release.verify_freeze(manifest, release.SOURCE)

    def test_added_asset_cannot_enter_upload_set(self):
        manifest = self.prepare()
        (manifest.parent / "assets/extra.txt").write_text("extra")
        with self.assertRaisesRegex(ValueError, "extra files"):
            release.verify_freeze(manifest, release.SOURCE)

    def test_existing_output_and_source_output_are_rejected(self):
        self.output.mkdir()
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.prepare()
        with self.assertRaisesRegex(ValueError, "outside"):
            release.prepare(release.SOURCE, CANDIDATE, "123", 1, release.SOURCE / "generated", api=self.api)

    def test_source_mutation_during_prepare_fails_without_publishing_output(self):
        with mock.patch.object(release, "identity_for", side_effect=[IDENTITY, {**IDENTITY, "workingTreeModified": True}]):
            with self.assertRaisesRegex(ValueError, "Source identity changed"):
                self.prepare()
        self.assertFalse(self.output.exists())


    def test_preflight_rechecks_metadata_without_downloading_packages_again(self):
        document = release.read_json(self.prepare())
        before = len(self.api.download_calls)
        release.verify_ci_packages(self.api, document)
        self.assertEqual(len(self.api.download_calls), before)

    def test_downloaded_raw_size_must_match_artifact(self):
        self.api.artifacts[0]["size_in_bytes"] += 1
        with self.assertRaisesRegex(ValueError, "size differs"):
            self.prepare()

    def test_schema_three_freeze_is_rejected(self):
        manifest = self.prepare()
        document = release.read_json(manifest)
        document["schemaVersion"] = 3
        release.write_json(manifest, document)
        with self.assertRaisesRegex(ValueError, "schema"):
            release.verify_freeze(manifest, release.SOURCE)

    def test_rewritten_local_package_metadata_must_match_raw_artifact(self):
        manifest = self.prepare()
        document = release.read_json(manifest)
        configuration = next(iter(document["configurations"].values()))
        configuration["packages"][0]["sha256"] = "9" * 64
        release.write_json(manifest, document)
        with self.assertRaisesRegex(ValueError, "differ from the selected CI artifact"):
            release.verify_freeze(manifest, release.SOURCE)

    def test_rewritten_entire_freeze_cannot_replace_remote_artifact(self):
        manifest = self.prepare()
        document = release.read_json(manifest)
        selected = next(iter(document["configurations"].values()))
        artifact, package = selected["artifacts"][0], selected["packages"][0]
        path = manifest.parent / "assets" / package["filename"]
        path.write_bytes(b"self-consistent local replacement")
        artifact.update(artifactSize=path.stat().st_size, artifactDigest="sha256:" + release.digest(path))
        package.update(bytes=path.stat().st_size, sha256=release.digest(path))
        for item in document["assets"]:
            if item["name"] == package["filename"]:
                item.update(bytes=path.stat().st_size, sha256=release.digest(path))
        checksum = manifest.parent / "assets/SHA256SUMS"
        checksum.write_text("".join(f"{item['sha256']}  {item['name']}\n" for item in sorted(document["assets"], key=lambda row: row["name"])
                                     if item["role"] != "checksums"), encoding="utf-8")
        for item in document["assets"]:
            if item["role"] == "checksums":
                item.update(bytes=checksum.stat().st_size, sha256=release.digest(checksum))
        release.write_json(manifest, document)
        release.verify_freeze(manifest, release.SOURCE)
        with self.assertRaisesRegex(ValueError, "changed after release preparation"):
            release.verify_ci_packages(self.api, document)

    def test_main_movement_prevents_prepare_downloads(self):
        self.api.main_sha = "9" * 40
        with self.assertRaisesRegex(ValueError, "Remote main"):
            self.prepare()
        self.assertEqual(self.api.download_calls, [])

    def test_source_archive_is_moved_and_disk_budget_precedes_packages(self):
        with mock.patch.object(release, "require_free_space", wraps=release.require_free_space) as budget:
            manifest = self.prepare()
        self.assertFalse(self.source_bundle.exists())
        expected = sum(row["size_in_bytes"] for row in self.api.artifacts)
        self.assertEqual(budget.call_args.args[1], expected)
        self.assertTrue((manifest.parent / "assets" / self.source_bundle.name).is_file())

    def test_insufficient_disk_space_prevents_package_downloads(self):
        with mock.patch.object(release.shutil, "disk_usage", return_value=types.SimpleNamespace(free=release.SPACE_RESERVE)):
            with self.assertRaisesRegex(ValueError, "Insufficient release disk"):
                self.prepare()
        self.assertEqual(self.api.download_calls, [])
        self.assertFalse(self.output.exists())


class PackageContractTests(ExternalTest):
    def test_rocm_setup_zip_replaces_only_installer(self):
        options = release.package_name_options("windows-x64-rocm", "0.1.0")
        self.assertEqual(options, [{"qcs-0.1.0-windows-x64-rocm-10.0.0.exe", "qcs-0.1.0-windows-x64-rocm-10.0.0.zip"},
                                  {"qcs-0.1.0-windows-x64-rocm-10.0.0-setup.zip", "qcs-0.1.0-windows-x64-rocm-10.0.0.zip"}])

    def test_single_release_asset_limit_is_strictly_below_two_gib(self):
        release.require_asset_size(2 * 1024**3 - 1, "fixture.zip")
        for size in (0, -1, True, 2 * 1024**3):
            with self.subTest(size=size), self.assertRaises(ValueError):
                release.require_asset_size(size, "fixture.zip")

    def test_paths_and_outputs_cannot_escape(self):
        for name in ("../outside", "a/../../outside", "C:/outside", "a\\outside", "a\nname"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                release.safe_file(self.root, name)


class FakeGitHub(FakeCI):
    def __init__(self):
        super().__init__()
        self.release = None
        self.assets = []
        self.uploaded = []
        self.tag_sha = None
        self.fail_next = None
        self.lose_publish_response = False
        self.tag_create_error = None
        self.asset_contents = {}

    def request(self, method, endpoint, data=None):
        prefix = "/repos/" + release.REPOSITORY
        if "/git/ref/tags/" in endpoint:
            if not self.tag_sha:
                raise release.ApiError(404)
            return {"object": {"type": "commit", "sha": self.tag_sha}}
        if method == "POST" and endpoint == prefix + "/git/refs":
            self.calls.append((method, endpoint, data))
            self.tag_sha = CANDIDATE
            if self.tag_create_error:
                raise self.tag_create_error
            return {"object": {"type": "commit", "sha": self.tag_sha}}
        if method == "POST" and endpoint == prefix + "/releases":
            self.calls.append((method, endpoint, data))
            self.release = {**data, "id": 17, "published_at": None}
            return deepcopy(self.release)
        if endpoint.startswith(prefix + "/releases?"):
            return [deepcopy(self.release)] if self.release else []
        if endpoint.startswith(prefix + "/releases/17/assets?"):
            return deepcopy(self.assets)
        if endpoint == prefix + "/releases/17":
            if method == "PATCH":
                self.calls.append((method, endpoint, data))
                self.release.update(data)
                if data.get("draft") is False:
                    self.release["published_at"] = "2026-01-01T04:00:00Z"
                    self.tag_sha = CANDIDATE
                if self.lose_publish_response and data.get("draft") is False:
                    self.lose_publish_response = False
                    raise OSError("SYNTHETIC response lost after server publish")
            return deepcopy(self.release)
        if endpoint == prefix + "/releases/latest":
            return deepcopy(self.release)
        return super().request(method, endpoint, data)

    def asset_digest(self, asset):
        return asset["digest"].removeprefix("sha256:")

    def asset_bytes(self, asset, *, max_bytes=32 * 1024 * 1024):
        value = self.asset_contents[asset["id"]]
        if len(value) > max_bytes or len(value) != asset["size"] or "sha256:" + hashlib.sha256(value).hexdigest() != asset["digest"]:
            raise ValueError("Synthetic metadata bytes do not match")
        return value

    def upload(self, release_id, path, name):
        if name == self.fail_next:
            self.fail_next = None
            raise release.ApiError(503)
        self.uploaded.append(name)
        result = {"id": len(self.assets) + 1, "name": name, "state": "uploaded", "size": path.stat().st_size,
                  "digest": "sha256:" + release.digest(path)}
        self.assets.append(result)
        self.asset_contents[result["id"]] = path.read_bytes()
        return deepcopy(result)


class UploadTests(ExternalTest):
    def setUp(self):
        super().setUp()
        self.api = FakeGitHub()
        self.source_bundle = self.file(f"sources/qcs-source-materials-{CANDIDATE[:12]}.zip", b"SYNTHETIC CORRESPONDING SOURCES")
        for patch in (mock.patch.object(release, "identity_for", return_value=IDENTITY),
                      mock.patch.object(release, "build_sources", return_value=self.source_bundle),
                      mock.patch.object(release, "validate_sources")):
            patch.start()
            self.addCleanup(patch.stop)
        self.manifest = release.prepare(release.SOURCE, CANDIDATE, "123", 1, self.root / "frozen", api=self.api)
        self.document = release.read_json(self.manifest)

    def upload(self):
        return release.upload_draft(self.manifest, release.SOURCE, TAG, api=self.api)

    def publish(self):
        return release.publish(self.manifest, release.SOURCE, TAG, api=self.api)

    def test_create_and_reuse_draft_checks_all_twenty_four_assets_without_redownload(self):
        first = self.upload()
        second = self.upload()
        self.assertEqual(len(first["uploaded"]), 24)
        self.assertEqual(second["uploaded"], [])
        self.assertEqual(len(second["reused"]), 24)
        self.assertEqual(len(self.api.uploaded), 24)
        self.assertEqual(len(self.api.download_calls), 21)
        self.assertTrue(self.api.release["draft"])
        self.assertFalse(first["published"])

    def test_partial_upload_resumes_without_replacing_completed_asset(self):
        self.api.fail_next = self.document["assets"][1]["name"]
        with self.assertRaises(release.ApiError):
            self.upload()
        first = self.document["assets"][0]["name"]
        self.assertEqual(self.api.uploaded, [first])
        result = self.upload()
        self.assertEqual(result["reused"], [first])
        self.assertEqual(self.api.uploaded.count(first), 1)
        self.assertTrue(self.api.release["draft"])

    def test_same_size_different_hash_is_not_treated_as_uploaded(self):
        self.upload()
        self.api.assets[0]["digest"] = "sha256:" + "a" * 64
        before = list(self.api.uploaded)
        with self.assertRaisesRegex(ValueError, "No asset was overwritten"):
            self.upload()
        self.assertEqual(self.api.uploaded, before)

    def test_published_release_is_rejected_before_upload(self):
        self.upload()
        self.publish()
        before = len(self.api.uploaded)
        with self.assertRaisesRegex(ValueError, "only operates on unpublished drafts"):
            self.upload()
        self.assertEqual(len(self.api.uploaded), before)

    def test_tag_at_another_commit_is_rejected_before_create(self):
        self.api.tag_sha = "9" * 40
        with self.assertRaisesRegex(ValueError, "different commit"):
            self.upload()
        self.assertIsNone(self.api.release)

    def test_branch_target_draft_is_not_silently_retargeted(self):
        self.upload()
        self.api.release["target_commitish"] = "main"
        with self.assertRaisesRegex(ValueError, "not pinned"):
            self.upload()

    def test_unexpected_assets_are_not_deleted(self):
        self.upload()
        self.api.assets.append({"name": "old-candidate.zip"})
        with self.assertRaisesRegex(ValueError, "unexpected assets"):
            self.upload()
        self.assertTrue(any(item["name"] == "old-candidate.zip" for item in self.api.assets))

    def test_local_asset_mutation_prevents_release_creation(self):
        first = self.manifest.parent / self.document["assets"][0]["path"]
        first.write_bytes(b"changed since freeze")
        with self.assertRaisesRegex(ValueError, "content changed"):
            self.upload()
        self.assertIsNone(self.api.release)

    def test_oversized_asset_fails_before_any_remote_request(self):
        self.document["assets"][0]["bytes"] = 2 * 1024**3
        release.write_json(self.manifest, self.document)
        before = len(self.api.calls)
        with self.assertRaisesRegex(ValueError, "smaller than 2 GiB"):
            self.upload()
        self.assertEqual(len(self.api.calls), before)

    def test_new_failed_ci_attempt_prevents_remote_release_creation(self):
        self.api.run.update(run_attempt=2, conclusion="failure")
        with self.assertRaisesRegex(ValueError, "identity differs"):
            self.upload()
        self.assertIsNone(self.api.release)

    def test_changed_remote_selection_prevents_release_creation(self):
        self.api.artifacts[0]["digest"] = "sha256:" + "9" * 64
        with self.assertRaisesRegex(ValueError, "changed after release preparation"):
            self.upload()
        self.assertIsNone(self.api.release)

    def test_publish_requires_uploaded_draft(self):
        with self.assertRaisesRegex(ValueError, "No prepared draft"):
            self.publish()
        self.assertIsNone(self.api.release)

    def test_publish_is_explicit_complete_and_idempotent(self):
        self.upload()
        first = self.publish()
        second = self.publish()
        self.assertTrue(first["published"])
        self.assertFalse(first["alreadyPublished"])
        self.assertTrue(second["alreadyPublished"])
        self.assertEqual(len(first["assets"]), 24)
        patches = [data for method, _, data in self.api.calls if method == "PATCH"]
        self.assertEqual(patches, [{"tag_name": TAG, "target_commitish": CANDIDATE,
                                  "draft": False, "prerelease": False, "make_latest": "true"}])
        self.assertEqual(len(self.api.download_calls), 21)

    def test_published_retry_requires_the_same_latest_stable_release(self):
        self.upload()
        self.publish()
        original = self.api.request
        def different_latest(method, endpoint, data=None):
            if endpoint == f"/repos/{release.REPOSITORY}/releases/latest":
                return {"id": 99}
            return original(method, endpoint, data)
        with mock.patch.object(self.api, "request", side_effect=different_latest):
            with self.assertRaisesRegex(ValueError, "latest stable release"):
                self.publish()
        self.assertEqual(len([item for item in self.api.calls if item[0] == "PATCH"]), 1)

    def test_publish_response_loss_confirms_success_and_retry(self):
        self.upload()
        self.api.lose_publish_response = True
        self.assertTrue(self.publish()["published"])
        self.assertTrue(self.publish()["alreadyPublished"])

    def test_publish_missing_asset_does_not_upload_or_publish(self):
        self.upload()
        self.api.assets.pop()
        before = len(self.api.uploaded)
        with self.assertRaisesRegex(ValueError, "incomplete"):
            self.publish()
        self.assertEqual(len(self.api.uploaded), before)
        self.assertTrue(self.api.release["draft"])
        self.upload()
        self.assertTrue(self.publish()["published"])

    def test_published_missing_asset_is_not_a_successful_retry(self):
        self.upload()
        self.publish()
        self.api.assets.pop()
        with self.assertRaisesRegex(ValueError, "incomplete"):
            self.publish()

    def test_publish_conflicting_asset_or_tag_never_patches(self):
        self.upload()
        for conflict in ("asset", "tag"):
            with self.subTest(conflict=conflict):
                original = deepcopy(self.api.assets)
                if conflict == "asset":
                    self.api.assets[0]["digest"] = "sha256:" + "9" * 64
                else:
                    self.api.tag_sha = "9" * 40
                with self.assertRaises(ValueError):
                    self.publish()
                self.api.assets = original
                self.api.tag_sha = None
        self.assertFalse(any(method == "PATCH" for method, _, _ in self.api.calls))

    def test_publish_creates_missing_tag_only_after_complete_validation(self):
        self.upload()
        self.publish()
        mutations = [(method, endpoint, data) for method, endpoint, data in self.api.calls if method in ("POST", "PATCH")]
        self.assertEqual(mutations[-2][1], f"/repos/{release.REPOSITORY}/git/refs")
        self.assertEqual(mutations[-2][2], {"ref": "refs/tags/" + TAG, "sha": CANDIDATE})
        self.assertEqual(mutations[-1][0], "PATCH")

    def test_tag_create_concurrent_same_commit_and_lost_response_are_recoverable(self):
        self.upload()
        for error in (release.ApiError(409), release.ApiError(422), OSError("SYNTHETIC tag response lost")):
            with self.subTest(error=str(error)):
                self.api.tag_sha = None
                self.api.tag_create_error = error
                release.ensure_release_tag(self.api, TAG, CANDIDATE)
                self.assertEqual(self.api.tag_sha, CANDIDATE)

    def test_incomplete_assets_do_not_create_a_tag(self):
        self.upload()
        self.api.assets.pop()
        with self.assertRaisesRegex(ValueError, "incomplete"):
            self.publish()
        self.assertIsNone(self.api.tag_sha)

    def test_ci_changes_during_publication_preflight_prevent_tag_creation(self):
        self.upload()
        original = release.verify_ci_packages
        calls = []
        def change(api, document):
            calls.append(True)
            if len(calls) == 2:
                api.run["run_attempt"] = 2
            return original(api, document)
        with mock.patch.object(release, "verify_ci_packages", side_effect=change):
            with self.assertRaisesRegex(ValueError, "identity differs"):
                self.publish()
        self.assertIsNone(self.api.tag_sha)
        self.assertTrue(self.api.release["draft"])

    def test_publish_main_moved_is_rejected(self):
        self.upload()
        self.api.main_sha = "9" * 40
        with self.assertRaisesRegex(ValueError, "Remote main"):
            self.publish()
        self.assertTrue(self.api.release["draft"])

    def test_tag_must_match_package_version(self):
        with self.assertRaisesRegex(ValueError, "frozen project version"):
            release.upload_draft(self.manifest, release.SOURCE, TAG + "-mismatch", api=self.api)
        self.assertIsNone(self.api.release)

    def test_notes_only_contain_categories_and_all_twenty_four_exact_downloads(self):
        self.upload()
        notes = self.api.release["body"]
        prefix = f"https://github.com/{release.REPOSITORY}/releases/download/{TAG}/"
        expected_names = [item["name"] for item in self.document["assets"]] + [release.MANIFEST]
        self.assertEqual(len(expected_names), 24)
        self.assertCountEqual(re.findall(r"\]\(([^)]+)\)", notes), [prefix + name for name in expected_names])
        self.assertEqual([line for line in notes.splitlines() if line.startswith("### ")],
                         ["### Windows x64", "### Linux x64", "### macOS Apple Silicon", "### 其他下载"])
        for line in filter(None, notes.splitlines()):
            self.assertTrue(line.startswith(("### ", "|", "- [")), line)
        for text in ("安装与使用", "签名与验证范围", "首次使用", "payload-manifest.json", "源码提交", "CI：", "attempt1.zip"):
            self.assertNotIn(text, notes)

    def test_rocm_setup_notes_use_selected_installer_name(self):
        document = deepcopy(self.document)
        packages = document["configurations"]["windows-x64-rocm"]["packages"]
        for item in packages:
            if item["filename"].endswith(".exe"):
                item["filename"] = item["filename"].removesuffix(".exe") + "-setup.zip"
        notes = release.release_notes(document, TAG)
        self.assertIn(f"[ZIP 安装器](https://github.com/{release.REPOSITORY}/releases/download/{TAG}/qcs-{VERSION}-windows-x64-rocm-10.0.0-setup.zip)", notes)
        self.assertNotIn(f"qcs-{VERSION}-windows-x64-rocm-10.0.0.exe)", notes)

    def test_backend_labels_bind_platform_specific_sdk_versions(self):
        body = release.release_notes(self.document, TAG)
        windows, linux = body.split("### Linux x64")
        self.assertIn("| Vulkan SDK 1.4.357.0 |", windows)
        self.assertIn("| Vulkan SDK 1.4.313.0 |", linux)
        self.assertEqual(body.count("| ROCm SDK 10.0.0 |"), 2)
        self.assertIn("| CUDA 12.4 |", windows)
        self.assertIn("| CUDA 12.8 |", linux)
        self.assertEqual(body, release.release_notes(self.document, TAG))
        for field, value in (("backend", "unknown"), ("toolkitVersion", ""), ("toolkitVersion", "driver-latest")):
            inventory = deepcopy(release.variants())
            inventory["windows-x64-rocm"][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                release.release_notes(self.document, TAG, inventory)

    def test_draft_resume_synchronizes_template_without_changing_assets(self):
        self.upload()
        before = deepcopy(self.api.assets)
        self.api.release["body"] = "Manually changed draft"
        result = self.upload()
        self.assertEqual(result["uploaded"], [])
        self.assertEqual(self.api.assets, before)
        self.assertEqual(self.api.release["body"], release.release_notes(self.document, TAG))
        self.assertEqual([data for method, _, data in self.api.calls if method == "PATCH"],
                         [{"body": release.release_notes(self.document, TAG)}])

    def test_publish_rejects_drifted_body_before_publishing(self):
        self.upload()
        self.api.release["body"] = "Manually changed draft"
        with self.assertRaisesRegex(ValueError, "body differs"):
            self.publish()
        self.assertTrue(self.api.release["draft"])
        self.assertFalse(any(method == "PATCH" for method, _, _ in self.api.calls))

    def legacy_release(self):
        self.upload()
        self.publish()
        inventory = deepcopy(release.variants())
        document = deepcopy(self.document)
        document["version"] = "0.2.0"
        mapping = {}
        for identifier, row in inventory.items():
            row.pop("packageVariant", None)
            for package in document["configurations"][identifier]["packages"]:
                old_name = package["filename"]
                extension = ".tar.gz" if old_name.endswith(".tar.gz") else "." + old_name.rsplit(".", 1)[1]
                name = f"qcs-0.2.0-{identifier}{extension}"
                mapping[old_name] = name
                package["filename"] = name
        for row in document["assets"]:
            if row["name"] in mapping:
                row["name"] = mapping[row["name"]]
                row["path"] = "assets/" + row["name"]
        for row in self.api.assets:
            row["name"] = mapping.get(row["name"], row["name"])
            if row["name"] == release.MANIFEST:
                content = (json.dumps(document) + "\n").encode()
                self.api.asset_contents[row["id"]] = content
                row.update(size=len(content), digest="sha256:" + hashlib.sha256(content).hexdigest())
        self.api.release.update(tag_name="v0.2.0", body="Old download table")
        self.api.main_sha = "9" * 40
        self.api.calls.clear()
        return inventory, document

    def test_historical_body_update_preserves_original_assets_and_is_idempotent(self):
        inventory, document = self.legacy_release()
        assets = deepcopy(self.api.assets)
        before = len(self.api.download_calls)
        with mock.patch.object(release, "historical_variants", return_value=inventory), \
                mock.patch.object(release, "verify_ci_packages", side_effect=AssertionError("Historical notes must not need retained CI artifacts")), \
                mock.patch.object(release, "identity_for", side_effect=AssertionError("Historical notes must not require old main")):
            result = release.update_notes(release.SOURCE, "v0.2.0", CANDIDATE, api=self.api)
            second = release.update_notes(release.SOURCE, "v0.2.0", CANDIDATE, api=self.api)
        body = self.api.release["body"]
        self.assertTrue(result["bodyUpdated"])
        self.assertFalse(second["bodyUpdated"])
        self.assertEqual(self.api.assets, assets)
        self.assertEqual(len(self.api.download_calls), before)
        self.assertIn("| ROCm SDK 10.0.0 |", body)
        self.assertIn("/qcs-0.2.0-windows-x64-rocm.exe)", body)
        self.assertNotIn("/qcs-0.2.0-windows-x64-rocm-10.0.0.exe)", body)
        self.assertEqual([data for method, _, data in self.api.calls if method == "PATCH"], [{"body": body}])
        self.assertEqual(body, release.release_notes(document, "v0.2.0", inventory))

    def test_historical_body_update_rejects_wrong_commit_or_asset_before_write(self):
        inventory, _ = self.legacy_release()
        with mock.patch.object(release, "historical_variants", return_value=inventory):
            with self.assertRaisesRegex(ValueError, "expected commit"):
                release.update_notes(release.SOURCE, "v0.2.0", "8" * 40, api=self.api)
            self.api.assets[0]["digest"] = "sha256:" + "8" * 64
            with self.assertRaisesRegex(ValueError, "Remote asset differs"):
                release.update_notes(release.SOURCE, "v0.2.0", CANDIDATE, api=self.api)
        self.assertFalse(any(method == "PATCH" for method, _, _ in self.api.calls))


class NotesOutputTests(ExternalTest):
    def test_notes_cannot_change_or_add_files_inside_freeze(self):
        manifest = self.file("freeze/qcs-release-manifest.json", b"original manifest")
        for relative in ("freeze/qcs-release-manifest.json", "freeze/notes.md", "freeze/assets/notes.md"):
            with self.subTest(output=relative), mock.patch.object(release, "notes") as render, \
                    mock.patch.object(sys, "stderr", io.StringIO()):
                self.assertEqual(release.main(["notes", "--tag", TAG, "--manifest", str(manifest),
                                               "--output", str(self.root / relative)]), 1)
                render.assert_not_called()
        self.assertEqual(manifest.read_bytes(), b"original manifest")
        self.assertEqual(list(manifest.parent.iterdir()), [manifest])

    def test_notes_write_sibling_with_lf_or_print_stdout(self):
        manifest = self.file("freeze/qcs-release-manifest.json")
        output = self.root / "freeze-notes.md"
        body = "## Windows\n\nDownload\n"
        with mock.patch.object(release, "notes", return_value=body), mock.patch.object(sys, "stdout", io.StringIO()):
            self.assertEqual(release.main(["notes", "--tag", TAG, "--manifest", str(manifest),
                                           "--output", str(output)]), 0)
        self.assertEqual(output.read_bytes(), body.encode())
        with mock.patch.object(release, "notes", return_value=body), mock.patch.object(sys, "stdout", io.StringIO()) as stream:
            self.assertEqual(release.main(["notes", "--tag", TAG]), 0)
            self.assertEqual(stream.getvalue(), body)


class GitNormalizationTests(ExternalTest):
    def test_text_crlf_and_lf_share_identity_but_binary_bytes_do_not(self):
        subprocess.run(["git", "init", "--quiet", str(self.root)], check=True, capture_output=True)
        self.file(".gitattributes", b"* text eol=lf\nbinary.bin -text\n")
        text_lf = release.git_blob_identity(self.root, "text.txt", io.BytesIO(b"first\nsecond\n"))
        text_crlf = release.git_blob_identity(self.root, "text.txt", io.BytesIO(b"first\r\nsecond\r\n"))
        binary_lf = release.git_blob_identity(self.root, "binary.bin", io.BytesIO(b"first\nsecond\n"))
        binary_crlf = release.git_blob_identity(self.root, "binary.bin", io.BytesIO(b"first\r\nsecond\r\n"))
        self.assertEqual(text_lf, text_crlf)
        self.assertNotEqual(binary_lf, binary_crlf)


class SourceIdentityTests(ExternalTest):
    def setUp(self):
        super().setUp()
        self.file("README.md", b"SYNTHETIC source checkout\n")
        self.git("init", "--quiet", "--initial-branch=main")
        self.git("add", ".")
        self.git("-c", "user.name=QCS synthetic test", "-c", "user.email=synthetic@example.invalid",
                 "commit", "--quiet", "-m", "Synthetic source fixture")
        self.commit = self.git("rev-parse", "HEAD")
        source_patch = mock.patch.object(release, "SOURCE", self.root)
        source_patch.start()
        self.addCleanup(source_patch.stop)

    def git(self, *arguments):
        command = ["git", "-c", "core.autocrlf=false", "-C", str(self.root), *arguments]
        result = subprocess.run(command, text=True, capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, "Git fixture command failed:\n" + result.stdout + result.stderr)
        return result.stdout.strip()

    def test_clean_checkout_is_bound_to_its_commit_and_tree(self):
        identity = release.identity_for(self.root, self.commit)
        self.assertEqual(identity["checkoutCommit"], self.commit)
        self.assertEqual(identity["sourceTree"], self.git("rev-parse", "HEAD^{tree}"))
        self.assertFalse(identity["workingTreeModified"])

    def test_clean_non_main_branch_cannot_be_released(self):
        self.git("checkout", "--quiet", "-b", "feature")
        with self.assertRaisesRegex(ValueError, "main branch"):
            release.identity_for(self.root, self.commit)

    def test_wrong_commit_and_untracked_changes_cannot_be_released(self):
        with self.assertRaisesRegex(ValueError, "exact clean source commit"):
            release.identity_for(self.root, "0" * 40)
        self.file("untracked.txt", b"local changes")
        with self.assertRaisesRegex(ValueError, "exact clean source commit"):
            release.identity_for(self.root, self.commit)


class SourceMaterialTests(ExternalTest):
    def setUp(self):
        super().setUp()
        self.package = {"name": "fixture", "url": "https://example.invalid/source.zip", "sha256": "a" * 64}
        self.manifest = {"commit": CANDIDATE, "sourceTree": IDENTITY["sourceTree"], "workingTreeModified": False,
                         "packages": [self.package], "projectFiles": [{"path": "README"}]}
        self.bundle = self.root / "sources.zip"
        self.write_bundle()
        self.sources = types.SimpleNamespace(verify_bundle=mock.Mock(return_value={"passed": True}),
                                             entries=mock.Mock(return_value=[self.package]))
        for patch in (mock.patch.object(release, "module", return_value=self.sources),
                      mock.patch.object(release.subprocess, "check_output", return_value=b"100644 blob " + b"c" * 40 + b"\tREADME\0"),
                      mock.patch.object(release, "git_blob_identity", return_value="c" * 40)):
            patch.start()
            self.addCleanup(patch.stop)

    def write_bundle(self):
        with zipfile.ZipFile(self.bundle, "w") as archive:
            archive.writestr("source-manifest.json", json.dumps(self.manifest))
            archive.writestr("project/README", "SYNTHETIC SOURCE")

    def test_single_bundle_binds_locks_and_committed_project(self):
        release.validate_sources(self.bundle, IDENTITY, release.SOURCE)
        self.sources.verify_bundle.assert_called_once_with(self.bundle, candidate_commit=CANDIDATE)

    def test_dirty_or_wrong_source_candidate_rejected(self):
        for key, value in (("commit", "2" * 40), ("sourceTree", "3" * 40), ("workingTreeModified", True)):
            with self.subTest(key=key):
                original = self.manifest[key]
                self.manifest[key] = value
                self.write_bundle()
                with self.assertRaisesRegex(ValueError, "another or modified"):
                    release.validate_sources(self.bundle, IDENTITY, release.SOURCE)
                self.manifest[key] = original

    def test_source_lock_substitution_rejected(self):
        self.sources.entries.return_value = [{**self.package, "sha256": "b" * 64}]
        with self.assertRaisesRegex(ValueError, "lock mismatch"):
            release.validate_sources(self.bundle, IDENTITY, release.SOURCE)

    def test_missing_source_file_rejected(self):
        self.manifest["projectFiles"] = []
        self.write_bundle()
        with self.assertRaisesRegex(ValueError, "files differ"):
            release.validate_sources(self.bundle, IDENTITY, release.SOURCE)

    def test_source_file_must_match_committed_blob(self):
        with mock.patch.object(release, "git_blob_identity", return_value="d" * 40):
            with self.assertRaisesRegex(ValueError, "differs from the candidate"):
                release.validate_sources(self.bundle, IDENTITY, release.SOURCE)


class TagIdentityTests(unittest.TestCase):
    def test_annotated_tag_resolves_to_exact_candidate(self):
        api = mock.Mock()
        api.request.side_effect = [{"object": {"type": "tag", "sha": "a" * 40}},
                                   {"object": {"type": "commit", "sha": CANDIDATE}}]
        self.assertTrue(release.validate_tag(api, "v0.1.0", CANDIDATE, required=True))
        self.assertTrue(api.request.call_args_list[1].args[1].endswith("/git/tags/" + "a" * 40))

    def test_required_tag_must_exist(self):
        api = mock.Mock()
        api.request.side_effect = release.ApiError(404)
        with self.assertRaises(release.ApiError):
            release.validate_tag(api, "v0.1.0", CANDIDATE, required=True)

    def test_concurrent_tag_creation_at_another_commit_is_rejected(self):
        api = mock.Mock()
        api.request.side_effect = [release.ApiError(404), release.ApiError(422),
                                   {"object": {"type": "commit", "sha": "9" * 40}}]
        with self.assertRaisesRegex(ValueError, "different commit"):
            release.ensure_release_tag(api, "v0.1.0", CANDIDATE)
        self.assertFalse(any(call.args[0] == "PATCH" for call in api.request.call_args_list))


class ArtifactTransportTests(ExternalTest):
    def setUp(self):
        super().setUp()
        self.api = release.GitHub("synthetic-secret")
        self.api._opener = mock.Mock()
        self.path = self.root / "artifact.zip"

    def download(self, content=b"fixture archive"):
        self.api.download_artifact(1, self.path, "sha256:" + hashlib.sha256(content).hexdigest(), max_bytes=1024)

    def test_download_hash_verified(self):
        self.api._opener.open.return_value = io.BytesIO(b"fixture archive")
        self.download()
        self.assertEqual(self.path.read_bytes(), b"fixture archive")

    def test_download_digest_mismatch_removes_partial_file(self):
        self.api._opener.open.return_value = io.BytesIO(b"wrong archive")
        with self.assertRaisesRegex(ValueError, "SHA-256 differs"):
            self.download()
        self.assertFalse(self.path.exists())

    def test_download_does_not_delete_an_existing_file(self):
        self.path.write_bytes(b"existing")
        self.api._opener.open.return_value = io.BytesIO(b"fixture archive")
        with self.assertRaises(FileExistsError):
            self.download()
        self.assertEqual(self.path.read_bytes(), b"existing")

    def test_download_size_limit_removes_partial_file(self):
        self.api._opener.open.return_value = io.BytesIO(b"x" * 1025)
        with self.assertRaisesRegex(ValueError, "size budget"):
            self.download()
        self.assertFalse(self.path.exists())

    def test_artifact_redirect_drops_token_before_azure_download(self):
        redirect = urllib.error.HTTPError("https://api.github.com/x", 302, "Found",
            {"Location": "https://fixture.blob.core.windows.net/artifacts/a?sig=fixture"}, None)
        self.api._opener.open.side_effect = [redirect, io.BytesIO(b"fixture archive")]
        self.download()
        request = self.api._opener.open.call_args_list[1].args[0]
        self.assertIsNone(request.get_header("Authorization"))

    def test_every_signed_download_redirect_omits_api_token(self):
        def redirect(location):
            return urllib.error.HTTPError("https://api.github.com/x", 302, "Found", {"Location": location}, None)
        self.api._opener.open.side_effect = [
            redirect("https://fixture.actions.githubusercontent.com/archive"),
            redirect("https://fixture.blob.core.windows.net/artifact?sig=fixture"), io.BytesIO(b"fixture archive")]
        self.download()
        requests = [call.args[0] for call in self.api._opener.open.call_args_list]
        self.assertEqual(requests[0].get_header("Authorization"), "Bearer synthetic-secret")
        self.assertTrue(all(request.get_header("Authorization") is None for request in requests[1:]))

    def test_unknown_artifact_redirect_rejected(self):
        self.api._opener.open.side_effect = urllib.error.HTTPError("https://api.github.com/x", 302, "Found",
            {"Location": "https://untrusted.invalid/a"}, None)
        with self.assertRaises(release.ApiError):
            self.download()
        self.assertFalse(self.path.exists())


class TransportTests(unittest.TestCase):
    def test_small_metadata_checks_size_digest_and_drops_redirect_credentials(self):
        api = release.GitHub("synthetic-secret")
        api._opener = mock.Mock()
        redirect = urllib.error.HTTPError("https://api.github.com/x", 302, "Found",
                                          {"Location": "https://release-assets.githubusercontent.com/test?signature=x"}, None)
        api._opener.open.side_effect = [redirect, io.BytesIO(b"metadata")]
        asset = {"id": 1, "size": 8, "digest": "sha256:" + hashlib.sha256(b"metadata").hexdigest()}
        self.assertEqual(api.asset_bytes(asset), b"metadata")
        request = api._opener.open.call_args_list[1].args[0]
        self.assertIsNone(request.get_header("Authorization"))
        for content, supplied in ((b"short", asset["digest"]), (b"excess bytes", asset["digest"]),
                                  (b"metadata", "sha256:" + "1" * 64)):
            with self.subTest(content=content, digest=supplied):
                api._opener.open.side_effect = None
                api._opener.open.return_value = io.BytesIO(content)
                with self.assertRaises(ValueError):
                    api.asset_bytes({**asset, "digest": supplied})

    def test_small_metadata_rejects_oversize_before_network(self):
        api = release.GitHub("synthetic-secret")
        api._opener = mock.Mock()
        for size in (0, -1, True, "8", 32 * 1024 * 1024 + 1):
            with self.subTest(size=size), self.assertRaises(ValueError):
                api.asset_bytes({"id": 1, "size": size})
        api._opener.open.assert_not_called()

    def test_missing_remote_digest_streams_content(self):
        api = release.GitHub("synthetic-secret")
        api._opener = mock.Mock()
        api._opener.open.return_value = io.BytesIO(b"remote bytes")
        self.assertEqual(api.asset_digest({"id": 1, "size": 12}), hashlib.sha256(b"remote bytes").hexdigest())

    def test_download_redirect_drops_bearer_token(self):
        api = release.GitHub("synthetic-secret")
        api._opener = mock.Mock()
        redirect = urllib.error.HTTPError("https://api.github.com/x", 302, "Found",
                                          {"Location": "https://release-assets.githubusercontent.com/test?signature=x"}, None)
        api._opener.open.side_effect = [redirect, io.BytesIO(b"x")]
        self.assertEqual(api.asset_digest({"id": 1, "size": 1}), hashlib.sha256(b"x").hexdigest())
        request = api._opener.open.call_args_list[1].args[0]
        self.assertIsNone(request.get_header("Authorization"))

    def test_unknown_redirect_host_is_rejected(self):
        api = release.GitHub("synthetic-secret")
        api._opener = mock.Mock()
        api._opener.open.side_effect = urllib.error.HTTPError("https://api.github.com/x", 302, "Found",
                                                             {"Location": "https://untrusted.invalid/test"}, None)
        with self.assertRaises(release.ApiError):
            api.asset_digest({"id": 1, "size": 1})
        self.assertEqual(api._opener.open.call_count, 1)

    def test_wrong_repository_endpoint_is_rejected(self):
        api = release.GitHub("synthetic-secret")
        api._opener = mock.Mock()
        with self.assertRaisesRegex(ValueError, "authorized repository"):
            api.request("POST", "/repos/someone/else/releases", {"draft": True})
        api._opener.open.assert_not_called()

    def test_gh_stderr_cannot_leak_credentials(self):
        completed = subprocess.CompletedProcess(["gh"], 1, stdout="synthetic-secret", stderr="synthetic-secret")
        with mock.patch.dict(os.environ, {"GH_TOKEN": "", "GITHUB_TOKEN": ""}), \
                mock.patch.object(release.shutil, "which", return_value="gh"), \
                mock.patch.object(release.subprocess, "run", return_value=completed):
            with self.assertRaises(ValueError) as error:
                release.authentication()
        self.assertNotIn("synthetic-secret", str(error.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
