#!/usr/bin/env python3
"""Unit tests for upgrade_readiness.py: the PDB, webhook, maintenance and skew rules on canned objects."""

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

    def test_expected_pods_above_the_matched_total_sets_the_total(self):
        # A bare ReplicaSet shares the Deployment's labels: the controller expects 5 pods,
        # so minAvailable 4 leaves one disruption and blocks nothing; 5 blocks.
        dep = workload("Deployment", "d", 3, {"app": "x"})
        sel = {"matchLabels": {"app": "x"}}
        self.assertEqual(self._grade(dep, pdb("p", {"minAvailable": 4, "selector": sel}, expected=5, allowed=1))["blocking"], [])
        finding = self._grade(dep, pdb("p", {"minAvailable": 5, "selector": sel}, expected=5, allowed=0))["blocking"][0]
        self.assertEqual(finding["field"], "minAvailable: 5 (>= 5 expected pods)")
        self.assertEqual(finding["expected_pods"], 5)
        # A stale status below the spec total does not shrink it.
        self.assertEqual(self._grade(dep, pdb("p", {"minAvailable": 3, "selector": sel}, expected=1, allowed=0))["blocking"][0]["field"], "minAvailable: 3 (>= 3 expected pods)")

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
        monday = r.evaluate_window(window, AT.replace(hour=6))
        self.assertEqual(monday["state"], "open")
        self.assertEqual(monday["detail"], "MO from 04:00Z for 4h")
        tuesday = r.evaluate_window(window, AT.replace(day=15, hour=6))
        self.assertEqual(tuesday["state"], "closed")
        self.assertEqual(tuesday["next_opening"], "2026-09-21T04:00Z")

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
        # A first occurrence beyond the one-week horizon is still the next opening.
        far = {"recurringWindow": {"recurrence": "FREQ=DAILY", "window": {"startTime": "2026-10-01T04:00:00Z", "endTime": "2026-10-01T08:00:00Z"}}}
        result = r.evaluate_window(far, AT)
        self.assertEqual(result["state"], "closed")
        self.assertEqual(result["next_opening"], "2026-10-01T04:00Z")

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
        self.assertEqual(r.parse_rfc3339("2026-12-11t14:35:00.250z"), datetime(2026, 12, 11, 14, 35, 0, 250000, tzinfo=timezone.utc))
        # RFC 3339 proper: a bare date, the basic form and a naive time are refused, not
        # read as midnight or as UTC.
        for bad in ("2026-13-01T00:00:00Z", "2026-09-14", "20260914T150000Z", "2026-09-14T15:00:00", "2026-09-14 15:00:00Z", "tomorrow", "", None):
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
    NO_WEBHOOKS = {"blocking": []}

    def test_ready_needs_every_rule_evaluated(self):
        pdbs = {"blocking": []}
        self.assertEqual(r.readiness_status(pdbs, self.NO_WEBHOOKS, self.CLEAR, self.NO_SKEW, True), "ready")
        self.assertEqual(r.readiness_status(None, None, self.CLEAR, self.NO_SKEW, True), "unknown")
        self.assertEqual(r.readiness_status(pdbs, None, self.CLEAR, self.NO_SKEW, True), "unknown")
        self.assertEqual(r.readiness_status(pdbs, self.NO_WEBHOOKS, self.CLEAR, self.NO_SKEW, False), "unknown")
        self.assertEqual(r.readiness_status(pdbs, self.NO_WEBHOOKS, {"blocking_exclusions": [], "undecided_exclusions": ["x"]}, self.NO_SKEW, True), "unknown")
        self.assertEqual(r.readiness_status(pdbs, self.NO_WEBHOOKS, self.CLEAR, {"blocking": [], "unknown": ["p"]}, True), "unknown")

    def test_blocked_beats_unknown(self):
        self.assertEqual(r.readiness_status({"blocking": [{"pdb": "a/b"}]}, self.NO_WEBHOOKS, self.CLEAR, self.NO_SKEW, False), "blocked")
        self.assertEqual(r.readiness_status(None, None, {"blocking_exclusions": ["x"], "undecided_exclusions": []}, self.NO_SKEW, True), "blocked")
        self.assertEqual(r.readiness_status(None, None, self.CLEAR, {"blocking": ["p"], "unknown": []}, True), "blocked")
        self.assertEqual(r.readiness_status({"blocking": []}, {"blocking": [{"webhook": "g/h"}]}, self.CLEAR, self.NO_SKEW, False), "blocked")


def webhook_config(kind, name, hooks):
    return {"kind": kind, "metadata": {"name": name}, "webhooks": hooks}


def rule(resources, operations=("CREATE",), groups=("",), scope=None, versions=("*",)):
    record = {"apiGroups": list(groups), "apiVersions": list(versions), "operations": list(operations), "resources": list(resources)}
    if scope is not None:
        record["scope"] = scope
    return record


def hook(name, rules, policy=None, service=("scen", "gate-svc"), port=None, url=None):
    record = {"name": name, "rules": rules, "clientConfig": {}}
    if policy is not None:
        record["failurePolicy"] = policy
    if url is not None:
        record["clientConfig"]["url"] = url
    elif service is not None:
        record["clientConfig"]["service"] = {"namespace": service[0], "name": service[1]}
        if port is not None:
            record["clientConfig"]["service"]["port"] = port
    return record


def service(namespace, name, ports=((443, "https"),)):
    return {"kind": "Service", "metadata": {"namespace": namespace, "name": name}, "spec": {"ports": [{"port": p, "name": n} for p, n in ports]}}


