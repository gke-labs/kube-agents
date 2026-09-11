#!/usr/bin/env python3
"""Unit tests for fleet_upgrade_report.py, with gcloud replaced by canned JSON."""

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))
import fleet_upgrade_report as report  # noqa: E402


def cluster(name, location, master, pools, channel="REGULAR", status="RUNNING"):
    record = {
        "name": name,
        "location": location,
        "status": status,
        "currentMasterVersion": master,
        "nodePools": [{"name": p, "version": v, "status": "RUNNING"} for p, v in pools],
    }
    if channel is not None:
        record["releaseChannel"] = {"channel": channel}
    return record


def server_config(**defaults):
    return {"channels": [{"channel": c, "defaultVersion": v, "validVersions": [v]} for c, v in defaults.items()]}


class FakeGcloud:
    """Answers `clusters list` per project and `get-server-config` per location."""

    def __init__(self, clusters_by_project, config_by_location, failing_projects=(), failing_locations=()):
        self.clusters_by_project = clusters_by_project
        self.config_by_location = config_by_location
        self.failing_projects = set(failing_projects)
        self.failing_locations = set(failing_locations)
        self.calls = []

    def __call__(self, cmd):
        self.calls.append(cmd)
        if cmd[:4] == ["gcloud", "container", "clusters", "list"]:
            project = cmd[4].split("=", 1)[1]
            if project in self.failing_projects:
                return 1, "", f"ERROR: permission denied on {project}"
            return 0, json.dumps(self.clusters_by_project.get(project, [])), ""
        if cmd[:3] == ["gcloud", "container", "get-server-config"]:
            location = cmd[3].split("=", 1)[1]
            if location in self.failing_locations:
                return 1, "", f"ERROR: location {location} unavailable"
            return 0, json.dumps(self.config_by_location[location]), ""
        raise AssertionError(f"unexpected command {cmd}")


class ParseVersionTest(unittest.TestCase):
    def test_parses_gke_build(self):
        self.assertEqual(report.parse_version("1.30.5-gke.1355000"), (1, 30, 5, 1355000))

    def test_missing_build_is_zero(self):
        self.assertEqual(report.parse_version("1.31.0"), (1, 31, 0, 0))

    def test_garbage_is_none(self):
        for text in (None, "", "latest", "1.30", "v1.30.1", "1.30.1-gke", "1.30.1-gke.abc", 42):
            self.assertIsNone(report.parse_version(text), text)

    def test_numeric_not_lexical_ordering(self):
        self.assertLess(report.parse_version("1.30.9-gke.1"), report.parse_version("1.30.10-gke.1"))

    def test_minor_gap(self):
        self.assertEqual(report.minor_gap((1, 32, 0, 0), (1, 30, 5, 1)), 2)
        self.assertEqual(report.minor_gap((1, 30, 0, 0), (1, 31, 0, 0)), -1)
        self.assertIsNone(report.minor_gap((2, 0, 0, 0), (1, 31, 0, 0)))


