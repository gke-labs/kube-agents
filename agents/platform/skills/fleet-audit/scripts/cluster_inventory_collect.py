#!/usr/bin/env python3
"""cluster_inventory_collect.py: the single-cluster inventory audit as one command.

The onboarding sweep's Step 3 (`bootstrap_scan_gate.py` task body,
`governance/inventory.md`) has the sweep's own worker audit every cluster the
Cluster Agent cards do not cover: the clusters with no Cluster Agent and, since
the first-run card cap (#3702), the clusters past it. Walking Steps 2 to 4 of
`cluster_inventory_audit_sop.md` by hand costs the worker twenty to forty tool
calls per cluster, and three such walks do not fit the platform profile's
kanban worker budget; the sweep then completes with one cluster recorded, or
runs past the hand-off's wait (#4115). This script reads the same facts in
three reads and prints the SOP's Step 5 ``metadata`` object, which the
worker pastes into the card's ``clusters`` list unchanged.

Three reads: ``gcloud container clusters describe`` for the topology, through
the credential proxy's gcloud shim; a paged workload dump (Deployments,
StatefulSets, DaemonSets, Jobs) and a paged dump of namespaces with their
ResourceQuotas and LimitRanges, as ``kubectl get --raw`` pages through the
kube-proxy kubeconfig the sandbox's ``get-credentials`` writes (#3760), which
needs the sandbox image's kubectl 1.34 or later (#4739). Nine reads plus one
page per 500 objects of a kind, each up to 60 s, under a run-wide budget of
READ_BUDGET_SECONDS, so the script returns inside the terminal's 600 s cap. A
read that fails, or finds no budget left, is a ``gaps`` entry, never a guess:
the unread fields are null, and the object still names its project and
cluster, so the hand-off lists the cluster with its gap rather than losing it.

Findings carry the SOP's own slugs and nothing the first-run audits own.
Workloads in provider namespaces (kube-system and the ``gke-``/``gmp-``
families) are listed in ``workloads`` but raise no finding: the hand-off
marks a finding there ``provider_managed`` and the ranking stage scores it
apart, and the by-hand audit this replaces reports on the user's workloads.

    cluster_inventory_collect.py --project P --location L --cluster C [--out FILE]
"""
from __future__ import annotations

import argparse
import functools
import json
import shlex
import sys
import time
from pathlib import Path

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.append(str(Path(__file__).resolve().parent))
import collect  # noqa: E402
import collector_run  # noqa: E402

# The SOP's check slugs (cluster_inventory_audit_sop.md, Step 5).
CHECK_PROBES_STARTUP = "probes-startup"
CHECK_READONLY_ROOT_FS = "readonly-root-fs"
CHECK_ADDED_CAPABILITIES = "added-capabilities"
CHECK_NO_RESOURCEQUOTA = "no-resourcequota"
CHECK_SHIELDED_NODES = "shielded-nodes"
CHECK_DATAPATH_PROVIDER = "datapath-provider"
CHECK_MANAGED_PROMETHEUS = "managed-prometheus"
CHECK_NO_AUTOUPGRADE = "no-autoupgrade"
CHECK_STATIC_SA_KEY = "static-sa-key"
# Step 4.1's "no collector is reachable at all"; the SOP's table gives it no slug.
CHECK_NO_OTEL_COLLECTOR = "no-otel-collector"

AREA_SECURITY = "security"
AREA_RELIABILITY = "reliability"
AREA_OBSERVABILITY = "observability"
SEVERITY_HIGH = "high"
SEVERITY_MEDIUM = "medium"
SEVERITY_LOW = "low"

# An added SYS_ADMIN counts as a privileged container, which the first-run
# audits report; the SOP keeps it out of `added-capabilities`.
PRIVILEGED_CAPABILITY = "SYS_ADMIN"

# The kinds Step 3 lists, as the paged reader names them.
WORKLOAD_RESOURCES = "deployments,statefulsets,daemonsets,jobs"
GOVERNANCE_RESOURCES = "namespaces,resourcequotas,limitranges"
KIND_NAMESPACE = "Namespace"
KIND_RESOURCE_QUOTA = "ResourceQuota"
KIND_LIMIT_RANGE = "LimitRange"
KIND_DEPLOYMENT = "Deployment"
KIND_STATEFULSET = "StatefulSet"
KIND_DAEMONSET = "DaemonSet"
KIND_JOB = "Job"
WORKLOAD_KINDS = (KIND_DEPLOYMENT, KIND_STATEFULSET, KIND_DAEMONSET, KIND_JOB)

