"""The eval job exports the run's GitOps repository under the name the diff check reads.

`pull_request_diff_contains` binds the pull request a reply names to the
repository the run's agent writes to when `BENCH_GITOPS_REPO` is set;
`hack/ci-eval-pr.sh` sets it with the deploy's precedence, a developer's
`EVAL_GITOPS_REPO` first and then the project mapping the deploy and the
ledger reset use, right after that mapping is resolved for the lease. The
export is lifted out of the shipped script and run under bash with the
resolver stubbed, and the variable's name is pinned to the verifier's
constant, so neither side can rename it alone.
"""

import pathlib
import re
import subprocess
import unittest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CI_EVAL = _REPO_ROOT / "hack" / "ci-eval-pr.sh"
_VERIFIERS = _REPO_ROOT / "bench" / "kube_agents_bench" / "verifiers.py"
_MAPPED = "gke-agentic/kube-agents-evals-21-infra"
_OVERRIDE = "someone/their-own-infra"


def lifted_block() -> str:
    src = _CI_EVAL.read_text(encoding="utf-8")
    match = re.search(r'^EVAL_LEDGER_REPO="\$\(eval_gitops_repo.*?^export BENCH_GITOPS_REPO=[^\n]*$', src, re.DOTALL | re.MULTILINE)
    if match is None:  # pragma: no cover - a reshape should say so loudly
        raise AssertionError(f"the BENCH_GITOPS_REPO export block was not found in {_CI_EVAL}")
    return match.group(0)


def run_block(mapped: bool, developer_override: str = "") -> subprocess.CompletedProcess:
    resolver = f'eval_gitops_repo() {{ echo "{_MAPPED}"; }}' if mapped else "eval_gitops_repo() { return 1; }"
    override = f'EVAL_GITOPS_REPO="{developer_override}"' if developer_override else "unset EVAL_GITOPS_REPO"
    script = f"""set -euo pipefail
unset BENCH_GITOPS_REPO
PROJECT_ID="kube-agents-evals-21"
{override}
{resolver}
{lifted_block()}
echo "REPO=${{BENCH_GITOPS_REPO-<unset>}}"
"""
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=False)


class GitOpsRepoExportTest(unittest.TestCase):
    def test_a_mapped_project_exports_its_repository(self) -> None:
        result = run_block(mapped=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"REPO={_MAPPED}", result.stdout)

    def test_an_unmapped_project_exports_an_empty_value_and_the_run_goes_on(self) -> None:
        result = run_block(mapped=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("REPO=", result.stdout)
        self.assertNotIn("REPO=<unset>", result.stdout)

    def test_a_developers_override_wins_as_it_does_for_the_deploy(self) -> None:
        result = run_block(mapped=True, developer_override=_OVERRIDE)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"REPO={_OVERRIDE}", result.stdout)
        self.assertNotIn(_MAPPED, result.stdout)

    def test_the_name_is_the_verifiers(self) -> None:
        verifiers = _VERIFIERS.read_text(encoding="utf-8")
        match = re.search(r'^_GITOPS_REPO_ENV_VAR = "([A-Z_]+)"$', verifiers, re.MULTILINE)
        self.assertIsNotNone(match)
        self.assertIn(f"export {match.group(1)}=", lifted_block())

    def test_the_export_follows_the_lease_time_resolution(self) -> None:
        src = _CI_EVAL.read_text(encoding="utf-8")
        self.assertLess(src.index('EVAL_LEDGER_REPO="$(eval_gitops_repo'), src.index("export BENCH_GITOPS_REPO="))
        self.assertLess(src.index("export BENCH_GITOPS_REPO="), src.index('reset_audit_ledgers "lease"'))


if __name__ == "__main__":
    unittest.main()
