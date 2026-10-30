#!/usr/bin/env python3
"""Unit tests for upgrade_readiness.py: the PDB, webhook, maintenance and skew rules on canned objects."""

import collections
import copy
import json
import os
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(__file__))
import readiness_rules as shared  # noqa: E402
import upgrade_readiness as r  # noqa: E402
from readiness_rules import audit_log  # noqa: E402

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


def rule_module(rule_id, entry=0, can_block=True, evaluate=None, describe=None):
    """A stand-in for a rule module: a class carrying the contract's attributes."""

    class Rule:
        RULE_ID = rule_id
        ENTRY = entry
        CAN_BLOCK = can_block

    Rule.evaluate = staticmethod(evaluate or (lambda cluster, member, read, target, context: shared.new_result()))
    Rule.describe = staticmethod(describe or (lambda finding: "described " + str(finding.get("what"))))
    return Rule


def bare_module(name, evaluate=None, **attributes):
    """A module object, as EXTRA_RULES carries, with the attributes given and nothing else."""
    module = types.ModuleType(name)
    module.evaluate = evaluate or (lambda cluster, member, read, target, context: shared.new_result())
    module.describe = lambda finding: "described " + str(finding.get("what"))
    for key, value in attributes.items():
        setattr(module, key, value)
    return module


def returning(**lists):
    def evaluate(cluster, member, read, target, context):
        out = shared.new_result()
        for key, value in lists.items():
            out[key].extend(value)
        return out

    return evaluate


