"""kcc_health.py — the Config Connector objects that are not applying, as the
collectors record them for `finish`.

A `manifest` fix that edits a Config Connector declaration does nothing while
Config Connector does not apply that object. On 2026-09-05 one `PubSubTopic`
failed with `UpdateFailed` on an IAM role and put fifteen `ContainerCluster`s
into `DependencyNotReady`; every manifest fix against them would have merged
into a controller that applied nothing. The collectors read GCP, where those
clusters were healthy, so no stream saw it.

`finish` must not read a cluster (docs/designs/fleet-audit-collector-manifest.md):
the collectors do the reading. This module is the one read they share
(`read_objects`). On each cluster a collector swept, it reads the API
server's discovery once (`kubectl api-resources`, which needs no RBAC grant) to
find the Config Connector kinds (a group that ends in `.cnrm.cloud.google.com`,
in the `gcp` category), then makes one list request per kind (`kubectl get
--raw /apis/<group>/<version>/<plural>`), with no more requests in flight in
the process than the credential broker admits (`proxy_slots`), and puts
the result on that cluster's manifest entry. `kubectl get gcp -A` does the same work one kind after another, and
through the credential proxy its 230 requests took longer than the read's
time limit on a cluster with the add-on.

- `kcc_unready`: the objects whose `Ready` condition is `False` for a reason
  other than a transient one (`TRANSIENT_REASONS`: Config Connector is still
  applying or deleting the object), each as
  `{kind, namespace, name, why}`, plus `resource` (the object's
  `spec.resourceID`), `cluster` (a `ContainerNodePool`'s `clusterRef`),
  `location` (the object's `spec.location`) and `project` (the GCP project
  the object manages: its `cnrm.cloud.google.com/project-id` annotation,
  else the same annotation on its namespace, else the namespace's name, as
  Config Connector itself decides it) where they are set. When an unready
  object has no project annotation, one `kubectl get namespaces` gives the
  namespaces; when that read fails, the project stays unknown. An empty
  list means the cluster runs Config Connector and every object it holds
  is ready, not yet reached, or still updating or deleting.
- `kcc_unread`: why the read did not give an answer (a timeout, a forbidden
  read, output that is not a list). This is "unknown", never "healthy".
- Neither key on a `collected` entry: the cluster serves none of the kinds
  this collector reads: for compliance, no Config Connector kind in the
  `gcp` category; for drift, patch readiness and stockout, neither of
  `CONTAINER_KINDS`.

The reads start after the sweep and must end inside the collector's time
budget (`RUN_DEADLINE_S` from the collector's start), so the manifest prints
before the SOP's terminal timeout. Each timeout is cut to the time left, and
an entry whose read cannot start in time gets `kcc_unread` that says so.

An object with no `Ready` condition yet is left out: that is the normal state
for seconds after an apply, and it is not evidence of a stall.

`collect.py` gives the compliance stream these keys from the same read, which
its `kcc-object-wedged` check also uses (`read_fields`), so that stream reads
once. The streams whose fixes edit Config Connector declarations and that do
not read `gcp` themselves call `annotate_manifest` after their sweep.

Stdlib only, and importable by every collector in this directory.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Iterable, Iterator, NamedTuple

# The category Config Connector registers across every CRD it installs.
# `read_objects` keeps the kinds in it and lists each in parallel, because
# `kubectl get gcp -A` lists the ~230 kinds one after another, too slowly
# through the credential proxy.
KCC_CATEGORY = "gcp"
# The one condition every Config Connector kind carries.
KCC_READY_CONDITION = "Ready"
# The `Ready=False` reasons Config Connector sets while it still applies or
# deletes an object. They are not a stall: the next reconcile usually clears
# them, so a note that says "not applying" on them would be false.
TRANSIENT_REASONS = frozenset({"Updating", "Deleting"})
# The kind whose identity needs its cluster: every cluster has a `default-pool`.
KCC_NODE_POOL_KIND = "ContainerNodePool"
# The annotation that names the GCP project a Config Connector object manages.
KCC_PROJECT_ANNOTATION = "cnrm.cloud.google.com/project-id"
# The manifest keys this module writes on a cluster entry.
UNREADY_FIELD = "kcc_unready"
UNREAD_FIELD = "kcc_unread"
# How much of a controller message survives. Config Connector pastes the whole
# upstream API error in, which for a 403 is several hundred characters of URL
# after the part that matters. The same budget as collect.py's
# `MAX_KCC_WEDGE_MESSAGE_CHARS`.
MAX_WHY_CHARS = 240
# What `why` says when the condition carries neither a reason nor a message.
NO_REASON_GIVEN = "no reason given"
# collect.py's `KCC_READ_TIMEOUT_S`: the most time the whole read of one
# cluster (the discovery and every kind's list) may take.
READ_TIMEOUT_S = 240
# The suffix of every Config Connector API group.
KCC_GROUP_SUFFIX = ".cnrm.cloud.google.com"
# The discovery that finds the Config Connector kinds: `kubectl api-resources`
# reads the API server's discovery documents, which every authenticated
# identity may read, so no RBAC grant is needed to learn that a cluster serves
# no Config Connector kind. With aggregated discovery it is one or two
# requests. `-o wide` adds the verbs and the categories; `--no-headers` leaves
# one resource per line.
DISCOVERY_ARGV = ("kubectl", "api-resources", "-o", "wide", "--no-headers")
# The kinds the drift, patch readiness and stockout collectors read: their
# findings are on `Cluster/` and `NodePool/` objects, and a finding's note
# names only its own object, so no other kind can change a note. From the
# agent each list request through the credential proxy takes seconds, and
# listing all ~230 kinds of an install does not fit the time budget. The
# compliance collector reads every kind (`kinds=None`), because its
# `kcc-object-wedged` check needs them all. As `(group, kind)`.
CONTAINER_GROUP = "container.cnrm.cloud.google.com"
CONTAINER_KINDS = frozenset({(CONTAINER_GROUP, "ContainerCluster"), (CONTAINER_GROUP, KCC_NODE_POOL_KIND)})
# The entry key that names the kinds a read listed, when it listed only some
# (`CONTAINER_KINDS`): `finish` then knows that an object of another kind
# was not read, rather than read and ready.
KINDS_READ_FIELD = "kcc_kinds_read"
# The words of an `api-resources -o wide` line after NAME and SHORTNAMES:
# APIVERSION, NAMESPACED, KIND, VERBS, CATEGORIES. SHORTNAMES can be empty, so
# a line is read from its NAMESPACED column (`true` or `false`).
NAMESPACED_VALUES = ("true", "false")
DISCOVERY_LIST_SEPARATOR = ","
LIST_VERB = "list"
# What `kubectl api-resources` says, and exits 1 with, when an aggregated API
# (often `metrics.k8s.io`) did not answer. The resources it printed are still
# good unless the group that did not answer is a Config Connector group.
PARTIAL_DISCOVERY_MARKER = "unable to retrieve the complete list of server APIs"
# The timeout of the discovery, and of one kind's list request; both are also
# cut to the time left.
DISCOVERY_TIMEOUT_S = 60
KIND_READ_TIMEOUT_S = 60
# How many Config Connector requests run at once in this process, across all
# clusters (`proxy_slots`): the sweep width the credential broker admits, as
# every pool in this directory reads it (`broker_admission.sweep_width`), and
# `FALLBACK_SLOTS` when that module cannot be imported. The collectors read
# several clusters at once, each with its own kind pool, so without this bound
# width x width requests would queue in the broker, which refuses a request
# that waits past its slot wait and so makes a host's read unknown. Every
# `kubectl` call of the read takes a slot first (`_with_proxy_slot`), and one
# cluster's kind pool is as wide as the slots. Other reads of the compliance
# sweep threads at the same time are not counted here, so the broker's queue
# can still hold up to one sweep width less one more requests than this bound.
# Each request is its own `kubectl` process and its own response, so no
# response nears the broker's output cap.
FALLBACK_SLOTS = 4
_slot_pool: "threading.BoundedSemaphore | None" = None
_slot_pool_lock = threading.Lock()
# What a step says when no slot or no time was left before the deadline.
NO_SLOT_DETAIL = "no proxy slot was free before the read's deadline"
# What the API server says for a kind whose CRD went away after the discovery:
# that kind holds no object, so it is skipped.
KIND_GONE_MARKERS = ("NotFound", "the server could not find the requested resource")
# The outcomes of a read (`KccRead.status`).
READ_LISTED = "listed"
READ_ABSENT = "absent"
READ_TIMED_OUT = "timed-out"
READ_FAILED = "failed"
READ_UNREADABLE = "unreadable"
# The timeout for `get-credentials`, collect.py's `DEFAULT_TIMEOUT_S`.
CREDENTIALS_TIMEOUT_S = 60
# The SOPs run each collector in one terminal call with this timeout. When it
# expires, the call is killed and the manifest is lost.
TERMINAL_TIMEOUT_S = 600
# The time kept free after the last read: the manifest must be printed, and
# the interpreter started before the collector's clock did.
PRINT_MARGIN_S = 60
# When the reads must end, in seconds from the collector's start.
RUN_DEADLINE_S = TERMINAL_TIMEOUT_S - PRINT_MARGIN_S
# A step that has less time than this left does not start: a read cut to a
# few seconds cannot finish on a cluster that runs Config Connector.
MIN_STEP_S = 10
# What `kcc_unread` says for an entry whose read did not start in time.
BUDGET_SPENT_REASON = (
    "the collector's time budget ({budget}s from its start) was spent before this read could start, "
    "so whether Config Connector applies its objects here is not known."
)
# What `default_run` returns for a command it killed, as `timeout(1)` does.
TIMEOUT_RC = 124
# How much of stderr goes into a `kcc_unread` reason.
MAX_ERROR_CHARS = 300
# The manifest outcome of a cluster the collector read.
OUTCOME_COLLECTED = "collected"
# The entries that are not clusters: stockout's `project/<id>` targets, at
# location `global`, hold the project-level reads and have no kubeconfig.
PROJECT_TARGET_PREFIX = "project/"
GLOBAL_LOCATION = "global"
# GKE's cluster status, where a collector records it on the entry (the drift
# collector marks every listed cluster `collected`, in any status), and the
# statuses in which the cluster's API server answers. A cluster that is
# PROVISIONING, STOPPING or in ERROR cannot run the Config Connector
# controllers usefully, and a read there only spends the time budget, so it
# is not read and is not a host.
CLUSTER_STATUS_FIELD = "cluster_status"
READABLE_STATUSES = frozenset({"RUNNING", "RECONCILING"})
# The flag that turns the read on. A collector reads Config Connector only
# with it: only `finish` uses the result. The SOP command passes it whenever
# the audit runs by its SOP, scheduled or on demand
# (references/on-demand.md); a chat fleet question runs the same collectors
# without it and pays nothing for a read that nothing uses.
FLAG = "--config-connector"
FLAG_HELP = (
    "after the sweep, read Config Connector on each collected cluster and record kcc_unready or "
    "kcc_unread on its entry, for finish; the SOP command passes it, scheduled or on demand, and a "
    "chat fleet question does not"
)
# The carried key a collector sets on a cluster whose `clusters list` record
# shows the Config Connector add-on enabled
# (`addonsConfig.configConnectorConfig.enabled`). Those clusters are read
# first, so a large fleet cannot spend the time budget before it reaches them.
ADDON_FIELD = "config_connector_addon"
# The second read, on a host whose unready objects carry no project
# annotation: Config Connector takes the project from the object's
# namespace annotation then, so the namespaces give it. Its own timeout,
# also cut to the time left.
NAMESPACES_TIMEOUT_S = 60
# What `kcc_unread` says when the kubeconfig file is not there. kubectl does
# not fail on a missing `KUBECONFIG` file: it reads the defaults or the
# in-cluster account, which is another cluster.
NO_KUBECONFIG_REASON = (
    "no kubeconfig at {path}, so the read did not run and whether Config Connector applies "
    "its objects here is not known."
)


class Result(NamedTuple):
    rc: int
    stdout: str
    stderr: str


RunFn = Callable[..., Result]
# How `annotate_manifest` maps its read over the clusters: the builtin `map`,
# one at a time, or the `map` of a `ThreadPoolExecutor` that the collector
# opens after its sweep. That pool is a new one, with the sweep's width
# (`broker_admission.sweep_width`), and `proxy_slots` bounds the requests.
MapFn = Callable[[Callable[[dict], dict], Iterable[dict]], Iterator[dict]]
# The kubeconfig a collector already fetched for an entry, or None. With one,
# `annotate_manifest` does not run `get-credentials` again.
KubeconfigFn = Callable[[dict], "Path | None"]
# The clock the deadline is measured on.
ClockFn = Callable[[], float]


def default_run(argv: list[str], *, env: dict | None = None, timeout: int = READ_TIMEOUT_S) -> Result:
    """Run `argv`; a timeout is `TIMEOUT_RC`, never an exception."""
    try:
        done = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return Result(TIMEOUT_RC, "", f"killed after {timeout}s")
    except OSError as exc:
        return Result(-1, "", str(exc))
    return Result(done.returncode, done.stdout, done.stderr)


def ready_condition(item: dict) -> dict | None:
    """The `Ready` condition off a Config Connector object, if it has one."""
    status = item.get("status")
    conditions = status.get("conditions") if isinstance(status, dict) else None
    for condition in conditions or []:
        if isinstance(condition, dict) and condition.get("type") == KCC_READY_CONDITION:
            return condition
    return None


def why_not_ready(condition: dict) -> str:
    """`<reason>: <message>` off a `Ready=False` condition, on one line and clipped."""
    reason = str(condition.get("reason") or "").strip()
    message = str(condition.get("message") or "").strip()
    why = f"{reason}: {message}" if reason and message else reason or message or NO_REASON_GIVEN
    return " ".join(why.split())[:MAX_WHY_CHARS]


def _cluster_ref(spec: dict) -> str:
    """The bare cluster name a `ContainerNodePool`'s `clusterRef` names."""
    ref = spec.get("clusterRef")
    if not isinstance(ref, dict):
        return ""
    return str(ref.get("name") or ref.get("external") or "").rstrip("/").rpartition("/")[2]


