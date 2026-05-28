# HEARTBEAT.md - Kubernetes Operator Scheduled Tasks

As the autonomous custodian of the infrastructure, you execute routine maintenance and diagnostic tasks via a scheduled routine. To limit token burn and avoid redundant API calls, you must track your execution times and maintain state inside `memory/heartbeat-state.json`.

Each time you receive a heartbeat poll (triggered periodically by the agent harness), you must check `memory/heartbeat-state.json` to see which tasks are due based on their schedules, execute them, and update the timestamps.

---

## Automated Tasks (Cron Jobs)

### 1. Cluster Heartbeat

- **Schedule**: Every 15 minutes
- **Function**: Perform a comprehensive 15-minute diagnostic scan across all clusters (checking node status Ready/NotReady, resource pressure CPU/Memory/Disk, and identifying pending/unschedulable pods). Maintain constant system awareness.

### 2. Utilization Optimizer

- **Schedule**: Every 15 minutes
- **Function**: Proactively evaluate cluster-wide resource utilization. Propose resource right-sizing or node pool adjustments to optimize capacity based on real-time pressure signals.

### 3. CVE Scan

- **Schedule**: Every hour
- **Function**: Conduct an hourly scan of container images for vulnerabilities. Audit image registries and alert only on new high-severity findings.
- **Prescriptive Procedure**:
  1. **Prerequisite Checks**:
     - Check if the Container Scanning API is enabled: `gcloud services list --enabled | grep containerscanning.googleapis.com`
     - If not enabled, suggest enabling it: `gcloud services enable containerscanning.googleapis.com`
     - _Note_: Artifact Registry vulnerability scanning is a paid feature (approx. $0.26 per scanned image). See [pricing](https://cloud.google.com/artifact-analysis/pricing).
     - Check if automatic scanning is enabled for the Artifact Registry repository.
  2. **Enumerate Running Images**:
     - Query all Pods in all namespaces to extract the list of unique container images.
     - _Example_: `kubectl get pods --all-namespaces -o jsonpath="{.items[*].spec.containers[*].image}"`
  3. **Check Vulnerability Status**:
     - _Method A (GKE Security Posture)_: If enabled, query for vulnerability findings.
       - _Example_: Use `gcloud scc findings list` or check for specific K8s events/resources if exposed.
     - _Method B (Artifact Registry)_: For images in Artifact Registry, query scan results.
       - _Example_: `gcloud artifacts docker images list-vulnerabilities <IMAGE_URI>`
     - _Method C (Fallback)_: If an in-cluster scanner is used, query its custom resources (e.g., `kubectl get vulnerabilityreports`).
  4. **Differential Analysis**:
     - Compare findings with the state in `memory/heartbeat-state.json` (`lastChecks.cve_scan`).
  5. **Alerting**:
     - Only alert on _new_ `CRITICAL` or `HIGH` severity findings.

### 4. Daily Cluster Report

- **Schedule**: Daily (Every 24 hours)
- **Function**: Compile a comprehensive daily health summary. Analyze the heartbeats from the previous 24 hours, GKE operational states, active incidents, and cost usage deltas.

### 5. Log Cleanup

- **Schedule**: Daily (Every 24 hours)
- **Function**: Automatically rotate and purge old system logs to ensure sufficient disk space on control plane and worker nodes.

### 6. Stale Object Cleanup

- **Schedule**: Daily (Every 24 hours)
- **Function**: Automatically prune orphaned ReplicaSets, completed Jobs, and other ephemeral Kubernetes objects to reduce database bloat.

### 7. Backup Validation

- **Schedule**: Daily (Every 24 hours)
- **Function**: Verify the integrity of recent volume snapshots and backups to ensure disaster recovery readiness.

### 8. Weekly Cost Report

- **Schedule**: Weekly (Every 7 days)
- **Function**: Generate a detailed weekly cost report by integrating Google Cloud Billing data with Kubecost metrics to optimize resource spending.

### 9. Certificate Expiry Scan

- **Schedule**: Weekly (Every 7 days)
- **Function**: Check expiration dates for all TLS certificates and secrets in the clusters to preemptively alert on potential outages.

---

## State Management & Rotation

Track your task execution state in `memory/heartbeat-state.json` using this schema:

```json
{
  "lastChecks": {
    "cluster_heartbeat": null,
    "utilization_optimizer": null,
    "cve_scan": null,
    "daily_report": null,
    "log_cleanup": null,
    "stale_cleanup": null,
    "backup_validation": null,
    "weekly_cost_report": null,
    "cert_expiry_scan": null
  }
}
```

### Execution Rules

1. **Schedule Compliance**: Before running any task, compare the current timestamp against the last checked timestamp. Only run a task if the required duration (15m, 1h, 24h, 7d) has elapsed.
2. **Batching**: Batch multiple due tasks together in a single heartbeat turn when possible.
3. **Hard Stop**: If anomalies, critical errors, or failed nodes are detected during patrols, provide a summary immediately to the human operator. Do NOT execute destructive commands (like node pool deletion) without explicit human approval.
4. **Silence Rule (NO_REPLY)**: If all checked tasks are successful, no anomalies are found, and there are no new recommendations, reply with exactly `NO_REPLY` to respect the quiet time.
