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
no chat post. Three checks grade that, against what the
``builtin-schedule-run-now`` stack records in ``since_state_file`` before and
after it fires the run. Two of them are bounded below by the fire time it
records (or by the run's start, without a state file); the third diffs the
findings queue against the snapshot it records:

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
    root-owned image copy, with ``--root`` on that tree, ``--repo`` naming the
    install's own store key (decided as the broker decides it, from the
    gitops-state ConfigMap), and ``--include-shadow``: readers refuse a shadow
    run's envelope without it.
    The listener's HTTP routes take only the broker's audience-bound token,
    which the harness does not hold and does not need.
``findings_queue_untouched``
    The audit's findings-queue rows are as they were before the fire: the
    stack's snapshot (each row's hash over every column, through the Session
    KV server's ``GET /v1/findings``, as ``findings_item_state`` reads the
    queue) diffed against the same snapshot now, over the rows of the audit's
    ``source`` (and ``check_prefix`` family). Any added, removed or changed row
    fails; other writers' rows are read and ignored.
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
    fired_at_of,
    read_state_file,
)
from kube_agents_bench.worker_trajectory import FALLBACK_PYTHON, HERMES_PYTHON

# The install's copy of the store's bounded reader in the agent image
# (deploy/docker/Dockerfile copies agents/platform/skills to
# /opt/platform-template/skills and chowns it to root), so the model cannot
# have edited the grader's reader. Its sibling fleet-audit/scripts holds the
# report_status helpers it imports.
READER_PATH = "/opt/platform-template/skills/fleet-audit-reports/scripts/report_query.py"
# The reader's helpers in the same image tree, which list the ring for the
# fired task's entry.
READER_HELPERS_DIR = "/opt/platform-template/skills/fleet-audit/scripts"
# report_query.py: INCLUDE_SHADOW_FLAG. Readers refuse a shadow run's envelope
# without it; the canary's comparison and its eval pass it.
INCLUDE_SHADOW_FLAG = "--include-shadow"
# Where the agent image keeps gitops_workspace.py (deploy/docker/Dockerfile copies
# agents/platform/scripts to /opt/defaults/scripts), the reader of the
# gitops-state ConfigMap mounted at /etc/gitops in the gateway's agent container.
INSTALL_SCRIPTS_DIR = "/opt/defaults/scripts"
# audit_report.py: NO_FORGE_REPO, the store key an install with no publish
# repository writes under (credential_proxy.REPORT_NO_FORGE_STORE_KEY decoded).
NO_FORGE_REPO = "no_forge/local"
# Far above any document's finding count; report_query's `findings` caps here.
READER_FINDINGS_LIMIT = 10000
SHADOW_READ = "__SHADOW_REPORT_READ__"

