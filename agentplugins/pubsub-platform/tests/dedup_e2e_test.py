#!/usr/bin/env python3
"""End-to-end suppression test against a live Hermes deployment.

    KUBE_CONTEXT=<kubectl context of the cluster running the agent> \
    GCP_PROJECT_ID=<project> TARGET_CLUSTER_NAME=<cluster the plugin watches> \
    TARGET_CLUSTER_LOCATION=<its region> \
    python3 agentplugins/pubsub-platform/tests/dedup_e2e_test.py

The unit tests next door prove the adapter's logic in isolation. They cannot prove that
the logic is *reached*: the route config that ships in the AgentPlugin, the log filter,
the dedup fields and the dispatch mode all live outside the code, and every one of them
has already been wrong at least once on a real deployment — dedup switched off by a stray
environment variable, a filter expression that silently evaluated to empty, dedup fields
that keyed on per-retry detail. This publishes real messages to the real topic and reads
what the running adapter did with them.

It deploys what it tests, by running the two plugins' own `install.sh` before the first
alert (`SKIP_INSTALL=true` reuses whatever is already deployed). That is not setup
convenience: everything above lives in the charts those installers apply, so a test run
against a hand-made deployment proves the chart on that cluster rather than the chart in
this repository. It also removes the mismatch this test used to fail on — the cluster
name is compiled into the route's filter, so a deployment made for a different cluster
drops every alert published here and all five assertions fail for a reason unrelated to
dedup.

What it asserts, one published alert at a time:

  1. a new incident is filed as a kanban task
  2. the identical alert is suppressed
  3. the same incident with different retry detail (another zone, another failure reason)
     is suppressed — this is what turned one stockout into an investigation per retry
  4. a different workload is filed as its own task
  5. an alert naming no workload is dropped by the filter before dedup sees it

Cost and cleanup. Steps 1 and 4 file real tasks, and the dispatcher will start working
them within seconds. The test archives every task it caused — reclaiming the worker's
claim first, because a running task cannot be archived — and restores the dedup registry
it found. It only ever touches tasks whose body carries this run's unique marker, so a
genuine investigation running alongside it is left alone. Cleanup also runs when an
assertion fails.

What is NOT undone is everything the installers do, because installing is the point
rather than a fixture: two images published to Artifact Registry, a Pub/Sub topic,
subscription and log sink, IAM bindings on all three, and — since APPLY_TUNING defaults
to true — the execution limits patched onto the PlatformAgent's spec. A run leaves the
cluster installed, which is what SKIP_INSTALL=true then reuses. Point this at a
deployment you are willing to have installed over.

Point it at a single-replica agent, too. Every assertion here reads one pod's log and one
copy of the dedup registry, and a published message is delivered to whichever replica pulls
it — so on a multi-replica gateway a suppression that worked still reads as a failure. See
ready_gateway_pods for what the registry does and does not share between pods.

Environment, all required and none defaulted to a particular fleet: KUBE_CONTEXT,
GCP_PROJECT_ID, TARGET_CLUSTER_NAME (the cluster the route is installed for),
TARGET_CLUSTER_LOCATION. Optional: NAMESPACE, AGENT_REF, GATEWAY_DEPLOYMENT,
STOCKOUT_TOPIC, STOCKOUT_SUBSCRIPTION, STOCKOUT_SINK, STOCKOUT_ROUTE, SETTLE_SECONDS,
SKIP_INSTALL, INSTALL_TIMEOUT, OPERATOR_SETTLE_SECONDS, GENERATION_STABLE_SECONDS,
PUBSUB_PLUGIN_IMAGE, STOCKOUT_PLUGIN_IMAGE.

The Pub/Sub trio travels together: the topic, the subscription bound to it and the sink
that writes to it are one pipe, and overriding one alone breaks it. Override all three or
none.

The two image variables are per-plugin on purpose, and PLUGIN_IMAGE — which the
installers read directly — is deliberately not honoured here. One value shared by both
installers publishes the adapter's files as the investigator's image, or the reverse,
and the plugin whose files went missing has no skill to register: `require_skills: true`
then refuses every dispatch and all five assertions fail for a reason that is not dedup.
"""

