#!/usr/bin/env python3
"""Tests for patch_readiness.py, the security-patch-orchestrator collector."""

import io
import json
import os
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(__file__))
import audit_report  # noqa: E402
import patch_readiness as pr  # noqa: E402


def run_of(rc: int, stdout: str = "", stderr: str = "") -> pr.Run:
    return pr.Run(["gcloud"], rc, stdout, stderr, 0.01)


def cluster(name="prod-usc1", location="us-central1", master="1.30.5-gke.100", channel="REGULAR", autopilot=False, node_pools=None, **overrides):
    doc = {
        "name": name,
        "location": location,
        "status": "RUNNING",
        "currentMasterVersion": master,
        "releaseChannel": {"channel": channel} if channel else {},
        "autopilot": {"enabled": autopilot},
        "nodePools": node_pools if node_pools is not None else [pool("default-pool", master)],
        "maintenancePolicy": {"window": {"recurringWindow": {}}},
        "notificationConfig": {"pubsub": {"enabled": True}},
    }
    doc.update(overrides)
    return doc


def pool(name="default-pool", version="1.30.5-gke.100", status="RUNNING", auto_upgrade=True, auto_repair=True, image_type="COS_CONTAINERD", **overrides):
    doc = {
        "name": name,
        "version": version,
        "status": status,
        "management": {"autoUpgrade": auto_upgrade, "autoRepair": auto_repair},
        "config": {"imageType": image_type},
    }
    doc.update(overrides)
    return doc


def server_config(channel="REGULAR", default="1.30.5-gke.100", valid_versions=None, valid_image_types=None):
    return {
        "channels": [{"channel": channel, "defaultVersion": default, "validVersions": valid_versions or [default]}],
        "validMasterVersions": valid_versions or [default],
        "validImageTypes": valid_image_types or ["COS_CONTAINERD", "UBUNTU_CONTAINERD", "WINDOWS_LTSC_CONTAINERD"],
    }


BASELINE = pr.normalize_server_config(server_config())
NOW = datetime(2026, 1, 15, tzinfo=timezone.utc)


def short(entry: dict) -> str:
    """A cluster entry's bare name; the manifest qualifies it as
    `<project>/<location>/<name>`. A `project/<id>` row is returned whole."""
    name = entry["name"]
    return name if name.startswith(pr.PROJECT_TARGET_PREFIX) else name.rsplit(pr.QUALIFIED_TARGET_SEPARATOR, 1)[-1]


class VersionArithmeticTest(unittest.TestCase):
    def test_parses_with_build(self):
        self.assertEqual(pr.parse_version("1.30.5-gke.1355000"), (1, 30, 5, 1355000))

    def test_parses_without_build(self):
        self.assertEqual(pr.parse_version("1.30.5"), (1, 30, 5, 0))

    def test_unparseable_is_none(self):
        self.assertIsNone(pr.parse_version("not-a-version"))
        self.assertIsNone(pr.parse_version(""))

    def test_never_string_compares(self):
        # 1.30.9 < 1.30.10 numerically, the opposite of a string compare.
        self.assertLess(pr.parse_version("1.30.9"), pr.parse_version("1.30.10"))

    def test_minor_of(self):
        self.assertEqual(pr.minor_of("1.30.5-gke.1"), (1, 30))


class MasterBehindTest(unittest.TestCase):
    def test_no_baseline_is_not_a_finding(self):
        self.assertIsNone(pr.check_master_behind(cluster(), None))

    def test_the_arm_impact_is_published_over_the_models(self):
        """(a) says nothing patches the control plane and (b)/(c) say it is
        behind while still patched: which arm fired is the collector's fact, so
        `master-behind` candidates carry `impact_authoritative` and
        `audit_report.adopt_arm_impact` publishes their impact."""
        unsupported = pr._emit("master-behind", pr.check_master_behind(cluster(master="1.28.0-gke.1"), BASELINE))
        self.assertTrue(unsupported["impact_authoritative"])
        self.assertEqual(unsupported["impact"], pr.UNSUPPORTED_MASTER_IMPACT)
        lagging, default = "1.30.3-gke.100", "1.30.5-gke.100"
        baseline = pr.normalize_server_config(server_config(default=default, valid_versions=[lagging, default]))
        behind = pr._emit("master-behind", pr.check_master_behind(cluster(master=lagging), baseline))
        self.assertTrue(behind["impact_authoritative"])
        self.assertEqual(behind["impact"], pr.BEHIND_MASTER_IMPACT.format(default=default))
        self.assertFalse(pr._emit("no-channel", {"object": "Cluster/c", "excerpt": "x"})["impact_authoritative"])

    def test_absent_from_valid_versions_is_critical(self):
        c = cluster(master="1.28.0-gke.1")
        hit = pr.check_master_behind(c, BASELINE)
        self.assertEqual(hit["severity"], "critical")

    def test_off_the_channel_roster_but_still_offered_is_not_critical(self):
        """A staggered rollout is not an end of support.

        `validVersions` is per channel and moves the moment that channel
        promotes a new build, so a cluster the wave has not reached yet is
        absent from its own channel's roster while GKE goes on patching it.
        `critical` used to fire on exactly that, ahead of the branches that
        grade it, and its impact told the operator the cluster "receives no
        further patches".

        Both arms below are the live 2026-09-06 fleet. Six clusters ran
        `1.35.7-gke.1027000`, which us-east4 still lists in
        `validMasterVersions` while REGULAR had moved to `1150000`;
        `drift-peer-std-9` ran `1.36.3-gke.1640000` on RAPID, which RAPID had
        left behind and REGULAR and EXTENDED still listed. GKE auto-upgraded
        five of the seven control planes unattended in the same 48 hours.
        """
        current, default = "1.35.7-gke.1027000", "1.35.7-gke.1150000"
        arms = {
            "in validMasterVersions": {
                "channels": [{"channel": "REGULAR", "defaultVersion": default, "validVersions": [default]}],
                "validMasterVersions": [current, default],
            },
            "in another channel's validVersions": {
                "channels": [
                    {"channel": "RAPID", "defaultVersion": default, "validVersions": [default]},
                    {"channel": "EXTENDED", "defaultVersion": current, "validVersions": [current]},
                ],
                "validMasterVersions": [],
            },
        }
        for label, raw in arms.items():
            with self.subTest(offered=label):
                channel = "REGULAR" if label.startswith("in valid") else "RAPID"
                hit = pr.check_master_behind(
                    cluster(channel=channel, master=current), pr.normalize_server_config(raw)
                )
                self.assertEqual(hit["severity"], "minor")
                # Not silence either: the reader still needs to know it is
                # behind, and that its own channel has moved past it.
                self.assertIn(default, hit["excerpt"])
                self.assertIn("no longer on that channel's roster", hit["excerpt"])

    def test_a_baseline_without_valid_master_versions_does_not_judge_off_roster(self):
        """No `validMasterVersions` is a field the response did not carry, not
        a location that offers nothing. A cluster off its channel's roster is
        neither published as out of support nor recorded as checked; one on
        the roster is judged as before."""
        current, default = "1.35.7-gke.1027000", "1.35.7-gke.1150000"
        for label, raw in {
            "empty": {
                "channels": [{"channel": "REGULAR", "defaultVersion": default, "validVersions": [default]}],
                "validMasterVersions": [],
            },
            "absent": {
                "channels": [{"channel": "REGULAR", "defaultVersion": default, "validVersions": [default]}],
            },
        }.items():
            with self.subTest(validMasterVersions=label):
                baseline = pr.normalize_server_config(raw)
                for master in (current, "1.36.1-gke.1", "1.28.1-gke.1"):
                    off = cluster(channel="REGULAR", master=master)
                    self.assertIsNone(pr.check_master_behind(off, baseline))
                    self.assertFalse(pr.master_behind_judged(off, baseline))
                on = cluster(channel="REGULAR", master=default)
                self.assertTrue(pr.master_behind_judged(on, baseline))

    def test_a_version_no_route_offers_is_still_critical(self):
        """The fix must not cost the check its real case. Same shape as the
        test above, with the version absent from every roster the location
        publishes — nothing will upgrade this control plane, which is what
        `critical` claims."""
        raw = {
            "channels": [
                {"channel": "REGULAR", "defaultVersion": "1.35.7-gke.1150000", "validVersions": ["1.35.7-gke.1150000"]},
                {"channel": "EXTENDED", "defaultVersion": "1.34.0-gke.1", "validVersions": ["1.34.0-gke.1"]},
            ],
            "validMasterVersions": ["1.35.7-gke.1150000", "1.34.0-gke.1"],
        }
        hit = pr.check_master_behind(
            cluster(master="1.28.0-gke.1"), pr.normalize_server_config(raw)
        )
        self.assertEqual(hit["severity"], "critical")

    def test_a_static_cluster_on_a_version_only_a_channel_lists_is_critical(self):
        """Another channel's roster is a route only for a cluster on a channel.
        A static-version cluster upgrades from `validMasterVersions` alone, so
        a build only EXTENDED still carries leaves it on a version nothing it
        can reach will patch -- the one cluster most likely to be unpatched,
        which used to read as having passed."""
        raw = {
            "channels": [{"channel": "EXTENDED", "defaultVersion": "1.28.1-gke.1", "validVersions": ["1.28.1-gke.1"]}],
            "validMasterVersions": ["1.30.5-gke.100"],
        }
        hit = pr.check_master_behind(
            cluster(channel="", master="1.28.1-gke.1"), pr.normalize_server_config(raw)
        )
        self.assertEqual(hit["severity"], "critical")

    def test_the_roster_clause_is_absent_when_the_channel_still_lists_it(self):
        """The clause is evidence, not decoration: a cluster inside its own
        channel's roster must not carry it, or it says nothing."""
        baseline = pr.normalize_server_config(
            server_config(default="1.30.9-gke.1", valid_versions=["1.30.5-gke.100", "1.30.9-gke.1"])
        )
        hit = pr.check_master_behind(cluster(master="1.30.5-gke.100"), baseline)
        self.assertNotIn("roster", hit["excerpt"])

    def test_a_minor_behind_default_is_major(self):
        baseline = pr.normalize_server_config(server_config(default="1.31.0-gke.1", valid_versions=["1.30.5-gke.100", "1.31.0-gke.1"]))
        hit = pr.check_master_behind(cluster(master="1.30.5-gke.100"), baseline)
        self.assertEqual(hit["severity"], "major")

    def test_two_minors_behind_default_says_two(self):
        baseline = pr.normalize_server_config(server_config(default="1.32.0-gke.1", valid_versions=["1.30.5-gke.100", "1.32.0-gke.1"]))
        hit = pr.check_master_behind(cluster(master="1.30.5-gke.100"), baseline)
        self.assertIn("is 2 minors behind channel default 1.32.0-gke.1", hit["excerpt"])
        self.assertIn("1.32.0-gke.1", hit["impact"])

    def test_same_minor_older_patch_is_minor(self):
        baseline = pr.normalize_server_config(server_config(default="1.30.9-gke.1", valid_versions=["1.30.5-gke.100", "1.30.9-gke.1"]))
        hit = pr.check_master_behind(cluster(master="1.30.5-gke.100"), baseline)
        self.assertEqual(hit["severity"], "minor")

    def test_equal_to_default_is_not_flagged(self):
        self.assertIsNone(pr.check_master_behind(cluster(master="1.30.5-gke.100"), BASELINE))

    def test_newer_than_default_is_not_flagged(self):
        baseline = pr.normalize_server_config(server_config(default="1.30.0-gke.1", valid_versions=["1.30.5-gke.100", "1.30.0-gke.1"]))
        self.assertIsNone(pr.check_master_behind(cluster(master="1.30.5-gke.100"), baseline))

    def test_no_channel_uses_valid_master_versions(self):
        baseline = pr.normalize_server_config(server_config(valid_versions=["1.30.5-gke.100"]))
        self.assertIsNone(pr.check_master_behind(cluster(channel=""), baseline))
        c = cluster(channel="", master="9.9.9-gke.1")
        self.assertEqual(pr.check_master_behind(c, baseline)["severity"], "critical")

    def test_unknown_channel_spelling_is_not_a_crash(self):
        c = cluster(channel="MYSTERY")
        self.assertIsNone(pr.check_master_behind(c, BASELINE))

    def test_an_empty_valid_roster_flags_nobody_rather_than_everybody(self):
        """`current not in valid` is true of every version against `[]`, so a
        baseline that carried no `validVersions` used to report the entire
        fleet `critical` — "absent from validVersions" on clusters running the
        version the channel had just promoted. An empty roster is a field the
        server config did not return, not a fleet where nothing is offered."""
        for label, raw in (
            ("channel", {"channels": [{"channel": "REGULAR", "defaultVersion": "1.30.5-gke.100", "validVersions": []}], "validMasterVersions": []}),
            ("static", {"channels": [], "validMasterVersions": []}),
        ):
            with self.subTest(baseline=label):
                baseline = pr.normalize_server_config(raw)
                channel = "REGULAR" if label == "channel" else ""
                self.assertIsNone(pr.check_master_behind(cluster(channel=channel), baseline))

    def test_a_roster_the_baseline_lacks_is_not_recorded_as_run(self):
        """A check that judged nothing is a gap, not a clean result: listed in
        `commands` with no candidate, it would read as clean."""
        for label, c in (("missing channel", cluster(channel="MYSTERY")), ("empty roster", cluster())):
            with self.subTest(label):
                raw = server_config()
                if label == "empty roster":
                    raw["channels"][0]["validVersions"] = []
                    raw["validMasterVersions"] = []
                slugs, _ = pr.collect_one_cluster(c, pr.normalize_server_config(raw), now=NOW)
                self.assertNotIn("master-behind", slugs)
                self.assertIn("stale-image-type", slugs)
        slugs, _ = pr.collect_one_cluster(cluster(), BASELINE, now=NOW)
        self.assertIn("master-behind", slugs)

    def test_a_channel_without_a_default_is_not_recorded_as_run(self):
        """(b) and (c) read the channel default; without one only (a) ran, so
        a clean cluster is a gap and an (a) hit is still recorded."""
        raw = server_config()
        raw["channels"][0].pop("defaultVersion", None)
        baseline = pr.normalize_server_config(raw)
        slugs, _ = pr.collect_one_cluster(cluster(), baseline, now=NOW)
        self.assertNotIn("master-behind", slugs)
        slugs, candidates = pr.collect_one_cluster(cluster(master="9.9.9-gke.1"), baseline, now=NOW)
        self.assertIn("master-behind", slugs)
        self.assertIn("master-behind", [c["check"] for c in candidates])

    def test_an_unspecified_channel_is_read_as_no_channel(self):
        """GKE spells a static-version cluster two ways, and `UNSPECIFIED` is
        truthy: it took the channel branch, missed in `channels`, and returned
        `None`. That exempted the clusters that take no automatic patches at
        all, while the manifest still recorded `master-behind` as run and
        clean. `check_no_channel` already normalised it; this did not."""
        baseline = pr.normalize_server_config(server_config(valid_versions=["1.30.5-gke.100"]))
        c = cluster(channel="UNSPECIFIED", master="9.9.9-gke.1")
        self.assertEqual(pr.check_master_behind(c, baseline)["severity"], "critical")
        self.assertIsNotNone(pr.check_no_channel(c))

    def test_a_reconciling_cluster_is_suppressed(self):
        """§3's universal gate covers 3.1, 3.2 and 3.3; only 3.2 implemented
        it, so a cluster halfway through the upgrade that fixes the drift was
        reported as drifted."""
        c = cluster(master="1.28.0-gke.1", status="RECONCILING")
        self.assertIsNone(pr.check_master_behind(c, BASELINE))
        self.assertEqual(pr.check_master_behind(cluster(master="1.28.0-gke.1"), BASELINE)["severity"], "critical")


