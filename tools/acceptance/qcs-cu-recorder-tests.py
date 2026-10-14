#!/usr/bin/env python3
"""Synthetic stdlib tests for the external CU recorder; no UI or real candidate.

Every fixture and generated export lives in a fresh TemporaryDirectory. No test
creates ten passing cases or a passed CU summary. The real identity contracts
use temporary Git repositories and Python to produce inert fixture files. The
redirected CLI test starts Python children. Other tests block subprocess, and
no test starts the application. Scenario tests use clearly marked synthetic state.
"""
from __future__ import annotations

import base64
from contextlib import redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
import zipfile

sys.dont_write_bytecode = True
CAPABILITIES = {
    "realDirectoryLinks": {"status": "registered", "implementation": "Windows junction" if os.name == "nt" else "POSIX symlink"},
    "realFileSymlinks": {"status": "not_applicable" if os.name == "nt" else "registered",
                         "reason": "Windows standard-user validation uses real directory junctions; file symlinks are validated on macOS/Linux" if os.name == "nt" else "Native file symlink tests are registered"},
}
SCRIPT = Path(__file__).with_name("qcs-cu-recorder.py")
SPEC = importlib.util.spec_from_file_location("synthetic_cu_recorder_under_test", SCRIPT)
recorder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(recorder)

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII="
)
START = "2026-01-01T00:00:10+00:00"
OBSERVED = "2026-01-01T00:00:20+00:00"
LATER = "2026-01-01T00:00:30+00:00"


class TemporaryTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="qcs-recorder-SYNTHETIC-tests-", dir=os.environ.get("QCOPILOTS_SEARCH_TEST_ROOT") or None)
        self.addCleanup(temporary.cleanup)
        self.temp = Path(temporary.name).resolve()
        # Even an accidental unmocked identity()/init path must never run git or an app.
        for name in ("check_output", "run", "Popen", "call", "check_call"):
            patcher = mock.patch.object(
                recorder.subprocess, name, side_effect=AssertionError("Subprocess forbidden in synthetic tests")
            )
            patcher.start()
            self.addCleanup(patcher.stop)

    def file(self, relative, contents=b"SYNTHETIC TEST DATA"):
        path = self.temp / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contents)
        return path

    def json_file(self, relative, value):
        return self.file(relative, json.dumps(value, ensure_ascii=False).encode("utf-8"))

    def call_silently(self, function, args):
        with redirect_stdout(io.StringIO()):
            return function(args)


class SafePathTests(TemporaryTest):
    def setUp(self):
        super().setUp()
        self.root = self.temp / "root"
        self.root.mkdir()
        self.target = self.file("root/evidence/中文 screenshot.png", PNG)

    def test_regular_relative_path(self):
        self.assertEqual(recorder.safe(self.root, "evidence/中文 screenshot.png"), self.target)

    def test_rejects_traversal_absolute_colon_backslash_and_empty_parts(self):
        invalid = (
            "../outside.png", "evidence/../../outside.png", "/absolute.png",
            "C:/absolute.png", "evidence/file:stream", "evidence\\file.png",
            "\\\\server\\share\\file.png", "evidence//file.png", "./file.png",
            "evidence/./file.png", "evidence/../file.png", "evidence/", "", None,
        )
        for path in invalid:
            with self.subTest(path=path), self.assertRaises(ValueError):
                recorder.safe(self.root, path, exists=False)

    def test_rejects_missing_file_and_directory(self):
        for relative in ("missing.png", "evidence"):
            with self.subTest(path=relative), self.assertRaises(ValueError):
                recorder.safe(self.root, relative)

    def test_rejects_symlink_branch_without_requiring_windows_privilege(self):
        with mock.patch.object(recorder.Path, "is_symlink", return_value=True):
            with self.assertRaisesRegex(ValueError, "symlink|junction"):
                recorder.safe(self.root, "evidence/中文 screenshot.png")

    def directory_link(self, destination, alias):
        if os.name == "nt":
            import _winapi
            _winapi.CreateJunction(str(destination.resolve()), str(alias))
            self.addCleanup(alias.rmdir)
            self.assertTrue(alias.lstat().st_file_attributes & recorder.stat.FILE_ATTRIBUTE_REPARSE_POINT)
        else:
            alias.symlink_to(destination, target_is_directory=True)
            self.addCleanup(alias.unlink)
            self.assertTrue(alias.is_symlink())

    def test_rejects_real_directory_link(self):
        alias = self.root / "directory-link"
        self.directory_link(self.target.parent, alias)
        with self.assertRaisesRegex(ValueError, "symlink|junction"):
            recorder.safe(self.root, "directory-link/中文 screenshot.png")

    def test_rejects_real_directory_link_escape(self):
        outside = self.file("outside/data.png", PNG)
        alias = self.root / "outside-link"
        self.directory_link(outside.parent, alias)
        with self.assertRaisesRegex(ValueError, "escapes root"):
            recorder.safe(self.root, "outside-link/data.png")

    if os.name == "nt":
        def test_reparse_attribute_rejects_real_junction_without_path_junction_detection(self):
            alias = self.root / "legacy-python-link"
            self.directory_link(self.target.parent, alias)
            with mock.patch.object(recorder.Path, "is_junction", return_value=False, create=True):
                with self.assertRaisesRegex(ValueError, "symlink|junction"):
                    recorder.safe(self.root, "legacy-python-link/中文 screenshot.png")

    if os.name != "nt":
        def test_rejects_real_file_symlink(self):
            alias = self.root / "image-link.png"
            alias.symlink_to(self.target)
            self.addCleanup(alias.unlink)
            with self.assertRaisesRegex(ValueError, "symlink|junction"):
                recorder.safe(self.root, alias.name)

        def test_rejects_real_file_symlink_escape(self):
            outside = self.file("outside.png", PNG)
            alias = self.root / "outside-file-link.png"
            alias.symlink_to(outside)
            self.addCleanup(alias.unlink)
            with self.assertRaisesRegex(ValueError, "escapes root"):
                recorder.safe(self.root, alias.name)

    def test_hash_record_rejects_same_size_mutation(self):
        item = recorder.record(self.root, self.target)
        self.assertEqual(recorder.check_record(self.root, item), self.target)
        data = self.target.read_bytes()
        self.target.write_bytes(data[:-1] + bytes([data[-1] ^ 1]))
        with self.assertRaisesRegex(ValueError, "Evidence changed"):
            recorder.check_record(self.root, item)


