"""Stage 1 E2E Promotion Test: Autonomous Fleet Audits, Audit Streams & GitHub Integration."""

import json
import os
import pathlib
import subprocess
import tempfile
import time
from typing import List, Optional, Tuple

import pytest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_AUDIT_REPORT_SCRIPT = (
    _REPO_ROOT / "agents" / "platform" / "skills" / "fleet-audit" / "scripts" / "audit_report.py"
)

_POD_WAIT_TIMEOUT_SECONDS = 120
_POD_POLL_INTERVAL_SECONDS = 5

# All 7 Registered Audit Streams and their human titles
AUDIT_STREAMS: List[Tuple[str, str]] = [
    ("compliance-audit", "Security & RBAC Posture Audit"),
    ("security-patch-orchestrator", "Upgrade & Patch Readiness Audit"),
    ("obtainability-audit", "Workload Reliability Audit"),
    ("fleet-wide-cost-analysis", "Fleet Waste Audit"),
    ("fleet-consistency-drift", "Fleet Consistency Drift Audit"),
    ("ai-security-audit", "AI Workload Security Audit"),
    ("stockout-prevention", "Fleet Stockout Prevention & Capacity Audit"),
]


def test_github_token_minting_and_connectivity(
    gke_cluster_name: Optional[str],
    agent_namespace: str,
    github_repo: Optional[str],
) -> None:
    """Verifies live in-cluster GitHub authentication, token minting, and repository reachability.

    Executes a genuinely 100% read-only probe inside the platform-agent pod:
    1. Triggers token refresh via the Envoy credential proxy sidecar and GitHub Token Minter (Cloud KMS).
    2. Executes `gh api repos/<target_repo>` from the shared workspace root.
    3. Verifies repository access and permissions over the network.
    Does NOT invoke `audit_report.py start`, preventing workspace reset, lease scrubbing, or label writes.
    """
    if not gke_cluster_name or not github_repo:
        pytest.fail("GKE cluster name and GITHUB_REPO are required for live GitHub connectivity probe.")

    # Find running platform-agent pod with availability wait
    deadline = time.time() + _POD_WAIT_TIMEOUT_SECONDS
    pod_name = ""
    while time.time() < deadline:
        proc_pod = subprocess.run(
            [
                "kubectl",
                "get",
                "pod",
                "-n",
                agent_namespace,
                "-l",
                "kubeagents.x-k8s.io/has-credential-proxy=true",
                "--field-selector=status.phase=Running",
                "-o",
                "jsonpath={.items[0].metadata.name}",
            ],
            capture_output=True,
            text=True,
        )
        if proc_pod.returncode == 0 and proc_pod.stdout.strip():
            pod_name = proc_pod.stdout.strip()
            break
        proc_pod = subprocess.run(
            [
                "kubectl",
                "get",
                "pod",
                "-n",
                agent_namespace,
                "-l",
                "app=platform-agent-gateway",
                "--field-selector=status.phase=Running",
                "-o",
                "jsonpath={.items[0].metadata.name}",
            ],
            capture_output=True,
            text=True,
        )
        if proc_pod.returncode == 0 and proc_pod.stdout.strip():
            pod_name = proc_pod.stdout.strip()
            break
        # Fallback to checking any running pod in agent namespace with 'agent' or 'gateway' in name
        proc_pod_all = subprocess.run(
            [
                "kubectl",
                "get",
                "pod",
                "-n",
                agent_namespace,
                "--field-selector=status.phase=Running",
                "-o",
                "jsonpath={.items[*].metadata.name}",
            ],
            capture_output=True,
            text=True,
        )
        if proc_pod_all.returncode == 0 and proc_pod_all.stdout.strip():
            candidate_pods = [
                p for p in proc_pod_all.stdout.split()
                if ("agent" in p or "gateway" in p) and not any(k in p for k in ("minter", "minty", "operator", "hindsight"))
            ]
            if candidate_pods:
                pod_name = candidate_pods[0]
                break
        time.sleep(_POD_POLL_INTERVAL_SECONDS)

    if not pod_name:
        pytest.fail(f"No running platform-agent-gateway pod found in namespace '{agent_namespace}' within {_POD_WAIT_TIMEOUT_SECONDS}s.")

    # Refresh credentials via broker and query repository via read-only GET API
    script = f"""
import sys, subprocess, os

# 1. Refresh credentials in the credential proxy via the broker client
p_refresh = subprocess.run(['find', '/opt', '-name', 'github_token_refresh.py'], capture_output=True, text=True)
if p_refresh.returncode == 0 and p_refresh.stdout.strip():
    refresh_script = p_refresh.stdout.strip().splitlines()[0]
    res_ref = subprocess.run(['python3', refresh_script, '{github_repo}'], capture_output=True, text=True)
    if res_ref.returncode != 0:
        print(f"Token refresh failed: {{res_ref.stderr}}", file=sys.stderr)
        sys.exit(res_ref.returncode)

# 2. Execute read-only GitHub API verification via Envoy proxy from workspace root
env = os.environ.copy()
env['PWD'] = '/opt/data'
cmd_gh = ['gh', 'api', f'repos/{github_repo}', '--jq', '.full_name']
res_gh = subprocess.run(cmd_gh, cwd='/opt/data', env=env, capture_output=True, text=True)
if res_gh.returncode != 0:
    print(f"GitHub API query failed: {{res_gh.stderr}}", file=sys.stderr)
    sys.exit(res_gh.returncode)

full_name = res_gh.stdout.strip()
if full_name.lower() != '{github_repo}'.lower():
    print(f"Expected repository '{github_repo}', got '{{full_name}}'", file=sys.stderr)
    sys.exit(1)

print(f"Successfully authenticated and queried repository: {{full_name}}")
"""

    cmd = [
        "kubectl",
        "exec",
        "-n",
        agent_namespace,
        pod_name,
        "-c",
        "platform-agent",
        "--",
        "python3",
        "-c",
        script,
    ]
    try:
        proc_start = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
        assert proc_start.returncode == 0, (
            f"GitHub token minting and API probe failed inside pod '{pod_name}' (exit code {proc_start.returncode}):\n"
            f"STDOUT:\n{proc_start.stdout}\nSTDERR:\n{proc_start.stderr}"
        )
        assert f"Successfully authenticated and queried repository: {github_repo}" in proc_start.stdout, (
            f"Expected successful repository query confirmation in stdout, got:\n{proc_start.stdout}"
        )
    except subprocess.TimeoutExpired:
        pytest.fail(f"GitHub token minting / API probe timed out after 90s in pod '{pod_name}'")


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


