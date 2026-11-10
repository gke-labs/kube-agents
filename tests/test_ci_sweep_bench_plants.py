"""The bench plant sweep deletes what it lists, lists only by the bench label,
and never acts on a cluster it did not authenticate to itself.

`hack/ci_sweep_bench_plants.sh` is the backstop `hack/ci-teardown.sh` runs as
its first step at every lease start and end: a bench case's stack that a run
killed between apply and destroy leaves its webhook configurations, node
readiness rules, compute classes and node pools on the host, labelled
`managed-by=kube-agents-bench`, and nothing else removes them. The script
runs here against stub `kubectl` and `gcloud` on PATH, the way
tests/test_namespaced_plant.py runs the plant shell, and what is asserted is
which commands it issues, in what order, with what exit. Three properties
make it a backstop rather than a hazard: it selects by the bench label and
never by a bare kind or the chart's label; a failing kind or delete neither
stops the legs after it nor hides in the exit; and the default is a dry run
that issues no delete, so a hand run is a read until `--delete` is given.
"""

import os
import pathlib
import subprocess
import tempfile
import unittest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SCRIPT = _REPO_ROOT / "hack" / "ci_sweep_bench_plants.sh"

_SELECTOR = "managed-by=kube-agents-bench"
_CHART_SELECTOR = "app.kubernetes.io/part-of=kube-agents"
# The kinds the script sweeps, in order. Hand-copied, not parsed from the
# script: a test that derives the expected list from the list under test
# asserts nothing.
_KINDS = (
    "validatingwebhookconfigurations.admissionregistration.k8s.io",
    "mutatingwebhookconfigurations.admissionregistration.k8s.io",
    "nodereadinessrules.readiness.node.x-k8s.io",
    "computeclasses.cloud.google.com",
)
_POOL_FILTER = "config.resourceLabels.managed-by=kube-agents-bench AND status!=STOPPING"
_ABSENT_STDERR = "error: the server doesn't have a resource type \"nodereadinessrules\""
_PROJECT = "kube-agents-evals-7"
_CLUSTER = "platform-agent-host"
_LOCATION = "us-east4"
_ARGS = ["--project", _PROJECT, "--cluster", _CLUSTER, "--location", _LOCATION]

# PLANT_NAMES holds "<kind>|<object/name>" lines the stub answers a `get` of
# that kind with. ABSENT_KIND names a kind whose group the cluster does not
# serve; LIST_FAIL_KIND one whose list fails for another reason.
# DELETE_FAIL_NAME names an object whose delete fails. Every call records
# the KUBECONFIG it saw, so the private-kubeconfig property is checkable.
_KUBECTL_STUB = """#!/bin/bash
echo "kubectl $* [KUBECONFIG=${KUBECONFIG:-unset}]" >> "$CALLS"
if [ "$1" = "get" ]; then
  kind="$2"
  if [ "$kind" = "${ABSENT_KIND:-}" ]; then
    echo "error: the server doesn't have a resource type \\"nodereadinessrules\\"" >&2; exit 1
  fi
  if [ "$kind" = "${LIST_FAIL_KIND:-}" ]; then
    echo "Unable to connect to the server: dial tcp: connection refused" >&2; exit 1
  fi
  printf '%s\\n' "${PLANT_NAMES:-}" | awk -F'|' -v k="$kind" '$1 == k { print $2 }'
  exit 0
fi
if [ "$1" = "delete" ]; then
  if [ "$2" = "${DELETE_FAIL_NAME:-}" ]; then
    echo "error: admission webhook denied the request" >&2; exit 1
  fi
  echo "$2 deleted"; exit 0
fi
exit 0
"""

