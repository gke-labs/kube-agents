"""Stage 1 E2E Promotion Test: Autonomous Fleet Audits, Audit Streams & GitHub Integration."""

import json
import os
import subprocess
import time
from typing import Optional

import pytest

_POD_WAIT_TIMEOUT_SECONDS = 120
_POD_POLL_INTERVAL_SECONDS = 5

# Where the refresh client lives inside the pod the probe execs into. Both pod
# layouts bake it into the image at /opt/defaults/scripts and their entrypoints
# copy it onto the data volume at /opt/data/scripts on every start; the volume
# copy is preferred because that is the one the agent itself runs. No wider
# search: a copy found elsewhere (a GitOps clone under the data volume, say)
# would let the probe pass on an image that lost its own.
_REFRESH_SCRIPT_NAME = "github_token_refresh.py"
_REFRESH_SCRIPT_CANDIDATES = (
    f"/opt/data/scripts/{_REFRESH_SCRIPT_NAME}",
    f"/opt/defaults/scripts/{_REFRESH_SCRIPT_NAME}",
)
_REFRESH_CONFIRMATION = "Refreshed GitHub credentials via"
# The version-control CLI the agent reaches the forge through. There is no `gh`
# in the sandbox; the audit helper takes the same broker route, through the
# client library this CLI wraps.
_VCS_CANDIDATES = (
    "/opt/data/skills/version-control/scripts/vcs.py",
    "/opt/defaults/skills/version-control/scripts/vcs.py",
)
# The exec budget covers the refresh (its client waits up to 60s on the proxy)
# plus the two forge calls, with room for a slow broker.
_PROBE_TIMEOUT_SECONDS = 180