import json
import os
import random
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

KUBE_CONTEXT = os.environ.get("KUBE_CONTEXT", "")
PROJECT_ID = os.environ.get("GCP_PROJECT_ID", "")
NAMESPACE = os.environ.get("NAMESPACE", "kubeagents-system")
# Passed to the installers, which name the agent the plugin attaches to.
AGENT_REF = os.environ.get("AGENT_REF", "platform-agent")
# The operator names the gateway `<agent>-gateway`, so this follows AGENT_REF rather than
# repeating its default. Hardcoded, the two drift the moment somebody sets AGENT_REF: the
# installers attach the plugin to one agent and every assertion here watches another's
# pod, so nothing is ever seen and the failure looks like a broken route.
DEPLOYMENT = os.environ.get("GATEWAY_DEPLOYMENT", f"{AGENT_REF}-gateway")
TOPIC = os.environ.get("STOCKOUT_TOPIC", "gke-stockout-alerts-topic")
# Passed to the installer alongside the topic. A subscription's topic is fixed at
# creation, so overriding one without the other leaves the adapter pulling a queue the
# sink never writes to — the installer refuses that outright, rather than here.
SUBSCRIPTION = os.environ.get("STOCKOUT_SUBSCRIPTION", "gke-stockout-alerts-sub")
# Passed for the same reason, at the other end of the pipe. A log sink is project-wide,
# and the installer refuses to repoint an existing one at a different topic — so a run
# that overrides STOCKOUT_TOPIC and nothing else does not hijack the default sink, it
# stops with an error naming this variable.
SINK = os.environ.get("STOCKOUT_SINK", "gke-stockout-alerts-sink")
ROUTE = os.environ.get("STOCKOUT_ROUTE", "gke_stockout_alerts")
CLUSTER = os.environ.get("TARGET_CLUSTER_NAME", "")
LOCATION = os.environ.get("TARGET_CLUSTER_LOCATION", "")
REGISTRY = "/opt/data/pubsub_registry.json"

# How long to wait for the adapter to act on a published message. The subscriber pulls
# continuously; a few seconds is plenty, but the board's dispatcher adds a tick.
SETTLE_SECONDS = int(os.environ.get("SETTLE_SECONDS", "45"))

REPO_ROOT = Path(__file__).resolve().parents[3]
# Installer, and the variable naming a prebuilt image for that plugin alone. Never one
# shared PLUGIN_IMAGE: see the module docstring for what that installs.
INSTALLERS = (
    (REPO_ROOT / "agentplugins" / "pubsub-platform" / "install.sh", "PUBSUB_PLUGIN_IMAGE"),
    (REPO_ROOT / "agentplugins" / "gke-stockout-investigator" / "install.sh", "STOCKOUT_PLUGIN_IMAGE"),
)
# The stockout installer enables APIs, creates the topic, subscription and log sink, and
# builds the plugin images, so it is minutes rather than seconds on a first run.
INSTALL_TIMEOUT = int(os.environ.get("INSTALL_TIMEOUT", "1200"))
# How long to give the operator to turn the applied CRs into a new Deployment spec before
# giving up on seeing the roll start. Not an assertion: a re-run that changes nothing
# legitimately never moves the generation.
OPERATOR_SETTLE_SECONDS = int(os.environ.get("OPERATOR_SETTLE_SECONDS", "120"))
# How long the generation has to hold still before the spec counts as settled.
#
# One install produces more than one revision. The helm apply reconciles the AgentPlugin
# into the Deployment; the stockout installer's tuning patch then changes the agent's
# config, and its hash rides on the pod template (kubeagents.x-k8s.io/config-hash), so
# that is a second one. Waiting for the first bump and calling `rollout status` can
# therefore succeed against the intermediate revision moments before the next arrives.
GENERATION_STABLE_SECONDS = int(os.environ.get("GENERATION_STABLE_SECONDS", "20"))
# Rejecting anything but true/false, rather than treating every other value as false:
# SKIP_INSTALL=1 reads as an opt-out to a human, and silently reinstalling would replace
# the deployment somebody meant to inspect.
SKIP_INSTALL = os.environ.get("SKIP_INSTALL", "false").strip().lower()

