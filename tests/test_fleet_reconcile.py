"""The fleet reconcile applies the stack it was given and refuses the plan it was not.

`hack/fleet_reconcile.py` holds a write credential on every pool project's
fleet, so these tests pin what bounds it. It applies only the plan it
inspected: a delete or a replace is applied only on an address the committed
allowlist declares, anything else is refused and named, never applied. It
holds a project only through Boskos, as many at a time as its workers, and
gives every one back, on success, on a refusal, on a fault, on SIGTERM. It
never starts a project its budget cannot fit, and stops when main's fleet
tree moves. `--drifted` reads exactly the projects the fixture-state scan
marks drifted. And a project Boskos will not hand over is busy or not reached,
not failed: the job stays green and the next run gets it.

The shared walk in `hack/boskos_pool.py` is covered here for what the sweep's
tests do not reach: acquiring one project by name.
"""

import argparse
import importlib.util
import io
import json
import pathlib
import re
import subprocess
import os
import signal
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
from unittest import mock

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_MODULE_PATH = _REPO_ROOT / "hack" / "fleet_reconcile.py"

_spec = importlib.util.spec_from_file_location("fleet_reconcile", _MODULE_PATH)
reconcile = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(reconcile)
boskos_pool = reconcile.boskos_pool

BOSKOS = "http://boskos.test"
OWNER = "test-owner"
P7 = "kube-agents-evals-7"
P8 = "kube-agents-evals-8"


def _http_error(code, url):
    return urllib.error.HTTPError(url, code, "status %d" % code, {}, io.BytesIO(b""))


def _plan(*changes):
    return json.dumps({"resource_changes": [{"address": address, "change": {"actions": list(actions)}} for actions, address in changes]})


UPDATE_ONLY = _plan((["update"], "google_container_cluster.seeded_b"))
CREATE_AND_UPDATE = _plan((["create"], "google_compute_disk.orphan"), (["update"], "google_container_cluster.seeded_b"), (["no-op"], "google_service_account.fleet_reader"))
REPLACE = _plan((["delete", "create"], "google_container_node_pool.seeded_a_idle"), (["update"], "google_container_cluster.seeded_b"))
REPLACE_NO_SURGE = _plan((["delete", "create"], "google_container_node_pool.no_surge_pool"), (["update"], "google_container_cluster.seeded_b"))
DELETE = _plan((["delete"], "google_compute_disk.orphan"))
FORGET = _plan((["forget"], "google_compute_disk.orphan"), (["update"], "google_container_cluster.seeded_b"))
KNOWN = {P7, P8}


