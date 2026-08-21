"""Unit tests for resolver.py, the github-issue-resolver skill's helper.

Run: python3 -m unittest agents/platform/skills/github-issue-resolver/scripts/test_resolver.py
"""

import argparse
import contextlib
import importlib
import io
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

# Import the module under test from this directory.
sys.path.insert(0, str(Path(__file__).parent.absolute()))
resolver = importlib.import_module("resolver")

def _sequence(values):
    """Consume one entry per call, with the final entry repeating forever."""
    pending = list(values)
    def take():
        return pending.pop(0) if len(pending) > 1 else pending[0]
    return take

GH_AUTH_STDERR = "gh: HTTP 401: Bad credentials (https://api.github.com/graphql)"
GH_NOT_FOUND_STDERR = "gh: Not Found (HTTP 404)"

def _gh_stub(
    auth_rc: int = 0,
    list_rc: int = 0,
    list_stdout: str = "[]",
    record=None,
    repo_responses=None,
    auth_rcs=None,
    write_rcs=None,
    write_stderr: str = "",
    list_stderr: str = "",
):
    next_auth = _sequence(auth_rcs if auth_rcs else [auth_rc])
    next_write = _sequence(write_rcs if write_rcs else [0])

    def run(argv, **kwargs):
        if argv and argv[0] == "kubectl":
            cm_json = json.dumps({"data": {"managed_repos": "acme/toolkit, acme/repo2"}})
            return subprocess.CompletedProcess(argv, 0, cm_json, "")
        if record is not None:
            record.append(argv)
        sub = argv[1:]
        if sub[:2] == ["auth", "status"]:
            return subprocess.CompletedProcess(argv, next_auth(), "", "")
        if sub[:2] == ["issue", "list"]:
            if repo_responses is not None and "-R" in argv:
                repo_idx = argv.index("-R") + 1
                repo_name = argv[repo_idx]
                if repo_name in repo_responses:
                    resp = repo_responses[repo_name]
                    return subprocess.CompletedProcess(
                        argv,
                        resp.get("rc", 0),
                        resp.get("stdout", "[]"),
                        resp.get("stderr", ""),
                    )
            return subprocess.CompletedProcess(argv, list_rc, list_stdout, list_stderr)
        return subprocess.CompletedProcess(argv, next_write(), "[]", write_stderr)

    return run


@contextlib.contextmanager
def _fresh_refresh_state():
    with mock.patch.object(resolver, "_refresh_attempted", False):
        with mock.patch.object(resolver, "_refresh_failed", False):
            yield


class GetManagedReposTest(unittest.TestCase):
    def test_extracts_managed_repos_list(self):
        cm_json = json.dumps({"data": {"managed_repos": "gke-labs/kube-agents, acme/toolkit"}})
        with mock.patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, cm_json, "")):
            self.assertEqual(resolver.get_managed_repos(), ["gke-labs/kube-agents", "acme/toolkit"])

    def test_empty_when_no_managed_repos(self):
        cm_json = json.dumps({"data": {"managed_repos": ""}})
        with mock.patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, cm_json, "")):
            self.assertEqual(resolver.get_managed_repos(), [])

    def test_raises_when_kubectl_fails(self):
        with mock.patch("subprocess.run", side_effect=subprocess.CalledProcessError(1, ["kubectl"], stderr="Forbidden")):
            with self.assertRaises(RuntimeError) as ctx:
                resolver.get_managed_repos()
            self.assertIn("Failed to read ConfigMap", str(ctx.exception))
            self.assertIn("Forbidden", str(ctx.exception))

    def test_raises_when_kubectl_not_found(self):
        with mock.patch("subprocess.run", side_effect=FileNotFoundError("kubectl")):
            with self.assertRaises(RuntimeError) as ctx:
                resolver.get_managed_repos()
            self.assertIn("kubectl binary not found", str(ctx.exception))

    def test_raises_when_json_invalid(self):
        with mock.patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, "not-json", "")):
            with self.assertRaises(RuntimeError) as ctx:
                resolver.get_managed_repos()
            self.assertIn("Failed to parse ConfigMap", str(ctx.exception))


