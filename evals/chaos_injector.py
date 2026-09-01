"""
chaos_injector.py
=================
Fault Injection Module for kube-agents Dynamic Evaluation Testbed.

Applies deterministic fault mutations to a target Kubernetes cluster using the
official Kubernetes Python API, with built-in '--mock' support for local testing.
"""

import argparse
import sys
import time
import yaml

try:
    from kubernetes import client, config
    K8S_AVAILABLE = True
except ImportError:
    K8S_AVAILABLE = False


def load_scenario(scenario_path: str) -> dict:
    """Loads and validates a ScenarioSpec YAML file."""
    with open(scenario_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def inject_fault(scenario: dict, mock: bool = False) -> bool:
    """Applies the fault mutation specified in the scenario."""
    meta = scenario.get("metadata", {})
    fault = scenario.get("fault", {})
    
    print(f"\n[⚡ CHAOS] Injecting Fault: {meta.get('name')}")
    print(f"[⚡ CHAOS] Description: {meta.get('description')}")
    print(f"[⚡ CHAOS] Target: {fault.get('target_namespace')}/{fault.get('target_deployment')}")
    
    if mock:
        print("[⚡ CHAOS] [MOCK MODE] Simulating resource constraint injection...")
        time.sleep(0.5)
        print("[⚡ CHAOS] [MOCK MODE] Pod 'recommendationservice-7df9b8f' entered CrashLoopBackOff (OOMKilled).")
        return True
    
    if not K8S_AVAILABLE:
        print("[❌ ERROR] 'kubernetes' Python package not installed. Run: pip install -r requirements.txt", file=sys.stderr)
        return False

    try:
        config.load_kube_config()
        apps_v1 = client.AppsV1Api()
        
        namespace = fault.get("target_namespace", "default")
        deployment_name = fault.get("target_deployment")
        patch_body = fault.get("mutation", {}).get("body")
        
        print(f"[⚡ CHAOS] Applying Strategic Merge Patch to Deployment '{deployment_name}'...")
        apps_v1.patch_namespaced_deployment(
            name=deployment_name,
            namespace=namespace,
            body=patch_body
        )
        print(f"[✅ CHAOS] Successfully patched {deployment_name}. Fault active.")
        return True
    except Exception as e:
        print(f"[❌ ERROR] Failed to inject fault: {e}", file=sys.stderr)
        return False


def main():
    parser = argparse.ArgumentParser(description="Chaos Fault Injector")
    parser.add_argument("--scenario", default="scenarios/online-boutique-oom-crash.yaml", help="Path to scenario YAML file")
    parser.add_argument("--mock", action="store_true", help="Run in mock mode without live cluster")
    args = parser.parse_args()
    
    scenario = load_scenario(args.scenario)
    success = inject_fault(scenario, mock=args.mock)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
