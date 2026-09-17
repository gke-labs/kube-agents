"""The smoke pipeline's EVAL_MODE_NEXT flag leaves the default path untouched.

`hack/ci-deploy.sh` flips the eval install to `spec.mode: next` when
`EVAL_MODE_NEXT=1`, and only then. Every pull request in the repository runs
the script with the flag unset, so the property most worth pinning is the
negative one: with the flag unset, the build submits exactly the substitutions
it submitted before the flag existed, the Helm install gets no extra value, and
nothing later in the script patches the CR. The positive half is pinned by the
same lifting technique tests/test_ci_deploy_rc_images.py uses: section 4 run
with the flag set names the three A2A images and fills the operator.extraEnv
values the release expands, and the two refusals (release-candidate path, Prow
run with no pull request) are present where the script says they are.

The names the flag path hands the operator, or reads back from what it
renders, are copied from the operator's Go source and pinned against it here:
a rename there would otherwise make the override a silent no-op and the run
fail 600 s later on an ImagePullBackOff attributed to the wrong thing.

The gate order in step 6b is pinned as text. The order is the dependency
order the block's comment states (NATS, callout, provisioning Job, agent), and
two workloads are deliberately absent from it: the A2A gateway, which cannot
start in an eval project (#1660), and the shell StatefulSet, which does not
change under next.
"""

import pathlib
import re
import subprocess
import unittest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CI_DEPLOY = _REPO_ROOT / "hack" / "ci-deploy.sh"
_CLOUDBUILD = _REPO_ROOT / "deploy" / "docker" / "cloudbuild-ci.yaml"
_CONTROLLER = _REPO_ROOT / "k8s-operator" / "internal" / "controller"
_A2A_MANIFESTS = _CONTROLLER / "platformagent_a2a_manifests.go"
_A2A_CALLOUT = _CONTROLLER / "platformagent_a2a_callout.go"
_AGENT_MANIFESTS = _CONTROLLER / "platformagent_manifests.go"
_OPERATOR_TEMPLATE = _REPO_ROOT / "charts" / "kube-agents" / "templates" / "operator-deployment.yaml"

_AR_REPO = "us-central1-docker.pkg.dev/kube-agents-evals/kube-agents"
_TAG = "pr-1686-abc1234"
_A2A_SUBSTITUTIONS = ("_A2A_GATEWAY_URI", "_A2A_CALLOUT_URI", "_A2A_WORKER_URI")
_A2A_IMAGES = ("a2a-gateway", "a2a-authcallout", "a2a-worker")

_BUILD_SECTION = (r"^# ─── 4\. Build Container Images.*?", r"^# ─── 5\. Chart Deployment")
_MODE_SECTION = (r"^# ─── 6b\. EVAL_MODE_NEXT.*?", r"^# ─── 7\. Agent API Connectivity")


def lifted(start: str, stop: str) -> str:
    src = _CI_DEPLOY.read_text(encoding="utf-8")
    match = re.search(rf"{start}(?={stop})", src, re.DOTALL | re.MULTILINE)
    if match is None:  # pragma: no cover - a re-banner should say so loudly
        raise AssertionError(f"no section matching {start!r} in {_CI_DEPLOY}")
    return match.group(0)


def constants() -> dict[str, str]:
    """The script's readonly block, name to literal value (unexpanded)."""
    src = _CI_DEPLOY.read_text(encoding="utf-8")
    found = {}
    for line in src.splitlines():
        match = re.match(r"readonly ([A-Z0-9_]+)=(.*)$", line)
        if match:
            found[match.group(1)] = match.group(2).strip("\"'")
    return found


def constants_block() -> str:
    src = _CI_DEPLOY.read_text(encoding="utf-8")
    return "\n".join(line for line in src.splitlines() if line.startswith("readonly "))


def go_constant(path: pathlib.Path, name: str) -> str:
    match = re.search(rf"^\s*{name}\s*=\s*\"([^\"]+)\"", path.read_text(encoding="utf-8"), re.MULTILINE)
    if match is None:
        raise AssertionError(f"{name} not found in {path}")
    return match.group(1)


