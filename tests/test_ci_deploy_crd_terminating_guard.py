"""5b′ refuses to install over a chart CRD still Terminating (#6458, #3764).

A CRD a previous run's teardown could not finish deleting is held by CRs whose
finalizers no operator is left to clear. Applying the chart's CRDs over it
"succeeds" with a warning, and the install then dies downstream: the first CR
create is refused (`create not allowed while custom resource definition is
terminating`, nightly main-a of 2026-11-19), or the operator loops on `no
matches for kind` and the rollout never comes (#3764). The deploy now reads
each chart CRD's deletionTimestamp before the apply and exits, naming the
CRDs, when one carries it.

These tests pin:

* a Terminating CRD stops the deploy before the apply, with an error that
  names it and the two issues;
* CRDs with no deletionTimestamp, or no CRDs at all (a fresh project), are
  applied as before;
* a probe that cannot read the CRDs leaves the decision to the apply;
* the probe passes `--no-headers` and asks for the name and deletionTimestamp
  columns, since its awk reads column two of every line it gets.

The section is lifted from the script's own text and executed under bash with
a stubbed kubectl, the approach of tests/test_ci_deploy_helm_retry.py.
"""

import pathlib
import stat
import subprocess
import tempfile
import unittest

from tests.testing.common import get_isolated_test_env

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CI_DEPLOY = _REPO_ROOT / "hack" / "ci-deploy.sh"

_SECTION_START = "# ─── 5b′. The chart's CRDs, before the chart"
_SECTION_END = "# ─── 5b″. Delete NATS claims no StatefulSet owns"

# What `kubectl get -f charts/kube-agents/crds/ --no-headers -o custom-columns=
# NAME:.metadata.name,DELETING:.metadata.deletionTimestamp` prints: one row per
# CRD, `<none>` where the field is absent.
_LIVE_ROWS = (
    "agentplugins.kubeagents.x-k8s.io          <none>",
    "agentprofiles.kubeagents.x-k8s.io         <none>",
    "dispatchjobtemplates.kubeagents.x-k8s.io  <none>",
    "platformagents.kubeagents.x-k8s.io        <none>",
    "tiers.kubeagents.x-k8s.io                 <none>",
)
_TERMINATING_ROWS = (
    "agentplugins.kubeagents.x-k8s.io          <none>",
    "agentprofiles.kubeagents.x-k8s.io         2026-11-19T00:18:00Z",
    "dispatchjobtemplates.kubeagents.x-k8s.io  <none>",
    "platformagents.kubeagents.x-k8s.io        2026-11-19T00:18:00Z",
    "tiers.kubeagents.x-k8s.io                 <none>",
)
# The header kubectl prints above those rows without --no-headers.
_HEADER_ROW = "NAME                                      DELETING"
# Repeated here on purpose rather than parsed out of the script: the column
# spec the probe must ask for, so column two is the deletionTimestamp.
_PROBE_COLUMNS = "-o custom-columns=NAME:.metadata.name,DELETING:.metadata.deletionTimestamp"
_CRD_APPLY_PREFIX = "kubectl apply --server-side --force-conflicts -f "


def _deploy_text():
    return _CI_DEPLOY.read_text(encoding="utf-8")


def _head_constants(text):
    """The file-head `readonly` declarations the section reads."""
    return "\n".join(line for line in text.splitlines() if line.startswith("readonly "))


def _section(text):
    start = text.find(_SECTION_START)
    assert start != -1, f"{_SECTION_START!r} not found in hack/ci-deploy.sh"
    end = text.find(_SECTION_END, start)
    assert end != -1, f"{_SECTION_END!r} not found after {_SECTION_START!r}"
    return text[start:end]


