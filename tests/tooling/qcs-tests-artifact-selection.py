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
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

sys.dont_write_bytecode = True
SCRIPT = Path(__file__).resolve().parents[2] / ".github/scripts/qcs-artifact-selection.py"
spec = importlib.util.spec_from_file_location("qcs_artifact_selection_tests", SCRIPT)
selection = importlib.util.module_from_spec(spec)
spec.loader.exec_module(selection)
HEAD = "a" * 40
RUN = "1234567"
REPOSITORY = "fixture-owner/QCopilots-Search"


def package_suffixes(platform):
    if platform.startswith("windows-"):
        return (".exe", ".zip")
    if platform.startswith("linux-"):
        return (".deb", ".tar.gz")
    if platform.startswith("macos-"):
        return (".dmg",)
    raise ValueError("Unknown fixture platform: " + platform)


def package_name(platform, suffix):
    return selection.package_stem(platform, selection.project_version(SCRIPT.parents[2])) + suffix


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


def artifact(source, identifier, suffix=None):
    platform = source["name"].removeprefix("qcs ")
    return {"id": identifier,
            "name": package_name(platform, suffix or package_suffixes(platform)[0]),
            "expired": False, "digest": "sha256:" + f"{identifier:064x}",
            "size_in_bytes": identifier + 100,
            "created_at": source["steps"][-1]["started_at"],
            "workflow_run": {"id": int(RUN), "head_sha": HEAD}}