# POOL_LIST is the newline-separated names the list answers with (the stub
# cannot evaluate the filter; the test asserts the filter is passed);
# POOL_LIST_RC its exit; POOL_DELETE_FAIL a pool whose delete answers 409;
# CREDS_RC the get-credentials exit.
_GCLOUD_STUB = """#!/bin/bash
echo "gcloud $*" >> "$CALLS"
case "$*" in
  *"clusters get-credentials --help"*) echo "--dns-endpoint"; exit 0 ;;
  *"clusters describe"*) exit 0 ;;
  *"clusters get-credentials"*) exit "${CREDS_RC:-0}" ;;
  *"node-pools list"*)
    [ "${POOL_LIST_RC:-0}" = "0" ] || { echo "ERROR: (gcloud.container.node-pools.list) PERMISSION_DENIED" >&2; exit "$POOL_LIST_RC"; }
    [ -n "${POOL_LIST:-}" ] && printf '%s\\n' "$POOL_LIST"
    exit 0 ;;
  *"node-pools delete"*)
    if [ "$4" = "${POOL_DELETE_FAIL:-}" ]; then
      echo "ERROR: (gcloud.container.node-pools.delete) ResponseError: code=409, message=Operation operation-1 is currently deleting node pool" >&2; exit 1
    fi
    exit 0 ;;
esac
exit 0
"""