def unready_objects(items: list) -> list[dict]:
    """Every object in the merged Config Connector lists (`read_objects`)
    whose `Ready` is `False`."""
    found: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        ready = ready_condition(item)
        if ready is None or str(ready.get("status")) != "False":
            continue
        if str(ready.get("reason") or "").strip() in TRANSIENT_REASONS:
            continue
        meta = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        kind = str(item.get("kind") or "")
        name = str(meta.get("name") or "")
        if not kind or not name:
            continue
        record = {
            "kind": kind,
            "namespace": str(meta.get("namespace") or ""),
            "name": name,
            "why": why_not_ready(ready),
        }
        spec = item.get("spec") if isinstance(item.get("spec"), dict) else {}
        resource = str(spec.get("resourceID") or "")
        if resource and resource != name:
            record["resource"] = resource
        if kind == KCC_NODE_POOL_KIND and _cluster_ref(spec):
            record["cluster"] = _cluster_ref(spec)
        location = str(spec.get("location") or "")
        if location:
            record["location"] = location
        annotations = meta.get("annotations") if isinstance(meta.get("annotations"), dict) else {}
        project = str(annotations.get(KCC_PROJECT_ANNOTATION) or "")
        if project:
            record["project"] = project
        found.append(record)
    return sorted(found, key=lambda r: (r["kind"], r["namespace"], r["name"]))


