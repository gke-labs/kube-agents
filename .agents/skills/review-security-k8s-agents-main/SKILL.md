---
name: review-security-k8s-agents-main
description: Orchestrates comprehensive Kubernetes security audits specifically tailored for AI agent workloads.
---
# Task
Coordinate AI agent security review sub-agents, gather findings, and produce a summarized JSON report.

# Workflow
## 1. Context Ingestion
Pass project context (from `review-security-k8s-understand`) to sub-agents.

## 2. Parallel Reviews
Launch in parallel sub-agents:
- `review-security-k8s-agents-sandbox`
- `review-security-k8s-agents-firewall`
- `review-security-k8s-agents-credentials`
- `review-security-k8s-agents-prompt-injection`
- `review-security-k8s-agents-data-exfil`
- `review-security-k8s-agents-audit-logs`

**CRITICAL**: Instruct each to output JSON:
```json
[{"agent": "<skill-name>", "findings": [{"message": "<desc>", "file": "<name>", "line": "<num>"}]}]
```
(Return empty list if no findings). Wait for completion.

## 3. Triage & Filtering
Evaluate the raw findings against the project context to determine actual risk. Filter out findings that are functionally required by the workload's specific role or adequately mitigated by broader architectural controls. 
- *Example:* Filter out missing egress proxy warnings if the agent's execution sandbox is completely air-gapped and the main control loop is strictly allowlisted to a single LLM API.
- *Example:* Filter out root execution warnings *inside* the execution sandbox if the context confirms the sandbox utilizes a secure VM-based `RuntimeClass` (e.g. gVisor or Kata Containers) providing a secure sandbox isolation boundary.

## 4. Aggregation
Merge the filtered findings into a single JSON array. Output MUST be valid JSON string (markdown blocks okay). Omit agents with no findings or return empty `findings`.