def run_build_section(mode_next: str | None) -> subprocess.CompletedProcess:
    """Run section 4 with `gcloud` stubbed to print its argv, then print the
    operator.extraEnv values it left for the release."""
    script = "\n".join(
        [
            "set -euo pipefail",
            f'export AR_REPO="{_AR_REPO}"',
            f'export TAG="{_TAG}"',
            'export PROJECT_ID="kube-agents-evals"',
            'export HERMES_AGENT_TAG="v0"',
            "BUILD_WORKER_ARGS=(--machine-type=e2-highcpu-8)",
            "A2A_OPERATOR_ENV_ARGS=()",
            'gcloud() { printf "%s\\n" "$@"; }',
            "" if mode_next is None else f'export EVAL_MODE_NEXT="{mode_next}"',
            constants_block(),
            lifted(*_BUILD_SECTION),
            'for arg in ${A2A_OPERATOR_ENV_ARGS[@]+"${A2A_OPERATOR_ENV_ARGS[@]}"}; do echo "HELM=${arg}"; done',
        ]
    )
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=False)


def substitutions(result: subprocess.CompletedProcess) -> str:
    for line in result.stdout.splitlines():
        if line.startswith("--substitutions="):
            return line.partition("=")[2]
    raise AssertionError(f"no --substitutions in:\n{result.stdout}\n{result.stderr}")


def helm_args(result: subprocess.CompletedProcess) -> list[str]:
    return [line.partition("=")[2] for line in result.stdout.splitlines() if line.startswith("HELM=")]


class FlagUnsetIsTodayTest(unittest.TestCase):
    def test_the_build_submits_no_a2a_substitution_and_no_helm_value(self) -> None:
        for value in (None, "", "0", "true", "yes"):
            with self.subTest(EVAL_MODE_NEXT=value):
                result = run_build_section(value)
                self.assertEqual(result.returncode, 0, result.stderr)
                subs = substitutions(result)
                for name in _A2A_SUBSTITUTIONS:
                    self.assertNotIn(name, subs)
                self.assertFalse(subs.endswith(","), subs)
                self.assertEqual(helm_args(result), [])

    def test_the_cloud_build_images_list_is_still_the_four(self) -> None:
        """The a2a step pushes from inside itself because `images:` cannot be
        conditional; the list must not have grown to include them."""
        text = _CLOUDBUILD.read_text(encoding="utf-8")
        images = re.search(r"^images:\n((?:  - .*\n)+)", text, re.MULTILINE)
        self.assertIsNotNone(images)
        for name in _A2A_SUBSTITUTIONS:
            self.assertNotIn(name, images.group(1))
        for name in _A2A_SUBSTITUTIONS:
            self.assertRegex(text, rf'(?m)^  {name}: ""$')