def fill_namespace_projects(records: list[dict], namespaces: list) -> None:
    """Give each record with no `project` the project Config Connector uses.

    Config Connector takes the project from the namespace's
    `cnrm.cloud.google.com/project-id` annotation when the object does not
    name one, and uses the namespace's name as the project ID when the
    namespace has no annotation either. `namespaces` is the list of
    Namespace objects from a read that succeeded; a record whose namespace
    is not in it keeps its project unknown.
    """
    projects: dict[str, str] = {}
    for item in namespaces or []:
        meta = item.get("metadata") if isinstance(item, dict) and isinstance(item.get("metadata"), dict) else {}
        annotations = meta.get("annotations") if isinstance(meta.get("annotations"), dict) else {}
        name = str(meta.get("name") or "")
        if name:
            projects[name] = str(annotations.get(KCC_PROJECT_ANNOTATION) or "") or name
    for record in records:
        if not record.get("project") and projects.get(record.get("namespace", "")):
            record["project"] = projects[record["namespace"]]


def _needs_namespace_projects(fields: dict) -> bool:
    return any(not record.get("project") for record in fields.get(UNREADY_FIELD) or [])


def read_namespace_projects(fields: dict, kubeconfig: Path, *, run: RunFn, deadline: float, clock: ClockFn = time.monotonic) -> None:
    """Fill the unready objects' missing projects from one namespaces read.
    A failed read, or one that cannot start before `deadline`, leaves them
    unknown."""
    result = _with_proxy_slot(
        run, ["kubectl", "get", "namespaces", "-o", "json"], kubeconfig, NAMESPACES_TIMEOUT_S, deadline, clock
    )
    if result is None or result.rc != 0:
        return
    try:
        parsed = json.loads(result.stdout)
    except json.JSONDecodeError:
        return
    if isinstance(parsed, dict) and isinstance(parsed.get("items"), list):
        fill_namespace_projects(fields[UNREADY_FIELD], parsed["items"])


