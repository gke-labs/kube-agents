"""hack/ci-deploy.sh gives the agent the leased project's own Gemini key under MODEL_PROVIDER=gemini.

The eval nightlies export `MODEL_PROVIDER=gemini` and read the key that
`scripts/provision_ci_pool_gemini_key.sh` keeps in each pool project's Secret
Manager, so no two leased projects draw from one per-minute quota window. The
job env's `GEMINI_API_KEY` stays the judge's key and the `vertex_ai` path's
credential. The functional cases run `resolve_agent_gemini_api_key`, cut out
of the script between its marker comments, against a fake `gcloud` on PATH.
"""

import json
import os
import pathlib
import stat
import subprocess
import tempfile
import unittest

from tests.testing.common import get_isolated_test_env

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CI_DEPLOY = _REPO_ROOT / "hack" / "ci-deploy.sh"

# Literals on purpose: a test that read them back from the script would assert nothing.
_SECRET = "kube-agents-eval-gemini-api-key"
_PROJECT = "kube-agents-evals-99"
_JOB_KEY = "job-env-judge-key"
_REPAIR_SCRIPT = "provision_ci_pool_gemini_key.sh"
_BEGIN_MARKER = "# --- agent key: begin"
_END_MARKER = "# --- agent key: end"
_CONSTANT_LINE = 'readonly EVAL_AGENT_GEMINI_KEY_SECRET="${EVAL_AGENT_GEMINI_KEY_SECRET:-kube-agents-eval-gemini-api-key}"'
_PROVIDER_CONSTANT_LINE = 'readonly MODEL_PROVIDER_GEMINI="gemini"'

# Logs argv as one JSON line per call; prints FAKE_GCLOUD_STDOUT and exits FAKE_GCLOUD_EXIT.
_FAKE_GCLOUD = r"""#!/usr/bin/env python3
import json, os, sys
with open(os.environ["FAKE_GCLOUD_LOG"], "a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\n")
sys.stdout.write(os.environ.get("FAKE_GCLOUD_STDOUT", ""))
sys.exit(int(os.environ.get("FAKE_GCLOUD_EXIT", "0")))
"""

# Runs the extracted function, then prints the resolved key on a marked line.
_HARNESS = """set -euo pipefail
{constant}
{provider_constant}
eval "$(sed -n '/^{begin}/,/^{end}/p' "$CI_DEPLOY")"
resolve_agent_gemini_api_key
printf 'RESOLVED=[%s]\\n' "${{AGENT_GEMINI_API_KEY}}"
"""


class CiDeployGeminiKeyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.text = _CI_DEPLOY.read_text()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        tmp = pathlib.Path(self.tmp.name)
        self.bin_dir = tmp / "bin"
        self.bin_dir.mkdir()
        gcloud = self.bin_dir / "gcloud"
        gcloud.write_text(_FAKE_GCLOUD)
        gcloud.chmod(gcloud.stat().st_mode | stat.S_IXUSR)
        self.log = tmp / "gcloud.log"
        self.log.touch()

    def _run(self, provider: str, stdout: str = "", exit_code: int = 0) -> subprocess.CompletedProcess:
        harness = _HARNESS.format(
            constant=_CONSTANT_LINE, provider_constant=_PROVIDER_CONSTANT_LINE, begin=_BEGIN_MARKER, end=_END_MARKER
        )
        env = get_isolated_test_env(
            overrides={
                "CI_DEPLOY": str(_CI_DEPLOY),
                "MODEL_PROVIDER": provider,
                "PROJECT_ID": _PROJECT,
                "GEMINI_API_KEY": _JOB_KEY,
                "FAKE_GCLOUD_LOG": str(self.log),
                "FAKE_GCLOUD_STDOUT": stdout,
                "FAKE_GCLOUD_EXIT": str(exit_code),
            },
            bin_dir=str(self.bin_dir),
            absent=("EVAL_AGENT_GEMINI_KEY_SECRET", "AGENT_GEMINI_API_KEY"),
        )
        return subprocess.run(["bash", "-c", harness], capture_output=True, text=True, env=env)

    def _calls(self) -> list:
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def test_the_secret_name_is_a_named_constant_with_an_env_override(self) -> None:
        self.assertTrue(_CONSTANT_LINE in self.text, f"hack/ci-deploy.sh lacks: {_CONSTANT_LINE}")

    def test_the_gemini_provider_is_a_named_constant(self) -> None:
        self.assertTrue(_PROVIDER_CONSTANT_LINE in self.text, f"hack/ci-deploy.sh lacks: {_PROVIDER_CONSTANT_LINE}")
        block = self.text[self.text.index(_BEGIN_MARKER) : self.text.index(_END_MARKER)]
        self.assertNotIn('"gemini"', block)

    def test_the_helm_credential_line_passes_the_resolved_key(self) -> None:
        self.assertTrue(
            '--set-string "platformAgent.credentials.data.GEMINI_API_KEY=${AGENT_GEMINI_API_KEY}"' in self.text,
            "the Helm release must pass the key resolve_agent_gemini_api_key chose",
        )
        self.assertFalse(
            '--set-string "platformAgent.credentials.data.GEMINI_API_KEY=${GEMINI_API_KEY}"' in self.text,
            "the agent's credential must come from resolve_agent_gemini_api_key, not the judge's job-env key",
        )

    def test_vertex_ai_never_reads_secret_manager_and_uses_the_job_env_key(self) -> None:
        proc = self._run("vertex_ai", stdout="should-not-be-read\n")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self._calls(), [])
        self.assertIn(f"RESOLVED=[{_JOB_KEY}]", proc.stdout)

    def test_gemini_reads_the_leased_projects_secret_and_trims_the_newline(self) -> None:
        proc = self._run("gemini", stdout="k9\n")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        calls = self._calls()
        self.assertEqual(len(calls), 1, calls)
        argv = calls[0]
        self.assertEqual(argv[:4], ["secrets", "versions", "access", "latest"])
        self.assertIn(f"--secret={_SECRET}", argv)
        self.assertIn(f"--project={_PROJECT}", argv)
        self.assertIn("RESOLVED=[k9]", proc.stdout)
        self.assertNotIn(_JOB_KEY, proc.stdout)
        self.assertNotIn("k9", proc.stderr, "the key must not be printed")

    def test_gemini_with_an_unreadable_secret_fails_before_helm_and_names_the_repair(self) -> None:
        proc = self._run("gemini", exit_code=1)
        self.assertEqual(proc.returncode, 1)
        self.assertNotIn("RESOLVED=", proc.stdout)
        self.assertIn(_REPAIR_SCRIPT, proc.stderr)
        self.assertIn(_PROJECT, proc.stderr)
        self.assertIn(_SECRET, proc.stderr)

    def test_gemini_with_an_empty_secret_fails(self) -> None:
        proc = self._run("gemini", stdout="\n")
        self.assertEqual(proc.returncode, 1)
        self.assertNotIn("RESOLVED=", proc.stdout)
        self.assertIn(_REPAIR_SCRIPT, proc.stderr)
        self.assertIn(_PROJECT, proc.stderr)
        self.assertIn(_SECRET, proc.stderr)

    def test_gemini_with_a_whitespace_only_secret_fails(self) -> None:
        proc = self._run("gemini", stdout="  \n")
        self.assertEqual(proc.returncode, 1)
        self.assertNotIn("RESOLVED=", proc.stdout)
        self.assertIn(_REPAIR_SCRIPT, proc.stderr)
        self.assertIn(_PROJECT, proc.stderr)
        self.assertIn(_SECRET, proc.stderr)

    def test_gemini_strips_a_crlf_line_ending_from_the_key(self) -> None:
        proc = self._run("gemini", stdout="k9\r\n")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("RESOLVED=[k9]", proc.stdout)

    def test_the_rationale_comment_names_the_nightlies_as_the_gemini_path(self) -> None:
        head = self.text[: self.text.index('export MODEL_PROVIDER="${MODEL_PROVIDER:-vertex_ai}"')]
        comment = head[head.rindex("\n\n") :]
        self.assertIn("nightlies", comment)
        self.assertIn("presubmit", comment)
        self.assertIn("429", comment)
        self.assertIn("own key", comment)


if __name__ == "__main__":
    unittest.main()
