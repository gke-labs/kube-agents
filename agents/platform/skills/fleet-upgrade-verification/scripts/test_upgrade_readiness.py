#!/usr/bin/env python3
"""Unit tests for upgrade_readiness.py: the PDB, maintenance and skew rules on canned objects."""

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(__file__))
import upgrade_readiness as r  # noqa: E402

AT = datetime(2026, 9, 14, 15, 0, tzinfo=timezone.utc)
TARGET_TEXT = "1.35.1-gke.1000"
TARGET = (1, 35, 1, 1000)
MASTER = (1, 34, 11, 1000)


def workload(kind, name, replicas, labels, namespace="shop"):
    return {
        "kind": kind,
        "metadata": {"namespace": namespace, "name": name},
        "spec": {"replicas": replicas, "template": {"metadata": {"labels": labels}}},
    }


def pdb(name, spec, expected=None, allowed=None, namespace="shop"):
    record = {"kind": "PodDisruptionBudget", "metadata": {"namespace": namespace, "name": name}, "spec": spec}
    status = {}
    if expected is not None:
        status["expectedPods"] = expected
    if allowed is not None:
        status["disruptionsAllowed"] = allowed
    if status:
        record["status"] = status
    return record


def pool(name, version):
    from fleet_upgrade_report import parse_version  # the report's parser, so the tuples agree
    return {"name": name, "version": version, "parsed": parse_version(version)}


def exclusion(name, scope, start=AT - timedelta(days=2), end=AT + timedelta(days=88)):
    record = {"startTime": start.strftime("%Y-%m-%dT%H:%M:%SZ"), "endTime": end.strftime("%Y-%m-%dT%H:%M:%SZ")}
    if scope is not None:
        record["maintenanceExclusionOptions"] = {"scope": scope}
    return {"window": {"maintenanceExclusions": {name: record}}}


class SelectorTest(unittest.TestCase):
    def test_match_labels(self):
        self.assertTrue(r.selector_matches({"matchLabels": {"app": "web"}}, {"app": "web", "tier": "fe"}))
        self.assertFalse(r.selector_matches({"matchLabels": {"app": "web"}}, {"app": "api"}))

    def test_match_expressions(self):
        sel = {"matchExpressions": [{"key": "app", "operator": "In", "values": ["web", "api"]}, {"key": "canary", "operator": "DoesNotExist"}]}
        self.assertTrue(r.selector_matches(sel, {"app": "api"}))
        self.assertFalse(r.selector_matches(sel, {"app": "api", "canary": "true"}))
        self.assertFalse(r.selector_matches({"matchExpressions": [{"key": "app", "operator": "NotIn", "values": ["web"]}]}, {"app": "web"}))
        self.assertTrue(r.selector_matches({"matchExpressions": [{"key": "app", "operator": "Exists"}]}, {"app": "x"}))

    def test_null_matches_nothing_and_empty_matches_everything(self):
        self.assertFalse(r.selector_matches(None, {"app": "web"}))
        self.assertTrue(r.selector_matches({}, {"app": "web"}))


