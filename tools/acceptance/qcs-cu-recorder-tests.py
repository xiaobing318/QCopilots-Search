#!/usr/bin/env python3
"""Synthetic stdlib tests for the external CU recorder; no UI or real candidate.

Every fixture and generated export lives in a fresh TemporaryDirectory. No test
creates ten passing cases or a passed CU summary. subprocess is blocked in every
test. A mocked load_run supplies clearly marked synthetic state where needed.
"""
from __future__ import annotations

import base64
from contextlib import redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
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
        self.temp = Path(temporary.name)
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
            self.assertTrue(alias.is_junction())
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


if __name__ == "__main__":
    print(json.dumps({"testCapabilities": CAPABILITIES}, ensure_ascii=False), file=sys.stderr)
    unittest.main(verbosity=2)