RUN_ID = f"dedupe2e{int(time.time())}{random.randint(100, 999)}"


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def die(msg: str) -> None:
    raise AssertionError(msg)


def run(cmd: list, check: bool = True, timeout: int = 120) -> subprocess.CompletedProcess:
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if check and res.returncode != 0:
        die(f"command failed: {' '.join(cmd[:6])}…\n{res.stderr.strip()[:500]}")
    return res


def kubectl(args: list, check: bool = True, timeout: int = 120) -> str:
    return run(["kubectl", "--context", KUBE_CONTEXT, "--request-timeout=60s"] + args,
               check=check, timeout=timeout).stdout


# The pod every observation after preflight is made against, and whether pinning has been
# switched on yet. See pin_agent_pod.
_PINNED_POD = ""
_PIN_ENABLED = False


def ready_gateway_pods() -> list:
    """Ready, not-terminating gateway pods, oldest first.

    Not `items[0]` of `--field-selector=status.phase=Running`: a pod keeps phase Running
    until its containers actually exit, so during a roll the outgoing pod is still in
    that list and may well be first. Two things here read per-pod state rather than
    cluster state, and both are wrong if they land on the pod on its way out — `kubectl
    exec` into a Terminating pod fails outright, and `kubectl logs` returns the log of a
    process that never saw the message just published, which is what steps 2 and 5
    assert on.

    The dedup registry is NOT one of those things, on the deployment this test is written
    for. It lives on the agent's PVC (`<agent>-data`, mounted at /opt/data) and the adapter
    re-reads it from disk on every message, so under a Deployment — one pod, or several
    sharing the one claim — a reset written through any pod is a reset every pod sees.
    That is what lets preflight's reset and cleanup's restore be made through whichever pod
    answers, and why the backup is written to /opt/data rather than to a container-local
    /tmp.

    It does not hold under the StatefulSet gateway_workload() describes: that shape exists
    precisely because the storage is RWO, so each pod gets a claim of its own and a
    registry written through one is invisible to the others. Nothing here — the reset, the
    backup, the restore, or the suppression assertions themselves — is sound against a
    multi-replica agent, because the message this test publishes is delivered to whichever
    replica pulls it. Run it against a single-replica agent.
    """
    raw = kubectl(["get", "pod", "-n", NAMESPACE, "-l", f"app={DEPLOYMENT}", "-o", "json"])
    try:
        items = json.loads(raw).get("items", [])
    except ValueError:
        items = []
    live = [
        p for p in items
        if p.get("status", {}).get("phase") == "Running"
        and not p.get("metadata", {}).get("deletionTimestamp")
        and any(c.get("type") == "Ready" and c.get("status") == "True"
                for c in p.get("status", {}).get("conditions", []))
    ]
    # creationTimestamp has second granularity, so the pod name breaks a tie rather than
    # leaving two pods created in the same second in whatever order the API returned them.
    live.sort(key=lambda p: (p["metadata"].get("creationTimestamp", ""),
                             p["metadata"]["name"]))
    return [p["metadata"]["name"] for p in live]


