#!/usr/bin/env python3
"""E2E: the credential boundary between the agent, the sandbox and the broker.

REQUIRES A LIVE CLUSTER. There is no CI job for this file and there cannot be a
useful one without running Pods: every assertion here is about what the kernel
shows one container about another, which is precisely the thing a rendered
manifest cannot tell you. The operator's Go tests assert the inputs -- the
broker absent from both other Pods, broker-private volumes, `RunAsNonRoot` --
and this asserts the consequence.

It is deliberately cheap to run compared with agentplugins_e2e_test.py: no image
build, no registry, no writes of any kind. It reads `/proc` and `kubectl get`.
It does not modify the namespace and is safe against a cluster you care about.

    KUBE_CONTEXT=gke_my-project_europe-west1_ka-dev-mgmt \\
    NAMESPACE=kubeagents-system \\
    python3 tests/e2e/operator/credential_isolation_e2e_test.py

Exit status 0 means every check passed. Any failure prints the check that failed
and exits 1.

Three Pods, and the boundary is between them:

  * the gateway Pod, which runs the model
  * the shell sandbox Pod, which runs the code the model writes
  * the credential broker Pod, which holds every credential neither of the
    other two is allowed to see

Why each check exists:

  1. The broker being a Pod of its own is the whole control, so it is asserted
     structurally first: absent from the gateway Pod's container list, absent
     from the sandbox's, present as its own workload. A broker that crept back
     into either Pod would leave the rest of this file passing for the wrong
     reason -- loopback and a shared /proc are only expressible inside one Pod.

  2. Distinct Pod IPs. GKE resolves Workload Identity by Pod IP, so this is the
     fact the credential separation rests on: a container that shares an IP with
     the broker can curl 169.254.169.254 and mint the broker's own GSA token,
     whatever else is in the way.

  3. No broker process is visible in either other container's /proc, and no
     /proc/<pid>/environ they can read carries the broker's marker variable.
     Separate Pods should make both vacuous; they are asserted anyway, because
     "should" here is a CRI behaviour rather than a guarantee, and because the
     scan is what would notice a second credential holder this file does not
     know to look for by name.

  4. The broker's HOME and its backend socket directory are on emptyDirs neither
     other Pod mounts. kubectl reads $HOME/.kube/kuberc with no flag at all and a
     kuberc can set `as`, so a reachable broker HOME is caller-supplied
     impersonation through a file. The render-time half of this is pinned in the
     operator's manifest tests; this is the runtime half.

Not covered: what the metadata server answers each Pod with. That is the
consequence check 2 exists for, and asserting it directly needs an HTTP client
in the sandbox, which is exactly what the sandbox image does not ship.
"""

import json
import os
import subprocess
import sys

AGENT_CONTAINER = "platform-agent"
SANDBOX_CONTAINER = "shell"
BROKER_CONTAINER = "envoy-credential-proxy"
# Set for the broker and for no other container. Used as the marker for "this
# environ belongs to the credential holder".
BROKER_ONLY_ENV_MARKER = "CREDENTIAL_PROXY_STATE_DIR"
BROKER_STATE_DIR = "/var/lib/credential-proxy"
BROKER_RUNTIME_DIR = "/var/run/credential-proxy"

# credentialProxySelector in the operator is the source. The gateway and the
# sandbox are found by the container they run instead: their "app" labels are
# derived from the PlatformAgent's name, which this file does not know.
BROKER_LABEL = "kubeagents.x-k8s.io/component=credential-proxy"

failures: list[str] = []

# Resolved in main(). Importing this module must not touch a cluster or exit:
# a linter, or a discovery run whose pattern someone widens, would otherwise
# take the whole process down with it.
KUBE_CONTEXT = ""
NAMESPACE = ""
GATEWAY_POD = ""
SANDBOX_POD = ""
BROKER_POD = ""


def require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        sys.exit(f"Environment variable {name!r} must be set.")
    return value


def kubectl(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["kubectl", "--context", KUBE_CONTEXT, "-n", NAMESPACE, *args],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )


def exec_in(pod: str, container: str, script: str) -> subprocess.CompletedProcess[str]:
    return kubectl(["exec", pod, "-c", container, "--", "sh", "-c", script])


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {name}", flush=True)
    if not ok:
        if detail:
            print(f"      {detail.rstrip()}", flush=True)
        failures.append(name)


def containers_of(pod: str) -> list[str]:
    """Both lists: a native sidecar is an init container, not an app container."""
    result = kubectl(
        [
            "get",
            "pod",
            pod,
            "-o",
            "jsonpath={.spec.initContainers[*].name} {.spec.containers[*].name}",
        ]
    )
    return result.stdout.split()


