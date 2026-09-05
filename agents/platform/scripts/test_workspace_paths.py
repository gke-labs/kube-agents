#!/usr/bin/env python3
"""Tests for the workspace protocol's path validator.

The validator was reachable from tests only through
`content_workspace.repo_relative`, and `content_workspace` is the one module
the sandbox image does not carry -- so the end that turns a name into a write
was covered by exercising the end that does not. These import
`workspace_paths` on its own, the way `inspect_repository` does.

Names travel both ways through this validator, which is the property most of
these are about: `list` answers with names read out of a repository nobody
here chose, and `read` takes one of those names back, so a spelling refused
here is a file the protocol cannot see.

Run:  python3 agents/platform/scripts/test_workspace_paths.py
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.absolute()))

import workspace_paths


class ColonTest(unittest.TestCase):
    def test_a_colon_in_a_name_is_a_name(self):
        """git accepts these and a repository can hold one.

        `grep` already passes `-z` so that a name carrying a colon cannot be
        misread, which means `list` can answer with one -- and a caller that
        hands that name back to `read` must not be told its own repository's
        file is an absolute path.
        """
        for path in ("foo:bar.yaml", "C:/manifests/app.yaml", "manifests/a:b.yaml"):
            with self.subTest(path=path):
                self.assertEqual(path, workspace_paths.validate_path(path))

    def test_a_colon_does_not_get_a_segment_out_of_the_dotgit_rule(self):
        """The one refusal a colon still participates in.

        `.git:x` is `.git` to anything that treats the colon as a suffix
        separator, and the repository this writes into is checked out
        somewhere this validator does not choose.
        """
        with self.assertRaises(workspace_paths.WorkspaceError):
            workspace_paths.validate_path(".git:stream/config")


class ControlCharacterTest(unittest.TestCase):
    def test_every_control_character_is_refused_not_just_nul_and_newline(self):
        for path in (
            "manifests/dep\x00loyment.yaml",
            "manifests/app\x1byaml",  # ESC: rewrites the terminal that logs it
            "manifests/app\tyaml",
            "manifests/app\x85yaml",  # C1 NEL
            "manifests/app\x0byaml",
        ):
            with self.subTest(path=path):
                with self.assertRaises(workspace_paths.WorkspaceError) as caught:
                    workspace_paths.validate_path(path)
                self.assertIn("control characters", str(caught.exception))

    def test_a_name_ending_in_a_newline_says_so_rather_than_calling_it_spacing(self):
        """`strip` answers for it otherwise, and answers wrongly.

        "leading or trailing whitespace; write it without" reads as a
        formatting nit. The name carries a newline, which is the thing worth
        saying, so the control-character check has to come first.
        """
        with self.assertRaises(workspace_paths.WorkspaceError) as caught:
            workspace_paths.validate_path("manifests/app.yaml\n")
        self.assertIn("control characters", str(caught.exception))

    def test_surrounding_whitespace_is_still_whitespace(self):
        for path in (" manifests/app.yaml", "manifests/app.yaml "):
            with self.subTest(path=path):
                with self.assertRaises(workspace_paths.WorkspaceError) as caught:
                    workspace_paths.validate_path(path)
                self.assertIn("whitespace", str(caught.exception))


class StandaloneRulesTest(unittest.TestCase):
    """The refusals the sandbox end depends on, checked without the broker."""

    def test_the_shape_of_a_name_that_escapes(self):
        for path in (
            "",
            "/etc/passwd",
            "../etc/passwd",
            "manifests/../../etc/passwd",
            "manifests//app.yaml",
            "manifests/./app.yaml",
            "manifests\\app.yaml",
            ".git/config",
            "manifests/.git/hooks/pre-commit",
            "GIT~1/config",
        ):
            with self.subTest(path=path):
                with self.assertRaises(workspace_paths.WorkspaceError):
                    workspace_paths.validate_path(path)

    def test_a_non_string_is_refused_before_anything_touches_it(self):
        for value in (None, 17, b"manifests/app.yaml", ["manifests"]):
            with self.subTest(value=value):
                with self.assertRaises(workspace_paths.WorkspaceError):
                    workspace_paths.validate_path(value)

    def test_an_ordinary_name_comes_back_unchanged(self):
        for path in (
            "manifests/prod/deployment.yaml",
            ".gitignore",
            "charts/kube-agents/values.yaml",
        ):
            with self.subTest(path=path):
                self.assertEqual(path, workspace_paths.validate_path(path))

    def test_the_dotgit_rule_is_public_because_another_module_calls_it(self):
        """`content_workspace` filters its own tree walk with this.

        It reached in for a private name to do it, which is the two
        implementations the module header argues against, one import short of
        being two functions.
        """
        self.assertTrue(workspace_paths.looks_like_dotgit(".GIT"))
        self.assertFalse(workspace_paths.looks_like_dotgit("gitops"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
