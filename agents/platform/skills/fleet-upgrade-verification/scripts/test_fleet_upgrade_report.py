#!/usr/bin/env python3
"""Unit tests for fleet_upgrade_report.py, with gcloud replaced by canned JSON."""

import errno
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import subprocess
from unittest import mock
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))
import fleet_upgrade_report as report  # noqa: E402
import credential_proxy_client  # noqa: E402 -- on sys.path through the reporter's SHARED_SCRIPT_DIRS

# Every resolver asks the operator's answer when a run passes neither scope flag:
# the root-owned file the sandbox writes, else KUBEAGENTS_SCOPE_DECLARED. A shell
# that carries either (the sandbox session, a developer reproducing the guard)
# would turn this suite's listing tests into refusals. The suite is about the
# collector, not the host it runs on, so both are neutralised at import.
os.environ.pop("KUBEAGENTS_SCOPE_DECLARED", None)  # module-level, on purpose
_fleet_scope_args = report.fleet_scope_args  # the collector's own import; one module, one path

_fleet_scope_args.SCOPE_DECLARED_FILE = "/nonexistent/kube-agents-sandbox/scope-declared"


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
    def test_a_cluster_filter_skips_the_other_members(self):
        target = "1.31.0-gke.1"
        fake = FakeGcloud({"p": [cluster("a", "us-central1", target, [("p", target)]), cluster("b", "us-central1", target, [("p", target)])]}, {})
        with patch.object(report, "run_cmd", fake):
            result = report.build_report(["p"], target, clusters=["b"])
        self.assertEqual([m["cluster"] for m in result["members"]], ["b"])
        with patch.object(report, "run_cmd", fake):
            everything = report.build_report(["p"], target)
        self.assertEqual([m["cluster"] for m in everything["members"]], ["a", "b"])

    def test_a_location_qualified_filter_leaves_a_same_named_twin_alone(self):
        target = "1.31.0-gke.1"
        fake = FakeGcloud({"p": [cluster("prod", "us-central1", target, [("p", target)]), cluster("prod", "europe-west1", target, [("p", target)])]}, {})
        with patch.object(report, "run_cmd", fake):
            result = report.build_report(["p"], target, clusters=["us-central1/prod"])
        self.assertEqual([(m["location"], m["cluster"]) for m in result["members"]], [("us-central1", "prod")])
        with patch.object(report, "run_cmd", fake):
            both = report.build_report(["p"], target, clusters=["prod"])
        self.assertEqual(len(both["members"]), 2)

    def test_an_unmatched_cluster_spec_is_an_error_and_exit_one_not_an_empty_report(self):
        target = "1.31.0-gke.1"
        fake = FakeGcloud({"p": [cluster("prod", "us-central1", target, [("p", target)])]}, {})
        with patch.object(report, "run_cmd", fake):
            result = report.build_report(["p"], target, clusters=["prd", "europe-west1/prod", "prod"])
        self.assertEqual([m["cluster"] for m in result["members"]], ["prod"])
        self.assertEqual(
            [(e["location"], e["cluster"], e["message"]) for e in result["errors"]],
            [
                (None, "prd", "no cluster matched --cluster 'prd' in p"),
                ("europe-west1", "prod", "no cluster matched --cluster 'europe-west1/prod' in p"),
            ],
        )
        with tempfile.TemporaryDirectory() as state_dir, patch.object(report, "run_cmd", fake), redirect_stdout(io.StringIO()) as out:
            rc = report.main(["--project", "p", "--cluster", "prd", "--target-version", target, "--state-dir", state_dir])
        self.assertEqual(rc, report.EXIT_PARTIAL)
        self.assertIn("no cluster matched --cluster 'prd' in p", out.getvalue())

    def test_an_unmatched_spec_over_a_failed_listing_says_the_listing_failed_not_no_match(self):
        """A project the run could not list may hold the cluster: the line says
        which projects were listed and which were not, never "no match" over
        a project it never read."""
        target = "1.31.0-gke.1"
        fake = FakeGcloud({"other": [cluster("a", "us-central1", target, [("p", target)])]}, {}, failing_projects=["acme"])
        with patch.object(report, "run_cmd", fake):
            result = report.build_report(["acme", "other"], target, clusters=["us-central1/prod"])
        messages = [e["message"] for e in result["errors"]]
        self.assertEqual(len(messages), 2)
        self.assertIn("permission denied on acme", messages[0])
        self.assertEqual(messages[1], "no cluster matched --cluster 'us-central1/prod' in other; acme could not be listed, so whether it is there is unknown")
        with patch.object(report, "run_cmd", fake):
            alone = report.build_report(["acme"], target, clusters=["us-central1/prod"])
        self.assertEqual([e["message"] for e in alone["errors"]][1], "--cluster 'us-central1/prod' could not be matched: acme could not be listed")
        self.assertNotIn("no cluster matched", " ".join(e["message"] for e in alone["errors"]))

    def test_a_qualified_and_a_bare_spec_naming_one_cluster_are_both_matched(self):
        target = "1.31.0-gke.1"
        fake = FakeGcloud({"p": [cluster("prod", "us-central1", target, [("p", target)])]}, {})
        with patch.object(report, "run_cmd", fake):
            result = report.build_report(["p"], target, clusters=["prod", "us-central1/prod"])
        self.assertEqual([m["cluster"] for m in result["members"]], ["prod"])
        self.assertEqual(result["errors"], [])
        with tempfile.TemporaryDirectory() as state_dir, patch.object(report, "run_cmd", fake), redirect_stdout(io.StringIO()) as out:
            rc = report.main(["--project", "p", "--cluster", "prod", "--cluster", "us-central1/prod", "--target-version", target, "--state-dir", state_dir])
        self.assertEqual(rc, report.EXIT_OK)
        self.assertNotIn("no cluster matched", out.getvalue())

    def test_a_cluster_spec_outside_the_gke_name_grammar_is_a_usage_error_before_any_read(self):
        """Each half is a GKE name, lowercase letters, digits and hyphens, never
        a hyphen first: an empty half, a second slash, whitespace from a pasted
        line, an uppercase letter or a leading hyphen can match nothing, and is
        refused with the spec as typed rather than reported as a miss."""
        target = "1.31.0-gke.1"
        fake = FakeGcloud({"p": [cluster("prod", "us-central1", target, [("p", target)])]}, {})
        for spec in ("/prod", "prod/", "", "us-central1/prod/extra", "us-central1/prod ", " prod", "us-central1/Prod", "us central1/prod", "us-central1/-a", "-us-central1/prod", "-prod"):
            with self.subTest(spec=spec):
                err = io.StringIO()
                with patch.object(report, "run_cmd", fake), redirect_stdout(io.StringIO()), redirect_stderr(err):
                    # One token, `--cluster=<spec>`: argparse reads a separate
                    # `-prod` as an option and never hands it to the parser.
                    rc = report.main(["--project", "p", f"--cluster={spec}", "--target-version", target])
                self.assertEqual(rc, report.EXIT_USAGE)
                # The line names the spec as typed, slash and all.
                self.assertIn(f"--cluster {spec!r} is neither", err.getvalue())
                self.assertEqual(fake.calls, [])
        # `/prod` is not the bare form: a direct caller is refused too, rather
        # than handed `prod` in every location.
        with patch.object(report, "run_cmd", fake), self.assertRaises(ValueError):
            report.build_report(["p"], target, clusters=["/prod"])
        self.assertEqual(fake.calls, [])

    def test_the_spec_half_grammar_is_the_brokers_gke_name_component(self):
        """One GKE name grammar in the tree: the broker's context component,
        the scoped pool's copy of it, and this file's spec half. Held equal
        character for character, anchors aside (every use here is
        `fullmatch`), and driven with the same inputs, so an edit to one fails
        here rather than admitting a half the broker refuses."""
        broker = credential_proxy_client._GKE_CONTEXT_COMPONENT
        self.assertEqual(report.CLUSTER_SPEC_HALF_RE.pattern, broker.pattern.removeprefix("^").removesuffix(r"\Z"))
        for candidate in ("abc", "a", "a-b-c", "us-east4", "0abc", "abc-", "-abc", "ABC", "a_b", "a.b", "a/b", "", "a b", "abc\n", "\nabc", "abc\r\n"):
            with self.subTest(candidate=candidate):
                self.assertEqual(
                    bool(broker.fullmatch(candidate)),
                    bool(report.CLUSTER_SPEC_HALF_RE.fullmatch(candidate)),
                    f"the broker and the reporter disagree about {candidate!r}",
                )
        self.assertIsNone(report.parse_cluster_spec("us-central1/-a"))
        self.assertIsNone(report.parse_cluster_spec("-prod"))
        self.assertEqual(report.parse_cluster_spec("us-central1/a-1"), ("us-central1", "a-1"))

    def test_rollout_in_progress_beside_cluster_is_a_usage_error_before_any_read(self):
        target = "1.31.0-gke.1"
        fake = FakeGcloud({"p": [cluster("prod", "us-central1", target, [("p", target)])]}, {})
        err = io.StringIO()
        with patch.object(report, "run_cmd", fake), redirect_stdout(io.StringIO()), redirect_stderr(err):
            rc = report.main(["--project", "p", "--cluster", "prod", "--rollout-in-progress", "--target-version", target])
        self.assertEqual(rc, report.EXIT_USAGE)
        self.assertIn(report.ROLLOUT_FLAG_NEEDS_FULL_READ, err.getvalue())
        self.assertEqual(fake.calls, [])

    def test_a_narrowed_run_leaves_the_rollout_record_alone(self):
        target = "1.31.0-gke.1"
        fake = FakeGcloud({"p": [cluster("a", "us-central1", target, [("p", target)]), cluster("b", "us-central1", target, [("p", target)])]}, {})
        with tempfile.TemporaryDirectory() as state_dir, patch.object(report, "run_cmd", fake), redirect_stdout(io.StringIO()) as out:
            rc = report.main(["--project", "p", "--target-version", target, "--state-dir", state_dir])
            self.assertEqual(rc, report.EXIT_OK)
            path = report.state_path(state_dir, target)
            before = open(path, encoding="utf-8").read()
            rc = report.main(["--project", "p", "--cluster", "us-central1/b", "--target-version", target, "--state-dir", state_dir])
            self.assertEqual(rc, report.EXIT_OK)
            self.assertEqual(open(path, encoding="utf-8").read(), before)
            self.assertIn(report.NARROWED_RUN_NOTE, out.getvalue())

    def test_a_narrowed_run_marks_its_report_and_a_full_run_does_not(self):
        """A reader of the JSON (the deprecation scan's floor, the collector's
        join) has to tell a narrowed `members[]` from a full run whose project
        held one cluster; `narrowed_to` is that mark."""
        target = "1.31.0-gke.1"
        fake = FakeGcloud({"p": [cluster("a", "us-central1", target, [("p", target)]), cluster("b", "us-central1", target, [("p", target)])]}, {})
        with tempfile.TemporaryDirectory() as tmp, patch.object(report, "run_cmd", fake), redirect_stdout(io.StringIO()):
            output = os.path.join(tmp, "r.json")
            rc = report.main(["--project", "p", "--cluster", "us-central1/b", "--cluster", "b", "--target-version", target, "--state-dir", tmp, "--output", output])
            self.assertEqual(rc, report.EXIT_OK)
            with open(output, encoding="utf-8") as handle:
                narrowed = json.load(handle)
            self.assertEqual(narrowed["narrowed_to"], ["b", "us-central1/b"])
            self.assertEqual([m["cluster"] for m in narrowed["members"]], ["b"])
            rc = report.main(["--project", "p", "--target-version", target, "--state-dir", tmp, "--output", output])
            self.assertEqual(rc, report.EXIT_OK)
            with open(output, encoding="utf-8") as handle:
                full = json.load(handle)
            self.assertIsNone(full["narrowed_to"])
            self.assertEqual([m["cluster"] for m in full["members"]], ["a", "b"])

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

    def test_api_disabled_project_is_ignored_without_error(self):
        target = "1.31.0-gke.1"

        def fake_run(cmd, *args, **kwargs):
            if "project=no-gke" in " ".join(cmd):
                return (1, "", "ERROR: (gcloud.container.clusters.list) SERVICE_DISABLED: Kubernetes Engine API has not been used in project no-gke")
            return (0, json.dumps([cluster("a", "us-central1", target, [("p", target)])]), "")

        with patch.object(report, "run_cmd", side_effect=fake_run):
            result = report.build_report(["no-gke", "good"], target)
        self.assertEqual([m["cluster"] for m in result["members"]], ["a"])
        self.assertEqual(result["errors"], [])

    def _numbered_refusal(self, own_number):
        target = "1.31.0-gke.1"

        def fake_run(cmd, *args, **kwargs):
            joined = " ".join(cmd)
            if cmd[:3] == ["gcloud", "projects", "describe"]:
                return (0, f"{own_number}\n", "")
            if "project=refused" in joined:
                return (1, "", "ERROR: (gcloud.container.clusters.list) SERVICE_DISABLED: Kubernetes Engine API "
                               "has not been used in project 111111111111 before or it is disabled.")
            return (0, json.dumps([cluster("a", "us-central1", target, [("p", target)])]), "")

        with patch.object(report, "run_cmd", side_effect=fake_run):
            return report.build_report(["refused", "good"], target)

    def test_quota_project_refusal_is_a_failed_read(self):
        """A refusal naming another project's number is an error, so its members carry forward."""
        result = self._numbered_refusal("222222222222")
        self.assertEqual([e["project"] for e in result["errors"]], ["refused"])
        self.assertIn("quota project", result["errors"][0]["message"])

    def test_own_numbered_refusal_is_ignored(self):
        result = self._numbered_refusal("111111111111")
        self.assertEqual(result["errors"], [])