class FlagSetIsNextTest(unittest.TestCase):
    def test_the_build_names_the_three_a2a_images(self) -> None:
        result = run_build_section("1")
        self.assertEqual(result.returncode, 0, result.stderr)
        subs = substitutions(result)
        for name, image in zip(_A2A_SUBSTITUTIONS, _A2A_IMAGES, strict=True):
            self.assertIn(f"{name}={_AR_REPO}/{image}:{_TAG}", subs)

    def test_the_release_gets_the_three_overrides_as_operator_extra_env(self) -> None:
        result = run_build_section("1")
        self.assertEqual(result.returncode, 0, result.stderr)
        args = helm_args(result)
        env_vars = (
            go_constant(_A2A_MANIFESTS, "a2aGatewayImageEnvVar"),
            go_constant(_A2A_CALLOUT, "a2aCalloutImageEnvVar"),
            go_constant(_A2A_MANIFESTS, "a2aWorkerImageEnvVar"),
        )
        expected = []
        for index, (env_var, image) in enumerate(zip(env_vars, _A2A_IMAGES, strict=True)):
            expected += [
                "--set-string",
                f"operator.extraEnv[{index}].name={env_var}",
                "--set-string",
                f"operator.extraEnv[{index}].value={_AR_REPO}/{image}:{_TAG}",
            ]
        self.assertEqual(args, expected)
        # The chart renders the value the array names, last in the container's env.
        self.assertIn(".Values.operator.extraEnv", _OPERATOR_TEMPLATE.read_text(encoding="utf-8"))
        # And the release expands the array.
        release = re.search(r"helm upgrade --install.*?--wait --timeout", _CI_DEPLOY.read_text(encoding="utf-8"), re.DOTALL)
        self.assertIsNotNone(release)
        self.assertIn('${A2A_OPERATOR_ENV_ARGS[@]+"${A2A_OPERATOR_ENV_ARGS[@]}"}', release.group(0))

    def test_the_names_the_script_relies_on_match_the_operator_source(self) -> None:
        consts = constants()
        component_label = go_constant(_A2A_MANIFESTS, "a2aComponentLabel")
        provision = go_constant(_A2A_MANIFESTS, "a2aProvisionComponent")
        self.assertEqual(consts["A2A_PROVISION_JOB_SELECTOR"], f"{component_label}={provision}")
        self.assertEqual(
            consts["A2A_PART_OF_SELECTOR"],
            f"app.kubernetes.io/part-of={go_constant(_A2A_MANIFESTS, 'a2aPartOf')}",
        )
        manifests = _A2A_MANIFESTS.read_text(encoding="utf-8")
        for func, suffix in (("a2aNATSName", "-a2a-nats"), ("a2aCalloutName", "-a2a-callout"), ("a2aGatewayName", "-a2a-gateway")):
            with self.subTest(func=func):
                self.assertRegex(manifests, rf'func {func}\(.*\) string\s*{{\s*return agent\.Name \+ "{suffix}"')
        block = lifted(*_MODE_SECTION)
        self.assertIn('-a2a-nats"', block)
        self.assertIn('-a2a-callout"', block)
        self.assertIn('-a2a-gateway"', block)
        self.assertEqual(consts["A2A_NATS_POD_SELECTOR"], "app=${PLATFORM_AGENT_CR_NAME}-a2a-nats")
        self.assertRegex(manifests, r'podLabels := map\[string\]string{"app": name}')
        managed_env_key = go_constant(_AGENT_MANIFESTS, "managedEnvKey")
        self.assertIn(f"jsonpath='{{.data.{managed_env_key.replace('.', chr(92) + '.')}}}'", block)

    def test_the_release_candidate_path_refuses_the_flag(self) -> None:
        text = _CI_DEPLOY.read_text(encoding="utf-8")
        rc_branch = text.index('if [ -n "${RC_COMMIT_SHA:-}" ]; then')
        refusal = text.index('if [ "${EVAL_MODE_NEXT:-}" = "1" ]; then', rc_branch)
        self.assertLess(refusal - rc_branch, 600, "the refusal belongs at the top of the RC branch")

    def test_a_prow_run_without_a_pull_request_refuses_the_flag(self) -> None:
        text = _CI_DEPLOY.read_text(encoding="utf-8")
        self.assertIn(
            'if [ "${EVAL_MODE_NEXT:-}" = "1" ] && [ "${IS_PROW_RUN}" = "true" ] && [ -z "${PULL_NUMBER:-}" ]; then',
            text,
        )

    def test_the_gates_run_in_dependency_order_and_skip_the_two_that_cannot(self) -> None:
        block = lifted(*_MODE_SECTION)
        gated = re.findall(r'gate_mode_next_rollout "(\S+)"', block)
        self.assertEqual(
            gated,
            [
                "statefulset/${PLATFORM_AGENT_CR_NAME}-a2a-nats",
                "deployment/${PLATFORM_AGENT_CR_NAME}-a2a-callout",
                "deployment/${AGENT_DEPLOYMENT_NAME}",
            ],
        )
        job_wait = block.index("kubectl wait --for=condition=complete jobs")
        self.assertLess(block.index('-a2a-callout"'), job_wait)
        self.assertLess(job_wait, block.index('gate_mode_next_rollout "deployment/${AGENT_DEPLOYMENT_NAME}"'))
        for never_gated in ("a2a-gateway", "platform-agent-shell"):
            with self.subTest(never_gated=never_gated):
                for line in block.splitlines():
                    if "rollout status" in line or "gate_mode_next_rollout" in line:
                        self.assertNotIn(never_gated, line)

    def test_the_generation_is_read_before_the_patch(self) -> None:
        block = lifted(*_MODE_SECTION)
        self.assertLess(block.index("GEN_BEFORE="), block.index("kubectl patch platformagent"))


if __name__ == "__main__":
    unittest.main()
