#!/usr/bin/env python3
"""E2E: the credential boundary between the sandbox and the credential broker.

REQUIRES A LIVE CLUSTER. There is no CI job for this file and there cannot be a
useful one without a running agent Pod: every assertion here is about what the
kernel shows one container about another, which is precisely the thing a
rendered manifest cannot tell you. The operator's Go tests assert the inputs --
`shareProcessNamespace` unset, split UIDs, broker-private volumes -- and this
asserts the consequence.

It is deliberately cheap to run compared with agentplugins_e2e_test.py: no image
build, no registry, no writes of any kind. It reads `/proc` and runs `id`. It
does not modify the namespace and is safe against a cluster you care about.

    KUBE_CONTEXT=gke_my-project_europe-west1_ka-dev-mgmt \\
    NAMESPACE=kubeagents-system \\
    python3 tests/e2e/operator/credential_isolation_e2e_test.py

Exit status 0 means every check passed. Any failure prints the check that failed
and exits 1.

Why each check exists:

  1. A shared process namespace puts the broker's /proc/<pid>/environ -- where
     its credentials are -- inside a directory the sandbox can read, and that is
     reachable by a prompt-injected model with no process compromise at all. The
     operator no longer sets the manifest field; these two checks are what prove
     the runtime followed. `shareProcessNamespace: true` is observable from inside a
     container in two ways, and both are asserted, because either could be true
     while the other is masked by a CRI quirk: PID 1 becomes the infra
     ("pause") process rather than the container's own entrypoint, and the
     broker's processes become visible in the agent's /proc.

  2. The two controls are independent and both are needed. Reading another
     process's /proc/<pid>/environ takes ptrace-level access, which the kernel
     grants on a uid match; seeing the pid at all takes a shared namespace.
     Splitting the UIDs removes the first even if the namespace ever comes
     back, which is why the UIDs are checked here and not only at render time.

  3. The broker's HOME and its backend socket directory are on emptyDirs the
     agent container does not mount. kubectl reads $HOME/.kube/kuberc with no
     flag at all and a kuberc can set `as`, so a writable broker HOME is
     caller-supplied impersonation through a file. The render-time half of this
     is pinned in the operator's manifest tests; this is the runtime half.
"""

import os
import subprocess
import sys

AGENT_CONTAINER = "platform-agent"
# The broker is a sidecar in the default layout and a Pod of its own behind
# spec.security.splitCredentialBrokerPod. Only the sidecar layout puts the two
# in one Pod, which is the only layout where a shared process namespace is even
# expressible -- so this suite targets it, and says so rather than passing
# vacuously against a split install.
BROKER_CONTAINER = "envoy-credential-proxy"
# From the operator: sandboxUID and credentialProxyUID.
EXPECTED_AGENT_UID = "10000"
EXPECTED_BROKER_UID = "10001"
# Set for the broker and for no other container. Used as the marker for "this
# environ belongs to the credential holder".
BROKER_ONLY_ENV_MARKER = "CREDENTIAL_PROXY_STATE_DIR"
BROKER_STATE_DIR = "/var/lib/credential-proxy"
BROKER_RUNTIME_DIR = "/var/run/credential-proxy"

failures: list[str] = []

# Resolved in main(). Importing this module must not touch a cluster or exit:
# a linter, or a discovery run whose pattern someone widens, would otherwise
# take the whole process down with it.
KUBE_CONTEXT = ""
NAMESPACE = ""
POD = ""


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


def exec_in(container: str, script: str) -> subprocess.CompletedProcess[str]:
    return kubectl(["exec", POD, "-c", container, "--", "sh", "-c", script])


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {name}", flush=True)
    if not ok:
        if detail:
            print(f"      {detail.rstrip()}", flush=True)
        failures.append(name)


def find_agent_pod() -> str:
    result = kubectl(
        [
            "get",
            "pods",
            "-l",
            "kubeagents.x-k8s.io/has-credential-proxy=true",
            "--field-selector=status.phase=Running",
            "-o",
            "jsonpath={.items[0].metadata.name}",
        ]
    )
    pod = result.stdout.strip()
    if not pod:
        sys.exit(
            f"No running Pod with the credential proxy in namespace {NAMESPACE}.\n"
            f"kubectl said: {result.stderr.strip() or '(no output)'}"
        )
    return pod


def assert_the_sidecar_layout() -> None:
    """Refuse to report success against a layout these checks do not cover."""
    # Both lists, because the broker is a native sidecar: it carries
    # restartPolicy: Always and so is an init container, not an app container.
    result = kubectl(
        [
            "get",
            "pod",
            POD,
            "-o",
            "jsonpath={.spec.initContainers[*].name} {.spec.containers[*].name}",
        ]
    )
    names = result.stdout.split()
    if BROKER_CONTAINER not in names:
        sys.exit(
            f"Pod {POD} has no {BROKER_CONTAINER} container (containers: {names}).\n"
            "This suite asserts the sidecar layout, where agent and broker share a Pod.\n"
            "With splitCredentialBrokerPod enabled there is no shared process namespace to\n"
            "check, and passing here would mean nothing. Nothing was verified."
        )


def check_pid_one_is_not_the_infra_process() -> None:
    """shareProcessNamespace: true makes the pause binary PID 1 in every container."""
    for container in (AGENT_CONTAINER, BROKER_CONTAINER):
        result = exec_in(container, "tr '\\0' ' ' < /proc/1/cmdline")
        cmdline = result.stdout.strip()
        check(
            f"{container}: PID 1 is the container's own entrypoint, not the Pod infra process",
            bool(cmdline) and "pause" not in cmdline,
            f"exit={result.returncode} cmdline={cmdline!r} stderr={result.stderr.strip()}",
        )


