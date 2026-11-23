"""Tests for scripts/release/open_upgrade_relabel_pr.sh, on a fixture repository with a bare origin.

`gh` and `npx` are stubs that record their arguments, so nothing reaches GitHub or npm.

    python3 -m unittest tests.test_open_upgrade_relabel_pr
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from check_pr_template import sections as template_sections  # noqa: E402
from test_check_upgrade_table_releases import PAGE, Repo, row  # noqa: E402

COPIED = (
    "scripts/release/open_upgrade_relabel_pr.sh",
    "scripts/release/common.sh",
    "scripts/installer/gke_dns_endpoint.sh",  # common.sh sources it
    "scripts/relabel_upgrade_table_pending.py",
    "scripts/check_upgrade_table_releases.py",
)
SCRIPT = "scripts/release/open_upgrade_relabel_pr.sh"
BRANCH = "relabel-upgrade-table/pending"
ISOLATED_ENV_VARS = ("GIT_USER_NAME", "GIT_USER_EMAIL", "STUB_OPEN_PR")
# The template the review bot holds every description to. The body the script writes is read
# against the repository's own copy, so a section added there fails here until the script answers it.
TEMPLATE = ROOT / ".github" / "PULL_REQUEST_TEMPLATE.md"
# A heading line as the description writes it, at the level the template gives it.
HEADING_RE = re.compile(r"^(#{2,6}) (.+)$")
UX_BOX_TICKED = "- [x] No user-visible change"
BODY_FLAG = " --body "
# How a line the stubs logged for a call starts; a body line starts with neither.
STUBBED_CALL_PREFIXES = ("gh ", "npx ")
NOT_LIVE_TESTED_PREFIX = "Not live-tested: "
LIVE_VALIDATION_HEADING = "### Live validation"
STUB = """#!/bin/sh
echo "$(basename "$0") $*" >> "{log}"
if [ "$(basename "$0")" = npx ]; then echo "npx GH_TOKEN=${{GH_TOKEN:-unset}}" >> "{log}"; fi
if [ "$(basename "$0")" = gh ] && [ "$1 $2" = "pr list" ]; then printf '%s' "${{STUB_OPEN_PR:-}}"; fi
exit 0
"""


class OpenUpgradeRelabelPrTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.origin = base / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", str(self.origin)], check=True)
        (base / "work").mkdir()
        self.repo = Repo(base / "work")
        for path in COPIED:
            target = self.repo.root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / path, target)
        self.repo.commit("start", row=row("0.9.0", "an old change"))
        self.repo.tag("0.9.0")
        self.repo.git("remote", "add", "origin", str(self.origin))
        self.bin = base / "bin"
        self.bin.mkdir()
        self.log = base / "calls.log"
        self.log.touch()
        for tool in ("gh", "npx"):
            stub = self.bin / tool
            stub.write_text(STUB.format(log=self.log), encoding="utf-8")
            stub.chmod(0o755)

    def run_script(self, version: str = "1.0.0", **env: str) -> subprocess.CompletedProcess:
        clean = {k: v for k, v in os.environ.items() if not k.startswith("GITHUB_") and k not in ISOLATED_ENV_VARS}
        clean.update(PATH=f"{self.bin}{os.pathsep}{os.environ['PATH']}", RELEASE_VERSION=version, **env)
        return subprocess.run(["bash", SCRIPT], cwd=self.repo.root, capture_output=True, text=True, env=clean)

    def calls(self) -> list[str]:
        return self.log.read_text(encoding="utf-8").splitlines()

    def origin_has(self, ref: str) -> bool:
        return subprocess.run(["git", "--git-dir", str(self.origin), "rev-parse", "--verify", "-q", ref],
                              capture_output=True).returncode == 0

    def origin_commit_identity(self) -> list[str]:
        return subprocess.run(
            ["git", "--git-dir", str(self.origin), "log", "-1", "--format=%an%n%ae%n%cn%n%ce", f"refs/heads/{BRANCH}"],
            check=True, capture_output=True, text=True,
        ).stdout.splitlines()

    def test_a_shipped_pending_row_is_pushed_and_a_pull_request_opened(self):
        self.repo.commit("feat: shipped", row=row("pending", "a shipped change"))
        self.repo.tag("1.0.0")
        self.repo.commit("feat: not yet", row=row("pending", "an unshipped change"))
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.origin_has(f"refs/heads/{BRANCH}"))
        pushed = subprocess.run(["git", "--git-dir", str(self.origin), "show", f"{BRANCH}:{PAGE}"],
                                check=True, capture_output=True, text=True).stdout
        self.assertIn("| 1.0.0   | a shipped change | x |", pushed)
        self.assertIn("| pending | an unshipped change | x |", pushed)
        self.assertEqual(
            self.origin_commit_identity(),
            [
                "kube-agents-release-bot[bot]",
                "324943056+kube-agents-release-bot[bot]@users.noreply.github.com",
                "kube-agents-release-bot[bot]",
                "324943056+kube-agents-release-bot[bot]@users.noreply.github.com",
            ],
        )
        create = [c for c in self.calls() if c.startswith("gh pr create")]
        self.assertEqual(len(create), 1, self.calls())
        self.assertIn("--base main", create[0])
        self.assertIn(f"--head {BRANCH}", create[0])
        self.assertIn("relabel the pending rows 1.0.0 shipped", create[0])
        self.assertNotIn("fixing main's Documentation Checks", create[0])
        self.assertIn("`a shipped change` → 1.0.0", self.log.read_text(encoding="utf-8"))
        self.assertTrue(any(c.startswith("npx --yes prettier@3.9.6 --write") for c in self.calls()))

    def body_given_to(self, command: str) -> list[str]:
        """The body lines `command` was handed, up to the next stubbed call or the log's end."""
        lines = self.calls()
        start = next(i for i, line in enumerate(lines) if line.startswith(command))
        rest = lines[start + 1:]
        end = next((i for i, line in enumerate(rest) if line.startswith(STUBBED_CALL_PREFIXES)), len(rest))
        # The stub logs its arguments on one line, so the body's first line follows the flag.
        return [lines[start].split(BODY_FLAG, 1)[1], *rest[:end]]

    def opened_body(self) -> list[str]:
        return self.body_given_to("gh pr create")

    def test_the_body_answers_every_section_of_the_pull_request_template(self):
        self.repo.commit("feat: shipped", row=row("pending", "a shipped change"))
        self.repo.tag("1.0.0")
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        body = self.opened_body()
        # The parser `make docs-check-pr-template` uses, so the two cannot read the template differently.
        wanted = [heading for heading, _ in template_sections(TEMPLATE.read_text(encoding="utf-8"))]
        self.assertTrue(wanted, "the template has no sections to hold the body to")
        written = {m.group(2): i for i, line in enumerate(body) if (m := HEADING_RE.match(line))}
        for heading in wanted:
            with self.subTest(heading=heading):
                self.assertIn(heading, written)
                # An empty heading is not an answer: the next line that is not blank is text,
                # not the next heading.
                after = [line for line in body[written[heading] + 1:] if line.strip()]
                self.assertTrue(after and not HEADING_RE.match(after[0]), f"{heading} is left empty")

    def test_the_body_says_the_page_was_not_live_tested_and_ticks_the_ux_box(self):
        self.repo.commit("feat: shipped", row=row("pending", "a shipped change"))
        self.repo.tag("1.0.0")
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        body = self.opened_body()
        self.assertIn(UX_BOX_TICKED, body)
        after_live = [line for line in body[body.index(LIVE_VALIDATION_HEADING) + 1:] if line.strip()]
        self.assertTrue(after_live[0].startswith(NOT_LIVE_TESTED_PREFIX), after_live[0])

    def test_a_later_release_rewrites_the_open_pull_requests_title_and_description(self):
        self.repo.commit("feat: first", row=row("pending", "the first change"))
        self.repo.tag("1.0.0")
        self.assertEqual(self.run_script("1.0.0").returncode, 0)
        self.repo.commit("feat: second", row=row("pending", "the second change"))
        self.repo.tag("1.1.0")
        self.log.write_text("", encoding="utf-8")
        result = self.run_script("1.1.0", STUB_OPEN_PR="42")
        self.assertEqual(result.returncode, 0, result.stderr)
        edit = [c for c in self.calls() if c.startswith("gh pr edit")]
        self.assertEqual(len(edit), 1, self.calls())
        self.assertIn("gh pr edit 42 ", edit[0])
        self.assertIn("relabel the pending rows 1.0.0, 1.1.0 shipped", edit[0])
        body = self.body_given_to("gh pr edit")
        self.assertIn("- `the first change` → 1.0.0", body)
        self.assertIn("- `the second change` → 1.1.0", body)
        self.assertTrue(any("Written by the release workflow after it tagged 1.1.0" in line for line in body), body)
        self.assertFalse(any(c.startswith("gh pr create") for c in self.calls()))

    def test_prettier_from_npm_never_sees_the_token(self):
        self.repo.commit("feat: shipped", row=row("pending", "a shipped change"))
        self.repo.tag("1.0.0")
        result = self.run_script(GH_TOKEN="a-test-token")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("npx GH_TOKEN=unset", self.calls())
        self.assertNotIn("a-test-token", self.log.read_text(encoding="utf-8"))

    def test_nothing_to_relabel_pushes_nothing_and_opens_nothing(self):
        self.repo.commit("feat: not yet", row=row("pending", "an unshipped change"))
        self.repo.tag("1.0.0", "HEAD~1")
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("nothing to relabel", result.stdout)
        self.assertFalse(self.origin_has(f"refs/heads/{BRANCH}"))
        self.assertEqual(self.calls(), [])

    def test_a_re_run_updates_the_open_pull_request_rather_than_opening_another(self):
        self.repo.commit("feat: shipped", row=row("pending", "a shipped change"))
        self.repo.tag("1.0.0")
        result = self.run_script(STUB_OPEN_PR="4242")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("#4242", result.stdout)
        self.assertTrue(self.origin_has(f"refs/heads/{BRANCH}"))
        self.assertFalse(any(c.startswith("gh pr create") for c in self.calls()))

    def test_a_later_release_names_each_rows_own_release_and_reuses_the_branch(self):
        """A row 1.0.0 shipped, still pending because its relabel has not merged, keeps 1.0.0 when 1.1.0's
        run relabels it with a row 1.1.0 shipped, and the run reuses the one branch."""
        self.repo.commit("feat: first", row=row("pending", "a first change"))
        self.repo.tag("1.0.0")
        self.repo.commit("feat: second", row=row("pending", "a second change"))
        self.repo.tag("1.1.0")
        result = self.run_script(version="1.1.0")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.origin_has(f"refs/heads/{BRANCH}"))
        create = [c for c in self.calls() if c.startswith("gh pr create")]
        self.assertIn("relabel the pending rows 1.0.0, 1.1.0 shipped", create[0])
        log = self.log.read_text(encoding="utf-8")
        self.assertIn("`a first change` → 1.0.0", log)
        self.assertIn("`a second change` → 1.1.0", log)

    def test_a_change_cell_with_a_code_span_is_fenced_with_two_backticks(self):
        self.repo.commit("feat: code", row=row("pending", "the `spec.mode` field moves"))
        self.repo.tag("1.0.0")
        self.run_script()
        self.assertIn("- `` the `spec.mode` field moves `` → 1.0.0", self.log.read_text(encoding="utf-8"))

    def test_a_version_that_is_not_a_release_number_is_refused(self):
        result = self.run_script(version="v1.0")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.calls(), [])

    def test_commit_identity_ignores_caller_git_env_and_honours_git_user_overrides(self):
        self.repo.commit("feat: shipped", row=row("pending", "a shipped change"))
        self.repo.tag("1.0.0")
        self.repo.git("config", "author.name", "wrong-config-author")
        self.repo.git("config", "committer.email", "wrong-config@example.invalid")
        result = self.run_script(
            GIT_AUTHOR_NAME="wrong-author",
            GIT_AUTHOR_EMAIL="wrong-author@example.invalid",
            GIT_COMMITTER_NAME="wrong-committer",
            GIT_COMMITTER_EMAIL="wrong-committer@example.invalid",
            GIT_USER_NAME="custom-bot[bot]",
            GIT_USER_EMAIL="12345+custom-bot[bot]@users.noreply.github.com",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.origin_commit_identity(),
            [
                "custom-bot[bot]",
                "12345+custom-bot[bot]@users.noreply.github.com",
                "custom-bot[bot]",
                "12345+custom-bot[bot]@users.noreply.github.com",
            ],
        )


if __name__ == "__main__":
    unittest.main()
