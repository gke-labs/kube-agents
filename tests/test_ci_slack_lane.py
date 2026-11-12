"""The Slack lane's nightly wiring: hack/ci-deploy.sh's 2a block and hack/ci-eval-pr.sh's lane step.

ci-kube-agents-eval-nightly-slack sets EVAL_SLACK_LANE=1 beside EVAL_MODE_NEXT=1. The deploy
arms Slack through the eval app when its secrets read, and the eval runs hack/slack-lane/run.sh
in place of the matrix. The 2a block is run here against a fake gcloud, so the secrets it reads
and the Helm values it hands the install are pinned without a cluster.

Run: python3 -m unittest discover -s tests -p 'test_ci_slack_lane.py' -v
"""

import os
import pathlib
import re
import subprocess
import tempfile
import textwrap
import unittest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_DEPLOY = _REPO_ROOT / "hack" / "ci-deploy.sh"
_EVAL = _REPO_ROOT / "hack" / "ci-eval-pr.sh"
_LANE = _REPO_ROOT / "hack" / "slack-lane" / "run.sh"
_JOB = "ci-kube-agents-eval-nightly-slack"


def _deploy_constants() -> str:
    text = _DEPLOY.read_text()
    return "\n".join(line for line in text.splitlines() if line.startswith("readonly SLACK_LANE_"))


def _block_2a() -> str:
    text = _DEPLOY.read_text()
    start = text.index("# ─── 2a. The Slack lane's Slack")
    end = text.index("\n# ─── ", start + 1)
    return text[start:end]


def _run_2a(env: dict, secrets: dict) -> subprocess.CompletedProcess:
    """Runs the 2a block with a fake gcloud that answers `secrets versions access` from secrets."""
    tmp = tempfile.TemporaryDirectory()
    bin_dir = pathlib.Path(tmp.name)
    cases = "\n".join(f'  {name}) printf %s "{value}" ;;' for name, value in secrets.items())
    gcloud = bin_dir / "gcloud"
    gcloud.write_text(textwrap.dedent("""\
        #!/usr/bin/env bash
        secret=""
        for a in "$@"; do case "$a" in --secret=*) secret="${a#--secret=}" ;; esac; done
        case "$secret" in
        """) + cases + "\n  *) exit 1 ;;\nesac\n")
    gcloud.chmod(0o755)
    script = "set -euo pipefail\n" + _deploy_constants() + "\n" + _block_2a() + '\nprintf "ARG %s\\n" ${SLACK_LANE_HELM_ARGS[@]+"${SLACK_LANE_HELM_ARGS[@]}"}\n'
    full_env = {"PATH": f"{bin_dir}:{os.environ['PATH']}", **env}
    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=full_env, check=False)
    tmp.cleanup()
    return proc


_ALL = {
    "slack-eval-bot-token": "xoxb-1",
    "slack-eval-app-token": "xapp-1",
    "slack-eval-channel-id": "C0CI",
    "slack-eval-test-user-id": "UTEST",
}


class DeployTest(unittest.TestCase):
    def test_the_job_may_deploy_next(self):
        names = re.search(r'^readonly EVAL_MODE_NEXT_JOB_NAMES="([^"]*)"', _DEPLOY.read_text(), re.MULTILINE).group(1).split()
        self.assertIn(_JOB, names)

    def test_with_every_secret_slack_is_armed_for_the_test_user(self):
        proc = _run_2a({"EVAL_SLACK_LANE": "1", "EVAL_MODE_NEXT": "1"}, _ALL)
        self.assertEqual(0, proc.returncode, proc.stderr)
        args = [line[len("ARG "):] for line in proc.stdout.splitlines() if line.startswith("ARG ")]
        self.assertIn("platformAgent.integration.slack.enabled=true", args)
        self.assertIn("platformAgent.integration.slack.homeChannel=C0CI", args)
        self.assertIn("platformAgent.integration.slack.allowedUsers[0]=UTEST", args)
        self.assertIn("operator.slackTestUsers[0]=UTEST", args)
        files = [a for a in args if a.startswith("platformAgent.credentials.data.SLACK_")]
        self.assertEqual(2, len(files))
        self.assertNotIn("xoxb-1", proc.stdout)
        self.assertNotIn("xapp-1", proc.stdout)

    def test_a_missing_secret_leaves_slack_off(self):
        partial = {k: v for k, v in _ALL.items() if k != "slack-eval-app-token"}
        proc = _run_2a({"EVAL_SLACK_LANE": "1", "EVAL_MODE_NEXT": "1"}, partial)
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertNotIn("ARG platformAgent", proc.stdout)
        self.assertIn("slack-eval-app-token", proc.stdout)

    def test_the_lane_needs_next(self):
        proc = _run_2a({"EVAL_SLACK_LANE": "1"}, _ALL)
        self.assertNotEqual(0, proc.returncode)
        self.assertIn("EVAL_MODE_NEXT=1", proc.stderr)

    def test_other_runs_are_untouched(self):
        proc = _run_2a({"EVAL_MODE_NEXT": "1"}, _ALL)
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertEqual([], [line for line in proc.stdout.splitlines() if line.strip() not in ("", "ARG")])

    def test_the_helm_install_carries_the_args(self):
        self.assertIn('${SLACK_LANE_HELM_ARGS[@]+"${SLACK_LANE_HELM_ARGS[@]}"}', _DEPLOY.read_text())

    def test_the_lane_and_the_deploy_read_one_channel(self):
        lane = re.search(r'CHANNEL_SECRET="\$\{SLACK_LANE_CHANNEL_SECRET:-([^}]+)\}"', _LANE.read_text()).group(1)
        self.assertEqual(lane, re.search(r'^readonly SLACK_LANE_CHANNEL_SECRET="([^"]+)"', _DEPLOY.read_text(), re.MULTILINE).group(1))


class EvalTest(unittest.TestCase):
    def test_the_lane_runs_before_the_fan_out_and_exits_green(self):
        text = _EVAL.read_text()
        lane = text.index('if [ "${EVAL_SLACK_LANE:-}" = "1" ]; then\n  profile_begin "slack lane')
        self.assertLess(lane, text.index("# ─── Parallel fan-out"))
        block = text[lane:text.index("\nfi\n", lane)]
        self.assertIn("slack-lane/run.sh", block)
        self.assertIn("spec.integration.slack.enabled", block, "the lane runs only on an install with Slack on")
        self.assertIn("exit 0", block)

    def test_the_lane_publishes_no_dashboard(self):
        body = re.search(r"^publish_eval_dashboard\(\) \{.*?^\}", _EVAL.read_text(), re.MULTILINE | re.DOTALL).group(0)
        self.assertIn("EVAL_SLACK_LANE", body)


if __name__ == "__main__":
    unittest.main()
