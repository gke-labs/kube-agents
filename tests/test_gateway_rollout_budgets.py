"""Tests for the platform-agent-gateway rollout budgets.

The companion to test_hindsight_probes.py, for the Deployment upgrade.sh
actually gates on -- and for scripts/release/wait_for_gke_readiness.sh,
which waits on the same Deployments after the RC environment is provisioned and
is bound by the same rule. The same three numbers have to stay in the same
order:

    startupProbe budget  <  rollout gate  <  progressDeadlineSeconds

The upper bound is the hard one. Past the deadline the Deployment reports
ProgressDeadlineExceeded and any caller's wait returns early however long it
was given, so a gate at or above the deadline buys nothing.

The gateway spent a long time violating this on both sides. Kubernetes
defaults progressDeadlineSeconds to 600s, and nothing set it, while
agentAPIProbe(10, 60) sanctions a 605s cold boot -- the kubelet was told to
tolerate a boot the Deployment gives up on. The rollout gate sat at 180s,
under both, and reported red on deploys that had succeeded: a gateway pod
measured 215s to Ready in autopush and 259s in staging.

Every number is read from the source that owns it rather than hardcoded here,
so raising one cannot leave this suite asserting against a value no install
uses.
"""

import pathlib
import re
import sys
import tempfile
import unittest
from unittest import mock

import yaml

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_MANIFESTS_GO = _ROOT / "k8s-operator" / "internal" / "controller" / "platformagent_manifests.go"
_UPGRADE_SCRIPT = _ROOT / "upgrade.sh"
_READINESS_SCRIPT = _ROOT / "scripts" / "release" / "wait_for_gke_readiness.sh"
_MODE_GATE_SCRIPT = _ROOT / "scripts" / "release" / "platform_agent_mode.sh"
_CONFIRM_IMAGE_SCRIPT = _ROOT / "scripts" / "confirm_agent_image.sh"
# Where the front doors' constants for the chart's fixed object names live.
# upgrade.sh spells a Deployment through one of them ("deployment/${NAME}"),
# so a gate is matched by the literal name or by any constant that holds it.
_INSTALLER_COMMON = _ROOT / "scripts" / "installer" / "installer_common.sh"


def _deployment_spellings(deployment):
    """The literal name plus every `readonly X="<name>"` constant that equals it."""
    constants = re.findall(
        rf'^readonly (\w+)="{re.escape(deployment)}"$',
        _INSTALLER_COMMON.read_text(),
        re.MULTILINE,
    )
    return [re.escape(deployment)] + [rf"\$\{{{name}\}}" for name in constants]


def rollout_gate_pattern(deployment):
    """The `kubectl rollout status` line for one Deployment, however it is spelled."""
    return re.compile(
        r'kubectl rollout status "?deployment/(?:'
        + "|".join(_deployment_spellings(deployment))
        + r')"?\s[^\n]*?--timeout=(\d+)s'
    )

# What the gate must have over the startupProbe budget, in seconds, for
# Recreate termination of the previous pod plus sequential cold image pulls
# across initContainers (platform-agent, plugins, credential-proxy) before the
# main container starts at all. Measured at 440s (7m20s) in autopush (#2087).
_PULL_ALLOWANCE_SECONDS = 480

# Kubernetes' default when a Deployment does not set progressDeadlineSeconds.
# Deployments with no explicit progressDeadlineSeconds rely on this default.
_DEFAULT_PROGRESS_DEADLINE_SECONDS = 600


def _gateway_startup_budget_seconds():
    """How long the gateway's startupProbe tolerates failure.

    Both halves come from the Go source: the call site fixes periodSeconds and
    failureThreshold, and agentAPIProbe fixes initialDelaySeconds. Counting the
    initial delay matters -- omitting it under-reports the budget, which is the
    direction that hides a violation.
    """
    text = _MANIFESTS_GO.read_text()

    call = re.search(r"StartupProbe:\s*agentAPIProbe\((\d+),\s*(\d+)\)", text)
    assert call, "could not find the gateway StartupProbe call to agentAPIProbe"
    period, failure_threshold = int(call.group(1)), int(call.group(2))

    body = re.search(r"func agentAPIProbe\([^)]*\)[^{]*\{.*?\n\}", text, re.DOTALL)
    assert body, "could not find func agentAPIProbe"
    initial = re.search(r"InitialDelaySeconds:\s*(\d+)", body.group(0))
    assert initial, "could not find InitialDelaySeconds in agentAPIProbe"

    return int(initial.group(1)) + period * failure_threshold


def _gateway_progress_deadline_seconds():
    """The ceiling the operator pins on the gateway Deployment."""
    match = re.search(
        r"const gatewayProgressDeadlineSeconds int32 = (\d+)", _MANIFESTS_GO.read_text()
    )
    assert match, "could not find gatewayProgressDeadlineSeconds in platformagent_manifests.go"
    return int(match.group(1))