class KccKind(NamedTuple):
    """One Config Connector kind, as the discovery names it."""

    group: str
    version: str
    plural: str
    kind: str


class KccRead(NamedTuple):
    """What one cluster's Config Connector read found.

    `status` is one of the `READ_*` outcomes; `items` holds every object of
    every kind when it is `READ_LISTED`; `detail` says what went wrong
    otherwise; `kinds` is how many Config Connector kinds the discovery named;
    `timeout_s` is the most time the read had.
    """

    status: str
    items: list
    detail: str
    kinds: int
    timeout_s: int


def discovery_argv() -> list[str]:
    """The discovery that finds the Config Connector kinds."""
    return list(DISCOVERY_ARGV)


def kind_argv(kind: KccKind) -> list[str]:
    """One kind's cluster-wide list, with no discovery."""
    return ["kubectl", "get", "--raw", f"/apis/{kind.group}/{kind.version}/{kind.plural}"]


def kcc_kinds(discovery: str) -> list[KccKind]:
    """The Config Connector kinds in `kubectl api-resources -o wide` output: a
    group that ends in `KCC_GROUP_SUFFIX`, the `gcp` category, and the `list`
    verb."""
    kinds: list[KccKind] = []
    for line in discovery.splitlines():
        words = line.split()
        marks = [index for index, word in enumerate(words) if word in NAMESPACED_VALUES]
        if not words or not marks or marks[0] < 2 or len(words) <= marks[0] + 1:
            continue
        at = marks[0]
        group, _, version = words[at - 1].rpartition("/")
        kind = words[at + 1]
        verbs = words[at + 2].split(DISCOVERY_LIST_SEPARATOR) if len(words) > at + 2 else []
        categories = words[at + 3].split(DISCOVERY_LIST_SEPARATOR) if len(words) > at + 3 else []
        if group.endswith(KCC_GROUP_SUFFIX) and KCC_CATEGORY in categories and LIST_VERB in verbs and version:
            kinds.append(KccKind(group, version, words[0], kind))
    return sorted(set(kinds))


