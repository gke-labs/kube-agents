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

"""The ``triage_starts`` check: did every admitted alert of a burst start its triage turn.

The Session KV server writes one ``intercepted_events`` row per alert the watcher forwards
(``record_intercepted_event`` in ``agents/platform/scripts/session_kv_server.py``), and for
an alert it admits it opens a Hermes gateway session under the row's ``session_id`` and
sends the triage turn to it (``trigger_agent_troubleshooter``). The turn's ``/chat`` call
stores the turn's message in the default profile's ``state.db`` ``messages`` table. This
passes when every admitted row for the stack's pods has a session with a message there.

A session alone is not a start: a session create that times out on the daemon's side still
commits in Hermes, and a daemon that gives up there leaves a session with no turn.

Rows are joined to this run's pods by ``object_uid``, the pod UID the watcher sends, read
from the cluster rather than bounded by a time. The UIDs come from the record the stack
writes when it plants (``planted_record``, a ConfigMap in the namespace), not from the pods
still there at the check: a bare pod the platform deletes mid-run (a node removed under it)
is not recreated, and its alert was admitted and its start still counts (#5925). Every pod
still in the namespace must be in the record, so a record that leaves out a live pod is an
error rather than a way to drop that pod's lost start. The run's start
(``TranscriptSnapshot.started_at``) is taken after the stack's apply, so it postdates every
row the plant raised, and the namespace is static, so a time window would also take an
earlier repetition's rows.

Its own module rather than a section of ``verifiers.py``, registered through the same
``devops_bench.verifiers`` entry-point group.
"""

import json
import os
import shlex
import subprocess
from typing import Any, Callable, Literal

from devops_bench.verification.base import VERIFIERS, VerificationStatus
from pydantic import Field

from kube_agents_bench.oobe import agent_shell
from kube_agents_bench.verifiers import _OnboardingPollVerifier
from kube_agents_bench.worker_trajectory import DATA_ROOT, FALLBACK_PYTHON, HERMES_PYTHON

# session_kv_server.py: SESSION_KV_DB_PATH's default. The read prefers the container's own
# value, as the server does.
SESSION_KV_DB = "/var/lib/kube-agents/session/session_kv.db"
# The default profile's session store, which the gateway's /api/sessions writes
# (board.STORE_FILE, worker_trajectory.state_db("default")). The daemon's session lands on
# the default profile (_create_gateway_session's docstring).
GATEWAY_STATE_DB = f"{DATA_ROOT}/state.db"
STARTS_READ = "__TRIAGE_STARTS_READ__"
# Session ids a fail names before counting the rest.
MAX_NAMED_SESSIONS = 10
# The cluster read; the namespace and the context follow.
KUBECTL_GET_PODS = ("kubectl", "get", "pods", "-o", "json")
KUBECTL_GET_CONFIGMAP = ("kubectl", "get", "configmap", "-o", "json")
# The record's key: one "<uid> <name>" line per planted pod
# (bench/tf/prebuilt/triage-start-burst/main.tf, step 1).
PLANTED_RECORD_KEY = "pods"
# What read_planted_record returns when the namespace holds no record.
RECORD_ABSENT = "absent"

# Prints every ledger row for the given pod UIDs in the namespace, the ids among their
# session ids that the gateway's store holds, and how many messages each of those has.
# A store that is missing or lacks its table, or a sqlite failure, is printed as an error
# rather than raised, so the verdict names it instead of reading as an unreachable pod.
_READ_SCRIPT = """
import json, os, sqlite3, sys
kv_db, state_db, sentinel, namespace = sys.argv[1:5]
uids = sys.argv[5:]
kv_db = os.environ.get("SESSION_KV_DB_PATH") or kv_db
SQLITE_BUSY_TIMEOUT = 10
out = {"rows": [], "sessions": [], "messages": {}, "error": None}


def ro(path):
    if not os.path.exists(path):
        raise FileNotFoundError("no such store: " + path)
    return sqlite3.connect("file:" + path + "?mode=ro", uri=True, timeout=SQLITE_BUSY_TIMEOUT)


def has_table(conn, name):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)).fetchone() is not None


def marks(values):
    return ",".join("?" for _ in values)


try:
    kv = ro(kv_db)
    if not has_table(kv, "intercepted_events"):
        raise LookupError(kv_db + " has no intercepted_events table")
    if uids:
        for row in kv.execute(
                "SELECT id, object_uid, workload, reason, severity, notified, delivery_error, session_id, created_at"
                " FROM intercepted_events WHERE namespace = ? AND object_uid IN (%s) ORDER BY id" % marks(uids),
                [namespace] + uids):
            out["rows"].append(dict(zip(
                ("id", "object_uid", "workload", "reason", "severity", "notified", "delivery_error",
                 "session_id", "created_at"), row)))
    sids = sorted({r["session_id"] for r in out["rows"] if r["session_id"]})
    store = ro(state_db)
    if not has_table(store, "sessions"):
        raise LookupError(state_db + " has no sessions table")
    if not has_table(store, "messages"):
        raise LookupError(state_db + " has no messages table")
    if sids:
        out["sessions"] = [r[0] for r in store.execute(
            "SELECT id FROM sessions WHERE id IN (%s)" % marks(sids), sids)]
        if out["sessions"]:
            out["messages"] = {sid: n for sid, n in store.execute(
                "SELECT session_id, COUNT(*) FROM messages WHERE session_id IN (%s) GROUP BY session_id"
                % marks(out["sessions"]), out["sessions"])}
except (OSError, LookupError, sqlite3.Error) as exc:
    out["error"] = str(exc)
print(sentinel + json.dumps(out))
"""


