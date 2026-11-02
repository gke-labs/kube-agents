"""
A plan-level rule, with no entry in docs/designs/upgrade-failure-catalogue.md: the fleet
host project already holds a RolloutSequence, so a plan that creates a second is refused.

GKE fleet rollout sequencing carries one RolloutSequence per fleet host project. A sequence
is global, and lists its stages: each names fleet projects, a cluster selector (a CEL
expression over the cluster's labels; none selects every member) and a soak duration. The
platform refuses a second sequence in the host project with FAILED_PRECONDITION, so an
upgrade plan that would create one for a train has to know whether the host holds one
already, and add the train as stages of it or plan the train in another host project.

The rule reads the member's `fleet` block from the cluster record (`fleet.project`, the
host project, an id or a number; absent for a cluster that is not a fleet member) and lists
the host project's sequences through the report's runner, once per host project per run:
the read is filed on the host's first member among the clusters the run grades (every
listed member on a full run; the named ones under `--cluster`), sorted by project, location
and name, and every other graded member of the host carries a note naming where. A member that is not a fleet member is a note: no sequence can carry
it, so an upgrade of it is the manual path. One existing sequence is one risk naming its
stages, each with its fleet projects, selector and soak, and the remedy is the clause after
the last `; `, which is the one action the weekly review takes from the text. An empty list
is a note. A refused, failed, cut or unparsable read is `unknown`; since the rule cannot
block, the unknown goes in the note and never moves the verdict. `CAN_BLOCK` is False
because a fleet that uses sequencing holds exactly one sequence in steady state, so a
blocker would stand on every report; the plan-level block is the upgrade SOP's precheck
step, which reads this rule's output.

The read is `gcloud container fleet rolloutsequences list --project <host> --format=json`
(one word, `rolloutsequences`), which needs `gkehub.googleapis.com` enabled on the host
project and `roles/gkehub.viewer` there (`gkehub.rolloutSequences.list`); the fleet block
comes with the cluster listing and costs no read of its own.

Sources: https://docs.cloud.google.com/kubernetes-engine/docs/concepts/rollout-sequencing-custom-stages/about-rollout-sequencing and
https://docs.cloud.google.com/kubernetes-engine/docs/how-to/rollout-sequencing-custom-stages/manage-upgrades-with-rollout-sequencing
"""

import json
import re

from readiness_rules import (
    CONTEXT_CACHE,
    CONTEXT_CLUSTERS,
    CONTEXT_GRADED,
    CONTEXT_RUN_CMD,
    CONTEXT_TIMEOUT_SECONDS,
    PROXY_TRUNCATION_MARKER,
    new_result,
    one_line,
)

RULE_ID = "rollout-sequence-precheck"
ENTRY = None  # a plan-level rule: it grades the upgrade plan, not a catalogue scenario
CAN_BLOCK = False

# The read, as the credential proxy's gcloud allowlist spells it.
GCLOUD = "gcloud"
LIST_ARGV = ("container", "fleet", "rolloutsequences", "list")
PROJECT_FLAG_FORMAT = "--project={project}"
FORMAT_JSON = "--format=json"
COMMAND_JOIN = " "
# The cluster record's fleet membership (`gcloud container clusters list`): absent for a
# cluster that is not a fleet member. `project` is the host project, an id or a number; the
# membership name carries the host's number when the record names no project.
FLEET_KEY = "fleet"
FLEET_PROJECT_KEY = "project"
MEMBERSHIP_KEY = "membership"
MEMBERSHIP_PROJECT_RE = re.compile(r"^projects/([^/]+)/")
# The record's own coordinates, for the listing order the read is filed in; the project is
# read from the record's `selfLink`, the one field of the listing that names it.
NAME_KEY = "name"
LOCATION_KEY = "location"
ZONE_KEY = "zone"  # the record's older spelling of the location
SELF_LINK_KEY = "selfLink"
SELF_LINK_PROJECT_RE = re.compile(r"/projects/([^/]+)/")
MEMBER_LABEL_SEPARATOR = "/"
# Where the host projects' lists are kept in the context's cache: {host: (sequences, error)}.
CACHE_KEY = "rollout_sequences"
# The RolloutSequence resource's fields (gkehub.googleapis.com/v1,
# projects/*/locations/global/rolloutSequences/*), as the list prints them.
SEQUENCE_NAME_KEY = "name"
STAGES_KEY = "stages"
FLEET_PROJECTS_KEY = "fleetProjects"
SOAK_KEY = "soakDuration"
CLUSTER_SELECTOR_KEY = "clusterSelector"
# The API returns the stage's selector as `clusterSelector.labelSelector` (a live listing of
# 2026-10-23); `expression` is the field the discovery document names, kept as the fallback.
SELECTOR_EXPRESSION_KEY = "labelSelector"
SELECTOR_EXPRESSION_FALLBACK_KEY = "expression"
RESOURCE_NAME_SEPARATOR = "/"
FLEET_PROJECT_PREFIX = "projects/"
KIND_ROLLOUT_SEQUENCE = "RolloutSequence"
OBJECT_FORMAT = "{host}/{name}"
# How a stage reads in the cell: its fleet projects, selector and soak; the stages are
# listed inside the parentheses, so the `; ` between them never reads as the remedy cut.
STAGE_FORMAT = "stage {number}: projects {projects}, selector {selector}, soak {soak}"
STAGE_SEPARATOR = "; "
LIST_JOIN = ", "
NO_SELECTOR = "none (every member)"
NO_SOAK = "none"
NO_PROJECTS = "none named"
NO_STAGES = "none listed"
SOAK_SECONDS_RE = re.compile(r"^(\d+)(?:\.\d+)?s$")
SECONDS_PER_DAY = 86400
SOAK_DAYS_FORMAT = "{soak} ({days}d)"
STDERR_EXCERPT_CHARS = 200
NO_STDERR = "no stderr"