# Runs in the gateway's agent container: asks report_query.py for one envelope
# of the stream without its document (`show`), the ids of its findings
# (`findings`), and, when the check reads evidence, each matched finding whole
# (`finding`). Prints one JSON object: the envelopes in the shape
# verifiers.report_store_command prints, or the error that kept the read from
# answering at all (a reader that refused its arguments, an unreadable install
# key, a truncated findings list). A reader answer that is an error (no run
# stored, a shadow envelope refused) is an entry with `error`, which grades as
# unread.
#
# Every question names the install's own store key with --repo, decided as the
# broker decides the one key a profile may write under
# (credential_proxy.report_publish_store_key_for, over
# gitops_workspace.get_managed_repos and get_gitops_repo): no managed
# repository is no_forge; exactly one is that one; several is the gitops_repo
# one when the key names a registered repository, else no_forge. A list that
# cannot be read is an error, as the broker refuses the write then.
#
# With a task, the envelope is the fired run's: the ring entry under that key
# whose writer sidecar (audit_report_store.py, `runs/<stamp>.writer`, which the
# broker stamps `builtin-schedule:<stream>:<task>`) names the task and holds the
# SHA-256 of the entry's own bytes. It is asked for with --run, so a later run
# of the schedule cannot stand in for it. Without a task, the newest envelope.
# Options go before `--` and the stream and finding id after it, so no id is
# read as an option.
# argv: reader, root, stream, flag, limit, keys (JSON), excerpt phrases (JSON or ""),
# scripts dir, no-forge repo, task ("" for none), writer prefix, reader helpers dir, sentinel.
_SHADOW_DRIVER = r"""
import hashlib, json, os, subprocess, sys
reader, root, stream, flag, limit, keys, phrases, scripts, no_forge, task, writer_prefix, helpers, sentinel = sys.argv[1:14]
keys = json.loads(keys)
phrases = json.loads(phrases) if phrases else None
out = {"envelopes": [], "error": None, "install_repo": None, "run": None}
def finish(error=None):
    if error:
        out["error"] = error
    print(sentinel + json.dumps(out))
    sys.exit(0)
try:
    sys.path.insert(0, scripts)
    import gitops_workspace
    managed = list(gitops_workspace.get_managed_repos())
    if not managed:
        install = no_forge
    elif len(managed) == 1:
        install = managed[0]
    else:
        chosen = gitops_workspace.get_gitops_repo(managed) if gitops_workspace.gitops_repo_key_text() else None
        install = chosen or no_forge
except Exception as exc:
    finish("the install's store key could not be read: %s: %s" % (type(exc).__name__, exc))
out["install_repo"] = install
run = None
if task:
    try:
        sys.path.insert(0, helpers)
        import report_status as rs
        runs_dir = os.path.join(rs.store_path(root, stream, install), rs.RUNS_DIR)
        names = rs.list_runs(root, stream, install)
    except Exception as exc:
        finish("the ring under %s could not be listed: %s: %s" % (install, type(exc).__name__, exc))
    wanted = "%s:%s:%s" % (writer_prefix, stream, task)
    for name in reversed(names):
        stem = name[: -len(".json")]
        try:
            with open(os.path.join(runs_dir, stem + ".writer"), "rb") as handle:
                sidecar = json.loads(handle.read().decode("ascii"))
            with open(os.path.join(runs_dir, name), "rb") as handle:
                digest = hashlib.sha256(handle.read()).hexdigest()
        except (OSError, ValueError):
            continue
        if isinstance(sidecar, dict) and sidecar.get("writer") == wanted and sidecar.get("sha256") == digest:
            run = stem
            break
    if run is None:
        out["envelopes"].append({"repo": install, "error": "no ring entry under %s was written by task %s" % (install, task)})
        finish()
    out["run"] = run
def ask(command, positionals, options=()):
    argv = [sys.executable, reader, command, "--root", root, "--repo", install, flag, *options]
    if run is not None:
        argv += ["--run", run]
    done = subprocess.run([*argv, "--", *positionals], capture_output=True, text=True)
    try:
        payload = json.loads(done.stdout.strip().splitlines()[-1])
    except (IndexError, ValueError):
        tail = (done.stderr.strip().splitlines() or ["no output"])[-1]
        return None, "report_query.py %s exited %d: %s" % (command, done.returncode, tail)
    if not isinstance(payload, dict):
        return None, "report_query.py %s printed no JSON object" % command
    return payload, payload.get("error")
show, err = ask("show", [stream])
if show is None:
    finish(err)
if err:
    out["envelopes"].append({"repo": install, "error": err})
    finish()
found, err = ask("findings", [stream], ["--limit", limit])
if found is None:
    finish(err)
if err:
    out["envelopes"].append({"repo": show.get("repo"), "error": err})
    finish()
if found.get("truncated"):
    finish("the findings list of %s was truncated at %s; the named finding may be past it" % (stream, limit))
projected = show.get("envelope") or {}
entry = {key: projected.get(key) for key in keys}
entry["repo"] = show.get("repo")
if "started_at" in projected:
    entry["started_at"] = projected["started_at"]
entry["finding_ids"] = [str(f.get("id", "")) for f in found.get("findings") or [] if isinstance(f, dict)]
if phrases is not None:
    excerpts = {}
    for fid in entry["finding_ids"]:
        if any(p in fid for p in phrases):
            one, err = ask("finding", [stream, fid])
            finding = (one or {}).get("finding") if not err else None
            evidence = (finding or {}).get("evidence") or {}
            excerpts[fid] = str(evidence.get("excerpt") or "") if isinstance(evidence, dict) else ""
    entry["finding_excerpts"] = excerpts
out["envelopes"].append(entry)
finish()
"""
# audit_report_store.py: REPORT_WRITER_BUILTIN, the writer a profile task's
# ring entries are stamped with, `builtin-schedule:<stream>:<task id>`.
REPORT_WRITER_BUILTIN = "builtin-schedule"

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
# enum. A post was made or tried, or the run's delivery was chat and only its
# outcome kept it from posting: `stored_no_home` (no home channel to take it)
# and `silent` (a clean run, which chat delivery skips; a run with findings
# would have posted). A run whose delivery cannot post reads none of these.
POSTING_DELIVERIES = frozenset({"posted", "post_failed", "post_unknown", "stored_no_home", "silent"})
QUIET_DELIVERIES = frozenset({"no_delivery", "local"})


