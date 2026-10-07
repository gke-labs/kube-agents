"""The smoke pipeline's EVAL_MODE_NEXT flag leaves the default path untouched.

`hack/ci-deploy.sh` flips the eval install to `spec.mode: next` when
`EVAL_MODE_NEXT=1`, and only then. Every pull request in the repository runs
the script with the flag unset, so the property most worth pinning is the
negative one: with the flag unset, the build submits exactly the substitutions
it submitted before the flag existed, the Helm install gets no extra value, and
step 6b makes no kubectl call at all. The positive half is pinned by the same
lifting technique tests/test_ci_deploy_rc_images.py uses: section 4 run with
the flag set names the six next-stack images and fills the operator.extraEnv
values the release expands (the five image overrides, the inject door's flag,
and the three settings of the bridge the operator renders: its image, its
concurrency and its executor pin), the Cloud Build's `a2a` and `a2a-bridge`
steps run with `docker` stubbed build and push those six in order with the
bridge FROM this build's platform image, the three guards (release-candidate
path, Prow run with no pull request that is not a next-lane job, the
concurrency's grammar) run against the values they refuse and admit, and the
release-candidate refusal sits at the top of the branch the script says it
guards.

The names the flag path hands the operator, or reads back from what it
renders, are copied from the operator's Go source, the bridge's, and the eval
script, and pinned against them here: a rename there would otherwise make the
override a silent no-op and the run fail 600 s later on an ImagePullBackOff, a
bridge at the wrong concurrency or executor, or a token read from a Secret that
no longer exists, attributed to the wrong thing.

The gate order in step 6b is pinned as text. The order is the dependency
order the block's comment states (NATS, callout, provisioning Job, agent, the
inject door, the bridge's log line), and two workloads are deliberately absent
from it: the A2A gateway, reported and not gated, and the shell StatefulSet,
which does not change under next.

The TASKS budget (gke-labs/kube-agents#2077, #2592). The operator sizes the
TASKS stream's consumer budget from the CR's maxSessions and from the bridge
workers the pod runs, creates the stream at the larger of that budget and a
floor of 64, never edits a stream that exists, and refuses a later render
whose budget the live stream cannot hold. Since #2592 the operator renders the
bridge from the first next render, with the concurrency step 5 sets on it, so
the mode patch is the only patch and the one provision Job already counts the
bridge's workers. The first patch still carries a maxSessions sized so that
budget fits the floor, computed from four constants copied from the
operator's and pinned against them (the Go side's
TestCiDeploySizesMaxSessionsToTheTasksFloor pins the same four against the
functions that derive them). The provision wait's re-render form and the
Degraded gate that read the CR after the old sidecar patch's re-run are no
longer called by step 6b; their function-level tests stay, against the code
that ships.
"""

import json
import pathlib
import re
import shlex
import subprocess
import textwrap
import unittest

import yaml

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CI_DEPLOY = _REPO_ROOT / "hack" / "ci-deploy.sh"
_CI_EVAL = _REPO_ROOT / "hack" / "ci-eval-pr.sh"
_CLOUDBUILD = _REPO_ROOT / "deploy" / "docker" / "cloudbuild-ci.yaml"
_BRIDGE_DOCKERFILE = _REPO_ROOT / "a2a" / "Dockerfile.hermes-bridge"
_INVENTORY_CHECK = _REPO_ROOT / "hack" / "check-image-inventory.sh"
_CONTROLLER = _REPO_ROOT / "k8s-operator" / "internal" / "controller"
_A2A_MANIFESTS = _CONTROLLER / "platformagent_a2a_manifests.go"
_A2A_CALLOUT = _CONTROLLER / "platformagent_a2a_callout.go"
_A2A_VERIFIER = _CONTROLLER / "platformagent_a2a_verifier.go"
_A2A_CONSOLE = _CONTROLLER / "platformagent_a2a_console.go"
_A2A_BRIDGE = _CONTROLLER / "platformagent_a2a_bridge.go"
_AGENT_MANIFESTS = _CONTROLLER / "platformagent_manifests.go"
_API_TYPES = _REPO_ROOT / "k8s-operator" / "api" / "v1alpha1" / "common_types.go"
_BRIDGE_MAIN = _REPO_ROOT / "a2a" / "cmd" / "hermes-bridge" / "main.go"
_BRIDGE_GO = _REPO_ROOT / "a2a" / "hermes-bridge" / "bridge.go"
_BRIDGE_API_GO = _REPO_ROOT / "a2a" / "hermes-bridge" / "api.go"
_OPERATOR_TEMPLATE = _REPO_ROOT / "charts" / "kube-agents" / "templates" / "operator-deployment.yaml"

_AR_REPO = "us-central1-docker.pkg.dev/kube-agents-evals/kube-agents"
_TAG = "pr-1686-abc1234"
_A2A_SUBSTITUTIONS = (
    "_A2A_GATEWAY_URI",
    "_A2A_CALLOUT_URI",
    "_A2A_WORKER_URI",
    "_A2A_VERIFIER_URI",
    "_A2A_CONSOLE_URI",
    "_A2A_BRIDGE_URI",
)
_A2A_IMAGES = ("a2a-gateway", "a2a-authcallout", "a2a-worker", "a2a-verifier", "a2a-console", "hermes-bridge")
_A2A_DOCKERFILE_SUFFIXES = ("gateway", "authcallout", "worker", "verifier", "console")
_PLATFORM_URI = f"{_AR_REPO}/platform-agent:{_TAG}"
_FLAG_UNSET_SPELLINGS = (None, "", "0", "true", "yes")
# The next lane's two Prow jobs (oss-test-infra), the only runs section 2b
# admits the flag on without a pull request; the nightly and a made-up job
# stand for every other Prow run.
_NEXT_LANE_JOB_NAMES = ("pull-kube-agents-smoke-test-next", "ci-kube-agents-eval-next")
_NIGHTLY_JOB_NAME = "ci-kube-agents-eval-nightly"
_OTHER_JOB_NAME = "ci-x"

_BUILD_SECTION = (r"^# ─── 4\. Build Container Images.*?", r"^# ─── 5\. Chart Deployment")
_MODE_SECTION = (r"^# ─── 6b\. EVAL_MODE_NEXT.*?", r"^# ─── 7\. Agent API Connectivity")
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


# The variables a lifted section reads from the job environment. Unset first,
# so "not exported by the test" means unset rather than whatever the shell
# running the tests happens to export (a developer reproducing a flag run).
_AMBIENT_ENV = ("EVAL_MODE_NEXT", "EVAL_TASK_PARALLELISM", "RC_COMMIT_SHA", "PULL_NUMBER", "JOB_NAME")


def run_bash(script: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", f"set -euo pipefail\nunset {' '.join(_AMBIENT_ENV)}\n{script}"],
        capture_output=True,
        text=True,
        check=False,
    )


def lifted_block(start: str, stop: str, what: str) -> str:
    """The text from the first line containing `start` through the first line
    containing `stop` after it, both included."""
    lines = text(_CI_DEPLOY).splitlines()
    for index, line in enumerate(lines):
        if start in line:
            for end in range(index, len(lines)):
                if stop in lines[end]:
                    return "\n".join(lines[index : end + 1])
            break
    raise AssertionError(f"{what} not found in {_CI_DEPLOY}")


def shell_function(name: str) -> str:
    """A function of the script, from its `name() {` line to the `}` that
    closes it in column 0."""
    lines = text(_CI_DEPLOY).splitlines()
    for index, line in enumerate(lines):
        if line == f"{name}() {{":
            for end in range(index, len(lines)):
                if lines[end] == "}":
                    return "\n".join(lines[index : end + 1])
            break
    raise AssertionError(f"{name}() not found in {_CI_DEPLOY} in the shape this test lifts")


def max_sessions_derivation() -> str:
    """Section 2b's sizing of MODE_NEXT_MAX_SESSIONS: the arithmetic and the
    clamp under it."""
    return lifted_block("MODE_NEXT_MAX_SESSIONS=$((", "  fi", "the maxSessions derivation")


def prow_guard() -> str:
    """Section 2b's refusal: from its `if` to the block's own unindented `fi`
    (lifted_block's substring stop would end it at the first line containing
    the two letters)."""
    start = 'if [ "${EVAL_MODE_NEXT:-}" = "1" ] && [ "${IS_PROW_RUN}" = "true" ] && [ -z "${PULL_NUMBER:-}" ]; then'
    lines = text(_CI_DEPLOY).splitlines()
    for index, line in enumerate(lines):
        if line == start:
            for end in range(index, len(lines)):
                if lines[end] == "fi":
                    return "\n".join(lines[index : end + 1])
            break
    raise AssertionError(f"the section 2b refusal not found in {_CI_DEPLOY}")


def flag_line(mode_next: str | None) -> str:
    return "" if mode_next is None else f'export EVAL_MODE_NEXT="{mode_next}"'


# The concurrency section 2b admitted, which section 4 hands the operator.
_BRIDGE_CONCURRENCY = "6"


