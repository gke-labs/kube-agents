"""The verification fields of a recipe.yaml, the stock probes, and the post-merge check dispatching on them.

Run: python3 -m pytest agents/platform/scripts/test_recipe_probes.py
"""

from __future__ import annotations

import json
import sqlite3
import sys
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import post_merge_check
from fix_recipes import probes, registry
from test_post_merge_check import MERGE, NOW, REPO, deployment, schema

BASE = "kind: some-fix\nname: some fixes\ntitle: Some fixes\ndial_environment: dev\n"
LIMITS = BASE + ("applied: field_equals\napplied_resource: limitranges\napplied_noun: limit range\n"
                 "applied_field: spec.limits.max\napplied_value: \"2\"\nhealth: none\n")


class VerificationFieldsTest(unittest.TestCase):
    def test_no_pdb_declares_what_was_hard_coded(self):
        recipe = registry.recipe("no-pdb")
        self.assertEqual((recipe.applied, recipe.applied_resource, recipe.applied_noun, recipe.health),
                         ("object_present", "poddisruptionbudgets.policy", "budget", "restart_flat"))

    def test_the_defaults_are_no_applied_probe_and_a_health_window(self):
        recipe = registry.parse(BASE)
        self.assertEqual((recipe.applied, recipe.health), ("", "restart_flat"))
        self.assertTrue(probes.holds_health_window(recipe))

    def test_field_equals_and_health_none_parse(self):
        recipe = registry.parse(LIMITS)
        self.assertEqual((recipe.applied_field, recipe.applied_value, recipe.health),
                         ("spec.limits.max", "2", "none"))
        self.assertFalse(probes.holds_health_window(recipe))

    def test_what_it_refuses(self):
        refused = {
            "an unknown probe": BASE + "applied: rollout_watch\n",
            "object_present with no resource": BASE + "applied: object_present\n",
            "field_equals with no value": BASE + "applied: field_equals\napplied_resource: limitranges\n"
                                                 "applied_field: spec.x\n",
            "a field for a probe that does not read it": BASE + "applied: object_present\napplied_resource: x\n"
                                                              "applied_field: spec.x\n",
            "a resource with no probe": BASE + "applied_resource: limitranges\n",
            "a noun with no probe": BASE + "applied_noun: thing\n",
            "a resource that is not a resource name": BASE + "applied: object_present\napplied_resource: Pod/x\n",
            "a field path with a space": BASE + "applied: field_equals\napplied_resource: x\n"
                                                "applied_field: \"spec. x\"\napplied_value: a\n",
            "an unknown health window": BASE + "health: rollout_watch\n",
        }
        for why, text in refused.items():
            with self.subTest(why), self.assertRaises(registry.RecipeError):
                registry.parse(text)

    def test_an_unlisted_kind_has_no_recipe(self):
        self.assertIsNone(registry.recipe("not-a-kind"))


class Cluster:
    """``kubectl_json`` from a table keyed by resource, then by object name for a single get."""

    def __init__(self, lists):
        self.lists = lists
        self.calls = []

    def __call__(self, record, argv):
        self.calls.append(argv)
        resource = argv[1]
        if len(argv) > 2 and not argv[2].startswith("-"):
            found = [item for item in self.lists.get(resource, []) if item["metadata"]["name"] == argv[2]]
            if not found:
                raise post_merge_check.WorkloadGone(f'{resource} "{argv[2]}" not found')
            return found[0]
        return {"items": self.lists.get(resource, [])}


RECORD = {"kind": "some-fix", "namespace": "shop", "object": "LimitRange/caps"}


