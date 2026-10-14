#!/usr/bin/env python3
"""Exercise release evidence validation with synthetic, in-memory counterexamples.

These documents are test inputs, never application acceptance reports. No model,
application process, source-tree temporary directory, or developer profile is used.
"""
from collections import Counter
import base64
from copy import deepcopy
import hashlib
import importlib.util
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
        "standardLicenseIds": ["MIT"], "standardLicenseFiles": {"MIT": "qt/sources/qtbase/LICENSES/MIT.txt"}}),
        encoding="utf-8")
    return {"passed": True, "files": len(files), "qtModules": list(modules), "standardLicenses": 1,
            "manifest": manifest.relative_to(root).as_posix(),
            "manifestSha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
            "installedManifestPath": str(application / "share/qcs/licenses/qcs-runtime" / manifest.name)}


def package_release_fixture(root, *, worker=None, model_fingerprint="synthetic-model-fingerprint",
                            extraction_fingerprint="synthetic-extraction-fingerprint"):
    """Synthetic verifier input using immutable vectors, never an inference run."""
    root.mkdir(parents=True, exist_ok=True)
    if worker is None:
        worker = root / "synthetic application 中文" / "qcs-worker.exe"
        worker.parent.mkdir()
        worker.write_bytes(b"SYNTHETIC VALIDATOR WORKER, NOT AN EXECUTABLE")
    application = worker.parent.resolve()
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
    return {"syntheticValidatorFixture": True, "passed": True, "releaseComplete": True,
            "verificationScope": "release", "realModelRequired": True,
            "applicationRoot": str(application), "licenseMaterials": package_license_fixture(root, application),
            "modelSmoke": {"passed": True, "dimensions": 768, "minimumCosine": 0.999,
                "modelDirectory": str(model_directory),
                "modelVerification": {"valid": True, "directory": str(model_directory), "artifacts": artifacts},
                "referenceHashes": {name: hashlib.sha256((fixtures / name).read_bytes()).hexdigest()
                                    for name in ("screen.png", "speech.wav", "vectors.json")},
                "vectors": vectors,
                "runtime": {"linkage": "shared-cpu-dispatch", "architecture": "amd64", "cpuBaselineForced": False,
                    "workerExecutable": {"path": str(worker.resolve()), "bytes": worker.stat().st_size,
                                         "sha256": hashlib.sha256(worker.read_bytes()).hexdigest()},
                    "libraries": libraries},
                "serviceSemantic": {"semantic": True, "warning": "", "hits": [{"path": "synthetic-notes.txt"}]}}}


class PackageReleaseEvidenceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="qcs-package-evidence-中文 ",
            dir=os.environ.get("QCOPILOTS_SEARCH_TEST_ROOT") or None)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.doc = package_release_fixture(self.root)

    def inspect(self, doc=..., *, platform_id="windows-x64", **fingerprints):
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

    def test_complete_synthetic_golden_reports_validate_without_running_models(self):
        self.assertEqual(self.inspect(), [])

    def test_static_runtime_is_valid_only_for_apple_silicon(self):
        runtime = self.doc["modelSmoke"]["runtime"]
        runtime.update({"linkage": "static-cpu", "architecture": "arm64", "libraries": []})
        self.assertEqual(self.inspect(platform_id="macos-arm64"), [])
        for platform_id in ("windows-x64", "linux-x64", "macos-x64"):
            with self.subTest(platform_id=platform_id):
                self.assertTrue(self.inspect(platform_id=platform_id))

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


def capacity_fixture():
    """Small validator fixture shaped like the native synthetic-capacity report."""
    status = {"annGeneration": 11, "annVectors": 10000, "files": 1000,
              "generation": 11, "pendingShards": 0, "segments": 10000,
              "vectors": 10000, "recoveryWarning": "", "maintenanceWarning": "",
              "schemaVersion": 2, "pipelineFingerprint": "synthetic-capacity-768-v1"}
    faults = []
    for i, name in enumerate(("committed-before-ann", "uncommitted-sqlite",
                             "ann-file-saved-before-manifest", "ann-manifest-published-before-outbox-cleanup")):
        version = 0 if i == 1 else i
        faults.append({"faultPoint": name, "childExitCode": 73, "passed": True,
                       "expectedVersion": f"validator-version-{version}", "observedVersion": f"validator-version-{version}",
                       "expectedToken": f"validator-token-{version}", "observedToken": f"validator-token-{version}",
                       "expectedVectorDimension": 700 + version, "observedVectorDimension": 700 + version,
                       "observedVectorCosine": 1.0, "vectorMatched": True,
                       "previousSegmentId": (0, 1, 1, 2)[i], "observedSegmentId": (1, 1, 2, 3)[i],
                       "staleVersionHits": 0, "oldKeywordHits": 0, "uncommittedCorruptionRows": 0})
    faults[-1].update(injectedSameShardEvents=2048, postRecoveryWritePassed=True,
                      postRecoveryWriteMilliseconds=10, stalePublishedOutboxEvents=0)
    return {
        "formatVersion": 1, "benchmark": "synthetic-capacity", "passed": True,
        "dimensions": 768, "segments": 10000, "files": 1000,
        "segmentsPerFile": 10, "seed": "test-fixture-only",
        "generatorVersion": "splitmix64-v1-cluster64-noise008-uniform10percent",
        "startedUtc": "2026-01-01T00:00:00Z", "finishedUtc": "2026-01-01T00:01:00Z",
        "builtStatus": deepcopy(status), "reopenedStatus": deepcopy(status),
        "queries": [{"query": n, "distribution": "uniform" if n == 0 else "clustered",
                     "recallAt20": 1.0, "returnedFiles": 20, "sourceFile": n}
                    for n in range(10)],
        "recallAt20": 1.0, "uniformRecallAt20": 1.0, "clusteredRecallAt20": 1.0,
        "recallTarget": 0.95, "recallPassed": True,
        "oracleMethod": "SQLite read-only stream, independent FP16 decode, cosine, best segment per file, exact top-20 heaps",
        "oracleRows": 10000, "oracleMilliseconds": 1,
        "warmupQueries": 10, "timedQueries": 20,
        "latencyMilliseconds": {"p50": 20.0, "p95": 30.0, "p99": 40.0},
        "p95TargetMilliseconds": 500, "latencyPassed": True,
        "latencyIncludesQueryEncoding": False,
        "filters": [{"nominalPercent": percent, "checkedQueries": 10,
                     "corpusFiles": 1000, "oracleRows": rows, "oracleMilliseconds": 1,
                     "oracleMethod": "SQLite read-only stream, independent FP16 decode, cosine, best segment per file, exact top-20 heaps",
                     "p95Milliseconds": 30.0, "passed": True, "recallAt20": 1.0}
                    for percent, rows in ((10, 1000), (1, 100), (0.1, 10))],
        "filtersPassed": True, "unfilteredHundredPercentCoveredByBaseline": True,
        "concurrentRebuildEvaluated": True,
        "concurrentRebuild": {"overlappingQueries": 2, "p50Milliseconds": 100.0,
                              "p95Milliseconds": 200.0, "p99Milliseconds": 200.0,
                              "passed": True, "rebuildMilliseconds": 200,
                              "singleShard": True, "timingClock": "steady-clock-relative-nanoseconds",
                              "observedBuilds": 1, "measuredQueries": 3,
                              "buildIntervals": [{"startNanoseconds": 100000000, "endNanoseconds": 300000000}],
                              "queryIntervals": [{"startNanoseconds": start, "endNanoseconds": end,
                                                   "durationMilliseconds": (end - start) / 1000000,
                                                   "overlapsBuild": overlap}
                                                  for start, end, overlap in ((0, 100000000, False),
                                                      (100000000, 200000000, True), (200000000, 400000000, True))]},
        "mutations": {"filesPerPhase": 1000, "vectorsPerPhase": 10000,
                      "passed": True, "addMilliseconds": 10, "updateMilliseconds": 10,
                      "deleteMilliseconds": 10, "queryScope": "filtered-exact-file-plus-global-production-search-sample",
                      "phases": [{"phase": phase, "passed": True, "filesChecked": 1000,
                                  "keywordQueries": 1000, "vectorQueries": 1000, "staleHitCount": 0,
                                  "retiredSegmentIdsChecked": 0 if phase == "add" else 10000,
                                  "retiredSegmentIdsRemaining": 0, "globalSearchQueries": 16,
                                  "globalSearchCheckedHits": 1, "globalSearchStaleHitCount": 0,
                                  "globalSearchVectorCount": 10000 if phase == "delete" else 20000,
                                  "globalSearchBackend": "exact-subset"}
                                 for phase in ("add", "update", "delete")]},
        "recoveryFaultInjectionEvaluated": True, "annManifestInterruptionEvaluated": True,
        "publishedManifestOutboxInterruptionEvaluated": True,
        "recoveryFaults": faults,
        "minimumFreeBytes": 3 * 1024**3, "minimumObservedFreeBytes": 5 * 1024**3,
        "directoryBudgetBytes": 7 * 1024**3, "sampledDirectoryPeakBytes": 100 * 1024**2,
        "projectedBytesIncludingTenPercentHeadroom": 110 * 1024**2,
        "pilotBytes": 80 * 1024**2, "pilotSegments": 10000,
        "diskMeasurement": "Sampled at batch and phase boundaries, not a continuous peak measurement",
        "cleanupSucceeded": True, "keep": False,
        "physicalFileTraversalEvaluated": False, "semanticQualityEvaluated": False,
    }