class PdbGradingTest(unittest.TestCase):
    def _grade(self, *items):
        return r.grade_pdbs(*r.split_items(list(items)))

    def test_max_unavailable_zero_on_fully_scheduled_deployment_is_named(self):
        # #1343 criterion 3: the acceptance criterion.
        result = self._grade(
            workload("Deployment", "web", 3, {"app": "web"}),
            pdb("block-drain", {"maxUnavailable": 0, "selector": {"matchLabels": {"app": "web"}}}, expected=3, allowed=0),
        )
        self.assertEqual(len(result["blocking"]), 1)
        finding = result["blocking"][0]
        self.assertEqual(finding["pdb"], "shop/block-drain")
        self.assertEqual(finding["field"], "maxUnavailable: 0")
        self.assertEqual(finding["workloads"], [{"kind": "Deployment", "namespace": "shop", "name": "web", "replicas": 3}])
        self.assertEqual(finding["disruptions_allowed"], 0)
        self.assertEqual(r.describe_finding(finding), "shop/block-drain (maxUnavailable: 0; Deployment shop/web (3 replicas))")

    def test_max_unavailable_zero_percent_blocks_and_positive_percent_does_not(self):
        dep = workload("Deployment", "web", 3, {"app": "web"})
        sel = {"matchLabels": {"app": "web"}}
        self.assertEqual(self._grade(dep, pdb("p", {"maxUnavailable": "0%", "selector": sel}))["blocking"][0]["field"], "maxUnavailable: 0%")
        self.assertEqual(self._grade(dep, pdb("p", {"maxUnavailable": "10%", "selector": sel}))["blocking"], [])
        self.assertEqual(self._grade(dep, pdb("p", {"maxUnavailable": 1, "selector": sel}))["blocking"], [])

    def test_min_available_at_below_and_above_replicas(self):
        dep = workload("StatefulSet", "db", 3, {"app": "db"})
        sel = {"matchLabels": {"app": "db"}}
        self.assertEqual(self._grade(dep, pdb("p", {"minAvailable": 3, "selector": sel}))["blocking"][0]["field"], "minAvailable: 3 (>= 3 expected pods)")
        self.assertEqual(self._grade(dep, pdb("p", {"minAvailable": 2, "selector": sel}))["blocking"], [])
        self.assertEqual(self._grade(dep, pdb("p", {"minAvailable": 4, "selector": sel}))["blocking"][0]["field"], "minAvailable: 4 (>= 3 expected pods)")
        self.assertEqual(self._grade(dep, pdb("p", {"minAvailable": "3", "selector": sel}))["blocking"][0]["field"], "minAvailable: 3 (>= 3 expected pods)")

    def test_min_available_percentages_round_up_like_the_controller(self):
        sel = {"matchLabels": {"app": "web"}}
        three = workload("Deployment", "web", 3, {"app": "web"})
        self.assertEqual(self._grade(three, pdb("p", {"minAvailable": "100%", "selector": sel}))["blocking"][0]["field"], "minAvailable: 100% (rounds up to 3 of 3 expected pods)")
        self.assertEqual(self._grade(three, pdb("p", {"minAvailable": "50%", "selector": sel}))["blocking"], [])
        # 90% of nine rounds up to nine: every pod, so every drain is blocked.
        nine = workload("Deployment", "web", 9, {"app": "web"})
        self.assertEqual(self._grade(nine, pdb("p", {"minAvailable": "90%", "selector": sel}))["blocking"][0]["field"], "minAvailable: 90% (rounds up to 9 of 9 expected pods)")
        ten = workload("Deployment", "web", 10, {"app": "web"})
        self.assertEqual(self._grade(ten, pdb("p", {"minAvailable": "90%", "selector": sel}))["blocking"], [])

    def test_match_expressions_selector_finds_the_workload(self):
        dep = workload("Deployment", "web", 2, {"app": "web", "tier": "fe"})
        sel = {"matchExpressions": [{"key": "tier", "operator": "In", "values": ["fe"]}]}
        result = self._grade(dep, pdb("p", {"maxUnavailable": 0, "selector": sel}))
        self.assertEqual(result["blocking"][0]["workloads"][0]["name"], "web")

    def test_replicas_absent_means_one(self):
        dep = workload("Deployment", "web", 1, {"app": "web"})
        del dep["spec"]["replicas"]
        result = self._grade(dep, pdb("p", {"minAvailable": 1, "selector": {"matchLabels": {"app": "web"}}}))
        self.assertEqual(result["blocking"][0]["workloads"][0]["replicas"], 1)

    def test_replica_total_spans_every_matched_workload(self):
        # minAvailable 3 over two three-replica Deployments leaves three disruptions; not a blocker.
        a = workload("Deployment", "a", 3, {"team": "shop"})
        b = workload("Deployment", "b", 3, {"team": "shop"})
        sel = {"matchLabels": {"team": "shop"}}
        self.assertEqual(self._grade(a, b, pdb("p", {"minAvailable": 3, "selector": sel}))["blocking"], [])
        finding = self._grade(a, b, pdb("p", {"minAvailable": 6, "selector": sel}))["blocking"][0]
        self.assertEqual([w["name"] for w in finding["workloads"]], ["a", "b"])

    def test_namespace_is_respected(self):
        dep = workload("Deployment", "web", 3, {"app": "web"}, namespace="other")
        result = self._grade(dep, pdb("p", {"maxUnavailable": 0, "selector": {"matchLabels": {"app": "web"}}}))
        self.assertEqual(result["blocking"], [])
        self.assertEqual(result["orphan"], 1)

    def test_scaled_to_zero_and_orphan_pdbs_are_skipped_and_counted(self):
        zero = workload("Deployment", "idle", 0, {"app": "idle"})
        result = self._grade(
            zero,
            pdb("idle", {"maxUnavailable": 0, "selector": {"matchLabels": {"app": "idle"}}}, expected=0),
            pdb("no-status", {"maxUnavailable": 0, "selector": {"matchLabels": {"app": "idle"}}}),
            pdb("orphan", {"maxUnavailable": 0, "selector": {"matchLabels": {"app": "gone"}}}),
        )
        self.assertEqual(result["blocking"], [])
        self.assertEqual(result["scaled_to_zero"], 2)
        self.assertEqual(result["orphan"], 1)
        self.assertEqual(result["evaluated"], 0)

    def test_pdb_covering_pods_of_an_unread_kind_is_unmatched_not_orphan(self):
        result = self._grade(pdb("rs", {"maxUnavailable": 0, "selector": {"matchLabels": {"app": "bare"}}}, expected=2, allowed=0))
        self.assertEqual(result["unmatched"], 1)
        self.assertEqual(result["orphan"], 0)

    def test_daemonset_and_unknown_kinds_are_not_workloads(self):
        items = [
            {"kind": "DaemonSet", "metadata": {"namespace": "shop", "name": "agent"}, "spec": {"template": {"metadata": {"labels": {"app": "agent"}}}}},
            "not a dict",
            pdb("p", {"maxUnavailable": 0, "selector": {"matchLabels": {"app": "agent"}}}),
        ]
        pdbs, workloads = r.split_items(items)
        self.assertEqual(len(pdbs), 1)
        self.assertEqual(workloads, [])


