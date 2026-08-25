"""Stage 3 E2E Promotion Test: GKE Stockout Ingress Smoke & Comprehensive Scenarios Suite."""

import os
import pathlib
import subprocess
import time
from typing import List, Optional, Tuple

import pytest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_SCENARIOS_DIR = _REPO_ROOT / "agentplugins" / "gke-stockout-investigator" / "scenarios"

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
    """Ensures that the Pub/Sub topic, logging sinks, and AgentPlugin are configured on the cluster."""
    if os.environ.get("SKIP_STOCKOUT") == "1":
        pytest.skip("SKIP_STOCKOUT=1 is set; skipping stockout investigator plugin setup.")

    if not gcp_project_id or not gke_cluster_name:
        pytest.fail("GCP_PROJECT_ID and GKE_CLUSTER_NAME are required for stockout E2E tests.")

    # 1. Ensure PubSub topic exists
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

    # 2. Verify AgentPlugin CRD exists on cluster (installed canonically via Helm chart)
    check_crd = subprocess.run(
        ["kubectl", "get", "crd", "agentplugins.kubeagents.x-k8s.io"],
        capture_output=True,
        text=True,
    )
    if check_crd.returncode != 0:
        pytest.fail(
            "AgentPlugin CRD 'agentplugins.kubeagents.x-k8s.io' not found on cluster; "
            "it is managed and installed by the kube-agents Helm chart."
        )

    # 3. Check if gkestockoutinvestigator is registered
    check_plugin = subprocess.run(
        ["kubectl", "get", "agentplugins", "gkestockoutinvestigator", "-n", agent_namespace],
        capture_output=True,
        text=True,
    )
    if check_plugin.returncode != 0:
        # Try to install from local helm/kustomize template if available
        install_script = _REPO_ROOT / "agentplugins" / "gke-stockout-investigator" / "install.sh"
        if install_script.is_file():
            install_env = {
                **os.environ,
                "GCP_PROJECT_ID": gcp_project_id,
                "TARGET_CLUSTER_NAME": gke_cluster_name,
                "TARGET_CLUSTER_LOCATION": gcp_region,
                "HERMES_NAMESPACE": agent_namespace,
            }
            proc = subprocess.run(
                [str(install_script)],
                capture_output=True,
                text=True,
                env=install_env,
            )
            if proc.returncode != 0:
                pytest.fail(
                    f"Could not auto-install stockout investigator plugin:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
                )
            time.sleep(5)


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

    proc = subprocess.run([str(verify_script)], capture_output=True, text=True, env=env)
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

    proc = subprocess.run(
        [str(scenario_script), "--teardown", "--watch-timeout", watch_timeout],
        capture_output=True,
        text=True,
        env=env,
    )
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
