#!/usr/bin/env python3
"""Verify fallback selection, archive identity and the real offline installer."""
from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import uuid
import zipfile

sys.dont_write_bytecode = True
SOURCE = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("qcs_windows_package", SOURCE / "tools/qcs-package-windows.py")
package = importlib.util.module_from_spec(spec)
spec.loader.exec_module(package)


class PayloadFixture(unittest.TestCase):
    def setUp(self):
        # Keep unittest's verbose case/result line contiguous for the strict
        # evidence parser, including output from mocked packaging commands.
        self.captured_stdout, self.captured_stderr = io.StringIO(), io.StringIO()
        capture = contextlib.ExitStack()
        capture.enter_context(contextlib.redirect_stdout(self.captured_stdout))
        capture.enter_context(contextlib.redirect_stderr(self.captured_stderr))
        self.addCleanup(capture.close)
        root = os.environ.get("QCOPILOTS_SEARCH_TEST_ROOT")
        if root:
            Path(root).mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="qcs-nsis-payload-", dir=root)
        self.addCleanup(self.temporary.cleanup)
        self.work = Path(self.temporary.name)
        self.payload = self.work / "payload"
        (self.payload / "bin").mkdir(parents=True)
        (self.payload / "bin/qcs-gui.exe").write_bytes(bytes(range(256)) * 300)
        (self.payload / "资料").mkdir()
        (self.payload / "资料/a $1.txt").write_text("离线安装 中文 and spaces", encoding="utf-8")
        self.manifest = package.payload_manifest(self.payload, package.ROCM_VARIANT, "0.1.0")


class PayloadTests(PayloadFixture):
    def test_manifest_binds_every_byte_and_unicode_path(self):
        self.assertEqual(len(self.manifest["files"]), 2)
        for item in self.manifest["files"]:
            data = (self.payload / item["path"]).read_bytes()
            self.assertEqual((len(data), hashlib.sha256(data).hexdigest()), (item["bytes"], item["sha256"]))
        self.assertEqual(self.manifest["totalBytes"], sum(entry["bytes"] for entry in self.manifest["files"]))

    def test_rejects_paths_with_windows_ambiguity(self):
        for value in ("../x", "/x", "bin//x", "bin/./x", "x.", "x ", "C:/x", "bin\\x",
                      "bin/x:stream", "bin/*", "bin/NUL.txt", "CON", "Uninstall.exe"):
            with self.subTest(path=value), self.assertRaises(RuntimeError):
                package.safe_relative(value)

    def test_large_payload_and_specific_compiler_error_are_both_required(self):
        huge = {"files": [{"path": "bin/large.dll", "bytes": package.NSIS_LIMIT + 1, "sha256": "a" * 64}]}
        overflow = "Internal compiler error #12345: error mmapping file (2147483640, 32) is out of range."
        self.assertTrue(package.nsis_size_failure(overflow, huge))
        self.assertFalse(package.nsis_size_failure(overflow, self.manifest))
        self.assertFalse(package.nsis_size_failure("Error: can't write to output", huge))
        self.assertFalse(package.nsis_size_failure("Error: unknown symbol", huge))
        self.assertFalse(package.nsis_size_failure("deflateInit() failed", huge))
        self.assertFalse(package.nsis_size_failure("error creating mmap the size of 2147483647", huge))
        self.assertFalse(package.nsis_size_failure("error mmapping datablock to 33554432", huge))
        duplicated = {"files": [{"path": f"bin/{index}.dll", "bytes": package.NSIS_LIMIT // 2,
                                  "sha256": "b" * 64} for index in range(3)]}
        self.assertFalse(package.nsis_size_failure(overflow, duplicated))

    def test_setup_archive_streams_exact_manifest_and_rejects_mutated_stage(self):
        def compile_script(command, check):
            (Path(command[-1]).parent / "setup.exe").write_bytes(b"compiler result fixture")
        output = self.work / "qcs-0.1.0-windows-x64-rocm-setup.zip"
        with mock.patch.object(package.subprocess, "run", side_effect=compile_script):
            package.create_setup_zip(self.payload, self.manifest, output, Path("makensis.exe"))
        with zipfile.ZipFile(output) as archive:
            self.assertEqual(set(archive.namelist()), {"setup.exe", "payload-manifest.json",
                             *("payload/" + item["path"] for item in self.manifest["files"])})
            self.assertEqual(json.loads(archive.read("payload-manifest.json")), self.manifest)
            for item in self.manifest["files"]:
                self.assertEqual(hashlib.sha256(archive.read("payload/" + item["path"])).hexdigest(), item["sha256"])
        original = output.read_bytes()
        (self.payload / "bin/qcs-gui.exe").write_bytes(b"changed")
        with mock.patch.object(package.subprocess, "run", side_effect=compile_script), self.assertRaisesRegex(RuntimeError, "changed"):
            package.create_setup_zip(self.payload, self.manifest, output, Path("makensis.exe"))
        self.assertEqual(output.read_bytes(), original)

    def test_package_moves_only_finished_installer_and_removes_its_stage(self):
        output = self.work / "packages"
        historical = output / "_CPack_Packages/historical/payload.dat"
        historical.parent.mkdir(parents=True)
        historical.write_bytes(b"keep historical output")
        other = output / "qcs-other-candidate.exe"
        other.write_bytes(b"keep other candidate")
        name = "qcs-0.1.0-windows-x64-rocm-candidate"
        for suffix, previous in ((".exe", "-setup.zip"), ("-setup.zip", ".exe")):
            with self.subTest(suffix=suffix):
                (output / (name + previous)).write_bytes(b"old alternate")

                def produce(build, owned, config, variant, compiler):
                    self.assertEqual(owned.parent, output)
                    stage = owned / "_CPack_Packages/win64/NSIS/payload.dat"
                    stage.parent.mkdir(parents=True)
                    stage.write_bytes(b"temporary full payload")
                    stage.chmod(stat.S_IREAD)
                    result = owned / (name + suffix)
                    result.write_bytes(b"finished package")
                    return result

                with mock.patch.object(package, "_package_in_directory", side_effect=produce):
                    result = package.package(self.work / "build", output, "Release", package.ROCM_VARIANT)
                self.assertEqual(result, output / (name + suffix))
                self.assertEqual(result.read_bytes(), b"finished package")
                self.assertFalse((output / (name + previous)).exists())
                self.assertEqual(list(output.glob("qcs-nsis-*")), [])
                self.assertEqual(historical.read_bytes(), b"keep historical output")
                self.assertEqual(other.read_bytes(), b"keep other candidate")

    def test_package_failure_cleans_owned_stage_and_preserves_previous_package(self):
        output = self.work / "packages"
        output.mkdir()
        previous = output / "qcs-0.1.0-windows-x64-rocm-candidate.exe"
        previous.write_bytes(b"previous successful package")

        def fail(build, owned, config, variant, compiler):
            stage = owned / "_CPack_Packages/win64/NSIS/payload.dat"
            stage.parent.mkdir(parents=True)
            stage.write_bytes(b"failed temporary payload")
            raise RuntimeError("fixture compiler failure")

        with mock.patch.object(package, "_package_in_directory", side_effect=fail), self.assertRaisesRegex(RuntimeError, "compiler failure"):
            package.package(self.work / "build", output, "Release", package.ROCM_VARIANT)
        self.assertEqual(list(output.iterdir()), [previous])
        self.assertEqual(previous.read_bytes(), b"previous successful package")