class BenchPlantSweepTest(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        root = pathlib.Path(self._dir.name)
        stub_dir = root / "bin"
        stub_dir.mkdir()
        for name, source in (("kubectl", _KUBECTL_STUB), ("gcloud", _GCLOUD_STUB)):
            stub = stub_dir / name
            stub.write_text(source)
            stub.chmod(0o755)
        self._stub_dir = stub_dir
        self._calls = root / "calls"

    def _run(self, *args, **scenario):
        env = {k: v for k, v in os.environ.items() if k != "KUBECONFIG"}
        env["PATH"] = f"{self._stub_dir}{os.pathsep}{env['PATH']}"
        env["CALLS"] = str(self._calls)
        env.update({k: str(v) for k, v in scenario.items()})
        completed = subprocess.run(
            ["bash", str(_SCRIPT), *args], env=env, capture_output=True, text=True, timeout=60, check=False
        )
        calls = self._calls.read_text().splitlines() if self._calls.exists() else []
        return completed, calls

    @staticmethod
    def _kubectl_deletes(calls):
        return [c for c in calls if c.startswith("kubectl delete ")]

    @staticmethod
    def _kubectl_gets(calls):
        return [c for c in calls if c.startswith("kubectl get ")]

    @staticmethod
    def _pool_deletes(calls):
        return [c for c in calls if c.startswith("gcloud container node-pools delete ")]

    # --- (a) selection: the bench label, every kind, in order ----------------

    def test_every_kind_is_listed_by_the_bench_label_in_order(self):
        completed, calls = self._run(*_ARGS)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        gets = self._kubectl_gets(calls)
        self.assertEqual([g.split()[2] for g in gets], list(_KINDS), gets)
        for get in gets:
            argv = get.split()
            self.assertIn("-l", argv, get)
            self.assertEqual(argv[argv.index("-l") + 1], _SELECTOR, get)
            self.assertNotIn(_CHART_SELECTOR, argv, get)

    def test_the_pool_leg_passes_the_label_filter_and_skips_stopping_pools(self):
        completed, calls = self._run(*_ARGS)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        lists = [c for c in calls if c.startswith("gcloud container node-pools list ")]
        self.assertEqual(len(lists), 1, calls)
        self.assertIn(f"--filter={_POOL_FILTER}", lists[0])
        for flag, value in (("--cluster", _CLUSTER), ("--location", _LOCATION), ("--project", _PROJECT)):
            self.assertIn(f"{flag} {value}", lists[0])

    # --- (b) dry run by default, deletes by name under --delete ---------------

    def test_dry_run_lists_and_deletes_nothing(self):
        names = "\n".join(
            (
                f"{_KINDS[0]}|validatingwebhookconfiguration.admissionregistration.k8s.io/bench-hook",
                f"{_KINDS[3]}|computeclass.cloud.google.com/general-ccc",
            )
        )
        completed, calls = self._run(*_ARGS, PLANT_NAMES=names, POOL_LIST="bench-pool")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(self._kubectl_deletes(calls), [])
        self.assertEqual(self._pool_deletes(calls), [])
        self.assertIn("would delete validatingwebhookconfiguration.admissionregistration.k8s.io/bench-hook", completed.stdout)
        self.assertIn("would delete computeclass.cloud.google.com/general-ccc", completed.stdout)
        self.assertIn("would delete node pool bench-pool", completed.stdout)
        self.assertIn("3 matched, 0 failed", completed.stdout)

    def test_delete_removes_each_listed_name_without_waiting(self):
        names = "\n".join(
            (
                f"{_KINDS[0]}|validatingwebhookconfiguration.admissionregistration.k8s.io/bench-hook",
                f"{_KINDS[1]}|mutatingwebhookconfiguration.admissionregistration.k8s.io/bench-mutate",
                f"{_KINDS[3]}|computeclass.cloud.google.com/general-ccc",
            )
        )
        completed, calls = self._run(*_ARGS, "--delete", PLANT_NAMES=names)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        deletes = self._kubectl_deletes(calls)
        self.assertEqual(
            [d.split()[2] for d in deletes],
            [
                "validatingwebhookconfiguration.admissionregistration.k8s.io/bench-hook",
                "mutatingwebhookconfiguration.admissionregistration.k8s.io/bench-mutate",
                "computeclass.cloud.google.com/general-ccc",
            ],
            deletes,
        )
        for delete in deletes:
            argv = delete.split()
            self.assertIn("--ignore-not-found", argv, delete)
            self.assertIn("--wait=false", argv, delete)
            # By name, never by kind plus selector: the dry-run line is the
            # delete's exact target.
            self.assertNotIn("-l", argv, delete)
        self.assertIn("deleted computeclass.cloud.google.com/general-ccc", completed.stdout)

    # --- (c) one leg's failure neither stops the next nor hides ---------------

    def test_an_unserved_group_counts_absent_and_the_sweep_stays_green(self):
        completed, calls = self._run(*_ARGS, "--delete", ABSENT_KIND=_KINDS[2])
        self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
        self.assertIn(f"{_KINDS[2]}: not served on this cluster", completed.stdout)
        self.assertIn("1 kinds not served", completed.stdout)
        # The compute classes after it are still listed.
        self.assertEqual([g.split()[2] for g in self._kubectl_gets(calls)], list(_KINDS))

    def test_a_failed_list_reds_the_exit_but_the_later_kinds_and_pools_still_run(self):
        completed, calls = self._run(
            *_ARGS,
            "--delete",
            LIST_FAIL_KIND=_KINDS[0],
            PLANT_NAMES=f"{_KINDS[3]}|computeclass.cloud.google.com/general-ccc",
            POOL_LIST="bench-pool",
        )
        self.assertEqual(completed.returncode, 1, completed.stdout)
        self.assertIn("list failed", completed.stdout)
        self.assertEqual([d.split()[2] for d in self._kubectl_deletes(calls)], ["computeclass.cloud.google.com/general-ccc"])
        self.assertEqual(len(self._pool_deletes(calls)), 1, calls)

    def test_a_failed_delete_reds_the_exit_and_the_next_delete_is_still_attempted(self):
        names = "\n".join(
            (
                f"{_KINDS[0]}|validatingwebhookconfiguration.admissionregistration.k8s.io/first",
                f"{_KINDS[0]}|validatingwebhookconfiguration.admissionregistration.k8s.io/second",
            )
        )
        completed, calls = self._run(
            *_ARGS,
            "--delete",
            PLANT_NAMES=names,
            DELETE_FAIL_NAME="validatingwebhookconfiguration.admissionregistration.k8s.io/first",
        )
        self.assertEqual(completed.returncode, 1, completed.stdout)
        self.assertEqual(len(self._kubectl_deletes(calls)), 2, calls)
        self.assertIn("1 failed", completed.stdout)

    # --- (d) the pool leg: async, never default-pool, continues past a 409 ----

    def test_pools_are_deleted_async_by_name_and_a_409_does_not_stop_the_next(self):
        completed, calls = self._run(
            *_ARGS, "--delete", POOL_LIST="bench-a\nbench-b", POOL_DELETE_FAIL="bench-a"
        )
        self.assertEqual(completed.returncode, 1, completed.stdout)
        deletes = self._pool_deletes(calls)
        self.assertEqual([d.split()[4] for d in deletes], ["bench-a", "bench-b"], deletes)
        for delete in deletes:
            self.assertIn("--async", delete.split())
            self.assertIn("--quiet", delete.split())
        self.assertIn("delete failed: node pool bench-a", completed.stdout)
        self.assertIn("deleted node pool bench-b", completed.stdout)

    def test_default_pool_is_never_deleted_even_when_labelled(self):
        completed, calls = self._run(*_ARGS, "--delete", POOL_LIST="default-pool\nbench-a")
        self.assertEqual(completed.returncode, 1, completed.stdout)
        self.assertEqual([d.split()[4] for d in self._pool_deletes(calls)], ["bench-a"])
        self.assertIn("refusing to delete default-pool", completed.stdout)

    def test_an_empty_pool_listing_is_zero_pools_not_a_failure(self):
        completed, calls = self._run(*_ARGS, "--delete", POOL_LIST="")
        self.assertEqual(completed.returncode, 0, completed.stdout)
        self.assertEqual(self._pool_deletes(calls), [])
        self.assertIn("0 matched, 0 failed", completed.stdout)

    def test_a_failed_pool_listing_reds_the_exit(self):
        completed, _ = self._run(*_ARGS, "--delete", POOL_LIST_RC=1)
        self.assertEqual(completed.returncode, 1, completed.stdout)
        self.assertIn("node pools: list failed", completed.stdout)

    # --- (e) it authenticates itself, and refuses to guess a target -----------

    def test_credentials_go_into_a_private_kubeconfig_before_any_kubectl_call(self):
        completed, calls = self._run(*_ARGS, "--delete", PLANT_NAMES=f"{_KINDS[3]}|computeclass.cloud.google.com/x")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        # The endpoint probe (gke_dns_endpoint_flag) runs first; the first
        # kubectl call comes after the real get-credentials.
        creds = [c for c in calls if c.startswith("gcloud container clusters get-credentials ") and "--help" not in c]
        self.assertEqual(len(creds), 1, calls)
        for flag, value in (("--location", _LOCATION), ("--project", _PROJECT)):
            self.assertIn(f"{flag} {value}", creds[0])
        first_kubectl = next(i for i, c in enumerate(calls) if c.startswith("kubectl "))
        self.assertLess(calls.index(creds[0]), first_kubectl, calls)
        kubeconfigs = {c.rsplit("[KUBECONFIG=", 1)[1].rstrip("]") for c in calls if c.startswith("kubectl ")}
        self.assertEqual(len(kubeconfigs), 1, calls)
        (kubeconfig,) = kubeconfigs
        self.assertNotEqual(kubeconfig, "unset")
        self.assertFalse(pathlib.Path(kubeconfig).exists(), "the private kubeconfig dir outlived the run")

    def test_failed_credentials_stop_the_sweep_before_any_delete(self):
        completed, calls = self._run(*_ARGS, "--delete", CREDS_RC=1, POOL_LIST="bench-a")
        self.assertEqual(completed.returncode, 1, completed.stdout)
        self.assertEqual([c for c in calls if c.startswith("kubectl ") or "node-pools" in c], [])

    def test_missing_arguments_exit_two_before_any_call(self):
        for args in (
            [],
            ["--project", _PROJECT, "--cluster", _CLUSTER],
            ["--project", "", "--cluster", _CLUSTER, "--location", _LOCATION],
            # A value flag given last with no value: a usage error, not a loop.
            ["--cluster", _CLUSTER, "--location", _LOCATION, "--project"],
            [*_ARGS, "--bogus"],
        ):
            with self.subTest(args=args):
                completed, calls = self._run(*args)
                self.assertEqual(completed.returncode, 2, completed.stderr)
                self.assertEqual(calls, [])
                self.assertIn("usage:", completed.stderr)

    def test_script_is_executable_and_parses(self):
        self.assertTrue(os.access(_SCRIPT, os.X_OK), _SCRIPT)
        parse = subprocess.run(["bash", "-n", str(_SCRIPT)], capture_output=True, text=True, check=False)
        self.assertEqual(parse.returncode, 0, parse.stderr)


if __name__ == "__main__":
    unittest.main()