FINDING_TEXT = (
    "host project {host} already holds RolloutSequence {name} ({count} stage(s): {stages}); "
    "the platform allows one per host project, so a plan that creates a second is refused (FAILED_PRECONDITION); "
    "add the train as stages of {name}, or plan it in a separate host project"
)
NOTE_NOT_A_MEMBER = "not a fleet member: no RolloutSequence can carry it; an upgrade of it is the manual path"
NOTE_FILED_ON = "precheck for host project {host} filed on {cluster}"
NOTE_NONE = "host project {host}: no RolloutSequence"
UNKNOWN_READ = "RolloutSequences of host project {host} not read ({error})"
NO_RUNNER_ERROR = "no command runner in the readiness context"
READ_FAILED_ERROR = "`{command}` failed (rc={rc}): {stderr}"
OUTPUT_CUT_ERROR = "`{command}` output was cut at the credential proxy's output cap"
UNPARSABLE_ERROR = "`{command}` returned output that is not a JSON list (rc=0)"


def host_project(cluster) -> str | None:
    """The member's fleet host project (`fleet.project`, an id or a number; else the project
    segment of `fleet.membership`), or None for a cluster that is not a fleet member."""
    fleet = cluster.get(FLEET_KEY) if isinstance(cluster, dict) else None
    if not isinstance(fleet, dict):
        return None
    project = fleet.get(FLEET_PROJECT_KEY)
    if isinstance(project, (str, int)) and not isinstance(project, bool) and str(project).strip():
        return str(project).strip()
    match = MEMBERSHIP_PROJECT_RE.match(str(fleet.get(MEMBERSHIP_KEY) or ""))
    return match.group(1) if match else None


def record_project(cluster: dict) -> str:
    """The project a listed record belongs to, from its `selfLink`; empty when it has none."""
    match = SELF_LINK_PROJECT_RE.search(str(cluster.get(SELF_LINK_KEY) or ""))
    return match.group(1) if match else ""


def cluster_key(cluster: dict) -> tuple[str, str, str]:
    """`(project, location, name)`: the listing order, and what tells two records apart."""
    return record_project(cluster), str(cluster.get(LOCATION_KEY) or cluster.get(ZONE_KEY) or ""), str(cluster.get(NAME_KEY) or "")


def member_label(cluster: dict) -> str:
    """`project/location/name`, without the project when the record names none."""
    return MEMBER_LABEL_SEPARATOR.join(part for part in cluster_key(cluster) if part)


def host_members(host: str, clusters, cluster: dict) -> list[dict]:
    """The host's members among the listed records, in listing order, the graded cluster
    among them (a context built without it still grades the member)."""
    members = [c for c in clusters or [] if isinstance(c, dict) and host_project(c) == host]
    if cluster_key(cluster) not in {cluster_key(c) for c in members}:
        members.append(cluster)
    return sorted(members, key=cluster_key)


def list_sequences(run_cmd, host: str, timeout=None) -> tuple[list | None, str | None]:
    """The host project's RolloutSequences through the runner: `(sequences, None)`, or
    `(None, error)` when there is no runner, the command failed, the credential proxy cut
    its output at the cap, or the output is not a JSON list."""
    cmd = [GCLOUD, *LIST_ARGV, PROJECT_FLAG_FORMAT.format(project=host), FORMAT_JSON]
    command = COMMAND_JOIN.join(cmd)
    if run_cmd is None:
        return None, NO_RUNNER_ERROR
    rc, stdout, stderr = run_cmd(cmd, timeout) if timeout else run_cmd(cmd)
    if rc != 0:
        return None, READ_FAILED_ERROR.format(command=command, rc=rc, stderr=one_line(stderr, STDERR_EXCERPT_CHARS) or NO_STDERR)
    if PROXY_TRUNCATION_MARKER in str(stderr or ""):
        return None, OUTPUT_CUT_ERROR.format(command=command)
    if not str(stdout or "").strip():
        return [], None
    try:
        listed = json.loads(stdout)
    except ValueError:
        listed = None
    if not isinstance(listed, list):
        return None, UNPARSABLE_ERROR.format(command=command)
    return [item for item in listed if isinstance(item, dict)], None