@unittest.skipUnless(os.name == "nt", "The native NSIS installer runs on Windows")
class NativeInstallerTests(PayloadFixture):
    def setUp(self):
        super().setUp()
        compiler = os.environ.get("QCOPILOTS_SEARCH_NSIS_EXECUTABLE") or shutil.which("makensis")
        if not compiler:
            self.fail("Native installer tests require the bootstrapped NSIS compiler on PATH or QCOPILOTS_SEARCH_NSIS_EXECUTABLE")
        self.identity = "qcs-fallback-test-" + uuid.uuid4().hex
        self.destination = self.work / "installed-application"
        self.executable = self.work / "setup.exe"
        self.script = self.work / "setup.nsi"
        self.script.write_text(package.render_script(self.manifest, self.executable, SOURCE / "LICENSE",
                                                    product_id=self.identity, menu=self.identity), encoding="utf-8-sig")
        self.run_child([compiler, "/V2", str(self.script)])
        # Test-only registration and menu names cannot collide with a user install.
        self.addCleanup(self.remove_test_installation)

    def run_child(self, command, *, expect_success=True):
        result = subprocess.run(command, timeout=60, capture_output=True, text=True,
                                encoding="utf-8", errors="replace")
        self.child_output = f"Command: {command}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        if expect_success:
            self.assertEqual(result.returncode, 0, self.child_output)
        return result.returncode

    def remove_test_installation(self):
        uninstaller = self.destination / "Uninstall.exe"
        if uninstaller.is_file():
            command = subprocess.list2cmdline([str(uninstaller), "/S"]) + " _?=" + str(self.destination)
            self.run_child(command)
        import winreg
        for key in ("Software\\QCopilots\\" + self.identity,
                    "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\" + self.identity):
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY):
                    self.fail("Installer cleanup retained its isolated test registration")
            except FileNotFoundError:
                pass
        menu = Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs" / self.identity
        self.assertFalse(menu.exists(), "Installer cleanup retained its isolated Start Menu directory")

    def install(self, destination=None):
        command = subprocess.list2cmdline([str(self.executable), "/S"]) + " /D=" + str(destination or self.destination)
        return self.run_child(command, expect_success=False)

    def test_install_uninstall_preserves_unowned_file(self):
        self.assertEqual(self.install(), 0, self.child_output)
        for item in self.manifest["files"]:
            self.assertEqual(package.digest(self.destination / item["path"]), item["sha256"])
        sentinel = self.destination / "my-user-file.txt"
        sentinel.write_text("owned by user", encoding="utf-8")
        self.remove_test_installation()
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "owned by user")
        for item in self.manifest["files"]:
            self.assertFalse((self.destination / item["path"]).exists())

    def verify_installer_icons(self):
        self.assertEqual(self.install(), 0, self.child_output)
        self.run_child([sys.executable, "-B", str(SOURCE / "tests/runtime/app_icons_test.py"),
                        "--resources", str(SOURCE / "resources/icons"),
                        "--executable", str(self.executable),
                        "--executable", str(self.destination / "Uninstall.exe")])

    def test_installer_and_uninstaller_embed_product_icon(self):
        self.verify_installer_icons()

    def test_standard_installer_and_uninstaller_embed_product_icon(self):
        # Exercise the complete CPack template with a small, isolated payload.
        # Its real user-facing registry and menu names must never be used here.
        template = (SOURCE / "cmake/nsis/NSIS.template.in").read_text(encoding="utf-8")
        template = template.replace("\\qcopilots-search\"", "\\" + self.identity + '"')
        template = template.replace("$SMPROGRAMS\\QCopilots Search", "$SMPROGRAMS\\" + self.identity)
        values = {
            "CPACK_TEMPORARY_DIRECTORY": str(self.payload),
            "CPACK_TOPLEVEL_DIRECTORY": str(self.work),
            "CPACK_OUTPUT_FILE_NAME": self.executable.name,
            "CPACK_RESOURCE_FILE_LICENSE": str(SOURCE / "LICENSE"),
            "CPACK_NSIS_MUI_ICON": str(SOURCE / "resources/icons/qcs.ico"),
            "CPACK_NSIS_MUI_UNIICON": str(SOURCE / "resources/icons/qcs.ico"),
            "CPACK_PACKAGE_VERSION": self.manifest["version"],
            "CPACK_NSIS_FULL_INSTALL": 'File /r "${INST_DIR}\\*"',
            "CPACK_NSIS_DELETE_FILES": '\n'.join(
                'Delete "$INSTDIR\\' + package.nsis_literal(item["path"].replace("/", "\\")) + '"'
                for item in self.manifest["files"]),
            "CPACK_NSIS_DELETE_DIRECTORIES": 'RMDir "$INSTDIR\\bin"\nRMDir "$INSTDIR\\资料"',
        }
        for key, value in values.items():
            template = template.replace("@" + key + "@", value)
        self.assertNotIn("@CPACK_", template)
        self.script.write_text(template, encoding="utf-8-sig")
        compiler = os.environ.get("QCOPILOTS_SEARCH_NSIS_EXECUTABLE") or shutil.which("makensis")
        self.run_child([compiler, "/V2", str(self.script)])
        self.verify_installer_icons()

    def test_same_length_corruption_fails_before_installing(self):
        path = self.payload / "bin/qcs-gui.exe"
        path.write_bytes(b"X" * path.stat().st_size)
        self.assertNotEqual(self.install(), 0, self.child_output)
        self.assertFalse(self.destination.exists())

    def test_missing_payload_fails_before_installing(self):
        (self.payload / "资料/a $1.txt").unlink()
        self.assertNotEqual(self.install(), 0, self.child_output)
        self.assertFalse(self.destination.exists())

    def test_overlapping_payload_destination_is_rejected(self):
        self.assertNotEqual(self.install(self.payload), 0, self.child_output)
        # GetShortPathName returns the original spelling when 8.3 generation
        # is disabled. Either result must refer to the same rejected source.
        import ctypes
        buffer = ctypes.create_unicode_buffer(32768)
        count = ctypes.windll.kernel32.GetShortPathNameW(str(self.payload), buffer, len(buffer))
        self.assertGreater(count, 0)
        self.assertLess(count, len(buffer))
        self.assertNotEqual(self.install(Path(buffer.value)), 0, self.child_output)
        for item in self.manifest["files"]:
            self.assertEqual(package.digest(self.payload / item["path"]), item["sha256"])


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    unittest.main()