class ProbesTest(unittest.TestCase):
    def test_object_present_by_name_when_the_record_names_no_workload(self):
        recipe = registry.parse(BASE + "applied: object_present\napplied_resource: limitranges\n")
        cluster = Cluster({"limitranges": [{"metadata": {"name": "other"}}]})
        self.assertEqual(probes.applied(recipe, cluster, RECORD, None, post_merge_check.selects), (False, "no object caps yet"))
        cluster.lists["limitranges"].append({"metadata": {"name": "caps"}})
        self.assertEqual(probes.applied(recipe, cluster, RECORD, None, post_merge_check.selects), (True, "object caps is present"))

    def test_object_present_by_selector_when_the_record_names_a_workload(self):
        recipe = registry.recipe("no-pdb")
        budget = {"metadata": {"name": "checkout-pdb"}, "spec": {"selector": {"matchLabels": {"app": "checkout"}}}}
        record = {"kind": "no-pdb", "namespace": "shop", "object": "Deployment/checkout"}
        cluster = Cluster({"poddisruptionbudgets.policy": [budget]})
        self.assertEqual(probes.applied(recipe, cluster, record, deployment(), post_merge_check.selects),
                         (True, "budget checkout-pdb selects the workload"))
        cluster.lists["poddisruptionbudgets.policy"] = []
        self.assertEqual(probes.applied(recipe, cluster, record, deployment(), post_merge_check.selects),
                         (False, "no budget selects the workload yet"))

    def test_field_equals(self):
        recipe = registry.parse(LIMITS)
        cases = (({"limits": {"max": 2}}, True), ({"limits": {"max": "2"}}, True), ({"limits": {"max": 3}}, False),
                 ({"limits": {}}, False), ({"limits": {"max": {"cpu": 2}}}, False))
        for spec, expected in cases:
            with self.subTest(spec=spec):
                cluster = Cluster({"limitranges": [{"metadata": {"name": "caps"}, "spec": spec}]})
                self.assertEqual(probes.applied(recipe, cluster, RECORD, None, post_merge_check.selects)[0], expected)
        cluster = Cluster({"limitranges": [{"metadata": {"name": "caps"}, "spec": {"limits": {"max": 3}}}]})
        self.assertEqual(probes.applied(recipe, cluster, RECORD, None, post_merge_check.selects)[1],
                         "limit range caps has spec.limits.max 3, not 2 yet")

    def test_a_kind_with_no_applied_probe_cannot_be_observed_directly(self):
        for recipe in (None, registry.parse(BASE)):
            self.assertEqual(probes.applied(recipe, Cluster({}), RECORD, None, post_merge_check.selects),
                             (False, "a some-fix fix cannot be observed directly"))


class NonWorkloadKindTest(unittest.TestCase):
    """A ``field_equals`` / ``health: none`` kind goes merge, applied, passed without reading a workload."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.conn.execute(schema())
        self.conn.execute(
            "INSERT INTO post_merge_records (url, repo, number, kind, project, location, cluster, namespace, object, "
            "opened_at, merged_at, merge_commit) VALUES (?, ?, 1, 'some-fix', 'p', 'l', 'prod', 'shop', "
            "'LimitRange/caps', ?, ?, ?)",
            (f"{REPO}/pull/1", REPO, NOW.isoformat(), NOW.isoformat(), MERGE),
        )
        patcher = mock.patch.object(post_merge_check, "_recipe", return_value=registry.parse(LIMITS))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.cluster = Cluster({"limitranges": [{"metadata": {"name": "caps"}, "spec": {"limits": {"max": 1}}}]})

    def tick(self, minutes):
        return post_merge_check.watch_health(self.conn, self.cluster, NOW + timedelta(minutes=minutes))

    def row(self):
        self.conn.row_factory = sqlite3.Row
        found = dict(self.conn.execute("SELECT * FROM post_merge_records").fetchone())
        self.conn.row_factory = None
        return found

    def test_applied_by_field_then_passed_with_no_window(self):
        self.assertEqual(self.tick(1)["waiting"], [f"{REPO}/pull/1"])
        self.assertIn("spec.limits.max 1, not 2 yet", self.row()["sync_state"])
        self.cluster.lists["limitranges"][0]["spec"]["limits"]["max"] = 2
        self.assertEqual(self.tick(2)["applied"], [f"{REPO}/pull/1"])
        self.assertEqual(json.loads(self.row()["health_baseline"]), {})
        self.assertEqual(self.tick(3)["passed"], [f"{REPO}/pull/1"])
        row = self.row()
        self.assertEqual((row["health_result"], row["health_detail"]),
                         ("passed", "no health window: some-fix declares health: none"))
        read = {argv[1] for argv in self.cluster.calls}
        self.assertFalse(read & {"deployments.apps", "statefulsets.apps", "pods"}, read)

    def test_a_restart_flat_kind_naming_a_non_workload_cannot_be_judged(self):
        post_merge_check._recipe.return_value = registry.parse(
            BASE + "applied: object_present\napplied_resource: limitranges\n")
        with self.assertLogs("post_merge_check", level="WARNING"):
            self.assertEqual(self.tick(1)["failed"], [f"{REPO}/pull/1"])
        self.assertIn("not a workload the health window reads", self.row()["health_detail"])


if __name__ == "__main__":
    unittest.main()
