"""
Entry 12 of docs/designs/upgrade-failure-catalogue.md, a node label removed: a template that
requires a label the rebuilt nodes no longer carry can never schedule again.

Only the terms that select nodes carrying a label are read (`nodeSelector`, and required
affinity with `In` or `Exists`); `NotIn`, `DoesNotExist`, `Gt`, `Lt` and preferred terms never
strand a pod and are never graded. A label (or label value) in `DROPPED_NODE_LABELS` is
`blocking` only when the upgrade crosses the drop for a pool the template can land on: the
pool's current minor is below the drop and the target at or past it. A drop every such pool
has passed already is a note: the pods either run or are already unschedulable, and neither
is the upgrade's doing; so is a drop after this upgrade's target (a later upgrade's), and a
selector naming a pool the cluster record lacks; a pool whose version did not parse is
`unknown`, since whether it crosses the drop cannot be read. A label that no pool's `config.labels` defines and no node in the
template's pools carries today is a `risk`, named as such: nothing in the cluster's
configuration sets it, so the rebuilt nodes carry it no more. A key GKE sets from the pool's
configuration (`GKE_NODE_LABEL_PREFIXES`: the pool, accelerator, OS and machine labels) or one
a pool's `config.labels` defines is a pool shape, graded by the rules that read pools, and
never this rule's risk even with no node today: a GPU selector with no GPU pool is entry 18's
finding, not a removed label. One of the deprecated beta labels the kubelet still sets is a
`risk` named with its GA replacement. The carried question is not asked on Autopilot (GKE
provisions the nodes) nor for a pool that is empty today and scales from zero, and it is
`unknown` when the Node read failed. The rule can block, so `CAN_BLOCK` is True and its
unknowns move the member's verdict.

Sources: kubernetes/kubernetes#107533 (kubeadm stops setting the master label in 1.24);
docs.cloud.google.com/kubernetes-engine/docs/deprecations/docker-containerd (GKE 1.24 removes
Docker node images); kubernetes.io/docs/reference/labels-annotations-taints/ (the deprecated
beta labels).
"""

import readiness_rules as rules
import upgrade_shape_tables as tables

RULE_ID = "removed-node-label"
ENTRY = 12
CAN_BLOCK = True
# The reads this rule needs beyond the PDB read, as `read_errors` keys them.
TEMPLATE_READ_KINDS = ("daemonset", "cronjob")
VALUE_FORMAT = "{key}={value}"
ANY_VALUE_FORMAT = "{key}"
DROPPED_DETAIL = "selects on {label} ({where}), dropped at {minor}: {source}; pool(s) {pools} cross the drop in this upgrade to {target}, and the rebuilt nodes carry it no more"
DROPPED_NO_TARGET_DETAIL = "selects on {label} ({where}), dropped at {minor}: {source}; whether a pool crosses the drop needs a target"
DROPPED_PASSED_NOTE = "{kind} {object} selects on {label} ({where}), dropped at {minor}, which {pools} passed already; the pods either run or are already unschedulable, and the upgrade changes neither"
DROPPED_AFTER_TARGET_NOTE = "{kind} {object} selects on {label} ({where}), dropped at {minor}, after this upgrade's target {target}; a later upgrade crosses the drop"
DROPPED_NO_POOL_NOTE = "{kind} {object} selects on {label} ({where}), dropped at {minor}; {scope} not in the cluster record, so no pool crosses the drop in this upgrade"
DROPPED_VERSION_UNKNOWN_DETAIL = "selects on {label} ({where}), dropped at {minor}; the version of pool(s) {pools} did not parse, so whether they cross the drop in this upgrade is unknown"
SCOPE_NO_POOL_RECORD = "the cluster record lists no pool, so none is"
SCOPE_NAMED_POOLS_MISSING = "pool(s) {pools} are"
NOT_CARRIED_DETAIL = "selects on {label} ({where}): no pool's labels define it and no node in {scope} carries it today; the rebuilt nodes carry it no more"
# Where a pool's configuration defines its nodes' labels (NodeConfig.labels).
POOL_LABELS_KEY = "labels"
DEPRECATED_DETAIL = "selects on {label} ({where}), deprecated since {since} in favour of {replacement}; the kubelet still sets it today, and a node image that stops setting it strands the pods"
DEPRECATED_PREFIX_DETAIL = "selects on {label} ({where}), a beta-prefixed label Kubernetes has deprecated; a node image that stops setting it strands the pods"
NODES_UNREAD_DETAIL = "selects on {label} ({where}); the Node read failed ({reason}), so whether a node carries it is unknown"
EMPTY_SCOPE_NOTE = "{kind} {object} selects on {label} ({where}) and {scope} has no node today{zero}; not graded as stranded"
SCALES_FROM_ZERO_TEXT = " (the pool scales from zero)"
SCOPE_POOLS = "pool(s) {pools}"
SCOPE_POOLS_MISSING = "pool(s) {pools} (not in the cluster record)"
SCOPE_CLUSTER = "the cluster"
POOLS_ALL = "every pool"


