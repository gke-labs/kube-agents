"""Stage 3 E2E Promotion Test: GKE Stockout Ingress Smoke & Comprehensive Scenarios Suite."""

import json
import os
import pathlib
import subprocess
import time
import warnings
from typing import Any, Dict, List, Optional, Tuple

import pytest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_PLUGIN_DIR = _REPO_ROOT / "agentplugins" / "gke-stockout-investigator"
_SCENARIOS_DIR = _PLUGIN_DIR / "scenarios"
_INSTALL_SCRIPT = _PLUGIN_DIR / "install.sh"
_CLEAN_KANBAN_SCRIPT = _SCENARIOS_DIR / "lib" / "clean_stale_kanban_tasks.py"

# AgentPlugin object name, Helm release, and Hermes plugin module — one identifier, fixed
# as RELEASE in install.sh because the CRD's name pattern is ^[a-z][a-z0-9]*$.
_PLUGIN_NAME = "gkestockoutinvestigator"
# The skill directory inside the plugin image (agentplugins/.../files/skills/<name>).
_PLUGIN_SKILL_NAME = "gke-stockout-investigator"
# install.sh's own default for the PlatformAgent it attaches to. Mirrored, not read off
# the live CR: the fixture has to name the same agent install.sh will, and install.sh
# takes it from AGENT_REF or this default without consulting the cluster.
_DEFAULT_AGENT_REF = "platform-agent"

# Everything this fixture does has to finish inside the E2E job's `timeout-minutes`
# (.github/workflows/e2e-run.yml, whose default is what the RC pipeline gets), which also
# has to cover runner setup, the other e2e modules, and scenario 04's own 360s watch. A wait that outlives the job
# is worse than no wait: GitHub cancels the runner and pytest never prints the message
# this fixture exists to print, so the diagnosis is lost in exactly the case it was
# written for. Each wait below is clamped to whatever is left of this budget, so the
# fixture always fails inside the job rather than being killed alongside it.
#
# 600s leaves the 30-minute step its existing margin: the suite ran 544s end to end in
# run 32866087154, so a fixture that spends its whole budget still lands inside 20
# minutes. Sum of the individual caps exceeds it deliberately — each is the honest
# ceiling for its own step, and the budget is what binds when several go long at once.
_FIXTURE_BUDGET_SECONDS = 600
# Only reachable when the AgentPlugin is absent, which on the RC means the environment was
# never provisioned rather than that the plugin drifted. Bounded because install.sh builds
# and pushes an image on that path: before this, a hung gcloud or docker burned the whole
# job timeout with no output. The fixture budget above binds first, and that is the
# intended ceiling — a plugin missing outright is a finding, not something to wait out.
_INSTALL_TIMEOUT_SECONDS = 600
# How long the gateway's generation has to hold still before its spec counts as settled.
#
# Step 2 provisions the environment immediately before this suite runs, and a `helm
# upgrade` there reaches the gateway by two routes — the chart's own render and the
# config hash that rides on the pod template as kubeagents.x-k8s.io/config-hash
# (platformagent_manifests.go:1987). Observing the first bump and calling `rollout
# status` can therefore succeed against an intermediate revision moments before the
# second arrives.
#
# 20s and the reasoning are taken from the sibling suite that hit this first:
# agentplugins/pubsub-platform/tests/dedup_e2e_test.py:125-131.
_GENERATION_STABLE_SECONDS = 20
# Not the 900s cold-boot budget that .github/workflows/reusable-deploy-agent.yml:150-157
# derives from agentAPIProbe(10, 60) plus an image-pull allowance. That figure is for a
# gateway being started; by the time this suite runs, step 2 has already provisioned the
# environment and `scripts/release/wait_for_gke_readiness.sh` has waited for the pod. What
# is left for this wait is a re-template still in flight, so the ceiling is sized for a
# roll on a warm node rather than a boot from nothing.
_ROLLOUT_TIMEOUT_SECONDS = 300
_PLUGIN_READY_TIMEOUT_SECONDS = 300
# Polled rather than read once, because rollout-complete does not always mean the entrypoint
# has finished linking plugins.
#
# On the RC's single-replica gateway it does. platform-agent carries
# ReadinessProbe: agentAPIProbe(15, 3) (platformagent_manifests.go:2927, added by #674), and
# deploy/shared/profile_plugins.py runs at entrypoint step 2.65 before `exec "$@"`, so a pod
# that answers the probe has already linked them.
#
# Under leader election it does not. replicas > 1 sets ENABLE_LEADER_ELECTION (:1848-1853),
# and agentAPIProbe then exits 0 on connection-refused (:2773) so a standby can report Ready
# without serving — which means Ready no longer implies the entrypoint reached `exec`. The
# window is for that configuration.
#
# Older comments here and at tests/e2e/operator/agentplugins_e2e_test.py:1212-1226 say the
# platform-agent container has no readiness probe at all. That was true before #674 and is
# not now; the sibling still needs correcting.
_SKILL_MOUNT_TIMEOUT_SECONDS = 120

# What _kubectl reports for a call that never answered. 124 is what `timeout(1)` uses, and
# no kubectl exit code collides with it; callers that must not read a timeout as "absent"
# either test for it or pass fail_on_timeout.
_KUBECTL_TIMEOUT_RC = 124

_AGENT_AVAILABILITY_TIMEOUT_SECONDS = 180
_AGENT_POLL_INTERVAL_SECONDS = 5
_DEFAULT_ROUTE_NAME = "gke_stockout_alerts"
_SMOKE_VERIFY_TIMEOUT_SECONDS = 300
# Headroom covers preflight checks (bounded by AGENT_AVAILABILITY_TIMEOUT=180 plus SKILL_MOUNT_TIMEOUT=30 = 210s max) and cleanup.
_SCENARIO_RUN_HEADROOM_SECONDS = 300


