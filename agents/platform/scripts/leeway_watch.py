#!/usr/bin/env python3
"""leeway_watch.py - sweep fleet clusters daily with `leeway report` and
surface new, worsened, or weekly-refreshed topology skew findings.

The `topology-drift` skill diagnoses single-cluster and fleet-wide topology
findings (`leeway.contract_violated`, `leeway.placement_drift`,
`leeway.domain_unavailable`) and ComputeClass priority-rank findings
(`leeway.rank_wedged`, `leeway.rank_degraded`, `leeway.rank_no_migration`,
`leeway.rank_tier_unused`) on demand. This job runs once
a day from the Platform Agent's cron roster (`no_agent: true`, `deliver:
"chat"`) so the fleet learns about single-zone blast-radius concentration and
broken spread constraints before a zone outage or node drain exposes them.

Which clusters. Target clusters are resolved on the agent pod:
1. `LEEWAY_WATCH_CLUSTERS` when set (comma-separated `project/location/cluster`
   triples), less any clusters the declared scope excludes
   (`spec.scope.exclude.clusters`), read in batches as a project's clusters
   are.
2. On an install with a declared scope (`spec.scope`), the resolved projects
   from `fleet_scope_targets.py` (`fleet_scope.json` / `ScopeTargets.projects`),
   less any clusters in `ScopeTargets.excluded_clusters`.
3. Otherwise `LEEWAY_WATCH_PROJECTS` when set, or the management project
   (`GCP_PROJECT_ID`) together with every project a Cluster Agent profile's
   `cluster_identity` names; with neither, the sandbox's `gcloud config
   get-value project`; and with nothing at all the tick fails closed rather
   than enumerate every project the credential can list. A project whose
   clusters the Cluster Agent profiles name is swept by those
   `<project>/<location>/<cluster>` triples; any other project is enumerated
   first with `gcloud container clusters list --project <project>` in the
   sandbox and swept by the triples that returns, less any clusters in
   `ScopeTargets.excluded_clusters`. Every `leeway report` run is a `--clusters`
   run: in the sandbox each triple becomes a
   `kubectl --context=gke_<project>_<location>_<cluster>` read through the
   credential-proxy shim, which is the one route the sandbox has to a
   cluster.
4. Under `spec.mode: next` (`KUBEAGENTS_RESOLVED_SCOPE_FILE` set), steps 2
   and 3 read the scope publisher's resolved scope instead
   (`fleet_scope_targets.list_scope_clusters`, #5621): the management and
   explicit projects are enumerated with `gcloud` as above, a member
   project is swept by the triples the publisher listed for it, and a
   member it holds no list for is a scan error, not an empty project. No
   Cluster Agent profile is read.

Where it runs. `leeway` is baked into `agent-sandbox` at `/usr/local/bin/leeway`
and routes Kubernetes object reads through the sandbox's `kubectl`
`credential-proxy` shim (`CREDENTIAL_PROXY_URL`). `leeway_watch.py` invokes
`leeway report --topology-tier-c-signals --parallelism 2 --timeout <n>s` in the
sandbox through `sandbox_exec.run` and parses the `leeway.report/v1alpha1` JSON
document from stdout. `--timeout` is leeway's per-cluster deadline (its own
default is one minute, which a large cluster can exceed); it is sized so every
cluster in the call can use its share of the call's sandbox timeout, capped so
one stuck cluster cannot spend the tick (`cluster_timeout_seconds`).

Batches and turns. A project's clusters (or `LEEWAY_WATCH_CLUSTERS`) are read
in calls of at most `CLUSTERS_PER_BATCH` (12), each with its own 15-minute
sandbox timeout (or what is left of the 45-minute tick), so a call that fails
costs only its own clusters. No call gives a cluster less than
`CONSERVATIVE_CLUSTER_READ_SECONDS` (two minutes), and the last deadline ends
`LEEWAY_OVERHEAD_SECONDS` or more before the sandbox would kill the call: a call
started late holds only the waves that fit (`batch_size`), and none starts
with less than one wave left. Clusters after that point are not reached this
run. Every cluster in a call is stamped in `cluster_runs` however the call
ended; the least recently stamped go first, and clusters stamped together are
shuffled by the run's date so batch membership changes from run to run.
Projects go least recently swept first (`project_runs`). A project whose turn
starts with a full call's time is stamped then, so it rotates behind the
others whatever the turn produces. Its turn ends at its first sandbox timeout,
so a project whose calls hang costs one call. A tick in which no call returned
a report still saves the stamps.

Scan errors and the partial-scan line. Each target (a project, or "the
configured clusters") has at most one entry. A target whose one call held all
its clusters and raised or timed out keeps that call's message; a target no
call started for keeps "tick budget exhausted before <target> could run",
which only the report shows, since a budget gap posts nothing. Any other is
summarised by counts, its parts joined by " — " (`p1: 72 of 100 clusters read
— 12 failed (<cause>) — 16 not reached (out of time this run)`). Error rows
are folded in, naming up to MAX_NAMED_ERROR_CLUSTERS clusters (`c00, c01 and
c02 returned an error (forbidden)`) and counting more (`4 clusters returned an
error`). A row is matched to its call's cluster by its `source`, then its
context name, then a short name no two of the call's clusters share; a row
that matches none is counted (`1 cluster returned an error`) and never named.
Only ok rows count as read. A target only the budget kept from being read in
full is not a failure: it posts nothing and is listed under the report's "Not
Reached This Run". The partial-scan line posts when a target fails that has
no open episode, naming the targets that failed this run (up to
MAX_NAMES_IN_LINE, then "+N more"). An episode remembers the clusters that
failed (in a call that raised or timed out, or as an error row) and closes
once each has returned an ok row since or left its project's list; a target
that failed as a whole (its list could not be read, or an error row matched
none of its clusters) closes once a call for it starts and nothing fails. The
recovery line posts when the last episode closes; a target that leaves the
sweep drops its episode without a line. An older ledger's signature of error
kinds stays until a run where nothing fails and nothing goes unread, which
posts the recovery line; until then a target failing with a kind it holds
posts nothing. When no call returned a report the tick fails with one fixed
sentence that names no target and no count, so the rotation does not change
the line and it posts once while the condition lasts; a run of one target
whose list failed, or whose one call held every cluster, keeps that target's
entry instead.

Threshold filtering and environment knobs. Before ledger gating,
`filter_report_findings` drops `unknown`-cause topology findings at their
pigeonhole floor (`observedSkew - minAchievableSkew < 1`, e.g. `[0, 1, 1]` or
`[1, 1, 2]` across three zones) by default while keeping attributed findings,
`selector_confined` single-zone pool confinement, `leeway.domain_unavailable`,
and stackable skews above the floor (`[4, 0]`). Findings still present in the
unfiltered `leeway` report but suppressed by active thresholds remain silently
in `ledger.json` rather than retiring as resolved. `LEEWAY_WATCH_MIN_SEVERITY`,
`LEEWAY_WATCH_MIN_DRIFT`, `LEEWAY_WATCH_MIN_MAX_DOMAIN_SHARE` (topology
findings), `LEEWAY_WATCH_MIN_EXCESS_OVER_FLOOR`, and
`LEEWAY_WATCH_REFRESH_DAYS` — together with `LEEWAY_WATCH_CLUSTERS`,
`LEEWAY_WATCH_PROJECTS`, and `LEEWAY_WATCH_HOME` — are optional overrides for
runs started by hand in the pod (pass `--dry-run` when probing narrower or wider
thresholds so `ledger.json` and `reports/latest.*` are not mutated); the
operator's `spec.deployment.env` allowlist does not carry them.

State ledger, blackboard topic, and 7-day reminder gate. Under
`<profile home>/leeway/`:
- `ledger.json`: tracks each active finding by its six-part key
  (`<cluster>/<namespace>/<subject_kind>/<subject_name>/<topology_key>/<finding_kind>`)
  with `first_seen`, `last_report_at`, `suspected_cause`, `severity`, `tier`,
  `max_domain_share`, and `observed_skew`; `project_runs` and `cluster_runs`,
  which order the sweep; and the targets with an open partial-scan episode.
- `reports/<timestamp>.md` and `reports/<timestamp>.json` (with `latest.md` and
  `latest.json` symlinks, keeping the newest 10 pairs): written whenever at
  least one finding is due, with that run's report, partial or not. Retired
  findings, a deferred refresh and a changed threshold override rewrite them
  only in a run that read every target in full without failing: a failure or
  clusters not reached hold that refresh back, though not reached posts
  nothing. On a fleet too big for one run, `latest.*` and the topic therefore
  change only when a finding comes due, and otherwise keep the previous report
  until a run reads every project in full. When
  `NATS_URL` is set, the JSON report is also published to the A2A blackboard
  topic `topology-skew` (`a2a.topics.agent.platform.topology-skew`), warning
  once in chat on failure and retrying on a later tick that reads every target
  in full without failing.
A finding is due for reporting when:
1. It is **new** (not present in `ledger.json`),
2. Its **suspected cause changed or severity escalated** since the last report,
   or
3. Its **7-day scheduled refresh** window has elapsed (`now - last_report_at >=
   7 days - 10m`).
Chat report lines name up to six due subjects with their cause and share.
Findings on clusters whose scan succeeded (`status == "ok"`) and that are no
longer present are retired from `ledger.json` with a single resolution line.
A finding on a cluster its project's cluster list no longer names retires once
that list was read. Clusters whose scan failed (`status == "error"`), that were
not reached, or whose project could not be scanned retain their existing ledger
entries so a transient outage does not erase state and re-page as new tomorrow.
A quiet tick prints nothing.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, overload

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fleet_scope_targets  # noqa: E402
import gitops_workspace  # noqa: E402
import sandbox_exec  # noqa: E402

# Where the job keeps its ledger and reports, under the agent home.
HOME_ENV = "HERMES_HOME"
DEFAULT_HOME = "/opt/data"
PROFILE_HOME = Path("profiles") / "platform"
WATCH_HOME_ENV = "LEEWAY_WATCH_HOME"
WATCH_DIR_NAME = "leeway"
LEDGER_FILE_NAME = "ledger.json"
LEDGER_TMP_SUFFIX = ".tmp"
LEDGER_SCHEMA_KEY = "schema_version"
LEDGER_SCHEMA_VERSION = 1
REPORTS_DIR_NAME = "reports"
REPORT_MARKDOWN_SUFFIX = ".md"
REPORT_JSON_SUFFIX = ".json"
REPORT_TIMESTAMP_FORMAT = "%Y%m%dT%H%M%SZ"
LATEST_MD_LINK_NAME = "latest.md"
LATEST_JSON_LINK_NAME = "latest.json"
REPORTS_KEPT = 10
JSON_INDENT = 2

# Refresh cadence: report once on appearance or cause/severity escalation, then
# once a week while the finding persists.
REFRESH_DAYS_ENV = "LEEWAY_WATCH_REFRESH_DAYS"
REFRESH_DAYS_DEFAULT = 7
REFRESH_DAYS_MAX = 365
REFRESH_SLACK_SECONDS = 10 * 60

# Alert filtering thresholds (optional environment overrides).
MIN_SEVERITY_ENV = "LEEWAY_WATCH_MIN_SEVERITY"
MIN_SEVERITY_DEFAULT = "info"
MIN_DRIFT_ENV = "LEEWAY_WATCH_MIN_DRIFT"
MIN_DRIFT_DEFAULT = 0.0
MIN_DRIFT_MAX = 1.0
MIN_MAX_DOMAIN_SHARE_ENV = "LEEWAY_WATCH_MIN_MAX_DOMAIN_SHARE"
MIN_MAX_DOMAIN_SHARE_DEFAULT = 0.0
MIN_MAX_DOMAIN_SHARE_MAX = 1.0
MIN_EXCESS_OVER_FLOOR_ENV = "LEEWAY_WATCH_MIN_EXCESS_OVER_FLOOR"
MIN_EXCESS_OVER_FLOOR_DEFAULT = 1
MIN_EXCESS_OVER_FLOOR_MAX = 10000
ZERO_FLOAT = 0.0
SCORE_ROUND_DECIMALS = 4
PERCENT_SCALE = 100

# Cluster and project discovery.
CLUSTERS_ENV = "LEEWAY_WATCH_CLUSTERS"
PROJECTS_ENV = "LEEWAY_WATCH_PROJECTS"
CSV_SEPARATOR = ","
MANAGEMENT_PROJECT_ENV = "GCP_PROJECT_ID"
CONFIG_PROJECT_ARGV = ("gcloud", "config", "get-value", "project")
PROJECT_LOOKUP_TIMEOUT_SECONDS = 30
NO_PROJECT_DETAIL = (
    f"no GCP project: set {CLUSTERS_ENV}, {PROJECTS_ENV}, or {MANAGEMENT_PROJECT_ENV}, "
    "onboard a cluster, or configure gcloud in the sandbox; "
    "refusing to let the sweep enumerate every project the credential can list"
)

# How `leeway report` runs in the sandbox. Every target is a
# `<project>/<location>/<cluster>` triple passed with `--clusters`: inside the
# sandbox leeway reads each one as `kubectl --context=gke_<project>_<location>_<cluster>`
# through the credential-proxy shim. `--clusters-from <project>` is not used
# there: it lists the project through the GKE API over the proxy's
# `/v1/gcp/container.googleapis.com/` route, which the proxy's api_policy does
# not relay. A project's clusters are enumerated with `gcloud container
# clusters list` instead, which the sandbox's gcloud shim does relay.
LEEWAY_EXECUTABLE = "leeway"
LEEWAY_REPORT_SUBCOMMAND = "report"
TIER_C_FLAG = "--topology-tier-c-signals"
PARALLELISM_FLAG = "--parallelism"
PARALLELISM = 2
PARALLELISM_VALUE = str(PARALLELISM)
CLUSTER_TIMEOUT_FLAG = "--timeout"
CLUSTER_TIMEOUT_FORMAT = "{seconds}s"
# leeway's own `--timeout` default (`DefaultClusterTimeout`), kept as the floor
# so a short call never gives a cluster less than leeway would on its own.
MIN_CLUSTER_TIMEOUT_SECONDS = 60
# The ceiling, so one cluster whose read stalls rather than fails spends at most
# this much of the tick: five projects each with one stalled cluster still fit
# TICK_BUDGET_SECONDS.
MAX_CLUSTER_TIMEOUT_SECONDS = 300
# Held back from the call's sandbox timeout, so the last per-cluster deadline
# fires this long before the sandbox kills the call. It covers the ssh hop
# into the sandbox, leeway's start-up under gVisor and its report assembly.
# Process start-up alone has measured over 30 s under gVisor on a busy node,
# so this is three times that rather than the bare start-up time.
LEEWAY_OVERHEAD_SECONDS = 90
CLUSTER_SEPARATOR = ","
CLUSTERS_FLAG = "--clusters"
LIST_CLUSTERS_ARGV = ("gcloud", "container", "clusters", "list", "--format=value(name,location)", "--project")
LIST_CLUSTERS_FAILED_DETAIL = "gcloud container clusters list --project {project} exited {code}: {error}"
LIST_CLUSTERS_TIMED_OUT_DETAIL = "gcloud container clusters list --project {project} timed out after {seconds}s"
# The sandbox timeout of one `leeway report` call.
PROJECT_TIMEOUT_SECONDS = 900
TICK_BUDGET_SECONDS = 2700
# A conservative wall time for one cluster's read: about twice the slowest
# read measured (a ~23k-pod cluster at 44-66 s without gVisor; small clusters
# read in 14-43 s each under gVisor).
CONSERVATIVE_CLUSTER_READ_SECONDS = 120
# Clusters per `leeway report` call: the waves of PARALLELISM that fit a full
# call at the conservative read time, (900 - 90) // 120 = 6 waves, so 12
# clusters with a 135 s `--timeout` each. A project of up to this many
# clusters is one call.
CLUSTERS_PER_BATCH = PARALLELISM * (
    (PROJECT_TIMEOUT_SECONDS - LEEWAY_OVERHEAD_SECONDS) // CONSERVATIVE_CLUSTER_READ_SECONDS
)
# The least a call is started with: one wave at the conservative read time
# plus the overhead. With less left, the rest waits for a later run.
MIN_PROJECT_RUN_SECONDS = CONSERVATIVE_CLUSTER_READ_SECONDS + LEEWAY_OVERHEAD_SECONDS
STDERR_EXCERPT_CHARS = 300
TIMED_OUT_DETAIL = "the sandbox run timed out after {seconds}s"
LEEWAY_NO_REPORT_DETAIL = "leeway exited {code} without a report: {excerpt}"
# The attribute a failed call's exception carries leeway's bare stderr on.
SHORT_CAUSE_ATTR = "short_cause"
BUDGET_EXHAUSTED_DETAIL = "tick budget exhausted before {target} could run"
SCAN_FAILED_DETAIL = "leeway scan for {target} failed: {error}"
# A target is one entry by counts, unless one call held all its clusters and
# raised. Causes are leeway's stderr line, cut to CAUSE_MAX_CHARS. Parts are
# joined by GAP_SEPARATOR, which the line's list of entries (", ") and its
# tail ("; ") do not use, so a reader sees where one target ends.
TARGET_SUMMARY_DETAIL = "{read} of {total} clusters read — {gaps}"
FAILED_PART = "{count} failed ({cause})"
CLUSTER_ERRORS_PART = "{count} clusters returned an error ({cause})"
ONE_CLUSTER_ERROR_PART = "1 cluster returned an error ({cause})"
# Up to this many clusters that returned an error row are named; past it, or
# when a row matched no cluster of its call, counted.
MAX_NAMED_ERROR_CLUSTERS = 3
NAMED_CLUSTER_ERRORS_PART = "{names} returned an error ({cause})"
NAMED_CLUSTERS_SEPARATOR = ", "
NAMED_CLUSTERS_LAST_SEPARATOR = " and "
NOT_REACHED_PART = "{count} not reached (out of time this run)"
TURN_ENDED_PART = "{count} not tried this run (reading stops at the first timeout)"
OTHER_CAUSES_SUFFIX = " + {count} other cause(s)"
GAP_SEPARATOR = " — "
# The tick-failure detail when no call returned, unless the run was one call
# (or one cluster-list read) for one target: which targets fail, their counts
# and their causes change with the rotation, so it names none of them and the
# line repeats word for word while the condition lasts.
NO_REPORT_DETAIL = "no cluster could be read this run (every read timed out or failed); it will try again next run"
# A cluster's kubeconfig context name, as leeway may print it on a row.
GKE_CONTEXT_FORMAT = "gke_{project}_{location}_{cluster}"
# The parts of a `<project>/<location>/<cluster>` triple.
TRIPLE_PART_COUNT = 3
CAUSE_MAX_CHARS = 160
CAUSE_ELLIPSIS = "…"
# How LEEWAY_WATCH_CLUSTERS is named when it takes more than one call.
EXPLICIT_TARGET_LABEL = "the configured clusters"
# A project the resolved scope names but holds no cluster list for (spec.mode: next).
UNREAD_PROJECT_DETAIL = "{target} was not swept: {reason}"

# Scan-error kinds. A budget_exhausted target was not reached this run: not a
# failure, so it posts nothing and holds no episode open.
ERROR_KIND_BUDGET_EXHAUSTED = "budget_exhausted"
ERROR_KIND_TIMEOUT = "timeout"
ERROR_KIND_SCAN_FAILED = "scan_failed"
ERROR_KIND_CLUSTER_ERROR = "cluster_error"
# Kinds an older ledger's `announced.partial` signature held for a budget gap,
# which is no failure under the per-target episodes.
LEGACY_NOT_REACHED_KINDS = frozenset({ERROR_KIND_BUDGET_EXHAUSTED, "cut_short"})

# `leeway.report/v1alpha1` schema vocabulary and normalization defaults.
SCHEMA_VERSION_VALUE = "leeway.report/v1alpha1"
STATUS_OK = "ok"
STATUS_ERROR = "error"
GKE_SOURCE_PREFIX = "gke:"
CLUSTER_KEY_SEPARATOR = "/"
CLUSTER_WIDE_NAMESPACE = "_cluster"
DEFAULT_SUBJECT_KIND_CLUSTER = "Cluster"
DEFAULT_SUBJECT_KIND_COMPUTE_CLASS = "ComputeClass"
DEFAULT_UNKNOWN = "unknown"
DEFAULT_DOMAIN_UNKNOWN = "?"
# A finding without a `kind` is malformed; it is keyed under "unknown" rather
# than under a real kind it never claimed. leeway's kinds are
# leeway.contract_violated / placement_drift / baseline_breach /
# domain_unavailable and leeway.rank_wedged / rank_degraded /
# rank_no_migration / rank_tier_unused (pkg/leeway/finding.go).
DEFAULT_FINDING_KIND_DRIFT = DEFAULT_UNKNOWN
DEFAULT_FINDING_KIND_RANK = DEFAULT_UNKNOWN
FINDING_KIND_DOMAIN_UNAVAILABLE = "leeway.domain_unavailable"
CAUSE_SELECTOR_CONFINED = "selector_confined"
# A rank finding's axis for the ledger key: its judging `rule` (`last-rank`,
# `rank0-floor`, ...), which is what distinguishes two findings on one class.
DEFAULT_TOPOLOGY_KEY_COMPUTE_CLASS = "compute-class"
DEFAULT_SEVERITY = "warning"
DEFAULT_DISPLAY_NA = "n/a"
DEFAULT_CLUSTER_SCAN_FAILED = "cluster scan failed"
DEFAULT_NO_OUTPUT = "no output"
CLUSTERS_FIELD = "clusters"
STATUS_FIELD = "status"
ERROR_FIELD = "error"
TOPOLOGY_FINDINGS_FIELD = "topology_findings"
RANK_FINDINGS_FIELD = "rank_findings"
SUMMARY_FIELD = "summary"
CLUSTERS_TOTAL_FIELD = "clusters_total"
CLUSTERS_SUCCEEDED_FIELD = "clusters_succeeded"
CLUSTERS_FAILED_FIELD = "clusters_failed"
BY_SEVERITY_FIELD = "by_severity"
BY_TIER_FIELD = "by_tier"
SCORE_FIELD = "score"
OBSERVED_SKEW_RAW_FIELD = "observedSkew"
MIN_ACHIEVABLE_SKEW_FIELD = "minAchievableSkew"
DRIFT_FIELD = "drift"
MAX_DOMAIN_SHARE_RAW_FIELD = "maxDomainShare"
KEY_FIELD = "key"
CLUSTER_FIELD = "cluster"
SOURCE_FIELD = "source"
KIND_FIELD = "kind"
NAMESPACE_FIELD = "namespace"
SUBJECT_KIND_FIELD = "subject_kind"
SUBJECT_NAME_FIELD = "subject_name"
SEVERITY_FIELD = "severity"
TIER_FIELD = "tier"
SUSPECTED_CAUSE_FIELD = "suspected_cause"
OBSERVED_SKEW_FIELD = "observed_skew"
EXCESS_OVER_FLOOR_KEY = "excess_over_floor"
DRIFT_KEY = "drift"
HAS_DRIFT_KEY = "has_drift"
MAX_DOMAIN_SHARE_KEY = "max_domain_share"
HAS_MAX_DOMAIN_SHARE_KEY = "has_max_domain_share"

# A2A blackboard topic (`a2a.topics.agent.platform.topology-skew`).
NATS_URL_ENV = "NATS_URL"
A2A_EXECUTABLE = "a2a"
A2A_TOPICS_SUBCOMMAND = "topics"
A2A_WRITE_VERB = "write"
A2A_TEXT_FLAG = "--text"
A2A_DATA_FLAG = "--data"
A2A_DATA_FILE_PREFIX = "@"
A2A_TOPIC_NAME = "topology-skew"
A2A_TOPIC_SUBJECT = "a2a.topics.agent.platform.topology-skew"
A2A_PUBLISH_TIMEOUT_SECONDS = 30
A2A_PUBLISH_FAILED_DETAIL = "a2a topics write {topic} exited {code}: {error}"
A2A_PUBLISH_TIMED_OUT_DETAIL = "TimeoutExpired: a2a topics write {topic} timed out after {seconds}s"
A2A_PUBLISH_EXCEPTION_DETAIL = "{error_type}: {error}"
A2A_RETRY_SUMMARY = "{prefix}: topology-skew blackboard report refreshed"
ERROR_KIND_TOPIC_PUBLISH = "topic_publish_failed"
SPACE_CHAR = " "
BACKTICK_CHAR = "`"
SINGLE_QUOTE_CHAR = "'"

# Severity ordering for escalation detection.
SEVERITY_RANKS: dict[str, int] = {
    "info": 1,
    "warning": 2,
    "critical": 3,
}

# Trigger reasons.
REASON_NEW = "new finding"
REASON_CHANGED = "cause/severity change"
REASON_REFRESH = "scheduled refresh"
REASON_ALL_RESOLVED = "all findings resolved"

# Ledger keys.
FINDINGS_KEY = "findings"
FIRST_SEEN_KEY = "first_seen"
LAST_REPORT_KEY = "last_report_at"
LAST_TICK_KEY = "last_tick"
PROJECT_RUNS_KEY = "project_runs"
# When each cluster was last in a `leeway report` call, however it ended.
CLUSTER_RUNS_KEY = "cluster_runs"
SCAN_ERRORS_KEY = "scan_errors"
ANNOUNCED_KEY = "announced"
# An older ledger's partial-scan signature: a list of error kinds.
ANNOUNCED_PARTIAL_KEY = "partial"
# The targets whose failure was announced and has not read cleanly since, each
# mapped to the clusters that failed (a call that raised or timed out, or an
# error row), or to null when the target failed as a whole (its cluster list
# could not be read). An older ledger holds a plain list of targets.
ANNOUNCED_FAILED_TARGETS_KEY = "failed_targets"
ANNOUNCED_TOPIC_PUBLISH_KEY = "topic_publish"
ANNOUNCED_DEFERRED_REFRESH_KEY = "deferred_report_refresh"
ANNOUNCED_OVERRIDE_THRESHOLDS_KEY = "override_report_thresholds"
FAILURE_MARKER_FILE_NAME = "last-failure.txt"

# Output lines (stdout is `deliver: "chat"`).
LINE_PREFIX = "leeway"
RECOVERED_LINE = "{prefix} watch: the tick runs again"
FAILED_LINE = "{prefix} watch: {what} failed: {detail}"
FAILED_WHAT_TICK = "the tick"
FAILED_WHAT_WRITE = "writing the ledger or the report"
RETIRED_LINE = "{prefix}: {count} finding(s) resolved ({names}); retired from the watch"
REPORT_LINE = (
    "{prefix}: {due_count} finding(s) due ({reasons}) across {cluster_count} cluster(s) ({clusters}): {names}; "
    "report on the gateway pod at {path}; next refresh after {next_date}"
)
TOPIC_PUBLISH_FAILED_LINE = (
    "{prefix} watch: publishing to blackboard topic {topic} failed ({detail}); "
    "on upgraded installs, check `kubectl describe platformagent` for TopicsStateSubjectMissing "
    "and append {subject} with `nats stream edit TOPICS-STATE`"
)
TOPIC_PUBLISH_CLEARED_LINE = "{prefix} watch: publishing to blackboard topic {topic} succeeded again"
PARTIAL_LINE = (
    "{prefix} watch: the fleet scan was partial ({errors} error(s)): {reasons}; "
    "findings on unreadable clusters are retained until read again"
)
PARTIAL_CLEARED_LINE = "{prefix} watch: every target that failed reads cleanly again"
REPORT_NOT_REACHED_HEADING = "## Not Reached This Run"
REPORT_NOT_REACHED_INTRO = (
    "The run's time ran out before these were read. Their unread clusters go first on their next turn, "
    "and their findings are kept until they are read."
)
DRY_RUN_WOULD_REPORT = "dry run: would report {count} finding(s) ({reasons}) across {clusters}: {names}"
DRY_RUN_WOULD_RETIRE = "dry run: would retire {count} resolved finding(s) ({names})"
DRY_RUN_NOTHING_DUE = "dry run: nothing due; active findings: {count}"
DUE_SUBJECT_NAMESPACED = "{kind}/{namespace}/{name}"
DUE_SUBJECT_CLUSTER = "{kind}/{name}"
DUE_SUMMARY_WITH_SHARE = "{subject} [{cause}, {pct}%]"
DUE_SUMMARY_NO_SHARE = "{subject} [{cause}]"
DUE_CLUSTER_PREFIX = "{cluster}:{summary}"
REASON_COUNT_FORMAT = "{count} {reason}"
LIST_SEPARATOR = ", "
MAX_NAMES_IN_LINE = 6
NAMES_OVERFLOW = ", +{more} more"
WRITE_FAILED_EXIT = 2
DATE_FORMAT = "%Y-%m-%d"


class SandboxTimedOut(RuntimeError):
    """The sandbox run hit the client-side timeout."""


class NoReportReturned(RuntimeError):
    """No call returned a report; its message is the whole tick-failure detail."""


class WriteFailed(Exception):
    """The ledger or a report file could not be written; the tick's result must not be claimed."""


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(moment: datetime) -> str:
    return moment.isoformat()


