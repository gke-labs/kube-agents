"""The three safety checks on a generated disruption-budget fix, for the credential broker.

Before the broker may mark a fleet-audit `no-pdb` fix as one a pipeline could
merge without a review, it runs the checks the sandbox's `pdb_safety.py` runs for
the pull request's body itself, on one namespace read with its own credential,
because nothing the sandbox reports can be trusted for that: `replicas-ready`
(with an autoscaler's floor), `no-overlapping-budget` and `selector-matches`
(Jobs and CronJobs included), behind the audit's scope rules S1-S5
(`in_scope`): a workload the audit would not read is never eligible.

`fleet-audit/scripts/test_broker_checks_parity.py` holds the verdicts and the
copied rules to the sandbox's own. Stdlib only: the credential-proxy image ships
this directory and nothing from the fleet-audit skill. Nothing calls it yet.
"""

from __future__ import annotations

import json

REPLICAS_READY = "replicas-ready"
NO_OVERLAPPING_BUDGET = "no-overlapping-budget"
SELECTOR_MATCHES = "selector-matches"
CHECK_NAMES = (REPLICAS_READY, NO_OVERLAPPING_BUDGET, SELECTOR_MATCHES)
MIN_REPLICAS = 2
#: An autoscaler with no `minReplicas` scales down to this (`collect.DEFAULT_HPA_MIN_REPLICAS`).
DEFAULT_HPA_MIN_REPLICAS = 1
#: The workloads a budget can count through their scale subresource.
BUDGET_TARGET_KINDS = ("Deployment", "StatefulSet")
#: An autoscaler's `scaleTargetRef` names one of those under this group.
WORKLOAD_API_VERSION = "apps/v1"
#: The kinds one namespace read needs: the targets, every other pod template a
#: selector could reach (Jobs and CronJobs too), the autoscalers, and the
#: budgets already there.
READ_KINDS = "deployments,statefulsets,daemonsets,cronjobs,jobs,poddisruptionbudgets,horizontalpodautoscalers"
POD_TEMPLATE_KINDS = ("Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob")
SELECTOR_KEYS = ("matchLabels", "matchExpressions")

#: The audit's scope rules S1-S5 (`collect.normalize_workloads`), copied
#: because this module may not import the fleet-audit skill; the parity test
#: compares them with `collect`'s.
SYSTEM_NAMESPACES = frozenset({
    "kube-system", "kube-public", "kube-node-lease", "gmp-system", "gmp-public", "gke-gmp-system",
    "cnrm-system", "configconnector-operator-system", "krmapihosting-system", "istio-system",
    "asm-system", "anthos-identity-service", "gatekeeper-system", "composer-system",
})
SYSTEM_NAMESPACE_PREFIXES = ("gke-", "config-management-")
ADDON_MANAGER_LABEL = "addonmanager.kubernetes.io/mode"
OPT_OUT_KEY = "kubeagents.x-k8s.io/reliability-audit"
BUILTIN_OWNER_GROUPS = frozenset({"", "apps", "batch"})
BUILTIN_OWNER_KINDS = frozenset({"Deployment", "StatefulSet", "DaemonSet", "ReplicaSet", "ReplicationController", "Job", "CronJob"})

def selector_matches(selector: dict, labels: dict) -> bool:
    """Kubernetes `LabelSelector` semantics. An operator this does not know
    matches nothing; where a match would make a check fail (an overlapping
    budget), `judge` treats such a selector as matching instead."""
    for key, value in (selector.get("matchLabels") or {}).items():
        if labels.get(key) != value:
            return False
    for expr in selector.get("matchExpressions") or []:
        if not isinstance(expr, dict):
            return False
        key, op, values = expr.get("key"), expr.get("operator"), expr.get("values") or []
        if op == "In":
            matched = labels.get(key) in values
        elif op == "NotIn":
            matched = labels.get(key) not in values
        elif op == "Exists":
            matched = key in labels
        elif op == "DoesNotExist":
            matched = key not in labels
        else:
            matched = False
        if not matched:
            return False
    return True


def _result(name: str, passed: bool, detail: str) -> dict:
    return {"name": name, "passed": passed, "detail": detail}


def unanswered(detail: str) -> list[dict]:
    """Every check failed for one reason: nothing could be judged."""
    return [_result(name, False, detail) for name in CHECK_NAMES]


def _name(item: dict) -> str:
    return str((item.get("metadata") or {}).get("name") or "")


def _pod_template_labels(item: dict) -> dict | None:
    """The labels of the pods `item` makes, or None when it makes none a
    selector here could reach: a Job a CronJob owns is its CronJob's, and a
    CronJob's template is its Job template's (`collect.pod_templates_by_namespace`)."""
    kind = item.get("kind")
    spec = item.get("spec") or {}
    if kind == "CronJob":
        spec = (spec.get("jobTemplate") or {}).get("spec") or {}
    elif kind == "Job":
        if any(ref.get("kind") == "CronJob" for ref in (item.get("metadata") or {}).get("ownerReferences") or []):
            return None
    elif kind not in POD_TEMPLATE_KINDS:
        return None
    return ((spec.get("template") or {}).get("metadata") or {}).get("labels") or {}


def _autoscaler(items: list[dict], kind: str, name: str) -> dict | None:
    """The HorizontalPodAutoscaler that owns the workload's replica count (`collect._hpa_targeting`)."""
    for item in items:
        if item.get("kind") != "HorizontalPodAutoscaler":
            continue
        target = (item.get("spec") or {}).get("scaleTargetRef") or {}
        if target.get("apiVersion") == WORKLOAD_API_VERSION and target.get("kind") == kind and target.get("name") == name:
            return item
    return None


