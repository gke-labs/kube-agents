"""
Entry 17 of docs/designs/upgrade-failure-catalogue.md, a node networking agent fails on the
new image: a per-node agent on the node's own network that depends on the node image, through
a selector on a node-image label (`NODE_IMAGE_LABELS`), a mount of the kernel's module tree or
the network plugin's directories (`KERNEL_MODULE_HOST_PATHS`, `CNI_HOST_PATHS`), or a command
that loads a kernel module (`KERNEL_MODULE_COMMANDS`, as a word of the container's command or
args or of a script they hand a shell through `-c`, read with the package's
`shell_scripts` and `shell_words`), breaks when a node upgrade changes the image under it.

Every such DaemonSet with `hostNetwork: true` outside the system namespaces is a `risk`, named
with its couplings; the detail names the pools below the target, whose image this upgrade
changes, or says no pool is below it yet. Nothing here blocks: GKE rebuilds the node and the
agent fails afterwards, so `CAN_BLOCK` is False. The DaemonSets come from the workload read;
when that read failed the rule is `unknown`, which stays in the note.

Sources: kubernetes.io/docs/concepts/extend-kubernetes/compute-storage-net/network-plugins/
(the CNI directories); the catalogue's entry 17 (the Reddit 1.24 outage).
"""

import readiness_rules as rules
import upgrade_shape_tables as tables

RULE_ID = "node-image-coupled-agent"
ENTRY = 17
CAN_BLOCK = False
KINDS = ("DaemonSet",)
# The read this rule needs beyond the PDB read, as `read_errors` keys it.
DAEMONSET_READ_KIND = "daemonset"
VOLUME_HOST_PATH = "hostPath"
LABEL_COUPLING = "selects on node-image label {label}"
MOUNT_COUPLING = "mounts {path} from the node"
COMMAND_COUPLING = "runs {program}"
DETAIL = "on the node's network and coupled to the node image: {couplings}; {pools}"
POOLS_BELOW_TEXT = "pool(s) {pools} are below the target and get a new node image in this upgrade"
NO_POOL_BELOW_TEXT = "no pool is below the target; the next node upgrade changes the image under it"
NO_TARGET_TEXT = "which pools change image needs a target"
WORKLOADS_UNREAD_DETAIL = "DaemonSets not read ({reason}); nothing graded"


def couplings(spec: dict) -> list[str]:
    out = []
    for selector in rules.selectors(spec):
        if selector["key"] in tables.NODE_IMAGE_LABELS:
            out.append(LABEL_COUPLING.format(label=selector["key"]))
    for volume in spec.get("volumes") or []:
        if not isinstance(volume, dict) or VOLUME_HOST_PATH not in volume:
            continue
        path = (volume.get(VOLUME_HOST_PATH) or {}).get("path") or ""
        if path.startswith(tables.KERNEL_MODULE_HOST_PATHS + tables.CNI_HOST_PATHS):
            out.append(MOUNT_COUPLING.format(path=path))
    for container in rules.containers(spec):
        command = (container.get("command") or []) + (container.get("args") or [])
        words = [str(w).rsplit(rules.PATH_SEPARATOR, 1)[-1] for w in command]
        for script in rules.shell_scripts(command):
            words.extend(rules.shell_words(script))
        for program in tables.KERNEL_MODULE_COMMANDS:
            if program in words:
                out.append(COMMAND_COUPLING.format(program=program))
                break
    return list(dict.fromkeys(out))


def evaluate(cluster: dict, member: dict, read, target, context: dict) -> dict:
    result = rules.new_result()
    unread = rules.read_error(read, DAEMONSET_READ_KIND)
    if unread:
        result["unknown"].append(rules.rule_unknown(RULE_ID, ENTRY, WORKLOADS_UNREAD_DETAIL.format(reason=unread)))
        return result
    for obj, spec, _ in rules.templates(rules.read_items(read), KINDS):
        if spec.get("hostNetwork") is not True:
            continue
        found = couplings(spec)
        if not found:
            continue
        pools = rules.pools_for_template(spec, context.get(rules.CONTEXT_POOLS) or [])
        below = rules.pools_below_target(pools, target)
        if target is None:
            pools_text = NO_TARGET_TEXT
        elif below:
            pools_text = POOLS_BELOW_TEXT.format(pools=rules.LIST_SEPARATOR.join(below))
        else:
            pools_text = NO_POOL_BELOW_TEXT
        detail = DETAIL.format(couplings=rules.LIST_SEPARATOR.join(found), pools=pools_text)
        result["risks"].append(rules.new_finding(RULE_ID, ENTRY, rules.GRADE_RISK, obj, detail, couplings=found, pools_below_target=below))
    return result


def describe(entry: dict) -> str:
    return rules.describe(entry)
