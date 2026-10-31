"""dev_rebuild_agent.sh reports a refused image update and takes the default agent on a closed stdin."""

import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = pathlib.Path("scripts/dev/dev_rebuild_agent.sh")
# The script sources ../installer/common.sh, keeps its state beside it and reads tags.env and
# install.defaults.env from the repository root, so the pieces travel together into a scratch root.
COPIED_TREES = ("scripts/installer", "scripts/dev")
COPIED_FILES = ("install.defaults.env", "tags.env")
VARS_FILE = pathlib.Path("scripts/installer/vars.sh")
RUN_TIMEOUT_SECONDS = 60
SUCCESS_LINE = "Redeployment triggered successfully"
STEP_FAILED_LINE = "Failed to execute step: 4. Trigger Redeployment in GKE"
STUB_GCLOUD = """#!/usr/bin/env bash
case "$*" in
  "config get-value project"*) echo "dev-project" ;;
esac
exit 0
"""
# No CRD, so the Deployment path is the only one that moves the image; `set image` answers
# with the exit code the test chooses.
STUB_KUBECTL = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$KUBECTL_CALLS"
case "$*" in
  "config current-context"*) echo "gke_dev-project_us-east4_dev-cluster" ;;
  "get crd"*) exit 1 ;;
  "get deployments -A"*)
    echo "kubeagents-system:platform-agent-gateway"
    [ "${STUB_WITH_CALLOUT:-0}" -eq 1 ] && echo "kubeagents-system:platform-agent-a2a-callout" ;;
  "get deployment "*)
    if [ "${STUB_GET_DEPLOYMENT_EXIT:-0}" -ne 0 ]; then
      echo 'Error from server (Forbidden): deployments.apps "platform-agent-gateway" is forbidden' >&2
      exit "$STUB_GET_DEPLOYMENT_EXIT"
    fi
    case "$3" in
      platform-agent-gateway) printf 'agent-api-auth\\nplatform-agent\\n' ;;
      platform-agent-a2a-callout) printf 'callout\\n' ;;
    esac ;;
  "set image"*)
    if [ "${STUB_SET_IMAGE_EXIT:-0}" -ne 0 ]; then
      echo 'Error from server (Forbidden): deployments.apps "platform-agent-gateway" is forbidden' >&2
      exit "$STUB_SET_IMAGE_EXIT"
    fi
    # Refuse a container the Deployment does not have, as kubectl does.
    case "$3" in
      deployment/platform-agent-gateway) have=" agent-api-auth platform-agent " ;;
      deployment/platform-agent-a2a-callout) have=" callout " ;;
      *) have=" " ;;
    esac
    for update in "${@:6}"; do
      case "$have" in
        *" ${update%%=*} "*) ;;
        *) echo "error: unable to find container named \"${update%%=*}\"" >&2; exit 1 ;;
      esac
    done ;;
esac
exit 0
"""


class DevRebuildAgentReportsTest(unittest.TestCase):
    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp(prefix="dev-rebuild-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        for tree in COPIED_TREES:
            shutil.copytree(REPO / tree, self.root / tree)
        for name in COPIED_FILES:
            shutil.copy(REPO / name, self.root / name)
        (self.root / VARS_FILE).unlink(missing_ok=True)
        stub_bin = self.root / "bin"
        stub_bin.mkdir()
        for name, text in (("gcloud", STUB_GCLOUD), ("kubectl", STUB_KUBECTL)):
            stub = stub_bin / name
            stub.write_text(text)
            stub.chmod(0o755)
        self.calls = self.root / "kubectl-calls.log"
        self.env = {
            **{k: v for k, v in os.environ.items() if k in ("TMPDIR", "LANG", "LC_ALL")},
            "PATH": f"{stub_bin}:/usr/bin:/bin",
            "HOME": str(self.root),
            "PROJECT_ID": "dev-project",
            "REGION": "us-east4",
            "CLUSTER_NAME": "dev-cluster",
            "KUBECTL_CALLS": str(self.calls),
        }

    def run_script(self, *args, set_image_exit=0, with_callout=False, get_deployment_exit=0):
        env = {
            **self.env,
            "STUB_SET_IMAGE_EXIT": str(set_image_exit),
            "STUB_WITH_CALLOUT": str(int(with_callout)),
            "STUB_GET_DEPLOYMENT_EXIT": str(get_deployment_exit),
        }
        return subprocess.run(
            ["bash", str(self.root / SCRIPT), *args],
            cwd=self.root, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=RUN_TIMEOUT_SECONDS,
        )

    def test_a_refused_image_update_fails_the_step_instead_of_reporting_success(self):
        proc = self.run_script("platform", set_image_exit=1)
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("is forbidden", proc.stderr)
        self.assertIn(STEP_FAILED_LINE, proc.stdout + proc.stderr)
        self.assertNotIn(SUCCESS_LINE, proc.stdout)

    def test_an_accepted_image_update_still_reports_success(self):
        proc = self.run_script("platform")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn(SUCCESS_LINE, proc.stdout)
        [call] = [call for call in self.calls.read_text().splitlines() if call.startswith("set image")]
        words = call.split()
        self.assertEqual(words[:4], ["set", "image", "deployment/platform-agent-gateway", "-n"])
        self.assertEqual([word.split("=")[0] for word in words[5:]], ["platform-agent", "agent-api-auth"], call)

    def test_a_deployment_with_none_of_our_containers_is_skipped_not_failed(self):
        proc = self.run_script("platform", with_callout=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("skipping", proc.stdout)
        self.assertIn(SUCCESS_LINE, proc.stdout)
        set_image_calls = [call for call in self.calls.read_text().splitlines() if call.startswith("set image")]
        self.assertEqual([call.split()[2] for call in set_image_calls], ["deployment/platform-agent-gateway"])

    def test_a_failed_read_of_a_deployments_containers_fails_the_step_instead_of_skipping(self):
        proc = self.run_script("platform", get_deployment_exit=1)
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("is forbidden", proc.stderr)
        self.assertIn(STEP_FAILED_LINE, proc.stdout + proc.stderr)
        self.assertNotIn("skipping", proc.stdout)
        self.assertNotIn(SUCCESS_LINE, proc.stdout)

    def test_a_closed_stdin_takes_the_default_agent_instead_of_dying_at_the_prompt(self):
        proc = self.run_script()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn(SUCCESS_LINE, proc.stdout)


if __name__ == "__main__":
    unittest.main()
