#!/usr/bin/env python3
"""Restore an immutable release freeze without rebuilding or publishing it."""
from __future__ import annotations

from pathlib import Path
import re
import shutil
import stat
import tempfile
import zipfile


REPOSITORY = "xiaobing318/QCopilots-Search"
WORKFLOW_PATH = ".github/workflows/qcs-release.yml"
JOB_NAME = "Release"
UPLOAD_STEP = "Upload immutable release freeze"
MAX_FREEZE_BYTES = 2 * 1024**3 + 32 * 1024**2
MAX_MANIFEST_BYTES = 32 * 1024**2
MAX_CHECKSUM_BYTES = 64 * 1024
RESERVE_BYTES = 3 * 1024**3


def artifact_name(candidate, run_id, attempt):
    if not isinstance(candidate, str) or not re.fullmatch(r"[0-9a-f]{40}", candidate):
        raise ValueError("Freeze candidate must be a complete lowercase Git SHA")
    if not re.fullmatch(r"[1-9][0-9]*", str(run_id)) or type(attempt) is not int or attempt < 1:
        raise ValueError("Freeze native run and attempt must be positive integers")
    return f"qcs-release-freeze-{candidate}-run{run_id}-a{attempt}"


def _context(release, item, repository, workflow_id, candidate, freeze_run_id, attempt=None):
    if (str(item.get("id")) != str(freeze_run_id) or item.get("workflow_id") != workflow_id
            or item.get("path", "").split("@", 1)[0] != WORKFLOW_PATH
            or item.get("event") != "workflow_dispatch" or item.get("head_branch") != "main"
            or item.get("head_sha") != candidate
            or item.get("repository", {}).get("id") != repository["id"]
            or item.get("repository", {}).get("full_name") != REPOSITORY):
        raise ValueError("Freeze run belongs to another repository, workflow, event, branch or candidate")
    observed_attempt = release.positive_id(item.get("run_attempt"))
    if attempt is not None and observed_attempt != attempt:
        raise ValueError("Freeze source attempt metadata differs from its original job")
    return observed_attempt


def _upload_execution(release, job):
    """Bind only the immutable upload boundary, including its actual runner.

    Later steps may fail, be cancelled before starting, or still be running.
    Those steps cannot invalidate an already successful immutable upload.
    """
    selector = release.selector_module()
    started = selector.timestamp(job.get("started_at"), "Freeze job started_at")
    created = selector.timestamp(job.get("created_at"), "Freeze job created_at")
    completed = None
    if job.get("completed_at") is not None:
        completed = selector.timestamp(job["completed_at"], "Freeze job completed_at")
        if completed < started:
            raise ValueError("Freeze job execution timestamps are reversed")
    release.positive_id(job.get("runner_id"))
    if not isinstance(job.get("runner_name"), str) or not job["runner_name"]:
        raise ValueError("Freeze job runner name is absent")
    labels = job.get("labels")
    if not isinstance(labels, list) or not labels or not all(isinstance(item, str) and item for item in labels):
        raise ValueError("Freeze job runner labels are absent")
    steps = job.get("steps")
    if not isinstance(steps, list) or not all(isinstance(step, dict) for step in steps):
        raise ValueError("Freeze job steps are absent or invalid")
    numbers = [release.positive_id(step.get("number")) for step in steps]
    if numbers != sorted(set(numbers)):
        raise ValueError("Freeze job step numbers are duplicated or unordered")
    uploads = [step for step in steps if step.get("name") == UPLOAD_STEP]
    if len(uploads) != 1:
        raise ValueError("Freeze job needs exactly one immutable upload step")
    upload = uploads[0]
    if upload.get("status") != "completed" or upload.get("conclusion") != "success":
        raise ValueError("Freeze upload step did not complete successfully")
    first = selector.timestamp(upload.get("started_at"), "Freeze upload started_at")
    last = selector.timestamp(upload.get("completed_at"), "Freeze upload completed_at")
    if not started <= first <= last or (completed is not None and last > completed):
        raise ValueError("Freeze upload lies outside its original job execution")
    prefix = steps[:steps.index(upload) + 1]
    for step in prefix:
        if (not isinstance(step.get("name"), str) or not step["name"] or step.get("status") != "completed"
                or not isinstance(step.get("conclusion"), str) or not step["conclusion"]):
            raise ValueError("Freeze upload prefix contains an incomplete step")
        if step.get("started_at") is None and step.get("completed_at") is None and step["conclusion"] == "skipped":
            continue
        prefix_first = selector.timestamp(step.get("started_at"), "Freeze prefix started_at")
        prefix_last = selector.timestamp(step.get("completed_at"), "Freeze prefix completed_at")
        if not started <= prefix_first <= prefix_last <= last:
            raise ValueError("Freeze upload prefix lies outside its execution boundary")
    if created > started and (completed is None or created <= completed):
        raise ValueError("Freeze job creation does not identify an original or copied execution")
    keys = ("run_id", "head_sha", "head_branch", "name", "workflow_name", "runner_id", "runner_name", "started_at")
    if any(key not in job for key in keys):
        raise ValueError("Freeze job execution identity is incomplete")
    fingerprint = selector.canonical({**{key: job[key] for key in keys}, "labels": sorted(labels), "uploadPrefix": prefix})
    return fingerprint, created <= started, first, last, upload


