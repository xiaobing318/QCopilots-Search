#!/usr/bin/env python3
"""Install, exercise and remove a DEB only inside a disposable Ubuntu container."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import uuid

PRODUCT = "qcopilots-search"
SOURCE = Path(__file__).resolve().parents[2]
IMAGE = "ubuntu:22.04@sha256:5ec03bb3441e8b0bf3b4f9cd4629a1ae763010dc3035bb8da3ae6cf026486401"
for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")


def digest(path):
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest()


def run(command, *, check=True, env=None):
    command = [str(item) for item in command]
    print("+ " + subprocess.list2cmdline(command), flush=True)
    result = subprocess.run(command, check=False, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", env=env)
    print(result.stdout, end="")
    print(result.stderr, end="", file=sys.stderr)
    if check and result.returncode:
        raise RuntimeError(f"Command exited {result.returncode}: {command[0]}")
    return result


def external(path):
    resolved = path.expanduser().resolve()
    if resolved == SOURCE or SOURCE in resolved.parents or resolved == Path(resolved.anchor):
        raise RuntimeError(f"DEB verification needs an external non-root directory: {resolved}")
    return resolved


def snapshot_profile(profile):
    result = {}
    for path in sorted(profile.rglob("*")):
        if path.is_symlink():
            raise RuntimeError("Unexpected symlink in the isolated DEB test profile")
        if path.is_file():
            result[path.relative_to(profile).as_posix()] = digest(path)
    return result


def inside_container():
    if platform.system() != "Linux" or os.geteuid() != 0 or not Path("/.dockerenv").is_file() or os.environ.get("QCOPILOTS_SEARCH_DEB_CONTAINER") != "1":
        raise RuntimeError("The installation phase may only run in the explicitly created disposable container")
    package = Path("/incoming/qcopilots-search.deb")
    report_root = Path("/reports")
    work = Path("/work")
    profile = work / "user-profile"
    report = {"schemaVersion": 1, "scenario": "deb-install-uninstall", "passed": False,
              "containerImage": IMAGE, "packageSha256": digest(package),
              "commit": os.environ.get("QCOPILOTS_SEARCH_CI_COMMIT"), "checks": []}
    installed = False
    try:
        report["osRelease"] = Path("/etc/os-release").read_text(encoding="utf-8")
        name = run(["dpkg-deb", "--field", package, "Package"]).stdout.strip()
        if name != PRODUCT:
            raise RuntimeError(f"Unexpected DEB package identity: {name}")
        report["packageVersion"] = run(["dpkg-deb", "--field", package, "Version"]).stdout.strip()
        previous = run(["dpkg-query", "--show", "--showformat=${db:Status-Abbrev}", PRODUCT], check=False)
        if previous.returncode == 0 and previous.stdout.startswith("ii"):
            raise RuntimeError("The clean container unexpectedly contains an existing installation")
        run(["dpkg", "--install", package], check=False)
        # Resolve only the package manager's declared dependencies inside this
        # disposable filesystem, then require a successful direct dpkg install.
        run(["apt-get", "--yes", "--no-install-recommends", "--fix-broken", "install"])
        run(["dpkg", "--install", package])
        installed = True
        status = run(["dpkg-query", "--show", "--showformat=${db:Status-Abbrev}", PRODUCT]).stdout
        if not status.startswith("ii"):
            raise RuntimeError("DEB is not in the fully installed state")
        files = run(["dpkg-query", "--listfiles", PRODUCT]).stdout
        manifest = work / "qcopilots-search-dpkg-files.txt"
        manifest.write_text(files, encoding="utf-8")
        payload_files = [Path(line) for line in files.splitlines() if line.startswith("/") and (Path(line).is_file() or Path(line).is_symlink())]
        report["installedFiles"] = len(payload_files)
        report["checks"].append("dpkg installed the final DEB under /usr")
        user = "qcopilots-search-tester"
        user_id = int(os.environ["QCOPILOTS_SEARCH_HOST_UID"])
        group_id = int(os.environ["QCOPILOTS_SEARCH_HOST_GID"])
        if user_id <= 0 or group_id <= 0:
            raise RuntimeError("The host and package test user must be non-root")
        import grp
        try:
            grp.getgrgid(group_id)
        except KeyError:
            run(["groupadd", "--gid", str(group_id), user])
        run(["useradd", "--create-home", "--uid", str(user_id), "--gid", str(group_id), user])
        # Matching the host IDs lets the outer runner retain ownership of its
        # mounted reports and test data. Never recursively chown host content.
        run(["chown", f"{user_id}:{group_id}", work, report_root])
        # The common verifier exercises the actual installation as a normal
        # user. It neither copies nor deletes /usr, and validates loaded Qt and
        # native libraries against the dpkg-owned file manifest.
        run(["runuser", "--user", user, "--", "xvfb-run", "--auto-servernum",
             "--server-args=-screen 0 1600x1000x24", "python3",
             "/source/tools/qcopilots-search-verify-package.py",
             "--build-root", work, "--installed-root", "/usr",
             "--installed-manifest", manifest, "--profile-dir", profile,
             "--report-dir", report_root / "smoke"])
        smoke = json.loads((report_root / "smoke/report.json").read_text(encoding="utf-8"))
        if smoke.get("passed") is not True:
            raise RuntimeError("The installed DEB failed the common package smoke")
        report["checks"].append("non-root GUI, service, worker, OCR, PDF, media and search smoke passed")
        sentinel = profile / "qcopilots-search-uninstall-preservation.txt"
        sentinel.write_text(str(uuid.uuid4()), encoding="utf-8")
        before = snapshot_profile(profile)
        run(["dpkg", "--remove", PRODUCT])
        installed = False
        after = snapshot_profile(profile)
        if before != after:
            raise RuntimeError("DEB removal changed the isolated user profile")
        remaining = [str(path) for path in payload_files if path.exists() or path.is_symlink()]
        if remaining:
            raise RuntimeError("DEB removal left package payload files: " + ", ".join(remaining[:10]))
        report["retainedProfileFiles"] = len(after)
        report["checks"].append("dpkg removed all owned payload files and preserved every user profile file hash")
        report["passed"] = True
    except Exception as error:
        report["error"] = str(error)
    finally:
        if installed:
            cleanup = run(["dpkg", "--remove", PRODUCT], check=False)
            if cleanup.returncode:
                report["cleanupError"] = "dpkg removal failed inside the disposable container"
        (report_root / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2))
    if not report["passed"]:
        raise RuntimeError(report.get("error", "DEB acceptance failed"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-root", type=Path)
    parser.add_argument("--package-file", type=Path)
    parser.add_argument("--report-dir", type=Path)
    parser.add_argument("--inside-container", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.inside_container:
        inside_container()
        return
    if platform.system() != "Linux":
        raise RuntimeError("DEB acceptance requires the native Linux CI host")
    if os.geteuid() == 0 or os.getegid() == 0:
        raise RuntimeError("Start DEB acceptance from a non-root CI user with Docker access")
    if not args.build_root or not args.package_file:
        parser.error("--build-root and --package-file are required")
    root = external(args.build_root)
    package = args.package_file.resolve()
    report = external(args.report_dir or root / "reports/package-deb")
    report.mkdir(parents=True, exist_ok=True)
    work = root / "test-runs" / (PRODUCT + "-deb-" + str(uuid.uuid4()))
    work.mkdir(parents=True)
    if package.suffix != ".deb" or not package.is_file():
        raise RuntimeError("A generated DEB file is required")
    container_script = """set -eu
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install --yes --no-install-recommends python3 xvfb xauth ca-certificates passwd \\
  libgl1 libegl1 libopengl0 libfontconfig1 libfreetype6 libglib2.0-0 libdbus-1-3 \\
  libx11-xcb1 libxcb-cursor0 libxcb-icccm4 libxcb-image0 libxcb-keysyms1 \\
  libxcb-randr0 libxcb-render-util0 libxcb-xinerama0 libxcb-xfixes0 libxkbcommon-x11-0 \\
  libasound2 libpulse0 fonts-noto-cjk fonts-dejavu-core
exec python3 /source/.github/scripts/qcopilots-search-deb-ci.py --inside-container
"""
    command = ["docker", "run", "--rm", "--platform", "linux/amd64",
               "--env", "QCOPILOTS_SEARCH_DEB_CONTAINER=1",
               "--env", "QCOPILOTS_SEARCH_HOST_UID=" + str(os.getuid()),
               "--env", "QCOPILOTS_SEARCH_HOST_GID=" + str(os.getgid()),
               "--env", "QCOPILOTS_SEARCH_CI_COMMIT=" + os.environ.get("GITHUB_SHA", ""),
               "--mount", f"type=bind,source={SOURCE},target=/source,readonly",
               "--mount", f"type=bind,source={package},target=/incoming/qcopilots-search.deb,readonly",
               "--mount", f"type=bind,source={report},target=/reports",
               "--mount", f"type=bind,source={work},target=/work",
               IMAGE, "bash", "-c", container_script]
    result = run(command, check=False)
    (report / "container.log").write_text(result.stdout + result.stderr, encoding="utf-8")
    if result.returncode:
        raise RuntimeError("Disposable DEB installation acceptance failed. See container.log and report.json.")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"{PRODUCT}: {error}", file=sys.stderr)
        sys.exit(1)
