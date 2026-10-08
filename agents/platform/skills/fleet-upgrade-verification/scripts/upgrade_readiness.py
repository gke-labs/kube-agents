#!/usr/bin/env python3
"""
upgrade_readiness.py — the rules behind `fleet_upgrade_report.py --readiness`.

Pure functions over data the report script has already read: the PodDisruptionBudgets,
workloads, webhook configurations and EndpointSlices of one cluster, its
`maintenancePolicy`, and its node-pool versions. Nothing
here runs a command or touches a clock; the caller passes the instant to evaluate at. The
first three rules are the ones the governance SOPs define in prose; the fourth has no SOP
check yet and is stated here:

- a drain-blocking PDB, `obtainability_audit_sop.md` §3.4: `maxUnavailable` 0 or `0%`, or
  `minAvailable` at or above the matched workloads' replica total (an integer, or a
  percentage that rounds up to it the way the disruption controller rounds);
- a maintenance exclusion in effect whose scope covers the upgrade the target needs,
  `security_patch_orchestrator_sop.md` §3.8, and the maintenance window's state at the
  instant, §3.7;
- node-pool version skew against the target control plane, §3.2: more than two minors, or
  a different major, blocks the control-plane upgrade until the pool moves;
- a fail-closed admission webhook whose backend the API server cannot reach (no Service,
  no Service port for the webhook's port, or no ready endpoint behind that port), graded
  on its rules: one that can match a write a node drain or a node join makes
  (`UPGRADE_PATH_TARGETS` below is the list, with the package behind each write) breaks
  the upgrade; so does one whose `namespaceSelector` admits `kube-system` or `kube-public`
  and whose rules match a Role or RoleBinding write the API server's own start-up reconciles there
  (`CONTROL_PLANE_KUBE_SYSTEM_WRITES` below); one that matches none of those is a current
  outage for what it does match and is reported, not graded. The lists are the rule's
  reading of the path, not a proof of the upgrade's safety.
"""

import re
from datetime import datetime, time, timedelta, timezone

# Per-member verdicts. `blocked` when any rule blocks; `unknown` when a rule could not be
# evaluated (the cluster read failed, or there is no target to grade against) and nothing
# blocked outright; `ready` otherwise.
READINESS_READY = "ready"
READINESS_BLOCKED = "blocked"
READINESS_UNKNOWN = "unknown"
READINESS_ORDER = (READINESS_BLOCKED, READINESS_READY, READINESS_UNKNOWN)

# The workload kinds a PDB is matched against. A DaemonSet is never here: a drain deletes
# its pods rather than evicting them, so a PDB on one blocks nothing (SOP §3.3).
WORKLOAD_KINDS = ("Deployment", "StatefulSet")
PDB_KIND = "PodDisruptionBudget"
# `spec.replicas` absent means one replica, the API default.
DEFAULT_REPLICAS = 1
# matchExpressions operators, as the LabelSelectorRequirement API names them.
OP_IN = "In"
OP_NOT_IN = "NotIn"
OP_EXISTS = "Exists"
OP_DOES_NOT_EXIST = "DoesNotExist"
PERCENT_SUFFIX = "%"
PERCENT_BASE = 100
# How a finding names its PDB and its workloads.
PDB_NAME_FORMAT = "{namespace}/{name}"
WORKLOAD_FORMAT = "{kind} {namespace}/{name} ({replicas} replicas)"
FIELD_MAX_UNAVAILABLE = "maxUnavailable: {value}"
FIELD_MIN_AVAILABLE_INT = "minAvailable: {value} (>= {total} expected pods)"
FIELD_MIN_AVAILABLE_PERCENT = "minAvailable: {value} (rounds up to {healthy} of {total} expected pods)"

# Exclusion scopes, as `maintenanceExclusionOptions.scope` spells them. An exclusion with
# no options block is NO_UPGRADES, the API default.
SCOPE_NO_UPGRADES = "NO_UPGRADES"
SCOPE_NO_MINOR_UPGRADES = "NO_MINOR_UPGRADES"
SCOPE_NO_MINOR_OR_NODE_UPGRADES = "NO_MINOR_OR_NODE_UPGRADES"
DEFAULT_EXCLUSION_SCOPE = SCOPE_NO_UPGRADES
KNOWN_SCOPES = (SCOPE_NO_UPGRADES, SCOPE_NO_MINOR_UPGRADES, SCOPE_NO_MINOR_OR_NODE_UPGRADES)
# What an exclusion verdict says. An exclusion holds back GKE's automatic upgrades only;
# an operator running `gcloud container clusters upgrade` by hand is not subject to it,
# which is why the text names auto-upgrade rather than the upgrade.
EXCLUSION_BLOCKS = "blocks auto-upgrade to {target} until {end}: {why}"
EXCLUSION_NOT_APPLICABLE = "in effect until {end} but its scope does not cover this upgrade ({why})"
EXCLUSION_TARGET_UNKNOWN = "in effect until {end}; whether its scope covers the upgrade needs a target"
EXCLUSION_UNKNOWN_SCOPE = "in effect until {end} with an unrecognised scope; not evaluated"
EXCLUSION_UNPARSABLE = "start or end time unparsable; not evaluated"
WHY_ANY_UPGRADE = "the scope covers every upgrade"
WHY_MINOR_UPGRADE = "the upgrade is a minor upgrade for {components}"
WHY_NODE_UPGRADE = "pool(s) {pools} need a node upgrade"
WHY_PATCH_ONLY = "patch-only upgrade"
WHY_NOTHING_TO_UPGRADE = "no component is below the target"
CONTROL_PLANE_LABEL = "the control plane"
POOL_LABEL = "pool {name}"
COMPONENT_SEPARATOR = " and "
LIST_SEPARATOR = ", "

# Maintenance window kinds and states. `not evaluated` is a recurrence outside the two
# forms handled here; it is never a verdict either way.
WINDOW_NONE = "none"
WINDOW_DAILY = "daily"
WINDOW_RECURRING = "recurring"
WINDOW_OPEN = "open"
WINDOW_CLOSED = "closed"
WINDOW_NOT_EVALUATED = "not evaluated"
# A daily window is always four hours; the record says so in `duration` as an ISO 8601
# duration, and a record without it gets the same value.
DAILY_WINDOW_HOURS = 4
DAILY_START_FORMAT = "%H:%M"
ISO_DURATION_RE = re.compile(r"^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?)?$")
# RRULE support: `FREQ=DAILY`, and `FREQ=WEEKLY` with an optional plain `BYDAY` list; an
# `INTERVAL` other than 1, an ordinal BYDAY (`1SA`), or any other key or frequency is not
# evaluated. The two supported forms are the ones GKE's console and the SOP's remediation
# write.
RRULE_SEPARATOR = ";"
RRULE_KEY_VALUE_SEPARATOR = "="
RRULE_FREQ = "FREQ"
RRULE_BYDAY = "BYDAY"
RRULE_INTERVAL = "INTERVAL"
RRULE_DAILY = "DAILY"
RRULE_WEEKLY = "WEEKLY"
RRULE_DEFAULT_INTERVAL = "1"
BYDAY_SEPARATOR = ","
WEEKDAYS = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")
DAYS_PER_WEEK = 7
SECONDS_PER_HOUR = 3600
# How far either side of the instant occurrences are generated: one week covers every
# weekly rule, plus the window's own length for a window that started before the range.
OCCURRENCE_HORIZON = timedelta(days=DAYS_PER_WEEK)
WINDOW_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%MZ"
# RFC 3339 proper: date, `T`, time, and an offset or `Z`. `datetime.fromisoformat` alone
# also takes a bare date, the basic form and a naive time, and a bare `--at 2026-09-14`
# read as midnight would move an exclusion or window verdict by up to a day unnoticed.
RFC3339_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[Zz]|[+-]\d{2}:\d{2})$")

