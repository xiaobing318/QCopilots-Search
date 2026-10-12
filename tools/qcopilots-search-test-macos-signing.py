#!/usr/bin/env python3
"""Portable signing-contract tests. Real Apple codesign is exercised by native CI.

The runner below records commands without invoking a signing tool. Real symbolic
link cases explicitly report unavailable Windows privileges as skipped coverage.
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import plistlib
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
SCRIPT = Path(__file__).resolve().with_name("qcopilots-search-sign-macos.py")
SPEC = importlib.util.spec_from_file_location("qcopilots_search_sign_macos", SCRIPT)
signing = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = signing
SPEC.loader.exec_module(signing)

EXECUTE, DYLIB, BUNDLE = 2, 6, 8
EXPECTED_ERROR = (ValueError, RuntimeError)


def thin(file_type=DYLIB, *, bits=64, endian="<", cpu=0x01000007):
    """An actual Mach-O header with no load commands, sufficient for inventory."""
    magic = 0xFEEDFACF if bits == 64 else 0xFEEDFACE
    fields = [magic, cpu, 3, file_type, 0, 0, 0]
    if bits == 64:
        fields.append(0)
    return struct.pack(endian + "I" * len(fields), *fields)


def fat(file_types=(DYLIB, DYLIB), *, bits=32, endian=">"):
    """A fat container whose architecture table references two real headers."""
    magic = 0xCAFEBABF if bits == 64 else 0xCAFEBABE
    payload = bytearray(256 * (len(file_types) + 1))
    struct.pack_into(endian + "II", payload, 0, magic, len(file_types))
    entry_format = endian + ("IIQQII" if bits == 64 else "IIIII")
    entry_size = struct.calcsize(entry_format)
    for index, file_type in enumerate(file_types):
        cpu = 0x01000007 if index == 0 else 0x0100000C
        offset = 256 * (index + 1)
        architecture = thin(file_type, cpu=cpu)
        values = [cpu, 3, offset, len(architecture), 8]
        if bits == 64:
            values.append(0)
        struct.pack_into(entry_format, payload, 8 + index * entry_size, *values)
        payload[offset:offset + len(architecture)] = architecture
    return bytes(payload)


def java_class(major):
    """A minimal class with a legal UTF8 first constant and class references."""
    class_name = b"QCopilotsSearch"
    superclass = b"java/lang/Object"
    constants = (b"\x01" + struct.pack(">H", len(class_name)) + class_name
                 + b"\x07\x00\x01"
                 + b"\x01" + struct.pack(">H", len(superclass)) + superclass
                 + b"\x07\x00\x03")
    return (struct.pack(">IHHH", 0xCAFEBABE, 0, major, 5) + constants
            + struct.pack(">HHHHHHH", 0x0021, 2, 4, 0, 0, 0, 0))


class RecordingRunner:
    def __init__(self, fail=None):
        self.commands = []
        self.fail = fail

    def __call__(self, command, **kwargs):
        command = [str(argument) for argument in command]
        self.commands.append(command)
        failed = bool(self.fail and self.fail(command))
        return subprocess.CompletedProcess(command, 19 if failed else 0,
                                           "", "injected codesign failure" if failed else "")


class SigningTests(unittest.TestCase):
    def setUp(self):
        configured = os.environ.get("QCOPILOTS_SEARCH_TEST_ROOT")
        if configured:
            Path(configured).mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="qcopilots-search-signing-", dir=configured)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.app = self.root / "应用 空格" / "qcopilots-search.app"
        self.create_bundle(self.app, "qcopilots-search")

    def write(self, relative, content):
        path = self.app / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    @staticmethod
    def create_bundle(path, executable):
        (path / "Contents/MacOS").mkdir(parents=True, exist_ok=True)
        (path / "Contents/MacOS" / executable).write_bytes(thin(EXECUTE))
        (path / "Contents/Info.plist").write_bytes(plistlib.dumps({
            "CFBundleExecutable": executable,
            "CFBundleIdentifier": "org.qcopilots.search." + executable,
            "CFBundlePackageType": "APPL",
        }))

    def framework(self):
        relative = "Contents/Frameworks/QtCore.framework"
        self.write(relative + "/Versions/A/QtCore", thin(DYLIB))
        self.write(relative + "/Versions/A/Resources/Info.plist", plistlib.dumps({
            "CFBundleExecutable": "QtCore", "CFBundlePackageType": "FMWK",
            "CFBundleIdentifier": "org.qt-project.QtCore",
        }))
        return relative

    def symlink(self, link, target, *, directory=False):
        link.parent.mkdir(parents=True, exist_ok=True)
        try:
            link.symlink_to(target, target_is_directory=directory)
        except OSError as error:
            if os.name == "nt" and error.winerror == 1314:
                self.skipTest("Windows lacks symbolic-link creation privilege (WinError 1314). Native CI must run this case.")
            raise

    def inventory_fixture(self):
        self.write("Contents/Frameworks/libtesseract.5.5.3.dylib", thin(DYLIB))
        self.write("Contents/Frameworks/libtesseract.5.5.dylib", thin(DYLIB))
        self.write("Contents/PlugIns/ggml/libggml-cpu-x64.so", thin(BUNDLE))
        self.write("Contents/MacOS/qcopilots-search-worker", thin(EXECUTE))
        self.write("Contents/Resources/readme.txt", b"documentation, not executable code")
        self.write("Contents/Resources/settings.json", b"{}")
        self.framework()

    @staticmethod
    def is_sign(command):
        return "--sign" in command or "-s" in command

    @staticmethod
    def target(command, bundle):
        for argument in reversed(command):
            path = Path(argument)
            if path.is_absolute() and path == bundle:
                return "."
            if path.is_absolute() and bundle in path.parents:
                return path.relative_to(bundle).as_posix()
        raise AssertionError(f"No bundle-contained target in command: {command}")

    def test_thin_32_64_headers_in_both_byte_orders(self):
        for bits in (32, 64):
            for endian in ("<", ">"):
                for file_type in (EXECUTE, DYLIB, BUNDLE):
                    with self.subTest(bits=bits, endian=endian, file_type=file_type):
                        path = self.write("probe", thin(file_type, bits=bits, endian=endian))
                        self.assertEqual(signing.macho_file_type(path), file_type)

    def test_fat_32_64_headers_in_both_byte_orders(self):
        for bits in (32, 64):
            for endian in ("<", ">"):
                with self.subTest(bits=bits, endian=endian):
                    path = self.write("probe", fat(bits=bits, endian=endian))
                    self.assertEqual(signing.macho_file_type(path), DYLIB)

    def test_non_code_and_java_class_are_not_macho(self):
        for data in (b"", b"ordinary text", b"\x00\x01\x02", java_class(65)):
            with self.subTest(data=data):
                path = self.write("probe", data)
                self.assertIsNone(signing.macho_file_type(path))

    def test_java_8_class_with_utf8_constant_pool_is_not_a_fat_binary(self):
        # Java major 52 appears as nfat_arch=52 when CAFEBABE is interpreted as
        # a fat header. Its valid first UTF8 constant must disambiguate it.
        path = self.write("Contents/Resources/QCopilotsSearch.class", java_class(52))
        self.assertIsNone(signing.macho_file_type(path))
        inventory = signing.plan_bundle(self.app)
        self.assertNotIn(path.relative_to(self.app).as_posix(), inventory["machOFiles"])

    def test_inventory_architectures_come_from_thin_headers_and_fat_tables(self):
        expected = {"Contents/MacOS/qcopilots-search": ["x86_64"]}
        for endian, suffix in (("<", "le"), (">", "be")):
            # Misleading names establish that CPU types come from bytes.
            intel = f"Contents/Frameworks/pretends-arm64-{suffix}.dylib"
            arm = f"Contents/Frameworks/pretends-x86_64-{suffix}.dylib"
            self.write(intel, thin(endian=endian, cpu=0x01000007))
            self.write(arm, thin(endian=endian, cpu=0x0100000C))
            expected[intel] = ["x86_64"]
            expected[arm] = ["arm64"]
            for bits in (32, 64):
                universal = f"Contents/Frameworks/no-architecture-name-{bits}-{suffix}.bin"
                self.write(universal, fat(bits=bits, endian=endian))
                expected[universal] = ["x86_64", "arm64"]
        inventory = signing.plan_bundle(self.app)
        self.assertEqual(inventory["architectures"], expected)
        self.assertEqual(set(inventory["architectures"]), set(inventory["machOFiles"]))
        report = signing.sign_bundle(self.app, runner=RecordingRunner())
        self.assertEqual(report["inventory"]["architectures"], expected)

    def test_truncated_thin_headers_are_rejected(self):
        for length in (4, 12, 20, 31):
            with self.subTest(length=length):
                path = self.write("probe", thin()[:length])
                with self.assertRaises(ValueError):
                    signing.macho_file_type(path)

    def test_truncated_fat_table_and_architecture_are_rejected(self):
        for data in (fat()[:4], fat()[:12], fat()[:36], fat()[:270]):
            with self.subTest(length=len(data)):
                path = self.write("probe", data)
                with self.assertRaises(ValueError):
                    signing.macho_file_type(path)

    def test_fat_mixed_file_types_are_rejected(self):
        path = self.write("probe", fat((EXECUTE, DYLIB)))
        with self.assertRaises(ValueError):
            signing.macho_file_type(path)

    def test_fat_invalid_slice_magic_is_rejected(self):
        data = bytearray(fat())
        data[256:260] = b"NOPE"
        path = self.write("probe", data)
        with self.assertRaises(ValueError):
            signing.macho_file_type(path)

    def test_fat_out_of_bounds_and_overlapping_slices_are_rejected(self):
        invalid = []
        outside = bytearray(fat())
        struct.pack_into(">I", outside, 16, len(outside) + 256)
        invalid.append(outside)
        overlapping = bytearray(fat())
        struct.pack_into(">I", overlapping, 36, 256)
        invalid.append(overlapping)
        empty = bytearray(fat())
        struct.pack_into(">I", empty, 4, 0)
        invalid.append(empty)
        for index, data in enumerate(invalid):
            with self.subTest(case=index):
                path = self.write("probe", data)
                with self.assertRaises(ValueError):
                    signing.macho_file_type(path)

    def test_non_java_fat_with_65_architectures_is_rejected(self):
        for bits in (32, 64):
            for endian in ("<", ">"):
                with self.subTest(bits=bits, endian=endian):
                    data = bytearray(fat(bits=bits, endian=endian))
                    struct.pack_into(endian + "I", data, 4, 65)
                    path = self.write("Contents/Frameworks/libexcessive.dylib", data)
                    with self.assertRaises(ValueError):
                        signing.macho_file_type(path)
                    with self.assertRaises(ValueError):
                        signing.plan_bundle(self.app)

    def test_inventory_includes_unreferenced_versioned_library_and_plugin(self):
        self.inventory_fixture()
        plan = signing.plan_bundle(self.app)
        self.assertEqual(Path(plan["bundle"]), self.app)
        expected = {
            "Contents/MacOS/qcopilots-search",
            "Contents/MacOS/qcopilots-search-worker",
            "Contents/Frameworks/libtesseract.5.5.3.dylib",
            "Contents/Frameworks/libtesseract.5.5.dylib",
            "Contents/PlugIns/ggml/libggml-cpu-x64.so",
            "Contents/Frameworks/QtCore.framework/Versions/A/QtCore",
        }
        self.assertEqual(set(plan["machOFiles"]), expected)
        self.assertEqual(len(plan["machOFiles"]), len(expected))
        self.assertEqual({unit["executable"] for unit in plan["units"]}, expected)
        self.assertEqual(len(plan["units"]), len(expected))
        self.assertTrue(all("\\" not in name for name in plan["machOFiles"]))

    def test_framework_and_outer_app_use_bundle_units(self):
        framework = self.framework()
        plan = signing.plan_bundle(self.app)
        units = {unit["path"]: unit for unit in plan["units"]}
        self.assertEqual(units[framework]["kind"], "bundle")
        self.assertEqual(units[framework]["fileType"], DYLIB)
        self.assertEqual(units[framework]["executable"], framework + "/Versions/A/QtCore")
        self.assertEqual(units["."]["kind"], "bundle")
        self.assertEqual(units["."]["fileType"], EXECUTE)
        self.assertEqual(plan["units"][-1]["path"], ".")

    def test_framework_with_multiple_concrete_versions_is_rejected_even_with_direct_binary(self):
        framework = self.framework()
        self.write(framework + "/Versions/B/QtCore", thin(DYLIB))
        self.write(framework + "/Versions/B/Resources/Info.plist", plistlib.dumps({
            "CFBundleExecutable": "QtCore", "CFBundlePackageType": "FMWK",
            "CFBundleIdentifier": "org.qt-project.QtCore",
        }))
        for has_direct_binary in (False, True):
            with self.subTest(has_direct_binary=has_direct_binary):
                if has_direct_binary:
                    self.write(framework + "/QtCore", thin(DYLIB))
                runner = RecordingRunner()
                with self.assertRaises(ValueError):
                    signing.sign_bundle(self.app, runner=runner)
                self.assertEqual(runner.commands, [])

    def test_nested_helper_and_framework_precede_enclosing_bundles(self):
        framework = self.framework()
        helper = framework + "/Helpers/qcopilots-search-helper.app"
        self.create_bundle(self.app / helper, "qcopilots-search-helper")
        library = helper + "/Contents/Frameworks/libhelper.1.dylib"
        self.write(library, thin(DYLIB))
        paths = [unit["path"] for unit in signing.plan_bundle(self.app)["units"]]
        self.assertLess(paths.index(library), paths.index(helper))
        self.assertLess(paths.index(helper), paths.index(framework))
        self.assertEqual(paths[-1], ".")

    def test_missing_bundle_executable_fails_before_signing(self):
        (self.app / "Contents/MacOS/qcopilots-search").unlink()
        runner = RecordingRunner()
        with self.assertRaises(EXPECTED_ERROR + (OSError,)):
            signing.sign_bundle(self.app, runner=runner)
        self.assertEqual(runner.commands, [])

    def test_malformed_bundled_macho_is_rejected_before_signing(self):
        self.write("Contents/Frameworks/libbroken.dylib", thin()[:12])
        runner = RecordingRunner()
        with self.assertRaises(ValueError):
            signing.sign_bundle(self.app, runner=runner)
        self.assertEqual(runner.commands, [])

    def test_internal_library_symlink_is_not_signed_twice(self):
        target = self.write("Contents/Frameworks/liborphan.2.4.dylib", thin(DYLIB))
        alias = target.with_name("liborphan.2.dylib")
        self.symlink(alias, target.name)
        plan = signing.plan_bundle(self.app)
        self.assertIn(target.relative_to(self.app).as_posix(), plan["machOFiles"])
        self.assertNotIn(alias.relative_to(self.app).as_posix(), plan["machOFiles"])

    def test_directory_symlink_is_not_recursed(self):
        self.write("Contents/PlugIns/ggml/libbackend.so", thin(BUNDLE))
        self.symlink(self.app / "Contents/PlugIns/alias", "ggml", directory=True)
        plan = signing.plan_bundle(self.app)
        self.assertIn("Contents/PlugIns/ggml/libbackend.so", plan["machOFiles"])
        self.assertFalse(any("/alias/" in name for name in plan["machOFiles"]))

    def test_external_file_symlink_is_rejected(self):
        outside = self.root / "outside.dylib"
        outside.write_bytes(thin(DYLIB))
        self.symlink(self.app / "Contents/Frameworks/escaped.dylib", outside)
        runner = RecordingRunner()
        with self.assertRaises(EXPECTED_ERROR):
            signing.sign_bundle(self.app, runner=runner)
        self.assertEqual(runner.commands, [])

    def test_external_directory_symlink_is_rejected(self):
        outside = self.root / "outside libraries"
        outside.mkdir()
        (outside / "libexternal.dylib").write_bytes(thin(DYLIB))
        self.symlink(self.app / "Contents/PlugIns/escaped", outside, directory=True)
        with self.assertRaises(EXPECTED_ERROR):
            signing.plan_bundle(self.app)

    def test_symlink_escape_resolution_contract_without_platform_privileges(self):
        # Model the filesystem answers for one link. Real symlink tests above
        # separately exercise the operating system where privileges permit.
        alias = self.write("Contents/Frameworks/escaped.dylib", thin(DYLIB))
        outside = self.root / "outside.dylib"
        outside.write_bytes(thin(DYLIB))
        original_resolve = Path.resolve
        original_is_symlink = Path.is_symlink

        def resolve(path, *args, **kwargs):
            return outside if path == alias else original_resolve(path, *args, **kwargs)

        def is_symlink(path):
            return path == alias or original_is_symlink(path)

        runner = RecordingRunner()
        with patch.object(Path, "resolve", resolve), patch.object(Path, "is_symlink", is_symlink):
            with self.assertRaises(EXPECTED_ERROR):
                signing.sign_bundle(self.app, runner=runner)
        self.assertEqual(runner.commands, [])

    def test_commands_sign_every_unit_in_plan_order_without_deep_signing(self):
        self.inventory_fixture()
        plan = signing.plan_bundle(self.app)
        runner = RecordingRunner()
        signing.sign_bundle(self.app, runner=runner)
        signed = [command for command in runner.commands if self.is_sign(command)]
        self.assertEqual([self.target(command, self.app) for command in signed],
                         [unit["path"] for unit in plan["units"]])
        for command in signed:
            self.assertNotIn("--deep", command)
            flag = "--sign" if "--sign" in command else "-s"
            self.assertEqual(command[command.index(flag) + 1], "-")
            self.assertNotIn("--entitlements", command)
            for argument in command:
                if argument.startswith("--preserve-metadata="):
                    self.assertNotIn("entitlements", argument.partition("=")[2].split(","))
        self.assertEqual(self.target(signed[-1], self.app), ".")

    def test_each_unit_has_strict_verification_and_final_app_has_deep(self):
        self.inventory_fixture()
        plan = signing.plan_bundle(self.app)
        runner = RecordingRunner()
        signing.sign_bundle(self.app, runner=runner)
        verified = [command for command in runner.commands if "--verify" in command]
        self.assertTrue(verified)
        self.assertTrue(all("--strict" in command for command in verified))
        self.assertTrue({unit["path"] for unit in plan["units"]}.issubset(
            {self.target(command, self.app) for command in verified}))
        self.assertEqual(self.target(verified[-1], self.app), ".")
        self.assertIn("--deep", verified[-1])
        self.assertGreater(runner.commands.index(verified[-1]), max(
            index for index, command in enumerate(runner.commands) if self.is_sign(command)))

    def test_codesign_failure_stops_immediately(self):
        self.inventory_fixture()
        runner = RecordingRunner(fail=self.is_sign)
        with self.assertRaises(EXPECTED_ERROR):
            signing.sign_bundle(self.app, runner=runner)
        self.assertEqual(len(runner.commands), 1)

    def test_verification_failure_stops_immediately(self):
        self.inventory_fixture()
        runner = RecordingRunner(fail=lambda command: "--verify" in command)
        with self.assertRaises(EXPECTED_ERROR):
            signing.sign_bundle(self.app, runner=runner)
        self.assertIn("--verify", runner.commands[-1])
        self.assertEqual(sum("--verify" in command for command in runner.commands), 1)

    def test_final_deep_verification_failure_is_not_reported_as_success(self):
        runner = RecordingRunner(fail=lambda command: "--verify" in command and "--deep" in command)
        report_path = self.root / "failed-deep-verification.json"
        with self.assertRaises(EXPECTED_ERROR):
            signing.sign_bundle(self.app, runner=runner, report_path=report_path)
        self.assertIn("--deep", runner.commands[-1])
        self.assertFalse(json.loads(report_path.read_text(encoding="utf-8"))["passed"])

    def test_nonadhoc_identity_is_preserved_and_never_falls_back(self):
        self.inventory_fixture()
        identity = "0123456789ABCDEF0123456789ABCDEF01234567"
        runner = RecordingRunner()
        signing.sign_bundle(self.app, identity=identity, runner=runner)
        units = {unit["path"]: unit for unit in signing.plan_bundle(self.app)["units"]}
        for command in filter(self.is_sign, runner.commands):
            flag = "--sign" if "--sign" in command else "-s"
            self.assertEqual(command[command.index(flag) + 1], identity)
            self.assertIn("--timestamp", command)
            runtime = "runtime" in command
            self.assertEqual(runtime, units[self.target(command, self.app)]["fileType"] == EXECUTE)
        failed = RecordingRunner(fail=self.is_sign)
        with self.assertRaises(EXPECTED_ERROR):
            signing.sign_bundle(self.app, identity=identity, runner=failed)
        self.assertEqual(len(failed.commands), 1)
        self.assertNotIn("-", failed.commands[0])

    def test_empty_identity_rejected_before_any_codesign(self):
        for identity in ("", "   "):
            with self.subTest(identity=identity):
                runner = RecordingRunner()
                with self.assertRaises(EXPECTED_ERROR):
                    signing.sign_bundle(self.app, identity=identity, runner=runner)
                self.assertEqual(runner.commands, [])

    def test_entitlements_apply_only_to_the_named_executable(self):
        self.inventory_fixture()
        entitlements = self.root / "worker 权限.entitlements"
        entitlements.write_bytes(plistlib.dumps({"com.apple.security.network.client": True}))
        runner = RecordingRunner()
        worker = "Contents/MacOS/qcopilots-search-worker"
        signing.sign_bundle(self.app, entitlements={worker: entitlements}, runner=runner)
        entitled = [command for command in runner.commands if "--entitlements" in command]
        self.assertEqual(len(entitled), 1)
        self.assertEqual(self.target(entitled[0], self.app), worker)
        self.assertEqual(Path(entitled[0][entitled[0].index("--entitlements") + 1]), entitlements)

    def test_library_and_plugin_entitlements_are_rejected_before_signing(self):
        self.inventory_fixture()
        entitlements = self.root / "invalid-for-library.entitlements"
        entitlements.write_bytes(plistlib.dumps({}))
        for target in ("Contents/Frameworks/libtesseract.5.5.3.dylib",
                       "Contents/PlugIns/ggml/libggml-cpu-x64.so"):
            with self.subTest(target=target):
                runner = RecordingRunner()
                with self.assertRaises(EXPECTED_ERROR):
                    signing.sign_bundle(self.app, entitlements={target: entitlements}, runner=runner)
                self.assertEqual(runner.commands, [])

    def test_unknown_entitlements_target_is_rejected_before_signing(self):
        entitlements = self.root / "unknown-target.entitlements"
        entitlements.write_bytes(plistlib.dumps({}))
        runner = RecordingRunner()
        with self.assertRaises(ValueError):
            signing.sign_bundle(self.app, entitlements={"../outside": entitlements}, runner=runner)
        self.assertEqual(runner.commands, [])

    def test_report_is_written_outside_bundle_and_is_machine_readable(self):
        self.inventory_fixture()
        report_path = self.root / "reports 空间" / "qcopilots-search-signing.json"
        runner = RecordingRunner()
        result = signing.sign_bundle(self.app, report_path=report_path, runner=runner)
        self.assertIsInstance(result, dict)
        self.assertTrue(result["passed"])
        self.assertEqual(result["identity"], "-")
        self.assertEqual(result["signatureType"], "ad-hoc")
        self.assertIn("inventory", result)
        self.assertIn("commands", result)
        self.assertTrue(report_path.is_file())
        saved = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertTrue(saved["passed"])
        self.assertFalse(any(path.name == report_path.name for path in self.app.rglob("*")))

    def test_report_inside_bundle_is_rejected_before_signing(self):
        runner = RecordingRunner()
        report = self.app / "Contents/Resources/signing-report.json"
        with self.assertRaises(ValueError):
            signing.sign_bundle(self.app, runner=runner, report_path=report)
        self.assertEqual(runner.commands, [])
        self.assertFalse(report.exists())

    def test_codesign_failure_preserves_failure_report(self):
        self.inventory_fixture()
        runner = RecordingRunner(fail=self.is_sign)
        report = self.root / "failure-evidence.json"
        with self.assertRaises(RuntimeError):
            signing.sign_bundle(self.app, runner=runner, report_path=report)
        saved = json.loads(report.read_text(encoding="utf-8"))
        self.assertFalse(saved["passed"])
        self.assertEqual(saved["signatureType"], "ad-hoc")
        self.assertEqual(len(runner.commands), 1)


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    unittest.main(verbosity=2)
