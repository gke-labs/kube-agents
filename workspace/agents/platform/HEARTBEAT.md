# HEARTBEAT.md - Platform Engineering Agent Scheduled Tasks

As the platform architect and standards enforcer for the internal developer platform, you execute routine audits and governance checks via a scheduled routine. To limit token burn and avoid redundant API calls, you must track your execution times and maintain state inside `memory/heartbeat-state.json`.

Each time you receive a heartbeat poll (triggered periodically by the gateway or host harness), you must check `memory/heartbeat-state.json` to see which tasks are due based on their schedules, execute them, and update the timestamps.

---

## Automated Tasks (Cron Jobs)

### 1. Mesh & Ingress Health Check
- **Schedule**: Every 15 minutes
- **Function**: Verify the health and responsiveness of Service Mesh (Istio/Linkerd) control planes, Ingress gateways, and API Gateway routing tiers. Alert immediately on traffic drops or gateway anomalies.

### 2. Platform Catalog Sync
- **Schedule**: Every 30 minutes
- **Function**: Synchronize active cluster workloads and services with the Internal Developer Platform (IDP / Backstage / Port) catalog to ensure accurate ownership and dependency mappings.

### 3. Policy Violation Audit
- **Schedule**: Hourly (Every 60 minutes)
- **Function**: Scan clusters for OPA Gatekeeper or Kyverno admission policy violations or warnings. Notify workload owners of non-compliant configurations.

### 4. Template Drift Detection
- **Schedule**: Daily (Every 24 hours)
- **Function**: Compare deployed team application manifests against golden baseline templates (Helm/Kustomize/Terraform). Detect configuration drift or deprecated API versions and auto-generate remediation PRs.

### 5. FinOps Allocation Audit
- **Schedule**: Daily (Every 24 hours)
- **Function**: Audit namespace labeling and resource tagging compliance across all clusters to ensure accurate cost attribution and chargeback reporting.

### 6. Shared Services Integrity Check
- **Schedule**: Daily (Every 24 hours)
- **Function**: Audit the health, capacity, and multi-tenant isolation of shared platform services (e.g., shared Redis, Kafka, cert-manager).

### 7. Platform Compliance Report
- **Schedule**: Weekly (Every 7 days)
- **Function**: Generate a comprehensive platform-wide compliance report detailing standardization metrics, IDP adoption, and overall alignment with architectural benchmarks.

---

## State Management & Rotation

Track your task execution state in `memory/heartbeat-state.json` using this schema:

```json
{
  "lastChecks": {
    "mesh_health_check": null,
    "catalog_sync": null,
    "policy_audit": null,
    "template_drift": null,
    "finops_audit": null,
    "shared_services_check": null,
    "compliance_report": null
  }
}
```

### Execution Rules
1. **Schedule Compliance**: Before running any task, compare the current timestamp against the last checked timestamp. Only run a task if the required duration (15m, 30m, 1h, 24h, 7d) has elapsed.
2. **Batching**: Batch multiple due tasks together in a single heartbeat turn when possible.
3. **Alerting & Safety**: Direct critical alerts (such as Ingress gateway failures or widespread policy breaches) to platform engineering communication channels. For minor template drift, generate automated PRs.
4. **Silence Rule (NO_REPLY)**: If all checked tasks are successful, no anomalies are found, and platform compliance is within healthy thresholds, reply with exactly `NO_REPLY` to respect quiet time.
