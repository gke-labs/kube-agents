#!/usr/bin/env python3
"""stall_watch.py - post controller stalls that appeared or cleared since the last tick.

A controller that stops making progress without erroring is invisible to
everything else on the roster. ``k8s-event-watcher`` opens a triage card
within seconds of a Warning event whose reason is on its list, but a Gateway
waiting on a TLS Secret that certificate automation will never create emits
only a controller-level ``SYNC`` warning the list never names, and the watcher
never fires. The daily Workload Reliability Audit reads workload templates and
excludes Events, so it cannot see a reconcile that stopped either. Until
someone asks a Cluster Agent to run ``gke-stall-detection``, nothing looks
(#1342, slice 2).

This is a ``no_agent`` entry on the Platform Agent's roster that delivers a
report, in the shape ``agents/platform/cron/README.md`` describes: it prompts
no model, files no card, and a clean tick prints nothing, which the scheduler
relays nowhere. Each tick:

1. lists the project's clusters; ``RUNNING`` and ``RECONCILING`` clusters are
   swept (a reconciling control plane still answers), any other status is
   reported as unreadable with the status rather than skipped;
2. per cluster, fetches credentials into a per-cluster kubeconfig and lists
   the namespaces that are not system namespaces;
3. per namespace, runs the Cluster Agent's ``stall_report.py --json`` over a
   bounded list of controller kinds;
4. diffs the rows against the ledger the previous tick left and prints only
   what changed: objects that started stalling, objects whose stall cleared,
   clusters or namespaces that became unreadable or readable again.

Every ``gcloud``, ``kubectl`` and ``stall_report.py`` call runs in the shell
sandbox through ``sandbox_exec.run``: the agent container carries no kubectl
or gcloud, and the sandbox is where the credential-proxy shims live. The
sandbox login that runs them is ``hermes``, whose rule (``deploy/sandbox/
Dockerfile``) is that it never executes a file under the agent-owned
``/opt/data``. So ``stall_report.py`` is not run from the sandbox's copy: its
source is read here, from the agent image's ``/opt/defaults/scripts``, and
handed to ``python3 -I -`` on the command's stdin. ``-I`` matters as much as
the stdin: the sandbox command runs with ``/opt/data`` as its working
directory, and without isolated mode that directory is first on the module
path, so a ``json.py`` the model dropped there would be the code that ran.
What ``hermes`` executes is the image's code at this commit and the standard
library, the per-cluster kubeconfigs stay in ``hermes``'s own home where the
model cannot reach them, and the sandbox image's copy of the script being
older than the agent image's stops mattering. Nothing here mutates a cluster.

Why a bounded kind list. ``stall_report.py`` defaults to every namespaced kind
the API server knows, one ``kubectl get`` each, which is right for one
namespace a user asked about and wrong for a fleet sweep every half hour.
``DEFAULT_KINDS`` names the controllers whose stalls this watch exists for.
``STALL_WATCH_KINDS`` replaces it for a run started by hand in the pod, and
``all`` restores the script's default; the operator's env allowlist does not
carry it yet, so an install cannot set it through the CR. Pods are left out
on purpose: a Pod that cannot start raises the Warning reasons the event
watcher is gated on, and its owner shows here through its own condition or
reference.

Why a ledger. A stall lasts hours or days, and a report that reprinted it
every thirty minutes would be muted within the hour. The ledger holds one
entry per row ``stall_report.py`` emits, but chat gets one bullet per object:
an object is announced when its first row appears and when its last row
clears, and a row that joins an object already announced (a Deployment that
adds ProgressDeadlineExceeded ten minutes after its dangling reference) is
folded in silently. A ``repeating-warnings`` row exists only while its event
recurred inside the script's window, so a warning that comes back every hour
would otherwise flap in and out; such a row clears only after two
consecutive scans without it. A namespace or cluster this tick could not
read keeps its rows rather than clearing them, because absence of evidence
is not recovery; one that is gone from the listing clears them at once,
because deleting the namespace is how the motivating case usually ends. A
listing gcloud itself calls incomplete clears no row for a cluster absent
from it, and a sweep stops at
a wall-clock budget short of the schedule and reports what it did not reach,
because Hermes kills a script that runs an hour and a ledger never written
is a tick that never happened.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Siblings in `$HERMES_HOME/scripts`, this script's own directory and therefore
# already on `sys.path` when the scheduler runs it as a plain subprocess.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import gitops_workspace  # noqa: E402
import sandbox_exec  # noqa: E402
from gke_endpoint import dns_endpoint_args  # noqa: E402

# --------------------------------------------------------------------------
# constants
# --------------------------------------------------------------------------

PROFILES_DIR = "profiles"
PLATFORM_PROFILE = "platform"
CRON_DIR = "cron"
STATE_FILE_NAME = "stall_watch.json"
STATE_PATH_ENV = "STALL_WATCH_STATE"
STATE_SCHEMA_VERSION = 2
STATE_TMP_SUFFIX = ".tmp"

#: The project to sweep: the watch's own override first, then the one the
#: operator already sets on the agent container from spec.harness.projectID.
PROJECT_ENVS = ("STALL_WATCH_PROJECT", "GCP_PROJECT_ID")
KINDS_ENV = "STALL_WATCH_KINDS"
REPORT_SCRIPT_ENV = "STALL_WATCH_REPORT_SCRIPT"
#: The value of STALL_WATCH_KINDS that hands stall_report.py its own default.
ALL_KINDS = "all"
#: Controllers whose silent stalls this watch exists for. Kinds a cluster does
#: not serve cost one warning each inside stall_report.py and nothing here.
DEFAULT_KINDS = (
    "deployments",
    "statefulsets",
    "daemonsets",
    "jobs",
    "gateways.gateway.networking.k8s.io",
    "httproutes.gateway.networking.k8s.io",
    "certificates.cert-manager.io",
)
KINDS_SEPARATOR = ","

#: The agent image's copy of stall_report.py, staged by the Dockerfile and not
#: on the volume, so it is the code at this commit and nothing the model wrote.
IMAGE_REPORT_SCRIPT = "/opt/defaults/scripts/stall_report.py"
LOCAL_REPORT_SCRIPT_NAME = "stall_report.py"
PYTHON_EXECUTABLE = "python3"
#: Isolated mode: no cwd or script directory on sys.path, no user site, no
#: PYTHON* variables. Without it the sandbox's working directory, which the
#: model owns, is the first place an `import json` looks.
PYTHON_ISOLATED_FLAG = "-I"
#: `python3 -` reads the program from stdin; the script's own argv follows.
STDIN_SCRIPT_ARG = "-"
#: Per-cluster kubeconfigs, one file per cluster so two clusters never share a
#: current-context. Same directory platform_mcp_server.py uses, for the same
#: reason it gives: hermes-owned, so the model cannot plant an exec stanza. The
#: prefix keeps the watch's files apart from the MCP server's, which it would
#: otherwise rewrite under a kubectl the server is running.
SANDBOX_KUBECONFIG_DIR = "/home/hermes/.kubeconfigs"
LOCAL_KUBECONFIG_DIR = ".kubeconfigs"
KUBECONFIG_FILE_PREFIX = "stall_watch_kubeconfig_"
KUBECONFIG_FILE_SUFFIX = ".yaml"
KUBECONFIG_SLUG_SEPARATOR = "_"
KUBECONFIG_SLUG_KEEP = "-."
KUBECONFIG_SLUG_REPLACEMENT = "-"
#: Statuses under which a GKE control plane still answers. RECONCILING covers
#: every upgrade and repair window; treating it as unreadable would post a
#: "could not read" and a "readable again" per cluster per window.
SWEEPABLE_CLUSTER_STATUSES = frozenset({"RUNNING", "RECONCILING"})
#: `kubectl get namespaces -o name` prints one of these per line.
NAMESPACE_NAME_PREFIX = "namespace/"
#: gcloud exits 0 on a partial listing and says so on stderr ("The following
#: zones did not respond ... List results may be incomplete."); a cluster
#: absent from such a listing is unknown, not gone.
INCOMPLETE_LISTING_MARKERS = ("did not respond", "may be incomplete")
#: Pseudo-scopes the unreadable ledger uses for the listing and the budget.
LISTING_SCOPE = "cluster listing"
BUDGET_SCOPE = "sweep budget"
#: Wall-clock budget for one sweep. Hermes kills a no_agent script at an hour
#: and the schedule is half of that; a fleet the budget cannot cover is read in
#: the same order every tick and the remainder is reported, not lost.
TICK_BUDGET_SECONDS = 1500

#: The fleet-wide system-namespace set, spelled as the Workload Reliability
#: Audit's exclusion S1 spells it. `kubeagents-system` is deliberately absent:
#: the harness watches itself.
SYSTEM_NAMESPACES = frozenset(
    {
        "kube-system",
        "kube-public",
        "kube-node-lease",
        "gmp-system",
        "gmp-public",
        "gke-gmp-system",
        "cnrm-system",
        "configconnector-operator-system",
        "krmapihosting-system",
        "istio-system",
        "asm-system",
        "anthos-identity-service",
        "gatekeeper-system",
        "composer-system",
    }
)
SYSTEM_NAMESPACE_PREFIXES = ("gke-", "gke-managed-", "config-management-")

#: A kubectl against an unreachable cluster hangs for 300 s per call (#1799).
#: The hops before a cluster's first scan are cut short of that, and a scan
#: that times out ends that cluster's sweep for the tick, so a cluster that
#: goes dark mid-sweep costs one scan timeout rather than one per namespace.
PROJECT_LOOKUP_TIMEOUT_SECONDS = 30
CLUSTER_LIST_TIMEOUT_SECONDS = 120
GET_CREDENTIALS_TIMEOUT_SECONDS = 60
NAMESPACE_LIST_TIMEOUT_SECONDS = 60
NAMESPACE_SCAN_TIMEOUT_SECONDS = 300
#: stall_report.py exits 2 when it could read no kind at all; anything else
#: non-zero is a crash, and both leave the namespace unread this tick.
REPORT_UNREADABLE_EXIT = 2

#: Consecutive scans a row must be absent from before it clears, per
#: heuristic; one for everything not named here.
CLEAR_AFTER_MISSED_SCANS = {"repeating-warnings": 2}
DEFAULT_CLEAR_AFTER_MISSED_SCANS = 1
#: Chat renders a bullet list; past this many objects per section the rest is a count.
MAX_LISTED_ROWS = 30
#: One bullet per object, with at most this many of its rows spelled out.
MAX_DETAILS_PER_OBJECT = 3
DETAIL_JOINER = "; "
LEDGER_KEY_SEPARATOR = "|"
#: A cluster is `name@location`: two projects' clusters may share a name
#: across regions, and the kubeconfig and every sibling key on both.
CLUSTER_ID_SEPARATOR = "@"
SCOPE_SEPARATOR = "/"
#: A repeating-warnings detail carries the event count (`SYNC x743: ...`), which
#: rises every tick; the ledger keys the row on the detail with the count removed.
EVENT_COUNT_IN_DETAIL = re.compile(r"^(\S+) x\d+: ")
HEADLINE_PREFIX = "🧭 **Controller stall watch**"
NEW_HEADING = "**New stalls**"
CLEARED_HEADING = "**Cleared**"
UNREADABLE_HEADING = "**Could not read**"
READABLE_AGAIN_HEADING = "**Readable again**"
SWEEP_FAILED_PREFIX = "⚠️ **Controller stall watch — sweep failed:**"
SWEEP_RECOVERED_LINE = "✅ **Controller stall watch** — the sweep runs again."
STDERR_EXCERPT_CHARS = 200

# --------------------------------------------------------------------------
# sandbox plumbing
# --------------------------------------------------------------------------


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def run_sandbox(
    argv: list[str], *, timeout: float, kubeconfig: str | None = None, stdin: str | None = None
) -> subprocess.CompletedProcess:
    """One hop into the shell sandbox. Only KUBECONFIG crosses; the rest of
    this process's environment has no business on the other side."""
    remote_env = {"KUBECONFIG": kubeconfig} if kubeconfig else None
    return sandbox_exec.run(argv, remote_env=remote_env, timeout=timeout, check=False, stdin=stdin)


