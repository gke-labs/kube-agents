#!/usr/bin/env python3
"""scope_exclude: which clusters the credential broker refuses, read from scope.json."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

import scope_exclude
from scope_exclude import (
    RULE_EXCLUDE_UNREADABLE,
    RULE_EXCLUDED_CLUSTER,
    RULE_HELD_CLUSTER,
    RULE_HOLD_UNREADABLE,
    ExcludedCluster,
    Exclusions,
    HeldCluster,
    Holds,
)

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


class HoldsTest(unittest.TestCase):
    """spec.scope.hold: hold.json, refused through Exclusions like an exclusion."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.scope = Path(self.temp_dir.name) / "scope.json"
        self.hold = Path(self.temp_dir.name) / "hold.json"
        self.scope.write_text(json.dumps(rendered()), encoding="utf-8")

    def write(self, document) -> None:
        text = document if isinstance(document, str) else json.dumps(document)
        self.hold.write_text(text, encoding="utf-8")
        stamp = self.hold.stat().st_mtime_ns + 1_000_000 * (1 + len(text))
        os.utime(self.hold, ns=(stamp, stamp))

    def exclusions(self) -> Exclusions:
        return Exclusions(path=lambda: str(self.scope), holds=Holds(path=lambda: str(self.hold)))

    def refused(self, exclusions: Exclusions, project: str, location: str, cluster: str) -> HeldCluster:
        with self.assertRaises(HeldCluster) as caught:
            exclusions.check(project, location, cluster)
        return caught.exception

    def test_a_held_cluster_is_refused_with_the_hold_and_its_reason(self):
        self.write({"clusters": [EXCLUDED], "reason": "INC-42 rollback"})
        exclusions = self.exclusions()
        exc = self.refused(exclusions, "payments-staging", "us-central1", "scratch")
        self.assertIsInstance(exc, ExcludedCluster)
        self.assertEqual(RULE_HELD_CLUSTER, exc.rule)
        for phrase in ("spec.scope.hold", "INC-42 rollback", "report this and stop", "scratch (payments-staging, us-central1)"):
            self.assertIn(phrase, str(exc))
        exclusions.check("payments-staging", "us-central1", "other")
        exclusions.check("payments-staging", "us-central1-a", "scratch")

    def test_hold_all_refuses_every_cluster_and_every_unnamed_call(self):
        self.write({"all": True, "reason": "change freeze"})
        exclusions = self.exclusions()
        for target in (("payments-prod", "us-east1", "a"), ("123456789012", "us-central1", "b")):
            self.assertEqual(RULE_HELD_CLUSTER, self.refused(exclusions, *target).rule)
        with self.assertRaises(HeldCluster) as caught:
            exclusions.check_unnamed()
        self.assertIn("change freeze", str(caught.exception))

    def test_an_unnamed_call_is_refused_while_any_cluster_is_held(self):
        self.write({"clusters": [EXCLUDED]})
        with self.assertRaises(HeldCluster):
            self.exclusions().check_unnamed()
        self.write({})
        self.exclusions().check_unnamed()

    def test_a_project_number_is_refused_while_any_cluster_is_held(self):
        self.write({"clusters": [EXCLUDED]})
        exc = self.refused(self.exclusions(), "123456789012", "us-central1", "other")
        self.assertIn("project ID", str(exc))

    def test_an_empty_hold_or_an_unset_variable_holds_nothing(self):
        for document in ({}, {"clusters": []}, {"all": False, "reason": "lifted"}):
            with self.subTest(document=document):
                self.write(document)
                self.exclusions().check("payments-staging", "us-central1", "scratch")
        Exclusions(path=lambda: str(self.scope), holds=Holds(path=lambda: "")).check(
            "payments-staging", "us-central1", "scratch")

    def test_an_unreadable_hold_refuses_every_cluster(self):
        exc = self.refused(self.exclusions(), "payments-prod", "us-central1", "any")
        self.assertEqual(RULE_HOLD_UNREADABLE, exc.rule)
        for document in (
            "{not json",
            [],
            {"all": "yes"},
            {"reason": 7},
            {"clusters": {}},
            {"clusters": [{"projectId": "payments-staging", "location": "us-central1"}]},
            {"clusters": [dict(EXCLUDED, clusterName="Scratch")]},
        ):
            with self.subTest(document=document):
                self.write(document)
                self.assertEqual(RULE_HOLD_UNREADABLE, self.refused(self.exclusions(), "payments-prod", "us-central1", "any").rule)

    def test_a_hold_applies_and_lifts_on_the_next_check(self):
        exclusions = self.exclusions()
        self.write({})
        exclusions.check("payments-staging", "us-central1", "scratch")
        self.write({"clusters": [EXCLUDED], "reason": "INC-1"})
        self.refused(exclusions, "payments-staging", "us-central1", "scratch")
        self.write({})
        exclusions.check("payments-staging", "us-central1", "scratch")

    def test_an_exclusion_answers_before_a_hold(self):
        self.scope.write_text(json.dumps(rendered(EXCLUDED)), encoding="utf-8")
        self.write({"clusters": [EXCLUDED]})
        with self.assertRaises(ExcludedCluster) as caught:
            self.exclusions().check("payments-staging", "us-central1", "scratch")
        self.assertEqual(RULE_EXCLUDED_CLUSTER, caught.exception.rule)

    def test_the_reason_is_printable_and_capped(self):
        self.write({"clusters": [EXCLUDED], "reason": "line one\nline two\x1b[31m" + "x" * 400})
        message = str(self.refused(self.exclusions(), "payments-staging", "us-central1", "scratch"))
        self.assertNotIn("\n", message)
        self.assertNotIn("\x1b", message)
        self.assertLess(len(message), scope_exclude.HOLD_REASON_MAX_LENGTH + 300)


if __name__ == "__main__":
    unittest.main()