def cached_sequences(context: dict, host: str) -> tuple[list | None, str | None]:
    """`list_sequences` for the host, read once per context: the cache under CACHE_KEY holds
    the answer per host, so a context shared across members reads each host once."""
    cache = context.get(CONTEXT_CACHE)
    store = cache.get(CACHE_KEY) if isinstance(cache, dict) else None
    if isinstance(cache, dict) and not isinstance(store, dict):
        store = cache[CACHE_KEY] = {}
    if isinstance(store, dict) and host in store:
        sequences, error = store[host]
        return (list(sequences) if sequences is not None else None), error
    sequences, error = list_sequences(context.get(CONTEXT_RUN_CMD), host, context.get(CONTEXT_TIMEOUT_SECONDS))
    if isinstance(store, dict):
        store[host] = (list(sequences) if sequences is not None else None, error)
    return sequences, error


def short_name(resource_name) -> str:
    """`prod-train` from `projects/p/locations/global/rolloutSequences/prod-train`."""
    return str(resource_name or "").rsplit(RESOURCE_NAME_SEPARATOR, 1)[-1]


def stage_record(stage) -> dict:
    """One stage as the finding carries it: fleet projects (ids, without the `projects/`
    prefix), the selector expression or None, the soak or None."""
    stage = stage if isinstance(stage, dict) else {}
    projects = [str(p)[len(FLEET_PROJECT_PREFIX) :] if str(p).startswith(FLEET_PROJECT_PREFIX) else str(p) for p in (stage.get(FLEET_PROJECTS_KEY) or []) if p]
    selector = stage.get(CLUSTER_SELECTOR_KEY)
    expression = (selector.get(SELECTOR_EXPRESSION_KEY) or selector.get(SELECTOR_EXPRESSION_FALLBACK_KEY)) if isinstance(selector, dict) else None
    soak = stage.get(SOAK_KEY)
    return {"fleet_projects": projects, "selector": str(expression) if expression else None, "soak": str(soak) if soak else None}


def format_soak(soak) -> str:
    """`604800s (7d)`; the API's text as it is when it is not whole days."""
    if not soak:
        return NO_SOAK
    match = SOAK_SECONDS_RE.match(str(soak))
    if match and int(match.group(1)) % SECONDS_PER_DAY == 0 and int(match.group(1)) > 0:
        return SOAK_DAYS_FORMAT.format(soak=soak, days=int(match.group(1)) // SECONDS_PER_DAY)
    return str(soak)


def risk_for(host: str, sequence: dict, members: list[str]) -> dict:
    name = short_name(sequence.get(SEQUENCE_NAME_KEY))
    return {
        "kind": KIND_ROLLOUT_SEQUENCE,
        "namespace": "",
        "name": name,
        "object": OBJECT_FORMAT.format(host=host, name=name),
        "host_project": host,
        "resource": str(sequence.get(SEQUENCE_NAME_KEY) or ""),
        "stages": [stage_record(stage) for stage in (sequence.get(STAGES_KEY) or []) if isinstance(stage, dict)],
        "clusters": list(members),
    }


def evaluate(cluster: dict, member: dict, read, target, context) -> dict:
    out = new_result()
    host = host_project(cluster)
    if host is None:
        out["notes"].append(NOTE_NOT_A_MEMBER)
        return out
    members = host_members(host, context.get(CONTEXT_CLUSTERS), cluster)
    # The read is filed on the host's first member among the clusters the run
    # grades (`--cluster` narrows who is graded, not who the rule sees), so a
    # report of one member still carries the host's risk; on a full run that is
    # the host's first listed member.
    graded = context.get(CONTEXT_GRADED)
    filing = host_members(host, graded, cluster) if graded is not None else members
    first = filing[0]
    if cluster_key(first) != cluster_key(cluster):
        out["notes"].append(NOTE_FILED_ON.format(host=host, cluster=member_label(first)))
        return out
    sequences, error = cached_sequences(context, host)
    if error is not None:
        out["unknown"].append(UNKNOWN_READ.format(host=host, error=error))
        return out
    if not sequences:
        out["notes"].append(NOTE_NONE.format(host=host))
        return out
    labels = [member_label(c) for c in members]
    for sequence in sequences:
        out["risks"].append(risk_for(host, sequence, labels))
    return out


def describe(finding: dict) -> str:
    stages = finding.get("stages") or []
    rendered = STAGE_SEPARATOR.join(
        STAGE_FORMAT.format(
            number=number,
            projects=LIST_JOIN.join(stage.get("fleet_projects") or []) or NO_PROJECTS,
            selector=stage.get("selector") or NO_SELECTOR,
            soak=format_soak(stage.get("soak")),
        )
        for number, stage in enumerate(stages, 1)
    )
    return FINDING_TEXT.format(host=finding["host_project"], name=finding["name"], count=len(stages), stages=rendered or NO_STAGES)
