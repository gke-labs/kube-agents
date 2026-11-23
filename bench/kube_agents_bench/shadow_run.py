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

"""Checks for a shadow run: the report it kept, and the writes it must not make.

A shadow run (``audit_report.py start/finish --store-only``, which the
runtime's ``PROFILE_TASK_DRY_RUN=true`` requires) runs beside the job it may
replace and writes its report to the report store's shadow stream and nothing
else: no ledger, no forge or git write, no remediation, no findings-queue row,
no chat post. Three checks grade that, each bounded below by the fire time the
``builtin-schedule-run-now`` stack records (``since_state_file``) or by the
run's start:

``shadow_report_stored``
    The envelope the run stored on its shadow stream, which ``stream`` names:
    the triggering AgentSchedule's name, which the worker exports as
    ``PROFILE_TRIGGER_STREAM`` (``audit_report.shadow_stream``), or
    ``<audit>-shadow`` for a run started by hand in the platform shell. The
    broker lets a profile write only under the install's own store key and
    only to its trigger's stream, so a scheduled canary's report is there and
    the ``<audit>-shadow`` fallback is refused to it. The envelope holds
    ``shadow: true``, ``shadow_stream`` naming that stream and ``audit_id``
    naming the audit, and a finding the case names. It is read in the gateway
    pod, where the broker's report-store listener (``audit_report_store.py``,
    on the gateway's ``agent-api-auth`` container) writes what a profile worker
    sends it, onto the agent PVC under ``/opt/data/fleet-audit/reports``, which
    the gateway's agent container mounts too. The read is a ``kubectl exec``
    there running the install's own reader, ``report_query.py`` from the
    root-owned image copy, with ``--root`` on that tree and
    ``--include-shadow``: readers refuse a shadow run's envelope without it.
    The listener's HTTP routes take only the broker's audience-bound token,
    which the harness does not hold and does not need.
``findings_queue_untouched``
    No findings-queue row registered, re-registered or verified in the window
    (``first_seen`` or ``last_verified`` at or after the bound), through the
    Session KV server's ``GET /v1/findings``, as ``findings_item_state`` reads it.
``schedule_report_not_posted``
    The fired run's report went to no chat: the AgentSchedule's own record of
    its delivery (``status.recentRuns[].delivery``, which the operator reads
    back from the gateway) is one that posts nothing.

``github_writes`` with the case's ``writes_alone`` already holds the ledger
issue and remediation pull requests to none.

Its own module rather than a section of ``verifiers.py``, registered through
the same ``devops_bench.verifiers`` entry-point group.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Literal

from devops_bench.verification.base import VERIFIERS, VerificationStatus
from pydantic import Field, field_validator, model_validator

from kube_agents_bench import findings, onboarding, oobe, transcript, verifiers
from kube_agents_bench.verifiers import (
    _NO_RUN_CLOCK_REASON,
    _NO_TRANSCRIPT_REASON,
    LEDGER_AUDIT_IDS,
    AuditId,
    _OnboardingPollVerifier,
    _StoredReportVerifier,
    read_fired_at,
)
from kube_agents_bench.worker_trajectory import FALLBACK_PYTHON, HERMES_PYTHON

# The install's copy of the store's bounded reader in the agent image
# (deploy/docker/Dockerfile copies agents/platform/skills to
# /opt/platform-template/skills and chowns it to root), so the model cannot
# have edited the grader's reader. Its sibling fleet-audit/scripts holds the
# report_status helpers it imports.
READER_PATH = "/opt/platform-template/skills/fleet-audit-reports/scripts/report_query.py"
# report_query.py: INCLUDE_SHADOW_FLAG. Readers refuse a shadow run's envelope
# without it; the canary's comparison and its eval pass it.
INCLUDE_SHADOW_FLAG = "--include-shadow"
# Far above any document's finding count; report_query's `findings` caps here.
READER_FINDINGS_LIMIT = 10000
SHADOW_READ = "__SHADOW_REPORT_READ__"

# Runs in the gateway's agent container: asks report_query.py for the stream's
# newest envelope without its document (`show`), the ids of its findings
# (`findings`), and, when the check reads evidence, each matched finding whole
# (`finding`). Prints one JSON object: the envelopes in the shape
# verifiers.report_store_command prints, or the error that kept the reader from
# answering at all (not JSON: a reader that refused its arguments). A reader
# answer that is an error (no run stored, a shadow envelope refused) is an
# entry with `error`, which grades as unread.
# argv: reader, root, stream, flag, limit, keys (JSON), excerpt phrases (JSON or ""), sentinel.
_SHADOW_DRIVER = r"""
import json, subprocess, sys
reader, root, stream, flag, limit, keys, phrases, sentinel = sys.argv[1:9]
keys = json.loads(keys)
phrases = json.loads(phrases) if phrases else None
def ask(*args):
    done = subprocess.run([sys.executable, reader, *args, "--root", root, flag], capture_output=True, text=True)
    try:
        payload = json.loads(done.stdout.strip().splitlines()[-1])
    except (IndexError, ValueError):
        tail = (done.stderr.strip().splitlines() or ["no output"])[-1]
        return None, "report_query.py %s exited %d: %s" % (args[0], done.returncode, tail)
    if not isinstance(payload, dict):
        return None, "report_query.py %s printed no JSON object" % args[0]
    return payload, payload.get("error")
