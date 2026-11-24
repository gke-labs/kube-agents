#!/usr/bin/env python3
"""cluster_metadata_scope: GCP metadata reads about a cluster held to the install's
exclusions and holds, for every caller."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import cluster_metadata_scope
from cluster_metadata_scope import filter_is_appendable, scoped_argv
from scope_exclude import (
    RULE_EXCLUDE_UNREADABLE,
    RULE_EXCLUDED_CLUSTER,
    RULE_HELD_CLUSTER,
    ExcludedCluster,
    Exclusions,
    Holds,
)

PROJECT = "payments-staging"
EXCLUDED = {"projectId": PROJECT, "location": "us-central1", "clusterName": "scratch"}
HELD = {"projectId": PROJECT, "location": "us-east1", "clusterName": "paused"}
NUMBERED = {"projectId": "123456789012", "location": "europe-west1", "clusterName": "legacy"}
EXCLUDED_CLAUSE = (
    'NOT ((resource.labels.project_id="payments-staging" AND resource.labels.location="us-central1"'
    ' AND resource.labels.cluster_name="scratch"))'
)


def scope(*clusters: dict) -> dict:
    return {"present": True, "projects": [PROJECT], "exclude": {"projects": [], "clusters": list(clusters)}}


class MetadataScopeTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.scope_path = Path(temp.name) / "scope.json"
        self.hold_path = Path(temp.name) / "hold.json"
        env = mock.patch.dict(os.environ, {cluster_metadata_scope.DEFAULT_PROJECT_ENV: PROJECT})
        env.start()
        self.addCleanup(env.stop)

    def exclusions(self, *clusters: dict, held: list | None = None, held_all: bool = False) -> Exclusions:
        self.scope_path.write_text(json.dumps(scope(*clusters)), encoding="utf-8")
        hold = ""
        if held is not None or held_all:
            self.hold_path.write_text(json.dumps({"all": held_all, "clusters": held or []}), encoding="utf-8")
            hold = str(self.hold_path)
        return Exclusions(path=lambda: str(self.scope_path), holds=Holds(path=lambda: hold))

    def refused(self, argv: list[str], exclusions: Exclusions) -> ExcludedCluster:
        with self.assertRaises(ExcludedCluster) as caught:
            scoped_argv(argv, exclusions)
        return caught.exception

    # --- container clusters describe / node-pools ------------------------------

    def test_describing_an_excluded_cluster_is_refused_in_every_spelling(self):
        exclusions = self.exclusions(EXCLUDED)
        for argv in (
            ["gcloud", "container", "clusters", "describe", "scratch", "--location", "us-central1"],
            ["gcloud", "container", "clusters", "describe", "scratch", "--region=us-central1", "--project", PROJECT],
            ["gcloud", "beta", "container", "clusters", "describe", "scratch", "--location=us-central1"],
            ["gcloud", "--project", PROJECT, "container", "clusters", "describe", "scratch", "--zone", "us-central1"],
            ["gcloud", "container", "clusters", "describe",
             f"projects/{PROJECT}/locations/us-central1/clusters/scratch"],
            # The location left to gcloud's default: refused on the name alone.
            ["gcloud", "container", "clusters", "describe", "scratch"],
        ):
            with self.subTest(argv=argv):
                exc = self.refused(argv, exclusions)
                self.assertEqual(exc.rule, RULE_EXCLUDED_CLUSTER)
                self.assertIn("scratch", str(exc))

    def test_node_pool_reads_of_an_excluded_cluster_are_refused(self):
        exclusions = self.exclusions(EXCLUDED)
        for argv in (
            ["gcloud", "container", "node-pools", "list", "--cluster", "scratch", "--location", "us-central1"],
            ["gcloud", "container", "node-pools", "describe", "default-pool", "--cluster=scratch",
             "--region", "us-central1"],
            ["gcloud", "container", "node-pools", "list", "--cluster", "scratch"],
        ):
            with self.subTest(argv=argv):
                self.assertEqual(self.refused(argv, exclusions).rule, RULE_EXCLUDED_CLUSTER)

    def test_in_scope_clusters_are_unchanged(self):
        exclusions = self.exclusions(EXCLUDED)
        for argv in (
            ["gcloud", "container", "clusters", "describe", "prod", "--location", "us-central1"],
            # Same name, another location the install doesn't exclude.
            ["gcloud", "container", "clusters", "describe", "scratch", "--location", "us-east4"],
            # Same name and location, another project.
            ["gcloud", "container", "clusters", "describe", "scratch", "--location", "us-central1",
             "--project", "other-project"],
            ["gcloud", "container", "node-pools", "list", "--cluster", "prod", "--location", "us-central1"],
            ["gcloud", "container", "clusters", "list"],
            ["gcloud", "compute", "instances", "list"],
            ["kubectl", "get", "pods"],
        ):
            with self.subTest(argv=argv):
                self.assertIs(scoped_argv(argv, exclusions), argv)

    def test_a_held_cluster_is_refused_as_held(self):
        exclusions = self.exclusions(held=[HELD])
        argv = ["gcloud", "container", "clusters", "describe", "paused", "--location", "us-east1"]
        self.assertEqual(self.refused(argv, exclusions).rule, RULE_HELD_CLUSTER)

    def test_an_entry_spelled_by_project_number_matches_the_name_in_any_project(self):
        exclusions = self.exclusions(NUMBERED)
        argv = ["gcloud", "container", "clusters", "describe", "legacy", "--location", "europe-west1"]
        self.assertEqual(self.refused(argv, exclusions).rule, RULE_EXCLUDED_CLUSTER)

    def test_a_describe_naming_no_cluster_is_refused_while_any_is_excluded(self):
        exclusions = self.exclusions(EXCLUDED)
        self.refused(["gcloud", "container", "node-pools", "list"], exclusions)

    def test_nothing_changes_on_an_install_that_excludes_and_holds_nothing(self):
        exclusions = self.exclusions()
        for argv in (
            ["gcloud", "container", "clusters", "describe", "scratch"],
            ["gcloud", "container", "node-pools", "list"],
            ["gcloud", "logging", "read", 'resource.type="k8s_container"', "--limit", "10"],
            ["gcloud", "logging", "read"],
        ):
            with self.subTest(argv=argv):
                self.assertIs(scoped_argv(argv, exclusions), argv)

    def test_an_unreadable_scope_file_refuses_a_metadata_read(self):
        self.scope_path.write_text("{not json", encoding="utf-8")
        exclusions = Exclusions(path=lambda: str(self.scope_path), holds=Holds(path=lambda: ""))
        for argv in (
            ["gcloud", "container", "clusters", "describe", "prod", "--location", "us-central1"],
            ["gcloud", "logging", "read", "severity>=ERROR"],
        ):
            with self.subTest(argv=argv):
                self.assertEqual(self.refused(argv, exclusions).rule, RULE_EXCLUDE_UNREADABLE)

    # --- logging read ---------------------------------------------------------

    def test_a_logging_filter_gets_the_exclusion_anded_on(self):
        exclusions = self.exclusions(EXCLUDED)
        argv = ["gcloud", "logging", "read", 'resource.type="k8s_container"', "--limit", "10"]
        self.assertEqual(
            scoped_argv(argv, exclusions),
            ["gcloud", "logging", "read", f'(resource.type="k8s_container") AND {EXCLUDED_CLAUSE}', "--limit", "10"],
        )

    def test_a_logging_read_with_no_filter_gets_the_clause_as_its_filter(self):
        exclusions = self.exclusions(EXCLUDED)
        self.assertEqual(
            scoped_argv(["gcloud", "logging", "read", "--freshness", "1h"], exclusions),
            ["gcloud", "logging", "read", EXCLUDED_CLAUSE, "--freshness", "1h"],
        )
        self.assertEqual(
            scoped_argv(["gcloud", "--project", PROJECT, "beta", "logging", "read"], exclusions),
            ["gcloud", "--project", PROJECT, "beta", "logging", "read", EXCLUDED_CLAUSE],
        )

    def test_adversarial_filters_can_not_shed_the_clause(self):
        # Each filter names the excluded cluster in a way a name parser could
        # miss. None is parsed: the clause is ANDed onto the whole, so whatever
        # the filter matches, the excluded cluster's entries are dropped.
        exclusions = self.exclusions(EXCLUDED)
        for text in (
            'resource.labels.cluster_name="scratch"',
            'resource.labels.cluster_name=scratch',
            'resource.labels.cluster_name:"scrat"',
            'resource.labels.cluster_name=~"^scr.*"',
            'severity>=ERROR OR resource.labels.cluster_name="scratch"',
            'labels."k8s-pod/app"="x" OR resource.labels.cluster_name="scratch"',
            '"scratch"',
            'NOT NOT resource.labels.cluster_name="scratch"',
            '(resource.labels.cluster_name="scratch") OR (severity="INFO")',
            'jsonPayload.message="a \\" quote" OR resource.labels.cluster_name="scratch"',
            "   ",
        ):
            with self.subTest(filter=text):
                out = scoped_argv(["gcloud", "logging", "read", text], exclusions)
                self.assertEqual(len(out), 4)
                self.assertTrue(out[3].endswith(f" AND {EXCLUDED_CLAUSE}") or out[3] == EXCLUDED_CLAUSE, out[3])
                if text.strip():
                    self.assertEqual(out[3], f"({text}) AND {EXCLUDED_CLAUSE}")

    def test_filters_that_could_swallow_the_clause_are_refused(self):
        # An open string or a stray parenthesis could turn the appended clause
        # into string content or regroup it, so these are refused, not parsed.
        exclusions = self.exclusions(EXCLUDED)
        for text in (
            'jsonPayload.message="unterminated',
            'severity>=ERROR) OR (resource.labels.cluster_name="scratch"',
            'severity>=ERROR) OR resource.labels.cluster_name="scratch" AND (x',
            '(severity>=ERROR',
            "resource.labels.cluster_name='scratch'",
            'jsonPayload.message="ends in an escape\\"',
        ):
            with self.subTest(filter=text):
                self.assertEqual(self.refused(["gcloud", "logging", "read", text], exclusions).rule,
                                 RULE_EXCLUDED_CLUSTER)

    def test_two_filter_positionals_are_refused_while_anything_is_excluded(self):
        exclusions = self.exclusions(EXCLUDED)
        self.refused(["gcloud", "logging", "read", "severity>=ERROR", "extra"], exclusions)

    def test_held_and_numbered_clusters_join_the_clause(self):
        exclusions = self.exclusions(EXCLUDED, NUMBERED, held=[HELD])
        out = scoped_argv(["gcloud", "logging", "read", "severity>=ERROR"], exclusions)
        clause = out[3]
        self.assertIn('resource.labels.cluster_name="scratch"', clause)
        self.assertIn('resource.labels.cluster_name="paused"', clause)
        # A number-spelled entry matches its location and name in any project:
        # entries carry the project's ID, never its number.
        self.assertIn('(resource.labels.location="europe-west1" AND resource.labels.cluster_name="legacy")', clause)
        self.assertNotIn("123456789012", clause)

    def test_holding_every_cluster_drops_every_cluster_entry(self):
        exclusions = self.exclusions(held_all=True)
        self.assertEqual(
            scoped_argv(["gcloud", "logging", "read", "severity>=ERROR"], exclusions),
            ["gcloud", "logging", "read", "(severity>=ERROR) AND NOT resource.labels.cluster_name:*"],
        )

    def test_filter_is_appendable(self):
        for text, want in (
            ('a="b"', True),
            ('a="b\\"c"', True),
            ('(a OR b) AND (c)', True),
            ('a="(not a paren"', True),
            ('a="b', False),
            ('a) OR (b', False),
            ('(a', False),
            ("a='b'", False),
        ):
            with self.subTest(text=text):
                self.assertEqual(filter_is_appendable(text), want)


if __name__ == "__main__":
    unittest.main()