def endpoint_slice(namespace, svc, ready_flags, port_name="https"):
    return {
        "kind": "EndpointSlice",
        "metadata": {"namespace": namespace, "name": f"{svc}-abc", "labels": {"kubernetes.io/service-name": svc}},
        "ports": [{"name": port_name, "port": 8443}],
        "endpoints": [{"conditions": {} if flag is None else {"ready": flag}} for flag in ready_flags],
    }


POD_GATE = [rule(["pods"])]
def scoped(record, namespace_selector):
    record["namespaceSelector"] = namespace_selector
    return record
LIVE = [service("scen", "gate-svc")], [endpoint_slice("scen", "gate-svc", [True])]


def grade(hooks, services=(), slices=(), kind="ValidatingWebhookConfiguration"):
    return r.grade_webhooks([webhook_config(kind, "gate", hooks)], list(services), list(slices))


class WebhookBackendTest(unittest.TestCase):
    def test_missing_service_blocks_a_pod_gate(self):
        graded = grade([hook("g.example.com", POD_GATE, policy="Fail")])
        self.assertEqual(len(graded["blocking"]), 1)
        finding = graded["blocking"][0]
        self.assertEqual(finding["webhook"], "gate/g.example.com")
        self.assertEqual(finding["reason"], "Service scen/gate-svc does not exist")
        self.assertEqual(finding["upgrade_path"], ["CREATE pods"])
        self.assertIn("matches CREATE pods", r.describe_webhook_finding(finding))

    def test_absent_failure_policy_is_fail_the_v1_default(self):
        self.assertEqual(len(grade([hook("d.example.com", POD_GATE)], kind="MutatingWebhookConfiguration")["blocking"]), 1)

    def test_ready_backend_on_the_webhook_port_does_not_block(self):
        graded = grade([hook("g.example.com", POD_GATE, policy="Fail")], *LIVE)
        self.assertEqual((graded["blocking"], graded["outage"], graded["evaluated"]), ([], [], 1))

    def test_endpoint_without_a_ready_condition_counts_as_ready(self):
        graded = grade([hook("g.example.com", POD_GATE, policy="Fail")], [service("scen", "gate-svc")], [endpoint_slice("scen", "gate-svc", [None])])
        self.assertEqual(graded["blocking"], [])

    def test_every_endpoint_not_ready_blocks(self):
        graded = grade([hook("g.example.com", POD_GATE, policy="Fail")], [service("scen", "gate-svc")], [endpoint_slice("scen", "gate-svc", [False, False])])
        self.assertEqual(graded["blocking"][0]["reason"], "Service scen/gate-svc has no ready endpoints on port 443")

    def test_service_without_the_webhook_port_blocks(self):
        # The API server refuses to resolve the webhook: no Service port equals the webhook's port.
        graded = grade([hook("g.example.com", POD_GATE, policy="Fail", port=9443)], *LIVE)
        self.assertEqual(graded["blocking"][0]["reason"], "Service scen/gate-svc has no port 9443")

    def test_endpoints_on_another_named_port_do_not_count(self):
        services = [service("scen", "gate-svc", ports=((443, "https"), (8080, "metrics")))]
        slices = [endpoint_slice("scen", "gate-svc", [True], port_name="metrics")]
        self.assertEqual(len(grade([hook("g.example.com", POD_GATE, policy="Fail")], services, slices)["blocking"]), 1)

    def test_unnamed_single_port_matches_an_unnamed_slice_port(self):
        services = [service("scen", "gate-svc", ports=((443, None),))]
        slices = [endpoint_slice("scen", "gate-svc", [True], port_name=None)]
        self.assertEqual(grade([hook("g.example.com", POD_GATE, policy="Fail")], services, slices)["blocking"], [])

    def test_slices_of_another_service_or_namespace_do_not_count(self):
        services = [service("scen", "gate-svc")]
        slices = [endpoint_slice("scen", "other-svc", [True]), endpoint_slice("prod", "gate-svc", [True])]
        self.assertEqual(len(grade([hook("g.example.com", POD_GATE, policy="Fail")], services, slices)["blocking"]), 1)

    def test_fail_open_and_url_backends_are_counted_not_graded(self):
        graded = grade([hook("a.example.com", POD_GATE, policy="Ignore"), hook("e.example.com", POD_GATE, policy="Fail", service=None, url="https://localhost:5443/v")])
        self.assertEqual((graded["blocking"], graded["outage"]), ([], []))
        self.assertEqual((graded["fail_open"], graded["url_backends"], graded["evaluated"]), (1, 1, 0))


