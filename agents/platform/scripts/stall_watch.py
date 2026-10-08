#!/usr/bin/env python3
"""stall_watch.py - raise an alert for controller stalls that appeared since the last tick, and close what cleared.

A controller that stops making progress without erroring is invisible to
everything else on the roster. ``k8s-event-watcher`` opens a triage card
within seconds of a Warning event whose reason is on its list, but a Gateway
waiting on a TLS Secret that certificate automation will never create emits
only a controller-level ``SYNC`` warning the list never names, and the watcher
never fires. The daily Workload Reliability Audit reads workload templates and
excludes Events, so it cannot see a reconcile that stopped either. Until
someone asks a Cluster Agent to run ``gke-stall-detection``, nothing looks
(#1342, slice 2).

This is a ``no_agent`` entry on the Platform Agent's roster. The tick prompts
no model. Each tick:

1. lists the clusters of the management project and of every project a
   Cluster Agent profile's ``cluster_identity`` names, which is how a project
   ``spec.scope`` brought in reaches the watch; ``RUNNING`` and
   ``RECONCILING`` clusters with a scaffolded Cluster Agent profile are swept
   (a reconciling control plane still answers), any other status is recorded
   as unreadable with the status rather than skipped, and a cluster with no
   profile, one the reconciler pruned for ``spec.scope.exclude.clusters`` or
   ``RECONCILE_EXCLUDE`` or has not yet scaffolded, is left unread: the
   exclusion is the operator keeping a model turn off that cluster, and this
   watch follows the same roster;
2. per cluster, fetches credentials into a per-cluster kubeconfig and lists
   the namespaces that are not system namespaces;
3. per namespace, runs the Cluster Agent's ``stall_report.py --json`` over a
   bounded list of controller kinds;
4. diffs the rows against the ledger the previous tick left, and on a new
   stall episode in a namespace hands it to the Session KV server as a
   ``controller-stall`` inject: the route ``k8s-event-watcher`` and the drift
   detector use. The server posts the alert, records its chat thread and
   starts a Planning Agent turn that files one card to the cluster's Cluster
   Agent, which runs ``gke-stall-detection`` and reports in the triage
   template; the report lands in the alert's thread and is saved, so a reply
   of "apply" reaches the agent that opens the pull request with the report
   attached (docs/designs/stall-watch-inject.md). The watch files no card
   itself: a card it filed would have no session, and so no thread. It finds
   the card by the session that filed it, and from then on a new object in
   the namespace is a comment on that card, and when every object has
   cleared the card gets a closing comment and is completed. At most
   ``MAX_ALERTS_PER_TICK`` alerts are raised per tick; the namespaces past
   that wait for the next one. Chat gets the alert for a new episode, and the
   watch's own lines: a stall cleared, namespaces held for the next tick,
   alerts refused and raised again, and the sweep failing and recovering.

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

Why a ledger. A stall lasts hours or days, and a watch that raised an alert
for it every thirty minutes would be muted within the hour. The ledger holds
one entry per row ``stall_report.py`` emits and one episode per namespace with
an open alert: the alert is raised when a namespace's first row appears and
its card closes when its last row clears, a new object in the namespace is a
comment on the card, and a row that joins an object already on it (a Deployment that adds
ProgressDeadlineExceeded ten minutes after its dangling reference) is folded
in silently. A ``repeating-warnings`` row exists only while its event
recurred inside the script's window, so a warning that comes back every hour
would otherwise flap in and out; such a row clears only after two
consecutive scans without it. A namespace or cluster this tick could not
read keeps its rows rather than clearing them, because absence of evidence
is not recovery, and so does a row whose own kind a scan skipped, or a
repeating-warnings row when the events were not read (the kind list is
filtered per cluster to what it serves, so such a skip is a failure and never
a missing CRD, and every other row in the namespace is judged as usual); one that is gone from the listing clears
them at once, because deleting the namespace is how the motivating case
usually ends. A listing gcloud itself calls incomplete clears no row for a
cluster absent from it, and a sweep stops at a wall-clock budget short of
the schedule and reports what it did not reach, because Hermes kills a
script that runs an hour and a ledger never written is a tick that never
happened.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import shlex
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor, wait
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
STATE_SCHEMA_VERSION = 4
#: The version that keyed a cluster without its project. It swept only the
#: management project, so its keys are read as that project's.
PROJECTLESS_SCHEMA_VERSION = 3
STATE_TMP_SUFFIX = ".tmp"

#: The management project, listed on every tick: the watch's own override
#: first, then the one the operator already sets on the agent container from
#: spec.harness.projectID. The other projects are the Cluster Agent roster's.
PROJECT_ENVS = ("STALL_WATCH_PROJECT", "GCP_PROJECT_ID")
KINDS_ENV = "STALL_WATCH_KINDS"
REPORT_SCRIPT_ENV = "STALL_WATCH_REPORT_SCRIPT"
#: The value of STALL_WATCH_KINDS that hands stall_report.py its own default.
ALL_KINDS = "all"
#: Controllers whose silent stalls this watch exists for. Per cluster, the
#: list is cut to what the cluster serves before it is handed to the script.
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
#: The kind list is filtered to what a cluster serves, from one
#: `kubectl api-resources` per cluster, so a warning from the report script
#: about a kind it could not scan is always a failure and never "this cluster
#: has no cert-manager".
API_RESOURCES_ARGV = ("kubectl", "api-resources", "--namespaced=true", "--verbs=list", "-o", "name")
API_RESOURCES_TIMEOUT_SECONDS = 60
#: stall_report.py exits 0 after skipping a kind or the events it could not
#: read and says so on stderr, naming the resource; such a scan clears no
#: row of that kind (or no repeating-warnings row, for the events) and every
#: other row in the namespace is judged as usual.
SKIPPED_KIND_PATTERN = re.compile(r"warning: (\S+) in \S+ not scanned")
EVENTS_NOT_READ_PATTERN = re.compile(r"warning: events in \S+ not read")
EVENTS_MARKER = "events"
REPEATING_WARNINGS_HEURISTIC = "repeating-warnings"
PLURAL_IES = "ies"
PLURAL_ES = "es"
#: gcloud exits 0 on a partial listing and says so on stderr ("The following
#: zones did not respond ... List results may be incomplete."); a cluster
#: absent from such a listing is unknown, not gone.
INCOMPLETE_LISTING_MARKERS = ("did not respond", "may be incomplete")
#: Pseudo-scopes the unreadable ledger uses for the listing and the budget.
LISTING_SCOPE = "cluster listing"
BUDGET_SCOPE = "sweep budget"
#: Wall-clock budget for one sweep. Hermes kills a no_agent script at an hour
#: and the schedule is half of that. A fleet the budget cannot cover is read
#: from where the last tick stopped, so every scope is reached in turn and the
#: remainder is reported, not lost.
TICK_BUDGET_SECONDS = 1500
#: Ledger key for where an exhausted sweep stopped: the cluster and namespace
#: the next tick starts from.
CURSOR_KEY = "cursor"

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
#: Projects are listed the way the reconciler lists them: the management
#: project first and alone, since its ssh opens the connection the rest share,
#: then LIST_WORKERS at a time, each gcloud timeout cut to the budget left. A
#: listing still running at the deadline is unlisted this tick. One at a time,
#: a hundred projects (the scope's cap) each hanging to the gcloud timeout would
#: outlast the whole tick.
#: Every listing is a gcloud process the credential proxy runs. The pool was
#: sized to the four requests the proxy admitted at its old 1Gi default
#: (docs/designs/credential-proxy-child-memory-budget.md §2.2), past which a
#: listing queues at the proxy and one still waiting at its 60 s admission
#: bound is refused busy and reads unlisted. At the 2Gi default the proxy's
#: slot cap of eight binds, and it is shared: while this pool lists, kanban
#: workers' commands have four slots, and none if cluster_agent_reconcile.py's
#: four-wide pool is listing at the same time.
LIST_WORKERS = 4
#: 12 s per listing at four wide: the per-listing share the 150 s budget gave
#: at eight. The listing runs inside TICK_BUDGET_SECONDS, so it leaves the
#: scans at least 1500 - 300 - LIST_GRACE_SECONDS.
LIST_BUDGET_SECONDS = 300
LIST_GRACE_SECONDS = 5
GET_CREDENTIALS_TIMEOUT_SECONDS = 60
NAMESPACE_LIST_TIMEOUT_SECONDS = 60
NAMESPACE_SCAN_TIMEOUT_SECONDS = 300
#: stall_report.py exits 2 when it could read no kind at all; anything else
#: non-zero is a crash, and both leave the namespace unread this tick.
REPORT_UNREADABLE_EXIT = 2

#: Where the ledger keeps an open alert and its card per `cluster/namespace` scope.
EPISODES_KEY = "episodes"
#: Only a cluster with a scaffolded Cluster Agent profile is swept, and its
#: alert names that profile. A cluster without one, pruned for the scope's
#: exclude.clusters or RECONCILE_EXCLUDE or not yet scaffolded, is neither read nor
#: alerted for: the exclusion is the operator keeping a model turn off that
#: cluster, and an alert would hand its rows to another profile instead.
NO_PROFILE_REASON = "no Cluster Agent profile; not read"
#: A profile whose cluster_identity cannot be read adds no project to the
#: roster. Its cluster is swept when its project is listed anyway, and otherwise
#: keeps its rows.
NO_IDENTITY_REASON = "no readable cluster_identity; adds no project to the sweep"
#: The ledger's key for such a profile.
PROFILE_SCOPE = "profile"
#: Alerts raised per tick. Each starts a Planning Agent turn and a Cluster
#: Agent turn, and the number of namespaces with a new stall is chosen by
#: whoever can create namespaces, so the rest keep their rows and wait, oldest
#: first sighting first: a tenant filling three fresh namespaces every tick
#: cannot keep an older stall from its alert. The default of
#: github_scan_gate's PR_AGENT_MAX_PER_TICK. A stall claims no alert quota on
#: the Session KV server, so this is the only bound.
MAX_ALERTS_PER_TICK = 3
#: Object names a chat line or card comment spells out before counting the rest.
MAX_OBJECTS_IN_LINE = 8
#: The longest name the Session KV server takes (`DRIFT_MAX_FIELD_CHARS` there).
#: It refuses a record with a longer namespace or cluster, and drops a longer
#: object name, so object names are cut to it here: a namespace whose stalled
#: objects all have longer names would otherwise never be alerted.
MAX_NAME_CHARS = 200
#: The HTTP status the server answers a record it refuses with: a fault in that
#: one record, not in the server.
HTTP_BAD_REQUEST = 400
#: Rows one record carries. The card lists sixty of them and counts the rest;
#: this keeps one namespace with thousands of stalled objects from becoming a
#: request of the same size.
MAX_ROWS_IN_RECORD = 500
#: The Session KV server on the pod's loopback, and the kind its inject route
#: dispatches a stall on (`INJECT_KIND_STALL` in session_kv_server.py).
SESSION_KV_URL = "http://127.0.0.1:8699"
SESSION_KV_AUTH_ENV = "SESSION_KV_API_KEY"
SESSION_KV_TIMEOUT_SECONDS = 30
CONTENT_TYPE_HEADER = "Content-Type"
JSON_CONTENT_TYPE = "application/json"
AUTH_HEADER = "Authorization"
BEARER_PREFIX = "Bearer "
HEALTHZ_PATH = "/healthz"
SESSIONS_PATH = "/sessions"
INJECT_SUFFIX = "/inject"
INJECT_KIND = "controller-stall"
INJECTED_STATUS = "injected"
#: The Session KV server's field names: what /healthz lists, what POST
#: /sessions answers, the inject envelope's one field, and its answer.
INJECT_KINDS_KEY = "inject_kinds"
SESSION_ID_KEY = "sessionID"
MESSAGE_KEY = "message"
STATUS_KEY = "status"
#: An alert whose session has filed no card a day after it was raised is raised
#: again, and the new alert replaces its episode: the Planning Agent turn it started
#: failed, and a stall can outlast that by days. A day rather than a few ticks,
#: because a shorter retry posts a fresh "digging down" alert every hour or so
#: for as long as the turns keep failing.
UNFILED_ALERT_RETRY_SECONDS = 24 * 60 * 60
#: The episode field naming the alert's session, and the board column the card
#: carries it in.
SESSION_KEY = "session"
SESSION_COLUMN = "session_id"
INJECT_FAILED_PREFIX = "⚠️ **Controller stall watch — alerts not raised:**"
INJECT_RECOVERED_LINE = "✅ **Controller stall watch** — alerts are raised again."
#: The ledger's record of the last refusal, so its chat line is said once.
INJECT_ERROR_KEY = "inject_error"
#: A card's status once it is no longer being worked; a new object then raises
#: a new alert rather than commenting on a finished card.
TERMINAL_CARD_STATUSES = frozenset({"done", "archived", "cancelled", "failed"})
#: The board is whatever Hermes' own kanban_db_path() resolves (HERMES_KANBAN_DB,
#: then kanban/current), falling back to the agent home's kanban.db, the same
#: file kanban_board_health.board_path names.
BOARD_DB_NAME = "kanban.db"
BOARD_BUSY_TIMEOUT_SECONDS = 10
NOTICED_PREFIX = "🧭 stall noticed"
CLEARED_PREFIX = "✅ stall cleared"
DRY_RUN_PREFIX = "dry run:"
#: A card the board no longer has, or cannot describe for this many ticks
#: running, ends its episode so the namespace is not wedged behind it.
MAX_UNKNOWN_CARD_TICKS = 3
#: A card the Cluster Agent is working on is not completed under it when the
#: stall clears; the watch comments and lets the worker finish, then closes
#: the episode once the card reaches a terminal status.
RUNNING_CARD_STATUS = "running"
#: Plurals kubectl forms with -es or -ies, so a skipped `ingresses` holds
#: Ingress rows and `networkpolicies` holds NetworkPolicy rows.
PLURAL_ES_SUFFIXES = ("sses", "shes", "ches", "xes", "zes")
#: Consecutive scans a row must be absent from before it clears, per
#: heuristic; one for everything not named here. A repeating-warnings row
#: exists only while its event recurred inside the report's window, and a
#: dangling-reference row vanishes for a scan whose one referent listing
#: failed while the object listing succeeded; both flap on one miss.
CLEAR_AFTER_MISSED_SCANS = {"repeating-warnings": 2, "dangling-reference": 2}
DEFAULT_CLEAR_AFTER_MISSED_SCANS = 1
LEDGER_KEY_SEPARATOR = "|"
#: A cluster is `project:name@location`, the triple the scope's
#: exclude.clusters names. A domain-scoped project ID has a colon of its own
#: and cluster names and locations have neither separator, so a key is split
#: from the right.
CLUSTER_ID_SEPARATOR = "@"
PROJECT_SEPARATOR = ":"
SCOPE_SEPARATOR = "/"
#: A repeating-warnings detail carries the event count (`SYNC x743: ...`), which
#: rises every tick; the ledger keys the row on the detail with the count removed.
EVENT_COUNT_IN_DETAIL = re.compile(r"^(\S+) x\d+: ")
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


def report_argv(namespace: str, kinds: str | None = None) -> list[str]:
    cmd = [PYTHON_EXECUTABLE, PYTHON_ISOLATED_FLAG, STDIN_SCRIPT_ARG, "--namespace", namespace, "--json"]
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


def list_clusters(project: str, timeout: float = CLUSTER_LIST_TIMEOUT_SECONDS) -> tuple[list[dict], str | None]:
    """Every cluster as {name, location, status}, and why the listing is
    incomplete when gcloud said so. Raises on a list that could not be read,
    so the caller can tell an empty project from a failed call."""
    r = run_sandbox(
        ["gcloud", "container", "clusters", "list", f"--project={project}", "--format=json"],
        timeout=timeout,
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


def served_kinds(kubeconfig: str) -> set[str] | None:
    """The namespaced, listable resources the cluster serves, as kubectl names
    them (`deployments.apps`) and by bare plural (`deployments`), so a bare
    configured kind matches and a grouped one matches only its own group
    (Istio's `gateways` does not stand in for the Gateway API's); None when
    the listing was empty, in which case the kind list is passed unfiltered."""
    # kubectl prints the full list and still exits non-zero when one aggregated
    # API (metrics-server, say) fails discovery; only an empty listing is a
    # failure, the same reading stall_report.namespaced_resources makes.
    r = run_sandbox(list(API_RESOURCES_ARGV), timeout=API_RESOURCES_TIMEOUT_SECONDS, kubeconfig=kubeconfig)
    served: set[str] = set()
    for line in r.stdout.splitlines():
        name = line.strip()
        if name:
            served.add(name)
            served.add(name.split(".", 1)[0])
    return served or None


def kinds_for(served: set[str] | None) -> tuple[str | None, set[str]]:
    """The --kind value for one cluster, the configured list minus what the
    cluster does not serve (None lets the script scan every kind), and the
    kinds the filter removed. A removed kind is never asked for, so its rows
    are held as unread in every namespace of the cluster rather than judged
    absent: a discovery listing that dropped a served group must not close a
    card under a live stall."""
    configured = kinds_argument()
    if configured is None or served is None:
        return configured, set()
    kept = [k for k in configured.split(KINDS_SEPARATOR) if k in served]
    dropped = {k for k in configured.split(KINDS_SEPARATOR) if k not in served}
    if not kept:
        return configured, set()
    return KINDS_SEPARATOR.join(kept), dropped


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


def skipped_in(stderr: str) -> set[str]:
    """The resources a scan could not list, and EVENTS_MARKER when it could not
    read the events, from the report script's stderr."""
    skipped = set(SKIPPED_KIND_PATTERN.findall(stderr or ""))
    if EVENTS_NOT_READ_PATTERN.search(stderr or ""):
        skipped.add(EVENTS_MARKER)
    return skipped