class ProjectResolutionTest(unittest.TestCase):
    def test_cli_projects_win(self):
        with patch.dict(os.environ, {report.MONITORED_PROJECTS_ENV: "x,y"}):
            self.assertEqual(report.get_target_projects(["b", "a", "a"]), ["a", "b"])

    def test_env_projects_merge(self):
        env = {report.MONITORED_PROJECTS_ENV: "m1, m2,", "GCP_PROJECT_ID": "g1", "GKE_PROJECT_ID": "", "PROJECT_ID": ""}
        with patch.dict(os.environ, env, clear=False):
            self.assertEqual(report.get_target_projects(None), ["g1", "m1", "m2"])

    def test_env_projects_whitespace_and_commas(self):
        env = {report.MONITORED_PROJECTS_ENV: "m1 m2, m3", "GCP_PROJECT_ID": "g1", "GKE_PROJECT_ID": "", "PROJECT_ID": ""}
        with patch.dict(os.environ, env, clear=False):
            self.assertEqual(report.get_target_projects(None), ["g1", "m1", "m2", "m3"])

    def test_gcloud_default_when_nothing_set(self):
        env = {report.MONITORED_PROJECTS_ENV: "", "GCP_PROJECT_ID": "", "GKE_PROJECT_ID": "", "PROJECT_ID": ""}
        with patch.dict(os.environ, env, clear=False), patch.object(report, "run_cmd", return_value=(0, "from-gcloud\n", "")):
            self.assertEqual(report.get_target_projects(None), ["from-gcloud"])

    def test_projects_list_discovered_when_monitored_not_set(self):
        env = {report.MONITORED_PROJECTS_ENV: "", "GCP_PROJECT_ID": "p-host", "GKE_PROJECT_ID": "", "PROJECT_ID": ""}
        def fake_run(cmd, **kwargs):
            if "projects" in cmd and "list" in cmd:
                return (0, "p-host\np-extra\n", "")
            return (0, "", "")
        with patch.dict(os.environ, env, clear=False), patch.object(report, "run_cmd", side_effect=fake_run):
            self.assertEqual(report.get_target_projects(None), ["p-extra", "p-host"])

    def test_listing_that_omits_the_host_project_is_reported_as_filtered(self):
        env = {report.MONITORED_PROJECTS_ENV: "", "GCP_PROJECT_ID": "p-host", "GKE_PROJECT_ID": "", "PROJECT_ID": ""}
        def fake_run(cmd, **kwargs):
            if "projects" in cmd and "list" in cmd:
                return (0, "p-extra\n", "")
            return (0, "", "")
        errors: list[str] = []
        with patch.dict(os.environ, env, clear=False), patch.object(report, "run_cmd", side_effect=fake_run):
            self.assertEqual(report.get_target_projects(None, errors), ["p-extra", "p-host"])
        self.assertEqual(len(errors), 1)
        self.assertIn("did not name p-host", errors[0])

    def test_blank_monitored_projects_runs_discovery(self):
        env = {report.MONITORED_PROJECTS_ENV: " , ", "GCP_PROJECT_ID": "p-host", "GKE_PROJECT_ID": "", "PROJECT_ID": ""}
        def fake_run(cmd, **kwargs):
            if "projects" in cmd and "list" in cmd:
                return (0, "p-host\np-extra\n", "")
            return (0, "", "")
        errors: list[str] = []
        with patch.dict(os.environ, env, clear=False), patch.object(report, "run_cmd", side_effect=fake_run):
            self.assertEqual(report.get_target_projects(None, errors), ["p-extra", "p-host"])
        self.assertEqual(errors, [])

    def test_numeric_project_id_is_normalised_before_comparing_with_listing(self):
        env = {report.MONITORED_PROJECTS_ENV: "", "GCP_PROJECT_ID": "123456789012", "GKE_PROJECT_ID": "", "PROJECT_ID": ""}
        def fake_run(cmd, **kwargs):
            if cmd[:3] == ["gcloud", "projects", "describe"]:
                return (0, "p-host\n", "")
            if "projects" in cmd and "list" in cmd:
                return (0, "p-host\np-extra\n", "")
            return (0, "", "")
        errors: list[str] = []
        with patch.dict(os.environ, env, clear=False), patch.object(report, "run_cmd", side_effect=fake_run):
            self.assertEqual(report.get_target_projects(None, errors), ["p-extra", "p-host"])
        self.assertEqual(errors, [])

    def test_numeric_project_id_describe_failure_records_error_without_double_auditing(self):
        env = {report.MONITORED_PROJECTS_ENV: "", "GCP_PROJECT_ID": "123456789012", "GKE_PROJECT_ID": "", "PROJECT_ID": ""}
        def fake_run(cmd, **kwargs):
            if cmd[:3] == ["gcloud", "projects", "describe"]:
                return (1, "", "PERMISSION_DENIED: resourcemanager.projects.get denied")
            if "config" in cmd:
                return (0, "p-host\n", "")
            if "projects" in cmd and "list" in cmd:
                return (0, "p-host\np-extra\n", "")
            return (0, "", "")
        errors: list[str] = []
        with patch.dict(os.environ, env, clear=False), patch.object(report, "run_cmd", side_effect=fake_run):
            self.assertEqual(report.get_target_projects(None, errors), ["p-extra", "p-host"])
        self.assertEqual(len(errors), 1)
        self.assertIn("gcloud projects describe 123456789012", errors[0])
        self.assertIn("PERMISSION_DENIED", errors[0])
        self.assertNotIn("listing is filtered", errors[0])


