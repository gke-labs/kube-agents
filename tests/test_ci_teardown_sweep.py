"""The teardown's label sweeps run on every path, whatever Helm says.

#961 gave the chart its first cluster-scoped resources (two
ValidatingAdmissionPolicies and their bindings), and #1006 is what happened
next: a run killed mid-flight left the policy behind with no Helm release
record, `helm uninstall` had nothing to act on, and every later lease of the
project died at deploy on "cannot be imported into the current release" until
a human deleted the object. Namespace deletion cannot catch a cluster-scoped
object by construction, so `hack/ci-teardown.sh` carries an unconditional
label sweep — and these tests pin the three properties that make it a fix
rather than a decoration:

* it deletes every audited cluster-scoped kind by the part-of label, with
  --ignore-not-found;
* it still runs when the steps before it (helm uninstall, the CRD delete)
  have failed, because the aborted-run case is the whole point;
* a failing kubectl neither stops the sweep at the first kind nor changes
  the teardown's exit code.

#1172 added the same sweep one scope down. Nothing else deletes a namespaced
object the release stops accounting for: teardown never deletes the namespace,
and owner-reference GC stops resolving owners once the PlatformAgent CRD goes.
The three properties above apply to it, plus one — it must select by the
part-of label and leave Helm's release-record Secrets, labelled owner=helm,
alone. Whether a record survives is Step 1's decision.

The steps are lifted from the script's own text and executed under bash with
stubbed kubectl/helm, so the assertions are against the code that ships
rather than a copy (the same approach as tests/test_ci_gitops_repo.py and
scripts/test_ci_eval_trap.py). The lift starts after the get-credentials and
context guards: those two are must-not-delete-the-wrong-cluster exits, the
one path on which the sweep is *supposed* to be unreachable.
"""

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

# The audit of #1006: every cluster-scoped kind the chart renders
# (agent-rbac-admission-policy.yaml, operator-rbac.yaml, operator-webhooks.yaml)
# or the operator applies at reconcile time (reconcileRBAC, the
# credential-broker TokenReview pair). Repeated here on purpose rather than
# parsed out of the script: a test that derives the expected list from the
# list under test asserts nothing. The chart's CRDs are absent because they
# install unlabelled and the teardown deletes them by file.
_SWEPT_KINDS = (
    "validatingadmissionpolicies.admissionregistration.k8s.io",
    "validatingadmissionpolicybindings.admissionregistration.k8s.io",
    "mutatingwebhookconfigurations.admissionregistration.k8s.io",
    "validatingwebhookconfigurations.admissionregistration.k8s.io",
    "clusterroles.rbac.authorization.k8s.io",
    "clusterrolebindings.rbac.authorization.k8s.io",
)

# The #1172 audit: every namespaced kind the chart renders or the operator
# applies, all of which carry the part-of label — from kube-agents.labels in
# the chart's templates, and from commonLabels() in the operator's Go.
# Hand-copied for the reason above. The chart's own CRs are absent: the CRD
# delete takes them on one path and the next lease's upgrade needs them on the
# other.
#
# persistentvolumeclaims is in the list because reconcilePVC creates the
# operator's own claims through withCommonLabels, so they are labelled and
# selectable. The claims that come from a StatefulSet's volumeClaimTemplates
# are not — that ObjectMeta carries a name and nothing else — which is why
# hindsight's claim and any RWO custom storage survive the sweep.
_SWEPT_NAMESPACED_KINDS = (
    "deployments.apps",
    "statefulsets.apps",
    "jobs.batch",
    "services",
    "configmaps",
    "secrets",
    "serviceaccounts",
    "roles.rbac.authorization.k8s.io",
    "rolebindings.rbac.authorization.k8s.io",
    "networkpolicies.networking.k8s.io",
    "poddisruptionbudgets.policy",
    "podmonitorings.monitoring.googleapis.com",
    "certificates.cert-manager.io",
    "issuers.cert-manager.io",
    "persistentvolumeclaims",
)

_NAMESPACE = "kubeagents-system"
_SELECTOR = "app.kubernetes.io/part-of=kube-agents"
# Step 1's fallback selects release records by Helm's own labels. Named here so
# the sweep test can assert the two selectors never converge.
_RECORD_SELECTOR = "owner=helm,name=kube-agents"


