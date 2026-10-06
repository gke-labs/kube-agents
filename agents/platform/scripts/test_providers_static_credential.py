#!/usr/bin/env python3
"""The long-lived token a forge reads from a mounted Secret, and its git helper.

    python3 -m pytest -q agents/platform/scripts/test_providers_static_credential.py

The git half is run through `git credential fill` rather than asserted as a
string: what matters is which helper git actually asks, for which host, and
that the ambient helper another forge installed is not one of them.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import git_credential_token_file
import providers
from providers.credentials import StaticFileCredential
from workspace_paths import WorkspaceError

HOST = "forge.example.test"
HELPER = str(Path(git_credential_token_file.__file__).resolve())


class CredentialTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.token = self.dir / "token"
        self.token.write_text("glpat-first\n")

    def credential(self, **kwargs):
        kwargs.setdefault("header", "PRIVATE-TOKEN")
        kwargs.setdefault("helper", HELPER)
        return StaticFileCredential(str(self.token), HOST, **kwargs)

    def test_it_is_exported_beside_the_other_strategies(self):
        self.assertIs(providers.StaticFileCredential, StaticFileCredential)

    def test_ensure_has_nothing_to_do(self):
        self.assertIsNone(self.credential().ensure("acme/infra"))

    def test_the_header_is_read_from_the_file_on_every_call(self):
        # A rotated Secret is the next call's token, with no restart.
        credential = self.credential()
        self.assertEqual({"PRIVATE-TOKEN": "glpat-first"}, credential.headers("acme/infra"))
        self.token.write_text("glpat-rotated\n")
        self.assertEqual({"PRIVATE-TOKEN": "glpat-rotated"}, credential.headers("acme/infra"))

    def test_the_forge_chooses_the_header_and_its_format(self):
        credential = self.credential(header="Authorization", header_format="Bearer {token}")
        self.assertEqual({"Authorization": "Bearer glpat-first"}, credential.headers("a/b"))

    def test_a_missing_or_empty_file_is_refused_not_sent_anonymously(self):
        # Review finding: a second line passed, then failed as an invalid
        # header on the API side and as nothing at all from the git helper.
        # Review round 2: a byte-order mark or a copy-pasted non-ASCII
        # character passed and died as a bare 500 inside the header encoder.
        for content in (
            None, "", "  \n", "glpat-a\nglpat-b\n", "glpat-a\rx",
            "\ufeffglpat-a\n", "glpat\u2011a", "glpat a",
        ):
            with self.subTest(content=content):
                if content is None:
                    self.token.unlink(missing_ok=True)
                else:
                    self.token.write_text(content)
                with self.assertRaises(WorkspaceError) as caught:
                    self.credential().headers("acme/infra")
                self.assertEqual(503, caught.exception.status)
                self.assertEqual("FORGE_CREDENTIAL_UNAVAILABLE", caught.exception.fields["code"])

    def test_nothing_that_would_reach_the_shell_is_accepted(self):
        # Git runs the helper value through the shell, so its two arguments are
        # held to a character set with nothing a shell treats specially.
        for path, username, host in (
            ("/run/token; rm -rf /", "oauth2", HOST),
            ("/run/to ken", "oauth2", HOST),
            ("relative/token", "oauth2", HOST),
            ("/run/../etc/shadow", "oauth2", HOST),
            ("/run/token", "o$(id)", HOST),
            ("/run/token", "oauth2", "forge.example.test/evil"),
            ("/run/token", "oauth2", ""),
        ):
            with self.subTest(path=path, username=username, host=host):
                with self.assertRaises(ValueError):
                    StaticFileCredential(path, host, header="PRIVATE-TOKEN", username=username)

    def test_git_is_given_a_helper_for_this_host_after_the_ambient_one_is_cleared(self):
        self.assertEqual(
            (
                ("credential.helper", ""),
                (f"credential.https://{HOST}.helper", f"{HELPER} {self.token} oauth2"),
            ),
            self.credential().git_config("acme/infra"),
        )


class GitAsksTheHelperTest(unittest.TestCase):
    """`git credential fill` under the config the broker applies."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.token = self.dir / "token"
        self.token.write_text("glpat-secret\n")
        self.credential = StaticFileCredential(
            str(self.token), HOST, header="PRIVATE-TOKEN", helper=HELPER
        )

    def fill(self, host, ambient=True):
        config = []
        if ambient:
            # Another forge's CLI installed this for every host; the layer the
            # broker adds must keep git from asking it.
            config += ["-c", "credential.helper=!f() { echo password=ambient-write-token; }; f"]
        for key, value in self.credential.git_config("acme/infra"):
            config += ["-c", f"{key}={value}"]
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(self.dir),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CEILING_DIRECTORIES": str(self.dir.parent),
        }
        return subprocess.run(
            ["git", *config, "credential", "fill"],
            input=f"protocol=https\nhost={host}\n\n",
            capture_output=True,
            text=True,
            env=env,
            cwd=self.dir,
            timeout=30,
        )

    def test_this_hosts_git_gets_the_token_from_the_file(self):
        done = self.fill(HOST)
        self.assertEqual(0, done.returncode, done.stderr)
        self.assertIn("password=glpat-secret", done.stdout)
        self.assertIn("username=oauth2", done.stdout)
        self.assertNotIn("ambient-write-token", done.stdout)

    def test_a_rotated_file_is_the_next_invocations_token(self):
        self.fill(HOST)
        self.token.write_text("glpat-rotated\n")
        self.assertIn("password=glpat-rotated", self.fill(HOST).stdout)

    def test_another_host_is_neither_this_token_nor_the_ambient_one(self):
        done = self.fill("github.com")
        self.assertNotIn("glpat-secret", done.stdout)
        self.assertNotIn("ambient-write-token", done.stdout)

    def test_a_token_the_api_side_refuses_gives_git_nothing_either(self):
        # Review round 2: the helper still passed a carriage return (and a
        # byte-order mark) that `_token()` refuses, splitting the two faces.
        for content in ("glpat-a\rx\n", "\ufeffglpat-a\n"):
            with self.subTest(content=content):
                self.token.write_text(content, encoding="utf-8")
                done = self.fill(HOST, ambient=False)
                self.assertNotIn("password=", done.stdout)

    def test_a_missing_file_gives_git_nothing_rather_than_a_traceback(self):
        self.token.unlink()
        done = self.fill(HOST, ambient=False)
        self.assertNotIn("password=", done.stdout)
        self.assertNotIn("Traceback", done.stderr)


class HelperTest(unittest.TestCase):
    def test_only_get_answers(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as handle:
            handle.write("t")
        self.addCleanup(os.unlink, handle.name)
        for verb in ("store", "erase"):
            with self.subTest(verb=verb):
                done = subprocess.run(
                    [HELPER, handle.name, "oauth2", verb],
                    input="protocol=https\nhost=x\n\n",
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                self.assertEqual(0, done.returncode)
                self.assertEqual("", done.stdout)


if __name__ == "__main__":
    unittest.main()