def resource_names_kind(resource: str, kind: str) -> bool:
    """Whether a kubectl resource name (`deployments`, `gateways.gateway...`)
    is the plural of an object's Kind (`Deployment`, `Gateway`)."""
    plural = resource.split(".", 1)[0].lower()
    if plural.endswith(PLURAL_IES):
        singular = plural[: -len(PLURAL_IES)] + "y"
    elif plural.endswith(PLURAL_ES_SUFFIXES):
        singular = plural[: -len(PLURAL_ES)]
    else:
        singular = plural.rstrip("s")
    return singular == kind.lower()


def scan_namespace(kubeconfig: str, namespace: str, source: str, kinds: str | None) -> tuple[list[dict], set[str]]:
    """The findings stall_report.py reports for one namespace, and the
    resources it could not read. Raises when the namespace could not be read
    at all, so the caller keeps its ledger rows."""
    r = run_sandbox(report_argv(namespace, kinds), timeout=NAMESPACE_SCAN_TIMEOUT_SECONDS, kubeconfig=kubeconfig, stdin=source)
    if r.returncode == REPORT_UNREADABLE_EXIT:
        raise RuntimeError(f"no kind could be read: {stderr_excerpt(r.stderr)}")
    if r.returncode != 0:
        raise RuntimeError(f"stall_report.py exited {r.returncode}: {stderr_excerpt(r.stderr)}")
    report = parse_json(r.stdout, "stall_report.py") or {}
    return report.get("findings") or [], skipped_in(r.stderr)