def semantic_fixture():
    """Build the frozen v2 counts with synthetic labels, never inference evidence."""
    modalities = ("text", "ocr", "image", "pdf", "speech", "environment", "video", "code", "image-natural", "video-natural")
    files = [{"file": f"fixture-{i}.txt", "modality": modalities[i % 10], "segments": 6,
              "sha256": f"{i + 1:064x}", "extractedModalities": {"text": 6}, "extractedTextCharacters": 1000}
             for i in range(186)]
    queries, negatives = [], []
    for partition, count, offset in (("development", 119, 0), ("holdout", 40, 119)):
        for n in range(count):
            topic = f"{partition}-topic-{n}"
            modality = modalities[n % 10]
            channel = "code" if modality == "code" else "audio" if modality in ("speech", "environment") else (
                "image" if modality in ("image", "image-natural", "video", "video-natural") else "text")
            metadata = {"topic": topic, "partition": partition,
                        "family": ("holdout:" if partition == "holdout" else "family:") + topic,
                        "modality": modality, "code": modality == "code"}
            if partition == "development":
                metadata["fold"] = 0 if n < 37 else 1 if n < 78 else 2
            name = files[offset + n]["file"]
            for language in ("en", "zh"):
                queries.append({**metadata, "query": f"validator fixture {partition} {n} {language}",
                    "language": language, "policyModality": channel, "expectedFile": name, "rank": 1,
                    "encodingMilliseconds": 1, "retrievalMilliseconds": 2,
                    "productionTop10": [{"file": name, "score": 1 / 61, "source": "semantic"}],
                    "exactSemanticTop10": [{"file": name, "cosine": .9}]})
                if partition == "holdout" or n < 15 or 37 <= n < 50 or 78 <= n < 98:
                    negatives.append({**metadata, "query": f"absent fixture {partition} {n} {language}",
                        "language": language, "returnedCount": 0, "productionTop10": [],
                        "exactSemanticTop10": [{"file": files[0]["file"], "cosine": .2}]})
    def metrics(rows, exact=False):
        counts = Counter(key for row in rows for key in
            ("overall", "language:" + row["language"], "modality:" + row["modality"]))
        return {key: {"queries": count, "hits": count, "recallAt10": 1., "passed": True,
                       "requiredRecallAt10": .9 if key == "overall" else .8,
                       **({"exactSemanticRecallAt10": 1.} if exact else {})} for key, count in counts.items()}
    corpus = {"fixtureManifestSha256": "3" * 64, "generatorSha256": "4" * 64,
              "topicManifestSha256": "5" * 64, "relevanceManifestSha256": "6" * 64,
              "groupPlanSha256": "9" * 64, "v2TextCodeManifestSha256": "a" * 64,
              "v2MediaManifestSha256": "b" * 64}
    policy = {"version": "qcs-relevance-v2-quarter-123456789abc", "calibrated": True,
              "modelFingerprint": "qcopilots-search-embedding-v1:" + "1" * 64,
              "corpusSha256": hashlib.sha256(json.dumps(corpus, ensure_ascii=False,
                  sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest(),
              "rawExportSha256": "7" * 64, "calibrationReportSha256": "8" * 64,
              "thresholds": {key: .5 for key in ("text", "image", "audio", "code")}}
    return {
        "schema": 1, "mode": "release", "status": "passed", "passed": True,
        "calibrationProtocol": "grouped-cv-v2-quarter", "metricsScope": "all-production-queries",
        "finalHoldoutEvaluated": True,
        "semanticCandidateMode": "matches-only", "relevancePolicy": policy, "corpusIdentity": corpus,
        "actualEmbeddedSegments": 1116, "completedFiles": len(files), "fixtures": len(files),
        "files": files, "queries": queries, "positiveQueries": 318, "metrics": metrics(queries, True),
        "partitionMetrics": {partition: {"passed": True, "metrics": metrics(
            [row for row in queries if row["partition"] == partition])} for partition in ("development", "holdout")},
        "modelFingerprint": "qcopilots-search-embedding-v1:" + "1" * 64,
        "extractionFingerprint": "qcopilots-search-extraction-v3:" + "2" * 64,
        **{key: value for key, value in corpus.items() if key in (
            "fixtureManifestSha256", "generatorSha256", "topicManifestSha256", "relevanceManifestSha256")},
        "annotationProtocol": "Synthetic validator fixture, not acceptance evidence.",
        "corpusScope": "Synthetic validator fixture, not an executed corpus.",
        "noAnswerInterpretation": "Synthetic fixture exercises calibrated rejection validation.",
        "noAnswerQueries": negatives,
        "noAnswerSummary": {partition: {"queries": count, "queriesReturningCandidates": 0,
                                         "falseReturnRate": 0, "maximumFalseReturnRate": .1, "passed": True,
                                         "minimumTopCosine": .2, "maximumTopCosine": .2, "meanTopCosine": .2}
                            for partition, count in (("development", 96), ("holdout", 80))},
        "startedAt": "2026-01-01T00:00:00Z", "finishedAt": "2026-01-01T00:01:00Z",
    }


def semantic_contract_fixture(doc):
    return {"identity": doc["corpusIdentity"],
            "development": {row["topic"]: {"family": row["family"], "fold": row["fold"]}
                            for row in doc["queries"] if row["partition"] == "development"},
            "holdout": {row["topic"] for row in doc["queries"] if row["partition"] == "holdout"}}


def physical_fixture():
    return {"schema": 1, "scenario": "physical-file-capacity", "passed": True,
            "createdPhysicalFiles": 100000, "remainingPhysicalFiles": 100000,
            "generatedFixture": True, "cleanupVerified": True, "staleResults": 0,
            "added": 100, "modified": 100, "deleted": 100,
            "cancelledAfterDiscoveredFiles": 100, "createMs": 1000, "scanMs": 200,
            "exclusionCancellationAndReconcileMs": 300, "payloadBytes": 1000000,
            "timestampUtc": "2026-01-01T00:00:00Z"}


class CapacityEvidenceTests(unittest.TestCase):
    def inspect(self, doc, **thresholds):
        limits = {"minimum_segments": 10000, "minimum_queries": 10, "minimum_timings": 20}
        limits.update(thresholds)
        result = evidence.validate_capacity(doc, **limits)
        self.assertIsInstance(result, list)
        self.assertTrue(all(isinstance(error, str) for error in result))
        return result

    def test_complete_small_fixture_meets_only_its_explicit_thresholds(self):
        self.assertEqual(self.inspect(capacity_fixture()), [])

    def test_smoke_cannot_claim_million_segment_acceptance(self):
        self.assertTrue(self.inspect(capacity_fixture(), minimum_segments=1000000))

    def test_too_few_oracle_queries_rejected(self):
        self.assertTrue(self.inspect(capacity_fixture(), minimum_queries=100))

    def test_too_few_timed_queries_rejected(self):
        self.assertTrue(self.inspect(capacity_fixture(), minimum_timings=500))

    def test_passed_alone_is_not_evidence(self):
        self.assertTrue(self.inspect({"passed": True}))

    def test_missing_filters_concurrency_and_recovery_rejected(self):
        for field in ("filters", "concurrentRebuild", "recoveryFaults"):
            with self.subTest(field=field):
                doc = capacity_fixture()
                del doc[field]
                self.assertTrue(self.inspect(doc))

    def test_missing_or_repeated_filter_selectivity_rejected(self):
        for replacement in ([], [capacity_fixture()["filters"][0]] * 3):
            doc = capacity_fixture()
            doc["filters"] = replacement
            self.assertTrue(self.inspect(doc))

    def test_no_query_overlap_cannot_claim_concurrent_rebuild(self):
        doc = capacity_fixture()
        doc["concurrentRebuild"]["overlappingQueries"] = 0
        self.assertTrue(self.inspect(doc))

    def test_reported_overlap_requires_real_interval_intersection(self):
        doc = capacity_fixture()
        doc["concurrentRebuild"]["buildIntervals"] = [{"startNanoseconds": 500000000, "endNanoseconds": 700000000}]
        self.assertTrue(self.inspect(doc))

    def test_concurrent_percentiles_and_durations_must_match_raw_intervals(self):
        for field in ("p50Milliseconds", "p95Milliseconds", "p99Milliseconds"):
            doc = capacity_fixture()
            doc["concurrentRebuild"][field] = 150.0
            self.assertTrue(self.inspect(doc))
        doc = capacity_fixture()
        doc["concurrentRebuild"]["queryIntervals"][1]["durationMilliseconds"] = 50.0
        self.assertTrue(self.inspect(doc))

    def test_concurrent_build_count_and_interval_boundary_are_enforced(self):
        doc = capacity_fixture()
        doc["concurrentRebuild"]["buildIntervals"] *= 2
        doc["concurrentRebuild"]["observedBuilds"] = 2
        self.assertTrue(self.inspect(doc))
        doc = capacity_fixture()
        doc["concurrentRebuild"]["queryIntervals"][0]["overlapsBuild"] = True
        self.assertTrue(self.inspect(doc))

    def test_mutation_phases_require_full_queries_and_retired_id_checks(self):
        for phase in range(3):
            for field, value in (("filesChecked", 999), ("keywordQueries", 999), ("vectorQueries", 999),
                                 ("globalSearchQueries", 15), ("globalSearchCheckedHits", 0), ("staleHitCount", 1),
                                 ("retiredSegmentIdsRemaining", 1), ("globalSearchStaleHitCount", 1),
                                 ("retiredSegmentIdsChecked", 1 if phase == 0 else 9999)):
                with self.subTest(phase=phase, field=field):
                    doc = capacity_fixture()
                    doc["mutations"]["phases"][phase][field] = value
                    self.assertTrue(self.inspect(doc))
        doc = capacity_fixture()
        doc["mutations"]["phases"][1] = deepcopy(doc["mutations"]["phases"][0])
        self.assertTrue(self.inspect(doc))

    def test_small_global_search_cannot_claim_ann_at_or_below_20000_vectors(self):
        for phase in range(3):
            doc = capacity_fixture()
            doc["mutations"]["phases"][phase]["globalSearchBackend"] = "ann"
            self.assertTrue(self.inspect(doc))

    def test_large_global_search_requires_ann_for_every_mutation_phase(self):
        doc = capacity_fixture()
        doc.update(segments=30000, files=3000, oracleRows=30000)
        for name in ("builtStatus", "reopenedStatus"):
            doc[name].update(segments=30000, vectors=30000, annVectors=30000, files=3000)
        for filtered in doc["filters"]:
            filtered["corpusFiles"] = 3000
            filtered["oracleRows"] *= 3
        for phase in doc["mutations"]["phases"]:
            phase["globalSearchVectorCount"] = 30000 if phase["phase"] == "delete" else 40000
            phase["globalSearchBackend"] = "ann"
        self.assertEqual(self.inspect(doc), [])
        for phase in range(3):
            incorrect = deepcopy(doc)
            incorrect["mutations"]["phases"][phase]["globalSearchBackend"] = "exact-subset"
            self.assertTrue(self.inspect(incorrect))

    def test_global_search_vector_count_must_match_committed_phase(self):
        for phase in range(3):
            doc = capacity_fixture()
            doc["mutations"]["phases"][phase]["globalSearchVectorCount"] -= 1
            self.assertTrue(self.inspect(doc))

    def test_recovery_requires_distinct_committed_versions_and_tokens(self):
        for key in ("Version", "Token"):
            doc = capacity_fixture()
            for prefix in ("expected", "observed"):
                doc["recoveryFaults"][2][prefix + key] = doc["recoveryFaults"][0][prefix + key]
            self.assertTrue(self.inspect(doc))

    def test_recovery_rejects_old_or_broken_segment_identity_chain(self):
        for phase, field, value in ((0, "previousSegmentId", 1), (2, "observedSegmentId", 1),
                                    (3, "previousSegmentId", 1), (1, "observedSegmentId", 2)):
            doc = capacity_fixture()
            doc["recoveryFaults"][phase][field] = value
            self.assertTrue(self.inspect(doc))

    def test_rollback_cannot_publish_uncommitted_values_or_leave_corruption(self):
        for field in ("staleVersionHits", "oldKeywordHits", "uncommittedCorruptionRows"):
            doc = capacity_fixture()
            doc["recoveryFaults"][1][field] = 1
            self.assertTrue(self.inspect(doc))
        doc = capacity_fixture()
        doc["recoveryFaults"][1].update(expectedVersion="uncommitted", observedVersion="uncommitted")
        self.assertTrue(self.inspect(doc))

    def test_recovery_vector_observation_must_match_expected_basis(self):
        for field, value in (("observedVectorDimension", 701), ("observedVectorCosine", 0.998),
                             ("observedVectorCosine", float("nan")), ("vectorMatched", False)):
            doc = capacity_fixture()
            doc["recoveryFaults"][0][field] = value
            self.assertTrue(self.inspect(doc))

    def test_unexecuted_recovery_fault_rejected(self):
        doc = capacity_fixture()
        doc["recoveryFaults"][0]["childExitCode"] = 0
        self.assertTrue(self.inspect(doc))

    def test_stale_outbox_or_failed_post_recovery_write_rejected(self):
        for field, value in (("stalePublishedOutboxEvents", 1), ("postRecoveryWritePassed", False)):
            doc = capacity_fixture()
            doc["recoveryFaults"][-1][field] = value
            self.assertTrue(self.inspect(doc))

    def test_nonfinite_metric_values_rejected(self):
        for value in (float("nan"), float("inf"), float("-inf")):
            for container, field in ((None, "recallAt20"), ("latencyMilliseconds", "p95"),
                                     ("concurrentRebuild", "p95Milliseconds")):
                with self.subTest(value=value, container=container, field=field):
                    doc = capacity_fixture()
                    (doc if container is None else doc[container])[field] = value
                    self.assertTrue(self.inspect(doc))

    def test_filtered_query_metrics_are_required(self):
        for field, value in (("checkedQueries", 9), ("recallAt20", 0.94),
                             ("p95Milliseconds", 501), ("recallAt20", float("nan"))):
            with self.subTest(field=field, value=value):
                doc = capacity_fixture()
                doc["filters"][0][field] = value
                self.assertTrue(self.inspect(doc))

    def test_each_recovery_fault_must_be_distinct_and_present(self):
        doc = capacity_fixture()
        doc["recoveryFaults"][1] = deepcopy(doc["recoveryFaults"][0])
        self.assertTrue(self.inspect(doc))

    def test_disk_budget_and_reserve_cannot_be_ignored(self):
        for field, value in (("sampledDirectoryPeakBytes", 8 * 1024**3),
                             ("minimumObservedFreeBytes", 2 * 1024**3)):
            doc = capacity_fixture()
            doc[field] = value
            self.assertTrue(self.inspect(doc))

    def test_query_recall_must_match_summary(self):
        doc = capacity_fixture()
        doc["queries"][0]["recallAt20"] = 0.0
        self.assertTrue(self.inspect(doc))

    def test_oracle_query_ids_must_be_distinct_valid_integers(self):
        for value in (1, None, "", True, -1, 1.5):
            with self.subTest(value=value):
                doc = capacity_fixture()
                doc["queries"][0]["query"] = value
                self.assertTrue(self.inspect(doc))

    def test_repeating_small_oracle_set_cannot_satisfy_larger_query_minimum(self):
        doc = capacity_fixture()
        doc["queries"] *= 10
        for filtered in doc["filters"]:
            filtered["checkedQueries"] = 100
        self.assertTrue(self.inspect(doc, minimum_queries=100))

    def test_reloaded_segment_count_must_match_written_count(self):
        doc = capacity_fixture()
        doc["reopenedStatus"]["segments"] -= 1
        self.assertTrue(self.inspect(doc))

    def test_exact_oracle_must_cover_every_written_segment(self):
        doc = capacity_fixture()
        doc["oracleRows"] -= 1
        self.assertTrue(self.inspect(doc))

    def test_unexpected_document_and_nested_shapes_return_errors(self):
        for value in (None, [], "passed", True, 1):
            with self.subTest(root=value):
                self.assertTrue(self.inspect(value))
        for field in ("builtStatus", "reopenedStatus", "latencyMilliseconds", "concurrentRebuild", "mutations"):
            for value in (None, [], "bad-shape"):
                with self.subTest(field=field, value=value):
                    doc = capacity_fixture()
                    doc[field] = value
                    self.assertTrue(self.inspect(doc))
        for field in ("queries", "filters", "recoveryFaults"):
            for value in (None, {}, "bad-shape", [None]):
                with self.subTest(field=field, value=value):
                    doc = capacity_fixture()
                    doc[field] = value
                    self.assertTrue(self.inspect(doc))

    def test_latency_cannot_raise_its_own_release_target(self):
        doc = capacity_fixture()
        doc["latencyMilliseconds"]["p95"] = 700
        doc["latencyMilliseconds"]["p99"] = 800
        doc["p95TargetMilliseconds"] = 1000
        self.assertTrue(self.inspect(doc))

    def test_boolean_or_string_is_not_a_numeric_observation(self):
        for value in (True, "10000"):
            doc = capacity_fixture()
            doc["segments"] = value
            self.assertTrue(self.inspect(doc))


class NsisContractEvidenceTests(unittest.TestCase):
    def fixture(self):
        identifier = "__main__.WorkflowTests.test_nsis_compiles_recursive_unicode_payload_beyond_max_path"
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
                       raw.replace("... ok", "... FAIL"), raw.replace("test_nsis_compiles_recursive_unicode_payload_beyond_max_path", "test_other"),
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


class CalibrationSnapshotBindingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="qcs-policy-binding-",
            dir=os.environ.get("QCOPILOTS_SEARCH_TEST_ROOT") or None)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.snapshots = self.root / "tests/semantic/calibration"
        self.snapshots.mkdir(parents=True)
        self.source = mock.patch.object(evidence, "SOURCE", self.root)
        self.source.start()
        self.addCleanup(self.source.stop)

    def bound_fixture(self, accepted=True):
        source = Path(__file__).resolve().parents[2]
        spec = importlib.util.spec_from_file_location("qcs_semantic_snapshot_fixture",
            source / "tools/qcs-semantic-policy-evidence-tests.py")
        fixtures = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fixtures)
        snapshot, policy, report = fixtures.fixture()
        snapshot["accepted"] = accepted
        path = self.snapshots / "reviewed.json"
        path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        policy["calibrationReportSha256"] = evidence.digest(path)
        report["relevancePolicy"] = deepcopy(policy)
        (self.root / "tools").mkdir()
        (self.root / "tools/qcs-semantic-policy-evidence.py").write_bytes(
            (source / "tools/qcs-semantic-policy-evidence.py").read_bytes())
        return policy, report

    def test_exact_bound_snapshot_is_loaded_and_independently_validated(self):
        self.assertEqual(evidence.relevance_calibration_errors(*self.bound_fixture()), [])

    def test_digest_matching_failed_snapshot_is_still_rejected(self):
        self.assertTrue(evidence.relevance_calibration_errors(*self.bound_fixture(accepted=False)))

    def test_self_claimed_accepted_document_without_compiled_digest_is_not_loaded(self):
        (self.snapshots / "accepted.json").write_text('{"accepted":true}', encoding="utf-8")
        self.assertTrue(evidence.relevance_calibration_errors({"calibrationReportSha256": "0" * 64}, {}))

    def test_missing_frozen_snapshot_is_not_replaced_by_report_claims(self):
        self.assertTrue(evidence.relevance_calibration_errors({"calibrationReportSha256": "1" * 64}, {}))

    def test_duplicate_byte_identical_snapshot_is_ambiguous(self):
        for name in ("one.json", "two.json"):
            (self.snapshots / name).write_bytes(b'{"accepted":true}\n')
        digest = evidence.digest(self.snapshots / "one.json")
        self.assertTrue(evidence.relevance_calibration_errors({"calibrationReportSha256": digest}, {}))

    def test_snapshot_changed_after_header_generation_fails_before_summary_parse(self):
        path = self.snapshots / "reviewed.json"
        path.write_bytes(b'{"accepted":true}\n')
        digest = evidence.digest(path)
        path.write_bytes(b'{"accepted":false}\n')
        self.assertTrue(evidence.relevance_calibration_errors({"calibrationReportSha256": digest}, {}))