def _rollout_gate_seconds(workflow, deployment):
    """The --timeout on a `kubectl rollout status` for one Deployment."""
    match = rollout_gate_pattern(deployment).search(workflow.read_text())
    assert match, f"could not find the rollout gate for {deployment} in {workflow.name}"
    return int(match.group(1))


class GatewayRolloutBudgetTest(unittest.TestCase):
    """The three gateway budgets, and the order they have to stay in."""

    def setUp(self):
        self.startup = _gateway_startup_budget_seconds()
        self.gate = _rollout_gate_seconds(_UPGRADE_SCRIPT, "platform-agent-gateway")
        self.deadline = _gateway_progress_deadline_seconds()

    def test_the_startup_budget_covers_a_cold_gvisor_boot(self):
        # Fifteen minutes. Cold container boot under gVisor (stage2-hook chown,
        # skill sync + provenance verification, multi-profile config generation,
        # SSH sandbox mirroring, and Hermes Gateway startup) measured 531s-570s
        # in autopush (#2087), leaving almost no margin under the old 605s ceiling.
        self.assertGreaterEqual(
            self.startup,
            900,
            f"a {self.startup}s startupProbe budget leaves too little headroom over "
            "a 531s-570s cold gVisor boot (#2087)",
        )

    def test_the_gate_covers_the_startup_budget_and_the_image_pull(self):
        self.assertGreaterEqual(
            self.gate,
            self.startup + _PULL_ALLOWANCE_SECONDS,
            f"a {self.gate}s gate leaves {self.gate - self.startup}s for node scale-up and "
            f"an image pull on top of a {self.startup}s startupProbe budget; the workflow "
            "fails the deploy when it expires, so this reds a rollout that succeeded",
        )

    def test_the_progress_deadline_outlasts_the_gate(self):
        # Without this the gate is decorative: kubectl rollout status returns
        # "exceeded its progress deadline" the moment the Deployment gives up,
        # however long the caller asked to wait.
        self.assertGreater(
            self.deadline,
            self.gate,
            f"a {self.gate}s gate against a {self.deadline}s progressDeadlineSeconds cannot "
            "run its full length; raise the deadline in the operator, not just the gate",
        )

    def test_the_progress_deadline_outlasts_the_startup_budget(self):
        # The inversion this file exists for: the kubelet tolerating a longer
        # cold boot than the Deployment will wait for means a pod using its
        # sanctioned startup time is failed by the Deployment regardless of
        # what any gate says.
        self.assertGreater(
            self.deadline,
            self.startup,
            f"agentAPIProbe sanctions a {self.startup}s cold boot the Deployment abandons "
            f"at {self.deadline}s",
        )


def _readiness_gate_seconds(deployment, variable):
    """The gate wait_for_gke_readiness.sh applies to one Deployment.

    Two halves, because the script gates through a shell constant rather than a
    literal: the constant's value, and the fact that this Deployment's `rollout
    status` is the line that reads it. Asserting only the value would keep
    passing if the two Deployments were pointed at the same constant again,
    which is the state this split exists to leave behind.
    """
    text = _READINESS_SCRIPT.read_text()

    declared = re.search(rf'readonly {re.escape(variable)}="(\d+)s"', text)
    assert declared, f"could not find readonly {variable} in {_READINESS_SCRIPT.name}"

    used = re.search(
        rf"kubectl rollout status deployment/{re.escape(deployment)}\b[^\n]*?"
        rf'--timeout="\$\{{{re.escape(variable)}\}}"',
        text,
    )
    assert used, f"{deployment}'s rollout status in {_READINESS_SCRIPT.name} does not use {variable}"

    return int(declared.group(1))