def stderr_excerpt(text: str | None) -> str:
    text = " ".join((text or "").split())
    return text[:STDERR_EXCERPT_CHARS]


def failure_text(exc: BaseException) -> str:
    if isinstance(exc, subprocess.TimeoutExpired):
        return f"timed out after {int(exc.timeout)}s"
    if isinstance(exc, RuntimeError):
        return str(exc)
    return f"{type(exc).__name__}: {exc}"


def parse_json(text: str, what: str):
    try:
        return json.loads(text or "null")
    except ValueError as exc:
        raise RuntimeError(f"{what} returned unparsable output: {exc}") from exc


def kubeconfig_slug(part: str) -> str:
    return "".join(ch if ch.isalnum() or ch in KUBECONFIG_SLUG_KEEP else KUBECONFIG_SLUG_REPLACEMENT for ch in part)


def kubeconfig_path(project: str, cluster: str, location: str) -> str:
    if sandbox_exec.sandbox_enabled():
        directory = SANDBOX_KUBECONFIG_DIR
    else:
        directory = os.path.join(gitops_workspace.agent_home(), LOCAL_KUBECONFIG_DIR)
        os.makedirs(directory, exist_ok=True)
    slug = KUBECONFIG_SLUG_SEPARATOR.join(kubeconfig_slug(p) for p in (project, cluster, location))
    return os.path.join(directory, f"{KUBECONFIG_FILE_PREFIX}{slug}{KUBECONFIG_FILE_SUFFIX}")


