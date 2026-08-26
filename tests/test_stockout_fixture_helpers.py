"""Tests for the pure helpers behind the stockout E2E fixture.

`tests/e2e/test_stockout_investigation.py` only runs in the release-candidate pipeline,
against a live GKE cluster, once a day at best. Its helpers, though, are functions over
strings and JSON: which workload kind exists, which pod belongs to the current revision,
whether a wait was cut short by its own ceiling or by the fixture's budget, and whether a
kubectl that never answered is being read as "the object is absent". Every one of those has
already been wrong at least once, and each time the cost was a release cycle rather than a
pull request, because nothing outside the pipeline exercised them.

So they are exercised here, with kubectl replaced by a stub. Nothing in this file touches a
cluster or a network; the module under test is imported for its functions and its fixture
never runs.

The one non-pure assertion is the rollout budget, which is read from the workflow that owns
it rather than hardcoded, the way tests/test_gateway_rollout_budgets.py does — a fixture
whose gate drifts below the deploy workflow's is the H1 defect this suite exists to keep
from coming back.
"""

import importlib.util
import json
import pathlib
import subprocess
import sys
import time
import types
import unittest
from unittest import mock

import yaml

from test_gateway_rollout_budgets import _rollout_gate_seconds

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_FIXTURE = _ROOT / "tests" / "e2e" / "test_stockout_investigation.py"
_AGENT_WORKFLOW = _ROOT / ".github" / "workflows" / "reusable-deploy-agent.yml"
_RC_PIPELINE = _ROOT / ".github" / "workflows" / "rc-release-pipeline.yml"


class _StubFailure(Exception):
    """What the stubbed pytest.fail and pytest.skip raise."""


def _pytest_stub() -> types.ModuleType:
    """A stand-in for pytest, which no unit-test job installs.

    `tests/test_*.py` is collected by python-tests.yml, which installs
    requirements-test.txt, and by agent-startup-test.yml, which installs only pyyaml and
    says in a comment that the tests use stdlib unittest so they run "without adding a test
    framework to the repo". pytest lives in tests/e2e/requirements.txt and nothing else
    installs it, so importing the module under test for real reds both jobs on every PR
    that touches this directory.

    The module touches four attributes: `fixture` and `mark.parametrize` at import time,
    and `fail` and `skip` when a helper rejects something. A fifth added later raises
    AttributeError naming it, which is the intended way to find out.
    """
    stub = types.ModuleType("pytest")
    stub.fixture = lambda *args, **kwargs: (lambda func: func)
    mark = types.SimpleNamespace(parametrize=lambda *args, **kwargs: (lambda func: func))
    stub.mark = mark

    def _fail(msg="", pytrace=True):
        raise _StubFailure(msg)

    def _skip(msg="", **kwargs):
        raise _StubFailure(msg)

    stub.fail = _fail
    stub.skip = _skip
    return stub


def _load_fixture_module():
    """Import the e2e module by path, with pytest stubbed.

    By path because `tests/e2e` is not a package and the module name starts with `test_`,
    so a plain import would either fail or invite this file's own runner to collect it
    twice. Importing it runs module-level code only: constants, function definitions and
    the two decorators. No cluster call happens until a fixture is requested.

    The stub replaces pytest even where the real package is installed, for the reason the
    sibling loader in test_e2e_github_repo_resolution.py gives: `make test-python` runs on
    an interpreter without pytest, so the stub is what makes the import possible at all
    there, and forcing it everywhere keeps the module the same shape in both places. Under
    real pytest `@pytest.fixture` returns an object that raises when called directly, so a
    test reaching for the undecorated function would pass on a checkout with
    tests/e2e/requirements.txt installed and fail without it.

    sys.modules is restored on the way out so nothing afterwards picks the stub up.
    """
    with mock.patch.dict(sys.modules, {"pytest": _pytest_stub()}):
        spec = importlib.util.spec_from_file_location("_stockout_fixture", _FIXTURE)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


sof = _load_fixture_module()