def proxy_slots() -> int:
    """How many Config Connector requests may run at once in this process:
    the broker's sweep width (`broker_admission.sweep_width`, read once per
    process), or `FALLBACK_SLOTS` when that module is not on the path."""
    try:
        import broker_admission  # noqa: PLC0415 -- in agents/platform/scripts, which the collectors put on the path
    except ImportError:
        return FALLBACK_SLOTS
    return broker_admission.sweep_width()


def _proxy_slot_pool() -> "threading.BoundedSemaphore":
    """The process's one slot pool, made at its first use with `proxy_slots()`."""
    global _slot_pool
    with _slot_pool_lock:
        if _slot_pool is None:
            _slot_pool = threading.BoundedSemaphore(proxy_slots())
        return _slot_pool


def _with_proxy_slot(
    run: RunFn, argv: list[str], kubeconfig: Path, timeout_s: int, deadline: float, clock: ClockFn
) -> "Result | None":
    """`run(argv)` while holding one of the `proxy_slots`, or None when no
    slot is free, or no step's time is left, before `deadline`."""
    wait = deadline - clock()
    slots = _proxy_slot_pool()
    if wait <= 0 or not slots.acquire(timeout=wait):
        return None
    try:
        left = _time_left(deadline, clock)
        if not left:
            return None
        return run(argv, env={**os.environ, "KUBECONFIG": str(kubeconfig)}, timeout=min(timeout_s, left))
    finally:
        slots.release()


