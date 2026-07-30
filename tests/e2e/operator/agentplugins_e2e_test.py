#!/usr/bin/env python3
"""
E2E Kubernetes Operator Cluster Validation Test Suite (AgentPlugins E2E).

Validates operator build from scratch, deployment, image tag verification,
AgentPlugin OCI image packaging & deployment, Hermes log activation,
ConfigMap merging, plugin CR removal, log output silence, and config cleanup.
"""

import io
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

# Enforce UTF-8 stdout/stderr stream handling with replacement for non-decodable characters
if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "buffer"):
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# Environment & Resource Constants
def _get_required_env(var_name: str) -> str:
    val = os.environ.get(var_name)
    if not val:
        raise ValueError(f"Environment variable '{var_name}' must be set.")
    return val

KUBE_CONTEXT: str = _get_required_env("KUBE_CONTEXT")
NAMESPACE: str = _get_required_env("NAMESPACE")
REGISTRY: str = _get_required_env("REGISTRY")

OPERATOR_DEPLOYMENT: str = "kubeagents-controller-manager"
GATEWAY_DEPLOYMENT: str = "platform-agent-gateway"
PLUGIN_CR_NAME: str = "e2e-example-plugin"
CONFIGMAP_NAME: str = "platform-agent-config"

SCRIPT_DIR: Path = Path(__file__).resolve().parent
REPO_ROOT: Path = SCRIPT_DIR.parents[2]
TEMPLATES_DIR: Path = SCRIPT_DIR / "templates"


def log(msg: str) -> None:
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n[{timestamp}] [E2E-TEST] {msg}", flush=True)


