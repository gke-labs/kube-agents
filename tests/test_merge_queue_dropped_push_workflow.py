"""The `dropped-push` job of `merge-gate.yml`: when it runs, and what it may do.

The job posts a comment after a merge, on `pull_request_target`, so it holds a
write token on an event a fork pull request starts. The shared suites hold the
checkout ref, the repository guard, the timeout and the `env:` rule for every
`pull_request_target` job (`tests/test_pull_request_target_workflows.py`).
This one holds what is particular to this job:

  - It runs only for a pull request that was closed by a merge. On any other
    event it would compare a pull request that has no merged commit.
  - Its token writes pull requests and reads contents, and nothing else.
  - It checks out the default branch and runs the script, and no other step
    exists: a step added later would run with the write token.
  - The `closed` event that starts it does not also start the `pull-request`
    job, which posts gate statuses on an open pull request's head.
"""

import pathlib
import unittest

import yaml

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "merge-gate.yml"
_SCRIPT = "scripts/merge_queue_dropped_push.py"


def _document() -> dict:
    return yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))


def _condition(job: dict) -> str:
    return " ".join(str(job.get("if", "")).split())


class DroppedPushJobTest(unittest.TestCase):
    def setUp(self) -> None:
        self.document = _document()
        self.job = self.document["jobs"]["dropped-push"]

    def test_the_workflow_starts_on_a_closed_pull_request(self) -> None:
        # PyYAML reads an unquoted `on:` key as the boolean True (YAML 1.1).
        on = self.document.get("on", self.document.get(True))
        self.assertIn("closed", on["pull_request_target"]["types"])

    def test_the_job_runs_only_for_a_merged_pull_request_in_this_repository(self) -> None:
        self.assertEqual(
            "github.repository == 'gke-labs/kube-agents' && "
            "github.event_name == 'pull_request_target' && "
            "github.event.action == 'closed' && "
            "github.event.pull_request.merged == true",
            _condition(self.job),
        )

    def test_the_token_writes_pull_requests_and_reads_contents_only(self) -> None:
        self.assertEqual({"contents": "read", "pull-requests": "write"}, self.job["permissions"])

    def test_the_job_checks_out_the_default_branch_and_runs_the_script_only(self) -> None:
        checkout, compare = self.job["steps"]
        self.assertTrue(checkout["uses"].startswith("actions/checkout@"))
        self.assertEqual(
            {"ref": "${{ github.event.repository.default_branch }}", "persist-credentials": False},
            checkout["with"],
        )
        self.assertEqual(f'python3 {_SCRIPT} --number "$NUMBER"', compare["run"].strip())
        self.assertEqual(
            {"GITHUB_TOKEN": "${{ secrets.GITHUB_TOKEN }}", "NUMBER": "${{ github.event.pull_request.number }}"},
            compare["env"],
        )
        self.assertTrue((_REPO_ROOT / _SCRIPT).is_file())

    def test_a_closed_pull_request_does_not_start_the_status_job(self) -> None:
        self.assertIn("github.event.action != 'closed'", _condition(self.document["jobs"]["pull-request"]))

    def test_no_other_job_can_write_to_pull_requests(self) -> None:
        for name, job in self.document["jobs"].items():
            if name == "dropped-push":
                continue
            with self.subTest(job=name):
                self.assertNotEqual("write", (job.get("permissions") or {}).get("pull-requests"))


if __name__ == "__main__":
    unittest.main()