def check_the_broker_is_actually_running() -> None:
    """Ordering matters: an invisible broker and a dead broker look identical.

    Run before the /proc scan so a CrashLoopBackOff cannot make the next check
    pass for the wrong reason.
    """
    result = exec_in(
        BROKER_CONTAINER,
        "for p in /proc/[0-9]*; do tr '\\0' ' ' < $p/cmdline 2>/dev/null; echo; done",
    )
    processes = result.stdout
    check(
        "the broker container is running the credential proxy (otherwise the scan below is vacuous)",
        "credential_proxy.py" in processes,
        f"exit={result.returncode} saw={processes.strip()!r}",
    )


def check_the_agent_cannot_see_the_broker() -> None:
    """An absence, so it needs a positive control: the scan has to have scanned.

    Same guard the broker side gets above. If the /proc/[0-9]* glob yields
    nothing under this container's sh, `leaked` is empty for a reason that has
    nothing to do with isolation, and without `visible` this would pass.
    """
    result = exec_in(
        AGENT_CONTAINER,
        "for p in /proc/[0-9]*; do tr '\\0' ' ' < $p/cmdline 2>/dev/null; echo; done",
    )
    visible = [line for line in result.stdout.splitlines() if line.strip()]
    leaked = [
        line
        for line in visible
        if "credential_proxy.py" in line or "envoy" in line or "envoy-credential-sidecar" in line
    ]
    check(
        "the agent container's /proc scan returned processes (otherwise the check below is vacuous)",
        result.returncode == 0 and bool(visible),
        f"exit={result.returncode} stderr={result.stderr.strip()}",
    )
    check(
        "no broker process is visible in the agent container's /proc",
        result.returncode == 0 and not leaked,
        f"exit={result.returncode} leaked={leaked}",
    )


def check_the_agent_cannot_read_a_credential_environ() -> None:
    """The consequence, asserted directly rather than inferred from the above.

    grep -l over every readable environ. A hit means the sandbox can read the
    environment of a process that holds credentials, whichever process it is --
    including one this file does not know to look for by name.

    The positive control matters more here than anywhere else in this file.
    This is the only check whose external tool is exercised nowhere else -- `tr`
    fails loudly through check 1 if it is missing, `grep` would not -- and an
    absence-of-output assertion cannot tell "nothing to find" from "nothing
    ran". So: run the identical command in the broker container, where the
    marker is in its own environ and a hit is guaranteed, and require one. That
    catches a missing grep, a renamed environment variable, and a kubectl exec
    that failed for any reason, none of which the negative half can see.
    """
    scan = f"grep -l {BROKER_ONLY_ENV_MARKER} /proc/[0-9]*/environ 2>/dev/null; true"

    control = exec_in(BROKER_CONTAINER, scan)
    control_hits = [line for line in control.stdout.splitlines() if line.strip()]
    check(
        f"the same scan finds {BROKER_ONLY_ENV_MARKER} in the broker container (positive control)",
        control.returncode == 0 and bool(control_hits),
        f"exit={control.returncode} stdout={control.stdout.strip()!r} "
        f"stderr={control.stderr.strip()} -- grep missing, or the variable was renamed",
    )

    result = exec_in(AGENT_CONTAINER, scan)
    hits = [line for line in result.stdout.splitlines() if line.strip()]
    check(
        f"no /proc/<pid>/environ readable by the agent carries {BROKER_ONLY_ENV_MARKER}",
        result.returncode == 0 and not hits,
        f"exit={result.returncode} stderr={result.stderr.strip()} "
        f"readable credential environs: {hits}",
    )


def check_the_users_differ() -> None:
    users = {}
    for container in (AGENT_CONTAINER, BROKER_CONTAINER):
        result = exec_in(container, "id -u")
        users[container] = result.stdout.strip()
    check(
        f"the agent runs as {EXPECTED_AGENT_UID} and the broker as {EXPECTED_BROKER_UID}",
        users[AGENT_CONTAINER] == EXPECTED_AGENT_UID
        and users[BROKER_CONTAINER] == EXPECTED_BROKER_UID,
        f"observed {users}",
    )


def check_the_broker_private_directories_are_out_of_reach() -> None:
    """The runtime half of the broker-private volume assertions.

    Absent from the agent's mount table is the assertion, not absent from its
    filesystem: an empty directory of the same name would be a pass either way,
    and it is the mount that decides whether the bytes are shared.
    """
    result = exec_in(AGENT_CONTAINER, "cat /proc/self/mounts")
    mounts = result.stdout
    for directory, why in (
        (BROKER_STATE_DIR, "the broker's HOME, where kubectl reads .kube/kuberc"),
        (BROKER_RUNTIME_DIR, "the backend socket, which is the credentials"),
    ):
        check(
            f"the agent container does not mount {directory} ({why})",
            result.returncode == 0
            and not any(f" {directory} " in line for line in mounts.splitlines()),
            f"exit={result.returncode} mounts={mounts.strip()}",
        )


def main() -> None:
    global KUBE_CONTEXT, NAMESPACE, POD
    KUBE_CONTEXT = require_env("KUBE_CONTEXT")
    NAMESPACE = require_env("NAMESPACE")
    POD = find_agent_pod()

    print(f"Pod:       {POD}")
    print(f"Namespace: {NAMESPACE}\n")
    assert_the_sidecar_layout()
    check_pid_one_is_not_the_infra_process()
    check_the_broker_is_actually_running()
    check_the_agent_cannot_see_the_broker()
    check_the_agent_cannot_read_a_credential_environ()
    check_the_users_differ()
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