def _label_text(key: str, values: list) -> str:
    return VALUE_FORMAT.format(key=key, value=values[0]) if len(values) == 1 else ANY_VALUE_FORMAT.format(key=key)


def _dropped_row(key: str, operator: str, values: list):
    """The DROPPED_NODE_LABELS row a positive selector hits: the key with any value, or the key
    with the dropped value among an `In` term's values."""
    for label, value, minor, source in tables.DROPPED_NODE_LABELS:
        if label != key:
            continue
        if value is None or (operator == rules.OP_IN and value in values):
            return label, value, minor, source
    return None


def _pool_defined(key: str, values: list, pools: list[dict]) -> bool:
    """Whether the label is one GKE sets from the pool's configuration (a key under
    GKE_NODE_LABEL_PREFIXES) or one a pool's `config.labels` defines with a selected value."""
    if key.startswith(tables.GKE_NODE_LABEL_PREFIXES):
        return True
    for pool in pools:
        labels = rules.pool_config(pool).get(POOL_LABELS_KEY)
        if isinstance(labels, dict) and key in labels and (not values or str(labels[key]) in values):
            return True
    return False


def _carried(key: str, values: list, node_list: list[dict]) -> bool:
    for node in node_list:
        labels = (node.get("metadata") or {}).get("labels") or {}
        if key not in labels:
            continue
        if not values or labels[key] in values:
            return True
    return False


def _unknown(obj: dict, detail: str, **extra) -> dict:
    finding = rules.new_finding(RULE_ID, ENTRY, rules.GRADE_UNKNOWN, obj, detail, **extra)
    finding["reason"] = rules.describe(finding)
    return finding


