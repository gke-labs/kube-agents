import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import gitops_preflight

SCRIPT = Path(__file__).resolve().parent / "gitops_preflight.py"
REPO = "acme/fleet"
ENTRY = [{"type": "github", "url": f"https://github.com/{REPO}"}]


class _StateDir:
    """A mounted /etc/gitops stand-in: the directory, with or without the key."""

    def __init__(self, entries=None):
        self.entries = entries

    def __enter__(self):
        self.tmp = tempfile.TemporaryDirectory()
        state_file = Path(self.tmp.name) / "managed_repos"
        if self.entries is not None:
            state_file.write_text(json.dumps(self.entries), encoding="utf-8")
        self.env = patch.dict(os.environ, {"GITOPS_STATE_PATH": str(state_file)})
        self.env.start()
        return state_file

    def __exit__(self, *exc):
        self.env.stop()
        self.tmp.cleanup()


class PreflightAnswersInOneLine(unittest.TestCase):
    """Whoever runs this treats its line as final, so each state's first word
    and exit code are pinned here."""

    def test_a_mount_without_the_key_is_no_registered_repo(self):
        with _StateDir(entries=None):
            code, line = gitops_preflight.preflight()
        self.assertEqual(gitops_preflight.EXIT_NO_REGISTERED_REPO, code)
        self.assertTrue(line.startswith("NO_REGISTERED_REPO: "), line)
        self.assertIn('kanban_block(kind="needs_input")', line)
        self.assertIn("spec.integration.repositories", line)

    def test_an_empty_list_is_no_registered_repo(self):
        with _StateDir(entries=[]):
            code, line = gitops_preflight.preflight()
        self.assertEqual(gitops_preflight.EXIT_NO_REGISTERED_REPO, code)
        self.assertTrue(line.startswith("NO_REGISTERED_REPO: "), line)

    def test_a_registered_repository_is_named(self):
        with _StateDir(entries=ENTRY):
            code, line = gitops_preflight.preflight()
        self.assertEqual(gitops_preflight.EXIT_REGISTERED, code)
        self.assertEqual(f"REGISTERED: {REPO}", line)

    def test_an_unregistered_name_is_no_registered_repo_for_that_name(self):
        with _StateDir(entries=ENTRY):
            code, line = gitops_preflight.preflight("acme/other")
        self.assertEqual(gitops_preflight.EXIT_NO_REGISTERED_REPO, code)
        self.assertIn("acme/other is not registered", line)

    def test_other_spellings_of_a_registered_name_are_the_same_repository(self):
        answered = subprocess.CompletedProcess([], 0, stdout="{}", stderr="")
        for spelling in (
            "Acme/Fleet",
            "github.com/acme/fleet",
            "https://github.com/acme/fleet/",
            "https://github.com/acme/fleet.git",
            "git@github.com:acme/fleet.git",
            "https://GitHub.com/Acme/Fleet",
        ):
            with self.subTest(spelling=spelling), _StateDir(entries=ENTRY), patch(
                "subprocess.run", return_value=answered
            ):
                code, line = gitops_preflight.preflight(spelling)
            self.assertEqual(gitops_preflight.EXIT_REGISTERED, code, line)

    def test_the_reach_probe_spends_the_credential(self):
        # `capabilities` answers without the network, so it cannot tell a
        # repository the credential reads from one it does not.
        answered = subprocess.CompletedProcess([], 0, stdout="{}", stderr="")
        with _StateDir(entries=ENTRY), patch("subprocess.run", return_value=answered) as run:
            gitops_preflight.preflight(REPO)
        argv = run.call_args.args[0]
        self.assertIn("proposal", argv)
        self.assertNotIn("capabilities", argv)

    def test_a_registered_repository_the_broker_cannot_reach_is_unreachable(self):
        refused = subprocess.CompletedProcess(
            [], 1, stdout=json.dumps({"error": "403 Forbidden", "code": "FORGE_FORBIDDEN"}, indent=2), stderr=""
        )
        with _StateDir(entries=ENTRY), patch("subprocess.run", return_value=refused):
            code, line = gitops_preflight.preflight(REPO)
        self.assertEqual(gitops_preflight.EXIT_REPO_UNREACHABLE, code)
        self.assertTrue(line.startswith(f"REPO_UNREACHABLE: {REPO}: "), line)
        self.assertIn("403 Forbidden", line)

    def test_a_broker_or_forge_that_cannot_answer_is_broker_unavailable(self):
        answers = {
            "rate limited": json.dumps({"error": "slow down", "code": "FORGE_RATE_LIMITED"}),
            "forge outage": json.dumps({"error": "the forge is having its own problems", "code": "FORGE_UNAVAILABLE"}),
            "route unsupported": json.dumps({"error": "roll the image", "code": "BROKER_ROUTE_UNSUPPORTED"}),
            "broker down": json.dumps({"error": "the broker at http://x could not be reached: refused. Retry shortly."}),
        }
        for desc, stdout in answers.items():
            failed = subprocess.CompletedProcess([], 1, stdout=stdout, stderr="")
            with self.subTest(desc), _StateDir(entries=ENTRY), patch("subprocess.run", return_value=failed):
                code, line = gitops_preflight.preflight(REPO)
            self.assertEqual(gitops_preflight.EXIT_BROKER_UNAVAILABLE, code, line)
            self.assertTrue(line.startswith("BROKER_UNAVAILABLE: "), line)
            self.assertIn("not a registration problem", line)
            self.assertNotIn("retried once", line)
            self.assertIn("says what to fix", line)

    def test_entries_that_are_all_unusable_are_named_not_called_missing(self):
        unusable = [{"type": "github", "url": "https://github.example.com:8443/acme/fleet"}]
        with _StateDir(entries=unusable):
            code, line = gitops_preflight.preflight()
        self.assertEqual(gitops_preflight.EXIT_NO_REGISTERED_REPO, code)
        self.assertIn("not usable", line)
        self.assertIn("github.example.com:8443/acme/fleet", line)

    def test_a_repository_the_broker_does_not_manage_is_no_registered_repo(self):
        refused = subprocess.CompletedProcess(
            [], 1, stdout=json.dumps({"error": "not managed", "code": "REPOSITORY_NOT_MANAGED"}), stderr=""
        )
        with _StateDir(entries=ENTRY), patch("subprocess.run", return_value=refused):
            code, line = gitops_preflight.preflight(REPO)
        self.assertEqual(gitops_preflight.EXIT_NO_REGISTERED_REPO, code)
        self.assertTrue(line.startswith("NO_REGISTERED_REPO: the broker does not manage"), line)

    def test_registered_answers_in_the_lists_spelling(self):
        answered = subprocess.CompletedProcess([], 0, stdout="{}", stderr="")
        with _StateDir(entries=ENTRY), patch("subprocess.run", return_value=answered) as run:
            code, line = gitops_preflight.preflight("https://GitHub.com/Acme/Fleet.git")
        self.assertEqual(f"REGISTERED: {REPO}", line)
        self.assertIn(REPO, run.call_args.args[0])

    def test_an_unregistered_url_is_never_echoed_with_userinfo(self):
        with _StateDir(entries=ENTRY):
            code, line = gitops_preflight.preflight("https://user:s3cret@github.com/acme/other")
        self.assertEqual(gitops_preflight.EXIT_NO_REGISTERED_REPO, code)
        self.assertNotIn("s3cret", line)

    def test_a_multi_line_read_error_stays_one_line(self):
        with patch.object(
            gitops_preflight.gitops_workspace, "get_managed_repos", side_effect=RuntimeError("a\nb\nc")
        ):
            code, line = gitops_preflight.preflight()
        self.assertEqual(1, len(line.splitlines()))

    def test_a_github_browser_url_names_its_repository(self):
        answered = subprocess.CompletedProcess([], 0, stdout="{}", stderr="")
        for url in ("https://github.com/acme/fleet/tree/main", "https://github.com/acme/fleet/pull/12"):
            with self.subTest(url=url), _StateDir(entries=ENTRY), patch("subprocess.run", return_value=answered):
                code, line = gitops_preflight.preflight(url)
            self.assertEqual(f"REGISTERED: {REPO}", line)

    def test_a_reachable_registered_repository_is_registered(self):
        answered = subprocess.CompletedProcess([], 0, stdout="{}", stderr="")
        with _StateDir(entries=ENTRY), patch("subprocess.run", return_value=answered):
            code, line = gitops_preflight.preflight(REPO)
        self.assertEqual(gitops_preflight.EXIT_REGISTERED, code)
        self.assertEqual(f"REGISTERED: {REPO}", line)

    def test_an_unreadable_registry_is_state_unreadable(self):
        with patch.object(
            gitops_preflight.gitops_workspace, "get_managed_repos", side_effect=RuntimeError("boom")
        ):
            code, line = gitops_preflight.preflight()
        self.assertEqual(gitops_preflight.EXIT_STATE_UNREADABLE, code)
        self.assertTrue(line.startswith("STATE_UNREADABLE: "), line)

    def test_the_exit_codes_are_distinct_and_only_registered_is_zero(self):
        codes = {
            gitops_preflight.EXIT_REGISTERED,
            gitops_preflight.EXIT_NO_REGISTERED_REPO,
            gitops_preflight.EXIT_REPO_UNREACHABLE,
            gitops_preflight.EXIT_STATE_UNREADABLE,
            gitops_preflight.EXIT_BROKER_UNAVAILABLE,
        }
        self.assertEqual(5, len(codes))
        self.assertEqual(0, gitops_preflight.EXIT_REGISTERED)