def find_running_pod(selector: str, what: str) -> str:
    result = kubectl(
        [
            "get",
            "pods",
            "-l",
            selector,
            "--field-selector=status.phase=Running",
            "-o",
            "jsonpath={.items[0].metadata.name}",
        ]
    )
    pod = result.stdout.strip()
    if not pod:
        sys.exit(
            f"No running {what} Pod ({selector}) in namespace {NAMESPACE}.\n"
            f"kubectl said: {result.stderr.strip() or '(no output)'}\n"
            "Nothing was verified."
        )
    return pod


def find_pod_running_container(container: str, what: str) -> str:
    """Locate a Pod by the container it runs, for the two with no stable label."""
    result = kubectl(["get", "pods", "--field-selector=status.phase=Running", "-o", "json"])
    if result.returncode != 0:
        sys.exit(f"kubectl get pods failed: {result.stderr.strip()}\nNothing was verified.")
    for item in json.loads(result.stdout or "{}").get("items", []):
        spec = item.get("spec", {})
        names = [c["name"] for c in spec.get("initContainers", []) + spec.get("containers", [])]
        if container in names:
            return item["metadata"]["name"]
    sys.exit(
        f"No running Pod in {NAMESPACE} has a {container!r} container, so the {what} Pod "
        "could not be found. Nothing was verified."
    )


def pod_ip(pod: str) -> str:
    return kubectl(["get", "pod", pod, "-o", "jsonpath={.status.podIP}"]).stdout.strip()


def check_the_broker_is_a_pod_of_its_own() -> None:
    """The layout assertion, and the reason the rest of the file means anything."""
    for pod, what in ((GATEWAY_POD, "gateway"), (SANDBOX_POD, "sandbox")):
        names = containers_of(pod)
        check(
            f"the {what} Pod has no {BROKER_CONTAINER} container",
            bool(names) and BROKER_CONTAINER not in names,
            f"containers={names}",
        )
    check(
        f"the broker Pod {BROKER_POD} runs {BROKER_CONTAINER}",
        BROKER_CONTAINER in containers_of(BROKER_POD),
        f"containers={containers_of(BROKER_POD)}",
    )


def check_the_pod_ips_differ() -> None:
    """Workload Identity resolves by Pod IP, so a shared IP is a shared identity."""
    ips = {name: pod_ip(pod) for name, pod in (
        ("gateway", GATEWAY_POD),
        ("sandbox", SANDBOX_POD),
        ("broker", BROKER_POD),
    )}
    check(
        "the gateway, sandbox and broker Pods have three distinct IPs",
        all(ips.values()) and len(set(ips.values())) == 3,
        f"observed {ips}",
    )


def check_the_broker_is_actually_running() -> None:
    """Ordering matters: an invisible broker and a dead broker look identical.

    Run before the /proc scans so a CrashLoopBackOff cannot make them pass for
    the wrong reason.
    """
    result = exec_in(
        BROKER_POD,
        BROKER_CONTAINER,
        "for p in /proc/[0-9]*; do tr '\\0' ' ' < $p/cmdline 2>/dev/null; echo; done",
    )
    processes = result.stdout
    check(
        "the broker container is running the credential proxy (otherwise the scans below are vacuous)",
        "credential_proxy.py" in processes,
        f"exit={result.returncode} saw={processes.strip()!r}",
    )


def check_no_broker_process_is_visible() -> None:
    """An absence, so it needs a positive control: the scan has to have scanned.

    If the /proc/[0-9]* glob yields nothing under a container's sh, `leaked` is
    empty for a reason that has nothing to do with isolation, and without
    `visible` this would pass.
    """
    for pod, container, what in (
        (GATEWAY_POD, AGENT_CONTAINER, "agent"),
        (SANDBOX_POD, SANDBOX_CONTAINER, "sandbox"),
    ):
        result = exec_in(
            pod,
            container,
            "for p in /proc/[0-9]*; do tr '\\0' ' ' < $p/cmdline 2>/dev/null; echo; done",
        )
        visible = [line for line in result.stdout.splitlines() if line.strip()]
        leaked = [
            line for line in visible if "credential_proxy.py" in line or "envoy" in line
        ]
        check(
            f"the {what} container's /proc scan returned processes (otherwise the next check is vacuous)",
            result.returncode == 0 and bool(visible),
            f"exit={result.returncode} stderr={result.stderr.strip()}",
        )
        check(
            f"no broker process is visible in the {what} container's /proc",
            result.returncode == 0 and not leaked,
            f"exit={result.returncode} leaked={leaked}",
        )