class ExplicitTargetTest(unittest.TestCase):
    TARGET = "1.31.4-gke.1183000"

    def _run(self, clusters):
        fake = FakeGcloud({"p1": clusters}, {})
        with patch.object(report, "run_cmd", fake):
            result = report.build_report(["p1"], self.TARGET)
        self.assertFalse(any(c[1:3] == ["container", "get-server-config"] for c in fake.calls), "explicit target must not read the server config")
        return result

    def _member(self, clusters):
        result = self._run(clusters)
        self.assertEqual(len(result["members"]), 1)
        return result["members"][0]

    def test_lagging_control_plane(self):
        m = self._member([cluster("a", "us-central1", "1.30.5-gke.1355000", [("default-pool", "1.30.5-gke.1355000")])])
        self.assertEqual(m["status"], report.STATUS_LAGGING)
        self.assertEqual(m["gap_minors"], 1)
        self.assertEqual(m["target_source"], report.TARGET_SOURCE_FLAG)
        self.assertEqual(m["target_version"], self.TARGET)

    def test_lagging_pool_on_current_control_plane(self):
        m = self._member([cluster("a", "us-central1", self.TARGET, [("fast", self.TARGET), ("slow", "1.29.8-gke.1")])])
        self.assertEqual(m["status"], report.STATUS_LAGGING)
        self.assertEqual(m["gap_minors"], 2)
        self.assertEqual(m["lowest_node_pool"]["name"], "slow")

    def test_current(self):
        m = self._member([cluster("a", "us-central1", self.TARGET, [("default-pool", self.TARGET)])])
        self.assertEqual(m["status"], report.STATUS_CURRENT)
        self.assertEqual(m["gap_minors"], 0)
        self.assertEqual(m["note"], "")

    def test_ahead_is_reported_not_flagged(self):
        m = self._member([cluster("a", "us-central1", "1.32.1-gke.1", [("default-pool", "1.32.1-gke.1")])])
        self.assertEqual(m["status"], report.STATUS_AHEAD)
        self.assertEqual(m["gap_minors"], -1)

    def test_patch_behind_on_same_minor_is_not_lagging(self):
        m = self._member([cluster("a", "us-central1", "1.31.2-gke.1", [("default-pool", "1.31.2-gke.1")])])
        self.assertEqual(m["status"], report.STATUS_PATCH_BEHIND)
        self.assertEqual(m["gap_minors"], 0)
        self.assertEqual(m["note"], "")

    def test_build_behind_on_same_patch_is_patch_behind(self):
        m = self._member([cluster("a", "us-central1", "1.31.4-gke.1027000", [("default-pool", "1.31.4-gke.1027000")])])
        self.assertEqual(m["status"], report.STATUS_PATCH_BEHIND)
        self.assertEqual(m["gap_minors"], 0)

    def test_minor_behind_pool_beats_patch_behind_control_plane(self):
        m = self._member([cluster("a", "us-central1", "1.31.2-gke.1", [("old", "1.30.9-gke.1")])])
        self.assertEqual(m["status"], report.STATUS_LAGGING)
        self.assertEqual(m["gap_minors"], 1)

    def test_control_plane_ahead_with_pool_lagging_is_lagging(self):
        m = self._member([cluster("a", "us-central1", "1.32.0-gke.1", [("old", "1.30.0-gke.1")])])
        self.assertEqual(m["status"], report.STATUS_LAGGING)
        self.assertEqual(m["gap_minors"], 1)

    def test_control_plane_ahead_with_pool_current_is_ahead_with_zero_gap(self):
        m = self._member([cluster("a", "us-central1", "1.32.0-gke.1", [("p", self.TARGET)])])
        self.assertEqual(m["status"], report.STATUS_AHEAD)
        self.assertEqual(m["gap_minors"], 0)

    def test_pool_ahead_with_control_plane_current_is_ahead_with_zero_gap(self):
        m = self._member([cluster("a", "us-central1", self.TARGET, [("p", "1.32.0-gke.1")])])
        self.assertEqual(m["status"], report.STATUS_AHEAD)
        self.assertEqual(m["gap_minors"], 0)

    def test_major_behind_is_lagging_with_undefined_gap(self):
        m = self._member([cluster("a", "us-central1", "0.99.0-gke.1", [("p", "0.99.0-gke.1")])])
        self.assertEqual(m["status"], report.STATUS_LAGGING)
        self.assertIsNone(m["gap_minors"])
        self.assertIn("major version differs", m["note"])

    def test_major_ahead_is_ahead_with_undefined_gap(self):
        m = self._member([cluster("a", "us-central1", "2.0.0-gke.1", [("p", "2.0.0-gke.1")])])
        self.assertEqual(m["status"], report.STATUS_AHEAD)
        self.assertIsNone(m["gap_minors"])
        self.assertIn("major version differs", m["note"])

    def test_unparsable_pool_is_skipped_not_masking(self):
        m = self._member([cluster("a", "us-central1", self.TARGET, [("good", "1.28.0-gke.1"), ("bad", "weird")])])
        self.assertEqual(m["status"], report.STATUS_LAGGING)
        self.assertEqual(m["gap_minors"], 3)
        self.assertEqual(m["lowest_node_pool"]["name"], "good")
        self.assertIn("bad ('weird')", m["note"])

    def test_no_parsable_pool_is_unknown(self):
        m = self._member([cluster("a", "us-central1", self.TARGET, [("bad", "weird")])])
        self.assertEqual(m["status"], report.STATUS_UNKNOWN)
        self.assertIsNone(m["lowest_node_pool"])
        self.assertIn("skipped: bad", m["note"])

    def test_unparsable_master_is_unknown(self):
        m = self._member([cluster("a", "us-central1", "weird", [("default-pool", self.TARGET)])])
        self.assertEqual(m["status"], report.STATUS_UNKNOWN)
        self.assertIsNone(m["gap_minors"])
        self.assertIn("unparsable", m["note"])

    def test_missing_node_pools_is_unknown(self):
        record = cluster("a", "us-central1", self.TARGET, [])
        del record["nodePools"]
        m = self._member([record])
        self.assertEqual(m["status"], report.STATUS_UNKNOWN)
        self.assertIn("nodePools", m["note"])

    def test_reconciling_cluster_is_noted(self):
        m = self._member([cluster("a", "us-central1", self.TARGET, [("default-pool", "1.30.1-gke.1")], status="RECONCILING")])
        self.assertEqual(m["status"], report.STATUS_LAGGING)
        self.assertIn("in flight", m["note"])

    def test_reconciling_pool_is_noted(self):
        record = cluster("a", "us-central1", self.TARGET, [("default-pool", self.TARGET)])
        record["nodePools"][0]["status"] = "RECONCILING"
        m = self._member([record])
        self.assertEqual(m["status"], report.STATUS_CURRENT)
        self.assertIn("in flight", m["note"])


