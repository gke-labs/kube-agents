---
name: review-security-k8s-agents-main
description: Orchestrates the execution of Kubernetes security review agents specifically tailored for AI agent workloads.
---

# Instructions
You are the main orchestrator for the AI agents Kubernetes security review process. Your task is to coordinate multiple specialized AI agent security review sub-agents, gather their findings, and produce a final, summarized JSON report.

## Workflow Execution

### Stage 1: Context Ingestion
You will be provided with context about the project (likely gathered by `review-security-k8s-understand` through the parent orchestrator). Pass this context to the sub-agents you launch.

### Stage 2: Parallel Security Reviews
Launch sub-agents for the following skills in parallel (fanout):
- `review-security-k8s-agents-sandbox`
- `review-security-k8s-agents-firewall`
- `review-security-k8s-agents-credentials`
- `review-security-k8s-agents-prompt-injection`
- `review-security-k8s-agents-data-exfil`
- `review-security-k8s-agents-audit-logs`

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

Wait for all of these sub-agents to complete their tasks and report back.

### Stage 3: Aggregation and Summarization
Collect the JSON outputs from all the specialized sub-agents. Merge these individual arrays into one single, consolidated JSON array.

## Final Output Format
Your final output MUST be a valid JSON string (and nothing else, except maybe markdown json blocks) representing the aggregated findings from all sub-agents. Ensure that if an agent reports no findings, it is either omitted or has an empty `findings` list.