class ScreenshotMagicTests(TemporaryTest):
    def test_accepts_png_magic_independent_of_extension(self):
        recorder.image_file(self.file("valid-but-no-extension", PNG))

    def test_accepts_jpeg_magic(self):
        # This helper validates magic/length only, not a full JPEG decode.
        recorder.image_file(self.file("synthetic-jpeg-header.dat", b"\xff\xd8\xff" + b"SYNTHETIC" * 8))

    def test_rejects_false_extensions_and_other_magic(self):
        for name, data in (("fake.png", b"SYNTHETIC NOT AN IMAGE" * 3),
                           ("fake.jpg", b"GIF89a" + b"x" * 40),
                           ("fake.webp", b"RIFF" + b"x" * 40)):
            with self.subTest(name=name), self.assertRaises(ValueError):
                recorder.image_file(self.file(name, data))

    def test_rejects_empty_or_truncated_images(self):
        for index, data in enumerate((b"", b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff" + b"x" * 28)):
            with self.subTest(index=index), self.assertRaises(ValueError):
                recorder.image_file(self.file(f"short-{index}.png", data))


class CandidateBinaryTests(TemporaryTest):
    def setUp(self):
        super().setUp()
        self.app = self.temp / "synthetic-app"
        self.app.mkdir()
        self.package = self.temp / "SYNTHETIC-NOT-A-REAL-CANDIDATE.zip"
        self.contents = {name: ("NOT EXECUTABLE: SYNTHETIC " + name).encode() for name in recorder.NAMES}
        for name, data in self.contents.items():
            (self.app / (name + ".exe")).write_bytes(data)
        self.candidate = {"syntheticTestOnly": True, "executables": {
            name: {"sha256": recorder.sha(self.app / (name + ".exe"))} for name in recorder.NAMES
        }}
        self.make_zip([(f"candidate/bin/{name}.exe", data) for name, data in self.contents.items()])

    def make_zip(self, members):
        with zipfile.ZipFile(self.package, "w") as archive:
            for name, data in members:
                archive.writestr(name, data)
        self.candidate["packageSha256"] = recorder.sha(self.package)

    def verify(self):
        return recorder.verify_binaries(self.package, self.app, self.candidate)

    def test_consistent_four_dummy_members_can_be_inspected_without_execution(self):
        self.assertEqual(set(self.verify()), set(recorder.NAMES))

    def test_missing_package_is_rejected(self):
        self.package.unlink()
        with self.assertRaises(OSError):
            self.verify()

    def test_wrong_package_sha_is_rejected(self):
        self.candidate["packageSha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "package SHA"):
            self.verify()

    def test_invalid_zip_is_rejected(self):
        self.package.write_bytes(b"SYNTHETIC NOT A ZIP")
        self.candidate["packageSha256"] = recorder.sha(self.package)
        with self.assertRaises(zipfile.BadZipFile):
            self.verify()

    def test_missing_or_extra_named_executable_in_record_is_rejected(self):
        original = self.candidate["executables"].copy()
        for variant in ({k: v for k, v in original.items() if k != recorder.NAMES[0]},
                        {**original, "unexpected": {"sha256": "f" * 64}}):
            with self.subTest(names=list(variant)):
                self.candidate["executables"] = variant
                with self.assertRaisesRegex(ValueError, "Exactly four"):
                    self.verify()

    def test_each_missing_zip_executable_is_rejected(self):
        for excluded in recorder.NAMES:
            with self.subTest(excluded=excluded):
                self.make_zip([(f"bin/{name}.exe", data) for name, data in self.contents.items() if name != excluded])
                with self.assertRaisesRegex(ValueError, "exactly one"):
                    self.verify()

    def test_duplicate_zip_executable_is_rejected(self):
        members = [(f"bin/{name}.exe", data) for name, data in self.contents.items()]
        members.append((f"duplicate/{recorder.NAMES[0].upper()}.EXE", self.contents[recorder.NAMES[0]]))
        self.make_zip(members)
        with self.assertRaisesRegex(ValueError, "exactly one"):
            self.verify()

    def test_zip_member_bytes_disagree_with_build_record(self):
        self.make_zip([(f"bin/{name}.exe", data + b"CHANGED") for name, data in self.contents.items()])
        with self.assertRaisesRegex(ValueError, "disagree"):
            self.verify()

    def test_extracted_executable_bytes_disagree_with_zip(self):
        (self.app / (recorder.NAMES[0] + ".exe")).write_bytes(b"CHANGED SYNTHETIC DATA")
        with self.assertRaisesRegex(ValueError, "disagree"):
            self.verify()

    def test_missing_extracted_executable_is_rejected(self):
        (self.app / (recorder.NAMES[0] + ".exe")).unlink()
        with self.assertRaises(OSError):
            self.verify()

    def test_unsafe_zip_executable_paths_are_rejected(self):
        for prefix in ("../bin/", "/absolute/bin/"):
            with self.subTest(prefix=prefix):
                self.make_zip([(prefix + name + ".exe", data) for name, data in self.contents.items()])
                with self.assertRaisesRegex(ValueError, "Unsafe ZIP"):
                    self.verify()

    def test_init_rejects_missing_candidate_record_before_any_process(self):
        with self.assertRaises(OSError):
            recorder.initialize(SimpleNamespace(candidate_record=self.temp / "missing.json"))

    def test_init_rejects_historical_report_as_candidate_record(self):
        path = self.json_file("fake-history.json", {"schemaVersion": 1, "kind": "windows-computer-use", "passed": False})
        with self.assertRaisesRegex(ValueError, "package-candidate"):
            recorder.initialize(SimpleNamespace(candidate_record=path))


class ScenarioEvidenceTests(TemporaryTest):
    def setUp(self):
        super().setUp()
        self.root = self.temp / "SYNTHETIC-NOT-REAL-CU-RUN"
        for directory in ("inputs", "evidence", "exports"):
            (self.root / directory).mkdir(parents=True)
        self.state = {
            "syntheticTestOnly": True, "kind": "NOT-A-REAL-RECORDER-RUN",
            "identity": {"checkoutCommit": "a" * 40, "candidateHead": "a" * 40},
            "packageSha256": "b" * 64, "executables": {name: {"sha256": "c" * 64} for name in recorder.NAMES},
            "modelFingerprint": "SYNTHETIC-model", "extractionFingerprint": "SYNTHETIC-extraction",
            "platform": {"system": "SYNTHETIC", "architecture": "SYNTHETIC"},
            "runId": "SYNTHETIC-UNIT-TEST", "profileRoot": str(self.root / "unused-profile"), "startedAt": START,
        }
        recorder.write(self.root / "run.json", self.state)
        self.load = mock.patch.object(recorder, "load_run", return_value=(self.root, self.state))
        self.load.start()
        self.addCleanup(self.load.stop)
        self.clock = mock.patch.object(recorder, "now", return_value=START)
        self.now = self.clock.start()
        self.addCleanup(self.clock.stop)
        self.args = SimpleNamespace(run=self.root, scenario="CU-01", expected="SYNTHETIC expected behavior")
        self.screenshot = self.file("incoming/real-format-synthetic-pixel.png", PNG)

    def begin(self):
        self.call_silently(recorder.begin, self.args)
        self.now.return_value = LATER
        return recorder.active(self.root, self.args.scenario)[0]

    def entry(self, **updates):
        value = {"syntheticTestOnly": True, "action": "SYNTHETIC action; no real UI", "actual": "SYNTHETIC result",
                 "utc": OBSERVED, "operator": "tool-observation", "window": {"id": "SYNTHETIC-WINDOW"},
                 "packageSha256": self.state["packageSha256"], "screenshots": [str(self.screenshot)]}
        value.update(updates)
        return self.json_file("incoming/entry.json", value)

    def observe(self, **updates):
        self.args.entry = self.entry(**updates)
        return self.call_silently(recorder.observe, self.args)

    def finish(self, status="passed"):
        return self.call_silently(recorder.finish, SimpleNamespace(
            run=self.root, scenario=self.args.scenario, status=status,
            actual="SYNTHETIC operator assessment; never a real acceptance", assistance=None))

    def test_begin_cannot_overlap_running_attempt(self):
        self.begin()
        with self.assertRaisesRegex(ValueError, "already running"):
            self.begin()

    def test_observation_requires_begun_scenario(self):
        with self.assertRaisesRegex(ValueError, "Begin the scenario"):
            self.observe()

    def test_observation_rejects_before_start_future_and_non_utc_time(self):
        self.begin()
        for timestamp in ("2026-01-01T00:00:09Z", "2026-01-01T00:00:31Z",
                          "2026-01-01T00:00:20", "2026-01-01T01:00:20+01:00"):
            with self.subTest(timestamp=timestamp), self.assertRaises(ValueError):
                self.observe(utc=timestamp)

    def test_observation_rejects_time_moving_backwards(self):
        self.begin()
        self.observe()
        with self.assertRaisesRegex(ValueError, "backwards"):
            self.observe(utc="2026-01-01T00:00:19Z")

    def test_observation_requires_screenshot_and_real_format(self):
        self.begin()
        for screenshots in ([], None, "not-a-list", [str(self.file("incoming/fake.png", b"x" * 100))]):
            with self.subTest(screenshots=screenshots), self.assertRaises(ValueError):
                self.observe(screenshots=screenshots)

    def test_observation_rejects_mixed_candidate_package(self):
        self.begin()
        with self.assertRaisesRegex(ValueError, "candidate package SHA"):
            self.observe(packageSha256="d" * 64)

    def test_observation_requires_explicit_package_and_window(self):
        self.begin()
        for changes in ({"packageSha256": None}, {"window": {}}, {"window": None}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.observe(**changes)

    def test_pass_is_rejected_without_observations(self):
        self.begin()
        with self.assertRaisesRegex(ValueError, "without actual observations"):
            self.finish()

    def test_screenshot_is_copied_and_hash_recorded(self):
        path = self.begin()
        self.observe()
        row = recorder.read(path)
        self.assertEqual(len(row["screenshots"]), 1)
        copied = recorder.safe(self.root, row["screenshots"][0])
        self.assertNotEqual(copied, self.screenshot)
        self.assertEqual(copied.read_bytes(), PNG)
        for item in row["files"]:
            recorder.check_record(self.root, item)

    def test_finish_rejects_same_size_screenshot_sha_mutation(self):
        path = self.begin()
        self.observe()
        screenshot = recorder.safe(self.root, recorder.read(path)["screenshots"][0])
        data = screenshot.read_bytes()
        screenshot.write_bytes(data[:-1] + bytes([data[-1] ^ 1]))
        with self.assertRaisesRegex(ValueError, "Evidence changed"):
            self.finish()

    def test_summarize_rejects_modified_source_observation_record(self):
        path = self.begin()
        self.observe()
        source = next(item for item in recorder.read(path)["files"] if item["path"].endswith("-source.json"))
        recorder.safe(self.root, source["path"]).write_bytes(b"SYNTHETIC CHANGED JSON")
        with self.assertRaisesRegex(ValueError, "Evidence changed"):
            self.call_silently(recorder.summarize, self.args)

    def test_summarize_rejects_screenshot_reference_missing_from_hash_records(self):
        path = self.begin()
        self.observe()
        self.finish(status="failed")
        row = recorder.read(path)
        # Hashes of original files still match, but the actual report reference
        # was altered to an absent file. Integrity must reject this mismatch.
        row["screenshots"] = ["evidence/CU-01/attempt-001/does-not-exist.png"]
        recorder.write(path, row)
        with self.assertRaises(ValueError):
            self.call_silently(recorder.summarize, self.args)

    def test_summarize_rejects_existing_screenshot_without_hash_binding(self):
        path = self.begin()
        self.observe()
        self.finish(status="failed")
        row = recorder.read(path)
        picture = row["screenshots"][0]
        self.assertTrue(recorder.safe(self.root, picture).is_file())
        # Keep the file and the matching operation-log reference. Only remove
        # the integrity binding so mere existence cannot satisfy validation.
        row["files"] = [item for item in row["files"] if item["path"] != picture]
        recorder.write(path, row)
        with self.assertRaisesRegex(ValueError, "Screenshot lacks a hash binding"):
            self.call_silently(recorder.summarize, self.args)
        self.assertEqual(list((self.root / "exports").iterdir()), [])

    def test_summarize_rejects_changed_action_with_unchanged_operation_log(self):
        path = self.begin()
        self.observe()
        self.finish(status="failed")
        row = recorder.read(path)
        row["actions"][0] = "SYNTHETIC altered action not present in the bound log"
        recorder.write(path, row)
        with self.assertRaisesRegex(ValueError, "Action text differs"):
            self.call_silently(recorder.summarize, self.args)
        self.assertEqual(list((self.root / "exports").iterdir()), [])

    def test_user_assisted_observation_requires_assistance_at_finish(self):
        self.begin()
        self.observe(operator="user-assisted")
        with self.assertRaisesRegex(ValueError, "assistance record"):
            self.finish()

    def test_all_ten_missing_cases_generate_only_failed_synthetic_summary(self):
        result = self.call_silently(recorder.summarize, self.args)
        self.assertEqual(result, 1)
        reports = list(self.root.glob("windows-computer-use-*.json"))
        report = next(recorder.read(path) for path in reports if not path.stem.endswith("-files"))
        self.assertFalse(report["passed"])
        self.assertTrue(report["files"])
        self.assertTrue(all(recorder.check_record(self.root, item) for item in report["files"]))
        self.assertEqual(report["runId"], "SYNTHETIC-UNIT-TEST")
        self.assertEqual(len(report["errors"]), 10)
        self.assertEqual([item["id"] for item in report["scenarios"]], list(recorder.CASES))
        self.assertTrue(all(item["status"] == "not_run" for item in report["scenarios"]))

    def test_summarize_rejects_mixed_candidate_identity_and_fingerprints(self):
        path = self.begin()
        original = recorder.read(path)
        for key in ("identity", "packageSha256", "executables", "modelFingerprint", "extractionFingerprint"):
            with self.subTest(key=key):
                changed = dict(original)
                changed[key] = "SYNTHETIC OTHER CANDIDATE"
                recorder.write(path, changed)
                with self.assertRaisesRegex(ValueError, "Mixed candidate"):
                    self.call_silently(recorder.summarize, self.args)
        self.assertEqual(list((self.root / "exports").iterdir()), [])

    def test_finished_operation_log_sha_change_is_rejected(self):
        path = self.begin()
        self.observe()
        self.finish(status="failed")
        row = recorder.read(path)
        recorder.safe(self.root, row["operationLog"]).write_bytes(b"[]")
        with self.assertRaisesRegex(ValueError, "Evidence changed"):
            self.call_silently(recorder.summarize, self.args)


class FixtureReceiptTests(TemporaryTest):
    """Synthetic copy receipts test integrity, never actual CU completion."""
    def setUp(self):
        super().setUp()
        self.root = self.temp / "SYNTHETIC fixture run"
        for folder in ("inputs", "evidence", "samples", "negative-models"):
            (self.root / folder).mkdir(parents=True)
        self.sample = self.root / "samples/canary.txt"
        self.sample.write_bytes(b"SYNTHETIC fixture before CU mutations")
        sample = recorder.record(self.root, self.sample)
        self.source = {"schemaVersion": 1, "kind": "qcopilots-search-cu-fixture-source",
                       "syntheticTestOnly": True, "files": [{"relativePath": sample["path"],
                        "bytes": sample["bytes"], "sha256": sample["sha256"],
                        "mtimeMs": self.sample.stat().st_mtime_ns // 1_000_000}]}
        recorder.write(self.root / recorder.FIXTURE_SOURCE, self.source)
        (self.root / recorder.FIXTURE_INPUT).write_bytes((self.root / recorder.FIXTURE_SOURCE).read_bytes())
        retained = recorder.record(self.root, self.root / recorder.FIXTURE_INPUT)
        self.state = {"schemaVersion": 1, "kind": "qcopilots-search-cu-run", "syntheticTestOnly": True,
                      "runId": "SYNTHETIC fixture receipt only", "startedAt": START,
                      "identity": {"checkoutCommit": "a" * 40, "candidateHead": "a" * 40},
                      "packageSha256": "b" * 64, "executables": {}, "modelFingerprint": "SYNTHETIC-model",
                      "extractionFingerprint": "SYNTHETIC-extraction", "platform": {"system": "SYNTHETIC"},
                      "profileRoot": str(self.root / "unused-profile"), "inputs": [retained]}
        recorder.write(self.root / "run.json", self.state)
        self.initial_hash = recorder.sha(self.root / "run.json")
        self.plan_path = self.temp / "SYNTHETIC-plan.json"
        rows, planned = [], []
        for relative in (sample["path"], recorder.FIXTURE_SOURCE):
            target = self.root / relative
            item = recorder.record(self.root, target)
            details = {"bytes": item["bytes"], "sha256": item["sha256"],
                       "mtimeMs": target.stat().st_mtime_ns // 1_000_000}
            planned.append({"sourceRelativePath": relative, "targetRelativePath": relative, **details})
            rows.append({"targetRelativePath": relative, "targetPath": str(target),
                         "source": {"path": str(self.temp / "unused-original-source" / relative), **details},
                         "verifiedTarget": {"path": str(target), **details},
                         "expectedBytes": details["bytes"], "expectedSha256": details["sha256"],
                         "expectedMtimeMs": details["mtimeMs"]})
        recorder.write(self.plan_path, {"schemaVersion": 2, "kind": "qcopilots-search-cu-fixture-copy-plan", "files": planned})
        self.receipt = {"schemaVersion": 2, "kind": "qcopilots-search-cu-fixture-copy", "syntheticTestOnly": True,
                        "executed": True, "complete": True, "sourceFilesUnchanged": True, "acceptancePassed": False,
                        **{key: self.state[key] for key in ("runId", "identity", "packageSha256", "modelFingerprint", "extractionFingerprint")},
                        "runJsonSha256": self.initial_hash, "planPath": str(self.plan_path),
                        "sourceRoot": str(self.temp / "unused-original-source"), "sampleRoot": str(self.root / "samples"),
                        "negativeModelDirectory": str(self.root / "negative-models"), "manifestRelativePath": recorder.FIXTURE_COPY,
                        "planSha256": recorder.sha(self.plan_path), "filesToCopy": len(rows), "verifiedFileCount": len(rows),
                        "files": rows, "recorderFixtureManifest": retained,
                        "sourceManifest": {"relativePath": recorder.FIXTURE_SOURCE, "bytes": retained["bytes"], "sha256": retained["sha256"]}}
        self.save_receipt()

    def save_receipt(self):
        recorder.write(self.root / recorder.FIXTURE_COPY, self.receipt)

    def bind(self):
        return recorder.fixture_copy_evidence(self.root, self.state)

    def test_binds_original_run_and_keeps_copy_proof_after_canary_changes(self):
        files = self.bind()
        self.assertNotEqual(recorder.sha(self.root / "run.json"), self.initial_hash)
        self.assertEqual(self.state["fixtureCopyEvidence"]["initialRun"]["sha256"], self.initial_hash)
        self.sample.unlink()  # CU-09 intentionally modifies and deletes samples.
        self.plan_path.unlink()  # The retained plan is now independent of its source path.
        self.assertEqual(self.bind(), files)

    def test_summarize_archives_copy_receipt_and_source_without_mutable_samples(self):
        self.bind()
        self.sample.write_bytes(b"SYNTHETIC allowed CU-09 changed canary")
        with mock.patch.object(recorder, "load_run", return_value=(self.root, self.state)):
            self.assertEqual(self.call_silently(recorder.summarize, SimpleNamespace(run=self.root)), 1)
        report = next(recorder.read(path) for path in self.root.glob("windows-computer-use-*.json") if not path.stem.endswith("-files"))
        self.assertFalse(report["passed"])
        names = {item["path"] for item in report["files"]}
        self.assertIn(recorder.FIXTURE_COPY, names)
        self.assertIn(recorder.FIXTURE_SOURCE, names)
        self.assertIn("inputs/fixture-copy-initial-run.json", names)
        self.assertNotIn("samples/canary.txt", names)
        for item in report["files"]:
            recorder.check_record(self.root, item)

    def test_bound_manifest_requires_an_actual_copy_report(self):
        (self.root / recorder.FIXTURE_COPY).unlink()
        with self.assertRaisesRegex(ValueError, "Missing evidence"):
            self.bind()

    def test_rejects_incomplete_or_other_candidate_copy_report(self):
        changes = {"executed": False, "complete": False, "sourceFilesUnchanged": False,
                   "acceptancePassed": True, "identity": {}, "packageSha256": "f" * 64,
                   "runJsonSha256": "f" * 64, "planSha256": "f" * 64, "verifiedFileCount": 999}
        for key, value in changes.items():
            original = self.receipt[key]
            self.receipt[key] = value
            self.save_receipt()
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.bind()
            self.receipt[key] = original
        self.save_receipt()

    def test_rejects_changed_source_manifest_or_initial_target(self):
        source_path = self.root / recorder.FIXTURE_SOURCE
        original = source_path.read_bytes()
        source_path.write_bytes(original + b" ")
        with self.assertRaisesRegex(ValueError, "source manifest differs"):
            self.bind()
        source_path.write_bytes(original)
        self.sample.write_bytes(b"SYNTHETIC unexpectedly changed before beginning")
        with self.assertRaisesRegex(ValueError, "Evidence changed"):
            self.bind()

    def test_rejects_incomplete_or_remapped_plan(self):
        plan = recorder.read(self.plan_path)
        for changed in ({**plan, "files": plan["files"][:-1]},
                        {**plan, "files": [{**item, "sourceRelativePath": "samples/other.txt"} for item in plan["files"]]}):
            recorder.write(self.plan_path, changed)
            self.receipt["planSha256"] = recorder.sha(self.plan_path)
            self.save_receipt()
            with self.assertRaisesRegex(ValueError, "plan"):
                self.bind()

    def test_rejects_modified_copy_report_after_capture(self):
        self.bind()
        self.receipt["syntheticTampering"] = True
        self.save_receipt()
        with self.assertRaisesRegex(ValueError, "Evidence changed"):
            self.bind()

    def test_rejects_snapshot_corruption_during_initial_capture(self):
        original_copy = recorder.shutil.copyfileobj
        def corrupt_snapshot(incoming, outgoing, *args):
            original_copy(incoming, outgoing, *args)
            outgoing.write(b"SYNTHETIC corruption")
        with mock.patch.object(recorder.shutil, "copyfileobj", side_effect=corrupt_snapshot):
            with self.assertRaisesRegex(ValueError, "snapshot changed"):
                self.bind()
        self.assertNotIn("fixtureCopyEvidence", self.state)

    def test_rejects_receipt_changes_during_initial_capture(self):
        original_copy = recorder.shutil.copyfileobj
        def change_receipt(incoming, outgoing, *args):
            original_copy(incoming, outgoing, *args)
            self.receipt["syntheticTampering"] = True
            self.save_receipt()
        with mock.patch.object(recorder.shutil, "copyfileobj", side_effect=change_receipt):
            with self.assertRaisesRegex(ValueError, "Evidence changed"):
                self.bind()
        self.assertNotIn("fixtureCopyEvidence", self.state)

    def test_rejects_redirected_plan_directory(self):
        alias = self.temp / "plan-directory-alias"
        if os.name == "nt":
            import _winapi
            _winapi.CreateJunction(str(self.plan_path.parent), str(alias))
            self.addCleanup(alias.rmdir)
        else:
            alias.symlink_to(self.plan_path.parent, target_is_directory=True)
            self.addCleanup(alias.unlink)
        self.receipt["planPath"] = str(alias / self.plan_path.name)
        self.save_receipt()
        with self.assertRaisesRegex(ValueError, "symlink|junction"):
            self.bind()

    def test_unbound_manual_fixture_runs_remain_compatible(self):
        self.state["inputs"] = []
        (self.root / recorder.FIXTURE_COPY).unlink()
        self.assertEqual(self.bind(), [])

    def test_actual_copy_producer_receipt_is_consumed_without_rewriting_fields(self):
        spec = importlib.util.spec_from_file_location("synthetic_copy_contract_helpers", SCRIPT.with_name("qcs-test-copy-cu-fixtures.py"))
        helpers = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helpers)
        fixture = helpers.FixtureCopyTests("test_copy_verifies_sources_run_and_manifest")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.prepare_bound_source()
        result = helpers.copy.materialize(fixture.run_json, fixture.plan, execute=True)
        self.assertTrue(result["complete"])
        self.assertFalse(result["acceptancePassed"])
        state = recorder.read(fixture.run_json)
        receipt_bytes = Path(result["manifest"]).read_bytes()
        files = recorder.fixture_copy_evidence(fixture.run, state)
        self.assertEqual(Path(result["manifest"]).read_bytes(), receipt_bytes)
        self.assertIn(recorder.FIXTURE_COPY, {item["path"] for item in files})
        (fixture.run / "samples/笔记.txt").unlink()
        self.assertEqual(recorder.fixture_copy_evidence(fixture.run, state), files)


class RealCandidateIdentityTests(unittest.TestCase):
    """Real Git/build-receipt contracts with inert files, never product or CU acceptance."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="qcs-recorder-identity-",
                                               dir=os.environ.get("QCOPILOTS_SEARCH_TEST_ROOT") or None)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.source = self.root / "source"
        self.source.mkdir()
        release_path = SCRIPT.parents[1] / "qcs-release-evidence.py"
        spec = importlib.util.spec_from_file_location("real_cu_release_identity", release_path)
        self.release = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.release)
        fixture_path = SCRIPT.parents[2] / ".github/scripts/qcs-tests-release-evidence.py"
        fixture_spec = importlib.util.spec_from_file_location("cu_package_contract_fixtures", fixture_path)
        self.fixtures = importlib.util.module_from_spec(fixture_spec)
        fixture_spec.loader.exec_module(self.fixtures)
        for relative in (*self.release.LOCKS, "src/core/pipeline_manifest.h"):
            target = self.source / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((SCRIPT.parents[2] / relative).read_bytes())
        self.git("init", "--quiet")
        self.git("config", "core.autocrlf", "false")
        self.git("add", ".")
        self.git("-c", "user.name=QCS synthetic fixture", "-c", "user.email=synthetic@example.invalid",
                 "-c", "commit.gpgsign=false", "commit", "--quiet", "-m", "Synthetic identity fixture")
        self.commit = self.git("rev-parse", "HEAD")

    def git(self, *arguments):
        return subprocess.check_output(["git", "-C", str(self.source), *arguments],
                                       encoding="utf-8").strip()

    def candidate(self):
        app = self.root / "app"
        app.mkdir()
        programs = {name: app / (name + ".exe") for name in recorder.NAMES}
        command = [sys.executable, "-B", "-c",
                   "from pathlib import Path; import sys; "
                   "[Path(p).write_bytes(b'SYNTHETIC NOT EXECUTABLE ' + Path(p).name.encode()) for p in sys.argv[1:]]",
                   *(str(path) for path in programs.values())]
        provenance, build = self.release.record_build(self.source, self.root / "provenance", programs, command)
        self.assertTrue(build["passed"])
        self.assertEqual(self.release.verified_build(provenance.parent, build), [])
        package = self.root / "synthetic.zip"
        with zipfile.ZipFile(package, "w") as archive:
            for path in programs.values():
                archive.write(path, "bin/" + path.name)
        application = self.root / "installed"
        with zipfile.ZipFile(package) as archive:
            archive.extractall(application)
        verification_root = self.root / "verification"
        contracts = recorder.fingerprints(self.source)
        report = self.fixtures.package_release_fixture(verification_root,
            worker=application / "bin/qcs-worker.exe", application_root=application,
            model_fingerprint=contracts["modelFingerprint"],
            extraction_fingerprint=contracts["extractionFingerprint"])
        report.update({"commit": self.commit, "workingTreeModified": False,
            "packageSha256": recorder.sha(package),
            "checks": [{"name": f"synthetic-validator-{number}", "passed": True} for number in range(9)]})
        report["buildIdentity"]["candidateCommit"] = self.commit
        self.fixtures.write_program_inventory(verification_root, report)
        verification = verification_root / "verification.json"
        recorder.write(verification, report)
        record, candidate = self.release.record_package(provenance, package, verification,
            self.root / "candidates", contracts["modelFingerprint"], contracts["extractionFingerprint"])
        self.assertEqual(self.release.inspect_candidate(record.parent, candidate), [])
        return SimpleNamespace(candidate_record=record, source=self.source,
                               package=record.parent / candidate["package"], app_dir=application / "bin",
                               run_parent=self.root / "runs", fixture_manifest=None), candidate

    def test_release_and_recorder_share_complete_real_git_identity(self):
        expected = self.release.source_identity(self.source, self.commit)
        self.assertEqual(len(expected["lockHashes"]), 8)
        self.assertEqual(set(recorder.LOCKS), set(self.release.LOCKS))
        self.assertEqual(recorder.identity(self.source, self.commit), expected)

    def test_init_and_reload_accept_real_build_identity_and_reject_lock_mutation(self):
        arguments, candidate = self.candidate()
        if recorder.platform.system() != "Windows" or recorder.platform.machine().lower() not in ("amd64", "x86_64"):
            with self.assertRaisesRegex(ValueError, "native Windows x64"):
                recorder.initialize(arguments)
            return
        output = io.StringIO()
        with redirect_stdout(output):
            recorder.initialize(arguments)
        initialized = json.loads(output.getvalue())
        self.assertFalse(initialized["passed"])
        run, state = recorder.load_run(initialized["runRoot"])
        self.assertEqual(state["identity"], candidate["identity"])
        self.assertEqual(state["identity"]["lockHashes"], self.release.source_identity(self.source)["lockHashes"])
        self.assertEqual(list((run / "evidence").iterdir()), [])
        lock = self.source / "config/gpu-sdk.lock.json"
        lock.write_bytes(lock.read_bytes() + b"\n")
        with self.assertRaisesRegex(ValueError, "clean"):
            recorder.load_run(run)

    def test_init_rejects_missing_extra_and_changed_real_lock_hashes(self):
        arguments, candidate = self.candidate()
        original = dict(candidate["identity"]["lockHashes"])
        missing = {name: value for name, value in original.items() if name != "config/gpu-sdk.lock.json"}
        extra = {**original, "unrelated.lock": "0" * 64}
        changed = {**original, "config/gpu-sdk.lock.json": "0" * 64}
        for description, locks in (("missing", missing), ("extra", extra), ("changed", changed)):
            with self.subTest(locks=description):
                candidate["identity"]["lockHashes"] = locks
                recorder.write(arguments.candidate_record, candidate)
                with self.assertRaisesRegex(ValueError, "Build-time identity differs"):
                    recorder.initialize(arguments)
        self.assertFalse(arguments.run_parent.exists())


    def initialize_fixture(self, arguments):
        # Inert fixture files exercise the Windows recorder contract on every OS.
        # This platform substitution never represents actual Windows acceptance.
        output = io.StringIO()
        with (mock.patch.object(recorder.platform, "system", return_value="Windows"),
              mock.patch.object(recorder.platform, "machine", return_value="AMD64"),
              redirect_stdout(output)):
            recorder.initialize(arguments)
        result = json.loads(output.getvalue())
        self.assertFalse(result["passed"])
        self.assertEqual(result["status"], "initialized_no_ui_operations")
        return Path(result["runRoot"])

    def test_new_run_binds_original_candidate_and_keeps_canonical_executables(self):
        arguments, candidate = self.candidate()
        run = self.initialize_fixture(arguments)
        _, state = recorder.load_run(run)
        self.assertEqual(state["candidateRecordPath"], str(arguments.candidate_record.resolve()))
        self.assertEqual(state["candidateRecordSha256"], recorder.sha(arguments.candidate_record))
        self.assertEqual(state["executables"], candidate["executables"])
        self.assertTrue((run / "inputs/package-build-provenance.json").is_file())
        self.assertFalse((run / "inputs/package-ci-provenance.json").exists())
        self.assertTrue(all("absolutePath" not in value for value in state["executables"].values()))
        arguments.candidate_record.write_bytes(arguments.candidate_record.read_bytes() + b"\n")
        with self.assertRaisesRegex(ValueError, "Original candidate record changed"):
            recorder.load_run(run)

    def test_existing_local_run_without_original_record_fields_remains_loadable(self):
        arguments, _ = self.candidate()
        run = self.initialize_fixture(arguments)
        state = recorder.read(run / "run.json")
        state.pop("candidateRecordPath")
        state.pop("candidateRecordSha256")
        recorder.write(run / "run.json", state)
        self.assertEqual(recorder.load_run(run)[1], state)

    def test_run_identity_fields_cannot_diverge_from_its_retained_candidate(self):
        arguments, _ = self.candidate()
        run = self.initialize_fixture(arguments)
        original = recorder.read(run / "run.json")
        for key in ("identity", "packageSha256", "executables", "modelFingerprint", "extractionFingerprint"):
            with self.subTest(field=key):
                state = dict(original)
                state[key] = "SYNTHETIC changed run field"
                recorder.write(run / "run.json", state)
                with self.assertRaisesRegex(ValueError, "Run differs from its frozen candidate"):
                    recorder.load_run(run)


class RedirectedCliTests(unittest.TestCase):
    def test_init_and_summary_use_ascii_run_roots_and_preserve_chinese_content(self):
        # Only a Python CLI child is launched. Four dummy ZIP members are never
        # executed, source identity is mocked, and no real candidate is claimed.
        with tempfile.TemporaryDirectory(prefix="qcs-recorder-CLI-SYNTHETIC-", dir=os.environ.get("QCOPILOTS_SEARCH_TEST_ROOT") or None) as temporary:
            root = Path(temporary)
            source, app = root / "source", root / "app"
            source.mkdir()
            app.mkdir()
            package = root / "SYNTHETIC.zip"
            executables = {}
            with zipfile.ZipFile(package, "w") as archive:
                for name in recorder.NAMES:
                    data = ("SYNTHETIC NOT EXECUTABLE " + name).encode()
                    path = app / (name + ".exe")
                    path.write_bytes(data)
                    archive.writestr("bin/" + path.name, data)
                    executables[name] = {"sha256": recorder.sha(path)}
            provenance = root / "SYNTHETIC-provenance.json"
            recorder.write(provenance, {"syntheticTestOnly": True})
            candidate = {"schemaVersion": 1, "kind": "package-candidate", "syntheticTestOnly": True,
                         "identity": {"candidateHead": "a" * 40, "checkoutCommit": "a" * 40},
                         "recordedAt": START, "executables": executables, "packageSha256": recorder.sha(package),
                         "modelFingerprint": "SYNTHETIC-model", "extractionFingerprint": "SYNTHETIC-extraction",
                         "buildProvenance": {"path": provenance.name, "sha256": recorder.sha(provenance)}}
            candidate_path = root / "SYNTHETIC-candidate.json"
            recorder.write(candidate_path, candidate)
            harness = '''import importlib.util,json,sys
from unittest import mock
spec=importlib.util.spec_from_file_location("synthetic_cli",sys.argv[1])
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
c=json.load(open(sys.argv[2],encoding="utf-8"))
with mock.patch.object(m,"identity",return_value=c["identity"]), mock.patch.object(m,"fingerprints",return_value={k:c[k] for k in ("modelFingerprint","extractionFingerprint")}), mock.patch.object(m,"inspect_package_candidate"), mock.patch.object(m.subprocess,"check_output",return_value=""), mock.patch.object(m.platform,"system",return_value="Windows"), mock.patch.object(m.platform,"machine",return_value="AMD64"):
    raise SystemExit(m.main(sys.argv[3:]))
'''
            environment = {**os.environ, "PYTHONIOENCODING": "ascii"}
            prefix = [sys.executable, "-B", "-c", harness, str(SCRIPT), str(candidate_path)]
            result = subprocess.run([*prefix, "init", "--candidate-record", str(candidate_path), "--package", str(package),
                                     "--app-dir", str(app), "--source", str(source), "--run-parent", str(root / "runs")],
                                    capture_output=True, env=environment, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8"))
            initialized = json.loads(result.stdout.decode("utf-8"))
            run = Path(initialized["runRoot"])
            self.assertTrue(run.name.startswith("qcs-cu-run-"))
            self.assertTrue(run.name.isascii())
            self.assertFalse(any(character.isspace() for character in run.name))
            self.assertTrue((run / "run.json").is_file())
            state = recorder.read(run / "run.json")
            for key in ("profileRoot", "modelDirectory", "samplesDirectory"):
                directory = Path(state[key])
                self.assertEqual(directory.parent, run)
                self.assertTrue(directory.name.isascii())
                self.assertFalse(any(character.isspace() for character in directory.name))
            sample = Path(state["samplesDirectory"]) / "中文搜索样本.txt"
            sample.write_text("中文搜索内容保留", encoding="utf-8")
            started = subprocess.run([*prefix, "begin", "--run", str(run), "--scenario", "CU-01",
                                      "--expected", "中文搜索内容保留"],
                                     capture_output=True, env=environment, timeout=30)
            self.assertEqual(started.returncode, 0, started.stderr.decode("utf-8"))
            summary = subprocess.run([*prefix, "summarize", "--run", str(run)], capture_output=True, env=environment, timeout=30)
            self.assertEqual(summary.returncode, 1, summary.stderr.decode("utf-8"))
            report = json.loads(summary.stdout.decode("utf-8"))
            self.assertFalse(report["passed"])
            self.assertTrue(Path(report["report"]).is_file())
            retained = recorder.read(report["report"])
            self.assertEqual(retained["scenarios"][0]["expected"], "中文搜索内容保留")
            self.assertEqual(sample.read_text(encoding="utf-8"), "中文搜索内容保留")


class CiCandidateRecorderTests(unittest.TestCase):
    """Real freeze/candidate readers over inert packages, never CI or UI acceptance."""

    def setUp(self):
        fixture_path = SCRIPT.parents[2] / ".github/scripts/qcs-tests-release-evidence.py"
        spec = importlib.util.spec_from_file_location("cu_ci_origin_fixtures", fixture_path)
        self.fixtures = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.fixtures)
        self.fixture = self.fixtures.CiOriginEvidenceTests(
            "test_existing_local_build_candidate_remains_valid_without_release_comparison")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        path, candidate, _, manifest, verification = self.fixture.ci_candidate()
        self.candidate = candidate
        report = recorder.read(verification)
        self.arguments = SimpleNamespace(candidate_record=path, source=self.fixture.source,
            package=manifest.parent / "assets" / Path(candidate["package"]).name,
            app_dir=Path(report["modelSmoke"]["runtime"]["workerExecutable"]["path"]).parent,
            run_parent=self.fixture.root / "synthetic CI recorder runs", fixture_manifest=None)
        # Keep all actual candidate, artifact, digest and package validation.
        # Only source material production and declarative fingerprint parsing
        # use synthetic boundaries. The temporary Git identity remains real.
        for patch in (mock.patch.object(recorder, "release_evidence", return_value=self.fixtures.evidence),
                      mock.patch.object(recorder, "fingerprints", return_value={
                          name: candidate[name] for name in ("modelFingerprint", "extractionFingerprint")})):
            patch.start()
            self.addCleanup(patch.stop)

    def initialize(self):
        return RealCandidateIdentityTests.initialize_fixture(self, self.arguments)

    def test_ci_candidate_initializes_and_reloads_without_invented_local_build(self):
        self.assertNotIn("buildProvenance", self.candidate)
        self.assertNotEqual(self.arguments.package.resolve(),
                            (self.arguments.candidate_record.parent / self.candidate["package"]).resolve())
        run = self.initialize()
        _, state = recorder.load_run(run)
        self.assertEqual(state["identity"], self.candidate["identity"])
        self.assertEqual(state["executables"], self.candidate["executables"])
        self.assertTrue((run / "inputs/package-ci-provenance.json").is_file())
        self.assertFalse((run / "inputs/package-build-provenance.json").exists())
        paths = {item["path"] for item in state["inputs"]}
        self.assertEqual(len([path for path in paths if "/ci/artifacts/" in path]), 1)
        self.assertFalse(any("/ci/package/" in path for path in paths))
        self.assertFalse(any(path.endswith("begin.json") or "/build/" in path for path in paths))
        with redirect_stdout(io.StringIO()):
            self.assertEqual(recorder.summarize(SimpleNamespace(run=run)), 1)
        report = next(recorder.read(path) for path in run.glob("windows-computer-use-*.json")
                      if not path.stem.endswith("-files"))
        self.assertFalse(report["passed"])
        self.assertEqual([item["id"] for item in report["scenarios"]], list(recorder.CASES))
        self.assertTrue(all(item["status"] == "not_run" for item in report["scenarios"]))
        self.assertEqual(report["executables"], self.candidate["executables"])

    def test_ci_initialization_rejects_different_package_and_each_changed_program(self):
        package = self.arguments.package
        original = package.read_bytes()
        alternative = self.fixture.root / "SYNTHETIC-same-programs-other-zip.zip"
        alternative.write_bytes(original)
        with zipfile.ZipFile(alternative, "a") as archive:
            archive.comment = b"SYNTHETIC different archive, same commit and executable bytes"
        self.assertNotEqual(recorder.sha(alternative), self.candidate["packageSha256"])
        with zipfile.ZipFile(package) as expected, zipfile.ZipFile(alternative) as changed:
            self.assertEqual({item.filename: expected.read(item) for item in expected.infolist()},
                             {item.filename: changed.read(item) for item in changed.infolist()})
        self.arguments.package = alternative
        with self.assertRaisesRegex(ValueError, "package SHA differs"):
            self.initialize()
        self.arguments.package = package
        package.write_bytes(original + b"SYNTHETIC ZIP changed")
        with self.assertRaises(ValueError):
            self.initialize()
        package.write_bytes(original)
        for name in recorder.NAMES:
            path = self.arguments.app_dir / (name + ".exe")
            original = path.read_bytes()
            path.write_bytes(original[:-1] + bytes([original[-1] ^ 1]))
            with self.subTest(program=name), self.assertRaisesRegex(ValueError, "disagree"):
                self.initialize()
            path.write_bytes(original)
        self.assertFalse(self.arguments.run_parent.exists())

    def test_ci_reload_rejects_package_and_each_changed_program(self):
        run = self.initialize()
        package = self.arguments.package
        original = package.read_bytes()
        package.write_bytes(original + b"SYNTHETIC changed after init")
        with self.assertRaises(ValueError):
            recorder.load_run(run)
        package.write_bytes(original)
        for name in recorder.NAMES:
            path = self.arguments.app_dir / (name + ".exe")
            original = path.read_bytes()
            path.write_bytes(original[:-1] + bytes([original[-1] ^ 1]))
            with self.subTest(program=name), self.assertRaisesRegex(ValueError, "disagree"):
                recorder.load_run(run)
            path.write_bytes(original)
        recorder.load_run(run)

    def test_ci_reload_rejects_changed_retained_and_original_provenance(self):
        run = self.initialize()
        retained = run / "inputs/package-ci-provenance.json"
        original = self.arguments.candidate_record.parent / self.candidate["ciProvenance"]["path"]
        for path in (retained, original):
            data = path.read_bytes()
            path.write_bytes(data + b"\n")
            with self.subTest(path=path.name), self.assertRaises(ValueError):
                recorder.load_run(run)
            path.write_bytes(data)
        recorder.load_run(run)

    def test_ci_reload_requires_original_candidate_binding(self):
        run = self.initialize()
        state = recorder.read(run / "run.json")
        state.pop("candidateRecordPath")
        state.pop("candidateRecordSha256")
        recorder.write(run / "run.json", state)
        with self.assertRaisesRegex(ValueError, "missing its original provenance binding"):
            recorder.load_run(run)

    def test_ci_candidate_mutation_during_copy_is_rejected(self):
        original_copy = recorder.shutil.copyfile
        candidate_path = self.arguments.candidate_record
        def changed_copy(source, destination, *args, **kwargs):
            if Path(source) == candidate_path:
                changed = recorder.read(candidate_path)
                changed["recordedAt"] = START
                recorder.write(candidate_path, changed)
            return original_copy(source, destination, *args, **kwargs)
        with mock.patch.object(recorder.shutil, "copyfile", side_effect=changed_copy):
            with self.assertRaisesRegex(ValueError, "changed while being captured"):
                self.initialize()
        self.assertFalse(list(self.arguments.run_parent.rglob("run.json")))

    def test_ci_provenance_mutation_during_copy_is_rejected(self):
        original_copy = recorder.shutil.copyfile
        provenance = self.arguments.candidate_record.parent / self.candidate["ciProvenance"]["path"]
        def changed_copy(source, destination, *args, **kwargs):
            if Path(source) == provenance:
                provenance.write_bytes(provenance.read_bytes() + b"\n")
            return original_copy(source, destination, *args, **kwargs)
        with mock.patch.object(recorder.shutil, "copyfile", side_effect=changed_copy):
            with self.assertRaisesRegex(ValueError, "changed while being captured"):
                self.initialize()
        self.assertFalse(list(self.arguments.run_parent.rglob("run.json")))

    def test_ci_evidence_copy_corruption_of_same_size_is_rejected(self):
        original_copy = recorder.shutil.copyfile
        entry = next(item for item in self.candidate["files"] if item["path"].startswith("model-")
                     and item["path"].endswith("-reply.json"))
        evidence_path = self.arguments.candidate_record.parent / entry["path"]
        def changed_copy(source, destination, *args, **kwargs):
            result = original_copy(source, destination, *args, **kwargs)
            if Path(source) == evidence_path:
                path = Path(destination)
                data = path.read_bytes()
                path.write_bytes(data[:-1] + bytes([data[-1] ^ 1]))
            return result
        with mock.patch.object(recorder.shutil, "copyfile", side_effect=changed_copy):
            with self.assertRaisesRegex(ValueError, "changed while being captured"):
                self.initialize()
        self.assertFalse(list(self.arguments.run_parent.rglob("run.json")))

    def test_ci_program_changed_while_inputs_are_captured_is_rejected(self):
        original_copy = recorder.shutil.copyfile
        def changed_copy(source, destination, *args, **kwargs):
            result = original_copy(source, destination, *args, **kwargs)
            if Path(source) == self.arguments.candidate_record:
                program = self.arguments.app_dir / "qcs-gui.exe"
                program.write_bytes(program.read_bytes() + b"SYNTHETIC changed during capture")
            return result
        with mock.patch.object(recorder.shutil, "copyfile", side_effect=changed_copy):
            with self.assertRaisesRegex(ValueError, "disagree"):
                self.initialize()
        self.assertFalse(list(self.arguments.run_parent.rglob("run.json")))


if __name__ == "__main__":
    print(json.dumps({"testCapabilities": CAPABILITIES}, ensure_ascii=False), file=sys.stderr)
    unittest.main(verbosity=2)