# Version skew, SOP §3.2: GKE keeps nodes within two minors of the control plane, so a
# pool more than two behind the target (or on another major) blocks the control-plane
# upgrade until the pool moves; exactly two is at the ceiling and worth a note.
SKEW_CEILING_MINORS = 2
SKEW_BLOCKS = "blocks"
SKEW_AT_CEILING = "at ceiling"
SKEW_OK = "ok"
SKEW_UNKNOWN = "unknown"
SKEW_NOT_APPLICABLE = "n/a"
SKEW_AUTOPILOT_REASON = "Autopilot: Google owns the node pools"
SKEW_NO_TARGET_REASON = "no target to measure against"
SKEW_MAJOR_DIFFERS = "major version differs from the target"

# Fail-closed webhooks. admissionregistration.k8s.io/v1 defaults `failurePolicy` to Fail,
# so an absent field grades as Fail. Only a webhook whose backend is a Service in the
# cluster is graded: a URL backend is outside what the read can see, so it is counted in
# the JSON (GKE installs two of its own on every cluster, so a note would say nothing).
WEBHOOK_CONFIG_KINDS = ("ValidatingWebhookConfiguration", "MutatingWebhookConfiguration")
SERVICE_KIND = "Service"
ENDPOINTSLICE_KIND = "EndpointSlice"
SERVICE_NAME_LABEL = "kubernetes.io/service-name"
FAILURE_POLICY_FAIL = "Fail"
# The API server resolves a webhook's Service the same way for either routing mode: the
# Service must exist and carry a port equal to `clientConfig.service.port` (443 when
# unset); endpoint routing then picks a ready endpoint from the EndpointSlices whose port
# carries that Service port's name. An endpoint with no `ready` condition counts as ready,
# as the EndpointSlice API says a consumer must assume.
DEFAULT_WEBHOOK_PORT = 443
BACKEND_NO_SERVICE = "Service {service} does not exist"
BACKEND_NO_PORT = "Service {service} has no port {port}"
BACKEND_NO_ENDPOINTS = "Service {service} has no ready endpoints on port {port}"
# What a node upgrade needs admitted, as (API group, resource, operation, scope): the
# writes a node drain and a node join make through the API server, each one refused by a
# fail-closed webhook whose backend is down, and none of which the drain or the join
# proceeds without. This tuple is the list's one home: the docstrings, `SKILL.md`, the
# design notes and the eval case state the rule and point here. Grouped by phase, with the
# kubernetes package that makes each write; admission reads a PATCH as UPDATE, so UPDATE
# covers both. A rule that can match any entry puts the webhook in the upgrade's path; a
# rule that matches none is reported, not graded, because the list is what the rule knows
# of the path rather than a proof the upgrade is unaffected. Weighed and left off, because
# the upgrade completes without them: events (`client-go/tools/record` drops a record it
# cannot write and the caller goes on); Endpoints and EndpointSlices
# (`pkg/controller/endpointslice` repairs Service routing as pods move, and nothing the
# drain, the node delete or the join waits on); the CSINode's deletion
# (`pkg/controller/garbagecollector` removes it after its Node is gone, and an orphan stalls
# nothing); PersistentVolumeClaims (a replacement pod reuses its bound claim).
# `objectSelector` and `matchConditions` are not evaluated, and `namespaceSelector` only for
# the `kube-system` reach (`CONTROL_PLANE_KUBE_SYSTEM_WRITES` below): a webhook they narrow
# is otherwise reported as able to match, which errs toward naming it. `apiVersions` is
# evaluated, against the served version on each row (`VERSION_V1` below).
SCOPE_NAMESPACED = "Namespaced"
SCOPE_CLUSTER = "Cluster"
SCOPE_ANY = "*"
WILDCARD = "*"
# The version the API server serves each write at, carried on its row: the matcher reads a
# rule's `apiVersions` as the API server does (`*` or the request's version), so a rule pinned
# to a version the server no longer serves (`policy/v1beta1`, `certificates.k8s.io/v1beta1`,
# `storage.k8s.io/v1beta1`) matches nothing. The pin is judged per rule (`_rule_version_pinned`):
# a webhook whose every rule is pinned is an outage, not a blocker, whose cell says the server
# sends it no request at a served version (`WEBHOOK_PINNED_MATCHES`); one that pairs a pinned
# rule with a rule the server does serve off the path fails that rule's requests now, and its
# cell names the live rules as failing and the pinned rule alone as sent nothing
# (`WEBHOOK_MIXED_MATCHES`). Every write on the list is served at `v1` alone on any GKE version
# in support; under the default `matchPolicy: Equivalent` a rule naming another served version
# of the same resource would also match, and no resource here has one.
VERSION_V1 = "v1"
GROUP_CORE = ""
GROUP_POLICY = "policy"
GROUP_COORDINATION = "coordination.k8s.io"
GROUP_CERTIFICATES = "certificates.k8s.io"
GROUP_STORAGE = "storage.k8s.io"
GROUP_RBAC = "rbac.authorization.k8s.io"
OP_CREATE = "CREATE"
OP_UPDATE = "UPDATE"
OP_DELETE = "DELETE"
# The drain. The eviction (`pkg/registry/core/pod/storage/eviction.go`), whose handler
# decrements the budget's status before the pod goes and fails the eviction when it
# cannot (`pkg/controller/disruption` recomputes it afterwards); the old pod's terminal
# status and then its deletion, both by the kubelet's status manager (`pkg/kubelet/status`).
UPGRADE_PATH_DRAIN = (
    (GROUP_CORE, VERSION_V1, "pods/eviction", OP_CREATE, SCOPE_NAMESPACED),
    (GROUP_POLICY, VERSION_V1, "poddisruptionbudgets/status", OP_UPDATE, SCOPE_NAMESPACED),
    (GROUP_CORE, VERSION_V1, "pods/status", OP_UPDATE, SCOPE_NAMESPACED),
    (GROUP_CORE, VERSION_V1, "pods", OP_DELETE, SCOPE_NAMESPACED),
)
# The replacement pods. Created by their controllers (`pkg/controller/replicaset`, and
# `pkg/controller/daemon` for the new node's system pods), placed by the scheduler's
# binding (`pkg/scheduler`), and started only once the kubelet has a token for each
# projected service-account volume (`pkg/kubelet/token`).
UPGRADE_PATH_REPLACEMENT_PODS = (
    (GROUP_CORE, VERSION_V1, "pods", OP_CREATE, SCOPE_NAMESPACED),
    (GROUP_CORE, VERSION_V1, "pods/binding", OP_CREATE, SCOPE_NAMESPACED),
    (GROUP_CORE, VERSION_V1, "serviceaccounts/token", OP_CREATE, SCOPE_NAMESPACED),
)
# The nodes. The new one registers and reports status (`pkg/kubelet/kubelet_node_status.go`;
# the attach-detach controller writes `volumesAttached` into the same status,
# `pkg/controller/volume/attachdetach/statusupdater`); its spec is updated by the cordon
# (`k8s.io/kubectl/pkg/drain`), by the cloud node controller that clears the
# `uninitialized` taint (`k8s.io/cloud-provider/controllers/node`) and by the lifecycle
# controller's taints (`pkg/controller/nodelifecycle`); the old one is deleted once its
# VM is gone (`k8s.io/cloud-provider/controllers/nodelifecycle`). The kubelet's heartbeat
# lease in kube-node-lease (`pkg/kubelet/nodelease`, through
# `k8s.io/component-helpers/apimachinery/lease`) is what keeps the node Ready between
# status reports.
UPGRADE_PATH_NODES = (
    (GROUP_CORE, VERSION_V1, "nodes", OP_CREATE, SCOPE_CLUSTER),
    (GROUP_CORE, VERSION_V1, "nodes", OP_UPDATE, SCOPE_CLUSTER),
    (GROUP_CORE, VERSION_V1, "nodes/status", OP_UPDATE, SCOPE_CLUSTER),
    (GROUP_CORE, VERSION_V1, "nodes", OP_DELETE, SCOPE_CLUSTER),
    (GROUP_COORDINATION, VERSION_V1, "leases", OP_CREATE, SCOPE_NAMESPACED),
    (GROUP_COORDINATION, VERSION_V1, "leases", OP_UPDATE, SCOPE_NAMESPACED),
)
# The new kubelet's identity. It files a certificate signing request to bootstrap its
# client certificate (`pkg/kubelet/certificate/bootstrap`); the approver writes the
# `approval` subresource (`pkg/controller/certificates/approver`) and the signer the
# `status` subresource (`pkg/controller/certificates/signer`). An unsigned request leaves
# the node without a client certificate.
UPGRADE_PATH_KUBELET_IDENTITY = (
    (GROUP_CERTIFICATES, VERSION_V1, "certificatesigningrequests", OP_CREATE, SCOPE_CLUSTER),
    (GROUP_CERTIFICATES, VERSION_V1, "certificatesigningrequests/approval", OP_UPDATE, SCOPE_CLUSTER),
    (GROUP_CERTIFICATES, VERSION_V1, "certificatesigningrequests/status", OP_UPDATE, SCOPE_CLUSTER),
)
# A replacement pod's persistent disk. The kubelet creates its CSINode when it starts and
# holds its Ready condition on the write, then updates it as each CSI driver registers
# (`pkg/volume/csi/nodeinfomanager`); the external-attacher reads the driver's node id
# from it. The attach-detach controller creates a VolumeAttachment for the new node and
# deletes the drained node's (`pkg/controller/volume/attachdetach`, through
# `pkg/volume/csi/csi_attacher.go`); a ReadWriteOnce disk attaches nowhere else until
# that delete completes. The external-attacher (`kubernetes-csi/external-attacher`,
# `pkg/controller/csi_handler.go`) writes its finalizer on the attachment and on the
# PersistentVolume before it attaches, then `attached: true` into the attachment's status.
UPGRADE_PATH_STORAGE = (
    (GROUP_STORAGE, VERSION_V1, "csinodes", OP_CREATE, SCOPE_CLUSTER),
    (GROUP_STORAGE, VERSION_V1, "csinodes", OP_UPDATE, SCOPE_CLUSTER),
    (GROUP_STORAGE, VERSION_V1, "volumeattachments", OP_CREATE, SCOPE_CLUSTER),
    (GROUP_STORAGE, VERSION_V1, "volumeattachments", OP_UPDATE, SCOPE_CLUSTER),
    (GROUP_STORAGE, VERSION_V1, "volumeattachments/status", OP_UPDATE, SCOPE_CLUSTER),
    (GROUP_STORAGE, VERSION_V1, "volumeattachments", OP_DELETE, SCOPE_CLUSTER),
    (GROUP_CORE, VERSION_V1, "persistentvolumes", OP_UPDATE, SCOPE_CLUSTER),
)
UPGRADE_PATH_TARGETS = UPGRADE_PATH_DRAIN + UPGRADE_PATH_REPLACEMENT_PODS + UPGRADE_PATH_NODES + UPGRADE_PATH_KUBELET_IDENTITY + UPGRADE_PATH_STORAGE
UPGRADE_PATH_LABEL = "{operation} {resource}"
# The control plane's own writes that a new master cannot start without, which a
# control-plane upgrade makes on each new master: kube-apiserver's `rbac/bootstrap-roles`
# post-start hook reconciles the bootstrap Roles and RoleBindings in `kube-system` and
# `kube-public` (`pkg/registry/rbac/rest/storage_rbac.go`, `EnsureRBACPolicy`: a 30-second
# poll, then `unable to initialize roles`, which `runPostStartHook` turns into a fatal; the
# objects are `bootstrappolicy.NamespaceRoles()` and `NamespaceRoleBindings()` in
# `plugin/pkg/auth/authorizer/rbac/bootstrappolicy/namespace_policy.go`, six and six in
# `kube-system` and the `bootstrap-signer` pair in `kube-public`). A fail-closed webhook with
# a dead backend whose rules match one of these writes and whose `namespaceSelector` admits
# either namespace refuses that reconcile, so the master crash-loops the way Jetstack's 2019
# GKE outage did (`docs/designs/upgrade-readiness-checks.md`), and it is graded `blocked` like
# a webhook on the node path, with the cell naming the write and the namespaces admitted.
# ConfigMaps are not on the list: the `ca-registration` hook that outage deadlocked on left the
# start-up path in Kubernetes 1.17, and its successor
# (`pkg/controlplane/controller/clusterauthenticationtrust`) writes from a retrying background
# queue, so a refused ConfigMap write is an outage, not a stuck master. The leader-election
# Leases the controller manager and scheduler take in `kube-system` are already on
# UPGRADE_PATH_NODES (the Lease rows), so a Lease gate blocks on that list. Rows as in
# UPGRADE_PATH_TARGETS; every one is Namespaced and served at v1. The namespaces are judged on
# their default `kubernetes.io/metadata.name` label alone (`namespace_selector_reaches`).
NAMESPACE_NAME_LABEL = "kubernetes.io/metadata.name"
KUBE_SYSTEM_NAMESPACE = "kube-system"
KUBE_PUBLIC_NAMESPACE = "kube-public"
BOOTSTRAP_POLICY_NAMESPACES = (KUBE_SYSTEM_NAMESPACE, KUBE_PUBLIC_NAMESPACE)
CONTROL_PLANE_KUBE_SYSTEM_WRITES = (
    (GROUP_RBAC, VERSION_V1, "roles", OP_CREATE, SCOPE_NAMESPACED),
    (GROUP_RBAC, VERSION_V1, "roles", OP_UPDATE, SCOPE_NAMESPACED),
    (GROUP_RBAC, VERSION_V1, "rolebindings", OP_CREATE, SCOPE_NAMESPACED),
    (GROUP_RBAC, VERSION_V1, "rolebindings", OP_UPDATE, SCOPE_NAMESPACED),
)
KUBE_SYSTEM_WRITE_LABEL = "{operation} {resource} in {namespaces}"
NAMESPACE_JOIN = ","
WEBHOOK_NAME_FORMAT = "{config}/{webhook}"
WEBHOOK_SERVICE_FORMAT = "{namespace}/{name}"
WEBHOOK_FINDING_FORMAT = "{webhook} ({config_kind}): failurePolicy Fail and {reason}; matches {matches}"
WEBHOOK_OUTAGE_MATCHES = "none of the operations this rule reads as the upgrade's path (its rules: {rules}); it fails its own requests now and is reported, not graded"
# The off-path cell for a rule that names an upgrade-path write only at a version the server
# does not serve: the server sends the webhook none of those requests, so nothing fails now,
# and the cell says so rather than reporting a live outage.
WEBHOOK_PINNED_MATCHES = "no request the server sends at a served version (its rules: {rules}; the server serves {pinned} at {served} alone, so it sends this webhook none of them); it is reported, not graded"
# The off-path cell for a webhook that pairs such a pinned rule with a rule the server does
# serve: the live rules' requests fail now and are named, and the pinned rule alone is the one
# the server sends nothing.
WEBHOOK_MIXED_MATCHES = "none of the operations this rule reads as the upgrade's path (its rules: {rules}); it fails now the requests matched by {live}, and is reported, not graded; the server serves {pinned} at {served} alone, so it sends the rule {pinned_rules} nothing"
# A webhook's rules, rendered for the cell so the operator can judge an outage: operations
# joined by `/`, resources by `,`, with the API groups named when any is not the core group,
# and the core group then named `core` beside the others rather than dropped (a rule on
# `["", "apps"]` gates core resources too, and a cell that says only `in apps` misreads it).
RULE_FORMAT = "{operations} {resources}"
RULE_GROUP_FORMAT = "{rule} in {groups}"
RULE_CORE_GROUP_NAME = "core"
# A rule's `apiVersions` is rendered only when it pins one (anything but `*` alone): a cell
# that names a path write at a version the server does not serve then says why it is an outage.
RULE_VERSIONS_FORMAT = "{rule} at {versions}"
RULE_OPERATION_JOIN = "/"
RULE_RESOURCE_JOIN = ","
RULE_NONE = "no rules"


