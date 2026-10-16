#!/usr/bin/env python3
"""Portable tests for the final macOS bundle's read-only link verification."""
import importlib.util
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
ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("qcopilots_search_macos_links",
    ROOT / "tools" / "qcs-verify-macos-links.py")
links = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(links)


def macho(file_type, cpu=0x01000007):
    return struct.pack("<8I", 0xFEEDFACF, cpu, 3, file_type, 0, 0, 0, 0)


def commands(*entries):
    # ID is intentionally ignored. It must not become a self-dependency.
    entries = (("LC_ID_DYLIB", "@rpath/self.dylib"), *entries)
    return "image:\n" + "".join(f"Load command {index}\n          cmd {kind}\n"
        f"      cmdsize 64\n         {'path' if kind == 'LC_RPATH' else 'name'} {value} (offset 24)\n"
        for index, (kind, value) in enumerate(entries))


class LinkTests(unittest.TestCase):
    def setUp(self):
        root = os.environ.get("QCOPILOTS_SEARCH_TEST_ROOT") or None
        if root:
            Path(root).mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="qcs-links-", dir=root)
        self.addCleanup(self.temporary.cleanup)
        self.app = Path(self.temporary.name) / "中文 空格" / "qcs-gui.app"
        self.outputs, self.calls = {}, []
        self.main = self.bundle(self.app, "qcs-gui")
        self.library = self.image("Contents/Frameworks/libnative.1.dylib")

    def bundle(self, root, name):
        binary = root / "Contents/MacOS" / name
        binary.parent.mkdir(parents=True)
        binary.write_bytes(macho(2))
        (root / "Contents/Info.plist").write_bytes(plistlib.dumps({"CFBundleExecutable": name}))
        self.outputs[binary.resolve()] = commands(("LC_RPATH", "@executable_path/../Frameworks"))
        return binary.resolve()

    def image(self, relative, file_type=6, entries=()):
        path = self.app / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(macho(file_type))
        self.outputs[path.resolve()] = commands(*entries)
        return path.resolve()

    def runner(self, command, **kwargs):
        self.calls.append(command)
        return subprocess.CompletedProcess(command, 0, self.outputs[Path(command[-1])], "")

    def verify(self):
        return links.verify_bundle(self.app, self.runner)

    def test_bundle_paths_system_dependencies_and_all_load_kinds(self):
        self.outputs[self.main] = commands(("LC_RPATH", "@executable_path/../Frameworks"),
            *((kind, "@rpath/libnative.1.dylib") for kind in sorted(links.LOADS)),
            ("LC_LOAD_DYLIB", "/System/Library/Frameworks/CoreFoundation.framework/Versions/A/CoreFoundation"),
            ("LC_LOAD_DYLIB", "/usr/lib/libSystem.B.dylib"))
        before = {path: path.read_bytes() for path in self.outputs}
        report = self.verify()
        self.assertTrue(report["passed"], report)
        self.assertEqual(len(report["sources"]), 2)
        self.assertEqual(len(report["sources"][1]["resolved"]) + len(report["sources"][0]["resolved"]), len(links.LOADS))
        self.assertEqual(before, {path: path.read_bytes() for path in self.outputs})
        self.assertTrue(all(command[:4] == ["/usr/bin/otool", "-arch", "all", "-l"] for command in self.calls))

    def test_rejects_brew_build_absolute_and_relative_loads(self):
        for load in ("/opt/homebrew/lib/libnative.dylib", "/usr/local/lib/libnative.dylib",
                     str(self.library), "libnative.1.dylib", "@loader_path/../../../../outside.dylib",
                     "/usr/lib/../../opt/homebrew/libnative.dylib"):
            with self.subTest(load=load):
                self.outputs[self.main] = commands(("LC_LOAD_DYLIB", load))
                self.assertFalse(self.verify()["passed"])

    def test_soname_copy_with_unused_external_rpath_is_rejected(self):
        self.image("Contents/Frameworks/libnative.1.2.3.dylib")
        self.outputs[self.library] = commands(("LC_RPATH", "/Users/runner/build/lib"))
        report = self.verify()
        self.assertFalse(report["passed"])
        self.assertTrue(any("libnative.1.dylib" in error and "absolute" in error for error in report["errors"]))
        self.assertEqual(len(self.calls), 3)

    def test_absolute_internal_rpath_and_unknown_tokens_are_rejected(self):
        for rpath in (str(self.library.parent), "@rpath/Frameworks", "Frameworks", "@unknown/lib"):
            with self.subTest(rpath=rpath):
                self.outputs[self.library] = commands(("LC_RPATH", rpath))
                self.assertFalse(self.verify()["passed"])

    def test_rpath_missing_non_macho_and_traversal_fail(self):
        for load in ("@rpath/missing.dylib", "@rpath/../../../outside.dylib", "@rpath/readme.txt"):
            with self.subTest(load=load):
                (self.library.parent / "readme.txt").write_text("resource", encoding="utf-8")
                self.outputs[self.main] = commands(("LC_RPATH", "@executable_path/../Frameworks"),
                                                   ("LC_LOAD_DYLIB", load))
                self.assertFalse(self.verify()["passed"])

    def test_loader_relative_and_inherited_executable_rpath(self):
        sibling = self.image("Contents/Frameworks/libchild.dylib")
        self.outputs[self.library] = commands(("LC_LOAD_DYLIB", "@rpath/libchild.dylib"))
        self.outputs[sibling] = commands(("LC_LOAD_DYLIB", "@loader_path/libnative.1.dylib"))
        self.assertTrue(self.verify()["passed"])

    def test_every_executable_context_is_checked(self):
        helper = self.image("Contents/Helpers/helper", file_type=2)
        self.outputs[self.library] = commands(("LC_LOAD_DYLIB", "@executable_path/../Frameworks/libnative.1.dylib"))
        self.assertTrue(self.verify()["passed"])
        nested_helper = self.image("Contents/Helpers/nested/helper", file_type=2)
        self.assertFalse(self.verify()["passed"])
        self.assertNotEqual(helper.parent, nested_helper.parent)

    def test_one_executable_cannot_borrow_another_executables_rpaths(self):
        helper = self.image("Contents/MacOS/qcs-worker", file_type=2)
        self.outputs[self.library] = commands(("LC_LOAD_DYLIB", "@rpath/libnative.1.dylib"))
        report = self.verify()
        self.assertFalse(report["passed"])
        self.assertTrue(any(helper.name in error and "Unresolved" in error for error in report["errors"]))

    def test_nested_app_uses_its_own_executable_context(self):
        nested = self.app / "Contents/Helpers/child.app"
        child = self.bundle(nested, "child")
        library = self.image("Contents/Helpers/child.app/Contents/Frameworks/libchild.dylib")
        self.outputs[child] = commands(("LC_LOAD_DYLIB", "@executable_path/../Frameworks/libchild.dylib"))
        self.outputs[library] = commands(("LC_RPATH", "@executable_path/../Frameworks"),
                                         ("LC_LOAD_DYLIB", "@rpath/libchild.dylib"))
        self.assertTrue(self.verify()["passed"])

    def test_nested_app_cannot_validate_external_library_in_wrong_context(self):
        nested = self.app / "Contents/Helpers/child.app"
        child = self.bundle(nested, "child")
        self.outputs[child] = commands(("LC_LOAD_DYLIB", "@loader_path/../../../../Frameworks/libnative.1.dylib"))
        self.outputs[self.library] = commands(("LC_LOAD_DYLIB", "@executable_path/../Frameworks/libnative.1.dylib"))
        report = self.verify()
        self.assertFalse(report["passed"])
        self.assertTrue(any("Cross-app" in error for error in report["errors"]))

    def test_resolved_symlink_escape_rejected_without_privileged_creation(self):
        alias = self.app / "Contents/Frameworks/escape.dylib"
        outside = Path(self.temporary.name) / "outside.dylib"
        outside.write_bytes(macho(6))
        self.outputs[self.main] = commands(("LC_LOAD_DYLIB", "@loader_path/../Frameworks/escape.dylib"))
        original = Path.resolve
        # Test the resolver boundary on every platform. The signing inventory's
        # separate tests exercise actual symlinks on supported filesystems.
        def resolve(path, *args, **kwargs):
            normalized = original(path, *args, **kwargs)
            return outside.resolve() if normalized == alias else normalized
        with patch.object(Path, "resolve", resolve):
            report = self.verify()
        self.assertFalse(report["passed"])
        self.assertTrue(any("escapes" in error for error in report["errors"]))

    def test_resolved_internal_alias_points_to_inventory(self):
        alias = self.app / "Contents/Frameworks/libalias.dylib"
        self.outputs[self.main] = commands(("LC_LOAD_DYLIB", "@loader_path/../Frameworks/libalias.dylib"))
        original = Path.resolve
        def resolve(path, *args, **kwargs):
            normalized = original(path, *args, **kwargs)
            return self.library if normalized == alias else normalized
        with patch.object(Path, "resolve", resolve):
            self.assertTrue(self.verify()["passed"])

    def test_otool_failure_and_malformed_output_fail_closed(self):
        report = links.verify_bundle(self.app, lambda command, **kwargs:
            subprocess.CompletedProcess(command, 1, "", "cannot read Mach-O"))
        self.assertFalse(report["passed"])
        for output in ("", "image:\n", "Load command 0\n cmd LC_RPATH\n cmdsize 16\n"):
            with self.subTest(output=output), self.assertRaises(ValueError):
                links.parse_commands(output)

    def test_universal_slices_are_not_combined(self):
        output = ("image (architecture x86_64):\n" + commands(("LC_RPATH", "@loader_path"))
                  + "image (architecture arm64):\n" + commands(("LC_LOAD_DYLIB", "@rpath/missing.dylib")))
        parsed = links.parse_commands(output)
        self.assertEqual(parsed["arm64"]["rpaths"], [])
        self.assertEqual(parsed["x86_64"]["loads"], [])

    def test_missing_slice_and_dependency_architecture_are_rejected(self):
        self.outputs[self.main] = "image (architecture arm64):\n" + commands()
        self.assertFalse(self.verify()["passed"])
        self.outputs[self.main] = commands(("LC_LOAD_DYLIB", "@loader_path/../Frameworks/libnative.1.dylib"))
        self.library.write_bytes(macho(6, 0x0100000C))
        report = self.verify()
        self.assertFalse(report["passed"])
        self.assertTrue(any("lacks architecture x86_64" in error for error in report["errors"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