def agent_pod() -> str:
    """The gateway pod to talk to: the newest Ready one, or the pinned one after pinning.

    Two phases, because the two halves of this run want opposite things. Before
    pin_agent_pod, the deployment is still being rolled by install_plugins and a caller
    wants to FOLLOW that — deployed_route polls precisely because the pod holding the new
    config is not up yet, and latching onto the outgoing pod would mean polling something
    that will never have the route until the API server finally marks it deleted.

    After pin_agent_pod, one pod for the rest of the run. Resolving each exec and each
    log read independently lets an assertion straddle two pods: publish against the pod
    that has the route, then read the log of the pod that replaced it and see nothing.

    A pinned pod that is no longer Ready is not silently swapped out. It is replaced and
    said out loud, because every observation made before that point was made against a
    different process, and a suppression assertion that spans the two proves nothing.
    That warning means something has gone wrong; it must not be reachable from the
    ordinary startup roll, which is why the pin does not begin until the roll is over.
    """
    global _PINNED_POD
    live = ready_gateway_pods()
    if not live:
        die(f"no Ready {DEPLOYMENT} pod in {NAMESPACE}")
    if not _PIN_ENABLED:
        return live[-1]
    if _PINNED_POD:
        if _PINNED_POD in live:
            return _PINNED_POD
        log(f"WARNING: pod {_PINNED_POD} went away mid-run and {live[-1]} replaced it; "
            "observations either side of this line are of different processes")
    _PINNED_POD = live[-1]
    return _PINNED_POD


def pin_agent_pod() -> None:
    """Fix the pod for the rest of the run, after the roll has finished."""
    global _PINNED_POD, _PIN_ENABLED
    _PIN_ENABLED = True
    _PINNED_POD = ""
    log(f"  observing pod {agent_pod()}")


def in_pod(script: str, check: bool = True) -> str:
    return kubectl(["exec", "-n", NAMESPACE, agent_pod(), "-c", "platform-agent", "--",
                    "sh", "-c", script], check=check)


def hermes(args: str, check: bool = True) -> str:
    return in_pod(f"HOME=/tmp /opt/hermes/.venv/bin/hermes {args}", check=check)


def gateway_log(since_seconds: int) -> str:
    return kubectl(["logs", "-n", NAMESPACE, agent_pod(), "-c", "platform-agent",
                    f"--since={since_seconds}s"], check=False)


# ----------------------------------------------------------------- alert payloads

def alert(controller: str, *, kind: str = "ReplicaSet", namespace: str = "dedup-e2e",
          zone: str = "", reason: str = "no.scale.up.nap.pod.zonal.resources.exceeded") -> dict:
    """A `noDecisionStatus.noScaleUp` event, shaped like the autoscaler's own.

    `zone` and `reason` are the fields that vary between the retries of ONE incident.
    The zone defaults to a zone of the configured region, so nothing here names a
    particular fleet.
    """
    zone = zone or f"{LOCATION}-b"
    return {
        "insertId": f"{RUN_ID}-{random.randint(1000, 9999)}",
        "logName": f"projects/{PROJECT_ID}/logs/test-stockout",
        "severity": "WARNING",
        "resource": {"type": "k8s_cluster", "labels": {
            "cluster_name": CLUSTER, "location": LOCATION, "project_id": PROJECT_ID}},
        "jsonPayload": {
            "messageId": "scale.up.error.out.of.resources",
            "noDecisionStatus": {"noScaleUp": {
                "unhandledPodGroupsTotalCount": 1,
                "unhandledPodGroups": [{
                    "podGroup": {
                        "totalPodCount": 1,
                        "samplePod": {
                            "name": f"{controller}-xyz",
                            "namespace": namespace,
                            "controller": {"apiVersion": "apps/v1", "kind": kind, "name": controller},
                        },
                    },
                    "napFailureReasons": [{"messageId": reason, "parameters": [zone]}],
                }],
            }},
            # Marker the cleanup keys on. It rides inside the payload, so it reaches the
            # task body through the prompt's {__raw__} and identifies our tasks exactly —
            # an organic investigation running at the same time is never touched.
            "dedupE2eRunId": RUN_ID,
        },
    }


