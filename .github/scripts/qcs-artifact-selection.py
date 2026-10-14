#!/usr/bin/env python3
"""Select immutable CI artifacts from complete, preserved GitHub execution history.

GitHub copies completed jobs into a new run attempt when another job is rerun.
The copied row has a new ID and attempt but retains its actual execution times.
Only a unique original execution with the same detailed fingerprint can supply
that copied row's artifact. No successful older attempt is used as a fallback.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import urllib.error
import urllib.request


PLATFORMS = ("windows-x64", "linux-x64", "macos-arm64", "macos-x64")
API_ROOT = "https://api.github.com"
PAGE_SIZE = 100
MAX_PAGES = 100
MAX_PAGE_BYTES = 16 * 1024 * 1024
SHA = re.compile(r"[0-9a-f]{40}\Z")
HASH = re.compile(r"[0-9a-f]{64}\Z")
ARTIFACT_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")


def integer(value, label, minimum=1):
    if type(value) is not int or value < minimum:
        raise ValueError(f"{label} must be an integer >= {minimum}")
    return value


def timestamp(value, label):
    if not isinstance(value, str):
        raise ValueError(f"{label} must be an ISO timestamp")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{label} must be an ISO timestamp") from error
    if result.tzinfo is None:
        raise ValueError(f"{label} must include a timezone")
    return result


def arguments(repository, run_id, candidate_head, profile, attempt):
    if not isinstance(repository, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("Repository must be OWNER/REPO")
    if not isinstance(run_id, str) or not re.fullmatch(r"[1-9][0-9]*", run_id):
        raise ValueError("Workflow run ID must be a positive decimal string")
    if not isinstance(candidate_head, str) or not SHA.fullmatch(candidate_head):
        raise ValueError("Candidate head must be a complete lowercase Git SHA")
    if profile not in ("fast", "full"):
        raise ValueError("Test profile must be fast or full")
    integer(attempt, "Aggregate attempt")


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def read_json_bytes(data):
    return json.loads(data.decode("utf-8"),
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError("Non-finite JSON value")))


def execution_fingerprint(job):
    """Match actual execution fields, excluding GitHub's copied-row metadata."""
    keys = ("run_id", "head_sha", "workflow_name", "head_branch", "name", "status", "conclusion",
            "started_at", "completed_at", "runner_id", "runner_name")
    for key in keys:
        if key not in job:
            raise ValueError(f"Job execution omits {key}")
    started = timestamp(job["started_at"], "Job started_at")
    completed = timestamp(job["completed_at"], "Job completed_at")
    if completed < started:
        raise ValueError("Job execution timestamps are reversed")
    integer(job["runner_id"], "Runner ID")
    if not isinstance(job["runner_name"], str) or not job["runner_name"]:
        raise ValueError("Job runner name is absent")
    labels = job.get("labels")
    if not isinstance(labels, list) or not labels or not all(isinstance(item, str) and item for item in labels):
        raise ValueError("Job runner labels are absent")
    steps = job.get("steps")
    if not isinstance(steps, list) or not steps:
        raise ValueError("Job execution steps are absent")
    previous = 0
    for step in steps:
        if not isinstance(step, dict):
            raise ValueError("Job step is not an object")
        number = integer(step.get("number"), "Step number")
        if number <= previous:
            raise ValueError("Job steps are repeated or out of order")
        previous = number
        for key in ("name", "status", "conclusion", "started_at", "completed_at"):
            if key not in step:
                raise ValueError(f"Job step omits {key}")
        if not isinstance(step["name"], str) or not step["name"] or step["status"] != "completed":
            raise ValueError("Job execution contains an incomplete step")
        if not isinstance(step["conclusion"], str) or not step["conclusion"]:
            raise ValueError("Job step conclusion is absent")
        first, last = step["started_at"], step["completed_at"]
        if first is None and last is None and step["conclusion"] == "skipped":
            continue
        first = timestamp(first, "Step started_at")
        last = timestamp(last, "Step completed_at")
        if not started <= first <= last <= completed:
            raise ValueError("Step execution lies outside its job timestamps")
    value = {key: job[key] for key in keys}
    value.update(labels=sorted(labels), steps=steps)
    return canonical(value)