def packages(source, first_id):
    platform = source["name"].removeprefix("qcs ")
    return [artifact(source, first_id + index, suffix)
            for index, suffix in enumerate(package_suffixes(platform))]


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
        self.artifacts = [package for index, source in enumerate(self.jobs)
                          for package in packages(source, 1001 + index * 10)]

    def inspect(self, attempt=1):
        return selection.select(REPOSITORY, RUN, HEAD, attempt, self.jobs, self.artifacts)

    def partial_retry(self, *, cloned=True, platform="windows-x64-cpu"):
        original = list(self.jobs)
        retried = job(platform, 101, attempt=2, hour=2)
        self.jobs.append(retried)
        self.artifacts.extend(packages(retried, 2001))
        if cloned:
            self.jobs.extend(clone(source, index + 102, 2) for index, source in enumerate(original)
                             if source["name"] != "qcs " + platform)
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
        self.assertEqual(len(self.artifacts), 21)
        self.assertEqual(sum(len(entry["artifacts"]) for entry in result["selected"].values()), 21)
        for index, platform in enumerate(selection.CONFIGURATIONS):
            entry = result["selected"][platform]
            self.assertEqual(entry["sourceAttempt"], 1)
            self.assertEqual(entry["sourceJobId"], index + 1)
            self.assertEqual(entry["observedJobId"], index + 1)
            expected = packages(self.jobs[index], 1001 + index * 10)
            self.assertEqual(entry["artifacts"], [
                {"artifactId": item["id"], "artifactName": item["name"],
                 "artifactDigest": item["digest"], "artifactSize": item["size_in_bytes"]}
                for item in expected])

    def test_shared_manifest_defines_all_eleven_configurations(self):
        manifest = json.loads((SCRIPT.parents[2] / "config/build-variants.json").read_text(encoding="utf-8"))
        self.assertEqual(selection.CONFIGURATIONS, tuple(row["id"] for row in manifest["variants"]))
        self.assertEqual(len(selection.CONFIGURATIONS), 11)
        self.assertEqual(selection.PLATFORMS, selection.CONFIGURATIONS)
        self.assertFalse(any(identifier.startswith("macos-x64") for identifier in selection.CONFIGURATIONS))

    def test_rocm_and_vulkan_packages_include_the_exact_locked_sdk_version(self):
        expected = {
            "windows-x64-rocm": "windows-x64-rocm-10.0.0",
            "linux-x64-rocm": "linux-x64-rocm-10.0.0",
            "windows-x64-vulkan": "windows-x64-vulkan-1.4.357.0",
            "linux-x64-vulkan": "linux-x64-vulkan-1.4.313.0",
        }
        for identifier, package_variant in expected.items():
            with self.subTest(identifier=identifier):
                self.assertEqual(selection.package_stem(identifier, "0.3.0"), "qcs-0.3.0-" + package_variant)
        for identifier in selection.CONFIGURATIONS:
            if identifier not in expected:
                self.assertEqual(selection.package_stem(identifier, "0.3.0"), "qcs-0.3.0-" + identifier)
        self.assertEqual(selection.package_name_options("windows-x64-rocm", "0.3.0"), [
            {"qcs-0.3.0-windows-x64-rocm-10.0.0.exe", "qcs-0.3.0-windows-x64-rocm-10.0.0.zip"},
            {"qcs-0.3.0-windows-x64-rocm-10.0.0-setup.zip", "qcs-0.3.0-windows-x64-rocm-10.0.0.zip"},
        ])

    def test_current_manifest_rejects_missing_unsafe_or_inconsistent_package_names(self):
        manifest = json.loads((selection.SOURCE / "config/build-variants.json").read_text(encoding="utf-8"))
        lock = json.loads((selection.SOURCE / "config/gpu-sdk.lock.json").read_text(encoding="utf-8"))
        for identifier in selection.CONFIGURATIONS:
            for incorrect in (None, "", identifier + "-wrong", "../package", "package/name", "package\n"):
                with self.subTest(identifier=identifier, incorrect=incorrect):
                    changed = deepcopy(manifest)
                    row = next(row for row in changed["variants"] if row["id"] == identifier)
                    if incorrect is None:
                        del row["packageVariant"]
                    else:
                        row["packageVariant"] = incorrect
                    with self.assertRaisesRegex(ValueError, "packageVariant"):
                        selection.validate_variants(changed, lock)

    def test_toolkit_version_must_match_sdk_lock_even_with_consistent_package_name(self):
        manifest = json.loads((selection.SOURCE / "config/build-variants.json").read_text(encoding="utf-8"))
        lock = json.loads((selection.SOURCE / "config/gpu-sdk.lock.json").read_text(encoding="utf-8"))
        for row in manifest["variants"]:
            if row["backend"] not in ("cuda", "hip", "vulkan"):
                continue
            with self.subTest(identifier=row["id"]):
                changed = deepcopy(manifest)
                variant = next(item for item in changed["variants"] if item["id"] == row["id"])
                variant["toolkitVersion"] = "99.0.0"
                if variant["backend"] in ("hip", "vulkan"):
                    variant["packageVariant"] = variant["id"] + "-99.0.0"
                with self.assertRaisesRegex(ValueError, "SDK lock"):
                    selection.validate_variants(changed, lock)
        changed_lock = deepcopy(lock)
        changed_lock["upstreamCommit"] = "f" * 40
        with self.assertRaisesRegex(ValueError, "upstream commit"):
            selection.validate_variants(manifest, changed_lock)

    def test_unversioned_or_wrong_sdk_artifacts_cannot_satisfy_current_gate(self):
        original = deepcopy(self.artifacts)
        for platform in ("windows-x64-rocm", "linux-x64-rocm", "windows-x64-vulkan", "linux-x64-vulkan"):
            current = selection.package_stem(platform, selection.project_version(SCRIPT.parents[2]))
            old = f"qcs-{selection.project_version(SCRIPT.parents[2])}-{platform}"
            for replacement in (old, old + "-99.0.0"):
                with self.subTest(platform=platform, replacement=replacement):
                    self.artifacts = deepcopy(original)
                    for artifact_row in self.artifacts:
                        if artifact_row["name"].startswith(current):
                            artifact_row["name"] = replacement + artifact_row["name"][len(current):]
                    result = self.inspect()
                    self.assertFalse(result["passed"])
                    self.assertNotIn(platform, result["selected"])

    def cmake_variant(self, variant, module=None):
        cmake = shutil.which("cmake")
        self.assertIsNotNone(cmake, "Package variant tests require the project CMake toolchain")
        script = self.root / "select-package-variant.cmake"
        script.write_text('set(WIN32 "${QCS_TEST_WIN32}")\nset(APPLE "${QCS_TEST_APPLE}")\n'
                          'include("${QCS_VARIANTS_MODULE}")\n'
                          'qcopilots_search_select_variant()\n'
                          'if(NOT QCOPILOTS_SEARCH_PACKAGE_VARIANT STREQUAL QCS_EXPECTED_PACKAGE_VARIANT)\n'
                          '  message(FATAL_ERROR "Unexpected package variant: ${QCOPILOTS_SEARCH_PACKAGE_VARIANT}")\n'
                          'endif()\n', encoding="utf-8")
        return subprocess.run([cmake,
            "-DQCS_VARIANTS_MODULE=" + str(module or selection.SOURCE / "cmake/qcs-gpu-variants.cmake"),
            "-DQCOPILOTS_SEARCH_VARIANT=" + variant["id"],
            "-DQCS_EXPECTED_PACKAGE_VARIANT=" + variant["packageVariant"],
            "-DQCS_TEST_WIN32=" + ("TRUE" if variant["os"] == "windows" else "FALSE"),
            "-DQCS_TEST_APPLE=" + ("TRUE" if variant["os"] == "macos" else "FALSE"),
            "-DCMAKE_SYSTEM_NAME=" + {"windows": "Windows", "linux": "Linux", "macos": "Darwin"}[variant["os"]],
            "-DCMAKE_SYSTEM_PROCESSOR=" + {"x64": "AMD64", "arm64": "arm64"}[variant["arch"]],
            "-P", str(script)], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)

    def test_cmake_exports_same_package_names_for_all_eleven_variants(self):
        for variant in selection.variants().values():
            with self.subTest(identifier=variant["id"]):
                result = self.cmake_variant(variant)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_cmake_package_variant_is_visible_outside_runtime_subdirectory(self):
        source = self.root / "cmake-scope"
        (source / "runtime").mkdir(parents=True)
        (source / "CMakeLists.txt").write_text(
            'cmake_minimum_required(VERSION 3.28)\nproject(package_contract NONE)\n'
            'add_subdirectory(runtime)\n'
            'if(NOT QCOPILOTS_SEARCH_PACKAGE_VARIANT STREQUAL "windows-x64-vulkan-1.4.357.0")\n'
            '  message(FATAL_ERROR "Package identity was lost outside the runtime directory")\n'
            'endif()\n', encoding="utf-8")
        (source / "runtime/CMakeLists.txt").write_text(
            'set(WIN32 TRUE)\nset(APPLE FALSE)\nset(CMAKE_SYSTEM_NAME Windows)\n'
            'set(CMAKE_SYSTEM_PROCESSOR AMD64)\ninclude("${QCS_VARIANTS_MODULE}")\n'
            'qcopilots_search_select_variant()\n', encoding="utf-8")
        result = subprocess.run([shutil.which("cmake"), "-S", str(source), "-B", str(self.root / "scope-build"),
            "-DQCS_VARIANTS_MODULE=" + str(selection.SOURCE / "cmake/qcs-gpu-variants.cmake"),
            "-DQCOPILOTS_SEARCH_VARIANT=windows-x64-vulkan"], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_cmake_rejects_wrong_sdk_and_unversioned_package_name(self):
        copied = self.root / "candidate"
        (copied / "cmake").mkdir(parents=True)
        (copied / "config").mkdir()
        module = copied / "cmake/qcs-gpu-variants.cmake"
        shutil.copy2(selection.SOURCE / "cmake/qcs-gpu-variants.cmake", module)
        shutil.copy2(selection.SOURCE / "config/gpu-sdk.lock.json", copied / "config/gpu-sdk.lock.json")
        manifest = json.loads((selection.SOURCE / "config/build-variants.json").read_text(encoding="utf-8"))
        for platform in ("windows-x64-rocm", "linux-x64-rocm", "windows-x64-vulkan", "linux-x64-vulkan"):
            for mode in ("wrong-sdk", "old-name"):
                with self.subTest(platform=platform, mode=mode):
                    changed = deepcopy(manifest)
                    variant = next(row for row in changed["variants"] if row["id"] == platform)
                    if mode == "wrong-sdk":
                        variant.update(toolkitVersion="99.0.0", packageVariant=platform + "-99.0.0")
                    else:
                        variant["packageVariant"] = platform
                    self.write_document(copied / "config/build-variants.json", changed)
                    result = self.cmake_variant(variant, module)
                    self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertIn("GPU SDK lock" if mode == "wrong-sdk" else "packageVariant", result.stderr)

    def test_only_packages_artifact_names_can_satisfy_gate(self):
        expected = package_name("windows-x64-cpu", ".exe")
        self.assertEqual(self.artifacts[0]["name"], expected)
        self.artifacts[0]["name"] = expected.replace("qcs-", "qcs-native-", 1)
        self.assertFalse(self.inspect()["passed"])
        self.artifacts[0]["name"] = expected
        result = self.inspect()
        self.assertTrue(result["passed"], result["errors"])
        self.assertEqual(result["schemaVersion"], 3)
        self.assertEqual(set(result), {"schemaVersion", "kind", "repository", "runId", "aggregateAttempt",
                                      "candidateHead", "version", "passed", "errors", "selected", "excluded"})
        self.assertEqual(result["version"], selection.project_version(SCRIPT.parents[2]))

    def test_every_raw_package_is_required_not_only_one_file_per_variant(self):
        original = deepcopy(self.artifacts)
        for artifact_row in original:
            with self.subTest(name=artifact_row["name"]):
                self.artifacts = [row for row in original if row["id"] != artifact_row["id"]]
                result = self.inspect()
                self.assertFalse(result["passed"])
                self.assertEqual(len(result["selected"]), len(selection.CONFIGURATIONS) - 1)

    def test_package_version_and_stable_exact_file_names_are_required(self):
        original = deepcopy(self.artifacts[0])
        current = selection.project_version(SCRIPT.parents[2])
        for name in (original["name"].replace(current, "999.0.0", 1),
                     original["name"].replace(current + "-", "", 1),
                     original["name"].removesuffix(".exe") + "-" + HEAD[:12] + "-attempt1.exe",
                     original["name"] + ".zip"):
            with self.subTest(name=name):
                self.artifacts[0] = dict(original, name=name)
                result = self.inspect()
                self.assertFalse(result["passed"])
                self.assertNotIn("windows-x64-cpu", result["selected"])

    def test_raw_artifact_size_must_be_positive_integer_below_two_gib(self):
        original = deepcopy(self.artifacts[0])
        for size in (None, False, True, 0, -1, 1.0, "100", 2 ** 31, 2 ** 31 + 1):
            with self.subTest(size=size):
                self.artifacts[0] = dict(original, size_in_bytes=size)
                self.assertFalse(self.inspect()["passed"])
        self.artifacts[0] = deepcopy(original)
        del self.artifacts[0]["size_in_bytes"]
        self.assertFalse(self.inspect()["passed"])
        self.artifacts[0] = dict(original, size_in_bytes=2 ** 31 - 1)
        result = self.inspect()
        self.assertTrue(result["passed"], result["errors"])
        self.assertEqual(result["selected"]["windows-x64-cpu"]["artifacts"][0]["artifactSize"],
                         2 ** 31 - 1)

    def test_rocm_setup_zip_replaces_only_installer_and_keeps_portable_zip(self):
        installer = next(row for row in self.artifacts
                         if row["name"] == package_name("windows-x64-rocm", ".exe"))
        installer["name"] = package_name("windows-x64-rocm", "-setup.zip")
        result = self.inspect()
        self.assertTrue(result["passed"], result["errors"])
        names = {row["artifactName"] for row in result["selected"]["windows-x64-rocm"]["artifacts"]}
        self.assertEqual(names, {package_name("windows-x64-rocm", "-setup.zip"),
                                 package_name("windows-x64-rocm", ".zip")})
        self.assertEqual(sum(len(row["artifacts"]) for row in result["selected"].values()), 21)

    def test_rocm_same_execution_cannot_supply_both_installer_formats(self):
        source = next(row for row in self.jobs if row["name"] == "qcs windows-x64-rocm")
        self.artifacts.append(artifact(source, 9000, "-setup.zip"))
        result = self.inspect()
        self.assertFalse(result["passed"])
        self.assertNotIn("windows-x64-rocm", result["selected"])

    def test_rocm_rerun_format_switch_excludes_old_alternate_and_old_portable(self):
        original_jobs, original_artifacts = deepcopy(self.jobs), deepcopy(self.artifacts)
        for previous, current in ((".exe", "-setup.zip"), ("-setup.zip", ".exe")):
            with self.subTest(previous=previous, current=current):
                self.jobs, self.artifacts = deepcopy(original_jobs), deepcopy(original_artifacts)
                old = next(row for row in self.artifacts
                           if row["name"] == package_name("windows-x64-rocm", ".exe"))
                old["name"] = package_name("windows-x64-rocm", previous)
                old_ids = {row["id"] for row in self.artifacts
                           if row["name"] in (old["name"], package_name("windows-x64-rocm", ".zip"))}
                self.partial_retry(platform="windows-x64-rocm")
                fresh = next(row for row in self.artifacts if row["id"] == 2001)
                fresh["name"] = package_name("windows-x64-rocm", current)
                result = self.inspect(2)
                self.assertTrue(result["passed"], result["errors"])
                entry = result["selected"]["windows-x64-rocm"]
                self.assertEqual(entry["sourceAttempt"], 2)
                self.assertEqual({row["artifactId"] for row in entry["artifacts"]}, {2001, 2002})
                self.assertEqual({row["artifactId"] for row in result["excluded"]}, old_ids)

    def test_setup_zip_is_not_an_installer_alternative_for_other_windows_variants(self):
        self.artifacts[0]["name"] = package_name("windows-x64-cpu", "-setup.zip")
        self.assertFalse(self.inspect()["passed"])

    def test_every_configuration_requires_its_job_and_package_artifact(self):
        original_jobs = list(self.jobs)
        original_artifacts = list(self.artifacts)
        for index, configuration in enumerate(selection.CONFIGURATIONS):
            for missing in ("job", "artifact"):
                with self.subTest(configuration=configuration, missing=missing):
                    self.jobs = [row for number, row in enumerate(original_jobs)
                                 if missing != "job" or number != index]
                    self.artifacts = [row for row in original_artifacts
                                      if missing != "artifact" or row["id"] != 1001 + index * 10]
                    result = self.inspect()
                    self.assertFalse(result["passed"])
                    self.assertNotIn(configuration, result["selected"])
                    self.assertEqual(len(result["selected"]), len(selection.CONFIGURATIONS) - 1)

    def test_partial_retry_reuses_original_execution_of_copied_jobs(self):
        self.partial_retry()
        result = self.inspect(2)
        self.assertTrue(result["passed"], result["errors"])
        self.assertEqual(result["selected"]["windows-x64-cpu"]["sourceAttempt"], 2)
        self.assertEqual([row["artifactId"] for row in result["selected"]["windows-x64-cpu"]["artifacts"]],
                         [2001, 2002])
        for identifier, platform in enumerate(selection.CONFIGURATIONS[1:], 2):
            entry = result["selected"][platform]
            self.assertEqual(entry["sourceAttempt"], 1)
            self.assertEqual(entry["sourceJobId"], identifier)
            self.assertNotEqual(entry["observedJobId"], identifier)
        self.assertEqual([item["artifactId"] for item in result["excluded"]], [1001, 1002])

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
        self.assertTrue(result["errors"])

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
        self.artifacts.extend(packages(recovered, 3001))
        copied = [clone(source, index + 202, 3)
                  for index, source in enumerate(self.jobs[1:len(selection.CONFIGURATIONS)])]
        self.jobs.extend(copied)
        current = [recovered, *copied]
        self.jobs.extend(clone(source, index + 301, 4) for index, source in enumerate(current))
        result = self.inspect(4)
        self.assertTrue(result["passed"], result["errors"])
        self.assertEqual(result["selected"]["windows-x64-cpu"]["sourceAttempt"], 3)
        self.assertEqual([row["artifactId"] for row in result["selected"]["windows-x64-cpu"]["artifacts"]],
                         [3001, 3002])

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
        self.artifacts[0]["name"] = f"qcs-packages-windows-x64-cpu-{HEAD}-attempt1"
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
        for version in (1, 2, 4, True, 3.0):
            with self.subTest(version=version):
                document["schemaVersion"] = version
                self.write_document(path, document)
                with self.assertRaisesRegex(ValueError, "Unsupported artifact selection document"):
                    selection.load_selection(path)

    def test_load_rejects_boolean_or_float_substitution_for_integer_identities(self):
        path = self.write_snapshot()
        original = json.loads(path.read_text(encoding="utf-8"))
        for key, value in (("sourceJobId", True), ("sourceAttempt", True)):
            with self.subTest(key=key):
                document = deepcopy(original)
                document["selected"]["windows-x64-cpu"][key] = value
                self.write_document(path, document)
                with self.assertRaisesRegex(ValueError, "preserved API execution history"):
                    selection.load_selection(path)

    def test_load_recomputes_each_raw_artifact_field_and_rejects_substitutions(self):
        path = self.write_snapshot()
        original = json.loads(path.read_text(encoding="utf-8"))
        mutations = (("artifactId", 1001.0), ("artifactId", True), ("artifactId", 9999),
                     ("artifactSize", True), ("artifactSize", 1101.0), ("artifactSize", 1102),
                     ("artifactDigest", "sha256:" + "f" * 64),
                     ("artifactName", package_name("windows-x64-cpu", "-renamed.exe")))
        for key, value in mutations:
            with self.subTest(key=key, value=value):
                document = deepcopy(original)
                document["selected"]["windows-x64-cpu"]["artifacts"][0][key] = value
                self.write_document(path, document)
                with self.assertRaisesRegex(ValueError, "preserved API execution history"):
                    selection.load_selection(path)

    def test_load_rejects_missing_or_extra_packages_and_legacy_flat_entry(self):
        path = self.write_snapshot()
        original = json.loads(path.read_text(encoding="utf-8"))
        for mutation in ("missing", "extra", "legacy"):
            with self.subTest(mutation=mutation):
                document = deepcopy(original)
                entry = document["selected"]["windows-x64-cpu"]
                if mutation == "missing":
                    entry["artifacts"].pop()
                elif mutation == "extra":
                    entry["artifacts"].append(deepcopy(entry["artifacts"][0]))
                else:
                    artifact_row = entry.pop("artifacts")[0]
                    entry.update(artifact_row)
                self.write_document(path, document)
                with self.assertRaisesRegex(ValueError, "preserved API execution history"):
                    selection.load_selection(path)

    def test_load_rejects_missing_or_changed_version_and_unexpected_top_level_fields(self):
        path = self.write_snapshot()
        original = json.loads(path.read_text(encoding="utf-8"))
        for mutation in ("missing-version", "changed-version", "extra-field"):
            with self.subTest(mutation=mutation):
                document = deepcopy(original)
                if mutation == "missing-version":
                    del document["version"]
                elif mutation == "changed-version":
                    document["version"] = "999.0.0"
                else:
                    document["ignoredAuthority"] = True
                self.write_document(path, document)
                with self.assertRaises(ValueError):
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
        expected_ids = ",".join(str(item["id"]) for item in self.artifacts)
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
