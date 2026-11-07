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
- no young ``.bootstrap_greeting_pending.*`` -> the greeting of that opening turn
  is on its way (the plugin removes the hold), or never came within
  ``GREETING_WAIT_SECONDS``; the report follows the greeting instead of racing it.
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

A report nobody aligns to takes a third path (#4058). Once the sweep has been
filed for ``UNSPOKEN_FILE_AFTER_SECONDS`` with no human marker, the run files
the report as a ledger issue, in the GitOps repository when it is on GitHub or
else in the install's one managed GitHub repository, beside the first-run
audits' own, claims the delivery, and sends one line linking the issue to the
platform whose home posts route through the gateway (kept there for the digest
when no home channel is set). An install with no such repository keeps the
report in the store, and ``UNDELIVERED_RETIRE_SECONDS`` after the sweep, the
age past which the plugin primes nothing, the report is claimed without a post
and retired in favour of the scheduled audits.

With the flag on and a Slack destination, the report goes out as a card instead,
built by ``inventory_card`` from the files the prioritization worker left beside
the report (the items, scores, limits and shown files, read once before the claim
by ``_read_sweep_files``) and never from the report's text: a headline counting
the things to fix first, one row per listed item with its scored risk and action,
the queue line, the scan's checks folded, and the "Fix the first one" and "See all
N" buttons. The card needs the hand-off to have filed a prioritization card and
the files to be there, readable and in step (the recomputed selection equals the
shown file); otherwise the text goes, and one line in the oobe stage log says why
(``_first_report_card``), as it does on the origin path for a card its door could
not post (in a recorded conversation, a card that did not post is posted again on a
later tick, below). On a Slack
origin with the credential proxy's Slack relay in the environment it is posted
here as Block Kit through ``slack_blocks_post``, because the scheduler's delivery
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
marker by ``_audits_note``: the report's scan leaves security and RBAC, reliability, capacity and
cost to those audits (``inventory_findings.AUDIT_FAMILIES``; an install whose kept marker records
an older stage's no-repository skip ran the workload checks in the scan), and a short report would
otherwise read as a healthy fleet. It also names every shipped fleet audit that ran since the sweep
was filed and whose summary was not posted, however its run started, with a request for those
reports (``_lost_audits``): an audit that finished before anyone opened the chat had no home channel
to post to. A first-run audit's summary is posted after all by the ``oobe`` tick's last stage,
``resend_lost_summaries``, once the operator projects a home channel, while its run is inside
``RESEND_WINDOW_SECONDS``; every other one keeps the request. The note is built before the claim. The
text path appends it to the report; the card paths print it on their own, since the card leaves
closing lines out.

Just before the claim, the run registers the sweep's findings in the
findings queue from this pod, from the items and scores the prioritization
worker left beside the report (``_register_findings`` says why the worker's
own registration is not enough). Between the claim and the first byte of
stdout, it marks the report's findings shown in the findings queue, as the paced publisher ``first_report``,
from ``INVENTORY.shown.json`` (what ``inventory_findings.py select`` chose), read with
the other files before the claim.
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
what the user sees: as written; on Slack with the flag on, as the card built from
that stage's files without a model call; or, when the card cannot be built, laid
out again by ``inventory_presenter`` without one; followed by the audits note. The
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
from typing import NamedTuple

import bootstrap_handoff  # beside this script in the pod
import chat_notify  # beside this script in the pod
import findings_queue as fq  # beside this script in the pod
import forge  # beside this script in the pod
import gitops_workspace  # beside this script in the pod
import inventory_card  # beside this script in the pod
import inventory_findings  # beside this script in the pod
import oobe  # beside this script in the pod
import sandbox_exec
import slack_presenter  # beside this script in the pod

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
# Touched once the sweep's findings are in the findings queue (or the sweep found
# none), before the claim: the record that the fleet was checked, which the
# Slack Home tab's board reads (agents/platform/scripts/board_snapshot.py) so a
# clean cluster is not drawn as unchecked after a delivery that never posts.
FINDINGS_REGISTERED_MARKER = ".bootstrap_findings_registered"

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
# The pacing limits the hand-off wrote beside the raw file (bootstrap_handoff.LIMITS_PATH) and
# `select` read; the card recomputes `select`'s choice under the same ones.
LIMITS_NAME = "INVENTORY.limits.json"
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
# Google Chat's platform name. Its card goes only through the gateway (spec.mode: next),
# which lays the chat message out; Hermes's own Chat adapter under today takes text.
GCHAT_PLATFORM = "google_chat"
# Why the first report went as text and not as the card, one line in the oobe stage log
# (_stage_note), which an operator reads; the scheduler keeps a zero-exit run's stderr nowhere.
# No name here reads CARD_NO or CARD_NUM: CodeQL's private-data heuristic takes that for a card
# number and flags every stage-log line it reaches.
TEXT_REPORT_NOTE = "the first report went as text: {reason}"
CARD_FLAG_OFF = "KAGE_SLACK_UX is off"
CARD_PLATFORM_TAKES_TEXT = "the report goes to {platform}, which takes text"
CARD_PLATFORM_UNRECORDED = "a chat whose platform is not recorded"
CARD_HOME_UNROUTED = "the gateway routes no home posts to {platform}"
CARD_MARKER_UNREAD = "the hand-off marker could not be read, so the files beside the report may be an earlier sweep's"
CARD_RANKING_MISSING = "the hand-off filed no prioritization card, so the files beside the report are an earlier sweep's"
CARD_FILE_UNUSABLE = "{name} could not be read: {problem}"
CARD_FILE_MISSING = "no {name}"
CARD_BUILD_FAILED = "the card could not be built: {error!r}"
# A card that was built and not posted: the Slack doors' reasons.
CARD_UNPOSTED = "the report card was not posted, so the text goes: {reason}"
CARD_CHAT_ID_MISSING = "the origin is not a Slack chat with a chat id"
CARD_THREAD_MISSING = "the origin has no thread for the gateway to post into"
CARD_RELAY_MISSING = "no Slack relay is configured"
CARD_REFUSED = "Slack refused the blocks: {error}"
CARD_GATEWAY_REFUSED = "the gateway did not post it: {error}"
CARD_POST_FAILED = "{error!r}"

# The note on the fleet audits (_audits_note). The scan leaves their workload checks to the first-run
# audits. Its stage sentences say only what oobe's marker records the stage did; whether an audit ran is
# said by AUDITS_LOST_NOTE alone, from the cron store, so no sentence about the stage can contradict it.
AUDITS_SKIPPED_NOTE = "The audits did not run as part of onboarding: {reason}."
AUDITS_NO_REASON = "no reason was recorded"
# The reason an older oobe stage recorded when it skipped for no repository. A marker an install
# kept through the upgrade still carries it, and on such an install the scan ran those checks,
# unless a repository registered when the sweep was filed was removed before the stage read the list.
LEGACY_NO_REPOSITORY_REASON = "no GitHub repository in managed_repos for the audits to publish to"
AUDITS_COVERED_NOTE = (
    "This scan checked probes, requests and limits, autoscaling, NetworkPolicy and pod security in "
    "their place, so this report covers them, but not the rest of "
    f"{inventory_findings.AUDIT_FAMILY_WORDS}, which those audits check on their schedules."
)
# Every other skip leaves the audits to their schedules, and the scan did not run their checks.
AUDITS_NOT_COVERED_NOTE = (
    f"This report does not cover {inventory_findings.AUDIT_FAMILY_WORDS}; those audits check them on their schedules."
)
# A skip with the hand-off's no-coverage report (bootstrap_handoff.NO_RANKING).
AUDITS_UNCHECKED_SCHEDULED_NOTE = (
    "No cluster was audited, so this report covers no cluster; those audits check "
    f"{inventory_findings.AUDIT_FAMILY_WORDS} on their schedules."
)
# The first-run audits by what each reports on, in the posture line's order. The job ids stay out of
# these sentences; the list of unposted summaries keeps them, since a request for one names it.
AUDITS_AREAS = "The {areas} {audits}"
AUDITS_WORD = ("audit", "audits")
# Also the note before the marker exists: the stage writes it on its first tick after the ranking
# card finishes, which a user already in the chat can beat to this run.
AUDITS_RUNNING_NOTE = (
    f"{AUDITS_AREAS.format(areas=inventory_findings.AUDIT_FAMILY_WORDS, audits=AUDITS_WORD[1])} run one after another "
    "once this scan is done."
)
AUDITS_STARTED_NOTE = "{audits} started as part of onboarding."
AUDITS_NONE_STARTED_NOTE = (
    "None of the first-run audits ran as part of onboarding. This report does not cover "
    f"{inventory_findings.AUDIT_FAMILY_WORDS}."
)
AUDITS_NOT_STARTED_NOTE = "Did not run as part of onboarding: {audits}."
AUDITS_NOT_STARTED_ITEM = "the {area} audit ({reason})"
# oobe gives up on an audit it could not start; the audit still runs on its own schedule.
AUDITS_GAVE_UP_REASON = "it did not start, and runs on its own schedule"

# The shipped fleet audits beyond the first-run ones (agents/platform/cron/jobs.json, each `deliver: chat`),
# in roster order. One that ends before anyone opens the chat loses its summary the same way.
OTHER_FLEET_AUDITS = (
    "security-patch-orchestrator",
    "fleet-consistency-drift",
    "ai-security-audit",
    "gcp-networking-fabric-audit",
    "gce-compute-fleet-audit",
)
# A request that needs no chat history, so it works in any conversation; the nightly request-path case
# sends it verbatim. A test holds every audit the note checks to the store's audits (audit_report.AUDITS).
AUDITS_REQUEST_FORMAT = "Show me the latest report of each of these fleet audits from the report store: {ids}."
# The first-run chain's fifth job is not a posture family: named in the note by what it is.
AUDIT_AREA_EXTRA = {"upgrade-retrospective": "upgrade retrospective"}
# Said of every failed delivery, whatever refused it: usually no home channel yet, which an audit
# that ends before anyone opens the chat always meets, and the scheduler does not post it again
# (_lost_audits).
AUDITS_LOST_NOTE = (
    "These ran, but their summaries were not posted to the chat: {labels}. To see them, send me: `{request}`"
)
# An audit whose run failed wrote no report, so the request above cannot fetch what was lost: the
# scheduler's failure notice. Said, like AUDITS_LOST_NOTE, of every failed delivery, whatever refused it.
AUDITS_FAILED_RUNS_NOTE = "These failed, and their failure notices were not posted to the chat: {labels}."
AUDITS_UNREAD_NOTE = (
    "I could not read the audit run records, so this note cannot say which summaries reached the chat. "
    "To see any of them, send me: `{request}`"
)

# The scheduler's record of where a run's final message went (Hermes cron/executions.db), and the run
# statuses that can carry one: a completed run's summary, or a failed run's failure notice.
DELIVERY_OUTCOME_COLUMN = "delivery_outcome"
RUN_FAILED_STATUS = "failed"
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

# What the chat relay posted, per audit job: session_kv_server.py's audit_report_runs, written when it posts
# a fleet-audit job's report. `delivered` alone is not proof, because the relay plugin answers success for a
# run whose last message is a silence marker the scheduler let through, and `failed` alone is not either,
# because the plugin answers an error for a post that reached one platform and missed another.
SESSION_KV_DB_ENV = "SESSION_KV_DB_PATH"
DEFAULT_SESSION_KV_DB_PATH = "/var/lib/kube-agents/session/session_kv.db"
# SQLite's CURRENT_TIMESTAMP, which is UTC.
RELAY_POSTED_AT_FORMAT = "%Y-%m-%d %H:%M:%S"

# One ended run: (claimed_at, finished_at, delivery_outcome, status).
AuditRun = tuple[float, float | None, str | None, str]


class LostRun(NamedTuple):
    """One audit whose newest run in its window posted nothing: a lost summary, or a crashed run's lost notice."""

    audit: str
    crashed: bool
    claimed_at: float

# The sentence that names the home channel, and what replaces it where the gateway has none to post
# to (no A2A_NOTIFY_PLATFORM on the conversation path).
AUDITS_HOME_SENTENCE = "Each posts a summary to the home channel when it finds something new."
AUDITS_NO_HOME_SENTENCE = (
    "Each one's summary goes to the home channel, and this install has none yet, so those summaries "
    "are not posted to chat."
)

# A first-run audit whose summary found no home channel is posted again by the re-send stage
# (``resend_lost_summaries``) once the operator projects one: a request to the chat relay's own route,
# on the findings endpoint's server, so the relay reads the audit's chat report record and posts it as a
# scheduled run's delivery leg would, and records the post in ``audit_report_runs``.
CRON_REPORTS_PATH = "/v1/cron-reports"
# The relay reads the audit's record only for a request carrying this key (session_kv_server.SCHEDULER_LEG_KEY).
SCHEDULER_LEG_KEY = "also_delivered_to"
# What the relay posts when it cannot tell the job for a fleet audit (its roster unreadable): the request
# that fetches the report, so the message still leads to it.
RESEND_FALLBACK_REPORT = (
    "The {label} ran during onboarding, before this channel was set. To see its report, send me: `{request}`"
)
# A run is re-sent only while its claim is this recent. The relay posts an audit's record only under its own
# two-hour age (session_kv_server.CHAT_REPORT_MAX_AGE_S) and a "did not finish" notice past it; the record is
# written during the run, after the claim, so a claim inside this window keeps the record inside the relay's.
RESEND_WINDOW_SECONDS = 90 * 60
# The request's fields, as the relay plugin (deploy/docker/plugins/chat/adapter.py) names them.
REQUEST_JOB_KEY = "job_id"
REQUEST_PROFILE_KEY = "profile"
REQUEST_TITLE_KEY = "title"
REQUEST_REPORT_KEY = "report"
# How many times one audit's re-send is tried, one per tick. An answered request settles the audit
# (``RESENT_POSTED_KEY``); for one whose answer was lost the relay's own record decides; the cap is for a
# relay that keeps refusing. A refusal while the gateway's projected copy of the home file still lags the
# agent's (one kubelet refresh) costs one of these, and the gateway keeps that post for its digest.
RESEND_MAX_ATTEMPTS = 5
# One request to the relay, which posts synchronously and answers when the post has landed or failed.
RESEND_TIMEOUT_SECONDS = 180
RELAY_ANSWER_MAX_CHARS = 300
# ``audit -> {"tries": n, "posted": bool}``, beside the other markers; absent until the first try.
RESENT_FILE = ".oobe_summaries_resent.json"
RESENT_TRIES_KEY = "tries"
RESENT_POSTED_KEY = "posted"

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

# The plugin's hold on the report while the opening turn's greeting is on its way: one
# empty file per opening turn (bootstrap_onboarding, GREETING_PENDING_PREFIX), which the
# plugin removes when the greeting leaves the agent. Aged by mtime here, never read. The
# wait is the ceiling for a greeting turn that never reports back (a gateway restart
# inside the turn, a turn that ends with no text): past it the report goes out without
# the greeting. The longest first turn measured, a real ask that delegated work, took
# 218 s (#1971, #2009); 300 s covers it with a margin for a loaded model and its provider
# retries. A greeting that runs longer still arrives, after the report.
GREETING_PENDING_GLOB = ".bootstrap_greeting_pending.*"
GREETING_WAIT_SECONDS = 300


# The report when no chat thread can take it (#4058). A human who speaks in
# Slack or Google Chat binds the report to that thread (the plugin's
# ``.user_aligned`` markers); an install nobody speaks to, or one with no chat
# platform at all, would otherwise hold the report forever. So once the sweep
# has been filed for UNSPOKEN_FILE_AFTER_SECONDS with nobody aligned, the report
# is filed as a ledger issue (the GitOps repository when it is on GitHub, else
# the one managed GitHub repository), beside the first-run audits' own, and one
# line linking it goes to the home channel when a platform
# routes there (kept by the gateway for its digest when no home channel is
# set). The wait is what tells "nobody yet" from "nobody": a chat install's
# first message usually comes within hours of install, and a report filed as an
# issue before it would pre-empt the thread it was written for.
SECONDS_PER_DAY = 24 * 60 * 60
UNSPOKEN_FILE_AFTER_SECONDS = 1 * SECONDS_PER_DAY
# Past this age the plugin primes nothing (bootstrap_onboarding
# SCAN_MAX_AGE_SECONDS), so no thread can ever take the report: a run that
# finds it still undelivered claims it without posting and lets the scheduled
# audits, which by then say more, stand in. Pinned equal by a test.
UNDELIVERED_RETIRE_SECONDS = 7 * SECONDS_PER_DAY
# The ledger issue: found again by its label and body marker, so a run that
# died between the create and its record reuses the issue rather than opening
# a second. The label mirrors chat_delivery_watch's, which the same broker
# verbs maintain.
REPORT_ISSUE_LABEL = "agent:onboarding"
REPORT_ISSUE_LABEL_COLOR = "0E8A16"
REPORT_ISSUE_LABEL_DESCRIPTION = "Onboarding's first-run inventory report, filed when no chat thread could take it"
REPORT_ISSUE_MARKER = "<!-- onboarding-first-report -->"
# The marker that names this install's sweep, so an install that shares the
# repository with another (dev projects reinstall onto one) reuses only its own
# issue; REPORT_ISSUE_MARKER alone still finds an older build's issue.
REPORT_ISSUE_SWEEP_MARKER = "<!-- onboarding-first-report sweep={sweep} -->"
# Written by the retire path beside the claim; with REPORT_FILED_FILE it is
# what tells a claim this path kept from one its run died holding.
REPORT_RETIRED_FILE = ".bootstrap_report_retired"
# Written by the no-human path just before it claims and removed with every
# release of that claim: a claim beside it with neither record above is one a
# run died holding, and the stale-claim rule releases it; the origin path, which
# never writes it, keeps its claim settled the moment it exists, as before.
REPORT_CLAIMING_FILE = ".bootstrap_report_claiming"
REPORT_ISSUE_TITLE = "First-run inventory report ({date} UTC)"
REPORT_ISSUE_INTRO = (
    "Onboarding's first-run inventory report, filed here on {date} UTC because no chat thread could take it: "
    "nobody had written to the agent on Slack or Google Chat since the sweep was filed on {filed} UTC. "
    "It describes the fleet as it was then; the scheduled audits carry the current picture."
)
REPORT_ISSUE_LIST_LIMIT = 20
REPORT_ISSUE_DATE_FMT = "%Y-%m-%d %H:%M"
# Where this stage records the issue it filed (repository, number, when), read
# by a later run's log line and by an operator asking where the report went.
REPORT_FILED_FILE = ".bootstrap_report_filed"
FILED_REPO = "repo"
FILED_NUMBER = "number"
FILED_URL = "url"
FILED_AT = "filed_at"
# The issue's web address when the broker's answer carries none: github.com for
# an `owner/name` slug, the named host for a GitHub Enterprise Server
# `host/owner/name` one (gitops_workspace.get_managed_github_repo_names).
ISSUE_URL_FORMAT = "https://{host}/{slug}/issues/{number}"
ISSUE_URL_DEFAULT_HOST = "github.com"
# The keys a broker answer may carry the issue's address under, and what an address starts with.
ISSUE_URL_KEYS = ("html_url", "url")
URL_SCHEME_PREFIX = "http"
# GitHub refuses an issue body past this many characters; a report that long is
# recorded as unfilable rather than retried five times for a 422.
ISSUE_BODY_MAX_CHARS = 65536
# This path's own record of attempts that released the claim (the conversation
# path keeps CONVERSATION_ATTEMPTS_FILE): the two must not share a count, or
# five refused creates would stop the thread delivery a human binds later.
REPORT_ATTEMPTS_FILE = ".bootstrap_report_attempts.json"
# The one line that goes to the home channel once the issue is up: a link, so a
# reader opens the report from the message (chat_ux.md, no plumbing in user
# text), and the user's own fact rather than the stage's. ``a2a notify`` exits
# NOTIFY_KEPT when the gateway kept it for its digest, which is delivery
# enough: the digest names it, dated, once a home channel is set.
REPORT_ISSUE_HOME_POST = (
    "First-run inventory report filed at {url} on {date} UTC: nobody had written to me in chat since the "
    "first look at your fleet."
)
# The reasons this path records (REPORT_ATTEMPTS_FILE), one per tick at most.
NO_LEDGER_REPO_REASON = (
    "no chat thread took the first-run report and the install has no GitOps repository to file it in; "
    "it stays in the report store"
)
REPORT_LEDGER_REPO_ENV = "ONBOARDING_LEDGER_REPO"
LEDGER_REPO_AMBIGUOUS_REASON = (
    "no chat thread took the first-run report and the install manages {count} GitHub repositories with no "
    "GitOps role among them; not choosing one to file it in (set " + REPORT_LEDGER_REPO_ENV + " to name it)"
)
REPORT_RETIRED_NOTE = (
    "the first-run report was not delivered within {days} days of the sweep and is retired; "
    "the scheduled audits stand in for it"
)
REPORT_FILED_NOTE = "the first-run report went to {ref}: no chat thread took it"
REPORT_UNLABELLED_NOTE = (
    "the first-run report issue {ref} carries no " + REPORT_ISSUE_LABEL + " label (the repository refused it); "
    "the issue resolver will not skip it"
)
REPORT_FILING_FAILED_REASON = "filing the first-run report in {repo} failed: {error!r}"
LEDGER_REPO_UNREADABLE_REASON = "the managed repositories could not be read: {error}; the first-run report waits"
REPORT_HOME_LINE_NOTE = "the home line for the filed report {outcome}"
HOME_LINE_POSTED = "was posted"
HOME_LINE_KEPT = "was kept by the gateway for its digest (no home channel yet)"
HOME_LINE_FAILED = "was not posted (a2a notify exited {code}: {detail})"
HOME_LINE_NO_EXIT = "none"
REPORT_TOO_LONG_REASON = (
    "the first-run report is longer than an issue body may be ({chars} characters, limit {limit}); not filing it"
)


def _data_dir() -> Path:
    return Path(os.environ.get("HERMES_HOME", "/opt/data"))


def _awaiting_delivery(data_dir: Path) -> bool:
    """True when a human is present and the greeting is not still on its way;
    main() has already returned on a delivered report.

    The markers live on this pod, so this costs a stat or two; it runs before the
    report read, which may cross into the sandbox.
    """
    if not ((data_dir / ".user_aligned").exists() or (data_dir / CONVERSATION_ALIGNED_MARKER).exists()):
        return False
    return not _greeting_pending(data_dir)


def _greeting_pending(data_dir: Path) -> bool:
    """Whether the plugin still holds the report behind the opening turn's greeting.

    The newest hold decides. One older than GREETING_WAIT_SECONDS belongs to a
    greeting turn that never reported back: every hold is removed, the stage log
    says so once, and the report goes out. Never raises: a raise here would exit
    the run non-zero on every tick, which posts to the chat on the origin path
    and blocks the report for good on the conversation path.
    """
    try:
        holds = list(data_dir.glob(GREETING_PENDING_GLOB))
    except OSError:
        return False
    newest = None
    for hold in holds:
        try:
            at = hold.stat().st_mtime
        except OSError:
            continue
        newest = at if newest is None else max(newest, at)
    if newest is None:
        return False
    if abs(time.time() - newest) < GREETING_WAIT_SECONDS:
        return True
    for hold in holds:
        try:
            hold.unlink()
        except OSError:
            pass
    _stage_note(data_dir, f"the greeting did not finish within {GREETING_WAIT_SECONDS} s; delivering the report")
    return False


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


def _read_attempts(data_dir: Path, name: str = CONVERSATION_ATTEMPTS_FILE) -> dict:
    try:
        record = json.loads((data_dir / name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return record if isinstance(record, dict) else {}


def _attempt_count(record: dict) -> int:
    count = record.get(ATTEMPTS_COUNT)
    return count if isinstance(count, int) and not isinstance(count, bool) and count > 0 else 0


def _record_attempt(data_dir: Path, reason: str, counted: bool, name: str = CONVERSATION_ATTEMPTS_FILE) -> None:
    """Record why the report is not out; ``counted`` adds one post that released the claim."""
    sys.stderr.write(f"bootstrap_delivery: {reason}\n")
    record = _read_attempts(data_dir, name)
    count = _attempt_count(record) + (1 if counted else 0)
    if not counted and record.get(ATTEMPTS_REASON) == reason:
        return  # the same block as the last tick's: no write each minute
    record.update({ATTEMPTS_COUNT: count, ATTEMPTS_REASON: reason})
    if counted:
        # Only a counted attempt starts the wait before the next one.
        record[ATTEMPTS_AT] = time.time()
    if counted and count >= CONVERSATION_MAX_ATTEMPTS:
        _stage_note(data_dir, f"the report was not posted after {count} attempts and now waits: {reason}")
    target = data_dir / name
    tmp = target.with_name(target.name + ".tmp")
    try:
        tmp.write_text(json.dumps(record, sort_keys=True) + "\n", encoding="utf-8")
        tmp.replace(target)
    except OSError as e:
        sys.stderr.write(f"bootstrap_delivery: could not record the attempt: {e}\n")


def _outcome_recorded(data_dir: Path) -> bool:
    return (data_dir / CONVERSATION_DONE_FILE).exists()


def delivery_settled(data_dir: Path) -> bool:
    """Whether a taken claim may retire the jobs: always on the origin path (a human was aligned),
    on the conversation path once the report's post recorded an outcome, and on the path with no
    human once the filing or the retire is on record; a claim that run died holding is released
    like a stale conversation claim."""
    if (data_dir / REPORT_CLAIMING_FILE).exists():
        # This path's claim: its own record settles it, whatever a conversation bound a moment
        # later says; that conversation's run finds the report archived and posts nothing.
        return (data_dir / REPORT_FILED_FILE).exists() or (data_dir / REPORT_RETIRED_FILE).exists()
    if _bound_conversation(data_dir) is not None:
        return _outcome_recorded(data_dir)
    return True


def _mark_claiming(data_dir: Path) -> bool:
    """Mark the claim about to be taken as this path's; without the mark no claim is taken, since an
    unmarked claim that then dies would read as a delivered origin claim and retire the jobs."""
    try:
        (data_dir / REPORT_CLAIMING_FILE).write_text(f"{time.time()}\n", encoding="utf-8")
    except OSError as e:
        sys.stderr.write(f"bootstrap_delivery: could not record the claim's path, not claiming: {e}\n")
        return False
    return True


def _unmark_claiming(data_dir: Path) -> None:
    """A lost claim is someone else's (an origin run that got there first): the marker must not
    make theirs read as one of this path's with no record."""
    try:
        (data_dir / REPORT_CLAIMING_FILE).unlink()
    except FileNotFoundError:
        pass
    except OSError as e:
        sys.stderr.write(f"bootstrap_delivery: could not drop the claim's path marker: {e}\n")


def _claim_for_this_path(data_dir: Path) -> bool:
    if not _mark_claiming(data_dir):
        return False
    if _claim_delivery(data_dir):
        return True
    _unmark_claiming(data_dir)
    return False


def _release_claim(data_dir: Path) -> None:
    for name in (".bootstrap_completed", REPORT_CLAIMING_FILE):
        try:
            (data_dir / name).unlink()
        except FileNotFoundError:
            pass
        except OSError as e:
            sys.stderr.write(f"bootstrap_delivery: could not release the claim ({name}): {e}\n")


def _release_stale_claim(data_dir: Path, claimed_at: float) -> None:
    """Release a claim whose run never recorded an outcome: a conversation-path one, or a no-human
    one (``REPORT_CLAIMING_FILE`` beside it), each counted in its own record.

    Only past ``STALE_CLAIM_SECONDS``, so a run still posting keeps it. It
    counts as an attempt unless one was already recorded after the claim.
    """
    if time.time() - claimed_at < STALE_CLAIM_SECONDS:
        return
    name = REPORT_ATTEMPTS_FILE if (data_dir / REPORT_CLAIMING_FILE).exists() else CONVERSATION_ATTEMPTS_FILE
    try:
        last = float(_read_attempts(data_dir, name).get(ATTEMPTS_AT))
    except (TypeError, ValueError):
        last = 0.0
    if last <= claimed_at:
        _record_attempt(data_dir, "a run claimed the report and recorded no outcome; trying again", counted=True, name=name)
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


def _parse_shown(raw: bytes):
    """``SHOWN_NAME``'s record from its bytes; ``ValueError`` past ``SHOWN_MAX_BYTES`` or when it is not JSON."""
    if len(raw) > SHOWN_MAX_BYTES:
        raise ValueError(f"larger than {SHOWN_MAX_BYTES} bytes")
    return json.loads(raw.decode("utf-8"))


class SweepFiles(NamedTuple):
    """What the prioritization worker left beside the report, read once before the claim.

    ``items`` and ``scores`` are the parsed files, None when absent; ``limits`` the pacing
    limits the hand-off wrote, the defaults when the file is absent or unusable, as ``select``
    read them; ``shown`` the shown file's bytes, None when absent, which ``_mark_shown`` and the
    card each parse for what they need. ``problems`` names each file that was there and could not
    be read or parsed, with why; its readers say what they did without it.
    """

    items: dict | None
    scores: dict | None
    limits: fq.PacingLimits
    shown: bytes | None
    problems: dict[str, str]


def _read_sweep_files(data_dir: Path, in_sandbox: bool) -> SweepFiles:
    """The files beside the report, each read once, where the report was read.

    Raises ``sandbox_exec.SandboxUnavailable`` or ``subprocess.TimeoutExpired`` when the sandbox
    did not answer, for ``main`` to treat as it does the report's read: silent, retried next tick.
    Every other failure to read or parse a file goes in ``problems`` under its name; a file that
    is not there is None and no problem.
    """
    parsed: dict[str, dict | None] = {}
    problems: dict[str, str] = {}
    for name in (ITEMS_NAME, SCORES_NAME, LIMITS_NAME):
        try:
            parsed[name] = _read_json_beside(data_dir, in_sandbox, name)
        except (sandbox_exec.SandboxUnavailable, subprocess.TimeoutExpired):
            raise
        except Exception as e:  # noqa: BLE001 - the file's readers say what they did without it
            parsed[name] = None
            problems[name] = str(e)
    try:
        shown = _read_shown(data_dir, in_sandbox)
    except (sandbox_exec.SandboxUnavailable, subprocess.TimeoutExpired):
        raise
    except Exception as e:  # noqa: BLE001 - as above
        shown = None
        problems[SHOWN_NAME] = str(e)
    if LIMITS_NAME in problems:
        sys.stderr.write(
            f"bootstrap_delivery: cannot read {LIMITS_NAME} ({problems[LIMITS_NAME]}); using the default limits\n"
        )
    elif parsed[LIMITS_NAME] is None:
        sys.stderr.write(f"bootstrap_delivery: no {LIMITS_NAME}; using the default limits\n")
    limits = inventory_findings.limits_from(parsed[LIMITS_NAME] or {})
    return SweepFiles(parsed[ITEMS_NAME], parsed[SCORES_NAME], limits, shown, problems)


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


def _register_findings(data_dir: Path, no_ranking: bool | None, files: SweepFiles) -> set[str]:
    """Register every finding the sweep extracted in the findings queue, as
    ``inventory_findings.py register`` does, from this pod, from the items and
    scores read before the claim (``files``).

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
        if no_ranking is None:
            sys.stderr.write("bootstrap_delivery: the hand-off marker could not be read; registering nothing\n")
            return suppressed
        if no_ranking:
            sys.stderr.write(
                "bootstrap_delivery: the hand-off filed no prioritization card, so any "
                f"{ITEMS_NAME} is an earlier sweep's; registering nothing\n"
            )
            return suppressed
        if ITEMS_NAME in files.problems:
            raise ValueError(files.problems[ITEMS_NAME])
        if files.items is None:
            sys.stderr.write(f"bootstrap_delivery: no {ITEMS_NAME}; registering nothing\n")
            return suppressed
        items = files.items[ITEMS_KEY]
        if not items:
            _mark_registered(data_dir)
            return suppressed  # a clean fleet: nothing to register, and no scores file
        if SCORES_NAME in files.problems:
            raise ValueError(files.problems[SCORES_NAME])
        raw_scores = files.scores
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
    refused = 0
    for where, batch, scope in batches:
        try:
            result = inventory_findings.post_batch(endpoint, batch, scope)
        except urllib.error.HTTPError as e:
            # This cluster only.
            sys.stderr.write(f"bootstrap_delivery: the findings queue refused {where}'s findings: {e.code}\n")
            refused += 1
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
    # A refused batch is a cluster the queue does not hold, so the fleet is not checked yet.
    if not refused:
        _mark_registered(data_dir)
    return suppressed


def _mark_registered(data_dir: Path) -> None:
    """Record that the sweep's findings reached the queue; best effort, never raises."""
    try:
        (data_dir / FINDINGS_REGISTERED_MARKER).touch(exist_ok=True)
    except OSError as e:
        sys.stderr.write(f"bootstrap_delivery: could not touch {FINDINGS_REGISTERED_MARKER}: {e}\n")


def _session_kv_headers() -> dict[str, str]:
    headers = dict(JSON_HEADERS)
    token = (os.environ.get(SESSION_KV_AUTH_ENV) or "").strip()
    if token:
        headers[AUTH_HEADER] = f"{BEARER_PREFIX}{token}"
    return headers


def _post_surfaced(endpoint: str, finding_id: str, body: dict) -> None:
    path = SURFACED_PATH.format(id=urllib.parse.quote(finding_id, safe=""))
    request = urllib.request.Request(
        f"{endpoint}{path}", data=json.dumps(body).encode("utf-8"), headers=_session_kv_headers(), method="POST"
    )
    with urllib.request.urlopen(request, timeout=MARK_TIMEOUT_SECONDS) as response:
        response.read()


def _mark_shown(files: SweepFiles, suppressed: set[str] = frozenset()) -> bool:
    """Mark every row of every item the report lists shown, as ``PUBLISHER``,
    from the shown file read before the claim (``files``), except the
    ``suppressed`` ids registration reported: the user dismissed them, so they
    are never pending and spend no slot of the day's limit. The queue refuses
    such a mark anyway; skipping it saves the request.

    Returns whether to set the shown file aside: True unless there is none,
    so a file this run could not read is not marked by a later report.
    Never raises: the claim is taken, so nothing here may stop the report
    going out.
    """
    problem = files.problems.get(SHOWN_NAME)
    if problem:
        sys.stderr.write(f"bootstrap_delivery: could not read {SHOWN_NAME}; marking nothing: {problem}\n")
        return True
    raw = files.shown
    if raw is None:
        sys.stderr.write(f"bootstrap_delivery: no {SHOWN_NAME}; marking nothing\n")
        return False
    try:
        items = _parse_shown(raw)[SHOWN_ITEMS]
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


def _card_platform(platform: str | None) -> bool:
    """Whether the first report's card goes to ``platform``: Slack, or Google Chat where the
    gateway serves it (``spec.mode: next``), home posts or conversations, and lays the card out."""
    if platform == PRESENTED_PLATFORM:
        return True
    return platform == GCHAT_PLATFORM and (chat_notify.routes(platform) or chat_notify.gateway_platform() == platform)


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
    try:
        filed = oobe.scan_filed(data_dir)
        skipped_at = float(state.get(oobe.STATE_AT))
    except (OSError, TypeError, ValueError):
        # Also a marker whose bytes do not decode: the not-covered caveat is the safe one.
        return False
    return filed is not None and skipped_at >= filed[1]


def _audits_note(data_dir: Path, no_coverage: bool, home: bool = True) -> str:
    """The note on the fleet audits the delivery closes with.

    In order: what the stage did (``_stage_sentences``); the audits whose summaries ran but were not
    posted, each fleet audit whatever path its run took, or why that cannot be told; then where later
    summaries go. ``home`` false is a gateway with no home channel to post to, which every path says.
    With one, the home sentence is left out beside a list of unposted summaries, which it would contradict.
    ``no_coverage`` is a report no cluster's audit reached (``_no_ranking``), which covers nothing.
    """
    state = oobe.read_state(data_dir)
    stage, later_posts = _stage_sentences(data_dir, state, no_coverage)
    sentences = [stage]
    try:
        lost = _lost_sentences(data_dir, state)
    except Exception as e:  # noqa: BLE001 - the stage sentence, and its coverage caveat, still go out
        _stage_note(data_dir, f"the audits note could not tell which summaries were posted: {e!r}")
        lost = [AUDITS_UNREAD_NOTE.format(request=_request(_checked_audits()))]
    sentences += lost
    later_posts = later_posts and not lost
    if not home:
        sentences.append(AUDITS_NO_HOME_SENTENCE)
    elif later_posts:
        sentences.append(AUDITS_HOME_SENTENCE)
    return " ".join(sentences)


def _lost_sentences(data_dir: Path, state: dict) -> list[str]:
    """The sentences on runs whose summaries or failure notices were not posted, or that this cannot be told."""
    audits = _checked_audits()
    runs = _audit_runs(data_dir, audits)
    if runs is None:
        return [AUDITS_UNREAD_NOTE.format(request=_request(audits))]
    lost = _lost_audits(audits, runs, _windows(data_dir, state, audits), _relay_posts(data_dir))
    summaries = [run.audit for run in lost if not run.crashed]
    failed = [run.audit for run in lost if run.crashed]
    sentences = []
    if summaries:
        labels = ", ".join(_audit_label(a) for a in summaries)
        sentences.append(AUDITS_LOST_NOTE.format(labels=labels, request=_request(summaries)))
    if failed:
        sentences.append(AUDITS_FAILED_RUNS_NOTE.format(labels=", ".join(_audit_label(a) for a in failed)))
    return sentences


def _safe_audits_note(data_dir: Path, no_coverage: bool, home: bool) -> str:
    """``_audits_note``, or on any error the unreadable-records note: delivery calls no stage under oobe's guard."""
    try:
        return _audits_note(data_dir, no_coverage, home)
    except Exception as e:  # noqa: BLE001 - a raise here would hold the report back on every tick
        _stage_note(data_dir, f"the audits note failed, so it says the run records could not be read: {e!r}")
        note = AUDITS_UNREAD_NOTE.format(request=_request(_checked_audits()))
        return note if home else f"{note} {AUDITS_NO_HOME_SENTENCE}"


def _stage_sentences(data_dir: Path, state: dict, no_coverage: bool) -> tuple[str, bool]:
    """What oobe's marker says the stage did, and whether audits it started or will start are still to report.

    Before the marker exists the stage has not decided, and the sentence is the chain's.
    """
    if state.get(oobe.STATE_SKIPPED):
        reason = str(state.get(oobe.STATE_REASON) or AUDITS_NO_REASON)
        if no_coverage:
            coverage = AUDITS_UNCHECKED_SCHEDULED_NOTE
        elif reason == LEGACY_NO_REPOSITORY_REASON and _skip_follows_this_sweep(data_dir, state):
            coverage = AUDITS_COVERED_NOTE
        else:
            coverage = AUDITS_NOT_COVERED_NOTE
        return f"{AUDITS_SKIPPED_NOTE.format(reason=reason)} {coverage}", False
    fired = [a for a in oobe.FIRST_RUN_AUDITS if a in _list(state.get(oobe.STATE_FIRED))]
    held = state.get(oobe.STATE_HELD) if isinstance(state.get(oobe.STATE_HELD), dict) else {}
    gave_up = _list(state.get(oobe.STATE_GAVE_UP))
    reasons = {a: held[a] for a in oobe.FIRST_RUN_AUDITS if a in held}
    reasons.update({a: AUDITS_GAVE_UP_REASON for a in oobe.FIRST_RUN_AUDITS if a in gave_up and a not in reasons})
    not_started = [AUDITS_NOT_STARTED_ITEM.format(area=_area(a), reason=reasons[a]) for a in _in_area_order(list(reasons))]
    if not state.get(oobe.STATE_DONE):
        note, later_posts = AUDITS_RUNNING_NOTE, True
    elif fired:
        note, later_posts = AUDITS_STARTED_NOTE.format(audits=_areas(fired)), True
    else:
        note, later_posts = AUDITS_NONE_STARTED_NOTE, False
    if not_started:
        note = f"{note} {AUDITS_NOT_STARTED_NOTE.format(audits=', '.join(not_started))}"
    return note, later_posts


def _area(audit: str) -> str:
    """What ``audit`` reports on, or its job id for an audit with no area."""
    return inventory_findings.AUDIT_FAMILIES.get(audit) or AUDIT_AREA_EXTRA.get(audit) or _audit_label(audit)


def _in_area_order(audits: list[str]) -> list[str]:
    """``audits`` in the posture line's order of their areas, any audit with no area last."""
    order = list(inventory_findings.AUDIT_FAMILIES)
    return sorted(audits, key=lambda a: order.index(a) if a in order else len(order))


def _areas(audits: list[str]) -> str:
    """"The a, b and c audits", in the posture line's order of the areas."""
    words = [_area(a) for a in _in_area_order(audits)]
    joined = words[0] if len(words) == 1 else f"{', '.join(words[:-1])} and {words[-1]}"
    return AUDITS_AREAS.format(areas=joined, audits=AUDITS_WORD[len(words) != 1])


def _checked_audits() -> tuple[str, ...]:
    """Every fleet audit whose lost summary the note names: the first-run audits in chain order, then the rest.

    Built when called, not at import: ``oobe`` imports this module, so its tuples are not defined yet
    when ``oobe`` is imported first.
    """
    first = tuple(oobe.FIRST_RUN_AUDITS)
    return first + tuple(a for a in OTHER_FLEET_AUDITS if a not in first)


def _audit_label(audit: str) -> str:
    return f"`{audit}`"


def _request(audits: list[str] | tuple[str, ...]) -> str:
    """The message that fetches ``audits``' latest reports, in their order."""
    return AUDITS_REQUEST_FORMAT.format(ids=", ".join(audits))


def _windows(data_dir: Path, state: dict, audits: tuple[str, ...]) -> dict[str, float]:
    """``audit -> time``: a run that ended before it is not one this onboarding owes the user.

    The sweep's filing, or for an audit the stage marked its mark, whichever is later. A mark that is
    not a number reads as no mark, and a sweep marker that cannot be read as none: this runs before
    the claim, and a raise here would hold the report back on every tick.
    """
    try:
        filed = oobe.scan_filed(data_dir)
    except (OSError, ValueError) as e:
        _stage_note(data_dir, f"cannot read the sweep marker, so the audits note reads every run: {e}")
        filed = None
    since = filed[1] if filed is not None else 0
    marks = state.get(oobe.STATE_MARKS) if isinstance(state.get(oobe.STATE_MARKS), dict) else {}
    fired = _list(state.get(oobe.STATE_FIRED))
    windows = {}
    for audit in audits:
        mark = marks.get(audit) if audit in fired else None
        windows[audit] = max(since, mark) if isinstance(mark, (int, float)) else since
    return windows


def _audit_runs(data_dir: Path, jobs: tuple[str, ...]) -> dict[str, list[AuditRun]] | None:
    """``job -> [(claimed_at, finished_at, delivery_outcome, status), ...]``: the ended runs of ``jobs``, oldest first.

    Completed runs carry a summary, failed ones the scheduler's failure notice. Empty before the
    Platform Agent's cron store exists; None when it cannot be read, and the stage log says why
    (Hermes drops this run's stderr when it exits 0).
    """
    ledger = data_dir / oobe.PROFILES_DIR / oobe.PLATFORM_PROFILE / oobe.CRON_DIR / oobe.EXECUTIONS_DB
    if not ledger.is_file():
        return {}
    statuses = (oobe.COMPLETED_STATUS, RUN_FAILED_STATUS)
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
                f"SELECT job_id, claimed_at, {finished}, {outcome}, status FROM executions "
                f"WHERE job_id IN ({','.join('?' * len(jobs))}) AND status IN ({','.join('?' * len(statuses))}) "
                "ORDER BY claimed_at",
                (*jobs, *statuses),
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as e:
        _stage_note(data_dir, f"cannot read {ledger}, so the audits note cannot say which summaries were posted: {e}")
        return None
    runs: dict[str, list[AuditRun]] = {}
    for job_id, claimed, finished_at, delivery, status in rows:
        try:
            claimed_at = datetime.fromisoformat(claimed).timestamp()
        except (TypeError, ValueError):
            continue
        try:
            ended = datetime.fromisoformat(finished_at).timestamp()
        except (TypeError, ValueError):
            ended = None
        runs.setdefault(job_id, []).append((claimed_at, ended, delivery, status))
    return runs


def _relay_posts(data_dir: Path) -> dict[str, float] | None:
    """``job -> when the chat relay last posted for it``.

    From session-kv's database; None when unreadable, which leaves each run's delivery outcome to
    decide alone, and the stage log says why.
    """
    db = Path(os.environ.get(SESSION_KV_DB_ENV) or DEFAULT_SESSION_KV_DB_PATH)
    if not db.is_file():
        _stage_note(data_dir, f"no {db}, so the audits note takes each run's delivery outcome as it is")
        return None
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=bootstrap_handoff.SQLITE_BUSY_TIMEOUT_SECONDS)
        try:
            rows = conn.execute(
                "SELECT job_id, posted_at FROM audit_report_runs WHERE profile = ?", (oobe.PLATFORM_PROFILE,)
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as e:
        _stage_note(data_dir, f"cannot read {db}, so the audits note takes each run's delivery outcome as it is: {e}")
        return None
    posts = {}
    for job_id, posted_at in rows:
        try:
            posted = datetime.strptime(posted_at, RELAY_POSTED_AT_FORMAT).replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            continue
        posts[job_id] = posted.timestamp()
    return posts


def _lost_audits(audits: tuple[str, ...], runs: dict[str, list[AuditRun]], windows: dict[str, float],
                 posts: dict[str, float] | None) -> list[LostRun]:
    """A ``LostRun`` for each audit whose newest run in its window with something to post did not post it.

    A run that ends before anyone has opened the chat finds no home channel, the relay refuses
    it, and nothing posts it later; this delivery is the first message that user reads. Runs are
    judged newest first. A silent one is passed over, so it does not stand in for a lost one. A relay
    post at or after a run's claim settles it as posted, whatever its outcome says. Lost otherwise
    means a ``failed`` outcome or one the table does not know. A run the scheduler counts as delivered
    that the relay has no post for posted nothing, and is passed over too; with no relay record to ask,
    its outcome stands. A run that crashed wrote no report, so it does not stand in for an earlier run
    whose report is in the store: the audit is named for both. Its posted failure notice replaces the
    relay's one row per job, so the post is read as the crashed run's, and nothing left in the row says
    whether an earlier run posted: the walk goes on, and each earlier run is judged by its outcome.
    Where that names a summary that did post, the user asks one question instead of missing a report.
    """
    lost = []
    for audit in audits:
        since = windows.get(audit, 0)
        posted = posts.get(audit) if posts is not None else None
        crashed = False
        for claimed, ended, outcome, status in reversed(runs.get(audit, ())):
            if (ended if ended is not None else claimed) < since:
                break
            kind = DELIVERY_OUTCOME_CLASS.get(outcome, OUTCOME_LOST)
            if kind == OUTCOME_SILENT:
                continue
            if posted is not None and posted >= claimed:
                # The relay keeps one row per job, so the post is this run's; a posted failure notice is
                # not an earlier run's report.
                if status != RUN_FAILED_STATUS:
                    break
                posted = None
                continue
            if kind == OUTCOME_LOST and status == RUN_FAILED_STATUS:
                if not crashed:
                    lost.append(LostRun(audit, True, claimed))
                crashed = True
                continue
            if kind == OUTCOME_LOST:
                lost.append(LostRun(audit, False, claimed))
                break
            if posts is None and status != RUN_FAILED_STATUS:
                break
    return lost


def _resend_state(data_dir: Path) -> dict[str, dict]:
    """``audit -> {"tries": n, "posted": bool}`` from ``RESENT_FILE``; empty when absent or unreadable."""
    try:
        raw = json.loads((data_dir / RESENT_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {k: v for k, v in raw.items() if isinstance(k, str) and isinstance(v, dict)}


def _resend_tries(entry: dict) -> int:
    tries = entry.get(RESENT_TRIES_KEY)
    return tries if isinstance(tries, int) and not isinstance(tries, bool) else 0


def _record_resend(data_dir: Path, audit: str, tried: bool, posted: bool) -> None:
    """Count a try and settle ``audit`` once the relay answered, replacing the file whole so a run killed
    mid-write leaves the previous one."""
    state = _resend_state(data_dir)
    entry = state.get(audit, {})
    entry[RESENT_TRIES_KEY] = _resend_tries(entry) + (1 if tried else 0)
    entry[RESENT_POSTED_KEY] = bool(entry.get(RESENT_POSTED_KEY)) or posted
    state[audit] = entry
    target = data_dir / RESENT_FILE
    tmp = target.with_name(target.name + oobe.TMP_SUFFIX)
    tmp.write_text(json.dumps(state, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(target)


class RelayAnswer(NamedTuple):
    """One request's outcome: the line for the log, whether the relay answered (the audit is settled), and
    whether the request counts as a try."""

    outcome: str
    posted: bool
    tried: bool


def pending_resends(data_dir: Path, state: dict, now: float) -> list[LostRun]:
    """The first-run audits whose summary found no home channel and whose report the relay can still post.

    ``_lost_audits`` over the first-run chain, less the crashed runs (a failure notice is not a report), the
    runs claimed more than ``RESEND_WINDOW_SECONDS`` ago, and the audits tried ``RESEND_MAX_ATTEMPTS`` times.
    Less the audits the relay answered for (``RESENT_POSTED_KEY``): a relay row settles the audit in
    ``_lost_audits`` only until a later run of it fails, after which the earlier run reads as lost again, and
    a relay that could not tell the job posted the request text and wrote no row. Nothing when the run records
    or the relay's cannot be read: without the relay's record a post whose answer was lost cannot be told from
    a lost one, and a second copy is what the record guards against.
    """
    audits = tuple(oobe.FIRST_RUN_AUDITS)
    runs = _audit_runs(data_dir, audits)
    if not runs:
        return []
    posts = _relay_posts(data_dir)
    if posts is None:
        return []
    resent = _resend_state(data_dir)
    return [
        run
        for run in _lost_audits(audits, runs, _windows(data_dir, state, audits), posts)
        if not run.crashed
        and now - run.claimed_at <= RESEND_WINDOW_SECONDS
        and not resent.get(run.audit, {}).get(RESENT_POSTED_KEY)
        and _resend_tries(resent.get(run.audit, {})) < RESEND_MAX_ATTEMPTS
    ]


def _job_titles(data_dir: Path) -> dict[str, str]:
    """``job id -> name`` from the Platform Agent's roster, the title a scheduled delivery carries; empty when unreadable."""
    jobs = oobe.roster_jobs(data_dir) or []
    return {str(job["id"]): str(job.get("name") or "") for job in jobs if job.get("id")}


def _post_cron_report(audit: str, title: str) -> RelayAnswer:
    """Ask the relay to post ``audit``'s latest report as a scheduled run's delivery leg would.

    A 2xx settles the audit: the relay posted the report, or a notice in its place, and asking again
    would post a notice. A refusal is a try; so is an answer that does not come, which is not a
    failure: the relay may have posted, and its record says so next tick.
    """
    body = {
        REQUEST_JOB_KEY: audit,
        REQUEST_PROFILE_KEY: oobe.PLATFORM_PROFILE,
        REQUEST_TITLE_KEY: title,
        REQUEST_REPORT_KEY: RESEND_FALLBACK_REPORT.format(label=title, request=_request([audit])),
        SCHEDULER_LEG_KEY: [],
    }
    request = urllib.request.Request(
        f"{_findings_endpoint()}{CRON_REPORTS_PATH}",
        data=json.dumps(body).encode("utf-8"),
        headers=_session_kv_headers(),
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=RESEND_TIMEOUT_SECONDS) as response:
            answer = json.loads(response.read().decode("utf-8", "replace") or "{}")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:RELAY_ANSWER_MAX_CHARS]
        return RelayAnswer(f"the relay answered {e.code}: {detail}", False, True)
    except (urllib.error.URLError, OSError, ValueError) as e:
        return RelayAnswer(f"no answer from the relay, which may have posted; its record decides the next tick: {e!r}", False, True)
    status = answer.get("status") if isinstance(answer, dict) else None
    return RelayAnswer(f"the relay answered {status or 'ok'}", True, True)


def resend_lost_summaries(data_dir: Path, now: float) -> None:
    """The ``oobe`` stage that posts the first-run audit summaries the want of a home channel lost.

    A first-run audit that ends before anyone has opened the chat finds no home channel: under
    ``spec.mode: next`` the gateway keeps the post for its digest, the run is recorded failed, and
    nothing posts the summary once the channel is learned minutes later. This stage watches the home
    channel the operator projects into the pod (``chat_notify.home_file``: Slack under next; today mode
    and Google Chat project none, and get no re-send) and, once it names a channel, asks the chat relay
    to post each pending run's report through the route a scheduled run's delivery leg takes, so the
    relay reads the audit's own chat report record, posts it the same way, and records the post. The
    relay's answer settles the audit in ``RESENT_FILE``; for a request whose answer was lost the relay's
    record decides. Each outcome goes to the stage log.
    """
    if not chat_notify.home_file_value():
        return
    pending = pending_resends(data_dir, oobe.read_state(data_dir), now)
    if not pending:
        return
    titles = _job_titles(data_dir)
    for run in pending:
        answer = _post_cron_report(run.audit, titles.get(run.audit) or run.audit)
        _record_resend(data_dir, run.audit, answer.tried, answer.posted)
        _stage_note(data_dir, f"re-sent the {run.audit} summary to the home channel: {answer.outcome}")


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


def _first_report_card(
    platform: str | None, no_ranking: bool | None, files: SweepFiles, suppressed: set[str]
) -> tuple[inventory_card.Card | None, str]:
    """The first report as the card ``inventory_card`` builds from the stage's files, or None and
    why the text goes instead.

    The card needs all of: ``KAGE_SLACK_UX`` on; a card platform as the destination (``platform``:
    Slack, or Google Chat through the gateway under ``spec.mode: next``); a hand-off
    that filed a prioritization card (``no_ranking`` False: otherwise the files beside the report
    are an earlier sweep's, or that cannot be told); the items and shown files there and readable,
    and the scores file readable when an item is listed; and a card the builder accepts, which recomputes
    ``select``'s choice and checks it against the shown file. Built once per run, before the
    claim, for whichever door posts it. Never raises: a report the card cannot carry goes as text.
    """
    if not slack_presenter.enabled():
        return None, CARD_FLAG_OFF
    if not _card_platform(platform):
        return None, CARD_PLATFORM_TAKES_TEXT.format(platform=platform or CARD_PLATFORM_UNRECORDED)
    if no_ranking is None:
        return None, CARD_MARKER_UNREAD
    if no_ranking:
        return None, CARD_RANKING_MISSING
    for name in (ITEMS_NAME, SHOWN_NAME):
        if name in files.problems:
            return None, CARD_FILE_UNUSABLE.format(name=name, problem=files.problems[name])
    for name, present in ((ITEMS_NAME, files.items), (SHOWN_NAME, files.shown)):
        if present is None:
            return None, CARD_FILE_MISSING.format(name=name)
    # The builder reads the scores file only for a listed item, as ``_register_findings`` does, so
    # one that cannot be read beside an empty item list (a clean fleet writes none) is no reason to
    # hold the done card back.
    item_list = files.items.get(ITEMS_KEY)
    if SCORES_NAME in files.problems and isinstance(item_list, list) and item_list:
        return None, CARD_FILE_UNUSABLE.format(name=SCORES_NAME, problem=files.problems[SCORES_NAME])
    try:
        shown = _parse_shown(files.shown)
    except ValueError as e:
        return None, CARD_FILE_UNUSABLE.format(name=SHOWN_NAME, problem=e)
    try:
        return inventory_card.build(files.items, files.scores, files.limits, shown, frozenset(suppressed)), ""
    except inventory_card.CardUnavailable as e:
        return None, str(e)
    except Exception as e:  # noqa: BLE001 - the text must still go
        return None, CARD_BUILD_FAILED.format(error=e)


def _posted_as_blocks(data_dir: Path, card: inventory_card.Card | None) -> bool:
    """Whether the report was posted as the card, to Slack or to Google Chat through
    the gateway under ``spec.mode: next``; False posts nothing.

    False, and the caller prints the text, with no card (``_first_report_card``
    said why), an origin that is not a card platform with a chat id, or no relay. Any
    failure is False too, including one that may have posted (a timeout once
    the request was sent): this report is sent once per install, and a second
    copy of it is a smaller loss than none. Each goes to the stage log.
    """
    if card is None:
        return False
    try:
        origin = _origin()
        channel = str(origin.get(CHAT_ID_KEY) or "")
        thread = str(origin.get(THREAD_ID_KEY) or "")
        platform = str(origin.get(PLATFORM_KEY) or "")
        if not _card_platform(platform) or not channel:
            _stage_note(data_dir, CARD_UNPOSTED.format(reason=CARD_CHAT_ID_MISSING))
            return False
        if chat_notify.routes(platform):
            return _posted_through_gateway(data_dir, card, platform, channel, thread)
        if platform != PRESENTED_PLATFORM:
            # Google Chat's card goes only through the gateway; the relay below is Slack's.
            _stage_note(data_dir, CARD_UNPOSTED.format(reason=CARD_HOME_UNROUTED.format(platform=platform)))
            return False
        import slack_blocks_post

        if not slack_blocks_post.configured():
            _stage_note(data_dir, CARD_UNPOSTED.format(reason=CARD_RELAY_MISSING))
            return False
        blocks, text = inventory_card.blocks(card)
        try:
            slack_blocks_post.post(channel, text, blocks, thread)
        except slack_blocks_post.Refused as e:
            _stage_note(data_dir, CARD_UNPOSTED.format(reason=CARD_REFUSED.format(error=e)))
            return False
        return True
    except Exception as e:  # noqa: BLE001 - the text must still go
        _stage_note(data_dir, CARD_UNPOSTED.format(reason=CARD_POST_FAILED.format(error=e)))
    return False


def _posted_through_gateway(data_dir: Path, card: inventory_card.Card, platform: str, channel: str, thread: str) -> bool:
    """Under ``spec.mode: next``, whether the card posted as a chat message through the gateway.

    The gateway holds the chat platform there, Slack or Google Chat, and the
    broker's relay is gone, so the card goes as ``a2a notify --chat-file``
    (``inventory_card.chat``) into the origin's thread, which the gateway posts
    only when it is a thread of its home channel. A Slack thread goes
    channel-qualified, as chat_notify's send does; a Chat thread is a resource
    name that names its space already. False, and the caller prints the text, with no thread to
    reply on (a bare post would land in the home channel, wherever the origin
    is) or any failure, including one that may have posted, for the reason
    ``_posted_as_blocks`` gives.
    """
    if not thread:
        _stage_note(data_dir, CARD_UNPOSTED.format(reason=CARD_THREAD_MISSING))
        return False
    chat, text = inventory_card.chat(card)
    path = ""
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            json.dump(chat, handle)
            path = handle.name
        res = subprocess.run(
            chat_notify.chat_command(
                platform, f"{channel}/{thread}" if platform == PRESENTED_PLATFORM else thread, text, path,
                chat_notify.NOTIFY_WAIT_SECONDS,
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
        _stage_note(data_dir, CARD_UNPOSTED.format(reason=CARD_GATEWAY_REFUSED.format(error=res.stderr.strip())))
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
                return NOT_POSTED, f"the gateway posted nothing for now (its route absent, or the record unreadable): {(res.stderr or '').strip()}"
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
                          mark_shown, card: inventory_card.Card | None) -> tuple[str, str, bool]:
    """Post the report and the audits note: the card then the note, or one text message.

    ``card`` is ``_first_report_card``'s for this conversation, None for the
    text. On the card path anything short of posted is returned as
    ``NOT_POSTED``, since the next attempt's key edits a card that landed. Once
    the claim is kept, the outcome is recorded, then the findings are marked
    shown (``mark_shown``, whose result is returned), both before the card
    path's note goes, since the outcome ends the findings nudge's hold. A note
    that fails is logged: the report is out.
    """
    if card is None:
        outcome, reason = _notify_conversation(conversation, _with_note(_presented(content, conversation["platform"]), note))
        if outcome == NOT_POSTED:
            return outcome, reason, False
        _record_outcome(data_dir, outcome)
        return outcome, reason, mark_shown()
    chat, text = inventory_card.chat(card)
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
                             note: str, suppressed: set[str], files: SweepFiles,
                             card: inventory_card.Card | None) -> bool:
    """After the claim: post into the conversation, and release the claim unless the report is out.

    True when the claim is kept. The findings are marked shown only once it is
    (posted, or on the text path most likely posted), so a released claim
    leaves none counted against the day's limit.
    """
    outcome, reason, shown = _post_to_conversation(
        data_dir, conversation, content, note, lambda: _mark_shown(files, suppressed), card
    )
    if outcome == NOT_POSTED:
        # Recorded first, so the released claim is retried only after the wait.
        _record_attempt(data_dir, reason, counted=True)
        _release_claim(data_dir)
        return False
    if outcome != POSTED:
        _record_attempt(data_dir, reason, counted=False)
    _cleanup(data_dir, in_sandbox, shown)
    return True


def _ledger_repo() -> str | None:
    """Where the report is filed: the GitOps repository when it is on GitHub, else the install's one
    managed GitHub repository (so a GitOps repository on another forge still files into that one, as
    the chat-delivery ledger does); several GitHub repositories with no GitOps role among them is a
    recorded reason, not a guess; none on GitHub files nothing."""
    configured = os.environ.get(REPORT_LEDGER_REPO_ENV, "").strip()
    if configured:
        return configured
    repos = sorted(gitops_workspace.get_managed_github_repo_names())
    gitops = gitops_workspace.get_gitops_repo()
    if gitops in repos:
        return gitops
    if len(repos) > 1:
        raise LedgerRepoAmbiguous(len(repos))
    return repos[0] if repos else None


class LedgerRepoAmbiguous(RuntimeError):
    def __init__(self, count: int) -> None:
        super().__init__(LEDGER_REPO_AMBIGUOUS_REASON.format(count=count))
        self.count = count


def _filed_record(data_dir: Path) -> dict:
    try:
        record = json.loads((data_dir / REPORT_FILED_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return record if isinstance(record, dict) else {}


def _record_filed(data_dir: Path, repo: str, number: int, url: str) -> None:
    target = data_dir / REPORT_FILED_FILE
    tmp = target.with_name(target.name + ".tmp")
    try:
        tmp.write_text(
            json.dumps({FILED_REPO: repo, FILED_NUMBER: number, FILED_URL: url, FILED_AT: time.time()}, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
        tmp.replace(target)
    except OSError as e:
        sys.stderr.write(f"bootstrap_delivery: could not record the filed report: {e}\n")


def _utc(stamp: float) -> str:
    return datetime.fromtimestamp(stamp, timezone.utc).strftime(REPORT_ISSUE_DATE_FMT)


def _issue_url(repo: str, number: int, issue: dict) -> str:
    """The issue's web address: the broker's, or built from the slug when the answer carries none."""
    for key in ISSUE_URL_KEYS:
        value = issue.get(key)
        if isinstance(value, str) and value.startswith(URL_SCHEME_PREFIX):
            return value
    host, slash, rest = repo.partition("/")
    if slash and "/" in rest:  # host/owner/name
        return ISSUE_URL_FORMAT.format(host=host, slug=rest, number=number)
    return ISSUE_URL_FORMAT.format(host=ISSUE_URL_DEFAULT_HOST, slug=repo, number=number)


def _existing_report_issue(repo: str, sweep: str) -> tuple[int, str, object] | None:
    """The open issue a run before this one filed this sweep's report as, by label and the sweep
    marker, or None; another install's issue in a shared repository names another sweep.

    An entry without a usable number is skipped rather than raised on: the listing is the
    broker's, and one odd row must not hold the report.
    """
    answer = forge.call(
        "issue-list", {"labels": [REPORT_ISSUE_LABEL], "state": "open", "limit": REPORT_ISSUE_LIST_LIMIT}, repo
    )
    marker = REPORT_ISSUE_SWEEP_MARKER.format(sweep=sweep)
    issues = list(answer.get("issues") or [])
    if not issues:
        # An earlier run's issue may carry no label (the repository refused it): one more
        # listing without the label filter, by the sweep marker alone.
        answer = forge.call("issue-list", {"state": "open", "limit": REPORT_ISSUE_LIST_LIMIT}, repo)
        issues = list(answer.get("issues") or [])
    found: list[tuple[int, dict]] = []
    for issue in issues:
        if not isinstance(issue, dict) or marker not in str(issue.get("body") or ""):
            continue
        try:
            found.append((int(issue.get("number")), issue))
        except (TypeError, ValueError):
            continue
    if not found:
        return None
    number, issue = max(found, key=lambda pair: pair[0])
    return number, _issue_url(repo, number, issue), issue.get("labels")


def _issue_text(body: str, sweep: str, filed_at: float, now: float) -> str:
    """The issue body as filed: the two markers, the intro, a blank line, the report with its note."""
    date, filed = _utc(now), _utc(filed_at)
    return "\n".join(
        [REPORT_ISSUE_MARKER, REPORT_ISSUE_SWEEP_MARKER.format(sweep=sweep), REPORT_ISSUE_INTRO.format(date=date, filed=filed), "", body]
    )


def _issue_labelled(labels: object) -> bool:
    """Whether a broker answer's label list carries the report label, in either shape the forge uses."""
    names = []
    for item in labels if isinstance(labels, list) else []:
        names.append(item.get("name") if isinstance(item, dict) else item)
    return REPORT_ISSUE_LABEL in names


def _file_report_issue(repo: str, body: str, sweep: str, filed_at: float, now: float) -> tuple[int, str, object]:
    """Open the ledger issue holding the report (or reuse the one already open): its number, URL and labels.

    Raises ``forge.ForgeError`` when the broker refuses. The label is best effort: the issue is
    worth opening without it, and a repository whose labels this credential may not touch is not
    a reason to hold the report.
    """
    existing = _existing_report_issue(repo, sweep)
    if existing is not None:
        return existing
    with contextlib.suppress(forge.ForgeError):
        forge.call(
            "label-ensure",
            {"name": REPORT_ISSUE_LABEL, "color": REPORT_ISSUE_LABEL_COLOR, "description": REPORT_ISSUE_LABEL_DESCRIPTION},
            repo,
        )
    title = REPORT_ISSUE_TITLE.format(date=_utc(now))
    text = _issue_text(body, sweep, filed_at, now)
    answer = forge.call("issue-create", {"title": title, "body": text, "labels": [REPORT_ISSUE_LABEL]}, repo)
    issue = answer.get("issue") if isinstance(answer.get("issue"), dict) else {}
    try:
        number = int(issue.get("number") or 0)
    except (TypeError, ValueError):
        number = 0
    if not number:
        raise forge.ForgeError(forge.REASON_UNREACHABLE, "issue-create answered with no issue number")
    return number, _issue_url(repo, number, issue), issue.get("labels")


def _post_home_line(data_dir: Path, url: str, now: float) -> None:
    """One line naming the filed report, to the platform whose home posts route through the gateway.

    After the claim and the issue, so best effort: the issue is the record. ``NOTIFY_KEPT`` is
    the gateway holding the line for its digest, which is where a channel set later reads it.
    """
    platform = chat_notify.routed_platform()
    if not platform:
        return
    text = REPORT_ISSUE_HOME_POST.format(url=url, date=_utc(now))
    argv = chat_notify.command(platform, text, wait_seconds=chat_notify.NOTIFY_WAIT_SECONDS)
    # A route that is not there right now (a gateway roll) is waited out as
    # the conversation path waits it out; one that stays away is a log line.
    for delay in (*chat_notify.NOTIFY_ROUTE_RETRY_DELAYS_SECONDS, None):
        try:
            res = subprocess.run(
                argv, capture_output=True, text=True, stdin=subprocess.DEVNULL,
                timeout=chat_notify.NOTIFY_SUBPROCESS_TIMEOUT_SECONDS, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            _stage_note(data_dir, REPORT_HOME_LINE_NOTE.format(outcome=HOME_LINE_FAILED.format(code=HOME_LINE_NO_EXIT, detail=e)))
            return
        if not chat_notify.route_unavailable(res.returncode) or delay is None:
            break
        time.sleep(delay)
    if res.returncode == 0:
        outcome = HOME_LINE_POSTED
    elif chat_notify.kept(res.returncode):
        outcome = HOME_LINE_KEPT
    else:
        detail = _gateway_error(res.stdout) or (res.stderr or "").strip()
        outcome = HOME_LINE_FAILED.format(code=res.returncode, detail=detail)
    _stage_note(data_dir, REPORT_HOME_LINE_NOTE.format(outcome=outcome))


def _retire_undelivered(data_dir: Path, in_sandbox: bool) -> int:
    """Claim the report without posting it and set it aside; the later run's retire removes the jobs.

    Its findings still go into the queue first, as on the other paths, so the nudge brings them up
    later: nobody saw this report.
    """
    try:
        files = _read_sweep_files(data_dir, in_sandbox)
    except (sandbox_exec.SandboxUnavailable, subprocess.TimeoutExpired) as e:
        sys.stderr.write(f"bootstrap_delivery: the shell sandbox did not answer: {e}\n")
        return 0
    _register_findings(data_dir, _no_ranking(data_dir), files)
    if not _claim_for_this_path(data_dir):
        return 0
    if _someone_is_here(data_dir):
        # A human arrived during the reads above (a today platform has no sweep-age guard): their
        # thread takes the report, late as it is.
        _release_claim(data_dir)
        return 0
    try:
        (data_dir / REPORT_RETIRED_FILE).write_text(f"{time.time()}\n", encoding="utf-8")
    except OSError as e:
        sys.stderr.write(f"bootstrap_delivery: could not record the retire: {e}\n")
    _stage_note(data_dir, REPORT_RETIRED_NOTE.format(days=UNDELIVERED_RETIRE_SECONDS // SECONDS_PER_DAY))
    _cleanup(data_dir, in_sandbox)
    return 0


def _file_without_thread(data_dir: Path, in_sandbox: bool, sweep: str, filed_at: float, now: float) -> int:
    """File the report as a ledger issue, after the claim, and say so in the home channel.

    The reads come before the claim as on the other paths, so a sandbox that stopped answering
    leaves no claim to undo. A refused create releases the claim and counts an attempt, with the
    conversation path's waits between tries; past its last, the report waits for the retire.
    """
    record = _read_attempts(data_dir, REPORT_ATTEMPTS_FILE)
    count = _attempt_count(record)
    if count >= CONVERSATION_MAX_ATTEMPTS:
        return 0
    if count:
        try:
            last = float(record.get(ATTEMPTS_AT))
        except (TypeError, ValueError):
            last = 0.0
        if now - last < CONVERSATION_RETRY_WAITS_SECONDS[count - 1]:
            return 0
    try:
        repo = _ledger_repo()
    except LedgerRepoAmbiguous as e:
        _record_attempt(data_dir, str(e), counted=False, name=REPORT_ATTEMPTS_FILE)
        return 0
    except Exception as e:  # noqa: BLE001 - an unreadable mount is a recorded reason, not a tick-long traceback
        _record_attempt(data_dir, LEDGER_REPO_UNREADABLE_REASON.format(error=e), counted=False, name=REPORT_ATTEMPTS_FILE)
        return 0
    if repo is None:
        _record_attempt(data_dir, NO_LEDGER_REPO_REASON, counted=False, name=REPORT_ATTEMPTS_FILE)
        return 0

    try:
        raw = _read_report(data_dir, in_sandbox)
    except (sandbox_exec.SandboxUnavailable, subprocess.TimeoutExpired) as e:
        sys.stderr.write(f"bootstrap_delivery: the shell sandbox did not answer: {e}\n")
        return 0
    except (OSError, sandbox_exec.SandboxMisconfigured, sandbox_exec.SandboxReadFailed) as e:
        sys.stderr.write(f"bootstrap_delivery: could not read INVENTORY.md: {e}\n")
        return 1
    if raw is None:
        return 0  # the report is not written yet
    if len(raw) > REPORT_MAX_BYTES:
        sys.stderr.write(f"bootstrap_delivery: INVENTORY.md is larger than {REPORT_MAX_BYTES} bytes; not filing it\n")
        return 1
    content = raw.decode("utf-8", errors="replace")
    # Sized as filed (markers, intro, report, note), before the second sandbox read and the
    # findings registration; counted, so a report that will never fit stops after the waits.
    no_ranking = _no_ranking(data_dir)
    note = _safe_audits_note(data_dir, no_ranking is True, bool(chat_notify.routed_platform()))
    body = _with_note(content, note)
    composed = len(_issue_text(body, sweep, filed_at, now))
    if composed > ISSUE_BODY_MAX_CHARS:
        _record_attempt(
            data_dir, REPORT_TOO_LONG_REASON.format(chars=composed, limit=ISSUE_BODY_MAX_CHARS), counted=True,
            name=REPORT_ATTEMPTS_FILE,
        )
        return 0
    try:
        files = _read_sweep_files(data_dir, in_sandbox)
    except (sandbox_exec.SandboxUnavailable, subprocess.TimeoutExpired) as e:
        sys.stderr.write(f"bootstrap_delivery: the shell sandbox did not answer: {e}\n")
        return 0
    suppressed = _register_findings(data_dir, no_ranking, files)

    if not _claim_for_this_path(data_dir):
        return 0
    if _someone_is_here(data_dir):
        # A human arrived during the reads above: the report is theirs, through their thread.
        _release_claim(data_dir)
        return 0
    # Anything that escapes between the claim and the record releases the claim; a run that dies
    # holding it is released by the stale-claim rule, since ``delivery_settled`` waits for the record.
    try:
        number, url, labels = _file_report_issue(repo, body, sweep, filed_at, now)
    except forge.ForgeError as e:
        _record_attempt(data_dir, f"issue-create on {repo} refused: {e.reason}: {e.value}", counted=True, name=REPORT_ATTEMPTS_FILE)
        _release_claim(data_dir)
        return 0
    except Exception as e:  # noqa: BLE001 - see above; the next tick retries after the wait
        _record_attempt(data_dir, REPORT_FILING_FAILED_REASON.format(repo=repo, error=e), counted=True, name=REPORT_ATTEMPTS_FILE)
        _release_claim(data_dir)
        return 0
    ref = f"{repo}#{number}"
    _record_filed(data_dir, repo, number, url)
    _stage_note(data_dir, REPORT_FILED_NOTE.format(ref=ref))
    if not _issue_labelled(labels):
        _stage_note(data_dir, REPORT_UNLABELLED_NOTE.format(ref=ref))
    shown = _mark_shown(files, suppressed)
    _cleanup(data_dir, in_sandbox, shown)
    _post_home_line(data_dir, url, now)
    return 0


def _someone_is_here(data_dir: Path) -> bool:
    """Whether a human has reached the install, whatever the greeting hold says.

    The plugin binds the conversation and writes its hold before the aligned marker, and drops
    the hold only once the greeting is on its way; ``_awaiting_delivery`` is False through all of
    that. None of it is "nobody": the report belongs to that person's thread.
    """
    return (
        (data_dir / ".user_aligned").exists()
        or (data_dir / CONVERSATION_ALIGNED_MARKER).exists()
        or _bound_conversation(data_dir) is not None
        or _greeting_pending(data_dir)
    )


def _deliver_without_thread(data_dir: Path) -> int:
    """The path for a report nobody has aligned to: silent until the wait, then filed, then retired.

    Counted from when the sweep was filed, the one stamp the plugin's own age rule reads too.
    """
    if _someone_is_here(data_dir):
        return 0  # a human's thread takes the report once the greeting is out
    filed = oobe.scan_filed(data_dir)
    if filed is None:
        return 0  # the scan has not started
    now = time.time()
    age = now - filed[1]
    if age < UNSPOKEN_FILE_AFTER_SECONDS:
        return 0  # a human may still open the thread the report was written for
    try:
        in_sandbox = sandbox_exec.sandbox_enabled()
    except sandbox_exec.SandboxMisconfigured as e:
        sys.stderr.write(f"bootstrap_delivery: cannot tell where INVENTORY.md is: {e}\n")
        return 1
    if age >= UNDELIVERED_RETIRE_SECONDS:
        return _retire_undelivered(data_dir, in_sandbox)
    return _file_without_thread(data_dir, in_sandbox, filed[0], filed[1], now)


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
        return _deliver_without_thread(data_dir)  # nobody to deliver to yet, or ever
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

    # The files beside the report, read before the claim like the report, so a
    # sandbox that stopped answering leaves no claim behind to undo.
    try:
        files = _read_sweep_files(data_dir, in_sandbox)
    except (sandbox_exec.SandboxUnavailable, subprocess.TimeoutExpired) as e:
        sys.stderr.write(f"bootstrap_delivery: the shell sandbox did not answer: {e}\n")
        return 0
    # Before the claim, which ends the nudge's hold, so the rows exist when
    # the marks below are sent.
    no_ranking = _no_ranking(data_dir)
    suppressed = _register_findings(data_dir, no_ranking, files)
    # One card for whichever door posts it, from the files read above; built
    # here, before the claim, since it reads nothing more.
    platform = conversation["platform"] if conversation is not None else _origin_platform()
    card, text_reason = _first_report_card(platform, no_ranking, files, suppressed)
    # Also before the claim, so its reads cannot cost the report once the claim is taken.
    home = bool(chat_notify.routed_platform()) if conversation is not None else True
    note = _safe_audits_note(data_dir, no_ranking is True, home)

    # The cheap check above is advisory; this is the decision. Nothing may be
    # written to stdout before it succeeds.
    if not _claim_delivery(data_dir):
        return 0  # another run is delivering this report — stay silent

    if conversation is not None:
        kept = _deliver_to_conversation(data_dir, in_sandbox, conversation, content, note, suppressed, files, card)
    else:
        # The origin path keeps every claim: stdout is the scheduler's.
        kept = True
        # Right after the claim, which ends the nudge's hold, so a nudge run has
        # the shortest window in which to announce these findings as new.
        shown = _mark_shown(files, suppressed)

        if _posted_as_blocks(data_dir, card):
            sys.stdout.write(note + "\n")
        else:
            sys.stdout.write(_with_note(_presented(content, platform), note))
        sys.stdout.flush()

        # Cleanup runs only after the report is posted or safely on stdout (already
        # captured by the scheduler), so removing INVENTORY.md here cannot truncate delivery.
        _cleanup(data_dir, in_sandbox, shown)

    # Logged by the run that keeps the claim, so a text post that failed and
    # released it leaves no line; the attempt that posts writes the one.
    if kept and card is None:
        _stage_note(data_dir, TEXT_REPORT_NOTE.format(reason=text_reason))
    return 0


if __name__ == "__main__":
    sys.exit(main())
