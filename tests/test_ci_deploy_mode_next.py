"""The smoke pipeline's EVAL_MODE_NEXT flag leaves the default path untouched.

`hack/ci-deploy.sh` flips the eval install to `spec.mode: next` when
`EVAL_MODE_NEXT=1`, and only then. Every pull request in the repository runs
the script with the flag unset, so the property most worth pinning is the
negative one: with the flag unset, the build submits exactly the substitutions
it submitted before the flag existed, the Helm install gets no extra value, and
step 6b makes no kubectl call at all. The positive half is pinned by the same
lifting technique tests/test_ci_deploy_rc_images.py uses: section 4 run with
the flag set names the four next-stack images and fills the operator.extraEnv
values the release expands (the three image overrides and the inject door's
flag), the Cloud Build `a2a` step run with `docker` stubbed builds and pushes
those four in order with the bridge FROM this build's platform image, the
sidecar patch rendered from a fixture Deployment carries what the bridge doc
lists and what the agent container had, and the two refusals (release-candidate
path, Prow run with no pull request) are present where the script says they are.

The names the flag path hands the operator, or reads back from what it
renders, are copied from the operator's Go source, the bridge's, and the eval
script, and pinned against them here: a rename there would otherwise make the
override a silent no-op and the run fail 600 s later on an ImagePullBackOff, a
sidecar that cannot authenticate, or a token read from a Secret that no longer
exists, attributed to the wrong thing.

The gate order in step 6b is pinned as text. The order is the dependency
order the block's comment states (NATS, callout, provisioning Job, agent, the
inject door, the sidecar's roll, the bridge's log line), and two workloads are
deliberately absent from it: the A2A gateway, reported and not gated, and the
shell StatefulSet, which does not change under next.
"""

import json
import pathlib
import re
import subprocess
import sys
import textwrap
import unittest

import yaml

_HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))


_REPO_ROOT = _HERE.parent
_CI_DEPLOY = _REPO_ROOT / "hack" / "ci-deploy.sh"
_CI_EVAL = _REPO_ROOT / "hack" / "ci-eval-pr.sh"
_CLOUDBUILD = _REPO_ROOT / "deploy" / "docker" / "cloudbuild-ci.yaml"
_BRIDGE_DOCKERFILE = _REPO_ROOT / "a2a" / "Dockerfile.hermes-bridge"
_INVENTORY_CHECK = _REPO_ROOT / "hack" / "check-image-inventory.sh"
_CONTROLLER = _REPO_ROOT / "k8s-operator" / "internal" / "controller"
_A2A_MANIFESTS = _CONTROLLER / "platformagent_a2a_manifests.go"
_A2A_CALLOUT = _CONTROLLER / "platformagent_a2a_callout.go"
_A2A_IDENTITIES = _CONTROLLER / "platformagent_a2a_identities.go"
_AGENT_MANIFESTS = _CONTROLLER / "platformagent_manifests.go"
_API_TYPES = _REPO_ROOT / "k8s-operator" / "api" / "v1alpha1" / "common_types.go"
_BRIDGE_MAIN = _REPO_ROOT / "a2a" / "cmd" / "hermes-bridge" / "main.go"
_BRIDGE_GO = _REPO_ROOT / "a2a" / "hermes-bridge" / "bridge.go"
_OPERATOR_TEMPLATE = _REPO_ROOT / "charts" / "kube-agents" / "templates" / "operator-deployment.yaml"

_AR_REPO = "us-central1-docker.pkg.dev/kube-agents-evals/kube-agents"
_TAG = "pr-1686-abc1234"
_A2A_SUBSTITUTIONS = ("_A2A_GATEWAY_URI", "_A2A_CALLOUT_URI", "_A2A_WORKER_URI", "_A2A_BRIDGE_URI")
_A2A_IMAGES = ("a2a-gateway", "a2a-authcallout", "a2a-worker", "hermes-bridge")
_A2A_DOCKERFILE_SUFFIXES = ("gateway", "authcallout", "worker", "hermes-bridge")
_PLATFORM_URI = f"{_AR_REPO}/platform-agent:{_TAG}"
_FLAG_UNSET_SPELLINGS = (None, "", "0", "true", "yes")