# bootstrap_handoff.PROVIDER_NAMESPACES and PROVIDER_NAMESPACE_PREFIXES: the
# namespaces the platform provider runs. Repeated here because that module
# lives in the agent pod and this script runs in the shell sandbox.
PROVIDER_NAMESPACES = ("kube-system", "kube-public", "kube-node-lease")
PROVIDER_NAMESPACE_PREFIXES = ("gke-", "gmp-")

# GKE API values the topology reads.
DATAPATH_V2 = "ADVANCED_DATAPATH"
# Namespaces and names that identify an OpenTelemetry collector (Step 4.1).
OTEL_MANAGED_NAMESPACE = "gke-managed-otel"
OTEL_NAME_WORDS = ("otel-collector", "opentelemetry")
OBSERVABILITY_MANAGED = "gke-managed-otel"
OBSERVABILITY_SELF_HOSTED = "self-hosted"
OBSERVABILITY_NONE = "none"
# Step 3.3's deployment toolchains, by the namespace each one installs into,
# in the order the first match wins.
TOOLCHAIN_NAMESPACES = (
    ("config-management-system", "Config Sync"),
    ("argocd", "Argo CD"),
    ("flux-system", "Flux"),
)
HELM_MANAGED_BY = "Helm"
MANAGED_BY_LABEL = "app.kubernetes.io/managed-by"
HELM_RELEASE_ANNOTATION = "meta.helm.sh/release-name"
TOOLCHAIN_HELM = "Helm releases"
TOOLCHAIN_NONE = "none observed"
# How a workload shows it exports telemetry (Step 5's `telemetry` values).
TELEMETRY_OTLP = "otlp"
TELEMETRY_PROMETHEUS = "prometheus-scrape"
TELEMETRY_NONE = "none"
OTLP_ENV_PREFIX = "OTEL_EXPORTER_OTLP"
PROMETHEUS_SCRAPE_ANNOTATION = "prometheus.io/scrape"
# The environment variable a static service-account key file is handed in by.
STATIC_KEY_ENV = "GOOGLE_APPLICATION_CREDENTIALS"
AUTOSCALING_OFF = "off"
AUTOSCALING_FORMAT = "{min}-{max}"
# The SOP step each read answers, named in its gap.
STEP_TOPOLOGY = "Step 2"
STEP_WORKLOADS = "Step 3"
TRUE_WORD = "true"
# What a cluster description that is not a JSON object is reported as.
DESCRIBE_NOT_AN_OBJECT = "the cluster description was not a JSON object"
# How much of a failed command's stderr a gap quotes.
GAP_STDERR_CHARS = 200
# The whole run's read budget: the terminal that runs this script is capped at
# TERMINAL_TIMEOUT_SECONDS (agents/platform/AGENTS.md, the foreground maximum),
# and nine reads plus one page per 500 objects of a kind, each up to
# collect.DEFAULT_TIMEOUT_S, can pass it on a large, slow cluster. Every read
# gets the time left under this budget, and a read that finds none left is a
# gap, so the script always returns before the terminal gives up on it.
TERMINAL_TIMEOUT_SECONDS = 600
READ_BUDGET_SECONDS = 480
BUDGET_EXHAUSTED = "the read budget of {budget} s was spent before this read, so it did not run"
EXIT_OK = 0

Run = collector_run.Run
RunFn = collect.RunFn
default_run = functools.partial(collector_run.default_run, timeout=collect.DEFAULT_TIMEOUT_S)
failed_page_text = collector_run.failed_page_text


def _describe_argv(project: str, location: str, cluster: str) -> list[str]:
    return [
        "gcloud", "container", "clusters", "describe", cluster,
        "--location", location, "--project", project, "--format", "json",
    ]