class ChannelFallbackTest(unittest.TestCase):
    CONFIG = {
        "us-central1": server_config(RAPID="1.32.0-gke.1", REGULAR="1.31.0-gke.1", STABLE="1.30.0-gke.1"),
        "europe-west1": server_config(RAPID="1.32.0-gke.1", REGULAR="1.31.0-gke.1", STABLE="1.30.0-gke.1"),
    }

    def test_each_member_measured_against_its_own_channel(self):
        clusters = [
            cluster("rapid-a", "us-central1", "1.32.0-gke.1", [("p", "1.32.0-gke.1")], channel="RAPID"),
            cluster("seeded-b", "us-central1", "1.30.2-gke.1", [("p", "1.30.2-gke.1")], channel="REGULAR"),
            cluster("stable-c", "europe-west1", "1.30.0-gke.1", [("p", "1.30.0-gke.1")], channel="STABLE"),
        ]
        fake = FakeGcloud({"p1": clusters}, self.CONFIG)
        with patch.object(report, "run_cmd", fake):
            result = report.build_report(["p1"], None)
        by_name = {m["cluster"]: m for m in result["members"]}
        self.assertEqual(by_name["rapid-a"]["status"], report.STATUS_CURRENT)
        self.assertEqual(by_name["rapid-a"]["target_version"], "1.32.0-gke.1")
        self.assertEqual(by_name["rapid-a"]["target_source"], "channel default (RAPID)")
        self.assertEqual(by_name["seeded-b"]["status"], report.STATUS_LAGGING)
        self.assertEqual(by_name["seeded-b"]["gap_minors"], 1)
        self.assertEqual(by_name["seeded-b"]["target_source"], "channel default (REGULAR)")
        self.assertEqual(by_name["stable-c"]["status"], report.STATUS_CURRENT)
        self.assertEqual(by_name["stable-c"]["target_source"], "channel default (STABLE)")
        self.assertIsNone(result["target_version"])
        self.assertEqual(result["summary"], {"lagging": 1, "patch-behind": 0, "current": 2, "ahead": 0, "unknown": 0})

    def test_server_config_fetched_once_per_location(self):
        clusters = [
            cluster("a", "us-central1", "1.31.0-gke.1", [("p", "1.31.0-gke.1")]),
            cluster("b", "us-central1", "1.31.0-gke.1", [("p", "1.31.0-gke.1")]),
            cluster("c", "europe-west1", "1.31.0-gke.1", [("p", "1.31.0-gke.1")]),
        ]
        fake = FakeGcloud({"p1": clusters}, self.CONFIG)
        with patch.object(report, "run_cmd", fake):
            report.build_report(["p1"], None)
        config_calls = [c for c in fake.calls if c[1:3] == ["container", "get-server-config"]]
        self.assertEqual(len(config_calls), 2)

    def test_no_channel_is_unknown(self):
        for channel in (None, "UNSPECIFIED"):
            fake = FakeGcloud({"p1": [cluster("a", "us-central1", "1.31.0-gke.1", [("p", "1.31.0-gke.1")], channel=channel)]}, self.CONFIG)
            with patch.object(report, "run_cmd", fake):
                m = report.build_report(["p1"], None)["members"][0]
            self.assertEqual(m["status"], report.STATUS_UNKNOWN, channel)
            self.assertEqual(m["note"], "no release channel; pass --target-version", channel)
            self.assertIsNone(m["target_version"])
            self.assertEqual(m["target_source"], report.EMPTY_CELL, channel)

    def test_server_config_failure_marks_that_location_unknown(self):
        clusters = [
            cluster("ok", "us-central1", "1.31.0-gke.1", [("p", "1.31.0-gke.1")]),
            cluster("dark", "europe-west1", "1.31.0-gke.1", [("p", "1.31.0-gke.1")]),
        ]
        fake = FakeGcloud({"p1": clusters}, self.CONFIG, failing_locations=["europe-west1"])
        with patch.object(report, "run_cmd", fake):
            result = report.build_report(["p1"], None)
        by_name = {m["cluster"]: m for m in result["members"]}
        self.assertEqual(by_name["ok"]["status"], report.STATUS_CURRENT)
        self.assertEqual(by_name["dark"]["status"], report.STATUS_UNKNOWN)
        self.assertIn("get-server-config failed", by_name["dark"]["note"])
        self.assertEqual(len(result["errors"]), 1)
        self.assertEqual(result["errors"][0]["location"], "europe-west1")

    def test_channel_missing_from_server_config_is_unknown(self):
        config = {"us-central1": server_config(REGULAR="1.31.0-gke.1")}
        fake = FakeGcloud({"p1": [cluster("a", "us-central1", "1.31.0-gke.1", [("p", "1.31.0-gke.1")], channel="EXTENDED")]}, config)
        with patch.object(report, "run_cmd", fake):
            m = report.build_report(["p1"], None)["members"][0]
        self.assertEqual(m["status"], report.STATUS_UNKNOWN)
        self.assertIn("EXTENDED", m["note"])


