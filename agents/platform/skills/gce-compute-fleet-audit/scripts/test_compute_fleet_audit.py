#!/usr/bin/env python3
"""Unit tests for compute_fleet_audit.py."""

import datetime
import hashlib
import io
import json
import os
import subprocess
import sys
import unittest
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
import compute_fleet_audit as cf  # noqa: E402

FLEET_AUDIT_SCRIPTS = os.path.join(os.path.dirname(__file__), "..", "..", "fleet-audit", "scripts")
sys.path.insert(0, os.path.abspath(FLEET_AUDIT_SCRIPTS))

NOW = datetime.datetime(2026, 9, 4, tzinfo=datetime.timezone.utc)
OLD_STAMP = "2020-01-01T00:00:00.000-08:00"
FRESH_STAMP = "2026-09-01T00:00:00.000-08:00"


def run_of(rc: int, stdout: str = "", stderr: str = "") -> cf.Run:
    return cf.Run(["gcloud"], rc, stdout, stderr, 0.01)


class Unstubbed(BaseException):
    """A call no stub modelled. A `BaseException`, so `collect_fleet`'s
    `except Exception` cannot turn it into an ordinary gate-failed row: the
    test errors naming the command instead."""


def unevaluated(entry: dict) -> dict:
    """`{check: reason}` for what a target lists in `checks_unevaluated`."""
    return {e["check"]: e["reason"] for e in entry.get("checks_unevaluated") or []}


# The metadata block that makes an instance one §2.1 has anything to say about.
# Without a startup script `google_metadata_script_runner` never runs, so the
# exit status the check greps for cannot appear and the instance is not a
# target — see `NO_STARTUP_SCRIPT_REASON`.
STARTUP_META = '"metadata": {"items": [{"key": "startup-script", "value": "#!/bin/sh"}]}'
# What `project-info describe` returns on a project that sets no fleet-wide
# startup script. The common case, and the one the reference install is in.
NO_PROJECT_STARTUP_SCRIPT = '{"commonInstanceMetadata": {"items": []}}'


def one_running_instance(
    name: str = "vm-1", zone: str = "us-central1-a", *, startup_script: bool = True
) -> str:
    meta = f", {STARTUP_META}" if startup_script else ""
    return (
        f'[{{"name": "{name}", "status": "RUNNING", '
        f'"zone": "https://x/projects/proj-1/zones/{zone}"{meta}}}]'
    )


def mig(name: str = "mig-1", zone: str = "us-central1-a", **actions) -> dict:
    """One `instance-groups managed list` item, converged unless told otherwise.

    All thirteen `currentActions` counters are present and zero, the way the
    API publishes them, so a test that wants churn names only the counter it
    is raising.
    """
    counters = {
        key: 0
        for key in (
            "abandoning", "creating", "creatingWithoutRetries", "deleting",
            "none", "recreating", "refreshing", "restarting", "resuming",
            "starting", "stopping", "suspending", "verifying",
        )
    }
    counters.update(actions)
    return {
        "name": name,
        "size": 2,
        "targetSize": 2,
        "zone": f"https://x/projects/proj-1/zones/{zone}",
        "status": {"isStable": not any(counters.values()), "versionTarget": {"isReached": True}},
        "currentActions": counters,
    }


def node(cpus: int = 8, used_cpus: int = 0, mem: int = 32768, used_mem: int = 0) -> dict:
    """One `sole-tenancy node-groups list-nodes` item."""
    return {
        "totalResources": {"guestCpus": cpus, "memoryMb": mem},
        "consumedResources": {"guestCpus": used_cpus, "memoryMb": used_mem},
    }


class DefaultRunTest(unittest.TestCase):
    def test_a_timeout_after_writing_stderr_returns_text(self):
        expired = subprocess.TimeoutExpired(["gcloud"], 1, output=b"partial", stderr=b"WARNING: slow")
        with patch.object(cf.subprocess, "run", side_effect=expired):
            result = cf.default_run(["gcloud", "compute", "instances", "list"])
        self.assertEqual(result.rc, cf.TIMEOUT_RC)
        self.assertEqual((result.stdout, result.stderr), ("partial", "WARNING: slow"))


class RunAndGateTest(unittest.TestCase):
    def test_gate_closes_on_nonzero_rc(self):
        parsed, result = cf.run_and_gate(["gcloud"], run=lambda argv, **kw: run_of(1, "[]", "boom"))
        self.assertIsNone(parsed)
        self.assertEqual(result.rc, 1)

    def test_gate_closes_on_empty_stdout(self):
        parsed, _ = cf.run_and_gate(["gcloud"], run=lambda argv, **kw: run_of(0, "   "))
        self.assertIsNone(parsed)

    def test_gate_closes_on_non_json(self):
        parsed, _ = cf.run_and_gate(["gcloud"], run=lambda argv, **kw: run_of(0, "ERROR: nope"))
        self.assertIsNone(parsed)

    def test_gate_opens_on_clean_json(self):
        parsed, _ = cf.run_and_gate(["gcloud"], run=lambda argv, **kw: run_of(0, '[{"name": "a"}]'))
        self.assertEqual(parsed, [{"name": "a"}])


class HelpersTest(unittest.TestCase):
    def test_last_segment(self):
        self.assertEqual(cf._last_segment("https://x/zones/us-central1-a"), "us-central1-a")
        self.assertEqual(cf._last_segment(""), "")

    def test_redact_leaves_the_marker_line_readable(self):
        """The excerpt has to survive redaction or the finding says nothing.

        `adopt_collector_evidence` overwrites the model's excerpt with this
        string, so a redactor that swallowed §2.1's own marker would publish a
        `critical` finding whose evidence line is `[REDACTED]`.
        """
        self.assertEqual(cf.redact("startup-script exit status 1"), "startup-script exit status 1")

    def test_redact_removes_an_oauth_token(self):
        """The SOP's red line, enforced here rather than by the model.

        Before the manifest conversion the model retyped every excerpt and
        could drop a secret on the way; the collector's excerpt now ships
        verbatim, so scrubbing is this file's job.
        """
        out = cf.redact("curl -H ya29.a0AfH6SMBxxxxxxxxxxxxxxxx failed: startup-script exit status 1")
        self.assertNotIn("ya29.a0AfH6SMB", out)
        self.assertIn(cf.REDACTED, out)
        self.assertIn("startup-script exit status 1", out)

    def test_redact_removes_a_private_key_header(self):
        self.assertNotIn("PRIVATE KEY", cf.redact("-----BEGIN RSA PRIVATE KEY-----"))

    def test_redact_removes_a_named_secret_assignment(self):
        self.assertNotIn("hunter2hunter2", cf.redact("password=hunter2hunter2"))

    def test_redact_clips_to_the_document_excerpt_ceiling(self):
        self.assertLessEqual(len(cf.redact("x" * 5000)), cf.MAX_EXCERPT_CHARS)


class StartupScriptTest(unittest.TestCase):
    def test_flags_the_exit_status_marker(self):
        hit = cf.check_startup_script("vm-1", "us-central1-a", "boot\nstartup-script exit status 1\nmore\n")
        self.assertEqual(hit["object"], "ComputeInstance/us-central1-a/vm-1")
        self.assertEqual(hit["excerpt"], "startup-script exit status 1")

    def test_the_current_guest_agent_failure_line_is_flagged(self):
        # As the planted VM's console printed it on 2026-10-06.
        line = (
            "2026-10-06T23:14:30.922156+00:00 fa2-gce-startup-fail google_metadata_script_runner[926]: "
            'Script "startup-script" failed with error: exit status 1'
        )
        serial = line + "\n...: Finished running startup scripts\n"
        hit = cf.check_startup_script("fa2-gce-startup-fail", "us-central1-a", serial)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["excerpt"], line)

    def test_any_non_zero_status_and_the_url_variant_are_flagged(self):
        for text in (
            "startup-script exit status 2",
            "startup-script-url exit status 127",
            'Script "startup-script-url" failed with error: exit status 3',
        ):
            with self.subTest(text):
                self.assertIsNotNone(cf.check_startup_script("vm-1", "us-central1-a", text + "\n"))

    def test_a_failure_an_earlier_run_left_on_the_console_is_not_flagged(self):
        serial = (
            "google_metadata_script_runner[926]: Starting startup scripts (version 20250101)\n"
            'google_metadata_script_runner[926]: Script "startup-script" failed with error: exit status 1\n'
            "google_metadata_script_runner[812]: Starting startup scripts (version 20250101)\n"
            "google_metadata_script_runner[812]: Finished running startup scripts\n"
        )
        self.assertIsNone(cf.check_startup_script("vm-1", "us-central1-a", serial))
        failing_again = serial + 'google_metadata_script_runner[812]: Script "startup-script" failed with error: exit status 2\n'
        self.assertIsNotNone(cf.check_startup_script("vm-1", "us-central1-a", failing_again))

    def test_a_windows_startup_script_failure_is_flagged(self):
        line = 'GCEMetadataScripts: Script "windows-startup-script-ps1" failed with error: exit status 1'
        self.assertIsNotNone(cf.check_startup_script("win-1", "us-central1-a", line + "\n"))

    def test_a_clean_boot_is_not_flagged(self):
        for text in (
            "startup-script exit status 0",
            "Finished running startup scripts",
            'Script "startup-script" exited successfully',
        ):
            with self.subTest(text):
                self.assertIsNone(cf.check_startup_script("vm-1", "us-central1-a", text + "\n"))

    def test_flags_the_finished_with_error_marker(self):
        hit = cf.check_startup_script("vm-2", "us-central1-a", "Finished running startup scripts with error\n")
        self.assertEqual(hit["object"], "ComputeInstance/us-central1-a/vm-2")

    def test_does_not_flag_a_clean_boot(self):
        self.assertIsNone(cf.check_startup_script("vm-1", "us-central1-a", "Finished running startup scripts.\n"))

    def test_the_match_is_case_sensitive(self):
        """`STARTUP_FAILURE_PATTERN` is case-sensitive, as the guest agent's
        lines are, so a differently-cased line is not a hit. Pinned so a later
        change to the matcher is a deliberate change to what gets flagged
        rather than a side effect."""
        self.assertIsNone(cf.check_startup_script("vm-1", "us-central1-a", "STARTUP-SCRIPT EXIT STATUS 1\n"))

    def test_first_match_wins_and_the_scan_stops(self):
        hit = cf.check_startup_script("vm-1", "us-central1-a",
            "startup-script exit status 1\nFinished running startup scripts with error\n",
        )
        self.assertEqual(hit["excerpt"], "startup-script exit status 1")

    def test_empty_serial_output_is_not_a_crash(self):
        self.assertIsNone(cf.check_startup_script("vm-1", "us-central1-a", ""))

    def test_the_hit_hands_the_gke_exclusion_back_to_the_model(self):
        """§2.1's Do-NOT-flag limb excludes GKE-managed nodes and this
        collector does not apply it, so the candidate says so rather than
        presenting the hit as fully mechanical."""
        hit = cf.check_startup_script("gke-pool-1-abcd", "us-central1-a", "startup-script exit status 1\n")
        self.assertEqual(hit["needs_triage"], cf.TRIAGE_GKE_NODE)

    def test_a_prefixed_credential_name_is_redacted(self):
        for line in ("DB_PASSWORD=hunter2 startup-script exit status 1", "GITHUB_TOKEN=abc123 startup-script exit status 1"):
            with self.subTest(line):
                hit = cf.check_startup_script("vm-1", "us-central1-a", line + "\n")
                self.assertNotIn("hunter2", hit["excerpt"])
                self.assertNotIn("abc123", hit["excerpt"])

    def test_a_secret_in_the_serial_line_never_reaches_the_excerpt(self):
        hit = cf.check_startup_script("vm-1", "us-central1-a", "token: ya29.c.b0Aaekm1Jxxxxxxxxxxxxxxxxxxxxxx startup-script exit status 1\n"
        )
        self.assertNotIn("ya29.c.b0Aaekm1J", hit["excerpt"])

    def test_a_service_account_token_with_no_keyword_before_it_is_redacted(self):
        # Short of the 40-character catch-all and with no `token` word to
        # trip the keyword pattern: only the `ya29` pattern can catch it.
        hit = cf.check_startup_script("vm-1", "us-central1-a", "ya29.c.b0Aaekm1Jxxxxxxxxxxxxxxxxxxxxxx startup-script exit status 1\n")
        self.assertNotIn("b0Aaekm1J", hit["excerpt"])