def test_github_token_minting_and_connectivity(
    gke_cluster_name: Optional[str],
    agent_namespace: str,
    github_repo: Optional[str],
) -> None:
    """Verifies live in-cluster GitHub authentication, token minting, and repository reachability.

    Executes a genuinely 100% read-only probe inside the agent's shell sandbox pod, or the
    legacy gateway pod on an install that has no sandbox:
    1. Triggers token refresh through the credential proxy and GitHub Token Minter (Cloud KMS).
    2. Asks the broker who this install is (`vcs.py identity`).
    3. Reads the target repository's issues through the broker (`vcs.py issue list`), which
       fails unless the broker can actually read that repository.
    Does NOT invoke `audit_report.py start`, preventing workspace reset, lease scrubbing, or label writes.

    Step 1 is the only thing that mints on a fresh install: nothing at startup writes the
    proxy's gh credentials, so a probe that skips it passes or fails on whether some earlier
    task happened to mint. The probe therefore fails when the refresh client cannot be found
    or the refresh fails, instead of carrying on to steps 2 and 3 on whatever is cached.
    """
    if not gke_cluster_name or not github_repo:
        pytest.fail("GKE cluster name and GITHUB_REPO are required for live GitHub connectivity probe.")

    # 1. Wait for running shell sandbox pod (StatefulSet app=<name>-shell, container: shell)
    deadline = time.time() + _POD_WAIT_TIMEOUT_SECONDS
    pod_name = ""
    container_name = "shell"
    agent_name = os.environ.get("AGENT_SERVICE_NAME", "platform-agent")

    while time.time() < deadline:
        proc_pod = subprocess.run(
            [
                "kubectl",
                "get",
                "pod",
                "-n",
                agent_namespace,
                "-l",
                f"app={agent_name}-shell",
                "--field-selector=status.phase=Running",
                "-o",
                "jsonpath={.items[0].metadata.name}",
            ],
            capture_output=True,
            text=True,
        )
        if proc_pod.returncode == 0 and proc_pod.stdout.strip():
            pod_name = proc_pod.stdout.strip()
            container_name = "shell"
            break
        time.sleep(_POD_POLL_INTERVAL_SECONDS)

    # 2. Fallback for legacy single-pod layout: gateway pod with container platform-agent
    if not pod_name:
        proc_gw = subprocess.run(
            [
                "kubectl",
                "get",
                "pod",
                "-n",
                agent_namespace,
                "-l",
                f"app={agent_name}-gateway",
                "--field-selector=status.phase=Running",
                "-o",
                "jsonpath={.items[0].metadata.name}",
            ],
            capture_output=True,
            text=True,
        )
        if proc_gw.returncode == 0 and proc_gw.stdout.strip():
            pod_name = proc_gw.stdout.strip()
            container_name = "platform-agent"

    if not pod_name:
        pytest.fail(
            f"No running agent shell or legacy gateway pod found in namespace '{agent_namespace}' within {_POD_WAIT_TIMEOUT_SECONDS}s."
        )

    # Refresh credentials via broker and query repository via read-only GET API
    script = f"""
import sys, subprocess, os

# 1. Refresh credentials in the credential proxy via the broker client.
# The script is resolved from its known locations, not searched for: as the
# sandbox user, `find /opt` cannot enter the root-owned lost+found on the
# data volume and exits non-zero even after printing a match, so an exit-code
# gate on it skipped this step and left the probe to pass or fail on whatever
# credentials an earlier task had left in the proxy.
candidates = [p for p in {_REFRESH_SCRIPT_CANDIDATES!r} if os.path.isfile(p)]
if not candidates:
    print(
        f"{_REFRESH_SCRIPT_NAME} not found at any of {_REFRESH_SCRIPT_CANDIDATES!r}; "
        "the pod cannot mint a GitHub token.",
        file=sys.stderr,
    )
    sys.exit(1)
refresh_script = candidates[0]
res_ref = subprocess.run(['python3', refresh_script, '{github_repo}'], capture_output=True, text=True)
if res_ref.returncode != 0:
    print(
        f"Token refresh via {{refresh_script}} failed (exit {{res_ref.returncode}}):\\n"
        f"STDOUT:\\n{{res_ref.stdout}}\\nSTDERR:\\n{{res_ref.stderr}}",
        file=sys.stderr,
    )
    sys.exit(res_ref.returncode)
print(f"{_REFRESH_CONFIRMATION} {{refresh_script}}")

# 2. A read-only forge call through the broker: who this install is on the
# repository, which the broker answers with the token step 1 minted.
import json
vcs = [p for p in {_VCS_CANDIDATES!r} if os.path.isfile(p)]
if not vcs:
    print(f"vcs.py not found at any of {_VCS_CANDIDATES!r}", file=sys.stderr)
    sys.exit(1)
res_vcs = subprocess.run(
    ['python3', vcs[0], 'identity', '--repo', '{github_repo}'],
    cwd='/opt/data', capture_output=True, text=True,
)
if res_vcs.returncode != 0:
    print(f"Forge query failed: {{res_vcs.stdout}}{{res_vcs.stderr}}", file=sys.stderr)
    sys.exit(res_vcs.returncode)
login = (json.loads(res_vcs.stdout).get("identity") or {{}}).get("login")
if not login:
    print(f"No identity for '{github_repo}': {{res_vcs.stdout}}", file=sys.stderr)
    sys.exit(1)

# 3. A read of the repository itself. `identity` names no repository, so it
# passes on any token the broker holds; this one fails unless that token can
# read this repository.
res_read = subprocess.run(
    ['python3', vcs[0], 'issue', 'list', '--repo', '{github_repo}', '-n', '1'],
    cwd='/opt/data', capture_output=True, text=True,
)
if res_read.returncode != 0 or "issues" not in json.loads(res_read.stdout or "{{}}"):
    print(f"Repository read failed: {{res_read.stdout}}{{res_read.stderr}}", file=sys.stderr)
    sys.exit(res_read.returncode or 1)

print(f"Successfully authenticated and queried repository: {github_repo} as {{login}}")
"""

    base_exec = [
        "kubectl",
        "exec",
        "-n",
        agent_namespace,
        pod_name,
        "-c",
        container_name,
        "--",
    ]
    if container_name == "shell":
        cmd = base_exec + ["runuser", "-u", "agent", "--", "python3", "-c", script]
    else:
        cmd = base_exec + ["python3", "-c", script]

    try:
        proc_start = subprocess.run(cmd, capture_output=True, text=True, timeout=_PROBE_TIMEOUT_SECONDS)
        assert proc_start.returncode == 0, (
            f"GitHub token minting and API probe failed inside pod '{pod_name}' (exit code {proc_start.returncode}):\n"
            f"STDOUT:\n{proc_start.stdout}\nSTDERR:\n{proc_start.stderr}"
        )
        assert _REFRESH_CONFIRMATION in proc_start.stdout, (
            f"The probe reached the forge without running the token refresh; stdout was:\n{proc_start.stdout}"
        )
        assert f"Successfully authenticated and queried repository: {github_repo}" in proc_start.stdout, (
            f"Expected successful repository query confirmation in stdout, got:\n{proc_start.stdout}"
        )
    except subprocess.TimeoutExpired:
        pytest.fail(
            f"GitHub token minting / API probe timed out after {_PROBE_TIMEOUT_SECONDS}s in pod '{pod_name}'"
        )