class PoolSkewTest(unittest.TestCase):
    def test_autopilot_is_judged_like_any_other_cluster(self):
        """Autopilot node pools carry a real `version`, so a skew there is an
        observation and not a category error. The operator cannot fix it, which
        makes the finding `kind: manual` per the SOP -- it does not make the
        ten-minor gap below untrue."""
        c = cluster(autopilot=True, node_pools=[pool(version="1.20.0-gke.1")])
        hits = pr.check_pool_skew(c)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["severity"], "critical")

    def test_three_minors_behind_is_critical(self):
        c = cluster(master="1.33.0-gke.1", node_pools=[pool(version="1.30.0-gke.1")])
        hits = pr.check_pool_skew(c)
        self.assertEqual(hits[0]["severity"], "critical")

    def test_different_major_is_critical(self):
        c = cluster(master="2.0.0-gke.1", node_pools=[pool(version="1.30.0-gke.1")])
        self.assertEqual(pr.check_pool_skew(c)[0]["severity"], "critical")

    def test_two_minors_behind_is_major(self):
        c = cluster(master="1.32.0-gke.1", node_pools=[pool(version="1.30.0-gke.1")])
        self.assertEqual(pr.check_pool_skew(c)[0]["severity"], "major")

    def test_one_minor_behind_with_autoupgrade_off_is_major(self):
        c = cluster(master="1.31.0-gke.1", node_pools=[pool(version="1.30.0-gke.1", auto_upgrade=False)])
        self.assertEqual(pr.check_pool_skew(c)[0]["severity"], "major")

    def test_one_minor_behind_with_autoupgrade_on_is_minor(self):
        c = cluster(master="1.31.0-gke.1", node_pools=[pool(version="1.30.0-gke.1", auto_upgrade=True)])
        self.assertEqual(pr.check_pool_skew(c)[0]["severity"], "minor")

    def test_one_minor_behind_with_autoupgrade_absent_is_major(self):
        """The API omits a false boolean, so an absent `autoUpgrade` is off."""
        p = pool(version="1.30.0-gke.1")
        p["management"] = {"autoRepair": True}
        c = cluster(master="1.31.0-gke.1", node_pools=[p])
        self.assertEqual(pr.check_pool_skew(c)[0]["severity"], "major")

    def test_one_patch_behind_is_not_flagged(self):
        c = cluster(master="1.30.5-gke.100", node_pools=[pool(version="1.30.4-gke.100")])
        self.assertEqual(pr.check_pool_skew(c), [])

    def test_the_same_patch_on_an_older_build_is_not_flagged(self):
        """A smaller lag than one patch, and the usual shape of a staged node
        rollout: the allowance covers it rather than flagging it `minor`."""
        c = cluster(master="1.30.5-gke.200", node_pools=[pool(version="1.30.5-gke.100")])
        self.assertEqual(pr.check_pool_skew(c), [])

    def test_several_patches_behind_is_minor(self):
        c = cluster(master="1.30.9-gke.100", node_pools=[pool(version="1.30.4-gke.100")])
        self.assertEqual(pr.check_pool_skew(c)[0]["severity"], "minor")

    def test_ahead_of_control_plane_is_major(self):
        c = cluster(master="1.30.0-gke.1", node_pools=[pool(version="1.31.0-gke.1")])
        self.assertEqual(pr.check_pool_skew(c)[0]["severity"], "major")

    def test_reconciling_pool_is_suppressed(self):
        c = cluster(master="1.33.0-gke.1", node_pools=[pool(version="1.30.0-gke.1", status="RECONCILING")])
        self.assertEqual(pr.check_pool_skew(c), [])

    def test_reconciling_cluster_suppresses_every_pool(self):
        c = cluster(master="1.33.0-gke.1", node_pools=[pool(version="1.30.0-gke.1")], status="RECONCILING")
        self.assertEqual(pr.check_pool_skew(c), [])

    def test_same_version_is_never_flagged(self):
        c = cluster(master="1.30.5-gke.100", node_pools=[pool(version="1.30.5-gke.100")])
        self.assertEqual(pr.check_pool_skew(c), [])