class OutputShapeTest(unittest.TestCase):
    def setUp(self):
        # main() records every run under DEFAULT_STATE_DIR; keep the tests out of /opt/data.
        self.state_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.state_dir, True)
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

    def test_main_returns_partial_when_projects_list_failed(self):
        def fake_run(cmd, **kwargs):
            if "projects" in cmd and "list" in cmd:
                return (1, "", "ERROR: PERMISSION_DENIED resourcemanager.projects.list")
            if "config" in cmd:
                return (0, "p1\n", "")
            return self.fake(cmd, **kwargs)

        env = {report.MONITORED_PROJECTS_ENV: "", "GCP_PROJECT_ID": "p1", "GKE_PROJECT_ID": "", "PROJECT_ID": ""}
        stdout = io.StringIO()
        with patch.dict(os.environ, env, clear=False), patch.object(report, "run_cmd", fake_run), redirect_stdout(stdout):
            rc = report.main(["--target-version", self.target])
        self.assertEqual(rc, report.EXIT_PARTIAL)
        self.assertIn(f"read failed for {report.PROJECTS_LIST_ERROR_SCOPE}", stdout.getvalue())
        self.assertIn("PERMISSION_DENIED", stdout.getvalue())

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
        self.addCleanup(shutil.rmtree, self.state_dir, True)
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

    def test_a_member_at_the_target_that_changes_version_is_completed_not_started(self):
        # Channel-default runs: the REGULAR default moves a patch and a current member
        # follows it in its window. It is current again at new versions: a completed
        # upgrade, and a mover, not a member that has just started.
        config = {"us-central1": server_config(REGULAR="1.31.0-gke.1")}
        clusters = [
            cluster("x", "us-central1", "1.31.0-gke.1", [("p", "1.31.0-gke.1")]),
            cluster("y", "us-central1", "1.30.0-gke.1", [("p", "1.30.0-gke.1")]),
        ]
        self._run_channel_default(clusters, config)
        self.now = self.T0 + timedelta(hours=1)
        config = {"us-central1": server_config(REGULAR="1.31.1-gke.1")}
        clusters[0] = cluster("x", "us-central1", "1.31.1-gke.1", [("p", "1.31.1-gke.1")])
        out = self._run_channel_default(clusters, config)
        rows = self._progress_rows(out)
        self.assertEqual(rows["x"][5], "current")
        self.assertEqual(rows["x"][6], "completed")
        self.assertEqual(rows["y"][6], "stalled (unchanged for 1h 0m)", "x moving makes the rollout active")
        self.assertIn("1 completed, 0 started, 1 stalled, 0 unchanged, 0 new", out)
        # Explicit target: a current member that moves past it is `completed` too, and a
        # current member that did not move stays `unchanged`.
        self._run(self._fleet())
        self.now = self.T0 + timedelta(hours=2)
        rows = self._progress_rows(self._run(self._fleet(d="1.32.0-gke.1"))[1])
        self.assertEqual((rows["d"][5], rows["d"][6]), ("ahead", "completed"))
        self.now = self.T0 + timedelta(hours=3)
        rows = self._progress_rows(self._run(self._fleet(d="1.32.0-gke.1"))[1])
        self.assertEqual((rows["d"][5], rows["d"][6]), ("ahead", "unchanged"))

    def _run_channel_default(self, clusters, config, *extra_args, failing_locations=()):
        fake = FakeGcloud({"p1": clusters}, config, failing_locations=failing_locations)
        out = io.StringIO()
        with patch.object(report, "run_cmd", fake), patch.object(report, "utc_now", self._clock), redirect_stdout(out):
            report.main(["--state-dir", self.state_dir, "--project", "p1", *extra_args])
        return out.getvalue()

    def test_a_failed_server_config_read_is_not_a_move_and_keeps_the_clock(self):
        # Run 2 cannot grade anyone (get-server-config fails); run 3 must not report the
        # current member as completed, must not call the rollout active on it, and must
        # date the lagging member's stall from run 1, not run 3.
        config = {"us-central1": server_config(REGULAR="1.31.0-gke.1")}
        clusters = [
            cluster("x", "us-central1", "1.30.0-gke.1", [("p", "1.30.0-gke.1")]),
            cluster("y", "us-central1", "1.31.0-gke.1", [("p", "1.31.0-gke.1")]),
        ]
        self._run_channel_default(clusters, config)
        self.now = self.T0 + timedelta(hours=1)
        out = self._run_channel_default(clusters, config, failing_locations=["us-central1"])
        rows = self._progress_rows(out)
        self.assertEqual((rows["x"][5], rows["x"][6]), ("unknown", "unchanged"))
        self.assertEqual((rows["y"][5], rows["y"][6]), ("unknown", "unchanged"))
        self.assertNotIn("rollout active", out)
        members = self._state("channel-default")["members"]
        self.assertEqual(members["p1/us-central1/x"]["status"], "lagging", "an ungraded run keeps the last graded status")
        self.assertEqual(members["p1/us-central1/x"]["unchanged_since"], "2026-09-11T10:00:00Z")
        self.now = self.T0 + timedelta(hours=2)
        out = self._run_channel_default(clusters, config)
        rows = self._progress_rows(out)
        self.assertEqual(rows["y"][6], "unchanged")
        self.assertEqual(rows["x"][6], "unchanged")
        self.assertNotIn("rollout active", out)
        self.assertEqual(self._state("channel-default")["members"]["p1/us-central1/x"]["unchanged_since"], "2026-09-11T10:00:00Z")
        self.now = self.T0 + timedelta(hours=3)
        out = self._run_channel_default(clusters, config, "--rollout-in-progress")
        self.assertEqual(self._progress_rows(out)["x"][6], "stalled (unchanged for 3h 0m)")

    def test_an_unknown_baseline_yields_completed_only_on_a_version_change(self):
        config = {"us-central1": server_config(REGULAR="1.31.0-gke.1")}
        clusters = [
            cluster("same", "us-central1", "1.31.0-gke.1", [("p", "1.31.0-gke.1")]),
            cluster("moved", "us-central1", "1.30.0-gke.1", [("p", "1.30.0-gke.1")]),
        ]
        self._run_channel_default(clusters, config, failing_locations=["us-central1"])
        self.assertEqual(self._state("channel-default")["members"]["p1/us-central1/same"]["status"], "unknown")
        self.now = self.T0 + timedelta(hours=1)
        clusters[1] = cluster("moved", "us-central1", "1.31.0-gke.1", [("p", "1.31.0-gke.1")])
        rows = self._progress_rows(self._run_channel_default(clusters, config))
        self.assertEqual(rows["same"][6], "unchanged", "current at the same versions as an ungraded baseline is not a completion")
        self.assertEqual(rows["moved"][6], "completed")

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

