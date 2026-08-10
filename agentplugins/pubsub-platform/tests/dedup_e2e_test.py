#!/usr/bin/env python3
"""End-to-end suppression test against a live Hermes deployment.

    KUBE_CONTEXT=<kubectl context of the cluster running the agent> \
    GCP_PROJECT_ID=<project> TARGET_CLUSTER_NAME=<cluster the plugin watches> \
    python3 agentplugins/pubsub-platform/tests/dedup_e2e_test.py

The unit tests next door prove the adapter's logic in isolation. They cannot prove that
the logic is *reached*: the route config that ships in the AgentPlugin, the log filter,
the dedup fields and the dispatch mode all live outside the code, and every one of them
has already been wrong at least once on a real deployment — dedup switched off by a stray
environment variable, a filter expression that silently evaluated to empty, dedup fields
that keyed on per-retry detail. This publishes real messages to the real topic and reads
what the running adapter did with them.

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

Environment, all required and none defaulted to a particular fleet: KUBE_CONTEXT,
GCP_PROJECT_ID, TARGET_CLUSTER_NAME (must match the deployed route's filter, or every
alert is dropped and each assertion fails for the wrong reason), TARGET_CLUSTER_LOCATION.
Optional: NAMESPACE, STOCKOUT_TOPIC, STOCKOUT_ROUTE, SETTLE_SECONDS.
"""

import json
import os
import random
import subprocess
import sys
import time
from datetime import datetime

KUBE_CONTEXT = os.environ.get("KUBE_CONTEXT", "")
PROJECT_ID = os.environ.get("GCP_PROJECT_ID", "")
NAMESPACE = os.environ.get("NAMESPACE", "kubeagents-system")
DEPLOYMENT = os.environ.get("GATEWAY_DEPLOYMENT", "platform-agent-gateway")
TOPIC = os.environ.get("STOCKOUT_TOPIC", "gke-stockout-alerts-topic")
ROUTE = os.environ.get("STOCKOUT_ROUTE", "gke_stockout_alerts")
CLUSTER = os.environ.get("TARGET_CLUSTER_NAME", "")
LOCATION = os.environ.get("TARGET_CLUSTER_LOCATION", "")
REGISTRY = "/opt/data/pubsub_registry.json"

# How long to wait for the adapter to act on a published message. The subscriber pulls
# continuously; a few seconds is plenty, but the board's dispatcher adds a tick.
SETTLE_SECONDS = int(os.environ.get("SETTLE_SECONDS", "45"))

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


def agent_pod() -> str:
    pod = kubectl([
        "get", "pod", "-n", NAMESPACE, "-l", f"app={DEPLOYMENT}",
        "--field-selector=status.phase=Running", "-o", "jsonpath={.items[0].metadata.name}",
    ]).strip()
    if not pod:
        die(f"no Running {DEPLOYMENT} pod in {NAMESPACE}")
    return pod


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
        in_pod(f"cp {registry_backup} {REGISTRY} && rm -f {registry_backup}", check=False)
        log("  restored the dedup registry that was there before")
    else:
        in_pod(f"rm -f {REGISTRY}", check=False)
        log("  removed the registry (there was none before)")


# --------------------------------------------------------------------------- main

def preflight() -> str:
    missing = [n for n, v in (("KUBE_CONTEXT", KUBE_CONTEXT), ("GCP_PROJECT_ID", PROJECT_ID),
                              ("TARGET_CLUSTER_NAME", CLUSTER), ("TARGET_CLUSTER_LOCATION", LOCATION))
               if not v]
    if missing:
        # No defaults: the cluster name has to match the deployed route's filter, or every
        # published alert is dropped and each assertion fails for the wrong reason.
        die(f"set {', '.join(missing)}")
    log(f"Run marker: {RUN_ID}")

    route = in_pod(
        "/opt/hermes/.venv/bin/python3 -c \""
        "import json, yaml;"
        "c = yaml.safe_load(open('/opt/data/config.yaml'));"
        f"r = c['platforms']['pubsub']['extra']['subscriptions']['{ROUTE}'];"
        "print(json.dumps({'dedup': bool(r.get('deduplicate_fields')),"
        " 'dispatch': r.get('dispatch', 'api'), 'filter': bool(r.get('filter'))}))\"")
    cfg = json.loads(route.strip())
    if not cfg["dedup"]:
        die(f"route '{ROUTE}' has no deduplicate_fields; nothing to test")
    if not cfg["filter"]:
        die(f"route '{ROUTE}' has no filter; step 5 would be meaningless")
    log(f"  route '{ROUTE}': dedup on, dispatch={cfg['dispatch']}")

    disabled = in_pod("echo ${DISABLE_PUBSUB_DEDUP:-unset}").strip()
    if disabled.lower() == "true":
        die("DISABLE_PUBSUB_DEDUP=true on the pod: dedup is switched off, so this test "
            "would pass or fail for the wrong reason. Remove it and re-run.")
    log(f"  DISABLE_PUBSUB_DEDUP={disabled}")

    backup = ""
    if in_pod(f"test -f {REGISTRY} && echo yes || echo no").strip() == "yes":
        backup = f"/tmp/pubsub_registry.{RUN_ID}.bak"
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
