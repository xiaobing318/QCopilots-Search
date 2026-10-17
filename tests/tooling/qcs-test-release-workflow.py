#!/usr/bin/env python3
"""Manual release orchestration contracts. All remote responses and release writes are mocked."""
from __future__ import annotations

from copy import deepcopy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("qcs_release_workflow_tested", ROOT / ".github/scripts/qcs-release-ci.py")
workflow = importlib.util.module_from_spec(spec)
spec.loader.exec_module(workflow)
CANDIDATE = "1" * 40


def environment(**changes):
    result = {"GITHUB_ACTIONS": "true", "GITHUB_EVENT_NAME": "workflow_dispatch",
              "GITHUB_REPOSITORY": workflow.REPOSITORY, "GITHUB_REF": "refs/heads/main",
              "GITHUB_SHA": CANDIDATE, "GITHUB_RUN_ID": "800", "GITHUB_RUN_ATTEMPT": "1",
              "QCS_RELEASE_ACTION": "draft", "QCS_RELEASE_CANDIDATE": CANDIDATE,
              "QCS_RELEASE_CI_RUN": "700", "QCS_RELEASE_CI_ATTEMPT": "2",
              "QCS_RELEASE_TAG": "v0.3.0", "QCS_RELEASE_FREEZE_RUN": "",
              "RUNNER_ENVIRONMENT": "github-hosted", "RUNNER_OS": "Linux", "ImageOS": "ubuntu24"}
    result.update(changes)
    return result


def request(**changes):
    return workflow.inputs(environment(**changes))


def positive_id(value):
    if type(value) is not int or value <= 0:
        raise ValueError("invalid id")
    return value


class FakeAPI:
    def __init__(self):
        self.calls = []
        self.repository = {"id": 42, "full_name": workflow.REPOSITORY, "archived": False}
        self.workflow = {"id": 90, "path": workflow.WORKFLOW_PATH}
        self.run = {"id": 800, "workflow_id": 90, "event": "workflow_dispatch", "head_sha": CANDIDATE,
                    "head_branch": "main", "path": workflow.WORKFLOW_PATH, "repository": self.repository,
                    "status": "in_progress", "conclusion": None}
        self.artifacts = []

    def request(self, method, endpoint, data=None):
        self.calls.append((method, endpoint, data))
        if method != "GET":
            raise AssertionError("Preflight must never mutate remote state")
        prefix = f"/repos/{workflow.REPOSITORY}"
        if endpoint == prefix:
            return deepcopy(self.repository)
        if endpoint == prefix + f"/actions/runs/{self.run['id']}":
            return deepcopy(self.run)
        if endpoint == prefix + "/actions/workflows/90":
            return deepcopy(self.workflow)
        raise AssertionError(endpoint)

    def add_freeze(self):
        self.artifacts.append({"id": 901, "name": workflow.freeze_name(request()), "expired": False,
                               "size_in_bytes": 1024, "digest": "sha256:" + "a" * 64,
                               "workflow_run": {"id": self.run["id"], "head_sha": CANDIDATE}})
        return self.artifacts[-1]


class FakeRelease:
    USER_PACKAGE_COUNT = 21
    MANIFEST = "qcs-release-manifest.json"
    positive_id = staticmethod(positive_id)

    def __init__(self, api):
        self.api = api
        self.validate_remote_main = mock.Mock()
        self.validate_tag = mock.Mock()
        self.identity_for = mock.Mock()
        self.digest = mock.Mock(return_value="f" * 64)
        self.verify_freeze = mock.Mock(return_value={"assets": [{"role": "sources", "bytes": 1000}]})
        selected = {"fixture": {"artifacts": [{"artifactSize": 400} for _ in range(21)]}}
        self.collect_ci = mock.Mock(return_value={"selection": {"selected": selected}})

    @staticmethod
    def project_version(source):
        return "0.3.0"

    def collect_pages(self, api, run_id, kind):
        assert api is self.api and kind == "artifacts"
        return deepcopy(self.api.artifacts)