class RunCmdTest(unittest.TestCase):
    def test_timeout_is_a_failed_read(self):
        import subprocess

        def hang(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd=args[0], timeout=kwargs["timeout"])

        with patch.object(report.subprocess, "run", hang):
            rc, out, err = report.run_cmd(["gcloud", "container", "clusters", "list"])
        self.assertEqual(rc, -1)
        self.assertEqual(out, "")
        self.assertIn(f"timed out after {report.GCLOUD_TIMEOUT_SECONDS} seconds", err)

    def test_timeout_is_passed_to_subprocess(self):
        seen = {}

        def record(*args, **kwargs):
            seen.update(kwargs)
            raise FileNotFoundError("gcloud")

        with patch.object(report.subprocess, "run", record):
            rc, _, err = report.run_cmd(["gcloud"])
        self.assertEqual(seen.get("timeout"), report.GCLOUD_TIMEOUT_SECONDS)
        self.assertEqual(rc, -1)
        self.assertIn("gcloud", err)


class ProjectFailureTest(unittest.TestCase):
    def test_one_failed_project_does_not_abort_the_others(self):
        target = "1.31.0-gke.1"
        fake = FakeGcloud(
            {"good": [cluster("a", "us-central1", target, [("p", target)])], "bad": []},
            {},
            failing_projects=["bad"],
        )
        with patch.object(report, "run_cmd", fake):
            result = report.build_report(["bad", "good"], target)
        self.assertEqual([m["cluster"] for m in result["members"]], ["a"])
        self.assertEqual(len(result["errors"]), 1)
        self.assertEqual(result["errors"][0]["project"], "bad")
        self.assertIn("permission denied", result["errors"][0]["message"])
        text = report.render_table(result)
        self.assertIn("read failed for bad", text)


class ProjectResolutionTest(unittest.TestCase):
    def test_cli_projects_win(self):
        with patch.dict(os.environ, {report.MONITORED_PROJECTS_ENV: "x,y"}):
            self.assertEqual(report.get_target_projects(["b", "a", "a"]), ["a", "b"])

    def test_env_projects_merge(self):
        env = {report.MONITORED_PROJECTS_ENV: "m1, m2,", "GCP_PROJECT_ID": "g1", "GKE_PROJECT_ID": "", "PROJECT_ID": ""}
        with patch.dict(os.environ, env, clear=False):
            self.assertEqual(report.get_target_projects(None), ["g1", "m1", "m2"])

    def test_gcloud_default_when_nothing_set(self):
        env = {report.MONITORED_PROJECTS_ENV: "", "GCP_PROJECT_ID": "", "GKE_PROJECT_ID": "", "PROJECT_ID": ""}
        with patch.dict(os.environ, env, clear=False), patch.object(report, "run_cmd", return_value=(0, "from-gcloud\n", "")):
            self.assertEqual(report.get_target_projects(None), ["from-gcloud"])