class RunningInstancesTest(unittest.TestCase):
    def test_keeps_running_instances_and_splits_the_zone(self):
        self.assertEqual(
            cf.running_instances(
                [{"name": "vm-1", "status": "RUNNING", "zone": "https://x/zones/us-central1-a"}]
            ),
            [("vm-1", "us-central1-a")],
        )

    def test_drops_a_terminated_instance(self):
        self.assertEqual(
            cf.running_instances(
                [{"name": "vm-1", "status": "TERMINATED", "zone": "https://x/zones/us-central1-a"}]
            ),
            [],
        )

    def test_drops_an_instance_with_no_addressable_zone(self):
        self.assertEqual(cf.running_instances([{"name": "vm-1", "status": "RUNNING"}]), [])

    def test_a_non_object_item_is_not_a_crash(self):
        self.assertEqual(cf.running_instances(["vm-1", None]), [])


class OrphanedSnapshotTest(unittest.TestCase):
    def snapshot(self, **overrides) -> dict:
        base = {
            "name": "snap-1",
            "sourceDisk": "https://x/projects/proj-1/zones/us-central1-a/disks/gone",
            "creationTimestamp": OLD_STAMP,
        }
        base.update(overrides)
        return base

    def test_flags_an_old_snapshot_of_a_deleted_disk(self):
        hit = cf.check_orphaned_snapshot(self.snapshot(), set(), NOW)
        self.assertEqual(hit["object"], "Snapshot/snap-1")
        self.assertIn('"sourceDisk": "gone"', hit["excerpt"])
        self.assertEqual(hit["needs_triage"], cf.TRIAGE_RETENTION_HOLD)

    def test_does_not_flag_when_the_disk_is_still_there_by_name(self):
        self.assertIsNone(cf.check_orphaned_snapshot(self.snapshot(), {"gone"}, NOW))

    def test_does_not_flag_when_the_disk_is_still_there_by_self_link(self):
        """A `sourceDisk` arrives as a bare name in some payloads and as a full
        `selfLink` in others; indexing only one form deletes live disks'
        snapshots."""
        link = "https://x/projects/proj-1/zones/us-central1-a/disks/gone"
        self.assertIsNone(cf.check_orphaned_snapshot(self.snapshot(), {link}, NOW))

    def test_does_not_flag_a_snapshot_a_schedule_took(self):
        """A schedule-created snapshot names its schedule; Snapshot carries no
        `resourcePolicies`, which is a disk field."""
        for key in ("sourceSnapshotSchedulePolicy", "sourceSnapshotSchedulePolicyId"):
            with self.subTest(key):
                self.assertIsNone(
                    cf.check_orphaned_snapshot(self.snapshot(**{key: "daily"}), set(), NOW)
                )
        self.assertIsNotNone(
            cf.check_orphaned_snapshot(self.snapshot(resourcePolicies=["daily"]), set(), NOW)
        )

    def test_does_not_flag_a_snapshot_inside_the_ninety_day_window(self):
        self.assertIsNone(
            cf.check_orphaned_snapshot(self.snapshot(creationTimestamp=FRESH_STAMP), set(), NOW)
        )

    def test_does_not_flag_a_snapshot_with_no_source_disk(self):
        self.assertIsNone(cf.check_orphaned_snapshot(self.snapshot(sourceDisk=""), set(), NOW))

    def test_an_unparseable_timestamp_is_not_a_crash_and_not_a_finding(self):
        self.assertIsNone(
            cf.check_orphaned_snapshot(self.snapshot(creationTimestamp="whenever"), set(), NOW)
        )

    def test_a_naive_timestamp_is_not_a_crash(self):
        """`now` is offset-aware, so subtracting a naive stamp raises
        `TypeError` rather than `ValueError`. Catching only the parse error
        lost the whole project to one badly-stamped snapshot."""
        self.assertIsNone(
            cf.check_orphaned_snapshot(
                self.snapshot(creationTimestamp="2020-01-01T00:00:00"), set(), NOW
            )
        )

    def test_a_missing_timestamp_is_not_a_finding(self):
        self.assertIsNone(
            cf.check_orphaned_snapshot(self.snapshot(creationTimestamp=""), set(), NOW)
        )

    def test_active_disk_index_carries_both_spellings(self):
        index = cf.active_disk_index([{"name": "d1", "selfLink": "https://x/disks/d1"}])
        self.assertEqual(index, {"d1", "https://x/disks/d1"})

    def test_active_disk_index_tolerates_a_non_object(self):
        self.assertEqual(cf.active_disk_index([None, "d1"]), set())


