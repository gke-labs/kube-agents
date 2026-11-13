"""The credential broker reads a no-pdb fix with `automerge_eligibility` (stdlib
only, no fleet-audit import); its file format must stay the one `audit_report`
writes."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import audit_report  # puts the shared scripts directory on the path
import automerge_eligibility
from test_recheck import CLUSTER, LABELS, NS


class BrokerBudgetParityTest(unittest.TestCase):
    ae = automerge_eligibility

    def test_the_broker_reads_the_budget_the_audit_writes(self):
        for candidate, fid in (
            ({"cluster": CLUSTER, "object": "Deployment/checkout", "namespace": NS, "pod_selector": {"matchLabels": LABELS}}, "no-pdb.f-1"),
            ({"cluster": "p/l/c", "object": "StatefulSet/db-0", "namespace": "data",
              "pod_selector": {"matchExpressions": [{"key": "app", "operator": "In", "values": ["db"]}], "matchLabels": {"t": "x"}}}, "no-pdb.f-2"),
        ):
            with self.subTest(candidate=candidate):
                name, text = audit_report._pdb_manifest(candidate, fid)
                self.assertEqual(
                    self.ae.render_budget(candidate["cluster"], candidate["object"], candidate["namespace"], candidate["pod_selector"], fid),
                    (name, text),
                )
                self.assertIsNotNone(self.ae.parse_budget(text))
        self.assertEqual(self.ae.FIX_TYPE, audit_report.GENERATED_FIX_CHECK)
        self.assertEqual(self.ae.BUDGET_SUFFIX, audit_report.GENERATED_PDB_SUFFIX)

    def test_the_brokers_field_patterns_are_the_generators(self):
        self.assertEqual(self.ae.KUBERNETES_NAME.pattern, audit_report.KUBERNETES_NAME_PATTERN.pattern.strip("^$"))
        self.assertEqual(self.ae.FINDING_ID.pattern, audit_report.FINDING_ID_RE.pattern.removeprefix("^").removesuffix("\\Z"))


if __name__ == "__main__":
    unittest.main()
