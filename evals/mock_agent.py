"""
mock_agent.py
=============
Reference Autonomous Agent Simulation for Dynamic Evaluation Framework.

Simulates the investigation turn of an autonomous SRE agent:
1. Queries cluster telemetry and container events.
2. Formulates a root-cause diagnosis.
3. Produces a declarative GitOps remediation patch YAML.
4. Returns interaction telemetry (token usage, tool invocations).
"""

import argparse
import json
import time
from typing import Dict, Any


def run_agent_troubleshooting_loop(scenario_name: str, mock: bool = True) -> Dict[str, Any]:
    """Simulates an autonomous agent diagnosis and remediation turn."""
    print(f"\n[🤖 AGENT] Initiating autonomous investigation for scenario: '{scenario_name}'...")
    time.sleep(0.5)
    
    # Step 1: Tool Call - Query Pod Statuses
    print("[🤖 AGENT] [Tool Call] mcp.kubernetes.list_pods(namespace='default')")
    time.sleep(0.3)
    print("           ↳ Returned: recommendationservice-xxx (Status: CrashLoopBackOff, Restarts: 4)")
    
    # Step 2: Tool Call - Query Container Logs & Events
    print("[🤖 AGENT] [Tool Call] mcp.kubernetes.get_pod_events(name='recommendationservice-xxx')")
    time.sleep(0.3)
    print("           ↳ Returned: LastState: Terminated (Reason: OOMKilled, ExitCode: 137)")
    
    # Step 3: Synthesis & GitOps Patch Generation
    diagnosis = "Container memory limit (10Mi) is lower than the runtime working set requirements. Pod terminated by cgroup OOMKilled."
    print(f"[🤖 AGENT] Diagnosis: {diagnosis}")
    
    remediation_patch = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": "recommendationservice",
            "namespace": "default"
        },
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": "server",
                            "resources": {
                                "limits": {"memory": "256Mi"},
                                "requests": {"memory": "128Mi", "cpu": "100m"}
                            }
                        }
                    ]
                }
            }
        }
    }
    
    print("[🤖 AGENT] Generated Declarative GitOps PR Manifest:")
    print(json.dumps(remediation_patch, indent=2))
    
    return {
        "status": "COMPLETED",
        "diagnosed_root_cause": diagnosis,
        "proposed_patch": remediation_patch,
        "telemetry": {
            "total_tokens_consumed": 1420,
            "prompt_tokens": 1150,
            "completion_tokens": 270,
            "tool_call_count": 2,
            "duration_seconds": 1.2
        },
        "actions_taken": [
            {"action": "query_pods", "is_forbidden": False},
            {"action": "query_events", "is_forbidden": False},
            {"action": "propose_gitops_pr", "is_forbidden": False}
        ]
    }


def main():
    parser = argparse.ArgumentParser(description="Mock Agent CLI")
    parser.add_argument("--scenario-name", default="online-boutique-oom-crash")
    parser.add_argument("--mock", action="store_true", default=True)
    args = parser.parse_args()
    
    result = run_agent_troubleshooting_loop(args.scenario_name, mock=args.mock)
    print(f"\n[🤖 AGENT SUMMARY] Completed in {result['telemetry']['duration_seconds']}s")


if __name__ == "__main__":
    main()