class ReleaseReadinessGateTest(unittest.TestCase):
    """The RC pipeline's own gates, which are not the deploy workflows'.

    wait_for_gke_readiness.sh waits on the same two Deployments after the RC
    environment is provisioned, so it is bound by the same ordering -- but it
    sat outside this file's scope and ran a single 300s gate for both. That was
    under the gateway's 605s startupProbe budget, and went unnoticed while the
    RC provisioned Standard clusters: a fresh Autopilot cluster pays node
    scale-up and a first image pull before the container starts.
    """

    def setUp(self):
        self.startup = _gateway_startup_budget_seconds()
        self.deadline = _gateway_progress_deadline_seconds()
        self.gateway_gate = _readiness_gate_seconds(
            "platform-agent-gateway", "GATEWAY_READINESS_TIMEOUT"
        )
        self.litellm_gate = _readiness_gate_seconds("litellm", "LITELLM_READINESS_TIMEOUT")

    def test_the_gateway_gate_covers_the_startup_budget_and_the_image_pull(self):
        self.assertGreaterEqual(
            self.gateway_gate,
            self.startup + _PULL_ALLOWANCE_SECONDS,
            f"a {self.gateway_gate}s gate leaves "
            f"{self.gateway_gate - self.startup}s for node scale-up and an image pull on "
            f"top of a {self.startup}s startupProbe budget; on a fresh Autopilot cluster "
            "this reds an RC that was still coming up",
        )

    def test_the_gateway_gate_stays_under_the_progress_deadline(self):
        self.assertLess(
            self.gateway_gate,
            self.deadline,
            f"a {self.gateway_gate}s gate against a {self.deadline}s progressDeadlineSeconds "
            "cannot run its full length",
        )

    def test_the_litellm_gate_stays_under_the_default_progress_deadline(self):
        # litellm sets no progressDeadlineSeconds of its own, so unlike the
        # gateway it has the 600s default as its ceiling, not 1800s.
        self.assertLess(
            self.litellm_gate,
            _DEFAULT_PROGRESS_DEADLINE_SECONDS,
            f"litellm runs on the {_DEFAULT_PROGRESS_DEADLINE_SECONDS}s default deadline; a "
            f"{self.litellm_gate}s gate cannot run its full length. Set an explicit deadline "
            "first, as the gateway does",
        )

    def test_the_two_deployments_do_not_share_one_gate(self):
        """The ceilings differ (600s vs 1800s), so one number cannot respect both."""
        self.assertNotEqual(
            self.gateway_gate,
            self.litellm_gate,
            "a single readiness gate for both Deployments is either under the gateway's "
            "cold-start cost or over litellm's default progress deadline",
        )


class UpgradeRolloutGateTest(unittest.TestCase):
    """The upgrade.sh rollout gates stay under default progress deadlines."""

    def test_controller_gate_stays_under_the_default_progress_deadline(self):
        gate = _rollout_gate_seconds(_UPGRADE_SCRIPT, "kube-agents-controller-manager")
        self.assertLess(
            gate,
            _DEFAULT_PROGRESS_DEADLINE_SECONDS,
            f"kube-agents-controller-manager runs on the {_DEFAULT_PROGRESS_DEADLINE_SECONDS}s default deadline; "
            f"a {gate}s gate cannot run its full length.",
        )


# Fixed runner setup (checkout, setup-python, install_e2e_deps.sh, setup-gcloud,
# get-gke-credentials) before wait_for_gke_readiness.sh starts.
_E2E_RUNNER_SETUP_SECONDS = 300
# Post-readiness test execution allowance for the RC gate (blocking `gchat` plus
# optional `rc`, where the stockout fixture claims 600s and scenario 04 watches
# for 360s) and for `--suite all` across all five E2E test files (matching the
# 60-minute suite budget in staging-promotion-pipeline.yml).
_E2E_RC_SUITE_ALLOWANCE_SECONDS = 1200
_E2E_ALL_SUITES_ALLOWANCE_SECONDS = 3600
# What the upgrade.sh workflows do outside the waits the floor sums. The
# reconcile job: checkout, auth and gcloud setup, the lease and KMS checks,
# and terraform init/plan/apply outside the Helm release wait. The rollback
# leg: a fetch-depth 0 checkout, two checkout_at, two check_images_exist
# registry sweeps, a dry run, snapshots and helm history calls between its
# four moves (rollback_environment.sh L443-L470, L493-L509).
_UPGRADE_WORKFLOW_SETUP_SECONDS = 600
_ROLLBACK_LEG_SETUP_SECONDS = 900


def _confirm_agent_image_timeout_seconds():
    """Default timeout of scripts/confirm_agent_image.sh, called when COMMIT_SHA is set."""
    match = re.search(
        r'^timeout="\$\{AGENT_IMAGE_CONFIRM_TIMEOUT:-(\d+)\}"$',
        _CONFIRM_IMAGE_SCRIPT.read_text(),
        re.MULTILINE,
    )
    assert match, "could not find AGENT_IMAGE_CONFIRM_TIMEOUT default in confirm_agent_image.sh"
    return int(match.group(1))


def _mode_next_gate_seconds():
    """The whole spec.mode `next` gate in platform_agent_mode.sh: one budget, every wait inside it.

    wait_for_gke_readiness.sh runs it after its own two gates when the caller
    passes `mode: next`, so such a caller's job has to cover it once on top of
    them. The default is read rather than restated, so raising it reds a
    caller whose timeout no longer covers the gate.
    """
    match = re.search(
        r'^readonly PLATFORM_AGENT_MODE_GATE_TIMEOUT_SECONDS="\$\{PLATFORM_AGENT_MODE_GATE_TIMEOUT_SECONDS:-(\d+)\}"$',
        _MODE_GATE_SCRIPT.read_text(),
        re.MULTILINE,
    )
    assert match, f"could not find PLATFORM_AGENT_MODE_GATE_TIMEOUT_SECONDS in {_MODE_GATE_SCRIPT.name}"
    return int(match.group(1))