def report_source() -> str:
    """The text of stall_report.py that travels to the sandbox on stdin."""
    override = os.environ.get(REPORT_SCRIPT_ENV)
    candidates = [override] if override else [IMAGE_REPORT_SCRIPT, str(Path(__file__).resolve().parent / LOCAL_REPORT_SCRIPT_NAME)]
    for candidate in candidates:
        try:
            return Path(candidate).read_text()
        except OSError:
            continue
    raise RuntimeError(f"stall_report.py not found at {', '.join(candidates)}")


def report_argv(namespace: str) -> list[str]:
    cmd = [PYTHON_EXECUTABLE, PYTHON_ISOLATED_FLAG, STDIN_SCRIPT_ARG, "--namespace", namespace, "--json"]
    kinds = kinds_argument()
    if kinds:
        cmd += ["--kind", kinds]
    return cmd


def kinds_argument() -> str | None:
    """The --kind value for stall_report.py, or None to let it scan every kind."""
    raw = os.environ.get(KINDS_ENV, "").strip()
    if raw.lower() == ALL_KINDS:
        return None
    if raw:
        return KINDS_SEPARATOR.join(k.strip() for k in raw.split(KINDS_SEPARATOR) if k.strip())
    return KINDS_SEPARATOR.join(DEFAULT_KINDS)