def parse_iso(value: object) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def refresh_days() -> int:
    raw = os.environ.get(REFRESH_DAYS_ENV, "").strip()
    try:
        value = int(raw)
    except ValueError:
        return REFRESH_DAYS_DEFAULT
    return value if 0 < value <= REFRESH_DAYS_MAX else REFRESH_DAYS_DEFAULT


def min_severity_rank() -> int:
    raw = os.environ.get(MIN_SEVERITY_ENV, "").strip().lower()
    return SEVERITY_RANKS.get(raw, SEVERITY_RANKS[MIN_SEVERITY_DEFAULT])


def min_drift() -> float:
    raw = os.environ.get(MIN_DRIFT_ENV, "").strip()
    if not raw:
        return MIN_DRIFT_DEFAULT
    try:
        value = float(raw)
    except ValueError:
        return MIN_DRIFT_DEFAULT
    return value if math.isfinite(value) and ZERO_FLOAT <= value <= MIN_DRIFT_MAX else MIN_DRIFT_DEFAULT


def min_max_domain_share() -> float:
    raw = os.environ.get(MIN_MAX_DOMAIN_SHARE_ENV, "").strip()
    if not raw:
        return MIN_MAX_DOMAIN_SHARE_DEFAULT
    try:
        value = float(raw)
    except ValueError:
        return MIN_MAX_DOMAIN_SHARE_DEFAULT
    return (
        value
        if math.isfinite(value) and ZERO_FLOAT <= value <= MIN_MAX_DOMAIN_SHARE_MAX
        else MIN_MAX_DOMAIN_SHARE_DEFAULT
    )


