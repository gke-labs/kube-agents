"""Unit tests for resolver.py, the github-issue-resolver skill's helper.

Run: python3 -m unittest agents/platform/skills/github-issue-resolver/scripts/test_resolver.py

Two properties carry most of the weight here.

The first is the distinction between *silence* and *fault*. "No repository is
configured" and "the repository is configured but I cannot read it" both stop
the resolver, but only the first is a supported state. If both are reported the
same way, the skill silences both, and a deployment whose SETTINGS.md has a
typo in it stops triaging issues permanently with nobody the wiser. Every
routing test below exists to keep those two outcomes apart.

The second is that ``--report-file`` is confined to the scratch directory. The
report is posted to a public issue and then unlinked, so a path that escapes
the directory is both an exfiltration and an arbitrary-delete primitive. Those
tests assert the rejection happens *before* any ``gh`` call and before the
unlink, not merely that an error is printed.
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


def _write_settings(directory: str, value=None, key: bool = True) -> str:
    """Write a SETTINGS.md fixture mirroring buildSettingsConfigMap's format.

    ``key=False`` omits the ``Git Repo:`` line entirely, which is distinct from
    a line whose value is empty.
    """
    path = os.path.join(directory, "SETTINGS.md")
    body = "# GKE Scope Configuration\n"
    if key:
        body += f"- **Git Repo:** {'' if value is None else value}\n"
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(body)
    return path


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


class GetTargetRepoParsingTest(unittest.TestCase):
    """Every URL form an operator could plausibly paste into SETTINGS.md."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.d = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def _parse(self, value):
        return resolver.get_target_repo(
            required=False, settings_path=_write_settings(self.d, value)
        )

    def test_accepts_supported_url_forms(self):
        cases = {
            "https://github.com/gke-labs/kube-agents": "gke-labs/kube-agents",
            "https://github.com/gke-labs/kube-agents.git": "gke-labs/kube-agents",
            "http://github.com/acme/toolkit": "acme/toolkit",
            # The previous parser stripped "www." explicitly; anchoring the
            # host must not quietly drop support for it.
            "https://www.github.com/acme/toolkit": "acme/toolkit",
            # SCP-form SSH puts a colon, not a slash, after the host.
            "git@github.com:acme/toolkit.git": "acme/toolkit",
            "ssh://git@github.com/acme/toolkit.git": "acme/toolkit",
            "github.com/acme/toolkit": "acme/toolkit",
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(self._parse(value), expected)

    def test_accepts_bare_owner_repo_shorthand(self):
        """The operator accepts this form, so we must too.

        ``ValidateGitRepoURL`` returns nil for a bare "owner/repo"
        (common_types.go, ownerRepoRegex -- with "gke-labs/kube-agents" as its
        own worked example, asserted in common_types_test.go), and
        ``buildSettingsConfigMap`` writes it through verbatim rather than
        substituting "None". Rejecting it here would alert every poll on a
        supported configuration -- exactly the loud-on-a-working-deployment
        failure this script exists to avoid.
        """
        cases = {
            "gke-labs/kube-agents": "gke-labs/kube-agents",
            "gke-labs/kube-agents.git": "gke-labs/kube-agents",
            "acme/toolkit": "acme/toolkit",
            "acme/digit": "acme/digit",
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(self._parse(value), expected)

    def test_suffix_strip_does_not_eat_repo_name_characters(self):
        """Regression guard for the ``rstrip('.git')`` character-set bug.

        ``rstrip`` removes any trailing run of ``.``, ``g``, ``i``, ``t`` --
        so "digit" lost its tail. These two names are the canaries.
        """
        for name in ("acme/digit", "acme/toolkit", "acme/gitgit"):
            with self.subTest(name=name):
                self.assertEqual(self._parse(f"https://github.com/{name}"), name)


class GetTargetRepoRejectionTest(unittest.TestCase):
    """Values that name *something*, but nothing we are willing to act on."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.d = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def _assert_rejected(self, value):
        with self.assertRaises(resolver.RepoUnparseable):
            resolver.get_target_repo(
                required=False, settings_path=_write_settings(self.d, value)
            )

    def test_host_must_not_match_as_a_substring(self):
        """A typo'd host must not silently retarget the agent."""
        for value in (
            "https://evilgithub.com/attacker/repo",
            "https://www.evilgithub.com/attacker/repo",
            "https://notgithub.com/attacker/repo",
            "notgithub.com/attacker/repo",
            "https://github.com.evil.com/attacker/repo",
        ):
            with self.subTest(value=value):
                self._assert_rejected(value)

    def test_host_must_not_match_as_a_path_segment(self):
        """github.com in the *path* of another host is not our repository.

        The operator's ``ValidateGitRepoURL`` only requires a non-empty host,
        so any of these can land in SETTINGS.md. Anchoring merely on a
        preceding delimiter accepted all of them -- a value that reads like an
        internal mirror in review would silently point the agent at public
        GitHub, where it posts kubectl-derived triage reports.
        """
        for value in (
            "https://evil.com/github.com/attacker/repo",
            "https://gitlab.com/github.com/attacker/repo",
            "git@evil.com:x/github.com/attacker/repo",
            "https://user@evil.com/github.com/a/b",
        ):
            with self.subTest(value=value):
                self._assert_rejected(value)

    def test_rejects_non_github_hosts(self):
        for value in (
            "https://ghe.corp.example.com/acme/toolkit",
            "https://gitlab.com/acme/toolkit",
        ):
            with self.subTest(value=value):
                self._assert_rejected(value)

    def test_rejects_traversal_and_flag_like_components(self):
        """The character class permits "." and "-"; these must not survive it.

        ``../..`` is a shape the pattern happily produces, and a leading dash
        would be read by ``gh -R`` as a flag rather than as a repository.
        """
        for value in (
            "https://github.com/../../etc",
            "github.com/../..",
            "https://github.com/acme/.git",
            "https://github.com/-flag/repo",
            "https://github.com/acme/-flag",
            # These satisfy BARE_REPO_RE, so only the component guard stops
            # them. Accepting the shorthand must not open a traversal path.
            "../..",
            "./.",
            "-flag/repo",
            "acme/-flag",
        ):
            with self.subTest(value=value):
                self._assert_rejected(value)

    def test_rejects_unstructured_garbage(self):
        for value in ("totally-bogus", "/", "???", "a/b/c", "https://", "acme/"):
            with self.subTest(value=value):
                self._assert_rejected(value)

    def test_unparseable_raises_in_both_required_modes(self):
        """Finding 1's core guarantee.

        ``required`` governs the *absent* case only. A configured-but-broken
        value is a fault either way -- if ``required=False`` downgraded it to
        ``None``, poll would silence it forever.
        """
        path = _write_settings(self.d, "totally-bogus")
        for required in (True, False):
            with self.subTest(required=required):
                with self.assertRaises(resolver.RepoUnparseable):
                    resolver.get_target_repo(
                        required=required, settings_path=path
                    )


class GetTargetRepoAbsentTest(unittest.TestCase):
    """No repository configured is a supported deployment, not a fault."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.d = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def _absent_paths(self):
        return {
            # What the operator writes when Integration.GitHub is unset.
            "literal None": _write_settings(self.d, "None"),
            "lowercase none": _write_settings(self.d, "none"),
            "empty value": _write_settings(self.d, ""),
            "no Git Repo line": _write_settings(self.d, key=False),
            "missing file": os.path.join(self.d, "does-not-exist.md"),
        }

    def test_absent_returns_none_when_not_required(self):
        for label, path in self._absent_paths().items():
            with self.subTest(case=label):
                self.assertIsNone(
                    resolver.get_target_repo(required=False, settings_path=path)
                )

    def test_absent_exits_when_required(self):
        for label, path in self._absent_paths().items():
            with self.subTest(case=label):
                with contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as ctx:
                        resolver.get_target_repo(
                            required=True, settings_path=path
                        )
                self.assertEqual(ctx.exception.code, 1)

    def test_required_defaults_to_true(self):
        """A caller that omits the flag gets the safe behaviour, not silence."""
        path = _write_settings(self.d, "None")
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                resolver.get_target_repo(settings_path=path)


class ResolveRepoOrExitTest(unittest.TestCase):
    """claim/transition have an issue number in hand and no degraded mode."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.d = self._tmp.name
        self._settings = resolver.SETTINGS_PATH

    def tearDown(self):
        resolver.SETTINGS_PATH = self._settings
        self._tmp.cleanup()

    def test_unparseable_becomes_exit_not_exception(self):
        resolver.SETTINGS_PATH = _write_settings(self.d, "totally-bogus")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as ctx:
                resolver.resolve_repo_or_exit(required=True)
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("Could not extract target repository", err.getvalue())

    def test_valid_repo_passes_through(self):
        resolver.SETTINGS_PATH = _write_settings(
            self.d, "https://github.com/acme/toolkit"
        )
        self.assertEqual(resolver.resolve_repo_or_exit(required=True), "acme/toolkit")


class HandlePollRoutingTest(unittest.TestCase):
    """Each failure mode must be distinguishable in the emitted JSON."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.d = self._tmp.name
        self._settings = resolver.SETTINGS_PATH

    def tearDown(self):
        resolver.SETTINGS_PATH = self._settings
        self._tmp.cleanup()

    def _poll(self, value, key=True, **stub):
        resolver.SETTINGS_PATH = _write_settings(self.d, value, key=key)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            with mock.patch.object(subprocess, "run", _gh_stub(**stub)):
                resolver.handle_poll(argparse.Namespace())
        return json.loads(buf.getvalue())

    def test_not_configured_is_its_own_status(self):
        """Distinct from NO_ISSUES so the two cannot be conflated later."""
        self.assertEqual(self._poll("None")["status"], "NOT_CONFIGURED")
        self.assertEqual(self._poll(None, key=False)["status"], "NOT_CONFIGURED")

    def test_unparseable_repo_is_a_loud_error(self):
        payload = self._poll("totally-bogus")
        self.assertEqual(payload["status"], "ERROR")
        self.assertEqual(payload["reason"], "GIT_REPO_UNPARSEABLE")

    def test_broken_auth_is_a_loud_error(self):
        payload = self._poll("https://github.com/acme/toolkit", auth_rc=1)
        self.assertEqual(payload["status"], "ERROR")
        self.assertEqual(payload["reason"], "GITHUB_AUTH_NOT_CONFIGURED")

    def test_unreachable_repo_is_a_loud_error(self):
        """`gh auth status` passes if *any* host is authenticated.

        A token without scope for this repo, or a repo that 404s, only fails
        at `issue list` -- which previously exited non-zero having printed no
        JSON at all, leaving the skill with nothing to branch on.
        """
        payload = self._poll("https://github.com/acme/toolkit", list_rc=1)
        self.assertEqual(payload["status"], "ERROR")
        self.assertEqual(payload["reason"], "REPO_UNREACHABLE")
        self.assertEqual(payload["repository"], "acme/toolkit")

    def test_healthy_and_quiet_is_no_issues(self):
        payload = self._poll("https://github.com/acme/toolkit")
        self.assertEqual(payload["status"], "NO_ISSUES")

    def test_healthy_with_work_is_found(self):
        payload = self._poll(
            "https://github.com/acme/toolkit",
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

    def test_no_routing_path_raises_systemexit(self):
        """poll's contract is JSON on stdout, never a bare non-zero exit."""
        cases = (
            {"value": "None"},
            {"value": "totally-bogus"},
            {"value": "https://github.com/acme/toolkit", "auth_rc": 1},
            {"value": "https://github.com/acme/toolkit", "list_rc": 1},
            {"value": "https://github.com/acme/toolkit"},
        )
        for case in cases:
            value = case.pop("value")
            with self.subTest(case=value, **case):
                try:
                    payload = self._poll(value, **case)
                except SystemExit as exc:  # pragma: no cover - failure path
                    self.fail(f"poll exited with {exc.code} instead of emitting JSON")
                self.assertIn("status", payload)


class ReportFilePathGuardTest(unittest.TestCase):
    """--report-file is published publicly and then unlinked.

    A path that escapes the scratch directory is therefore both an
    exfiltration primitive and an arbitrary-delete primitive. Rejection must
    happen before either effect.
    """

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.d = self._tmp.name
        self._settings = resolver.SETTINGS_PATH
        self._scratch = resolver.SCRATCH_DIR

        self.scratch = os.path.join(self.d, "scratch")
        os.makedirs(self.scratch)
        # A sibling whose name shares the scratch prefix; the "+ os.sep" in the
        # guard is what keeps this from being accepted.
        self.sibling = os.path.join(self.d, "scratch-evil")
        os.makedirs(self.sibling)

        self.secret = os.path.join(self.d, "secret.md")
        with open(self.secret, "w", encoding="utf-8") as handle:
            handle.write("private")

        resolver.SCRATCH_DIR = self.scratch
        resolver.SETTINGS_PATH = _write_settings(
            self.d, "https://github.com/acme/toolkit"
        )

    def tearDown(self):
        resolver.SETTINGS_PATH = self._settings
        resolver.SCRATCH_DIR = self._scratch
        self._tmp.cleanup()

    def _transition(self, report_file):
        """Returns (exit_code_or_None, gh_argv_list)."""
        calls = []
        args = argparse.Namespace(
            issue=1, state="resolved", report_file=report_file
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
                # Nothing was published...
                self.assertEqual(calls, [])
                # ...and nothing was deleted.
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
        # The scratch file is removed once its contents are public.
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
        """An absent binary is not a rejected token.

        They need different operators and different fixes, so collapsing them
        into one reason code would send whoever reads the alert to the wrong
        place -- the same conflation this script exists to avoid.
        """
        with TemporaryDirectory() as tmp:
            original = resolver.SETTINGS_PATH
            resolver.SETTINGS_PATH = _write_settings(
                tmp, "https://github.com/acme/toolkit"
            )
            try:
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(
                    io.StringIO()
                ):
                    with mock.patch.object(
                        subprocess, "run", side_effect=FileNotFoundError
                    ):
                        resolver.handle_poll(argparse.Namespace())
                payload = json.loads(buf.getvalue())
            finally:
                resolver.SETTINGS_PATH = original
        self.assertEqual(payload["status"], "ERROR")
        self.assertEqual(payload["reason"], "GH_CLI_NOT_FOUND")


if __name__ == "__main__":
    unittest.main()