class ExtraRulesTest(unittest.TestCase):
    """The fold: every registered rule runs over one member, findings carry their rule and
    text, an `unknown` carries whether its rule can block, and a rule's defect is its own
    unknown rather than a crash."""

    READ = {"kubeconfig": "k", "items": [], "error": None, "read_errors": {kind: None for kind in shared.READ_KINDS}}

    def test_findings_are_stamped_and_counted_per_rule(self):
        blocker = rule_module("blocker", entry=2, evaluate=returning(blocking=[{"what": "no room"}], notes=["pool p has room"]))
        risky = rule_module("risky", entry=11, can_block=False, evaluate=returning(risks=[{"what": "zonal"}], unknown=["odd location"]))
        folded = r.evaluate_extra_rules({}, {}, self.READ, TARGET, {}, rules=[blocker, risky])
        self.assertEqual(folded["blocking"], [{"what": "no room", "text": "described no room", "rule": "blocker", "entry": 2}])
        self.assertEqual(folded["risks"], [{"what": "zonal", "text": "described zonal", "rule": "risky", "entry": 11}])
        self.assertEqual(folded["unknown"], [{"reason": "odd location", "rule": "risky", "entry": 11, "can_block": False, "text": "risky: odd location"}])
        self.assertEqual(folded["notes"], ["blocker: pool p has room"])
        self.assertEqual(folded["rules"], {"blocker": {"entry": 2, "can_block": True, "blocking": 1, "risks": 0, "unknown": 0, "notes": 1}, "risky": {"entry": 11, "can_block": False, "blocking": 0, "risks": 1, "unknown": 1, "notes": 0}})

    def test_unknown_entries_keep_their_fields_and_gain_a_reason(self):
        b_style = {"rule": "x", "entry": 4, "grade": "unknown", "object": None, "detail": "the Node read failed"}
        c_style = {"rule": "x", "tier": "unknown", "reason": "no runner"}
        rule = rule_module("x", entry=4, evaluate=returning(unknown=[b_style, c_style, "plain"]))
        folded = r.evaluate_extra_rules({}, {}, self.READ, TARGET, {}, rules=[rule])
        self.assertEqual([u["reason"] for u in folded["unknown"]], ["the Node read failed", "no runner", "plain"])
        self.assertEqual(folded["unknown"][0]["detail"], "the Node read failed")
        self.assertEqual(folded["unknown"][1]["tier"], "unknown")
        self.assertTrue(all(u["can_block"] and u["rule"] == "x" and u["entry"] == 4 for u in folded["unknown"]))
        self.assertEqual(folded["unknown"][2]["text"], "x: plain")

    def test_a_rule_that_raises_is_unknown_not_a_crash(self):
        def boom(*args):
            raise ValueError("boom")

        broken = rule_module("broken", entry=0, evaluate=boom)
        fine = rule_module("fine", evaluate=returning(risks=[{"what": "ok"}]))
        folded = r.evaluate_extra_rules({}, {}, self.READ, TARGET, {}, rules=[broken, fine])
        self.assertEqual([(u["rule"], u["reason"], u["can_block"]) for u in folded["unknown"]], [("broken", "rule raised ValueError('boom'); not evaluated", True)])
        # The guard's own rendering never escapes it: an exception whose argument's repr raises
        # is named by its type, and one carrying a tenant-sized payload is cut.
        class Unprintable:
            def __repr__(self):
                raise RuntimeError("no repr")

        def boom_unprintable(*args):
            raise ValueError(Unprintable())

        def boom_huge(*args):
            raise ValueError("bad spec " + "x" * 5000)

        guarded = r.evaluate_extra_rules({}, {}, self.READ, TARGET, {}, rules=[rule_module("unprintable", evaluate=boom_unprintable), rule_module("huge", evaluate=boom_huge), fine])
        self.assertEqual([(u["rule"], u["reason"]) for u in guarded["unknown"]][0], ("unprintable", "rule raised ValueError; not evaluated"))
        self.assertTrue(guarded["unknown"][1]["reason"].startswith("rule raised ValueError('bad spec xxx"))
        self.assertLessEqual(len(guarded["unknown"][1]["reason"]), len("rule raised ; not evaluated") + shared.ERROR_EXCERPT_CHARS)
        self.assertEqual([f["rule"] for f in guarded["risks"]], ["fine"])
        json.dumps(guarded)
        self.assertEqual(folded["rules"]["broken"], {"entry": 0, "can_block": True, "blocking": 0, "risks": 0, "unknown": 1, "notes": 0})
        # `SystemExit` is a rule's defect too: `sys.exit()` in a module ported from a script
        # is a BaseException, and a guard on `Exception` alone let it end the run after
        # every member was read. A repr that exits is named by its type like one that
        # raises. The interpreter's own signals pass through.
        def boom_exit(*args):
            sys.exit(2)

        class ExitingRepr:
            def __repr__(self):
                raise SystemExit(3)

        def boom_exiting_repr(*args):
            raise ValueError(ExitingRepr())

        exited = r.evaluate_extra_rules({}, {}, self.READ, TARGET, {}, rules=[rule_module("exiting", evaluate=boom_exit), rule_module("exiting-repr", evaluate=boom_exiting_repr), fine])
        self.assertEqual([(u["rule"], u["reason"]) for u in exited["unknown"]], [("exiting", "rule raised SystemExit(2); not evaluated"), ("exiting-repr", "rule raised ValueError; not evaluated")])
        self.assertEqual([f["rule"] for f in exited["risks"]], ["fine"])
        json.dumps(exited)

        def interrupted(*args):
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            r.evaluate_extra_rules({}, {}, self.READ, TARGET, {}, rules=[rule_module("interrupted", evaluate=interrupted), fine])
        self.assertEqual([f["rule"] for f in folded["risks"]], ["fine"])
        # A describe that raises is the same defect, and a rule returning nothing is empty.
        bad_describe = rule_module("bad-describe", evaluate=returning(risks=[{"what": "x"}]), describe=boom)
        empty = rule_module("empty", evaluate=lambda *args: None)
        folded = r.evaluate_extra_rules({}, {}, self.READ, TARGET, {}, rules=[bad_describe, empty])
        self.assertEqual([u["rule"] for u in folded["unknown"]], ["bad-describe"])
        self.assertEqual(folded["rules"]["empty"], {"entry": 0, "can_block": True, "blocking": 0, "risks": 0, "unknown": 0, "notes": 0})

    def test_a_result_of_the_wrong_shape_is_one_unknown_not_a_crash_or_a_letter_each(self):
        # A string where a list was owed is the likeliest slip; read letter by letter it
        # would print one note per character. A dict, a list for the whole result, a finding
        # that is not a dict, a finding whose text is not a string and a note that is not a
        # string are the same defect: one unknown naming it, the other rules untouched.
        class Unlistable(dict):
            def items(self):
                raise OSError("no items")

        cases = {
            "string-unknown": (lambda *a: {"unknown": "no target to grade against"}, "unknown is str, not a list"),
            "dict-notes": (lambda *a: {**shared.new_result(), "notes": {"a": 1}}, "notes is dict, not a list"),
            "list-result": (lambda *a: [], "evaluate returned list, not a dict"),
            "string-finding": (lambda *a: {**shared.new_result(), "risks": ["context"]}, "a risks finding is str, not a dict"),
            "list-finding": (lambda *a: {**shared.new_result(), "blocking": [["text", 1]]}, "a blocking finding is list, not a dict"),
            "none-text": (lambda *a: {**shared.new_result(), "risks": [{"what": "x", "text": None}]}, "a risks finding's text is NoneType, not a string"),
            "int-note": (lambda *a: {**shared.new_result(), "notes": [3]}, "a note is int, not a string"),
            # A list under a key the contract does not name read as a clean rule; a key left out stays an empty list.
            "foreign-key": (lambda *a: {**shared.new_result(), "risk": [{"what": "pv"}]}, "the result carries ['risk'], not among ['blocking', 'risks', 'unknown', 'notes']"),
            "findings-key": (lambda *a: {"findings": [{"what": "pv"}]}, "the result carries ['findings'], not among ['blocking', 'risks', 'unknown', 'notes']"),
            # An unknown entry with neither a reason nor a detail was rendered as the dict's repr.
            "unknown-without-reason": (lambda *a: {**shared.new_result(), "unknown": [{"rule": "x", "entry": 4, "grade": "unknown", "object": None, "detail": ""}]}, "an unknown entry carries no reason or detail (keys ['detail', 'entry', 'grade', 'object', 'rule'])"),
            "unknown-int": (lambda *a: {**shared.new_result(), "unknown": [3]}, "an unknown entry is int, not a string or a dict with a reason"),
            # An empty or whitespace-only string is named as such, not as "str, not a string".
            "unknown-empty": (lambda *a: {**shared.new_result(), "unknown": [""]}, "an unknown entry is an empty string"),
            "unknown-blank": (lambda *a: {**shared.new_result(), "unknown": ["   "]}, "an unknown entry is an empty string"),
            # A blank finding text or note passes "is a string" and prints as nothing: named as blank.
            "blank-text": (lambda *a: {**shared.new_result(), "risks": [{"what": "x", "text": ""}]}, "a risks finding's text is blank"),
            "spaced-text": (lambda *a: {**shared.new_result(), "blocking": [{"what": "x", "text": " \t"}]}, "a blocking finding's text is blank"),
            "blank-note": (lambda *a: {**shared.new_result(), "notes": ["  "]}, "a note is blank"),
            # A value whose encoding raises something other than TypeError or ValueError is the
            # result's JSON defect, named as such rather than as the rule raising.
            "unlistable-value": (lambda *a: {**shared.new_result(), "risks": [{"what": Unlistable(a=1)}]}, "the result is not JSON-serialisable: OSError('no items')"),
        }
        fine = rule_module("fine", evaluate=returning(risks=[{"what": "ok"}]))
        for name, (evaluate, reason) in cases.items():
            broken = rule_module(name, evaluate=evaluate)
            folded = r.evaluate_extra_rules({}, {}, self.READ, TARGET, {}, rules=[broken, fine])
            self.assertEqual([(u["rule"], u["reason"]) for u in folded["unknown"]], [(name, reason)], name)
            self.assertEqual(folded["rules"][name], {"entry": 0, "can_block": True, "blocking": 0, "risks": 0, "unknown": 1, "notes": 0}, name)
            self.assertEqual([f["rule"] for f in folded["risks"]], ["fine"], name)
            self.assertEqual(folded["notes"], [], name)
        # A `describe` that renders empty is the same blank text.
        folded = r.evaluate_extra_rules({}, {}, self.READ, TARGET, {}, rules=[rule_module("mute", evaluate=returning(risks=[{"what": "x"}]), describe=lambda finding: ""), fine])
        self.assertEqual([(u["rule"], u["reason"]) for u in folded["unknown"]], [("mute", "a risks finding's text is blank")])
        # The bare reason is the fold's own probe's: a TypeError the rule itself raises is still
        # what it raised, under `rule raised`.
        def raising_type_error(*args):
            raise TypeError("mine")

        folded = r.evaluate_extra_rules({}, {}, self.READ, TARGET, {}, rules=[rule_module("typed", evaluate=raising_type_error), fine])
        self.assertEqual([(u["rule"], u["reason"]) for u in folded["unknown"]], [("typed", "rule raised TypeError('mine'); not evaluated")])

    def test_a_finding_json_cannot_carry_is_the_rules_defect_not_the_writers(self):
        # The findings go to `--output` as they are; a set, a datetime or a tuple-keyed dict
        # would fail json.dump after every member was read, so the fold refuses it here.
        # A NaN or infinite float is refused too: `json.dumps` would write it as a bare
        # token (`NaN`, `Infinity`) that no JSON parser reads.
        for name, value in (("set", {"a", "b"}), ("datetime", AT), ("tuple-keys", {(1, 2): "x"}), ("nan", float("nan")), ("inf", float("inf"))):
            broken = rule_module(name, evaluate=returning(risks=[{"what": value}]))
            fine = rule_module("fine", evaluate=returning(risks=[{"what": "ok"}]))
            folded = r.evaluate_extra_rules({}, {}, self.READ, TARGET, {}, rules=[broken, fine])
            self.assertEqual([u["rule"] for u in folded["unknown"]], [name], name)
            self.assertIn("the result is not JSON-serialisable", folded["unknown"][0]["reason"], name)
            self.assertEqual([f["rule"] for f in folded["risks"]], ["fine"], name)
            json.dumps(folded)

    def test_a_rules_bad_audit_log_cache_entry_is_its_unknown_not_the_reports_crash(self):
        # The report reads the audit-log summaries from the cache the rules fill, after the
        # fold returns; a rule that wrote another shape there raised out of `assess_readiness`
        # after every member was read. The fold checks the cache around each rule, so the
        # defect is that rule's `unknown`, and `summaries` hands the report only what `read`
        # stored.
        def context():
            return {shared.CONTEXT_PROJECT: "p", shared.CONTEXT_LOCATION: "us-central1-a", shared.CONTEXT_CLUSTER_NAME: "c", shared.CONTEXT_RUN_CMD: lambda cmd, timeout=None: (0, "[]", ""), shared.CONTEXT_AT: AT, shared.CONTEXT_CACHE: {}}

        def writing(value):
            def evaluate(cluster, member, read, target, ctx):
                ctx[shared.CONTEXT_CACHE][audit_log.CACHE_KEY] = value
                return shared.new_result()

            return evaluate

        def reading(cluster, member, read, target, ctx):
            audit_log.read_removed(ctx)
            return shared.new_result()

        fine = rule_module("fine", evaluate=returning(risks=[{"what": "ok"}]))
        clean = audit_log.read_removed(context())
        caller = audit_log.caller_records([{"protoPayload": {"authenticationInfo": {"principalEmail": "ops@example.com"}}}])[0]
        summary_only = {key: clean[key] for key in (audit_log.READ_KEY,) + audit_log.SUMMARY_KEYS}
        cases = {"list": [{"error": "x"}], "none-entry": {"removed": None}, "stranger": {"other": clean}, "nan-entry": {"removed": {**clean, "entries": float("nan")}}, "short": {"removed": {"read": "removed"}}, "summary-only": {"removed": summary_only}, "callers-int": {"removed": {**clean, "callers": 3}}, "caller-int": {"removed": {**clean, "callers": [3]}}, "caller-short": {"removed": {**clean, "callers": [{"principal": "ops@example.com"}]}}, "caller-count-str": {"removed": {**clean, "callers": [{**caller, "count": "9"}]}}}
        for name, value in cases.items():
            ctx = context()
            folded = r.evaluate_extra_rules({}, {}, self.READ, TARGET, ctx, rules=[rule_module(name, evaluate=writing(value)), fine])
            self.assertEqual([u["rule"] for u in folded["unknown"]], [name], name)
            self.assertTrue(folded["unknown"][0]["reason"].startswith("the rule left the audit-log cache unreadable: "), (name, folded["unknown"][0]["reason"]))
            self.assertEqual([f["rule"] for f in folded["risks"]], ["fine"], name)
            self.assertEqual(audit_log.summaries(ctx), {}, name)
            json.dumps(folded)
        # A read made through `audit_log.read` is kept, a later rule's damage to the other
        # entry is that rule's, and the damaged entry alone is left out of the summaries.
        def spoiling(cluster, member, read, target, ctx):
            ctx[shared.CONTEXT_CACHE][audit_log.CACHE_KEY]["deprecated"] = 3
            return shared.new_result()

        ctx = context()
        folded = r.evaluate_extra_rules({}, {}, self.READ, TARGET, ctx, rules=[rule_module("reader", evaluate=reading), rule_module("spoiler", evaluate=spoiling)])
        self.assertEqual([u["rule"] for u in folded["unknown"]], ["spoiler"])
        self.assertEqual(list(audit_log.summaries(ctx)), ["removed"])
        self.assertEqual({key: audit_log.summaries(ctx)["removed"][key] for key in ("entries", "pages", "sampled", "error")}, {"entries": 0, "pages": 1, "sampled": False, "error": None})
        self.assertEqual(set(audit_log.summaries(ctx)["removed"]), set(audit_log.SUMMARY_KEYS))
        # A defect already in the cache before a rule runs is not that rule's: a list under the
        # key, or the cache key holding None, which the fold replaces with one dict before the
        # first rule, so the key holds that dict afterwards (the snapshot tells a key holding
        # None apart from no key at all).
        ctx = context()
        ctx[shared.CONTEXT_CACHE][audit_log.CACHE_KEY] = [1]
        folded = r.evaluate_extra_rules({}, {}, self.READ, TARGET, ctx, rules=[fine])
        self.assertEqual((folded["unknown"], [f["rule"] for f in folded["risks"]]), ([], ["fine"]))
        none_cache = {shared.CONTEXT_CACHE: None}
        folded = r.evaluate_extra_rules({}, {}, self.READ, TARGET, none_cache, rules=[fine, rule_module("second", evaluate=returning(risks=[{"what": "s"}]))])
        self.assertEqual((folded["unknown"], [f["rule"] for f in folded["risks"]]), ([], ["fine", "second"]))
        # The fold replaces a cache that is not a plain dict once, before the first rule, so the
        # rules share one dict: a reading rule's result is cached for the next and summarised.
        self.assertEqual(none_cache, {shared.CONTEXT_CACHE: {}})
        # A defective entry under a read name (`None` here) is not a stored read: a rule that
        # replaces it through `read` is not charged with a lost read, so no restore runs and the
        # read stays for the rule after it, which reads it from the cache, and for the summary.
        def reading_removed(cluster, member, read, target, ctx):
            audit_log.read_removed(ctx)
            out = shared.new_result()
            out["risks"].append({"what": "rr"})
            return out

        inherited = context()
        inherited[shared.CONTEXT_CACHE][audit_log.CACHE_KEY] = {"removed": None}
        folded = r.evaluate_extra_rules({}, {}, self.READ, TARGET, inherited, rules=[rule_module("reader", evaluate=reading_removed), rule_module("second", evaluate=reading_removed)])
        self.assertEqual((folded["unknown"], [f["rule"] for f in folded["risks"]]), ([], ["reader", "second"]))
        self.assertEqual(sorted(audit_log.summaries(inherited)), ["removed"])
        # The same holds one level in and one level out. An inherited entry the probe faults for
        # a reason `read`'s own test did not see (every stored key but `read` missing) is read afresh, never
        # handed to a rule as the read; over `cache: None` the first reader's result is cached
        # for the second and summarised; and a cache of another class, whose `get` raises, is
        # replaced before the first rule, so no snapshot, probe or summary runs its methods.
        def reading_callers(cluster, member, read, target, ctx):
            log = audit_log.read_removed(ctx)
            out = shared.new_result()
            out["risks"].append({"what": sum(caller["count"] for caller in log["callers"])})
            return out

        short = context()
        short[shared.CONTEXT_CACHE][audit_log.CACHE_KEY] = {"removed": {"read": "removed"}}
        folded = r.evaluate_extra_rules({}, {}, self.READ, TARGET, short, rules=[rule_module("reader", evaluate=reading_callers), rule_module("second", evaluate=reading_callers)])
        self.assertEqual((folded["unknown"], [f["rule"] for f in folded["risks"]]), ([], ["reader", "second"]))
        self.assertEqual(sorted(audit_log.summaries(short)), ["removed"])
        # An entry of the summary's shape alone (`read` and the seven summary keys, no `callers`),
        # which a probe requiring the summary keys alone passed, and a caller record of another
        # shape are read afresh too: the `callers` a rule is handed is a list of records whatever
        # the inherited entry held, and the summary carries what `read` stored.
        for name, entry in {"summary-only": summary_only, "caller-short": {**clean, "callers": [{"principal": "ops@example.com"}]}}.items():
            inherited_shape = context()
            inherited_shape[shared.CONTEXT_CACHE][audit_log.CACHE_KEY] = {"removed": entry}
            folded = r.evaluate_extra_rules({}, {}, self.READ, TARGET, inherited_shape, rules=[rule_module("reader", evaluate=reading_callers), rule_module("second", evaluate=reading_callers)])
            self.assertEqual((folded["unknown"], [f["rule"] for f in folded["risks"]]), ([], ["reader", "second"]), name)
            self.assertEqual(sorted(audit_log.summaries(inherited_shape)), ["removed"], name)
        none_read = context()
        none_read[shared.CONTEXT_CACHE] = None
        folded = r.evaluate_extra_rules({}, {}, self.READ, TARGET, none_read, rules=[rule_module("reader", evaluate=reading_callers), rule_module("second", evaluate=reading_callers)])
        self.assertEqual((folded["unknown"], [f["rule"] for f in folded["risks"]]), ([], ["reader", "second"]))
        self.assertEqual(sorted(audit_log.summaries(none_read)), ["removed"])

        class BadCache(dict):
            def get(self, *args):
                raise OSError("no get")

        self.assertEqual(audit_log.cache_snapshot({shared.CONTEXT_CACHE: BadCache()})[1], None)
        self.assertEqual(audit_log.cache_defect({shared.CONTEXT_CACHE: BadCache()}), "the readiness context's cache is BadCache, not a dict")
        self.assertEqual(audit_log.summaries({shared.CONTEXT_CACHE: BadCache()}), {})
        bad = context()
        bad[shared.CONTEXT_CACHE] = BadCache()
        folded = r.evaluate_extra_rules({}, {}, self.READ, TARGET, bad, rules=[rule_module("reader", evaluate=reading_callers), fine])
        self.assertEqual((folded["unknown"], [f["rule"] for f in folded["risks"]]), ([], ["reader", "fine"]))
        self.assertEqual(sorted(audit_log.summaries(bad)), ["removed"])
        # A mapping of a rule's own class whose `items()` raises, inherited under the key, is a
        # defect named by its class and not the first rule's: no snapshot, probe or summary runs
        # its methods, nothing is charged, and a rule that reads replaces it with the plain dict
        # `read` keeps.
        class Unlistable(dict):
            def items(self):
                raise OSError("no items")

        poisoned = {shared.CONTEXT_CACHE: {audit_log.CACHE_KEY: Unlistable(removed=1)}}
        self.assertIsInstance(audit_log.cache_snapshot(poisoned)[1], audit_log.ForeignMapping)  # named by its class before its methods run
        self.assertEqual(audit_log.cache_defect(poisoned), "cache['audit_log'] is Unlistable, not a dict of read results")
        self.assertEqual(audit_log.summaries(poisoned), {})
        unlistable = context()
        unlistable[shared.CONTEXT_CACHE][audit_log.CACHE_KEY] = Unlistable(removed=1)
        folded = r.evaluate_extra_rules({}, {}, self.READ, TARGET, unlistable, rules=[rule_module("reader", evaluate=reading_removed), fine])
        self.assertEqual((folded["unknown"], [f["rule"] for f in folded["risks"]]), ([], ["reader", "fine"]))
        # A mapping of another class whose methods behave (an OrderedDict holding a sound read)
        # is a defect named by its class, not a cache: `read` keeps a plain dict, so the snapshot
        # agrees with it. Inherited, it is nobody's, and the rule that reads is not charged for
        # the plain dict `read` put in its place; written by a rule, it is that rule's, and the
        # reader after it starts from the cache as it stood before the writer.
        def reading_deprecated_log(cluster, member, read, target, ctx):
            audit_log.read_deprecated(ctx)
            out = shared.new_result()
            out["risks"].append({"what": "d"})
            return out

        ordered = context()
        audit_log.read_removed(ordered)
        ordered[shared.CONTEXT_CACHE][audit_log.CACHE_KEY] = collections.OrderedDict(ordered[shared.CONTEXT_CACHE][audit_log.CACHE_KEY])
        self.assertEqual(audit_log.cache_defect(ordered), "cache['audit_log'] is OrderedDict, not a dict of read results")
        self.assertEqual(audit_log.summaries(ordered), {})
        folded = r.evaluate_extra_rules({}, {}, self.READ, TARGET, ordered, rules=[rule_module("reader", evaluate=reading_deprecated_log), fine])
        self.assertEqual((folded["unknown"], [f["rule"] for f in folded["risks"]]), ([], ["reader", "fine"]))
        self.assertEqual(sorted(audit_log.summaries(ordered)), ["deprecated"])

        def reading_then_reordering(cluster, member, read, target, ctx):
            audit_log.read_removed(ctx)
            ctx[shared.CONTEXT_CACHE][audit_log.CACHE_KEY] = collections.OrderedDict(ctx[shared.CONTEXT_CACHE][audit_log.CACHE_KEY])
            out = shared.new_result()
            out["risks"].append({"what": "w"})
            return out

        reordered = context()
        folded = r.evaluate_extra_rules({}, {}, self.READ, TARGET, reordered, rules=[rule_module("writer", evaluate=reading_then_reordering), rule_module("reader", evaluate=reading_deprecated_log)])
        self.assertEqual([u["rule"] for u in folded["unknown"]], ["writer"])
        self.assertIn("cache['audit_log'] is OrderedDict, not a dict of read results", folded["unknown"][0]["reason"])
        self.assertEqual([f["rule"] for f in folded["risks"]], ["reader"])
        self.assertEqual(sorted(audit_log.summaries(reordered)), ["deprecated"])
        # A writer that leaves, under the cache key, an object of its own class whose
        # `__deepcopy__` returns a dict of the stored shape: the snapshot names it by its class
        # and never copies it (a copy would run that method, and the writer would pass as clean
        # while the reader after it was charged with a read the fabricated snapshot held), so
        # the writer is charged at its own check, the key is dropped, and the reader after it
        # reads afresh and keeps its findings.
        probe = context()
        audit_log.read_removed(probe)
        stored = copy.deepcopy(probe[shared.CONTEXT_CACHE][audit_log.CACHE_KEY])

        class Copier:
            def __deepcopy__(self, memo):
                return copy.deepcopy(stored)

        def storing_copier(cluster, member, read, target, ctx):
            ctx[shared.CONTEXT_CACHE][audit_log.CACHE_KEY] = Copier()
            out = shared.new_result()
            out["risks"].append({"what": "w"})
            return out

        copied = context()
        folded = r.evaluate_extra_rules({}, {}, self.READ, TARGET, copied, rules=[rule_module("writer", evaluate=storing_copier), rule_module("reader", evaluate=reading_deprecated_log)])
        self.assertEqual([u["rule"] for u in folded["unknown"]], ["writer"])
        self.assertIn("cache['audit_log'] is Copier, not a dict of read results", folded["unknown"][0]["reason"])
        self.assertEqual([f["rule"] for f in folded["risks"]], ["reader"])
        self.assertEqual(sorted(audit_log.summaries(copied)), ["deprecated"])
        # A value of a JSON type's subclass a writer leaves inside a read it created (its own
        # snapshot held no read to compare), and a stored entry of a dict subclass whose `get`
        # raises: both are named at the writer's own check by the exact-type walk, so no
        # comparison, snapshot or later read runs the object's methods, the rules after the
        # writer are not charged, and a reader after it reads afresh.
        class EvilInt(int):
            def __eq__(self, other):
                raise ValueError("cmp")

            __hash__ = int.__hash__

        class SubGet(dict):
            def get(self, *args):
                raise OSError("no get")

        def reading_then_poisoning_value(cluster, member, read, target, ctx):
            audit_log.read_removed(ctx)
            ctx[shared.CONTEXT_CACHE][audit_log.CACHE_KEY]["removed"]["entries"] = EvilInt(2)
            out = shared.new_result()
            out["risks"].append({"what": "w"})
            return out

        def reading_then_subclassing_entry(cluster, member, read, target, ctx):
            audit_log.read_removed(ctx)
            logs = ctx[shared.CONTEXT_CACHE][audit_log.CACHE_KEY]
            logs["removed"] = SubGet(logs["removed"])
            out = shared.new_result()
            out["risks"].append({"what": "w"})
            return out

        for name, writer, detail in (("evil-value", reading_then_poisoning_value, "the 'removed' entry holds a value of class EvilInt at the entry['entries'], not a JSON value"), ("subclass-entry", reading_then_subclassing_entry, "the 'removed' entry holds a value of class SubGet at the entry, not a JSON value")):
            poisoned_ctx = context()
            folded = r.evaluate_extra_rules({}, {}, self.READ, TARGET, poisoned_ctx, rules=[rule_module(name, evaluate=writer), fine, rule_module("reader", evaluate=reading_removed)])
            self.assertEqual([u["rule"] for u in folded["unknown"]], [name], name)
            self.assertIn(detail, folded["unknown"][0]["reason"], name)
            self.assertEqual([f["rule"] for f in folded["risks"]], ["fine", "reader"], name)
            self.assertEqual(sorted(audit_log.summaries(poisoned_ctx)), ["removed"], name)
        self.assertEqual((audit_log.summaries(ctx), audit_log.summaries({}), audit_log.summaries({shared.CONTEXT_CACHE: None}), audit_log.cache_defect({})), ({}, {}, {}, None))
        self.assertIsNotNone(audit_log.cache_defect({shared.CONTEXT_CACHE: None}))
        # A rule's damage is put back before the next rule runs: the next rule's read works
        # and is kept, and each later offender is charged on its own, not masked by the first.
        def reading_with_risk(cluster, member, read, target, ctx):
            audit_log.read_removed(ctx)
            out = shared.new_result()
            out["risks"].append({"what": "r"})
            return out

        def spoiling_entry(which, value):
            def evaluate(cluster, member, read, target, ctx):
                ctx[shared.CONTEXT_CACHE][audit_log.CACHE_KEY][which] = value
                return shared.new_result()

            return evaluate

        def nulling(cluster, member, read, target, ctx):
            ctx[shared.CONTEXT_CACHE] = None
            return shared.new_result()

        ctx = context()
        rules = [rule_module("list", evaluate=writing([1])), rule_module("reader", evaluate=reading_with_risk), rule_module("nuller", evaluate=spoiling_entry("removed", None)), rule_module("stranger", evaluate=spoiling_entry("deprecated", 3)), fine]
        folded = r.evaluate_extra_rules({}, {}, self.READ, TARGET, ctx, rules=rules)
        self.assertEqual([u["rule"] for u in folded["unknown"]], ["list", "nuller", "stranger"])
        self.assertEqual([f["rule"] for f in folded["risks"]], ["reader", "fine"])
        self.assertEqual(list(audit_log.summaries(ctx)), ["removed"])
        ctx = context()
        folded = r.evaluate_extra_rules({}, {}, self.READ, TARGET, ctx, rules=[rule_module("nulling", evaluate=nulling), rule_module("reader", evaluate=reading_with_risk)])
        self.assertEqual(([u["rule"] for u in folded["unknown"]], [f["rule"] for f in folded["risks"]], list(audit_log.summaries(ctx))), ([], ["reader"], ["removed"]))
        # The cache is put back on the raising path too, so the raising rule is the one
        # charged and the next rule reads what `read` stored; and a well-formed overwrite
        # that drops or changes a stored read is damage, not a clean cache.
        def damaging_raiser(cluster, member, read, target, ctx):
            ctx[shared.CONTEXT_CACHE][audit_log.CACHE_KEY]["removed"] = None
            raise ValueError("boom")

        def nulling_raiser(cluster, member, read, target, ctx):
            ctx[shared.CONTEXT_CACHE] = None
            raise ValueError("boom")

        def emptying(cluster, member, read, target, ctx):
            ctx[shared.CONTEXT_CACHE][audit_log.CACHE_KEY] = {}
            return shared.new_result()

        def replacing(cluster, member, read, target, ctx):
            ctx[shared.CONTEXT_CACHE] = {audit_log.CACHE_KEY: dict(ctx[shared.CONTEXT_CACHE][audit_log.CACHE_KEY])}
            return shared.new_result()

        def reading_deprecated(cluster, member, read, target, ctx):
            audit_log.read_deprecated(ctx)
            out = shared.new_result()
            out["risks"].append({"what": "d"})
            return out

        for name, offender in (("damaging-raiser", damaging_raiser), ("nulling-raiser", nulling_raiser)):
            ctx = context()
            folded = r.evaluate_extra_rules({}, {}, self.READ, TARGET, ctx, rules=[rule_module("reader", evaluate=reading_with_risk), rule_module(name, evaluate=offender), rule_module("second", evaluate=reading_deprecated)])
            self.assertEqual([(u["rule"], u["reason"]) for u in folded["unknown"]], [(name, "rule raised ValueError('boom'); not evaluated")], name)
            self.assertEqual([f["rule"] for f in folded["risks"]], ["reader", "second"], name)
            self.assertEqual(sorted(audit_log.summaries(ctx)), ["deprecated", "removed"], name)
        ctx = context()
        folded = r.evaluate_extra_rules({}, {}, self.READ, TARGET, ctx, rules=[rule_module("reader", evaluate=reading_with_risk), rule_module("emptying", evaluate=emptying), rule_module("second", evaluate=reading_deprecated)])
        self.assertEqual([u["rule"] for u in folded["unknown"]], ["emptying"])
        self.assertIn("the rule left the audit-log cache unreadable: the 'removed' read stored before the rule is gone or changed", folded["unknown"][0]["reason"])
        self.assertEqual(sorted(audit_log.summaries(ctx)), ["deprecated", "removed"])
        # A rule that replaces or nulls the cache *in its own context* reaches nobody: each
        # rule is handed its own context, and only the shared cache object is held to.
        for name, offender in (("replacing", replacing), ("nulling", nulling)):
            ctx = context()
            folded = r.evaluate_extra_rules({}, {}, self.READ, TARGET, ctx, rules=[rule_module("reader", evaluate=reading_with_risk), rule_module(name, evaluate=offender), rule_module("second", evaluate=reading_deprecated)])
            self.assertEqual((folded["unknown"], [f["rule"] for f in folded["risks"]], sorted(audit_log.summaries(ctx))), ([], ["reader", "second"], ["deprecated", "removed"]), name)
        # An object the snapshot cannot copy (a generator) is the storing rule's damage, named,
        # and the restore still runs, so the next rule is neither charged nor stopped.
        def storing_generator(cluster, member, read, target, ctx):
            ctx[shared.CONTEXT_CACHE][audit_log.CACHE_KEY]["scratch"] = (e for e in [1, 2])
            return shared.new_result()

        def poisoning_callers(cluster, member, read, target, ctx):
            ctx[shared.CONTEXT_CACHE][audit_log.CACHE_KEY]["removed"]["callers"].append((e for e in [1]))
            return shared.new_result()

        for name, offender, reason in (("generator", storing_generator, "cache['audit_log'] carries 'scratch', which is not one of the reads"), ("poisoning", poisoning_callers, "the 'removed' entry holds a value of class generator at the entry['callers']")):
            ctx = context()
            folded = r.evaluate_extra_rules({}, {}, self.READ, TARGET, ctx, rules=[rule_module("reader", evaluate=reading_with_risk), rule_module(name, evaluate=offender), rule_module("second", evaluate=reading_deprecated)])
            self.assertEqual([u["rule"] for u in folded["unknown"]], [name], name)
            self.assertIn(reason, folded["unknown"][0]["reason"], name)
            self.assertEqual([f["rule"] for f in folded["risks"]], ["reader", "second"], name)
            self.assertEqual(sorted(audit_log.summaries(ctx)), ["deprecated", "removed"], name)
            self.assertEqual(audit_log.cache_defect(ctx), None, name)
        # A value of a JSON type's subclass whose comparison would raise with an unprintable
        # argument: the by-class walk names it before any comparison runs, the rule is charged,
        # and the restore runs for the next rule.
        class Unprintable:
            def __repr__(self):
                raise RuntimeError("no repr")

        class Evil(int):  # an int subclass: the walk names it by class, so its `__eq__` never runs
            def __eq__(self, other):
                raise ValueError(Unprintable())

            __hash__ = int.__hash__

        def poisoning_compare(cluster, member, read, target, ctx):
            ctx[shared.CONTEXT_CACHE][audit_log.CACHE_KEY]["removed"]["page_limit"] = Evil(1000)
            return shared.new_result()

        ctx = context()
        folded = r.evaluate_extra_rules({}, {}, self.READ, TARGET, ctx, rules=[rule_module("reader", evaluate=reading_with_risk), rule_module("evil", evaluate=poisoning_compare), rule_module("second", evaluate=reading_deprecated)])
        self.assertEqual([u["rule"] for u in folded["unknown"]], ["evil"])
        self.assertIn("the 'removed' entry holds a value of class Evil at the entry['page_limit'], not a JSON value", folded["unknown"][0]["reason"])
        self.assertEqual([f["rule"] for f in folded["risks"]], ["reader", "second"])
        self.assertEqual(sorted(audit_log.summaries(ctx)), ["deprecated", "removed"])
        # The same holds when what a rule stored would exit instead of raising: an object whose
        # copy would raise SystemExit and an int subclass whose comparison would are both named
        # by class before either method runs, the rule is charged either way, and the run goes on.
        class ExitingCopy:
            def __deepcopy__(self, memo):
                raise SystemExit(4)

        class ExitingCompare(int):
            def __eq__(self, other):
                raise SystemExit(5)

            __hash__ = int.__hash__

        def storing_exiting_copy(cluster, member, read, target, ctx):
            ctx[shared.CONTEXT_CACHE][audit_log.CACHE_KEY]["removed"]["callers"].append(ExitingCopy())
            return shared.new_result()

        def storing_exiting_compare(cluster, member, read, target, ctx):
            ctx[shared.CONTEXT_CACHE][audit_log.CACHE_KEY]["removed"]["page_limit"] = ExitingCompare(1000)
            return shared.new_result()

        for name, offender, reason in (("exiting-copy", storing_exiting_copy, "the 'removed' entry holds a value of class ExitingCopy at the entry['callers']"), ("exiting-compare", storing_exiting_compare, "the 'removed' entry holds a value of class ExitingCompare at the entry['page_limit'], not a JSON value")):
            ctx = context()
            folded = r.evaluate_extra_rules({}, {}, self.READ, TARGET, ctx, rules=[rule_module("reader", evaluate=reading_with_risk), rule_module(name, evaluate=offender), rule_module("second", evaluate=reading_deprecated)])
            self.assertEqual([u["rule"] for u in folded["unknown"]], [name], name)
            self.assertIn(reason, folded["unknown"][0]["reason"], name)
            self.assertEqual([f["rule"] for f in folded["risks"]], ["reader", "second"], name)
            self.assertEqual(sorted(audit_log.summaries(ctx)), ["deprecated", "removed"], name)
        # A rule that reads a window and then leaves a plain object in the shared copy's
        # `callers` -- one that copies cleanly and compares by identity -- is charged at its own
        # cache check as a shape defect; before the probe read the whole result, it passed as
        # sound and every later rule was charged with a changed read, its findings lost.
        class Plain:
            pass

        def reading_then_storing_plain(cluster, member, read, target, ctx):
            audit_log.read_removed(ctx)
            ctx[shared.CONTEXT_CACHE][audit_log.CACHE_KEY]["removed"]["callers"].append(Plain())
            return shared.new_result()

        ctx = context()
        folded = r.evaluate_extra_rules({}, {}, self.READ, TARGET, ctx, rules=[rule_module("writer", evaluate=reading_then_storing_plain), rule_module("second", evaluate=reading_deprecated)])
        self.assertEqual([u["rule"] for u in folded["unknown"]], ["writer"])
        self.assertIn("the 'removed' entry holds a value of class Plain at the entry['callers']", folded["unknown"][0]["reason"])
        self.assertEqual([f["rule"] for f in folded["risks"]], ["second"])
        self.assertEqual(sorted(audit_log.summaries(ctx)), ["deprecated"])
        # A generator in the cache before the fold ran is nobody's: the snapshot stands, the
        # rules run, and the entry is left as it was found.
        ctx = context()
        ctx[shared.CONTEXT_CACHE][audit_log.CACHE_KEY] = {"scratch": (e for e in [1])}
        folded = r.evaluate_extra_rules({}, {}, self.READ, TARGET, ctx, rules=[rule_module("reader", evaluate=reading_with_risk), fine])
        self.assertEqual(([u["rule"] for u in folded["unknown"]], [f["rule"] for f in folded["risks"]]), ([], ["reader", "fine"]))
        self.assertIn("scratch", ctx[shared.CONTEXT_CACHE][audit_log.CACHE_KEY])
        # What `read` returns is the rule's own copy: sorting or filtering it in place is
        # not damage, and the rule keeps its findings.
        def sorting_reader(cluster, member, read, target, ctx):
            log = audit_log.read_removed(ctx)
            log["callers"].sort(key=lambda c: -c.get("count", 0))
            log["callers"][:] = []
            out = shared.new_result()
            out["blocking"].append({"what": "sorted"})
            return out

        ctx = context()
        folded = r.evaluate_extra_rules({}, {}, self.READ, TARGET, ctx, rules=[rule_module("reader", evaluate=reading_with_risk), rule_module("sorter", evaluate=sorting_reader), rule_module("second", evaluate=reading_deprecated)])
        self.assertEqual(folded["unknown"], [])
        self.assertEqual([f["rule"] for f in folded["blocking"]], ["sorter"])
        self.assertEqual(sorted(audit_log.summaries(ctx)), ["deprecated", "removed"])
        # A read name that is not one of the reads is the rule's own error, named as the call.
        def misnamed_reader(cluster, member, read, target, ctx):
            audit_log.read(ctx, "deprecate")
            return shared.new_result()

        ctx = context()
        folded = r.evaluate_extra_rules({}, {}, self.READ, TARGET, ctx, rules=[rule_module("misnamed", evaluate=misnamed_reader), fine])
        self.assertEqual([(u["rule"], u["reason"]) for u in folded["unknown"]], [("misnamed", "rule raised ValueError(\"audit_log.read('deprecate'): not one of the reads ('removed', 'deprecated')\"); not evaluated")])
        self.assertEqual(audit_log.summaries(ctx), {})

    def test_each_rule_is_handed_its_own_copies_so_its_writes_reach_nobody(self):
        # `cluster`, `member`, `read` and the context's fleet records reached every rule by
        # reference, and the report read them after the fold: a rule narrowing `read["items"]`
        # in place emptied the list for every later rule, and one nulling a peer's maintenance
        # policy changed what every later member was graded from.
        def clearing(cluster, member, read, target, ctx):
            read["items"].clear()
            read["read_errors"]["pods"] = "mine"
            member["cluster"] = "renamed"
            cluster["name"] = "renamed"
            return shared.new_result()

        def nulling_peer(cluster, member, read, target, ctx):
            for peer in ctx[shared.CONTEXT_CLUSTERS]:
                peer["maintenancePolicy"] = None
            ctx[shared.CONTEXT_POOLS].clear()
            return shared.new_result()

        def counting(cluster, member, read, target, ctx):
            out = shared.new_result()
            out["notes"].append(f"{len(read['items'])} item(s); {len(ctx[shared.CONTEXT_CLUSTERS])} peer(s) with policy {[p.get('maintenancePolicy') for p in ctx[shared.CONTEXT_CLUSTERS]]}; {len(ctx[shared.CONTEXT_POOLS])} pool(s); {member['cluster']} {cluster['name']}")
            return out

        read = {"kubeconfig": "k", "items": [{"kind": "Pod"}, {"kind": "Node"}], "error": None, "read_errors": {kind: None for kind in shared.READ_KINDS}}
        peers = [{"name": "peer", "maintenancePolicy": {"window": {}}}]
        ctx = {shared.CONTEXT_CLUSTERS: peers, shared.CONTEXT_POOLS: [{"name": "p"}], shared.CONTEXT_CACHE: {}}
        cluster, member = {"name": "c"}, {"cluster": "c"}
        folded = r.evaluate_extra_rules(cluster, member, read, TARGET, ctx, rules=[rule_module("clearing", evaluate=clearing), rule_module("nulling-peer", evaluate=nulling_peer), rule_module("counting", evaluate=counting)])
        self.assertEqual(folded["unknown"], [])
        self.assertEqual(folded["notes"], ["counting: 2 item(s); 1 peer(s) with policy [{'window': {}}]; 1 pool(s); c c"])
        self.assertEqual((read["items"], read["read_errors"]["pods"], member["cluster"], cluster["name"]), ([{"kind": "Pod"}, {"kind": "Node"}], None, "c", "c"))
        self.assertEqual((peers, ctx[shared.CONTEXT_POOLS]), ([{"name": "peer", "maintenancePolicy": {"window": {}}}], [{"name": "p"}]))

    def test_findings_are_copied_before_they_are_stamped(self):
        shared_finding = {"what": "pool p"}
        first = rule_module("first", entry=2, evaluate=lambda *a: {**shared.new_result(), "risks": [shared_finding]}, describe=lambda f: "first's text")
        second = rule_module("second", entry=12, evaluate=lambda *a: {**shared.new_result(), "risks": [shared_finding, shared_finding]}, describe=lambda f: "second's text")
        folded = r.evaluate_extra_rules({}, {}, self.READ, TARGET, {}, rules=[first, second])
        # The rule's own object is untouched, and each fold copy carries its own rule and text.
        self.assertEqual(shared_finding, {"what": "pool p"})
        self.assertEqual([(f["rule"], f["entry"], f["text"]) for f in folded["risks"]], [("first", 2, "first's text"), ("second", 12, "second's text"), ("second", 12, "second's text")])
        self.assertEqual(len({id(f) for f in folded["risks"]}), 3)
        self.assertEqual((folded["rules"]["first"]["risks"], folded["rules"]["second"]["risks"]), (1, 2))

    def test_cell_text_is_one_line_and_a_reason_is_cut_while_the_json_keeps_it(self):
        two_lines = "E1013 memcache.go couldn't get current server API group list\nUnable to connect to the server: dial tcp: i/o timeout\n" + "x" * 300
        rule = rule_module("multi", evaluate=returning(unknown=[two_lines], risks=[{"what": "x", "text": "line one\nline two"}], notes=["note\nwith a break"]))
        folded = r.evaluate_extra_rules({}, {}, self.READ, TARGET, {}, rules=[rule])
        unknown = folded["unknown"][0]
        self.assertEqual(unknown["reason"], two_lines)
        self.assertNotIn("\n", unknown["text"])
        self.assertTrue(unknown["text"].startswith("multi: E1013 memcache.go couldn't get current server API group list Unable to connect"))
        self.assertEqual(len(unknown["text"]), len("multi: ") + shared.NOTE_EXCERPT_CHARS)
        self.assertEqual(folded["risks"][0]["text"], "line one line two")
        self.assertEqual(folded["notes"], ["multi: note with a break"])

    def test_findings_are_stamped_inside_the_guard(self):
        # Stamping and rendering happen before the fold returns, so every blocker and risk
        # the fold emits is a dict with a string text, its rule and its entry.
        rule = rule_module("stamped", entry=19, evaluate=returning(blocking=[{"what": "pv"}], risks=[{"what": "pv2", "text": "own"}]))
        folded = r.evaluate_extra_rules({}, {}, self.READ, TARGET, {}, rules=[rule])
        for finding in folded["blocking"] + folded["risks"]:
            self.assertEqual((finding["rule"], finding["entry"]), ("stamped", 19))
            self.assertIsInstance(finding["text"], str)

    def test_module_attributes_are_checked_before_anything_is_derived_from_them(self):
        # RULE_ID keys `rules`, so a list raised `unhashable type` outside the guard and
        # killed the run after every member was read; ENTRY is stamped on the per-rule
        # record and the unknown entries, which a probe of the result alone never saw, so a
        # set reached the writer whenever the rule filed no blocker or risk; bool("False")
        # is True. Each is one unknown under the module's name, unevaluated, the other rules
        # untouched, and the fold is JSON.
        fine = rule_module("fine", evaluate=returning(risks=[{"what": "ok"}]))
        cases = {
            "surge_capacity": (dict(RULE_ID=["surge", "capacity"], ENTRY=2, CAN_BLOCK=True), None, "RULE_ID is list, not a string; not evaluated"),
            "quiet_entry": (dict(RULE_ID="quiet", ENTRY={11}, CAN_BLOCK=True), None, "ENTRY is set, not a JSON value; not evaluated"),
            "loud_entry": (dict(RULE_ID="loud", ENTRY={11}, CAN_BLOCK=True), returning(blocking=[{"what": "b"}]), "ENTRY is set, not a JSON value; not evaluated"),
            "stringly": (dict(RULE_ID="stringly", ENTRY=5, CAN_BLOCK="False"), None, "CAN_BLOCK is str, not a bool; not evaluated"),
            "nan_entry": (dict(RULE_ID="nan", ENTRY=float("nan"), CAN_BLOCK=True), None, "ENTRY is float, not a JSON value; not evaluated"),
            # A blank id passes "is a string" and would key `rules` and print as nothing.
            "blank_id": (dict(RULE_ID="", ENTRY=2, CAN_BLOCK=True), None, "RULE_ID is blank; not evaluated"),
            "spaced_id": (dict(RULE_ID="  ", ENTRY=2, CAN_BLOCK=True), None, "RULE_ID is blank; not evaluated"),
            # HOLDS, when declared, names the moves the contract knows, in a non-empty tuple.
            "holds_str": (dict(RULE_ID="holdsy", ENTRY=5, CAN_BLOCK=True, HOLDS="cluster"), None, "HOLDS is str, not a non-empty tuple of cluster or node-pool; not evaluated"),
            "holds_value": (dict(RULE_ID="holdsv", ENTRY=5, CAN_BLOCK=True, HOLDS=("pods",)), None, "HOLDS is tuple, not a non-empty tuple of cluster or node-pool; not evaluated"),
            "holds_empty": (dict(RULE_ID="holdse", ENTRY=5, CAN_BLOCK=True, HOLDS=()), None, "HOLDS is tuple, not a non-empty tuple of cluster or node-pool; not evaluated"),
        }
        for name, (attributes, evaluate, reason) in cases.items():
            broken = bare_module(name, evaluate=evaluate, **attributes)
            folded = r.evaluate_extra_rules({}, {}, self.READ, TARGET, {}, rules=[broken, fine])
            self.assertEqual([(u["rule"], u["entry"], u["can_block"], u["reason"], u["text"]) for u in folded["unknown"]], [(name, None, True, reason, f"{name}: {reason}")], name)
            self.assertEqual(folded["rules"][name], {"entry": None, "can_block": True, "blocking": 0, "risks": 0, "unknown": 1, "notes": 0}, name)
            self.assertEqual(folded["rules"]["fine"], {"entry": 0, "can_block": True, "blocking": 0, "risks": 1, "unknown": 0, "notes": 0}, name)
            self.assertEqual((folded["blocking"], [f["rule"] for f in folded["risks"]], folded["notes"]), ([], ["fine"], []), name)
            json.dumps(folded)  # what the writer does with it
        # A declared HOLDS is stamped on each blocker as `holds`; a module that declares none stamps nothing.
        holder = bare_module("holder", evaluate=returning(blocking=[{"what": "b"}]), RULE_ID="holder", ENTRY=22, CAN_BLOCK=True, HOLDS=(shared.HOLDS_NODE_POOL,))
        silent = bare_module("silent", evaluate=returning(blocking=[{"what": "s"}]), RULE_ID="silent", ENTRY=5, CAN_BLOCK=True)
        folded = r.evaluate_extra_rules({}, {}, self.READ, TARGET, {}, rules=[holder, silent])
        self.assertEqual([(f["rule"], f.get("holds")) for f in folded["blocking"]], [("holder", ["node-pool"]), ("silent", None)])
        self.assertEqual(folded["unknown"], [])
        # An `unknown` entry carries the module's HOLDS too, so a reader knows which move the rule could not grade.
        unread = bare_module("unread", evaluate=returning(unknown=["the instances were not read"]), RULE_ID="unread", ENTRY=22, CAN_BLOCK=True, HOLDS=(shared.HOLDS_NODE_POOL,))
        folded = r.evaluate_extra_rules({}, {}, self.READ, TARGET, {}, rules=[unread, silent])
        self.assertEqual([(u["rule"], u["can_block"], u.get("holds")) for u in folded["unknown"]], [("unread", True, ["node-pool"])])
        # A rule that raises, or returns a shape the contract forbids, carries its HOLDS on the unknown the fold files for it:
        # the attributes were read before it ran, and the audit would otherwise read the row as holding both moves.
        def raising(cluster, member, read, target, context):
            raise ValueError("one odd record")

        raiser = bare_module("raiser", evaluate=raising, RULE_ID="raiser", ENTRY=22, CAN_BLOCK=True, HOLDS=(shared.HOLDS_NODE_POOL,))
        malformed = bare_module("malformed", evaluate=lambda cluster, member, read, target, context: {"blocking": "not a list"}, RULE_ID="malformed", ENTRY=21, CAN_BLOCK=True, HOLDS=(shared.HOLDS_NODE_POOL,))
        folded = r.evaluate_extra_rules({}, {}, self.READ, TARGET, {}, rules=[raiser, malformed, silent])
        self.assertEqual([(u["rule"], u["can_block"], u.get("holds")) for u in folded["unknown"]], [("raiser", True, ["node-pool"]), ("malformed", True, ["node-pool"])])
        self.assertTrue(folded["unknown"][0]["reason"].startswith("rule raised ValueError('one odd record')"), folded["unknown"][0]["reason"])
        # An attribute whose read itself raises is the same defect under the fold's guard: a
        # module that resolves its names through a module-level `__getattr__` (the lazy-import
        # idiom) and raises there is one unknown under its `__name__`; an ENTRY whose encoding
        # raises something other than the TypeError or ValueError the probe caught (a dict
        # subclass whose `items()` raises, as a value nested past the recursion limit does) is
        # "not a JSON value"; an object nothing can be read from is named by its type. The
        # interpreter's own interrupt still passes through.
        def no_backend(name):
            raise ImportError("no backend")

        lazy = types.ModuleType("lazy")
        lazy.__getattr__ = no_backend

        class Unlistable(dict):
            def items(self):
                raise OSError("no items")

        bottomless = bare_module("bottomless", evaluate=None, RULE_ID="bottomless", ENTRY=Unlistable(entry=2), CAN_BLOCK=True)

        class Unreadable:
            def __getattr__(self, name):
                raise KeyError(name)

            def __str__(self):
                raise RuntimeError("no str")

        folded = r.evaluate_extra_rules({}, {}, self.READ, TARGET, {}, rules=[lazy, bottomless, Unreadable(), fine])
        self.assertEqual([(u["rule"], u["entry"], u["can_block"], u["reason"]) for u in folded["unknown"]], [
            ("lazy", None, True, "reading the module's attributes raised ImportError('no backend'); not evaluated"),
            ("bottomless", None, True, "ENTRY is Unlistable, not a JSON value; not evaluated"),
            ("Unreadable", None, True, "reading the module's attributes raised KeyError('__name__'); not evaluated"),
        ])
        self.assertEqual([f["rule"] for f in folded["risks"]], ["fine"])
        self.assertEqual(folded["rules"]["lazy"], {"entry": None, "can_block": True, "blocking": 0, "risks": 0, "unknown": 1, "notes": 0})
        json.dumps(folded)

        def interrupting(name):
            raise KeyboardInterrupt

        interrupted = types.ModuleType("interrupted")
        interrupted.__getattr__ = interrupting
        with self.assertRaises(KeyboardInterrupt):
            r.evaluate_extra_rules({}, {}, self.READ, TARGET, {}, rules=[interrupted, fine])
        # A `RULE_ID` that is a `str` subclass is keyed and printed as the plain string it spells,
        # since the id keys `rules` and formats the notes after the guard closes: one whose
        # `__hash__` raises is evaluated under that string; one whose `__str__` raises is the
        # module's defect under its module name.
        class Hashless(str):
            def __hash__(self):
                raise TypeError("no hash")

        class Strless(str):
            def __str__(self):
                raise RuntimeError("no str")

        hashless = bare_module("hashless", evaluate=returning(risks=[{"what": "h"}]), RULE_ID=Hashless("hash-less"), ENTRY=7, CAN_BLOCK=True)
        strless = bare_module("strless", evaluate=None, RULE_ID=Strless("str-less"), ENTRY=8, CAN_BLOCK=True)
        folded = r.evaluate_extra_rules({}, {}, self.READ, TARGET, {}, rules=[hashless, strless, fine])
        self.assertEqual(sorted(folded["rules"]), ["fine", "hash-less", "strless"])
        self.assertTrue(all(type(key) is str for key in folded["rules"]))
        self.assertEqual([f["rule"] for f in folded["risks"]], ["hash-less", "fine"])
        self.assertEqual([(u["rule"], u["reason"]) for u in folded["unknown"]], [("strless", "reading the module's attributes raised RuntimeError('no str'); not evaluated")])
        json.dumps(folded)
        # A module with no RULE_ID at all is keyed by its module name and evaluated as usual.
        nameless = bare_module("nameless", evaluate=returning(risks=[{"what": "n"}]), ENTRY=3, CAN_BLOCK=False)
        folded = r.evaluate_extra_rules({}, {}, self.READ, TARGET, {}, rules=[nameless])
        self.assertEqual(folded["rules"], {"nameless": {"entry": 3, "can_block": False, "blocking": 0, "risks": 1, "unknown": 0, "notes": 0}})
        self.assertEqual(folded["risks"][0]["rule"], "nameless")

    def test_a_second_module_with_a_registered_id_is_not_evaluated(self):
        first = rule_module("twice", entry=4, evaluate=returning(risks=[{"what": "a"}]))
        second = rule_module("twice", entry=12, evaluate=returning(blocking=[{"what": "b"}], risks=[{"what": "c"}]))
        folded = r.evaluate_extra_rules({}, {}, self.READ, TARGET, {}, rules=[first, second])
        self.assertEqual(folded["blocking"], [])
        self.assertEqual([f["what"] for f in folded["risks"]], ["a"])
        self.assertEqual([(u["rule"], u["entry"], u["can_block"], u["reason"]) for u in folded["unknown"]], [("twice", 4, True, "a second module registers RULE_ID twice; the duplicate was not evaluated")])
        self.assertEqual(folded["rules"], {"twice": {"entry": 4, "can_block": True, "blocking": 0, "risks": 1, "unknown": 1, "notes": 0}})
        # The invariant the JSON readers rely on: per-rule counts describe the flat lists.
        for key in ("blocking", "risks", "unknown", "notes"):
            self.assertEqual(folded["rules"]["twice"][key], sum(1 for item in folded[key] if (item["rule"] if isinstance(item, dict) else None) == "twice" or (key == "notes" and item.startswith("twice: "))))
        self.assertEqual(r.readiness_status({"blocking": []}, {"blocking": []}, {"blocking_exclusions": [], "undecided_exclusions": []}, {"blocking": [], "unknown": []}, True, folded), r.READINESS_UNKNOWN)
        # A duplicate of a rule that cannot block carries that rule's can_block: the per-rule
        # record and its entries agree, and the member's verdict is untouched.
        cannot = rule_module("zonal", entry=11, can_block=False, evaluate=returning(risks=[{"what": "z"}]))
        again = rule_module("zonal", entry=11, can_block=True, evaluate=returning(blocking=[{"what": "b"}]))
        folded = r.evaluate_extra_rules({}, {}, self.READ, TARGET, {}, rules=[cannot, again])
        self.assertEqual([(u["rule"], u["can_block"]) for u in folded["unknown"]], [("zonal", False)])
        self.assertEqual(folded["rules"]["zonal"]["can_block"], False)
        self.assertEqual(folded["blocking"], [])
        self.assertEqual(r.readiness_status({"blocking": []}, {"blocking": []}, {"blocking_exclusions": [], "undecided_exclusions": []}, {"blocking": [], "unknown": []}, True, folded), r.READINESS_READY)

    def test_a_blocker_from_a_rule_that_cannot_block_is_demoted_to_a_risk_with_a_note(self):
        rule = rule_module("cannot", entry=11, can_block=False, evaluate=returning(blocking=[{"what": "oops"}, {"what": "graded", "grade": "blocking"}, {"what": "tiered", "tier": "blocking"}], risks=[{"what": "fine"}]))
        folded = r.evaluate_extra_rules({}, {}, self.READ, TARGET, {}, rules=[rule])
        self.assertEqual(folded["blocking"], [])
        self.assertEqual([f["what"] for f in folded["risks"]], ["oops", "graded", "tiered", "fine"])
        # A demoted finding is re-graded under whichever word it carries: no risk says it blocks.
        self.assertEqual([(f.get("grade"), f.get("tier")) for f in folded["risks"]], [(None, None), ("risk", None), (None, "risk"), (None, None)])
        self.assertEqual(folded["notes"], ["cannot: filed a blocker but declares CAN_BLOCK False; reported as a risk"])
        self.assertEqual(folded["rules"]["cannot"]["risks"], 4)

    def test_the_registry_is_the_default_and_rules_selects(self):
        registered = r.evaluate_extra_rules({"name": "c", "location": "us-central1-a"}, {}, self.READ, TARGET, {})
        self.assertEqual(list(registered["rules"]), [m.RULE_ID for m in r.EXTRA_RULES])
        self.assertIn("zonal-control-plane", [f["rule"] for f in registered["risks"]])
        only = r.evaluate_extra_rules({"name": "c", "location": "us-central1-a"}, {}, self.READ, TARGET, {}, rules=[rule_module("other")])
        self.assertEqual(list(only["rules"]), ["other"])
        self.assertEqual(only["risks"], [])

    def test_text_set_by_the_rule_is_kept(self):
        rule = rule_module("pre", evaluate=returning(risks=[{"what": "x", "text": "the rule's own"}]))
        folded = r.evaluate_extra_rules({}, {}, self.READ, TARGET, {}, rules=[rule])
        self.assertEqual(folded["risks"][0]["text"], "the rule's own")


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
        rules = {"blocking": [{"rule": "x"}], "risks": [], "unknown": [{"rule": "y", "can_block": True}], "notes": [], "rules": {}}
        self.assertEqual(r.readiness_status(None, None, self.CLEAR, self.NO_SKEW, False, rules), "blocked")

    def test_extra_rules_fold_into_the_verdict_by_can_block(self):
        pdbs = {"blocking": []}
        clean = {"blocking": [], "risks": [], "unknown": [], "notes": [], "rules": {}}
        self.assertEqual(r.readiness_status(pdbs, self.NO_WEBHOOKS, self.CLEAR, self.NO_SKEW, True, clean), r.READINESS_READY)
        # Risks and notes never move the verdict.
        self.assertEqual(r.readiness_status(pdbs, self.NO_WEBHOOKS, self.CLEAR, self.NO_SKEW, True, {**clean, "risks": [{"rule": "zonal-control-plane"}], "notes": ["x: y"]}), r.READINESS_READY)
        # An unknown from a rule that can block is unknown; from one that cannot, it is not.
        self.assertEqual(r.readiness_status(pdbs, self.NO_WEBHOOKS, self.CLEAR, self.NO_SKEW, True, {**clean, "unknown": [{"rule": "surge-capacity", "can_block": True, "reason": "no target"}]}), r.READINESS_UNKNOWN)
        self.assertEqual(r.readiness_status(pdbs, self.NO_WEBHOOKS, self.CLEAR, self.NO_SKEW, True, {**clean, "unknown": [{"rule": "zonal-control-plane", "can_block": False, "reason": "odd"}]}), r.READINESS_READY)
        self.assertEqual(r.readiness_status(pdbs, self.NO_WEBHOOKS, self.CLEAR, self.NO_SKEW, True, {**clean, "unknown": [{"rule": "a", "can_block": False}, {"rule": "b", "can_block": True}]}), r.READINESS_UNKNOWN)
        # An unknown of another shape counts, the conservative reading.
        self.assertEqual(r.readiness_status(pdbs, self.NO_WEBHOOKS, self.CLEAR, self.NO_SKEW, True, {**clean, "unknown": ["why"]}), r.READINESS_UNKNOWN)
        self.assertEqual(r.readiness_status(pdbs, self.NO_WEBHOOKS, self.CLEAR, self.NO_SKEW, True, {**clean, "unknown": [{"rule": "a"}]}), r.READINESS_UNKNOWN)
        # Blocked beats an unknown from either kind of rule; the four built-in rules' own unknown still holds.
        self.assertEqual(r.readiness_status(pdbs, self.NO_WEBHOOKS, self.CLEAR, self.NO_SKEW, True, {**clean, "blocking": [{"rule": "x"}], "unknown": [{"rule": "y", "can_block": False}]}), r.READINESS_BLOCKED)
        self.assertEqual(r.readiness_status(None, None, self.CLEAR, self.NO_SKEW, True, clean), r.READINESS_UNKNOWN)
        # None means the per-entry rules did not run; the four built-in rules decide alone.
        self.assertEqual(r.readiness_status(pdbs, self.NO_WEBHOOKS, self.CLEAR, self.NO_SKEW, True, None), r.READINESS_READY)


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