def collect_origin(release, api, candidate, run_id, attempt, freeze_run_id):
    """Read the unique artifact and original successful upload, never write GitHub."""
    name = artifact_name(candidate, run_id, attempt)
    if not re.fullmatch(r"[1-9][0-9]*", str(freeze_run_id)):
        raise ValueError("Freeze workflow run ID must be a positive integer")
    prefix = f"/repos/{REPOSITORY}"
    repository = api.request("GET", prefix)
    if repository.get("full_name") != REPOSITORY or repository.get("archived") is not False:
        raise ValueError("Freeze repository differs from the authorized repository")
    release.positive_id(repository.get("id"))
    run = api.request("GET", prefix + f"/actions/runs/{freeze_run_id}")
    workflow_id = release.positive_id(run.get("workflow_id"))
    workflow = api.request("GET", prefix + f"/actions/workflows/{workflow_id}")
    if workflow.get("id") != workflow_id or workflow.get("path") != WORKFLOW_PATH:
        raise ValueError("Freeze run does not use the release workflow")
    latest_attempt = _context(release, run, repository, workflow_id, candidate, freeze_run_id)
    artifacts = release.collect_pages(api, str(freeze_run_id), "artifacts")
    matches = [item for item in artifacts if item.get("name") == name]
    if len(matches) != 1:
        raise ValueError("Freeze run needs exactly one immutable artifact with the requested name")
    artifact = matches[0]
    release.positive_id(artifact.get("id"))
    size = release.positive_id(artifact.get("size_in_bytes"))
    checksum = artifact.get("digest")
    linked = artifact.get("workflow_run", {})
    if (artifact.get("expired") is not False or not isinstance(checksum, str)
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", checksum) or size > MAX_FREEZE_BYTES
            or str(linked.get("id")) != str(freeze_run_id) or linked.get("head_sha") != candidate):
        raise ValueError("Freeze artifact is expired or has an invalid identity, size or digest")
    if "head_branch" in linked and linked["head_branch"] != "main":
        raise ValueError("Freeze artifact belongs to a different branch")
    selector = release.selector_module()
    created = selector.timestamp(artifact.get("created_at"), "Freeze artifact created_at")
    jobs = release.collect_pages(api, str(freeze_run_id), "jobs")
    history, seen_attempts = [], set()
    for job in jobs:
        release.positive_id(job.get("id"))
        job_attempt = release.positive_id(job.get("run_attempt"))
        if (str(job.get("run_id")) != str(freeze_run_id) or job.get("head_sha") != candidate
                or job_attempt > latest_attempt):
            raise ValueError("Freeze job inventory belongs to another run, candidate or attempt")
        if job.get("name") != JOB_NAME:
            continue
        if job.get("head_branch") != "main" or job_attempt in seen_attempts:
            raise ValueError("Freeze job history is ambiguous or belongs to another branch")
        seen_attempts.add(job_attempt)
        # A retry may skip the upload because this immutable artifact exists.
        upload = [step for step in job.get("steps", []) if isinstance(step, dict) and step.get("name") == UPLOAD_STEP]
        if len(upload) == 1 and upload[0].get("status") == "completed" and upload[0].get("conclusion") == "success":
            fingerprint, original, first, last, step = _upload_execution(release, job)
            if first <= created <= last:
                history.append((job, fingerprint, original, step))
    originals = [item for item in history if item[2]]
    if len(originals) != 1:
        raise ValueError("Freeze artifact has no unique original successful upload execution")
    source_job, fingerprint, _, upload = originals[0]
    for observed, other_fingerprint, original, _ in history:
        if (other_fingerprint != fingerprint or (not original and observed["run_attempt"] <= source_job["run_attempt"])):
            raise ValueError("Freeze copied job does not match its unique original upload execution")
    source_attempt = source_job["run_attempt"]
    historical = api.request("GET", prefix + f"/actions/runs/{freeze_run_id}/attempts/{source_attempt}")
    _context(release, historical, repository, workflow_id, candidate, freeze_run_id, source_attempt)
    exact_job = api.request("GET", prefix + f"/actions/jobs/{source_job['id']}")
    if (exact_job.get("id") != source_job["id"] or exact_job.get("run_attempt") != source_attempt
            or exact_job.get("created_at") != source_job.get("created_at")
            or _upload_execution(release, exact_job)[:2] != (fingerprint, True)):
        raise ValueError("Freeze original job changed while its upload was being verified")
    latest = api.request("GET", prefix + f"/actions/runs/{freeze_run_id}")
    if _context(release, latest, repository, workflow_id, candidate, freeze_run_id) != latest_attempt:
        raise ValueError("Freeze workflow attempt changed during provenance collection")
    return {"workflowRunId": str(freeze_run_id), "workflowId": workflow_id, "workflowPath": WORKFLOW_PATH,
            "sourceAttempt": source_attempt, "sourceJobId": source_job["id"], "uploadStepNumber": upload["number"],
            "uploadStartedAt": upload["started_at"], "uploadCompletedAt": upload["completed_at"],
            "artifactId": artifact["id"], "artifactName": name, "artifactBytes": size,
            "artifactDigest": checksum, "artifactCreatedAt": artifact["created_at"]}