def _readable(selector: dict) -> bool:
    """Whether every expression in `selector` is one `selector_matches` can read."""
    return all(
        isinstance(e, dict) and e.get("operator") in ("In", "NotIn", "Exists", "DoesNotExist")
        for e in selector.get("matchExpressions") or []
    )


def in_scope(item: dict) -> bool:
    """Whether the audit reads this workload at all (S1-S5): not in a system
    namespace, not addon-managed, not owned by a built-in controller, not opted
    out, not scaled to zero."""
    meta = item.get("metadata") or {}
    ns = str(meta.get("namespace") or "")
    if ns in SYSTEM_NAMESPACES or ns.startswith(SYSTEM_NAMESPACE_PREFIXES):
        return False
    labels = meta.get("labels") or {}
    annotations = meta.get("annotations") or {}
    if ADDON_MANAGER_LABEL in labels:
        return False
    for ref in meta.get("ownerReferences") or []:
        api_version = str(ref.get("apiVersion", ""))
        group = api_version.rsplit("/", 1)[0] if "/" in api_version else ""
        if str(ref.get("kind", "")) in BUILTIN_OWNER_KINDS and group in BUILTIN_OWNER_GROUPS:
            return False
    if labels.get(OPT_OUT_KEY) == "exempt" or annotations.get(OPT_OUT_KEY) == "exempt":
        return False
    return (item.get("spec") or {}).get("replicas") != 0


def judge(dump: dict, obj: str, namespace: str, budget_name: str, selector: dict) -> list[dict]:
    """The three checks for a budget named `budget_name` with `selector`, for
    `obj` (`Kind/name`) in `namespace`, against one read of that namespace."""
    kind, _, name = obj.partition("/")
    items = [i for i in dump.get("items") or [] if (i.get("metadata") or {}).get("namespace") == namespace]
    workload = next((i for i in items if i.get("kind") == kind and _name(i) == name), None)
    if workload is None or kind not in BUDGET_TARGET_KINDS:
        return unanswered(f"{obj} in {namespace} was not found")
    if not in_scope(workload):
        return unanswered(
            f"{obj} in {namespace} is outside the audit's scope (scaled to zero, opted out, "
            "owned by another controller, or in a system namespace)"
        )
    spec = workload.get("spec") or {}
    status = workload.get("status") or {}
    wanted = spec.get("replicas", 1) or 1
    # An autoscaler can take the workload down to its floor after this read,
    # so the floor is what has to reach MIN_REPLICAS. None, not falsy: a
    # scale-to-zero floor is 0.
    hpa = _autoscaler(items, kind, name)
    declared = (hpa.get("spec") or {}).get("minReplicas") if hpa else None
    floor = (DEFAULT_HPA_MIN_REPLICAS if declared is None else declared) if hpa else wanted
    ready = int(status.get("readyReplicas") or 0)
    generation = (workload.get("metadata") or {}).get("generation")
    observed = status.get("observedGeneration")
    # A status the controller has not caught up with describes the old spec.
    stale = generation is not None and (observed is None or observed < generation)
    results = [
        _result(
            REPLICAS_READY,
            min(wanted, floor) >= MIN_REPLICAS and ready >= wanted and not stale,
            f"{obj}: replicas={wanted}, ready={ready}"
            + (f", autoscaler floor={floor}" if hpa else "")
            + (f", status from generation {observed} of {generation}" if stale else ""),
        )
    ]

    labels = _pod_template_labels(workload) or {}
    budgets = [i for i in items if i.get("kind") == "PodDisruptionBudget"]
    # A null selector selects nothing in policy/v1; only `{}` selects all.
    overlapping = sorted(
        _name(p) for p in budgets
        if isinstance((p.get("spec") or {}).get("selector"), dict)
        and (not _readable(p["spec"]["selector"]) or selector_matches(p["spec"]["selector"], labels))
    )
    if overlapping:
        why = f"already selected by PodDisruptionBudget {', '.join(overlapping)}"
    elif any(_name(p) == budget_name for p in budgets):
        why = f"PodDisruptionBudget {budget_name} already exists in {namespace}, and the fix would replace it"
    else:
        why = ""
    results.append(
        _result(NO_OVERLAPPING_BUDGET, not why, why or f"no PodDisruptionBudget in {namespace} selects its pods")
    )

    own = spec.get("selector")
    others = sorted(
        f"{i.get('kind')}/{_name(i)}"
        for i in items
        if i is not workload
        and (template := _pod_template_labels(i)) is not None
        and selector_matches(selector, template)
    )
    if not any(selector.get(key) for key in SELECTOR_KEYS) or set(selector) - set(SELECTOR_KEYS):
        why = "the budget's selector is empty or has keys policy/v1 does not take"
    elif not selector_matches(selector, labels):
        why = "the budget's selector does not select the workload's pods"
    elif selector != own:
        why = f"the budget's selector differs from the workload's live selector {json.dumps(own, sort_keys=True)}"
    elif others:
        why = f"the budget's selector also selects {', '.join(others)}'s pods"
    else:
        why = ""
    results.append(
        _result(SELECTOR_MATCHES, not why, why or "the budget's selector is the workload's own and selects only its pods")
    )
    return results