class CollectProjectTest(unittest.TestCase):
    def fake_run(self, responses: dict) -> cf.RunFn:
        """`responses` maps a substring of the joined argv to a `Run`. First
        match wins, in insertion order, so a test can stub only the calls it
        cares about."""

        def run(argv, **kwargs):
            joined = " ".join(argv)
            for needle, result in responses.items():
                if needle in joined:
                    return result
            raise Unstubbed(f"unstubbed command: {joined}")

        return run

    def clean(self, **overrides) -> dict:
        # `node-groups list-nodes` is stubbed before `node-groups list` because
        # `fake_run` matches on substring in insertion order and the shorter
        # needle is a prefix of the longer command.
        responses = {
            "instances list": run_of(0, one_running_instance()),
            "project-info describe": run_of(0, NO_PROJECT_STARTUP_SCRIPT),
            "get-serial-port-output": run_of(0, "boot ok\n"),
            "instance-groups managed list": run_of(0, json.dumps([mig()])),
            "node-groups list-nodes": run_of(0, "[]"),
            "node-groups list": run_of(0, "[]"),
            "disks list": run_of(0, "[]"),
            "snapshots list": run_of(0, "[]"),
        }
        responses.update(overrides)
        return responses

    # --- schema conformance ------------------------------------------------ #

    def test_a_collected_target_carries_every_required_key(self):
        entry = cf.collect_project("proj-1", run=self.fake_run(self.clean()))
        self.assertEqual(entry["name"], "project/proj-1")
        self.assertEqual(entry["project"], "proj-1")
        self.assertEqual(entry["location"], "global")
        self.assertEqual(entry["outcome"], "collected")
        for key in ("commands", "candidates", "checks_not_applicable"):
            self.assertIsInstance(entry[key], list, key)

    def test_a_project_target_carries_no_autopilot_key(self):
        """§6: the mode is a property of a cluster, and `false` on a target
        that stands for a project reads as a fleet of Standard clusters."""
        entry = cf.collect_project("proj-1", run=self.fake_run(self.clean()))
        self.assertNotIn("autopilot", entry)

    def test_every_commands_entry_is_an_object_with_a_string_check_and_int_rc(self):
        """`cross_check_manifest` reads `entry.get("check")` with no isinstance
        guard and compares `rc == 0` as an integer: a bare string in the list
        crashes `finish`, and a `"0"` string silently fails the claim."""
        entry = cf.collect_project("proj-1", run=self.fake_run(self.clean()))
        for record in entry["commands"]:
            self.assertIsInstance(record, dict)
            self.assertIsInstance(record["check"], str)
            self.assertEqual(record["check"], record["check"].strip())
            self.assertIsInstance(record["rc"], int)
            self.assertNotIsInstance(record["rc"], bool)
            self.assertIsInstance(record["command"], str)
            self.assertGreaterEqual(len(record["command"]), 8)
            self.assertLessEqual(len(record["command"]), cf.MAX_COMMAND_CHARS)

    def test_every_not_applicable_entry_is_an_object_with_a_usable_reason(self):
        """`validate_na_reason` refuses a reason under sixteen characters, and
        `cross_check_manifest` calls `.get` on the entry without a guard."""
        entry = cf.collect_project("proj-1", run=self.fake_run(self.clean()))
        for record in entry["checks_not_applicable"]:
            self.assertIsInstance(record, dict)
            self.assertIsInstance(record["check"], str)
            self.assertGreaterEqual(len(record["reason"]), 16)

    def test_every_candidate_carries_the_identity_fields(self):
        responses = self.clean(
            **{
                "get-serial-port-output": run_of(0, "startup-script exit status 1\n"),
                "snapshots list": run_of(
                    0,
                    '[{"name": "snap-1", "sourceDisk": "https://x/disks/gone", '
                    f'"creationTimestamp": "{OLD_STAMP}"}}]',
                ),
            }
        )
        entry = cf.collect_project("proj-1", run=self.fake_run(responses))
        self.assertEqual(len(entry["candidates"]), 2)
        for candidate in entry["candidates"]:
            self.assertIn(candidate["check"], cf.SEVERITY)
            self.assertEqual(candidate["namespace"], "")
            self.assertTrue(candidate["object"])
            self.assertIn(candidate["severity"], ("critical", "major", "minor"))
            self.assertTrue(candidate["excerpt"])
            self.assertTrue(candidate["impact"])
            self.assertIn("needs_triage", candidate)

    def test_candidate_identities_are_unique_within_the_target(self):
        """`finish` refuses a document holding two findings that agree on
        (check, cluster, namespace, object)."""
        entry = cf.collect_project(
            "proj-1",
            run=self.fake_run(
                self.clean(
                    **{
                        "get-serial-port-output": run_of(0, "startup-script exit status 1\n"),
                        "snapshots list": run_of(
                            0,
                            '[{"name": "snap-1", "sourceDisk": "https://x/disks/a", '
                            f'"creationTimestamp": "{OLD_STAMP}"}}, '
                            '{"name": "snap-2", "sourceDisk": "https://x/disks/b", '
                            f'"creationTimestamp": "{OLD_STAMP}"}}]',
                        ),
                    }
                )
            ),
        )
        ids = [(c["check"], c["namespace"], c["object"]) for c in entry["candidates"]]
        self.assertEqual(len(ids), len(set(ids)))

    # --- detection through the collector ----------------------------------- #

    def test_the_severities_are_the_sop_severities(self):
        entry = cf.collect_project(
            "proj-1",
            run=self.fake_run(
                self.clean(
                    **{
                        "get-serial-port-output": run_of(0, "startup-script exit status 1\n"),
                        "snapshots list": run_of(
                            0,
                            '[{"name": "snap-1", "sourceDisk": "https://x/disks/gone", '
                            f'"creationTimestamp": "{OLD_STAMP}"}}]',
                        ),
                    }
                )
            ),
        )
        by_check = {c["check"]: c["severity"] for c in entry["candidates"]}
        self.assertEqual(by_check["gce-startup-script-status"], "critical")
        self.assertEqual(by_check["orphaned-snapshots"], "minor")

    def test_a_snapshot_of_a_live_disk_is_not_flagged_through_the_collector(self):
        entry = cf.collect_project(
            "proj-1",
            run=self.fake_run(
                self.clean(
                    **{
                        "disks list": run_of(0, '[{"name": "gone"}]'),
                        "snapshots list": run_of(
                            0,
                            '[{"name": "snap-1", "sourceDisk": "https://x/disks/gone", '
                            f'"creationTimestamp": "{OLD_STAMP}"}}]',
                        ),
                    }
                )
            ),
        )
        self.assertEqual(entry["candidates"], [])

    def test_a_project_with_nothing_wrong_is_collected_with_no_candidates(self):
        entry = cf.collect_project("proj-1", run=self.fake_run(self.clean()))
        self.assertEqual(entry["outcome"], "collected")
        self.assertEqual(entry["candidates"], [])
        self.assertNotIn("limitations", entry)

    def test_zero_instances_is_declared_because_it_does_not_prove_zero(self):
        """An empty `instances list` is not a fleet of zero VMs.

        This test previously asserted the opposite — that absence provably
        means zero, so §2.1 could record the enumeration and declare nothing.
        GKE Autopilot node VMs are the counterexample: they are absent from the
        Compute API for the audit identity by design, returning 404 rather than
        403, so a project whose nodes are all Autopilot enumerates empty while
        running plenty of VMs. Recording the slug as run against that is a
        clean bill of health for a fleet nobody looked at.

        Declared not applicable rather than unevaluated, because the exclusion is a
        genuine narrowing of the universe: Google manages those nodes, so their
        startup scripts are not the operator's to set or fix. A marker would
        union the project into `blocked` and pin every GCE finding on it open
        forever, and this project is the stream's only target.
        """
        entry = cf.collect_project(
            "proj-1", run=self.fake_run(self.clean(**{"instances list": run_of(0, "[]")}))
        )
        declared = {d["check"]: d["reason"] for d in entry["checks_not_applicable"]}
        self.assertIn(cf.STARTUP_SLUG, declared)
        self.assertNotIn(cf.STARTUP_SLUG, [c["check"] for c in entry["commands"]])
        self.assertNotIn(cf.STARTUP_SLUG, unevaluated(entry))
        self.assertIn("Autopilot", declared[cf.STARTUP_SLUG])
        self.assertNotIn("limitations", entry)

    def test_instances_that_exist_but_none_running_is_declared_too(self):
        """A stopped fleet serves no serial console, so §2.1 had nothing to read.

        Distinct from the empty case above and from the all-refused case below:
        the instances were enumerated and their state positively establishes
        that the check cannot apply, so it is not applicable rather than unevaluated
        and the reason cites the count it saw.
        """
        stopped = json.dumps(
            [{"name": "vm-1", "zone": "https://x/zones/us-central1-a", "status": "TERMINATED"}]
        )
        entry = cf.collect_project(
            "proj-1",
            run=self.fake_run(self.clean(**{"instances list": run_of(0, stopped)})),
        )
        declared = {d["check"]: d["reason"] for d in entry["checks_not_applicable"]}
        self.assertIn(cf.STARTUP_SLUG, declared)
        self.assertNotIn(cf.STARTUP_SLUG, unevaluated(entry))
        self.assertIn("1 instance(s)", declared[cf.STARTUP_SLUG])
        self.assertNotIn("limitations", entry)

    def test_running_instances_that_run_no_startup_script_are_declared(self):
        """The false clean this branch exists to remove, measured live.

        On the reference install §2.1 read 14 RUNNING consoles — 2.4 MB of text
        — and reported the check run and clean. Not one of those instances
        carried `startup-script` or `startup-script-url`, and the project set
        neither, so `google_metadata_script_runner` never ran and the sentence
        the check greps for could not occur in any console. Nothing was wrong
        and nothing could have been found; the pass was vacuous.

        Not applicable rather than unevaluated for the reason the empty enumeration
        is: the absence is a fact the read positively established, not a
        surface nobody reached.
        """
        entry = cf.collect_project(
            "proj-1",
            run=self.fake_run(
                self.clean(
                    **{
                        "instances list": run_of(
                            0, one_running_instance(startup_script=False)
                        )
                    }
                )
            ),
        )
        declared = {d["check"]: d["reason"] for d in entry["checks_not_applicable"]}
        self.assertIn(cf.STARTUP_SLUG, declared)
        self.assertNotIn(cf.STARTUP_SLUG, unevaluated(entry))
        self.assertIn("1 RUNNING instance(s)", declared[cf.STARTUP_SLUG])
        self.assertNotIn(cf.STARTUP_SLUG, [c["check"] for c in entry["commands"]])
        self.assertNotIn("limitations", entry)

    def test_every_absence_declaration_names_the_population_it_measured(self):
        """§2.1's three absence branches each count a fleet with holes in it.

        The Compute API hides Autopilot nodes from this identity, so every
        figure these reasons quote is the visible population rather than the
        project's. Measured live: eighteen instances exist on the reference
        install and the platform GSA enumerates fourteen. Only the empty-
        enumeration branch said so; the other two quoted their counts flat.
        """
        cases = {
            "no instances at all": "[]",
            "instances, none RUNNING": json.dumps(
                [
                    {
                        "name": "vm-1",
                        "zone": "https://x/zones/us-central1-a",
                        "status": "TERMINATED",
                    }
                ]
            ),
            "RUNNING, no startup script": one_running_instance(startup_script=False),
        }
        for desc, listing in cases.items():
            with self.subTest(case=desc):
                entry = cf.collect_project(
                    "proj-1",
                    run=self.fake_run(
                        self.clean(**{"instances list": run_of(0, listing)})
                    ),
                )
                reason = {
                    d["check"]: d["reason"] for d in entry["checks_not_applicable"]
                }[cf.STARTUP_SLUG]
                self.assertIn("visible to the audit identity", reason, desc)
                self.assertIn("Autopilot", reason, desc)

    def test_the_no_startup_script_reason_holds_only_for_standard_nodes(self):
        """The reason may not generalise its Standard evidence to GKE at large.

        Each of the four `gk3-*` nodes on the reference install sets
        `startup-script` — to `echo startup-script-override`, the stub Google
        installs to neutralise the hook — while every visible `gke-*` node sets
        `user-data` and `kube-env` and no startup-script key. The verdict is
        unaffected, since a stub on a node Google manages is not the operator's
        to act on, but the old wording explained the absence with a claim about
        GKE nodes as a class that one `instances describe` disproves. A reason a
        reader can falsify is worse than no reason: it is the only thing
        standing between them and trusting this `not applicable`.
        """
        entry = cf.collect_project(
            "proj-1",
            run=self.fake_run(
                self.clean(
                    **{
                        "instances list": run_of(
                            0, one_running_instance(startup_script=False)
                        )
                    }
                )
            ),
        )
        reason = {
            d["check"]: d["reason"] for d in entry["checks_not_applicable"]
        }[cf.STARTUP_SLUG]
        self.assertIn("Standard node pool", reason)
        self.assertNotIn("a project whose only instances are GKE nodes", reason)

    def test_a_windows_startup_script_makes_an_instance_a_target(self):
        seen = []
        windows = (
            '[{"name": "win-1", "status": "RUNNING", "zone": "https://x/projects/proj-1/zones/us-central1-a", '
            '"metadata": {"items": [{"key": "windows-startup-script-ps1", "value": "exit 1"}]}}]'
        )

        def run(argv, **kwargs):
            joined = " ".join(argv)
            seen.append(joined)
            if "instances list" in joined:
                return run_of(0, windows)
            if "project-info describe" in joined:
                return run_of(0, NO_PROJECT_STARTUP_SCRIPT)
            return run_of(0, "[]")

        cf.collect_project("proj-1", run=run)
        self.assertTrue([c for c in seen if "get-serial-port-output win-1" in c])

    def test_no_console_is_read_for_an_instance_that_runs_no_script(self):
        """The declaration has to stop the reads, not just relabel them.

        Each console read is a `gcloud` round trip against a multi-hundred-KB
        body, and on the reference install they were the bulk of the collect.
        """
        seen = []

        def run(argv, **kwargs):
            joined = " ".join(argv)
            seen.append(joined)
            if "instances list" in joined:
                return run_of(0, one_running_instance(startup_script=False))
            if "project-info describe" in joined:
                return run_of(0, NO_PROJECT_STARTUP_SCRIPT)
            return run_of(0, "[]")

        cf.collect_project("proj-1", run=run)
        self.assertFalse(
            [c for c in seen if "get-serial-port-output" in c],
            "read a console that cannot carry the marker",
        )

    def test_a_project_wide_startup_script_keeps_every_instance_a_target(self):
        """Common metadata runs on every instance in the project, so the
        per-instance keys say nothing on their own. Filtering without this read
        would declare §2.1 inapplicable over a fleet that does run scripts —
        the same false clean, arrived at from the other side."""
        entry = cf.collect_project(
            "proj-1",
            run=self.fake_run(
                self.clean(
                    **{
                        "instances list": run_of(
                            0, one_running_instance(startup_script=False)
                        ),
                        "project-info describe": run_of(
                            0,
                            '{"commonInstanceMetadata": {"items": '
                            '[{"key": "startup-script", "value": "#!/bin/sh"}]}}',
                        ),
                        "get-serial-port-output": run_of(
                            0, "startup-script exit status 1\n"
                        ),
                    }
                )
            ),
        )
        self.assertNotIn(
            cf.STARTUP_SLUG, [d["check"] for d in entry["checks_not_applicable"]]
        )
        self.assertEqual(
            [c["object"] for c in entry["candidates"]],
            ["ComputeInstance/us-central1-a/vm-1"],
        )

    def test_an_unreadable_project_info_keeps_every_instance_a_target(self):
        """Fail toward doing the work. A refused or malformed `project-info`
        read leaves the fleet-wide question open, and guessing "no script"
        there would skip §2.1 across a whole project on the strength of a read
        that did not happen. Reading a console nobody needed costs seconds; the
        other error is the false clean."""
        entry = cf.collect_project(
            "proj-1",
            run=self.fake_run(
                self.clean(
                    **{
                        "instances list": run_of(
                            0, one_running_instance(startup_script=False)
                        ),
                        "project-info describe": run_of(1, "", "PERMISSION_DENIED"),
                        "get-serial-port-output": run_of(
                            0, "startup-script exit status 1\n"
                        ),
                    }
                )
            ),
        )
        self.assertEqual(entry["outcome"], "collected")
        self.assertNotIn(
            cf.STARTUP_SLUG, [d["check"] for d in entry["checks_not_applicable"]]
        )
        self.assertEqual(len(entry["candidates"]), 1)
        # No `limitations` either: the fallback reads more, not less, so it
        # inflates no coverage claim -- and a limitation on this stream's only
        # target would hold every GCE finding open forever.
        self.assertNotIn("limitations", entry)

    def test_a_project_info_failure_does_not_fail_the_project_closed(self):
        """`gated` raises `GateFailure` on a bad read and the whole target is
        recorded unreadable. This read is deliberately outside that gate: it
        refines which instances to look at, and losing it costs precision, not
        correctness."""
        entry = cf.collect_project(
            "proj-1",
            run=self.fake_run(
                self.clean(
                    **{"project-info describe": run_of(1, "", "PERMISSION_DENIED")}
                )
            ),
        )
        self.assertEqual(entry["outcome"], "collected")

    # --- the three checks nobody implemented -------------------------------- #

    def test_an_empty_enumeration_is_structural_not_unevaluated(self):
        """The distinction the whole coverage model turns on.

        A project reserving no sole-tenant node groups gets a plain
        `checks_not_applicable` entry. Listing it in `checks_unevaluated`
        instead would make every run partial, so one absent object class
        would stop findings on the other three checks from ever being
        announced resolved and their remediation pull requests from closing.
        """
        entry = cf.collect_project("proj-1", run=self.fake_run(self.clean()))
        declared = {e["check"]: e["reason"] for e in entry["checks_not_applicable"]}
        self.assertIn(cf.SOLE_TENANT_SLUG, declared)
        self.assertNotIn(
            cf.SOLE_TENANT_SLUG,
            unevaluated(entry),
            "an enumeration that ran and came back empty is a result, not a gap",
        )

    def test_a_clean_project_carries_no_unevaluated_marker_at_all(self):
        """The regression that motivated implementing §2.2 and §2.4.

        The roster used to be five checks with three of them declared
        unevaluated on every target forever, which claimed coverage the run
        did not have. A clean project now lists nothing unevaluated and leaves
        no coverage gap, asserted through `audit_report`'s own reader.
        """
        import audit_report

        entry = cf.collect_project("proj-1", run=self.fake_run(self.clean()))
        self.assertEqual(unevaluated(entry), {})
        cluster = {
            "name": entry["name"],
            "checks_run": [
                {"check": c["check"], "command": c["command"]} for c in entry["commands"]
            ],
            "checks_not_applicable": entry["checks_not_applicable"],
        }
        doc = {"audit": cf.AUDIT_ID, "scope": {"clusters": [cluster]}, "findings": []}
        self.assertEqual(audit_report.coverage_gaps(doc), [])

    def test_every_roster_slug_is_either_run_or_declared(self):
        """No slug may go unmentioned, and the roster is the list to check
        against — hard-coding the four names here would keep passing after
        someone adds a fifth to `AuditSpec` and implements nothing."""
        import audit_report

        entry = cf.collect_project("proj-1", run=self.fake_run(self.clean()))
        accounted = {c["check"] for c in entry["commands"]} | {
            d["check"] for d in entry["checks_not_applicable"]
        }
        roster = set(audit_report.AUDITS[cf.AUDIT_ID].checks)
        self.assertEqual(roster - accounted, set())
        self.assertEqual(accounted - roster, set())

    def test_no_command_is_recorded_for_a_check_declared_inapplicable(self):
        """Rule 6.5 exists because one broad read recorded against every slug
        it feeds corroborates a claim the collector meant to refuse. The
        collector filters at the source rather than relying on the reader."""
        entry = cf.collect_project("proj-1", run=self.fake_run(self.clean()))
        declared = {e["check"] for e in entry["checks_not_applicable"]}
        recorded = {c["check"] for c in entry["commands"]}
        self.assertEqual(declared & recorded, set())

    def test_every_candidate_cites_a_roster_slug(self):
        """`finding.check` is validated against the roster plus the derived
        slugs, so a candidate citing anything else costs the whole document."""
        import audit_report

        entry = cf.collect_project(
            "proj-1",
            run=self.fake_run(self.clean(**{"get-serial-port-output": run_of(0, "startup-script exit status 1\n")})),
        )
        emitted = {c["check"] for c in entry["candidates"]}
        self.assertTrue(emitted)
        self.assertEqual(emitted - audit_report.audit_finding_checks(cf.AUDIT_ID), set())

    # --- gcloud failure handling -------------------------------------------- #

    def test_a_failed_instances_list_gate_fails_the_whole_target(self):
        entry = cf.collect_project(
            "proj-1", run=self.fake_run(self.clean(**{"instances list": run_of(1, "", "PERMISSION_DENIED")}))
        )
        self.assertEqual(entry["outcome"], "gate-failed")
        self.assertIn(cf.STARTUP_SLUG, entry["error"])
        self.assertIn("rc=1", entry["error"])
        self.assertIn("PERMISSION_DENIED", entry["error"])
        self.assertNotIn("candidates", entry)

    def test_a_failed_disks_list_gate_fails_the_target_rather_than_skipping_a_check(self):
        """The pre-manifest revision skipped the snapshot check silently and
        left the project in scope carrying one check, with nothing saying the
        other had been abandoned."""
        entry = cf.collect_project("proj-1", run=self.fake_run(self.clean(**{"disks list": run_of(1, "", "denied")})))
        self.assertEqual(entry["outcome"], "gate-failed")
        self.assertIn(cf.SNAPSHOT_SLUG, entry["error"])

    def test_a_failed_snapshots_list_gate_fails_the_target(self):
        entry = cf.collect_project(
            "proj-1", run=self.fake_run(self.clean(**{"snapshots list": run_of(1, "", "denied")}))
        )
        self.assertEqual(entry["outcome"], "gate-failed")
        self.assertIn(cf.SNAPSHOT_SLUG, entry["error"])

    def test_non_json_output_gates_closed(self):
        entry = cf.collect_project(
            "proj-1", run=self.fake_run(self.clean(**{"instances list": run_of(0, "Updates are available")}))
        )
        self.assertEqual(entry["outcome"], "gate-failed")

    def test_a_json_object_where_an_array_was_expected_gates_closed(self):
        """A dict passes `json.loads` and iterating it yields keys, so reading
        zero items off an error envelope would report an empty, healthy
        project."""
        entry = cf.collect_project(
            "proj-1", run=self.fake_run(self.clean(**{"snapshots list": run_of(0, '{"error": "nope"}')}))
        )
        self.assertEqual(entry["outcome"], "gate-failed")
        self.assertIn("not a JSON array", entry["error"])

    def test_the_error_is_clipped_at_the_source(self):
        entry = cf.collect_project(
            "proj-1", run=self.fake_run(self.clean(**{"instances list": run_of(1, "", "x" * 5000)}))
        )
        self.assertLessEqual(len(entry["error"]), cf.ERROR_CLIP_CHARS)

    def test_a_gate_failed_target_still_appears_in_the_manifest(self):
        """A target missing from `clusters[]` is indistinguishable from a
        target that does not exist, and the document is then free to publish a
        fleet-wide all-clear over it."""
        manifest = cf.collect_fleet(
            "proj-1", run=self.fake_run(self.clean(**{"instances list": run_of(1, "", "denied")}))
        )
        self.assertEqual(
            [c["name"] for c in manifest["clusters"]],
            ["project/proj-1", cf.UNENUMERATED_PROJECTS_TARGET],
        )

    def test_one_failed_serial_read_costs_coverage_not_the_project(self):
        seen = []

        def run(argv, **kwargs):
            joined = " ".join(argv)
            seen.append(joined)
            if "instances list" in joined:
                return run_of(
                    0,
                    '[{"name": "vm-1", "status": "RUNNING", "zone": "https://x/zones/z1"}, '
                    '{"name": "vm-2", "status": "RUNNING", "zone": "https://x/zones/z1"}]',
                )
            if "get-serial-port-output vm-1" in joined:
                return run_of(1, "", "instance not ready")
            if "get-serial-port-output vm-2" in joined:
                return run_of(0, "startup-script exit status 1\n")
            return run_of(0, "[]")

        entry = cf.collect_project("proj-1", run=run)
        self.assertEqual(entry["outcome"], "collected")
        self.assertEqual([c["object"] for c in entry["candidates"]], ["ComputeInstance/z1/vm-2"])
        self.assertIn("vm-1 (z1)", entry["limitations"])
        self.assertIn(cf.STARTUP_SLUG, [c["check"] for c in entry["commands"]])

    def test_every_serial_read_failing_is_unevaluated_not_a_clean_fleet(self):
        def run(argv, **kwargs):
            joined = " ".join(argv)
            if "instances list" in joined:
                return run_of(0, one_running_instance())
            if "get-serial-port-output" in joined:
                return run_of(1, "", "instance not ready")
            return run_of(0, "[]")

        entry = cf.collect_project("proj-1", run=run)
        self.assertEqual(entry["outcome"], "collected")
        self.assertIn(cf.STARTUP_SLUG, unevaluated(entry))
        self.assertNotIn(cf.STARTUP_SLUG, {e["check"] for e in entry["checks_not_applicable"]})
        self.assertNotIn(cf.STARTUP_SLUG, [c["check"] for c in entry["commands"]])
        self.assertIn("could not be evaluated", entry["limitations"])

    def test_an_empty_serial_body_counts_as_unread(self):
        """`rc == 0` with nothing on stdout is not a clean read of a console
        with no errors in it — it is a read that returned nothing."""

        def run(argv, **kwargs):
            joined = " ".join(argv)
            if "instances list" in joined:
                return run_of(0, one_running_instance())
            if "get-serial-port-output" in joined:
                return run_of(0, "   ")
            return run_of(0, "[]")

        entry = cf.collect_project("proj-1", run=run)
        self.assertIn(cf.STARTUP_SLUG, unevaluated(entry))
        self.assertNotIn(cf.STARTUP_SLUG, {e["check"] for e in entry["checks_not_applicable"]})

    # --- provenance ---------------------------------------------------------- #

    def test_the_recorded_command_is_the_argv_that_ran(self):
        """The pre-manifest revision recorded `--project=<id>` while running
        `--project <id>`, and recorded the project-wide enumeration for a
        finding produced by a per-instance read."""
        seen = []

        def run(argv, **kwargs):
            joined = " ".join(argv)
            seen.append(joined)
            if "instances list" in joined:
                return run_of(0, one_running_instance())
            if "get-serial-port-output" in joined:
                return run_of(0, "boot ok\n")
            return run_of(0, "[]")

        entry = cf.collect_project("proj-1", run=run)
        for record in entry["commands"]:
            for part in record["command"].split(" && "):
                self.assertIn(part.strip(), seen)

    def test_the_serial_read_names_port_one(self):
        """§2.1's command and its evidence example both spell `--port=1`, and
        the model copies this string into `checks_run`."""
        entry = cf.collect_project(
            "proj-1",
            run=self.fake_run(self.clean()),
        )
        startup = next(c for c in entry["commands"] if c["check"] == cf.STARTUP_SLUG)
        self.assertIn("--port=1", startup["command"])

    def test_a_joined_command_stays_under_the_harness_ceiling(self):
        """An over-length `command` is not a clipped field: `validate_check_command`
        refuses the whole document, so a project with enough VMs would publish
        nothing at all."""
        instances = ",".join(
            f'{{"name": "vm-{i}", "status": "RUNNING", "zone": "https://x/zones/us-central1-a"}}'
            for i in range(200)
        )

        def run(argv, **kwargs):
            joined = " ".join(argv)
            if "instances list" in joined:
                return run_of(0, f"[{instances}]")
            if "get-serial-port-output" in joined:
                return run_of(0, "boot ok\n")
            return run_of(0, "[]")

        entry = cf.collect_project("proj-1", run=run)
        startup = next(c for c in entry["commands"] if c["check"] == cf.STARTUP_SLUG)
        self.assertLessEqual(len(startup["command"]), cf.MAX_COMMAND_CHARS)
        self.assertIn("more read(s) of the same shape", startup["command"])