def _gap(step: str, argv: list[str], result: Run) -> str:
    text = result.stderr.strip() or result.stdout.strip()
    return f"{step}: `{failed_page_text(argv)}` failed (rc={result.rc}): {text[:GAP_STDERR_CHARS]}"


def _provider_managed(namespace: str) -> bool:
    return namespace in PROVIDER_NAMESPACES or namespace.startswith(PROVIDER_NAMESPACE_PREFIXES)


def _items(dump: dict | None, kind: str) -> list[dict]:
    return [i for i in (dump or {}).get("items", []) or [] if isinstance(i, dict) and i.get("kind") == kind]


def _containers(workload: dict) -> list[dict]:
    template = ((workload.get("spec") or {}).get("template") or {}).get("spec") or {}
    return [c for c in (template.get("containers") or []) if isinstance(c, dict)]


def _pod_annotations(workload: dict) -> dict:
    return (((workload.get("spec") or {}).get("template") or {}).get("metadata") or {}).get("annotations") or {}


def _replicas(workload: dict) -> str:
    spec, status = workload.get("spec") or {}, workload.get("status") or {}
    kind = workload.get("kind")
    if kind == KIND_DAEMONSET:
        return f"{status.get('numberReady') or 0}/{status.get('desiredNumberScheduled') or 0}"
    if kind == KIND_JOB:
        return f"{status.get('succeeded') or 0}/{spec.get('completions') or 1}"
    return f"{status.get('readyReplicas') or 0}/{spec.get('replicas') if spec.get('replicas') is not None else 1}"


def _telemetry(workload: dict) -> str:
    for c in _containers(workload):
        for env in c.get("env") or []:
            if isinstance(env, dict) and str(env.get("name", "")).startswith(OTLP_ENV_PREFIX):
                return TELEMETRY_OTLP
    if str(_pod_annotations(workload).get(PROMETHEUS_SCRAPE_ANNOTATION, "")).lower() == TRUE_WORD:
        return TELEMETRY_PROMETHEUS
    return TELEMETRY_NONE


def _added_capabilities(workload: dict) -> list[str]:
    added: list[str] = []
    for c in _containers(workload):
        for cap in ((c.get("securityContext") or {}).get("capabilities") or {}).get("add") or []:
            name = str(cap).upper()
            if name not in added:
                added.append(name)
    return added


def _uses_static_key(workload: dict) -> bool:
    return any(
        isinstance(env, dict) and env.get("name") == STATIC_KEY_ENV
        for c in _containers(workload)
        for env in c.get("env") or []
    )


def workload_entry(workload: dict) -> dict:
    """One Step 5 ``workloads`` entry from a workload object."""
    containers = _containers(workload)
    meta = workload.get("metadata") or {}
    return {
        "namespace": meta.get("namespace", ""),
        "name": meta.get("name", ""),
        "kind": workload.get("kind", ""),
        "replicas": _replicas(workload),
        "probes": {"startup": bool(containers) and all(c.get("startupProbe") for c in containers)},
        "telemetry": _telemetry(workload),
        "security_context": {
            "read_only_root_fs": bool(containers)
            and all((c.get("securityContext") or {}).get("readOnlyRootFilesystem") is True for c in containers),
            "added_capabilities": _added_capabilities(workload),
        },
    }


def _finding(check: str, severity: str, area: str, namespace: str, workload: str, issue: str, rec: str) -> dict:
    return {
        "check": check, "severity": severity, "area": area, "namespace": namespace,
        "workload": workload, "issue": issue, "recommendation": rec,
    }


def workload_findings(entry: dict) -> list[dict]:
    """The SOP's per-workload findings for one ``workloads`` entry."""
    ns, name = entry["namespace"], entry["name"]
    if _provider_managed(ns):
        return []
    out = []
    label = f"{entry['kind']} {ns}/{name}"
    if not entry["probes"]["startup"]:
        out.append(_finding(
            CHECK_PROBES_STARTUP, SEVERITY_MEDIUM, AREA_RELIABILITY, ns, name,
            f"{label} has a container with no startupProbe",
            "Add a startupProbe so a slow start is not killed by the liveness probe",
        ))
    sc = entry["security_context"]
    if not sc["read_only_root_fs"]:
        out.append(_finding(
            CHECK_READONLY_ROOT_FS, SEVERITY_MEDIUM, AREA_SECURITY, ns, name,
            f"{label} has a container without readOnlyRootFilesystem: true",
            "Set securityContext.readOnlyRootFilesystem: true and mount writable paths as volumes",
        ))
    added = [c for c in sc["added_capabilities"] if c != PRIVILEGED_CAPABILITY]
    if added:
        out.append(_finding(
            CHECK_ADDED_CAPABILITIES, SEVERITY_HIGH, AREA_SECURITY, ns, name,
            f"{label} adds Linux capabilities {', '.join(added)}",
            "Drop the added capabilities or move the work to a component that needs them",
        ))
    return out