class InputTests(unittest.TestCase):
    def test_valid_draft(self):
        result = request()
        self.assertEqual(result["native_attempt"], 2)
        self.assertIsNone(result["freeze_run_id"])
        self.assertEqual(workflow.freeze_name(result), f"qcs-release-freeze-{CANDIDATE}-run700-a2")

    def test_publish_requires_original_run(self):
        with self.assertRaisesRegex(ValueError, "original"):
            request(QCS_RELEASE_ACTION="publish")
        self.assertEqual(request(QCS_RELEASE_ACTION="publish", QCS_RELEASE_FREEZE_RUN="800")["freeze_run_id"], 800)

    def test_authorized_event_repository_and_branch_required(self):
        for key, value in (("GITHUB_ACTIONS", "false"), ("GITHUB_EVENT_NAME", "push"),
                           ("GITHUB_REPOSITORY", "fixture/fork"), ("GITHUB_REF", "refs/heads/feature"),
                           ("GITHUB_REF", "refs/tags/v0.3.0")):
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                request(**{key: value})

    def test_candidate_must_be_exact_workflow_sha(self):
        for candidate in ("1" * 39, "A" * 40, "2" * 40, CANDIDATE + "\n", "$(touch /tmp/x)"):
            with self.subTest(candidate=candidate), self.assertRaises(ValueError):
                request(QCS_RELEASE_CANDIDATE=candidate)

    def test_action_and_tag_injection_rejected(self):
        for field, value in (("QCS_RELEASE_ACTION", "publish; true"), ("QCS_RELEASE_TAG", "v0.3.0\nfoo"),
                             ("QCS_RELEASE_TAG", "--help"), ("QCS_RELEASE_TAG", "$(id)")):
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                request(**{field: value})

    def test_ids_and_attempts_are_strict_positive_decimals(self):
        for field in ("QCS_RELEASE_CI_RUN", "QCS_RELEASE_CI_ATTEMPT", "GITHUB_RUN_ID",
                      "GITHUB_RUN_ATTEMPT", "QCS_RELEASE_FREEZE_RUN"):
            for value in ("0", "01", "-1", " 1", "1\n", "1; false", "9223372036854775808"):
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    request(**{field: value})


class RemoteReadTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeAPI()
        self.release = FakeRelease(self.api)

    def test_preflight_is_read_only_and_checks_exact_ci(self):
        with mock.patch.object(workflow, "validate_checkout"), mock.patch("builtins.print"):
            report = workflow.preflight(request(), self.api, self.release)
        self.assertIsNone(report["freeze"])
        self.assertEqual(report["package_count"], 21)
        self.assertEqual(report["package_bytes"], 8400)
        self.assertTrue(all(method == "GET" for method, _, _ in self.api.calls))
        self.release.collect_ci.assert_called_once_with(self.api, CANDIDATE, "700", 2)
        self.release.validate_remote_main.assert_called_once_with(self.api, CANDIDATE)

    def test_incorrect_project_tag_rejected_before_remote_calls(self):
        with mock.patch.object(workflow, "validate_checkout"), self.assertRaisesRegex(ValueError, "project version"):
            workflow.preflight(request(QCS_RELEASE_TAG="v0.9.0"), self.api, self.release)
        self.assertEqual(self.api.calls, [])

    def test_failed_native_ci_blocks_before_artifact_or_release_write(self):
        self.release.collect_ci.side_effect = ValueError("native attempt failed")
        with mock.patch.object(workflow, "validate_checkout"), self.assertRaisesRegex(ValueError, "native attempt"):
            workflow.preflight(request(), self.api, self.release)
        self.release.validate_tag.assert_not_called()

    def test_missing_native_package_rejected(self):
        self.release.collect_ci.return_value["selection"]["selected"]["fixture"]["artifacts"].pop()
        with mock.patch.object(workflow, "validate_checkout"), self.assertRaisesRegex(ValueError, "21"):
            workflow.preflight(request(), self.api, self.release)

    def test_bool_native_size_rejected(self):
        self.release.collect_ci.return_value["selection"]["selected"]["fixture"]["artifacts"][0]["artifactSize"] = True
        with mock.patch.object(workflow, "validate_checkout"), self.assertRaisesRegex(ValueError, "positive integer"):
            workflow.preflight(request(), self.api, self.release)

    def test_same_run_retry_reuses_existing_artifact(self):
        self.api.add_freeze()
        result = workflow.find_freeze(self.api, self.release, request(GITHUB_RUN_ATTEMPT="2"))
        self.assertEqual(result["artifact_id"], 901)
        self.assertEqual(result["run_id"], 800)

    def test_failed_or_cancelled_original_draft_can_be_restored(self):
        self.api.run["status"] = "completed"
        for conclusion in ("failure", "cancelled"):
            self.api.run["conclusion"] = conclusion
            self.api.artifacts = []
            self.api.add_freeze()
            result = workflow.find_freeze(self.api, self.release, request(QCS_RELEASE_FREEZE_RUN="800"))
            self.assertEqual(result["run_id"], 800)

    def test_explicit_missing_freeze_never_regenerates(self):
        with self.assertRaisesRegex(ValueError, "cannot be regenerated"):
            workflow.find_freeze(self.api, self.release, request(QCS_RELEASE_FREEZE_RUN="800"))

    def test_duplicate_freeze_rejected(self):
        row = self.api.add_freeze()
        self.api.artifacts.append(deepcopy(row))
        with self.assertRaisesRegex(ValueError, "duplicate"):
            workflow.find_freeze(self.api, self.release, request())

    def test_other_freeze_in_same_run_prevents_regeneration(self):
        self.api.add_freeze()["name"] = "qcs-release-freeze-another-candidate"
        with self.assertRaisesRegex(ValueError, "different freeze"):
            workflow.find_freeze(self.api, self.release, request())

    def test_invalid_freeze_metadata_rejected(self):
        changes = [("expired", True), ("expired", None), ("id", 0), ("size_in_bytes", 0),
                   ("size_in_bytes", True), ("digest", "sha256:abc"),
                   ("workflow_run", {"id": 801, "head_sha": CANDIDATE}),
                   ("workflow_run", {"id": 800, "head_sha": "2" * 40})]
        for key, value in changes:
            with self.subTest(key=key, value=value):
                self.api.artifacts = []
                self.api.add_freeze()[key] = value
                with self.assertRaises(ValueError):
                    workflow.find_freeze(self.api, self.release, request())

    def test_wrong_workflow_repository_candidate_or_event_rejected(self):
        changes = [("event", "push"), ("head_sha", "2" * 40), ("head_branch", "other"),
                   ("path", ".github/workflows/other.yml"), ("id", 801),
                   ("repository", {"id": 43, "full_name": workflow.REPOSITORY})]
        for key, value in changes:
            with self.subTest(key=key, value=value):
                original = self.api.run
                self.api.run = {**original, key: value}
                # Wrong ID is returned from the exact requested route in this synthetic case.
                api = mock.Mock(request=lambda method, endpoint: deepcopy(self.api.run) if "/actions/runs/" in endpoint
                                else self.api.request(method, endpoint))
                with self.assertRaises(ValueError):
                    workflow.validate_workflow_run(api, self.release, 800, CANDIDATE)
                self.api.run = original

    def test_wrong_workflow_id_metadata_rejected(self):
        self.api.workflow["id"] = 91
        with self.assertRaises(ValueError):
            workflow.validate_workflow_run(self.api, self.release, 800, CANDIDATE)


