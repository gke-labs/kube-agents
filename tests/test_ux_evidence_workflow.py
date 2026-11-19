"""The UX evidence check judges the PR body as it is when the job runs.

A re-run replays its event's payload, so a check that read the payload's body
stayed red after the author fixed it. Pinned here because nothing else reads
the workflow.
"""

import pathlib
import unittest

import yaml

WORKFLOW = pathlib.Path(__file__).resolve().parent.parent / ".github" / "workflows" / "ux-evidence.yml"


class UxEvidenceWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.job = yaml.safe_load(WORKFLOW.read_text())["jobs"]["evidence"]
        self.step = next(s for s in self.job["steps"] if s.get("id") == "check")

    def test_the_body_is_read_from_the_api_before_the_payload(self):
        script = self.step["run"]
        self.assertIn('gh api "repos/$GITHUB_REPOSITORY/pulls/$PR_NUMBER"', script)
        self.assertLess(script.index("gh api"), script.index('"$PR_BODY"'))

    def test_only_the_evidence_job_may_read_pull_requests(self):
        self.assertEqual(self.job["permissions"].get("pull-requests"), "read")
        chat = yaml.safe_load(WORKFLOW.read_text())["jobs"]["chat-rules"]
        self.assertNotIn("permissions", chat)


if __name__ == "__main__":
    unittest.main()
