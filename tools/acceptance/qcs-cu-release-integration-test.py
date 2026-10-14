#!/usr/bin/env python3
"""SYNTHETIC recorder-to-release contract test. Never a real CU acceptance.

All ten fake passed scenarios, images, logs and reports are created in one
TemporaryDirectory and deleted before this test returns. The recorder's real
begin/observe/finish/summarize flow produces the raw report. The requested release
tool's real verify_manifest and validate_cu functions consume it without patching
or manufacturing report fields, both before and after collect_local copies it
into its new Computer Use unit root. The candidate unit is a minimal import placeholder.
This checks only CU import and its preflight sub-gate, not full release preflight.
No UI, real candidate, git or subprocess runs.
"""
from __future__ import annotations

import argparse
import base64
from contextlib import ExitStack, redirect_stdout
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
from unittest import mock

sys.dont_write_bytecode = True
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII="
)


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ValueError(f"Cannot import tool: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def require(condition, description):
    if not condition:
        raise AssertionError(description)


def fake_hash(label):
    return hashlib.sha256(("SYNTHETIC CONTRACT TEST ONLY: " + label).encode()).hexdigest()


def invoke(function, args):
    captured = io.StringIO()
    with redirect_stdout(captured):
        result = function(args)
    return result, captured.getvalue()


def integration(recorder_path, release_path):
    with ExitStack() as guard:
        # Neither producer nor consumer may launch any process in this test.
        for name in ("check_output", "run", "Popen", "call", "check_call"):
            guard.enter_context(mock.patch.object(
                subprocess, name, side_effect=AssertionError("Subprocess forbidden in SYNTHETIC contract test")
            ))
        recorder = load_module("synthetic_cu_contract_recorder", recorder_path)
        release = load_module("synthetic_cu_contract_release", release_path)
        with tempfile.TemporaryDirectory(prefix="qcs-CU-SYNTHETIC-CONTRACT-", dir=os.environ.get("QCOPILOTS_SEARCH_TEST_ROOT") or None) as temporary:
            root = Path(temporary).resolve()
            for folder in ("inputs", "evidence", "profile", "models", "samples", "exports", "incoming"):
                (root / folder).mkdir()
            (root / "SYNTHETIC-TEST-NOT-ACCEPTANCE.txt").write_text(
                "Contract test only. No person or agent performed any UI action. Delete this whole temporary directory.\n",
                encoding="utf-8",
            )
            identity = {
                "candidateHead": fake_hash("candidate")[:40],
                "checkoutCommit": fake_hash("candidate")[:40],
                "sourceTree": fake_hash("tree")[:40],
                "workingTreeModified": False,
                "lockHashes": {name: fake_hash("lock " + name) for name in release.LOCKS},
            }
            candidate = {
                "schemaVersion": 1, "kind": "package-candidate", "syntheticTestOnly": True,
                "identity": identity, "packageSha256": fake_hash("not a real package"),
                "modelFingerprint": "SYNTHETIC-model:" + fake_hash("not a real model"),
                "extractionFingerprint": "SYNTHETIC-extraction:" + fake_hash("not a real extraction"),
                "executables": {name: {
                    "sha256": fake_hash("not a real executable " + name),
                    "packageMember": "SYNTHETIC/bin/" + name + ".exe",
                } for name in recorder.NAMES},
            }
            epoch = datetime(2020, 1, 1, tzinfo=timezone.utc)
            timestamp = lambda offset: (epoch + timedelta(seconds=offset)).isoformat()
            state = {
                "schemaVersion": 1, "kind": "qcopilots-search-cu-run", "syntheticTestOnly": True,
                "runId": "SYNTHETIC-CONTRACT-TEST-NOT-REAL-ACCEPTANCE",
                "startedAt": timestamp(0), "identity": deepcopy(identity),
                "packageSha256": candidate["packageSha256"],
                "executables": deepcopy(candidate["executables"]),
                "verifiedExecutables": {name: {
                    **item, "bytes": 123, "absolutePath": str(root / "unused-bin" / (name + ".exe"))
                } for name, item in candidate["executables"].items()},
                "modelFingerprint": candidate["modelFingerprint"],
                "extractionFingerprint": candidate["extractionFingerprint"],
                "platform": {"system": "Windows", "architecture": "x86_64"},
                "profileRoot": str(root / "profile"), "modelDirectory": str(root / "models"),
            }
            # No candidate capture/init is claimed. Only load_run is replaced so
            # the actual producer flow can exercise its output schema safely.
            recorder.write(root / "run.json", state)
            recorder.write(root / "inputs" / "package-candidate.json", candidate)
            pixel = root / "incoming" / "SYNTHETIC-one-pixel.png"
            pixel.write_bytes(PNG)
            guard.enter_context(mock.patch.object(recorder, "load_run", return_value=(root, state)))
            clock = guard.enter_context(mock.patch.object(recorder, "now", return_value=timestamp(0)))
            for number, scenario in enumerate(recorder.CASES, start=1):
                clock.return_value = timestamp(number * 10)
                invoke(recorder.begin, SimpleNamespace(
                    run=root, scenario=scenario, expected=f"SYNTHETIC {scenario} schema expectation. No UI action."
                ))
                clock.return_value = timestamp(number * 10 + 1)
                entry_path = root / "incoming" / f"SYNTHETIC-{scenario}.json"
                recorder.write(entry_path, {
                    "syntheticTestOnly": True,
                    "utc": clock.return_value,
                    "action": f"SYNTHETIC {scenario} action text for contract validation only",
                    "actual": "SYNTHETIC observation. This image is a test pixel, not a real screenshot.",
                    "operator": "tool-observation",
                    "window": {"id": f"SYNTHETIC-WINDOW-{number}", "title": "NOT A REAL WINDOW"},
                    "packageSha256": candidate["packageSha256"],
                    "screenshots": [str(pixel)],
                })
                invoke(recorder.observe, SimpleNamespace(run=root, scenario=scenario, entry=entry_path))
                clock.return_value = timestamp(number * 10 + 2)
                invoke(recorder.finish, SimpleNamespace(
                    run=root, scenario=scenario, status="passed", assistance=None,
                    actual="SYNTHETIC case passed only for producer/consumer schema compatibility.",
                ))
            clock.return_value = timestamp(120)
            exit_code, captured = invoke(recorder.summarize, SimpleNamespace(run=root))
            require(exit_code == 0, f"Synthetic recorder summarize failed: {captured}")
            produced = json.loads(captured.strip().splitlines()[-1])
            report_path = Path(produced["report"]).resolve()
            require(report_path.parent == root, "Raw report must be at runRoot so its relative evidence paths resolve")
            raw = release.read_json(report_path)
            require(raw.get("runId") == state["runId"], "Synthetic run identity was lost")
            require(raw.get("passed") is True and len(raw.get("scenarios", [])) == 10,
                    "Actual recorder flow did not produce all ten synthetic passed scenarios")
            require(raw.get("executables") == candidate["executables"],
                    "Raw executable identities differ from the original candidate object")
            require(all("absolutePath" not in item and "bytes" not in item for item in raw["executables"].values()),
                    "Verified executable locations/sizes leaked into canonical candidate identities")
            paths = [item["path"] for item in raw.get("files", [])]
            require(paths and len(paths) == len(set(paths)), "Raw report needs unique embedded file hash bindings")
            bound = set(paths)
            for scenario in raw["scenarios"]:
                require(scenario["executables"] == candidate["executables"], "Scenario executable identities changed")
                require(scenario["operationLog"] in bound, "Operation log is missing from the raw file manifest")
                require(all(picture in bound for picture in scenario["screenshots"]),
                        "Scenario screenshot is missing from the raw file manifest")
                operations = release.read_json(recorder.safe(root, scenario["operationLog"]))
                require(all(operation["sourceRecord"] in bound for operation in operations),
                        "Original observation JSON is missing from the raw file manifest")
            # These are the real consumer functions, called directly on the
            # unmodified raw report returned by the real producer functions.
            manifest_errors = release.verify_manifest(report_path.parent, raw)
            cu_errors = release.validate_cu(
                raw, report_path.parent, identity=candidate["identity"],
                package_sha256=candidate["packageSha256"], model_fingerprint=candidate["modelFingerprint"],
            )
            require(manifest_errors == [], f"release.verify_manifest rejected recorder raw: {manifest_errors}")
            require(cu_errors == [], f"release.validate_cu rejected recorder raw: {cu_errors}")

            # collect_local binds the candidate and Computer Use units. The
            # candidate placeholder only tests import, never a release gate.
            records = {"computerUse": report_path}
            for name in ("candidate",):
                placeholder_root = root / "SYNTHETIC-import-placeholders" / name
                placeholder_root.mkdir(parents=True)
                payload = placeholder_root / "SYNTHETIC-NOT-RELEASE-EVIDENCE.txt"
                payload.write_text(
                    f"SYNTHETIC {name} import placeholder only. No acceptance or release gate was performed.\n",
                    encoding="utf-8",
                )
                placeholder_path = placeholder_root / f"SYNTHETIC-{name}-manifest.json"
                release.write_json(placeholder_path, {
                    "schemaVersion": 1, "kind": "synthetic-import-placeholder", "syntheticTestOnly": True,
                    "identity": deepcopy(identity), "files": [release.file_record(placeholder_root, payload)],
                })
                records[name] = placeholder_path
            collected_path, collected = release.collect_local(records, root / "SYNTHETIC-collection-destination")
            copied_path = release.safe_file(collected_path.parent, collected["units"]["computerUse"])
            require(copied_path != report_path and copied_path.parent == collected_path.parent / "computerUse",
                    "collect_local did not relocate CU raw into its separate Computer Use unit root")
            copied_raw = release.read_json(copied_path)
            require(copied_raw == raw, "collect_local changed the raw CU report during import")
            copied_bound = {item["path"] for item in copied_raw["files"]}
            for scenario in copied_raw["scenarios"]:
                for relative in [scenario["operationLog"], *scenario["screenshots"]]:
                    require(relative in copied_bound, "Copied scenario attachment has no embedded hash binding")
                    require(release.safe_file(copied_path.parent, relative).is_file(),
                            "Copied scenario attachment does not exist under the new CU unit root")
                operations = release.read_json(release.safe_file(copied_path.parent, scenario["operationLog"]))
                for operation in operations:
                    require(operation["sourceRecord"] in copied_bound,
                            "Copied original observation has no embedded hash binding")
                    release.safe_file(copied_path.parent, operation["sourceRecord"])
            copied_manifest_errors = release.verify_manifest(copied_path.parent, copied_raw)
            copied_cu_errors = release.validate_cu(
                copied_raw, copied_path.parent, identity=candidate["identity"],
                package_sha256=candidate["packageSha256"], model_fingerprint=candidate["modelFingerprint"],
            )
            require(copied_manifest_errors == [],
                    f"release.verify_manifest rejected the copied CU unit: {copied_manifest_errors}")
            require(copied_cu_errors == [],
                    f"release.validate_cu rejected the copied CU unit: {copied_cu_errors}")
            summary = {
                "kind": "synthetic-recorder-release-contract-test",
                "testPassed": True,
                "realAcceptancePerformed": False,
                "syntheticScenarioCount": len(raw["scenarios"]),
                "embeddedFileCount": len(paths),
                "canonicalExecutablesPreserved": True,
                "operationLogsAndScreenshotsHashBound": True,
                "verifyManifestErrors": manifest_errors,
                "validateCuErrors": cu_errors,
                "collectLocalCuImportValidated": True,
                "copiedAttachmentsHashBoundAndPresent": True,
                "copiedVerifyManifestErrors": copied_manifest_errors,
                "copiedValidateCuErrors": copied_cu_errors,
                "fullReleasePreflightPerformed": False,
                "scope": "SYNTHETIC CU import and CU preflight sub-gate only. The candidate unit is an import placeholder.",
            }
        require(not root.exists(), "Synthetic passed raw report was not deleted with its temporary directory")
        summary["temporaryEvidenceDeleted"] = True
        return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release-tool", required=True, type=Path,
                        help="Actual release-evidence Python tool to import read-only")
    parser.add_argument("--recorder", type=Path,
                        default=Path(__file__).with_name("qcs-cu-recorder.py"))
    args = parser.parse_args(argv)
    try:
        result = integration(args.recorder.resolve(strict=True), args.release_tool.resolve(strict=True))
    except Exception as error:
        print(json.dumps({"kind": "synthetic-recorder-release-contract-test", "testPassed": False,
                          "realAcceptancePerformed": False, "error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
