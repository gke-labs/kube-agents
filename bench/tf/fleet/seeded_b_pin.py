#!/usr/bin/env python3
"""seeded-b's pin for bench/tf/fleet/main.tf, run by its `external` data source.

Reads REGULAR's version list at the zone (`gcloud container get-server-config`)
and seeded-b's running master version (`gcloud container clusters describe`; a
cluster that does not exist is a create) and prints the flat string map the
data source takes:

  regular_default         REGULAR's default version at the zone
  lagging_version         the newest patch REGULAR offers one minor behind it,
                          "" when it offers none
  running_master_version  seeded-b's master, "" before the cluster exists
  held                    "true" when the running patch is one REGULAR does
                          not carry
  pin                     what min_master_version is set to this cycle

The pin is lagging_version, except for a held cluster. GKE refuses a minor
upgrade from a patch the enrolled channel does not carry ("control plane patch
version and the desired control plane patch version have matching minor
versions", release-channels#newer-patch-versions) and allows a same-minor one,
so a held cluster is pinned to the newest REGULAR patch of its own minor above
the running version when there is one, else to the running version, and the
apply after that step does the minor bump from an on-channel patch. The
provider only ever raises a master, so neither choice asks for a downgrade. A
read that fails exits non-zero with the reason: it is the data source failing,
never a create.
"""

import json
import re
import subprocess
import sys

CHANNEL = "REGULAR"
GCLOUD = "gcloud"
SERVER_CONFIG_ARGS = ("container", "get-server-config")
DESCRIBE_ARGS = ("container", "clusters", "describe")
PROJECT_FLAG = "--project=%s"
ZONE_FLAG = "--zone=%s"
JSON_FORMAT_FLAG = "--format=json"
# Two reads of one zone, once per plan.
GCLOUD_TIMEOUT_SECONDS = 120
# The query main.tf sends; every value a string, as the data source requires.
QUERY_PROJECT = "project"
QUERY_ZONE = "zone"
QUERY_CLUSTER = "cluster"
QUERY_KEYS = (QUERY_PROJECT, QUERY_ZONE, QUERY_CLUSTER)
# Fields of the two gcloud documents.
SERVER_CONFIG_CHANNELS = "channels"
CHANNEL_NAME = "channel"
CHANNEL_DEFAULT = "defaultVersion"
CHANNEL_VALID = "validVersions"
CLUSTER_MASTER_VERSION = "currentMasterVersion"
# What `clusters describe` puts on stderr for a cluster that does not exist;
# any other failure is a failed read.
NOT_FOUND_MARKER = "code=404"
# The result keys, as main.tf reads them.
OUT_REGULAR_DEFAULT = "regular_default"
OUT_LAGGING_VERSION = "lagging_version"
OUT_RUNNING = "running_master_version"
OUT_HELD = "held"
OUT_PIN = "pin"
# GKE version strings, `1.34.11-gke.1209000`; anything else is refused rather
# than misordered. Minor is the second field.
VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)-gke\.(\d+)$")
MINOR_INDEX = 1
EXIT_FAILED = 1
LOG_PREFIX = "seeded_b_pin"

# The subprocess entry point, a module attribute so a test can stand in for it.
gcloud_runner = subprocess.run


class PinError(Exception):
    """A read or a version this script will not decide on."""


def parse_version(version):
    """`1.34.11-gke.1209000` -> (1, 34, 11, 1209000); orders patches correctly
    where a string sort would not."""
    match = VERSION_RE.match(str(version))
    if not match:
        raise PinError("not a GKE version: %r" % (version,))
    return tuple(int(part) for part in match.groups())


def _newest(versions):
    return max(versions, key=parse_version) if versions else ""


def choose(regular_default, regular_valid, running):
    """The pin, from REGULAR's default and valid list and seeded-b's running
    master version (None before the cluster exists)."""
    valid = [str(version) for version in regular_valid]
    for version in valid:
        parse_version(version)
    lag_minor = parse_version(regular_default)[MINOR_INDEX] - 1
    lagging = _newest([version for version in valid if parse_version(version)[MINOR_INDEX] == lag_minor])
    held = running is not None and running not in valid
    if held:
        running_key = parse_version(running)
        step = _newest([version for version in valid if parse_version(version)[MINOR_INDEX] == running_key[MINOR_INDEX] and parse_version(version) > running_key])
        chosen = step or running
    else:
        chosen = lagging
    return {OUT_REGULAR_DEFAULT: regular_default, OUT_LAGGING_VERSION: lagging, OUT_RUNNING: running or "", OUT_HELD: held, OUT_PIN: chosen}


def _gcloud(args):
    argv = [GCLOUD] + list(args)
    try:
        result = gcloud_runner(argv, capture_output=True, text=True, timeout=GCLOUD_TIMEOUT_SECONDS)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PinError("could not run %s: %s" % (" ".join(argv), exc))
    return " ".join(argv), result


def _document(command, stdout):
    try:
        doc = json.loads(stdout)
    except ValueError as exc:
        raise PinError("%s printed no JSON object: %s" % (command, exc))
    if not isinstance(doc, dict):
        raise PinError("%s printed no JSON object" % command)
    return doc


def read_server_config(project, zone):
    """REGULAR's (default version, valid versions) at the zone."""
    command, result = _gcloud([*SERVER_CONFIG_ARGS, PROJECT_FLAG % project, ZONE_FLAG % zone, JSON_FORMAT_FLAG])
    if result.returncode != 0:
        raise PinError("%s exited %d: %s" % (command, result.returncode, result.stderr.strip()))
    doc = _document(command, result.stdout)
    for entry in doc.get(SERVER_CONFIG_CHANNELS) or []:
        if isinstance(entry, dict) and entry.get(CHANNEL_NAME) == CHANNEL:
            return str(entry.get(CHANNEL_DEFAULT) or ""), [str(version) for version in entry.get(CHANNEL_VALID) or []]
    raise PinError("%s lists no %s channel" % (command, CHANNEL))


def read_running_version(project, zone, cluster):
    """seeded-b's current master version, or None when the cluster does not exist."""
    command, result = _gcloud([*DESCRIBE_ARGS, cluster, PROJECT_FLAG % project, ZONE_FLAG % zone, JSON_FORMAT_FLAG])
    if result.returncode != 0:
        if NOT_FOUND_MARKER in result.stderr:
            return None
        raise PinError("%s exited %d: %s" % (command, result.returncode, result.stderr.strip()))
    version = _document(command, result.stdout).get(CLUSTER_MASTER_VERSION)
    if not version:
        raise PinError("%s: no %s in the document" % (command, CLUSTER_MASTER_VERSION))
    return str(version)


def main(stdin=sys.stdin, stdout=sys.stdout, stderr=sys.stderr):
    try:
        query = json.load(stdin)
        if not isinstance(query, dict):
            raise PinError("the query is not a JSON object")
        missing = [key for key in QUERY_KEYS if not isinstance(query.get(key), str) or not query[key]]
        if missing:
            raise PinError("the query lacks %s" % ", ".join(missing))
        regular_default, regular_valid = read_server_config(query[QUERY_PROJECT], query[QUERY_ZONE])
        running = read_running_version(query[QUERY_PROJECT], query[QUERY_ZONE], query[QUERY_CLUSTER])
        chosen = choose(regular_default, regular_valid, running)
    except (PinError, ValueError) as exc:
        print("%s: %s" % (LOG_PREFIX, exc), file=stderr)
        return EXIT_FAILED
    chosen[OUT_HELD] = str(chosen[OUT_HELD]).lower()
    json.dump(chosen, stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
