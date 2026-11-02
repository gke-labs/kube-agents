#!/usr/bin/env python3
"""Subprocess plumbing shared by the fleet-audit collectors.

One owner for the names every collector needs: `Run`, `default_run`,
`run_and_gate`, `paged_dump`, `Discovery`, `_text` and `TIMEOUT_RC`. A
collector binds them at import time (`Run = collector_run.Run`,
`default_run = functools.partial(collector_run.default_run, timeout=...)`), so
its tests and fakes keep addressing them through the collector's own name.
`default_run` has no default timeout here on purpose: the collectors do not
agree on one, and each binds its own.

`paged_dump` is how a collector reads a large kind. One `kubectl get a,b,c -A
-o json` dump of a few hundred nodes' worth of objects meets the credential
proxy's 8 MiB output cap, and the cut dump then fails the gate for the whole
cluster. A paged read asks the API server for one kind at a time, `PAGE_SIZE`
objects per page through `kubectl get --raw <collection>?limit=N&continue=T`,
decodes each page on its own and drops its `metadata.managedFields`, so no
single reply and no single decode holds more than one page. The merged result
has the `{"items": [...]}` shape the collectors already parse, each item
stamped with the `kind` and `apiVersion` a raw list omits, since the
collectors split a multi-kind dump by `kind`. The credential proxy's command
policy admits exactly these paths (`command_policy.KUBECTL_RAW_COLLECTIONS`);
`test_collector_run` holds `LIST_KINDS` inside that allowlist.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
import time
import urllib.parse
from typing import Callable, Iterable, NamedTuple

#: The exit code a timed-out child is reported with, the one `timeout(1)` uses.
TIMEOUT_RC = 124
# Every collector's `gcloud` and `kubectl` is the credential-proxy shim, which retries a broker
# refusal on its own (credential_proxy_client, keeping a retry's admission wait, not its run,
# inside a deadline the caller may shorten through this variable). The caps the collectors bind
# (60 to 240 s) hold at most one admission wait and one run against the broker's 300 s bound on
# a `gcloud` it runs (its `--timeout-seconds` default), so a retry would carry past the cap,
# where TIMEOUT_RC stands in for the broker's reason. `default_run` therefore hands every child
# the cap less that run bound, under which no retry fits, unless the caller's environment
# already names one; a refused read then records the refusal's text at once, as
# cluster_preflight.sh's calls do. The variable's name is spelled here rather than imported from
# the shim (test_collector_run.py holds the two equal), as the audits and the scaffold do.
SHIM_RETRY_DEADLINE_ENV = "CREDENTIAL_PROXY_RETRY_DEADLINE_SECONDS"
BROKER_GCLOUD_RUN_S = 300
#: Objects per page of a paged collection read: the API server's `limit`, the
#: count kubectl itself asks for per chunk, and small enough that a page of the
#: largest ordinary kind stays well under the credential proxy's output cap.
PAGE_SIZE = 500
#: Kinds whose objects are large enough that a page of `PAGE_SIZE` could itself
#: meet the cap. A Node carries `status.images`, every image the kubelet holds,
#: and runs to tens of kilobytes; 100 of them is a few megabytes.
PAGE_SIZE_BY_RESOURCE: dict[str, int] = {"nodes": 100}
#: A ceiling on pages per kind, so a server that keeps answering with a
#: continue token cannot hold a collector forever: this many pages of
#: `PAGE_SIZE` is five million objects of one kind.
MAX_PAGES = 10000
KUBECTL_RAW_FLAG = "--raw"
LIMIT_KEY = "limit"
CONTINUE_KEY = "continue"
ITEMS_KEY = "items"
METADATA_KEY = "metadata"
MANAGED_FIELDS_KEY = "managedFields"
KIND_KEY = "kind"
API_VERSION_KEY = "apiVersion"
#: The envelope a merged paged read is returned in: kubectl's own `List`, the
#: shape every collector's gate and parser already reads.
LIST_KIND = "List"
LIST_API_VERSION = "v1"
#: The stderr a paged read fails with when a page is not a list, or the server
#: keeps handing back the same continue token.
PAGE_NOT_A_LIST_TEXT = "paged read: the page has no `items` list"
PAGE_LOOP_TEXT = "paged read: the server repeated a continue token"
PAGE_CEILING_TEXT = f"paged read: more than {MAX_PAGES} pages of one kind"
#: What a merged read records as its output: the counts and one digest over
#: every page as it arrived, so the manifest's `output_sha256` still changes
#: when the content does, without the merged text being kept to digest.
PAGED_SUMMARY_FORMAT = "{items} items in {pages} pages; sha256 {digest}"


class ListKind(NamedTuple):
    """One collection a paged read may page: its cluster-wide path, and the
    `apiVersion` and `kind` the raw list's items are stamped with."""

    path: str
    api_version: str
    kind: str


