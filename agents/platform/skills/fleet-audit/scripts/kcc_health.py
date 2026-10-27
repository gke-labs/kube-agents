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

- `kcc_unready`: the objects whose `Ready` condition is `False`, each as
  `{kind, namespace, name, why}`, plus `resource` (the object's
  `spec.resourceID`) and `cluster` (a `ContainerNodePool`'s `clusterRef`) where
  they are set. An empty list means the cluster runs Config Connector and every
  object it holds is ready or not yet reached.
- `kcc_unread`: why the read did not give an answer (a timeout, a forbidden
  read, output that is not a list). This is "unknown", never "healthy".
- Neither key: the API server does not serve the `gcp` category, so Config
  Connector does not run on that cluster.

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
from pathlib import Path
from typing import Callable, Iterable, Iterator, NamedTuple

# The category Config Connector registers across every CRD it installs, so one
# read returns every object it owns.
KCC_CATEGORY = "gcp"
# The one condition every Config Connector kind carries.
KCC_READY_CONDITION = "Ready"
# The kind whose identity needs its cluster: every cluster has a `default-pool`.
KCC_NODE_POOL_KIND = "ContainerNodePool"
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
# What `default_run` returns for a command it killed, as `timeout(1)` does.
TIMEOUT_RC = 124
# The API server's answer when it does not serve the category.
RESOURCE_TYPE_ABSENT_MARKER = "doesn't have a resource type"
# How much of stderr goes into a `kcc_unread` reason.
MAX_ERROR_CHARS = 300
# The manifest outcome of a cluster the collector read.
OUTCOME_COLLECTED = "collected"
# Where the fetched kubeconfigs go: the agent's data volume, as every
# collector's own kubeconfigs do.
KUBECONFIG_DIR = Path(os.environ.get("HERMES_HOME") or "/opt/data") / ".kubeconfigs"
# The file name of one cluster's kubeconfig in `KUBECONFIG_DIR`.
KUBECONFIG_NAME = "kcc_{project}_{cluster}_{location}.yaml"


class Result(NamedTuple):
    rc: int
    stdout: str
    stderr: str


RunFn = Callable[..., Result]
# How `annotate_manifest` maps its read over the clusters: the builtin `map`,
# one at a time, or a collector's own `ThreadPoolExecutor.map`, so the reads
# share the pool that collector already holds to the broker's admitted count
# (collect.py's `MAX_WORKERS`) rather than opening a second one.
MapFn = Callable[[Callable[[dict], dict], Iterable[dict]], Iterator[dict]]


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
        found.append(record)
    return sorted(found, key=lambda r: (r["kind"], r["namespace"], r["name"]))


def entry_fields(parsed: object, rc: int, stderr: str) -> dict:
    """The keys one cluster's `gcp` read puts on its manifest entry.

    `parsed` is the read's output as JSON, or None when it was not readable.
    """
    if rc == 0 and isinstance(parsed, dict) and isinstance(parsed.get("items"), list):
        return {UNREADY_FIELD: unready_objects(parsed["items"])}
    if rc == TIMEOUT_RC:
        return {
            UNREAD_FIELD: f"`kubectl get {KCC_CATEGORY} -A` did not finish in {READ_TIMEOUT_S}s, "
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


def read_cluster(kubeconfig: Path, *, run: RunFn = default_run) -> dict:
    """Read one cluster and return its entry keys (`entry_fields`)."""
    result = run(read_argv(), env={**os.environ, "KUBECONFIG": str(kubeconfig)}, timeout=READ_TIMEOUT_S)
    try:
        parsed = json.loads(result.stdout) if result.rc == 0 and result.stdout.strip() else None
    except json.JSONDecodeError:
        parsed = None
    return entry_fields(parsed, result.rc, result.stderr)


def _annotate_entry(entry: dict, run: RunFn, kubeconfig_dir: Path) -> dict:
    """Fetch one cluster's credentials and read it; the keys for its entry."""
    project = str(entry.get("project") or "")
    location = str(entry.get("location") or "")
    cluster = str(entry.get("name") or "").rpartition("/")[2]
    kubeconfig = kubeconfig_dir / KUBECONFIG_NAME.format(project=project, cluster=cluster, location=location)
    argv = ["gcloud", "container", "clusters", "get-credentials", cluster, "--location", location, "--project", project]
    fetched = run(argv, env={**os.environ, "KUBECONFIG": str(kubeconfig)}, timeout=CREDENTIALS_TIMEOUT_S)
    if fetched.rc != 0:
        excerpt = " ".join(fetched.stderr.split())[:MAX_ERROR_CHARS] or "no stderr"
        return {
            UNREAD_FIELD: f"get-credentials exited {fetched.rc} ({excerpt}), so whether Config "
            "Connector applies its objects here is not known."
        }
    return read_cluster(kubeconfig, run=run)


def annotate_manifest(
    manifest: dict,
    *,
    run: RunFn = default_run,
    kubeconfig_dir: Path = KUBECONFIG_DIR,
    mapper: MapFn = map,
) -> None:
    """Read every collected cluster in `manifest` once and add its keys.

    Only `collected` entries that name a project and a location are read; an
    entry that already carries either key was read by its collector. A read
    that raises is recorded as `kcc_unread`: this is an addition to a manifest
    whose work is done, and it must not lose that work.
    """
    targets = [
        entry
        for entry in manifest.get("clusters") or []
        if isinstance(entry, dict)
        and entry.get("outcome") == OUTCOME_COLLECTED
        and entry.get("project")
        and entry.get("location")
        and UNREADY_FIELD not in entry
        and UNREAD_FIELD not in entry
    ]
    if not targets:
        return
    try:
        kubeconfig_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        for entry in targets:
            entry[UNREAD_FIELD] = f"no directory for the kubeconfig ({exc}), so whether Config Connector applies its objects here is not known."
        return

    def one(entry: dict) -> dict:
        try:
            return _annotate_entry(entry, run, kubeconfig_dir)
        except Exception as exc:  # noqa: BLE001 -- the manifest stands without this read
            return {UNREAD_FIELD: f"the read raised {type(exc).__name__}: {exc}"[:MAX_ERROR_CHARS]}

    for entry, fields in zip(targets, mapper(one, targets)):
        entry.update(fields)