class FleetSpreadTest(unittest.TestCase):
    def test_a_two_minor_spread_is_flagged_once_on_the_laggard(self):
        clusters = [cluster(name="old", master="1.28.0-gke.1"), cluster(name="new", master="1.30.0-gke.1")]
        hits = pr.check_fleet_spread(clusters)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["object"], "Cluster/old")

    def test_the_laggard_is_the_oldest_version_on_the_oldest_minor(self):
        """Clusters on one channel share a minor, so the name broke the tie and
        the finding moved to whichever project sorted first."""
        clusters = [
            cluster(name="acme/us-central1/aaa", master="1.28.15-gke.1"),
            cluster(name="zzz/us-east4/zzz", master="1.28.0-gke.1"),
            cluster(name="new", master="1.30.0-gke.1"),
        ]
        self.assertEqual(pr.check_fleet_spread(clusters)[0]["object"], "Cluster/zzz/us-east4/zzz")

    def test_a_one_minor_spread_is_not_flagged(self):
        clusters = [cluster(name="a", master="1.29.0-gke.1"), cluster(name="b", master="1.30.0-gke.1")]
        self.assertEqual(pr.check_fleet_spread(clusters), [])

    def test_a_single_cluster_is_never_a_spread(self):
        self.assertEqual(pr.check_fleet_spread([cluster()]), [])

    def test_a_reconciling_cluster_does_not_widen_the_spread(self):
        """§3's gate has to drop the cluster from the computation, not just
        from the finding: a cluster mid-upgrade is the likeliest outlier, so
        leaving it in reports a two-minor fleet that is one minor wide the
        moment its upgrade lands, and attaches the finding to the cluster
        already being fixed."""
        clusters = [cluster(name="old", master="1.28.0-gke.1", status="RECONCILING"), cluster(name="new", master="1.30.0-gke.1")]
        self.assertEqual(pr.check_fleet_spread(clusters), [])


    def test_a_fleet_on_two_majors_is_flagged_on_the_laggard(self):
        """§2 reads any difference in the first element as unbounded skew,
        so two majors is the widest spread there is. The guard returned [] for
        it, and the excerpt's minor count means nothing across the boundary."""
        clusters = [cluster(name="old", master="1.33.0-gke.1"), cluster(name="new", master="2.0.0-gke.1")]
        hits = pr.check_fleet_spread(clusters)
        self.assertEqual([h["object"] for h in hits], ["Cluster/old"])
        self.assertIn("across major versions", hits[0]["excerpt"])
        self.assertNotIn("minors wide", hits[0]["excerpt"])

class NoChannelTest(unittest.TestCase):
    def test_flags_empty_channel(self):
        self.assertIsNotNone(pr.check_no_channel(cluster(channel="")))

    def test_flags_unspecified(self):
        self.assertIsNotNone(pr.check_no_channel(cluster(channel="UNSPECIFIED")))

    def test_does_not_flag_regular(self):
        self.assertIsNone(pr.check_no_channel(cluster(channel="REGULAR")))


class NoAutoupgradeAutorepairTest(unittest.TestCase):
    def test_autopilot_is_read_rather_than_assumed(self):
        """GKE guarantees both are on under Autopilot and the API says so on
        every pool. The audit's job is to confirm that from the response, not
        to skip the field on the strength of the guarantee -- the run where
        they disagree is the only one that matters."""
        c = cluster(autopilot=True, node_pools=[pool(auto_upgrade=False, auto_repair=False)])
        self.assertEqual(len(pr.check_no_autoupgrade(c)), 1)
        self.assertEqual(len(pr.check_no_autorepair(c)), 1)
        healthy = cluster(autopilot=True, node_pools=[pool()])
        self.assertEqual(pr.check_no_autoupgrade(healthy), [])
        self.assertEqual(pr.check_no_autorepair(healthy), [])

    def test_flags_disabled_autoupgrade(self):
        c = cluster(node_pools=[pool(auto_upgrade=False)])
        self.assertEqual(len(pr.check_no_autoupgrade(c)), 1)

    def test_flags_disabled_autorepair(self):
        c = cluster(node_pools=[pool(auto_repair=False)])
        self.assertEqual(len(pr.check_no_autorepair(c)), 1)

    def test_the_excerpt_says_which_of_disabled_or_absent_was_read(self):
        """`management` is omitted entirely on a pool that has never had either
        setting, and that is a different observation from an explicit `false`."""
        explicit = cluster(node_pools=[pool(auto_repair=False)])
        missing = cluster(node_pools=[pool(management={})])
        self.assertEqual(
            pr.check_no_autorepair(explicit)[0]["excerpt"], "management.autoRepair=false"
        )
        self.assertEqual(
            pr.check_no_autorepair(missing)[0]["excerpt"], "management.autoRepair absent"
        )

    def test_does_not_flag_enabled(self):
        c = cluster(node_pools=[pool(auto_upgrade=True, auto_repair=True)])
        self.assertEqual(pr.check_no_autoupgrade(c), [])
        self.assertEqual(pr.check_no_autorepair(c), [])


class NoMaintenanceWindowTest(unittest.TestCase):
    def test_flags_no_window(self):
        c = cluster(**{"maintenancePolicy": {}})
        self.assertIsNotNone(pr.check_no_maintenance_window(c))

    def test_does_not_flag_recurring_window(self):
        c = cluster(**{"maintenancePolicy": {"window": {"recurringWindow": {}}}})
        self.assertIsNone(pr.check_no_maintenance_window(c))

    def test_does_not_flag_daily_window(self):
        c = cluster(**{"maintenancePolicy": {"window": {"dailyMaintenanceWindow": {}}}})
        self.assertIsNone(pr.check_no_maintenance_window(c))


class BlockingExclusionEscalationTest(unittest.TestCase):
    """§3.8's escalation input is derived in `collect_one_cluster` from the
    two version checks, so it is tested through it."""

    SHORT_END = "2026-01-25T00:00:00Z"
    LONG_END = "2026-06-01T00:00:00Z"

    def frozen(self, end, **kwargs):
        exclusion = {"startTime": "2026-01-01T00:00:00Z", "endTime": end, "maintenanceExclusionOptions": {"scope": "NO_UPGRADES"}}
        return cluster(maintenancePolicy={"window": {"recurringWindow": {}, "maintenanceExclusions": {"freeze": exclusion}}}, **kwargs)

    def blocking(self, c, baseline):
        slugs, candidates = pr.collect_one_cluster(c, baseline, now=NOW)
        hits = [cand for cand in candidates if cand["check"] == "blocking-exclusion"]
        return "blocking-exclusion" in slugs, hits

    def test_a_short_freeze_beside_a_pool_three_minors_behind_is_major(self):
        c = self.frozen(self.SHORT_END, node_pools=[pool(), pool("behind", version="1.27.3-gke.100")])
        judged, hits = self.blocking(c, BASELINE)
        self.assertTrue(judged)
        self.assertEqual([hit["severity"] for hit in hits], ["major"])

    def test_a_same_minor_master_lag_does_not_escalate(self):
        lagging = "1.30.3-gke.100"
        baseline = pr.normalize_server_config(server_config(valid_versions=[lagging, "1.30.5-gke.100"]))
        for end, expected in ((self.SHORT_END, []), (self.LONG_END, ["minor"])):
            with self.subTest(end=end):
                c = self.frozen(end, master=lagging, node_pools=[pool(version=lagging)])
                judged, hits = self.blocking(c, baseline)
                self.assertTrue(judged)
                self.assertEqual([hit["severity"] for hit in hits], expected)

    def test_a_freeze_the_version_checks_could_not_grade_leaves_commands(self):
        """With no baseline `master-behind` judged nothing, so a short freeze
        read as clean and a long one as `minor` over a cluster that may be
        critically behind."""
        for end in (self.SHORT_END, self.LONG_END):
            with self.subTest(end=end):
                self.assertEqual(self.blocking(self.frozen(end), None), (False, []))

    def test_a_freeze_whose_window_does_not_parse_leaves_commands(self):
        """The check skips an exclusion it cannot read, so `commands` said a
        freeze it never judged was clean."""
        for end in ("", "not-a-time", None):
            with self.subTest(end=end):
                c = self.frozen(end, node_pools=[pool(), pool("behind", version="1.27.3-gke.100")])
                self.assertEqual(self.blocking(c, BASELINE), (False, []))

    def test_an_unreadable_exclusion_outside_the_blocking_scopes_costs_nothing(self):
        c = self.frozen("not-a-time")
        c["maintenancePolicy"]["window"]["maintenanceExclusions"]["freeze"]["maintenanceExclusionOptions"]["scope"] = "NO_MINOR_UPGRADES"
        self.assertEqual(self.blocking(c, BASELINE), (True, []))

    def test_an_unfrozen_cluster_is_judged_without_the_version_checks(self):
        self.assertEqual(self.blocking(cluster(), None), (True, []))

    def test_a_version_finding_grades_the_freeze_without_the_other_check(self):
        c = self.frozen(self.SHORT_END, node_pools=[pool(), pool("behind", version="1.27.3-gke.100")])
        judged, hits = self.blocking(c, None)
        self.assertTrue(judged)
        self.assertEqual([hit["severity"] for hit in hits], ["major"])