def min_excess_over_floor() -> int:
    raw = os.environ.get(MIN_EXCESS_OVER_FLOOR_ENV, "").strip()
    if not raw:
        return MIN_EXCESS_OVER_FLOOR_DEFAULT
    try:
        value = int(raw)
    except ValueError:
        return MIN_EXCESS_OVER_FLOOR_DEFAULT
    return value if 0 <= value <= MIN_EXCESS_OVER_FLOOR_MAX else MIN_EXCESS_OVER_FLOOR_DEFAULT


def profile_home() -> Path:
    """The platform profile's home: HERMES_HOME itself under the ticker, or the
    profile directory beneath it from a shell in the container."""
    home = Path(os.environ.get(HOME_ENV, DEFAULT_HOME))
    beneath = home / PROFILE_HOME
    return beneath if beneath.is_dir() else home


def watch_home() -> Path:
    override = os.environ.get(WATCH_HOME_ENV, "").strip()
    if override:
        return Path(override)
    return profile_home() / WATCH_DIR_NAME


# --- cluster & project discovery ---------------------------------------------


def explicit_clusters() -> list[str]:
    """Explicit cluster triples from LEEWAY_WATCH_CLUSTERS, when set."""
    raw = os.environ.get(CLUSTERS_ENV, "").strip()
    if not raw:
        return []
    return sorted({c.strip() for c in raw.split(CSV_SEPARATOR) if c.strip()})


def roster_clusters_by_project() -> dict[str, list[str]]:
    """Map each project to its sorted `<project>/<location>/<cluster>` triples
    from Cluster Agent profiles: the profiles walk
    `fleet_scope_targets.list_scope_clusters` makes outside spec.mode: next.
    A project only incomplete identities name maps to no triple."""
    scope = fleet_scope_targets.list_scope_clusters(gitops_workspace.agent_home())
    return {entry.project: [CLUSTER_KEY_SEPARATOR.join(t) for t in entry.clusters or ()] for entry in scope.projects}


def roster_projects() -> set[str]:
    """The projects the Cluster Agent profiles' identities name."""
    return set(roster_clusters_by_project().keys())


def declared_scope(agent_home: str | os.PathLike | None = None) -> fleet_scope_targets.ScopeTargets | None:
    """The install's declared scope as the reconcile resolved it, or None on an install
    that declares none. An unreadable scope render is a fault that must fail the tick,
    not an undeclared install."""
    return fleet_scope_targets.declared_scope_targets(agent_home)


def excluded_clusters(scope: fleet_scope_targets.ScopeTargets | None = None) -> set[str]:
    """`<project>/<location>/<cluster>` triples that `spec.scope.exclude.clusters` leaves out."""
    targets = scope if scope is not None else declared_scope()
    if targets is None or not targets.excluded_clusters:
        return set()
    return {"/".join(ref) for ref in targets.excluded_clusters}


def projects(scope: fleet_scope_targets.ScopeTargets | None = None) -> list[str]:
    """The projects to sweep: from the declared scope when present, else from
    LEEWAY_WATCH_PROJECTS, Cluster Agent profiles, or the management project."""
    targets = scope if scope is not None else declared_scope()
    explicit = [p.strip() for p in os.environ.get(PROJECTS_ENV, "").split(CSV_SEPARATOR) if p.strip()]
    if targets is not None:
        # A declared scope is in force: sweep its resolved projects.
        if explicit:
            return sorted(set(explicit) & set(targets.projects))
        return list(targets.projects)

    if explicit:
        return sorted(set(explicit))
    found = roster_projects()
    management = os.environ.get(MANAGEMENT_PROJECT_ENV, "").strip()
    if management:
        found.add(management)
    if found:
        return sorted(found)
    try:
        completed = sandbox_exec.run(list(CONFIG_PROJECT_ARGV), timeout=PROJECT_LOOKUP_TIMEOUT_SECONDS, check=False)
    except subprocess.TimeoutExpired:
        raise RuntimeError(TIMED_OUT_DETAIL.format(seconds=PROJECT_LOOKUP_TIMEOUT_SECONDS)) from None
    configured = (completed.stdout or "").strip()
    if not configured:
        raise RuntimeError(NO_PROJECT_DETAIL)
    return [configured]


def order_by_least_recent(items: list[str], stamps: dict[str, Any]) -> list[str]:
    """Order targets so those never run or run longest ago go first."""
    return sorted(items, key=lambda item: (str(stamps.get(item) or ""), item))


# --- sandbox execution -------------------------------------------------------


def cluster_timeout_seconds(call_timeout: int, cluster_count: int) -> int:
    """leeway's per-cluster `--timeout` for a call of `cluster_count` clusters.

    leeway reads `PARALLELISM` clusters at a time, so the call runs in
    `ceil(cluster_count / PARALLELISM)` waves; each wave gets an equal share of
    the call's sandbox timeout, less `LEEWAY_OVERHEAD_SECONDS`, between
    `MIN_CLUSTER_TIMEOUT_SECONDS` and `MAX_CLUSTER_TIMEOUT_SECONDS`. A slow
    cluster then ends as a per-cluster `error` row instead of the sandbox
    killing the whole call and discarding the clusters that finished.
    `batch_size` keeps every call above the floor, so the waves' deadlines end
    at least LEEWAY_OVERHEAD_SECONDS inside the call's sandbox timeout.
    """
    waves = math.ceil(max(cluster_count, 1) / PARALLELISM)
    share = (call_timeout - LEEWAY_OVERHEAD_SECONDS) // waves
    return max(MIN_CLUSTER_TIMEOUT_SECONDS, min(MAX_CLUSTER_TIMEOUT_SECONDS, share))


def run_leeway_report(flag: str, value: str, *, timeout: int = PROJECT_TIMEOUT_SECONDS) -> dict[str, Any]:
    """Run `leeway report --topology-tier-c-signals --parallelism 2 --timeout <n>s <flag> <value>` in the sandbox."""
    per_cluster = cluster_timeout_seconds(timeout, len(value.split(CLUSTER_SEPARATOR)))
    argv = [
        LEEWAY_EXECUTABLE,
        LEEWAY_REPORT_SUBCOMMAND,
        TIER_C_FLAG,
        PARALLELISM_FLAG,
        PARALLELISM_VALUE,
        CLUSTER_TIMEOUT_FLAG,
        CLUSTER_TIMEOUT_FORMAT.format(seconds=per_cluster),
        flag,
        value,
    ]
    try:
        completed = sandbox_exec.run(argv, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        raise SandboxTimedOut(TIMED_OUT_DETAIL.format(seconds=timeout)) from None
    stdout = (completed.stdout or "").strip()
    if not stdout:
        stderr = (completed.stderr or "").strip()
        excerpt = stderr[-STDERR_EXCERPT_CHARS:] if stderr else DEFAULT_NO_OUTPUT
        error = RuntimeError(LEEWAY_NO_REPORT_DETAIL.format(code=completed.returncode, excerpt=excerpt))
        setattr(error, SHORT_CAUSE_ATTR, excerpt)
        raise error
    try:
        report = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"leeway output was not valid JSON: {exc}") from exc
    if not isinstance(report, dict) or not isinstance(report.get("clusters"), list):
        raise RuntimeError("leeway report JSON is missing the 'clusters' list")
    return report


def project_cluster_triples(project: str, *, timeout: int = PROJECT_LOOKUP_TIMEOUT_SECONDS) -> list[str]:
    """`<project>/<location>/<cluster>` for every cluster `gcloud` lists in the project.

    Read in the sandbox, where the gcloud shim relays `container clusters list`
    and the GKE API route leeway's own `--clusters-from <project>` would take is
    not relayed. `--format=value(name,location)` prints one tab-separated
    `name location` pair per line; anything else on a line is skipped.
    """
    argv = [*LIST_CLUSTERS_ARGV, project]
    try:
        completed = sandbox_exec.run(argv, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        raise SandboxTimedOut(TIMED_OUT_DETAIL.format(seconds=timeout)) from None
    if completed.returncode != 0:
        stderr = (completed.stderr or "").strip()
        raise RuntimeError(
            LIST_CLUSTERS_FAILED_DETAIL.format(
                project=project,
                code=completed.returncode,
                error=stderr[-STDERR_EXCERPT_CHARS:] if stderr else "no output",
            )
        )
    triples: set[str] = set()
    for line in (completed.stdout or "").splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        name, location = parts
        triples.add(CLUSTER_KEY_SEPARATOR.join([project, location, name]))
    return sorted(triples)


def canonical_cluster_name(cr: dict[str, Any]) -> str:
    """Return `<project>/<location>/<cluster>` from `cr['source']` when present, else `cr['cluster']`."""
    raw_source = str(cr.get("source") or "").strip()
    if raw_source.startswith(GKE_SOURCE_PREFIX):
        triple = raw_source[len(GKE_SOURCE_PREFIX) :].strip()
        if triple.count(CLUSTER_KEY_SEPARATOR) == 2:
            return triple
    return str(cr.get("cluster") or "").strip()


def merge_reports(reports: list[dict[str, Any]], now: datetime) -> dict[str, Any]:
    """Combine per-project `leeway.report/v1alpha1` documents into one."""
    clusters_by_name: dict[str, dict[str, Any]] = {}
    for rep in reports:
        for cr in rep.get("clusters", []):
            if not isinstance(cr, dict):
                continue
            cname = canonical_cluster_name(cr)
            if cname:
                normalized_cr = dict(cr)
                normalized_cr["cluster"] = cname
                clusters_by_name[cname] = normalized_cr
    merged_clusters = [clusters_by_name[k] for k in sorted(clusters_by_name)]
    succeeded = 0
    failed = 0
    topo_total = 0
    rank_total = 0
    by_severity: dict[str, int] = {}
    by_tier: dict[str, int] = {}
    for cr in merged_clusters:
        if cr.get("status") == STATUS_OK:
            succeeded += 1
        else:
            failed += 1
        t_findings = cr.get("topology_findings") or []
        r_findings = cr.get("rank_findings") or []
        topo_total += len(t_findings)
        rank_total += len(r_findings)
        for f in list(t_findings) + list(r_findings):
            if not isinstance(f, dict):
                continue
            sev = str(f.get("severity") or "")
            tier = str(f.get("tier") or "")
            if sev:
                by_severity[sev] = by_severity.get(sev, 0) + 1
            if tier:
                by_tier[tier] = by_tier.get(tier, 0) + 1
    summary: dict[str, Any] = {
        "clusters_total": len(merged_clusters),
        "clusters_succeeded": succeeded,
        "clusters_failed": failed,
        "topology_findings": topo_total,
        "rank_findings": rank_total,
    }
    if by_severity:
        summary["by_severity"] = by_severity
    if by_tier:
        summary["by_tier"] = by_tier
    return {
        "schema_version": SCHEMA_VERSION_VALUE,
        "generated_at": iso(now),
        "clusters": merged_clusters,
        "summary": summary,
    }


# --- findings extraction & gate ----------------------------------------------


@overload
def _int(value: object, default: int = 0) -> int: ...


@overload
def _int(value: object, default: None) -> int | None: ...


def _int(value: object, default: int | None = 0) -> int | None:
    if value is None or isinstance(value, bool):
        return default
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return default


@overload
def _float(value: object, default: float = 0.0) -> float: ...


@overload
def _float(value: object, default: None) -> float | None: ...


def _float(value: object, default: float | None = 0.0) -> float | None:
    if value is None or isinstance(value, bool):
        return default
    try:
        parsed = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return default
    return parsed if math.isfinite(parsed) and math.isfinite(parsed * 100.0) else default


def _str_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(v) for v in value if v is not None and str(v).strip()]