def _teardown_steps():
    """The lifted steps, prefixed with the file-head `readonly` constants.

    The no-magic-constants rule declares names at the top of the file, above
    _STEPS_START, so the lift carries them along or `set -u` kills the run.
    """
    text = _CI_TEARDOWN.read_text(encoding="utf-8")
    start = text.find(_STEPS_START)
    assert start != -1, f"{_STEPS_START!r} not found in hack/ci-teardown.sh"
    constants = "\n".join(
        line for line in text[:start].splitlines() if line.startswith("readonly ")
    )
    return constants + "\n" + text[start:]


class CiTeardownSweepTest(unittest.TestCase):
    maxDiff = None

    def _run_steps(self, kubectl_exit=0, helm_exit=0):
        """Run the teardown steps with recording stubs.

        Returns (returncode, kubectl argv lines, stderr). Each stub appends
        its argv to a log and exits with the requested code, so a test can
        make any step "fail" and watch what still runs after it.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            bin_dir = tmp_path / "bin"
            bin_dir.mkdir()
            log = tmp_path / "kubectl.log"
            log.touch()
            for tool, code in (("kubectl", kubectl_exit), ("helm", helm_exit)):
                stub = bin_dir / tool
                stub.write_text(
                    "#!/usr/bin/env bash\n"
                    + (f'echo "{tool} $*" >> "{log}"\n')
                    + f"exit {code}\n",
                    encoding="utf-8",
                )
                stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
            # set -uo pipefail mirrors the script's own header; NAMESPACE is
            # the one variable the lifted steps read that the head sets.
            proc = subprocess.run(
                ["bash", "-c", "set -uo pipefail\n" + _teardown_steps()],
                capture_output=True,
                text=True,
                cwd=_REPO_ROOT,
                env=get_isolated_test_env(
                    bin_dir=bin_dir, overrides={"NAMESPACE": "kubeagents-system"}
                ),
            )
            calls = log.read_text(encoding="utf-8").splitlines()
        return proc.returncode, calls, proc.stderr

    def _sweep_calls(self, calls):
        return [c for c in calls if c.startswith("kubectl delete") and "-l" in c.split()]

    # --- (a) the sweep issues the right deletes ----------------------------

    def test_every_audited_kind_is_swept_by_label(self):
        rc, calls, err = self._run_steps()
        self.assertEqual(rc, 0, err)
        sweeps = self._sweep_calls(calls)
        for kind in _SWEPT_KINDS:
            with self.subTest(kind=kind):
                matching = [c for c in sweeps if kind in c.split()]
                self.assertEqual(
                    len(matching), 1, f"expected exactly one sweep of {kind}: {sweeps}"
                )
                argv = matching[0].split()
                self.assertIn(_SELECTOR, argv)
                self.assertIn("--ignore-not-found", argv)

    def test_the_sweep_selects_by_label_never_by_bare_kind(self):
        """A delete of a whole cluster-scoped kind with no selector would take
        GKE's own objects with it; every sweep line must carry -l."""
        rc, calls, err = self._run_steps()
        self.assertEqual(rc, 0, err)
        for kind in _SWEPT_KINDS:
            for call in [c for c in calls if kind in c.split()]:
                argv = call.split()
                self.assertIn("-l", argv, f"unselectored delete of {kind}: {call}")
                self.assertIn(_SELECTOR, argv)

    # --- (b) reachability after earlier failures ---------------------------

    def test_sweep_still_runs_when_helm_uninstall_fails(self):
        """The aborted-run case: no release record, `helm uninstall` red."""
        rc, calls, err = self._run_steps(helm_exit=1)
        self.assertEqual(rc, 0, err)
        sweeps = self._sweep_calls(calls)
        for kind in _SWEPT_KINDS:
            with self.subTest(kind=kind):
                self.assertTrue(any(kind in c.split() for c in sweeps), sweeps)

    # --- (c) kubectl failure changes nothing --------------------------------

    def test_kubectl_failure_neither_stops_the_sweep_nor_reds_the_teardown(self):
        rc, calls, err = self._run_steps(kubectl_exit=1, helm_exit=1)
        self.assertEqual(rc, 0, err)
        sweeps = self._sweep_calls(calls)
        # Every kind is still attempted: one kind's failure must not eat the
        # deletes of the kinds after it.
        for kind in _SWEPT_KINDS:
            with self.subTest(kind=kind):
                self.assertTrue(any(kind in c.split() for c in sweeps), sweeps)

    # --- (d) the namespaced sweep, same three properties ---------------------

    def test_every_audited_namespaced_kind_is_swept_by_label(self):
        rc, calls, err = self._run_steps()
        self.assertEqual(rc, 0, err)
        sweeps = self._sweep_calls(calls)
        for kind in _SWEPT_NAMESPACED_KINDS:
            with self.subTest(kind=kind):
                matching = [c for c in sweeps if kind in c.split()]
                self.assertEqual(
                    len(matching), 1, f"expected exactly one sweep of {kind}: {sweeps}"
                )
                argv = matching[0].split()
                self.assertIn(_SELECTOR, argv)
                self.assertIn("--ignore-not-found", argv)
                self.assertIn("-n", argv)
                self.assertIn(_NAMESPACE, argv)

    def test_claims_are_swept_after_the_workloads_that_mount_them(self):
        """A PVC still mounted by a running pod does not delete: the
        kubernetes.io/pvc-protection finalizer holds it open until the pod is
        gone, and `kubectl delete` waits. Deleting the Deployment and the
        StatefulSet first is what keeps that wait short, so the ordering is a
        property of the step rather than an accident of how the list was
        typed."""
        rc, calls, err = self._run_steps()
        self.assertEqual(rc, 0, err)
        sweeps = self._sweep_calls(calls)

        def index_of(kind):
            for i, call in enumerate(sweeps):
                if kind in call.split():
                    return i
            self.fail(f"{kind} never swept: {sweeps}")

        claim_at = index_of("persistentvolumeclaims")
        for workload in ("deployments.apps", "statefulsets.apps"):
            with self.subTest(workload=workload):
                self.assertLess(
                    index_of(workload),
                    claim_at,
                    f"{workload} must be deleted before persistentvolumeclaims",
                )

    def test_the_namespaced_sweep_never_deletes_a_helm_release_record(self):
        """Helm's records are Secrets, and the sweep deletes Secrets. It must
        select them by the chart's part-of label — which no record carries —
        and never by owner=helm, which every record carries. Whether a record
        survives is Step 1's decision (#1180), not this sweep's.

        Run with a red helm deliberately. That is the only path on which Step
        1's fallback issues its own `kubectl delete secret -l owner=helm,...`,
        so it is the only run where both selectors are in flight and the
        assertion has two things to tell apart. An earlier version of this test
        ran the green path and filtered on `"secrets" in call.split()`, which
        the fallback's singular `secret` could not match either way: it
        inspected nothing and passed against a script with no Step 4 at all.
        """
        rc, calls, err = self._run_steps(helm_exit=1)
        self.assertEqual(rc, 0, err)
        secret_deletes = [
            c
            for c in calls
            if c.startswith("kubectl delete") and {"secret", "secrets"} & set(c.split())
        ]
        by_owner = [c for c in secret_deletes if _RECORD_SELECTOR in c.split()]
        by_part_of = [c for c in secret_deletes if _SELECTOR in c.split()]
        # Both must be present, or this is again asserting over an empty list.
        self.assertEqual(
            len(by_owner), 1, f"expected Step 1's record delete: {secret_deletes}"
        )
        self.assertEqual(
            len(by_part_of), 1, f"expected Step 4's Secret sweep: {secret_deletes}"
        )
        self.assertNotIn(_RECORD_SELECTOR, by_part_of[0].split(), by_part_of[0])
        self.assertNotIn(_SELECTOR, by_owner[0].split(), by_owner[0])

    def test_namespaced_sweep_survives_earlier_failures(self):
        """The aborted-run case one scope down: helm red, kubectl red, and
        every kind still attempted."""
        for label, kwargs in (("helm", {"helm_exit": 1}), ("kubectl", {"kubectl_exit": 1})):
            with self.subTest(failing=label):
                rc, calls, err = self._run_steps(**kwargs)
                self.assertEqual(rc, 0, err)
                sweeps = self._sweep_calls(calls)
                for kind in _SWEPT_NAMESPACED_KINDS:
                    self.assertTrue(
                        any(kind in c.split() for c in sweeps), f"{kind}: {sweeps}"
                    )

    # --- the file itself -----------------------------------------------------

    def test_ci_teardown_parses(self):
        subprocess.run(["bash", "-n", str(_CI_TEARDOWN)], check=True)


if __name__ == "__main__":
    unittest.main()