class ExclusionTest(unittest.TestCase):
    POOLS = [pool("default-pool", "1.34.11-gke.1000")]

    def _one(self, policy, target_text=TARGET_TEXT, target=TARGET, master=MASTER, pools=None, at=AT):
        result = r.evaluate_maintenance(policy, at, target_text, target, master, self.POOLS if pools is None else pools)
        self.assertEqual(len(result["exclusions"]), 1)
        return result

    def test_in_effect_no_minor_upgrades_blocks_a_minor_target(self):
        result = self._one(exclusion("hold-the-minor-lag", "NO_MINOR_UPGRADES"))
        entry = result["exclusions"][0]
        self.assertTrue(entry["in_effect"])
        self.assertTrue(entry["blocks"])
        self.assertEqual(result["blocking_exclusions"], ["hold-the-minor-lag"])
        self.assertIn("blocks auto-upgrade to 1.35.1-gke.1000 until 2026-12-11T15:00Z", entry["detail"])
        self.assertIn("minor upgrade for the control plane and pool default-pool", entry["detail"])

    def test_no_minor_upgrades_does_not_block_a_patch_only_target(self):
        result = self._one(exclusion("x", "NO_MINOR_UPGRADES"), target_text="1.34.12-gke.1", target=(1, 34, 12, 1))
        self.assertFalse(result["exclusions"][0]["blocks"])
        self.assertIn("patch-only upgrade", result["exclusions"][0]["detail"])
        self.assertEqual(result["blocking_exclusions"], [])

    def test_no_minor_upgrades_blocks_when_only_a_pool_needs_the_minor(self):
        result = self._one(exclusion("x", "NO_MINOR_UPGRADES"), master=TARGET, pools=[pool("old", "1.34.0-gke.1")])
        self.assertTrue(result["exclusions"][0]["blocks"])
        self.assertIn("minor upgrade for pool old", result["exclusions"][0]["detail"])

    def test_no_upgrades_blocks_even_a_patch(self):
        result = self._one(exclusion("freeze", "NO_UPGRADES"), target_text="1.34.12-gke.1", target=(1, 34, 12, 1))
        self.assertTrue(result["exclusions"][0]["blocks"])
        self.assertIn("covers every upgrade", result["exclusions"][0]["detail"])

    def test_missing_scope_is_no_upgrades(self):
        result = self._one(exclusion("legacy", None), target_text="1.34.12-gke.1", target=(1, 34, 12, 1))
        self.assertEqual(result["exclusions"][0]["scope"], "NO_UPGRADES")
        self.assertTrue(result["exclusions"][0]["blocks"])

    def test_no_minor_or_node_upgrades_blocks_a_patch_a_pool_needs(self):
        policy = exclusion("x", "NO_MINOR_OR_NODE_UPGRADES")
        patch = self._one(policy, target_text="1.34.12-gke.1", target=(1, 34, 12, 1), master=(1, 34, 12, 1))
        self.assertTrue(patch["exclusions"][0]["blocks"])
        self.assertIn("pool(s) default-pool need a node upgrade", patch["exclusions"][0]["detail"])
        minor = self._one(policy)
        self.assertIn("minor upgrade for the control plane", minor["exclusions"][0]["detail"])
        current = self._one(policy, target_text="1.34.11-gke.1000", target=MASTER, master=MASTER)
        self.assertFalse(current["exclusions"][0]["blocks"])
        self.assertIn("no component is below the target", current["exclusions"][0]["detail"])

    def test_expired_and_future_exclusions_are_not_in_effect(self):
        expired = self._one(exclusion("old", "NO_UPGRADES", start=AT - timedelta(days=30), end=AT - timedelta(days=1)))
        future = self._one(exclusion("soon", "NO_UPGRADES", start=AT + timedelta(days=1), end=AT + timedelta(days=30)))
        for result in (expired, future):
            self.assertFalse(result["exclusions"][0]["in_effect"])
            self.assertFalse(result["exclusions"][0]["blocks"])
            self.assertEqual(result["blocking_exclusions"], [])

    def test_at_moves_the_verdict(self):
        policy = exclusion("soon", "NO_UPGRADES", start=AT + timedelta(days=1), end=AT + timedelta(days=30))
        self.assertTrue(self._one(policy, at=AT + timedelta(days=2))["exclusions"][0]["blocks"])

    def test_unknown_target_leaves_a_minor_scope_undecided_but_not_no_upgrades(self):
        undecided = self._one(exclusion("x", "NO_MINOR_UPGRADES"), target_text=None, target=None)
        self.assertIsNone(undecided["exclusions"][0]["blocks"])
        self.assertEqual(undecided["undecided_exclusions"], ["x"])
        decided = self._one(exclusion("x", "NO_UPGRADES"), target_text=None, target=None)
        self.assertTrue(decided["exclusions"][0]["blocks"])

    def test_unknown_scope_and_bad_times_are_undecided(self):
        odd = self._one(exclusion("x", "NO_SOMETHING"))
        self.assertIsNone(odd["exclusions"][0]["blocks"])
        broken = self._one({"window": {"maintenanceExclusions": {"x": {"startTime": "yesterday", "endTime": "tomorrow"}}}})
        self.assertIsNone(broken["exclusions"][0]["blocks"])
        self.assertEqual(broken["undecided_exclusions"], ["x"])

    def test_no_policy_has_no_exclusions_and_no_window(self):
        result = r.evaluate_maintenance(None, AT, TARGET_TEXT, TARGET, MASTER, self.POOLS)
        self.assertEqual(result["exclusions"], [])
        self.assertEqual(result["window"]["kind"], "none")
        self.assertEqual(result["window"]["state"], "none")