# --------------------------------------------------------------------------
# fleet reads
# --------------------------------------------------------------------------


def project_id() -> str | None:
    for name in PROJECT_ENVS:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    r = run_sandbox(["gcloud", "config", "get-value", "project"], timeout=PROJECT_LOOKUP_TIMEOUT_SECONDS)
    return r.stdout.strip() or None


def list_clusters(project: str) -> tuple[list[dict], str | None]:
    """Every cluster as {name, location, status}, and why the listing is
    incomplete when gcloud said so. Raises on a list that could not be read,
    so the caller can tell an empty project from a failed call."""
    r = run_sandbox(
        ["gcloud", "container", "clusters", "list", f"--project={project}", "--format=json"],
        timeout=CLUSTER_LIST_TIMEOUT_SECONDS,
    )
    if r.returncode != 0:
        raise RuntimeError(f"gcloud container clusters list exited {r.returncode}: {stderr_excerpt(r.stderr)}")
    clusters = parse_json(r.stdout, "gcloud container clusters list") or []
    stderr = (r.stderr or "").lower()
    incomplete = stderr_excerpt(r.stderr) if any(m in stderr for m in INCOMPLETE_LISTING_MARKERS) else None
    return [
        {"name": c["name"], "location": c["location"], "status": c.get("status", "")}
        for c in clusters
        if isinstance(c, dict) and c.get("name") and c.get("location")
    ], incomplete


