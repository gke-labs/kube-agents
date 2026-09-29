"""The GitOps-repository step of scripts/provision_ci_pool_project.sh, executed.

A new repository has no commits, and nothing downstream can make the first one,
so the step has to. The condition that decides it is one `gh repo view` field,
and a wrong jq path fails in one of two silent directions: never seeding (a new
project sits empty, the incident this guards against) or always seeding (a PUT
without `sha` on a repository that already has the file, which aborts every
re-run). The section is lifted from the script by its markers and run against a
fake `gh` on PATH, so these assertions are against the code that ships.
"""

import base64
import json
import os
import pathlib
import re
import stat
import subprocess
import tempfile
import unittest

from tests.testing.common import get_isolated_test_env

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SCRIPT = _REPO_ROOT / "scripts" / "provision_ci_pool_project.sh"
_SECTION_START = "# ─── GitOps Repo & App Installation Check"
_SECTION_END_RE = re.compile(r"^# ─── (?!GitOps Repo)", re.MULTILINE)
_CONSTANT_RE = re.compile(r"^readonly GITOPS_SEED_\w+=.*$", re.MULTILINE)

# Literals on purpose: the section under test defines the same values, and a
# test that read them back from it would assert nothing.
_SEED_FILE = "README.md"
_SEED_MESSAGE = "Initial commit"
_SEED_CONTENT = "# GitOps Infrastructure Repo\n"

_FAKE_GH = r"""#!/usr/bin/env bash
# Records every call, one JSON array per line, and answers what the case set.
printf '%s\n' "$(printf '%s\n' "$@" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read().split("\n")[:-1]))')" >> "${FAKE_GH_LOG}"
case "$1 $2" in
  "repo view")
    if [ "${3:-}" != "--json" ] && [ "${4:-}" = "--json" ]; then
      # gh repo view <repo> --json defaultBranchRef --jq <filter>. The filter is
      # honoured the way gh does for the two shapes that matter: `.name` off
      # the ref prints the name or nothing, anything else prints the JSON value,
      # which for an empty repository is the word null, not an empty line.
      [ "${FAKE_VIEW_FAILS:-0}" = "1" ] && { echo "gh: HTTP 502" >&2; exit 1; }
      filter=""; while [ $# -gt 0 ]; do [ "$1" = "--jq" ] && filter="${2:-}"; shift; done
      if [ "${filter}" = ".defaultBranchRef.name" ]; then
        printf '%s\n' "${FAKE_DEFAULT_BRANCH}"
      elif [ -z "${FAKE_DEFAULT_BRANCH}" ]; then
        echo null
      else
        printf '{"name":"%s"}\n' "${FAKE_DEFAULT_BRANCH}"
      fi
    fi
    exit 0 ;;
  "repo create") exit 0 ;;
  "api /orgs/gke-agentic/installations") exit 0 ;;
  "api -X") echo '{}'; exit 0 ;;
esac
echo "fake gh: unexpected call: $*" >&2
exit 97
"""


def _section() -> str:
    text = _SCRIPT.read_text(encoding="utf-8")
    start = text.find(_SECTION_START)
    assert start != -1, f"{_SECTION_START!r} not found in {_SCRIPT}"
    end = _SECTION_END_RE.search(text, start + len(_SECTION_START))
    assert end, "no section marker follows the GitOps repository block"
    constants = "\n".join(_CONSTANT_RE.findall(text))
    assert constants.count("\n") == 2, "expected the three GITOPS_SEED_* constants"
    return constants + "\n" + text[start : end.start()]


class ProvisionGitopsRepoSeedTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        bindir = pathlib.Path(self.tmp.name) / "bin"
        bindir.mkdir()
        fake = bindir / "gh"
        fake.write_text(_FAKE_GH)
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        self.bindir = bindir
        self.log = pathlib.Path(self.tmp.name) / "gh.log"

    def _run(self, default_branch: str, view_fails: bool = False):
        env = get_isolated_test_env(
            overrides={
                "PATH": f"{self.bindir}{os.pathsep}{os.environ.get('PATH', '')}",
                "FAKE_GH_LOG": str(self.log),
                "FAKE_DEFAULT_BRANCH": default_branch,
                "FAKE_VIEW_FAILS": "1" if view_fails else "0",
                "PROJECT_ID": "kube-agents-evals-99",
                "GITOPS_REPO": "gke-agentic/kube-agents-evals-99-infra",
                "APP_ID": "4675512",
                "LEDGER_APP_ID": "4739812",
                "LEDGER_INSTALLATION_ID": "1",
            }
        )
        proc = subprocess.run(
            ["bash", "-c", "set -euo pipefail\n" + _section()],
            capture_output=True,
            text=True,
            env=env,
        )
        calls = [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []
        puts = [c for c in calls if c[:2] == ["api", "-X"]]
        return proc, puts

    def test_an_empty_repository_gets_exactly_the_seed_commit(self):
        proc, puts = self._run(default_branch="")
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertIn("Seeding gke-agentic/kube-agents-evals-99-infra", proc.stdout)
        self.assertEqual(1, len(puts), puts)
        argv = puts[0]
        self.assertEqual(
            ["api", "-X", "PUT", f"repos/gke-agentic/kube-agents-evals-99-infra/contents/{_SEED_FILE}"],
            argv[:4],
        )
        fields = dict(argv[i + 1].split("=", 1) for i in range(len(argv)) if argv[i] == "-f")
        self.assertEqual(_SEED_MESSAGE, fields["message"])
        self.assertEqual(_SEED_CONTENT, base64.b64decode(fields["content"]).decode())

    def test_a_repository_with_a_branch_is_left_alone(self):
        proc, puts = self._run(default_branch="main")
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertNotIn("Seeding", proc.stdout)
        self.assertEqual([], puts)

    def test_a_failed_read_stops_the_step_rather_than_seeding_blind(self):
        proc, puts = self._run(default_branch="", view_fails=True)
        self.assertNotEqual(0, proc.returncode)
        self.assertIn("not seeding it blind", proc.stderr)
        self.assertEqual([], puts)

    def test_the_script_still_parses(self):
        subprocess.run(["bash", "-n", str(_SCRIPT)], check=True)


if __name__ == "__main__":
    unittest.main()