def original_execution(observed, history):
    fingerprint = execution_fingerprint(observed)
    started = timestamp(observed["started_at"], "Job started_at")
    completed = timestamp(observed["completed_at"], "Job completed_at")
    created = timestamp(observed.get("created_at"), "Job created_at")
    if started < created <= completed:
        raise ValueError("Job creation falls inside an execution and cannot establish an original or copied row")
    originals = []
    for job in history:
        if (job["run_attempt"] > observed["run_attempt"]
                or job.get("started_at") != observed["started_at"]
                or job.get("completed_at") != observed["completed_at"]
                or job.get("status") != observed["status"]
                or job.get("conclusion") != observed["conclusion"]):
            continue
        if execution_fingerprint(job) != fingerprint:
            continue
        other_created = timestamp(job.get("created_at"), "Job created_at")
        if other_created <= started:
            originals.append(job)
        elif other_created <= completed:
            raise ValueError("Matching execution has ambiguous copied-row timestamps")
    if len(originals) != 1:
        raise ValueError("Latest job has no unique original execution with matching detailed steps")
    source = originals[0]
    if created <= started:
        if source["id"] != observed["id"]:
            raise ValueError("An original job cannot borrow another execution's artifact")
    elif source["run_attempt"] >= observed["run_attempt"]:
        raise ValueError("Copied job does not point to an earlier original execution")
    return source


def artifact_name(platform, head, profile, attempt):
    prefix = "fast" if profile == "fast" else "native"
    return f"qcs-{prefix}-{platform}-{head}-attempt{attempt}"