def normalize_topology_finding(cluster: str, finding: dict[str, Any]) -> dict[str, Any]:
    """Normalize a single topology_findings entry with its canonical ledger key."""
    subject = finding.get("subject") if isinstance(finding.get("subject"), dict) else {}
    ns = str(subject.get("namespace") or CLUSTER_WIDE_NAMESPACE)
    s_kind = str(subject.get("kind") or DEFAULT_SUBJECT_KIND_CLUSTER)
    s_name = str(subject.get("name") or finding.get("kind") or DEFAULT_UNKNOWN)
    replicas = _int(subject.get("replicas"), 0)
    topo_key = str(finding.get("topologyKey") or DEFAULT_UNKNOWN)
    f_kind = str(finding.get("kind") or DEFAULT_FINDING_KIND_DRIFT)
    key = CLUSTER_KEY_SEPARATOR.join([cluster, ns, s_kind, s_name, topo_key, f_kind])

    score = finding.get(SCORE_FIELD) if isinstance(finding.get(SCORE_FIELD), dict) else {}
    domains = finding.get("domains") if isinstance(finding.get("domains"), list) else []
    max_actual = 0
    total_actual = 0
    domain_parts: list[str] = []
    for d in domains:
        if not isinstance(d, dict):
            continue
        d_name = str(d.get("domain") or DEFAULT_DOMAIN_UNKNOWN)
        actual = _int(d.get("actual"), 0)
        if actual > max_actual:
            max_actual = actual
        total_actual += actual
        domain_parts.append(f"{d_name}: {actual}")
    denom = replicas if replicas > 0 else total_actual
    raw_share_opt = _float(score.get(MAX_DOMAIN_SHARE_RAW_FIELD), None)
    fallback_share = max_actual / denom if denom > 0 else 0.0
    max_share = raw_share_opt if raw_share_opt is not None else fallback_share
    has_max_share = raw_share_opt is not None or denom > 0
    blast_pct = round(max_share * 100)
    blast_radius = f"{max_actual}/{denom} ({blast_pct}%)" if denom > 0 else DEFAULT_DISPLAY_NA

    raw_observed_skew = _int(score.get(OBSERVED_SKEW_RAW_FIELD), None)
    raw_min_skew = _int(score.get(MIN_ACHIEVABLE_SKEW_FIELD), None)
    raw_drift = _float(score.get(DRIFT_FIELD), None)
    remediation = finding.get("remediation") if isinstance(finding.get("remediation"), dict) else {}
    return {
        "key": key,
        "cluster": cluster,
        "kind": f_kind,
        "namespace": ns,
        "subject_kind": s_kind,
        "subject_name": s_name,
        "topology_key": topo_key,
        "tier": str(finding.get("tier") or ""),
        "severity": str(finding.get("severity") or DEFAULT_SEVERITY),
        "suspected_cause": str(finding.get("suspectedCause") or DEFAULT_UNKNOWN),
        "contributing_factors": _str_list(finding.get("contributingFactors")),
        "observed_skew": _int(raw_observed_skew, 0),
        EXCESS_OVER_FLOOR_KEY: (
            raw_observed_skew - raw_min_skew
            if raw_observed_skew is not None and raw_min_skew is not None
            else None
        ),
        DRIFT_KEY: round(raw_drift if raw_drift is not None else ZERO_FLOAT, SCORE_ROUND_DECIMALS),
        HAS_DRIFT_KEY: raw_drift is not None,
        MAX_DOMAIN_SHARE_KEY: round(max_share, SCORE_ROUND_DECIMALS),
        HAS_MAX_DOMAIN_SHARE_KEY: has_max_share,
        "distribution_display": ", ".join(domain_parts) if domain_parts else DEFAULT_DISPLAY_NA,
        "blast_radius_display": blast_radius,
        "remediation": {
            "kind": str(remediation.get("kind") or ""),
            "summary": str(remediation.get("summary") or ""),
            "yaml": str(remediation.get("yaml") or ""),
        },
    }


def normalize_rank_finding(cluster: str, finding: dict[str, Any]) -> dict[str, Any]:
    """Normalize a single rank_findings entry with its canonical ledger key.

    A `leeway.RankFinding` (pkg/leeway/rankfinding.go) carries `subject`
    (`{kind: "PreferenceAxis", name: <class>}`), `objectKind` (`ComputeClass`),
    `rule` (the judging rule, e.g. `last-rank`), `observed` (`rank0Share`,
    `lastRank`, `lastRankShare`, `meanRank`, `pendingPods`, ...), `ranks[]`
    (`rank`, `rules`, `share`, `pods`) and `reason`; it has no
    `suspectedCause`, `contributingFactors` or `remediation`. The rule stands
    in for the cause in the ledger's change detection, and the last-rank share
    -- the fraction of the window the class spent on its last-resort rank --
    stands in for the blast-radius share.
    """
    subject = finding.get("subject") if isinstance(finding.get("subject"), dict) else {}
    cc_name = str(subject.get("name") or DEFAULT_UNKNOWN)
    s_kind = str(finding.get("objectKind") or DEFAULT_SUBJECT_KIND_COMPUTE_CLASS)
    f_kind = str(finding.get("kind") or DEFAULT_FINDING_KIND_RANK)
    rule = str(finding.get("rule") or "")
    topo_key = f"rule:{rule}" if rule else DEFAULT_TOPOLOGY_KEY_COMPUTE_CLASS
    key = CLUSTER_KEY_SEPARATOR.join([cluster, CLUSTER_WIDE_NAMESPACE, s_kind, cc_name, topo_key, f_kind])
    observed = finding.get("observed") if isinstance(finding.get("observed"), dict) else {}
    last_rank = _int(observed.get("lastRank"), 0)
    last_rank_share = _float(observed.get("lastRankShare"), 0.0)
    rank0_share = _float(observed.get("rank0Share"), 0.0)
    mean_rank = _float(observed.get("meanRank"), 0.0)
    pending = _int(observed.get("pendingPods"), 0)
    ranks = finding.get("ranks") if isinstance(finding.get("ranks"), list) else []
    rank_parts = [
        f"rank {_int(r.get('rank'), 0)} ({r.get('rules') or 'no rules'}): {round(_float(r.get('share'), 0.0) * 100)}%"
        for r in ranks
        if isinstance(r, dict)
    ]
    reason = str(finding.get("reason") or "").strip()
    return {
        "key": key,
        "cluster": cluster,
        "kind": f_kind,
        "namespace": CLUSTER_WIDE_NAMESPACE,
        "subject_kind": s_kind,
        "subject_name": cc_name,
        "topology_key": topo_key,
        "tier": str(finding.get("tier") or ""),
        "severity": str(finding.get("severity") or DEFAULT_SEVERITY),
        "suspected_cause": rule or DEFAULT_UNKNOWN,
        "contributing_factors": [reason] if reason else [],
        "observed_skew": last_rank,
        "max_domain_share": round(last_rank_share, 4),
        "distribution_display": (
            ", ".join(rank_parts)
            if rank_parts
            else f"rank0Share={round(rank0_share * 100)}%, lastRankShare={round(last_rank_share * 100)}%, meanRank={mean_rank:g}"
        ),
        "blast_radius_display": f"lastRankShare {round(last_rank_share * 100)}%" + (f", {pending} pending" if pending else ""),
        "remediation": {"kind": "", "summary": "", "yaml": ""},
    }


def passes_topology_thresholds(
    norm: dict[str, Any],
    *,
    min_sev_rank: int | None = None,
    min_d: float | None = None,
    min_share: float | None = None,
    min_excess: int | None = None,
) -> bool:
    """Return True when a normalized topology finding passes configured alert thresholds."""
    sev_floor = min_severity_rank() if min_sev_rank is None else min_sev_rank
    if SEVERITY_RANKS.get(norm[SEVERITY_FIELD], SEVERITY_RANKS[DEFAULT_SEVERITY]) < sev_floor:
        return False
    if norm[KIND_FIELD] == FINDING_KIND_DOMAIN_UNAVAILABLE:
        return True
    excess_floor = min_excess_over_floor() if min_excess is None else min_excess
    excess = norm.get(EXCESS_OVER_FLOOR_KEY)
    if (
        norm[SUSPECTED_CAUSE_FIELD] == DEFAULT_UNKNOWN
        and excess is not None
        and excess < excess_floor
    ):
        return False
    drift_floor = min_drift() if min_d is None else min_d
    if (
        norm[SUSPECTED_CAUSE_FIELD] != CAUSE_SELECTOR_CONFINED
        and norm[HAS_DRIFT_KEY]
        and norm[DRIFT_KEY] < drift_floor
    ):
        return False
    share_floor = min_max_domain_share() if min_share is None else min_share
    if norm[HAS_MAX_DOMAIN_SHARE_KEY] and norm[MAX_DOMAIN_SHARE_KEY] < share_floor:
        return False
    return True


def passes_rank_thresholds(norm: dict[str, Any], *, min_sev_rank: int | None = None) -> bool:
    """Return True when a normalized rank finding passes configured alert thresholds.

    ComputeClass rank findings (`rank_wedged`, `rank0-share` on >=3-tier classes,
    `rank_no_migration`) can legitimately have `lastRankShare == 0.0`, so
    `LEEWAY_WATCH_MIN_MAX_DOMAIN_SHARE` applies to topology findings only.
    """
    sev_floor = min_severity_rank() if min_sev_rank is None else min_sev_rank
    return SEVERITY_RANKS.get(norm[SEVERITY_FIELD], SEVERITY_RANKS[DEFAULT_SEVERITY]) >= sev_floor


def filter_report_findings(report: dict[str, Any]) -> dict[str, Any]:
    """Filter a merged report's findings and summary through the active alert thresholds."""
    min_sev_rank = min_severity_rank()
    min_d = min_drift()
    min_share = min_max_domain_share()
    min_excess = min_excess_over_floor()
    filtered_clusters: list[dict[str, Any]] = []
    succeeded = 0
    failed = 0
    topo_total = 0
    rank_total = 0
    by_severity: dict[str, int] = {}
    by_tier: dict[str, int] = {}
    for cr in report.get(CLUSTERS_FIELD, []):
        if not isinstance(cr, dict):
            continue
        cluster = canonical_cluster_name(cr)
        entry = dict(cr)
        if entry.get(STATUS_FIELD) == STATUS_OK:
            succeeded += 1
            kept_topo: list[dict[str, Any]] = []
            for tf in entry.get(TOPOLOGY_FINDINGS_FIELD) or []:
                if isinstance(tf, dict) and passes_topology_thresholds(
                    normalize_topology_finding(cluster, tf),
                    min_sev_rank=min_sev_rank,
                    min_d=min_d,
                    min_share=min_share,
                    min_excess=min_excess,
                ):
                    kept_topo.append(tf)
            kept_rank: list[dict[str, Any]] = []
            for rf in entry.get(RANK_FINDINGS_FIELD) or []:
                if isinstance(rf, dict) and passes_rank_thresholds(
                    normalize_rank_finding(cluster, rf),
                    min_sev_rank=min_sev_rank,
                ):
                    kept_rank.append(rf)
            entry[TOPOLOGY_FINDINGS_FIELD] = kept_topo
            entry[RANK_FINDINGS_FIELD] = kept_rank
            topo_total += len(kept_topo)
            rank_total += len(kept_rank)
            for item in kept_topo + kept_rank:
                sev = str(item.get(SEVERITY_FIELD) or "")
                tier = str(item.get(TIER_FIELD) or "")
                if sev:
                    by_severity[sev] = by_severity.get(sev, 0) + 1
                if tier:
                    by_tier[tier] = by_tier.get(tier, 0) + 1
        else:
            failed += 1
        filtered_clusters.append(entry)
    summary: dict[str, Any] = {
        CLUSTERS_TOTAL_FIELD: len(filtered_clusters),
        CLUSTERS_SUCCEEDED_FIELD: succeeded,
        CLUSTERS_FAILED_FIELD: failed,
        TOPOLOGY_FINDINGS_FIELD: topo_total,
        RANK_FINDINGS_FIELD: rank_total,
    }
    if by_severity:
        summary[BY_SEVERITY_FIELD] = by_severity
    if by_tier:
        summary[BY_TIER_FIELD] = by_tier
    updated = dict(report)
    updated[CLUSTERS_FIELD] = filtered_clusters
    updated[SUMMARY_FIELD] = summary
    return updated