class SemanticEvidenceTests(unittest.TestCase):
    def setUp(self):
        # Synthetic fixture headers never replace the real candidate's policy.
        fixture = semantic_fixture()
        self.compiled_policy = mock.patch.object(evidence, "compiled_relevance_policy",
                                                 return_value=fixture["relevancePolicy"])
        self.compiled_policy.start()
        self.addCleanup(self.compiled_policy.stop)
        self.contract = mock.patch.object(evidence, "semantic_corpus_contract", return_value=semantic_contract_fixture(fixture))
        self.contract.start()
        self.addCleanup(self.contract.stop)
        # Snapshot CV recomputation has its own independent helper test suite.
        self.calibration = mock.patch.object(evidence, "relevance_calibration_errors", return_value=[])
        self.calibration.start()
        self.addCleanup(self.calibration.stop)

    def test_complete_synthetic_validation_fixture(self):
        self.assertEqual(evidence.validate_semantic(semantic_fixture()), [])

    @staticmethod
    def recompute_positive_summaries(doc):
        for partition, summary in [(None, doc)] + list(doc["partitionMetrics"].items()):
            rows = [row for row in doc["queries"] if partition is None or row["partition"] == partition]
            for key, metric in summary["metrics"].items():
                group = [row for row in rows if key == "overall" or
                         key in ("language:" + row["language"], "modality:" + row["modality"])]
                metric["queries"] = len(group)
                metric["hits"] = sum(row["rank"] > 0 for row in group)
                metric["recallAt10"] = metric["hits"] / len(group)
                metric["passed"] = metric["recallAt10"] >= metric["requiredRecallAt10"]
            summary["passed"] = all(metric["passed"] for metric in summary["metrics"].values())

    def test_old_v1_or_midpoint_protocol_never_becomes_final_acceptance(self):
        for protocol in (None, "grouped-cv-v2", "v1", "calibration-holdout-v1"):
            doc = semantic_fixture()
            doc["calibrationProtocol"] = protocol
            with self.subTest(protocol=protocol):
                self.assertTrue(evidence.validate_semantic(doc))

    def test_final_holdout_must_be_actually_evaluated(self):
        for field, value in (("finalHoldoutEvaluated", False), ("finalHoldoutEvaluated", 1),
                             ("metricsScope", "development-only-raw-ranking")):
            doc = semantic_fixture()
            doc[field] = value
            self.assertTrue(evidence.validate_semantic(doc))
        doc = semantic_fixture()
        doc["partitionMetrics"]["holdout"] = {"status": "not-evaluated"}
        self.assertTrue(evidence.validate_semantic(doc))

    def test_each_new_corpus_hash_is_required_and_must_match_candidate_source(self):
        for name in ("groupPlanSha256", "v2TextCodeManifestSha256", "v2MediaManifestSha256"):
            for remove in (False, True):
                doc = semantic_fixture()
                if remove:
                    del doc["corpusIdentity"][name]
                else:
                    doc["corpusIdentity"][name] = "c" * 64
                with self.subTest(name=name, remove=remove):
                    self.assertTrue(evidence.validate_semantic(doc))

    def test_preserved_failed_calibration_snapshot_is_not_a_pass(self):
        with mock.patch.object(evidence, "relevance_calibration_errors", return_value=["Frozen grouped CV failed"]):
            self.assertIn("Frozen grouped CV failed", evidence.validate_semantic(semantic_fixture()))

    def test_negative_query_space_cannot_be_flipped_without_changing_labels(self):
        doc = semantic_fixture()
        doc["noAnswerQueries"][0]["code"] = not doc["noAnswerQueries"][0]["code"]
        self.assertTrue(evidence.validate_semantic(doc))

    def test_development_cannot_hide_weak_independent_holdout_recall(self):
        for group in ("overall", "modality:pdf"):
            doc = semantic_fixture()
            rows = [row for row in doc["queries"] if row["partition"] == "holdout"
                    and (group == "overall" or row["modality"] == "pdf")]
            for row in rows[:9 if group == "overall" else 2]:
                row.update(rank=0, productionTop10=[])
            self.recompute_positive_summaries(doc)
            self.assertTrue(doc["metrics"]["overall"]["passed"])
            # The overall summary cannot override the separate final-holdout gate.
            doc["passed"] = True
            errors = evidence.validate_semantic(doc)
            self.assertTrue(any(f"holdout {group}" in error for error in errors), errors)

    def test_missing_or_repeated_fixture_cannot_keep_the_old_denominators(self):
        for field, value in (("fixtures", 185), ("completedFiles", 185), ("positiveQueries", 317)):
            doc = semantic_fixture()
            doc[field] = value
            self.assertTrue(evidence.validate_semantic(doc))
        for replacement in (None, semantic_fixture()["files"][0]):
            doc = semantic_fixture()
            if replacement is None:
                doc["files"].pop()
            else:
                doc["files"][-1] = replacement
            self.assertTrue(evidence.validate_semantic(doc))

    def test_observed_v1_query_cannot_be_relabelled_as_new_holdout(self):
        doc = semantic_fixture()
        old, final = doc["queries"][0], doc["queries"][-1]
        old["partition"], final["partition"] = final["partition"], old["partition"]
        self.assertTrue(evidence.validate_semantic(doc))

    def test_fold_changes_and_heldout_fold_metadata_fail_closed(self):
        for field, value in (("fold", 2), ("fold", True), ("family", "other-family")):
            doc = semantic_fixture()
            doc["queries"][0][field] = value
            self.assertTrue(evidence.validate_semantic(doc))
        for field in ("queries", "noAnswerQueries"):
            doc = semantic_fixture()
            doc[field][-1]["fold"] = 0
            self.assertTrue(evidence.validate_semantic(doc))

    def test_same_expected_file_cannot_cross_frozen_families(self):
        doc = semantic_fixture()
        row = doc["queries"][2]
        other = doc["queries"][0]["expectedFile"]
        row.update(expectedFile=other, productionTop10=[{"file": other, "score": .01}],
                   exactSemanticTop10=[{"file": other, "cosine": .9}])
        self.assertTrue(any("expected file leaks" in error for error in evidence.validate_semantic(doc)))

    def test_passed_alone_is_not_model_quality_evidence(self):
        self.assertTrue(evidence.validate_semantic({"passed": True}))

    def test_corpus_and_query_minimums_are_enforced(self):
        for field, value in (("actualEmbeddedSegments", 999), ("positiveQueries", 199)):
            with self.subTest(field=field):
                doc = semantic_fixture()
                doc[field] = value
                self.assertTrue(evidence.validate_semantic(doc))

    def test_query_rank_must_match_actual_returned_candidates(self):
        doc = semantic_fixture()
        doc["queries"][0]["productionTop10"] = []
        self.assertTrue(evidence.validate_semantic(doc))

    def test_summary_hits_must_match_query_records(self):
        doc = semantic_fixture()
        doc["metrics"]["overall"]["hits"] = 199
        self.assertTrue(evidence.validate_semantic(doc))

    def test_summary_query_count_must_match_records(self):
        doc = semantic_fixture()
        doc["queries"].pop()
        self.assertTrue(evidence.validate_semantic(doc))

    def test_positive_query_text_must_be_nonempty_and_distinct(self):
        for value in (semantic_fixture()["queries"][1]["query"], "", "  ", None, True):
            with self.subTest(value=value):
                doc = semantic_fixture()
                doc["queries"][0]["query"] = value
                self.assertTrue(evidence.validate_semantic(doc))

    def test_missing_language_or_modality_cannot_hide_a_group(self):
        for key in ("language:zh", "modality:ocr", "modality:environment", "modality:code"):
            doc = semantic_fixture()
            del doc["metrics"][key]
            self.assertTrue(evidence.validate_semantic(doc))

    def test_no_answer_evidence_is_required(self):
        doc = semantic_fixture()
        doc["noAnswerQueries"] = []
        self.assertTrue(evidence.validate_semantic(doc))

    def test_no_answer_summary_must_match_records(self):
        doc = semantic_fixture()
        doc["noAnswerSummary"]["holdout"]["queries"] = 1
        self.assertTrue(evidence.validate_semantic(doc))

    def test_no_answer_holdout_gate_recomputes_rate_from_returned_lists(self):
        for count in (8, 9):
            doc = semantic_fixture()
            rows = [row for row in doc["noAnswerQueries"] if row["partition"] == "holdout"]
            for row in rows[:count]:
                row.update(returnedCount=1, productionTop10=[{"file": doc["files"][0]["file"], "score": .01}])
            doc["noAnswerSummary"]["holdout"].update(queriesReturningCandidates=count, falseReturnRate=count / 80)
            self.assertEqual(bool(evidence.validate_semantic(doc)), count > 8)

    def test_negative_count_cannot_hide_actual_results(self):
        doc = semantic_fixture()
        doc["noAnswerQueries"][0]["productionTop10"] = [{"file": "hidden-match.txt"}]
        self.assertTrue(evidence.validate_semantic(doc))

    def test_calibration_export_and_unbound_policy_cannot_be_release_evidence(self):
        for field, value in (("semanticCandidateMode", "raw-candidates"), ("mode", "calibration-export")):
            doc = semantic_fixture()
            doc[field] = value
            self.assertTrue(evidence.validate_semantic(doc))
        for field, value in (("calibrated", False), ("calibrated", 1), ("modelFingerprint", "other-model"),
                             ("corpusSha256", "0" * 64), ("rawExportSha256", ""),
                             ("calibrationReportSha256", "1" * 64), ("version", "different-version")):
            with self.subTest(field=field):
                doc = semantic_fixture()
                doc["relevancePolicy"][field] = value
                self.assertTrue(evidence.validate_semantic(doc))

    def test_threshold_change_nan_and_missing_modality_fail(self):
        for value in (.6, float("nan"), float("inf"), True, 2):
            doc = semantic_fixture()
            doc["relevancePolicy"]["thresholds"]["audio"] = value
            self.assertTrue(evidence.validate_semantic(doc))
        doc = semantic_fixture()
        del doc["relevancePolicy"]["thresholds"]["code"]
        self.assertTrue(evidence.validate_semantic(doc))

    def test_holdout_topics_must_be_independent(self):
        doc = semantic_fixture()
        doc["noAnswerQueries"][-1]["topic"] = doc["noAnswerQueries"][0]["topic"]
        self.assertTrue(evidence.validate_semantic(doc))

    def test_nonfinite_recall_and_cosine_rejected(self):
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value):
                doc = semantic_fixture()
                doc["metrics"]["overall"]["recallAt10"] = value
                self.assertTrue(evidence.validate_semantic(doc))
                doc = semantic_fixture()
                doc["queries"][0]["exactSemanticTop10"][0]["cosine"] = value
                self.assertTrue(evidence.validate_semantic(doc))

    def test_exact_summary_must_match_independent_oracle_records(self):
        doc = semantic_fixture()
        doc["queries"][0]["exactSemanticTop10"] = []
        self.assertTrue(evidence.validate_semantic(doc))

    def test_group_recall_cannot_be_hidden_by_overall_success(self):
        doc = semantic_fixture()
        for row in doc["queries"]:
            if row["modality"] == "environment":
                row["productionTop10"] = []
                row["rank"] = 0
        # Even internally consistent summaries cannot override the fixed 0.8 group gate.
        for key, metric in doc["metrics"].items():
            rows = [row for row in doc["queries"] if key == "overall" or
                    key == "language:" + row["language"] or key == "modality:" + row["modality"]]
            metric["hits"] = sum(row["rank"] > 0 for row in rows)
            metric["recallAt10"] = metric["hits"] / len(rows)
            metric["requiredRecallAt10"] = 0
        self.assertTrue(evidence.validate_semantic(doc))

    def test_unexpected_document_and_nested_shapes_return_errors(self):
        for value in (None, [], "passed", True, 1):
            with self.subTest(root=value):
                self.assertTrue(evidence.validate_semantic(value))
        for field in ("queries", "noAnswerQueries"):
            for value in (None, {}, "bad-shape", [None]):
                with self.subTest(field=field, value=value):
                    doc = semantic_fixture()
                    doc[field] = value
                    self.assertTrue(evidence.validate_semantic(doc))
        for field in ("metrics", "noAnswerSummary"):
            doc = semantic_fixture()
            doc[field] = None
            self.assertTrue(evidence.validate_semantic(doc))
        for field in ("productionTop10", "exactSemanticTop10"):
            for value in (None, {}, "bad-shape", [None]):
                with self.subTest(query_field=field, value=value):
                    doc = semantic_fixture()
                    doc["queries"][0][field] = value
                    self.assertTrue(evidence.validate_semantic(doc))