class KubeSystemReachTest(unittest.TestCase):
    """A dead gate off the node path that can refuse the bootstrap Role and RoleBinding
    writes a new master's start-up reconciles in kube-system and kube-public blocks the
    control-plane upgrade (the Jetstack 2019 shape, on the write that still gates a master)."""

    ROLE_GATE = [rule(["roles"], groups=("rbac.authorization.k8s.io",))]
    SEEDED_SCOPE = {"matchLabels": {"kubernetes.io/metadata.name": "seeded-upgrade"}}

    def _one(self, hooks):
        graded = grade(hooks)
        return graded["blocking"], graded["outage"]

    def test_a_cluster_wide_dead_role_gate_blocks_and_names_the_write(self):
        blocking, outage = self._one([hook("opa.example.com", self.ROLE_GATE, policy="Fail")])
        self.assertEqual(outage, [])
        self.assertEqual(blocking[0]["upgrade_path"], ["CREATE roles in kube-system,kube-public"])
        self.assertIn("matches CREATE roles in kube-system,kube-public", r.describe_webhook_finding(blocking[0]))

    def test_a_rolebinding_update_gate_blocks_too(self):
        gate = hook("opa.example.com", [rule(["rolebindings"], operations=("UPDATE",), groups=("rbac.authorization.k8s.io",))], policy="Fail")
        blocking, _ = self._one([gate])
        self.assertEqual(blocking[0]["upgrade_path"], ["UPDATE rolebindings in kube-system,kube-public"])

    def test_an_empty_selector_admits_every_namespace(self):
        blocking, _ = self._one([scoped(hook("opa.example.com", self.ROLE_GATE, policy="Fail"), {})])
        self.assertEqual(len(blocking), 1)

    def test_a_cluster_wide_dead_configmap_gate_stays_an_outage(self):
        # The ConfigMap write the Jetstack outage deadlocked on left the master's start-up
        # path in Kubernetes 1.17; a refused ConfigMap write is retried, not fatal.
        blocking, outage = self._one([hook("opa.example.com", [rule(["configmaps"])], policy="Fail")])
        self.assertEqual((blocking, len(outage)), ([], 1))

    def test_a_selector_naming_another_namespace_keeps_the_gate_an_outage(self):
        blocking, outage = self._one([scoped(hook("gate.seeded.invalid", self.ROLE_GATE, policy="Fail"), self.SEEDED_SCOPE)])
        self.assertEqual((blocking, len(outage)), ([], 1))

    def test_a_selector_excluding_kube_system_alone_still_reaches_kube_public(self):
        # The bootstrap-signer Role and RoleBinding are reconciled in kube-public too.
        gate = scoped(hook("opa.example.com", self.ROLE_GATE, policy="Fail"), {"matchExpressions": [{"key": "kubernetes.io/metadata.name", "operator": "NotIn", "values": ["kube-system"]}]})
        blocking, _ = self._one([gate])
        self.assertEqual(blocking[0]["upgrade_path"], ["CREATE roles in kube-public"])

    def test_a_selector_excluding_both_bootstrap_namespaces_keeps_the_gate_an_outage(self):
        gate = scoped(hook("opa.example.com", self.ROLE_GATE, policy="Fail"), {"matchExpressions": [{"key": "kubernetes.io/metadata.name", "operator": "NotIn", "values": ["kube-system", "kube-public"]}]})
        blocking, outage = self._one([gate])
        self.assertEqual((blocking, len(outage)), ([], 1))

    def test_a_dead_clusterrole_gate_blocks_whatever_its_selector_says(self):
        # Admission matches a cluster-scoped object before it reads the namespace selector,
        # and the bootstrap hook reconciles the ClusterRoles first.
        excluded = {"matchExpressions": [{"key": "kubernetes.io/metadata.name", "operator": "NotIn", "values": ["kube-system", "kube-public"]}]}
        gate = scoped(hook("kyverno.example.com", [rule(["roles", "rolebindings", "clusterroles", "clusterrolebindings"], operations=("CREATE", "UPDATE"), groups=("rbac.authorization.k8s.io",))], policy="Fail"), excluded)
        blocking, outage = self._one([gate])
        self.assertEqual(outage, [])
        self.assertEqual(blocking[0]["upgrade_path"], ["CREATE clusterroles", "UPDATE clusterroles", "CREATE clusterrolebindings", "UPDATE clusterrolebindings"])

    def test_a_clusterrole_gate_pinned_to_an_unserved_version_is_sent_nothing(self):
        gate = hook("opa.example.com", [rule(["clusterroles"], groups=("rbac.authorization.k8s.io",), versions=("v1beta1",))], policy="Fail")
        blocking, outage = self._one([gate])
        self.assertEqual(blocking, [])
        self.assertEqual(outage[0]["version_pinned"], ["CREATE clusterroles"])

    def test_a_selector_this_reader_cannot_evaluate_errs_toward_blocking(self):
        gate = scoped(hook("opa.example.com", self.ROLE_GATE, policy="Fail"), {"matchExpressions": [{"key": "tier", "operator": "Gt", "values": ["1"]}]})
        blocking, _ = self._one([gate])
        self.assertEqual(len(blocking), 1)

    def test_a_selector_on_a_label_kube_system_does_not_carry_by_default_reads_as_not_admitting(self):
        # The documented limit: kube-system is judged on its default label alone.
        gate = scoped(hook("opa.example.com", self.ROLE_GATE, policy="Fail"), {"matchLabels": {"gatekeeper": "enabled"}})
        blocking, outage = self._one([gate])
        self.assertEqual((blocking, len(outage)), ([], 1))

    def test_a_dead_lease_gate_already_blocks_on_the_node_path(self):
        # Leader-election Leases live in kube-system, but the node path's Lease rows already
        # make any Lease gate a blocker, so the kube-system list does not repeat them.
        gate = hook("leases.example.com", [rule(["leases"], operations=("UPDATE",), groups=("coordination.k8s.io",))], policy="Fail")
        blocking, _ = self._one([gate])
        self.assertEqual(blocking[0]["upgrade_path"], ["UPDATE leases"])

    def test_a_dead_gate_on_another_resource_stays_an_outage_whatever_its_selector(self):
        # cert-manager's shape: fail-closed on its own kinds, no namespace selector.
        gate = hook("webhook.cert-manager.io", [rule(["certificates", "certificaterequests"], operations=("CREATE", "UPDATE"), groups=("cert-manager.io",))], policy="Fail")
        blocking, outage = self._one([gate])
        self.assertEqual((blocking, len(outage)), ([], 1))

    def test_a_role_gate_pinned_to_an_unserved_version_is_sent_nothing_and_says_so(self):
        gate = hook("opa.example.com", [rule(["roles"], groups=("rbac.authorization.k8s.io",), versions=("v1beta1",))], policy="Fail")
        blocking, outage = self._one([gate])
        self.assertEqual(blocking, [])
        self.assertEqual(outage[0]["version_pinned"], ["CREATE roles in kube-system,kube-public"])
        self.assertIn("the server serves CREATE roles in kube-system,kube-public at v1 alone, so it sends this webhook none of them", r.describe_webhook_finding(outage[0]))

    def test_a_pinned_role_gate_scoped_off_kube_system_is_a_plain_outage(self):
        gate = scoped(hook("opa.example.com", [rule(["roles"], groups=("rbac.authorization.k8s.io",), versions=("v1beta1",))], policy="Fail"), self.SEEDED_SCOPE)
        _, outage = self._one([gate])
        self.assertEqual(outage[0]["version_pinned"], [])
        self.assertIn("it fails its own requests now", r.describe_webhook_finding(outage[0]))

    def test_the_node_path_match_is_named_ahead_of_the_kube_system_reach(self):
        gate = hook("opa.example.com", [rule(["pods", "roles"], groups=("", "rbac.authorization.k8s.io"))], policy="Fail")
        blocking, _ = self._one([gate])
        self.assertEqual(blocking[0]["upgrade_path"], ["CREATE pods", "CREATE roles in kube-system,kube-public"])

    def test_the_control_plane_write_lists_are_pinned(self):
        self.assertEqual(
            r.CONTROL_PLANE_KUBE_SYSTEM_WRITES,
            (
                ("rbac.authorization.k8s.io", "v1", "roles", "CREATE", "Namespaced"),
                ("rbac.authorization.k8s.io", "v1", "roles", "UPDATE", "Namespaced"),
                ("rbac.authorization.k8s.io", "v1", "rolebindings", "CREATE", "Namespaced"),
                ("rbac.authorization.k8s.io", "v1", "rolebindings", "UPDATE", "Namespaced"),
            ),
        )
        self.assertEqual(
            r.CONTROL_PLANE_CLUSTER_WRITES,
            (
                ("rbac.authorization.k8s.io", "v1", "clusterroles", "CREATE", "Cluster"),
                ("rbac.authorization.k8s.io", "v1", "clusterroles", "UPDATE", "Cluster"),
                ("rbac.authorization.k8s.io", "v1", "clusterrolebindings", "CREATE", "Cluster"),
                ("rbac.authorization.k8s.io", "v1", "clusterrolebindings", "UPDATE", "Cluster"),
            ),
        )


