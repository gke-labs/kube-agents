"""
Entry 4 of docs/designs/upgrade-failure-catalogue.md, data on the node: a rebuilt node is a
new machine, and what a workload kept on the old one is gone.

The rule reads Deployments and StatefulSets and lists state, not scratch: a StatefulSet's
`emptyDir` (its identity survives the rebuild, its node-local data does not), a `hostPath` a
container mounts writable, a `hostPath` under a Local SSD mount, or a claim (or a
StatefulSet's claim template) on a StorageClass whose provisioner is
`kubernetes.io/no-provisioner`, how Local SSD becomes a PersistentVolume, is a `risk`, named
with the volume. A Deployment's `emptyDir`, which every reschedule empties and the application
rebuilds, a memory-backed `emptyDir`, a `hostPath` mounted read-only and a `hostPath` declared
but mounted by no container are notes. DaemonSets
(per-node agents by design) and CronJobs (transient pods) are not read. When the StorageClass
or claim read failed and a template mounts a claim, the claim's class is `unknown`; when the
PDB read's workloads were not read at all, the rule is one `unknown` and grades nothing. Nothing
here blocks: a rebuild loses the data and GKE does not stop for it, so `CAN_BLOCK` is False.

Sources: docs.cloud.google.com/kubernetes-engine/docs/concepts/local-ssd (data on Local SSD
does not persist across a node upgrade); the catalogue's entry 4.
"""

import readiness_rules as rules
import upgrade_shape_tables as tables

RULE_ID = "data-on-the-node"
ENTRY = 4
CAN_BLOCK = False
KINDS = ("Deployment", "StatefulSet")
# The reads this rule needs beyond the PDB read, as `read_errors` keys them.
CLAIM_READ_KIND = "pvc"
STORAGE_CLASS_READ_KIND = "storageclass"
VOLUME_EMPTY_DIR = "emptyDir"
VOLUME_HOST_PATH = "hostPath"
VOLUME_CLAIM = "persistentVolumeClaim"
MEDIUM_MEMORY = "Memory"
EMPTY_DIR_TEXT = "{name} (emptyDir{stateful})"
EMPTY_DIR_MEMORY_TEXT = "{name} (emptyDir, medium Memory)"
HOST_PATH_TEXT = "{name} (hostPath {path}{ssd}{stateful})"
HOST_PATH_READ_ONLY_TEXT = "{name} (hostPath {path}, mounted read-only)"
HOST_PATH_UNMOUNTED_TEXT = "{name} (hostPath {path}, declared, mounted by no container)"
HOST_PATH_SSD_TEXT = ", a Local SSD mount"
CLAIM_TEXT = "{name} (claim {claim} on local StorageClass {storage_class})"
CLAIM_TEMPLATE_TEXT = "{name} (volumeClaimTemplate on local StorageClass {storage_class})"
STATEFUL_NAME_TEXT = "; the name suggests state"
DETAIL = "keeps data on the node in {volumes}; a node rebuild loses it{statefulset}"
STATEFULSET_TEXT = ", and a StatefulSet's identity survives the rebuild while its node-local data does not"
NOTE_SCRATCH = "{kind} {object} keeps scratch on the node in {volumes}; not a risk: a Deployment's emptyDir is emptied by every reschedule and a read-only or memory-backed mount holds nothing of the workload's"
STORAGE_UNREAD_DETAIL = "mounts claim(s) {claims}; the {kind} read failed ({reason}), so whether a claim is on a local class is unknown"
WORKLOADS_NOT_READ = "Deployments and StatefulSets not read ({reason}); nothing graded"


def _local_storage_classes(items: list) -> set[str]:
    return {
        (sc.get("metadata") or {}).get("name", "")
        for sc in rules.items_of_kind(items, rules.KIND_STORAGE_CLASS)
        if sc.get("provisioner") == tables.LOCAL_VOLUME_PROVISIONER
    }


def _claims(items: list) -> dict[tuple[str, str], dict]:
    out = {}
    for pvc in rules.items_of_kind(items, rules.KIND_PERSISTENT_VOLUME_CLAIM):
        meta = pvc.get("metadata") or {}
        out[(meta.get("namespace", ""), meta.get("name", ""))] = pvc
    return out


def _stateful(name: str) -> str:
    return STATEFUL_NAME_TEXT if tables.STATEFUL_VOLUME_NAME_RE.search(name or "") else ""


def _mounts(spec: dict) -> tuple[set[str], set[str]]:
    """(volume names some container mounts, those at least one mounts without `readOnly: true`)."""
    mounted, writable = set(), set()
    for container in rules.containers(spec):
        for mount in container.get("volumeMounts") or []:
            if isinstance(mount, dict) and mount.get("name"):
                mounted.add(mount["name"])
                if mount.get("readOnly") is not True:
                    writable.add(mount["name"])
    return mounted, writable


def _unknown(obj: dict, detail: str, **extra) -> dict:
    finding = rules.new_finding(RULE_ID, ENTRY, rules.GRADE_UNKNOWN, obj, detail, **extra)
    finding["reason"] = rules.describe(finding)
    return finding