@contextmanager
def owned_by(path, uid):
    """`os.stat` and `os.fstat` reporting `uid` as `path`'s owner -- on the path, and on a descriptor
    opened on it (matched by device and inode) -- the test's own fact rather than the runner's uid, real
    for every other path: the branch a mocked root takes then does not depend on who runs the suite."""
    real_stat, real_fstat = os.stat, os.fstat
    try:
        identity = os.lstat(path)
        same = (identity.st_dev, identity.st_ino)
    except FileNotFoundError:
        same = None

    def rewrite(result):
        return os.stat_result((result.st_mode, result.st_ino, result.st_dev, result.st_nlink, uid, result.st_gid, result.st_size, result.st_atime, result.st_mtime, result.st_ctime))

    def fake(target, *args, **kwargs):
        result = real_stat(target, *args, **kwargs)
        return rewrite(result) if isinstance(target, (str, bytes, os.PathLike)) and os.fspath(target) == os.fspath(path) else result

    def fake_fstat(fd):
        result = real_fstat(fd)
        return rewrite(result) if same is not None and (result.st_dev, result.st_ino) == same else result

    with patch.object(os, "stat", fake), patch.object(os, "fstat", fake_fstat):
        yield


class PrepareKubeconfigTest(unittest.TestCase):
    def test_the_default_directory_is_shared_and_a_named_one_keeps_the_umask(self):
        # The default `$HERMES_HOME/.kubeconfigs` follows the fleet-audit collectors' rule: sticky and
        # world-writable whatever the umask, widened when this uid made it at another mode, moved aside
        # and remade when another uid's, a stale kubeconfig this uid cannot write removed (root keeps
        # it). A directory `--kubeconfig-dir` names is the caller's and keeps the caller's umask.
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, True)
        with patch.dict(os.environ, {report.HERMES_HOME_ENV: root}):
            shared = report.default_kubeconfig_dir()
            path = report.kubeconfig_path(shared, "p1", "seeded-a", "us-central1-a")
            os.mkdir(shared, 0o755)
            with open(path, "w", encoding="utf-8") as f:
                f.write("stale")
            os.chmod(path, 0o400)
            mask = os.umask(0o077)
            try:
                self.assertIsNone(report.prepare_kubeconfig(shared, path))
            finally:
                os.umask(mask)
            self.assertEqual(os.stat(shared).st_mode & report.KUBECONFIG_DIR_MODE_MASK, report.KUBECONFIG_DIR_MODE)
            self.assertEqual(os.path.exists(path), os.geteuid() == 0)
            with open(path, "w", encoding="utf-8") as f:
                f.write("root's")
            with patch.object(os, "geteuid", return_value=os.stat(shared).st_uid + 1), mock.patch("sys.stderr", new_callable=io.StringIO) as err:
                self.assertIsNone(report.prepare_kubeconfig(shared, path))
            # The move-aside is said, as the shared rule says it.
            self.assertIn("moved aside to .kubeconfigs.unusable-", err.getvalue())
            self.assertFalse(os.path.exists(path))
            self.assertEqual(len([n for n in os.listdir(root) if n.startswith(".kubeconfigs.unusable-")]), 1)
            self.assertEqual(os.stat(shared).st_mode & report.KUBECONFIG_DIR_MODE_MASK, report.KUBECONFIG_DIR_MODE)
            named = os.path.join(root, "nested", "kubeconfigs")
            mask = os.umask(0o077)
            try:
                self.assertIsNone(report.prepare_kubeconfig(named, report.kubeconfig_path(named, "p1", "seeded-a", "us-central1-a")))
            finally:
                os.umask(mask)
            self.assertEqual(os.stat(named).st_mode & report.KUBECONFIG_DIR_MODE_MASK, 0o700)
            # The shared rule's note contract: a directory another uid owns in a parent this uid
            # cannot rename in is a note naming the owner, which the member's read error carries,
            # not an OSError reported as a directory creation.
            other = os.stat(shared).st_uid + 1
            with patch.object(os, "geteuid", return_value=other), patch.object(os, "rename", side_effect=PermissionError(13, "Permission denied")):
                note = report.prepare_kubeconfig(shared, path)
            self.assertIn(f"this run is uid {other}; it could not be moved aside", note)
            with patch.object(report, "prepare_kubeconfig", return_value=note):
                items, error, kubeconfig = report.read_cluster_objects({"name": "seeded-a", "location": "us-central1-a"}, "p1", shared)
            self.assertEqual((items, error, kubeconfig), (None, report.KUBECONFIG_NOT_PREPARED_FORMAT.format(note=note), path))
            # A stat the old guard would have caught: a parent this uid cannot search is a note too,
            # not a traceback out of the run.
            real_stat = os.stat

            def refusing(target, *args, **kwargs):
                if str(target) == shared:
                    raise PermissionError(13, "Permission denied")
                return real_stat(target, *args, **kwargs)

            with patch.object(os, "stat", side_effect=refusing):
                note = report.prepare_kubeconfig(shared, path)
            self.assertEqual(note, report.KUBECONFIG_NOT_READ_FORMAT.format(directory=shared, error="[Errno 13] Permission denied"))

    def test_a_root_run_keeps_the_ticks_directory_and_a_lost_rename_race_is_no_refusal(self):
        # From root's side the tick's directory is another uid's, and root keeps it rather than moving
        # the tick's files aside (mocked root shows this branch; the `os.access` keep of a stale file
        # root can write needs a real root). A rename that finds the directory already moved, another
        # run's tick in the same second having won, reads what stands there now and is no refusal.
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, True)
        with patch.dict(os.environ, {report.HERMES_HOME_ENV: root}):
            shared = report.default_kubeconfig_dir()
            path = report.kubeconfig_path(shared, "p1", "seeded-a", "us-central1-a")
            os.mkdir(shared, 0o755)
            with open(path, "w", encoding="utf-8") as f:
                f.write("the tick's")
            with patch.object(os, "geteuid", return_value=report.ROOT_UID), owned_by(shared, report.ROOT_UID + 1), mock.patch("sys.stderr", new_callable=io.StringIO) as err:
                self.assertIsNone(report.prepare_kubeconfig(shared, path))
            self.assertNotIn("moved aside", err.getvalue())
            self.assertEqual((os.stat(shared).st_mode & report.KUBECONFIG_DIR_MODE_MASK, os.path.exists(path), sorted(os.listdir(root))), (report.KUBECONFIG_DIR_MODE, True, [".kubeconfigs"]))
            # A link planted at the file's name is removed, never written through, the target untouched.
            target = os.path.join(root, "target")
            with open(target, "w", encoding="utf-8") as f:
                f.write("root's secret")
            os.chmod(target, 0o600)
            os.unlink(path)
            os.symlink(target, path)
            with patch.object(os, "geteuid", return_value=report.ROOT_UID), owned_by(shared, report.ROOT_UID + 1):
                self.assertIsNone(report.prepare_kubeconfig(shared, path))
            with open(target, encoding="utf-8") as f:
                kept = f.read()
            self.assertEqual((os.path.lexists(path), kept, os.stat(target).st_mode & 0o7777), (False, "root's secret", 0o600))
            with open(path, "w", encoding="utf-8") as f:
                f.write("the tick's")
            other = os.stat(shared).st_uid + 1
            real_rename = os.rename

            def winner_first(src, dst, *args, **kwargs):
                # The winner moved the directory and remade it between this run's stat and rename.
                real_rename(src, dst, *args, **kwargs)
                os.mkdir(src, 0o755)
                raise FileNotFoundError(errno.ENOENT, "No such file or directory")

            with patch.object(os, "geteuid", return_value=other), patch.object(os, "rename", winner_first), mock.patch("sys.stderr", new_callable=io.StringIO) as err:
                self.assertIsNone(report.prepare_kubeconfig(shared, path))
            self.assertNotIn("could not be moved aside", err.getvalue())
            [aside] = [n for n in os.listdir(root) if n.startswith(".kubeconfigs.unusable-")]
            self.assertEqual((os.path.exists(os.path.join(root, aside, os.path.basename(path))), os.stat(shared).st_mode & report.KUBECONFIG_DIR_MODE_MASK, os.path.exists(path)), (True, report.KUBECONFIG_DIR_MODE, False))

    def test_a_symlink_at_the_shared_directory_is_refused_not_followed(self):
        # As the shared rule: a symlink planted at the default directory's name is refused with a
        # note and never followed, so a root run neither chmods nor writes into its target.
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, True)
        elsewhere = os.path.join(root, "elsewhere")
        os.mkdir(elsewhere, 0o700)
        with patch.dict(os.environ, {report.HERMES_HOME_ENV: root}):
            shared = report.default_kubeconfig_dir()
            os.symlink(elsewhere, shared)
            path = report.kubeconfig_path(shared, "p1", "seeded-a", "us-central1-a")
            for euid in (os.geteuid(), report.ROOT_UID):
                with self.subTest(euid=euid), patch.object(os, "geteuid", return_value=euid):
                    note = report.prepare_kubeconfig(shared, path)
                self.assertEqual(note, report.KUBECONFIG_IS_SYMLINK_FORMAT.format(directory=shared))
                self.assertEqual((os.stat(elsewhere).st_mode & report.KUBECONFIG_DIR_MODE_MASK, os.path.islink(shared)), (0o700, True))


    def test_this_uids_private_directory_whose_mode_cannot_change_is_kept(self):
        # As the shared rule: a chmod the filesystem refuses keeps a directory this uid owns alone
        # (owner write and search, no other uid's write) at its mode, said on stderr; any other mode,
        # and another uid's directory from root's side, stay the note.
        refused = OSError(errno.EPERM, "Operation not permitted")
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, True)
        with patch.dict(os.environ, {report.HERMES_HOME_ENV: root}):
            shared = report.default_kubeconfig_dir()
            os.mkdir(shared)
            os.chmod(shared, 0o755)
            path = report.kubeconfig_path(shared, "p1", "seeded-a", "us-central1-a")
            with patch.object(os, "fchmod", side_effect=refused), redirect_stderr(io.StringIO()) as err:
                self.assertIsNone(report.prepare_kubeconfig(shared, path))
            self.assertEqual(os.stat(shared).st_mode & report.KUBECONFIG_DIR_MODE_MASK, 0o755)
            self.assertIn(report.KUBECONFIG_MODE_KEPT_FORMAT.format(directory=shared, mode="0o755", wanted=oct(report.KUBECONFIG_DIR_MODE), error=refused), err.getvalue())
            for mode in (0o777, 0o555):
                with self.subTest(mode=oct(mode)):
                    os.chmod(shared, mode)
                    with patch.object(os, "fchmod", side_effect=refused):
                        note = report.prepare_kubeconfig(shared, path)
                    self.assertEqual(note, report.KUBECONFIG_MODE_NOT_CHANGED_FORMAT.format(directory=shared, mode=oct(mode), wanted=oct(report.KUBECONFIG_DIR_MODE), error=refused))
            os.chmod(shared, 0o755)
            with patch.object(os, "geteuid", return_value=report.ROOT_UID), owned_by(shared, report.ROOT_UID + 1), patch.object(os, "fchmod", side_effect=refused):
                note = report.prepare_kubeconfig(shared, path)
            self.assertEqual(note, report.KUBECONFIG_MODE_NOT_CHANGED_FORMAT.format(directory=shared, mode="0o755", wanted=oct(report.KUBECONFIG_DIR_MODE), error=refused))