class CiDeployCrdTerminatingGuardTest(unittest.TestCase):
    maxDiff = None

    def _run_section(self, rows=(), get_exit=0):
        """Run the lifted section with a recording kubectl.

        Returns (returncode, call argv lines, stdout, stderr). `rows` is what
        the stub prints for the CRD probe and `get_exit` its exit code; the
        apply is recorded and succeeds.
        """
        text = _deploy_text()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            bin_dir = tmp_path / "bin"
            bin_dir.mkdir()
            log = tmp_path / "calls.log"
            log.touch()
            kubectl_stub = bin_dir / "kubectl"
            # Like kubectl, the stub prints the column header unless told not
            # to: the probe's awk reads column two of every line, so a header
            # row would name a CRD called NAME as Terminating on every deploy.
            kubectl_stub.write_text(
                "#!/usr/bin/env bash\n"
                f'echo "kubectl $*" >> "{log}"\n'
                'if [[ "$1" == "get" && "$2" == "-f" ]]; then\n'
                f'  [[ " $* " == *" --no-headers "* ]] || echo "{_HEADER_ROW}"\n'
                + "".join(f'  echo "{row}"\n' for row in rows)
                + f"  exit {get_exit}\n"
                "fi\n"
                "exit 0\n",
                encoding="utf-8",
            )
            kubectl_stub.chmod(kubectl_stub.stat().st_mode | stat.S_IXUSR)
            proc = subprocess.run(
                [
                    "bash",
                    "-c",
                    "set -euo pipefail\n" + _head_constants(text) + "\n" + _section(text),
                ],
                capture_output=True,
                text=True,
                cwd=_REPO_ROOT,
                env=get_isolated_test_env(
                    bin_dir=bin_dir,
                    overrides={
                        "SCRIPT_DIR": str(_REPO_ROOT / "hack"),
                        "NAMESPACE": "kubeagents-system",
                        "CLUSTER_NAME": "platform-agent-host",
                        "REGION": "us-central1",
                        "PROJECT_ID": "kube-agents-evals-9",
                    },
                ),
            )
            calls = log.read_text(encoding="utf-8").splitlines()
        return proc.returncode, calls, proc.stdout, proc.stderr

    def _applies(self, calls):
        return [c for c in calls if c.startswith(_CRD_APPLY_PREFIX)]

    def test_a_terminating_crd_stops_the_deploy_before_the_apply(self):
        rc, calls, out, err = self._run_section(rows=_TERMINATING_ROWS)
        self.assertEqual(rc, 1, (out, err))
        self.assertEqual(self._applies(calls), [], calls)
        self.assertIn("ERROR:", err)
        self.assertIn("still Terminating", err)
        self.assertIn("agentprofiles.kubeagents.x-k8s.io", err)
        self.assertIn("platformagents.kubeagents.x-k8s.io", err)
        # The ones not Terminating are not blamed.
        self.assertNotIn("tiers.kubeagents.x-k8s.io", err)
        self.assertIn("kube-agents-evals-9", err)
        self.assertIn("#6458", err)
        self.assertIn("#3764", err)

    def test_the_error_says_it_is_infrastructure_and_how_to_clear_it(self):
        """A reader of a red run needs to know the change under test is not
        implied, and what clears the project."""
        rc, calls, out, err = self._run_section(rows=_TERMINATING_ROWS)
        self.assertEqual(rc, 1, (out, err))
        self.assertIn("infrastructure", err)
        self.assertIn("finalizers", err)
        self.assertIn("hack/ci-teardown.sh", err)

    def test_live_crds_are_applied(self):
        rc, calls, out, err = self._run_section(rows=_LIVE_ROWS)
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(self._applies(calls)), 1, calls)
        self.assertNotIn("ERROR", err)

    def test_the_probe_asks_for_the_timestamp_column_without_a_header(self):
        """The probe's awk reads column two of every line it gets. Without
        --no-headers kubectl prints `NAME DELETING` first, column two is not
        `<none>`, and every deploy would refuse over a CRD called NAME; the
        stub prints that header unless the flag is there, so dropping it fails
        test_live_crds_are_applied too. The column spec is pinned by the
        assertion alone."""
        rc, calls, out, err = self._run_section(rows=_LIVE_ROWS)
        self.assertEqual(rc, 0, err)
        probes = [c for c in calls if c.startswith("kubectl get -f ")]
        self.assertEqual(len(probes), 1, calls)
        self.assertIn("--no-headers", probes[0].split(), probes[0])
        self.assertIn(_PROBE_COLUMNS, probes[0])

    def test_a_fresh_project_with_no_crds_is_applied(self):
        """--ignore-not-found on the probe: an empty read is a fresh project."""
        rc, calls, out, err = self._run_section(rows=())
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(self._applies(calls)), 1, calls)
        probes = [c for c in calls if c.startswith("kubectl get -f ")]
        self.assertEqual(len(probes), 1, calls)
        self.assertIn("--ignore-not-found", probes[0].split(), probes[0])

    def test_a_probe_that_cannot_read_leaves_the_decision_to_the_apply(self):
        """Under set -e a failed probe would otherwise end the deploy with no
        word on why; the apply fails on the same cause with its own."""
        rc, calls, out, err = self._run_section(rows=(), get_exit=1)
        self.assertEqual(rc, 0, err)
        self.assertIn("could not read the chart's CRDs", out)
        self.assertEqual(len(self._applies(calls)), 1, calls)


if __name__ == "__main__":
    unittest.main()
