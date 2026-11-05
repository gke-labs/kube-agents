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
   triples), passed directly to `leeway report --clusters`, less any clusters
   the declared scope excludes (`spec.scope.exclude.clusters`).
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

Where it runs. `leeway` is baked into `agent-sandbox` at `/usr/local/bin/leeway`
and routes Kubernetes object reads through the sandbox's `kubectl`
`credential-proxy` shim (`CREDENTIAL_PROXY_URL`). `leeway_watch.py` invokes
`leeway report` in the sandbox through `sandbox_exec.run` and parses the
`leeway.report/v1alpha1` JSON document from stdout.

State ledger and 7-day reminder gate. Under `<profile home>/leeway/`:
- `ledger.json`: tracks each active finding by its six-part key
  (`<cluster>/<namespace>/<subject_kind>/<subject_name>/<topology_key>/<finding_kind>`)
  with `first_seen`, `last_report_at`, `suspected_cause`, `severity`, `tier`,
  `max_domain_share`, and `observed_skew`.
- `reports/<timestamp>.md` and `reports/<timestamp>.json` (with `latest.md` and
  `latest.json` symlinks, keeping the newest 10 pairs): written whenever at
  least one finding is due.
A finding is due for reporting when:
1. It is **new** (not present in `ledger.json`),
2. Its **suspected cause changed or severity escalated** since the last report,
   or
3. Its **7-day scheduled refresh** window has elapsed (`now - last_report_at >=
   7 days - 10m`).
Findings on clusters whose scan succeeded (`status == "ok"`) and that are no
longer present are retired from `ledger.json` with a single resolution line.
Clusters whose scan failed (`status == "error"`) or whose project could not be
scanned retain their existing ledger entries so a transient outage does not
erase state and re-page as new tomorrow. A quiet tick prints nothing.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

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

# Cluster and project discovery.
CLUSTERS_ENV = "LEEWAY_WATCH_CLUSTERS"
PROJECTS_ENV = "LEEWAY_WATCH_PROJECTS"
CSV_SEPARATOR = ","
MANAGEMENT_PROJECT_ENV = "GCP_PROJECT_ID"
PROFILES_DIR = "profiles"
IDENTITY_PROJECT_KEY = "project"
IDENTITY_LOCATION_KEY = "location"
IDENTITY_CLUSTER_KEY = "cluster"
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
CLUSTERS_FLAG = "--clusters"
LIST_CLUSTERS_ARGV = ("gcloud", "container", "clusters", "list", "--format=value(name,location)", "--project")
LIST_CLUSTERS_FAILED_DETAIL = "gcloud container clusters list --project {project} exited {code}: {error}"
PROJECT_TIMEOUT_SECONDS = 900
TICK_BUDGET_SECONDS = 2700
MIN_PROJECT_RUN_SECONDS = 60
STDERR_EXCERPT_CHARS = 300
TIMED_OUT_DETAIL = "the sandbox run timed out after {seconds}s"
BUDGET_EXHAUSTED_DETAIL = "tick budget exhausted before {target} could run"
SCAN_CUT_SHORT_DETAIL = "leeway scan for {target} cut short by the tick budget"
SCAN_FAILED_DETAIL = "leeway scan for {target} failed: {error}"

# Partial-scan error kinds (used as deduplication signature keys so rotating
# project names do not re-page every day for the same underlying condition).
ERROR_KIND_BUDGET_EXHAUSTED = "budget_exhausted"
ERROR_KIND_CUT_SHORT = "cut_short"
ERROR_KIND_TIMEOUT = "timeout"
ERROR_KIND_SCAN_FAILED = "scan_failed"
ERROR_KIND_CLUSTER_ERROR = "cluster_error"

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
# A rank finding's axis for the ledger key: its judging `rule` (`last-rank`,
# `rank0-floor`, ...), which is what distinguishes two findings on one class.
DEFAULT_TOPOLOGY_KEY_COMPUTE_CLASS = "compute-class"
DEFAULT_SEVERITY = "warning"
DEFAULT_DISPLAY_NA = "n/a"
DEFAULT_CLUSTER_SCAN_FAILED = "cluster scan failed"