# --------------------------------------------------------------------------
# ledger
# --------------------------------------------------------------------------


def empty_state() -> dict:
    return {"version": STATE_SCHEMA_VERSION, "stalls": {}, "unreadable": {}, EPISODES_KEY: {}, "sweep_error": None, INJECT_ERROR_KEY: None, "updated_at": None, CURSOR_KEY: None}


def load_state(path: Path, project: str | None = None) -> dict:
    """The ledger, a projectless one moved under the management project first.
    Without that project a projectless ledger comes back as it is, version
    included: the tick cannot sweep, so it saves the ledger unchanged and the
    next tick moves it. Discarding it would raise a second alert for every
    namespace with an open one."""
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return empty_state()
    if not isinstance(data, dict):
        return empty_state()
    if data.get("version") == PROJECTLESS_SCHEMA_VERSION and project:
        data = with_project(data, project)
    if data.get("version") not in (STATE_SCHEMA_VERSION, PROJECTLESS_SCHEMA_VERSION):
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


def cluster_id(project: str, name: str, location: str) -> str:
    return f"{project}{PROJECT_SEPARATOR}{name}{CLUSTER_ID_SEPARATOR}{location}"


def split_cluster_id(cid: str) -> tuple[str, str, str]:
    head, _, location = cid.rpartition(CLUSTER_ID_SEPARATOR)
    project, _, name = head.rpartition(PROJECT_SEPARATOR)
    return project, name, location


def cluster_label(cid: str) -> str:
    project, name, location = split_cluster_id(cid)
    return f"`{project}/{name}` ({location})"


def with_project(data: dict, project: str) -> dict:
    """A projectless ledger with every cluster key moved under `project`. The
    unreadable map is dropped: every tick rewrites it."""
    prefix = f"{project}{PROJECT_SEPARATOR}"
    stalls = {prefix + k: {**e, "cluster": prefix + str(e.get("cluster", ""))} for k, e in (data.get("stalls") or {}).items()}
    cursor = data.get(CURSOR_KEY)
    if isinstance(cursor, dict) and cursor.get("cluster"):
        cursor = {**cursor, "cluster": prefix + cursor["cluster"]}
    return {
        **data,
        "version": STATE_SCHEMA_VERSION,
        "stalls": stalls,
        "unreadable": {},
        EPISODES_KEY: {prefix + k: v for k, v in (data.get(EPISODES_KEY) or {}).items()},
        CURSOR_KEY: cursor,
    }


def ledger_key(cid: str, finding: dict) -> str:
    return LEDGER_KEY_SEPARATOR.join(
        [cid, finding.get("namespace", ""), finding.get("object", ""), finding.get("heuristic", ""), stable_detail(finding.get("detail", ""))]
    )


def scope_key(cid: str, namespace: str | None = None) -> str:
    return cid if namespace is None else f"{cid}{SCOPE_SEPARATOR}{namespace}"


