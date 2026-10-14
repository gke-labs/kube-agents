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
    """SOUL.md and every PR-writing skill treat this script's line as final, so
    each state's first word and exit code are pinned here."""

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

    def test_a_registered_repository_the_broker_cannot_reach_is_unreachable(self):
        refused = subprocess.CompletedProcess([], 1, stdout="", stderr="403 Forbidden")
        with _StateDir(entries=ENTRY), patch("subprocess.run", return_value=refused):
            code, line = gitops_preflight.preflight(REPO)
        self.assertEqual(gitops_preflight.EXIT_REPO_UNREACHABLE, code)
        self.assertTrue(line.startswith(f"REPO_UNREACHABLE: {REPO}: "), line)
        self.assertIn("403 Forbidden", line)

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
        }
        self.assertEqual(4, len(codes))
        self.assertEqual(0, gitops_preflight.EXIT_REGISTERED)


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