class CollectFleetTest(unittest.TestCase):
    def stub(self):
        # One RUNNING instance per project, so no project is left out as
        # holding nothing to audit.
        def run(argv, **kwargs):
            if "instances list" in " ".join(argv):
                return run_of(0, one_running_instance())
            return run_of(0, "[]")

        return run

    def env(self, **values):
        return patch.dict(os.environ, {**NO_PROJECT_ENV, **values})

    def test_sweeps_every_monitored_project(self):
        with self.env(MONITORED_PROJECT_IDS="p-a, p-b"):
            manifest = cf.collect_fleet(run=self.stub())
        self.assertEqual(
            sorted(c["name"] for c in manifest["clusters"]),
            sorted(["project/p-a", "project/p-b", cf.UNENUMERATED_PROJECTS_TARGET]),
        )

    def test_single_project_override_bypasses_the_environment(self):
        with self.env(MONITORED_PROJECT_IDS="p-a,p-b"):
            manifest = cf.collect_fleet("only-this", run=self.stub())
        self.assertEqual(
            [c["name"] for c in manifest["clusters"]],
            ["project/only-this", cf.UNENUMERATED_PROJECTS_TARGET],
        )

    def test_one_project_crashing_costs_that_project_and_no_other(self):
        """The SOP redirects stdout into the manifest file, so an exception
        escaping `collect_fleet` truncates it — the run loses every project to
        one bad object instead of one."""
        stub = self.stub()

        def run(argv, **kwargs):
            joined = " ".join(argv)
            if "p-a" in joined:
                raise TypeError("unmodelled")
            return stub(argv, **kwargs)

        with self.env(MONITORED_PROJECT_IDS="p-a,p-b"):
            manifest = cf.collect_fleet(run=run)
        by_name = {c["name"]: c for c in manifest["clusters"]}
        self.assertEqual(by_name["project/p-a"]["outcome"], "gate-failed")
        self.assertIn("TypeError", by_name["project/p-a"]["error"])
        self.assertEqual(by_name["project/p-b"]["outcome"], "collected")

    def test_no_project_resolved_still_produces_a_target(self):
        """An empty `clusters` list reads as a fleet with nothing in it, which
        is a clean, fully covered scope."""
        with self.env():
            manifest = cf.collect_fleet(run=lambda argv, **kw: run_of(1, "", "no auth"))
        self.assertEqual(
            [c["name"] for c in manifest["clusters"]],
            ["project/unknown", cf.UNENUMERATED_PROJECTS_TARGET],
        )
        self.assertEqual({c["outcome"] for c in manifest["clusters"]}, {"gate-failed"})

    def test_a_project_holding_nothing_to_audit_is_listed_with_every_check_inapplicable(self):
        """No instance, MIG, node group or snapshot. Listed with all four
        reasons and no limitations note: `finish` takes that as accounted for,
        and a single-project install still has a target to publish."""
        import audit_report as ar

        with self.env(MONITORED_PROJECT_IDS="p-empty"):
            manifest = cf.collect_fleet(run=lambda argv, **kw: run_of(0, "[]"))
        entry = next(c for c in manifest["clusters"] if c["name"] == "project/p-empty")
        self.assertEqual(entry["outcome"], "collected")
        self.assertEqual(entry["commands"], [])
        self.assertNotIn("limitations", entry)
        self.assertEqual(
            {e["check"] for e in entry["checks_not_applicable"]},
            set(ar.audit_target_checks(cf.AUDIT_ID, entry["name"])),
        )

    def test_the_manifest_carries_the_top_level_contract(self):
        manifest = cf.collect_fleet("proj-1", run=self.stub())
        self.assertEqual(manifest["version"], cf.MANIFEST_VERSION)
        self.assertEqual(manifest["audit"], "gce-compute-fleet-audit")
        self.assertEqual(manifest["checks_revision"], cf.CHECKS_REVISION)
        for key in ("started_at", "finished_at"):
            self.assertRegex(manifest[key], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

    def test_target_names_are_unique_across_the_manifest(self):
        """`cross_check_manifest` builds `{c["name"]: c}`, so a duplicate name
        makes the earlier target invisible to all six rejection rules."""
        with self.env(MONITORED_PROJECT_IDS="p-a,p-b,p-c"):
            manifest = cf.collect_fleet(run=self.stub())
        names = [c["name"] for c in manifest["clusters"]]
        self.assertEqual(len(names), len(set(names)))


class ManifestComposesWithAuditReportTest(unittest.TestCase):
    """`collect_fleet`'s real output, run through `audit_report`'s own
    `cross_check_manifest` — the same integration shape
    `test_networking_audit.py` uses for the networking stream."""

    AUDIT = "gce-compute-fleet-audit"

    def fleet(self, **overrides) -> dict:
        # One converged MIG and no sole-tenant node groups: the shape of the
        # reference install, where §2.2 runs and finds nothing and §2.4 declares
        # a structural non-applicability. Between them the target exercises both
        # dispositions the roster cross-check has to accept.
        responses = {
            "instances list": run_of(0, one_running_instance()),
            "project-info describe": run_of(0, NO_PROJECT_STARTUP_SCRIPT),
            "get-serial-port-output": run_of(0, "startup-script exit status 1\n"),
            "instance-groups managed list": run_of(0, json.dumps([mig()])),
            "node-groups list-nodes": run_of(0, "[]"),
            "node-groups list": run_of(0, "[]"),
            "disks list": run_of(0, "[]"),
            "snapshots list": run_of(0, "[]"),
        }
        responses.update(overrides)

        def run(argv, **kwargs):
            joined = " ".join(argv)
            for needle, result in responses.items():
                if needle in joined:
                    return result
            raise Unstubbed(f"unstubbed command: {joined}")

        return cf.collect_fleet("proj-1", run=run)

    @staticmethod
    def _scope_entry(entry: dict) -> dict:
        """The SOP's copy rule, applied literally: copy `commands` minus any
        slug this same target declares not-applicable, and carry the
        collector's `checks_not_applicable` and `limitations` through
        untouched."""
        na_slugs = {d["check"] for d in entry.get("checks_not_applicable") or []}
        out = {
            "name": entry["name"],
            "location": entry["location"],
            "project": entry["project"],
            "checks_run": [
                {"check": c["check"], "command": c["command"]}
                for c in entry["commands"]
                if c["check"] not in na_slugs
            ],
            "checks_not_applicable": entry.get("checks_not_applicable") or [],
        }
        if entry.get("limitations"):
            out["limitations"] = entry["limitations"]
        return out

    @staticmethod
    def _finding(candidate: dict, cluster: str) -> dict:
        """A candidate turned into the document finding the SOP asks the model
        to write: the collector's fields verbatim, plus the recommendation and
        remediation only the model authors."""
        return {
            "check": candidate["check"],
            "severity": candidate["severity"],
            "title": "Boot-time configuration never applied on a running instance",
            "cluster": cluster,
            "namespace": candidate["namespace"],
            "object": candidate["object"],
            "impact": candidate["impact"],
            "evidence": {
                "command": "gcloud compute instances list --project proj-1 --format=json",
                "excerpt": candidate["excerpt"],
            },
            "recommendation": {
                "action": "Correct the boot metadata on the instance template that produced this VM.",
                "rationale": (
                    "Resetting the instance would clear the symptom and reproduce it on the next "
                    "boot, because the failing script is baked into the template."
                ),
                "risk": "The instance has to reboot before the corrected script runs.",
            },
            "remediation": {"kind": "manual", "path": "", "note": "Update the instance template."},
        }

    @staticmethod
    def _skipped(manifest: dict, *, but: str = "") -> list[dict]:
        """The SOP's rule for a `gate-failed` target: `scope.skipped`, with its
        `error` as the reason. `but` leaves one out, for a test about it."""
        return [
            {"cluster": e["name"], "reason": e["error"]}
            for e in manifest["clusters"]
            if e["outcome"] == "gate-failed" and e["name"] != but
        ]

    def _document(self, manifest: dict) -> dict:
        collected = [e for e in manifest["clusters"] if e["outcome"] == "collected"]
        entries = [self._scope_entry(e) for e in collected]
        findings = []
        for entry in collected:
            for candidate in entry.get("candidates") or []:
                findings.append(self._finding(candidate, entry["name"]))
        return {
            "audit": self.AUDIT,
            "scope": {"clusters": entries, "skipped": self._skipped(manifest)},
            "findings": findings,
        }

    def test_the_copy_recipe_publishes_on_the_first_attempt(self):
        import audit_report

        manifest = self.fleet()
        data = self._document(manifest)
        audit_report.cross_check_manifest(data, manifest)
        audit_report.validate_findings(data, self.AUDIT)

    def test_the_run_is_not_partial_when_every_readable_check_ran(self):
        """The three unimplemented checks leave the coverage denominator via
        `checks_not_applicable`, so a fleet the collector fully covered does
        not report a gap it can never close."""
        import audit_report

        manifest = self.fleet(**{"get-serial-port-output": run_of(0, "boot ok\n")})
        data = self._document(manifest)
        audit_report.cross_check_manifest(data, manifest)
        # The one gap is the `--project-id` narrowing, which is the run's to
        # report; the project it did read leaves none.
        self.assertEqual(
            [g for g in audit_report.coverage_gaps(data) if cf.UNENUMERATED_PROJECTS_TARGET not in g],
            [],
        )

    def test_the_collectors_evidence_is_adopted_onto_the_model_finding(self):
        """The whole point of the conversion: the excerpt that ships is the
        collector's observed line, not the model's retyped one."""
        import audit_report

        manifest = self.fleet()
        data = self._document(manifest)
        data["findings"][0]["evidence"]["excerpt"] = "the model made this up"
        audit_report.adopt_collector_evidence(data["findings"], manifest)
        self.assertEqual(data["findings"][0]["evidence"]["excerpt"], "startup-script exit status 1")

    def test_claiming_a_check_declared_inapplicable_is_rejected(self):
        """Rule 6.5. The collector declared the slug inapplicable, so a
        document asserting it ran is refused however the command is spelled.

        Aimed at `sole-tenant-headroom` deliberately. The slug has to be one
        that is *on the roster* and declared not-applicable by this fixture,
        which is what Rule 6.5 is about; a slug that left the roster is
        refused a step earlier as simply unknown, and pointing this test at one
        would have it pass without the rule under test ever running.
        """
        import audit_report

        manifest = self.fleet()
        declared = {
            d["check"] for d in manifest["clusters"][0]["checks_not_applicable"]
        }
        self.assertIn("sole-tenant-headroom", declared)

        data = self._document(manifest)
        data["scope"]["clusters"][0]["checks_run"].append(
            {
                "check": "sole-tenant-headroom",
                "command": "gcloud compute sole-tenancy node-groups list --project proj-1 --format=json",
            }
        )
        with self.assertRaises(audit_report.ValidationError) as caught:
            audit_report.cross_check_manifest(data, manifest)
        self.assertIn("sole-tenant-headroom", str(caught.exception))

    def test_a_gate_failed_target_left_out_of_the_document_is_rejected(self):
        """Rule 6.2. A project the collector could not read has to be
        accounted for somewhere, or the run publishes an all-clear over it."""
        import audit_report

        manifest = self.fleet(**{"instances list": run_of(1, "", "PERMISSION_DENIED")})
        data = {
            "audit": self.AUDIT,
            "scope": {"clusters": [], "skipped": self._skipped(manifest, but="project/proj-1")},
            "findings": [],
        }
        with self.assertRaises(audit_report.ValidationError) as caught:
            audit_report.cross_check_manifest(data, manifest)
        self.assertIn("project/proj-1", str(caught.exception))

    def test_a_gate_failed_target_routed_to_skipped_publishes(self):
        import audit_report

        manifest = self.fleet(**{"instances list": run_of(1, "", "PERMISSION_DENIED")})
        data = {
            "audit": self.AUDIT,
            "scope": {"clusters": [], "skipped": self._skipped(manifest)},
            "findings": [],
        }
        self.assertIn("project/proj-1", [s["cluster"] for s in data["scope"]["skipped"]])
        audit_report.cross_check_manifest(data, manifest)  # must not raise

    def test_a_collected_target_dropped_from_the_document_is_rejected(self):
        """Rule 6.1. On 2026-08-29 a collector read four clusters, the document
        named one, and `finish` published a full-fleet all-clear over a quarter
        of the fleet."""
        import audit_report

        manifest = self.fleet()
        data = {
            "audit": self.AUDIT,
            "scope": {"clusters": [], "skipped": self._skipped(manifest)},
            "findings": [],
        }
        with self.assertRaises(audit_report.ValidationError) as caught:
            audit_report.cross_check_manifest(data, manifest)
        self.assertIn("project/proj-1", str(caught.exception))

    def test_a_check_the_collector_never_recorded_is_rejected(self):
        """Rule 6.4."""
        import audit_report

        manifest = self.fleet()
        data = self._document(manifest)
        data["scope"]["clusters"][0]["checks_run"] = [
            {"check": "gce-startup-script-status", "command": "gcloud compute instances list --project proj-1"},
            {"check": "orphaned-snapshots", "command": "gcloud compute snapshots list --project proj-1"},
        ]
        manifest["clusters"][0]["commands"] = []
        with self.assertRaises(audit_report.ValidationError):
            audit_report.cross_check_manifest(data, manifest)

    def test_an_unreadable_target_claiming_checks_without_limitations_is_rejected(self):
        """Rule 6.3. Hand-collection is legal; reporting it as a clean full
        read is not."""
        import audit_report

        manifest = self.fleet(**{"instances list": run_of(1, "", "PERMISSION_DENIED")})
        data = {
            "audit": self.AUDIT,
            "scope": {
                "clusters": [
                    {
                        "name": "project/proj-1",
                        "location": "global",
                        "project": "proj-1",
                        "checks_run": [
                            {
                                "check": "gce-startup-script-status",
                                "command": "gcloud compute instances list --project proj-1 --format=json",
                            }
                        ],
                    }
                ],
                "skipped": self._skipped(manifest, but="project/proj-1"),
            },
            "findings": [],
        }
        with self.assertRaises(audit_report.ValidationError) as caught:
            audit_report.cross_check_manifest(data, manifest)
        self.assertIn("project/proj-1", str(caught.exception))

    def test_every_candidate_slug_is_on_the_streams_roster(self):
        import audit_report

        manifest = self.fleet()
        roster = set(audit_report.audit_checks(self.AUDIT))
        for entry in manifest["clusters"]:
            for candidate in entry.get("candidates") or []:
                self.assertIn(candidate["check"], roster)

    def test_every_declared_slug_is_on_the_streams_roster(self):
        """A declaration naming a slug the roster does not carry subtracts
        nothing and reads as a check somebody removed."""
        import audit_report

        manifest = self.fleet()
        roster = set(audit_report.audit_checks(self.AUDIT))
        for entry in manifest["clusters"]:
            for declared in entry.get("checks_not_applicable") or []:
                self.assertIn(declared["check"], roster)

    def test_the_collector_covers_or_declares_every_roster_slug(self):
        """Nothing on the roster may go unmentioned: a slug that is neither
        recorded nor declared is a coverage gap the ledger cannot explain."""
        import audit_report

        manifest = self.fleet()
        entry = manifest["clusters"][0]
        accounted = {c["check"] for c in entry["commands"]} | {
            d["check"] for d in entry["checks_not_applicable"]
        }
        self.assertEqual(set(audit_report.audit_checks(self.AUDIT)), accounted)


class ChecksRevisionTest(unittest.TestCase):
    """This collector tells the harness which version of itself ran.

    `audit_report.py` compares this run's revision with the previous run's to
    decide whether a finding that stopped appearing was fixed or merely stopped
    being looked for. Publishing nothing gives it no signal, and it falls back
    to claiming a fix.
    """

    def test_the_revision_is_a_digest_of_this_collectors_source(self):
        path = Path(cf.__file__).resolve()
        expected = hashlib.sha256(path.read_bytes()).hexdigest()[:12]
        self.assertEqual(cf.CHECKS_REVISION, expected)

    def test_the_manifest_carries_it(self):
        """Driven, not grepped: the constant is inert unless it reaches the
        manifest `audit_report.py` reads."""
        manifest = cf.collect_fleet("acme", run=lambda argv, **kwargs: cf.Run(argv, 0, "[]", "", 0.01))
        self.assertEqual(manifest["checks_revision"], cf.CHECKS_REVISION)
        self.assertEqual(manifest["version"], cf.MANIFEST_VERSION)


class AdversarialReviewRegressionTest(unittest.TestCase):
    """Four defects the pre-PR adversarial pass confirmed against the first
    draft of this collector. Each is pinned by the failure it actually caused,
    not by the shape of the fix."""

    def fake_run(self, responses: dict) -> cf.RunFn:
        # The §2.2 and §2.4 reads are appended *after* whatever the caller
        # stubbed, so a caller that names one still wins the first-match scan.
        # None of the defects below is about a MIG or a node group, and every
        # test in this class would otherwise have to stub two reads it does not
        # care about.
        stubs = dict(responses)
        stubs.setdefault("instance-groups managed list", run_of(0, "[]"))
        stubs.setdefault("node-groups list-nodes", run_of(0, "[]"))
        stubs.setdefault("node-groups list", run_of(0, "[]"))
        stubs.setdefault("project-info describe", run_of(0, NO_PROJECT_STARTUP_SCRIPT))

        def run(argv, **kwargs):
            joined = " ".join(argv)
            for needle, result in stubs.items():
                if needle in joined:
                    return result
            raise Unstubbed(f"unstubbed command: {joined}")

        return run

    def test_two_same_named_instances_in_different_zones_stay_distinct(self):
        """A GCE instance name is unique per *zone*, so one project can hold
        `web-1` in two zones at once. Unqualified, both candidates derived the
        finding id `...computeinstance-web-1` and `validate_findings` refused
        the entire document — the run published nothing at all, rather than
        publishing one of the two.
        """
        two_zones = (
            '[{"name": "web-1", "status": "RUNNING", '
            f'"zone": "https://x/projects/p1/zones/us-central1-a", {STARTUP_META}}},'
            '{"name": "web-1", "status": "RUNNING", '
            f'"zone": "https://x/projects/p1/zones/us-central1-b", {STARTUP_META}}}]'
        )
        entry = cf.collect_project(
            "p1",
            run=self.fake_run(
                {
                    "instances list": run_of(0, two_zones),
                    "get-serial-port-output": run_of(0, "startup-script exit status 1\n"),
                    "disks list": run_of(0, "[]"),
                    "snapshots list": run_of(0, "[]"),
                }
            ),
        )
        objects = [c["object"] for c in entry["candidates"]]
        self.assertEqual(len(objects), 2)
        self.assertEqual(len(set(objects)), 2, f"collided: {objects}")
        self.assertEqual(
            sorted(objects),
            ["ComputeInstance/us-central1-a/web-1", "ComputeInstance/us-central1-b/web-1"],
        )

    def test_the_target_name_is_classified_as_a_project_not_a_cluster(self):
        """`audit_report.target_kind` recognises a `project/` prefix. The
        hyphenated `project-<id>` this collector used to emit fell through to
        the bare-name branch, so a sweep of one GCP project published the scope
        line "1 cluster"."""
        import audit_report as ar

        entry = cf.collect_project(
            "p1",
            run=self.fake_run(
                {
                    "instances list": run_of(0, one_running_instance()),
                    "get-serial-port-output": run_of(0, "boot ok\n"),
                    "disks list": run_of(0, "[]"),
                    "snapshots list": run_of(0, "[]"),
                }
            ),
        )
        self.assertEqual(ar.target_kind(entry["name"]), "project")

    def test_redaction_spares_a_gke_node_name_and_a_path(self):
        """The catch-all pattern matched any 40-char run of lowercase, digits
        and separators — which is a GKE node name and a deep filesystem path.
        `adopt_collector_evidence` overwrites the model's excerpt with this
        string, so the degraded line was what shipped: the node name is what
        `needs_triage: gke-managed-node` asks the model to judge, and the path
        is the only part of the excerpt saying *what* failed."""
        node = "gke-prod-cluster-default-pool-9f8a7b6c-abcd"
        self.assertGreaterEqual(len(node), 40, "fixture must exceed the pattern's floor")
        line = f"Sep  4 19:00:00 {node} startup-script: /opt/bootstrap/very/long/path/to/installer.sh: line 12: startup-script exit status 1"
        out = cf.redact(line)
        self.assertIn(node, out)
        self.assertIn("/opt/bootstrap/very/long/path/to/installer.sh", out)
        self.assertNotIn(cf.REDACTED, out)

    def test_redaction_still_catches_encoded_material_and_hex_digests(self):
        """The other side of the same fix: narrowing the catch-all must not
        open a hole. Both shapes carry no keyword for the named-secret pattern
        to key on, so the catch-all is the only thing covering them."""
        blob = "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8S9t0U1v2W3x4"
        self.assertNotIn(blob, cf.redact(f"echo {blob} >> /etc/x"))
        digest = "d41d8cd98f00b204e9800998ecf8427e" + "a" * 32
        self.assertNotIn(digest, cf.redact(f"checksum {digest} ok"))

    def test_each_candidate_names_its_own_read_past_the_command_clip(self):
        """`_joined_record` clips the join at MAX_COMMAND_CHARS, so the slug's
        record cannot name every failing instance's read. Each candidate
        carries its own serial read as `command`, which
        `adopt_collector_evidence` prefers."""
        many = ",".join(
            f'{{"name": "vm-{i:03d}", "status": "RUNNING", '
            f'"zone": "https://x/projects/p1/zones/us-central1-a"}}'
            for i in range(60)
        )

        def run(argv, **kwargs):
            joined = " ".join(argv)
            if "instances list" in joined:
                return run_of(0, f"[{many}]")
            if "get-serial-port-output" in joined:
                return run_of(0, "startup-script exit status 1\n")
            return run_of(0, "[]")

        entry = cf.collect_project("p1", run=run)
        startup = [c for c in entry["commands"] if c["check"] == cf.STARTUP_SLUG]
        self.assertIn("more read(s)", startup[0]["command"], "fixture must overflow the budget")
        self.assertEqual(len(entry["candidates"]), 60)
        for candidate in entry["candidates"]:
            instance = candidate["object"].rsplit("/", 1)[1]
            self.assertIn("get-serial-port-output " + instance + " ", candidate["command"])


class MigConvergenceTest(unittest.TestCase):
    """§2.2. Two limbs, and several shapes that deliberately are not limbs."""

    def test_a_converged_group_is_not_a_finding(self):
        self.assertIsNone(cf.check_mig_convergence(mig()))

    def test_creating_and_deleting_at_once_is_the_resize_loop(self):
        hit = cf.check_mig_convergence(mig(creating=2, deleting=1))
        self.assertEqual(hit["object"], "ManagedInstanceGroup/us-central1-a/mig-1")
        self.assertIn("creating=2", hit["excerpt"])
        self.assertIn("deleting=1", hit["excerpt"])

    def test_a_group_only_scaling_up_is_not_a_finding(self):
        """The false positive the check is shaped to avoid. A healthy
        autoscaler under load creates instances and is `isStable: false` the
        whole time; flagging that reports every group on the fleet."""
        self.assertIsNone(cf.check_mig_convergence(mig(creating=4)))

    def test_a_group_only_scaling_down_is_not_a_finding(self):
        self.assertIsNone(cf.check_mig_convergence(mig(deleting=4)))

    def test_a_rolling_update_is_not_a_finding(self):
        """`recreating` is how a MIG rolls a new template through. It is not a
        resize at all, and neither limb reads it."""
        self.assertIsNone(cf.check_mig_convergence(mig(recreating=5)))

    def test_the_gke_exclusion_is_handed_back_to_the_model(self):
        for name in ("gke-prod-default-pool-1234-grp", "gk3-auto-pool-1-abcd-grp"):
            hit = cf.check_mig_convergence(mig(name=name, creating=1, deleting=1))
            self.assertEqual(hit["needs_triage"], cf.TRIAGE_GKE_MIG, name)

    def test_a_non_gke_group_carries_no_triage(self):
        hit = cf.check_mig_convergence(mig(name="batch-workers", creating=1, deleting=1))
        self.assertIsNone(hit["needs_triage"])

    def test_creating_without_retries_alone_is_not_a_finding(self):
        """The field counts instances the group will try once each to create;
        a failed creation lowers `targetSize`, so it says nothing about a
        stall."""
        for name in ("mig-1", "gke-prod-default-pool-1234-grp"):
            self.assertIsNone(cf.check_mig_convergence(mig(name=name, creatingWithoutRetries=3)))

    def test_an_update_in_progress_is_not_a_resize_loop(self):
        """A rolling update with surge creates and deletes at once by design."""
        updating = mig(creating=1, deleting=1)
        updating["status"]["versionTarget"]["isReached"] = False
        self.assertIsNone(cf.check_mig_convergence(updating))

    def test_a_resize_loop_with_no_retry_creations_keeps_its_marker(self):
        """`creatingWithoutRetries` alongside the loop changes nothing."""
        hit = cf.check_mig_convergence(
            mig(name="gke-prod-default-pool-1234-grp", creating=1, deleting=1, creatingWithoutRetries=2)
        )
        self.assertIn("creating=1", hit["excerpt"])
        self.assertEqual(hit["needs_triage"], cf.TRIAGE_GKE_MIG)

    def test_a_regional_group_is_scoped_by_region(self):
        """A MIG list mixes zonal and regional groups, and only one of the two
        keys is present on any given item."""
        regional = mig(name="rmig")
        del regional["zone"]
        regional["region"] = "https://x/projects/proj-1/regions/us-central1"
        regional["currentActions"].update(creating=1, deleting=1)
        hit = cf.check_mig_convergence(regional)
        self.assertEqual(hit["object"], "ManagedInstanceGroup/us-central1/rmig")

    def test_two_same_named_groups_in_different_zones_stay_distinct(self):
        """The collision `check_startup_script` is zone-qualified for: a MIG
        name is unique per scope, and two candidates deriving one finding id
        make `validate_findings` refuse the whole document."""
        a = cf.check_mig_convergence(mig(zone="us-central1-a", creating=1, deleting=1))
        b = cf.check_mig_convergence(mig(zone="us-central1-b", creating=1, deleting=1))
        self.assertNotEqual(a["object"], b["object"])

    def test_a_missing_actions_object_is_not_a_crash(self):
        broken = mig()
        del broken["currentActions"]
        self.assertIsNone(cf.check_mig_convergence(broken))

    def test_a_non_integer_counter_reads_as_zero(self):
        """A changed contract must read as "no churn observed" rather than cost
        the project its whole MIG check."""
        weird = mig()
        weird["currentActions"]["creating"] = "two"
        weird["currentActions"]["deleting"] = 1
        self.assertIsNone(cf.check_mig_convergence(weird))

    def test_an_unnamed_group_is_skipped(self):
        self.assertIsNone(cf.check_mig_convergence(mig(name="")))


class SoleTenantHeadroomTest(unittest.TestCase):
    """§2.4. The `measured` half of the return value is the point: it keeps
    "read it, it is fine" apart from "never read it"."""

    GROUP = {"name": "ng-1", "zone": "https://x/projects/proj-1/zones/us-central1-a"}

    def test_an_idle_group_is_clean_and_measured(self):
        hit, measured = cf.check_sole_tenant_headroom(self.GROUP, [node(), node()])
        self.assertIsNone(hit)
        self.assertTrue(measured)

    def test_a_full_single_node_group_is_flagged(self):
        hit, measured = cf.check_sole_tenant_headroom(
            self.GROUP, [node(cpus=8, used_cpus=8, mem=32768, used_mem=30000)]
        )
        self.assertTrue(measured)
        self.assertEqual(hit["object"], "NodeGroup/us-central1-a/ng-1")
        self.assertEqual(hit["needs_triage"], cf.TRIAGE_MAINTENANCE)
        self.assertIn("100%", hit["excerpt"])

    def test_ninety_percent_with_a_whole_node_spare_is_not_flagged(self):
        """The "without failover host headroom" half of §2.4's conjunction. Ten
        nodes at 90% still survive losing one, so utilisation alone is not the
        condition."""
        nodes = [node(cpus=10, used_cpus=9) for _ in range(10)]
        hit, measured = cf.check_sole_tenant_headroom(self.GROUP, nodes)
        self.assertTrue(measured)
        self.assertIsNone(hit)

    def test_memory_pressure_alone_can_flag(self):
        hit, _ = cf.check_sole_tenant_headroom(
            self.GROUP, [node(cpus=8, used_cpus=1, mem=1000, used_mem=950)]
        )
        # vCPU sits at 12.5%, so only the memory limb can carry the threshold.
        self.assertIsNotNone(hit)

    def test_an_autoscaling_group_is_excluded_and_still_counts_as_measured(self):
        """§2.4's Do-NOT-flag limb. Excluded is not the same as unread — the
        group was measured, so it must not make the check unevaluated."""
        for mode in ("ON", "ONLY_SCALE_OUT"):
            group = dict(self.GROUP, autoscalingPolicy={"mode": mode})
            hit, measured = cf.check_sole_tenant_headroom(group, [node(cpus=8, used_cpus=8)])
            self.assertIsNone(hit, mode)
            self.assertTrue(measured, mode)

    def test_the_two_non_autoscaling_modes_are_still_evaluated(self):
        """`mode` has four values in the Compute v1 discovery document, and
        excluding on "anything but OFF" would drop a MODE_UNSPECIFIED group out
        of the check while still reporting it measured — headroom never read,
        published as headroom found adequate."""
        for mode in ("OFF", "MODE_UNSPECIFIED"):
            group = dict(self.GROUP, autoscalingPolicy={"mode": mode})
            hit, measured = cf.check_sole_tenant_headroom(group, [node(cpus=8, used_cpus=8)])
            self.assertIsNotNone(hit, mode)
            self.assertTrue(measured, mode)

    def test_an_absent_autoscaling_policy_is_evaluated(self):
        hit, measured = cf.check_sole_tenant_headroom(
            self.GROUP, [node(cpus=8, used_cpus=8)]
        )
        self.assertIsNotNone(hit)
        self.assertTrue(measured)

    def test_nodes_without_resource_figures_report_unmeasured(self):
        """The `checks_unevaluated` case, and the only one this check
        produces. Returning `(None, True)` here would publish a clean verdict
        off a read that yielded nothing."""
        hit, measured = cf.check_sole_tenant_headroom(self.GROUP, [{"status": "READY"}])
        self.assertIsNone(hit)
        self.assertFalse(measured)

    def test_a_group_holding_no_nodes_is_measured_and_clean(self):
        """No node means no host to lose: a verdict, not a missing read."""
        self.assertEqual(cf.check_sole_tenant_headroom(self.GROUP, []), (None, True))

    def test_an_autoscaling_group_scaled_to_no_nodes_is_excluded(self):
        group = dict(self.GROUP, autoscalingPolicy={"mode": "ON", "minNodes": 0})
        self.assertEqual(cf.check_sole_tenant_headroom(group, []), (None, True))

    def test_a_zero_capacity_node_does_not_skew_the_ratio(self):
        """Counting a node that reports no capacity would shrink the
        denominator and manufacture a utilisation figure from a bad record."""
        # Counted, the empty node would add its 32768 MB to the memory
        # denominator and dilute the 95% the real node carries to under 3%.
        nodes = [node(cpus=0, used_cpus=0), node(cpus=10, used_cpus=5, mem=1000, used_mem=950)]
        hit, measured = cf.check_sole_tenant_headroom(self.GROUP, nodes)
        self.assertTrue(measured)
        self.assertIsNotNone(hit)

    def test_a_malformed_node_is_skipped_not_fatal(self):
        nodes = [None, "junk", node(cpus=8, used_cpus=8)]
        hit, measured = cf.check_sole_tenant_headroom(self.GROUP, nodes)
        self.assertTrue(measured)
        self.assertIsNotNone(hit)


class AutoscalingNodeGroupReadTest(unittest.TestCase):
    def test_an_autoscaling_group_is_not_read_and_does_not_make_the_run_partial(self):
        seen = []
        groups = json.dumps([{
            "name": "ng-auto", "zone": "https://x/projects/proj-1/zones/us-central1-a",
            "autoscalingPolicy": {"mode": "ON"},
        }])

        def run(argv, **kwargs):
            joined = " ".join(argv)
            seen.append(joined)
            if "instances list" in joined:
                return run_of(0, one_running_instance())
            if "get-serial-port-output" in joined:
                return run_of(0, "boot ok\n")
            if "project-info describe" in joined:
                return run_of(0, NO_PROJECT_STARTUP_SCRIPT)
            if "list-nodes" in joined:
                return run_of(1, "", "PERMISSION_DENIED compute.nodeGroups.listNodes")
            if "node-groups list" in joined:
                return run_of(0, groups)
            return run_of(0, "[]")

        entry = cf.collect_project("proj-1", run=run)
        self.assertFalse([c for c in seen if "list-nodes" in c])
        self.assertNotIn(cf.SOLE_TENANT_SLUG, unevaluated(entry))
        self.assertNotIn("sole-tenant-headroom", entry.get("limitations", ""))


class SlugReadsTest(unittest.TestCase):
    def test_the_digest_is_the_concatenations_and_no_body_is_kept(self):
        reads = cf.SlugReads()
        bodies = ["a" * 5000, "b" * 7000]
        for i, body in enumerate(bodies):
            reads.add(f"cmd-{i}", cf.Run(["gcloud"], 0, body, "", 0.5))
        self.assertEqual(reads.hexdigest(), hashlib.sha256("".join(bodies).encode()).hexdigest())
        self.assertEqual(reads.commands, ["cmd-0", "cmd-1"])
        self.assertNotIn("a" * 5000, repr(vars(reads)))


class ExcludedNodeGroupCountTest(unittest.TestCase):
    def test_an_excluded_group_is_not_counted_as_measured(self):
        groups = json.dumps([
            {"name": "ng-auto", "zone": "https://x/projects/proj-1/zones/us-central1-a", "autoscalingPolicy": {"mode": "ON"}},
            {"name": "ng-fixed", "zone": "https://x/projects/proj-1/zones/us-central1-a"},
            {"name": "ng-ok", "zone": "https://x/projects/proj-1/zones/us-central1-a"},
        ])

        def run(argv, **kwargs):
            joined = " ".join(argv)
            if "instances list" in joined:
                return run_of(0, one_running_instance())
            if "get-serial-port-output" in joined:
                return run_of(0, "boot ok\n")
            if "project-info describe" in joined:
                return run_of(0, NO_PROJECT_STARTUP_SCRIPT)
            if "list-nodes ng-fixed" in joined:
                return run_of(1, "", "PERMISSION_DENIED")
            if "list-nodes ng-ok" in joined:
                return run_of(0, json.dumps([node()]))
            if "node-groups list" in joined:
                return run_of(0, groups)
            return run_of(0, "[]")

        entry = cf.collect_project("proj-1", run=run)
        self.assertIn("measured 1 of 2 node group(s)", entry["limitations"])


class NothingCollectedTest(unittest.TestCase):
    def test_a_run_that_read_no_target_carries_a_top_level_error(self):
        with patch.dict(os.environ, {**NO_PROJECT_ENV, "MONITORED_PROJECT_IDS": "p-a"}):
            manifest = cf.collect_fleet(run=lambda argv, **kw: run_of(1, "", "ERROR: instances list timed out"))
        self.assertIn("no project could be read", manifest["error"])
        self.assertFalse([c for c in manifest["clusters"] if c.get("outcome") == "collected"])

    def test_a_run_with_a_collected_target_has_no_error(self):
        def run(argv, **kwargs):
            if "instances list" in " ".join(argv):
                return run_of(0, one_running_instance())
            return run_of(0, "[]")

        with patch.dict(os.environ, {**NO_PROJECT_ENV, "MONITORED_PROJECT_IDS": "p-a"}):
            manifest = cf.collect_fleet(run=run)
        self.assertNotIn("error", manifest)

    def test_an_api_off_only_project_is_named_as_the_cause(self):
        refusal = (
            "ERROR: Compute Engine API has not been used in project p-off before or it "
            "is disabled. SERVICE_DISABLED"
        )
        with patch.dict(os.environ, {**NO_PROJECT_ENV, "MONITORED_PROJECT_IDS": "p-off"}):
            manifest = cf.collect_fleet(run=lambda argv, **kw: run_of(1, "", refusal))
        self.assertIn("Compute Engine API is off in p-off", manifest["error"])

    def test_main_exits_non_zero_when_nothing_was_collected(self):
        with patch.object(cf, "collect_fleet", return_value={"clusters": [], "error": "no project could be read"}):
            with patch("sys.stdout", new=io.StringIO()):
                self.assertEqual(cf.main([]), 1)


class NewChecksInCollectProjectTest(unittest.TestCase):
    """The two checks wired through `collect_project`, where the manifest
    dispositions are actually decided."""

    def run_with(self, **overrides):
        base = {
            "instances list": run_of(0, one_running_instance()),
            "project-info describe": run_of(0, NO_PROJECT_STARTUP_SCRIPT),
            "get-serial-port-output": run_of(0, "boot ok\n"),
            "instance-groups managed list": run_of(0, json.dumps([mig()])),
            "node-groups list-nodes": run_of(0, "[]"),
            "node-groups list": run_of(0, "[]"),
            # One snapshot whose source disk is still there, so §2.5 runs and
            # finds nothing. An empty list would take the check into
            # `checks_not_applicable` instead, and these tests are about
            # whether a failure elsewhere stops it running at all.
            "disks list": run_of(0, '[{"name": "disk-1", "zone": "https://x/zones/z1"}]'),
            "snapshots list": run_of(
                0,
                '[{"name": "snap-1", "sourceDisk": "https://x/disks/disk-1", '
                f'"creationTimestamp": "{OLD_STAMP}"}}]',
            ),
        }
        base.update(overrides)

        def run(argv, **kwargs):
            joined = " ".join(argv)
            # Longest needle first, so `node-groups list-nodes ng-1` wins over
            # `node-groups list-nodes`, which wins over `node-groups list`.
            for needle, result in sorted(base.items(), key=lambda item: -len(item[0])):
                if needle in joined:
                    return result
            raise Unstubbed(f"unstubbed command: {joined}")

        return cf.collect_project("proj-1", run=run)

    def test_a_project_with_no_migs_declares_a_structural_na(self):
        entry = self.run_with(**{"instance-groups managed list": run_of(0, "[]")})
        declared = {d["check"]: d["reason"] for d in entry["checks_not_applicable"]}
        self.assertIn(cf.MIG_SLUG, declared)
        self.assertNotIn(cf.MIG_SLUG, unevaluated(entry))

    def test_a_project_with_migs_records_a_command_not_a_declaration(self):
        entry = self.run_with()
        self.assertIn(cf.MIG_SLUG, {c["check"] for c in entry["commands"]})
        self.assertNotIn(cf.MIG_SLUG, {d["check"] for d in entry["checks_not_applicable"]})

    def test_a_project_with_no_snapshots_declares_a_structural_na(self):
        """The reference install holds zero snapshots, so §2.5 was publishing a
        clean pass over an empty population on every run."""
        entry = self.run_with(**{"snapshots list": run_of(0, "[]")})
        declared = {d["check"]: d["reason"] for d in entry["checks_not_applicable"]}
        self.assertIn(cf.SNAPSHOT_SLUG, declared)
        self.assertNotIn(cf.SNAPSHOT_SLUG, unevaluated(entry))
        self.assertNotIn(cf.SNAPSHOT_SLUG, {c["check"] for c in entry["candidates"]})

    def test_a_project_with_snapshots_records_a_command_not_a_declaration(self):
        entry = self.run_with()
        self.assertIn(cf.SNAPSHOT_SLUG, {c["check"] for c in entry["commands"]})
        self.assertNotIn(cf.SNAPSHOT_SLUG, {d["check"] for d in entry["checks_not_applicable"]})

    def test_a_churning_mig_becomes_a_candidate(self):
        entry = self.run_with(
            **{
                "instance-groups managed list": run_of(
                    0, json.dumps([mig(creating=1, deleting=1)])
                )
            }
        )
        hits = [c for c in entry["candidates"] if c["check"] == cf.MIG_SLUG]
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["severity"], "major")

    def test_node_groups_that_exist_but_cannot_be_measured_are_unevaluated(self):
        entry = self.run_with(
            **{
                "node-groups list": run_of(
                    0, json.dumps([{"name": "ng-1", "zone": "https://x/zones/z1"}])
                ),
                "node-groups list-nodes": run_of(0, json.dumps([{"status": "READY"}])),
            }
        )
        self.assertIn("1 node group(s)", unevaluated(entry)[cf.SOLE_TENANT_SLUG])
        self.assertNotIn(cf.SOLE_TENANT_SLUG, {d["check"] for d in entry["checks_not_applicable"]})
        self.assertNotIn(cf.SOLE_TENANT_SLUG, {c["check"] for c in entry["commands"]})
        # `finish` refuses a target listing an unevaluated check with no
        # `limitations`, so the collector writes the sentence the SOP copies.
        self.assertIn("sole-tenant-headroom could not be evaluated", entry["limitations"])

    def test_a_measured_node_group_is_not_declared_at_all(self):
        entry = self.run_with(
            **{
                "node-groups list": run_of(
                    0, json.dumps([{"name": "ng-1", "zone": "https://x/zones/z1"}])
                ),
                "node-groups list-nodes": run_of(0, json.dumps([node(cpus=8, used_cpus=1)])),
            }
        )
        self.assertNotIn(
            cf.SOLE_TENANT_SLUG, {d["check"] for d in entry["checks_not_applicable"]}
        )
        self.assertIn(cf.SOLE_TENANT_SLUG, {c["check"] for c in entry["commands"]})

    def test_one_unreadable_node_group_does_not_cost_the_project(self):
        """A `list-nodes` that fails is not a project-level gate failure, the
        way one unreadable serial console is not: the snapshot check still has
        to run."""
        entry = self.run_with(
            **{
                "node-groups list": run_of(
                    0, json.dumps([{"name": "ng-1", "zone": "https://x/zones/z1"}])
                ),
                "node-groups list-nodes": run_of(1, "", "PERMISSION_DENIED"),
            }
        )
        self.assertEqual(entry["outcome"], "collected")
        self.assertIn(cf.SNAPSHOT_SLUG, {c["check"] for c in entry["commands"]})

    def test_some_unmeasured_node_groups_are_named_in_limitations(self):
        """One group measured, one whose `list-nodes` failed: the check keeps
        its verdict for the first and names the second, rather than reading
        as run and clean on the project."""
        groups = [
            {"name": "ng-1", "zone": "https://x/zones/z1"},
            {"name": "ng-2", "zone": "https://x/zones/z1"},
        ]
        entry = self.run_with(
            **{
                "node-groups list-nodes ng-2": run_of(1, "", "PERMISSION_DENIED"),
                "node-groups list-nodes ng-1": run_of(0, json.dumps([node(cpus=8, used_cpus=1)])),
                "node-groups list": run_of(0, json.dumps(groups)),
            }
        )
        self.assertNotIn(cf.SOLE_TENANT_SLUG, unevaluated(entry))
        self.assertIn("measured 1 of 2 node group(s)", entry["limitations"])
        self.assertIn("ng-2 (z1)", entry["limitations"])
        record = next(c for c in entry["commands"] if c["check"] == cf.SOLE_TENANT_SLUG)
        self.assertNotIn("list-nodes ng-2", record["command"])

    def test_a_failed_project_info_read_is_not_recorded_as_run(self):
        """A failed read published under `rc: 0` claims a command succeeded."""
        entry = self.run_with(**{"project-info describe": run_of(1, "", "PERMISSION_DENIED")})
        record = next(c for c in entry["commands"] if c["check"] == cf.STARTUP_SLUG)
        self.assertNotIn("project-info describe", record["command"])

    def test_a_failed_mig_list_gate_fails_the_target(self):
        """Unlike `list-nodes`, the group enumeration is gated: reading zero
        groups off a failed call would report a project with no MIGs."""
        entry = self.run_with(
            **{"instance-groups managed list": run_of(1, "", "PERMISSION_DENIED")}
        )
        self.assertEqual(entry["outcome"], "gate-failed")
        self.assertIn(cf.MIG_SLUG, entry["error"])


