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

  reset <state>                remove a state file an earlier apply left
  idle <schedule> <seconds>    refuse a schedule that is not built-in, then wait until
                               it has no run in flight or claimed, so the run-now
                               starts a run rather than adopting one
  fire --nonce <n> <schedule> <state>
                               record this apply's nonce, when, and a snapshot of the findings queue (each
                               row's id, source, check and hash), in the state file
                               <state> in the gateway's agent container, then set the
                               run-now annotation to a token the schedule has not
                               handled
  wait <schedule> <state> <run-seconds> <delivery-seconds>
                               wait until the fired run has ended, then until the
                               gateway has said how its report was delivered, and
                               record both in the state file
  clean [--stop-run --nonce <n>] <schedule> <state>
                               remove the run-now annotation when it still holds a token
                               this script set, and the state file; with --stop-run (a
                               failed apply) and a state file this apply's nonce wrote,
                               also stop a run provably the fire's, with the schedule's
                               stop annotation

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
``findings_queue_untouched`` diffs the queue against the snapshot.
"""

import argparse
import json
from datetime import datetime
import re
import secrets
import subprocess
import sys
import time

# k8s-operator/api/v1alpha1/agentschedule_types.go.
RUN_NOW_ANNOTATION = "kubeagents.x-k8s.io/run-now"
STOP_ANNOTATION = "kubeagents.x-k8s.io/stop"
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
# An AgentSchedule name, checked before it reaches kubectl's argv.
SCHEDULE_NAME_RE = re.compile(r"^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?\Z")

# kube_agents_bench/findings.py: SESSION_KV_URL, ROW_LIMIT, REQUEST_TIMEOUT_SEC,
# SNAPSHOT_READ and SNAPSHOT_SCRIPT, copied because this file runs on its own
# (main.tf inlines it); the stack's tests hold the copies equal.
SESSION_KV_URL = "http://127.0.0.1:8699"  # sanitizer: allow the Session KV server on the pod's loopback, not an address of anyone's
FINDINGS_ROW_LIMIT = 1000
FINDINGS_TIMEOUT_SEC = 20.0
SNAPSHOT_READ = "__FINDINGS_QUEUE_SNAPSHOT__"
SNAPSHOT_SCRIPT = r"""
import hashlib, json, os, sys, urllib.error, urllib.parse, urllib.request
base, limit, timeout, sentinel = sys.argv[1:5]
out = {"rows": None, "count": 0, "error": None}
token = (os.environ.get("SESSION_KV_API_KEY") or "").strip()
if not token:
    out["error"] = "SESSION_KV_API_KEY is not set in the agent container"
else:
    query = urllib.parse.urlencode({"limit": limit})
    request = urllib.request.Request(base + "/v1/findings?" + query, headers={"Authorization": "Bearer " + token})
    try:
        with urllib.request.urlopen(request, timeout=float(timeout)) as response:
            rows = json.loads(response.read().decode("utf-8")).get("findings")
        if not isinstance(rows, list):
            out["error"] = "GET /v1/findings returned no list of findings"
        else:
            out["count"] = len(rows)
            out["rows"] = {
                str(row.get("id")): {
                    "source": row.get("source"),
                    "check_slug": row.get("check_slug"),
                    "hash": hashlib.sha256(json.dumps(row, sort_keys=True, default=str).encode("utf-8")).hexdigest(),
                }
                for row in rows
                if isinstance(row, dict)
            }
    except (urllib.error.URLError, OSError, ValueError) as exc:
        out["error"] = "GET /v1/findings: %s" % exc