def fetch_credentials(project: str, cluster: str, location: str) -> str:
    """Point a per-cluster kubeconfig at the cluster and return its path."""
    path = kubeconfig_path(project, cluster, location)
    cmd = [
        "gcloud", "container", "clusters", "get-credentials", cluster,
        f"--location={location}", f"--project={project}",
        *dns_endpoint_args(project, cluster, location),
    ]
    r = run_sandbox(cmd, timeout=GET_CREDENTIALS_TIMEOUT_SECONDS, kubeconfig=path)
    if r.returncode != 0:
        raise RuntimeError(f"get-credentials exited {r.returncode}: {stderr_excerpt(r.stderr)}")
    return path


def is_system_namespace(name: str) -> bool:
    return name in SYSTEM_NAMESPACES or name.startswith(SYSTEM_NAMESPACE_PREFIXES)


def list_namespaces(kubeconfig: str) -> list[str]:
    """Every non-system namespace, Terminating ones included: their objects
    are still listable, and one whose content is gone is how a ledger row
    clears while the namespace waits on a finalizer."""
    r = run_sandbox(["kubectl", "get", "namespaces", "-o", "name"], timeout=NAMESPACE_LIST_TIMEOUT_SECONDS, kubeconfig=kubeconfig)
    if r.returncode != 0:
        raise RuntimeError(f"kubectl get namespaces exited {r.returncode}: {stderr_excerpt(r.stderr)}")
    names = []
    for line in r.stdout.splitlines():
        line = line.strip()
        if line.startswith(NAMESPACE_NAME_PREFIX):
            names.append(line[len(NAMESPACE_NAME_PREFIX):])
    return sorted(n for n in names if n and not is_system_namespace(n))


def scan_namespace(kubeconfig: str, namespace: str, source: str) -> list[dict]:
    """The findings stall_report.py reports for one namespace. Raises when the
    namespace could not be read, so the caller keeps its ledger rows."""
    r = run_sandbox(report_argv(namespace), timeout=NAMESPACE_SCAN_TIMEOUT_SECONDS, kubeconfig=kubeconfig, stdin=source)
    if r.returncode == REPORT_UNREADABLE_EXIT:
        raise RuntimeError(f"no kind could be read: {stderr_excerpt(r.stderr)}")
    if r.returncode != 0:
        raise RuntimeError(f"stall_report.py exited {r.returncode}: {stderr_excerpt(r.stderr)}")
    report = parse_json(r.stdout, "stall_report.py") or {}
    return report.get("findings") or []


# --------------------------------------------------------------------------
# ledger
# --------------------------------------------------------------------------


def empty_state() -> dict:
    return {"version": STATE_SCHEMA_VERSION, "stalls": {}, "unreadable": {}, "sweep_error": None, "updated_at": None}


def load_state(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return empty_state()
    if not isinstance(data, dict) or data.get("version") != STATE_SCHEMA_VERSION:
        return empty_state()
    state = empty_state()
    state.update({k: data.get(k, v) for k, v in state.items()})
    return state


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + STATE_TMP_SUFFIX)
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, path)


def stable_detail(detail: str) -> str:
    return EVENT_COUNT_IN_DETAIL.sub(r"\1: ", detail)


def cluster_id(name: str, location: str) -> str:
    return f"{name}{CLUSTER_ID_SEPARATOR}{location}"


def cluster_label(cid: str) -> str:
    name, sep, location = cid.partition(CLUSTER_ID_SEPARATOR)
    return f"`{name}` ({location})" if sep else f"`{name}`"


def ledger_key(cid: str, finding: dict) -> str:
    return LEDGER_KEY_SEPARATOR.join(
        [cid, finding.get("namespace", ""), finding.get("object", ""), finding.get("heuristic", ""), stable_detail(finding.get("detail", ""))]
    )


