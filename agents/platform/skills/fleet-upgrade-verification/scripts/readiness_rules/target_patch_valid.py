"""
Entry 10 of docs/designs/upgrade-failure-catalogue.md, on the version side: what the cluster
runs, and what it is about to run, against what its channel serves. A patch version can be
withdrawn between the plan and the window; the withdrawn patch stays selectable, and a node
pool upgrade to it fails on an image the provider no longer serves. The rule reads the
location's server config (the report's `server_config` read, `gcloud container
get-server-config`, once per location) and grades two things against the cluster's own
channel's `validVersions`: the report's target version for the member, a blocker when it is
in neither that list nor `validNodeVersions`; and the running control-plane version, a risk
when the channel's list has moved past it. A cluster on no channel is graded against
`validMasterVersions`, the list a static-version cluster takes versions from. The action
names the newest active patch of the version's minor on the channel.

The risk is deliberately not exempted when the running version is still in another channel's
`validVersions` or in `validMasterVersions`: that exemption is the one
`patch_readiness.py`'s `_still_offered` applies to the Monday audit's `master-behind` check,
where it keeps a staggered rollout from reading as unsupported, and it is what hid the
incident this rule exists for: a pool upgrade to the running patch fails once the cluster's
own channel has dropped it, whatever another channel still lists.
"""

import re

from readiness_rules import (
    CONTEXT_LOCATION,
    CONTEXT_TARGET_TEXT,
    LOCATION_READ_SERVER_CONFIG,
    new_result,
    project_read,
)

RULE_ID = "target-patch-valid"
ENTRY = 10
CAN_BLOCK = True

# The server config's fields and the cluster record's channel.
CHANNELS_KEY = "channels"
CHANNEL_KEY = "channel"
VALID_VERSIONS_KEY = "validVersions"
VALID_MASTER_VERSIONS_KEY = "validMasterVersions"
VALID_NODE_VERSIONS_KEY = "validNodeVersions"
RELEASE_CHANNEL_KEY = "releaseChannel"
CURRENT_MASTER_VERSION_KEY = "currentMasterVersion"
NO_CHANNEL_VALUES = ("", "UNSPECIFIED")
NO_CHANNEL_LABEL = "no channel"
# `MAJOR.MINOR.PATCH-gke.BUILD`, the shape every list carries; a version without the suffix is build 0.
VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:-gke\.(\d+))?$")
VERSION_PARTS = 4
MINOR_PARTS = 2
KIND_TARGET = "target"
KIND_CONTROL_PLANE = "control-plane"
# The action names an active version to move to: the newest patch of the version's own minor at
# or above it (a channel can still list an older patch of the minor it has moved past, which is
# no target), else the lowest active version above it, else nothing the channel lists.
ACTION_NEWEST_FORMAT = "target the newest active patch of the minor on {channel}: {newest}"
ACTION_NEXT_FORMAT = "no active patch of {minor} at or above it on {channel}; the next active version is {next}"
ACTION_NONE_FORMAT = "no active version at or above it on {channel}"
MINOR_FORMAT = "{major}.{minor}"
TARGET_FINDING_FORMAT = (
    "target {version} is in neither the {channel} channel's validVersions nor validNodeVersions at {location}: "
    "a withdrawn patch stays selectable and a node pool upgrade to it fails on an image the provider no longer serves; {action}"
)
CONTROL_PLANE_FINDING_FORMAT = (
    "control plane {version} is not among the {channel} channel's validVersions at {location} "
    "(the channel has moved past it, whatever another channel still lists); {action}"
)
NOTE_FORMAT = "channel {channel}; target {target} {target_verdict}; control plane {master} {master_verdict} in validVersions at {location}"
TARGET_VALID = "valid"
TARGET_WITHDRAWN = "withdrawn"
TARGET_NONE = "none"
MASTER_LISTED = "listed"
MASTER_NOT_LISTED = "not listed"
MASTER_NONE = "none"
SERVER_CONFIG_NOT_A_DICT = "the server-config read returned {type}, not a dict"
CHANNEL_NOT_IN_CONFIG = "channel {channel} is not in get-server-config for {location}, so the valid versions are unknown"
EMPTY_ROSTER_REASON = "get-server-config for {location} lists no {field} for {channel}, so the valid versions are unknown"


def parse_version(text) -> tuple | None:
    if not isinstance(text, str):
        return None
    m = VERSION_RE.match(text.strip())
    if not m:
        return None
    return tuple(int(part or 0) for part in m.groups())


