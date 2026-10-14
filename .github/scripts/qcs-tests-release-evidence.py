#!/usr/bin/env python3
"""Exercise release evidence validation with synthetic, in-memory counterexamples.

These documents are test inputs, never application acceptance reports. No model,
application process, source-tree temporary directory, or developer profile is used.
"""
import base64
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
import hashlib
import importlib.util
import io
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import zipfile

sys.dont_write_bytecode = True
spec = importlib.util.spec_from_file_location(
    "qcopilots_search_release_evidence",
    Path(__file__).resolve().parents[2] / "tools" / "qcs-release-evidence.py")
evidence = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evidence)


def package_license_fixture(root, application):
    """Small synthetic SPDX tree with actual file hashes, no SDK download."""
    materials = root / "runtime-license-materials"
    materials.mkdir()
    modules = {name: "6.12.0" for name in (
        "qtbase", "qt5compat", "qtsvg", "qtimageformats", "qtmultimedia", "qttranslations")}
    modules.update({"qtpdf": "6.140.0", "qtwebengine-chromium-pdf": "6.140.0"})
    source_lock = Path(__file__).resolve().parents[2] / "tools/qcs-source-packages.lock.json"
    locked = json.loads(source_lock.read_text(encoding="utf-8"))
    archives = {item["name"]: item for item in locked["packages"]}
    contents = {"README.txt": "SYNTHETIC VALIDATOR FIXTURE, NOT RELEASE LICENSE MATERIALS\n",
                "llama/LICENSE": "Synthetic llama license text for validator tests only.\n",
                "llama/vendor/stb/stb_image.h": "SYNTHETIC stb_image.h notice placeholder, validator input only.\n",
                "llama/vendor/miniaudio/miniaudio.h": "SYNTHETIC miniaudio.h notice placeholder, validator input only.\n",
                "llama/vendor/sheredom/subprocess.h": "SYNTHETIC subprocess.h notice placeholder, validator input only.\n",
                "qt/source-packages.lock.json": json.dumps(locked),
                "qt/sources/qtbase/LICENSES/MIT.txt": "Synthetic MIT license text for validator tests only.\n"}
    for module, version in modules.items():
        contents[f"qt/sbom/{module}-{version}.spdx.json"] = json.dumps({
            "spdxVersion": "SPDX-2.3", "packages": [{"name": module, "licenseDeclared": "MIT"}]})
    files = []
    for name, text in contents.items():
        path = materials / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        files.append({"path": name, "bytes": path.stat().st_size,
                      "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    manifest = materials / "qcs-license-manifest.json"
    manifest.write_text(json.dumps({"schemaVersion": 1, "kind": "runtime-license-materials", "qtModules": modules,
        "files": files, "sourceArchives": [{key: archives[module][key]
            for key in ("name", "url", "filename", "size", "sha256")}
            for module in ("qtbase", "qtsvg", "qtimageformats", "qtmultimedia")],
        "moduleCoverage": {module: ["MIT"] for module in modules}, "customLicenseFiles": {},
        "standardLicenseIds": ["MIT"], "standardLicenseFiles": {"MIT": "qt/sources/qtbase/LICENSES/MIT.txt"},
        "runtimeResources": []}),
        encoding="utf-8")
    return {"passed": True, "files": len(files), "qtModules": list(modules), "standardLicenses": 1,
            "runtimeComponents": [],
            "manifest": manifest.relative_to(root).as_posix(),
            "manifestSha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
            "installedManifestPath": str(application / "share/qcs/licenses/qcs-runtime" / manifest.name)}


def write_program_inventory(root, report, programs=None):
    """Explicitly capture synthetic installed files after a test changes identity."""
    worker = Path(report["modelSmoke"]["runtime"]["workerExecutable"]["path"])
    application = Path(report["applicationRoot"]).resolve()
    if programs is None:
        programs = {name: worker.parent / (name + worker.suffix) for name in (
            "qcs-gui", "qcs-service", "qcs-worker", "qcs-cli")}
    inventory = {"schemaVersion": 1, "kind": "qcs-installed-programs",
        "candidateCommit": report["buildIdentity"]["candidateCommit"],
        "variant": report["variant"], "packageSha256": report["packageSha256"],
        "applicationRoot": report["applicationRoot"], "programs": {
            name: {"path": path.resolve().relative_to(application).as_posix(),
                "bytes": path.stat().st_size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
            for name, path in programs.items()}}
    path = root / "installed-programs.json"
    path.write_text(json.dumps(inventory), encoding="utf-8")
    report["programInventory"] = {"path": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                                  "stable": True}
    return inventory


def use_posix_program_fixture(report):
    """Give cross-platform validator fixtures their actual installed basenames."""
    worker = report["modelSmoke"]["runtime"]["workerExecutable"]
    directory = Path(worker["path"]).parent
    for name in ("qcs-gui", "qcs-service", "qcs-worker", "qcs-cli"):
        (directory / name).write_bytes((directory / (name + ".exe")).read_bytes())
    worker["path"] = str(directory / "qcs-worker")


def package_release_fixture(root, *, worker=None, application_root=None,
                            model_fingerprint="synthetic-model-fingerprint",
                            extraction_fingerprint="synthetic-extraction-fingerprint"):
    """Synthetic verifier input using immutable vectors, never an inference run."""
    root.mkdir(parents=True, exist_ok=True)
    if worker is None:
        worker = root / "synthetic application 中文" / "qcs-worker.exe"
        worker.parent.mkdir()
        worker.write_bytes(b"SYNTHETIC VALIDATOR WORKER, NOT AN EXECUTABLE")
    application = Path(application_root or worker.parent).resolve()
    for name in ("qcs-gui", "qcs-service", "qcs-cli"):
        path = worker.parent / (name + worker.suffix)
        if not path.exists():
            path.write_bytes(b"SYNTHETIC VALIDATOR PROGRAM, NOT AN EXECUTABLE " + name.encode())
    fixtures = Path(__file__).resolve().parents[2] / "tests/runtime/fixtures"
    references = json.loads((fixtures / "vectors.json").read_text(encoding="utf-8"))["vectors"]
    vectors = []
    for name, modality in (("query", "text"), ("screen-280", "image"), ("speech", "audio")):
        embedding = deepcopy(references[name])
        result = {"modelFingerprint": model_fingerprint, "extractionFingerprint": extraction_fingerprint}
        if modality == "text":
            result["embedding"] = embedding
        else:
            segment = {"modality": modality, "embedding": embedding}
            segment.update({"imageBudget": 280} if modality == "image" else {"startMs": 0, "endMs": 4000})
            result.update({"complete": True, "state": "complete", "error": "", "embedded": True,
                           "embeddingErrors": [], "segments": [segment]})
        raw = root / ("model-" + name + "-reply.json")
        raw.write_text(json.dumps({"id": "model-" + name, "ok": True, "result": result}), encoding="utf-8")
        vectors.append({"name": name, "modality": modality, "dimensions": 768,
                        "modelFingerprint": model_fingerprint, "extractionFingerprint": extraction_fingerprint,
                        "l2Norm": math.sqrt(math.fsum(value * value for value in embedding)), "cosine": 1.0,
                        "embedding": embedding, "expectedEmbedding": deepcopy(embedding),
                        "rawReply": {"path": raw.name, "sha256": hashlib.sha256(raw.read_bytes()).hexdigest()}})
    libraries = []
    for role, filename in (("llama", "llama.dll"), ("mtmd", "mtmd.dll"), ("ggml", "ggml.dll"),
                           ("ggml-base", "ggml-base.dll"), ("cpu-backend", "ggml-cpu-synthetic.dll")):
        library = application / filename
        library.write_bytes(b"SYNTHETIC VALIDATOR LIBRARY " + role.encode())
        libraries.append({"role": role, "path": str(library), "bytes": library.stat().st_size,
                          "sha256": hashlib.sha256(library.read_bytes()).hexdigest()})
    model_directory = root / "synthetic model metadata only"
    artifacts = [
        {"name": "embeddinggemma-2-Q8_0.gguf", "bytes": 309855456, "expectedBytes": 309855456,
         "sha256": "2188ac1deca4b77dffefd603c2776a9d76d9d74ec01841392982ebb840b09135", "present": True},
        {"name": "mmproj-embeddinggemma-2-Q8_0.gguf", "bytes": 554821024, "expectedBytes": 554821024,
         "sha256": "c4a8a52691ecef40618438928bdf9e68379b854e24166f292592353db0aab64f", "present": True},
    ]
    source = Path(__file__).resolve().parents[2]
    diagnostics = {"buildVariant": "windows-x64-cpu", "requestedBackend": "auto",
        "packagedBackend": "cpu", "selectedBackend": "cpu", "deviceName": "CPU",
        "deviceDescription": "Synthetic CPU fixture", "gpuSelected": False,
        "fallbackReason": "", "loaded": True}
    report = {"syntheticValidatorFixture": True, "passed": True, "releaseComplete": True,
            "commit": "1" * 40, "packageSha256": hashlib.sha256(b"SYNTHETIC VALIDATOR PACKAGE").hexdigest(),
            "variant": "windows-x64-cpu", "buildIdentity": {
                "schemaVersion": 1, "version": "0.1.0", "variant": "windows-x64-cpu", "backend": "cpu",
                "configuration": "Release", "candidateCommit": "1" * 40, "workingTreeModified": False,
                "attempt": 1, "dependencyLockSha256": hashlib.sha256(
                    (source / "dependencies.lock.json").read_bytes()).hexdigest(),
                "variantsSha256": hashlib.sha256((source / "config/build-variants.json").read_bytes()).hexdigest()},
            "backendVerification": {
                "cpu": {"passed": True, "diagnostics": {**diagnostics, "requestedBackend": "cpu"}},
                "strictGpu": {"passed": True, "hardwareTested": False, "reason": "No packaged GPU backend",
                    "diagnostics": {**diagnostics, "requestedBackend": "gpu", "loaded": False,
                        "selectedBackend": ""}}},
            "verificationScope": "release", "realModelRequired": True,
            "applicationRoot": str(application), "licenseMaterials": package_license_fixture(root, application),
            "modelSmoke": {"passed": True, "dimensions": 768, "minimumCosine": 0.999,
                "modelDirectory": str(model_directory),
                "modelVerification": {"valid": True, "directory": str(model_directory), "artifacts": artifacts},
                "referenceHashes": {name: hashlib.sha256((fixtures / name).read_bytes()).hexdigest()
                                    for name in ("screen.png", "speech.wav", "vectors.json")},
                "vectors": vectors,
                "runtime": {"linkage": "shared-backend-dispatch", "architecture": "amd64",
                    "cpuBaselineForced": False, "cpuDispatch": {"x64": True, "arm64": False},
                    "diagnostics": diagnostics,
                    "workerExecutable": {"path": str(worker.resolve()), "bytes": worker.stat().st_size,
                                         "sha256": hashlib.sha256(worker.read_bytes()).hexdigest()},
                    "libraries": libraries},
                "serviceSemantic": {"semantic": True, "warning": "", "hits": [{"path": "synthetic-notes.txt"}]}}}
    write_program_inventory(root, report)
    return report


class PackageReleaseEvidenceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="qcs-package-evidence-中文 ",
            dir=os.environ.get("QCOPILOTS_SEARCH_TEST_ROOT") or None)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.doc = package_release_fixture(self.root)

    def inspect(self, doc=..., *, platform_id="windows-x64-cpu", **fingerprints):
        return evidence.validate_package_release(self.doc if doc is ... else doc, self.root,
            model_fingerprint=fingerprints.get("model", "synthetic-model-fingerprint"),
            extraction_fingerprint=fingerprints.get("extraction", "synthetic-extraction-fingerprint"),
            platform_id=platform_id)

    def change_raw(self, vector, change):
        path = self.root / vector["rawReply"]["path"]
        raw = json.loads(path.read_text(encoding="utf-8"))
        change(raw)
        path.write_text(json.dumps(raw), encoding="utf-8")
        vector["rawReply"]["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()

    def synchronize_embedding(self, vector):
        def update(raw):
            if vector["modality"] == "text":
                raw["result"]["embedding"] = vector["embedding"]
            else:
                raw["result"]["segments"][0]["embedding"] = vector["embedding"]
        self.change_raw(vector, update)

    def inspect_inventory_change(self, change):
        """Rehash only an explicitly altered synthetic inventory, then restore it."""
        changed = deepcopy(self.doc)
        path = self.root / changed["programInventory"]["path"]
        original = path.read_bytes()
        inventory = json.loads(original)
        change(inventory)
        try:
            path.write_text(json.dumps(inventory), encoding="utf-8")
            changed["programInventory"]["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
            return self.inspect(changed)
        finally:
            path.write_bytes(original)

    def test_complete_synthetic_golden_reports_validate_without_running_models(self):
        self.assertEqual(self.inspect(), [])

    def test_installed_program_inventory_is_required_and_independent(self):
        self.assertIn("installed-programs.json", evidence.package_evidence_paths(self.doc, self.root))
        changed = deepcopy(self.doc)
        changed.pop("programInventory")
        self.assertTrue(self.inspect(changed))
        for value in (None, [], {}, True, {"stable": True}):
            with self.subTest(value=value):
                changed = deepcopy(self.doc)
                changed["programInventory"] = value
                self.assertTrue(self.inspect(changed))
        (self.root / self.doc["programInventory"]["path"]).unlink()
        self.assertTrue(self.inspect())

    def test_installed_program_inventory_must_be_byte_bound(self):
        changed = deepcopy(self.doc)
        changed["programInventory"]["sha256"] = "0" * 64
        self.assertTrue(self.inspect(changed))
        inventory = self.root / self.doc["programInventory"]["path"]
        inventory.write_bytes(inventory.read_bytes() + b"\n")
        self.assertTrue(self.inspect())

    def test_installed_program_inventory_requires_strict_stability(self):
        for value in (None, False, 0, 1, "true", [], {}):
            with self.subTest(value=value):
                changed = deepcopy(self.doc)
                changed["programInventory"]["stable"] = value
                self.assertTrue(self.inspect(changed))
        self.doc["programInventory"].pop("stable")
        self.assertTrue(self.inspect())

    def test_installed_program_inventory_paths_cannot_escape_or_alias_report_root(self):
        for value in ("../installed-programs.json", str(self.root / "installed-programs.json"),
                      "nested/../../installed-programs.json", "./installed-programs.json",
                      "nested\\installed-programs.json", "installed-programs.json:stream"):
            with self.subTest(value=value):
                changed = deepcopy(self.doc)
                changed["programInventory"]["path"] = value
                self.assertTrue(self.inspect(changed))

    def test_rehashed_inventory_must_match_candidate_variant_package_and_application(self):
        for field, value in (("schemaVersion", 2), ("kind", "other-programs"),
                             ("candidateCommit", "2" * 40), ("variant", "linux-x64-cpu"),
                             ("packageSha256", "0" * 64),
                             ("applicationRoot", str(self.root / "other application"))):
            with self.subTest(field=field):
                self.assertTrue(self.inspect_inventory_change(lambda inventory: inventory.__setitem__(field, value)))

    def test_package_hash_is_required_and_checked_against_external_bytes(self):
        self.assertEqual(evidence.validate_package_release(self.doc, self.root,
            package_sha256=self.doc["packageSha256"]), [])
        self.assertTrue(evidence.validate_package_release(self.doc, self.root, package_sha256="0" * 64))
        for value in (None, "", "not-a-digest", "0" * 64):
            with self.subTest(value=value):
                changed = deepcopy(self.doc)
                changed["packageSha256"] = value
                self.assertTrue(self.inspect(changed))

    def test_installed_program_inventory_requires_exactly_four_programs(self):
        for name in ("qcs-gui", "qcs-service", "qcs-worker", "qcs-cli"):
            with self.subTest(missing=name):
                self.assertTrue(self.inspect_inventory_change(lambda inventory: inventory["programs"].pop(name)))
        self.assertTrue(self.inspect_inventory_change(lambda inventory: inventory["programs"].__setitem__(
            "unexpected-program", deepcopy(inventory["programs"]["qcs-cli"]))))
        for value in (None, [], "four programs", {}):
            with self.subTest(programs=value):
                self.assertTrue(self.inspect_inventory_change(lambda inventory: inventory.__setitem__("programs", value)))

    def test_installed_program_records_require_relative_paths_sizes_and_hashes(self):
        invalid = (("path", "../qcs-cli.exe"), ("path", "/qcs-cli.exe"),
                   ("path", "C:/qcs-cli.exe"), ("path", "bin\\qcs-cli.exe"),
                   ("path", "./qcs-cli.exe"), ("path", "bin/../qcs-cli.exe"),
                   ("path", "qcs-cli.exe:stream"), ("bytes", 0), ("bytes", True),
                   ("bytes", 1.5), ("sha256", "not-a-digest"))
        for field, value in invalid:
            with self.subTest(field=field, value=value):
                self.assertTrue(self.inspect_inventory_change(
                    lambda inventory: inventory["programs"]["qcs-cli"].__setitem__(field, value)))
        self.assertTrue(self.inspect_inventory_change(lambda inventory: inventory["programs"]["qcs-cli"].update(
            inventory["programs"]["qcs-worker"])))

    def test_runtime_worker_must_match_independent_inventory_path_size_and_hash(self):
        worker = self.doc["modelSmoke"]["runtime"]["workerExecutable"]
        for field, value in (("path", str(Path(self.doc["applicationRoot"]) / "other/qcs-worker.exe")),
                             ("bytes", worker["bytes"] + 1), ("sha256", "0" * 64)):
            with self.subTest(field=field):
                changed = deepcopy(self.doc)
                changed["modelSmoke"]["runtime"]["workerExecutable"][field] = value
                self.assertTrue(self.inspect(changed))
                self.assertTrue(self.inspect_inventory_change(lambda inventory: inventory["programs"]["qcs-worker"].__setitem__(
                    field, "other/qcs-worker.exe" if field == "path" else value)))

    def test_preserved_inventory_does_not_require_original_installation_to_exist(self):
        inventory = json.loads((self.root / "installed-programs.json").read_text(encoding="utf-8"))
        application = Path(self.doc["applicationRoot"])
        for record in inventory["programs"].values():
            (application / record["path"]).unlink()
        self.assertEqual(self.inspect(), [])

    def test_linux_and_macos_accept_deployed_bytes_distinct_from_raw_build(self):
        build_worker = self.root / "synthetic-raw-build-worker"
        build_worker.write_bytes(b"SYNTHETIC RAW BUILD BEFORE RPATH STRIP OR SIGNING")
        build_hash = hashlib.sha256(build_worker.read_bytes()).hexdigest()
        for variant, backend, architecture in (("linux-x64-cpu", "cpu", "x86_64"),
                                                ("macos-arm64-metal", "metal", "arm64")):
            with self.subTest(variant=variant):
                changed = deepcopy(self.doc)
                runtime = changed["modelSmoke"]["runtime"]
                changed["variant"] = variant
                changed["buildIdentity"].update({"variant": variant, "backend": backend})
                runtime.update({"architecture": architecture,
                    "cpuDispatch": {"x64": architecture == "x86_64", "arm64": False}})
                for diagnostics in (runtime["diagnostics"], changed["backendVerification"]["cpu"]["diagnostics"],
                                    changed["backendVerification"]["strictGpu"]["diagnostics"]):
                    diagnostics.update({"buildVariant": variant, "packagedBackend": backend})
                if backend != "cpu":
                    runtime["diagnostics"]["fallbackReason"] = "No GPU in synthetic deployment fixture"
                self.assertNotEqual(build_hash, runtime["workerExecutable"]["sha256"])
                use_posix_program_fixture(changed)
                write_program_inventory(self.root, changed)
                self.assertEqual(self.inspect(changed, platform_id=variant), [])

    def test_apple_silicon_uses_shared_metal_libraries_without_x64_cpu_dispatch(self):
        runtime = self.doc["modelSmoke"]["runtime"]
        runtime.update({"architecture": "arm64", "cpuDispatch": {"x64": False, "arm64": False}})
        self.doc["variant"] = "macos-arm64-metal"
        self.doc["buildIdentity"].update({"variant": "macos-arm64-metal", "backend": "metal"})
        for diagnostics in (runtime["diagnostics"], self.doc["backendVerification"]["cpu"]["diagnostics"],
                            self.doc["backendVerification"]["strictGpu"]["diagnostics"]):
            diagnostics.update({"buildVariant": "macos-arm64-metal", "packagedBackend": "metal"})
        runtime["diagnostics"]["fallbackReason"] = "No compatible Metal device in synthetic fixture"
        self.doc["backendVerification"]["strictGpu"]["reason"] = runtime["diagnostics"]["fallbackReason"]
        use_posix_program_fixture(self.doc)
        write_program_inventory(self.root, self.doc)
        self.assertEqual(self.inspect(platform_id="macos-arm64-metal"), [])
        for dispatch in ({"x64": True, "arm64": False}, {"x64": False, "arm64": True}):
            with self.subTest(dispatch=dispatch):
                runtime["cpuDispatch"] = dispatch
                self.assertIn("Package CPU dispatch selection differs from its architecture",
                              self.inspect(platform_id="macos-arm64-metal"))

    def test_cpu_dispatch_requires_exact_boolean_architecture_mapping(self):
        invalid = (None, True, False, [], {}, {"x64": True}, {"arm64": False},
                   {"x64": True, "arm64": False, "other": False},
                   {"x64": 1, "arm64": False}, {"x64": True, "arm64": 0},
                   {"x64": "true", "arm64": False}, {"x64": False, "arm64": False},
                   {"x64": True, "arm64": True})
        for value in invalid:
            with self.subTest(value=value):
                changed = deepcopy(self.doc)
                changed["modelSmoke"]["runtime"]["cpuDispatch"] = value
                self.assertIn("Package CPU dispatch selection differs from its architecture", self.inspect(changed))
        self.doc["modelSmoke"]["runtime"].pop("cpuDispatch")
        self.assertIn("Package CPU dispatch selection differs from its architecture", self.inspect())

    def test_real_runtime_report_producer_matches_release_validator_contract(self):
        producer_spec = importlib.util.spec_from_file_location(
            "qcs_package_producer_contract",
            Path(__file__).resolve().parents[2] / "tools/qcs-verify-package.py")
        producer = importlib.util.module_from_spec(producer_spec)
        producer_spec.loader.exec_module(producer)
        cases = (("amd64", "win32", "windows-x64-cpu", "cpu"),
                 ("x86_64", "linux", "linux-x64-cpu", "cpu"),
                 ("arm64", "darwin", "macos-arm64-metal", "metal"),
                 ("aarch64", "darwin", "macos-arm64-metal", "metal"))
        for architecture, host, variant, backend in cases:
            with self.subTest(architecture=architecture, variant=variant):
                changed = deepcopy(self.doc)
                runtime = changed["modelSmoke"]["runtime"]
                changed["variant"] = variant
                changed["buildIdentity"].update({"variant": variant, "backend": backend})
                for diagnostics in (runtime["diagnostics"], changed["backendVerification"]["cpu"]["diagnostics"],
                                    changed["backendVerification"]["strictGpu"]["diagnostics"]):
                    diagnostics.update({"buildVariant": variant, "packagedBackend": backend})
                if backend != "cpu":
                    runtime["diagnostics"]["fallbackReason"] = "No GPU in synthetic producer contract test"
                if host != "win32":
                    use_posix_program_fixture(changed)
                application = Path(changed["applicationRoot"])
                libraries = [item["path"] for item in runtime["libraries"]]
                with mock.patch.object(producer.platform, "machine", return_value=architecture), \
                        mock.patch.object(producer.sys, "platform", host), \
                        mock.patch.object(producer, "loaded_libraries", return_value=libraries):
                    changed["modelSmoke"]["runtime"] = producer.inference_runtime_origins(
                        object(), Path(runtime["workerExecutable"]["path"]), application,
                        diagnostics=runtime["diagnostics"])
                write_program_inventory(self.root, changed)
                self.assertEqual(self.inspect(changed, platform_id=variant), [])

    def test_build_variant_and_backend_diagnostics_are_required(self):
        for field in ("variant", "buildIdentity", "backendVerification"):
            with self.subTest(field=field):
                changed = deepcopy(self.doc)
                changed.pop(field)
                self.assertTrue(self.inspect(changed))
        changed = deepcopy(self.doc)
        changed["modelSmoke"]["runtime"].pop("diagnostics")
        self.assertTrue(self.inspect(changed))
        self.doc["modelSmoke"]["runtime"]["diagnostics"]["buildVariant"] = "windows-x64-vulkan"
        self.assertTrue(self.inspect())

    def test_strict_gpu_cannot_report_a_cpu_fallback_as_hardware_success(self):
        strict = self.doc["backendVerification"]["strictGpu"]
        strict.update({"hardwareTested": True, "reason": ""})
        strict["diagnostics"].update({"loaded": True, "selectedBackend": "cpu", "gpuSelected": False})
        self.assertTrue(self.inspect())

    def test_partial_or_unrequired_verification_is_not_release_evidence(self):
        for key, value in (("passed", False), ("releaseComplete", False), ("verificationScope", "keyword-only"),
                           ("realModelRequired", False)):
            with self.subTest(key=key):
                changed = deepcopy(self.doc)
                changed[key] = value
                self.assertTrue(self.inspect(changed))

    def test_missing_or_false_model_smoke_is_rejected(self):
        for value in (None, {}, {"passed": False}):
            with self.subTest(value=value):
                changed = deepcopy(self.doc)
                changed["modelSmoke"] = value
                self.assertTrue(self.inspect(changed))

    def test_both_locked_models_are_required_without_duplicates(self):
        for artifacts in ([], self.doc["modelSmoke"]["modelVerification"]["artifacts"][:1],
                          [self.doc["modelSmoke"]["modelVerification"]["artifacts"][0]] * 2):
            with self.subTest(count=len(artifacts)):
                changed = deepcopy(self.doc)
                changed["modelSmoke"]["modelVerification"]["artifacts"] = artifacts
                self.assertTrue(self.inspect(changed))

    def test_model_manifest_hash_size_presence_and_directory_must_agree(self):
        for key, value in (("sha256", "0" * 64), ("bytes", 1), ("expectedBytes", 1), ("present", False)):
            with self.subTest(key=key):
                changed = deepcopy(self.doc)
                changed["modelSmoke"]["modelVerification"]["artifacts"][0][key] = value
                self.assertTrue(self.inspect(changed))
        self.doc["modelSmoke"]["modelVerification"]["directory"] += "-different"
        self.assertTrue(self.inspect())

    def test_exact_three_golden_names_and_modalities_are_required(self):
        original = self.doc["modelSmoke"]["vectors"]
        for vectors in (original[:2], [original[0], original[0], original[2]]):
            with self.subTest(names=[v["name"] for v in vectors]):
                changed = deepcopy(self.doc)
                changed["modelSmoke"]["vectors"] = vectors
                self.assertTrue(self.inspect(changed))
        self.doc["modelSmoke"]["vectors"][1]["modality"] = "text"
        self.assertTrue(self.inspect())

    def test_vectors_require_768_finite_numeric_components(self):
        for values in ([0.0] * 767, [0.0] * 769, [float("nan")] * 768,
                       [float("inf")] * 768, [True] * 768):
            with self.subTest(first=values[0], count=len(values)):
                changed = deepcopy(self.doc)
                changed["modelSmoke"]["vectors"][0]["embedding"] = values
                self.assertTrue(self.inspect(changed))

    def test_norm_is_recomputed_from_matching_raw_embedding(self):
        vector = self.doc["modelSmoke"]["vectors"][0]
        vector["embedding"] = [value * 2 for value in vector["embedding"]]
        self.synchronize_embedding(vector)
        self.assertTrue(self.inspect())

    def test_cosine_is_recomputed_even_when_reported_value_claims_success(self):
        vector = self.doc["modelSmoke"]["vectors"][0]
        vector["embedding"] = [-value for value in vector["embedding"]]
        self.synchronize_embedding(vector)
        self.assertTrue(self.inspect())

    def test_expected_embedding_cannot_be_replaced_along_with_actual(self):
        vector = self.doc["modelSmoke"]["vectors"][0]
        replacement = deepcopy(self.doc["modelSmoke"]["vectors"][2]["embedding"])
        vector.update({"embedding": replacement, "expectedEmbedding": deepcopy(replacement),
                       "l2Norm": math.sqrt(math.fsum(value * value for value in replacement))})
        self.synchronize_embedding(vector)
        self.assertTrue(self.inspect())

    def test_reported_norm_cosine_and_reference_hash_cannot_be_forged(self):
        for key, value in (("l2Norm", 0.5), ("cosine", 0.1), ("dimensions", 3)):
            with self.subTest(key=key):
                changed = deepcopy(self.doc)
                changed["modelSmoke"]["vectors"][0][key] = value
                self.assertTrue(self.inspect(changed))
        self.doc["modelSmoke"]["referenceHashes"]["vectors.json"] = "0" * 64
        self.assertTrue(self.inspect())

    def test_raw_reply_file_and_hash_are_required(self):
        vector = self.doc["modelSmoke"]["vectors"][0]
        changed = deepcopy(self.doc)
        changed["modelSmoke"]["vectors"][0]["rawReply"]["sha256"] = "0" * 64
        self.assertTrue(self.inspect(changed))
        (self.root / vector["rawReply"]["path"]).unlink()
        self.assertTrue(self.inspect())

    def test_raw_reply_paths_cannot_escape_the_report_root(self):
        for path in ("../outside.json", str(self.root / "absolute.json"), "nested/../../outside.json"):
            with self.subTest(path=path):
                changed = deepcopy(self.doc)
                changed["modelSmoke"]["vectors"][0]["rawReply"]["path"] = path
                self.assertTrue(self.inspect(changed))

    def test_raw_reply_embedding_must_equal_the_vector_record(self):
        vector = self.doc["modelSmoke"]["vectors"][0]
        self.change_raw(vector, lambda raw: raw["result"]["embedding"].__setitem__(0, 0.0))
        self.assertTrue(self.inspect())

    def test_raw_reply_success_id_and_unique_media_segment_are_checked(self):
        vector = self.doc["modelSmoke"]["vectors"][1]
        for field, value in (("ok", False), ("id", "another-request")):
            with self.subTest(field=field):
                original = json.loads((self.root / vector["rawReply"]["path"]).read_text(encoding="utf-8"))
                self.change_raw(vector, lambda raw: raw.__setitem__(field, value))
                self.assertTrue(self.inspect())
                self.change_raw(vector, lambda raw: raw.update(original))
        self.change_raw(vector, lambda raw: raw["result"]["segments"].append(deepcopy(raw["result"]["segments"][0])))
        self.assertTrue(self.inspect())

    def test_media_partial_state_or_error_cannot_hide_behind_complete_flags(self):
        for vector in self.doc["modelSmoke"]["vectors"][1:]:
            original = json.loads((self.root / vector["rawReply"]["path"]).read_text(encoding="utf-8"))
            for field, value in (("state", "partial"), ("state", None), ("error", "synthetic extraction error")):
                with self.subTest(name=vector["name"], field=field, value=value):
                    try:
                        self.change_raw(vector, lambda raw: raw["result"].__setitem__(field, value))
                        self.assertTrue(self.inspect())
                    finally:
                        self.change_raw(vector, lambda raw: raw.update(original))

    def test_media_reply_cannot_contain_an_extra_different_modality_segment(self):
        for vector in self.doc["modelSmoke"]["vectors"][1:]:
            with self.subTest(name=vector["name"]):
                original = json.loads((self.root / vector["rawReply"]["path"]).read_text(encoding="utf-8"))
                try:
                    self.change_raw(vector, lambda raw: raw["result"]["segments"].append(
                        {"modality": "text", "text": "unexpected synthetic extra segment"}))
                    self.assertTrue(self.inspect())
                finally:
                    self.change_raw(vector, lambda raw: raw.update(original))

    def test_media_budget_and_audio_window_must_match_reference_input(self):
        for index, field, value in ((1, "imageBudget", 140), (1, "imageBudget", None),
                                    (2, "startMs", 1), (2, "endMs", 4001), (2, "endMs", None)):
            vector = self.doc["modelSmoke"]["vectors"][index]
            with self.subTest(name=vector["name"], field=field, value=value):
                original = json.loads((self.root / vector["rawReply"]["path"]).read_text(encoding="utf-8"))
                try:
                    self.change_raw(vector, lambda raw: raw["result"]["segments"][0].__setitem__(field, value))
                    self.assertTrue(self.inspect())
                finally:
                    self.change_raw(vector, lambda raw: raw.update(original))

    def test_vector_and_raw_reply_fingerprints_match_the_bound_candidate(self):
        for key in ("modelFingerprint", "extractionFingerprint"):
            with self.subTest(key=key):
                changed = deepcopy(self.doc)
                changed["modelSmoke"]["vectors"][1][key] = "synthetic-wrong-fingerprint"
                self.assertTrue(self.inspect(changed))
        self.assertTrue(self.inspect(model="synthetic-other-candidate"))
        self.assertTrue(self.inspect(extraction="synthetic-other-candidate"))
        vector = self.doc["modelSmoke"]["vectors"][0]
        self.change_raw(vector, lambda raw: raw["result"].__setitem__("modelFingerprint", "wrong-raw-fingerprint"))
        self.assertTrue(self.inspect())

    def test_shared_runtime_requires_all_five_library_roles(self):
        self.doc["modelSmoke"]["runtime"]["libraries"].pop()
        self.assertTrue(self.inspect())

    def test_x64_cpu_dispatch_cannot_be_omitted_or_reported_as_static(self):
        for field, value in (("linkage", "static"), ("cpuDispatch", {"x64": False, "arm64": False})):
            with self.subTest(field=field):
                changed = deepcopy(self.doc)
                changed["modelSmoke"]["runtime"][field] = value
                self.assertTrue(self.inspect(changed))

    def test_runtime_paths_hashes_and_sizes_are_required(self):
        self.assertTrue(evidence.validate_package_release(self.doc, self.root, worker_sha256="0" * 64))
        for key, value in (("path", str(self.root / "outside-application.dll")), ("sha256", "not-a-hash"), ("bytes", 0)):
            with self.subTest(key=key):
                changed = deepcopy(self.doc)
                changed["modelSmoke"]["runtime"]["libraries"][0][key] = value
                self.assertTrue(self.inspect(changed))
        self.doc["modelSmoke"]["runtime"]["workerExecutable"]["path"] = str(self.root / "outside-worker.exe")
        self.assertTrue(self.inspect())

    def test_missing_licenses_or_nonsemantic_service_results_are_rejected(self):
        changed = deepcopy(self.doc)
        changed["licenseMaterials"]["passed"] = False
        self.assertTrue(self.inspect(changed))
        for result in ({}, {"semantic": False, "hits": [{}]}, {"semantic": True, "hits": []},
                       {"semantic": True, "hits": [{}], "warning": "synthetic degraded result"}):
            with self.subTest(result=result):
                changed = deepcopy(self.doc)
                changed["modelSmoke"]["serviceSemantic"] = result
                self.assertTrue(self.inspect(changed))

    def test_license_manifest_and_referenced_files_must_exist(self):
        materials = self.doc["licenseMaterials"]
        manifest = self.root / materials["manifest"]
        (manifest.parent / "llama/LICENSE").unlink()
        self.assertTrue(self.inspect())
        manifest.unlink()
        self.assertTrue(self.inspect())

    def test_license_manifest_hash_and_material_content_are_verified(self):
        changed = deepcopy(self.doc)
        changed["licenseMaterials"]["manifestSha256"] = "0" * 64
        self.assertTrue(self.inspect(changed))
        manifest = self.root / self.doc["licenseMaterials"]["manifest"]
        (manifest.parent / "llama/LICENSE").write_text("Changed synthetic license bytes", encoding="utf-8")
        self.assertTrue(self.inspect())

    def test_license_coverage_is_recomputed_even_when_manifest_hash_is_updated(self):
        materials = self.doc["licenseMaterials"]
        manifest = self.root / materials["manifest"]
        value = json.loads(manifest.read_text(encoding="utf-8"))
        value["moduleCoverage"]["qtpdf"] = []
        manifest.write_text(json.dumps(value), encoding="utf-8")
        materials["manifestSha256"] = hashlib.sha256(manifest.read_bytes()).hexdigest()
        self.assertTrue(self.inspect())

    def test_required_vendor_headers_cannot_be_removed_from_files_and_manifest(self):
        manifest = self.root / self.doc["licenseMaterials"]["manifest"]
        original_manifest = manifest.read_bytes()
        for name in ("llama/vendor/stb/stb_image.h", "llama/vendor/miniaudio/miniaudio.h",
                     "llama/vendor/sheredom/subprocess.h"):
            with self.subTest(name=name):
                header = manifest.parent / name
                original_header = header.read_bytes()
                try:
                    header.unlink()
                    value = json.loads(original_manifest)
                    value["files"] = [entry for entry in value["files"] if entry["path"] != name]
                    manifest.write_text(json.dumps(value), encoding="utf-8")
                    changed = deepcopy(self.doc)
                    changed["licenseMaterials"]["files"] = len(value["files"])
                    changed["licenseMaterials"]["manifestSha256"] = hashlib.sha256(manifest.read_bytes()).hexdigest()
                    self.assertTrue(any("Required vendored license header" in error for error in self.inspect(changed)))
                finally:
                    header.write_bytes(original_header)
                    manifest.write_bytes(original_manifest)

    def test_sdk_license_info_in_files_requires_corresponding_license_coverage(self):
        materials = self.doc["licenseMaterials"]
        manifest = self.root / materials["manifest"]
        name = "qt/sbom/qtbase-6.12.0.spdx.json"
        sbom = manifest.parent / name
        document = json.loads(sbom.read_text(encoding="utf-8"))
        document["packages"][0]["licenseInfoInFiles"] = ["Apache-2.0"]
        sbom.write_text(json.dumps(document), encoding="utf-8")
        value = json.loads(manifest.read_text(encoding="utf-8"))
        record = next(entry for entry in value["files"] if entry["path"] == name)
        record.update({"bytes": sbom.stat().st_size, "sha256": hashlib.sha256(sbom.read_bytes()).hexdigest()})
        manifest.write_text(json.dumps(value), encoding="utf-8")
        materials["manifestSha256"] = hashlib.sha256(manifest.read_bytes()).hexdigest()
        self.assertTrue(any("SDK license coverage differs" in error for error in self.inspect()))

    def test_malformed_external_shapes_return_errors(self):
        for doc in (None, [], "not-a-report", {"modelSmoke": []}, {"passed": True}):
            with self.subTest(doc=doc):
                self.assertTrue(self.inspect(doc))


class NsisContractEvidenceTests(unittest.TestCase):
    def fixture(self):
        identifier = "__main__.WorkflowTests.test_nsis_compiles_recursive_payload_beyond_max_path"
        raw = f"{identifier.rsplit('.', 1)[-1]} ({identifier}) ... ok\n\nRan 1 test in 0.001s\n\nOK\n"
        summary = {"schemaVersion": 1, "passed": True, "exitCode": 0, "log": "nsis-contract.log",
                   "counts": {"run": 1, "passed": 1, "failed": 0, "skipped": 0, "notApplicable": 0},
                   "cases": [{"case": identifier, "status": "ok"}], "errors": []}
        return summary, raw

    def test_exact_native_case_passes_without_unrelated_cleanup_cases(self):
        self.assertEqual(evidence.validate_nsis_contract(*self.fixture()), [])

    def test_skip_failure_wrong_case_or_missing_raw_output_cannot_pass(self):
        summary, raw = self.fixture()
        for output in ("", "Ran 1 test in 0.001s\n\nOK\n", raw.replace("... ok", "... skipped 'NSIS unavailable'"),
                       raw.replace("... ok", "... FAIL"), raw.replace("test_nsis_compiles_recursive_payload_beyond_max_path", "test_other"),
                       raw + "[This part of the test output was removed since it exceeds the threshold of 1024 bytes.]\n"):
            with self.subTest(output=output):
                self.assertTrue(evidence.validate_nsis_contract(summary, output))

    def test_summary_false_flags_counts_cases_exit_or_log_are_rejected(self):
        for key, value in (("passed", False), ("passed", 1), ("exitCode", False), ("exitCode", 1),
                           ("counts", {}), ("cases", []), ("errors", ["failed"]), ("log", "other.log")):
            summary, raw = self.fixture()
            summary[key] = value
            with self.subTest(key=key, value=value):
                self.assertTrue(evidence.validate_nsis_contract(summary, raw))

    def test_extra_duplicate_case_cannot_satisfy_single_native_case_contract(self):
        summary, raw = self.fixture()
        line = raw.splitlines()[0]
        altered = raw.replace("Ran 1 test", "Ran 2 tests").replace(line, line + "\n" + line)
        self.assertTrue(evidence.validate_nsis_contract(summary, altered))


def semantic_fixture():
    """Synthetic functional-report contract, never evidence of model execution."""
    source = Path(__file__).resolve().parents[2]
    fixtures = (
        ("text", "txt", "text", {"text": 1}), ("pdf", "pdf", "pdf", {"text": 1}),
        ("code", "py", "code", {"text": 1}),
        ("illustration", "png", "image", {"image": 1}), ("photo", "jpg", "image", {"image": 1}),
        ("ocr-en", "png", "ocr", {"image": 1}), ("ocr-zh", "png", "ocr", {"image": 1}),
        ("speech", "wav", "audio", {"audio": 1}), ("environment", "ogg", "audio", {"audio": 1}),
        ("tone", "wav", "audio", {"audio": 1}), ("video-generated", "avi", "video", {"image": 2}),
        ("video-natural", "webm", "video", {"audio": 2, "image": 8}),
    )
    files = [{"id": name, "file": f"{name}/source.{extension}", "modality": modality,
              "code": name == "code", "segments": sum(modalities.values()),
              "extractedModalities": modalities,
              "extractedTextCharacters": 100 if name in ("text", "pdf", "code", "ocr-en", "ocr-zh") else 0,
              "sha256": hashlib.sha256(("SYNTHETIC FILE " + name).encode()).hexdigest()}
             for name, extension, modality, modalities in fixtures]
    return {"schema": 2, "test": "multimodal-functional", "mode": "functional",
            "syntheticValidatorFixture": True, "passed": True, "realModel": True,
            "persisted": True, "reloaded": True, "fixtures": 12, "completedFiles": 12,
            "queryCount": 24, "completedQueries": 24,
            "actualEmbeddedSegments": sum(item["segments"] for item in files),
            "modelFingerprint": "synthetic-model-fingerprint",
            "extractionFingerprint": "synthetic-extraction-fingerprint",
            "fixtureManifestSha256": hashlib.sha256(
                (source / "tests/acceptance/fixtures/assets.json").read_bytes()).hexdigest(),
            "generatorSha256": hashlib.sha256(
                (source / "tests/semantic/semantic_acceptance.cpp").read_bytes()).hexdigest(),
            "files": files,
            "queries": [{"id": f"{item['id']}-{language}", "fixtureId": item["id"],
                "language": language, "query": f"SYNTHETIC {language} query for {item['id']}",
                "source": "semantic", "returnedFile": item["file"],
                "locationPassed": True, "passed": True}
                for item in files for language in ("en", "zh")]}


class SemanticFunctionalEvidenceTests(unittest.TestCase):
    def test_complete_functional_contract_validates_without_running_a_model(self):
        self.assertEqual(evidence.validate_semantic(semantic_fixture()), [])

    def test_extracted_modalities_require_a_nonempty_count_mapping(self):
        doc = semantic_fixture()
        del doc["files"][0]["extractedModalities"]
        self.assertTrue(evidence.validate_semantic(doc))
        for value in (None, {}, [], ["text"], "text", 1, True):
            with self.subTest(value=value):
                doc = semantic_fixture()
                doc["files"][0]["extractedModalities"] = value
                self.assertTrue(evidence.validate_semantic(doc))

    def test_extracted_modality_counts_are_positive_integers_not_booleans(self):
        for value in (0, -1, True, False, 1.0, "1", None, {}, []):
            with self.subTest(value=value):
                doc = semantic_fixture()
                doc["files"][0]["extractedModalities"] = {"text": value}
                self.assertTrue(evidence.validate_semantic(doc))

    def test_extracted_modality_names_are_known_encoding_channels(self):
        for name in ("unknown", "pdf", "ocr", "", "Text", " text", 1):
            with self.subTest(name=name):
                doc = semantic_fixture()
                doc["files"][0]["extractedModalities"] = {name: 1}
                self.assertTrue(evidence.validate_semantic(doc))

    def test_extracted_modality_counts_bind_every_embedded_segment(self):
        for modalities in ({"image": 8}, {"audio": 2}, {"audio": 2, "image": 7},
                           {"audio": 2, "image": 8, "text": 1},
                           {"audio": 2, "image": 7, "unknown": 1}):
            with self.subTest(modalities=modalities):
                doc = semantic_fixture()
                doc["files"][-1]["extractedModalities"] = modalities
                self.assertTrue(evidence.validate_semantic(doc))

    def test_validation_only_or_missing_real_inference_cannot_claim_completion(self):
        for field, value in (("mode", "validate-only"), ("passed", False),
                             ("realModel", False), ("realModel", 1),
                             ("persisted", False), ("reloaded", False)):
            with self.subTest(field=field, value=value):
                doc = semantic_fixture()
                doc[field] = value
                self.assertTrue(evidence.validate_semantic(doc))

    def test_all_file_chains_and_both_query_languages_are_required(self):
        for missing in range(12):
            with self.subTest(missing=missing):
                doc = semantic_fixture()
                doc["files"].pop(missing)
                self.assertTrue(evidence.validate_semantic(doc))
        doc = semantic_fixture()
        doc["queries"].pop()
        self.assertTrue(evidence.validate_semantic(doc))
        doc = semantic_fixture()
        doc["queries"][1] = deepcopy(doc["queries"][0])
        self.assertTrue(evidence.validate_semantic(doc))

    def test_file_identity_and_embedded_segment_totals_cannot_be_forged(self):
        for field, value in (("id", "unknown-fixture"), ("segments", 0),
                             ("sha256", "not-a-sha256"), ("file", "other/source.txt")):
            with self.subTest(field=field):
                doc = semantic_fixture()
                doc["files"][0][field] = value
                self.assertTrue(evidence.validate_semantic(doc))
        doc = semantic_fixture()
        doc["actualEmbeddedSegments"] += 1
        self.assertTrue(evidence.validate_semantic(doc))

    def test_query_must_return_its_isolated_source_through_semantic_search(self):
        for field, value in (("returnedFile", "another/source.txt"), ("source", "keyword"),
                             ("fixtureId", "another-fixture"), ("locationPassed", False),
                             ("passed", False), ("query", "")):
            with self.subTest(field=field):
                doc = semantic_fixture()
                doc["queries"][0][field] = value
                self.assertTrue(evidence.validate_semantic(doc))

    def test_fixture_and_generator_bytes_are_bound_to_the_current_checkout(self):
        for field in ("fixtureManifestSha256", "generatorSha256"):
            with self.subTest(field=field):
                doc = semantic_fixture()
                doc[field] = "0" * 64
                self.assertTrue(evidence.validate_semantic(doc))

    def test_partial_counts_and_missing_fingerprints_are_rejected(self):
        for field, value in (("fixtures", 11), ("completedFiles", 11), ("queryCount", 23),
                             ("completedQueries", 23), ("modelFingerprint", ""),
                             ("extractionFingerprint", "")):
            with self.subTest(field=field):
                doc = semantic_fixture()
                doc[field] = value
                self.assertTrue(evidence.validate_semantic(doc))

    def test_malformed_external_report_returns_errors(self):
        for doc in (None, [], "not-a-report", {"passed": True}):
            with self.subTest(doc=doc):
                self.assertTrue(evidence.validate_semantic(doc))
        for field in ("files", "queries"):
            for value in (None, {}, [None]):
                with self.subTest(field=field, value=value):
                    doc = semantic_fixture()
                    doc[field] = value
                    self.assertTrue(evidence.validate_semantic(doc))


class JsonEvidenceTests(unittest.TestCase):
    def test_nonfinite_json_constants_are_not_accepted(self):
        with tempfile.TemporaryDirectory(prefix="qcs-evidence-中文 ",
                dir=os.environ.get("QCOPILOTS_SEARCH_TEST_ROOT") or None) as temporary:
            path = Path(temporary) / "qcs-fixture.json"
            for constant in ("NaN", "Infinity", "-Infinity"):
                with self.subTest(constant=constant):
                    path.write_text('{"value":' + constant + '}', encoding="utf-8")
                    with self.assertRaises(ValueError):
                        evidence.read_json(path)

    def test_unicode_and_bom_are_portable_from_an_explicit_path(self):
        with tempfile.TemporaryDirectory(prefix="qcs-evidence-中文 ",
                dir=os.environ.get("QCOPILOTS_SEARCH_TEST_ROOT") or None) as temporary:
            path = Path(temporary) / "带空格 fixture.json"
            path.write_text('{"note":"测试证据"}', encoding="utf-8-sig")
            self.assertEqual(evidence.read_json(path), {"note": "测试证据"})


class ManifestEvidenceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="qcs-manifest-中文 ",
            dir=os.environ.get("QCOPILOTS_SEARCH_TEST_ROOT") or None)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "reports" / "evidence 中文.json"
        self.path.parent.mkdir()
        self.payload = b'{"note":"synthetic validator input"}\n'
        self.path.write_bytes(self.payload)
        self.manifest = {
            "schemaVersion": 1, "kind": "package-verification", "variant": "windows-x64-cpu",
            "identity": {"candidateHead": "1" * 40, "checkoutCommit": "2" * 40,
                         "sourceTree": "3" * 40, "workingTreeModified": False,
                         "lockHashes": {name: "4" * 64 for name in evidence.LOCKS}},
            "files": [{"path": self.path.relative_to(self.root).as_posix(),
                       "bytes": len(self.payload), "sha256": hashlib.sha256(self.payload).hexdigest()}],
        }

    def inspect(self):
        result = evidence.verify_manifest(self.root, self.manifest)
        self.assertIsInstance(result, list)
        self.assertTrue(all(isinstance(error, str) for error in result))
        return result

    def test_bound_content_and_clean_source_identity_pass(self):
        self.assertEqual(self.inspect(), [])

    def test_equal_size_content_tampering_is_detected(self):
        # Preserve the size so a length-only integrity check cannot pass.
        self.path.write_bytes(self.payload.replace(b"input", b"other"))
        self.assertTrue(self.inspect())

    def test_wrong_size_is_detected_even_with_correct_digest(self):
        self.manifest["files"][0]["bytes"] += 1
        self.assertTrue(self.inspect())

    def test_missing_report_cannot_reuse_stored_success(self):
        self.path.unlink()
        self.assertTrue(self.inspect())

    def test_empty_or_duplicate_file_records_rejected(self):
        original = deepcopy(self.manifest["files"])
        for files in ([], original * 2):
            self.manifest["files"] = files
            self.assertTrue(self.inspect())

    def test_modified_or_unknown_source_tree_is_not_release_evidence(self):
        for value in (True, None, 0, "false"):
            with self.subTest(value=value):
                self.manifest["identity"]["workingTreeModified"] = value
                self.assertTrue(self.inspect())

    def test_each_dependency_lock_fingerprint_is_required(self):
        original = deepcopy(self.manifest["identity"]["lockHashes"])
        for missing in evidence.LOCKS:
            with self.subTest(missing=missing):
                self.manifest["identity"]["lockHashes"] = deepcopy(original)
                del self.manifest["identity"]["lockHashes"][missing]
                self.assertTrue(self.inspect())

    def test_variant_sdk_and_license_source_files_are_bound_to_source_identity(self):
        required = {"config/build-variants.json", "config/gpu-sdk.lock.json", "config/gpu-license-sources.lock.json"}
        self.assertTrue(required.issubset(evidence.LOCKS))
        expected = {}
        for name in evidence.LOCKS:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            content = ("synthetic identity input: " + name).encode("utf-8")
            path.write_bytes(content)
            expected[name] = hashlib.sha256(content).hexdigest()
        with mock.patch.object(evidence.subprocess, "check_output", side_effect=["a" * 40, "b" * 40, ""]):
            original = evidence.source_identity(self.root)
        self.assertEqual(original["lockHashes"], expected)
        for name in required:
            with self.subTest(path=name):
                (self.root / name).write_bytes(b"changed configuration identity")
                with mock.patch.object(evidence.subprocess, "check_output", side_effect=["a" * 40, "b" * 40, ""]):
                    changed = evidence.source_identity(self.root)
                self.assertNotEqual(changed["lockHashes"][name], original["lockHashes"][name])

    def test_invalid_commit_tree_or_lock_digest_is_rejected(self):
        for field in ("candidateHead", "checkoutCommit", "sourceTree"):
            original = self.manifest["identity"][field]
            self.manifest["identity"][field] = "not-a-commit"
            self.assertTrue(self.inspect())
            self.manifest["identity"][field] = original
        self.manifest["identity"]["lockHashes"][evidence.LOCKS[0]] = "0" * 63
        self.assertTrue(self.inspect())

    def test_path_traversal_absolute_and_drive_paths_are_rejected(self):
        for path in ("../outside.json", "/absolute.json", "C:/absolute.json",
                     "reports/../../outside.json", "reports\\evidence 中文.json",
                     "reports/evidence 中文.json:stream"):
            with self.subTest(path=path):
                self.manifest["files"][0]["path"] = path
                self.assertTrue(self.inspect())
                with self.assertRaises(ValueError):
                    evidence.safe_file(self.root, path)

    def test_noncanonical_relative_aliases_are_rejected(self):
        for path in ("reports/./evidence 中文.json", "reports//evidence 中文.json",
                     "./reports/evidence 中文.json", "reports/evidence 中文.json/"):
            with self.subTest(path=path):
                with self.assertRaises(ValueError):
                    evidence.safe_file(self.root, path)

    def test_symlinks_are_rejected_without_requiring_host_privilege(self):
        # Simulate the filesystem type observation, not the checker or its result.
        original = Path.is_symlink
        target = self.path.resolve()
        with mock.patch.object(Path, "is_symlink", lambda path: path == target or original(path)):
            with self.assertRaises(ValueError):
                evidence.safe_file(self.root, self.path.relative_to(self.root).as_posix())
            self.assertTrue(self.inspect())

    def test_system_ancestor_link_is_outside_the_evidence_boundary(self):
        # macOS temporary roots may themselves live beneath the system /var link.
        # Only links within the selected evidence root can redirect an evidence name.
        original = Path.is_symlink
        ancestor = self.root.parent.parent
        with mock.patch.object(Path, "is_symlink", lambda path: path == ancestor or original(path)):
            self.assertEqual(evidence.safe_file(self.root, self.path.relative_to(self.root).as_posix()),
                             self.path.resolve())

    def test_malformed_manifests_return_errors(self):
        for malformed in (None, [], {"identity": None}, {"identity": [], "files": []},
                          {"identity": deepcopy(self.manifest["identity"]), "files": [None]}):
            with self.subTest(manifest=malformed):
                self.assertTrue(evidence.verify_manifest(self.root, malformed))


class ComputerUseEvidenceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="qcs-cu-validator-中文 ",
            dir=os.environ.get("QCOPILOTS_SEARCH_TEST_ROOT") or None)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        # A tiny synthetic PNG is only a validator fixture, not a captured application.
        self.picture = self.root / "synthetic-validator-fixture.png"
        self.picture.write_bytes(base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jR1cAAAAASUVORK5CYII="))
        self.log = self.root / "synthetic-validator-operations.log"
        self.log.write_text("Synthetic validator fixture. No user operation occurred.\n", encoding="utf-8")
        self.identity = {"candidateHead": "1" * 40, "checkoutCommit": "2" * 40,
                         "sourceTree": "3" * 40, "workingTreeModified": False,
                         "lockHashes": {name: "4" * 64 for name in evidence.LOCKS}}
        self.package = "5" * 64
        self.fingerprint = "qcopilots-search-embedding-v1:" + "6" * 64
        self.executables = {name: {"sha256": f"{n + 1:064x}", "packageMember": name + ".exe"}
                            for n, name in enumerate(("qcs-gui", "qcs-service",
                                                      "qcs-worker", "qcs-cli"))}
        self.doc = {
            "schemaVersion": 1, "kind": "windows-computer-use", "passed": True,
            "identity": deepcopy(self.identity), "commit": self.identity["checkoutCommit"],
            "packageSha256": self.package, "modelFingerprint": self.fingerprint,
            "executables": deepcopy(self.executables),
            "startedAt": "2026-01-01T00:00:00Z", "finishedAt": "2026-01-01T00:30:00Z",
            "scenarios": [{"id": f"CU-{n:02}", "status": "passed",
                           "expected": "Synthetic validator expectation",
                           "actual": "Synthetic validator observation",
                           "actions": ["Synthetic validator action"],
                           "packageSha256": self.package, "executables": deepcopy(self.executables),
                           "startedAt": f"2026-01-01T00:{n:02}:00Z",
                           "finishedAt": f"2026-01-01T00:{n:02}:30Z",
                           "screenshots": [self.picture.name], "operationLog": self.log.name}
                          for n in range(1, 11)],
        }

    def inspect(self):
        result = evidence.validate_cu(self.doc, self.root, identity=self.identity,
                                      package_sha256=self.package, model_fingerprint=self.fingerprint)
        self.assertIsInstance(result, list)
        self.assertTrue(all(isinstance(error, str) for error in result))
        return result

    def test_complete_validator_fixture_has_bound_scenarios(self):
        self.assertEqual(self.inspect(), [])

    def test_passed_flag_without_operations_is_incomplete(self):
        self.doc = {"passed": True}
        self.assertTrue(self.inspect())

    def test_all_ten_distinct_scenarios_are_required(self):
        for scenarios in (self.doc["scenarios"][:-1], [self.doc["scenarios"][0]] * 10):
            self.doc["scenarios"] = scenarios
            self.assertTrue(self.inspect())

    def test_non_passed_and_empty_actual_operations_are_rejected(self):
        original = deepcopy(self.doc)
        for field, value in (("status", "not-run"), ("expected", ""),
                             ("actual", "  "), ("actions", [])):
            self.doc = deepcopy(original)
            self.doc["scenarios"][0][field] = value
            self.assertTrue(self.inspect())

    def test_stale_commit_model_or_package_is_rejected(self):
        original = deepcopy(self.doc)
        for field, value in (("commit", "0" * 40), ("packageSha256", "0" * 64),
                             ("modelFingerprint", "different-model")):
            self.doc = deepcopy(original)
            self.doc[field] = value
            self.assertTrue(self.inspect())

    def test_source_identity_must_match_candidate(self):
        self.doc["identity"]["sourceTree"] = "0" * 40
        self.assertTrue(self.inspect())

    def test_scene_cannot_mix_another_package_or_executable(self):
        original = deepcopy(self.doc)
        self.doc["scenarios"][0]["packageSha256"] = "0" * 64
        self.assertTrue(self.inspect())
        self.doc = original
        self.doc["scenarios"][0]["executables"]["qcs-gui"]["sha256"] = "0" * 64
        self.assertTrue(self.inspect())

    def test_session_and_scene_timestamp_ranges_are_enforced(self):
        original = deepcopy(self.doc)
        for field, value in (("startedAt", "2025-12-31T23:59:00Z"),
                             ("finishedAt", "2026-01-01T00:31:00Z"),
                             ("finishedAt", "2026-01-01T00:01:00Z"),
                             ("startedAt", "2026-01-01T00:01:00")):
            self.doc = deepcopy(original)
            self.doc["scenarios"][0][field] = value
            self.assertTrue(self.inspect())
        self.doc = original
        self.doc["finishedAt"] = self.doc["startedAt"]
        self.assertTrue(self.inspect())

    def test_each_scene_requires_an_existing_screenshot(self):
        original = deepcopy(self.doc)
        self.doc["scenarios"][0]["screenshots"] = []
        self.assertTrue(self.inspect())
        self.doc = original
        self.picture.unlink()
        self.assertTrue(self.inspect())

    def test_empty_or_wrong_format_screenshot_is_rejected(self):
        for payload in (b"", b"not a screenshot" * 10):
            self.picture.write_bytes(payload)
            self.assertTrue(self.inspect())

    def test_screenshot_cannot_escape_the_evidence_root(self):
        self.doc["scenarios"][0]["screenshots"] = ["../other.png"]
        self.assertTrue(self.inspect())

    def test_missing_or_empty_operation_log_rejected(self):
        self.log.write_bytes(b"")
        self.assertTrue(self.inspect())
        self.log.unlink()
        self.assertTrue(self.inspect())

    def test_malformed_scenarios_return_errors(self):
        original = deepcopy(self.doc)
        for value in (None, {}, [None], "passed"):
            with self.subTest(value=value):
                self.doc = deepcopy(original)
                self.doc["scenarios"] = value
                self.assertTrue(self.inspect())


def accessible_short_ancestor(directory):
    """Choose only a cwd, never a destination for files outside a test root."""
    requested = directory.resolve()
    for candidate in (requested, *requested.parents):
        if evidence.fits_windows_cwd(candidate) and candidate.is_dir() and os.access(candidate, os.R_OK | os.X_OK):
            return candidate
    raise unittest.SkipTest("Environment has no accessible short cwd for launching an absolute-output fixture")


class ExecutionWorkingDirectoryTests(unittest.TestCase):
    """Launch actual tiny processes while evidence remains under long paths."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="qcs-cwd-evidence-中文 ",
            dir=os.environ.get("QCOPILOTS_SEARCH_TEST_ROOT") or None)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        # The explicit launcher is independently temporary. Evidence can live
        # beneath an arbitrarily deep configured test root on volumes without 8.3.
        launcher = tempfile.TemporaryDirectory(prefix="qcs-cwd-")
        self.addCleanup(launcher.cleanup)
        self.launcher = Path(launcher.name).resolve()
        self.absolute_output_cwd = accessible_short_ancestor(self.launcher)
        self.long_root = self.root / "long evidence"
        while len(str(self.long_root).encode("utf-16-le")) // 2 < 310:
            self.long_root /= "long evidence 中文 " + "segment" * 7
        self.long_root.mkdir(parents=True)

    def run_child(self, root, *, working_directory=None, relative_files=True):
        expected_cwd = working_directory or root
        input_path = (expected_cwd / "relative input 中文.txt") if relative_files else (root / "absolute input 中文.txt")
        output_path = (expected_cwd / "relative output.txt") if relative_files else (root / "absolute output.txt")
        input_path.write_text("relative fixture", encoding="utf-8")
        command = [sys.executable, "-c",
            "import json,os,pathlib,sys\n"
            "assert pathlib.Path(sys.argv[2]).read_text(encoding='utf-8')=='relative fixture'\n"
            "pathlib.Path(sys.argv[3]).write_text('child output',encoding='utf-8')\n"
            "pathlib.Path(sys.argv[1]).write_text(json.dumps({'cwd':os.getcwd(),'environment':"
            "os.environ['QCOPILOTS_SEARCH_CWD_TEST']}),encoding='utf-8')\n"
            "print('actual child stdout')\nprint('actual child stderr',file=sys.stderr)\n",
            str(root / "child.json"), input_path.name if relative_files else str(input_path),
            output_path.name if relative_files else str(output_path)]
        environment = {**os.environ, "QCOPILOTS_SEARCH_CWD_TEST": "unchanged relative/value 中文"}
        snapshot = environment.copy()
        receipt = evidence.execute_record(command, root, environment=environment,
                                          working_directory=working_directory)
        self.assertEqual(receipt["exitCode"], 0, (root / "stderr.log").read_text(encoding="utf-8"))
        self.assertEqual(receipt["command"], command)
        self.assertEqual(environment, snapshot)
        observed = json.loads((root / "child.json").read_text(encoding="utf-8"))
        self.assertTrue(os.path.samefile(observed["cwd"], expected_cwd))
        self.assertEqual(observed["environment"], environment["QCOPILOTS_SEARCH_CWD_TEST"])
        self.assertEqual(output_path.read_text(), "child output")
        self.assertIn("actual child stdout", (root / "stdout.log").read_text())
        self.assertIn("actual child stderr", (root / "stderr.log").read_text())
        return receipt

    def test_default_cwd_keeps_relative_arguments_in_same_directory(self):
        try:
            evidence.execution_working_directory(self.launcher)
        except ValueError as error:
            self.skipTest(f"Isolated system temporary cwd cannot launch a relative-file fixture: {error}")
        receipt = self.run_child(self.launcher)
        self.assertEqual(receipt["requestedWorkingDirectory"], str(self.launcher))
        self.assertTrue(os.path.samefile(receipt["actualWorkingDirectory"], self.launcher))

    def test_long_default_uses_real_alias_or_reports_filesystem_limitation(self):
        try:
            requested, actual = evidence.execution_working_directory(self.long_root)
        except ValueError as error:
            # A real no-8.3 filesystem is supported through the explicit choice,
            # not reported as a skipped automatic-alias process test.
            self.assertEqual(sys.platform, "win32")
            self.assertIn("--working-directory", str(error))
            receipt = self.run_child(self.long_root, working_directory=self.absolute_output_cwd, relative_files=False)
            self.assertEqual(receipt["requestedWorkingDirectory"], str(self.absolute_output_cwd))
        else:
            receipt = self.run_child(self.long_root)
            self.assertEqual(requested, self.long_root)
            self.assertTrue(os.path.samefile(actual, requested))
            self.assertEqual(receipt["requestedWorkingDirectory"], str(requested))
            self.assertEqual(receipt["actualWorkingDirectory"], str(actual))
            if sys.platform == "win32":
                self.assertLessEqual(len(str(actual).encode("utf-16-le")) // 2, 258)

    def test_explicit_cwd_keeps_long_evidence_and_changes_relative_base_only(self):
        owned_cwd = os.path.samefile(self.absolute_output_cwd, self.launcher)
        receipt = self.run_child(self.long_root, working_directory=self.absolute_output_cwd, relative_files=owned_cwd)
        self.assertEqual(receipt["requestedWorkingDirectory"], str(self.absolute_output_cwd))
        self.assertFalse((self.long_root / "relative output.txt").exists())
        self.assertTrue((self.long_root / "child.json").is_file())

    def test_exact_windows_cwd_boundary_with_real_processes(self):
        prefix_units = len(str(self.launcher).encode("utf-16-le")) // 2 + 1
        if prefix_units >= 258:
            reason = "Controlled temporary root is too long to construct a canonical 258-unit cwd without writing outside it"
            print(json.dumps({"case": "actual-process-cwd-boundary", "status": "capability-unavailable", "reason": reason}))
            self.skipTest(reason)
        for units in (258, 259):
            with self.subTest(utf16_units=units):
                directory = self.launcher / ("b" * (units - prefix_units))
                directory.mkdir()
                self.assertEqual(len(str(directory).encode("utf-16-le")) // 2, units)
                try:
                    requested, actual = evidence.execution_working_directory(directory)
                except ValueError as error:
                    self.assertEqual((sys.platform, units), ("win32", 259))
                    self.assertIn("--working-directory", str(error))
                    self.run_child(directory, working_directory=self.absolute_output_cwd, relative_files=False)
                else:
                    receipt = self.run_child(directory)
                    if sys.platform == "win32":
                        self.assertLessEqual(len(str(actual).encode("utf-16-le")) // 2, 258)
                        if units == 258:
                            self.assertEqual(actual, requested)
                    print(json.dumps({"case": "actual-process-cwd-boundary", "requestedUtf16Units": units,
                        "actualUtf16Units": len(str(actual).encode("utf-16-le")) // 2,
                        "exitCode": receipt["exitCode"]}))

    def test_explicit_cwd_launches_real_process_when_alias_lookup_is_unavailable(self):
        # Only the API failure is simulated. The explicit-cwd child really runs,
        # and writes its report into the same long evidence directory.
        with mock.patch.object(evidence.sys, "platform", "win32"), \
                mock.patch.object(evidence, "windows_short_directory", return_value=None):
            with self.assertRaisesRegex(ValueError, "--working-directory"):
                evidence.execution_working_directory(self.long_root)
            self.run_child(self.long_root, working_directory=self.absolute_output_cwd, relative_files=False)

    def test_unusable_aliases_fail_before_launch_with_explicit_cwd_guidance(self):
        # These are failure-branch simulations, separate from the real API and
        # process execution above. They do not claim that this volume lacks 8.3.
        missing = Path(self.absolute_output_cwd.anchor) / ("qcs-missing-" + self.root.name[-8:])
        for candidate in (None, self.absolute_output_cwd, missing, self.long_root):
            with self.subTest(candidate=candidate), mock.patch.object(evidence.sys, "platform", "win32"), \
                    mock.patch.object(evidence, "windows_short_directory", return_value=candidate), \
                    mock.patch.object(evidence.subprocess, "run") as spawn:
                with self.assertRaisesRegex(ValueError, "--working-directory"):
                    evidence.execute_record([sys.executable, "-c", "pass"], self.long_root)
                spawn.assert_not_called()

    def test_utf16_units_not_python_character_count_control_windows_limit(self):
        # Pure Windows path data, independent of the host filesystem and TEMP.
        boundary = "C:/" + "x" * 255
        non_bmp = "C:/" + "🚀" * 128
        self.assertTrue(evidence.fits_windows_cwd(boundary))
        self.assertFalse(evidence.fits_windows_cwd(boundary + "x"))
        self.assertLess(len(non_bmp), 258)
        self.assertFalse(evidence.fits_windows_cwd(non_bmp))

    def test_missing_explicit_directory_is_rejected_before_launch(self):
        with mock.patch.object(evidence.subprocess, "run") as spawn:
            with self.assertRaisesRegex(ValueError, "does not exist"):
                evidence.execute_record([sys.executable, "-c", "pass"], self.root,
                                        working_directory=self.root / "not created")
            spawn.assert_not_called()

    def test_long_temp_root_keeps_core_executions_and_reports_boundary_limit(self):
        cases = [
            "ExecutionWorkingDirectoryTests.test_explicit_cwd_keeps_long_evidence_and_changes_relative_base_only",
            "ExecutionWorkingDirectoryTests.test_explicit_cwd_launches_real_process_when_alias_lookup_is_unavailable",
            "ExecutionWorkingDirectoryTests.test_utf16_units_not_python_character_count_control_windows_limit",
            "ExecutionWorkingDirectoryTests.test_exact_windows_cwd_boundary_with_real_processes",
            "ExecutionProvenanceTests.test_cli_working_directory_records_actual_build_process"]
        command = [sys.executable, "-c",
            "import pathlib,runpy,sys,tempfile\n"
            # Override the Python-selected root after interpreter startup. This
            # exercises real TemporaryDirectory paths without changing the host
            # interpreter launcher's own OS temporary-directory requirements.
            "tempfile.tempdir=sys.argv.pop(1)\n"
            "assert len(str(pathlib.Path(tempfile.gettempdir()).resolve()).encode('utf-16-le'))//2>300\n"
            "sys.argv=sys.argv[1:]\nrunpy.run_path(sys.argv[0],run_name='__main__')",
            str(self.long_root), str(Path(__file__).resolve())]
        for both_roots_long in (False, True):
            # The second run directly covers an already-long configured root.
            # It needs no synthetic Git checkout or external Git cwd support.
            selected = cases[:-1] if both_roots_long else cases
            environment = os.environ.copy()
            # Keep the nested Git fixture in an explicitly owned original root.
            # An unset inherited variable must not move it into the overridden
            # long tempfile root and accidentally test Git's own cwd limit.
            original_fixture_root = min((self.root, self.launcher),
                key=lambda path: len(str(path).encode("utf-16-le")))
            environment["QCOPILOTS_SEARCH_TEST_ROOT"] = str(
                self.long_root if both_roots_long else original_fixture_root)
            result = subprocess.run([*command, *selected, "-v"], cwd=self.absolute_output_cwd,
                env=environment, capture_output=True, text=True, encoding="utf-8", timeout=60)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn('"status": "capability-unavailable"', result.stdout)
            self.assertIn(f"Ran {len(selected)} tests", result.stderr)
            self.assertIn("OK (skipped=1)", result.stderr)
            print(json.dumps({"case": "actual-process-long-python-temp-root", "exitCode": result.returncode,
                "coreTestsPassed": len(selected) - 1, "cwdBoundaryCapabilityUnavailable": 1,
                "configuredRootAlsoLong": both_roots_long,
                "temporaryRootUtf16Units": len(str(self.long_root).encode("utf-16-le")) // 2}))


class ExecutionProvenanceTests(unittest.TestCase):
    """Run tiny fixture commands in a fresh Git repository, never the application."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="qcs-provenance-中文 ",
            dir=os.environ.get("QCOPILOTS_SEARCH_TEST_ROOT") or None)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        launcher = tempfile.TemporaryDirectory(prefix="qcs-cwd-")
        self.addCleanup(launcher.cleanup)
        self.launcher = Path(launcher.name).resolve()
        self.working_directory = accessible_short_ancestor(self.launcher)
        self.source = self.root / "synthetic source 中文"
        self.source.mkdir()
        self.hooks = self.root / "empty-git-hooks"
        self.hooks.mkdir()
        self.environment = os.environ.copy()
        for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"):
            self.environment.pop(name, None)
        self.git("init", "--quiet", "--initial-branch=main")
        for name in evidence.LOCKS:
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("synthetic validator dependency lock\n", encoding="utf-8")
        self.git("add", "--all")
        self.git("commit", "--quiet", "-m", "Synthetic validator fixture")
        self.output = self.root / "synthetic programs"
        self.output.mkdir()
        self.programs = {name: self.output / (name + ".exe") for name in (
            "qcs-gui", "qcs-service", "qcs-worker", "qcs-cli")}
        self.build_command = [sys.executable, "-c",
            "import pathlib,sys\nfor name in sys.argv[1:]:\n p=pathlib.Path(name)\n p.write_bytes(b'SYNTHETIC VALIDATOR FIXTURE '+p.name.encode())",
            *[str(path) for path in self.programs.values()]]

    def git(self, *arguments):
        result = subprocess.run(["git", "-c", "user.name=Validator fixture",
            "-c", "user.email=validator@example.invalid", "-c", "commit.gpgsign=false",
            "-c", "core.autocrlf=false", "-c", "core.fsmonitor=false",
            "-c", f"core.hooksPath={self.hooks}", "-C", str(self.source), *arguments],
            env=self.environment, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0,
            f"Synthetic Git fixture command failed: {arguments!r}\n{result.stdout}\n{result.stderr}")
        return result

    def build(self):
        return evidence.record_build(self.source, self.root / "recorded builds",
                                     self.programs, self.build_command,
                                     working_directory=self.working_directory)

    def candidate(self, *, changed_program=None):
        build_path, build = self.build()
        if changed_program is not None:
            program = self.programs[changed_program]
            program.write_bytes(program.read_bytes() + b" SYNTHETIC CHANGE AFTER RECORDED BUILD")
        package = self.root / "qcs-synthetic-validator.zip"
        with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_STORED) as archive:
            for name, path in self.programs.items():
                archive.write(path, "bin/" + name + ".exe")
        application = self.root / "synthetic installed application"
        with zipfile.ZipFile(package) as archive:
            archive.extractall(application)
        verification = self.root / "synthetic-package-verification.json"
        report = package_release_fixture(self.root, worker=application / "bin/qcs-worker.exe",
                                         application_root=application)
        report.update({"commit": build["identity"]["checkoutCommit"], "workingTreeModified": False,
            "packageSha256": hashlib.sha256(package.read_bytes()).hexdigest(),
            "checks": [{"name": f"synthetic-validator-{n}", "passed": True} for n in range(9)]})
        report["buildIdentity"]["candidateCommit"] = build["identity"]["checkoutCommit"]
        write_program_inventory(self.root, report)
        if changed_program is not None:
            self.assertEqual(evidence.validate_package_release(report, self.root,
                package_sha256=hashlib.sha256(package.read_bytes()).hexdigest()), [])
        verification.write_text(json.dumps(report), encoding="utf-8")
        return evidence.record_package(build_path, package, verification,
            self.root / "recorded candidates", "synthetic-model-fingerprint", "synthetic-extraction-fingerprint")


    def test_cli_working_directory_records_actual_build_process(self):
        parent = self.root / "recorded cli executions"
        while len(str(parent).encode("utf-16-le")) // 2 < 300:
            parent /= "evidence 中文 " + "segment" * 7
        parent.mkdir(parents=True)
        input_path = self.launcher / "relative input.txt"
        input_path.write_text("explicit cwd", encoding="utf-8")
        input_argument = input_path.name if self.working_directory == self.launcher else str(input_path)
        build_command = self.build_command.copy()
        build_command[2] += f"\nassert pathlib.Path({input_argument!r}).read_text()=='explicit cwd'"
        command = [sys.executable, str(Path(evidence.__file__)), "record-build",
            "--source", str(self.source), "--output-root", str(parent),
            "--working-directory", str(self.working_directory)]
        for name, path in self.programs.items():
            command += ["--program", f"{name}={path}"]
        result = subprocess.run(command + ["--", *build_command], cwd=self.working_directory,
            capture_output=True, text=True, encoding="utf-8", env=self.environment)
        self.assertEqual(result.returncode, 0, result.stderr)
        build_path = Path(json.loads(result.stdout)["record"])
        build = evidence.read_json(build_path)
        self.assertTrue(build["passed"])
        self.assertTrue(build_path.resolve().is_relative_to(parent.resolve()))
        self.assertEqual(evidence.verified_build(build_path.parent, build), [])
        execution = build["execution"]
        self.assertEqual(execution["requestedWorkingDirectory"], str(self.working_directory))
        self.assertTrue(os.path.samefile(execution["actualWorkingDirectory"], self.working_directory))


    def test_clean_fixture_build_and_package_provenance_are_inspectable(self):
        path, candidate = self.candidate()
        self.assertEqual(evidence.inspect_candidate(path.parent, candidate), [])
        build_path = path.parent / candidate["buildProvenance"]["path"]
        self.assertEqual(evidence.verified_build(build_path.parent, evidence.read_json(build_path)), [])

    def test_record_package_rejects_self_consistent_program_changes_after_build(self):
        parent = self.root
        for name in sorted(self.programs):
            with self.subTest(program=name):
                self.root = parent / name
                self.root.mkdir()
                expected = ("Package inference worker differs from the recorded executable"
                    if name == "qcs-worker" else f"Packaged executable differs from its recorded build: {name}")
                with self.assertRaisesRegex(ValueError, expected):
                    self.candidate(changed_program=name)

    def test_inspect_candidate_keeps_original_build_binding_after_all_package_hashes_change(self):
        parent = self.root
        for name in sorted(self.programs):
            with self.subTest(program=name):
                self.root = parent / name
                self.root.mkdir()
                path, candidate = self.candidate()
                root = path.parent
                package = root / candidate["package"]
                member = candidate["executables"][name]["packageMember"]
                with zipfile.ZipFile(package) as archive:
                    members = {entry.filename: archive.read(entry) for entry in archive.infolist()}
                members[member] += b" SYNTHETIC REPLACEMENT AFTER CAPTURE"
                with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_STORED) as archive:
                    for relative, payload in members.items():
                        archive.writestr(relative, payload)
                program_hash = hashlib.sha256(members[member]).hexdigest()
                package_hash = hashlib.sha256(package.read_bytes()).hexdigest()
                candidate["packageSha256"] = package_hash
                candidate["executables"][name]["sha256"] = program_hash
                report_path = root / candidate["verification"]
                report = json.loads(report_path.read_text(encoding="utf-8"))
                report["packageSha256"] = package_hash
                inventory_path = root / report["programInventory"]["path"]
                inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
                inventory["packageSha256"] = package_hash
                inventory["programs"][name].update({"bytes": len(members[member]), "sha256": program_hash})
                inventory_path.write_text(json.dumps(inventory), encoding="utf-8")
                report["programInventory"]["sha256"] = hashlib.sha256(inventory_path.read_bytes()).hexdigest()
                if name == "qcs-worker":
                    report["modelSmoke"]["runtime"]["workerExecutable"].update(
                        {"bytes": len(members[member]), "sha256": program_hash})
                report_path.write_text(json.dumps(report), encoding="utf-8")
                candidate["files"] = [evidence.file_record(root, root / entry["path"])
                                      for entry in candidate["files"]]
                self.assertEqual(evidence.verify_manifest(root, candidate), [])
                build_path = root / candidate["buildProvenance"]["path"]
                self.assertEqual(evidence.verified_build(build_path.parent, evidence.read_json(build_path)), [])
                self.assertEqual(evidence.validate_package_release(report, root,
                    package_sha256=package_hash), [])
                self.assertIn(f"Candidate executable is not the recorded build output: {name}",
                              evidence.inspect_candidate(root, candidate))

    def test_package_and_candidate_reject_rehashed_inventory_that_disagrees_with_unchanged_zip(self):
        parent = self.root
        for field in ("path", "bytes", "sha256"):
            with self.subTest(field=field):
                self.root = parent / field
                self.root.mkdir()
                path, candidate = self.candidate()
                root = path.parent
                build_path = root / candidate["buildProvenance"]["path"]
                report_path = root / candidate["verification"]
                report = json.loads(report_path.read_text(encoding="utf-8"))
                inventory_path = root / report["programInventory"]["path"]
                inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
                record = inventory["programs"]["qcs-gui"]
                record[field] = {"path": "other/qcs-gui.exe", "bytes": record["bytes"] + 1,
                                 "sha256": "0" * 64}[field]
                inventory_path.write_text(json.dumps(inventory), encoding="utf-8")
                report["programInventory"]["sha256"] = hashlib.sha256(inventory_path.read_bytes()).hexdigest()
                report_path.write_text(json.dumps(report), encoding="utf-8")
                candidate["files"] = [evidence.file_record(root, root / entry["path"])
                                      for entry in candidate["files"]]
                self.assertEqual(evidence.verify_manifest(root, candidate), [])
                self.assertEqual(evidence.verified_build(build_path.parent, evidence.read_json(build_path)), [])
                self.assertEqual(evidence.validate_package_release(report, root,
                    package_sha256=candidate["packageSha256"]), [])
                with self.assertRaisesRegex(ValueError,
                        "Packaged executable differs from its independent final inventory: qcs-gui"):
                    evidence.record_package(build_path, root / candidate["package"], report_path,
                        self.root / "rejected candidates", "synthetic-model-fingerprint", "synthetic-extraction-fingerprint")
                self.assertEqual(evidence.inspect_candidate(root, candidate),
                    ["Candidate executable differs from its independent final inventory: qcs-gui"])

    def test_candidate_archive_tampering_is_detected(self):
        path, candidate = self.candidate()
        with (path.parent / candidate["package"]).open("ab") as stream:
            stream.write(b"Synthetic tamper")
        self.assertTrue(evidence.inspect_candidate(path.parent, candidate))

    def test_stale_build_hash_and_unbound_candidate_files_are_rejected(self):
        path, candidate = self.candidate()
        stale = deepcopy(candidate)
        stale["buildProvenance"]["sha256"] = "0" * 64
        self.assertTrue(evidence.inspect_candidate(path.parent, stale))
        verification = evidence.read_json(path.parent / candidate["verification"])
        required = [candidate["package"], candidate["verification"], candidate["buildProvenance"]["path"],
                    *evidence.package_evidence_paths(verification, path.parent)]
        for name in required:
            unbound = deepcopy(candidate)
            unbound["files"] = [item for item in unbound["files"] if item["path"] != name]
            self.assertTrue(evidence.inspect_candidate(path.parent, unbound))

    def test_dirty_checkout_before_build_is_rejected_before_execution(self):
        (self.source / evidence.LOCKS[0]).write_text("changed before build", encoding="utf-8")
        with mock.patch.object(evidence, "execute_record") as execute:
            with self.assertRaises(ValueError):
                self.build()
            execute.assert_not_called()

    def test_source_change_during_build_cannot_produce_success_receipt(self):
        self.build_command = [sys.executable, "-c",
            "import pathlib,sys\npathlib.Path(sys.argv[1]).write_text('changed during fixture command')",
            str(self.source / evidence.LOCKS[0])]
        path, receipt = self.build()
        self.assertFalse(receipt["passed"])
        self.assertTrue(evidence.verified_build(path.parent, receipt))