class BlockingExclusionTest(unittest.TestCase):
    def exclusion_cluster(self, start, end, scope="NO_UPGRADES"):
        return cluster(
            maintenancePolicy={
                "window": {
                    "recurringWindow": {},
                    "maintenanceExclusions": {"freeze": {"startTime": start, "endTime": end, "maintenanceExclusionOptions": {"scope": scope}}},
                }
            }
        )

    def test_a_freeze_ending_past_thirty_days_is_flagged_even_without_a_version_finding(self):
        c = self.exclusion_cluster("2026-01-01T00:00:00Z", "2026-06-01T00:00:00Z")
        hit = pr.check_blocking_exclusion(c, now=NOW, has_version_finding=False)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["severity"], "minor")

    def test_a_freeze_ending_within_thirty_days_with_no_version_finding_is_not_flagged(self):
        c = self.exclusion_cluster("2026-01-01T00:00:00Z", "2026-01-20T00:00:00Z")
        self.assertIsNone(pr.check_blocking_exclusion(c, now=NOW, has_version_finding=False))

    def test_a_freeze_ending_within_thirty_days_holding_back_a_version_finding_is_major(self):
        c = self.exclusion_cluster("2026-01-01T00:00:00Z", "2026-01-20T00:00:00Z")
        hit = pr.check_blocking_exclusion(c, now=NOW, has_version_finding=True)
        self.assertEqual(hit["severity"], "major")

    def test_a_freeze_ending_thirty_days_and_change_out_counts_as_long(self):
        """`(end - now).days` truncates, so 30 days 23 hours read as 30 and
        fell under a `> 30` threshold. The SOP's rule is on the time left, not
        on its whole-day floor."""
        # NOW is 2026-01-15, so this ends 30 days and 23 hours out.
        c = self.exclusion_cluster("2026-01-01T00:00:00Z", "2026-02-14T23:00:00Z")
        hit = pr.check_blocking_exclusion(c, now=NOW, has_version_finding=False)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["severity"], "minor")

    def test_a_freeze_ending_exactly_thirty_days_out_is_not_long(self):
        c = self.exclusion_cluster("2026-01-01T00:00:00Z", "2026-02-14T00:00:00Z")
        self.assertIsNone(pr.check_blocking_exclusion(c, now=NOW, has_version_finding=False))

    def test_an_exclusion_with_no_scope_is_a_full_freeze(self):
        """`NO_UPGRADES` is the scope enum's zero value and GKE's JSON omits a
        zero value, so an exclusion created without a scope arrives with no
        `maintenanceExclusionOptions` at all -- the hardest freeze there is."""
        c = self.exclusion_cluster("2026-01-01T00:00:00Z", "2026-06-01T00:00:00Z")
        del c["maintenancePolicy"]["window"]["maintenanceExclusions"]["freeze"]["maintenanceExclusionOptions"]
        hit = pr.check_blocking_exclusion(c, now=NOW, has_version_finding=False)
        self.assertIsNotNone(hit)
        self.assertIn("NO_UPGRADES", hit["excerpt"])

    def test_a_timestamp_without_an_offset_is_read_as_utc(self):
        """Compared naive against an aware `now`, it raised `TypeError`, which
        `crashed_entries` turned into a gate-failed project."""
        c = self.exclusion_cluster("2026-01-01T00:00:00", "2026-06-01T00:00:00")
        self.assertIsNotNone(pr.check_blocking_exclusion(c, now=NOW, has_version_finding=False))

    def test_an_expired_exclusion_is_not_flagged(self):
        c = self.exclusion_cluster("2025-01-01T00:00:00Z", "2025-06-01T00:00:00Z")
        self.assertIsNone(pr.check_blocking_exclusion(c, now=NOW, has_version_finding=True))

    def test_a_future_exclusion_is_not_flagged(self):
        c = self.exclusion_cluster("2027-01-01T00:00:00Z", "2027-06-01T00:00:00Z")
        self.assertIsNone(pr.check_blocking_exclusion(c, now=NOW, has_version_finding=True))

    def test_no_minor_upgrades_scope_is_never_flagged(self):
        c = self.exclusion_cluster("2026-01-01T00:00:00Z", "2026-06-01T00:00:00Z", scope="NO_MINOR_UPGRADES")
        self.assertIsNone(pr.check_blocking_exclusion(c, now=NOW, has_version_finding=True))

    def test_exclusions_is_a_map_not_a_list(self):
        # Regression: iterating the raw dict without .items() would walk
        # the exclusion *names* as if they were the exclusion dicts.
        c = self.exclusion_cluster("2026-01-01T00:00:00Z", "2026-06-01T00:00:00Z")
        exclusions = c["maintenancePolicy"]["window"]["maintenanceExclusions"]
        self.assertIsInstance(exclusions, dict)
        self.assertIn("freeze", exclusions)
        hit = pr.check_blocking_exclusion(c, now=NOW, has_version_finding=False)
        self.assertIsNotNone(hit)
        self.assertIn("freeze", hit["excerpt"])


    def test_the_freeze_that_ends_last_is_reported_whatever_the_map_order(self):
        """Every qualifying exclusion overwrote the last, so the excerpt named
        whichever the API listed last: the wrong freeze to shorten, and one
        that could flip between runs."""
        long = {"startTime": "2026-01-01T00:00:00Z", "endTime": "2026-09-01T00:00:00Z", "maintenanceExclusionOptions": {"scope": "NO_UPGRADES"}}
        short = {"startTime": "2026-01-01T00:00:00Z", "endTime": "2026-03-01T00:00:00Z", "maintenanceExclusionOptions": {"scope": "NO_UPGRADES"}}
        for order in ((("long-freeze", long), ("short-freeze", short)), (("short-freeze", short), ("long-freeze", long))):
            with self.subTest(first=order[0][0]):
                c = cluster(maintenancePolicy={"window": {"recurringWindow": {}, "maintenanceExclusions": dict(order)}})
                hit = pr.check_blocking_exclusion(c, now=NOW, has_version_finding=False)
                self.assertIn("long-freeze", hit["excerpt"])
                self.assertIn("2026-09-01", hit["excerpt"])

class StaleImageTypeTest(unittest.TestCase):
    def test_autopilot_pools_carry_an_image_type_and_are_checked(self):
        """`config.imageType` is populated on Autopilot pools (`COS_CONTAINERD`
        on the fleet measured 2026-09-05), so the old reason for skipping --
        that no pool there carries one -- described a response GKE does not
        return."""
        c = cluster(autopilot=True, node_pools=[pool(image_type="COS")])
        self.assertEqual(len(pr.check_stale_image_type(c, BASELINE)), 1)
        healthy = cluster(autopilot=True, node_pools=[pool(image_type="COS_CONTAINERD")])
        self.assertEqual(pr.check_stale_image_type(healthy, BASELINE), [])

    def test_flags_deprecated_cos(self):
        c = cluster(node_pools=[pool(image_type="COS")])
        self.assertEqual(len(pr.check_stale_image_type(c, BASELINE)), 1)

    def test_flags_absent_from_valid_types(self):
        c = cluster(node_pools=[pool(image_type="SOME_FUTURE_TYPE")])
        self.assertEqual(len(pr.check_stale_image_type(c, BASELINE)), 1)

    def test_does_not_flag_current_containerd_variant(self):
        c = cluster(node_pools=[pool(image_type="COS_CONTAINERD")])
        self.assertEqual(pr.check_stale_image_type(c, BASELINE), [])

    def test_case_insensitive(self):
        c = cluster(node_pools=[pool(image_type="cos_containerd")])
        self.assertEqual(pr.check_stale_image_type(c, BASELINE), [])

    def test_an_empty_image_type_roster_flags_nothing(self):
        """`master-behind`'s empty-roster rule: a baseline without
        `validImageTypes` would otherwise call every pool no longer offered."""
        baseline = dict(BASELINE, validImageTypes=set())
        c = cluster(node_pools=[pool(image_type="COS_CONTAINERD")])
        self.assertEqual(pr.check_stale_image_type(c, baseline), [])

    def test_an_empty_image_type_roster_is_not_recorded_as_run(self):
        baseline = dict(BASELINE, validImageTypes=set())
        slugs, _ = pr.collect_one_cluster(cluster(), baseline, now=NOW)
        self.assertNotIn("stale-image-type", slugs)
        self.assertIn("master-behind", slugs)

    def test_no_baseline_is_not_a_crash(self):
        c = cluster(node_pools=[pool(image_type="COS")])
        self.assertEqual(pr.check_stale_image_type(c, None), [])


class NoNotificationsTest(unittest.TestCase):
    def test_flags_disabled(self):
        c = cluster(**{"notificationConfig": {"pubsub": {"enabled": False}}})
        self.assertEqual(
            pr.check_no_notifications(c)["excerpt"], "notificationConfig.pubsub.enabled=false"
        )

    def test_flags_absent(self):
        c = cluster(**{"notificationConfig": {}})
        self.assertEqual(
            pr.check_no_notifications(c)["excerpt"], "notificationConfig.pubsub.enabled absent"
        )

    def test_disabled_and_absent_do_not_read_the_same(self):
        """The two cases above are one finding with two different fixes: an
        absent block has to be created, a disabled one flipped. They shared an
        excerpt reading "false or absent" until this asserted otherwise, which
        also meant a cluster moving from absent to explicitly-false published
        byte-identical evidence and showed up as unchanged."""
        disabled = cluster(**{"notificationConfig": {"pubsub": {"enabled": False}}})
        absent = cluster(**{"notificationConfig": {}})
        self.assertNotEqual(
            pr.check_no_notifications(disabled)["excerpt"],
            pr.check_no_notifications(absent)["excerpt"],
        )

    def test_enabled_with_no_filter_is_not_flagged(self):
        c = cluster(**{"notificationConfig": {"pubsub": {"enabled": True}}})
        self.assertIsNone(pr.check_no_notifications(c))

    def test_enabled_with_filter_excluding_upgrade_event_is_flagged(self):
        c = cluster(**{"notificationConfig": {"pubsub": {"enabled": True, "filter": {"eventType": ["SECURITY_BULLETIN_EVENT"]}}}})
        self.assertIsNotNone(pr.check_no_notifications(c))

    def test_enabled_with_filter_including_upgrade_event_is_not_flagged(self):
        c = cluster(**{"notificationConfig": {"pubsub": {"enabled": True, "filter": {"eventType": ["UPGRADE_AVAILABLE_EVENT"]}}}})
        self.assertIsNone(pr.check_no_notifications(c))


    def test_a_pool_with_a_scheduled_upgrade_is_not_flagged(self):
        """§3.10's Do NOT flag: `autoUpgradeStartTime` on a pool is GKE having
        already scheduled the upgrade the notification would announce."""
        scheduled = pool(management={"autoUpgrade": True, "autoRepair": True, "upgradeOptions": {"autoUpgradeStartTime": "2026-01-20T00:00:00Z"}})
        c = cluster(node_pools=[scheduled], **{"notificationConfig": {"pubsub": {"enabled": False}}})
        self.assertIsNone(pr.check_no_notifications(c))


class DefaultRunTest(unittest.TestCase):
    def test_a_timeout_that_wrote_stderr_returns_text(self):
        """`TimeoutExpired.stderr` is bytes even under `text=True`, and every
        caller matches markers against a str."""
        exc = pr.subprocess.TimeoutExpired(["gcloud"], 1, output=b"", stderr=b"zone did not respond")
        with mock.patch.object(pr.subprocess, "run", side_effect=exc):
            result = pr.default_run(["gcloud"])
        self.assertEqual(result.rc, pr.TIMEOUT_RC)
        self.assertIn(pr.ZONE_TIMEOUT_MARKER, result.stderr)