# ---------------------------------------------------------------------------- PDBs


def split_items(items: list) -> tuple[list[dict], list[dict]]:
    """(pdbs, workloads) from the mixed `items` of `kubectl get pdb,deploy,statefulset -A`."""
    pdbs, workloads = [], []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        kind = item.get("kind")
        if kind == PDB_KIND:
            pdbs.append(item)
        elif kind in WORKLOAD_KINDS:
            workloads.append(item)
    return pdbs, workloads


def _requirements_hold(selector: dict, labels: dict, *, unevaluable: bool) -> bool:
    """Whether `labels` satisfy a label selector's `matchLabels` and `matchExpressions`;
    `unevaluable` is the answer for a requirement this reader cannot evaluate (not a mapping,
    an operator it does not know). The nil-versus-empty decision is each caller's, because
    policy/v1 and admission read it differently."""
    labels = labels if isinstance(labels, dict) else {}
    for key, value in (selector.get("matchLabels") or {}).items():
        if labels.get(key) != value:
            return False
    for req in selector.get("matchExpressions") or []:
        if not isinstance(req, dict):
            return unevaluable
        key, op, values = req.get("key"), req.get("operator"), req.get("values") or []
        if op == OP_IN:
            if key not in labels or labels[key] not in values:
                return False
        elif op == OP_NOT_IN:
            if key in labels and labels[key] in values:
                return False
        elif op == OP_EXISTS:
            if key not in labels:
                return False
        elif op == OP_DOES_NOT_EXIST:
            if key in labels:
                return False
        else:
            return unevaluable
    return True


