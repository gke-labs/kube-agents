"""A new cluster in a Terraform repository is a pull request, not a stop.

SOUL §3 item 7 told the Platform Agent that a Terraform repository has no write
path and is a stop, while the `gcp-terraform` skill it routes to writes new
clusters, so the persona instructed a refusal of what its own skill does
(gke-labs/kube-agents#3691). `gke-cluster-creation` Step 6 and `gcp-terraform`'s
When-to-Use carried the same stop.

The routing lives in prose and nothing executes it, so these tests pin the
phrases it is made of: a rewording that keeps the route keeps them; one that
brings the stop back fails here before a model obeys it on a run.

Run:
  python3 -m unittest discover -s tests -p 'test_persona_terraform_new_cluster.py' -v
"""

from __future__ import annotations

import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PLATFORM_SOUL = REPO_ROOT / "agents/platform/SOUL.md"
CLUSTER_CREATION_SKILL = REPO_ROOT / "agents/platform/skills/gke-cluster-creation/SKILL.md"
TERRAFORM_SKILL = REPO_ROOT / "agents/platform/skills/gcp-terraform/SKILL.md"
MIN_FILE_CHARS = 1000

# §3 item 7, from its lead to the end of its line (it is the section's last item).
ITEM_LEAD = "**A new cluster is a pull request in the repository's own format.**"
NEXT_ITEM = "\n"
SOUL_ROUTE = "Terraform HCL: write the `google_container_cluster` and its node pools with `gcp-terraform`"
STOP_PHRASES = ("no Terraform write path", "stop and say so")
# gke-cluster-creation Step 6.
STEP_LEAD = "**Propose**"
NEXT_STEP = "7.  **Track**"
STEP_ROUTE = "Terraform HCL through the `gcp-terraform` skill"
STEP_STOP = "A Terraform repository"
# gcp-terraform's When-to-Use.
SKILL_ROUTE = "A new cluster comes here when `SOUL.md` §3 item 7 routes it here, which it does for a Terraform repository."
SKILL_STOP = "the stop is the answer"


def _read(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    if len(text) < MIN_FILE_CHARS:
        raise AssertionError(f"{path} read back suspiciously short ({len(text)} chars)")
    return text


def _flat(text: str) -> str:
    # The skills wrap their prose; compare phrases across line breaks.
    return " ".join(text.split())


class TerraformNewClusterRouteTest(unittest.TestCase):
    longMessage = False

    def test_soul_item_7_routes_terraform_to_the_skill(self):
        soul = _read(PLATFORM_SOUL)
        start = soul.index(ITEM_LEAD)
        item = soul[start : soul.index(NEXT_ITEM, start)]
        self.assertIn(SOUL_ROUTE, item, "§3 item 7 no longer routes Terraform HCL to gcp-terraform")
        for phrase in STOP_PHRASES:
            with self.subTest(phrase=phrase):
                self.assertNotIn(phrase, item, f"§3 item 7 carries the Terraform stop again ({phrase!r})")

    def test_cluster_creation_step_6_routes_terraform_to_the_skill(self):
        skill = _read(CLUSTER_CREATION_SKILL)
        start = skill.index(STEP_LEAD)
        step = _flat(skill[start : skill.index(NEXT_STEP, start)])
        self.assertIn(STEP_ROUTE, step, "gke-cluster-creation Step 6 no longer routes Terraform HCL to gcp-terraform")
        self.assertNotIn(STEP_STOP, step, "gke-cluster-creation Step 6 carries the Terraform stop again")

    def test_terraform_skill_takes_the_new_cluster(self):
        skill = _flat(_read(TERRAFORM_SKILL))
        self.assertIn(SKILL_ROUTE, skill, "gcp-terraform's When-to-Use no longer takes a new cluster")
        self.assertNotIn(SKILL_STOP, skill, "gcp-terraform's When-to-Use carries the Terraform stop again")


if __name__ == "__main__":
    unittest.main()