# The environment `get_target_projects` reads, cleared so a test names only
# what it sets.
NO_PROJECT_ENV = {cf.MONITORED_PROJECTS_ENV: "", "GCP_PROJECT_ID": "", "GKE_PROJECT_ID": "", "PROJECT_ID": ""}


def discovery_run(*, listed: str = "", config: str = "", described: dict | None = None,
                  list_rc: int = 0, list_stderr: str = "", describe_rc: int = 0,
                  describe_stderr: str = "") -> cf.RunFn:
    """A `run` answering the discovery calls: `projects list`, `config
    get-value project`, and `projects describe <n>` from `described`."""

    def run(argv, **kwargs):
        if argv[:3] == list(cf.PROJECT_DESCRIBE_CMD):
            if describe_rc:
                return run_of(describe_rc, "", describe_stderr)
            return run_of(0, f"{(described or {}).get(argv[3], '')}\n")
        if list(argv) == list(cf.PROJECTS_LIST_CMD):
            return run_of(list_rc, listed, list_stderr)
        if list(argv) == list(cf.CONFIG_PROJECT_CMD):
            return run_of(0, config)
        # One RUNNING instance, so a discovered project is not left out as
        # holding nothing to audit.
        if "instances list" in " ".join(argv):
            return run_of(0, one_running_instance())
        return run_of(0, "[]")

    return run