out = {"envelopes": [], "error": None}
show, err = ask("show", stream)
if show is None:
    out["error"] = err
elif err:
    out["envelopes"].append({"error": err})
else:
    found, err = ask("findings", stream, "--limit", limit)
    if found is None:
        out["error"] = err
    elif err:
        out["envelopes"].append({"repo": show.get("repo"), "error": err})
    else:
        projected = show.get("envelope") or {}
        entry = {key: projected.get(key) for key in keys}
        entry["repo"] = show.get("repo")
        if "started_at" in projected:
            entry["started_at"] = projected["started_at"]
        entry["finding_ids"] = [str(f.get("id", "")) for f in found.get("findings") or [] if isinstance(f, dict)]
        if found.get("truncated"):
            entry["error"] = "findings truncated at %s" % limit
            entry["finished_at"] = None
        if phrases is not None:
            excerpts = {}
            for fid in entry["finding_ids"]:
                if any(p in fid for p in phrases):
                    one, err = ask("finding", stream, fid)
                    finding = (one or {}).get("finding") if not err else None
                    evidence = (finding or {}).get("evidence") or {}
                    excerpts[fid] = str(evidence.get("excerpt") or "") if isinstance(evidence, dict) else ""
            entry["finding_excerpts"] = excerpts
        out["envelopes"].append(entry)
print(sentinel + json.dumps(out))
"""

# audit_report.py: SHADOW_STREAM_SUFFIX, SHADOW_STREAM_RE (the report store's
# stream-name shape, audit_report_store.AUDIT_ID_RE), SHADOW_KEY and
# SHADOW_STREAM_KEY. test_shadow_run.py holds the copies equal where the
# script defines them.
SHADOW_STREAM_SUFFIX = "-shadow"
SHADOW_STREAM_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
SHADOW_KEY = "shadow"
SHADOW_STREAM_KEY = "shadow_stream"
ENVELOPE_AUDIT_KEY = "audit_id"
FORCED_ENVELOPE_KEYS = (SHADOW_KEY, SHADOW_STREAM_KEY, ENVELOPE_AUDIT_KEY)

# The AgentSchedule, read with the runner's kubectl as resource_property reads it.
SCHEDULE_RESOURCE = "agentschedules.kubeagents.x-k8s.io"
# k8s-operator/api/v1alpha1/agentschedule_types.go: AgentScheduleRun.Delivery's
# enum. A post was made or tried, or the run asked for chat and found no home
# channel to take it: a run that posts nothing reads none of these.
POSTING_DELIVERIES = frozenset({"posted", "post_failed", "post_unknown", "stored_no_home"})
QUIET_DELIVERIES = frozenset({"no_delivery", "local", "silent"})
# SQLite's datetime('now'), which findings_queue.py stamps rows with: UTC, no zone.
SQLITE_STAMP_SEPARATOR = " "


def _window_start(
    state_file: str | None, skew_sec: float, timeout: float
) -> tuple[datetime | None, str | None]:
    """The window's lower bound, or the reason there is none.

    The stack's ``fired_at`` in ``state_file`` (in the gateway's agent
    container, where the stack writes it), with no skew: the fire and what the
    run writes are stamped by the same install. Without a state file, the run's
    start less ``skew_sec``.
    """
    if state_file:
        fired = read_fired_at(oobe.agent_shell, state_file, timeout)
        if fired is None:
            return None, f"there is no fired run in {state_file}: the stack did not fire the run"
        return fired, None
    snap = transcript.get()
    if snap is None:
        return None, _NO_TRANSCRIPT_REASON
    if not snap.started_at:
        return None, _NO_RUN_CLOCK_REASON
    return datetime.fromtimestamp(snap.started_at, tz=timezone.utc) - timedelta(seconds=skew_sec), None


def read_state(shell: Callable[[str, float], str], path: str, timeout: float) -> dict[str, Any] | None:
    """The JSON object the stack recorded at ``path``, or None when there is none."""
    reply = shell(f"cat {shlex.quote(path)}", timeout)
    try:
        state = json.loads(reply.strip().splitlines()[-1])
    except (IndexError, ValueError):
        return None
    return state if isinstance(state, dict) else None


def shadow_read_command(stream: str, keys: tuple[str, ...], phrases: list[str] | None) -> str:
    """The ``sh -c`` line that reads ``stream`` with the install's reader, shadow runs included."""
    args = [
        READER_PATH,
        verifiers.REPORT_STORE_ROOT,
        stream,
        INCLUDE_SHADOW_FLAG,
        str(READER_FINDINGS_LIMIT),
        json.dumps(list(keys)),
        json.dumps(phrases) if phrases is not None else "",
        SHADOW_READ,
    ]
    return (
        f'PY={shlex.quote(HERMES_PYTHON)}; [ -x "$PY" ] || PY={shlex.quote(FALLBACK_PYTHON)}; '
        f'"$PY" -c {shlex.quote(_SHADOW_DRIVER)} ' + " ".join(shlex.quote(a) for a in args)
    )