def extract_freeze(release, archive_path, stage, candidate):
    limits = {release.MANIFEST: MAX_MANIFEST_BYTES, "assets/SHA256SUMS": MAX_CHECKSUM_BYTES,
              f"assets/qcs-source-materials-{candidate[:12]}.zip": release.MAX_ASSET_BYTES}
    with zipfile.ZipFile(archive_path) as archive:
        entries = archive.infolist()
        if len(entries) != len(limits) or {item.filename for item in entries} != set(limits):
            raise ValueError("Freeze archive must contain exactly its three original files")
        if sum(item.file_size for item in entries) > MAX_FREEZE_BYTES:
            raise ValueError("Freeze archive exceeds the total uncompressed size limit")
        for item in entries:
            mode = item.external_attr >> 16
            if (item.orig_filename != item.filename or item.is_dir()
                    or stat.S_IFMT(mode) not in (0, stat.S_IFREG) or item.external_attr & 0x10
                    or item.flag_bits & 1 or not 0 < item.file_size <= limits[item.filename]):
                raise ValueError("Freeze archive contains a linked, encrypted, empty or oversized entry")
            destination = stage / item.filename
            destination.parent.mkdir(parents=True, exist_ok=True)
            count = 0
            with archive.open(item) as source, destination.open("xb") as output:
                for block in iter(lambda: source.read(1024 * 1024), b""):
                    count += len(block)
                    if count > item.file_size:
                        raise ValueError("Freeze archive entry exceeds its declared size")
                    output.write(block)
            if count != item.file_size:
                raise ValueError("Freeze archive entry was truncated")


def _manifest(release, stage, identity, run_id, attempt, tag, source):
    document = release.read_json(stage / release.MANIFEST)
    if (document.get("schemaVersion") != release.SCHEMA or document.get("kind") != "release"
            or document.get("repository") != REPOSITORY or document.get("identity") != identity
            or document.get("runId") != str(run_id) or type(document.get("requestedAttempt")) is not int
            or document["requestedAttempt"] != attempt or document.get("version") != release.project_version(source)
            or tag != "v" + document.get("version", "") or "draftOnly" in document or "published" in document):
        raise ValueError("Restored freeze manifest differs from the candidate, tag or native CI identity")
    names = (f"qcs-source-materials-{identity['candidateHead'][:12]}.zip", "SHA256SUMS")
    assets = document.get("assets")
    if not isinstance(assets, list) or not all(isinstance(item, dict) for item in assets):
        raise ValueError("Restored freeze asset metadata is invalid")
    for name, role in zip(names, ("sources", "checksums")):
        rows = [item for item in assets if item.get("name") == name]
        path = release.safe_file(stage, "assets/" + name)
        expected = release.record(stage, path, name=name, role=role, configuration="all")
        if rows != [expected]:
            raise ValueError("Restored source or checksums differ from their original manifest bytes")
    return document