def split_scope(scope: str) -> tuple[str, str | None]:
    cid, sep, namespace = scope.partition(SCOPE_SEPARATOR)
    return cid, (namespace if sep else None)


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

    def __init__(self, projects: Iterable[str] = ()) -> None:
        self.projects = set(projects)
        self.rows: dict[str, dict] = {}
        #: `cluster/namespace` scopes whose scan ran this tick, and for the
        #: ones that skipped a kind or the events, what they skipped.
        self.read_scopes: set[str] = set()
        self.partial_scopes: dict[str, set[str]] = {}
        #: Every managed cluster the projects listed, whatever its status, and
        #: the projects whose listing failed or gcloud called incomplete.
        self.listed_clusters: set[str] = set()
        self.unlisted_projects: set[str] = set()
        #: Listed clusters with no Cluster Agent profile: not read, not listed
        #: here, so their rows clear the way a deleted cluster's do.
        self.unmanaged: set[str] = set()
        #: Clusters whose namespace listing succeeded, and those namespaces.
        self.read_clusters: set[str] = set()
        self.listed_namespaces: dict[str, set[str]] = {}
        self.unreadable: dict[str, str] = {}
        self.budget_exhausted = False
        #: Where the budget stopped the sweep, for the next tick to start from.
        self.cursor: dict | None = None

    @property
    def clusters(self) -> int:
        return len(self.read_clusters)

    @property
    def namespaces(self) -> int:
        return len(self.read_scopes)

    def verdict(self, entry: dict) -> str:
        """What this tick can say about a ledger row: its namespace or cluster
        is GONE from the listing, the namespace was scanned and the row was
        ABSENT, or the scope (or the row's own kind) was not read and the row
        is UNKNOWN."""
        cid, namespace = entry.get("cluster", ""), entry.get("namespace", "")
        if cid not in self.listed_clusters:
            project, name, location = split_cluster_id(cid)
            if project in self.unlisted_projects:
                return UNKNOWN
            if project not in self.projects and cluster_agent_for(project, name, location) is not None:
                # The profile is there but its identity did not name the
                # project, so the project was not listed this tick.
                return UNKNOWN
            return GONE
        namespaces = self.listed_namespaces.get(cid)
        if namespaces is not None and namespace not in namespaces:
            return GONE
        scope = scope_key(cid, namespace)
        if scope not in self.read_scopes:
            return UNKNOWN
        skipped = self.partial_scopes.get(scope, set())
        if EVENTS_MARKER in skipped and entry.get("heuristic") == REPEATING_WARNINGS_HEURISTIC:
            return UNKNOWN
        kind = str(entry.get("object", "")).split("/", 1)[0]
        if any(resource_names_kind(r, kind) for r in skipped if r != EVENTS_MARKER):
            return UNKNOWN
        return ABSENT

    def left_roster(self, cid: str) -> bool:
        """Whether the cluster's rows went because it lost its Cluster Agent
        profile, rather than because the cluster or namespace was deleted."""
        return cid in self.unmanaged or split_cluster_id(cid)[0] not in self.projects

    def out_of_budget(self, started: float, cid: str, namespace: str | None = None) -> bool:
        if self.budget_exhausted:
            return True
        if time.monotonic() - started <= TICK_BUDGET_SECONDS:
            return False
        self.budget_exhausted = True
        self.cursor = {"cluster": cid, "namespace": namespace}
        self.unreadable[BUDGET_SCOPE] = (
            f"sweep budget of {TICK_BUDGET_SECONDS}s exhausted after {self.clusters} clusters and "
            f"{self.namespaces} namespaces; the rest were not read this tick"
        )
        return True


#: What one scope may fail with and the sweep carry on. SandboxUnavailable is a
#: RuntimeError, so the handlers re-raise it first: with the sandbox gone
#: nothing else this tick can succeed, and the tick reports one sweep failure.
READ_FAILURES = (RuntimeError, subprocess.TimeoutExpired, OSError)


def rotate_to_cursor(sweepable: list, cursor: dict | None) -> list:
    """Start from the cluster an exhausted sweep stopped at, wrapping around,
    so a fleet the budget cannot cover is still read in full over ticks."""
    if not cursor:
        return sweepable
    for i, (cid, _) in enumerate(sweepable):
        if cid == cursor.get("cluster"):
            return sweepable[i:] + sweepable[:i]
    return sweepable


def profile_identity(home: Path) -> dict[str, str] | None:
    """The profile's cluster_identity, or None when it is absent or cannot be
    read. The file is in the model-writable agent home, so a malformed one
    reads as absent rather than failing every project's sweep."""
    from cluster_agent_profile import read_cluster_identity  # lazy, as in cluster_agent_for

    try:
        return read_cluster_identity(home)
    except Exception:  # noqa: BLE001 - any unreadable file is an absent identity
        return None


def roster_projects() -> tuple[set[str], dict[str, str]]:
    """The projects the Cluster Agent profiles' identities name, and a ledger
    entry for each profile whose identity names none."""
    from cluster_agent_profile import RESERVED_PROFILES  # lazy, as in cluster_agent_for

    base = Path(gitops_workspace.agent_home()) / PROFILES_DIR
    if not base.is_dir():
        return set(), {}
    projects, unread = set(), {}
    for home in base.iterdir():
        if home.name in RESERVED_PROFILES or not home.is_dir():
            continue
        identity = profile_identity(home)
        if identity:
            projects.add(identity["project"])
        else:
            unread[f"{PROFILE_SCOPE} {home.name}"] = NO_IDENTITY_REASON
    return projects, unread


def list_projects(projects: list[str], first: str, started: float) -> dict[str, tuple[list[dict], str | None] | Exception]:
    """Every project's listing, or what it failed with; a listing still
    running at the deadline fails with TimeoutExpired. `first` starts the
    budget, so it gets gcloud's whole timeout."""
    deadline = started + LIST_BUDGET_SECONDS

    def listing(project: str, timeout: float) -> tuple[list[dict], str | None] | Exception:
        try:
            return list_clusters(project, timeout=timeout)
        except sandbox_exec.SandboxUnavailable:
            raise
        except READ_FAILURES as exc:
            return exc

    def within_budget(project: str) -> tuple[list[dict], str | None] | Exception:
        return listing(project, max(1.0, min(CLUSTER_LIST_TIMEOUT_SECONDS, deadline - time.monotonic())))

    results = {first: listing(first, CLUSTER_LIST_TIMEOUT_SECONDS)}
    rest = [p for p in projects if p != first]
    if not rest:
        return results
    pool = ThreadPoolExecutor(max_workers=min(LIST_WORKERS, len(rest)))
    futures = {project: pool.submit(within_budget, project) for project in rest}
    done, _ = wait(futures.values(), timeout=max(0.0, deadline + LIST_GRACE_SECONDS - time.monotonic()))
    pool.shutdown(wait=False, cancel_futures=True)
    for project, future in futures.items():
        # result() re-raises a lost sandbox from the worker.
        results[project] = future.result() if future in done else subprocess.TimeoutExpired("gcloud container clusters list", LIST_BUDGET_SECONDS)
    return results


