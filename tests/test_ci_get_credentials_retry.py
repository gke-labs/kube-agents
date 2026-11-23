"""The CI scripts' cluster auth step survives a 503 from the GKE API.

`hack/ci-env.sh`'s `get_cluster_credentials` wraps
`gcloud container clusters get-credentials` in a bounded retry. A single
"ResponseError: code=503, message=The service is currently unavailable."
used to fail a presubmit run at its auth step, before any build or test.

These tests source the real `hack/ci-env.sh` under bash with `gcloud` and
`sleep` stand-ins on PATH, and pin:

* a call that succeeds costs one gcloud call and no wait;
* a 503 is retried, with a delay that grows per attempt, and the step then passes;
* a 503 on every attempt stops at the attempt limit with gcloud's exit status;
* any other failure returns on the first attempt with gcloud's exit status;
* gcloud's stderr is printed for every attempt;
* every attempt chooses the endpoint again, so an endpoint read that failed
  during the outage does not decide the kubeconfig;
* the host-cluster auth step of every CI script that sources ci-env.sh goes
  through the helper.
"""

import os
import pathlib
import re
import stat
import subprocess
import tempfile
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
HACK = REPO_ROOT / "hack"
ENV_SCRIPT = HACK / "ci-env.sh"
# Each script that sources ci-env.sh and authenticates to the host cluster,
# with the number of auth steps it has.
AUTH_SCRIPTS = {"ci-deploy.sh": 1, "ci-eval-pr.sh": 1, "ci-connection-test.sh": 1, "ci-teardown.sh": 1, "ci-forge.sh": 2}
# The helper call for the host cluster, without what the script does on failure.
AUTH_STEP_RE = re.compile(
    r'^\s*(get_cluster_credentials "\$\{?(?:HOST_)?CLUSTER_NAME\}?" "\$\{?REGION\}?" "\$\{?PROJECT_ID\}?")', re.MULTILINE
)
# A direct call for the host cluster: the single-shot form the helper replaces.
DIRECT_CALL_RE = re.compile(r'^\s*gcloud container clusters get-credentials "\$\{?(?:HOST_)?CLUSTER_NAME\}?"', re.MULTILINE)
HOST_CALL = "container clusters get-credentials platform-agent-host --region us-central1 --project kube-agents-evals --quiet"

UNAVAILABLE = "ERROR: (gcloud.container.clusters.get-credentials) ResponseError: code=503, message=The service is currently unavailable."
PERMISSION_DENIED = "ERROR: (gcloud.container.clusters.get-credentials) ResponseError: code=403, message=Permission denied on resource project."
PROGRESS = "Fetching cluster endpoint and auth data."

# Fails the first STUB_FAILURES get-credentials calls with STUB_ERROR and
# STUB_EXIT, then succeeds. For the endpoint helper it offers --dns-endpoint in
# `--help`, and its `describe` fails STUB_DESCRIBE_FAILURES times, then answers
# with a DNS endpoint that accepts external traffic.
GCLOUD_STUB = """#!/usr/bin/env bash
case "$*" in
  *"get-credentials --help"*) echo "--dns-endpoint"; exit 0 ;;
  *"container clusters describe"*)
    echo "$*" >> "${STUB_LOG}/describe.args"
    if [ "$(wc -l < "${STUB_LOG}/describe.args")" -le "${STUB_DESCRIBE_FAILURES:-0}" ]; then
      echo "ERROR: (gcloud.container.clusters.describe) ResponseError: code=503" >&2
      exit 1
    fi
    printf 'gke-abc.us-central1.gke.goog\\tTrue\\n'
    exit 0 ;;
  *"container clusters get-credentials"*) ;;
  *) exit 0 ;;
esac
echo "$*" >> "${STUB_LOG}/gcloud.args"
n=$(wc -l < "${STUB_LOG}/gcloud.args")
echo "Fetching cluster endpoint and auth data." >&2
if [ "$n" -le "${STUB_FAILURES:-0}" ]; then
  echo "${STUB_ERROR}" >&2
  exit "${STUB_EXIT:-1}"
fi
echo "kubeconfig entry generated for platform-agent-host."
"""