def selector_matches(selector, labels: dict) -> bool:
    """policy/v1 semantics: a null selector matches no pod, an empty one every pod."""
    if not isinstance(selector, dict):
        return False
    return _requirements_hold(selector, labels, unevaluable=False)


def _percent(value) -> int | None:
    """`"75%"` -> 75; None for anything that is not a percentage string."""
    if isinstance(value, str) and value.endswith(PERCENT_SUFFIX):
        try:
            return int(value[: -len(PERCENT_SUFFIX)].strip())
        except ValueError:
            return None
    return None


def _integer(value) -> int | None:
    """An int, or a string holding one; None otherwise (booleans excluded)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _round_up(percent: int, total: int) -> int:
    """`ceil(percent * total / 100)`, the disruption controller's rounding for both fields."""
    return -(-percent * total // PERCENT_BASE)


def blocking_field(spec: dict, total_replicas: int) -> str | None:
    """The spec field that blocks every drain of `total_replicas` pods, or None.

    `maxUnavailable` 0 or `0%` allows no disruption at any scale; a positive percentage
    rounds up to at least one pod, so it never blocks. `minAvailable` blocks when it demands
    every expected pod: an integer at or above the total, or a percentage that rounds up to
    the total (`100%` always, `90%` on nine replicas too).
    """
    if "maxUnavailable" in spec:
        value = spec.get("maxUnavailable")
        if _integer(value) == 0 or _percent(value) == 0:
            return FIELD_MAX_UNAVAILABLE.format(value=value)
        return None
    if "minAvailable" in spec:
        value = spec.get("minAvailable")
        as_int = _integer(value)
        if as_int is not None:
            return FIELD_MIN_AVAILABLE_INT.format(value=value, total=total_replicas) if as_int >= total_replicas else None
        as_percent = _percent(value)
        if as_percent is not None:
            healthy = _round_up(as_percent, total_replicas)
            if healthy >= total_replicas:
                return FIELD_MIN_AVAILABLE_PERCENT.format(value=value, healthy=healthy, total=total_replicas)
    return None


def _workload_summary(workload: dict) -> dict:
    meta = workload.get("metadata") or {}
    replicas = _integer((workload.get("spec") or {}).get("replicas"))
    return {
        "kind": workload.get("kind"),
        "namespace": meta.get("namespace", ""),
        "name": meta.get("name", ""),
        "replicas": DEFAULT_REPLICAS if replicas is None else replicas,
    }


def grade_pdbs(pdbs: list[dict], workloads: list[dict]) -> dict:
    """Which PDBs block a node drain, and how many were skipped and why.

    A PDB is matched to the Deployments and StatefulSets in its namespace whose pod-template
    labels satisfy its selector; the replica total of those workloads is what the controller
    expects, and the spec is graded against it (SOP §3.4 decides on the spec, with
    `status.disruptionsAllowed` as corroboration). Skipped and counted rather than graded:
    a PDB whose `status.expectedPods` is 0 or whose matched workloads are all scaled to
    zero (`scaled_to_zero`), one that matches no workload and covers no pod (`orphan`), and
    one that covers pods but matches neither kind read here (`unmatched`), which the caller
    notes rather than grades so a PDB on another controller is never silently ready.
    """
    by_namespace: dict[str, list[dict]] = {}
    for workload in workloads:
        namespace = (workload.get("metadata") or {}).get("namespace", "")
        by_namespace.setdefault(namespace, []).append(workload)

    result = {"blocking": [], "scaled_to_zero": 0, "orphan": 0, "unmatched": 0, "evaluated": 0}
    for pdb in pdbs:
        meta = pdb.get("metadata") or {}
        spec = pdb.get("spec") or {}
        status = pdb.get("status") or {}
        namespace = meta.get("namespace", "")
        matched = [
            _workload_summary(w)
            for w in by_namespace.get(namespace, [])
            if selector_matches(spec.get("selector"), (((w.get("spec") or {}).get("template") or {}).get("metadata") or {}).get("labels"))
        ]
        expected_pods = _integer(status.get("expectedPods"))
        total = sum(w["replicas"] for w in matched)
        if not matched:
            if expected_pods:
                result["unmatched"] += 1
            else:
                result["orphan"] += 1
            continue
        if expected_pods == 0 or total == 0:
            result["scaled_to_zero"] += 1
            continue
        result["evaluated"] += 1
        # The controller counts every pod the selector covers, including pods of a kind
        # not read here (a bare ReplicaSet beside the Deployment); when its count is the
        # larger, that is the total minAvailable is measured against.
        field = blocking_field(spec, max(total, expected_pods or 0))
        if field is None:
            continue
        result["blocking"].append(
            {
                "pdb": PDB_NAME_FORMAT.format(namespace=namespace, name=meta.get("name", "")),
                "namespace": namespace,
                "name": meta.get("name", ""),
                "field": field,
                "workloads": matched,
                "expected_pods": expected_pods,
                "disruptions_allowed": _integer(status.get("disruptionsAllowed")),
            }
        )
    return result


def describe_finding(finding: dict) -> str:
    """`ns/name (maxUnavailable: 0; Deployment ns/web (3 replicas))` for a table cell."""
    workloads = LIST_SEPARATOR.join(WORKLOAD_FORMAT.format(**w) for w in finding["workloads"])
    return f"{finding['pdb']} ({finding['field']}; {workloads})"


# --------------------------------------------------------------------- maintenance


def parse_rfc3339(text) -> datetime | None:
    """An RFC 3339 timestamp as an aware UTC datetime; None when it does not parse."""
    if not isinstance(text, str) or not RFC3339_RE.match(text.strip()):
        return None
    try:
        parsed = datetime.fromisoformat(text.strip().upper())
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc)