def read_shadow_report(
    shell: Callable[[str, float], str], stream: str, timeout: float, keys: tuple[str, ...], phrases: list[str] | None
) -> tuple[list[dict[str, Any]] | None, str | None]:
    """The stream's envelopes as the reader gives them, or None and why the reader did not answer."""
    reply = shell(shadow_read_command(stream, keys, phrases), timeout)
    marker = reply.find(SHADOW_READ)
    if marker < 0:
        return None, None
    try:
        parsed = json.loads(reply[marker + len(SHADOW_READ):].strip().splitlines()[0])
    except (IndexError, ValueError):
        return None, "the reader's answer was not JSON"
    if not isinstance(parsed, dict):
        return None, "the reader's answer was not a JSON object"
    if parsed.get("error"):
        return None, str(parsed["error"])
    envelopes = parsed.get("envelopes")
    if not isinstance(envelopes, list):
        return None, "the reader's answer carried no envelopes"
    return [entry for entry in envelopes if isinstance(entry, dict)], None


@VERIFIERS.register("shadow_report_stored")
class ShadowReportStoredVerifier(_StoredReportVerifier):
    """Checks that a shadow run of ``audit`` kept its report on its shadow stream.

    ``stream`` is the shadow stream: the AgentSchedule's name for a scheduled
    canary, or ``<audit>-shadow`` for a run started by hand. It must be a store
    stream name and not a live audit's stream, as ``audit_report.shadow_stream``
    refuses one; ``audit`` keeps the roster rule. Reads the stream's newest
    envelope in the gateway pod's report store with the install's
    ``report_query.py --include-shadow`` (the stream's one repository
    directory, the install's own store key for a profile's write; a stream
    under several is refused by the reader), and grades it as
    ``audit_report_stored`` does (the window, the start bound, ``status``,
    ``required_envelope``, the finding and evidence phrases), holding it to
    ``shadow: true``, ``shadow_stream: <stream>`` and ``audit_id: <audit>`` as
    well. A named finding is required: a shadow report with no finding the
    case planted says nothing about the audit.

    No envelope from this run, or a reader that answered the stream holds
    none, is a fail. A pod that cannot be read, a reader that refuses its
    arguments (one without ``--include-shadow``), an unreadable state file or
    a run with no start time is ``status="error"``.
    """

    type: Literal["shadow_report_stored"]
    audit: AuditId
    stream: str
    required_finding_phrases: list[str] = Field(min_length=1)

    @field_validator("stream")
    @classmethod
    def _a_shadow_stream(cls, value: str) -> str:
        if not SHADOW_STREAM_RE.match(value):
            raise ValueError(f"stream {value!r} is not a report-store stream name (a DNS-1123 label)")
        if value in LEDGER_AUDIT_IDS:
            raise ValueError(f"stream {value!r} is a live audit's stream; a shadow run never writes there")
        return value

    @model_validator(mode="after")
    def _forced_keys_are_the_checks(self) -> "ShadowReportStoredVerifier":
        overlap = sorted(set(self.required_envelope) & set(FORCED_ENVELOPE_KEYS))
        if overlap:
            raise ValueError(f"required_envelope names {overlap}, which shadow_report_stored sets itself")
        return self

    def _stream(self) -> str:
        return self.stream

    def _store_shells(self) -> tuple[Callable[[str, float], str], ...]:
        return (oobe.agent_shell,)

    def _store_where(self) -> str:
        return "the gateway's agent container"

    def _state_shell(self) -> Callable[[str, float], str]:
        return oobe.agent_shell

    def _forced_envelope(self) -> dict[str, Any]:
        return {SHADOW_KEY: True, SHADOW_STREAM_KEY: self._stream(), ENVELOPE_AUDIT_KEY: self.audit}

    def _read_listing(
        self, shell: Callable[[str, float], str], stream: str, timeout: float, keys: tuple[str, ...], excerpts: bool
    ) -> tuple[list[dict[str, Any]] | None, str | None]:
        wanted = tuple(dict.fromkeys(("status", "finished_at", *keys)))
        return read_shadow_report(shell, stream, timeout, wanted, self.required_finding_phrases if excerpts else None)


