"""
Entry 19 of docs/designs/upgrade-failure-catalogue.md: in-tree volumes lose their CSI
path.

A PersistentVolume written against the in-tree `gcePersistentDisk` plugin attaches only
through CSI migration to the PD CSI driver, so on a cluster whose
`gcePersistentDiskCsiDriverConfig` add-on is off it cannot attach: the pod a node
upgrade reschedules stays Pending. The rule reads every PersistentVolume with an in-tree
source, the claim bound to it, and the pods and templates that mount that claim. A claim
counts only while it is bound to this volume: the volume `Bound` and the claim its
`claimRef` names carrying `spec.volumeName` for it (the phase alone when the claims were
not read). A `Released` volume keeps the `claimRef` of a claim since deleted, and a claim
recreated under that name binds to another volume, so its consumers mount that one; such
a stale `claimRef` is a note naming where the claim went, and the volume is graded as
unmounted. With the add-on off, a volume a workload mounts is blocking (the attach fails
when the drain moves the pod) and an unmounted one a risk; with the add-on on, every
in-tree volume is a risk, since it still depends on the migration shim rather than on
the driver. The rule needs the PersistentVolumes (`unknown` without them) and the Pods
(`unknown` without them, grading what the templates show).

Sources: https://kubernetes.io/blog/2022/09/26/storage-in-tree-to-csi-migration-status-update-1.25/
and https://cloud.google.com/kubernetes-engine/docs/how-to/persistent-volumes/gce-pd-csi-driver
"""

from readiness_rules import (
    KIND_CRONJOB,
    KIND_DAEMONSET,
    KIND_DEPLOYMENT,
    KIND_PERSISTENT_VOLUME,
    KIND_PERSISTENT_VOLUME_CLAIM,
    KIND_STATEFULSET,
    LIST_SEPARATOR,
    POD_READ_KIND,
    active_pods,
    claim_names,
    items_of_kind,
    new_result,
    owner_label,
    pod_owner,
    read_error,
    read_items,
    template_specs,
)

RULE_ID = "in-tree-volumes"
ENTRY = 19
CAN_BLOCK = True

# The read kinds this rule needs besides the Pods, as `read_errors` keys them: the volumes, and
# the claims that tell a live `claimRef` from a stale one.
PV_READ_KIND = "pv"
PVC_READ_KIND = "pvc"
PHASE_BOUND = "Bound"
VOLUME_NAME_KEY = "volumeName"
CONSUMER_KINDS = (KIND_DEPLOYMENT, KIND_STATEFULSET, KIND_DAEMONSET, KIND_CRONJOB)
IN_TREE_SOURCE_KEY = "gcePersistentDisk"
DISK_NAME_KEY = "pdName"
ADDONS_CONFIG_KEY = "addonsConfig"
CSI_ADDON_KEY = "gcePersistentDiskCsiDriverConfig"
CSI_ADDON_ENABLED_KEY = "enabled"
CSI_DRIVER = "pd.csi.storage.gke.io"
CLAIM_FORMAT = "{namespace}/{name}"
STALE_CLAIM_FORMAT = "{claim} (stale)"
NO_CLAIM = "unbound"

VOLUME_TEXT = "PersistentVolume {pv} (in-tree gcePersistentDisk {disk}, claim {claim}, {phase})"
BLOCKING_TEXT = "{volume} with the PD CSI driver add-on off: the attach fails when a drain moves {workloads}; enable the add-on"
RISK_ADDON_OFF_TEXT = "{volume} with the PD CSI driver add-on off: nothing mounts it now, and it cannot attach until the add-on is on"
RISK_ADDON_ON_TEXT = "{volume} attaches through CSI migration{workloads}; move it to {driver}"
RISK_WORKLOADS_TEXT = " for {workloads}"
PHASE_UNKNOWN = "phase unknown"
NOTE_STALE_CLAIM = "PersistentVolume {pv} ({phase}) still names claim {claim}, {where}; the workloads mounting that claim do not mount this volume"
STALE_BOUND_ELSEWHERE = "which is bound to PersistentVolume {volume}"
STALE_CLAIM_GONE = "which no longer exists"
STALE_NOT_BOUND = "which is not bound to it"
UNKNOWN_PV_UNREAD = "PersistentVolumes not read ({error}), so in-tree volumes are unknown"
UNKNOWN_PODS_UNREAD = "Pods not read ({error}), so a pod mounting an in-tree volume outside a template is unseen"


def csi_addon_enabled(cluster: dict) -> bool:
    return bool(((cluster.get(ADDONS_CONFIG_KEY) or {}).get(CSI_ADDON_KEY) or {}).get(CSI_ADDON_ENABLED_KEY))