def static_key_findings(workloads: list[dict]) -> list[dict]:
    out = []
    for w in workloads:
        meta = w.get("metadata") or {}
        ns = meta.get("namespace", "")
        if _provider_managed(ns) or not _uses_static_key(w):
            continue
        out.append(_finding(
            CHECK_STATIC_SA_KEY, SEVERITY_HIGH, AREA_SECURITY, ns, meta.get("name", ""),
            f"{w.get('kind')} {ns}/{meta.get('name', '')} reaches Google Cloud APIs with a static key file ({STATIC_KEY_ENV})",
            "Use Workload Identity: annotate the Kubernetes ServiceAccount with iam.gke.io/gcp-service-account",
        ))
    return out


def namespace_governance(dump: dict | None) -> tuple[list[dict], list[dict]]:
    """Step 3.1: one row per non-provider namespace, and a finding for each without a quota or limit range."""
    quotas = {(q.get("metadata") or {}).get("namespace") for q in _items(dump, KIND_RESOURCE_QUOTA)}
    limits = {(lr.get("metadata") or {}).get("namespace") for lr in _items(dump, KIND_LIMIT_RANGE)}
    rows, findings = [], []
    for ns_obj in _items(dump, KIND_NAMESPACE):
        ns = (ns_obj.get("metadata") or {}).get("name", "")
        if not ns or _provider_managed(ns):
            continue
        row = {"namespace": ns, "resource_quota": ns in quotas, "limit_range": ns in limits}
        rows.append(row)
        if not (row["resource_quota"] and row["limit_range"]):
            missing = [n for n, present in ((KIND_RESOURCE_QUOTA, row["resource_quota"]), (KIND_LIMIT_RANGE, row["limit_range"])) if not present]
            findings.append(_finding(
                CHECK_NO_RESOURCEQUOTA, SEVERITY_LOW, AREA_RELIABILITY, ns, "",
                f"namespace {ns} has no {' or '.join(missing)}",
                "Add a ResourceQuota and a LimitRange so one namespace cannot take the cluster's capacity",
            ))
    return rows, findings


def _observability(workloads: list[dict] | None) -> str | None:
    """Step 4.1's verdict from the workload dump, or None when the dump was not read."""
    if workloads is None:
        return None
    for w in workloads:
        meta = w.get("metadata") or {}
        if meta.get("namespace") == OTEL_MANAGED_NAMESPACE:
            return OBSERVABILITY_MANAGED
    for w in workloads:
        name = str((w.get("metadata") or {}).get("name", "")).lower()
        if any(word in name for word in OTEL_NAME_WORDS):
            return OBSERVABILITY_SELF_HOSTED
    return OBSERVABILITY_NONE


def _deployment_toolchain(namespaces: list[str] | None, workloads: list[dict] | None) -> str | None:
    """Step 3.3's toolchain column: a toolchain's namespace when the namespace dump
    was read and names one, else what the workloads say, else None when the
    workload dump was not read ("none observed" is a verdict only on a dump that was)."""
    for ns, toolchain in TOOLCHAIN_NAMESPACES:
        if ns in (namespaces or []):
            return toolchain
    if workloads is None:
        return None
    for w in workloads:
        meta = w.get("metadata") or {}
        managed_by = str((meta.get("labels") or {}).get(MANAGED_BY_LABEL, "")).lower()
        if managed_by == HELM_MANAGED_BY.lower() or HELM_RELEASE_ANNOTATION in (meta.get("annotations") or {}):
            return TOOLCHAIN_HELM
    return TOOLCHAIN_NONE


