# Copyright 2026 The Kubernetes Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""The builtin-schedule-run-now stack: ``run_now.py`` against a fake AgentSchedule.

The fake stands in for kubectl: one schedule whose status a test moves the way
the AgentSchedule reconciler does, and the gateway container's state file and
clock. The tests show what a case depends on: the stack fires only a built-in
schedule with no run in flight, records the pod's clock as ``fired_at`` before
it sets a fresh run-now token, finds the run that token started and waits for
it to end and for its delivery, and removes only what it set; and the stack's
files, the operator's constants and the store checks agree on the names.
"""

from __future__ import annotations

import importlib.util
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[2]
STACK = REPO / "bench" / "tf" / "prebuilt" / "builtin-schedule-run-now"
SCHEDULE_TYPES = REPO / "k8s-operator" / "api" / "v1alpha1" / "agentschedule_types.go"
NAME = "compliance-audit-canary"
STATE = "/opt/data/.bench-builtin-schedule-run-now.json"
POD_NOW = "2026-11-23T17:00:00+00:00"

_spec = importlib.util.spec_from_file_location("builtin_schedule_run_now", STACK / "run_now.py")
run_now = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(run_now)


class FakeKubectl:
    """One AgentSchedule and the gateway container's files, with a script of status changes per poll."""

    def __init__(self, status=None, labels=None, annotations=None, script=()):
        self.object = {
            "metadata": {
                "name": NAME,
                "labels": {run_now.ORIGIN_LABEL: run_now.ORIGIN_BUILTIN} if labels is None else labels,
                "annotations": dict(annotations or {}),
            },
            "status": dict(status or {}),
        }
        # Each read of the schedule first applies the next step, if any.
        self.script = list(script)
        self.files: dict[str, str] = {}
        self.log: list[tuple] = []
        self.unreadable = False

    def schedule(self, name):
        assert name == NAME
        if self.unreadable:
            raise SystemExit("kubectl get failed: forbidden")
        if self.script:
            step = self.script.pop(0)
            step(self.object)
        return json.loads(json.dumps(self.object))

    def annotate(self, name, value):
        self.log.append(("annotate", value))
        annotations = self.object["metadata"]["annotations"]
        if value is None:
            annotations.pop(run_now.RUN_NOW_ANNOTATION, None)
        else:
            annotations[run_now.RUN_NOW_ANNOTATION] = value

    def pod_now(self):
        self.log.append(("pod_now",))
        return POD_NOW

    def read_state(self, path):
        return json.loads(self.files[path])

    def write_state(self, path, state):
        self.log.append(("write_state", dict(state)))
        self.files[path] = json.dumps(state)

    def remove_state(self, path):
        self.log.append(("remove_state", path))
        self.files.pop(path, None)


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    clock = {"now": 0.0}
    monkeypatch.setattr(run_now, "_clock", lambda: clock["now"])

    def sleep(seconds):
        clock["now"] += seconds

    monkeypatch.setattr(run_now, "_sleep", sleep)
    return clock


def _status(obj, **fields):
    obj["status"].update(fields)


def _token(fake):
    return fake.object["metadata"]["annotations"][run_now.RUN_NOW_ANNOTATION]


def _fired(fake):
    run_now.fire(fake, NAME, STATE)
    return json.loads(fake.files[STATE])


# ---------------------------------------------------------------- idle and fire


def test_idle_refuses_a_schedule_that_is_not_built_in():
    fake = FakeKubectl(labels={run_now.ORIGIN_LABEL: "user"})
    with pytest.raises(SystemExit, match="not built-in"):
        run_now.idle(fake, NAME, 60)
    with pytest.raises(SystemExit, match="not built-in"):
        run_now.fire(FakeKubectl(labels={}), NAME, STATE)


def test_idle_waits_for_the_run_in_flight_to_settle():
    fake = FakeKubectl(
        status={"activeTaskID": "t0"},
        script=[lambda o: None, lambda o: _status(o, activeTaskID="", pendingSlot={"idempotencyKey": "k"}), lambda o: _status(o, pendingSlot=None)],
    )
    run_now.idle(fake, NAME, 600)
    assert not fake.script


def test_idle_gives_up_on_a_run_that_does_not_settle():
    fake = FakeKubectl(status={"activeTaskID": "t0"})
    with pytest.raises(SystemExit, match="in flight after 30s"):
        run_now.idle(fake, NAME, 30)