class ProjectResolutionTest(unittest.TestCase):
    """Discovery reports whatever narrows or loses part of the fleet, so the
    manifest carries a `project/UNENUMERATED_PROJECTS` row rather than reading
    as the whole fleet."""

    def resolve(self, env: dict, run: cf.RunFn, cli: str | None = None) -> tuple[list[str], list[str]]:
        notes: list[str] = []
        with patch.dict(os.environ, {**NO_PROJECT_ENV, **env}):
            return cf.get_target_projects(cli, notes, run=run), notes

    def test_a_cli_project_wins_and_says_it_narrowed_the_run(self):
        projects, notes = self.resolve({cf.MONITORED_PROJECTS_ENV: "x,y"}, discovery_run(), "cli-proj")
        self.assertEqual(projects, ["cli-proj"])
        self.assertEqual(len(notes), 1)
        self.assertIn("--project-id", notes[0])

    def test_monitored_projects_merge_with_the_env_and_skip_discovery(self):
        def run(argv, **kwargs):
            raise AssertionError(f"discovery ran: {argv}")

        projects, notes = self.resolve(
            {cf.MONITORED_PROJECTS_ENV: "m1, m2 m3", "GCP_PROJECT_ID": "g1"}, run
        )
        self.assertEqual(projects, ["g1", "m1", "m2", "m3"])
        self.assertEqual(len(notes), 1)
        self.assertIn(cf.MONITORED_PROJECTS_ENV, notes[0])

    def test_a_blank_monitored_value_runs_discovery(self):
        projects, notes = self.resolve(
            {cf.MONITORED_PROJECTS_ENV: " , ", "GCP_PROJECT_ID": "p-host"},
            discovery_run(listed="p-host\np-extra\n"),
        )
        self.assertEqual(projects, ["p-extra", "p-host"])
        self.assertEqual(notes, [])

    def test_the_configured_project_is_unioned_with_the_listing(self):
        projects, notes = self.resolve(
            {"GCP_PROJECT_ID": "p-env"},
            discovery_run(listed="p-env\np-config\np-extra\n", config="p-config\n"),
        )
        self.assertEqual(projects, ["p-config", "p-env", "p-extra"])
        self.assertEqual(notes, [])

    def test_a_listing_that_omits_the_configured_project_is_filtered(self):
        projects, notes = self.resolve({"GCP_PROJECT_ID": "p-host"}, discovery_run(listed="p-extra\n"))
        self.assertEqual(projects, ["p-extra", "p-host"])
        self.assertEqual(len(notes), 1)
        self.assertIn("did not name p-host", notes[0])

    def test_a_failed_listing_falls_back_and_says_so(self):
        projects, notes = self.resolve(
            {"GCP_PROJECT_ID": "p-host"},
            discovery_run(list_rc=1, list_stderr="ERROR: PERMISSION_DENIED resourcemanager.projects.list"),
        )
        self.assertEqual(projects, ["p-host"])
        self.assertEqual(len(notes), 1)
        self.assertIn("PERMISSION_DENIED", notes[0])

    def test_a_numeric_project_is_resolved_before_the_listing_is_compared(self):
        projects, notes = self.resolve(
            {"GCP_PROJECT_ID": "123456789012"},
            discovery_run(listed="p-host\np-extra\n", described={"123456789012": "p-host"}),
        )
        self.assertEqual(projects, ["p-extra", "p-host"])
        self.assertEqual(notes, [])

    def test_an_unresolvable_numeric_project_is_noted_and_not_audited_twice(self):
        projects, notes = self.resolve(
            {"GCP_PROJECT_ID": "123456789012"},
            discovery_run(
                listed="p-host\np-extra\n", config="p-host\n", describe_rc=1,
                describe_stderr="PERMISSION_DENIED: resourcemanager.projects.get denied",
            ),
        )
        self.assertEqual(projects, ["p-extra", "p-host"])
        self.assertEqual(len(notes), 1)
        self.assertIn("gcloud projects describe 123456789012", notes[0])
        self.assertNotIn("listing is filtered", notes[0])