def sweep_fleet(management_project: str, cursor: dict | None = None) -> Sweep:
    """Sweep the management project and every project on the roster. One
    project's listing failing holds that project's rows; every listing failing
    fails the sweep."""
    projects, unread_profiles = roster_projects()
    sweep = Sweep({management_project} | projects)
    sweep.unreadable.update(unread_profiles)
    started = time.monotonic()
    source = report_source()
    failures: dict[str, Exception] = {}
    listings = list_projects(sorted(sweep.projects), management_project, started)
    # Every listed cluster is registered before any is read: a cluster the
    # budget never reaches is unread, not gone.
    sweepable = []
    for project in sorted(sweep.projects):
        listing = listings[project]
        if isinstance(listing, Exception):
            failures[project] = listing
            sweep.unlisted_projects.add(project)
            sweep.unreadable[f"{LISTING_SCOPE} {project}"] = failure_text(listing)
            continue
        clusters, incomplete = listing
        if incomplete:
            sweep.unlisted_projects.add(project)
            sweep.unreadable[f"{LISTING_SCOPE} {project}"] = f"incomplete: {incomplete}"
        for cluster in clusters:
            cid = cluster_id(project, cluster["name"], cluster["location"])
            if cluster_agent_for(project, cluster["name"], cluster["location"]) is None:
                sweep.unmanaged.add(cid)
                sweep.unreadable[scope_key(cid)] = NO_PROFILE_REASON
                continue
            sweep.listed_clusters.add(cid)
            if cluster["status"] in SWEEPABLE_CLUSTER_STATUSES:
                sweepable.append((cid, cluster))
            else:
                sweep.unreadable[scope_key(cid)] = f"status={cluster['status'] or 'unknown'}"
    if len(failures) == len(sweep.projects):
        raise failures[management_project]
    for cid, cluster in rotate_to_cursor(sweepable, cursor):
        project, name, location = split_cluster_id(cid)
        if sweep.out_of_budget(started, cid):
            break
        try:
            kubeconfig = fetch_credentials(project, name, location)
            namespaces = list_namespaces(kubeconfig)
            kinds, dropped = kinds_for(served_kinds(kubeconfig))
        except sandbox_exec.SandboxUnavailable:
            raise
        except READ_FAILURES as exc:
            sweep.unreadable[scope_key(cid)] = failure_text(exc)
            continue
        sweep.read_clusters.add(cid)
        sweep.listed_namespaces[cid] = set(namespaces)
        if cursor and cursor.get("cluster") == cid and cursor.get("namespace") in namespaces:
            # Resume inside the cluster the last tick stopped in; the namespaces
            # before the cursor were read then and are unknown now, not gone.
            namespaces = namespaces[namespaces.index(cursor["namespace"]):]
        for namespace in namespaces:
            if sweep.out_of_budget(started, cid, namespace):
                break
            try:
                findings, unread = scan_namespace(kubeconfig, namespace, source, kinds)
            except sandbox_exec.SandboxUnavailable:
                raise
            except subprocess.TimeoutExpired as exc:
                sweep.unreadable[scope_key(cid)] = (
                    f"namespace {namespace} {failure_text(exc)}; the cluster's remaining namespaces were skipped this tick"
                )
                break
            except READ_FAILURES as exc:
                sweep.unreadable[scope_key(cid, namespace)] = failure_text(exc)
                continue
            sweep.read_scopes.add(scope_key(cid, namespace))
            # A kind the cluster does not serve holds its rows without making
            # the namespace unreadable: most clusters lack some optional CRD.
            if unread | dropped:
                sweep.partial_scopes[scope_key(cid, namespace)] = unread | dropped
            if unread:
                sweep.unreadable[scope_key(cid, namespace)] = f"partial: {', '.join(sorted(unread))} not read"
            for f in findings:
                sweep.rows[ledger_key(cid, f)] = {
                    "cluster": cid,
                    "namespace": f.get("namespace", namespace),
                    "object": f.get("object", ""),
                    "heuristic": f.get("heuristic", ""),
                    "detail": f.get("detail", ""),
                    "stalled_for": f.get("stalled_for", ""),
                }
    return sweep


def object_key(row: dict) -> tuple[str, str, str]:
    return (row.get("cluster", ""), row.get("namespace", ""), row.get("object", ""))


def diff_and_update(state: dict, sweep: Sweep, now: str) -> tuple[dict, dict]:
    """Fold the sweep into the ledger. Returns the objects that started stalling
    this tick and the objects whose last row cleared, each grouped by
    `cluster/namespace` scope."""
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
        verdict = sweep.verdict(entry)
        if verdict == UNKNOWN:
            continue
        missed = int(entry.get("missed") or 0) + 1
        if verdict == GONE or missed >= clear_after(entry.get("heuristic", "")):
            del current[key]
        else:
            current[key] = {**entry, "missed": missed}
    state["stalls"] = current
    state["unreadable"] = dict(sweep.unreadable)
    state["updated_at"] = now
    known_before = {object_key(e) for e in previous.values()}
    known_after = {object_key(e) for e in current.values()}
    new_by_scope: dict[str, list[dict]] = {}
    for row in sweep.rows.values():
        if object_key(row) not in known_before:
            new_by_scope.setdefault(scope_key(row["cluster"], row["namespace"]), []).append(row)
    cleared_by_scope: dict[str, list[dict]] = {}
    for entry in previous.values():
        if object_key(entry) not in known_after:
            cleared_by_scope.setdefault(scope_key(entry["cluster"], entry["namespace"]), []).append(entry)
    return new_by_scope, cleared_by_scope


# --------------------------------------------------------------------------
# the alert hand-off
# --------------------------------------------------------------------------