def _excerpt(stderr: str) -> str:
    return " ".join(stderr.split())[:MAX_ERROR_CHARS] or "no stderr"


def read_objects(
    kubeconfig: Path,
    *,
    run: RunFn = default_run,
    deadline: float,
    clock: ClockFn = time.monotonic,
    workers: int | None = None,
    kinds: "frozenset[tuple[str, str]] | None" = None,
) -> KccRead:
    """List every Config Connector object on one cluster before `deadline`.

    Discovery (`kubectl api-resources`) finds the kinds (only those in
    `kinds`, as `(group, kind)`, when it is given; none served is
    `READ_ABSENT`), then one list
    request per kind runs on a pool of `workers` (`proxy_slots()` when
    not given). Every `kubectl` call takes a proxy slot first
    (`_with_proxy_slot`). Each step's timeout is cut to the time left, and a
    step that cannot start in time is not run, so the read ends by
    `deadline`. A kind that does not list makes the whole read unknown,
    except a kind whose CRD went away after the discovery.
    """
    timeout_s = max(int(deadline - clock()), 0)
    found = _with_proxy_slot(run, discovery_argv(), kubeconfig, DISCOVERY_TIMEOUT_S, deadline, clock)
    if found is None:
        return KccRead(READ_TIMED_OUT, [], f"the discovery did not start: {NO_SLOT_DETAIL}", 0, timeout_s)
    if found.rc == TIMEOUT_RC:
        return KccRead(READ_TIMED_OUT, [], "the discovery did not finish", 0, timeout_s)
    partial = found.rc != 0 and PARTIAL_DISCOVERY_MARKER in found.stderr and KCC_GROUP_SUFFIX not in found.stderr
    if found.rc != 0 and not partial:
        return KccRead(READ_FAILED, [], f"the discovery exited {found.rc} ({_excerpt(found.stderr)})", 0, timeout_s)
    wanted = kinds
    kinds = [kind for kind in kcc_kinds(found.stdout) if wanted is None or (kind.group, kind.kind) in wanted]
    if not kinds:
        return KccRead(READ_ABSENT, [], "", 0, timeout_s)

    def one(kind: KccKind) -> tuple[str, list, str]:
        result = _with_proxy_slot(run, kind_argv(kind), kubeconfig, KIND_READ_TIMEOUT_S, deadline, clock)
        if result is None:
            return READ_TIMED_OUT, [], f"{kind.plural}.{kind.group}: {NO_SLOT_DETAIL}"
        if result.rc == TIMEOUT_RC:
            return READ_TIMED_OUT, [], f"{kind.plural}.{kind.group}: the list did not finish"
        if result.rc != 0:
            if any(marker in result.stderr for marker in KIND_GONE_MARKERS):
                return READ_LISTED, [], ""
            return READ_FAILED, [], f"{kind.plural}.{kind.group}: the list exited {result.rc} ({_excerpt(result.stderr)})"
        try:
            parsed = json.loads(result.stdout)
        except json.JSONDecodeError:
            parsed = None
        if not isinstance(parsed, dict) or not isinstance(parsed.get("items"), list):
            return READ_UNREADABLE, [], f"{kind.plural}.{kind.group}: the list is not a list of objects"
        # A list item can leave out its kind; the CRD names it.
        items = [{"kind": kind.kind, **item} if isinstance(item, dict) and not item.get("kind") else item for item in parsed["items"]]
        return READ_LISTED, items, ""

    workers = workers or proxy_slots()
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(kinds)))) as pool:
        results = list(pool.map(one, kinds))
    for status in (READ_FAILED, READ_UNREADABLE, READ_TIMED_OUT):
        problems = [detail for got, _, detail in results if got == status]
        if problems:
            more = f" (and {len(problems) - 1} more kind(s))" if len(problems) > 1 else ""
            return KccRead(status, [], problems[0] + more, len(kinds), timeout_s)
    return KccRead(READ_LISTED, [item for _, items, _ in results for item in items], "", len(kinds), timeout_s)