def run_build_section(mode_next: str | None) -> subprocess.CompletedProcess:
    """Run section 4 with `gcloud` stubbed to print its argv, then print the
    operator.extraEnv values it left for the release. MODE_NEXT_BRIDGE_CONCURRENCY
    is set as section 2b leaves it under the flag."""
    return run_bash(
        "\n".join(
            [
                f'export AR_REPO="{_AR_REPO}"',
                f'MODE_NEXT_BRIDGE_CONCURRENCY="{_BRIDGE_CONCURRENCY}"',
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


def build_step(step_id: str) -> dict:
    steps = yaml.safe_load(text(_CLOUDBUILD))["steps"]
    for step in steps:
        if step.get("id") == step_id:
            return step
    raise AssertionError(f"cloudbuild-ci.yaml has no step with id {step_id}")


def run_build_step(step_id: str, subs: dict[str, str], docker_stub: str) -> subprocess.CompletedProcess:
    """Run a step's script as Cloud Build would: `$$` collapsed to `$` and
    each `$_NAME` replaced by its substitution, with `docker` stubbed."""
    script = build_step(step_id)["args"][1].replace("$$", "$")
    for name, value in subs.items():
        script = script.replace(f"${name}", value)
    return run_bash(f"{docker_stub}\n{script}")


def next_stack_substitutions() -> dict[str, str]:
    subs = {name: f"{_AR_REPO}/{image}:{_TAG}" for name, image in zip(_A2A_SUBSTITUTIONS, _A2A_IMAGES, strict=True)}
    subs["_PLATFORM_URI"] = _PLATFORM_URI
    return subs


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

    def test_the_next_stack_steps_are_no_ops_with_their_substitutions_empty(self) -> None:
        for step_id in ("a2a", "a2a-bridge"):
            with self.subTest(step=step_id):
                result = run_build_step(
                    step_id,
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
    def test_the_build_names_the_five_next_stack_images(self) -> None:
        result = run_build_section("1")
        self.assertEqual(result.returncode, 0, result.stderr)
        subs = substitutions(result)
        for name, image in zip(_A2A_SUBSTITUTIONS, _A2A_IMAGES, strict=True):
            self.assertIn(f"{name}={_AR_REPO}/{image}:{_TAG}", subs)

    def test_the_release_gets_the_overrides_the_inject_flag_and_the_bridge_settings_as_operator_extra_env(self) -> None:
        result = run_build_section("1")
        self.assertEqual(result.returncode, 0, result.stderr)
        args = helm_args(result)
        env_vars = (
            go_constant(_A2A_MANIFESTS, "a2aGatewayImageEnvVar"),
            go_constant(_A2A_CALLOUT, "a2aCalloutImageEnvVar"),
            go_constant(_A2A_MANIFESTS, "a2aWorkerImageEnvVar"),
            # The verifier sits on the request path: an eval install that does
            # not override it leaves the Deployment on a private dev registry
            # it cannot pull, and every executor refuses every task.
            go_constant(_A2A_VERIFIER, "a2aVerifierImageEnvVar"),
            go_constant(_A2A_CONSOLE, "a2aConsoleImageEnvVar"),
        )
        expected = []
        for index, (env_var, image) in enumerate(zip(env_vars, _A2A_IMAGES[:5], strict=True)):
            expected += [
                "--set-string",
                f"operator.extraEnv[{index}].name={env_var}",
                "--set-string",
                f"operator.extraEnv[{index}].value={_AR_REPO}/{image}:{_TAG}",
            ]
        # The inject door is armed the same way; that the operator opens it only
        # on the exact word "true" is the conformance suite's pin (test_A_authority).
        inject_env_var = go_constant(_A2A_MANIFESTS, "a2aInjectBackendEnvVar")
        expected += [
            "--set-string",
            f"operator.extraEnv[5].name={inject_env_var}",
            "--set-string",
            "operator.extraEnv[5].value=true",
        ]
        # The bridge the operator renders under next (#2592): its image (this
        # build's), the concurrency section 2b admitted, and the executor pin,
        # each under the name the operator reads it by.
        consts = constants()
        expected += [
            "--set-string",
            f"operator.extraEnv[6].name={go_constant(_A2A_BRIDGE, 'a2aBridgeImageEnvVar')}",
            "--set-string",
            f"operator.extraEnv[6].value={_AR_REPO}/hermes-bridge:{_TAG}",
            "--set-string",
            f"operator.extraEnv[7].name={go_constant(_A2A_BRIDGE, 'a2aBridgeConcurrencyOperatorEnvVar')}",
            "--set-string",
            f"operator.extraEnv[7].value={_BRIDGE_CONCURRENCY}",
            "--set-string",
            f"operator.extraEnv[8].name={go_constant(_A2A_BRIDGE, 'a2aBridgeExecutorOperatorEnvVar')}",
            "--set-string",
            f"operator.extraEnv[8].value={consts['BRIDGE_EXECUTOR_PINNED']}",
        ]
        self.assertEqual(args, expected)
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
        callout_suffix = go_constant(_API_TYPES, "a2aCalloutNameSuffix")
        self.assertEqual(consts["A2A_NATS_POD_SELECTOR"], "app=${PLATFORM_AGENT_CR_NAME}" + nats_suffix)
        manifests = text(_A2A_MANIFESTS)
        self.assertRegex(manifests, r'func a2aGatewayName\(.*\) string\s*{\s*return agent\.Name \+ "-a2a-gateway"')
        self.assertRegex(manifests, r'func a2aInjectName\(.*\) string\s*{\s*return agent\.Name \+ "-a2a-inject"')
        self.assertEqual(consts["A2A_INJECT_NAME"], "${PLATFORM_AGENT_CR_NAME}-a2a-inject")
        self.assertEqual(consts["A2A_INJECT_TOKEN_KEY"], go_constant(_A2A_MANIFESTS, "a2aInjectTokenKey"))
        # The rendered bridge: the container the log gate reads, the image name
        # step 4 pushes under, and the three operator settings step 5 sets, each
        # the operator's own spelling and each read from the operator's own
        # environment rather than the CR's.
        self.assertEqual(consts["BRIDGE_SIDECAR_NAME"], go_constant(_A2A_BRIDGE, "a2aBridgeContainerName"))
        self.assertEqual(consts["A2A_BRIDGE_IMAGE_NAME"], go_constant(_A2A_BRIDGE, "a2aBridgeImageName"))
        bridge_go = text(_A2A_BRIDGE)
        for const, go_name in (
            ("A2A_BRIDGE_IMAGE_ENV_VAR", "a2aBridgeImageEnvVar"),
            ("A2A_BRIDGE_CONCURRENCY_ENV_VAR", "a2aBridgeConcurrencyOperatorEnvVar"),
            ("A2A_BRIDGE_EXECUTOR_ENV_VAR", "a2aBridgeExecutorOperatorEnvVar"),
        ):
            with self.subTest(const=const):
                self.assertEqual(consts[const], go_constant(_A2A_BRIDGE, go_name))
                self.assertIn(f"os.Getenv({go_name})", bridge_go)
        block = lifted(*_MODE_SECTION)
        self.assertIn('-a2a-callout"', block)
        self.assertEqual(callout_suffix, "-a2a-callout")
        self.assertIn('-a2a-gateway"', block)
        # In the NATS StatefulSet's builder, not just somewhere in the file.
        nats_builder = manifests[manifests.index("func buildA2ANATSStatefulSet(") :]
        nats_builder = nats_builder[: nats_builder.index("\n}\n")]
        self.assertRegex(nats_builder, r'podLabels := map\[string\]string{"app": name}')
        self.assertIn("name := a2aNATSName(agent)", nats_builder)
        managed_env_key = go_constant(_AGENT_MANIFESTS, "managedEnvKey")
        self.assertIn(f"jsonpath='{{.data.{managed_env_key.replace('.', chr(92) + '.')}}}'", block)

    def test_the_bridge_env_and_log_line_match_the_bridge_source(self) -> None:
        consts = constants()
        main_go = text(_BRIDGE_MAIN)
        self.assertEqual(int(consts["BRIDGE_QUEUE_CAPACITY"]), go_int_constant(_BRIDGE_GO, "taskQueueCapacity"))
        self.assertIn('Info("hermes bridge consuming", "profile", b.cfg.Profile, ', text(_BRIDGE_GO))
        # The shape the deploy greps is the JSON handler's: `"msg":"..."` and
        # `"profile":"..."`. A text handler would print the same words in a
        # shape neither grep matches.
        self.assertIn("slog.New(slog.NewJSONHandler(os.Stderr, nil))", main_go)
        self.assertEqual(consts["BRIDGE_CONSUMING_LOG_MSG"], '"msg":"hermes bridge consuming"')
        self.assertEqual(consts["BRIDGE_CONSUMING_LOG_PROFILE"], f'"profile":"{go_constant(_BRIDGE_MAIN, "defaultProfile")}"')
        # The lane pins the subprocess executor, and the start line is where the
        # deploy proves the pin took: the variable the operator renders the pin
        # into is the one the bridge reads, the value is one it accepts, and the
        # field it logs the choice under is the one the wait greps.
        self.assertEqual(go_constant(_A2A_MANIFESTS, "a2aBridgeExecutorEnvVar"), go_constant(_BRIDGE_MAIN, "executorEnv"))
        self.assertEqual(consts["BRIDGE_EXECUTOR_PINNED"], go_constant(_BRIDGE_API_GO, "ExecutorCLI"))
        self.assertIn('"executor", b.cfg.Executor)', text(_BRIDGE_GO))
        self.assertEqual(consts["BRIDGE_CONSUMING_LOG_EXECUTOR"], f'"executor":"{consts["BRIDGE_EXECUTOR_PINNED"]}"')
        self.assertIn(
            '| grep -F "${BRIDGE_CONSUMING_LOG_MSG}" | grep -F "${BRIDGE_CONSUMING_LOG_PROFILE}" | grep -F "${BRIDGE_CONSUMING_LOG_EXECUTOR}" |',
            text(_CI_DEPLOY),
            "the start-line wait requires all three fields, or a bridge on the wrong executor passes it",
        )

    def test_the_concurrency_default_is_the_eval_scripts_and_fits_the_queue(self) -> None:
        consts = constants()
        eval_default = re.search(r'^EVAL_TASK_PARALLELISM="\$\{EVAL_TASK_PARALLELISM:-(\d+)\}"$', text(_CI_EVAL), re.MULTILINE)
        self.assertIsNotNone(eval_default, "hack/ci-eval-pr.sh no longer defaults EVAL_TASK_PARALLELISM where this test reads it")
        self.assertEqual(int(consts["EVAL_TASK_PARALLELISM_DEFAULT"]), int(eval_default.group(1)))
        self.assertLessEqual(int(consts["EVAL_TASK_PARALLELISM_DEFAULT"]), int(consts["BRIDGE_QUEUE_CAPACITY"]))
        script = text(_CI_DEPLOY)
        self.assertIn('MODE_NEXT_BRIDGE_CONCURRENCY="${EVAL_TASK_PARALLELISM:-${EVAL_TASK_PARALLELISM_DEFAULT}}"', script)
        # And the value the guard admitted is what the operator renders into
        # the bridge (and budgets TASKS by).
        self.assertIn('--set-string "operator.extraEnv[7].value=${MODE_NEXT_BRIDGE_CONCURRENCY}"', lifted(*_BUILD_SECTION))
        self.assertEqual(go_constant(_A2A_MANIFESTS, "a2aBridgeConcurrencyEnvVar"), "BRIDGE_CONCURRENCY")
        self.assertIn("{Name: a2aBridgeConcurrencyEnvVar, Value: a2aRenderedBridgeConcurrency()}", text(_A2A_BRIDGE))

    def test_the_release_candidate_path_refuses_the_flag(self) -> None:
        script = text(_CI_DEPLOY)
        rc_branch = script.index('if [ -n "${RC_COMMIT_SHA:-}" ]; then')
        refusal = script.index('if [ "${EVAL_MODE_NEXT:-}" = "1" ]; then', rc_branch)
        self.assertLess(refusal - rc_branch, 600, "the refusal belongs at the top of the RC branch")
        # Run, not only read: the inner guard, lifted, refuses under the flag
        # and lets an RC run through without it.
        guard = script[refusal:]
        guard = guard[: guard.index("\n  fi\n") + len("\n  fi\n")]
        self.assertEqual(run_bash(f"export EVAL_MODE_NEXT=1\n{guard}").returncode, 1)
        self.assertEqual(run_bash(guard).returncode, 0)

    def test_a_prow_run_without_a_pull_request_refuses_the_flag_unless_the_job_is_the_lanes(self) -> None:
        """Section 2b admits the flag on a pull request's run and on the jobs
        EVAL_MODE_NEXT_JOB_NAMES lists (the next lane's periodic has no
        PULL_NUMBER); every other Prow run under it is still refused, with the
        error naming the job, and the flag unset changes nothing."""
        guard = prow_guard()
        lane_jobs = constants()["EVAL_MODE_NEXT_JOB_NAMES"].split()
        self.assertEqual(lane_jobs, list(_NEXT_LANE_JOB_NAMES), "the allow-list is the next lane's two jobs")
        cases = {
            # (flag, IS_PROW_RUN, PULL_NUMBER, JOB_NAME) -> refused
            ("1", "true", "", _OTHER_JOB_NAME): True,
            ("1", "true", "", _NIGHTLY_JOB_NAME): True,
            ("1", "true", "1686", _OTHER_JOB_NAME): False,
            ("1", "true", "1686", _NEXT_LANE_JOB_NAMES[0]): False,
            ("1", "false", "", ""): False,
            ("", "true", "", _OTHER_JOB_NAME): False,
            ("", "true", "", _NEXT_LANE_JOB_NAMES[1]): False,
            **{("1", "true", "", job): False for job in lane_jobs},
        }
        for (flag, prow, pull, job), refused in cases.items():
            with self.subTest(flag=flag, prow=prow, pull=pull, job=job):
                env = f'export EVAL_MODE_NEXT="{flag}" IS_PROW_RUN="{prow}" PULL_NUMBER="{pull}" JOB_NAME="{job}"\n'
                result = run_bash(env + constants_block() + "\n" + guard)
                self.assertEqual(result.returncode, 1 if refused else 0, result.stderr)
                if refused:
                    self.assertIn(f"no PULL_NUMBER (JOB_NAME={job})", result.stderr)
                    # The consequence the error names is the one the tree has:
                    # a flagged run records nothing, so the refused job would
                    # go missing from main's record, not move it.
                    self.assertIn("record nothing to main's", result.stderr)
                    self.assertNotIn("would record next-mode samples", result.stderr)
                    self.assertEqual(result.stdout, "")
                elif flag == "1" and prow == "true" and not pull:
                    self.assertIn(f"EVAL_MODE_NEXT=1: accepted on {job}", result.stdout)
                else:
                    self.assertEqual(result.stdout, "", "the flag unset, a pull request's run, or a laptop logs nothing here")

    def test_the_lane_allow_list_matches_whole_job_names(self) -> None:
        """A prefix, a suffix or a substring of a listed name is not the job:
        `ci-kube-agents-eval-next-2` or `kube-agents-eval-next` must refuse.
        So must a value spelling two adjacent entries with the space between
        them, which a pattern over the joined list would have admitted."""
        guard = prow_guard()
        listed = _NEXT_LANE_JOB_NAMES[1]
        spanning = " ".join(_NEXT_LANE_JOB_NAMES)
        for job in (listed + "-2", listed[3:], listed[:-1], listed.upper(), f"{listed} ", spanning, f" {spanning} "):
            with self.subTest(job=job):
                env = f'export EVAL_MODE_NEXT="1" IS_PROW_RUN="true" PULL_NUMBER="" JOB_NAME="{job}"\n'
                self.assertEqual(run_bash(env + constants_block() + "\n" + guard).returncode, 1, job)

    def test_the_concurrency_guard_speaks_the_bridges_grammar(self) -> None:
        """What passes here is written into BRIDGE_CONCURRENCY verbatim and
        parsed by strconv.Atoi, which takes digits and nothing else; the guard
        has to refuse whatever Atoi would, or the bridge falls back to 2. It
        sits in section 2b, before anything is built, like the other refusals."""
        script = text(_CI_DEPLOY)
        guard_at = script.index('MODE_NEXT_BRIDGE_CONCURRENCY="${EVAL_TASK_PARALLELISM:-${EVAL_TASK_PARALLELISM_DEFAULT}}"')
        self.assertLess(guard_at, script.index("# ─── 4. Build Container Images"), "the guard belongs before the build")
        self.assertGreater(guard_at, script.index('[ -z "${PULL_NUMBER:-}" ]; then'), "the guard belongs beside the 2b refusal")
        guard = lifted_block(
            'MODE_NEXT_BRIDGE_CONCURRENCY="${EVAL_TASK_PARALLELISM:-${EVAL_TASK_PARALLELISM_DEFAULT}}"',
            "  fi",
            "the concurrency guard",
        )
        # Unset and empty both mean the default, as `${VAR:-default}` reads them
        # in hack/ci-eval-pr.sh too; everything else is the string, verbatim.
        admitted = {"1": "1", "4": "4", "6": "6", "1024": "1024", None: "4", "": "4"}
        # The last two are the width boundary: `test` cannot parse a digit
        # string past int64 and would let it through as neither -lt nor -gt.
        refused = (" 4", "4 ", "0", "-1", "1025", "four", "4.0", "+4", "10240", "99999999999999999999")
        for value, expected in admitted.items():
            with self.subTest(EVAL_TASK_PARALLELISM=value):
                env = "" if value is None else f'export EVAL_TASK_PARALLELISM="{value}"\n'
                result = run_bash(f'{env}{constants_block()}\n{guard}\necho "OUT=${{MODE_NEXT_BRIDGE_CONCURRENCY}}"')
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(f"OUT={expected}", result.stdout)
        for value in refused:
            with self.subTest(EVAL_TASK_PARALLELISM=value):
                result = run_bash(f'export EVAL_TASK_PARALLELISM="{value}"\n{constants_block()}\n{guard}')
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertIn("is not a concurrency the bridge can be given", result.stderr)

    def test_the_gates_run_in_dependency_order_and_skip_the_two_that_cannot(self) -> None:
        block = lifted(*_MODE_SECTION)
        gated = re.findall(r'gate_mode_next_rollout "(\S+)"', block)
        self.assertEqual(
            gated,
            [
                "statefulset/${PLATFORM_AGENT_CR_NAME}-a2a-nats",
                "deployment/${PLATFORM_AGENT_CR_NAME}-a2a-callout",
                "deployment/${AGENT_DEPLOYMENT_NAME}",
                # Twice: the operator adds the bridge to the agent pod only
                # once the bus is provisioned, so the agent rolls a second
                # time after the Job, and that roll is gated like the first.
                "deployment/${AGENT_DEPLOYMENT_NAME}",
                # Last, not after the Job its bucket comes from: see the
                # step header. A verifier still in its crash-loop backoff
                # when the eval starts submitting refuses every case
                # terminally, so this one is a gate and not a report, but
                # the backoff is spent alongside the rollouts above.
                "deployment/${PLATFORM_AGENT_CR_NAME}-a2a-verifier",
            ],
        )
        markers = [
            'gate_mode_next_rollout "deployment/${PLATFORM_AGENT_CR_NAME}-a2a-callout"',
            'wait_provision_job "the mode patch"',
            'gate_mode_next_rollout "deployment/${AGENT_DEPLOYMENT_NAME}"',
            '*" ${BRIDGE_SIDECAR_NAME} "*) break ;;',
            'gate_mode_next_rollout "deployment/${AGENT_DEPLOYMENT_NAME}"',
            'kubectl get "service/${A2A_INJECT_NAME}" "secret/${A2A_INJECT_NAME}"',
            'grep -F "${BRIDGE_CONSUMING_LOG_MSG}" | grep -F "${BRIDGE_CONSUMING_LOG_PROFILE}"',
            'gate_mode_next_rollout "deployment/${PLATFORM_AGENT_CR_NAME}-a2a-verifier"',
        ]
        position = 0
        for marker in markers:
            with self.subTest(marker=marker):
                found = block.find(marker, position)
                self.assertGreater(found, -1, f"{marker!r} is missing after offset {position}")
                position = found + len(marker)
        # One patch, one provision run: the bridge is the operator's from the
        # first render (#2592), so nothing declares it and nothing waits for a
        # re-run, so there is no refusal for the Degraded gate to read.
        self.assertEqual(block.count("kubectl patch platformagent"), 1)
        self.assertEqual(block.count("wait_provision_job \""), 1)
        self.assertNotIn('gate_cr_not_degraded "', block)
        self.assertNotIn("render_mode_next_sidecar_patch", text(_CI_DEPLOY))
        self.assertNotIn("SIDECAR_PATCH", text(_CI_DEPLOY))
        for never_gated in ("a2a-gateway", "platform-agent-shell"):
            with self.subTest(never_gated=never_gated):
                for line in block.splitlines():
                    if "rollout status" in line or "gate_mode_next_rollout" in line:
                        self.assertNotIn(never_gated, line)

    def test_the_provisioning_job_gate_stops_on_either_terminal_condition(self) -> None:
        """The gate polls the Jobs' True conditions and stops on Complete or
        Failed; a Failed Job must not sit out the budget, and no condition at
        all must. The function is lifted whole and run against a kubectl
        stub, on a budget of a few seconds."""
        cases = {
            # what the jobs jsonpath prints -> (exit status, message fragment)
            "j1:SuccessCriteriaMet,Complete, ": (0, None),
            "j1:Failed, ": (1, "conditions: Failed"),
            "": (1, "conditions: none"),
            "j1:Suspended, ": (1, "conditions: Suspended"),
        }
        for stub, (status, fragment) in cases.items():
            with self.subTest(conditions=stub):
                result = run_provision_wait(jobs=stub, call='wait_provision_job "the mode patch"')
                self.assertEqual(result.returncode, status, result.stdout + result.stderr)
                if fragment is None:
                    self.assertIn("PASSED NAME=j1 CONDITIONS=Complete", result.stdout)
                else:
                    self.assertIn(fragment, result.stdout)
                    self.assertIn("DUMPED", result.stdout)
                    self.assertNotIn("PASSED", result.stdout)
        # How long each takes is the next test's.

    def test_a_failed_job_is_reported_within_one_poll_and_an_absent_one_at_the_deadline(self) -> None:
        # The stub answers the Ready condition read at once, so the Failed
        # branch's wait for the operator's status costs nothing here; the
        # dump runs last before the exit and carries the clock.
        for stub, bound in (("j1:Failed, ", 2), ("", None)):
            with self.subTest(conditions=stub):
                result = run_provision_wait(jobs=stub, call='wait_provision_job "the mode patch"', ready="A2AProvisionFailed: refused")
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                elapsed = int(re.search(r"DUMPED ELAPSED=(\d+)", result.stdout).group(1))
                if bound is None:
                    self.assertGreaterEqual(elapsed, 3, "an absent Job runs the budget out")
                else:
                    self.assertLess(elapsed, bound, "a Failed Job is reported within one poll")

    def test_the_generation_is_read_before_the_patch(self) -> None:
        block = lifted(*_MODE_SECTION)
        self.assertLess(block.index("GEN_BEFORE="), block.index("kubectl patch platformagent"))
        self.assertLess(block.index("kubectl patch platformagent"), block.index('wait_agent_generation_past "${GEN_BEFORE}" "the mode patch"'))


class TasksBudgetSizingTest(unittest.TestCase):
    """(b): the first patch carries a maxSessions sized so the one
    provision's budget, which counts the rendered bridge's workers, fits the
    floor."""

    def test_the_budget_terms_are_the_operators(self) -> None:
        """The four numbers are copied from the operator, which keeps them as
        Go constants and a function over them; each is read back from the
        source it came from. The Go side pins the same four against the
        functions (TestCiDeploySizesMaxSessionsToTheTasksFloor)."""
        consts = constants()
        manifests = text(_A2A_MANIFESTS)
        self.assertEqual(int(consts["A2A_TASKS_FLOOR"]), go_int_constant(_A2A_MANIFESTS, "a2aTasksMaxConsumersFloor"))
        per_session = go_int_constant(_A2A_MANIFESTS, "a2aSessionConsumersPerSession")
        self.assertEqual(int(consts["A2A_SESSION_CONSUMERS"]), per_session)
        # The reserve table at zero workers, and its slope. The overlap row
        # is an alias of the per-session count, not a literal.
        self.assertRegex(manifests, re.compile(r"^\s*a2aTasksIncarnationOverlap\s*=\s*a2aSessionConsumersPerSession$", re.MULTILINE))
        tail = go_int_constant(_A2A_MANIFESTS, "a2aTasksReplayTailFactor")
        fixed = (
            go_int_constant(_A2A_MANIFESTS, "a2aTasksStandingDurables")
            + go_int_constant(_A2A_MANIFESTS, "a2aTasksAuditDurableHeadroom")
            + per_session
            + go_int_constant(_A2A_MANIFESTS, "a2aTasksWebReaders")
            + tail * (go_int_constant(_A2A_MANIFESTS, "a2aTasksReplayBridgeDispatch") + go_int_constant(_A2A_MANIFESTS, "a2aTasksReplayGatewaySweep"))
        )
        per_worker = tail * (go_int_constant(_A2A_MANIFESTS, "a2aTasksReplayAsk") + go_int_constant(_A2A_MANIFESTS, "a2aTasksReplayLookAhead"))
        self.assertEqual(int(consts["A2A_RESERVE_FIXED"]), fixed)
        self.assertEqual(int(consts["A2A_RESERVE_PER_WORKER"]), per_worker)
        # And the function the operator evaluates is that table, in the
        # order the script's comment states it.
        self.assertRegex(
            manifests,
            r"func a2aTasksReservedConsumersFor\(bridgeConcurrency int\) int \{\s*"
            r"return a2aTasksStandingDurables \+ a2aTasksAuditDurableHeadroom \+ a2aTasksIncarnationOverlap \+\s*"
            r"a2aTasksWebReaders \+ a2aTasksReplayConsumersFor\(bridgeConcurrency\)",
        )

    def test_max_sessions_is_the_largest_that_fits_the_floor_and_at_least_one(self) -> None:
        """Section 2b's arithmetic, run for the lane's worker counts. The
        tech lead's numbers: 6 at the presubmit's 4 workers, 2 at 6; at 8 the
        floor cannot hold the reserve and the clamp gives 1, and the one
        provision creates TASKS at the budget instead (#2592: no later render
        re-measures it)."""
        consts = constants()
        floor, per_session = int(consts["A2A_TASKS_FLOOR"]), int(consts["A2A_SESSION_CONSUMERS"])
        fixed, per_worker = int(consts["A2A_RESERVE_FIXED"]), int(consts["A2A_RESERVE_PER_WORKER"])
        expected = {1: 12, 2: 10, 4: 6, 6: 2, 7: 1, 8: 1, int(consts["BRIDGE_QUEUE_CAPACITY"]): 1}
        for workers, want in expected.items():
            with self.subTest(workers=workers):
                result = run_bash(
                    "\n".join(
                        [
                            constants_block(),
                            f"MODE_NEXT_BRIDGE_CONCURRENCY={workers}",
                            max_sessions_derivation(),
                            'echo "N=${MODE_NEXT_MAX_SESSIONS}"',
                        ]
                    )
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                got = int(re.search(r"N=(\d+)", result.stdout).group(1))
                self.assertEqual(got, want)
                budget = got * per_session + fixed + per_worker * workers
                if budget <= floor:
                    self.assertGreater(budget + per_session, floor, "not the largest maxSessions that fits")
                else:
                    self.assertEqual(got, 1, "past the floor only the clamp is left")
        # The default lane fits; the reason the sizing exists.
        default = int(consts["EVAL_TASK_PARALLELISM_DEFAULT"])
        self.assertLessEqual(expected[default] * per_session + fixed + per_worker * default, floor)

    def test_the_first_patch_carries_the_mode_and_the_sized_max_sessions_before_the_first_job_wait(self) -> None:
        consts = constants()
        patch = json.loads(consts["MODE_NEXT_PATCH_FORMAT"] % 6)
        self.assertEqual(patch, {"spec": {"mode": "next", "harness": {"tuning": {"maxSessions": 6}}}})
        # The path is the CRD's: PlatformAgentSpec.Harness -> HarnessSpec.Tuning
        # -> TuningSpec.MaxSessions, as the API package tags them.
        api = text(_API_TYPES)
        self.assertIn('Tuning *TuningSpec `json:"tuning,omitempty"`', api)
        self.assertIn('MaxSessions *int `json:"maxSessions,omitempty"`', api)
        self.assertIn('Harness *HarnessSpec `json:"harness,omitempty"`', text(_API_TYPES.parent / "platformagent_types.go"))
        # Sized in section 2b, from the concurrency the guard there admitted,
        # before anything is built.
        script = text(_CI_DEPLOY)
        guard = script.index('MODE_NEXT_BRIDGE_CONCURRENCY="${EVAL_TASK_PARALLELISM:-${EVAL_TASK_PARALLELISM_DEFAULT}}"')
        derivation = script.index("MODE_NEXT_MAX_SESSIONS=$((")
        self.assertLess(guard, derivation)
        self.assertLess(derivation, script.index("# ─── 4. Build Container Images"))
        self.assertIn("A2A_RESERVE_PER_WORKER * MODE_NEXT_BRIDGE_CONCURRENCY", script[derivation : derivation + 200])
        # And in step 6b the log line, the patch's rendering, the patch and
        # the first Job wait, in that order.
        block = lifted(*_MODE_SECTION)
        markers = [
            'echo "setting spec.harness.tuning.maxSessions=${MODE_NEXT_MAX_SESSIONS} so the ${A2A_TASKS_FLOOR}-wide TASKS holds the ${MODE_NEXT_BRIDGE_CONCURRENCY}-worker bridge\'s budget',
            'printf -v MODE_NEXT_PATCH "${MODE_NEXT_PATCH_FORMAT}" "${MODE_NEXT_MAX_SESSIONS}"',
            'kubectl patch platformagent "${PLATFORM_AGENT_CR_NAME}" -n "${NAMESPACE}" --type merge -p "${MODE_NEXT_PATCH}"',
            'wait_provision_job "the mode patch"',
        ]
        position = 0
        for marker in markers:
            with self.subTest(marker=marker):
                found = block.find(marker, position)
                self.assertGreater(found, -1, f"{marker!r} is missing after offset {position}")
                position = found + len(marker)
        # No other patch precedes it: the first render sees the cap.
        self.assertEqual(block.index("kubectl patch platformagent"), block.index(markers[2]))


# A `phase` entry for run_provision_wait that stands for a dropped read.
DROPPED_READ = "<dropped>"


def run_provision_wait(
    *,
    jobs: str,
    call: str,
    observed: str = "",
    ready: str | list[str] = "",
    phase: str | list[str] = "",
    status_attempts: int = 1,
    unschedulable_attempts: int = 2,
    pods: str | list[str] = "",
) -> subprocess.CompletedProcess:
    """Run wait_provision_job or gate_cr_not_degraded as step 6b calls them,
    against a kubectl stub that answers each jsonpath read with what the test
    set: the Jobs listing (name, colon, True condition types each followed by
    a comma, then a space, per Job), the CR's observedGeneration, its Ready
    condition as `reason: message`, and its phase. On a budget of three
    seconds and a one-second poll; the Degraded gate's re-reads poll at zero
    seconds, on a window of `unschedulable_attempts`.

    `phase` and `ready` may each be a list, one entry per version of the
    CR's status: every read of the CR's status (phase, Ready condition, or
    both in one jsonpath) is answered from the next version, as if the
    operator wrote a new status between any two reads, and reads past the
    end repeat the last entry. So a read of both answers the Nth phase and
    the Nth condition together, and two separate reads straddle versions,
    the way a status update landing between two GETs would. A version whose
    phase is DROPPED_READ answers every read of it with nothing and a failed
    kubectl, as a GET the API drops does. The output counts the status reads
    (READS=) on both the pass and the fail line, so a test can tell one read
    from a wait.

    `pods` is the agent's pods as the gate's one pod read lists them (a line
    per pod: name, a tab, the node it is bound to), and may be a list too,
    one entry per pod read, the last repeating. An entry of DROPPED_READ is
    a pod read the API drops. The stub answers only a read by the agent
    Deployment's own label; any other selector reads back as dropped. The
    pod reads are counted too, on a line of their own after the pass or fail
    line (POD_READS=)."""
    consts = constants()
    phases = phase if isinstance(phase, list) else [phase]
    readies = ready if isinstance(ready, list) else [ready]
    pod_lists = pods if isinstance(pods, list) else [pods]
    stub = (
        'kubectl() { local v; case "$*" in '
        '*"get pods"*) v="$(pod_version)"; [[ "$*" == *"-l app=platform-agent-gateway "* ]] || return 1; '
        f'[ "$(stub_nth "${{v}}" "${{POD_STUB[@]}}")" = {shlex.quote(DROPPED_READ)} ] && return 1; '
        'stub_nth "${v}" "${POD_STUB[@]}" ;; '
        '*"get jobs"*) printf "%s" "${JOBS_STUB}" ;; '
        '*observedGeneration*) printf "%s" "${OBSERVED_STUB}" ;; '
        "*status.phase*'type==\"Ready\"'*) v=\"$(stub_version)\"; stub_dropped \"${v}\" && return 1; "
        'printf "%s\t%s" "$(stub_nth "${v}" "${PHASE_STUB[@]}")" "$(stub_nth "${v}" "${READY_STUB[@]}")" ;; '
        "*'type==\"Ready\"'*) v=\"$(stub_version)\"; stub_dropped \"${v}\" && return 1; stub_nth \"${v}\" \"${READY_STUB[@]}\" ;; "
        '*status.phase*) v="$(stub_version)"; stub_dropped "${v}" && return 1; stub_nth "${v}" "${PHASE_STUB[@]}" ;; '
        '*) echo "kubectl $*" ;; esac; }'
    )
    # The status version this read sees (from zero), counting the read.
    stub_version = 'stub_version() { local n; n="$(<"${READS_FILE}")"; echo $((n + 1)) >"${READS_FILE}"; echo "${n}"; }'
    # The same for the pod reads, on a count of their own.
    pod_version = 'pod_version() { local n; n="$(<"${POD_READS_FILE}")"; echo $((n + 1)) >"${POD_READS_FILE}"; echo "${n}"; }'
    # The Nth (from zero) of the entries after N, or the last past the end.
    stub_nth = (
        'stub_nth() { local n="$1"; shift; '
        'if [ "${n}" -ge "$#" ]; then n=$(($# - 1)); fi; '
        'shift "${n}"; printf "%s" "$1"; }'
    )
    # A version whose phase is DROPPED_READ is a read the API drops: kubectl
    # prints nothing and fails, as a GET that errors does.
    stub_dropped = f'stub_dropped() {{ [ "$(stub_nth "$1" "${{PHASE_STUB[@]}}")" = {shlex.quote(DROPPED_READ)} ]; }}'
    return run_bash(
        "\n".join(
            [
                f'export JOBS_STUB="{jobs}" OBSERVED_STUB="{observed}"',
                f"PHASE_STUB=({' '.join(shlex.quote(p) for p in phases)})",
                f"READY_STUB=({' '.join(shlex.quote(r) for r in readies)})",
                f"POD_STUB=({' '.join(shlex.quote(p) for p in pod_lists)})",
                'READS_FILE="$(mktemp)"',
                'POD_READS_FILE="$(mktemp)"',
                'trap \'rm -f "${READS_FILE}" "${POD_READS_FILE}"\' EXIT',
                'echo 0 >"${READS_FILE}"',
                'echo 0 >"${POD_READS_FILE}"',
                'NAMESPACE="kubeagents-system"',
                'PLATFORM_AGENT_CR_NAME="platform-agent"',
                f'AGENT_DEPLOYMENT_NAME="{consts["AGENT_DEPLOYMENT_NAME"]}"',
                f'A2A_PROVISION_JOB_SELECTOR="{consts["A2A_PROVISION_JOB_SELECTOR"]}"',
                'A2A_NATS_POD_SELECTOR="app=platform-agent-a2a-nats"',
                "MODE_NEXT_DIAG_LOG_LINES=5",
                "MODE_NEXT_POLL_SECONDS=1",
                "MODE_NEXT_PROVISION_JOB_TIMEOUT_SECONDS=3",
                f"MODE_NEXT_STATUS_ATTEMPTS={status_attempts}",
                f"MODE_NEXT_UNSCHEDULABLE_ATTEMPTS={unschedulable_attempts}",
                f'JOB_CONDITION_COMPLETE="{consts["JOB_CONDITION_COMPLETE"]}"',
                f'JOB_CONDITION_FAILED="{consts["JOB_CONDITION_FAILED"]}"',
                f'CR_PHASE_DEGRADED="{consts["CR_PHASE_DEGRADED"]}"',
                f'CR_READY_REASON_PROVISION_FAILED="{consts["CR_READY_REASON_PROVISION_FAILED"]}"',
                f'CR_READY_REASON_POD_UNSCHEDULABLE="{consts["CR_READY_REASON_POD_UNSCHEDULABLE"]}"',
                f"SCHEDULER_CAPACITY_SHORTFALL_RE={shlex.quote(consts['SCHEDULER_CAPACITY_SHORTFALL_RE'])}",
                "MODE_NEXT_START=0",
                stub_version,
                pod_version,
                stub_nth,
                stub_dropped,
                stub,
                # The gate's re-reads sleep MODE_NEXT_POLL_SECONDS; zero here,
                # so a five-read wait costs nothing. The Job wait polls by the
                # clock and keeps its one-second poll.
                'gate_cr_not_degraded_fast() { MODE_NEXT_POLL_SECONDS=0 gate_cr_not_degraded "$@"; }',
                'dump_mode_next_state() { echo "DUMPED ELAPSED=${SECONDS} READS=$(<"${READS_FILE}")"; echo "POD_READS=$(<"${POD_READS_FILE}")"; }',
                shell_function("cr_phase_and_ready_condition"),
                shell_function("cr_ready_condition"),
                shell_function("wait_provision_job"),
                shell_function("gate_cr_not_degraded"),
                "SECONDS=0",
                call,
                'echo "PASSED NAME=${PROVISION_JOB_NAME:-} CONDITIONS=${PROVISION_JOB_CONDITIONS:-} RERENDERED=${PROVISION_JOB_RERENDERED:-} ELAPSED=${SECONDS} READS=$(<"${READS_FILE}")"',
                'echo "POD_READS=$(<"${POD_READS_FILE}")"',
            ]
        )
    )


class ProvisionRerunGateTest(unittest.TestCase):
    """(c), as a function: the provision wait's re-render form counts only a
    Job that is not the superseded run's, and reds on a refusal instead of
    proceeding over it. Step 6b called it after the sidecar patch until the
    operator rendered the bridge (#2592) and calls neither it nor the
    Degraded gate now; the tests hold the functions that still ship."""

    _RERUN = 'wait_provision_job "a re-render" j1 3'

    def test_the_rerun_wait_counts_only_the_new_job(self) -> None:
        # The first run, j1, stays listed until the operator's pass sweeps it,
        # and that pass rolls the agent Deployment before it touches the Job,
        # so the read after the generation wait can still show j1 alone.
        with self.subTest("the new Job completes beside the old one"):
            result = run_provision_wait(jobs="j1:Complete, j2:SuccessCriteriaMet,Complete, ", call=self._RERUN)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("PASSED NAME=j2 CONDITIONS=Complete RERENDERED=true", result.stdout)
            self.assertIn("✓ A2A provisioning Job j2 complete", result.stdout)
        with self.subTest("the old Job's Complete does not count for the new one"):
            result = run_provision_wait(jobs="j1:Complete, j2: ", call=self._RERUN, observed="3")
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertIn("(Job: j2; conditions: none)", result.stdout)
            self.assertGreaterEqual(int(re.search(r"DUMPED ELAPSED=(\d+)", result.stdout).group(1)), 3, "a running re-run is waited for")
        with self.subTest("the old Job swept and the new one not yet listed is waited for"):
            result = run_provision_wait(jobs="", call=self._RERUN, observed="3")
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertIn("(Job: none; conditions: none)", result.stdout)
            self.assertGreaterEqual(int(re.search(r"DUMPED ELAPSED=(\d+)", result.stdout).group(1)), 3)

    def test_an_unchanged_render_is_told_from_a_job_not_yet_created_by_the_observed_generation(self) -> None:
        with self.subTest("the operator kept the old Job after observing the patch"):
            result = run_provision_wait(jobs="j1:Complete, ", call=self._RERUN, observed="3")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("PASSED NAME=j1 CONDITIONS=Complete RERENDERED=false", result.stdout)
            self.assertIn("left the A2A provisioning Job's render unchanged (j1 is still the current run)", result.stdout)
        with self.subTest("the operator has not observed the patch yet"):
            result = run_provision_wait(jobs="j1:Complete, ", call=self._RERUN, observed="2")
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertNotIn("PASSED", result.stdout)
            self.assertGreaterEqual(int(re.search(r"DUMPED ELAPSED=(\d+)", result.stdout).group(1)), 3)
        with self.subTest("no observedGeneration on the CR is not observed"):
            result = run_provision_wait(jobs="j1:Complete, ", call=self._RERUN, observed="")
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)

    def test_a_failed_rerun_reds_the_step_with_the_operators_refusal(self) -> None:
        refusal = "A2AProvisionFailed: TASKS holds 64 consumers and this configuration needs 71"
        result = run_provision_wait(jobs="j2:Failed, ", call=self._RERUN, observed="3", ready=refusal, status_attempts=3)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("(Job: j2; conditions: Failed)", result.stdout)
        self.assertIn(f"platform-agent Ready condition: {refusal}", result.stdout)
        self.assertIn("--- provisioning Job pod logs ---", result.stdout)
        self.assertIn("DUMPED", result.stdout)
        self.assertNotIn("PASSED", result.stdout)
        # The status can lag the Job by one operator requeue; the wait for it
        # is bounded and the failure is reported without it.
        result = run_provision_wait(jobs="j2:Failed, ", call=self._RERUN, observed="3", ready="Ready: fine", status_attempts=2)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("platform-agent Ready condition: Ready: fine", result.stdout)
        self.assertLess(int(re.search(r"DUMPED ELAPSED=(\d+)", result.stdout).group(1)), 4)

    def test_a_dropped_status_read_after_a_failed_rerun_is_one_more_poll(self) -> None:
        """The status wait after a Failed re-run shares the gate's read. A read
        the API drops there matches no refusal, so it is one more poll, and
        the step fails on the Job either way; it never passes."""
        refusal = "A2AProvisionFailed: TASKS holds 64 consumers and this configuration needs 71"
        result = run_provision_wait(
            jobs="j2:Failed, ", call=self._RERUN, observed="3", phase=[DROPPED_READ, "Degraded"], ready=["", refusal], status_attempts=3
        )
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(f"platform-agent Ready condition: {refusal}", result.stdout)
        self.assertRegex(result.stdout, r"DUMPED ELAPSED=\d+ READS=2\n")
        self.assertNotIn("PASSED", result.stdout)

    def test_the_degraded_gate_reds_on_a_refusal_the_job_did_not_show(self) -> None:
        gate = 'gate_cr_not_degraded "a re-render"'
        cases = {
            # (phase, Ready condition) -> exit status
            ("Ready", "Ready: all workloads ready"): 0,
            ("Degraded", "A2AProvisionFailed: TASKS holds 64 consumers"): 1,
            ("Ready", "A2AProvisionFailed: TASKS holds 64 consumers"): 1,
            ("Degraded", "InvalidGitRepoURL: not this PR's"): 1,
        }
        for (phase, ready), status in cases.items():
            with self.subTest(phase=phase, ready=ready):
                result = run_provision_wait(jobs="", call=gate, phase=phase, ready=ready)
                self.assertEqual(result.returncode, status, result.stdout + result.stderr)
                if status == 0:
                    self.assertIn("PASSED", result.stdout)
                    self.assertIn(f"✓ platform-agent is {phase} after a re-render", result.stdout)
                else:
                    self.assertIn(f"ERROR: platform-agent is {phase} after a re-render; Ready condition: {ready}", result.stdout)
                    self.assertIn("DUMPED", result.stdout)
                    self.assertNotIn("PASSED", result.stdout)
        # A status read back empty is no answer: the CR carries a status once
        # its provisioning Job has run. Re-read to the end of the window,
        # then red as unread, never a pass as an unphased CR.
        result = run_provision_wait(jobs="", call=gate, phase="", ready="")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("ERROR: could not read platform-agent after a re-render: all 3 reads in ", result.stdout)
        self.assertNotIn("unphased", result.stdout)
        self.assertNotIn("PASSED", result.stdout)

    def test_the_status_words_are_the_operators(self) -> None:
        consts = constants()
        controller = text(_CONTROLLER / "platformagent_controller.go")
        self.assertIn(f'agent.Status.Phase = "{consts["CR_PHASE_DEGRADED"]}"', controller)
        self.assertIn(f'r.updateStatusDegraded(ctx, instance, "{consts["CR_READY_REASON_PROVISION_FAILED"]}", a2aState.message', controller)
        self.assertRegex(controller, r'Type:\s*"Ready",\s*Status:\s*metav1\.ConditionFalse,\s*Reason:\s*reason,')
        self.assertIn('ObservedGeneration int64 `json:"observedGeneration,omitempty"`', text(_API_TYPES))
        # The ordering the wait relies on: the pass rolls the agent
        # Deployment before it creates or sweeps the provision Job, and
        # nothing watches Jobs, so the Failed status arrives on a requeue.
        self.assertLess(controller.index("r.reconcileWorkload(ctx, instance,"), controller.index("r.reconcileA2A(ctx, instance)"))
        self.assertNotIn("Owns(&batchv1.Job{})", controller)


# The Ready conditions gate_cr_not_degraded reads, in the shape the
# operator writes them ("<reason>: <message>", the message wrapping the
# scheduler's in getDeploymentStatusDetails). The first is #2414's, the
# scheduler's elided middle filled in with its usual wording.
_WAITING_FOR_CAPACITY = (
    "PodUnschedulable: Pod platform-agent-gateway-57ddfbc5bf-25f5x cannot be scheduled onto any available node: "
    "0/2 nodes are available: 2 Insufficient cpu, 2 Insufficient memory. preemption: 0/2 nodes are available: "
    "2 No preemption victims found for incoming pod."
)
_CAPACITY_BESIDE_AFFINITY = (
    "PodUnschedulable: Pod platform-agent-gateway-6798bd77c7-wqdjd cannot be scheduled onto any available node: "
    "0/3 nodes are available: 1 node(s) didn't match PersistentVolume's node affinity, 2 Insufficient cpu, "
    "2 Insufficient memory. preemption: 0/3 nodes are available: 1 Preemption is not helpful for scheduling, "
    "2 No preemption victims found for incoming pod."
)
_MEMORY_ONLY = (
    "PodUnschedulable: Pod platform-agent-gateway-6d55f76f5f-qvdwq cannot be scheduled onto any available node: "
    "0/1 nodes are available: 1 Insufficient memory."
)
_ALL_READY = "Ready: all workloads ready"


class TransientUnschedulableGateTest(unittest.TestCase):
    """gate_cr_not_degraded after a re-render (#2414; step 6b called it
    after the sidecar patch until #2592). The re-render rolls the agent pod,
    and on Autopilot that pod can be Unschedulable for want of
    CPU or memory while a node is added; the operator calls that Degraded.
    The gate re-reads that one Degraded for a bounded window and nothing
    else: a refused provision and every other Degraded fail on the first
    read that answers, as they did before."""

    _GATE = 'gate_cr_not_degraded_fast "a re-render"'

    def _reads(self, result: subprocess.CompletedProcess) -> int:
        return int(re.search(r"READS=(\d+)", result.stdout).group(1))

    def test_a_wait_for_capacity_that_clears_passes(self) -> None:
        for ready in (_WAITING_FOR_CAPACITY, _CAPACITY_BESIDE_AFFINITY, _MEMORY_ONLY):
            with self.subTest(ready=ready):
                result = run_provision_wait(
                    jobs="",
                    call=self._GATE,
                    phase=["Degraded", "Degraded", "Ready"],
                    ready=[ready, ready, _ALL_READY],
                    unschedulable_attempts=5,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("PASSED", result.stdout)
                self.assertEqual(self._reads(result), 3)
                # The artifact shows the transient and how long it took. The
                # seconds are $SECONDS deltas, which step on a wall-clock
                # second boundary however little time passed, so any count
                # is taken; the poll interval is the stub's zero.
                self.assertRegex(
                    result.stdout,
                    re.escape("platform-agent is Degraded after a re-render on a pod waiting for CPU or memory, ")
                    + r"\d+s"
                    + re.escape(f" in; re-read 1/5 in 0s (Ready condition: {ready})"),
                )
                self.assertIn("re-read 2/5", result.stdout)
                self.assertNotIn("re-read 3/5", result.stdout)
                self.assertRegex(result.stdout, re.escape("✓ the wait for capacity cleared after 2 re-reads, ") + r"\d+s\n")
                self.assertIn(f"✓ platform-agent is Ready after a re-render (Ready condition: {_ALL_READY})", result.stdout)
                self.assertNotIn("ERROR", result.stdout)

    def test_the_phase_and_condition_come_from_one_read(self) -> None:
        """The operator writes the phase and the Ready condition in one status
        update, and the wait polls across exactly that update. The stub
        answers each status read from the next version: here the first is
        Degraded waiting for capacity, the second healthy. One read per poll
        sees each version whole, waits once and passes. Two reads per poll
        would pair the first version's Degraded with the second's
        `Ready: all workloads ready` and fail on a contradiction."""
        result = run_provision_wait(
            jobs="",
            call=self._GATE,
            phase=["Degraded", "Ready"],
            ready=[_WAITING_FOR_CAPACITY, _ALL_READY],
            unschedulable_attempts=5,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self._reads(result), 2)
        self.assertIn(f"re-read 1/5 in 0s (Ready condition: {_WAITING_FOR_CAPACITY})", result.stdout)
        self.assertNotIn("re-read 2/5", result.stdout)
        self.assertIn(f"✓ platform-agent is Ready after a re-render (Ready condition: {_ALL_READY})", result.stdout)
        self.assertNotIn("ERROR", result.stdout)

    def test_a_wait_for_capacity_that_outlasts_the_window_fails_as_before(self) -> None:
        result = run_provision_wait(jobs="", call=self._GATE, phase="Degraded", ready=_WAITING_FOR_CAPACITY, unschedulable_attempts=3)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        # One read and the window's three re-reads, then the failure today's
        # gate prints, diagnostics and all.
        self.assertEqual(self._reads(result), 4)
        self.assertIn("re-read 3/3", result.stdout)
        self.assertNotIn("re-read 4/3", result.stdout)
        self.assertIn("the wait for capacity ended after 3 re-reads", result.stdout)
        self.assertIn(f"ERROR: platform-agent is Degraded after a re-render; Ready condition: {_WAITING_FOR_CAPACITY}", result.stdout)
        self.assertIn("--- provisioning Job pod logs ---", result.stdout)
        self.assertIn("kubectl logs -n kubeagents-system -l kubeagents.x-k8s.io/a2a-component=provision", result.stdout)
        self.assertIn("DUMPED", result.stdout)
        self.assertNotIn("PASSED", result.stdout)

    def test_a_wait_that_turns_into_another_degraded_fails_on_that_read(self) -> None:
        turned = {
            "a different Degraded": ("Degraded", "ImagePullBackOff: Container 'hermes-bridge' in pod platform-agent-gateway-x is waiting"),
            "a refused provision": ("Ready", "A2AProvisionFailed: TASKS holds 64 consumers and this configuration needs 71"),
            "unschedulable for want of something else": ("Degraded", "PodUnschedulable: Pod p cannot be scheduled onto any available node: 0/3 nodes are available: 3 node(s) didn't match Pod's node affinity/selector."),
        }
        for name, (phase, ready) in turned.items():
            with self.subTest(name):
                result = run_provision_wait(
                    jobs="", call=self._GATE, phase=["Degraded", phase], ready=[_WAITING_FOR_CAPACITY, ready], unschedulable_attempts=5
                )
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertEqual(self._reads(result), 2)
                # The first read was forgiven, and the second ended the wait:
                # a gate that failed on the first read, or took the phase and
                # the condition from two reads that straddle the update, can
                # land on READS=2 and this ERROR line without having waited.
                self.assertIn(f"re-read 1/5 in 0s (Ready condition: {_WAITING_FOR_CAPACITY})", result.stdout)
                self.assertNotIn("re-read 2/5", result.stdout)
                self.assertRegex(result.stdout, re.escape("the wait for capacity ended after 1 re-reads, ") + r"\d+s\n")
                self.assertIn(f"ERROR: platform-agent is {phase} after a re-render; Ready condition: {ready}", result.stdout)
                self.assertIn("DUMPED", result.stdout)
                self.assertNotIn("PASSED", result.stdout)

    def test_a_read_dropped_mid_wait_is_one_more_poll(self) -> None:
        """The read the wait sits on swallows a failed kubectl (the gate's
        first read always has, as main's did), so a GET the API drops reads
        back as no phase and no condition. Mid-wait that is one more poll,
        counted against the window, never the CR turning healthy: the read
        before it said Degraded. So is a status read back empty."""
        cases = {
            "dropped, then another Degraded": (
                ["Degraded", DROPPED_READ, "Degraded"],
                [_WAITING_FOR_CAPACITY, "", "ImagePullBackOff: Container 'hermes-bridge' is waiting"],
                1,
            ),
            "dropped, then a refused provision": (
                ["Degraded", DROPPED_READ, "Ready"],
                [_WAITING_FOR_CAPACITY, "", "A2AProvisionFailed: TASKS holds 64 consumers and this configuration needs 71"],
                1,
            ),
            "dropped, then Ready": (["Degraded", DROPPED_READ, "Ready"], [_WAITING_FOR_CAPACITY, "", _ALL_READY], 0),
            "an empty status, then another Degraded": (
                ["Degraded", "", "Degraded"],
                [_WAITING_FOR_CAPACITY, "", "ImagePullBackOff: Container 'hermes-bridge' is waiting"],
                1,
            ),
        }
        for name, (phases, readies, status) in cases.items():
            with self.subTest(name):
                result = run_provision_wait(jobs="", call=self._GATE, phase=phases, ready=readies, unschedulable_attempts=5)
                self.assertEqual(result.returncode, status, result.stdout + result.stderr)
                self.assertEqual(self._reads(result), 3)
                self.assertIn(f"re-read 1/5 in 0s (Ready condition: {_WAITING_FOR_CAPACITY})", result.stdout)
                self.assertRegex(
                    result.stdout,
                    re.escape("the read of platform-agent after a re-render returned nothing, ")
                    + r"\d+s"
                    + re.escape(f" in; re-read 2/5 in 0s (last Ready condition: {_WAITING_FOR_CAPACITY})"),
                )
                self.assertNotIn("re-read 3/5", result.stdout)
                self.assertNotIn("is unphased", result.stdout)
                if status == 0:
                    self.assertRegex(result.stdout, re.escape("✓ the wait for capacity cleared after 2 re-reads, ") + r"\d+s\n")
                    self.assertIn(f"✓ platform-agent is Ready after a re-render (Ready condition: {_ALL_READY})", result.stdout)
                    self.assertNotIn("ERROR", result.stdout)
                else:
                    self.assertRegex(result.stdout, re.escape("the wait for capacity ended after 2 re-reads, ") + r"\d+s\n")
                    self.assertIn(f"ERROR: platform-agent is {phases[2]} after a re-render; Ready condition: {readies[2]}", result.stdout)
                    self.assertIn("DUMPED", result.stdout)
                    self.assertNotIn("PASSED", result.stdout)

    def test_reads_dropped_to_the_end_of_the_window_fail_on_the_last_one_that_answered(self) -> None:
        result = run_provision_wait(
            jobs="", call=self._GATE, phase=["Degraded", DROPPED_READ], ready=[_WAITING_FOR_CAPACITY, ""], unschedulable_attempts=3
        )
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(self._reads(result), 4)
        self.assertIn("re-read 3/3", result.stdout)
        self.assertNotIn("re-read 4/3", result.stdout)
        self.assertRegex(
            result.stdout,
            re.escape("the wait for capacity ended after 3 re-reads, ")
            + r"\d+s"
            + re.escape("; the last read returned nothing, so this is the last one that answered\n"),
        )
        self.assertIn(f"ERROR: platform-agent is Degraded after a re-render; Ready condition: {_WAITING_FOR_CAPACITY}", result.stdout)
        self.assertIn("DUMPED", result.stdout)
        self.assertNotIn("PASSED", result.stdout)

    def test_a_dropped_first_read_is_one_more_poll(self) -> None:
        """One GET reads both the phase and the condition and swallows its
        failure, so a first read the API drops reads back as nothing. That
        is one more poll against the window, as it is mid-wait, never a pass
        as an unphased CR: main's gate made two reads, and a refusal passed
        only if both were dropped. The second read here is the answer."""
        refusal = "A2AProvisionFailed: TASKS holds 64 consumers and this configuration needs 71"
        cases = {
            "dropped, then Degraded with a refused provision": ("Degraded", refusal, 1),
            "dropped, then a refused provision under Ready": ("Ready", refusal, 1),
            "dropped, then another Degraded": ("Degraded", "InvalidGitRepoURL: not this PR's", 1),
            "dropped, then Ready": ("Ready", _ALL_READY, 0),
        }
        for name, (phase, ready, status) in cases.items():
            with self.subTest(name):
                result = run_provision_wait(
                    jobs="", call=self._GATE, phase=[DROPPED_READ, phase], ready=["", ready], unschedulable_attempts=5
                )
                self.assertEqual(result.returncode, status, result.stdout + result.stderr)
                self.assertEqual(self._reads(result), 2)
                self.assertIn(
                    "the first read of platform-agent after a re-render returned nothing; re-read 1/5 in 0s (no read has answered yet)\n",
                    result.stdout,
                )
                self.assertNotIn("re-read 2/5", result.stdout)
                self.assertNotIn("unphased", result.stdout)
                self.assertNotIn("wait for capacity", result.stdout)
                self.assertRegex(
                    result.stdout, re.escape(f"{'✓ ' if status == 0 else ''}platform-agent answered after 1 reads that returned nothing, ") + r"\d+s\n"
                )
                if status == 0:
                    self.assertIn(f"✓ platform-agent is Ready after a re-render (Ready condition: {_ALL_READY})", result.stdout)
                    self.assertNotIn("ERROR", result.stdout)
                else:
                    self.assertIn(f"ERROR: platform-agent is {phase} after a re-render; Ready condition: {ready}", result.stdout)
                    self.assertIn("DUMPED", result.stdout)
                    self.assertNotIn("PASSED", result.stdout)

    def test_a_window_of_reads_that_all_return_nothing_fails_as_unread(self) -> None:
        """No read answered, so there is no phase or condition to report and
        no last answer to fall back on: the gate fails closed and says the CR
        could not be read, not that it was unphased or Degraded."""
        result = run_provision_wait(jobs="", call=self._GATE, phase=DROPPED_READ, unschedulable_attempts=3)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(self._reads(result), 4)
        self.assertIn("the first read of platform-agent after a re-render returned nothing; re-read 1/3 in 0s (no read has answered yet)", result.stdout)
        self.assertRegex(
            result.stdout,
            re.escape("the read of platform-agent after a re-render returned nothing again, ")
            + r"\d+s"
            + re.escape(" in; re-read 3/3 in 0s (no read has answered yet)"),
        )
        self.assertNotIn("re-read 4/3", result.stdout)
        self.assertRegex(
            result.stdout,
            re.escape("ERROR: could not read platform-agent after a re-render: all 4 reads in ")
            + r"\d+s"
            + re.escape(" returned nothing, so there is no phase or Ready condition to judge\n"),
        )
        self.assertNotIn("unphased", result.stdout)
        self.assertNotIn("last Ready condition", result.stdout)
        self.assertNotIn("wait for capacity", result.stdout)
        self.assertIn("--- provisioning Job pod logs ---", result.stdout)
        self.assertIn("DUMPED", result.stdout)
        self.assertNotIn("PASSED", result.stdout)

    def test_a_refused_provision_fails_on_the_first_read(self) -> None:
        refusals = {
            ("Degraded", "A2AProvisionFailed: TASKS holds 64 consumers and this configuration needs 71"),
            ("Ready", "A2AProvisionFailed: TASKS holds 64 consumers and this configuration needs 71"),
            # The reason decides, not words that happen to be in the message.
            ("Degraded", "A2AProvisionFailed: provision pod: 0/2 nodes are available: 2 Insufficient cpu, 2 Insufficient memory."),
        }
        for phase, ready in refusals:
            with self.subTest(phase=phase, ready=ready):
                # A window far longer than the stub's one answer would need,
                # so a wait shows as extra reads, not a pass.
                result = run_provision_wait(jobs="", call=self._GATE, phase=phase, ready=ready, unschedulable_attempts=60)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertEqual(self._reads(result), 1)
                self.assertNotIn("re-read", result.stdout)
                self.assertIn(f"ERROR: platform-agent is {phase} after a re-render; Ready condition: {ready}", result.stdout)
                self.assertIn("--- provisioning Job pod logs ---", result.stdout)
                self.assertIn("DUMPED", result.stdout)

    def test_unschedulable_for_what_a_scale_up_cannot_fix_fails_on_the_first_read(self) -> None:
        prefix = "PodUnschedulable: Pod platform-agent-gateway-x cannot be scheduled onto any available node: "
        cannot_fix = {
            "node affinity or selector only": prefix
            + "0/3 nodes are available: 3 node(s) didn't match Pod's node affinity/selector. preemption: 0/3 nodes are available: 3 Preemption is not helpful for scheduling.",
            "an untolerated taint only": prefix
            + "0/3 nodes are available: 3 node(s) had untolerated taint {node.kubernetes.io/not-ready: }. preemption: 0/3 nodes are available: 3 Preemption is not helpful for scheduling.",
            "affinity and taint, no capacity": prefix
            + "0/3 nodes are available: 1 node(s) had untolerated taint {dedicated: infra}, 2 node(s) didn't match Pod's node affinity/selector.",
            "a resource no scale-up of these nodes adds": prefix + "0/2 nodes are available: 2 Insufficient nvidia.com/gpu.",
            "the RuntimeClass sentence": "PodUnschedulable: Pod platform-agent-gateway-x is waiting to be scheduled because no nodes in the cluster match the requested RuntimeClass 'gvisor'. For GKE Standard, enable GKE Sandbox by provisioning a gVisor node pool.",
        }
        for name, ready in cannot_fix.items():
            with self.subTest(name):
                result = run_provision_wait(jobs="", call=self._GATE, phase="Degraded", ready=ready, unschedulable_attempts=60)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertEqual(self._reads(result), 1)
                self.assertNotIn("re-read", result.stdout)
                self.assertIn(f"ERROR: platform-agent is Degraded after a re-render; Ready condition: {ready}", result.stdout)
                self.assertIn("DUMPED", result.stdout)

    def test_the_window_is_bounded_and_inside_the_next_gates_rollout_budget(self) -> None:
        consts = constants()
        window = int(consts["MODE_NEXT_UNSCHEDULABLE_ATTEMPTS"]) * int(consts["MODE_NEXT_POLL_SECONDS"])
        self.assertGreater(window, 0)
        self.assertLess(window, int(consts["MODE_NEXT_ROLLOUT_TIMEOUT"].rstrip("s")))

    def test_the_reason_and_message_are_the_operators(self) -> None:
        """The gate matches the reason the operator gives an Unschedulable pod
        and the scheduler message it wraps; a rename there would make the
        wait a silent no-op and #2414 come back."""
        consts = constants()
        controller = text(_CONTROLLER / "platformagent_controller.go")
        self.assertIn(f'reason = "{consts["CR_READY_REASON_POD_UNSCHEDULABLE"]}"', controller)
        self.assertIn('message = fmt.Sprintf("Pod %s cannot be scheduled onto any available node: %s.", pod.Name, cleanMsg)', controller)


# The agent pod _WAITING_FOR_CAPACITY names, as the gate's pod read lists it
# (name, node, phase, deletionTimestamp, tab-separated): unscheduled (no node
# yet), and bound to a node.
_AGENT_POD = "platform-agent-gateway-57ddfbc5bf-25f5x"
_OTHER_POD = "platform-agent-gateway-57ddfbc5bf-9q8rw"
_NODE = "gk3-autopilot-pool-2-1a2b3c4d-x7k9"
_DELETED_AT = "2026-10-06T12:00:00Z"


def _pod(name: str, node: str = "", phase: str = "Pending", deleted: str = "") -> str:
    return f"{name}\t{node}\t{phase}\t{deleted}\n"


_POD_UNSCHEDULED = _pod(_AGENT_POD)
_POD_SCHEDULED = _pod(_AGENT_POD, _NODE)


class CapacityWaitReadsThePodTest(unittest.TestCase):
    """While the gate forgives a capacity PodUnschedulable it also reads the
    agent's pods, once per poll, and hands off to the rollout gate once one
    is bound to a node. The CR cannot be waited on for that: the operator
    watches no Pods, so the scheduler binding the pod moves nothing that
    wakes it, and its status keeps the scheduler's old message until its
    next pass -- the Deployment's status changing when the pod is Ready, or
    a requeue that is fifteen minutes once the provision Job is done. Image
    pulls and the agent's start on a fresh node all ran inside the window on
    that stale text."""

    _GATE = 'gate_cr_not_degraded_fast "a re-render"'

    def _count(self, name: str, result: subprocess.CompletedProcess) -> int:
        return int(re.search(rf"(?:^| ){name}=(\d+)", result.stdout, re.MULTILINE).group(1))

    def _handed_off(self, result: subprocess.CompletedProcess, rereads: int, pod: str = _AGENT_POD) -> None:
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PASSED", result.stdout)
        self.assertRegex(
            result.stdout,
            re.escape(f"✓ the wait for capacity handed off after {rereads} re-reads, ")
            + r"\d+s"
            + re.escape(
                f": the agent pod was scheduled on {_NODE}"
                f" ({pod}); the rollout gate decides from here"
                f" (platform-agent still reads Degraded; Ready condition: {_WAITING_FOR_CAPACITY})\n"
            ),
        )
        # A hand-off, not the CR read as healthy.
        self.assertNotIn("✓ platform-agent is", result.stdout)
        self.assertNotIn("cleared", result.stdout)
        self.assertNotIn("ERROR", result.stdout)

    def test_a_scheduled_pod_under_a_stale_condition_hands_off_on_that_poll(self) -> None:
        result = run_provision_wait(
            jobs="", call=self._GATE, phase="Degraded", ready=_WAITING_FOR_CAPACITY, pods=_POD_SCHEDULED, unschedulable_attempts=5
        )
        self._handed_off(result, 0)
        self.assertEqual(self._count("READS", result), 1)
        self.assertEqual(self._count("POD_READS", result), 1)
        self.assertNotIn("re-read 1/", result.stdout)

    def test_a_pod_scheduled_on_the_third_poll_hands_off_then(self) -> None:
        result = run_provision_wait(
            jobs="",
            call=self._GATE,
            phase="Degraded",
            ready=_WAITING_FOR_CAPACITY,
            pods=[_POD_UNSCHEDULED, _POD_UNSCHEDULED, _POD_SCHEDULED],
            unschedulable_attempts=5,
        )
        self._handed_off(result, 2)
        self.assertEqual(self._count("READS", result), 3)
        self.assertEqual(self._count("POD_READS", result), 3)
        self.assertIn("re-read 2/5", result.stdout)
        self.assertNotIn("re-read 3/5", result.stdout)

    def test_an_unbound_pod_listed_first_does_not_hide_a_bound_one(self) -> None:
        """The scan skips a line with no node rather than stopping on it."""
        unbound = _pod(_OTHER_POD)
        result = run_provision_wait(
            jobs="", call=self._GATE, phase="Degraded", ready=_WAITING_FOR_CAPACITY, pods=unbound + _POD_SCHEDULED, unschedulable_attempts=5
        )
        self._handed_off(result, 0)
        self.assertEqual(self._count("POD_READS", result), 1)

    def _no_hand_off(self, pods: str | list[str]) -> None:
        result = run_provision_wait(jobs="", call=self._GATE, phase="Degraded", ready=_WAITING_FOR_CAPACITY, pods=pods, unschedulable_attempts=3)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(self._count("POD_READS", result), 4)
        self.assertIn("the wait for capacity ended after 3 re-reads", result.stdout)
        self.assertIn(f"ERROR: platform-agent is Degraded after a re-render; Ready condition: {_WAITING_FOR_CAPACITY}", result.stdout)
        self.assertNotIn("handed off", result.stdout)
        self.assertNotIn("PASSED", result.stdout)

    def test_a_terminal_bound_pod_beside_the_pending_one_does_not_hand_off(self) -> None:
        """An evicted or admission-rejected agent pod keeps its node and sits
        under the label until pod GC; Recreate does not wait for it. It is
        not the pod the wait is about, so the gate keeps polling."""
        for phase in ("Failed", "Succeeded"):
            with self.subTest(f"an old {phase} pod listed first"):
                self._no_hand_off(_pod(_OTHER_POD, _NODE, phase) + _POD_UNSCHEDULED)
            with self.subTest(f"the named pod {phase} on its node, its replacement pending"):
                # The condition still names the pod the kubelet rejected
                # until the operator's next pass.
                self._no_hand_off(_pod(_AGENT_POD, _NODE, phase) + _pod(_OTHER_POD))

    def test_a_terminating_bound_pod_does_not_hand_off(self) -> None:
        for name, pods in {
            "the named pod, alone": _pod(_AGENT_POD, _NODE, "Running", _DELETED_AT),
            "the named pod, its replacement pending": _pod(_AGENT_POD, _NODE, "Running", _DELETED_AT) + _pod(_OTHER_POD),
            "another pod, the named one pending": _pod(_OTHER_POD, _NODE, "Running", _DELETED_AT) + _POD_UNSCHEDULED,
        }.items():
            with self.subTest(name):
                self._no_hand_off(pods)

    def test_the_named_pod_becoming_bound_hands_off_and_the_line_names_it(self) -> None:
        """The condition names a pod; while it is listed and live, the gate
        decides on it alone, not on another bound pod listed before it."""
        other_bound = _pod(_OTHER_POD, _NODE, "Running")
        result = run_provision_wait(
            jobs="",
            call=self._GATE,
            phase="Degraded",
            ready=_WAITING_FOR_CAPACITY,
            pods=[other_bound + _POD_UNSCHEDULED, other_bound + _POD_SCHEDULED],
            unschedulable_attempts=5,
        )
        self._handed_off(result, 1)
        self.assertEqual(self._count("POD_READS", result), 2)
        self.assertNotIn(f"({_OTHER_POD})", result.stdout)

    def test_a_named_pod_no_longer_listed_falls_back_to_a_live_bound_pod(self) -> None:
        result = run_provision_wait(
            jobs="",
            call=self._GATE,
            phase="Degraded",
            ready=_WAITING_FOR_CAPACITY,
            pods=_pod(_AGENT_POD, _NODE, "Failed") + _pod(_OTHER_POD, _NODE, "Running"),
            unschedulable_attempts=5,
        )
        self._handed_off(result, 0, pod=_OTHER_POD)

    def test_the_pod_read_is_one_call_with_phase_and_deletion(self) -> None:
        gate = shell_function("gate_cr_not_degraded")
        self.assertEqual(gate.count("kubectl get pods"), 1)
        self.assertIn(
            """jsonpath='{range .items[*]}{.metadata.name}{"\\t"}{.spec.nodeName}{"\\t"}{.status.phase}{"\\t"}{.metadata.deletionTimestamp}{"\\n"}{end}'""",
            gate,
        )

    def test_a_pod_never_scheduled_fails_at_the_end_of_the_window_as_before(self) -> None:
        for name, pods in {"unscheduled throughout": _POD_UNSCHEDULED, "no agent pod listed": ""}.items():
            with self.subTest(name):
                result = run_provision_wait(
                    jobs="", call=self._GATE, phase="Degraded", ready=_WAITING_FOR_CAPACITY, pods=pods, unschedulable_attempts=3
                )
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertEqual(self._count("READS", result), 4)
                # Every poll read the pod, the window's last included.
                self.assertEqual(self._count("POD_READS", result), 4)
                self.assertIn("re-read 3/3", result.stdout)
                self.assertIn("the wait for capacity ended after 3 re-reads", result.stdout)
                self.assertIn(f"ERROR: platform-agent is Degraded after a re-render; Ready condition: {_WAITING_FOR_CAPACITY}", result.stdout)
                self.assertIn("DUMPED", result.stdout)
                self.assertNotIn("handed off", result.stdout)
                self.assertNotIn("PASSED", result.stdout)

    def test_a_dropped_pod_read_is_one_more_poll(self) -> None:
        result = run_provision_wait(
            jobs="",
            call=self._GATE,
            phase="Degraded",
            ready=_WAITING_FOR_CAPACITY,
            pods=[DROPPED_READ, _POD_SCHEDULED],
            unschedulable_attempts=5,
        )
        self._handed_off(result, 1)
        self.assertEqual(self._count("READS", result), 2)
        self.assertEqual(self._count("POD_READS", result), 2)
        self.assertIn("re-read 1/5", result.stdout)
        # And dropped to the end of the window, the gate fails closed.
        result = run_provision_wait(
            jobs="", call=self._GATE, phase="Degraded", ready=_WAITING_FOR_CAPACITY, pods=DROPPED_READ, unschedulable_attempts=3
        )
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(self._count("POD_READS", result), 4)
        self.assertIn(f"ERROR: platform-agent is Degraded after a re-render; Ready condition: {_WAITING_FOR_CAPACITY}", result.stdout)
        self.assertNotIn("handed off", result.stdout)

    def test_a_condition_about_another_workloads_pod_is_not_handed_off_by_the_agents(self) -> None:
        """The rollout gate the hand-off leads to is the agent Deployment's,
        so only a condition about an agent pod is handed off on one. The
        operator scans the A2A gateway, the sandbox and the proxy pods too."""
        other = _WAITING_FOR_CAPACITY.replace(_AGENT_POD, "platform-agent-a2a-gateway-6c9f7d8b5-q2wzn")
        result = run_provision_wait(jobs="", call=self._GATE, phase="Degraded", ready=other, pods=_POD_SCHEDULED, unschedulable_attempts=3)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(self._count("READS", result), 4)
        self.assertEqual(self._count("POD_READS", result), 0)
        self.assertIn(f"ERROR: platform-agent is Degraded after a re-render; Ready condition: {other}", result.stdout)
        self.assertNotIn("handed off", result.stdout)

    def test_the_pod_read_is_the_operators_selector_for_the_agent_pod(self) -> None:
        """The stub answers only `-l app=platform-agent-gateway`; this pins
        that to what the operator lists the agent's pods by when it writes
        PodUnschedulable, and to the Deployment the rollout gate waits on."""
        consts = constants()
        self.assertEqual(consts["AGENT_DEPLOYMENT_NAME"], "${PLATFORM_AGENT_CR_NAME}-gateway")
        controller = text(_CONTROLLER / "platformagent_controller.go")
        scan = controller[controller.index("func (r *PlatformAgentReconciler) getDeploymentStatusDetails(") :]
        self.assertIn('{"app": agent.Name + "-gateway"},', scan[: scan.index("pods := make(")])
        self.assertIn('-l "app=${AGENT_DEPLOYMENT_NAME}"', shell_function("gate_cr_not_degraded"))


class RenderedBridgeTest(unittest.TestCase):
    """The bridge is the operator's (#2592): the deploy declares no sidecar
    and hands the operator the three settings instead. What the rendered
    container carries -- the agent's env, mounts, securityContext and
    resources, minus the bus token and the agent's bus identity, plus the
    bridge's own env -- is the operator's to pin, and
    k8s-operator/internal/controller/platformagent_a2a_bridge_test.go does
    (TestANextInstallWithNoDeclaredBridgeGetsOne). What is pinned here is the
    hand-off: nothing in the deploy writes spec.deployment.sidecars, and the
    operator renders a container under the name the log gate reads."""

    def test_the_deploy_declares_no_sidecar(self) -> None:
        code = [line for line in text(_CI_DEPLOY).splitlines() if not line.lstrip().startswith("#")]
        for line in code:
            with self.subTest(line=line):
                self.assertNotIn("sidecars", line)

    def test_the_operator_renders_the_container_the_log_gate_reads(self) -> None:
        consts = constants()
        bridge_go = text(_A2A_BRIDGE)
        self.assertEqual(consts["BRIDGE_SIDECAR_NAME"], go_constant(_A2A_BRIDGE, "a2aBridgeContainerName"))
        self.assertIn("Name:            a2aBridgeContainerName,", bridge_go)
        self.assertIn("Image:           a2aBridgeImage(agentContainer.Image),", bridge_go)
        self.assertIn('-c "${BRIDGE_SIDECAR_NAME}"', lifted(*_MODE_SECTION))


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

    def test_the_inventory_check_pins_its_builder_and_the_inventory_carries_it(self) -> None:
        script = text(_INVENTORY_CHECK)
        # The go-directive call site is pinned by test_check_image_inventory_go_directive.
        self.assertIn("check_base_image golang a2a/Dockerfile.hermes-bridge GOLANG_IMAGE GOLANG_VERSION", script)
        # The bridge is release surface: a first-party entry the release
        # workflow publishes, whose override is the one the operator reads
        # when it renders the bridge (#2592), and whose compiled name the
        # inventory check holds.
        self.assertIn(
            "check_compiled_image_name hermes-bridge k8s-operator/internal/controller/platformagent_a2a_bridge.go a2aBridgeImageName",
            script,
        )
        inventory = json.loads(text(_REPO_ROOT / "images.json"))
        by_name = {image["name"]: image for image in inventory["images"]}
        self.assertIn("hermes-bridge", by_name)
        bridge = by_name["hermes-bridge"]
        self.assertEqual(bridge["origin"], "first-party")
        self.assertEqual(bridge["tagPolicy"], "release")
        self.assertEqual(bridge["override"], go_constant(_A2A_BRIDGE, "a2aBridgeImageEnvVar"))
        for name, override in (
            ("a2a-gateway", "A2A_GATEWAY_IMAGE"),
            ("a2a-worker", "A2A_WORKER_IMAGE"),
            ("a2a-authcallout", "A2A_CALLOUT_IMAGE"),
            ("a2a-verifier", "A2A_VERIFIER_IMAGE"),
            ("a2a-console", "A2A_CONSOLE_IMAGE"),
            ("hermes-bridge", "A2A_BRIDGE_IMAGE"),
        ):
            self.assertEqual(by_name[name]["override"], override)
            self.assertEqual(by_name[name]["tagPolicy"], "release")

    def test_the_a2a_step_starts_at_once_and_builds_the_five_in_order(self) -> None:
        """The five A2A images do not depend on the platform image, so their
        step must not queue behind it; only the bridge does."""
        self.assertEqual(build_step("a2a")["waitFor"], ["-"])
        result = run_build_step("a2a", next_stack_substitutions(), 'docker() { echo "docker $*"; }')
        self.assertEqual(result.returncode, 0, result.stderr)
        expected = []
        for suffix, image in zip(_A2A_DOCKERFILE_SUFFIXES, _A2A_IMAGES[:5], strict=True):
            uri = f"{_AR_REPO}/{image}:{_TAG}"
            # The worker's Dockerfile copies the credential broker's shim client
            # from agents/platform/scripts, so it builds from the repository
            # root; the gateway and the callout stay on the a2a/ module root.
            context = "." if suffix == "worker" else "a2a"
            expected.append(f"docker build --platform linux/amd64 -t {uri} -f a2a/Dockerfile.{suffix} {context}")
            expected.append(f"docker push {uri}")
        self.assertEqual(result.stdout.splitlines(), expected)

    def test_the_bridge_step_waits_for_the_platform_image_and_builds_from_it(self) -> None:
        self.assertEqual(build_step("a2a-bridge")["waitFor"], ["platform", "a2a"])
        result = run_build_step("a2a-bridge", next_stack_substitutions(), 'docker() { echo "docker $*"; }')
        self.assertEqual(result.returncode, 0, result.stderr)
        uri = f"{_AR_REPO}/hermes-bridge:{_TAG}"
        # The inspect that precedes the build runs with its output discarded, so
        # it leaves no line here; the refusal test below is where it is observed.
        self.assertEqual(
            result.stdout.splitlines(),
            [
                f"docker build --platform linux/amd64 -t {uri} --build-arg PLATFORM_AGENT_IMAGE={_PLATFORM_URI} -f a2a/Dockerfile.hermes-bridge a2a",
                f"docker push {uri}",
            ],
        )

    def test_the_bridge_step_refuses_a_platform_image_that_is_not_this_builds(self) -> None:
        stub = textwrap.dedent(
            """\
            docker() {
              if [ "$1" = "image" ] && [ "$2" = "inspect" ]; then return 1; fi
              echo "docker $*"
            }
            """
        )
        result = run_build_step("a2a-bridge", next_stack_substitutions(), stub)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("built FROM this run's platform image", result.stderr)
        self.assertEqual(result.stdout, "")


if __name__ == "__main__":
    unittest.main()