class WebhookScopeTest(unittest.TestCase):
    def _path(self, rules):
        return r.upgrade_path_matches({"rules": rules})

    def test_a_gate_outside_the_upgrade_path_is_an_outage_not_a_blocker(self):
        # The seeded fleet's fixture: a gate on ConfigMaps with no Service, scoped to its own
        # namespace (bench/tf/fleet/defects-b.tf). It is an outage because its rules match
        # ConfigMaps only, which neither graded list carries; the scope confines the gate.
        gate = scoped(hook("gate.seeded.invalid", [rule(["configmaps"])], policy="Fail", service=("seeded-upgrade", "nonexistent-admission-gate")), {"matchLabels": {"kubernetes.io/metadata.name": "seeded-upgrade"}})
        graded = grade([gate])
        self.assertEqual(graded["blocking"], [])
        self.assertEqual(len(graded["outage"]), 1)
        self.assertIn("matches none of the operations this rule reads as the upgrade's path (its rules: CREATE configmaps)", r.describe_webhook_finding(graded["outage"][0]))
        self.assertEqual(graded["outage"][0]["rules"], ["CREATE configmaps"])

    def test_an_outage_cell_names_every_rule_with_its_group(self):
        # GKE's managed Prometheus operator gate, outside the path: the cell says what it does match.
        hook_rules = [rule(["rules", "clusterrules"], groups=("monitoring.googleapis.com",), operations=("CREATE", "UPDATE")), rule(["configmaps"])]
        graded = grade([hook("mon.example.com", hook_rules, policy="Fail")])
        self.assertEqual(graded["outage"][0]["rules"], ["CREATE/UPDATE rules,clusterrules in monitoring.googleapis.com", "CREATE configmaps"])
        self.assertIn("(its rules: CREATE/UPDATE rules,clusterrules in monitoring.googleapis.com, CREATE configmaps)", r.describe_webhook_finding(graded["outage"][0]))

    def test_an_outage_cell_names_the_core_group_beside_another(self):
        # A rule on `["", "apps"]` gates core resources too: the cell says `core,apps`, not `apps`.
        hook_rules = [rule(["configmaps", "deployments"], groups=("", "apps")), rule(["configmaps"], groups=("",)), rule(["rules"], groups=("monitoring.googleapis.com", ""))]
        graded = grade([hook("policy.example.com", hook_rules, policy="Fail")])
        self.assertEqual(graded["outage"][0]["rules"], ["CREATE configmaps,deployments in core,apps", "CREATE configmaps", "CREATE rules in monitoring.googleapis.com,core"])

    def test_each_upgrade_path_target(self):
        self.assertEqual(self._path([rule(["pods"])]), ["CREATE pods"])
        self.assertEqual(self._path([rule(["pods/binding"])]), ["CREATE pods/binding"])
        self.assertEqual(self._path([rule(["pods/status"], operations=("UPDATE",))]), ["UPDATE pods/status"])
        self.assertEqual(self._path([rule(["pods"], operations=("DELETE",))]), ["DELETE pods"])
        self.assertEqual(self._path([rule(["pods/eviction"])]), ["CREATE pods/eviction"])
        self.assertEqual(self._path([rule(["nodes"], operations=("CREATE", "UPDATE", "DELETE"))]), ["CREATE nodes", "UPDATE nodes", "DELETE nodes"])
        self.assertEqual(self._path([rule(["nodes/status"], operations=("UPDATE",))]), ["UPDATE nodes/status"])
        self.assertEqual(self._path([rule(["leases"], operations=("CREATE", "UPDATE"), groups=("coordination.k8s.io",))]), ["CREATE leases", "UPDATE leases"])
        # The kubelet's TokenRequest for every projected token, the new node's bootstrap CSR,
        # and the attach controller's VolumeAttachment for a replacement pod's disk.
        self.assertEqual(self._path([rule(["serviceaccounts/token"])]), ["CREATE serviceaccounts/token"])
        self.assertEqual(self._path([rule(["serviceaccounts/*"])]), ["CREATE serviceaccounts/token"])
        self.assertEqual(self._path([rule(["serviceaccounts"])]), [])  # the object, not its token subresource
        self.assertEqual(self._path([rule(["certificatesigningrequests"], groups=("certificates.k8s.io",))]), ["CREATE certificatesigningrequests"])
        # The CSR is useless until approved and signed: both are UPDATEs on its subresources,
        # which a rule on the bare resource does not reach and a rule on the subresource does.
        self.assertEqual(self._path([rule(["certificatesigningrequests/approval"], operations=("UPDATE",), groups=("certificates.k8s.io",))]), ["UPDATE certificatesigningrequests/approval"])
        self.assertEqual(self._path([rule(["certificatesigningrequests/status"], operations=("UPDATE",), groups=("certificates.k8s.io",))]), ["UPDATE certificatesigningrequests/status"])
        self.assertEqual(self._path([rule(["certificatesigningrequests/*"], operations=("UPDATE",), groups=("certificates.k8s.io",))]), ["UPDATE certificatesigningrequests/approval", "UPDATE certificatesigningrequests/status"])
        self.assertEqual(self._path([rule(["certificatesigningrequests"], operations=("UPDATE",), groups=("certificates.k8s.io",))]), [])  # the object, not its subresources
        # The eviction handler's budget-status write: the subresource, not the object.
        self.assertEqual(self._path([rule(["poddisruptionbudgets/status"], operations=("UPDATE",), groups=("policy",))]), ["UPDATE poddisruptionbudgets/status"])
        self.assertEqual(self._path([rule(["poddisruptionbudgets"], operations=("UPDATE",), groups=("policy",))]), [])
        # The CSINode the kubelet creates before it reports Ready and updates as drivers register;
        # its deletion is the garbage collector's, after the node is gone, and is not in the path.
        self.assertEqual(self._path([rule(["csinodes"], operations=("CREATE", "UPDATE", "DELETE"), groups=("storage.k8s.io",))]), ["CREATE csinodes", "UPDATE csinodes"])
        # The attachment's create and delete by the attach-detach controller, the external-attacher's
        # finalizer and status writes, and its finalizer on the PersistentVolume.
        self.assertEqual(self._path([rule(["volumeattachments"], operations=("CREATE", "UPDATE", "DELETE"), groups=("storage.k8s.io",))]), ["CREATE volumeattachments", "UPDATE volumeattachments", "DELETE volumeattachments"])
        self.assertEqual(self._path([rule(["volumeattachments/status"], operations=("UPDATE",), groups=("storage.k8s.io",))]), ["UPDATE volumeattachments/status"])
        self.assertEqual(self._path([rule(["volumeattachments/*"], operations=("UPDATE",), groups=("storage.k8s.io",))]), ["UPDATE volumeattachments", "UPDATE volumeattachments/status"])
        self.assertEqual(self._path([rule(["persistentvolumes"], operations=("UPDATE",))]), ["UPDATE persistentvolumes"])
        self.assertEqual(self._path([rule(["persistentvolumeclaims"], operations=("UPDATE",))]), [])
        # Weighed and left off: events are dropped when they cannot be written, and Service routing
        # is nothing the drain or the join waits on.
        self.assertEqual(self._path([rule(["events"], operations=("CREATE", "UPDATE"))]), [])
        self.assertEqual(self._path([rule(["events"], operations=("CREATE",), groups=("events.k8s.io",))]), [])
        self.assertEqual(self._path([rule(["endpoints"], operations=("CREATE", "UPDATE"))]), [])
        self.assertEqual(self._path([rule(["endpointslices"], operations=("CREATE", "UPDATE", "DELETE"), groups=("discovery.k8s.io",))]), [])

    def test_the_upgrade_path_list_is_pinned(self):
        # The one home of the list; a change here is a change to what the rule grades, and the
        # module's comments carry the source of every row.
        self.assertEqual(r.UPGRADE_PATH_TARGETS, (
            ("", "v1", "pods/eviction", "CREATE", "Namespaced"),
            ("policy", "v1", "poddisruptionbudgets/status", "UPDATE", "Namespaced"),
            ("", "v1", "pods/status", "UPDATE", "Namespaced"),
            ("", "v1", "pods", "DELETE", "Namespaced"),
            ("", "v1", "pods", "CREATE", "Namespaced"),
            ("", "v1", "pods/binding", "CREATE", "Namespaced"),
            ("", "v1", "serviceaccounts/token", "CREATE", "Namespaced"),
            ("", "v1", "nodes", "CREATE", "Cluster"),
            ("", "v1", "nodes", "UPDATE", "Cluster"),
            ("", "v1", "nodes/status", "UPDATE", "Cluster"),
            ("", "v1", "nodes", "DELETE", "Cluster"),
            ("coordination.k8s.io", "v1", "leases", "CREATE", "Namespaced"),
            ("coordination.k8s.io", "v1", "leases", "UPDATE", "Namespaced"),
            ("certificates.k8s.io", "v1", "certificatesigningrequests", "CREATE", "Cluster"),
            ("certificates.k8s.io", "v1", "certificatesigningrequests/approval", "UPDATE", "Cluster"),
            ("certificates.k8s.io", "v1", "certificatesigningrequests/status", "UPDATE", "Cluster"),
            ("storage.k8s.io", "v1", "csinodes", "CREATE", "Cluster"),
            ("storage.k8s.io", "v1", "csinodes", "UPDATE", "Cluster"),
            ("storage.k8s.io", "v1", "volumeattachments", "CREATE", "Cluster"),
            ("storage.k8s.io", "v1", "volumeattachments", "UPDATE", "Cluster"),
            ("storage.k8s.io", "v1", "volumeattachments/status", "UPDATE", "Cluster"),
            ("storage.k8s.io", "v1", "volumeattachments", "DELETE", "Cluster"),
            ("", "v1", "persistentvolumes", "UPDATE", "Cluster"),
        ))
        self.assertEqual(len(set(r.UPGRADE_PATH_TARGETS)), len(r.UPGRADE_PATH_TARGETS))

    def _each_alone_blocks(self, targets):
        # A dead fail-closed gate whose one rule names exactly one target: blocked on that
        # target alone, with nothing in the outage bucket, whether the rule's `apiVersions`
        # is `*` or the served version on the row.
        for group, version, resource, operation, scope in targets:
            for versions in (("*",), (version,)):
                with self.subTest(group=group, resource=resource, operation=operation, versions=versions):
                    graded = grade([hook("one.example.com", [rule([resource], operations=(operation,), groups=(group,), scope=scope, versions=versions)], policy="Fail")])
                    self.assertEqual([f["upgrade_path"] for f in graded["blocking"]], [[f"{operation} {resource}"]])
                    self.assertEqual(graded["outage"], [])

    def test_a_gate_on_each_drain_write_alone_blocks(self):
        self._each_alone_blocks(r.UPGRADE_PATH_DRAIN)

    def test_a_gate_on_each_replacement_pod_write_alone_blocks(self):
        self._each_alone_blocks(r.UPGRADE_PATH_REPLACEMENT_PODS)

    def test_a_gate_on_each_node_write_alone_blocks(self):
        self._each_alone_blocks(r.UPGRADE_PATH_NODES)

    def test_a_gate_on_each_kubelet_identity_write_alone_blocks(self):
        self._each_alone_blocks(r.UPGRADE_PATH_KUBELET_IDENTITY)

    def test_a_gate_on_each_storage_write_alone_blocks(self):
        self._each_alone_blocks(r.UPGRADE_PATH_STORAGE)

    def test_a_gate_on_csinodes_or_csr_approval_alone_blocks(self):
        # A new node stays NotReady without its CSINode; a bootstrap CSR nobody can approve or
        # sign leaves it without a client certificate. Each alone grades the member blocked.
        graded = grade([
            hook("csinode.example.com", [rule(["csinodes"], groups=("storage.k8s.io",))], policy="Fail"),
            hook("approve.example.com", [rule(["certificatesigningrequests/approval"], operations=("UPDATE",), groups=("certificates.k8s.io",))], policy="Fail"),
            hook("sign.example.com", [rule(["certificatesigningrequests/status"], operations=("UPDATE",), groups=("certificates.k8s.io",))], policy="Fail"),
        ])
        self.assertEqual([f["upgrade_path"] for f in graded["blocking"]], [["CREATE csinodes"], ["UPDATE certificatesigningrequests/approval"], ["UPDATE certificatesigningrequests/status"]])
        self.assertEqual(graded["outage"], [])

    def test_a_gate_on_token_requests_or_bootstrap_csrs_alone_blocks(self):
        # Replacement pods stuck in ContainerCreating without a token; a surge node that never joins without its CSR.
        graded = grade([hook("tokens.example.com", [rule(["serviceaccounts/token"])], policy="Fail"), hook("csr.example.com", [rule(["certificatesigningrequests"], groups=("certificates.k8s.io",))], policy="Fail")])
        self.assertEqual([f["upgrade_path"] for f in graded["blocking"]], [["CREATE serviceaccounts/token"], ["CREATE certificatesigningrequests"]])
        self.assertEqual(graded["outage"], [])

    def test_a_gate_on_scheduling_alone_blocks(self):
        # A scheduling-policy webhook on pods/binding stops every replacement pod from being placed.
        graded = grade([hook("bind.example.com", [rule(["pods/binding"])], policy="Fail")])
        self.assertEqual([f["upgrade_path"] for f in graded["blocking"]], [["CREATE pods/binding"]])

    def test_resource_wildcards(self):
        self.assertEqual(self._path([rule(["*"])]), ["CREATE pods", "CREATE nodes"])  # `*` covers resources, not subresources
        self.assertEqual(self._path([rule(["*/*"])]), ["CREATE pods/eviction", "CREATE pods", "CREATE pods/binding", "CREATE serviceaccounts/token", "CREATE nodes"])
        # `pods/*` is pods and its subresources, as the API server reads a `*` subresource.
        self.assertEqual(self._path([rule(["pods/*"])]), ["CREATE pods/eviction", "CREATE pods", "CREATE pods/binding"])
        self.assertEqual(self._path([rule(["pods/*"], operations=("DELETE",))]), ["DELETE pods"])
        self.assertEqual(self._path([rule(["nodes/*"], operations=("CREATE", "UPDATE", "DELETE"))]), ["CREATE nodes", "UPDATE nodes", "UPDATE nodes/status", "DELETE nodes"])
        # A gate on `leases/*` with a dead backend refuses every kubelet heartbeat: a blocker, not an outage.
        self.assertEqual(self._path([rule(["leases/*"], operations=("CREATE", "UPDATE"), groups=("coordination.k8s.io",))]), ["CREATE leases", "UPDATE leases"])
        self.assertEqual(self._path([rule(["*/eviction"])]), ["CREATE pods/eviction"])
        # An empty subresource after the slash is the resource itself, as the API server splits it.
        self.assertEqual(self._path([rule(["pods/"])]), ["CREATE pods"])
        self.assertEqual(self._path([rule(["*/"])]), ["CREATE pods", "CREATE nodes"])

    def test_resource_matches_is_the_api_servers_split_and_two_comparisons(self):
        # k8s.io/apiserver's rules.Matcher: splitResource on the first `/` (no `/` is an empty
        # subresource), then `res == "*" || res == opRes` and `sub == "*" || sub == opSub`.
        # Each spelling the API admits, against a resource and a subresource request.
        table = {
            "*": {"pods": True, "pods/status": False, "nodes": True, "nodes/status": False},
            "pods": {"pods": True, "pods/status": False, "nodes": False, "nodes/status": False},
            "pods/*": {"pods": True, "pods/status": True, "nodes": False, "nodes/status": False},
            "pods/status": {"pods": False, "pods/status": True, "nodes": False, "nodes/status": False},
            "*/status": {"pods": False, "pods/status": True, "nodes": False, "nodes/status": True},
            "*/*": {"pods": True, "pods/status": True, "nodes": True, "nodes/status": True},
            "pods/": {"pods": True, "pods/status": False, "nodes": False, "nodes/status": False},
            "*/": {"pods": True, "pods/status": False, "nodes": True, "nodes/status": False},
        }
        for spec, expected in table.items():
            for target, want in expected.items():
                with self.subTest(spec=spec, target=target):
                    self.assertEqual(r._resource_matches(spec, target), want)

    def test_operation_group_and_scope_must_all_match(self):
        self.assertEqual(self._path([rule(["pods"], operations=("UPDATE",))]), [])
        self.assertEqual(self._path([rule(["pods"], operations=("*",))]), ["DELETE pods", "CREATE pods"])
        self.assertEqual(self._path([rule(["pods"], groups=("apps",))]), [])
        self.assertEqual(self._path([rule(["*"], groups=("*",), operations=("*",))]), ["DELETE pods", "CREATE pods", "CREATE nodes", "UPDATE nodes", "DELETE nodes", "CREATE leases", "UPDATE leases", "CREATE certificatesigningrequests", "CREATE csinodes", "UPDATE csinodes", "CREATE volumeattachments", "UPDATE volumeattachments", "DELETE volumeattachments", "UPDATE persistentvolumes"])  # `*` is every resource and no subresource
        self.assertEqual(self._path([rule(["nodes"], scope="Namespaced")]), [])
        self.assertEqual(self._path([rule(["pods"], scope="Cluster")]), [])

    def test_api_versions_must_match_as_the_api_server_reads_them(self):
        # k8s.io/apiserver's rules.Matcher also requires `apiVersions` to carry `*` or the
        # request's version. A rule pinned to a version the server does not serve matches
        # nothing, so a dead gate left behind by pre-1.19 tooling is an outage, not a blocker.
        self.assertEqual(self._path([rule(["certificatesigningrequests"], groups=("certificates.k8s.io",), versions=("v1beta1",))]), [])
        self.assertEqual(self._path([rule(["poddisruptionbudgets/status"], operations=("UPDATE",), groups=("policy",), versions=("v1beta1",))]), [])
        self.assertEqual(self._path([rule(["csinodes", "volumeattachments"], groups=("storage.k8s.io",), versions=("v1beta1",))]), [])
        self.assertEqual(self._path([rule(["pods"], versions=())]), [])
        # `*`, the served version alone, or the served version among others, all match.
        self.assertEqual(self._path([rule(["certificatesigningrequests"], groups=("certificates.k8s.io",), versions=("*",))]), ["CREATE certificatesigningrequests"])
        self.assertEqual(self._path([rule(["certificatesigningrequests"], groups=("certificates.k8s.io",), versions=("v1",))]), ["CREATE certificatesigningrequests"])
        self.assertEqual(self._path([rule(["certificatesigningrequests"], groups=("certificates.k8s.io",), versions=("v1beta1", "v1"))]), ["CREATE certificatesigningrequests"])
        # Every row on the list is served at v1.
        self.assertEqual({row[1] for row in r.UPGRADE_PATH_TARGETS}, {"v1"})

    def test_a_dead_gate_pinned_to_an_unserved_version_is_an_outage_that_names_the_version(self):
        graded = grade([hook("stale.example.com", [rule(["certificatesigningrequests"], groups=("certificates.k8s.io",), versions=("v1beta1",))], policy="Fail")])
        self.assertEqual(graded["blocking"], [])
        self.assertEqual([f["rules"] for f in graded["outage"]], [["CREATE certificatesigningrequests in certificates.k8s.io at v1beta1"]])
        # The server serves the write at v1 alone, so it sends this webhook nothing: the cell
        # says so, and does not report requests failing now.
        self.assertEqual([f["version_pinned"] for f in graded["outage"]], [["CREATE certificatesigningrequests"]])
        cell = r.describe_webhook_finding(graded["outage"][0])
        self.assertIn("matches no request the server sends at a served version (its rules: CREATE certificatesigningrequests in certificates.k8s.io at v1beta1; the server serves CREATE certificatesigningrequests at v1 alone, so it sends this webhook none of them); it is reported, not graded", cell)
        self.assertNotIn("fails its own requests now", cell)
        # A dead gate on a resource off the list does fail the requests it matches now.
        graded = grade([hook("cm.example.com", [rule(["configmaps"])], policy="Fail")])
        self.assertEqual([f["version_pinned"] for f in graded["outage"]], [[]])
        self.assertIn("it fails its own requests now", r.describe_webhook_finding(graded["outage"][0]))
        # A pinned rule beside a served one is a blocker, with nothing pinned.
        graded = grade([hook("mixed.example.com", [rule(["certificatesigningrequests"], groups=("certificates.k8s.io",), versions=("v1beta1",)), rule(["pods"])], policy="Fail")])
        self.assertEqual([f["version_pinned"] for f in graded["blocking"]], [[]])
        self.assertEqual([f["pinned_rules"] for f in graded["blocking"]], [[]])
        self.assertIn("matches CREATE pods", r.describe_webhook_finding(graded["blocking"][0]))
        # A rule on `*` or on the served version alone carries no version suffix.
        for versions in (("*",), ("v1",)):
            with self.subTest(versions=versions):
                graded = grade([hook("live.example.com", [rule(["certificatesigningrequests"], groups=("certificates.k8s.io",), versions=versions)], policy="Fail")])
                self.assertEqual([f["rules"] for f in graded["blocking"]], [["CREATE certificatesigningrequests in certificates.k8s.io"]] if versions == ("*",) else [["CREATE certificatesigningrequests in certificates.k8s.io at v1"]])

    def test_a_pinned_rule_beside_a_live_off_path_rule_fails_the_live_rule_now(self):
        # The pin is judged per rule. A webhook pairing a CSR rule at v1beta1 with a ConfigMap
        # rule at v1 refuses every ConfigMap creation now: the cell names that rule as failing
        # and the pinned rule alone as sent nothing, rather than saying the server sends the
        # webhook none of its requests.
        pinned = rule(["certificatesigningrequests"], groups=("certificates.k8s.io",), versions=("v1beta1",))
        graded = grade([hook("mixed.example.com", [pinned, rule(["configmaps"], versions=("v1",))], policy="Fail")])
        self.assertEqual(graded["blocking"], [])
        finding = graded["outage"][0]
        self.assertEqual(finding["version_pinned"], ["CREATE certificatesigningrequests"])
        self.assertEqual(finding["pinned_rules"], ["CREATE certificatesigningrequests in certificates.k8s.io at v1beta1"])
        self.assertEqual(finding["live_rules"], ["CREATE configmaps at v1"])
        cell = r.describe_webhook_finding(finding)
        self.assertIn("matches none of the operations this rule reads as the upgrade's path (its rules: CREATE certificatesigningrequests in certificates.k8s.io at v1beta1, CREATE configmaps at v1); it fails now the requests matched by CREATE configmaps at v1, and is reported, not graded; the server serves CREATE certificatesigningrequests at v1 alone, so it sends the rule CREATE certificatesigningrequests in certificates.k8s.io at v1beta1 nothing", cell)
        self.assertNotIn("sends this webhook none of them", cell)
        self.assertNotIn("fails its own requests now", cell)
        # The order of the rules does not change which is named as failing.
        graded = grade([hook("mixed.example.com", [rule(["configmaps"]), pinned], policy="Fail")])
        self.assertEqual((graded["outage"][0]["live_rules"], graded["outage"][0]["pinned_rules"]), (["CREATE configmaps"], ["CREATE certificatesigningrequests in certificates.k8s.io at v1beta1"]))
        # Only a rule the list can vouch for is pinned: a wildcard at v1beta1 and a path
        # resource named beside one off the list at v1beta1 may still be served, so both keep
        # the failing-now sentence with nothing pinned.
        for rules in (
            [rule(["*"], groups=("*",), operations=("*",), versions=("v1beta1",))],
            [rule(["certificatesigningrequests", "clustertrustbundles"], groups=("certificates.k8s.io",), versions=("v1beta1",))],
            [rule(["certificatesigningrequests"], groups=("certificates.k8s.io", "*"), versions=("v1beta1",))],
        ):
            with self.subTest(rules=rules):
                graded = grade([hook("wide.example.com", rules, policy="Fail")])
                finding = graded["outage"][0]
                self.assertEqual((finding["version_pinned"], finding["pinned_rules"]), ([], []))
                self.assertEqual(finding["live_rules"], finding["rules"])
                self.assertIn("it fails its own requests now", r.describe_webhook_finding(finding))

    def test_monitoring_resources_are_outside_the_path(self):
        # GKE's managed Prometheus operator gates its own resources fail-closed.
        self.assertEqual(self._path([rule(["rules", "clusterrules", "globalrules"], groups=("monitoring.googleapis.com",), operations=("CREATE", "UPDATE"))]), [])

    def test_split_webhook_items(self):
        items = [webhook_config("ValidatingWebhookConfiguration", "v", []), webhook_config("MutatingWebhookConfiguration", "m", []), service("scen", "s"), endpoint_slice("scen", "s", [True]), {"kind": "PodDisruptionBudget"}, "junk"]
        configs, services, slices = r.split_webhook_items(items)
        self.assertEqual(([c["metadata"]["name"] for c in configs], len(services), len(slices)), (["v", "m"], 1, 1))


if __name__ == "__main__":
    unittest.main()
