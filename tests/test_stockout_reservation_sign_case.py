# Copyright 2026 The Kubernetes Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""The two checks of the case `stockout-pinned-pool` for the autoscaler's
`scale.up.error.reservation.not.found`, held to the stockout collector.

The seeded fleet's `dev-reserved-pool` (role missing-reservation-autoscaled)
autoscales and names a reservation that does not exist, and one Pending pod
makes the autoscaler write that id into its visibility log. The case reads the
finding that the collector files for the id (check 3.11). The check id, the
object and the impact sentence are in the collector; the phrases are in the
case file. These tests join them, with the autoscaler entry that a cluster
logged and with the pools as GKE holds them. No command runs.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "agents" / "platform" / "skills" / "fleet-audit" / "scripts"
CASE_FILE = REPO_ROOT / "bench" / "tasks" / "stockout-pinned-pool" / "task.yaml"
# The case's check that reads the object of the finding.
CASE_CHECK = "an-autoscaler-finding-names-the-reservation-not-found-sign"
# The case's check that reads the collector's impact sentence in the body.
CASE_IMPACT_CHECK = "the-reservation-not-found-finding-has-the-collectors-impact"
ROLE = "missing-reservation-autoscaled"
# The check that files the autoscaler's id (3.11) and the check that files a
# production pool whose named reservation is missing (3.10(d)).
AUTOSCALER_CHECK = "autoscaler-out-of-resources"
RESERVATION_CHECK = "reservation-mismatch-risk"
SIGN_OBJECT = "ScaleUpError/scale.up.error.reservation.not.found"

sys.path.insert(0, str(SCRIPTS))

import audit_report  # noqa: E402
import fleet_stockout  # noqa: E402

# The recorded log entries of the collector's own tests: the entry for a
# missing reservation (GKE 1.35.8, 2026-11-18), its instance group, and an
# entry for a stockout (2026-08-14).
from test_fleet_stockout import (  # noqa: E402
    ERROR_MSG_ENTRY,
    RESERVATION_NOT_FOUND_ENTRY,
    RESERVATION_NOT_FOUND_GROUP_URL,
)

PROJECT = "acme"
ZONE = "us-central1-a"
SLOT_A = {"name": "seeded-a", "project": PROJECT, "location": ZONE, "autopilot": False}
RESERVATION = {"consumeReservationType": "SPECIFIC_RESERVATION", "key": "compute.googleapis.com/reservation-name", "values": ["seeded-absent-reservation"]}
# The instance group of slot a's other reservation pool. The name is not from
# a recording; it must only differ from the group of the recorded entry.
OTHER_GROUP_URL = f"https://www.googleapis.com/compute/v1/projects/{PROJECT}/zones/{ZONE}/instanceGroups/gke-seeded-a-missing-reservation-pool-grp"
# What a worker writes as the impact of a finding.
WORKER_IMPACT = "The pod of reserved-batch stays Pending."


def _reserved_pool(name: str, role: str, group_url: str, **more) -> dict:
    """A zero-node pool of slot a that consumes only the named reservation, as
    the node pool list reports it: one zone, and the fleet's label and taint."""
    return {
        "name": name,
        "status": "RUNNING",
        "locations": [ZONE],
        "instanceGroupUrls": [group_url],
        "config": {
            "machineType": "n2-standard-2",
            "resourceLabels": {"managed-by": "kube-agents-seeded-fleet"},
            "labels": {"seeded-role": role},
            "taints": [{"key": "seeded-role", "value": role, "effect": "NO_SCHEDULE"}],
            "reservationAffinity": RESERVATION,
        },
        **more,
    }


# The fixture's pool. The recorded entry names the instance group of the
# cluster that logged it, so the pool here has that group.
DEV_RESERVED_POOL = _reserved_pool("dev-reserved-pool", ROLE, RESERVATION_NOT_FOUND_GROUP_URL, autoscaling={"enabled": True, "maxNodeCount": 1})
# Slot a's pool with no autoscaling (role missing-reservation-pool).
MISSING_RESERVATION_POOL = _reserved_pool("missing-reservation-pool", "missing-reservation", OTHER_GROUP_URL)


