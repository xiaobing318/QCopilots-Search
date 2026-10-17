#!/usr/bin/env python3
"""Install, exercise and remove a DEB only inside a disposable Ubuntu container."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import stat
import subprocess
import sys
import tempfile
import uuid

PRODUCT = "qcs"
SOURCE = Path(__file__).resolve().parents[2]
IMAGE = "ubuntu:22.04@sha256:5ec03bb3441e8b0bf3b4f9cd4629a1ae763010dc3035bb8da3ae6cf026486401"
IMAGES = {
    "ubuntu-22.04": IMAGE,
    "ubuntu-24.04": "ubuntu:24.04@sha256:534baea6a22c03a63003dbc8dbe78fe34bc0d7e595d9a9dc9834884ff530eb55",
}
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


def linux_variant(identifier):
    variants = json.loads((SOURCE / "config/build-variants.json").read_text(encoding="utf-8"))["variants"]
    matches = [variant for variant in variants if variant["id"] == identifier]
    if (len(matches) != 1 or matches[0]["os"] != "linux" or matches[0]["arch"] != "x64" or
            matches[0]["runner"] not in IMAGES):
        raise RuntimeError(f"DEB acceptance needs a supported Linux x64 variant and Ubuntu baseline: {identifier}")
    return matches[0]


def container_package_filename(filename):
    if not isinstance(filename, str) or not re.fullmatch(r"qcs-[A-Za-z0-9][A-Za-z0-9._-]*\.deb", filename):
        raise RuntimeError("A safe original DEB package filename is required")
    return filename


def inside_container():
    if platform.system() != "Linux" or os.geteuid() != 0 or not Path("/.dockerenv").is_file() or os.environ.get("QCOPILOTS_SEARCH_DEB_CONTAINER") != "1":
        raise RuntimeError("The installation phase may only run in the explicitly created disposable container")
    filename = container_package_filename(os.environ.get("QCOPILOTS_SEARCH_DEB_FILENAME"))
    package = Path("/incoming/" + filename)
    report_root = Path("/reports")
    work = Path("/work")
    user_id = int(os.environ["QCOPILOTS_SEARCH_HOST_UID"])
    group_id = int(os.environ["QCOPILOTS_SEARCH_HOST_GID"])
    if user_id <= 0 or group_id <= 0:
        raise RuntimeError("The host and package test user must be non-root")
    variant = linux_variant(os.environ.get("QCOPILOTS_SEARCH_VARIANT", "linux-x64-cpu"))
    install_and_verify(package, report_root, work, Path("/source"), Path("/models"),
                       user_id, group_id, image=IMAGES[variant["runner"]], variant=variant["id"])


def install_and_verify(package, report_root, work, source, models, user_id, group_id,
                       *, image, variant=None, user="qcs-tester", isolated_home=False, environment=None):
    """Only callers that established a disposable container may enter this phase."""
    profile = work / "user-profile"
    report = {"schemaVersion": 1, "scenario": "deb-install-uninstall", "passed": False,
              "verificationScope": "full", "fullVerificationPassed": False,
              "containerImage": image, "packageSha256": digest(package),
              "variantId": variant,
              "commit": os.environ.get("QCOPILOTS_SEARCH_CANDIDATE_COMMIT") or
                        os.environ.get("QCOPILOTS_SEARCH_CANDIDATE_HEAD"), "checks": []}
    installed = False
    user_created = False
    group_created = False
    try:
        report["osRelease"] = Path("/etc/os-release").read_text(encoding="utf-8")
        name = run(["dpkg-deb", "--field", package, "Package"]).stdout.strip()
        if name != PRODUCT:
            raise RuntimeError(f"Unexpected DEB package identity: {name}")
        report["packageVersion"] = run(["dpkg-deb", "--field", package, "Version"]).stdout.strip()
        previous = run(["dpkg-query", "--show", "--showformat=${db:Status-Abbrev}", PRODUCT], check=False)
        if previous.returncode == 0 and previous.stdout.startswith("ii"):
            raise RuntimeError("The clean container unexpectedly contains an existing installation")
        # dpkg can partially install a package before returning failure. The
        # finally block must remove that partial installation as well.
        installed = True
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
        manifest = work / "qcs-dpkg-files.txt"
        manifest.write_text(files, encoding="utf-8")
        payload_files = [Path(line) for line in files.splitlines() if line.startswith("/") and (Path(line).is_file() or Path(line).is_symlink())]
        report["installedFiles"] = len(payload_files)
        report["checks"].append("dpkg installed the final DEB under /usr")
        import grp
        try:
            grp.getgrgid(group_id)
        except KeyError:
            run(["groupadd", "--gid", str(group_id), user])
            group_created = True
        home_arguments = ["--home-dir", work / "home"] if isolated_home else []
        run(["useradd", "--create-home", *home_arguments, "--uid", str(user_id), "--gid", str(group_id), user])
        user_created = True
        # Matching the host IDs lets the outer runner retain ownership of its
        # mounted reports and test data. Never recursively chown host content.
        run(["chown", f"{user_id}:{group_id}", work, report_root])
        # The common verifier exercises the actual installation as a normal
        # user. It neither copies nor deletes /usr, and validates loaded Qt and
        # native libraries against the dpkg-owned file manifest.
        variant_arguments = ["--variant", variant] if variant else []
        run(["runuser", "--user", user, "--", "xvfb-run", "--auto-servernum",
             "--server-args=-screen 0 1600x1000x24", "python3",
             source / "tools/qcs-verify-package.py",
             "--build-root", work, "--package-file", package, "--installed-root", "/usr",
             "--installed-manifest", manifest, "--profile-dir", profile,
             "--model-dir", models, "--report-dir", report_root / "smoke", *variant_arguments], env=environment)
        smoke = json.loads((report_root / "smoke/report.json").read_text(encoding="utf-8"))
        if (smoke.get("passed") is not True or smoke.get("verificationScope") != "full"
                or smoke.get("fullVerificationPassed") is not True
                or smoke.get("modelSmoke", {}).get("passed") is not True):
            raise RuntimeError("The installed DEB failed the complete package and real-model smoke")
        verified_commit = smoke.get("commit")
        if verified_commit is not None and (
                not isinstance(verified_commit, str) or not re.fullmatch(r"[0-9a-f]{40}", verified_commit)):
            raise RuntimeError("The installed DEB smoke has an invalid source commit")
        if smoke.get("buildIdentity", {}).get("candidateCommit") != verified_commit:
            raise RuntimeError("The installed DEB embedded commit differs from its verified smoke commit")
        for name in ("QCOPILOTS_SEARCH_CANDIDATE_COMMIT", "QCOPILOTS_SEARCH_CANDIDATE_HEAD"):
            if os.environ.get(name) and os.environ[name] != verified_commit:
                raise RuntimeError(f"{name} differs from the installed DEB verified smoke commit")
        if smoke.get("packageSha256") != report["packageSha256"]:
            raise RuntimeError("The installed DEB smoke package digest differs from the original installer")
        report["commit"] = verified_commit
        report["checks"].append("non-root GUI, service, worker, OCR, PDF, media, real-model inference and search smoke passed")
        sentinel = profile / "qcs-uninstall-preservation.txt"
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
        report["fullVerificationPassed"] = True
        report["passed"] = True
    except Exception as error:
        report["error"] = str(error)
    finally:
        if installed:
            cleanup = run(["dpkg", "--remove", PRODUCT], check=False)
            if cleanup.returncode:
                report["cleanupError"] = "dpkg removal failed inside the disposable container"
                report["passed"] = report["fullVerificationPassed"] = False
        if isolated_home and user_created:
            cleanup = run(["userdel", user], check=False)
            if cleanup.returncode:
                report["cleanupError"] = "Could not remove the isolated container test account"
                report["passed"] = report["fullVerificationPassed"] = False
        if isolated_home and group_created:
            # Some distributions remove the user's matching group as part of
            # userdel. Do not turn that successful cleanup into a failure.
            try:
                grp.getgrnam(user)
            except KeyError:
                pass
            else:
                cleanup = run(["groupdel", user], check=False)
                if cleanup.returncode:
                    report["cleanupError"] = "Could not remove the isolated container test group"
                    report["passed"] = report["fullVerificationPassed"] = False
        (report_root / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2))
    if not report["passed"]:
        raise RuntimeError(report.get("error", report.get("cleanupError", "DEB acceptance failed")))


def job_container_context(args):
    """Reject host/root use before creating any directory or invoking dpkg.

    This is an explicit opt-in for the disposable GitHub-hosted CUDA matrix
    containers. It is not a general-purpose local root installation mode.
    """
    if (platform.system() != "Linux" or os.geteuid() != 0 or
            os.environ.get("QCOPILOTS_SEARCH_CI_CONTAINER") != "1" or
            os.environ.get("GITHUB_ACTIONS") != "true" or
            os.environ.get("RUNNER_ENVIRONMENT") != "github-hosted" or
            not Path("/.dockerenv").is_file()):
        raise RuntimeError("Direct DEB acceptance requires the explicitly selected GitHub-hosted disposable job container")
    for name in ("GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT"):
        if not re.fullmatch(r"[1-9][0-9]*", os.environ.get(name, "")):
            raise RuntimeError(f"A valid GitHub job identity is required: {name}")
    workspace = os.environ.get("GITHUB_WORKSPACE")
    workspace_path = Path(workspace).resolve() if workspace else None
    if (workspace_path is None or SOURCE not in (workspace_path, workspace_path / "qcs-src") or
            not (SOURCE / ".git").exists()):
        raise RuntimeError("The container verifier must execute from the GitHub workspace checkout")
    variant_id = os.environ.get("QCOPILOTS_SEARCH_VARIANT")
    if variant_id != os.environ.get("QCOPILOTS_SEARCH_CI_ID"):
        raise RuntimeError("The job container variant and CI identity differ")
    if getattr(args, "variant", variant_id) != variant_id:
        raise RuntimeError("The requested variant differs from the job container identity")
    variant = linux_variant(variant_id)
    if (variant["backend"] != "cuda" or not variant.get("container") or
            variant["container"] != os.environ.get("QCOPILOTS_SEARCH_CI_CONTAINER_IMAGE")):
        raise RuntimeError("Direct DEB acceptance is limited to the locked Linux CUDA job containers")
    temporary_value = os.environ.get("RUNNER_TEMP")
    if not temporary_value:
        raise RuntimeError("RUNNER_TEMP is required for disposable container verification")
    temporary = external(Path(temporary_value))
    if not temporary.is_dir():
        raise RuntimeError("RUNNER_TEMP does not exist")

    def within_temporary(path):
        resolved = external(path)
        if temporary not in resolved.parents:
            raise RuntimeError(f"Container verification paths must be strictly inside RUNNER_TEMP: {resolved}")
        return resolved

    root = within_temporary(args.build_root)
    package = within_temporary(args.package_file)
    models = within_temporary(args.model_dir)
    report = within_temporary(args.report_dir or root / "reports/package-deb")
    if not root.is_dir() or not models.is_dir() or not package.is_file() or package.suffix != ".deb":
        raise RuntimeError("The job container requires an existing build root, model directory and DEB")
    if root not in package.parents or root not in report.parents:
        raise RuntimeError("The DEB and its report must belong to the selected external build root")
    if report.exists():
        raise RuntimeError("Use a fresh report directory for each DEB verification")
    if report == models or models in report.parents or report in models.parents:
        raise RuntimeError("The DEB report directory must not overlap the read-only model cache")
    return {"root": root, "package": package, "models": models, "report": report,
            "temporary": temporary, "image": variant["container"], "variant": variant_id}


def job_container(args):
    context = job_container_context(args)
    import grp
    import pwd
    # Select an unused non-root identity without changing an existing account.
    identities = {entry.pw_uid for entry in pwd.getpwall()} | {entry.gr_gid for entry in grp.getgrall()}
    user_id = next((value for value in range(20000, 21000) if value not in identities), None)
    if user_id is None:
        raise RuntimeError("No unused isolated container test identity is available")
    user = "qcs-deb-" + uuid.uuid4().hex[:12]
    report = context["report"]
    report.mkdir(parents=True)
    work = Path(tempfile.mkdtemp(prefix="qcs-deb-", dir=context["temporary"]))
    original_modes = {}

    def accessible(path, required):
        # Only runner-owned temporary content may have its permissions changed.
        if path != context["temporary"] and context["temporary"] not in path.parents:
            raise RuntimeError(f"Refusing to change permissions outside RUNNER_TEMP: {path}")
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & required != required:
            original_modes.setdefault(path, mode)
            path.chmod(mode | required)

    try:
        # mkdtemp roots are normally 0700. Grant only the traversal/read access
        # needed by the disposable user, then restore every original mode.
        for target in (work, report, context["models"]):
            for parent in (target, *target.parents):
                accessible(parent, stat.S_IXOTH)
                if parent == context["temporary"]:
                    break
        accessible(context["models"], stat.S_IROTH | stat.S_IXOTH)
        for path in context["models"].rglob("*"):
            if path.is_symlink():
                raise RuntimeError("Container model cache must not contain symbolic links")
            accessible(path, stat.S_IROTH | (stat.S_IXOTH if path.is_dir() else 0))
        environment = os.environ.copy()
        environment["QCOPILOTS_SEARCH_TEST_ROOT"] = str(work / "runs")
        # Git otherwise rejects the root-owned checkout when its identity is
        # inspected as the isolated user. Scope trust to this exact command.
        environment.update({"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "safe.directory",
                            "GIT_CONFIG_VALUE_0": str(SOURCE)})
        install_and_verify(context["package"], report, work, SOURCE, context["models"],
                           user_id, user_id, image=context["image"], variant=context["variant"], user=user,
                           isolated_home=True, environment=environment)
    finally:
        failures = []
        for path, mode in reversed(list(original_modes.items())):
            try:
                path.chmod(mode)
            except OSError as error:
                failures.append(f"{path}: {error}")
        if failures:
            raise RuntimeError("Cannot restore container test directory permissions: " + ", ".join(failures))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-root", type=Path)
    parser.add_argument("--package-file", type=Path)
    parser.add_argument("--model-dir", type=Path)
    parser.add_argument("--report-dir", type=Path)
    parser.add_argument("--variant", default=os.environ.get("QCOPILOTS_SEARCH_VARIANT", "linux-x64-cpu"),
                        help="Linux variant from config/build-variants.json (default: Linux CPU)")
    parser.add_argument("--inside-container", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.inside_container:
        inside_container()
        return
    if platform.system() != "Linux":
        raise RuntimeError("DEB acceptance requires the native Linux CI host")
    if not args.build_root or not args.package_file or not args.model_dir:
        parser.error("--build-root, --package-file and --model-dir are required")
    if os.environ.get("QCOPILOTS_SEARCH_CI_CONTAINER") == "1":
        job_container(args)
        return
    if os.geteuid() == 0 or os.getegid() == 0:
        raise RuntimeError("Start DEB acceptance from a non-root CI user with Docker access")
    variant = linux_variant(args.variant)
    root = external(args.build_root)
    package = args.package_file.resolve()
    filename = container_package_filename(package.name)
    models = args.model_dir.expanduser().resolve()
    if not models.is_dir():
        raise RuntimeError("Required real-model directory does not exist")
    report = external(args.report_dir or root / "reports/package-deb")
    report.mkdir(parents=True, exist_ok=True)
    run_root = root / "runs"
    run_root.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="qcs-deb-", dir=run_root))
    if package.suffix != ".deb" or not package.is_file():
        raise RuntimeError("A generated DEB file is required")
    glib_package = "libglib2.0-0t64" if variant["runner"] == "ubuntu-24.04" else "libglib2.0-0"
    audio_package = "libasound2t64" if variant["runner"] == "ubuntu-24.04" else "libasound2"
    container_script = f"""set -eu
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install --yes --no-install-recommends python3 git xvfb xauth ca-certificates passwd \\
  libgl1 libegl1 libopengl0 libfontconfig1 libfreetype6 {glib_package} libdbus-1-3 \\
  libx11-xcb1 libxcb-cursor0 libxcb-icccm4 libxcb-image0 libxcb-keysyms1 \\
  libxcb-randr0 libxcb-render-util0 libxcb-shape0 libxcb-xinerama0 libxcb-xfixes0 libxkbcommon-x11-0 \\
  {audio_package} libpulse0 fonts-noto-cjk fonts-dejavu-core