class IncumbentTopicTest(unittest.TestCase):
    """`attach_incumbent_topic` -- the fleet fact behind §3.10's topic bullet.

    A cluster that fires this check with Pub/Sub off has no topic, so the
    only place the agent can learn which topic the fleet already uses is from a
    cluster that is not the finding's subject. That is what this pass carries.
    """

    TOPIC = "projects/acme/topics/gke-upgrade-notifications"
    OTHER = "projects/acme/topics/somewhere-else"

    def entry(self, name, topic, *, checks=("no-notifications",), outcome="collected"):
        return {
            "name": name,
            "outcome": outcome,
            "_notification_topic": topic,
            "candidates": [
                {"check": check, "object": f"Cluster/{name}", "excerpt": "notificationConfig.pubsub.enabled absent"}
                for check in checks
            ],
        }

    def excerpt(self, entry, index=0):
        return entry["candidates"][index]["excerpt"]

    def test_the_enrolled_cluster_names_the_topic_for_the_one_that_is_not(self):
        entries = [self.entry("enrolled", self.TOPIC, checks=()), self.entry("bare", "")]
        pr.attach_incumbent_topic(entries)
        self.assertIn(self.TOPIC, self.excerpt(entries[1]))

    def test_the_original_excerpt_survives_the_append(self):
        entries = [self.entry("enrolled", self.TOPIC, checks=()), self.entry("bare", "")]
        pr.attach_incumbent_topic(entries)
        self.assertTrue(self.excerpt(entries[1]).startswith("notificationConfig.pubsub.enabled absent"))

    def test_a_fleet_nobody_has_enrolled_gets_no_invented_topic(self):
        """§3.10's naming rule owns the green-field case. A topic appended here
        would be one no cluster publishes to -- a guess wearing the collector's
        authority, which the agent has no way to tell from a measurement."""
        entries = [self.entry("a", ""), self.entry("b", "")]
        pr.attach_incumbent_topic(entries)
        self.assertNotIn("publish upgrade notifications", self.excerpt(entries[0]))

    def test_a_fleet_split_across_two_topics_gets_no_hint(self):
        entries = [self.entry("a", self.TOPIC, checks=()), self.entry("b", self.OTHER, checks=()), self.entry("c", "")]
        pr.attach_incumbent_topic(entries)
        self.assertNotIn("publish upgrade notifications", self.excerpt(entries[2]))

    def test_two_clusters_on_one_topic_still_count_as_one(self):
        entries = [self.entry("a", self.TOPIC, checks=()), self.entry("b", self.TOPIC, checks=()), self.entry("c", "")]
        pr.attach_incumbent_topic(entries)
        self.assertIn(self.TOPIC, self.excerpt(entries[2]))

    def test_only_the_no_notifications_candidate_is_touched(self):
        entries = [self.entry("enrolled", self.TOPIC, checks=()), self.entry("bare", "", checks=("no-notifications", "master-behind"))]
        pr.attach_incumbent_topic(entries)
        self.assertIn(self.TOPIC, self.excerpt(entries[1], 0))
        self.assertNotIn(self.TOPIC, self.excerpt(entries[1], 1))

    def test_a_cluster_filtering_its_own_topic_is_not_pointed_at_the_fleets(self):
        entries = [self.entry("enrolled", self.TOPIC, checks=()), self.entry("filtered", self.TOPIC)]
        entries[1]["candidates"][0]["excerpt"] = f"{pr.FILTER_EXCLUDES_PREFIX} UPGRADE_AVAILABLE_EVENT: ['SECURITY_BULLETIN_EVENT']"
        pr.attach_incumbent_topic(entries)
        self.assertNotIn(self.TOPIC, self.excerpt(entries[1]))

    def test_the_private_key_never_reaches_the_manifest(self):
        entries = [self.entry("enrolled", self.TOPIC, checks=()), self.entry("bare", "")]
        pr.attach_incumbent_topic(entries)
        self.assertNotIn("_notification_topic", entries[0])
        self.assertNotIn("_notification_topic", entries[1])

    def test_the_key_is_dropped_even_when_no_topic_is_attached(self):
        """The pass returns early on a split fleet, and the key has to go
        anyway -- a leaked underscore key is an unknown manifest field."""
        entries = [self.entry("a", self.TOPIC, checks=()), self.entry("b", self.OTHER, checks=())]
        pr.attach_incumbent_topic(entries)
        self.assertNotIn("_notification_topic", entries[0])

    def test_an_unreadable_cluster_neither_supplies_nor_receives_a_topic(self):
        entries = [self.entry("gone", self.TOPIC, checks=(), outcome="gate-failed"), self.entry("bare", "")]
        pr.attach_incumbent_topic(entries)
        self.assertNotIn("publish upgrade notifications", self.excerpt(entries[1]))

    def test_a_disabled_block_carrying_a_stale_topic_is_not_an_incumbent(self):
        """`notificationConfig.pubsub.topic` outlives `enabled: false` in the
        API response, and a topic nothing publishes to is not evidence."""
        c = cluster(**{"notificationConfig": {"pubsub": {"enabled": False, "topic": self.TOPIC}}})
        self.assertEqual(pr.notification_topic(c), "")

    def test_a_cluster_filtering_out_upgrade_events_is_not_an_incumbent(self):
        """It publishes to the topic, but not upgrade notifications, and this
        same stream flags it for that; naming its topic as where the fleet
        publishes them would put a false sentence in the ledger."""
        pubsub = {"enabled": True, "topic": self.TOPIC, "filter": {"eventType": ["SECURITY_BULLETIN_EVENT"]}}
        self.assertEqual(pr.notification_topic(cluster(notificationConfig={"pubsub": pubsub})), "")
        pubsub["filter"]["eventType"].append(pr.UPGRADE_AVAILABLE_EVENT)
        self.assertEqual(pr.notification_topic(cluster(notificationConfig={"pubsub": pubsub})), self.TOPIC)

    def test_an_enrolled_cluster_reports_its_topic(self):
        c = cluster(**{"notificationConfig": {"pubsub": {"enabled": True, "topic": self.TOPIC}}})
        self.assertEqual(pr.notification_topic(c), self.TOPIC)

    def test_the_fleet_pass_reads_what_the_collector_recorded(self):
        """End to end through `collect_fleet`, so the two halves cannot drift:
        the worker writes `_notification_topic`, the fleet pass reads it."""
        clusters = [
            cluster(name="enrolled", **{"notificationConfig": {"pubsub": {"enabled": True, "topic": self.TOPIC}}}),
            cluster(name="bare", **{"notificationConfig": {}}),
        ]
        responses = {
            "projects list": run_of(0, json.dumps([{"projectId": "acme"}])),
            "clusters list": run_of(0, json.dumps(clusters)),
            "get-server-config": run_of(0, json.dumps(server_config())),
        }

        def run(argv, **kwargs):
            joined = " ".join(argv)
            for needle, result in responses.items():
                if needle in joined:
                    return result
            return run_of(1, stderr="unexpected")

        manifest = pr.collect_fleet("acme", run=run, now=NOW)
        bare = next(e for e in manifest["clusters"] if short(e) == "bare")
        hit = next(c for c in bare["candidates"] if c["check"] == "no-notifications")
        self.assertIn(self.TOPIC, hit["excerpt"])


