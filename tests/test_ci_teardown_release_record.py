"""Step 2's fallback removes the Helm release record when the uninstall fails.

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

Step 3 then asks the same question before deleting the chart's CRDs, and its
three branches are pinned here too. Both of Helm's routes over a surviving
record need those CRDs — `uninstall` resolves a REST mapping for every object
in the stored manifest, which contains a PlatformAgent, and `upgrade` never
reinstalls `crds/` — so deleting them under a record leaves the next lease able
to do neither. That held `kube-agents-evals-4` from 2026-09-01 14:33 UTC until
a human repaired it the following afternoon, every re-run deleting the CRDs
again on the way past. An unreadable probe keeps them for the same reason.

A CRD delete that times out is held by CRs whose finalizers no operator is
left to clear (#6458, #3764): Step 2 removed the operator after the chart's
pre-delete hook gave up waiting on the same finalizers, and a CRD blocks on
customresourcecleanup until its last CR is gone. Left Terminating, the CRD met
the next lease's install, which the API server refuses to create CRs into. The
tests below pin that on a timed-out delete Step 3 lists the CRs of every kind
the chart's crds/ declare, releases their finalizers, deletes them and deletes
the CRDs again; that a delete which finishes, or the keep path, releases
nothing; and that a second timeout is still counted and still reaches the
sweeps.

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
# The lane values Step 1 hands the bench plant sweep, and the stub that
# stands in for it under the lift.
_PROJECT = "kube-agents-evals-7"
_CLUSTER = "platform-agent-host"
_REGION = "us-east4"
_BENCH_SWEEP_STUB = "bench-sweep"

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

# The CRs a timed-out CRD delete is held by, as (resource, namespace, name):
# the two kinds the nightly of 2026-11-19 left behind (#6458). The resource
# names are what `kubectl get -f charts/kube-agents/crds/ -o name` yields with
# its kind prefix stripped, which is how the script addresses them.
_CHART_CRD_DIR = _REPO_ROOT / "charts" / "kube-agents" / "crds"
_CRD_NAME_PREFIX = "customresourcedefinition.apiextensions.k8s.io/"
_ORPHANS = (
    ("agentprofiles.kubeagents.x-k8s.io", _NAMESPACE, "tse"),
    ("platformagents.kubeagents.x-k8s.io", _NAMESPACE, "platform-agent"),
)
# Repeated on purpose, like the selector above: the merge patch that releases
# a CR's finalizers.
_FINALIZERS_RELEASE_PATCH = '{"metadata":{"finalizers":null}}'

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
        crd_redelete_exit=0,
        orphans=(),
    ):
        """Run the teardown steps with recording stubs.

        `crd_delete_exit` answers the first CRD delete and `crd_redelete_exit`
        the one Step 3 issues again after releasing orphaned CRs; `orphans`
        are the (resource, namespace, name) CRs the stub lists for the kinds
        the chart's crds/ declare, and it names only their kinds to the
        `kubectl get -f` that reads the files.

        Returns (returncode, call argv lines, stdout, stderr). Each stub
        appends its argv to a log; helm answers `uninstall` and `history`
        with the requested exit codes and JSON, and kubectl additionally
        prints the two record-Secret names when it is invoked as the record
        delete, so the fallback's count log can be asserted. `history_exit`
        defaults red — the poisoned run's record is typically unreadable the
        same way its uninstall failed.

        `kubectl get` is answered separately, from `records`, because Step 3
        probes the same selector Step 2 deletes by and the two must be able to
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
            crd_delete_count = tmp_path / "crd-deletes"
            crd_delete_count.write_text("0", encoding="utf-8")
            orphan_kinds = sorted({resource for resource, _, _ in orphans})
            kubectl_stub = bin_dir / "kubectl"
            kubectl_stub.write_text(
                "#!/usr/bin/env bash\n"
                f'echo "kubectl $*" >> "{log}"\n'
                # The orphan release (#6458) reads the chart's CRD names from
                # the files, then each kind's CRs across namespaces.
                'if [[ "$1" == "get" && "$2" == "-f" ]]; then\n'
                + "".join(f'  echo "{_CRD_NAME_PREFIX}{kind}"\n' for kind in orphan_kinds)
                + "  exit 0\nfi\n"
                + "".join(
                    f'if [[ "$1" == "get" && "$2" == "{kind}" ]]; then\n'
                    + "".join(
                        f'  echo "{ns}   {name}"\n'
                        for resource, ns, name in orphans
                        if resource == kind
                    )
                    + "  exit 0\nfi\n"
                    for kind in orphan_kinds
                )
                + 'if [[ "$1" == "get" ]]; then\n'
                + "".join(f'  echo "{name}"\n' for name in records)
                + f"  [[ {kubectl_exit} -ne 0 ]] && echo '{_PROBE_STDERR}' >&2\n"
                + f"  exit {kubectl_exit}\n"
                "fi\n"
                # The CRD delete answers separately so a test can reach Step
                # 3's timed-out branch, which needs a readable probe (else the
                # keep branch fires first) and a red delete; the delete Step 3
                # issues again after releasing orphaned CRs answers on its own.
                'if [[ "$1" == "delete" && "$2" == "-f" ]]; then\n'
                f'  n="$(cat "{crd_delete_count}")"\n'
                f'  echo "$((n + 1))" > "{crd_delete_count}"\n'
                f'  [[ "$n" -eq 0 ]] && exit {crd_delete_exit}\n'
                f"  exit {crd_redelete_exit}\n"
                "fi\n"
                f'if [[ "$*" == *"{_RECORD_SELECTOR}"* && {kubectl_exit} -eq 0 ]]; then\n'
                + "".join(f'  echo "{name}"\n' for name in _STUB_RECORD_NAMES)
                + "fi\n"
                f"exit {kubectl_exit}\n",
                encoding="utf-8",
            )
            # The bench plant sweep (Step 1) runs as a recording stub under
            # the lift, so the real sibling script never reaches gcloud here;
            # tests/test_ci_sweep_bench_plants.py covers it.
            bench_sweep_stub = bin_dir / _BENCH_SWEEP_STUB
            bench_sweep_stub.write_text(
                "#!/usr/bin/env bash\n" f'echo "{_BENCH_SWEEP_STUB} $*" >> "{log}"\n' "exit 0\n",
                encoding="utf-8",
            )
            for stub in (helm_stub, kubectl_stub, bench_sweep_stub):
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
                    bin_dir=bin_dir,
                    overrides={
                        "NAMESPACE": _NAMESPACE,
                        "PROJECT_ID": _PROJECT,
                        "CLUSTER_NAME": _CLUSTER,
                        "REGION": _REGION,
                        "CI_TEARDOWN_BENCH_SWEEP_SCRIPT": str(bench_sweep_stub),
                    },
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

    # --- Step 3 asks the same question before deleting the CRDs -------------

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
        self.assertIn("release record survived Step 2", out)

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
            ("deleted)", {}),
            ("skipped", {"kubectl_exit": 1}),
            ("deleted after releasing", {"crd_delete_exit": 1}),
            ("delete timed out or failed", {"crd_delete_exit": 1, "crd_redelete_exit": 1}),
        ):
            with self.subTest(branch=branch):
                rc, calls, out, err = self._run_steps(**kwargs)
                self.assertEqual(rc, 0, err)
                self.assertIn(f"CRD step ({branch}", out)

    def test_the_crd_delete_is_bounded(self):
        """`kubectl delete` waits forever by default, and a CRD delete blocks
        on customresourcecleanup until every CR of that kind is gone — which a
        PlatformAgent finalizer no live operator can clear never is, until
        Step 3 releases it. Unbounded, that hangs teardown past Prow's job
        timeout and Steps 4 to 6, whose own comment says they must stay
        reachable on every path, never run."""
        rc, calls, out, err = self._run_steps()
        self.assertEqual(rc, 0, err)
        deletes = self._crd_deletes(calls)
        self.assertEqual(len(deletes), 1, calls)
        self.assertIn("--timeout", deletes[0].split(), deletes[0])

    def test_a_crd_delete_that_does_not_finish_still_reaches_the_sweeps(self):
        """The point of bounding it: the run continues, says so, and stays
        green, so the orphans Steps 4 and 5 exist to remove still go."""
        rc, calls, out, err = self._run_steps(crd_delete_exit=1)
        self.assertEqual(rc, 0, err)
        self.assertIn("did not finish within", out)
        self.assertIn("Step 4", out)
        self.assertIn("Step 5", out)

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
        still exits 0 and still reaches the steps after Step 2."""
        rc, calls, out, err = self._run_steps(uninstall_exit=1, kubectl_exit=1)
        self.assertEqual(rc, 0, err)
        # Step 3 ran, and with its probe red it cannot rule out a surviving
        # record, so it keeps the CRDs rather than re-arming #1172's trap.
        self.assertIn("could not read", out)
        # And the steps after it ran too.
        self.assertTrue(
            any(c.startswith("kubectl delete clusterroles") for c in calls),
            f"the cluster-scoped sweep never ran: {calls}",
        )

    # --- a timed-out CRD delete releases the CRs holding it (#6458) ---------

    def _patches(self, calls):
        return [c for c in calls if c.startswith("kubectl patch ")]

    def test_a_timed_out_crd_delete_releases_the_orphaned_crs_then_deletes_again(self):
        """#6458: the nightly of 2026-11-19 left agentprofiles and
        platformagents Terminating under the next install. Each CR of each
        kind is released by the finalizers merge patch, namespaced, then
        deleted without waiting, and the CRDs are deleted again after; the
        step then reports a clean delete rather than a failure."""
        rc, calls, out, err = self._run_steps(crd_delete_exit=1, orphans=_ORPHANS)
        self.assertEqual(rc, 0, err)
        for resource, ns, name in _ORPHANS:
            self.assertIn(
                f"kubectl patch {resource} -n {ns} {name} --type=merge -p {_FINALIZERS_RELEASE_PATCH}",
                "\n".join(calls),
            )
            self.assertTrue(
                any(
                    c.startswith(f"kubectl delete {resource} -n {ns} {name} --ignore-not-found --wait=false")
                    for c in calls
                ),
                calls,
            )
        self.assertEqual(len(self._patches(calls)), len(_ORPHANS), calls)
        crd_deletes = [i for i, c in enumerate(calls) if c.startswith(_CRD_DELETE_PREFIX)]
        patches = [i for i, c in enumerate(calls) if c.startswith("kubectl patch ")]
        self.assertEqual(len(crd_deletes), 2, calls)
        self.assertLess(crd_deletes[0], min(patches), calls)
        self.assertLess(max(patches), crd_deletes[1], calls)
        self.assertIn(f"✓ CRD step (deleted after releasing {len(_ORPHANS)} orphaned CR(s))", out)
        self.assertNotIn("✗ CRD step", out)

    def test_the_release_reads_the_kinds_from_the_chart_crds_not_a_list(self):
        """A kind list in the script would drift the day a CRD is added; the
        release asks the files, then every namespace for each kind."""
        rc, calls, out, err = self._run_steps(crd_delete_exit=1, orphans=_ORPHANS)
        self.assertEqual(rc, 0, err)
        reads = [c for c in calls if c.startswith("kubectl get -f charts/kube-agents/crds/")]
        self.assertEqual(len(reads), 1, calls)
        self.assertIn("-o name", reads[0])
        for resource in sorted({resource for resource, _, _ in _ORPHANS}):
            self.assertTrue(
                any(c.startswith(f"kubectl get {resource} --all-namespaces") for c in calls),
                f"{resource} was never listed: {calls}",
            )

    def test_the_release_deletes_each_cr_without_waiting(self):
        """A CR whose finalizers are released goes on its own; a delete that
        waited would block on one the API server has not yet collected and
        hold the sweeps after it, which the file forbids."""
        rc, calls, out, err = self._run_steps(crd_delete_exit=1, orphans=_ORPHANS)
        self.assertEqual(rc, 0, err)
        for resource, ns, name in _ORPHANS:
            deletes = [c for c in calls if c.startswith(f"kubectl delete {resource} -n {ns} {name} ")]
            self.assertEqual(len(deletes), 1, calls)
            self.assertIn("--wait=false", deletes[0].split(), deletes[0])

    def test_a_crd_delete_that_finishes_releases_nothing(self):
        """The release skips the operator's own deletion cleanup, so it runs
        only once a delete has timed out: a delete that finishes means the
        operator, or the hook, already cleared every CR."""
        rc, calls, out, err = self._run_steps(orphans=_ORPHANS)
        self.assertEqual(rc, 0, err)
        self.assertEqual(self._patches(calls), [])
        self.assertFalse(any(c.startswith("kubectl get -f") for c in calls), calls)
        self.assertEqual(len(self._crd_deletes(calls)), 1, calls)

    def test_a_surviving_record_releases_nothing(self):
        """On the keep path the CRs are what the next lease upgrades over."""
        rc, calls, out, err = self._run_steps(
            uninstall_exit=1,
            history_json=_HISTORY_DEPLOYED,
            history_exit=0,
            records=_STUB_RECORD_NAMES,
            orphans=_ORPHANS,
        )
        self.assertEqual(rc, 0, err)
        self.assertEqual(self._patches(calls), [])
        self.assertEqual(self._crd_deletes(calls), [])

    def test_a_second_timeout_is_counted_and_still_reaches_the_sweeps(self):
        """Something other than a finalizer holding the CRD is still a failed
        step, and the log carries what was released; the exit-code contract
        and the sweeps after it hold as before."""
        rc, calls, out, err = self._run_steps(
            crd_delete_exit=1, crd_redelete_exit=1, orphans=_ORPHANS
        )
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(self._crd_deletes(calls)), 2, calls)
        self.assertIn("✗ CRD step (delete timed out or failed", out)
        self.assertIn(f"{len(_ORPHANS)} orphaned CR(s) released", out)
        self.assertIn("Step 4", out)
        self.assertIn("Step 5", out)

    def test_every_chart_crd_is_namespaced(self):
        """The release reads each CR's namespace and name and addresses it
        with -n; a cluster-scoped CRD would need a different call, so one
        added to the chart fails here rather than silently in a teardown."""
        crd_files = sorted(_CHART_CRD_DIR.glob("*.yaml"))
        self.assertTrue(crd_files, _CHART_CRD_DIR)
        for path in crd_files:
            self.assertIn("\n  scope: Namespaced\n", path.read_text(encoding="utf-8"), path.name)


if __name__ == "__main__":
    unittest.main()