def summary_alert() -> dict:
    """A NAP summary: real, frequent, and names no workload for the filter to accept."""
    return {
        "insertId": f"{RUN_ID}-summary",
        "logName": f"projects/{PROJECT_ID}/logs/test-stockout",
        "resource": {"type": "k8s_cluster", "labels": {
            "cluster_name": CLUSTER, "location": LOCATION, "project_id": PROJECT_ID}},
        "jsonPayload": {"noDecisionStatus": {"noScaleUp": {
            "napFailureReasons": [{"messageId": "no.scale.up.nap.disabled"}]}},
            "dedupE2eRunId": RUN_ID},
    }


def publish(payload: dict) -> None:
    run(["gcloud", "pubsub", "topics", "publish", TOPIC,
         f"--project={PROJECT_ID}", f"--message={json.dumps(payload)}"])


# ------------------------------------------------------------------- observations

def our_tasks() -> dict:
    """Task id -> status, for every task carrying this run's marker."""
    raw = hermes("kanban ls --json", check=False)
    try:
        data = json.loads(raw)
    except Exception:
        return {}
    tasks = data.get("tasks") if isinstance(data, dict) else data
    found = {}
    for task in tasks or []:
        blob = json.dumps(task)
        if RUN_ID in blob:
            found[task.get("id", "")] = task.get("status", "")
    return found


def wait_for_task(expected: int, label: str) -> dict:
    """Wait until this run owns `expected` tasks; return them."""
    deadline = time.time() + SETTLE_SECONDS
    tasks = {}
    while time.time() < deadline:
        tasks = our_tasks()
        if len(tasks) >= expected:
            return tasks
        time.sleep(3)
    die(f"{label}: expected {expected} task(s) for this run, saw {len(tasks)} after "
        f"{SETTLE_SECONDS}s: {tasks}")


def expect_no_new_task(baseline: int, label: str) -> None:
    """Assert no task appeared — the point of a suppression test."""
    deadline = time.time() + SETTLE_SECONDS
    while time.time() < deadline:
        tasks = our_tasks()
        if len(tasks) > baseline:
            die(f"{label}: a task was filed when the alert should have been suppressed "
                f"({sorted(set(tasks) )})")
        time.sleep(3)


def log_says(marker: str, since_seconds: int) -> bool:
    return marker.lower() in gateway_log(since_seconds).lower()


# ------------------------------------------------------------------------ cleanup

def cleanup(registry_backup: str) -> None:
    log("Cleaning up")
    tasks = our_tasks()
    for task_id, status in sorted(tasks.items()):
        # A running task cannot be archived: release the worker's claim first. Both calls
        # are best-effort — leaving a task behind must not mask the test's own verdict.
        if status not in ("done", "ready"):
            hermes(f'kanban reclaim {task_id} --reason "dedup e2e cleanup"', check=False)
        hermes(f"kanban archive {task_id}", check=False)
    remaining = our_tasks()
    if remaining:
        log(f"WARNING: could not archive {sorted(remaining)} — archive them by hand")
    else:
        log(f"  archived {len(tasks)} task(s) this run created")

    if registry_backup:
        # Confirmed, not assumed. This runs from a `finally`, so it must not raise — but
        # a restore that quietly failed and reported success is worse than one that
        # failed loudly: the deployment silently loses the suppression state it had, and
        # the next real alert of an already-open incident opens a second investigation.
        done = in_pod(
            f"cp {registry_backup} {REGISTRY} && rm -f {registry_backup} && echo restored",
            check=False).strip()
        if done == "restored":
            log("  restored the dedup registry that was there before")
        else:
            log(f"WARNING: could not restore {registry_backup} over {REGISTRY}. The "
                "registry this run started from is lost; the next alert for an incident "
                "that was already open will be filed again.")
    else:
        in_pod(f"rm -f {REGISTRY}", check=False)
        log("  removed the registry (there was none before)")


# --------------------------------------------------------------------------- main

_WORKLOAD = ""