def _completed(stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess(args=["kubectl"], returncode=returncode,
                                       stdout=stdout, stderr=stderr)


class RemainingTest(unittest.TestCase):
    """_remaining has to say which ceiling bound it, not just how long is left."""

    def test_cap_binds_when_budget_is_generous(self):
        seconds, bound = sof._remaining(time.time() + 10_000, 120)
        self.assertEqual(seconds, 120)
        self.assertEqual(bound, "its own ceiling")

    def test_budget_binds_when_it_is_the_smaller(self):
        seconds, bound = sof._remaining(time.time() + 5, 120)
        self.assertLessEqual(seconds, 5)
        self.assertEqual(bound, "the fixture's budget")

    def test_exhausted_budget_is_zero_not_negative(self):
        seconds, bound = sof._remaining(time.time() - 60, 120)
        self.assertEqual(seconds, 0)
        self.assertEqual(bound, "the fixture's budget")


class AsTextTest(unittest.TestCase):
    """TimeoutExpired hands back bytes even in text mode; a b'...' blob is unreadable."""

    def test_bytes_are_decoded(self):
        self.assertEqual(sof._as_text(b"step 5\nstep 6"), "step 5\nstep 6")

    def test_undecodable_bytes_do_not_raise(self):
        self.assertIn("�", sof._as_text(b"\xff\xfe"))

    def test_none_is_empty(self):
        self.assertEqual(sof._as_text(None), "")


class KubectlTimeoutTest(unittest.TestCase):
    """A kubectl that never answered must not be readable as "the object is absent"."""

    def test_timeout_becomes_a_distinguishable_returncode(self):
        with mock.patch.object(sof.subprocess, "run",
                               side_effect=subprocess.TimeoutExpired(cmd="kubectl", timeout=30)):
            res = sof._kubectl("get", "pods")
        self.assertEqual(res.returncode, sof._KUBECTL_TIMEOUT_RC)
        self.assertIn("did not answer", res.stderr)

    def test_fail_on_timeout_raises_instead_of_returning_a_failure(self):
        with mock.patch.object(sof.subprocess, "run",
                               side_effect=subprocess.TimeoutExpired(cmd="kubectl", timeout=30)):
            with self.assertRaises(BaseException) as caught:
                sof._kubectl("get", "deployment", "x", fail_on_timeout=True)
        self.assertIn("did not answer", str(caught.exception))

    def test_a_real_failure_is_passed_through_unchanged(self):
        with mock.patch.object(sof.subprocess, "run",
                               return_value=_completed(returncode=1, stderr="NotFound")):
            res = sof._kubectl("get", "deployment", "x")
        self.assertEqual(res.returncode, 1)
        self.assertNotEqual(res.returncode, sof._KUBECTL_TIMEOUT_RC)


class GatewayWorkloadTest(unittest.TestCase):
    def test_statefulset_is_found_when_there_is_no_deployment(self):
        def fake(*args, **kwargs):
            return _completed(returncode=0 if "statefulset" in args else 1)

        with mock.patch.object(sof, "_kubectl", side_effect=fake):
            kind, name = sof._gateway_workload("platform-agent", "ns")
        self.assertEqual((kind, name), ("statefulset", "platform-agent-gateway"))

    def test_neither_present_reports_no_kind(self):
        with mock.patch.object(sof, "_kubectl", return_value=_completed(returncode=1)):
            kind, name = sof._gateway_workload("platform-agent", "ns")
        self.assertIsNone(kind)
        self.assertEqual(name, "platform-agent-gateway")

    def test_existence_probes_are_ground_truth(self):
        """A slow API server must not be reported as a missing gateway."""
        seen = []

        def fake(*args, **kwargs):
            seen.append(kwargs.get("fail_on_timeout", False))
            return _completed(returncode=1)

        with mock.patch.object(sof, "_kubectl", side_effect=fake):
            sof._gateway_workload("platform-agent", "ns")
        self.assertTrue(all(seen), "existence probes must pass fail_on_timeout")


class GenerationTest(unittest.TestCase):
    def test_parses_an_integer(self):
        with mock.patch.object(sof, "_kubectl", return_value=_completed(stdout="7\n")):
            self.assertEqual(sof._generation("deployment", "d", "ns"), 7)

    def test_absent_object_is_none(self):
        with mock.patch.object(sof, "_kubectl", return_value=_completed(returncode=1)):
            self.assertIsNone(sof._generation("deployment", "d", "ns"))

    def test_unparseable_output_is_none_rather_than_an_exception(self):
        with mock.patch.object(sof, "_kubectl", return_value=_completed(stdout="")):
            self.assertIsNone(sof._generation("deployment", "d", "ns"))


class CurrentRevisionSelectorTest(unittest.TestCase):
    """The rollback case: Kubernetes scales up an OLDER ReplicaSet, and the newest one
    reporting replicas is then the one being drained."""

    @staticmethod
    def _rs(name, pod_hash, revision, replicas):
        return {
            "metadata": {
                "name": name,
                "labels": {"pod-template-hash": pod_hash},
                "annotations": {"deployment.kubernetes.io/revision": revision},
            },
            "status": {"replicas": replicas},
        }

    def _selector_for(self, deployment_revision, replicasets):
        def fake(*args, **kwargs):
            if args[1] == "deployment":
                return _completed(stdout=deployment_revision)
            return _completed(stdout=json.dumps({"items": replicasets}))

        with mock.patch.object(sof, "_kubectl", side_effect=fake):
            return sof._current_revision_selector("deployment", "platform-agent-gateway", "ns")

    def test_picks_the_replicaset_matching_the_deployment_revision(self):
        selector = self._selector_for("3", [
            self._rs("rs-old", "aaa", "2", 0),
            self._rs("rs-new", "bbb", "3", 2),
        ])
        self.assertEqual(selector, "pod-template-hash=bbb")

    def test_rollback_picks_the_older_replicaset_the_deployment_points_at(self):
        # Revision 4 is a rollback to the template revision 2 used: the annotation moves to
        # the ReplicaSet being scaled UP, which is the older one by creation time. Choosing
        # "newest with replicas > 0" would pin rs-draining and probe a dying pod.
        selector = self._selector_for("4", [
            self._rs("rs-restored", "aaa", "4", 2),
            self._rs("rs-draining", "bbb", "3", 1),
        ])
        self.assertEqual(selector, "pod-template-hash=aaa")

    def test_no_matching_replicaset_yields_no_selector(self):
        self.assertIsNone(self._selector_for("9", [self._rs("rs", "aaa", "3", 1)]))

    def test_statefulset_uses_the_update_revision(self):
        with mock.patch.object(sof, "_kubectl", return_value=_completed(stdout="gw-7c9\n")):
            selector = sof._current_revision_selector("statefulset", "gw", "ns")
        self.assertEqual(selector, "controller-revision-hash=gw-7c9")


class GatewayPodTest(unittest.TestCase):
    """Three ways to come back empty, three different places to send the reader."""

    @staticmethod
    def _pod(name, phase="Running", ready=True, deleting=False):
        meta = {"name": name}
        if deleting:
            meta["deletionTimestamp"] = "2026-08-26T00:00:00Z"
        return {
            "metadata": meta,
            "status": {
                "phase": phase,
                "conditions": [{"type": "Ready", "status": "True" if ready else "False"}],
            },
        }

    def _resolve(self, pods, revision="pod-template-hash=bbb"):
        with mock.patch.object(sof, "_kubectl",
                               return_value=_completed(stdout=json.dumps({"items": pods}))):
            return sof._gateway_pod("platform-agent", "ns", revision)

    def test_ready_pod_is_returned(self):
        pod, detail = self._resolve([self._pod("gw-new")])
        self.assertEqual(pod, "gw-new")
        self.assertEqual(detail, "")

    def test_terminating_pod_is_skipped_in_favour_of_a_live_one(self):
        pod, _ = self._resolve([self._pod("gw-old", deleting=True), self._pod("gw-new")])
        self.assertEqual(pod, "gw-new")

    def test_no_pods_names_the_selector(self):
        pod, detail = self._resolve([])
        self.assertIsNone(pod)
        self.assertIn("no pod matches", detail)
        self.assertIn("pod-template-hash=bbb", detail)

    def test_pod_present_but_unready_says_so_rather_than_no_pod(self):
        pod, detail = self._resolve([self._pod("gw-new", ready=False)])
        self.assertIsNone(pod)
        self.assertIn("none is usable", detail)
        self.assertIn("Running, not Ready", detail)
        self.assertNotIn("no pod matches", detail)

    def test_only_terminating_pods_is_reported_as_terminating(self):
        pod, detail = self._resolve([self._pod("gw-old", deleting=True)])
        self.assertIsNone(pod)
        self.assertIn("terminating", detail)

    def test_a_failed_list_is_not_reported_as_an_empty_one(self):
        with mock.patch.object(sof, "_kubectl",
                               return_value=_completed(returncode=1, stderr="forbidden")):
            pod, detail = sof._gateway_pod("platform-agent", "ns", None)
        self.assertIsNone(pod)
        self.assertIn("could not list pods", detail)


class SkillPathTest(unittest.TestCase):
    """The probe path has to follow the CR's agentHome, not a shell variable the exec
    environment does not carry."""

    def _probe_path(self, agent_home, target_profile):
        calls = []

        def fake_kubectl(*args, **kwargs):
            if "exec" in args:
                calls.append(args[-1])
                return _completed(stdout="PRESENT\n")
            if "platformagent" in args:
                return _completed(stdout=agent_home)
            return _completed(stdout=json.dumps(
                {"items": [GatewayPodTest._pod("gw-1")]}))

        with mock.patch.object(sof, "_kubectl", side_effect=fake_kubectl):
            with mock.patch.object(sof, "_current_revision_selector", return_value=None):
                sof._verify_skill_mounted("platform-agent", "ns", target_profile,
                                          "deployment", "gw", time.time() + 60)
        return calls[0]

    def test_targeted_profile_path_follows_the_cr_agent_home(self):
        self.assertIn("/var/agent/profiles/platform/plugins/gkestockoutinvestigator/",
                      self._probe_path("/var/agent", "platform"))

    def test_default_profile_lives_at_the_home_root(self):
        path = self._probe_path("/opt/data", "")
        self.assertIn("/opt/data/plugins/gkestockoutinvestigator/", path)
        self.assertNotIn("/profiles/", path)

    def test_unset_agent_home_falls_back_to_the_crd_default(self):
        self.assertIn("/opt/data/profiles/platform/", self._probe_path("", "platform"))

    def test_hermes_home_is_not_expanded_in_the_probe(self):
        self.assertNotIn("HERMES_HOME", self._probe_path("/opt/data", "platform"))


class SkillMountTest(unittest.TestCase):
    """The verdict on the mount has to distinguish "the file is absent" from "nobody
    looked", because only the first is a statement about the plugin."""

    def _verify(self, kind, revision, on_exec="PRESENT\n", window=60):
        execs = []

        def fake_kubectl(*args, **kwargs):
            if "platformagent" in args:
                return _completed(stdout="/opt/data")
            if "exec" in args:
                execs.append(args)
                return _completed(stdout=on_exec)
            return _completed(stdout=json.dumps({"items": [GatewayPodTest._pod("gw-old")]}))

        # sleep is stubbed so a retry path costs no wall time; the loop still spends its
        # window because the deadline is real, and `window=0` is what makes that one pass.
        with mock.patch.object(sof, "_kubectl", side_effect=fake_kubectl), \
                mock.patch.object(sof, "_current_revision_selector", return_value=revision), \
                mock.patch.object(sof.time, "sleep"):
            result = sof._verify_skill_mounted("platform-agent", "ns", "platform", kind,
                                               "gw", time.time() + window)
        return result, execs

    def test_a_present_skill_returns_the_pod_it_was_found_in(self):
        pod, execs = self._verify("deployment", "pod-template-hash=aaa")
        self.assertEqual(pod, "gw-old")
        self.assertEqual(len(execs), 1)

    def test_an_unpinned_revision_still_probes(self):
        # _current_revision_selector returns None on a timeout, a missing revision
        # annotation or unparseable JSON. _gateway_pod then lists unfiltered, which is
        # weaker but still the best answer available — refusing to probe would turn a slow
        # API server into a verdict about the plugin.
        pod, execs = self._verify("deployment", None)
        self.assertEqual(pod, "gw-old")
        self.assertEqual(len(execs), 1)

    def test_statefulset_is_pinned_by_controller_revision_hash(self):
        pod, execs = self._verify("statefulset", "controller-revision-hash=gw-7c9")
        self.assertEqual(pod, "gw-old")
        self.assertEqual(len(execs), 1)

    def test_absent_skill_is_retried_before_it_is_called_conclusive(self):
        # Under leader election a Ready pod may not have reached `exec` yet, so ABSENT is
        # transient; the window has to be spent before the verdict.
        with self.assertRaises(BaseException) as caught:
            self._verify("deployment", "pod-template-hash=aaa", on_exec="ABSENT\n", window=0)
        self.assertIn("is not mounted in gw-old", str(caught.exception))

    def test_an_exec_that_never_ran_is_reported_as_inconclusive(self):
        """A failed exec is not evidence about the plugin, and must not read as any."""
        def fake_kubectl(*args, **kwargs):
            if "platformagent" in args:
                return _completed(stdout="/opt/data")
            if "exec" in args:
                return _completed(returncode=1, stderr="error dialing backend")
            return _completed(stdout=json.dumps({"items": [GatewayPodTest._pod("gw-old")]}))

        with mock.patch.object(sof, "_kubectl", side_effect=fake_kubectl), \
                mock.patch.object(sof, "_current_revision_selector", return_value=None), \
                mock.patch.object(sof.time, "sleep"):
            with self.assertRaises(BaseException) as caught:
                sof._verify_skill_mounted("platform-agent", "ns", "platform", "deployment",
                                          "gw", time.time())
        message = str(caught.exception)
        self.assertIn("Could not establish", message)
        self.assertNotIn("is not mounted", message)


class BudgetTest(unittest.TestCase):
    """The fixture's ceiling has to stay under what the job that runs it allows."""

    def test_rollout_gate_stays_under_the_deploy_workflow_cold_boot_budget(self):
        # Deliberately not equal to it. The deploy workflow's gate covers a gateway being
        # started from nothing; this fixture runs after step 2 has already provisioned the
        # environment and waited for the pod, so what is left is a re-template on a warm
        # node. Asserting the relation rather than the number keeps this honest if the
        # deploy workflow's own budget moves. The helper is imported rather than the regex
        # rewritten: test_gateway_rollout_budgets.py owns that expression.
        self.assertLess(
            sof._ROLLOUT_TIMEOUT_SECONDS,
            _rollout_gate_seconds(_AGENT_WORKFLOW, "platform-agent-gateway"),
            "the fixture waits on a warm re-template, so its ceiling should sit below the "
            "cold-boot budget the deploy workflow sanctions",
        )

    def test_fixture_budget_fits_inside_the_pipeline_step(self):
        # Parsed rather than split on the job key and regexed for the next timeout-minutes:
        # that reads a step-level timeout, or the wrong job's, without saying so.
        jobs = yaml.safe_load(_RC_PIPELINE.read_text())["jobs"]
        self.assertIn("step-3-run-e2e-tests", jobs, "the RC pipeline's e2e job was renamed")
        minutes = jobs["step-3-run-e2e-tests"].get("timeout-minutes")
        self.assertIsNotNone(minutes, "step 3 has no job-level timeout-minutes")
        job_seconds = int(minutes) * 60
        # The fixture is one part of the step, which also runs setup, two other e2e modules
        # and the scenarios. Half the job is the loosest bound worth asserting; the precise
        # arithmetic lives in the workflow comment, where it can be read next to the number.
        self.assertLess(
            sof._FIXTURE_BUDGET_SECONDS, job_seconds / 2,
            "the stockout fixture may not claim half of step 3's budget",
        )

    def test_every_wait_is_capped_by_the_budget(self):
        for name in ("_INSTALL_TIMEOUT_SECONDS", "_ROLLOUT_TIMEOUT_SECONDS",
                     "_PLUGIN_READY_TIMEOUT_SECONDS", "_SKILL_MOUNT_TIMEOUT_SECONDS",
                     "_GENERATION_STABLE_SECONDS"):
            with self.subTest(constant=name):
                self.assertLessEqual(getattr(sof, name), sof._FIXTURE_BUDGET_SECONDS)


if __name__ == "__main__":
    unittest.main()