print(sentinel + json.dumps(out))
"""
# The agent container's Python, as the bench's reads pick it.
POD_PYTHON = '/opt/hermes/.venv/bin/python3'
POD_PYTHON_FALLBACK = 'python3'

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

    def annotate(self, name, value, key=RUN_NOW_ANNOTATION):
        """Set the annotation ``key`` to ``value``, or remove it when ``value`` is None."""
        pair = f"{key}-" if value is None else f"{key}={value}"
        self.run(["annotate", RESOURCE, name, pair, "--overwrite"])

    def findings_snapshot(self):
        """The findings queue's snapshot from the agent container (SNAPSHOT_SCRIPT's object)."""
        command = (
            f'PY={POD_PYTHON}; [ -x "$PY" ] || PY={POD_PYTHON_FALLBACK}; "$PY" -c "$0" "$@"'
        )
        reply = self.exec(
            ["sh", "-c", command, SNAPSHOT_SCRIPT, SESSION_KV_URL, str(FINDINGS_ROW_LIMIT), str(FINDINGS_TIMEOUT_SEC), SNAPSHOT_READ]
        )
        marker = reply.find(SNAPSHOT_READ)
        if marker < 0:
            raise SystemExit("the findings queue snapshot printed nothing")
        return json.loads(reply[marker + len(SNAPSHOT_READ):].strip().splitlines()[0])

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


def _require_name(name):
    if not SCHEDULE_NAME_RE.match(name or ""):
        raise SystemExit(f"{name!r} is not an AgentSchedule name (a DNS-1123 label)")


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
    _require_name(name)
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


def fire(kubectl, name, state_path, nonce):
    _require_name(name)
    schedule = kubectl.schedule(name)
    _require_builtin(schedule, name)
    status = _status(schedule)
    # Again, just before the fire: a slot the cadence claimed since `idle`
    # would be adopted, and its run read as this one.
    reason = _busy(status)
    if reason is not None:
        raise SystemExit(f"AgentSchedule {name}: {reason}; not firing over it")
    token = new_token()
    # Before the clock and the fire: every row the run could write is in it.
    snapshot = kubectl.findings_snapshot()
    if snapshot.get("error"):
        raise SystemExit(f"the findings queue could not be read before the fire: {snapshot['error']}")
    fired_at = kubectl.pod_now()
    kubectl.write_state(
        state_path,
        {
            "schedule": name,
            "nonce": nonce,
            "token": token,
            "fired_at": fired_at,
            "known_tasks": _known_tasks(status),
            "findings_snapshot": snapshot,
        },
    )
    kubectl.annotate(name, token)
    print(f"set {RUN_NOW_ANNOTATION}={token} on AgentSchedule {name} at {fired_at}")


def _claim_pending(status, token):
    """Whether the schedule has claimed a run for ``token`` that the gateway has not accepted yet.

    status.pendingSlot.runNow names the run-now token a claimed slot answers
    (AgentSchedulePendingSlot.RunNow); the token is recorded as handled only
    once the claim becomes a run or is dropped.
    """
    slot = status.get("pendingSlot")
    return isinstance(slot, dict) and slot.get("runNow") == token


def _fired_task(status, state):
    """The fired run's task, once the status names one the schedule did not have before the fire.

    Only once the schedule has handled this token: before then the run is at
    most claimed (``pendingSlot.runNow`` is the token) and has no task, and a
    new task is the cadence's, not the fire's.
    """
    if status.get("lastHandledRunNow") != state["token"]:
        return None
    known = set(state.get("known_tasks") or [])
    active = status.get("activeTaskID")
    if active and active not in known:
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
    _require_name(name)
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
            elif _claim_pending(status, state["token"]):
                print(f"AgentSchedule {name}: the run is claimed; waiting for the gateway to accept it")
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


def _parse_time(value):
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return stamp if stamp.tzinfo is not None else None


def _provably_ours(status, state):
    """The run in flight or claimed that the fire started, or None when none provably is.

    Ours is: the task the wait recorded, while it is still activeTaskID; a
    claim whose pendingSlot.runNow is our token (the token is recorded as
    handled only once the claim is accepted or dropped); or, with our token
    handled, an activeTaskID the schedule did not have before the fire whose
    activeTaskStartedAt is at or after the fire. Anything else (a cadence run,
    someone else's run-now, a run-now the gateway refused) is not ours.
    """
    task = state.get("task_id")
    active = status.get("activeTaskID")
    if task and active == task:
        return task
    if _claim_pending(status, state["token"]):
        return "the pending claim"
    if task or status.get("lastHandledRunNow") != state["token"] or not active:
        return None
    started, fired = _parse_time(status.get("activeTaskStartedAt")), _parse_time(state.get("fired_at"))
    if active not in set(state.get("known_tasks") or []) and started and fired and started >= fired:
        return active
    return None


def reset(kubectl, state_path):
    """Remove any state file an earlier apply left, before this one starts."""
    kubectl.remove_state(state_path)


def clean(kubectl, name, state_path, stop_run=False, nonce=None):
    """Undo the fire: the run-now annotation, then, with stop_run, a run provably ours, then the state file.

    With ``stop_run`` (a failed apply), only a state file whose ``nonce`` is
    this apply's is acted on: a file an earlier apply left, or none at all
    (the apply failed before it fired), stops nothing. Our run-now
    annotation goes first, so the token can no longer be claimed; the status
    is then read again and the stop annotation set only for a run
    :func:`_provably_ours` names. A stop set while nothing is claimed or in
    flight is consumed at once and the claim that follows would run anyway,
    so the removal, not a stop, is what keeps an unclaimed fire from running.
    """
    _require_name(name)
    state = None
    if stop_run:
        try:
            read = kubectl.read_state(state_path)
        except (SystemExit, ValueError) as exc:
            print(f"no state file to act on; no run to stop: {exc}", file=sys.stderr)
            read = None
        if isinstance(read, dict) and read.get("token") and nonce and read.get("nonce") == nonce:
            state = read
        elif read is not None:
            print("the state file is not this apply's; no run to stop", file=sys.stderr)
    try:
        schedule = kubectl.schedule(name)
    except SystemExit as exc:
        kubectl.remove_state(state_path)
        print(f"could not read AgentSchedule {name}; its annotation stays: {exc}", file=sys.stderr)
        return
    value = ((schedule.get("metadata") or {}).get("annotations") or {}).get(RUN_NOW_ANNOTATION, "")
    # Only a token this script set: ours when there is a state, else any of
    # this script's prefix (a handled token is inert; it goes so the schedule
    # is left as found).
    if (state is not None and value == state["token"]) or (state is None and value.startswith(TOKEN_PREFIX)):
        kubectl.annotate(name, None)
    if state is not None:
        status = _status(kubectl.schedule(name))
        ours = _provably_ours(status, state)
        if ours is not None:
            token = new_token()
            # Left set: a stop removed before it is handled is a stop lost,
            # and a handled one is inert.
            kubectl.annotate(name, token, key=STOP_ANNOTATION)
            print(f"set {STOP_ANNOTATION}={token} on AgentSchedule {name}: stopping {ours}", file=sys.stderr)
    kubectl.remove_state(state_path)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--deployment", required=True)
    parser.add_argument("--container", required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("idle")
    command.add_argument("schedule")
    command.add_argument("seconds")
    command = commands.add_parser("reset")
    command.add_argument("state")
    command = commands.add_parser("fire")
    command.add_argument("--nonce", required=True)
    command.add_argument("schedule")
    command.add_argument("state")
    command = commands.add_parser("clean")
    command.add_argument("--stop-run", action="store_true")
    command.add_argument("--nonce")
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
    elif args.command == "reset":
        reset(kubectl, args.state)
    elif args.command == "fire":
        fire(kubectl, args.schedule, args.state, args.nonce)
    elif args.command == "wait":
        wait(kubectl, args.schedule, args.state, args.run_seconds, args.delivery_seconds)
    else:
        clean(kubectl, args.schedule, args.state, stop_run=args.stop_run, nonce=args.nonce)


if __name__ == "__main__":
    main()