class HandlePollRoutingTest(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.d = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def _poll(self, repos, refresh=None, **stub):
        self.refresh_calls = []

        def _refresh(repo):
            self.refresh_calls.append(repo)
            if refresh is not None:
                refresh(repo)

        buf, err = io.StringIO(), io.StringIO()
        with contextlib.ExitStack() as stack:
            stack.enter_context(contextlib.redirect_stdout(buf))
            stack.enter_context(contextlib.redirect_stderr(err))
            stack.enter_context(mock.patch.object(resolver, "get_managed_repos", return_value=repos))
            stack.enter_context(mock.patch.object(subprocess, "run", _gh_stub(**stub)))
            stack.enter_context(mock.patch.object(resolver, "refresh_credentials", _refresh))
            stack.enter_context(_fresh_refresh_state())
            resolver.handle_poll(argparse.Namespace())
        self.stderr = err.getvalue()
        return json.loads(buf.getvalue())

    def test_configmap_read_failure_is_a_loud_error(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            with mock.patch.object(resolver, "get_managed_repos", side_effect=RuntimeError("kubectl failed: Forbidden")):
                resolver.handle_poll(argparse.Namespace())
        payload = json.loads(buf.getvalue())
        self.assertEqual(payload["status"], "ERROR")
        self.assertEqual(payload["reason"], "CONFIGMAP_READ_FAILED")
        self.assertIn("Forbidden", payload["error"])

    def test_not_configured_is_its_own_status(self):
        self.assertEqual(self._poll([])["status"], "NOT_CONFIGURED")

    def test_broken_auth_is_a_loud_error(self):
        payload = self._poll(["acme/toolkit"], auth_rc=1)
        self.assertEqual(payload["status"], "ERROR")
        self.assertEqual(payload["reason"], "GITHUB_AUTH_NOT_CONFIGURED")
        self.assertEqual(self.refresh_calls, ["acme/toolkit"])

    def test_expired_token_is_refreshed_and_the_poll_continues(self):
        payload = self._poll(["acme/toolkit"], auth_rcs=[1, 0])
        self.assertEqual(payload["status"], "NO_ISSUES")
        self.assertEqual(self.refresh_calls, ["acme/toolkit"])

    def test_refresh_failure_is_not_reported_as_missing_config(self):
        def _boom(repo):
            raise RuntimeError("Credential sidecar failed to refresh GitHub auth")
        payload = self._poll(["acme/toolkit"], auth_rc=1, refresh=_boom)
        self.assertEqual(payload["status"], "ERROR")
        self.assertEqual(payload["reason"], "GITHUB_TOKEN_REFRESH_FAILED")

    def test_refresh_detail_goes_to_stderr_and_not_the_payload(self):
        def _boom(repo):
            raise RuntimeError("minty said 403 for tenant-secret-detail")
        payload = self._poll(["acme/toolkit"], auth_rc=1, refresh=_boom)
        self.assertNotIn("tenant-secret-detail", json.dumps(payload))
        self.assertEqual(set(payload), {"status", "reason"})
        self.assertIn("tenant-secret-detail", self.stderr)
        self.assertIn("RuntimeError", self.stderr)

    def test_healthy_auth_does_not_refresh_pre_emptively(self):
        self._poll(["acme/toolkit"])
        self.assertEqual(self.refresh_calls, [])

    def test_unreachable_repo_is_a_loud_error(self):
        payload = self._poll(["acme/toolkit"], list_rc=1, list_stderr=GH_NOT_FOUND_STDERR)
        self.assertEqual(payload["status"], "ERROR")
        self.assertEqual(payload["reason"], "REPO_UNREACHABLE")
        self.assertEqual(payload["unreachable_repos"], ["acme/toolkit"])
        self.assertEqual(self.refresh_calls, [])

    def test_healthy_and_quiet_is_no_issues(self):
        payload = self._poll(["acme/toolkit"])
        self.assertEqual(payload["status"], "NO_ISSUES")
        self.assertEqual(payload["managed_repos"], ["acme/toolkit"])
        self.assertEqual(payload["unreachable_repos"], [])

    def test_healthy_with_work_is_found(self):
        payload = self._poll(
            ["acme/toolkit"],
            list_stdout=json.dumps([
                {"number": 9, "title": "second", "body": "b", "comments": []},
                {"number": 7, "title": "first", "body": "b", "comments": [{"author": {"login": "alice"}, "body": "hi", "createdAt": "2026-07-30T00:00:00Z"}]},
            ]),
        )
        self.assertEqual(payload["status"], "FOUND")
        self.assertEqual(payload["issue_number"], 7)
        self.assertEqual(payload["repository"], "acme/toolkit")
        self.assertEqual(payload["comments"][0]["author"], "alice")
        self.assertEqual(payload["unreachable_repos"], [])

    def test_multi_repo_one_unreachable_one_healthy_with_work(self):
        payload = self._poll(
            ["broken/repo", "healthy/repo"],
            repo_responses={
                "broken/repo": {"rc": 1},
                "healthy/repo": {"rc": 0, "stdout": json.dumps([{"number": 12, "title": "work item", "body": "details", "comments": []}])},
            },
        )
        self.assertEqual(payload["status"], "FOUND")
        self.assertEqual(payload["issue_number"], 12)
        self.assertEqual(payload["repository"], "healthy/repo")
        self.assertEqual(payload["unreachable_repos"], ["broken/repo"])

    def test_multi_repo_picks_oldest_issue_chronologically(self):
        payload = self._poll(
            ["repo-new/young", "repo-old/mature"],
            repo_responses={
                "repo-new/young": {"rc": 0, "stdout": json.dumps([{"number": 2, "title": "recent issue", "createdAt": "2026-08-10T12:00:00Z", "comments": []}])},
                "repo-old/mature": {"rc": 0, "stdout": json.dumps([{"number": 1500, "title": "older issue", "createdAt": "2026-08-01T10:00:00Z", "comments": []}])},
            },
        )
        self.assertEqual(payload["status"], "FOUND")
        self.assertEqual(payload["issue_number"], 1500)
        self.assertEqual(payload["repository"], "repo-old/mature")

    def test_multi_repo_one_unreachable_one_healthy_no_work(self):
        payload = self._poll(
            ["broken/repo", "healthy/repo"],
            repo_responses={"broken/repo": {"rc": 1}, "healthy/repo": {"rc": 0, "stdout": "[]"}},
        )
        self.assertEqual(payload["status"], "NO_ISSUES")
        self.assertEqual(payload["managed_repos"], ["broken/repo", "healthy/repo"])
        self.assertEqual(payload["unreachable_repos"], ["broken/repo"])

    def test_multi_repo_all_unreachable_is_error(self):
        payload = self._poll(["broken/repo1", "broken/repo2"], list_rc=1)
        self.assertEqual(payload["status"], "ERROR")
        self.assertEqual(payload["reason"], "REPO_UNREACHABLE")
        self.assertEqual(payload["unreachable_repos"], ["broken/repo1", "broken/repo2"])


class ValidateRepoOrExitTest(unittest.TestCase):
    def test_valid_repo_in_managed_passes(self):
        with mock.patch.object(resolver, "get_managed_repos", return_value=["acme/toolkit"]):
            resolver._validate_repo_or_exit("acme/toolkit")

    def test_invalid_format_exits(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            with self.assertRaises(SystemExit) as ctx:
                resolver._validate_repo_or_exit("invalid-repo")
        self.assertEqual(ctx.exception.code, 1)
        payload = json.loads(buf.getvalue())
        self.assertEqual(payload["status"], "ERROR")
        self.assertEqual(payload["reason"], "INVALID_REPOSITORY")

    def test_configmap_read_failed_exits(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            with mock.patch.object(resolver, "get_managed_repos", side_effect=RuntimeError("kubectl failed: Forbidden")):
                with self.assertRaises(SystemExit) as ctx:
                    resolver._validate_repo_or_exit("acme/toolkit")
        self.assertEqual(ctx.exception.code, 1)
        payload = json.loads(buf.getvalue())
        self.assertEqual(payload["status"], "ERROR")
        self.assertEqual(payload["reason"], "CONFIGMAP_READ_FAILED")
        self.assertIn("Forbidden", payload["error"])

    def test_unmanaged_repo_exits(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            with mock.patch.object(resolver, "get_managed_repos", return_value=["acme/toolkit"]):
                with self.assertRaises(SystemExit) as ctx:
                    resolver._validate_repo_or_exit("other-org/other-repo")
        self.assertEqual(ctx.exception.code, 1)
        payload = json.loads(buf.getvalue())
        self.assertEqual(payload["status"], "ERROR")
        self.assertEqual(payload["reason"], "UNMANAGED_REPOSITORY")


class HandleClaimTest(unittest.TestCase):
    def test_claim_adds_label_and_comment(self):
        calls = []
        args = argparse.Namespace(issue=42, repo="acme/toolkit")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            with mock.patch.object(subprocess, "run", _gh_stub(record=calls)):
                with mock.patch.object(resolver, "get_managed_repos", return_value=["acme/toolkit"]):
                    resolver.handle_claim(args)
        payload = json.loads(buf.getvalue())
        self.assertEqual(payload["status"], "CLAIMED")
        self.assertEqual(payload["issue_number"], 42)
        self.assertEqual(payload["repository"], "acme/toolkit")

    def test_claim_refused_when_configmap_read_fails(self):
        args = argparse.Namespace(issue=42, repo="acme/toolkit")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            with mock.patch.object(resolver, "get_managed_repos", side_effect=RuntimeError("kubectl failed: Forbidden")):
                with self.assertRaises(SystemExit) as ctx:
                    resolver.handle_claim(args)
        self.assertEqual(ctx.exception.code, 1)
        payload = json.loads(buf.getvalue())
        self.assertEqual(payload["status"], "ERROR")
        self.assertEqual(payload["reason"], "CONFIGMAP_READ_FAILED")


class ReportFilePathGuardTest(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.d = self._tmp.name
        self._scratch = resolver.SCRATCH_DIR
        self.scratch = os.path.join(self.d, "scratch")
        os.makedirs(self.scratch)
        self.sibling = os.path.join(self.d, "scratch-evil")
        os.makedirs(self.sibling)
        self.secret = os.path.join(self.d, "secret.md")
        with open(self.secret, "w", encoding="utf-8") as handle:
            handle.write("private")
        resolver.SCRATCH_DIR = self.scratch

    def tearDown(self):
        resolver.SCRATCH_DIR = self._scratch
        self._tmp.cleanup()

    def _transition(self, report_file, mock_repos=["acme/toolkit"], **stub):
        calls = []
        self.refresh_calls = []
        args = argparse.Namespace(issue=1, repo="acme/toolkit", state="resolved", report_file=report_file)
        buf, err = io.StringIO(), io.StringIO()
        code = None
        with contextlib.ExitStack() as stack:
            stack.enter_context(contextlib.redirect_stdout(buf))
            stack.enter_context(contextlib.redirect_stderr(err))
            stack.enter_context(mock.patch.object(subprocess, "run", _gh_stub(record=calls, **stub)))
            stack.enter_context(mock.patch.object(resolver, "refresh_credentials", lambda repo: self.refresh_calls.append(repo)))
            if mock_repos is not None:
                stack.enter_context(mock.patch.object(resolver, "get_managed_repos", return_value=mock_repos))
            stack.enter_context(_fresh_refresh_state())
            try:
                resolver.handle_transition(args)
            except SystemExit as exc:
                code = exc.code
        return code, calls

    def test_an_expired_token_does_not_lose_the_report(self):
        report = os.path.join(self.scratch, "report_1.md")
        with open(report, "w", encoding="utf-8") as handle:
            handle.write("# findings")
        code, calls = self._transition(report, write_rcs=[1, 0], write_stderr=GH_AUTH_STDERR)
        self.assertIsNone(code)
        self.assertEqual(self.refresh_calls, ["acme/toolkit"])
        subcommands = [argv[1:3] for argv in calls]
        self.assertIn(["issue", "comment"], subcommands)
        self.assertIn(["issue", "edit"], subcommands)
        self.assertIn(["issue", "close"], subcommands)
        self.assertFalse(os.path.exists(report))

    def test_a_permanently_broken_token_still_exits(self):
        report = os.path.join(self.scratch, "report_2.md")
        with open(report, "w", encoding="utf-8") as handle:
            handle.write("# findings")
        code, _ = self._transition(report, write_rcs=[1], write_stderr=GH_AUTH_STDERR)
        self.assertEqual(code, 1)
        self.assertEqual(self.refresh_calls, ["acme/toolkit"])
        self.assertTrue(os.path.exists(report))

    def test_rejects_paths_outside_scratch(self):
        outside = os.path.join(self.scratch, "..", "secret.md")
        sibling_report = os.path.join(self.sibling, "report_1.md")
        with open(sibling_report, "w", encoding="utf-8") as handle:
            handle.write("x")
        symlink = os.path.join(self.scratch, "link.md")
        os.symlink(self.secret, symlink)
        cases = {
            "traversal": outside,
            "absolute outside": self.secret,
            "sibling sharing the prefix": sibling_report,
            "symlink escaping scratch": symlink,
            "the scratch directory itself": self.scratch,
        }
        for label, path in cases.items():
            with self.subTest(case=label):
                code, calls = self._transition(path)
                self.assertEqual(code, 1)
                self.assertEqual(calls, [])
                self.assertTrue(os.path.exists(self.secret))

    def test_accepts_and_cleans_up_a_legitimate_report(self):
        report = os.path.join(self.scratch, "report_1.md")
        with open(report, "w", encoding="utf-8") as handle:
            handle.write("# findings")
        code, calls = self._transition(report)
        self.assertIsNone(code)
        subcommands = [argv[1:3] for argv in calls]
        self.assertIn(["issue", "comment"], subcommands)
        self.assertIn(["issue", "edit"], subcommands)
        self.assertIn(["issue", "close"], subcommands)
        self.assertFalse(os.path.exists(report))

    def test_missing_report_inside_scratch_is_rejected_without_publishing(self):
        code, calls = self._transition(os.path.join(self.scratch, "absent.md"))
        self.assertEqual(code, 1)
        self.assertEqual(calls, [])

    def test_transition_refused_when_configmap_read_fails(self):
        report = os.path.join(self.scratch, "report_1.md")
        with open(report, "w", encoding="utf-8") as handle:
            handle.write("# findings")
        with mock.patch.object(resolver, "get_managed_repos", side_effect=RuntimeError("kubectl failed: Forbidden")):
            code, calls = self._transition(report, mock_repos=None)
        self.assertEqual(code, 1)
        self.assertEqual(calls, [])


class RunGhRetryTest(unittest.TestCase):
    def setUp(self):
        self.refresh_calls = []

    def _run(self, argv, check, **stub):
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(subprocess, "run", _gh_stub(**stub)))
            stack.enter_context(mock.patch.object(resolver, "refresh_credentials", lambda repo: self.refresh_calls.append(repo)))
            stack.enter_context(mock.patch.object(resolver, "get_managed_repos", return_value=["acme/toolkit"]))
            stack.enter_context(_fresh_refresh_state())
            return resolver.run_gh(argv, check=check)

    def test_a_checked_call_survives_an_expired_token(self):
        result = self._run(["issue", "comment", "1", "-R", "acme/toolkit"], True, write_rcs=[1, 0], write_stderr=GH_AUTH_STDERR)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.refresh_calls, ["acme/toolkit"])

    def test_a_genuinely_broken_call_still_exits(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as ctx:
                self._run(["issue", "comment", "1", "-R", "acme/toolkit"], True, write_rcs=[1], write_stderr=GH_AUTH_STDERR)
        self.assertEqual(ctx.exception.code, 1)
        self.assertEqual(self.refresh_calls, ["acme/toolkit"])

    def test_a_healthy_call_never_reaches_the_broker(self):
        result = self._run(["issue", "list"], False)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.refresh_calls, [])

    def test_a_missing_binary_never_reaches_the_broker(self):
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(subprocess, "run", side_effect=FileNotFoundError))
            stack.enter_context(mock.patch.object(resolver, "refresh_credentials", lambda repo: self.refresh_calls.append(repo)))
            stack.enter_context(_fresh_refresh_state())
            result = resolver.run_gh(["auth", "status"], check=False)
        self.assertEqual(result.returncode, 127)
        self.assertEqual(self.refresh_calls, [])

    def test_one_mint_covers_a_whole_invocation(self):
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(subprocess, "run", _gh_stub(write_rcs=[1], write_stderr=GH_AUTH_STDERR)))
            stack.enter_context(mock.patch.object(resolver, "refresh_credentials", lambda repo: self.refresh_calls.append(repo)))
            stack.enter_context(_fresh_refresh_state())
            resolver.ensure_labels_exist("acme/toolkit")
        self.assertEqual(self.refresh_calls, ["acme/toolkit"])

    def test_an_unreachable_repo_is_not_a_mint(self):
        result = self._run(["issue", "list"], False, list_rc=1, list_stderr=GH_NOT_FOUND_STDERR)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.refresh_calls, [])

    def test_a_rate_limit_is_not_a_mint(self):
        result = self._run(["issue", "list"], False, list_rc=1, list_stderr="gh: API rate limit exceeded (HTTP 403)")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.refresh_calls, [])

    def test_a_sidecar_timeout_is_never_retried(self):
        result = self._run(["issue", "comment", "1"], False, write_rcs=[124], write_stderr=GH_AUTH_STDERR)
        self.assertEqual(result.returncode, 124)
        self.assertEqual(self.refresh_calls, [])

    def test_an_unconfigured_repo_is_not_a_mint(self):
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(subprocess, "run", _gh_stub(list_rc=1)))
            stack.enter_context(mock.patch.object(resolver, "refresh_credentials", lambda repo: self.refresh_calls.append(repo)))
            stack.enter_context(mock.patch.object(resolver, "get_managed_repos", return_value=[]))
            stack.enter_context(_fresh_refresh_state())
            result = resolver.run_gh(["issue", "list"], check=False)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.refresh_calls, [])