class CollectProjectTest(unittest.TestCase):
    def fake_run(self, responses):
        def run(argv, **kwargs):
            joined = " ".join(argv)
            for needle, result in responses.items():
                if needle in joined:
                    return result
            raise AssertionError(f"unstubbed command: {joined}")

        return run

    def test_a_clean_project_collects_with_no_candidates(self):
        c = cluster()
        responses = {
            "clusters list": run_of(0, json.dumps([c])),
            "get-server-config": run_of(0, json.dumps(server_config())),
        }
        entries = pr.collect_project("acme", run=self.fake_run(responses), now=NOW)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["outcome"], "collected")
        self.assertEqual(entries[0]["candidates"], [])
        self.assertEqual({c["check"] for c in entries[0]["commands"]}, set(pr.SEVERITY))

    def test_an_unjudged_baseline_check_is_left_out_of_commands(self):
        """`collect_one_cluster` drops a slug whose roster the baseline lacks,
        and this re-added both baseline checks whenever the server config was
        read, so the manifest recorded them as run and clean all the same."""
        raw = server_config()
        raw["channels"][0]["validVersions"] = []
        raw["validMasterVersions"] = []
        raw["validImageTypes"] = []
        responses = {
            "clusters list": run_of(0, json.dumps([cluster()])),
            "get-server-config": run_of(0, json.dumps(raw)),
        }
        entries = pr.collect_project("acme", run=self.fake_run(responses), now=NOW)
        checks = {c["check"] for c in entries[0]["commands"]}
        self.assertNotIn("master-behind", checks)
        self.assertNotIn("stale-image-type", checks)

    def test_an_unparseable_master_version_leaves_the_version_checks_out_of_commands(self):
        """`check_master_behind`, `check_pool_skew` and the spread all skip a
        master version they cannot parse, and the manifest still listed their
        slugs as run: in `commands`, no candidate reads as clean."""
        for label, master in (("absent", None), ("new-shape", "1.31")):
            with self.subTest(master=label):
                c = cluster(master=master)
                if master is None:
                    del c["currentMasterVersion"]
                responses = {
                    "clusters list": run_of(0, json.dumps([c])),
                    "get-server-config": run_of(0, json.dumps(server_config())),
                }
                entries = pr.collect_project("acme", run=self.fake_run(responses), now=NOW)
                checks = {cmd["check"] for cmd in entries[0]["commands"]}
                self.assertEqual(checks & {"master-behind", "pool-skew", "fleet-spread"}, set())
                self.assertIn("stale-image-type", checks)
                self.assertIn("no-channel", checks)

    def test_a_pool_the_version_checks_cannot_read_leaves_its_check_out_of_commands(self):
        """The master parses, so the cluster-level rule keeps `pool-skew`; a
        pool the check skipped would still have read as judged and clean."""
        cases = (
            ("pool-skew", pool("odd", version="1.29")),
            ("stale-image-type", pool("bare", config={})),
        )
        for slug, unreadable in cases:
            with self.subTest(slug=slug):
                behind = pool("behind", version="1.27.3-gke.100", image_type="UBUNTU")
                c = cluster(node_pools=[pool(), behind, unreadable])
                responses = {
                    "clusters list": run_of(0, json.dumps([c])),
                    "get-server-config": run_of(0, json.dumps(server_config())),
                }
                entry = pr.collect_project("acme", run=self.fake_run(responses), now=NOW)[0]
                self.assertNotIn(slug, {cmd["check"] for cmd in entry["commands"]})
                self.assertNotIn(slug, {cand["check"] for cand in entry["candidates"]})
                self.assertIn("no-channel", {cmd["check"] for cmd in entry["commands"]})

    def test_a_pool_mid_upgrade_does_not_cost_pool_skew_its_slug(self):
        """`check_pool_skew` leaves a reconciling pool out on purpose, so its
        version is not one the check needed to read."""
        c = cluster(node_pools=[pool(), pool("moving", version="", status="RECONCILING")])
        responses = {
            "clusters list": run_of(0, json.dumps([c])),
            "get-server-config": run_of(0, json.dumps(server_config())),
        }
        entry = pr.collect_project("acme", run=self.fake_run(responses), now=NOW)[0]
        self.assertIn("pool-skew", {cmd["check"] for cmd in entry["commands"]})

    def test_clusters_list_failure_is_recorded_as_a_gate_failed_project(self):
        """Returning [] dropped the project out of the manifest, where it read
        as a project holding no clusters rather than one nobody could
        enumerate — so nothing held the document to those clusters and the run
        published a fleet verdict over a fleet it had not seen."""
        entries = pr.collect_project("acme", run=self.fake_run({"clusters list": run_of(1, "", "denied")}), now=NOW)
        self.assertEqual([e["name"] for e in entries], ["project/acme"])
        self.assertEqual(entries[0]["outcome"], "gate-failed")
        self.assertIn("denied", entries[0]["error"])
        self.assertIn("rc=1", entries[0]["error"])

    def test_a_readable_project_adds_no_project_entry(self):
        responses = {
            "clusters list": run_of(0, json.dumps([cluster()])),
            "get-server-config": run_of(0, json.dumps(server_config())),
        }
        entries = pr.collect_project("acme", run=self.fake_run(responses), now=NOW)
        self.assertEqual([e for e in entries if e["name"].startswith("project/")], [])

    def test_every_cluster_publishes_the_mode(self):
        """The mode already silences four of the ten checks here, so this
        collector resolves it before writing a line — and then withheld it,
        leaving each stream to re-derive a fact it was holding."""
        responses = {
            "clusters list": run_of(0, json.dumps([cluster("ap", autopilot=True), cluster("std")])),
            "get-server-config": run_of(0, json.dumps(server_config())),
        }
        entries = pr.collect_project("acme", run=self.fake_run(responses), now=NOW)
        self.assertEqual({short(e): e["autopilot"] for e in entries}, {"ap": True, "std": False})

    def test_the_project_level_entry_claims_no_mode(self):
        """A project is not a cluster. The gate-failed entry stands for a
        `clusters list` that never answered, so there is no mode to publish and
        a `false` there would read as a fleet of Standard clusters."""
        entries = pr.collect_project("acme", run=self.fake_run({"clusters list": run_of(1, "", "denied")}), now=NOW)
        self.assertNotIn("autopilot", entries[0])

    def test_get_server_config_failure_drops_only_the_baseline_checks(self):
        c = cluster()
        responses = {
            "clusters list": run_of(0, json.dumps([c])),
            "get-server-config": run_of(1, "", "denied"),
        }
        entries = pr.collect_project("acme", run=self.fake_run(responses), now=NOW)
        self.assertEqual(entries[0]["outcome"], "collected")
        slugs = {cmd["check"] for cmd in entries[0]["commands"]}
        self.assertNotIn("master-behind", slugs)
        self.assertNotIn("stale-image-type", slugs)
        self.assertIn("pool-skew", slugs)

    def test_a_dirty_cluster_reports_findings(self):
        c = cluster(node_pools=[pool(auto_upgrade=False, auto_repair=False)])
        responses = {
            "clusters list": run_of(0, json.dumps([c])),
            "get-server-config": run_of(0, json.dumps(server_config())),
        }
        entries = pr.collect_project("acme", run=self.fake_run(responses), now=NOW)
        slugs = {cand["check"] for cand in entries[0]["candidates"]}
        self.assertIn("no-autoupgrade", slugs)
        self.assertIn("no-autorepair", slugs)

    def test_only_the_laggard_carries_the_fleet_spread_finding(self):
        clusters = [cluster(name="old", master="1.28.0-gke.1"), cluster(name="new", master="1.30.0-gke.1")]
        responses = {
            "clusters list": run_of(0, json.dumps(clusters)),
            "get-server-config": run_of(0, json.dumps(server_config())),
        }
        entries = pr.collect_project("acme", run=self.fake_run(responses), now=NOW)
        pr.attach_fleet_spread(entries)
        old_entry = next(e for e in entries if short(e) == "old")
        new_entry = next(e for e in entries if short(e) == "new")
        self.assertIn("fleet-spread", {c["check"] for c in old_entry["candidates"]})
        self.assertNotIn("fleet-spread", {c["check"] for c in new_entry["candidates"]})

    def test_both_clusters_record_the_fleet_spread_command(self):
        """§3.3 emits one finding, but it reads every cluster to get there.

        The clean-fleet half of this is the one that used to be wrong: a fleet
        with no spread produced no hit, so no cluster recorded the command, and
        §6 scored `fleet-spread` as never run on all of them."""
        spread = [cluster(name="old", master="1.28.0-gke.1"), cluster(name="new", master="1.30.0-gke.1")]
        tight = [cluster(name="a", master="1.30.0-gke.1"), cluster(name="b", master="1.30.1-gke.2")]
        for label, clusters in (("spread", spread), ("tight", tight)):
            with self.subTest(fleet=label):
                responses = {
                    "clusters list": run_of(0, json.dumps(clusters)),
                    "get-server-config": run_of(0, json.dumps(server_config())),
                }
                entries = pr.collect_project("acme", run=self.fake_run(responses), now=NOW)
                for entry in entries:
                    self.assertIn("fleet-spread", {c["check"] for c in entry["commands"]})

    def test_a_clean_fleet_reports_no_coverage_gap_for_fleet_spread(self):
        """End-to-end: §6's arithmetic over a tight fleet, which is the shape
        every healthy run of this audit takes."""
        clusters = [cluster(name="a"), cluster(name="b")]
        responses = {
            "clusters list": run_of(0, json.dumps(clusters)),
            "get-server-config": run_of(0, json.dumps(server_config())),
        }
        entries = pr.collect_project("acme", run=self.fake_run(responses), now=NOW)
        roster = set(audit_report.audit_target_checks("security-patch-orchestrator", "a"))
        for entry in entries:
            self.assertEqual(entry["candidates"], [])
            self.assertEqual(roster - {c["check"] for c in entry["commands"]}, set())

    def test_the_sop_spells_an_unreadable_project_the_way_the_manifest_does(self):
        """The SOP told the worker to write `<project>/*` and the collector
        writes `project/<project>`, so the cross-check refused every document
        that followed the instruction: a run that hit one unreadable project
        could not publish at all. Derive the spelling from the collector rather
        than restating it, so moving the f-string moves this assertion too."""
        sop = os.path.join(os.path.dirname(__file__), "..", "..", "..", "governance", "security_patch_orchestrator_sop.md")
        if not os.path.exists(sop):  # not shipped alongside the skill at runtime
            self.skipTest(f"{sop} not present")
        with open(sop, encoding="utf-8") as handle:
            body = handle.read()
        entries = pr.collect_project("acme", run=self.fake_run({"clusters list": run_of(1, "", "denied")}), now=NOW)
        template = entries[0]["name"].replace("acme", "<project>")
        self.assertIn(f'"cluster": "{template}"', body)
        self.assertNotIn("<project>/*", body)