def _as_text(stream: Any) -> str:
    """Renders captured output as text.

    TimeoutExpired carries whatever had been read when the timer fired, and on POSIX that
    is bytes even for a text-mode run — printed raw it becomes a b'...' blob with the
    newlines escaped, which is the one moment the output is being read closely.
    """
    if stream is None:
        return ""
    if isinstance(stream, bytes):
        return stream.decode("utf-8", "replace")
    return str(stream)


def _kubectl(
    *args: str, timeout: int = 30, fail_on_timeout: bool = False
) -> subprocess.CompletedProcess:
    """Run kubectl, turning a timeout into a failed result rather than an exception.

    Every polling caller below is inside a fixture whose value is the message it prints. A
    raised TimeoutExpired replaces that message with a traceback, and the call most likely
    to be slow is an exec against a pod that is rolling — precisely the moment being
    diagnosed. Those callers retry, so a timed-out attempt costs one iteration.

    `fail_on_timeout` is for the callers that read the result as ground truth about cluster
    state. There a returned failure is worse than an exception: `_gateway_workload` would
    read it as "no such workload" and assert that the agent has no gateway, and the
    pre-install snapshot would read it as "the object does not exist" and skip the
    generation watch. Both are false statements about the cluster derived from a slow API
    server, and both are less useful than saying kubectl did not answer.
    """
    try:
        return subprocess.run(
            ["kubectl", *args], capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired as exc:
        rendered = " ".join(args)
        if fail_on_timeout:
            pytest.fail(
                f"kubectl did not answer within {timeout}s, and its result is read as cluster "
                f"state, not retried: kubectl {rendered}\n"
                f"STDOUT:\n{_as_text(exc.stdout)}"
            )
        return subprocess.CompletedProcess(
            args=["kubectl", *args],
            returncode=_KUBECTL_TIMEOUT_RC,
            stdout=_as_text(exc.stdout),
            stderr=f"kubectl did not answer within {timeout}s: kubectl {rendered}",
        )


def _remaining(deadline: float, cap: int) -> Tuple[int, str]:
    """How long a wait gets, and which of the two ceilings decided it.

    Returns (seconds, "its own ceiling" | "the fixture's budget"). A wait cut short by the
    budget and one that ran its full course produce the same expiry, so reporting a 4s
    skill-mount window without saying which sends the reader after a product fault that is
    really a fixture running late.
    """
    left = int(deadline - time.time())
    if left < cap:
        return max(0, left), "the fixture's budget"
    return cap, "its own ceiling"


def _gateway_workload(agent_ref: str, namespace: str) -> Tuple[Optional[str], str]:
    """Resolves the gateway workload's kind and name.

    The operator renders the gateway as either a Deployment or a StatefulSet depending on
    spec.storage/replicas (reconcileWorkload in platformagent_controller.go), and deletes
    whichever one it is not. Both names are '<agentRef>-gateway'.
    """
    name = f"{agent_ref}-gateway"
    for kind in ("deployment", "statefulset"):
        # fail_on_timeout: "absent" is the conclusion drawn from a non-zero exit here, and
        # a slow API server must not produce it.
        if _kubectl("get", kind, name, "-n", namespace, fail_on_timeout=True).returncode == 0:
            return kind, name
    return None, name


def _generation(
    kind: str, name: str, namespace: str, fail_on_timeout: bool = False
) -> Optional[int]:
    """.metadata.generation of an object, or None if it does not exist.

    Generation bumps only when the spec changes, which is what makes it the signal for
    "this reinstall actually altered something" as opposed to "the object is still there".

    Pass fail_on_timeout for the readings that are compared against later — a snapshot that
    degraded to None because kubectl was slow silently disables the comparison it exists
    for. Inside a polling loop, leave it off and let the next iteration retry.
    """
    res = _kubectl(
        "get", kind, name, "-n", namespace, "-o", "jsonpath={.metadata.generation}",
        fail_on_timeout=fail_on_timeout,
    )
    if res.returncode != 0:
        return None
    try:
        return int(res.stdout.strip())
    except ValueError:
        return None


def _wait_for_gateway_rollout(
    agent_ref: str, namespace: str, deadline: float
) -> Tuple[str, str]:
    """Blocks until the gateway has finished rolling and is serving its current spec.

    Step 2 of the RC pipeline provisions the environment immediately before this suite
    runs, and the gateway it leaves behind may still be rolling: `helm upgrade` moves the
    pod template, and the operator re-templates the workload again when it reconciles the
    AgentPlugin. A test that probes the pod while that is in flight talks to a terminating
    one, which is how a smoke test and a scenario in the same run end up inspecting two
    different ReplicaSets — and how run 32866087154 came to report a plugin skill missing
    that the next run found present.

    The generation is required to hold still before the rollout is watched, because the
    first bump is not always the last; see _GENERATION_STABLE_SECONDS.

    Returns (kind/name, one line recording where the generation settled).
    """
    kind, name = _gateway_workload(agent_ref, namespace)
    if kind is None:
        pytest.fail(
            f"No Deployment or StatefulSet '{name}' in namespace '{namespace}'; the "
            f"PlatformAgent '{agent_ref}' the stockout plugin attaches to has no gateway "
            "workload."
        )
    target = f"{kind}/{name}"

    current = _generation(kind, name, namespace)
    if current is None:
        outcome = f"{target}: generation unreadable, waiting on the rollout alone"
    else:
        settled = _wait_for_generation_to_settle(kind, name, namespace, current, deadline)
        outcome = f"{target}: generation settled at {settled}"

    rollout_budget, rollout_bound = _remaining(deadline, _ROLLOUT_TIMEOUT_SECONDS)
    if rollout_budget <= 0:
        pytest.fail(
            f"The fixture's {_FIXTURE_BUDGET_SECONDS}s budget was spent before {target} could be "
            f"watched for a rollout. {outcome}"
        )
    res = _kubectl(
        "rollout", "status", target, "-n", namespace, f"--timeout={rollout_budget}s",
        timeout=rollout_budget + 30,
    )
    if res.returncode != 0:
        pytest.fail(
            f"The gateway {target} did not finish rolling out within {rollout_budget}s "
            f"({rollout_bound}). {outcome}\n"
            f"STDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}"
        )
    return target, outcome


def _wait_for_generation_to_settle(
    kind: str, name: str, namespace: str, gen: int, deadline: float
) -> int:
    """Waits for the workload's generation to stop moving, and returns where it stopped.

    The first bump is not the last one: see _GENERATION_STABLE_SECONDS. Returns the current
    generation when the budget runs out, without failing — an unsettled generation is
    handled by the rollout wait that follows, which is the check that can see it.
    """
    stable_for, _bound = _remaining(deadline, _GENERATION_STABLE_SECONDS)
    last_change = time.time()
    while time.time() - last_change < stable_for:
        if time.time() >= deadline:
            return gen
        time.sleep(3)
        current = _generation(kind, name, namespace)
        if current is not None and current != gen:
            gen = current
            last_change = time.time()
    return gen


def _wait_for_plugin_ready(namespace: str, budget_deadline: float) -> Dict[str, Any]:
    """Blocks until the AgentPlugin's status reflects its current spec.

    Phase alone is not enough. A Ready left over from a previous reconcile is exactly what
    a stale plugin looks like, so observedGeneration has to have caught up to the object's
    generation before the phase means anything.

    Runs before the rollout wait rather than after. The operator writes this status at
    platformagent_controller.go:485 and the workload at :514 inside one reconcile, so
    waiting here first means the rollout wait that follows is looking at a workload the
    operator has already written, not one it is about to.
    """
    window, bound = _remaining(budget_deadline, _PLUGIN_READY_TIMEOUT_SECONDS)
    deadline = time.time() + window
    detail = "the AgentPlugin was never read"
    while True:
        res = _kubectl("get", "agentplugins", _PLUGIN_NAME, "-n", namespace, "-o", "json")
        if res.returncode == 0:
            try:
                obj = json.loads(res.stdout)
            except json.JSONDecodeError as exc:
                detail = f"could not parse the AgentPlugin JSON: {exc}"
                obj = None
            if obj is not None:
                generation = obj.get("metadata", {}).get("generation")
                status = obj.get("status", {})
                phase = status.get("phase")
                observed = status.get("observedGeneration")
                if phase == "Ready" and observed == generation:
                    return obj
                detail = (
                    f"phase={phase!r}, observedGeneration={observed}, generation={generation}"
                )
        else:
            detail = res.stderr.strip() or f"kubectl exited {res.returncode}"
        if time.time() >= deadline:
            break
        time.sleep(5)
    pytest.fail(
        f"AgentPlugin '{_PLUGIN_NAME}' in '{namespace}' did not become Ready for its current "
        f"spec within {window}s ({bound}): {detail}"
    )


def _current_revision_selector(kind: str, name: str, namespace: str) -> Optional[str]:
    """A label selector pinning the workload's current pod revision, if one is readable.

    deletionTimestamp and the Ready condition alone do not identify the pod the tests will
    use: during a roll the outgoing pod is Running, Ready and not yet marked for deletion,
    so a probe can pass against the state the install just replaced. The revision label is
    what distinguishes them — pod-template-hash off the current ReplicaSet for a Deployment,
    controller-revision-hash off .status.updateRevision for a StatefulSet.

    Recomputed on every attempt by the caller, not pinned once, for the reason
    tests/e2e/operator/agentplugins_e2e_test.py:1164-1180 gives: the answer moves while a
    roll is in progress, and a hash pinned before it settles can never match.
    """
    if kind == "statefulset":
        res = _kubectl(
            "get", "statefulset", name, "-n", namespace,
            "-o", "jsonpath={.status.updateRevision}",
        )
        rev = res.stdout.strip() if res.returncode == 0 else ""
        return f"controller-revision-hash={rev}" if rev else None

    # Matched on deployment.kubernetes.io/revision, not on "newest ReplicaSet reporting
    # replicas > 0". The latter is wrong precisely when a candidate reverts a plugin or
    # tuning change: a rollback scales up an OLDER ReplicaSet, so the newest one with
    # replicas is the one being drained, and pinning it makes the probe wait out its
    # window against a dying pod on a cluster that is perfectly healthy. The Deployment's
    # own revision annotation is the discriminator that survives that.
    dep = _kubectl(
        "get", "deployment", name, "-n", namespace,
        "-o", r"jsonpath={.metadata.annotations.deployment\.kubernetes\.io/revision}",
    )
    revision = dep.stdout.strip() if dep.returncode == 0 else ""
    if not revision:
        return None
    res = _kubectl(
        "get", "rs", "-n", namespace,
        "-l", f"app={name}",
        "-o", "json",
    )
    if res.returncode != 0:
        return None
    try:
        items = json.loads(res.stdout).get("items", [])
    except json.JSONDecodeError:
        return None
    for rs in items:
        meta = rs.get("metadata", {})
        if meta.get("annotations", {}).get("deployment.kubernetes.io/revision") != revision:
            continue
        pod_hash = meta.get("labels", {}).get("pod-template-hash")
        if pod_hash:
            return f"pod-template-hash={pod_hash}"
    return None


def _gateway_pod(agent_ref: str, namespace: str, revision: Optional[str]) -> Tuple[Optional[str], str]:
    """The Ready gateway pod of the current revision, and why there is none when there is not.

    Returns (name, detail). The detail separates the three ways this comes back empty —
    no pod carries the label at all, pods carry it but none is Ready, and pods are Ready
    but none belongs to the revision asked for — because each sends a reader somewhere
    different, and reporting all three as "no pod found" sends them to the labels.
    """
    selector = f"app={agent_ref}-gateway"
    res = _kubectl(
        "get", "pods", "-n", namespace,
        "-l", f"{selector},{revision}" if revision else selector,
        "-o", "json",
    )
    if res.returncode != 0:
        return None, f"kubectl could not list pods: {res.stderr.strip()}"
    try:
        items = json.loads(res.stdout).get("items", [])
    except json.JSONDecodeError:
        return None, "kubectl returned output that is not JSON when listing pods"

    if not items:
        if revision:
            return None, f"no pod matches {selector},{revision} in '{namespace}'"
        return None, f"no pod matches {selector} in '{namespace}'"

    not_ready = []
    for pod in items:
        meta = pod.get("metadata", {})
        pod_name = meta.get("name", "<unnamed>")
        status = pod.get("status", {})
        if meta.get("deletionTimestamp"):
            not_ready.append(f"{pod_name} (terminating)")
            continue
        if status.get("phase") != "Running":
            not_ready.append(f"{pod_name} ({status.get('phase')})")
            continue
        conditions = status.get("conditions", [])
        if any(c.get("type") == "Ready" and c.get("status") == "True" for c in conditions):
            return pod_name, ""
        not_ready.append(f"{pod_name} (Running, not Ready)")
    return None, (
        f"{len(items)} pod(s) match {selector}"
        f"{',' + revision if revision else ''} but none is usable: {', '.join(not_ready)}"
    )


def _agent_home(agent_ref: str, namespace: str) -> str:
    """The agent's home directory as the operator resolved it.

    Not $HERMES_HOME. That variable is exported by deploy/shared/docker-entrypoint.sh:5
    inside the entrypoint's own process tree, and `kubectl exec` starts a new one — it sees
    the image's ENV plus the pod spec's, where the operator sets PLATFORM_AGENT_HOME
    (platformagent_manifests.go:1667-1671) and never HERMES_HOME. Expanding HERMES_HOME in
    an exec therefore always falls through to its default, which is right by accident on a
    default install and wrong on any CR that sets spec.harness.hermes.agentHome.

    Read from the CR rather than from the pod: it is the same field the operator feeds to
    pluginMountPath, so the probe and the mount cannot disagree. The shell default below
    covers a CR that leaves it unset, matching the CRD's own default of /opt/data.
    """
    res = _kubectl(
        "get", "platformagent", agent_ref, "-n", namespace,
        "-o", "jsonpath={.spec.harness.hermes.agentHome}",
        fail_on_timeout=True,
    )
    home = res.stdout.strip() if res.returncode == 0 else ""
    return home or "/opt/data"


def _plugin_target_profile(namespace: str) -> str:
    """Reads targetProfile from the AgentPlugin CR, or empty string if unset."""
    res = _kubectl(
        "get", "agentplugins", _PLUGIN_NAME, "-n", namespace,
        "-o", "jsonpath={.spec.targetProfile}",
    )
    return res.stdout.strip() if res.returncode == 0 else ""


def _plugin_skill_path(
    agent_ref: str,
    namespace: str,
    target_profile: Optional[str] = None,
) -> str:
    """Returns the expected in-pod path to the plugin's SKILL.md.

    A targeted profile is staged outside the PVC at /opt/agent-plugins/<profile>/<plugin>
    and linked to <home>/profiles/<profile>/plugins/<plugin> by the entrypoint; the
    default profile's plugins are mounted at <home>/plugins/<plugin> directly. The link
    is what this probes, so the citation is the linker, not the mount:
    deploy/shared/profile_plugins.py (and pluginMountPath in the operator for the mount).
    """
    if target_profile is None:
        target_profile = _plugin_target_profile(namespace)
    home = _agent_home(agent_ref, namespace)
    if target_profile:
        plugin_root = f"{home}/profiles/{target_profile}/plugins/{_PLUGIN_NAME}"
    else:
        plugin_root = f"{home}/plugins/{_PLUGIN_NAME}"
    return f"{plugin_root}/skills/{_PLUGIN_SKILL_NAME}/SKILL.md"


def _verify_skill_mounted(
    agent_ref: str,
    namespace: str,
    target_profile: str,
    kind: str,
    workload_name: str,
    budget_deadline: float,
) -> str:
    """Fails unless the plugin's SKILL.md is readable inside the current gateway pod.

    A Ready AgentPlugin says the operator reconciled something, not that the skill reached
    the profile that runs the investigation. When it has not, every alert still publishes,
    the adapter logs nothing, and each scenario spends its full 360s watch before failing —
    which is what run 32866087154 reported as "Platform Agent never started investigation".
    The mount is checked here, once, where the message can name the path.

    Called after the rollout wait, so the pod it resolves is the one the tests below will
    use. Returns that pod, so the caller can record which one they inherit.
    """
    skill_path = _plugin_skill_path(agent_ref, namespace, target_profile)
    # Both outcomes print a token and the command exits 0, so "the file is absent" and
    # "the exec did not run" stay distinguishable; only the first is conclusive.
    probe = f'test -f "{skill_path}" && echo PRESENT || echo ABSENT'

    window, bound = _remaining(budget_deadline, _SKILL_MOUNT_TIMEOUT_SECONDS)
    deadline = time.time() + window
    detail = "the gateway pod was never resolved"
    absent_in = ""
    while True:
        revision = _current_revision_selector(kind, workload_name, namespace)
        pod, why = _gateway_pod(agent_ref, namespace, revision)
        if pod:
            res = _kubectl(
                "exec", "-i=false", "-n", namespace, pod, "-c", "platform-agent",
                "--", "sh", "-c", probe,
            )
            if res.returncode != 0:
                detail = f"kubectl exec into {pod} failed: {res.stderr.strip()}"
            elif "PRESENT" in res.stdout:
                return pod
            else:
                # Retried rather than failed on sight, because ABSENT is not always final.
                # A single-replica gateway settles it: platform-agent carries
                # ReadinessProbe agentAPIProbe(15, 3) (platformagent_manifests.go:2927) and
                # the entrypoint links plugins at step 2.65 before `exec "$@"`, so Ready
                # implies the links exist. Under leader election it does not — replicas > 1
                # sets ENABLE_LEADER_ELECTION, and agentAPIProbe then exits 0 on
                # connection-refused (:2773), so a Ready pod there may not have reached
                # `exec` yet and ABSENT is transient. The window covers that case; what it
                # buys on the RC's single replica is one retry's worth of nothing.
                absent_in = pod
                detail = f"{skill_path} does not exist in {pod}"
        else:
            absent_in = ""
            detail = why
        if time.time() >= deadline:
            break
        time.sleep(5)

    if absent_in:
        # Conclusive: the exec ran, repeatedly, and the file is not there.
        pytest.fail(
            f"The stockout plugin's skill is not mounted in {absent_in}: {skill_path} does not "
            f"exist, {window}s ({bound}) after the gateway rolled out. The AgentPlugin "
            f"reconciled, so the alerts these tests publish would be investigated by nobody. "
            f"Either the plugin image did not reach the '{target_profile or 'default'}' profile, "
            "or the spec the operator reconciled is not the one this candidate installs."
        )
    # Inconclusive rather than a verdict on the plugin: no pod resolved, or every exec
    # failed. Saying which, and which ceiling ran out, keeps this from reading as an
    # accusation against the product when it is the fixture running late.
    pytest.fail(
        f"Could not establish whether the stockout plugin's skill is mounted within {window}s "
        f"({bound}) of the gateway rolling out: {detail}.\n"
        f"The path that would have been probed is {skill_path}."
    )


def _clean_stale_kanban_tasks(
    pod: str,
    namespace: str,
    route_name: str,
    fatal: bool = False,
) -> int:
    """Reclaims and archives any lingering kanban tasks for route_name so slots are open.

    Uses the shared clean_stale_kanban_tasks.py script executed inside the platform-agent
    container. When fatal is True, raises RuntimeError if cleanup encounters an error;
    when fatal is False (default), logs a warning so transient board races do not fail tests.
    """
    if not _CLEAN_KANBAN_SCRIPT.is_file():
        msg = f"Kanban cleanup script missing at {_CLEAN_KANBAN_SCRIPT}"
        if fatal:
            raise RuntimeError(msg)
        warnings.warn(msg)
        return 0

    py_code = _CLEAN_KANBAN_SCRIPT.read_text()
    res = _kubectl(
        "exec", "-i=false", "-n", namespace, pod, "-c", "platform-agent", "--",
        "python3", "-c", py_code, route_name,
    )
    if res.returncode != 0:
        msg = f"Kanban cleanup failed on pod {pod}: {res.stderr or res.stdout}"
        if fatal:
            raise RuntimeError(msg)
        warnings.warn(msg)
        return 0
    try:
        return int(res.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return 0


def _wait_for_agent_available(
    agent_ref: str,
    namespace: str,
    timeout: int = _AGENT_AVAILABILITY_TIMEOUT_SECONDS,
    clean_kanban: bool = True,
    target_profile: Optional[str] = None,
) -> str:
    """Waits until the platform-agent gateway workload is rolled out, ready, and responsive."""
    kind, workload_name = _gateway_workload(agent_ref, namespace)
    if kind is None:
        pytest.fail(
            f"No Deployment or StatefulSet '{workload_name}' in namespace '{namespace}'; "
            f"PlatformAgent '{agent_ref}' has no gateway workload."
        )
    target = f"{kind}/{workload_name}"

    res = _kubectl("rollout", "status", target, "-n", namespace, f"--timeout={timeout}s", timeout=timeout + 30)
    if res.returncode != 0:
        pytest.fail(f"Gateway {target} rollout not ready within {timeout}s: {res.stderr}")

    # Recompute the deadline so pod readiness and skill mount have their own dedicated budget,
    # rather than sharing a deadline that a slow rollout could exhaust before the probe loop starts.
    deadline = time.time() + timeout
    skill_path = _plugin_skill_path(agent_ref, namespace, target_profile)
    probe = f'test -f "{skill_path}" && echo PRESENT || echo ABSENT'
    ready_pod = None
    while time.time() < deadline:
        revision = _current_revision_selector(kind, workload_name, namespace)
        pod, _ = _gateway_pod(agent_ref, namespace, revision)
        if pod:
            probe_res = _kubectl("exec", "-i=false", "-n", namespace, pod, "-c", "platform-agent", "--", "sh", "-c", probe)
            if probe_res.returncode == 0 and "PRESENT" in probe_res.stdout:
                ready_pod = pod
                break
        time.sleep(_AGENT_POLL_INTERVAL_SECONDS)

    if not ready_pod:
        pytest.fail(f"Platform Agent pod for {target} not available and ready within {timeout}s in {namespace}")

    if clean_kanban:
        route_name = os.environ.get("STOCKOUT_ROUTE", _DEFAULT_ROUTE_NAME)
        _clean_stale_kanban_tasks(ready_pod, namespace, route_name)

    return ready_pod


# All 10 GKE Stockout Investigator diagnostic failure scenarios
STOCKOUT_SCENARIO_DEFINITIONS: List[Tuple[str, str, str]] = [
    (
        "01-gpu-regional-scarcity",
        "Rule E",
        "L4 GPUs exhausted in the workload's only permitted zone",
    ),
    (
        "02-gpu-quota-exceeded",
        "Rule F",
        "GPUs requested against smaller regional quota",
    ),
    (
        "03-large-vm-shape-scarcity",
        "Rule B",
        "Pinned to c3-standard-176, the rarest shape in the family",
    ),
    (
        "04-missing-zone-fallback",
        "Rule A",
        "Ordinary workload pinned to one family in one zone",
    ),
    (
        "05-missing-ondemand-floor",
        "Rule D",
        "Every ComputeClass priority is Spot with no on-demand floor",
    ),
    (
        "06-stateful-disk-generation-mix",
        "Rule C",
        "Volume type attaches on some offered generations, not others",
    ),
    (
        "07-hyperdisk-incompatibility",
        "Rule H",
        "Hyperdisk on a class offering only pre-Hyperdisk families",
    ),
    (
        "08-ccc-priority-starvation",
        "Rule G",
        "Over-granular priority list causing autoscaler loop",
    ),
    (
        "09-duplicate-signal",
        "Dedup",
        "The same alert three times: dedup and duplicate-PR suppression",
    ),
    (
        "10-false-signal",
        "False Signal",
        "Alert for a healthy workload; agent stands down with no action",
    ),
]


@pytest.fixture(scope="module")
def ensure_stockout_plugin_installed(
    gcp_project_id: Optional[str],
    gke_cluster_name: Optional[str],
    gcp_region: str,
    agent_namespace: str,
) -> None:
    """Waits for the deployed stockout plugin to settle, then refuses to run on a broken one.

    Installs the plugin only when the AgentPlugin CR is absent, which is what this fixture
    has always done. What it adds is the waiting: step 2 of the RC pipeline provisions the
    environment immediately before this module runs, so the gateway may still be rolling
    when the first scenario starts, and the plugin's skill is linked by the entrypoint of
    whichever pod wins. Probing before that settles reads the outgoing pod.

    That is not a hypothetical. Run 32866087154 reported `SKILL.md not found under the
    platform profile's plugins directory` and every selected scenario then burned its full
    360s watch to report "Platform Agent never started investigation"; run 32953137904, on
    the same cluster, found the skill present. The difference between them is which pod the
    preflight caught, and this fixture is what removes that from the result.

    Three waits, in this order: the AgentPlugin's status catches up to its spec, the gateway
    finishes rolling, and the plugin's SKILL.md is readable inside the pod that survived.
    Any of them failing ends the module naming what was wrong, in place of ten minutes of
    watch timeouts that name nothing.

    Deliberately not a reinstall. Running `install.sh` every session would repair an
    AgentPlugin whose rendered spec has drifted from this candidate's, but it repairs
    nothing in the common case — an unchanged plugin source tree renders a byte-identical
    CR — while adding unconditional writes to the RC's critical path, including four
    `add-iam-policy-binding` calls that read-modify-write the *project* IAM policy. An IAM
    race unrelated to the candidate would then block the release. Drift is worth detecting;
    the checks below do that and say so, rather than papering over it with a write.

    `SKIP_STOCKOUT=1` skips the whole thing, tests included.
    """
    if os.environ.get("SKIP_STOCKOUT") == "1":
        pytest.skip("SKIP_STOCKOUT=1 is set; skipping stockout investigator plugin setup.")

    if not gcp_project_id or not gke_cluster_name:
        pytest.fail("GCP_PROJECT_ID and GKE_CLUSTER_NAME are required for stockout E2E tests.")

    budget_deadline = time.time() + _FIXTURE_BUDGET_SECONDS

    # 1. Ensure the Pub/Sub topic exists.
    topic = os.environ.get("STOCKOUT_TOPIC", "gke-stockout-alerts-topic")
    check_topic = subprocess.run(
        ["gcloud", "pubsub", "topics", "describe", topic, f"--project={gcp_project_id}"],
        capture_output=True,
    )
    if check_topic.returncode != 0:
        subprocess.run(
            ["gcloud", "pubsub", "topics", "create", topic, f"--project={gcp_project_id}"],
            capture_output=True,
        )

    # 2. The CRD is the one prerequisite install.sh does not own: the kube-agents Helm
    # chart installs it, and helm would otherwise fail on an unknown kind.
    check_crd = _kubectl("get", "crd", "agentplugins.kubeagents.x-k8s.io", fail_on_timeout=True)
    if check_crd.returncode != 0:
        pytest.fail(
            "AgentPlugin CRD 'agentplugins.kubeagents.x-k8s.io' not found on cluster; "
            "it is managed and installed by the kube-agents Helm chart."
        )

    # AGENT_REF and KUBECTL_CONTEXT are pinned rather than inherited, so the agent and
    # cluster this fixture inspects cannot be different ones from those install.sh would
    # write to. install.sh falls back to `kubectl config current-context`, which is
    # whatever the last `get-credentials` in this process left behind.
    agent_ref = os.environ.get("AGENT_REF") or _DEFAULT_AGENT_REF
    kube_context = os.environ.get("KUBECTL_CONTEXT") or ""
    if not kube_context:
        ctx_res = _kubectl("config", "current-context", fail_on_timeout=True)
        kube_context = ctx_res.stdout.strip() if ctx_res.returncode == 0 else ""
        if not kube_context:
            pytest.fail(
                "No kubectl context is set and KUBECTL_CONTEXT is unset, so there is no cluster "
                "to verify the stockout plugin against."
            )

    # 3. Install only when the plugin is genuinely absent.
    check_plugin = _kubectl(
        "get", "agentplugins", _PLUGIN_NAME, "-n", agent_namespace, fail_on_timeout=True
    )
    if check_plugin.returncode != 0:
        if not _INSTALL_SCRIPT.is_file():
            pytest.fail(f"Stockout investigator install script missing at '{_INSTALL_SCRIPT}'.")
        install_env = {
            **os.environ,
            "GCP_PROJECT_ID": gcp_project_id,
            "TARGET_CLUSTER_NAME": gke_cluster_name,
            "TARGET_CLUSTER_LOCATION": gcp_region,
            "HERMES_NAMESPACE": agent_namespace,
            "AGENT_REF": agent_ref,
            "KUBECTL_CONTEXT": kube_context,
        }
        install_timeout, install_bound = _remaining(budget_deadline, _INSTALL_TIMEOUT_SECONDS)
        try:
            proc = subprocess.run(
                [str(_INSTALL_SCRIPT)],
                capture_output=True,
                text=True,
                env=install_env,
                timeout=install_timeout,
            )
        except subprocess.TimeoutExpired as exc:
            pytest.fail(
                f"Stockout investigator install.sh did not finish within {install_timeout}s "
                f"({install_bound}):\n"
                f"STDOUT:\n{_as_text(exc.stdout)}\nSTDERR:\n{_as_text(exc.stderr)}"
            )
        if proc.returncode != 0:
            pytest.fail(
                f"Stockout investigator install.sh failed with exit code {proc.returncode}:\n"
                f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
            )
        # Printed on success too. It names the image tag and says whether the build was
        # skipped, which is the only record of what the tests below ran against.
        print(proc.stdout)

    # 4. Plugin status first, rollout second. See _wait_for_plugin_ready for why the other
    # order lets a slow reconcile pass a check against the outgoing pod.
    plugin = _wait_for_plugin_ready(agent_namespace, budget_deadline)
    target, outcome = _wait_for_gateway_rollout(agent_ref, agent_namespace, budget_deadline)
    print(outcome)

    kind, workload_name = target.split("/", 1)
    pod = _verify_skill_mounted(
        agent_ref,
        agent_namespace,
        plugin.get("spec", {}).get("targetProfile", ""),
        kind,
        workload_name,
        budget_deadline,
    )
    # Pre-test cleanup: clear any stale kanban tasks from previous runs so concurrency slots
    # (max_in_progress = 2) are not starved. Non-fatal so a board race or alert arrival during
    # fixture setup does not error the entire test session.
    _clean_stale_kanban_tasks(
        pod, agent_namespace, os.environ.get("STOCKOUT_ROUTE", _DEFAULT_ROUTE_NAME), fatal=False
    )
    print(f"stockout plugin verified in {pod}; the tests below run against it")


def test_stockout_ingress_alert_smoke(
    ensure_stockout_plugin_installed: None,
    gcp_project_id: Optional[str],
    gke_cluster_name: Optional[str],
    gcp_region: str,
    agent_namespace: str,
) -> None:
    """Verifies that synthetic autoscaler scale-up error alerts can be published to the PubSub topic."""
    if os.environ.get("SKIP_STOCKOUT") == "1":
        pytest.skip("SKIP_STOCKOUT=1 is set; skipping stockout ingress smoke test.")

    verify_script = _REPO_ROOT / "agentplugins" / "gke-stockout-investigator" / "verify.sh"
    if not verify_script.is_file():
        pytest.fail(f"Stockout verify script missing at '{verify_script}'.")
    if not gcp_project_id or not gke_cluster_name:
        pytest.fail("GCP_PROJECT_ID and GKE_CLUSTER_NAME are required for stockout smoke test.")

    # Check if the stockout plugin is active in the cluster
    res_plugin = subprocess.run(
        ["kubectl", "get", "agentplugins", "gkestockoutinvestigator", "-n", agent_namespace],
        capture_output=True,
        text=True,
        timeout=5,
    )
    if res_plugin.returncode != 0:
        pytest.fail("gkestockoutinvestigator AgentPlugin is not active in cluster; ingress smoke test failed.")

    agent_ref = os.environ.get("AGENT_REF") or _DEFAULT_AGENT_REF
    ready_pod = _wait_for_agent_available(agent_ref, agent_namespace)

    env = {
        **os.environ,
        "TARGET_CLUSTER_NAME": gke_cluster_name,
        "GCP_PROJECT_ID": gcp_project_id,
        "TARGET_CLUSTER_LOCATION": gcp_region,
        # verify.sh reads AGENT_NAMESPACE too. Under execute_e2e_tests.py it is already
        # exported, but a bare `pytest tests/e2e/...` is a documented run mode, and there
        # the fixture would probe one namespace while verify.sh read another.
        "AGENT_NAMESPACE": agent_namespace,
    }

    try:
        proc = subprocess.run(
            [str(verify_script)],
            capture_output=True,
            text=True,
            env=env,
            timeout=_SMOKE_VERIFY_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        pytest.fail(
            f"Stockout ingress alert verify.sh timed out after {_SMOKE_VERIFY_TIMEOUT_SECONDS}s:\n"
            f"STDOUT:\n{_as_text(exc.stdout)}\nSTDERR:\n{_as_text(exc.stderr)}"
        )
    finally:
        # Best-effort cleanup: archive tasks created by verify.sh so subsequent scenarios
        # have open concurrency slots. Non-fatal so cleanup errors do not mask test assertions.
        try:
            _clean_stale_kanban_tasks(
                ready_pod, agent_namespace, os.environ.get("STOCKOUT_ROUTE", _DEFAULT_ROUTE_NAME), fatal=False
            )
        except Exception:
            pass
    assert proc.returncode == 0, (
        f"Stockout ingress alert verify.sh failed with exit code {proc.returncode}:\n"
        f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    )


@pytest.mark.parametrize(
    "scenario_slug,rule,description",
    STOCKOUT_SCENARIO_DEFINITIONS,
    ids=[slug for slug, _, _ in STOCKOUT_SCENARIO_DEFINITIONS],
)
def test_stockout_scenario(
    ensure_stockout_plugin_installed: None,
    gcp_project_id: Optional[str],
    gke_cluster_name: Optional[str],
    gcp_region: str,
    agent_namespace: str,
    scenario_slug: str,
    rule: str,
    description: str,
) -> None:
    """Exercises an end-to-end stockout investigation scenario against the target GKE cluster."""
    if os.environ.get("SKIP_STOCKOUT") == "1":
        pytest.skip("SKIP_STOCKOUT=1 is set; skipping stockout scenarios.")

    # Filter by STOCKOUT_SCENARIOS if specified (default: "04" for fast promotion gating; "all" for nightly matrix)
    selected_scenarios = os.environ.get("STOCKOUT_SCENARIOS", "04").strip()
    if selected_scenarios and selected_scenarios.lower() != "all":
        allowed_list = [s.strip() for s in selected_scenarios.split(",")]
        # Match by prefix (e.g. "04" matches "04-missing-zone-fallback") or exact slug
        if not any(scenario_slug.startswith(pattern) or pattern in scenario_slug for pattern in allowed_list):
            pytest.skip(f"Scenario {scenario_slug} not included in STOCKOUT_SCENARIOS='{selected_scenarios}'")

    if "gpu" in scenario_slug.lower():
        # Check if the cluster has any GPU accelerators or nodepools
        res_gpu = subprocess.run(
            ["kubectl", "get", "nodes", "-o", "jsonpath={.items[*].status.allocatable}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if "nvidia.com/gpu" not in res_gpu.stdout:
            pytest.skip(f"Cluster '{gke_cluster_name}' has no GPU nodes (nvidia.com/gpu); skipping GPU scenario '{scenario_slug}'.")

    scenario_script = _SCENARIOS_DIR / f"{scenario_slug}.sh"
    if not scenario_script.is_file():
        pytest.fail(f"Scenario script '{scenario_script}' missing.")
    if not gcp_project_id or not gke_cluster_name:
        pytest.fail("GCP_PROJECT_ID and GKE_CLUSTER_NAME are required for stockout scenario.")

    agent_ref = os.environ.get("AGENT_REF") or _DEFAULT_AGENT_REF
    ready_pod = _wait_for_agent_available(agent_ref, agent_namespace)

    env = {
        **os.environ,
        "TARGET_CLUSTER_NAME": gke_cluster_name,
        "GCP_PROJECT_ID": gcp_project_id,
        "TARGET_CLUSTER_LOCATION": gcp_region,
        # scenarios/lib/common.sh reads AGENT_NAMESPACE; see the note in the smoke test.
        "AGENT_NAMESPACE": agent_namespace,
    }

    # Watch timeout can be customized via STOCKOUT_WATCH_TIMEOUT (default 360 seconds)
    watch_timeout = os.environ.get("STOCKOUT_WATCH_TIMEOUT", "360")
    scenario_timeout = int(watch_timeout) + _SCENARIO_RUN_HEADROOM_SECONDS

    try:
        proc = subprocess.run(
            [str(scenario_script), "--teardown", "--watch-timeout", watch_timeout],
            capture_output=True,
            text=True,
            env=env,
            timeout=scenario_timeout,
        )
    except subprocess.TimeoutExpired as exc:
        pytest.fail(
            f"Stockout Scenario '{scenario_slug}' ({rule}) timed out after {scenario_timeout}s:\n"
            f"STDOUT:\n{_as_text(exc.stdout)}\nSTDERR:\n{_as_text(exc.stderr)}"
        )
    finally:
        # Best-effort cleanup: archive tasks created by the scenario script so subsequent
        # scenarios have open concurrency slots. Non-fatal so cleanup errors do not mask test assertions.
        try:
            _clean_stale_kanban_tasks(
                ready_pod, agent_namespace, os.environ.get("STOCKOUT_ROUTE", _DEFAULT_ROUTE_NAME), fatal=False
            )
        except Exception:
            pass
    assert proc.returncode == 0, (
        f"Stockout Scenario '{scenario_slug}' ({rule} - {description}) failed with exit code {proc.returncode}:\n"
        f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    )
    assert "no new session or board task after" not in proc.stdout, (
        f"Stockout Scenario '{scenario_slug}' ({rule}) timed out: Platform Agent never started investigation:\n"
        f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    )
    assert ("investigation started:" in proc.stdout or "the workload scheduled after all" in proc.stdout), (
        f"Stockout Scenario '{scenario_slug}' ({rule}) did not record an active investigation:\n"
        f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    )