def format_instant(when: datetime | None) -> str | None:
    return when.astimezone(timezone.utc).strftime(WINDOW_TIMESTAMP_FORMAT) if when else None


def parse_iso_duration(text) -> timedelta | None:
    """`PT4H0M0S` -> 4 hours; None when it does not parse."""
    if not isinstance(text, str):
        return None
    m = ISO_DURATION_RE.match(text.strip())
    if not m or not any(m.groups()):
        return None
    days, hours, minutes, seconds = m.groups()
    return timedelta(days=int(days or 0), hours=int(hours or 0), minutes=int(minutes or 0), seconds=float(seconds or 0))


def _minor(version: tuple) -> tuple[int, int]:
    return version[0], version[1]


def _components_needing_minor(target, master, pools: list[dict]) -> list[str]:
    """The components for which the upgrade to `target` is a minor upgrade."""
    components = []
    if master is not None and _minor(target) > _minor(master):
        components.append(CONTROL_PLANE_LABEL)
    for pool in pools:
        version = pool.get("parsed")
        if version is not None and _minor(target) > _minor(version):
            components.append(POOL_LABEL.format(name=pool.get("name", "")))
    return components


def _pools_needing_node_upgrade(target, pools: list[dict]) -> list[str]:
    return [pool.get("name", "") for pool in pools if pool.get("parsed") is not None and pool["parsed"] < target]


def evaluate_exclusion(name: str, exclusion: dict, at: datetime, target_text: str | None, target, master, pools: list[dict]) -> dict:
    """One exclusion at `at`: whether it is in effect and whether its scope covers the upgrade.

    `blocks` is True, False, or None when it could not be decided (no target, an unknown
    scope, unparsable times). `pools` carry `name` and `parsed` (a version tuple or None).
    """
    start = parse_rfc3339(exclusion.get("startTime"))
    end = parse_rfc3339(exclusion.get("endTime"))
    scope = (exclusion.get("maintenanceExclusionOptions") or {}).get("scope") or DEFAULT_EXCLUSION_SCOPE
    entry = {
        "name": name,
        "scope": scope,
        "start_time": exclusion.get("startTime"),
        "end_time": exclusion.get("endTime"),
        "in_effect": False,
        "blocks": False,
        "detail": "",
    }
    if start is None or end is None:
        entry["blocks"] = None
        entry["detail"] = EXCLUSION_UNPARSABLE
        return entry
    entry["in_effect"] = start <= at <= end
    if not entry["in_effect"]:
        return entry
    end_text = format_instant(end)
    if scope not in KNOWN_SCOPES:
        entry["blocks"] = None
        entry["detail"] = EXCLUSION_UNKNOWN_SCOPE.format(end=end_text)
        return entry
    if scope == SCOPE_NO_UPGRADES:
        entry["blocks"] = True
        entry["detail"] = EXCLUSION_BLOCKS.format(target=target_text or "?", end=end_text, why=WHY_ANY_UPGRADE)
        return entry
    if target is None:
        entry["blocks"] = None
        entry["detail"] = EXCLUSION_TARGET_UNKNOWN.format(end=end_text)
        return entry
    minor_components = _components_needing_minor(target, master, pools)
    if minor_components:
        entry["blocks"] = True
        why = WHY_MINOR_UPGRADE.format(components=COMPONENT_SEPARATOR.join(minor_components))
        entry["detail"] = EXCLUSION_BLOCKS.format(target=target_text, end=end_text, why=why)
        return entry
    if scope == SCOPE_NO_MINOR_OR_NODE_UPGRADES:
        node_pools = _pools_needing_node_upgrade(target, pools)
        if node_pools:
            entry["blocks"] = True
            why = WHY_NODE_UPGRADE.format(pools=LIST_SEPARATOR.join(node_pools))
            entry["detail"] = EXCLUSION_BLOCKS.format(target=target_text, end=end_text, why=why)
            return entry
    below = (master is not None and master < target) or _pools_needing_node_upgrade(target, pools)
    entry["detail"] = EXCLUSION_NOT_APPLICABLE.format(end=end_text, why=WHY_PATCH_ONLY if below else WHY_NOTHING_TO_UPGRADE)
    return entry


def parse_rrule(text) -> dict | None:
    """{"freq": ..., "bydays": [...]} for a supported recurrence; None otherwise."""
    if not isinstance(text, str) or not text.strip():
        return None
    fields = {}
    for part in text.strip().split(RRULE_SEPARATOR):
        if RRULE_KEY_VALUE_SEPARATOR not in part:
            return None
        key, value = part.split(RRULE_KEY_VALUE_SEPARATOR, 1)
        fields[key.strip().upper()] = value.strip().upper()
    if fields.get(RRULE_INTERVAL, RRULE_DEFAULT_INTERVAL) != RRULE_DEFAULT_INTERVAL:
        return None
    freq = fields.get(RRULE_FREQ)
    extra_keys = set(fields) - {RRULE_FREQ, RRULE_BYDAY, RRULE_INTERVAL}
    if extra_keys:
        return None
    if freq == RRULE_DAILY:
        return {"freq": RRULE_DAILY, "bydays": []} if RRULE_BYDAY not in fields else None
    if freq == RRULE_WEEKLY:
        bydays = [d for d in fields.get(RRULE_BYDAY, "").split(BYDAY_SEPARATOR) if d]
        if any(d not in WEEKDAYS for d in bydays):
            return None
        return {"freq": RRULE_WEEKLY, "bydays": bydays}
    return None