_BUILD_SECTION = (r"^# ─── 4\. Build Container Images.*?", r"^# ─── 5\. Chart Deployment")
_MODE_SECTION = (r"^# ─── 6b\. EVAL_MODE_NEXT.*?", r"^# ─── 7\. Agent API Connectivity")
_SIDECAR_FUNCTION = "render_mode_next_sidecar_patch"
# The function embeds a python program whose dict literal closes with a `}` in
# column 0, which _lift_shell's closing-brace rule would take for the end of
# the function; its real end is the line that hands the program its arguments.
_SIDECAR_FUNCTION_RE = rf"^{_SIDECAR_FUNCTION}\(\) \{{\n.*?^' \"\$@\"\n\}}\n"

# The fixture the sidecar renderer is fed: the shape of the agent Deployment
# the operator renders under next, reduced to what the renderer reads and what
# it must not copy.
_AGENT_ENV = [
    {"name": "PLATFORM_AGENT_HOME", "value": "/opt/data"},
    {"name": "HOME", "value": "/opt/data/home"},
    {"name": "API_SERVER_KEY", "valueFrom": {"secretKeyRef": {"name": "platform-agent-secrets", "key": "API_SERVER_KEY"}}},
    {"name": "NATS_URL", "value": "nats://from-the-agent:4222"},
    {"name": "A2A_BUS_USER", "value": "agent"},
    {"name": "AGENT_SHARED_STATE_SETUP", "value": "owner"},
    {"name": "PATH", "value": "/opt/hermes/.venv/bin:/usr/bin"},
]
_AGENT_MOUNTS = [
    {"name": "platform-agent-data-vol", "mountPath": "/opt/data"},
    {"name": "platform-agent-managed-vol", "mountPath": "/etc/hermes"},
    {"name": "a2a-bus-token", "mountPath": "/var/run/secrets/a2a-bus", "readOnly": True},
    {"name": "tmp-scratch", "mountPath": "/tmp"},
]
_AGENT_SECURITY_CONTEXT = {
    "allowPrivilegeEscalation": False,
    "readOnlyRootFilesystem": True,
    "capabilities": {"drop": ["ALL"]},
}
_AGENT_RESOURCES = {"requests": {"cpu": "1", "memory": "2Gi"}, "limits": {"cpu": "3", "memory": "8Gi"}}
_AGENT_DEPLOYMENT = {
    "spec": {
        "template": {
            "spec": {
                "containers": [
                    {
                        "name": "platform-agent",
                        "image": _PLATFORM_URI,
                        "imagePullPolicy": "Always",
                        "env": _AGENT_ENV,
                        "envFrom": [{"secretRef": {"name": "extra"}}],
                        "ports": [{"name": "api", "containerPort": 8642}],
                        "readinessProbe": {"httpGet": {"path": "/health", "port": 8642}},
                        "volumeMounts": _AGENT_MOUNTS,
                        "securityContext": _AGENT_SECURITY_CONTEXT,
                        "resources": _AGENT_RESOURCES,
                    },
                    {
                        "name": "platform-agent-dashboard",
                        "image": _PLATFORM_URI,
                        "env": [{"name": "AGENT_SHARED_STATE_SETUP", "value": "skip"}],
                        "volumeMounts": [{"name": "platform-agent-data-vol", "mountPath": "/opt/data"}],
                    },
                ]
            }
        }
    }
}