def _window_start(
    state_file: str | None, state: dict[str, Any] | None, skew_sec: float
) -> tuple[datetime | None, str | None]:
    """The window's lower bound, or the reason there is none.

    The stack's ``fired_at`` in ``state`` (``state_file`` as read once for the
    poll, in the gateway's agent container), with no skew: the fire and what
    the run writes are stamped by the same install. Without a state file, the
    run's start less ``skew_sec``.
    """
    if state_file:
        fired = fired_at_of(state)
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
    return read_state_file(shell, path, timeout)


def shadow_read_command(stream: str, keys: tuple[str, ...], phrases: list[str] | None, task: str | None = None) -> str:
    """The ``sh -c`` line that reads ``stream`` with the install's reader, shadow runs included.

    With ``task``, the ring entry that task wrote; without, the newest envelope.
    """
    args = [
        READER_PATH,
        verifiers.REPORT_STORE_ROOT,
        stream,
        INCLUDE_SHADOW_FLAG,
        str(READER_FINDINGS_LIMIT),
        json.dumps(list(keys)),
        json.dumps(phrases) if phrases is not None else "",
        INSTALL_SCRIPTS_DIR,
        NO_FORGE_REPO,
        task or "",
        REPORT_WRITER_BUILTIN,
        READER_HELPERS_DIR,
        SHADOW_READ,
    ]
    return (
        f'PY={shlex.quote(HERMES_PYTHON)}; [ -x "$PY" ] || PY={shlex.quote(FALLBACK_PYTHON)}; '
        f'"$PY" -c {shlex.quote(_SHADOW_DRIVER)} ' + " ".join(shlex.quote(a) for a in args)
    )


def read_shadow_report(
    shell: Callable[[str, float], str],
    stream: str,
    timeout: float,
    keys: tuple[str, ...],
    phrases: list[str] | None,
    task: str | None = None,
) -> tuple[list[dict[str, Any]] | None, str | None]:
    """The stream's envelopes as the reader gives them, or None and why the reader did not answer."""
    reply = shell(shadow_read_command(stream, keys, phrases, task), timeout)
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
    refuses one; ``audit`` keeps the roster rule. Reads one envelope of the
    stream under the install's own store key in the gateway pod's report
    store, with the install's ``report_query.py --include-shadow --repo
    <key>``. The key is decided as the broker decides the only key a profile
    may write under, from the gitops-state ConfigMap the gateway mounts, so a
    document under any other key is not this run's and is not read. With
    ``since_state_file``, the envelope is the fired run's own: the ring entry
    whose writer sidecar the broker stamped ``builtin-schedule:<stream>:<task>``
    for the ``task_id`` the stack recorded (the store-only finish records no
    task in the envelope), read with ``--run``, so a later run of the schedule
    can neither mask it nor pass for it; no such entry is a fail. Without a
    state file, the newest envelope. It grades the envelope as
    ``audit_report_stored`` does (the window, the start bound, ``status``,
    ``required_envelope``, the finding and evidence phrases), holding it to
    ``shadow: true``, ``shadow_stream: <stream>`` and ``audit_id: <audit>`` as
    well. A named finding is required: a shadow report with no finding the
    case planted says nothing about the audit.

    Scope: a scheduled canary, whose profile worker writes through the broker
    onto the gateway pod's store. A hand run's ``<audit>-shadow`` stream
    written by a shell on the sandbox pod's own store is not read.

    No envelope from this run, or a reader that answered the stream holds
    none under the install's key, is a fail. A pod that cannot be read, an
    install key that cannot be read, a reader that refuses its arguments (one
    without ``--include-shadow``), a truncated findings list, a state file
    that is unreadable or names no task, or a run with no start time is
    ``status="error"``.
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
        self,
        shell: Callable[[str, float], str],
        stream: str,
        timeout: float,
        keys: tuple[str, ...],
        excerpts: bool,
        state: dict[str, Any] | None,
    ) -> tuple[list[dict[str, Any]] | None, str | None]:
        task = None
        if self.since_state_file:
            task = (state or {}).get("task_id")
            if not task:
                return None, f"{self.since_state_file} names no fired task, so the run's own ring entry cannot be found"
        wanted = tuple(dict.fromkeys(("status", "finished_at", *keys)))
        return read_shadow_report(shell, stream, timeout, wanted, self.required_finding_phrases if excerpts else None, task)


