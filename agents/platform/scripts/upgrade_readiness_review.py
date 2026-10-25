#!/usr/bin/env python3
"""upgrade_readiness_review.py - the weekly upgrade readiness review: every
cluster graded, every finding registered on the findings queue, one technical
report written, nothing said in chat.

The daily ``upgrade-readiness-watch`` reads a cluster only while a release
channel offers it a version it is below, and says one line about the report it
wrote. This job runs once a week, on Saturday, over every cluster in the
install's scope whatever its version, and feeds what the readiness report
finds into the findings queue (``findings_queue.py``, the store behind the
``register_findings`` tool and the hourly ``findings-morning-nudge``). The
queue is what carries a finding to a decision: it names new criticals in chat
at the queue's pace, reminds the user once a day of the criticals nobody has
decided on, keeps a dismissed finding dismissed when the next week finds it
again, brings a snoozed one back on its date, and lets an accepted one stop
asking. So this job registers and writes; the queue reminds; and the Saturday
``upgrade-readiness-weekly-brief`` job, an agent turn, reads what this job
wrote and the queue's state and composes the one weekly message a person
reads. The split keeps the deterministic half out of a model turn and the
model turn out of the path that makes a finding durable.

What a finding is. One per blocker or risk the readiness report names on one
cluster: a drain-blocking PodDisruptionBudget, a fail-closed admission webhook
(in the upgrade's path, or already refusing what it matches), a node pool past
or at the skew ceiling, a maintenance exclusion covering the upgrade, and
every readiness rule's blocker or risk when the report carries rules
(``readiness.rule_blocking`` and ``readiness.risks``, keyed by catalogue entry).
Its identity is the queue's: ``(check, project, cluster, namespace, object)``,
with the check ``upgrade-<kind>``, so the same budget found next
Saturday updates one row rather than adding a second. Its severity is the
queue's rubric applied by kind and catalogue entry (``RUBRIC`` below), never a
judgement made here: a blocker that would take a serving workload down or
refuse the control-plane move lands critical, a dated risk lands major, a
posture lands minor. The recommendation is the catalogue entry's "mitigate
before" line and the rule's own text; remediation is ``manual`` because this
job does not know the manifest that declares the object.

Where it runs. On the agent pod, from the Platform Agent's roster, as the
daily watch does: the readiness report itself runs in the shell sandbox
through the watch's own loader (``upgrade_readiness_watch.run_report``), one
run per roster project (a declared scope refuses any project outside it), and
its JSON comes back here. Nothing
the model can write to is on that path.

What it writes. Under ``<agent home>/upgrade-readiness/weekly/``: one
directory per run, ``<stamp>/report.md`` (the technical report: verdict per
cluster, every finding with its id, severity, evidence and action, then the
report script's own tables), ``<stamp>/report.json`` (the merged report),
``<stamp>/findings.json`` (what was registered and the queue's answer), and
``latest.md`` / ``latest.json`` (the newest report and a summary the brief
reads: counts, ids by outcome, what is new since the previous run and what the
previous run named that this one did not). The newest ``KEEP_RUNS`` runs are
kept. A copy of ``latest.md`` and ``latest.json`` is placed in the sandbox at
``SANDBOX_COPY_DIR`` by the terminal login, so the agent can read the report
when a user asks for it; the copy is a copy, the record is on this side.

Stdout is the chat message (``deliver: "chat"``) and is empty on success: the
brief speaks for the week, and the queue speaks for every finding. A run that
read some projects and not others prints one line naming the projects it did
not read, since the brief's counts cannot show them; a run whose findings the
queue refused, or whose sandbox copy failed, prints one line for each, since
both leave the week where the brief cannot act on it; a run that fails prints
one line saying so, once per distinct failure, and the next clean run prints
the recovery line; the exit is 0 throughout, and 1 only when the record could
not be written, a failure said every time it happens, since the marker that
holds a failure back sits beside the record it could not write. ``--dry-run`` runs the
reports and prints the summary without registering or writing anything.

``UPGRADE_READINESS_PROJECTS``, ``UPGRADE_READINESS_WATCH_HOME``,
``SESSION_KV_ENDPOINT`` and ``SESSION_KV_API_KEY`` are read as the watch and the
nudge read them.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import http.client
import json
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import sandbox_exec  # noqa: E402
import findings_queue  # noqa: E402
import upgrade_readiness_watch as watch  # noqa: E402

JOB_ID = "upgrade-readiness-weekly-review"
BRIEF_JOB_ID = "upgrade-readiness-weekly-brief"
LOG_PREFIX = "upgrade readiness weekly review"

# Where the record lives, under the watch's home (`<agent home>/upgrade-readiness/`).
WEEKLY_DIR_NAME = "weekly"
# The summary's cluster buckets beside the watch's three verdicts.
REVIEWED_KEY = "reviewed"
UNREAD_KEY = "unread"
REPORT_MD_NAME = "report.md"
REPORT_JSON_NAME = "report.json"
FINDINGS_JSON_NAME = "findings.json"
LATEST_MD_NAME = "latest.md"
LATEST_JSON_NAME = "latest.json"
FAILURE_MARKER_NAME = "last-failure.txt"
KEEP_RUNS = 8
STAMP_FORMAT = "%Y%m%dT%H%M%SZ"

# The copy the agent can read, on the sandbox volume, written by the terminal login.
SANDBOX_COPY_DIR = "/opt/data/upgrade-readiness/weekly"
SANDBOX_COPY_TIMEOUT_SECONDS = 60
SANDBOX_COPY_MODE = 0o444  # the files: the agent reads them, only the review rewrites them
SANDBOX_COPY_DIR_OPEN_MODE = 0o755  # while the review writes
SANDBOX_COPY_DIR_CLOSED_MODE = 0o555  # between reviews: no new file, no replacement

# The findings queue, as findings_nudge.py reaches it.
KV_ENDPOINT_ENV = "SESSION_KV_ENDPOINT"
KV_AUTH_VARIABLE = "SESSION_KV_API_KEY"  # the name of the variable the bearer token is read from
DEFAULT_KV_ENDPOINT = "http://127.0.0.1:8699"
KV_FINDINGS_PATH = "/v1/findings"
KV_TIMEOUT_SECONDS = 30
# The queue answers 503 only when it wrote nothing (its lock wait ran out, or its
# transaction rolled back), and says the same body may be sent again: once, after
# this pause, while the run's time allows it.
HTTP_BUSY = 503
BUSY_RETRY_SECONDS = 5
KV_ERROR_EXCERPT_CHARS = 300  # of the queue's refusal body, kept in the problem line
KV_BATCH_LIMIT = findings_queue.MAX_BATCH  # one POST /v1/findings holds this many rows

SOURCE = "upgrade-readiness"  # the review's own writer name in the queue (findings_queue.SOURCES)
CHECK_PREFIX = "upgrade-"
CHECK_BUDGET = CHECK_PREFIX + "drain-blocking-budget"
CHECK_EXCLUSION = CHECK_PREFIX + "maintenance-exclusion"
CHECK_SKEW = CHECK_PREFIX + "pool-skew"
CHECK_WEBHOOK = CHECK_PREFIX + "fail-closed-webhook"

# Report keys the mapping reads (the fleet-upgrade-verification report's own names).
PDBS_KEY = "pdbs"
MAINTENANCE_KEY = "maintenance"
SKEW_KEY = "skew"
WEBHOOKS_KEY = "webhooks"
RULE_BLOCKING_KEY = "rule_blocking"
RISKS_KEY = "risks"
RULES_KEY = "rules"
BLOCKING_KEY = "blocking"
OUTAGE_KEY = "outage"
EXCLUSIONS_KEY = "exclusions"
POOLS_KEY = "pools"
READ_ERROR_KEYS = ("read_error", "webhook_read_error")
# What the grader could not decide: a pool whose version did not parse (or every pool,
# with no target), an exclusion with an unparsable time or scope, a rule that failed.
SKEW_UNKNOWN_KEY = "unknown"
UNDECIDED_EXCLUSIONS_KEY = "undecided_exclusions"
RULES_UNKNOWN_KEY = "unknown"
DEFAULT_WEBHOOK_CONFIG_KIND = "webhookconfiguration"
# The member a finding came from, carried beside the payload (stripped before
# registration) and written into the summary's rows: cluster names repeat across
# locations, so project and cluster alone do not name a member.
MEMBER_FIELD = "_member"
MEMBERS_FIELD = "_members"  # every member a folded finding stands for
ROW_MEMBER_KEY = "member"
ROW_MEMBERS_KEY = "members"
SKEW_VERDICT_BLOCKS = "blocks"
SKEW_VERDICT_CEILING = "at ceiling"
WORKLOAD_REPLICAS_KEY = "replicas"
WORKLOADS_KEY = "workloads"
NAME_KEY = "name"
PDB_KEY = "pdb"
NAMESPACE_KEY = "namespace"
KIND_KEY = "kind"
FIELD_KEY = "field"
DISRUPTIONS_ALLOWED_KEY = "disruptions_allowed"
BLOCKS_KEY = "blocks"
SCOPE_KEY = "scope"
END_TIME_KEY = "end_time"
DETAIL_KEY = "detail"
VERDICT_KEY = "verdict"
VERSION_KEY = "version"
CONFIG_KEY = "config"
CONFIG_KIND_KEY = "config_kind"
WEBHOOK_KEY = "webhook"
LIVE_RULES_KEY = "live_rules"
UPGRADE_PATH_KEY = "upgrade_path"
REASON_KEY = "reason"
SERVICE_KEY = "service"
RULE_KEY = "rule"
ENTRY_KEY = "entry"
OBJECT_KEY = "object"
POOL_KEY = "pool"
TEXT_KEY = "text"
SUMMARY_KEY = "summary"

# The per-run budget: the watch's readiness timeout per project, the watch's
# whole-tick budget for the reads (Hermes kills a no_agent script at an hour, as
# the watch records), and a floor under which a project is not started. Each
# project is registered as soon as it returns, so a run the hour cuts short
# keeps what it registered. Registration has its own share past the reads, and
# a batch it cannot send by then is booked as not sent, so a queue that hangs
# cannot hold the run past the hour before the record is written at the end.
PROJECT_TIMEOUT_SECONDS = watch.READINESS_TIMEOUT_SECONDS
MIN_PROJECT_RUN_SECONDS = 120
TOTAL_BUDGET_SECONDS = watch.TICK_BUDGET_SECONDS
REGISTRATION_GRACE_SECONDS = 600
REGISTRATION_OUT_OF_TIME_DETAIL = "not sent: the run's time for the queue ran out"

# The rubric (findings_queue §4.2: B, L, detect, recover, C percent), by kind.
# Severity falls out of it: `critical` at 150 and above or at the L>=10, B>=3
# floor, `major` from 40, `minor` below.
RUBRIC = {
    "budget-singleton": (5, 6, 3, 2, 100),  # 150: the drain evicts the workload's only pod
    "budget": (3, 6, 3, 2, 100),  # 90: replicas lose one at a time, held an hour each
    "exclusion": (2, 4, 2, 1, 100),  # 24: a posture with an end date
    "skew-blocks": (8, 6, 2, 2, 100),  # 192: GKE refuses the control-plane move
    "skew-ceiling": (8, 4, 2, 2, 100),  # 128: the next minor is refused until the pool moves
    "webhook-blocking": (8, 6, 3, 2, 100),  # 240: the drain or the master's own writes are refused
    "webhook-outage": (2, 10, 2, 2, 100),  # 80: refusing what it matches today, inside its scope
    "rule-blocking": (5, 6, 3, 2, 100),  # 150: a rule that says the upgrade cannot proceed
}
# A rule's risk, by catalogue entry: what breaks at the upgrade, how likely,
# how it is noticed and recovered, and how directly the rule observed it.
ENTRY_RISK_RUBRIC = {
    2: (3, 6, 2, 2, 90),
    4: (3, 6, 3, 3, 90),
    6: (3, 4, 3, 2, 90),
    8: (3, 4, 2, 2, 90),
    9: (2, 2, 2, 2, 90),
    10: (2, 4, 2, 2, 90),
    11: (2, 2, 2, 1, 100),
    12: (5, 4, 2, 2, 90),
    13: (3, 4, 2, 2, 60),
    14: (5, 6, 2, 2, 90),
    15: (5, 6, 2, 2, 90),
    16: (3, 2, 3, 2, 90),
    17: (8, 4, 2, 3, 60),
    18: (5, 4, 2, 2, 60),
    19: (5, 4, 2, 3, 90),
    20: (5, 6, 2, 2, 90),
    23: (3, 4, 3, 3, 90),  # 65, major: a capacity failure is noticed when the operation errors and waits for the region
    25: (3, 4, 2, 1, 100),  # 36, minor: the channel's list says so before the window, and the fix is the next active patch
    26: (3, 4, 2, 2, 90),  # 43, major: the issue decides; the match is exact on the add-on and the patch
}
DEFAULT_RISK_RUBRIC = (3, 4, 2, 2, 90)
RUBRIC_KEYS = ("B", "L", "detect", "recover")
CONFIDENCE_KEY = "C"

# The catalogue's entries (docs/designs/upgrade-failure-catalogue.md): the
# title, and the "mitigate before" line, shortened to the first action.
ENTRY_TITLES = {
    1: "A PodDisruptionBudget forbids the eviction",
    2: "No spare capacity for the displaced pods",
    3: "Every replica in one zone or on one node",
    4: "Data on the node is gone",
    5: "Maintenance window too short, or an exclusion ends mid-roll",
    6: "A served API version is removed",
    7: "A fail-closed webhook whose backend is not up",
    8: "A default changes in the new minor",
    9: "A feature is deprecated but still served",
    10: "Add-on and client skew",
    11: "The control plane is unreachable for minutes on a zonal cluster",
    12: "A node label is removed",
    13: "The container runtime changes",
    14: "cgroup v2 under a runtime that cannot read it",
    15: "The OOM killer starts killing the whole container",
    16: "The network dataplane changes",
    17: "A node networking agent fails on the new image",
    18: "GPU driver mismatch",
    19: "In-tree volumes lose their CSI path",
    20: "Images on a retired registry",
    21: "The load balancer's connection drain outlasts the node's deregistration wait",
    22: "A Node object outlives its virtual machine",
    23: "A control-plane upgrade or resize fails on regional compute capacity",
    24: "A cluster held in a long reconcile",
    25: "The target patch is withdrawn before the window",
    26: "A known issue in an add-on at the target version",
}
ENTRY_MITIGATIONS = {
    1: "Give the budget room: maxUnavailable at least 1 or minAvailable below the replica count, and a "
    "second replica so the budget can be honoured; for a true singleton, accept the outage inside a "
    "maintenance window or choose a blue-green upgrade with a soak long enough to fix the budget.",
    2: "Return the pool to GKE's default surge settings: maxSurge at least 1 and maxUnavailable 0, with "
    "one node of headroom and the quota and autoscaler ceiling that allow the extra node.",
    3: "Spread the replicas with topologySpreadConstraints across zones and hosts, or pod anti-affinity, "
    "and a budget so the drain waits between replicas.",
    4: "Keep state on PersistentVolumes or object storage; Local SSD and emptyDir only for what the "
    "application can rebuild.",
    5: "Set a window long enough for node count times drain time, end exclusions outside the planned "
    "change, and use blue-green upgrades where the window is tight.",
    6: "Migrate the callers, not the objects: bump client libraries and kubectl, rewrite manifests to the "
    "new API version, and rewrite Helm release state with helm mapkubeapis.",
    7: "Give the webhook a namespaceSelector that excludes kube-system, a timeout of a few seconds, "
    "failurePolicy Ignore unless it is a security control, two backend replicas behind a budget, and a "
    "valid backend certificate.",
    8: "Read the target minor's notes, run Pod Security Admission in warn and audit before enforce, and "
    "rehearse on a staging cluster already at the target version.",
    9: "Plan the migration while the feature still works: EndpointSlices for Endpoints, a LoadBalancer "
    "Service or the Gateway API for externalIPs.",
    10: "Upgrade add-ons to a version whose matrix includes the target before the cluster moves, keep "
    "kubectl within one minor, and keep node pools inside the skew window.",
    11: "Use a regional cluster for anything automation depends on, and retries with backoff in clients "
    "that cannot tolerate a few minutes of 5xx.",
    12: "Replace deprecated labels in selectors with their GA names (kubernetes.io/arch, "
    "topology.kubernetes.io/zone).",
    13: "Move agents to the CRI v1 API, rebuild v1-schema images, and drop containerd 1.x configuration "
    "overrides.",
    14: "Use a runtime that reads cgroup v2 (JDK 8u372 or 11.0.16 and later, or the runtimes the cgroup "
    "v2 page names), or explicit heap flags.",
    15: "Raise the limit for multi-process containers, split workers into their own containers or pods, "
    "or set singleProcessOOMKill in the pool's node system config.",
    16: "Rehearse the target version on a staging cluster with the same dataplane, and keep NetworkPolicy "
    "explicit rather than relying on defaults.",
    17: "Upgrade a canary pool first and watch Service routing from inside the cluster before the rest; "
    "surge upgrades with maxUnavailable 0 so a broken node never takes capacity with it.",
    18: "Match the driver to the images before the upgrade: the GPU how-to page's table gives the driver "
    "per GKE version and NVIDIA's minimum-driver matrix the floor the images accept; canary a GPU pool.",
    19: "Enable the PD CSI driver add-on and move StorageClasses to pd.csi.storage.gke.io.",
    20: "Mirror every image into a registry you own and keep egress allowlists in step.",
    21: "Set the backend service's connection draining timeout at or under the wait: gcloud compute "
    "backend-services update <name> --global|--region <region> --connection-draining-timeout=600.",
    22: "Delete the orphan Node object (kubectl delete node <name>); nothing runs on it.",
    23: "Set a maintenance window in an off-peak hour for the region and retry the upgrade in it; keep "
    "upgrade notifications consumed so a failed attempt is a message the same day.",
    24: "Do not start the upgrade: freeze changes and, past a few hours, open a support case quoting the "
    "operation id, or the status and its message when no operation holds the cluster; re-check after "
    "the status settles.",
    25: "Target the newest active patch of the minor on the channel at or above the version, or the next "
    "active version the channel lists; hold when none is active.",
    26: "Upgrade the matched add-on to the fixed-in version before the window (for a managed add-on, the "
    "GKE patch the issue names as fixed), or hold the cluster at a version the issue does not cover.",
}
# Entries the built-in reads stand for.
ENTRY_BUDGET = 1
ENTRY_EXCLUSION = 5
ENTRY_WEBHOOK = 7
ENTRY_SKEW = 10
DEFAULT_ENTRY_TITLE = "Upgrade readiness rule"
DEFAULT_MITIGATION = "Read the finding's text and the catalogue entry it names, and fix the object before the upgrade."
# Entries whose rule carries its own remedy in the finding text, after the last `; ` outside
# parentheses (the surge-capacity rule's Pod-range and accelerator shapes): the action is that
# clause, not the entry's one-size mitigation, which for entry 2 names the surge settings a
# pool on GKE's defaults already has.
RULE_REMEDY_ENTRIES = frozenset({2})
# A plan-level rule, one with no catalogue entry (readiness_rules' `ENTRY` is None): its row
# takes the rule's own title, keyed by rule id, where an entry's row takes the catalogue
# heading; its action and its remediation note are the remedy clause of its text; and its
# risk is graded by the rule-keyed rubric. The rollout-sequence precheck is one: the host
# project already holds the one RolloutSequence the platform allows, a posture the plan
# adapts to, so its risk is minor. One row per host project per week falls out of the
# identity: the check, the host's first member (where the report files the read) and the
# sequence's name.
RULE_TITLES = {"rollout-sequence-precheck": "A second RolloutSequence in the host project is refused"}
RULE_REMEDY_RULES = frozenset({"rollout-sequence-precheck"})
RULE_RISK_RUBRIC = {
    "rollout-sequence-precheck": (2, 4, 2, 1, 100),  # 8: nothing breaks at the upgrade; the plan takes the existing sequence
}
ENTRY_TITLE_FORMAT = "{entry}. {title}: {subject} {lead}"
PLAN_LEVEL_TITLE_FORMAT = "{title}: {subject} {lead}"
PLAN_LEVEL_RISK_LEAD = "shapes the upgrade plan"
ENTRY_LABEL_FORMAT = "catalogue entry {entry}"
PLAN_LEVEL_ENTRY_LABEL = "a plan-level rule, no catalogue entry"
REMEDY_SEPARATOR = "; "
PAREN_OPEN = "("
PAREN_CLOSE = ")"

REMEDIATION_KIND = "manual"
VERIFICATION_KIND = "manual"
CLUSTER_OBJECT = "Cluster"
WEBHOOK_OBJECT_SEPARATOR = "/"
MAX_TEXT_CHARS = 3900  # under the queue's 4000, with room for the separators
MAX_TITLE_CHARS = 480  # under the queue's 500

CREATED = "created"
UPDATED = "updated"
SUPPRESSED = "suppressed"
REGISTRATION_FAILED = "failed"

SEVERITY_ORDER = ("critical", "major", "minor")
REPORT_TITLE = "Weekly upgrade readiness review"
SUMMARY_VERSION = 1
NO_PROJECTS_DETAIL = "no project to review: set UPGRADE_READINESS_PROJECTS or declare a scope"
BUDGET_EXHAUSTED_DETAIL = "the run's time budget ran out before this project"
CUT_SHORT_PREFIX = "readiness run cut short by the review's time budget"
CUT_SHORT_DETAIL = CUT_SHORT_PREFIX + " after {seconds}s; it goes first next week"
# A budget failure is one the order can repair; a run that failed on its own
# (its full timeout, a refusal, a listing failure) rotates to the back instead.
BUDGET_FAILURE_PREFIXES = (CUT_SHORT_PREFIX,)
LISTING_FAILED_DETAIL = "the project's cluster listing failed"
EXCLUSION_SURROGATE_OBJECT = "exclusion-{digest}"  # an exclusion name with no ASCII letter or digit
EXCLUSION_DIGEST_CHARS = 8
MAX_PROBLEMS_IN_LINE = 3  # of the queue's refusals quoted in the chat line; the record has them all
MAX_NAMES_IN_LINE = 6  # projects named in the partial line
OVERFLOW_NOTE = "and {count} more in the record"
OVERFLOW_NOTE_PLAIN = "and {count} more"  # for the line of a run that wrote no record
CLAIM_FAILED_MARK = "the completeness claim did not reach the queue"
# A 200 answer that does not acknowledge the batch: a list of results naming exactly the rows
# sent, and for a claim the count it lowered. Anything else is not the findings route
# answering this batch (a redirect urllib turned into a GET answers the findings list, a
# replaying front another batch), so nothing in it is counted; the detail names each cause.
UNACKNOWLEDGED_DETAIL = "the queue's answer does not acknowledge the batch ({reasons})"
UNACKNOWLEDGED_NOT_A_LIST = "no list of results"
UNACKNOWLEDGED_MISSING = "{count} of {sent} row(s) without a result"
UNACKNOWLEDGED_EXTRA = "{count} result(s) for row(s) the batch never sent"
UNACKNOWLEDGED_CLAIM = "no count of the rows the claim lowered"
UNACKNOWLEDGED_SEPARATOR = ", "
ANSWER_RESULTS_KEY = "results"
ANSWER_DOWNGRADED_KEY = "downgraded"
DUPLICATE_DETAIL_SEPARATOR = " | also: "  # two rows of one member that derive one queue id
SHARED_DETAIL_SEPARATOR = " | also on {member}: "  # two same-named clusters whose objects coincide
TMP_SUFFIX = watch.LEDGER_TMP_SUFFIX  # a file is written beside its name and renamed over it
FAILED_LINE = "{prefix}: failed: {detail}"
RECOVERED_LINE = "{prefix}: runs again after a failure"
PARTIAL_LINE = "{prefix}: {count} project(s) not read this week: {detail}; their clusters keep last week's findings and are not counted"
REGISTRATION_FAILED_LINE = "{prefix}: {rows} finding(s) and {claims} completeness claim(s) did not reach the findings queue: {detail}; {where}; a cluster whose claim did not land keeps last week's rows at full confidence"
REGISTRATION_IN_REPORT = "the findings are in the report, not on the queue, and the brief says so"
REGISTRATION_NO_RECORD = "the record could not be written either, so this line is the one place they are said"
COPY_FAILED_LINE = "{prefix}: the sandbox copy failed: {detail}; the brief reads the previous copy until the next review"
# A failure is announced once per distinct cause: the marker keeps the failure's text
# with every number replaced, so a timeout that reads 1187s one week and 1203s the
# next is the same failure.
FAILURE_DIGITS = re.compile(r"\d+")
FAILURE_DIGIT_MARK = "#"
PARTIAL_DETAIL_SEPARATOR = "; "
RESOLVED_IDS_SEPARATOR = ", "  # a report table's heading: the ids its report resolved
CLAIM_WITHHELD_DETAIL = "withheld, since {count} row(s) were refused before sending"
DRY_RUN_HEADER = "dry run: nothing registered, nothing written"


# --- time, paths -------------------------------------------------------------


def now_utc() -> datetime:
    return watch.now_utc()


def iso(moment: datetime) -> str:
    return watch.iso(moment)


def weekly_home() -> Path:
    return watch.watch_home() / WEEKLY_DIR_NAME


def failure_marker() -> Path:
    return weekly_home() / FAILURE_MARKER_NAME


# --- the findings queue --------------------------------------------------------


class QueueBusy(OSError):
    """The queue's 503: nothing was written, so the batch may be sent again."""