def in_tree_volumes(items) -> list[dict]:
    return [pv for pv in items_of_kind(items, KIND_PERSISTENT_VOLUME) if isinstance((pv.get("spec") or {}).get(IN_TREE_SOURCE_KEY), dict)]


def claim_stale(items, pv_name: str, namespace: str, claim: str, phase, claims_read: bool) -> str:
    """Why the volume's `claimRef` is not a live binding, or "" when it is: the claim named is
    bound to another volume, no longer exists (when the claims were read), or the volume is not
    `Bound` to it (a `Released` volume keeps the claimRef of a claim since deleted)."""
    pvc = next((c for c in items_of_kind(items, KIND_PERSISTENT_VOLUME_CLAIM) if ((c.get("metadata") or {}).get("namespace", ""), (c.get("metadata") or {}).get("name", "")) == (namespace, claim)), None)
    if pvc is None and claims_read:
        return STALE_CLAIM_GONE
    volume = (pvc.get("spec") or {}).get(VOLUME_NAME_KEY) if pvc else None
    if volume and volume != pv_name:
        return STALE_BOUND_ELSEWHERE.format(volume=volume)
    if phase != PHASE_BOUND:
        return STALE_NOT_BOUND
    return ""


def claim_consumers(items, namespace: str, claim: str) -> list[str]:
    """Owners of the pods and templates that mount the claim, each once."""
    owners = set()
    for pod in active_pods(items):
        if (pod.get("metadata") or {}).get("namespace", "") == namespace and claim in claim_names(pod.get("spec") or {}):
            owners.add(owner_label(pod_owner(pod)))
    for owner, spec in template_specs(items, CONSUMER_KINDS):
        if owner["namespace"] == namespace and claim in claim_names(spec):
            owners.add(owner_label(owner))
    return sorted(owners)


def evaluate(cluster: dict, member: dict, read, target, context) -> dict:
    out = new_result()
    pv_error = read_error(read, PV_READ_KIND)
    if pv_error:
        out["unknown"].append(UNKNOWN_PV_UNREAD.format(error=pv_error))
        return out
    items = read_items(read)
    volumes = in_tree_volumes(items)
    pods_error = read_error(read, POD_READ_KIND)
    if pods_error and volumes:
        out["unknown"].append(UNKNOWN_PODS_UNREAD.format(error=pods_error))
    addon_on = csi_addon_enabled(cluster)
    claims_read = not read_error(read, PVC_READ_KIND)
    for pv in volumes:
        meta = pv.get("metadata") or {}
        spec = pv.get("spec") or {}
        claim_ref = spec.get("claimRef") or {}
        phase = (pv.get("status") or {}).get("phase")
        claim = None
        live = False
        workloads: list[str] = []
        if isinstance(claim_ref, dict) and claim_ref.get("name"):
            claim = CLAIM_FORMAT.format(namespace=claim_ref.get("namespace", ""), name=claim_ref.get("name", ""))
            stale = claim_stale(items, meta.get("name", ""), claim_ref.get("namespace", ""), claim_ref.get("name", ""), phase, claims_read)
            live = not stale
            if live:
                workloads = claim_consumers(items, claim_ref.get("namespace", ""), claim_ref.get("name", ""))
            else:
                out["notes"].append(NOTE_STALE_CLAIM.format(pv=meta.get("name", ""), phase=phase or PHASE_UNKNOWN, claim=claim, where=stale))
        finding = {
            "pv": meta.get("name", ""),
            "disk": (spec.get(IN_TREE_SOURCE_KEY) or {}).get(DISK_NAME_KEY, ""),
            "claim": claim,
            "claim_live": live,
            "phase": phase,
            "workloads": workloads,
            "addon_enabled": addon_on,
        }
        if not addon_on and workloads:
            out["blocking"].append(finding)
        else:
            out["risks"].append(finding)
    return out


def describe(finding: dict) -> str:
    claim = finding["claim"] or NO_CLAIM
    if finding["claim"] and not finding.get("claim_live", True):
        claim = STALE_CLAIM_FORMAT.format(claim=finding["claim"])
    volume = VOLUME_TEXT.format(pv=finding["pv"], disk=finding["disk"], claim=claim, phase=finding["phase"] or PHASE_UNKNOWN)
    workloads = LIST_SEPARATOR.join(finding["workloads"])
    if not finding["addon_enabled"]:
        if workloads:
            return BLOCKING_TEXT.format(volume=volume, workloads=workloads)
        return RISK_ADDON_OFF_TEXT.format(volume=volume)
    return RISK_ADDON_ON_TEXT.format(volume=volume, workloads=RISK_WORKLOADS_TEXT.format(workloads=workloads) if workloads else "", driver=CSI_DRIVER)