class FilesystemAndExecutionTests(unittest.TestCase):
    def setUp(self):
        base = os.environ.get("QCOPILOTS_SEARCH_TEST_ROOT")
        if base:
            Path(base).mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix="qcs-release-workflow-", dir=base)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.api = FakeAPI()
        self.release = FakeRelease(self.api)

    def test_checkout_requires_exact_clean_candidate(self):
        for answers in (("2" * 40,), (CANDIDATE, " M tracked.txt"), (CANDIDATE, "?? local.txt")):
            with mock.patch.object(workflow, "git", side_effect=answers), self.assertRaises(ValueError):
                workflow.validate_checkout(request())

    def test_external_task_directory(self):
        path = workflow.task_root(request(), environment(RUNNER_TEMP=str(self.root)))
        self.assertEqual(path, self.root / "qcs-release-800-1")
        self.assertFalse(path.exists())

    def test_task_directory_cannot_overlap_checkout(self):
        for path in (ROOT, ROOT.parent, ROOT / ".cache", Path("relative")):
            with self.subTest(path=path), self.assertRaises(ValueError):
                workflow.task_root(request(), environment(RUNNER_TEMP=str(path)))

    def test_disk_sufficient_does_not_remove_anything(self):
        usage = types.SimpleNamespace(free=20 * workflow.GIB)
        with mock.patch.object(workflow.shutil, "disk_usage", return_value=usage), \
                mock.patch.object(workflow.subprocess, "run") as command, mock.patch("builtins.print"):
            workflow.ensure_disk_space(self.root, 8 * workflow.GIB, environment())
        command.assert_not_called()

    def test_disk_cleanup_denied_on_other_hosts(self):
        for change in ({"RUNNER_ENVIRONMENT": "self-hosted"}, {"ImageOS": "ubuntu22"},
                       {"GITHUB_ACTIONS": "false"}, {"RUNNER_OS": "Windows"}):
            with self.subTest(change=change), mock.patch.object(workflow.platform, "system", return_value="Linux"), \
                    mock.patch.object(workflow.shutil, "disk_usage", return_value=types.SimpleNamespace(free=1)), \
                    mock.patch.object(workflow.subprocess, "run") as command, mock.patch("builtins.print"):
                with self.assertRaisesRegex(ValueError, "GitHub-hosted Ubuntu"):
                    workflow.ensure_disk_space(self.root, 8 * workflow.GIB, environment(**change))
                command.assert_not_called()

    def test_disk_budget_uses_real_package_total(self):
        usage = types.SimpleNamespace(free=18 * workflow.GIB)
        with mock.patch.object(workflow.shutil, "disk_usage", return_value=usage), \
                mock.patch.object(workflow.platform, "system", return_value="Windows"), mock.patch("builtins.print"):
            with self.assertRaisesRegex(ValueError, "Insufficient"):
                workflow.ensure_disk_space(self.root, 17 * workflow.GIB, environment())

    def test_cleanup_is_bounded_and_stops_at_target(self):
        unused_sdk = self.root / "unused-sdk"
        unused_sdk.mkdir()
        usage = [types.SimpleNamespace(free=1), types.SimpleNamespace(free=17 * workflow.GIB)]
        with mock.patch.object(workflow.platform, "system", return_value="Linux"), \
                mock.patch.object(workflow.shutil, "disk_usage", side_effect=usage), \
                mock.patch.object(workflow, "cleanup_target", return_value=unused_sdk) as target, \
                mock.patch.object(workflow.subprocess, "run") as command, mock.patch("builtins.print"):
            workflow.ensure_disk_space(self.root, 8 * workflow.GIB, environment())
        target.assert_called_once_with("/usr/local/lib/android")
        command.assert_called_once_with(["sudo", "--non-interactive", "rm", "-rf", "--", str(unused_sdk)], check=True)

    def test_cleanup_cannot_expand_allowlist(self):
        for path in ("/", "/home/runner", "/opt/hostedtoolcache", "/usr/share/dotnet/..", "/usr/local/lib/android/"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                workflow.cleanup_target(path)

    def test_cleanup_rejects_symlink(self):
        with mock.patch.object(workflow.Path, "is_symlink", return_value=True), self.assertRaisesRegex(ValueError, "symbolic"):
            workflow.cleanup_target("/usr/share/dotnet")

    def test_insufficient_space_prevents_prepare_or_release_mutation(self):
        with mock.patch.object(workflow, "preflight", return_value={"package_bytes": 1}), \
                mock.patch.object(workflow, "task_root", return_value=self.root / "work"), \
                mock.patch.object(workflow, "ensure_disk_space", side_effect=ValueError("insufficient")), \
                mock.patch.object(workflow, "developer") as developer:
            with self.assertRaisesRegex(ValueError, "insufficient"):
                workflow.stage(request(), self.api, self.release)
            developer.assert_not_called()
        self.assertFalse((self.root / "work").exists())

    def staged(self, freeze=None):
        report = {"request": request(), "freeze": freeze, "package_count": 21,
                  "package_bytes": 8400, "freeze_name": workflow.freeze_name(request())}
        root = self.root / "work"
        with mock.patch.object(workflow, "preflight", return_value=report), \
                mock.patch.object(workflow, "task_root", return_value=root), \
                mock.patch.object(workflow, "ensure_disk_space"), \
                mock.patch.object(workflow.subprocess, "run") as git, \
                mock.patch.object(workflow, "developer") as developer, \
                mock.patch.object(workflow, "output_values") as outputs, mock.patch("builtins.print"):
            state = workflow.stage(request(), self.api, self.release)
        return root, state, git, developer, outputs

    def test_new_draft_prepares_then_exposes_exact_original_files(self):
        root, state, git, developer, outputs = self.staged()
        self.assertEqual(developer.call_args.args[0], "release-prepare")
        self.assertEqual(developer.call_count, 1)
        self.assertTrue(state["new_freeze"])
        self.assertEqual(state["freeze_run_id"], 800)
        self.assertEqual(state["source_bytes"], 1000)
        self.assertEqual(outputs.call_args.args[0]["new_freeze"], "true")
        self.assertEqual(git.call_args.args[0], ["git", "-C", str(ROOT), "checkout", "-B", "main", CANDIDATE])
        self.assertEqual(json.loads((root / "state.json").read_text())["manifest_sha256"], "f" * 64)

    def test_retry_restores_instead_of_regenerating(self):
        _, state, _, developer, outputs = self.staged({"run_id": 799, "artifact_id": 901})
        self.assertEqual(developer.call_args.args[0:3], ("release-restore", "--freeze-run-id", 799))
        self.assertEqual(developer.call_count, 1)
        self.assertFalse(state["new_freeze"])
        self.assertEqual(state["freeze_run_id"], 799)
        self.assertEqual(outputs.call_args.args[0]["new_freeze"], "false")
        self.assertNotIn("--source-cache-dir", developer.call_args.args)

    def test_finish_requires_uploaded_freeze(self):
        state = {"freeze_dir": str(self.root), "manifest_sha256": "f" * 64}
        with mock.patch.object(workflow, "read_state", return_value=state), \
                mock.patch.object(workflow, "preflight", return_value={"freeze": None}), \
                mock.patch.object(workflow, "developer") as developer:
            with self.assertRaisesRegex(ValueError, "before.*artifact exists"):
                workflow.finish(request(), self.api, self.release)
            developer.assert_not_called()

    def test_finish_uses_original_manifest_for_draft_and_publish(self):
        state = {"freeze_dir": str(self.root), "manifest_sha256": "f" * 64}
        for action, command in (("draft", "release-upload-draft"), ("publish", "release-publish")):
            with self.subTest(action=action), mock.patch.object(workflow, "read_state", return_value=state), \
                    mock.patch.object(workflow, "preflight", return_value={"freeze": {"run_id": 800}}), \
                    mock.patch.object(workflow, "developer") as developer:
                workflow.finish(request(QCS_RELEASE_ACTION=action, QCS_RELEASE_FREEZE_RUN="800"), self.api, self.release)
                developer.assert_called_once_with(command, "--manifest", self.root / "qcs-release-manifest.json", "--tag", "v0.3.0")

    def test_finish_rejects_manifest_changed_after_freeze_upload(self):
        state = {"freeze_dir": str(self.root), "manifest_sha256": "a" * 64}
        with mock.patch.object(workflow, "read_state", return_value=state), \
                mock.patch.object(workflow, "preflight", return_value={"freeze": {"run_id": 800}}), \
                mock.patch.object(workflow, "developer") as developer:
            with self.assertRaisesRegex(ValueError, "changed"):
                workflow.finish(request(), self.api, self.release)
            developer.assert_not_called()

    def test_commands_use_argument_arrays_not_shell(self):
        with mock.patch.object(workflow.subprocess, "run") as run:
            workflow.developer("release-publish", "--manifest", self.root / "space path.json", "--tag", "v0.3.0")
        self.assertIsInstance(run.call_args.args[0], list)
        self.assertNotIn("shell", run.call_args.kwargs)
        self.assertEqual(run.call_args.args[0][-1], "v0.3.0")

    def test_single_line_workflow_outputs(self):
        destination = self.root / "outputs"
        with mock.patch.dict(os.environ, {"GITHUB_OUTPUT": str(destination)}):
            workflow.output_values({"freeze_name": "safe", "new_freeze": "true"})
            with self.assertRaises(ValueError):
                workflow.output_values({"freeze_name": "safe\ninjected=true"})
        self.assertEqual(destination.read_text(), "freeze_name=safe\nnew_freeze=true\n")

    def test_summary_is_offline_and_records_recovery_id(self):
        root, state, _, _, _ = self.staged({"run_id": 799})
        destination = self.root / "summary.md"
        with mock.patch.object(workflow, "task_root", return_value=root), \
                mock.patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": str(destination)}):
            workflow.summary(request())
        text = destination.read_text()
        self.assertIn("freeze_run_id=799", text)
        self.assertIn("Original manifest SHA-256", text)
        self.assertIn("upload step to have succeeded", text)
        self.assertIn("14-day retention", text)


class WorkflowFileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = (ROOT / workflow.WORKFLOW_PATH).read_text(encoding="utf-8")

    def test_only_explicit_manual_trigger(self):
        triggers = self.text.split("on:\n", 1)[1].split("\npermissions:", 1)[0]
        self.assertIn("  workflow_dispatch:", triggers)
        for forbidden in ("  push:", "  pull_request:", "  release:", "  workflow_run:"):
            self.assertNotIn(forbidden, triggers)

    def test_permissions_separated_by_job(self):
        self.assertIn("permissions: {}", self.text)
        readonly, writable = self.text.split("  release:\n", 1)
        self.assertIn("contents: read\n      actions: read", readonly)
        self.assertNotIn("contents: write", readonly)
        self.assertIn("needs: preflight", writable)
        self.assertIn("contents: write\n      actions: read", writable)
        for forbidden in ("actions: write", "packages: write", "id-token: write", "secrets."):
            self.assertNotIn(forbidden, self.text)

    def test_dispatch_inputs_are_environment_values_never_shell_code(self):
        for name in ("action", "candidate_sha", "ci_run_id", "ci_attempt", "tag", "freeze_run_id"):
            self.assertIn("${{ inputs." + name + " }}", self.text)
        for line in self.text.splitlines():
            if line.lstrip().startswith("run:"):
                self.assertNotIn("${{", line)
        self.assertEqual(self.text.count("ref: ${{ github.sha }}"), 2)
        self.assertEqual(self.text.count("persist-credentials: false"), 2)
        self.assertNotIn("ref: ${{ inputs.", self.text)
        self.assertEqual(self.text.count("if: github.repository == 'xiaobing318/QCopilots-Search' && github.ref == 'refs/heads/main'"), 2)

    def test_pinned_actions_and_non_cancelling_concurrency(self):
        for line in self.text.splitlines():
            if "uses:" in line:
                self.assertRegex(line, r"@[0-9a-f]{40}(?:\s|$)")
        self.assertIn("group: qcs-release\n  cancel-in-progress: false", self.text)
        self.assertNotIn("environment:", self.text)

    def test_freeze_upload_before_release_write_has_exact_three_paths(self):
        upload = self.text.split("- name: " + workflow.FREEZE_STEP, 1)[1].split("- name:", 1)[0]
        self.assertIn("id: upload_freeze", upload)
        self.assertIn("overwrite: false", upload)
        self.assertIn("compression-level: 0", upload)
        self.assertIn("archive: true", upload)
        self.assertIn("retention-days: 14", upload)
        self.assertIn("if-no-files-found: error", upload)
        self.assertNotIn("archive: false", upload)
        paths = [line.strip() for line in upload.splitlines() if "${{ steps.stage.outputs.freeze_dir }}" in line]
        self.assertEqual(paths, ["${{ steps.stage.outputs.freeze_dir }}/qcs-release-manifest.json",
                                "${{ steps.stage.outputs.freeze_dir }}/assets/SHA256SUMS",
                                "${{ steps.stage.outputs.freeze_dir }}/assets/qcs-source-materials-${{ steps.stage.outputs.candidate_short }}.zip"])
        self.assertLess(self.text.index(workflow.FREEZE_STEP), self.text.index("qcs-release-ci.py finish"))
        self.assertIn("if: steps.stage.outputs.new_freeze == 'true'", upload)
        self.assertIn("  release:\n    name: " + workflow.FREEZE_JOB, self.text)

    def test_no_native_workflow_dispatch_or_policy_change(self):
        self.assertNotIn("qcs-native.yml", self.text)
        self.assertNotIn("workflow_dispatches", self.text)
        native = (ROOT / ".github/workflows/qcs-native.yml").read_text(encoding="utf-8")
        self.assertIn("fail-fast: false", native)


if __name__ == "__main__":
    unittest.main()
