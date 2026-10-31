"""
Entry 16 of docs/designs/upgrade-failure-catalogue.md: the network dataplane changes.

On the legacy dataplane a NetworkPolicy is enforced only while the network policy add-on
is on, and the add-on takes effect at the node rebuild an upgrade performs; Dataplane V2
enforces every policy, and a cluster moves to it only by being recreated. So the shape
that breaks is a legacy cluster with policy enforcement off and NetworkPolicy objects in
its namespaces: GKE's recommender files it as `NETWORK_POLICIES_UNRECONCILED`, and the
policies start to apply the moment enforcement turns on, at the rebuild that follows
enabling the add-on or on the Dataplane V2 cluster that replaces this one. The rule reads
`networkConfig.datapathProvider` (absent means legacy), the add-on
(`networkPolicy.enabled`, which `addonsConfig.networkPolicyConfig.disabled` turns off)
and the NetworkPolicy objects, and reports that shape as a risk naming the namespaces
and their policy counts, with the clusters in the run already on Dataplane V2 where a
rehearsal would enforce the same policies. Every other legacy cluster gets a note stating
its dataplane and enforcement, with its policies enforced by the add-on or with none to
enforce, so a report states the fact for every legacy member; a Dataplane V2 cluster is
nothing. `CAN_BLOCK` is False: nothing here stops the upgrade, so a legacy member whose
NetworkPolicies were not read is reported in the note and leaves the verdict to the
other rules.

Sources: https://docs.cloud.google.com/kubernetes-engine/docs/concepts/dataplane-v2 (the
no-migration rule) and the recommender's network-policy insight,
https://docs.cloud.google.com/kubernetes-engine/docs/how-to/optimize-with-recommenders.
"""

from readiness_rules import CONTEXT_CLUSTERS, KIND_NETWORK_POLICY, LIST_SEPARATOR, items_of_kind, new_result, read_error, read_items

RULE_ID = "network-dataplane"
ENTRY = 16
CAN_BLOCK = False

# The read kind this rule needs, as `read_errors` keys it.
NETWORK_POLICY_READ_KIND = "networkpolicy"
# `networkConfig.datapathProvider` values. A record without the field is on the legacy
# dataplane: the API sets the field only when Dataplane V2 was chosen.
NETWORK_CONFIG_KEY = "networkConfig"
DATAPATH_PROVIDER_KEY = "datapathProvider"
DATAPATH_ADVANCED = "ADVANCED_DATAPATH"
DATAPATH_LEGACY = "LEGACY_DATAPATH"
NETWORK_POLICY_KEY = "networkPolicy"
NETWORK_POLICY_ENABLED_KEY = "enabled"
ADDONS_CONFIG_KEY = "addonsConfig"
NETWORK_POLICY_CONFIG_KEY = "networkPolicyConfig"
NETWORK_POLICY_DISABLED_KEY = "disabled"
NAMESPACE_COUNT_FORMAT = "{namespace} ({count})"

FINDING_TEXT = (
    "legacy dataplane with policy enforcement off and NetworkPolicies in {namespaces}: the policies apply nowhere today "
    "and start to apply the moment enforcement turns on, at the node rebuild that follows enabling the network policy add-on "
    "or on a Dataplane V2 cluster{peers}; test what they deny before either"
)
PEERS_TEXT = " (in the listing: {peers}, each enforcing its own policies)"
NOTE_ENFORCED = "legacy dataplane; NetworkPolicies in {namespaces} are enforced by the network policy add-on"
NOTE_NO_POLICIES = "legacy dataplane, policy enforcement {enforcement}; network policies: none, nothing to enforce"
ENFORCEMENT_ON = "on (network policy add-on)"
ENFORCEMENT_OFF = "off"
UNKNOWN_READ = "legacy dataplane; NetworkPolicies not read ({error}), so whether any apply nowhere today is unknown"


def datapath_provider(cluster: dict) -> str:
    return str((cluster.get(NETWORK_CONFIG_KEY) or {}).get(DATAPATH_PROVIDER_KEY) or DATAPATH_LEGACY)


def policy_enforced(cluster: dict) -> bool:
    enabled = bool((cluster.get(NETWORK_POLICY_KEY) or {}).get(NETWORK_POLICY_ENABLED_KEY))
    disabled = bool(((cluster.get(ADDONS_CONFIG_KEY) or {}).get(NETWORK_POLICY_CONFIG_KEY) or {}).get(NETWORK_POLICY_DISABLED_KEY))
    return enabled and not disabled


def policies_by_namespace(items) -> dict[str, int]:
    counts: dict[str, int] = {}
    for policy in items_of_kind(items, KIND_NETWORK_POLICY):
        namespace = (policy.get("metadata") or {}).get("namespace", "")
        counts[namespace] = counts.get(namespace, 0) + 1
    return dict(sorted(counts.items()))


def _namespaces_text(namespaces: dict[str, int]) -> str:
    return LIST_SEPARATOR.join(NAMESPACE_COUNT_FORMAT.format(namespace=ns, count=count) for ns, count in namespaces.items())


def evaluate(cluster: dict, member: dict, read, target, context) -> dict:
    out = new_result()
    if datapath_provider(cluster) == DATAPATH_ADVANCED:
        return out
    error = read_error(read, NETWORK_POLICY_READ_KIND)
    if error:
        out["unknown"].append(UNKNOWN_READ.format(error=error))
        return out
    namespaces = policies_by_namespace(read_items(read))
    enforced = policy_enforced(cluster)
    if not namespaces:
        out["notes"].append(NOTE_NO_POLICIES.format(enforcement=ENFORCEMENT_ON if enforced else ENFORCEMENT_OFF))
        return out
    if enforced:
        out["notes"].append(NOTE_ENFORCED.format(namespaces=_namespaces_text(namespaces)))
        return out
    # The Dataplane V2 members of the listing the run made (the projects' whole listing, even
    # when `--cluster` narrows who is graded). The member itself is never among them: it is on
    # the legacy dataplane, or the rule returned above. Two clusters named alike in two
    # projects are two records, so a V2 twin of this member is a peer.
    peers = sorted(
        str(other.get("name", ""))
        for other in ((context or {}).get(CONTEXT_CLUSTERS) or [])
        if isinstance(other, dict) and datapath_provider(other) == DATAPATH_ADVANCED
    )
    out["risks"].append({"datapath": datapath_provider(cluster), "enforced": False, "namespaces": namespaces, "policies": sum(namespaces.values()), "peers_on_v2": peers})
    return out


def describe(finding: dict) -> str:
    peers = PEERS_TEXT.format(peers=LIST_SEPARATOR.join(finding["peers_on_v2"])) if finding["peers_on_v2"] else ""
    return FINDING_TEXT.format(namespaces=_namespaces_text(finding["namespaces"]), peers=peers)