def extract_findings(
    report: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], set[str], dict[str, str]]:
    """Extract normalized findings, ok clusters, and per-cluster errors from a report."""
    findings: dict[str, dict[str, Any]] = {}
    ok_clusters: set[str] = set()
    cluster_errors: dict[str, str] = {}
    for cr in report.get(CLUSTERS_FIELD, []):
        if not isinstance(cr, dict):
            continue
        cluster = canonical_cluster_name(cr)
        if not cluster:
            continue
        if cr.get(STATUS_FIELD) == STATUS_OK:
            ok_clusters.add(cluster)
            for tf in cr.get(TOPOLOGY_FINDINGS_FIELD) or []:
                if isinstance(tf, dict):
                    norm = normalize_topology_finding(cluster, tf)
                    findings[norm[KEY_FIELD]] = norm
            for rf in cr.get(RANK_FINDINGS_FIELD) or []:
                if isinstance(rf, dict):
                    norm = normalize_rank_finding(cluster, rf)
                    findings[norm[KEY_FIELD]] = norm
        else:
            cluster_errors[cluster] = str(cr.get(ERROR_FIELD) or DEFAULT_CLUSTER_SCAN_FAILED)
    return findings, ok_clusters, cluster_errors


def finding_trigger_reason(
    item: dict[str, Any],
    prior: dict[str, Any] | None,
    now: datetime,
    days: int,
) -> str | None:
    """Return why a finding is due to be reported on this tick, or None if not due."""
    if not prior or not isinstance(prior, dict):
        return REASON_NEW
    prior_cause = str(prior.get("suspected_cause") or "")
    prior_sev = str(prior.get("severity") or "")
    cur_cause = str(item.get("suspected_cause") or "")
    cur_sev = str(item.get("severity") or "")
    if cur_cause != prior_cause or SEVERITY_RANKS.get(cur_sev, 0) > SEVERITY_RANKS.get(prior_sev, 0):
        return REASON_CHANGED
    last_reported = parse_iso(prior.get(LAST_REPORT_KEY))
    if last_reported is None:
        return REASON_NEW
    window = timedelta(days=days) - timedelta(seconds=REFRESH_SLACK_SECONDS)
    if now - last_reported >= window:
        return REASON_REFRESH
    return None


def short_finding_label(item: dict[str, Any]) -> str:
    """Concise human label for chat lines."""
    cluster_short = str(item.get("cluster") or "").rsplit("/", 1)[-1]
    ns = str(item.get("namespace") or "")
    name = str(item.get("subject_name") or "")
    if ns and ns != CLUSTER_WIDE_NAMESPACE:
        return f"{cluster_short}:{ns}/{name}"
    return f"{cluster_short}:{name}"


def due_finding_summary(item: dict[str, Any], *, include_cluster: bool) -> str:
    """Concise workload summary (`Kind/ns/name [cause, share%]`) for due chat alerts."""
    ns = str(item.get(NAMESPACE_FIELD) or "")
    s_kind = str(item.get(SUBJECT_KIND_FIELD) or DEFAULT_SUBJECT_KIND_CLUSTER)
    s_name = str(item.get(SUBJECT_NAME_FIELD) or DEFAULT_UNKNOWN)
    if ns and ns != CLUSTER_WIDE_NAMESPACE:
        subject = DUE_SUBJECT_NAMESPACED.format(kind=s_kind, namespace=ns, name=s_name)
    else:
        subject = DUE_SUBJECT_CLUSTER.format(kind=s_kind, name=s_name)
    cause = str(item.get(SUSPECTED_CAUSE_FIELD) or DEFAULT_UNKNOWN)
    if str(item.get(KIND_FIELD) or "") == FINDING_KIND_DOMAIN_UNAVAILABLE:
        summary = DUE_SUMMARY_NO_SHARE.format(subject=subject, cause=cause)
    else:
        pct = round(_float(item.get(MAX_DOMAIN_SHARE_KEY), ZERO_FLOAT) * PERCENT_SCALE)
        summary = DUE_SUMMARY_WITH_SHARE.format(subject=subject, cause=cause, pct=pct)
    if include_cluster:
        cluster_short = str(item.get(CLUSTER_FIELD) or "").rsplit(CLUSTER_KEY_SEPARATOR, 1)[-1]
        return DUE_CLUSTER_PREFIX.format(cluster=cluster_short, summary=summary)
    return summary


def format_name_list(names: list[str], limit: int = MAX_NAMES_IN_LINE) -> str:
    if len(names) <= limit:
        return ", ".join(names)
    shown = ", ".join(names[:limit])
    return shown + NAMES_OVERFLOW.format(more=len(names) - limit)


# --- ledger & report persistence ---------------------------------------------


def empty_ledger() -> dict[str, Any]:
    return {LEDGER_SCHEMA_KEY: LEDGER_SCHEMA_VERSION, FINDINGS_KEY: {}}


def load_ledger(home: Path) -> dict[str, Any]:
    path = home / LEDGER_FILE_NAME
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return empty_ledger()
    except OSError as exc:
        raise RuntimeError(f"ledger at {path} could not be read ({exc}); refusing to overwrite it") from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise RuntimeError(f"ledger at {path} is not valid JSON ({exc}); refusing to overwrite it") from None
    if not isinstance(data, dict) or (FINDINGS_KEY in data and not isinstance(data.get(FINDINGS_KEY), dict)):
        raise RuntimeError(f"ledger at {path} is not this job's ledger; refusing to overwrite it")
    data.setdefault(FINDINGS_KEY, {})
    data[LEDGER_SCHEMA_KEY] = LEDGER_SCHEMA_VERSION
    return data


