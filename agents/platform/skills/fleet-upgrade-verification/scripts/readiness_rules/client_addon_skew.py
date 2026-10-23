#!/usr/bin/env python3
"""
Entry 10 of docs/designs/upgrade-failure-catalogue.md: a client or an add-on outside the
range the target minor supports.

Clients. kubectl is supported within one minor, older or newer, of kube-apiserver
(`upgrade_shape_tables.KUBECTL_SKEW_SOURCE`). The rule reads kubectl versions from two
places and grades each against the target minor: the `kubectl/vX.Y.Z` user agents in the
shared deprecated-or-kubectl audit read (`audit_log.py`; the Admin Activity log carries
writes, so a kubectl that only reads is not there; the read's records are per API, so one
principal with one user agent is folded into one client naming the APIs it wrote), and the image tags of workloads that
run a kubectl binary, from the PDB read (Deployments, StatefulSets) and the namespaced read
(DaemonSets, CronJobs). A kubectl further than one minor from the target is a risk naming
the version and the principal or workload. A client-go user agent carries the binary's
version, not the library's, so those callers are counted in the note, never graded.

Add-ons. The cluster record's `addonsConfig` names the GKE-managed add-ons that are on; it
exposes no version for them and GKE owns their lifecycle, so they are listed in the note and
not graded. Third-party add-ons are recognised by image repository in the same workload
reads; one whose vendor table (`upgrade_shape_tables.ADDON_SUPPORT`) does not cover the
target is a risk. One with no table, or at a release the table does not list, is a note
("version not graded") that changes no verdict: a missing row in this table is not the
cluster's problem. Nothing is inferred. `CAN_BLOCK` is False: a skewed client or add-on
breaks after the upgrade, not the upgrade, so the rule's `unknown` (a read that failed) goes
in the note and leaves the verdict to the other rules.
"""

import re

import readiness_rules
import upgrade_shape_tables as tables
from readiness_rules import audit_log, finding

RULE_ID = "client-addon-skew"
ENTRY = 10
CAN_BLOCK = False

# `kubectl/v1.29.0 (linux/amd64) kubernetes/3f7a50f`: the user agent kubectl sends.
KUBECTL_USER_AGENT_RE = re.compile(r"^kubectl/v?(\d+)\.(\d+)")
# A client-go built binary ends its user agent with `kubernetes/<commit>`.
CLIENT_GO_USER_AGENT_MARKER = " kubernetes/"
# `registry.k8s.io/kubectl:v1.29.0` -> repository and tag; a digest is dropped.
IMAGE_DIGEST_SEPARATOR = "@"
IMAGE_TAG_SEPARATOR = ":"
IMAGE_PATH_SEPARATOR = "/"
DOCKER_HUB_PREFIX = "docker.io/"
CONTAINER_LISTS = ("containers", "initContainers")
IMAGE_KEY = "image"
# The kinds of the namespaced read this rule's image grading needs; the PDB read's
# Deployments and StatefulSets come with every read.
NEEDED_READ_KINDS = ("cronjob", "daemonset")
# The two finding kinds this rule files.
KIND_CLIENT = "client"
KIND_ADDON = "addon"
# `addonsConfig` blocks that spell "on" as the absence of `disabled: true` (an empty block
# is on); every other block, `dnsCacheConfig` among them, spells it `enabled: true`, as the
# GKE API's cluster_service.proto defines them.
ADDONS_CONFIG_KEY = "addonsConfig"
DISABLED_STYLE_ADDONS = (
    "httpLoadBalancing",
    "horizontalPodAutoscaling",
    "kubernetesDashboard",
    "networkPolicyConfig",
    "cloudRunConfig",
    "istioConfig",
)
ADDON_ENABLED_KEY = "enabled"
ADDON_DISABLED_KEY = "disabled"

