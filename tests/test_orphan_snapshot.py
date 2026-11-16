"""The orphaned-snapshot fixtures are planted once, by gcloud, and never re-taken.

`bench/tf/fleet/orphan_snapshot.py` runs from a terraform_data local-exec
because no Terraform resource holds "a snapshot whose source disk is gone".
These tests pin the sequence (create the throwaway disk, snapshot it under
each name, delete the disk), that every step is idempotent so a replace of
the resource repairs what is missing without touching an existing snapshot
(whose creationTimestamp is the GCE compute audit's 91-day clock), that a
source disk someone re-created is deleted, and that a describe failing for
any reason other than "not found" stops the run before anything is created.
"""

import importlib.util
import io
import json
import os
import pathlib
import subprocess
import unittest
from unittest.mock import patch

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_MODULE_PATH = _REPO_ROOT / "bench" / "tf" / "fleet" / "orphan_snapshot.py"

_spec = importlib.util.spec_from_file_location("orphan_snapshot", _MODULE_PATH)
orphan_snapshot = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(orphan_snapshot)

NAMES = ["declared-orphan-snap-seeded", "orphan-snap-seeded"]
ENV = {
    "ORPHAN_SNAPSHOT_PROJECT": "kube-agents-evals-7",
    "ORPHAN_SNAPSHOT_ZONE": "us-central1-a",
    "ORPHAN_SNAPSHOT_SOURCE": "orphan-snap-source-seeded",
    "ORPHAN_SNAPSHOT_NAMES": json.dumps(NAMES),
    "ORPHAN_SNAPSHOT_LABELS": json.dumps({"managed-by": "kube-agents-seeded-fleet"}),
}
NOT_FOUND = "ERROR: (gcloud.compute.snapshots.describe) Could not fetch resource:\n - The resource 'projects/p/global/snapshots/x' was not found\n"
DENIED = "ERROR: (gcloud.compute.snapshots.describe) Could not fetch resource:\n - Required 'compute.snapshots.get' permission\n"


ZONE = ENV["ORPHAN_SNAPSHOT_ZONE"]
OTHER_ZONE = "us-east1-b"
# A regional disk's location, served under `region` rather than `zone`.
REGION = "region:us-central1"
DISKS_DENIED = "ERROR: (gcloud.compute.disks.list) Some requests did not succeed:\n - Required 'compute.disks.list' permission\n"