class _Tofu:
    """A stand-in for `tofu`: plays back one plan per project and records the calls."""

    def __init__(self, plans=None, plan_exit=None, fail=None):
        self.plans = plans or {}
        self.plan_exit = plan_exit or {}
        self.fail = fail or {}
        self.calls = []
        self.project = None

    def __call__(self, argv, cwd=None, timeout=None, **_):
        self.calls.append(list(argv))
        assert argv[0] == "tofu", argv
        verb = argv[1]
        for arg in argv:
            if arg.startswith("-backend-config=bucket="):
                self.project = arg.split("=", 2)[2].removesuffix("-tf-state")
        if verb in self.fail:
            failure = self.fail[verb]
            if isinstance(failure, Exception):
                raise failure
            return subprocess.CompletedProcess(argv, 1, "", failure)
        if verb == "plan":
            return subprocess.CompletedProcess(argv, self.plan_exit.get(self.project, reconcile.PLAN_HAS_CHANGES), "", "")
        if verb == "show":
            return subprocess.CompletedProcess(argv, 0, self.plans[self.project], "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    def verbs(self):
        return [call[1] for call in self.calls]


def _gcloud_ok(argv, **_):
    return subprocess.CompletedProcess(argv, 0, "", "")


def setUpModule():
    # boskos_pool holds every lease a second before releasing it (its cache
    # lag); the reconcile's holds last minutes, so nothing here depends on it.
    # The applied.json marker goes through gcloud, which the suite never runs.
    global _real_pool_pause, _real_gcloud
    _real_pool_pause = boskos_pool.pause
    boskos_pool.pause = lambda seconds: None
    _real_gcloud = reconcile.gcloud_runner
    reconcile.gcloud_runner = _gcloud_ok


def tearDownModule():
    boskos_pool.pause = _real_pool_pause
    reconcile.gcloud_runner = _real_gcloud


class _Boskos:
    """A stand-in for the Boskos server: `free` in order for /acquire, by name for /acquirebystate."""

    def __init__(self, free=(), release_errors=None):
        self.free = list(free)
        self.release_errors = release_errors or {}
        self.acquired = []
        self.released = []
        self.resets = []
        self.beats = []
        self.walked = 0

    def __call__(self, request, timeout=None):
        url = request.full_url
        query = dict(part.split("=", 1) for part in url.split("?", 1)[1].split("&"))
        action = url.split("?", 1)[0].rsplit("/", 1)[1]
        if action == "update":
            assert query["state"] == reconcile.HOLD_STATE and query["owner"] == OWNER, query
            self.beats.append(query["name"])
            return io.BytesIO(b"")
        if action == "reset":
            self.resets.append(query)
            return io.BytesIO(b"{}")
        if action == "acquire":
            assert query["dest"] == reconcile.HOLD_STATE and query["state"] == "free", query
            self.walked += 1
            if not self.free:
                raise _http_error(404, url)
            name = self.free.pop(0)
            self.acquired.append(name)
            return io.BytesIO(json.dumps({"name": name}).encode())
        if action == "acquirebystate":
            assert query["dest"] == reconcile.HOLD_STATE and query["state"] == "free", query
            name = query["names"]
            if name not in self.free:
                raise _http_error(404, url)
            self.free.remove(name)
            self.acquired.append(name)
            return io.BytesIO(json.dumps([{"name": name}]).encode())
        if action == "release":
            assert query["dest"] == "free" and query["owner"] == OWNER, query
            failure = self.release_errors.get(query["name"])
            if failure is not None:
                raise failure
            self.released.append(query["name"])
            return io.BytesIO(b"")
        raise AssertionError("unexpected Boskos call %s" % url)


class PlanInspectionTest(unittest.TestCase):
    def test_an_in_place_plan_is_applied(self):
        tofu = _Tofu({P7: UPDATE_ONLY})
        outcome, detail = reconcile.reconcile_project(P7, runner=tofu)
        self.assertEqual(outcome, reconcile.OUTCOME_APPLIED)
        self.assertEqual(detail, "0 to add, 1 to change, 0 to replace, 0 to destroy, 0 refused")
        self.assertEqual(tofu.verbs(), ["init", "plan", "show", "apply"])
        self.assertEqual(tofu.calls[3][-1], tofu.calls[2][-1], "apply takes the plan file show inspected")
        self.assertIn("-var=project_id=%s" % P7, tofu.calls[1])
        self.assertIn("-backend-config=bucket=%s-tf-state" % P7, tofu.calls[0])
        self.assertIn("-backend-config=prefix=seeded-fleet", tofu.calls[0])
        self.assertIn("-lockfile=readonly", tofu.calls[0], "the committed lock chooses the providers")

    def test_the_fleet_stack_commits_a_lock_file_that_matches_its_constraints(self):
        lock = (reconcile.FLEET_DIR / ".terraform.lock.hcl").read_text()
        versions = (reconcile.FLEET_DIR / "versions.tf").read_text()
        for provider in ("google", "kubernetes"):
            self.assertIn(f'provider "registry.opentofu.org/hashicorp/{provider}"', lock)
        constraints = re.findall(r'version\s*=\s*"(~> [\d.]+)"', versions)
        self.assertEqual(len(constraints), 2, "one `~>` constraint per provider; another form needs this test and the lock re-done")
        for constraint in constraints:
            self.assertIn(f'constraints = "{constraint}"', lock)
        # One h1 hash per locked platform per provider: linux_amd64 for the
        # periodic, darwin for the hands that run it locally.
        for block in lock.split('provider "')[1:]:
            self.assertGreaterEqual(block.count('"h1:'), 3, block[:60])

    def test_a_create_is_a_reconcile_not_a_refusal(self):
        # The orphan disk a cleanup deleted comes back; that is the point.
        tofu = _Tofu({P7: CREATE_AND_UPDATE})
        outcome, detail = reconcile.reconcile_project(P7, runner=tofu)
        self.assertEqual((outcome, detail), (reconcile.OUTCOME_APPLIED, "1 to add, 1 to change, 0 to replace, 0 to destroy, 0 refused"))

    def test_a_replace_is_refused_and_named_before_anything_is_applied(self):
        tofu = _Tofu({P7: REPLACE})
        outcome, detail = reconcile.reconcile_project(P7, runner=tofu)
        self.assertEqual(outcome, reconcile.OUTCOME_REFUSED)
        self.assertIn("delete+create google_container_node_pool.seeded_a_idle", detail)
        self.assertNotIn("apply", tofu.verbs())

    def test_the_no_surge_pool_replace_is_applied_and_counted(self):
        # The one replacement the stack asks for on purpose: the pool is replaced
        # at a minor roll because an in-place update waits out its budget.
        tofu = _Tofu({P7: REPLACE_NO_SURGE})
        outcome, detail = reconcile.reconcile_project(P7, runner=tofu)
        self.assertEqual(outcome, reconcile.OUTCOME_APPLIED)
        self.assertIn("1 to replace", detail)
        self.assertIn("apply", tofu.verbs())

    def test_a_delete_is_refused(self):
        tofu = _Tofu({P7: DELETE})
        outcome, _ = reconcile.reconcile_project(P7, runner=tofu)
        self.assertEqual(outcome, reconcile.OUTCOME_REFUSED)
        self.assertNotIn("apply", tofu.verbs())

    def test_an_action_that_is_not_a_create_or_update_is_refused(self):
        # `forget` drops a resource from state; a later tofu may add others.
        tofu = _Tofu({P7: FORGET})
        outcome, detail = reconcile.reconcile_project(P7, runner=tofu)
        self.assertEqual(outcome, reconcile.OUTCOME_REFUSED)
        self.assertIn("forget google_compute_disk.orphan", detail)
        self.assertIn("1 refused", detail)
        self.assertNotIn("apply", tofu.verbs())

    def test_a_show_that_is_json_but_not_an_object_is_that_projects_failure(self):
        for body in ("[]", "null", "{}", '{"resource_changes": null}', '{"resource_changes": []}', '{"resource_changes": [null]}', '{"resource_changes": {"a": {}}}', '{"resource_changes": 5}', '{"resource_changes": "abc"}', '{"resource_changes": [{"address": "a", "change": "x"}]}', '{"resource_changes": [{"address": "a", "change": {"actions": [null]}}]}', '{"resource_changes": [{"address": "a", "change": {"actions": 5}}]}',
                     # Falsy wrong types must be refused as loudly as truthy ones.
                     '{"resource_changes": 0}', '{"resource_changes": false}', '{"resource_changes": ""}', '{"resource_changes": {}}',
                     '{"resource_changes": [{"address": "a", "change": []}]}', '{"resource_changes": [{"address": "a", "change": 0}]}', '{"resource_changes": [{"address": "a", "change": {"actions": 0}}]}', '{"resource_changes": [{"address": "a", "change": {"actions": {}}}]}', '{"resource_changes": [{"address": "a", "change": {"actions": ""}}]}',
                     # The one falsy list: a shape the format never emits, so refused, not skipped.
                     '{"resource_changes": [{"address": "a", "change": {"actions": []}}]}'):
            tofu = _Tofu({P7: body})
            outcome, detail = reconcile.reconcile_project(P7, runner=tofu)
            self.assertEqual(outcome, reconcile.OUTCOME_FAILED, body)
            self.assertTrue(detail.startswith("tofu show"), detail)

    def test_a_plan_with_nothing_to_do_applies_nothing(self):
        tofu = _Tofu({}, plan_exit={P7: reconcile.PLAN_NO_CHANGES})
        outcome, _ = reconcile.reconcile_project(P7, runner=tofu)
        self.assertEqual(outcome, reconcile.OUTCOME_UNCHANGED)
        self.assertEqual(tofu.verbs(), ["init", "plan"])

    def test_dry_run_plans_and_inspects_without_applying(self):
        tofu = _Tofu({P7: UPDATE_ONLY})
        outcome, detail = reconcile.reconcile_project(P7, runner=tofu, dry_run=True)
        self.assertEqual(outcome, reconcile.OUTCOME_PLANNED)
        self.assertIn("update google_container_cluster.seeded_b", detail)
        self.assertEqual(tofu.verbs(), ["init", "plan", "show"])

    def test_a_failing_init_is_that_projects_failure(self):
        tofu = _Tofu({}, fail={"init": "Error: storage: bucket doesn't exist"})
        outcome, detail = reconcile.reconcile_project(P7, runner=tofu)
        self.assertEqual(outcome, reconcile.OUTCOME_FAILED)
        self.assertIn("tofu init exited 1", detail)
        self.assertIn("bucket doesn't exist", detail)

    def test_a_plan_that_errors_is_a_failure_not_a_change(self):
        # Exit 1 from -detailed-exitcode is an error; only 0 and 2 are answers.
        tofu = _Tofu({}, fail={"plan": "Error: Failed to load plugin schemas"})
        outcome, detail = reconcile.reconcile_project(P7, runner=tofu)
        self.assertEqual(outcome, reconcile.OUTCOME_FAILED)
        self.assertIn("tofu plan exited 1", detail)

    def test_a_tofu_that_hits_the_ceiling_is_a_failure(self):
        tofu = _Tofu({}, fail={"init": subprocess.TimeoutExpired(["tofu"], 5)})
        outcome, detail = reconcile.reconcile_project(P7, runner=tofu, timeout=5)
        self.assertEqual(outcome, reconcile.OUTCOME_FAILED)
        self.assertIn("did not finish within 5s", detail)
        self.assertIn("tofu force-unlock", detail, "a kill at the ceiling leaves the lock; the line says so")

    def test_a_show_that_is_not_json_is_a_failure(self):
        tofu = _Tofu({P7: "<html>"})
        outcome, detail = reconcile.reconcile_project(P7, runner=tofu)
        self.assertEqual(outcome, reconcile.OUTCOME_FAILED)
        self.assertIn("not JSON", detail)


class TargetsTest(unittest.TestCase):
    def test_drifted_projects_are_exactly_the_scans_drift_map(self):
        document = {
            "projects": {
                P8: {"roles": {"crashloop-workload": {"state": "drifted", "detail": ["x"]}}},
                P7: {"roles": {"crashloop-workload": {"state": "healthy", "detail": []}, "idle-pool": {"state": "not_checked", "detail": ["y"]}}},
                "kube-agents-evals-9": {"roles": {"idle-pool": {"state": "drifted", "detail": []}}},
            }
        }
        self.assertEqual(reconcile.drifted_projects(document), [P8, "kube-agents-evals-9"])

    def test_a_scan_with_no_drift_targets_nothing(self):
        self.assertEqual(reconcile.drifted_projects({"projects": {P7: {"roles": {}}}}), [])
        self.assertEqual(reconcile.drifted_projects({}), [])

    def test_the_scan_is_read_from_gcs_with_gcloud(self):
        def gcloud(argv, **_):
            self.assertEqual(argv, ["gcloud", "storage", "cat", reconcile.DEFAULT_FIXTURE_STATE])
            return subprocess.CompletedProcess(argv, 0, json.dumps({"projects": {}}), "")

        self.assertEqual(reconcile.load_fixture_state(reconcile.DEFAULT_FIXTURE_STATE, runner=gcloud), {"projects": {}})

    def test_a_missing_gcloud_is_named_not_called_a_service(self):
        def no_gcloud(argv, **_):
            raise FileNotFoundError(2, "No such file or directory", "gcloud")

        with self.assertRaises(reconcile.ReconcileError) as raised:
            reconcile.load_fixture_state(reconcile.DEFAULT_FIXTURE_STATE, runner=no_gcloud)
        self.assertIn("could not run gcloud", str(raised.exception))

    def test_a_missing_local_scan_file_is_named_not_called_a_service(self):
        with self.assertRaises(reconcile.ReconcileError) as raised:
            reconcile.load_fixture_state("/no/such/fixture-state.json")
        self.assertIn("could not read /no/such/fixture-state.json", str(raised.exception))
        with self.assertRaises(reconcile.ReconcileError):
            reconcile.pool_projects("/no/such/ci-deploy.sh")

    def test_an_unreadable_scan_is_a_fault(self):
        def gcloud(argv, **_):
            return subprocess.CompletedProcess(argv, 1, "", "AccessDeniedException: 403")

        with self.assertRaises(reconcile.ReconcileError):
            reconcile.load_fixture_state(reconcile.DEFAULT_FIXTURE_STATE, runner=gcloud)

    def test_the_pool_size_is_the_mapping_in_ci_deploy(self):
        # The walk's bound; every registered project is a row there.
        self.assertGreaterEqual(reconcile.pool_size(), 30)


class LeaseTest(unittest.TestCase):
    def test_a_named_project_is_acquired_by_name_applied_and_released(self):
        boskos = _Boskos(free=[P7, P8])
        tofu = _Tofu({P7: UPDATE_ONLY})
        with mock.patch.object(boskos_pool.urllib.request, "urlopen", boskos):
            outcomes = reconcile.reconcile_named([P7], BOSKOS, OWNER, runner=tofu, known=KNOWN)
        self.assertEqual(outcomes[P7][0], reconcile.OUTCOME_APPLIED)
        self.assertEqual((boskos.acquired, boskos.released), ([P7], [P7]))
        self.assertEqual(boskos.free, [P8], "the project not named was never touched")

    def test_a_named_project_that_is_not_free_is_busy_not_failed(self):
        boskos = _Boskos(free=[P8])
        tofu = _Tofu({})
        with mock.patch.object(boskos_pool.urllib.request, "urlopen", boskos):
            outcomes = reconcile.reconcile_named([P7], BOSKOS, OWNER, runner=tofu, known=KNOWN)
        self.assertEqual(outcomes[P7], (reconcile.OUTCOME_BUSY, reconcile.REASON_BUSY))
        self.assertIn("not registered", reconcile.REASON_BUSY, "Boskos's 404 does not say which, so the line names both")
        self.assertEqual(tofu.calls, [])
        self.assertNotIn(reconcile.OUTCOME_BUSY, reconcile.FAILING_OUTCOMES)

    def test_a_name_outside_the_pool_mapping_fails_before_boskos_is_asked(self):
        def no_boskos(request, timeout=None):
            raise AssertionError("Boskos was called: %s" % request.full_url)

        tofu = _Tofu({})
        with mock.patch.object(boskos_pool.urllib.request, "urlopen", no_boskos):
            outcomes = reconcile.reconcile_named(["kube-agents-evals-99"], BOSKOS, OWNER, runner=tofu, known=KNOWN)
        self.assertEqual(outcomes["kube-agents-evals-99"], (reconcile.OUTCOME_FAILED, reconcile.REASON_UNMAPPED))
        self.assertEqual(tofu.calls, [])

    def test_the_default_mapping_is_the_one_in_ci_deploy(self):
        self.assertIn("kube-agents-evals-3", reconcile.pool_projects())
        self.assertEqual(len(reconcile.pool_projects()), reconcile.pool_size())

    def test_a_refused_project_is_released(self):
        boskos = _Boskos(free=[P7])
        with mock.patch.object(boskos_pool.urllib.request, "urlopen", boskos):
            outcomes = reconcile.reconcile_named([P7], BOSKOS, OWNER, runner=_Tofu({P7: REPLACE}), known=KNOWN)
        self.assertEqual(outcomes[P7][0], reconcile.OUTCOME_REFUSED)
        self.assertEqual(boskos.released, [P7])

    def test_a_termination_during_a_hold_releases_the_project(self):
        # The hold's finally, not the runner: the runner raises on its first call.
        boskos = _Boskos(free=[P7])

        def terminated(argv, **_):
            raise boskos_pool.Terminated("signal 15")

        with mock.patch.object(boskos_pool.urllib.request, "urlopen", boskos):
            with self.assertRaises(boskos_pool.Terminated):
                reconcile.reconcile_named([P7], BOSKOS, OWNER, runner=terminated, known=KNOWN)
        self.assertEqual(boskos.released, [P7])

    def test_a_release_that_fails_is_that_projects_failure(self):
        boskos = _Boskos(free=[P7], release_errors={P7: _http_error(500, BOSKOS)})
        with mock.patch.object(boskos_pool.urllib.request, "urlopen", boskos):
            outcomes = reconcile.reconcile_named([P7], BOSKOS, OWNER, runner=_Tofu({P7: UPDATE_ONLY}), known=KNOWN)
        self.assertEqual(outcomes[P7][0], reconcile.OUTCOME_FAILED)
        self.assertIn("release failed", outcomes[P7][1])

    def test_a_release_that_fails_before_a_termination_is_on_the_record(self):
        # The report main writes on the way out must not say a project was
        # applied when its release failed and it sits in the hold state.
        def tofu(argv, **_):
            if "kube-agents-evals-8-tf-state" in " ".join(argv):
                raise boskos_pool.Terminated("signal 2")
            if argv[1] == "plan":
                return subprocess.CompletedProcess(argv, reconcile.PLAN_HAS_CHANGES, "", "")
            if argv[1] == "show":
                return subprocess.CompletedProcess(argv, 0, UPDATE_ONLY, "")
            return subprocess.CompletedProcess(argv, 0, "", "")

        boskos = _Boskos(free=[P7, P8], release_errors={P7: _http_error(502, BOSKOS)})
        outcomes = {}
        with mock.patch.object(boskos_pool.urllib.request, "urlopen", boskos), mock.patch("sys.stdout", io.StringIO()), mock.patch("sys.stderr", io.StringIO()):
            with self.assertRaises(boskos_pool.Terminated):
                reconcile.reconcile_pool(BOSKOS, OWNER, runner=tofu, known=KNOWN, outcomes=outcomes)
        self.assertEqual(outcomes[P7][0], reconcile.OUTCOME_FAILED)
        self.assertIn("release failed", outcomes[P7][1])

    def test_a_terminated_project_whose_release_also_fails_keeps_its_interrupted_outcome(self):
        # The force-unlock hint must survive: the release failure joins the
        # interrupted reason rather than replacing it.
        def tofu(argv, **_):
            if "kube-agents-evals-8-tf-state" in " ".join(argv):
                raise boskos_pool.Terminated("signal 2")
            if argv[1] == "plan":
                return subprocess.CompletedProcess(argv, reconcile.PLAN_HAS_CHANGES, "", "")
            if argv[1] == "show":
                return subprocess.CompletedProcess(argv, 0, UPDATE_ONLY, "")
            return subprocess.CompletedProcess(argv, 0, "", "")

        boskos = _Boskos(free=[P7, P8], release_errors={P8: _http_error(502, BOSKOS)})
        outcomes = {}
        with mock.patch.object(boskos_pool.urllib.request, "urlopen", boskos), mock.patch("sys.stdout", io.StringIO()), mock.patch("sys.stderr", io.StringIO()):
            with self.assertRaises(boskos_pool.Terminated):
                reconcile.reconcile_pool(BOSKOS, OWNER, runner=tofu, known=KNOWN, outcomes=outcomes)
        self.assertEqual(outcomes[P8][0], reconcile.OUTCOME_INTERRUPTED)
        self.assertIn("force-unlock", outcomes[P8][1])
        self.assertIn("release failed", outcomes[P8][1])

    def test_a_named_projects_release_failure_before_a_termination_is_on_the_record(self):
        # The hourly's path (reconcile_named): a release refused, then a
        # termination raised by the unblock, must still record the failure.
        class _Boskos_signalling_release(_Boskos):
            def __call__(self, request, timeout=None):
                if "/release?" in request.full_url:
                    os.kill(os.getpid(), signal.SIGINT)
                return super().__call__(request, timeout)

        boskos = _Boskos_signalling_release(free=[P7], release_errors={P7: _http_error(502, BOSKOS)})
        outcomes = {}
        previous = signal.signal(signal.SIGINT, boskos_pool.terminate)
        try:
            with mock.patch.object(boskos_pool.urllib.request, "urlopen", boskos), mock.patch("sys.stdout", io.StringIO()), mock.patch("sys.stderr", io.StringIO()):
                with self.assertRaises(boskos_pool.Terminated):
                    reconcile.reconcile_named([P7], BOSKOS, OWNER, runner=_Tofu({P7: UPDATE_ONLY}), known=KNOWN, outcomes=outcomes)
        finally:
            signal.signal(signal.SIGINT, previous)
        self.assertEqual(outcomes[P7][0], reconcile.OUTCOME_FAILED)
        self.assertIn("release failed", outcomes[P7][1])

    def test_no_lease_asks_boskos_nothing_and_takes_a_project_outside_the_pool(self):
        # The dev-project path: no Boskos, and no mapping check, since the
        # check exists only to stop a typo reading as busy at Boskos.
        def no_boskos(request, timeout=None):
            raise AssertionError("Boskos was called: %s" % request.full_url)

        dev = "my-dev-project"
        with mock.patch.object(boskos_pool.urllib.request, "urlopen", no_boskos), mock.patch.object(
            reconcile, "pool_projects", side_effect=AssertionError("the mapping was read on the dev-project path")
        ):
            outcomes = reconcile.reconcile_named([P7, dev], BOSKOS, OWNER, lease=False, runner=_Tofu({P7: UPDATE_ONLY, dev: UPDATE_ONLY}))
        self.assertEqual(outcomes[P7][0], reconcile.OUTCOME_APPLIED)
        self.assertEqual(outcomes[dev][0], reconcile.OUTCOME_APPLIED)

    def test_the_pool_walk_applies_every_free_project_once_and_releases_each(self):
        boskos = _Boskos(free=[P7, P8])
        tofu = _Tofu({P7: UPDATE_ONLY, P8: CREATE_AND_UPDATE})
        with mock.patch.object(boskos_pool.urllib.request, "urlopen", boskos):
            outcomes = reconcile.reconcile_pool(BOSKOS, OWNER, runner=tofu, known=KNOWN)
        self.assertEqual({p: o for p, (o, _) in outcomes.items()}, {P7: reconcile.OUTCOME_APPLIED, P8: reconcile.OUTCOME_APPLIED})
        self.assertEqual(sorted(boskos.released), [P7, P8])
        self.assertEqual(tofu.verbs().count("apply"), 2)

class MainTest(unittest.TestCase):
    def _main(self, argv, boskos, tofu, scan=None):
        """main() with the process-wide signal handlers patched, so the test
        runner keeps its own SIGINT/SIGTERM behaviour after this class."""
        stderr = io.StringIO()
        load = mock.patch.object(reconcile, "load_fixture_state", lambda source, runner=None: scan) if scan is not None else mock.patch.object(reconcile, "load_fixture_state", reconcile.load_fixture_state)
        with mock.patch.object(boskos_pool.urllib.request, "urlopen", boskos), mock.patch.object(
            reconcile, "tofu_runner", tofu
        ), mock.patch.object(reconcile.signal, "signal") as installed, mock.patch("sys.stdout", io.StringIO()), mock.patch(
            "sys.stderr", stderr
        ), load:
            rc = reconcile.main(argv + ["--boskos-server", BOSKOS, "--boskos-owner", OWNER])
        self.assertEqual(sorted(c.args[0] for c in installed.call_args_list), sorted(reconcile.TERMINATION_SIGNALS))
        for c in installed.call_args_list:
            self.assertIs(c.args[1], boskos_pool.terminate)
        return rc, stderr.getvalue()

    def test_a_refusal_exits_one_and_names_the_project(self):
        rc, stderr = self._main(["--project", P7], _Boskos(free=[P7]), _Tofu({P7: REPLACE}))
        self.assertEqual(rc, reconcile.EXIT_FAILED)
        self.assertIn(P7, stderr)

    def test_a_run_terminated_mid_walk_has_named_every_project_it_reached(self):
        # The per-project line is printed as each finishes, and the summary
        # is printed on the way out, so a weekly killed at its deadline still
        # says what it applied and refused.
        calls = []

        def tofu(argv, **_):
            calls.append(argv)
            if "kube-agents-evals-8-tf-state" in " ".join(argv):
                raise boskos_pool.Terminated("signal 2")
            if argv[1] == "plan":
                return subprocess.CompletedProcess(argv, reconcile.PLAN_HAS_CHANGES, "", "")
            if argv[1] == "show":
                return subprocess.CompletedProcess(argv, 0, UPDATE_ONLY, "")
            return subprocess.CompletedProcess(argv, 0, "", "")

        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(boskos_pool.urllib.request, "urlopen", _Boskos(free=[P7, P8])), mock.patch.object(
            reconcile, "tofu_runner", tofu
        ), mock.patch.object(reconcile.signal, "signal"), mock.patch("sys.stdout", stdout), mock.patch("sys.stderr", stderr):
            rc = reconcile.main(["--project", P7, "--project", P8, "--boskos-server", BOSKOS, "--boskos-owner", OWNER])
        self.assertEqual(rc, boskos_pool.TERMINATED_EXIT_CODE)
        self.assertIn(f"{P7}: applied", stdout.getvalue())
        # The project the signal landed in is named, with the recovery: the
        # operator's force-unlock is against that project's state.
        self.assertIn(f"{P8}: interrupted (terminated (signal 2) while tofu ran", stdout.getvalue())
        self.assertIn("force-unlock", stdout.getvalue())
        self.assertIn("reconciled 2 project(s): 1 applied, 0 converged, 0 unchanged, 0 planned, 0 busy, 0 refused or failed, 1 interrupted, 0 not reached", stdout.getvalue())
        self.assertIn(f"terminated (signal 2) after 2 project(s); interrupted in {P8}", stderr.getvalue())

    def test_drifted_reads_the_scan_resets_strands_and_applies_the_projects_listed(self):
        boskos = _Boskos(free=[P7, P8])
        scan = {"projects": {P8: {"roles": {"idle-pool": {"state": "drifted", "detail": []}}}, P7: {"roles": {"idle-pool": {"state": "healthy", "detail": []}}}}}
        tofu = _Tofu({P8: UPDATE_ONLY})
        rc, _ = self._main(["--drifted"], boskos, tofu, scan=scan)
        self.assertEqual(rc, reconcile.EXIT_OK)
        self.assertEqual(boskos.acquired, [P8])
        self.assertEqual(tofu.verbs(), ["init", "plan", "show", "apply"], "the drifted project is applied, not only planned")
        self.assertIn("-var=project_id=%s" % P8, tofu.calls[1])
        self.assertEqual(boskos.resets[0]["state"], reconcile.HOLD_STATE)
        self.assertEqual(boskos.resets[0]["expire"], reconcile.STRANDED_AFTER)

    def test_all_asks_for_every_mapped_project_after_resetting_strands_and_applies_each(self):
        # The full-pass arm: every mapped project is asked for by name, held
        # and applied once, and the exit is the outcomes'.
        boskos = _Boskos(free=[P7, P8])
        tofu = _Tofu({P7: UPDATE_ONLY, P8: UPDATE_ONLY})
        with mock.patch.object(reconcile, "pool_projects", lambda *a, **k: set(KNOWN)):
            rc, _ = self._main(["--all"], boskos, tofu)
        self.assertEqual(rc, reconcile.EXIT_OK)
        self.assertEqual(boskos.resets[0]["state"], reconcile.HOLD_STATE)
        self.assertEqual(boskos.acquired, [P7, P8])
        self.assertEqual(boskos.released, [P7, P8])
        self.assertEqual(tofu.verbs().count("apply"), 2)

    def test_the_report_is_written_for_a_pass_a_failure_and_a_termination(self):
        # The CI health bot reads it from the job's artifacts; ARTIFACTS is
        # where Prow's pod utilities upload from, so the default lands there.
        with tempfile.TemporaryDirectory() as tmp:
            report = pathlib.Path(tmp) / "fleet-reconcile.json"
            with mock.patch.dict(os.environ, {reconcile.ARTIFACTS_ENV: tmp}):
                rc, _ = self._main(["--project", P7, "--dry-run"], _Boskos(free=[P7]), _Tofu({P7: UPDATE_ONLY}))
            self.assertEqual(rc, reconcile.EXIT_OK)
            doc = json.loads(report.read_text())
            self.assertEqual((doc["schema_version"], doc["mode"], doc["dry_run"], doc["exit"], doc["exit_code"], doc["error"]), (1, "project", True, "ok", 0, None))
            self.assertEqual(doc["outcomes"][P7]["outcome"], reconcile.OUTCOME_PLANNED)
            self.assertEqual(doc["summary"][reconcile.OUTCOME_PLANNED], 1)
            self.assertTrue(doc["started_at"].endswith("Z") and doc["finished_at"] >= doc["started_at"])
            # A refusal: the exit and the project's reason are in it.
            explicit = pathlib.Path(tmp) / "elsewhere.json"
            rc, _ = self._main(["--project", P7, "--report", str(explicit)], _Boskos(free=[P7]), _Tofu({P7: REPLACE}))
            self.assertEqual(rc, reconcile.EXIT_FAILED)
            doc = json.loads(explicit.read_text())
            self.assertEqual((doc["exit"], doc["outcomes"][P7]["outcome"]), ("failed", reconcile.OUTCOME_REFUSED))
            self.assertIn("1 project(s) not reconciled", doc["error"])
            # No ARTIFACTS and no flag: no report is written anywhere.
            with mock.patch.dict(os.environ, {}, clear=False), mock.patch.object(reconcile, "write_report") as writer:
                os.environ.pop(reconcile.ARTIFACTS_ENV, None)
                self._main(["--project", P7, "--dry-run"], _Boskos(free=[P7]), _Tofu({P7: UPDATE_ONLY}))
            writer.assert_not_called()

    def test_an_unhandled_exception_still_writes_its_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = pathlib.Path(tmp) / "r.json"
            with mock.patch.object(reconcile, "_run", side_effect=KeyError("boom")), mock.patch.object(reconcile.signal, "signal"):
                with self.assertRaises(KeyError):
                    reconcile.main(["--project", P7, "--report", str(report), "--boskos-server", BOSKOS, "--boskos-owner", OWNER])
            doc = json.loads(report.read_text())
            self.assertEqual((doc["exit"], doc["exit_code"], doc["outcomes"]), ("error", None, {}))
            self.assertEqual(doc["error"], "KeyError: 'boom'", "the report names what killed the run")

    def test_a_terminated_run_still_writes_its_report(self):
        def tofu(argv, **_):
            if argv[1] == "plan":
                raise boskos_pool.Terminated("signal 2")
            return subprocess.CompletedProcess(argv, 0, "", "")
        with tempfile.TemporaryDirectory() as tmp:
            report = pathlib.Path(tmp) / "r.json"
            with mock.patch.object(boskos_pool.urllib.request, "urlopen", _Boskos(free=[P7])), mock.patch.object(reconcile, "tofu_runner", tofu), mock.patch.object(reconcile.signal, "signal"), mock.patch("sys.stdout", io.StringIO()), mock.patch("sys.stderr", io.StringIO()):
                rc = reconcile.main(["--project", P7, "--report", str(report), "--boskos-server", BOSKOS, "--boskos-owner", OWNER])
            self.assertEqual(rc, boskos_pool.TERMINATED_EXIT_CODE)
            doc = json.loads(report.read_text())
            self.assertEqual((doc["exit"], doc["exit_code"], doc["outcomes"][P7]["outcome"]), ("terminated", 143, reconcile.OUTCOME_INTERRUPTED))
            self.assertIn("terminated (signal 2)", doc["error"])

    def test_a_busy_pool_exits_zero(self):
        rc, _ = self._main(["--project", P7], _Boskos(free=[]), _Tofu({}))
        self.assertEqual(rc, reconcile.EXIT_OK)

    def test_an_unmapped_project_exits_one(self):
        rc, stderr = self._main(["--project", "kube-agents-evals-99"], _Boskos(free=[]), _Tofu({}))
        self.assertEqual(rc, reconcile.EXIT_FAILED)
        self.assertIn("kube-agents-evals-99", stderr)

    def test_no_lease_without_a_project_is_refused_by_the_parser(self):
        with self.assertRaises(SystemExit), mock.patch("sys.stderr", io.StringIO()):
            reconcile.main(["--all", "--no-lease"])

    def test_a_main_ref_without_a_remote_is_refused_by_the_parser(self):
        # `--stop-when-moved main` would fetch remote "main", branch "", fail
        # every check and leave the guard off with a warning.
        for bad in ("main", "origin/", "/main"):
            with self.assertRaises(SystemExit, msg=bad), mock.patch("sys.stderr", io.StringIO()):
                reconcile.main(["--all", "--stop-when-moved", bad])


class HoldTest(unittest.TestCase):
    def test_a_held_project_is_heartbeat_while_the_apply_runs(self):
        boskos = _Boskos(free=[P7])

        def slow(project):
            time.sleep(0.3)
            return (reconcile.OUTCOME_APPLIED, "")

        with mock.patch.object(boskos_pool, "HEARTBEAT_SECONDS", 0.05), mock.patch.object(
            boskos_pool.urllib.request, "urlopen", boskos
        ):
            boskos_pool.hold(BOSKOS, OWNER, reconcile.HOLD_STATE, P7, slow, {}, heartbeat=True)
        self.assertGreaterEqual(len(boskos.beats), 2)
        self.assertEqual(boskos.beats[0], P7)
        self.assertEqual(boskos.released, [P7], "released after the last beat")

    def test_the_reconciles_own_holds_are_heartbeat(self):
        # Through reconcile_named and reconcile_pool, not hold() alone: a
        # dropped heartbeat=True in either would hand a mid-apply project to
        # the reaper five minutes in.
        def slow_tofu(argv, **_):
            if argv[1] == "apply":
                time.sleep(0.3)
            if argv[1] == "plan":
                return subprocess.CompletedProcess(argv, reconcile.PLAN_HAS_CHANGES, "", "")
            if argv[1] == "show":
                return subprocess.CompletedProcess(argv, 0, UPDATE_ONLY, "")
            return subprocess.CompletedProcess(argv, 0, "", "")

        for run in ("named", "pool"):
            boskos = _Boskos(free=[P7])
            with mock.patch.object(boskos_pool, "HEARTBEAT_SECONDS", 0.05), mock.patch.object(boskos_pool.urllib.request, "urlopen", boskos):
                if run == "named":
                    reconcile.reconcile_named([P7], BOSKOS, OWNER, runner=slow_tofu, known=KNOWN)
                else:
                    reconcile.reconcile_pool(BOSKOS, OWNER, runner=slow_tofu, known=KNOWN)
            self.assertGreaterEqual(len(boskos.beats), 2, run)
            self.assertEqual(boskos.released, [P7], run)

    def test_a_termination_during_the_release_still_releases_and_then_propagates(self):
        # The signal is held back across the release: the project goes back to
        # free first, and the termination is delivered after.
        class _Boskos_signalling_release(_Boskos):
            def __call__(self, request, timeout=None):
                if "/release?" in request.full_url:
                    os.kill(os.getpid(), signal.SIGINT)
                return super().__call__(request, timeout)

        boskos = _Boskos_signalling_release(free=[P7])
        previous = signal.signal(signal.SIGINT, boskos_pool.terminate)
        outcomes = {}
        stdout = io.StringIO()
        try:
            with mock.patch.object(boskos_pool.urllib.request, "urlopen", boskos), mock.patch("sys.stdout", stdout):
                with self.assertRaises(boskos_pool.Terminated):
                    reconcile.reconcile_named([P7], BOSKOS, OWNER, runner=_Tofu({P7: UPDATE_ONLY}), known=KNOWN, outcomes=outcomes)
            self.assertIs(signal.getsignal(signal.SIGINT), boskos_pool.terminate, "the handler is back after the release")
        finally:
            signal.signal(signal.SIGINT, previous)
        self.assertEqual(boskos.released, [P7], "released before the termination was delivered")
        # The apply that happened is on record and on stdout before the raise.
        self.assertEqual(outcomes[P7][0], reconcile.OUTCOME_APPLIED)
        self.assertIn(f"{P7}: applied", stdout.getvalue())

    def test_a_termination_during_the_acquire_releases_the_project_and_then_propagates(self):
        # Between the acquire and the armed finally: deferred, then raised
        # before the visit, so the project is acquired, never applied, and
        # given back.
        class _Boskos_signalling_acquire(_Boskos):
            def __call__(self, request, timeout=None):
                out = super().__call__(request, timeout)
                if "/acquirebystate?" in request.full_url:
                    os.kill(os.getpid(), signal.SIGINT)
                return out

        boskos = _Boskos_signalling_acquire(free=[P7])
        tofu = _Tofu({P7: UPDATE_ONLY})
        previous = signal.signal(signal.SIGINT, boskos_pool.terminate)
        try:
            with mock.patch.object(boskos_pool.urllib.request, "urlopen", boskos):
                with self.assertRaises(boskos_pool.Terminated):
                    reconcile.reconcile_named([P7], BOSKOS, OWNER, runner=tofu, known=KNOWN)
        finally:
            signal.signal(signal.SIGINT, previous)
        self.assertEqual(boskos.acquired, [P7])
        self.assertEqual(boskos.released, [P7], "acquired, then given back")
        self.assertEqual(tofu.calls, [], "never applied")

    def test_a_second_termination_during_the_release_after_one_in_the_acquire_still_releases(self):
        # The first signal lands during the acquire and is raised by the
        # unblock; the release that follows must still run under held signals,
        # so a second signal during it is deferred rather than skipping it.
        seen = []

        class _Boskos_two_signals(_Boskos):
            def __call__(self, request, timeout=None):
                if "/release?" in request.full_url:
                    seen.append(signal.getsignal(signal.SIGINT))
                    os.kill(os.getpid(), signal.SIGINT)
                out = super().__call__(request, timeout)
                if "/acquirebystate?" in request.full_url:
                    os.kill(os.getpid(), signal.SIGINT)
                return out

        boskos = _Boskos_two_signals(free=[P7])
        previous = signal.signal(signal.SIGINT, boskos_pool.terminate)
        try:
            with mock.patch.object(boskos_pool.urllib.request, "urlopen", boskos):
                with self.assertRaises(boskos_pool.Terminated):
                    reconcile.reconcile_named([P7], BOSKOS, OWNER, runner=_Tofu({P7: UPDATE_ONLY}), known=KNOWN)
        finally:
            signal.signal(signal.SIGINT, previous)
        self.assertEqual(boskos.released, [P7], "the release ran despite the second signal")
        self.assertEqual(seen, [boskos_pool._defer], "the release ran under held signals")
        self.assertEqual(boskos_pool._HOLD_DEPTH, 0)

    def test_a_termination_as_the_release_arms_its_deferral_still_releases(self):
        # The moment between the hold's finally starting and its handlers
        # being held: a signal there is raised out of the swap, and the
        # release must run anyway.
        boskos = _Boskos(free=[P7])
        original = boskos_pool._hold_signals
        blocks = []

        def hooked(block):
            if block:
                blocks.append(True)
                if len(blocks) == 2:
                    os.kill(os.getpid(), signal.SIGINT)
                    time.sleep(0.05)
            return original(block)

        previous = signal.signal(signal.SIGINT, boskos_pool.terminate)
        try:
            with mock.patch.object(boskos_pool.urllib.request, "urlopen", boskos), mock.patch.object(
                boskos_pool, "_hold_signals", hooked
            ), mock.patch("sys.stdout", io.StringIO()):
                with self.assertRaises(boskos_pool.Terminated):
                    reconcile.reconcile_named([P7], BOSKOS, OWNER, runner=_Tofu({P7: UPDATE_ONLY}), known=KNOWN)
        finally:
            boskos_pool._DEFERRED.clear()
            boskos_pool._HOLD_DEPTH = 0
            signal.signal(signal.SIGINT, previous)
        self.assertEqual(len(blocks), 2, "the signal landed at the release's block")
        self.assertEqual(boskos.released, [P7], "released although the signal landed before the handlers were held")

    def test_a_signal_held_back_is_raised_on_the_unblock_in_that_frame(self):
        previous = signal.signal(signal.SIGINT, boskos_pool.terminate)
        try:
            boskos_pool._hold_signals(True)
            self.assertIs(signal.getsignal(signal.SIGINT), boskos_pool._defer, "deferred while held")
            os.kill(os.getpid(), signal.SIGINT)
            time.sleep(0.05)
            self.assertEqual(boskos_pool._DEFERRED, [signal.SIGINT])
            with self.assertRaises(boskos_pool.Terminated):
                boskos_pool._hold_signals(False)
            self.assertIs(signal.getsignal(signal.SIGINT), boskos_pool.terminate, "the handler is back")
            self.assertEqual(boskos_pool._DEFERRED, [])
        finally:
            boskos_pool._DEFERRED.clear()
            boskos_pool._HOLD_DEPTH = 0
            signal.signal(signal.SIGINT, previous)

    def test_a_raised_termination_holds_later_ones_until_the_next_unblock(self):
        # The gap between a termination being raised and the code unwinding
        # from it reaching its next deferred region: a second signal there is
        # held, and raised at that region's unblock.
        previous = signal.signal(signal.SIGINT, boskos_pool.terminate)
        try:
            with self.assertRaises(boskos_pool.Terminated):
                boskos_pool.terminate(signal.SIGINT, None)
            self.assertIs(signal.getsignal(signal.SIGINT), boskos_pool._defer, "later signals are held")
            os.kill(os.getpid(), signal.SIGINT)
            time.sleep(0.05)
            self.assertEqual(boskos_pool._DEFERRED, [signal.SIGINT])
            boskos_pool._hold_signals(True)
            self.assertEqual(boskos_pool._HOLD_DEPTH, 1)
            with self.assertRaises(boskos_pool.Terminated):
                boskos_pool._hold_signals(False)
            self.assertIs(signal.getsignal(signal.SIGINT), boskos_pool.terminate, "the handler is back")
            self.assertEqual(boskos_pool._HOLD_DEPTH, 0)
        finally:
            boskos_pool._DEFERRED.clear()
            boskos_pool._HOLD_DEPTH = 0
            signal.signal(signal.SIGINT, previous)

    def test_the_reset_window_outlasts_one_projects_ceiling_and_its_grace(self):
        # A live apply's project must never be reset to free under it.
        minutes = int(reconcile.STRANDED_AFTER.removesuffix("m"))
        self.assertGreater(minutes * 60, reconcile.PROJECT_TIMEOUT_SECONDS + reconcile.INTERRUPT_GRACE_SECONDS)

    def test_one_deadline_covers_every_tofu_call(self):
        # Four commands do not each get the whole ceiling.
        timeouts = []

        def runner(argv, timeout=None, **_):
            timeouts.append(timeout)
            time.sleep(0.05)
            if argv[1] == "plan":
                return subprocess.CompletedProcess(argv, reconcile.PLAN_HAS_CHANGES, "", "")
            if argv[1] == "show":
                return subprocess.CompletedProcess(argv, 0, UPDATE_ONLY, "")
            return subprocess.CompletedProcess(argv, 0, "", "")

        reconcile.reconcile_project(P7, runner=runner, timeout=10)
        self.assertEqual(len(timeouts), 4)
        self.assertTrue(all(later < earlier for earlier, later in zip(timeouts, timeouts[1:])), timeouts)


class TofuRunnerTest(unittest.TestCase):
    """The real runner: the ceiling interrupts tofu rather than leaving it, and a tofu that ignores the interrupt is killed."""

    def test_the_ceiling_interrupts_the_child_and_raises(self):
        # The child installs the default handler itself, so the test does
        # not depend on the disposition it inherited from the runner.
        script = "import signal, time; signal.signal(signal.SIGINT, signal.default_int_handler); time.sleep(30)"
        started = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            reconcile.tofu_runner([sys.executable, "-c", script], timeout=1.0)
        self.assertLess(time.monotonic() - started, 6)

    def test_a_child_that_ignores_the_interrupt_is_killed_after_the_grace(self):
        # Two seconds to install SIG_IGN before the ceiling, and a grace the
        # elapsed time must exceed: the kill arm, not the interrupt arm.
        script = "import signal, time; signal.signal(signal.SIGINT, signal.SIG_IGN); time.sleep(30)"
        started = time.monotonic()
        with mock.patch.object(reconcile, "INTERRUPT_GRACE_SECONDS", 1.0):
            with self.assertRaises(subprocess.TimeoutExpired):
                reconcile.tofu_runner([sys.executable, "-c", script], timeout=2.0)
        elapsed = time.monotonic() - started
        self.assertGreaterEqual(elapsed, 3.0, "the grace was waited out before the kill")
        self.assertLess(elapsed, 8)

    def _stubborn_child(self, tmp):
        """A child that records its pid, ignores SIGINT and sleeps."""
        pidfile = os.path.join(tmp, "pid")
        script = "import os, signal, time; open(%r, 'w').write(str(os.getpid())); signal.signal(signal.SIGINT, signal.SIG_IGN); time.sleep(30)" % pidfile
        return script, pidfile

    def _assert_dead(self, pidfile):
        pid = int(open(pidfile).read())
        with self.assertRaises(ProcessLookupError, msg="the child is still running detached"):
            os.kill(pid, 0)

    def test_a_second_termination_during_the_grace_kills_the_child(self):
        # A second Ctrl-C must not leave tofu running detached while the
        # project goes back to the pool: the child is dead when the runner
        # raises, and not before the second signal.
        with tempfile.TemporaryDirectory() as tmp:
            script, pidfile = self._stubborn_child(tmp)
            previous = signal.signal(signal.SIGINT, boskos_pool.terminate)
            first = threading.Timer(1.5, os.kill, args=(os.getpid(), signal.SIGINT))
            second = threading.Timer(2.5, os.kill, args=(os.getpid(), signal.SIGINT))
            started = time.monotonic()
            try:
                first.start()
                second.start()
                with mock.patch.object(reconcile, "INTERRUPT_GRACE_SECONDS", 20):
                    with self.assertRaises(boskos_pool.Terminated):
                        reconcile.tofu_runner([sys.executable, "-c", script], timeout=30)
            finally:
                first.cancel()
                second.cancel()
                signal.signal(signal.SIGINT, previous)
            elapsed = time.monotonic() - started
            self.assertGreaterEqual(elapsed, 2.5, "the first signal alone does not end it")
            self.assertLess(elapsed, 8, "killed on the second signal, not after the 20 s grace")
            self._assert_dead(pidfile)

    def test_a_termination_during_the_ceilings_grace_is_what_propagates(self):
        # The ceiling fires first, then Prow's SIGINT lands during the grace:
        # the run must stop, not report the project as timed out and walk on.
        with tempfile.TemporaryDirectory() as tmp:
            script, pidfile = self._stubborn_child(tmp)
            previous = signal.signal(signal.SIGINT, boskos_pool.terminate)
            # Two seconds for the child to install SIG_IGN, then the ceiling,
            # then the signal during the grace.
            later = threading.Timer(3.5, os.kill, args=(os.getpid(), signal.SIGINT))
            started = time.monotonic()
            try:
                later.start()
                with mock.patch.object(reconcile, "INTERRUPT_GRACE_SECONDS", 20):
                    with self.assertRaises(boskos_pool.Terminated):
                        reconcile.tofu_runner([sys.executable, "-c", script], timeout=2.0)
            finally:
                later.cancel()
                signal.signal(signal.SIGINT, previous)
            self.assertLess(time.monotonic() - started, 9)
            self._assert_dead(pidfile)

    def test_a_termination_signal_reaches_the_child_before_it_propagates(self):
        # Prow's entrypoint sends SIGINT to this process; with the script's
        # handler installed that is a Terminated, and the child must get its
        # own SIGINT (and the chance to unlock state) before it propagates.
        with tempfile.TemporaryDirectory() as tmp:
            marker = os.path.join(tmp, "interrupted")
            script = (
                "import signal, sys, time\n"
                "signal.signal(signal.SIGINT, lambda *_: (open(%r, 'w').close(), sys.exit(3)))\n"
                "time.sleep(30)\n" % marker
            )
            previous = signal.signal(signal.SIGINT, boskos_pool.terminate)
            # Two seconds for the child to start and install its handler.
            timer = threading.Timer(2.0, os.kill, args=(os.getpid(), signal.SIGINT))
            try:
                timer.start()
                with self.assertRaises(boskos_pool.Terminated):
                    reconcile.tofu_runner([sys.executable, "-c", script], timeout=30)
            finally:
                timer.cancel()
                signal.signal(signal.SIGINT, previous)
            self.assertTrue(os.path.exists(marker), "the child never saw SIGINT")

    def test_a_termination_while_tofu_is_starting_still_interrupts_it(self):
        # A signal during Popen is deferred until the handle exists, then
        # takes the forward-and-kill path rather than leaving a child running.
        # Without the deferral the handler raises before the handle is
        # returned, the runner has nothing to forward to or kill, and the
        # child lives on unreaped: the handle is kept here and the child is
        # asserted exited and reaped when the runner raises.
        real_popen = subprocess.Popen
        handles = []

        def popen_then_signal(*args, **kwargs):
            proc = real_popen(*args, **kwargs)
            handles.append(proc)
            os.kill(os.getpid(), signal.SIGINT)
            return proc

        with tempfile.TemporaryDirectory() as tmp:
            script, _ = self._stubborn_child(tmp)
            previous = signal.signal(signal.SIGINT, boskos_pool.terminate)
            started = time.monotonic()
            try:
                with mock.patch.object(reconcile.subprocess, "Popen", popen_then_signal):
                    with mock.patch.object(reconcile, "INTERRUPT_GRACE_SECONDS", 1.0):
                        with self.assertRaises(boskos_pool.Terminated):
                            reconcile.tofu_runner([sys.executable, "-c", script], timeout=30)
            finally:
                signal.signal(signal.SIGINT, previous)
            self.assertLess(time.monotonic() - started, 6, "the child was interrupted and killed, not left for 30 s")
            self.assertEqual(len(handles), 1)
            self.assertIsNotNone(handles[0].poll(), "the child is still running, detached from the runner")

    def _signal_before_the_first_deferral(self, handles, fired):
        """boskos_pool._hold_signals with a SIGINT sent to this process on the
        first block after the child exists: the gap between the runner
        catching a termination or the ceiling and its first deferred region."""
        original = boskos_pool._hold_signals

        def hooked(block):
            if block and handles and not fired:
                fired.append(True)
                os.kill(os.getpid(), signal.SIGINT)
            return original(block)

        return hooked

    def _run_with_a_signal_in_the_gap(self, timeout, first_signal_after=None):
        real_popen = subprocess.Popen
        handles, fired = [], []

        def popen(*args, **kwargs):
            proc = real_popen(*args, **kwargs)
            handles.append(proc)
            return proc

        with tempfile.TemporaryDirectory() as tmp:
            script, pidfile = self._stubborn_child(tmp)
            previous = signal.signal(signal.SIGINT, boskos_pool.terminate)
            timer = threading.Timer(first_signal_after, os.kill, args=(os.getpid(), signal.SIGINT)) if first_signal_after else None
            started = time.monotonic()
            try:
                if timer:
                    timer.start()
                with mock.patch.object(reconcile.subprocess, "Popen", popen), mock.patch.object(
                    boskos_pool, "_hold_signals", self._signal_before_the_first_deferral(handles, fired)
                ), mock.patch.object(reconcile, "INTERRUPT_GRACE_SECONDS", 20):
                    with self.assertRaises(boskos_pool.Terminated):
                        reconcile.tofu_runner([sys.executable, "-c", script], timeout=timeout)
            finally:
                if timer:
                    timer.cancel()
                boskos_pool._DEFERRED.clear()
                boskos_pool._HOLD_DEPTH = 0
                signal.signal(signal.SIGINT, previous)
            self.assertEqual(fired, [True], "the gap was reached")
            self.assertLess(time.monotonic() - started, 8, "no grace wait, no 30 s child")
            self._assert_dead(pidfile)

    def test_a_second_termination_before_the_forward_is_deferred_and_kills_the_child(self):
        # The first signal is raised out of communicate; the second lands
        # before the forward's deferral begins. It is held and read at the
        # forward as a stop-now: the child is killed, nothing escapes with it
        # running.
        self._run_with_a_signal_in_the_gap(timeout=30, first_signal_after=1.0)

    def test_a_termination_after_the_ceiling_before_the_forward_still_kills_the_child(self):
        # The ceiling is caught, and a signal lands before the forward's
        # deferral begins: it escapes the except body, and the child is killed
        # on the way out rather than left applying under a released project.
        self._run_with_a_signal_in_the_gap(timeout=1.0)

    def test_a_second_termination_while_the_first_is_being_forwarded_kills_the_child(self):
        # A signal in the window between catching the first termination and
        # forwarding it: deferred, read as "stop now", the child is killed and
        # a Terminated propagates, with no grace wait.
        real_popen = subprocess.Popen

        class _Popen(real_popen):
            def send_signal(self, sig):
                os.kill(os.getpid(), signal.SIGINT)
                return super().send_signal(sig)

        with tempfile.TemporaryDirectory() as tmp:
            script, pidfile = self._stubborn_child(tmp)
            previous = signal.signal(signal.SIGINT, boskos_pool.terminate)
            started = time.monotonic()
            try:
                with mock.patch.object(reconcile.subprocess, "Popen", _Popen), mock.patch.object(reconcile, "INTERRUPT_GRACE_SECONDS", 20):
                    with self.assertRaises(boskos_pool.Terminated):
                        reconcile.tofu_runner([sys.executable, "-c", script], timeout=2.0)
            finally:
                signal.signal(signal.SIGINT, previous)
            self.assertLess(time.monotonic() - started, 8, "no grace wait after a stop-now")
            self._assert_dead(pidfile)

    def test_a_finished_child_is_returned_with_its_output(self):
        result = reconcile.tofu_runner([sys.executable, "-c", "print('hi')"], timeout=10)
        self.assertEqual((result.returncode, result.stdout.strip()), (0, "hi"))

    def test_the_child_runs_in_its_own_session(self):
        # A terminal's Ctrl-C goes to the foreground process group; tofu in
        # its own session sees only the one interrupt this process forwards.
        result = reconcile.tofu_runner([sys.executable, "-c", "import os; print(os.getsid(0))"], timeout=10)
        self.assertNotEqual(int(result.stdout.strip()), os.getsid(0))


# ---------------------------------------------------------------------------
# Apply-on-merge: the run budget, the by-name pass over the pool, the stop
# when main moves, the re-stamp outcome, the allowlist, the traceable report
# and the workers. Each guard below was seen red before its code was written.

SEEDED_B = "google_container_cluster.seeded_b"
EXCLUSION = {"maintenance_policy": [{"maintenance_exclusion": [{"exclusion_name": "hold-the-minor-lag", "start_time": "2026-10-01T00:00:00Z", "end_time": "2026-12-30T00:00:00Z"}]}], "min_master_version": "1.34.11", "name": "fleet-seeded-b"}


def _restamped(before=EXCLUSION, **after_changes):
    after = json.loads(json.dumps(before))
    after["maintenance_policy"][0]["maintenance_exclusion"][0]["start_time"] = "2026-10-05T00:00:00Z"
    after["maintenance_policy"][0]["maintenance_exclusion"][0]["end_time"] = "2027-01-03T00:00:00Z"
    after.update(after_changes)
    return after


def _plan_with_diff(address, before, after, actions=("update",), after_unknown=None, extra=()):
    changes = [{"address": address, "change": {"actions": list(actions), "before": before, "after": after, "after_unknown": after_unknown or {}}}]
    changes += [{"address": a, "change": {"actions": list(acts)}} for acts, a in extra]
    return json.dumps({"resource_changes": changes})


RESTAMP_ONLY = _plan_with_diff(SEEDED_B, EXCLUSION, _restamped())
RESTAMP_AND_UPGRADE = _plan_with_diff(SEEDED_B, EXCLUSION, _restamped(min_master_version="1.34.12"))
RESTAMP_ELSEWHERE = _plan_with_diff("google_container_cluster.seeded_a", EXCLUSION, _restamped())
RESTAMP_UNKNOWN_FIELD = _plan_with_diff(SEEDED_B, EXCLUSION, _restamped(), after_unknown={"node_version": True})
RESTAMP_PLUS_CREATE = _plan_with_diff(SEEDED_B, EXCLUSION, _restamped(), extra=((["create"], "google_compute_disk.orphan"),))


def _allow(triples):
    return [reconcile.AllowEntry(address, why, bool(standing)) for address, why, standing in triples]


class _Clock:
    """A monotonic clock the tests move by hand."""

    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def _tofu_taking(seconds, clock, plans):
    """A _Tofu whose apply advances the fake clock by `seconds`."""
    inner = _Tofu(plans)

    def runner(argv, **kw):
        if argv[1] == "apply":
            clock.now += seconds
        return inner(argv, **kw)

    return runner


class BudgetTest(unittest.TestCase):
    """A run never starts a project it cannot finish inside its budget."""

    def test_a_project_is_not_started_with_less_than_a_ceiling_left(self):
        clock = _Clock()
        tofu = _tofu_taking(50, clock, {P7: UPDATE_ONLY, P8: UPDATE_ONLY})
        boskos = _Boskos(free=[P7, P8])
        with mock.patch.object(reconcile, "clock", clock), mock.patch.object(boskos_pool.urllib.request, "urlopen", boskos):
            run = reconcile.Run(budget_seconds=100, ceiling_seconds=60)
            outcomes = reconcile.reconcile_pool(BOSKOS, OWNER, runner=tofu, known=KNOWN, run=run)
        self.assertEqual(outcomes[P7][0], reconcile.OUTCOME_APPLIED)
        self.assertEqual(outcomes[P8][0], reconcile.OUTCOME_NOT_REACHED)
        self.assertIn("budget", outcomes[P8][1])
        self.assertEqual(boskos.acquired, [P7], "the project that was not started was never leased")

    def test_without_a_budget_every_project_is_started(self):
        clock = _Clock()
        tofu = _tofu_taking(5000, clock, {P7: UPDATE_ONLY, P8: UPDATE_ONLY})
        boskos = _Boskos(free=[P7, P8])
        with mock.patch.object(reconcile, "clock", clock), mock.patch.object(boskos_pool.urllib.request, "urlopen", boskos):
            outcomes = reconcile.reconcile_pool(BOSKOS, OWNER, runner=tofu, known=KNOWN, run=reconcile.Run())
        self.assertEqual({p: o for p, (o, _) in outcomes.items()}, {P7: reconcile.OUTCOME_APPLIED, P8: reconcile.OUTCOME_APPLIED})

    def test_the_per_project_deadline_is_the_ceiling(self):
        timeouts = []

        def runner(argv, timeout=None, **_):
            timeouts.append(timeout)
            if argv[1] == "plan":
                return subprocess.CompletedProcess(argv, reconcile.PLAN_HAS_CHANGES, "", "")
            if argv[1] == "show":
                return subprocess.CompletedProcess(argv, 0, UPDATE_ONLY, "")
            return subprocess.CompletedProcess(argv, 0, "", "")

        boskos = _Boskos(free=[P7])
        with mock.patch.object(boskos_pool.urllib.request, "urlopen", boskos):
            reconcile.reconcile_pool(BOSKOS, OWNER, runner=runner, known={P7}, run=reconcile.Run(budget_seconds=7200, ceiling_seconds=300))
        self.assertTrue(timeouts and all(t <= 300 for t in timeouts), timeouts)

    def test_not_reached_is_counted_and_is_not_a_failure(self):
        clock = _Clock()
        tofu = _tofu_taking(50, clock, {P7: UPDATE_ONLY, P8: UPDATE_ONLY})
        boskos = _Boskos(free=[P7, P8])
        stdout = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            report = pathlib.Path(tmp) / "r.json"
            with mock.patch.object(reconcile, "clock", clock), mock.patch.object(boskos_pool.urllib.request, "urlopen", boskos), mock.patch.object(
                reconcile, "tofu_runner", tofu
            ), mock.patch.object(reconcile.signal, "signal"), mock.patch.object(reconcile, "pool_projects", lambda *a, **k: set(KNOWN)), mock.patch(
                "sys.stdout", stdout
            ), mock.patch("sys.stderr", io.StringIO()):
                rc = reconcile.main(["--all", "--budget-seconds", "100", "--project-ceiling-seconds", "60", "--report", str(report), "--boskos-server", BOSKOS, "--boskos-owner", OWNER])
            doc = json.loads(report.read_text())
        self.assertEqual(rc, reconcile.EXIT_OK)
        self.assertEqual(doc["summary"][reconcile.OUTCOME_NOT_REACHED], 1)
        self.assertEqual((doc["budget_seconds"], doc["ceiling_seconds"]), (100, 60))
        self.assertIn("1 not reached", stdout.getvalue())


class PassTest(unittest.TestCase):
    """`--all` asks for every mapped project by name and keeps asking for the busy ones."""

    def test_every_mapped_project_is_asked_for_by_name_and_the_pool_is_never_walked(self):
        boskos = _Boskos(free=[P8, P7])
        tofu = _Tofu({P7: UPDATE_ONLY, P8: UPDATE_ONLY})
        with mock.patch.object(boskos_pool.urllib.request, "urlopen", boskos):
            outcomes = reconcile.reconcile_pool(BOSKOS, OWNER, runner=tofu, known=KNOWN, run=reconcile.Run())
        self.assertEqual(boskos.acquired, [P7, P8], "sorted, by name")
        self.assertEqual(boskos.walked, 0, "no /acquire of whatever is free")
        self.assertEqual(sorted(boskos.released), [P7, P8])
        self.assertEqual({p: o for p, (o, _) in outcomes.items()}, {P7: reconcile.OUTCOME_APPLIED, P8: reconcile.OUTCOME_APPLIED})

    def test_a_busy_project_is_asked_for_again_until_it_is_free(self):
        boskos = _Boskos(free=[P7])
        pauses = []

        def pause(seconds):
            pauses.append(seconds)
            boskos.free.append(P8)

        tofu = _Tofu({P7: UPDATE_ONLY, P8: UPDATE_ONLY})
        with mock.patch.object(boskos_pool.urllib.request, "urlopen", boskos), mock.patch.object(reconcile, "pause", pause):
            outcomes = reconcile.reconcile_pool(BOSKOS, OWNER, runner=tofu, known=KNOWN, run=reconcile.Run(budget_seconds=7200))
        self.assertEqual(outcomes[P8][0], reconcile.OUTCOME_APPLIED)
        self.assertEqual(pauses, [reconcile.POLL_INTERVAL_SECONDS])

    def test_a_project_still_busy_when_the_budget_ends_is_not_reached(self):
        clock = _Clock()
        boskos = _Boskos(free=[P7])

        def pause(seconds):
            clock.now += seconds

        tofu = _Tofu({P7: UPDATE_ONLY})
        with mock.patch.object(reconcile, "clock", clock), mock.patch.object(boskos_pool.urllib.request, "urlopen", boskos), mock.patch.object(reconcile, "pause", pause):
            outcomes = reconcile.reconcile_pool(BOSKOS, OWNER, runner=tofu, known=KNOWN, run=reconcile.Run(budget_seconds=400, ceiling_seconds=60))
        self.assertEqual(outcomes[P7][0], reconcile.OUTCOME_APPLIED)
        self.assertEqual(outcomes[P8][0], reconcile.OUTCOME_NOT_REACHED)
        self.assertIn("not free", outcomes[P8][1])
        self.assertLessEqual(clock.now, 400)

    def test_a_registration_outside_the_mapping_is_never_asked_for(self):
        boskos = _Boskos(free=["kube-agents-evals-99", P7])
        tofu = _Tofu({P7: UPDATE_ONLY})
        with mock.patch.object(boskos_pool.urllib.request, "urlopen", boskos):
            outcomes = reconcile.reconcile_pool(BOSKOS, OWNER, runner=tofu, known={P7}, run=reconcile.Run())
        self.assertEqual(list(outcomes), [P7])
        self.assertEqual(boskos.acquired, [P7])


def _git_whose_fetch_fails(args):
    if args[:1] == ["fetch"]:
        raise reconcile.ReconcileError("fetch: could not resolve host")
    return "tree-aaa" if args[1].startswith("HEAD:") else "commit-111"


class MainMovedTest(unittest.TestCase):
    """A run applies the tree it started with, and stops when main's fleet tree is no longer that one."""

    def _git(self, trees):
        calls = []

        def git(args):
            calls.append(list(args))
            if args[:1] == ["fetch"]:
                return ""
            if args[:1] == ["rev-parse"] and args[1].startswith("FETCH_HEAD:"):
                return trees.pop(0) if len(trees) > 1 else trees[0]
            if args[:1] == ["rev-parse"] and args[1] == "HEAD:bench/tf/fleet":
                return "tree-aaa"
            if args[:1] == ["rev-parse"] and args[1] == "HEAD":
                return "commit-111"
            raise AssertionError(args)

        git.calls = calls
        return git

    def test_the_run_stops_when_the_fleet_tree_on_main_moves(self):
        git = self._git(["tree-aaa", "tree-bbb"])
        boskos = _Boskos(free=[P7, P8])
        tofu = _Tofu({P7: UPDATE_ONLY, P8: UPDATE_ONLY})
        with mock.patch.object(reconcile, "git_output", git), mock.patch.object(boskos_pool.urllib.request, "urlopen", boskos), mock.patch.object(reconcile, "MAIN_CHECK_INTERVAL_SECONDS", 0):
            run = reconcile.Run(main_ref="origin/main")
            outcomes = reconcile.reconcile_pool(BOSKOS, OWNER, runner=tofu, known=KNOWN, run=run)
        self.assertEqual(outcomes[P7][0], reconcile.OUTCOME_APPLIED)
        self.assertEqual(outcomes[P8][0], reconcile.OUTCOME_NOT_REACHED)
        self.assertIn("tree-bbb", outcomes[P8][1])
        self.assertIn(["fetch", "--quiet", "--depth=1", "origin", "main"], git.calls)
        self.assertEqual(boskos.acquired, [P7])

    def test_a_fetch_that_fails_is_a_warning_not_a_stop(self):
        boskos = _Boskos(free=[P7, P8])
        tofu = _Tofu({P7: UPDATE_ONLY, P8: UPDATE_ONLY})
        stderr = io.StringIO()
        with mock.patch.object(reconcile, "git_output", _git_whose_fetch_fails), mock.patch.object(boskos_pool.urllib.request, "urlopen", boskos), mock.patch.object(reconcile, "MAIN_CHECK_INTERVAL_SECONDS", 0), mock.patch("sys.stderr", stderr):
            outcomes = reconcile.reconcile_pool(BOSKOS, OWNER, runner=tofu, known=KNOWN, run=reconcile.Run(main_ref="origin/main"))
        self.assertEqual({p: o for p, (o, _) in outcomes.items()}, {P7: reconcile.OUTCOME_APPLIED, P8: reconcile.OUTCOME_APPLIED})
        self.assertIn("could not resolve host", stderr.getvalue())

    def test_without_a_main_ref_git_is_never_fetched(self):
        git = self._git(["tree-aaa"])
        boskos = _Boskos(free=[P7])
        with mock.patch.object(reconcile, "git_output", git), mock.patch.object(boskos_pool.urllib.request, "urlopen", boskos):
            reconcile.reconcile_pool(BOSKOS, OWNER, runner=_Tofu({P7: UPDATE_ONLY}), known={P7}, run=reconcile.Run())
        self.assertFalse(any(c[:1] == ["fetch"] for c in git.calls))


class ConvergedTest(unittest.TestCase):
    """A plan whose only change is seeded-b's exclusion re-stamp is `converged`, decided on the changed fields, not the address."""

    def test_a_restamp_only_plan_is_applied_and_reported_converged(self):
        tofu = _Tofu({P7: RESTAMP_ONLY})
        outcome, detail = reconcile.reconcile_project(P7, runner=tofu)
        self.assertEqual(outcome, reconcile.OUTCOME_CONVERGED)
        self.assertIn("re-stamp", detail)
        self.assertEqual(tofu.verbs(), ["init", "plan", "show", "apply"], "the re-stamp is still applied")

    def test_a_restamp_with_a_version_change_on_the_same_address_is_applied_not_converged(self):
        outcome, _ = reconcile.reconcile_project(P7, runner=_Tofu({P7: RESTAMP_AND_UPGRADE}))
        self.assertEqual(outcome, reconcile.OUTCOME_APPLIED)

    def test_a_restamp_shaped_change_on_another_address_is_applied(self):
        outcome, _ = reconcile.reconcile_project(P7, runner=_Tofu({P7: RESTAMP_ELSEWHERE}))
        self.assertEqual(outcome, reconcile.OUTCOME_APPLIED)

    def test_an_unknown_after_value_counts_as_a_changed_field(self):
        outcome, _ = reconcile.reconcile_project(P7, runner=_Tofu({P7: RESTAMP_UNKNOWN_FIELD}))
        self.assertEqual(outcome, reconcile.OUTCOME_APPLIED)

    def test_a_restamp_beside_any_other_change_is_applied(self):
        outcome, _ = reconcile.reconcile_project(P7, runner=_Tofu({P7: RESTAMP_PLUS_CREATE}))
        self.assertEqual(outcome, reconcile.OUTCOME_APPLIED)

    def test_an_update_without_before_and_after_is_applied(self):
        outcome, _ = reconcile.reconcile_project(P7, runner=_Tofu({P7: UPDATE_ONLY}))
        self.assertEqual(outcome, reconcile.OUTCOME_APPLIED)

    def test_a_dry_run_of_a_restamp_says_so_and_applies_nothing(self):
        tofu = _Tofu({P7: RESTAMP_ONLY})
        outcome, detail = reconcile.reconcile_project(P7, runner=tofu, dry_run=True)
        self.assertEqual(outcome, reconcile.OUTCOME_PLANNED)
        self.assertIn("re-stamp", detail)
        self.assertNotIn("apply", tofu.verbs())

    def test_converged_is_not_a_failure_and_is_counted(self):
        self.assertNotIn(reconcile.OUTCOME_CONVERGED, reconcile.FAILING_OUTCOMES)
        self.assertNotIn(reconcile.OUTCOME_NOT_REACHED, reconcile.FAILING_OUTCOMES)
        boskos = _Boskos(free=[P7])
        stdout = io.StringIO()
        with mock.patch.object(boskos_pool.urllib.request, "urlopen", boskos), mock.patch("sys.stdout", stdout):
            outcomes = reconcile.reconcile_pool(BOSKOS, OWNER, runner=_Tofu({P7: RESTAMP_ONLY}), known={P7}, run=reconcile.Run())
            failing = reconcile.report(outcomes)
        self.assertEqual(failing, [])
        self.assertIn("1 converged", stdout.getvalue())


class AllowlistTest(unittest.TestCase):
    """What a re-apply may delete or replace is declared in the fleet directory, not in this script."""

    def test_the_committed_allowlist_carries_the_no_surge_pool_as_a_standing_entry(self):
        entries = reconcile.load_allowlist(reconcile.ALLOWLIST_FILE)
        standing = [e for e in entries if e.standing]
        self.assertEqual([e.address for e in standing], ["google_container_node_pool.no_surge_pool"])
        self.assertTrue(all(e.why for e in entries), "every entry says why")
        self.assertFalse(hasattr(reconcile, "REPLACE_ALLOWED_ADDRESSES"), "one list, in the file")

    def test_a_delete_on_a_listed_address_is_applied_and_counted(self):
        allow = _allow([("google_compute_disk.orphan", "revert of the cost fixture", False)])
        tofu = _Tofu({P7: DELETE})
        outcome, detail = reconcile.reconcile_project(P7, runner=tofu, allow=allow)
        self.assertEqual(outcome, reconcile.OUTCOME_APPLIED)
        self.assertEqual(detail, "0 to add, 0 to change, 0 to replace, 1 to destroy, 0 refused")
        self.assertIn("apply", tofu.verbs())

    def test_a_delete_on_an_unlisted_address_is_still_refused(self):
        allow = _allow([("google_compute_disk.other", "something else", False)])
        outcome, detail = reconcile.reconcile_project(P7, runner=_Tofu({P7: DELETE}), allow=allow)
        self.assertEqual(outcome, reconcile.OUTCOME_REFUSED)
        self.assertIn("delete google_compute_disk.orphan", detail)

    def test_a_replace_on_a_listed_address_is_applied(self):
        allow = _allow([("google_container_node_pool.seeded_a_idle", "one-time rebuild", False)])
        outcome, _ = reconcile.reconcile_project(P7, runner=_Tofu({P7: REPLACE}), allow=allow)
        self.assertEqual(outcome, reconcile.OUTCOME_APPLIED)

    def test_a_forget_is_refused_even_when_listed(self):
        allow = _allow([("google_compute_disk.orphan", "x", False)])
        outcome, _ = reconcile.reconcile_project(P7, runner=_Tofu({P7: FORGET}), allow=allow)
        self.assertEqual(outcome, reconcile.OUTCOME_REFUSED)

    def test_an_entry_the_plan_did_not_need_is_reported_unused_and_a_standing_one_is_not(self):
        allow = _allow([("google_compute_disk.gone", "an old revert", False), ("google_container_node_pool.no_surge_pool", "minor roll", True)])
        boskos = _Boskos(free=[P7])
        run = reconcile.Run(allow=allow)
        with mock.patch.object(boskos_pool.urllib.request, "urlopen", boskos):
            reconcile.reconcile_pool(BOSKOS, OWNER, runner=_Tofu({P7: UPDATE_ONLY}), known={P7}, run=run)
        self.assertEqual(run.extras[P7]["allowlist_unused"], ["google_compute_disk.gone"])

    def test_an_entry_the_plan_used_is_not_reported(self):
        allow = _allow([("google_compute_disk.orphan", "revert", False)])
        boskos = _Boskos(free=[P7])
        run = reconcile.Run(allow=allow)
        with mock.patch.object(boskos_pool.urllib.request, "urlopen", boskos):
            reconcile.reconcile_pool(BOSKOS, OWNER, runner=_Tofu({P7: DELETE}), known={P7}, run=run)
        self.assertEqual(run.extras[P7]["allowlist_unused"], [])

    def test_a_malformed_allowlist_is_refused_before_anything_is_leased(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "allow.json"
            for bad in ('{"address": "x"}', '[{"address": "x"}]', '[{"why": "no address"}]', "not json"):
                path.write_text(bad)
                with self.assertRaises(reconcile.ReconcileError, msg=bad):
                    reconcile.load_allowlist(path)
            # `standing` decides whether an entry is ever reported unused, so a
            # string "false" (truthy) must be refused, not read as permanent;
            # an address that is not a resource address can never match; an
            # unknown key is a typo of one of the three.
            for bad in ('[{"address": "google_compute_disk.orphan", "why": "x", "standing": "false"}]', '[{"address": "not an address", "why": "x"}]', '[{"address": "google_compute_disk.orphan", "why": "x", "permanent": true}]'):
                path.write_text(bad)
                with self.assertRaises(reconcile.ReconcileError, msg=bad):
                    reconcile.load_allowlist(path)
            path.write_text(json.dumps([{"address": "google_compute_disk.orphan", "why": "revert"}, {"address": 'module.fleet.kubernetes_network_policy_v1.default_deny["token"]', "why": "revert", "standing": False}]))
            entries = reconcile.load_allowlist(path)
            self.assertEqual((entries[0].address, entries[0].why, entries[0].standing), ("google_compute_disk.orphan", "revert", False))
            self.assertEqual(entries[1].address, 'module.fleet.kubernetes_network_policy_v1.default_deny["token"]')

    def test_a_missing_allowlist_file_means_nothing_may_be_destroyed(self):
        self.assertEqual(reconcile.load_allowlist(pathlib.Path("/nonexistent/allow.json")), [])


class ReportFieldsTest(unittest.TestCase):
    """The report says what was applied, where, when, from which commit; each project keeps a marker in its state bucket."""

    def _git(self, args):
        if args == ["rev-parse", "HEAD"]:
            return "commit-111"
        if args == ["rev-parse", "HEAD:bench/tf/fleet"]:
            return "tree-aaa"
        raise AssertionError(args)

    def test_the_report_carries_the_commit_the_tree_the_times_and_the_visited_count(self):
        boskos = _Boskos(free=[P7])
        published = []
        with tempfile.TemporaryDirectory() as tmp:
            report = pathlib.Path(tmp) / "r.json"
            with mock.patch.object(reconcile, "git_output", self._git), mock.patch.object(boskos_pool.urllib.request, "urlopen", boskos), mock.patch.object(
                reconcile, "tofu_runner", _Tofu({P7: UPDATE_ONLY})
            ), mock.patch.object(reconcile, "publish_applied", lambda *a, **k: published.append(a) or None), mock.patch.object(reconcile.signal, "signal"), mock.patch.object(
                reconcile, "pool_projects", lambda *a, **k: set(KNOWN)
            ), mock.patch("sys.stdout", io.StringIO()), mock.patch("sys.stderr", io.StringIO()), mock.patch.dict(os.environ, {"BUILD_ID": "123", "JOB_NAME": "post-x"}):
                rc = reconcile.main(["--all", "--workers", "1", "--report", str(report), "--boskos-server", BOSKOS, "--boskos-owner", OWNER])
            doc = json.loads(report.read_text())
        self.assertEqual(rc, reconcile.EXIT_OK)
        self.assertEqual((doc["commit"], doc["fleet_tree"], doc["workers"], doc["build"], doc["job"]), ("commit-111", "tree-aaa", 1, "123", "post-x"))
        self.assertEqual((doc["visited"], doc["mapped"]), (1, 2))
        # No budget was given, so the busy project is busy, as a hand run reports it.
        self.assertEqual(doc["outcomes"][P8]["outcome"], reconcile.OUTCOME_BUSY)
        entry = doc["outcomes"][P7]
        self.assertTrue(entry["started_at"].endswith("Z") and entry["finished_at"] >= entry["started_at"])
        self.assertEqual(entry["allowlist_unused"], [])
        self.assertEqual(doc["summary"][reconcile.OUTCOME_BUSY], 1)
        self.assertEqual(len(published), 1)

    def test_an_applied_project_gets_a_marker_in_its_state_bucket(self):
        copies = []

        def gcloud(argv, **kw):
            copies.append((list(argv), kw.get("input")))
            return subprocess.CompletedProcess(argv, 0, "", "")

        run = reconcile.Run(commit="commit-111", fleet_tree="tree-aaa", build="123", job="post-x")
        with mock.patch.object(reconcile, "gcloud_runner", gcloud):
            warning = reconcile.publish_applied(P7, reconcile.OUTCOME_APPLIED, run)
        self.assertIsNone(warning)
        self.assertEqual(len(copies), 1)
        argv, stdin = copies[0]
        self.assertEqual(argv, ["gcloud", "storage", "cp", "-", f"gs://{P7}-tf-state/seeded-fleet/applied.json"])
        body = json.loads(stdin)
        self.assertEqual((body["commit"], body["fleet_tree"], body["build"], body["job"], body["outcome"]), ("commit-111", "tree-aaa", "123", "post-x", "applied"))
        self.assertTrue(body["finished_at"].endswith("Z"))

    def test_a_failed_marker_write_is_a_warning_on_the_outcome_not_a_failure(self):
        def gcloud(argv, **kw):
            return subprocess.CompletedProcess(argv, 1, "", "AccessDeniedException: 403")

        boskos = _Boskos(free=[P7])
        run = reconcile.Run(commit="c", fleet_tree="t", publish=True)
        with mock.patch.object(reconcile, "gcloud_runner", gcloud), mock.patch.object(boskos_pool.urllib.request, "urlopen", boskos):
            outcomes = reconcile.reconcile_pool(BOSKOS, OWNER, runner=_Tofu({P7: UPDATE_ONLY}), known={P7}, run=run)
        self.assertEqual(outcomes[P7][0], reconcile.OUTCOME_APPLIED)
        self.assertIn("applied.json", outcomes[P7][1])
        self.assertIn("403", outcomes[P7][1])

    def test_no_marker_is_written_on_a_dry_run_or_a_refusal(self):
        copies = []

        def gcloud(argv, **kw):
            copies.append(argv)
            return subprocess.CompletedProcess(argv, 0, "", "")

        for plan, dry in ((UPDATE_ONLY, True), (REPLACE, False)):
            boskos = _Boskos(free=[P7])
            with mock.patch.object(reconcile, "gcloud_runner", gcloud), mock.patch.object(boskos_pool.urllib.request, "urlopen", boskos):
                reconcile.reconcile_pool(BOSKOS, OWNER, runner=_Tofu({P7: plan}), known={P7}, dry_run=dry, run=reconcile.Run(commit="c", fleet_tree="t", publish=True))
        self.assertEqual(copies, [])


class WorkersTest(unittest.TestCase):
    """N projects at once, each under its own lease; a termination still releases every one."""

    def test_two_workers_hold_two_projects_at_once(self):
        live, peak, lock = [0], [0], threading.Lock()

        def tofu(argv, **_):
            if argv[1] == "apply":
                with lock:
                    live[0] += 1
                    peak[0] = max(peak[0], live[0])
                time.sleep(0.3)
                with lock:
                    live[0] -= 1
            if argv[1] == "plan":
                return subprocess.CompletedProcess(argv, reconcile.PLAN_HAS_CHANGES, "", "")
            if argv[1] == "show":
                return subprocess.CompletedProcess(argv, 0, UPDATE_ONLY, "")
            return subprocess.CompletedProcess(argv, 0, "", "")

        boskos = _Boskos(free=[P7, P8])
        with mock.patch.object(boskos_pool.urllib.request, "urlopen", boskos):
            outcomes = reconcile.reconcile_pool(BOSKOS, OWNER, runner=tofu, known=KNOWN, run=reconcile.Run(workers=2))
        self.assertEqual(peak[0], 2, "both applies ran at the same time")
        self.assertEqual(sorted(boskos.released), [P7, P8])
        self.assertEqual({p: o for p, (o, _) in outcomes.items()}, {P7: reconcile.OUTCOME_APPLIED, P8: reconcile.OUTCOME_APPLIED})

    def test_hold_signals_is_a_no_op_off_the_main_thread(self):
        before = signal.getsignal(signal.SIGINT)
        errors = []

        def body():
            try:
                boskos_pool._hold_signals(True)
                boskos_pool._hold_signals(False)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        t = threading.Thread(target=body)
        t.start()
        t.join()
        self.assertEqual(errors, [])
        self.assertIs(signal.getsignal(signal.SIGINT), before)
        self.assertEqual(boskos_pool._HOLD_DEPTH, 0)

    def test_a_termination_with_workers_interrupts_every_apply_and_releases_every_project(self):
        started = threading.Barrier(3)

        def tofu(argv, **_):
            if argv[1] == "apply":
                started.wait(timeout=5)
                # The fake stands in for a tofu that exits on the interrupt.
                for _ in range(100):
                    if reconcile.terminating():
                        return subprocess.CompletedProcess(argv, 130, "", "interrupted")
                    time.sleep(0.02)
                return subprocess.CompletedProcess(argv, 0, "", "")
            if argv[1] == "plan":
                return subprocess.CompletedProcess(argv, reconcile.PLAN_HAS_CHANGES, "", "")
            if argv[1] == "show":
                return subprocess.CompletedProcess(argv, 0, UPDATE_ONLY, "")
            return subprocess.CompletedProcess(argv, 0, "", "")

        # Three mapped projects for two workers: the third must never start
        # once the termination has landed, and the report must say so.
        p9 = "kube-agents-evals-9"
        boskos = _Boskos(free=[P7, P8, p9])
        previous = signal.signal(signal.SIGINT, boskos_pool.terminate)
        outcomes = {}

        def fire():
            started.wait(timeout=5)
            os.kill(os.getpid(), signal.SIGINT)

        try:
            threading.Thread(target=fire, daemon=True).start()
            with mock.patch.object(boskos_pool.urllib.request, "urlopen", boskos), mock.patch("sys.stdout", io.StringIO()):
                with self.assertRaises(boskos_pool.Terminated):
                    reconcile.reconcile_pool(BOSKOS, OWNER, runner=tofu, known=KNOWN | {p9}, run=reconcile.Run(workers=2), outcomes=outcomes)
        finally:
            signal.signal(signal.SIGINT, previous)
            reconcile._TERMINATING.clear()
        self.assertEqual(sorted(boskos.released), [P7, P8])
        self.assertEqual(sorted(boskos.acquired), [P7, P8], "no project is leased after the termination")
        self.assertEqual({p: o for p, (o, _) in outcomes.items()}, {P7: reconcile.OUTCOME_INTERRUPTED, P8: reconcile.OUTCOME_INTERRUPTED, p9: reconcile.OUTCOME_NOT_REACHED})
        self.assertTrue(all("force-unlock" in d for p, (_, d) in outcomes.items() if p != p9))
        self.assertIn("terminated", outcomes[p9][1])

    def test_the_drain_waits_on_the_workers_not_on_thread_join(self):
        # Python 3.12's Thread.join marks a thread stopped when the join is
        # interrupted by an exception, which is what the termination does to
        # the main thread; a drain built on join then returns before the
        # workers have released their projects. A join that answers at once,
        # as a stopped thread's would, must not shorten the drain.
        started = threading.Barrier(3)

        def tofu(argv, **_):
            if argv[1] == "apply":
                started.wait(timeout=5)
                for _ in range(100):
                    if reconcile.terminating():
                        time.sleep(0.2)
                        return subprocess.CompletedProcess(argv, 130, "", "interrupted")
                    time.sleep(0.02)
                return subprocess.CompletedProcess(argv, 0, "", "")
            if argv[1] == "plan":
                return subprocess.CompletedProcess(argv, reconcile.PLAN_HAS_CHANGES, "", "")
            if argv[1] == "show":
                return subprocess.CompletedProcess(argv, 0, UPDATE_ONLY, "")
            return subprocess.CompletedProcess(argv, 0, "", "")

        boskos = _Boskos(free=[P7, P8])
        previous = signal.signal(signal.SIGINT, boskos_pool.terminate)
        outcomes = {}

        def fire():
            started.wait(timeout=5)
            os.kill(os.getpid(), signal.SIGINT)

        try:
            threading.Thread(target=fire, daemon=True).start()
            with mock.patch.object(threading.Thread, "join", lambda self, timeout=None: None), mock.patch.object(
                boskos_pool.urllib.request, "urlopen", boskos
            ), mock.patch("sys.stdout", io.StringIO()):
                with self.assertRaises(boskos_pool.Terminated):
                    reconcile.reconcile_pool(BOSKOS, OWNER, runner=tofu, known=KNOWN, run=reconcile.Run(workers=2), outcomes=outcomes)
        finally:
            signal.signal(signal.SIGINT, previous)
            reconcile._TERMINATING.clear()
        self.assertEqual(sorted(boskos.released), [P7, P8], "both holds were released before the termination propagated")

    def test_no_tofu_is_started_once_a_termination_has_landed(self):
        # The forward reaches the children alive at that instant; a worker
        # between two steps, or just out of its acquire, must not start the
        # next one under the drain timer.
        calls = []
        reconcile._TERMINATING.set()
        try:
            with self.assertRaises(boskos_pool.Terminated):
                reconcile._tofu(["plan"], lambda argv, **_: calls.append(argv), reconcile.clock() + 60)
        finally:
            reconcile._TERMINATING.clear()
        self.assertEqual(calls, [], "no child was spawned")

    def test_a_worker_between_steps_when_the_termination_lands_records_interrupted_and_plans_nothing(self):
        # P7 is mid-apply; P8 is still in its init when the signal fires. P8
        # must come back interrupted with no plan run, and both released.
        started = threading.Barrier(3)
        verbs = {P7: [], P8: []}

        def tofu(argv, **_):
            project = next(a.split("=", 2)[2].removesuffix("-tf-state") for a in argv if a.startswith("-backend-config=bucket=")) if argv[1] == "init" else tofu.current.get(threading.get_ident())
            if argv[1] == "init":
                tofu.current[threading.get_ident()] = project
            verbs[project].append(argv[1])
            if project == P8 and argv[1] == "init":
                started.wait(timeout=5)
                while not reconcile.terminating():
                    time.sleep(0.02)
                time.sleep(0.1)
                return subprocess.CompletedProcess(argv, 0, "", "")
            if project == P7 and argv[1] == "apply":
                started.wait(timeout=5)
                while not reconcile.terminating():
                    time.sleep(0.02)
                return subprocess.CompletedProcess(argv, 130, "", "interrupted")
            if argv[1] == "plan":
                return subprocess.CompletedProcess(argv, reconcile.PLAN_HAS_CHANGES, "", "")
            if argv[1] == "show":
                return subprocess.CompletedProcess(argv, 0, UPDATE_ONLY, "")
            return subprocess.CompletedProcess(argv, 0, "", "")

        tofu.current = {}
        boskos = _Boskos(free=[P7, P8])
        previous = signal.signal(signal.SIGINT, boskos_pool.terminate)
        outcomes = {}

        def fire():
            started.wait(timeout=5)
            os.kill(os.getpid(), signal.SIGINT)

        try:
            threading.Thread(target=fire, daemon=True).start()
            with mock.patch.object(boskos_pool.urllib.request, "urlopen", boskos), mock.patch("sys.stdout", io.StringIO()):
                with self.assertRaises(boskos_pool.Terminated):
                    reconcile.reconcile_pool(BOSKOS, OWNER, runner=tofu, known=KNOWN, run=reconcile.Run(workers=2), outcomes=outcomes)
        finally:
            signal.signal(signal.SIGINT, previous)
            reconcile._TERMINATING.clear()
        self.assertEqual(verbs[P8], ["init"], "P8 ran nothing after the termination")
        self.assertEqual({p: o for p, (o, _) in outcomes.items()}, {P7: reconcile.OUTCOME_INTERRUPTED, P8: reconcile.OUTCOME_INTERRUPTED})
        self.assertIn("nothing is locked", outcomes[P8][1])
        self.assertEqual(sorted(boskos.released), [P7, P8])

    def test_a_termination_reaches_the_live_tofu_children_of_the_workers(self):
        # Real children this time: each worker's apply is a process that
        # exits 130 on SIGINT. Signals reach the main thread only, so it is
        # the forward in _run_workers that must stop them.
        with tempfile.TemporaryDirectory() as tmp:
            script = "import os, signal, sys, time; open(os.path.join(%r, str(os.getpid())), 'w').close(); signal.signal(signal.SIGINT, lambda *a: sys.exit(130)); time.sleep(30)" % tmp

            def tofu(argv, **kw):
                if argv[1] == "apply":
                    return reconcile.tofu_runner([sys.executable, "-c", script], timeout=kw.get("timeout"))
                if argv[1] == "plan":
                    return subprocess.CompletedProcess(argv, reconcile.PLAN_HAS_CHANGES, "", "")
                if argv[1] == "show":
                    return subprocess.CompletedProcess(argv, 0, UPDATE_ONLY, "")
                return subprocess.CompletedProcess(argv, 0, "", "")

            def fire():
                for _ in range(250):
                    if len(os.listdir(tmp)) >= 2:
                        os.kill(os.getpid(), signal.SIGINT)
                        return
                    time.sleep(0.02)

            boskos = _Boskos(free=[P7, P8])
            previous = signal.signal(signal.SIGINT, boskos_pool.terminate)
            outcomes = {}
            started = time.monotonic()
            try:
                threading.Thread(target=fire, daemon=True).start()
                with mock.patch.object(boskos_pool.urllib.request, "urlopen", boskos), mock.patch("sys.stdout", io.StringIO()):
                    with self.assertRaises(boskos_pool.Terminated):
                        reconcile.reconcile_pool(BOSKOS, OWNER, runner=tofu, known=KNOWN, run=reconcile.Run(workers=2), outcomes=outcomes)
            finally:
                signal.signal(signal.SIGINT, previous)
                reconcile._TERMINATING.clear()
            self.assertLess(time.monotonic() - started, 20, "the children exited on the interrupt, not on the 30 s sleep")
            pids = [int(name) for name in os.listdir(tmp)]
            self.assertEqual(len(pids), 2)
            for pid in pids:
                with self.assertRaises(ProcessLookupError, msg="a child is still running"):
                    os.kill(pid, 0)
        self.assertEqual({p: o for p, (o, _) in outcomes.items()}, {P7: reconcile.OUTCOME_INTERRUPTED, P8: reconcile.OUTCOME_INTERRUPTED})
        self.assertEqual(sorted(boskos.released), [P7, P8])

    def test_a_worker_that_hits_a_boskos_fault_records_its_project_and_stops_the_run(self):
        class _Boskos_failing_one(_Boskos):
            def __call__(self, request, timeout=None):
                if "/acquirebystate?" in request.full_url and "names=%s" % P8 in request.full_url:
                    raise _http_error(500, request.full_url)
                return super().__call__(request, timeout)

        p9 = "kube-agents-evals-9"
        boskos = _Boskos_failing_one(free=[P7, P8, p9])
        slow = _Tofu({P7: UPDATE_ONLY, P8: UPDATE_ONLY, p9: UPDATE_ONLY})
        outcomes = {}
        with mock.patch.object(boskos_pool.urllib.request, "urlopen", boskos), mock.patch("sys.stdout", io.StringIO()), mock.patch("sys.stderr", io.StringIO()):
            with self.assertRaises(urllib.error.HTTPError):
                reconcile.reconcile_pool(BOSKOS, OWNER, runner=slow, known=KNOWN | {p9}, run=reconcile.Run(workers=2), outcomes=outcomes)
        self.assertEqual(outcomes[P8][0], reconcile.OUTCOME_FAILED)
        self.assertIn("500", outcomes[P8][1])
        self.assertEqual(outcomes[p9][0], reconcile.OUTCOME_NOT_REACHED, "the other worker stopped rather than leasing on under a failing Boskos")
        self.assertIn("error", outcomes[p9][1])
        self.assertEqual(sorted(boskos.released), [P7])

    def test_a_single_worker_termination_still_lists_the_pending_projects(self):
        def tofu(argv, **_):
            if argv[1] == "apply":
                raise boskos_pool.Terminated("signal 15")
            if argv[1] == "plan":
                return subprocess.CompletedProcess(argv, reconcile.PLAN_HAS_CHANGES, "", "")
            if argv[1] == "show":
                return subprocess.CompletedProcess(argv, 0, UPDATE_ONLY, "")
            return subprocess.CompletedProcess(argv, 0, "", "")

        boskos = _Boskos(free=[P7, P8])
        outcomes = {}
        with mock.patch.object(boskos_pool.urllib.request, "urlopen", boskos), mock.patch("sys.stdout", io.StringIO()):
            with self.assertRaises(boskos_pool.Terminated):
                reconcile.reconcile_pool(BOSKOS, OWNER, runner=tofu, known=KNOWN, run=reconcile.Run(), outcomes=outcomes)
        self.assertEqual({p: o for p, (o, _) in outcomes.items()}, {P7: reconcile.OUTCOME_INTERRUPTED, P8: reconcile.OUTCOME_NOT_REACHED})
        self.assertIn("terminated", outcomes[P8][1])
        # The named arm too.
        boskos = _Boskos(free=[P7, P8])
        outcomes = {}
        with mock.patch.object(boskos_pool.urllib.request, "urlopen", boskos), mock.patch("sys.stdout", io.StringIO()):
            with self.assertRaises(boskos_pool.Terminated):
                reconcile.reconcile_named([P7, P8], BOSKOS, OWNER, runner=tofu, known=KNOWN, outcomes=outcomes)
        self.assertEqual(outcomes[P8][0], reconcile.OUTCOME_NOT_REACHED)

    def test_a_project_that_failed_before_its_plan_was_read_reports_no_allowlist_verdict(self):
        allow = _allow([("google_compute_disk.gone", "old revert", False)])
        boskos = _Boskos(free=[P7])
        run = reconcile.Run(allow=allow)
        with mock.patch.object(boskos_pool.urllib.request, "urlopen", boskos):
            outcomes = reconcile.reconcile_pool(BOSKOS, OWNER, runner=_Tofu({}, fail={"init": "backend: bucket not found"}), known={P7}, run=run)
        self.assertEqual(outcomes[P7][0], reconcile.OUTCOME_FAILED)
        self.assertNotIn("allowlist_unused", run.extras[P7], "a plan that was not read says nothing about the allowlist")

    def test_the_report_says_when_the_main_moved_check_could_not_run(self):
        boskos = _Boskos(free=[P7])
        with mock.patch.object(reconcile, "git_output", _git_whose_fetch_fails), mock.patch.object(boskos_pool.urllib.request, "urlopen", boskos), mock.patch.object(reconcile, "MAIN_CHECK_INTERVAL_SECONDS", 0), mock.patch("sys.stderr", io.StringIO()):
            run = reconcile.Run(main_ref="origin/main")
            reconcile.reconcile_pool(BOSKOS, OWNER, runner=_Tofu({P7: UPDATE_ONLY}), known={P7}, run=run)
        self.assertIn("could not resolve host", run.main_check_error)
        with tempfile.TemporaryDirectory() as tmp:
            report = pathlib.Path(tmp) / "r.json"
            reconcile.write_report(str(report), argparse.Namespace(project=None, drifted=False, dry_run=False), {}, 0, None, 0, run)
            doc = json.loads(report.read_text())
        self.assertEqual(doc["main_ref"], "origin/main")
        self.assertIn("could not resolve host", doc["main_check_error"])


if __name__ == "__main__":
    unittest.main()