def scope_key(cid: str, namespace: str | None = None) -> str:
    return cid if namespace is None else f"{cid}{SCOPE_SEPARATOR}{namespace}"


def split_scope(scope: str) -> tuple[str, str | None]:
    cid, sep, namespace = scope.partition(SCOPE_SEPARATOR)
    return cid, (namespace if sep else None)


def scope_label(scope: str) -> str:
    if scope == LISTING_SCOPE:
        return "the cluster listing"
    if scope == BUDGET_SCOPE:
        return "the rest of the fleet (sweep budget)"
    cid, namespace = split_scope(scope)
    label = cluster_label(cid)
    return label if namespace is None else f"{label} {SCOPE_SEPARATOR} `{namespace}`"


def clear_after(heuristic: str) -> int:
    return CLEAR_AFTER_MISSED_SCANS.get(heuristic, DEFAULT_CLEAR_AFTER_MISSED_SCANS)


# --------------------------------------------------------------------------
# one tick
# --------------------------------------------------------------------------

GONE = "gone"
ABSENT = "absent"
UNKNOWN = "unknown"


class Sweep:
    """What one pass over the fleet saw."""

    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}
        #: `cluster/namespace` scopes whose scan completed this tick.
        self.read_scopes: set[str] = set()
        #: Every cluster the project listed, whatever its status, and whether
        #: gcloud vouched for the listing being complete.
        self.listed_clusters: set[str] = set()
        self.listing_complete = True
        #: Clusters whose namespace listing succeeded, and those namespaces.
        self.read_clusters: set[str] = set()
        self.listed_namespaces: dict[str, set[str]] = {}
        self.unreadable: dict[str, str] = {}
        self.budget_exhausted = False

    @property
    def clusters(self) -> int:
        return len(self.read_clusters)

    @property
    def namespaces(self) -> int:
        return len(self.read_scopes)

    def verdict(self, cid: str, namespace: str) -> str:
        """What this tick can say about a ledger row in that scope: its
        namespace or cluster is GONE from the listing, the namespace was
        scanned and the row was ABSENT, or the scope was not read and the row
        is UNKNOWN."""
        if cid not in self.listed_clusters:
            return GONE if self.listing_complete else UNKNOWN
        namespaces = self.listed_namespaces.get(cid)
        if namespaces is not None and namespace not in namespaces:
            return GONE
        return ABSENT if scope_key(cid, namespace) in self.read_scopes else UNKNOWN

    def scope_read(self, scope: str) -> bool:
        """A namespace scope counts as read when it was scanned; a cluster
        scope when its namespaces were listed, whether or not it had any."""
        if scope == LISTING_SCOPE:
            return self.listing_complete
        if scope == BUDGET_SCOPE:
            return not self.budget_exhausted
        cid, namespace = split_scope(scope)
        if namespace is not None:
            return scope in self.read_scopes
        return cid in self.read_clusters

    def scope_gone(self, scope: str) -> bool:
        if scope in (LISTING_SCOPE, BUDGET_SCOPE):
            return False
        cid, namespace = split_scope(scope)
        if cid not in self.listed_clusters:
            return self.listing_complete
        namespaces = self.listed_namespaces.get(cid)
        return namespace is not None and namespaces is not None and namespace not in namespaces

    def out_of_budget(self, started: float) -> bool:
        if self.budget_exhausted:
            return True
        if time.monotonic() - started <= TICK_BUDGET_SECONDS:
            return False
        self.budget_exhausted = True
        self.unreadable[BUDGET_SCOPE] = (
            f"sweep budget of {TICK_BUDGET_SECONDS}s exhausted after {self.clusters} clusters and "
            f"{self.namespaces} namespaces; the rest were not read this tick"
        )
        return True


READ_FAILURES = (RuntimeError, subprocess.TimeoutExpired, sandbox_exec.SandboxUnavailable, OSError)