def gateway_workload() -> str:
    """`deployment/<name>` or `statefulset/<name>`, whichever the operator built.

    Not hardcoded to a Deployment. The operator builds a StatefulSet instead when the
    agent asks for more than one replica AND custom RWO storage (useStatefulSet in
    k8s-operator/internal/controller/platformagent_manifests.go), deleting the other kind
    as it goes, so exactly one of the two exists. Both are named `<agent>-gateway` and
    both carry the `app=<agent>-gateway` label the pod lookup uses, so this is the only
    place the distinction shows.

    Empty when neither exists yet, which is the honest answer on a cluster that has never
    had this agent installed. Not cached in that case, so a later call sees the workload
    the installers create.
    """
    global _WORKLOAD
    if _WORKLOAD:
        return _WORKLOAD
    for kind in ("deployment", "statefulset"):
        found = kubectl(["get", kind, DEPLOYMENT, "-n", NAMESPACE,
                         "-o", "jsonpath={.metadata.name}"], check=False).strip()
        if found == DEPLOYMENT:
            _WORKLOAD = f"{kind}/{DEPLOYMENT}"
            return _WORKLOAD
    return ""


def gateway_generation() -> int:
    """The gateway workload's spec generation, or 0 before it exists."""
    workload = gateway_workload()
    if not workload:
        return 0
    raw = kubectl(["get", workload, "-n", NAMESPACE,
                   "-o", "jsonpath={.metadata.generation}"], check=False).strip()
    return int(raw) if raw.isdigit() else 0


def install_plugins() -> None:
    """Deploy the adapter and the stockout route with the plugins' own installers.

    The installers are idempotent and content-addressed: a re-run with nothing changed
    republishes no image and leaves the deployment alone, so this is cheap on every run
    after the first.
    """
    # Read before anything is applied: the wait at the end needs to know what "changed"
    # means, and after the installers have run it is too late to ask.
    generation_before = gateway_generation()
    base = {
        **os.environ,
        # install.sh reads the context from KUBECTL_CONTEXT and the namespace from
        # HERMES_NAMESPACE; this test names both differently.
        "KUBECTL_CONTEXT": KUBE_CONTEXT,
        "HERMES_NAMESPACE": NAMESPACE,
        "GCP_PROJECT_ID": PROJECT_ID,
        "TARGET_CLUSTER_NAME": CLUSTER,
        # Explicit, not inherited: DEPLOYMENT is derived from it, so the agent the
        # installers attach the plugins to has to be the agent whose pod is watched here.
        "AGENT_REF": AGENT_REF,
        # All three, always. The subscription's topic is fixed at creation and a log sink
        # is project-wide, so passing only the topic would leave a pre-existing default
        # subscription attached to the default topic — the adapter pulling a queue
        # nothing publishes to — and would ask the installer to repoint the shared
        # default sink, which it refuses.
        "STOCKOUT_TOPIC": TOPIC,
        "STOCKOUT_SUBSCRIPTION": SUBSCRIPTION,
        "STOCKOUT_SINK": SINK,
    }
    # Never inherited: one reference cannot be right for two different plugins.
    base.pop("PLUGIN_IMAGE", None)

    for installer, image_var in INSTALLERS:
        env = dict(base)
        prebuilt = os.environ.get(image_var, "")
        if prebuilt:
            env["PLUGIN_IMAGE"] = prebuilt
        log(f"Installing {installer.parent.name} ({installer.relative_to(REPO_ROOT)})")
        # Output is not captured: these run for minutes, and a silent installer looks
        # like a hung test.
        try:
            res = subprocess.run([str(installer)], env=env, timeout=INSTALL_TIMEOUT)
        except subprocess.TimeoutExpired:
            # Raised as an assertion so it reports as a test failure rather than a
            # traceback: the handler in __main__ only knows about AssertionError.
            die(f"{installer.parent.name} install.sh did not finish within "
                f"{INSTALL_TIMEOUT}s; raise INSTALL_TIMEOUT if a first install needs longer")
        if res.returncode != 0:
            die(f"{installer} exited {res.returncode}")

    # The installers apply CRs; the operator rewrites the Deployment afterwards. A
    # `rollout status` issued inside that window reports success against the spec that
    # was already there — the wait passes before the new plugins exist anywhere, which is
    # the one thing it is supposed to prevent. So wait for the spec to move first.
    #
    # And then to STOP moving. One install produces two revisions, not one: the helm
    # apply reconciles the AgentPlugin, and the tuning patch that follows it changes the
    # config hash on the pod template. Breaking out on the first bump leaves `rollout
    # status` free to report Complete against the intermediate revision in the moment
    # before the second arrives, which is the same "waited for the wrong thing" this
    # whole block exists to avoid.
    #
    # Bounded and non-fatal, because a generation that never moves is the correct outcome
    # of a re-run that changed nothing. deployed_route() is what actually proves the
    # route is live; this only stops the roll being missed entirely.
    log("Waiting for the operator to reconcile the plugins into the gateway")
    deadline = time.time() + OPERATOR_SETTLE_SECONDS
    seen = generation_before
    moved_at = None
    while time.time() < deadline:
        current = gateway_generation()
        if current != seen:
            seen = current
            moved_at = time.time()
            log(f"  the gateway spec moved to generation {current}")
        elif moved_at is not None and time.time() - moved_at >= GENERATION_STABLE_SECONDS:
            break
        time.sleep(3)
    if moved_at is None:
        log(f"  spec unchanged after {OPERATOR_SETTLE_SECONDS}s — nothing to roll, or the "
            "operator is not reconciling; deployed_route() below is the real check")

    workload = gateway_workload()
    if not workload:
        die(f"no {DEPLOYMENT} Deployment or StatefulSet in {NAMESPACE} after both "
            f"installers ran; the operator never reconciled agent '{AGENT_REF}'")
    run(["kubectl", "--context", KUBE_CONTEXT, "-n", NAMESPACE, "rollout", "status",
         workload, "--timeout=600s"], timeout=660)


