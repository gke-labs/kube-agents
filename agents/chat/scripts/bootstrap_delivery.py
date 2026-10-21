#!/usr/bin/env python3
"""Deterministic (no-LLM) delivery for first-time onboarding.

This is the delivery stage of the ``oobe`` cron job (``oobe.py``), which runs
with ``no_agent: true``. Its stdout is delivered as written by the cron
scheduler to the job's configured target (``deliver: origin`` — the chat the
user first spoke in, bound by the ``bootstrap_onboarding`` plugin). It backed
the ``bootstrap-inventory-delivery`` job before ``oobe`` took it over; that
entry ships disabled.

Delivery is claimed exactly once, and only when discovery has finished AND a
human has connected:

- ``.user_aligned`` present -> a human has opened the chat (set by the plugin;
  never by a background task — see the plugin README).
- ``.bootstrap_completed`` absent -> the report has not been delivered yet.
- ``INVENTORY.md`` present  -> the background scan has produced the report.

The two markers are this pod's, and are checked first. The report is not: the
prioritization worker writes it through its terminal, and with the shell
sandbox on that terminal is the sandbox pod, whose data volume this pod does
not mount. So the report is read over ``sandbox_exec.read_bytes`` when the
sandbox is on, and from ``HERMES_HOME`` when it is off, and only once both
markers say a delivery is due — the ssh read is the one check with a cost.

When all three hold, the script claims delivery, prints ``INVENTORY.md``
(as written, or reshaped by ``inventory_presenter`` when ``KAGE_SLACK_UX`` is on
and the job is bound to Slack) followed by the audits note, and sets the report aside where it was read. Otherwise it
prints nothing, which the ``no_agent`` cron path treats as a silent run (no
message). A report it cannot read, or one over ``REPORT_MAX_BYTES``, fails the
run instead (exit 1), which the scheduler posts as an alert; an unreachable
sandbox stays silent and is retried on the next tick. The first run
``RETIRE_AFTER_SECONDS`` or more after a delivery removes the two onboarding
cron jobs; ``_retire_jobs`` says why the delivering run cannot.

With the flag on, a Slack origin and the credential proxy's Slack relay in the
environment, the report goes out as Block Kit instead (a headline with the
total, the top two rows, the rest folded, and "Fix the first one" and "See all N"
buttons), posted here through ``slack_blocks_post`` because the scheduler's delivery
takes text only. Under ``spec.mode: next``, on an install whose job was linked to a Slack chat before
the switch and that has a Slack home channel, the same card goes through the A2A gateway as a chat message instead
(``_posted_through_gateway``).

Under ``spec.mode: next`` the plugin may instead have recorded the conversation the user first
wrote in (``CONVERSATION_FILE``, with ``CONVERSATION_ALIGNED_MARKER`` as the human-present marker).
That record wins over a job origin. The run then never prints the report, since the scheduler
cannot reach a gateway conversation: it checks before the claim that this child's env carries the
bus address, a bus identity and the gateway's platform, posts the report
and the audits note with ``a2a notify --conversation``, marks the findings shown only once it keeps
the claim, and releases the claim to try again after a wait (on the card path for anything short of a
post, since the next attempt's update key edits a card that landed; on the text path for
``NOT_POSTED``; on either for a claim that outlives the longest post with no outcome), up to
``CONVERSATION_MAX_ATTEMPTS`` (``CONVERSATION_ATTEMPTS_FILE`` records why). So on that path a claim
is one attempt rather than the delivery, and the marks follow the post rather than the claim.
Then only the audits note below is printed, as a second message; any failure prints the
text. A failure after the request was sent may have posted, so that path can send the
report twice; see ``_posted_as_blocks``.

Every delivery closes with a short note on the first-run audits, read off the ``oobe`` stage's
marker by ``_audits_note``: the report's scan leaves the workload checks to those audits (except
on an install whose kept marker records an older stage's no-repository skip, where the scan ran
them), and a short report would otherwise read as a healthy fleet. An audit the stage started or
adopted whose summary failed to deliver, as every one that finished before anyone opened the chat
does for want of a home channel, is named (``_undelivered_audits``). The text path appends it to
the report; the
card paths print it on their own, since the card leaves closing lines out.

Just before the claim, the run registers the sweep's findings in the
findings queue from this pod, from the items and scores the prioritization
worker left beside the report (``_register_findings`` says why the worker's
own registration is not enough). Between the claim and the first byte of
stdout, it marks the report's findings shown in the findings queue, as the paced publisher ``first_report``,
from ``INVENTORY.shown.json`` (what ``inventory_findings.py select`` chose).
That counts them against the day's limit and keeps the hourly findings nudge,
whose hold ends at the claim, from announcing them again as new. The marks are
best-effort: a missing file or an unreachable queue is logged to stderr and the
report is delivered regardless, and the registration is best-effort the
same way.

The claim is what makes "one delivery run per report" true rather than merely likely.
``.bootstrap_completed`` is created with ``O_CREAT | O_EXCL`` *before* anything
reaches stdout, so of two runs racing on the same report — a scheduled tick and
the plugin's ``trigger_job``, say — exactly one can win the create and emit;
the loser exits silently. Checking the marker and then writing it after
delivery would leave both runs inside the same window, and the user would be
sent the entire onboarding report twice.

Because the prioritization stage writes a finished, presentation-ready
``INVENTORY.md``, no LLM is involved in delivery: what that stage produced is
what the user sees, as written or, on Slack with the flag on, laid out again by
``inventory_presenter`` without a model call, followed by the audits note. The
sweep's complete findings are a different file (``INVENTORY.raw.md``) and are
never delivered from here.
"""

import contextlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import bootstrap_handoff  # beside this script in the pod
import chat_notify  # beside this script in the pod
import oobe  # beside this script in the pod
import sandbox_exec

# The two jobs oobe took over, removed once onboarding is done.
SCAN_JOB_ID = "bootstrap-inventory-scan"
DELIVERY_JOB_ID = "bootstrap-inventory-delivery"
# The job that runs this stage, and so the one the plugin links to the chat.
OOBE_JOB_ID = "oobe"

# The delivered report is renamed here rather than deleted. It is the only copy
# of a sweep that can take many minutes over a whole fleet, and a chat message
# is easy to lose; keeping it means a re-send is a `cat`, not a re-scan.
DELIVERED_REPORT_NAME = "INVENTORY.delivered.md"
REPORT_NAME = "INVENTORY.md"

# The items the report lists, written by ``inventory_findings.py select``
# (its DEFAULT_SHOWN_PATH and the keys below; test_bootstrap_onboarding_scripts.py
# holds the copies equal), and set aside beside the delivered report once read.
SHOWN_NAME = "INVENTORY.shown.json"
DELIVERED_SHOWN_NAME = "INVENTORY.shown.delivered.json"
SHOWN_ITEMS = "items"
SHOWN_CLASS = "class"
SHOWN_IDS = "ids"
# A few ids per item; far above any real file.
SHOWN_MAX_BYTES = 64 * 1024
# The sweep's extracted findings and the worker's scores, which this run
# registers in the queue: inventory_findings.py's DEFAULT_ITEMS_PATH and
# DEFAULT_SCORES_PATH (test_bootstrap_onboarding_scripts.py holds them equal).
ITEMS_NAME = "INVENTORY.items.json"
SCORES_NAME = "INVENTORY.scores.json"
ITEMS_KEY = "items"
SCORES_KEY = "scores"
# A few KB per finding; far above any fleet's sweep.
BATCH_MAX_BYTES = 4 * 1024 * 1024
# The outcome the queue gives a row the user dismissed (findings_queue._register_one).
SUPPRESSED = "suppressed"

