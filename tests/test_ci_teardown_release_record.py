"""Step 1's fallback removes the Helm release record when the uninstall fails.

#1172: `helm uninstall` in `hack/ci-teardown.sh` can fail and the `|| true`
discipline then reports the teardown green with the release-record Secrets
(`owner=helm,name=kube-agents`, in a namespace teardown never deletes) still
in place. The next PR that Boskos hands the project meets them at
`hack/ci-deploy.sh`'s `helm upgrade --install`, which takes the upgrade path
against a release with no deployed revision and dies with `UPGRADE FAILED:
"kube-agents" has no deployed releases` — and that run's own teardown cannot
remove the record either, so the project stays poisoned until a human
intervenes (three runs burned on 2026-09-02 alone).

These tests pin the fallback's four properties:

* when `helm uninstall` fails and no revision is `deployed` (or the record
  cannot be read at all), the release-record Secrets are deleted by the
  `owner=helm,name=kube-agents` selector, namespaced, with
  --ignore-not-found, and the log says how many records went;
* when `helm uninstall` fails but a revision IS deployed — a healthy
  release whose uninstall failed transiently — the record stays: it is what
  lets the next lease take the clean upgrade path over the surviving
  objects, and deleting it would force a fresh install into adoption
  conflicts;
* when `helm uninstall` succeeds, the record is Helm's to manage and the
  fallback stays out of it — no record delete is issued;
* a failing kubectl neither stops the steps after the fallback nor changes
  the teardown's exit code, which nothing in the script may do.

Step 2 then asks the same question before deleting the chart's CRDs, and its
three branches are pinned here too. Both of Helm's routes over a surviving
record need those CRDs — `uninstall` resolves a REST mapping for every object
in the stored manifest, which contains a PlatformAgent, and `upgrade` never
reinstalls `crds/` — so deleting them under a record leaves the next lease able
to do neither. That held `kube-agents-evals-4` from 2026-09-01 14:33 UTC until
a human repaired it the following afternoon, every re-run deleting the CRDs
again on the way past. An unreadable probe keeps them for the same reason.

The steps are lifted from the script's own text and executed under bash with
stubbed kubectl/helm, the same approach as tests/test_ci_teardown_sweep.py.
"""

import json
import pathlib
import stat
import subprocess
import tempfile
import unittest

from tests.testing.common import get_isolated_test_env

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CI_TEARDOWN = _REPO_ROOT / "hack" / "ci-teardown.sh"

# Everything from here to end-of-file is the teardown proper — the steps that
# run once the auth and context guards have passed. Asserted present below, so
# a rename fails here loudly instead of silently shrinking what is tested.
_STEPS_START = "START_TIME=$SECONDS"

# Repeated here on purpose rather than parsed out of the script: a test that
# derives the expected selector from the code under test asserts nothing.
_RECORD_SELECTOR = "owner=helm,name=kube-agents"
_CRD_DELETE_PREFIX = "kubectl delete -f charts/kube-agents/crds/"

# What the stubbed `kubectl delete secret ... -o name` prints when it is asked
# for the release record: two record Secrets, the way a release with a failed
# revision on top of an initial one really looks.
_STUB_RECORD_NAMES = (
    "secret/sh.helm.release.v1.kube-agents.v1",
    "secret/sh.helm.release.v1.kube-agents.v2",
)

_NAMESPACE = "kubeagents-system"

# What the stubbed probe writes to stderr when it fails: the real message for
# the one unreadable state that is knowably empty, so the test can show the
# log now carries the distinction the branch itself cannot make.
_PROBE_STDERR = f'Error from server (NotFound): namespaces "{_NAMESPACE}" not found'

# helm history -o json output shapes, as the real encoder emits them.
_HISTORY_DEPLOYED = json.dumps(
    [
        {"revision": 1, "status": "superseded"},
        {"revision": 2, "status": "deployed"},
    ]
)
_HISTORY_POISONED = json.dumps(
    [
        {"revision": 1, "status": "pending-install"},
        {"revision": 2, "status": "failed"},
    ]
)


def _teardown_text():
    return _CI_TEARDOWN.read_text(encoding="utf-8")