# findings_queue.py: SOURCES, the writers a row names, and the check-family
# grammar (CHECK_FAMILY_RE, CHECK_FAMILY_SEPARATOR) a listing narrows by.
FindingSource = Literal["inventory", "event-watcher", "audit", "upgrade-readiness"]
CHECK_FAMILY_SEPARATOR = "-"
CHECK_FAMILY_RE = re.compile(rf"^[a-z0-9]+{re.escape(CHECK_FAMILY_SEPARATOR)}$")
# How many row ids a reason names before it counts the rest.
MAX_ROWS_NAMED = 10


def _in_family(check_slug: Any, family: str) -> bool:
    """Whether ``check_slug`` is in ``family``, as findings_queue.list_findings folds it."""
    slug = str(check_slug or "").lower()
    return slug.startswith(family) or slug == family[: -len(CHECK_FAMILY_SEPARATOR)]


def _named(ids: list[str]) -> str:
    shown = ", ".join(ids[:MAX_ROWS_NAMED])
    return shown + (f" and {len(ids) - MAX_ROWS_NAMED} more" if len(ids) > MAX_ROWS_NAMED else "")


@VERIFIERS.register("findings_queue_untouched")
class FindingsQueueUntouchedVerifier(_OnboardingPollVerifier):
    """Checks that the run under test left the audit's findings-queue rows exactly as they were.

    The ``builtin-schedule-run-now`` stack records, before it fires the run, a
    snapshot of the queue in ``since_state_file``: each row's id, ``source``,
    ``check_slug`` and the SHA-256 of the whole row as the Session KV server's
    ``GET /v1/findings`` returns it (:data:`kube_agents_bench.findings.SNAPSHOT_SCRIPT`).
    This takes the same snapshot now, in the gateway's agent container, and
    diffs the two over the rows the audit would write: those whose ``source``
    is ``source`` and, with ``check_prefix`` (a check family, as the queue's
    listing reads it), whose check is in that family. A row in scope that was
    added, removed or changed in any column fails, naming it: a registration,
    a registration again, a verification (an unverifiable one included, which
    moves a row to stale and leaves ``last_verified`` alone), an absence
    downgrade across a cluster, a patch and a surfacing mark all change the
    row. Rows out of scope are other writers' (the event watcher, other
    schedules, the turn) and are not read.

    A missing or failed snapshot in the state file, a read now that fails, and
    either snapshot at the route's row cap (absence cannot be shown past it)
    are ``status="error"``.
    """

    type: Literal["findings_queue_untouched"]
    since_state_file: str = Field(min_length=1)
    source: FindingSource
    check_prefix: str | None = None

    @field_validator("check_prefix")
    @classmethod
    def _a_family(cls, value: str | None) -> str | None:
        if value is not None and not CHECK_FAMILY_RE.fullmatch(value):
            raise ValueError(f"check_prefix {value!r} is not a check family: one segment and its separator, as 'upgrade-' is")
        return value

    def _in_scope(self, row: Any) -> bool:
        if not isinstance(row, dict) or row.get("source") != self.source:
            return False
        return self.check_prefix is None or _in_family(row.get("check_slug"), self.check_prefix)

    def _check(self, read_timeout: float) -> tuple[VerificationStatus, str, dict[str, Any] | None]:
        state = read_state(oobe.agent_shell, self.since_state_file, read_timeout)
        before = (state or {}).get("findings_snapshot")
        if not isinstance(before, dict) or not isinstance(before.get("rows"), dict):
            return "error", f"{self.since_state_file} holds no findings-queue snapshot: the stack did not take one before the fire", None
        if before.get("error"):
            return "error", f"the snapshot before the fire failed: {before['error']}", None
        after = findings.read_snapshot(oobe.agent_shell, read_timeout)
        if after is None:
            return "error", "the gateway's agent container could not be read (kubectl exec failed or the command did not run)", None
        if after.get("error") or not isinstance(after.get("rows"), dict):
            return "error", f"the findings queue could not be read: {after.get('error') or 'no rows'}", None
        for name, snap in (("before the fire", before), ("now", after)):
            if int(snap.get("count") or 0) >= findings.ROW_LIMIT:
                return "error", f"the snapshot {name} holds the route's cap of {findings.ROW_LIMIT} rows, so a row past it is unseen", None
        old = {fid: row for fid, row in before["rows"].items() if self._in_scope(row)}
        new = {fid: row for fid, row in after["rows"].items() if self._in_scope(row)}
        added = sorted(set(new) - set(old))
        removed = sorted(set(old) - set(new))
        changed = sorted(fid for fid in set(old) & set(new) if old[fid].get("hash") != new[fid].get("hash"))
        # A row that moved into or out of scope (its source or check rewritten) is a change too.
        moved = sorted(
            fid for fid in (set(old) ^ set(new))
            if fid in before["rows"] and fid in after["rows"]
        )
        added = [fid for fid in added if fid not in moved]
        removed = [fid for fid in removed if fid not in moved]
        changed = sorted(set(changed) | set(moved))
        scope = f"source {self.source!r}" + (f", checks {self.check_prefix}*" if self.check_prefix else "")
        raw = {"scope": scope, "rows": len(new), "added": added, "removed": removed, "changed": changed}
        if added or removed or changed:
            parts = [f"{label} {_named(ids)}" for label, ids in (("added", added), ("removed", removed), ("changed", changed)) if ids]
            return "fail", f"findings-queue rows ({scope}) written since the fire: " + "; ".join(parts), raw
        return "pass", f"all {len(new)} findings-queue row(s) ({scope}) are as they were before the fire", raw


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
    after the window's bound, less ``max_clock_skew_sec`` once (the operator
    stamps the slot with its own clock). Passes when each graded run's ``delivery``
    is one that cannot post (``local``, ``no_delivery``). One that posted or
    tried to (``posted``, ``post_failed``, ``post_unknown``), or whose delivery
    was chat and only the outcome kept it quiet (``stored_no_home``: no home
    channel; ``silent``: a clean run, which chat delivery skips; either would
    post on another install or another day), fails.

    No graded run (the stack's task is not in the ring, or no run in the
    window), a run whose delivery the gateway has not reported yet, an
    unknown delivery, an unreadable schedule or an unreadable state file is
    ``status="error"``: there is nothing to grade.
    """

    type: Literal["schedule_report_not_posted"]
    schedule: str = Field(min_length=1)

    @field_validator("schedule")
    @classmethod
    def _a_schedule_name(cls, value: str) -> str:
        if not SHADOW_STREAM_RE.match(value):
            raise ValueError(f"schedule {value!r} is not an AgentSchedule name (a DNS-1123 label)")
        return value
    since_state_file: str | None = None
    max_clock_skew_sec: float = Field(default=120.0, ge=0)

    def _check(self, read_timeout: float) -> tuple[VerificationStatus, str, dict[str, Any] | None]:
        state = read_state(oobe.agent_shell, self.since_state_file, read_timeout) if self.since_state_file else None
        task = (state or {}).get("task_id")
        since, why = _window_start(self.since_state_file, state, self.max_clock_skew_sec)
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
            # The skew once: _window_start took it off the run's start already;
            # the stack's fire time is the pod's clock, the slot the operator's.
            bound = since - timedelta(seconds=self.max_clock_skew_sec) if self.since_state_file else since
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