#: Every collection the collectors page, by canonical resource name. The paths
#: are the policy's allowlist (`command_policy.KUBECTL_RAW_COLLECTIONS`); a kind
#: added here is added there in the same change, and `secrets` goes in neither.
LIST_KINDS: dict[str, ListKind] = {
    "events": ListKind("/api/v1/events", "v1", "Event"),
    "limitranges": ListKind("/api/v1/limitranges", "v1", "LimitRange"),
    "namespaces": ListKind("/api/v1/namespaces", "v1", "Namespace"),
    "nodes": ListKind("/api/v1/nodes", "v1", "Node"),
    "persistentvolumeclaims": ListKind("/api/v1/persistentvolumeclaims", "v1", "PersistentVolumeClaim"),
    "persistentvolumes": ListKind("/api/v1/persistentvolumes", "v1", "PersistentVolume"),
    "pods": ListKind("/api/v1/pods", "v1", "Pod"),
    "resourcequotas": ListKind("/api/v1/resourcequotas", "v1", "ResourceQuota"),
    "serviceaccounts": ListKind("/api/v1/serviceaccounts", "v1", "ServiceAccount"),
    "services": ListKind("/api/v1/services", "v1", "Service"),
    "mutatingwebhookconfigurations": ListKind(
        "/apis/admissionregistration.k8s.io/v1/mutatingwebhookconfigurations",
        "admissionregistration.k8s.io/v1", "MutatingWebhookConfiguration",
    ),
    "validatingwebhookconfigurations": ListKind(
        "/apis/admissionregistration.k8s.io/v1/validatingwebhookconfigurations",
        "admissionregistration.k8s.io/v1", "ValidatingWebhookConfiguration",
    ),
    "daemonsets": ListKind("/apis/apps/v1/daemonsets", "apps/v1", "DaemonSet"),
    "deployments": ListKind("/apis/apps/v1/deployments", "apps/v1", "Deployment"),
    "replicasets": ListKind("/apis/apps/v1/replicasets", "apps/v1", "ReplicaSet"),
    "statefulsets": ListKind("/apis/apps/v1/statefulsets", "apps/v1", "StatefulSet"),
    "horizontalpodautoscalers": ListKind(
        "/apis/autoscaling/v2/horizontalpodautoscalers", "autoscaling/v2", "HorizontalPodAutoscaler"
    ),
    "cronjobs": ListKind("/apis/batch/v1/cronjobs", "batch/v1", "CronJob"),
    "jobs": ListKind("/apis/batch/v1/jobs", "batch/v1", "Job"),
    "endpointslices": ListKind("/apis/discovery.k8s.io/v1/endpointslices", "discovery.k8s.io/v1", "EndpointSlice"),
    # No collector pages these; they are here because the policy admits them
    # for the capacity-obtainability skill and the two tables are one set.
    "nodes.metrics.k8s.io": ListKind("/apis/metrics.k8s.io/v1beta1/nodes", "metrics.k8s.io/v1beta1", "NodeMetrics"),
    "pods.metrics.k8s.io": ListKind("/apis/metrics.k8s.io/v1beta1/pods", "metrics.k8s.io/v1beta1", "PodMetrics"),
    "ingresses": ListKind("/apis/networking.k8s.io/v1/ingresses", "networking.k8s.io/v1", "Ingress"),
    "networkpolicies": ListKind("/apis/networking.k8s.io/v1/networkpolicies", "networking.k8s.io/v1", "NetworkPolicy"),
    "poddisruptionbudgets": ListKind("/apis/policy/v1/poddisruptionbudgets", "policy/v1", "PodDisruptionBudget"),
    "clusterrolebindings": ListKind(
        "/apis/rbac.authorization.k8s.io/v1/clusterrolebindings", "rbac.authorization.k8s.io/v1", "ClusterRoleBinding"
    ),
    "clusterroles": ListKind("/apis/rbac.authorization.k8s.io/v1/clusterroles", "rbac.authorization.k8s.io/v1", "ClusterRole"),
    "rolebindings": ListKind("/apis/rbac.authorization.k8s.io/v1/rolebindings", "rbac.authorization.k8s.io/v1", "RoleBinding"),
    "roles": ListKind("/apis/rbac.authorization.k8s.io/v1/roles", "rbac.authorization.k8s.io/v1", "Role"),
    "storageclasses": ListKind("/apis/storage.k8s.io/v1/storageclasses", "storage.k8s.io/v1", "StorageClass"),
}
#: The short and singular spellings the collectors' kind lists use, so a list
#: such as `"deploy,sts,ds,cronjob,pod"` names the same collections kubectl
#: resolves it to.
RESOURCE_ALIASES: dict[str, str] = {
    "cronjob": "cronjobs", "cj": "cronjobs",
    "daemonset": "daemonsets", "ds": "daemonsets",
    "deployment": "deployments", "deploy": "deployments",
    "endpointslice": "endpointslices",
    "event": "events", "ev": "events",
    "hpa": "horizontalpodautoscalers", "horizontalpodautoscaler": "horizontalpodautoscalers",
    "ingress": "ingresses", "ing": "ingresses",
    "job": "jobs",
    "limitrange": "limitranges", "limits": "limitranges",
    "mutatingwebhookconfiguration": "mutatingwebhookconfigurations",
    "namespace": "namespaces", "ns": "namespaces",
    "netpol": "networkpolicies", "networkpolicy": "networkpolicies",
    "node": "nodes", "no": "nodes",
    "pdb": "poddisruptionbudgets", "poddisruptionbudget": "poddisruptionbudgets",
    "persistentvolume": "persistentvolumes", "pv": "persistentvolumes",
    "persistentvolumeclaim": "persistentvolumeclaims", "pvc": "persistentvolumeclaims",
    "pod": "pods", "po": "pods",
    "replicaset": "replicasets", "rs": "replicasets",
    "resourcequota": "resourcequotas", "quota": "resourcequotas",
    "role": "roles", "rolebinding": "rolebindings",
    "clusterrole": "clusterroles", "clusterrolebinding": "clusterrolebindings",
    "serviceaccount": "serviceaccounts", "sa": "serviceaccounts",
    "service": "services", "svc": "services",
    "statefulset": "statefulsets", "sts": "statefulsets",
    "storageclass": "storageclasses", "sc": "storageclasses",
    "validatingwebhookconfiguration": "validatingwebhookconfigurations",
}