exec python3 /source/.github/scripts/qcs-deb-ci.py --inside-container
"""
    candidate_environment = {name: os.environ[name] for name in
        ("QCOPILOTS_SEARCH_CANDIDATE_COMMIT", "QCOPILOTS_SEARCH_CANDIDATE_HEAD")
        if os.environ.get(name)}
    command = ["docker", "run", "--rm", "--platform", "linux/amd64",
               "--env", "QCOPILOTS_SEARCH_DEB_CONTAINER=1",
               "--env", "QCOPILOTS_SEARCH_DEB_FILENAME=" + filename,
               "--env", "QCOPILOTS_SEARCH_VARIANT=" + variant["id"],
               "--env", "QCOPILOTS_SEARCH_CI_ID=" + variant["id"],
               "--env", "QCOPILOTS_SEARCH_HOST_UID=" + str(os.getuid()),
               "--env", "QCOPILOTS_SEARCH_HOST_GID=" + str(os.getgid()),
               *(argument for name, value in candidate_environment.items() for argument in ("--env", name + "=" + value)),
               "--mount", f"type=bind,source={SOURCE},target=/source,readonly",
               "--mount", f"type=bind,source={package},target=/incoming/{filename},readonly",
               "--mount", f"type=bind,source={models},target=/models,readonly",
               "--mount", f"type=bind,source={report},target=/reports",
               "--mount", f"type=bind,source={work},target=/work",
               IMAGES[variant["runner"]], "bash", "-c", container_script]
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