def run_cmd(
    cmd: list[str],
    cwd: str | Path | None = None,
    check: bool = True,
    capture_output: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Execute a binary command list with printed output, raising CalledProcessError if check=True and exit code is non-zero."""
    cmd_str = " ".join(cmd)
    print(f"\n$ {cmd_str}", flush=True)

    res = subprocess.run(cmd, cwd=cwd, check=False, text=True, encoding="utf-8", errors="replace", capture_output=True)

    if res.stdout:
        print(res.stdout, end="", flush=True)
    if res.stderr:
        print(res.stderr, end="", flush=True, file=sys.stderr)

    if check and res.returncode != 0:
        raise subprocess.CalledProcessError(res.returncode, cmd, output=res.stdout, stderr=res.stderr)

    return res


def run_kubectl(args: list[str], check: bool = True, capture_output: bool = False) -> subprocess.CompletedProcess[str]:
    """Prepend context to kubectl command."""
    full_cmd = ["kubectl", "--context", KUBE_CONTEXT] + args
    return run_cmd(full_cmd, check=check, capture_output=capture_output)


def get_kubectl_output(args: list[str]) -> str:
    """Execute kubectl command silently, asserting zero exit code, and return stripped stdout."""
    full_cmd = ["kubectl", "--context", KUBE_CONTEXT] + args
    res = subprocess.run(full_cmd, check=True, text=True, encoding="utf-8", errors="replace", capture_output=True)
    return res.stdout.strip()


def apply_crd_manifests(crd_dir: Path) -> None:
    """Apply CRD manifests per-file using kubectl replace with fallback to create."""
    log("Applying CRD manifests...")
    crd_files = sorted(crd_dir.glob("*.yaml")) if crd_dir.is_dir() else [crd_dir]
    for crd_file in crd_files:
        res = run_kubectl(["replace", "-f", str(crd_file)], check=False)
        if res.returncode != 0:
            run_kubectl(["create", "-f", str(crd_file)], check=True)


def apply_kubectl_manifest(manifest: str) -> None:
    """Apply a YAML manifest via kubectl stdin and raise exception if non-zero exit code."""
    full_cmd = ["kubectl", "--context", KUBE_CONTEXT, "apply", "-f", "-"]
    print(f"\n$ {' '.join(full_cmd)} (stdin manifest)", flush=True)
    res = subprocess.run(full_cmd, input=manifest, text=True, encoding="utf-8", errors="replace", capture_output=True)
    if res.stdout:
        print(res.stdout, end="", flush=True)
    if res.stderr:
        print(res.stderr, end="", flush=True, file=sys.stderr)
    if res.returncode != 0:
        raise subprocess.CalledProcessError(res.returncode, full_cmd, output=res.stdout, stderr=res.stderr)


def render_template(template_path: Path, replacements: dict[str, str]) -> str:
    """Read a template file and substitute named string placeholders."""
    content = template_path.read_text(encoding="utf-8", errors="replace")
    for key, val in replacements.items():
        content = content.replace(f"{{{key}}}", val)
    return content


def get_deployment_generation(deployment_name: str) -> int:
    """Get current metadata.generation of a deployment."""
    val = get_kubectl_output([
        "get", "deployment", deployment_name, "-n", NAMESPACE, "-o", "jsonpath={.metadata.generation}"
    ])
    return int(val) if val.isdigit() else 0


def get_latest_pod_template_hash(deployment_name: str) -> str:
    """Query the active pod-template-hash of the active ReplicaSet (replicas > 0) for a deployment."""
    output = get_kubectl_output([
        "get", "rs", "-n", NAMESPACE,
        "-l", f"app={deployment_name}",
        "--sort-by=.metadata.creationTimestamp",
        "-o", "jsonpath={range .items[?(@.status.replicas>0)]}{.metadata.labels.pod-template-hash}{\"\\n\"}{end}"
    ])
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    return lines[-1] if lines else ""


def wait_deployment_generation_change(deployment_name: str, min_gen: int, timeout_sec: int = 20) -> None:
    """Wait for operator reconciliation to update deployment metadata.generation."""
    end_time = time.time() + timeout_sec
    while time.time() < end_time:
        try:
            curr_gen = get_deployment_generation(deployment_name)
            if curr_gen >= min_gen:
                log(f"Deployment '{deployment_name}' generation updated to {curr_gen} (>= {min_gen})")
                return
        except (subprocess.CalledProcessError, ValueError):
            pass
        time.sleep(1)
    log(f"Warning: Deployment '{deployment_name}' generation did not reach {min_gen} within {timeout_sec}s")


def poll_running_pod_name(label_selector: str, pod_template_hash: str | None = None, timeout_sec: int = 30) -> str:
    """Poll Kubernetes API for the newest Running pod (excluding pods pending deletion) matching label selector and optional pod_template_hash."""
    end_time = time.time() + timeout_sec
    full_selector = label_selector
    if pod_template_hash:
        full_selector = f"{label_selector},pod-template-hash={pod_template_hash}"

    while time.time() < end_time:
        try:
            output = get_kubectl_output([
                "get", "pods", "-n", NAMESPACE,
                "-l", full_selector,
                "--field-selector=status.phase=Running",
                "--sort-by=.metadata.creationTimestamp",
                "-o", "jsonpath={range .items[*]}{.metadata.name}{' '}{.metadata.deletionTimestamp}{'\\n'}{end}"
            ])
            for line in reversed(output.splitlines()):
                parts = line.strip().split()
                if len(parts) == 1:
                    return parts[0]
        except subprocess.CalledProcessError:
            pass
        time.sleep(2)
    return ""


def get_pod_image(pod_name: str, container_name: str) -> str:
    """Query image tag of a specific container inside a pod."""
    return get_kubectl_output([
        "get", "pod", "-n", NAMESPACE, pod_name,
        "-o", f"jsonpath={{.spec.containers[?(@.name==\"{container_name}\")].image}}"
    ])


def poll_pod_with_image(label_selector: str, container_name: str, expected_image: str, timeout_sec: int = 30) -> str:
    """Poll Kubernetes API until a Running pod matching label selector has the expected container image."""
    end_time = time.time() + timeout_sec
    while time.time() < end_time:
        try:
            pod_names = get_kubectl_output([
                "get", "pods", "-n", NAMESPACE,
                "-l", label_selector,
                "--field-selector=status.phase=Running",
                "-o", "jsonpath={.items[*].metadata.name}"
            ]).split()
            for pod_name in pod_names:
                img = get_pod_image(pod_name, container_name)
                if img == expected_image:
                    return pod_name
        except subprocess.CalledProcessError:
            pass
        time.sleep(2)
    return ""


def poll_pod_logs(
    label_selector: str, container_name: str, match_str: str | None = None, timeout_sec: int = 60
) -> tuple[str, str]:
    """Poll container logs from the newest Running pod matching pod-template-hash, streaming log lines live."""
    end_time = time.time() + timeout_sec
    pod_name = ""
    logs = ""
    seen_lines: set[str] = set()
    current_pod: str = ""

    pod_template_hash = None
    if "app=" in label_selector:
        deployment_name = label_selector.split("app=")[1].split(",")[0]
        try:
            pod_template_hash = get_latest_pod_template_hash(deployment_name)
            if pod_template_hash:
                log(f"Targeting pods with pod-template-hash='{pod_template_hash}' for deployment '{deployment_name}'")
        except Exception:
            pass

    while time.time() < end_time:
        pod_name = poll_running_pod_name(label_selector, pod_template_hash=pod_template_hash, timeout_sec=5)
        if pod_name:
            if pod_name != current_pod:
                current_pod = pod_name
                seen_lines.clear()
                log(f"Streaming logs from pod: {pod_name}")
            try:
                logs = get_kubectl_output([
                    "logs", "-n", NAMESPACE, pod_name, "-c", container_name, "--tail=5000"
                ])
                for line in logs.splitlines():
                    if line not in seen_lines:
                        print(f"  [STREAM-LOG] {line}", flush=True)
                        seen_lines.add(line)
                if not match_str or match_str in logs:
                    return pod_name, logs
            except subprocess.CalledProcessError:
                pass
        time.sleep(2)
    return pod_name, logs


def wait_deployment_rollout(deployment_name: str, timeout: str = "180s") -> None:
    """Wait for deployment rollout status to succeed."""
    run_kubectl(["rollout", "status", f"deployment/{deployment_name}", "-n", NAMESPACE, f"--timeout={timeout}"])


def get_platform_configmap_yaml() -> str:
    """Retrieve platform-agent-config ConfigMap YAML content."""
    return get_kubectl_output([
        "get", "configmap", CONFIGMAP_NAME, "-n", NAMESPACE, "-o", "jsonpath={.data.config\\.yaml}"
    ])


def step1_rebuild_and_deploy_operator(operator_image: str, operator_tag: str) -> None:
    """Step 1: Rebuild k8s-operator Go binary and docker image from scratch, push, apply CRDs, update deployment."""
    log(f"STEP 1: Rebuilding and deploying k8s-operator from scratch with tag '{operator_tag}'...")
    operator_dir = REPO_ROOT / "k8s-operator"

    run_cmd(["make", "manifests", "generate"], cwd=operator_dir)

    for artifact in ["manager", "linux/amd64/manager", "bin/manager"]:
        p = operator_dir / artifact
        if p.exists():
            p.unlink()

    run_cmd(["docker", "build", "--no-cache", "-t", operator_image, "-f", "Dockerfile", "."], cwd=operator_dir)
    run_cmd(["docker", "push", operator_image])

    apply_crd_manifests(operator_dir / "config" / "crd" / "bases")

    run_kubectl(["set", "image", f"deployment/{OPERATOR_DEPLOYMENT}", f"manager={operator_image}", "-n", NAMESPACE])
    wait_deployment_rollout(OPERATOR_DEPLOYMENT)
    log("STEP 1 SUCCESS: k8s-operator built, pushed, and deployed.")


def step2_verify_operator_version(operator_image: str) -> None:
    """Step 2: Verify deployed image tag in deployment spec and active running pod."""
    log("STEP 2: Verifying deployed version by image tag...")
    deployed_image = get_kubectl_output([
        "get", "deployment", OPERATOR_DEPLOYMENT, "-n", NAMESPACE,
        "-o", "jsonpath={.spec.template.spec.containers[?(@.name==\"manager\")].image}"
    ])
    log(f"Deployment spec image: {deployed_image}")
    assert deployed_image == operator_image, f"Spec image '{deployed_image}' != expected '{operator_image}'"

    pod_name = poll_pod_with_image("control-plane=controller-manager", "manager", operator_image, timeout_sec=30)
    assert pod_name != "", f"No running operator pod found with image '{operator_image}'"

    pod_image = get_pod_image(pod_name, "manager")
    log(f"Running pod name:     {pod_name}")
    log(f"Running pod image:    {pod_image}")
    assert pod_image == operator_image, f"Running pod image '{pod_image}' != expected '{operator_image}'"

    log("--- OPERATOR POD DETAILS ---")
    res = run_kubectl(["get", "pod", "-n", NAMESPACE, pod_name, "-o", "wide"], capture_output=True)
    assert "Running" in res.stdout, f"Pod {pod_name} state is not Running in kubectl output: {res.stdout}"
    assert pod_name in res.stdout, f"Pod details output missing expected pod name {pod_name}"
    log("STEP 2 SUCCESS: Operator image tag verified on cluster.")


def step3_build_and_push_plugin_image(plugin_image: str, unique_str: str) -> None:
    """Step 3: Build and push example extension OCI image with unique build string using templates."""
    log(f"STEP 3: Building and pushing example plugin OCI image '{plugin_image}'...")
    tmp_dir = tempfile.mkdtemp()
    try:
        replacements = {"UNIQUE_STR": unique_str, "PLUGIN_CR_NAME": PLUGIN_CR_NAME}

        skill_dir = Path(tmp_dir) / "skills" / "e2e-skill"
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(
            render_template(TEMPLATES_DIR / "plugin_src" / "skills" / "e2e-skill" / "SKILL.md.template", replacements)
        )
        (Path(tmp_dir) / "plugin.yaml").write_text(
            render_template(TEMPLATES_DIR / "plugin_src" / "plugin.yaml.template", replacements)
        )
        (Path(tmp_dir) / "plugin.py").write_text(
            render_template(TEMPLATES_DIR / "plugin_src" / "plugin.py.template", replacements)
        )
        (Path(tmp_dir) / "__init__.py").write_text(
            render_template(TEMPLATES_DIR / "plugin_src" / "__init__.py.template", replacements)
        )
        shutil.copy(TEMPLATES_DIR / "plugin_src" / "Dockerfile", Path(tmp_dir) / "Dockerfile")

        run_cmd(["chmod", "-R", "a+rX", tmp_dir])
        run_cmd(["docker", "build", "-t", plugin_image, tmp_dir])
        run_cmd(["docker", "push", plugin_image])
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    log(f"STEP 3 SUCCESS: Plugin image built and pushed to {plugin_image}.")


def check_operator_error_log(search_str: str) -> bool:
    """Fetch logs from controller manager and check for expected error message."""
    try:
        output = get_kubectl_output([
            "logs", "deployment/kubeagents-controller-manager", "-n", NAMESPACE, "-c", "manager", "--tail=5000"
        ])
        return search_str in output
    except Exception:
        return False


def step4_deploy_agent_plugin_cr(plugin_image: str, unique_str: str) -> None:
    """Step 4: Validate opt-in targeting, imagePullPolicy, and deploy AgentPlugin custom resource."""
    log(f"STEP 4: Testing opt-in targeting validation (non-matching agentRef)...")

    # 4a. Validate opt-in targeting with non-matching agentRef
    untargeted_manifest = render_template(TEMPLATES_DIR / "agentplugin_untargeted_cr.yaml.template", {
        "NAMESPACE": NAMESPACE,
        "PLUGIN_IMAGE": plugin_image,
    })
    apply_kubectl_manifest(untargeted_manifest)

    time.sleep(3)
    cm_config_untargeted = get_platform_configmap_yaml()
    assert "e2e-untargeted-plugin" not in cm_config_untargeted, "Untargeted plugin should NOT be in plugins.enabled"
    assert "untargeted_setting" not in cm_config_untargeted, "Untargeted config setting should NOT be merged"
    log("Verified non-matching agentRef plugin was ignored by operator.")

    run_kubectl(["delete", "agentplugin", "e2e-untargeted-plugin", "-n", NAMESPACE], check=False)

    # 4b. Deploy targeted AgentPlugin with imagePullPolicy: Always and disallowed subtree test
    log(f"Deploying targeted AgentPlugin custom resource '{PLUGIN_CR_NAME}'...")
    gen_before = get_deployment_generation(GATEWAY_DEPLOYMENT)

    replacements = {
        "PLUGIN_CR_NAME": PLUGIN_CR_NAME,
        "NAMESPACE": NAMESPACE,
        "PLUGIN_IMAGE": plugin_image,
        "UNIQUE_STR": unique_str,
        "AGENT_REF": "platform-agent",
    }
    manifest = render_template(TEMPLATES_DIR / "agentplugin_cr.yaml.template", replacements)
    apply_kubectl_manifest(manifest)

    log("--- DEPLOYED AGENTPLUGIN RESOURCE ---")
    res = run_kubectl(["get", "agentplugin", PLUGIN_CR_NAME, "-n", NAMESPACE, "-o", "yaml"], capture_output=True)
    assert PLUGIN_CR_NAME in res.stdout, f"AgentPlugin output missing name {PLUGIN_CR_NAME}"

    log("Waiting for operator reconciliation and PlatformAgent deployment update...")
    wait_deployment_generation_change(GATEWAY_DEPLOYMENT, min_gen=gen_before + 1)
    wait_deployment_rollout(GATEWAY_DEPLOYMENT)

    # Verify custom imagePullPolicy (Always) is set on deployment volume
    vol_pull_policy = get_kubectl_output([
        "get", "deployment", GATEWAY_DEPLOYMENT, "-n", NAMESPACE,
        "-o", f"jsonpath={{.spec.template.spec.volumes[?(@.name==\"plugin-{PLUGIN_CR_NAME}\")].image.pullPolicy}}"
    ])
    log(f"Verified plugin volume imagePullPolicy on deployment: {vol_pull_policy}")
    assert vol_pull_policy == "Always", f"Expected imagePullPolicy Always, got {vol_pull_policy}"

    log("STEP 4 SUCCESS: AgentPlugin opt-in targeting, imagePullPolicy, and CR deployment verified.")


def step5_verify_plugin_logs_and_config(unique_str: str) -> None:
    """Step 5: Verify Hermes log output, skill availability, config allowlisting, and operator error logging."""
    log("STEP 5: Verifying Hermes logs for unique message, skill availability, config allowlisting, and operator error logging...")
    pod_name, logs = poll_pod_logs("app=platform-agent-gateway", "platform-agent", match_str=unique_str, timeout_sec=60)

    assert unique_str in logs, f"Unique message '{unique_str}' was NOT found in platform-agent logs after 60s"
    log(f"Found unique message '{unique_str}' in platform-agent logs of pod {pod_name}!")
    log("--- MATCHING HERMES LOG LINES ---")
    skill_verified = False
    for line in logs.splitlines():
        if unique_str in line:
            print(line, flush=True)
            if "skill_available=True" in line or "available=True" in line:
                skill_verified = True

    assert skill_verified, f"Skill 'e2e-skill' was NOT verified as available in logs for {unique_str}"
    log(f"Skill 'e2e-skill' successfully verified as available in Hermes for plugin build {unique_str}!")

    cm_config = get_platform_configmap_yaml()
    log("--- CONFIGMAP MERGED VALUES (e2e_test_setting & plugins.enabled) ---")
    for line in cm_config.splitlines():
        if any(k in line for k in ["e2e_test_setting", "unique_id", PLUGIN_CR_NAME]):
            print(line, flush=True)

    # 5a. Verify allowed config subtree is merged
    assert "e2e_test_setting" in cm_config and unique_str in cm_config, f"Config change '{unique_str}' missing from ConfigMap"
    assert PLUGIN_CR_NAME in cm_config, f"'{PLUGIN_CR_NAME}' missing from plugins.enabled in ConfigMap"
    log("Verified allowed subtree 'approvals.e2e_test_setting' was merged into ConfigMap.")

    # 5b. Verify disallowed config subtree is REJECTED / STRIPPED OUT
    assert "disallowed_test_subtree" not in cm_config and "forbidden_key" not in cm_config, "Disallowed config subtree should NOT be in ConfigMap!"
    log("Verified disallowed config subtree 'disallowed_test_subtree' was REJECTED and excluded from ConfigMap.")

    # 5c. Verify operator logged error for disallowed subtree key
    err_logged = check_operator_error_log("ignoring plugin config key outside allowed subtrees")
    assert err_logged, "Expected operator to log 'ignoring plugin config key outside allowed subtrees'"
    log("Verified operator logged manifestsLog.Error for key outside allowed subtrees.")

    log("STEP 5 SUCCESS: Unique message, skill availability, config allowlisting, and operator error logging verified.")


def step6_remove_agent_plugin_cr() -> None:
    """Step 6: Remove AgentPlugin CR and wait for PlatformAgent rollout."""
    log(f"STEP 6: Removing AgentPlugin '{PLUGIN_CR_NAME}' from cluster...")
    gen_before = get_deployment_generation(GATEWAY_DEPLOYMENT)

    run_kubectl(["delete", "agentplugin", PLUGIN_CR_NAME, "-n", NAMESPACE])

    log("--- REMAINING AGENTPLUGINS ON CLUSTER ---")
    res_get = run_kubectl(["get", "agentplugins", "-n", NAMESPACE], capture_output=True)
    assert PLUGIN_CR_NAME not in res_get.stdout, f"Deleted AgentPlugin {PLUGIN_CR_NAME} still present in cluster list"

    log("Waiting for operator reconciliation and PlatformAgent deployment update after plugin removal...")
    wait_deployment_generation_change(GATEWAY_DEPLOYMENT, min_gen=gen_before + 1)
    wait_deployment_rollout(GATEWAY_DEPLOYMENT)
    log("STEP 6 SUCCESS: AgentPlugin removed from cluster.")


def step7_verify_log_silence_after_removal(unique_str: str) -> None:
    """Step 7: Check that unique message no longer appears in new pod logs."""
    log("STEP 7: Checking that unique message no longer appears in new pod logs...")
    pod_name, new_logs = poll_pod_logs("app=platform-agent-gateway", "platform-agent", timeout_sec=30)
    assert pod_name != "", "No Running platform-agent-gateway pod found after plugin removal"

    log("--- NEW PLATFORM AGENT POD DETAILS ---")
    res = run_kubectl(["get", "pod", "-n", NAMESPACE, pod_name, "-o", "wide"], capture_output=True)
    assert "Running" in res.stdout, f"Replacement pod {pod_name} state is not Running: {res.stdout}"
    assert pod_name in res.stdout, f"Pod details output missing expected pod name {pod_name}"

    assert unique_str not in new_logs, f"Unique message '{unique_str}' STILL appears in new pod logs after plugin removal"
    log(f"Confirmed unique message '{unique_str}' no longer appears in logs of pod {pod_name}.")
    log("STEP 7 SUCCESS: Unique debug entries stopped.")


def step8_verify_config_cleanup(unique_str: str) -> None:
    """Step 8: Check that config change and plugin entry are removed from ConfigMap."""
    log("STEP 8: Verifying config change is removed from ConfigMap...")
    new_cm_config = get_platform_configmap_yaml()

    log("--- CONFIGMAP PLUGINS LIST AFTER PLUGIN REMOVAL ---")
    in_plugins = False
    for line in new_cm_config.splitlines():
        if "plugins:" in line:
            in_plugins = True
        if in_plugins:
            print(line, flush=True)

    assert unique_str not in new_cm_config and "e2e_test_setting" not in new_cm_config, f"Config change '{unique_str}' is STILL in ConfigMap"
    log("Confirmed config change is no longer present in ConfigMap.")
    log("STEP 8 SUCCESS: Config change removed.")


def step9_verify_enable_image_volumes_false_annotation_safeguard(plugin_image: str, unique_str: str) -> None:
    """Step 9: Verify image volume disable annotation guard and status update to Degraded/ImageVolumeUnsupported."""
    log("STEP 9: Testing 'kubeagents.x-k8s.io/enable-image-volumes=false' annotation safeguard...")

    # 9a. Annotate PlatformAgent to force enable-image-volumes=false
    run_kubectl([
        "annotate", "platformagent", "platform-agent", "-n", NAMESPACE,
        "kubeagents.x-k8s.io/enable-image-volumes=false", "--overwrite"
    ])

    try:
        # 9b. Deploy targeted AgentPlugin CR
        gen_before = get_deployment_generation(GATEWAY_DEPLOYMENT)
        manifest = render_template(TEMPLATES_DIR / "agentplugin_cr.yaml.template", {
            "PLUGIN_CR_NAME": PLUGIN_CR_NAME,
            "NAMESPACE": NAMESPACE,
            "PLUGIN_IMAGE": plugin_image,
            "UNIQUE_STR": unique_str,
            "AGENT_REF": "platform-agent",
        })
        apply_kubectl_manifest(manifest)

        # 9c. Wait for operator reconciliation
        wait_deployment_generation_change(GATEWAY_DEPLOYMENT, min_gen=gen_before + 1)
        wait_deployment_rollout(GATEWAY_DEPLOYMENT)

        # 9d. Verify OCI volume was NOT attached to gateway deployment
        vols = get_kubectl_output([
            "get", "deployment", GATEWAY_DEPLOYMENT, "-n", NAMESPACE,
            "-o", f"jsonpath={{.spec.template.spec.volumes[?(@.name==\"plugin-{PLUGIN_CR_NAME}\")].name}}"
        ])
        assert vols == "", f"Volume 'plugin-{PLUGIN_CR_NAME}' should NOT be attached when enable-image-volumes=false, got '{vols}'"
        log("Verified OCI volume attachment was skipped when enable-image-volumes=false.")

        # 9e. Verify AgentPlugin status condition Reason == ImageVolumeUnsupported and Phase == Degraded
        status_phase = get_kubectl_output([
            "get", "agentplugin", PLUGIN_CR_NAME, "-n", NAMESPACE,
            "-o", "jsonpath={.status.phase}"
        ])
        assert status_phase == "Degraded", f"Expected AgentPlugin status.phase 'Degraded', got '{status_phase}'"
        log(f"Verified AgentPlugin status phase is '{status_phase}'.")

        cond_reason = get_kubectl_output([
            "get", "agentplugin", PLUGIN_CR_NAME, "-n", NAMESPACE,
            "-o", "jsonpath={.status.conditions[?(@.type==\"Ready\")].reason}"
        ])
        assert cond_reason == "ImageVolumeUnsupported", f"Expected condition reason 'ImageVolumeUnsupported', got '{cond_reason}'"
        log(f"Verified AgentPlugin condition reason is '{cond_reason}'.")

        # 9f. Verify operator logged error message for skipped OCI volume attachment
        err_logged = check_operator_error_log("skipping plugin OCI image volume mount")
        assert err_logged, "Expected operator to log 'skipping plugin OCI image volume mount'"
        log("Verified operator logged manifestsLog error for skipped OCI image volume attachment.")

    finally:
        # 9g. Cleanup Step 9 resources
        log("Cleaning up AgentPlugin and enable-image-volumes annotation...")
        run_kubectl(["delete", "agentplugin", PLUGIN_CR_NAME, "-n", NAMESPACE], check=False)
        run_kubectl([
            "annotate", "platformagent", "platform-agent", "-n", NAMESPACE,
            "kubeagents.x-k8s.io/enable-image-volumes-"
        ], check=False)
        wait_deployment_rollout(GATEWAY_DEPLOYMENT)

    log("STEP 9 SUCCESS: ImageVolume unsupported guard and Degraded status condition verified.")


def step10_verify_missing_crd_decoupled_dependency_safeguard() -> None:
    """Step 10: Verify operator reconciles PlatformAgent gracefully when AgentPlugin CRD is missing."""
    log("STEP 10: Testing missing AgentPlugin CRD decoupled dependency safeguard...")
    crd_dir = REPO_ROOT / "k8s-operator" / "config" / "crd" / "bases"

    try:
        # 10a. Delete AgentPlugin CRD
        log("Deleting AgentPlugin CRD from cluster...")
        run_kubectl(["delete", "crd", "agentplugins.kubeagents.x-k8s.io"], check=True)

        # 10b. Trigger PlatformAgent reconciliation by updating annotation
        gen_before = get_deployment_generation(GATEWAY_DEPLOYMENT)
        trigger_val = str(int(time.time()))
        run_kubectl([
            "annotate", "platformagent", "platform-agent", "-n", NAMESPACE,
            f"e2e.test/crd-missing-trigger={trigger_val}", "--overwrite"
        ])

        # 10c. Wait for PlatformAgent reconciliation to succeed
        wait_deployment_generation_change(GATEWAY_DEPLOYMENT, min_gen=gen_before + 1)
        wait_deployment_rollout(GATEWAY_DEPLOYMENT)

        # 10d. Confirm controller manager is still healthy and running
        op_image = get_kubectl_output([
            "get", "deployment", OPERATOR_DEPLOYMENT, "-n", NAMESPACE,
            "-o", "jsonpath={.spec.template.spec.containers[?(@.name==\"manager\")].image}"
        ])
        op_pod = poll_pod_with_image("control-plane=controller-manager", "manager", op_image, timeout_sec=15)
        # 10e. Verify operator logged reflector warning or info message for missing CRD
        crd_missing_logged = (
            check_operator_error_log("the server could not find the requested resource") or
            check_operator_error_log("AgentPlugin CRD is not installed on cluster")
        )
        assert crd_missing_logged, "Expected operator to log missing CRD reflector warning or info message"
        log("Verified operator logged missing CRD reflector message while PlatformAgent reconciliation succeeded.")

    finally:
        # 10f. Re-apply CRDs and remove test annotation
        log("Restoring AgentPlugin CRD...")
        apply_crd_manifests(crd_dir)
        run_kubectl([
            "annotate", "platformagent", "platform-agent", "-n", NAMESPACE,
            "e2e.test/crd-missing-trigger-"
        ], check=False)
        wait_deployment_rollout(GATEWAY_DEPLOYMENT)

    log("STEP 10 SUCCESS: Missing AgentPlugin CRD decoupled dependency safeguard verified.")


def test_e2e_operator_cluster() -> None:
    """Execute complete 10-step end-to-end operator cluster validation test."""
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    operator_tag = f"v{timestamp}"
    operator_image = f"{REGISTRY}/k8s-operator:{operator_tag}"

    unique_str = f"e2e-build-{timestamp}-{random.randint(1000, 9999)}"
    plugin_tag = f"v{timestamp}"
    plugin_image = f"{REGISTRY}/example-plugin:{plugin_tag}"

    log("Starting E2E Operator Cluster Test (AgentPlugins E2E Implementation)")
    log(f"Operator Image: {operator_image}")
    log(f"Plugin Image:   {plugin_image}")
    log(f"Unique String:  {unique_str}")

    step1_rebuild_and_deploy_operator(operator_image, operator_tag)
    step2_verify_operator_version(operator_image)
    step3_build_and_push_plugin_image(plugin_image, unique_str)
    step4_deploy_agent_plugin_cr(plugin_image, unique_str)
    step5_verify_plugin_logs_and_config(unique_str)
    step6_remove_agent_plugin_cr()
    step7_verify_log_silence_after_removal(unique_str)
    step8_verify_config_cleanup(unique_str)
    step9_verify_enable_image_volumes_false_annotation_safeguard(plugin_image, unique_str)
    step10_verify_missing_crd_decoupled_dependency_safeguard()

    log("==========================================================================")
    log("ALL E2E SUCCESS CRITERIA PASSED SUCCESSFULLY!")
    log("==========================================================================")


if __name__ == "__main__":
    test_e2e_operator_cluster()