class Run(NamedTuple):
    """One subprocess's outcome, in the shape the manifest records it."""

    argv: list[str]
    rc: int
    stdout: str
    stderr: str
    duration_s: float


RunFn = Callable[..., Run]


class Discovery(NamedTuple):
    """The projects a collector sweeps, and how complete that list is."""

    projects: list[str]
    # Set when no project could be resolved at all: an empty fleet is then a
    # failure to look, and the manifest says so with a top-level `error`.
    error: str | None
    # Set when discovery resolved part of the fleet, or `--project` narrowed
    # it; the collector turns it into a row the document must account for.
    partial: str | None = None


def _text(output: str | bytes | None) -> str:
    # `TimeoutExpired` carries the child's output as bytes even under `text=True`,
    # and every reader of `Run` matches markers against a str.
    if isinstance(output, bytes):
        return output.decode(errors="replace")
    return output or ""


def shim_retry_deadline(timeout: int) -> int:
    """The retry deadline a child capped at `timeout` is told: the cap less the broker's
    `gcloud` run bound, and never negative. Under every cap a collector binds it is 0."""
    return max(0, int(timeout) - BROKER_GCLOUD_RUN_S)


def child_env(env: dict | None, timeout: int) -> dict:
    """The environment the child runs in: the caller's, or this process's, with the shim's
    retry deadline derived from the cap when the caller named none."""
    child = dict(os.environ if env is None else env)
    child.setdefault(SHIM_RETRY_DEADLINE_ENV, str(shim_retry_deadline(timeout)))
    return child