def session_kv(path: str, body: dict | None = None, method: str = "") -> dict:
    """One call to the Session KV server on the pod's loopback, authenticated
    the way findings_nudge.py authenticates; SESSION_KV_API_KEY survives into a
    no_agent child."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {CONTENT_TYPE_HEADER: JSON_CONTENT_TYPE} if data is not None else {}
    token = (os.environ.get(SESSION_KV_AUTH_ENV) or "").strip()
    if token:
        headers[AUTH_HEADER] = f"{BEARER_PREFIX}{token}"
    request = urllib.request.Request(
        f"{SESSION_KV_URL}{path}", data=data, headers=headers, method=method or ("POST" if data is not None else "GET")
    )
    # Callers handle HTTPError, OSError and ValueError, so every other way the
    # answer can fail to arrive whole is a ValueError here.
    try:
        with urllib.request.urlopen(request, timeout=SESSION_KV_TIMEOUT_SECONDS) as response:
            answer = json.loads(response.read().decode("utf-8") or "{}")
    except http.client.HTTPException as exc:  # a body cut short, a status line that is not HTTP
        if isinstance(exc, OSError):
            raise  # RemoteDisconnected: the server went away, which is unreachable
        raise ValueError(f"the Session KV server's answer was malformed: {exc!r}") from exc
    if not isinstance(answer, dict):
        raise ValueError(f"the Session KV server answered {type(answer).__name__}, not a JSON object")
    return answer


def stall_payload(project: str, cluster: str, location: str, namespace: str, assignee: str, rows: list[dict], first_seen: str) -> dict:
    """The record a new episode hands the Session KV server. A row's detail
    stays out: condition reasons, spec paths, referent names and event messages
    are text a tenant writes, and the Cluster Agent reads them again when it
    runs the skill."""
    objects = [
        {"object": r["object"][:MAX_NAME_CHARS], "heuristic": r["heuristic"], "stalled_for": r.get("stalled_for") or ""}
        for r in sorted(rows, key=lambda r: (r["object"], r["heuristic"]))
    ]
    return {
        "kind": INJECT_KIND,
        "cluster": cluster,
        "project": project,
        "location": location,
        "namespace": namespace,
        "assignee": assignee,
        "first_seen": first_seen,
        "objects": objects[:MAX_ROWS_IN_RECORD],
    }


class AlertRefused(Exception):
    """The Session KV server did not take the alert; the message says why."""


class RecordRefused(AlertRefused):
    """The server refused this one record (HTTP 400). Other namespaces' alerts
    are still worth sending this tick."""


# One reader per route: each returns the field the watch uses, typed, or raises
# the ValueError its caller reports as an answer that could not be read. No
# caller reads an answer's fields itself.


def healthz_kinds() -> list[str]:
    kinds = session_kv(HEALTHZ_PATH).get(INJECT_KINDS_KEY)
    if kinds is None:
        return []
    if not isinstance(kinds, list) or not all(isinstance(k, str) for k in kinds):
        raise ValueError(f"the Session KV server's {INJECT_KINDS_KEY} is not a list of names: {kinds!r}")
    return kinds


def new_session() -> str:
    session_id = session_kv(SESSIONS_PATH, method="POST").get(SESSION_ID_KEY)
    if not isinstance(session_id, str) or not session_id.strip():
        raise ValueError(f"the Session KV server returned no session id: {session_id!r}")
    return session_id


def inject_status(session_id: str, payload: dict) -> str:
    status = session_kv(
        f"{SESSIONS_PATH}/{urllib.parse.quote(session_id, safe='')}{INJECT_SUFFIX}",
        {MESSAGE_KEY: json.dumps(payload)},
    ).get(STATUS_KEY)
    if not isinstance(status, str):
        raise ValueError(f"the Session KV server's inject answer has no status: {status!r}")
    return status


def open_session() -> str:
    """A session for one alert. A server that does not advertise the kind is
    refused before any session is opened: its dispatch would take the record
    for a Kubernetes event and post a Pod alert naming nothing."""
    try:
        if INJECT_KIND not in healthz_kinds():
            raise AlertRefused(f"the Session KV server does not advertise the {INJECT_KIND} inject kind")
        return new_session()
    except urllib.error.HTTPError as exc:
        raise AlertRefused(f"the Session KV server answered with an error: {exc}") from exc
    except ValueError as exc:  # JSONDecodeError and UnicodeDecodeError are ValueErrors
        raise AlertRefused(f"the Session KV server's answer could not be read: {exc}") from exc
    except OSError as exc:  # URLError is an OSError
        raise AlertRefused(f"the Session KV server could not be reached: {exc}") from exc


def inject(session_id: str, payload: dict) -> None:
    """Hand the record to the session; raises AlertRefused unless it was taken."""
    try:
        status = inject_status(session_id, payload)
    except urllib.error.HTTPError as exc:
        if exc.code == HTTP_BAD_REQUEST:
            raise RecordRefused(f"the Session KV server refused the record: {exc}") from exc
        raise AlertRefused(f"the inject failed: {exc}") from exc
    except (OSError, ValueError) as exc:
        raise AlertRefused(f"the inject failed: {exc}") from exc
    if status != INJECTED_STATUS:
        # No session id in the text: the ledger compares it with the last
        # refusal to say each refusal once, and every attempt has a new session.
        raise AlertRefused(f"the Session KV server answered {status!r} to the inject")


def kanban(command: str) -> str:
    """One board command through the same in-process API github_scan_gate.py
    uses, so a cron child needs no `hermes` on PATH and no session."""
    from hermes_cli.kanban import run_slash  # lazy: the board API is the gateway venv's, not a test's

    return str(run_slash(command))


def card_status(task_id: str) -> str | None:
    """The card's status, or None when the board could not say; a caller
    treats None as unknown, never as open or closed."""
    try:
        out = kanban(f"show --json {shlex.quote(task_id)}")
        start, end = out.find("{"), out.rfind("}")
        if start == -1 or end <= start:
            raise ValueError("no JSON object in the board response")
        status = (json.loads(out[start : end + 1]).get("task") or {}).get("status")
        return str(status) if status else None
    except Exception as exc:  # noqa: BLE001 - reported, and the caller retries next tick
        sys.stderr.write(f"stall_watch: could not read card {task_id}: {exc}\n")
        return None


def board_path() -> Path:
    """The board the Planning Agent files on: Hermes' own resolution when the
    API is importable, else the agent home's kanban.db."""
    try:
        from hermes_cli.kanban_db import kanban_db_path  # lazy: the gateway venv's, not a test's

        return Path(str(kanban_db_path()))
    except Exception:  # noqa: BLE001 - outside the gateway venv the default board is the only one
        return Path(gitops_workspace.agent_home()) / BOARD_DB_NAME


def card_exists(task_id: str, db_path: Path | None = None) -> bool | None:
    """Whether the board has the card at all, read straight from its tasks
    table; None when the board could not be opened."""
    try:
        conn = sqlite3.connect(f"file:{db_path or board_path()}?mode=ro", uri=True, timeout=BOARD_BUSY_TIMEOUT_SECONDS)
        try:
            return conn.execute("SELECT 1 FROM tasks WHERE id = ?", (task_id,)).fetchone() is not None
        finally:
            conn.close()
    except sqlite3.Error as exc:
        sys.stderr.write(f"stall_watch: could not open the board to look for {task_id}: {exc}\n")
        return None


class BoardUnreadable(Exception):
    """The board could not be read, which says nothing about whether a card is there."""


def board_records_sessions(db_path: Path | None = None) -> bool | None:
    """Whether the board's tasks table has the `session_id` column the watch
    finds an alert's card by; None when the board could not be opened or has no
    tasks table yet, which the first card filed creates. Without
    the column every alert's card would be unfindable, so its episode would
    never close and the namespace would never be alerted for again."""
    try:
        conn = sqlite3.connect(f"file:{db_path or board_path()}?mode=ro", uri=True, timeout=BOARD_BUSY_TIMEOUT_SECONDS)
        try:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(tasks)")}
        finally:
            conn.close()
    except sqlite3.Error as exc:
        sys.stderr.write(f"stall_watch: could not open the board to check its schema: {exc}\n")
        return None
    return SESSION_COLUMN in columns if columns else None


def card_for_session(session_id: str, assignee: str = "", db_path: Path | None = None) -> str | None:
    """The card the alert's session filed, or None while there is none; raises
    BoardUnreadable when the board cannot say. Hermes stamps the filing session on `tasks.session_id`;
    the Planning Agent's kanban_create runs in the session the Session KV
    server opened for the alert, so that is the link. A card assigned to the
    Cluster Agent the record named is preferred over any other the turn filed.
    A board with no file or no tasks table yet has no card, the reading
    board_records_sessions gives it too, so the day-later retry and the
    clearing step still apply."""
    path = db_path or board_path()
    if not Path(path).exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=BOARD_BUSY_TIMEOUT_SECONDS)
        try:
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'tasks'").fetchone() is None:
                return None
            row = conn.execute(
                "SELECT id FROM tasks WHERE session_id = ? ORDER BY assignee = ? DESC, created_at, id LIMIT 1",
                (session_id, assignee),
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        sys.stderr.write(f"stall_watch: could not look up the card for session {session_id}: {exc}\n")
        raise BoardUnreadable(str(exc)) from exc
    return str(row[0]) if row else None


def cluster_agent_for(project: str, cluster: str, location: str) -> str | None:
    """The cluster's Cluster Agent profile when the reconciler has scaffolded
    one, else None. Named the way cluster_agent_profile.py names profiles."""
    from cluster_agent_profile import profile_name  # lazy: pulls the scaffold module's imports

    name = profile_name(project, cluster, location)
    home = Path(gitops_workspace.agent_home()) / PROFILES_DIR / name
    if not home.is_dir():
        return None
    identity = profile_identity(home)
    if identity and (identity["project"], identity["cluster"], identity["location"]) != (project, cluster, location):
        # profile_name collapses separators, so `acme-prod`/`web` and
        # `acme`/`prod-web` share a name; the identity says whose it is.
        return None
    return name


def scope_label_text(scope: str) -> str:
    cid, namespace = split_scope(scope)
    return f"{cluster_label(cid)} / `{namespace}`"


def object_names(rows: list[dict]) -> list[str]:
    return sorted({r["object"] for r in rows})


def names_text(names: list[str]) -> str:
    """The first MAX_OBJECTS_IN_LINE names and a count of the rest, so a
    namespace with hundreds of stalled objects is one bounded line."""
    shown = names[:MAX_OBJECTS_IN_LINE]
    rest = len(names) - len(shown)
    return ", ".join(shown) + (f" and {rest} more" if rest else "")


def comment_card(task_id: str, text: str) -> bool:
    try:
        kanban(f"comment {shlex.quote(task_id)} {shlex.quote(text)}")
        return True
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"stall_watch: could not comment on {task_id}: {exc}\n")
        return False


