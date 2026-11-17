"""dev_rebuild_agent.sh reports a refused image update, takes the default agent on a closed stdin,
and builds the three images that go under one dev tag."""

import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

import yaml

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
REGISTRY = "us-east4-docker.pkg.dev/dev-project/kube-agents"
BRIDGE_DOCKERFILE = "a2a/Dockerfile.hermes-bridge"
BRIDGE_BUILD_CONFIG = "scripts/dev/cloudbuild-hermes-bridge.yaml"
STUB_GCLOUD = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$GCLOUD_CALLS"
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
# Records each call. A call that holds $STUB_DOCKER_FAIL_ON fails; every other one succeeds.
STUB_DOCKER = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$DOCKER_CALLS"
if [ -n "${STUB_DOCKER_FAIL_ON:-}" ]; then
  case "$*" in *"$STUB_DOCKER_FAIL_ON"*) exit 1 ;; esac
fi
exit 0
"""
WORKER_POOL = "projects/dev-project/locations/us-east4/workerPools/dev-pool"
STEP_BUILD_FAILED_LINE = "Failed to execute step: 2. Build & Push Agent Image"


def substitutions(call: str) -> dict[str, str]:
    """The `--substitutions` of one recorded `gcloud builds submit` call."""
    [word] = [word for word in call.split() if word.startswith("--substitutions=")]
    return dict(pair.split("=", 1) for pair in word.split("=", 1)[1].split(","))


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
        for name, text in (("gcloud", STUB_GCLOUD), ("kubectl", STUB_KUBECTL), ("docker", STUB_DOCKER)):
            stub = stub_bin / name
            stub.write_text(text)
            stub.chmod(0o755)
        self.calls = self.root / "kubectl-calls.log"
        self.gcloud_calls = self.root / "gcloud-calls.log"
        self.docker_calls = self.root / "docker-calls.log"
        self.env = {
            **{k: v for k, v in os.environ.items() if k in ("TMPDIR", "LANG", "LC_ALL")},
            "PATH": f"{stub_bin}:/usr/bin:/bin",
            "HOME": str(self.root),
            "PROJECT_ID": "dev-project",
            "REGION": "us-east4",
            "CLUSTER_NAME": "dev-cluster",
            "KUBECTL_CALLS": str(self.calls),
            "GCLOUD_CALLS": str(self.gcloud_calls),
            "DOCKER_CALLS": str(self.docker_calls),
        }

    def run_script(self, *args, set_image_exit=0, with_callout=False, get_deployment_exit=0, extra_env=None):
        env = {
            **self.env,
            **(extra_env or {}),
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

    def deployed_agent_image(self) -> str:
        """The agent image that the script put on the Deployment: what the bridge must match."""
        [call] = [call for call in self.calls.read_text().splitlines() if call.startswith("set image")]
        [image] = [word.split("=", 1)[1] for word in call.split() if word.startswith("platform-agent=")]
        return image

    def test_the_cloud_build_path_builds_the_bridge_from_the_agent_image_it_just_built(self):
        proc = self.run_script("platform")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        builds = [call for call in self.gcloud_calls.read_text().splitlines() if call.startswith("builds submit")]
        self.assertEqual(len(builds), 3, builds)
        agent, proxy, bridge = (substitutions(call) for call in builds)
        self.assertEqual(agent["_TARGET"], "platform")
        self.assertEqual(proxy["_TARGET"], "credential-proxy")
        self.assertIn(f"--config={BRIDGE_BUILD_CONFIG}", builds[2].split())
        agent_uri = self.deployed_agent_image()
        tag = agent_uri.rsplit(":", 1)[1]
        self.assertEqual(agent_uri, f"{REGISTRY}/platform-agent:{tag}")
        self.assertEqual(agent["_IMAGE_URI"], agent_uri)
        # The operator takes the bridge from the deployed agent image: same path, same tag.
        self.assertEqual(bridge, {"_BRIDGE_URI": f"{REGISTRY}/hermes-bridge:{tag}", "_PLATFORM_URI": agent_uri})
        self.assertIn(f"{REGISTRY}/hermes-bridge:{tag}", proc.stdout)

    def test_the_bridge_build_config_takes_what_the_script_sends_and_pushes_the_image(self):
        self.run_script("platform")
        [bridge] = [call for call in self.gcloud_calls.read_text().splitlines() if BRIDGE_BUILD_CONFIG in call]
        config = yaml.safe_load((REPO / BRIDGE_BUILD_CONFIG).read_text())
        self.assertEqual(set(config["substitutions"]), set(substitutions(bridge)))
        self.assertEqual(config["images"], ["$_BRIDGE_URI"])
        [step] = config["steps"]
        args = step["args"]
        self.assertEqual(args[0], "build")
        self.assertEqual(args[args.index("-t") + 1], "$_BRIDGE_URI")
        self.assertEqual(args[args.index("--build-arg") + 1], "PLATFORM_AGENT_IMAGE=$_PLATFORM_URI")
        self.assertEqual(args[args.index("-f") + 1], BRIDGE_DOCKERFILE)
        self.assertEqual(args[args.index("--platform") + 1], "linux/amd64")
        # The context is the a2a module, where the Dockerfile's `COPY . .` reads from.
        self.assertEqual(args[-1], "a2a")
        self.assertTrue((REPO / BRIDGE_DOCKERFILE).is_file())

    def test_the_worker_pool_reaches_all_three_builds(self):
        proc = self.run_script("platform", extra_env={"CLOUD_BUILD_WORKER_POOL": WORKER_POOL})
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        builds = [call for call in self.gcloud_calls.read_text().splitlines() if call.startswith("builds submit")]
        self.assertEqual(len(builds), 3, builds)
        for call in builds:
            self.assertIn(f"--worker-pool={WORKER_POOL}", call.split(), call)

    def test_the_local_path_builds_and_pushes_the_bridge_after_the_agent_image_is_pushed(self):
        proc = self.run_script("platform", "--local")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        calls = self.docker_calls.read_text().splitlines()
        agent_uri = self.deployed_agent_image()
        tag = agent_uri.rsplit(":", 1)[1]
        bridge_uri = f"{REGISTRY}/hermes-bridge:{tag}"
        [bridge_build] = [index for index, call in enumerate(calls) if call.startswith("build ") and BRIDGE_DOCKERFILE in call]
        words = calls[bridge_build].split()
        # One tag only, and it is the tag of the deployed agent image, not `latest`.
        self.assertEqual([words[index + 1] for index, word in enumerate(words) if word == "-t"], [bridge_uri])
        self.assertIn(f"PLATFORM_AGENT_IMAGE={agent_uri}", words)
        self.assertEqual(words[words.index("--platform") + 1], "linux/amd64")
        self.assertEqual(words[-1], str(self.root / "a2a"))
        self.assertLess(calls.index(f"push {agent_uri}"), bridge_build)
        self.assertGreater(calls.index(f"push {bridge_uri}"), bridge_build)

    def test_a_failed_bridge_build_stops_the_run_before_the_redeploy(self):
        proc = self.run_script("platform", "--local", extra_env={"STUB_DOCKER_FAIL_ON": BRIDGE_DOCKERFILE})
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn(STEP_BUILD_FAILED_LINE, proc.stdout + proc.stderr)
        self.assertNotIn(SUCCESS_LINE, proc.stdout)
        self.assertFalse([call for call in self.docker_calls.read_text().splitlines() if call.startswith("push ") and "hermes-bridge" in call])
        kubectl_calls = self.calls.read_text().splitlines() if self.calls.exists() else []
        self.assertFalse([call for call in kubectl_calls if call.startswith(("set image", "patch "))])


if __name__ == "__main__":
    unittest.main()