def default_run(argv: list[str], *, env: dict | None = None, timeout: int) -> Run:
    """The real subprocess call. Every driver takes `run` as a parameter and the
    tests inject a fake in its place, so nothing above this needs a live cluster."""
    t0 = time.monotonic()
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, env=child_env(env, timeout), timeout=timeout)
        return Run(argv, proc.returncode, proc.stdout, proc.stderr, time.monotonic() - t0)
    except subprocess.TimeoutExpired as exc:
        return Run(argv, TIMEOUT_RC, _text(exc.stdout), _text(exc.stderr), time.monotonic() - t0)
    except Exception as exc:  # a missing binary, a bad env
        return Run(argv, -1, "", str(exc), time.monotonic() - t0)


def canonical_resource(name: str) -> str:
    """The `LIST_KINDS` key for a kind as a collector spells it, or ValueError
    for one no paged read may page: a kind outside the table is a kind the
    policy refuses. The collectors' kind lists are constants, and
    `test_collector_run` resolves every one of them, so a misspelt kind fails
    there rather than inside a sweep."""
    resource = RESOURCE_ALIASES.get(name, name)
    if resource not in LIST_KINDS:
        raise ValueError(f"{name!r} is not a collection a paged read may page")
    return resource


def list_argv(
    resource: str, *, limit: int | None = None, continue_token: str = "", query: dict[str, str] | None = None
) -> list[str]:
    """The `kubectl get --raw` argv for one page of `resource`: the collection
    path with `limit`, the extra `query` (a field or label selector), and the
    `continue` token when one is being followed, percent-encoded."""
    canonical = canonical_resource(resource)
    params: dict[str, str] = {LIMIT_KEY: str(limit or PAGE_SIZE_BY_RESOURCE.get(canonical, PAGE_SIZE))}
    params.update(query or {})
    if continue_token:
        params[CONTINUE_KEY] = continue_token
    return ["kubectl", "get", KUBECTL_RAW_FLAG, f"{LIST_KINDS[canonical].path}?{urllib.parse.urlencode(params)}"]


def paged_command_text(resources: Iterable[str], *, query: dict[str, str] | None = None, kubeconfig=None) -> str:
    """The shell line a paged read is recorded as: each kind's first page, with
    the kubeconfig the reads ran under spelled out; a reader follows `continue`."""
    lines = "; ".join(shlex.join(list_argv(resource, query=query)) for resource in resources)
    return f"KUBECONFIG={shlex.quote(str(kubeconfig))} {lines}" if kubeconfig else lines


def _strip_and_stamp(items: list, listed: ListKind) -> None:
    for item in items:
        if not isinstance(item, dict):
            continue
        metadata = item.get(METADATA_KEY)
        if isinstance(metadata, dict):
            metadata.pop(MANAGED_FIELDS_KEY, None)
        item.setdefault(KIND_KEY, listed.kind)
        item.setdefault(API_VERSION_KEY, listed.api_version)


def _failed(result: Run, reason: str) -> Run:
    return result._replace(stdout="", stderr=(result.stderr.rstrip() + "\n" + reason).strip())