# The findings queue on this pod's loopback, as findings_nudge.py reaches it.
# The cron child inherits SESSION_KV_API_KEY (deploy/docker/plugins/verify_chat_relay.py).
FINDINGS_ENDPOINT_ENV = "SESSION_KV_ENDPOINT"
SESSION_KV_AUTH_ENV = "SESSION_KV_API_KEY"
DEFAULT_FINDINGS_ENDPOINT = "http://127.0.0.1:8699"
SURFACED_PATH = "/v1/findings/{id}/surfaced"
JSON_HEADERS = {"Content-Type": "application/json"}
AUTH_HEADER = "Authorization"
BEARER_PREFIX = "Bearer "
# The paced publisher this report marks as (findings_queue.PACED_PUBLISHERS).
PUBLISHER = "first_report"
# One run id per delivery, so the rows of one item are one addition.
RUN_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
# Short: the report waits on these. A queue that does not answer one mark is
# not asked for the rest.
MARK_TIMEOUT_SECONDS = 5

# The sandbox's data volume, whatever HERMES_HOME says on this side: the same
# path by construction (deploy/sandbox/Dockerfile), and a separate constant
# because it names a directory on the far side of the connection.
SANDBOX_HOME = "/opt/data"

# Far above the report the prioritization stage writes, which is sized for one
# chat message. It bounds what one tick moves over ssh; a report past it is
# refused rather than cut, since a truncated report would be delivered as whole.
REPORT_MAX_BYTES = 256 * 1024
# The conversation path hands the report to `a2a notify` as one argv string,
# which Linux caps at 128 KiB (MAX_ARG_STRLEN); this leaves room for the audits
# note and the Slack reshape. A larger report is refused there, not cut.
CONVERSATION_REPORT_MAX_BYTES = 96 * 1024
SANDBOX_TIMEOUT_SECONDS = 30

# The longest one conversation post can run (_notify_conversation): every try of
# `a2a notify` to its kill bound, and the waits between tries.
CONVERSATION_POST_MAX_SECONDS = (
    (len(chat_notify.NOTIFY_ROUTE_RETRY_DELAYS_SECONDS) + 1) * chat_notify.NOTIFY_SUBPROCESS_TIMEOUT_SECONDS
    + sum(chat_notify.NOTIFY_ROUTE_RETRY_DELAYS_SECONDS)
)
CLAIM_MARGIN_SECONDS = 60
# How old ``.bootstrap_completed`` must be before a run removes the jobs. A
# younger marker may belong to a racing run that is still delivering (see the
# claim in the module docstring), and removing the delivery job under it would discard its
# report. Above that run's post-claim work: on the origin path one read and two
# archives over ssh, marks that stop at the first MARK_TIMEOUT_SECONDS timeout,
# and a stdout write; on the conversation path the report's post and the note's.
RETIRE_AFTER_SECONDS = 2 * CONVERSATION_POST_MAX_SECONDS + CLAIM_MARGIN_SECONDS
# A conversation-path claim this old with no outcome recorded belongs to a run
# that never finished its post (a pod restart between the claim and the answer).
STALE_CLAIM_SECONDS = CONVERSATION_POST_MAX_SECONDS + CLAIM_MARGIN_SECONDS

# By absolute path, so no PATH entry picks the binary. A function defined under
# this name in a ~/.bashrc the model wrote, which older sandbox images allow,
# still shadows it, since bash allows a slash in a function name; that loses only
# the rename, as the claim comes first.
REMOTE_MV = "/bin/mv"

# The only surface the reshaped report is written for; every other one gets it as written.
PRESENTED_PLATFORM = "slack"

# The note on the first-run audits (_audits_note). The scan leaves their workload checks to them.
AUDITS_SKIPPED_NOTE = "The first-run audits did not run: {reason}."
AUDITS_NO_REASON = "no reason was recorded"
# The reason an older oobe stage recorded when it skipped for no repository. A marker an install
# kept through the upgrade still carries it, and on such an install the scan ran those checks,
# unless a repository registered when the sweep was filed was removed before the stage read the list.
LEGACY_NO_REPOSITORY_REASON = "no GitHub repository in managed_repos for the audits to publish to"
AUDITS_COVERED_NOTE = (
    "This scan checked probes, requests and limits, autoscaling, NetworkPolicy and pod security in "
    "their place, so this report covers them."
)
# Every other skip leaves the audits to their schedules, and the scan did not run their checks.
AUDITS_NOT_COVERED_NOTE = (
    "This scan did not check probes, requests and limits, autoscaling, NetworkPolicy or pod security "
    "either, so this report does not cover them."
)
# A skip with the hand-off's no-coverage report (bootstrap_handoff.NO_RANKING).
AUDITS_UNCHECKED_SCHEDULED_NOTE = (
    "No cluster was audited either, so this report covers none of probes, requests and limits, "
    "autoscaling, NetworkPolicy or pod security; those audits check them on their schedules."
)
# Also the note before the marker exists: the stage writes it on its first tick after the ranking
# card finishes, which a user already in the chat can beat to this run.
AUDITS_RUNNING_NOTE = (
    "Workload checks (probes, requests and limits, autoscaling, NetworkPolicy, pod security) come "
    "from the cost, security, reliability and capacity audits, which run one after another once this "
    "scan is done. Each posts a summary to the home channel when it finds something new."
)
AUDITS_RAN_NOTE = (
    "Workload checks (probes, requests and limits, autoscaling, NetworkPolicy, pod security) come "
    "from the first-run audits, which have started: {audits}. Each posts a summary to the home channel "
    "when it finds something new."
)
AUDITS_NONE_RAN_NOTE = (
    "None of the first-run audits ran, so nothing has checked probes, requests and limits, "
    "autoscaling, NetworkPolicy or pod security yet."
)
AUDITS_NOT_RUN_NOTE = "Not run: {audits}."
# oobe gives up on an audit it could not start; the audit still runs on its own schedule.
AUDITS_GAVE_UP_REASON = "it did not start, and runs on its own schedule"
# Every audit the chain started has completed its run, so "have started" would be stale.
AUDITS_FINISHED_NOTE = (
    "Workload checks (probes, requests and limits, autoscaling, NetworkPolicy, pod security) come "
    "from the first-run audits, which have run: {audits}."
)
# Said of every failed delivery, whatever refused it: usually no home channel yet, which an audit
# that ends before anyone opens the chat always meets, and the scheduler does not post it again
# (_undelivered_audits).
AUDITS_UNDELIVERED_NOTE = (
    "These finished, but their summaries were not posted to the chat: {audits}. "
    "Ask me for any of these reports."
)

# The scheduler's record of where a run's final message went (Hermes cron/executions.db).
DELIVERY_OUTCOME_COLUMN = "delivery_outcome"
OUTCOME_LOST = "lost"
OUTCOME_SILENT = "silent"
OUTCOME_TRIED = "tried"
DELIVERY_FAILED = "failed"
# Every value Hermes records there, and None for a run with none recorded. A value not in the table reads as
# lost: naming a summary that did post costs the user one question, dropping one that did not loses it.
DELIVERY_OUTCOME_CLASS: dict[str | None, str] = {
    DELIVERY_FAILED: OUTCOME_LOST,
    # Posted nothing on purpose.
    "suppressed": OUTCOME_SILENT,
    "suppressed_acked": OUTCOME_SILENT,
    None: OUTCOME_SILENT,
    "delivered": OUTCOME_TRIED,
    "queued": OUTCOME_TRIED,
    # Hermes records it only for `deliver: origin` with no origin to resolve; the audits use `deliver: chat`.
    "not_configured": OUTCOME_TRIED,
}