def _occurrences(first_start: datetime, bydays: list[str], at: datetime, duration: timedelta) -> list[datetime]:
    """Window starts around `at`: every day, or every listed weekday, at `first_start`'s time."""
    weekday_indexes = {WEEKDAYS.index(d) for d in bydays} if bydays else set(range(DAYS_PER_WEEK))
    lower = at - OCCURRENCE_HORIZON - duration
    upper = at + OCCURRENCE_HORIZON
    starts = []
    day = lower.date()
    while day <= upper.date():
        if day.weekday() in weekday_indexes:
            start = datetime.combine(day, first_start.timetz())
            if start >= first_start:
                starts.append(start)
        day += timedelta(days=1)
    return starts


def _window_state(starts: list[datetime], duration: timedelta, at: datetime, first_start: datetime) -> dict:
    """Open or closed at `at`, when it closes, and the next start after `at`.

    The starts cover one week either side of `at`; a window whose first occurrence is
    further out than that has that occurrence as its next opening.
    """
    open_until = None
    for start in starts:
        if start <= at < start + duration:
            open_until = start + duration
            break
    next_opening = min((s for s in starts if s > at), default=None)
    if next_opening is None and first_start > at:
        next_opening = first_start
    return {
        "state": WINDOW_OPEN if open_until else WINDOW_CLOSED,
        "closes_at": format_instant(open_until),
        "next_opening": format_instant(next_opening),
    }


def evaluate_window(window: dict, at: datetime) -> dict:
    """The maintenance window's kind and its state at `at`.

    Handles `dailyMaintenanceWindow` (a four-hour window at a UTC time of day) and
    `recurringWindow` with `FREQ=DAILY` or `FREQ=WEEKLY[;BYDAY=...]`, whose first
    occurrence and length come from its `window`. Anything else is `not evaluated`.
    """
    daily = (window or {}).get("dailyMaintenanceWindow")
    recurring = (window or {}).get("recurringWindow")
    result = {"kind": WINDOW_NONE, "recurrence": None, "state": WINDOW_NONE, "closes_at": None, "next_opening": None, "detail": ""}
    if isinstance(daily, dict):
        result["kind"] = WINDOW_DAILY
        try:
            start_time = datetime.strptime(str(daily.get("startTime", "")), DAILY_START_FORMAT).time()
        except ValueError:
            result["state"] = WINDOW_NOT_EVALUATED
            result["detail"] = f"daily window start {daily.get('startTime')!r} unparsable"
            return result
        duration = parse_iso_duration(daily.get("duration")) or timedelta(hours=DAILY_WINDOW_HOURS)
        first = datetime.combine(at.date() - OCCURRENCE_HORIZON, time(start_time.hour, start_time.minute, tzinfo=timezone.utc))
        result.update(_window_state(_occurrences(first, [], at, duration), duration, at, first))
        result["detail"] = f"daily at {start_time.strftime(DAILY_START_FORMAT)}Z for {int(duration.total_seconds() // SECONDS_PER_HOUR)}h"
        return result
    if isinstance(recurring, dict):
        result["kind"] = WINDOW_RECURRING
        result["recurrence"] = recurring.get("recurrence")
        rule = parse_rrule(recurring.get("recurrence"))
        span = recurring.get("window") or {}
        start = parse_rfc3339(span.get("startTime"))
        end = parse_rfc3339(span.get("endTime"))
        if rule is None or start is None or end is None or end <= start:
            result["state"] = WINDOW_NOT_EVALUATED
            result["detail"] = f"recurrence {recurring.get('recurrence')!r} not evaluated; only FREQ=DAILY and FREQ=WEEKLY[;BYDAY=...] are"
            return result
        duration = end - start
        # RFC 5545: a WEEKLY rule with no BYDAY recurs on DTSTART's weekday, not every day.
        bydays = rule["bydays"] or ([WEEKDAYS[start.weekday()]] if rule["freq"] == RRULE_WEEKLY else [])
        result.update(_window_state(_occurrences(start, bydays, at, duration), duration, at, start))
        days = LIST_SEPARATOR.join(bydays) if bydays else RRULE_DAILY.lower()
        result["detail"] = f"{days} from {start.strftime(DAILY_START_FORMAT)}Z for {int(duration.total_seconds() // SECONDS_PER_HOUR)}h"
        return result
    result["detail"] = "no maintenance window; automatic upgrades may start at any hour"
    return result


def evaluate_maintenance(policy: dict | None, at: datetime, target_text: str | None, target, master, pools: list[dict]) -> dict:
    """Every exclusion and the window of one cluster's `maintenancePolicy`, at `at`."""
    window = (policy or {}).get("window") or {}
    exclusions = window.get("maintenanceExclusions") or {}
    entries = [
        evaluate_exclusion(name, exclusion if isinstance(exclusion, dict) else {}, at, target_text, target, master, pools)
        for name, exclusion in sorted(exclusions.items())
    ]
    return {
        "exclusions": entries,
        "blocking_exclusions": [e["name"] for e in entries if e["blocks"] is True],
        "undecided_exclusions": [e["name"] for e in entries if e["blocks"] is None],
        "window": evaluate_window(window, at),
    }


# ---------------------------------------------------------------------------- skew


def evaluate_skew(target, pools: list[dict], autopilot: bool) -> dict:
    """Per pool, how many minors it trails the target control plane, and whether that blocks.

    `pools` carry `name`, `version` and `parsed`. Not applicable on Autopilot, where Google
    owns the pools; not evaluated without a target.
    """
    if autopilot:
        return {"applicable": False, "reason": SKEW_AUTOPILOT_REASON, "pools": [], "blocking": [], "at_ceiling": [], "unknown": []}
    if target is None:
        return {"applicable": True, "reason": SKEW_NO_TARGET_REASON, "pools": [], "blocking": [], "at_ceiling": [], "unknown": [p.get("name", "") for p in pools]}
    graded = []
    for pool in pools:
        version = pool.get("parsed")
        entry = {"name": pool.get("name", ""), "version": pool.get("version"), "minors_behind_target": None, "verdict": SKEW_UNKNOWN, "detail": ""}
        if version is None:
            entry["detail"] = f"version {pool.get('version')!r} unparsable"
        elif version[0] != target[0]:
            entry["verdict"] = SKEW_BLOCKS
            entry["detail"] = SKEW_MAJOR_DIFFERS
        else:
            behind = target[1] - version[1]
            entry["minors_behind_target"] = behind
            if behind > SKEW_CEILING_MINORS:
                entry["verdict"] = SKEW_BLOCKS
                entry["detail"] = f"{behind} minors behind the target control plane; more than {SKEW_CEILING_MINORS} blocks the control-plane upgrade until the pool moves"
            elif behind == SKEW_CEILING_MINORS:
                entry["verdict"] = SKEW_AT_CEILING
                entry["detail"] = f"{behind} minors behind the target control plane, at the skew ceiling; the next minor is blocked until the pool moves"
            else:
                entry["verdict"] = SKEW_OK
        graded.append(entry)
    return {
        "applicable": True,
        "reason": None,
        "pools": graded,
        "blocking": [p["name"] for p in graded if p["verdict"] == SKEW_BLOCKS],
        "at_ceiling": [p["name"] for p in graded if p["verdict"] == SKEW_AT_CEILING],
        "unknown": [p["name"] for p in graded if p["verdict"] == SKEW_UNKNOWN],
    }


# ---------------------------------------------------------------------- webhooks


def split_webhook_items(items: list) -> tuple[list[dict], list[dict], list[dict]]:
    """(webhook configurations, Services, EndpointSlices) from a mixed `items` list."""
    configs, services, slices = [], [], []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        kind = item.get("kind")
        if kind in WEBHOOK_CONFIG_KINDS:
            configs.append(item)
        elif kind == SERVICE_KIND:
            services.append(item)
        elif kind == ENDPOINTSLICE_KIND:
            slices.append(item)
    return configs, services, slices


