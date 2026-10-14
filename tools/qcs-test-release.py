#!/usr/bin/env python3
"""Release package and upload contracts. Synthetic fixtures never represent release acceptance."""
from copy import deepcopy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock
import urllib.error
import zipfile

sys.dont_write_bytecode = True
spec = importlib.util.spec_from_file_location("qcs_release", Path(__file__).with_name("qcs-release.py"))
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)
evidence = release.module("qcs-release-evidence")
CANDIDATE = "1" * 40
IDENTITY = {"candidateHead": CANDIDATE, "checkoutCommit": CANDIDATE, "sourceTree": "2" * 40,
            "workingTreeModified": False, "lockHashes": {name: "3" * 64 for name in evidence.LOCKS}}


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
    """Synthetic GitHub JSON and ZIP bytes exercise the real eleven-job selector."""
    def __init__(self):
        self.repository = {"id": 42, "full_name": release.REPOSITORY, "archived": False}
        self.workflow = {"id": 71, "path": release.WORKFLOW_PATH}
        self.run = {"id": 123, "head_sha": CANDIDATE, "workflow_id": 71, "run_attempt": 1,
                    "path": release.WORKFLOW_PATH, "repository": deepcopy(self.repository),
                    "status": "completed", "conclusion": "success"}
        self.attempt = deepcopy(self.run)
        self.jobs, self.artifacts, self.downloads, self.calls = [], [], {}, []
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
        content = io.BytesIO()
        with zipfile.ZipFile(content, "w") as archive:
            for name in sorted(release.package_names(configuration, release.project_version(release.SOURCE), CANDIDATE, attempt)):
                archive.writestr(name, b"SYNTHETIC USER PACKAGE: " + name.encode())
        artifact_id = identifier + 1000
        self.downloads[artifact_id] = content.getvalue()
        self.artifacts.append({"id": artifact_id, "name": release.selector_module().artifact_name(configuration, CANDIDATE, attempt),
            "expired": False, "digest": "sha256:" + hashlib.sha256(content.getvalue()).hexdigest(),
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
        if endpoint.startswith(prefix + "/actions/runs/123/attempts/"):
            return deepcopy(self.attempt)
        for kind in ("jobs", "artifacts"):
            if endpoint.startswith(prefix + "/actions/runs/123/" + kind + "?"):
                rows = getattr(self, kind)
                page = int(urllib.parse.parse_qs(urllib.parse.urlsplit(endpoint).query)["page"][0])
                return {"total_count": len(rows), kind: deepcopy(rows[(page - 1) * 100:page * 100])}
        raise AssertionError(endpoint)

    def download_artifact(self, artifact_id, destination, expected_digest, *, max_bytes):
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
        self.assertEqual(len(list((relocated / "provenance/artifacts").glob("*.zip"))), 11)
        self.assertFalse(document["published"])

    def test_partial_retry_uses_original_attempt_in_package_names(self):
        self.api.partial_retry()
        document = release.read_json(self.prepare(attempt=2))
        self.assertEqual(document["configurations"]["windows-x64-cpu"]["sourceAttempt"], 2)
        self.assertEqual(document["configurations"]["linux-x64-cpu"]["sourceAttempt"], 1)
        package = document["configurations"]["linux-x64-cpu"]["packages"][0]["filename"]
        self.assertIn("-attempt1.", package)

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
        self.api.downloads[1001] += b"changed"
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

    def test_self_consistent_package_metadata_cannot_replace_original_artifact(self):
        manifest = self.prepare()
        document = release.read_json(manifest)
        item = document["assets"][0]
        path = manifest.parent / item["path"]
        path.write_bytes(b"self-consistent replacement")
        item.update(bytes=path.stat().st_size, sha256=release.digest(path))
        for entry in document["files"]:
            if entry["path"] == item["path"]:
                entry.update(bytes=item["bytes"], sha256=item["sha256"])
        release.write_json(manifest, document)
        with self.assertRaisesRegex(ValueError, "accepted source or CI provenance"):
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


class CiCpuOriginTests(ExternalTest):
    def setUp(self):
        super().setUp()
        self.api = FakeCI()
        self.origin = self.root / "candidate/ci"
        patch = mock.patch.object(release, "identity_for", return_value=IDENTITY)
        patch.start()
        self.addCleanup(patch.stop)

    def create_origin(self, attempt=1):
        collected = release.collect_ci(self.api, CANDIDATE, "123", attempt)
        selected = collected["selection"]["selected"][release.CPU_VARIANT]
        artifact = self.file("candidate/ci/artifacts/" + str(selected["artifactId"]) + ".zip",
                             self.api.downloads[selected["artifactId"]])
        github = self.origin / "github.json"
        release.write_json(github, collected)
        names = release.package_names(release.CPU_VARIANT, release.project_version(release.SOURCE),
                                      CANDIDATE, selected["sourceAttempt"])
        filename = next(name for name in names if name.endswith(".zip"))
        with zipfile.ZipFile(artifact) as archive:
            package = self.file("candidate/ci/package/" + filename, archive.read(filename))
        self.document = {"schemaVersion": 1, "kind": "ci-cpu-package-origin", "repository": release.REPOSITORY,
                         "identity": deepcopy(IDENTITY), "version": release.project_version(release.SOURCE),
                         "runId": "123", "requestedAttempt": attempt, "variant": release.CPU_VARIANT,
                         "selected": selected, "githubSnapshot": release.record(self.origin, github),
                         "artifact": release.record(self.origin, artifact, digest=selected["artifactDigest"]),
                         "package": release.record(self.origin, package, filename=filename)}
        self.save()
        return self.document

    def save(self):
        release.write_json(self.origin / release.CI_CPU_ORIGIN, self.document)

    def verify(self):
        with mock.patch.object(release, "GitHub", side_effect=AssertionError("Verification must remain offline")), \
                mock.patch.object(release, "authentication", side_effect=AssertionError("No credentials permitted")):
            return release.verify_ci_cpu_origin(self.origin, self.document, release.SOURCE)

    def replace_snapshot(self, collected):
        path = self.origin / "github.json"
        release.write_json(path, collected)
        self.document["githubSnapshot"] = release.record(self.origin, path)
        self.save()

    def test_subset_replays_all_eleven_without_other_artifact_files(self):
        self.create_origin()
        # Surrounding candidate evidence is intentionally outside the closed ci/ tree.
        self.file("candidate/package-verification.json")
        result = self.verify()
        self.assertEqual(result["identity"], IDENTITY)
        self.assertEqual(result["selected"], self.document["selected"])
        self.assertEqual(result["package"]["path"], self.origin / self.document["package"]["path"])
        self.assertEqual(len(list((self.origin / "artifacts").glob("*.zip"))), 1)
        self.assertEqual(len(release.read_json(self.origin / "github.json")["selection"]["selected"]), 11)
        self.assertTrue(all(result[key]["path"].is_absolute() for key in ("package", "artifact", "githubSnapshot")))

    def test_origin_can_move_without_original_freeze(self):
        self.create_origin()
        relocated = self.root / "moved"
        self.origin.rename(relocated)
        self.origin = relocated
        self.assertEqual(self.verify()["package"]["sha256"], self.document["package"]["sha256"])

    def test_complete_frozen_cpu_resolver_checks_other_variants_first(self):
        source_bundle = self.file(f"qcs-source-materials-{CANDIDATE[:12]}.zip", b"SYNTHETIC SOURCES")
        with mock.patch.object(release, "build_sources", return_value=source_bundle), \
                mock.patch.object(release, "validate_sources"):
            manifest = release.prepare(release.SOURCE, CANDIDATE, "123", 1, self.root / "freeze", api=self.api)
            result = release.verify_frozen_cpu_package(manifest, release.SOURCE)
            self.assertEqual(result["selected"]["artifactId"], 1001)
            self.assertEqual(result["package"]["sha256"], release.digest(result["package"]["path"]))
            self.assertEqual(result["artifact"]["digest"], "sha256:" + result["artifact"]["sha256"])
            (manifest.parent / "provenance/artifacts/1002.zip").write_bytes(b"changed other variant")
            with self.assertRaisesRegex(ValueError, "content changed"):
                release.verify_frozen_cpu_package(manifest, release.SOURCE)

    def test_wrong_origin_candidate_run_attempt_variant_or_source_rejected(self):
        self.create_origin()
        original = deepcopy(self.document)
        mutations = [("identity", {**IDENTITY, "candidateHead": "9" * 40}),
                     ("identity", {**IDENTITY, "workingTreeModified": True}),
                     ("identity", {**IDENTITY, "sourceTree": "9" * 40}),
                     ("repository", "another/repository"), ("runId", "124"),
                     ("requestedAttempt", 2), ("variant", "windows-x64-vulkan"), ("version", "9.9.9")]
        for key, value in mutations:
            with self.subTest(key=key, value=value):
                self.document = deepcopy(original)
                self.document[key] = value
                self.save()
                with self.assertRaises(ValueError):
                    self.verify()

    def test_origin_requires_current_clean_identity(self):
        self.create_origin()
        with mock.patch.object(release, "identity_for", return_value={**IDENTITY, "workingTreeModified": True}):
            with self.assertRaisesRegex(ValueError, "current clean candidate"):
                self.verify()

    def test_source_identity_is_rechecked_after_package_verification(self):
        self.create_origin()
        with mock.patch.object(release, "identity_for", side_effect=[IDENTITY, {**IDENTITY, "workingTreeModified": True}]):
            with self.assertRaisesRegex(ValueError, "Source identity changed"):
                self.verify()

    def test_selected_cpu_execution_fields_must_match_snapshot(self):
        self.create_origin()
        original = deepcopy(self.document)
        for key in ("sourceJobId", "observedJobId", "artifactId", "sourceAttempt", "artifactDigest", "artifactName"):
            with self.subTest(key=key):
                self.document = deepcopy(original)
                value = self.document["selected"][key]
                self.document["selected"][key] = value + 1 if type(value) is int else value + "changed"
                self.save()
                with self.assertRaises((ValueError, FileNotFoundError)):
                    self.verify()

    def test_non_cpu_latest_failure_cannot_be_ignored(self):
        self.create_origin()
        collected = release.read_json(self.origin / "github.json")
        collected["jobs"][-1]["conclusion"] = "failure"
        self.replace_snapshot(collected)
        with self.assertRaisesRegex(ValueError, "Latest native job"):
            self.verify()

    def test_non_cpu_missing_job_or_artifact_invalidates_subset(self):
        self.create_origin()
        original = release.read_json(self.origin / "github.json")
        for key in ("jobs", "artifacts"):
            with self.subTest(key=key):
                collected = deepcopy(original)
                collected[key].pop()
                self.replace_snapshot(collected)
                with self.assertRaisesRegex(ValueError, "selection failed"):
                    self.verify()

    def test_saved_selection_and_exclusions_must_equal_replay(self):
        self.create_origin()
        original = release.read_json(self.origin / "github.json")
        for field in ("selected", "excluded"):
            with self.subTest(field=field):
                collected = deepcopy(original)
                if field == "selected":
                    collected["selection"][field].pop("macos-arm64-metal")
                else:
                    collected["selection"][field].append({"artifactId": 9999})
                self.replace_snapshot(collected)
                with self.assertRaisesRegex(ValueError, "complete execution history"):
                    self.verify()

    def test_copied_cpu_job_retains_original_attempt_package(self):
        self.api.run["run_attempt"] = self.api.attempt["run_attempt"] = 2
        for index, job in enumerate(deepcopy(self.api.jobs), 101):
            job.update(id=index, run_attempt=2, created_at="2026-01-01T03:00:00Z")
            self.api.jobs.append(job)
        self.create_origin(attempt=2)
        result = self.verify()
        self.assertEqual(result["requestedAttempt"], 2)
        self.assertEqual(result["selected"]["sourceAttempt"], 1)
        self.assertEqual(result["selected"]["sourceJobId"], 1)
        self.assertEqual(result["selected"]["observedJobId"], 101)
        self.assertIn("-attempt1.zip", result["package"]["filename"])

    def test_snapshot_context_candidate_repository_workflow_must_match(self):
        self.create_origin()
        original = release.read_json(self.origin / "github.json")
        for scope, key, value in (("run", "head_sha", "9" * 40), ("run", "run_attempt", 2),
                                  ("attempt", "conclusion", "failure"), ("workflow", "path", "other.yml"),
                                  ("repository", "full_name", "another/repository")):
            with self.subTest(scope=scope, key=key):
                collected = deepcopy(original)
                collected["context"][scope][key] = value
                self.replace_snapshot(collected)
                with self.assertRaises(ValueError):
                    self.verify()

    def test_each_bound_file_mutation_rejected(self):
        self.create_origin()
        for key in ("githubSnapshot", "artifact", "package"):
            with self.subTest(key=key):
                path = self.origin / self.document[key]["path"]
                content = path.read_bytes()
                path.write_bytes(content + b"changed")
                with self.assertRaisesRegex(ValueError, "file bytes changed"):
                    self.verify()
                path.write_bytes(content)

    def test_rewritten_package_manifest_cannot_replace_artifact_member(self):
        self.create_origin()
        package = self.origin / self.document["package"]["path"]
        package.write_bytes(b"self-consistent substituted ZIP")
        self.document["package"] = release.record(self.origin, package, filename=package.name)
        self.save()
        with self.assertRaisesRegex(ValueError, "original artifact member"):
            self.verify()

    def test_rewritten_artifact_record_cannot_replace_github_digest(self):
        self.create_origin()
        artifact = self.origin / self.document["artifact"]["path"]
        artifact.write_bytes(artifact.read_bytes() + b"changed")
        self.document["artifact"] = release.record(self.origin, artifact, digest="sha256:" + release.digest(artifact))
        self.save()
        with self.assertRaisesRegex(ValueError, "digest differs from GitHub"):
            self.verify()

    def test_artifact_digest_declaration_must_equal_selected_digest(self):
        self.create_origin()
        self.document["artifact"]["digest"] = "sha256:" + "9" * 64
        self.save()
        with self.assertRaisesRegex(ValueError, "digest differs from GitHub"):
            self.verify()

    def test_missing_installer_extra_package_and_case_duplicate_rejected(self):
        self.create_origin()
        original = deepcopy(self.document)
        snapshot = release.read_json(self.origin / "github.json")
        artifact = self.origin / self.document["artifact"]["path"]
        names = sorted(release.package_names(release.CPU_VARIANT, self.document["version"], CANDIDATE, 1))
        for members in ([names[1]], [*names, "report.json"], [*names, names[1].upper()], [*names, "../outside"]):
            with self.subTest(members=members):
                self.document = deepcopy(original)
                with zipfile.ZipFile(artifact, "w") as archive:
                    for name in members:
                        archive.writestr(name, b"SYNTHETIC USER PACKAGE: " + name.encode())
                value = "sha256:" + release.digest(artifact)
                collected = deepcopy(snapshot)
                collected["artifacts"][0]["digest"] = value
                collected["selection"]["selected"][release.CPU_VARIANT]["artifactDigest"] = value
                self.document["selected"]["artifactDigest"] = value
                self.document["artifact"] = release.record(self.origin, artifact, digest=value)
                self.replace_snapshot(collected)
                with self.assertRaisesRegex(ValueError, "extra user packages"):
                    self.verify()

    def test_extra_file_and_empty_directory_are_rejected(self):
        self.create_origin()
        extra = self.origin / "extra"
        extra.write_text("unexpected")
        with self.assertRaisesRegex(ValueError, "extra files"):
            self.verify()
        extra.unlink()
        extra.mkdir()
        with self.assertRaisesRegex(ValueError, "unexpected directories"):
            self.verify()

    def test_root_or_parent_reparse_point_is_rejected(self):
        self.create_origin()
        original = release.redirected
        for redirected_root in (self.origin, self.origin.parent):
            with self.subTest(path=redirected_root), \
                    mock.patch.object(release, "redirected", side_effect=lambda path: path == redirected_root or original(path)):
                with self.assertRaisesRegex(ValueError, "links or junctions"):
                    self.verify()

    def test_inner_reparse_point_is_rejected(self):
        self.create_origin()
        original = release.redirected
        target = self.origin / "package"
        with mock.patch.object(release, "redirected", side_effect=lambda path: path == target or original(path)):
            with self.assertRaisesRegex(ValueError, "links or junctions"):
                self.verify()

    def test_path_traversal_or_alias_in_record_is_rejected(self):
        self.create_origin()
        original = deepcopy(self.document)
        for key in ("githubSnapshot", "artifact", "package"):
            for path in ("../outside", "C:/outside", "package/../github.json", "package\\file", "/outside"):
                with self.subTest(key=key, path=path):
                    self.document = deepcopy(original)
                    self.document[key]["path"] = path
                    self.save()
                    with self.assertRaisesRegex(ValueError, "identity or path"):
                        self.verify()

    def test_saved_origin_must_match_supplied_document(self):
        self.create_origin()
        path = self.origin / release.CI_CPU_ORIGIN
        saved = deepcopy(self.document)
        saved["runId"] = "999"
        release.write_json(path, saved)
        with self.assertRaisesRegex(ValueError, "saved bytes"):
            self.verify()

    def test_schema_and_numeric_booleans_are_rejected(self):
        self.create_origin()
        original = deepcopy(self.document)
        for key in ("schemaVersion", "requestedAttempt"):
            with self.subTest(key=key):
                self.document = deepcopy(original)
                self.document[key] = True
                self.save()
                with self.assertRaises(ValueError):
                    self.verify()
        self.document = deepcopy(original)
        self.document["package"]["bytes"] = True
        self.save()
        with self.assertRaisesRegex(ValueError, "identity or path"):
            self.verify()
        for key in ("sourceAttempt", "sourceJobId", "observedJobId", "artifactId"):
            with self.subTest(key=key):
                self.document = deepcopy(original)
                self.document["selected"][key] = True
                self.save()
                with self.assertRaisesRegex(ValueError, "object ID"):
                    self.verify()

    def test_malformed_documents_and_snapshot_fail_with_validation_error(self):
        self.create_origin()
        original = deepcopy(self.document)
        for bad in (None, [], {}, {**original, "selected": []}, {**original, "githubSnapshot": []},
                    {**original, "identity": None}, {**original, "package": {}}):
            with self.subTest(document=bad):
                with self.assertRaises(ValueError):
                    release.verify_ci_cpu_origin(self.origin, bad, release.SOURCE)
        snapshot = release.read_json(self.origin / "github.json")
        for bad in (None, [], {}, {**snapshot, "context": []}):
            with self.subTest(snapshot=bad):
                self.replace_snapshot(bad)
                with self.assertRaises(ValueError):
                    self.verify()


class PackageArchiveTests(ExternalTest):
    def archive(self, names):
        path = self.root / "artifact.zip"
        with zipfile.ZipFile(path, "w") as archive:
            for name in names:
                archive.writestr(name, b"fixture package")
        return path

    def test_wrong_backend_missing_or_extra_payload_rejected(self):
        expected = {"qcs-0.1.0-windows-x64-cpu-111111111111-attempt1.exe"}
        for names in ([], ["qcs-0.1.0-windows-x64-cuda-13.4-111111111111-attempt1.exe"],
                      [*expected, "report.json"], ["../outside"], ["folder/file.zip"]):
            with self.subTest(names=names), self.assertRaises(ValueError):
                release.inspect_packages(self.archive(names), expected)

    def test_symlink_package_rejected(self):
        path = self.root / "linked.zip"
        with zipfile.ZipFile(path, "w") as archive:
            info = zipfile.ZipInfo("qcs.zip")
            info.external_attr = 0o120777 << 16
            archive.writestr(info, "outside")
        with self.assertRaisesRegex(ValueError, "ordinary"):
            release.inspect_packages(path, {"qcs.zip"})

    def test_rocm_setup_zip_replaces_installer_without_replacing_portable_package(self):
        options = release.package_name_options("windows-x64-rocm", "0.1.0", CANDIDATE, 1)
        self.assertEqual(len(options), 2)
        self.assertEqual(len(options[0] & options[1]), 1)
        inspected = release.inspect_packages(self.archive(options[1]), options)
        self.assertTrue(any(name.endswith("-setup.zip") for name in inspected))
        with self.assertRaisesRegex(ValueError, "extra user packages"):
            release.inspect_packages(self.archive(options[0] | options[1]), options)
        cpu = release.package_name_options("windows-x64-cpu", "0.1.0", CANDIDATE, 1)
        self.assertEqual(len(cpu), 1)

    def test_single_release_asset_limit_is_strictly_below_two_gib(self):
        release.require_asset_size(2 * 1024**3 - 1, "fixture.zip")
        for size in (0, -1, True, 2 * 1024**3):
            with self.subTest(size=size), self.assertRaises(ValueError):
                release.require_asset_size(size, "fixture.zip")

    def test_paths_and_outputs_cannot_escape(self):
        for name in ("../outside", "a/../../outside", "C:/outside", "a\\outside", "a\nname"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                release.safe_file(self.root, name)


class FakeGitHub:
    def __init__(self):
        self.release = None
        self.assets = []
        self.uploaded = []
        self.calls = []
        self.tag_sha = None
        self.fail_next = None

    def request(self, method, endpoint, data=None):
        self.calls.append((method, endpoint, data))
        prefix = "/repos/" + release.REPOSITORY
        if endpoint == prefix:
            return {"id": 42, "full_name": release.REPOSITORY, "archived": False}
        if endpoint == prefix + "/commits/" + CANDIDATE:
            return {"sha": CANDIDATE}
        if "/git/ref/tags/" in endpoint:
            if not self.tag_sha:
                raise release.ApiError(404)
            return {"object": {"type": "commit", "sha": self.tag_sha}}
        if method == "POST" and endpoint == prefix + "/releases":
            self.release = {**data, "id": 17, "published_at": None}
            return deepcopy(self.release)
        if endpoint.startswith(prefix + "/releases?"):
            return [deepcopy(self.release)] if self.release else []
        if endpoint.startswith(prefix + "/releases/17/assets?"):
            return deepcopy(self.assets)
        if endpoint == prefix + "/releases/17":
            return deepcopy(self.release)
        raise AssertionError((method, endpoint, data))

    def asset_digest(self, asset):
        return asset["digest"].removeprefix("sha256:")

    def upload(self, release_id, path, name):
        if name == self.fail_next:
            self.fail_next = None
            raise release.ApiError(503)
        self.uploaded.append(name)
        result = {"id": len(self.assets) + 1, "name": name, "state": "uploaded", "size": path.stat().st_size,
                  "digest": "sha256:" + release.digest(path)}
        self.assets.append(result)
        return deepcopy(result)


class UploadTests(ExternalTest):
    def setUp(self):
        super().setUp()
        self.manifest = self.file(release.MANIFEST, b'{"synthetic":true}')
        self.first = self.file("assets/qcs-a.zip", b"first")
        self.second = self.file("assets/qcs-b.zip", b"second")
        self.document = {"identity": IDENTITY, "runId": "123", "requestedAttempt": 1, "assets": [release.record(self.root, item, name=item.name)
                                                         for item in (self.first, self.second)]}
        self.api = FakeGitHub()
        release.write_json(self.root / "provenance/github.json", {"selection": {"synthetic": True}})
        current = mock.patch.object(release, "collect_ci", return_value={"selection": {"synthetic": True}})
        self.collect = current.start()
        self.addCleanup(current.stop)
        patch = mock.patch.object(release, "verify_freeze", return_value=self.document)
        self.verify = patch.start()
        self.addCleanup(patch.stop)
        identity_patch = mock.patch.object(release, "identity_for", return_value=IDENTITY)
        identity_patch.start()
        self.addCleanup(identity_patch.stop)

    def upload(self):
        return release.upload_draft(self.manifest, release.SOURCE, "v0.0-test", api=self.api)

    def test_create_and_reuse_draft_checks_remote_hashes(self):
        first = self.upload()
        second = self.upload()
        self.assertEqual(len(first["uploaded"]), 3)
        self.assertEqual(second["uploaded"], [])
        self.assertEqual(len(second["reused"]), 3)
        self.assertEqual(len(self.api.uploaded), 3)
        self.assertTrue(self.api.release["draft"])
        self.assertFalse(first["published"])
        self.assertEqual(self.verify.call_count, 2)

    def test_partial_upload_resumes_without_replacing_completed_asset(self):
        self.api.fail_next = "qcs-b.zip"
        with self.assertRaises(release.ApiError):
            self.upload()
        self.assertEqual(self.api.uploaded, ["qcs-a.zip"])
        result = self.upload()
        self.assertEqual(result["reused"], ["qcs-a.zip"])
        self.assertEqual(self.api.uploaded.count("qcs-a.zip"), 1)
        self.assertTrue(self.api.release["draft"])

    def test_same_size_different_hash_is_not_treated_as_already_uploaded(self):
        self.upload()
        self.api.assets[0]["digest"] = "sha256:" + "a" * 64
        before = list(self.api.uploaded)
        with self.assertRaisesRegex(ValueError, "No asset was overwritten"):
            self.upload()
        self.assertEqual(self.api.uploaded, before)

    def test_published_release_is_rejected_before_upload(self):
        self.upload()
        self.api.release["draft"] = False
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

    def test_local_asset_mutation_before_transfer_is_rejected(self):
        self.first.write_bytes(b"changed since freeze")
        with self.assertRaisesRegex(ValueError, "changed after upload preflight"):
            self.upload()
        self.assertEqual(self.api.uploaded, [])

    def test_failed_local_gate_prevents_all_remote_requests(self):
        self.verify.side_effect = ValueError("freeze verification failed")
        with self.assertRaisesRegex(ValueError, "freeze verification failed"):
            self.upload()
        self.assertEqual(self.api.calls, [])

    def test_oversized_asset_fails_before_any_remote_request(self):
        self.document["assets"][0]["bytes"] = 2 * 1024**3
        with self.assertRaisesRegex(ValueError, "smaller than 2 GiB"):
            self.upload()
        self.assertEqual(self.api.calls, [])

    def test_new_failed_ci_attempt_prevents_remote_release_creation(self):
        self.collect.side_effect = ValueError("The latest workflow attempt must have completed successfully")
        with self.assertRaisesRegex(ValueError, "latest workflow attempt"):
            self.upload()
        self.assertIsNone(self.api.release)
        self.assertEqual(self.api.uploaded, [])

    def test_changed_remote_selection_prevents_remote_release_creation(self):
        self.collect.return_value = {"selection": {"different": True}}
        with self.assertRaisesRegex(ValueError, "changed after release preparation"):
            self.upload()
        self.assertIsNone(self.api.release)


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


class SourceMaterialTests(ExternalTest):
    def setUp(self):
        super().setUp()
        self.package = {"name": "fixture", "url": "https://example.invalid/source.zip", "sha256": "a" * 64}
        self.manifest = {"commit": CANDIDATE, "workingTreeModified": False,
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
        for key, value in (("commit", "2" * 40), ("workingTreeModified", True)):
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