class UnenumeratedTargetTest(unittest.TestCase):
    """Each note reaches the manifest as one `gate-failed` row `finish` makes
    the document account for."""

    def manifest(self, env: dict, run: cf.RunFn, cli: str | None = None) -> dict:
        with patch.dict(os.environ, {**NO_PROJECT_ENV, **env}):
            return cf.collect_fleet(cli, run=run)

    def row(self, manifest: dict) -> dict | None:
        rows = [c for c in manifest["clusters"] if c["name"] == cf.UNENUMERATED_PROJECTS_TARGET]
        self.assertLessEqual(len(rows), 1)
        return rows[0] if rows else None

    def test_a_failed_listing_puts_the_row_in_the_manifest(self):
        manifest = self.manifest(
            {"GCP_PROJECT_ID": "p-host"},
            discovery_run(list_rc=1, list_stderr="ERROR: PERMISSION_DENIED resourcemanager.projects.list"),
        )
        row = self.row(manifest)
        self.assertIsNotNone(row)
        self.assertEqual(row["outcome"], "gate-failed")
        self.assertIn("PERMISSION_DENIED", row["error"])
        self.assertIn(cf.UNENUMERATED_TAIL, row["error"])

    def test_a_long_listing_refusal_keeps_the_tail(self):
        manifest = self.manifest(
            {"GCP_PROJECT_ID": "p-host"},
            discovery_run(list_rc=1, list_stderr="ERROR: (gcloud.projects.list) " + "x" * 400),
        )
        row = self.row(manifest)
        self.assertTrue(row["error"].endswith(cf.UNENUMERATED_TAIL))
        self.assertLessEqual(len(row["error"]), cf.ERROR_CLIP_CHARS)

    def test_a_narrowed_run_puts_the_row_in_the_manifest(self):
        row = self.row(self.manifest({}, discovery_run(), "cli-proj"))
        self.assertIsNotNone(row)
        self.assertIn("scope narrowed to cli-proj", row["error"])

    def test_a_complete_discovery_has_no_row(self):
        manifest = self.manifest({"GCP_PROJECT_ID": "p-host"}, discovery_run(listed="p-host\n"))
        self.assertIsNone(self.row(manifest))
        self.assertEqual([c["name"] for c in manifest["clusters"]], ["project/p-host"])

    def test_two_notes_share_one_row(self):
        """`finish` keys the manifest by name, so a second row would hide the first."""
        manifest = self.manifest(
            {"GCP_PROJECT_ID": "123456789012"},
            discovery_run(listed="p-extra\n", config="p-host\n", describe_rc=1, describe_stderr="denied"),
        )
        row = self.row(manifest)
        self.assertIn("gcloud projects describe 123456789012", row["error"])