def kv_endpoint() -> str:
    return (os.environ.get(KV_ENDPOINT_ENV) or DEFAULT_KV_ENDPOINT).rstrip("/")


def kv_request(path: str, body: dict | None = None, timeout: float = KV_TIMEOUT_SECONDS) -> dict:
    """POST ``body`` to the queue, or GET ``path`` when there is no body."""
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = {} if body is None else {"Content-Type": "application/json"}
    token = (os.environ.get(KV_AUTH_VARIABLE) or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    method = "GET" if body is None else "POST"
    req = urllib.request.Request(f"{kv_endpoint()}{path}", data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            answer = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # The queue says which row or field it refused, or that it is busy, in
        # the body; str(HTTPError) is the status line alone.
        try:
            body = " ".join(exc.read().decode("utf-8", "replace").split())[:KV_ERROR_EXCERPT_CHARS]
        except (OSError, http.client.HTTPException):  # the error body itself cut short
            body = ""
        error = QueueBusy if exc.code == HTTP_BUSY else OSError
        raise error(f"HTTP {exc.code} {exc.reason}: {body}" if body else f"HTTP {exc.code} {exc.reason}") from None
    except http.client.HTTPException as exc:
        # A response cut short or malformed is a transport failure of this
        # batch, as the nudge treats it, not the end of the run.
        raise OSError(f"the queue's answer was cut short: {exc.__class__.__name__}: {exc}") from None
    if not isinstance(answer, dict):
        # Something that is not this route answered: a proxy's page, a wrong port.
        raise OSError(f"the queue answered with {type(answer).__name__}, not an object")
    return answer


# --- the mapping: report rows to findings ------------------------------------


def rubric(values: tuple[int, int, int, int, int]) -> dict:
    out = dict(zip(RUBRIC_KEYS, values[:4]))
    out[CONFIDENCE_KEY] = values[4] / 100
    return out


def clip(text: object, limit: int = MAX_TEXT_CHARS) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def entry_title(entry: int | None) -> str:
    return ENTRY_TITLES.get(entry or 0, DEFAULT_ENTRY_TITLE)


def entry_mitigation(entry: int | None) -> str:
    return ENTRY_MITIGATIONS.get(entry or 0, DEFAULT_MITIGATION)


def rule_remedy(text: str) -> str:
    """The remedy clause a rule's text ends with: what follows the last `; ` outside
    parentheses, capitalised; empty when the text carries none."""
    depth, cut = 0, -1
    for index, char in enumerate(text):
        if char == PAREN_OPEN:
            depth += 1
        elif char == PAREN_CLOSE:
            depth = max(0, depth - 1)
        elif depth == 0 and text.startswith(REMEDY_SEPARATOR, index):
            cut = index
    if cut < 0:
        return ""
    clause = text[cut + len(REMEDY_SEPARATOR) :].strip()
    return clause[:1].upper() + clause[1:] if clause else ""


def rule_action(entry: int | None, text: str, rule: str | None = None) -> str:
    """A rule row's action: the rule's own remedy for the entries and the plan-level rules
    that carry one, else the entry's mitigation."""
    if entry in RULE_REMEDY_ENTRIES or rule in RULE_REMEDY_RULES:
        remedy = rule_remedy(text)
        if remedy:
            return remedy
    return entry_mitigation(entry)


def rule_title(rule: str, entry: int | None, subject: str, lead: str) -> str:
    """The row's title: the catalogue heading for an entry, the rule's own for a plan-level
    rule (`DEFAULT_ENTRY_TITLE` for one RULE_TITLES does not name)."""
    if entry is None:
        return PLAN_LEVEL_TITLE_FORMAT.format(title=RULE_TITLES.get(rule, DEFAULT_ENTRY_TITLE), subject=subject, lead=lead)
    return ENTRY_TITLE_FORMAT.format(entry=entry, title=entry_title(entry), subject=subject, lead=lead)


def entry_label(entry: int | None) -> str:
    return ENTRY_LABEL_FORMAT.format(entry=entry) if entry is not None else PLAN_LEVEL_ENTRY_LABEL


def risk_rubric(rule: str, entry: int | None) -> tuple[int, int, int, int, int]:
    """A rule's risk rubric: by rule id for the rules RULE_RISK_RUBRIC names, else by entry."""
    return RULE_RISK_RUBRIC.get(rule) or ENTRY_RISK_RUBRIC.get(entry or 0, DEFAULT_RISK_RUBRIC)


def cluster_fields(member: dict) -> tuple[str, str, str]:
    project, location, cluster = (str(member.get(key) or "") for key in watch.MEMBER_ID_KEYS)
    return project, location, cluster


def finding(
    *,
    member: dict,
    check: str,
    entry: int | None,
    namespace: str,
    object_name: str,
    title: str,
    detail: str,
    rubric_values: tuple[int, int, int, int, int],
    action: str,
    rationale: str,
    verify: str,
    still_failing_when: str,
    mitigation: str | None = None,
) -> dict:
    project, location, cluster = cluster_fields(member)
    where = watch.MEMBER_KEY_SEPARATOR.join((project, location, cluster))
    return {
        "source": SOURCE,
        "check": check,
        "project": project,
        "cluster": cluster,
        "namespace": namespace,
        "object": object_name[: findings_queue.MAX_LABEL_CHARS],
        "title": clip(title, MAX_TITLE_CHARS),
        "detail": clip(detail),
        "rubric": rubric(rubric_values),
        "recommendation": {
            "action": clip(action),
            "rationale": clip(rationale),
            "risk": clip(f"Read-only first, on {where}: {verify}"),
        },
        "remediation": {"kind": REMEDIATION_KIND, "note": clip(entry_mitigation(entry) if mitigation is None else mitigation)},
        "verification": {
            "kind": VERIFICATION_KIND,
            "command": clip(f"On {where}: {verify}"),
            "still_failing_when": clip(still_failing_when),
        },
        # Carried beside the payload for the report; stripped before registration.
        "_entry": entry,
        MEMBER_FIELD: where,
        "_location": location,
    }


def budget_findings(member: dict) -> list[dict]:
    out = []
    pdbs = (member.get(watch.READINESS_KEY) or {}).get(PDBS_KEY) or {}
    for budget in pdbs.get(BLOCKING_KEY) or []:
        if not isinstance(budget, dict):
            continue
        workloads = [w for w in (budget.get(WORKLOADS_KEY) or []) if isinstance(w, dict)]
        # A workload scaled to zero has no pod the drain can evict; the budget
        # is a singleton's when a running workload has one replica, or when
        # the report names no workload at all.
        running = [w for w in workloads if (w.get(WORKLOAD_REPLICAS_KEY) or 0) > 0]
        singleton = any((w.get(WORKLOAD_REPLICAS_KEY) or 0) <= 1 for w in running) or not running
        owners = ", ".join(f"{w.get(KIND_KEY)} {w.get(NAMESPACE_KEY)}/{w.get(NAME_KEY)} ({w.get(WORKLOAD_REPLICAS_KEY)} replicas)" for w in workloads)
        name = str(budget.get(NAME_KEY) or budget.get(PDB_KEY) or "")
        namespace = str(budget.get(NAMESPACE_KEY) or "")
        field = budget.get(FIELD_KEY)
        out.append(
            finding(
                member=member,
                check=CHECK_BUDGET,
                entry=ENTRY_BUDGET,
                namespace=namespace,
                object_name=name,
                title=f"{ENTRY_BUDGET}. {entry_title(ENTRY_BUDGET)}: {namespace}/{name} ({field}) holds the node drain",
                detail=f"PodDisruptionBudget {namespace}/{name} ({field}, disruptionsAllowed {budget.get(DISRUPTIONS_ALLOWED_KEY)}) covers {owners or 'no running workload'}; a surge upgrade holds each node's drain for up to an hour and then evicts the pod anyway.",
                rubric_values=RUBRIC["budget-singleton" if singleton else "budget"],
                action=f"Raise maxUnavailable to 1 (or lower minAvailable) on PodDisruptionBudget {namespace}/{name}, and run two replicas of {owners or 'the workload'} so the budget can be honoured.",
                rationale=entry_mitigation(ENTRY_BUDGET),
                verify=f"kubectl get pdb {name} -n {namespace} -o jsonpath='{{.spec}}{{\"  \"}}{{.status.disruptionsAllowed}}'",
                still_failing_when="disruptionsAllowed is 0 while the workload runs all its wanted replicas.",
            )
        )
    return out


def exclusion_findings(member: dict) -> list[dict]:
    out = []
    maintenance = (member.get(watch.READINESS_KEY) or {}).get(MAINTENANCE_KEY) or {}
    for exclusion in maintenance.get(EXCLUSIONS_KEY) or []:
        if not isinstance(exclusion, dict) or not exclusion.get(BLOCKS_KEY):
            continue
        name = str(exclusion.get(NAME_KEY) or "")
        end_time = exclusion.get(END_TIME_KEY)
        # The name is the free-form key of the cluster's maintenanceExclusions
        # map; one with no ASCII letter or digit derives no usable queue id, so
        # the object is a digest of it and the title keeps the name.
        object_name = name if re.search(r"[A-Za-z0-9]", name) else EXCLUSION_SURROGATE_OBJECT.format(digest=hashlib.sha256(name.encode("utf-8")).hexdigest()[:EXCLUSION_DIGEST_CHARS])
        out.append(
            finding(
                member=member,
                check=CHECK_EXCLUSION,
                entry=ENTRY_EXCLUSION,
                namespace="",
                object_name=object_name,
                title=f"{ENTRY_EXCLUSION}. {entry_title(ENTRY_EXCLUSION)}: exclusion {name} ({exclusion.get(SCOPE_KEY)}) holds the automatic upgrade until {end_time}",
                detail=str(exclusion.get(DETAIL_KEY) or ""),
                rubric_values=RUBRIC["exclusion"],
                action=f"Decide whether exclusion {name} should end on {end_time}: keep it for a planned freeze, shorten it when the lag is unintended; a manual upgrade is not held by it.",
                rationale=entry_mitigation(ENTRY_EXCLUSION),
                verify="gcloud container clusters describe <cluster> --location <location> --format='json(maintenancePolicy.window.maintenanceExclusions)'",
                still_failing_when="the exclusion is in effect with a scope that covers the upgrade the cluster is due.",
            )
        )
    return out


def skew_findings(member: dict) -> list[dict]:
    out = []
    skew = (member.get(watch.READINESS_KEY) or {}).get(SKEW_KEY) or {}
    for pool in skew.get(POOLS_KEY) or []:
        if not isinstance(pool, dict):
            continue
        verdict = str(pool.get(VERDICT_KEY) or "")
        if verdict not in (SKEW_VERDICT_BLOCKS, SKEW_VERDICT_CEILING):
            continue
        name = str(pool.get(NAME_KEY) or "")
        blocks = verdict == SKEW_VERDICT_BLOCKS
        out.append(
            finding(
                member=member,
                check=CHECK_SKEW,
                entry=ENTRY_SKEW,
                namespace="",
                object_name=name,
                title=f"{ENTRY_SKEW}. {entry_title(ENTRY_SKEW)}: pool {name} at {pool.get(VERSION_KEY)} {'blocks' if blocks else 'is at the ceiling for'} the control-plane upgrade",
                detail=str(pool.get(DETAIL_KEY) or ""),
                rubric_values=RUBRIC["skew-blocks" if blocks else "skew-ceiling"],
                action=f"Upgrade node pool {name} to the control plane's version before the control-plane move; GKE refuses a control plane more than two minors ahead of a pool.",
                rationale=entry_mitigation(ENTRY_SKEW),
                verify=f"gcloud container node-pools describe {name} --cluster <cluster> --location <location> --format='value(version)'",
                still_failing_when="the pool is two or more minors behind the target control plane.",
            )
        )
    return out


def webhook_findings(member: dict) -> list[dict]:
    out = []
    webhooks = (member.get(watch.READINESS_KEY) or {}).get(WEBHOOKS_KEY) or {}
    for kind, key in (("webhook-blocking", BLOCKING_KEY), ("webhook-outage", OUTAGE_KEY)):
        for hook in webhooks.get(key) or []:
            if not isinstance(hook, dict):
                continue
            config = str(hook.get(CONFIG_KEY) or "")
            config_kind = str(hook.get(CONFIG_KIND_KEY) or DEFAULT_WEBHOOK_CONFIG_KIND).lower()
            name = str(hook.get(NAME_KEY) or hook.get(WEBHOOK_KEY) or "")
            rules = ", ".join(str(r) for r in (hook.get(LIVE_RULES_KEY) or hook.get(RULES_KEY) or []))
            path = ", ".join(str(r) for r in (hook.get(UPGRADE_PATH_KEY) or []))
            blocking = kind == "webhook-blocking"
            what = "refuses writes the upgrade makes" if blocking else "refuses what it matches today"
            out.append(
                finding(
                    member=member,
                    check=CHECK_WEBHOOK,
                    entry=ENTRY_WEBHOOK,
                    namespace="",
                    # The kind is part of the identity: a Validating and a Mutating
                    # configuration may share a name and a webhook name.
                    object_name=WEBHOOK_OBJECT_SEPARATOR.join((config_kind, config, name)),
                    title=f"{ENTRY_WEBHOOK}. {entry_title(ENTRY_WEBHOOK)}: {config}/{name} fails closed with no backend and {what}",
                    detail=f"{hook.get(REASON_KEY) or 'backend unreachable'}; rules: {rules or 'none'}" + (f"; in the upgrade's path: {path}" if path else ""),
                    rubric_values=RUBRIC[kind],
                    action=f"Restore the backend Service {hook.get(SERVICE_KEY) or ''} or set failurePolicy Ignore on {config}/{name}, and keep kube-system out of its namespaceSelector.",
                    rationale=entry_mitigation(ENTRY_WEBHOOK),
                    verify=f"kubectl get {config_kind} {config} -o jsonpath='{{.webhooks[*].failurePolicy}}' and kubectl get endpoints -n <ns> <service>",
                    still_failing_when="failurePolicy is Fail and the backend Service has no ready endpoint.",
                )
            )
    return out


def rule_entry(item: dict, rules: dict) -> tuple[str, int | None]:
    """The rule a blocker or risk belongs to and its catalogue entry. The report
    stamps both on every row; the entry falls back to the rule's own entry in
    the ``rules`` table, and a row that names no rule is skipped."""
    rule = str(item.get(RULE_KEY) or "")
    entry = item.get(ENTRY_KEY)
    if entry is None and rule and isinstance(rules, dict) and isinstance(rules.get(rule), dict):
        entry = rules[rule].get(ENTRY_KEY)
    if isinstance(entry, bool):  # a rule's ENTRY is a number; True would be entry 1
        entry = None
    try:
        entry = int(entry) if entry is not None else None
    except (TypeError, ValueError):
        entry = None
    return rule, entry


def rule_object(item: dict) -> tuple[str, str]:
    """The namespace and object a rule's row names: the workload for a workload
    row, the pool for a pool row, the cluster for a cluster-wide one."""
    if item.get(OBJECT_KEY) and item.get(KIND_KEY):
        return str(item.get(NAMESPACE_KEY) or ""), f"{item[KIND_KEY]}/{item.get(NAME_KEY) or str(item[OBJECT_KEY]).split('/')[-1]}"
    if item.get(POOL_KEY):
        return "", str(item[POOL_KEY])
    if item.get(OBJECT_KEY):
        parts = str(item[OBJECT_KEY]).split("/")
        return (parts[0], "/".join(parts[1:])) if len(parts) > 1 else ("", parts[0])
    if item.get(NAME_KEY):
        return str(item.get(NAMESPACE_KEY) or ""), f"{item.get(KIND_KEY) or 'Object'}/{item[NAME_KEY]}"
    return "", CLUSTER_OBJECT


def rule_findings(member: dict) -> list[dict]:
    out = []
    readiness = member.get(watch.READINESS_KEY) or {}
    rules = readiness.get(RULES_KEY) or {}
    for key, blocking in ((RULE_BLOCKING_KEY, True), (RISKS_KEY, False)):
        for item in readiness.get(key) or []:
            if not isinstance(item, dict):
                continue
            rule, entry = rule_entry(item, rules)
            if not rule:
                continue
            namespace, object_name = rule_object(item)
            text = str(item.get(TEXT_KEY) or item.get(SUMMARY_KEY) or item.get(DETAIL_KEY) or "")
            values = RUBRIC["rule-blocking"] if blocking else risk_rubric(rule, entry)
            lead = "blocks the upgrade" if blocking else ("is at risk in the upgrade" if entry is not None else PLAN_LEVEL_RISK_LEAD)
            action = rule_action(entry, text, rule)
            out.append(
                finding(
                    member=member,
                    check=CHECK_PREFIX + rule,
                    entry=entry,
                    namespace=namespace,
                    object_name=object_name,
                    title=rule_title(rule, entry, object_name if object_name != CLUSTER_OBJECT else "this cluster", lead),
                    detail=text,
                    rubric_values=values,
                    action=action,
                    rationale=f"The readiness rule {rule} ({entry_label(entry)}) read: {text}",
                    verify=f"re-run the readiness report ({rule}) for this cluster",
                    still_failing_when=f"the readiness rule {rule} still names {object_name}.",
                    mitigation=action if entry is None else None,
                )
            )
    return out


def member_findings(member: dict) -> list[dict]:
    """Every finding of one member, one per queue id: two rows that derive one
    id (a rule that names one object twice, or a cluster-wide rule row twice)
    are one finding whose detail carries both texts and whose rubric is the
    higher, since the queue would hold one row and answer the second as an
    update of the first."""
    return fold_shared_ids(budget_findings(member) + exclusion_findings(member) + skew_findings(member) + webhook_findings(member) + rule_findings(member))


def fold_shared_ids(items: list[dict]) -> list[dict]:
    """One finding per queue id across the items handed in. Within a member,
    two rows that derive one id are one finding whose detail carries both
    texts; across members, two same-named clusters in one project whose
    objects coincide (regional twins deployed alike) derive one id too, since
    the queue's identity carries no location, and the one finding's detail
    names the other member. The rubric is the higher of the two."""
    by_id: dict[str, dict] = {}
    for item in items:
        fid = item_id(item)
        first = by_id.get(fid)
        if first is None:
            item.setdefault(MEMBERS_FIELD, [item[MEMBER_FIELD]])
            by_id[fid] = item
            continue
        if item[MEMBER_FIELD] != first[MEMBER_FIELD]:
            first["detail"] = clip(first["detail"] + SHARED_DETAIL_SEPARATOR.format(member=item[MEMBER_FIELD]) + item["detail"])
            for member in item.get(MEMBERS_FIELD, [item[MEMBER_FIELD]]):
                if member not in first[MEMBERS_FIELD]:
                    first[MEMBERS_FIELD].append(member)
        else:
            first["detail"] = clip(first["detail"] + DUPLICATE_DETAIL_SEPARATOR + item["detail"])
        if rank_of(item) > rank_of(first):
            first["rubric"] = item["rubric"]
    return list(by_id.values())


def rank_of(item: dict) -> float:
    return findings_queue.rank_score(findings_queue.validate_rubric(item["rubric"]))


def member_complete(member: dict, report_errors: list) -> bool:
    """Whether the run read this cluster in full, which is what lets the queue
    lower the confidence of a finding it did not re-report. A member whose own
    read failed is not complete; nor one the grader left ``unknown`` or could
    not decide a part of (a pool whose version did not parse, an exclusion
    with an unparsable time or scope, a rule that failed to run), since the
    rows it would have written are missing rather than cleared; nor one in a
    location whose ``get-server-config`` failed (an ``errors`` row with the
    location and no cluster), which left it no target to grade against."""
    readiness = member.get(watch.READINESS_KEY) or {}
    if any(readiness.get(key) for key in READ_ERROR_KEYS):
        return False
    if readiness.get(watch.STATUS_KEY) == watch.READINESS_UNKNOWN:
        return False
    undecided = (
        (readiness.get(SKEW_KEY) or {}).get(SKEW_UNKNOWN_KEY),
        (readiness.get(MAINTENANCE_KEY) or {}).get(UNDECIDED_EXCLUSIONS_KEY),
        readiness.get(RULES_UNKNOWN_KEY),
    )
    if any(undecided):
        return False
    errors = [error for error in report_errors if isinstance(error, dict)]
    key = watch.member_key(member)
    if any(watch.member_key(error) == key for error in errors):
        return False
    location_keys = watch.MEMBER_ID_KEYS[:-1]
    cluster_key = watch.MEMBER_ID_KEYS[-1]
    location = tuple(member.get(k) for k in location_keys)
    failed_locations = {tuple(error.get(k) for k in location_keys) for error in errors if error.get(location_keys[-1]) and not error.get(cluster_key)}
    return location not in failed_locations


def payload(item: dict) -> dict:
    return {k: v for k, v in item.items() if not k.startswith("_")}


# --- running the reports -------------------------------------------------------


def ordered_names(names: list[str], previous: dict | None) -> list[str]:
    """The projects last week's budget cut short or never started go first,
    so a budget that ran out does not run out on the same tail twice; a
    project that failed on its own (its full timeout, a refusal, a listing
    failure) keeps its place, so it does not take the budget off the top
    every week. Within each group a spelling by number runs after the ids,
    so a project the roster spells both ways is read under its id first; the
    number is then skipped when the watch's ledger maps it to that id
    (``run_projects``), else run and dropped once its report resolves it."""
    failures = last_failures(previous)
    budget = {p for p, detail in failures.items() if str(detail) == BUDGET_EXHAUSTED_DETAIL or str(detail).startswith(BUDGET_FAILURE_PREFIXES)}
    known_ids = learned_project_ids()
    first = [n for n in names if n in budget or watch.canonical_project(n, known_ids) in budget]

    def group(selected: list[str]) -> list[str]:
        return [n for n in selected if not watch.is_project_number(n)] + [n for n in selected if watch.is_project_number(n)]

    return group(first) + group([n for n in names if n not in first])


def last_failures(previous: dict | None) -> dict[str, str]:
    """The last run's failures: the record's, or, when that run failed in every
    project and so wrote no record, the pairs its failure marker holds
    (``failure_signature``); a run that reads clears the marker. The marker
    folds digits; budget failures are told by ``BUDGET_EXHAUSTED_DETAIL`` and
    ``CUT_SHORT_PREFIX``, neither of which carries one, so they still read as
    budget failures."""
    try:
        pairs = json.loads(failure_marker().read_text(encoding="utf-8"))
        if isinstance(pairs, list) and pairs:
            return {str(project): str(detail) for project, detail in pairs}
    except (OSError, ValueError, TypeError):  # no marker, another failure's text, or not pairs
        pass
    return (previous or {}).get("project_failures") or {}


def learned_project_ids() -> dict[str, str]:
    """The spelling-to-id map the daily watch keeps in its ledger
    (``learn_project_id``), or none when the ledger cannot be read: the
    review then learns a duplicate spelling from the report, as before."""
    try:
        ids = watch.load_ledger(watch.watch_home() / watch.LEDGER_FILE_NAME).get(watch.PROJECT_IDS_KEY)
    except (OSError, ValueError, RuntimeError):
        return {}
    return {str(k): str(v) for k, v in ids.items()} if isinstance(ids, dict) else {}


def run_projects(names: list[str], scope, on_project=None) -> tuple[list[dict], list[dict], dict[str, str], dict[str, str], set[str]]:
    """One readiness run per project inside the total budget. Returns the
    members, the report errors, the report script's tables per project, the
    projects that failed with why, and the ids the report resolved and read.
    `on_project(members, errors)` runs as each project returns, which is where
    registration happens, so a run the hour cuts short keeps what it
    registered. A project is keyed by the id the watch's ledger learned for
    its spelling, so a project the roster spells by number and by id fails
    once, and a table is headed by the id its report resolved, which is what
    the rows carry. A spelling is asked for by that id on any install: an id
    needs no describe, so a describe that fails on Saturday costs nothing for
    a project the watch has resolved, and a declared scope, which lists ids,
    reads it as listed. A spelling whose project is already read or already failed this
    run is not run at all, as the watch's table hop skips a tabled key: a
    second failure would replace the first one's cause, and one the budget cut
    short would put the project ahead of every other next week. One the
    ledger does not map yet is run, and dropped whole once its report
    resolves it to an id already read: no table, no errors, no failure,
    nothing registered."""
    members: list[dict] = []
    errors: list[dict] = []
    tables: dict[str, str] = {}
    failures: dict[str, str] = {}
    read_ids: set[str] = set()
    known_ids = learned_project_ids()
    deadline = time.monotonic() + TOTAL_BUDGET_SECONDS
    for project in names:
        key = watch.canonical_project(project, known_ids)
        if key in read_ids or key in failures:
            # The same project, already read or failed this run under its other spelling.
            continue
        remaining = deadline - time.monotonic()
        if remaining < MIN_PROJECT_RUN_SECONDS:
            failures[key] = BUDGET_EXHAUSTED_DETAIL
            continue
        timeout = min(PROJECT_TIMEOUT_SECONDS, remaining)
        asked = key
        try:
            envelope = watch.run_report([asked], readiness=True, scope=scope, outside_stands=False, timeout=timeout)
        except watch.SandboxTimedOut as exc:
            # The review's own budget, not the sandbox, ended a run it started
            # with less than the full timeout; said as such, so the remedy read
            # is fewer projects or a longer budget, not a sandbox fault.
            failures[key] = CUT_SHORT_DETAIL.format(seconds=int(timeout)) if timeout < PROJECT_TIMEOUT_SECONDS else str(exc)
            continue
        except (RuntimeError, ValueError) as exc:  # the watch's ReportRefused is a RuntimeError
            failures[key] = str(exc)
            continue
        report = envelope.get(watch.ENVELOPE_REPORT_KEY) or {}
        found = [m for m in report.get(watch.MEMBERS_KEY) or [] if isinstance(m, dict)]
        read_errors = [e for e in report.get(watch.ERRORS_KEY) or [] if isinstance(e, dict)]
        resolved = {str(p) for p in report.get(watch.REPORT_PROJECTS_KEY) or [asked]}
        if resolved & read_ids:
            # The roster spelled one project twice, by number and by id: the
            # report resolved both to the same id, and its clusters are already
            # read and registered under the first run. The second leaves nothing.
            continue
        errors.extend(read_errors)
        tables[RESOLVED_IDS_SEPARATOR.join(sorted(resolved))] = str(envelope.get(watch.ENVELOPE_TABLES_KEY) or "")
        unlisted = watch.unlisted_projects(read_errors)
        if unlisted:
            # The sandbox ran but the project's cluster listing failed: the
            # report says so in an errors row with a project and no location
            # and returns no member for it. One project runs at a time, so any
            # such row is this project's, keyed by the spelling the roster gave
            # or by the id the report resolved a number to. A project not read,
            # as the watch counts it, not a clean project with no clusters; booked
            # under the id the report resolved, as its table is, so a number and
            # its id that both fail here are one project.
            failures[next(iter(resolved)) if len(resolved) == 1 else key] = next(
                (str(e.get(watch.MESSAGE_KEY) or LISTING_FAILED_DETAIL) for e in read_errors if e.get(watch.MEMBER_ID_KEYS[0]) in unlisted and not e.get(watch.MEMBER_ID_KEYS[1])),
                LISTING_FAILED_DETAIL,
            )
            continue
        read_ids |= resolved
        # This project failed earlier this run under another spelling (an id whose
        # run timed out before its number read): read now, not a project not read.
        for read in resolved:
            failures.pop(read, None)
        members.extend(found)
        if on_project is not None:
            on_project(found, read_errors)
    return members, errors, tables, failures, read_ids


# --- registering ------------------------------------------------------------------


def register(members: list[dict], errors: list[dict], findings: list[dict], stop: float | None = None) -> tuple[dict[str, str], list[str], set[str]]:
    """Register the findings member by member, each batch with its scope when
    the cluster was read in full (a complete cluster with nothing to report
    sends an empty batch with the scope). Returns the outcome per id, the
    registration errors, and the member keys whose claim of completeness
    reached the queue, which is what the week's delta may be counted over.
    Batches are keyed by member (project, location and cluster): a cluster
    name repeats across locations. The queue keys rows and claims by project
    and cluster alone, so two members of one name in one project send their
    batches and no claim: one twin's claim would lower the other's rows, and
    a row left at full confidence is the lesser error. Each row is validated
    here before it is sent, so one the queue would refuse fails alone instead
    of its whole batch; its cluster then sends no claim, which would book that
    finding's row absent, and the claim is counted as one that did not reach
    the queue. ``stop`` (a ``time.monotonic`` instant) bounds the sends: each
    waits at most what is left, and a batch with nothing left is booked as not
    sent, its claim with it."""
    outcomes: dict[str, str] = {}
    problems: list[str] = []
    claimed: set[str] = set()
    by_member: dict[str, list[dict]] = {}
    for item in fold_shared_ids(findings):
        by_member.setdefault(item[MEMBER_FIELD], []).append(item)
    pairs = [cluster_fields(m)[:: len(watch.MEMBER_ID_KEYS) - 1] for m in members]
    twins = {pair for pair in pairs if pairs.count(pair) > 1}
    for member in members:
        project, _location, cluster = cluster_fields(member)
        key = watch.member_key(member)
        rows = by_member.get(key, [])
        batch = []
        for item in rows:
            try:
                findings_queue.validate_finding(payload(item))
            except findings_queue.FindingError as exc:
                problems.append(f"{project}/{cluster}: {item['check']} {item['namespace'] + '/' if item['namespace'] else ''}{item['object']}: {exc}")
                outcomes[item_id(item)] = REGISTRATION_FAILED
                continue
            batch.append(item)
        complete = member_complete(member, errors) and (project, cluster) not in twins
        if complete and len(batch) < len(rows):
            # A row refused here is a finding read and not sent: a claim would book its row
            # from last week absent, so the cluster claims nothing. Counted as a claim that
            # did not reach the queue where one would have been sent, since last week's rows
            # on the cluster keep their confidence all the same.
            complete = False
            if len(rows) <= KV_BATCH_LIMIT:
                problems.append(f"{project}/{cluster}: {CLAIM_FAILED_MARK}: {CLAIM_WITHHELD_DETAIL.format(count=len(rows) - len(batch))}")
        if not batch and not complete:
            continue
        # A complete read of a cluster with nothing to report is sent too: it is
        # what lets the queue lower the confidence of last week's rows there,
        # and it reaches only this job's own family of checks. A cluster with
        # more findings than one batch holds is sent in pieces and claims no
        # completeness, since the claim covers only the ids in the call.
        chunks = [batch[i : i + KV_BATCH_LIMIT] for i in range(0, len(batch), KV_BATCH_LIMIT)] or [[]]
        for chunk in chunks:
            body = {"findings": [payload(item) for item in chunk]}
            claims = complete and len(chunks) == 1
            if claims:
                # The family and the review's own source together: the claim reaches only
                # the rows this job wrote in its family, never the `upgrade-` rows the audit
                # streams or the retrospective write under their own sources.
                body["scope"] = {"project": project, "cluster": cluster, "complete": True, "check_prefix": CHECK_PREFIX, "source": SOURCE}
            def time_left() -> float:
                return KV_TIMEOUT_SECONDS if stop is None else min(KV_TIMEOUT_SECONDS, stop - time.monotonic())

            try:
                if time_left() <= 0:
                    raise OSError(REGISTRATION_OUT_OF_TIME_DETAIL)
                try:
                    answer = kv_request(KV_FINDINGS_PATH, body, timeout=time_left())
                except QueueBusy:
                    if time_left() <= BUSY_RETRY_SECONDS:
                        raise
                    time.sleep(BUSY_RETRY_SECONDS)
                    answer = kv_request(KV_FINDINGS_PATH, body, timeout=time_left())
                sent = {item_id(item) for item in chunk}
                results = answer.get(ANSWER_RESULTS_KEY)
                acknowledged = {str(r["id"]) for r in results if isinstance(r, dict) and r.get("id")} if isinstance(results, list) else set()
                # Exactly the ids sent: a result for an id the batch never carried is
                # not this batch's answer, and acknowledges neither its rows nor its claim.
                if not isinstance(results, list):
                    reasons = [UNACKNOWLEDGED_NOT_A_LIST]
                else:
                    missing, extra = sent - acknowledged, acknowledged - sent
                    reasons = ([UNACKNOWLEDGED_MISSING.format(count=len(missing), sent=len(sent))] if missing else []) + ([UNACKNOWLEDGED_EXTRA.format(count=len(extra))] if extra else [])
                if claims and not isinstance(answer.get(ANSWER_DOWNGRADED_KEY), int):
                    reasons.append(UNACKNOWLEDGED_CLAIM)
                if reasons:
                    raise ValueError(UNACKNOWLEDGED_DETAIL.format(reasons=UNACKNOWLEDGED_SEPARATOR.join(reasons)))
            except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as exc:
                # A batch that carried the claim lost it with its rows, whatever
                # the rows: the line counts it, since last week's rows for the
                # cluster keep their confidence.
                problems.append(f"{project}/{cluster}: {CLAIM_FAILED_MARK}: {exc}" if claims else f"{project}/{cluster}: {exc}")
                for item in chunk:
                    outcomes[item_id(item)] = REGISTRATION_FAILED
                continue
            if claims:
                claimed.add(key)
            for result in answer[ANSWER_RESULTS_KEY]:
                if isinstance(result, dict) and result.get("id"):
                    outcomes[str(result["id"])] = str(result.get("outcome") or "")
    return outcomes, problems, claimed


def item_id(item: dict) -> str:
    """The id the queue gives this row, from the validator that computes it,
    so the record and the queue never key one finding two ways; a row the
    validator refuses keeps the plain derivation for its failure entry."""
    try:
        return findings_queue.validate_finding(payload(item))["id"]
    except findings_queue.FindingError:
        return findings_queue.derive_finding_id(item["check"], item["project"], item["cluster"], item["namespace"], item["object"])


def severity_of(item: dict) -> str:
    r = findings_queue.validate_rubric(item["rubric"])
    return findings_queue.severity_for(findings_queue.rank_score(r), r)


# --- the record ------------------------------------------------------------------


def summary(
    *,
    stamp: str,
    now: datetime,
    names: list[str],
    members: list[dict],
    errors: list[dict],
    failures: dict[str, str],
    findings: list[dict],
    outcomes: dict[str, str],
    problems: list[str],
    previous: dict | None,
    report_path: Path,
    claimed: set[str] | None = None,
    read_ids: set[str] | None = None,
) -> dict:
    keys = [watch.member_key(m) for m in members]
    blocked, ready, unknown, unread = watch.readiness_verdicts({watch.MEMBERS_KEY: members, watch.ERRORS_KEY: errors}, keys)
    rows = []
    for item in findings:
        fid = item_id(item)
        rows.append(
            {
                "id": fid,
                "severity": severity_of(item),
                "check": item["check"],
                "entry": item.get("_entry"),
                "project": item["project"],
                "cluster": item["cluster"],
                "namespace": item["namespace"],
                "object": item["object"],
                "title": item["title"],
                "action": item["recommendation"]["action"],
                "outcome": outcomes.get(fid, ""),
                ROW_MEMBER_KEY: item[MEMBER_FIELD],
                ROW_MEMBERS_KEY: list(item.get(MEMBERS_FIELD) or [item[MEMBER_FIELD]]),
            }
        )
    counts = {severity: sum(1 for r in rows if r["severity"] == severity) for severity in SEVERITY_ORDER}
    ids = [r["id"] for r in rows]
    # Only a cluster read in full, whose claim of completeness reached the
    # queue, can say that last week's row is gone; one whose read failed, or
    # that was sent in pieces, keeps its rows unjudged, as the queue does.
    reviewed_keys = {watch.member_key(m) for m in members if member_complete(m, errors)}
    if claimed is not None:
        reviewed_keys &= claimed
    # The queue lowers an absent row by its project and cluster, whichever member
    # wrote it (findings_queue._downgrade_absent), so last week's row is judged by
    # its pair too. Twins claim nothing, so a claimed pair names one cluster.
    reviewed_pairs = {tuple(key.split(watch.MEMBER_KEY_SEPARATOR)[:: len(watch.MEMBER_ID_KEYS) - 1]) for key in reviewed_keys}
    previous_ids = [
        r.get("id")
        for r in (previous or {}).get("findings") or []
        if r.get("id") and (str(r.get("project") or ""), str(r.get("cluster") or "")) in reviewed_pairs
    ]
    return {
        "version": SUMMARY_VERSION,
        "job": JOB_ID,
        "stamp": stamp,
        "generated_at": iso(now),
        # The ids the report resolved and read (what the queue's rows carry),
        # then the projects that failed, for the brief's paging and its line.
        "projects": sorted(read_ids) + list(failures) if read_ids is not None else names,
        "clusters": {
            REVIEWED_KEY: keys,
            watch.READINESS_BLOCKED: blocked,
            watch.READINESS_READY: ready,
            watch.READINESS_UNKNOWN: unknown,
            UNREAD_KEY: unread,
        },
        "project_failures": failures,
        "counts": counts,
        "findings": rows,
        "new_ids": [r["id"] for r in rows if r["outcome"] == CREATED],
        "suppressed_ids": [r["id"] for r in rows if r["outcome"] == SUPPRESSED],
        "no_longer_reported_ids": [fid for fid in previous_ids if fid not in ids],
        "registration_errors": problems,
        "report_path": str(report_path),
        "sandbox_copy": f"{SANDBOX_COPY_DIR}/{LATEST_MD_NAME}",
        "brief_job": BRIEF_JOB_ID,
    }


def render_report(summary_doc: dict, findings: list[dict], tables: dict[str, str], errors: list[dict]) -> str:
    clusters = summary_doc["clusters"]
    lines = [
        f"# {REPORT_TITLE}",
        "",
        f"Generated {summary_doc['generated_at']} by `{JOB_ID}`. Projects: {', '.join(summary_doc['projects']) or 'none'}.",
        "",
        "## At a glance",
        "",
        f"- Clusters reviewed: {len(clusters['reviewed'])}; blocked: {len(clusters['blocked'])}; ready: {len(clusters['ready'])}; "
        f"unknown: {len(clusters['unknown'])}; not read: {len(clusters['unread'])}.",
        f"- Findings: {summary_doc['counts']['critical']} critical, {summary_doc['counts']['major']} major, {summary_doc['counts']['minor']} minor; "
        f"{len(summary_doc['new_ids'])} new since the previous review, {len(summary_doc['no_longer_reported_ids'])} no longer reported, "
        f"{len(summary_doc['suppressed_ids'])} dismissed earlier and kept dismissed.",
        "- Decide on a finding by replying in chat with its id and `dismiss`, `snooze until <date>` or `accept`.",
        "",
        "## Verdict per cluster",
        "",
        "| Cluster | Verdict | Critical | Major | Minor |",
        "| --- | --- | --- | --- | --- |",
    ]
    rows_by_cluster: dict[str, list[dict]] = {}
    for row in summary_doc["findings"]:
        for member in row.get(ROW_MEMBERS_KEY) or [row[ROW_MEMBER_KEY]]:
            rows_by_cluster.setdefault(member, []).append(row)
    verdict_of = {}
    for name, key in ((watch.READINESS_BLOCKED, watch.READINESS_BLOCKED), (watch.READINESS_READY, watch.READINESS_READY), (watch.READINESS_UNKNOWN, watch.READINESS_UNKNOWN), ("not read", UNREAD_KEY)):
        for member in clusters[key]:
            verdict_of[member] = name
    for member in clusters[REVIEWED_KEY]:
        rows = rows_by_cluster.get(member, [])
        lines.append(
            f"| {member} | {verdict_of.get(member, 'unknown')} | "
            + " | ".join(str(sum(1 for r in rows if r["severity"] == s)) for s in SEVERITY_ORDER)
            + " |"
        )
    if summary_doc["project_failures"]:
        lines += ["", "## Projects not read", ""]
        lines += [f"- {project}: {detail}" for project, detail in summary_doc["project_failures"].items()]
    lines += ["", "## Findings", ""]
    if not summary_doc["findings"]:
        lines.append("None.")
    order = {s: i for i, s in enumerate(SEVERITY_ORDER)}
    details = {item_id(item): item for item in findings}
    for row in sorted(summary_doc["findings"], key=lambda r: (order.get(r["severity"], len(order)), r["cluster"], r["id"])):
        item = details.get(row["id"], {})
        lines += [
            f"### [{row['severity']}] {row['title']}",
            "",
            f"- Id: `{row['id']}` ({row['outcome'] or 'not registered'})",
            f"- Where: {', '.join(row.get(ROW_MEMBERS_KEY) or [row[ROW_MEMBER_KEY]])}" + (f", namespace {row['namespace']}" if row["namespace"] else "") + f", {row['object']}",
            f"- Evidence: {item.get('detail') or '-'}",
            f"- Do: {row['action']}",
            f"- Verify: {(item.get('verification') or {}).get('command') or '-'}",
            "",
        ]
    if summary_doc["registration_errors"]:
        lines += ["## Registration errors", ""] + [f"- {p}" for p in summary_doc["registration_errors"]] + [""]
    if errors:
        lines += ["## Read errors", ""] + [f"- {json.dumps(e, sort_keys=True)}" for e in errors] + [""]
    lines += ["## Report script output", ""]
    for project, text in tables.items():
        lines += [f"### {project}", "", watch.tables_without_the_output_line(text).rstrip(), ""]
    return "\n".join(lines).rstrip() + "\n"


def load_previous(home: Path) -> dict | None:
    path = home / LATEST_JSON_NAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_record(home: Path, stamp: str, report_md: str, report_json: dict, findings_json: dict, summary_doc: dict) -> Path:
    run_dir = home / stamp
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / REPORT_MD_NAME).write_text(report_md, encoding="utf-8")
    (run_dir / REPORT_JSON_NAME).write_text(json.dumps(report_json, indent=1, sort_keys=True), encoding="utf-8")
    (run_dir / FINDINGS_JSON_NAME).write_text(json.dumps(findings_json, indent=1, sort_keys=True), encoding="utf-8")
    # The two latest files are replaced one after the other, after both are
    # written: no half-written file, and a mixed pair only if this process dies
    # between the two renames.
    latest = {LATEST_MD_NAME: report_md, LATEST_JSON_NAME: json.dumps(summary_doc, indent=1, sort_keys=True)}
    for name, text in latest.items():
        (home / (name + TMP_SUFFIX)).write_text(text, encoding="utf-8")
    for name in latest:
        os.replace(home / (name + TMP_SUFFIX), home / name)
    prune(home)
    return run_dir / REPORT_MD_NAME


def prune(home: Path) -> None:
    runs = sorted(p for p in home.iterdir() if p.is_dir())
    for stale in runs[:-KEEP_RUNS]:
        shutil.rmtree(stale, ignore_errors=True)


def sandbox_copy_program(files: dict[str, str]) -> str:
    """The program the terminal login runs to place the copy: the directory
    made and opened, each file written whole beside its name, set read-only and
    renamed into place once both are complete, the directory closed again. The copy is the terminal login's own file, so
    nothing stops a model turn that ran the readiness report by hand from
    copying its JSON over the brief's Markdown, which one did; read-only modes
    make that an error the turn sees instead of a record the brief misreads
    (the login owns the files and could chmod them back; the modes stop the
    accident, not the intent).
    The contents travel base64-encoded, because a JSON string literal in Python
    source turns a character outside the Basic Multilingual Plane into a
    surrogate pair the file write refuses."""
    encoded = {name: base64.b64encode(text.encode("utf-8")).decode("ascii") for name, text in files.items()}
    return "\n".join(
        [
            "import base64, os",
            f"DIR = {json.dumps(SANDBOX_COPY_DIR)}",
            f"FILES = {json.dumps(encoded)}",
            f"MODE = {SANDBOX_COPY_MODE}",
            f"OPEN = {SANDBOX_COPY_DIR_OPEN_MODE}",
            f"CLOSED = {SANDBOX_COPY_DIR_CLOSED_MODE}",
            f"TMP = {json.dumps(TMP_SUFFIX)}",
            "os.makedirs(DIR, exist_ok=True)",
            "os.chmod(DIR, OPEN)",
            "try:",
            # Both files land beside their names and are renamed over them only
            # once both are complete, so a reader never meets a half-written
            # file. The two renames are separate steps: a process that dies
            # between them, the one gap left, leaves this week's report beside
            # last week's summary, with the second temporary as its trace. The
            # directory is closed again whatever happened.
            "    for name, text in FILES.items():",
            "        tmp = os.path.join(DIR, name + TMP)",
            "        if os.path.exists(tmp):",  # a run that failed between the write and the rename
            "            os.remove(tmp)",
            "        with open(tmp, 'wb') as handle:",
            "            handle.write(base64.b64decode(text))",
            "    for name in FILES:",
            "        os.chmod(os.path.join(DIR, name + TMP), MODE)",
            "        os.replace(os.path.join(DIR, name + TMP), os.path.join(DIR, name))",
            "finally:",
            "    os.chmod(DIR, CLOSED)",
            "print(len(FILES))",
            "",
        ]
    )


def copy_locally(files: dict[str, str]) -> str | None:
    """Without an ssh sandbox the agent's terminal runs on this pod, so the copy
    is a plain write here and nothing is made read-only: the modes exist for a
    login that is not this job, and the directory may be the record's own."""
    target = Path(SANDBOX_COPY_DIR)
    try:
        if target.resolve() == weekly_home().resolve():
            return None
        target.mkdir(parents=True, exist_ok=True)
        for name, text in files.items():
            (target / (name + TMP_SUFFIX)).write_text(text, encoding="utf-8")
        for name in files:
            os.replace(target / (name + TMP_SUFFIX), target / name)
    except OSError as exc:
        return str(exc)
    return None


def copy_to_sandbox(report_md: str, summary_doc: dict) -> str | None:
    """Place latest.md and latest.json where the agent reads them; the error
    text on failure, None on success."""
    files = {LATEST_MD_NAME: report_md, LATEST_JSON_NAME: json.dumps(summary_doc, indent=1, sort_keys=True)}
    if not sandbox_exec.sandbox_enabled():
        return copy_locally(files)
    program = sandbox_copy_program(files)
    try:
        completed = sandbox_exec.run(
            [watch.PYTHON_EXECUTABLE, watch.PYTHON_ISOLATED_FLAG, watch.STDIN_SCRIPT_ARG],
            timeout=SANDBOX_COPY_TIMEOUT_SECONDS,
            check=False,
            stdin=program,
            principal=sandbox_exec.TERMINAL_PRINCIPAL,
        )
    except Exception as exc:  # noqa: BLE001 - the copy is best effort; the record is on this side
        return str(exc)
    if completed.returncode != 0:
        return " ".join((completed.stderr or completed.stdout or "").split())[: watch.STDERR_EXCERPT_CHARS]
    return None


# --- the tick ---------------------------------------------------------------------


def shown(entries: list[str], limit: int, tail: str = OVERFLOW_NOTE) -> str:
    """The first ``limit`` entries, then how many more (the record holds them,
    where a record was written): a chat line names a few, never a fleet."""
    if len(entries) <= limit:
        return PARTIAL_DETAIL_SEPARATOR.join(entries)
    return PARTIAL_DETAIL_SEPARATOR.join(entries[:limit] + [tail.format(count=len(entries) - limit)])


class ProjectsFailed(RuntimeError):
    """Every project failed: the text names each with its reason, and ``failures``
    keys the announcement, so a project's name stays literal in the marker."""

    def __init__(self, detail: str, failures: dict[str, str]) -> None:
        super().__init__(detail)
        self.failures = dict(failures)


def failure_signature(detail: str, failures: dict[str, str] | None = None) -> str:
    """The marker's key: the text with every number folded, or, for a run whose
    every project failed, each project as ``run_projects`` keys it, digits
    unfolded, beside its folded reason, so a timeout's figure re-posts nothing
    while a different project failing the same way is a new line. Those pairs
    are also what orders the next run (``last_failures``), since such a run
    writes no record."""
    if failures:
        return json.dumps(sorted([p, FAILURE_DIGITS.sub(FAILURE_DIGIT_MARK, str(d))] for p, d in failures.items()))
    return FAILURE_DIGITS.sub(FAILURE_DIGIT_MARK, detail)


def announce_failure_once(detail: str, failures: dict[str, str] | None = None) -> bool:
    try:
        marker = failure_marker()
        signature = failure_signature(detail, failures)
        if marker.exists() and marker.read_text(encoding="utf-8") == signature:
            return False
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(signature, encoding="utf-8")
    except OSError:
        pass
    return True


def clear_failure_marker() -> bool:
    try:
        marker = failure_marker()
        if marker.exists():
            marker.unlink()
            return True
    except OSError:
        pass
    return False


def review(dry_run: bool = False) -> tuple[list[str], int]:
    """Run the review. Returns the lines to print and the exit code."""
    now = now_utc()
    stamp = now.strftime(STAMP_FORMAT)
    scope = watch.declared_scope()
    names = watch.projects()
    if not names:
        raise RuntimeError(NO_PROJECTS_DETAIL)
    home = weekly_home()
    previous = load_previous(home)
    findings: list[dict] = []
    outcomes: dict[str, str] = {}
    problems: list[str] = []
    claimed: set[str] = set()

    # The reads end at the budget; the sends have their own share after it.
    stop = time.monotonic() + TOTAL_BUDGET_SECONDS + REGISTRATION_GRACE_SECONDS

    def registered(found: list[dict], read_errors: list[dict]) -> None:
        items = fold_shared_ids([item for member in found for item in member_findings(member)])
        findings.extend(items)
        if dry_run:
            return
        got, bad, done = register(found, read_errors, items, stop)
        outcomes.update(got)
        problems.extend(bad)
        claimed.update(done)

    members, errors, tables, failures, read_ids = run_projects(ordered_names(names, previous), scope, on_project=registered)
    if failures and not read_ids:
        raise ProjectsFailed(shown([f"{p}: {d}" for p, d in failures.items()], MAX_NAMES_IN_LINE, OVERFLOW_NOTE_PLAIN), failures)
    report_path = home / stamp / REPORT_MD_NAME
    summary_doc = summary(
        stamp=stamp, now=now, names=names, members=members, errors=errors, failures=failures,
        findings=findings, outcomes=outcomes, problems=problems, previous=previous, report_path=report_path,
        claimed=None if dry_run else claimed,
        read_ids=read_ids,
    )
    report_md = render_report(summary_doc, findings, tables, errors)
    if dry_run:
        lines = [DRY_RUN_HEADER, json.dumps({k: v for k, v in summary_doc.items() if k != "findings"}, indent=1, sort_keys=True)]
        lines += [f"- {r['severity']}: {r['id']}: {r['title']}" for r in summary_doc["findings"]]
        return lines, 0
    report_json = {"generated_at": iso(now), "projects": summary_doc["projects"], watch.MEMBERS_KEY: members, watch.ERRORS_KEY: errors, "project_failures": failures}
    findings_json = {"generated_at": iso(now), "findings": [payload(item) for item in findings], "outcomes": outcomes, "errors": problems}
    # Said in chat, not on stderr, which the scheduler keeps nowhere for a
    # zero-exit script: findings off the queue, a copy the brief cannot read and
    # projects not read are the week's failures whether or not the record is
    # written, so a record that cannot be written does not silence them.
    def registration_lines(where: str) -> list[str]:
        if not problems:
            return []
        failed_rows = sum(1 for outcome in outcomes.values() if outcome == REGISTRATION_FAILED)
        failed_claims = sum(1 for problem in problems if CLAIM_FAILED_MARK in problem)
        return [REGISTRATION_FAILED_LINE.format(prefix=LOG_PREFIX, rows=failed_rows, claims=failed_claims, detail=shown(problems, MAX_PROBLEMS_IN_LINE), where=where)]

    partial_lines = []
    if failures:
        detail = shown([f"{p}: {d}" for p, d in failures.items()], MAX_NAMES_IN_LINE)
        partial_lines.append(PARTIAL_LINE.format(prefix=LOG_PREFIX, count=len(failures), detail=detail))
    try:
        write_record(home, stamp, report_md, report_json, findings_json, summary_doc)
    except OSError as exc:
        failed = FAILED_LINE.format(prefix=LOG_PREFIX, detail=f"could not write the record under {home}: {exc}")
        return [failed, *registration_lines(REGISTRATION_NO_RECORD), *partial_lines], 1
    copy_error = copy_to_sandbox(report_md, summary_doc)
    lines = []
    if clear_failure_marker():
        lines.append(RECOVERED_LINE.format(prefix=LOG_PREFIX))
    lines += registration_lines(REGISTRATION_IN_REPORT)
    if copy_error:
        lines.append(COPY_FAILED_LINE.format(prefix=LOG_PREFIX, detail=copy_error))
    return lines + partial_lines, 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the weekly upgrade readiness review: grade every cluster, register the findings, write the report.")
    parser.add_argument("--dry-run", action="store_true", help="Run the reports and print what would be registered and written; change nothing.")
    args = parser.parse_args(argv)
    try:
        lines, code = review(dry_run=args.dry_run)
    except Exception as exc:  # noqa: BLE001 - one line per distinct failure, then exit 0 so the schedule holds
        detail = str(exc) or exc.__class__.__name__
        if args.dry_run or announce_failure_once(detail, exc.failures if isinstance(exc, ProjectsFailed) else None):
            print(FAILED_LINE.format(prefix=LOG_PREFIX, detail=detail))
        return 0
    for line in lines:
        print(line)
    return code


if __name__ == "__main__":
    sys.exit(main())