def read_fields(read: KccRead) -> dict:
    """The keys one cluster's read (`read_objects`) puts on its manifest entry."""
    if read.status == READ_LISTED:
        return {UNREADY_FIELD: unready_objects(read.items)}
    if read.status == READ_ABSENT:
        return {}
    unknown = "so whether Config Connector applies its objects here is not known."
    if read.status == READ_TIMED_OUT:
        return {UNREAD_FIELD: f"The Config Connector read did not finish in {read.timeout_s}s ({read.detail}), {unknown}"}
    return {UNREAD_FIELD: f"The Config Connector read failed ({read.detail}), {unknown}"}


def read_cluster(
    kubeconfig: Path,
    *,
    run: RunFn = default_run,
    deadline: float,
    clock: ClockFn = time.monotonic,
    kinds: "frozenset[tuple[str, str]] | None" = None,
) -> dict:
    """Read one cluster and return its entry keys (`read_fields`). A read of
    only some `kinds` that listed them also names them (`KINDS_READ_FIELD`)."""
    fields = read_fields(read_objects(kubeconfig, run=run, deadline=deadline, clock=clock, kinds=kinds))
    if kinds is not None and UNREADY_FIELD in fields:
        fields[KINDS_READ_FIELD] = sorted(kind for _, kind in kinds)
    return fields


def add_flag(parser) -> None:
    """Add `FLAG` to a collector's argument parser (`args.config_connector`)."""
    parser.add_argument(FLAG, action="store_true", help=FLAG_HELP)


def addon_enabled(cluster: dict) -> bool:
    """Whether a `clusters list` record shows the Config Connector add-on on."""
    addons = cluster.get("addonsConfig") if isinstance(cluster.get("addonsConfig"), dict) else {}
    config = addons.get("configConnectorConfig") if isinstance(addons.get("configConnectorConfig"), dict) else {}
    return bool(config.get("enabled"))


def run_deadline(started: float) -> float:
    """The `deadline` for `annotate_manifest`, from the collector's start."""
    return started + RUN_DEADLINE_S


def _budget_spent() -> dict:
    return {UNREAD_FIELD: BUDGET_SPENT_REASON.format(budget=RUN_DEADLINE_S)}


def _time_left(deadline: float, clock: ClockFn) -> int:
    """Whole seconds to `deadline`, or 0 when less than `MIN_STEP_S` is left."""
    left = int(deadline - clock())
    return left if left >= MIN_STEP_S else 0


def _annotate_entry(
    entry: dict,
    run: RunFn,
    deadline: float,
    clock: ClockFn,
    kubeconfig_for: KubeconfigFn | None,
    kinds: "frozenset[tuple[str, str]] | None" = None,
) -> dict:
    """Fetch one cluster's credentials and read it; the keys for its entry.

    The kubeconfig goes where `collect.kubeconfig_path` puts it, so every
    collector writes one file for a cluster.

    Each step's timeout is cut to the time left before `deadline`, so the
    read ends before it. A step that cannot start in time is not run.
    """
    project = str(entry.get("project") or "")
    location = str(entry.get("location") or "")
    cluster = str(entry.get("name") or "").rpartition("/")[2]
    kubeconfig = kubeconfig_for(entry) if kubeconfig_for else None
    if kubeconfig is None:
        left = _time_left(deadline, clock)
        if not left:
            return _budget_spent()
        from collect import kubeconfig_path, prepare_kubeconfig  # noqa: PLC0415 -- the one rule for the shared directory, in collect.py

        kubeconfig = kubeconfig_path(project, cluster, location)
        note = prepare_kubeconfig(kubeconfig)
        if note:
            return {UNREAD_FIELD: f"{note}, so whether Config Connector applies its objects here is not known."}
        argv = ["gcloud", "container", "clusters", "get-credentials", cluster, "--location", location, "--project", project]
        fetched = run(argv, env={**os.environ, "KUBECONFIG": str(kubeconfig)}, timeout=min(CREDENTIALS_TIMEOUT_S, left))
        if fetched.rc != 0:
            excerpt = " ".join(fetched.stderr.split())[:MAX_ERROR_CHARS] or "no stderr"
            return {
                UNREAD_FIELD: f"get-credentials exited {fetched.rc} ({excerpt}), so whether Config "
                "Connector applies its objects here is not known."
            }
    if not kubeconfig.is_file():
        return {UNREAD_FIELD: NO_KUBECONFIG_REASON.format(path=kubeconfig)}
    left = _time_left(deadline, clock)
    if not left:
        return _budget_spent()
    fields = read_cluster(kubeconfig, run=run, deadline=min(deadline, clock() + READ_TIMEOUT_S), clock=clock, kinds=kinds)
    if _needs_namespace_projects(fields):
        left = _time_left(deadline, clock)
        if left:
            read_namespace_projects(fields, kubeconfig, run=run, deadline=deadline, clock=clock)
    return fields


