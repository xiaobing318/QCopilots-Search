#!/usr/bin/env python3
"""Exercise real index GPU workers through isolated production service/CLI processes.

This is an opt-in hardware check, not a replacement for the normal test suite.
Each run starts fresh processes and an empty index. Times include initialization
and filesystem cache effects, so their ratios are observations, not a benchmark.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

sys.dont_write_bytecode = True
SOURCE = Path(__file__).resolve().parents[1]
TOKEN = "qcsgpucheckcorpus"


def device_key(device):
    backend = device.get("backend", "").lower()
    identity = device.get("deviceId") or device.get("name")
    if not backend or backend == "cpu" or not identity:
        raise RuntimeError("GPU identity requires a non-CPU backend and device ID or unique name")
    return backend, identity


def selected_devices(probe):
    devices = [entry for entry in probe.get("devices", []) if entry.get("gpu") and entry.get("available")]
    if not devices:
        raise RuntimeError("No compatible GPU can be selected. CPU fallback does not pass this hardware check")
    identities = [device_key(entry) for entry in devices]
    if len(set(identities)) != len(identities):
        raise RuntimeError("GPU probe returned duplicate device identities")
    if any(entry[0] != probe.get("packagedBackend", "").lower() for entry in identities):
        raise RuntimeError("GPU probe returned a device outside the packaged backend")
    return devices


def selector(device):
    return {key: device.get(key, "") for key in ("backend", "deviceId", "name")}


def require_external(path):
    resolved = Path(path).resolve()
    if resolved == SOURCE or SOURCE in resolved.parents:
        raise RuntimeError("GPU check output and scratch directories must be outside the source tree")
    return resolved


def create_corpus(directory, count, paragraphs):
    directory.mkdir(parents=True, exist_ok=False)
    manifest = []
    for number in range(count):
        text = f"{TOKEN} document {number:05d}\n\n" + "\n\n".join(
            f"Observation {part}: A lighthouse keeper studies ocean tides and navigates the coast. "
            "This document describes weather patterns, fishing boats, and safe routes through a harbor."
            for part in range(paragraphs)) + "\n"
        target = directory / f"document-{number:05d}.txt"
        target.write_text(text, encoding="utf-8", newline="\n")
        manifest.append({"name": target.name, "bytes": target.stat().st_size,
                         "sha256": hashlib.sha256(target.read_bytes()).hexdigest()})
    return manifest


def gpu_evidence(entry):
    runtime = entry.get("runtime", {})
    if not (runtime.get("gpuSelected") is True and runtime.get("gpuSelfTestPassed") is True
            and runtime.get("textWarmupCompleted") is True and runtime.get("loaded") is True):
        raise RuntimeError("GPU execution lacks loaded-model, self-test, or warmup evidence")
    if runtime.get("fallbackReason") or runtime.get("error"):
        raise RuntimeError("GPU execution reported a fallback or runtime error")
    actual = {"backend": runtime.get("selectedBackend"), "deviceId": runtime.get("deviceId"),
              "name": runtime.get("deviceName")}
    if device_key(actual) != device_key(entry):
        raise RuntimeError("Runtime executed on a different GPU than its index slot")
    if runtime.get("requestedBackend") != "gpu" or runtime.get("deviceBindingExplicit") is not True:
        raise RuntimeError("GPU execution was not strict and explicitly device-bound")
    return runtime


def validate_run(status, devices, file_count, evidence):
    if status.get("modelAvailable") is not True:
        raise RuntimeError("Verified models are unavailable")
    progress = status.get("progress", {})
    if progress.get("state") not in ("idle", "complete", "completed") or progress.get("failed", 0) or progress.get("error"):
        raise RuntimeError("Indexing did not finish without errors")
    if progress.get("total") != file_count or progress.get("completed") != file_count:
        raise RuntimeError("Indexing did not complete the entire isolated corpus")
    expected = {device_key(entry) for entry in devices}
    if not expected:
        raise RuntimeError("At least one explicitly selected GPU is required")
    if len(expected) != len(devices):
        raise RuntimeError("Selected GPU identities are duplicated")
    counts = status.get("indexDeviceCounts", {})
    if counts.get("busy", 0) or counts.get("failed", 0) or status.get("queuedFiles", 0):
        raise RuntimeError("GPU indexing still has busy, failed, or queued work")
    if any(status.get(key) for key in ("pipelineTransitionError", "failures", "warning")):
        raise RuntimeError("GPU indexing reported a pipeline, storage, or file failure")
    slots = status.get("indexDevices", [])
    actual = [device_key(entry) for entry in slots]
    if len(actual) != len(expected) or set(actual) != expected:
        raise RuntimeError("Index slots differ from the exact selected GPU set")
    for entry in slots:
        if entry.get("state") not in ("ready", "idle") or entry.get("error") or entry.get("activePath"):
            raise RuntimeError("An index GPU failed or still has unfinished work")
        if entry.get("completedTasks", 0) <= 0:
            raise RuntimeError("A selected GPU completed no index tasks. Increase --files or --paragraphs")
        observed = evidence.get(device_key(entry))
        if observed is None:
            raise RuntimeError("No real GPU execution evidence was observed for a selected device")
        if device_key(observed) != device_key(entry) or observed.get("completedTasks", 0) <= 0:
            raise RuntimeError("GPU execution evidence does not belong to a completed task on this device")
        gpu_evidence(observed)
    return {"files": file_count, "segments": progress.get("segments"), "devices": slots}


def child_environment():
    environment = os.environ.copy()
    for name in ("QCS_HOME", "QCOPILOTS_SEARCH_PROFILE_DIR", "QCOPILOTS_SEARCH_TEST_WORKER",
                 "QCOPILOTS_SEARCH_TEST_SERVICE", "QCOPILOTS_SEARCH_INDEX_DEVICE"):
        environment.pop(name, None)
    environment["QCOPILOTS_SEARCH_BACKEND"] = "gpu"
    environment["QT_QPA_PLATFORM"] = "offscreen"
    return environment


def invoke(cli, profile, models, work, environment, *arguments, timeout=30):
    command = [str(cli), "--profile-dir", str(profile), "--model-dir", str(models), "--no-start",
               "--timeout", str(max(1, int(timeout * 1000))), *map(str, arguments)]
    result = subprocess.run(command, cwd=work, env=environment, capture_output=True,
                            text=True, encoding="utf-8", timeout=timeout + 5,
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    try:
        envelope = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"CLI returned invalid JSON: {result.stderr} {result.stdout}") from error
    if result.returncode or not envelope.get("ok"):
        raise RuntimeError(f"CLI {arguments[0]} failed: {envelope.get('error')} {result.stderr}")
    return envelope["result"]


def probe_devices(worker, models, work, environment, timeout):
    completed = subprocess.run([str(worker), "--mode", "full", "--profile-dir", str(work / "probe-profile"),
                                "--model-dir", str(models)],
        input=json.dumps({"id": "devices", "operation": "devices"}) + "\n",
        capture_output=True, text=True, encoding="utf-8", cwd=work, env=environment,
        timeout=timeout, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    (work / "probe.log").write_text(completed.stderr, encoding="utf-8")
    messages = [json.loads(line) for line in completed.stdout.splitlines() if line.strip()]
    replies = [item for item in messages if item.get("id") == "devices" and "ok" in item]
    if completed.returncode or len(replies) != 1 or not replies[0].get("ok"):
        raise RuntimeError(f"GPU enumeration failed: {replies} (see probe.log)")
    return replies[0]["result"]


def force_stop_tree(child):
    if os.name == "nt":
        stopped = subprocess.run(["taskkill", "/PID", str(child.pid), "/T", "/F"],
            capture_output=True, text=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
        if stopped.returncode and child.poll() is None:
            raise RuntimeError(f"Could not terminate this GPU-check process tree: {stopped.stderr}")
    else:
        # Each service has its own session, so this cannot target another profile.
        os.killpg(child.pid, signal.SIGKILL)
    child.wait(timeout=10)


def run_index(label, devices, binaries, models, corpus, file_count, work, environment, timeout):
    run_root = work / label
    run_root.mkdir()
    profile = run_root / "profile"
    command = [str(binaries["service"]), "--profile-dir", str(profile), "--model-dir", str(models)]
    for device in devices:
        command += ["--index-device", json.dumps(selector(device), separators=(",", ":"))]
    evidence = {}
    timeline = []
    child = None
    started = time.monotonic()
    deadline = started + timeout
    ready = False
    try:
        with (run_root / "service.log").open("w", encoding="utf-8") as log:
            child = subprocess.Popen(command, cwd=run_root, env=environment, stdout=log, stderr=log,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                start_new_session=os.name != "nt")
            while time.monotonic() < deadline:
                if child.poll() is not None:
                    raise RuntimeError(f"Service exited with {child.returncode}. See {run_root / 'service.log'}")
                try:
                    status = invoke(binaries["cli"], profile, models, run_root, environment, "status", timeout=1)
                    ready = True
                    break
                except RuntimeError:
                    time.sleep(0.1)
            if not ready:
                raise RuntimeError("GPU-check service did not become ready before the timeout")
            invoke(binaries["cli"], profile, models, run_root, environment, "index", corpus,
                   timeout=min(30, max(1, deadline - time.monotonic())))
            while time.monotonic() < deadline:
                if child.poll() is not None:
                    raise RuntimeError("GPU-check service exited during indexing")
                status = invoke(binaries["cli"], profile, models, run_root, environment, "status", timeout=10)
                slots = status.get("indexDevices", [])
                for entry in slots:
                    if entry.get("completedTasks", 0) > 0:
                        # No fallback or different-device completion can contribute evidence.
                        if entry.get("runtime", {}).get("loaded"):
                            gpu_evidence(entry)
                            evidence[device_key(entry)] = entry
                        else:
                            device_key(entry)
                progress = status.get("progress", {})
                timeline.append({"seconds": round(time.monotonic() - started, 3),
                                 "progress": progress, "indexDeviceCounts": status.get("indexDeviceCounts", {}),
                                 "devices": [{key: slot.get(key) for key in
                                              ("backend", "deviceId", "name", "state", "activePath", "completedTasks")}
                                             for slot in slots]})
                if progress.get("state") in ("failed", "error", "partial", "paused") or progress.get("failed", 0):
                    raise RuntimeError(f"Indexing failed: {progress}")
                if progress.get("completed") == file_count and progress.get("state") in ("idle", "complete", "completed"):
                    break
                time.sleep(0.5)
            else:
                raise RuntimeError("GPU indexing timed out")
            elapsed = time.monotonic() - started
            verified = validate_run(status, devices, file_count, evidence)
            retrieval = invoke(binaries["cli"], profile, models, run_root, environment,
                               "search", TOKEN, "--keyword-only", "--limit", file_count)
            expected = {str(path.resolve()) for path in corpus.glob("*.txt")}
            actual = {str(Path(hit["path"]).resolve()) for hit in retrieval.get("hits", [])}
            if actual != expected:
                raise RuntimeError("Keyword retrieval did not return exactly the indexed corpus")
            semantic = invoke(binaries["cli"], profile, models, run_root, environment,
                              "search", "maritime navigation and coastal safety", "--limit", file_count,
                              timeout=min(timeout, 600))
            if semantic.get("semantic") is not True or semantic.get("warning") or not semantic.get("hits"):
                raise RuntimeError("Real semantic retrieval did not return indexed vectors")
            verified.update({"label": label, "secondsIncludingInitialization": elapsed,
                             "filesPerSecondIncludingInitialization": file_count / elapsed,
                             "maximumObservedBusySlots": max(item["indexDeviceCounts"].get("busy", 0) for item in timeline),
                             "semanticRetrieval": True, "keywordRetrievedFiles": len(actual),
                             "executionEvidence": list(evidence.values()), "finalStatus": status})
            return verified
    finally:
        try:
            (run_root / "timeline.json").write_text(json.dumps(timeline, indent=2, ensure_ascii=False), encoding="utf-8")
        finally:
            # Diagnostics I/O failure must never leave our indexing children running.
            if child is not None and child.poll() is None:
                if ready:
                    try:
                        invoke(binaries["cli"], profile, models, run_root, environment, "shutdown", timeout=10)
                        child.wait(timeout=30)
                    except (RuntimeError, subprocess.TimeoutExpired):
                        force_stop_tree(child)
                        raise RuntimeError("GPU-check service required forced process-tree termination")
                else:
                    force_stop_tree(child)
                    raise RuntimeError("GPU-check service never became ready and required process-tree termination")
            if child is not None and child.returncode:
                raise RuntimeError(f"GPU-check service did not shut down cleanly: {child.returncode}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bin-dir", type=Path, required=True, help="Installed directory containing qcs-cli/service/worker")
    parser.add_argument("--model-dir", type=Path, required=True, help="Existing verified locked model directory")
    parser.add_argument("--work-root", type=Path, required=True, help="External parent for a unique retained diagnostic run")
    parser.add_argument("--files", type=int, default=32, help="Identical text files for every run, 2..500")
    parser.add_argument("--paragraphs", type=int, default=32, help="Paragraphs per file, 1..1000")
    parser.add_argument("--timeout", type=int, default=3600, help="Maximum seconds per service run")
    parser.add_argument("--skip-single", action="store_true", help="Only validate all compatible GPUs, without single-card timings")
    args = parser.parse_args(argv)
    if not 2 <= args.files <= 500 or not 1 <= args.paragraphs <= 1000 or not 30 <= args.timeout <= 86400:
        parser.error("Require --files 2..500, --paragraphs 1..1000 and --timeout 30..86400")
    parent = require_external(args.work_root)
    parent.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="qcs-gpu-check-", dir=parent)).resolve()
    report = {"schemaVersion": 1, "hardwareVerified": False, "workDirectory": str(work), "runs": [],
              "timingMeaning": "Fresh service and index per run, includes model initialization. Shared OS file caches are not cleared. No warm steady-state or linear-scaling claim.",
              "workload": "Generated text only. Multimodal numerical regression remains in the normal test suite."}
    try:
        suffix = ".exe" if os.name == "nt" else ""
        binaries = {name: args.bin_dir.resolve() / ("qcs-" + name + suffix) for name in ("cli", "service", "worker")}
        if any(not path.is_file() for path in binaries.values()) or not args.model_dir.is_dir():
            raise RuntimeError("--bin-dir must contain all three installed programs and --model-dir must exist")
        environment = child_environment()
        models = args.model_dir.resolve()
        report["programs"] = {name: {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                              for name, path in binaries.items()}
        report["models"] = invoke(binaries["cli"], work / "model-profile", models, work, environment,
                                  "model", "verify", timeout=min(args.timeout, 600))
        probe = probe_devices(binaries["worker"], models, work, environment, min(args.timeout, 120))
        report["probe"] = probe
        devices = selected_devices(probe)
        if len(devices) < 2:
            raise RuntimeError("Multi-GPU hardware acceptance requires at least two compatible GPUs")
        if args.files < len(devices) * 2:
            raise RuntimeError("Choose at least two files per compatible GPU")
        corpus = work / "corpus"
        report["corpus"] = create_corpus(corpus, args.files, args.paragraphs)
        print(f"GPU check diagnostics: {work}", flush=True)
        groups = [("all-gpus", devices)]
        if not args.skip_single:
            groups += [(f"single-gpu-{number + 1}", [device]) for number, device in enumerate(devices)]
        for label, selected in groups:
            print(f"Running {label} with {len(selected)} GPU(s)", flush=True)
            report["runs"].append(run_index(label, selected, binaries, models, corpus, args.files, work, environment, args.timeout))
        signatures = {(run["files"], run["segments"]) for run in report["runs"]}
        if len(signatures) != 1:
            raise RuntimeError("Single-card and all-card runs produced different file or segment counts")
        report["hardwareVerified"] = True
        if len(report["runs"]) > 1:
            all_time = report["runs"][0]["secondsIncludingInitialization"]
            report["observedSingleToAllTimeRatios"] = {
                run["label"]: run["secondsIncludingInitialization"] / all_time for run in report["runs"][1:]}
    except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired) as error:
        report["error"] = str(error)
        print(f"GPU check failed: {error}", file=sys.stderr)
    finally:
        report_path = work / "report.json"
        report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"Report: {report_path}", flush=True)
    return 0 if report["hardwareVerified"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
