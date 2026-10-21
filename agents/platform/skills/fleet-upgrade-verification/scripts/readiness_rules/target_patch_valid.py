"""
Entry 25 of docs/designs/upgrade-failure-catalogue.md: the target patch is withdrawn before the
window. What the cluster runs, and what it is about to run, against what its channel serves. A patch version can be
withdrawn between the plan and the window; the withdrawn patch stays selectable, and a node
pool upgrade to it fails on an image the provider no longer serves. The rule reads the
location's server config (the report's `server_config` read, `gcloud container
get-server-config`, once per location) and grades two things against the cluster's own
channel's `validVersions`: the report's target version for the member, a blocker when the
channel's list does not carry it and the control plane has to move to it (the target is above
the running control-plane version), or when neither that list nor `validNodeVersions` carries it
and only pools move (the control plane is at or above it; the node list serves a pool move
alone, and a cluster on a channel takes a control-plane version only from its channel's list); a
target given as `MAJOR.MINOR.PATCH` with no `-gke.` build, which the API reads as that patch's
newest listed build, is in a list when the list carries a build of it, and a full version is
matched exactly; and the running control-plane version, a risk when the channel's list has
moved past it. A cluster on no channel is graded against
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
    HOLDS_BOTH,
    LOCATION_READ_SERVER_CONFIG,
    new_result,
    project_read,
)

RULE_ID = "target-patch-valid"
ENTRY = 25
CAN_BLOCK = True
HOLDS = HOLDS_BOTH  # a withdrawn patch is no target for the control plane or a pool

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
# What separates the patch from the build: a target without it names every build of the patch.
BUILD_SEPARATOR = "-gke."
VERSION_PARTS = 4
MINOR_PARTS = 2
KIND_TARGET = "target"
KIND_CONTROL_PLANE = "control-plane"
# Which move the target asks of the member: the control plane's (it is above the running
# control-plane version, so the channel's own list has to carry it) or the pools' alone.
MOVE_CONTROL_PLANE = "control-plane move"
MOVE_POOLS = "pool move"
MOVE_NONE = "none"
NODE_LIST_CLAUSE = " (validNodeVersions lists it, which serves a pool move alone)"
# The action names an active version to move to: the newest patch of the version's own minor at
# or above it (a channel can still list an older patch of the minor it has moved past, which is
# no target), else the lowest active version above it, else nothing the channel lists.
ACTION_NEWEST_FORMAT = "target the newest active patch of the minor on {where}: {newest}"
ACTION_NEXT_FORMAT = "no active patch of {minor} at or above it on {where}; the next active version is {next}"
ACTION_NONE_FORMAT = "no active version at or above it on {where}"
# The list a finding was graded against, and where its action looks: the channel's validVersions
# on a channel, validMasterVersions on none (a static cluster takes versions from that list).
ROSTER_CHANNEL_FORMAT = "the {channel} channel's {field}"
ROSTER_NO_CHANNEL_FORMAT = "{field} (no channel)"
WHERE_CHANNEL_FORMAT = "the {channel} channel"
PAST_CHANNEL = "the channel has moved past it, whatever another channel still lists"
PAST_NO_CHANNEL = "the list no longer serves it"
MINOR_FORMAT = "{major}.{minor}"
TARGET_FINDING_FORMAT = (
    "target {version} is in neither {roster} nor validNodeVersions at {location}: "
    "a withdrawn patch stays selectable and a node pool upgrade to it fails on an image the provider no longer serves; {action}"
)
TARGET_CONTROL_PLANE_FINDING_FORMAT = (
    "target {version} is not in {roster} at {location}, which the control plane's move to it needs{node_clause}: "
    "the channel refuses the move, and a withdrawn patch stays selectable; {action}"
)
CONTROL_PLANE_FINDING_FORMAT = (
    "control plane {version} is not among {roster} at {location} "
    "({past}); {action}"
)
NOTE_FORMAT = "channel {channel}; target {target} {target_verdict}; control plane {master} {master_verdict} in {field} at {location}; the target's move: {move}"
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


def listed_builds(text, roster) -> list[str]:
    """The versions in `roster` that `text` names, in the list's order (newest first as the API
    serves it): itself when the roster carries it; for a version with no `-gke.` build
    (`MAJOR.MINOR.PATCH`, which the API reads as that patch's newest listed build), every listed
    build of the patch; [] when the roster carries none."""
    if text in roster:
        return [text]
    if not isinstance(text, str) or BUILD_SEPARATOR in text:
        return []
    prefix = text + BUILD_SEPARATOR
    return [candidate for candidate in roster if isinstance(candidate, str) and candidate.startswith(prefix)]


def version_listed(text, roster) -> bool:
    """Whether `text` is in `roster`: a full version by exact match; a version with no `-gke.`
    build (`MAJOR.MINOR.PATCH`, which the API reads as that patch's newest listed build) when a
    listed version carries it as the prefix before `-gke.`."""
    return bool(listed_builds(text, roster))


def control_plane_moves(target_text, master_text, roster) -> bool:
    """Whether the control plane has to move to the target: the target is above the running
    control-plane version (a build-less target counts as the newest build of its patch the channel
    lists, else build 0); a target or control plane that does not parse counts as a move."""
    target = parse_version(target_text)
    master = parse_version(master_text)
    if target is None or master is None:
        return True
    if isinstance(target_text, str) and BUILD_SEPARATOR not in target_text:
        builds = [parse_version(version) for version in roster if isinstance(version, str) and version.startswith(target_text + BUILD_SEPARATOR)]
        builds = [build for build in builds if build is not None]
        target = max(builds) if builds else target
    return target > master


def target_builds(target_text, roster, node_versions, moves_control_plane: bool) -> list[str]:
    """The listed versions the member's upgrade to `target_text` lands on, the one reading of a
    target this rule and `known-issues-matrix` share: the versions in the channel's `roster` the
    target names (`listed_builds`); else, when the pools alone move, the ones in `node_versions`,
    the list a pool move alone takes a version from (a control-plane move takes one from the
    channel's list only); [] when the lists its move reads carry none."""
    builds = listed_builds(target_text, roster)
    if builds or moves_control_plane:
        return builds
    return listed_builds(target_text, node_versions)


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


def _on_channel(finding: dict) -> bool:
    return finding.get("channel") != NO_CHANNEL_LABEL


def _roster_text(finding: dict) -> str:
    """The list the finding was graded against, as the text names it."""
    field = finding.get("field") or (VALID_VERSIONS_KEY if _on_channel(finding) else VALID_MASTER_VERSIONS_KEY)
    return ROSTER_CHANNEL_FORMAT.format(channel=finding["channel"], field=field) if _on_channel(finding) else ROSTER_NO_CHANNEL_FORMAT.format(field=field)


def _where_text(finding: dict) -> str:
    """Where the action looks for an active version."""
    return WHERE_CHANNEL_FORMAT.format(channel=finding["channel"]) if _on_channel(finding) else _roster_text(finding)


def action_text(finding: dict) -> str:
    """The move the finding recommends, from its `newest` and `next_version`."""
    where = _where_text(finding)
    if finding.get("newest"):
        return ACTION_NEWEST_FORMAT.format(where=where, newest=finding["newest"])
    version = parse_version(finding.get("version")) or (0, 0)
    minor = MINOR_FORMAT.format(major=version[0], minor=version[1])
    if finding.get("next_version"):
        return ACTION_NEXT_FORMAT.format(minor=minor, where=where, next=finding["next_version"])
    return ACTION_NONE_FORMAT.format(where=where)


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
    move = MOVE_NONE
    if target_text:
        moves_control_plane = control_plane_moves(target_text, master_text, roster)
        move = MOVE_CONTROL_PLANE if moves_control_plane else MOVE_POOLS
        in_node_list = version_listed(target_text, node_versions)
        if target_builds(target_text, roster, node_versions, moves_control_plane):  # the lists its move reads, as known-issues-matrix reads them
            target_verdict = TARGET_VALID
        else:
            target_verdict = TARGET_WITHDRAWN
            newest, next_version = active_versions_above(target_text, roster)
            out["blocking"].append({"kind": KIND_TARGET, "version": target_text, "channel": channel_label, "field": field, "location": location, "newest": newest, "next_version": next_version, "move": move, "in_node_list": in_node_list})
    master_verdict = MASTER_NONE
    if master_text:
        if master_text in roster:
            master_verdict = MASTER_LISTED
        else:
            master_verdict = MASTER_NOT_LISTED
            newest, next_version = active_versions_above(master_text, roster)
            out["risks"].append({"kind": KIND_CONTROL_PLANE, "version": master_text, "channel": channel_label, "field": field, "location": location, "newest": newest, "next_version": next_version})
    out["notes"].append(NOTE_FORMAT.format(channel=channel_label, target=target_text or TARGET_NONE, target_verdict=target_verdict, master=master_text or MASTER_NONE, master_verdict=master_verdict, field=field, location=location, move=move))
    return out


def describe(finding: dict) -> str:
    action = action_text(finding)
    roster = _roster_text(finding)
    if finding.get("kind") == KIND_TARGET and finding.get("move") == MOVE_CONTROL_PLANE:
        return TARGET_CONTROL_PLANE_FINDING_FORMAT.format(version=finding["version"], roster=roster, location=finding["location"], node_clause=NODE_LIST_CLAUSE if finding.get("in_node_list") else "", action=action)
    if finding.get("kind") == KIND_TARGET:
        return TARGET_FINDING_FORMAT.format(version=finding["version"], roster=roster, location=finding["location"], action=action)
    return CONTROL_PLANE_FINDING_FORMAT.format(version=finding["version"], roster=roster, location=finding["location"], past=PAST_CHANNEL if _on_channel(finding) else PAST_NO_CHANNEL, action=action)