def _sandbox_rollout_timeout_seconds():
    """SANDBOX_ROLLOUT_TIMEOUT in upgrade.sh, waited twice (shell + credential-proxy)."""
    match = re.search(r'^SANDBOX_ROLLOUT_TIMEOUT="(\d+)s"$', _UPGRADE_SCRIPT.read_text(), re.MULTILINE)
    assert match, "could not find SANDBOX_ROLLOUT_TIMEOUT in upgrade.sh"
    return int(match.group(1))


def _terraform_helm_timeout_seconds():
    """helm_release.kube_agents timeout in terraform/examples/full-install/main.tf."""
    tf = (_ROOT / "terraform" / "examples" / "full-install" / "main.tf").read_text()
    block = re.search(r'resource "helm_release" "kube_agents"\s*\{.*?\n\}', tf, re.DOTALL)
    assert block, "could not find resource helm_release kube_agents in main.tf"
    match = re.search(r"^\s*timeout\s*=\s*(\d+)\s*$", block.group(0), re.MULTILINE)
    assert match, "could not find timeout in helm_release.kube_agents"
    return int(match.group(1))


def _helm_retag_timeout_seconds():
    """--wait --timeout in helm_retag() in upgrade.sh, paid by operator and harness modes."""
    text = _UPGRADE_SCRIPT.read_text()
    block = re.search(r"helm_retag\(\)\s*\{.*?\n\s*\}", text, re.DOTALL)
    assert block, "could not find helm_retag() in upgrade.sh"
    match = re.search(r"--wait\s+--timeout\s+(\d+)m\b", block.group(0))
    assert match, "could not find --wait --timeout <N>m in helm_retag()"
    return int(match.group(1)) * 60


def _rollback_constant_seconds(name):
    """A readonly timeout constant (in seconds) in scripts/release/rollback_environment.sh."""
    rollback = (_ROOT / "scripts" / "release" / "rollback_environment.sh").read_text()
    match = re.search(rf"^readonly {re.escape(name)}=(\d+)$", rollback, re.MULTILINE)
    assert match, f"could not find readonly {name} in rollback_environment.sh"
    return int(match.group(1))


_UPGRADE_MODES = frozenset({"operator", "harness", "full"})