class OutputShapeTest(unittest.TestCase):
    def setUp(self):
        # main() records every run under DEFAULT_STATE_DIR; keep the tests out of /opt/data.
        self.state_dir = tempfile.mkdtemp()
        patcher = patch.object(report, "DEFAULT_STATE_DIR", self.state_dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.target = "1.31.0-gke.1"
        self.fake = FakeGcloud(
            {"p1": [
                cluster("seeded-b", "us-central1", "1.30.2-gke.1", [("default-pool", "1.30.2-gke.1")]),
                cluster("seeded-a", "us-central1", self.target, [("default-pool", self.target)]),
            ]},
            {},
        )

    def test_table_has_header_rows_and_summary(self):
        with patch.object(report, "run_cmd", self.fake):
            text = report.render_table(report.build_report(["p1"], self.target))
        lines = text.splitlines()
        self.assertEqual(lines[0], "| " + " | ".join(report.TABLE_COLUMNS) + " |")
        self.assertTrue(set(lines[1]) <= set("|- "))
        self.assertIn("| p1 | seeded-a | us-central1 | REGULAR | 1.31.0-gke.1 | 1.31.0-gke.1 (default-pool) | 1.31.0-gke.1 | 0 | current | - |", lines)
        self.assertIn("| p1 | seeded-b | us-central1 | REGULAR | 1.30.2-gke.1 | 1.30.2-gke.1 (default-pool) | 1.31.0-gke.1 | 1 | lagging | - |", lines)
        self.assertIn("2 member(s) across 1 project(s): 1 lagging, 0 patch-behind, 1 current, 0 ahead, 0 unknown; target 1.31.0-gke.1", text)

    def test_channel_default_label_in_target_column(self):
        fake = FakeGcloud({"p1": [cluster("a", "us-central1", "1.30.2-gke.1", [("p", "1.30.2-gke.1")])]}, {"us-central1": server_config(REGULAR="1.31.0-gke.1")})
        with patch.object(report, "run_cmd", fake):
            text = report.render_table(report.build_report(["p1"], None))
        self.assertIn("| 1.31.0-gke.1 channel default (REGULAR) | 1 | lagging |", text)
        self.assertIn("target: each cluster's channel default", text)

    def test_main_writes_json_and_returns_zero(self):
        out_path = os.path.join(tempfile.mkdtemp(), "nested", "fleet_versions.json")
        stdout = io.StringIO()
        with patch.object(report, "run_cmd", self.fake), redirect_stdout(stdout):
            rc = report.main(["--project", "p1", "--target-version", self.target, "--output", out_path])
        self.assertEqual(rc, report.EXIT_OK)
        with open(out_path, encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["target_version"], self.target)
        self.assertEqual(data["projects"], ["p1"])
        self.assertEqual([m["cluster"] for m in data["members"]], ["seeded-a", "seeded-b"])
        self.assertEqual(data["summary"]["lagging"], 1)
        self.assertEqual(data["errors"], [])
        member = data["members"][1]
        for key in ("project", "cluster", "location", "channel", "control_plane_version", "node_pools", "lowest_node_pool", "target_version", "target_source", "gap_minors", "status", "note"):
            self.assertIn(key, member)
        self.assertIn("Wrote 2 member(s)", stdout.getvalue())

    def test_main_returns_partial_when_a_read_failed(self):
        fake = FakeGcloud({}, {}, failing_projects=["p1"])
        with patch.object(report, "run_cmd", fake), redirect_stdout(io.StringIO()):
            rc = report.main(["--project", "p1", "--target-version", self.target])
        self.assertEqual(rc, report.EXIT_PARTIAL)

    def test_main_returns_partial_when_output_cannot_be_written(self):
        out_dir = tempfile.mkdtemp()
        # A directory where the file should go: open() fails with IsADirectoryError, an OSError.
        with patch.object(report, "run_cmd", self.fake), redirect_stdout(io.StringIO()):
            rc = report.main(["--project", "p1", "--target-version", self.target, "--output", out_dir])
        self.assertEqual(rc, report.EXIT_PARTIAL)

    def test_bad_target_version_is_usage_error(self):
        with patch.object(report, "run_cmd", self.fake), redirect_stdout(io.StringIO()):
            rc = report.main(["--project", "p1", "--target-version", "latest"])
        self.assertEqual(rc, report.EXIT_USAGE)


class ElapsedFormatTest(unittest.TestCase):
    def test_units(self):
        self.assertEqual(report.format_elapsed(0), "<1m")
        self.assertEqual(report.format_elapsed(59), "<1m")
        self.assertEqual(report.format_elapsed(45 * 60), "45m")
        self.assertEqual(report.format_elapsed(2 * 3600 + 15 * 60), "2h 15m")
        self.assertEqual(report.format_elapsed(3 * 86400 + 3600), "3d 1h")
        self.assertEqual(report.format_elapsed(-5), "<1m")


class RolloutTrackingTest(unittest.TestCase):
    """Drives main() run after run against one temp --state-dir with a moving fleet and clock."""

    TARGET = "1.31.4-gke.1183000"
    OLD = "1.30.5-gke.1355000"
    T0 = datetime(2026, 9, 11, 10, 0, tzinfo=timezone.utc)

    def setUp(self):
        self.state_dir = tempfile.mkdtemp()
        self.now = self.T0

    def _clock(self):
        return self.now

    def _run(self, clusters_by_project, *extra_args, failing_projects=(), projects=("p1",), target=TARGET):
        fake = FakeGcloud(clusters_by_project, {}, failing_projects=failing_projects)
        stdout, stderr = io.StringIO(), io.StringIO()
        argv = ["--state-dir", self.state_dir]
        for project in projects:
            argv += ["--project", project]
        if target:
            argv += ["--target-version", target]
        argv += list(extra_args)
        with patch.object(report, "run_cmd", fake), patch.object(report, "utc_now", self._clock), redirect_stdout(stdout), patch.object(sys, "stderr", stderr):
            rc = report.main(argv)
        return rc, stdout.getvalue(), stderr.getvalue()

    def _state(self, key=TARGET):
        with open(os.path.join(self.state_dir, key + ".json"), encoding="utf-8") as f:
            return json.load(f)

    @staticmethod
    def _progress_rows(text):
        rows = {}
        for line in text.splitlines():
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) == len(report.PROGRESS_COLUMNS) and cells[0] not in ("project", "---"):
                rows[cells[1]] = cells
        return rows

    def _fleet(self, a=OLD, b=OLD, c=OLD, d=TARGET, a_status="RUNNING"):
        return {"p1": [
            cluster("a", "us-central1", a, [("p", a)], status=a_status),
            cluster("b", "us-central1", b, [("p", b)]),
            cluster("c", "us-central1", c, [("p", c)]),
            cluster("d", "us-central1", d, [("p", d)]),
        ]}

    def test_first_run_records_a_baseline_and_prints_no_delta(self):
        rc, out, err = self._run(self._fleet())
        self.assertEqual(rc, report.EXIT_OK, err)
        self.assertIn("no previous run for target " + self.TARGET, out)
        self.assertIn(os.path.join(self.state_dir, self.TARGET + ".json"), out)
        self.assertEqual(self._progress_rows(out), {})
        state = self._state()
        self.assertEqual(state["format_version"], report.STATE_FORMAT_VERSION)
        self.assertEqual(state["target"], self.TARGET)
        self.assertEqual(state["recorded_at"], "2026-09-11T10:00:00Z")
        member = state["members"]["p1/us-central1/a"]
        self.assertEqual(member, {"control_plane_version": self.OLD, "lowest_node_pool_version": self.OLD, "status": "lagging", "unchanged_since": "2026-09-11T10:00:00Z"})
        self.assertEqual(state["members"]["p1/us-central1/d"]["status"], "current")

    def test_second_run_shows_the_delta_and_flags_the_stalled_member(self):
        # The acceptance criterion: one member upgraded between the runs, one moved its
        # control plane only, one did not move; the member already current stays quiet.
        self._run(self._fleet())
        self.now = self.T0 + timedelta(hours=2, minutes=15)
        fleet = self._fleet(a=self.TARGET)
        fleet["p1"][1]["currentMasterVersion"] = self.TARGET
        rc, out, err = self._run(fleet)
        self.assertEqual(rc, report.EXIT_OK, err)
        rows = self._progress_rows(out)
        self.assertEqual(rows["a"][6], "completed")
        self.assertEqual(rows["a"][3], f"{self.OLD} / {self.OLD}")
        self.assertEqual(rows["a"][4], f"{self.TARGET} / {self.TARGET}")
        self.assertEqual(rows["b"][6], "started")
        self.assertEqual(rows["b"][5], "lagging")
        self.assertEqual(rows["c"][6], "stalled (unchanged for 2h 15m)")
        self.assertEqual(rows["c"][5], "lagging")
        self.assertEqual(rows["d"][6], "unchanged")
        self.assertIn("compared with the run at 2026-09-11T10:00:00Z", out)
        self.assertIn("1 completed, 1 started, 1 stalled, 1 unchanged, 0 new; rollout active (another member moved since the previous run)", out)

    def test_no_mover_is_unchanged_not_stalled_unless_flagged(self):
        self._run(self._fleet())
        self.now = self.T0 + timedelta(minutes=30)
        _, out, _ = self._run(self._fleet())
        rows = self._progress_rows(out)
        self.assertEqual(rows["a"][6], "unchanged")
        self.assertEqual(rows["c"][6], "unchanged")
        self.assertNotIn("stalled (", out)
        self.assertIn("nothing is flagged stalled", out)
        self.now = self.T0 + timedelta(minutes=45)
        _, out, _ = self._run(self._fleet(), "--rollout-in-progress")
        rows = self._progress_rows(out)
        self.assertEqual(rows["a"][6], "stalled (unchanged for 45m)")
        self.assertEqual(rows["c"][6], "stalled (unchanged for 45m)")
        self.assertEqual(rows["d"][6], "unchanged", "a current member is never stalled")
        self.assertIn("rollout active (--rollout-in-progress)", out)

    def test_unchanged_since_is_carried_across_runs_so_elapsed_grows(self):
        self._run(self._fleet())
        self.now = self.T0 + timedelta(hours=1)
        self._run(self._fleet(a=self.TARGET))
        self.assertEqual(self._state()["members"]["p1/us-central1/c"]["unchanged_since"], "2026-09-11T10:00:00Z")
        self.assertEqual(self._state()["members"]["p1/us-central1/a"]["unchanged_since"], "2026-09-11T11:00:00Z")
        self.now = self.T0 + timedelta(days=1, hours=3)
        _, out, _ = self._run(self._fleet(a=self.TARGET, b=self.TARGET))
        rows = self._progress_rows(out)
        self.assertEqual(rows["c"][6], "stalled (unchanged for 1d 3h)")
        self.assertEqual(rows["b"][6], "completed")
        self.assertEqual(rows["a"][6], "unchanged")

    def test_in_flight_member_is_started_not_stalled(self):
        self._run(self._fleet())
        self.now = self.T0 + timedelta(hours=1)
        _, out, _ = self._run(self._fleet(a_status="RECONCILING"), "--rollout-in-progress")
        rows = self._progress_rows(out)
        self.assertEqual(rows["a"][6], "started")
        self.assertEqual(rows["c"][6], "stalled (unchanged for 1h 0m)")

    def test_patch_behind_member_can_stall_but_unknown_cannot(self):
        fleet = {"p1": [
            cluster("pb", "us-central1", "1.31.2-gke.1", [("p", "1.31.2-gke.1")]),
            cluster("unk", "us-central1", "weird", [("p", "weird")]),
            cluster("mover", "us-central1", self.OLD, [("p", self.OLD)]),
        ]}
        self._run(fleet)
        self.now = self.T0 + timedelta(hours=1)
        fleet["p1"][2] = cluster("mover", "us-central1", self.TARGET, [("p", self.TARGET)])
        _, out, _ = self._run(fleet)
        rows = self._progress_rows(out)
        self.assertEqual(rows["pb"][6], "stalled (unchanged for 1h 0m)")
        self.assertEqual(rows["unk"][6], "unchanged")
        self.assertEqual(rows["mover"][6], "completed")

    def test_status_change_without_a_version_change_restarts_the_clock(self):
        # Channel-default runs: the default advances under a current member, which is
        # then patch-behind at the same versions. Not a stall on that run; one on the next.
        config = {"us-central1": server_config(REGULAR="1.31.0-gke.1")}
        clusters = [
            cluster("x", "us-central1", "1.31.0-gke.1", [("p", "1.31.0-gke.1")]),
            cluster("mover", "us-central1", "1.30.0-gke.1", [("p", "1.30.0-gke.1")]),
        ]
        argv = ["--state-dir", self.state_dir, "--project", "p1"]
        with patch.object(report, "run_cmd", FakeGcloud({"p1": clusters}, config)), patch.object(report, "utc_now", self._clock), redirect_stdout(io.StringIO()):
            report.main(argv)
        self.assertEqual(self._state("channel-default")["members"]["p1/us-central1/x"]["status"], "current")
        self.now = self.T0 + timedelta(hours=1)
        config = {"us-central1": server_config(REGULAR="1.31.1-gke.1")}
        clusters[1] = cluster("mover", "us-central1", "1.31.1-gke.1", [("p", "1.31.1-gke.1")])
        out = io.StringIO()
        with patch.object(report, "run_cmd", FakeGcloud({"p1": clusters}, config)), patch.object(report, "utc_now", self._clock), redirect_stdout(out):
            report.main(argv)
        rows = self._progress_rows(out.getvalue())
        self.assertEqual(rows["x"][5], "patch-behind")
        self.assertEqual(rows["x"][6], "unchanged")
        self.assertEqual(rows["mover"][6], "completed")
        self.assertEqual(self._state("channel-default")["members"]["p1/us-central1/x"]["unchanged_since"], "2026-09-11T11:00:00Z")
        self.now = self.T0 + timedelta(hours=2)
        out = io.StringIO()
        with patch.object(report, "run_cmd", FakeGcloud({"p1": clusters}, config)), patch.object(report, "utc_now", self._clock), redirect_stdout(out):
            report.main(argv + ["--rollout-in-progress"])
        self.assertEqual(self._progress_rows(out.getvalue())["x"][6], "stalled (unchanged for 1h 0m)")

    def test_a_different_target_reads_a_different_record(self):
        self._run(self._fleet())
        self.now = self.T0 + timedelta(hours=1)
        other = "1.32.0-gke.1"
        _, out, _ = self._run(self._fleet(), target=other)
        self.assertIn("no previous run for target " + other, out)
        self.assertEqual(self._progress_rows(out), {})
        self.assertTrue(os.path.exists(os.path.join(self.state_dir, other + ".json")))
        self.assertEqual(self._state()["recorded_at"], "2026-09-11T10:00:00Z", "the first target's record is untouched")

    def test_member_missing_after_a_failed_read_is_carried_forward_then_dropped(self):
        fleet = self._fleet()
        fleet["p2"] = [cluster("far", "europe-west1", self.OLD, [("p", self.OLD)])]
        self._run(fleet, projects=("p1", "p2"))
        self.assertIn("p2/europe-west1/far", self._state()["members"])
        self.now = self.T0 + timedelta(hours=1)
        rc, out, _ = self._run(fleet, projects=("p1", "p2"), failing_projects=("p2",))
        self.assertEqual(rc, report.EXIT_PARTIAL)
        self.assertIn("- p2/europe-west1/far: in the previous record, not read this run (clusters list failed for p2); carried forward", out)
        self.assertNotIn("stalled (", out)
        self.assertEqual(self._state()["members"]["p2/europe-west1/far"]["unchanged_since"], "2026-09-11T10:00:00Z")
        # A run scoped to p1 alone did not read p2 either: still carried forward.
        self.now = self.T0 + timedelta(hours=2)
        _, out, _ = self._run(fleet, projects=("p1",))
        self.assertIn("(p2 not in this run's projects); carried forward", out)
        self.assertIn("p2/europe-west1/far", self._state()["members"])
        # A clean read of p2 without the cluster: reported once, then gone.
        self.now = self.T0 + timedelta(hours=3)
        fleet["p2"] = []
        _, out, _ = self._run(fleet, projects=("p1", "p2"))
        self.assertIn("- p2/europe-west1/far: in the previous record, not in this run's cluster list; dropped from the record", out)
        self.assertNotIn("p2/europe-west1/far", self._state()["members"])
        self.now = self.T0 + timedelta(hours=4)
        _, out, _ = self._run(fleet, projects=("p1", "p2"))
        self.assertNotIn("p2/europe-west1/far", out)

    def test_unwritable_state_dir_exits_partial_with_the_table_printed(self):
        blocker = os.path.join(self.state_dir, "not-a-dir")
        with open(blocker, "w", encoding="utf-8") as f:
            f.write("x")
        fake = FakeGcloud(self._fleet(), {})
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(report, "run_cmd", fake), redirect_stdout(stdout), patch.object(sys, "stderr", stderr):
            rc = report.main(["--state-dir", blocker, "--project", "p1", "--target-version", self.TARGET])
        self.assertEqual(rc, report.EXIT_PARTIAL)
        self.assertIn("| p1 | a | us-central1 |", stdout.getvalue())
        self.assertIn("failed to write the record", stderr.getvalue())

    def test_corrupt_record_is_reported_and_replaced(self):
        path = os.path.join(self.state_dir, self.TARGET + ".json")
        with open(path, "w", encoding="utf-8") as f:
            f.write("{not json")
        rc, out, err = self._run(self._fleet())
        self.assertEqual(rc, report.EXIT_OK)
        self.assertIn("unreadable, starting a new baseline", err)
        self.assertIn("no previous run for target", out)
        self.assertEqual(self._state()["format_version"], report.STATE_FORMAT_VERSION)

    def test_json_output_carries_progress_and_rollout(self):
        self._run(self._fleet())
        self.now = self.T0 + timedelta(hours=1)
        out_path = os.path.join(self.state_dir, "out", "report.json")
        rc, _, err = self._run(self._fleet(a=self.TARGET), "--output", out_path)
        self.assertEqual(rc, report.EXIT_OK, err)
        with open(out_path, encoding="utf-8") as f:
            data = json.load(f)
        by_name = {m["cluster"]: m for m in data["members"]}
        self.assertEqual(by_name["a"]["progress"], "completed")
        self.assertEqual(by_name["c"]["progress"], "stalled")
        self.assertEqual(by_name["c"]["unchanged_since"], "2026-09-11T10:00:00Z")
        self.assertEqual(by_name["c"]["unchanged_for_seconds"], 3600)
        self.assertEqual(by_name["a"]["unchanged_for_seconds"], 0)
        rollout = data["rollout"]
        self.assertEqual(rollout["state_file"], os.path.join(self.state_dir, self.TARGET + ".json"))
        self.assertEqual(rollout["previous_run_at"], "2026-09-11T10:00:00Z")
        self.assertEqual(rollout["recorded_at"], "2026-09-11T11:00:00Z")
        self.assertTrue(rollout["active"])
        self.assertEqual(rollout["active_reason"], report.ACTIVE_REASON_MOVERS)
        self.assertEqual(rollout["summary"], {"completed": 1, "started": 0, "stalled": 2, "unchanged": 1, "new": 0})
        self.assertEqual(rollout["missing_members"], [])

    def test_first_run_json_has_new_progress_and_no_previous_run(self):
        out_path = os.path.join(self.state_dir, "report.json")
        self._run(self._fleet(), "--output", out_path)
        with open(out_path, encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual({m["progress"] for m in data["members"]}, {"new"})
        self.assertIsNone(data["rollout"]["previous_run_at"])
        self.assertFalse(data["rollout"]["active"])
        self.assertEqual(data["rollout"]["summary"]["new"], 4)


if __name__ == "__main__":
    unittest.main()
