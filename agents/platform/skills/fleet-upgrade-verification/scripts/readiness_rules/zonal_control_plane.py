"""
Entry 11 of docs/designs/upgrade-failure-catalogue.md: the control plane is unreachable
for minutes on a zonal cluster.

A zonal cluster has one control-plane replica, and GKE replaces it during the upgrade,
so the Kubernetes API is unavailable for minutes; the nodes and their pods keep running.
The rule reads the cluster's `location`: a zone (`us-central1-a`) is a zonal control
plane, a region (`us-central1`) a regional one. A multi-zonal cluster, nodes in several
zones under one zonal control plane, reads as zonal, which it is. Whether the cluster's
API clients retry is not readable from the cluster, so the finding is a risk on every
report and never a block: the upgrade completes; what fails is a client that does not
retry. `CAN_BLOCK` is False for the same reason, so a location the rule cannot place is
reported in the note and leaves the member's verdict to the other rules. The rule reads
the cluster record alone and needs none of the kubectl reads.

Source: https://docs.cloud.google.com/kubernetes-engine/docs/concepts/types-of-clusters
"""

import re

from readiness_rules import new_result

RULE_ID = "zonal-control-plane"
ENTRY = 11
CAN_BLOCK = False

# GCP location names: a region is `<continent>-<area><number>`, a zone adds `-<letter>`.
ZONE_RE = re.compile(r"^[a-z]+-[a-z]+\d+-[a-z]$")
REGION_RE = re.compile(r"^[a-z]+-[a-z]+\d+$")
LOCATION_KEY = "location"
# The cluster record's older spelling of the same field.
ZONE_KEY = "zone"
NODE_LOCATIONS_KEY = "locations"

FINDING_TEXT = (
    "control plane is zonal ({location}): the API is unavailable for minutes while GKE replaces its one replica, "
    "so schedule the control-plane upgrade in a maintenance window and have API clients retry with backoff; "
    "running pods are unaffected, and only a regional cluster, a recreate, keeps the API available"
)
UNKNOWN_LOCATION = "location {location!r} is neither a zone nor a region, so the control plane's type is unknown"


def evaluate(cluster: dict, member: dict, read, target, context) -> dict:
    out = new_result()
    location = str(cluster.get(LOCATION_KEY) or cluster.get(ZONE_KEY) or "")
    if ZONE_RE.match(location):
        out["risks"].append({"location": location, "node_locations": list(cluster.get(NODE_LOCATIONS_KEY) or [])})
    elif not REGION_RE.match(location):
        out["unknown"].append(UNKNOWN_LOCATION.format(location=location))
    return out


def describe(finding: dict) -> str:
    return FINDING_TEXT.format(location=finding["location"])