def save_ledger(home: Path, ledger: dict[str, Any]) -> None:
    try:
        home.mkdir(parents=True, exist_ok=True)
        target = home / LEDGER_FILE_NAME
        tmp = home / (LEDGER_FILE_NAME + LEDGER_TMP_SUFFIX)
        tmp.write_text(json.dumps(ledger, indent=JSON_INDENT, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, target)
    except OSError as exc:
        raise WriteFailed(f"cannot write ledger at {home / LEDGER_FILE_NAME}: {exc}") from exc


def render_markdown_report(
    report: dict[str, Any],
    findings: dict[str, dict[str, Any]],
    due_reasons: dict[str, str],
    scan_errors: dict[str, str],
    now: datetime,
    days: int,
    not_reached: dict[str, str] | None = None,
) -> str:
    next_date = (now + timedelta(days=days)).strftime(DATE_FORMAT)
    reasons_counts: dict[str, int] = {}
    for reason in due_reasons.values():
        reasons_counts[reason] = reasons_counts.get(reason, 0) + 1
    reasons_summary = (
        ", ".join(f"{count} {reason}" for reason, count in sorted(reasons_counts.items())) or REASON_ALL_RESOLVED
    )

    lines: list[str] = [
        "# Topology Skew & Leeway Report",
        "",
        (
            f"Produced {iso(now)} by the `leeway-watch` job ({reasons_summary}). "
            f"The next scheduled refresh is after {next_date} while any finding persists."
        ),
        "",
        "## Fleet Summary",
        "",
        "| Cluster | Status | Topology Findings | Rank Findings | Due This Tick |",
        "| ------- | ------ | ----------------: | ------------: | ------------: |",
    ]
    for cr in report.get("clusters", []):
        if not isinstance(cr, dict):
            continue
        c_name = str(cr.get("cluster") or "")
        status = str(cr.get("status") or "")
        t_count = len(cr.get("topology_findings") or [])
        r_count = len(cr.get("rank_findings") or [])
        c_due = sum(1 for k, f in findings.items() if f["cluster"] == c_name and k in due_reasons)
        lines.append(f"| `{c_name}` | `{status}` | {t_count} | {r_count} | {c_due} |")

    lines.extend(
        [
            "",
            "## Findings",
            "",
            "| Cluster | Workload / Subject | Axis | Tier | Severity | Blast Radius | Distribution | Suspected Cause | Trigger |",
            "| ------- | ------------------ | ---- | :--: | -------- | ------------ | ------------ | --------------- | ------- |",
        ]
    )
    for key in sorted(findings):
        item = findings[key]
        subj = (
            f"`{item['namespace']}/{item['subject_kind']}/{item['subject_name']}`"
            if item["namespace"] != CLUSTER_WIDE_NAMESPACE
            else f"`{item['subject_kind']}/{item['subject_name']}`"
        )
        trigger = due_reasons.get(key, "active")
        lines.append(
            f"| `{item['cluster']}` | {subj} | `{item['topology_key']}` | {item['tier'] or '—'} | "
            f"`{item['severity']}` | {item['blast_radius_display']} | {item['distribution_display']} | "
            f"`{item['suspected_cause']}` | {trigger} |"
        )

    lines.extend(["", "## Remediation Details", ""])
    for key in sorted(findings):
        item = findings[key]
        subj = (
            f"{item['namespace']}/{item['subject_kind']}/{item['subject_name']}"
            if item["namespace"] != CLUSTER_WIDE_NAMESPACE
            else f"{item['subject_kind']}/{item['subject_name']}"
        )
        rem = item.get("remediation") or {}
        lines.append(f"### `{item['cluster']}` — `{subj}` (`{item['kind']}`)")
        lines.append("")
        lines.append(f"- **Severity / Tier:** `{item['severity']}` (Tier {item['tier'] or '—'})")
        lines.append(f"- **Topology Key:** `{item['topology_key']}`")
        lines.append(f"- **Replica Distribution:** {item['distribution_display']}")
        lines.append(f"- **Single-Zone Blast Radius:** {item['blast_radius_display']}")
        lines.append(f"- **Suspected Cause:** `{item['suspected_cause']}`")
        if item.get("contributing_factors"):
            lines.append(f"- **Contributing Factors:** {', '.join(item['contributing_factors'])}")
        if rem.get("kind") or rem.get("summary"):
            lines.append(f"- **Remediation (`{rem.get('kind') or 'action'}`):** {rem.get('summary')}")
        if rem.get("yaml"):
            lines.extend(["", "```yaml", str(rem["yaml"]).rstrip(), "```"])
        lines.append("")

    if scan_errors:
        lines.extend(["## Scan Errors", ""])
        for target, err in sorted(scan_errors.items()):
            lines.append(f"- `{target}`: {err}")
        lines.append("")
    if not_reached:
        lines.extend([REPORT_NOT_REACHED_HEADING, "", REPORT_NOT_REACHED_INTRO, ""])
        for target, gap in sorted(not_reached.items()):
            lines.append(f"- `{target}`: {gap}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def update_symlink(link: Path, target_name: str) -> None:
    tmp = link.with_name(link.name + LEDGER_TMP_SUFFIX)
    if tmp.exists() or tmp.is_symlink():
        tmp.unlink()
    tmp.symlink_to(target_name)
    os.replace(tmp, link)


def prune_old_reports(reports_dir: Path) -> None:
    md_files = sorted(
        p for p in reports_dir.glob(f"*{REPORT_MARKDOWN_SUFFIX}") if not p.is_symlink()
    )
    excess = len(md_files) - REPORTS_KEPT
    if excess <= 0:
        return
    for old_md in md_files[:excess]:
        old_json = old_md.with_suffix(REPORT_JSON_SUFFIX)
        for p in (old_md, old_json):
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass


def write_report_files(
    home: Path,
    report: dict[str, Any],
    markdown: str,
    now: datetime,
) -> Path:
    reports_dir = home / REPORTS_DIR_NAME
    stamp = now.strftime(REPORT_TIMESTAMP_FORMAT)
    md_name = stamp + REPORT_MARKDOWN_SUFFIX
    json_name = stamp + REPORT_JSON_SUFFIX
    md_path = reports_dir / md_name
    json_path = reports_dir / json_name
    try:
        reports_dir.mkdir(parents=True, exist_ok=True)
        md_path.write_text(markdown, encoding="utf-8")
        json_path.write_text(json.dumps(report, indent=JSON_INDENT, sort_keys=True) + "\n", encoding="utf-8")
        update_symlink(reports_dir / LATEST_MD_LINK_NAME, md_name)
        update_symlink(reports_dir / LATEST_JSON_LINK_NAME, json_name)
        prune_old_reports(reports_dir)
    except OSError as exc:
        raise WriteFailed(f"cannot write report under {reports_dir}: {exc}") from exc
    return md_path


def publish_blackboard_topic(summary_text: str, json_path: Path) -> tuple[bool, str | None]:
    """Publish the latest leeway report to `agent.platform.topology-skew` when the A2A bus is configured."""
    if not os.environ.get(NATS_URL_ENV, "").strip() or not shutil.which(A2A_EXECUTABLE):
        return False, None
    argv = [
        A2A_EXECUTABLE,
        A2A_TOPICS_SUBCOMMAND,
        A2A_WRITE_VERB,
        A2A_TOPIC_NAME,
        A2A_TEXT_FLAG,
        summary_text,
        A2A_DATA_FLAG,
        f"{A2A_DATA_FILE_PREFIX}{json_path}",
    ]
    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=A2A_PUBLISH_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False, A2A_PUBLISH_TIMED_OUT_DETAIL.format(
            topic=A2A_TOPIC_NAME,
            seconds=A2A_PUBLISH_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        exc_raw = SPACE_CHAR.join(str(exc).split()).replace(BACKTICK_CHAR, SINGLE_QUOTE_CHAR)
        exc_excerpt = exc_raw[:STDERR_EXCERPT_CHARS] if exc_raw else DEFAULT_NO_OUTPUT
        return False, A2A_PUBLISH_EXCEPTION_DETAIL.format(
            error_type=type(exc).__name__,
            error=exc_excerpt,
        )
    if completed.returncode == 0:
        return True, None
    err_raw = SPACE_CHAR.join((completed.stderr or completed.stdout or "").split()).replace(
        BACKTICK_CHAR, SINGLE_QUOTE_CHAR
    )
    excerpt = err_raw[-STDERR_EXCERPT_CHARS:] if err_raw else DEFAULT_NO_OUTPUT
    detail = A2A_PUBLISH_FAILED_DETAIL.format(
        topic=A2A_TOPIC_NAME,
        code=completed.returncode,
        error=excerpt,
    )
    return False, detail


def record_topic_publish_result(
    published: bool,
    err_detail: str | None,
    announced: dict[str, Any],
    lines: list[str],
) -> None:
    """Announce blackboard topic publish failure or recovery once via the ledger's announced map."""
    if err_detail is not None:
        if announced.get(ANNOUNCED_TOPIC_PUBLISH_KEY) != ERROR_KIND_TOPIC_PUBLISH:
            lines.append(
                TOPIC_PUBLISH_FAILED_LINE.format(
                    prefix=LINE_PREFIX,
                    topic=A2A_TOPIC_NAME,
                    detail=err_detail,
                    subject=A2A_TOPIC_SUBJECT,
                )
            )
        announced[ANNOUNCED_TOPIC_PUBLISH_KEY] = ERROR_KIND_TOPIC_PUBLISH
    elif ANNOUNCED_TOPIC_PUBLISH_KEY in announced:
        del announced[ANNOUNCED_TOPIC_PUBLISH_KEY]
        if published:
            lines.append(TOPIC_PUBLISH_CLEARED_LINE.format(prefix=LINE_PREFIX, topic=A2A_TOPIC_NAME))


# --- failure marker ----------------------------------------------------------


def failure_detail(exc: Exception) -> str:
    """A tick failure's detail: the exception's class and message, or the
    plain sentence alone when no call returned a report."""
    if isinstance(exc, NoReportReturned):
        return str(exc)
    return f"{type(exc).__name__}: {exc}"


def record_tick_failure(home: Path, detail: str, *, dry_run: bool) -> list[str]:
    line = FAILED_LINE.format(prefix=LINE_PREFIX, what=FAILED_WHAT_TICK, detail=detail)
    if dry_run:
        return [line]
    marker = home / FAILURE_MARKER_FILE_NAME
    try:
        if marker.is_file() and marker.read_text(encoding="utf-8").strip() == detail:
            return []
        home.mkdir(parents=True, exist_ok=True)
        marker.write_text(detail + "\n", encoding="utf-8")
    except OSError:
        pass
    return [line]


def clear_tick_failure(home: Path, *, dry_run: bool) -> list[str]:
    marker = home / FAILURE_MARKER_FILE_NAME
    if dry_run or not marker.is_file():
        return []
    try:
        marker.unlink()
    except OSError:
        return []
    return [RECOVERED_LINE.format(prefix=LINE_PREFIX)]


# --- main tick ---------------------------------------------------------------


@dataclass
class BatchSweep:
    """What reading one target's clusters in batches produced."""

    reports: list[dict[str, Any]] = field(default_factory=list)
    # The target's one scan-error entry and its kind; None when every cluster
    # was in a call that returned without an error row for it.
    detail: str | None = None
    kind: str | None = None
    # The first failure, for a target where no call returned a report.
    first_error: Exception | None = None
    # How many calls were started, and whether every cluster was in one.
    calls: int = 0
    every_cluster_called: bool = True
    # The clusters returned as ok rows, and those that failed: in a call that
    # raised or timed out, or returned as an error row.
    clean: set[str] = field(default_factory=set)
    failed: set[str] = field(default_factory=set)
    # Whether an error row matched no cluster of its call, so the failure
    # cannot be pinned to a cluster.
    unattributed_failure: bool = False


@dataclass
class TargetRead:
    """A target this tick read: its cluster list, and what its calls returned."""

    listed: set[str] = field(default_factory=set)
    clean: set[str] = field(default_factory=set)
    failed: set[str] = field(default_factory=set)
    unattributed_failure: bool = False


def batch_size(call_timeout: int) -> int:
    """Clusters for a call started with `call_timeout` seconds: CLUSTERS_PER_BATCH
    at the full call timeout, else only the waves of PARALLELISM that fit at
    CONSERVATIVE_CLUSTER_READ_SECONDS each."""
    waves = max((call_timeout - LEEWAY_OVERHEAD_SECONDS) // CONSERVATIVE_CLUSTER_READ_SECONDS, 1)
    return min(CLUSTERS_PER_BATCH, PARALLELISM * waves)


def short_cause(raw: object) -> str:
    """One line of a failure's cause, cut to CAUSE_MAX_CHARS."""
    cause = SPACE_CHAR.join(str(raw).split())
    if len(cause) <= CAUSE_MAX_CHARS:
        return cause
    return cause[: CAUSE_MAX_CHARS - len(CAUSE_ELLIPSIS)] + CAUSE_ELLIPSIS


def first_cause(causes: list[str]) -> str:
    """The first cause, and how many different ones followed it."""
    others = len(set(causes) - {causes[0]})
    return causes[0] + (OTHER_CAUSES_SUFFIX.format(count=others) if others else "")


@dataclass
class CallRows:
    """What one call's report returned: the batch's clusters returned as ok
    rows, those returned as error rows with their cause, and the rows that
    matched no cluster of the call (no name, or a short name two of its
    clusters share), which are counted but never named or remembered."""

    ok: set[str] = field(default_factory=set)
    errors: dict[str, str] = field(default_factory=dict)
    unmatched_ok: int = 0
    unmatched_errors: list[str] = field(default_factory=list)


def row_cluster(row: dict[str, Any], batch: set[str], aliases: dict[str, str]) -> str | None:
    """The call's triple a row is for: by its `source` (`gke:<triple>`), else by
    its `cluster` as a triple, a context name or a short name no two of the
    call's clusters share; None when nothing matches."""
    source = str(row.get(SOURCE_FIELD) or "").strip()
    if source.startswith(GKE_SOURCE_PREFIX) and source[len(GKE_SOURCE_PREFIX) :].strip() in batch:
        return source[len(GKE_SOURCE_PREFIX) :].strip()
    return aliases.get(str(row.get(CLUSTER_FIELD) or "").strip())


def batch_rows(rep: dict[str, Any], batch: list[str]) -> CallRows:
    """Sort a report's rows by the call's clusters (`row_cluster`). A row
    neither ok nor error is neither."""
    held = set(batch)
    aliases: dict[str, str] = {}
    short: dict[str, list[str]] = {}
    for triple in batch:
        aliases[triple] = triple
        parts = triple.split(CLUSTER_KEY_SEPARATOR)
        if len(parts) == TRIPLE_PART_COUNT:
            project, location, cluster = parts
            aliases[GKE_CONTEXT_FORMAT.format(project=project, location=location, cluster=cluster)] = triple
        short.setdefault(parts[-1], []).append(triple)
    aliases.update({name: triples[0] for name, triples in short.items() if len(triples) == 1 and name not in aliases})
    aliases.pop("", None)
    rows = CallRows()
    for row in rep.get(CLUSTERS_FIELD, []):
        if not isinstance(row, dict) or row.get(STATUS_FIELD) not in (STATUS_OK, STATUS_ERROR):
            continue
        name = row_cluster(row, held, aliases)
        cause = short_cause(row.get(ERROR_FIELD) or DEFAULT_CLUSTER_SCAN_FAILED)
        if name is None:
            if row.get(STATUS_FIELD) == STATUS_OK:
                rows.unmatched_ok += 1
            else:
                rows.unmatched_errors.append(cause)
        elif row.get(STATUS_FIELD) == STATUS_OK:
            rows.ok.add(name)
        else:
            rows.errors.setdefault(name, cause)
    rows.ok -= set(rows.errors)
    return rows


def join_names(names: list[str]) -> str:
    """`a`, `a and b`, `a, b and c`."""
    if len(names) <= 1:
        return "".join(names)
    return NAMED_CLUSTERS_SEPARATOR.join(names[:-1]) + NAMED_CLUSTERS_LAST_SEPARATOR + names[-1]


def order_clusters(triples: list[str], cluster_runs: dict[str, Any], now: datetime) -> list[str]:
    """Least recently stamped first. Clusters with the same stamp are shuffled
    by a hash of the run's date, so batch membership changes from run to run
    and a cluster that hangs its call does not take the same batch-mates down
    every time."""
    salt = now.strftime(DATE_FORMAT)

    def key(triple: str) -> tuple[str, str]:
        mixed = hashlib.sha256(f"{salt}{CLUSTER_KEY_SEPARATOR}{triple}".encode()).hexdigest()
        return str(cluster_runs.get(triple) or ""), mixed

    return sorted(triples, key=key)


def sweep_in_batches(
    label: str,
    triples: list[str],
    deadline: float,
    cluster_runs: dict[str, Any],
    now: datetime,
    *,
    full_names: bool = False,
) -> BatchSweep:
    """Read `triples` in `leeway report` calls of at most `batch_size` clusters.

    Every started call stamps its clusters in `cluster_runs`, however it ends.
    No call starts with less than MIN_PROJECT_RUN_SECONDS left; the clusters
    after that point are not reached. The target's turn ends at its first
    sandbox timeout, so a target whose calls hang costs one call. A failure or
    a gap is one entry named `label`: a target whose one call held every
    cluster and raised keeps that call's own message; any other is summarised
    by counts, naming up to MAX_NAMED_ERROR_CLUSTERS clusters that returned an
    error row (by cluster name, or by triple when `full_names`).
    """
    result = BatchSweep()
    ordered = order_clusters(triples, cluster_runs, now)
    position = read = failed = 0
    causes: list[str] = []
    # (cluster, cause) per error row; the cluster is None for a row that
    # matched no cluster of its call.
    row_errors: list[tuple[str | None, str]] = []
    one_call_detail: str | None = None
    timed_out = False
    while position < len(ordered) and not timed_out:
        remaining = int(deadline - time.monotonic())
        if remaining < MIN_PROJECT_RUN_SECONDS:
            result.first_error = result.first_error or RuntimeError(BUDGET_EXHAUSTED_DETAIL.format(target=label))
            break
        timeout = min(PROJECT_TIMEOUT_SECONDS, remaining)
        batch = ordered[position : position + batch_size(timeout)]
        holds_every_cluster = position == 0 and len(batch) == len(ordered)
        position += len(batch)
        result.calls += 1
        cluster_runs.update(dict.fromkeys(batch, iso(now)))
        try:
            rep = run_leeway_report(CLUSTERS_FLAG, CLUSTER_SEPARATOR.join(sorted(batch)), timeout=timeout)
        except SandboxTimedOut as exc:
            error: Exception = exc
            detail = TIMED_OUT_DETAIL.format(seconds=timeout)
            kind = ERROR_KIND_TIMEOUT
            timed_out = True
        except Exception as exc:  # noqa: BLE001
            error = exc
            detail = SCAN_FAILED_DETAIL.format(target=label, error=f"{type(exc).__name__}: {exc}")
            kind = ERROR_KIND_SCAN_FAILED
        else:
            result.reports.append(rep)
            rows = batch_rows(rep, batch)
            # A report never accounts for more clusters than its call held:
            # rows matched to the batch come first, then the unmatched ones,
            # each counted once, error rows before ok rows.
            room = len(batch) - len(rows.ok) - len(rows.errors)
            unmatched_errors = rows.unmatched_errors[: max(room, 0)]
            unmatched_ok = min(rows.unmatched_ok, max(room - len(unmatched_errors), 0))
            read += len(rows.ok) + unmatched_ok
            result.clean.update(rows.ok)
            result.failed.update(rows.errors)
            result.unattributed_failure = result.unattributed_failure or bool(unmatched_errors)
            row_errors.extend(sorted(rows.errors.items()))
            row_errors.extend((None, cause) for cause in unmatched_errors)
            continue
        result.first_error = result.first_error or error
        failed += len(batch)
        result.failed.update(batch)
        causes.append(short_cause(getattr(error, SHORT_CAUSE_ATTR, None) or error))
        result.kind = result.kind or kind
        one_call_detail = detail if holds_every_cluster else None

    left = len(ordered) - position
    result.every_cluster_called = not left
    if not failed and not row_errors and not left:
        return result
    result.kind = result.kind or (ERROR_KIND_CLUSTER_ERROR if row_errors else ERROR_KIND_BUDGET_EXHAUSTED)
    if result.calls == 0:
        result.detail = BUDGET_EXHAUSTED_DETAIL.format(target=label)
    elif one_call_detail is not None:
        result.detail = one_call_detail
    else:
        gaps: list[str] = []
        if failed:
            gaps.append(FAILED_PART.format(count=failed, cause=first_cause(causes)))
        if row_errors:
            cause = first_cause([row_cause for _, row_cause in row_errors])
            named = [name for name, _ in row_errors if name is not None]
            if len(named) == len(row_errors) <= MAX_NAMED_ERROR_CLUSTERS:
                names = [name if full_names else name.rsplit(CLUSTER_KEY_SEPARATOR, 1)[-1] for name in named]
                gaps.append(NAMED_CLUSTER_ERRORS_PART.format(names=join_names(names), cause=cause))
            elif len(row_errors) == 1:
                gaps.append(ONE_CLUSTER_ERROR_PART.format(cause=cause))
            else:
                gaps.append(CLUSTER_ERRORS_PART.format(count=len(row_errors), cause=cause))
        if left:
            gaps.append((TURN_ENDED_PART if timed_out else NOT_REACHED_PART).format(count=left))
        result.detail = TARGET_SUMMARY_DETAIL.format(read=read, total=len(ordered), gaps=GAP_SEPARATOR.join(gaps))
    return result


def ledger_cluster_runs(ledger: dict[str, Any]) -> dict[str, Any]:
    """A copy of the ledger's per-cluster stamps; empty in a ledger written before batching."""
    stamps = ledger.get(CLUSTER_RUNS_KEY)
    return dict(stamps) if isinstance(stamps, dict) else {}


def forget_unlisted_clusters(cluster_runs: dict[str, Any], project: str, triples: list[str]) -> None:
    """Drop the stamps of `project`'s clusters that its cluster list no longer names."""
    prefix = project + CLUSTER_KEY_SEPARATOR
    listed = set(triples)
    for stale in [t for t in cluster_runs if t.startswith(prefix) and t not in listed]:
        del cluster_runs[stale]


def sweep_fleet(
    ledger: dict[str, Any],
    now: datetime,
    deadline: float,
    cluster_runs: dict[str, Any] | None = None,
    project_runs: dict[str, Any] | None = None,
    target_reads: dict[str, TargetRead] | None = None,
) -> tuple[dict[str, Any], dict[str, str], dict[str, str], dict[str, str], set[str], set[str]]:
    """Run `leeway report` across configured clusters or projects.

    Each target is read in batches (`sweep_in_batches`) and has at most one
    scan-error entry. Projects go least recently swept first; one whose turn
    starts with a full call's time is stamped then, so it rotates behind the
    others whatever its turn produces. `scanned_projects` holds the projects
    whose cluster list was read. `cluster_runs`, `project_runs` and
    `target_reads` (the targets at least one call was started for, or whose
    list was empty, with what their calls returned), when given, are updated
    in place.
    """
    cluster_runs = ledger_cluster_runs(ledger) if cluster_runs is None else cluster_runs
    project_runs = dict(ledger.get(PROJECT_RUNS_KEY) or {}) if project_runs is None else project_runs
    target_reads = {} if target_reads is None else target_reads
    scope = declared_scope()
    excluded = excluded_clusters(scope)
    clusters = [c for c in explicit_clusters() if c not in excluded]
    scan_errors: dict[str, str] = {}
    error_kinds: dict[str, str] = {}
    reports: list[dict[str, Any]] = []
    scanned_projects: set[str] = set()
    requested_clusters: set[str] = set(clusters)
    # Calls started across the sweep, and those that held every cluster of
    # their target.
    calls = whole_calls = 0

    if clusters:
        label = EXPLICIT_TARGET_LABEL if len(clusters) > CLUSTERS_PER_BATCH else CLUSTER_SEPARATOR.join(clusters)
        swept = sweep_in_batches(label, clusters, deadline, cluster_runs, now, full_names=True)
        if not swept.reports and swept.first_error is not None:
            # No call read anything: fail the tick. One call that held every
            # cluster keeps its cause, as before batching; otherwise the cause
            # depends on which batch went first, so the detail names none.
            if swept.calls > 1 or not swept.every_cluster_called:
                raise NoReportReturned(NO_REPORT_DETAIL)
            raise swept.first_error
        reports.extend(swept.reports)
        target_reads[EXPLICIT_TARGET_LABEL] = TargetRead(set(clusters), swept.clean, swept.failed, swept.unattributed_failure)
        if swept.detail is not None and swept.kind is not None:
            scan_errors[EXPLICIT_TARGET_LABEL] = swept.detail
            error_kinds[EXPLICIT_TARGET_LABEL] = swept.kind
        return (
            merge_reports(reports, now),
            project_runs,
            scan_errors,
            error_kinds,
            scanned_projects,
            requested_clusters,
        )

    # Under spec.mode: next the resolved scope names the projects and each
    # member project's clusters (#5621); the management and explicit projects
    # are listed live, and a member nothing listed is recorded unread.
    # Outside it the profiles' triples sweep their projects, as before.
    entries: dict[str, fleet_scope_targets.ProjectClusters] = {}
    if fleet_scope_targets.resolved_scope_file():
        cluster_scope = fleet_scope_targets.list_scope_clusters(gitops_workspace.agent_home())
        entries = cluster_scope.by_project()
        excluded |= {CLUSTER_KEY_SEPARATOR.join(ref) for ref in cluster_scope.excluded_clusters}
        known_triples_by_proj = {
            proj: [CLUSTER_KEY_SEPARATOR.join(t) for t in entry.clusters or ()] for proj, entry in entries.items()
        }
        explicit = {p.strip() for p in os.environ.get(PROJECTS_ENV, "").split(CSV_SEPARATOR) if p.strip()}
        # LEEWAY_WATCH_PROJECTS narrows the sweep, but never past the row an
        # unusable resolved scope leaves for the members it could not name:
        # that row is a scan error, so nothing a member holds is retired.
        names = [
            proj for proj in entries
            if not explicit or proj in explicit or proj == fleet_scope_targets.UNRESOLVED_SCOPE_ROW
        ]
        target_projects = order_by_least_recent(names, project_runs)
    else:
        known_triples_by_proj = roster_clusters_by_project()
        target_projects = order_by_least_recent(projects(scope), project_runs)
    for proj in target_projects:
        entry = entries.get(proj)
        if entry is not None and entry.status == fleet_scope_targets.STATUS_UNREAD:
            scan_errors[proj] = UNREAD_PROJECT_DETAIL.format(target=proj, reason=entry.reason)
            error_kinds[proj] = ERROR_KIND_SCAN_FAILED
            continue
        remaining = int(deadline - time.monotonic())
        if remaining < MIN_PROJECT_RUN_SECONDS:
            scan_errors[proj] = BUDGET_EXHAUSTED_DETAIL.format(target=proj)
            error_kinds[proj] = ERROR_KIND_BUDGET_EXHAUSTED
            continue
        # A turn that starts with a full call's time rotates the project behind
        # the others, whatever it produces; one the budget reached late is
        # stamped only if every cluster got a call.
        if remaining >= PROJECT_TIMEOUT_SECONDS:
            project_runs[proj] = iso(now)
        triples = [t for t in (known_triples_by_proj.get(proj) or []) if t not in excluded]
        try:
            if not triples and (entry is None or entry.status == fleet_scope_targets.STATUS_LIST_LIVE):
                triples = [t for t in project_cluster_triples(proj) if t not in excluded]
        except SandboxTimedOut:
            scan_errors[proj] = LIST_CLUSTERS_TIMED_OUT_DETAIL.format(
                project=proj, seconds=PROJECT_LOOKUP_TIMEOUT_SECONDS
            )
            error_kinds[proj] = ERROR_KIND_TIMEOUT
            continue
        except Exception as exc:  # noqa: BLE001
            scan_errors[proj] = SCAN_FAILED_DETAIL.format(target=proj, error=f"{type(exc).__name__}: {exc}")
            error_kinds[proj] = ERROR_KIND_SCAN_FAILED
            continue
        forget_unlisted_clusters(cluster_runs, proj, triples)
        scanned_projects.add(proj)
        if not triples:
            # A project with no clusters is a clean, empty read.
            target_reads[proj] = TargetRead()
            project_runs[proj] = iso(now)
            continue
        requested_clusters.update(triples)
        swept = sweep_in_batches(proj, triples, deadline, cluster_runs, now)
        calls += swept.calls
        whole_calls += int(swept.calls == 1 and swept.every_cluster_called)
        reports.extend(swept.reports)
        if swept.calls:
            target_reads[proj] = TargetRead(set(triples), swept.clean, swept.failed, swept.unattributed_failure)
        if swept.detail is not None and swept.kind is not None:
            scan_errors[proj] = swept.detail
            error_kinds[proj] = swept.kind
        if swept.every_cluster_called:
            project_runs[proj] = iso(now)

    failures = {k: v for k, v in scan_errors.items() if error_kinds.get(k) != ERROR_KIND_BUDGET_EXHAUSTED}
    if not reports and failures:
        # No call returned: fail the tick. A run of one target whose cluster
        # list failed, or whose one call held every cluster, keeps that
        # target's entry, as before batching. Any
        # other names no target and no count, since the rotation changes
        # which targets fail, how far each got and why, and the line must
        # repeat word for word while the condition lasts.
        if len(scan_errors) == 1 and calls == whole_calls <= 1:
            raise RuntimeError(format_name_list([f"{k}: {v}" for k, v in sorted(failures.items())]))
        raise NoReportReturned(NO_REPORT_DETAIL)

    return merge_reports(reports, now), project_runs, scan_errors, error_kinds, scanned_projects, requested_clusters


def can_retire_finding(
    key: str,
    prior_entry: dict[str, Any],
    ok_clusters: set[str],
    cluster_errors: dict[str, str],
    scanned_projects: set[str],
    scan_errors: dict[str, str],
    explicit_targets: set[str] | None = None,
) -> bool:
    """Return True when a prior finding's absence is backed by a clean read.

    A cluster read to a result without an error row retires its absent
    findings; a cluster a call was meant to read (`explicit_targets`) and that
    was not keeps them. A cluster its project's cluster list no longer names
    retires them once that list was read (`scanned_projects`).
    """
    cluster = str(prior_entry.get("cluster") or key.split(CLUSTER_KEY_SEPARATOR, 1)[0])
    is_bare_ledger_cluster = CLUSTER_KEY_SEPARATOR not in cluster
    bare_cluster = cluster.rsplit(CLUSTER_KEY_SEPARATOR, 1)[-1]
    error_bare = {c.rsplit(CLUSTER_KEY_SEPARATOR, 1)[-1] for c in cluster_errors}
    if cluster in cluster_errors or (is_bare_ledger_cluster and bare_cluster in error_bare):
        return False
    ok_bare = {c.rsplit(CLUSTER_KEY_SEPARATOR, 1)[-1] for c in ok_clusters}
    if cluster in ok_clusters or (is_bare_ledger_cluster and bare_cluster in ok_bare):
        return True
    targets = explicit_targets or set()
    target_bare = {c.rsplit(CLUSTER_KEY_SEPARATOR, 1)[-1] for c in targets}
    if cluster in targets or (is_bare_ledger_cluster and bare_cluster in target_bare):
        # The cluster was explicitly requested via `--clusters` and omitted from
        # `report.clusters` (neither ok nor error); retain until cleanly read.
        return False
    if targets and not scanned_projects:
        return not scan_errors and not cluster_errors
    # Not a cluster this tick meant to read: retire when its project's cluster
    # list was read without it, or when the entire sweep succeeded with zero
    # scan/cluster errors.
    proj = cluster.split(CLUSTER_KEY_SEPARATOR, 1)[0] if not is_bare_ledger_cluster else ""
    if proj and proj in scanned_projects:
        return True
    if scanned_projects and not scan_errors and not cluster_errors:
        return True
    return False


def stored_episodes(announced: dict[str, Any]) -> dict[str, set[str] | None]:
    """The open episodes the ledger holds: each target's failed clusters, or
    None for a target that failed as a whole. An older ledger's plain list of
    targets opens each as a whole."""
    stored = announced.get(ANNOUNCED_FAILED_TARGETS_KEY)
    if isinstance(stored, dict):
        return {
            str(target): {str(c) for c in clusters} if isinstance(clusters, list) else None
            for target, clusters in stored.items()
        }
    if isinstance(stored, list):
        return dict.fromkeys((str(target) for target in stored), None)
    return {}


def announce_failed_targets(
    announced: dict[str, Any],
    failures: dict[str, str],
    target_reads: dict[str, TargetRead],
    present: set[str],
    kinds: dict[str, str] | None = None,
    not_reached: bool = False,
) -> list[str]:
    """The partial-scan line when a target fails that has no open episode, and
    the recovery line when the last open episode closes.

    An episode remembers the clusters that failed. It closes once each of them
    has returned an ok row since, or has left its target's cluster list; a
    cluster failing again, or not read, keeps it open. A target that failed as
    a whole (its list could not be read, or an error row matched none of its
    clusters) closes once a call for it is started and nothing fails. A target
    no longer swept (absent from `present`) drops its episode silently.

    An older ledger's signature of error kinds (`kinds` maps each failing
    target to its kind) that held a failure is kept until a run in which
    nothing fails and nothing goes unread, which posts the recovery line. Until
    then a target failing with a kind the signature holds is taken as already
    announced, silently, and one failing with another kind posts.
    """
    episodes = stored_episodes(announced)
    legacy = announced.get(ANNOUNCED_PARTIAL_KEY)
    legacy_kinds = {str(kind) for kind in legacy} - LEGACY_NOT_REACHED_KINDS if isinstance(legacy, list) else set()
    if not legacy_kinds:
        announced.pop(ANNOUNCED_PARTIAL_KEY, None)
    adopted = {t for t in failures if t not in episodes and (kinds or {}).get(t) in legacy_kinds}
    lines: list[str] = []
    new = sorted(set(failures) - set(episodes) - adopted)
    if new:
        named = new + sorted(set(failures) - set(new))
        reasons = format_name_list([f"{target}: {failures[target]}" for target in named])
        lines.append(PARTIAL_LINE.format(prefix=LINE_PREFIX, errors=len(failures), reasons=reasons))
    still_open: dict[str, set[str] | None] = {}
    closed = False
    for target in sorted(set(episodes) | set(failures)):
        if target not in failures and target not in present:
            continue
        read = target_reads.get(target)
        remembered = episodes.get(target)
        if remembered is not None and read is not None:
            unlisted = {c for c in remembered if CLUSTER_KEY_SEPARATOR in c and c not in read.listed}
            remembered = remembered - read.clean - unlisted
        if target in failures:
            if read is not None and read.unattributed_failure:
                still_open[target] = None
                continue
            attributed = read.failed if read is not None else set()
            if target not in episodes or episodes[target] is None:
                still_open[target] = set(attributed) or None
            else:
                still_open[target] = (remembered or set()) | attributed or None
        elif remembered is None and read is None:
            still_open[target] = None
        elif remembered:
            still_open[target] = remembered
        else:
            closed = True
    if legacy_kinds:
        if not failures and not not_reached and not still_open:
            announced.pop(ANNOUNCED_PARTIAL_KEY, None)
            lines.append(PARTIAL_CLEARED_LINE.format(prefix=LINE_PREFIX))
    elif closed and not still_open:
        lines.append(PARTIAL_CLEARED_LINE.format(prefix=LINE_PREFIX))
    if still_open:
        announced[ANNOUNCED_FAILED_TARGETS_KEY] = {
            target: sorted(clusters) if clusters is not None else None for target, clusters in still_open.items()
        }
    else:
        announced.pop(ANNOUNCED_FAILED_TARGETS_KEY, None)
    return lines


def save_stamps(home: Path, ledger: dict[str, Any], project_runs: dict[str, Any], cluster_runs: dict[str, Any]) -> None:
    """Keep a failed tick's sweep-order stamps, so the target that failed it
    does not go first again; nothing else in the ledger changes."""
    if project_runs == (ledger.get(PROJECT_RUNS_KEY) or {}) and cluster_runs == ledger_cluster_runs(ledger):
        return
    try:
        save_ledger(home, {**ledger, PROJECT_RUNS_KEY: project_runs, CLUSTER_RUNS_KEY: cluster_runs})
    except WriteFailed:
        pass


def tick(*, dry_run: bool = False, now: datetime | None = None) -> int:
    moment = now or now_utc()
    home = watch_home()
    days = refresh_days()
    deadline = time.monotonic() + TICK_BUDGET_SECONDS

    ledger: dict[str, Any] | None = None
    cluster_runs: dict[str, Any] = {}
    project_runs: dict[str, Any] = {}
    target_reads: dict[str, TargetRead] = {}
    try:
        ledger = load_ledger(home)
        cluster_runs = ledger_cluster_runs(ledger)
        project_runs = dict(ledger.get(PROJECT_RUNS_KEY) or {})
        report, project_runs, scan_errors, error_kinds, scanned_projects, requested_clusters = sweep_fleet(
            ledger, moment, deadline, cluster_runs, project_runs, target_reads
        )
    except Exception as exc:  # noqa: BLE001
        detail = failure_detail(exc)
        if ledger is not None and not dry_run:
            save_stamps(home, ledger, project_runs, cluster_runs)
        for line in record_tick_failure(home, detail, dry_run=dry_run):
            print(line)
        return 0

    lines: list[str] = []

    raw_findings, ok_clusters, cluster_errors = extract_findings(report)
    report = filter_report_findings(report)
    active_findings, _, _ = extract_findings(report)
    # A target only the budget kept from being read in full is not a failure:
    # it posts nothing and holds no episode open. It still holds back the
    # report refresh and the topic retry below (`scan_errors`), since the
    # sweep did not read the whole fleet. Error rows are folded into their
    # target's entry.
    not_reached = {k: v for k, v in scan_errors.items() if error_kinds.get(k) == ERROR_KIND_BUDGET_EXHAUSTED}
    all_errors = {k: v for k, v in scan_errors.items() if k not in not_reached}
    announced = dict(ledger.get(ANNOUNCED_KEY) or {})
    lines.extend(
        announce_failed_targets(
            announced,
            all_errors,
            target_reads,
            set(scan_errors) | set(target_reads),
            error_kinds,
            bool(not_reached),
        )
    )

    prior_findings: dict[str, Any] = dict(ledger.get(FINDINGS_KEY) or {})
    next_findings: dict[str, Any] = {}
    due_reasons: dict[str, str] = {}
    retired_labels: list[str] = []

    # 1. Evaluate active findings against the ledger.
    for key, item in sorted(active_findings.items()):
        prior = prior_findings.get(key) if isinstance(prior_findings.get(key), dict) else None
        reason = finding_trigger_reason(item, prior, moment, days)
        if reason is not None:
            due_reasons[key] = reason
        first_seen = str(prior.get(FIRST_SEEN_KEY)) if prior and prior.get(FIRST_SEEN_KEY) else iso(moment)
        last_report_at = (
            iso(moment)
            if reason is not None
            else (str(prior.get(LAST_REPORT_KEY)) if prior and prior.get(LAST_REPORT_KEY) else iso(moment))
        )
        # Preserve the last reported severity and suspected_cause baseline on quiet
        # de-escalation ticks so a warning <-> critical oscillation does not re-page
        # inside the 7-day suppression window.
        baseline_severity = (
            item["severity"]
            if reason is not None or not prior
            else str(prior.get("severity") or item["severity"])
        )
        baseline_cause = (
            item["suspected_cause"]
            if reason is not None or not prior
            else str(prior.get("suspected_cause") or item["suspected_cause"])
        )
        entry: dict[str, Any] = {
            "cluster": item["cluster"],
            "kind": item["kind"],
            "namespace": item["namespace"],
            "subject_kind": item["subject_kind"],
            "subject_name": item["subject_name"],
            "topology_key": item["topology_key"],
            "tier": item["tier"],
            "severity": baseline_severity,
            "suspected_cause": baseline_cause,
            "observed_skew": item["observed_skew"],
            "max_domain_share": item["max_domain_share"],
            FIRST_SEEN_KEY: first_seen,
            LAST_REPORT_KEY: last_report_at,
        }
        if item.get(EXCESS_OVER_FLOOR_KEY) is not None:
            entry[EXCESS_OVER_FLOOR_KEY] = item[EXCESS_OVER_FLOOR_KEY]
        next_findings[key] = entry

    # 2. Check prior findings not seen in active_findings: retain silently if
    #    still emitted by leeway and suppressed only by active thresholds (unless
    #    an above-floor skew genuinely rebalanced down to the pigeonhole floor),
    #    retire if absent/rebalanced on a cleanly scanned cluster, or retain if
    #    the cluster/project scan failed.
    for key, prior in sorted(prior_findings.items()):
        if key in active_findings or not isinstance(prior, dict):
            continue
        raw_item = raw_findings.get(key)
        if raw_item is not None:
            raw_excess = raw_item.get(EXCESS_OVER_FLOOR_KEY)
            at_default_floor = (
                raw_item.get(SUSPECTED_CAUSE_FIELD) == DEFAULT_UNKNOWN
                and raw_excess is not None
                and raw_excess < MIN_EXCESS_OVER_FLOOR_DEFAULT
            )
            prior_cause = str(prior.get(SUSPECTED_CAUSE_FIELD) or DEFAULT_UNKNOWN)
            prior_excess = _int(prior.get(EXCESS_OVER_FLOOR_KEY), None)
            was_above_floor = prior_cause != DEFAULT_UNKNOWN or (
                prior_excess >= MIN_EXCESS_OVER_FLOOR_DEFAULT
                if prior_excess is not None
                else _int(prior.get(OBSERVED_SKEW_FIELD), 0) > raw_item[OBSERVED_SKEW_FIELD]
            )
            if not (at_default_floor and was_above_floor):
                next_findings[key] = prior
                continue
        if can_retire_finding(
            key, prior, ok_clusters, cluster_errors, scanned_projects, scan_errors, requested_clusters
        ):
            retired_labels.append(short_finding_label(prior))
        else:
            next_findings[key] = prior

    if dry_run:
        if retired_labels:
            print(
                DRY_RUN_WOULD_RETIRE.format(
                    count=len(retired_labels),
                    names=format_name_list(retired_labels),
                )
            )
        if due_reasons:
            due_items = [active_findings[k] for k in sorted(due_reasons)]
            due_clusters = sorted({item[CLUSTER_FIELD] for item in due_items})
            multi_cluster = len(due_clusters) > 1
            reasons_counts: dict[str, int] = {}
            for r in due_reasons.values():
                reasons_counts[r] = reasons_counts.get(r, 0) + 1
            reasons_str = LIST_SEPARATOR.join(
                REASON_COUNT_FORMAT.format(count=c, reason=r) for r, c in sorted(reasons_counts.items())
            )
            print(
                DRY_RUN_WOULD_REPORT.format(
                    count=len(due_reasons),
                    reasons=reasons_str,
                    clusters=format_name_list(due_clusters),
                    names=format_name_list([due_finding_summary(i, include_cluster=multi_cluster) for i in due_items]),
                )
            )
        if not retired_labels and not due_reasons:
            print(DRY_RUN_NOTHING_DUE.format(count=len(active_findings)))
        return 0

    current_thresholds: list[int | float] = [
        min_severity_rank(),
        min_drift(),
        min_max_domain_share(),
        min_excess_over_floor(),
    ]
    thresholds_overridden = current_thresholds != [
        SEVERITY_RANKS[MIN_SEVERITY_DEFAULT],
        MIN_DRIFT_DEFAULT,
        MIN_MAX_DOMAIN_SHARE_DEFAULT,
        MIN_EXCESS_OVER_FLOOR_DEFAULT,
    ]
    override_stale = (
        ANNOUNCED_OVERRIDE_THRESHOLDS_KEY in announced
        and announced.get(ANNOUNCED_OVERRIDE_THRESHOLDS_KEY) != current_thresholds
    )

    try:
        if retired_labels:
            retired_line = RETIRED_LINE.format(
                prefix=LINE_PREFIX,
                count=len(retired_labels),
                names=format_name_list(retired_labels),
            )
            lines.append(retired_line)
        if due_reasons:
            announced.pop(ANNOUNCED_DEFERRED_REFRESH_KEY, None)
            if thresholds_overridden:
                announced[ANNOUNCED_OVERRIDE_THRESHOLDS_KEY] = current_thresholds
            else:
                announced.pop(ANNOUNCED_OVERRIDE_THRESHOLDS_KEY, None)
            markdown = render_markdown_report(
                report, active_findings, due_reasons, all_errors, moment, days, not_reached=not_reached
            )
            md_path = write_report_files(home, report, markdown, moment)
            due_items = [active_findings[k] for k in sorted(due_reasons)]
            due_clusters = sorted({item[CLUSTER_FIELD] for item in due_items})
            multi_cluster = len(due_clusters) > 1
            reasons_counts = {}
            for r in due_reasons.values():
                reasons_counts[r] = reasons_counts.get(r, 0) + 1
            reasons_str = LIST_SEPARATOR.join(
                REASON_COUNT_FORMAT.format(count=c, reason=r) for r, c in sorted(reasons_counts.items())
            )
            next_date = (moment + timedelta(days=days)).strftime(DATE_FORMAT)
            report_line = REPORT_LINE.format(
                prefix=LINE_PREFIX,
                due_count=len(due_reasons),
                reasons=reasons_str,
                cluster_count=len(due_clusters),
                clusters=format_name_list(due_clusters),
                names=format_name_list([due_finding_summary(i, include_cluster=multi_cluster) for i in due_items]),
                path=md_path,
                next_date=next_date,
            )
            lines.append(report_line)
            published, err_detail = publish_blackboard_topic(report_line, md_path.with_suffix(REPORT_JSON_SUFFIX))
            record_topic_publish_result(published, err_detail, announced, lines)
        elif retired_labels and scan_errors:
            # A finding retired on a readable cluster during a partial scan (a
            # failure, or clusters not reached) makes `latest.json` stale; defer
            # refreshing and re-publishing until the whole fleet reads cleanly.
            announced[ANNOUNCED_DEFERRED_REFRESH_KEY] = REASON_ALL_RESOLVED
        elif (retired_labels or ANNOUNCED_DEFERRED_REFRESH_KEY in announced or override_stale) and not scan_errors:
            announced.pop(ANNOUNCED_DEFERRED_REFRESH_KEY, None)
            if thresholds_overridden:
                announced[ANNOUNCED_OVERRIDE_THRESHOLDS_KEY] = current_thresholds
            else:
                announced.pop(ANNOUNCED_OVERRIDE_THRESHOLDS_KEY, None)
            markdown = render_markdown_report(
                report, active_findings, due_reasons, all_errors, moment, days, not_reached=not_reached
            )
            md_path = write_report_files(home, report, markdown, moment)
            summary_line = retired_line if retired_labels else A2A_RETRY_SUMMARY.format(prefix=LINE_PREFIX)
            published, err_detail = publish_blackboard_topic(summary_line, md_path.with_suffix(REPORT_JSON_SUFFIX))
            record_topic_publish_result(published, err_detail, announced, lines)
        elif ANNOUNCED_TOPIC_PUBLISH_KEY in announced and not scan_errors:
            latest_json = home / REPORTS_DIR_NAME / LATEST_JSON_LINK_NAME
            if latest_json.is_file():
                published, err_detail = publish_blackboard_topic(
                    A2A_RETRY_SUMMARY.format(prefix=LINE_PREFIX), latest_json
                )
                record_topic_publish_result(published, err_detail, announced, lines)
            else:
                del announced[ANNOUNCED_TOPIC_PUBLISH_KEY]

        updated_ledger: dict[str, Any] = {
            LEDGER_SCHEMA_KEY: LEDGER_SCHEMA_VERSION,
            LAST_TICK_KEY: iso(moment),
            FINDINGS_KEY: next_findings,
            PROJECT_RUNS_KEY: project_runs,
        }
        if cluster_runs:
            updated_ledger[CLUSTER_RUNS_KEY] = cluster_runs
        if scan_errors:
            updated_ledger[SCAN_ERRORS_KEY] = scan_errors
        if announced:
            updated_ledger[ANNOUNCED_KEY] = announced
        save_ledger(home, updated_ledger)
        lines = clear_tick_failure(home, dry_run=dry_run) + lines
    except WriteFailed as exc:
        print(FAILED_LINE.format(prefix=LINE_PREFIX, what=FAILED_WHAT_WRITE, detail=str(exc)))
        return WRITE_FAILED_EXIT

    for line in lines:
        print(line)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the daily fleet topology skew & leeway watch.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run the fleet scan and gate without writing reports or modifying the ledger.",
    )
    args = parser.parse_args(argv)
    try:
        return tick(dry_run=args.dry_run)
    except WriteFailed as exc:
        print(FAILED_LINE.format(prefix=LINE_PREFIX, what=FAILED_WHAT_WRITE, detail=str(exc)))
        return WRITE_FAILED_EXIT
    except Exception as exc:  # noqa: BLE001
        detail = failure_detail(exc)
        for line in record_tick_failure(watch_home(), detail, dry_run=args.dry_run):
            print(line)
        return 0


if __name__ == "__main__":
    sys.exit(main())
