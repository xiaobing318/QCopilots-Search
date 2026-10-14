#!/usr/bin/env python3
"""Portable fixture-copy contract tests. No applications or real profiles are used."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import struct
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import uuid

sys.dont_write_bytecode = True
spec = importlib.util.spec_from_file_location("qcs_copy_cu_fixtures", Path(__file__).with_name("qcs-copy-cu-fixtures.py"))
copy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(copy)


class FixtureCopyTests(unittest.TestCase):
    def setUp(self):
        requested = os.environ.get("QCOPILOTS_SEARCH_TEST_ROOT") or None
        if requested and not Path(requested).is_dir():
            raise RuntimeError("QCOPILOTS_SEARCH_TEST_ROOT must be an existing directory")
        self.temp = tempfile.TemporaryDirectory(prefix="qcs-cu-fixtures-", dir=requested)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.sources = self.root / "中文 样本"
        (self.sources / "samples/排除目录").mkdir(parents=True)
        (self.sources / "negative-models/fixture-licenses").mkdir(parents=True)
        (self.sources / "samples/笔记.txt").write_text("canary 初始内容\n", encoding="utf-8")
        (self.sources / "samples/排除目录/不要索引.txt").write_text("excluded", encoding="utf-8")
        (self.sources / "negative-models/broken.gguf").write_bytes(b"not-a-model")
        (self.sources / "negative-models/fixture-licenses/license.txt").write_text("fixture notice", encoding="utf-8")
        self.run = self.root / "run 任意名称"
        for name in ("inputs", "profile", "models", "samples", "exports", "evidence"):
            (self.run / name).mkdir(parents=True, exist_ok=True)
        identity = {"candidateHead": "a" * 40, "checkoutCommit": "a" * 40, "workingTreeModified": False}
        candidate = {"kind": "package-candidate", "schemaVersion": 1, "identity": identity, "packageSha256": "b" * 64}
        candidate_path = self.run / "inputs/package-candidate.json"
        copy.write_json(candidate_path, candidate)
        self.state = {"schemaVersion": 1, "kind": "qcopilots-search-cu-run", "runId": str(uuid.uuid4()),
                      "identity": identity, "samplesDirectory": str(self.run / "samples"),
                      "profileRoot": str(self.run / "profile"), "modelDirectory": str(self.run / "models"),
                      "packageSha256": "b" * 64, "modelFingerprint": "fixture-model-id", "extractionFingerprint": "fixture-extraction-id",
                      "inputs": [{"path": "inputs/package-candidate.json", "bytes": candidate_path.stat().st_size,
                                  "sha256": copy.sha(candidate_path)}]}
        self.run_json = self.run / "run.json"
        copy.write_json(self.run_json, self.state)
        self.plan = self.root / "qcs-plan.json"
        copy.create_plan(self.sources, self.plan, "samples/排除目录")

    def mutate_plan(self, callback):
        plan = json.loads(self.plan.read_text(encoding="utf-8"))
        callback(plan)
        self.plan.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")

    def bind_source_manifest(self, source_root):
        retained = self.run / copy.RECORDER_SOURCE_MANIFEST
        retained.write_bytes((source_root / copy.SOURCE_MANIFEST).read_bytes())
        self.state["inputs"].append({"path": copy.RECORDER_SOURCE_MANIFEST,
                                     "bytes": retained.stat().st_size, "sha256": copy.sha(retained)})
        self.run_json.write_text(json.dumps(self.state, ensure_ascii=False), encoding="utf-8")
        return retained

    def prepare_bound_source(self):
        # A small complete prepared-source contract keeps binding edge cases
        # independent of media generation. The full generator is tested below.
        files = [{"relativePath": path.relative_to(self.sources).as_posix(),
                  "bytes": path.stat().st_size, "sha256": copy.sha(path),
                  "mtimeMs": path.stat().st_mtime_ns // 1_000_000}
                 for path in copy.tree_files(self.sources)]
        copy.write_json(self.sources / copy.SOURCE_MANIFEST,
                        {"schemaVersion": 1, "kind": "qcopilots-search-cu-fixture-source",
                         "generator": {"path": "tools/acceptance/qcs-copy-cu-fixtures.py", "sha256": copy.sha(copy.__file__)},
                         "sourceInputs": [], "sourceRoot": str(self.sources), "files": files})
        retained = self.bind_source_manifest(self.sources)
        self.plan = self.root / "bound-copy-plan.json"
        copy.create_plan(self.sources, self.plan)
        return retained

    def test_dry_run_writes_nothing_and_cannot_pass_acceptance(self):
        before = sorted(path.relative_to(self.run).as_posix() for path in self.run.rglob("*"))
        result = copy.materialize(self.run_json, self.plan)
        self.assertFalse(result["executed"])
        self.assertFalse(result["acceptancePassed"])
        self.assertEqual(result["filesToCopy"], 4)
        self.assertEqual(before, sorted(path.relative_to(self.run).as_posix() for path in self.run.rglob("*")))

    def test_copy_verifies_sources_run_and_manifest(self):
        original_run = self.run_json.read_bytes()
        originals = {path: path.read_bytes() for path in self.sources.rglob("*") if path.is_file()}
        result = copy.materialize(self.run_json, self.plan, True)
        self.assertTrue(result["complete"])
        self.assertFalse(result["acceptancePassed"])
        self.assertEqual(original_run, self.run_json.read_bytes())
        for source, value in originals.items():
            self.assertEqual(value, source.read_bytes())
            self.assertEqual(value, (self.run / source.relative_to(self.sources)).read_bytes())
        manifest = json.loads(Path(result["manifest"]).read_text(encoding="utf-8"))
        self.assertEqual(manifest["verifiedFileCount"], 4)
        self.assertTrue(manifest["sourceFilesUnchanged"])
        self.assertEqual(manifest["planSha256"], copy.sha(self.plan))

    def test_repeat_copy_fails_instead_of_overwriting(self):
        copy.materialize(self.run_json, self.plan, True)
        with self.assertRaisesRegex(ValueError, "nonempty"):
            copy.materialize(self.run_json, self.plan, True)

    def test_relocated_sources_use_explicit_override(self):
        relocated = self.root / "relocated fixtures"
        self.sources.rename(relocated)
        with self.assertRaises(ValueError):
            copy.materialize(self.run_json, self.plan)
        result = copy.materialize(self.run_json, self.plan, True, relocated)
        self.assertTrue(result["complete"])

    def test_source_changes_are_rejected_before_any_copy(self):
        (self.sources / "samples/笔记.txt").write_text("changed", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "File size differs|File SHA-256 differs"):
            copy.materialize(self.run_json, self.plan, True)
        self.assertFalse(any((self.run / "samples").iterdir()))

    def test_source_mtime_change_alone_is_rejected_before_any_copy(self):
        source = self.sources / "samples/笔记.txt"
        original = source.read_bytes()
        before = source.stat()
        os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns + 5_000_000_000))
        self.assertEqual(source.read_bytes(), original)
        with self.assertRaises(ValueError):
            copy.materialize(self.run_json, self.plan, True)
        self.assertFalse(any((self.run / "samples").iterdir()))
        self.assertFalse((self.run / "negative-models").exists())

    def test_copy_restores_planned_mtimes_and_preserves_source_bytes_and_times(self):
        paths = sorted(path for path in self.sources.rglob("*") if path.is_file())
        for index, path in enumerate(paths):
            timestamp = 1_600_000_000_000_000_000 + index * 1_000_000_000
            os.utime(path, ns=(timestamp, timestamp))
        originals = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths}
        plan_path = self.root / "qcs-fixed-times-plan.json"
        copy.create_plan(self.sources, plan_path)
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        self.assertEqual(plan["schemaVersion"], 2)
        self.assertEqual(len(plan["files"]), len(originals))
        result = copy.materialize(self.run_json, plan_path, True)
        report = json.loads(Path(result["manifest"]).read_text(encoding="utf-8"))
        copied = {row["targetRelativePath"]: row for row in report["files"]}
        for row in plan["files"]:
            with self.subTest(path=row["sourceRelativePath"]):
                source = self.sources / row["sourceRelativePath"]
                destination = self.run / row["targetRelativePath"]
                content, original_ns = originals[source]
                self.assertEqual(row["mtimeMs"], original_ns // 1_000_000)
                self.assertEqual(source.read_bytes(), content)
                self.assertEqual(source.stat().st_mtime_ns, original_ns)
                self.assertEqual(destination.read_bytes(), content)
                self.assertEqual(destination.stat().st_mtime_ns // 1_000_000, row["mtimeMs"])
                self.assertEqual(copied[row["targetRelativePath"]]["verifiedTarget"]["mtimeMs"], row["mtimeMs"])

    def test_mtime_verification_is_optional_but_detects_time_only_damage(self):
        source = self.sources / "samples/笔记.txt"
        size, digest = source.stat().st_size, copy.sha(source)
        before = source.stat()
        expected_ms = before.st_mtime_ns // 1_000_000
        copy.check_file(source, size, digest, mtime_ms=expected_ms)
        os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns + 5_000_000_000))
        copy.check_file(source, size, digest)
        with self.assertRaises(ValueError):
            copy.check_file(source, size, digest, mtime_ms=expected_ms)

    def test_plan_without_timestamp_contract_is_rejected(self):
        self.mutate_plan(lambda plan: plan.update(schemaVersion=1))
        with self.assertRaises(ValueError):
            copy.materialize(self.run_json, self.plan, True)
        self.assertFalse(any((self.run / "samples").iterdir()))

    def test_plan_timestamp_tampering_is_rejected_before_copy(self):
        self.mutate_plan(lambda plan: plan["files"][0].update(mtimeMs=0))
        with self.assertRaises(ValueError):
            copy.materialize(self.run_json, self.plan, True)
        self.assertFalse(any((self.run / "samples").iterdir()))

    def test_generated_source_materializes_into_a_fresh_recorder_run(self):
        generated = self.root / "generated 中文 source"
        prepared = copy.prepare_source(generated)
        source_manifest = json.loads(Path(prepared["manifest"]).read_text(encoding="utf-8"))
        retained = self.bind_source_manifest(generated)
        plan_path = self.root / "generated-plan.json"
        copy.create_plan(generated, plan_path)
        result = copy.materialize(self.run_json, plan_path, True)
        self.assertTrue(result["complete"])
        self.assertFalse(result["acceptancePassed"])
        browse = self.run / copy.BROWSE_ROOT
        self.assertEqual(sum(path.is_file() for path in browse.rglob("*")), 205)
        self.assertEqual((self.run / copy.SOURCE_MANIFEST).read_bytes(), Path(prepared["manifest"]).read_bytes())
        for row in source_manifest["files"]:
            with self.subTest(path=row["relativePath"]):
                copied = self.run / row["relativePath"]
                copy.check_file(copied, row["bytes"], row["sha256"], mtime_ms=row["mtimeMs"])
        self.assertFalse(any((self.run / "profile").iterdir()))
        self.assertFalse(any((self.run / "models").iterdir()))
        report = json.loads(Path(result["manifest"]).read_text(encoding="utf-8"))
        self.assertEqual(report["sourceRoot"], str(generated))
        self.assertEqual(report["sourceManifest"], {"relativePath": copy.SOURCE_MANIFEST,
                         "bytes": retained.stat().st_size, "sha256": copy.sha(retained)})
        self.assertEqual(report["recorderFixtureManifest"], self.state["inputs"][-1])

    def test_bound_manifest_identity_cannot_be_replaced_with_identical_content_files(self):
        retained = self.prepare_bound_source()
        source = self.sources / copy.SOURCE_MANIFEST
        manifest = json.loads(source.read_text(encoding="utf-8"))
        before = {row["relativePath"]: (row["bytes"], row["sha256"]) for row in manifest["files"]}
        manifest["sourceRoot"] = str(self.root / "different prepared source identity")
        source.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        self.assertNotEqual(copy.sha(source), copy.sha(retained))
        self.assertEqual(before, {row["relativePath"]: (row["bytes"], row["sha256"]) for row in manifest["files"]})
        self.plan = self.root / "different-identity-plan.json"
        copy.create_plan(self.sources, self.plan)
        with self.assertRaisesRegex(ValueError, "differs from the recorder-bound"):
            copy.materialize(self.run_json, self.plan, True)
        self.assertFalse(any((self.run / "samples").iterdir()))
        self.assertFalse((self.run / "negative-models").exists())

    def test_bound_manifest_hash_is_checked_before_copy(self):
        retained = self.prepare_bound_source()
        retained.write_bytes(retained.read_bytes() + b" ")
        with self.assertRaisesRegex(ValueError, "File size differs"):
            copy.materialize(self.run_json, self.plan, True)
        self.assertFalse(any((self.run / "samples").iterdir()))

    def test_bound_source_requires_complete_unmapped_plan(self):
        self.prepare_bound_source()
        original = self.plan.read_bytes()
        for mutate in (lambda plan: plan["files"].pop(),
                       lambda plan: plan["files"][0].update(targetRelativePath="samples/remapped.txt"),
                       lambda plan: plan["files"].remove(next(row for row in plan["files"] if row["sourceRelativePath"] == copy.SOURCE_MANIFEST))):
            with self.subTest(mutate=mutate):
                self.plan.write_bytes(original)
                self.mutate_plan(mutate)
                with self.assertRaisesRegex(ValueError, "complete source set without remapping"):
                    copy.materialize(self.run_json, self.plan, True)
                self.assertFalse(any((self.run / "samples").iterdir()))

    def test_bound_manifest_cannot_copy_a_manual_source_without_manifest(self):
        self.prepare_bound_source()
        (self.sources / copy.SOURCE_MANIFEST).unlink()
        self.plan = self.root / "manual-copy-plan.json"
        copy.create_plan(self.sources, self.plan)
        with self.assertRaisesRegex(ValueError, "requires a prepared source manifest"):
            copy.materialize(self.run_json, self.plan, True)

    def test_missing_unbound_and_duplicate_retained_manifests_are_rejected(self):
        retained = self.prepare_bound_source()
        original = retained.read_bytes()
        retained.unlink()
        with self.assertRaisesRegex(ValueError, "missing or unbound"):
            copy.materialize(self.run_json, self.plan)
        retained.write_bytes(original)
        record = self.state["inputs"].pop()
        self.run_json.write_text(json.dumps(self.state), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "missing or unbound"):
            copy.materialize(self.run_json, self.plan)
        self.state["inputs"].extend([record, record])
        self.run_json.write_text(json.dumps(self.state), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "duplicate fixture source"):
            copy.materialize(self.run_json, self.plan)

    def test_bound_manifest_is_rechecked_after_copy(self):
        retained = self.prepare_bound_source()
        real_copy = copy.shutil.copyfileobj
        changed = False

        def change_retained_during_copy(*args, **kwargs):
            nonlocal changed
            result = real_copy(*args, **kwargs)
            if not changed:
                retained.write_bytes(retained.read_bytes() + b" ")
                changed = True
            return result

        with patch.object(copy.shutil, "copyfileobj", side_effect=change_retained_during_copy):
            with self.assertRaisesRegex(ValueError, "File size differs"):
                copy.materialize(self.run_json, self.plan, True)
        self.assertFalse((self.run / copy.MANIFEST).exists())
        failure = json.loads((self.run / copy.FAILURE).read_text(encoding="utf-8"))
        self.assertFalse(failure["complete"])

    def test_bound_source_can_be_relocated_without_changing_manifest_identity(self):
        self.prepare_bound_source()
        relocated = self.root / "relocated identical source"
        self.sources.rename(relocated)
        result = copy.materialize(self.run_json, self.plan, True, relocated)
        self.assertTrue(result["complete"])
        report = json.loads(Path(result["manifest"]).read_text(encoding="utf-8"))
        self.assertEqual(report["sourceRoot"], str(relocated))

    def test_candidate_input_tampering_is_rejected(self):
        with (self.run / "inputs/package-candidate.json").open("a", encoding="utf-8") as output:
            output.write(" ")
        with self.assertRaisesRegex(ValueError, "File size differs"):
            copy.materialize(self.run_json, self.plan)

    def test_dirty_candidate_identity_is_rejected(self):
        self.state["identity"]["workingTreeModified"] = True
        self.run_json.write_text(json.dumps(self.state), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "identity is not clean"):
            copy.materialize(self.run_json, self.plan)

    def test_profile_isolation_is_required(self):
        self.state["profileRoot"] = str(self.root / "unrelated profile")
        self.run_json.write_text(json.dumps(self.state), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "runtime path differs"):
            copy.materialize(self.run_json, self.plan)

    def test_existing_application_artifacts_are_rejected(self):
        (self.run / "profile/database.sqlite").write_bytes(b"fixture db")
        with self.assertRaisesRegex(ValueError, "nonempty"):
            copy.materialize(self.run_json, self.plan)

    def test_bad_relative_paths_are_rejected(self):
        for relative in ("../escape", "/samples/escape", "samples/../escape", "samples//file", "samples/.",
                         "samples/a:stream", "samples\\file", "models/model.gguf", "samples/CON.txt",
                         "samples/trailing.", "samples/trailing ", "samples/a?b", "samples/a\x00b", copy.MANIFEST):
            with self.subTest(relative=relative), self.assertRaises(ValueError):
                copy.target(self.run, relative)

    def test_duplicate_destinations_are_rejected(self):
        self.mutate_plan(lambda plan: plan["files"].append(dict(plan["files"][0])))
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            copy.materialize(self.run_json, self.plan)

    def test_case_insensitive_destination_collision_is_rejected(self):
        def change(plan):
            row = dict(plan["files"][0])
            row["targetRelativePath"] = row["targetRelativePath"].upper().replace("NEGATIVE-MODELS", "negative-models")
            plan["files"].append(row)
        self.mutate_plan(change)
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            copy.materialize(self.run_json, self.plan)

    def test_source_path_escape_is_rejected(self):
        self.mutate_plan(lambda plan: plan["files"][0].update(sourceRelativePath="samples/../../run.json"))
        with self.assertRaisesRegex(ValueError, "outside permitted"):
            copy.materialize(self.run_json, self.plan)

    def test_overlap_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "separate directories"):
            copy.materialize(self.run_json, self.plan, source_root=self.root)

    def test_invalid_size_and_hash_are_rejected(self):
        for size, digest in ((True, "a" * 64), (-1, "a" * 64), (1, "not-a-hash")):
            with self.subTest(size=size, digest=digest), self.assertRaisesRegex(ValueError, "Invalid file size"):
                copy.check_file(self.sources / "samples/笔记.txt", size, digest)

    def test_real_model_is_rejected(self):
        with (self.sources / "negative-models/big.gguf").open("wb") as model:
            model.truncate(1024 * 1024 + 1)
        with self.assertRaisesRegex(ValueError, "Real model"):
            copy.create_plan(self.sources, self.root / "qcs-big-plan.json")

    def test_plan_must_be_outside_sources_and_exclusively_created(self):
        with self.assertRaisesRegex(ValueError, "outside the read-only"):
            copy.create_plan(self.sources, self.sources / "plan.json")
        with self.assertRaises(FileExistsError):
            copy.create_plan(self.sources, self.plan)

    def test_partial_copy_failure_retains_evidence_and_never_removes_sources(self):
        with patch.object(copy.shutil, "copyfileobj", side_effect=OSError("fixture disk failure")):
            with self.assertRaisesRegex(OSError, "fixture disk failure"):
                copy.materialize(self.run_json, self.plan, True)
        report = json.loads((self.run / copy.FAILURE).read_text(encoding="utf-8"))
        self.assertFalse(report["complete"])
        self.assertFalse(report["acceptancePassed"])
        self.assertIn("fixture disk failure", report["error"])
        self.assertTrue((self.sources / "samples/笔记.txt").is_file())

    def test_copy_and_report_failure_preserve_original_exception_without_add_note(self):
        class LegacyFailure(OSError):
            @property
            def add_note(self):
                raise AttributeError("Python 3.10 has no BaseException.add_note")
        original = LegacyFailure("original copy failure")
        with patch.object(copy.shutil, "copyfileobj", side_effect=original), \
                patch.object(copy, "write_json", side_effect=OSError("failure report is unwritable")):
            with self.assertRaises(LegacyFailure) as caught:
                copy.materialize(self.run_json, self.plan, True)
        self.assertIs(caught.exception, original)
        self.assertEqual(str(caught.exception), "original copy failure")
        self.assertTrue((self.sources / "samples/笔记.txt").is_file())

    def test_unreadable_subdirectory_cannot_silently_disappear_from_manual_plan(self):
        blocked = self.sources / "samples/unreadable"
        blocked.mkdir()
        (blocked / "must-not-disappear.txt").write_text("must remain in the input set", encoding="utf-8")
        plan = self.root / "unreadable-plan.json"
        original_scandir = copy.os.scandir
        def scandir(path):
            if Path(path) == blocked:
                raise PermissionError("directory cannot be enumerated")
            return original_scandir(path)
        with patch.object(copy.os, "scandir", side_effect=scandir):
            with self.assertRaisesRegex(PermissionError, "cannot be enumerated"):
                copy.create_plan(self.sources, plan)
        self.assertFalse(plan.exists())
        self.assertEqual((blocked / "must-not-disappear.txt").read_text(encoding="utf-8"), "must remain in the input set")

    def test_linked_source_guard_rejects_before_reading_file(self):
        # Exercise the guard on every host, without requiring Windows symlink
        # privileges. Actual filesystem-link behavior is a platform test.
        candidate = self.sources / "samples/笔记.txt"
        with patch.object(Path, "is_symlink", autospec=True, side_effect=lambda path: path == candidate):
            with self.assertRaisesRegex(ValueError, "Linked path"):
                copy.create_plan(self.sources, self.root / "qcs-links.json")

    def test_only_exact_macos_os_alias_targets_are_accepted(self):
        for name, canonical in (("/var", "/private/var"), ("/tmp", "/private/tmp")):
            fake = Mock()
            fake.as_posix.return_value = name
            fake.resolve.return_value = PurePosixPath(canonical)
            with patch.object(copy.sys, "platform", "darwin"):
                self.assertTrue(copy._system_alias(fake))
                fake.resolve.return_value = PurePosixPath("/elsewhere")
                self.assertFalse(copy._system_alias(fake))
                fake.as_posix.return_value = name + "/child"
                fake.resolve.return_value = PurePosixPath(canonical)
                self.assertFalse(copy._system_alias(fake))
            fake.as_posix.return_value = name
            with patch.object(copy.sys, "platform", "linux"):
                self.assertFalse(copy._system_alias(fake))

    def test_final_link_is_rejected_even_if_system_alias_check_accepts_it(self):
        with patch.object(Path, "is_symlink", autospec=True, side_effect=lambda path: path == self.sources):
            with patch.object(copy, "_system_alias", return_value=True):
                with self.assertRaisesRegex(ValueError, "Linked path"):
                    copy.unlinked(self.sources)

    def test_create_plan_requires_sample(self):
        empty_source = self.root / "empty-source"
        empty_source.mkdir()
        with self.assertRaisesRegex(ValueError, "At least one sample"):
            copy.create_plan(empty_source, self.root / "qcs-empty.json")


class FixtureSourceTests(unittest.TestCase):
    def setUp(self):
        requested = os.environ.get("QCOPILOTS_SEARCH_TEST_ROOT") or None
        if requested and not Path(requested).is_dir():
            raise RuntimeError("QCOPILOTS_SEARCH_TEST_ROOT must be an existing directory")
        self.temp = tempfile.TemporaryDirectory(prefix="qcs-cu-source-", dir=requested)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def prepare(self, name):
        root = self.root / name
        result = copy.prepare_source(root)
        self.assertEqual(Path(result["sourceRoot"]), root)
        self.assertEqual(result["browsingFiles"], 205)
        manifest_path = root / copy.SOURCE_MANIFEST
        self.assertEqual(Path(result["manifest"]), manifest_path)
        self.assertEqual(result["manifestSha256"], copy.sha(manifest_path))
        return root, json.loads(manifest_path.read_text(encoding="utf-8"))

    def test_cli_preparation_and_plan_emit_utf8_under_legacy_stream_encoding(self):
        import subprocess
        source = self.root / "CLI 中文 source"
        plan = self.root / "CLI 中文 plan.json"
        environment = dict(os.environ, PYTHONIOENCODING="ascii")
        script = str(Path(__file__).with_name("qcs-copy-cu-fixtures.py"))
        for arguments in (("--prepare-source", "--source-root", str(source)),
                          ("--create-plan", "--source-root", str(source), "--plan", str(plan))):
            result = subprocess.run([sys.executable, "-B", script, *arguments],
                                    capture_output=True, env=environment, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", errors="replace"))
            self.assertIsInstance(json.loads(result.stdout.decode("utf-8")), dict)
        self.assertEqual(json.loads(plan.read_text(encoding="utf-8"))["schemaVersion"], 2)

    def test_partial_preparation_is_preserved_and_cannot_be_reclassified_as_manual_source(self):
        root = self.root / "failed preparation"
        original_check = copy.check_file
        calls = 0
        def check(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("fixture verification failed")
            return original_check(*args, **kwargs)
        with patch.object(copy, "check_file", side_effect=check):
            with self.assertRaisesRegex(OSError, "fixture verification failed"):
                copy.prepare_source(root)
        self.assertTrue((root / copy.PREPARING).is_file())
        self.assertFalse((root / copy.SOURCE_MANIFEST).exists())
        before = {path.relative_to(root).as_posix(): path.read_bytes()
                  for path in root.rglob("*") if path.is_file()}
        self.assertGreater(len(before), 1)
        plan = self.root / "partial-plan.json"
        with self.assertRaisesRegex(ValueError, "Incomplete fixture preparation"):
            copy.create_plan(root, plan)
        self.assertFalse(plan.exists())
        after = {path.relative_to(root).as_posix(): path.read_bytes()
                 for path in root.rglob("*") if path.is_file()}
        self.assertEqual(before, after)

    def snapshot(self, root):
        return {path.relative_to(root).as_posix(): (path.stat().st_size, copy.sha(path), path.stat().st_mtime_ns)
                for path in root.rglob("*") if path.is_file()}

    def test_generated_browsing_files_and_manifest_match_actual_content(self):
        root, manifest = self.prepare("中文 source with spaces")
        rows = manifest["files"]
        relative_paths = [row["relativePath"] for row in rows]
        self.assertEqual(len(relative_paths), len(set(relative_paths)))
        self.assertEqual(len(relative_paths), len({value.casefold() for value in relative_paths}))
        for row in rows:
            with self.subTest(path=row["relativePath"]):
                relative = PurePosixPath(row["relativePath"])
                self.assertFalse(relative.is_absolute())
                self.assertNotIn("..", relative.parts)
                self.assertIn(relative.parts[0], ("samples", "negative-models"))
                path = root.joinpath(*relative.parts)
                self.assertTrue(path.is_file())
                self.assertFalse(path.is_symlink())
                self.assertEqual(row["bytes"], path.stat().st_size)
                self.assertEqual(row["sha256"], copy.sha(path))
                self.assertEqual(row["mtimeMs"], path.stat().st_mtime_ns // 1_000_000)
                self.assertTrue(row["role"])
        prefix = copy.BROWSE_ROOT + "/"
        browsing_rows = [row for row in rows if row["relativePath"].startswith(prefix)]
        actual = {path.relative_to(root).as_posix() for path in (root / copy.BROWSE_ROOT).rglob("*") if path.is_file()}
        self.assertEqual(len(browsing_rows), 205)
        self.assertEqual(actual, {row["relativePath"] for row in browsing_rows})
        for relative in actual:
            self.assertEqual(PurePosixPath(relative).suffix, ".txt")
            self.assertTrue((root / relative).read_text(encoding="utf-8").strip())
        self.assertTrue(any(any(ord(char) > 127 for char in Path(value).name) for value in actual))
        self.assertTrue(any(" " in Path(value).name for value in actual))
        self.assertTrue(any(len(PurePosixPath(value).parts) >= 5 for value in actual))
        other_samples = [value for value in relative_paths if value.startswith("samples/") and not value.startswith(prefix)]
        self.assertTrue(other_samples, "Interaction fixtures must have a root separate from the 205 browsing files")
        suffixes = {PurePosixPath(value).suffix.lower() for value in other_samples}
        self.assertTrue({".ipynb", ".pdf"}.issubset(suffixes))
        self.assertTrue({".docx", ".xlsx", ".pptx", ".odt", ".rtf"} & suffixes)
        self.assertTrue(any(value.startswith("negative-models/") and value.endswith(".gguf") for value in relative_paths))

    def test_fixed_timestamps_include_ties_across_both_page_boundaries(self):
        _, manifest = self.prepare("page boundaries")
        prefix = copy.BROWSE_ROOT + "/"
        times = sorted((row["mtimeMs"] for row in manifest["files"] if row["relativePath"].startswith(prefix)), reverse=True)
        self.assertEqual(len(times), 205)
        self.assertGreater(times[0], times[-1])
        for boundary in (100, 200):
            with self.subTest(boundary=boundary):
                self.assertEqual(len(set(times[boundary - 2:boundary + 1])), 1)
        # Equal-time rows are intentionally not ordered by filename here. The
        # product's tie breaker is the file ID assigned by the real index.

    def test_generated_fixture_identity_is_reproducible_across_roots(self):
        _, first = self.prepare("first source")
        _, second = self.prepare("second 中文 source")
        def identities(manifest):
            return {row["relativePath"]: (row["bytes"], row["sha256"], row["mtimeMs"], row["role"])
                    for row in manifest["files"]}
        self.assertEqual(identities(first), identities(second))

    def test_changed_generator_identity_rejects_plan_without_modifying_sources(self):
        root, manifest = self.prepare("generator identity")
        manifest["generator"]["sha256"] = "0" * 64
        (root / copy.SOURCE_MANIFEST).write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        before = self.snapshot(root)
        plan = self.root / "changed-generator-plan.json"
        with self.assertRaisesRegex(ValueError, "generator differs"):
            copy.create_plan(root, plan)
        self.assertFalse(plan.exists())
        self.assertEqual(self.snapshot(root), before)

    def test_changed_prepared_file_set_rejects_plan_without_modifying_sources(self):
        root, manifest = self.prepare("file membership")
        extra = root / copy.BROWSE_ROOT / "unrecorded extra.txt"
        extra.write_text("This file was not in the prepared manifest.", encoding="utf-8")
        before = self.snapshot(root)
        extra_plan = self.root / "extra-file-plan.json"
        with self.assertRaisesRegex(ValueError, "unrecorded or missing"):
            copy.create_plan(root, extra_plan)
        self.assertFalse(extra_plan.exists())
        self.assertEqual(self.snapshot(root), before)
        extra.unlink()
        (root / manifest["files"][0]["relativePath"]).unlink()
        before = self.snapshot(root)
        missing_plan = self.root / "missing-file-plan.json"
        with self.assertRaises(ValueError):
            copy.create_plan(root, missing_plan)
        self.assertFalse(missing_plan.exists())
        self.assertEqual(self.snapshot(root), before)

    def test_late_video_scene_survives_coarse_grayscale_deduplication(self):
        root, manifest = self.prepare("late video scene")
        videos = [row for row in manifest["files"] if row["role"] == "video"]
        self.assertEqual(len(videos), 1)
        row = videos[0]
        data = (root / row["relativePath"]).read_bytes()
        self.assertEqual(data[:4], b"RIFF")
        self.assertEqual(data[8:12], b"AVI ")
        self.assertEqual(struct.unpack_from("<I", data, 4)[0] + 8, len(data))

        def chunks(payload):
            position = 0
            while position < len(payload):
                self.assertGreaterEqual(len(payload) - position, 8)
                tag = payload[position:position + 4]
                length = struct.unpack_from("<I", payload, position + 4)[0]
                start, end = position + 8, position + 8 + length
                self.assertLessEqual(end, len(payload))
                content = payload[start:end]
                yield tag, content
                if tag == b"LIST":
                    self.assertGreaterEqual(length, 4)
                    yield from chunks(content[4:])
                position = end + length % 2
            self.assertEqual(position, len(payload))

        parts = list(chunks(data[12:]))
        headers = [payload for tag, payload in parts if tag == b"avih"]
        formats = [payload for tag, payload in parts if tag == b"strf"]
        frames = [payload for tag, payload in parts if tag == b"00db"]
        self.assertEqual(len(headers), 1)
        self.assertEqual(len(formats), 1)
        frame_us = struct.unpack_from("<I", headers[0])[0]
        self.assertGreater(frame_us, 0)
        self.assertEqual(struct.unpack_from("<I", headers[0], 16)[0], len(frames))
        _, width, height, planes, bits, compression = struct.unpack_from("<IiiHHI", formats[0])
        self.assertEqual((planes, bits, compression), (1, 24, 0))
        self.assertGreater(width, 0)
        self.assertGreater(height, 0)
        self.assertEqual((width % 8, height % 8), (0, 0))
        late_ms = row["expectedFrameStartMs"]
        self.assertLessEqual(row["targetRegionMs"][0], late_ms)
        self.assertLess(late_ms, row["targetRegionMs"][1])
        self.assertLessEqual(row["targetRegionMs"][1] * 1000, len(frames) * frame_us)
        late_frame = late_ms * 1000 // frame_us
        self.assertGreater(late_frame, 1)
        self.assertLess(late_frame, len(frames))

        def coarse_hash(frame):
            # This independent area average checks the fixture's spatial
            # distinction. Real Qt/FFmpeg extraction remains separate evidence.
            stride = ((width * 3 + 3) // 4) * 4
            self.assertEqual(len(frame), stride * height)
            cells = []
            for cell_y in range(8):
                for cell_x in range(8):
                    total = 0
                    for y in range(cell_y * height // 8, (cell_y + 1) * height // 8):
                        for x in range(cell_x * width // 8, (cell_x + 1) * width // 8):
                            blue, green, red = frame[y * stride + x * 3:y * stride + x * 3 + 3]
                            total += (red * 11 + green * 16 + blue * 5) // 32
                    cells.append(total // ((width // 8) * (height // 8)))
            mean = sum(cells) // 64
            return sum((value > mean) << index for index, value in enumerate(cells))

        distance = (coarse_hash(frames[1]) ^ coarse_hash(frames[late_frame])).bit_count()
        self.assertGreater(distance, 4, "Late scene must differ spatially enough to avoid frame deduplication")

    def test_existing_empty_and_nonempty_roots_are_never_overwritten(self):
        empty = self.root / "existing empty"
        empty.mkdir()
        occupied = self.root / "existing content"
        occupied.mkdir()
        canary = occupied / "canary.txt"
        canary.write_bytes(b"unrelated existing content")
        original = (canary.read_bytes(), canary.stat().st_mtime_ns)
        for target in (empty, occupied):
            with self.subTest(root=target), self.assertRaises((OSError, ValueError)):
                copy.prepare_source(target)
        self.assertEqual(list(empty.iterdir()), [])
        self.assertEqual(list(occupied.iterdir()), [canary])
        self.assertEqual((canary.read_bytes(), canary.stat().st_mtime_ns), original)

    def test_relative_output_is_rejected_before_creating_anything(self):
        relative = Path("qcs-forbidden-relative-" + uuid.uuid4().hex)
        with self.assertRaises(ValueError):
            copy.prepare_source(relative)
        self.assertFalse(relative.exists())

    def test_output_within_checkout_is_rejected_before_creating_anything(self):
        checkout = Path(__file__).resolve().parents[2]
        target = checkout / ("qcs-forbidden-source-" + uuid.uuid4().hex)
        with self.assertRaises(ValueError):
            copy.prepare_source(target)
        self.assertFalse(target.exists())

    def test_linked_output_ancestor_is_rejected_before_creation(self):
        ancestor = self.root / "linked ancestor"
        ancestor.mkdir()
        target = ancestor / "new source"
        with patch.object(Path, "is_symlink", autospec=True, side_effect=lambda path: path == ancestor):
            with self.assertRaisesRegex(ValueError, "Linked path"):
                copy.prepare_source(target)
        self.assertEqual(list(ancestor.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