# The sentence of the two notes above that names the home channel, and what replaces it where the
# gateway has none to post to (no A2A_NOTIFY_PLATFORM on the conversation path).
AUDITS_HOME_SENTENCE = "Each posts a summary to the home channel when it finds something new."
AUDITS_NO_HOME_SENTENCE = (
    "Each one's summary goes to the home channel, and this install has none yet, so those summaries "
    "are not posted to chat."
)

# Under spec.mode next the plugin records the conversation the report goes to (its
# CONVERSATION_FILE) and touches CONVERSATION_ALIGNED_MARKER in place of .user_aligned. Either one
# present puts the run on the conversation path, which never prints the report: the scheduler
# cannot post to a gateway conversation.
CONVERSATION_FILE = "onboarding_conversation.json"
CONVERSATION_ALIGNED_MARKER = ".user_aligned_conversation"
CONVERSATION_KEYS = ("platform", "conversation", "context_id")
# This stage's record of the conversation path: the attempts that released the claim and when the
# last one did, and why the report is not out yet. Stderr from a run that exits 0 is dropped, so
# this is where that shows.
CONVERSATION_ATTEMPTS_FILE = ".bootstrap_conversation_attempts.json"
ATTEMPTS_COUNT = "attempts"
ATTEMPTS_REASON = "last_reason"
ATTEMPTS_AT = "last_at"
# The waits before the second and each later post after one that posted nothing; past the last,
# the report waits. Spread over hours, because the gateway's "not a live conversation" also covers
# a session record it could not read in time.
CONVERSATION_RETRY_WAITS_SECONDS = (5 * 60, 30 * 60, 2 * 60 * 60, 6 * 60 * 60)
CONVERSATION_MAX_ATTEMPTS = len(CONVERSATION_RETRY_WAITS_SECONDS) + 1
# The card's update key. Every card attempt sends it, so an attempt after one
# that landed edits that card rather than adding one, and the card path retries
# whatever did not answer as posted. Text goes without a key, because the
# gateway cuts a keyed text post to one message, so the text path retries only
# what most likely posted nothing.
CONVERSATION_REPORT_UPDATE = "onboarding-report"
# The gateway's answer when it could not edit the keyed card it holds
# (a2a/gateway/notify.go): the card is in the chat, so the report is out. The
# gateway and agent images roll apart, so this text is pinned by a test.
NOTIFY_EDIT_FAILED_PREFIX = "edit failed:"
# Written right after the report's post answers, with the outcome: the
# conversation path's "delivered", which the stale-claim rule and retirement wait on.
CONVERSATION_DONE_FILE = ".bootstrap_conversation_done"
# A conversation post's outcome: posted; posted nothing as far as can be told
# (exit 1, a route absent through the retries, a CLI that could not start); or
# may have posted (exit 3, a killed CLI, any other exit).
POSTED = "posted"
NOT_POSTED = "not posted"
MAYBE_POSTED = "maybe posted"
NOTIFY_EXIT_FAILED = 1

# The delivery job's ``origin`` and the keys the plugin writes into it.
ORIGIN_KEY = "origin"
PLATFORM_KEY = "platform"
CHAT_ID_KEY = "chat_id"
THREAD_ID_KEY = "thread_id"


def _data_dir() -> Path:
    return Path(os.environ.get("HERMES_HOME", "/opt/data"))


def _awaiting_delivery(data_dir: Path) -> bool:
    """True when a human is present; main() has already returned on a delivered report.

    The markers live on this pod, so this costs a stat or two; it runs before the
    report read, which may cross into the sandbox.
    """
    return (data_dir / ".user_aligned").exists() or (data_dir / CONVERSATION_ALIGNED_MARKER).exists()


def _bound_conversation(data_dir: Path) -> dict | None:
    """The conversation the plugin bound the report to, or None when the run is on the origin path.

    A conversation file or marker that is there but unusable is a dict with empty values, which
    ``_conversation_ready`` refuses: it must never fall back to printing the report.
    """
    path = data_dir / CONVERSATION_FILE
    if not path.exists() and not (data_dir / CONVERSATION_ALIGNED_MARKER).exists():
        return None
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        record = {}
    if not isinstance(record, dict):
        record = {}
    return {key: str(record.get(key) or "").strip() for key in CONVERSATION_KEYS}