def _named(items: list[dict], namespace: str, name: str) -> dict | None:
    for item in items:
        meta = item.get("metadata") or {}
        if meta.get("namespace") == namespace and meta.get("name") == name:
            return item
    return None


def backend_problem(service_ref: dict, services: list[dict], slices: list[dict]) -> str | None:
    """Why the API server cannot reach this webhook's Service, or None when it can.

    The Service must exist and carry a port equal to the webhook's port; the ready count is
    over the endpoints of the Service's EndpointSlices whose port has that Service port's
    name (an unnamed single port matches an unnamed slice port)."""
    namespace, name = service_ref.get("namespace", ""), service_ref.get("name", "")
    label = WEBHOOK_SERVICE_FORMAT.format(namespace=namespace, name=name)
    port = service_ref.get("port") or DEFAULT_WEBHOOK_PORT
    service = _named(services, namespace, name)
    if service is None:
        return BACKEND_NO_SERVICE.format(service=label)
    service_ports = [p for p in (service.get("spec") or {}).get("ports") or [] if isinstance(p, dict) and p.get("port") == port]
    if not service_ports:
        return BACKEND_NO_PORT.format(service=label, port=port)
    port_name = service_ports[0].get("name") or ""
    ready = 0
    for slice_ in slices:
        meta = slice_.get("metadata") or {}
        if meta.get("namespace") != namespace or (meta.get("labels") or {}).get(SERVICE_NAME_LABEL) != name:
            continue
        if not any(isinstance(p, dict) and (p.get("name") or "") == port_name for p in slice_.get("ports") or []):
            continue
        for endpoint in slice_.get("endpoints") or []:
            if isinstance(endpoint, dict) and (endpoint.get("conditions") or {}).get("ready") is not False:
                ready += 1
    if not ready:
        return BACKEND_NO_ENDPOINTS.format(service=label, port=port)
    return None


def _resource_matches(spec: str, target: str) -> bool:
    """RuleWithOperations `resources` semantics, written as the API server's matcher is
    (`splitResource`, then two comparisons): the spec's resource is `*` or the target's, and
    its subresource is `*` or the target's, where no `/` means an empty subresource. So `*`
    is every resource but no subresource, `*/*` every resource and subresource, `pods/*` pods
    together with every subresource of pods (a `*` subresource also matches a request that
    has none, so `leases/*` reaches the lease writes a kubelet heartbeat makes), and `pods/`
    is `pods`, as the API admits and reads it. Two comparisons leave no spelling to diverge
    on."""
    resource, _, sub = target.partition("/")
    spec_resource, _, spec_sub = spec.partition("/")
    return spec_resource in (resource, WILDCARD) and spec_sub in (sub, WILDCARD)


def _rule_reaches(rule: dict, group: str, version: str | None, resource: str, operation: str, scope: str) -> bool:
    """Whether one rule matches one write, as the API server's matcher reads it; with
    `version` None the rule's `apiVersions` is not read."""
    rule_scope = rule.get("scope") or SCOPE_ANY
    if rule_scope not in (SCOPE_ANY, scope):
        return False
    if not ({WILDCARD, group} & set(rule.get("apiGroups") or [])):
        return False
    if version is not None and not ({WILDCARD, version} & set(rule.get("apiVersions") or [])):
        return False
    if not ({WILDCARD, operation} & set(rule.get("operations") or [])):
        return False
    return any(isinstance(spec, str) and _resource_matches(spec, resource) for spec in rule.get("resources") or [])


def _rules(hook: dict) -> list[dict]:
    return [rule for rule in hook.get("rules") or [] if isinstance(rule, dict)]


def _upgrade_path_labels(rules: list[dict], *, read_version: bool) -> list[str]:
    matched = []
    for group, version, resource, operation, scope in UPGRADE_PATH_TARGETS:
        if any(_rule_reaches(rule, group, version if read_version else None, resource, operation, scope) for rule in rules):
            matched.append(UPGRADE_PATH_LABEL.format(operation=operation, resource=resource))
    return matched


def upgrade_path_matches(hook: dict) -> list[str]:
    """The operations a node upgrade needs that this webhook's rules can match, as labels."""
    return _upgrade_path_labels(_rules(hook), read_version=True)


def namespace_selector_reaches(hook: dict, labels: dict) -> bool:
    """Whether the webhook's `namespaceSelector` admits a namespace carrying `labels`, as
    admission reads it: absent or empty admits every namespace, and a requirement this reader
    cannot evaluate counts as admitting, which errs toward naming the webhook. The caller
    passes the namespace's labels; `kube-system` and `kube-public` are judged on their default
    `kubernetes.io/metadata.name` label alone, so a selector keyed on a label an operator added
    to them reads as not admitting, the one direction this reader errs away from naming."""
    selector = hook.get("namespaceSelector")
    if not isinstance(selector, dict) or not selector:
        return True
    return _requirements_hold(selector, labels, unevaluable=True)


def bootstrap_namespaces_admitted(hook: dict) -> list[str]:
    """The bootstrap-policy namespaces this webhook's `namespaceSelector` admits, in
    `BOOTSTRAP_POLICY_NAMESPACES` order; empty when it admits neither."""
    return [name for name in BOOTSTRAP_POLICY_NAMESPACES if namespace_selector_reaches(hook, {NAMESPACE_NAME_LABEL: name})]


def _kube_system_labels(rules: list[dict], namespaces: list[str], *, read_version: bool) -> list[str]:
    matched = []
    for group, version, resource, operation, scope in CONTROL_PLANE_KUBE_SYSTEM_WRITES:
        if any(_rule_reaches(rule, group, version if read_version else None, resource, operation, scope) for rule in rules):
            matched.append(KUBE_SYSTEM_WRITE_LABEL.format(operation=operation, resource=resource, namespaces=NAMESPACE_JOIN.join(namespaces)))
    return matched


def kube_system_write_matches(hook: dict) -> list[str]:
    """The control plane's own bootstrap-policy writes this webhook can match, as labels: empty
    unless its `namespaceSelector` admits `kube-system` or `kube-public` and a rule matches a
    row of `CONTROL_PLANE_KUBE_SYSTEM_WRITES` at the served version."""
    admitted = bootstrap_namespaces_admitted(hook)
    if not admitted:
        return []
    return _kube_system_labels(_rules(hook), admitted, read_version=True)


def _graded_targets(hook: dict) -> tuple:
    """The rows this webhook is graded on: the node path always, the bootstrap-policy writes
    when its `namespaceSelector` admits `kube-system` or `kube-public`."""
    if bootstrap_namespaces_admitted(hook):
        return UPGRADE_PATH_TARGETS + CONTROL_PLANE_KUBE_SYSTEM_WRITES
    return UPGRADE_PATH_TARGETS


def _pinned_labels(rules: list[dict], hook: dict) -> list[str]:
    """The graded writes `rules` name only at a version the server does not serve, labelled."""
    labels = _upgrade_path_labels(rules, read_version=False)
    admitted = bootstrap_namespaces_admitted(hook)
    if admitted:
        labels += _kube_system_labels(rules, admitted, read_version=False)
    return labels