class WindowTest(unittest.TestCase):
    def test_daily_window_closed_with_next_opening(self):
        result = r.evaluate_window({"dailyMaintenanceWindow": {"startTime": "03:00", "duration": "PT4H0M0S"}}, AT)
        self.assertEqual(result["kind"], "daily")
        self.assertEqual(result["state"], "closed")
        self.assertEqual(result["next_opening"], "2026-09-15T03:00Z")
        self.assertIsNone(result["closes_at"])
        self.assertEqual(result["detail"], "daily at 03:00Z for 4h")

    def test_daily_window_open(self):
        result = r.evaluate_window({"dailyMaintenanceWindow": {"startTime": "03:00"}}, AT.replace(hour=5))
        self.assertEqual(result["state"], "open")
        self.assertEqual(result["closes_at"], "2026-09-14T07:00Z")
        self.assertEqual(result["next_opening"], "2026-09-15T03:00Z")

    def test_daily_window_spanning_midnight(self):
        result = r.evaluate_window({"dailyMaintenanceWindow": {"startTime": "22:00"}}, AT.replace(hour=1))
        self.assertEqual(result["state"], "open")
        self.assertEqual(result["closes_at"], "2026-09-14T02:00Z")

    def test_weekly_byday(self):
        window = {"recurringWindow": {"recurrence": "FREQ=WEEKLY;BYDAY=SA,SU", "window": {"startTime": "2026-01-03T04:00:00Z", "endTime": "2026-01-03T08:00:00Z"}}}
        weekday = r.evaluate_window(window, AT)  # a Monday
        self.assertEqual(weekday["state"], "closed")
        self.assertEqual(weekday["next_opening"], "2026-09-19T04:00Z")
        sunday = r.evaluate_window(window, datetime(2026, 9, 13, 5, 0, tzinfo=timezone.utc))
        self.assertEqual(sunday["state"], "open")
        self.assertEqual(sunday["closes_at"], "2026-09-13T08:00Z")
        self.assertEqual(sunday["detail"], "SA, SU from 04:00Z for 4h")

    def test_weekly_without_byday_uses_the_start_weekday(self):
        window = {"recurringWindow": {"recurrence": "FREQ=WEEKLY", "window": {"startTime": "2026-01-05T04:00:00Z", "endTime": "2026-01-05T08:00:00Z"}}}  # a Monday
        result = r.evaluate_window(window, AT.replace(hour=6))
        self.assertEqual(result["state"], "open")

    def test_recurring_daily_with_long_window(self):
        window = {"recurringWindow": {"recurrence": "FREQ=DAILY", "window": {"startTime": "2026-01-01T20:00:00Z", "endTime": "2026-01-02T06:00:00Z"}}}
        result = r.evaluate_window(window, AT.replace(hour=2))
        self.assertEqual(result["state"], "open")
        self.assertEqual(result["closes_at"], "2026-09-14T06:00Z")

    def test_before_the_first_occurrence_is_closed_with_that_opening(self):
        window = {"recurringWindow": {"recurrence": "FREQ=DAILY", "window": {"startTime": "2026-09-16T04:00:00Z", "endTime": "2026-09-16T08:00:00Z"}}}
        result = r.evaluate_window(window, AT)
        self.assertEqual(result["state"], "closed")
        self.assertEqual(result["next_opening"], "2026-09-16T04:00Z")

    def test_unsupported_recurrences_are_not_evaluated(self):
        for rule in ("FREQ=MONTHLY;BYMONTHDAY=1", "FREQ=WEEKLY;INTERVAL=2;BYDAY=SA", "FREQ=WEEKLY;BYDAY=1SA", "FREQ=DAILY;BYDAY=SA", "nonsense", ""):
            window = {"recurringWindow": {"recurrence": rule, "window": {"startTime": "2026-01-03T04:00:00Z", "endTime": "2026-01-03T08:00:00Z"}}}
            result = r.evaluate_window(window, AT)
            self.assertEqual(result["state"], "not evaluated", rule)
            self.assertIsNone(result["next_opening"], rule)

    def test_bad_daily_start_is_not_evaluated(self):
        result = r.evaluate_window({"dailyMaintenanceWindow": {"startTime": "3am"}}, AT)
        self.assertEqual(result["state"], "not evaluated")

    def test_duration_parsing(self):
        self.assertEqual(r.parse_iso_duration("PT4H0M0S"), timedelta(hours=4))
        self.assertEqual(r.parse_iso_duration("P1DT2H"), timedelta(days=1, hours=2))
        self.assertIsNone(r.parse_iso_duration("4h"))
        self.assertIsNone(r.parse_iso_duration(None))

    def test_rfc3339_parsing(self):
        self.assertEqual(r.parse_rfc3339("2026-12-11T14:35:00Z"), datetime(2026, 12, 11, 14, 35, tzinfo=timezone.utc))
        self.assertEqual(r.parse_rfc3339("2026-12-11T15:35:00+01:00"), datetime(2026, 12, 11, 14, 35, tzinfo=timezone.utc))
        self.assertEqual(r.parse_rfc3339("2026-12-11T14:35:00").tzinfo, timezone.utc)
        for bad in ("2026-13-01T00:00:00Z", "tomorrow", "", None):
            self.assertIsNone(r.parse_rfc3339(bad), bad)