class RunGhTest(unittest.TestCase):
    def test_missing_binary_exits_when_checking(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with mock.patch.object(subprocess, "run", side_effect=FileNotFoundError):
                with self.assertRaises(SystemExit) as ctx:
                    resolver.run_gh(["auth", "status"], check=True)
        self.assertEqual(ctx.exception.code, 127)

    def test_missing_binary_degrades_when_not_checking(self):
        with mock.patch.object(subprocess, "run", side_effect=FileNotFoundError):
            result = resolver.run_gh(["auth", "status"], check=False)
        self.assertEqual(result.returncode, 127)
        self.assertEqual(result.stdout, "")

    def test_missing_binary_routes_poll_to_its_own_reason(self):
        refreshed = []
        buf = io.StringIO()
        with contextlib.ExitStack() as stack:
            stack.enter_context(contextlib.redirect_stdout(buf))
            stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
            stack.enter_context(mock.patch.object(subprocess, "run", side_effect=FileNotFoundError))
            stack.enter_context(mock.patch.object(resolver, "refresh_credentials", lambda repo: refreshed.append(repo)))
            stack.enter_context(mock.patch.object(resolver, "get_managed_repos", return_value=["acme/toolkit"]))
            stack.enter_context(_fresh_refresh_state())
            resolver.handle_poll(argparse.Namespace())
        payload = json.loads(buf.getvalue())
        self.assertEqual(payload["status"], "ERROR")
        self.assertEqual(payload["reason"], "GH_CLI_NOT_FOUND")
        self.assertEqual(refreshed, [])


if __name__ == "__main__":
    unittest.main()