def deployed_route(timeout_sec: int = 240) -> dict:
    """Read the route the agent is actually running, once the pod serving it is up.

    Polled rather than read once: the operator rewrites the deployment after the CRs
    land, so for a stretch after the installer returns the only Running pod is the old
    one, whose config.yaml has no route in it yet.
    """
    deadline = time.time() + timeout_sec
    problem = "no pod answered"
    while True:
        try:
            raw = in_pod(
                "/opt/hermes/.venv/bin/python3 -c \""
                "import json, yaml;"
                "c = yaml.safe_load(open('/opt/data/config.yaml'));"
                f"r = c['platforms']['pubsub']['extra']['subscriptions']['{ROUTE}'];"
                "print(json.dumps({'dedup': bool(r.get('deduplicate_fields')),"
                " 'dispatch': r.get('dispatch', 'api'), 'filter': bool(r.get('filter'))}))\"",
                check=False)
            return json.loads(raw.strip())
        except (AssertionError, ValueError) as exc:
            problem = str(exc)[:200] or problem
        if time.time() >= deadline:
            die(f"route '{ROUTE}' never appeared in the agent's config.yaml after "
                f"{timeout_sec}s: {problem}")
        time.sleep(5)


def preflight() -> str:
    missing = [n for n, v in (("KUBE_CONTEXT", KUBE_CONTEXT), ("GCP_PROJECT_ID", PROJECT_ID),
                              ("TARGET_CLUSTER_NAME", CLUSTER), ("TARGET_CLUSTER_LOCATION", LOCATION))
               if not v]
    if missing:
        # No defaults: the cluster name is compiled into the route's filter, so a wrong
        # one drops every published alert and each assertion fails for the wrong reason.
        die(f"set {', '.join(missing)}")
    if SKIP_INSTALL not in ("true", "false"):
        die(f"SKIP_INSTALL must be 'true' or 'false', got '{SKIP_INSTALL}'")
    log(f"Run marker: {RUN_ID}")

    if SKIP_INSTALL == "true":
        log("SKIP_INSTALL=true: testing whatever is already deployed")
    else:
        install_plugins()

    cfg = deployed_route()
    if not cfg["dedup"]:
        die(f"route '{ROUTE}' has no deduplicate_fields; nothing to test")
    if not cfg["filter"]:
        die(f"route '{ROUTE}' has no filter; step 5 would be meaningless")
    log(f"  route '{ROUTE}': dedup on, dispatch={cfg['dispatch']}")

    # From here on, one pod. deployed_route() had to be free to follow the roll; the
    # assertions must not be.
    pin_agent_pod()

    disabled = in_pod("echo ${DISABLE_PUBSUB_DEDUP:-unset}").strip()
    if disabled.lower() == "true":
        die("DISABLE_PUBSUB_DEDUP=true on the pod: dedup is switched off, so this test "
            "would pass or fail for the wrong reason. Remove it and re-run.")
    log(f"  DISABLE_PUBSUB_DEDUP={disabled}")

    backup = ""
    if in_pod(f"test -f {REGISTRY} && echo yes || echo no").strip() == "yes":
        # Beside the registry on the agent's PVC, not in the pod's /tmp. /tmp is
        # container-local: any roll between here and cleanup — an eviction, a late
        # reconcile, a node event — takes the backup with it, and the registry this
        # deployment was suppressing against is then gone for good.
        backup = f"{os.path.dirname(REGISTRY)}/pubsub_registry.{RUN_ID}.bak"
        in_pod(f"cp {REGISTRY} {backup}")
        log("  set the existing dedup registry aside")
    in_pod(f"rm -f {REGISTRY}")
    return backup


