"""The fleet reconcile applies the stack it was given and refuses the plan it was not.

`hack/fleet_reconcile.py` holds a write credential on every pool project's
fleet, so these tests pin the four things that bound it. It applies only the
plan it inspected: a plan that destroys or replaces anything is refused and
named, never applied. It holds a project only through Boskos, one at a time,
and gives every one back, on success, on a refusal, on a fault, on SIGTERM.
`--drifted` reads exactly the projects the fixture-state scan marks drifted.
And a project Boskos will not hand over is busy, not failed: the job stays
green and the next run gets it.

The shared walk in `hack/boskos_pool.py` is covered here for what the sweep's
tests do not reach: acquiring one project by name.
"""

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


class _Boskos:
    """A stand-in for the Boskos server: `free` in order for /acquire, by name for /acquirebystate."""

    def __init__(self, free=(), release_errors=None):
        self.free = list(free)
        self.release_errors = release_errors or {}
        self.acquired = []
        self.released = []
        self.resets = []
        self.beats = []

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
        self.assertEqual(detail, "0 to add, 1 to change, 0 refused")
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
        self.assertEqual((outcome, detail), (reconcile.OUTCOME_APPLIED, "1 to add, 1 to change, 0 refused"))

    def test_a_replace_is_refused_and_named_before_anything_is_applied(self):
        tofu = _Tofu({P7: REPLACE})
        outcome, detail = reconcile.reconcile_project(P7, runner=tofu)
        self.assertEqual(outcome, reconcile.OUTCOME_REFUSED)
        self.assertIn("delete+create google_container_node_pool.seeded_a_idle", detail)
        self.assertNotIn("apply", tofu.verbs())

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
        for body in ("[]", "null", '{"resource_changes": [null]}', '{"resource_changes": {"a": {}}}', '{"resource_changes": 5}', '{"resource_changes": "abc"}', '{"resource_changes": [{"address": "a", "change": "x"}]}', '{"resource_changes": [{"address": "a", "change": {"actions": [null]}}]}', '{"resource_changes": [{"address": "a", "change": {"actions": 5}}]}',
                     # Falsy wrong types must be refused as loudly as truthy ones.
                     '{"resource_changes": [{"address": "a", "change": []}]}', '{"resource_changes": [{"address": "a", "change": 0}]}', '{"resource_changes": [{"address": "a", "change": {"actions": 0}}]}', '{"resource_changes": [{"address": "a", "change": {"actions": {}}}]}', '{"resource_changes": [{"address": "a", "change": {"actions": ""}}]}'):
            tofu = _Tofu({P7: body})
            outcome, detail = reconcile.reconcile_project(P7, runner=tofu)
            self.assertEqual(outcome, reconcile.OUTCOME_FAILED, body)
            self.assertTrue(detail.startswith("tofu show wrote"), detail)

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

    def test_no_lease_asks_boskos_nothing_and_takes_a_project_outside_the_pool(self):
        # The dev-project path: no Boskos, and no mapping check, since the
        # check exists only to stop a typo reading as busy at Boskos.
        def no_boskos(request, timeout=None):
            raise AssertionError("Boskos was called: %s" % request.full_url)

        dev = "my-dev-project"
        with mock.patch.object(boskos_pool.urllib.request, "urlopen", no_boskos):
            outcomes = reconcile.reconcile_named([P7, dev], BOSKOS, OWNER, lease=False, runner=_Tofu({P7: UPDATE_ONLY, dev: UPDATE_ONLY}), known=KNOWN)
        self.assertEqual(outcomes[P7][0], reconcile.OUTCOME_APPLIED)
        self.assertEqual(outcomes[dev][0], reconcile.OUTCOME_APPLIED)

    def test_the_pool_walk_applies_every_free_project_once_and_releases_each(self):
        boskos = _Boskos(free=[P7, P8])
        tofu = _Tofu({P7: UPDATE_ONLY, P8: CREATE_AND_UPDATE})
        with mock.patch.object(boskos_pool.urllib.request, "urlopen", boskos):
            outcomes = reconcile.reconcile_pool(BOSKOS, OWNER, 2, runner=tofu, known=KNOWN)
        self.assertEqual({p: o for p, (o, _) in outcomes.items()}, {P7: reconcile.OUTCOME_APPLIED, P8: reconcile.OUTCOME_APPLIED})
        self.assertEqual(sorted(boskos.released), [P7, P8])
        self.assertEqual(tofu.verbs().count("apply"), 2)

    def test_the_pool_walk_leaves_a_hand_out_outside_the_mapping_untouched(self):
        # Registered in Boskos but not mapped: released, reported, no tofu.
        stray = "kube-agents-evals-99"
        boskos = _Boskos(free=[stray, P7])
        tofu = _Tofu({P7: UPDATE_ONLY})
        with mock.patch.object(boskos_pool.urllib.request, "urlopen", boskos):
            outcomes = reconcile.reconcile_pool(BOSKOS, OWNER, 2, runner=tofu, known=KNOWN)
        self.assertEqual(outcomes[stray], (reconcile.OUTCOME_FAILED, reconcile.REASON_UNMAPPED))
        self.assertEqual(outcomes[P7][0], reconcile.OUTCOME_APPLIED)
        self.assertEqual(sorted(boskos.released), [P7, stray])
        self.assertNotIn(stray, " ".join(" ".join(c) for c in tofu.calls))


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

    def test_drifted_reads_the_scan_resets_strands_and_applies_the_projects_listed(self):
        boskos = _Boskos(free=[P7, P8])
        scan = {"projects": {P8: {"roles": {"idle-pool": {"state": "drifted", "detail": []}}}, P7: {"roles": {"idle-pool": {"state": "healthy", "detail": []}}}}}
        rc, _ = self._main(["--drifted"], boskos, _Tofu({P8: UPDATE_ONLY}), scan=scan)
        self.assertEqual(rc, reconcile.EXIT_OK)
        self.assertEqual(boskos.acquired, [P8])
        self.assertEqual(boskos.resets[0]["state"], reconcile.HOLD_STATE)
        self.assertEqual(boskos.resets[0]["expire"], reconcile.STRANDED_AFTER)

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
            later = threading.Timer(2.5, os.kill, args=(os.getpid(), signal.SIGINT))
            started = time.monotonic()
            try:
                later.start()
                with mock.patch.object(reconcile, "INTERRUPT_GRACE_SECONDS", 20):
                    with self.assertRaises(boskos_pool.Terminated):
                        reconcile.tofu_runner([sys.executable, "-c", script], timeout=1.0)
            finally:
                later.cancel()
                signal.signal(signal.SIGINT, previous)
            self.assertLess(time.monotonic() - started, 8)
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
            timer = threading.Timer(0.5, os.kill, args=(os.getpid(), signal.SIGINT))
            try:
                timer.start()
                with self.assertRaises(boskos_pool.Terminated):
                    reconcile.tofu_runner([sys.executable, "-c", script], timeout=30)
            finally:
                timer.cancel()
                signal.signal(signal.SIGINT, previous)
            self.assertTrue(os.path.exists(marker), "the child never saw SIGINT")

    def test_a_finished_child_is_returned_with_its_output(self):
        result = reconcile.tofu_runner([sys.executable, "-c", "print('hi')"], timeout=10)
        self.assertEqual((result.returncode, result.stdout.strip()), (0, "hi"))

    def test_the_child_runs_in_its_own_session(self):
        # A terminal's Ctrl-C goes to the foreground process group; tofu in
        # its own session sees only the one interrupt this process forwards.
        result = reconcile.tofu_runner([sys.executable, "-c", "import os; print(os.getsid(0))"], timeout=10)
        self.assertNotEqual(int(result.stdout.strip()), os.getsid(0))


if __name__ == "__main__":
    unittest.main()
