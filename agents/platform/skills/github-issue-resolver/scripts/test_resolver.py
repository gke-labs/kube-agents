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


def _gh_stub(auth_rc: int = 0, list_rc: int = 0, list_stdout: str = "[]", record=None):
    """A ``subprocess.run`` replacement that routes on the gh subcommand."""

    def run(argv, **kwargs):
        if record is not None:
            record.append(argv)
        sub = argv[1:]
        if sub[:2] == ["auth", "status"]:
            return subprocess.CompletedProcess(argv, auth_rc, "", "")
        if sub[:2] == ["issue", "list"]:
            return subprocess.CompletedProcess(argv, list_rc, list_stdout, "")
        return subprocess.CompletedProcess(argv, 0, "[]", "")

    return run


class GetManagedReposTest(unittest.TestCase):
    """Test get_managed_repos parsing from the ConfigMap."""

    def test_extracts_managed_repos_list(self):
        cm_json = json.dumps(
            {"data": {"managed_repos": "gke-labs/kube-agents, acme/toolkit"}}
        )
        with mock.patch(
            "subprocess.run",
            return_value=subprocess.CompletedProcess([], 0, cm_json, ""),
        ):
            self.assertEqual(
                resolver.get_managed_repos(),
                ["gke-labs/kube-agents", "acme/toolkit"],
            )

    def test_empty_when_no_managed_repos(self):
        cm_json = json.dumps({"data": {"managed_repos": ""}})
        with mock.patch(
            "subprocess.run",
            return_value=subprocess.CompletedProcess([], 0, cm_json, ""),
        ):
            self.assertEqual(resolver.get_managed_repos(), [])

    def test_empty_when_kubectl_fails(self):
        with mock.patch(
            "subprocess.run", side_effect=subprocess.CalledProcessError(1, [])
        ):
            self.assertEqual(resolver.get_managed_repos(), [])


class HandlePollRoutingTest(unittest.TestCase):
    """Each failure mode must be distinguishable in the emitted JSON."""

    def _poll(self, repos, **stub):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            with mock.patch.object(resolver, "get_managed_repos", return_value=repos):
                with mock.patch.object(subprocess, "run", _gh_stub(**stub)):
                    resolver.handle_poll(argparse.Namespace())
        return json.loads(buf.getvalue())

    def test_not_configured_is_its_own_status(self):
        """Distinct from NO_ISSUES when no repos are managed."""
        self.assertEqual(self._poll([])["status"], "NOT_CONFIGURED")

    def test_broken_auth_is_a_loud_error(self):
        payload = self._poll(["acme/toolkit"], auth_rc=1)
        self.assertEqual(payload["status"], "ERROR")
        self.assertEqual(payload["reason"], "GITHUB_AUTH_NOT_CONFIGURED")

    def test_unreachable_repo_is_a_loud_error(self):
        payload = self._poll(["acme/toolkit"], list_rc=1)
        self.assertEqual(payload["status"], "ERROR")
        self.assertEqual(payload["reason"], "REPO_UNREACHABLE")
        self.assertEqual(payload["repository"], "acme/toolkit")

    def test_healthy_and_quiet_is_no_issues(self):
        payload = self._poll(["acme/toolkit"])
        self.assertEqual(payload["status"], "NO_ISSUES")

    def test_healthy_with_work_is_found(self):
        payload = self._poll(
            ["acme/toolkit"],
            list_stdout=json.dumps(
                [
                    {
                        "number": 9,
                        "title": "second",
                        "body": "b",
                        "comments": [],
                    },
                    {
                        "number": 7,
                        "title": "first",
                        "body": "b",
                        "comments": [
                            {
                                "author": {"login": "alice"},
                                "body": "hi",
                                "createdAt": "2026-07-30T00:00:00Z",
                            }
                        ],
                    },
                ]
            ),
        )
        self.assertEqual(payload["status"], "FOUND")
        # Lowest-numbered open issue wins, regardless of listing order.
        self.assertEqual(payload["issue_number"], 7)
        self.assertEqual(payload["repository"], "acme/toolkit")
        self.assertEqual(payload["comments"][0]["author"], "alice")


class HandleClaimTest(unittest.TestCase):
    """Test claim operation."""

    def test_claim_adds_label_and_comment(self):
        calls = []
        args = argparse.Namespace(issue=42, repo="acme/toolkit")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            with mock.patch.object(subprocess, "run", _gh_stub(record=calls)):
                resolver.handle_claim(args)
        payload = json.loads(buf.getvalue())
        self.assertEqual(payload["status"], "CLAIMED")
        self.assertEqual(payload["issue_number"], 42)
        self.assertEqual(payload["repository"], "acme/toolkit")


class ReportFilePathGuardTest(unittest.TestCase):
    """--report-file is published publicly and then unlinked."""

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

    def _transition(self, report_file):
        """Returns (exit_code_or_None, gh_argv_list)."""
        calls = []
        args = argparse.Namespace(
            issue=1, repo="acme/toolkit", state="resolved", report_file=report_file
        )
        buf, err = io.StringIO(), io.StringIO()
        code = None
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
            with mock.patch.object(subprocess, "run", _gh_stub(record=calls)):
                try:
                    resolver.handle_transition(args)
                except SystemExit as exc:
                    code = exc.code
        return code, calls

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


class RunGhTest(unittest.TestCase):
    """A missing `gh` binary must not look like a clean result."""

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
        with mock.patch.object(
            resolver, "get_managed_repos", return_value=["acme/toolkit"]
        ):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(
                io.StringIO()
            ):
                with mock.patch.object(
                    subprocess, "run", side_effect=FileNotFoundError
                ):
                    resolver.handle_poll(argparse.Namespace())
            payload = json.loads(buf.getvalue())
        self.assertEqual(payload["status"], "ERROR")
        self.assertEqual(payload["reason"], "GH_CLI_NOT_FOUND")


if __name__ == "__main__":
    unittest.main()