def main() -> int:
    backup = preflight()
    workload_a = f"dedupe2e-a-{RUN_ID[-6:]}"
    workload_b = f"dedupe2e-b-{RUN_ID[-6:]}"
    try:
        log("1. A new incident is filed")
        t0 = time.time()
        publish(alert(workload_a))
        wait_for_task(1, "new incident")
        log("   ✓ filed as a kanban task")

        log("2. The identical alert is suppressed")
        publish(alert(workload_a))
        expect_no_new_task(1, "identical alert")
        if not log_says("duplicate message detected", int(time.time() - t0) + 30):
            die("no 'Duplicate message detected' in the gateway log: the alert was dropped "
                "somewhere else, which is not the same thing as being deduplicated")
        log("   ✓ suppressed, and the log says why")

        log("3. The same incident, different retry detail, is suppressed")
        publish(alert(workload_a, zone="us-east1-c",
                      reason="no.scale.up.nap.pod.zonal.failing.predicates"))
        publish(alert(workload_a, zone="us-east1-d",
                      reason="no.scale.up.nap.pod.zonal.illegal.config"))
        expect_no_new_task(1, "retry variants")
        log("   ✓ one stockout stays one investigation across autoscaler retries")

        log("4. A different workload is its own incident")
        publish(alert(workload_b))
        tasks = wait_for_task(2, "second workload")
        log(f"   ✓ filed separately ({len(tasks)} tasks for this run)")

        log("5. An alert naming no workload never reaches dedup")
        t5 = time.time()
        publish(summary_alert())
        expect_no_new_task(2, "summary alert")
        if not log_says("filtered out by expression", int(time.time() - t5) + 30):
            die("the summary alert produced no task, but the log does not show the filter "
                "rejecting it — check whether it was silently lost instead")
        log("   ✓ dropped by the filter")

        log("")
        log("ALL DEDUP E2E CHECKS PASSED")
        return 0
    finally:
        cleanup(backup)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AssertionError as exc:
        log(f"FAILED: {exc}")
        sys.exit(1)
