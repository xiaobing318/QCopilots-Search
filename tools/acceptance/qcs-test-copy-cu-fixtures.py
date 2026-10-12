#!/usr/bin/env python3
"""Portable fixture-copy contract tests. No applications or real profiles are used."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
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


if __name__ == "__main__":
    unittest.main()
