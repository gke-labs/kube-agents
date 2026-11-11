"""The fleet-audit inspection allowlist and the prose that restates it.

`INSPECTION_BINARIES` in `audit_report.py` decides which `checks_run[].command`
entries count as having read the fleet. Eight governance SOPs, the
findings-document reference and the ledger design document restate the list by
hand, so a binary added or retired in one place and not the others leaves the
agent told to use a command the validator rejects, or the validator accepting
one no SOP mentions. Nothing mechanical held them together before this test.
"""

import pathlib
import re
import sys
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "agents" / "platform" / "skills" / "fleet-audit" / "scripts"))

import audit_report  # noqa: E402

# The read-only reporter the upgrade audit records; it is an entry in the tuple
# but not a binary the SOP sentences list.
REPORTER = "fleet_upgrade_report.py"
EXPECTED_BINARIES = ("kubectl", "gcloud", "bq", "helm", "curl")
# The sentence every restatement carries, up to the "or" before the last name.
ROSTER_SENTENCE = re.compile(r"`kubectl`(?:[,/ ]+(?:or )?`[a-z_.-]+`)+")
RESTATEMENTS = (
    "agents/platform/governance/ai_security_audit_sop.md",
    "agents/platform/governance/compliance_audit_sop.md",
    "agents/platform/governance/fleet_consistency_drift_sop.md",
    "agents/platform/governance/fleet_wide_cost_analysis_sop.md",
    "agents/platform/governance/obtainability_audit_sop.md",
    "agents/platform/governance/security_patch_orchestrator_sop.md",
    "agents/platform/governance/stockout_prevention_sop.md",
    "agents/platform/governance/upgrade_retrospective_sop.md",
    "agents/platform/skills/fleet-audit/references/findings-document.md",
    "docs/designs/fleet-audit-issue-ledger.md",
)


class InspectionBinariesTest(unittest.TestCase):
    def test_the_allowlist_is_the_five_binaries_and_the_reporter(self):
        self.assertEqual(audit_report.INSPECTION_BINARIES, EXPECTED_BINARIES + (REPORTER,))

    def test_every_restatement_lists_exactly_the_allowlist(self):
        for relative in RESTATEMENTS:
            text = (REPO_ROOT / relative).read_text(encoding="utf-8")
            matches = ROSTER_SENTENCE.findall(text)
            with self.subTest(file=relative):
                self.assertTrue(matches, "no roster sentence found")
                for sentence in matches:
                    named = tuple(re.findall(r"`([a-z_.-]+)`", sentence))
                    self.assertEqual(named, EXPECTED_BINARIES, sentence)


if __name__ == "__main__":
    unittest.main()