AUDIT_READ_FAILED = "kubectl user agents: {reason}"
OBJECTS_NOT_READ = "cluster objects not read ({reason}); kubectl images and add-ons not graded"
KIND_NOT_READ = "{kind}s not read ({reason}); their kubectl images and add-ons not graded"
NOTE_NO_TARGET = "no target; {count} kubectl client{plural} not graded against a skew window"
CLIENT_NAME = "kubectl"
CLIENT_FROM_AUDIT = "principal {principal} (user agent {user_agent}, {count} write{plural} to {apis} in {days}d)"
CLIENT_FROM_IMAGE = "{workload} (image {image})"
CLIENT_SKEW_DETAIL = (
    "{gap} minor{plural} {direction} the target {target}; kubectl is supported within "
    "{allowed} minor of kube-apiserver ({source})"
)
CLIENT_MAJOR_DETAIL = "major version differs from the target {target}"
DIRECTION_BEHIND = "behind"
DIRECTION_AHEAD = "ahead of"
MINOR_PLURAL = "s"
CALL_PLURAL = "s"
ADDON_UNSUPPORTED_DETAIL = "{addon} {version} supports Kubernetes {low} to {high} ({source}, read {read_on}); the target {target} is outside it"
NOTE_ADDON_NO_TABLE = "{addon} {version} at {where}: version not graded (no support table with a source here; check the vendor's matrix against {target})"
NOTE_ADDON_RELEASE_NOT_LISTED = "{addon} {version} at {where}: version not graded (release not in the table read on {read_on}, {source}; check it against {target})"
NOTE_ADDON_NO_TARGET = "{addon} {version} at {where}: version not graded (no target)"
NOTE_GKE_ADDONS = "GKE add-ons on: {addons} (GKE-managed; the cluster record carries no version, so not graded)"
NOTE_CLIENT_GO = "{count} client-go caller{plural} among the deprecated-API writers the audit read returns, whose user agent carries the binary's version, not the library's; not graded"
NOTE_ADDON_OK = "{addon} {version} supports the target {target} ({source})"
NOTE_CLIENTS_OK = "{count} kubectl client{plural} within one minor of the target"
LIST_SEPARATOR = ", "
NO_TARGET_TEXT = "(no target)"
DESCRIBE_CLIENT = "{client} {version} from {where}: {detail}"
DESCRIBE_ADDON = "{addon} {version} at {where}: {detail}"
VERSION_FORMAT = "v{major}.{minor}"


def _image_parts(image: str) -> tuple[str, str]:
    """(repository, tag) for an image reference; the digest is dropped."""
    reference = str(image or "").split(IMAGE_DIGEST_SEPARATOR, 1)[0]
    repository, tag = reference, ""
    last = reference.rsplit(IMAGE_PATH_SEPARATOR, 1)[-1]
    if IMAGE_TAG_SEPARATOR in last:
        repository, tag = reference.rsplit(IMAGE_TAG_SEPARATOR, 1)
    return repository, tag


def _addon_name(repository: str) -> str | None:
    if repository in tables.ADDON_IMAGES:
        return tables.ADDON_IMAGES[repository]
    if repository.startswith(DOCKER_HUB_PREFIX) and repository[len(DOCKER_HUB_PREFIX) :] in tables.ADDON_IMAGES:
        return tables.ADDON_IMAGES[repository[len(DOCKER_HUB_PREFIX) :]]
    return None


def workload_images(items: list) -> list[tuple[str, str]]:
    """(workload label, image) for every container of every workload kind read."""
    found = []
    for item in items or []:
        spec = finding.pod_spec(item)
        if spec is None:
            continue
        label = finding.workload_label(item)
        for list_key in CONTAINER_LISTS:
            for container in spec.get(list_key) or []:
                if isinstance(container, dict) and container.get(IMAGE_KEY):
                    found.append((label, str(container[IMAGE_KEY])))
    return found