def _kubectl(args: list[str], timeout: float) -> subprocess.CompletedProcess[str] | None:
    """Run kubectl against the agent's cluster, or ``None`` if it could not run.

    The same context ``onboarding._kubectl_exec`` reaches the agent pod through
    (``AGENT_CLUSTER_CONTEXT``): the stack plants on the cluster the agent runs in.
    """
    cmd = list(args)
    context = os.environ.get("AGENT_CLUSTER_CONTEXT")
    if context:
        cmd.extend(["--context", context])
    try:
        return subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        return None


def read_planted_record(namespace: str, name: str, timeout: float) -> dict[str, str] | str | None:
    """The pods the stack recorded planting, UID to name; ``RECORD_ABSENT`` if the namespace
    holds no record of that name; ``None`` if it could not be read or is malformed (a line
    that is not two fields, a repeated UID, or no pods at all)."""
    proc = _kubectl([*KUBECTL_GET_CONFIGMAP, name, "-n", namespace], timeout)
    if proc is None:
        return None
    if proc.returncode != 0:
        return RECORD_ABSENT if "NotFound" in proc.stderr else None
    try:
        data = json.loads(proc.stdout).get("data")
    except (json.JSONDecodeError, AttributeError):
        return None
    text = data.get(PLANTED_RECORD_KEY) if isinstance(data, dict) else None
    if not isinstance(text, str):
        return None
    pods = {}
    for line in text.splitlines():
        parts = line.split()
        if not parts:
            continue
        if len(parts) != 2 or parts[0] in pods:
            return None
        pods[parts[0]] = parts[1]
    return pods or None


def read_planted_pods(namespace: str, timeout: float) -> dict[str, str] | None:
    """The pods in ``namespace`` on the agent's cluster, UID to name, or ``None`` if unreadable."""
    proc = _kubectl([*KUBECTL_GET_PODS, "-n", namespace], timeout)
    if proc is None or proc.returncode != 0:
        return None
    try:
        items = json.loads(proc.stdout).get("items")
    except (json.JSONDecodeError, AttributeError):
        return None
    if not isinstance(items, list):
        return None
    pods = {}
    for item in items:
        meta = item.get("metadata") if isinstance(item, dict) else None
        if isinstance(meta, dict) and meta.get("uid"):
            pods[str(meta["uid"])] = str(meta.get("name") or "")
    return pods


def read_command(namespace: str, uids: list[str]) -> str:
    """The ``sh -c`` line that runs the read in the agent container."""
    args = " ".join(shlex.quote(a) for a in [SESSION_KV_DB, GATEWAY_STATE_DB, STARTS_READ, namespace, *uids])
    return (
        f'PY={shlex.quote(HERMES_PYTHON)}; [ -x "$PY" ] || PY={shlex.quote(FALLBACK_PYTHON)}; '
        f'"$PY" -c {shlex.quote(_READ_SCRIPT)} {args}'
    )


def read_starts(
    shell: Callable[[str, float], str], namespace: str, uids: list[str], timeout: float
) -> dict[str, Any] | None:
    """The ledger rows for ``uids`` and the sessions the gateway holds for them, or ``None``."""
    reply = shell(read_command(namespace, uids), timeout)
    marker = reply.rfind(STARTS_READ)
    if marker < 0:
        return None
    try:
        parsed = json.loads(reply[marker + len(STARTS_READ) :])
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict) or not isinstance(parsed.get("rows"), list):
        return None
    return parsed


def admitted(row: dict[str, Any]) -> bool:
    """Whether the daemon admitted the alert, from what its ledger row stored.

    ``notified`` is 1, or 2 on a headless install, for an admitted alert and 0 for one the
    severity filter or the daily limit refused. ``mark_delivery_failed`` resets it to 0 when
    the chat post fails and writes ``delivery_error``, and the triage start still follows
    (``trigger_agent_troubleshooter``), so a non-empty ``delivery_error`` is admitted too.
    """
    return bool(row.get("notified")) or bool(row.get("delivery_error"))


