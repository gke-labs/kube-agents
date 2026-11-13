"""Tests for scripts/release/open_upgrade_relabel_pr.sh, on a fixture repository with a bare origin.

`gh` and `npx` are stubs that record their arguments, so nothing reaches GitHub or npm.

    python3 -m unittest tests.test_open_upgrade_relabel_pr
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from test_check_upgrade_table_releases import PAGE, Repo, row  # noqa: E402

COPIED = (
    "scripts/release/open_upgrade_relabel_pr.sh",
    "scripts/release/common.sh",
    "scripts/installer/gke_dns_endpoint.sh",  # common.sh sources it
    "scripts/relabel_upgrade_table_pending.py",
    "scripts/check_upgrade_table_releases.py",
)
SCRIPT = "scripts/release/open_upgrade_relabel_pr.sh"
BRANCH = "relabel-upgrade-table/1.0.0"
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
        clean = {k: v for k, v in os.environ.items() if not k.startswith("GITHUB_")}
        clean.update(PATH=f"{self.bin}{os.pathsep}{os.environ['PATH']}", RELEASE_VERSION=version, **env)
        return subprocess.run(["bash", SCRIPT], cwd=self.repo.root, capture_output=True, text=True, env=clean)

    def calls(self) -> list[str]:
        return self.log.read_text(encoding="utf-8").splitlines()

    def origin_has(self, ref: str) -> bool:
        return subprocess.run(["git", "--git-dir", str(self.origin), "rev-parse", "--verify", "-q", ref],
                              capture_output=True).returncode == 0

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
        create = [c for c in self.calls() if c.startswith("gh pr create")]
        self.assertEqual(len(create), 1, self.calls())
        self.assertIn("--base main", create[0])
        self.assertIn(f"--head {BRANCH}", create[0])
        self.assertIn("relabel the rows 1.0.0 shipped, fixing main's Documentation Checks", create[0])
        self.assertIn("`a shipped change` → 1.0.0", self.log.read_text(encoding="utf-8"))
        self.assertTrue(any(c.startswith("npx --yes prettier@3.9.6 --write") for c in self.calls()))

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

    def test_a_version_that_is_not_a_release_number_is_refused(self):
        result = self.run_script(version="v1.0")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.calls(), [])


if __name__ == "__main__":
    unittest.main()