def evaluate(cluster: dict, member: dict, read, target, context: dict) -> dict:
    result = rules.new_result()
    items = rules.read_items(read)
    unread_detail = rules.workloads_unread_detail(read, TEMPLATE_READ_KINDS)
    if unread_detail:
        result["unknown"].append(rules.rule_unknown(RULE_ID, ENTRY, unread_detail))
    nodes_failed = rules.read_error(read, rules.NODE_READ_KIND)
    autopilot = bool(context.get(rules.CONTEXT_AUTOPILOT))
    all_nodes = rules.items_of_kind(items, rules.KIND_NODE)
    for obj, spec, _ in rules.templates(items):
        pools = rules.pools_for_template(spec, context.get(rules.CONTEXT_POOLS) or [])
        pool_names = sorted(p.get("name", "") for p in pools)
        named = rules.selected_values(spec, tables.NODEPOOL_LABEL)
        node_list = [n for n in all_nodes if not named or rules.node_pool(n) in pool_names]
        if named and not pools:
            scope = SCOPE_POOLS_MISSING.format(pools=rules.LIST_SEPARATOR.join(named))
        elif named:
            scope = SCOPE_POOLS.format(pools=rules.LIST_SEPARATOR.join(pool_names))
        else:
            scope = SCOPE_CLUSTER
        seen = set()
        for selector in rules.selectors(spec):
            key, operator, values, where = selector["key"], selector["operator"], selector["values"], selector["where"]
            if operator not in rules.POSITIVE_OPERATORS or (key, operator, tuple(values)) in seen:
                continue
            seen.add((key, operator, tuple(values)))
            text = _label_text(key, values)
            common = {"kind": obj["kind"], "object": obj["object"], "label": text, "where": where}
            dropped = _dropped_row(key, operator, values)
            if dropped is not None:
                label, value, minor, source = dropped
                drop_text = _label_text(label, [value] if value is not None else [])
                minor_text = rules.minor_text(minor)
                if target is None:
                    detail = DROPPED_NO_TARGET_DETAIL.format(label=drop_text, where=where, minor=minor_text, source=source)
                    result["unknown"].append(_unknown(obj, detail, label=key, value=value))
                    continue
                target_minor = tuple(target[:2])
                crossing = [p.get("name", "") for p in pools if p.get("parsed") is not None and p["parsed"][:2] < minor <= target_minor]
                unparsed = [p.get("name", "") for p in pools if p.get("parsed") is None]
                if crossing:
                    detail = DROPPED_DETAIL.format(label=drop_text, where=where, minor=minor_text, source=source, pools=rules.LIST_SEPARATOR.join(crossing), target=rules.minor_text(target))
                    result["blocking"].append(rules.new_finding(RULE_ID, ENTRY, rules.GRADE_BLOCKING, obj, detail, label=key, value=value, dropped_at=minor_text, pools_crossing=crossing))
                elif target_minor < minor:
                    result["notes"].append(DROPPED_AFTER_TARGET_NOTE.format(minor=minor_text, target=rules.minor_text(target), **{**common, "label": drop_text}))
                elif not pools:
                    missing = SCOPE_NAMED_POOLS_MISSING.format(pools=rules.LIST_SEPARATOR.join(named)) if named else SCOPE_NO_POOL_RECORD
                    result["notes"].append(DROPPED_NO_POOL_NOTE.format(minor=minor_text, scope=missing, **{**common, "label": drop_text}))
                elif unparsed:
                    detail = DROPPED_VERSION_UNKNOWN_DETAIL.format(label=drop_text, where=where, minor=minor_text, pools=rules.LIST_SEPARATOR.join(unparsed))
                    result["unknown"].append(_unknown(obj, detail, label=key, value=value))
                else:
                    passed = SCOPE_POOLS.format(pools=rules.LIST_SEPARATOR.join(pool_names)) if named else POOLS_ALL
                    result["notes"].append(DROPPED_PASSED_NOTE.format(minor=minor_text, pools=passed, **{**common, "label": drop_text}))
                continue
            if not autopilot and not _pool_defined(key, values, pools):
                if nodes_failed:
                    detail = NODES_UNREAD_DETAIL.format(label=text, where=where, reason=nodes_failed)
                    result["unknown"].append(_unknown(obj, detail, label=key))
                    continue
                if not node_list:
                    zero = SCALES_FROM_ZERO_TEXT if any(rules.pool_scales_from_zero(p) for p in pools) else ""
                    result["notes"].append(EMPTY_SCOPE_NOTE.format(scope=scope, zero=zero, **common))
                    continue
                if not _carried(key, values, node_list):
                    detail = NOT_CARRIED_DETAIL.format(label=text, where=where, scope=scope)
                    result["risks"].append(rules.new_finding(RULE_ID, ENTRY, rules.GRADE_RISK, obj, detail, label=key))
                    continue
            if key in tables.DEPRECATED_NODE_LABELS:
                replacement, since = tables.DEPRECATED_NODE_LABELS[key]
                detail = DEPRECATED_DETAIL.format(label=text, where=where, since=since, replacement=replacement)
                result["risks"].append(rules.new_finding(RULE_ID, ENTRY, rules.GRADE_RISK, obj, detail, label=key, replacement=replacement))
            elif key.startswith(tables.DEPRECATED_NODE_LABEL_PREFIXES):
                detail = DEPRECATED_PREFIX_DETAIL.format(label=text, where=where)
                result["risks"].append(rules.new_finding(RULE_ID, ENTRY, rules.GRADE_RISK, obj, detail, label=key))
    return result


def describe(entry: dict) -> str:
    return rules.describe(entry)