class CollectFleetTest(unittest.TestCase):
    def test_project_override_skips_discovery(self):
        def run(argv, **kwargs):
            if "clusters" in argv and "list" in argv:
                return run_of(0, json.dumps([cluster()]))
            if "get-server-config" in argv:
                return run_of(0, json.dumps(server_config()))
            raise AssertionError(f"unexpected discovery call: {argv}")

        manifest = pr.collect_fleet("acme-only", run=run, now=NOW)
        read = [c for c in manifest["clusters"] if c["name"] != pr.UNENUMERATED_PROJECTS_TARGET]
        self.assertEqual({c["project"] for c in read}, {"acme-only"})
        self.assertEqual(manifest["audit"], "security-patch-orchestrator")

    def test_a_scoped_run_says_it_never_looked_at_the_rest_of_the_fleet(self):
        """`--project` skips discovery, so without a row saying so the manifest
        reads as a fleet of one project and `finish` resolves every ledger
        finding on a cluster in any other. §3.3's spread is also measured
        across the one project only."""

        def run(argv, **kwargs):
            if "clusters" in argv and "list" in argv:
                return run_of(0, json.dumps([cluster()]))
            if "get-server-config" in argv:
                return run_of(0, json.dumps(server_config()))
            raise AssertionError(f"unexpected discovery call: {argv}")

        manifest = pr.collect_fleet("acme-only", run=run, now=NOW)
        by_name = {short(c): c for c in manifest["clusters"]}
        entry = by_name["project/UNENUMERATED_PROJECTS"]
        self.assertEqual(entry["outcome"], "gate-failed")
        self.assertIn("--project", entry["error"])
        self.assertIn("acme-only", entry["error"])
        self.assertNotIn("error", manifest)

    def test_a_project_the_sweep_cannot_read_stays_in_scope_as_gate_failed(self):
        # Dropping a project whose `clusters list` fails means a project nobody
        # could enumerate leaves no trace in the manifest at all.
        def run(argv, **kwargs):
            if argv[:3] == ["gcloud", "config", "get-value"]:
                return run_of(0, "base\n")
            if argv[:3] == ["gcloud", "projects", "list"]:
                return run_of(0, "base\nforbidden\n")
            if "clusters" in argv and "list" in argv:
                if "forbidden" in argv:
                    return run_of(1, "", "PERMISSION_DENIED: container.clusters.list")
                return run_of(0, json.dumps([cluster()]))
            if "get-server-config" in argv:
                return run_of(0, json.dumps(server_config()))
            raise AssertionError(f"unexpected call: {argv}")

        manifest = pr.collect_fleet(run=run, now=NOW)
        by_name = {short(c): c for c in manifest["clusters"]}
        self.assertIn("project/forbidden", by_name)
        self.assertEqual(by_name["project/forbidden"]["outcome"], "gate-failed")
        self.assertIn("PERMISSION_DENIED", by_name["project/forbidden"]["error"])

    def test_one_project_crashing_costs_that_project_and_no_other(self):
        """`future.result()` re-raises, and the SOP redirects this collector's
        stdout into the manifest — so an unmodelled exception on one project
        used to leave a zero-byte file and lose the whole fleet. Only a failed
        `clusters list` was modelled; a `TypeError` off an unexpected API shape
        was not."""
        boom_calls = []

        def run(argv, **kwargs):
            if argv[:3] == ["gcloud", "config", "get-value"]:
                return run_of(0, "base\n")
            if argv[:3] == ["gcloud", "projects", "list"]:
                return run_of(0, "base\nboom\n")
            if "clusters" in argv and "list" in argv:
                if "boom" in argv:
                    boom_calls.append(argv)
                    raise TypeError("unsupported operand type(s) for /: 'str' and 'str'")
                return run_of(0, json.dumps([cluster()]))
            if "get-server-config" in argv:
                return run_of(0, json.dumps(server_config()))
            raise AssertionError(f"unexpected call: {argv}")

        manifest = pr.collect_fleet(run=run, now=NOW)
        by_name = {short(c): c for c in manifest["clusters"]}
        self.assertEqual(by_name["project/boom"]["outcome"], "gate-failed")
        self.assertIn("TypeError", by_name["project/boom"]["error"])
        self.assertIn("base", {c["project"] for c in manifest["clusters"]})

    def test_the_spread_is_measured_across_projects_not_within_one(self):
        """§3.3 is "across all audited clusters", and computing it inside the
        per-project worker made it neither: a fleet whose two minors live in
        two projects reported nothing, and a fleet spread across three
        reported it three times with three different laggards."""

        def run(argv, **kwargs):
            if argv[:3] == ["gcloud", "config", "get-value"]:
                return run_of(0, "old-proj\n")
            if argv[:3] == ["gcloud", "projects", "list"]:
                return run_of(0, "old-proj\nnew-proj\n")
            if "clusters" in argv and "list" in argv:
                master = "1.28.0-gke.1" if "old-proj" in argv else "1.30.0-gke.1"
                name = "old" if "old-proj" in argv else "new"
                return run_of(0, json.dumps([cluster(name=name, master=master)]))
            if "get-server-config" in argv:
                return run_of(0, json.dumps(server_config(valid_versions=["1.28.0-gke.1", "1.30.0-gke.1"])))
            raise AssertionError(f"unexpected call: {argv}")

        manifest = pr.collect_fleet(run=run, now=NOW)
        by_name = {short(c): c for c in manifest["clusters"]}
        self.assertIn("fleet-spread", {c["check"] for c in by_name["old"]["candidates"]})
        self.assertNotIn("fleet-spread", {c["check"] for c in by_name["new"]["candidates"]})
        self.assertNotIn("_master_version", by_name["old"])
        self.assertNotIn("_status", by_name["old"])

    def test_same_named_clusters_in_two_projects_stay_two_targets(self):
        """`audit_report._vouching_clusters` keys on the manifest name, so two
        bare `seeded-b` entries collapse into one. Every name is qualified, and
        the candidate's `object` keeps the bare resource."""

        def run(argv, **kwargs):
            if argv[:3] == ["gcloud", "config", "get-value"]:
                return run_of(0, "a\n")
            if argv[:3] == ["gcloud", "projects", "list"]:
                return run_of(0, "a\nb\n")
            if "clusters" in argv and "list" in argv:
                return run_of(0, json.dumps([cluster(name="seeded-b", channel="")]))
            if "get-server-config" in argv:
                return run_of(0, json.dumps(server_config()))
            raise AssertionError(f"unexpected call: {argv}")

        manifest = pr.collect_fleet(run=run, now=NOW)
        names = sorted(c["name"] for c in manifest["clusters"])
        self.assertEqual(names, ["a/us-central1/seeded-b", "b/us-central1/seeded-b"])
        for entry in manifest["clusters"]:
            self.assertEqual({c["object"] for c in entry["candidates"]}, {"Cluster/seeded-b"})

    def test_qualifying_the_cluster_name_needed_an_id_scheme_bump(self):
        """The qualified name is the `cluster` segment of every finding id, so
        each patch finding is re-spelled on its first run under the collector.
        Without a new `ID_SCHEME` the delta reads the rename as a fix: every
        open finding announced resolved, re-filed as new, and its remediation
        pull request closed as stale."""

        def fid(cluster_name):
            return audit_report.derive_finding_id(
                {"check": "master-behind", "cluster": cluster_name, "namespace": "", "object": "Cluster/seeded-b"}
            )

        self.assertNotEqual(fid("seeded-b"), fid(pr.target_name("p", "us-central1-a", "seeded-b")))
        self.assertGreaterEqual(audit_report.ID_SCHEME, 4)

    def test_a_cluster_section_one_skips_is_not_marked_collected(self):
        """§1.5 skips a PROVISIONING/STOPPING/ERROR or alpha cluster, and the
        collector marks it `out-of-scope`, which goes in neither scope list. It
        used to mark it `collected`, which `cross_check_manifest` holds to
        `scope.clusters`, so on a fleet holding one such cluster the run could
        not publish whichever list the model chose."""
        clusters = [
            cluster(name="fine"),
            cluster(name="mid-flight", status="PROVISIONING"),
            cluster(name="going", status="STOPPING"),
            cluster(name="broken", status="ERROR"),
            cluster(name="alpha", enableKubernetesAlpha=True),
        ]
        responses = {
            "clusters list": run_of(0, json.dumps(clusters)),
            "get-server-config": run_of(0, json.dumps(server_config())),
        }

        def run(argv, **kwargs):
            joined = " ".join(argv)
            for needle, result in responses.items():
                if needle in joined:
                    return result
            raise AssertionError(f"unstubbed command: {joined}")

        by_name = {short(e): e for e in pr.collect_project("acme", run=run, now=NOW)}
        self.assertEqual(by_name["fine"]["outcome"], "collected")
        for name in ("mid-flight", "going", "broken", "alpha"):
            with self.subTest(cluster=name):
                self.assertEqual(by_name[name]["outcome"], "out-of-scope")
                self.assertNotIn("candidates", by_name[name])
                self.assertGreater(len(by_name[name]["error"]), 16)

    def test_a_project_with_no_clusters_is_left_out_rather_than_gate_failed(self):
        # The other half of the same distinction: an empty list is an answer,
        # and a project that genuinely holds no clusters owes this audit
        # nothing. Only a failed read is a loss worth recording.
        def run(argv, **kwargs):
            if argv[:3] == ["gcloud", "config", "get-value"]:
                return run_of(0, "base\n")
            if argv[:3] == ["gcloud", "projects", "list"]:
                return run_of(0, "base\nempty\n")
            if "clusters" in argv and "list" in argv:
                if "empty" in argv:
                    return run_of(0, "[]")
                return run_of(0, json.dumps([cluster()]))
            if "get-server-config" in argv:
                return run_of(0, json.dumps(server_config()))
            raise AssertionError(f"unexpected call: {argv}")

        manifest = pr.collect_fleet(run=run, now=NOW)
        self.assertEqual({c["project"] for c in manifest["clusters"]}, {"base"})


    def discovering(self, *, base="base", listing=(0, "base\n"), clusters=None):
        clusters = clusters or {}

        def run(argv, **kwargs):
            if argv[:3] == ["gcloud", "config", "get-value"]:
                return run_of(0, f"{base}\n") if base else run_of(1, "", "unset")
            if argv[:3] == ["gcloud", "projects", "list"]:
                rc, out = listing
                return run_of(rc, out, "" if rc == 0 else "PERMISSION_DENIED: resourcemanager.projects.list")
            if "clusters" in argv and "list" in argv:
                project = argv[argv.index("--project") + 1]
                return clusters.get(project, run_of(0, json.dumps([cluster(name=f"{project}-c")])))
            if "get-server-config" in argv:
                return run_of(0, json.dumps(server_config()))
            raise AssertionError(f"unexpected call: {argv}")

        return run

    def test_a_project_with_the_api_disabled_reads_as_empty(self):
        """A project whose Kubernetes Engine API is off cannot hold a cluster;
        treating its failed listing as a lost project makes every non-GKE
        project the credential sees a coverage gap on every run."""
        disabled = run_of(1, "", "ERROR: SERVICE_DISABLED: Kubernetes Engine API has not been used in project 42")
        run = self.discovering(listing=(0, "base\nnogke\n"), clusters={"nogke": disabled})
        manifest = pr.collect_fleet(run=run, now=NOW)
        self.assertEqual({c["project"] for c in manifest["clusters"]}, {"base"})
        self.assertNotIn("error", manifest)

    def test_a_listing_some_zones_did_not_answer_keeps_its_clusters_and_says_so(self):
        partial = run_of(0, json.dumps([cluster()]), "WARNING: The following zones did not respond: us-east1-b.")
        run = self.discovering(clusters={"base": partial})
        entries = pr.collect_fleet(run=run, now=NOW)["clusters"]
        self.assertEqual([e["outcome"] for e in entries if e["project"] == "base"].count("collected"), 1)
        (row,) = [e for e in entries if e["outcome"] == "gate-failed"]
        self.assertEqual(row["name"], "project/base")
        self.assertIn("us-east1-b", row["error"])

    def test_a_failed_project_listing_leaves_an_unenumerated_row(self):
        run = self.discovering(listing=(1, ""))
        manifest = pr.collect_fleet(run=run, now=NOW)
        by_name = {short(c): c for c in manifest["clusters"]}
        self.assertEqual(by_name["project/UNENUMERATED_PROJECTS"]["outcome"], "gate-failed")
        self.assertIn("PERMISSION_DENIED", by_name["project/UNENUMERATED_PROJECTS"]["error"])
        self.assertEqual(by_name["base-c"]["outcome"], "collected")
        self.assertNotIn("error", manifest)

    def test_a_filtered_project_listing_leaves_an_unenumerated_row(self):
        """rc 0 without the active project in its own output: the listing is
        filtered, so the fleet is short by projects nobody can name."""
        run = self.discovering(listing=(0, "other\n"))
        by_name = {short(c): c for c in pr.collect_fleet(run=run, now=NOW)["clusters"]}
        self.assertIn("filtered", by_name["project/UNENUMERATED_PROJECTS"]["error"])
        self.assertIn("base-c", by_name)
        self.assertIn("other-c", by_name)

    def test_a_complete_listing_leaves_no_unenumerated_row(self):
        run = self.discovering(listing=(0, "base\nother\n"))
        names = [short(c) for c in pr.collect_fleet(run=run, now=NOW)["clusters"]]
        self.assertNotIn("project/UNENUMERATED_PROJECTS", names)
        self.assertEqual(sorted(names), ["base-c", "other-c"])

    def test_no_project_at_all_is_a_top_level_error(self):
        run = self.discovering(base="", listing=(1, ""))
        manifest = pr.collect_fleet(run=run, now=NOW)
        self.assertIn("project discovery failed", manifest["error"])
        self.assertEqual(manifest["clusters"], [])

    def test_every_project_failing_is_a_top_level_error(self):
        denied = run_of(1, "", "PERMISSION_DENIED: container.clusters.list")
        run = self.discovering(listing=(0, "base\nother\n"), clusters={"base": denied, "other": denied})
        manifest = pr.collect_fleet(run=run, now=NOW)
        self.assertIn("no cluster could be read", manifest["error"])

    def test_a_failed_project_beside_one_holding_only_out_of_scope_clusters(self):
        """The answering project listed a cluster, so "held none" was false
        about a manifest that carries its out-of-scope entry."""
        denied = run_of(1, "", "PERMISSION_DENIED: container.clusters.list")
        provisioning = run_of(0, json.dumps([cluster(name="new", status="PROVISIONING")]))
        run = self.discovering(listing=(0, "base\nother\n"), clusters={"base": denied, "other": provisioning})
        manifest = pr.collect_fleet(run=run, now=NOW)
        self.assertIn("no cluster could be read", manifest["error"])
        self.assertIn("no auditable cluster (1 out of scope)", manifest["error"])
        self.assertNotIn("held none", manifest["error"])
        self.assertIn("out-of-scope", {c["outcome"] for c in manifest["clusters"]})

    def test_main_exits_non_zero_on_a_manifest_error_and_still_prints_it(self):
        run = self.discovering(base="", listing=(1, ""))
        real = pr.collect_fleet
        out = io.StringIO()
        with mock.patch.object(pr, "collect_fleet", lambda project=None: real(project, run=run, now=NOW)):
            with redirect_stdout(out), redirect_stderr(io.StringIO()):
                rc = pr.main([])
        self.assertEqual(rc, 1)
        self.assertIn("error", json.loads(out.getvalue()))


