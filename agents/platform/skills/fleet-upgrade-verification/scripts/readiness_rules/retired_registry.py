"""
Entry 20 of docs/designs/upgrade-failure-catalogue.md, images on a retired registry: a
rebuilt node has no image cache and pulls every image again.

A host in `RETIRED_IMAGE_HOSTS` stopped publishing; it still answers a pull today,
`k8s.gcr.io` by a redirect to registry.k8s.io, so a template naming one is a `risk` to watch
rather than a blocker: the pull works until the redirect or the read-only project stops, and
`CAN_BLOCK` is False. Every template outside the system namespaces whose container or init
container names such a host is listed, with the image, the host and what the host does
today; the detail names the pools below the target, whose nodes this upgrade rebuilds, or
says no pool is below it yet. The rule needs the DaemonSets and CronJobs of the workload
read; when that read failed it grades the Deployments and StatefulSets it has and says so.

Sources: kubernetes.io/blog/2023/02/06/k8s-gcr-io-freeze-announcement/ and
kubernetes.io/blog/2023/03/10/image-registry-redirect/ (the freeze and the redirect);
helm.sh/blog/helm-2-becomes-unsupported/ (Helm 2's registry).
"""

import readiness_rules as rules
import upgrade_shape_tables as tables

RULE_ID = "retired-registry"
ENTRY = 20
CAN_BLOCK = False
# The reads this rule needs beyond the PDB read, as `read_errors` keys them.
TEMPLATE_READ_KINDS = ("daemonset", "cronjob")
IMAGE_TEXT = "container {container} pulls {image} from {host} ({note})"
DETAIL = "{images}; a rebuilt node pulls it again from a host that stopped publishing; {pools}"
POOLS_BELOW_TEXT = "pool(s) {pools} are below the target and are rebuilt in this upgrade"
NO_POOL_BELOW_TEXT = "no pool is below the target; the next node upgrade pulls it again"
NO_TARGET_TEXT = "which pools are rebuilt needs a target"


def retired_host(image: str) -> str | None:
    for host in tables.RETIRED_IMAGE_HOSTS:
        if image.startswith(host):
            return host
    return None


def evaluate(cluster: dict, member: dict, read, target, context: dict) -> dict:
    result = rules.new_result()
    items = rules.read_items(read)
    unread_detail = rules.workloads_unread_detail(read, TEMPLATE_READ_KINDS)
    if unread_detail:
        result["unknown"].append(rules.rule_unknown(RULE_ID, ENTRY, unread_detail))
    for obj, spec, _ in rules.templates(items):
        images = []
        hosts = []
        for container in rules.containers(spec):
            image = container.get("image") or ""
            host = retired_host(image)
            if host is None:
                continue
            hosts.append(host)
            images.append(IMAGE_TEXT.format(container=container.get("name", ""), image=image, host=host.rstrip(rules.PATH_SEPARATOR), note=tables.RETIRED_IMAGE_HOST_NOTES.get(host, "")))
        if not images:
            continue
        pools = rules.pools_for_template(spec, context.get(rules.CONTEXT_POOLS) or [])
        below = rules.pools_below_target(pools, target)
        if target is None:
            pools_text = NO_TARGET_TEXT
        elif below:
            pools_text = POOLS_BELOW_TEXT.format(pools=rules.LIST_SEPARATOR.join(below))
        else:
            pools_text = NO_POOL_BELOW_TEXT
        detail = DETAIL.format(images=rules.LIST_SEPARATOR.join(images), pools=pools_text)
        result["risks"].append(rules.new_finding(RULE_ID, ENTRY, rules.GRADE_RISK, obj, detail, hosts=hosts, pools_below_target=below))
    return result


def describe(entry: dict) -> str:
    return rules.describe(entry)