# A2A blackboard topic (`a2a.topics.agent.platform.topology-skew`).
NATS_URL_ENV = "NATS_URL"
A2A_EXECUTABLE = "a2a"
A2A_TOPIC_NAME = "topology-skew"
A2A_PUBLISH_TIMEOUT_SECONDS = 30

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
SCAN_ERRORS_KEY = "scan_errors"
ANNOUNCED_KEY = "announced"
ANNOUNCED_PARTIAL_KEY = "partial"
FAILURE_MARKER_FILE_NAME = "last-failure.txt"

# Output lines (stdout is `deliver: "chat"`).
LINE_PREFIX = "leeway"
RECOVERED_LINE = "{prefix} watch: the tick runs again"
FAILED_LINE = "{prefix} watch: {what} failed: {detail}"
FAILED_WHAT_TICK = "the tick"
FAILED_WHAT_WRITE = "writing the ledger or the report"
RETIRED_LINE = "{prefix}: {count} finding(s) resolved ({names}); retired from the watch"
REPORT_LINE = (
    "{prefix}: {due_count} finding(s) due ({reasons}) across {cluster_count} cluster(s) ({clusters}); "
    "report on the gateway pod at {path}; next refresh after {next_date}"
)
PARTIAL_LINE = (
    "{prefix} watch: the fleet scan was partial ({errors} error(s)): {reasons}; "
    "findings on unreadable clusters are retained until read again"
)
PARTIAL_CLEARED_LINE = "{prefix} watch: the fleet scan reads every target again"
DRY_RUN_WOULD_REPORT = "dry run: would report {count} finding(s) ({reasons}) across {clusters}: {names}"
DRY_RUN_WOULD_RETIRE = "dry run: would retire {count} resolved finding(s) ({names})"
DRY_RUN_NOTHING_DUE = "dry run: nothing due; active findings: {count}"
MAX_NAMES_IN_LINE = 6
NAMES_OVERFLOW = ", +{more} more"
WRITE_FAILED_EXIT = 2
DATE_FORMAT = "%Y-%m-%d"


class SandboxTimedOut(RuntimeError):
    """The sandbox run hit the client-side timeout."""


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
    """Map each project to its sorted `<project>/<location>/<cluster>` triples from Cluster Agent profiles."""
    from cluster_agent_profile import RESERVED_PROFILES, read_cluster_identity

    base = Path(gitops_workspace.agent_home()) / PROFILES_DIR
    if not base.is_dir():
        return {}
    by_proj: dict[str, set[str]] = {}
    for home in base.iterdir():
        if home.name in RESERVED_PROFILES or not home.is_dir():
            continue
        try:
            identity = read_cluster_identity(home)
        except Exception:  # noqa: BLE001
            identity = None
        if not identity:
            continue
        proj = str(identity.get(IDENTITY_PROJECT_KEY) or "").strip()
        loc = str(identity.get(IDENTITY_LOCATION_KEY) or "").strip()
        cluster = str(identity.get(IDENTITY_CLUSTER_KEY) or "").strip()
        if proj:
            by_proj.setdefault(proj, set())
            if loc and cluster:
                by_proj[proj].add(CLUSTER_KEY_SEPARATOR.join([proj, loc, cluster]))
    return {proj: sorted(triples) for proj, triples in by_proj.items()}


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