def _rule_version_pinned(rule: dict, targets: tuple) -> bool:
    """Whether the server sends this rule nothing because of its `apiVersions`: it names a
    graded write (it would reach one of `targets` with the version check off) at a version
    other than the one the server serves it at, and names nothing the list cannot vouch for,
    so no wildcard group or resource and no resource off the list, whose served versions this
    rule does not know. Decided per rule, so a webhook that pairs a pinned rule with a live
    one is described as failing the live rule's requests, not as sent nothing."""
    if {WILDCARD, VERSION_V1} & set(rule.get("apiVersions") or []):
        return False
    groups = rule.get("apiGroups") or []
    specs = [spec for spec in rule.get("resources") or [] if isinstance(spec, str)]
    if not groups or not specs or WILDCARD in groups or any(WILDCARD in spec for spec in specs):
        return False
    on_path = {(group, resource) for group, _, resource, _, _ in targets}
    if not all((group, spec) in on_path for group in groups for spec in specs):
        return False
    return any(_rule_reaches(rule, group, None, resource, operation, scope) for group, _, resource, operation, scope in targets)


def version_pinned_rules(hook: dict) -> list[dict]:
    """The rules of this webhook the server sends nothing (`_rule_version_pinned`), judged
    against the rows the webhook is graded on."""
    targets = _graded_targets(hook)
    return [rule for rule in _rules(hook) if _rule_version_pinned(rule, targets)]


def grade_webhooks(configs: list[dict], services: list[dict], slices: list[dict]) -> dict:
    """Fail-closed webhooks whose backend is unreachable, split by whether they break an upgrade.

    `blocking`: the webhook's rules can match a write in `UPGRADE_PATH_TARGETS`, one the
    drain or the node join makes and does not proceed without, so the upgrade cannot
    complete while its backend is down; or its `namespaceSelector` admits `kube-system` or
    `kube-public` and its rules match a Role or RoleBinding write a new master's start-up reconciles there
    (`CONTROL_PLANE_KUBE_SYSTEM_WRITES`), so the control-plane upgrade cannot complete either.
    `outage`: the backend is unreachable but the rules match none of those; its requests fail now and the member is not graded on it, with
    what it does match named so the operator can judge it, because the list is what this
    rule knows of the path, not a proof of safety. An outage finding whose rules name a
    path write only at a version the server does not serve carries those writes in
    `version_pinned` and those rules in `pinned_rules`, the rest in `live_rules`: the server
    sends the pinned rules none of them, and the cell says so of the webhook when every rule
    is pinned, and of the pinned rule alone when a live rule sits beside it.
    Fail-open webhooks are counted
    (`fail_open`), and so are fail-closed webhooks with a URL backend (`url_backends`),
    which nothing read here can check.
    """
    result = {"blocking": [], "outage": [], "evaluated": 0, "fail_open": 0, "url_backends": 0}
    for config in configs:
        config_kind = config.get("kind")
        config_name = (config.get("metadata") or {}).get("name", "")
        for hook in config.get("webhooks") or []:
            if not isinstance(hook, dict):
                continue
            if hook.get("failurePolicy", FAILURE_POLICY_FAIL) != FAILURE_POLICY_FAIL:
                result["fail_open"] += 1
                continue
            service_ref = (hook.get("clientConfig") or {}).get("service")
            if not isinstance(service_ref, dict):
                result["url_backends"] += 1
                continue
            result["evaluated"] += 1
            reason = backend_problem(service_ref, services, slices)
            if reason is None:
                continue
            matches = upgrade_path_matches(hook) or kube_system_write_matches(hook)
            pinned = [] if matches else version_pinned_rules(hook)
            finding = {
                "webhook": WEBHOOK_NAME_FORMAT.format(config=config_name, webhook=hook.get("name", "")),
                "config_kind": config_kind,
                "config": config_name,
                "name": hook.get("name", ""),
                "service": WEBHOOK_SERVICE_FORMAT.format(namespace=service_ref.get("namespace", ""), name=service_ref.get("name", "")),
                "reason": reason,
                "upgrade_path": matches,
                "version_pinned": _pinned_labels(pinned, hook),
                "rules": describe_rules(hook),
                "pinned_rules": [describe_rule(rule) for rule in pinned],
                "live_rules": [describe_rule(rule) for rule in _rules(hook) if not any(rule is p for p in pinned)],
            }
            result["blocking" if matches else "outage"].append(finding)
    return result


def describe_rule(rule: dict) -> str:
    """One rule as `OP/OP resource,resource[ in group,group][ at version,version]`, so a cell
    that says the webhook is outside the upgrade's path also says what it does match, and at
    which versions when the rule pins them."""
    operations = RULE_OPERATION_JOIN.join(str(o) for o in rule.get("operations") or [])
    resources = RULE_RESOURCE_JOIN.join(str(r) for r in rule.get("resources") or [])
    text = RULE_FORMAT.format(operations=operations, resources=resources).strip()
    groups = [str(g) for g in rule.get("apiGroups") or []]
    if any(groups):
        text = RULE_GROUP_FORMAT.format(rule=text, groups=RULE_RESOURCE_JOIN.join(g or RULE_CORE_GROUP_NAME for g in groups))
    versions = [str(v) for v in rule.get("apiVersions") or []]
    if versions and versions != [WILDCARD]:
        text = RULE_VERSIONS_FORMAT.format(rule=text, versions=RULE_RESOURCE_JOIN.join(versions))
    return text


def describe_rules(hook: dict) -> list[str]:
    """Each of the webhook's rules, as `describe_rule` renders it."""
    return [describe_rule(rule) for rule in _rules(hook)]


def describe_webhook_finding(finding: dict) -> str:
    rules = LIST_SEPARATOR.join(finding.get("rules") or []) or RULE_NONE
    if finding["upgrade_path"]:
        matches = LIST_SEPARATOR.join(finding["upgrade_path"])
    elif finding.get("version_pinned") and finding.get("live_rules"):
        matches = WEBHOOK_MIXED_MATCHES.format(
            rules=rules,
            live=LIST_SEPARATOR.join(finding["live_rules"]),
            pinned=LIST_SEPARATOR.join(finding["version_pinned"]),
            served=VERSION_V1,
            pinned_rules=LIST_SEPARATOR.join(finding.get("pinned_rules") or []),
        )
    elif finding.get("version_pinned"):
        matches = WEBHOOK_PINNED_MATCHES.format(rules=rules, pinned=LIST_SEPARATOR.join(finding["version_pinned"]), served=VERSION_V1)
    else:
        matches = WEBHOOK_OUTAGE_MATCHES.format(rules=rules)
    return WEBHOOK_FINDING_FORMAT.format(webhook=finding["webhook"], config_kind=finding["config_kind"], reason=finding["reason"], matches=matches)


# ------------------------------------------------------------------------- verdict


def readiness_status(pdbs: dict | None, webhooks: dict | None, maintenance: dict, skew: dict, target_known: bool) -> str:
    """`blocked` beats `unknown` beats `ready`: a definite blocker is reported whatever else
    could not be evaluated, and a member is `ready` only when every rule was evaluated."""
    if (pdbs and pdbs["blocking"]) or (webhooks and webhooks["blocking"]) or maintenance["blocking_exclusions"] or skew["blocking"]:
        return READINESS_BLOCKED
    if pdbs is None or webhooks is None or not target_known or maintenance["undecided_exclusions"] or skew["unknown"]:
        return READINESS_UNKNOWN
    return READINESS_READY
