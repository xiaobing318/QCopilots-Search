#!/usr/bin/env python3
"""Fast release-tool contracts. Synthetic fixtures never represent release acceptance."""
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


class FreezeTests(ExternalTest):
    def setUp(self):
        super().setUp()
        self.native, self.local = self.root / "native", self.root / "local"
        for platform_id in evidence.PLATFORMS:
            root = self.native / platform_id
            for relative in ("pkg/qcs-example.zip", "pkg/qcs-source-manifest.json", "pkg/qcs-source-materials-example.zip",
                             "pkg/qcs-symbols-example.zip", "reports/package/runtime-license-materials/LICENSE"):
                self.file(root.relative_to(self.root) / relative, (platform_id + ": " + relative).encode())
            document = {"identity": IDENTITY, "platformId": platform_id,
                        "files": [release.record(root, path) for path in sorted(root.rglob("*")) if path.is_file()]}
            release.write_json(root / "reports/native-evidence.json", document)
        self.file("local/candidate/qcs-example.zip", b"distinct local Computer Use package")
        release.write_json(self.local / "candidate/candidate.json", {"package": "qcs-example.zip"})
        release.write_json(self.local / "manifest.json", {"identity": IDENTITY,
            "units": {"candidate": "candidate/candidate.json"},
            "files": [release.record(self.local, path) for path in sorted(self.local.rglob("*")) if path.is_file()]})
        full = self.file("full/reports/run/ctest.xml")
        self.full_report = self.root / "full/reports/run/check-report.json"
        release.write_json(self.full_report, {"reportPath": "reports/run/check-report.json", "identity": IDENTITY,
            "files": [release.record(self.root / "full", full)]})
        self.gate = {"passed": True, "errors": [], "kind": "release-preflight", "candidateHead": CANDIDATE}
        self.evidence = types.SimpleNamespace(verify_manifest=evidence.verify_manifest, PLATFORMS=evidence.PLATFORMS,
                                             discover_native=evidence.discover_native,
                                             preflight=mock.Mock(side_effect=lambda *args: deepcopy(self.gate)))
        self.full_passed = True

        def inspect_check(path, head):
            document = release.read_json(path)
            root = path.parents[len(Path(document["reportPath"]).parts) - 1]
            errors = evidence.verify_manifest(root, document)
            return {"passed": self.full_passed and not errors, "errors": errors,
                    "identity": document["identity"], "files": document["files"]}

        self.dev = types.SimpleNamespace(inspect_check=mock.Mock(side_effect=inspect_check))
        loader = lambda name: self.evidence if name == "qcs-release-evidence" else self.dev if name == "qcs-dev" else None
        for patch in (mock.patch.object(release, "module", side_effect=loader),
                      mock.patch.object(release, "identity_for", return_value=IDENTITY),
                      mock.patch.object(release, "validate_materials")):
            patch.start()
            self.addCleanup(patch.stop)

    def prepare(self):
        return release.prepare(release.SOURCE, CANDIDATE, self.native, self.local,
                               self.full_report, self.root / "frozen")

    def test_prepare_and_portable_reverification_bind_every_asset(self):
        path = self.prepare()
        document = release.verify_freeze(path, release.SOURCE)
        names = {item["name"] for item in document["assets"]}
        self.assertIn("qcs-local-windows-x64-example.zip", names)
        self.assertIn("qcs-windows-x64-example.zip", names)
        self.assertIn("qcs-macos-arm64-source-manifest.json", names)
        self.assertIn("qcs-linux-x64-licenses.zip", names)
        self.assertGreaterEqual(self.evidence.preflight.call_count, 3)
        self.assertGreaterEqual(self.dev.inspect_check.call_count, 4)
        self.assertFalse(document["published"])

    def test_incomplete_preflight_never_creates_a_frozen_candidate(self):
        self.gate["passed"] = False
        self.gate["errors"] = ["native platform missing"]
        with self.assertRaisesRegex(ValueError, "native platform missing"):
            self.prepare()
        self.assertFalse((self.root / "frozen").exists())

    def test_local_full_gate_is_required(self):
        self.full_passed = False
        with self.assertRaisesRegex(ValueError, "Local full check failed"):
            self.prepare()
        self.assertFalse((self.root / "frozen").exists())

    def test_assets_changed_after_prepare_are_rejected(self):
        path = self.prepare()
        (path.parent / "assets/qcs-windows-x64-example.zip").write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "content changed"):
            release.verify_freeze(path, release.SOURCE)

    def test_collection_rejects_changed_bound_inputs(self):
        self.file("native/windows-x64/pkg/qcs-example.zip", b"changed")
        with self.assertRaisesRegex(ValueError, "changed evidence"):
            self.prepare()
        self.assertFalse((self.root / "frozen").exists())

    def test_existing_output_is_not_overwritten(self):
        path = self.prepare()
        checksum = release.digest(path)
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.prepare()
        self.assertEqual(release.digest(path), checksum)

    def test_asset_hash_claims_cannot_replace_provenance(self):
        path = self.prepare()
        document = release.read_json(path)
        item = next(item for item in document["assets"] if item["name"] == "qcs-windows-x64-example.zip")
        (path.parent / item["path"]).write_bytes(b"replacement")
        item["sha256"] = release.digest(path.parent / item["path"])
        with self.assertRaisesRegex(ValueError, "accepted provenance"):
            release.validate_asset_origins(path.parent, release.SOURCE, document,
                                           evidence.discover_native(path.parent / "evidence/native"))

    def test_asset_list_cannot_omit_a_platform_package(self):
        path = self.prepare()
        document = release.read_json(path)
        document["assets"] = [item for item in document["assets"] if item["name"] != "qcs-linux-x64-example.zip"]
        with self.assertRaisesRegex(ValueError, "omits or adds"):
            release.validate_asset_origins(path.parent, release.SOURCE, document,
                                           evidence.discover_native(path.parent / "evidence/native"))


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
        self.document = {"identity": IDENTITY, "assets": [release.record(self.root, item, name=item.name)
                                                         for item in (self.first, self.second)]}
        self.api = FakeGitHub()
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
        self.verify.side_effect = ValueError("full gate failed")
        with self.assertRaisesRegex(ValueError, "full gate failed"):
            self.upload()
        self.assertEqual(self.api.calls, [])

    def test_oversized_asset_fails_before_any_remote_request(self):
        self.document["assets"][0]["bytes"] = 2 * 1024**3
        with self.assertRaisesRegex(ValueError, "smaller than 2 GiB"):
            self.upload()
        self.assertEqual(self.api.calls, [])