class SkewTest(unittest.TestCase):
    def _verdicts(self, target, pools):
        return {p["name"]: p["verdict"] for p in r.evaluate_skew(target, pools, False)["pools"]}

    def test_three_two_one_and_cross_major(self):
        pools = [pool("three", "1.32.0-gke.1"), pool("two", "1.33.0-gke.1"), pool("one", "1.34.0-gke.1"), pool("major", "0.99.0-gke.1")]
        result = r.evaluate_skew(TARGET, pools, False)
        self.assertEqual(self._verdicts(TARGET, pools), {"three": "blocks", "two": "at ceiling", "one": "ok", "major": "blocks"})
        self.assertEqual(result["blocking"], ["three", "major"])
        self.assertEqual(result["at_ceiling"], ["two"])
        by_name = {p["name"]: p for p in result["pools"]}
        self.assertEqual(by_name["three"]["minors_behind_target"], 3)
        self.assertIn("blocks the control-plane upgrade until the pool moves", by_name["three"]["detail"])
        self.assertEqual(by_name["major"]["detail"], "major version differs from the target")

    def test_pool_ahead_is_ok(self):
        self.assertEqual(self._verdicts(TARGET, [pool("new", "1.36.0-gke.1")]), {"new": "ok"})

    def test_autopilot_is_not_applicable(self):
        result = r.evaluate_skew(TARGET, [pool("x", "1.30.0-gke.1")], True)
        self.assertFalse(result["applicable"])
        self.assertEqual(result["blocking"], [])
        self.assertIn("Autopilot", result["reason"])

    def test_no_target_is_unknown(self):
        result = r.evaluate_skew(None, [pool("x", "1.30.0-gke.1")], False)
        self.assertTrue(result["applicable"])
        self.assertEqual(result["unknown"], ["x"])
        self.assertEqual(result["blocking"], [])

    def test_unparsable_pool_is_unknown(self):
        result = r.evaluate_skew(TARGET, [pool("weird", "latest")], False)
        self.assertEqual(result["unknown"], ["weird"])


