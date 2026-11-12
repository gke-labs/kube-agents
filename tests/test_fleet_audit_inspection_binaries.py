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
# The allowlist clause every restatement carries: the phrase that introduces
# the list, then the names, across line wraps. Anchoring on the phrase rather
# than on the first name means a name written before `kubectl` is seen, and
# a sentence elsewhere that happens to name two binaries is not.
ROSTER_SENTENCE = re.compile(
    r"(?:name one of|names none of|naming none of)\s+(?:`[a-z0-9_.-]+`[,/\s]+(?:or\s+)?)*`[a-z0-9_.-]+`"
)
BINARY_NAME = re.compile(r"`([a-z0-9_.-]+)`")
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
                    named = tuple(BINARY_NAME.findall(sentence))
                    self.assertEqual(named, EXPECTED_BINARIES, sentence)

    def test_the_roster_pattern_sees_a_name_on_either_side_and_across_a_wrap(self):
        drifted = (
            "It must name one of `legacy`, `kubectl`, `gcloud`, `bq`, `helm`, or `curl`.",
            "It must name one of `kubectl`, `gcloud`, `bq`, `helm`,\n    or `curl`, or `python3`.",
            "the validator rejects one that names none of `kubectl`/`gcloud`/`bq`/`helm`/`curl`/`legacy`.",
        )
        for sentence in drifted:
            with self.subTest(sentence=sentence):
                match = ROSTER_SENTENCE.search(sentence)
                self.assertIsNotNone(match)
                self.assertNotEqual(tuple(BINARY_NAME.findall(match.group(0))), EXPECTED_BINARIES)
        wrapped = "It must name one of\n`kubectl`, `gcloud`, `bq`,\n`helm`, or `curl`; `echo` is rejected."
        match = ROSTER_SENTENCE.search(wrapped)
        self.assertEqual(tuple(BINARY_NAME.findall(match.group(0))), EXPECTED_BINARIES)
        # Prose that names two binaries outside the allowlist clause is not a roster.
        self.assertIsNone(ROSTER_SENTENCE.search("read-only `kubectl`, `gcloud` reads of the fleet"))


if __name__ == "__main__":
    unittest.main()