class DeclaredScopeFromTheToolTest(unittest.TestCase):
    """The declared scope the agent carries from the platform_control fleet_scope
    tool (`--scope-projects`, `--scope-unread`) is the sweep: nothing is listed,
    an unread declared project is the one note, and without the flags the
    listing runs as before."""

    ENV = {report.MONITORED_PROJECTS_ENV: "", "GCP_PROJECT_ID": "ops-mgmt", "GKE_PROJECT_ID": "", "PROJECT_ID": ""}

    def setUp(self):
        # The variable half of the operator's answer, per test as well as at
        # import; the file half is pointed at a path that does not exist above.
        env = mock.patch.dict(os.environ, {}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("KUBEAGENTS_SCOPE_DECLARED", None)
        report.declared_scope.set(None, None)

    def tearDown(self):
        os.environ.pop("KUBEAGENTS_SCOPE_DECLARED", None)
        report.declared_scope.set(None, None)

    def test_the_declared_scope_is_swept_and_nothing_is_listed(self):
        notes: list[str] = []
        report.declared_scope.set("payments-prod,ops-mgmt", "payments-staging=denied")
        with mock.patch.dict(os.environ, self.ENV), mock.patch.object(report, "run_cmd", side_effect=lambda *a, **k: (_ for _ in ()).throw(AssertionError("nothing should be listed"))):
            self.assertEqual(report.get_target_projects(None, notes), ["ops-mgmt", "payments-prod"])
        self.assertEqual(len(notes), 1)
        self.assertIn("payments-staging (denied)", notes[0])

    def test_every_declared_project_read_leaves_no_note(self):
        notes: list[str] = []
        report.declared_scope.set("ops-mgmt", "")
        with mock.patch.dict(os.environ, self.ENV), mock.patch.object(report, "run_cmd", side_effect=lambda *a, **k: (1, "", "")):
            self.assertEqual(report.get_target_projects(None, notes), ["ops-mgmt"])
        self.assertEqual(notes, [])

    def test_without_the_flags_the_listing_runs_as_before(self):
        def fake(cmd, **kwargs):
            if "projects" in cmd and "list" in cmd:
                return (0, "ops-mgmt\nother\n", "")
            return (0, "ops-mgmt\n", "")
        report.declared_scope.set(None, None)
        with mock.patch.dict(os.environ, self.ENV), mock.patch.object(report, "run_cmd", side_effect=fake):
            self.assertEqual(report.get_target_projects(None, []), ["ops-mgmt", "other"])

    def test_the_passed_scope_outranks_a_monitored_projects_variable(self):
        # An operator's MONITORED_PROJECT_IDS narrows a listing; a scope the SOP
        # passed from the fleet_scope tool is the whole scope, env or no env.
        report.declared_scope.set("ops-mgmt", None)
        with mock.patch.dict(os.environ, {**self.ENV, report.MONITORED_PROJECTS_ENV: "acme-only"}), mock.patch.object(report, "run_cmd", side_effect=lambda *a, **k: (1, "", "")):
            self.assertEqual(report.get_target_projects(None, []), ["ops-mgmt"])

    def test_a_project_outside_the_declared_scope_is_refused(self):
        errors: list[str] = []
        report.declared_scope.set("ops-mgmt,payments-prod", None)
        with mock.patch.dict(os.environ, self.ENV):
            self.assertEqual(report.get_target_projects(["payments-prod"], errors), ["payments-prod"])
            self.assertEqual(report.get_target_projects(["acme-only"], errors), [])
        self.assertTrue(any("declared scope does not list" in e for e in errors))

    def test_the_one_project_the_audit_collector_hands_on_is_the_whole_scope_the_override_needs(self):
        """The upgrade audit's collector runs this reporter per behind cluster
        as `--project <p> --scope-projects <p> --cluster ...`, the one project
        and never the sweep or the unread rows, so its recorded command does
        not grow with the fleet. That is enough here: with `--project` the
        scope is read only to pass the project through `override_error`, so
        the run proceeds on a scoped sandbox with no error and no note."""
        errors: list[str] = []
        report.declared_scope.set("payments-prod", None)
        with mock.patch.dict(os.environ, {**self.ENV, "KUBEAGENTS_SCOPE_DECLARED": "true"}), mock.patch.object(report, "run_cmd", side_effect=lambda *a, **k: (_ for _ in ()).throw(AssertionError("nothing may be read"))):
            self.assertEqual(report.get_target_projects(["payments-prod"], errors), ["payments-prod"])
        self.assertEqual(errors, [])

    def test_a_project_without_the_flags_on_a_scoped_sandbox_is_refused_before_any_read(self):
        errors: list[str] = []
        with mock.patch.dict(os.environ, {**self.ENV, "KUBEAGENTS_SCOPE_DECLARED": "true"}), mock.patch.object(report, "run_cmd", side_effect=lambda *a, **k: (_ for _ in ()).throw(AssertionError("nothing may be read"))):
            report.declared_scope.set(None, None)
            self.assertEqual(report.get_target_projects(["123456789012"], errors), [])
        self.assertIn("got no collector_args", errors[0])

    def test_a_project_number_whose_describe_fails_is_reported_as_unresolved_not_outside(self):
        errors: list[str] = []
        report.declared_scope.set("ops-mgmt", None)
        with mock.patch.dict(os.environ, self.ENV), mock.patch.object(report, "run_cmd", side_effect=lambda *a, **k: (1, "", "denied")):
            self.assertEqual(report.get_target_projects(["123456789012"], errors), [])
        self.assertTrue(any("could not be resolved" in e for e in errors), errors)
        self.assertFalse(any("declared scope does not list" in e for e in errors), errors)

    def test_without_a_declared_scope_an_unresolved_project_number_proceeds_as_before(self):
        # The refusal of an unresolved number belongs to the declared-scope
        # case alone; a checkout or an install with no scope keeps main's
        # behaviour, the raw number with the describe failure recorded.
        notes: list[str] = []
        report.declared_scope.set(None, None)
        with mock.patch.dict(os.environ, self.ENV), mock.patch.object(report, "run_cmd", side_effect=lambda *a, **k: (1, "", "denied")):
            self.assertEqual(report.get_target_projects(["123456789012"], notes), ["123456789012"])
        self.assertFalse(any("could not resolve" in n for n in notes), notes)

    def test_a_blank_project_says_so_under_a_declared_scope(self):
        errors: list[str] = []
        report.declared_scope.set("ops-mgmt", None)
        with mock.patch.dict(os.environ, self.ENV):
            self.assertEqual(report.get_target_projects([""], errors), [])
        self.assertTrue(any("no value" in e for e in errors), errors)
        # Without a declared scope the usage line main prints is the answer;
        # this remedy would name a scope the install does not have.
        report.declared_scope.set(None, None)
        errors = []
        self.assertEqual(report.get_target_projects([""], errors), [])
        self.assertEqual(errors, [])

    def test_main_hands_the_flags_to_the_resolver(self):
        # The wiring the SOP relies on: the two flags main parses reach the
        # holder the resolver reads, before anything else runs.
        with mock.patch.object(report.declared_scope, "set", side_effect=SystemExit(0)) as handed, self.assertRaises(SystemExit):
            report.main(["--scope-projects", "ops-mgmt,payments-prod", "--scope-unread", "payments-staging=denied"])
        handed.assert_called_once_with("ops-mgmt,payments-prod", "payments-staging=denied")

    def test_a_declared_scope_with_nothing_readable_sweeps_nothing_and_lists_nothing(self):
        # `--scope-unread` alone (what collector_args carries when no declared
        # project was readable) is a boundary with nothing inside it: the run
        # reports that and must not widen to MONITORED_PROJECT_IDS or the listing.
        errors: list[str] = []
        report.declared_scope.set(None, "payments-staging=denied")
        with mock.patch.dict(os.environ, {**self.ENV, report.MONITORED_PROJECTS_ENV: "acme-only"}):
            self.assertEqual(report.get_target_projects(None, errors), [])
        self.assertEqual(len(errors), 1)
        self.assertIn("no project this install could read", errors[0])

    def test_the_flags_are_parsed_from_the_command_line(self):
        proc = subprocess.run([sys.executable, report.__file__, "--help"], capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("--scope-projects", proc.stdout)
        self.assertIn("--scope-unread", proc.stdout)

if __name__ == "__main__":
    unittest.main()


class FakeReadinessCommands(FakeGcloud):
    """FakeGcloud plus `get-credentials` and `kubectl get`, recording the KUBECONFIG each ran with."""

    def __init__(self, clusters_by_project, config_by_location, objects_by_cluster, failing_kubectl=(), failing_credentials=()):
        super().__init__(clusters_by_project, config_by_location)
        self.objects_by_cluster = objects_by_cluster
        self.failing_kubectl = set(failing_kubectl)
        self.failing_credentials = set(failing_credentials)
        self.kubeconfigs = []
        self.current_cluster = None

    def __call__(self, cmd, timeout=None, env=None):
        if cmd[:4] == ["gcloud", "container", "clusters", "get-credentials"]:
            self.calls.append(cmd)
            self.current_cluster = cmd[4]
            self.kubeconfigs.append(env.get("KUBECONFIG") if env else None)
            if self.current_cluster in self.failing_credentials:
                return 1, "", "ERROR: (gcloud.container.clusters.get-credentials) forbidden"
            return 0, "", ""
        if cmd[:2] == ["kubectl", "get"]:
            self.calls.append(cmd)
            self.kubeconfigs.append(env.get("KUBECONFIG") if env else None)
            if self.current_cluster in self.failing_kubectl:
                return 1, "", "Unable to connect to the server: dial tcp: i/o timeout"
            return 0, json.dumps({"kind": "List", "items": self.objects_by_cluster.get(self.current_cluster, [])}), ""
        return super().__call__(cmd)


def k8s_workload(kind, namespace, name, replicas, labels):
    return {"kind": kind, "metadata": {"namespace": namespace, "name": name}, "spec": {"replicas": replicas, "template": {"metadata": {"labels": labels}}}}


def k8s_pdb(namespace, name, spec, expected):
    return {"kind": "PodDisruptionBudget", "metadata": {"namespace": namespace, "name": name}, "spec": spec, "status": {"expectedPods": expected, "disruptionsAllowed": 0}}


class ReadinessTest(unittest.TestCase):
    TARGET = "1.35.1-gke.1000"
    AT = "2026-09-14T15:00:00Z"

    def setUp(self):
        self.state_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.state_dir, True)
        self.kubeconfig_dir = os.path.join(tempfile.mkdtemp(), "kubeconfigs")
        self.addCleanup(shutil.rmtree, os.path.dirname(self.kubeconfig_dir), True)
        blocked = cluster("seeded-b", "us-central1-a", "1.34.11-gke.1000", [("default-pool", "1.34.11-gke.1000")])
        blocked["maintenancePolicy"] = {
            "window": {
                "dailyMaintenanceWindow": {"startTime": "03:00", "duration": "PT4H0M0S"},
                "maintenanceExclusions": {
                    "hold-the-minor-lag": {
                        "startTime": "2026-09-12T14:35:00Z",
                        "endTime": "2026-12-11T14:35:00Z",
                        "maintenanceExclusionOptions": {"scope": "NO_MINOR_UPGRADES"},
                    }
                },
            }
        }
        ready = cluster("seeded-a", "us-central1-a", self.TARGET, [("default-pool", self.TARGET)])
        autopilot = cluster("robot-host", "us-central1", self.TARGET, [("nap-1", self.TARGET)])
        autopilot["autopilot"] = {"enabled": True}
        autopilot["controlPlaneEndpointsConfig"] = {"dnsEndpointConfig": {"endpoint": "gke-abc.us-central1.gke.goog", "allowExternalTraffic": True}}
        self.clusters = {"p1": [blocked, ready, autopilot]}
        self.objects = {
            "seeded-a": [
                k8s_workload("Deployment", "shop", "web", 3, {"app": "web"}),
                k8s_pdb("shop", "web-pdb", {"maxUnavailable": 1, "selector": {"matchLabels": {"app": "web"}}}, 3),
            ],
            "robot-host": [
                k8s_workload("Deployment", "readiness-1411", "pause", 2, {"app": "pause"}),
                k8s_pdb("readiness-1411", "block-drain", {"maxUnavailable": 0, "selector": {"matchLabels": {"app": "pause"}}}, 2),
                k8s_pdb("readiness-1411", "orphan", {"maxUnavailable": 0, "selector": {"matchLabels": {"app": "gone"}}}, 0),
            ],
        }

    def _args(self, *extra):
        return ["--state-dir", self.state_dir, "--project", "p1", "--target-version", self.TARGET, *extra]

    def _run(self, fake, *extra):
        out_path = os.path.join(tempfile.mkdtemp(), "report.json")
        stdout = io.StringIO()
        with patch.object(report, "run_cmd", fake), redirect_stdout(stdout):
            rc = report.main(self._args("--readiness", "--at", self.AT, "--kubeconfig-dir", self.kubeconfig_dir, "--output", out_path, *extra))
        with open(out_path, encoding="utf-8") as f:
            data = json.load(f)
        return rc, stdout.getvalue(), data

    def test_without_readiness_no_credentials_or_kubectl_call_and_no_readiness_key(self):
        fake = FakeGcloud(self.clusters, {})  # raises on any command it does not know
        stdout = io.StringIO()
        with patch.object(report, "run_cmd", fake), redirect_stdout(stdout):
            rc = report.main(self._args())
        self.assertEqual(rc, report.EXIT_OK)
        self.assertFalse(any(c[3:4] == ["get-credentials"] or c[:1] == ["kubectl"] for c in fake.calls))
        with patch.object(report, "run_cmd", fake):
            data = report.build_report(["p1"], self.TARGET)
        self.assertNotIn("readiness", data)
        self.assertFalse(any("readiness" in m for m in data["members"]))
        self.assertNotIn("| readiness |", stdout.getvalue())

    def test_blocking_pdb_exclusion_and_autopilot_skew_in_table_and_json(self):
        fake = FakeReadinessCommands(self.clusters, {}, self.objects)
        rc, text, data = self._run(fake)
        self.assertEqual(rc, report.EXIT_OK)
        by_name = {m["cluster"]: m["readiness"] for m in data["members"]}

        host = by_name["robot-host"]
        self.assertEqual(host["status"], "blocked")
        finding = host["pdbs"]["blocking"][0]
        self.assertEqual(finding["pdb"], "readiness-1411/block-drain")
        self.assertEqual(finding["field"], "maxUnavailable: 0")
        self.assertEqual(finding["workloads"][0], {"kind": "Deployment", "namespace": "readiness-1411", "name": "pause", "replicas": 2})
        self.assertEqual(host["pdbs"]["orphan"], 1)
        self.assertFalse(host["skew"]["applicable"])
        self.assertIn("1 orphan PDB(s)", host["note"])

        b = by_name["seeded-b"]
        self.assertEqual(b["status"], "blocked")
        self.assertEqual(b["pdbs"]["blocking"], [])
        self.assertEqual(b["maintenance"]["blocking_exclusions"], ["hold-the-minor-lag"])
        self.assertEqual(b["maintenance"]["window"]["state"], "closed")
        self.assertEqual(b["maintenance"]["window"]["next_opening"], "2026-09-15T03:00Z")

        a = by_name["seeded-a"]
        self.assertEqual(a["status"], "ready")
        self.assertEqual(a["skew"]["pools"][0]["verdict"], "ok")
        self.assertEqual(data["readiness"]["summary"], {"blocked": 2, "ready": 1, "unknown": 0})
        self.assertEqual(data["readiness"]["evaluated_at"], "2026-09-14T15:00:00Z")

        lines = text.splitlines()
        self.assertIn("| " + " | ".join(report.READINESS_COLUMNS) + " |", lines)
        host_row = next(l for l in lines if l.startswith("| p1 | robot-host |") and "| blocked |" in l)
        self.assertIn("readiness-1411/block-drain (maxUnavailable: 0; Deployment readiness-1411/pause (2 replicas))", host_row)
        self.assertIn("no exclusion in effect; no maintenance window", host_row)
        self.assertIn("n/a (Autopilot", host_row)
        b_row = next(l for l in lines if l.startswith("| p1 | seeded-b |") and "| blocked |" in l)
        self.assertIn("exclusion hold-the-minor-lag (NO_MINOR_UPGRADES) blocks auto-upgrade to 1.35.1-gke.1000 until 2026-12-11T14:35Z", b_row)
        self.assertIn("window daily at 03:00Z for 4h: closed, next opening 2026-09-15T03:00Z", b_row)
        self.assertIn("Readiness at 2026-09-14T15:00:00Z: 2 blocked, 1 ready, 0 unknown", text)
        # The version table is still printed first and unchanged in shape.
        self.assertTrue(lines[0].startswith("| " + " | ".join(report.TABLE_COLUMNS)))

    def test_kubeconfig_per_target_and_dns_endpoint_only_when_allowed(self):
        fake = FakeReadinessCommands(self.clusters, {}, self.objects)
        rc, _, data = self._run(fake)
        self.assertEqual(rc, report.EXIT_OK)
        creds = [c for c in fake.calls if c[3:4] == ["get-credentials"]]
        self.assertEqual(len(creds), 3)
        by_cluster = {c[4]: c for c in creds}
        self.assertIn("--dns-endpoint", by_cluster["robot-host"])
        self.assertNotIn("--dns-endpoint", by_cluster["seeded-a"])
        self.assertNotIn("--dns-endpoint", by_cluster["seeded-b"])
        self.assertEqual(by_cluster["seeded-b"][:7], ["gcloud", "container", "clusters", "get-credentials", "seeded-b", "--location=us-central1-a", "--project=p1"])
        expected = os.path.join(self.kubeconfig_dir, "kubeconfig_p1_seeded-b_us-central1-a.yaml")
        self.assertIn(expected, fake.kubeconfigs)
        self.assertTrue(all(k and k.startswith(self.kubeconfig_dir + os.sep) for k in fake.kubeconfigs))
        self.assertTrue(os.path.isdir(self.kubeconfig_dir))
        kubectl = [c for c in fake.calls if c[:1] == ["kubectl"]]
        self.assertEqual(kubectl, [["kubectl", "get", "pdb,deploy,statefulset", "-A", "-o", "json"]] * 3)
        self.assertEqual(data["readiness"]["kubeconfig_dir"], self.kubeconfig_dir)
        self.assertEqual({m["cluster"]: m["readiness"]["kubeconfig"] for m in data["members"]}["seeded-b"], expected)

    def test_dns_endpoint_not_added_when_external_traffic_is_off(self):
        record = cluster("closed", "us-central1", self.TARGET, [("p", self.TARGET)])
        record["controlPlaneEndpointsConfig"] = {"dnsEndpointConfig": {"endpoint": "gke-abc.us-central1.gke.goog", "allowExternalTraffic": False}}
        self.assertEqual(report.dns_endpoint_args(record), [])
        record["controlPlaneEndpointsConfig"]["dnsEndpointConfig"]["allowExternalTraffic"] = True
        self.assertEqual(report.dns_endpoint_args(record), ["--dns-endpoint"])
        del record["controlPlaneEndpointsConfig"]["dnsEndpointConfig"]["endpoint"]
        self.assertEqual(report.dns_endpoint_args(record), [])

    def test_kubectl_failure_is_an_error_row_exit_1_and_the_others_are_graded(self):
        fake = FakeReadinessCommands(self.clusters, {}, self.objects, failing_kubectl=["seeded-a"])
        rc, text, data = self._run(fake)
        self.assertEqual(rc, report.EXIT_PARTIAL)
        by_name = {m["cluster"]: m["readiness"] for m in data["members"]}
        self.assertEqual(by_name["seeded-a"]["status"], "unknown")
        self.assertIsNone(by_name["seeded-a"]["pdbs"])
        self.assertIn("i/o timeout", by_name["seeded-a"]["read_error"])
        self.assertEqual(by_name["robot-host"]["status"], "blocked")
        self.assertEqual(by_name["seeded-b"]["status"], "blocked")
        self.assertEqual([e["cluster"] for e in data["errors"]], ["seeded-a"])
        self.assertIn("- read failed for p1 (us-central1-a) cluster seeded-a: kubectl get pdb,deploy,statefulset -A -o json failed (1)", text)
        self.assertIn("| read failed |", text)
        # The version row is unaffected, and the rollout record does not treat the project as unread.
        self.assertEqual({m["cluster"]: m["status"] for m in data["members"]}["seeded-a"], report.STATUS_CURRENT)
        self.assertEqual(data["rollout"]["missing_members"], [])

    def test_get_credentials_failure_is_an_error_row_and_skips_kubectl(self):
        fake = FakeReadinessCommands(self.clusters, {}, self.objects, failing_credentials=["seeded-b"])
        rc, _, data = self._run(fake)
        self.assertEqual(rc, report.EXIT_PARTIAL)
        b = next(m for m in data["members"] if m["cluster"] == "seeded-b")["readiness"]
        # The exclusion still blocks it; the PDB rule alone is unread.
        self.assertEqual(b["status"], "blocked")
        self.assertIsNone(b["pdbs"])
        self.assertIn("forbidden", b["read_error"])
        self.assertEqual(len([c for c in fake.calls if c[:1] == ["kubectl"]]), 2)

    def test_unknown_target_grades_the_pdb_rule_and_marks_the_rest_unknown(self):
        record = cluster("nochannel", "us-central1", "1.34.0-gke.1", [("p", "1.34.0-gke.1")], channel=None)
        fake = FakeReadinessCommands({"p1": [record]}, {}, {"nochannel": self.objects["seeded-a"]})
        stdout = io.StringIO()
        with patch.object(report, "run_cmd", fake), redirect_stdout(stdout):
            rc = report.main(["--state-dir", self.state_dir, "--project", "p1", "--readiness", "--kubeconfig-dir", self.kubeconfig_dir])
        self.assertEqual(rc, report.EXIT_OK)
        self.assertIn("| unknown | none |", stdout.getvalue())
        self.assertIn("no target; exclusion scope and skew not graded", stdout.getvalue())

    def test_default_kubeconfig_dir_follows_hermes_home(self):
        with patch.dict(os.environ, {"HERMES_HOME": "/home/hermes"}):
            self.assertEqual(report.default_kubeconfig_dir(), "/home/hermes/.kubeconfigs")
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(report.default_kubeconfig_dir(), "/opt/data/.kubeconfigs")
        self.assertEqual(report.kubeconfig_path("/d", "p", "../x", ""), "/d/kubeconfig_p_.._x_unset.yaml")

    def test_bad_at_is_a_usage_error_and_at_needs_readiness(self):
        fake = FakeReadinessCommands(self.clusters, {}, self.objects)
        with patch.object(report, "run_cmd", fake), redirect_stdout(io.StringIO()):
            self.assertEqual(report.main(self._args("--readiness", "--at", "tomorrow", "--kubeconfig-dir", self.kubeconfig_dir)), report.EXIT_USAGE)
            self.assertEqual(report.main(self._args("--at", self.AT)), report.EXIT_USAGE)
            self.assertEqual(report.main(self._args("--kubeconfig-dir", self.kubeconfig_dir)), report.EXIT_USAGE)
        self.assertEqual(fake.calls, [])