def channel_roster(server_config: dict, channel: str) -> tuple[list[str] | None, str]:
    """(validVersions of the cluster's channel, the field read), or (validMasterVersions, that field)
    for a cluster on no channel; None when the channel is not in the config."""
    if channel in NO_CHANNEL_VALUES:
        return list(server_config.get(VALID_MASTER_VERSIONS_KEY) or []), VALID_MASTER_VERSIONS_KEY
    for entry in server_config.get(CHANNELS_KEY) or []:
        if isinstance(entry, dict) and entry.get(CHANNEL_KEY) == channel:
            return list(entry.get(VALID_VERSIONS_KEY) or []), VALID_VERSIONS_KEY
    return None, VALID_VERSIONS_KEY


def active_versions_above(version_text: str, roster: list[str]) -> tuple[str | None, str | None]:
    """(the newest version in `roster` of `version_text`'s minor at or above it, the lowest version
    in `roster` above it), each None when the roster has none; (None, None) for a version that
    does not parse."""
    version = parse_version(version_text)
    if version is None:
        return None, None
    newest, newest_text, lowest, lowest_text = None, None, None, None
    for candidate in roster:
        parsed = parse_version(candidate)
        if parsed is None or parsed < version:
            continue
        if parsed[:MINOR_PARTS] == version[:MINOR_PARTS] and (newest is None or parsed > newest):
            newest, newest_text = parsed, candidate
        if parsed > version and (lowest is None or parsed < lowest):
            lowest, lowest_text = parsed, candidate
    return newest_text, lowest_text


def action_text(finding: dict) -> str:
    """The move the finding recommends, from its `newest` and `next_version`."""
    if finding.get("newest"):
        return ACTION_NEWEST_FORMAT.format(channel=finding["channel"], newest=finding["newest"])
    version = parse_version(finding.get("version")) or (0, 0)
    minor = MINOR_FORMAT.format(major=version[0], minor=version[1])
    if finding.get("next_version"):
        return ACTION_NEXT_FORMAT.format(minor=minor, channel=finding["channel"], next=finding["next_version"])
    return ACTION_NONE_FORMAT.format(channel=finding["channel"])


def evaluate(cluster: dict, member: dict, read, target, context) -> dict:
    out = new_result()
    server_config, error = project_read(context, LOCATION_READ_SERVER_CONFIG)
    if error is not None:
        out["unknown"].append(error)
        return out
    if not isinstance(server_config, dict):
        out["unknown"].append(SERVER_CONFIG_NOT_A_DICT.format(type=type(server_config).__name__))
        return out
    location = str(context.get(CONTEXT_LOCATION) or cluster.get("location") or "")
    channel = str((cluster.get(RELEASE_CHANNEL_KEY) or {}).get(CHANNEL_KEY) or "")
    channel_label = channel if channel not in NO_CHANNEL_VALUES else NO_CHANNEL_LABEL
    roster, field = channel_roster(server_config, channel)
    if roster is None:
        out["unknown"].append(CHANNEL_NOT_IN_CONFIG.format(channel=channel, location=location))
        return out
    if not roster:
        out["unknown"].append(EMPTY_ROSTER_REASON.format(location=location, field=field, channel=channel_label))
        return out
    node_versions = list(server_config.get(VALID_NODE_VERSIONS_KEY) or [])
    target_text = context.get(CONTEXT_TARGET_TEXT)
    master_text = cluster.get(CURRENT_MASTER_VERSION_KEY)
    target_verdict = TARGET_NONE
    if target_text:
        if target_text in roster or target_text in node_versions:
            target_verdict = TARGET_VALID
        else:
            target_verdict = TARGET_WITHDRAWN
            newest, next_version = active_versions_above(target_text, roster)
            out["blocking"].append({"kind": KIND_TARGET, "version": target_text, "channel": channel_label, "location": location, "newest": newest, "next_version": next_version})
    master_verdict = MASTER_NONE
    if master_text:
        if master_text in roster:
            master_verdict = MASTER_LISTED
        else:
            master_verdict = MASTER_NOT_LISTED
            newest, next_version = active_versions_above(master_text, roster)
            out["risks"].append({"kind": KIND_CONTROL_PLANE, "version": master_text, "channel": channel_label, "location": location, "newest": newest, "next_version": next_version})
    out["notes"].append(NOTE_FORMAT.format(channel=channel_label, target=target_text or TARGET_NONE, target_verdict=target_verdict, master=master_text or MASTER_NONE, master_verdict=master_verdict, location=location))
    return out


def describe(finding: dict) -> str:
    action = action_text(finding)
    template = TARGET_FINDING_FORMAT if finding.get("kind") == KIND_TARGET else CONTROL_PLANE_FINDING_FORMAT
    return template.format(version=finding["version"], channel=finding["channel"], location=finding["location"], action=action)
