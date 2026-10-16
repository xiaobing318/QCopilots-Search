#!/usr/bin/env python3
"""Exercise DEB container boundaries without changing the host package manager."""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

sys.dont_write_bytecode = True
SPEC = importlib.util.spec_from_file_location("qcs_deb_ci", Path(__file__).resolve().parents[2] / ".github/scripts/qcs-deb-ci.py")
deb = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(deb)
VARIANTS = json.loads((deb.SOURCE / "config/build-variants.json").read_text(encoding="utf-8"))["variants"]
CUDA = next(variant for variant in VARIANTS if variant["os"] == "linux" and variant["backend"] == "cuda")
HEAD = "a" * 40


class DebContainerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="qcs-deb-test-",
            dir=os.environ.get("QCOPILOTS_SEARCH_TEST_ROOT") or None)
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.workspace = self.base / "workspace"
        self.source = self.workspace / "qcs-src"
        (self.source / ".git").mkdir(parents=True)
        (self.source / "config").mkdir()
        (self.source / "config/build-variants.json").write_text(json.dumps({"variants": VARIANTS}), encoding="utf-8")
        self.runner = self.base / "runner-temp"
        self.root = self.runner / "qcs-build" / CUDA["id"]
        self.models = self.root.parent / "models"
        self.models.mkdir(parents=True)
        (self.models / "model.gguf").write_bytes(b"unit fixture, never used for inference")
        self.package = self.root / "pkg/Release/qcs-test.deb"
        self.package.parent.mkdir(parents=True)
        self.package.write_bytes(b"unit fixture, never installed")
        self.report = self.root / "reports/Release/check-one/package-deb"
        self.args = argparse.Namespace(build_root=self.root, package_file=self.package,
                                       model_dir=self.models, report_dir=self.report)
        self.environment = {"QCOPILOTS_SEARCH_CI_CONTAINER": "1", "GITHUB_ACTIONS": "true",
            "RUNNER_ENVIRONMENT": "github-hosted", "GITHUB_RUN_ID": "12", "GITHUB_RUN_ATTEMPT": "2",
            "GITHUB_WORKSPACE": str(self.workspace), "RUNNER_TEMP": str(self.runner),
            "QCOPILOTS_SEARCH_VARIANT": CUDA["id"], "QCOPILOTS_SEARCH_CI_ID": CUDA["id"],
            "QCOPILOTS_SEARCH_CI_CONTAINER_IMAGE": CUDA["container"],
            "QCOPILOTS_SEARCH_CANDIDATE_HEAD": HEAD}
        self.docker = self.base / ".dockerenv"
        self.docker.touch()
        self.os_release = self.base / "os-release"
        self.os_release.write_text("ID=ubuntu\nVERSION_ID=24.04\n", encoding="utf-8")
        self.payload = self.base / "installed/qcs-gui"

    def path(self, value):
        return {"/.dockerenv": self.docker, "/etc/os-release": self.os_release,
                "/usr/bin/qcs-gui": self.payload}.get(str(value), Path(value))

    def guarded(self, overrides=None):
        stack = ExitStack()
        stack.enter_context(mock.patch.object(deb, "SOURCE", self.source))
        stack.enter_context(mock.patch.object(deb, "Path", side_effect=self.path))
        stack.enter_context(mock.patch.object(deb.platform, "system", return_value="Linux"))
        stack.enter_context(mock.patch.object(deb.os, "geteuid", return_value=0, create=True))
        stack.enter_context(mock.patch.dict(deb.os.environ, {**self.environment, **(overrides or {})}, clear=True))
        return stack

    def test_accepts_manifest_locked_cuda_with_expected_checkout_and_new_layout(self):
        with self.guarded():
            result = deb.job_container_context(self.args)
        self.assertEqual(result["package"], self.package)
        self.assertEqual(result["models"], self.models)
        self.assertEqual(result["image"], CUDA["container"])
        self.assertFalse(self.report.exists())

    def test_rejects_local_self_hosted_wrong_identity_and_wrong_image_without_mutation(self):
        invalid = {"QCOPILOTS_SEARCH_CI_CONTAINER": "", "GITHUB_ACTIONS": "false",
            "RUNNER_ENVIRONMENT": "self-hosted", "GITHUB_RUN_ID": "0", "GITHUB_RUN_ATTEMPT": "x",
            "GITHUB_WORKSPACE": str(self.base), "QCOPILOTS_SEARCH_VARIANT": "linux-x64-cpu",
            "QCOPILOTS_SEARCH_CI_CONTAINER_IMAGE": "ubuntu:24.04", "RUNNER_TEMP": ""}
        for name, value in invalid.items():
            with self.subTest(variable=name), self.guarded({name: value}), mock.patch.object(deb, "run") as run:
                with self.assertRaises(RuntimeError):
                    deb.job_container(self.args)
                run.assert_not_called()
                self.assertFalse(self.report.exists())

    def test_rejects_non_cuda_variant_even_with_matching_ci_identity(self):
        with self.guarded({"QCOPILOTS_SEARCH_VARIANT": "linux-x64-cpu",
                           "QCOPILOTS_SEARCH_CI_ID": "linux-x64-cpu"}):
            with self.assertRaisesRegex(RuntimeError, "locked Linux CUDA"):
                deb.job_container_context(self.args)

    def test_requires_root_disposable_container_marker(self):
        with self.guarded(), mock.patch.object(deb.os, "geteuid", return_value=1000):
            with self.assertRaisesRegex(RuntimeError, "disposable job container"):
                deb.job_container_context(self.args)
        self.docker.unlink()
        with self.guarded():
            with self.assertRaisesRegex(RuntimeError, "disposable job container"):
                deb.job_container_context(self.args)

    def test_rejects_every_input_outside_runner_temp(self):
        for name in ("build_root", "package_file", "model_dir", "report_dir"):
            args = argparse.Namespace(**vars(self.args))
            setattr(args, name, self.base / "outside")
            with self.subTest(argument=name), self.guarded():
                with self.assertRaisesRegex(RuntimeError, "strictly inside RUNNER_TEMP"):
                    deb.job_container_context(args)
                self.assertFalse(self.report.exists())

    def test_rejects_package_and_report_from_another_build_root(self):
        other = self.runner / "other"
        other.mkdir()
        package = other / "qcs.deb"
        package.write_bytes(b"unused")
        for name, value in (("package_file", package), ("report_dir", other / "report")):
            args = argparse.Namespace(**vars(self.args))
            setattr(args, name, value)
            with self.subTest(argument=name), self.guarded():
                with self.assertRaisesRegex(RuntimeError, "selected external build root"):
                    deb.job_container_context(args)

    def test_rejects_reusing_an_existing_report(self):
        self.report.mkdir(parents=True)
        with self.guarded():
            with self.assertRaisesRegex(RuntimeError, "fresh report"):
                deb.job_container_context(self.args)

    def test_host_docker_uses_manifest_baseline_and_correct_ubuntu_runtime_packages(self):
        verifier_spec = importlib.util.spec_from_file_location("qcs_deb_actual_verifier", deb.SOURCE / "tools/qcs-verify-package.py")
        verifier = importlib.util.module_from_spec(verifier_spec)
        verifier_spec.loader.exec_module(verifier)
        for arguments in (("init",), ("add", "config/build-variants.json"),
                          ("-c", "user.name=QCS fixture", "-c", "user.email=fixture@example.invalid",
                           "-c", "commit.gpgsign=false", "commit", "-m", "Isolated candidate fixture")):
            result = subprocess.run(["git", "-C", str(self.source), *arguments], capture_output=True,
                                    text=True, encoding="utf-8", errors="replace", timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        candidate = subprocess.check_output(["git", "-C", str(self.source), "rev-parse", "HEAD"], text=True).strip()
        for identifier, baseline in (("linux-x64-cpu", "ubuntu-22.04"), ("linux-x64-rocm", "ubuntu-24.04")):
            argv = ["qcs-deb-ci", "--build-root", str(self.root), "--package-file", str(self.package),
                    "--model-dir", str(self.models), "--variant", identifier]
            with self.subTest(variant=identifier), self.guarded({"QCOPILOTS_SEARCH_CI_CONTAINER": "",
                    "QCOPILOTS_SEARCH_CANDIDATE_HEAD": candidate, "QCOPILOTS_SEARCH_CANDIDATE_COMMIT": ""}), \
                    mock.patch.object(deb.sys, "argv", argv), \
                    mock.patch.object(deb.os, "geteuid", return_value=1000, create=True), \
                    mock.patch.object(deb.os, "getegid", return_value=1000, create=True), \
                    mock.patch.object(deb.os, "getuid", return_value=1000, create=True), \
                    mock.patch.object(deb.os, "getgid", return_value=1000, create=True), \
                    mock.patch.object(deb, "run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
                deb.main()
                run.assert_called_once()
                command = run.call_args.args[0]
                self.assertEqual(command[:3], ["docker", "run", "--rm"])
                self.assertIn(deb.IMAGES[baseline], command)
                self.assertIn("QCOPILOTS_SEARCH_VARIANT=" + identifier, command)
                self.assertIn("QCOPILOTS_SEARCH_CI_ID=" + identifier, command)
                forwarded = dict(str(command[index + 1]).split("=", 1)
                                 for index, argument in enumerate(command) if argument == "--env")
                self.assertEqual(forwarded["QCOPILOTS_SEARCH_CANDIDATE_HEAD"], candidate)
                self.assertNotIn("QCOPILOTS_SEARCH_CANDIDATE_COMMIT", forwarded)
                self.assertTrue(all(forwarded.values()), "Docker must not inject empty identity declarations")
                if baseline == "ubuntu-24.04":
                    self.assertIn("libasound2t64", command[-1])
                    self.assertIn("libglib2.0-0t64", command[-1])
                else:
                    self.assertIn("libasound2 ", command[-1])
                    self.assertNotIn("t64", command[-1])
            identity = verifier.package_source_identity(self.source, forwarded)
            self.assertEqual(identity["commit"], candidate)
            self.assertFalse(identity["workingTreeModified"])

    def test_unsupported_linux_baseline_is_rejected(self):
        variants = [{**variant, "runner": "ubuntu-26.04"} for variant in VARIANTS]
        (self.source / "config/build-variants.json").write_text(json.dumps({"variants": variants}), encoding="utf-8")
        with self.guarded():
            with self.assertRaisesRegex(RuntimeError, "Ubuntu baseline"):
                deb.linux_variant(CUDA["id"])

    def test_local_root_cannot_enter_direct_or_docker_installation(self):
        argv = ["qcs-deb-ci", "--build-root", str(self.root), "--package-file", str(self.package),
                "--model-dir", str(self.models)]
        with self.guarded({"QCOPILOTS_SEARCH_CI_CONTAINER": ""}), mock.patch.object(deb.sys, "argv", argv), \
                mock.patch.object(deb, "run") as run:
            with self.assertRaisesRegex(RuntimeError, "non-root CI user"):
                deb.main()
            run.assert_not_called()

    def test_dispatches_container_without_docker_and_restores_permission_changes(self):
        groups = SimpleNamespace(getgrall=lambda: [SimpleNamespace(gr_gid=20000)])
        users = SimpleNamespace(getpwall=lambda: [SimpleNamespace(pw_uid=20001)])
        self.root.parent.chmod(0o700)
        original = stat.S_IMODE(self.root.parent.stat().st_mode)
        with self.guarded(), mock.patch.dict(sys.modules, {"grp": groups, "pwd": users}), \
                mock.patch.object(deb, "install_and_verify", side_effect=RuntimeError("synthetic smoke failure")) as install, \
                mock.patch.object(deb, "run") as run:
            with self.assertRaisesRegex(RuntimeError, "synthetic smoke failure"):
                deb.job_container(self.args)
            run.assert_not_called()
            args, options = install.call_args
            self.assertEqual(args[0], self.package)
            self.assertEqual(args[3:5], (self.source, self.models))
            self.assertEqual(args[5:7], (20002, 20002))
            self.assertTrue(options["isolated_home"])
            self.assertEqual(options["variant"], CUDA["id"])
            self.assertEqual(options["environment"]["GIT_CONFIG_VALUE_0"], str(self.source))
            self.assertTrue(Path(options["environment"]["QCOPILOTS_SEARCH_TEST_ROOT"]).is_relative_to(self.runner))
        self.assertEqual(stat.S_IMODE(self.root.parent.stat().st_mode), original)

    def exercise_install(self, *, complete=True, apt_error=False, preserve=True, cleanup_error=False,
                         environment=None, smoke_identity=None):
        self.report.mkdir(parents=True)
        work = self.runner / "isolated-work"
        work.mkdir()
        self.payload.parent.mkdir()
        self.payload.write_bytes(b"never execute this fixture")
        calls = []
        query_count = 0

        def fake_run(command, **options):
            nonlocal query_count
            command = [str(value) for value in command]
            calls.append((command, options))
            output, code = "", 0
            if command[:2] == ["dpkg-deb", "--field"]:
                output = "qcs" if command[-1] == "Package" else "0.1.0"
            elif command[:2] == ["dpkg-query", "--show"]:
                query_count += 1
                output, code = ("", 1) if query_count == 1 else ("ii ", 0)
            elif command[:2] == ["dpkg-query", "--listfiles"]:
                output = "/usr/bin/qcs-gui\n"
            elif command[0] == "apt-get" and apt_error:
                raise RuntimeError("synthetic dependency failure")
            elif command[0] == "runuser":
                profile = work / "user-profile"
                profile.mkdir()
                (profile / "user-data").write_text("retain me", encoding="utf-8")
                (self.report / "smoke").mkdir()
                (self.report / "smoke/report.json").write_text(json.dumps({"passed": complete,
                    "verificationScope": "full", "fullVerificationPassed": complete,
                    "modelSmoke": {"passed": complete},
                    "commit": HEAD, "buildIdentity": {"candidateCommit": HEAD},
                    "packageSha256": deb.digest(self.package), **(smoke_identity or {})}), encoding="utf-8")
            elif command[:2] == ["dpkg", "--remove"]:
                self.payload.unlink(missing_ok=True)
                if not preserve and (work / "user-profile/user-data").exists():
                    (work / "user-profile/user-data").unlink()
            elif command[0] == "userdel" and cleanup_error:
                code = 1
            return subprocess.CompletedProcess(command, code, output, "")

        groups = mock.Mock()
        groups.getgrgid.side_effect = KeyError("unused group")
        groups.getgrnam.side_effect = KeyError("userdel already removed group")
        with self.guarded(environment), mock.patch.dict(sys.modules, {"grp": groups}), \
                mock.patch.object(deb, "run", side_effect=fake_run), mock.patch("builtins.print"):
            try:
                deb.install_and_verify(self.package, self.report, work, self.source, self.models,
                    20000, 20000, image=CUDA["container"], variant=CUDA["id"],
                    isolated_home=True, user="qcs-deb-fixture")
                error = None
            except RuntimeError as caught:
                error = str(caught)
        return json.loads((self.report / "report.json").read_text(encoding="utf-8")), calls, error, work

    def test_direct_install_runs_nonroot_smoke_removes_payload_and_preserves_profile(self):
        report, calls, error, work = self.exercise_install()
        self.assertIsNone(error)
        self.assertTrue(report["passed"])
        self.assertTrue(report["fullVerificationPassed"])
        self.assertEqual(report["commit"], HEAD)
        self.assertEqual(report["packageSha256"], deb.digest(self.package))
        self.assertFalse(self.payload.exists())
        self.assertEqual((work / "user-profile/user-data").read_text(encoding="utf-8"), "retain me")
        self.assertEqual(report["retainedProfileFiles"], 2)
        command = next(command for command, _ in calls if command[0] == "runuser")
        self.assertEqual(command[1:4], ["--user", "qcs-deb-fixture", "--"])
        self.assertEqual(command[command.index("--model-dir") + 1], str(self.models))
        self.assertEqual(command[command.index("--installed-root") + 1], "/usr")
        self.assertEqual(command[command.index("--package-file") + 1], str(self.package))
        self.assertEqual(command[command.index("--variant") + 1], CUDA["id"])
        useradd = next(command for command, _ in calls if command[0] == "useradd")
        self.assertEqual(useradd[useradd.index("--home-dir") + 1], str(work / "home"))
        self.assertTrue(any(command[0] == "userdel" for command, _ in calls))
        self.assertFalse(any(command[0] in ("docker", "groupdel") for command, _ in calls))

    def test_verified_smoke_binds_candidate_without_environment_declaration(self):
        report, _, error, _ = self.exercise_install(environment={"QCOPILOTS_SEARCH_CANDIDATE_HEAD": ""})
        self.assertIsNone(error)
        self.assertEqual(report["commit"], HEAD)
        self.assertTrue(report["passed"])

    def test_matching_candidate_aliases_bind_the_same_verified_commit(self):
        report, _, error, _ = self.exercise_install(environment={
            "QCOPILOTS_SEARCH_CANDIDATE_COMMIT": HEAD})
        self.assertIsNone(error)
        self.assertEqual(report["commit"], HEAD)

    def test_conflicting_candidate_alias_cannot_pass_and_removes_installation(self):
        report, calls, error, _ = self.exercise_install(environment={"QCOPILOTS_SEARCH_CANDIDATE_COMMIT": "b" * 40})
        self.assertFalse(report["passed"])
        self.assertIn("QCOPILOTS_SEARCH_CANDIDATE_COMMIT differs", error)
        self.assertTrue(any(command[:2] == ["dpkg", "--remove"] for command, _ in calls))

    def test_head_declaration_must_match_verified_smoke_commit(self):
        report, _, error, _ = self.exercise_install(smoke_identity={
            "commit": "b" * 40, "buildIdentity": {"candidateCommit": "b" * 40}})
        self.assertFalse(report["passed"])
        self.assertIn("QCOPILOTS_SEARCH_CANDIDATE_HEAD differs", error)

    def test_ci_smoke_requires_matching_verified_commit(self):
        report, _, error, _ = self.exercise_install(smoke_identity={
            "commit": None, "buildIdentity": {"candidateCommit": None}})
        self.assertFalse(report["passed"])
        self.assertIn("QCOPILOTS_SEARCH_CANDIDATE_HEAD differs", error)

    def test_local_dirty_build_does_not_require_a_release_identity(self):
        report, _, error, _ = self.exercise_install(environment={"QCOPILOTS_SEARCH_CANDIDATE_HEAD": ""},
            smoke_identity={"commit": None, "buildIdentity": {
                "candidateCommit": None, "workingTreeModified": True}})
        self.assertIsNone(error)
        self.assertTrue(report["fullVerificationPassed"])
        self.assertIsNone(report["commit"])

    def test_invalid_commit_is_rejected_even_for_local_verification(self):
        report, _, error, _ = self.exercise_install(environment={"QCOPILOTS_SEARCH_CANDIDATE_HEAD": ""},
            smoke_identity={"commit": "not-a-commit"})
        self.assertFalse(report["passed"])
        self.assertIn("invalid source commit", error)

    def test_embedded_candidate_must_match_verified_smoke_commit(self):
        report, _, error, _ = self.exercise_install(smoke_identity={"buildIdentity": {"candidateCommit": "b" * 40}})
        self.assertFalse(report["passed"])
        self.assertIn("embedded commit differs", error)

    def test_smoke_must_bind_the_original_deb_bytes(self):
        report, _, error, _ = self.exercise_install(smoke_identity={"packageSha256": "0" * 64})
        self.assertFalse(report["passed"])
        self.assertIn("digest differs", error)

    def test_dependency_failure_removes_partial_installation(self):
        report, calls, error, _ = self.exercise_install(apt_error=True)
        self.assertFalse(report["passed"])
        self.assertIn("dependency failure", error)
        self.assertTrue(any(command[:2] == ["dpkg", "--remove"] for command, _ in calls))
        self.assertFalse(any(command[0] == "runuser" for command, _ in calls))

    def test_incomplete_smoke_cannot_pass(self):
        report, calls, error, _ = self.exercise_install(complete=False)
        self.assertFalse(report["passed"])
        self.assertIn("complete package and real-model smoke", error)
        self.assertTrue(any(command[0] == "userdel" for command, _ in calls))

    def test_keyword_only_smoke_cannot_pass_installation_verification(self):
        report, _, error, _ = self.exercise_install(smoke_identity={"verificationScope": "keyword-only"})
        self.assertFalse(report["fullVerificationPassed"])
        self.assertIn("complete package and real-model smoke", error)

    def test_missing_scope_cannot_pass_installation_verification(self):
        report, _, error, _ = self.exercise_install(smoke_identity={"verificationScope": None})
        self.assertFalse(report["fullVerificationPassed"])
        self.assertIn("complete package and real-model smoke", error)

    def test_uninstall_profile_loss_cannot_pass(self):
        report, _, error, _ = self.exercise_install(preserve=False)
        self.assertFalse(report["passed"])
        self.assertIn("changed the isolated user profile", error)

    def test_account_cleanup_failure_cannot_pass(self):
        report, _, error, _ = self.exercise_install(cleanup_error=True)
        self.assertFalse(report["passed"])
        self.assertFalse(report["fullVerificationPassed"])
        self.assertIn("isolated container test account", error)


if __name__ == "__main__":
    unittest.main()