def complete_card(task_id: str, result: str) -> bool:
    try:
        kanban(f"complete --result {shlex.quote(result)} {shlex.quote(task_id)}")
        return True
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"stall_watch: could not complete {task_id}: {exc}\n")
        return False


def scope_rows(state: dict, scope: str) -> list[dict]:
    return [e for e in state["stalls"].values() if scope_key(e["cluster"], e["namespace"]) == scope]


def would_raise(state: dict, sweep: Sweep, scope: str) -> bool:
    """Whether a scope is a candidate for an alert this tick: read this tick,
    with a row not on its way out."""
    return scope in sweep.read_scopes and any(not e.get("missed") for e in scope_rows(state, scope))


def scope_first_seen(state: dict, scope: str) -> str:
    """When the scope's oldest ledgered row was first seen: the queue order."""
    return min((str(e.get("first_seen") or "") for e in scope_rows(state, scope)), default="")


def end_episode(state: dict, scope: str) -> dict:
    return state[EPISODES_KEY].pop(scope)


def episode_card(episode: dict) -> str | None:
    """The episode's card: the one it records, or the one its alert's session
    filed, adopted the first time the board shows it. An episode the watch
    opened before the inject path carries `card` and no `session`. Raises
    BoardUnreadable, which a caller treats as a reason to wait a tick."""
    if episode.get("card"):
        return episode["card"]
    card = card_for_session(episode[SESSION_KEY], episode.get("assignee") or "") if episode.get(SESSION_KEY) else None
    if card:
        episode["card"] = card
    return card


def episode_gone(state: dict, scope: str, task_id: str) -> bool:
    """Called when the board could not describe the card. A card the board's
    tasks table no longer has ends its episode at once; otherwise the unknown
    is counted, whatever the tasks table says, and the episode ends after
    MAX_UNKNOWN_CARD_TICKS such ticks running. The counter resets only when a
    status is read."""
    episode = state[EPISODES_KEY][scope]
    if card_exists(task_id) is False:
        sys.stderr.write(f"stall_watch: card {task_id} for {scope} is gone from the board; its episode ends\n")
        end_episode(state, scope)
        return True
    episode["unknown"] = int(episode.get("unknown") or 0) + 1
    if episode["unknown"] >= MAX_UNKNOWN_CARD_TICKS:
        sys.stderr.write(f"stall_watch: the board could not describe card {task_id} for {scope} on {episode['unknown']} ticks running; its episode ends\n")
        end_episode(state, scope)
        return True
    return False


def comment_pending(episode: dict, namespace: str, card: str) -> None:
    """Tell the open card about objects that joined since it was filed; what
    the board refused stays pending and is tried again next tick."""
    pending = sorted(set(episode.get("pending", [])))
    if not pending:
        return
    if comment_card(card, f"The watch now also sees these objects stalled in `{namespace}`: {names_text(pending)}"):
        episode["objects"] = sorted(set(episode.get("objects", [])) | set(pending))
        episode["pending"] = []


def card_wait_over(episode: dict, now: str) -> bool:
    """Whether a card-less episode has waited as long as its card is given to
    appear."""
    if not episode.get(SESSION_KEY):
        return True  # nothing can file a card for it
    try:
        waited = (datetime.fromisoformat(now) - datetime.fromisoformat(str(episode.get("opened_at")))).total_seconds()
    except (ValueError, TypeError):  # TypeError: a hand-edited time with no timezone
        return True
    return waited >= UNFILED_ALERT_RETRY_SECONDS


def adopt_cards(state: dict, now: str) -> set[str]:
    """Look up the card each open alert's session filed, and return the scopes
    whose session has filed none UNFILED_ALERT_RETRY_SECONDS after the alert.
    Their episodes stay until a new alert replaces them or the clearing step
    ends them, so a skipped re-alert still leaves the clear to be said."""
    episodes = state.setdefault(EPISODES_KEY, {})
    expired: set[str] = set()
    for scope in sorted(episodes):
        episode = episodes[scope]
        if episode.get("card") or not episode.get(SESSION_KEY):
            continue
        try:
            if episode_card(episode):
                continue
        except BoardUnreadable:
            continue
        if card_wait_over(episode, now):
            expired.add(scope)
    return expired


def restore_episode(episodes: dict, scope: str, previous: dict | None) -> None:
    if previous is None:
        episodes.pop(scope, None)
    else:
        episodes[scope] = previous


