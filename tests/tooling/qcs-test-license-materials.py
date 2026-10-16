#!/usr/bin/env python3
"""Portable license-collector contracts using only tiny synthetic fixtures.

No SDK, downloaded archive, build output or real acceptance report is used.
Archive links are tar headers, so these cases need no symlink privileges.
"""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
import zipfile
from unittest.mock import patch


sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "tools" / "qcs-license-materials.py"
SPEC = importlib.util.spec_from_file_location("qcopilots_search_license_materials", SCRIPT)
materials = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = materials
SPEC.loader.exec_module(materials)

ERRORS = (ValueError, OSError)
MANIFEST = "qcs-license-manifest.json"
MODULES = {
    "qtbase": "6.12.0",
    "qt5compat": "6.12.0",
    "qtsvg": "6.12.0",
    "qtimageformats": "6.12.0",
    "qtmultimedia": "6.12.0",
    "qttranslations": "6.12.0",
    "qtpdf": "6.140.0",
    "qtwebengine-chromium-pdf": "6.140.0",
}
ARCHIVES = ("qtbase", "qtsvg", "qtimageformats", "qtmultimedia")
VENDORED_HEADERS = ("vendor/stb/stb_image.h", "vendor/miniaudio/miniaudio.h",
                    "vendor/sheredom/subprocess.h")
STANDARD_IDS = ("MIT", "CC0-1.0", "GPL-2.0-only", "GPL-3.0-only", "LGPL-3.0-only",
                "Qt-GPL-exception-1.0", "BSD-3-Clause")


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def json_bytes(value) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def synthetic_license(identifier: str) -> bytes:
    return (f"SYNTHETIC TEST FIXTURE ONLY: {identifier}\n"
            "Copyright (c) synthetic license-collector test authors.\n"
            "This is intentionally not a real redistribution license artifact.\n").encode()