class PhysicalEvidenceTests(unittest.TestCase):
    def test_complete_synthetic_validation_fixture(self):
        self.assertEqual(evidence.validate_physical(physical_fixture()), [])

    def test_passed_alone_is_not_physical_traversal_evidence(self):
        self.assertTrue(evidence.validate_physical({"passed": True}))

    def test_physical_file_minimum_is_enforced(self):
        doc = physical_fixture()
        doc["createdPhysicalFiles"] = 99999
        self.assertTrue(evidence.validate_physical(doc))

    def test_custom_minimum_is_explicit(self):
        doc = physical_fixture()
        doc["createdPhysicalFiles"] = doc["remainingPhysicalFiles"] = 1000
        self.assertEqual(evidence.validate_physical(doc, minimum_files=1000), [])

    def test_stale_result_cleanup_and_unexecuted_mutations_rejected(self):
        for field, value in (("staleResults", 1), ("cleanupVerified", False),
                             ("added", 0), ("modified", 0), ("deleted", 0),
                             ("cancelledAfterDiscoveredFiles", 0)):
            with self.subTest(field=field):
                doc = physical_fixture()
                doc[field] = value
                self.assertTrue(evidence.validate_physical(doc))

    def test_nonfinite_or_non_numeric_counts_rejected(self):
        for value in (float("nan"), float("inf"), True, "100000"):
            doc = physical_fixture()
            doc["createdPhysicalFiles"] = value
            self.assertTrue(evidence.validate_physical(doc))

    def test_unexpected_document_shape_returns_errors(self):
        for value in (None, [], "passed", True, 1):
            with self.subTest(value=value):
                self.assertTrue(evidence.validate_physical(value))


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
            "schemaVersion": 1, "kind": "native", "platformId": "windows-x64",
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


class NativeSelectionBindingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="qcs-selection-binding-",
            dir=os.environ.get("QCOPILOTS_SEARCH_TEST_ROOT") or None)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.candidate = "1" * 40
        self.selection = {"kind": "ci-artifact-selection", "schemaVersion": 1,
            "candidateHead": self.candidate, "testProfile": "full", "runId": "123456",
            "aggregateAttempt": 3, "selected": {name: {} for name in evidence.PLATFORMS}}

    def selection_file(self, relative="selection.json"):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.selection), encoding="utf-8")
        return path

    def load(self):
        selector = mock.Mock()
        selector.load_selection.return_value = deepcopy(self.selection)
        spec = mock.Mock()
        with mock.patch.object(evidence.importlib.util, "spec_from_file_location", return_value=spec), \
             mock.patch.object(evidence.importlib.util, "module_from_spec", return_value=selector):
            result = evidence.load_native_selection(self.root, self.candidate, expected_run="123456")
        selector.load_selection.assert_called_once_with(evidence.native_selection_path(self.root),
            expected_head=self.candidate, expected_profile="full", expected_run="123456")
        return result

    def test_normalized_bundle_loads_with_explicit_candidate_profile_and_run(self):
        self.selection_file()
        self.assertEqual(self.load(), self.selection)

    def test_downloaded_aggregate_directory_preserves_exact_candidate_and_attempt(self):
        relative = f"qcs-native-aggregate-{self.candidate}-attempt3/selection.json"
        path = self.selection_file(relative)
        self.assertEqual(evidence.native_selection_path(self.root), path)
        self.assertEqual(self.load(), self.selection)

    def test_normalized_selection_takes_precedence_over_downloaded_copy(self):
        selected = self.selection_file()
        self.selection_file(f"qcs-native-aggregate-{self.candidate}-attempt3/selection.json")
        self.assertEqual(evidence.native_selection_path(self.root), selected)

    def test_missing_or_multiple_downloaded_aggregates_are_not_guessed(self):
        with self.assertRaises(ValueError):
            evidence.native_selection_path(self.root)
        for attempt in (2, 3):
            self.selection_file(f"qcs-native-aggregate-{self.candidate}-attempt{attempt}/selection.json")
        with self.assertRaises(ValueError):
            evidence.native_selection_path(self.root)

    def test_downloaded_aggregate_cannot_claim_another_attempt_or_head(self):
        path = self.selection_file(f"qcs-native-aggregate-{self.candidate}-attempt2/selection.json")
        with self.assertRaises(ValueError):
            self.load()
        path.parent.rename(self.root / ("qcs-native-aggregate-" + "2" * 40 + "-attempt3"))
        with self.assertRaises(ValueError):
            self.load()

    def test_selection_requires_all_four_exact_platforms(self):
        self.selection_file()
        del self.selection["selected"]["macos-x64"]
        with self.assertRaises(ValueError):
            self.load()

    def test_api_selection_failure_is_not_replaced_by_native_success(self):
        self.selection_file()
        selector = mock.Mock()
        selector.load_selection.side_effect = ValueError("Latest attempt failed")
        with mock.patch.object(evidence.importlib.util, "spec_from_file_location", return_value=mock.Mock()), \
             mock.patch.object(evidence.importlib.util, "module_from_spec", return_value=selector):
            with self.assertRaisesRegex(ValueError, "Latest attempt failed"):
                evidence.load_native_selection(self.root, self.candidate)

    def real_snapshot_fixture(self):
        spec = importlib.util.spec_from_file_location("qcs_selection_contract_fixture",
            Path(__file__).resolve().with_name("qcs-tests-artifact-selection.py"))
        fixtures = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fixtures)
        case = fixtures.ArtifactSelectionTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        case.artifacts = [fixtures.artifact(source, index + 1001, "full")
                          for index, source in enumerate(case.jobs)]
        retried = case.partial_retry()
        case.artifacts[-1] = fixtures.artifact(retried, 2001, "full")
        path = case.write_snapshot(self.root, attempt=2, profile="full")
        return fixtures.HEAD, fixtures.RUN, path

    def test_real_api_snapshot_recomputation_accepts_mixed_original_execution_attempts(self):
        candidate, run, _ = self.real_snapshot_fixture()
        selected = evidence.load_native_selection(self.root, candidate, expected_run=run)
        self.assertEqual(selected["selected"]["windows-x64"]["sourceAttempt"], 2)
        self.assertEqual(selected["selected"]["linux-x64"]["sourceAttempt"], 1)
        self.assertEqual(selected["aggregateAttempt"], 2)

    def test_real_api_snapshot_bytes_and_selected_ids_are_revalidated_offline(self):
        candidate, run, path = self.real_snapshot_fixture()
        original = path.read_bytes()
        document = json.loads(original)
        document["selected"]["linux-x64"]["artifactId"] += 99
        path.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaises(ValueError):
            evidence.load_native_selection(self.root, candidate, expected_run=run)
        path.write_bytes(original)
        raw = self.root / document["files"][0]["path"]
        raw.write_bytes(raw.read_bytes() + b"\n")
        with self.assertRaises(ValueError):
            evidence.load_native_selection(self.root, candidate, expected_run=run)

    def bound_runner(self, value=None):
        runner = value if value is not None else {"schemaVersion": 1, "candidateHead": self.candidate,
            "platformId": "linux-x64", "runId": "123456", "runAttempt": "1", "testProfile": "full"}
        path = self.root / "reports/runner-identity.json"
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(runner), encoding="utf-8")
        return runner, {"files": [evidence.file_record(self.root, path)]}, path

    def test_runner_identity_is_read_only_when_its_exact_bytes_are_manifest_bound(self):
        runner, manifest, _ = self.bound_runner()
        self.assertEqual(evidence.native_runner_identity(self.root, manifest), runner)
        self.assertIsNone(evidence.native_runner_identity(self.root, {"files": []}))

    def test_changed_missing_or_repeated_runner_evidence_is_rejected(self):
        _, manifest, path = self.bound_runner()
        altered = deepcopy(manifest)
        altered["files"].append(deepcopy(altered["files"][0]))
        with self.assertRaises(ValueError):
            evidence.native_runner_identity(self.root, altered)
        path.write_text("{}", encoding="utf-8")
        with self.assertRaises(ValueError):
            evidence.native_runner_identity(self.root, manifest)
        path.unlink()
        with self.assertRaises(ValueError):
            evidence.native_runner_identity(self.root, manifest)

    def test_runner_identity_must_be_an_object(self):
        _, manifest, _ = self.bound_runner([])
        with self.assertRaises(ValueError):
            evidence.native_runner_identity(self.root, manifest)

    def test_preflight_forwards_exact_selection_and_expected_run(self):
        path = self.selection_file()
        aggregate = {"passed": False, "errors": ["Missing native units"], "gates": {}}
        with mock.patch.object(evidence, "aggregate_native", return_value=aggregate) as validate:
            result = evidence.preflight({}, self.root / "missing-local", self.candidate,
                selection_path=path, expected_run="123456")
        validate.assert_called_once_with({}, self.candidate, selection_path=path, expected_run="123456")
        self.assertFalse(result["passed"])

    def test_aggregate_cli_forwards_current_ci_run_and_attempt(self):
        roots = {"linux-x64": self.root / "synthetic-native"}
        output = self.root / "aggregate.json"
        with mock.patch.object(evidence, "discover_native", return_value=roots), \
             mock.patch.object(evidence, "aggregate_native", return_value={"passed": True}) as validate, \
             mock.patch("builtins.print"):
            code = evidence.main(["aggregate-native", "--candidate-commit", self.candidate,
                "--native-artifacts-dir", str(self.root), "--output", str(output),
                "--run-id", "123456", "--run-attempt", "3"])
        self.assertEqual(code, 0)
        validate.assert_called_once_with(roots, self.candidate, expected_run="123456", expected_attempt=3)


class NativeAggregateEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.candidate = "1" * 40
        self.identity = {"candidateHead": self.candidate, "checkoutCommit": "2" * 40,
                         "sourceTree": "3" * 40, "workingTreeModified": False,
                         "lockHashes": {name: "4" * 64 for name in evidence.LOCKS}}
        # Selection API parsing and content binding have separate tests. Here
        # use four already inspected units to exercise aggregate identity joins.
        self.parent = Path("native-selection-fixture").resolve()
        self.selection_path = self.parent / "selection.json"
        self.selection = {"kind": "ci-artifact-selection", "schemaVersion": 1,
            "candidateHead": self.candidate, "testProfile": "full", "runId": "123456",
            "aggregateAttempt": 3, "selected": {name: {
                "sourceAttempt": 1 if name == "linux-x64" else 3,
                "artifactName": f"qcs-native-{name}-{self.candidate}-attempt{1 if name == 'linux-x64' else 3}"}
                for name in evidence.PLATFORMS}}
        self.roots = {name: self.parent / item["artifactName"] for name, item in self.selection["selected"].items()}
        self.actual_platforms = {path.name: name for name, path in self.roots.items()}
        self.gates = {name: {"passed": True, "errors": [], "platformId": name,
                             "identity": deepcopy(self.identity), "modelFingerprint": "model-fingerprint",
                             "extractionFingerprint": "extraction-fingerprint", "runnerIdentity": {
                                 "schemaVersion": 1, "platformId": name, "candidateHead": self.candidate,
                                 "testProfile": "full", "runId": "123456",
                                 "runAttempt": str(self.selection["selected"][name]["sourceAttempt"])}}
                      for name in self.roots}

    def inspect(self, expected_attempt=None):
        def inspect_platform(path, *, expected_head):
            self.assertEqual(expected_head, self.candidate)
            platform = self.actual_platforms[path.name]
            return deepcopy(self.gates[platform])
        with mock.patch.object(evidence, "inspect_native", side_effect=inspect_platform), \
             mock.patch.object(evidence, "native_selection_path", return_value=self.selection_path), \
             mock.patch.object(evidence, "load_native_selection", return_value=deepcopy(self.selection)):
            return evidence.aggregate_native(self.roots, self.candidate, selection_path=self.selection_path,
                                             expected_attempt=expected_attempt)

    def test_four_complete_consistent_platforms_pass(self):
        self.assertTrue(self.inspect()["passed"])

    def test_ci_requires_its_exact_aggregate_attempt_without_changing_offline_validation(self):
        self.assertTrue(self.inspect(expected_attempt=3)["passed"])
        self.assertTrue(self.inspect()["passed"])
        for attempt in (1, 2, 4, 0, -1, True, "3"):
            with self.subTest(attempt=attempt):
                result = self.inspect(expected_attempt=attempt)
                self.assertFalse(result["passed"])
                self.assertTrue(any("current CI attempt" in error for error in result["errors"]))

    def test_missing_platform_cannot_pass(self):
        del self.roots["macos-x64"]
        self.assertFalse(self.inspect()["passed"])

    def test_duplicate_platform_payload_cannot_fill_a_different_slot(self):
        self.roots["macos-x64"] = self.roots["macos-arm64"]
        self.assertFalse(self.inspect()["passed"])

    def test_runner_must_match_selected_source_attempt_not_aggregate_attempt(self):
        self.assertTrue(self.inspect()["passed"])
        self.gates["linux-x64"]["runnerIdentity"]["runAttempt"] = "3"
        self.assertFalse(self.inspect()["passed"])

    def test_another_artifact_directory_cannot_stand_in_for_the_selected_artifact(self):
        self.selection["selected"]["linux-x64"]["artifactName"] += "-unselected"
        result = self.inspect()
        self.assertFalse(result["passed"])
        self.assertTrue(any("not the selected artifact" in error for error in result["errors"]))

    def test_runner_head_profile_run_platform_or_type_changes_fail(self):
        original = deepcopy(self.gates)
        for field, value in (("schemaVersion", True), ("platformId", "windows-x64"),
                             ("candidateHead", "0" * 40), ("testProfile", "fast"),
                             ("runId", "654321"), ("runAttempt", 1)):
            self.gates = deepcopy(original)
            self.gates["linux-x64"]["runnerIdentity"][field] = value
            with self.subTest(field=field):
                self.assertFalse(self.inspect()["passed"])

    def test_old_inspectable_units_without_bound_runner_cannot_pass_new_aggregate(self):
        del self.gates["linux-x64"]["runnerIdentity"]
        self.assertFalse(self.inspect()["passed"])

    def test_no_selection_never_infers_legacy_latest_attempt(self):
        with mock.patch.object(evidence, "inspect_native", return_value=self.gates["linux-x64"]), \
             mock.patch.object(evidence, "native_selection_path", side_effect=ValueError("No selection")):
            result = evidence.aggregate_native(self.roots, self.candidate)
        self.assertFalse(result["passed"])
        self.assertTrue(any("No selection" in error for error in result["errors"]))

    def test_units_from_separate_parent_roots_are_not_combined(self):
        self.roots["linux-x64"] = self.parent / "unrelated-run" / self.roots["linux-x64"].name
        self.assertFalse(self.inspect()["passed"])

    def test_single_platform_errors_are_preserved(self):
        self.gates["linux-x64"]["passed"] = False
        self.gates["linux-x64"]["errors"] = ["required scenario not run"]
        result = self.inspect()
        self.assertFalse(result["passed"])
        self.assertTrue(any("required scenario not run" in error for error in result["errors"]))

    def test_mismatched_source_and_dependency_fingerprints_rejected(self):
        original = deepcopy(self.gates)
        for field, value in (("candidateHead", "0" * 40), ("checkoutCommit", "0" * 40),
                             ("sourceTree", "0" * 40), ("lockHashes", {})):
            with self.subTest(field=field):
                self.gates = deepcopy(original)
                self.gates["linux-x64"]["identity"][field] = value
                self.assertFalse(self.inspect()["passed"])

    def test_mismatched_model_or_extraction_fingerprints_rejected(self):
        original = deepcopy(self.gates)
        for field in ("modelFingerprint", "extractionFingerprint"):
            self.gates = deepcopy(original)
            self.gates["macos-arm64"][field] = "different-fingerprint"
            self.assertFalse(self.inspect()["passed"])


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
            "ExecutionProvenanceTests.test_cli_working_directory_records_actual_build_and_local_processes"]
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
                "coreTestsPassed": len(selected) - 1, "physicalBoundaryCapabilityUnavailable": 1,
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

    def candidate(self):
        build_path, build = self.build()
        package = self.root / "qcs-synthetic-validator.zip"
        with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_STORED) as archive:
            for name, path in self.programs.items():
                archive.write(path, "bin/" + name + ".exe")
        verification = self.root / "synthetic-package-verification.json"
        report = package_release_fixture(self.root, worker=self.programs["qcs-worker"])
        report.update({"commit": build["identity"]["checkoutCommit"], "workingTreeModified": False,
            "packageSha256": hashlib.sha256(package.read_bytes()).hexdigest(),
            "checks": [{"name": f"synthetic-validator-{n}", "passed": True} for n in range(9)]})
        verification.write_text(json.dumps(report), encoding="utf-8")
        return evidence.record_package(build_path, package, verification,
            self.root / "recorded candidates", "synthetic-model-fingerprint", "synthetic-extraction-fingerprint")

    def local_command(self, suffix="", *extra_arguments):
        return [sys.executable, "-c",
            "import datetime,json,pathlib,sys\n"
            "report=json.loads(sys.argv[2])\n"
            "report['timestampUtc']=datetime.datetime.now(datetime.timezone.utc).isoformat()\n"
            "pathlib.Path(sys.argv[1]).write_text(json.dumps(report),encoding='utf-8')\n" + suffix,
            "{report}", json.dumps(physical_fixture()), *extra_arguments]

    def run_local(self, build_path, command=None):
        return evidence.run_local(build_path, self.root / "recorded executions", "physical",
            "qcs-cli", "synthetic-physical-validator.json", command or self.local_command(), {},
            working_directory=self.working_directory)

    def test_cli_working_directory_records_actual_build_and_local_processes(self):
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
        local_command = self.local_command(
            f"assert pathlib.Path({input_argument!r}).read_text()=='explicit cwd'\n")
        result = subprocess.run([sys.executable, str(Path(evidence.__file__)), "run-local",
            "--build-record", str(build_path), "--output-root", str(parent),
            "--kind", "physical", "--program-name", "qcs-cli",
            "--report", "physical.json", "--working-directory", str(self.working_directory),
            "--", *local_command], cwd=self.working_directory, capture_output=True,
            text=True, encoding="utf-8", env=self.environment)
        self.assertEqual(result.returncode, 0, result.stderr)
        local_path = Path(json.loads(result.stdout)["record"])
        local = evidence.read_json(local_path)
        self.assertTrue(local["passed"])
        self.assertTrue(local_path.resolve().is_relative_to(parent.resolve()))
        self.assertEqual(evidence.inspect_local_run(local_path.parent, local, "physical", build["identity"]), [])
        for receipt in (build, local):
            execution = receipt["execution"]
            self.assertEqual(execution["requestedWorkingDirectory"], str(self.working_directory))
            self.assertTrue(os.path.samefile(execution["actualWorkingDirectory"], self.working_directory))

    def test_clean_fixture_build_and_package_provenance_are_inspectable(self):
        path, candidate = self.candidate()
        self.assertEqual(evidence.inspect_candidate(path.parent, candidate), [])
        build_path = path.parent / candidate["buildProvenance"]["path"]
        self.assertEqual(evidence.verified_build(build_path.parent, evidence.read_json(build_path)), [])

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

    def test_local_fixture_execution_has_bound_build_and_report(self):
        build_path, build = self.build()
        path, receipt = self.run_local(build_path)
        self.assertTrue(receipt["passed"])
        self.assertEqual(evidence.inspect_local_run(path.parent, receipt, "physical", build["identity"]), [])

    def test_changed_producer_before_local_run_is_rejected(self):
        build_path, _ = self.build()
        self.programs["qcs-cli"].write_bytes(b"changed producer")
        with mock.patch.object(evidence, "execute_record") as execute:
            with self.assertRaises(ValueError):
                self.run_local(build_path)
            execute.assert_not_called()

    def test_changed_source_before_local_run_is_rejected(self):
        build_path, _ = self.build()
        (self.source / evidence.LOCKS[0]).write_text("changed after build", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.run_local(build_path)

    def test_source_or_producer_change_during_local_run_cannot_pass(self):
        for target in (self.source / evidence.LOCKS[0], self.programs["qcs-cli"]):
            with self.subTest(target=target.name):
                original = target.read_bytes() if target.exists() else None
                build_path, _ = self.build()
                command = self.local_command("pathlib.Path(sys.argv[3]).write_bytes(b'changed during run')\n", str(target))
                _, receipt = self.run_local(build_path, command)
                self.assertFalse(receipt["passed"])
                if original is not None:
                    target.write_bytes(original)

    def test_missing_native_artifacts_exit_nonzero_with_json_failure(self):
        output = self.root / "cli-failure.json"
        result = subprocess.run([sys.executable, str(Path(evidence.__file__)), "aggregate-native",
            "--candidate-commit", "1" * 40, "--native-artifacts-dir", str(self.root / "absent artifacts"),
            "--output", str(output)], cwd=self.working_directory, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 1, result.stderr)
        report = json.loads(output.read_text(encoding="utf-8"))
        self.assertFalse(report["passed"])
        self.assertTrue(report["errors"])
        self.assertEqual(json.loads(result.stdout), report)


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    unittest.main()
