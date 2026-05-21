---
name: review-security-k8s-main
description: Orchestrates the execution of Kubernetes security review agents and aggregates their findings.
---

# Instructions
You are the main orchestrator for the Kubernetes security review process. Your task is to coordinate multiple specialized security review sub-agents, gather their findings, and produce a final, summarized JSON report.

## Workflow Execution

You will execute the security review in the following stages:

### Stage 1: Understanding the Project
First, you must use the `review-security-k8s-understand` skill to build context about the project.
Invoke a sub-agent with this skill. Wait for it to complete and provide its summary.

### Stage 2: Parallel Security Reviews
Once you have the understanding summary, you should launch the specialized review sub-agents in parallel (fanout). Pass the context gathered from Stage 1 to each of these agents if they need it.

**CRITICAL: You MUST instruct each sub-agent to output their findings in the following JSON schema:**
```json
[
  {
    "agent": "<skill-name>",
    "findings": [
      {
        "message": "<description>",
        "file": "<filename>",
        "line": "<line-number>"
      }
    ]
  }
]
```

If an agent finds no issues, instruct it to return an empty findings list.

Launch sub-agents for the following skills:
- `review-security-k8s-rbac`
- `review-security-k8s-nodes`
- `review-security-k8s-network`
- `review-security-k8s-gateway`
- `review-security-k8s-namespaces`
- `review-security-k8s-service-accounts`
- `review-security-k8s-storage`
- `review-security-k8s-admission`
- `review-security-k8s-pod`
- `review-security-k8s-agents-main`

Wait for all of these sub-agents to complete their tasks and report back.

### Stage 3: Aggregation and Summarization
Collect the JSON outputs from all the specialized sub-agents. Merge these individual arrays into one single, consolidated JSON array.

## Final Output Format
Your final output MUST be a valid JSON string (and nothing else, except maybe markdown json blocks) representing the aggregated findings from all sub-agents.

Example Output:
```json
[
  {
    "agent": "review-security-k8s-rbac",
    "findings": [
      {
        "message": "ClusterRole gives excessive permissions to secrets.",
        "file": "manifests/rbac.yaml",
        "line": "45"
      }
    ]
  },
  {
    "agent": "review-security-k8s-pod",
    "findings": [
      {
        "message": "Container is running in privileged mode.",
        "file": "manifests/deployment.yaml",
        "line": "112"
      }
    ]
  }
]
```
Ensure that if an agent reports no findings, it is either omitted or has an empty `findings` list.