def check_no_credential_environ_is_readable() -> None:
    """The consequence, asserted directly rather than inferred from the above.

    grep -l over every readable environ. A hit means the container can read the
    environment of a process that holds credentials, whichever process it is --
    including one this file does not know to look for by name.

    The positive control matters more here than anywhere else in this file.
    This is the only check whose external tool is exercised nowhere else -- `tr`
    fails loudly through the scan above if it is missing, `grep` would not --
    and an absence-of-output assertion cannot tell "nothing to find" from
    "nothing ran". So: run the identical command in the broker container, where
    the marker is in its own environ and a hit is guaranteed, and require one.
    That catches a missing grep, a renamed environment variable, and a kubectl
    exec that failed for any reason, none of which the negative half can see.
    """
    scan = f"grep -l {BROKER_ONLY_ENV_MARKER} /proc/[0-9]*/environ 2>/dev/null; true"

    control = exec_in(BROKER_POD, BROKER_CONTAINER, scan)
    control_hits = [line for line in control.stdout.splitlines() if line.strip()]
    check(
        f"the same scan finds {BROKER_ONLY_ENV_MARKER} in the broker container (positive control)",
        control.returncode == 0 and bool(control_hits),
        f"exit={control.returncode} stdout={control.stdout.strip()!r} "
        f"stderr={control.stderr.strip()} -- grep missing, or the variable was renamed",
    )

    for pod, container, what in (
        (GATEWAY_POD, AGENT_CONTAINER, "agent"),
        (SANDBOX_POD, SANDBOX_CONTAINER, "sandbox"),
    ):
        result = exec_in(pod, container, scan)
        hits = [line for line in result.stdout.splitlines() if line.strip()]
        check(
            f"no /proc/<pid>/environ readable by the {what} carries {BROKER_ONLY_ENV_MARKER}",
            result.returncode == 0 and not hits,
            f"exit={result.returncode} stderr={result.stderr.strip()} "
            f"readable credential environs: {hits}",
        )


def check_the_broker_private_directories_are_out_of_reach() -> None:
    """The runtime half of the broker-private volume assertions.

    Absent from the mount table is the assertion, not absent from the
    filesystem: an empty directory of the same name would be a pass either way,
    and it is the mount that decides whether the bytes are shared.
    """
    for pod, container, what in (
        (GATEWAY_POD, AGENT_CONTAINER, "agent"),
        (SANDBOX_POD, SANDBOX_CONTAINER, "sandbox"),
    ):
        result = exec_in(pod, container, "cat /proc/self/mounts")
        mounts = result.stdout
        for directory, why in (
            (BROKER_STATE_DIR, "the broker's HOME, where kubectl reads .kube/kuberc"),
            (BROKER_RUNTIME_DIR, "the backend socket, which is the credentials"),
        ):
            check(
                f"the {what} container does not mount {directory} ({why})",
                result.returncode == 0
                and not any(f" {directory} " in line for line in mounts.splitlines()),
                f"exit={result.returncode} mounts={mounts.strip()}",
            )


def main() -> None:
    global KUBE_CONTEXT, NAMESPACE, GATEWAY_POD, SANDBOX_POD, BROKER_POD
    KUBE_CONTEXT = require_env("KUBE_CONTEXT")
    NAMESPACE = require_env("NAMESPACE")
    BROKER_POD = find_running_pod(BROKER_LABEL, "credential broker")
    GATEWAY_POD = find_pod_running_container(AGENT_CONTAINER, "gateway")
    SANDBOX_POD = find_pod_running_container(SANDBOX_CONTAINER, "shell sandbox")

    print(f"Namespace: {NAMESPACE}")
    print(f"Gateway:   {GATEWAY_POD}")
    print(f"Sandbox:   {SANDBOX_POD}")
    print(f"Broker:    {BROKER_POD}\n")
    check_the_broker_is_a_pod_of_its_own()
    check_the_pod_ips_differ()
    check_the_broker_is_actually_running()
    check_no_broker_process_is_visible()
    check_no_credential_environ_is_readable()
    check_the_broker_private_directories_are_out_of_reach()
    print()
    if failures:
        print(f"{len(failures)} check(s) FAILED:")
        for name in failures:
            print(f"  - {name}")
        sys.exit(1)
    print("The credential boundary holds at runtime.")


if __name__ == "__main__":
    main()