def _post_upgrade_waits_by_mode():
    """Every serial wait upgrade.sh's "5. Post-Upgrade Health Verification"
    block pays, bucketed by the --upgrade-mode that reaches it.

    Read off the script rather than listed here: each `kubectl rollout status
    ... --timeout=` and `confirm_agent_image.sh` call is attributed to the
    modes named by the enclosing `if` guards, and a guard that names no mode
    (only `kubectl get <object>`, which exists on a running install) bills
    every mode. `restarted_agent` is the operator arm's own flag, so a wait
    behind it bills the operator mode. Returns {mode: [(label, seconds), ...]}.

    Fails closed. The parser models exactly `if ...; then` / `fi` and a gate
    spelled `kubectl rollout status "<target>" ... --timeout=`; a shape it
    does not model (`elif`, `else`, `case`, a negated mode test, an unquoted
    target, `--timeout 120s`, `kubectl wait`) raises naming the line rather
    than billing the wait to the wrong modes or to none, since the floor this
    feeds exists to catch exactly a wait that is paid but not counted.
    """
    text = _UPGRADE_SCRIPT.read_text()
    block = re.search(
        r'print_step "5\. Post-Upgrade Health Verification".*?print_success "Upgraded deployments verified healthy\."',
        text,
        re.DOTALL,
    )
    assert block, "could not find the step-5 post-upgrade block in upgrade.sh"
    confirm_image = _confirm_agent_image_timeout_seconds()
    sandbox_gate = _sandbox_rollout_timeout_seconds()

    # Join continued lines so a multi-line command or guard is read as one:
    # `\`-continued, and bash's own continuation after a trailing `||`, `&&`
    # or `|` (the shape upgrade.sh L1973-1974 writes its `if` in).
    joined = re.sub(r"(\\|\|\||&&|\|)\n\s*", lambda m: ("" if m.group(1) == "\\" else m.group(1)) + " ", block.group(0))
    lines = joined.splitlines()
    waits = {mode: [] for mode in _UPGRADE_MODES}
    guards = []
    gate_re = re.compile(r'kubectl rollout status "(?P<target>[^"]+)"[^&|;]*?--timeout="?(?P<timeout>[^"\s]+)"?')
    mode_test_re = re.compile(r'\[\[?\s*"\$PARAM_UPGRADE_MODE"\s*(?P<op>==?|!=)\s*"(?P<mode>\w+)"\s*\]?\]')
    for raw in lines:
        line = raw.strip()
        if line.startswith("#"):
            continue
        unmodelled = re.match(r"(elif|else|case|while|until|for)\b", line)
        assert not unmodelled, (
            f"upgrade.sh step-5 block uses `{unmodelled.group(1)}`, which "
            f"_post_upgrade_waits_by_mode does not model; extend the parser: {line!r}"
        )
        # Every wait on the line, counted before the line is classified, so a
        # guard that waits (`if ! kubectl rollout status ...`) or a wait with
        # no --timeout (bounded only by progressDeadlineSeconds) is refused
        # rather than consumed by a branch that never looks for it.
        waits_here = line.count("kubectl rollout status") + line.count("kubectl wait")
        assert line.count("--timeout") <= waits_here, (
            f"a --timeout outside a kubectl rollout status/wait in upgrade.sh's step-5 block: {line!r}"
        )
        if line.startswith("if "):
            assert waits_here == 0, f"a guard that waits is not modelled: {line!r}"
            assert "PARAM_UPGRADE_MODE" not in line or not re.search(r"!\s*\[", line), (
                f"negated --upgrade-mode test is not modelled: {line!r}"
            )
            tests = list(mode_test_re.finditer(line))
            assert len(tests) == line.count("PARAM_UPGRADE_MODE"), (
                f"an --upgrade-mode test in a spelling the parser does not read: {line!r}"
            )
            negated = [t.group(0) for t in tests if t.group("op") == "!="]
            assert not negated, f"negated --upgrade-mode test is not modelled: {negated} in {line!r}"
            # Modes are narrowed only when every `||` alternative is a mode
            # test or restarted_agent; an alternative on anything else lets
            # every mode through, so such a guard is refused, not guessed at.
            body = line[len("if ") :].split("; then")[0]
            if tests:
                for clause in re.split(r"\|\|", body):
                    assert "PARAM_UPGRADE_MODE" in clause or "restarted_agent" in clause, (
                        f"a `||` alternative that is neither an --upgrade-mode test nor "
                        f"restarted_agent widens the modes this guard admits: {clause.strip()!r} in {line!r}"
                    )
            guards.append(line)
            continue
        if line == "fi":
            assert guards, "unbalanced fi in upgrade.sh step-5 block"
            guards.pop()
            continue

        gates = list(gate_re.finditer(line))
        confirm = "confirm_agent_image.sh" in line
        assert len(gates) == waits_here, (
            f"{waits_here} wait(s) on a line of upgrade.sh's step-5 block but {len(gates)} in the shape "
            f"_post_upgrade_waits_by_mode reads (quoted `kubectl rollout status` with `--timeout=`): {line!r}"
        )
        if not gates and not confirm:
            continue

        modes = set(_UPGRADE_MODES)
        for guard in guards:
            named = {t.group("mode") for t in mode_test_re.finditer(guard)}
            if "restarted_agent" in guard:
                named.add("operator")
            if named:
                modes &= named
        if confirm:
            for mode in modes:
                waits[mode].append(("confirm_agent_image.sh", confirm_image))
        for gate in gates:
            timeout = gate.group("timeout")
            if timeout == "$SANDBOX_ROLLOUT_TIMEOUT":
                seconds = sandbox_gate
            else:
                numeric = re.fullmatch(r"(\d+)s", timeout)
                assert numeric, f"unrecognised --timeout {timeout!r} in upgrade.sh step-5 block"
                seconds = int(numeric.group(1))
            for mode in modes:
                waits[mode].append((gate.group("target"), seconds))
    assert not guards, "unbalanced if in upgrade.sh step-5 block"
    return waits