def restore(release, source, candidate, run_id, attempt, tag, freeze_run_id, output, api=None):
    """Return restored manifest and origin. No remote mutation or source rebuild."""
    source, output = Path(source), Path(output)
    identity = release.identity_for(source, candidate)
    artifact_name(candidate, run_id, attempt)
    output = release.external(output, source)
    if output.exists():
        raise ValueError("Release restore output already exists. Use a new external directory")
    if tag != "v" + release.project_version(source):
        raise ValueError("Release restore tag must match the candidate project version")
    api = api or release.GitHub(release.authentication())
    release.validate_remote_main(api, candidate)
    collected = release.collect_ci(api, candidate, str(run_id), attempt)
    origin = collect_origin(release, api, candidate, run_id, attempt, freeze_run_id)
    output.parent.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(output.parent).free < origin["artifactBytes"] + MAX_FREEZE_BYTES + RESERVE_BYTES:
        raise ValueError("Insufficient disk space to safely extract the immutable release freeze")
    with tempfile.TemporaryDirectory(prefix=".qcs-release-restore-", dir=output.parent) as temporary:
        work = Path(temporary)
        stage, archive_path = work / "candidate", work / "freeze.zip"
        stage.mkdir()
        api.download_artifact(origin["artifactId"], archive_path, origin["artifactDigest"], max_bytes=origin["artifactBytes"])
        if (archive_path.stat().st_size != origin["artifactBytes"]
                or release.digest(archive_path) != origin["artifactDigest"].removeprefix("sha256:")):
            raise ValueError("Downloaded freeze archive size or SHA-256 differs from GitHub")
        extract_freeze(release, archive_path, stage, candidate)
        archive_path.unlink()
        document = _manifest(release, stage, identity, run_id, attempt, tag, source)
        selected = release.release_selection(document)
        if collected["selection"]["selected"] != selected:
            raise ValueError("Restored freeze selection differs from the exact successful native CI artifacts")
        artifact_ids, packages = set(), []
        for configuration, selection in sorted(selected.items()):
            rows = release.selected_artifacts(configuration, selection, document["version"], attempt, artifact_ids)
            expected = [{"filename": name, "bytes": item["artifactSize"],
                         "sha256": item["artifactDigest"].removeprefix("sha256:")} for name, item in rows.items()]
            if document["configurations"][configuration].get("packages") != expected:
                raise ValueError("Restored package metadata differs from its immutable native artifact")
            packages.extend(rows.items())
        if len(packages) != release.USER_PACKAGE_COUNT:
            raise ValueError("Release restore requires exactly 21 user packages")
        package_bytes = sum(item["artifactSize"] for _, item in packages)
        if shutil.disk_usage(work).free < package_bytes + RESERVE_BYTES:
            raise ValueError("Insufficient disk space for the exact CI package bytes and 3 GiB reserve")
        release.validate_sources(stage / "assets" / f"qcs-source-materials-{candidate[:12]}.zip", identity, source)
        for name, artifact in packages:
            destination = stage / "assets" / name
            api.download_artifact(artifact["artifactId"], destination, artifact["artifactDigest"], max_bytes=artifact["artifactSize"])
            if (destination.stat().st_size != artifact["artifactSize"]
                    or release.digest(destination) != artifact["artifactDigest"].removeprefix("sha256:")):
                raise ValueError("Restored CI package size or SHA-256 differs from its frozen artifact")
        release.verify_freeze(stage / release.MANIFEST, source)
        release.verify_ci_packages(api, document)
        release.validate_remote_main(api, candidate)
        if collect_origin(release, api, candidate, run_id, attempt, freeze_run_id) != origin:
            raise ValueError("Freeze artifact provenance changed during restoration")
        if release.identity_for(source, candidate) != identity:
            raise ValueError("Source identity changed during release restoration")
        stage.rename(output)
    return {"manifestPath": str(output / release.MANIFEST), "origin": origin,
            "restoredPackageCount": len(packages), "restoredPackageBytes": package_bytes}
