# HEARTBEAT.md - Platform Agent Scheduled Tasks

As the platform custodian and agent architect, you execute scheduled maintenance and verification tasks. Track your execution state inside `memory/heartbeat-state.json`.

## Automated Tasks

### 1. Agent Health & Status Audit
- **Schedule**: Every 15 minutes
- **Function**: Audit all dynamically provisioned child agents (`devteam`, `operator`). Verify heartbeats, check responsiveness, and inspect their daily logs for critical blockers.

### 2. Multi-Tenant Drift Detection
- **Schedule**: Hourly
- **Function**: Scan GKE namespaces to detect drift from standard security boundaries (e.g., deletion or modification of standard default NetworkPolicies or RBAC roles). Automatically re-enforce templates.

### 3. Fleet Resource Audit
- **Schedule**: Daily
- **Function**: High-level audit of GKE clusters, node usage, capacity allocations, and cost statistics to identify optimization or lifecycle maintenance tasks (e.g., cluster scale-downs, upgrades).

---

## State Management

Track task execution state in `memory/heartbeat-state.json`:
```json
{
  "lastChecks": {
    "agent_audit": null,
    "drift_detection": null,
    "fleet_audit": null
  }
}
```

### Execution Rules
1. **Schedule Compliance**: Compare current timestamp vs last check timestamp before running.
2. **Silence Rule (NO_REPLY)**: If all checks are successful and no anomalies or drift are detected, reply with exactly `NO_REPLY`.