class SiblingGatewayRolloutGatesTest(unittest.TestCase):
    """Other callers that wait on a cold gateway rollout or run wait_for_gke_readiness.sh."""

    def setUp(self):
        self.startup = _gateway_startup_budget_seconds()
        self.deadline = _gateway_progress_deadline_seconds()

    def test_gitops_pilot_gateway_gates_cover_startup_and_stay_under_deadline(self):
        pilot = (_ROOT / "bench" / "hack" / "run-gitops-pilot.sh").read_text()
        for name in ("CR_READY_TIMEOUT", "GATEWAY_ROLLOUT_TIMEOUT"):
            with self.subTest(constant=name):
                match = re.search(rf"^readonly {name}=(\d+)s$", pilot, re.MULTILINE)
                self.assertIsNotNone(match, f"could not find readonly {name} in run-gitops-pilot.sh")
                seconds = int(match.group(1))
                self.assertGreaterEqual(seconds, self.startup + _PULL_ALLOWANCE_SECONDS)
                self.assertLess(seconds, self.deadline)

    def test_e2e_gateway_rollout_defaults_cover_startup_and_stay_under_deadline(self):
        gchat = (_ROOT / "tests" / "e2e" / "gchat_agent_test.py").read_text()
        gchat_match = re.search(
            r'GATEWAY_ROLLOUT_TIMEOUT_SEC:\s*int\s*=\s*int\(os\.environ\.get\("GATEWAY_ROLLOUT_TIMEOUT_SEC",\s*"(\d+)"\)\)',
            gchat,
        )
        self.assertIsNotNone(gchat_match)
        gchat_sec = int(gchat_match.group(1))
        self.assertGreaterEqual(gchat_sec, self.startup + _PULL_ALLOWANCE_SECONDS)
        self.assertLess(gchat_sec, self.deadline)

        plugins = (_ROOT / "tests" / "e2e" / "operator" / "agentplugins_e2e_test.py").read_text()
        plugins_match = re.search(
            r"^DEFAULT_ROLLOUT_TIMEOUT_SEC:\s*int\s*=\s*(\d+)$", plugins, re.MULTILINE
        )
        self.assertIsNotNone(plugins_match)
        plugins_sec = int(plugins_match.group(1))
        self.assertGreaterEqual(plugins_sec, self.startup + _PULL_ALLOWANCE_SECONDS)
        self.assertLess(plugins_sec, self.deadline)

    def test_e2e_workflow_timeouts_cover_readiness_gates_and_suites(self):
        gateway_gate = _readiness_gate_seconds(
            "platform-agent-gateway", "GATEWAY_READINESS_TIMEOUT"
        )
        litellm_gate = _readiness_gate_seconds("litellm", "LITELLM_READINESS_TIMEOUT")
        gates_paid_twice = 2 * (gateway_gate + litellm_gate)
        confirm_image = _confirm_agent_image_timeout_seconds()

        e2e_cfg = yaml.safe_load((_ROOT / "tests" / "e2e" / "e2e_config.yaml").read_text())
        suite_files = {s["name"]: set(s.get("tests", ())) for s in e2e_cfg["suites"]}
        all_e2e_files = set().union(*suite_files.values())

        workflows_dir = _ROOT / ".github" / "workflows"
        e2e_run_doc = yaml.safe_load((workflows_dir / "e2e-run.yml").read_text())
        default_minutes = e2e_run_doc[True]["workflow_call"]["inputs"]["timeout_minutes"].get(
            "default"
        )
        self.assertIsNotNone(default_minutes, "e2e-run.yml has no default timeout_minutes")

        e2e_callers = []
        for wf_path in sorted(workflows_dir.glob("*.yml")):
            doc = yaml.safe_load(wf_path.read_text())
            for job_id, job in (doc.get("jobs") or {}).items():
                if job.get("uses") == "./.github/workflows/e2e-run.yml":
                    with_args = job.get("with") or {}
                    minutes = with_args.get("timeout_minutes", default_minutes)
                    suites = [with_args.get("blocking_suite", "")]
                    suites.extend(
                        s.strip()
                        for s in (with_args.get("optional_suites") or "").split(",")
                        if s.strip()
                    )
                    files = set().union(*(suite_files.get(s, set()) for s in suites))
                    suite_allowance = (
                        _E2E_ALL_SUITES_ALLOWANCE_SECONDS
                        if files == all_e2e_files or "stockout-full" in suites
                        else _E2E_RC_SUITE_ALLOWANCE_SECONDS
                    )
                    # `next` adds its gate once, after the two above; `today`
                    # (the input's default) adds nothing. Anything else -- an
                    # expression forwarding a caller's own input -- may be
                    # `next` at run time, so it is budgeted as `next`.
                    mode_gate = (
                        0
                        if with_args.get("mode", "today") == "today"
                        else _mode_next_gate_seconds()
                    )
                    required = (
                        gates_paid_twice
                        + confirm_image
                        + _E2E_RUNNER_SETUP_SECONDS
                        + suite_allowance
                        + mode_gate
                    )
                    e2e_callers.append((f"{wf_path.name}:{job_id}", int(minutes), required))

        self.assertGreaterEqual(
            len(e2e_callers), 2, "expected at least rc and nightly callers of e2e-run.yml"
        )

        manual_doc = yaml.safe_load((workflows_dir / "e2e-manual-runner.yml").read_text())
        manual_minutes = manual_doc["jobs"]["run-e2e"].get("timeout-minutes")
        self.assertIsNotNone(manual_minutes, "e2e-manual-runner.yml has no job timeout")
        e2e_callers.append(
            (
                "e2e-manual-runner.yml:run-e2e",
                int(manual_minutes),
                gates_paid_twice + _E2E_RUNNER_SETUP_SECONDS + _E2E_ALL_SUITES_ALLOWANCE_SECONDS,
            )
        )

        for label, minutes, required_seconds in e2e_callers:
            with self.subTest(caller=label):
                job_seconds = minutes * 60
                self.assertGreaterEqual(
                    job_seconds,
                    required_seconds,
                    f"{label} timeout ({minutes}m = {job_seconds}s) is below the "
                    f"modelled readiness gates + setup + suite floor ({required_seconds}s)",
                )

    def test_the_step_5_parser_sees_every_mode_gate(self):
        # Guards the parser the floor below rests on: the sandbox waits are
        # guarded only on the object existing, so every mode pays them; the
        # gateway gate reaches the operator mode through restarted_agent; and
        # confirm_agent_image.sh is a harness/full wait only.
        waits = _post_upgrade_waits_by_mode()
        sandbox_gate = _sandbox_rollout_timeout_seconds()
        gateway_gate = _rollout_gate_seconds(_UPGRADE_SCRIPT, "platform-agent-gateway")
        for mode in sorted(_UPGRADE_MODES):
            with self.subTest(mode=mode):
                seconds = [s for _, s in waits[mode]]
                self.assertEqual(seconds.count(sandbox_gate), 2, f"{mode}: {waits[mode]}")
                self.assertIn(gateway_gate, seconds, f"{mode}: {waits[mode]}")
        labels = {mode: [label for label, _ in waits[mode]] for mode in _UPGRADE_MODES}
        self.assertNotIn("confirm_agent_image.sh", labels["operator"])
        self.assertIn("confirm_agent_image.sh", labels["harness"])
        self.assertIn("confirm_agent_image.sh", labels["full"])

    def test_the_step_5_parser_refuses_shapes_it_does_not_model(self):
        # The parser re-implements a slice of bash, and every input its grammar
        # admits without handling lands as a wait that is paid but not counted.
        # So each such shape has to raise naming the line, not pass quietly.
        original = _UPGRADE_SCRIPT.read_text()
        gateway = 'kubectl rollout status "deployment/${PLATFORM_AGENT_DEPLOYMENT}" -n "$target_namespace" --timeout=1500s'
        statefulset_guard = '  if kubectl get statefulset "$PLATFORM_AGENT_SHELL_STATEFULSET"'
        operator_guard = '  if [ "$PARAM_UPGRADE_MODE" = "operator" ] || [ "$PARAM_UPGRADE_MODE" = "full" ]; then'
        self.assertIn(gateway, original)
        self.assertIn(statefulset_guard, original)
        self.assertIn(operator_guard, original)
        mutations = {
            "elif": (statefulset_guard, '  elif [ "$PARAM_UPGRADE_MODE" = "operator" ]; then\n    true\n  fi\n' + statefulset_guard),
            "else": ('    kubectl rollout status "statefulset/', '    true\n  else\n    kubectl rollout status "statefulset/'),
            "case": ('  print_success "Upgraded deployments verified healthy."', '  case "$PARAM_UPGRADE_MODE" in operator) true;; esac\n  print_success "Upgraded deployments verified healthy."'),
            "negated mode test": (operator_guard, '  if ! [ "$PARAM_UPGRADE_MODE" = "operator" ]; then'),
            "!= mode test": (operator_guard, '  if [ "$PARAM_UPGRADE_MODE" != "harness" ]; then'),
            "[[ ]] mode test": (operator_guard, '  if [[ "$PARAM_UPGRADE_MODE" == "operator" || "$PARAM_UPGRADE_MODE" == "full" ]]; then'),
            "|| alternative on another flag": (operator_guard, '  if [ "$PARAM_UPGRADE_MODE" = "operator" ] || [ "$FORCE_CONTROLLER_WAIT" = "true" ]; then'),
            "unquoted target": (gateway, gateway.replace('"deployment/${PLATFORM_AGENT_DEPLOYMENT}"', "deployment/${PLATFORM_AGENT_DEPLOYMENT}")),
            "--timeout space form": (gateway, gateway.replace("--timeout=1500s", "--timeout 1500s")),
            "no --timeout at all": (gateway, gateway.replace(" --timeout=1500s", "")),
            "a guard that waits": (statefulset_guard, '  if ! kubectl rollout status "deployment/x" -n "$target_namespace" --timeout=300s; then\n    true\n  fi\n' + statefulset_guard),
            "kubectl wait": (gateway, gateway + '\n    kubectl wait --for=condition=Ready pod -l app=x -n "$target_namespace" --timeout=600s'),
        }
        with tempfile.TemporaryDirectory() as tmp:
            mutated = pathlib.Path(tmp) / "upgrade.sh"
            for shape, (old, new) in mutations.items():
                with self.subTest(shape=shape):
                    text = original.replace(old, new, 1)
                    self.assertNotEqual(text, original, f"mutation {shape} did not apply")
                    mutated.write_text(text)
                    with mock.patch.object(sys.modules[__name__], "_UPGRADE_SCRIPT", mutated):
                        with self.assertRaises(AssertionError, msg=f"{shape} was parsed silently"):
                            _post_upgrade_waits_by_mode()

            # And the one continuation bash allows that is not a `\`: a guard
            # split after a trailing `||` (upgrade.sh L1973-1974's own shape)
            # has to be read whole, so the gate under it bills both modes.
            with self.subTest(shape="guard continued after ||"):
                split_guard = operator_guard.replace(" || ", " ||\n    ")
                text = original.replace(operator_guard, split_guard, 1)
                self.assertNotEqual(text, original)
                mutated.write_text(text)
                with mock.patch.object(sys.modules[__name__], "_UPGRADE_SCRIPT", mutated):
                    waits = _post_upgrade_waits_by_mode()
                controller = "deployment/${KUBE_AGENTS_OPERATOR_DEPLOYMENT}"
                self.assertIn(controller, [label for label, _ in waits["operator"]])
                self.assertIn(controller, [label for label, _ in waits["full"]])
                self.assertNotIn(controller, [label for label, _ in waits["harness"]])

            # Two gates on one line are two waits: both have to be attributed,
            # not the first one only.
            with self.subTest(shape="two gates on one line"):
                second = gateway.replace("deployment/${PLATFORM_AGENT_DEPLOYMENT}", "deployment/second").replace("1500s", "120s")
                text = original.replace(gateway, gateway + " && " + second, 1)
                self.assertNotEqual(text, original)
                mutated.write_text(text)
                with mock.patch.object(sys.modules[__name__], "_UPGRADE_SCRIPT", mutated):
                    waits = _post_upgrade_waits_by_mode()
                self.assertIn(("deployment/second", 120), waits["harness"])

    def test_upgrade_workflow_timeouts_cover_serial_rollout_gates(self):
        # Each --upgrade-mode's serial waits are the Helm wait its arm pays
        # (helm_retag for operator/harness, the Terraform helm_release timeout
        # for full) plus whatever upgrade.sh's step-5 block gates in that mode,
        # read off the script by _post_upgrade_waits_by_mode.
        helm_wait = _terraform_helm_timeout_seconds()
        helm_retag = _helm_retag_timeout_seconds()
        step_5 = {mode: sum(s for _, s in gates) for mode, gates in _post_upgrade_waits_by_mode().items()}
        rollback_ready = _rollback_constant_seconds("READY_TIMEOUT_SECONDS")
        rollback_operator_scale = _rollback_constant_seconds("OPERATOR_SCALE_TIMEOUT_SECONDS")
        rollback_image_confirm = _rollback_constant_seconds("IMAGE_CONFIRM_TIMEOUT_SECONDS")

        full_upgrade_waits = helm_wait + step_5["full"]
        operator_move_waits = helm_retag + step_5["operator"]
        harness_move_waits = helm_retag + step_5["harness"]
        # rollback_environment.sh: two operator + two harness moves, the
        # litellm-policy handoff's scale-down and restart gates, and two
        # assert_running checks (two image confirmations + rollout + Ready).
        assert_running_waits = 2 * rollback_image_confirm + 2 * rollback_ready
        rollback_leg_waits = (
            2 * operator_move_waits
            + 2 * harness_move_waits
            + 2 * rollback_operator_scale
            + 2 * assert_running_waits
        )

        workflows_dir = _ROOT / ".github" / "workflows"
        reconcile_doc = yaml.safe_load((workflows_dir / "reconcile-environment.yml").read_text())
        reconcile_sec = int(reconcile_doc["jobs"]["reconcile"]["timeout-minutes"]) * 60
        reconcile_floor = _UPGRADE_WORKFLOW_SETUP_SECONDS + full_upgrade_waits
        self.assertGreaterEqual(
            reconcile_sec,
            reconcile_floor,
            f"reconcile-environment.yml ({reconcile_sec}s) is below its setup allowance "
            f"({_UPGRADE_WORKFLOW_SETUP_SECONDS}s) plus upgrade.sh --upgrade-mode=full's "
            f"{full_upgrade_waits}s of serial waits",
        )

        promo_doc = yaml.safe_load((workflows_dir / "staging-promotion-pipeline.yml").read_text())
        rollback_sec = int(promo_doc["jobs"]["step-3b-rollback-leg"]["timeout-minutes"]) * 60
        rollback_floor = _ROLLBACK_LEG_SETUP_SECONDS + rollback_leg_waits
        self.assertGreaterEqual(
            rollback_sec,
            rollback_floor,
            f"step-3b-rollback-leg ({rollback_sec}s) is below its setup allowance "
            f"({_ROLLBACK_LEG_SETUP_SECONDS}s) plus rollback_environment.sh's {rollback_leg_waits}s "
            f"of serial waits (operator move {operator_move_waits}s, harness move {harness_move_waits}s)",
        )


if __name__ == "__main__":
    unittest.main()