def paged_list(
    resource: str,
    *,
    run: RunFn,
    query: dict[str, str] | None = None,
    page_size: int | None = None,
    digest: "hashlib._Hash | None" = None,
    **run_kwargs,
) -> tuple[list | None, Run, int]:
    """Every object of one kind, read `page_size` at a time, with each page's
    `metadata.managedFields` dropped as it is decoded and each item stamped
    with its kind. `(items, run, pages)` on success; `(None, run, pages)` with
    the failing page's `Run` when any page failed, printed nothing, printed
    something that is not a list, or the server looped on a token: a partial
    kind is never returned as a shorter one. `run_kwargs` (`env`, `timeout`)
    reach every page's `run` as the caller's `run_and_gate` passes them."""
    listed = LIST_KINDS[canonical_resource(resource)]
    items: list = []
    token = ""
    seen_tokens: set[str] = set()
    elapsed = 0.0
    pages = 0
    first_argv: list[str] | None = None
    while True:
        argv = list_argv(resource, limit=page_size, continue_token=token, query=query)
        first_argv = first_argv or argv
        result = run(argv, **run_kwargs)
        elapsed += result.duration_s
        pages += 1
        if result.rc != 0 or not result.stdout.strip():
            return None, result._replace(duration_s=elapsed), pages
        try:
            page = json.loads(result.stdout)
        except json.JSONDecodeError:
            return None, result._replace(duration_s=elapsed), pages
        if not isinstance(page, dict) or not isinstance(page.get(ITEMS_KEY), list):
            return None, _failed(result._replace(duration_s=elapsed), PAGE_NOT_A_LIST_TEXT), pages
        if digest is not None:
            digest.update(result.stdout.encode("utf-8"))
        page_items = page[ITEMS_KEY]
        _strip_and_stamp(page_items, listed)
        items.extend(page_items)
        metadata = page.get(METADATA_KEY)
        token = (metadata.get(CONTINUE_KEY) if isinstance(metadata, dict) else "") or ""
        del page, page_items, result
        if not token:
            break
        if token in seen_tokens:
            return None, Run(argv, 0, "", PAGE_LOOP_TEXT, elapsed), pages
        if pages >= MAX_PAGES:
            return None, Run(argv, 0, "", PAGE_CEILING_TEXT, elapsed), pages
        seen_tokens.add(token)
    return items, Run(first_argv, 0, "", "", elapsed), pages


def paged_dump(
    resources: Iterable[str], *, run: RunFn, query: dict[str, str] | None = None, **run_kwargs
) -> tuple[dict | None, Run]:
    """The collectors' multi-kind dump, read one kind at a time in pages and
    merged into kubectl's `List` shape, behind the same fail-closed gate as
    `run_and_gate`: `(None, run)` with the failing page's `Run` when any kind
    failed. On success the `Run` is the first page's argv, rc 0, and a
    summary line (`PAGED_SUMMARY_FORMAT`) as its stdout, so a manifest record
    built from it still digests the content that was read."""
    merged: list = []
    digest = hashlib.sha256()
    elapsed = 0.0
    pages = 0
    first: Run | None = None
    for resource in resources:
        items, result, kind_pages = paged_list(resource, run=run, query=query, digest=digest, **run_kwargs)
        elapsed += result.duration_s
        pages += kind_pages
        if items is None:
            return None, result._replace(duration_s=elapsed)
        first = first or result
        merged.extend(items)
    if first is None:
        raise ValueError("paged_dump needs at least one resource")
    summary = PAGED_SUMMARY_FORMAT.format(items=len(merged), pages=pages, digest=digest.hexdigest())
    return (
        {API_VERSION_KEY: LIST_API_VERSION, KIND_KEY: LIST_KIND, ITEMS_KEY: merged},
        Run(first.argv, 0, summary, "", elapsed),
    )


def run_and_gate(argv: list[str], *, run: RunFn, env: dict | None = None) -> tuple[object | None, Run]:
    """One JSON-producing command behind a fail-closed gate: `(None, run)` when
    the command failed, printed nothing, or printed something that is not JSON,
    so a caller never reads a truncated or empty answer as "nothing here"."""
    result = run(argv) if env is None else run(argv, env=env)
    if result.rc != 0 or not result.stdout.strip():
        return None, result
    try:
        return json.loads(result.stdout), result
    except json.JSONDecodeError:
        return None, result