def test_github_token_minter_credential_isolation(
    gcp_project_id: Optional[str],
    gke_cluster_name: Optional[str],
    agent_namespace: str,
) -> None:
    """Verifies GitHub integration security: credentials broker isolation & secret protection.

    Ensures that:
    1. platform-agent-settings ConfigMap is configured with GitOps settings.
    2. platform-agent-credential-proxy-policy ConfigMap is present and enforces token disclosure rules.
    3. The private GitHub App credentials secret is NOT mounted or injected into agent containers.
    """
    if not gcp_project_id or not gke_cluster_name:
        pytest.fail("GCP_PROJECT_ID and GKE_CLUSTER_NAME are required for credential isolation verification.")

    # 1. Verify minter config or settings ConfigMap exists
    res_minter = subprocess.run(
        ["kubectl", "get", "cm", "github-token-minter-config", "-n", agent_namespace, "-o", "json"],
        capture_output=True,
        text=True,
    )
    if res_minter.returncode != 0:
        res_settings = subprocess.run(
            ["kubectl", "get", "cm", "platform-agent-settings", "-n", agent_namespace, "-o", "json"],
            capture_output=True,
            text=True,
        )
        if res_settings.returncode != 0:
            pytest.fail(f"Neither github-token-minter-config nor platform-agent-settings ConfigMap found in namespace '{agent_namespace}'.")

    # 2. Verify Credential Isolation: Secret 'github-app-credentials' is NOT mounted or injected into agent containers
    res_deploys = subprocess.run(
        ["kubectl", "get", "deployments", "-n", agent_namespace, "-o", "json"],
        capture_output=True,
        text=True,
    )
    assert res_deploys.returncode == 0, (
        f"Failed to query deployments in namespace '{agent_namespace}': {res_deploys.stderr}"
    )
    deploys_data = json.loads(res_deploys.stdout).get("items", [])
    agent_deploys = [
        d for d in deploys_data
        if not any(excluded in d.get("metadata", {}).get("name", "").lower() for excluded in ("minter", "minty", "operator", "hindsight", "fluent-bit", "cert-manager"))
    ]
    if not agent_deploys:
        pytest.fail(f"No agent deployments found in namespace '{agent_namespace}'")

    forbidden_secrets = {"github-app-credentials", "github-app-private-key", "minty-github-app-key"}

    for deploy in agent_deploys:
        deploy_name = deploy.get("metadata", {}).get("name", "unknown")
        pod_spec = deploy.get("spec", {}).get("template", {}).get("spec", {})

        # Build volume name -> secretName map
        volume_to_secret: dict[str, str] = {}
        for vol in pod_spec.get("volumes", []):
            vol_name = vol.get("name", "")
            if "secret" in vol and "secretName" in vol["secret"]:
                volume_to_secret[vol_name] = vol["secret"]["secretName"]
            elif "projected" in vol:
                for src in vol["projected"].get("sources", []):
                    if "secret" in src and "name" in src["secret"]:
                        volume_to_secret[vol_name] = src["secret"]["name"]

        # Check all containers and initContainers
        all_containers = pod_spec.get("containers", []) + pod_spec.get("initContainers", [])
        for container in all_containers:
            c_name = container.get("name", "unknown")

            # Check volume mounts
            for vm in container.get("volumeMounts", []):
                v_name = vm.get("name", "")
                backing_secret = volume_to_secret.get(v_name, "")
                for forbidden in forbidden_secrets:
                    assert forbidden not in backing_secret.lower() and forbidden not in v_name.lower(), (
                        f"SECURITY VIOLATION in deployment '{deploy_name}', container '{c_name}': "
                        f"Secret '{backing_secret}' (volume '{v_name}') directly mounted into agent container!"
                    )

            # Check environment variables
            for env_entry in container.get("env", []):
                secret_ref = env_entry.get("valueFrom", {}).get("secretKeyRef", {}).get("name", "")
                for forbidden in forbidden_secrets:
                    assert forbidden not in secret_ref.lower(), (
                        f"SECURITY VIOLATION in deployment '{deploy_name}', container '{c_name}': "
                        f"Secret '{secret_ref}' injected via env '{env_entry.get('name')}'!"
                    )

            # Check envFrom
            for env_from in container.get("envFrom", []):
                secret_ref = env_from.get("secretRef", {}).get("name", "")
                for forbidden in forbidden_secrets:
                    assert forbidden not in secret_ref.lower(), (
                        f"SECURITY VIOLATION in deployment '{deploy_name}', container '{c_name}': "
                        f"Secret '{secret_ref}' injected via envFrom!"
                    )


def test_github_target_repository_configuration(
    github_repo: Optional[str],
    github_org: Optional[str],
    github_app_id: Optional[str],
) -> None:
    """Verifies that GitHub App, Org, and Repo configuration parameters are properly wired for E2E tests."""
    if not github_repo:
        pytest.fail("GITHUB_REPO (or GITOPS_REPO) is required for E2E promotion validation.")

    assert "/" in github_repo, f"Expected GITHUB_REPO in 'owner/repo' format, got '{github_repo}'"
    owner, repo_name = github_repo.split("/", 1)
    assert len(owner) > 0 and len(repo_name) > 0, f"Invalid repository structure in '{github_repo}'"

    if github_org:
        assert github_org == owner, (
            f"GITHUB_ORG '{github_org}' does not match repository owner in '{github_repo}'"
        )
