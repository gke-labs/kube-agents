---
name: review-security-k8s-agents-audit-logs
description: Reviews Kubernetes and application audit logging for AI agents, ensuring tamper-proof observability.
---
# Task
Review manifests, logging architecture, and agent configs to guarantee tamper-proof, comprehensive logging of AI agent activities.

# Checks
## 1. API Audit Coverage
- **Isolation**: Require dedicated `ServiceAccount` per agent. Flag shared or `default` accounts.
- **Audit Policy**: Ensure `AuditPolicy` captures all agent API requests (minimum `Metadata` level, prefer `RequestResponse` for mutations).

## 2. Tamper-Proof Architecture
- **Standard Streams**: Require logging to `stdout`/`stderr`. Flag local disk logging.
- **Log Isolation**: Agents MUST NOT have read/write/delete access to aggregated logs. Flag `hostPath` mounts to `/var/log/containers` or `/var/log/pods`.

## 3. Prompt & Output Auditing
- **Telemetry**: Ensure deep application logging is enabled for full LLM prompts and raw outputs.
- **Tool Execution**: Require exact input/output logging for all invoked tools (e.g. CLI, HTTP).
- **Data Scrubbing**: Require scrubbing/masking of sensitive data (PII, secrets) *before* logs are written.