def verdict(
    pods: dict[str, str],
    read: dict[str, Any],
    namespace: str,
    min_alerts: int,
    gone: list[str] | None = None,
) -> tuple[VerificationStatus, str, dict[str, Any]]:
    """Grade one read. Pure, so the tests drive it without a pod.

    ``pods`` is the planted set, UID to name; ``gone`` names those no longer in the namespace
    at the check, reported but not graded.
    """
    gone = sorted(gone or [])
    note = (
        f"; {len(gone)} planted pod(s) were gone from {namespace} by the check, graded from the plant's "
        f"record: {', '.join(gone[:MAX_NAMED_SESSIONS])}"
        if gone
        else ""
    )
    rows = read.get("rows") or []
    taken = [r for r in rows if admitted(r)]
    alerted = {r.get("object_uid") for r in taken}
    sessions = set(read.get("sessions") or [])
    messages = read.get("messages") or {}
    turned = {s for s in sessions if messages.get(s)}
    lost = [r for r in taken if r.get("session_id") not in turned]
    raw: dict[str, Any] = {
        "pods": len(pods),
        "rows": len(rows),
        "admitted": len(taken),
        "pods_alerted": len(alerted),
        "sessions": len(sessions),
        "sessions_with_messages": len(turned),
        "lost": [r.get("session_id") for r in lost],
        "pods_gone": gone,
    }
    if len(pods) < min_alerts:
        return "error", (
            f"{namespace} holds {len(pods)} planted pod(s), fewer than min_alerts ({min_alerts}): the stack did "
            "not plant the burst, or something deleted its pods"
        ), raw
    if len(alerted) < min_alerts:
        return "error", (
            f"only {len(alerted)} of the {len(pods)} pods in {namespace} have an admitted alert in the ledger "
            f"({len(rows)} row(s), {len(rows) - len(taken)} refused), fewer than min_alerts ({min_alerts}). "
            "The plant did not raise the burst, or the daemon refused part of it against its daily Warning "
            f"alert limit: ALERT_DAILY_LIMIT_WARNING must be 0 (off) or at least {min_alerts} plus what the "
            "install has spent today"
        ), raw
    unjoinable = [r.get("id") for r in taken if not r.get("session_id")]
    if unjoinable:
        return "error", (
            f"{len(unjoinable)} admitted ledger row(s) in {namespace} carry no session_id "
            f"(row ids {unjoinable[:MAX_NAMED_SESSIONS]}), so their triage cannot be looked up"
        ), raw
    if lost:
        named = ", ".join(str(r.get("session_id")) for r in lost[:MAX_NAMED_SESSIONS])
        more = f" and {len(lost) - MAX_NAMED_SESSIONS} more" if len(lost) > MAX_NAMED_SESSIONS else ""
        sessionless = sum(1 for r in lost if r.get("session_id") not in sessions)
        return "fail", (
            f"{len(lost)} of {len(taken)} admitted alerts for the pods in {namespace} never started a triage turn "
            f"({sessionless} with no Hermes session, {len(lost) - sessionless} with a session but no message): "
            f"{named}{more}{note}"
        ), raw
    return "pass", (
        f"all {len(taken)} admitted alerts for {len(alerted)} pods in {namespace} started a triage turn{note}"
    ), raw


@VERIFIERS.register("triage_starts")
class TriageStartsVerifier(_OnboardingPollVerifier):
    """Passes when at least ``min_alerts`` pods in ``namespace`` have an admitted alert and every
    admitted alert row for them has a Hermes session with a message (a started turn) in the
    gateway's store.

    The planted pods are read from ``planted_record`` when set (falling back to the live pods
    when the namespace holds no record, as an older stack's), else from the live pods.

    Admitted rows without a started turn are a fail naming them. The pods, the record, the
    Session KV store or the gateway's store unreadable, a live pod the record leaves out, fewer
    than ``min_alerts`` pods or admitted alerts, or an admitted row with no session id, is
    ``status="error"``.
    """

    type: Literal["triage_starts"]
    namespace: str = Field(min_length=1)
    min_alerts: int = Field(ge=1)
    planted_record: str | None = Field(default=None, min_length=1)

    def _check(self, read_timeout: float) -> tuple[VerificationStatus, str, dict[str, Any] | None]:
        live = read_planted_pods(self.namespace, read_timeout)
        if live is None:
            return "error", f"the pods in {self.namespace} could not be listed (kubectl get pods failed)", None
        pods, gone = live, []
        if self.planted_record:
            record = read_planted_record(self.namespace, self.planted_record, read_timeout)
            if record is None:
                return "error", (
                    f"the plant's record {self.namespace}/{self.planted_record} could not be read or is malformed"
                ), None
            if isinstance(record, dict):
                unrecorded = sorted(name for uid, name in live.items() if uid not in record)
                if unrecorded:
                    return "error", (
                        f"{len(unrecorded)} pod(s) in {self.namespace} are not in the plant's record "
                        f"{self.planted_record}: {', '.join(unrecorded[:MAX_NAMED_SESSIONS])}. The record must "
                        "list every pod the stack planted"
                    ), None
                pods = record
                gone = [name for uid, name in record.items() if uid not in live]
        read = read_starts(agent_shell, self.namespace, sorted(pods), read_timeout)
        if read is None:
            return "error", "the agent pod's stores could not be read (kubectl exec failed or the command did not run)", None
        if read.get("error"):
            return "error", f"the Session KV store or the gateway's session store could not be read: {read['error']}", read
        return verdict(pods, read, self.namespace, self.min_alerts, gone)