class ListingShowsBothRegistries(unittest.TestCase):
    """`--list` is for SOUL.md's repository resolution: managed versus context."""

    def test_nothing_registered_lists_none_twice_and_exits_zero(self):
        with _StateDir(entries=None):
            code, text = gitops_preflight.listing()
        self.assertEqual(gitops_preflight.EXIT_REGISTERED, code)
        self.assertEqual("MANAGED: none\nCONTEXT: none", text)

    def test_unusable_managed_entries_are_listed_as_unusable_not_none(self):
        unusable = [{"type": "github", "url": "https://github.example.com:8443/acme/fleet"}]
        with _StateDir(entries=unusable):
            code, text = gitops_preflight.listing()
        self.assertTrue(text.startswith("MANAGED: none usable (1 registered, unusable: "), text)

    def test_managed_and_context_are_listed_apart(self):
        context = [{"type": "github", "url": "https://github.com/acme/tf-live", "ref": "prod"}]
        with _StateDir(entries=ENTRY) as state_file:
            (state_file.parent / "context_repos").write_text(json.dumps(context), encoding="utf-8")
            code, text = gitops_preflight.listing()
        self.assertEqual(gitops_preflight.EXIT_REGISTERED, code)
        self.assertEqual(f"MANAGED: {REPO}\nCONTEXT: github.com/acme/tf-live@prod", text)

    def test_a_context_entry_with_a_refused_ref_says_it_is_not_searched(self):
        context = [{"type": "github", "url": "https://github.com/acme/tf-live", "ref": "-evil"}]
        with _StateDir(entries=ENTRY) as state_file:
            (state_file.parent / "context_repos").write_text(json.dumps(context), encoding="utf-8")
            code, text = gitops_preflight.listing()
        self.assertEqual(gitops_preflight.EXIT_REGISTERED, code)
        self.assertIn("ref-refused", text)
        self.assertIn("not-searched", text)

    def test_userinfo_in_a_context_url_is_never_printed(self):
        context = [{"type": "github", "url": "https://x-access-token:s3cret@github.com/acme/tf-live"}]
        with _StateDir(entries=ENTRY) as state_file:
            (state_file.parent / "context_repos").write_text(json.dumps(context), encoding="utf-8")
            code, text = gitops_preflight.listing()
        self.assertNotIn("s3cret", text)
        self.assertNotIn("x-access-token", text)

    def test_an_unreadable_context_registry_is_state_unreadable(self):
        with _StateDir(entries=ENTRY), patch.object(
            gitops_preflight.gitops_workspace, "get_context_repo_entries", side_effect=RuntimeError("boom")
        ):
            code, text = gitops_preflight.listing()
        self.assertEqual(gitops_preflight.EXIT_STATE_UNREADABLE, code)

    def test_an_unreadable_registry_is_state_unreadable(self):
        with patch.object(
            gitops_preflight.gitops_workspace, "get_managed_repos", side_effect=RuntimeError("boom")
        ):
            code, text = gitops_preflight.listing()
        self.assertEqual(gitops_preflight.EXIT_STATE_UNREADABLE, code)
        self.assertTrue(text.startswith("STATE_UNREADABLE: "), text)


class PreflightRunsAsAPlainCommand(unittest.TestCase):
    """Workers run it as `python3 <path>`, never `python3 -c`, which the
    sandbox's command scanner refuses. The process prints one line and exits
    with the state's code."""

    def test_the_script_prints_one_line_and_exits_with_the_code(self):
        with _StateDir(entries=None) as state_file:
            result = subprocess.run(
                [sys.executable, str(SCRIPT)],
                capture_output=True,
                text=True,
                env={**os.environ, "GITOPS_STATE_PATH": str(state_file)},
                check=False,
            )
        self.assertEqual(gitops_preflight.EXIT_NO_REGISTERED_REPO, result.returncode)
        self.assertEqual(1, len(result.stdout.strip().splitlines()))
        self.assertTrue(result.stdout.startswith("NO_REGISTERED_REPO: "))


if __name__ == "__main__":
    unittest.main()