def sweep_fleet(project: str) -> Sweep:
    sweep = Sweep()
    started = time.monotonic()
    source = report_source()
    clusters, incomplete = list_clusters(project)
    if incomplete:
        sweep.listing_complete = False
        sweep.unreadable[LISTING_SCOPE] = f"incomplete: {incomplete}"
    # Every listed cluster is registered before any is read: a cluster the
    # budget never reaches is unread, not gone.
    sweepable = []
    for cluster in clusters:
        cid = cluster_id(cluster["name"], cluster["location"])
        sweep.listed_clusters.add(cid)
        if cluster["status"] in SWEEPABLE_CLUSTER_STATUSES:
            sweepable.append((cid, cluster))
        else:
            sweep.unreadable[scope_key(cid)] = f"status={cluster['status'] or 'unknown'}"
    for cid, cluster in sweepable:
        name, location = cluster["name"], cluster["location"]
        if sweep.out_of_budget(started):
            break
        try:
            kubeconfig = fetch_credentials(project, name, location)
            namespaces = list_namespaces(kubeconfig)
        except READ_FAILURES as exc:
            sweep.unreadable[scope_key(cid)] = failure_text(exc)
            continue
        sweep.read_clusters.add(cid)
        sweep.listed_namespaces[cid] = set(namespaces)
        for namespace in namespaces:
            if sweep.out_of_budget(started):
                break
            try:
                findings = scan_namespace(kubeconfig, namespace, source)
            except subprocess.TimeoutExpired as exc:
                sweep.unreadable[scope_key(cid)] = (
                    f"namespace {namespace} {failure_text(exc)}; the cluster's remaining namespaces were skipped this tick"
                )
                break
            except READ_FAILURES as exc:
                sweep.unreadable[scope_key(cid, namespace)] = failure_text(exc)
                continue
            sweep.read_scopes.add(scope_key(cid, namespace))
            for f in findings:
                sweep.rows[ledger_key(cid, f)] = {
                    "cluster": cid,
                    "namespace": f.get("namespace", namespace),
                    "object": f.get("object", ""),
                    "heuristic": f.get("heuristic", ""),
                    "detail": f.get("detail", ""),
                    "message": f.get("message", ""),
                    "stalled_for": f.get("stalled_for", ""),
                    "stalled_seconds": int(f.get("stalled_seconds") or 0),
                }
    return sweep


def object_key(row: dict) -> tuple[str, str, str]:
    return (row.get("cluster", ""), row.get("namespace", ""), row.get("object", ""))


def group_by_object(rows: list[dict]) -> dict[tuple[str, str, str], list[dict]]:
    grouped: dict[tuple[str, str, str], list[dict]] = {}
    for row in rows:
        grouped.setdefault(object_key(row), []).append(row)
    return grouped


def row_text(row: dict) -> str:
    detail = row["detail"]
    if row.get("message"):
        detail = f"{detail}: {row['message']}"
    return f"{row['heuristic']} {detail}"


def names_a_referent(row: dict) -> bool:
    return bool(row.get("message")) or "not found" in row.get("detail", "")


def describe_object(key: tuple[str, str, str], rows: list[dict]) -> str:
    """One bullet: the object, its longest stall age, and its rows with the
    ones that name a referent first, since that is what a reader acts on."""
    cid, namespace, obj = key
    ordered = sorted(rows, key=lambda r: (not names_a_referent(r), r.get("heuristic", "")))
    shown = [row_text(r) for r in ordered[:MAX_DETAILS_PER_OBJECT]]
    more = len(ordered) - len(shown)
    if more:
        shown.append(f"and {more} more")
    oldest = max(rows, key=lambda r: int(r.get("stalled_seconds") or 0))
    age = f" ({oldest['stalled_for']})" if oldest.get("stalled_for") else ""
    return f"- {cluster_label(cid)} {SCOPE_SEPARATOR} `{namespace}` — {obj}{age}: {DETAIL_JOINER.join(shown)}"


def section(heading: str, lines: list[str]) -> list[str]:
    if not lines:
        return []
    shown = lines[:MAX_LISTED_ROWS]
    more = len(lines) - len(shown)
    if more:
        shown.append(f"- and {more} more")
    return [heading, *shown]


