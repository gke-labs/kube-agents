#!/usr/bin/env python3
"""scope_exclude: which clusters the credential broker refuses, read from scope.json."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

import scope_exclude
from scope_exclude import RULE_EXCLUDE_UNREADABLE, RULE_EXCLUDED_CLUSTER, ExcludedCluster, Exclusions

EXCLUDED = {"projectId": "payments-staging", "location": "us-central1", "clusterName": "scratch"}


def rendered(*clusters: dict) -> dict:
    """A scope.json as the operator renders it: every key present, lists sorted."""
    return {
        "present": True, "projects": ["payments-staging"], "folders": [], "organizations": [],
        "sharedVpcHosts": [], "metricsScopes": [], "maxProjects": 100,
        "exclude": {"projects": [], "clusters": list(clusters)},
    }


class ExclusionsTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.path = Path(self.temp_dir.name) / "scope.json"

    def write(self, document) -> None:
        text = document if isinstance(document, str) else json.dumps(document)
        self.path.write_text(text, encoding="utf-8")
        # A distinct mtime for each write, so a rewrite inside one clock tick
        # still reads as a change, as a ConfigMap update's new file does.
        stamp = self.path.stat().st_mtime_ns + 1_000_000 * (1 + len(text))
        os.utime(self.path, ns=(stamp, stamp))

    def exclusions(self) -> Exclusions:
        return Exclusions(path=lambda: str(self.path))

    def refused(self, exclusions: Exclusions, project: str, location: str, cluster: str) -> ExcludedCluster:
        with self.assertRaises(ExcludedCluster) as caught:
            exclusions.check(project, location, cluster)
        return caught.exception

    def test_the_excluded_cluster_is_refused_by_its_full_triple(self):
        self.write(rendered(EXCLUDED))
        exc = self.refused(self.exclusions(), "payments-staging", "us-central1", "scratch")
        self.assertEqual(RULE_EXCLUDED_CLUSTER, exc.rule)
        self.assertIn("scratch", str(exc))

    def test_a_cluster_differing_in_any_one_part_is_not_excluded(self):
        self.write(rendered(EXCLUDED))
        exclusions = self.exclusions()
        exclusions.check("payments-staging", "us-central1", "scratch-2")
        exclusions.check("payments-prod", "us-central1", "scratch")
        # A zonal name never matches a regional entry: GKE gives each cluster
        # one location, and the entry names that one.
        exclusions.check("payments-staging", "us-central1-a", "scratch")

    def test_no_exclusions_admit_every_cluster_even_by_project_number(self):
        self.write(rendered())
        self.exclusions().check("123456789012", "us-central1", "scratch")

    def test_a_project_number_is_refused_while_any_cluster_is_excluded(self):
        self.write(rendered(EXCLUDED))
        exc = self.refused(self.exclusions(), "123456789012", "us-central1", "scratch")
        self.assertEqual(RULE_EXCLUDED_CLUSTER, exc.rule)
        self.assertIn("project ID", str(exc))

    def test_an_entry_spelled_by_project_number_refuses_that_name_in_any_project(self):
        self.write(rendered(dict(EXCLUDED, projectId="123456789012")))
        exclusions = self.exclusions()
        self.refused(exclusions, "payments-staging", "us-central1", "scratch")
        self.refused(exclusions, "payments-prod", "us-central1", "scratch")
        exclusions.check("payments-staging", "us-central1", "other")

    def test_an_unset_variable_excludes_nothing(self):
        Exclusions(path=lambda: "").check("payments-staging", "us-central1", "scratch")

    def test_a_missing_file_refuses_every_cluster(self):
        exc = self.refused(self.exclusions(), "payments-prod", "us-central1", "any")
        self.assertEqual(RULE_EXCLUDE_UNREADABLE, exc.rule)

    def test_a_malformed_document_refuses_every_cluster(self):
        for document in (
            "{not json",
            [],
            {"exclude": []},
            {"exclude": {"clusters": {}}},
            {"exclude": {"clusters": ["payments-staging/us-central1/scratch"]}},
            {"exclude": {"clusters": [{"projectId": "payments-staging", "location": "us-central1"}]}},
            {"exclude": {"clusters": [dict(EXCLUDED, clusterName="Scratch")]}},
        ):
            with self.subTest(document=document):
                self.write(document)
                exc = self.refused(self.exclusions(), "payments-prod", "us-central1", "any")
                self.assertEqual(RULE_EXCLUDE_UNREADABLE, exc.rule)

    def test_a_document_without_an_exclude_block_excludes_nothing(self):
        self.write({"present": False})
        self.exclusions().check("payments-staging", "us-central1", "scratch")

    def test_an_edit_to_the_file_is_read_on_the_next_check(self):
        exclusions = self.exclusions()
        self.write(rendered())
        exclusions.check("payments-staging", "us-central1", "scratch")
        self.write(rendered(EXCLUDED))
        self.refused(exclusions, "payments-staging", "us-central1", "scratch")
        self.write(rendered())
        exclusions.check("payments-staging", "us-central1", "scratch")

    def test_parse_exclusions_returns_the_triples(self):
        self.assertEqual(
            frozenset({("payments-staging", "us-central1", "scratch")}),
            scope_exclude.parse_exclusions(rendered(EXCLUDED)),
        )


if __name__ == "__main__":
    unittest.main()