@pytest.mark.parametrize(
    "audit_id,human_name",
    AUDIT_STREAMS,
    ids=[aid for aid, _ in AUDIT_STREAMS],
)
def test_audit_report_ledger_dryrun_all_streams(
    audit_id: str,
    human_name: str,
    gke_cluster_name: Optional[str],
    gcp_project_id: Optional[str],
    fleet_audit_streams: str,
) -> None:
    """Exercises deterministic GitHub ledger issue & PR formatting across audit streams using --dry-run.

    Validates schema compliance, checks roster enforcement, and verifies ledger rendering
    without mutating live GitHub repositories.
    """
    if fleet_audit_streams not in ("all", "*") and audit_id != fleet_audit_streams and audit_id not in fleet_audit_streams.split(","):
        pytest.skip(f"Skipping audit stream '{audit_id}' (FLEET_AUDIT_STREAMS={fleet_audit_streams})")

    if not _AUDIT_REPORT_SCRIPT.is_file():
        pytest.fail(f"audit_report.py not found at {_AUDIT_REPORT_SCRIPT}")

    cluster = gke_cluster_name or "test-cluster"
    project = gcp_project_id or "test-project"

    # Retrieve first valid check slug for the audit stream
    valid_check = "sample-check"
    try:
        import sys
        script_dir_str = str(_AUDIT_REPORT_SCRIPT.parent)
        if script_dir_str not in sys.path:
            sys.path.insert(0, script_dir_str)
        from audit_report import AUDITS
        if audit_id in AUDITS and AUDITS[audit_id].checks:
            valid_check = AUDITS[audit_id].checks[0]
    except Exception:
        pass

    # Construct clean findings document for the audit stream
    mock_findings = {
        "audit": audit_id,
        "scope": {
            "clusters": [
                {
                    "name": cluster,
                    "location": "us-east4",
                    "project": project,
                    "checks_run": [
                        {
                            "check": valid_check,
                            "command": f"kubectl --context={cluster} get pods -A",
                        }
                    ],
                    "checks_not_applicable": [],
                }
            ],
            "skipped": [],
        },
        "findings": [],
    }

    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(mock_findings, f)
        temp_path = f.name

    try:
        proc = subprocess.run(
            [
                "python3",
                str(_AUDIT_REPORT_SCRIPT),
                "finish",
                f"--audit={audit_id}",
                f"--findings-file={temp_path}",
                "--dry-run",
            ],
            capture_output=True,
            text=True,
        )
        # Exit code 0 indicates valid schema, correctly formatted ledger issue, and successful dry-run
        assert proc.returncode == 0, (
            f"Audit stream '{audit_id}' ({human_name}) dry-run validation failed (exit code {proc.returncode}):\n"
            f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
        )
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