def select(repository, run_id, head, profile, attempt, jobs, artifacts):
    """Pure deterministic selection, suitable for revalidation without network."""
    arguments(repository, run_id, head, profile, attempt)
    result = {"schemaVersion": 1, "kind": "ci-artifact-selection", "repository": repository,
              "runId": run_id, "aggregateAttempt": attempt, "candidateHead": head,
              "testProfile": profile, "passed": False, "errors": [], "selected": {}, "excluded": []}
    errors = result["errors"]
    try:
        for label, rows in (("Jobs", jobs), ("Artifacts", artifacts)):
            if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
                raise ValueError(f"{label} API inventory is not a list of objects")
            ids = [integer(row.get("id"), f"{label} ID") for row in rows]
            if len(ids) != len(set(ids)):
                raise ValueError(f"{label} API inventory contains duplicate IDs")
        for job in jobs:
            integer(job.get("run_attempt"), "Job run_attempt")
            if str(job.get("run_id")) != run_id or job.get("head_sha") != head:
                raise ValueError("Job inventory belongs to another workflow run or candidate")
            if job.get("name") in {"qcs " + platform for platform in PLATFORMS} and job["run_attempt"] > attempt:
                raise ValueError("Native execution history is newer than the requested aggregate attempt")
        for platform in PLATFORMS:
            try:
                history = [job for job in jobs if job.get("name") == "qcs " + platform
                           and job["run_attempt"] <= attempt]
                if not history:
                    raise ValueError("No native job exists for the requested run and attempt")
                attempts = [job["run_attempt"] for job in history]
                if len(attempts) != len(set(attempts)):
                    raise ValueError("Native job history is ambiguous within a platform attempt")
                current = [job for job in history if job["run_attempt"] == attempt]
                if len(current) != 1:
                    raise ValueError("Current aggregate attempt needs exactly one native job observation")
                observed = current[0]
                if observed.get("status") != "completed" or observed.get("conclusion") != "success":
                    raise ValueError("Latest native job did not complete successfully. Earlier successful artifacts cannot replace it")
                source = original_execution(observed, history)
                for later in history:
                    if later["run_attempt"] > source["run_attempt"]:
                        later_source = original_execution(later, history)
                        if later_source["id"] != source["id"]:
                            raise ValueError("Current observation bypasses a newer actual execution. Earlier artifacts cannot replace it")
                name = artifact_name(platform, head, profile, source["run_attempt"])
                matches = [item for item in artifacts if item.get("name") == name]
                if len(matches) != 1:
                    raise ValueError("The original execution needs exactly one artifact named " + name)
                artifact = matches[0]
                linked_run = artifact.get("workflow_run", {})
                if (not isinstance(linked_run, dict) or str(linked_run.get("id")) != run_id
                        or linked_run.get("head_sha") != head):
                    raise ValueError("Artifact workflow run or candidate does not match the selected execution")
                digest = artifact.get("digest")
                if artifact.get("expired") is not False or not isinstance(digest, str) or not ARTIFACT_DIGEST.fullmatch(digest):
                    raise ValueError("Artifact is expired or has no complete SHA-256 digest")
                created = timestamp(artifact.get("created_at"), "Artifact created_at")
                if not timestamp(source["started_at"], "Job started_at") <= created <= timestamp(source["completed_at"], "Job completed_at"):
                    raise ValueError("Artifact was not created during its original execution")
                result["selected"][platform] = {
                    "sourceAttempt": source["run_attempt"], "sourceJobId": source["id"],
                    "observedJobId": observed["id"], "artifactId": artifact["id"],
                    "artifactName": name, "artifactDigest": digest}
            except (ValueError, TypeError, KeyError) as error:
                errors.append(f"{platform}: {error}")
        selected_ids = {item["artifactId"] for item in result["selected"].values()}
        result["excluded"] = [{"artifactId": item["id"], "artifactName": item.get("name"),
                               "reason": "Not the exact artifact of a selected original execution"}
                              for item in sorted(artifacts, key=lambda item: item["id"]) if item["id"] not in selected_ids]
    except (ValueError, TypeError, KeyError) as error:
        errors.append(str(error))
    result["passed"] = not errors and set(result["selected"]) == set(PLATFORMS)
    return result


def page_url(repository, run_id, kind, page):
    suffix = f"?per_page={PAGE_SIZE}&page={page}"
    if kind == "jobs":
        suffix += "&filter=all"
    return f"{API_ROOT}/repos/{repository}/actions/runs/{run_id}/{kind}{suffix}"


def unpack_pages(pages, kind):
    if not pages:
        raise ValueError(f"{kind} API snapshot is absent")
    total = None
    rows = []
    for index, document in enumerate(pages):
        if not isinstance(document, dict):
            raise ValueError(f"{kind} API page is not an object")
        current = integer(document.get("total_count"), f"{kind} total_count", 0)
        if total is None:
            total = current
            if total > MAX_PAGES * PAGE_SIZE:
                raise ValueError(f"{kind} API inventory exceeds the supported page budget")
        elif total != current:
            raise ValueError(f"{kind} API total_count changed during pagination")
        items = document.get(kind)
        expected_count = max(0, min(PAGE_SIZE, total - index * PAGE_SIZE))
        if not isinstance(items, list) or len(items) != expected_count:
            raise ValueError(f"{kind} API page is incomplete or has an unexpected size")
        rows.extend(items)
    expected_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    if len(pages) != expected_pages or len(rows) != total:
        raise ValueError(f"{kind} API pagination is incomplete or has extra pages")
    ids = [integer(row.get("id"), f"{kind} ID") for row in rows if isinstance(row, dict)]
    if len(ids) != len(rows) or len(ids) != len(set(ids)):
        raise ValueError(f"{kind} API pagination contains malformed or repeated entries")
    return rows


