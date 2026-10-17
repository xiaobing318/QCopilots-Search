#!/usr/bin/env python3
"""Manual GitHub release orchestration using the same verified developer commands."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys

sys.dont_write_bytecode = True
SOURCE = Path(__file__).resolve().parents[2]
REPOSITORY = "xiaobing318/QCopilots-Search"
WORKFLOW_PATH = ".github/workflows/qcs-release.yml"
FREEZE_STEP = "Upload immutable release freeze"
FREEZE_JOB = "Release"
GIB = 1024 ** 3
MINIMUM_FREE_BYTES = 16 * GIB
DISK_RESERVE_BYTES = 3 * GIB
CLEANUP_PATHS = ("/usr/local/lib/android", "/usr/share/dotnet")


def release_module():
    spec = importlib.util.spec_from_file_location("qcs_release_ci_library", SOURCE / "tools/qcs-release.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def positive_integer(value, label):
    if not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]{0,18}", value):
        raise ValueError(f"{label} must be a positive decimal integer")
    number = int(value)
    if number > 2 ** 63 - 1:
        raise ValueError(f"{label} exceeds the supported range")
    return number


def inputs(environment=None):
    env = os.environ if environment is None else environment
    if (env.get("GITHUB_ACTIONS") != "true" or env.get("GITHUB_EVENT_NAME") != "workflow_dispatch"
            or env.get("GITHUB_REPOSITORY") != REPOSITORY or env.get("GITHUB_REF") != "refs/heads/main"):
        raise ValueError("Release workflow requires a manual run on the authorized repository's main branch")
    candidate = env.get("QCS_RELEASE_CANDIDATE", "")
    if not re.fullmatch(r"[0-9a-f]{40}", candidate) or candidate != env.get("GITHUB_SHA"):
        raise ValueError("Candidate must be the exact full SHA of the workflow commit")
    action = env.get("QCS_RELEASE_ACTION")
    if action not in ("draft", "publish"):
        raise ValueError("Release action must be draft or publish")
    tag = env.get("QCS_RELEASE_TAG", "")
    if not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", tag):
        raise ValueError("Release tag must have the form v<major>.<minor>.<patch>")
    freeze = env.get("QCS_RELEASE_FREEZE_RUN", "")
    if action == "publish" and not freeze:
        raise ValueError("Publish requires the original release workflow freeze_run_id")
    return {"action": action, "candidate": candidate, "tag": tag,
            "native_run_id": positive_integer(env.get("QCS_RELEASE_CI_RUN", ""), "ci_run_id"),
            "native_attempt": positive_integer(env.get("QCS_RELEASE_CI_ATTEMPT", ""), "ci_attempt"),
            "current_run_id": positive_integer(env.get("GITHUB_RUN_ID", ""), "GITHUB_RUN_ID"),
            "current_attempt": positive_integer(env.get("GITHUB_RUN_ATTEMPT", ""), "GITHUB_RUN_ATTEMPT"),
            "freeze_run_id": positive_integer(freeze, "freeze_run_id") if freeze else None}


def freeze_name(request):
    return (f"qcs-release-freeze-{request['candidate']}"
            f"-run{request['native_run_id']}-a{request['native_attempt']}")


def git(*arguments):
    return subprocess.check_output(["git", "-C", str(SOURCE), *arguments],
                                   text=True, encoding="utf-8").strip()


def validate_checkout(request):
    if git("rev-parse", "HEAD") != request["candidate"]:
        raise ValueError("Checkout differs from the workflow candidate")
    if git("status", "--porcelain", "--untracked-files=all"):
        raise ValueError("Release workflow requires an unchanged checkout")


def validate_workflow_run(api, release, run_id, candidate):
    prefix = f"/repos/{REPOSITORY}"
    run = api.request("GET", prefix + f"/actions/runs/{run_id}")
    workflow_id = release.positive_id(run.get("workflow_id"))
    workflow = api.request("GET", prefix + f"/actions/workflows/{workflow_id}")
    repository = api.request("GET", prefix)
    if (str(run.get("id")) != str(run_id) or run.get("event") != "workflow_dispatch"
            or run.get("head_sha") != candidate or run.get("head_branch") != "main"
            or run.get("path", "").split("@", 1)[0] != WORKFLOW_PATH
            or workflow.get("path") != WORKFLOW_PATH or workflow.get("id") != workflow_id
            or repository.get("full_name") != REPOSITORY or repository.get("archived") is not False
            or run.get("repository", {}).get("id") != release.positive_id(repository.get("id"))
            or run.get("repository", {}).get("full_name") != REPOSITORY):
        raise ValueError("Frozen materials must originate in this manual release workflow on the exact main candidate")
    # A failed or cancelled draft may still have a fully uploaded immutable freeze.
    return run


def find_freeze(api, release, request):
    run_id = request["freeze_run_id"] or request["current_run_id"]
    validate_workflow_run(api, release, run_id, request["candidate"])
    expected = freeze_name(request)
    artifacts = release.collect_pages(api, str(run_id), "artifacts")
    matches = [row for row in artifacts if row.get("name") == expected]
    if len(matches) > 1:
        raise ValueError("Release run contains duplicate immutable freeze artifacts")
    if not matches:
        if request["freeze_run_id"]:
            raise ValueError("Original release freeze is missing or expired. It cannot be regenerated")
        if artifacts:
            raise ValueError("This release run already holds a different freeze. Do not reuse it for another candidate")
        return None
    artifact = matches[0]
    release.positive_id(artifact.get("id"))
    if (artifact.get("expired") is not False
            or type(artifact.get("size_in_bytes")) is not int or artifact["size_in_bytes"] <= 0
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", artifact.get("digest", ""))
            or str(artifact.get("workflow_run", {}).get("id")) != str(run_id)
            or artifact.get("workflow_run", {}).get("head_sha") != request["candidate"]):
        raise ValueError("Original release freeze is expired or has invalid immutable metadata")
    return {"run_id": run_id, "artifact_id": artifact["id"], "bytes": artifact["size_in_bytes"],
            "digest": artifact["digest"], "name": expected}


def preflight(request, api, release):
    validate_checkout(request)
    if request["tag"] != "v" + release.project_version(SOURCE):
        raise ValueError("Release tag differs from the candidate's project version")
    validate_workflow_run(api, release, request["current_run_id"], request["candidate"])
    release.validate_remote_main(api, request["candidate"])
    ci = release.collect_ci(api, request["candidate"], str(request["native_run_id"]), request["native_attempt"])
    release.validate_tag(api, request["tag"], request["candidate"])
    freeze = find_freeze(api, release, request)
    packages = [item for selected in ci["selection"]["selected"].values() for item in selected["artifacts"]]
    if len(packages) != release.USER_PACKAGE_COUNT:
        raise ValueError("Native CI must provide exactly 21 user packages")
    sizes = [item["artifactSize"] for item in packages]
    if any(type(size) is not int or size <= 0 for size in sizes):
        raise ValueError("Native package size must be an exact positive integer")
    report = {"request": request, "freeze": freeze, "package_count": len(packages),
              "package_bytes": sum(sizes), "freeze_name": freeze_name(request)}
    print(json.dumps(report, indent=2), flush=True)
    return report


def task_root(request, environment=None):
    env = os.environ if environment is None else environment
    raw = env.get("RUNNER_TEMP", "")
    if not raw or not Path(raw).is_absolute():
        raise ValueError("RUNNER_TEMP must be an absolute external directory")
    path = Path(raw)
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        raise ValueError("Runner temporary directory cannot use symbolic links")
    parent = path.resolve()
    source = SOURCE.resolve()
    if not parent.is_dir() or parent == source or source in parent.parents or parent in source.parents:
        raise ValueError("Runner temporary directory must be outside the checkout")
    result = parent / f"qcs-release-{request['current_run_id']}-{request['current_attempt']}"
    if result.exists() and (result.is_symlink() or not result.is_dir()):
        raise ValueError("Release work directory must be a regular external directory")
    return result


def cleanup_target(value):
    if value not in CLEANUP_PATHS:
        raise ValueError("Cleanup target is not in the release runner SDK allowlist")
    path = Path(value)
    if any(item.is_symlink() for item in (path, *path.parents)) or path.resolve() != path:
        raise ValueError("Release runner SDK cleanup cannot follow symbolic links")
    if path.exists() and not path.is_dir():
        raise ValueError("Release runner SDK cleanup target is not a directory")
    return path


def ensure_disk_space(root, package_bytes, environment=None):
    env = os.environ if environment is None else environment
    needed = max(MINIMUM_FREE_BYTES, package_bytes + DISK_RESERVE_BYTES)
    available = shutil.disk_usage(root).free
    print(f"Release runner disk: available={available} bytes, required={needed} bytes, "
          f"native packages={package_bytes} bytes", flush=True)
    if available >= needed:
        return
    if (platform.system() != "Linux" or env.get("GITHUB_ACTIONS") != "true"
            or env.get("RUNNER_ENVIRONMENT") != "github-hosted" or env.get("RUNNER_OS") != "Linux"
            or env.get("ImageOS") != "ubuntu24"):
        raise ValueError("Insufficient space. SDK cleanup is limited to a GitHub-hosted Ubuntu 24.04 release runner")
    for value in CLEANUP_PATHS:
        path = cleanup_target(value)
        if path.exists():
            print("Removing unused preinstalled SDK from release runner: " + str(path), flush=True)
            subprocess.run(["sudo", "--non-interactive", "rm", "-rf", "--", str(path)], check=True)
        available = shutil.disk_usage(root).free
        print(f"Release runner available after SDK cleanup: {available} bytes", flush=True)
        if available >= needed:
            return
    raise ValueError(f"Release runner has {available} free bytes but requires {needed}. No release was written")


def developer(command, *arguments):
    subprocess.run([sys.executable, "-B", str(SOURCE / "tools/qcs-dev.py"), command,
                    *(str(value) for value in arguments)], cwd=SOURCE, check=True)


def output_values(values):
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        raise ValueError("GITHUB_OUTPUT is required")
    with Path(path).open("a", encoding="utf-8") as stream:
        for key, value in values.items():
            if not re.fullmatch(r"[a-z_]+", key) or any(character in str(value) for character in "\r\n"):
                raise ValueError("Invalid single-line workflow output")
            stream.write(f"{key}={value}\n")


def stage(request, api, release):
    report = preflight(request, api, release)
    root = task_root(request)
    ensure_disk_space(root.parent, report["package_bytes"])
    if root.exists():
        raise ValueError("Release staging directory already exists. Restart the job to restore its immutable artifact")
    root.mkdir()
    # This is a disposable checkout of github.sha, never a branch selected by user input.
    subprocess.run(["git", "-C", str(SOURCE), "checkout", "-B", "main", request["candidate"]], check=True)
    release.identity_for(SOURCE, request["candidate"])
    destination = root / "freeze"
    common = ["--candidate-commit", request["candidate"], "--run-id", request["native_run_id"],
              "--attempt", request["native_attempt"], "--output-dir", destination]
    freeze = report["freeze"]
    if freeze:
        developer("release-restore", "--freeze-run-id", freeze["run_id"], *common, "--tag", request["tag"])
    else:
        developer("release-prepare", *common, "--source-cache-dir", root / "source-cache")
    manifest = destination / release.MANIFEST
    document = release.verify_freeze(manifest, SOURCE)
    state = {**report, "freeze_dir": str(destination), "new_freeze": freeze is None,
             "freeze_run_id": freeze["run_id"] if freeze else request["current_run_id"],
             "manifest_sha256": release.digest(manifest),
             "source_bytes": sum(row["bytes"] for row in document["assets"] if row["role"] == "sources")}
    (root / "state.json").write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    output_values({"freeze_dir": destination.as_posix(), "freeze_name": report["freeze_name"],
                   "candidate_short": request["candidate"][:12],
                   "new_freeze": "true" if freeze is None else "false"})
    print(f"Frozen source materials: {state['source_bytes']} bytes. Recovery freeze_run_id: {state['freeze_run_id']}", flush=True)
    return state


def read_state(request):
    root = task_root(request)
    state = json.loads((root / "state.json").read_text(encoding="utf-8"))
    if state.get("request") != request or state.get("freeze_dir") != str(root / "freeze"):
        raise ValueError("Release stage belongs to another request or directory")
    return state


def finish(request, api, release):
    state = read_state(request)
    # The workflow reaches this step only after its immutable upload succeeded.
    # Recheck remote main, CI and artifact existence before any Release mutation.
    report = preflight(request, api, release)
    if report["freeze"] is None:
        raise ValueError("Release cannot be written before its immutable freeze artifact exists")
    manifest = Path(state["freeze_dir"]) / release.MANIFEST
    if release.digest(manifest) != state["manifest_sha256"]:
        raise ValueError("Original frozen manifest changed during artifact upload")
    command = "release-upload-draft" if request["action"] == "draft" else "release-publish"
    developer(command, "--manifest", manifest, "--tag", request["tag"])


def summary(request):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        raise ValueError("GITHUB_STEP_SUMMARY is required")
    state_path = task_root(request) / "state.json"
    state = read_state(request) if state_path.is_file() else None
    lines = ["## Release workflow", "", f"- Action: `{request['action']}`",
             f"- Candidate: `{request['candidate']}`", f"- Tag: `{request['tag']}`",
             f"- Job status: `{os.environ.get('QCS_RELEASE_JOB_STATUS', 'unknown')}`",
             f"- Native CI: run `{request['native_run_id']}`, attempt `{request['native_attempt']}`"]
    if state:
        lines.extend([f"- Original freeze_run_id: `{state['freeze_run_id']}`",
                      f"- Freeze artifact: `{state['freeze_name']}` (14-day retention)",
                      f"- Original manifest SHA-256: `{state['manifest_sha256']}`",
                      f"- User packages: {state['package_count']}, {state['package_bytes']} bytes",
                      f"- Corresponding source materials: {state['source_bytes']} bytes", "",
                      "To recover a failed draft or publish its original materials, run this workflow again "
                      "with the same candidate, native CI run, attempt and tag, and the original "
                      f"`freeze_run_id={state['freeze_run_id']}`. Recovery requires the immutable freeze upload "
                      "step to have succeeded and both the freeze and native package artifacts to remain available."])
    lines.extend(["", f"[Release page](https://github.com/{REPOSITORY}/releases/tag/{request['tag']})", ""])
    with Path(path).open("a", encoding="utf-8") as stream:
        stream.write("\n".join(lines))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("preflight", "stage", "finish", "summary"))
    args = parser.parse_args()
    request = inputs()
    if args.command == "summary":
        summary(request)
        return
    release = release_module()
    api = release.GitHub(release.authentication())
    {"preflight": preflight, "stage": stage, "finish": finish}[args.command](request, api, release)


if __name__ == "__main__":
    main()