def test_fire_records_the_pods_clock_before_it_sets_a_fresh_token():
    fake = FakeKubectl(status={"lastTaskID": "t1", "recentRuns": [{"taskID": "t1"}, {"taskID": "t0"}], "lastHandledRunNow": "old"})
    state = _fired(fake)
    assert [entry[0] for entry in fake.log] == ["pod_now", "write_state", "annotate"]
    assert state["fired_at"] == POD_NOW
    assert datetime.fromisoformat(state["fired_at"]).tzinfo is not None
    assert state["schedule"] == NAME
    assert state["known_tasks"] == ["t0", "t1"]
    assert state["token"] == _token(fake)
    assert state["token"].startswith(run_now.TOKEN_PREFIX)
    assert state["token"] != "old"


def test_two_fires_set_two_tokens():
    assert run_now.new_token() != run_now.new_token()


def test_fire_refuses_a_run_claimed_since_idle():
    fake = FakeKubectl(status={"pendingSlot": {"idempotencyKey": "schedule/x"}})
    with pytest.raises(SystemExit, match="not firing over it"):
        run_now.fire(fake, NAME, STATE)
    assert fake.log == []


# ---------------------------------------------------------------- wait


def _accept(task):
    def step(obj):
        token = obj["metadata"]["annotations"][run_now.RUN_NOW_ANNOTATION]
        _status(obj, lastHandledRunNow=token, activeTaskID=task, pendingSlot=None)

    return step


def _settle(task, outcome="Succeeded", delivery=""):
    def step(obj):
        runs = obj["status"].get("recentRuns") or []
        _status(
            obj,
            activeTaskID="",
            lastTaskID=task,
            lastOutcome=outcome,
            recentRuns=[{"taskID": task, "outcome": outcome, "completionTime": "2026-11-23T17:20:00Z", "delivery": delivery}, *runs],
        )

    return step


def _deliver(task, delivery):
    def step(obj):
        for run in obj["status"]["recentRuns"]:
            if run["taskID"] == task:
                run["delivery"] = delivery

    return step


def test_wait_follows_the_fired_run_to_its_end_and_its_delivery():
    fake = FakeKubectl(status={"recentRuns": [{"taskID": "t0", "delivery": "posted"}]})
    _fired(fake)
    fake.script = [lambda o: None, _accept("t-new"), lambda o: None, _settle("t-new"), lambda o: None, _deliver("t-new", "local")]
    run_now.wait(fake, NAME, STATE, 3600, 660)
    state = json.loads(fake.files[STATE])
    assert state["task_id"] == "t-new"
    assert state["outcome"] == "Succeeded"
    assert state["delivery"] == "local"
    assert state["completed_at"] == "2026-11-23T17:20:00Z"
    # The fire time stays the one recorded before the annotation.
    assert state["fired_at"] == POD_NOW


def test_a_run_that_ended_between_two_polls_is_found_in_the_ring():
    fake = FakeKubectl(status={"lastTaskID": "t0", "recentRuns": [{"taskID": "t0", "delivery": "posted"}]})
    _fired(fake)

    def accepted_and_done(obj):
        _accept("t-fast")(obj)
        _settle("t-fast", delivery="silent")(obj)

    fake.script = [accepted_and_done]
    run_now.wait(fake, NAME, STATE, 3600, 660)
    state = json.loads(fake.files[STATE])
    assert (state["task_id"], state["delivery"]) == ("t-fast", "silent")


def test_a_run_now_handled_with_no_run_fails_the_apply_with_the_gateways_reason():
    fake = FakeKubectl()
    _fired(fake)

    def refused(obj):
        _status(
            obj,
            lastHandledRunNow=_token(fake),
            pendingSlot=None,
            conditions=[{"type": "Submitted", "status": "False", "reason": "tier_not_allowed", "message": "profile refuses unattended"}],
        )

    fake.script = [refused]
    with pytest.raises(SystemExit, match="started no run .*tier_not_allowed: profile refuses unattended"):
        run_now.wait(fake, NAME, STATE, 3600, 660)


def test_a_claim_the_gateway_has_not_taken_yet_is_waited_on():
    fake = FakeKubectl()
    _fired(fake)
    fake.script = [
        lambda o: _status(o, pendingSlot={"runNow": _token(fake)}),
        lambda o: None,
        _accept("t-late"),
        _settle("t-late", delivery="local"),
    ]
    run_now.wait(fake, NAME, STATE, 3600, 660)
    assert json.loads(fake.files[STATE])["task_id"] == "t-late"


