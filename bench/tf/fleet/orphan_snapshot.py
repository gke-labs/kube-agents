#!/usr/bin/env python3
# Copyright 2026 The Kubernetes Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Plant the orphaned-snapshot fixtures, run by terraform_data.orphan_snapshots in main.tf.

The GCE compute audit's orphaned-snapshots check (section 2.5 of its SOP)
flags a Persistent Disk snapshot whose `sourceDisk` names no live disk in the
project, that no snapshot schedule took, and that is more than 90 full days
old. No Terraform resource can hold that shape: a `google_compute_snapshot`
needs its source disk at create time, and a `google_compute_disk` the state
still holds is re-created by the next apply, which un-orphans the snapshot.
So this script does it as a sequence of gcloud calls: create a small
throwaway disk in the stack's zone unless it is there, take each snapshot the
stack names from it unless that snapshot exists, then delete every disk of
that name, in whatever zone. The snapshots are the fixture; the disk is
scaffolding, and the planted state is "snapshots present, no disk of the
source's name anywhere in the project", which is the question the compute
audit asks (it indexes live disks by bare name, project-wide) and the one
the fixture-state scan's `source_disk_gone` repeats.

Two entry points, both reading the same inputs:

* `orphan_snapshot.py` (no argument), the terraform_data's local-exec, plants:
  inputs from the environment (the ORPHAN_SNAPSHOT_* variables below).
* `orphan_snapshot.py probe`, a `data "external"` the stack reads at plan
  time: inputs as the data source's query on stdin, result on stdout as
  `{"missing": "<snapshot names not there, comma-joined>", "source_disk":
  "present" | "absent" (any zone), "read_at": "" | "<the time of this read>"}`. The
  result sits in the resource's triggers_replace, so a snapshot a sweep
  deleted, or a source disk someone re-created, plans a replace and the next
  reconcile re-runs the planter: the fleet's model is that the scheduled
  re-apply keeps the defects planted, and this is how these two keep to it
  with nothing of theirs in the state. `read_at` is empty when there is
  nothing to repair and the read's time otherwise: a terraform_data stores
  the trigger values of the plan that replaced it, which on a repairing run
  are the broken reading, so without the stamp the same breakage recurring
  before the next clean plan would read equal to the stored trigger and plan
  nothing. With it every plan that reads something to repair plans the
  replace, and a clean read settles at the same empty value each time. The
  run after a repair therefore plans one more replace, a no-op, when the
  probe reads the repaired state.