def diff_and_update(state: dict, sweep: Sweep, now: str) -> list[str]:
    """Fold the sweep into the ledger and return the lines worth posting."""
    previous = state["stalls"]
    current = dict(previous)
    for key, row in sweep.rows.items():
        entry = previous.get(key)
        if entry is None:
            entry = {**row, "first_seen": now}
        current[key] = {**entry, **row, "last_seen": now, "missed": 0}
    for key, entry in previous.items():
        if key in sweep.rows:
            continue
        verdict = sweep.verdict(entry.get("cluster", ""), entry.get("namespace", ""))
        if verdict == UNKNOWN:
            continue
        missed = int(entry.get("missed") or 0) + 1
        if verdict == GONE or missed >= clear_after(entry.get("heuristic", "")):
            del current[key]
        else:
            current[key] = {**entry, "missed": missed}
    state["stalls"] = current
    known_before = {object_key(e) for e in previous.values()}
    known_after = {object_key(e) for e in current.values()}
    new_objects = {k: rows for k, rows in group_by_object(list(sweep.rows.values())).items() if k not in known_before}
    cleared_objects = {k: rows for k, rows in group_by_object(list(previous.values())).items() if k not in known_after}

    prior_unreadable = state["unreadable"]
    newly_unreadable = [f"- {scope_label(scope)}: {reason}" for scope, reason in sorted(sweep.unreadable.items()) if scope not in prior_unreadable]
    readable_again = [
        f"- {scope_label(scope)}"
        for scope in sorted(prior_unreadable)
        if scope not in sweep.unreadable and sweep.scope_read(scope)
    ]
    state["unreadable"] = {
        scope: reason
        for scope, reason in {**prior_unreadable, **sweep.unreadable}.items()
        if scope in sweep.unreadable or not (sweep.scope_read(scope) or sweep.scope_gone(scope))
    }
    state["updated_at"] = now

    lines: list[str] = []
    lines += section(NEW_HEADING, [describe_object(k, rows) for k, rows in sorted(new_objects.items())])
    lines += section(CLEARED_HEADING, [describe_object(k, rows) for k, rows in sorted(cleared_objects.items())])
    lines += section(UNREADABLE_HEADING, newly_unreadable)
    lines += section(READABLE_AGAIN_HEADING, readable_again)
    if not lines:
        return []
    headline = (
        f"{HEADLINE_PREFIX} — {len(new_objects)} new, {len(cleared_objects)} cleared "
        f"(swept {sweep.clusters} clusters, {sweep.namespaces} namespaces; {len(known_after)} stalled objects open)"
    )
    return [headline, *lines]


def tick(state_path: Path, *, dry_run: bool) -> list[str]:
    state = load_state(state_path)
    now = now_iso()
    lines: list[str] = []
    try:
        project = project_id()
        if not project:
            raise RuntimeError(f"no GCP project: set {PROJECT_ENVS[0]} or configure gcloud in the sandbox")
        sweep = sweep_fleet(project)
    except Exception as exc:  # noqa: BLE001 - a failed sweep is reported once, not raised every tick
        text = failure_text(exc)
        if state.get("sweep_error") != text:
            lines.append(f"{SWEEP_FAILED_PREFIX} {text}")
        state["sweep_error"] = text
        state["updated_at"] = now
    else:
        if state.get("sweep_error"):
            lines.append(SWEEP_RECOVERED_LINE)
        state["sweep_error"] = None
        lines += diff_and_update(state, sweep, now)
    if not dry_run:
        save_state(state_path, state)
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="sweep and print; write no state")
    parser.add_argument("--state", type=Path, help=f"ledger path (default: <home>/{PROFILES_DIR}/{PLATFORM_PROFILE}/{CRON_DIR}/{STATE_FILE_NAME}, or ${STATE_PATH_ENV})")
    args = parser.parse_args(argv)
    agent_home = Path(gitops_workspace.agent_home())
    state_path = args.state or Path(os.environ.get(STATE_PATH_ENV) or agent_home / PROFILES_DIR / PLATFORM_PROFILE / CRON_DIR / STATE_FILE_NAME)
    lines = tick(state_path, dry_run=args.dry_run)
    if lines:
        print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