def test_a_run_that_does_not_end_fails_the_apply():
    fake = FakeKubectl()
    _fired(fake)
    fake.script = [_accept("t-slow")]
    with pytest.raises(SystemExit, match="did not end within 60s"):
        run_now.wait(fake, NAME, STATE, 60, 660)
    # The task it saw is on record for the teardown's reader.
    assert json.loads(fake.files[STATE])["task_id"] == "t-slow"


def test_a_delivery_the_gateway_never_reports_is_recorded_empty(no_waiting):
    fake = FakeKubectl()
    _fired(fake)
    fake.script = [_accept("t-quiet"), _settle("t-quiet")]
    run_now.wait(fake, NAME, STATE, 3600, 60)
    assert json.loads(fake.files[STATE])["delivery"] == ""


def test_a_lost_run_has_no_delivery_to_wait_for(no_waiting):
    fake = FakeKubectl()
    _fired(fake)
    fake.script = [_accept("t-lost"), _settle("t-lost", outcome="Lost")]
    before = no_waiting["now"]
    run_now.wait(fake, NAME, STATE, 3600, 660)
    state = json.loads(fake.files[STATE])
    assert state["outcome"] == "Lost"
    assert no_waiting["now"] - before < 660


def test_a_run_of_another_token_in_flight_is_not_the_fired_one():
    """An active task under a token not yet ours is not taken for the fired run."""
    fake = FakeKubectl()
    _fired(fake)
    fake.script = [
        lambda o: _status(o, activeTaskID="t-cadence", lastHandledRunNow="someone-else"),
        lambda o: _status(o, lastHandledRunNow=_token(fake)),
        _settle("t-cadence", delivery="local"),
    ]
    run_now.wait(fake, NAME, STATE, 3600, 660)
    # Adopted under our token once handled: the schedule's own run, read as the fired one.
    assert json.loads(fake.files[STATE])["task_id"] == "t-cadence"


# ---------------------------------------------------------------- clean


def test_clean_removes_the_state_file_and_its_own_token():
    fake = FakeKubectl()
    _fired(fake)
    run_now.clean(fake, NAME, STATE)
    assert STATE not in fake.files
    assert run_now.RUN_NOW_ANNOTATION not in fake.object["metadata"]["annotations"]


def test_clean_leaves_a_token_someone_else_set():
    fake = FakeKubectl(annotations={run_now.RUN_NOW_ANNOTATION: "1732380000"})
    run_now.clean(fake, NAME, STATE)
    assert fake.object["metadata"]["annotations"][run_now.RUN_NOW_ANNOTATION] == "1732380000"
    assert ("annotate", None) not in fake.log


def test_clean_with_an_unreadable_schedule_still_removes_the_state_file():
    fake = FakeKubectl()
    fake.files[STATE] = "{}"
    fake.unreadable = True
    run_now.clean(fake, NAME, STATE)
    assert STATE not in fake.files


# ---------------------------------------------------------------- kubectl and the command line


class Done:
    def __init__(self, stdout="", returncode=0, stderr=""):
        self.stdout, self.returncode, self.stderr = stdout, returncode, stderr


def test_the_kubectl_calls_are_the_schedule_and_the_gateway_container(monkeypatch):
    calls: list[tuple[list[str], Any]] = []

    def run(cmd, input=None, **kwargs):
        calls.append((cmd, input))
        if cmd[3:5] == ["get", run_now.RESOURCE]:
            return Done(json.dumps({"metadata": {"name": NAME}}))
        return Done(POD_NOW + "\n")

    monkeypatch.setattr(run_now.subprocess, "run", run)
    kubectl = run_now.Kubectl("kubeagents-system", "platform-agent-gateway", "platform-agent")
    kubectl.schedule(NAME)
    kubectl.annotate(NAME, "bench-run-now-1-ab")
    kubectl.annotate(NAME, None)
    assert kubectl.pod_now() == POD_NOW
    kubectl.write_state(STATE, {"fired_at": POD_NOW})
    kubectl.remove_state(STATE)
    ns = ["kubectl", "-n", "kubeagents-system"]
    exec_prefix = ["exec", "deployment/platform-agent-gateway", "-c", "platform-agent", "--"]
    assert calls[0][0] == [*ns, "get", run_now.RESOURCE, NAME, "-o", "json"]
    assert calls[1][0] == [*ns, "annotate", run_now.RESOURCE, NAME, f"{run_now.RUN_NOW_ANNOTATION}=bench-run-now-1-ab", "--overwrite"]
    assert calls[2][0] == [*ns, "annotate", run_now.RESOURCE, NAME, f"{run_now.RUN_NOW_ANNOTATION}-", "--overwrite"]
    assert calls[3][0] == [*ns, *exec_prefix, *run_now.POD_CLOCK]
    assert calls[4][0][:5] == [*ns, "exec", "-i"]
    assert calls[4][0][-1] == STATE
    assert json.loads(calls[4][1]) == {"fired_at": POD_NOW}
    assert calls[5][0] == [*ns, *exec_prefix, "rm", "-f", STATE]