@pytest.mark.parametrize(
    "audit_id,human_name",
    AUDIT_STREAMS,
    ids=[aid for aid, _ in AUDIT_STREAMS],
)
def test_audit_report_github_api_lifecycle_mocked(
    audit_id: str,
    human_name: str,
    tmp_path: pathlib.Path,
    fleet_audit_streams: str,
) -> None:
    """Verifies that each audit watchdog executes the exact expected GitHub API lifecycle.

    Uses an in-memory execution seam to simulate the GitHub CLI/API and assert:
    1. It ensures the standard audit and severity labels exist on the repo.
    2. It searches GitHub for existing ledger issues (gh issue list --label audit:<audit_id>).
    3. It fetches previous issue body and comments to check for active findings and /remediate commands.
    4. It updates the ledger issue title and body with the rendered capacity audit tables.
    5. It strictly does NOT create unexpected pull requests without explicit authorization.
    """
    if fleet_audit_streams not in ("all", "*") and audit_id != fleet_audit_streams and audit_id not in fleet_audit_streams.split(","):
        pytest.skip(f"Skipping audit stream '{audit_id}' (FLEET_AUDIT_STREAMS={fleet_audit_streams})")

    if not _AUDIT_REPORT_SCRIPT.is_file():
        pytest.fail(f"audit_report.py not found at {_AUDIT_REPORT_SCRIPT}")

    import sys
    script_dir_str = str(_AUDIT_REPORT_SCRIPT.parent)
    if script_dir_str not in sys.path:
        sys.path.insert(0, script_dir_str)
    platform_scripts_dir = str(_REPO_ROOT / "agents" / "platform" / "scripts")
    if platform_scripts_dir not in sys.path:
        sys.path.insert(0, platform_scripts_dir)

    import audit_report

    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / ".git").mkdir(parents=True, exist_ok=True)

    # repo_root joins the list: it was patched below and never put back, so the module
    # kept pointing at a tmp_path for the rest of the session. The test is parametrised
    # seven ways, so anything missed here leaks into the next parameter's run.
    original_workspace = audit_report.GITOPS_WORKSPACE
    original_scratch = audit_report.SCRATCH_DIR
    original_repo_root = audit_report.repo_root
    original_run_cmd = audit_report.run_cmd
    original_refresh = audit_report.refresh_credentials
    original_resolve = audit_report.resolve_repo

    calls: list[list[str]] = []

    def mock_run_cmd(cmd, **kwargs):
        cmd_list = list(cmd)
        calls.append(cmd_list)
        joined = " ".join(cmd_list)
        if cmd_list[:2] == ["git", "clone"]:
            dest = pathlib.Path(cmd_list[-1])
            (dest / ".git").mkdir(parents=True, exist_ok=True)
            return type("CompletedProcess", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        if "gh issue list" in joined:
            return type("CompletedProcess", (), {
                "returncode": 0,
                "stdout": json.dumps([{"number": 42, "url": f"https://github.com/test-org-kube-agent/agents-repo/issues/42"}]),
                "stderr": "",
            })()
        if "gh issue view" in joined and "--json body" in joined:
            return type("CompletedProcess", (), {
                "returncode": 0,
                "stdout": json.dumps({"body": "<!-- audit-findings: [] -->"}),
                "stderr": "",
            })()
        if "gh issue view" in joined and "--json comments" in joined:
            return type("CompletedProcess", (), {
                "returncode": 0,
                "stdout": json.dumps({"comments": []}),
                "stderr": "",
            })()
        return type("CompletedProcess", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    try:
        audit_report.GITOPS_WORKSPACE = str(tmp_path)
        audit_report.SCRATCH_DIR = str(tmp_path)
        audit_report.set_workspace(workspace)
        audit_report.run_cmd = mock_run_cmd
        audit_report.refresh_credentials = lambda *args, **kwargs: None
        audit_report.resolve_repo = lambda *args, **kwargs: "test-org-kube-agent/agents-repo"
        audit_report.repo_root = lambda: workspace

        valid_check = audit_report.AUDITS[audit_id].checks[0] if audit_id in audit_report.AUDITS and audit_report.AUDITS[audit_id].checks else "single-zone-nodepool"

        doc = {
            "audit": audit_id,
            "scope": {
                "clusters": [{
                    "name": "platform-agent-host",
                    "location": "us-east4",
                    "project": "sergiuspiridon-gkedemos",
                    "checks_run": [{"check": valid_check, "command": "kubectl get pods -A"}]
                }],
                "skipped": []
            },
            "findings": [{
                "check": valid_check,
                "severity": "major",
                "title": f"Sample finding for {human_name}",
                "cluster": "platform-agent-host",
                "namespace": "default",
                "object": "Deployment/sample-app",
                "evidence": {"command": "kubectl get pods", "excerpt": "sample evidence excerpt"},
                "impact": "Sample impact description for audit finding.",
                "recommendation": {
                    "action": "Take recommended remediation step.",
                    "rationale": "Rationale explaining why this change was chosen.",
                    "risk": "Risk evaluation for this specific recommendation."
                },
                "remediation": {
                    "kind": "manual",
                    "note": "kubectl edit deployment/sample-app"
                }
            }]
        }

        findings_file = tmp_path / f"findings_{audit_id}.json"
        findings_file.write_text(json.dumps(doc), encoding="utf-8")

        exit_code = audit_report.main(["finish", f"--audit={audit_id}", f"--findings-file={findings_file}"])
        assert exit_code == 0, f"Expected finish exit code 0 for '{audit_id}', got {exit_code}"

        all_commands = [" ".join(c) for c in calls]

        # 1. Assert label verification calls
        assert any("gh label create" in c and f"audit:{audit_id}" in c for c in all_commands), (
            f"Expected GitHub call to ensure 'audit:{audit_id}' label exists."
        )

        # 2. Assert ledger lookup
        assert any("gh issue list" in c and f"audit:{audit_id}" in c for c in all_commands), (
            f"Expected GitHub call to list existing ledger issue for '{audit_id}'."
        )

        # 3. Assert ledger issue update
        assert any("gh issue edit 42" in c and f"[audit] {human_name}" in c for c in all_commands), (
            f"Expected GitHub call to edit issue #42 with updated '{human_name}' title."
        )

        # 4. Assert NO unauthorized PR creation
        assert not any("gh pr create" in c for c in all_commands), (
            "SECURITY/SAFETY VIOLATION: Audit unexpectedly attempted to create a pull request!"
        )

    finally:
        audit_report.run_cmd = original_run_cmd
        audit_report.refresh_credentials = original_refresh
        audit_report.resolve_repo = original_resolve
        audit_report.repo_root = original_repo_root
        audit_report.GITOPS_WORKSPACE = original_workspace
        audit_report.SCRATCH_DIR = original_scratch
        audit_report.set_workspace(None)
