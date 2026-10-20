#!/usr/bin/env python3
"""Unit tests for readiness_rules/dataplane.py (catalogue entry 16)."""

import copy
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import readiness_rules as shared  # noqa: E402
from readiness_rules import dataplane  # noqa: E402
from test_readiness_rules import TARGET, cluster, read  # noqa: E402
from test_rule_capacity import context_for  # noqa: E402


def netpol(namespace, name):
    return {"kind": "NetworkPolicy", "metadata": {"namespace": namespace, "name": name}, "spec": {"podSelector": {}}}


def policies():
    return [netpol("seeded-debug", "default-deny"), netpol("seeded-upgrade", "default-deny"), netpol("seeded-upgrade", "apiserver-egress")]


class DataplaneRuleTest(unittest.TestCase):
    def test_contract_attributes(self):
        self.assertEqual((dataplane.RULE_ID, dataplane.ENTRY, dataplane.CAN_BLOCK), ("network-dataplane", 16, False))

    def test_unenforced_policies_on_legacy_are_a_risk_naming_the_namespaces(self):
        record = cluster(name="seeded-b")
        host = cluster(name="platform-agent-host", location="us-central1", networkConfig={"datapathProvider": "ADVANCED_DATAPATH"})
        out = dataplane.evaluate(record, {}, read(items=policies()), TARGET, context_for(record, clusters=[host, record]))
        self.assertEqual((out["blocking"], out["unknown"], out["notes"]), ([], [], []))
        finding = out["risks"][0]
        self.assertEqual(finding["namespaces"], {"seeded-debug": 1, "seeded-upgrade": 2})
        self.assertEqual((finding["policies"], finding["enforced"], finding["peers_on_v2"], finding["datapath"]), (3, False, ["platform-agent-host"], "LEGACY_DATAPATH"))
        text = dataplane.describe(finding)
        self.assertIn("policy enforcement off and NetworkPolicies in seeded-debug (1), seeded-upgrade (2)", text)
        # The peers are named as where enforcement is on, not as enforcing this member's policies, which nothing reads.
        self.assertIn("or on a Dataplane V2 cluster (in the listing: platform-agent-host, each enforcing its own policies); test what they deny before either", text)
        self.assertIn("start to apply the moment enforcement turns on", text)

    def test_without_a_v2_peer_the_shape_is_still_a_risk(self):
        record = cluster(name="seeded-b")
        out = dataplane.evaluate(record, {}, read(items=policies()), TARGET, context_for(record))
        self.assertEqual(out["risks"][0]["peers_on_v2"], [])
        self.assertNotIn("in the listing:", dataplane.describe(out["risks"][0]))

    def test_a_v2_twin_named_like_the_member_is_a_peer_and_the_member_never_is(self):
        # The fold hands the rule copies, so a peer is never told from the member by identity:
        # the member is left out because it is legacy (or the rule returned), and a Dataplane
        # V2 cluster of the same name in another project is a record of its own, a peer.
        record = cluster(name="prod")
        twin = cluster(name="prod", networkConfig={"datapathProvider": "ADVANCED_DATAPATH"})
        out = dataplane.evaluate(record, {}, read(items=policies()), TARGET, context_for(record, clusters=[copy.deepcopy(record), twin]))
        self.assertEqual(out["risks"][0]["peers_on_v2"], ["prod"])
        self.assertEqual(dataplane.evaluate(record, {}, read(items=policies()), TARGET, context_for(record, clusters=[copy.deepcopy(record)]))["risks"][0]["peers_on_v2"], [])

    def test_enforced_by_the_addon_is_a_note(self):
        record = cluster(name="seeded-b", networkPolicy={"enabled": True, "provider": "CALICO"}, addonsConfig={"networkPolicyConfig": {}})
        out = dataplane.evaluate(record, {}, read(items=policies()), TARGET, context_for(record))
        self.assertEqual(out["risks"], [])
        self.assertIn("enforced by the network policy add-on", out["notes"][0])
        record["addonsConfig"]["networkPolicyConfig"]["disabled"] = True
        self.assertEqual(len(dataplane.evaluate(record, {}, read(items=policies()), TARGET, context_for(record))["risks"]), 1)

    def test_every_legacy_member_without_policies_gets_a_note(self):
        legacy = cluster(name="seeded-c", networkConfig={"datapathProvider": "LEGACY_DATAPATH"})
        out = dataplane.evaluate(legacy, {}, read(items=[]), TARGET, context_for(legacy))
        self.assertEqual((out["blocking"], out["risks"], out["unknown"]), ([], [], []))
        self.assertEqual(out["notes"], ["legacy dataplane, policy enforcement off; network policies: none, nothing to enforce"])
        legacy["networkPolicy"] = {"enabled": True}
        self.assertIn("policy enforcement on (network policy add-on); network policies: none", dataplane.evaluate(legacy, {}, read(items=[]), TARGET, context_for(legacy))["notes"][0])

    def test_dataplane_v2_members_are_clean_whatever_was_read(self):
        v2 = cluster(name="host", networkConfig={"datapathProvider": "ADVANCED_DATAPATH"})
        self.assertEqual(dataplane.evaluate(v2, {}, read(items=policies()), TARGET, context_for(v2)), shared.new_result())
        self.assertEqual(dataplane.evaluate(v2, {}, read(error="forbidden"), TARGET, context_for(v2)), shared.new_result())

    def test_unread_policies_on_a_legacy_member_are_unknown(self):
        record = cluster(name="seeded-b")
        refused = dataplane.evaluate(record, {}, read(items=[], read_errors={"networkpolicy": "networkpolicies is forbidden"}), TARGET, context_for(record))
        self.assertEqual(refused["unknown"], ["legacy dataplane; NetworkPolicies not read (networkpolicies is forbidden), so whether any apply nowhere today is unknown"])
        nothing = dataplane.evaluate(record, {}, read(error="get-credentials failed"), TARGET, context_for(record))
        self.assertIn("NetworkPolicies not read (get-credentials failed)", nothing["unknown"][0])


if __name__ == "__main__":
    unittest.main()