class ApiDisabledTest(unittest.TestCase):
    """A project whose own Compute Engine API is off holds nothing to audit;
    a refusal naming another project is a failed read."""

    REFUSAL_BY_NUMBER = (
        "ERROR: (gcloud.compute.instances.list) SERVICE_DISABLED: Compute Engine API "
        "has not been used in project 111111111111 before or it is disabled."
    )

    def refusing(self, stderr: str, *, own_number: str = "", describe_rc: int = 0) -> cf.RunFn:
        def run(argv, **kwargs):
            if argv[:3] == list(cf.PROJECT_DESCRIBE_CMD):
                if describe_rc:
                    return run_of(describe_rc, "", "PERMISSION_DENIED: resourcemanager.projects.get")
                return run_of(0, f"{own_number}\n")
            return run_of(1, "", stderr)

        return run

    def test_a_refusal_naming_this_project_contributes_no_target(self):
        stderr = (
            "ERROR: (gcloud.compute.instances.list) SERVICE_DISABLED: Compute Engine API "
            "has not been used in project no-compute-proj before or it is disabled."
        )
        self.assertIsNone(cf.collect_project("no-compute-proj", run=self.refusing(stderr)))
        manifest = cf.collect_fleet("no-compute-proj", run=self.refusing(stderr))
        self.assertNotIn("project/no-compute-proj", [c["name"] for c in manifest["clusters"]])

    def test_a_refusal_by_this_projects_number_contributes_no_target(self):
        self.assertIsNone(
            cf.collect_project("real-proj", run=self.refusing(self.REFUSAL_BY_NUMBER, own_number="111111111111"))
        )

    def test_a_quota_projects_refusal_is_a_failed_read(self):
        entry = cf.collect_project("real-proj", run=self.refusing(self.REFUSAL_BY_NUMBER, own_number="222222222222"))
        self.assertEqual(entry["outcome"], "gate-failed")
        self.assertIn(
            "the API is off in a project other than 'real-proj', such as a quota project", entry["error"]
        )

    def test_a_refusal_whose_number_cannot_be_compared_is_a_failed_read(self):
        entry = cf.collect_project("real-proj", run=self.refusing(self.REFUSAL_BY_NUMBER, describe_rc=1))
        self.assertEqual(entry["outcome"], "gate-failed")
        self.assertIn("`gcloud projects describe real-proj` failed (rc=1)", entry["error"])

    def test_a_refusal_naming_no_project_is_a_failed_read(self):
        entry = cf.collect_project("real-proj", run=self.refusing("ERROR: SERVICE_DISABLED"))
        self.assertEqual(entry["outcome"], "gate-failed")
        self.assertIn("the refusal does not name 'real-proj'", entry["error"])


if __name__ == "__main__":
    unittest.main()