class VerdictTest(unittest.TestCase):
    CLEAR = {"blocking_exclusions": [], "undecided_exclusions": []}
    NO_SKEW = {"blocking": [], "unknown": []}

    def test_ready_needs_every_rule_evaluated(self):
        pdbs = {"blocking": []}
        self.assertEqual(r.readiness_status(pdbs, self.CLEAR, self.NO_SKEW, True), "ready")
        self.assertEqual(r.readiness_status(None, self.CLEAR, self.NO_SKEW, True), "unknown")
        self.assertEqual(r.readiness_status(pdbs, self.CLEAR, self.NO_SKEW, False), "unknown")
        self.assertEqual(r.readiness_status(pdbs, {"blocking_exclusions": [], "undecided_exclusions": ["x"]}, self.NO_SKEW, True), "unknown")
        self.assertEqual(r.readiness_status(pdbs, self.CLEAR, {"blocking": [], "unknown": ["p"]}, True), "unknown")

    def test_blocked_beats_unknown(self):
        self.assertEqual(r.readiness_status({"blocking": [{"pdb": "a/b"}]}, self.CLEAR, self.NO_SKEW, False), "blocked")
        self.assertEqual(r.readiness_status(None, {"blocking_exclusions": ["x"], "undecided_exclusions": []}, self.NO_SKEW, True), "blocked")
        self.assertEqual(r.readiness_status(None, self.CLEAR, {"blocking": ["p"], "unknown": []}, True), "blocked")


if __name__ == "__main__":
    unittest.main()