def _head_constants(text):
    """The file-head `readonly` declarations the lifted steps read.

    The no-magic-constants rule puts names at the top of the file, above
    _STEPS_START, so the lift has to carry them along.
    """
    head = text[: text.find(_STEPS_START)]
    return "\n".join(
        line for line in head.splitlines() if line.startswith("readonly ")
    )


def _teardown_steps(text):
    start = text.find(_STEPS_START)
    assert start != -1, f"{_STEPS_START!r} not found in hack/ci-teardown.sh"
    return text[start:]


class CiTeardownReleaseRecordTest(unittest.TestCase):
    maxDiff = None

    def _run_steps(
        self,
        kubectl_exit=0,
        uninstall_exit=0,
        history_json="",
        history_exit=1,
        records=(),
        crd_delete_exit=0,
    ):
        """Run the teardown steps with recording stubs.

        Returns (returncode, call argv lines, stdout, stderr). Each stub
        appends its argv to a log; helm answers `uninstall` and `history`
        with the requested exit codes and JSON, and kubectl additionally
        prints the two record-Secret names when it is invoked as the record
        delete, so the fallback's count log can be asserted. `history_exit`
        defaults red — the poisoned run's record is typically unreadable the
        same way its uninstall failed.

        `kubectl get` is answered separately, from `records`, because Step 2
        probes the same selector Step 1 deletes by and the two must be able to
        disagree: after a successful uninstall Helm has removed its records,
        so the probe reads empty while the delete would have printed names.
        The default empty matches that — and note the probe is also subject to
        `kubectl_exit`, which is how the unreadable branch is reached.
        """
        text = _teardown_text()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            bin_dir = tmp_path / "bin"
            bin_dir.mkdir()
            log = tmp_path / "calls.log"
            log.touch()
            history_file = tmp_path / "history.json"
            history_file.write_text(history_json, encoding="utf-8")
            helm_stub = bin_dir / "helm"
            helm_stub.write_text(
                "#!/usr/bin/env bash\n"
                f'echo "helm $*" >> "{log}"\n'
                'case "$1" in\n'
                f"  uninstall) exit {uninstall_exit} ;;\n"
                f'  history) cat "{history_file}"; exit {history_exit} ;;\n'
                "  *) exit 0 ;;\n"
                "esac\n",
                encoding="utf-8",
            )
            kubectl_stub = bin_dir / "kubectl"
            kubectl_stub.write_text(
                "#!/usr/bin/env bash\n"
                f'echo "kubectl $*" >> "{log}"\n'
                'if [[ "$1" == "get" ]]; then\n'
                + "".join(f'  echo "{name}"\n' for name in records)
                + f"  [[ {kubectl_exit} -ne 0 ]] && echo '{_PROBE_STDERR}' >&2\n"
                + f"  exit {kubectl_exit}\n"
                "fi\n"
                # The CRD delete answers separately so a test can reach Step
                # 2's timed-out branch, which needs a readable probe (else the
                # keep branch fires first) and a red delete.
                'if [[ "$1" == "delete" && "$2" == "-f" ]]; then\n'
                f"  exit {crd_delete_exit}\n"
                "fi\n"
                f'if [[ "$*" == *"{_RECORD_SELECTOR}"* && {kubectl_exit} -eq 0 ]]; then\n'
                + "".join(f'  echo "{name}"\n' for name in _STUB_RECORD_NAMES)
                + "fi\n"
                f"exit {kubectl_exit}\n",
                encoding="utf-8",
            )
            for stub in (helm_stub, kubectl_stub):
                stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
            proc = subprocess.run(
                [
                    "bash",
                    "-c",
                    "set -uo pipefail\n"
                    + _head_constants(text)
                    + "\n"
                    + _teardown_steps(text),
                ],
                capture_output=True,
                text=True,
                cwd=_REPO_ROOT,
                env=get_isolated_test_env(
                    bin_dir=bin_dir, overrides={"NAMESPACE": _NAMESPACE}
                ),
            )
            calls = log.read_text(encoding="utf-8").splitlines()
        return proc.returncode, calls, proc.stdout, proc.stderr

    def _record_deletes(self, calls):
        return [
            c
            for c in calls
            if c.startswith("kubectl delete secret") and _RECORD_SELECTOR in c.split()
        ]

    def _crd_deletes(self, calls):
        return [c for c in calls if c.startswith(_CRD_DELETE_PREFIX)]

    # --- the fallback fires when the uninstall fails ------------------------

    def test_failed_uninstall_with_no_deployed_revision_deletes_the_record(self):
        rc, calls, out, err = self._run_steps(
            uninstall_exit=1, history_json=_HISTORY_POISONED, history_exit=0
        )
        self.assertEqual(rc, 0, err)
        deletes = self._record_deletes(calls)
        self.assertEqual(
            len(deletes),
            1,
            f"expected exactly one release-record delete after a failed "
            f"helm uninstall of a no-deployed-revision release: {calls}",
        )
        argv = deletes[0].split()
        self.assertIn("-n", argv)
        self.assertIn(_NAMESPACE, argv)
        self.assertIn("--ignore-not-found", argv)

    def test_an_unreadable_record_is_deleted_too(self):
        """`helm history` red (record corrupt, or gone mid-probe): nothing
        to lose — the delete is --ignore-not-found against a record that,
        if it exists, is already unusable."""
        rc, calls, out, err = self._run_steps(uninstall_exit=1, history_exit=1)
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(self._record_deletes(calls)), 1, calls)

    def test_the_fallback_logs_how_many_records_it_deleted(self):
        """A red run's artifacts must show the heal happened; the stub
        deletes two record Secrets, so the log has to say two."""
        rc, calls, out, err = self._run_steps(uninstall_exit=1)
        self.assertEqual(rc, 0, err)
        self.assertIn("deleted 2", out)

    # --- and stays out of the way everywhere else ---------------------------

    def test_successful_uninstall_issues_no_record_delete(self):
        rc, calls, out, err = self._run_steps(uninstall_exit=0)
        self.assertEqual(rc, 0, err)
        self.assertEqual(self._record_deletes(calls), [])

    def test_a_deployed_revision_keeps_its_record(self):
        """A healthy release whose uninstall failed transiently: the record
        is what lets the next lease take the clean upgrade path over the
        surviving objects, so the fallback must not delete it."""
        rc, calls, out, err = self._run_steps(
            uninstall_exit=1, history_json=_HISTORY_DEPLOYED, history_exit=0
        )
        self.assertEqual(rc, 0, err)
        self.assertEqual(self._record_deletes(calls), [])
        self.assertIn("leaving the release record", out)

    # --- Step 2 asks the same question before deleting the CRDs -------------

    def test_no_surviving_record_deletes_the_crds(self):
        """The common path, and #1006's: nothing to strand, and the next lease
        is a fresh install, which reinstalls crds/ itself."""
        rc, calls, out, err = self._run_steps()
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(self._crd_deletes(calls)), 1, calls)

    def test_a_surviving_record_keeps_the_crds(self):
        """Both of Helm's routes over a record need the CRDs — `uninstall`
        resolves a REST mapping for every object in the stored manifest, which
        contains a PlatformAgent, and `upgrade` never reinstalls crds/. Delete
        them under a record and the next lease can do neither (#1172)."""
        rc, calls, out, err = self._run_steps(
            uninstall_exit=1,
            history_json=_HISTORY_DEPLOYED,
            history_exit=0,
            records=_STUB_RECORD_NAMES[:1],
        )
        self.assertEqual(rc, 0, err)
        self.assertEqual(self._crd_deletes(calls), [], out)
        self.assertIn("release record survived Step 1", out)

    def test_an_unreadable_probe_keeps_the_crds(self):
        """Unreadable has to count as "may survive": keeping a CRD costs one
        lease's tidiness, deleting one costs the project."""
        rc, calls, out, err = self._run_steps(uninstall_exit=1, kubectl_exit=1)
        self.assertEqual(rc, 0, err)
        self.assertEqual(self._crd_deletes(calls), [], out)
        self.assertIn("could not read", out)

    def test_the_probe_reads_records_by_helms_own_labels(self):
        """A probe on the chart's part-of label would find no record at all —
        Helm stamps owner=helm and none of the chart's labels — so the gate
        would open on every run and nothing would look wrong."""
        rc, calls, out, err = self._run_steps()
        self.assertEqual(rc, 0, err)
        probes = [c for c in calls if c.startswith("kubectl get secret")]
        self.assertEqual(len(probes), 1, calls)
        argv = probes[0].split()
        self.assertIn(_RECORD_SELECTOR, argv)
        self.assertIn("-n", argv)
        self.assertIn(_NAMESPACE, argv)

    def test_the_crd_step_reports_which_branch_it_took(self):
        """#1172 went 25 hours unnoticed because the log said only ✓."""
        for branch, kwargs in (
            ("deleted", {}),
            ("skipped", {"kubectl_exit": 1}),
            ("delete timed out or failed", {"crd_delete_exit": 1}),
        ):
            with self.subTest(branch=branch):
                rc, calls, out, err = self._run_steps(**kwargs)
                self.assertEqual(rc, 0, err)
                self.assertIn(f"CRD step ({branch}", out)

    def test_the_crd_delete_is_bounded(self):
        """`kubectl delete` waits forever by default, and a CRD delete blocks
        on customresourcecleanup until every CR of that kind is gone — which a
        PlatformAgent finalizer no live operator can clear never is. Unbounded,
        that hangs teardown past Prow's job timeout and Steps 3 and 4, whose
        own comment says they must stay reachable on every path, never run."""
        rc, calls, out, err = self._run_steps()
        self.assertEqual(rc, 0, err)
        deletes = self._crd_deletes(calls)
        self.assertEqual(len(deletes), 1, calls)
        self.assertIn("--timeout", deletes[0].split(), deletes[0])

    def test_a_crd_delete_that_does_not_finish_still_reaches_the_sweeps(self):
        """The point of bounding it: the run continues, says so, and stays
        green, so the orphans Steps 3 and 4 exist to remove still go."""
        rc, calls, out, err = self._run_steps(crd_delete_exit=1)
        self.assertEqual(rc, 0, err)
        self.assertIn("did not finish within", out)
        self.assertIn("Step 3", out)
        self.assertIn("Step 4", out)

    def test_the_uninstall_asks_helm_for_the_real_error(self):
        """Without --debug Helm returns only `failed to delete release: <name>`
        and hands the cause to cfg.Log, which drops it. That one line was all
        CI recorded for the 25 hours #1172 went unnoticed, so the flag is the
        change's diagnostic and not a convenience."""
        rc, calls, out, err = self._run_steps(
            uninstall_exit=1, history_json=_HISTORY_POISONED, history_exit=0
        )
        self.assertEqual(rc, 0, err)
        uninstalls = [c for c in calls if c.startswith("helm uninstall")]
        self.assertEqual(len(uninstalls), 1, calls)
        self.assertIn("--debug", uninstalls[0].split(), uninstalls[0])

    def test_an_unreadable_probe_says_why(self):
        """The reason used to go to /dev/null. A missing namespace is a
        knowably-empty read and an RBAC failure is not; the branch treats them
        alike, so the log is the only place the difference survives."""
        rc, calls, out, err = self._run_steps(uninstall_exit=1, kubectl_exit=1)
        self.assertEqual(rc, 0, err)
        self.assertIn("namespaces", out)
        self.assertIn("not found", out)

    # --- the exit-code contract holds ---------------------------------------

    def test_a_failing_kubectl_neither_stops_teardown_nor_reds_it(self):
        """Fallback delete red, record probe red, sweep red — the teardown
        still exits 0 and still reaches the steps after Step 1."""
        rc, calls, out, err = self._run_steps(uninstall_exit=1, kubectl_exit=1)
        self.assertEqual(rc, 0, err)
        # Step 2 ran, and with its probe red it cannot rule out a surviving
        # record, so it keeps the CRDs rather than re-arming #1172's trap.
        self.assertIn("could not read", out)
        # And the steps after it ran too.
        self.assertTrue(
            any(c.startswith("kubectl delete clusterroles") for c in calls),
            f"the cluster-scoped sweep never ran: {calls}",
        )


if __name__ == "__main__":
    unittest.main()
