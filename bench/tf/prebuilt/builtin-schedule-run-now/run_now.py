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

"""Fire one run of a built-in AgentSchedule with its run-now annotation, and wait it out.

Runs on the runner, with kubectl pointed at the install's cluster (main.tf
fetches the credentials). Four commands, each after the global flags
``--namespace``, ``--deployment`` and ``--container``:

  idle <schedule> <seconds>    refuse a schedule that is not built-in, then wait until
                               it has no run in flight or claimed, so the run-now
                               starts a run rather than adopting one
  fire <schedule> <state>      record when, in the state file <state> in the gateway's
                               agent container, then set the run-now annotation to a
                               token the schedule has not handled
  wait <schedule> <state> <run-seconds> <delivery-seconds>
                               wait until the fired run has ended, then until the
                               gateway has said how its report was delivered, and
                               record both in the state file
  clean <schedule> <state>     remove the state file, and the annotation when it still
                               holds a token this script set

The annotation (``kubeagents.x-k8s.io/run-now``, AgentScheduleRunNowAnnotation)
asks for one run: a value that differs from ``status.lastHandledRunNow`` claims a
run under the idempotency key ``run-now/<ns>/<name>/<token>`` and submits the
schedule's own run, with its ``schedule/<name>`` trigger, at the unattended tier
for a built-in; a run already in flight is adopted instead
(agentschedule_controller.go, "Run now"). The admission policy for built-in
schedules (config/admission/agentschedule-builtin-policy.yaml) leaves
annotations open to anyone, so the runner's own credentials set it.

The fire time is the gateway pod's clock, read before the annotation is set, so
it is a lower bound for anything the run writes. The store and side-effect
checks read it from the state file as ``fired_at`` (the shape the
audit-chat-report and unchanged-manifest-digest stacks write).
"""

import argparse
import json
import secrets
import subprocess
import sys
import time

# k8s-operator/api/v1alpha1/agentschedule_types.go.
RUN_NOW_ANNOTATION = "kubeagents.x-k8s.io/run-now"
ORIGIN_LABEL = "kubeagents.x-k8s.io/origin"
ORIGIN_BUILTIN = "builtin"
# AgentScheduleOutcomeLost: a run the gateway lost has no delivery to wait for.
OUTCOME_LOST = "Lost"
# The resource by its full name, so no other API group's `agentschedule` answers.
RESOURCE = "agentschedules.kubeagents.x-k8s.io"
# Every token this script sets starts with this, so `clean` removes only its own.
TOKEN_PREFIX = "bench-run-now-"
TOKEN_RANDOM_BYTES = 4
POLL_S = 15
KUBECTL_TIMEOUT_S = 60
# The pod's own clock, in the ISO-8601 form verifiers.read_fired_at parses.
POD_CLOCK = ["date", "-u", "+%Y-%m-%dT%H:%M:%S+00:00"]


class Kubectl:
    """kubectl in one namespace, with the gateway Deployment's agent container for exec."""

    def __init__(self, namespace, deployment, container):
        self.namespace = namespace
        self.deployment = deployment
        self.container = container

    def run(self, args, stdin=None):
        """Run kubectl with ``args`` in the namespace; SystemExit with its stderr on a failure."""
        done = subprocess.run(
            ["kubectl", "-n", self.namespace, *args],
            input=stdin, capture_output=True, text=True, timeout=KUBECTL_TIMEOUT_S, check=False,
        )
        if done.returncode != 0:
            raise SystemExit(f"kubectl {' '.join(args[:2])} failed: {(done.stderr or done.stdout).strip()}")
        return done.stdout

    def exec(self, command, stdin=None):
        interactive = ["-i"] if stdin is not None else []
        return self.run(
            ["exec", *interactive, f"deployment/{self.deployment}", "-c", self.container, "--", *command], stdin=stdin
        )

    def schedule(self, name):
        return json.loads(self.run(["get", RESOURCE, name, "-o", "json"]))

    def annotate(self, name, value):
        """Set the run-now annotation to ``value``, or remove it when ``value`` is None."""
        pair = f"{RUN_NOW_ANNOTATION}-" if value is None else f"{RUN_NOW_ANNOTATION}={value}"
        self.run(["annotate", RESOURCE, name, pair, "--overwrite"])

    def pod_now(self):
        return self.exec(POD_CLOCK).strip()

    def read_state(self, path):
        return json.loads(self.exec(["cat", path]))

    def write_state(self, path, state):
        # Written beside and renamed, so a reader never sees half a file.
        self.exec(["sh", "-c", 'cat > "$1.tmp" && mv "$1.tmp" "$1"', "sh", path], stdin=json.dumps(state))

    def remove_state(self, path):
        self.exec(["rm", "-f", path])


def _status(schedule):
    return schedule.get("status") or {}


def _busy(status):
    """Why the schedule can't start a fresh run now, or None."""
    if status.get("activeTaskID"):
        return f"run {status['activeTaskID']} is in flight"
    if status.get("pendingSlot"):
        return "a run is claimed and not yet accepted"
    return None


def _require_builtin(schedule, name):
    labels = (schedule.get("metadata") or {}).get("labels") or {}
    if labels.get(ORIGIN_LABEL) != ORIGIN_BUILTIN:
        raise SystemExit(
            f"AgentSchedule {name} is not built-in ({ORIGIN_LABEL}={labels.get(ORIGIN_LABEL)!r}); "
            "this stack fires built-in schedules only"
        )


def _known_tasks(status):
    """Every task the schedule's status names before the fire, so a new one is the fired run."""
    known = {run.get("taskID") for run in status.get("recentRuns") or []}
    known.update({status.get("lastTaskID"), status.get("activeTaskID")})
    return sorted(task for task in known if task)