class CandidateSummaryTest(unittest.TestCase):
    def test_counts_candidates_per_check_and_caps_the_names(self):
        count = pr.SUMMARY_MAX_CLUSTER_NAMES + 2
        manifest = {
            "clusters": [{"name": f"c{i}", "outcome": "collected", "candidates": [{"check": "no-notifications"}]} for i in range(count)]
            + [{"name": "project/x", "outcome": "gate-failed"}]
        }
        head, _ = pr.candidate_summary(manifest)
        self.assertIn(f"{count} cluster(s) collected, 1 project(s) unread; {count} candidate(s)", head)
        self.assertIn(f"no-notifications: {count}", head)
        self.assertIn("and 2 more", head)

    def test_the_unenumerated_row_is_not_counted_as_a_project(self):
        manifest = {
            "clusters": [
                {"name": "c", "outcome": "collected", "candidates": []},
                {"name": pr.UNENUMERATED_PROJECTS_TARGET, "outcome": "gate-failed"},
            ]
        }
        self.assertEqual(
            pr.candidate_summary(manifest),
            ["1 cluster(s) collected, project list incomplete; 0 candidate(s) to report"],
        )

    def test_a_clean_fleet_is_one_line(self):
        manifest = {"clusters": [{"name": "c", "outcome": "collected", "candidates": []}]}
        self.assertEqual(pr.candidate_summary(manifest), ["1 cluster(s) collected; 0 candidate(s) to report"])


class AutopilotNodePoolChecksTest(unittest.TestCase):
    """The four node-pool checks on an Autopilot cluster: they run.

    They used to be declared `checks_not_applicable`, each with a reason saying
    the field was not there to read. It is. `clusters list` returns Autopilot
    node pools with `version`, `config.imageType`, and a `management` block
    populated -- five pools per cluster on the fleet measured 2026-09-05 -- so
    the question these checks ask does arise on Autopilot and the API answers
    it. Autopilot removes the knob, not the reading, and an audit that skips
    the read is asserting the platform's guarantee rather than verifying it.
    """

    POOL_CHECKS = ("pool-skew", "no-autoupgrade", "no-autorepair", "stale-image-type")

    def collect(self, *, autopilot, server_config_rc=0, node_pools=None, master="1.30.5-gke.100"):
        responses = {
            "clusters list": run_of(0, json.dumps([cluster(autopilot=autopilot, master=master, node_pools=node_pools)])),
            "get-server-config": run_of(server_config_rc, json.dumps(server_config()) if server_config_rc == 0 else "", "denied"),
        }

        def run(argv, **kwargs):
            joined = " ".join(argv)
            for needle, result in responses.items():
                if needle in joined:
                    return result
            raise AssertionError(f"unstubbed command: {joined}")

        entries = pr.collect_project("acme", run=run, now=NOW)
        entry = entries[0]
        # No cluster shape rules a check out, so the collector declares none
        # inapplicable, and no slug can be both run and declared.
        self.assertNotIn("checks_not_applicable", entry)
        return entry, {c["check"] for c in entry["commands"]}

    def test_autopilot_runs_all_four_and_declares_nothing_inapplicable(self):
        _, ran = self.collect(autopilot=True)
        self.assertTrue(set(self.POOL_CHECKS) <= ran)

    def test_a_standard_cluster_is_treated_identically(self):
        """The whole distinction is gone, so the two shapes now produce the
        same dispositions -- which is the claim worth pinning, because a
        divergence that reappears would reappear silently."""
        _, ap_ran = self.collect(autopilot=True)
        _, std_ran = self.collect(autopilot=False)
        self.assertEqual(ap_ran, std_ran)

    def test_the_checks_actually_read_the_autopilot_fields(self):
        """The point of running them. If GKE ever returns a pool with
        auto-upgrade off, a deprecated image, or a minor's worth of version
        skew, an Autopilot cluster is where nobody is looking -- so each of the
        three must produce a finding from data the old code declined to read.
        """
        pools = [pool(
            "ap-pool-1",
            version="1.28.5-gke.100",  # two minors behind the 1.30.5 control plane
            auto_upgrade=False,
            auto_repair=False,
            image_type="UBUNTU",  # deprecated, and absent from validImageTypes
        )]
        entry, _ = self.collect(autopilot=True, node_pools=pools)
        found = {c["check"] for c in entry["candidates"]}
        self.assertTrue({"pool-skew", "no-autoupgrade", "no-autorepair", "stale-image-type"} <= found)

    def test_a_healthy_autopilot_cluster_still_reports_nothing(self):
        """Running the checks must not manufacture findings out of the normal
        Autopilot shape -- otherwise this trades four honest n/a entries for
        four per-cluster false positives across the fleet."""
        entry, _ = self.collect(autopilot=True)
        found = {c["check"] for c in entry["candidates"]}
        self.assertEqual(found & set(self.POOL_CHECKS), set())

    def test_an_autopilot_rollout_one_patch_behind_is_not_a_finding(self):
        """The one way an Autopilot pool innocently trails its control plane:
        Google upgrades the control plane first and rolls nodes over the days
        after. `check_pool_skew` already excluded that case for Standard; it is
        what makes running the check on Autopilot safe rather than noisy."""
        pools = [pool("ap-pool-1", version="1.30.4-gke.100")]
        entry, _ = self.collect(autopilot=True, node_pools=pools)
        self.assertNotIn("pool-skew", {c["check"] for c in entry["candidates"]})

    def test_a_missing_baseline_is_a_gap_for_both_baseline_checks(self):
        """`stale-image-type` needs the location roster to judge an image
        against, so a failed `get-server-config` leaves it unread -- a real
        coverage gap, and no longer excused as inapplicable."""
        _, ran = self.collect(autopilot=True, server_config_rc=1)
        self.assertNotIn("stale-image-type", ran)

    def test_master_behind_stays_a_real_check_on_autopilot(self):
        """An Autopilot control plane has a version like any other, and when
        the baseline fails it is a genuine gap, not something to excuse."""
        _, ran = self.collect(autopilot=True)
        self.assertIn("master-behind", ran)
        _, ran_no_baseline = self.collect(autopilot=True, server_config_rc=1)
        self.assertNotIn("master-behind", ran_no_baseline)

    def test_the_run_list_accounts_for_the_whole_roster(self):
        """An Autopilot cluster owes a disposition for all ten checks, and now
        every one of them is a check that ran rather than one excused."""
        _, ran = self.collect(autopilot=True)
        roster = set(audit_report.AUDITS["security-patch-orchestrator"].checks)
        self.assertEqual(roster - ran, set())
        self.assertEqual(roster, ran)


class ManifestComposesWithAuditReportTest(unittest.TestCase):
    def test_checks_run_copied_from_a_collected_cluster_survives_cross_check(self):
        c = cluster()
        responses = {
            # A full discovery rather than `--project`, whose unenumerated row
            # would owe the document a scope.skipped entry of its own.
            "config get-value project": run_of(0, "acme\n"),
            "projects list": run_of(0, "acme\n"),
            "clusters list": run_of(0, json.dumps([c])),
            "get-server-config": run_of(0, json.dumps(server_config())),
        }

        def run(argv, **kwargs):
            joined = " ".join(argv)
            for needle, result in responses.items():
                if needle in joined:
                    return result
            raise AssertionError(joined)

        manifest = pr.collect_fleet(run=run, now=NOW)
        entry = manifest["clusters"][0]
        data = {
            "audit": "security-patch-orchestrator",
            "scope": {"clusters": [{"name": entry["name"], "checks_run": [{"check": c["check"], "command": c["command"]} for c in entry["commands"]]}]},
        }
        audit_report.cross_check_manifest(data, manifest)  # must not raise

    def test_a_check_absent_from_the_manifest_is_rejected(self):
        c = cluster()
        responses = {
            # A full discovery rather than `--project`, whose unenumerated row
            # would owe the document a scope.skipped entry of its own.
            "config get-value project": run_of(0, "acme\n"),
            "projects list": run_of(0, "acme\n"),
            "clusters list": run_of(0, json.dumps([c])),
            "get-server-config": run_of(1, "", "denied"),
        }

        def run(argv, **kwargs):
            joined = " ".join(argv)
            for needle, result in responses.items():
                if needle in joined:
                    return result
            raise AssertionError(joined)

        manifest = pr.collect_fleet(run=run, now=NOW)
        entry = manifest["clusters"][0]
        data = {
            "audit": "security-patch-orchestrator",
            "scope": {"clusters": [{"name": entry["name"], "checks_run": [{"check": "master-behind", "command": "x"}]}]},
        }
        with self.assertRaises(audit_report.ValidationError):
            audit_report.cross_check_manifest(data, manifest)


if __name__ == "__main__":
    unittest.main()