def _audited(*pools: dict, log_entries: list[dict]) -> dict:
    """The collector's entry for slot a with these pools and this autoscaler
    log, after the fleet step that compares the pools with the project's
    reservations. The project has no reservation. The reads are answered
    here; no command runs."""

    def run(argv, **_):
        if argv[:2] == ["kubectl", "get"]:
            return fleet_stockout.Run(argv, 0, json.dumps({"items": []}), "", 0.0)
        if argv[:3] == ["gcloud", "container", "node-pools"]:
            return fleet_stockout.Run(argv, 0, json.dumps(list(pools)), "", 0.0)
        if argv[:3] == ["gcloud", "logging", "read"]:
            return fleet_stockout.Run(argv, 0, json.dumps(log_entries), "", 0.0)
        return fleet_stockout.Run(argv, 0, "", "", 0.0)

    with TemporaryDirectory() as tmp, patch.object(fleet_stockout, "KUBECONFIG_DIR", Path(tmp)):
        entry = fleet_stockout.collect_cluster(SLOT_A, run=run)
    fleet_stockout.file_reserved_pools(entry, {PROJECT: fleet_stockout.reservation_zones([])}, frozenset({PROJECT}))
    return entry


def _filed(entry: dict) -> list[tuple[str, str]]:
    """The findings of the two reservation signs, as `(check, object)`."""
    return sorted((c["check"], c["object"]) for c in entry["candidates"] if c["check"] in (AUTOSCALER_CHECK, RESERVATION_CHECK))


def _case_check(name: str) -> dict:
    case = yaml.safe_load(CASE_FILE.read_text(encoding="utf-8"))
    [check] = [c["check"] for c in case["verification_spec"] if c.get("name") == name]
    return check


def _published(entry: dict, candidate: dict) -> dict:
    """The finding that a worker writes for `candidate`: the collector's
    check, cluster and object, and the worker's own impact."""
    finding = {
        "check": candidate["check"],
        "cluster": entry["name"],
        "namespace": candidate["namespace"],
        "object": candidate["object"],
        "severity": candidate["severity"],
        "title": "The autoscaler did not find the reservation",
        "impact": WORKER_IMPACT,
    }
    return {**finding, "id": audit_report.published_id(finding)}


def _body(finding: dict) -> str:
    """The finding as the ledger shows it, in lowercase, as the bench check
    reads the body."""
    return "\n".join(audit_report.render_finding(finding)).lower()


class SlotATest(unittest.TestCase):
    """What the collector files on slot a for the two reservation pools."""

    def test_the_fixture_pool_keeps_the_autoscaler_finding(self):
        # `dev` makes the pool non-production, so 3.10(d) does not file it.
        entry = _audited(DEV_RESERVED_POOL, log_entries=[RESERVATION_NOT_FOUND_ENTRY])
        self.assertEqual(_filed(entry), [(AUTOSCALER_CHECK, SIGN_OBJECT)])

    def test_slot_a_gets_one_finding_for_each_pool(self):
        entry = _audited(MISSING_RESERVATION_POOL, DEV_RESERVED_POOL, log_entries=[RESERVATION_NOT_FOUND_ENTRY])
        self.assertEqual(_filed(entry), [(AUTOSCALER_CHECK, SIGN_OBJECT), (RESERVATION_CHECK, "NodePool/missing-reservation-pool")])

    def test_a_production_pool_that_the_reservation_check_files_loses_the_autoscaler_finding(self):
        # Why the fixture's pool has `dev` in its name: with a production
        # name, one fault is one finding, on the pool.
        production = {**DEV_RESERVED_POOL, "name": "reserved-pool"}
        entry = _audited(production, log_entries=[RESERVATION_NOT_FOUND_ENTRY])
        self.assertEqual(_filed(entry), [(RESERVATION_CHECK, "NodePool/reserved-pool")])
        # The log read ran and stays recorded.
        self.assertIn(AUTOSCALER_CHECK, {c["check"] for c in entry["commands"]})


