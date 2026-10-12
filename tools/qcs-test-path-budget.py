#!/usr/bin/env python3
"""Portable path inspection regression tests, including a real Ninja graph."""
from __future__ import annotations

import argparse
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

sys.dont_write_bytecode = True
SCRIPT = Path(__file__).resolve().with_name("qcs-path-budget.py")
SPEC = importlib.util.spec_from_file_location("qcs_path_budget", SCRIPT)
budget = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(budget)


class PathBudgetTests(unittest.TestCase):
    def setUp(self):
        configured = os.environ.get("QCOPILOTS_SEARCH_TEST_ROOT") or None
        if configured:
            Path(configured).mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="qcs-pb-", dir=configured)
        self.root = Path(self.temporary.name)

    def tearDown(self):
        # Cleanup errors deliberately fail the test instead of becoming warnings.
        self.temporary.cleanup()

    def test_empty_temporary_root_uses_system_default_not_cwd(self):
        nested = PathBudgetTests("test_classification")
        with patch.dict(os.environ, {"QCOPILOTS_SEARCH_TEST_ROOT": ""}), \
                patch.object(tempfile, "TemporaryDirectory", wraps=tempfile.TemporaryDirectory) as create:
            nested.setUp()
            try:
                self.assertIsNone(create.call_args.kwargs["dir"])
                self.assertEqual(nested.root.parent.resolve(), Path(tempfile.gettempdir()).resolve())
                self.assertNotEqual(nested.root.parent.resolve(), SCRIPT.parent.resolve())
            finally:
                nested.tearDown()

    def args(self, **kwargs):
        values = dict(source=None, root=self.root, build=None, stage=None, package_root=None,
                      platform="windows", ninja=None, allow_stress_root=False)
        values.update(kwargs)
        return argparse.Namespace(**values)

    def ninja(self):
        explicit = os.environ.get("QCOPILOTS_SEARCH_NINJA")
        found = shutil.which(explicit or "ninja")
        self.assertIsNotNone(found, "Real Ninja is required. Set QCOPILOTS_SEARCH_NINJA or PATH.")
        return found

    def graph(self):
        build = self.root / "构建 空格"
        build.mkdir()
        (build / "build.ninja").write_text(
            "rule compile\n  command = unused-compiler $in $out\n"
            "rule generate\n  command = unused-generator $out\n"
            "build CMakeFiles/qcs.dir/file.cpp.obj: compile\n"
            "build qcs_autogen/mocs_compilation.cpp: generate\n"
            "build generated$ 文件/qrc_resources.cpp: generate\n"
            "build qcs.exe: generate CMakeFiles/qcs.dir/file.cpp.obj\n"
            "build all: phony qcs.exe\n",
            encoding="utf-8")
        return build

    def test_utf16_counts_non_bmp_and_boundaries(self):
        self.assertEqual(budget.path_length("a" * 198 + "😀", "windows"), 200)
        self.assertEqual(budget.path_length("a" * 198 + "😀b", "windows"), 201)
        self.assertEqual(budget.path_length("中文 空格", "windows"), 5)
        self.assertEqual(budget.path_length("😀", "posix"), len(os.fsencode("😀")))

    def test_classification(self):
        samples = {
            "CMakeFiles/qcs.dir/file.cpp.obj": "object",
            "CMakeFiles/qcs.dir/file.cpp.o": "object",
            "qcs_autogen/CMakeFiles/qcs.dir/qrc_resources.cpp.obj": "object",
            "qcs_autogen/CMakeFiles/qcs.dir/qrc_resources.cpp.o": "object",
            "qcs_autogen/mocs_compilation.cpp": "moc",
            "generated/qrc_qcs.cpp": "rcc",
            "CMakeFiles/qcs.rsp": "response",
            "sb/tesseract/qcs_tess-populate-stamp/update.txt": "fetchcontent",
            "qcs.exe": "build",
        }
        for path, expected in samples.items():
            with self.subTest(path=path):
                self.assertEqual(budget.classify(Path(path)), expected)

    def test_ninja_decoded_paths_preserve_spaces_and_colons(self):
        parsed = budget.parse_ninja_targets(
            "folder with spaces/文件.cpp.obj: compile\nphony name: phony\n", self.root)
        self.assertEqual(parsed, [budget.NinjaTarget("folder with spaces/文件.cpp.obj",
                                                   (self.root / "folder with spaces/文件.cpp.obj").resolve())])
        with self.assertRaisesRegex(RuntimeError, "Unexpected Ninja target"):
            budget.parse_ninja_targets("not a valid target listing", self.root)

    def test_real_ninja_preserves_absolute_and_relative_output_spellings(self):
        build = self.graph()
        # CMake's C++ dyndep graph emits absolute CXXModules.json outputs among
        # otherwise relative targets. Ninja does not substitute one spelling
        # for the other, even when both refer to the same filesystem location.
        absolute = (build / "CMakeFiles/绝对 目标/CXXModules.json").as_posix()
        relative = "CMakeFiles/相对 目标/CXXModules.json"
        shared_relative = "same-output.json"
        shared_absolute = (build / shared_relative).as_posix()
        def escape(value):
            return value.replace("$", "$$").replace(" ", "$ ").replace(":", "$:")
        with (build / "build.ninja").open("a", encoding="utf-8") as stream:
            stream.write("rule spelling\n  command = unused-generator @\"$response\"\n")
            for name, response in ((absolute, "absolute.rsp"), (relative, "relative.rsp"),
                                   (shared_relative, "same-relative.rsp"), (shared_absolute, "same-absolute.rsp")):
                stream.write(f"build {escape(name)}: spelling\n  response = {response}\n")
            stream.write("default all\n")
        targets, _ = budget.ninja_outputs(build, self.ninja())
        self.assertIn(absolute, [target.name for target in targets])
        self.assertIn(relative, [target.name for target in targets])
        same = [target for target in targets if target.path == (build / shared_relative).resolve()]
        self.assertEqual({target.name for target in same}, {shared_relative, shared_absolute})
        report = budget.inspect_paths(self.args(build=build, ninja=self.ninja()))
        self.assertEqual(report["errors"], [])
        self.assertTrue(report["configured"])
        self.assertEqual(report["passed"], not report["offenders"])
        responses = {row["path"] for row in report["paths"] if row["category"] == "response"}
        self.assertEqual(responses, {str((build / name).resolve()) for name in
                                   ("absolute.rsp", "relative.rsp", "same-relative.rsp", "same-absolute.rsp")})
        self.assertFalse(Path(absolute).exists(), "Inspection must not execute a build command")

    def test_response_arguments_preserve_shell_quoting(self):
        command = 'tool @plain.rsp @"folder with spaces/中文.rsp" "@whole quoted.rsp"'
        expected = ["plain.rsp", "folder with spaces/中文.rsp", "whole quoted.rsp"]
        self.assertEqual(budget.response_arguments(command, "windows"), expected)
        self.assertEqual(budget.response_arguments(command, "posix"), expected)
        self.assertEqual(budget.response_arguments("tool @folder\\ with\\ spaces/file.rsp", "posix"),
                         ["folder with spaces/file.rsp"])

    def test_unmaterialized_rsp_from_subninja_is_checked(self):
        build = self.graph()
        response = "generated/" + "/".join(["r" * 65] * 4) + ".rsp"
        with (build / "build.ninja").open("a", encoding="utf-8") as stream:
            stream.write("subninja child.ninja\ndefault all\n")
        (build / "child.ninja").write_text(
            "rule response\n  command = unused-compiler @\"$rsp\"\n"
            "  rspfile = $rsp\n  rspfile_content = input.cpp\n"
            "build response.out: response\n  rsp = " + response + "\n", encoding="utf-8")
        report = budget.inspect_paths(self.args(build=build, ninja=self.ninja()))
        self.assertEqual(report["errors"], [])
        self.assertEqual(report["status"], "failed")
        offenders = [row for row in report["offenders"] if row["category"] == "response"]
        self.assertEqual(len(offenders), 1)
        self.assertEqual(offenders[0]["origins"], ["ninja-command"])
        self.assertFalse((build / response).exists())

    def test_unmaterialized_per_header_and_included_moc_outputs_are_checked(self):
        build = self.graph()
        metadata = build / "CMakeFiles/qcs_autogen.dir/AutogenInfo.json"
        metadata.parent.mkdir(parents=True)
        source = self.root / "tests.cpp"
        source.write_text('#include "tests.moc"\n', encoding="utf-8")
        autogen = build / "qcs_autogen"
        moc = "/".join(["m" * 65] * 4) + "/moc_header.cpp"
        metadata.write_text(json.dumps({
            "BUILD_DIR": str(autogen), "INCLUDE_DIR": str(autogen / "include"),
            "MULTI_CONFIG": False, "HEADERS": [["header.h", "MU", moc, None]],
            "SOURCES": [[str(source), "MU", None]],
            "MOC_COMPILATION_FILE": str(autogen / "mocs_compilation.cpp"),
        }), encoding="utf-8")
        report = budget.inspect_paths(self.args(build=build, ninja=self.ninja()))
        self.assertEqual(report["errors"], [])
        self.assertEqual(report["status"], "failed")
        rows = {row["path"]: row for row in report["paths"]}
        self.assertIn("cmake-autogen", rows[str((autogen / moc).resolve())]["origins"])
        self.assertIn(str((autogen / "include/tests.moc").resolve()), rows)
        self.assertFalse((autogen / moc).exists())

    def test_real_ninja_graph_and_generated_stamp(self):
        build = self.graph()
        stamp = build / "sb/tess/qcs_tess-populate-prefix/src/qcs_tess-populate-stamp/update-info.txt"
        stamp.parent.mkdir(parents=True)
        stamp.write_text("test stamp", encoding="utf-8")
        response = build / "CMakeFiles/qcs.rsp"
        response.parent.mkdir(parents=True)
        response.write_text("object list", encoding="utf-8")
        report = budget.inspect_paths(self.args(build=build, ninja=self.ninja()))
        self.assertEqual(report["errors"], [])
        self.assertTrue(report["configured"])
        # A custom test root may itself exceed the ordinary budgets. Inspection remains real.
        self.assertEqual(report["passed"], not report["offenders"])
        paths = {row["path"]: row for row in report["paths"]}
        self.assertEqual(paths[str(stamp.resolve())]["category"], "fetchcontent")
        obj = paths[str((build / "CMakeFiles/qcs.dir/file.cpp.obj").resolve())]
        self.assertEqual(obj["origins"], ["ninja"])
        self.assertFalse((build / "qcs.exe").exists(), "Inspection must not build the graph")
        self.assertFalse(any(row["path"].endswith(os.sep + "all") for row in paths.values()))
        self.assertEqual(report["summary"]["byCategory"]["object"], 1)

    def test_source_cache_and_runtime_trees_are_not_scanned(self):
        build = self.graph()
        for relative in ("nd/tess-src/input.cpp", "deps/raw/input.cpp", "models/model.gguf",
                         "runs/one/profile/data.sqlite"):
            path = build / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("not generated compiler data", encoding="utf-8")
        report = budget.inspect_paths(self.args(build=build, ninja=self.ninja()))
        inspected = {row["path"] for row in report["paths"]}
        self.assertNotIn(str((build / "nd/tess-src/input.cpp").resolve()), inspected)
        self.assertNotIn(str((build / "runs/one/profile/data.sqlite").resolve()), inspected)
        self.assertNotIn(str((build / "models/model.gguf").resolve()), inspected)

    def test_doctor_does_not_claim_configured_pass(self):
        report = budget.inspect_paths(self.args())
        self.assertEqual(report["status"], "not-configured")
        self.assertIsNone(report["passed"])
        self.assertFalse(report["configured"])

    def test_missing_build_and_ninja_failure_are_not_passes(self):
        report = budget.inspect_paths(self.args(build=self.root / "missing"))
        self.assertEqual(report["status"], "unavailable")
        self.assertFalse(report["passed"])
        build = self.graph()
        (build / "build.ninja").write_text("invalid syntax", encoding="utf-8")
        report = budget.inspect_paths(self.args(build=build, ninja=self.ninja()))
        self.assertEqual(report["status"], "unavailable")
        self.assertIn("Ninja target inspection failed", report["errors"][0])

    def test_overlong_root_fails_without_creating_it(self):
        long_root = self.root / ("a" * 90) / ("b" * 90) / ("c" * 90)
        report = budget.inspect_paths(self.args(root=long_root))
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["offenders"][0]["category"], "root")
        self.assertFalse(long_root.exists())

    def test_object_limit_is_stricter_than_other_paths(self):
        prefix = str(self.root / "output") + os.sep
        desired = max(201, budget.path_length(prefix, "windows") + 8)
        obj = Path(prefix + "a" * (desired - budget.path_length(prefix, "windows") - 4) + ".obj")
        other = obj.with_suffix(".txt")
        with patch.object(budget, "ninja_outputs", return_value=(
                [budget.NinjaTarget(str(path), path) for path in (obj, other)], "fixture")), \
                patch.object(budget, "ninja_responses", return_value=[]), \
                patch.object(budget, "scan_generated", return_value=[]):
            report = budget.inspect_paths(self.args(build=self.root))
        obj_row = next(row for row in report["paths"] if row["category"] == "object")
        self.assertEqual(obj_row["length"], desired)
        self.assertIn(obj_row, report["offenders"])
        if desired <= 240:
            self.assertEqual(len(report["offenders"]), 1)

    def test_object_budgets_match_platform_boundaries(self):
        # Pure path data: no filesystem operation depends on this short root,
        # including when the actual isolated test directory is itself very long.
        base = Path(self.root.anchor) / "qcs-budget"
        prefix = str(base / "obj") + os.sep
        for platform, limit in (("windows", 200), ("posix", 240)):
            for length in (200, 201, 220, 240, 241):
                # An RCC-generated source compiled to an object still uses the
                # object budget. Its qrc_ prefix must not select the RCC budget.
                for stem, suffix in (("", ".obj"), ("qrc_", ".cpp.obj"), ("qrc_", ".cpp.o")):
                    with self.subTest(platform=platform, length=length, stem=stem, suffix=suffix):
                        count = length - budget.path_length(prefix, platform) - len(stem + suffix)
                        obj = Path(prefix + stem + "x" * count + suffix)
                        with patch.object(budget, "ninja_outputs", return_value=(
                                [budget.NinjaTarget(str(obj), obj)], "fixture")), \
                                patch.object(budget, "ninja_responses", return_value=[]), \
                                patch.object(budget, "scan_generated", return_value=[]):
                            report = budget.inspect_paths(self.args(root=base, build=base, platform=platform))
                        self.assertEqual(report["errors"], [])
                        self.assertEqual(report["paths"][0]["category"], "object")
                        self.assertEqual(report["budgets"]["object"], limit)
                        self.assertEqual(report["paths"][0]["budget"], limit)
                        self.assertEqual(report["paths"][0]["length"], length)
                        self.assertEqual(report["passed"], length <= limit)

    def test_stress_is_observation_never_normal_pass(self):
        long_root = self.root / ("a" * 90) / ("b" * 90) / ("c" * 90)
        report = budget.inspect_paths(self.args(root=long_root, allow_stress_root=True))
        self.assertEqual(report["status"], "stress-observation")
        self.assertIsNone(report["passed"])
        self.assertTrue(report["offenders"])

    def test_stage_scan_keeps_installed_files_and_records_unmaterialized_outputs(self):
        stage = self.root / "stage"
        license_path = stage / "share/qcs/deps/notice.txt"
        license_path.parent.mkdir(parents=True)
        license_path.write_text("installed license", encoding="utf-8")
        report = budget.inspect_paths(self.args(stage=stage))
        self.assertIn(str(license_path.resolve()), [row["path"] for row in report["paths"]])
        self.assertIsNone(report["passed"])
        report = budget.inspect_paths(self.args(stage=stage / "missing"))
        self.assertEqual(report["status"], "not-configured")
        self.assertEqual(report["outputs"]["stage"]["status"], "not-materialized")
        self.assertIsNone(report["passed"])
        build = self.graph()
        report = budget.inspect_paths(self.args(build=build, ninja=self.ninja(),
                                              stage=stage / "missing", package_root=self.root / "pkg"))
        self.assertTrue(report["configured"])
        self.assertEqual(report["errors"], [])
        self.assertEqual(report["outputs"]["package_root"]["status"], "not-materialized")

    def test_cli_from_unrelated_cwd_and_report_outside_source(self):
        build = self.graph()
        source = self.root / "源码"
        source.mkdir()
        output = self.root / "evidence/report.json"
        environment = dict(os.environ, PYTHONIOENCODING="cp1252")
        process = subprocess.run(
            [sys.executable, "-B", str(SCRIPT), "--source", str(source), "--build", str(build),
             "--ninja", self.ninja(), "--report", str(output), "--platform", "windows"],
            cwd=source, env=environment, capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertIn(process.returncode, (0, 1), process.stderr)
        report = json.loads(output.read_text(encoding="utf-8"))
        self.assertTrue(report["configured"])
        self.assertEqual(list(source.iterdir()), [])
        invalid = subprocess.run(
            [sys.executable, "-B", str(SCRIPT), "--source", str(source), "--root", str(self.root),
             "--report", str(source / "bad.json")], cwd=self.root,
            capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(invalid.returncode, 2)
        self.assertIn("outside the source tree", invalid.stderr)
        self.assertFalse((source / "bad.json").exists())

    def test_cli_exit_codes_for_missing_build_and_overlong_root(self):
        for arguments, expected in (
            (["--build", str(self.root / "missing")], 2),
            (["--root", str(self.root / ("a" * 90) / ("b" * 90) / ("c" * 90))], 1),
        ):
            with self.subTest(expected=expected):
                output = self.root / f"result-{expected}.json"
                process = subprocess.run(
                    [sys.executable, "-B", str(SCRIPT), *arguments, "--report", str(output)],
                    cwd=self.root, capture_output=True, text=True, encoding="utf-8", timeout=30)
                self.assertEqual(process.returncode, expected, process.stderr)
                self.assertIsNotNone(json.loads(output.read_text(encoding="utf-8")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