def _sqlite_stamp(value: Any) -> datetime | None:
    """A findings-queue stamp as an aware UTC time; None for null; ValueError when it does not parse."""
    if value is None or value == "":
        return None
    stamp = datetime.fromisoformat(str(value).replace(SQLITE_STAMP_SEPARATOR, "T", 1))
    return stamp if stamp.tzinfo is not None else stamp.replace(tzinfo=timezone.utc)


@VERIFIERS.register("findings_queue_untouched")
class FindingsQueueUntouchedVerifier(_OnboardingPollVerifier):
    """Checks that no findings-queue row was written in the window.

    Reads the queue's rows (under ``project`` when given, else every row)
    through the Session KV server's ``GET /v1/findings`` in the gateway's agent
    container (:mod:`kube_agents_bench.findings`). A row whose ``first_seen``
    (its insert) or ``last_verified`` (a registration of it again, or a
    verification) is at or after the window's bound fails, and the reason names
    each such row. The stamps are whole seconds, so the bound is floored to the
    second: a write in the second of the fire counts.

    A queue that answers with the read's whole row cap cannot show that a row
    is absent, so that is ``status="error"``, as are an unreadable pod, a
    queue that refuses the read, a stamp that does not parse, and a missing
    state file.
    """

    type: Literal["findings_queue_untouched"]
    project: str = ""
    since_state_file: str | None = None
    max_clock_skew_sec: float = Field(default=120.0, ge=0)

    def _check(self, read_timeout: float) -> tuple[VerificationStatus, str, dict[str, Any] | None]:
        since, why = _window_start(self.since_state_file, self.max_clock_skew_sec, read_timeout)
        if since is None:
            return "error", str(why), None
        bound = since.replace(microsecond=0)
        read = findings.read_project(oobe.agent_shell, self.project, read_timeout)
        if read is None:
            return "error", "the gateway's agent container could not be read (kubectl exec failed or the command did not run)", None
        if read["error"]:
            return "error", f"the findings queue could not be read: {read['error']}", None
        rows = read["findings"]
        scope = f"under project {self.project!r}" if self.project else "in the queue"
        if len(rows) >= findings.ROW_LIMIT:
            return "error", f"the read returned its cap of {findings.ROW_LIMIT} rows {scope}, so a row in the window may be past it", None
        written: dict[str, str] = {}
        for row in rows:
            try:
                stamps = {key: _sqlite_stamp(row.get(key)) for key in ("first_seen", "last_verified")}
            except ValueError:
                return "error", f"row {row.get('id')!r} has a stamp that does not parse: {row.get('first_seen')!r}, {row.get('last_verified')!r}", None
            hits = [f"{key} {stamp.isoformat()}" for key, stamp in stamps.items() if stamp is not None and stamp >= bound]
            if hits:
                written[str(row.get("id"))] = ", ".join(hits)
        raw = {"since": bound.isoformat(), "rows": len(rows), "written": written}
        if written:
            return (
                "fail",
                f"{len(written)} findings-queue row(s) {scope} written since {bound.isoformat()}: "
                + "; ".join(f"{fid} ({when})" for fid, when in written.items()),
                raw,
            )
        return "pass", f"none of {len(rows)} findings-queue row(s) {scope} was written since {bound.isoformat()}", raw