def text(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8")


def lifted(start: str, stop: str) -> str:
    match = re.search(rf"{start}(?={stop})", text(_CI_DEPLOY), re.DOTALL | re.MULTILINE)
    if match is None:  # pragma: no cover - a re-banner should say so loudly
        raise AssertionError(f"no section matching {start!r} in {_CI_DEPLOY}")
    return match.group(0)


def constants() -> dict[str, str]:
    """The script's readonly block, name to literal value (unexpanded)."""
    found = {}
    for line in text(_CI_DEPLOY).splitlines():
        match = re.match(r"readonly ([A-Z0-9_]+)=(.*)$", line)
        if match:
            value = match.group(2)
            # One matching pair of outer quotes, not every quote character: two
            # of the constants are JSON fragments whose quotes are the value.
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            found[match.group(1)] = value
    return found


def constants_block() -> str:
    return "\n".join(line for line in text(_CI_DEPLOY).splitlines() if line.startswith("readonly "))


def go_constant(path: pathlib.Path, name: str) -> str:
    match = re.search(rf"^\s*{name}\s*=\s*\"([^\"]+)\"", text(path), re.MULTILINE)
    if match is None:
        raise AssertionError(f"{name} not found in {path}")
    return match.group(1)


def go_int_constant(path: pathlib.Path, name: str) -> int:
    match = re.search(rf"^\s*{name}\s*=\s*(\d+)\b", text(path), re.MULTILINE)
    if match is None:
        raise AssertionError(f"{name} not found in {path}")
    return int(match.group(1))


def run_bash(script: str, env_lines: list[str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", "\n".join(["set -euo pipefail", *(env_lines or []), script])],
        capture_output=True,
        text=True,
        check=False,
    )


def flag_line(mode_next: str | None) -> str:
    return "" if mode_next is None else f'export EVAL_MODE_NEXT="{mode_next}"'


def run_build_section(mode_next: str | None) -> subprocess.CompletedProcess:
    """Run section 4 with `gcloud` stubbed to print its argv, then print the
    operator.extraEnv values it left for the release."""
    return run_bash(
        "\n".join(
            [
                f'export AR_REPO="{_AR_REPO}"',
                f'export TAG="{_TAG}"',
                'export PROJECT_ID="kube-agents-evals"',
                'export HERMES_AGENT_TAG="v0"',
                "BUILD_WORKER_ARGS=(--machine-type=e2-highcpu-8)",
                "A2A_OPERATOR_ENV_ARGS=()",
                'gcloud() { printf "%s\\n" "$@"; }',
                flag_line(mode_next),
                constants_block(),
                lifted(*_BUILD_SECTION),
                'for arg in ${A2A_OPERATOR_ENV_ARGS[@]+"${A2A_OPERATOR_ENV_ARGS[@]}"}; do echo "HELM=${arg}"; done',
            ]
        )
    )


def substitutions(result: subprocess.CompletedProcess) -> str:
    for line in result.stdout.splitlines():
        if line.startswith("--substitutions="):
            return line.partition("=")[2]
    raise AssertionError(f"no --substitutions in:\n{result.stdout}\n{result.stderr}")


def helm_args(result: subprocess.CompletedProcess) -> list[str]:
    return [line.partition("=")[2] for line in result.stdout.splitlines() if line.startswith("HELM=")]


def sidecar_function() -> str:
    match = re.search(_SIDECAR_FUNCTION_RE, text(_CI_DEPLOY), re.DOTALL | re.MULTILINE)
    if match is None:
        raise AssertionError(f"{_CI_DEPLOY} no longer defines {_SIDECAR_FUNCTION}() in the shape this test lifts")
    return match.group(0)


def render_sidecar(deployment: dict, concurrency: str = "4") -> dict:
    """Run the renderer the way step 6b calls it, on a fixture Deployment."""
    consts = constants()
    args = [
        consts["AGENT_CONTAINER_NAME"],
        consts["BRIDGE_SIDECAR_NAME"],
        f"{_AR_REPO}/hermes-bridge:{_TAG}",
        consts["BRIDGE_NATS_URL_ENV_VAR"],
        "nats://platform-agent-a2a-nats.kubeagents-system.svc:4222",
        consts["BRIDGE_NATS_USER_ENV_VAR"],
        consts["A2A_BRIDGE_USER"],
        consts["BRIDGE_NATS_PASSWORD_ENV_VAR"],
        "platform-agent-a2a-nats-creds",
        consts["A2A_BRIDGE_PASSWORD_KEY"],
        consts["BRIDGE_CONCURRENCY_ENV_VAR"],
        concurrency,
        consts["A2A_BUS_TOKEN_VOLUME"],
        consts["AGENT_SHARED_STATE_SETUP_ENV_VAR"],
        consts["AGENT_SHARED_STATE_SETUP_SKIP"],
    ]
    quoted = " ".join(f"'{a}'" for a in args)
    result = subprocess.run(
        ["bash", "-c", f"set -euo pipefail\n{sidecar_function()}\n{_SIDECAR_FUNCTION} {quoted}"],
        input=json.dumps(deployment),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return json.loads(result.stdout)


def a2a_step() -> dict:
    steps = yaml.safe_load(text(_CLOUDBUILD))["steps"]
    for step in steps:
        if step.get("id") == "a2a":
            return step
    raise AssertionError("cloudbuild-ci.yaml has no step with id a2a")


def run_a2a_step(subs: dict[str, str], docker_stub: str) -> subprocess.CompletedProcess:
    """Run the a2a step's script as Cloud Build would: `$$` collapsed to `$`
    and each `$_NAME` replaced by its substitution, with `docker` stubbed."""
    script = a2a_step()["args"][1].replace("$$", "$")
    for name, value in subs.items():
        script = script.replace(f"${name}", value)
    return run_bash(f"{docker_stub}\n{script}")


class FlagUnsetIsTodayTest(unittest.TestCase):
    def test_the_build_submits_no_a2a_substitution_and_no_helm_value(self) -> None:
        for value in _FLAG_UNSET_SPELLINGS:
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
        config = yaml.safe_load(text(_CLOUDBUILD))
        self.assertEqual(config["images"], ["$_PLATFORM_URI", "$_PROXY_URI", "$_SANDBOX_URI", "$_OPERATOR_URI"])
        for name in _A2A_SUBSTITUTIONS:
            self.assertEqual(config["substitutions"][name], "")

    def test_the_a2a_step_is_a_no_op_with_its_substitutions_empty(self) -> None:
        result = run_a2a_step(
            {name: "" for name in (*_A2A_SUBSTITUTIONS, "_PLATFORM_URI")},
            'docker() { echo "docker must not run with the substitutions empty: $*" >&2; exit 99; }',
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(result.stdout.splitlines()), 1, result.stdout)

    def test_step_6b_makes_no_kubectl_call(self) -> None:
        """The whole of step 6b sits inside the flag's guard: with it unset,
        the lifted section defines its functions and exits without touching
        the cluster."""
        for value in _FLAG_UNSET_SPELLINGS:
            with self.subTest(EVAL_MODE_NEXT=value):
                result = run_bash(
                    "\n".join(
                        [
                            'export NAMESPACE="kubeagents-system"',
                            'export A2A_BRIDGE_URI=""',
                            'kubectl() { echo "kubectl must not run with the flag unset: $*" >&2; exit 99; }',
                            'python3() { echo "python3 must not run with the flag unset: $*" >&2; exit 99; }',
                            flag_line(value),
                            constants_block(),
                            lifted(*_MODE_SECTION),
                        ]
                    )
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertEqual(result.stderr, "")


class FlagSetIsNextTest(unittest.TestCase):
    def test_the_build_names_the_four_next_stack_images(self) -> None:
        result = run_build_section("1")
        self.assertEqual(result.returncode, 0, result.stderr)
        subs = substitutions(result)
        for name, image in zip(_A2A_SUBSTITUTIONS, _A2A_IMAGES, strict=True):
            self.assertIn(f"{name}={_AR_REPO}/{image}:{_TAG}", subs)

    def test_the_release_gets_the_three_overrides_and_the_inject_flag_as_operator_extra_env(self) -> None:
        result = run_build_section("1")
        self.assertEqual(result.returncode, 0, result.stderr)
        args = helm_args(result)
        env_vars = (
            go_constant(_A2A_MANIFESTS, "a2aGatewayImageEnvVar"),
            go_constant(_A2A_CALLOUT, "a2aCalloutImageEnvVar"),
            go_constant(_A2A_MANIFESTS, "a2aWorkerImageEnvVar"),
        )
        expected = []
        for index, (env_var, image) in enumerate(zip(env_vars, _A2A_IMAGES[:3], strict=True)):
            expected += [
                "--set-string",
                f"operator.extraEnv[{index}].name={env_var}",
                "--set-string",
                f"operator.extraEnv[{index}].value={_AR_REPO}/{image}:{_TAG}",
            ]
        # The inject door is armed the same way, and the operator opens it only
        # on the exact word its reader compares against.
        inject_env_var = go_constant(_A2A_MANIFESTS, "a2aInjectBackendEnvVar")
        self.assertRegex(text(_A2A_MANIFESTS), rf'os\.Getenv\({re.escape("a2aInjectBackendEnvVar")}\) == "true"')
        expected += [
            "--set-string",
            f"operator.extraEnv[3].name={inject_env_var}",
            "--set-string",
            "operator.extraEnv[3].value=true",
        ]
        self.assertEqual(args, expected)
        # The bridge image goes to the CR, not to the operator.
        self.assertNotIn("hermes-bridge", " ".join(args))
        # The chart renders the value the array names, last in the container's env.
        self.assertIn(".Values.operator.extraEnv", text(_OPERATOR_TEMPLATE))
        # And the release expands the array.
        release = re.search(r"helm upgrade --install.*?--wait --timeout", text(_CI_DEPLOY), re.DOTALL)
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
        nats_suffix = go_constant(_API_TYPES, "a2aNATSNameSuffix")
        creds_suffix = go_constant(_API_TYPES, "a2aCredsSecretSuffix")
        callout_suffix = go_constant(_API_TYPES, "a2aCalloutNameSuffix")
        self.assertEqual(consts["A2A_NATS_SERVICE_NAME"], "${PLATFORM_AGENT_CR_NAME}" + nats_suffix)
        self.assertEqual(consts["A2A_CREDS_SECRET_NAME"], "${A2A_NATS_SERVICE_NAME}" + creds_suffix)
        self.assertEqual(consts["A2A_NATS_POD_SELECTOR"], "app=${PLATFORM_AGENT_CR_NAME}" + nats_suffix)
        self.assertEqual(int(consts["A2A_NATS_CLIENT_PORT"]), go_int_constant(_A2A_MANIFESTS, "a2aNATSClientPort"))
        manifests = text(_A2A_MANIFESTS)
        self.assertRegex(manifests, r'func a2aGatewayName\(.*\) string\s*{\s*return agent\.Name \+ "-a2a-gateway"')
        self.assertRegex(manifests, r'func a2aInjectName\(.*\) string\s*{\s*return agent\.Name \+ "-a2a-inject"')
        self.assertEqual(consts["A2A_INJECT_NAME"], "${PLATFORM_AGENT_CR_NAME}-a2a-inject")
        self.assertEqual(consts["A2A_INJECT_TOKEN_KEY"], go_constant(_A2A_MANIFESTS, "a2aInjectTokenKey"))
        self.assertEqual(consts["A2A_BRIDGE_USER"], go_constant(_A2A_IDENTITIES, "a2aBridgeUser"))
        self.assertEqual(consts["A2A_BRIDGE_PASSWORD_KEY"], go_constant(_A2A_MANIFESTS, "a2aBridgePasswordKey"))
        self.assertEqual(consts["A2A_BUS_TOKEN_VOLUME"], go_constant(_A2A_CALLOUT, "a2aBusTokenVolume"))
        self.assertIn(f'"{consts["A2A_BUS_TOKEN_VOLUME"]}": {{}}', text(_API_TYPES))
        self.assertEqual(consts["AGENT_SHARED_STATE_SETUP_ENV_VAR"], go_constant(_AGENT_MANIFESTS, "sharedStateSetupEnvVar"))
        self.assertEqual(consts["AGENT_SHARED_STATE_SETUP_SKIP"], go_constant(_AGENT_MANIFESTS, "sharedStateSetupSkip"))
        self.assertIn(f'Name:            "{consts["AGENT_CONTAINER_NAME"]}",', text(_AGENT_MANIFESTS))
        block = lifted(*_MODE_SECTION)
        self.assertIn('-a2a-callout"', block)
        self.assertEqual(callout_suffix, "-a2a-callout")
        self.assertIn('-a2a-gateway"', block)
        self.assertRegex(manifests, r'podLabels := map\[string\]string{"app": name}')
        managed_env_key = go_constant(_AGENT_MANIFESTS, "managedEnvKey")
        self.assertIn(f"jsonpath='{{.data.{managed_env_key.replace('.', chr(92) + '.')}}}'", block)

    def test_the_bridge_env_and_log_line_match_the_bridge_source(self) -> None:
        consts = constants()
        main_go = text(_BRIDGE_MAIN)
        for const in ("BRIDGE_NATS_URL_ENV_VAR", "BRIDGE_NATS_USER_ENV_VAR", "BRIDGE_NATS_PASSWORD_ENV_VAR"):
            with self.subTest(const=const):
                self.assertIn(f'os.Getenv("{consts[const]}")', main_go)
        self.assertIn(f'"{consts["BRIDGE_CONCURRENCY_ENV_VAR"]}"', main_go)
        self.assertEqual(int(consts["BRIDGE_QUEUE_CAPACITY"]), go_int_constant(_BRIDGE_GO, "taskQueueCapacity"))
        self.assertIn('Info("hermes bridge consuming", "profile", b.cfg.Profile)', text(_BRIDGE_GO))
        self.assertEqual(consts["BRIDGE_CONSUMING_LOG_MSG"], '"msg":"hermes bridge consuming"')
        self.assertEqual(consts["BRIDGE_CONSUMING_LOG_PROFILE"], f'"profile":"{go_constant(_BRIDGE_MAIN, "defaultProfile")}"')

    def test_the_concurrency_default_is_the_eval_scripts_and_fits_the_queue(self) -> None:
        consts = constants()
        eval_default = re.search(r'^EVAL_TASK_PARALLELISM="\$\{EVAL_TASK_PARALLELISM:-(\d+)\}"$', text(_CI_EVAL), re.MULTILINE)
        self.assertIsNotNone(eval_default, "hack/ci-eval-pr.sh no longer defaults EVAL_TASK_PARALLELISM where this test reads it")
        self.assertEqual(int(consts["EVAL_TASK_PARALLELISM_DEFAULT"]), int(eval_default.group(1)))
        self.assertLessEqual(int(consts["EVAL_TASK_PARALLELISM_DEFAULT"]), int(consts["BRIDGE_QUEUE_CAPACITY"]))
        block = lifted(*_MODE_SECTION)
        self.assertIn('MODE_NEXT_BRIDGE_CONCURRENCY="${EVAL_TASK_PARALLELISM:-${EVAL_TASK_PARALLELISM_DEFAULT}}"', block)

    def test_the_release_candidate_path_refuses_the_flag(self) -> None:
        script = text(_CI_DEPLOY)
        rc_branch = script.index('if [ -n "${RC_COMMIT_SHA:-}" ]; then')
        refusal = script.index('if [ "${EVAL_MODE_NEXT:-}" = "1" ]; then', rc_branch)
        self.assertLess(refusal - rc_branch, 600, "the refusal belongs at the top of the RC branch")

    def test_a_prow_run_without_a_pull_request_refuses_the_flag(self) -> None:
        self.assertIn(
            'if [ "${EVAL_MODE_NEXT:-}" = "1" ] && [ "${IS_PROW_RUN}" = "true" ] && [ -z "${PULL_NUMBER:-}" ]; then',
            text(_CI_DEPLOY),
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
                "deployment/${AGENT_DEPLOYMENT_NAME}",
            ],
        )
        markers = [
            'gate_mode_next_rollout "deployment/${PLATFORM_AGENT_CR_NAME}-a2a-callout"',
            "kubectl wait --for=condition=complete jobs",
            'gate_mode_next_rollout "deployment/${AGENT_DEPLOYMENT_NAME}"',
            'kubectl get "service/${A2A_INJECT_NAME}" "secret/${A2A_INJECT_NAME}"',
            "render_mode_next_sidecar_patch \\",
            'kubectl patch platformagent "${PLATFORM_AGENT_CR_NAME}" -n "${NAMESPACE}" --type merge -p "${SIDECAR_PATCH}"',
            'wait_agent_generation_past "${SIDECAR_GEN_BEFORE}"',
            'gate_mode_next_rollout "deployment/${AGENT_DEPLOYMENT_NAME}"',
            'grep -F "${BRIDGE_CONSUMING_LOG_MSG}" | grep -F "${BRIDGE_CONSUMING_LOG_PROFILE}"',
        ]
        position = 0
        for marker in markers:
            with self.subTest(marker=marker):
                found = block.find(marker, position)
                self.assertGreater(found, -1, f"{marker!r} is missing after offset {position}")
                position = found + len(marker)
        for never_gated in ("a2a-gateway", "platform-agent-shell"):
            with self.subTest(never_gated=never_gated):
                for line in block.splitlines():
                    if "rollout status" in line or "gate_mode_next_rollout" in line:
                        self.assertNotIn(never_gated, line)

    def test_the_generation_is_read_before_each_patch(self) -> None:
        block = lifted(*_MODE_SECTION)
        self.assertLess(block.index("GEN_BEFORE="), block.index("kubectl patch platformagent"))
        sidecar_patch = block.index('--type merge -p "${SIDECAR_PATCH}"')
        self.assertLess(block.index("SIDECAR_GEN_BEFORE="), sidecar_patch)
        # And the render reads the Deployment after the mode roll, so the
        # environment it copies is the next-mode one.
        self.assertLess(block.index('gate_mode_next_rollout "deployment/${AGENT_DEPLOYMENT_NAME}"'), block.index("SIDECAR_PATCH="))


class SidecarPatchTest(unittest.TestCase):
    """render_mode_next_sidecar_patch, run on the fixture Deployment."""

    def setUp(self) -> None:
        self.consts = constants()
        self.patch = render_sidecar(_AGENT_DEPLOYMENT)
        sidecars = self.patch["spec"]["deployment"]["sidecars"]
        self.assertEqual(len(sidecars), 1)
        self.sidecar = sidecars[0]
        self.assertEqual(set(self.patch), {"spec"})
        self.assertEqual(set(self.patch["spec"]), {"deployment"})
        self.assertEqual(set(self.patch["spec"]["deployment"]), {"sidecars"})

    def test_it_is_the_bridge_image_named_as_the_script_names_it(self) -> None:
        self.assertEqual(self.sidecar["name"], self.consts["BRIDGE_SIDECAR_NAME"])
        self.assertEqual(self.sidecar["image"], f"{_AR_REPO}/hermes-bridge:{_TAG}")
        self.assertEqual(self.sidecar["imagePullPolicy"], "Always")

    def test_it_carries_the_agents_environment_plus_the_bridges_own(self) -> None:
        env = self.sidecar["env"]
        names = [e["name"] for e in env]
        # The agent's own entries, in their order, minus what the bridge sets itself.
        self.assertEqual(names[:5], ["PLATFORM_AGENT_HOME", "HOME", "API_SERVER_KEY", "A2A_BUS_USER", "PATH"])
        self.assertEqual(env[2], _AGENT_ENV[2], "a valueFrom entry is copied whole")
        # Then the bridge's, as the bridge doc lists them, and the entrypoint switch.
        self.assertEqual(
            env[5:],
            [
                {"name": "AGENT_SHARED_STATE_SETUP", "value": "skip"},
                {"name": "NATS_URL", "value": "nats://platform-agent-a2a-nats.kubeagents-system.svc:4222"},
                {"name": "NATS_USER", "value": "bridge"},
                {"name": "NATS_PASSWORD", "valueFrom": {"secretKeyRef": {"name": "platform-agent-a2a-nats-creds", "key": "bridge-password"}}},
                {"name": "BRIDGE_CONCURRENCY", "value": "4"},
            ],
        )
        self.assertEqual(names.count("NATS_URL"), 1, "the agent's NATS_URL is replaced, not shadowed")
        self.assertEqual(names.count("AGENT_SHARED_STATE_SETUP"), 1)
        self.assertEqual(self.sidecar["envFrom"], [{"secretRef": {"name": "extra"}}])

    def test_it_mounts_what_the_agent_mounts_except_the_reserved_bus_token(self) -> None:
        mounts = self.sidecar["volumeMounts"]
        self.assertEqual([m["name"] for m in mounts], ["platform-agent-data-vol", "platform-agent-managed-vol", "tmp-scratch"])
        self.assertNotIn(self.consts["A2A_BUS_TOKEN_VOLUME"], [m["name"] for m in mounts])

    def test_it_copies_security_context_and_resources_and_nothing_that_would_collide(self) -> None:
        self.assertEqual(self.sidecar["securityContext"], _AGENT_SECURITY_CONTEXT)
        self.assertEqual(self.sidecar["resources"], _AGENT_RESOURCES)
        for key in ("ports", "readinessProbe", "livenessProbe", "startupProbe", "command", "args", "lifecycle"):
            self.assertNotIn(key, self.sidecar)
        self.assertEqual(
            set(self.sidecar),
            {"name", "image", "imagePullPolicy", "env", "envFrom", "volumeMounts", "securityContext", "resources"},
        )

    def test_it_reads_the_agent_container_and_not_the_dashboard(self) -> None:
        self.assertNotIn({"name": "AGENT_SHARED_STATE_SETUP", "value": "owner"}, self.sidecar["env"])
        reordered = json.loads(json.dumps(_AGENT_DEPLOYMENT))
        reordered["spec"]["template"]["spec"]["containers"].reverse()
        self.assertEqual(render_sidecar(reordered), self.patch)

    def test_optional_fields_absent_on_the_agent_stay_absent(self) -> None:
        bare = json.loads(json.dumps(_AGENT_DEPLOYMENT))
        agent = bare["spec"]["template"]["spec"]["containers"][0]
        for key in ("imagePullPolicy", "envFrom", "securityContext", "resources", "env", "volumeMounts"):
            agent.pop(key)
        sidecar = render_sidecar(bare, concurrency="6")["spec"]["deployment"]["sidecars"][0]
        self.assertEqual(set(sidecar), {"name", "image", "env", "volumeMounts"})
        self.assertEqual(sidecar["volumeMounts"], [])
        self.assertEqual([e["name"] for e in sidecar["env"]], ["AGENT_SHARED_STATE_SETUP", "NATS_URL", "NATS_USER", "NATS_PASSWORD", "BRIDGE_CONCURRENCY"])
        self.assertEqual(sidecar["env"][-1]["value"], "6")

    def test_the_webhook_would_admit_it(self) -> None:
        """What platformagent_webhook.go checks on a sidecar: no privileged,
        no privilege escalation, no root, no added capabilities, no reserved
        volume mount. The copied context satisfies every one."""
        sc = self.sidecar["securityContext"]
        self.assertFalse(sc.get("privileged", False))
        self.assertFalse(sc.get("allowPrivilegeEscalation", True))
        self.assertNotEqual(sc.get("runAsUser"), 0)
        self.assertEqual(sc.get("capabilities", {}).get("add", []), [])


class BridgeImageBuildTest(unittest.TestCase):
    def test_the_dockerfile_takes_the_platform_image_as_an_argument_with_no_default(self) -> None:
        dockerfile = text(_BRIDGE_DOCKERFILE)
        self.assertRegex(dockerfile, r"(?m)^ARG PLATFORM_AGENT_IMAGE$")
        self.assertNotRegex(dockerfile, r"(?m)^ARG PLATFORM_AGENT_IMAGE=")
        self.assertRegex(dockerfile, r"(?m)^FROM \$\{PLATFORM_AGENT_IMAGE\}$")
        self.assertIn("go build -trimpath -o /out/hermes-bridge ./cmd/hermes-bridge", dockerfile)
        self.assertIn("COPY --from=build /out/hermes-bridge /usr/local/bin/hermes-bridge", dockerfile)
        # The agent entrypoint stays the ENTRYPOINT (the CMD is what changes), so
        # the sidecar's AGENT_SHARED_STATE_SETUP=skip means what it means for the
        # dashboard container.
        self.assertNotRegex(dockerfile, r"(?m)^ENTRYPOINT")
        self.assertRegex(dockerfile, r'(?m)^CMD \["/usr/local/bin/hermes-bridge"\]$')

    def test_the_inventory_check_pins_its_builder_and_states_the_exclusion(self) -> None:
        script = text(_INVENTORY_CHECK)
        self.assertIn("check_base_image golang a2a/Dockerfile.hermes-bridge GOLANG_IMAGE GOLANG_VERSION", script)
        self.assertIn("check_go_directive a2a/Dockerfile.hermes-bridge GOLANG_VERSION a2a/go.mod", script)
        self.assertIn("a2a/Dockerfile.hermes-bridge) is deliberately NOT", script)
        inventory = json.loads(text(_REPO_ROOT / "images.json"))
        self.assertNotIn("hermes-bridge", [image["name"] for image in inventory["images"]])

    def test_the_a2a_step_waits_for_the_platform_image_and_builds_the_four_in_order(self) -> None:
        self.assertEqual(a2a_step()["waitFor"], ["platform"])
        subs = {name: f"{_AR_REPO}/{image}:{_TAG}" for name, image in zip(_A2A_SUBSTITUTIONS, _A2A_IMAGES, strict=True)}
        subs["_PLATFORM_URI"] = _PLATFORM_URI
        result = run_a2a_step(subs, 'docker() { echo "docker $*"; }')
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        expected = []
        for suffix, image in zip(_A2A_DOCKERFILE_SUFFIXES, _A2A_IMAGES, strict=True):
            uri = f"{_AR_REPO}/{image}:{_TAG}"
            build_arg = f" --build-arg PLATFORM_AGENT_IMAGE={_PLATFORM_URI}" if image == "hermes-bridge" else ""
            # The inspect that precedes the bridge build runs with its output
            # discarded, so it leaves no line here; the refusal test below is
            # where it is observed.
            expected.append(f"docker build --platform linux/amd64 -t {uri} -f a2a/Dockerfile.{suffix}{build_arg} a2a")
            expected.append(f"docker push {uri}")
        self.assertEqual(lines, expected)

    def test_the_a2a_step_refuses_a_platform_image_that_is_not_this_builds(self) -> None:
        subs = {name: f"{_AR_REPO}/{image}:{_TAG}" for name, image in zip(_A2A_SUBSTITUTIONS, _A2A_IMAGES, strict=True)}
        subs["_PLATFORM_URI"] = _PLATFORM_URI
        stub = textwrap.dedent(
            """\
            docker() {
              if [ "$1" = "image" ] && [ "$2" = "inspect" ]; then return 1; fi
              echo "docker $*"
            }
            """
        )
        result = run_a2a_step(subs, stub)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("built FROM this run's platform image", result.stderr)
        self.assertNotIn("Dockerfile.hermes-bridge", result.stdout)
        self.assertEqual(len([line for line in result.stdout.splitlines() if " push " in line]), 3)


if __name__ == "__main__":
    unittest.main()