def enabled_gke_addons(cluster: dict) -> list[str]:
    """The `addonsConfig` keys whose block reads as on."""
    config = cluster.get(ADDONS_CONFIG_KEY) if isinstance(cluster, dict) else None
    enabled = []
    for key, block in sorted((config or {}).items()):
        if not isinstance(block, dict):
            continue
        if ADDON_ENABLED_KEY in block:
            on = bool(block.get(ADDON_ENABLED_KEY))
        elif ADDON_DISABLED_KEY in block:
            on = not block.get(ADDON_DISABLED_KEY)
        else:
            on = key in DISABLED_STYLE_ADDONS
        if on:
            enabled.append(key)
    return enabled


def _client_finding(version: tuple, where: str, target_minor: tuple) -> dict | None:
    """A risk for a kubectl at `version` against the target, or None when inside the window."""
    version_text = VERSION_FORMAT.format(major=version[0], minor=version[1])
    base = {"tier": finding.TIER_RISK, "kind": KIND_CLIENT, "client": CLIENT_NAME, "version": version_text, "where": where, "target": tables.format_minor(target_minor)}
    if version[0] != target_minor[0]:
        return {**base, "gap_minors": None, "detail": CLIENT_MAJOR_DETAIL.format(target=base["target"])}
    gap = target_minor[1] - version[1]
    if abs(gap) <= tables.KUBECTL_SKEW_MINORS:
        return None
    detail = CLIENT_SKEW_DETAIL.format(
        gap=abs(gap),
        plural="" if abs(gap) == 1 else MINOR_PLURAL,
        direction=DIRECTION_BEHIND if gap > 0 else DIRECTION_AHEAD,
        target=base["target"],
        allowed=tables.KUBECTL_SKEW_MINORS,
        source=tables.KUBECTL_SKEW_SOURCE,
    )
    return {**base, "gap_minors": gap, "detail": detail}


def _addon_verdict(addon: str, version: tuple, where: str, target_minor: tuple | None, result: dict) -> None:
    version_text = VERSION_FORMAT.format(major=version[0], minor=version[1])
    support = tables.ADDON_SUPPORT.get(addon)
    target_text = tables.format_minor(target_minor) if target_minor else NO_TARGET_TEXT
    if support is None:
        finding.add_note(result, NOTE_ADDON_NO_TABLE.format(addon=addon, version=version_text, where=where, target=target_text))
        return
    span = support["releases"].get(version)
    if span is None:
        finding.add_note(result, NOTE_ADDON_RELEASE_NOT_LISTED.format(addon=addon, version=version_text, where=where, read_on=support["read_on"], source=support["source"], target=target_text))
        return
    if target_minor is None:
        finding.add_note(result, NOTE_ADDON_NO_TARGET.format(addon=addon, version=version_text, where=where))
        return
    low, high = span
    if low <= target_minor <= high:
        finding.add_note(result, NOTE_ADDON_OK.format(addon=addon, version=version_text, target=target_text, source=support["source"]))
        return
    result[finding.RESULT_RISKS].append(
        {
            "tier": finding.TIER_RISK,
            "kind": KIND_ADDON,
            "addon": addon,
            "version": version_text,
            "where": where,
            "supported": (tables.format_minor(low), tables.format_minor(high)),
            "source": support["source"],
            "detail": ADDON_UNSUPPORTED_DETAIL.format(addon=addon, version=version_text, low=tables.format_minor(low), high=tables.format_minor(high), source=support["source"], read_on=support["read_on"], target=target_text),
        }
    )