Every step is idempotent, because a replace re-runs the whole planter (a
failed run taints the resource too). An existing snapshot is never touched:
`creationTimestamp` is server-set and immutable, the check's 90-day clock
runs from it, and a re-run must not restart it. A re-run therefore only
repairs what is missing (a snapshot, with a fresh clock) or what should be
gone (a disk of the source's name, in any zone). Nothing deletes a snapshot: `tofu destroy` leaves
them, at a few MB of stored blocks each, and they are removed by hand.

Standard library only: the reconcile image has python3 and gcloud, and the
reconciler's roles/compute.storageAdmin covers every call here. Either entry
point exits non-zero, with gcloud's own words, on any failure, so the plan or
the apply fails rather than planting or reporting blind.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone

LOG_PREFIX = "orphan_snapshot"
# The one argument the probe entry point takes.
PROBE_ARG = "probe"
# The planter's inputs, from the environment (the local-exec) ...
PROJECT_ENV = "ORPHAN_SNAPSHOT_PROJECT"
ZONE_ENV = "ORPHAN_SNAPSHOT_ZONE"
# The throwaway source disk's name.
SOURCE_ENV = "ORPHAN_SNAPSHOT_SOURCE"
# The snapshot names, as a JSON list.
NAMES_ENV = "ORPHAN_SNAPSHOT_NAMES"
# The labels for the disk and the snapshots, as a JSON object.
LABELS_ENV = "ORPHAN_SNAPSHOT_LABELS"
# ... and the probe's, as the data source's query (every value a string;
# the names a JSON list inside one).
QUERY_PROJECT = "project"
QUERY_ZONE = "zone"
QUERY_SOURCE = "source"
QUERY_NAMES = "names"
# The probe's result keys and the source disk's two readings. `read_at` is
# the time of the read when the other two report something to repair, and
# empty otherwise (the docstring says why).
RESULT_MISSING = "missing"
RESULT_SOURCE_DISK = "source_disk"
RESULT_READ_AT = "read_at"
SOURCE_PRESENT = "present"
SOURCE_ABSENT = "absent"
MISSING_SEPARATOR = ","
# The smallest pd-standard disk: the snapshot stores used blocks only, and an
# empty disk has none worth paying for.
SOURCE_DISK_SIZE_GB = "10"
SOURCE_DISK_TYPE = "pd-standard"
# A disk create or a snapshot of an empty disk takes seconds; a delete waits
# on its zone operation. Anything near this is the API being away.
GCLOUD_TIMEOUT_SECONDS = 600
# gcloud's words for a resource that is not there, on a describe. Any other
# failure of a describe stops the run rather than planting blind.
NOT_FOUND_RE = re.compile(r"was not found|not found", re.IGNORECASE)
# A Compute Engine resource name, as GCE defines it: a letter first, hyphens
# inside. Nothing else from the inputs goes on a command line.
NAME_RE = re.compile(r"[a-z]([-a-z0-9]*[a-z0-9])?")
# A Compute Engine zone, as the stack passes it.
ZONE_RE = re.compile(r"[a-z]+-[a-z]+[0-9]+-[a-z]")
# A label key or value, as the API accepts them.
LABEL_RE = re.compile(r"[a-z0-9_-]{1,63}")
SNAPSHOTS = "snapshots"
DISKS = "disks"
# A `disks list` item carries `zone` (a zonal disk) or `region` (a regional
# one), each a URL ending in the location's name, and the delete takes the
# matching flag.
ZONE_FIELD = "zone"
REGION_FIELD = "region"
ZONE_FLAG = "--zone"
REGION_FLAG = "--region"
# A Compute Engine region, as the URL ends in it.
REGION_RE = re.compile(r"[a-z]+-[a-z]+[0-9]+")
FORMAT_JSON = "json"


def log(message: str) -> None:
    print(f"{LOG_PREFIX}: {message}", file=sys.stderr)


def gcloud(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["gcloud", *args], capture_output=True, text=True, timeout=GCLOUD_TIMEOUT_SECONDS, check=False)


def fail(what: str, done: subprocess.CompletedProcess) -> None:
    sys.exit(f"{LOG_PREFIX}: {what} failed ({done.returncode}): {done.stderr.strip() or done.stdout.strip() or 'no output'}")


def exists(collection: str, name: str, project: str, *scope: str) -> bool:
    """Is the object there? A describe that fails for any reason other than
    "not found" stops the run: planting against an unknown state is how a
    second disk or a second clock gets started."""
    done = gcloud("compute", collection, "describe", name, "--project", project, *scope, "--format", "value(name)")
    if done.returncode == 0:
        return True
    if NOT_FOUND_RE.search(done.stderr):
        return False
    fail(f"compute {collection} describe {name}", done)
    return False  # pragma: no cover - fail() exits


def disks_named(project: str, name: str) -> list[tuple[str, str]]:
    """The (scope flag, location) of every disk of this name in the project,
    zonal or regional: the compute audit indexes live disks by bare name
    across locations, so a disk of the source's name anywhere un-orphans the
    snapshots. A failed list, or a location outside the shapes a command line
    may carry, stops the run."""
    done = gcloud("compute", DISKS, "list", "--project", project, f"--filter=name ~ ^{name}$", "--format", FORMAT_JSON)
    if done.returncode != 0:
        fail(f"compute {DISKS} list {name}", done)
    try:
        items = json.loads(done.stdout or "[]")
    except ValueError as exc:
        sys.exit(f"{LOG_PREFIX}: compute {DISKS} list {name} printed something other than JSON: {exc}")
    found = []
    for item in items if isinstance(items, list) else []:
        item = item if isinstance(item, dict) else {}
        zone = str(item.get(ZONE_FIELD) or "").rstrip("/").rsplit("/", 1)[-1]
        region = str(item.get(REGION_FIELD) or "").rstrip("/").rsplit("/", 1)[-1]
        if zone and ZONE_RE.fullmatch(zone):
            found.append((ZONE_FLAG, zone))
        elif region and REGION_RE.fullmatch(region):
            found.append((REGION_FLAG, region))
        else:
            sys.exit(f"{LOG_PREFIX}: compute {DISKS} list {name} named a location this script does not put on a command line: zone {zone!r}, region {region!r}")
    return found


def read_inputs(project: str, zone: str, source: str, names_json: str, where: str) -> tuple[str, str, str, list[str]]:
    """The validated (project, zone, source disk, snapshot names); exits on
    anything missing or outside the shapes a command line may carry."""
    try:
        names = json.loads(names_json or "[]")
    except ValueError as exc:
        sys.exit(f"{LOG_PREFIX}: {where}: the snapshot names must be a JSON list: {exc}")
    if not project or not ZONE_RE.fullmatch(zone) or not NAME_RE.fullmatch(source) or not names:
        sys.exit(f"{LOG_PREFIX}: {where}: a project, a zone, a source disk name and a non-empty list of snapshot names are required")
    if not isinstance(names, list) or not all(isinstance(n, str) and NAME_RE.fullmatch(n) for n in names):
        sys.exit(f"{LOG_PREFIX}: {where}: the snapshot names must be a JSON list of Compute Engine names")
    return project, zone, source, names


def labels_flag(labels_json: str) -> str:
    try:
        labels = json.loads(labels_json or "{}")
    except ValueError as exc:
        sys.exit(f"{LOG_PREFIX}: {LABELS_ENV} must be JSON: {exc}")
    if not isinstance(labels, dict):
        sys.exit(f"{LOG_PREFIX}: {LABELS_ENV} must be a JSON object")
    pairs = []
    for key, value in sorted(labels.items()):
        if not (LABEL_RE.fullmatch(str(key)) and LABEL_RE.fullmatch(str(value))):
            sys.exit(f"{LOG_PREFIX}: label {key!r}={value!r} is not a label this script puts on a command line")
        pairs.append(f"{key}={value}")
    return ",".join(pairs)


def plant() -> int:
    project, zone, source, names = read_inputs(
        os.environ.get(PROJECT_ENV, ""), os.environ.get(ZONE_ENV, ""), os.environ.get(SOURCE_ENV, ""),
        os.environ.get(NAMES_ENV, ""), f"{PROJECT_ENV}, {ZONE_ENV}, {SOURCE_ENV}, {NAMES_ENV}",
    )
    flag = labels_flag(os.environ.get(LABELS_ENV, ""))
    zone_scope = ("--zone", zone)

    missing = [name for name in names if not exists(SNAPSHOTS, name, project)]
    log(f"{len(names) - len(missing)} of {len(names)} snapshot(s) already there in {project}; an existing snapshot is never touched")
    if missing:
        if (ZONE_FLAG, zone) in disks_named(project, source):
            log(f"source disk {source} is there in {zone} from an earlier run")
        else:
            done = gcloud("compute", DISKS, "create", source, "--project", project, *zone_scope,
                          f"--size={SOURCE_DISK_SIZE_GB}GB", f"--type={SOURCE_DISK_TYPE}", f"--labels={flag}")
            if done.returncode != 0:
                fail(f"compute disks create {source}", done)
            log(f"created source disk {source} in {zone}")
        for name in missing:
            done = gcloud("compute", DISKS, "snapshot", source, "--project", project, *zone_scope,
                          f"--snapshot-names={name}", f"--labels={flag}")
            if done.returncode != 0:
                fail(f"compute disks snapshot {source} as {name}", done)
            log(f"took snapshot {name}; its 91-day clock starts now")
    # Every disk of the source's name goes, in whatever zone, whether this run
    # or an earlier one created it: the designed state is snapshots with no
    # live disk of that name anywhere, and one someone re-created, in this
    # zone or another, un-orphans every snapshot taken from the source.
    for scope_flag, location in disks_named(project, source):
        done = gcloud("compute", DISKS, "delete", source, "--project", project, scope_flag, location, "--quiet")
        if done.returncode != 0:
            fail(f"compute disks delete {source} in {location}", done)
        log(f"deleted source disk {source} in {location}; the snapshots are orphaned")
    return 0


def probe() -> int:
    """What the planter would repair, as the data source's result."""
    try:
        query = json.load(sys.stdin)
    except ValueError as exc:
        sys.exit(f"{LOG_PREFIX}: {PROBE_ARG}: the query on stdin must be JSON: {exc}")
    if not isinstance(query, dict) or not all(isinstance(v, str) for v in query.values()):
        sys.exit(f"{LOG_PREFIX}: {PROBE_ARG}: the query must be a JSON object of strings")
    project, zone, source, names = read_inputs(
        query.get(QUERY_PROJECT, ""), query.get(QUERY_ZONE, ""), query.get(QUERY_SOURCE, ""),
        query.get(QUERY_NAMES, ""), f"{PROBE_ARG} query {QUERY_PROJECT}, {QUERY_ZONE}, {QUERY_SOURCE}, {QUERY_NAMES}",
    )
    missing = [name for name in names if not exists(SNAPSHOTS, name, project)]
    source_disk = SOURCE_PRESENT if disks_named(project, source) else SOURCE_ABSENT
    read_at = datetime.now(timezone.utc).isoformat() if missing or source_disk == SOURCE_PRESENT else ""
    json.dump({RESULT_MISSING: MISSING_SEPARATOR.join(missing), RESULT_SOURCE_DISK: source_disk, RESULT_READ_AT: read_at}, sys.stdout)
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv == [PROBE_ARG]:
        return probe()
    if argv:
        sys.exit(f"{LOG_PREFIX}: usage: {sys.argv[0]} [{PROBE_ARG}]")
    return plant()


if __name__ == "__main__":
    sys.exit(main())