def _clock():
    return time.monotonic()


def _sleep(seconds):
    time.sleep(seconds)


def idle(kubectl, name, seconds):
    deadline = _clock() + float(seconds)
    while True:
        schedule = kubectl.schedule(name)
        _require_builtin(schedule, name)
        reason = _busy(_status(schedule))
        if reason is None:
            return
        if _clock() > deadline:
            raise SystemExit(f"AgentSchedule {name}: {reason} after {seconds}s")
        print(f"AgentSchedule {name}: {reason}; waiting for it to settle")
        _sleep(POLL_S)


def new_token():
    return f"{TOKEN_PREFIX}{int(time.time())}-{secrets.token_hex(TOKEN_RANDOM_BYTES)}"


def fire(kubectl, name, state_path):
    schedule = kubectl.schedule(name)
    _require_builtin(schedule, name)
    status = _status(schedule)
    # Again, just before the fire: a slot the cadence claimed since `idle`
    # would be adopted, and its run read as this one.
    reason = _busy(status)
    if reason is not None:
        raise SystemExit(f"AgentSchedule {name}: {reason}; not firing over it")
    token = new_token()
    fired_at = kubectl.pod_now()
    kubectl.write_state(
        state_path, {"schedule": name, "token": token, "fired_at": fired_at, "known_tasks": _known_tasks(status)}
    )
    kubectl.annotate(name, token)
    print(f"set {RUN_NOW_ANNOTATION}={token} on AgentSchedule {name} at {fired_at}")


def _fired_task(status, state):
    """The fired run's task, once the status names one the schedule did not have before the fire."""
    known = set(state.get("known_tasks") or [])
    active = status.get("activeTaskID")
    if active and active not in known and status.get("lastHandledRunNow") == state["token"]:
        return active
    # A run that ended between two polls is in the ring, newest first.
    for run in status.get("recentRuns") or []:
        if run.get("taskID") and run["taskID"] not in known:
            return run["taskID"]
    return None


def _submitted_message(status):
    for condition in status.get("conditions") or []:
        if condition.get("type") == "Submitted":
            return f"{condition.get('reason')}: {condition.get('message')}"
    return "no Submitted condition"


def _run(status, task):
    for run in status.get("recentRuns") or []:
        if run.get("taskID") == task:
            return run
    return None


def wait(kubectl, name, state_path, run_seconds, delivery_seconds):
    state = kubectl.read_state(state_path)
    deadline = _clock() + float(run_seconds)
    task = state.get("task_id")
    ended = None
    while ended is None:
        status = _status(kubectl.schedule(name))
        if task is None:
            task = _fired_task(status, state)
            if task is not None:
                state["task_id"] = task
                kubectl.write_state(state_path, state)
                print(f"AgentSchedule {name}: the fired run is task {task}")
        if task is not None and status.get("activeTaskID") != task:
            ended = _run(status, task)
        elif (
            task is None
            and status.get("lastHandledRunNow") == state["token"]
            and not status.get("pendingSlot")
            and not status.get("activeTaskID")
        ):
            # Handled with no run: the gateway refused the submission for good.
            raise SystemExit(f"AgentSchedule {name} handled the run-now and started no run ({_submitted_message(status)})")
        if ended is None:
            if _clock() > deadline:
                raise SystemExit(f"AgentSchedule {name}: the fired run did not end within {run_seconds}s of {state['fired_at']}")
            _sleep(POLL_S)
    deadline = _clock() + float(delivery_seconds)
    while not ended.get("delivery") and ended.get("outcome") != OUTCOME_LOST and _clock() <= deadline:
        _sleep(POLL_S)
        ended = _run(_status(kubectl.schedule(name)), task) or ended
    state.update(
        outcome=ended.get("outcome"), completed_at=ended.get("completionTime"), delivery=ended.get("delivery") or ""
    )
    kubectl.write_state(state_path, state)
    print(f"AgentSchedule {name}: task {task} ended {state['outcome']}, delivery {state['delivery'] or 'not reported'}")


def clean(kubectl, name, state_path):
    kubectl.remove_state(state_path)
    try:
        schedule = kubectl.schedule(name)
    except SystemExit as exc:
        print(f"could not read AgentSchedule {name}; its annotation stays: {exc}", file=sys.stderr)
        return
    value = ((schedule.get("metadata") or {}).get("annotations") or {}).get(RUN_NOW_ANNOTATION, "")
    # A handled token is inert; it goes only so the schedule is left as found,
    # and only when it is this script's.
    if value.startswith(TOKEN_PREFIX):
        kubectl.annotate(name, None)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--deployment", required=True)
    parser.add_argument("--container", required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("idle")
    command.add_argument("schedule")
    command.add_argument("seconds")
    for verb in ("fire", "clean"):
        command = commands.add_parser(verb)
        command.add_argument("schedule")
        command.add_argument("state")
    command = commands.add_parser("wait")
    command.add_argument("schedule")
    command.add_argument("state")
    command.add_argument("run_seconds")
    command.add_argument("delivery_seconds")
    args = parser.parse_args(argv)
    kubectl = Kubectl(args.namespace, args.deployment, args.container)
    if args.command == "idle":
        idle(kubectl, args.schedule, args.seconds)
    elif args.command == "fire":
        fire(kubectl, args.schedule, args.state)
    elif args.command == "wait":
        wait(kubectl, args.schedule, args.state, args.run_seconds, args.delivery_seconds)
    else:
        clean(kubectl, args.schedule, args.state)


if __name__ == "__main__":
    main()
