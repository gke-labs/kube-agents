"""The case `ccc-untainted-node-pool-absorption` holds its finding to the collector.

Its second objective passes only when the ledger carries the collector's own
excerpt for a ComputeClass that GKE reports misconfigured. The phrase lives in
the case file and the excerpt in the stockout collector, so a reworded excerpt
would leave the case red on a product that still files the finding. This test
joins the two.
"""

import json
import sys
import unittest
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
CASE_FILE = REPO_ROOT / "bench" / "tasks" / "ccc-untainted-node-pool-absorption" / "task.yaml"
EVIDENCE_CHECK = "the-finding-carries-the-collectors-evidence"
CLASS_CHECK = "ledger-files-the-misconfigured-class"
# The fleet's class as GKE reports it, captured live: a node pool with the class label
# and no class taint.
CAPTURE_FILE = (
    REPO_ROOT / "agents" / "platform" / "skills" / "fleet-audit" / "scripts" / "testdata" / "computeclass_taint_missing.json"
)
PLANTED_CLASS = json.loads(CAPTURE_FILE.read_text(encoding="utf-8"))

sys.path.insert(0, str(REPO_ROOT / "agents" / "platform" / "skills" / "fleet-audit" / "scripts"))

import fleet_stockout  # noqa: E402


def _check(name: str) -> dict:
    case = yaml.safe_load(CASE_FILE.read_text(encoding="utf-8"))
    (entry,) = [c for c in case["verification_spec"] if c.get("name") == name]
    return entry["check"]


class CasePhraseTest(unittest.TestCase):
    def test_the_evidence_phrase_is_in_the_collectors_excerpt(self):
        hit = fleet_stockout.check_compute_class_status(PLANTED_CLASS)
        phrases = _check(EVIDENCE_CHECK)["required_phrases"]
        self.assertTrue(phrases)
        for phrase in phrases:
            self.assertIn(phrase, hit["excerpt"])

    def test_the_class_check_names_the_collectors_slug_and_object(self):
        hit = fleet_stockout.check_compute_class_status(PLANTED_CLASS)
        check = _check(CLASS_CHECK)
        self.assertEqual(check["finding_check"], "dangling-compute-class")
        self.assertEqual(check["required_phrases"], [hit["object"]])


if __name__ == "__main__":
    unittest.main()