SLEEP_STUB = """#!/usr/bin/env bash
echo "$*" >> "${STUB_LOG}/sleep.args"
"""


def _constant(name):
    match = re.search(rf"^readonly {name}=(\d+)$", ENV_SCRIPT.read_text(encoding="utf-8"), re.MULTILINE)
    assert match is not None, f"readonly {name}=<number> not found in {ENV_SCRIPT}"
    return int(match.group(1))


ATTEMPTS = _constant("GKE_GET_CREDENTIALS_ATTEMPTS")
DELAY = _constant("GKE_GET_CREDENTIALS_RETRY_DELAY_SECONDS")


def _auth_steps(script):
    return AUTH_STEP_RE.findall((HACK / script).read_text(encoding="utf-8"))


class GetCredentialsRetryTest(unittest.TestCase):
    maxDiff = None

    def _run(self, body, failures=0, error=UNAVAILABLE, exit_code=1, describe_failures=0):
        """Run `body` under bash after sourcing ci-env.sh.

        Returns (completed process, gcloud get-credentials calls, sleep arguments).
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            bin_dir = tmp_path / "bin"
            bin_dir.mkdir()
            for name, text in (("gcloud", GCLOUD_STUB), ("sleep", SLEEP_STUB)):
                stub = bin_dir / name
                stub.write_text(text, encoding="utf-8")
                stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
            script = tmp_path / "run.sh"
            script.write_text(
                f'set -euo pipefail\nsource "{ENV_SCRIPT}"\n{body}\necho "STEP PASSED"\n',
                encoding="utf-8",
            )
            env = {
                "PATH": f"{bin_dir}:{os.environ['PATH']}",
                "HOME": tmp,
                "TMPDIR": tmp,
                "STUB_LOG": tmp,
                "STUB_FAILURES": str(failures),
                "STUB_ERROR": error,
                "STUB_EXIT": str(exit_code),
                "STUB_DESCRIBE_FAILURES": str(describe_failures),
            }
            proc = subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True, timeout=60, check=False)

            def lines(name):
                path = tmp_path / name
                return path.read_text(encoding="utf-8").splitlines() if path.exists() else []

            leftovers = sorted(p.name for p in tmp_path.iterdir() if p.name.startswith("tmp."))
            self.assertEqual(leftovers, [], "the helper left its stderr capture behind")
            return proc, lines("gcloud.args"), lines("sleep.args")

    def _helper(self, **kwargs):
        return self._run('get_cluster_credentials "host" "r1" "p1"', **kwargs)

    def test_a_call_that_succeeds_costs_one_call_and_no_wait(self):
        proc, calls, sleeps = self._helper()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(calls, ["container clusters get-credentials host --region r1 --project p1 --quiet --dns-endpoint"])
        self.assertEqual(sleeps, [])
        self.assertIn("kubeconfig entry generated", proc.stdout)
        self.assertIn(PROGRESS, proc.stderr)
        self.assertNotIn("WARNING", proc.stderr)

    def test_one_503_is_retried_and_the_step_passes(self):
        proc, calls, sleeps = self._helper(failures=1)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(set(calls)), 1, "the retry changed the arguments")
        self.assertEqual(sleeps, [str(DELAY)])
        self.assertIn("STEP PASSED", proc.stdout)
        self.assertIn(UNAVAILABLE, proc.stderr)
        self.assertIn(f"attempt 1 of {ATTEMPTS}", proc.stderr)

    def test_the_delay_grows_by_one_step_per_attempt(self):
        proc, calls, sleeps = self._helper(failures=ATTEMPTS - 1)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len(calls), ATTEMPTS)
        self.assertEqual(sleeps, [str(DELAY * n) for n in range(1, ATTEMPTS)])

    def test_the_unavailable_status_name_is_retried(self):
        proc, calls, _ = self._helper(failures=1, error="ERROR: (gcloud.container.clusters.get-credentials) UNAVAILABLE: try again")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len(calls), 2)

    def test_a_503_on_every_attempt_stops_at_the_limit_with_gclouds_status(self):
        proc, calls, sleeps = self._helper(failures=ATTEMPTS + 5, exit_code=7)
        self.assertEqual(proc.returncode, 7, proc.stderr)
        self.assertEqual(len(calls), ATTEMPTS)
        self.assertEqual(len(sleeps), ATTEMPTS - 1, "the helper waited after its last attempt")
        self.assertNotIn("STEP PASSED", proc.stdout)
        self.assertEqual(proc.stderr.count(UNAVAILABLE), ATTEMPTS, "stderr of an attempt was dropped")
        self.assertIn(f"all {ATTEMPTS} attempts", proc.stderr)

    def test_any_other_failure_returns_on_the_first_attempt(self):
        proc, calls, sleeps = self._helper(failures=ATTEMPTS + 5, error=PERMISSION_DENIED, exit_code=3)
        self.assertEqual(proc.returncode, 3, proc.stderr)
        self.assertEqual(len(calls), 1)
        self.assertEqual(sleeps, [])
        self.assertIn(PERMISSION_DENIED, proc.stderr)
        self.assertNotIn("WARNING", proc.stderr)
        self.assertNotIn("STEP PASSED", proc.stdout)

    def test_a_caller_can_handle_the_last_failure_itself(self):
        # ci-teardown.sh ends its auth step in `|| { ...; exit 1; }`.
        proc, calls, _ = self._run(
            'get_cluster_credentials "host" "r1" "p1" || { echo "HANDLED $?"; exit 9; }',
            failures=ATTEMPTS + 5,
            exit_code=7,
        )
        self.assertEqual(proc.returncode, 9, proc.stderr)
        self.assertEqual(len(calls), ATTEMPTS)
        self.assertIn("HANDLED 7", proc.stdout)

    def test_an_endpoint_read_that_failed_in_the_outage_is_made_again(self):
        # The first attempt runs with no endpoint flag: its describe got the 503 too.
        proc, calls, _ = self._helper(failures=1, describe_failures=1)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        base = "container clusters get-credentials host --region r1 --project p1 --quiet"
        self.assertEqual(calls, [base, f"{base} --dns-endpoint"])

    def test_the_auth_step_of_each_ci_script_survives_one_503(self):
        for script, steps in AUTH_SCRIPTS.items():
            found = _auth_steps(script)
            self.assertEqual(len(found), steps, f"auth steps through the helper in hack/{script}")
            for step in found:
                with self.subTest(script=script, step=step):
                    proc, calls, sleeps = self._run(step, failures=1)
                    self.assertEqual(proc.returncode, 0, proc.stderr)
                    self.assertEqual(calls, [f"{HOST_CALL} --dns-endpoint"] * 2)
                    self.assertEqual(sleeps, [str(DELAY)])
                    self.assertIn("STEP PASSED", proc.stdout)

    def test_no_ci_script_that_sources_ci_env_calls_gcloud_directly_for_the_host_cluster(self):
        sourcing = sorted(p.name for p in HACK.glob("*.sh") if 'ci-env.sh"' in p.read_text(encoding="utf-8"))
        self.assertLessEqual(set(AUTH_SCRIPTS), set(sourcing), "an AUTH_SCRIPTS entry does not source ci-env.sh")
        for script in sourcing:
            with self.subTest(script=script):
                self.assertIsNone(DIRECT_CALL_RE.search((HACK / script).read_text(encoding="utf-8")))


if __name__ == "__main__":
    unittest.main()