class LicenseMaterialsTests(unittest.TestCase):
    def setUp(self):
        configured = os.environ.get("QCOPILOTS_SEARCH_TEST_ROOT") or None
        if configured:
            Path(configured).mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(
            prefix="qcs-SYNTHETIC-license-", dir=configured)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.sdk = self.root / "SDK with spaces 测试"
        self.sbom = self.sdk / "sbom"
        self.sbom.mkdir(parents=True)
        self.llama = self.root / "synthetic llama"
        self.llama.mkdir()
        (self.llama / "LICENSE").write_bytes(synthetic_license("MIT"))
        for relative in VENDORED_HEADERS:
            header = self.llama / relative
            header.parent.mkdir(parents=True, exist_ok=True)
            header.write_bytes(b"/* " + synthetic_license(relative) + b" */\n")
        self.cache = self.root / "source cache"
        self.cache.mkdir()
        self.output = self.root / "collected materials"
        self.lock = self.root / "synthetic-source-lock.json"
        self.packages = []
        self.members = {}
        for module in ARCHIVES:
            archive_root = f"{module}-everywhere-src-6.12.0"
            self.members[module] = [
                (f"{archive_root}/LICENSES/{identifier}.txt", synthetic_license(identifier))
                for identifier in STANDARD_IDS
            ] + [(f"{archive_root}/src/irrelevant.cpp", b"// SYNTHETIC, never compiled\n")]
            self.packages.append({
                "name": module, "module": module, "version": "6.12.0",
                "filename": f"{archive_root}.tar.xz",
                "url": f"https://invalid.example/SYNTHETIC/{archive_root}.tar.xz",
                "size": 0, "sha256": "", "sourceArchiveRoot": archive_root,
            })
            self.write_archive(module)
        for module in MODULES:
            self.write_sbom(module)
        self.save_lock()
        # A regression must fail before attempting any real download.
        self.no_network = patch("urllib.request.urlopen", side_effect=AssertionError(
            "Synthetic license tests must never access the network"))
        self.no_network.start()
        self.addCleanup(self.no_network.stop)

    def entry(self, module):
        return next(item for item in self.packages if item["module"] == module)

    def save_lock(self):
        self.lock.write_bytes(json_bytes({"schemaVersion": 1, "packages": self.packages}))

    def write_archive(self, module):
        entry = self.entry(module)
        path = self.cache / entry["filename"]
        with tarfile.open(path, "w:xz") as archive:
            for item in self.members[module]:
                name, body, *link = item
                info = tarfile.TarInfo(name)
                info.mtime = 0
                if link:
                    info.type = link[0]
                    info.linkname = body
                    archive.addfile(info)
                else:
                    info.size = len(body)
                    archive.addfile(info, io.BytesIO(body))
        data = path.read_bytes()
        entry.update(size=len(data), sha256=sha(data))
        self.save_lock()

    def sbom_path(self, module):
        return self.sbom / f"{module}-{MODULES[module]}.spdx.json"

    def document(self, module):
        return {
            "spdxVersion": "SPDX-2.3", "SPDXID": "SPDXRef-DOCUMENT",
            "name": f"SYNTHETIC {module}",
            "documentNamespace": f"https://invalid.example/SYNTHETIC/{module}",
            "dataLicense": "CC0-1.0",
            "creationInfo": {"creators": ["Tool: SYNTHETIC unittest"],
                             "created": "2000-01-01T00:00:00Z"},
            "packages": [{
                "SPDXID": "SPDXRef-Package", "name": module,
                "versionInfo": MODULES[module], "downloadLocation": "NOASSERTION",
                "licenseDeclared": "MIT OR LGPL-3.0-only OR GPL-3.0-only",
                "licenseConcluded": "MIT AND LicenseRef-Synthetic",
                "copyrightText": "Copyright (c) SYNTHETIC fixture author",
            }],
            "hasExtractedLicensingInfos": [{
                "licenseId": "LicenseRef-Synthetic",
                "extractedText": f"SYNTHETIC custom license text for {module}.\n",
            }],
            "files": [], "relationships": [],
        }

    def write_sbom(self, module, document=None):
        self.sbom_path(module).write_bytes(json_bytes(
            self.document(module) if document is None else document))

    def collect(self, output=None):
        return materials.collect(self.sdk, self.llama, self.cache,
                                 output or self.output, self.lock, download=False)

    def saved_manifest(self):
        return json.loads((self.output / MANIFEST).read_text(encoding="utf-8"))

    def save_manifest(self, manifest):
        (self.output / MANIFEST).write_bytes(json_bytes(manifest))

    def expect_collect_rejected(self, pattern=None):
        if pattern:
            with self.assertRaisesRegex(ERRORS, pattern):
                self.collect()
        else:
            with self.assertRaises(ERRORS):
                self.collect()

    def test_collect_preserves_sources_and_binds_every_material(self):
        sources = [*(path for path in self.llama.rglob("*") if path.is_file()), *self.sbom.glob("*.spdx.json"),
                   *self.cache.glob("*.tar.xz"), self.lock]
        before = {str(path): sha(path.read_bytes()) for path in sources}
        manifest = self.collect()
        self.assertEqual(manifest, self.saved_manifest())
        self.assertEqual(manifest["schemaVersion"], 1)
        self.assertEqual(manifest["kind"], "runtime-license-materials")
        self.assertTrue(manifest["files"])
        paths = [item["path"] for item in manifest["files"]]
        self.assertEqual(len(paths), len(set(paths)))
        self.assertIn("llama/LICENSE", paths)
        for relative in VENDORED_HEADERS:
            self.assertIn("llama/" + relative, paths)
            self.assertEqual((self.output / "llama" / relative).read_bytes(),
                             (self.llama / relative).read_bytes())
        for module, version in MODULES.items():
            relative = f"qt/sbom/{module}-{version}.spdx.json"
            self.assertIn(relative, paths)
            self.assertEqual((self.output / relative).read_bytes(), self.sbom_path(module).read_bytes())
        for item in manifest["files"]:
            data = (self.output / item["path"]).read_bytes()
            self.assertEqual(len(data), item["bytes"])
            self.assertEqual(sha(data), item["sha256"])
        self.assertEqual({p.relative_to(self.output).as_posix() for p in self.output.rglob("*") if p.is_file()},
                         set(paths) | {MANIFEST})
        self.assertIsInstance(materials.verify_materials(self.output), dict)
        self.assertEqual(before, {str(path): sha(path.read_bytes()) for path in sources})

    def test_archive_parent_traversal_is_rejected(self):
        self.members["qtbase"].append(("../escaped-license.txt", b"SYNTHETIC"))
        self.write_archive("qtbase")
        self.expect_collect_rejected()
        self.assertFalse((self.root / "escaped-license.txt").exists())

    def test_archive_absolute_path_is_rejected(self):
        self.members["qtbase"].append(("/SYNTHETIC-outside/LICENSE.txt", b"SYNTHETIC"))
        self.write_archive("qtbase")
        self.expect_collect_rejected()

    def test_archive_backslash_path_is_rejected(self):
        self.members["qtbase"].append(("qtbase\\LICENSES\\MIT.txt", b"SYNTHETIC"))
        self.write_archive("qtbase")
        self.expect_collect_rejected()

    def test_archive_drive_path_is_rejected(self):
        self.members["qtbase"].append(("C:/SYNTHETIC/LICENSES/MIT.txt", b"SYNTHETIC"))
        self.write_archive("qtbase")
        self.expect_collect_rejected()

    def test_archive_license_symlink_is_rejected(self):
        root = self.entry("qtbase")["sourceArchiveRoot"]
        self.members["qtbase"].append((f"{root}/LICENSES/linked.txt", "../../elsewhere", tarfile.SYMTYPE))
        self.write_archive("qtbase")
        self.expect_collect_rejected()

    def test_archive_license_hardlink_is_rejected(self):
        root = self.entry("qtbase")["sourceArchiveRoot"]
        self.members["qtbase"].append((f"{root}/LICENSES/linked.txt", f"{root}/LICENSES/MIT.txt", tarfile.LNKTYPE))
        self.write_archive("qtbase")
        self.expect_collect_rejected()

    def test_irrelevant_upstream_symlink_is_not_materialized(self):
        root = self.entry("qtbase")["sourceArchiveRoot"]
        self.members["qtbase"].append((f"{root}/src/unrelated-link", "elsewhere.cpp", tarfile.SYMTYPE))
        self.write_archive("qtbase")
        manifest = self.collect()
        self.assertFalse(any("unrelated-link" in item["path"] for item in manifest["files"]))
        materials.verify_materials(self.output)

    def test_duplicate_archive_member_is_rejected(self):
        self.members["qtbase"].append(self.members["qtbase"][0])
        self.write_archive("qtbase")
        self.expect_collect_rejected()

    def test_bad_archive_sha_is_rejected(self):
        self.entry("qtbase")["sha256"] = "0" * 64
        self.save_lock()
        self.expect_collect_rejected()

    def test_bad_archive_size_is_rejected(self):
        self.entry("qtbase")["size"] += 1
        self.save_lock()
        self.expect_collect_rejected()

    def test_missing_cached_archive_fails_without_download(self):
        (self.cache / self.entry("qtbase")["filename"]).unlink()
        self.expect_collect_rejected()

    def test_missing_source_module_is_rejected(self):
        self.packages[:] = [item for item in self.packages if item["module"] != "qtmultimedia"]
        self.save_lock()
        self.expect_collect_rejected()

    def test_missing_vendored_header_is_rejected(self):
        for relative in VENDORED_HEADERS:
            with self.subTest(header=relative):
                header = self.llama / relative
                original = header.read_bytes()
                try:
                    header.unlink()
                    self.expect_collect_rejected()
                finally:
                    header.write_bytes(original)

    def test_duplicate_source_module_is_rejected(self):
        self.packages.append(dict(self.entry("qtbase")))
        self.save_lock()
        self.expect_collect_rejected()

    def test_missing_mandatory_sbom_is_rejected(self):
        self.sbom_path("qtwebengine-chromium-pdf").unlink()
        self.expect_collect_rejected()

    def test_malformed_sbom_json_is_rejected(self):
        self.sbom_path("qtbase").write_text('{"packages": [', encoding="utf-8")
        self.expect_collect_rejected()

    def test_duplicate_json_key_is_rejected(self):
        # A normal JSON parser would retain the later, valid packages value.
        # This fixture must fail specifically because the key is duplicated.
        payload = json_bytes(self.document("qtbase")).decode("utf-8")
        self.sbom_path("qtbase").write_text(
            payload.replace("{", '{"packages": [],', 1), encoding="utf-8")
        self.expect_collect_rejected()

    def test_custom_license_without_text_is_rejected(self):
        doc = self.document("qtbase")
        doc["hasExtractedLicensingInfos"][0].pop("extractedText")
        self.write_sbom("qtbase", doc)
        self.expect_collect_rejected()

    def test_custom_license_with_blank_text_is_rejected(self):
        doc = self.document("qtbase")
        doc["hasExtractedLicensingInfos"][0]["extractedText"] = " \n\t"
        self.write_sbom("qtbase", doc)
        self.expect_collect_rejected()

    def test_undefined_local_custom_license_is_rejected(self):
        doc = self.document("qtbase")
        doc["packages"][0]["licenseConcluded"] = "LicenseRef-UndefinedSynthetic"
        self.write_sbom("qtbase", doc)
        self.expect_collect_rejected()

    def test_duplicate_custom_license_id_is_rejected(self):
        doc = self.document("qtbase")
        doc["hasExtractedLicensingInfos"].append(dict(doc["hasExtractedLicensingInfos"][0]))
        self.write_sbom("qtbase", doc)
        self.expect_collect_rejected()

    def test_standard_license_without_text_coverage_is_rejected(self):
        doc = self.document("qtbase")
        doc["packages"][0]["licenseDeclared"] = "Apache-2.0"
        self.write_sbom("qtbase", doc)
        self.expect_collect_rejected()

    def test_standard_exception_requires_its_own_text(self):
        doc = self.document("qtbase")
        doc["packages"][0]["licenseDeclared"] = "GPL-3.0-only WITH Synthetic-exception-1.0"
        self.write_sbom("qtbase", doc)
        self.expect_collect_rejected()

    def test_plural_file_license_field_collects_standard_text(self):
        doc = self.document("qtbase")
        doc["files"] = [{"SPDXID": "SPDXRef-SyntheticFile", "fileName": "synthetic.c",
                         "licenseInfoInFiles": ["ISC"]}]
        self.write_sbom("qtbase", doc)
        archive_root = self.entry("qtbase")["sourceArchiveRoot"]
        original = synthetic_license("ISC")
        self.members["qtbase"].append((f"{archive_root}/LICENSES/ISC.txt", original))
        self.write_archive("qtbase")
        manifest = self.collect()
        self.assertIn("ISC", manifest["standardLicenseIds"])
        self.assertIn("ISC", manifest["moduleCoverage"]["qtbase"])
        relative = manifest["standardLicenseFiles"]["ISC"]
        self.assertEqual((self.output / relative).read_bytes(), original)
        self.assertEqual(next(item["sha256"] for item in manifest["files"] if item["path"] == relative),
                         sha(original))
        materials.verify_materials(self.output)

    def test_plural_file_license_field_without_standard_text_is_rejected(self):
        doc = self.document("qtbase")
        doc["files"] = [{"SPDXID": "SPDXRef-SyntheticFile", "fileName": "synthetic.c",
                         "licenseInfoInFiles": ["ISC"]}]
        self.write_sbom("qtbase", doc)
        self.expect_collect_rejected()

    def test_changed_manifest_attachment_is_rejected(self):
        self.collect()
        for entry in self.saved_manifest()["files"]:
            with self.subTest(path=entry["path"]):
                path = self.output / entry["path"]
                original = path.read_bytes()
                try:
                    path.write_bytes(original + b"\nSYNTHETIC tampering\n")
                    with self.assertRaises(ERRORS):
                        materials.verify_materials(self.output)
                finally:
                    path.write_bytes(original)
        materials.verify_materials(self.output)

    def test_missing_manifest_attachment_is_rejected(self):
        self.collect()
        for entry in self.saved_manifest()["files"]:
            with self.subTest(path=entry["path"]):
                path = self.output / entry["path"]
                original = path.read_bytes()
                try:
                    path.unlink()
                    with self.assertRaises(ERRORS):
                        materials.verify_materials(self.output)
                finally:
                    path.write_bytes(original)
        materials.verify_materials(self.output)

    def test_duplicate_manifest_path_is_rejected(self):
        self.collect()
        manifest = self.saved_manifest()
        manifest["files"].append(dict(manifest["files"][0]))
        self.save_manifest(manifest)
        with self.assertRaises(ERRORS):
            materials.verify_materials(self.output)

    def test_unsafe_manifest_path_is_rejected(self):
        self.collect()
        manifest = self.saved_manifest()
        outside = self.root / "outside-SYNTHETIC.txt"
        outside.write_bytes(b"SYNTHETIC outside content")
        manifest["files"][0].update(path="../outside-SYNTHETIC.txt",
                                    bytes=outside.stat().st_size, sha256=sha(outside.read_bytes()))
        self.save_manifest(manifest)
        with self.assertRaises(ERRORS):
            materials.verify_materials(self.output)

    def test_manifest_standard_license_mapping_must_remain_bound(self):
        self.collect()
        manifest = self.saved_manifest()
        manifest["standardLicenseFiles"]["MIT"] = "qt/sources/qtbase/LICENSES/missing.txt"
        self.save_manifest(manifest)
        with self.assertRaises(ERRORS):
            materials.verify_materials(self.output)

    def test_standard_license_cannot_map_to_existing_readme(self):
        self.collect()
        manifest = self.saved_manifest()
        self.assertIn("README.txt", {item["path"] for item in manifest["files"]})
        manifest["standardLicenseFiles"]["MIT"] = "README.txt"
        self.save_manifest(manifest)
        with self.assertRaises(ERRORS):
            materials.verify_materials(self.output)

    def test_standard_license_cannot_map_to_another_existing_license(self):
        self.collect()
        manifest = self.saved_manifest()
        other = "qt/sources/qtbase/LICENSES/BSD-3-Clause.txt"
        self.assertIn(other, {item["path"] for item in manifest["files"]})
        manifest["standardLicenseFiles"]["MIT"] = other
        self.save_manifest(manifest)
        with self.assertRaises(ERRORS):
            materials.verify_materials(self.output)

    def test_source_archive_metadata_mutation_is_rejected(self):
        self.collect()
        original = (self.output / MANIFEST).read_bytes()
        for index in range(len(self.saved_manifest()["sourceArchives"])):
            for field in ("name", "url", "filename", "size", "sha256"):
                with self.subTest(archive=index, field=field):
                    manifest = json.loads(original)
                    entry = manifest["sourceArchives"][index]
                    entry[field] = (entry[field] + 1 if field == "size" else
                                    "0" * 64 if field == "sha256" else
                                    "https://invalid.example/ALTERED-SYNTHETIC.tar.xz" if field == "url" else
                                    "ALTERED-SYNTHETIC" + entry[field])
                    try:
                        self.save_manifest(manifest)
                        with self.assertRaises(ERRORS):
                            materials.verify_materials(self.output)
                    finally:
                        (self.output / MANIFEST).write_bytes(original)
        materials.verify_materials(self.output)

    def test_attribution_preserves_literal_newlines_and_repeated_comment(self):
        archive_root = self.entry("qtbase")["sourceArchiveRoot"]
        relative = "src/3rdparty/synthetic/qt_attribution.json"
        original = (b'{"Comment": "SYNTHETIC first line\nliteral second line",\n'
                    b' "Comment": "SYNTHETIC repeated informational field",\n'
                    b' "LicenseFile": "terms.txt"}\n')
        terms = synthetic_license("attribution-referenced-terms")
        self.members["qtbase"].extend([
            (f"{archive_root}/{relative}", original),
            (f"{archive_root}/src/3rdparty/synthetic/terms.txt", terms),
        ])
        self.write_archive("qtbase")
        manifest = self.collect()
        path = f"qt/sources/qtbase/{relative}"
        self.assertEqual((self.output / path).read_bytes(), original)
        self.assertEqual(next(item["sha256"] for item in manifest["files"] if item["path"] == path),
                         sha(original))
        self.assertEqual((self.output / "qt/sources/qtbase/src/3rdparty/synthetic/terms.txt").read_bytes(),
                         terms)
        materials.verify_materials(self.output)

    def test_attribution_duplicate_file_reference_keys_are_rejected(self):
        archive_root = self.entry("qtbase")["sourceArchiveRoot"]
        base = list(self.members["qtbase"])
        for key in ("LicenseFile", "LicenseFiles", "NoticeFile"):
            with self.subTest(key=key):
                # The referenced destination exists. A last-key-wins parser would pass,
                # so rejection specifically exercises duplicate operational keys.
                value = json.dumps(["terms.txt"] if key == "LicenseFiles" else "terms.txt")
                duplicate = ('{"' + key + '": ' + value + ', "' + key + '": ' + value + '}\n').encode()
                self.members["qtbase"] = base + [
                    (f"{archive_root}/src/3rdparty/synthetic/qt_attribution.json", duplicate),
                    (f"{archive_root}/src/3rdparty/synthetic/terms.txt", synthetic_license("attribution-terms")),
                ]
                self.write_archive("qtbase")
                self.expect_collect_rejected()

    def test_repeated_generation_drops_removed_materials(self):
        self.collect()
        removed = self.output / "qt/extracted/qtbase/LicenseRef-Synthetic.txt"
        self.assertTrue(removed.is_file())
        doc = self.document("qtbase")
        doc["packages"][0]["licenseConcluded"] = "MIT"
        doc["hasExtractedLicensingInfos"] = []
        self.write_sbom("qtbase", doc)
        manifest = self.collect()
        self.assertFalse(removed.exists())
        self.assertNotIn(removed.relative_to(self.output).as_posix(),
                         {item["path"] for item in manifest["files"]})
        materials.verify_materials(self.output)


    def runtime_manifest(self):
        license_path = self.root / "GPU SDK/NOTICE.txt"
        license_path.parent.mkdir()
        license_path.write_bytes(synthetic_license("runtime-component"))
        selection = self.root / "runtime-licenses.json"
        document = {"schemaVersion": 1, "resources": [{"name": "synthetic-runtime", "version": "1.0",
                    "files": [{"path": "NOTICE.txt", "source": "GPU SDK/NOTICE.txt",
                               "sha256": sha(license_path.read_bytes())}]}]}
        selection.write_bytes(json_bytes(document))
        return selection, document, license_path

    def test_runtime_originals_are_bound_to_component_manifest(self):
        selection, _, original = self.runtime_manifest()
        materials.collect(self.sdk, self.llama, self.cache, self.output, self.lock,
                          runtime_license_manifests=[selection])
        self.assertEqual((self.output / "runtime/synthetic-runtime/NOTICE.txt").read_bytes(), original.read_bytes())
        result = materials.verify_materials(self.output)
        self.assertEqual(result["runtimeComponents"], ["synthetic-runtime"])
        manifest = self.saved_manifest()
        manifest["runtimeResources"] = []
        self.save_manifest(manifest)
        with self.assertRaisesRegex(ValueError, "notices differ"):
            materials.verify_materials(self.output)

    def test_runtime_changed_source_is_rejected(self):
        selection, _, original = self.runtime_manifest()
        original.write_bytes(b"SYNTHETIC changed notice")
        with self.assertRaisesRegex(ValueError, "SHA-256 differs"):
            materials.runtime_license_materials([selection])

    def test_runtime_missing_notices_or_unsafe_destination_is_rejected(self):
        selection, document, _ = self.runtime_manifest()
        document["resources"][0]["files"][0]["path"] = "../escaped.txt"
        selection.write_bytes(json_bytes(document))
        with self.assertRaisesRegex(ValueError, "Unsafe license path"):
            materials.runtime_license_materials([selection])
        document["resources"][0]["files"] = []
        selection.write_bytes(json_bytes(document))
        with self.assertRaisesRegex(ValueError, "no redistribution notices"):
            materials.runtime_license_materials([selection])

    def test_duplicate_components_across_selections_are_rejected(self):
        selection, _, _ = self.runtime_manifest()
        with self.assertRaisesRegex(ValueError, "duplicate runtime license component"):
            materials.runtime_license_materials([selection, selection])

    def gpu_source_lock(self, files, archives=None):
        path = self.root / "gpu-license-sources.lock.json"
        path.write_bytes(json_bytes({"schemaVersion": 1, "rocm": {"10.0.0": {
            "resources": [{"name": "synthetic-gpu", "version": "10.0.0", "files": files}],
            "sourceArchives": archives or []}}}))
        return patch.object(materials, "GPU_LICENSE_LOCK", path)

    def test_pinned_gpu_notice_is_loaded_offline_and_checked(self):
        data = synthetic_license("fixed GPU source")
        checksum = sha(data)
        (self.cache / checksum).write_bytes(data)
        record = {"path": "LICENSE.txt", "url": "https://invalid.example/fixed/LICENSE.txt",
                  "sha256": checksum, "size": len(data)}
        with self.gpu_source_lock([record]):
            selected = materials.fetch_gpu_license_sources("hip", "10.0.0", self.cache)
            self.assertEqual(Path(selected["resources"][0]["files"][0]["source"]).read_bytes(), data)
            (self.cache / checksum).write_bytes(b"changed")
            with self.assertRaisesRegex(ValueError, "Cached GPU license source"):
                materials.fetch_gpu_license_sources("hip", "10.0.0", self.cache)

    def test_pinned_gpu_notice_missing_cache_or_unreviewed_version_fails(self):
        record = {"path": "LICENSE", "url": "https://invalid.example/fixed/LICENSE",
                  "sha256": "1" * 64, "size": 10}
        with self.gpu_source_lock([record]):
            with self.assertRaisesRegex(ValueError, "source is missing"):
                materials.fetch_gpu_license_sources("rocm", "10.0.0", self.cache)
            with self.assertRaisesRegex(ValueError, "No reviewed"):
                materials.fetch_gpu_license_sources("hip", "10.0.1", self.cache)

    def test_pinned_gpu_archive_extracts_only_bound_notice(self):
        data = synthetic_license("archive notice")
        archive_path = self.root / "source.zip"
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.writestr("source/LICENSE", data)
            archive.writestr("source/unrelated.txt", b"SYNTHETIC not deployed")
        archive_hash = sha(archive_path.read_bytes())
        (self.cache / archive_hash).write_bytes(archive_path.read_bytes())
        record = {"path": "LICENSE", "archive": "test-source", "member": "source/LICENSE",
                  "sha256": sha(data), "size": len(data)}
        source = {"name": "test-source", "url": "https://invalid.example/fixed/source.zip",
                  "sha256": archive_hash, "size": archive_path.stat().st_size}
        with self.gpu_source_lock([record], [source]):
            result = materials.fetch_gpu_license_sources("hip", "10.0.0", self.cache)
        self.assertEqual(Path(result["resources"][0]["files"][0]["source"]).read_bytes(), data)
        self.assertFalse((self.cache / "source").exists())

    def test_pinned_gpu_download_with_wrong_bytes_leaves_no_completed_cache(self):
        record = {"path": "LICENSE", "url": "https://invalid.example/fixed/LICENSE",
                  "sha256": "1" * 64, "size": 10}
        with self.gpu_source_lock([record]), patch.object(materials.urllib.request, "urlopen",
                                                        return_value=io.BytesIO(b"wrongbytes")):
            with self.assertRaisesRegex(ValueError, "Downloaded GPU license source"):
                materials.fetch_gpu_license_sources("hip", "10.0.0", self.cache, download=True)
        self.assertFalse((self.cache / ("1" * 64)).exists())
        self.assertFalse(list(self.cache.glob("qcs-gpu-license-*.part")))


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    unittest.main(verbosity=2)