def test_a_kubectl_failure_stops_the_step(monkeypatch):
    monkeypatch.setattr(run_now.subprocess, "run", lambda cmd, **kw: Done(returncode=1, stderr="forbidden"))
    with pytest.raises(SystemExit, match="kubectl get agentschedules.kubeagents.x-k8s.io failed: forbidden"):
        run_now.Kubectl("ns", "d", "c").schedule(NAME)


def test_the_command_line_routes_each_step(monkeypatch):
    seen: list[tuple] = []
    for verb in ("idle", "fire", "wait", "clean"):
        monkeypatch.setattr(run_now, verb, lambda kubectl, *args, verb=verb: seen.append((verb, kubectl.namespace, kubectl.deployment, kubectl.container, args)))
    flags = ["--namespace", "ns", "--deployment", "dep", "--container", "ctr"]
    run_now.main([*flags, "idle", NAME, "1800"])
    run_now.main([*flags, "fire", NAME, STATE])
    run_now.main([*flags, "wait", NAME, STATE, "2700", "660"])
    run_now.main([*flags, "clean", NAME, STATE])
    assert seen == [
        ("idle", "ns", "dep", "ctr", (NAME, "1800")),
        ("fire", "ns", "dep", "ctr", (NAME, STATE)),
        ("wait", "ns", "dep", "ctr", (NAME, STATE, "2700", "660")),
        ("clean", "ns", "dep", "ctr", (NAME, STATE)),
    ]


# ---------------------------------------------------------------- the stack's parameters


def _variables() -> dict[str, str]:
    text = (STACK / "variables.tf").read_text()
    return {m.group(1): m.group(2) for m in re.finditer(r'^variable "([a-z_]+)" \{\n(.*?)^\}', text, re.M | re.S)}


def test_the_schedule_is_a_required_parameter_checked_as_a_name():
    variables = _variables()
    assert "schedule" in variables
    assert "default" not in variables["schedule"]
    pattern = re.search(r'regex\("([^"]+)", var\.schedule\)', variables["schedule"]).group(1)
    assert re.fullmatch(pattern, NAME)
    for bad in ("Compliance", "a_b", "-x", "x" * 64):
        assert not re.fullmatch(pattern, bad)


def test_the_stack_declares_what_the_deployer_injects_and_the_waits():
    variables = _variables()
    assert {"host_cluster_name", "host_cluster_location", "project_id", "agent_namespace", "agent_deployment", "agent_container"} <= set(variables)
    for wait in ("idle_wait", "run_wait", "delivery_wait"):
        assert re.search(r"default\s*=\s*\d+", variables[wait]), wait


def test_main_runs_every_step_with_the_schedule_and_the_waits():
    main = (STACK / "main.tf").read_text()
    for step in (
        'run_now idle "${var.schedule}" "${var.idle_wait}"',
        'run_now fire "${var.schedule}" "${local.state_file}"',
        'run_now wait "${var.schedule}" "${local.state_file}" "${var.run_wait}" "${var.delivery_wait}"',
        'run_now clean "${var.schedule}" "${local.state_file}"',
        'clean "${self.triggers.schedule}" "${self.triggers.state_file}"',
    ):
        assert step in main, step
    # Every variable the stack declares is used.
    for name in _variables():
        assert f"var.{name}" in main, name


def test_the_state_file_is_where_the_checks_are_told_to_read():
    main = (STACK / "main.tf").read_text()
    assert re.search(r'state_file = "([^"]+)"', main).group(1) == STATE


def test_the_annotation_and_the_label_are_the_operators():
    types = SCHEDULE_TYPES.read_text()
    assert re.search(r'AgentScheduleRunNowAnnotation = "([^"]+)"', types).group(1) == run_now.RUN_NOW_ANNOTATION
    assert re.search(r'AgentScheduleOriginLabel = "([^"]+)"', types).group(1) == run_now.ORIGIN_LABEL
    assert re.search(r'AgentScheduleOriginBuiltin = "([^"]+)"', types).group(1) == run_now.ORIGIN_BUILTIN
    assert re.search(r'AgentScheduleOutcomeLost\s+= "([^"]+)"', types).group(1) == run_now.OUTCOME_LOST