class FakeGcloud:
    """A project holding some of the objects; records every mutating call.
    `disk` is the zones holding a disk of the source's name (True: the
    stack's zone); the fake serves `disks list` by name across them, as
    gcloud does, and `disks delete` by the zone given."""

    def __init__(self, snapshots=(), disk=False, refuse=None):
        self.snapshots = set(snapshots)
        self.disk_zones = set(disk) if isinstance(disk, (tuple, list, set)) else ({ZONE} if disk else set())
        self.refuse = refuse
        self.calls = []

    @property
    def disk(self):
        return bool(self.disk_zones)

    def __call__(self, cmd, **kwargs):
        assert cmd[0] == "gcloud" and "--project" in cmd, cmd
        verb = cmd[2:4]
        if verb == ["disks", "list"]:
            if self.refuse == ("disks-list",):
                return subprocess.CompletedProcess(cmd, 1, "", DISKS_DENIED)
            pattern = next(a for a in cmd if a.startswith("--filter=name ~ ^"))[len("--filter=name ~ ^"):].removesuffix("$")
            items = [
                {"name": pattern, "region": f"https://www.googleapis.com/compute/v1/projects/p/regions/{z.split(':', 1)[1]}"}
                if z.startswith("region:")
                else {"name": pattern, "zone": f"https://www.googleapis.com/compute/v1/projects/p/zones/{z}"}
                for z in sorted(self.disk_zones)
                if pattern == ENV["ORPHAN_SNAPSHOT_SOURCE"]
            ]
            return subprocess.CompletedProcess(cmd, 0, json.dumps(items), "")
        name = cmd[4]
        if verb[1] == "describe":
            assert verb[0] == "snapshots", cmd
            there = name in self.snapshots
            if self.refuse == (verb[0], name):
                return subprocess.CompletedProcess(cmd, 1, "", DENIED)
            return subprocess.CompletedProcess(cmd, 0 if there else 1, name if there else "", "" if there else NOT_FOUND)
        self.calls.append(" ".join(cmd[1:]))
        zone = cmd[cmd.index("--zone") + 1] if "--zone" in cmd else (f"region:{cmd[cmd.index('--region') + 1]}" if "--region" in cmd else None)
        if verb == ["disks", "create"]:
            self.disk_zones.add(zone)
        elif verb == ["disks", "snapshot"]:
            assert zone in self.disk_zones, cmd
            self.snapshots.add(next(a for a in cmd if a.startswith("--snapshot-names=")).split("=", 1)[1])
        elif verb == ["disks", "delete"]:
            assert zone in self.disk_zones, cmd
            self.disk_zones.discard(zone)
        else:
            raise AssertionError(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")


def _run(fake, env=ENV):
    with patch.dict(os.environ, env, clear=True), patch.object(orphan_snapshot.subprocess, "run", fake), patch("sys.stderr", io.StringIO()):
        return orphan_snapshot.main([])


QUERY = {"project": ENV["ORPHAN_SNAPSHOT_PROJECT"], "zone": ENV["ORPHAN_SNAPSHOT_ZONE"], "source": ENV["ORPHAN_SNAPSHOT_SOURCE"], "names": json.dumps(NAMES)}


def _probe(fake, query=QUERY):
    out = io.StringIO()
    with patch.dict(os.environ, {}, clear=True), patch.object(orphan_snapshot.subprocess, "run", fake), patch("sys.stderr", io.StringIO()), patch("sys.stdin", io.StringIO(json.dumps(query))), patch("sys.stdout", out):
        rc = orphan_snapshot.main(["probe"])
    return rc, json.loads(out.getvalue())


class PlanterTest(unittest.TestCase):
    def test_a_fresh_project_gets_the_disk_both_snapshots_and_no_disk(self):
        fake = FakeGcloud()
        self.assertEqual(_run(fake), 0)
        self.assertEqual(
            fake.calls,
            [
                "compute disks create orphan-snap-source-seeded --project kube-agents-evals-7 --zone us-central1-a --size=10GB --type=pd-standard --labels=managed-by=kube-agents-seeded-fleet",
                "compute disks snapshot orphan-snap-source-seeded --project kube-agents-evals-7 --zone us-central1-a --snapshot-names=declared-orphan-snap-seeded --labels=managed-by=kube-agents-seeded-fleet",
                "compute disks snapshot orphan-snap-source-seeded --project kube-agents-evals-7 --zone us-central1-a --snapshot-names=orphan-snap-seeded --labels=managed-by=kube-agents-seeded-fleet",
                "compute disks delete orphan-snap-source-seeded --project kube-agents-evals-7 --zone us-central1-a --quiet",
            ],
        )
        self.assertEqual(fake.snapshots, set(NAMES))
        self.assertFalse(fake.disk)

    def test_a_planted_project_is_left_alone(self):
        # The replace re-runs the script on every project; an existing
        # snapshot's creationTimestamp is the audit's clock and must survive.
        fake = FakeGcloud(snapshots=NAMES)
        self.assertEqual(_run(fake), 0)
        self.assertEqual(fake.calls, [])

    def test_only_a_missing_snapshot_is_taken(self):
        fake = FakeGcloud(snapshots=[NAMES[0]])
        self.assertEqual(_run(fake), 0)
        self.assertEqual([c.split(" --project")[0] for c in fake.calls], [
            "compute disks create orphan-snap-source-seeded",
            "compute disks snapshot orphan-snap-source-seeded",
            "compute disks delete orphan-snap-source-seeded",
        ])
        self.assertIn("--snapshot-names=orphan-snap-seeded", fake.calls[1])
        self.assertNotIn("declared-orphan-snap-seeded", " ".join(fake.calls))

    def test_a_source_disk_left_behind_is_deleted_and_reused(self):
        # A run cut between the snapshot and the delete, or a disk someone
        # re-created under the source's name, which un-orphans every snapshot.
        fake = FakeGcloud(snapshots=NAMES, disk=True)
        self.assertEqual(_run(fake), 0)
        self.assertEqual(fake.calls, ["compute disks delete orphan-snap-source-seeded --project kube-agents-evals-7 --zone us-central1-a --quiet"])
        fake = FakeGcloud(snapshots=[NAMES[1]], disk=True)
        self.assertEqual(_run(fake), 0)
        self.assertEqual([c.split(" ")[2] for c in fake.calls], ["snapshot", "delete"])

    def test_a_describe_refused_for_any_other_reason_stops_before_planting(self):
        fake = FakeGcloud(refuse=("snapshots", NAMES[1]))
        with self.assertRaises(SystemExit) as raised:
            _run(fake)
        self.assertIn("compute.snapshots.get", str(raised.exception.code))
        self.assertEqual(fake.calls, [])
        fake = FakeGcloud(refuse=("disks-list",))
        with self.assertRaises(SystemExit) as raised:
            _run(fake)
        self.assertIn("compute.disks.list", str(raised.exception.code))
        self.assertEqual(fake.calls, [])

    def test_a_source_disk_in_another_zone_is_deleted_too(self):
        # The audit indexes live disks by bare name across zones, so a disk of
        # the source's name anywhere un-orphans the snapshots; the planter
        # creates in its own zone when that one is empty and deletes wherever
        # the name is found.
        fake = FakeGcloud(snapshots=NAMES, disk=(OTHER_ZONE,))
        self.assertEqual(_run(fake), 0)
        self.assertEqual(fake.calls, [f"compute disks delete orphan-snap-source-seeded --project kube-agents-evals-7 --zone {OTHER_ZONE} --quiet"])
        fake = FakeGcloud(snapshots=[NAMES[0]], disk=(OTHER_ZONE,))
        self.assertEqual(_run(fake), 0)
        self.assertEqual([c.split(" --project")[0] + (" " + c.split("--zone ")[1].split(" ")[0] if "--zone" in c else "") for c in fake.calls], [
            f"compute disks create orphan-snap-source-seeded {ZONE}",
            f"compute disks snapshot orphan-snap-source-seeded {ZONE}",
            f"compute disks delete orphan-snap-source-seeded {ZONE}",
            f"compute disks delete orphan-snap-source-seeded {OTHER_ZONE}",
        ])
        self.assertFalse(fake.disk)

    def test_a_regional_source_disk_is_deleted_with_its_region(self):
        # A regional PD of the source's name un-orphans the snapshots as a
        # zonal one does, and carries `region` where the zonal one carries
        # `zone`; the planter names and deletes it rather than failing the plan.
        fake = FakeGcloud(snapshots=NAMES, disk=(REGION,))
        self.assertEqual(_run(fake), 0)
        self.assertEqual(fake.calls, ["compute disks delete orphan-snap-source-seeded --project kube-agents-evals-7 --region us-central1 --quiet"])
        self.assertFalse(fake.disk)

    def test_a_failed_create_fails_the_apply_with_gclouds_words(self):
        fake = FakeGcloud()
        real = fake.__call__

        def failing(cmd, **kwargs):
            if cmd[2:4] == ["disks", "snapshot"]:
                return subprocess.CompletedProcess(cmd, 1, "", "ERROR: quota exceeded")
            return real(cmd, **kwargs)

        with self.assertRaises(SystemExit) as raised:
            _run(failing)
        self.assertIn("quota exceeded", str(raised.exception.code))

    def test_a_missing_or_malformed_variable_fails_before_any_call(self):
        for env in (
            {**ENV, "ORPHAN_SNAPSHOT_PROJECT": ""},
            {**ENV, "ORPHAN_SNAPSHOT_ZONE": "us-central1-a; rm -rf /"},
            {**ENV, "ORPHAN_SNAPSHOT_SOURCE": "--zone=x"},
            {**ENV, "ORPHAN_SNAPSHOT_NAMES": "[]"},
            {**ENV, "ORPHAN_SNAPSHOT_NAMES": json.dumps(["Bad Name"])},
            {**ENV, "ORPHAN_SNAPSHOT_NAMES": "not json"},
            {**ENV, "ORPHAN_SNAPSHOT_LABELS": json.dumps({"k": "v,x=y"})},
        ):
            with self.subTest(env=env):
                fake = FakeGcloud()
                with self.assertRaises(SystemExit):
                    _run(fake, env)
                self.assertEqual(fake.calls, [])


class ProbeTest(unittest.TestCase):
    """`orphan_snapshot.py probe`, the data source whose result is a trigger:
    it reads, mutates nothing, and reports what the planter would repair."""

    def test_a_fresh_project_reads_both_missing_and_no_disk(self):
        fake = FakeGcloud()
        rc, result = _probe(fake)
        self.assertEqual(rc, 0)
        self.assertEqual({k: v for k, v in result.items() if k != "read_at"}, {"missing": "declared-orphan-snap-seeded,orphan-snap-seeded", "source_disk": "absent"})
        self.assertEqual(fake.calls, [], "the probe plants nothing")

    def test_a_planted_project_reads_steady(self):
        # The same value on every clean read: the trigger settles.
        self.assertEqual(_probe(FakeGcloud(snapshots=NAMES)), (0, {"missing": "", "source_disk": "absent", "read_at": ""}))

    def test_a_swept_snapshot_and_a_recreated_disk_change_the_result(self):
        # Either change moves a trigger, so the next reconcile re-runs the planter.
        self.assertEqual(_probe(FakeGcloud(snapshots=[NAMES[0]]))[1]["missing"], "orphan-snap-seeded")
        self.assertEqual(_probe(FakeGcloud(snapshots=NAMES, disk=True))[1]["source_disk"], "present")

    def test_a_broken_reading_is_stamped_so_a_recurrence_still_plans_a_replace(self):
        # The stored trigger after a repairing run is the broken reading; the
        # same breakage read again must not equal it.
        first = _probe(FakeGcloud(snapshots=[NAMES[0]]))[1]
        second = _probe(FakeGcloud(snapshots=[NAMES[0]]))[1]
        self.assertTrue(first["read_at"] and second["read_at"])
        self.assertNotEqual(first["read_at"], second["read_at"])
        self.assertEqual(_probe(FakeGcloud(snapshots=NAMES, disk=True))[1]["read_at"] != "", True)
        self.assertEqual(_probe(FakeGcloud(snapshots=NAMES))[1]["read_at"], "")

    def test_a_refused_read_fails_the_plan(self):
        with self.assertRaises(SystemExit) as raised:
            _probe(FakeGcloud(refuse=("snapshots", NAMES[0])))
        self.assertIn("compute.snapshots.get", str(raised.exception.code))
        with self.assertRaises(SystemExit) as raised:
            _probe(FakeGcloud(snapshots=NAMES, refuse=("disks-list",)))
        self.assertIn("compute.disks.list", str(raised.exception.code))

    def test_a_source_disk_in_another_zone_or_a_region_reads_present(self):
        self.assertEqual(_probe(FakeGcloud(snapshots=NAMES, disk=(OTHER_ZONE,)))[1]["source_disk"], "present")
        self.assertEqual(_probe(FakeGcloud(snapshots=NAMES, disk=(REGION,)))[1]["source_disk"], "present")

    def test_a_malformed_query_fails_before_any_call(self):
        for query in ({**QUERY, "names": "[]"}, {**QUERY, "zone": "x; rm -rf /"}, {**QUERY, "project": ""}, {"names": 3}):
            with self.subTest(query=query):
                fake = FakeGcloud()
                with self.assertRaises(SystemExit):
                    _probe(fake, query)
                self.assertEqual(fake.calls, [])

    def test_any_other_argument_is_a_usage_error(self):
        with self.assertRaises(SystemExit):
            with patch("sys.stderr", io.StringIO()):
                orphan_snapshot.main(["plant", "now"])


class PlanterTriggerTest(unittest.TestCase):
    def test_the_stack_runs_the_script_with_its_hash_and_names_as_triggers(self):
        # A provisioner's command is not part of the resource's state: without
        # the script's hash in triggers_replace an edited orphan_snapshot.py
        # would plan nothing on a planted project, and without the names a
        # renamed snapshot would never be taken.
        stack = (_REPO_ROOT / "bench" / "tf" / "fleet" / "main.tf").read_text()
        block = stack[stack.index('resource "terraform_data" "orphan_snapshots"'):]
        block = block[: block.index("\n}\n")]
        # The triggers are checked inside the triggers_replace list, not the
        # whole block: the local-exec environment carries the same names
        # expression, so a names trigger dropped from the list would otherwise
        # still be found.
        triggers = block[block.index("triggers_replace"):block.index("\n  ]")]
        self.assertIn('filesha256("${path.module}/orphan_snapshot.py")', triggers)
        self.assertIn("local.orphan_snapshot_source", triggers)
        self.assertIn("jsonencode(local.orphan_snapshot_names)", triggers)
        self.assertIn('command     = "${path.module}/orphan_snapshot.py"', block)
        for env_name in ENV:
            self.assertIn(env_name, block)
        # The probe's readings are triggers too: that is what lets the daily
        # re-apply repair a swept snapshot or a re-created source disk with
        # nothing of theirs in the state.
        self.assertIn("data.external.orphan_snapshots.result.missing", triggers)
        self.assertIn("data.external.orphan_snapshots.result.source_disk", triggers)
        self.assertIn("data.external.orphan_snapshots.result.read_at", triggers)
        probe = stack[stack.index('data "external" "orphan_snapshots"'):]
        probe = probe[: probe.index("\n}\n")]
        self.assertIn('program = ["python3", "${path.module}/orphan_snapshot.py", "probe"]', probe)
        for key in QUERY:
            self.assertIn(f"{key} ", probe)
        # The planter is a standing allow-list entry: its replace is how a
        # deleted snapshot is re-taken, and the script leaves the rest alone.
        allow = json.loads((_REPO_ROOT / "bench" / "tf" / "fleet" / "reconcile-allow.json").read_text())
        entry = next(e for e in allow if e["address"] == "terraform_data.orphan_snapshots")
        self.assertTrue(entry["standing"])
        # The reconcile hashes the script as a stack input, so an edit to it
        # alone moves the fleet tree.
        reconcile = (_REPO_ROOT / "hack" / "fleet_reconcile.py").read_text()
        self.assertIn('"orphan_snapshot.py"', reconcile[reconcile.index("FLEET_INPUT_NAMES = ("):].split("\n", 1)[0])


if __name__ == "__main__":
    unittest.main()
