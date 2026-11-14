"""hack/slack-lane/run.sh reads hack/slack-lane/cases.txt and hands each case to devops-bench.

The lane is run here against a fake gcloud (which answers the three Secret Manager reads) and a
fake uv (which records the case and the BENCH_SLACK_THEN it was given), so the cases file's
grammar and the skip without secrets are pinned without Slack or a cluster.

Run: python3 -m unittest discover -s tests -p 'test_slack_lane_run.py' -v
"""

import json
import os
import pathlib
import subprocess
import tempfile
import unittest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_RUN = _REPO_ROOT / "hack" / "slack-lane" / "run.sh"
_CASES = _REPO_ROOT / "hack" / "slack-lane" / "cases.txt"
_TASKS = _REPO_ROOT / "bench" / "tasks"

_FAKE_GCLOUD = """#!/usr/bin/env bash
[ -n "${FAKE_GCLOUD_EMPTY:-}" ] && exit 1
printf %s fake-value
"""
# Records one JSON line per run: the case directory devops-bench was given and the then text.
_FAKE_UV = """#!/usr/bin/env bash
printf '{"task": "%s", "then": "%s"}\\n' "$3" "${BENCH_SLACK_THEN}" >>"${FAKE_UV_LOG}"
"""


def _run_lane(empty_secrets=False):
    tmp = tempfile.TemporaryDirectory()
    root = pathlib.Path(tmp.name)
    for name, body in (("gcloud", _FAKE_GCLOUD), ("uv", _FAKE_UV)):
        path = root / name
        path.write_text(body)
        path.chmod(0o755)
    log = root / "uv.log"
    env = {
        "PATH": f"{root}:{os.environ['PATH']}",
        "FAKE_UV_LOG": str(log),
        "SLACK_LANE_ARTIFACTS": str(root / "artifacts"),
    }
    if empty_secrets:
        env["FAKE_GCLOUD_EMPTY"] = "1"
    proc = subprocess.run(["bash", str(_RUN)], capture_output=True, text=True, env=env, check=False)
    runs = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    tmp.cleanup()
    return proc, runs


class SlackLaneRunTest(unittest.TestCase):
    def test_each_case_runs_once_with_its_then_text(self):
        proc, runs = _run_lane()
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertEqual(
            [
                {"task": "./tasks/chat-voice-answer-first", "then": ""},
                {"task": "./tasks/chat-question-asked-to-choose-pauses", "then": "none of those for now, leave it as it is"},
                {"task": "./tasks/chat-voice-capabilities-names-cluster-agents", "then": "which of those can change a deployment?"},
            ],
            runs,
        )

    def test_every_case_exists(self):
        for line in _CASES.read_text().splitlines():
            line = line.split("#", 1)[0].split(" then: ", 1)[0].strip()
            if line:
                with self.subTest(case=line):
                    self.assertTrue((_TASKS / line / "task.yaml").is_file())

    def test_without_secrets_the_lane_skips_and_runs_nothing(self):
        proc, runs = _run_lane(empty_secrets=True)
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertEqual([], runs)
        self.assertIn("slack lane: skipped", proc.stdout)


if __name__ == "__main__":
    unittest.main()