def _node_pools(describe: dict) -> list[dict]:
    pools = []
    for pool in describe.get("nodePools") or []:
        if not isinstance(pool, dict):
            continue
        scaling = pool.get("autoscaling") or {}
        pools.append({
            "name": pool.get("name", ""),
            "machine_type": (pool.get("config") or {}).get("machineType", ""),
            "nodes": pool.get("initialNodeCount") or 0,
            "autoscaling": (
                AUTOSCALING_FORMAT.format(min=scaling.get("minNodeCount") or 0, max=scaling.get("maxNodeCount") or 0)
                if scaling.get("enabled") else AUTOSCALING_OFF
            ),
        })
    return pools


def topology(describe: dict | None, namespaces: list[str] | None, workloads: list[dict] | None) -> dict:
    """Step 2 and the Step 3.3 toolchain column, from the cluster description and the dumps.

    A field whose read failed is None, which the report renders as n/a; a
    False here would state a setting as off that was never read.
    """
    if describe is None:
        version, pools, identity, dataplane = None, [], None, None
    else:
        version = describe.get("currentMasterVersion", "")
        pools = _node_pools(describe)
        identity = bool((describe.get("workloadIdentityConfig") or {}).get("workloadPool"))
        dataplane = (describe.get("networkConfig") or {}).get("datapathProvider") == DATAPATH_V2
    return {
        "k8s_version": version,
        "node_pools": pools,
        "workload_identity": identity,
        "dataplane_v2": dataplane,
        "observability": _observability(workloads),
        "deployment_toolchain": _deployment_toolchain(namespaces, workloads),
    }


def cluster_findings(describe: dict, topo: dict) -> list[dict]:
    """Step 4's cluster-level checks, with the SOP's slugs. Standard-only checks skip Autopilot."""
    out = []
    autopilot = bool((describe.get("autopilot") or {}).get("enabled"))
    if not autopilot and not (describe.get("shieldedNodes") or {}).get("enabled"):
        out.append(_finding(
            CHECK_SHIELDED_NODES, SEVERITY_MEDIUM, AREA_SECURITY, "", "",
            "Shielded GKE Nodes are not enabled", "Enable Shielded GKE Nodes on the cluster",
        ))
    if topo["dataplane_v2"] is False:
        out.append(_finding(
            CHECK_DATAPATH_PROVIDER, SEVERITY_LOW, AREA_SECURITY, "", "",
            "The cluster does not use Dataplane V2", "Migrate to Dataplane V2 for eBPF networking and NetworkPolicy logging",
        ))
    monitoring = describe.get("monitoringConfig") or {}
    if not (monitoring.get("managedPrometheusConfig") or {}).get("enabled"):
        out.append(_finding(
            CHECK_MANAGED_PROMETHEUS, SEVERITY_LOW, AREA_OBSERVABILITY, "", "",
            "Managed Service for Prometheus is not enabled", "Enable Managed Service for Prometheus on the cluster",
        ))
    if topo["observability"] == OBSERVABILITY_NONE:
        out.append(_finding(
            CHECK_NO_OTEL_COLLECTOR, SEVERITY_LOW, AREA_OBSERVABILITY, "", "",
            "No OpenTelemetry collector is deployed", "Enable GKE managed OTLP collection or deploy a collector",
        ))
    if not autopilot:
        for pool in describe.get("nodePools") or []:
            if isinstance(pool, dict) and not ((pool.get("management") or {}).get("autoUpgrade")):
                out.append(_finding(
                    CHECK_NO_AUTOUPGRADE, SEVERITY_MEDIUM, AREA_SECURITY, "", "",
                    f"node pool {pool.get('name', '')} has node auto-upgrade off", "Enable auto-upgrade on the node pool",
                ))
    return out