def read_schedule(name: str, timeout: float) -> dict[str, Any] | None:
    """The AgentSchedule ``name`` in the install's namespace, as kubectl prints it, or None."""
    cmd = ["kubectl", "get", SCHEDULE_RESOURCE, name, "-n", os.environ.get("AGENT_NAMESPACE", onboarding.DEFAULT_AGENT_NAMESPACE), "-o", "json"]
    context = os.environ.get("AGENT_CLUSTER_CONTEXT")
    if context:
        cmd.extend(["--context", context])
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    try:
        schedule = json.loads(proc.stdout)
    except ValueError:
        return None
    return schedule if isinstance(schedule, dict) else None


@VERIFIERS.register("schedule_report_not_posted")
class ScheduleReportNotPostedVerifier(_OnboardingPollVerifier):
    """Checks that the fired run of ``schedule`` posted its report nowhere.

    Reads the AgentSchedule's ``status.recentRuns``. The run graded is the one
    the ``builtin-schedule-run-now`` stack recorded as ``task_id`` in
    ``since_state_file``; without one, every run whose ``slotTime`` is at or
    after the window's bound less ``max_clock_skew_sec`` (the operator stamps
    the slot with its own clock). Passes when each graded run's ``delivery``
    is one that posts nothing (``local``, ``silent``, ``no_delivery``). One
    that posted or tried to (``posted``, ``post_failed``, ``post_unknown``), or
    that asked for chat and found no home channel (``stored_no_home``: a run
    that would post on an install with one), fails.

    No graded run (the stack's task is not in the ring, or no run in the
    window), a run whose delivery the gateway has not reported yet, an
    unknown delivery, an unreadable schedule or an unreadable state file is
    ``status="error"``: there is nothing to grade.
    """

    type: Literal["schedule_report_not_posted"]
    schedule: str = Field(min_length=1)
    since_state_file: str | None = None
    max_clock_skew_sec: float = Field(default=120.0, ge=0)

    def _check(self, read_timeout: float) -> tuple[VerificationStatus, str, dict[str, Any] | None]:
        task = None
        if self.since_state_file:
            state = read_state(oobe.agent_shell, self.since_state_file, read_timeout)
            task = (state or {}).get("task_id")
        since, why = _window_start(self.since_state_file, self.max_clock_skew_sec, read_timeout)
        if since is None:
            return "error", str(why), None
        schedule = read_schedule(self.schedule, read_timeout)
        if schedule is None:
            return "error", f"AgentSchedule {self.schedule} could not be read", None
        runs = [run for run in (schedule.get("status") or {}).get("recentRuns") or [] if isinstance(run, dict)]
        if task:
            graded = [run for run in runs if run.get("taskID") == task]
            missing = f"the fired run {task} is not in AgentSchedule {self.schedule}'s recent runs"
        else:
            bound = since - timedelta(seconds=self.max_clock_skew_sec)
            graded = []
            for run in runs:
                try:
                    slot = datetime.fromisoformat(str(run.get("slotTime")))
                except ValueError:
                    continue
                if slot.tzinfo is not None and slot >= bound:
                    graded.append(run)
            missing = f"AgentSchedule {self.schedule} has no run since {bound.isoformat()}"
        raw = {"runs": [{k: run.get(k) for k in ("taskID", "slotTime", "outcome", "delivery")} for run in graded]}
        if not graded:
            return "error", missing, raw
        posted = [run for run in graded if run.get("delivery") in POSTING_DELIVERIES]
        if posted:
            return (
                "fail",
                "; ".join(f"run {run.get('taskID')}: delivery {run.get('delivery')}" for run in posted),
                raw,
            )
        unread = [run for run in graded if run.get("delivery") not in QUIET_DELIVERIES]
        if unread:
            return (
                "error",
                "; ".join(f"run {run.get('taskID')}: delivery {run.get('delivery') or 'not reported yet'}" for run in unread),
                raw,
            )
        return (
            "pass",
            "; ".join(f"run {run.get('taskID')}: delivery {run.get('delivery')}" for run in graded),
            raw,
        )