def run_leeway_report(flag: str, value: str, *, timeout: int = PROJECT_TIMEOUT_SECONDS) -> dict[str, Any]:
    """Run `leeway report --topology-tier-c-signals <flag> <value>` in the sandbox."""
    argv = [LEEWAY_EXECUTABLE, LEEWAY_REPORT_SUBCOMMAND, TIER_C_FLAG, flag, value]
    try:
        completed = sandbox_exec.run(argv, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        raise SandboxTimedOut(TIMED_OUT_DETAIL.format(seconds=timeout)) from None
    stdout = (completed.stdout or "").strip()
    if not stdout:
        stderr = (completed.stderr or "").strip()
        excerpt = stderr[-STDERR_EXCERPT_CHARS:] if stderr else "no output"
        raise RuntimeError(f"leeway exited {completed.returncode} without a report: {excerpt}")
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


def _int(value: object, default: int = 0) -> int:
    if value is None or isinstance(value, bool):
        return default
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return default


def _float(value: object, default: float = 0.0) -> float:
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

    score = finding.get("score") if isinstance(finding.get("score"), dict) else {}
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
    raw_share = score.get("maxDomainShare")
    fallback_share = max_actual / denom if denom > 0 else 0.0
    max_share = _float(raw_share, fallback_share) if raw_share is not None else fallback_share
    blast_pct = round(max_share * 100)
    blast_radius = f"{max_actual}/{denom} ({blast_pct}%)" if denom > 0 else DEFAULT_DISPLAY_NA

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
        "observed_skew": _int(score.get("observedSkew"), 0),
        "max_domain_share": round(max_share, 4),
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


def extract_findings(report: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], set[str], dict[str, str]]:
    """Extract normalized findings, ok clusters, and per-cluster errors from a report."""
    findings: dict[str, dict[str, Any]] = {}
    ok_clusters: set[str] = set()
    cluster_errors: dict[str, str] = {}
    for cr in report.get("clusters", []):
        if not isinstance(cr, dict):
            continue
        cluster = canonical_cluster_name(cr)
        if not cluster:
            continue
        if cr.get("status") == STATUS_OK:
            ok_clusters.add(cluster)
            for tf in cr.get("topology_findings") or []:
                if isinstance(tf, dict):
                    norm = normalize_topology_finding(cluster, tf)
                    findings[norm["key"]] = norm
            for rf in cr.get("rank_findings") or []:
                if isinstance(rf, dict):
                    norm = normalize_rank_finding(cluster, rf)
                    findings[norm["key"]] = norm
        else:
            cluster_errors[cluster] = str(cr.get("error") or DEFAULT_CLUSTER_SCAN_FAILED)
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


def publish_blackboard_topic(summary_text: str, json_path: Path) -> bool:
    """Publish the latest leeway report to `agent.platform.topology-skew` when the A2A bus is configured."""
    if not os.environ.get(NATS_URL_ENV, "").strip() or not shutil.which(A2A_EXECUTABLE):
        return False
    argv = [
        A2A_EXECUTABLE,
        "topics",
        "write",
        A2A_TOPIC_NAME,
        "--text",
        summary_text,
        "--data",
        f"@{json_path}",
    ]
    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=A2A_PUBLISH_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


# --- failure marker ----------------------------------------------------------


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


