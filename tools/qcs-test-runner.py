#!/usr/bin/env python3
"""Run one test in an exclusively owned, disposable working directory."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any


FAILURE = 125


def identity(path: Path) -> tuple[int, int]:
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or getattr(info, "st_file_attributes", 0) & 0x400):
        raise RuntimeError(f"Not an ordinary directory: {path}")
    return info.st_dev, info.st_ino


def require_identity(path: Path, expected: tuple[int, int]) -> None:
    if identity(path) != expected:
        raise RuntimeError(f"Directory identity changed: {path}")


def cleanup_run(root: Path, root_id: tuple[int, int], run: Path,
                run_id: tuple[int, int]) -> None:
    """Reject replaced roots and reparse points instead of following them."""
    require_identity(root, root_id)
    if run.parent != root or not run.name.startswith("qcs-t-"):
        raise RuntimeError(f"Cleanup boundary violation: {run}")
    require_identity(run, run_id)
    for current, directories, files in os.walk(run, followlinks=False):
        for name in directories + files:
            item = Path(current) / name
            info = item.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise RuntimeError(f"Refusing cleanup across a link or reparse point: {item}")
    require_identity(root, root_id)
    require_identity(run, run_id)
    shutil.rmtree(run)
    if run.exists() or run.is_symlink():
        raise RuntimeError(f"Test directory remains after cleanup: {run}")


def write_report(root: Path, root_id: tuple[int, int], report: dict[str, Any]) -> Path:
    require_identity(root, root_id)
    directory = root / "reports"
    directory.mkdir(exist_ok=True)
    directory_id = identity(directory)
    destination = directory / f"{report['runId']}.json"
    descriptor, temporary = tempfile.mkstemp(prefix=".qcs-", suffix=".json", dir=directory)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        require_identity(root, root_id)
        require_identity(directory, directory_id)
        os.replace(temporary, destination)
    finally:
        # A replaced parent must not redirect even removal of the temporary report.
        require_identity(root, root_id)
        require_identity(directory, directory_id)
        if os.path.isfile(temporary):
            os.unlink(temporary)
    return destination


def stop_child(child: subprocess.Popen[Any]) -> None:
    if child.poll() is not None:
        return
    if os.name == "nt":
        # The PID is this wrapper's live child. /T also terminates its descendants.
        result = subprocess.run(["taskkill", "/PID", str(child.pid), "/T", "/F"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                check=False)
        if result.returncode and child.poll() is None:
            child.kill()
    else:
        try:
            os.killpg(child.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
    try:
        child.wait(timeout=5)
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            child.kill()
        else:
            os.killpg(child.pid, signal.SIGKILL)
        child.wait(timeout=5)


def run_test(args: argparse.Namespace) -> int:
    selected_root = os.environ.get("QCOPILOTS_SEARCH_TEST_ROOT") or args.root
    root = Path(selected_root).expanduser().absolute()
    root_id: tuple[int, int] | None = None
    run: Path | None = None
    run_id: tuple[int, int] | None = None
    child: subprocess.Popen[Any] | None = None
    old_handlers: dict[int, Any] = {}
    interrupted: list[int] = []
    started = time.monotonic()
    label = re.sub(r"[^A-Za-z0-9_.-]", "-", args.label).strip(".-")[:16] or "test"
    report: dict[str, Any] = {
        "schemaVersion": 1, "kind": "qcs-test-run", "label": args.label,
        "startedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
        "root": str(root), "command": args.command, "childExitCode": None,
        "exitCode": FAILURE, "passed": False, "retained": False,
        "cleanup": {"attempted": False, "succeeded": False, "error": None},
    }

    def on_signal(number: int, _frame: Any) -> None:
        interrupted.append(number)
        raise KeyboardInterrupt

    try:
        # Allow canonical system temporary ancestors (e.g. macOS /var), but never
        # accept the requested test root itself as a symlink or directory junction.
        if root.exists() or root.is_symlink():
            identity(root)
        root = root.resolve()
        report["root"] = str(root)
        root.mkdir(parents=True, exist_ok=True)
        root_id = identity(root)
        run = Path(tempfile.mkdtemp(prefix=f"qcs-t-{label}-", dir=root))
        run_id = identity(run)
        cwd, temporary = run / "cwd", run / "tmp"
        report.update(runId=run.name, runDirectory=str(run),
                      workingDirectory=str(cwd), testRoot=str(temporary))
        cwd.mkdir()
        temporary.mkdir()
        env = os.environ.copy()
        env.update(QCOPILOTS_SEARCH_TEST_ROOT=str(temporary),
                   QCOPILOTS_SEARCH_TEST_RUN_DIR=str(run),
                   TMP=str(temporary), TEMP=str(temporary), TMPDIR=str(temporary))
        for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
            number = getattr(signal, name, None)
            if number is not None:
                old_handlers[number] = signal.signal(number, on_signal)
        options: dict[str, Any] = {"cwd": cwd, "env": env}
        if os.name == "nt":
            options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            options["start_new_session"] = True
        child = subprocess.Popen(args.command, **options)
        report["childExitCode"] = child.wait()
        report["exitCode"] = report["childExitCode"]
    except KeyboardInterrupt:
        report["error"] = "Test execution interrupted"
        report["signal"] = interrupted[0] if interrupted else signal.SIGINT
        report["exitCode"] = 128 + report["signal"]
    except (OSError, RuntimeError) as error:
        report["error"] = str(error)
        report["exitCode"] = FAILURE
    finally:
        # Restore handlers before teardown to avoid recursively interrupting cleanup.
        for number, previous in old_handlers.items():
            signal.signal(number, previous)
        if child is not None and child.poll() is None:
            try:
                stop_child(child)
            except (OSError, subprocess.SubprocessError) as error:
                report["error"] = f"Could not stop child process: {error}"
                report["exitCode"] = FAILURE
        if child is not None:
            report["childExitCode"] = child.poll()
        if run is not None and root_id is not None and run_id is not None:
            keep = report["exitCode"] != 0 and (
                args.keep_failed or os.environ.get("QCOPILOTS_SEARCH_KEEP_TEST_ARTIFACTS") == "1"
                or os.environ.get("QCOPILOTS_SEARCH_KEEP_FAILED_TESTS") == "1")
            if keep or (child is not None and child.poll() is None):
                report["retained"] = True
            else:
                report["cleanup"]["attempted"] = True
                try:
                    cleanup_run(root, root_id, run, run_id)
                    report["cleanup"]["succeeded"] = True
                except (OSError, RuntimeError) as error:
                    report["cleanup"]["error"] = str(error)
                    report["retained"] = True
                    report["exitCode"] = FAILURE
        report["durationSeconds"] = round(time.monotonic() - started, 6)
        report["passed"] = report["exitCode"] == 0
        if "runId" in report and root_id is not None:
            try:
                path = write_report(root, root_id, report)
                print(f"QCS test report: {path}", file=sys.stderr, flush=True)
            except (OSError, RuntimeError) as error:
                report["reportError"] = str(error)
                report["passed"] = False
                report["exitCode"] = FAILURE
                print(json.dumps(report, ensure_ascii=False), file=sys.stderr, flush=True)
        else:
            print(json.dumps(report, ensure_ascii=False), file=sys.stderr, flush=True)
    result = int(report["exitCode"])
    return 128 - result if result < 0 else result


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--label", required=True)
    parser.add_argument("--root", required=True)
    parser.add_argument("--keep-failed", action="store_true")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.command and args.command[0] == "--":
        args.command.pop(0)
    if not args.command:
        parser.error("a test command is required after --")
    return run_test(args)


if __name__ == "__main__":
    raise SystemExit(main())