def snapshot_file(parent, relative):
    if not isinstance(relative, str) or "\\" in relative:
        raise ValueError("Snapshot path must be a portable relative path")
    path = PurePosixPath(relative)
    if path.is_absolute() or len(path.parts) != 2 or path.parts[0] != "selection-inputs" or ".." in path.parts:
        raise ValueError("Snapshot path must stay in selection-inputs")
    candidate = parent / path
    for part in (candidate.parent, candidate):
        info = part.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
            raise ValueError("Snapshot paths must not contain links or reparse points")
    if not candidate.is_file() or candidate.resolve().parent != (parent / "selection-inputs").resolve():
        raise ValueError("Snapshot must be an ordinary file inside selection-inputs")
    return candidate


def load_selection(selection_path, expected_head=None, expected_profile=None, expected_run=None):
    selection_path = Path(selection_path)
    try:
        if selection_path.stat().st_size > MAX_PAGE_BYTES:
            raise ValueError("Selection JSON is too large")
        document = read_json_bytes(selection_path.read_bytes())
        if type(document.get("schemaVersion")) is not int or document["schemaVersion"] != 1 or document.get("kind") != "ci-artifact-selection":
            raise ValueError("Unsupported artifact selection document")
        repository, run_id, head, profile, attempt = (document[key] for key in
            ("repository", "runId", "candidateHead", "testProfile", "aggregateAttempt"))
        arguments(repository, run_id, head, profile, attempt)
        for actual, expected, label in ((head, expected_head, "candidate"), (profile, expected_profile, "profile"),
                                        (run_id, expected_run, "run")):
            if expected is not None and actual != expected:
                raise ValueError(f"Artifact selection {label} differs from the requested identity")
        files = document.get("files")
        if not isinstance(files, list) or not 2 <= len(files) <= MAX_PAGES * 2:
            raise ValueError("Artifact selection has no complete API snapshot inventory")
        pages = {"jobs": [], "artifacts": []}
        seen = set()
        for entry in files:
            kind = entry["kind"]
            if kind not in pages:
                raise ValueError("Unknown API snapshot kind")
            page = integer(entry["page"], "API snapshot page")
            relative = f"selection-inputs/{kind}-{page:04d}.json"
            if (entry["path"] != relative or relative in seen or page != len(pages[kind]) + 1
                    or entry["url"] != page_url(repository, run_id, kind, page)):
                raise ValueError("API snapshot path, page order or URL does not match its selection")
            seen.add(relative)
            path = snapshot_file(selection_path.resolve().parent, relative)
            if path.stat().st_size > MAX_PAGE_BYTES:
                raise ValueError("API snapshot is too large")
            data = path.read_bytes()
            if (type(entry.get("bytes")) is not int or entry["bytes"] != len(data)
                    or not isinstance(entry.get("sha256"), str) or not HASH.fullmatch(entry["sha256"])
                    or hashlib.sha256(data).hexdigest() != entry["sha256"]):
                raise ValueError("API snapshot bytes or SHA-256 changed: " + relative)
            pages[kind].append(read_json_bytes(data))
        recomputed = select(repository, run_id, head, profile, attempt,
                            unpack_pages(pages["jobs"], "jobs"), unpack_pages(pages["artifacts"], "artifacts"))
        if canonical({key: document.get(key) for key in recomputed}) != canonical(recomputed):
            raise ValueError("Artifact selection differs from the preserved API execution history")
        if document.get("collectionErrors") != [] or not recomputed["passed"]:
            raise ValueError("Artifact selection did not pass: " + "; ".join(recomputed["errors"]))
        return document
    except (KeyError, TypeError, AttributeError, OSError) as error:
        raise ValueError(f"Cannot validate artifact selection: {error}") from error


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        raise ValueError("GitHub API redirected the request. No credentials were forwarded")