def evaluate(cluster: dict, member: dict, read, target, context: dict) -> dict:
    result = finding.empty_result()
    target_minor = (target[0], target[1]) if target else None
    clients: list[tuple[tuple, str]] = []
    client_go: set[tuple[str, str]] = set()

    log = audit_log.read_deprecated(context)
    if log["error"]:
        result[finding.RESULT_UNKNOWN].append(finding.unknown(RULE_ID, AUDIT_READ_FAILED.format(reason=log["error"])))
    # The read's records are per (principal, user agent, API); one binary is one client, so the
    # records of a principal and user agent fold into one, its writes summed and its APIs listed,
    # and a client-go binary is counted once however many APIs it wrote.
    kubectl_callers: dict[tuple[str, str], dict] = {}
    for caller in log["callers"]:
        if caller["caller_class"] == audit_log.CLASS_PROVIDER:
            continue
        match = KUBECTL_USER_AGENT_RE.match(str(caller["user_agent"]))
        if match:
            folded = kubectl_callers.setdefault((str(caller["principal"]), str(caller["user_agent"])), {"version": (int(match.group(1)), int(match.group(2))), "count": 0, "apis": set()})
            folded["count"] += caller["count"]
            folded["apis"].add(str(caller["api"]))
        elif CLIENT_GO_USER_AGENT_MARKER in str(caller["user_agent"]):
            client_go.add((str(caller["principal"]), str(caller["user_agent"])))
    for (principal, user_agent), folded in kubectl_callers.items():
        where = CLIENT_FROM_AUDIT.format(principal=principal, user_agent=user_agent, count=folded["count"], plural="" if folded["count"] == 1 else CALL_PLURAL, apis=LIST_SEPARATOR.join(sorted(folded["apis"])), days=log["window_days"])
        clients.append((folded["version"], where))
    if log["sampled"]:
        finding.add_note(result, audit_log.sampled_note(log))

    if read is None or read.get("items") is None:
        result[finding.RESULT_UNKNOWN].append(finding.unknown(RULE_ID, OBJECTS_NOT_READ.format(reason=readiness_rules.read_error(read, NEEDED_READ_KINDS[0]))))
    else:
        for kind, error in readiness_rules.unread_kinds(read, NEEDED_READ_KINDS).items():
            result[finding.RESULT_UNKNOWN].append(finding.unknown(RULE_ID, KIND_NOT_READ.format(kind=kind, reason=error)))
    for workload, image in workload_images(readiness_rules.read_items(read)):
        repository, tag = _image_parts(image)
        version = tables.parse_minor(tag)
        if repository.rsplit(IMAGE_PATH_SEPARATOR, 1)[-1] in tables.KUBECTL_IMAGE_NAMES and version is not None:
            clients.append((version, CLIENT_FROM_IMAGE.format(workload=workload, image=image)))
            continue
        addon = _addon_name(repository)
        if addon is not None and version is not None:
            _addon_verdict(addon, version, CLIENT_FROM_IMAGE.format(workload=workload, image=image), target_minor, result)

    if target_minor is None:
        if clients:
            finding.add_note(result, NOTE_NO_TARGET.format(count=len(clients), plural="" if len(clients) == 1 else MINOR_PLURAL))
    else:
        within = 0
        for version, where in clients:
            item = _client_finding(version, where, target_minor)
            if item is None:
                within += 1
            else:
                result[finding.RESULT_RISKS].append(item)
        if within:
            finding.add_note(result, NOTE_CLIENTS_OK.format(count=within, plural="" if within == 1 else MINOR_PLURAL))

    if client_go:
        finding.add_note(result, NOTE_CLIENT_GO.format(count=len(client_go), plural="" if len(client_go) == 1 else MINOR_PLURAL))
    addons = enabled_gke_addons(cluster)
    if addons:
        finding.add_note(result, NOTE_GKE_ADDONS.format(addons=LIST_SEPARATOR.join(addons)))
    return result


def describe(item: dict) -> str:
    if item.get("kind") == KIND_ADDON:
        return DESCRIBE_ADDON.format(addon=item["addon"], version=item["version"], where=item["where"], detail=item["detail"])
    return DESCRIBE_CLIENT.format(client=item["client"], version=item["version"], where=item["where"], detail=item["detail"])
