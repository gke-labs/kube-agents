"""kcc_health.py — the Config Connector objects that are not applying, as the
collectors record them for `finish`.

A `manifest` fix that edits a Config Connector declaration does nothing while
Config Connector does not apply that object. On 2026-09-05 one `PubSubTopic`
failed with `UpdateFailed` on an IAM role and put fifteen `ContainerCluster`s
into `DependencyNotReady`; every manifest fix against them would have merged
into a controller that applied nothing. The collectors read GCP, where those
clusters were healthy, so no stream saw it.

`finish` must not read a cluster (docs/designs/fleet-audit-collector-manifest.md):
the collectors do the reading. This module is the one read they share. On each
cluster a collector swept, it runs `kubectl get gcp -A -o json` once, and puts
the result on that cluster's manifest entry:

- `kcc_unready`: the objects whose `Ready` condition is `False` for a reason
  other than a transient one (`TRANSIENT_REASONS`: Config Connector is still
  applying or deleting the object), each as
  `{kind, namespace, name, why}`, plus `resource` (the object's
  `spec.resourceID`), `cluster` (a `ContainerNodePool`'s `clusterRef`) and
  `project` (the GCP project the object manages, from its
  `cnrm.cloud.google.com/project-id` annotation) where they are set. An object
  without the annotation has no `project`: the namespace's annotation is not
  read. An empty list means the cluster runs Config Connector and every
  object it holds is ready or not yet reached.
- `kcc_unread`: why the read did not give an answer (a timeout, a forbidden
  read, output that is not a list). This is "unknown", never "healthy".
- Neither key on a `collected` entry: the API server does not serve the `gcp`
  category, so Config Connector does not run on that cluster.

The reads start after the sweep and must end inside the collector's time
budget (`RUN_DEADLINE_S` from the collector's start), so the manifest prints
before the SOP's terminal timeout. Each timeout is cut to the time left, and
an entry whose read cannot start in time gets `kcc_unread` that says so.

An object with no `Ready` condition yet is left out: that is the normal state
for seconds after an apply, and it is not evidence of a stall.

`collect.py` gives the compliance stream these keys from the read its
`kcc-object-wedged` check already makes (`entry_fields`), so that stream reads
once. The streams whose fixes edit Config Connector declarations and that do
not read `gcp` themselves call `annotate_manifest` after their sweep.

Stdlib only, and importable by every collector in this directory.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Callable, Iterable, Iterator, NamedTuple

# The category Config Connector registers across every CRD it installs, so one
# read returns every object it owns.
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
# collect.py's `KCC_READ_TIMEOUT_S`, for the reason given there: `kubectl`
# expands the category to every CRD in it and lists each, so the read is slow
# exactly on the cluster that runs Config Connector.
READ_TIMEOUT_S = 240
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
# The API server's answer when it does not serve the category.
RESOURCE_TYPE_ABSENT_MARKER = "doesn't have a resource type"
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
# opens after its sweep. That pool is a new one, with the sweep's size
# (`MAX_WORKERS`), so the reads stay inside the broker's admitted count.
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
    """Every object in a `kubectl get gcp -A` list whose `Ready` is `False`."""
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
        annotations = meta.get("annotations") if isinstance(meta.get("annotations"), dict) else {}
        project = str(annotations.get(KCC_PROJECT_ANNOTATION) or "")
        if project:
            record["project"] = project
        found.append(record)
    return sorted(found, key=lambda r: (r["kind"], r["namespace"], r["name"]))


def entry_fields(parsed: object, rc: int, stderr: str, timeout_s: int = READ_TIMEOUT_S) -> dict:
    """The keys one cluster's `gcp` read puts on its manifest entry.

    `parsed` is the read's output as JSON, or None when it was not readable.
    `timeout_s` is the timeout the read had.
    """
    if rc == 0 and isinstance(parsed, dict) and isinstance(parsed.get("items"), list):
        return {UNREADY_FIELD: unready_objects(parsed["items"])}
    if rc == TIMEOUT_RC:
        return {
            UNREAD_FIELD: f"`kubectl get {KCC_CATEGORY} -A` did not finish in {timeout_s}s, "
            "so whether Config Connector applies its objects here is not known."
        }
    if rc != 0 and RESOURCE_TYPE_ABSENT_MARKER in stderr:
        return {}
    if rc != 0:
        excerpt = " ".join(stderr.split())[:MAX_ERROR_CHARS] or "no stderr"
        return {
            UNREAD_FIELD: f"`kubectl get {KCC_CATEGORY} -A` exited {rc} ({excerpt}), "
            "so whether Config Connector applies its objects here is not known."
        }
    return {
        UNREAD_FIELD: f"`kubectl get {KCC_CATEGORY} -A` exited 0 with output that is not a list "
        "of objects, so whether Config Connector applies its objects here is not known."
    }


def read_argv() -> list[str]:
    """The one read, as collect.py's compliance context builder issues it."""
    return ["kubectl", "get", KCC_CATEGORY, "-A", "-o", "json"]


def read_cluster(kubeconfig: Path, *, run: RunFn = default_run, timeout_s: int = READ_TIMEOUT_S) -> dict:
    """Read one cluster and return its entry keys (`entry_fields`)."""
    result = run(read_argv(), env={**os.environ, "KUBECONFIG": str(kubeconfig)}, timeout=timeout_s)
    try:
        parsed = json.loads(result.stdout) if result.rc == 0 and result.stdout.strip() else None
    except json.JSONDecodeError:
        parsed = None
    return entry_fields(parsed, result.rc, result.stderr, timeout_s)


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
    entry: dict, run: RunFn, deadline: float, clock: ClockFn, kubeconfig_for: KubeconfigFn | None
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
    return read_cluster(kubeconfig, run=run, timeout_s=min(READ_TIMEOUT_S, left))


def annotate_manifest(
    manifest: dict,
    *,
    deadline: float,
    run: RunFn = default_run,
    mapper: MapFn = map,
    clock: ClockFn = time.monotonic,
    kubeconfig_for: KubeconfigFn | None = None,
) -> None:
    """Read every collected cluster in `manifest` once and add its keys.

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

    def one(entry: dict) -> dict:
        try:
            return _annotate_entry(entry, run, deadline, clock, kubeconfig_for)
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