def fetch_page(url, token):
    if not isinstance(token, str) or not token or any(not 33 <= ord(character) <= 126 for character in token):
        raise ValueError("GITHUB_TOKEN must contain only visible ASCII characters without whitespace")
    try:
        request = urllib.request.Request(url, headers={"Authorization": "Bearer " + token,
            "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "qcs-ci-artifact-selection"}, method="GET")
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=30) as response:
            data = response.read(MAX_PAGE_BYTES + 1)
    except urllib.error.HTTPError as error:
        raise ValueError(f"GitHub read-only API request failed with HTTP {error.code}") from None
    except urllib.error.URLError:
        raise ValueError("GitHub read-only API request failed") from None
    except (ValueError, UnicodeError):
        raise ValueError("GitHub read-only API request was invalid or redirected. No credentials were forwarded") from None
    if len(data) > MAX_PAGE_BYTES:
        raise ValueError("GitHub API page exceeds the response size budget")
    return data


def collect(args):
    arguments(args.repository, args.run_id, args.candidate_head, args.profile, args.attempt)
    output = args.output_dir.expanduser().resolve()
    source = Path(__file__).resolve().parents[2]
    if output == source or source in output.parents:
        raise ValueError("Artifact selection outputs must be outside the source tree")
    if output.exists() and any(output.iterdir()):
        raise ValueError("Artifact selection needs an empty output directory")
    output.mkdir(parents=True, exist_ok=True)
    snapshots = output / "selection-inputs"
    snapshots.mkdir()
    files, failures = [], []
    pages = {"jobs": [], "artifacts": []}
    try:
        token = os.environ.get("GITHUB_TOKEN")
        if not token:
            raise ValueError("GITHUB_TOKEN is required for read-only artifact selection")
        for kind in pages:
            page, count = 1, 1
            while page <= count:
                url = page_url(args.repository, args.run_id, kind, page)
                data = fetch_page(url, token)
                path = snapshots / f"{kind}-{page:04d}.json"
                path.write_bytes(data)
                files.append({"path": path.relative_to(output).as_posix(), "bytes": len(data),
                              "sha256": hashlib.sha256(data).hexdigest(), "kind": kind, "page": page, "url": url})
                parsed = read_json_bytes(data)
                if not isinstance(parsed, dict):
                    raise ValueError(f"{kind} API page is not an object")
                pages[kind].append(parsed)
                total = integer(parsed.get("total_count"), f"{kind} total_count", 0)
                if page == 1:
                    count = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
                    if count > MAX_PAGES:
                        raise ValueError("GitHub inventory exceeds the supported page budget")
                page += 1
            unpack_pages(pages[kind], kind)
        document = select(args.repository, args.run_id, args.candidate_head, args.profile, args.attempt,
                          unpack_pages(pages["jobs"], "jobs"), unpack_pages(pages["artifacts"], "artifacts"))
    except (ValueError, TypeError, KeyError, OSError) as error:
        failures.append(str(error))
        document = select(args.repository, args.run_id, args.candidate_head, args.profile, args.attempt, [], [])
        document["errors"] = failures + document["errors"]
        document["passed"] = False
    document.update(files=files, collectionErrors=failures)
    path = output / "selection.json"
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    if not document["passed"]:
        raise ValueError("Artifact selection failed. Preserved report: " + str(path))
    load_selection(path, args.candidate_head, args.profile, args.run_id)
    if os.environ.get("GITHUB_OUTPUT"):
        with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as stream:
            ids = ",".join(str(document["selected"][platform]["artifactId"]) for platform in PLATFORMS)
            stream.write("artifact_ids=" + ids + "\n")
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("collect",))
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--candidate-head", required=True)
    parser.add_argument("--profile", required=True, choices=("fast", "full"))
    parser.add_argument("--attempt", required=True, type=int)
    args = parser.parse_args()
    try:
        print(collect(args))
    except (ValueError, OSError, TypeError, KeyError) as error:
        print("qcs artifact selection: " + str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