class CiOriginEvidenceTests(unittest.TestCase):
    """Synthetic package/CU contracts, never application or UI acceptance."""

    setUp = ExecutionProvenanceTests.setUp
    git = ExecutionProvenanceTests.git
    build = ExecutionProvenanceTests.build
    candidate = ExecutionProvenanceTests.candidate

    def frozen_ci(self, candidate_path, candidate, *, package_comment=None):
        """Use the real eleven-configuration freeze, replacing only source generation."""
        spec = importlib.util.spec_from_file_location("qcs_synthetic_release_fixture",
            Path(__file__).resolve().parents[2] / "tools/qcs-test-release.py")
        fixture = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fixture)
        fixture.CANDIDATE = candidate["identity"]["candidateHead"]
        release = fixture.release
        api = fixture.FakeCI()
        package = candidate_path.parent / candidate["package"]
        package_bytes = package.read_bytes()
        if package_comment is not None:
            modified = io.BytesIO(package_bytes)
            with zipfile.ZipFile(modified, "a") as archive:
                archive.comment = package_comment
            package_bytes = modified.getvalue()
        selected = next(item for item in api.artifacts
                        if "windows-x64-cpu-" in item["name"])
        with zipfile.ZipFile(io.BytesIO(api.downloads[selected["id"]])) as archive:
            members = {item.filename: archive.read(item) for item in archive.infolist()}
        cpu_name = next(name for name in members if name.endswith(".zip"))
        members[cpu_name] = package_bytes
        artifact = io.BytesIO()
        with zipfile.ZipFile(artifact, "w", compression=zipfile.ZIP_STORED) as archive:
            for name, data in members.items():
                archive.writestr(name, data)
        api.downloads[selected["id"]] = artifact.getvalue()
        selected["digest"] = "sha256:" + hashlib.sha256(artifact.getvalue()).hexdigest()
        source_bundle = self.root / f"qcs-source-materials-{fixture.CANDIDATE[:12]}.zip"
        source_bundle.write_bytes(b"SYNTHETIC SOURCE GENERATION BOUNDARY, NOT RELEASE SOURCES")
        for patch in (mock.patch.object(release, "identity_for", return_value=candidate["identity"]),
                      mock.patch.object(release, "build_sources", return_value=source_bundle),
                      mock.patch.object(release, "validate_sources")):
            patch.start()
            self.addCleanup(patch.stop)
        manifest = release.prepare(release.SOURCE, fixture.CANDIDATE, "123", 1,
            self.root / "synthetic frozen CI release", api=api)
        document = release.verify_freeze(manifest, release.SOURCE)
        self.assertEqual(len(document["configurations"]), 11)
        self.assertEqual(sum(item["role"] == "user-package" for item in document["assets"]), 21)
        frozen_package = manifest.parent / "assets" / cpu_name
        self.assertEqual(frozen_package.read_bytes(), package_bytes)
        report_root = self.root / "synthetic CI verification"
        application = report_root / "synthetic application"
        application.mkdir(parents=True)
        with zipfile.ZipFile(frozen_package) as archive:
            archive.extractall(application)
        report = package_release_fixture(report_root,
            worker=application / "bin/qcs-worker.exe", application_root=application)
        report.update({"commit": fixture.CANDIDATE, "workingTreeModified": False,
            "packageSha256": evidence.digest(frozen_package),
            "checks": [{"name": f"synthetic-validator-{number}", "passed": True} for number in range(9)]})
        report["buildIdentity"]["candidateCommit"] = fixture.CANDIDATE
        write_program_inventory(report_root, report)
        verification = report_root / "synthetic-package-verification.json"
        evidence.write_json(verification, report)
        self.assertEqual(evidence.validate_package_release(report, report_root,
            package_sha256=evidence.digest(frozen_package)), [])
        return release, manifest, verification

    def ci_candidate(self):
        """Capture through the official bridge with real frozen artifact bytes."""
        local_path, local = self.candidate()
        release, manifest, verification = self.frozen_ci(local_path, local)
        patch = mock.patch.object(evidence, "release_module", return_value=release)
        patch.start()
        self.addCleanup(patch.stop)
        path, candidate = evidence.record_ci_package(manifest, verification,
            self.root / "recorded CI candidates", local["modelFingerprint"], local["extractionFingerprint"])
        return path, candidate, release, manifest, verification

    def rebind_candidate_files(self, root, candidate):
        candidate["files"] = [evidence.file_record(root, root / entry["path"])
                              for entry in candidate["files"]]

    def local_evidence(self, candidate_path, candidate):
        """Bind test-only pixel and operation text to the supplied actual ZIP."""
        root = self.root / "synthetic computer-use validator input"
        root.mkdir()
        picture = root / "SYNTHETIC-validator-pixel.png"
        picture.write_bytes(base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jR1cAAAAASUVORK5CYII="))
        operation = root / "SYNTHETIC-validator-operation.log"
        operation.write_text("Synthetic validator input only. No UI operation occurred.\n", encoding="utf-8")
        document = {"schemaVersion": 1, "kind": "windows-computer-use", "passed": True,
            "syntheticValidatorFixture": True, "identity": deepcopy(candidate["identity"]),
            "commit": candidate["identity"]["checkoutCommit"],
            "packageSha256": candidate["packageSha256"],
            "modelFingerprint": candidate["modelFingerprint"],
            "extractionFingerprint": candidate["extractionFingerprint"],
            "executables": deepcopy(candidate["executables"]),
            "startedAt": "2026-01-01T00:00:00Z", "finishedAt": "2026-01-01T00:30:00Z",
            "scenarios": [{"id": f"CU-{number:02}", "status": "passed",
                "expected": "Synthetic schema expectation, no application run",
                "actual": "Synthetic schema observation, no application run",
                "actions": ["Synthetic validator action, no UI operation"],
                "packageSha256": candidate["packageSha256"],
                "executables": deepcopy(candidate["executables"]),
                "startedAt": f"2026-01-01T00:{number:02}:00Z",
                "finishedAt": f"2026-01-01T00:{number:02}:30Z",
                "screenshots": [picture.name], "operationLog": operation.name}
                for number in range(1, 11)],
            "files": [evidence.file_record(root, picture), evidence.file_record(root, operation)]}
        report = root / "SYNTHETIC-computer-use.json"
        evidence.write_json(report, document)
        self.assertEqual(evidence.validate_cu(document, root, identity=candidate["identity"],
            package_sha256=candidate["packageSha256"], model_fingerprint=candidate["modelFingerprint"]), [])
        manifest, _ = evidence.collect_local({"candidate": candidate_path, "computerUse": report},
                                             self.root / "synthetic local evidence")
        return manifest.parent

    def test_existing_local_build_candidate_remains_valid_without_release_comparison(self):
        path, candidate = self.candidate()
        root = self.local_evidence(path, candidate)
        result = evidence.local_preflight(root, candidate["identity"]["candidateHead"])
        self.assertTrue(result["passed"], result["errors"])
        self.assertTrue(result["localGates"]["candidate"]["passed"])
        self.assertTrue(result["localGates"]["computerUse"]["passed"])

    def test_recorded_ci_origin_contains_real_selected_zip_and_independent_program_inventory(self):
        path, candidate, release, manifest, verification = self.ci_candidate()
        self.assertNotIn("buildProvenance", candidate)
        self.assertEqual(candidate["kind"], "package-candidate")
        self.assertEqual(evidence.inspect_candidate(path.parent, candidate), [])
        origin_path = path.parent / candidate["ciProvenance"]["path"]
        self.assertEqual(evidence.digest(origin_path), candidate["ciProvenance"]["sha256"])
        origin = evidence.read_json(origin_path)
        self.assertEqual(origin["kind"], "ci-cpu-package-origin")
        self.assertEqual(origin["variant"], "windows-x64-cpu")
        selected = release.verify_frozen_cpu_package(manifest, release.SOURCE)
        self.assertEqual(origin["selected"], selected["selected"])
        self.assertEqual((path.parent / candidate["package"]).read_bytes(), selected["package"]["path"].read_bytes())
        self.assertEqual(set(candidate["executables"]), evidence.APPLICATIONS)
        self.assertEqual(evidence.read_json(verification)["packageSha256"], candidate["packageSha256"])

    def test_ci_package_and_cu_bind_the_same_final_release_zip(self):
        path, candidate, release, manifest, _ = self.ci_candidate()
        local = self.local_evidence(path, candidate)
        result = evidence.local_preflight(local, candidate["identity"]["candidateHead"], manifest)
        self.assertTrue(result["passed"], result["errors"])
        self.assertEqual(result["scope"], "final-release-package")
        gate = result["localGates"]["finalReleasePackage"]
        self.assertTrue(gate["passed"], gate["errors"])
        self.assertEqual(gate["packageSha256"], candidate["packageSha256"])
        self.assertEqual(gate["releaseManifestSha256"], evidence.digest(manifest))

    def test_local_build_origin_can_match_the_exact_same_ci_zip(self):
        path, candidate = self.candidate()
        release, manifest, _ = self.frozen_ci(path, candidate)
        local = self.local_evidence(path, candidate)
        with mock.patch.object(evidence, "release_module", return_value=release):
            result = evidence.local_preflight(local, candidate["identity"]["candidateHead"], manifest)
        self.assertTrue(result["passed"], result["errors"])
        self.assertTrue(result["localGates"]["finalReleasePackage"]["passed"])

    def test_same_commit_and_four_programs_cannot_substitute_a_different_zip_for_ci(self):
        path, candidate = self.candidate()
        release, manifest, _ = self.frozen_ci(path, candidate,
            package_comment=b"SYNTHETIC second archive of identical executable bytes")
        selected = release.verify_frozen_cpu_package(manifest, release.SOURCE)
        self.assertNotEqual(selected["package"]["sha256"], candidate["packageSha256"])
        with zipfile.ZipFile(selected["package"]["path"]) as archive:
            for program in candidate["executables"].values():
                self.assertEqual(hashlib.sha256(archive.read(program["packageMember"])).hexdigest(), program["sha256"])
        local = self.local_evidence(path, candidate)
        self.assertTrue(evidence.local_preflight(local, candidate["identity"]["candidateHead"])["passed"])
        with mock.patch.object(evidence, "release_module", return_value=release):
            result = evidence.local_preflight(local, candidate["identity"]["candidateHead"], manifest)
        self.assertFalse(result["passed"])
        self.assertTrue(result["localGates"]["candidate"]["passed"])
        self.assertTrue(result["localGates"]["computerUse"]["passed"])
        self.assertFalse(result["localGates"]["finalReleasePackage"]["passed"])

    def test_candidate_requires_exactly_one_local_or_ci_origin(self):
        path, candidate, _, _, _ = self.ci_candidate()
        for mutation in ("both", "neither", "null-local", "null-ci"):
            with self.subTest(mutation=mutation):
                changed = deepcopy(candidate)
                if mutation == "both":
                    changed["buildProvenance"] = deepcopy(changed["ciProvenance"])
                elif mutation == "neither":
                    changed.pop("ciProvenance")
                elif mutation == "null-local":
                    changed["buildProvenance"] = None
                else:
                    changed["ciProvenance"] = None
                self.assertTrue(evidence.inspect_candidate(path.parent, changed))

    def test_ci_origin_hash_and_every_raw_source_file_are_content_bound(self):
        path, candidate, _, _, _ = self.ci_candidate()
        stale = deepcopy(candidate)
        stale["ciProvenance"]["sha256"] = "0" * 64
        self.assertTrue(evidence.inspect_candidate(path.parent, stale))
        for entry in candidate["files"]:
            if not entry["path"].startswith("ci/"):
                continue
            with self.subTest(unbound=entry["path"]):
                changed = deepcopy(candidate)
                changed["files"] = [item for item in changed["files"] if item["path"] != entry["path"]]
                self.assertTrue(evidence.inspect_candidate(path.parent, changed))

    def test_record_ci_package_rejects_stale_or_unsuccessful_verification(self):
        _, candidate, _, manifest, verification = self.ci_candidate()
        original = evidence.read_json(verification)
        for field, value in (("commit", "0" * 40), ("packageSha256", "0" * 64),
                             ("workingTreeModified", True), ("passed", False)):
            with self.subTest(field=field):
                changed = deepcopy(original)
                changed[field] = value
                evidence.write_json(verification, changed)
                with self.assertRaises(ValueError):
                    evidence.record_ci_package(manifest, verification, self.root / "rejected CI candidates",
                        candidate["modelFingerprint"], candidate["extractionFingerprint"])
        evidence.write_json(verification, original)

    def test_rehashed_ci_origin_still_requires_original_workflow_artifact_and_candidate(self):
        path, candidate, _, _, _ = self.ci_candidate()
        origin_path = path.parent / candidate["ciProvenance"]["path"]
        original = evidence.read_json(origin_path)
        changes = (("repository", "another/repository"), ("variant", "windows-x64-vulkan"),
                   ("runId", "999"), ("requestedAttempt", 2),
                   ("identity", {**original["identity"], "candidateHead": "0" * 40}),
                   ("identity", {**original["identity"], "candidateHead": "0" * 40,
                                 "checkoutCommit": "0" * 40}),
                   ("selected", {**original["selected"], "artifactId": 99999}),
                   ("selected", {**original["selected"], "sourceJobId": 99999}),
                   ("artifact", {**original["artifact"], "digest": "sha256:" + "0" * 64}))
        for field, value in changes:
            with self.subTest(field=field, value=value):
                changed_origin = deepcopy(original)
                changed_origin[field] = value
                evidence.write_json(origin_path, changed_origin)
                changed_candidate = deepcopy(candidate)
                changed_candidate["ciProvenance"]["sha256"] = evidence.digest(origin_path)
                self.rebind_candidate_files(path.parent, changed_candidate)
                self.assertEqual(evidence.verify_manifest(path.parent, changed_candidate), [])
                self.assertTrue(evidence.inspect_candidate(path.parent, changed_candidate))
        evidence.write_json(origin_path, original)

    def test_frozen_ci_byte_changes_fail_capture_and_final_preflight(self):
        path, candidate, release, manifest, verification = self.ci_candidate()
        local = self.local_evidence(path, candidate)
        selected = release.verify_frozen_cpu_package(manifest, release.SOURCE)
        package = selected["package"]["path"]
        package.write_bytes(package.read_bytes() + b"SYNTHETIC mutation after freeze")
        with self.assertRaises(ValueError):
            evidence.record_ci_package(manifest, verification, self.root / "rejected changed freeze",
                candidate["modelFingerprint"], candidate["extractionFingerprint"])
        result = evidence.local_preflight(local, candidate["identity"]["candidateHead"], manifest)
        self.assertFalse(result["passed"])
        self.assertFalse(result["localGates"]["finalReleasePackage"]["passed"])
        self.assertEqual(evidence.inspect_candidate(path.parent, candidate), [])

    def test_all_four_programs_remain_bound_to_ci_when_local_metadata_is_rehashed(self):
        _, original, _, manifest, verification = self.ci_candidate()
        for name in sorted(evidence.APPLICATIONS):
            with self.subTest(program=name):
                path, candidate = evidence.record_ci_package(manifest, verification,
                    self.root / "mutated program candidates", original["modelFingerprint"],
                    original["extractionFingerprint"])
                root = path.parent
                package = root / candidate["package"]
                member = candidate["executables"][name]["packageMember"]
                with zipfile.ZipFile(package) as archive:
                    members = {entry.filename: archive.read(entry) for entry in archive.infolist()}
                members[member] += b" SYNTHETIC PROGRAM REPLACEMENT AFTER CI CAPTURE"
                with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_STORED) as archive:
                    for relative, payload in members.items():
                        archive.writestr(relative, payload)
                program_hash = hashlib.sha256(members[member]).hexdigest()
                package_hash = evidence.digest(package)
                candidate["packageSha256"] = package_hash
                candidate["executables"][name]["sha256"] = program_hash
                report_path = root / candidate["verification"]
                report = evidence.read_json(report_path)
                report["packageSha256"] = package_hash
                inventory_path = root / report["programInventory"]["path"]
                inventory = evidence.read_json(inventory_path)
                inventory["packageSha256"] = package_hash
                inventory["programs"][name].update(bytes=len(members[member]), sha256=program_hash)
                evidence.write_json(inventory_path, inventory)
                report["programInventory"]["sha256"] = evidence.digest(inventory_path)
                if name == "qcs-worker":
                    report["modelSmoke"]["runtime"]["workerExecutable"].update(
                        bytes=len(members[member]), sha256=program_hash)
                evidence.write_json(report_path, report)
                origin_path = root / candidate["ciProvenance"]["path"]
                origin = evidence.read_json(origin_path)
                origin["package"].update(bytes=package.stat().st_size, sha256=package_hash)
                evidence.write_json(origin_path, origin)
                candidate["ciProvenance"]["sha256"] = evidence.digest(origin_path)
                self.rebind_candidate_files(root, candidate)
                self.assertEqual(evidence.verify_manifest(root, candidate), [])
                self.assertEqual(evidence.validate_package_release(report, root,
                    package_sha256=package_hash), [])
                errors = evidence.inspect_candidate(root, candidate)
                self.assertTrue(any("CI CPU portable ZIP differs from its original artifact member" in error
                                    for error in errors), errors)

    def test_record_ci_rejects_rehashed_final_inventory_different_from_selected_zip(self):
        _, candidate, _, manifest, verification = self.ci_candidate()
        original = evidence.read_json(verification)
        inventory_path = verification.parent / original["programInventory"]["path"]
        original_inventory = evidence.read_json(inventory_path)
        for name in sorted(evidence.APPLICATIONS):
            with self.subTest(program=name):
                report, inventory = deepcopy(original), deepcopy(original_inventory)
                inventory["programs"][name]["sha256"] = "0" * 64
                evidence.write_json(inventory_path, inventory)
                report["programInventory"]["sha256"] = evidence.digest(inventory_path)
                if name == "qcs-worker":
                    report["modelSmoke"]["runtime"]["workerExecutable"]["sha256"] = "0" * 64
                evidence.write_json(verification, report)
                self.assertEqual(evidence.validate_package_release(report, verification.parent,
                    package_sha256=candidate["packageSha256"]), [])
                with self.assertRaises(ValueError):
                    evidence.record_ci_package(manifest, verification,
                        self.root / "rejected program inventories", candidate["modelFingerprint"],
                        candidate["extractionFingerprint"])
        evidence.write_json(inventory_path, original_inventory)
        evidence.write_json(verification, original)

    def test_official_cli_records_selected_ci_package_and_validates_release_binding(self):
        _, source_candidate, _, manifest, verification = self.ci_candidate()
        arguments = ["record-ci-package", "--release-manifest", str(manifest),
            "--verification", str(verification), "--output-root", str(self.root / "CLI CI candidates"),
            "--model-fingerprint", source_candidate["modelFingerprint"],
            "--extraction-fingerprint", source_candidate["extractionFingerprint"]]
        output = io.StringIO()
        with redirect_stdout(output):
            status = evidence.main(arguments)
        self.assertEqual(status, 0, output.getvalue())
        path = Path(json.loads(output.getvalue())["record"])
        candidate = evidence.read_json(path)
        self.assertEqual(evidence.inspect_candidate(path.parent, candidate), [])
        local = self.local_evidence(path, candidate)
        preflight = self.root / "CLI-final-package-preflight.json"
        with redirect_stdout(io.StringIO()):
            status = evidence.main(["validate-local", "--candidate-commit", candidate["identity"]["candidateHead"],
                "--local-evidence-dir", str(local), "--release-manifest", str(manifest), "--output", str(preflight)])
        self.assertEqual(status, 0)
        result = evidence.read_json(preflight)
        self.assertTrue(result["passed"], result["errors"])
        self.assertEqual(result["scope"], "final-release-package")
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as rejected:
            evidence.main([*arguments, "--package", str(path.parent / candidate["package"])])
        self.assertEqual(rejected.exception.code, 2)

    def test_cli_help_advertises_frozen_source_and_optional_final_release_comparison(self):
        for command, expected, excluded in (
                ("record-ci-package", ("--release-manifest", "--verification", "--model-fingerprint"),
                    ("--build-record", "--package ")),
                ("validate-local", ("--release-manifest", "--local-evidence-dir"), ())):
            with self.subTest(command=command):
                output = io.StringIO()
                with redirect_stdout(output), self.assertRaises(SystemExit) as shown:
                    evidence.main([command, "--help"])
                self.assertEqual(shown.exception.code, 0)
                for name in expected:
                    self.assertIn(name, output.getvalue())
                for name in excluded:
                    self.assertNotIn(name, output.getvalue())


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    unittest.main()