class MaterialTests(ExternalTest):
    def symbols(self, change=None):
        programs = [{"name": name, "unstrippedBinarySha256": "4" * 64} for name in sorted(evidence.APPLICATIONS)]
        content = b"synthetic symbols"
        manifest = {"platform": "Windows", "architecture": "AMD64", "programs": programs,
                    "files": [{"path": program + ".pdb", "bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}
                              for program in sorted(evidence.APPLICATIONS)]}
        if change:
            change(manifest)
        path = self.root / "symbols.zip"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("qcs-symbols-manifest.json", json.dumps(manifest))
            for item in manifest["files"]:
                archive.writestr(item["path"], content)
        release.write_json(self.root / "reports/build-provenance.json",
                           {"programs": {name: {"sha256": "4" * 64} for name in evidence.APPLICATIONS}})
        return path

    def test_symbol_archive_binds_programs_to_native_build(self):
        release.validate_symbols(self.symbols(), self.root, "windows-x64", evidence)

    def test_wrong_binary_identity_is_rejected(self):
        path = self.symbols(lambda doc: doc["programs"][0].update(unstrippedBinarySha256="5" * 64))
        with self.assertRaisesRegex(ValueError, "recorded native build"):
            release.validate_symbols(path, self.root, "windows-x64", evidence)

    def test_missing_program_is_rejected(self):
        path = self.symbols(lambda doc: doc["programs"].pop())
        with self.assertRaisesRegex(ValueError, "all four"):
            release.validate_symbols(path, self.root, "windows-x64", evidence)

    def test_bad_symbol_bytes_are_rejected(self):
        path = self.symbols(lambda doc: doc["files"][0].update(sha256="0" * 64))
        with self.assertRaisesRegex(ValueError, "checksum mismatch"):
            release.validate_symbols(path, self.root, "windows-x64", evidence)

    def test_missing_debug_file_is_rejected_despite_program_entry(self):
        path = self.symbols(lambda doc: doc["files"].pop())
        with self.assertRaisesRegex(ValueError, "no debug information"):
            release.validate_symbols(path, self.root, "windows-x64", evidence)

    def test_path_traversal_and_source_outputs_are_rejected(self):
        for name in ("../outside", "a/../../outside", "C:/outside", "a\\outside", "a\nname"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                release.safe_file(self.root, name)
        with self.assertRaises(ValueError):
            release.external(release.SOURCE / "generated", release.SOURCE)

    def test_evidence_partitions_include_each_file_once_with_headroom(self):
        names = ["evidence/a", "evidence/b", "evidence/c"]
        for name in names:
            self.file(name, b"x" * 100)
        groups = release.evidence_archive_groups(self.root, reversed(names), budget=1200)
        self.assertEqual(groups, [[name] for name in names])
        with self.assertRaisesRegex(ValueError, "too large"):
            release.evidence_archive_groups(self.root, names, budget=100)

    def source_fixture(self):
        package = {"name": "synthetic-library", "url": "https://example.invalid/source.zip", "sha256": "a" * 64}
        original = self.file("project/README", b"candidate source")
        document = {"commit": CANDIDATE, "workingTreeModified": False, "platform": "Windows", "architecture": "AMD64",
                    "packages": [package], "projectFiles": [{"path": "README", "sha256": release.digest(original)}]}
        symbol = self.symbols()
        destination = self.root / "pkg/qcs-symbols-example.zip"
        destination.parent.mkdir()
        symbol.rename(destination)
        sidecar = self.root / "pkg/qcs-source-manifest.json"
        release.write_json(sidecar, document)
        bundle = self.root / "pkg/qcs-source-materials-example.zip"
        with zipfile.ZipFile(bundle, "w") as archive:
            archive.writestr("source-manifest.json", json.dumps(document))
            archive.writestr("project/README", original.read_bytes())
        release.write_json(self.root / "reports/native-evidence.json", {"files": [
            release.record(self.root, path) for path in (destination, sidecar, bundle)]})
        sources = types.SimpleNamespace(verify_bundle=mock.Mock(return_value={"passed": True}),
                                        entries=mock.Mock(return_value=[package]))
        blob_hash = lambda data: hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
        tree = b"100644 blob " + blob_hash(original.read_bytes()).encode() + b"\tREADME\0"
        for patch in (mock.patch.object(release, "module", return_value=sources),
                      mock.patch.object(release, "git_blob_identity", side_effect=lambda source, path, stream: blob_hash(stream.read())),
                      mock.patch.object(release.subprocess, "check_output", return_value=tree)):
            patch.start()
            self.addCleanup(patch.stop)
        return sidecar, document, sources

    def test_sources_match_sidecar_locks_and_checkout(self):
        _, _, sources = self.source_fixture()
        release.validate_materials(self.root, "windows-x64", IDENTITY, self.root / "project", evidence)
        sources.verify_bundle.assert_called_once()

    def test_source_archive_sidecar_mismatch_is_rejected(self):
        sidecar, document, _ = self.source_fixture()
        document["commit"] = "8" * 40
        release.write_json(sidecar, document)
        with self.assertRaisesRegex(ValueError, "source manifest disagree"):
            release.validate_materials(self.root, "windows-x64", IDENTITY, self.root / "project", evidence)

    def test_self_consistent_source_archive_must_match_locked_dependency(self):
        _, _, sources = self.source_fixture()
        sources.entries.return_value = [{"name": "synthetic-library", "url": "https://example.invalid/locked.zip", "sha256": "b" * 64}]
        with self.assertRaisesRegex(ValueError, "lock mismatch"):
            release.validate_materials(self.root, "windows-x64", IDENTITY, self.root / "project", evidence)

    def test_source_archive_project_must_match_committed_blob(self):
        self.source_fixture()
        with mock.patch.object(release.subprocess, "check_output", return_value=b"100644 blob " + b"b" * 40 + b"\tREADME\0"):
            with self.assertRaisesRegex(ValueError, "differs from the candidate"):
                release.validate_materials(self.root, "windows-x64", IDENTITY, self.root / "project", evidence)


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