def sweep_fleet(
    ledger: dict[str, Any],
    now: datetime,
    deadline: float,
) -> tuple[dict[str, Any], dict[str, str], dict[str, str], dict[str, str], set[str], set[str]]:
    """Run `leeway report` across configured clusters or projects."""
    scope = declared_scope()
    excluded = excluded_clusters(scope)
    clusters = [c for c in explicit_clusters() if c not in excluded]
    project_runs = dict(ledger.get(PROJECT_RUNS_KEY) or {})
    scan_errors: dict[str, str] = {}
    error_kinds: dict[str, str] = {}
    reports: list[dict[str, Any]] = []
    scanned_projects: set[str] = set()
    requested_clusters: set[str] = set(clusters)

    if clusters:
        target_label = ",".join(clusters)
        remaining = int(deadline - time.monotonic())
        if remaining < MIN_PROJECT_RUN_SECONDS:
            raise RuntimeError(BUDGET_EXHAUSTED_DETAIL.format(target=target_label))
        rep = run_leeway_report(CLUSTERS_FLAG, target_label, timeout=min(PROJECT_TIMEOUT_SECONDS, remaining))
        reports.append(rep)
        return (
            merge_reports(reports, now),
            project_runs,
            scan_errors,
            error_kinds,
            scanned_projects,
            requested_clusters,
        )

    known_triples_by_proj = roster_clusters_by_project()
    target_projects = order_by_least_recent(projects(scope), project_runs)
    for proj in target_projects:
        remaining = int(deadline - time.monotonic())
        if remaining < MIN_PROJECT_RUN_SECONDS:
            scan_errors[proj] = BUDGET_EXHAUSTED_DETAIL.format(target=proj)
            error_kinds[proj] = ERROR_KIND_BUDGET_EXHAUSTED
            continue
        timeout = min(PROJECT_TIMEOUT_SECONDS, remaining)
        triples = [t for t in (known_triples_by_proj.get(proj) or []) if t not in excluded]
        try:
            if not triples:
                triples = [t for t in project_cluster_triples(proj) if t not in excluded]
            if not triples:
                # A project with no clusters is a clean, empty read.
                scanned_projects.add(proj)
                project_runs[proj] = iso(now)
                continue
            requested_clusters.update(triples)
            rep = run_leeway_report(CLUSTERS_FLAG, ",".join(triples), timeout=timeout)
        except SandboxTimedOut:
            if timeout < PROJECT_TIMEOUT_SECONDS:
                scan_errors[proj] = SCAN_CUT_SHORT_DETAIL.format(target=proj)
                error_kinds[proj] = ERROR_KIND_CUT_SHORT
            else:
                scan_errors[proj] = TIMED_OUT_DETAIL.format(seconds=timeout)
                error_kinds[proj] = ERROR_KIND_TIMEOUT
                project_runs[proj] = iso(now)
            continue
        except Exception as exc:  # noqa: BLE001
            scan_errors[proj] = SCAN_FAILED_DETAIL.format(target=proj, error=f"{type(exc).__name__}: {exc}")
            error_kinds[proj] = ERROR_KIND_SCAN_FAILED
            if timeout == PROJECT_TIMEOUT_SECONDS:
                project_runs[proj] = iso(now)
            continue
        reports.append(rep)
        scanned_projects.add(proj)
        project_runs[proj] = iso(now)

    if not reports and scan_errors:
        reasons = format_name_list([f"{k}: {v}" for k, v in sorted(scan_errors.items())])
        raise RuntimeError(reasons)

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
    """Return True when a prior finding's absence is backed by a clean read."""
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
    # If the cluster was not in the report at all, retire when its project was
    # cleanly scanned or when the entire sweep succeeded with zero scan/cluster errors.
    proj = cluster.split(CLUSTER_KEY_SEPARATOR, 1)[0] if not is_bare_ledger_cluster else ""
    if proj and proj in scanned_projects and proj not in scan_errors:
        return True
    if scanned_projects and not scan_errors and not cluster_errors:
        return True
    return False