def episode_lines(state: dict, sweep: Sweep, new_by_scope: dict, cleared_by_scope: dict, now: str, *, dry_run: bool = False, persist) -> list[str]:
    """Raise alerts for the tick's new episodes, and comment on and close the
    cards earlier alerts produced; return the chat lines. A new episode gets no
    line of its own, because the alert is its notice; a closed card gets one,
    and so do namespaces held for a later tick and a server that refused an
    alert. A dry run touches no board and raises nothing, and says what it
    would have done.

    `persist` saves the ledger. An episode is saved before its inject is sent,
    so a tick whose ledger cannot be written raises nothing rather than raising
    the same alerts again on every tick that follows."""
    episodes = state.setdefault(EPISODES_KEY, {})
    lines: list[str] = []
    expired = set() if dry_run else adopt_cards(state, now)
    # Every scope read this tick that has rows and no episode, or an episode
    # whose alert expired with no card, is a candidate, whether its objects
    # appeared now or it has waited: past the cap, after a refused alert, or
    # with no profile. Oldest first sighting first.
    candidates = dict(new_by_scope)
    for entry in state["stalls"].values():
        scope = scope_key(entry["cluster"], entry["namespace"])
        if scope not in candidates and (scope not in episodes or scope in expired) and would_raise(state, sweep, scope):
            candidates[scope] = []
    # Objects held for a card go through the same card check as new ones, so a
    # card finished before they reach it gets a new alert, not a comment.
    # A cleared scope is left to the clearing step.
    for scope, episode in episodes.items():
        if not dry_run and episode.get("pending") and scope not in candidates and scope_rows(state, scope):
            candidates[scope] = []
    raised = held = 0
    refusal: str | None = None
    for scope in sorted(candidates, key=lambda sc: (scope_first_seen(state, sc), sc)):
        new_rows = candidates[scope]
        cid, namespace = split_scope(scope)
        project, name, location = split_cluster_id(cid)
        # An expired episode is replaced only by an alert that is sent; an
        # episode this iteration ends below is not put back on a refusal.
        previous = episodes.get(scope) if scope in expired else None
        episode = None if scope in expired else episodes.get(scope)
        # An alert carries every object the scope holds, not only the ones that
        # appeared this tick.
        seen = {(r["object"], r["heuristic"], r["detail"]) for r in new_rows}
        rows = new_rows + [e for e in scope_rows(state, scope) if (e["object"], e["heuristic"], e["detail"]) not in seen]
        if dry_run:
            if episode:
                what, shown = f"would comment on card `{episode.get('card') or 'not yet filed'}` for", new_rows
            elif raised < MAX_ALERTS_PER_TICK:
                what, shown = "would raise an alert for", rows
                raised += 1
            else:
                held += 1
                continue
            lines.append(f"{DRY_RUN_PREFIX} {what} {scope_label_text(scope)}: {names_text(object_names(shown))}")
            continue
        if episode:
            try:
                card = episode_card(episode)
            except BoardUnreadable:
                card = None
            if card is None:
                # The board is unreadable, or the Planning Agent has not filed
                # the alert's card yet. Either way the namespace already has its
                # alert; the objects wait for a card to comment on.
                episode["pending"] = sorted(set(episode.get("pending", [])) | set(object_names(new_rows)))
                continue
            status = card_status(card)
            if status is None and not episode_gone(state, scope, card):
                # The rows stay ledgered; the comment waits for a board that answers.
                episode["pending"] = sorted(set(episode.get("pending", [])) | set(object_names(new_rows)))
                continue
            if status is not None and status not in TERMINAL_CARD_STATUSES:
                episode["unknown"] = 0
                episode["pending"] = sorted(set(episode.get("pending", [])) | set(object_names(new_rows)))
                comment_pending(episode, namespace, card)
                continue
            if status is not None:
                # The finished card's episode is replaced only by an alert that
                # is sent. Until then the new objects wait as pending, which
                # keeps the scope a candidate, and a clear still finds it.
                episode["pending"] = sorted(set(episode.get("pending", [])) | set(object_names(new_rows)))
                previous = episode
        if not would_raise(state, sweep, scope):
            continue
        if refusal is not None:
            # The server refused this tick's first alert; the rest wait for the
            # next tick rather than each spending a timeout on the same answer.
            continue
        if raised >= MAX_ALERTS_PER_TICK:
            held += 1
            continue
        assignee = cluster_agent_for(project, name, location)
        if assignee is None:
            # The profile went between the sweep and the alert; the next sweep
            # leaves the cluster out and its rows clear.
            sys.stderr.write(f"stall_watch: {cid} has no Cluster Agent profile; no alert for {scope}\n")
            continue
        payload = stall_payload(project, name, location, namespace, assignee, rows, scope_first_seen(state, scope) or now)
        try:
            if board_records_sessions() is False:
                raise AlertRefused("the kanban board records no tasks.session_id, so the card an alert produces could not be found")
            session = open_session()
            # `card` is present as None because the card-filing watch read it
            # unguarded: a rollback then ends the episode instead of crashing.
            episodes[scope] = {SESSION_KEY: session, "card": None, "assignee": assignee, "opened_at": now, "objects": object_names(rows)}
            # An OSError stops the tick; the scheduler reports the failure.
            persist()
            inject(session, payload)
        except RecordRefused as exc:
            # This record's fault, not the server's: the next namespace in line
            # still gets its alert.
            restore_episode(episodes, scope, previous)
            sys.stderr.write(f"stall_watch: no alert for {scope}: {exc}\n")
            continue
        except AlertRefused as exc:
            restore_episode(episodes, scope, previous)
            refusal = str(exc)
            sys.stderr.write(f"stall_watch: no alert for {scope}: {refusal}\n")
            continue
        raised += 1
    if held:
        text = f"{NOTICED_PREFIX} in {held} more namespace{'s' if held > 1 else ''}; alerts follow on later ticks, {MAX_ALERTS_PER_TICK} a tick"
        lines.append(f"{DRY_RUN_PREFIX} {text}" if dry_run else text)
    if refusal is not None:
        if state.get(INJECT_ERROR_KEY) != refusal:
            lines.append(f"{INJECT_FAILED_PREFIX} {refusal}")
        state[INJECT_ERROR_KEY] = refusal
    elif raised and not dry_run:
        # Only a sent alert shows the server is taking them again; a tick whose
        # one attempt was a refused record proves nothing either way.
        if state.get(INJECT_ERROR_KEY):
            lines.append(INJECT_RECOVERED_LINE)
        state[INJECT_ERROR_KEY] = None
    open_scopes = {scope_key(e["cluster"], e["namespace"]) for e in state["stalls"].values()}
    for scope in sorted(set(episodes) - open_scopes):
        episode = episodes[scope]
        cleared = names_text(object_names(cleared_by_scope.get(scope, [])) or episode.get("objects", []))
        if sweep.left_roster(split_scope(scope)[0]):
            cleared = "the cluster left the Cluster Agent roster"
            note = f"The cluster left the Cluster Agent roster; the watch no longer reads it, as of {now}."
        else:
            note = f"The watch no longer sees a stall in `{namespace_of(scope)}`: {cleared} cleared at {now}."
        if dry_run:
            lines.append(f"{DRY_RUN_PREFIX} would close card `{episode.get('card') or 'not yet filed'}` for {scope_label_text(scope)}")
            continue
        try:
            card = episode_card(episode)
        except BoardUnreadable:
            # Whether a card is open is unknown; ask again next tick.
            continue
        if card is None:
            # No card came of the alert, so there is nothing to close. A card
            # filed later still runs the scan and reports the recovery itself.
            end_episode(state, scope)
            lines.append(f"{CLEARED_PREFIX} in {scope_label_text(scope)}: {cleared}")
            continue
        status = card_status(card)
        if status is None:
            episode_gone(state, scope, card)
            continue
        episode["unknown"] = 0
        if status not in TERMINAL_CARD_STATUSES:
            if not episode.get("cleared_at"):
                if not comment_card(card, note):
                    continue
                episode["cleared_at"] = now
            if status == RUNNING_CARD_STATUS:
                # The worker is on it; it completes its own card. Closed next tick.
                continue
            if not complete_card(card, f"{note} Closed by the stall watch."):
                continue
        end_episode(state, scope)
        lines.append(f"{CLEARED_PREFIX} in {scope_label_text(scope)}: {cleared}; card `{card}` closed")
    return lines


def namespace_of(scope: str) -> str:
    return split_scope(scope)[1] or ""


def tick(state_path: Path, *, dry_run: bool) -> list[str]:
    now = now_iso()
    lines: list[str] = []
    try:
        project = project_id()
    except Exception as exc:  # noqa: BLE001 - reported below as the sweep failure
        project, lookup_error = None, exc
    else:
        lookup_error = None if project else RuntimeError(f"no GCP project: set {PROJECT_ENVS[0]} or configure gcloud in the sandbox")
    state = load_state(state_path, project)
    try:
        if lookup_error:
            raise lookup_error
        sweep = sweep_fleet(project, state.get(CURSOR_KEY))
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
        state[CURSOR_KEY] = sweep.cursor
        new_by_scope, cleared_by_scope = diff_and_update(state, sweep, now)
        lines += episode_lines(state, sweep, new_by_scope, cleared_by_scope, now, dry_run=dry_run, persist=lambda: save_state(state_path, state))
    if not dry_run:
        save_state(state_path, state)
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="sweep and print; write no state and raise no alert")
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