def namespace(name, labels=None):
    return {"kind": "Namespace", "metadata": {"name": name, "labels": {"kubernetes.io/metadata.name": name, **(labels or {})}}}
LIVE = [service("scen", "gate-svc")], [endpoint_slice("scen", "gate-svc", [True])]


def grade(hooks, services=(), slices=(), kind="ValidatingWebhookConfiguration", namespaces=()):
    return r.grade_webhooks([webhook_config(kind, "gate", hooks)], list(services), list(slices), list(namespaces))


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

    def test_an_off_list_rule_at_an_unserved_version_is_not_said_to_fail_now(self):
        # A leftover PodSecurityPolicy gate pinned to policy/v1beta1: no GKE version in support
        # sends that request, and the reader cannot tell for a resource off its lists, so the
        # cell says the requests fail if the server still serves the version, not that they do.
        rule = {"apiGroups": ["policy"], "apiVersions": ["v1beta1"], "resources": ["podsecuritypolicies"], "operations": ["CREATE", "UPDATE"]}
        graded = grade([hook("psp.example.com", [rule], policy="Fail")])
        self.assertEqual(graded["blocking"], [])
        cell = r.describe_webhook_finding(graded["outage"][0])
        self.assertNotIn("fails now every request the server sends it", cell)
        self.assertIn("if the server still serves those versions", cell)
        # The same resource at a served version, or unversioned, keeps the live-failure sentence.
        for versions in (["v1"], ["*"]):
            with self.subTest(versions=versions):
                live = grade([hook("x.example.com", [dict(rule, apiVersions=versions)], policy="Fail")])
                self.assertIn("fails now every request the server sends it", r.describe_webhook_finding(live["outage"][0]))

    def test_a_selector_exempting_kube_system_by_a_label_it_carries_keeps_the_gate_an_outage(self):
        # The policy engine's exemption idiom: `DoesNotExist` or `NotIn` on a label the installer
        # put on kube-system. The namespaces' labels are read, so the selector is judged as
        # admission judges it: a Role gate that exempts both bootstrap namespaces by that label
        # is an outage, not a blocker. Without the Namespace read the name label alone is known
        # and the exemption cannot be seen, so the gate is named.
        gate = [rule(["roles"], groups=("rbac.authorization.k8s.io",))]
        exempt = [namespace("kube-system", {"admission.policy.example/ignore": "true"}), namespace("kube-public", {"admission.policy.example/ignore": "true"})]
        for selector in (
            {"matchExpressions": [{"key": "admission.policy.example/ignore", "operator": "DoesNotExist"}]},
            {"matchExpressions": [{"key": "admission.policy.example/ignore", "operator": "NotIn", "values": ["true"]}]},
        ):
            with self.subTest(selector=selector):
                graded = grade([scoped(hook("opa.example.com", gate, policy="Fail"), selector)], namespaces=exempt)
                self.assertEqual((graded["blocking"], len(graded["outage"])), ([], 1))
                self.assertFalse(r.namespace_selector_reaches({"namespaceSelector": selector}, r.bootstrap_namespace_labels(exempt)["kube-system"]))
                unread = grade([scoped(hook("opa.example.com", gate, policy="Fail"), selector)])
                self.assertEqual(unread["blocking"][0]["upgrade_path"], ["CREATE roles in kube-system,kube-public"])
        # Exempting kube-system alone leaves kube-public reached.
        selector = {"matchExpressions": [{"key": "admission.policy.example/ignore", "operator": "DoesNotExist"}]}
        graded = grade([scoped(hook("opa.example.com", gate, policy="Fail"), selector)], namespaces=exempt[:1])
        self.assertEqual(graded["blocking"][0]["upgrade_path"], ["CREATE roles in kube-public"])

    def test_a_webhook_with_no_rules_is_counted_not_graded(self):
        # `rules` is optional and a webhook with none matches no request, so the server never
        # calls it: a dead backend behind it fails nothing now and is not an outage to report.
        for rules in ([], None):
            with self.subTest(rules=rules):
                config = webhook_config("ValidatingWebhookConfiguration", "gate", [hook("empty.example.com", rules, policy="Fail")])
                if rules is None:
                    del config["webhooks"][0]["rules"]
                graded = r.grade_webhooks([config], [], [])
                self.assertEqual((graded["blocking"], graded["outage"]), ([], []))
                self.assertEqual((graded["no_rules"], graded["evaluated"]), (1, 0))

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

    def test_a_selector_on_an_operator_added_label_reads_as_the_namespace_read_says(self):
        # `matchLabels` on a label the installer may have put on kube-system: admitting when the
        # Namespace read shows the label, not admitting when it does not, and not admitting when
        # the namespace was not read, since its name label is then all the reader has.
        gate = scoped(hook("opa.example.com", self.ROLE_GATE, policy="Fail"), {"matchLabels": {"gatekeeper": "enabled"}})
        blocking, outage = self._one([gate])
        self.assertEqual((blocking, len(outage)), ([], 1))
        graded = grade([gate], namespaces=[namespace("kube-system", {"gatekeeper": "enabled"})])
        self.assertEqual(graded["blocking"][0]["upgrade_path"], ["CREATE roles in kube-system"])
        graded = grade([gate], namespaces=[namespace("kube-system"), namespace("kube-public")])
        self.assertEqual((graded["blocking"], len(graded["outage"])), ([], 1))

    def test_bootstrap_namespace_labels_come_from_the_read(self):
        # The two bootstrap namespaces' labels as read, the name label always present, other
        # namespaces and junk ignored, and the name label alone for a namespace not read.
        read = r.bootstrap_namespace_labels([namespace("kube-system", {"a": "b"}), {"kind": "Namespace", "metadata": {"name": "kube-public"}}, namespace("default", {"x": "y"}), "junk"])
        self.assertEqual(read, {"kube-system": {"kubernetes.io/metadata.name": "kube-system", "a": "b"}, "kube-public": {"kubernetes.io/metadata.name": "kube-public"}})
        self.assertEqual(r.bootstrap_namespace_labels(None), {"kube-system": {"kubernetes.io/metadata.name": "kube-system"}, "kube-public": {"kubernetes.io/metadata.name": "kube-public"}})

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

    def test_a_pinned_role_gate_scoped_off_kube_system_is_sent_nothing_and_says_so_without_a_namespace(self):
        # The served version is the server's fact: a selector that admits neither bootstrap
        # namespace changes the label, not the sentence.
        gate = scoped(hook("opa.example.com", [rule(["roles"], groups=("rbac.authorization.k8s.io",), versions=("v1beta1",))], policy="Fail"), self.SEEDED_SCOPE)
        blocking, outage = self._one([gate])
        self.assertEqual(blocking, [])
        self.assertEqual(outage[0]["version_pinned"], ["CREATE roles"])
        self.assertIn("the server serves CREATE roles at v1 alone, so it sends this webhook none of them", r.describe_webhook_finding(outage[0]))

    def test_a_pinned_rule_is_judged_on_its_resources_however_spelled_and_whatever_its_operations(self):
        # `pods/` is `pods` here as it is in the matcher.
        gate = hook("stale.example.com", [rule(["pods/"], versions=("v1beta1",))], policy="Fail")
        blocking, outage = self._one([gate])
        self.assertEqual((blocking, outage[0]["version_pinned"]), ([], ["CREATE pods"]))
        self.assertIn("so it sends this webhook none of them", r.describe_webhook_finding(outage[0]))
        # A graded resource at an unserved version is pinned whatever the operation, since the
        # served version is the resource's; no graded row carries DELETE rolebindings, so the
        # label is the resource.
        gate = hook("old.example.com", [rule(["rolebindings"], operations=("DELETE",), groups=("rbac.authorization.k8s.io",), versions=("v1beta1",))], policy="Fail")
        blocking, outage = self._one([gate])
        self.assertEqual((blocking, outage[0]["version_pinned"]), ([], ["rolebindings in rbac.authorization.k8s.io"]))
        self.assertIn("the server serves rolebindings in rbac.authorization.k8s.io at v1 alone, so it sends this webhook none of them", r.describe_webhook_finding(outage[0]))
        for operations in (("UPDATE",), ("CONNECT",)):
            with self.subTest(operations=operations):
                gate = hook("old.example.com", [rule(["pods"], operations=operations, versions=("v1beta1",))], policy="Fail")
                blocking, outage = self._one([gate])
                self.assertEqual(blocking, [])
                self.assertNotIn("fails now every request the server sends it", r.describe_webhook_finding(outage[0]))
        # The parent of a graded subresource is a graded resource: the pre-1.25 PDB-policy gate,
        # bare or with every subresource, is sent nothing at policy/v1beta1. The bare spelling
        # reaches no row and is labelled at the resource; `/*` reaches the status row.
        gate = hook("pdb.example.com", [rule(["poddisruptionbudgets"], operations=("CREATE", "UPDATE"), groups=("policy",), versions=("v1beta1",))], policy="Fail")
        blocking, outage = self._one([gate])
        self.assertEqual((blocking, outage[0]["version_pinned"]), ([], ["poddisruptionbudgets in policy"]))
        self.assertIn("the server serves poddisruptionbudgets in policy at v1 alone, so it sends this webhook none of them", r.describe_webhook_finding(outage[0]))
        gate = hook("pdb.example.com", [rule(["poddisruptionbudgets/*"], operations=("UPDATE",), groups=("policy",), versions=("v1beta1",))], policy="Fail")
        blocking, outage = self._one([gate])
        self.assertEqual((blocking, outage[0]["version_pinned"]), ([], ["UPDATE poddisruptionbudgets/status"]))
        # A subresource is served under its parent's group-version.
        gate = hook("exec.example.com", [rule(["pods/exec"], operations=("CONNECT",), versions=("v1beta1",))], policy="Fail")
        _, outage = self._one([gate])
        self.assertEqual(outage[0]["version_pinned"], ["pods"])
        # A rule naming two graded groups and a graded resource from each also names the pairs that
        # do not exist (policy/pods); the server sends it nothing either way.
        gate = hook("two.example.com", [rule(["pods", "poddisruptionbudgets"], operations=("CREATE", "UPDATE"), groups=("", "policy"), versions=("v1beta1",))], policy="Fail")
        blocking, outage = self._one([gate])
        self.assertEqual(blocking, [])
        self.assertEqual(outage[0]["version_pinned"][0], "CREATE pods")
        self.assertIn("so it sends this webhook none of them", r.describe_webhook_finding(outage[0]))
        # A pair the server does not serve at all is pinned by the same separation and labelled
        # apart, as an unserved pair, so the cell says the server serves no such pair rather than
        # that it serves the pair at v1 alone; the finding's rules and its cell agree.
        gate = hook("typo.example.com", [rule(["pods"], groups=("policy",), versions=("v1beta1",))], policy="Fail")
        blocking, outage = self._one([gate])
        self.assertEqual((blocking, outage[0]["version_pinned"], outage[0]["unserved_pairs"], outage[0]["live_rules"]), ([], [], ["pods in policy"], []))
        self.assertEqual(outage[0]["pinned_rules"], ["CREATE pods in policy at v1beta1"])
        cell = r.describe_webhook_finding(outage[0])
        self.assertIn("(its rules: CREATE pods in policy at v1beta1; the server serves no pods in policy, so it sends this webhook none of them); it is reported, not graded", cell)
        self.assertNotIn("at v1 alone", cell)
        # The core group is named on an unserved pair, so the clause is not read as every group.
        gate = hook("typo.example.com", [rule(["poddisruptionbudgets"], versions=("v1beta1",))], policy="Fail")
        _, outage = self._one([gate])
        self.assertEqual(outage[0]["unserved_pairs"], ["poddisruptionbudgets in core"])
        self.assertIn("the server serves no poddisruptionbudgets in core, so it sends this webhook none of them", r.describe_webhook_finding(outage[0]))
        # A rule spelling a served pair and an unserved one carries both reasons, joined.
        gate = hook("both.example.com", [rule(["poddisruptionbudgets", "pods"], operations=("DELETE",), groups=("policy",), versions=("v1beta1",))], policy="Fail")
        _, outage = self._one([gate])
        self.assertEqual((outage[0]["version_pinned"], outage[0]["unserved_pairs"]), (["poddisruptionbudgets in policy"], ["pods in policy"]))
        self.assertIn("the server serves poddisruptionbudgets in policy at v1 alone and the server serves no pods in policy, so it sends this webhook none of them", r.describe_webhook_finding(outage[0]))
        # Beside a live rule, the pinned clause carries the same reasons.
        gate = hook("mixed.example.com", [rule(["pods"], groups=("policy",), versions=("v1beta1",)), rule(["configmaps"])], policy="Fail")
        _, outage = self._one([gate])
        self.assertIn("; the server serves no pods in policy, so it sends the rule CREATE pods in policy at v1beta1 nothing; reported, not graded", r.describe_webhook_finding(outage[0]))
        # A spelling naming every resource, or a resource off every list, is still not pinned:
        # their served versions are not this rule's to know.
        for spec, groups in (("*", ("",)), ("*/*", ("",)), ("*/status", ("",)), ("configmaps", ("",))):
            with self.subTest(spec=spec):
                gate = hook("wild.example.com", [rule([spec], groups=groups, versions=("v1beta1",))], policy="Fail")
                _, outage = self._one([gate])
                self.assertEqual(outage[0]["version_pinned"], [])

    def test_each_graded_resource_lives_in_one_graded_group(self):
        # `_rule_version_pinned` judges a rule's groups and resources separately, which is sound
        # while no graded resource name exists under two graded groups.
        groups_by_resource: dict[str, set[str]] = {}
        for group, _, resource, _, _ in r.GRADED_TARGETS:
            groups_by_resource.setdefault(r._resource_parent(resource), set()).add(group)
        self.assertEqual({name: groups for name, groups in groups_by_resource.items() if len(groups) > 1}, {})

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
        # The seeded fleet's fixture, as bench/tf/fleet/defects-b.tf writes it: a gate on
        # `CREATE configmaps` in the core group at `v1`, no Service, scoped to its own namespace.
        # It is an outage because its rule matches ConfigMaps only, which neither graded list
        # carries; the scope confines the gate; the cell carries the pinned `v1`.
        gate = scoped(hook("gate.seeded.invalid", [rule(["configmaps"], versions=("v1",))], policy="Fail", service=("seeded-upgrade", "nonexistent-admission-gate")), {"matchLabels": {"kubernetes.io/metadata.name": "seeded-upgrade"}})
        graded = grade([gate])
        self.assertEqual(graded["blocking"], [])
        self.assertEqual(len(graded["outage"]), 1)
        self.assertIn("matches none of the operations this rule reads as the upgrade's path (its rules: CREATE configmaps at v1); it fails now every request the server sends it; reported, not graded", r.describe_webhook_finding(graded["outage"][0]))
        self.assertEqual(graded["outage"][0]["rules"], ["CREATE configmaps at v1"])

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
        self.assertNotIn("fails now every request the server sends it", cell)
        # A dead gate on a resource off the list does fail the requests it matches now.
        graded = grade([hook("cm.example.com", [rule(["configmaps"])], policy="Fail")])
        self.assertEqual([f["version_pinned"] for f in graded["outage"]], [[]])
        self.assertIn("it fails now every request the server sends it", r.describe_webhook_finding(graded["outage"][0]))
        # A pinned rule beside a served one is a blocker on the served rule. The record classes
        # each rule by what the server serves it whichever bucket the finding is in, so the
        # pinned rule is pinned there too, not live by the finding's bucket; the cell names the
        # path write alone and says nothing of the classes.
        graded = grade([hook("mixed.example.com", [rule(["certificatesigningrequests"], groups=("certificates.k8s.io",), versions=("v1beta1",)), rule(["pods"])], policy="Fail")])
        self.assertEqual([f["upgrade_path"] for f in graded["blocking"]], [["CREATE pods"]])
        self.assertEqual([f["version_pinned"] for f in graded["blocking"]], [["CREATE certificatesigningrequests"]])
        self.assertEqual([f["pinned_rules"] for f in graded["blocking"]], [["CREATE certificatesigningrequests in certificates.k8s.io at v1beta1"]])
        self.assertEqual([f["live_rules"] for f in graded["blocking"]], [["CREATE pods"]])
        self.assertEqual([f["unknown_rules"] for f in graded["blocking"]], [[]])
        cell = r.describe_webhook_finding(graded["blocking"][0])
        self.assertIn("matches CREATE pods", cell)
        self.assertNotIn("sends this webhook none of them", cell)
        self.assertNotIn("v1beta1", cell.split("matches", 1)[1])
        # A rule on `*` alone carries no version suffix; one on `v1` alone carries `at v1`.
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
        self.assertIn("matches none of the operations this rule reads as the upgrade's path (its rules: CREATE certificatesigningrequests in certificates.k8s.io at v1beta1, CREATE configmaps at v1); it fails now every request the server sends it that matches CREATE configmaps at v1; the server serves CREATE certificatesigningrequests at v1 alone, so it sends the rule CREATE certificatesigningrequests in certificates.k8s.io at v1beta1 nothing; reported, not graded", cell)
        self.assertNotIn("sends this webhook none of them", cell)
        self.assertNotIn("sends it; reported", cell)  # the whole-webhook live clause
        # The order of the rules does not change which is named as failing.
        graded = grade([hook("mixed.example.com", [rule(["configmaps"]), pinned], policy="Fail")])
        self.assertEqual((graded["outage"][0]["live_rules"], graded["outage"][0]["pinned_rules"]), (["CREATE configmaps"], ["CREATE certificatesigningrequests in certificates.k8s.io at v1beta1"]))
        # Only a rule the list can vouch for is pinned, and only a rule at a served version is
        # live: a wildcard at v1beta1, a path resource named beside one off the list at v1beta1
        # and a wildcard group at v1beta1 may or may not still be served, which the reader
        # cannot say, so each is unknown and the cell asserts no failure now.
        for rules in (
            [rule(["*"], groups=("*",), operations=("*",), versions=("v1beta1",))],
            [rule(["certificatesigningrequests", "clustertrustbundles"], groups=("certificates.k8s.io",), versions=("v1beta1",))],
            [rule(["certificatesigningrequests"], groups=("certificates.k8s.io", "*"), versions=("v1beta1",))],
        ):
            with self.subTest(rules=rules):
                graded = grade([hook("wide.example.com", rules, policy="Fail")])
                finding = graded["outage"][0]
                self.assertEqual((finding["version_pinned"], finding["pinned_rules"], finding["live_rules"]), ([], [], []))
                self.assertEqual(finding["unknown_rules"], finding["rules"])
                cell = r.describe_webhook_finding(finding)
                self.assertNotIn("fails now every request the server sends it", cell)
                self.assertNotIn("that matches", cell)
                self.assertIn("its requests fail now if the server still serves those versions", cell)

    def test_each_rule_of_an_outage_is_classed_on_its_own_and_the_cell_says_each(self):
        # Three spellings of a dead policy engine's leftovers: a failure now is asserted for a
        # rule the server serves and for no other, whatever stands beside it.
        csr_pinned = rule(["certificatesigningrequests"], groups=("certificates.k8s.io",), versions=("v1beta1",))
        psp = rule(["podsecuritypolicies"], operations=("CREATE", "UPDATE"), groups=("policy",), versions=("v1beta1",))
        # A pinned rule beside an off-list rule at an unserved version: sent nothing, and unknown.
        finding = grade([hook("psp.example.com", [csr_pinned, psp], policy="Fail")])["outage"][0]
        self.assertEqual((finding["live_rules"], finding["pinned_rules"], finding["unknown_rules"]),
                         ([], ["CREATE certificatesigningrequests in certificates.k8s.io at v1beta1"], ["CREATE/UPDATE podsecuritypolicies in policy at v1beta1"]))
        cell = r.describe_webhook_finding(finding)
        self.assertIn("matches none of the operations this rule reads as the upgrade's path (its rules: CREATE certificatesigningrequests in certificates.k8s.io at v1beta1, CREATE/UPDATE podsecuritypolicies in policy at v1beta1); the server serves CREATE certificatesigningrequests at v1 alone, so it sends the rule CREATE certificatesigningrequests in certificates.k8s.io at v1beta1 nothing; the requests matched by CREATE/UPDATE podsecuritypolicies in policy at v1beta1 fail now if the server still serves those versions, which this reader does not know for a resource off its lists; reported, not graded", cell)
        self.assertNotIn("fails now", cell)
        # One rule naming a graded and an off-list resource, or group, at an unserved version is
        # unknown: the pre-1.25 policy gate on budgets and PodSecurityPolicies written as one rule.
        for rules in (
            [rule(["certificatesigningrequests", "clustertrustbundles"], groups=("certificates.k8s.io",), versions=("v1beta1",))],
            [rule(["pods"], groups=("", "apps"), versions=("v1beta1",))],
            [rule(["poddisruptionbudgets", "podsecuritypolicies"], operations=("CREATE", "UPDATE"), groups=("policy",), versions=("v1beta1",))],
        ):
            with self.subTest(rules=rules):
                finding = grade([hook("mixed.example.com", rules, policy="Fail")])["outage"][0]
                self.assertEqual((finding["live_rules"], finding["pinned_rules"]), ([], []))
                self.assertEqual(finding["unknown_rules"], finding["rules"])
                cell = r.describe_webhook_finding(finding)
                self.assertNotIn("fails now every request the server sends it", cell)
                self.assertIn("its requests fail now if the server still serves those versions", cell)
        # A live rule beside an unknown one: the live rule fails now, the unknown one is hedged.
        finding = grade([hook("cm.example.com", [rule(["configmaps"]), psp], policy="Fail")])["outage"][0]
        self.assertIn("; it fails now every request the server sends it that matches CREATE configmaps; the requests matched by CREATE/UPDATE podsecuritypolicies in policy at v1beta1 fail now if the server still serves those versions, which this reader does not know for a resource off its lists; reported, not graded", r.describe_webhook_finding(finding))
        # All three classes in one webhook: one clause each, live, pinned, unknown, in that order.
        finding = grade([hook("all.example.com", [psp, rule(["configmaps"]), csr_pinned], policy="Fail")])["outage"][0]
        cell = r.describe_webhook_finding(finding)
        self.assertIn("; it fails now every request the server sends it that matches CREATE configmaps; the server serves CREATE certificatesigningrequests at v1 alone, so it sends the rule CREATE certificatesigningrequests in certificates.k8s.io at v1beta1 nothing; the requests matched by CREATE/UPDATE podsecuritypolicies in policy at v1beta1 fail now if the server still serves those versions", cell)
        self.assertEqual(cell.count("fail now if"), 1)
        # A live rule on a resource the server may not serve at all (a CRD removed with its
        # controller): the cell claims a failure for every request the server sends, no more.
        finding = grade([hook("cm.example.com", [rule(["certificates"], operations=("CREATE", "UPDATE"), groups=("cert-manager.io",), versions=("v1",))], policy="Fail")])["outage"][0]
        cell = r.describe_webhook_finding(finding)
        self.assertIn("; it fails now every request the server sends it; reported, not graded", cell)
        self.assertNotIn("its own requests", cell)
        # The class is read from the versions: `*` or `v1` is live whatever the resource.
        for versions in (("*",), ("v1",), ("v1beta1", "v1")):
            with self.subTest(versions=versions):
                self.assertEqual(r._rule_served_class(dict(psp, apiVersions=list(versions)), r.GRADED_TARGETS), r.RULE_LIVE)
        self.assertEqual(r._rule_served_class(csr_pinned, r.GRADED_TARGETS), r.RULE_PINNED)
        self.assertEqual(r._rule_served_class(psp, r.GRADED_TARGETS), r.RULE_UNKNOWN)

    def test_monitoring_resources_are_outside_the_path(self):
        # GKE's managed Prometheus operator gates its own resources fail-closed.
        self.assertEqual(self._path([rule(["rules", "clusterrules", "globalrules"], groups=("monitoring.googleapis.com",), operations=("CREATE", "UPDATE"))]), [])

    def test_split_webhook_items(self):
        items = [webhook_config("ValidatingWebhookConfiguration", "v", []), webhook_config("MutatingWebhookConfiguration", "m", []), service("scen", "s"), endpoint_slice("scen", "s", [True]), namespace("kube-system"), {"kind": "PodDisruptionBudget"}, "junk"]
        configs, services, slices, namespaces = r.split_webhook_items(items)
        self.assertEqual(([c["metadata"]["name"] for c in configs], len(services), len(slices), [n["metadata"]["name"] for n in namespaces]), (["v", "m"], 1, 1, ["kube-system"]))

if __name__ == "__main__":
    unittest.main()