def tick(*, dry_run: bool = False, now: datetime | None = None) -> int:
    moment = now or now_utc()
    home = watch_home()
    days = refresh_days()
    deadline = time.monotonic() + TICK_BUDGET_SECONDS

    try:
        ledger = load_ledger(home)
        report, project_runs, scan_errors, error_kinds, scanned_projects, requested_clusters = sweep_fleet(
            ledger, moment, deadline
        )
    except Exception as exc:  # noqa: BLE001
        detail = f"{type(exc).__name__}: {exc}"
        for line in record_tick_failure(home, detail, dry_run=dry_run):
            print(line)
        return 0

    lines: list[str] = []

    active_findings, ok_clusters, cluster_errors = extract_findings(report)
    all_errors: dict[str, str] = {**scan_errors, **cluster_errors}
    all_error_kinds: dict[str, str] = {
        **error_kinds,
        **{c: ERROR_KIND_CLUSTER_ERROR for c in cluster_errors},
    }

    # Partial-scan announcement gate (keyed by error kinds so rotating project
    # names or transient text jitter do not re-page every day, and recovery is announced once).
    announced = dict(ledger.get(ANNOUNCED_KEY) or {})
    partial_sig = sorted(set(all_error_kinds.values()))
    if partial_sig:
        if announced.get(ANNOUNCED_PARTIAL_KEY) != partial_sig:
            reasons = format_name_list([f"{k}: {v}" for k, v in sorted(all_errors.items())])
            lines.append(
                PARTIAL_LINE.format(
                    prefix=LINE_PREFIX,
                    errors=len(all_errors),
                    reasons=reasons,
                )
            )
        announced[ANNOUNCED_PARTIAL_KEY] = partial_sig
    elif ANNOUNCED_PARTIAL_KEY in announced:
        del announced[ANNOUNCED_PARTIAL_KEY]
        lines.append(PARTIAL_CLEARED_LINE.format(prefix=LINE_PREFIX))

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
        next_findings[key] = {
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

    # 2. Check prior findings not seen in this tick: retire if their cluster
    #    was cleanly scanned, or retain if the cluster/project scan failed.
    for key, prior in sorted(prior_findings.items()):
        if key in active_findings or not isinstance(prior, dict):
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
            due_clusters = sorted({item["cluster"] for item in due_items})
            reasons_counts: dict[str, int] = {}
            for r in due_reasons.values():
                reasons_counts[r] = reasons_counts.get(r, 0) + 1
            reasons_str = ", ".join(f"{c} {r}" for r, c in sorted(reasons_counts.items()))
            print(
                DRY_RUN_WOULD_REPORT.format(
                    count=len(due_reasons),
                    reasons=reasons_str,
                    clusters=format_name_list(due_clusters),
                    names=format_name_list([short_finding_label(i) for i in due_items]),
                )
            )
        if not retired_labels and not due_reasons:
            print(DRY_RUN_NOTHING_DUE.format(count=len(active_findings)))
        return 0

    try:
        if retired_labels:
            retired_line = RETIRED_LINE.format(
                prefix=LINE_PREFIX,
                count=len(retired_labels),
                names=format_name_list(retired_labels),
            )
            lines.append(retired_line)
        if due_reasons:
            markdown = render_markdown_report(report, active_findings, due_reasons, all_errors, moment, days)
            md_path = write_report_files(home, report, markdown, moment)
            due_items = [active_findings[k] for k in sorted(due_reasons)]
            due_clusters = sorted({item["cluster"] for item in due_items})
            reasons_counts = {}
            for r in due_reasons.values():
                reasons_counts[r] = reasons_counts.get(r, 0) + 1
            reasons_str = ", ".join(f"{c} {r}" for r, c in sorted(reasons_counts.items()))
            next_date = (moment + timedelta(days=days)).strftime(DATE_FORMAT)
            report_line = REPORT_LINE.format(
                prefix=LINE_PREFIX,
                due_count=len(due_reasons),
                reasons=reasons_str,
                cluster_count=len(due_clusters),
                clusters=format_name_list(due_clusters),
                path=md_path,
                next_date=next_date,
            )
            lines.append(report_line)
            publish_blackboard_topic(report_line, md_path.with_suffix(REPORT_JSON_SUFFIX))
        elif retired_labels and not all_errors:
            markdown = render_markdown_report(report, active_findings, due_reasons, all_errors, moment, days)
            md_path = write_report_files(home, report, markdown, moment)
            publish_blackboard_topic(retired_line, md_path.with_suffix(REPORT_JSON_SUFFIX))

        updated_ledger: dict[str, Any] = {
            LEDGER_SCHEMA_KEY: LEDGER_SCHEMA_VERSION,
            LAST_TICK_KEY: iso(moment),
            FINDINGS_KEY: next_findings,
            PROJECT_RUNS_KEY: project_runs,
        }
        if all_errors:
            updated_ledger[SCAN_ERRORS_KEY] = all_errors
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
        detail = f"{type(exc).__name__}: {exc}"
        for line in record_tick_failure(watch_home(), detail, dry_run=args.dry_run):
            print(line)
        return 0


if __name__ == "__main__":
    sys.exit(main())