def node_local_volumes(obj: dict, spec: dict, workload: dict, local_classes: set[str], claims: dict) -> tuple[list[str], list[str], list[str], list[str]]:
    """(risks, scratch, claim names, claim template names) for one template: the volumes that
    hold state on the node, the ones that hold scratch, the claims mounted (whose class the PVC
    and StorageClass reads resolve) and the claim templates (whose class the template names, so
    the StorageClass read alone resolves it)."""
    risks, scratch, claim_names, template_names = [], [], [], []
    mounted, writable = _mounts(spec)
    for volume in spec.get("volumes") or []:
        if not isinstance(volume, dict):
            continue
        name = volume.get("name") or ""
        if VOLUME_EMPTY_DIR in volume:
            if (volume.get(VOLUME_EMPTY_DIR) or {}).get("medium") == MEDIUM_MEMORY:
                scratch.append(EMPTY_DIR_MEMORY_TEXT.format(name=name))
            elif obj["kind"] == rules.KIND_STATEFULSET:
                risks.append(EMPTY_DIR_TEXT.format(name=name, stateful=_stateful(name)))
            else:
                scratch.append(EMPTY_DIR_TEXT.format(name=name, stateful=_stateful(name)))
        elif VOLUME_HOST_PATH in volume:
            path = (volume.get(VOLUME_HOST_PATH) or {}).get("path") or ""
            local_ssd = path.startswith(tables.LOCAL_SSD_HOSTPATH_PREFIXES)
            if local_ssd or name in writable:
                ssd = HOST_PATH_SSD_TEXT if local_ssd else ""
                risks.append(HOST_PATH_TEXT.format(name=name, path=path, ssd=ssd, stateful=_stateful(name)))
            elif name in mounted:
                scratch.append(HOST_PATH_READ_ONLY_TEXT.format(name=name, path=path))
            else:
                scratch.append(HOST_PATH_UNMOUNTED_TEXT.format(name=name, path=path))
        elif VOLUME_CLAIM in volume:
            claim_name = (volume.get(VOLUME_CLAIM) or {}).get("claimName") or ""
            claim_names.append(claim_name)
            claim = claims.get((obj["namespace"], claim_name)) or {}
            storage_class = (claim.get("spec") or {}).get("storageClassName")
            if storage_class in local_classes:
                risks.append(CLAIM_TEXT.format(name=name, claim=claim_name, storage_class=storage_class))
    for template in (workload.get("spec") or {}).get("volumeClaimTemplates") or []:
        if not isinstance(template, dict):
            continue
        storage_class = (template.get("spec") or {}).get("storageClassName")
        template_name = (template.get("metadata") or {}).get("name", "")
        template_names.append(template_name)
        if storage_class in local_classes:
            risks.append(CLAIM_TEMPLATE_TEXT.format(name=template_name, storage_class=storage_class))
    return risks, scratch, claim_names, template_names


def evaluate(cluster: dict, member: dict, read, target, context: dict) -> dict:
    result = rules.new_result()
    workloads_error = rules.read_error(read, rules.PDB_WORKLOAD_KINDS[0])
    if workloads_error:
        result["unknown"].append(rules.rule_unknown(RULE_ID, ENTRY, WORKLOADS_NOT_READ.format(reason=workloads_error)))
        return result
    items = rules.read_items(read)
    storage_error = rules.read_error(read, STORAGE_CLASS_READ_KIND)
    claim_error = rules.read_error(read, CLAIM_READ_KIND)
    local_classes = _local_storage_classes(items)
    claims = _claims(items)
    for obj, spec, workload in rules.templates(items, KINDS):
        risks, scratch, claim_names, template_names = node_local_volumes(obj, spec, workload, local_classes, claims)
        if risks:
            statefulset = STATEFULSET_TEXT if obj["kind"] == rules.KIND_STATEFULSET else ""
            detail = DETAIL.format(volumes=rules.LIST_SEPARATOR.join(risks), statefulset=statefulset)
            result["risks"].append(rules.new_finding(RULE_ID, ENTRY, rules.GRADE_RISK, obj, detail, volumes=risks))
        if scratch:
            result["notes"].append(NOTE_SCRATCH.format(kind=obj["kind"], object=obj["object"], volumes=rules.LIST_SEPARATOR.join(scratch)))
        # A mounted claim's class needs the PVC read and the StorageClass read; a claim template
        # names its class, so only the StorageClass read decides whether it is local.
        unresolved = (claim_names if (claim_error or storage_error) else []) + (template_names if storage_error else [])
        if unresolved:
            kind, reason = (STORAGE_CLASS_READ_KIND, storage_error) if storage_error else (CLAIM_READ_KIND, claim_error)
            detail = STORAGE_UNREAD_DETAIL.format(claims=rules.LIST_SEPARATOR.join(unresolved), kind=kind, reason=reason)
            result["unknown"].append(_unknown(obj, detail, claims=unresolved))
    return result


def describe(entry: dict) -> str:
    return rules.describe(entry)