class CaseCheckTest(unittest.TestCase):
    """The case's check on the object of the finding (`scope: finding`)."""

    def passes(self, check: dict, candidate: dict) -> bool:
        """Whether the check passes on a finding of this candidate, as the
        bench check matches: the check id, then each phrase in the object,
        in lowercase."""
        return candidate["check"] == check["finding_check"] and all(p.lower() in candidate["object"].lower() for p in check["required_phrases"])

    def test_the_case_lists_the_fixture_role(self):
        case = yaml.safe_load(CASE_FILE.read_text(encoding="utf-8"))
        self.assertIn(ROLE, case["fixtures"])

    def test_the_check_names_the_collectors_check_and_object(self):
        check = _case_check(CASE_CHECK)
        entry = _audited(DEV_RESERVED_POOL, log_entries=[RESERVATION_NOT_FOUND_ENTRY])
        [hit] = [c for c in entry["candidates"] if c["check"] == AUTOSCALER_CHECK]
        self.assertEqual(check["scope"], "finding")
        self.assertEqual(check["finding_check"], hit["check"])
        self.assertEqual(check["required_phrases"], [fleet_stockout.AUTOSCALER_RESERVATION_NOT_FOUND])
        self.assertTrue(self.passes(check, hit))

    def test_the_check_passes_on_one_finding_of_slot_a_only(self):
        # Slot a with both pools and a second id in the log: the finding of
        # the pool and the finding of the other id do not pass the check.
        check = _case_check(CASE_CHECK)
        entry = _audited(MISSING_RESERVATION_POOL, DEV_RESERVED_POOL, log_entries=[RESERVATION_NOT_FOUND_ENTRY, ERROR_MSG_ENTRY])
        self.assertIn((AUTOSCALER_CHECK, "ScaleUpError/scale.up.error.out.of.resources"), _filed(entry))
        self.assertIn((RESERVATION_CHECK, "NodePool/missing-reservation-pool"), _filed(entry))
        self.assertEqual([c["object"] for c in entry["candidates"] if self.passes(check, c)], [SIGN_OBJECT])

    def test_the_check_finds_nothing_for_a_production_pool(self):
        check = _case_check(CASE_CHECK)
        entry = _audited({**DEV_RESERVED_POOL, "name": "reserved-pool"}, log_entries=[RESERVATION_NOT_FOUND_ENTRY])
        self.assertEqual([c for c in entry["candidates"] if self.passes(check, c)], [])


class CaseImpactCheckTest(unittest.TestCase):
    """The case's check on the body: the impact sentence that only the
    collector has for the id."""

    def phrase(self) -> str:
        check = _case_check(CASE_IMPACT_CHECK)
        # No scope: the bench check then reads the body.
        self.assertNotIn("scope", check)
        [phrase] = check["required_phrases"]
        return phrase.lower()

    def test_the_phrase_is_in_the_sentence_that_the_collector_marks_as_its_own(self):
        entry = _audited(DEV_RESERVED_POOL, log_entries=[RESERVATION_NOT_FOUND_ENTRY])
        [hit] = [c for c in entry["candidates"] if c["check"] == AUTOSCALER_CHECK]
        self.assertEqual(hit["impact"], fleet_stockout.RESERVATION_NOT_FOUND_IMPACT)
        self.assertIs(hit["impact_authoritative"], True)
        self.assertIn(self.phrase(), hit["impact"].lower())

    def test_no_other_sentence_of_the_collector_holds_the_phrase(self):
        # The body has the other findings of the run too: the reservation
        # finding of `missing-reservation-pool`, and each check's own sentence.
        phrase = self.phrase()
        for sentence in (fleet_stockout.MISSING_RESERVATION_IMPACT, *fleet_stockout.IMPACT.values()):
            with self.subTest(sentence=sentence):
                self.assertNotIn(phrase, sentence.lower())

    def test_the_report_writes_the_collectors_sentence_over_the_workers_words(self):
        entry = _audited(DEV_RESERVED_POOL, log_entries=[RESERVATION_NOT_FOUND_ENTRY])
        [hit] = [c for c in entry["candidates"] if c["check"] == AUTOSCALER_CHECK]
        finding = _published(entry, hit)
        self.assertNotIn(self.phrase(), _body(finding))
        self.assertEqual(audit_report.adopt_arm_impact([finding], {"clusters": [entry]}), [finding["id"]])
        self.assertIn(self.phrase(), _body(finding))

    def test_a_finding_by_hand_with_no_candidate_keeps_the_workers_words(self):
        # A collector that does not count the id files no candidate for it. A
        # worker can still file the id by hand, and the check on the object
        # then passes. This check does not: the sentence is not in the body.
        entry = _audited(DEV_RESERVED_POOL, log_entries=[RESERVATION_NOT_FOUND_ENTRY])
        [hit] = [c for c in entry["candidates"] if c["check"] == AUTOSCALER_CHECK]
        no_candidate = {**entry, "candidates": [c for c in entry["candidates"] if c is not hit]}
        finding = _published(entry, hit)
        self.assertEqual(audit_report.adopt_arm_impact([finding], {"clusters": [no_candidate]}), [])
        self.assertEqual(finding["impact"], WORKER_IMPACT)
        self.assertNotIn(self.phrase(), _body(finding))


if __name__ == "__main__":
    unittest.main()