def annotate_manifest(
    manifest: dict,
    *,
    deadline: float,
    run: RunFn = default_run,
    mapper: MapFn = map,
    clock: ClockFn = time.monotonic,
    kubeconfig_for: KubeconfigFn | None = None,
    kinds: "frozenset[tuple[str, str]] | None" = None,
) -> None:
    """Read every collected cluster in `manifest` once and add its keys.

    `kinds` limits the read to those `(group, kind)` pairs; the drift, patch
    readiness and stockout collectors pass `CONTAINER_KINDS`. A cluster that
    serves none of them gets neither key, as a cluster with no Config
    Connector does: for these collectors it holds no object a note can name.

    Only `collected` cluster entries that name a project and a location are
    read: a `project/<id>` entry at location `global` is not a cluster, and
    an entry whose `cluster_status` is not RUNNING or RECONCILING is not read
    and gets no key. An
    entry that already carries either key was read by its collector. No read
    runs without a kubeconfig file: that entry gets `kcc_unread`.
    `deadline` is a `clock` time (`run_deadline`): every read ends before it,
    and an entry whose read cannot start in time gets `kcc_unread`.
    `kubeconfig_for` gives the kubeconfig the collector already fetched for
    an entry, so no second `get-credentials` runs.

    This function never raises. A read that raises, or a failure around the
    reads, is recorded as `kcc_unread`: this is an addition to a manifest
    whose work is done, and it must not lose that work.
    """
    targets = [
        entry
        for entry in manifest.get("clusters") or []
        if isinstance(entry, dict)
        and entry.get("outcome") == OUTCOME_COLLECTED
        and entry.get("project")
        and entry.get("location")
        and entry.get("location") != GLOBAL_LOCATION
        and not str(entry.get("name") or "").startswith(PROJECT_TARGET_PREFIX)
        and (not entry.get(CLUSTER_STATUS_FIELD) or entry.get(CLUSTER_STATUS_FIELD) in READABLE_STATUSES)
        and UNREADY_FIELD not in entry
        and UNREAD_FIELD not in entry
    ]
    if not targets:
        return
    # Clusters with the add-on first (`ADDON_FIELD`); the sort is stable, so
    # the rest keep the collector's order.
    targets.sort(key=lambda entry: not entry.get(ADDON_FIELD))

    def one(entry: dict) -> dict:
        try:
            return _annotate_entry(entry, run, deadline, clock, kubeconfig_for, kinds)
        except Exception as exc:  # noqa: BLE001 -- the manifest stands without this read
            return {UNREAD_FIELD: f"the read raised {type(exc).__name__}: {exc}"[:MAX_ERROR_CHARS]}

    try:
        if not _time_left(deadline, clock):
            # Nothing can start: no directory is touched and nothing is run.
            for entry in targets:
                entry.update(_budget_spent())
            return
        if kubeconfig_for is None:
            import collect  # noqa: PLC0415 -- the one rule for the shared directory, in collect.py

            # Made usable once before the pool opens, as fleet_stockout.py
            # does: the threads would otherwise race one another's move-aside.
            note = collect.usable_kubeconfig_dir(collect.KUBECONFIG_DIR)
            if note:
                for entry in targets:
                    entry[UNREAD_FIELD] = f"{note}, so whether Config Connector applies its objects here is not known."
                return
        for entry, fields in zip(targets, mapper(one, targets)):
            entry.update(fields)
    except Exception as exc:  # noqa: BLE001 -- the manifest stands without this read
        reason = f"the Config Connector read raised {type(exc).__name__}: {exc}"[:MAX_ERROR_CHARS]
        for entry in targets:
            if UNREADY_FIELD not in entry and UNREAD_FIELD not in entry:
                entry[UNREAD_FIELD] = reason