def _read_attempts(data_dir: Path) -> dict:
    try:
        record = json.loads((data_dir / CONVERSATION_ATTEMPTS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return record if isinstance(record, dict) else {}


def _attempt_count(record: dict) -> int:
    count = record.get(ATTEMPTS_COUNT)
    return count if isinstance(count, int) and not isinstance(count, bool) and count > 0 else 0


def _record_attempt(data_dir: Path, reason: str, counted: bool) -> None:
    """Record why the report is not out; ``counted`` adds one post that released the claim."""
    sys.stderr.write(f"bootstrap_delivery: {reason}\n")
    record = _read_attempts(data_dir)
    count = _attempt_count(record) + (1 if counted else 0)
    if not counted and record.get(ATTEMPTS_REASON) == reason:
        return  # the same block as the last tick's: no write each minute
    record.update({ATTEMPTS_COUNT: count, ATTEMPTS_REASON: reason})
    if counted:
        # Only a counted attempt starts the wait before the next one.
        record[ATTEMPTS_AT] = time.time()
    if counted and count >= CONVERSATION_MAX_ATTEMPTS:
        _stage_note(data_dir, f"the report was not posted after {count} attempts and now waits: {reason}")
    target = data_dir / CONVERSATION_ATTEMPTS_FILE
    tmp = target.with_name(target.name + ".tmp")
    try:
        tmp.write_text(json.dumps(record, sort_keys=True) + "\n", encoding="utf-8")
        tmp.replace(target)
    except OSError as e:
        sys.stderr.write(f"bootstrap_delivery: could not record the attempt: {e}\n")


def _outcome_recorded(data_dir: Path) -> bool:
    return (data_dir / CONVERSATION_DONE_FILE).exists()


def delivery_settled(data_dir: Path) -> bool:
    """Whether a taken claim may retire the jobs: always on the origin path, and
    on the conversation path only once the report's post recorded an outcome."""
    return _bound_conversation(data_dir) is None or _outcome_recorded(data_dir)


def _release_claim(data_dir: Path) -> None:
    try:
        (data_dir / ".bootstrap_completed").unlink()
    except FileNotFoundError:
        pass
    except OSError as e:
        sys.stderr.write(f"bootstrap_delivery: could not release the claim: {e}\n")


def _release_stale_claim(data_dir: Path, claimed_at: float) -> None:
    """Release a conversation-path claim whose run never recorded an outcome.

    Only past ``STALE_CLAIM_SECONDS``, so a run still posting keeps it. It
    counts as an attempt unless one was already recorded after the claim.
    """
    if time.time() - claimed_at < STALE_CLAIM_SECONDS:
        return
    try:
        last = float(_read_attempts(data_dir).get(ATTEMPTS_AT))
    except (TypeError, ValueError):
        last = 0.0
    if last <= claimed_at:
        _record_attempt(data_dir, "a run claimed the report and recorded no outcome; trying again", counted=True)
    _release_claim(data_dir)


def _conversation_ready(data_dir: Path, conversation: dict) -> bool:
    """Whether this run may claim the report for the conversation path.

    Checked before the claim, in this process, because the cron child's env is
    scrubbed apart from the gateway's: a child that cannot run ``a2a notify``
    claims nothing. Past ``CONVERSATION_MAX_ATTEMPTS`` posts that posted
    nothing, and inside the wait after each, it is False too.
    """
    platform = conversation["platform"]
    if not all(conversation.values()):
        reason = f"{CONVERSATION_FILE} is missing or incomplete; the report waits"
    elif chat_notify.gateway_platform() != platform:
        reason = (
            f"the gateway does not serve {platform} conversations to this run "
            f"({chat_notify.NOTIFY_CONVERSATIONS_ENV}={chat_notify.gateway_platform()!r}); the report waits"
        )
    else:
        reason = chat_notify.bus_unreachable_reason()
        if reason:
            reason = f"a2a notify cannot reach the bus from this run: {reason}; the report waits"
    if reason:
        _record_attempt(data_dir, reason, counted=False)
        return False
    record = _read_attempts(data_dir)
    count = _attempt_count(record)
    if count >= CONVERSATION_MAX_ATTEMPTS:
        return False
    if count:
        try:
            last = float(record.get(ATTEMPTS_AT))
        except (TypeError, ValueError):
            last = 0.0
        if time.time() - last < CONVERSATION_RETRY_WAITS_SECONDS[count - 1]:
            return False
    return True


def _completed_at(data_dir: Path) -> float | None:
    """When the delivery claim was taken, or None if it has not been."""
    try:
        return (data_dir / ".bootstrap_completed").stat().st_mtime
    except FileNotFoundError:
        return None


def _read_report(data_dir: Path, in_sandbox: bool) -> bytes | None:
    """Up to ``REPORT_MAX_BYTES + 1`` bytes of the report, or None if there is none.

    Raises ``sandbox_exec.SandboxUnavailable`` or ``subprocess.TimeoutExpired``
    when the sandbox did not answer, ``sandbox_exec.SandboxReadFailed`` when the
    read ran and did not return the report, and ``OSError`` when the local file
    could not be read.
    """
    if in_sandbox:
        return sandbox_exec.read_bytes(
            f"{SANDBOX_HOME}/{REPORT_NAME}",
            max_bytes=REPORT_MAX_BYTES + 1,
            timeout=SANDBOX_TIMEOUT_SECONDS,
        )
    try:
        with open(data_dir / REPORT_NAME, "rb") as handle:
            return handle.read(REPORT_MAX_BYTES + 1)
    except FileNotFoundError:
        return None


def _claim_delivery(data_dir: Path) -> bool:
    """Atomically claim the right to deliver the report. True if we won.

    ``O_CREAT | O_EXCL`` is a single filesystem operation, so this is the point
    at which "may I deliver?" and "I am delivering" become indivisible. Called
    before the first byte of the report is written to stdout.

    A False return means another run already claimed it: the caller must emit
    nothing at all.
    """
    completed = data_dir / ".bootstrap_completed"
    try:
        fd = os.open(str(completed), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        return False  # another run got there first
    except OSError as e:
        # Cannot claim -> cannot safely deliver. Staying silent costs a retry
        # next tick; delivering unclaimed risks sending the report twice.
        sys.stderr.write(f"bootstrap_delivery: could not claim delivery: {e}\n")
        return False
    os.close(fd)
    return True


def _archive(data_dir: Path, in_sandbox: bool, name: str = REPORT_NAME, archived: str = DELIVERED_REPORT_NAME) -> None:
    """Rename ``name`` to ``archived`` where it was read."""
    if not in_sandbox:
        try:
            source = data_dir / name
            if source.exists():
                source.replace(data_dir / archived)
        except OSError as e:
            sys.stderr.write(f"bootstrap_delivery: could not archive {name}: {e}\n")
        return
    # As the terminal's login: the sandbox's /opt/data is that account's and
    # mode 755, so the default login cannot rename inside it.
    try:
        moved = sandbox_exec.run(
            [REMOTE_MV, "-f", "--", f"{SANDBOX_HOME}/{name}", f"{SANDBOX_HOME}/{archived}"],
            principal=sandbox_exec.TERMINAL_PRINCIPAL,
            timeout=SANDBOX_TIMEOUT_SECONDS,
        )
    except Exception as e:
        sys.stderr.write(f"bootstrap_delivery: could not archive {name} in the sandbox: {e}\n")
        return
    if moved.returncode != 0:
        sys.stderr.write(
            f"bootstrap_delivery: could not archive {name} in the sandbox: "
            f"{(moved.stderr or '').strip()}\n"
        )


def _read_beside(data_dir: Path, in_sandbox: bool, name: str, max_bytes: int) -> bytes | None:
    """Up to ``max_bytes + 1`` bytes of ``name`` where the report was read, or None if there is none."""
    if in_sandbox:
        return sandbox_exec.read_bytes(
            f"{SANDBOX_HOME}/{name}", max_bytes=max_bytes + 1, timeout=SANDBOX_TIMEOUT_SECONDS
        )
    try:
        with open(data_dir / name, "rb") as handle:
            return handle.read(max_bytes + 1)
    except FileNotFoundError:
        return None


def _read_shown(data_dir: Path, in_sandbox: bool) -> bytes | None:
    """``SHOWN_NAME``'s bytes where the report was read, or None if there is none."""
    return _read_beside(data_dir, in_sandbox, SHOWN_NAME, SHOWN_MAX_BYTES)


def _read_json_beside(data_dir: Path, in_sandbox: bool, name: str) -> dict | None:
    """``name`` parsed where the report was read, or None if there is none. Raises on anything unusable."""
    raw = _read_beside(data_dir, in_sandbox, name, BATCH_MAX_BYTES)
    if raw is None:
        return None
    if len(raw) > BATCH_MAX_BYTES:
        raise ValueError(f"{name} is larger than {BATCH_MAX_BYTES} bytes")
    parsed = json.loads(raw.decode("utf-8"))
    if not isinstance(parsed, dict):
        raise ValueError(f"{name} is not a JSON object")
    return parsed


def _findings_endpoint() -> str:
    return (os.environ.get(FINDINGS_ENDPOINT_ENV) or DEFAULT_FINDINGS_ENDPOINT).rstrip("/")


def _no_ranking(data_dir: Path) -> bool | None:
    """Whether the hand-off filed no prioritization card (``bootstrap_handoff.NO_RANKING``).

    The report is then the hand-off's no-coverage report: no cluster was audited. Read once, for the
    findings registration and the audits note. ``None`` when the marker cannot be read: the
    registration then registers nothing, as it did when it read the marker itself and failed.
    """
    try:
        import bootstrap_handoff  # beside this script in the pod

        handed_off = bootstrap_handoff._read_marker(data_dir / bootstrap_handoff.HANDOFF_MARKER)
        return handed_off.get("task_id") == bootstrap_handoff.NO_RANKING
    except Exception as e:  # noqa: BLE001 - delivery goes on without registering
        sys.stderr.write(f"bootstrap_delivery: could not read the hand-off marker: {e}\n")
        return None


def _register_findings(data_dir: Path, in_sandbox: bool, no_ranking: bool | None) -> set[str]:
    """Register every finding the sweep extracted in the findings queue, as
    ``inventory_findings.py register`` does, from this pod.

    The worker runs ``register`` through its terminal. With the shell sandbox
    on, that terminal cannot reach the queue on this pod's loopback, so
    ``register`` exits 13 and nothing reaches the queue; the findings the report
    leaves out would then never reach the findings nudge, which reads only the
    queue, and the report's marks would find no rows. This run can reach it.
    Registering is an upsert, so a batch the worker did register is written
    again unchanged.

    Nothing is registered when the hand-off filed no prioritization card
    (``no_ranking``, from ``_no_ranking``): no cluster was audited, nothing
    rewrote the items and scores, and any left beside the report are an
    earlier sweep's, whose complete scopes would re-open findings and mark
    current ones absent.

    Returns the ids the queue answered ``suppressed``: rows the user
    dismissed. With the shell sandbox on, ``select`` never learned them, so
    the report may list one; ``_mark_shown`` leaves them unmarked.

    Never raises. Runs before the claim, so it adds nothing to the claimed
    run's work (``RETIRE_AFTER_SECONDS``), and the nudge holds until the claim.
    """
    suppressed: set[str] = set()
    try:
        import inventory_findings  # beside this script in the pod

        if no_ranking is None:
            sys.stderr.write("bootstrap_delivery: the hand-off marker could not be read; registering nothing\n")
            return suppressed
        if no_ranking:
            sys.stderr.write(
                "bootstrap_delivery: the hand-off filed no prioritization card, so any "
                f"{ITEMS_NAME} is an earlier sweep's; registering nothing\n"
            )
            return suppressed
        extracted = _read_json_beside(data_dir, in_sandbox, ITEMS_NAME)
        if extracted is None:
            sys.stderr.write(f"bootstrap_delivery: no {ITEMS_NAME}; registering nothing\n")
            return suppressed
        items = extracted[ITEMS_KEY]
        if not items:
            return suppressed  # a clean fleet: nothing to register, and no scores file
        raw_scores = _read_json_beside(data_dir, in_sandbox, SCORES_NAME)
        if raw_scores is None:
            sys.stderr.write(f"bootstrap_delivery: no {SCORES_NAME}; registering nothing\n")
            return suppressed
        payloads = inventory_findings.build_payloads(items, raw_scores[SCORES_KEY])
        batches = inventory_findings.cluster_batches(payloads, inventory_findings.complete_clusters(raw_scores))
    except Exception as e:
        detail = "; ".join(getattr(e, "errors", None) or [str(e)])
        sys.stderr.write(f"bootstrap_delivery: could not read the sweep's findings; registering nothing: {detail}\n")
        return suppressed
    endpoint = _findings_endpoint()
    for where, batch, scope in batches:
        try:
            result = inventory_findings.post_batch(endpoint, batch, scope)
        except urllib.error.HTTPError as e:
            # This cluster only.
            sys.stderr.write(f"bootstrap_delivery: the findings queue refused {where}'s findings: {e.code}\n")
            continue
        except Exception as e:
            sys.stderr.write(
                f"bootstrap_delivery: the findings queue at {endpoint} did not answer ({e}); "
                "not registering the rest\n"
            )
            return suppressed
        outcomes = result.get("results") if isinstance(result, dict) else None
        for entry in outcomes if isinstance(outcomes, list) else []:
            if isinstance(entry, dict) and entry.get("outcome") == SUPPRESSED:
                suppressed.add(str(entry.get("id")))
    return suppressed


def _post_surfaced(endpoint: str, finding_id: str, body: dict) -> None:
    headers = dict(JSON_HEADERS)
    token = (os.environ.get(SESSION_KV_AUTH_ENV) or "").strip()
    if token:
        headers[AUTH_HEADER] = f"{BEARER_PREFIX}{token}"
    path = SURFACED_PATH.format(id=urllib.parse.quote(finding_id, safe=""))
    request = urllib.request.Request(
        f"{endpoint}{path}", data=json.dumps(body).encode("utf-8"), headers=headers, method="POST"
    )
    with urllib.request.urlopen(request, timeout=MARK_TIMEOUT_SECONDS) as response:
        response.read()


def _mark_shown(data_dir: Path, in_sandbox: bool, suppressed: set[str] = frozenset()) -> bool:
    """Mark every row of every item the report lists shown, as ``PUBLISHER``,
    except the ``suppressed`` ids registration reported: the user dismissed
    them, so they are never pending and spend no slot of the day's limit.
    The queue refuses such a mark anyway; skipping it saves the request.

    Returns whether to set the shown file aside: True unless there is none,
    so a file this run could not read is not marked by a later report.
    Never raises: the claim is taken, so nothing here may stop the report
    going out.
    """
    try:
        raw = _read_shown(data_dir, in_sandbox)
    except Exception as e:
        sys.stderr.write(f"bootstrap_delivery: could not read {SHOWN_NAME}; marking nothing: {e}\n")
        return True
    if raw is None:
        sys.stderr.write(f"bootstrap_delivery: no {SHOWN_NAME}; marking nothing\n")
        return False
    try:
        if len(raw) > SHOWN_MAX_BYTES:
            raise ValueError(f"larger than {SHOWN_MAX_BYTES} bytes")
        items = json.loads(raw.decode("utf-8"))[SHOWN_ITEMS]
        marks = [(str(item[SHOWN_CLASS]), [str(fid) for fid in item[SHOWN_IDS]]) for item in items]
    except Exception as e:
        sys.stderr.write(f"bootstrap_delivery: {SHOWN_NAME} is not readable; marking nothing: {e}\n")
        return True
    endpoint = _findings_endpoint()
    run = datetime.now(timezone.utc).strftime(RUN_FORMAT)
    for added_class, ids in marks:
        body = {"publisher": PUBLISHER, "added_class": added_class, "run": run}
        for finding_id in ids:
            if finding_id in suppressed:
                sys.stderr.write(f"bootstrap_delivery: not marking {finding_id} shown: the user dismissed it\n")
                continue
            try:
                _post_surfaced(endpoint, finding_id, body)
            except urllib.error.HTTPError as e:
                # This row only: an unregistered one is a 404, one the user
                # decided since registration a 400. Reading the
                # body can itself time out, which must not escape.
                try:
                    detail = e.read().decode("utf-8", "replace").strip()
                except Exception:
                    detail = ""
                sys.stderr.write(f"bootstrap_delivery: could not mark {finding_id} shown: {e.code} {detail}\n")
            except Exception as e:
                sys.stderr.write(
                    f"bootstrap_delivery: the findings queue at {endpoint} did not answer ({e}); "
                    "not marking the rest\n"
                )
                return True
    return True


def _cleanup(data_dir: Path, in_sandbox: bool, shown: bool = False) -> None:
    """Tidy up after the report has been posted as blocks or emitted to stdout.

    Onboarding is already marked complete by the delivery claim, so everything
    here is best-effort: a cleanup hiccup must never turn a delivered report
    into a reported failure.

    The report is renamed, not deleted — see ``DELIVERED_REPORT_NAME``. Moving
    it out of the way still matters: the sweep and prioritization SOPs, which
    run where the report is, treat a present ``INVENTORY.md`` as "already done",
    so leaving it in place would make a later, deliberate re-run of onboarding a
    no-op. ``shown`` sets the shown file aside beside it, so a later report
    whose worker never ran ``select`` is not marked with this one's items.
    """
    _archive(data_dir, in_sandbox)
    if shown:
        _archive(data_dir, in_sandbox, SHOWN_NAME, DELIVERED_SHOWN_NAME)


def _retire_jobs() -> None:
    """Remove the two disabled onboarding cron jobs, in-process.

    Only a run with nothing to deliver may call this, and ``oobe`` removes
    itself only after it. Removing a job while it runs drops the run's fire
    claim, and the scheduler then discards that run's output instead of posting
    it. So the run that delivers the report leaves every job in place, and a
    later run, which finds ``.bootstrap_completed`` and has nothing to post,
    removes them and loses nothing.
    """
    try:
        from cron.jobs import remove_job  # type: ignore import-not-found
    except Exception:
        return
    for job_id in (SCAN_JOB_ID, DELIVERY_JOB_ID):
        try:
            remove_job(job_id)
        except Exception as e:
            sys.stderr.write(f"bootstrap_delivery: could not remove {job_id}: {e}\n")


def _origin() -> dict:
    """The origin the plugin bound this job's delivery to: ``platform``,
    ``chat_id`` and ``thread_id``; empty if unknown.

    The plugin writes the origin before ``.user_aligned``, so it is set by the
    time a delivery can fire.
    """
    try:
        from cron.jobs import get_job  # type: ignore import-not-found

        # oobe's when it runs this stage; the old job's on an install still delivering through it.
        for job_id in (OOBE_JOB_ID, DELIVERY_JOB_ID):
            origin = (get_job(job_id) or {}).get(ORIGIN_KEY)
            if origin:
                return origin
        return {}
    except Exception as e:
        sys.stderr.write(f"bootstrap_delivery: could not read the delivery origin: {e}\n")
        return {}


def _origin_platform() -> str | None:
    """The platform the plugin bound this job's delivery to, or None if unknown."""
    return _origin().get(PLATFORM_KEY)


def _presented(content: str, platform: str | None = None) -> str:
    """The report as delivered: reshaped when ``KAGE_SLACK_UX`` is on and the
    job is bound to Slack (or ``platform`` is), as written otherwise.

    Both helpers ship beside this script in ``/opt/defaults/scripts``. Any
    failure to load or reshape delivers the report as written, since this runs
    after the claim and a lost report is not retried.
    """
    try:
        import slack_presenter

        if not slack_presenter.enabled() or (platform or _origin_platform()) != PRESENTED_PLATFORM:
            return content
        import inventory_presenter

        return inventory_presenter.present(content)
    except Exception as e:
        sys.stderr.write(f"bootstrap_delivery: delivering verbatim: {e}\n")
        return content


def _skip_follows_this_sweep(data_dir: Path, state: dict) -> bool:
    """Whether oobe recorded its skip after the sweep the report comes from was filed.

    A sweep filed after the skip (onboarding repeated with the stage's marker kept) ran on a later
    build, whose scan leaves the workload checks to the audits whatever the old skip said.
    """
    filed = bootstrap_handoff.read_scan_marker(data_dir / oobe.SCAN_FILED_MARKER)
    try:
        skipped_at = float(state.get(oobe.STATE_AT))
    except (TypeError, ValueError):
        return False
    return filed is not None and skipped_at >= filed[1]


def _audits_note(data_dir: Path, no_coverage: bool, home: bool = True) -> str:
    """The note on the first-run audits, saying where their summaries go only when ``home``.

    With no home channel and a list of unposted summaries already in the note,
    the home sentence is dropped rather than replaced, so "not posted" is said once.
    """
    note = _audits_state_note(data_dir, no_coverage)
    if home:
        return note
    replacement = "" if AUDITS_UNDELIVERED_NOTE.split("{audits}")[0] in note else " " + AUDITS_NO_HOME_SENTENCE
    return note.replace(" " + AUDITS_HOME_SENTENCE, replacement)


def _audits_state_note(data_dir: Path, no_coverage: bool) -> str:
    """The note on the first-run audits the delivery closes with, from oobe's marker.

    Before the marker exists the stage has not decided, and the note is the chain's. ``no_coverage``
    is a report no cluster's audit reached (``_no_ranking``), which covers nothing.
    """
    state = oobe.read_state(data_dir)
    if state.get(oobe.STATE_SKIPPED):
        reason = str(state.get(oobe.STATE_REASON) or AUDITS_NO_REASON)
        if no_coverage:
            coverage = AUDITS_UNCHECKED_SCHEDULED_NOTE
        elif reason == LEGACY_NO_REPOSITORY_REASON and _skip_follows_this_sweep(data_dir, state):
            coverage = AUDITS_COVERED_NOTE
        else:
            coverage = AUDITS_NOT_COVERED_NOTE
        return f"{AUDITS_SKIPPED_NOTE.format(reason=reason)} {coverage}"
    fired = [a for a in oobe.FIRST_RUN_AUDITS if a in _list(state.get(oobe.STATE_FIRED))]
    held = state.get(oobe.STATE_HELD) if isinstance(state.get(oobe.STATE_HELD), dict) else {}
    gave_up = _list(state.get(oobe.STATE_GAVE_UP))
    not_run = [f"`{a}` ({held[a]})" for a in oobe.FIRST_RUN_AUDITS if a in held]
    not_run += [f"`{a}` ({AUDITS_GAVE_UP_REASON})" for a in oobe.FIRST_RUN_AUDITS if a in gave_up]
    runs = _completed_runs(data_dir, fired)
    marks = state.get(oobe.STATE_MARKS) if isinstance(state.get(oobe.STATE_MARKS), dict) else {}
    # A mark that is not a number reads as no mark: this runs after the claim, and a raise here
    # would lose the report.
    since = {a: m if isinstance(m, (int, float)) else 0 for a, m in marks.items()}
    # Judged by when the run ended: a scheduled run already going when the stage marked the audit
    # is the stage's answer, though it was claimed before the mark.
    finished = [
        a for a in fired if any((ended or claimed) >= since.get(a, 0) for claimed, ended, _ in runs.get(a, ()))
    ]
    if not state.get(oobe.STATE_DONE):
        note = AUDITS_RUNNING_NOTE
    elif fired and finished == fired:
        note = AUDITS_FINISHED_NOTE.format(audits=", ".join(f"`{a}`" for a in fired))
    elif fired:
        note = AUDITS_RAN_NOTE.format(audits=", ".join(f"`{a}`" for a in fired))
    else:
        note = AUDITS_NONE_RAN_NOTE
    if not_run:
        note = f"{note} {AUDITS_NOT_RUN_NOTE.format(audits=', '.join(not_run))}"
    undelivered = _undelivered_audits(finished, runs)
    return f"{note} {AUDITS_UNDELIVERED_NOTE.format(audits=', '.join(undelivered))}" if undelivered else note


def _completed_runs(data_dir: Path, jobs: list[str]) -> dict[str, list[tuple[float, float | None, str | None]]]:
    """``job -> [(claimed_at, finished_at, delivery_outcome), ...]`` for the completed runs of ``jobs``, oldest first.

    Empty when the Platform Agent's cron store cannot be read: the note then says what it said
    before, rather than holding the report back, and the stage log says why (Hermes drops this
    run's stderr when it exits 0).
    """
    ledger = data_dir / oobe.PROFILES_DIR / oobe.PLATFORM_PROFILE / oobe.CRON_DIR / oobe.EXECUTIONS_DB
    if not jobs:
        return {}
    if not ledger.is_file():
        _stage_note(data_dir, f"no {ledger}, so the audits note names no unposted summary")
        return {}
    placeholders = ",".join("?" * len(jobs))
    try:
        conn = sqlite3.connect(
            f"file:{ledger}?mode=ro", uri=True, timeout=bootstrap_handoff.SQLITE_BUSY_TIMEOUT_SECONDS
        )
        try:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(executions)")}
            if DELIVERY_OUTCOME_COLUMN not in columns:
                _stage_note(
                    data_dir,
                    f"{ledger} has no {DELIVERY_OUTCOME_COLUMN} column, so the audits note names no unposted summary",
                )
            outcome = DELIVERY_OUTCOME_COLUMN if DELIVERY_OUTCOME_COLUMN in columns else "NULL"
            finished = oobe.FINISHED_AT_COLUMN if oobe.FINISHED_AT_COLUMN in columns else "NULL"
            rows = conn.execute(
                f"SELECT job_id, claimed_at, {finished}, {outcome} FROM executions "
                f"WHERE job_id IN ({placeholders}) AND status = ? ORDER BY claimed_at",
                (*jobs, oobe.COMPLETED_STATUS),
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as e:
        _stage_note(data_dir, f"cannot read {ledger}, so the audits note names no unposted summary: {e}")
        return {}
    runs: dict[str, list[tuple[float, float | None, str | None]]] = {}
    for job_id, claimed, finished_at, delivery in rows:
        try:
            claimed_at = datetime.fromisoformat(claimed).timestamp()
        except (TypeError, ValueError):
            continue
        try:
            ended = datetime.fromisoformat(finished_at).timestamp()
        except (TypeError, ValueError):
            ended = None
        runs.setdefault(job_id, []).append((claimed_at, ended, delivery))
    return runs


def _undelivered_audits(audits: list[str], runs: dict[str, list[tuple[float, float | None, str | None]]]) -> list[str]:
    """The audits whose latest run that tried to post its summary failed, or recorded an unknown outcome.

    A run that ends before anyone has opened the chat finds no home channel, the relay refuses
    it, and nothing posts it later; this delivery is the first message that user reads. Any other
    failed delivery is listed too, since its summary is just as missing. A later run that posted
    nothing on purpose (a silent run: suppressed, or no outcome recorded) does not stand in for
    the lost one.
    """
    out = []
    for audit in audits:
        classes = [DELIVERY_OUTCOME_CLASS.get(run[2], OUTCOME_LOST) for run in runs.get(audit, ())]
        tried = [c for c in classes if c != OUTCOME_SILENT]
        if tried and tried[-1] == OUTCOME_LOST:
            out.append(f"`{audit}`")
    return out


def _stage_note(data_dir: Path, message: str) -> None:
    """One line in the oobe stage log, which an operator reads; best effort."""
    try:
        with oobe._open_stage_log(data_dir) as log:
            log.write(f"bootstrap_delivery: {message}\n")
    except OSError as e:
        sys.stderr.write(f"bootstrap_delivery: {message} (and the stage log could not be written: {e})\n")


def _list(value) -> list:
    return value if isinstance(value, list) else []


def _with_note(report: str, note: str) -> str:
    return report.rstrip("\n") + "\n\n" + note + "\n"


def _posted_as_blocks(content: str) -> bool:
    """Whether the report was posted to Slack as Block Kit; False posts nothing.

    False, and the caller prints the text, unless the flag is on, the origin is
    Slack with a chat id, the relay is configured and the report parses. Any
    failure is False too, including one that may have posted (a timeout once
    the request was sent): this report is sent once per install, and a second
    copy of it is a smaller loss than none.
    """
    try:
        import slack_presenter

        if not slack_presenter.enabled():
            return False
        origin = _origin()
        channel = str(origin.get(CHAT_ID_KEY) or "")
        if origin.get(PLATFORM_KEY) != PRESENTED_PLATFORM or not channel:
            return False
        import chat_notify

        if chat_notify.routes(PRESENTED_PLATFORM):
            return _posted_through_gateway(content, channel, str(origin.get(THREAD_ID_KEY) or ""))
        import inventory_presenter
        import slack_blocks_post

        if not slack_blocks_post.configured():
            return False
        built = inventory_presenter.blocks(content)
        if built is None:
            return False
        blocks, text = built
        try:
            slack_blocks_post.post(channel, text, blocks, str(origin.get(THREAD_ID_KEY) or ""))
        except slack_blocks_post.Refused as e:
            sys.stderr.write(f"bootstrap_delivery: Slack refused the report blocks ({e})\n")
            return False
        return True
    except Exception as e:
        sys.stderr.write(f"bootstrap_delivery: posting the report as text: {e}\n")
    return False


def _posted_through_gateway(content: str, channel: str, thread: str) -> bool:
    """Under ``spec.mode: next``, whether the card posted as a chat message through the gateway.

    The gateway holds Slack there and the broker's relay is gone, so the card
    goes as ``a2a notify --chat-file`` (``inventory_presenter.chat``) into the
    origin's thread, which the gateway posts only when it is a thread of its
    home channel. False, and the caller prints the text, with no thread to
    reply on (a bare post would land in the home channel, wherever the origin
    is), a report that does not parse, or any failure, including one that may
    have posted, for the reason ``_posted_as_blocks`` gives.
    """
    import chat_notify
    import inventory_presenter

    built = inventory_presenter.chat(content) if thread else None
    if built is None:
        return False
    chat, text = built
    path = ""
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            json.dump(chat, handle)
            path = handle.name
        res = subprocess.run(
            chat_notify.chat_command(
                PRESENTED_PLATFORM, f"{channel}/{thread}", text, path, chat_notify.NOTIFY_WAIT_SECONDS
            ),
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=chat_notify.NOTIFY_SUBPROCESS_TIMEOUT_SECONDS,
            check=False,
        )
    finally:
        if path:
            with contextlib.suppress(OSError):
                os.unlink(path)
    if res.returncode != 0:
        sys.stderr.write(f"bootstrap_delivery: the gateway did not post the report card: {res.stderr.strip()}\n")
        return False
    return True


def _gateway_error(stdout: str) -> str:
    """The gateway's error from ``a2a notify``'s answer when it posted nothing, else ""."""
    for line in reversed((stdout or "").strip().splitlines()):
        try:
            answer = json.loads(line)
        except ValueError:
            continue
        if isinstance(answer, dict) and answer.get("error") and not answer.get("message_id"):
            return str(answer["error"])
        return ""
    return ""


def _notify_conversation(conversation: dict, text: str, chat: dict | None = None, update: str = "") -> tuple[str, str]:
    """Post ``text`` (or ``chat``, with ``text`` its fallback) into the conversation.

    Returns ``POSTED`` (or the gateway's ``edit failed:``, whose card is in the
    chat), ``NOT_POSTED`` or ``MAYBE_POSTED`` (see their constants), and a
    reason for the last two.
    """
    path = ""
    try:
        if chat is not None:
            with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
                json.dump(chat, handle)
                path = handle.name
        argv = chat_notify.conversation_command(
            conversation["platform"], conversation["conversation"], conversation["context_id"], text,
            path, update, chat_notify.NOTIFY_WAIT_SECONDS,
        )
        for delay in (*chat_notify.NOTIFY_ROUTE_RETRY_DELAYS_SECONDS, None):
            try:
                res = subprocess.run(
                    argv, capture_output=True, text=True, stdin=subprocess.DEVNULL,
                    timeout=chat_notify.NOTIFY_SUBPROCESS_TIMEOUT_SECONDS, check=False,
                )
            except subprocess.TimeoutExpired:
                return MAYBE_POSTED, "a2a notify did not finish in time; the post may have landed"
            except OSError as e:
                return NOT_POSTED, f"could not run a2a notify: {e}"
            if res.returncode == 0:
                return POSTED, ""
            if not chat_notify.route_unavailable(res.returncode):
                break
            if delay is None:
                return NOT_POSTED, f"the gateway's notify route was not there: {(res.stderr or '').strip()}"
            time.sleep(delay)
    except OSError as e:
        return NOT_POSTED, f"could not write the report card: {e}"
    finally:
        if path:
            with contextlib.suppress(OSError):
                os.unlink(path)
    error = _gateway_error(res.stdout)
    detail = error or (res.stderr or "").strip()
    if error.startswith(NOTIFY_EDIT_FAILED_PREFIX):
        return POSTED, ""
    if res.returncode == NOTIFY_EXIT_FAILED:
        return NOT_POSTED, f"a2a notify failed: {detail}"
    return MAYBE_POSTED, f"a2a notify exited {res.returncode}; the post may have landed: {detail}"


def _post_to_conversation(data_dir: Path, conversation: dict, content: str, note: str,
                          mark_shown) -> tuple[str, str, bool]:
    """Post the report and the audits note: the card then the note, or one text message.

    The card goes only when ``KAGE_SLACK_UX`` is on, the conversation is Slack's
    and the report parses. On the card path anything short of posted is
    returned as ``NOT_POSTED``, since the next attempt's key edits a card that
    landed. Once the claim is kept, the outcome is recorded, then the
    findings are marked shown (``mark_shown``, whose result is returned), both
    before the card path's note goes, since the outcome ends the findings
    nudge's hold. A note that fails is logged: the report is out.
    """
    platform = conversation["platform"]
    card = None
    try:
        import slack_presenter

        if platform == PRESENTED_PLATFORM and slack_presenter.enabled():
            import inventory_presenter

            card = inventory_presenter.chat(content)
    except Exception as e:
        sys.stderr.write(f"bootstrap_delivery: posting the report as text: {e}\n")
    if card is None:
        outcome, reason = _notify_conversation(conversation, _with_note(_presented(content, platform), note))
        if outcome == NOT_POSTED:
            return outcome, reason, False
        _record_outcome(data_dir, outcome)
        return outcome, reason, mark_shown()
    chat, text = card
    outcome, reason = _notify_conversation(conversation, text, chat, CONVERSATION_REPORT_UPDATE)
    if outcome != POSTED:
        return NOT_POSTED, reason, False
    _record_outcome(data_dir, outcome)
    shown = mark_shown()
    note_outcome, note_reason = _notify_conversation(conversation, note)
    if note_outcome != POSTED:
        _stage_note(data_dir, f"the report posted but its audits note did not: {note_reason}")
    return outcome, reason, shown


def _record_outcome(data_dir: Path, outcome: str) -> None:
    try:
        (data_dir / CONVERSATION_DONE_FILE).write_text(outcome + "\n", encoding="utf-8")
    except OSError as e:
        sys.stderr.write(f"bootstrap_delivery: could not record the outcome: {e}\n")


def _deliver_to_conversation(data_dir: Path, in_sandbox: bool, conversation: dict, content: str,
                             no_ranking: bool | None, suppressed: set[str]) -> None:
    """After the claim: post into the conversation, and release the claim unless the report is out.

    The findings are marked shown only once the claim is kept (posted, or on
    the text path most likely posted), so a released claim leaves none counted
    against the day's limit.
    """
    note = _audits_note(data_dir, no_ranking is True, home=bool(chat_notify.routed_platform()))
    outcome, reason, shown = _post_to_conversation(
        data_dir, conversation, content, note, lambda: _mark_shown(data_dir, in_sandbox, suppressed)
    )
    if outcome == NOT_POSTED:
        # Recorded first, so the released claim is retried only after the wait.
        _record_attempt(data_dir, reason, counted=True)
        _release_claim(data_dir)
        return
    if outcome != POSTED:
        _record_attempt(data_dir, reason, counted=False)
    _cleanup(data_dir, in_sandbox, shown)


def main(data_dir: Path | None = None) -> int:
    if data_dir is None:
        data_dir = _data_dir()

    completed = _completed_at(data_dir)
    if completed is not None:
        if not delivery_settled(data_dir):
            _release_stale_claim(data_dir, completed)
        elif time.time() - completed >= RETIRE_AFTER_SECONDS:
            _retire_jobs()
        return 0

    if not _awaiting_delivery(data_dir):
        return 0  # silent run — nobody to deliver to yet
    conversation = _bound_conversation(data_dir)
    if conversation is not None and not _conversation_ready(data_dir, conversation):
        return 0

    try:
        in_sandbox = sandbox_exec.sandbox_enabled()
    except sandbox_exec.SandboxMisconfigured as e:
        sys.stderr.write(f"bootstrap_delivery: cannot tell where INVENTORY.md is: {e}\n")
        return 1

    # Read before claiming, so a read failure leaves no claim behind to undo
    # and the next tick retries cleanly.
    try:
        raw = _read_report(data_dir, in_sandbox)
    except (sandbox_exec.SandboxUnavailable, subprocess.TimeoutExpired) as e:
        # Silent, and retried next tick: a non-zero exit is posted to the
        # user's chat as a failure alert on every tick. A lasting fault (a
        # rejected key, a changed host key, no ``ssh_host``) lands here too; the
        # agent's terminal shares that key and host, so it fails there too.
        sys.stderr.write(f"bootstrap_delivery: the shell sandbox did not answer: {e}\n")
        return 0
    except (OSError, sandbox_exec.SandboxMisconfigured, sandbox_exec.SandboxReadFailed) as e:
        sys.stderr.write(f"bootstrap_delivery: could not read INVENTORY.md: {e}\n")
        return 1
    if raw is None:
        return 0  # silent run — the report is not written yet
    if len(raw) > REPORT_MAX_BYTES:
        sys.stderr.write(
            f"bootstrap_delivery: INVENTORY.md is larger than {REPORT_MAX_BYTES} bytes; not delivering it\n"
        )
        return 1
    content = raw.decode("utf-8", errors="replace")
    # Measured as posted: each undecodable byte becomes a three-byte U+FFFD.
    if conversation is not None and len(content.encode("utf-8")) > CONVERSATION_REPORT_MAX_BYTES:
        _record_attempt(
            data_dir,
            f"INVENTORY.md is larger than {CONVERSATION_REPORT_MAX_BYTES} bytes, more than one post into the "
            "conversation can carry; not delivering it",
            counted=False,
        )
        return 1

    # Before the claim, which ends the nudge's hold, so the rows exist when
    # the marks below are sent.
    no_ranking = _no_ranking(data_dir)
    suppressed = _register_findings(data_dir, in_sandbox, no_ranking)

    # The cheap check above is advisory; this is the decision. Nothing may be
    # written to stdout before it succeeds.
    if not _claim_delivery(data_dir):
        return 0  # another run is delivering this report — stay silent

    if conversation is not None:
        _deliver_to_conversation(data_dir, in_sandbox, conversation, content, no_ranking, suppressed)
        return 0

    # Right after the claim, which ends the nudge's hold, so a nudge run has
    # the shortest window in which to announce these findings as new.
    shown = _mark_shown(data_dir, in_sandbox, suppressed)

    note = _audits_note(data_dir, no_ranking is True)
    if _posted_as_blocks(content):
        sys.stdout.write(note + "\n")
    else:
        sys.stdout.write(_with_note(_presented(content), note))
    sys.stdout.flush()

    # Cleanup runs only after the report is posted or safely on stdout (already
    # captured by the scheduler), so removing INVENTORY.md here cannot truncate delivery.
    _cleanup(data_dir, in_sandbox, shown)
    return 0


if __name__ == "__main__":
    sys.exit(main())