def build_metadata(
    project: str, location: str, cluster: str,
    describe: dict | None, workload_dump: dict | None, governance_dump: dict | None, gaps: list[str],
) -> dict:
    """The SOP's Step 5 object. ``None`` for a read that failed; its gap is already in ``gaps``."""
    # None for a dump that was not read: no workload, namespace or verdict is
    # derived from it, and the gap already in ``gaps`` says why.
    workloads = None if workload_dump is None else [w for kind in WORKLOAD_KINDS for w in _items(workload_dump, kind)]
    namespaces = (
        None if governance_dump is None
        else [(n.get("metadata") or {}).get("name", "") for n in _items(governance_dump, KIND_NAMESPACE)]
    )
    entries = [workload_entry(w) for w in workloads or []]
    governance, governance_findings = namespace_governance(governance_dump)
    topo = topology(describe, namespaces, workloads)
    findings: list[dict] = []
    if describe is not None:
        findings += cluster_findings(describe, topo)
    for entry in entries:
        findings += workload_findings(entry)
    findings += static_key_findings(workloads or [])
    findings += governance_findings
    return {
        "cluster": cluster,
        "location": location,
        "project": project,
        "topology": topo,
        "workloads": entries,
        "namespace_governance": governance,
        "findings": findings,
        "gaps": list(gaps),
    }


class _Budget:
    """The seconds left for the next read under READ_BUDGET_SECONDS; 0 means none."""

    def __init__(self, seconds: int = READ_BUDGET_SECONDS, clock=time.monotonic) -> None:
        self._deadline = clock() + seconds
        self._clock = clock
        self.seconds = seconds

    def left(self) -> int:
        return max(0, int(min(collect.DEFAULT_TIMEOUT_S, self._deadline - self._clock())))


def _run_within(budget: "_Budget", run: RunFn) -> RunFn:
    """``run`` with each call's timeout cut to what the budget has left."""

    def within(argv: list[str], **kwargs) -> Run:
        return run(argv, **{**kwargs, "timeout": max(1, budget.left())})

    return within


def collect_cluster_inventory(
    project: str, location: str, cluster: str, *, run: RunFn = default_run, budget: "_Budget | None" = None
) -> dict:
    budget = budget or _Budget()
    paced = _run_within(budget, run)
    gaps: list[str] = []
    describe_argv = _describe_argv(project, location, cluster)
    kubeconfig, cred = collect.fetch_credentials(project, cluster, location, run=paced)
    describe, result = collect.run_and_gate(describe_argv, kubeconfig, run=paced)
    if describe is None:
        gaps.append(_gap(STEP_TOPOLOGY, describe_argv, result))
    elif not isinstance(describe, dict):
        gaps.append(f"{STEP_TOPOLOGY}: `{shlex.join(describe_argv)}`: {DESCRIBE_NOT_AN_OBJECT}")
        describe = None
    if cred.rc != 0:
        gaps.append(_gap(STEP_WORKLOADS, cred.argv, cred))
        return build_metadata(project, location, cluster, describe, None, None, gaps)
    dumps: dict[str, dict | None] = {}
    for resources in (WORKLOAD_RESOURCES, GOVERNANCE_RESOURCES):
        if budget.left() <= 0:
            dumps[resources] = None
            gaps.append(f"{STEP_WORKLOADS}: `kubectl get {resources}`: " + BUDGET_EXHAUSTED.format(budget=budget.seconds))
            continue
        dump, result = collect.paged_and_gate(resources, kubeconfig, run=paced, timeout=max(1, budget.left()))
        dumps[resources] = dump
        if dump is None:
            gaps.append(_gap(STEP_WORKLOADS, result.argv, result))
    return build_metadata(
        project, location, cluster, describe, dumps[WORKLOAD_RESOURCES], dumps[GOVERNANCE_RESOURCES], gaps
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--project", required=True)
    parser.add_argument("--location", required=True, help="the cluster's region or zone")
    parser.add_argument("--cluster", required=True, help="the bare GKE cluster name")
    parser.add_argument("--out", help="write the metadata object here instead of stdout")
    args = parser.parse_args(argv)
    metadata = collect_cluster_inventory(args.project, args.location, args.cluster, run=default_run)
    text = json.dumps(metadata, indent=2) + "\n"
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    for gap in metadata["gaps"]:
        collect.log(f"{args.project}/{args.location}/{args.cluster}: {gap}")
    collect.log(
        f"{args.project}/{args.location}/{args.cluster}: {len(metadata['workloads'])} workload(s), "
        f"{len(metadata['findings'])} finding(s), {len(metadata['gaps'])} gap(s)"
    )
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
